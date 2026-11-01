from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import numpy as np

from ..models import Candle, Position, Signal


class Precomputed:
    """Indicator arrays for a full candle series; entry(i)/exit(i) evaluate bar i in O(1)."""

    def __init__(self, strategy: "Strategy", symbol: str, candles: list[Candle]):
        self.strategy = strategy
        self.symbol = symbol
        self.candles = candles
        self.arrays: dict[str, np.ndarray] = strategy.compute(candles) if len(candles) >= strategy.min_bars else {}

    def entry(self, i: int) -> Signal | None:
        if not self.arrays or i < self.strategy.min_bars - 1:
            return None
        return self.strategy.entry_at(self.symbol, self.candles, self.arrays, i)

    def exit(self, i: int, position: Position) -> str | None:
        if not self.arrays or i < self.strategy.min_bars - 1:
            return None
        return self.strategy.exit_at(self.candles, self.arrays, i, position)


class Strategy(ABC):
    name: str = "base"
    default_params: dict[str, Any] = {}

    def __init__(self, params: dict[str, Any] | None = None):
        self.params: dict[str, Any] = {**self.default_params, **(params or {})}

    @property
    def min_bars(self) -> int:
        return 50

    # ---- vectorised core (implemented by subclasses) --------------------------------------------
    @abstractmethod
    def compute(self, candles: list[Candle]) -> dict[str, np.ndarray]:
        """Indicator arrays aligned with candles."""

    @abstractmethod
    def entry_at(self, symbol: str, candles: list[Candle], a: dict[str, np.ndarray], i: int) -> Signal | None: ...

    def exit_at(self, candles: list[Candle], a: dict[str, np.ndarray], i: int, position: Position) -> str | None:
        return None

    # ---- convenience API used by the live engine ----------------------------------------------
    def precompute(self, symbol: str, candles: list[Candle]) -> Precomputed:
        return Precomputed(self, symbol, candles)

    def entry_signal(self, symbol: str, candles: list[Candle]) -> Signal | None:
        """BUY signal on the latest closed bar, or None."""
        if len(candles) < self.min_bars:
            return None
        return self.entry_at(symbol, candles, self.compute(candles), len(candles) - 1)

    def exit_signal(self, symbol: str, candles: list[Candle], position: Position) -> str | None:
        if len(candles) < self.min_bars:
            return None
        return self.exit_at(candles, self.compute(candles), len(candles) - 1, position)

    def describe(self) -> str:
        return f"{self.name}({', '.join(f'{k}={v}' for k, v in sorted(self.params.items()))})"
