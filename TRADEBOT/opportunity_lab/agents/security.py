"""Agent 2: token security and risk analyst. Rejects before any simulation. Never declares a token safe: it produces
flags with severities and approves only when there is no critical or high flag. Missing critical data is itself a
critical flag (fail closed).

Independent signals used: on-chain mint account (authorities, Token-2022 extensions and transfer fee), on-chain
holder concentration, Jupiter's indexer flags (isSus, organic score), DEX pair data (liquidity, age, buy/sell
counts), the scanner's own liquidity history (withdrawals), and read-only buy + sell-back quotes (route existence,
round-trip loss, price impact). A buy route without a sell route is treated as a honeypot.
"""
from __future__ import annotations

import time

from ..config import LabConfig
from ..models import Opportunity, RiskAssessment

SEV_WEIGHT = {"critical": 40, "high": 15, "medium": 5, "info": 0}


class SecurityAnalyst:
    def __init__(self, cfg: LabConfig, facts_source, route_source, history_fn, clock=time.time):
        self.cfg, self.c = cfg, cfg.security
        self.facts_source, self.route_source, self.history_fn, self.clock = facts_source, route_source, history_fn, clock

    def assess(self, opp: Opportunity, size_usd: float) -> RiskAssessment:
        c, now, s = self.c, self.clock(), opp.snapshot
        flags: list[dict] = []
        gaps: list[str] = []

        def flag(code: str, sev: str, detail: str) -> None:
            flags.append({"code": code, "severity": sev, "detail": detail})

        # ---- pair data
        if not s.liquidity_usd:
            flag("liquidity_unknown", "critical", "no liquidity figure"); gaps.append("liquidity")
        elif s.liquidity_usd < c.min_liquidity_usd:
            flag("low_liquidity", "critical", f"${s.liquidity_usd:,.0f} < ${c.min_liquidity_usd:,.0f}")
        age = s.age_min(now)
        if age is None:
            flag("age_unknown", "critical", "pair creation time unknown"); gaps.append("age")
        elif age < c.min_age_min:
            flag("too_new", "critical", f"{age:.0f} min old (< {c.min_age_min:.0f})")
        hist = self.history_fn(opp.mint) or []
        liqs = [h.liquidity_usd for h in hist if h.liquidity_usd]
        if len(liqs) >= 2 and max(liqs) > 0:
            drop = (1 - liqs[-1] / max(liqs)) * 100
            if drop >= c.max_liquidity_drop_pct:
                flag("liquidity_withdrawal", "critical", f"liquidity down {drop:.0f}% from its tracked peak")
        if (s.buys_h1 or 0) >= 50 and (s.sells_h1 or 0) == 0:
            flag("no_sellers", "high", f"{s.buys_h1} buys and 0 sells in 1 h (honeypot pattern)")
        if "promoted" in opp.signals:
            flag("paid_promotion", "info", "discovered through a paid DexScreener boost")

        # ---- mint facts
        facts = None
        try:
            facts = self.facts_source.mint_facts(opp.mint)
        except Exception as e:  # noqa: BLE001
            gaps.append(f"facts error: {type(e).__name__}")
        if facts is None or not facts.known:
            flag("facts_missing", "critical", "mint authority / freeze authority / extensions unknown"); gaps.append("mint facts")
        else:
            if facts.mint_authority and c.require_mint_authority_revoked:
                flag("mint_authority", "critical", "mint authority not revoked (supply can be inflated)")
            if facts.freeze_authority and c.require_freeze_authority_revoked:
                flag("freeze_authority", "critical", "freeze authority not revoked (holders can be frozen)")
            for ext in facts.extensions or []:
                if ext in c.reject_token2022_extensions:
                    flag(f"token2022_{ext}", "critical", f"Token-2022 extension {ext}")
            if (facts.transfer_fee_bps or 0) > c.max_transfer_fee_bps:
                flag("transfer_fee", "critical", f"transfer fee {facts.transfer_fee_bps:.0f} bps")
            if facts.jupiter_sus and c.reject_jupiter_sus:
                flag("jupiter_sus", "critical", "Jupiter audit marks the token suspicious")
            if facts.top10_pct is None:
                flag("holders_unknown", "high", "holder concentration unknown"); gaps.append("holders")
            else:
                if facts.top10_pct > c.max_top10_holder_pct:
                    flag("holder_concentration", "high", f"top-10 holders {facts.top10_pct:.0f}% (> {c.max_top10_holder_pct:.0f}%)")
                if (facts.top1_pct or 0) > c.max_single_holder_pct:
                    flag("whale", "high", f"largest holder {facts.top1_pct:.0f}% (> {c.max_single_holder_pct:.0f}%)")
            if c.min_organic_score and (facts.organic_score is None or facts.organic_score < c.min_organic_score):
                flag("low_organic_score", "medium", f"organic score {facts.organic_score}")

        # ---- executable route, both directions
        route = None
        try:
            route = self.route_source.route_check(opp.mint, size_usd)
        except Exception as e:  # noqa: BLE001
            gaps.append(f"route error: {type(e).__name__}")
        if route is None or route.unknown:
            flag("route_unknown", "critical", "no buy/sell quote available"); gaps.append("route")
        elif not route.ok:
            sev_code = "honeypot_suspect" if route.buy_out_raw else "no_route"
            flag(sev_code, "critical", route.error or "no executable route")
        else:
            if route.round_trip_loss_bps is not None and route.round_trip_loss_bps > c.max_round_trip_loss_bps:
                flag("round_trip_loss", "high", f"buy+sell back loses {route.round_trip_loss_bps:.0f} bps")
            if route.price_impact_bps is not None and route.price_impact_bps > c.max_price_impact_bps:
                flag("price_impact", "high", f"impact {route.price_impact_bps:.0f} bps")

        bad = [f for f in flags if f["severity"] in ("critical", "high")]
        score = max(0.0, 100.0 - sum(SEV_WEIGHT[f["severity"]] for f in flags))
        return RiskAssessment(opp.mint, now, approved=not bad, score=score, flags=flags, route=route, facts=facts, data_gaps=gaps)
