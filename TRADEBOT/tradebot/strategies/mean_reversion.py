"""RSI + Bollinger mean reversion: buy oversold re-entries, exit on RSI recovery / mid-band."""
from __future__ import annotations

from ..models import Candle, Position, Side, Signal
from . import indicators as ind
from .base import Strategy


class MeanReversionStrategy(Strategy):
    name = "mean_reversion"
    default_params = {"rsi_period": 14, "rsi_buy": 30, "rsi_exit": 55, "bb_period": 20, "bb_std": 2.0,
                      "stop_pct": 6.0, "max_hold_bars": 48}

    @property
    def min_bars(self) -> int:
        return max(int(self.params["rsi_period"]), int(self.params["bb_period"])) + 5

    def compute(self, candles: list[Candle]) -> dict[str, list[float]]:
        c = ind.closes(candles)
        lower, mid, _ = ind.bollinger(c, int(self.params["bb_period"]), float(self.params["bb_std"]))
        return {"c": c, "rsi": ind.rsi(c, int(self.params["rsi_period"])), "lower": lower, "mid": mid}

    def entry_at(self, symbol: str, candles: list[Candle], a: dict[str, list[float]], i: int) -> Signal | None:
        if i < 1:
            return None
        c, r, lower, mid = a["c"], a["rsi"], a["lower"], a["mid"]
        if ind.isnan(r[i], lower[i], lower[i - 1], mid[i]):
            return None
        # Oversold and closing back above the lower band after being below it: a re-entry, not a falling knife.
        if r[i] < self.params["rsi_buy"] and c[i - 1] < lower[i - 1] and c[i] >= lower[i]:
            bar_sec = candles[i].ts - candles[i - 1].ts if i >= 1 else 3600.0
            return Signal(symbol=symbol, side=Side.BUY, strategy=self.name,
                          strength=float(min(1.0, (self.params["rsi_buy"] - r[i]) / self.params["rsi_buy"] + 0.4)),
                          reason=f"RSI {r[i]:.1f} < {self.params['rsi_buy']} and re-entry above lower band",
                          price=float(c[i]), stop_loss=float(c[i] * (1 - self.params["stop_pct"] / 100)),
                          take_profit=float(mid[i]), max_hold_sec=float(self.params["max_hold_bars"] * bar_sec), ts=candles[i].ts)
        return None

    def exit_at(self, candles: list[Candle], a: dict[str, list[float]], i: int, position: Position) -> str | None:
        r = a["rsi"][i]
        if not ind.isnan(r) and r > self.params["rsi_exit"]:
            return "rsi_recovered"
        return None
