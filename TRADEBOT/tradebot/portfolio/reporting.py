"""Performance reporting: net profitability including fees and Claude API spend."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

from ..models import ClosedTrade, EquityPoint
from .ledger import Ledger


@dataclass
class PerformanceReport:
    mode: str
    initial_capital: float
    equity: float
    cash: float
    exposure: float
    realized_pnl: float
    unrealized_pnl: float
    fees: float
    api_cost: float
    net_profit: float
    net_return_pct: float
    max_drawdown_pct: float
    sharpe: float
    trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    avg_trade_pnl: float
    best_trade: float
    worst_trade: float
    by_strategy: dict[str, dict] = field(default_factory=dict)
    open_positions: list[dict] = field(default_factory=list)
    since_ts: float = 0.0


def max_drawdown(curve: list[float]) -> float:
    peak, mdd = -math.inf, 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak * 100)
    return mdd


def sharpe_ratio(curve: list[EquityPoint]) -> float:
    if len(curve) < 3:
        return 0.0
    rets = [(b.equity_usd / a.equity_usd - 1) for a, b in zip(curve, curve[1:]) if a.equity_usd > 0]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if var <= 0:
        return 0.0
    span = max(curve[-1].ts - curve[0].ts, 1.0)
    per_year = len(rets) / (span / (365 * 86400))
    return mean / math.sqrt(var) * math.sqrt(per_year)


def performance_report(ledger: Ledger, prices: dict[str, float], since_ts: float = 0.0) -> PerformanceReport:
    trades: list[ClosedTrade] = ledger.trades(since=since_ts)
    curve = ledger.equity_curve(since=since_ts)
    positions = ledger.load_positions()
    initial = ledger.get("initial_capital", 0.0) or 0.0
    cash = ledger.get("cash", initial) or 0.0
    exposure = sum(p.market_value(prices.get(p.symbol, p.avg_cost)) for p in positions.values())
    unrealized = sum(p.unrealized_pnl(prices.get(p.symbol, p.avg_cost)) for p in positions.values())
    realized = sum(t.pnl_usd for t in trades)
    fees = sum(t.fees_usd for t in trades) + sum(p.fees_usd for p in positions.values())
    api_cost = ledger.total_api_cost(since=since_ts)
    equity = cash + exposure
    net = realized + unrealized - api_cost          # trade fees already inside realized/unrealized basis
    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    gross_win = sum(t.pnl_usd for t in wins)
    gross_loss = -sum(t.pnl_usd for t in losses)
    by_strat: dict[str, dict] = {}
    for t in trades:
        d = by_strat.setdefault(t.strategy or "?", {"trades": 0, "pnl": 0.0, "wins": 0})
        d["trades"] += 1
        d["pnl"] += t.pnl_usd
        d["wins"] += int(t.pnl_usd > 0)
    return PerformanceReport(
        mode=ledger.mode, initial_capital=initial, equity=equity, cash=cash, exposure=exposure, realized_pnl=realized,
        unrealized_pnl=unrealized, fees=fees, api_cost=api_cost, net_profit=net,
        net_return_pct=(net / initial * 100) if initial else 0.0,
        max_drawdown_pct=max_drawdown([e.equity_usd for e in curve]), sharpe=sharpe_ratio(curve),
        trades=len(trades), wins=len(wins), losses=len(losses), win_rate=(len(wins) / len(trades) * 100) if trades else 0.0,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0),
        avg_trade_pnl=(realized / len(trades)) if trades else 0.0,
        best_trade=max((t.pnl_usd for t in trades), default=0.0), worst_trade=min((t.pnl_usd for t in trades), default=0.0),
        by_strategy=by_strat,
        open_positions=[{"symbol": p.symbol, "qty": p.qty, "avg_cost": p.avg_cost, "price": prices.get(p.symbol),
                         "unrealized": p.unrealized_pnl(prices.get(p.symbol, p.avg_cost)), "strategy": p.strategy}
                        for p in positions.values()],
        since_ts=since_ts,
    )


def format_report(r: PerformanceReport) -> str:
    lines = [
        f"=== tradebot performance ({r.mode}) ===",
        f"Equity          ${r.equity:,.2f}   (cash ${r.cash:,.2f}, exposure ${r.exposure:,.2f})",
        f"Initial capital ${r.initial_capital:,.2f}",
        f"Realized P&L    ${r.realized_pnl:,.2f}",
        f"Unrealized P&L  ${r.unrealized_pnl:,.2f}",
        f"Trading fees    ${r.fees:,.2f}   (included in P&L above)",
        f"Claude API cost ${r.api_cost:,.2f}",
        f"NET PROFIT      ${r.net_profit:,.2f}   ({r.net_return_pct:+.2f}%)",
        f"Max drawdown    {r.max_drawdown_pct:.2f}%    Sharpe {r.sharpe:.2f}",
        f"Trades {r.trades}  wins {r.wins}  losses {r.losses}  win rate {r.win_rate:.1f}%  profit factor {r.profit_factor:.2f}",
        f"Avg trade ${r.avg_trade_pnl:,.2f}   best ${r.best_trade:,.2f}   worst ${r.worst_trade:,.2f}",
    ]
    if r.by_strategy:
        lines.append("By strategy:")
        for k, v in sorted(r.by_strategy.items()):
            lines.append(f"  {k:<16} trades {v['trades']:>4}  pnl ${v['pnl']:>10,.2f}  win rate {v['wins']/v['trades']*100:5.1f}%")
    if r.open_positions:
        lines.append("Open positions:")
        for p in r.open_positions:
            px = f"{p['price']:.6g}" if p["price"] else "n/a"
            lines.append(f"  {p['symbol']:<8} qty {p['qty']:.6g} @ {p['avg_cost']:.6g}  px {px}  unrealized ${p['unrealized']:,.2f}  [{p['strategy']}]")
    if r.since_ts:
        lines.append(f"(since {dt.datetime.fromtimestamp(r.since_ts, dt.UTC):%Y-%m-%d %H:%M} UTC)")
    return "\n".join(lines)
