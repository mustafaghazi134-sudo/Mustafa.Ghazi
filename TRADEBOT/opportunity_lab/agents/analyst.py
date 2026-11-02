"""Agent 3: trading opportunity analyst. Deterministic: entry rules, cost estimate, sizing and the accept/reject
decision are code. Optional AI scorers (Claude, Grok) run in SHADOW mode: their scores are stored on the proposal
and compared later with the deterministic baseline; they cannot change `accept`, size or exits.

Strategies (first match wins, in configured order):
  momentum_volume     momentum AND volume_surge
  breakout            breakout AND m5 buy share >= 0.55 AND m5 volume above the hour's 5-min average
  buy_pressure        buy_pressure AND m5 price change > 0
  migration_momentum  migration AND m5 price change > 0

Edge: a strategy's pre-cost edge is CALIBRATED only from its own closed paper trades (n >= calibration_min_trades).
Until then it is "uncalibrated" and, in paper exploration mode, trades are allowed so that evidence accumulates.
A strategy whose calibrated net expectancy has a 95% bootstrap upper bound below zero is switched off ("negative").
"""
from __future__ import annotations

import json
import random
import time

from ..config import LabConfig
from ..costs import round_trip_bps
from ..ledger import LabLedger
from ..models import Opportunity, RiskAssessment, TradeProposal, new_id

MIN_ORDER_USD = 5.0


def strategy_match(name: str, sig: dict, snap) -> str | None:
    b, s = snap.buys_m5 or 0, snap.sells_m5 or 0
    share = b / (b + s) if b + s else 0.0
    up = (snap.price_change_m5 or 0) > 0
    avg5 = (snap.volume_h1 or 0) / 12
    if name == "momentum_volume" and "momentum" in sig and "volume_surge" in sig:
        return f"m5 change {sig['momentum']}% with volume {sig['volume_surge']}x the hour average"
    if name == "breakout" and "breakout" in sig and share >= 0.55 and (snap.volume_m5 or 0) > avg5:
        return f"price {sig['breakout']}% above the prior high, buy share {share:.2f}"
    if name == "buy_pressure" and "buy_pressure" in sig and up:
        return f"buy share {sig['buy_pressure']} over {b + s} m5 txns, price rising"
    if name == "migration_momentum" and "migration" in sig and up:
        return f"migrated to {sig['migration']}, price rising"
    return None


class OpportunityAnalyst:
    def __init__(self, cfg: LabConfig, ledger: LabLedger, clock=time.time, ai_scorers: list | None = None):
        self.cfg, self.c, self.ledger, self.clock = cfg, cfg.analyst, ledger, clock
        self.ai_scorers = ai_scorers or []

    def calibration(self, strategy: str) -> dict:
        rows = self.ledger.x("SELECT pnl_pct, payload FROM trades WHERE strategy=?", (strategy,)).fetchall()
        n = len(rows)
        if n < self.c.calibration_min_trades:
            return {"status": "uncalibrated", "n": n}
        net = [r["pnl_pct"] * 100 for r in rows]                       # bps
        pre = [x + float(json.loads(r["payload"] or "{}").get("est_cost_bps", 0)) for x, r in zip(net, rows)]
        rng = random.Random(0)
        boots = sorted(sum(rng.choice(net) for _ in range(n)) / n for _ in range(2000))
        lo, hi = boots[50], boots[1949]
        status = "negative" if hi < 0 else ("calibrated" if lo > 0 else "inconclusive")
        return {"status": status, "n": n, "net_bps": sum(net) / n, "net_ci95": [lo, hi], "pre_cost_bps": sum(pre) / n}

    def propose(self, opp: Opportunity, a: RiskAssessment) -> TradeProposal | None:
        snap, ex, risk = opp.snapshot, self.cfg.exits, self.cfg.risk
        for name in self.c.strategies:
            cond = strategy_match(name, opp.signals, snap)
            if cond:
                break
        else:
            return None
        reasons, notes = [], []
        size = min(risk.position_usd, (snap.liquidity_usd or 0) * self.c.max_size_pct_of_liquidity / 100)
        tf = (a.facts.transfer_fee_bps or 0.0) if a.facts else 0.0
        est = round_trip_bps(max(size, MIN_ORDER_USD), snap.liquidity_usd, snap.dex_id, self.cfg.execution,
                             self.c.adverse_selection_bps, a.route, tf)
        cost_pct = est.total_bps / 100
        rr = (ex.take_profit_pct - cost_pct) / (ex.stop_loss_pct + cost_pct) if ex.stop_loss_pct + cost_pct > 0 else 0.0
        cal = self.calibration(name)
        edge = cal.get("pre_cost_bps")
        accept = True
        if not a.approved:
            accept = False; reasons.append("security agent rejected: " + "; ".join(a.reasons[:3]))
        if size < MIN_ORDER_USD:
            accept = False; reasons.append(f"size ${size:.2f} below ${MIN_ORDER_USD:.0f} (pool too small for our limit)")
        if est.total_bps > self.c.max_total_cost_bps:
            accept = False; reasons.append(f"estimated round-trip cost {est.total_bps:.0f} bps > {self.c.max_total_cost_bps:.0f}")
        if rr < self.c.min_reward_risk:
            accept = False; reasons.append(f"reward/risk {rr:.2f} after costs < {self.c.min_reward_risk}")
        if cal["status"] == "negative":
            accept = False; reasons.append(f"{name} calibrated net expectancy is negative (n={cal['n']}, CI {cal['net_ci95']})")
        if self.c.require_calibrated_edge and cal["status"] != "calibrated":
            accept = False; reasons.append(f"{name} edge not calibrated positive ({cal['status']}, n={cal['n']})")
        if accept:
            reasons.append(f"{name}: {cond}; cost {est.total_bps:.0f} bps; reward/risk {rr:.2f}")
        if cal["status"] == "uncalibrated":
            notes.append(f"edge unknown: {cal['n']} of {self.c.calibration_min_trades} trades needed to calibrate {name}")
        if a.route is None or not a.route.ok:
            notes.append("cost from the AMM model, not a live quote")
        notes.append("costs " + str(est))
        if a.data_gaps:
            notes.append("data gaps: " + ", ".join(a.data_gaps))
        p = TradeProposal(id=new_id("prop"), opportunity_id=opp.id, mint=opp.mint, symbol=snap.symbol, strategy=name,
                          entry_condition=cond, decision_price=snap.price_usd or 0.0, edge_pre_cost_bps=edge,
                          edge_status=cal["status"], est_cost_bps=round(est.total_bps, 1), stop_pct=ex.stop_loss_pct,
                          target_pct=ex.take_profit_pct, reward_risk=round(rr, 3), size_usd=round(size, 2), accept=accept,
                          reasons=reasons, confidence_notes=notes, ts=self.clock())
        for scorer in self.ai_scorers:                          # shadow only: recorded, never consulted
            try:
                res = scorer.score(p, opp, a)
                if res is not None:
                    p.ai_scores[scorer.name] = res
            except Exception as e:  # noqa: BLE001
                p.ai_scores[scorer.name] = {"error": type(e).__name__}
        return p
