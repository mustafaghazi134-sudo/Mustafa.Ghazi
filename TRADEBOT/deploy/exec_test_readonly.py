"""READ-ONLY rehearsal of the execution test on real Jupiter routes and the real chain. Signs nothing, submits nothing.

1. Quote-only round trip at the test amount, with every fee field Jupiter reports.
2. Route verification and simulation of the exact two transactions (buy $USD of SOL with USDC, sell that SOL back)
   for:
   - the owner's test wallet, if deploy/exec_test_wallet.txt holds its PUBLIC address (balances shown; simulation
     needs it funded);
   - a funded wallet taken from a recent Jupiter swap on chain, used ONLY as the taker for building and simulating.
     Nothing is sent and nothing affects that wallet.

usage: TRADEBOT_CONFIG=deploy/paper_ci.toml python deploy/exec_test_readonly.py [usd]
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx
from solders.pubkey import Pubkey

from tradebot.config import load_settings
from tradebot.data.live import LiveProvider
from tradebot.execution import tx_verify as V
from tradebot.execution.jupiter_adapter import JupiterExecutionAdapter
from tradebot.models import Side
from tradebot.risk.engine import RiskEngine
from tradebot.risk.kill_switch import KillSwitch

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
JUP6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
FEE_FIELDS = ("router", "inAmount", "outAmount", "otherAmountThreshold", "slippageBps", "feeBps", "priceImpact", "signatureFeeLamports",
              "prioritizationFeeLamports", "rentFeeLamports", "gasless")


def rpc(url, method, params):
    for attempt in range(4):
        r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
        if r.status_code == 429:
            time.sleep(3 * (attempt + 1)); continue
        r.raise_for_status()
        j = r.json()
        if "error" in j:
            raise RuntimeError(j["error"])
        return j["result"]
    raise RuntimeError(f"{method}: rate limited")


def funded_wallet_from_recent_swaps(url, need_usdc: float, need_sol: float) -> str | None:
    sigs = rpc(url, "getSignaturesForAddress", [JUP6, {"limit": 120}])
    seen = set()
    for s in sigs:
        if s.get("err"):
            continue
        time.sleep(0.35)
        try:
            tx = rpc(url, "getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            payer = tx["transaction"]["message"]["accountKeys"][0]["pubkey"]
        except Exception:  # noqa: BLE001
            continue
        if payer in seen or not Pubkey.from_string(payer).is_on_curve():
            continue
        seen.add(payer)
        try:
            lam = rpc(url, "getBalance", [payer])["value"]
            toks = rpc(url, "getTokenAccountsByOwner", [payer, {"mint": USDC}, {"encoding": "jsonParsed"}])["value"]
            usdc = sum(float(t["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for t in toks)
        except Exception:  # noqa: BLE001
            continue
        ata = V.ata_address(payer, USDC)
        in_ata = sum(float(t["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for t in toks if t["pubkey"] == ata)
        if lam / 1e9 >= need_sol and in_ata >= need_usdc:
            return payer
    return None


def dump_route(a: JupiterExecutionAdapter, symbol: str, side: Side, amount: float) -> None:
    """Decode the transaction Jupiter builds for this wallet: every instruction, its program and accounts."""
    in_mint, out_mint, raw_in, _, _ = a._pair(symbol, side, amount)
    o = a._get_order(a.order_params(in_mint, out_mint, raw_in, a.wallet))
    print(f"  route router={o.get('router')} mode={o.get('mode')} prioritizationFeeLamports={o.get('prioritizationFeeLamports')} "
          f"rentFeeLamports={o.get('rentFeeLamports')} signatureFeeLamports={o.get('signatureFeeLamports')}")
    tx = V.decode_transaction(o["transaction"])
    keys, static = V.resolve_account_keys(tx.message, a.alts)
    wsol_ata = V.ata_address(a.wallet, V.WSOL_MINT)
    names = {a.wallet: "WALLET", wsol_ata: "WALLET_wSOL_ATA", V.ata_address(a.wallet, USDC): "WALLET_USDC_ATA"}
    for n, ix in enumerate(tx.message.instructions):
        prog = static[ix.program_id_index]
        accs = [names.get(keys[i], keys[i][:8]) if i < len(keys) else "?" for i in bytes(ix.accounts)]
        print(f"  ix{n} {prog[:12]} data={bytes(ix.data)[:12].hex()} accounts={accs[:8]}{' ...' if len(accs) > 8 else ''}")


def main() -> None:
    usd = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
    s = load_settings()
    url = s.execution.rpc_url
    print(f"=== EXECUTION-TEST REHEARSAL (read-only) {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  amount ${usd:.2f}  rpc {url}")
    prov = LiveProvider(s.data, s.universe, s.execution)

    def adapter(wallet: str) -> JupiterExecutionAdapter:
        s.live.wallet_pubkey = wallet
        return JupiterExecutionAdapter(RiskEngine(s.risk, KillSwitch(s.kill_file)), s, prov, controls=None, signer=None, dry_run=True)

    # 1. quote-only, with fee fields
    print("\n--- 1. quote-only round trip (no taker)")
    a0 = adapter("11111111111111111111111111111111")
    sol_px = a0.reference_price("SOL")
    print(f"reference SOL price {sol_px:.4f}")
    for i in range(3):
        try:
            b = a0._get_order(a0.order_params(a0.usdc, a0.mints["SOL"], int(usd * 1e6), None))
            qty = int(b["outAmount"]) / 1e9
            time.sleep(2.5)
            sl = a0._get_order(a0.order_params(a0.mints["SOL"], a0.usdc, int(b["outAmount"]), None))
            back = int(sl["outAmount"]) / 1e6
            worst = int(sl["otherAmountThreshold"]) / 1e6
            print(f"sample {i + 1}: buy {json.dumps({k: b.get(k) for k in FEE_FIELDS})}")
            print(f"          sell {json.dumps({k: sl.get(k) for k in FEE_FIELDS})}")
            print(f"          => ${usd:.2f} -> {qty:.6f} SOL -> ${back:.4f}: swap round trip {(1 - back / usd) * 1e4:.1f} bps (${usd - back:.4f}); "
                  f"sell floor at slippage limit ${worst:.4f}")
        except Exception as e:  # noqa: BLE001
            print(f"sample {i + 1} FAIL {type(e).__name__}: {e}")
        time.sleep(2.5)

    # 2. build + verify + simulate the exact transactions
    targets = []
    p = os.path.join(os.path.dirname(__file__), "exec_test_wallet.txt")
    if os.path.exists(p) and open(p).read().strip():
        targets.append(("OWNER TEST WALLET", open(p).read().split()[0].strip()))
    # decode-only: the wallet seen in the 09:20 run (2.46 SOL), used only as the taker to see the real SELL route
    ref_taker = "BXEHNbKPGeUHaqpaiHR6hc9mQkTZ2WFCXh6kkHkhFnCY"
    try:
        ad = adapter(ref_taker)
        q = ad.quote("SOL", Side.BUY, usd).out_amount
        for rep in range(3):
            print(f"\n--- SOL SELL route decode #{rep + 1} (taker {ref_taker[:8]}, decode only)")
            dump_route(ad, "SOL", Side.SELL, q)
            r = ad.dry_run_order("SOL", Side.SELL, q)
            print(f"  verifier: ok={r.get('ok')} problems={r.get('problems')}")
            time.sleep(3)
        print(f"\n--- cbBTC BUY route decode (taker {ref_taker[:8]}, decode only)")
        dump_route(ad, "BTC", Side.BUY, usd)
    except Exception as e:  # noqa: BLE001
        print(f"decode FAIL {type(e).__name__}: {e}")
    try:
        fw = None if os.environ.get("SKIP_TAKER_SEARCH") else funded_wallet_from_recent_swaps(url, need_usdc=usd * 1.5, need_sol=0.05)
        if fw:
            targets.append(("funded wallet from a recent Jupiter swap (simulation only)", fw))
        else:
            print("\nno funded wallet found among recent Jupiter swaps")
    except Exception as e:  # noqa: BLE001
        print(f"\nfunded-wallet search failed: {type(e).__name__}: {e}")
    keys = ("ok", "problems", "router", "mode", "inAmount", "outAmount", "otherAmountThreshold", "our_min_out", "feeBps", "priceImpact_bps",
            "programs", "signers", "priority_fee_lamports", "wallet_sol_cost_lamports", "simulation", "blocks_remaining", "timings_ms")
    for label, w in targets:
        print(f"\n--- 2. {label}: {w}")
        a = adapter(w)
        try:
            print("balances:", json.dumps({k: round(v, 6) for k, v in a.get_balances().items()}))
        except Exception as e:  # noqa: BLE001
            print(f"balances FAIL {type(e).__name__}: {e}")
            continue
        buy = a.dry_run_order("SOL", Side.BUY, usd)
        print("BUY  verify+simulate:", json.dumps({k: buy.get(k) for k in keys}, default=str))
        if buy.get("ok"):
            qty = int(buy["outAmount"]) / 1e9
            time.sleep(2.5)
            sell = a.dry_run_order("SOL", Side.SELL, qty)
            print("SELL verify+simulate:", json.dumps({k: sell.get(k) for k in keys}, default=str))
            for rep in range(3):
                time.sleep(2.5)
                try:
                    print(f"SELL route decode #{rep + 1}:")
                    dump_route(a, "SOL", Side.SELL, qty)
                except Exception as e:  # noqa: BLE001
                    print(f"  decode FAIL {type(e).__name__}: {e}")
            if sell.get("ok"):
                lam = (buy.get("wallet_sol_cost_lamports") or 0) + (sell.get("wallet_sol_cost_lamports") or 0)
                print(f"ROUND TRIP ${usd:.2f}: back ${int(sell['outAmount']) / 1e6:.4f}; wallet SOL cost of both transactions as built "
                      f"(fees + priority + rent not refunded in-tx) {lam} lamports = ${lam / 1e9 * sol_px:.4f}")
        time.sleep(2.5)
        # pure-SPL alternative (no native SOL wrapping): cbBTC
        cb = a.dry_run_order("BTC", Side.BUY, usd)
        print("cbBTC BUY  verify+simulate:", json.dumps({k: cb.get(k) for k in keys}, default=str))
        if cb.get("ok"):
            time.sleep(2.5)
            cq = int(cb["outAmount"]) / 1e8
            cs = a.dry_run_order("BTC", Side.SELL, cq)
            print("cbBTC SELL verify+simulate:", json.dumps({k: cs.get(k) for k in keys}, default=str))
        time.sleep(2.5)
    print("\nread-only: no signature was requested and nothing was submitted.")


if __name__ == "__main__":
    main()
