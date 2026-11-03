"""Read-only: why is (or isn't) each strategy signalling right now? Uses the engine's own provider, config and
strategy code on live candles and prints every rule input. Places nothing, signs nothing.

usage: TRADEBOT_CONFIG=deploy/paper_ci.toml python deploy/signal_state.py
"""
from __future__ import annotations

import datetime as dt
import math
import time

from tradebot.config import load_settings
from tradebot.data.live import LiveProvider
from tradebot.strategies.registry import build_strategy


def ts(x: float) -> str:
    return dt.datetime.fromtimestamp(x, dt.UTC).strftime("%Y-%m-%d %H:%M")


def main() -> None:
    s = load_settings()
    prov = LiveProvider(s.data, s.universe, s.execution)
    strategies = [build_strategy(c.name, c.params) for c in s.strategies if c.enabled]
    print(f"=== SIGNAL STATE {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  interval {s.data.candle_interval}  (read-only)")
    for sym in s.universe.symbols:
        candles = prov.get_candles(sym, s.data.candle_interval, s.data.candle_history)
        if not candles:
            print(f"{sym}: no candles"); continue
        print(f"\n{sym}: {len(candles)} candles, last bar opened {ts(candles[-1].ts)} UTC, close {candles[-1].close:,.4f}")
        for st in strategies:
            a = st.compute(candles)
            sig = st.entry_signal(sym, candles)
            if st.name == "tsmom":
                idx = [i for i, d in enumerate(a["daily"]) if d == 1.0]
                i = idx[-1] if idx else len(candles) - 1
                mom, c, e = a["mom"][i], a["c"][i], a["ema"][i]
                ok_mom = (not math.isnan(mom)) and mom > 0
                ok_ema = (not math.isnan(e)) and c > e
                print(f"  tsmom   last daily check bar {ts(candles[i].ts)}: 30d momentum {mom * 100:+.2f}% ({'PASS' if ok_mom else 'FAIL'} > 0), "
                      f"close {c:,.4f} vs EMA100d {e:,.4f} ({'PASS' if ok_ema else 'FAIL'} above) -> "
                      f"{'ENTRY condition met' if ok_mom and ok_ema else 'no entry'}; next check {ts((candles[i].ts // 86400 + 1) * 86400)} UTC")
            elif st.name == "momentum":
                i = len(candles) - 1
                f0, s0, f1, s1, tr, c = a["fast"][i - 1], a["slow"][i - 1], a["fast"][i], a["slow"][i], a["trend"][i], a["c"][i]
                cross = f0 <= s0 and f1 > s1
                print(f"  momentum bar {ts(candles[i].ts)}: EMA12 {f1:,.4f} vs EMA26 {s1:,.4f} ({'above' if f1 > s1 else 'below'}; "
                      f"fresh cross up: {'YES' if cross else 'no'}), close vs EMA100 {tr:,.4f}: {'above' if c > tr else 'below'} -> "
                      f"{'ENTRY condition met' if cross and c > tr else 'no entry'}")
            print(f"    engine entry_signal(): {sig.reason if sig else 'None'}")


if __name__ == "__main__":
    main()
