"""Read-only probe for the paper runner: data freshness, keyless Jupiter access, executable round-trip quotes, and
whether each executable Solana asset tracks the Binance instrument its signals are computed on. Signs nothing.

usage: python deploy/paper_probe.py   (uses TRADEBOT_CONFIG; prints a report)
"""
from __future__ import annotations

import json
import time

import httpx

from tradebot.config import jupiter_endpoints, load_settings


def main() -> None:
    s = load_settings()
    jup = jupiter_endpoints(s.data, s.execution)
    h = httpx.Client(timeout=15.0, headers=jup.headers)
    now = time.time()
    print(f"=== READ-ONLY PROBE {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))}  jupiter key: {'yes' if jup.has_key else 'no (keyless)'}")
    # 1. candle freshness on the signal instruments
    bin_px = {}
    for sym, pair in s.universe.candle_symbols.items():
        try:
            r = h.get(f"{s.data.binance_base_url}/api/v3/klines", params={"symbol": pair, "interval": "4h", "limit": 2})
            r.raise_for_status()
            k = r.json()[-1]
            age = now - k[0] / 1000
            bin_px[sym] = float(k[4])
            print(f"binance {pair:<8} last 4h candle opened {age / 60:6.1f} min ago, close {float(k[4]):.4f}  -> {'FRESH' if age < 4 * 3600 + 300 else 'STALE'}")
        except Exception as e:  # noqa: BLE001
            print(f"binance {pair:<8} FAIL {type(e).__name__}: {str(e)[:120]}")
    # 2. executable asset prices and basis vs the signal instrument
    ids = {sym: s.universe.mints[sym] for sym in s.universe.symbols}
    try:
        r = h.get(jup.price, params={"ids": ",".join(ids.values())})
        print(f"jupiter price v3 HTTP {r.status_code}")
        data = r.json() if r.status_code == 200 else {}
        for sym, mint in ids.items():
            px = (data.get(mint) or {}).get("usdPrice")
            if px and sym in bin_px:
                print(f"  {sym:<4} mint {mint}  jupiter ${px:,.4f}  binance ${bin_px[sym]:,.4f}  basis {(px / bin_px[sym] - 1) * 1e4:+.1f} bps")
            else:
                print(f"  {sym:<4} mint {mint}  jupiter price: {px}")
    except Exception as e:  # noqa: BLE001
        print(f"jupiter price v3 FAIL {type(e).__name__}: {str(e)[:120]}")
    # 3. executable round trip at the engine's order size (no taker = quote only, nothing can be signed)
    usdc = s.universe.mints["USDC"]
    size = min(s.initial_capital_usd * s.risk.max_position_pct / 100, s.risk.max_order_notional_usd)
    for sym, mint in ids.items():
        try:
            buy = h.get(f"{jup.swap}/order", params={"inputMint": usdc, "outputMint": mint, "amount": str(int(size * 1e6)),
                                                     "slippageBps": s.execution.live_slippage_bps})
            if buy.status_code != 200:
                print(f"quote USDC->{sym}: HTTP {buy.status_code} {buy.text[:120]}"); continue
            b = buy.json()
            out_min = int(b.get("otherAmountThreshold") or b["outAmount"])
            time.sleep(2.2)
            sell = h.get(f"{jup.swap}/order", params={"inputMint": mint, "outputMint": usdc, "amount": str(out_min),
                                                      "slippageBps": s.execution.live_slippage_bps})
            sd = sell.json() if sell.status_code == 200 else {}
            back = int(sd.get("otherAmountThreshold") or sd.get("outAmount") or 0) / 1e6
            print(f"quote ${size:.0f} USDC->{sym}->USDC worst-case round trip {(1 - back / size) * 1e4:6.1f} bps "
                  f"(router {b.get('router')}, fee {b.get('feeBps')} bps, impact {b.get('priceImpact')}%) + 2 network fees")
            time.sleep(2.2)
        except Exception as e:  # noqa: BLE001
            print(f"quote {sym}: FAIL {type(e).__name__}: {str(e)[:120]}")
    print(json.dumps({"signal_instruments": s.universe.candle_symbols, "executable_mints": ids}, indent=1))


if __name__ == "__main__":
    main()
