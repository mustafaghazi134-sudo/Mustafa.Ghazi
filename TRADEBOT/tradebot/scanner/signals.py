"""Deterministic short-horizon signals over 1-minute bars built from polled prices. Pure Python, no I/O.

Each signal returns a Candidate with an *expected favourable move* in bps and a holding horizon. The expected move is
deliberately a mechanical estimate (fraction of the observed dislocation / range), never a tuned forecast; the
scanner's job is to measure whether such candidates survive real round-trip costs, not to assert that they do.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math


@dataclass
class Bar:
    ts: float        # bar open, unix seconds (aligned to 60 s)
    open: float
    high: float
    low: float
    close: float
    n: int = 1       # polled prices in the bar


class BarBuilder:
    """Aggregates polled prices into 1-minute bars; keeps the last `keep` completed bars per symbol."""

    def __init__(self, keep: int = 240):
        self.keep = keep
        self._cur: dict[str, Bar] = {}
        self._done: dict[str, list[Bar]] = {}

    def add(self, symbol: str, ts: float, price: float) -> Bar | None:
        """Returns the bar that just COMPLETED, if any."""
        start = math.floor(ts / 60) * 60
        cur = self._cur.get(symbol)
        completed = None
        if cur is None:
            self._cur[symbol] = Bar(start, price, price, price, price)
        elif cur.ts != start:
            completed = cur
            self._done.setdefault(symbol, []).append(cur)
            if len(self._done[symbol]) > self.keep:
                del self._done[symbol][: len(self._done[symbol]) - self.keep]
            self._cur[symbol] = Bar(start, price, price, price, price)
        else:
            cur.high = max(cur.high, price); cur.low = min(cur.low, price); cur.close = price; cur.n += 1
        return completed

    def bars(self, symbol: str) -> list[Bar]:
        return list(self._done.get(symbol, []))

    def last_price(self, symbol: str) -> float | None:
        c = self._cur.get(symbol)
        return c.close if c else None


@dataclass
class Candidate:
    symbol: str
    signal: str
    expected_move_bps: float
    horizon_sec: int
    detail: dict = field(default_factory=dict)


def basis_reversion(symbol: str, venue_price: float, reference_price: float, min_gap_bps: float = 30.0,
                    capture: float = 0.5, horizon_sec: int = 300) -> Candidate | None:
    """Jupiter price below the Binance reference by more than min_gap_bps: expect partial convergence.
    Only the BUY side is tradable for a spot wallet, so a venue price ABOVE reference is not a candidate."""
    if venue_price <= 0 or reference_price <= 0:
        return None
    gap = (reference_price / venue_price - 1) * 1e4
    if gap < min_gap_bps:
        return None
    return Candidate(symbol, "basis_reversion", round(gap * capture, 1), horizon_sec,
                     {"gap_bps": round(gap, 1), "venue": venue_price, "reference": reference_price})


def breakout_5m(symbol: str, bars: list[Bar], lookback: int = 30, confirm_bps: float = 10.0,
                capture: float = 0.5, horizon_sec: int = 300) -> Candidate | None:
    """Close breaks above the prior `lookback`-minute high by confirm_bps with positive 5-minute momentum.
    Expected move = capture x the average 5-minute true range of the lookback window."""
    if len(bars) < lookback + 6:
        return None
    window = bars[-lookback - 1:-1]
    hi = max(b.high for b in window)
    last = bars[-1]
    if last.close <= hi * (1 + confirm_bps / 1e4):
        return None
    if last.close <= bars[-6].close:
        return None
    ranges = [(max(b.high for b in bars[i:i + 5]) - min(b.low for b in bars[i:i + 5])) / bars[i].close
              for i in range(len(bars) - lookback - 1, len(bars) - 5, 5)]
    if not ranges:
        return None
    exp = sum(ranges) / len(ranges) * 1e4 * capture
    return Candidate(symbol, "breakout_5m", round(exp, 1), horizon_sec, {"prior_high": hi, "close": last.close})


def dip_reversion_1m(symbol: str, bars: list[Bar], lookback: int = 20, z_min: float = 2.5,
                     capture: float = 0.5, horizon_sec: int = 180) -> Candidate | None:
    """One-minute return more than z_min standard deviations below the lookback mean: expect partial snap-back.
    This is the classic micro mean-reversion candidate and the one most exposed to adverse selection."""
    if len(bars) < lookback + 2:
        return None
    rets = [(bars[i].close / bars[i - 1].close - 1) for i in range(len(bars) - lookback, len(bars))]
    mu = sum(rets[:-1]) / (len(rets) - 1)
    var = sum((r - mu) ** 2 for r in rets[:-1]) / max(len(rets) - 2, 1)
    sd = math.sqrt(var)
    if sd <= 0:
        return None
    z = (rets[-1] - mu) / sd
    if z > -z_min:
        return None
    return Candidate(symbol, "dip_reversion_1m", round(abs(rets[-1]) * 1e4 * capture, 1), horizon_sec,
                     {"z": round(z, 2), "ret_1m_bps": round(rets[-1] * 1e4, 1)})


def evaluate_all(symbol: str, bars: list[Bar], venue_price: float | None, reference_price: float | None) -> list[Candidate]:
    out: list[Candidate] = []
    if venue_price and reference_price:
        c = basis_reversion(symbol, venue_price, reference_price)
        if c:
            out.append(c)
    for fn in (breakout_5m, dip_reversion_1m):
        c = fn(symbol, bars)
        if c:
            out.append(c)
    return out
