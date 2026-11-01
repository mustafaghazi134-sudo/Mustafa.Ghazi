from __future__ import annotations

from typing import Any

from ..config import StrategyConfig
from .base import Strategy
from .breakout import BreakoutStrategy
from .mean_reversion import MeanReversionStrategy
from .momentum import MomentumStrategy

STRATEGY_CLASSES: dict[str, type[Strategy]] = {
    MomentumStrategy.name: MomentumStrategy,
    MeanReversionStrategy.name: MeanReversionStrategy,
    BreakoutStrategy.name: BreakoutStrategy,
}

# Search spaces used by the optimizer / research loop.
PARAM_GRIDS: dict[str, dict[str, list[Any]]] = {
    "momentum": {"fast": [8, 12, 20], "slow": [26, 50], "trend": [100, 200], "atr_mult": [2.0, 3.0]},
    "mean_reversion": {"rsi_buy": [25, 30, 35], "rsi_exit": [50, 55, 60], "bb_std": [2.0, 2.5], "stop_pct": [5.0, 8.0]},
    "breakout": {"channel": [20, 40], "volume_mult": [1.2, 1.5, 2.0], "atr_mult": [2.0, 3.0]},
}


def build_strategy(name: str, params: dict[str, Any] | None = None) -> Strategy:
    try:
        return STRATEGY_CLASSES[name](params)
    except KeyError as e:
        raise ValueError(f"unknown strategy {name!r}; known: {sorted(STRATEGY_CLASSES)}") from e


def build_strategies(configs: list[StrategyConfig]) -> list[Strategy]:
    return [build_strategy(c.name, c.params) for c in configs if c.enabled]
