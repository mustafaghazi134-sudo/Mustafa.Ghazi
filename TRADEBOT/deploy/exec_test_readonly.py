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
    sigs = rpc(url, "getSignaturesForAddress", [JUP6, {"limit": 25}])
    seen = set()
    for s in sigs:
        if s.get("err"):
            continue
        time.sleep(0.6)
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
        if lam / 1e9 >= need_sol and usdc >= need_usdc:
            return payer
    return None


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
    try:
        fw = funded_wallet_from_recent_swaps(url, need_usdc=usd * 2, need_sol=0.1)
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
            if sell.get("ok"):
                lam = (buy.get("wallet_sol_cost_lamports") or 0) + (sell.get("wallet_sol_cost_lamports") or 0)
                print(f"ROUND TRIP ${usd:.2f}: back ${int(sell['outAmount']) / 1e6:.4f}; wallet SOL cost of both transactions as built "
                      f"(fees + priority + rent not refunded in-tx) {lam} lamports = ${lam / 1e9 * sol_px:.4f}")
        time.sleep(2.5)
    print("\nread-only: no signature was requested and nothing was submitted.")


if __name__ == "__main__":
    main()
