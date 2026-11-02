"""Benchmark the existing 4h paper engine offline: where does a cycle spend its time, and what bounds its reaction time?

Runs the real TradingBot on the synthetic provider (no network) under cProfile and reports per-cycle wall time,
time by subsystem, strategy evaluation cost per symbol, ledger write cost, and the static latency budget of the live
data/execution path read from the configuration (timeouts, retries, polling interval).

usage (from TRADEBOT/):  python -m research_lab.benchmark_engine [--cycles 300] [--json out.json]
"""
from __future__ import annotations

import argparse
import cProfile
import io
import json
import pstats
import statistics
import tempfile
import time

from tradebot.config import Settings
from tradebot.engine.bot import build_bot
from tradebot.strategies.registry import build_strategies

SUBSYSTEMS = {
    "provider": ("tradebot/data/",),
    "strategies": ("tradebot/strategies/",),
    "ledger(sqlite)": ("tradebot/portfolio/ledger.py", "sqlite3"),
    "accounting": ("tradebot/portfolio/accounting.py", "tradebot/portfolio/reporting.py"),
    "risk": ("tradebot/risk/",),
    "execution": ("tradebot/execution/",),
    "engine": ("tradebot/engine/",),
}


def _bucket(path: str) -> str:
    for name, pats in SUBSYSTEMS.items():
        if any(p in path for p in pats):
            return name
    return "other"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=300)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    tmp = tempfile.mkdtemp()
    s = Settings(mode="paper", data_dir=tmp, initial_capital_usd=1000.0)
    s.claude.enabled = False; s.claude.research_enabled = False
    s.data.provider = "synthetic"; s.data.candle_history = 1000
    s.universe.symbols = ["SOL", "JUP", "BONK"]
    s.risk.min_liquidity_usd = 1000; s.risk.min_volume_24h_usd = 1000
    s.risk.max_daily_loss_pct = 50; s.risk.max_drawdown_pct = 60
    bot = build_bot(s)

    # 1. per-cycle wall time under the profiler
    cycle_ms: list[float] = []
    prof = cProfile.Profile()
    prof.enable()
    for _ in range(args.cycles):
        t0 = time.perf_counter()
        bot.cycle()
        bot.provider.advance()
        cycle_ms.append((time.perf_counter() - t0) * 1000)
    prof.disable()
    st = pstats.Stats(prof)
    by_sub: dict[str, float] = {}
    for (path, _line, _fn), (_cc, _nc, tt, _ct, _callers) in st.stats.items():
        by_sub[_bucket(path)] = by_sub.get(_bucket(path), 0.0) + tt
    total = sum(by_sub.values()) or 1.0
    top = io.StringIO(); pstats.Stats(prof, stream=top).sort_stats("cumulative").print_stats(12)

    # 2. strategy evaluation cost on 1000 bars (what every cycle recomputes per symbol per strategy)
    candles = bot.provider.get_candles("SOL", s.data.candle_interval, 1000)
    strat_ms = {}
    for strat in build_strategies(s.strategies):
        t0 = time.perf_counter()
        for _ in range(20):
            strat.entry_signal("SOL", candles)
        strat_ms[strat.name] = (time.perf_counter() - t0) * 1000 / 20

    # 3. ledger write latency (one decision + one equity point)
    t0 = time.perf_counter()
    for i in range(200):
        bot.ledger.log_decision("bench", {"i": i}, "SOL", "bench", ts=bot.clock())
    ledger_ms = (time.perf_counter() - t0) * 1000 / 200

    # 4. static latency budget of the live path, from configuration and code
    budget = {
        "scan_interval_sec": s.engine.scan_interval_sec,
        "candle_interval": s.data.candle_interval,
        "strategy_evaluation": "tsmom: once per UTC day (00:00 bar); momentum: on each completed 4h bar",
        "http_requests_per_cycle_live": {"jupiter_price_v3": 1, "jupiter_tokens_v2_search": "1 (cached 60s)", "binance_ticker_price": 1,
                                         "binance_klines_per_symbol": "0 most cycles (cached 25% of bar)", "total_typical": "3"},
        "quote_timeout_sec": s.data.jupiter_quote_timeout_sec, "execute_timeout_sec": s.data.jupiter_execute_timeout_sec,
        "execute_max_retries": s.execution.execute_max_retries,
        "worst_case_order_path_sec": s.data.jupiter_quote_timeout_sec * (1 + s.execution.execute_max_retries) + s.data.jupiter_execute_timeout_sec * (1 + s.execution.execute_max_retries),
        "typical_order_path_sec": "quote ~0.3-1.0 + sign <0.01 + execute ~2-8 (Jupiter submits and confirms)  [UNMEASURED here: no network]",
        "reaction_time_bound": "signal can only be seen at the next 60 s poll after a 4h bar closes; the strategy itself only looks once a day",
    }
    out = {
        "cycles": args.cycles, "symbols": s.universe.symbols,
        "cycle_ms": {"median": round(statistics.median(cycle_ms), 2), "p95": round(sorted(cycle_ms)[int(len(cycle_ms) * 0.95) - 1], 2), "max": round(max(cycle_ms), 2)},
        "time_share_pct": {k: round(v / total * 100, 1) for k, v in sorted(by_sub.items(), key=lambda kv: -kv[1])},
        "strategy_eval_ms_per_symbol_1000_bars": {k: round(v, 2) for k, v in strat_ms.items()},
        "ledger_write_ms": round(ledger_ms, 3),
        "live_path_budget": budget,
    }
    print(json.dumps(out, indent=1))
    print(top.getvalue()[:3000])
    if args.json:
        with open(args.json, "w") as f:
            json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
