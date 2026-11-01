"""Portfolio bookkeeping: cash, positions, realized/unrealized P&L, equity snapshots, risk state."""
from __future__ import annotations

import datetime as dt
import logging

from ..models import ClosedTrade, EquityPoint, Fill, Position, Side, Signal
from ..risk.engine import PortfolioState
from .ledger import Ledger

log = logging.getLogger(__name__)


def _day_key(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%d")


class PortfolioBook:
    def __init__(self, ledger: Ledger, initial_cash: float, clock):
        self.ledger = ledger
        self.clock = clock
        self.positions: dict[str, Position] = ledger.load_positions()
        self.cash: float = ledger.get("cash", initial_cash)
        self.initial_capital: float = ledger.get("initial_capital", initial_cash)
        if ledger.get("initial_capital") is None:
            ledger.set("initial_capital", initial_cash)
            ledger.set("cash", self.cash)
        self.peak_equity: float = ledger.get("peak_equity", initial_cash)
        self.day_key: str = ledger.get("day_key", _day_key(clock()))
        self.day_start_equity: float = ledger.get("day_start_equity", initial_cash)

    # ---- fills ---------------------------------------------------------------------------------
    def apply_fill(self, fill: Fill, signal: Signal | None = None, exit_reason: str = "") -> ClosedTrade | None:
        self.ledger.record_fill(fill)
        if fill.side == Side.BUY:
            self.cash -= fill.notional_usd
            pos = self.positions.get(fill.symbol)
            if pos is None:
                pos = Position(symbol=fill.symbol, qty=fill.qty, avg_cost=fill.price, opened_ts=fill.ts,
                               strategy=signal.strategy if signal else "", fees_usd=fill.fee_usd, high_water=fill.price)
                if signal:
                    pos.stop_loss, pos.take_profit = signal.stop_loss, signal.take_profit
                    pos.trailing_stop_pct, pos.max_hold_sec = signal.trailing_stop_pct, signal.max_hold_sec
            else:
                total_cost = pos.avg_cost * pos.qty + fill.price * fill.qty
                pos.qty += fill.qty
                pos.avg_cost = total_cost / pos.qty
                pos.fees_usd += fill.fee_usd
            self.positions[fill.symbol] = pos
            self.ledger.upsert_position(pos)
            self.ledger.set("cash", self.cash)
            return None

        pos = self.positions.get(fill.symbol)
        if pos is None:
            log.error("sell fill for %s with no position; crediting cash only", fill.symbol)
            self.cash += fill.notional_usd - fill.fee_usd
            self.ledger.set("cash", self.cash)
            return None
        qty = min(fill.qty, pos.qty)
        proceeds = fill.notional_usd - fill.fee_usd
        self.cash += proceeds
        entry_fees_share = pos.fees_usd * (qty / pos.qty) if pos.qty else 0.0
        pnl = proceeds - qty * pos.avg_cost - entry_fees_share
        trade = ClosedTrade(symbol=fill.symbol, strategy=pos.strategy, qty=qty, entry_price=pos.avg_cost, exit_price=fill.price,
                            entry_ts=pos.opened_ts, exit_ts=fill.ts, fees_usd=fill.fee_usd + entry_fees_share, pnl_usd=pnl,
                            exit_reason=exit_reason or "sell")
        pos.qty -= qty
        pos.fees_usd -= entry_fees_share
        if pos.qty <= 1e-12:
            del self.positions[fill.symbol]
            self.ledger.delete_position(fill.symbol)
        else:
            self.ledger.upsert_position(pos)
        self.ledger.record_trade(trade)
        self.ledger.set("cash", self.cash)
        log.info("CLOSED %s %s pnl=$%.2f (%.2f%%) reason=%s", trade.symbol, trade.strategy, trade.pnl_usd, trade.return_pct, trade.exit_reason)
        return trade

    # ---- valuation -----------------------------------------------------------------------------
    def exposure(self, prices: dict[str, float]) -> float:
        return sum(p.market_value(prices.get(p.symbol, p.avg_cost)) for p in self.positions.values())

    def unrealized(self, prices: dict[str, float]) -> float:
        return sum(p.unrealized_pnl(prices.get(p.symbol, p.avg_cost)) for p in self.positions.values())

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.exposure(prices)

    def snapshot(self, prices: dict[str, float]) -> EquityPoint:
        now = self.clock()
        eq = self.equity(prices)
        day = _day_key(now)
        if day != self.day_key:
            self.day_key, self.day_start_equity = day, eq
            self.ledger.set("day_key", day)
            self.ledger.set("day_start_equity", eq)
        if eq > self.peak_equity:
            self.peak_equity = eq
            self.ledger.set("peak_equity", eq)
        day_start_ts = dt.datetime.strptime(self.day_key, "%Y-%m-%d").replace(tzinfo=dt.UTC).timestamp()
        if prices:
            self.ledger.set("last_prices", {k: float(v) for k, v in prices.items()})
        pt = EquityPoint(ts=now, equity_usd=eq, cash_usd=self.cash, exposure_usd=self.exposure(prices),
                         realized_pnl_usd=self.ledger.realized_pnl_since(day_start_ts), unrealized_pnl_usd=self.unrealized(prices))
        self.ledger.add_equity(pt)
        return pt

    def risk_state(self, prices: dict[str, float]) -> PortfolioState:
        now = self.clock()
        day_start_ts = dt.datetime.strptime(self.day_key, "%Y-%m-%d").replace(tzinfo=dt.UTC).timestamp()
        losses, last_loss = self.ledger.consecutive_losses()
        return PortfolioState(equity_usd=self.equity(prices), cash_usd=self.cash, positions=dict(self.positions),
                              trades_today=self.ledger.count_buy_fills_since(day_start_ts),
                              realized_pnl_today_usd=self.ledger.realized_pnl_since(day_start_ts),
                              consecutive_losses=losses, last_loss_ts=last_loss, peak_equity_usd=self.peak_equity,
                              day_start_equity_usd=self.day_start_equity if now else self.day_start_equity)
