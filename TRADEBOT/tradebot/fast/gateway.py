"""Deterministic pre-trade risk gateway for the fast engine.

Runs BEFORE the shared RiskEngine and BEFORE any order: both must approve. It is a pure function of a state snapshot
built from the fast ledger plus the request, so the same inputs always give the same answer, and it fails closed:
if the snapshot cannot be built, or anything inside raises, the entry is rejected with `risk_service_unavailable`.

Limits are in USD and deliberately small relative to the paper capital (see FastRiskConfig). Exits are never blocked
by the gateway (positions must always be closable); it only checks that an exit does not sell more than is held.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

from ..config import FastRiskConfig
from ..portfolio.ledger import Ledger


@dataclass
class GatewayState:
    now: float
    equity_usd: float
    cash_usd: float                       # already net of in-flight buys
    positions: dict[str, float]           # symbol -> qty
    exposure_usd: float                   # marked positions + in-flight buys
    pending_symbols: frozenset
    realized_today_usd: float
    unrealized_usd: float
    peak_equity_usd: float
    entries_last_hour: int
    entries_today: int
    last_entry_ts: float | None
    last_entry_by_symbol: dict[str, float]
    consecutive_losses: int
    last_loss_ts: float | None


@dataclass
class EntryRequest:
    symbol: str
    notional_usd: float
    stop_pct: float
    entry_cost_worst_bps: float           # all-in round trip (entry estimate)
    price_age_sec: float | None           # local age of the venue price used
    quote_age_sec: float | None
    reference_age_sec: float | None = None


@dataclass
class GatewayDecision:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    max_notional_usd: float = 0.0
    max_loss_usd: float = 0.0


def _day_start(ts: float) -> float:
    d = dt.datetime.fromtimestamp(ts, dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp()


def build_state(ledger: Ledger, book, prices: dict[str, float], now: float) -> GatewayState:
    """Snapshot from the fast ledger and book. Raises on any inconsistency (the caller fails closed)."""
    rs = book.risk_state(prices)
    positions = {s: p.qty for s, p in book.positions.items()}
    exposure = sum(p.qty * prices.get(s, p.avg_cost) for s, p in book.positions.items()) + rs.pending_buy_usd
    unreal = sum((prices.get(s, p.avg_cost) - p.avg_cost) * p.qty for s, p in book.positions.items())
    day0 = _day_start(now)
    buys = ledger._exec("SELECT ts, symbol FROM fills WHERE mode=? AND side='buy' AND ts>=? ORDER BY ts",
                        (ledger.mode, now - 86400 * 2)).fetchall()
    last_by_sym: dict[str, float] = {}
    for r in buys:
        last_by_sym[r["symbol"]] = r["ts"]
    losses, last_loss = ledger.consecutive_losses()
    return GatewayState(now=now, equity_usd=rs.equity_usd, cash_usd=rs.cash_usd, positions=positions, exposure_usd=exposure,
                        pending_symbols=rs.pending_buy_symbols, realized_today_usd=ledger.realized_pnl_since(day0),
                        unrealized_usd=unreal, peak_equity_usd=max(rs.peak_equity_usd, rs.equity_usd),
                        entries_last_hour=sum(1 for r in buys if r["ts"] >= now - 3600),
                        entries_today=sum(1 for r in buys if r["ts"] >= day0),
                        last_entry_ts=buys[-1]["ts"] if buys else None, last_entry_by_symbol=last_by_sym,
                        consecutive_losses=losses, last_loss_ts=last_loss)


class PreTradeGateway:
    def __init__(self, cfg: FastRiskConfig):
        self.cfg = cfg

    def check_entry(self, req: EntryRequest, st: GatewayState) -> GatewayDecision:
        try:
            return self._check_entry(req, st)
        except Exception as e:  # noqa: BLE001 - fail closed
            return GatewayDecision(False, [f"risk_service_unavailable: {type(e).__name__}: {e}"])

    def _check_entry(self, req: EntryRequest, st: GatewayState) -> GatewayDecision:
        c, r = self.cfg, []
        now = st.now
        # data freshness (fail closed on missing values)
        if req.price_age_sec is None or req.price_age_sec > c.max_price_age_sec:
            r.append(f"stale_price({req.price_age_sec if req.price_age_sec is None else round(req.price_age_sec, 1)}s>{c.max_price_age_sec:g}s)")
        if req.quote_age_sec is None or req.quote_age_sec > c.max_quote_age_sec:
            r.append(f"stale_quote({req.quote_age_sec if req.quote_age_sec is None else round(req.quote_age_sec, 1)}s>{c.max_quote_age_sec:g}s)")
        if req.reference_age_sec is not None and req.reference_age_sec > c.max_reference_age_sec:
            r.append(f"stale_reference({req.reference_age_sec:.1f}s>{c.max_reference_age_sec:g}s)")
        # loss limits
        day_pnl = st.realized_today_usd + st.unrealized_usd
        if day_pnl <= -c.daily_loss_limit_usd:
            r.append(f"daily_loss({day_pnl:.2f}<=-{c.daily_loss_limit_usd:g})")
        if st.peak_equity_usd - st.equity_usd >= c.max_drawdown_usd:
            r.append(f"drawdown({st.peak_equity_usd - st.equity_usd:.2f}>={c.max_drawdown_usd:g})")
        if st.consecutive_losses >= c.max_consecutive_losses and st.last_loss_ts is not None \
                and now - st.last_loss_ts < c.loss_streak_pause_sec:
            r.append(f"loss_streak({st.consecutive_losses})")
        # frequency
        if st.entries_last_hour >= c.max_trades_per_hour:
            r.append(f"trades_per_hour({st.entries_last_hour}>={c.max_trades_per_hour})")
        if st.entries_today >= c.max_trades_per_day:
            r.append(f"trades_per_day({st.entries_today}>={c.max_trades_per_day})")
        if st.last_entry_ts is not None and now - st.last_entry_ts < c.min_seconds_between_entries:
            r.append(f"entry_spacing({now - st.last_entry_ts:.0f}s<{c.min_seconds_between_entries:g}s)")
        last_sym = st.last_entry_by_symbol.get(req.symbol)
        if last_sym is not None and now - last_sym < c.symbol_cooldown_sec:
            r.append(f"symbol_cooldown({now - last_sym:.0f}s<{c.symbol_cooldown_sec:g}s)")
        # exposure / positions
        if req.symbol in st.positions or req.symbol in st.pending_symbols:
            r.append("already_exposed_to_symbol")
        open_count = len(set(st.positions) | set(st.pending_symbols))
        if open_count >= c.max_open_positions:
            r.append(f"open_positions({open_count}>={c.max_open_positions})")
        if req.entry_cost_worst_bps > c.max_entry_cost_bps:
            r.append(f"entry_cost({req.entry_cost_worst_bps:.0f}bps>{c.max_entry_cost_bps:g})")
        if r:
            return GatewayDecision(False, r)
        # sizing: the tightest cap wins; a size below the minimum is a rejection, not a smaller order
        loss_per_usd = req.stop_pct / 100 + max(req.entry_cost_worst_bps, 0.0) / 1e4
        caps = {
            "requested": req.notional_usd,
            "max_position": c.max_position_usd,
            "capital": c.max_capital_usd - st.exposure_usd,
            "exposure": c.max_total_exposure_usd - st.exposure_usd,
            "cash": st.cash_usd * 0.99,
            "loss_per_trade": c.max_loss_per_trade_usd / loss_per_usd if loss_per_usd > 0 else float("inf"),
        }
        binding = min(caps, key=caps.get)
        size = max(0.0, caps[binding])
        if size < c.min_notional_usd:
            return GatewayDecision(False, [f"size_below_min({size:.2f}<{c.min_notional_usd:g}; binding {binding})"])
        return GatewayDecision(True, [f"approved (binding {binding})"], size, size * loss_per_usd)

    def check_exit(self, symbol: str, qty: float, st: GatewayState) -> GatewayDecision:
        """Exits are always allowed when they only reduce a held position."""
        held = st.positions.get(symbol, 0.0)
        if qty <= 0 or qty > held * (1 + 1e-9):
            return GatewayDecision(False, [f"exit_qty({qty:.8g}) exceeds held({held:.8g})"])
        return GatewayDecision(True, ["exit allowed"], qty)


def describe(cfg: FastRiskConfig) -> str:
    return json.dumps(cfg.model_dump())
