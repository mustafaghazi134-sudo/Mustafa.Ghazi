"""Replay feed: drives the fast engine over REAL historical Binance 1-minute bars, offline.

What is real: every price is a real 1-minute close from the CSV (Binance spot). What is simulated and labelled so:
there is no Jupiter venue offline, so the "venue" price is the Binance close itself (no basis signal can fire), token
facts are fixed to "verified, liquid" for the configured symbols, and quotes are parametric (quoted = price less a
per-leg cost, min-out at the configured slippage), i.e. the same assumption the paper broker uses without a key.
Timestamps are the bar times; the engine's clock follows the replay so stops, horizons and the daily-loss halt
behave as they would in a live session. Nothing here is a profitability claim about live trading.
"""
from __future__ import annotations

import csv
import glob
import gzip
import zipfile
from typing import Iterable

from ..scanner.scanner import USDC, FeedResult

PARAMETRIC_COST_BPS_PER_LEG = 15.0


def _open_rows(path: str) -> Iterable[list[str]]:
    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                with z.open(name) as f:
                    for line in f.read().decode().splitlines():
                        yield line.split(",")
    elif path.endswith(".gz"):
        with gzip.open(path, "rt") as f:
            yield from csv.reader(f)
    else:
        with open(path, newline="") as f:
            yield from csv.reader(f)


def load_bars(files: list[str]) -> list[tuple[float, float, float, float, float]]:
    """-> [(ts_sec, open, high, low, close)] from official 12-column or header CSVs; sorted, de-duplicated."""
    out: dict[float, tuple[float, float, float, float, float]] = {}
    for path in sorted(f for g in files for f in glob.glob(g)):
        for row in _open_rows(path):
            if not row or not row[0].strip().replace(".", "").isdigit():
                continue                       # header or blank
            t = float(row[0])
            t = t / 1e6 if t > 1e15 else (t / 1e3 if t > 1e11 else t)   # us / ms / s
            try:
                o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
            except (ValueError, IndexError):
                # header-style parshant CSV: timestamp, datetime_utc, open, high, low, close, ...
                o, h, l, c = float(row[2]), float(row[3]), float(row[4]), float(row[5])
            out[t] = (t, o, h, l, c)
    return [out[k] for k in sorted(out)]


class ReplayFeed:
    """One tick per 1-minute bar close per symbol. `advance()` moves to the next bar; `now()` is the bar time."""

    def __init__(self, series: dict[str, list[tuple[float, float, float, float, float]]], mints: dict[str, str], decimals: dict[str, int],
                 cost_bps_per_leg: float = PARAMETRIC_COST_BPS_PER_LEG, slippage_bps: int = 50, start_ts: float | None = None):
        self.series = series
        self.mints = mints                      # symbol -> mint
        self.decimals = decimals
        self.cost = cost_bps_per_leg
        self.slippage_bps = slippage_bps
        self.idx = {s: 0 for s in series}
        self.ts = min(v[0][0] for v in series.values())
        if start_ts:
            for s, v in series.items():
                while self.idx[s] < len(v) and v[self.idx[s]][0] < start_ts:
                    self.idx[s] += 1
            self.ts = max(self.ts, start_ts)
        self.order_calls = 0

    # -- clock / stepping ---------------------------------------------------------------------
    def now(self) -> float:
        return self.ts

    def finished(self) -> bool:
        return all(self.idx[s] >= len(v) for s, v in self.series.items())

    def advance(self) -> None:
        self.ts += 60.0
        for s, v in self.series.items():
            while self.idx[s] < len(v) and v[self.idx[s]][0] < self.ts:
                self.idx[s] += 1

    def _price(self, sym: str) -> float | None:
        v = self.series[sym]; i = self.idx[sym]
        bar = v[i] if i < len(v) and v[i][0] == self.ts else (v[i - 1] if i > 0 else None)
        return bar[4] if bar else None          # close of the bar that opened at ts

    # -- Feed protocol --------------------------------------------------------------------------
    def prices(self, mints: list[str]) -> FeedResult:
        data = {}
        for sym, m in self.mints.items():
            p = self._price(sym)
            if p:
                data[m] = {"usdPrice": p, "blockId": int(self.ts / 0.4)}
        return FeedResult(data, 0.0, 200, True, 0)

    def reference_prices(self, pairs: list[str]) -> FeedResult:
        return FeedResult({}, 0.0, 200, True, 0)        # no second venue offline: basis_reversion cannot fire

    def slot(self) -> FeedResult:
        return FeedResult(int(self.ts / 0.4), 0.0, 200, True, 0)

    def token_facts(self, mints: list[str]) -> FeedResult:
        return FeedResult([{"id": m, "symbol": s, "decimals": self.decimals.get(s, 9), "isVerified": True, "organicScore": 99, "liquidity": 1e9,
                            "stats24h": {"buyVolume": 1e9, "sellVolume": 1e9}, "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True}}
                           for s, m in self.mints.items()], 0.0, 200, True, 0)

    def discover(self, category: str, interval: str) -> FeedResult:
        return FeedResult([], 0.0, 200, True, 0)

    def order(self, in_mint: str, out_mint: str, amount_raw: int, slippage_bps: int) -> FeedResult:
        self.order_calls += 1
        c = self.cost / 1e4
        if in_mint == USDC:
            sym = next(s for s, m in self.mints.items() if m == out_mint); px = self._price(sym)
            out = int(amount_raw / 1e6 / px * (1 - c) * 10 ** self.decimals.get(sym, 9))
        else:
            sym = next(s for s, m in self.mints.items() if m == in_mint); px = self._price(sym)
            out = int(amount_raw / 10 ** self.decimals.get(sym, 9) * px * (1 - c) * 1e6)
        return FeedResult({"inAmount": str(amount_raw), "outAmount": str(out), "otherAmountThreshold": str(int(out * (1 - slippage_bps / 1e4))),
                           "priceImpactPct": str(c / 3), "feeBps": 0, "router": "replay-parametric", "mode": "replay", "transaction": None,
                           "routePlan": [{"swapInfo": {"label": "parametric"}}]}, 0.0, 200, True, 0)
