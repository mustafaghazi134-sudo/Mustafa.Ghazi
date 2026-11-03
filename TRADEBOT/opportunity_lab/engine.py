"""Orchestrator: scan -> assess -> analyse -> (paper) enter -> monitor -> exit -> record, once per cycle.

Order inside a cycle: (1) scanner observes and records; (2) pending paper entries fill or fail; (3) open positions are
monitored and exited; (4) new opportunities are assessed, analysed and, if accepted and allowed by the risk gateway,
submitted as paper entries. A halt (data/lab/LAB_HALT or the main bot's data/KILL) stops step 4 only; a halt
written with flatten=true also exits every position at the next cycle.

The security agent's API budget is spent only on opportunities that match a strategy (cheap check first).
"""
from __future__ import annotations

import json
import logging
import time

from tradebot.instance_lock import InstanceLock

from .agents.analyst import OpportunityAnalyst, strategy_match
from .agents.executor import PaperExecutor
from .agents.scanner import Scanner
from .agents.security import SecurityAnalyst
from .config import LabConfig
from .ledger import LabLedger
from .risk import LabRiskGateway

log = logging.getLogger("opportunity_lab")


class _MarketView:
    """Fresh snapshots for the executor: ask the sources, record what they return, fall back to scanner history."""

    def __init__(self, scanner: Scanner):
        self.scanner = scanner

    def refresh(self, mints: list[str]) -> dict:
        out = {}
        for src in self.scanner.sources:
            missing = [m for m in mints if m not in out]
            if not missing:
                break
            try:
                for m, s in src.refresh(missing).items():
                    self.scanner._track(s)
                    out[m] = s
            except Exception:  # noqa: BLE001
                continue
        for m in mints:
            if m not in out and self.scanner.history.get(m):
                out[m] = self.scanner.history[m][-1]
        return out


class _CachedFacts:
    def __init__(self, inner, max_age: float, clock):
        self.inner, self.max_age, self.clock, self.cache = inner, max_age, clock, {}

    def mint_facts(self, mint: str):
        hit = self.cache.get(mint)
        if hit and self.clock() - hit[0] <= self.max_age:
            return hit[1]
        f = self.inner.mint_facts(mint)
        # cache only fully confirmed facts: a lookup that failed (429, throttle) must be retried, not reused
        if f is not None and f.known and getattr(f, "holders_confirmed", True):
            self.cache[mint] = (self.clock(), f)
        return f


class LabEngine:
    def __init__(self, cfg: LabConfig, market_sources: list, facts_source, route_source, clock=time.time,
                 ai_scorers: list | None = None, lock: bool = True):
        self.cfg, self.clock = cfg, clock
        self.lock = InstanceLock(cfg.data_path / "lab.lock").acquire() if lock else None
        self.ledger = LabLedger(cfg.db_path)
        self.gateway = LabRiskGateway(cfg, self.ledger, clock)
        self.scanner = Scanner(cfg, market_sources, self.ledger, clock)
        hist = lambda m: list(self.scanner.history.get(m, []))  # noqa: E731
        self.security = SecurityAnalyst(cfg, _CachedFacts(facts_source, cfg.security.max_assessment_age_sec, clock),
                                        route_source, hist, clock)
        self.analyst = OpportunityAnalyst(cfg, self.ledger, clock, ai_scorers)
        self.executor = PaperExecutor(cfg, self.ledger, self.gateway, _MarketView(self.scanner), route_source, hist, clock)
        self.recovery = self.executor.recover()
        self.counts = {"cycles": 0, "opportunities": 0, "assessed": 0, "security_rejected": 0, "no_strategy": 0,
                       "analyst_rejected": 0, "proposals_accepted": 0}
        self.ledger.event("start", json.dumps({"mode": "paper", "recovered": self.recovery}), ts=clock())

    def close(self) -> None:
        self.ledger.close()
        if self.lock:
            self.lock.release()

    def _flatten_requested(self) -> bool:
        try:
            return bool(json.loads(self.cfg.halt_file.read_text()).get("flatten")) if self.cfg.halt_file.exists() else False
        except (ValueError, OSError):
            return False

    def cycle(self) -> dict:
        now = self.clock()
        halted = self.gateway.halted()
        with self.ledger.atomic():                      # one commit for the cycle's observations
            opps = self.scanner.cycle()
        self.executor.process_pending()
        closed = self.executor.monitor(flatten=self._flatten_requested())
        accepted = 0
        for o in opps:
            if not o.eligible:
                continue
            self.counts["opportunities"] += 1
            if not any(strategy_match(n, o.signals, o.snapshot) for n in self.cfg.analyst.strategies):
                self.counts["no_strategy"] += 1
                continue
            a = self.security.assess(o, self.cfg.risk.position_usd)
            self.ledger.add_assessment(a, o.id)
            self.counts["assessed"] += 1
            if not a.approved:
                self.counts["security_rejected"] += 1
                continue
            p = self.analyst.propose(o, a)
            if p is None:
                continue
            self.ledger.add_proposal(p)
            if not p.accept:
                self.counts["analyst_rejected"] += 1
                continue
            self.counts["proposals_accepted"] += 1
            if halted:
                self.ledger.event("entry_skipped", f"{p.symbol}: {halted}", ts=now)
                continue
            if self.executor.submit_entry(p, o, a.facts):
                accepted += 1
        exposure = self.executor.book.exposure()
        self.ledger.add_equity(now, self.executor.book.cash, exposure)
        self.counts["cycles"] += 1
        summary = {"ts": now, "kind": "cycle", "halted": halted, "tracked": len(self.scanner.history), "opportunities": len(opps),
                   "entries_submitted": accepted, "closed": len(closed), "open": len(self.executor.book.positions),
                   "pending": len(self.executor.pending), "cash": round(self.executor.book.cash, 2), "exposure": round(exposure, 2),
                   "rate_limits": {k: v for k, v in self.scanner.sources[0].__dict__.get("http", None).limiter.stats().items()}
                   if self.scanner.sources and hasattr(self.scanner.sources[0], "http") else {}}
        with open(self.cfg.events_file, "a") as f:
            f.write(json.dumps(summary, default=str) + "\n")
        return summary

    def run(self, cycles: int | None = None, minutes: float | None = None, advance=None, sleep=time.sleep) -> dict:
        """advance(sec): simulated clocks move time forward; for live runs it is None and the loop sleeps."""
        start, n = self.clock(), 0
        while True:
            if cycles is not None and n >= cycles:
                break
            if minutes is not None and self.clock() - start >= minutes * 60:
                break
            self.cycle()
            n += 1
            if advance:
                advance(self.cfg.scan.poll_sec)
            else:
                sleep(self.cfg.scan.poll_sec)
        return {"cycles": n, **self.counts, "scanner": self.scanner.stats, "executor": self.executor.stats}

    def finish(self) -> int:
        """End of a test window: exit everything at the last observation; whatever cannot be sold is written off at
        zero (no open position is ever left out of the results)."""
        self.executor.process_pending()
        for iid in list(self.executor.pending):
            self.executor._fail(iid, "test window ended before the fill")
        self.executor.monitor(flatten=True)
        n = 0
        for pos in list(self.executor.book.positions.values()):
            self.executor._close(pos, "end_of_test_unsellable", 0.0, 0.0, "")
            n += 1
        return n
