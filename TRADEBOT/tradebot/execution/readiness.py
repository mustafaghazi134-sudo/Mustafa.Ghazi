"""Paper-record readiness, measured from timestamps and operational state (not from row counts).

Two separate questions, never merged:

1. OPERATIONAL readiness (`operating_report`, enforced with the existing thresholds
   `min_paper_trades_before_live`, `min_paper_days_before_live`, `min_paper_coverage_before_live`):
   did the paper engine actually run, without gaps, for long enough?
   Coverage = time between consecutive equity snapshots that were no further apart than the gap tolerance, minus any time
   the kill switch was tripped, divided by the span from the first snapshot to now. A snapshot is a timestamp; ten
   thousand snapshots crammed into one hour earn one hour. A long outage earns nothing however dense the rest is.
   Time-based facts come only from equity snapshots and kill-switch events, so a backdated or invented "start" event no
   longer starts the clock.

2. STRATEGY evidence (`strategy_report`): REPORT ONLY. It states facts about the closed trades (net expectancy, a
   bootstrap interval, drawdown, concentration). It enforces nothing: no numeric criterion has been approved, and
   passing operational readiness says nothing about profitability. Out-of-sample validity cannot be read from a ledger
   at all and is reported as "not evaluated".
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Any

HALT_KINDS = ("kill_switch", "kill_reset")
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20261104        # fixed: the same trades always give the same interval


@dataclass(frozen=True)
class PaperEvidence:
    """Everything the verifier needs, from either the local ledger or a verified imported bundle."""
    source: str                                   # "local_ledger" | "imported_bundle"
    equity: tuple[tuple[float, float], ...]       # (ts, equity_usd)
    trades: tuple[dict, ...]                      # entry_ts, exit_ts, pnl_usd, fees_usd, symbol, strategy
    halts: tuple[tuple[float, str], ...]          # (ts, "kill_switch" | "kill_reset")
    provenance: dict = field(default_factory=dict)


def evidence_from_ledger(ledger) -> PaperEvidence:
    trades = tuple({"entry_ts": t.entry_ts, "exit_ts": t.exit_ts, "pnl_usd": t.pnl_usd, "fees_usd": t.fees_usd,
                    "symbol": t.symbol, "strategy": t.strategy} for t in ledger.trades(mode="paper"))
    return PaperEvidence("local_ledger", tuple(ledger.equity_points(mode="paper")), trades,
                         tuple(ledger.events_of_kinds(HALT_KINDS, mode="paper")))


# ---- operational ----------------------------------------------------------------------------------------------------

@dataclass
class OperatingReport:
    first_ts: float | None
    span_days: float
    covered_days: float
    coverage: float
    snapshots_used: int
    snapshots_rejected: int
    gap_tolerance_sec: float
    outage_count: int
    longest_gap_sec: float
    stale_tail_sec: float
    halted_days: float
    outages: list[tuple[float, float]]

    def to_dict(self, max_outages: int = 20) -> dict:
        return {"first_ts": self.first_ts, "span_days": round(self.span_days, 3), "covered_days": round(self.covered_days, 3),
                "coverage": round(self.coverage, 4), "snapshots_used": self.snapshots_used,
                "snapshots_rejected": self.snapshots_rejected, "gap_tolerance_sec": self.gap_tolerance_sec,
                "outage_count": self.outage_count, "longest_gap_sec": round(self.longest_gap_sec, 1),
                "stale_tail_sec": round(self.stale_tail_sec, 1), "halted_days": round(self.halted_days, 3),
                "outages": [[round(a, 1), round(b, 1)] for a, b in self.outages[:max_outages]]}


def _halted_windows(halts, now: float) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    start: float | None = None
    for ts, kind in sorted((h for h in halts if isinstance(h[0], (int, float)) and math.isfinite(h[0]) and h[0] <= now)):
        if kind == "kill_switch" and start is None:
            start = ts
        elif kind == "kill_reset" and start is not None:
            out.append((start, ts))
            start = None
    if start is not None:
        out.append((start, now))                  # tripped and never reset: still halted
    return out


def _overlap(a0: float, a1: float, windows: list[tuple[float, float]]) -> float:
    return sum(max(0.0, min(a1, w1) - max(a0, w0)) for w0, w1 in windows)


def operating_report(ev: PaperEvidence, *, now: float, interval_sec: float, tolerance_intervals: float) -> OperatingReport:
    tol = max(1.0, interval_sec * tolerance_intervals)
    raw = [p[0] for p in ev.equity]
    ts = sorted({t for t in raw if isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t) and t <= now})
    rejected = sum(1 for t in raw if not (isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t) and t <= now))
    if not ts:
        return OperatingReport(None, 0.0, 0.0, 0.0, 0, rejected, tol, 0, 0.0, 0.0, 0.0, [])
    halted = _halted_windows(ev.halts, now)
    covered = 0.0
    outages: list[tuple[float, float]] = []
    for a, b in zip(ts, ts[1:]):
        if b - a <= tol:
            covered += (b - a) - _overlap(a, b, halted)
        else:
            outages.append((a, b))
    tail = now - ts[-1]
    if tail <= tol:
        covered += tail - _overlap(ts[-1], now, halted)
    else:
        outages.append((ts[-1], now))
    span = now - ts[0]
    longest = max((b - a for a, b in outages), default=0.0)
    halted_sec = _overlap(ts[0], now, halted)
    return OperatingReport(first_ts=ts[0], span_days=span / 86400.0, covered_days=max(0.0, covered) / 86400.0,
                           coverage=min(1.0, max(0.0, covered / span)) if span > 0 else 0.0, snapshots_used=len(ts),
                           snapshots_rejected=rejected, gap_tolerance_sec=tol, outage_count=len(outages),
                           longest_gap_sec=longest, stale_tail_sec=max(0.0, tail), halted_days=halted_sec / 86400.0,
                           outages=outages)


# ---- strategy evidence (report only) --------------------------------------------------------------------------------

def _max_drawdown_pct(equity: tuple[tuple[float, float], ...]) -> float:
    peak, worst = -math.inf, 0.0
    for _, v in sorted(equity):
        if not math.isfinite(v) or v <= 0:
            continue
        peak = max(peak, v)
        worst = max(worst, (peak - v) / peak * 100.0)
    return worst


def strategy_report(ev: PaperEvidence) -> dict:
    pnl = [float(t["pnl_usd"]) for t in ev.trades if isinstance(t.get("pnl_usd"), (int, float)) and math.isfinite(t["pnl_usd"])]
    n = len(pnl)
    rep: dict[str, Any] = {
        "enforced": False,
        "note": "REPORT ONLY. No numeric profitability criterion is approved or enforced. Operational readiness does not "
                "establish profitability, and a small trade count cannot.",
        "closed_trades": n, "max_drawdown_pct": round(_max_drawdown_pct(ev.equity), 3),
        "out_of_sample": "not evaluated (needs pre-registered frozen parameters and a holdout; cannot be read from a ledger)",
    }
    if n == 0:
        return rep | {"net_pnl_usd": 0.0, "mean_pnl_usd": None, "ci95_mean_pnl_usd": None, "ci_excludes_zero_above": False,
                      "win_rate": None, "top_trade_share_of_gains": None, "fees_usd": 0.0}
    mean = sum(pnl) / n
    rng = random.Random(BOOTSTRAP_SEED)
    means = sorted(sum(rng.choice(pnl) for _ in range(n)) / n for _ in range(BOOTSTRAP_RESAMPLES))
    lo, hi = means[int(0.025 * BOOTSTRAP_RESAMPLES)], means[int(0.975 * BOOTSTRAP_RESAMPLES) - 1]
    gains = [x for x in pnl if x > 0]
    return rep | {
        "net_pnl_usd": round(sum(pnl), 4), "mean_pnl_usd": round(mean, 4), "ci95_mean_pnl_usd": [round(lo, 4), round(hi, 4)],
        "ci_excludes_zero_above": lo > 0, "win_rate": round(len(gains) / n, 3),
        "top_trade_share_of_gains": round(max(gains) / sum(gains), 3) if gains else None,
        "fees_usd": round(sum(float(t.get("fees_usd") or 0.0) for t in ev.trades), 4),
    }


# ---- the enforced check ---------------------------------------------------------------------------------------------

@dataclass
class ReadinessResult:
    ok: bool
    reasons: list[str]
    trades: int
    days: float
    coverage: float
    source: str
    operating: OperatingReport
    strategy: dict

    def summary(self) -> dict:
        """What goes into the activation record and the audit log (no secrets, bounded size)."""
        return {"source": self.source, "closed_trades": self.trades, "span_days": round(self.days, 2),
                "coverage": round(self.coverage, 4), "outage_count": self.operating.outage_count,
                "longest_gap_sec": round(self.operating.longest_gap_sec, 1), "operating": self.operating.to_dict(5),
                "strategy_report": self.strategy}


def evaluate(settings, ev: PaperEvidence, *, now: float | None = None) -> ReadinessResult:
    """Apply the EXISTING thresholds to gap-measured evidence."""
    now = time.time() if now is None else now
    e = settings.engine
    op = operating_report(ev, now=now, interval_sec=e.equity_snapshot_interval_sec, tolerance_intervals=e.paper_gap_tolerance_intervals)
    trades = len(ev.trades)
    reasons: list[str] = []
    if trades < e.min_paper_trades_before_live:
        reasons.append(f"{trades} closed paper trades (need {e.min_paper_trades_before_live})")
    if op.span_days < e.min_paper_days_before_live:
        reasons.append(f"{op.span_days:.1f} days of paper snapshots (need {e.min_paper_days_before_live:g})")
    if op.coverage < e.min_paper_coverage_before_live:
        reasons.append(f"engine covered {op.coverage:.0%} of the period, {op.outage_count} outage(s), longest "
                       f"{op.longest_gap_sec / 3600:.1f} h (need {e.min_paper_coverage_before_live:.0%})")
    return ReadinessResult(not reasons, reasons, trades, op.span_days, op.coverage, ev.source, op, strategy_report(ev))
