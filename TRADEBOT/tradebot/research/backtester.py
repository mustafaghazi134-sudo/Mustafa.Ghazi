"""Event-driven single-asset backtester using the same strategy and exit code as live trading.

Entries fill at the next bar's open (no look-ahead) with slippage and fees. Stops are checked
against bar lows/highs, then strategy exits at the close.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Candle, ClosedTrade, Position
from ..strategies.base import Strategy
from ..engine.position_manager import check_exit
from .metrics import compute_metrics


@dataclass
class BacktestResult:
    strategy: str
    params: dict
    symbol: str
    trades: list[ClosedTrade]
    equity_curve: list[float]
    metrics: dict = field(default_factory=dict)


def backtest(strategy: Strategy, candles: list[Candle], symbol: str, initial_cash: float = 10_000.0,
             fee_bps: float = 30.0, slippage_bps: float = 15.0, position_pct: float = 100.0,
             warmup: int | None = None) -> BacktestResult:
    warm = warmup if warmup is not None else strategy.min_bars
    cash = initial_cash
    pos: Position | None = None
    pending = None  # signal to fill at next open
    trades: list[ClosedTrade] = []
    curve: list[float] = []
    bar_sec = (candles[1].ts - candles[0].ts) if len(candles) > 1 else 3600.0
    pre = strategy.precompute(symbol, candles)   # indicators computed once for the whole series

    def fill_buy(price: float, ts: float, sig) -> None:
        nonlocal cash, pos
        px = price * (1 + slippage_bps / 1e4)
        notional = cash * position_pct / 100
        fee = notional * fee_bps / 1e4
        qty = (notional - fee) / px
        cash -= notional
        pos = Position(symbol=symbol, qty=qty, avg_cost=px, opened_ts=ts, strategy=strategy.name, stop_loss=sig.stop_loss,
                       take_profit=sig.take_profit, trailing_stop_pct=sig.trailing_stop_pct, max_hold_sec=sig.max_hold_sec,
                       high_water=px, fees_usd=fee)

    def fill_sell(price: float, ts: float, reason: str) -> None:
        nonlocal cash, pos
        assert pos is not None
        px = price * (1 - slippage_bps / 1e4)
        gross = pos.qty * px
        fee = gross * fee_bps / 1e4
        cash += gross - fee
        pnl = gross - fee - pos.qty * pos.avg_cost - pos.fees_usd
        trades.append(ClosedTrade(symbol=symbol, strategy=strategy.name, qty=pos.qty, entry_price=pos.avg_cost, exit_price=px,
                                  entry_ts=pos.opened_ts, exit_ts=ts, fees_usd=fee + pos.fees_usd, pnl_usd=pnl, exit_reason=reason))
        pos = None

    for i in range(warm, len(candles)):
        bar = candles[i]
        if pending is not None and pos is None:
            fill_buy(bar.open, bar.ts, pending)
        pending = None
        if pos is not None:
            # intrabar stop / take-profit checks using low/high, conservative ordering: stop first
            reason = check_exit(pos, bar.low, bar.ts + bar_sec)
            if reason in ("stop_loss", "trailing_stop"):
                stop_px = pos.stop_loss if reason == "stop_loss" else pos.high_water * (1 - (pos.trailing_stop_pct or 0) / 100)
                fill_sell(min(bar.open, max(stop_px, bar.low)) if bar.open < stop_px else stop_px, bar.ts + bar_sec, reason)
            else:
                pos.high_water = max(pos.high_water, bar.high)
                if pos.take_profit is not None and bar.high >= pos.take_profit:
                    fill_sell(max(bar.open, pos.take_profit) if bar.open > pos.take_profit else pos.take_profit, bar.ts + bar_sec, "take_profit")
                else:
                    reason = check_exit(pos, bar.close, bar.ts + bar_sec)
                    if reason is None:
                        strat_reason = pre.exit(i, pos)
                        reason = f"strategy:{strat_reason}" if strat_reason else None
                    if reason:
                        fill_sell(bar.close, bar.ts + bar_sec, reason)
        if pos is None:
            sig = pre.entry(i)
            if sig is not None:
                pending = sig
        curve.append(cash + (pos.qty * bar.close if pos else 0.0))

    if pos is not None:  # mark-to-market close at the end
        fill_sell(candles[-1].close, candles[-1].ts + bar_sec, "end_of_data")
        curve[-1] = cash
    res = BacktestResult(strategy=strategy.name, params=dict(strategy.params), symbol=symbol, trades=trades, equity_curve=curve)
    res.metrics = compute_metrics(trades, curve, bar_sec, initial_cash)
    return res
