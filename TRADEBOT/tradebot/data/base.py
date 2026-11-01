"""Market data provider interface."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Candle, Ticker, TokenInfo


class StaleDataError(RuntimeError):
    pass


INTERVAL_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}


def interval_seconds(interval: str) -> int:
    try:
        return INTERVAL_SECONDS[interval]
    except KeyError as e:
        raise ValueError(f"unsupported interval {interval!r}") from e


class MarketDataProvider(ABC):
    """All providers return USD-denominated prices."""

    @abstractmethod
    def get_tickers(self, symbols: list[str]) -> dict[str, Ticker]: ...

    @abstractmethod
    def get_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]: ...

    def get_universe(self, max_symbols: int) -> list[TokenInfo]:
        """Optional: discover tradable tokens. Default: none (use configured list)."""
        return []

    def advance(self) -> None:
        """Replay providers step forward one bar; live providers do nothing."""
        return None

    @property
    def is_replay(self) -> bool:
        return False
