"""Walk-forward validation with parameter selection on the training window only.

Rolling scheme: train `train_months`, test the next `test_months`, roll forward by `test_months`.
Parameters are chosen on the training window by a robustness-aware score (penalises few trades);
the chosen set is then run once on the unseen test window. Out-of-sample (OOS) segments are stitched
by compounding, so the OOS equity curve is what a trader following the procedure would have had.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

import pandas as pd

from .engine import ExitSpec, Result, compute_metrics, run_backtest
from .strategies import REGISTRY


def param_grid(grid: dict) -> list[dict]:
    if not grid:
        return [{}]
    keys = list(grid)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(grid[k] for k in keys))]


def score(m: dict, min_trades: int = 8) -> float:
    """Selection score on the training window: Sharpe shrunk toward zero for small samples, drawdown-penalised."""
    n = m["trades"]
    if n < min_trades:
        return -1e9
    shrink = n / (n + 20)
    return m["sharpe"] * shrink - 0.02 * abs(m["max_drawdown_pct"]) + (0.5 if m["net_return_pct"] > 0 else -0.5)


def run_strategy(name: str, df: pd.DataFrame, params: dict, fee_bps: float, slippage_bps: float, bar_minutes: int = 60,
                 warmup: int = 800) -> Result:
    fn = REGISTRY[name]["fn"]
    entries, exits, spec = fn(df, {**REGISTRY[name]["default"], **params})
    # never trade during the indicator warm-up
    entries = entries.copy(); entries.iloc[:warmup] = False
    return run_backtest(df, entries, exits, spec, fee_bps=fee_bps, slippage_bps=slippage_bps, bar_minutes=bar_minutes)


@dataclass
class WFResult:
    strategy: str
    symbol: str
    segments: list[dict] = field(default_factory=list)   # per test window: chosen params, IS metrics, OOS metrics
    oos_equity: pd.Series | None = None
    oos_metrics: dict = field(default_factory=dict)
    oos_trades: int = 0
    param_stability: float = 0.0   # fraction of windows that chose the modal parameter set


def walk_forward(name: str, df: pd.DataFrame, fee_bps: float, slippage_bps: float, train_months: int = 12,
                 test_months: int = 3, warmup: int = 800, bar_minutes: int = 60) -> WFResult:
    grid = param_grid(REGISTRY[name]["grid"])
    res = WFResult(name, "")
    start = df.index[0]
    end = df.index[-1]
    t0 = start + pd.DateOffset(months=train_months)
    eq_parts: list[pd.Series] = []
    all_trades = []
    chosen = []
    while t0 + pd.DateOffset(months=1) <= end:
        t1 = min(t0 + pd.DateOffset(months=test_months), end)
        train = df[(df.index >= t0 - pd.DateOffset(months=train_months)) & (df.index < t0)]
        # the test slice carries `warmup` bars of history so indicators are defined from its first bar
        test_start_pos = df.index.searchsorted(t0)
        test = df.iloc[max(0, test_start_pos - warmup): df.index.searchsorted(t1)]
        if len(train) < warmup + 200 or len(test) <= warmup + 24:
            t0 = t1
            continue
        best, best_s, best_m = None, -1e18, None
        for p in grid:
            r = run_strategy(name, train, p, fee_bps, slippage_bps, bar_minutes, warmup=min(warmup, len(train) // 3))
            s = score(r.metrics)
            if s > best_s:
                best, best_s, best_m = p, s, r.metrics
        oos = run_strategy(name, test, best, fee_bps, slippage_bps, bar_minutes, warmup=warmup)
        oos_eq = oos.equity.iloc[warmup:]
        scale = (eq_parts[-1].iloc[-1] / oos_eq.iloc[0]) if eq_parts else (10_000.0 / oos_eq.iloc[0])
        eq_parts.append(oos_eq * scale)
        all_trades.extend(t for t in oos.trades if t.entry_i >= warmup)
        chosen.append(tuple(sorted(best.items())))
        res.segments.append({"test_start": str(t0)[:10], "test_end": str(t1)[:10], "params": best,
                             "is": {k: best_m[k] for k in ("trades", "net_return_pct", "sharpe", "max_drawdown_pct", "win_rate_pct")},
                             "oos": {k: oos.metrics[k] for k in ("trades", "net_return_pct", "sharpe", "max_drawdown_pct", "win_rate_pct", "profit_factor")}})
        t0 = t1
    if eq_parts:
        eq = pd.concat(eq_parts)
        eq = eq[~eq.index.duplicated(keep="last")]
        res.oos_equity = eq
        res.oos_trades = len(all_trades)
        res.oos_metrics = compute_metrics(Result(all_trades, eq), 10_000.0, bar_minutes)
        if chosen:
            modal = max(set(chosen), key=chosen.count)
            res.param_stability = round(chosen.count(modal) / len(chosen), 2)
    return res
