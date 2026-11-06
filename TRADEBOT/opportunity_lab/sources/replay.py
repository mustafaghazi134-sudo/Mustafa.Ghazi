"""Replay of recorded observations (the lab's own `snapshots` table, or JSONL files with the same fields).

Point-in-time rules, so a backtest cannot see the future:
  * a snapshot becomes visible only at its `observed_ts` (when the lab received it), never at the source's timestamp;
  * refresh(mints) returns the latest snapshot at or before now and no older than `max_staleness_sec`;
  * discover() returns tokens whose FIRST observation falls in the interval since the previous discover() call;
  * security facts and route checks are replayed from recorded assessments only; when none was recorded before now,
    they are reported as unknown, which makes the security agent reject (fail closed).
Tokens that later died or were delisted stay in the data exactly as recorded, so the replay has no survivorship
filter beyond what the live scanner itself observed.
"""
from __future__ import annotations

import bisect
import json
from pathlib import Path

from ..ledger import LabLedger
from ..models import MintFacts, RouteCheck, TokenSnapshot


class ReplayMarket:
    name = "replay"

    def __init__(self, clock, snapshots: list[TokenSnapshot], facts: dict[str, list[tuple[float, MintFacts]]] | None = None,
                 routes: dict[str, list[tuple[float, RouteCheck]]] | None = None, max_staleness_sec: float = 180.0):
        self.clock = clock
        self.by_mint: dict[str, list[TokenSnapshot]] = {}
        for s in sorted(snapshots, key=lambda s: s.observed_ts):
            self.by_mint.setdefault(s.mint, []).append(s)
        self._ts = {m: [s.observed_ts for s in v] for m, v in self.by_mint.items()}
        self.first_seen = sorted((v[0].observed_ts, m) for m, v in self.by_mint.items())
        self.facts, self.routes = facts or {}, routes or {}
        self.max_staleness = max_staleness_sec
        self._last_discover = float("-inf")

    @property
    def start(self) -> float:
        return self.first_seen[0][0] if self.first_seen else 0.0

    @property
    def end(self) -> float:
        return max((v[-1].observed_ts for v in self.by_mint.values()), default=0.0)

    def _latest(self, mint: str, now: float) -> TokenSnapshot | None:
        ts = self._ts.get(mint)
        if not ts:
            return None
        i = bisect.bisect_right(ts, now) - 1
        if i < 0:
            return None
        s = self.by_mint[mint][i]
        return s if now - s.observed_ts <= self.max_staleness else None

    def discover(self) -> list[TokenSnapshot]:
        now = self.clock()
        out = []
        for ts, m in self.first_seen:
            if self._last_discover < ts <= now:
                s = self._latest(m, now)
                if s:
                    out.append(s)
        self._last_discover = now
        return out

    def refresh(self, mints: list[str]) -> dict[str, TokenSnapshot]:
        now = self.clock()
        return {m: s for m in mints if (s := self._latest(m, now)) is not None}

    def _recorded(self, table: dict, mint: str):
        rows = table.get(mint) or []
        now = self.clock()
        cands = [x for t, x in rows if t <= now]
        return cands[-1] if cands else None

    def mint_facts(self, mint: str) -> MintFacts | None:
        return self._recorded(self.facts, mint)

    def route_check(self, mint: str, size_usd: float) -> RouteCheck:
        r = self._recorded(self.routes, mint)
        return r if r is not None else RouteCheck(ok=False, source="replay", error="no recorded route check", unknown=True)

    # ------------------------------------------------------------------ loaders
    @classmethod
    def from_ledger(cls, clock, path: str | Path, start: float = 0.0, end: float = 1e18, **kw) -> "ReplayMarket":
        led = LabLedger(path)
        snaps = led.snapshots(start, end)
        facts: dict = {}
        routes: dict = {}
        for r in led.x("SELECT ts, mint, route, facts FROM assessments WHERE ts>=? AND ts<? ORDER BY ts", (start, end)).fetchall():
            if r["facts"] and r["facts"] != "null":
                facts.setdefault(r["mint"], []).append((r["ts"], MintFacts(**json.loads(r["facts"]))))
            if r["route"] and r["route"] != "null":
                routes.setdefault(r["mint"], []).append((r["ts"], RouteCheck(**json.loads(r["route"]))))
        led.close()
        return cls(clock, snaps, facts, routes, **kw)

    @classmethod
    def from_jsonl(cls, clock, files: list[str], **kw) -> "ReplayMarket":
        snaps = []
        for f in files:
            for line in Path(f).read_text().splitlines():
                if line.strip():
                    d = json.loads(line)
                    d.setdefault("source", f"replay:{Path(f).name}")
                    snaps.append(TokenSnapshot.from_dict(d))
        return cls(clock, snaps, **kw)
