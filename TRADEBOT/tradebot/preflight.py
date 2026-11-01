"""Read-only diagnostics: network reachability, configuration, a real Jupiter quote with a full
fee/rent/slippage breakdown, and wallet balances by public key. Nothing here signs or sends."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import httpx

from .config import Settings, jupiter_endpoints

LAMPORTS = 1_000_000_000
BASE_FEE_LAMPORTS = 5_000                 # per signature
TOKEN_ACCOUNT_RENT_LAMPORTS = 2_039_280   # rent-exempt minimum for a 165-byte SPL token account (recoverable on close)
SYSTEM_ACCOUNT_MIN_LAMPORTS = 890_880     # a wallet left non-zero but below this fails "insufficient funds for rent"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def hosts(settings: Settings) -> dict[str, str]:
    jup = jupiter_endpoints(settings.data, settings.execution)
    return {
        "jupiter_price_v3": f"{jup.price}?ids={SOL_MINT}",
        "jupiter_tokens_v2": f"{jup.tokens}/search?query={SOL_MINT}",
        "jupiter_swap_quote": f"{jup.swap}/quote?inputMint={SOL_MINT}&outputMint={USDC_MINT}&amount=1000000&slippageBps=50",
        "jupiter_ultra_order": f"{jup.ultra}/order?inputMint={SOL_MINT}&outputMint={USDC_MINT}&amount=1000000",
        "binance_klines": f"{settings.data.binance_base_url}/api/v3/klines?symbol=SOLUSDT&interval=1h&limit=1",
        "dexscreener": f"{settings.data.dexscreener_base_url}/latest/dex/tokens/JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
        "coingecko": f"{settings.data.coingecko_base_url}/ping",
        "anthropic": "https://api.anthropic.com/v1/models",
    }


def _get(url: str, timeout: float = 12.0, headers: dict | None = None) -> tuple[int | None, str]:
    try:
        r = httpx.get(url, timeout=timeout, headers=headers or {})
        return r.status_code, r.text[:300]
    except httpx.HTTPError as e:
        return None, f"{type(e).__name__}: {e}"


def _rpc(url: str, method: str, params: list, timeout: float = 12.0) -> dict:
    r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"rpc {method}: {data['error']}")
    return data["result"]


# ----------------------------------------------------------------------------- preflight
def preflight(settings: Settings) -> list[tuple[str, str, str]]:
    """Returns rows of (check, status, detail). status in {OK, WARN, FAIL}."""
    rows: list[tuple[str, str, str]] = []
    rows.append(("mode", "OK" if settings.mode == "paper" else "WARN", settings.mode))
    key = os.environ.get("ANTHROPIC_API_KEY")
    rows.append(("ANTHROPIC_API_KEY", "OK" if key else "WARN",
                 "set" if key else "missing: Claude research/vetting disabled, deterministic strategies still run"))
    jup = jupiter_endpoints(settings.data, settings.execution)
    rows.append(("jupiter_base", "OK", f"{jup.base} ({'API key set' if jup.headers else 'free tier, no key'})"))
    for name, url in hosts(settings).items():
        code, detail = _get(url, headers=jup.headers if name.startswith("jupiter") else None)
        if code is None:
            rows.append((f"net:{name}", "FAIL", detail))
        elif name == "anthropic" and code == 401:
            rows.append((f"net:{name}", "OK", "reachable (401 without key is expected)"))
        elif 200 <= code < 300:
            rows.append((f"net:{name}", "OK", f"HTTP {code}"))
        else:
            rows.append((f"net:{name}", "WARN", f"HTTP {code}: {detail[:120]}"))
    try:
        res = _rpc(settings.execution.rpc_url, "getHealth", [])
        rows.append(("net:solana_rpc", "OK", str(res)))
    except Exception as e:  # noqa: BLE001
        rows.append(("net:solana_rpc", "FAIL", f"{type(e).__name__}: {e}"))
    rows.append(("kill_switch", "WARN" if settings.kill_file.exists() else "OK",
                 "TRIPPED (file present)" if settings.kill_file.exists() else "clear"))
    rows.append(("live_gate", "WARN" if settings.armed_file.exists() else "OK",
                 "armed file present" if settings.armed_file.exists() else "not armed (paper only)"))
    rows.append(("SOLANA_PRIVATE_KEY", "OK", "set (not displayed)" if os.environ.get("SOLANA_PRIVATE_KEY") else "not set (fine for paper)"))
    return rows


# ----------------------------------------------------------------------------- quote
@dataclass
class QuoteReport:
    in_symbol: str
    out_symbol: str
    in_amount: float
    out_amount: float
    min_out_amount: float
    price_impact_pct: float
    slippage_bps: int
    route: list[dict] = field(default_factory=list)
    platform_fee: dict | None = None
    sol_price_usd: float | None = None
    base_fee_sol: float = BASE_FEE_LAMPORTS / LAMPORTS
    priority_fee_sol: float = 0.0
    ata_rent_sol: float = TOKEN_ACCOUNT_RENT_LAMPORTS / LAMPORTS
    keep_min_sol: float = SYSTEM_ACCOUNT_MIN_LAMPORTS / LAMPORTS
    wallet_sol: float | None = None
    wallet_has_out_ata: bool | None = None
    context_slot: int | None = None
    ultra: dict | None = None   # Ultra order fee breakdown when available


def jupiter_quote(settings: Settings, in_symbol: str, out_symbol: str, amount: float, slippage_bps: int | None = None,
                  wallet: str | None = None) -> QuoteReport:
    mints, decs = settings.universe.mints, settings.universe.decimals
    if in_symbol not in mints or out_symbol not in mints:
        raise ValueError(f"unknown symbol; known: {sorted(mints)}")
    slip = slippage_bps if slippage_bps is not None else settings.execution.live_slippage_bps
    raw_in = int(round(amount * 10 ** decs[in_symbol]))
    jup = jupiter_endpoints(settings.data, settings.execution)
    r = httpx.get(f"{jup.swap}/quote", timeout=settings.data.request_timeout_sec, headers=jup.headers,
                  params={"inputMint": mints[in_symbol], "outputMint": mints[out_symbol], "amount": str(raw_in),
                          "slippageBps": slip, "restrictIntermediateTokens": "true"})
    r.raise_for_status()
    q = r.json()
    by_mint = {m: s for s, m in mints.items()}
    route = []
    for hop in q.get("routePlan", []):
        si = hop.get("swapInfo", {})
        fee_mint = si.get("feeMint")
        fee_sym = by_mint.get(fee_mint, (fee_mint or "")[:6])
        fee_dec = decs.get(fee_sym, 6)
        route.append({"label": si.get("label"), "percent": hop.get("percent"),
                      "in": int(si.get("inAmount", 0)) / 10 ** decs.get(by_mint.get(si.get("inputMint"), ""), 6),
                      "out": int(si.get("outAmount", 0)) / 10 ** decs.get(by_mint.get(si.get("outputMint"), ""), 6),
                      "lp_fee": int(si.get("feeAmount", 0)) / 10 ** fee_dec, "lp_fee_symbol": fee_sym})
    rep = QuoteReport(in_symbol=in_symbol, out_symbol=out_symbol, in_amount=int(q["inAmount"]) / 10 ** decs[in_symbol],
                      out_amount=int(q["outAmount"]) / 10 ** decs[out_symbol],
                      min_out_amount=int(q.get("otherAmountThreshold", q["outAmount"])) / 10 ** decs[out_symbol],
                      price_impact_pct=float(q.get("priceImpactPct") or 0.0), slippage_bps=int(q.get("slippageBps", slip)),
                      route=route, platform_fee=q.get("platformFee"), priority_fee_sol=settings.execution.priority_fee_lamports / LAMPORTS,
                      context_slot=q.get("contextSlot"))
    # SOL price from the quote itself when one side is SOL and the other USDC
    if {in_symbol, out_symbol} == {"SOL", "USDC"} and rep.in_amount > 0:
        rep.sol_price_usd = rep.out_amount / rep.in_amount if in_symbol == "SOL" else rep.in_amount / rep.out_amount
    # Ultra order (read-only: nothing is signed or executed) reports Jupiter's own fee estimate incl. rent & priority fee.
    try:
        params = {"inputMint": mints[in_symbol], "outputMint": mints[out_symbol], "amount": str(raw_in), "slippageBps": slip}
        if wallet:
            params["taker"] = wallet
        ro = httpx.get(f"{jup.ultra}/order", timeout=settings.data.request_timeout_sec, headers=jup.headers, params=params)
        ro.raise_for_status()
        o = ro.json()
        rep.ultra = {"router": o.get("router"), "swap_type": o.get("swapType"), "fee_bps": o.get("feeBps"),
                     "signature_fee_sol": int(o.get("signatureFeeLamports") or 0) / LAMPORTS,
                     "priority_fee_sol": int(o.get("prioritizationFeeLamports") or 0) / LAMPORTS,
                     "rent_fee_sol": int(o.get("rentFeeLamports") or 0) / LAMPORTS,
                     "out_amount": int(o["outAmount"]) / 10 ** decs[out_symbol] if o.get("outAmount") else None,
                     "price_impact_pct": float(o.get("priceImpactPct") or 0.0), "gasless": o.get("gasless"),
                     "error": o.get("errorMessage")}
    except (httpx.HTTPError, ValueError, KeyError) as e:
        rep.ultra = {"error": f"{type(e).__name__}: {e}"}
    if wallet:
        bal = wallet_balances(settings, wallet)
        rep.wallet_sol = bal["SOL"]
        rep.wallet_has_out_ata = out_symbol in bal["token_accounts"]
    return rep


def format_quote(rep: QuoteReport) -> str:
    L = [f"=== Jupiter quote (read-only, nothing signed) {rep.in_amount:.9g} {rep.in_symbol} -> {rep.out_symbol} ===",
         f"Expected out      {rep.out_amount:.9g} {rep.out_symbol}",
         f"Min out (slip)    {rep.min_out_amount:.9g} {rep.out_symbol}   (slippage tolerance {rep.slippage_bps} bps = {rep.slippage_bps/100:.2f}%)",
         f"Price impact      {rep.price_impact_pct*100:.4f}%",
         f"Context slot      {rep.context_slot}"]
    if rep.sol_price_usd:
        L.append(f"Implied SOL price ${rep.sol_price_usd:,.2f}")
    L.append("Route:")
    for h in rep.route:
        L.append(f"  {h['label']:<22} {h['percent']:>3}%  {h['in']:.9g} -> {h['out']:.9g}   LP fee {h['lp_fee']:.9g} {h['lp_fee_symbol']}")
    L.append(f"Jupiter platform fee (Swap API): {rep.platform_fee or 'none'}")
    if rep.ultra:
        u = rep.ultra
        if u.get("error") and u.get("out_amount") is None:
            L.append(f"Ultra order estimate: unavailable ({u['error']})")
        else:
            L.append(f"Ultra order estimate ({u.get('router')}, {u.get('swap_type')}): out {u['out_amount']:.9g} {rep.out_symbol}, "
                     f"Jupiter fee {u.get('fee_bps')} bps, signature fee {u['signature_fee_sol']:.9f} SOL, "
                     f"priority fee {u['priority_fee_sol']:.9f} SOL, rent {u['rent_fee_sol']:.9f} SOL, impact {u['price_impact_pct']*100:.4f}%")
    px = rep.sol_price_usd or 0.0
    usd = (lambda sol: f" (~${sol*px:.4f})" if px else "")
    needs_ata = rep.wallet_has_out_ata is not True
    ata = rep.ata_rent_sol if needs_ata else 0.0
    L += ["Network costs (payer side, in SOL):",
          f"  base tx fee            {rep.base_fee_sol:.9f}{usd(rep.base_fee_sol)}",
          f"  priority fee (config)  {rep.priority_fee_sol:.9f}{usd(rep.priority_fee_sol)}",
          f"  {rep.out_symbol} token account rent  {rep.ata_rent_sol:.9f}{usd(rep.ata_rent_sol)}  "
          + ("(wallet already has one: not charged)" if rep.wallet_has_out_ata else "(charged once if the wallet has no such account; refundable on close)"),
          f"  wallet rent-exempt floor to keep  {rep.keep_min_sol:.9f}{usd(rep.keep_min_sol)}"]
    total = rep.base_fee_sol + rep.priority_fee_sol + ata
    if rep.in_symbol == "SOL":
        need = rep.in_amount + total + rep.keep_min_sol
        L.append(f"SOL required for this swap: {rep.in_amount:.9f} (swap) + {total:.9f} (fees/rent) + {rep.keep_min_sol:.9f} (floor) = {need:.9f}{usd(need)}")
        if rep.wallet_sol is not None:
            verdict = "YES" if rep.wallet_sol >= need else "NO"
            L.append(f"Wallet SOL balance: {rep.wallet_sol:.9f}{usd(rep.wallet_sol)} -> can fund this swap: {verdict}"
                     + ("" if verdict == "YES" else f" (short by {need - rep.wallet_sol:.9f} SOL)"))
    return "\n".join(L)


# ----------------------------------------------------------------------------- wallet
def wallet_balances(settings: Settings, address: str) -> dict:
    """Balances by public key. Tries the RPC-free Jupiter Ultra balances endpoint first, then the RPC."""
    jup = jupiter_endpoints(settings.data, settings.execution)
    by_mint = {m: s for s, m in settings.universe.mints.items()}
    try:
        r = httpx.get(f"{jup.ultra}/balances/{address}", timeout=settings.data.request_timeout_sec, headers=jup.headers)
        r.raise_for_status()
        data = r.json()
        sol = data.get("SOL", {})
        out = {"address": address, "SOL": float(sol.get("uiAmount") or 0.0), "lamports": int(sol.get("amount") or 0),
               "token_accounts": {}, "source": "jupiter_ultra"}
        for mint, v in data.items():
            if mint == "SOL":
                continue
            out["token_accounts"][by_mint.get(mint, mint[:8] + "…")] = {"amount": float(v.get("uiAmount") or 0.0), "mint": mint,
                                                                         "rent_locked_sol": TOKEN_ACCOUNT_RENT_LAMPORTS / LAMPORTS}
        return out
    except (httpx.HTTPError, ValueError) as e:
        pass
    rpc = settings.execution.rpc_url
    lamports = _rpc(rpc, "getBalance", [address])["value"]
    out = {"address": address, "SOL": lamports / LAMPORTS, "lamports": lamports, "token_accounts": {}, "source": "rpc"}
    res = _rpc(rpc, "getTokenAccountsByOwner", [address, {"programId": TOKEN_PROGRAM}, {"encoding": "jsonParsed"}])
    for acc in res.get("value", []):
        info = acc["account"]["data"]["parsed"]["info"]
        sym = by_mint.get(info["mint"], info["mint"][:8] + "…")
        out["token_accounts"][sym] = {"amount": float(info["tokenAmount"]["uiAmount"] or 0.0), "mint": info["mint"],
                                      "rent_locked_sol": acc["account"]["lamports"] / LAMPORTS}
    return out


def format_wallet(bal: dict, sol_price_usd: float | None = None) -> str:
    px = sol_price_usd
    L = [f"=== wallet {bal['address']} (read-only) ===",
         f"SOL  {bal['SOL']:.9f}" + (f"  (~${bal['SOL']*px:.2f} at ${px:,.2f}/SOL)" if px else "")]
    for sym, t in bal["token_accounts"].items():
        L.append(f"{sym:<5}{t['amount']:.6f}   (token account holds {t['rent_locked_sol']:.9f} SOL rent)")
    if not bal["token_accounts"]:
        L.append("no SPL token accounts (a USDC account would cost 0.00203928 SOL rent to create)")
    spendable = bal["SOL"] - SYSTEM_ACCOUNT_MIN_LAMPORTS / LAMPORTS
    L.append(f"SOL above rent-exempt floor: {max(0.0, spendable):.9f}")
    return "\n".join(L)
