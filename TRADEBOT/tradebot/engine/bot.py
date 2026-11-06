"""The trading engine: scan -> signals -> (Claude vet) -> risk -> execute -> manage -> record, 24/7.

Order of defence on every cycle: kill switch -> data freshness -> risk engine equity checks ->
position exits -> new entries. Nothing here can bypass RiskEngine approvals.
"""
from __future__ import annotations

import datetime as dt
import logging
import signal as os_signal
import time
from pathlib import Path
from typing import Any, Callable

from ..config import Settings, StrategyConfig
from ..data.base import MarketDataProvider
from ..data.csv_replay import CsvReplayProvider
from ..data.synthetic import SyntheticProvider
from ..execution.base import Broker, ExecutionError, SubmittedUnknownError
from ..execution.live_gate import LiveGate
from ..execution.paper import PaperBroker
from ..models import Fill, OrderRequest, Side, Signal, Ticker
from ..portfolio.accounting import PortfolioBook
from ..portfolio.ledger import Ledger
from ..portfolio.reporting import format_report, performance_report
from ..research.claude_analyst import ClaudeAnalyst
from ..research.optimizer import Candidate, optimize
from ..risk.engine import MarketState, RiskEngine
from ..risk.kill_switch import KillSwitch
from ..strategies.base import Strategy
from ..strategies.registry import build_strategy
from .position_manager import check_exit

log = logging.getLogger(__name__)


class TradingBot:
    def __init__(self, settings: Settings, provider: MarketDataProvider, ledger: Ledger, risk: RiskEngine,
                 kill: KillSwitch, broker: Broker, book: PortfolioBook, strategies: list[Strategy],
                 analyst: ClaudeAnalyst, clock: Callable[[], float]):
        self.s = settings
        self.provider = provider
        self.ledger = ledger
        self.risk = risk
        self.kill = kill
        self.broker = broker
        self.book = book
        self.strategies = strategies
        self.analyst = analyst
        self.clock = clock
        self.symbols: list[str] = list(settings.universe.symbols)
        self.removed_symbols: set[str] = set(ledger.get("removed_symbols", []))
        self._stop = False
        self._flattened_after_kill = False
        self.last_research_ts: float = ledger.get("last_research_ts", 0.0)
        self.last_equity_snapshot_ts: float = 0.0
        self.last_report_day: str | None = ledger.get("last_report_day")
        self.cycles = 0
        self._signals_this_cycle = 0
        self._apply_saved_overrides()
        kill.on_trip(lambda reason: ledger.log_event("kill_switch", reason, ts=self.clock()))
        risk.on_daily_halt(self._on_daily_halt)
        self.reconcile_pending()                     # settle anything a previous process left unresolved

    def _on_daily_halt(self, reason: str) -> None:
        self.ledger.log_event("daily_halt", reason, ts=self.clock())
        if self.s.engine.flatten_on_daily_halt and self.book.positions:
            self.flatten_all(f"daily halt: {reason}")

    # ------------------------------------------------------------------ lifecycle
    def stop(self) -> None:
        self._stop = True

    def run_forever(self, max_cycles: int | None = None) -> None:
        self.ledger.log_event("start", f"mode={self.s.mode} provider={type(self.provider).__name__} symbols={self.symbols}", ts=self.clock())
        log.info("tradebot started: mode=%s symbols=%s strategies=%s", self.s.mode, self.symbols, [st.describe() for st in self.strategies])
        try:
            os_signal.signal(os_signal.SIGTERM, lambda *_: self.stop())
            os_signal.signal(os_signal.SIGINT, lambda *_: self.stop())
        except (ValueError, OSError):
            pass  # not in main thread
        while not self._stop:
            try:
                self.cycle()
            except Exception as e:  # noqa: BLE001 - the loop must survive; errors are counted toward the kill threshold
                log.exception("cycle failed")
                self.ledger.log_event("error", f"cycle: {e}", ts=self.clock())
                self.risk.on_execution_error(str(e))
            self.cycles += 1
            if max_cycles is not None and self.cycles >= max_cycles:
                break
            if self.provider.is_replay:
                if getattr(self.provider, "exhausted", False):
                    log.info("replay exhausted after %d cycles", self.cycles)
                    break
                if self.kill.is_tripped() and self._flattened_after_kill:
                    log.warning("kill switch tripped during replay; stopping")
                    break
                self.provider.advance()
            else:
                time.sleep(self.s.engine.scan_interval_sec)
        self.ledger.log_event("stop", f"after {self.cycles} cycles", ts=self.clock())

    # ------------------------------------------------------------------ one iteration
    def cycle(self) -> None:
        now = self.clock()
        tickers = self.provider.get_tickers(self.symbols)
        if not tickers:
            self.risk.on_execution_error("no tickers returned")
            return
        data_ts = max(t.ts for t in tickers.values())
        self.risk.on_data(data_ts)
        prices = {s: t.price for s, t in tickers.items()}
        self._signals_this_cycle = 0
        log.info("cycle %d prices: %s | positions: %s", self.cycles + 1,
                 ", ".join(f"{s}={p:.6g}" for s, p in prices.items()),
                 ", ".join(f"{p.symbol}({p.qty:.4g})" for p in self.book.positions.values()) or "none")

        if now - self.last_equity_snapshot_ts >= self.s.engine.equity_snapshot_interval_sec or self.provider.is_replay:
            pt = self.book.snapshot(prices)
            self.last_equity_snapshot_ts = now
            self.risk.on_equity(self.book.risk_state(prices))
            log.debug("equity $%.2f cash $%.2f exposure $%.2f", pt.equity_usd, pt.cash_usd, pt.exposure_usd)

        if self.kill.is_tripped():
            if self.s.engine.flatten_on_kill and not self._flattened_after_kill and self.book.positions:
                self.flatten_all(f"kill switch: {self.kill.reason()}")
            # done only when the book is actually flat: a failed or unpriced exit is retried on the next cycle
            self._flattened_after_kill = not self.book.positions or not self.s.engine.flatten_on_kill
            if not self._flattened_after_kill:
                log.error("kill switch: %d position(s) still open after flatten attempt; retrying next cycle", len(self.book.positions))
            if self.cycles % 10 == 0:
                log.warning("HALTED by kill switch (%s). Run `tradebot resume` after review.", self.kill.reason())
            return
        self._flattened_after_kill = False

        if self.ledger.pending_intents():
            self.reconcile_pending()
        self._check_reference_basis(tickers)
        self._manage_positions(tickers)
        self._scan_entries(tickers)
        if not self._signals_this_cycle and not self.provider.is_replay:
            log.info("cycle %d: no entry signals from %s (expected most cycles for a daily-evaluated 4h trend system)",
                     self.cycles + 1, [st.name for st in self.strategies])
        self._maybe_daily_report(prices)
        if self.s.claude.research_enabled and (now - self.last_research_ts) >= self.s.claude.research_interval_hours * 3600:
            try:
                self.research_once()
            except Exception as e:  # noqa: BLE001
                log.exception("research failed")
                self.ledger.log_event("error", f"research: {e}", ts=now)
            self.last_research_ts = now
            self.ledger.set("last_research_ts", now)

    # ------------------------------------------------------------------ durable execution (crash protection)
    def _execute_with_intent(self, order: OrderRequest, qty: float, decision, signal: Signal | None = None,
                             exit_reason: str = "") -> bool:
        """Every order goes through here. The intent is persisted before signing, updated with requestId and
        signature before submission, and resolved afterwards. A crash at any point leaves a row that
        reconcile_pending() can settle on restart; a pending row blocks new orders for the symbol."""
        if self.ledger.pending_intents(order.symbol):
            self.ledger.record_order(order, "rejected", "pending intent for symbol awaiting reconciliation")
            log.warning("blocked %s %s: an earlier order for this symbol is still unresolved", order.side.value, order.symbol)
            return False
        intent_id = self.ledger.create_intent(order, qty, decision.adjusted_notional_usd, quote={"approval": decision.approval.token[:8]}, ts=self.clock())
        self.broker.presubmit_hook = lambda info: self.ledger.update_intent(intent_id, state="signed", **info)
        try:
            fill = self.broker.execute(order, qty, decision.approval)
        except SubmittedUnknownError as e:
            known = {k: v for k, v in (("tx_signature", e.tx_signature), ("request_id", e.request_id)) if v}   # never erase persisted evidence
            self.ledger.update_intent(intent_id, state="submitted", result=str(e), **known)
            self.ledger.record_order(order, "unknown", str(e))
            self.ledger.log_event("execution_unknown", f"{order.symbol} {order.side.value} sig={e.tx_signature}", ts=self.clock())
            self.risk.on_execution_error(str(e))
            log.error("outcome unknown for %s %s; will reconcile: %s", order.side.value, order.symbol, e)
            return False
        except Exception as e:  # noqa: BLE001 - includes PermissionError / ExecutionError / network errors before signing
            row = self.ledger.get_intent(intent_id)
            if row is not None and row["tx_signature"]:
                # A signature was persisted, so the transaction may have been broadcast: an exception now is NOT
                # evidence of failure. Leave it pending for chain-evidence reconciliation.
                self.ledger.update_intent(intent_id, state="submitted", result=f"outcome unknown after signing: {e}")
                self.ledger.record_order(order, "unknown", str(e))
                self.risk.on_execution_error(str(e))
                log.error("outcome unknown for %s %s after signing; will reconcile: %s", order.side.value, order.symbol, e)
                return False
            self.ledger.update_intent(intent_id, state="failed", result=str(e))
            self.ledger.record_order(order, "failed", str(e))
            self.risk.on_execution_error(str(e))
            log.error("%s failed for %s: %s", order.side.value, order.symbol, e)
            return False
        finally:
            self.broker.presubmit_hook = None
        self.risk.on_execution_success()
        if not self._book_fill(intent_id, order, fill, signal, exit_reason):
            return False
        return True

    def _book_fill(self, intent_id: str, order: OrderRequest, fill: Fill, signal: Signal | None, exit_reason: str) -> bool:
        """Fill, position, cash, trade, order status and intent=confirmed are written in ONE transaction. If anything
        fails (or the process dies) none of it is kept and the intent stays pending with its signature, so
        reconcile_pending books it exactly once from chain evidence. Memory is reloaded from disk on failure."""
        try:
            with self.ledger.atomic():
                self.book.apply_fill(fill, signal=signal, exit_reason=exit_reason)
                self.ledger.record_order(order, "filled")
                self.ledger.update_intent(intent_id, state="confirmed", tx_signature=fill.tx_signature,
                                          result={"fill_id": fill.id, "qty": fill.qty, "price": fill.price, "notional_usd": fill.notional_usd})
        except Exception as e:  # noqa: BLE001
            self.book.reload()
            log.critical("booking of %s %s failed (%s); intent %s stays pending and will be reconciled", order.side.value,
                         order.symbol, e, intent_id)
            self.ledger.log_event("booking_failed", f"{intent_id} {order.symbol} {order.side.value}: {e}", ts=self.clock())
            return False
        return True

    def reconcile_pending(self) -> dict[str, int]:
        """Settle intents left pending by a crash or timeout, on evidence only:
        confirmed on-chain -> booked exactly once from the persisted quote; failed on-chain -> closed;
        not found AND blockhash expired -> expired (it can never land); otherwise it stays UNKNOWN (pending),
        blocks new orders for that symbol, and is re-checked every cycle. Time alone never resolves an intent."""
        counts = {"confirmed": 0, "failed": 0, "expired": 0, "abandoned": 0, "pending": 0}
        for row in self.ledger.pending_intents():
            age = self.clock() - row["ts"]
            if row["state"] == "created" or not row["tx_signature"]:
                # Ask the venue whether anything was signed for this order but never persisted (separate signer).
                # Only a definite "nothing" abandons; an unreachable signer leaves the intent pending.
                try:
                    signed = self.broker.signature_for_order(row["order_id"])
                except Exception as e:  # noqa: BLE001
                    log.error("reconcile: signer lookup for %s failed: %s (stays pending)", row["id"], e)
                    counts["pending"] += 1
                    continue
                if not signed:
                    self.ledger.update_intent(row["id"], state="abandoned", result="never signed")   # nothing could have been broadcast
                    counts["abandoned"] += 1
                    continue
                self.ledger.update_intent(row["id"], state="signed", tx_signature=signed["signature"], signed_tx=signed.get("signed_tx"),
                                          result="signature recovered from the signer log")
                row = self.ledger.get_intent(row["id"])
            try:
                ev = self.broker.transaction_status(row["tx_signature"], row["signed_tx"])
            except Exception as e:  # noqa: BLE001
                log.error("reconcile: status lookup failed for %s: %s (stays UNKNOWN)", row["tx_signature"], e)
                counts["pending"] += 1
                continue
            status, valid = ev.get("status"), ev.get("blockhash_valid")
            if status == "confirmed" and self.ledger.fill_for_order(row["order_id"]) is not None:
                # already booked (e.g. the booking committed but the process died before anything else): never twice
                self.ledger.update_intent(row["id"], state="confirmed", result="already booked; confirmed by reconciliation")
                counts["confirmed"] += 1
            elif status == "confirmed":
                side = Side(row["side"])
                try:
                    fill = self.broker.chain_fill(row)      # live: actual amounts from the transaction's balance changes
                except Exception as e:  # noqa: BLE001
                    log.critical("reconcile: %s confirmed on chain but its fill could not be read (%s); stays pending", row["id"], e)
                    counts["pending"] += 1
                    continue
                if fill is None:                            # venues without a chain record: book from the persisted intent
                    qty, notional = float(row["qty"]), float(row["notional_usd"])
                    price = notional / qty if qty else 0.0
                    fill = Fill(order_id=row["order_id"], symbol=row["symbol"], side=side, qty=qty, price=price, notional_usd=notional,
                                fee_usd=0.0, ts=self.clock(), tx_signature=row["tx_signature"])
                try:
                    with self.ledger.atomic():
                        self.book.apply_fill(fill, exit_reason="reconciled" if side == Side.SELL else "")
                        self.ledger.update_intent(row["id"], state="confirmed", result={"reconciled_from_quote": True, "fill_id": fill.id})
                except Exception as e:  # noqa: BLE001
                    self.book.reload()
                    log.critical("reconcile: booking %s failed (%s); stays pending", row["id"], e)
                    counts["pending"] += 1
                    continue
                self.ledger.log_event("reconciled", f"{row['symbol']} {row['side']} booked from persisted intent sig={row['tx_signature']}", ts=self.clock())
                counts["confirmed"] += 1
            elif status == "failed":
                self.ledger.update_intent(row["id"], state="failed", result="on-chain failure")
                counts["failed"] += 1
            elif status == "not_found" and valid is False:
                self.ledger.update_intent(row["id"], state="expired", result=f"not found and blockhash expired after {age:.0f}s")
                counts["expired"] += 1
            else:
                self.ledger.update_intent(row["id"], result=f"UNKNOWN: status={status} blockhash_valid={valid} age={age:.0f}s")
                if age > self.s.execution.intent_stale_warn_sec:
                    log.critical("intent %s for %s UNKNOWN for %.0fs; trading in %s is blocked until it resolves "
                                 "(or `tradebot resolve-intent %s expired|confirmed` after manual verification)",
                                 row["id"], row["symbol"], age, row["symbol"], row["id"])
                counts["pending"] += 1
        if any(counts.values()):
            log.warning("reconciled pending intents: %s", counts)
        return counts

    def reconcile_balances(self, tolerance_pct: float | None = None) -> list[str]:
        """Live only: compare ledger positions and cash with on-chain balances. Any mismatch beyond tolerance
        trips the kill switch, because trading on a wrong book is how real money gets lost."""
        tol = (tolerance_pct if tolerance_pct is not None else self.s.execution.balance_tolerance_pct) / 100
        try:
            onchain = self.broker.get_balances()
        except Exception as e:  # noqa: BLE001
            msg = f"balance reconciliation failed: {e}"
            self.kill.trip(msg, source="reconcile")
            return [msg]
        problems = []
        for sym, pos in self.book.positions.items():
            have = onchain.get(sym, 0.0)
            if have < pos.qty * (1 - tol):
                problems.append(f"{sym}: ledger {pos.qty:.6g} but wallet holds {have:.6g}")
        usdc = onchain.get(self.s.universe.quote_symbol)
        if usdc is not None and usdc < self.book.cash * (1 - tol) - 1.0:
            problems.append(f"cash: ledger ${self.book.cash:.2f} but wallet holds ${usdc:.2f} {self.s.universe.quote_symbol}")
        for p in problems:
            self.ledger.log_event("balance_mismatch", p, ts=self.clock())
        if problems:
            self.kill.trip("ledger/wallet mismatch: " + "; ".join(problems), source="reconcile")
        else:
            self.ledger.log_event("reconciled_balances", f"ok: {len(self.book.positions)} positions, cash ${self.book.cash:.2f}", ts=self.clock())
        return problems

    # ------------------------------------------------------------------ exits
    def _manage_positions(self, tickers: dict[str, Ticker]) -> None:
        for sym, pos in list(self.book.positions.items()):
            t = tickers.get(sym)
            if t is None:
                continue
            strat = next((s for s in self.strategies if s.name == pos.strategy), None)
            candles = self.provider.get_candles(sym, self.s.data.candle_interval, self.s.data.candle_history) if strat else None
            reason = check_exit(pos, t.price, self.clock(), candles, strat)
            self.ledger.upsert_position(pos)  # persist high-water updates
            if reason:
                self._close_position(sym, reason, t)

    def _close_position(self, sym: str, reason: str, t: Ticker) -> None:
        pos = self.book.positions.get(sym)
        if pos is None:
            return
        order = OrderRequest(symbol=sym, side=Side.SELL, notional_usd=pos.qty * t.price, qty=pos.qty, strategy=pos.strategy,
                             reason=reason, reduce_only=True)
        order.ts = self.clock()
        mkt = MarketState(price=t.price, liquidity_usd=t.liquidity_usd, volume_24h_usd=t.volume_24h_usd, data_ts=t.ts)
        decision = self.risk.evaluate(order, self.book.risk_state({sym: t.price}), mkt)
        self.ledger.log_decision("risk", {"order": order.id, "approved": decision.approved, "reasons": decision.reasons}, sym, pos.strategy, ts=order.ts)
        if not decision.approved or decision.approval is None:
            self.ledger.record_order(order, "rejected", "; ".join(decision.reasons))
            return
        self._execute_with_intent(order, decision.adjusted_qty or pos.qty, decision, exit_reason=reason)

    def flatten_all(self, reason: str) -> None:
        log.warning("FLATTENING all positions: %s", reason)
        tickers = self.provider.get_tickers(list(self.book.positions))
        for sym in list(self.book.positions):
            t = tickers.get(sym)
            if t is None:
                log.error("cannot flatten %s: no price", sym)
                continue
            self._close_position(sym, f"flatten:{reason}", t)
        self.ledger.log_event("flatten", reason, ts=self.clock())

    # ------------------------------------------------------------------ entries
    def _check_reference_basis(self, tickers: dict[str, Ticker]) -> None:
        """Deterministic tracking check: Jupiter price (what we trade) vs Binance spot (what the signals are computed
        on). Symbols whose gap exceeds max_basis_bps are blocked from NEW entries this cycle; exits are never blocked.
        Replay/synthetic providers return no reference prices, so this is a no-op there."""
        self._basis_blocked = set()
        if not self.s.data.reference_price_check:
            return
        try:
            ref = self.provider.get_reference_prices(list(tickers))
        except Exception as e:  # noqa: BLE001
            log.warning("reference prices unavailable (%s); basis check skipped this cycle", e)
            self.ledger.log_event("basis_unavailable", str(e)[:200], ts=self.clock())
            return
        if not ref:
            return
        basis: dict[str, float] = {}
        for sym, t in tickers.items():
            r = ref.get(sym)
            if not r:
                continue
            bps = (t.price / r - 1) * 1e4
            basis[sym] = round(bps, 1)
            if abs(bps) > self.s.data.max_basis_bps:
                self._basis_blocked.add(sym)
                log.warning("%s: Jupiter %.6g vs Binance %.6g = %+.0f bps (> %.0f): entries blocked this cycle",
                            sym, t.price, r, bps, self.s.data.max_basis_bps)
                self.ledger.log_event("basis_block", f"{sym} {bps:+.0f} bps (jup {t.price:.6g} vs ref {r:.6g})", ts=self.clock())
        if basis:
            self.ledger.set("last_basis_bps", {"ts": self.clock(), **basis})
            log.info("basis vs Binance (bps): %s", ", ".join(f"{k}={v:+.0f}" for k, v in basis.items()))

    def _scan_entries(self, tickers: dict[str, Ticker]) -> None:
        prices = {s: t.price for s, t in tickers.items()}
        for sym in self.symbols:
            if sym in self.removed_symbols or sym in self.book.positions:
                continue
            if sym in getattr(self, "_basis_blocked", set()):
                self.ledger.log_decision("entry_blocked", {"reason": "basis vs reference price exceeds max_basis_bps"}, sym, "", ts=self.clock())
                continue
            t = tickers.get(sym)
            if t is None:
                continue
            try:
                candles = self.provider.get_candles(sym, self.s.data.candle_interval, self.s.data.candle_history)
            except Exception as e:  # noqa: BLE001
                log.warning("candles unavailable for %s: %s", sym, e)
                continue
            for strat in self.strategies:
                sig = strat.entry_signal(sym, candles)
                if sig is None:
                    continue
                sig.ts = self.clock()
                self._signals_this_cycle += 1
                self.ledger.log_decision("signal", {"side": sig.side.value, "strength": sig.strength, "reason": sig.reason,
                                                    "price": sig.price, "stop": sig.stop_loss, "tp": sig.take_profit}, sym, strat.name, ts=sig.ts)
                self._try_enter(sig, t, prices)
                break  # one strategy per symbol per cycle

    def _try_enter(self, sig: Signal, t: Ticker, prices: dict[str, float]) -> None:
        state = self.book.risk_state(prices)
        base = state.equity_usd * self.s.risk.max_position_pct / 100 * (0.5 + 0.5 * sig.strength)
        multiplier = 1.0
        if self.s.claude.vet_signals and self.analyst.available:
            ctx = {"signal": {"symbol": sig.symbol, "strategy": sig.strategy, "strength": sig.strength, "reason": sig.reason,
                              "price": sig.price, "stop_loss": sig.stop_loss, "take_profit": sig.take_profit},
                   "market": {"liquidity_usd": t.liquidity_usd, "volume_24h_usd": t.volume_24h_usd, "change_24h_pct": t.change_24h_pct},
                   "proposed_notional_usd": round(base, 2), "equity_usd": round(state.equity_usd, 2),
                   "open_positions": [{"symbol": p.symbol, "value_usd": round(p.market_value(prices.get(p.symbol, p.avg_cost)), 2)}
                                      for p in state.positions.values()],
                   "recent_strategy_stats": self._strategy_stats(sig.strategy)}
            vet = self.analyst.vet_signal(ctx)
            if vet is None:
                if not self.s.claude.fail_open:
                    self.ledger.log_decision("claude_vet", {"result": "unavailable", "action": "skip (fail_closed)"}, sig.symbol, sig.strategy, ts=self.clock())
                    return
            else:
                self.ledger.log_decision("claude_vet", vet.model_dump(), sig.symbol, sig.strategy, ts=self.clock())
                if not vet.approve or vet.size_multiplier <= 0:
                    log.info("Claude vetoed %s %s: %s", sig.strategy, sig.symbol, vet.reason)
                    return
                multiplier = min(1.0, vet.size_multiplier)
        order = OrderRequest(symbol=sig.symbol, side=Side.BUY, notional_usd=base * multiplier, strategy=sig.strategy,
                             reason=sig.reason, signal=sig)
        order.ts = self.clock()
        mkt = MarketState(price=t.price, liquidity_usd=t.liquidity_usd, volume_24h_usd=t.volume_24h_usd, data_ts=t.ts)
        decision = self.risk.evaluate(order, state, mkt)
        self.ledger.log_decision("risk", {"order": order.id, "approved": decision.approved, "reasons": decision.reasons,
                                          "requested": order.notional_usd, "approved_notional": decision.adjusted_notional_usd},
                                 sig.symbol, sig.strategy, ts=order.ts)
        if not decision.approved or decision.approval is None:
            self.ledger.record_order(order, "rejected", "; ".join(decision.reasons))
            log.info("risk rejected %s %s: %s", sig.strategy, sig.symbol, decision.reasons)
            return
        self._execute_with_intent(order, decision.adjusted_qty or 0.0, decision, signal=sig)

    def _strategy_stats(self, name: str) -> dict:
        since = self.clock() - 7 * 86400
        trades = [t for t in self.ledger.trades(since=since) if t.strategy == name]
        return {"trades_7d": len(trades), "pnl_7d": round(sum(t.pnl_usd for t in trades), 2),
                "win_rate_7d": round(sum(1 for t in trades if t.pnl_usd > 0) / len(trades) * 100, 1) if trades else None}

    # ------------------------------------------------------------------ research
    def _apply_saved_overrides(self) -> None:
        overrides: dict[str, dict] = self.ledger.get("strategy_overrides", {}) or {}
        if not overrides:
            return
        rebuilt: list[Strategy] = []
        for st in self.strategies:
            o = overrides.get(st.name)
            if o is None:
                rebuilt.append(st)
            elif o.get("enabled", True):
                rebuilt.append(build_strategy(st.name, {**st.params, **o.get("params", {})}))
        self.strategies = rebuilt
        log.info("applied saved strategy overrides: %s", [s.describe() for s in self.strategies])

    def research_once(self) -> dict[str, Any]:
        """Backtest + walk-forward optimise every strategy, ask Claude to pick, apply bounded updates."""
        now = self.clock()
        bars = max(self.s.data.candle_history, 1000)
        candles = {}
        for sym in self.symbols:
            if sym in self.removed_symbols:
                continue
            try:
                c = self.provider.get_candles(sym, self.s.data.candle_interval, bars)
                if len(c) >= 200:
                    candles[sym] = c
            except Exception as e:  # noqa: BLE001
                log.warning("research: no candles for %s: %s", sym, e)
        if not candles:
            return {"error": "no candle data"}
        configured = {c.name: c for c in self.s.strategies}
        current = {st.name: st.params for st in self.strategies}
        candidates: dict[str, list[Candidate]] = {}
        for name in configured:
            base = current.get(name, configured[name].params)
            cands = optimize(name, candles, fee_bps=self.s.execution.paper_fee_bps, slippage_bps=self.s.execution.paper_slippage_bps,
                             base_params=base)
            candidates[name] = [c for c in cands if c.score > -1e8][:6]
        tickers = self.provider.get_tickers(list(candles))
        market_summary = {s: {"price": t.price, "chg24h_pct": round(t.change_24h_pct, 2), "liq_usd": round(t.liquidity_usd),
                              "vol24h_usd": round(t.volume_24h_usd)} for s, t in tickers.items()}
        perf = performance_report(self.ledger, {s: t.price for s, t in tickers.items()}, since_ts=now - 14 * 86400)
        recent = {"net_profit_14d": round(perf.net_profit, 2), "trades_14d": perf.trades, "win_rate": round(perf.win_rate, 1),
                  "max_dd_pct": round(perf.max_drawdown_pct, 2), "by_strategy": perf.by_strategy}
        proposal = self.analyst.research(market_summary, {k: [c.row() for c in v] for k, v in candidates.items()}, current, recent) \
            if self.analyst.available else None

        overrides: dict[str, dict] = self.ledger.get("strategy_overrides", {}) or {}
        applied: list[str] = []
        if proposal is not None:
            for upd in proposal.strategy_updates:
                cands = candidates.get(upd.name)
                if cands is None:
                    continue
                entry = overrides.setdefault(upd.name, {})
                entry["enabled"] = bool(upd.enabled)
                if upd.candidate_index is not None and 0 <= upd.candidate_index < len(cands):
                    entry["params"] = cands[upd.candidate_index].params   # only backtested, OOS-positive params can be chosen
                applied.append(f"{upd.name}: enabled={upd.enabled} params={entry.get('params', 'unchanged')} ({upd.rationale})")
            for sym in proposal.remove_symbols:
                if sym in self.symbols:
                    self.removed_symbols.add(sym)
            self.ledger.set("removed_symbols", sorted(self.removed_symbols))
            self.ledger.log_decision("research", {"source": "claude", "market_view": proposal.market_view, "applied": applied,
                                                  "removed": proposal.remove_symbols}, ts=now)
        else:
            # Deterministic pass: PROPOSE the top candidate when it clearly beats current params out-of-sample.
            # Proposals are only applied when claude.auto_apply_overrides is true; otherwise the running strategy keeps
            # its frozen parameters so the paper record stays an independent forward test (see research_lab/SPLITS.md).
            proposals: list[str] = []
            for name, cands in candidates.items():
                if not cands:
                    continue
                best = cands[0]
                cur_oos = next((c.out_sample.get("total_return_pct", 0) for c in cands if c.params == current.get(name)), None)
                if cur_oos is None or best.out_sample.get("total_return_pct", 0) > cur_oos + 2.0:
                    proposals.append(f"{name}: params={best.params} (oos={best.out_sample.get('total_return_pct')}%)")
                    if self.s.claude.auto_apply_overrides:
                        overrides.setdefault(name, {})["params"] = best.params
                        applied.append(f"{name}: params={best.params} (deterministic, oos={best.out_sample.get('total_return_pct')}%)")
            self.ledger.log_decision("research", {"source": "deterministic", "proposals": proposals, "applied": applied,
                                                  "auto_apply": self.s.claude.auto_apply_overrides}, ts=now)
            if proposals and not applied:
                log.info("research proposals (NOT applied; parameters frozen): %s", proposals)
        self.ledger.set("strategy_overrides", overrides)
        self._rebuild_strategies(overrides, configured)
        summary = {"candidates": {k: [c.row() for c in v[:3]] for k, v in candidates.items()}, "applied": applied,
                   "claude": proposal.model_dump() if proposal else None, "symbols_removed": sorted(self.removed_symbols)}
        log.info("research complete: %s", applied or "no changes")
        return summary

    def _rebuild_strategies(self, overrides: dict[str, dict], configured: dict[str, StrategyConfig]) -> None:
        out: list[Strategy] = []
        for name, cfg in configured.items():
            o = overrides.get(name, {})
            if not cfg.enabled or not o.get("enabled", True):
                continue
            out.append(build_strategy(name, {**cfg.params, **o.get("params", {})}))
        self.strategies = out

    # ------------------------------------------------------------------ reporting
    def _maybe_daily_report(self, prices: dict[str, float]) -> None:
        day = dt.datetime.fromtimestamp(self.clock(), dt.UTC).strftime("%Y-%m-%d")
        if self.last_report_day is None:
            self.last_report_day = day
            self.ledger.set("last_report_day", day)
            return
        if day == self.last_report_day:
            return
        rep = performance_report(self.ledger, prices, since_ts=self.clock() - 86400)
        text = format_report(rep)
        events = [f"{dt.datetime.fromtimestamp(e['ts'], dt.UTC):%H:%M} {e['kind']}: {e['message']}" for e in self.ledger.events(30)]
        narrative = self.analyst.daily_narrative(text, events) if self.s.claude.daily_report and self.analyst.available else None
        out_dir = self.s.data_path / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        body = text + ("\n\n" + narrative if narrative else "")
        (out_dir / f"{self.last_report_day}.txt").write_text(body)
        self.ledger.log_event("daily_report", f"net ${rep.net_profit:,.2f} trades {rep.trades}", ts=self.clock())
        self.last_report_day = day
        self.ledger.set("last_report_day", day)


# ---------------------------------------------------------------------- wiring
def build_provider(settings: Settings) -> MarketDataProvider:
    d = settings.data
    if d.provider == "synthetic":
        return SyntheticProvider(settings.universe.symbols or d.synthetic_symbols, seed=d.synthetic_seed,
                                 interval=d.candle_interval, history=d.candle_history)
    if d.provider == "csv":
        return CsvReplayProvider(Path(settings.data_dir) / "csv" if d.csv_dir == "data/csv" else d.csv_dir,
                                 settings.universe.symbols, interval=d.candle_interval, history=d.candle_history)
    from ..data.live import LiveProvider
    return LiveProvider(d, settings.universe, settings.execution)


def build_bot(settings: Settings, provider: MarketDataProvider | None = None) -> TradingBot:
    provider = provider or build_provider(settings)
    clock: Callable[[], float] = (lambda: provider.now_ts) if provider.is_replay else time.time  # type: ignore[attr-defined]
    # Replay runs (synthetic / csv) are ledgered separately so they never count toward the live gate.
    ledger = Ledger(settings.db_path, mode="replay" if provider.is_replay else settings.mode)
    kill = KillSwitch(settings.kill_file, flatten_on_trip=settings.engine.flatten_on_kill)
    risk = RiskEngine(settings.risk, kill, clock=clock)
    book = PortfolioBook(ledger, settings.initial_capital_usd, clock)
    analyst = ClaudeAnalyst(settings.claude, ledger)
    strategies = [build_strategy(c.name, c.params) for c in settings.strategies if c.enabled]
    if settings.is_live:
        # The trading process never loads a private key: signing happens in the separate signer service
        # (tradebot_signer), and real submission is additionally disabled at build level (execution/submission.py).
        from ..data.live import LiveProvider
        from ..execution.jupiter_adapter import JupiterExecutionAdapter
        from ..execution.live_controls import LiveControls
        from ..execution.signer_protocol import SignerClient, load_client_secret
        gate = LiveGate(settings, ledger)
        gate.assert_armed()                       # refuse to even construct a live bot unless armed
        if not isinstance(provider, LiveProvider):
            raise RuntimeError("live mode requires the live data provider")
        controls = LiveControls(settings.live, ledger, kill, settings.live_halt_file, clock=clock)
        signer = SignerClient(settings.live.signer_url, load_client_secret(settings.live.signer_secret_file))
        broker: Broker = JupiterExecutionAdapter(risk, settings, provider, gate=gate, controls=controls, signer=signer,
                                                 dry_run=False, clock=clock)
    else:
        broker = PaperBroker(risk, provider, fee_bps=settings.execution.paper_fee_bps,
                             slippage_bps=settings.execution.paper_slippage_bps, clock=clock,
                             quote_timeout_sec=settings.data.jupiter_quote_timeout_sec,
                             live_slippage_bps=settings.execution.live_slippage_bps,
                             allow_keyless_quotes=settings.execution.paper_keyless_quotes)
    bot = TradingBot(settings, provider, ledger, risk, kill, broker, book, strategies, analyst, clock)
    if settings.is_live and settings.execution.reconcile_on_start:
        problems = bot.reconcile_balances()
        if problems:
            log.critical("startup balance reconciliation failed; kill switch tripped: %s", problems)
    if settings.universe.auto_scan and not provider.is_replay:
        try:
            discovered = provider.get_universe(settings.universe.max_symbols)
            for tok in discovered:
                if tok.symbol not in bot.symbols and len(bot.symbols) < settings.universe.max_symbols:
                    bot.symbols.append(tok.symbol)
        except Exception as e:  # noqa: BLE001
            log.warning("universe scan failed: %s", e)
    return bot
