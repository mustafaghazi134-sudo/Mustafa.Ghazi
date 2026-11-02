"""The fast engine loop. PAPER ONLY by construction: the broker is always PaperBroker.

Per poll (default 5 s):
  1. SCAN     scanner feeds: Jupiter Price v3 (+ blockId), Binance reference, slot, token facts   (OpportunityScanner.poll)
  2. DETECT   1-minute bars -> deterministic candidates                                          (scanner.signals)
  3. COST     two fresh size-specific read-only quotes -> worst-case round trip -> net edge        (scanner.costs)
  4. RISK     independent RiskEngine (limits, kill switch, daily halt, exposure, streaks, data age)
  5. BUY      re-quote immediately before the order; reject if the edge vanished; PaperBroker fill
  6. MANAGE   every poll: stop-loss, take-profit, max hold, trailing; kill switch flattens
  7. SELL     reduce-only exit through the same risk engine and broker
  8. RECORD   fills, trades (net of fees), equity, latency per stage, rejections -> data/fast.db
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from ..config import Settings
from ..execution.base import ExecutionError, SubmittedUnknownError
from ..execution.paper import PaperBroker
from ..models import Candle, OrderRequest, Side, Signal, Ticker, TokenInfo
from ..data.base import MarketDataProvider
from ..portfolio.accounting import PortfolioBook
from ..portfolio.ledger import Ledger
from ..risk.engine import MarketState, RiskEngine
from ..risk.kill_switch import KillSwitch
from ..scanner.costs import net_edge_bps, round_trip_cost
from ..scanner.filters import cost_reasons, staleness_reasons, token_reasons
from ..scanner.scanner import Feed, OpportunityScanner
from ..scanner.signals import Candidate
from ..scanner.store import ScannerStore

log = logging.getLogger("tradebot.fast")


class FastProvider(MarketDataProvider):
    """Adapter so PaperBroker (which quotes off provider.get_tickers) sees the scanner's latest prices and facts.
    When the scanner feed has a Jupiter key, PaperBroker fills from a fresh size-specific Jupiter quote; otherwise
    (replay / no key) it uses the parametric model (fee + slippage)."""

    def __init__(self, jup=None):
        self.prices: dict[str, Ticker] = {}
        self.jup = jup

    def get_tickers(self, symbols: list[str]) -> dict[str, Ticker]:
        return {s: self.prices[s] for s in symbols if s in self.prices}

    def get_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        return []

    def get_universe(self, max_symbols: int) -> list[TokenInfo]:
        return []


class FastEngine(OpportunityScanner):
    def __init__(self, settings: Settings, store: ScannerStore, feed: Feed, ledger: Ledger, kill: KillSwitch,
                 clock: Callable[[], float] = time.time, broker: PaperBroker | None = None):
        if settings.mode == "live":
            raise PermissionError("the fast engine is paper-only; refuse to construct with mode=live")
        self._last_feed_latency_ms = 0.0
        super().__init__(settings, store, feed, clock)
        self.fc = settings.fast
        self.ledger = ledger
        self.kill = kill
        self.risk = RiskEngine(settings.risk, kill, clock=clock)
        self.risk.on_daily_halt(lambda reason: self._flatten_all(f"daily halt: {reason}"))
        self.provider = FastProvider(getattr(feed, "jup", None))
        self.broker = broker or PaperBroker(self.risk, self.provider, fee_bps=settings.execution.paper_fee_bps,
                                            slippage_bps=settings.execution.paper_slippage_bps, clock=clock,
                                            quote_timeout_sec=settings.data.jupiter_quote_timeout_sec,
                                            live_slippage_bps=settings.execution.live_slippage_bps)
        self.book = PortfolioBook(ledger, settings.fast.initial_capital_usd, clock)
        self._flattened_after_kill = False
        self._last_prices: dict[str, float] = {}
        self._reconcile_startup()
        log.info("fast engine: paper ledger %s, capital $%.2f, kill file %s", ledger.path, self.book.cash, kill.path)

    # ------------------------------------------------------------------ startup
    def _reconcile_startup(self) -> None:
        """Paper fills never leave the process, so an intent left pending by a crash cannot have reached a chain."""
        for r in self.ledger.pending_intents():
            self.ledger.update_intent(r["id"], state="abandoned", result="paper intent found pending at startup (process died before fill)")
            self.ledger.log_event("intent_abandoned", f"{r['id']} {r['symbol']} {r['side']}", ts=self.clock())

    # ------------------------------------------------------------------ one poll
    def poll(self) -> dict:
        out = super().poll()             # SCAN + DETECT + COST + (via _handle_candidate) RISK/BUY, then _settle = MANAGE/SELL
        if not out.get("ok"):
            self.risk.on_execution_error(out.get("reason", "feed failure"))
        prices = self._last_prices
        if prices:
            pt = self.book.snapshot(prices)
            self.risk.on_equity(self.book.risk_state(prices))
            out["equity"] = round(pt.equity_usd, 2); out["positions"] = len(self.book.positions)
        if self.kill.is_tripped():
            if self.fc.flatten_on_kill and not self._flattened_after_kill and self.book.positions:
                self._flatten_all(f"kill switch: {self.kill.reason()}")
            self._flattened_after_kill = True
        else:
            self._flattened_after_kill = False
        return out

    def _handle_candidate(self, cand: Candidate, price: float, block: int | None, slot: int | None, now: float) -> str:
        """DETECT -> COST -> RISK -> BUY. Returns 'open' when a paper position was opened, otherwise 'rejected'."""
        t_detect = time.perf_counter()
        sym = cand.symbol
        facts = self.facts.get(sym)
        self.provider.prices[sym] = Ticker(symbol=sym, price=price, ts=now,
                                           volume_24h_usd=facts.volume_24h_usd if facts else 0.0,
                                           liquidity_usd=facts.liquidity_usd if facts else 0.0)
        self._last_prices[sym] = price
        reasons = token_reasons(facts, self.limits) if facts else ["no_token_facts"]
        slots_behind = (slot - block) if (slot is not None and block is not None) else None
        reasons += staleness_reasons(None, slots_behind, self.limits)
        if sym in self.book.positions:
            reasons.append("position_open")
        if self.kill.is_tripped():
            reasons.append("kill_switch")
        if self.ledger.pending_intents(sym):
            reasons.append("pending_intent")
        detail = {**cand.detail, "venue_price": price, "block_id": block, "slot": slot}
        if reasons:
            return self._reject(cand, detail, reasons, None, None, now)

        # COST: fresh size-specific quotes (both legs) -> worst-case round trip -> net edge
        size = min(self.fc.size_usd, self.book.equity(self._last_prices) * self.s.risk.max_position_pct / 100)
        if size < self.s.risk.min_order_notional_usd:
            return self._reject(cand, detail, ["size_below_min_notional"], None, None, now)
        q = self.quotes_for(sym, size)
        t_quoted = time.perf_counter()
        if q is None:
            return self._reject(cand, detail, ["quote_unavailable"], None, None, now)
        qb, qs, quote_ms = q
        rtc = round_trip_cost(qb, qs, size, self.decimals[sym])
        cost = rtc.row(); cost["router"], cost["route"] = qb.router, qb.route
        edge = net_edge_bps(cand.expected_move_bps, rtc, self.sc.adverse_buffer_bps)
        reasons = cost_reasons(rtc.worst_case_bps, rtc.buy_impact_bps, rtc.sell_impact_bps, edge, self.limits)
        if reasons:
            return self._reject(cand, detail, reasons, cost, edge, now)

        # RISK: independent engine (limits, kill, daily halt, exposure, streaks, data age, slippage)
        entry_ref = rtc.buy_price_worst_usd
        sig = Signal(symbol=sym, side=Side.BUY, strategy=f"fast:{cand.signal}", strength=min(1.0, edge / max(self.limits.min_net_edge_bps, 1.0) / 2),
                     reason=f"{cand.signal} exp {cand.expected_move_bps:+.0f}bps cost {rtc.worst_case_bps:.0f}bps edge {edge:+.0f}bps",
                     price=price, stop_loss=entry_ref * (1 - self.fc.stop_pct / 100),
                     take_profit=entry_ref * (1 + max(cand.expected_move_bps * self.fc.take_profit_frac, rtc.worst_case_bps + self.fc.min_take_profit_bps) / 1e4),
                     trailing_stop_pct=self.fc.trailing_pct or None, max_hold_sec=cand.horizon_sec * self.fc.max_hold_mult, ts=now,
                     meta={"expected_move_bps": cand.expected_move_bps, "edge_bps": edge, "cost": cost})
        order = OrderRequest(symbol=sym, side=Side.BUY, notional_usd=size, strategy=sig.strategy, reason=sig.reason, signal=sig, ts=now)
        pf = self.book.risk_state(self._last_prices)
        mkt = MarketState(price=price, liquidity_usd=facts.liquidity_usd, volume_24h_usd=facts.volume_24h_usd, data_ts=now,
                          expected_slippage_bps=max(rtc.buy_impact_bps, rtc.sell_impact_bps))
        decision = self.risk.evaluate(order, pf, mkt)
        t_risk = time.perf_counter()
        self.ledger.log_decision("risk", {"order": order.id, "approved": decision.approved, "reasons": decision.reasons,
                                          "notional": decision.adjusted_notional_usd}, sym, sig.strategy, ts=now)
        if not decision:
            return self._reject(cand, detail, [f"risk:{r}" for r in decision.reasons], cost, edge, now)

        # BUY: re-quote immediately before the order; the edge must still be there at the price we would actually pay
        try:
            pre = self.broker.quote(sym, Side.BUY, decision.adjusted_notional_usd)
        except ExecutionError as e:
            return self._reject(cand, detail, [f"pre_exec_quote:{e}"], cost, edge, now)
        implied_cost_bps = (pre.price / price - 1) * 1e4 * 2 + 2 * 0.01 / decision.adjusted_notional_usd * 1e4  # both legs at this slippage + fees
        edge_now = round(cand.expected_move_bps - max(implied_cost_bps, rtc.worst_case_bps) - self.sc.adverse_buffer_bps, 1)
        if edge_now < self.limits.min_net_edge_bps:
            return self._reject(cand, detail, [f"edge_vanished({edge_now:+.0f}bps at exec quote)"], cost, edge, now)
        t_sent = time.perf_counter()
        ok = self._execute_with_intent(order, decision.adjusted_qty or 0.0, decision, sig)
        t_filled = time.perf_counter()
        latency = {"detect_to_quote_ms": round((t_quoted - t_detect) * 1000, 1), "quote_to_risk_ms": round((t_risk - t_quoted) * 1000, 1),
                   "risk_to_order_ms": round((t_sent - t_risk) * 1000, 1), "order_to_fill_ms": round((t_filled - t_sent) * 1000, 1),
                   "signal_to_order_ms": round((t_sent - t_detect) * 1000, 1), "quote_rtt_ms": round(quote_ms, 1),
                   "feed_latency_ms": round(self._last_feed_latency_ms, 1)}
        self.ledger.log_decision("latency", latency, sym, sig.strategy, ts=now)
        status = "open" if ok else "rejected"
        self.store.add_candidate(now, sym, cand.signal, cand.expected_move_bps, cand.horizon_sec, size, detail, cost, edge,
                                 "executed" if ok else "rejected", [] if ok else ["execution_failed"], entry_ref if ok else None, None)
        if ok:
            pos = self.book.positions.get(sym)
            log.info("BUY  %s %s $%.2f @ %.6g  stop %.6g tp %.6g  edge %+.0f bps  signal->order %.0f ms", sym, cand.signal,
                     decision.adjusted_notional_usd, pos.avg_cost if pos else float("nan"), sig.stop_loss, sig.take_profit, edge, latency["signal_to_order_ms"])
        return status

    def _reject(self, cand: Candidate, detail: dict, reasons: list[str], cost: dict | None, edge: float | None, now: float) -> str:
        self.store.add_candidate(now, cand.symbol, cand.signal, cand.expected_move_bps, cand.horizon_sec, self.fc.size_usd, detail, cost, edge, "rejected", reasons)
        self.ledger.log_decision("fast_reject", {"signal": cand.signal, "reasons": reasons, "edge_bps": edge}, cand.symbol, f"fast:{cand.signal}", ts=now)
        log.debug("REJECT %s %s: %s", cand.symbol, cand.signal, ",".join(reasons))
        return "rejected"

    # ------------------------------------------------------------------ execution with durable intent
    def _execute_with_intent(self, order: OrderRequest, qty: float, decision, signal: Signal | None = None, exit_reason: str = "") -> bool:
        if self.ledger.pending_intents(order.symbol):
            self.ledger.record_order(order, "rejected", "pending intent for symbol")
            return False
        iid = self.ledger.create_intent(order, qty, decision.adjusted_notional_usd, quote={"approval": decision.approval.token[:8]}, ts=self.clock())
        self.broker.presubmit_hook = lambda info: self.ledger.update_intent(iid, state="signed", **info)
        try:
            fill = self.broker.execute(order, qty, decision.approval)
        except SubmittedUnknownError as e:
            self.ledger.update_intent(iid, state="submitted", tx_signature=e.tx_signature, request_id=e.request_id, result=str(e))
            self.ledger.record_order(order, "unknown", str(e)); self.risk.on_execution_error(str(e)); return False
        except Exception as e:  # noqa: BLE001
            self.ledger.update_intent(iid, state="failed", result=str(e))
            self.ledger.record_order(order, "failed", str(e)); self.risk.on_execution_error(str(e))
            log.error("%s failed for %s: %s", order.side.value, order.symbol, e); return False
        finally:
            self.broker.presubmit_hook = None
        self.ledger.update_intent(iid, state="confirmed", tx_signature=fill.tx_signature,
                                  result={"fill_id": fill.id, "qty": fill.qty, "price": fill.price, "notional_usd": fill.notional_usd})
        self.risk.on_execution_success()
        self.ledger.record_order(order, "filled")
        self.book.apply_fill(fill, signal=signal, exit_reason=exit_reason)
        return True

    # ------------------------------------------------------------------ MANAGE / SELL
    def on_tick(self, sym: str, price: float, now: float) -> None:
        facts = self.facts.get(sym)
        self._last_prices[sym] = price
        self.provider.prices[sym] = Ticker(symbol=sym, price=price, ts=now, volume_24h_usd=facts.volume_24h_usd if facts else 0.0,
                                           liquidity_usd=facts.liquidity_usd if facts else 0.0)

    def _settle(self, now: float) -> None:
        """Replaces the scanner's hypothetical settlement: manage real paper positions every poll."""
        for sym, pos in list(self.book.positions.items()):
            px = self.bars.last_price(sym) or self._last_prices.get(sym)
            if not px:
                continue
            self._last_prices[sym] = px
            facts = self.facts.get(sym)
            self.provider.prices[sym] = Ticker(symbol=sym, price=px, ts=now, volume_24h_usd=facts.volume_24h_usd if facts else 0.0,
                                               liquidity_usd=facts.liquidity_usd if facts else 0.0)
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
        # rejected candidates still get their shadow mark at the horizon so 'missed' stays measurable
        # (executed candidates are stored with status 'executed', which the base settlement ignores)
        super()._settle(now)
        self.ledger.set("last_prices", dict(self._last_prices))

    def _close(self, sym: str, reason: str, px: float, now: float) -> None:
        pos = self.book.positions[sym]
        order = OrderRequest(symbol=sym, side=Side.SELL, notional_usd=pos.qty * px, qty=pos.qty, strategy=pos.strategy,
                             reason=reason, reduce_only=True, ts=now)
        mkt = MarketState(price=px, liquidity_usd=1e12, volume_24h_usd=1e12, data_ts=now)
        decision = self.risk.evaluate(order, self.book.risk_state(self._last_prices), mkt)
        if not decision:
            log.error("exit for %s refused by risk engine: %s", sym, decision.reasons); return
        t0 = time.perf_counter()
        ok = self._execute_with_intent(order, decision.adjusted_qty or pos.qty, decision, None, exit_reason=reason)
        if ok:
            tr = self.ledger.trades()[-1] if self.ledger.trades() else None
            self.ledger.log_decision("latency", {"exit_order_to_fill_ms": round((time.perf_counter() - t0) * 1000, 1), "reason": reason}, sym, pos.strategy, ts=now)
            if tr:
                log.info("SELL %s %s @ %.6g  net %+.4f USD (%+.2f%%) fees %.4f  held %.0fs", sym, reason, tr.exit_price, tr.pnl_usd,
                         tr.return_pct, tr.fees_usd, tr.exit_ts - tr.entry_ts)

    def _flatten_all(self, reason: str) -> None:
        now = self.clock()
        for sym in list(self.book.positions):
            px = self._last_prices.get(sym) or self.bars.last_price(sym)
            if px:
                self._close(sym, f"flatten: {reason}"[:60], px, now)
        self.ledger.log_event("flatten", reason, ts=now)

    # ------------------------------------------------------------------ feed latency bookkeeping
    def _record(self, name: str, res) -> None:
        super()._record(name, res)
        if name == "jupiter_price_v3":
            self._last_feed_latency_ms = res.latency_ms

    # ------------------------------------------------------------------ run
    def run(self, minutes: float | None = None, sleep: Callable[[float], None] = time.sleep, max_polls: int | None = None) -> None:
        start = self.clock()
        self.ledger.log_event("start", f"fast engine paper; {len(self.symbols)} symbols; poll {self.sc.poll_sec}s; size ${self.fc.size_usd}", ts=start)
        log.info("fast engine start: %d symbols, poll %.0fs, size $%.0f, min edge %.0f bps, stop %.2f%%, PAPER", len(self.symbols), self.sc.poll_sec,
                 self.fc.size_usd, self.limits.min_net_edge_bps, self.fc.stop_pct)
        while True:
            t0 = self.clock()
            try:
                out = self.poll()
                if self.polls % 60 == 0:
                    log.info("poll %d: %s", self.polls, out)
            except Exception:  # noqa: BLE001
                log.exception("poll failed")
                self.risk.on_execution_error("poll exception")
            if max_polls is not None and self.polls >= max_polls:
                break
            if minutes is not None and self.clock() - start >= minutes * 60:
                break
            sleep(max(0.0, self.sc.poll_sec - (self.clock() - t0)))
        self.ledger.log_event("stop", f"after {self.polls} polls", ts=self.clock())


# ---------------------------------------------------------------------- report
def fast_report(ledger: Ledger, store: ScannerStore, since_hours: float = 24.0, min_edge_bps: float = 20.0) -> str:
    import json
    from ..portfolio.reporting import performance_report
    from ..scanner.scanner import scan_report
    since = time.time() - since_hours * 3600 if since_hours else 0.0
    prices = ledger.get("last_prices", {}) or {}
    rep = performance_report(ledger, prices, since_ts=0.0)
    trades = ledger.trades()
    L = ["=== fast engine (PAPER) ==="]
    L.append(f"equity ${rep.equity:.2f}  cash ${rep.cash:.2f}  net profit ${rep.net_profit:+.2f} ({rep.net_return_pct:+.2f}%)  fees ${rep.fees:.2f}  "
             f"max DD {rep.max_drawdown_pct:.2f}%  trades {rep.trades}  win rate {rep.win_rate:.1f}%  PF {rep.profit_factor:.2f}")
    span = ledger._exec("SELECT MIN(ts) a, MAX(ts) b FROM (SELECT ts FROM events WHERE mode=? UNION ALL SELECT ts FROM decisions)", (ledger.mode,)).fetchone()
    first, end = span["a"], span["b"]                               # ledger clock (replay or live), never the wall clock
    if first and end:
        days = max((end - first) / 86400, 1 / 24)
        L.append(f"trading frequency: {len(trades) / days:.3f} closed trades/day over {days:.1f} days ({len(trades)} trades)")
    lat = [json.loads(d["payload"]) for d in ledger.decisions(kind="latency", limit=10000)]
    s2o = sorted(x["signal_to_order_ms"] for x in lat if "signal_to_order_ms" in x)
    o2f = sorted(x["order_to_fill_ms"] for x in lat if "order_to_fill_ms" in x)
    if s2o:
        L.append(f"latency signal->order: p50 {s2o[len(s2o)//2]:.0f} ms  p95 {s2o[int(len(s2o)*0.95)-1 if len(s2o)>1 else 0]:.0f} ms  max {s2o[-1]:.0f} ms   "
                 f"order->fill p50 {o2f[len(o2f)//2]:.0f} ms (paper)")
    rej: dict[str, int] = {}
    for d in ledger.decisions(kind="fast_reject", limit=100000):
        for r in json.loads(d["payload"]).get("reasons", []):
            k = r.split("(")[0]
            rej[k] = rej.get(k, 0) + 1
    if rej:
        L.append("rejections: " + ", ".join(f"{k}={v}" for k, v in sorted(rej.items(), key=lambda kv: -kv[1])[:12]))
    L.append("-- last trades (UTC) --")
    for t in trades[-15:]:
        L.append(f"  BUY  {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(t.entry_ts))}  {t.symbol:<6} {t.qty:.6g} @ {t.entry_price:.6g}   "
                 f"SELL {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(t.exit_ts))} @ {t.exit_price:.6g}  {t.exit_reason:<14} net {t.pnl_usd:+.4f} USD ({t.return_pct:+.2f}%)  fees {t.fees_usd:.4f}  [{t.strategy}]")
    if not trades:
        L.append("  (no closed trades)")
    L.append("")
    L.append(scan_report(store, since_hours=since_hours or 1e6, min_edge_bps=min_edge_bps))
    return "\n".join(L)
