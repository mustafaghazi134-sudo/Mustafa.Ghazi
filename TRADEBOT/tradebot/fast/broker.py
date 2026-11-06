"""Paper broker for the fast engine. PAPER ONLY: it never signs or sends anything.

Fills come from the same rate-limited, size-specific Jupiter /order quotes (no taker) that the engine used for its
decision, filled at the quote's MINIMUM acceptable amount (otherAmountThreshold), with the network fee from the
quote's lamport fields (or an assumed $0.01) charged in cash. A quote is reused for execution only while it is
fresher than `max_quote_age_sec`; otherwise a new one is fetched. No quote -> ExecutionError (fail closed).
"""
from __future__ import annotations

from typing import Callable

from ..execution.base import ExecutionError
from ..execution.base import Quote as BrokerQuote
from ..execution.paper import PaperBroker
from ..models import Side
from ..risk.engine import RiskEngine
from ..scanner.costs import Quote as JupQuote
from ..scanner.scanner import USDC
from .costs import ASSUMED_TX_FEE_USD, LAMPORTS_PER_SOL


class FastPaperBroker(PaperBroker):
    def __init__(self, risk: RiskEngine, provider, feed, resolve: Callable[[str], tuple[str, int]],
                 sol_price: Callable[[], float | None], clock: Callable[[], float], slippage_bps: int = 50,
                 max_quote_age_sec: float = 2.0):
        super().__init__(risk, provider, clock=clock)
        self.feed = feed
        self.resolve = resolve                 # symbol -> (mint, decimals)
        self.sol_price = sol_price
        self.slip = slippage_bps
        self.max_quote_age_sec = max_quote_age_sec
        self._cache: dict[tuple, tuple[float, BrokerQuote, JupQuote]] = {}
        self.quote_calls = 0
        self.quote_source = "jupiter min-out"

    def _fee_usd(self, q: JupQuote) -> float:
        sol = self.sol_price()
        if sol and (q.signature_fee_lamports is not None or q.priority_fee_lamports is not None):
            return ((q.signature_fee_lamports or 0) + (q.priority_fee_lamports or 0)) / LAMPORTS_PER_SOL * sol
        return ASSUMED_TX_FEE_USD

    def fetch(self, symbol: str, side: Side, amount: float) -> JupQuote:
        """amount: USD for buys, token qty for sells. Raises ExecutionError when no usable quote exists."""
        mint, dec = self.resolve(symbol)
        if side == Side.BUY:
            res = self.feed.order(USDC, mint, int(round(amount * 1e6)), self.slip)
        else:
            res = self.feed.order(mint, USDC, int(round(amount * 10 ** dec)), self.slip)
        self.quote_calls += 1
        if not res.ok or not res.data or res.data.get("error"):
            raise ExecutionError(f"no quote for {side.value} {symbol} (status {res.status})")
        try:
            return JupQuote.from_order(res.data, res.latency_ms, self.clock())
        except (KeyError, ValueError) as e:
            raise ExecutionError(f"unusable quote for {symbol}: {e}") from e

    def quote(self, symbol: str, side: Side, amount: float) -> BrokerQuote:
        key = (symbol, side.value, round(amount, 10))
        now = self.clock()
        hit = self._cache.get(key)
        if hit and now - hit[0] <= self.max_quote_age_sec:
            return hit[1]
        jq = self.fetch(symbol, side, amount)
        _, dec = self.resolve(symbol)
        fee = self._fee_usd(jq)
        impact = jq.price_impact_pct * 1e4
        if side == Side.BUY:
            qty = jq.min_out_amount / 10 ** dec
            if qty <= 0:
                raise ExecutionError(f"zero fill for {symbol}")
            bq = BrokerQuote(symbol, side, amount / qty, impact, fee, amount + fee, qty, raw={"jupiter": jq.__dict__})
        else:
            usd = jq.min_out_amount / 1e6
            if amount <= 0:
                raise ExecutionError(f"zero qty for {symbol}")
            bq = BrokerQuote(symbol, side, usd / amount, impact, fee, amount, usd - fee, raw={"jupiter": jq.__dict__})
        self._cache[key] = (now, bq, jq)
        if len(self._cache) > 64:
            self._cache = dict(list(self._cache.items())[-32:])
        return bq

    def last_jupiter_quote(self, symbol: str, side: Side, amount: float) -> JupQuote | None:
        hit = self._cache.get((symbol, side.value, round(amount, 10)))
        return hit[2] if hit else None
