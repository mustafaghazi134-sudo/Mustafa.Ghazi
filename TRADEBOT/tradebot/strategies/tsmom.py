"""Time-series momentum (TSMOM), daily-evaluated. The only candidate that survived walk-forward
validation after costs in research_lab (see research_lab/RESULTS.md).

Rule: once per UTC day (on the 1h bar that opens at 00:00), go long if the trailing `lookback_days`
return is positive; exit when it is negative. A wide disaster stop guards against a crash between daily
checks. Optional regime filter: require close above a long EMA. No averaging down, no pyramiding.
"""
from __future__ import annotations

import datetime as dt

from ..models import Candle, Position, Side, Signal
from . import indicators as ind
from .base import Strategy


def _bars_per_day(candles: list[Candle]) -> int:
    if len(candles) < 2:
        return 24
    step = max(candles[1].ts - candles[0].ts, 60.0)
    return max(1, int(round(86400 / step)))


class TsmomStrategy(Strategy):
    name = "tsmom"
    # trail_pct > 0 adds a trailing stop from the high-water mark (research_lab "tsmom_trail" variant);
    # ema_filter_days > 0 requires close above a long EMA (research_lab "tsmom_ema_filter" variant).
    default_params = {"lookback_days": 30, "stop_pct": 25.0, "trail_pct": 0.0, "ema_filter_days": 0, "eval_hour_utc": 0}

    @property
    def min_bars(self) -> int:
        return int(max(self.params["lookback_days"], self.params["ema_filter_days"]) * 24) + 2

    def compute(self, candles: list[Candle]) -> dict[str, list[float]]:
        c = ind.closes(candles)
        bpd = _bars_per_day(candles)
        lb = int(self.params["lookback_days"] * bpd)
        mom = [float("nan")] * len(c)
        for i in range(lb, len(c)):
            mom[i] = c[i] / c[i - lb] - 1 if c[i - lb] > 0 else float("nan")
        ema_days = int(self.params["ema_filter_days"])
        e = ind.ema(c, ema_days * bpd) if ema_days > 0 else [float("nan")] * len(c)
        daily = [1.0 if (bpd == 1 or dt.datetime.fromtimestamp(x.ts, dt.UTC).hour == int(self.params["eval_hour_utc"])) else 0.0 for x in candles]
        return {"c": c, "mom": mom, "ema": e, "daily": daily}

    def entry_at(self, symbol: str, candles: list[Candle], a: dict[str, list[float]], i: int) -> Signal | None:
        if a["daily"][i] != 1.0 or ind.isnan(a["mom"][i]):
            return None
        if a["mom"][i] <= 0:
            return None
        if int(self.params["ema_filter_days"]) > 0 and (ind.isnan(a["ema"][i]) or a["c"][i] <= a["ema"][i]):
            return None
        price = a["c"][i]
        trail = float(self.params["trail_pct"]) if self.params.get("trail_pct") else None
        return Signal(symbol=symbol, side=Side.BUY, strategy=self.name, strength=float(min(1.0, 0.5 + a["mom"][i])),
                      reason=f"{self.params['lookback_days']}d momentum {a['mom'][i]*100:+.1f}% > 0 at daily check",
                      price=float(price), stop_loss=float(price * (1 - self.params["stop_pct"] / 100)) if self.params["stop_pct"] else None,
                      trailing_stop_pct=trail, ts=candles[i].ts)

    def exit_at(self, candles: list[Candle], a: dict[str, list[float]], i: int, position: Position) -> str | None:
        if a["daily"][i] != 1.0 or ind.isnan(a["mom"][i]):
            return None
        if a["mom"][i] < 0:
            return "momentum_negative"
        if int(self.params["ema_filter_days"]) > 0 and not ind.isnan(a["ema"][i]) and a["c"][i] < a["ema"][i]:
            return "below_regime_ema"
        return None
