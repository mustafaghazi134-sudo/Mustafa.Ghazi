"""Replay historical candles from CSV files: <csv_dir>/<SYMBOL>_<interval>.csv with columns ts,open,high,low,close,volume."""
from __future__ import annotations

import csv
from pathlib import Path

from ..models import Candle, Ticker, TokenInfo
from .base import MarketDataProvider, interval_seconds


def read_candles_csv(path: Path) -> list[Candle]:
    out: list[Candle] = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            ts = float(row["ts"])
            if ts > 1e12:  # milliseconds
                ts /= 1000.0
            out.append(Candle(ts=ts, open=float(row["open"]), high=float(row["high"]), low=float(row["low"]),
                              close=float(row["close"]), volume=float(row.get("volume") or 0.0)))
    out.sort(key=lambda c: c.ts)
    return out


def write_candles_csv(path: Path, candles: list[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([int(c.ts), c.open, c.high, c.low, c.close, c.volume])


class CsvReplayProvider(MarketDataProvider):
    def __init__(self, csv_dir: str | Path, symbols: list[str], interval: str = "1h", history: int = 400):
        self.csv_dir = Path(csv_dir)
        self.interval = interval
        self.interval_sec = interval_seconds(interval)
        self.history = history
        self._series: dict[str, list[Candle]] = {}
        for s in symbols:
            p = self.csv_dir / f"{s}_{interval}.csv"
            if p.exists():
                self._series[s] = read_candles_csv(p)
        if not self._series:
            raise FileNotFoundError(f"no CSV candles found in {self.csv_dir} for {symbols} ({interval})")
        self.symbols = list(self._series)
        self.total_bars = min(len(v) for v in self._series.values())
        self.cursor = min(history, self.total_bars)

    @property
    def is_replay(self) -> bool:
        return True

    @property
    def exhausted(self) -> bool:
        return self.cursor >= self.total_bars

    @property
    def now_ts(self) -> float:
        s = self.symbols[0]
        idx = min(self.cursor, len(self._series[s])) - 1
        return self._series[s][idx].ts + self.interval_sec

    def advance(self) -> None:
        if self.cursor < self.total_bars:
            self.cursor += 1

    def full_series(self, symbol: str) -> list[Candle]:
        return list(self._series[symbol])

    def get_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        series = self._series[symbol]
        end = min(self.cursor, len(series))
        return list(series[max(0, end - limit):end])

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
        return [TokenInfo(symbol=s, mint=f"CSV{s}", decimals=6) for s in self.symbols[:max_symbols]]
