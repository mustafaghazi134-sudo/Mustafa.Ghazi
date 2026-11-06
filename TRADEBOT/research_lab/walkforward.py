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
                 warmup: int = 800, size_frac: float = 1.0, partners: dict[str, pd.DataFrame] | None = None) -> Result:
    fn = REGISTRY[name]["fn"]
    p = {**REGISTRY[name]["default"], **params}
    if "custom_backtest" in REGISTRY[name].get("flags", ()):
        return fn(df, p, fee_bps=fee_bps, slippage_bps=slippage_bps, size_frac=size_frac, bar_minutes=bar_minutes, warmup=warmup)
    if REGISTRY[name].get("needs_partners"):
        # partner series aligned to this frame's index (no future bars: reindex + ffill only backwards in time)
        p["_partners"] = {k: v.reindex(df.index, method="ffill") for k, v in (partners or {}).items()}
    entries, exits, spec = fn(df, p)
    # never trade during the indicator warm-up
    entries = entries.copy(); entries.iloc[:warmup] = False
    return run_backtest(df, entries, exits, spec, fee_bps=fee_bps, slippage_bps=slippage_bps, bar_minutes=bar_minutes, size_frac=size_frac)


@dataclass
class WFResult:
    strategy: str
    symbol: str
    segments: list[dict] = field(default_factory=list)   # per test window: chosen params, IS metrics, OOS metrics
    oos_equity: pd.Series | None = None
    oos_metrics: dict = field(default_factory=dict)
    oos_trades: int = 0
    param_stability: float = 0.0   # fraction of windows that chose the modal parameter set
    final_params: dict = field(default_factory=dict)   # parameters chosen by the last development window (frozen for the holdout)


def walk_forward(name: str, df: pd.DataFrame, fee_bps: float, slippage_bps: float, train_months: int = 12,
                 test_months: int = 3, warmup: int = 800, bar_minutes: int = 60, size_frac: float = 1.0,
                 partners: dict[str, pd.DataFrame] | None = None, end_at: pd.Timestamp | None = None) -> WFResult:
    """end_at: last timestamp the walk-forward may see (development period). Data after it is reserved for the holdout."""
    grid = param_grid(REGISTRY[name]["grid"])
    res = WFResult(name, "")
    if end_at is not None:
        df = df[df.index <= end_at]
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
            r = run_strategy(name, train, p, fee_bps, slippage_bps, bar_minutes, warmup=min(warmup, len(train) // 3), size_frac=size_frac, partners=partners)
            s = score(r.metrics)
            if s > best_s:
                best, best_s, best_m = p, s, r.metrics
        oos = run_strategy(name, test, best, fee_bps, slippage_bps, bar_minutes, warmup=warmup, size_frac=size_frac, partners=partners)
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
            res.final_params = dict(chosen[-1])
    return res


def holdout_run(name: str, df: pd.DataFrame, params: dict, holdout_start: pd.Timestamp, fee_bps: float, slippage_bps: float,
                warmup: int, bar_minutes: int, size_frac: float = 1.0, partners: dict[str, pd.DataFrame] | None = None) -> Result | None:
    """Frozen-parameter run on the untouched final period: history before holdout_start is used only as indicator warm-up."""
    pos = df.index.searchsorted(holdout_start)
    seg = df.iloc[max(0, pos - warmup):]
    if len(seg) <= warmup + 10:
        return None
    r = run_strategy(name, seg, params, fee_bps, slippage_bps, bar_minutes, warmup=warmup, size_frac=size_frac, partners=partners)
    # metrics over the holdout bars only
    r.equity = r.equity.iloc[warmup:]
    r.trades = [t for t in r.trades if t.entry_i >= warmup]
    r.metrics = compute_metrics(r, float(r.equity.iloc[0]) if len(r.equity) else 10_000.0, bar_minutes)
    return r
