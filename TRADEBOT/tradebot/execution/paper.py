"""Paper broker: simulated fills at provider prices with slippage and fees. Holds no real funds."""
from __future__ import annotations

import logging
import random

from ..data.base import MarketDataProvider
from ..models import Fill, OrderRequest, Side
from ..risk.engine import RiskEngine
from .base import Broker, ExecutionError, Quote

log = logging.getLogger(__name__)


class PaperBroker(Broker):
    def __init__(self, risk: RiskEngine, provider: MarketDataProvider, fee_bps: float = 30.0,
                 slippage_bps: float = 15.0, seed: int = 7, clock=None):
        super().__init__(risk)
        self.provider = provider
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        self.rng = random.Random(seed)
        self.clock = clock

    def _price(self, symbol: str) -> float:
        t = self.provider.get_tickers([symbol]).get(symbol)
        if not t or t.price <= 0:
            raise ExecutionError(f"no price for {symbol}")
        return t.price

    def quote(self, symbol: str, side: Side, amount: float) -> Quote:
        mid = self._price(symbol)
        slip_bps = float(self.slippage_bps * self.rng.uniform(0.3, 1.0))
        px = mid * (1 + slip_bps / 1e4) if side == Side.BUY else mid * (1 - slip_bps / 1e4)
        if side == Side.BUY:
            fee = amount * self.fee_bps / 1e4
            qty = (amount - fee) / px
            return Quote(symbol, side, px, slip_bps, fee, amount, qty)
        gross = amount * px
        fee = gross * self.fee_bps / 1e4
        return Quote(symbol, side, px, slip_bps, fee, amount, gross - fee)

    def _execute(self, order: OrderRequest, qty: float, q: Quote) -> Fill:
        ts = self.clock() if self.clock else None
        if order.side == Side.BUY:
            fill = Fill(order_id=order.id, symbol=order.symbol, side=order.side, qty=q.out_amount, price=q.price,
                        notional_usd=q.in_amount, fee_usd=q.fee_usd, slippage_bps=q.expected_slippage_bps, tx_signature="paper")
        else:
            fill = Fill(order_id=order.id, symbol=order.symbol, side=order.side, qty=qty, price=q.price,
                        notional_usd=qty * q.price, fee_usd=q.fee_usd, slippage_bps=q.expected_slippage_bps, tx_signature="paper")
        if ts is not None:
            fill.ts = ts
        log.info("PAPER FILL %s %s qty=%.6g px=%.6g notional=$%.2f fee=$%.2f", fill.side.value.upper(), fill.symbol,
                 fill.qty, fill.price, fill.notional_usd, fill.fee_usd)
        return fill
