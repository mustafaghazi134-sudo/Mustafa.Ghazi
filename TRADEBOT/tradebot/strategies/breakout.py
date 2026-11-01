"""Donchian channel breakout with volume confirmation and ATR stop."""
from __future__ import annotations

from ..models import Candle, Position, Side, Signal
from . import indicators as ind
from .base import Strategy


class BreakoutStrategy(Strategy):
    name = "breakout"
    default_params = {"channel": 20, "volume_mult": 1.5, "volume_period": 20, "atr_period": 14, "atr_mult": 2.0,
                      "tp_mult": 3.0}

    @property
    def min_bars(self) -> int:
        return max(int(self.params["channel"]), int(self.params["volume_period"]), int(self.params["atr_period"])) + 5

    def compute(self, candles: list[Candle]) -> dict[str, list[float]]:
        c = ind.closes(candles)
        v = ind.volumes(candles)
        up, _ = ind.donchian(candles, int(self.params["channel"]))
        _, dn_exit = ind.donchian(candles, max(5, int(self.params["channel"]) // 2))
        return {"c": c, "v": v, "up": up, "dn_exit": dn_exit, "vavg": ind.sma(v, int(self.params["volume_period"])),
                "atr": ind.atr(candles, int(self.params["atr_period"]))}

    def entry_at(self, symbol: str, candles: list[Candle], a: dict[str, list[float]], i: int) -> Signal | None:
        c, v, up, vavg, atr = a["c"], a["v"], a["up"], a["vavg"], a["atr"]
        if ind.isnan(up[i], vavg[i], atr[i]) or vavg[i] <= 0:
            return None
        if c[i] > up[i] and v[i] >= self.params["volume_mult"] * vavg[i]:
            return Signal(symbol=symbol, side=Side.BUY, strategy=self.name, strength=float(min(1.0, 0.5 + (c[i] / up[i] - 1) * 20)),
                          reason=f"close {c[i]:.6g} broke {self.params['channel']}-bar high {up[i]:.6g} on {v[i]/vavg[i]:.1f}x volume",
                          price=float(c[i]), stop_loss=float(c[i] - self.params["atr_mult"] * atr[i]),
                          take_profit=float(c[i] + self.params["tp_mult"] * atr[i]),
                          trailing_stop_pct=float(self.params["atr_mult"] * atr[i] / c[i] * 100), ts=candles[i].ts)
        return None

    def exit_at(self, candles: list[Candle], a: dict[str, list[float]], i: int, position: Position) -> str | None:
        dn = a["dn_exit"][i]
        if not ind.isnan(dn) and a["c"][i] < dn:
            return "channel_breakdown"
        return None
