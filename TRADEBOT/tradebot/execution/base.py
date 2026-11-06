from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

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


@runtime_checkable
class ExecutionAdapter(Protocol):
    """The contract every execution venue implements (PaperBroker, JupiterExecutionAdapter):

    kind                      "paper" | "dry_run" | "live"
    quote(sym, side, amount)  read-only price for amount (USD for buys, tokens for sells)
    execute(order, qty, appr) requires a single-use RiskEngine approval; returns a Fill, raises ExecutionError when
                              provably nothing was submitted, SubmittedUnknownError when the outcome needs chain evidence
    presubmit_hook            called with {request_id, tx_signature, signed_tx[, quote]} after signing, before sending
    transaction_status(...)   evidence for reconciliation: {"status": confirmed|failed|not_found|processing, "blockhash_valid"}
    chain_fill(intent_row)    the fill as recorded on chain for a confirmed intent, or None (venue has no chain)
    signature_for_order(id)   what was signed for an order whose signature was never persisted, or None
    get_balances()            wallet balances by symbol ({} for paper)
    """
    kind: str
    presubmit_hook: Callable[[dict], None] | None

    def quote(self, symbol: str, side: Side, amount: float) -> "Quote": ...
    def execute(self, order: OrderRequest, qty: float, approval: RiskApproval) -> Fill: ...
    def transaction_status(self, tx_signature: str, signed_tx_b64: str | None) -> dict: ...
    def chain_fill(self, row) -> Fill | None: ...
    def signature_for_order(self, order_id: str) -> dict | None: ...
    def get_balances(self) -> dict[str, float]: ...


class Broker(ABC):
    """Brokers execute only orders carrying a valid RiskEngine approval."""

    kind = "paper"

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
        self.risk.verify(approval, order, qty)       # raises PermissionError; consumes the approval (single use)
        self.risk.guard_execution(order)             # kill switch / daily halt re-checked right before quoting
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

    def transaction_status(self, tx_signature: str, signed_tx_b64: str | None) -> dict:
        """Evidence for reconciliation: {"status": confirmed|failed|not_found, "blockhash_valid": True|False|None}.
        blockhash_valid False means the transaction can no longer land (safe to expire); None means unknown."""
        return {"status": self.lookup_signature(tx_signature), "blockhash_valid": None}

    def chain_fill(self, row) -> Fill | None:
        """Fill as recorded on chain for a confirmed intent; None when the venue has no chain record."""
        return None

    def signature_for_order(self, order_id: str) -> dict | None:
        """{"signature", "signed_tx"} if something was signed for this order but never persisted; None otherwise."""
        return None
