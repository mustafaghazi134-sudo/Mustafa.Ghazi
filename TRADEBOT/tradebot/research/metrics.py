from __future__ import annotations

import math

from ..models import ClosedTrade


def compute_metrics(trades: list[ClosedTrade], equity_curve: list[float], bar_seconds: float, initial: float) -> dict:
    final = equity_curve[-1] if equity_curve else initial
    total_ret = (final / initial - 1) * 100 if initial else 0.0
    peak, mdd = -math.inf, 0.0
    for v in equity_curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak * 100)
    rets = [(b / a - 1) for a, b in zip(equity_curve, equity_curve[1:]) if a > 0]
    sharpe = 0.0
    if len(rets) > 2:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        if var > 0:
            sharpe = mean / math.sqrt(var) * math.sqrt(365 * 86400 / bar_seconds)
    wins = [t.pnl_usd for t in trades if t.pnl_usd > 0]
    losses = [-t.pnl_usd for t in trades if t.pnl_usd <= 0]
    gw, gl = sum(wins), sum(losses)
    n = len(trades)
    return {
        "total_return_pct": round(total_ret, 3),
        "max_drawdown_pct": round(mdd, 3),
        "sharpe": round(sharpe, 3),
        "trades": n,
        "win_rate_pct": round(len(wins) / n * 100, 2) if n else 0.0,
        "profit_factor": round(gw / gl, 3) if gl > 0 else (999.0 if gw > 0 else 0.0),
        "expectancy_usd": round((gw - gl) / n, 4) if n else 0.0,
        "avg_hold_hours": round(sum(t.exit_ts - t.entry_ts for t in trades) / n / 3600, 2) if n else 0.0,
        "fees_usd": round(sum(t.fees_usd for t in trades), 2),
        "final_equity": round(final, 2),
    }
