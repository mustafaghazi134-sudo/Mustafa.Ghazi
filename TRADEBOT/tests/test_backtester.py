import pytest

from tradebot.data.synthetic import SyntheticProvider, generate_series
from tradebot.models import Candle
from tradebot.research.backtester import backtest
from tradebot.research.optimizer import optimize, param_grid
from tradebot.strategies.registry import STRATEGY_CLASSES, build_strategy


def test_backtest_runs_all_strategies_and_matches_live_signal_path():
    prov = SyntheticProvider(["SOL"], seed=3, history=400, total_bars=1500)
    candles = prov.full_series("SOL")
    for name in STRATEGY_CLASSES:
        strat = build_strategy(name)
        res = backtest(strat, candles, "SOL")
        assert len(res.equity_curve) == len(candles) - strat.min_bars
        assert "total_return_pct" in res.metrics
        # every closed trade is consistent
        for t in res.trades:
            assert t.exit_ts > t.entry_ts and t.qty > 0
        # precomputed entry at the last bar equals the live-path entry on the window
        pre = strat.precompute("SOL", candles)
        live = strat.entry_signal("SOL", candles)
        assert (pre.entry(len(candles) - 1) is None) == (live is None)


def _bar(i, px, vol=1000.0):
    return Candle(ts=i * 3600, open=px, high=px + 1.5, low=px - 0.5, close=px + 1, volume=vol)


def test_backtest_profits_on_clean_uptrend():
    # Flat/declining prelude (so the EMAs sit on top of each other) then a clean uptrend: the fast EMA must
    # cross above the slow EMA and momentum must make money net of fees.
    prelude = [_bar(i, 120 - 0.2 * i) for i in range(100)]
    trend = [_bar(100 + i, 100 + 0.8 * i, 1000 + (i % 7) * 500) for i in range(300)]
    res = backtest(build_strategy("momentum", {"trend": 50}), prelude + trend, "UP")
    assert res.trades, "expected an entry on the crossover"
    assert res.metrics["total_return_pct"] > 0


def test_stop_loss_is_respected():
    # price collapses after an entry: loss is bounded near the stop, not the full move
    prelude = [_bar(i, 120 - 0.2 * i) for i in range(100)]
    up = [_bar(100 + i, 100 + i) for i in range(40)]
    top = 100 + 40
    down = [Candle(ts=(140 + i) * 3600, open=top - 5 * i, high=top + 1 - 5 * i, low=top - 6 - 5 * i, close=top - 5 - 5 * i, volume=1000) for i in range(25)]
    res = backtest(build_strategy("momentum", {"trend": 50}), prelude + up + down, "X")
    assert res.trades, "expected at least one trade"
    worst = min(t.return_pct for t in res.trades)
    assert worst > -12  # ATR-based stop keeps the single-trade loss bounded


def test_param_grid_and_walk_forward_filter():
    grid = param_grid("momentum", {"fast": [8, 12], "slow": [26], "trend": [100], "atr_mult": [2.0]})
    assert len(grid) == 2
    series = {"A": generate_series(5, 1200, 100.0, 3600, 0.0), "B": generate_series(6, 1200, 2.0, 3600, 0.0)}
    cands = optimize("momentum", series, grid={"fast": [8, 12], "slow": [26], "trend": [100], "atr_mult": [2.0]})
    assert len(cands) == 2
    for c in cands:
        if c.score > -1e8:
            assert c.out_sample["total_return_pct"] > 0 and c.out_sample["trades"] >= 3
