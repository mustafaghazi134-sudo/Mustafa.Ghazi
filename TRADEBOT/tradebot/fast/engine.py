"""The fast engine loop. PAPER ONLY by construction: the broker is always a paper broker.

Per poll (default 5 s), all deterministic Python, no Claude calls:
  0. RECOVER  settle unresolved order intents on evidence (recovery.reconcile_intents)
  1. SCAN     Jupiter Price v3 (rate-limited) + Binance reference (WebSocket or REST) + token facts
  2. DETECT   1-minute bars -> deterministic candidates (scanner.signals)
  3. COST     two size-specific /order quotes -> all-in entry estimate (fast.costs) -> net edge
  4. GATEWAY  deterministic pre-trade limits (fast.gateway), fail closed
  5. RISK     shared RiskEngine (single-use HMAC approval), fail closed
  6. BUY      re-quote immediately before the order; edge and quote age re-checked; durable intent; paper fill
  7. MANAGE   stop-loss / take-profit / trailing / max hold, on fresh prices only
  8. SELL     exit quote + exit cost estimate, reduce-only through gateway, risk engine and broker
  9. RECORD   atomic booking; ledger self-check after every fill; JSON events; per-stage latency; alerts

Fail-closed rules: no fresh venue price, no token facts, no quote, stale quote, gateway or risk exception -> no
entry. Feed outages raise alerts; they never trip the shared kill switch. Automatic halts write data/FAST_HALT,
which stops only this engine; the shared data/KILL stops both engines and is always obeyed.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config import Settings
from ..data.base import MarketDataProvider
from ..execution.base import ExecutionError, SubmittedUnknownError
from ..models import Candle, OrderRequest, Side, Signal, Ticker, TokenInfo
from ..portfolio.accounting import PortfolioBook
from ..portfolio.ledger import DuplicateOrderError, Ledger
from ..risk.engine import MarketState, RiskEngine
from ..risk.kill_switch import KillSwitch
from ..scanner.filters import token_reasons
from ..scanner.scanner import Feed, OpportunityScanner
from ..scanner.signals import Candidate, evaluate_all
from ..scanner.store import ScannerStore
from .broker import FastPaperBroker
from .costs import estimate_entry, estimate_exit
from .gateway import EntryRequest, PreTradeGateway, build_state
from .ratelimit import RateLimitedFeed, RateLimiter
from .recovery import reconcile_intents, verify_balances
from .telemetry import AlertManager, FastHalt, JsonEventLog, LatencyMetrics

log = logging.getLogger("tradebot.fast")


class FastProvider(MarketDataProvider):
    """Price view for the paper broker: the engine's latest venue prices and token facts."""

    def __init__(self, jup=None):
        self.prices: dict[str, Ticker] = {}
        self.jup = jup

    def get_tickers(self, symbols: list[str]) -> dict[str, Ticker]:
        return {s: self.prices[s] for s in symbols if s in self.prices}

    def get_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        return []

    def get_universe(self, max_symbols: int) -> list[TokenInfo]:
        return []


@dataclass
class _Px:
    price: float
    block: int | None
    recv_ts: float          # local time this price was received
    changed_ts: float       # local time the price or its blockId last changed


class FastEngine(OpportunityScanner):
    def __init__(self, settings: Settings, store: ScannerStore, feed: Feed, ledger: Ledger, kill: KillSwitch,
                 clock: Callable[[], float] = time.time, broker=None, limiter: RateLimiter | None = None,
                 events: JsonEventLog | None = None, alerts: AlertManager | None = None):
        if settings.mode == "live":
            raise PermissionError("the fast engine is paper-only; refuse to construct with mode=live")
        self._last_feed_latency_ms = 0.0
        self.limiter = limiter
        if limiter is not None and not isinstance(feed, RateLimitedFeed):
            feed = RateLimitedFeed(feed, limiter)
        super().__init__(settings, store, feed, clock)
        self.fc, self.fr = settings.fast, settings.fast_risk
        self.ledger = ledger
        data_dir = Path(settings.data_dir)
        self.shared_kill = kill
        self.kill = FastHalt(data_dir / "FAST_HALT", shared=kill, flatten_on_trip=self.fc.flatten_on_kill)
        self.events = events or JsonEventLog(data_dir / "fast_events.jsonl", clock)
        self.alerts = alerts or AlertManager(self.events, ledger, clock)
        self.metrics = LatencyMetrics()
        self.gateway = PreTradeGateway(settings.fast_risk)
        self.risk = RiskEngine(settings.risk, self.kill, clock=clock)     # auto-trips write FAST_HALT, never data/KILL
        self.risk.on_daily_halt(lambda reason: self._flatten_all(f"daily halt: {reason}"))
        self.provider = FastProvider(getattr(feed, "jup", None))
        self.broker = broker or FastPaperBroker(self.risk, self.provider, self.feed, self._resolve, self._sol_usd, clock,
                                                slippage_bps=settings.execution.live_slippage_bps,
                                                max_quote_age_sec=self.fc.max_quote_age_sec)
        self.book = PortfolioBook(ledger, self.fc.initial_capital_usd, clock)
        self.px: dict[str, _Px] = {}
        self.ref: dict[str, tuple[float, float, float]] = {}   # symbol -> (reference price, age when fetched, poll time)
        self._last_prices: dict[str, float] = {}
        self.last_price_ok_ts = clock()
        self._flattened_after_kill = False
        self._startup_recovery()

    # ------------------------------------------------------------------ helpers
    def _resolve(self, sym: str) -> tuple[str, int]:
        if sym not in self.symbols or sym not in self.decimals:
            raise ExecutionError(f"unknown symbol {sym}")
        return self.symbols[sym], self.decimals[sym]

    def _sol_usd(self) -> float | None:
        p = self.px.get("SOL")
        return p.price if p else None

    def _price_age(self, sym: str, now: float) -> float | None:
        """Seconds since the venue price (or its blockId) last changed, if it was received this poll; else None."""
        p = self.px.get(sym)
        if p is None or now - p.recv_ts > self.sc.poll_sec * 1.5:
            return None
        return now - p.changed_ts

    def _ws(self):
        return getattr(self.feed, "ws", None) or getattr(getattr(self.feed, "inner", None), "ws", None)

    def _balances(self) -> dict:
        return {"cash": round(self.book.cash, 6), "equity": round(self.book.equity(self._last_prices), 6),
                "positions": {s: round(p.qty, 10) for s, p in self.book.positions.items()}}

    def _check_books(self, where: str) -> list[str]:
        problems = verify_balances(self.ledger, self.book)
        if problems:
            self.alerts.alert("balance_mismatch", f"{where}: " + "; ".join(problems), "critical")
            if self.fc.halt_on_balance_mismatch:
                self.kill.trip(f"ledger self-check failed {where}: {problems[0]}", source="recovery")
        return problems

    # ------------------------------------------------------------------ startup recovery
    def _startup_recovery(self) -> None:
        counts = reconcile_intents(self.ledger, self.book, self.broker, self.clock, self._on_unknown, self.fc.unknown_intent_alert_sec)
        problems = self._check_books("at startup")
        self.events.emit("startup", ledger=str(self.ledger.path), reconciled=counts, balance_check="ok" if not problems else problems,
                         halted=self.kill.is_tripped(), **self._balances())
        log.info("fast engine: paper ledger %s, cash $%.2f, %d open position(s), reconciled %s",
                 self.ledger.path, self.book.cash, len(self.book.positions), counts)

    def _on_unknown(self, info: dict) -> None:
        self.alerts.alert(f"unknown_intent:{info['intent']}", f"order intent unresolved for {info['age_sec']}s", "critical", **info)

    # ------------------------------------------------------------------ one poll
    def poll(self) -> dict:
        tp = time.perf_counter()
        now = self.clock()
        self.polls += 1
        out: dict = {"ok": True, "opened": 0, "rejected": 0}
        if self.ledger.pending_intents():
            reconcile_intents(self.ledger, self.book, self.broker, self.clock, self._on_unknown, self.fc.unknown_intent_alert_sec)
        if now - self.last_facts_ts >= self.sc.facts_interval_sec:
            self.refresh_facts()
        if self.sc.discover_category and now - self.last_discover_ts >= self.sc.discover_interval_sec:
            self.discover_universe()

        # SCAN: venue prices (fail closed: no data -> no decisions on it)
        pres = self.feed.prices(list(self.symbols.values()))
        self._record("jupiter_price_v3", pres)
        fresh_syms: list[str] = []
        if pres.ok and isinstance(pres.data, dict):
            self.last_price_ok_ts = now
            self.alerts.resolve("data_outage")
            for sym, mint in list(self.symbols.items()):
                p = pres.data.get(mint)
                if not p or not p.get("usdPrice"):
                    continue
                price, block = float(p["usdPrice"]), p.get("blockId")
                old = self.px.get(sym)
                changed = now if (old is None or block != old.block or price != old.price) else old.changed_ts
                self.px[sym] = _Px(price, block, now, changed)
                self._last_prices[sym] = price
                self.on_tick(sym, price, now)
                self.store.log_tick(now, sym, price, block, None, None, pres.latency_ms)
                self.bars.add(sym, now, price)
                fresh_syms.append(sym)
        else:
            out.update(ok=False, reason=f"price feed status {pres.status}")
            outage = now - self.last_price_ok_ts
            if outage >= self.fc.data_outage_alert_sec:
                self.alerts.alert("data_outage", f"no venue prices for {outage:.0f}s (last status {pres.status})",
                                  "critical" if self.book.positions else "warning", open_positions=len(self.book.positions))
        # reference prices (Binance WebSocket when fresh, else REST)
        pairs = {s: self.s.universe.candle_symbols[s] for s in self.symbols if s in self.s.universe.candle_symbols}
        if pairs:
            rres = self.feed.reference_prices(list(pairs.values()))
            self._record("binance_ticker_price", rres)
            if rres.ok and isinstance(rres.data, dict):
                ws = self._ws()
                for sym, pair in pairs.items():
                    if pair in rres.data:
                        age = max(0.0, time.time() - ws.last_ts[pair]) if ws is not None and pair in ws.last_ts else 0.0
                        self.ref[sym] = (float(rres.data[pair]), age, now)

        # HALT: obey FAST_HALT and the shared KILL; flatten until flat, open nothing
        if self.kill.is_tripped():
            if self.fc.flatten_on_kill and not self._flattened_after_kill and self.book.positions:
                self._flatten_all(f"halt: {self.kill.reason()}")
            self._flattened_after_kill = not self.book.positions or not self.fc.flatten_on_kill
            out["halted"] = self.kill.reason()
        else:
            self._flattened_after_kill = False
            for sym in fresh_syms:
                ref = self.ref.get(sym)
                ref_px = ref[0] if ref and ref[2] == now else None
                for cand in evaluate_all(sym, self.bars.bars(sym), self.px[sym].price, ref_px):
                    key = (sym, cand.signal)
                    if self.cooldown.get(key, 0) > now:
                        continue
                    self.cooldown[key] = now + cand.horizon_sec
                    status = self._handle_candidate(cand, now)
                    out["opened" if status == "open" else "rejected"] += 1

        self._manage(now)
        super()._settle(now)                        # shadow-mark rejected candidates ("missed" accounting)
        if self._last_prices:
            pt = self.book.snapshot(self._last_prices)
            try:
                self.risk.on_equity(self.book.risk_state(self._last_prices))
            except Exception as e:  # noqa: BLE001
                self.alerts.alert("risk_monitor_error", str(e), "critical")
            out["equity"] = round(pt.equity_usd, 2)
        out["positions"] = len(self.book.positions)
        self.ledger.set("last_prices", dict(self._last_prices))
        self.metrics.record("poll_ms", (time.perf_counter() - tp) * 1000)
        if self.polls % 12 == 0 or out["opened"]:
            self.ledger.set("latency_summary", self.metrics.summary())
            if self.limiter is not None:
                self.ledger.set("rate_limits", self.limiter.stats())
            ws = self._ws()
            if ws is not None:
                self.ledger.set("websocket", ws.stats())
            self.events.emit("poll", polls=self.polls, **{**out, **self._balances()})
        self.store.set("last_poll", {"ts": now, "polls": self.polls, "universe": len(self.symbols), "req_last_min": self._requests_last_minute()})
        return out

    # ------------------------------------------------------------------ entries
    def _handle_candidate(self, cand: Candidate, now: float) -> str:
        """DETECT -> COST -> GATEWAY -> RISK -> REASSESS -> BUY. Returns 'open' or 'rejected'."""
        t0 = time.perf_counter()
        sym = cand.symbol
        facts = self.facts.get(sym)
        detail = {**cand.detail, "venue_price": self.px[sym].price, "block_id": self.px[sym].block}
        reasons = token_reasons(facts, self.limits) if facts else ["no_token_facts"]
        price_age = self._price_age(sym, now)
        if price_age is None or price_age > self.fr.max_price_age_sec:
            reasons.append(f"stale_price({price_age if price_age is None else round(price_age, 1)}s)")
        if sym in self.book.positions:
            reasons.append("position_open")
        if self.ledger.pending_intents(sym):
            reasons.append("pending_intent")
        if reasons:
            return self._reject(cand, detail, reasons, None, now)

        size = min(self.fc.size_usd, self.fr.max_position_usd)
        q = self.quotes_for(sym, size)                          # two rate-limited /order quotes
        t_quote = time.perf_counter()
        self.metrics.record("quote_ms", (t_quote - t0) * 1000)
        if q is None:
            return self._reject(cand, detail, ["quote_unavailable"], None, now)
        qb, qs, _ = q
        try:
            est = estimate_entry(sym, qb, qs, size, self.decimals[sym], self._sol_usd(), self.sc.adverse_buffer_bps, now)
        except (ValueError, ZeroDivisionError) as e:
            return self._reject(cand, detail, [f"cost_estimate_failed:{e}"], None, now)
        edge = cand.expected_move_bps - est.total_worst_bps
        if edge < self.sc.min_net_edge_bps:
            return self._reject(cand, detail, [f"net_edge<{self.sc.min_net_edge_bps:g}bps"], est.row(), now, edge)

        # GATEWAY (fail closed)
        try:
            st = build_state(self.ledger, self.book, self._last_prices, now)
        except Exception as e:  # noqa: BLE001
            self.alerts.alert("risk_service_unavailable", f"gateway state: {e}", "critical")
            return self._reject(cand, detail, [f"risk_service_unavailable:{type(e).__name__}"], est.row(), now, edge)
        ref = self.ref.get(sym)
        ref_age = (ref[1] + (now - ref[2])) if ref else None
        gd = self.gateway.check_entry(EntryRequest(sym, size, self.fc.stop_pct, est.total_worst_bps, price_age, est.quote_age_sec,
                                                   ref_age if cand.signal == "basis_reversion" else None), st)
        t_gate = time.perf_counter()
        self.metrics.record("gateway_ms", (t_gate - t_quote) * 1000)
        if not gd.approved:
            if any(r.startswith("risk_service_unavailable") for r in gd.reasons):
                self.alerts.alert("risk_service_unavailable", gd.reasons[0], "critical")
            return self._reject(cand, detail, [f"gateway:{r}" for r in gd.reasons], est.row(), now, edge)

        # RISK ENGINE (fail closed)
        px = self.px[sym].price
        entry_ref = size / (qb.min_out_amount / 10 ** self.decimals[sym])
        sig = Signal(symbol=sym, side=Side.BUY, strategy=f"fast:{cand.signal}", strength=1.0,
                     reason=f"{cand.signal} exp {cand.expected_move_bps:+.0f}bps cost {est.total_worst_bps:.0f}bps edge {edge:+.0f}bps",
                     price=px, stop_loss=entry_ref * (1 - self.fc.stop_pct / 100),
                     take_profit=entry_ref * (1 + max(cand.expected_move_bps * self.fc.take_profit_frac,
                                                      est.total_worst_bps + self.fc.min_take_profit_bps) / 1e4),
                     trailing_stop_pct=self.fc.trailing_pct or None, max_hold_sec=cand.horizon_sec * self.fc.max_hold_mult, ts=now,
                     meta={"expected_move_bps": cand.expected_move_bps, "edge_bps": edge, "cost": est.row()})
        order = OrderRequest(symbol=sym, side=Side.BUY, notional_usd=gd.max_notional_usd, strategy=sig.strategy, reason=sig.reason,
                             signal=sig, ts=now)
        try:
            mkt = MarketState(price=px, liquidity_usd=facts.liquidity_usd, volume_24h_usd=facts.volume_24h_usd, data_ts=now,
                              expected_slippage_bps=max(qb.price_impact_pct, qs.price_impact_pct) * 1e4)
            decision = self.risk.evaluate(order, self.book.risk_state(self._last_prices), mkt)
        except Exception as e:  # noqa: BLE001
            self.alerts.alert("risk_service_unavailable", f"risk engine: {e}", "critical")
            return self._reject(cand, detail, [f"risk_service_unavailable:{type(e).__name__}"], est.row(), now, edge)
        t_risk = time.perf_counter()
        self.metrics.record("risk_ms", (t_risk - t_gate) * 1000)
        self.ledger.log_decision("risk", {"order": order.id, "approved": decision.approved, "reasons": decision.reasons,
                                          "notional": decision.adjusted_notional_usd}, sym, sig.strategy, ts=now)
        if not decision:
            return self._reject(cand, detail, [f"risk:{r}" for r in decision.reasons], est.row(), now, edge)

        # REASSESS immediately before the order: fresh buy quote (cached for the fill) + fresh sell leg
        notional = decision.adjusted_notional_usd
        try:
            self.broker.quote(sym, Side.BUY, notional)
            jb = self.broker.last_jupiter_quote(sym, Side.BUY, notional)
            js = self.broker.fetch(sym, Side.SELL, jb.out_amount / 10 ** self.decimals[sym])
            est2 = estimate_entry(sym, jb, js, notional, self.decimals[sym], self._sol_usd(), self.sc.adverse_buffer_bps, self.clock())
        except (ExecutionError, ValueError, AttributeError) as e:
            return self._reject(cand, detail, [f"pre_exec_quote_unavailable:{e}"], est.row(), now, edge)
        t_requote = time.perf_counter()
        self.metrics.record("requote_ms", (t_requote - t_risk) * 1000)
        edge_now = cand.expected_move_bps - est2.total_worst_bps
        if edge_now < self.sc.min_net_edge_bps:
            return self._reject(cand, detail, [f"edge_vanished({edge_now:+.0f}bps at exec quote)"], est2.row(), now, edge_now)
        if est2.quote_age_sec > self.fr.max_quote_age_sec:
            return self._reject(cand, detail, [f"stale_quote({est2.quote_age_sec:.1f}s)"], est2.row(), now, edge_now)

        ok = self._execute_with_intent(order, decision.adjusted_qty or 0.0, decision, sig)
        t_fill = time.perf_counter()
        latency = {"detect_to_quote_ms": round((t_quote - t0) * 1000, 1), "gateway_ms": round((t_gate - t_quote) * 1000, 1),
                   "risk_ms": round((t_risk - t_gate) * 1000, 1), "requote_ms": round((t_requote - t_risk) * 1000, 1),
                   "order_to_fill_ms": round((t_fill - t_requote) * 1000, 1), "signal_to_order_ms": round((t_requote - t0) * 1000, 1),
                   "quote_rtt_ms": round(qb.latency_ms + qs.latency_ms, 1), "feed_latency_ms": round(self._last_feed_latency_ms, 1)}
        for k in ("signal_to_order_ms", "order_to_fill_ms"):
            self.metrics.record(k, latency[k])
        self.ledger.log_decision("latency", latency, sym, sig.strategy, ts=now)
        self.store.add_candidate(now, sym, cand.signal, cand.expected_move_bps, cand.horizon_sec, notional, detail, est2.row(), edge_now,
                                 "executed" if ok else "rejected", [] if ok else ["execution_failed"], entry_ref if ok else None, None)
        if ok:
            pos = self.book.positions.get(sym)
            self.events.emit("entry", symbol=sym, signal=cand.signal, order_id=order.id, notional_usd=round(notional, 4),
                             fill_price=pos.avg_cost if pos else None, stop=sig.stop_loss, take_profit=sig.take_profit,
                             expected_move_bps=cand.expected_move_bps, cost=est2.row(), edge_bps=round(edge_now, 1),
                             latency=latency, **self._balances())
            log.info("BUY  %s %s $%.2f @ %.6g  stop %.6g tp %.6g  cost %.0f bps  edge %+.0f bps", sym, cand.signal, notional,
                     pos.avg_cost if pos else float("nan"), sig.stop_loss, sig.take_profit, est2.total_worst_bps, edge_now)
        return "open" if ok else "rejected"

    def _reject(self, cand: Candidate, detail: dict, reasons: list[str], cost: dict | None, now: float, edge: float | None = None) -> str:
        self.store.add_candidate(now, cand.symbol, cand.signal, cand.expected_move_bps, cand.horizon_sec, self.fc.size_usd, detail,
                                 cost, edge, "rejected", reasons)
        self.ledger.log_decision("fast_reject", {"signal": cand.signal, "reasons": reasons, "edge_bps": edge}, cand.symbol,
                                 f"fast:{cand.signal}", ts=now)
        self.events.emit("candidate_rejected", symbol=cand.symbol, signal=cand.signal, reasons=reasons,
                         expected_move_bps=cand.expected_move_bps, edge_bps=None if edge is None else round(edge, 1),
                         cost_worst_bps=None if not cost else cost.get("total_worst_bps"))
        return "rejected"

    # ------------------------------------------------------------------ durable execution
    def _execute_with_intent(self, order: OrderRequest, qty: float, decision, signal: Signal | None = None, exit_reason: str = "") -> bool:
        if self.ledger.pending_intents(order.symbol):
            self.ledger.record_order(order, "rejected", "pending intent for symbol")
            return False
        try:
            iid = self.ledger.create_intent(order, qty, decision.adjusted_notional_usd,
                                            quote={"approval": decision.approval.token[:8]}, ts=self.clock())
        except DuplicateOrderError as e:
            self.alerts.alert("duplicate_order", str(e), "critical", order_id=order.id)
            return False
        self.broker.presubmit_hook = lambda info: self.ledger.update_intent(iid, state="signed", **info)
        try:
            fill = self.broker.execute(order, qty, decision.approval)
        except SubmittedUnknownError as e:
            self.ledger.update_intent(iid, state="submitted", tx_signature=e.tx_signature, request_id=e.request_id, result=str(e))
            self.ledger.record_order(order, "unknown", str(e))
            self.risk.on_execution_error(str(e))
            self.events.emit("order_unknown", level="warning", order_id=order.id, intent=iid, symbol=order.symbol, side=order.side.value,
                             error=str(e))
            return False
        except Exception as e:  # noqa: BLE001
            self.ledger.update_intent(iid, state="failed", result=str(e))
            self.ledger.record_order(order, "failed", str(e))
            self.risk.on_execution_error(str(e))
            self.events.emit("order_failed", level="warning", order_id=order.id, intent=iid, symbol=order.symbol, side=order.side.value,
                             error=str(e))
            log.error("%s failed for %s: %s", order.side.value, order.symbol, e)
            return False
        finally:
            self.broker.presubmit_hook = None
        self.risk.on_execution_success()
        try:                                     # fill + position + cash + trade + intent: one transaction
            with self.ledger.atomic():
                self.book.apply_fill(fill, signal=signal, exit_reason=exit_reason)
                self.ledger.record_order(order, "filled")
                self.ledger.update_intent(iid, state="confirmed", tx_signature=fill.tx_signature,
                                          result={"fill_id": fill.id, "qty": fill.qty, "price": fill.price, "notional_usd": fill.notional_usd})
        except Exception as e:  # noqa: BLE001
            self.book.reload()
            self.alerts.alert("booking_failed", f"{order.symbol} {order.side.value}: {e}", "critical", intent=iid)
            self.ledger.log_event("booking_failed", f"{iid} {order.symbol} {order.side.value}: {e}", ts=self.clock())
            return False
        self.events.emit("fill", order_id=order.id, intent=iid, symbol=fill.symbol, side=fill.side.value, qty=fill.qty, price=fill.price,
                         notional_usd=fill.notional_usd, fee_usd=fill.fee_usd, **self._balances())
        self._check_books(f"after {fill.side.value} {fill.symbol}")
        return True

    # ------------------------------------------------------------------ MANAGE / SELL
    def on_tick(self, sym: str, price: float, now: float) -> None:
        facts = self.facts.get(sym)
        self.provider.prices[sym] = Ticker(symbol=sym, price=price, ts=now, volume_24h_usd=facts.volume_24h_usd if facts else 0.0,
                                           liquidity_usd=facts.liquidity_usd if facts else 0.0)

    def _manage(self, now: float) -> None:
        for sym, pos in list(self.book.positions.items()):
            age = self._price_age(sym, now)
            if age is None:                       # no fresh price: never decide on stale data, but say so
                p = self.px.get(sym)
                stale_for = now - p.recv_ts if p else float("inf")
                if stale_for >= self.fc.data_outage_alert_sec:
                    self.alerts.alert(f"position_unpriced:{sym}", f"{sym} position has no fresh price for {stale_for:.0f}s", "critical")
                continue
            px = self.px[sym].price
            pos.high_water = max(pos.high_water or pos.avg_cost, px)
            reason = None
            if pos.stop_loss and px <= pos.stop_loss:
                reason = "stop_loss"
            elif pos.take_profit and px >= pos.take_profit:
                reason = "take_profit"
            elif pos.trailing_stop_pct and px <= pos.high_water * (1 - pos.trailing_stop_pct / 100) and px > pos.avg_cost:
                reason = "trailing_stop"
            elif pos.max_hold_sec and now - pos.opened_ts >= pos.max_hold_sec:
                reason = "max_hold"
            if reason:
                self._close(sym, reason, px, now)

    def _close(self, sym: str, reason: str, px: float, now: float) -> bool:
        pos = self.book.positions.get(sym)
        if pos is None:
            return False
        t0 = time.perf_counter()
        try:
            self.broker.quote(sym, Side.SELL, pos.qty)
            jq = self.broker.last_jupiter_quote(sym, Side.SELL, pos.qty)
            est = estimate_exit(sym, jq, pos.qty, px, self._sol_usd(), self.clock())
        except (ExecutionError, ValueError, AttributeError) as e:
            self.alerts.alert(f"exit_quote_unavailable:{sym}", f"cannot quote {reason} exit for {sym}: {e}; retrying", "critical")
            return False
        try:
            st = build_state(self.ledger, self.book, self._last_prices, now)
            gd = self.gateway.check_exit(sym, pos.qty, st)
        except Exception as e:  # noqa: BLE001 - exits are risk-reducing: a gateway fault must not trap a position
            self.alerts.alert("risk_service_unavailable", f"gateway exit check: {e}", "critical")
            gd = None
        if gd is not None and not gd.approved:
            self.alerts.alert(f"exit_blocked:{sym}", "; ".join(gd.reasons), "critical")
            return False
        order = OrderRequest(symbol=sym, side=Side.SELL, notional_usd=pos.qty * px, qty=pos.qty, strategy=pos.strategy,
                             reason=reason, reduce_only=True, ts=now)
        mkt = MarketState(price=px, liquidity_usd=1e12, volume_24h_usd=1e12, data_ts=now)
        decision = self.risk.evaluate(order, self.book.risk_state(self._last_prices), mkt)
        if not decision:
            self.alerts.alert(f"exit_refused:{sym}", "; ".join(decision.reasons), "critical")
            return False
        entry_ts = pos.opened_ts
        ok = self._execute_with_intent(order, decision.adjusted_qty or pos.qty, decision, None, exit_reason=reason)
        ms = (time.perf_counter() - t0) * 1000
        self.metrics.record("exit_ms", ms)
        if ok:
            tr = self.ledger.trades()[-1]
            self.ledger.log_decision("latency", {"exit_order_to_fill_ms": round(ms, 1), "reason": reason}, sym, tr.strategy, ts=now)
            self.events.emit("exit", symbol=sym, reason=reason, order_id=order.id, exit_price=tr.exit_price, pnl_usd=round(tr.pnl_usd, 6),
                             return_pct=round(tr.return_pct, 4), fees_usd=round(tr.fees_usd, 6), held_sec=round(now - entry_ts, 1),
                             exit_cost=est.row(), **self._balances())
            log.info("SELL %s %s @ %.6g  net %+.4f USD (%+.2f%%) fees %.4f  exit cost %.0f bps", sym, reason, tr.exit_price, tr.pnl_usd,
                     tr.return_pct, tr.fees_usd, est.total_worst_bps)
        return ok

    def _flatten_all(self, reason: str) -> None:
        now = self.clock()
        for sym in list(self.book.positions):
            p = self.px.get(sym)
            if p is None:
                self.alerts.alert(f"position_unpriced:{sym}", f"cannot flatten {sym}: no price", "critical")
                continue
            self._close(sym, f"flatten: {reason}"[:60], p.price, now)
        self.ledger.log_event("flatten", reason, ts=now)

    # ------------------------------------------------------------------ feed bookkeeping
    def _record(self, name: str, res) -> None:
        super()._record(name, res)
        if name == "jupiter_price_v3":
            self._last_feed_latency_ms = res.latency_ms
            self.metrics.record("price_feed_ms", res.latency_ms)

    # ------------------------------------------------------------------ run
    def run(self, minutes: float | None = None, sleep: Callable[[float], None] = time.sleep, max_polls: int | None = None) -> None:
        start = self.clock()
        self.ledger.log_event("start", f"fast engine paper; {len(self.symbols)} symbols; poll {self.sc.poll_sec}s; size ${self.fc.size_usd}", ts=start)
        self.events.emit("run_start", symbols=list(self.symbols), poll_sec=self.sc.poll_sec, size_usd=self.fc.size_usd,
                         gateway=self.fr.model_dump())
        log.info("fast engine start: %d symbols, poll %.0fs, size $%.0f, min edge %.0f bps, stop %.2f%%, PAPER", len(self.symbols),
                 self.sc.poll_sec, self.fc.size_usd, self.sc.min_net_edge_bps, self.fc.stop_pct)
        while True:
            t0 = self.clock()
            try:
                out = self.poll()
                if self.polls % 60 == 0:
                    log.info("poll %d: %s", self.polls, out)
            except Exception as e:  # noqa: BLE001 - the loop survives; a poll crash is an alert, not a kill
                log.exception("poll failed")
                self.alerts.alert("poll_exception", f"{type(e).__name__}: {e}", "critical")
            if max_polls is not None and self.polls >= max_polls:
                break
            if minutes is not None and self.clock() - start >= minutes * 60:
                break
            sleep(max(0.0, self.sc.poll_sec - (self.clock() - t0)))
        self.ledger.set("latency_summary", self.metrics.summary())
        if self.limiter is not None:
            self.ledger.set("rate_limits", self.limiter.stats())
        self.ledger.log_event("stop", f"after {self.polls} polls", ts=self.clock())
        self.events.emit("run_stop", polls=self.polls, **self._balances())


# ---------------------------------------------------------------------- report
def fast_report(ledger: Ledger, store: ScannerStore, since_hours: float = 24.0, min_edge_bps: float = 20.0) -> str:
    import json
    from ..portfolio.reporting import performance_report
    from ..scanner.scanner import scan_report
    from .recovery import expected_balances
    prices = ledger.get("last_prices", {}) or {}
    rep = performance_report(ledger, prices, since_ts=0.0)
    trades = ledger.trades()
    L = ["=== fast engine (PAPER) ==="]
    L.append(f"equity ${rep.equity:.2f}  cash ${rep.cash:.2f}  net profit ${rep.net_profit:+.2f} ({rep.net_return_pct:+.2f}%)  fees ${rep.fees:.2f}  "
             f"max DD {rep.max_drawdown_pct:.2f}%  trades {rep.trades}  win rate {rep.win_rate:.1f}%  PF {rep.profit_factor:.2f}")
    exp_cash, exp_pos = expected_balances(ledger, ledger.get("initial_capital", 0.0) or 0.0)
    stored_pos = {s: p.qty for s, p in ledger.load_positions().items()}
    agree = abs(exp_cash - float(ledger.get("cash", exp_cash))) < 1e-6 and all(
        abs(exp_pos.get(s, 0) - stored_pos.get(s, 0)) < 1e-9 for s in set(exp_pos) | set(stored_pos))
    L.append(f"ledger self-check: {'OK' if agree else 'MISMATCH'} (cash from fills ${exp_cash:.4f}; positions from fills {exp_pos or 'none'})")
    span = ledger._exec("SELECT MIN(ts) a, MAX(ts) b FROM (SELECT ts FROM events WHERE mode=? UNION ALL SELECT ts FROM decisions)", (ledger.mode,)).fetchone()
    first, end = span["a"], span["b"]
    if first and end:
        days = max((end - first) / 86400, 1 / 24)
        L.append(f"trading frequency: {len(trades) / days:.3f} closed trades/day over {days:.1f} days ({len(trades)} trades)")
    states = ledger._exec("SELECT state, COUNT(*) n FROM intents WHERE mode=? GROUP BY state", (ledger.mode,)).fetchall()
    L.append("order intents: " + (", ".join(f"{r['state']}={r['n']}" for r in states) or "none"))
    lat = ledger.get("latency_summary") or {}
    if lat:
        L.append("latency by stage: " + "; ".join(f"{k} p50 {v['p50_ms']} / p95 {v['p95_ms']} ms (n={v['n']})" for k, v in sorted(lat.items())))
    rl = ledger.get("rate_limits") or {}
    if rl:
        L.append("rate limits: " + "; ".join(f"{k} sent {v['sent']}, 429s {v['rate_limited_429']}, local throttles {v['throttled_local']}, "
                                             f"errors {v['errors']}" for k, v in sorted(rl.items())))
    ws = ledger.get("websocket") or {}
    if ws:
        L.append(f"websocket: connects {ws.get('connects')}, events {ws.get('events')}, stalls {ws.get('stalls')}, errors {ws.get('errors')}, "
                 f"latency p50 {ws.get('latency_p50_ms')} ms")
    alerts = [e for e in ledger.events(500) if e["kind"] == "alert"]
    L.append(f"alerts: {len(alerts)}" + "".join(f"\n  {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(a['ts']))} {a['message']}" for a in alerts[:10]))
    rej: dict[str, int] = {}
    for d in ledger.decisions(kind="fast_reject", limit=100000):
        for r in json.loads(d["payload"]).get("reasons", []):
            k = r.split("(")[0]
            rej[k] = rej.get(k, 0) + 1
    if rej:
        L.append("rejections: " + ", ".join(f"{k}={v}" for k, v in sorted(rej.items(), key=lambda kv: -kv[1])[:14]))
    L.append("-- last trades (UTC) --")
    for t in trades[-15:]:
        L.append(f"  BUY  {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(t.entry_ts))}  {t.symbol:<6} {t.qty:.6g} @ {t.entry_price:.6g}   "
                 f"SELL {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(t.exit_ts))} @ {t.exit_price:.6g}  {t.exit_reason:<14} net {t.pnl_usd:+.4f} USD "
                 f"({t.return_pct:+.2f}%)  fees {t.fees_usd:.4f}  [{t.strategy}]")
    if not trades:
        L.append("  (no closed trades)")
    L.append("")
    L.append(scan_report(store, since_hours=since_hours or 1e6, min_edge_bps=min_edge_bps))
    return "\n".join(L)
