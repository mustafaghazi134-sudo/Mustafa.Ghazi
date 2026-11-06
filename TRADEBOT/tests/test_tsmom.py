import datetime as dt

from tradebot.models import Candle, Position
from tradebot.strategies.registry import build_strategy


def _hourly(prices):
    start = dt.datetime(2024, 1, 1, tzinfo=dt.UTC).timestamp()
    return [Candle(ts=start + i * 3600, open=p, high=p * 1.001, low=p * 0.999, close=p, volume=1.0) for i, p in enumerate(prices)]


def test_tsmom_enters_only_at_daily_check_when_momentum_positive():
    s = build_strategy("tsmom", {"lookback_days": 2})
    up = _hourly([100 + i * 0.5 for i in range(24 * 4 + 1)])     # 4 days up
    pre = s.precompute("X", up)
    sigs = [(i, pre.entry(i)) for i in range(len(up))]
    fired = [i for i, sg in sigs if sg is not None]
    assert fired, "expected entries"
    assert all(dt.datetime.fromtimestamp(up[i].ts, dt.UTC).hour == 0 for i in fired), "entries only at the 00:00 UTC bar"
    sig = pre.entry(fired[-1])
    assert sig.stop_loss == up[fired[-1]].close * 0.75 and "momentum" in sig.reason


def test_tsmom_exits_when_momentum_turns_negative_and_never_averages_down():
    s = build_strategy("tsmom", {"lookback_days": 2})
    prices = [100 + i * 0.5 for i in range(24 * 3)] + [136 - i * 0.6 for i in range(24 * 3)]
    c = _hourly(prices)
    pre = s.precompute("X", c)
    pos = Position(symbol="X", qty=1, avg_cost=120, opened_ts=c[60].ts)
    exits = [i for i in range(len(c)) if pre.exit(i, pos)]
    assert exits and all(dt.datetime.fromtimestamp(c[i].ts, dt.UTC).hour == 0 for i in exits)
    assert pre.exit(exits[0], pos) == "momentum_negative"
    # while momentum is negative there are no entry signals (so nothing can be added to a losing position)
    assert all(pre.entry(i) is None for i in exits)


def test_tsmom_ema_filter_blocks_entries_below_regime():
    s = build_strategy("tsmom", {"lookback_days": 2, "ema_filter_days": 3})
    # strong 3-day downtrend then a small 2-day bounce: momentum turns positive but price stays below the 3-day EMA
    prices = [200 - i * 1.0 for i in range(24 * 3)] + [128 + i * 0.1 for i in range(24 * 2 + 1)]
    c = _hourly(prices)
    pre = s.precompute("X", c)
    assert all(pre.entry(i) is None for i in range(len(c)))


def test_tsmom_trailing_variant_sets_trailing_stop():
    s = build_strategy("tsmom", {"lookback_days": 2, "trail_pct": 20.0, "stop_pct": 0})
    up = _hourly([100 + i * 0.5 for i in range(24 * 4 + 1)])
    pre = s.precompute("X", up)
    sig = next(pre.entry(i) for i in range(len(up)) if pre.entry(i) is not None)
    assert sig.trailing_stop_pct == 20.0 and sig.stop_loss is None


def test_tsmom_fires_on_4h_bars_with_1000_bar_history():
    # Regression: the requirement must scale with the bar size (30d*6 = 180 bars at 4h, not 720).
    s = build_strategy("tsmom", {"lookback_days": 30, "ema_filter_days": 100})
    start = dt.datetime(2024, 1, 1, tzinfo=dt.UTC).timestamp()
    c = [Candle(ts=start + i * 14400, open=100 + i * 0.05, high=100.2 + i * 0.05, low=99.9 + i * 0.05, close=100.1 + i * 0.05, volume=1.0) for i in range(1000)]
    assert s.required_bars(c) == 602
    pre = s.precompute("X", c)
    assert any(pre.entry(i) is not None for i in range(len(c)))
