"""Deterministic synthetic market data (regime-switching GBM) for tests and offline paper runs."""
from __future__ import annotations

import math

import numpy as np

from ..models import Candle, Ticker, TokenInfo
from .base import MarketDataProvider, interval_seconds

_BASE_PRICES = {"SOL": 150.0, "JUP": 0.9, "RAY": 2.5, "BONK": 0.00002, "WIF": 1.8, "PYTH": 0.4, "JTO": 3.0}


def generate_series(seed: int, n: int, start_price: float, interval_sec: int, start_ts: float) -> list[Candle]:
    rng = np.random.default_rng(seed)
    candles: list[Candle] = []
    price = start_price
    regime_left = 0
    mu = 0.0
    sigma = 0.02
    for i in range(n):
        if regime_left <= 0:
            regime_left = int(rng.integers(40, 160))
            mu = float(rng.choice([-0.0015, 0.0, 0.0, 0.002]))
            sigma = float(rng.choice([0.01, 0.02, 0.035]))
        regime_left -= 1
        ret = mu + sigma * rng.standard_normal()
        o = price
        c = o * math.exp(ret)
        wick = abs(rng.standard_normal()) * sigma * o * 0.6
        h = max(o, c) + wick
        lo = max(min(o, c) - wick, 1e-12)
        vol = float(abs(rng.normal(1.0, 0.4)) * 1_000_000 * (1 + 3 * abs(ret) / max(sigma, 1e-9)))
        candles.append(Candle(ts=start_ts + i * interval_sec, open=o, high=h, low=lo, close=c, volume=vol))
        price = c
    return candles


class SyntheticProvider(MarketDataProvider):
    """Pre-generates a long series per symbol and exposes a moving window (replay)."""

    def __init__(self, symbols: list[str], seed: int = 42, interval: str = "1h",
                 history: int = 400, total_bars: int = 3000, start_ts: float | None = None):
        self.symbols = list(symbols)
        self.interval = interval
        self.interval_sec = interval_seconds(interval)
        self.history = history
        self.start_ts = start_ts if start_ts is not None else 1_700_000_000.0
        self._series: dict[str, list[Candle]] = {}
        for i, s in enumerate(self.symbols):
            base = _BASE_PRICES.get(s, 1.0)
            self._series[s] = generate_series(seed * 1000 + i, total_bars, base, self.interval_sec, self.start_ts)
        self.cursor = history  # index of the "current" bar (exclusive end of visible window)
        self.total_bars = total_bars

    @property
    def is_replay(self) -> bool:
        return True

    @property
    def now_ts(self) -> float:
        return self.start_ts + self.cursor * self.interval_sec

    def advance(self) -> None:
        if self.cursor < self.total_bars:
            self.cursor += 1

    @property
    def exhausted(self) -> bool:
        return self.cursor >= self.total_bars

    def full_series(self, symbol: str) -> list[Candle]:
        return list(self._series[symbol])

    def get_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        series = self._series[symbol]
        end = min(self.cursor, len(series))
        start = max(0, end - limit)
        return list(series[start:end])

    def get_tickers(self, symbols: list[str]) -> dict[str, Ticker]:
        out: dict[str, Ticker] = {}
        for s in symbols:
            if s not in self._series:
                continue
            series = self._series[s]
            idx = min(self.cursor, len(series)) - 1
            c = series[idx]
            day_bars = max(1, 86400 // self.interval_sec)
            prev = series[max(0, idx - day_bars)]
            vol = sum(x.volume for x in series[max(0, idx - day_bars + 1): idx + 1]) * c.close
            out[s] = Ticker(symbol=s, price=c.close, ts=self.now_ts, volume_24h_usd=vol,
                            liquidity_usd=5_000_000.0, change_24h_pct=(c.close / prev.close - 1) * 100)
        return out

    def get_universe(self, max_symbols: int) -> list[TokenInfo]:
        return [TokenInfo(symbol=s, mint=f"SYNTH{s}", decimals=6) for s in self.symbols[:max_symbols]]
