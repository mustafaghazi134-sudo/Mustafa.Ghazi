"""Core data models shared across the bot."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Mode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


def now_ts() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class TokenInfo:
    symbol: str
    mint: str
    decimals: int = 9
    name: str = ""


@dataclass
class Candle:
    ts: float          # bar open time, unix seconds
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Ticker:
    symbol: str
    price: float
    ts: float
    volume_24h_usd: float = 0.0
    liquidity_usd: float = 0.0
    change_24h_pct: float = 0.0


@dataclass
class Signal:
    """A strategy's intent. Never executed directly; it must pass vetting and risk."""
    symbol: str
    side: Side
    strategy: str
    strength: float                 # 0..1 confidence; sizes scale with it
    reason: str
    price: float                    # reference price when generated
    stop_loss: float | None = None
    take_profit: float | None = None
    trailing_stop_pct: float | None = None
    max_hold_sec: float | None = None
    ts: float = field(default_factory=now_ts)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderRequest:
    symbol: str
    side: Side
    notional_usd: float             # for buys: USD to spend; for sells: USD value of qty
    qty: float | None = None        # token quantity (required for sells)
    strategy: str = ""
    reason: str = ""
    reduce_only: bool = False       # closing/flattening an existing position
    signal: Signal | None = None
    id: str = field(default_factory=lambda: new_id("ord"))
    ts: float = field(default_factory=now_ts)


@dataclass
class Fill:
    order_id: str
    symbol: str
    side: Side
    qty: float
    price: float                    # effective average fill price in USD
    notional_usd: float
    fee_usd: float
    ts: float = field(default_factory=now_ts)
    tx_signature: str | None = None
    id: str = field(default_factory=lambda: new_id("fill"))
    slippage_bps: float = 0.0


@dataclass
class Position:
    symbol: str
    qty: float
    avg_cost: float                 # USD per token
    opened_ts: float
    strategy: str = ""
    stop_loss: float | None = None
    take_profit: float | None = None
    trailing_stop_pct: float | None = None
    max_hold_sec: float | None = None
    high_water: float = 0.0         # highest price seen since open (for trailing)
    fees_usd: float = 0.0
    id: str = field(default_factory=lambda: new_id("pos"))

    @property
    def cost_basis_usd(self) -> float:
        return self.qty * self.avg_cost

    def market_value(self, price: float) -> float:
        return self.qty * price

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.avg_cost) * self.qty


@dataclass
class ClosedTrade:
    symbol: str
    strategy: str
    qty: float
    entry_price: float
    exit_price: float
    entry_ts: float
    exit_ts: float
    fees_usd: float
    pnl_usd: float                  # net of fees
    exit_reason: str
    id: str = field(default_factory=lambda: new_id("trd"))

    @property
    def return_pct(self) -> float:
        cost = self.qty * self.entry_price
        return (self.pnl_usd / cost * 100.0) if cost else 0.0


@dataclass
class EquityPoint:
    ts: float
    equity_usd: float
    cash_usd: float
    exposure_usd: float
    realized_pnl_usd: float
    unrealized_pnl_usd: float
