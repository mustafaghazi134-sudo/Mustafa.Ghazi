"""Engine package. Import `tradebot.engine.bot` directly for TradingBot/build_bot (avoids import cycles)."""
from .position_manager import check_exit

__all__ = ["check_exit"]
