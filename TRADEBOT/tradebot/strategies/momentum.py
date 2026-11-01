"""EMA crossover trend-following with a slow trend filter and ATR-based stop."""
from __future__ import annotations

from ..models import Candle, Position, Side, Signal
from . import indicators as ind
from .base import Strategy


class MomentumStrategy(Strategy):
    name = "momentum"
    default_params = {"fast": 12, "slow": 26, "trend": 100, "atr_period": 14, "atr_mult": 2.5, "tp_mult": 4.0}

    @property
    def min_bars(self) -> int:
        return int(self.params["trend"]) + 5

    def compute(self, candles: list[Candle]) -> dict[str, list[float]]:
        c = ind.closes(candles)
        return {"c": c, "fast": ind.ema(c, int(self.params["fast"])), "slow": ind.ema(c, int(self.params["slow"])),
                "trend": ind.ema(c, int(self.params["trend"])), "atr": ind.atr(candles, int(self.params["atr_period"]))}

    def entry_at(self, symbol: str, candles: list[Candle], a: dict[str, list[float]], i: int) -> Signal | None:
        if i < 1:
            return None
        c, fast, slow, trend, atr = a["c"], a["fast"], a["slow"], a["trend"], a["atr"]
        if ind.isnan(fast[i], slow[i], trend[i], atr[i], fast[i - 1], slow[i - 1]):
            return None
        if fast[i - 1] <= slow[i - 1] and fast[i] > slow[i] and c[i] > trend[i]:
            spread = (fast[i] - slow[i]) / c[i]
            return Signal(symbol=symbol, side=Side.BUY, strategy=self.name, strength=float(min(1.0, 0.5 + spread * 50)),
                          reason=f"EMA{self.params['fast']}>EMA{self.params['slow']} cross above EMA{self.params['trend']}",
                          price=float(c[i]), stop_loss=float(c[i] - self.params["atr_mult"] * atr[i]),
                          take_profit=float(c[i] + self.params["tp_mult"] * atr[i]),
                          trailing_stop_pct=float(self.params["atr_mult"] * atr[i] / c[i] * 100), ts=candles[i].ts)
        return None

    def exit_at(self, candles: list[Candle], a: dict[str, list[float]], i: int, position: Position) -> str | None:
        fast, slow = a["fast"][i], a["slow"][i]
        if not ind.isnan(fast, slow) and fast < slow:
            return "ema_cross_down"
        return None
