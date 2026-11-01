from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

from ..models import Fill, OrderRequest, Side
from ..risk.engine import RiskApproval, RiskEngine


class ExecutionError(RuntimeError):
    pass


class SubmittedUnknownError(ExecutionError):
    """The transaction may have been broadcast but the outcome is unknown (timeout, connection drop).
    Carries what is needed to reconcile later. Callers must NOT resubmit a new order for the symbol."""

    def __init__(self, message: str, tx_signature: str | None, request_id: str | None):
        super().__init__(message)
        self.tx_signature = tx_signature
        self.request_id = request_id


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
        # Called with {"request_id", "tx_signature", "signed_tx"} AFTER signing and BEFORE submission so the
        # engine can persist the intent durably; a crash between the two can then be reconciled on restart.
        self.presubmit_hook: Callable[[dict], None] | None = None

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

    def lookup_signature(self, tx_signature: str) -> str:
        """'confirmed' | 'failed' | 'not_found' for a previously signed transaction. Used by startup reconciliation."""
        return "not_found"
