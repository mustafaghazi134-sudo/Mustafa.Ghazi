"""Exit logic shared by the live engine and the backtester."""
from __future__ import annotations

from ..models import Candle, Position
from ..strategies.base import Strategy


def check_exit(pos: Position, price: float, now: float, candles: list[Candle] | None = None,
               strategy: Strategy | None = None) -> str | None:
    """Update trailing high-water mark and return an exit reason, or None to keep holding."""
    if price > pos.high_water:
        pos.high_water = price
    stop = pos.stop_loss if pos.stop_loss is not None else None
    if pos.trailing_stop_pct is not None and pos.high_water > 0:
        trail = pos.high_water * (1 - pos.trailing_stop_pct / 100)
        stop = max(stop, trail) if stop is not None else trail
    if stop is not None and price <= stop:
        return "trailing_stop" if (pos.stop_loss is None or stop > pos.stop_loss) else "stop_loss"
    if pos.take_profit is not None and price >= pos.take_profit:
        return "take_profit"
    if pos.max_hold_sec is not None and now - pos.opened_ts >= pos.max_hold_sec:
        return "time_stop"
    if strategy is not None and candles:
        reason = strategy.exit_signal(pos.symbol, candles, pos)
        if reason:
            return f"strategy:{reason}"
    return None
