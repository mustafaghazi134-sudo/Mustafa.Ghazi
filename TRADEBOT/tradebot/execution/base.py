from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..models import Fill, OrderRequest, Side
from ..risk.engine import RiskApproval, RiskEngine


class ExecutionError(RuntimeError):
    pass


@dataclass
class Quote:
    symbol: str
    side: Side
    price: float                 # expected effective price (USD per token)
    expected_slippage_bps: float
    fee_usd: float
    in_amount: float             # USD for buys, token qty for sells
    out_amount: float            # token qty for buys, USD for sells
    raw: dict | None = None


class Broker(ABC):
    """Brokers execute only orders carrying a valid RiskEngine approval."""

    def __init__(self, risk: RiskEngine):
        self.risk = risk

    @abstractmethod
    def quote(self, symbol: str, side: Side, amount: float) -> Quote:
        """amount = USD notional for buys, token qty for sells."""

    @abstractmethod
    def _execute(self, order: OrderRequest, qty: float, quote: Quote) -> Fill: ...

    def execute(self, order: OrderRequest, qty: float, approval: RiskApproval) -> Fill:
        self.risk.verify(approval, order, qty)       # raises PermissionError
        amount = approval.notional_usd if order.side == Side.BUY else qty
        q = self.quote(order.symbol, order.side, amount)
        if q.expected_slippage_bps > self.risk.limits.max_slippage_bps:
            raise ExecutionError(f"quote slippage {q.expected_slippage_bps:.0f}bps exceeds limit {self.risk.limits.max_slippage_bps}bps")
        return self._execute(order, qty, q)

    def get_balances(self) -> dict[str, float]:
        return {}
