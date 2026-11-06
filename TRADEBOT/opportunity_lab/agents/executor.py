"""Agent 4: paper execution and exit manager. Simulates the full lifecycle; nothing here can sign or send anything.

Entry: proposal -> risk gateway -> immutable intent (unique per proposal) -> cash reserved -> after
`entry_latency_sec`, the first fresh observation decides: no fresh data, no route, or a price that ran more than the
slippage tolerance FAILS the entry; thin pools give PARTIAL fills (size capped by liquidity); otherwise fill at the
observed price with all modelled costs (DEX fee, constant-product impact at the observed liquidity, Token-2022
transfer fee, network fee, token-account rent).

Exits, checked every cycle, first hit wins: liquidity deterioration, fixed stop, volatility stop, trailing stop,
take-profit, momentum reversal, time limit, emergency flatten. Exits fill at the OBSERVED price (a gap through the
stop fills below it). No sell route: the position is marked unsellable and, after `unsellable_writeoff_min`, written
off at zero. Missing data is treated like a missing route.

Crash safety: an exit commits trade + closed position + cash in one transaction. An entry intent left `created` by a
crash is abandoned on restart (paper: nothing external exists), and its reservation is released.
"""
from __future__ import annotations

import json
import statistics
import time

from ..config import LabConfig
from ..costs import buy_fill, sell_fill
from ..ledger import DuplicateIntent, LabLedger
from ..models import MintFacts, Opportunity, PaperPosition, TradeProposal, new_id
from ..risk import Book, LabRiskGateway


class PaperExecutor:
    def __init__(self, cfg: LabConfig, ledger: LabLedger, gateway: LabRiskGateway, market, route_source,
                 history_fn=None, clock=time.time):
        self.cfg, self.ledger, self.gw, self.market, self.routes, self.clock = cfg, ledger, gateway, market, route_source, clock
        self.history_fn = history_fn or (lambda m: [])
        self.book = Book(cash=float(ledger.get("cash", cfg.risk.paper_capital_usd)))
        self.reserved: dict[str, float] = {}                 # intent id -> USD
        self.pending: dict[str, dict] = {}                   # intent id -> entry order awaiting its fill time
        self.facts: dict[str, MintFacts] = {}
        self.stats = {"entries_submitted": 0, "entries_filled": 0, "entries_failed": 0, "partial_fills": 0,
                      "exits": 0, "unsellable_events": 0, "writeoffs": 0, "risk_rejections": 0, "duplicates_blocked": 0}

    # ------------------------------------------------------------------ recovery
    def recover(self) -> dict:
        self.book.positions = {p.id: p for p in self.ledger.open_positions()}
        self.book.cash = float(self.ledger.get("cash", self.cfg.risk.paper_capital_usd))
        out = {"positions": len(self.book.positions), "abandoned": 0}
        for r in self.ledger.pending_intents():
            self.ledger.resolve_intent(r["id"], "abandoned", {"why": "process restarted before the paper fill"}, ts=self.clock())
            out["abandoned"] += 1
        if out["abandoned"] or out["positions"]:
            self.ledger.event("recovered", json.dumps(out), ts=self.clock())
        return out

    def _available_cash(self) -> float:
        return self.book.cash - sum(self.reserved.values())

    # ------------------------------------------------------------------ entries
    def submit_entry(self, p: TradeProposal, opp: Opportunity, facts: MintFacts | None) -> str | None:
        now = self.clock()
        view = Book(cash=self._available_cash(), positions=dict(self.book.positions), marks=dict(self.book.marks),
                    reserved=sum(self.reserved.values()), pending_mints={o["mint"] for o in self.pending.values()})
        problems = self.gw.check_entry(p.mint, p.size_usd, view)
        if problems:
            self.stats["risk_rejections"] += 1
            self.ledger.event("entry_rejected", f"{p.symbol} {p.strategy}: " + "; ".join(problems), {"proposal": p.id}, ts=now)
            return None
        try:
            iid = self.ledger.create_intent("entry:" + p.id, p.mint, "buy", {"proposal": p.id, "size_usd": p.size_usd,
                                                                             "decision_price": p.decision_price}, ts=now)
        except DuplicateIntent:
            self.stats["duplicates_blocked"] += 1
            return None
        self.reserved[iid] = p.size_usd
        if facts:
            self.facts[p.mint] = facts
        rets = self._returns(p.mint)
        self.pending[iid] = {"mint": p.mint, "proposal": p, "opportunity": opp, "due": now + self.cfg.execution.entry_latency_sec,
                             "submitted": now, "vol": statistics.pstdev(rets) if len(rets) >= 5 else None}
        self.stats["entries_submitted"] += 1
        return iid

    def _returns(self, mint: str) -> list[float]:
        px = [h.price_usd for h in self.history_fn(mint) if h.price_usd]
        return [px[i] / px[i - 1] - 1 for i in range(1, len(px))]

    def _fail(self, iid: str, why: str) -> None:
        o = self.pending.pop(iid)
        self.reserved.pop(iid, None)
        self.ledger.resolve_intent(iid, "failed", {"why": why}, ts=self.clock())
        self.stats["entries_failed"] += 1
        self.ledger.event("entry_failed", f"{o['proposal'].symbol}: {why}", {"intent": iid}, ts=self.clock())

    def process_pending(self) -> None:
        now, ex = self.clock(), self.cfg.execution
        for iid in list(self.pending):
            o = self.pending[iid]
            if now < o["due"]:
                continue
            p: TradeProposal = o["proposal"]
            snap = self.market.refresh([p.mint]).get(p.mint)
            if snap is None or snap.price_usd is None or now - snap.observed_ts > ex.max_quote_age_sec:
                self._fail(iid, "no fresh quote at fill time"); continue
            route = self.routes.route_check(p.mint, p.size_usd)
            if ex.fail_entry_without_route and not route.ok and not route.unknown:
                self._fail(iid, f"route unavailable at fill time: {route.error}"); continue
            if snap.price_usd > p.decision_price * (1 + ex.slippage_tolerance_bps / 1e4):
                self._fail(iid, f"price ran {(snap.price_usd / p.decision_price - 1) * 1e4:.0f} bps past the decision"); continue
            size = min(p.size_usd, (snap.liquidity_usd or 0) * self.cfg.analyst.max_size_pct_of_liquidity / 100)
            if size < 5.0:
                self._fail(iid, "liquidity too thin at fill time"); continue
            partial = size < p.size_usd - 1e-9
            tf = (self.facts.get(p.mint).transfer_fee_bps or 0.0) if self.facts.get(p.mint) else 0.0
            qty, eff, fees = buy_fill(size, snap.price_usd, snap.liquidity_usd, snap.dex_id, ex, tf)
            x = self.cfg.exits
            pos = PaperPosition(id=new_id("pos"), proposal_id=p.id, mint=p.mint, symbol=p.symbol, strategy=p.strategy,
                                entry_ts=now, entry_price=eff, qty=qty, cost_usd=size, entry_liquidity_usd=snap.liquidity_usd or 0.0,
                                high_water=snap.price_usd, stop_price=snap.price_usd * (1 - x.stop_loss_pct / 100),
                                target_price=snap.price_usd * (1 + x.take_profit_pct / 100))
            if o["vol"]:
                vs = snap.price_usd * (1 - x.vol_stop_mult * o["vol"])
                pos.vol_stop_price = vs if vs > pos.stop_price else None
            with self.ledger.atomic():
                self.ledger.resolve_intent(iid, "filled", {"position": pos.id, "fill_mid": snap.price_usd, "size_filled": size,
                                                           "partial": partial, "fees_usd": fees,
                                                           "latency_sec": now - o["submitted"]}, ts=now)
                self.ledger.save_position(pos)
                self.book.cash -= size
                self.ledger.set("cash", self.book.cash)
                self.ledger.set(f"pos_meta:{pos.id}", {"opportunity": o["opportunity"].id, "signals": o["opportunity"].signals,
                                                       "est_cost_bps": p.est_cost_bps, "decision_price": p.decision_price,
                                                       "entry_mid": snap.price_usd, "entry_fees_usd": fees, "partial": partial,
                                                       "ai_scores": p.ai_scores, "size_requested": p.size_usd})
            self.pending.pop(iid)
            self.reserved.pop(iid, None)
            self.book.positions[pos.id] = pos
            self.book.marks[p.mint] = snap.price_usd
            self.stats["entries_filled"] += 1
            self.stats["partial_fills"] += int(partial)

    # ------------------------------------------------------------------ exits
    def _exit_reason(self, pos: PaperPosition, snap, flatten: bool) -> str | None:
        x, now, px = self.cfg.exits, self.clock(), snap.price_usd
        if flatten:
            return "emergency_flatten"
        if pos.entry_liquidity_usd and (snap.liquidity_usd or 0) <= pos.entry_liquidity_usd * (1 - x.liquidity_drop_exit_pct / 100):
            return "liquidity_deterioration"
        if px <= pos.stop_price:
            return "stop_loss"
        if pos.vol_stop_price and px <= pos.vol_stop_price:
            return "vol_stop"
        entry_mid = pos.stop_price / (1 - x.stop_loss_pct / 100)
        if pos.high_water >= entry_mid * (1 + x.trailing_activation_pct / 100) and px <= pos.high_water * (1 - x.trailing_stop_pct / 100):
            return "trailing_stop"
        if px >= pos.target_price:
            return "take_profit"
        if snap.price_change_m5 is not None and snap.price_change_m5 <= -x.momentum_reversal_pct and now - pos.entry_ts > 60:
            return "momentum_reversal"
        if now - pos.entry_ts >= x.max_hold_min * 60:
            return "time_exit"
        return None

    def monitor(self, flatten: bool = False) -> list[dict]:
        now, closed = self.clock(), []
        for pos in list(self.book.positions.values()):
            snap = self.market.refresh([pos.mint]).get(pos.mint)
            fresh = snap is not None and snap.price_usd and now - snap.observed_ts <= self.cfg.execution.max_quote_age_sec
            if not fresh:
                self._unsellable(pos, "no fresh market data")
                continue
            self.book.marks[pos.mint] = snap.price_usd
            pos.high_water = max(pos.high_water, snap.price_usd)
            reason = self._exit_reason(pos, snap, flatten)
            if reason is None:
                self.ledger.save_position(pos)
                continue
            route = self.routes.route_check(pos.mint, pos.qty * snap.price_usd)
            if not route.ok and not route.unknown:
                self._unsellable(pos, f"{reason} wanted, but {route.error or 'no sell route'}")
                continue
            closed.append(self._close(pos, reason, snap.price_usd, snap.liquidity_usd, snap.dex_id))
        return closed

    def _unsellable(self, pos: PaperPosition, why: str) -> None:
        now = self.clock()
        if pos.no_route_since is None:
            pos.no_route_since = now
            self.stats["unsellable_events"] += 1
            self.ledger.event("unsellable", f"{pos.symbol}: {why}", {"position": pos.id}, ts=now)
            self.ledger.save_position(pos)
        elif now - pos.no_route_since >= self.cfg.exits.unsellable_writeoff_min * 60:
            self.stats["writeoffs"] += 1
            self._close(pos, "unsellable_writeoff", 0.0, 0.0, "")

    def _close(self, pos: PaperPosition, reason: str, mid: float, liq: float | None, dex: str) -> dict:
        now = self.clock()
        if mid > 0:
            tf = (self.facts.get(pos.mint).transfer_fee_bps or 0.0) if self.facts.get(pos.mint) else 0.0
            proceeds, exit_fees = sell_fill(pos.qty, mid, liq, dex, self.cfg.execution, tf)
        else:
            proceeds, exit_fees = 0.0, 0.0
        meta = self.ledger.get(f"pos_meta:{pos.id}", {}) or {}
        pnl = proceeds - pos.cost_usd
        row = {"position_id": pos.id, "opportunity_id": meta.get("opportunity"), "proposal_id": pos.proposal_id, "mint": pos.mint,
               "symbol": pos.symbol, "strategy": pos.strategy, "entry_ts": pos.entry_ts, "exit_ts": now, "entry_price": pos.entry_price,
               "exit_price": mid, "qty": pos.qty, "cost_usd": pos.cost_usd, "proceeds_usd": proceeds, "pnl_usd": pnl,
               "pnl_pct": pnl / pos.cost_usd if pos.cost_usd else 0.0, "fees_usd": float(meta.get("entry_fees_usd", 0.0)) + exit_fees,
               "exit_reason": reason, "payload": json.dumps({**meta, "exit_mid": mid, "exit_liquidity": liq,
                                                             "hold_min": (now - pos.entry_ts) / 60}, default=str)}
        pos.state = "written_off" if reason == "unsellable_writeoff" else "closed"
        with self.ledger.atomic():
            key = f"exit:{pos.id}"
            iid = self.ledger.create_intent(key, pos.mint, "sell", {"reason": reason}, ts=now)
            self.ledger.add_trade(row)
            self.ledger.save_position(pos)
            self.book.cash += proceeds
            self.ledger.set("cash", self.book.cash)
            self.ledger.resolve_intent(iid, "filled", {"proceeds": proceeds}, ts=now)
        self.book.positions.pop(pos.id, None)
        self.stats["exits"] += 1
        return row
