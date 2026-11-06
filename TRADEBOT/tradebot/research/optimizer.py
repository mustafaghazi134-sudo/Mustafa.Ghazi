"""Grid search with walk-forward validation. Candidates must be profitable out-of-sample to rank."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from ..models import Candle
from ..strategies.registry import PARAM_GRIDS, build_strategy
from .backtester import backtest


@dataclass
class Candidate:
    strategy: str
    params: dict[str, Any]
    in_sample: dict = field(default_factory=dict)
    out_sample: dict = field(default_factory=dict)
    score: float = 0.0
    symbols: int = 0

    def row(self) -> dict:
        return {"strategy": self.strategy, "params": self.params, "symbols": self.symbols, "score": round(self.score, 3),
                "is_return_pct": self.in_sample.get("total_return_pct"), "is_sharpe": self.in_sample.get("sharpe"),
                "is_trades": self.in_sample.get("trades"), "oos_return_pct": self.out_sample.get("total_return_pct"),
                "oos_sharpe": self.out_sample.get("sharpe"), "oos_max_dd_pct": self.out_sample.get("max_drawdown_pct"),
                "oos_trades": self.out_sample.get("trades"), "oos_profit_factor": self.out_sample.get("profit_factor")}


def param_grid(strategy: str, grid: dict[str, list[Any]] | None = None) -> list[dict[str, Any]]:
    g = grid or PARAM_GRIDS.get(strategy, {})
    if not g:
        return [{}]
    keys = list(g)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(g[k] for k in keys))]


def _aggregate(results: list[dict]) -> dict:
    if not results:
        return {}
    n = len(results)
    agg = {k: round(sum(r.get(k, 0.0) for r in results) / n, 3) for k in
           ("total_return_pct", "max_drawdown_pct", "sharpe", "win_rate_pct", "profit_factor", "expectancy_usd")}
    agg["trades"] = sum(r.get("trades", 0) for r in results)
    agg["profitable_symbols"] = sum(1 for r in results if r.get("total_return_pct", 0) > 0)
    return agg


def _valid(strategy: str, params: dict[str, Any]) -> bool:
    if strategy == "momentum":
        return params.get("fast", 1) < params.get("slow", 2) < params.get("trend", 3)
    if strategy == "mean_reversion":
        return params.get("rsi_buy", 0) < params.get("rsi_exit", 100)
    return True


def optimize(strategy: str, candles_by_symbol: dict[str, list[Candle]], grid: dict[str, list[Any]] | None = None,
             train_frac: float = 0.7, fee_bps: float = 30.0, slippage_bps: float = 15.0, min_oos_trades: int = 3,
             base_params: dict[str, Any] | None = None) -> list[Candidate]:
    out: list[Candidate] = []
    for params in param_grid(strategy, grid):
        full = {**(base_params or {}), **params}
        if not _valid(strategy, full):
            continue
        strat = build_strategy(strategy, full)
        is_rows, oos_rows = [], []
        for sym, candles in candles_by_symbol.items():
            if len(candles) < strat.required_bars(candles) * 2 + 20:
                continue
            split = int(len(candles) * train_frac)
            # out-of-sample segment includes warmup overlap so indicators are defined from its first bar
            oos_start = max(0, split - strat.required_bars(candles))
            is_rows.append(backtest(strat, candles[:split], sym, fee_bps=fee_bps, slippage_bps=slippage_bps).metrics)
            oos_rows.append(backtest(strat, candles[oos_start:], sym, fee_bps=fee_bps, slippage_bps=slippage_bps).metrics)
        if not is_rows:
            continue
        c = Candidate(strategy=strategy, params=full, in_sample=_aggregate(is_rows), out_sample=_aggregate(oos_rows), symbols=len(is_rows))
        oos, ins = c.out_sample, c.in_sample
        if oos.get("trades", 0) < min_oos_trades or oos.get("total_return_pct", 0) <= 0:
            c.score = -1e9 + oos.get("total_return_pct", 0)   # keep for reporting, never selected
        else:
            # reward OOS return and sharpe, penalise drawdown and IS/OOS divergence (overfitting)
            divergence = abs(ins.get("total_return_pct", 0) - oos.get("total_return_pct", 0))
            c.score = oos["total_return_pct"] + 5 * oos.get("sharpe", 0) - 0.5 * oos.get("max_drawdown_pct", 0) - 0.25 * divergence
        out.append(c)
    out.sort(key=lambda c: c.score, reverse=True)
    return out
