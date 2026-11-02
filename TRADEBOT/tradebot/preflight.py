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
        "jupiter_swap_v2_order": f"{jup.swap}/order?inputMint={SOL_MINT}&outputMint={USDC_MINT}&amount=1000000&slippageBps=50",
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
    rows.append(("jupiter_api_key", "OK" if jup.has_key else "FAIL",
                 f"{jup.base} (x-api-key set)" if jup.has_key else "missing: Jupiter REST requires an x-api-key (free at developers.jup.ag); set JUPITER_API_KEY"))
    for name, url in hosts(settings).items():
        code, detail = _get(url, headers=jup.headers if name.startswith("jupiter") else None)
        if code is None:
            rows.append((f"net:{name}", "FAIL", detail))
        elif name == "anthropic" and code == 401:
            rows.append((f"net:{name}", "OK", "reachable (401 without key is expected)"))
        elif name.startswith("jupiter") and code == 401 and not jup.has_key:
            rows.append((f"net:{name}", "WARN", "reachable, but 401 without x-api-key"))
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
    order_info: dict | None = None   # Swap v2 order details: router, mode, fee bps, network fee estimates


def jupiter_quote(settings: Settings, in_symbol: str, out_symbol: str, amount: float, slippage_bps: int | None = None,
                  wallet: str | None = None) -> QuoteReport:
    mints, decs = settings.universe.mints, settings.universe.decimals
    if in_symbol not in mints or out_symbol not in mints:
        raise ValueError(f"unknown symbol; known: {sorted(mints)}")
    slip = slippage_bps if slippage_bps is not None else settings.execution.live_slippage_bps
    raw_in = int(round(amount * 10 ** decs[in_symbol]))
    jup = jupiter_endpoints(settings.data, settings.execution)
    if not jup.has_key:
        raise RuntimeError("JUPITER_API_KEY is required for Jupiter quotes (free key at developers.jup.ag)")
    # Swap API v2: GET /order WITHOUT taker is the read-only quote (transaction is null). /swap/v1/quote is deprecated.
    r = httpx.get(f"{jup.swap}/order", timeout=settings.data.jupiter_quote_timeout_sec, headers=jup.headers,
                  params={"inputMint": mints[in_symbol], "outputMint": mints[out_symbol], "amount": str(raw_in), "slippageBps": slip})
    r.raise_for_status()
    q = r.json()
    if q.get("error"):
        raise RuntimeError(f"order error: {q['error']}")
    by_mint = {m: s for s, m in mints.items()}
    route = []
    for hop in q.get("routePlan", []):
        si = hop.get("swapInfo", {})
        fee_sym = by_mint.get(si.get("feeMint"), str(si.get("feeMint") or "")[:6])
        route.append({"label": si.get("label"), "percent": (hop.get("bps", 0) / 100) if "bps" in hop else hop.get("percent"),
                      "in": int(si.get("inAmount", 0)) / 10 ** decs.get(by_mint.get(si.get("inputMint"), ""), 6),
                      "out": int(si.get("outAmount", 0)) / 10 ** decs.get(by_mint.get(si.get("outputMint"), ""), 6),
                      "lp_fee": int(si.get("feeAmount", 0)) / 10 ** decs.get(fee_sym, 6), "lp_fee_symbol": fee_sym})
    rep = QuoteReport(in_symbol=in_symbol, out_symbol=out_symbol, in_amount=int(q["inAmount"]) / 10 ** decs[in_symbol],
                      out_amount=int(q["outAmount"]) / 10 ** decs[out_symbol],
                      min_out_amount=int(q.get("otherAmountThreshold", q["outAmount"])) / 10 ** decs[out_symbol],
                      price_impact_pct=float(q.get("priceImpactPct") or 0.0), slippage_bps=int(q.get("slippageBps", slip)),
                      route=route, platform_fee=None, priority_fee_sol=int(q.get("prioritizationFeeLamports") or 0) / LAMPORTS,
                      context_slot=q.get("contextSlot"))
    rep.order_info = {"router": q.get("router"), "mode": q.get("mode"), "swap_type": q.get("swapType"), "fee_bps": q.get("feeBps"),
                 "signature_fee_sol": int(q.get("signatureFeeLamports") or BASE_FEE_LAMPORTS) / LAMPORTS,
                 "priority_fee_sol": int(q.get("prioritizationFeeLamports") or 0) / LAMPORTS,
                 "rent_fee_sol": int(q.get("rentFeeLamports") or 0) / LAMPORTS, "out_amount": rep.out_amount,
                 "price_impact_pct": rep.price_impact_pct, "gasless": q.get("gasless"), "error": None}
    if {in_symbol, out_symbol} == {"SOL", "USDC"} and rep.in_amount > 0:
        rep.sol_price_usd = rep.out_amount / rep.in_amount if in_symbol == "SOL" else rep.in_amount / rep.out_amount
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
    if rep.order_info:
        u = rep.order_info
        L.append(f"Swap v2 order: router {u.get('router')} mode {u.get('mode')} type {u.get('swap_type')}, Jupiter fee {u.get('fee_bps')} bps, "
                 f"signature fee {u['signature_fee_sol']:.9f} SOL, priority fee {u['priority_fee_sol']:.9f} SOL, rent {u['rent_fee_sol']:.9f} SOL, gasless={u.get('gasless')}")
    px = rep.sol_price_usd or 0.0
    usd = (lambda sol: f" (~${sol*px:.4f})" if px else "")
    needs_ata = rep.wallet_has_out_ata is not True
    ata = rep.ata_rent_sol if needs_ata else 0.0
    L += ["Network costs (payer side, in SOL):",
          f"  base tx fee            {rep.base_fee_sol:.9f}{usd(rep.base_fee_sol)}",
          f"  priority fee (order)  {rep.priority_fee_sol:.9f}{usd(rep.priority_fee_sol)}",
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
    """Balances by public key via the Solana RPC (the Jupiter Ultra balances endpoint is deprecated)."""
    by_mint = {m: s for s, m in settings.universe.mints.items()}
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


# ----------------------------------------------------------------------------- asset verification
def verify_assets(settings: Settings, symbols: list[str] | None = None, equity_usd: float | None = None,
                  record: bool = True) -> list[dict]:
    """Read-only check that each traded symbol can actually be executed through Jupiter at the bot's order sizes,
    within the risk limits, and that its signal data (Binance candles) tracks the Jupiter-traded asset.
    Nothing is signed or sent. Each run is appended to <data_dir>/verify_assets.jsonl so costs are on record.

    Round-trip cost per size = 1 - USDC back / USDC in, for USDC -> asset -> USDC using two size-specific
    `/swap/v2/order` quotes without a taker. `expected` uses outAmount, `worst_case` uses otherAmountThreshold
    (the minimum the swap would accept at the configured slippage) on both legs, plus two Solana network fees."""
    from .data.live import LiveProvider
    from .execution.paper import SOLANA_TX_FEE_USD
    jup = jupiter_endpoints(settings.data, settings.execution)
    prov = LiveProvider(settings.data, settings.universe, settings.execution)
    if not jup.has_key:
        return [{"symbol": sym, "status": "FAIL", "problems": ["JUPITER_API_KEY missing: Jupiter REST requires an x-api-key (free at developers.jup.ag)"],
                 "order_usd": 0.0} for sym in (symbols or settings.universe.symbols)]
    eq = equity_usd or settings.initial_capital_usd
    intended = min(eq * settings.risk.max_position_pct / 100, settings.risk.max_order_notional_usd)
    sizes = sorted({round(settings.risk.min_order_notional_usd, 2), round(intended, 2), round(settings.risk.max_order_notional_usd, 2)})
    assumed_rt = 2 * (settings.execution.paper_fee_bps + settings.execution.paper_slippage_bps)
    usdc = settings.universe.mints["USDC"]
    try:
        refs = prov.get_reference_prices(list(symbols or settings.universe.symbols))
    except Exception as e:  # noqa: BLE001
        refs = {}; ref_err = f"{type(e).__name__}: {e}"
    else:
        ref_err = ""
    rows = []
    for sym in symbols or settings.universe.symbols:
        r: dict = {"symbol": sym, "order_usd": round(intended, 2), "sizes_usd": sizes, "status": "OK", "problems": []}
        mint = settings.universe.mints.get(sym); dec = settings.universe.decimals.get(sym)
        r["mint"] = mint; r["decimals"] = dec
        if not mint or dec is None:
            r["problems"].append("no mint/decimals configured"); r["status"] = "FAIL"; rows.append(r); continue
        # tickers: price + liquidity + volume (Jupiter price v3 + tokens v2, dexscreener fallback)
        try:
            t = prov.get_tickers([sym]).get(sym)
            if t is None:
                raise RuntimeError("no ticker")
            r["price"] = t.price; r["liquidity_usd"] = round(t.liquidity_usd); r["volume_24h_usd"] = round(t.volume_24h_usd)
            if t.liquidity_usd < settings.risk.min_liquidity_usd:
                r["problems"].append(f"liquidity ${t.liquidity_usd:,.0f} < min ${settings.risk.min_liquidity_usd:,.0f}")
            if t.volume_24h_usd < settings.risk.min_volume_24h_usd:
                r["problems"].append(f"24h volume ${t.volume_24h_usd:,.0f} < min ${settings.risk.min_volume_24h_usd:,.0f}")
        except Exception as e:  # noqa: BLE001
            r["problems"].append(f"ticker: {type(e).__name__}: {e}")
        # signal instrument vs traded asset, measured at the same instant (Binance spot vs Jupiter price)
        ref = refs.get(sym)
        if ref and r.get("price"):
            r["reference_price"] = ref
            r["basis_bps"] = round((r["price"] / ref - 1) * 1e4, 1)
            if abs(r["basis_bps"]) > settings.data.max_basis_bps:
                r["problems"].append(f"Jupiter vs Binance spot basis {r['basis_bps']:+.0f} bps exceeds max_basis_bps {settings.data.max_basis_bps:.0f}")
        elif ref_err:
            r["problems"].append(f"reference price: {ref_err}")
        # candles for signals
        try:
            c = prov.get_candles(sym, settings.data.candle_interval, settings.data.candle_history)
            r["candles"] = len(c); r["last_candle_close"] = c[-1].close if c else None
            need = max(s.required_bars(c) for s in _strategies(settings)) if c else 0
            if len(c) < need:
                r["problems"].append(f"only {len(c)} candles, strategies need {need}")
            if c and r.get("price"):
                r["basis_pct"] = round((r["price"] / c[-1].close - 1) * 100, 3)   # vs last COMPLETED candle (can be hours old)
                if abs(r["basis_pct"]) > 2.0 and "basis_bps" not in r:
                    r["problems"].append(f"Jupiter price vs last Binance close {r['basis_pct']}% (> 2%)")
        except Exception as e:  # noqa: BLE001
            r["problems"].append(f"candles: {type(e).__name__}: {e}")
        # round-trip quotes at every order size the bot could send
        r["round_trips"] = []
        for size in sizes:
            try:
                r["round_trips"].append(_round_trip(jup, settings, usdc, mint, dec, size, SOLANA_TX_FEE_USD))
            except Exception as e:  # noqa: BLE001
                r["problems"].append(f"quote ${size}: {type(e).__name__}: {e}")
        main = next((x for x in r["round_trips"] if x["size_usd"] == round(intended, 2)), None)
        if main:
            r.update({k: main[k] for k in ("jupiter_fee_bps", "router", "route", "buy_impact_bps", "sell_impact_bps", "round_trip_cost_bps", "worst_case_cost_bps")})
            if max(main["buy_impact_bps"], main["sell_impact_bps"]) > settings.risk.max_slippage_bps:
                r["problems"].append(f"price impact > max_slippage_bps {settings.risk.max_slippage_bps}")
            if main["round_trip_cost_bps"] > assumed_rt:
                r["problems"].append(f"round-trip cost {main['round_trip_cost_bps']} bps exceeds the cost assumed in research ({assumed_rt:.0f} bps)")
            if main["worst_case_cost_bps"] > 2 * assumed_rt:
                r["problems"].append(f"worst-case (min-out) round trip {main['worst_case_cost_bps']} bps is more than twice the research assumption")
        for x in r["round_trips"]:
            if x["size_usd"] != round(intended, 2) and x["round_trip_cost_bps"] > assumed_rt:
                r["problems"].append(f"at ${x['size_usd']}: round trip {x['round_trip_cost_bps']} bps > assumed {assumed_rt:.0f} bps")
        if r["problems"]:
            r["status"] = "FAIL" if any(p.startswith(("quote", "ticker", "candles", "no mint")) for p in r["problems"]) else "WARN"
        rows.append(r)
    if record:
        try:
            import json, os, time
            os.makedirs(settings.data_dir, exist_ok=True)
            with open(os.path.join(settings.data_dir, "verify_assets.jsonl"), "a") as f:
                f.write(json.dumps({"ts": time.time(), "equity_usd": eq, "assumed_round_trip_bps": assumed_rt, "rows": rows}, default=str) + "\n")
        except OSError:
            pass
    return rows


def _round_trip(jup, settings: Settings, usdc: str, mint: str, dec: int, size_usd: float, tx_fee_usd: float) -> dict:
    """Two read-only Swap v2 quotes (no taker => transaction null): USDC -> asset at size_usd, then the received amount back."""
    params = {"inputMint": usdc, "outputMint": mint, "amount": str(int(round(size_usd * 1e6))), "slippageBps": settings.execution.live_slippage_bps}
    q_buy = httpx.get(f"{jup.swap}/order", timeout=settings.data.jupiter_quote_timeout_sec, headers=jup.headers, params=params)
    q_buy.raise_for_status(); qb = q_buy.json()
    if qb.get("error"):
        raise RuntimeError(qb["error"])
    out_raw = int(qb["outAmount"])
    q_sell = httpx.get(f"{jup.swap}/order", timeout=settings.data.jupiter_quote_timeout_sec, headers=jup.headers,
                       params={"inputMint": mint, "outputMint": usdc, "amount": str(out_raw), "slippageBps": settings.execution.live_slippage_bps})
    q_sell.raise_for_status(); qs = q_sell.json()
    if qs.get("error"):
        raise RuntimeError(qs["error"])
    back_usd = int(qs["outAmount"]) / 1e6
    worst_back = int(qs.get("otherAmountThreshold") or qs["outAmount"]) / 1e6
    # worst case also assumes the BUY leg only delivered its minimum: scale the sell proceeds by min-out / expected-out
    buy_min_ratio = int(qb.get("otherAmountThreshold") or qb["outAmount"]) / out_raw if out_raw else 1.0
    fees = 2 * tx_fee_usd
    return {"size_usd": round(size_usd, 2), "jupiter_fee_bps": qb.get("feeBps"), "router": qb.get("router"), "mode": qb.get("mode"),
            "route": "+".join(h["swapInfo"]["label"] for h in qb.get("routePlan", []))[:60],
            "buy_impact_bps": round(float(qb.get("priceImpactPct") or 0) * 1e4, 1), "sell_impact_bps": round(float(qs.get("priceImpactPct") or 0) * 1e4, 1),
            "usdc_back": round(back_usd, 4), "network_fees_usd": fees,
            "round_trip_cost_bps": round((1 - (back_usd - fees) / size_usd) * 1e4, 1),
            "worst_case_cost_bps": round((1 - (worst_back * buy_min_ratio - fees) / size_usd) * 1e4, 1)}


def _strategies(settings: Settings):
    from .strategies.registry import build_strategies
    return build_strategies(settings.strategies)


def format_assets(rows: list[dict]) -> str:
    L = ["=== asset verification (read-only, nothing signed) ==="]
    for r in rows:
        L.append(f"{r['status']:<5}{r['symbol']:<6} mint {str(r.get('mint'))[:10]}… dec {r.get('decimals')}  price {r.get('price', 'n/a')}  "
                 f"liq ${r.get('liquidity_usd', 0):,}  vol24h ${r.get('volume_24h_usd', 0):,}  candles {r.get('candles', 'n/a')}  "
                 f"basis vs Binance spot {r.get('basis_bps', 'n/a')} bps (vs last close {r.get('basis_pct', 'n/a')}%)")
        for x in r.get("round_trips", []):
            L.append(f"        ${x['size_usd']:<8} round trip {x['round_trip_cost_bps']} bps expected / {x['worst_case_cost_bps']} bps worst-case  "
                     f"(jup fee {x.get('jupiter_fee_bps')} bps, impact {x['buy_impact_bps']}/{x['sell_impact_bps']} bps, router {x.get('router')}, {x.get('route')})")
        for p in r["problems"]:
            L.append(f"        - {p}")
    worst = max((["OK", "WARN", "FAIL"].index(r["status"]) for r in rows), default=0)
    L.append("result: " + ["all assets executable within limits", "warnings: review before paper trading", "FAIL: some assets cannot be traded as configured"][worst])
    return "\n".join(L)
