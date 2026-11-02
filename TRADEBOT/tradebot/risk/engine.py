"""Independent, deterministic risk engine.

Every order must be approved here. Approvals are HMAC tokens bound to the exact order fields and
short-lived; brokers verify them before touching money. Nothing else in the system (including
Claude) can mint a token or alter limits at runtime.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field

from ..config import RiskLimits
from ..models import OrderRequest, Position, Side
from .kill_switch import KillSwitch

log = logging.getLogger(__name__)


@dataclass
class PortfolioState:
    equity_usd: float
    cash_usd: float
    positions: dict[str, Position]
    trades_today: int
    realized_pnl_today_usd: float
    consecutive_losses: int
    last_loss_ts: float | None
    peak_equity_usd: float
    day_start_equity_usd: float
    # In-flight BUY orders (intents created/signed/submitted but not yet booked or resolved). Their notional is
    # already removed from cash_usd by the book and counts toward exposure and the open-position limit here.
    pending_buy_usd: float = 0.0
    pending_buy_symbols: frozenset = frozenset()


@dataclass
class MarketState:
    price: float
    liquidity_usd: float
    volume_24h_usd: float
    data_ts: float
    expected_slippage_bps: float = 0.0


@dataclass(frozen=True)
class RiskApproval:
    order_id: str
    symbol: str
    side: str
    qty: float
    notional_usd: float
    issued_ts: float
    token: str


@dataclass
class RiskDecision:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    adjusted_notional_usd: float = 0.0
    adjusted_qty: float | None = None
    approval: RiskApproval | None = None

    def __bool__(self) -> bool:
        return self.approved


class RiskEngine:
    def __init__(self, limits: RiskLimits, kill_switch: KillSwitch, clock=time.time):
        self.limits = limits.model_copy(deep=True)   # private copy: runtime mutation elsewhere has no effect
        self.kill = kill_switch
        self.clock = clock
        self._secret = secrets.token_bytes(32)
        self.consecutive_errors = 0
        self.last_data_ts: float | None = None
        self.daily_halt_day: str | None = None     # UTC day key while daily-loss halt is active
        self.daily_halt_reason: str | None = None
        self._halt_listeners: list = []
        self._consumed: dict[str, float] = {}         # order_id -> time the approval was used

    def on_daily_halt(self, fn) -> None:
        self._halt_listeners.append(fn)

    def _day_key(self, ts: float) -> str:
        import datetime as dt
        return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%d")

    def is_daily_halted(self) -> bool:
        if self.daily_halt_day is None:
            return False
        if self._day_key(self.clock()) != self.daily_halt_day:
            log.warning("daily-loss halt lifted: new UTC day")
            self.daily_halt_day = self.daily_halt_reason = None
            return False
        return True

    # ------------------------------------------------------------------ tokens
    def _sign(self, order_id: str, symbol: str, side: str, qty: float, notional: float, ts: float) -> str:
        msg = f"{order_id}|{symbol}|{side}|{qty:.12g}|{notional:.6f}|{ts:.3f}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def verify(self, approval: RiskApproval, order: OrderRequest, qty: float) -> None:
        """Raise if the approval does not match this order exactly or has expired."""
        now = self.clock()
        if now - approval.issued_ts > self.limits.approval_ttl_sec:
            raise PermissionError("risk approval expired")
        if approval.order_id != order.id or approval.symbol != order.symbol or approval.side != order.side.value:
            raise PermissionError("risk approval does not match order")
        if abs(approval.qty - qty) > max(1e-9, abs(qty) * 1e-6):
            raise PermissionError("risk approval quantity mismatch")
        expected = self._sign(approval.order_id, approval.symbol, approval.side, approval.qty, approval.notional_usd, approval.issued_ts)
        if not hmac.compare_digest(expected, approval.token):
            raise PermissionError("risk approval token invalid")
        # Single use: one approval authorises exactly one execution. A second execute() with the same approval
        # (a retry loop, a duplicated call, a replay within the TTL) is refused instead of trading twice.
        if approval.order_id in self._consumed:
            raise PermissionError("risk approval already used")
        self._consumed[approval.order_id] = now
        if len(self._consumed) > 10_000:                  # forget approvals far past their TTL
            cutoff = now - 10 * self.limits.approval_ttl_sec
            self._consumed = {k: v for k, v in self._consumed.items() if v >= cutoff}

    def guard_execution(self, order: OrderRequest) -> None:
        """Last check immediately before a broker quotes/signs: entries are refused if the kill switch or the
        daily halt became active after the approval was minted (another process, the CLI or the risk monitor
        may trip it in between). Risk-reducing exits are always allowed so positions can still be closed."""
        if order.side == Side.SELL and order.reduce_only:
            return
        if self.kill.is_tripped():
            raise PermissionError(f"kill switch tripped before execution: {self.kill.reason()}")
        if self.is_daily_halted():
            raise PermissionError(f"daily-loss halt active before execution: {self.daily_halt_reason}")

    def _mint(self, order: OrderRequest, qty: float, notional: float) -> RiskApproval:
        ts = self.clock()
        return RiskApproval(order_id=order.id, symbol=order.symbol, side=order.side.value, qty=qty,
                            notional_usd=notional, issued_ts=ts,
                            token=self._sign(order.id, order.symbol, order.side.value, qty, notional, ts))

    # ------------------------------------------------------------------ monitoring hooks
    def on_equity(self, state: PortfolioState) -> None:
        """Called on every equity snapshot. Trips the kill switch on daily-loss or drawdown breach."""
        if state.day_start_equity_usd > 0:
            daily_loss_pct = (state.day_start_equity_usd - state.equity_usd) / state.day_start_equity_usd * 100
            if daily_loss_pct >= self.limits.max_daily_loss_pct:
                reason = f"daily loss {daily_loss_pct:.2f}% >= limit {self.limits.max_daily_loss_pct}%"
                if self.limits.daily_loss_action == "kill":
                    self.kill.trip(reason, source="risk_engine")
                elif not self.is_daily_halted():
                    self.daily_halt_day = self._day_key(self.clock())
                    self.daily_halt_reason = reason
                    log.critical("DAILY LOSS HALT: %s. No new entries until next UTC day.", reason)
                    for fn in list(self._halt_listeners):
                        try:
                            fn(reason)
                        except Exception:  # noqa: BLE001
                            log.exception("daily halt listener failed")
        if state.peak_equity_usd > 0:
            dd_pct = (state.peak_equity_usd - state.equity_usd) / state.peak_equity_usd * 100
            if dd_pct >= self.limits.max_drawdown_pct:
                self.kill.trip(f"drawdown {dd_pct:.2f}% >= limit {self.limits.max_drawdown_pct}%", source="risk_engine")

    def on_data(self, data_ts: float) -> None:
        self.last_data_ts = data_ts
        age = self.clock() - data_ts
        if age > self.limits.stale_data_sec:
            self.kill.trip(f"market data stale for {age:.0f}s > {self.limits.stale_data_sec}s", source="risk_engine")

    def on_execution_error(self, err: str) -> None:
        self.consecutive_errors += 1
        if self.consecutive_errors >= self.limits.max_consecutive_errors:
            self.kill.trip(f"{self.consecutive_errors} consecutive execution errors; last: {err}", source="risk_engine")

    def on_execution_success(self) -> None:
        self.consecutive_errors = 0

    # ------------------------------------------------------------------ evaluation
    def evaluate(self, order: OrderRequest, pf: PortfolioState, mkt: MarketState) -> RiskDecision:
        L = self.limits
        reasons: list[str] = []
        now = self.clock()

        # Risk-reducing exits are allowed even when tripped, so positions can be flattened.
        if order.side == Side.SELL and order.reduce_only:
            pos = pf.positions.get(order.symbol)
            if pos is None or pos.qty <= 0:
                return RiskDecision(False, ["no position to reduce"])
            qty = min(order.qty if order.qty is not None else pos.qty, pos.qty)
            notional = qty * mkt.price
            return RiskDecision(True, ["reduce-only exit"], notional, qty, self._mint(order, qty, notional))

        if self.kill.is_tripped():
            return RiskDecision(False, [f"kill switch tripped: {self.kill.reason()}"])
        if self.is_daily_halted():
            return RiskDecision(False, [f"daily-loss halt active: {self.daily_halt_reason}"])
        if order.side != Side.BUY:
            return RiskDecision(False, ["only reduce-only sells or buys are supported"])

        sym = order.symbol
        if L.allowlist and sym not in L.allowlist:
            reasons.append(f"{sym} not in allowlist")
        if sym in L.denylist:
            reasons.append(f"{sym} is denylisted")
        if mkt.price <= 0:
            reasons.append("no valid price")
        if now - mkt.data_ts > L.stale_data_sec:
            reasons.append("market data stale")
        if mkt.liquidity_usd < L.min_liquidity_usd:
            reasons.append(f"liquidity ${mkt.liquidity_usd:,.0f} < ${L.min_liquidity_usd:,.0f}")
        if mkt.volume_24h_usd < L.min_volume_24h_usd:
            reasons.append(f"24h volume ${mkt.volume_24h_usd:,.0f} < ${L.min_volume_24h_usd:,.0f}")
        if mkt.expected_slippage_bps > L.max_slippage_bps:
            reasons.append(f"expected slippage {mkt.expected_slippage_bps:.0f}bps > {L.max_slippage_bps}bps")
        if pf.trades_today >= L.max_trades_per_day:
            reasons.append(f"daily trade cap {L.max_trades_per_day} reached")
        if pf.consecutive_losses >= L.max_consecutive_losses and pf.last_loss_ts is not None \
                and now - pf.last_loss_ts < L.loss_streak_pause_sec:
            remaining = L.loss_streak_pause_sec - (now - pf.last_loss_ts)
            reasons.append(f"{pf.consecutive_losses} consecutive losses; entries paused for {remaining/60:.0f} more min")
        if pf.last_loss_ts is not None and now - pf.last_loss_ts < L.loss_cooldown_sec:
            reasons.append("in post-loss cooldown")
        open_count = len(pf.positions) + len(set(pf.pending_buy_symbols) - set(pf.positions))
        if sym in pf.positions and pf.positions[sym].qty > 0:
            reasons.append("already holding this symbol")
        elif sym in pf.pending_buy_symbols:
            reasons.append("an order for this symbol is still in flight")
        elif open_count >= L.max_open_positions:
            reasons.append(f"max open positions {L.max_open_positions} reached (incl. {len(pf.pending_buy_symbols)} in flight)")
        if pf.day_start_equity_usd > 0:
            daily_loss_pct = (pf.day_start_equity_usd - pf.equity_usd) / pf.day_start_equity_usd * 100
            if daily_loss_pct >= L.max_daily_loss_pct:
                reasons.append("daily loss limit reached")
        if reasons:
            return RiskDecision(False, reasons)

        # Size down to the tightest constraint instead of rejecting outright.
        exposure = sum(p.qty * mkt.price if p.symbol == sym else p.cost_basis_usd for p in pf.positions.values()) + pf.pending_buy_usd
        caps = {
            "requested": order.notional_usd,
            "max_position": pf.equity_usd * L.max_position_pct / 100,
            "max_total_exposure": pf.equity_usd * L.max_total_exposure_pct / 100 - exposure,
            "max_order": L.max_order_notional_usd,
            "cash": pf.cash_usd * 0.995,  # leave room for fees
            "liquidity_1pct": mkt.liquidity_usd * 0.01,
        }
        binding = min(caps, key=caps.get)
        notional = max(0.0, caps[binding])
        if notional < L.min_order_notional_usd:
            return RiskDecision(False, [f"sized notional ${notional:.2f} below minimum ${L.min_order_notional_usd} (binding: {binding})"])
        qty = notional / mkt.price
        note = ["approved"] if binding == "requested" else [f"approved, sized down by {binding}"]
        return RiskDecision(True, note, notional, qty, self._mint(order, qty, notional))
