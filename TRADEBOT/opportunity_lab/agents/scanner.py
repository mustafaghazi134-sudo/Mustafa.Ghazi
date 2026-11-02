"""Agent 1: opportunity scanner. Discovers tokens from every enabled source, keeps a rolling history per token,
records every observation, and emits an Opportunity when at least one deterministic detector fires.

Detectors (all computed from data observed at or before `now`):
  new_token          pair younger than scan.new_token_max_age_min at first sighting (discovery tag, not a trade signal)
  volume_surge       m5 volume >= ratio x the average 5-minute volume of the last hour
  liquidity_growth   pool liquidity up >= X% within the tracked history; liquidity_drop is recorded as a warning
  momentum           m5 price change >= X%
  breakout           price above the maximum of the previous N observations
  buy_pressure       buys / (buys + sells) over 5 min >= ratio with at least min_txns transactions
  migration          the most liquid pair moved from a launchpad (pump.fun, Meteora DBC) to an AMM pool
"""
from __future__ import annotations

import time
from collections import deque

from ..config import LabConfig
from ..ledger import LabLedger
from ..models import Opportunity, TokenSnapshot, new_id

LAUNCHPADS = {"pumpfun", "pump.fun", "meteoradbc", "launchlab", "moonshot", "boop"}
POSITIVE = ("volume_surge", "liquidity_growth", "momentum", "breakout", "buy_pressure", "migration")


class Scanner:
    def __init__(self, cfg: LabConfig, sources: list, ledger: LabLedger, clock=time.time):
        self.cfg, self.c, self.sources, self.ledger, self.clock = cfg, cfg.scan, sources, ledger, clock
        self.history: dict[str, deque[TokenSnapshot]] = {}
        self.provenance: dict[str, set[str]] = {}
        self.last_emit: dict[str, float] = {}
        self.stats = {"observations": 0, "discovered": 0, "opportunities": 0, "ineligible": 0, "source_errors": 0}

    def _track(self, s: TokenSnapshot) -> None:
        h = self.history.get(s.mint)
        if h is None:
            if len(self.history) >= self.c.max_tracked_tokens:
                stalest = min(self.history, key=lambda m: self.history[m][-1].observed_ts)
                del self.history[stalest]
            h = self.history[s.mint] = deque(maxlen=self.c.history_points)
            self.stats["discovered"] += 1
        if h and h[-1].observed_ts == s.observed_ts and h[-1].source == s.source:
            return
        h.append(s)
        self.provenance.setdefault(s.mint, set()).add(s.source)
        self.ledger.add_snapshot(s)
        self.stats["observations"] += 1

    def signals(self, mint: str) -> dict:
        h = self.history.get(mint)
        if not h:
            return {}
        s, c, sig = h[-1], self.c, {}
        age = s.age_min()
        if age is not None and age <= c.new_token_max_age_min and len(h) == 1:
            sig["new_token"] = round(age, 1)
        if s.volume_m5 and s.volume_h1 and s.volume_h1 > 0:
            ratio = s.volume_m5 / (s.volume_h1 / 12)
            if ratio >= c.volume_surge_ratio:
                sig["volume_surge"] = round(ratio, 2)
        liqs = [x.liquidity_usd for x in h if x.liquidity_usd]
        if len(liqs) >= 2 and liqs[0] > 0:
            chg = (liqs[-1] / min(liqs) - 1) * 100
            drop = (1 - liqs[-1] / max(liqs)) * 100
            if chg >= c.liquidity_change_pct:
                sig["liquidity_growth"] = round(chg, 1)
            if drop >= c.liquidity_change_pct:
                sig["liquidity_drop"] = round(drop, 1)
        if s.price_change_m5 is not None and s.price_change_m5 >= c.momentum_pct_m5:
            sig["momentum"] = round(s.price_change_m5, 2)
        prices = [x.price_usd for x in list(h)[:-1] if x.price_usd]
        if s.price_usd and len(prices) >= c.breakout_lookback_points and s.price_usd > max(prices[-c.breakout_lookback_points:]):
            sig["breakout"] = round((s.price_usd / max(prices[-c.breakout_lookback_points:]) - 1) * 100, 2)
        b, se = s.buys_m5 or 0, s.sells_m5 or 0
        if b + se >= c.min_txns_m5 and b / (b + se) >= c.buy_pressure_ratio:
            sig["buy_pressure"] = round(b / (b + se), 3)
        dexes = [x.dex_id for x in h if x.dex_id]
        if len(dexes) >= 2 and dexes[-1] not in LAUNCHPADS and any(d in LAUNCHPADS for d in dexes[:-1]):
            sig["migration"] = dexes[-1]
        return sig

    def cycle(self) -> list[Opportunity]:
        now = self.clock()
        for src in self.sources:
            try:
                for s in src.discover():
                    self._track(s)
            except Exception as e:  # noqa: BLE001 - one bad source never stops the scan
                self.stats["source_errors"] += 1
                self.ledger.event("source_error", f"{getattr(src, 'name', src)} discover: {type(e).__name__}: {e}", ts=now)
        tracked = list(self.history)
        for src in self.sources:
            try:
                for s in src.refresh(tracked).values():
                    self._track(s)
            except Exception as e:  # noqa: BLE001
                self.stats["source_errors"] += 1
                self.ledger.event("source_error", f"{getattr(src, 'name', src)} refresh: {type(e).__name__}: {e}", ts=now)
        out = []
        for mint, h in self.history.items():
            s = h[-1]
            if now - s.observed_ts > 3 * self.c.poll_sec:
                continue                                    # stale: nothing current to act on
            sig = self.signals(mint)
            if not any(k in sig for k in POSITIVE):
                continue
            if any(mint in getattr(src, "promoted", ()) for src in self.sources):
                sig["promoted"] = True                      # paid boost: recorded, never a signal
            key = ",".join(sorted(k for k in sig if k in POSITIVE))
            if now - self.last_emit.get(mint + key, -1e18) < 600:
                continue
            self.last_emit[mint + key] = now
            o = Opportunity(new_id("opp"), mint, now, sig, s, sorted(self.provenance.get(mint, ())))
            if not s.price_usd or s.price_usd <= 0:
                o.eligible, o.ineligible_reason = False, "no price"
            elif (s.liquidity_usd or 0) < self.c.min_liquidity_usd:
                o.eligible, o.ineligible_reason = False, f"liquidity ${s.liquidity_usd or 0:,.0f} below discovery floor"
            elif s.quote_symbol and s.quote_symbol.upper() not in ("SOL", "WSOL", "USDC", "USDT"):
                o.eligible, o.ineligible_reason = False, f"quote token {s.quote_symbol} not routable by policy"
            self.stats["opportunities" if o.eligible else "ineligible"] += 1
            self.ledger.add_opportunity(o)
            out.append(o)
        return out
