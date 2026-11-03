"""READ-ONLY rehearsal of the execution test on real Jupiter routes and the real chain. Signs nothing, submits nothing.

For one wallet (deploy/exec_test_wallet.txt if present, else a funded public wallet found on chain only so the
real transactions can be simulated), it prints the wallet's balances, then builds, verifies and simulates the exact
two transactions the test would send (buy $USD of SOL with USDC, sell that SOL back), with the costs of each leg.

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


def rpc(url, method, params):
    r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"])
    return j["result"]


RPCS = ["https://solana-rpc.publicnode.com", "https://api.mainnet-beta.solana.com"]


def find_funded_wallet(url) -> str | None:
    """A normal (on-curve) wallet holding >= $25 USDC and >= 0.5 SOL, used ONLY as the taker for simulation."""
    accts = rpc(url, "getTokenLargestAccounts", [USDC, {"commitment": "confirmed"}])["value"]
    infos = rpc(url, "getMultipleAccounts", [[a["address"] for a in accts], {"encoding": "jsonParsed"}])["value"]
    for a, info in zip(accts, infos):
        try:
            owner = info["data"]["parsed"]["info"]["owner"]
            if not Pubkey.from_string(owner).is_on_curve():
                continue
            if rpc(url, "getBalance", [owner])["value"] >= 500_000_000 and float(a["uiAmount"] or 0) >= 25:
                return owner
        except Exception:  # noqa: BLE001
            continue
    return None


def main() -> None:
    usd = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    s = load_settings()
    url = s.execution.rpc_url
    print(f"=== EXECUTION-TEST REHEARSAL (read-only) {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  amount ${usd:.2f}")
    own = None
    p = os.path.join(os.path.dirname(__file__), "exec_test_wallet.txt")
    if os.path.exists(p):
        own = open(p).read().split()[0].strip() if open(p).read().strip() else None
    targets = []
    if own:
        targets.append(("OWNER TEST WALLET", own))
    fw = None
    print("\n--- quote-only round trip (no wallet; nothing built or simulated)")
    try:
        s.live.wallet_pubkey = "11111111111111111111111111111111"
        a0 = JupiterExecutionAdapter(RiskEngine(s.risk, KillSwitch(s.kill_file)), s, LiveProvider(s.data, s.universe, s.execution),
                                     controls=None, signer=None, dry_run=True)
        for i in range(3):
            qb = a0.quote("SOL", Side.BUY, usd)
            time.sleep(2.5)
            qs = a0.quote("SOL", Side.SELL, qb.out_amount)
            print(f"sample {i + 1}: buy ${usd:.2f} -> {qb.out_amount:.6f} SOL (cost {qb.expected_slippage_bps:.1f} bps vs reference, router "
                  f"{(qb.raw or {}).get('router')}); sell back -> ${qs.out_amount:.4f} (cost {qs.expected_slippage_bps:.1f} bps); "
                  f"round trip {(1 - qs.out_amount / usd) * 1e4:.1f} bps = ${usd - qs.out_amount:.4f}")
            time.sleep(2.5)
    except Exception as e:  # noqa: BLE001
        print(f"quote-only FAIL {type(e).__name__}: {e}")
    if not targets:
        print("\nno owner test wallet configured (deploy/exec_test_wallet.txt); skipping the funded-wallet search")
        print("\nread-only: no signature was requested and nothing was submitted.")
        return
    for cand in RPCS[1:]:
        for attempt in range(3):
            try:
                fw = find_funded_wallet(cand)
                url = cand
                break
            except Exception as e:  # noqa: BLE001
                print(f"funded-wallet search via {cand} attempt {attempt + 1}: {type(e).__name__}: {str(e)[:120]}")
                time.sleep(5 * (attempt + 1))
        if fw:
            break
    s.execution.rpc_url = url
    print(f"rpc used for balances and simulation: {url}")
    if fw:
        targets.append(("funded public wallet (simulation only)", fw))
    for label, w in targets:
        print(f"\n--- {label}: {w}")
        s.live.wallet_pubkey = w
        prov = LiveProvider(s.data, s.universe, s.execution)
        a = JupiterExecutionAdapter(RiskEngine(s.risk, KillSwitch(s.kill_file)), s, prov, controls=None, signer=None, dry_run=True)
        try:
            bal = a.get_balances()
            print("balances:", json.dumps({k: round(v, 6) for k, v in bal.items()}))
        except Exception as e:  # noqa: BLE001
            print(f"balances FAIL {type(e).__name__}: {e}")
            continue
        buy = a.dry_run_order("SOL", Side.BUY, usd)
        keys = ("ok", "problems", "router", "inAmount", "outAmount", "otherAmountThreshold", "our_min_out", "feeBps", "priceImpact_bps",
                "programs", "signers", "priority_fee_lamports", "wallet_sol_cost_lamports", "simulation", "reference_price", "timings_ms")
        print("BUY  dry run:", json.dumps({k: buy.get(k) for k in keys}, default=str))
        if buy.get("ok"):
            qty = int(buy["outAmount"]) / 1e9
            buy_cost = (usd / qty / buy["reference_price"] - 1) * 1e4
            time.sleep(2.5)
            sell = a.dry_run_order("SOL", Side.SELL, qty)
            print("SELL dry run:", json.dumps({k: sell.get(k) for k in keys}, default=str))
            if sell.get("ok"):
                back = int(sell["outAmount"]) / 1e6
                worst = int(sell["otherAmountThreshold"]) / 1e6
                net_fees = (buy.get("wallet_sol_cost_lamports") or 0) + (sell.get("wallet_sol_cost_lamports") or 0)
                print(f"ROUND TRIP ${usd:.2f}: quoted back ${back:.4f} (cost {(1 - back / usd) * 1e4:.1f} bps, buy leg {buy_cost:.1f} bps vs "
                      f"reference); worst case at slippage limit ${worst:.4f} ({(1 - worst / usd) * 1e4:.1f} bps); network+priority "
                      f"{net_fees} lamports (~${net_fees / 1e9 * buy['reference_price']:.4f})")
        time.sleep(2.5)
    print("\nread-only: no signature was requested and nothing was submitted.")


if __name__ == "__main__":
    main()
