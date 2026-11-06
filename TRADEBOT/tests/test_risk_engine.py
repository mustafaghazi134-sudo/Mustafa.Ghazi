import pytest

from tradebot.models import OrderRequest, Position, Side
from tradebot.risk.engine import MarketState, PortfolioState


def pf(clock, **kw) -> PortfolioState:
    base = dict(equity_usd=1000.0, cash_usd=1000.0, positions={}, trades_today=0, realized_pnl_today_usd=0.0,
                consecutive_losses=0, last_loss_ts=None, peak_equity_usd=1000.0, day_start_equity_usd=1000.0)
    base.update(kw)
    return PortfolioState(**base)


def mkt(clock, **kw) -> MarketState:
    base = dict(price=100.0, liquidity_usd=1_000_000, volume_24h_usd=1_000_000, data_ts=clock())
    base.update(kw)
    return MarketState(**base)


def buy(notional=50.0, symbol="SOL") -> OrderRequest:
    return OrderRequest(symbol=symbol, side=Side.BUY, notional_usd=notional, strategy="t")


def test_approves_and_mints_verifiable_token(risk, clock):
    o = buy(50)
    d = risk.evaluate(o, pf(clock), mkt(clock))
    assert d.approved and d.approval is not None
    assert d.adjusted_notional_usd == pytest.approx(50)
    risk.verify(d.approval, o, d.adjusted_qty)  # no raise


def test_token_bound_to_order_and_qty(risk, clock):
    o = buy(50)
    d = risk.evaluate(o, pf(clock), mkt(clock))
    other = buy(50)
    with pytest.raises(PermissionError):
        risk.verify(d.approval, other, d.adjusted_qty)
    with pytest.raises(PermissionError):
        risk.verify(d.approval, o, d.adjusted_qty * 2)


def test_token_expires(risk, clock):
    o = buy(50)
    d = risk.evaluate(o, pf(clock), mkt(clock))
    clock.advance(31)
    with pytest.raises(PermissionError):
        risk.verify(d.approval, o, d.adjusted_qty)


def test_forged_token_rejected(risk, clock):
    from dataclasses import replace
    o = buy(50)
    d = risk.evaluate(o, pf(clock), mkt(clock))
    forged = replace(d.approval, token="0" * 64)
    with pytest.raises(PermissionError):
        risk.verify(forged, o, d.adjusted_qty)


def test_sizes_down_to_position_cap(risk, clock):
    d = risk.evaluate(buy(900), pf(clock), mkt(clock))
    assert d.approved
    assert d.adjusted_notional_usd == pytest.approx(100)  # 10% of 1000
    assert "sized down" in d.reasons[0]


def test_rejects_below_minimum_after_sizing(risk, clock):
    d = risk.evaluate(buy(50), pf(clock, cash_usd=3.0), mkt(clock))
    assert not d.approved


def test_total_exposure_cap(risk, clock):
    positions = {s: Position(symbol=s, qty=1, avg_cost=240, opened_ts=0) for s in ("A", "B")}  # $480 exposure
    d = risk.evaluate(buy(100, "C"), pf(clock, cash_usd=520, positions=positions), mkt(clock))
    assert d.approved and d.adjusted_notional_usd == pytest.approx(20)  # 50% of 1000 - 480


def test_rejections(risk, clock):
    assert not risk.evaluate(buy(50), pf(clock), mkt(clock, liquidity_usd=100)).approved
    assert not risk.evaluate(buy(50), pf(clock), mkt(clock, volume_24h_usd=100)).approved
    assert not risk.evaluate(buy(50), pf(clock), mkt(clock, expected_slippage_bps=500)).approved
    assert not risk.evaluate(buy(50), pf(clock, trades_today=5), mkt(clock)).approved
    assert not risk.evaluate(buy(50), pf(clock, consecutive_losses=3, last_loss_ts=clock() - 700), mkt(clock)).approved
    assert not risk.evaluate(buy(50), pf(clock, last_loss_ts=clock() - 10), mkt(clock)).approved
    assert not risk.evaluate(buy(50), pf(clock), mkt(clock, data_ts=clock() - 1000)).approved
    many = {s: Position(symbol=s, qty=1, avg_cost=10, opened_ts=0) for s in "ABC"}
    assert not risk.evaluate(buy(50, "D"), pf(clock, positions=many), mkt(clock)).approved
    held = {"SOL": Position(symbol="SOL", qty=1, avg_cost=10, opened_ts=0)}
    assert not risk.evaluate(buy(50, "SOL"), pf(clock, positions=held), mkt(clock)).approved


def test_allow_and_deny_lists(limits, kill, clock):
    from tradebot.risk.engine import RiskEngine
    r = RiskEngine(limits.model_copy(update={"denylist": ["BONK"]}), kill, clock)
    assert not r.evaluate(buy(50, "BONK"), pf(clock), mkt(clock)).approved
    r = RiskEngine(limits.model_copy(update={"allowlist": ["SOL"]}), kill, clock)
    assert not r.evaluate(buy(50, "JUP"), pf(clock), mkt(clock)).approved
    assert r.evaluate(buy(50, "SOL"), pf(clock), mkt(clock)).approved


def test_kill_switch_blocks_buys_but_allows_reduce_only_exit(risk, kill, clock):
    kill.trip("test")
    assert not risk.evaluate(buy(50), pf(clock), mkt(clock)).approved
    pos = {"SOL": Position(symbol="SOL", qty=2.0, avg_cost=90, opened_ts=0)}
    exit_order = OrderRequest(symbol="SOL", side=Side.SELL, notional_usd=200, qty=2.0, reduce_only=True)
    d = risk.evaluate(exit_order, pf(clock, positions=pos), mkt(clock))
    assert d.approved and d.adjusted_qty == pytest.approx(2.0)


def test_non_reduce_only_sell_rejected(risk, clock):
    o = OrderRequest(symbol="SOL", side=Side.SELL, notional_usd=100, qty=1.0, reduce_only=False)
    assert not risk.evaluate(o, pf(clock), mkt(clock)).approved


def test_drawdown_trips_kill(risk, kill, clock):
    risk.on_equity(pf(clock, equity_usd=890, peak_equity_usd=1000, day_start_equity_usd=890))
    assert kill.is_tripped() and "drawdown" in kill.reason()


def test_daily_loss_halts_until_next_day(risk, kill, clock):
    risk.on_equity(pf(clock, equity_usd=965, day_start_equity_usd=1000))
    assert not kill.is_tripped()
    assert risk.is_daily_halted()
    assert not risk.evaluate(buy(50), pf(clock, equity_usd=965), mkt(clock)).approved
    clock.advance(86400)
    assert not risk.is_daily_halted()
    assert risk.evaluate(buy(50), pf(clock), mkt(clock)).approved


def test_daily_loss_kill_mode(limits, kill, clock):
    from tradebot.risk.engine import RiskEngine
    r = RiskEngine(limits.model_copy(update={"daily_loss_action": "kill"}), kill, clock)
    r.on_equity(pf(clock, equity_usd=960, day_start_equity_usd=1000))
    assert kill.is_tripped()


def test_stale_data_and_error_streak_trip_kill(risk, kill, clock):
    risk.on_data(clock() - 301)
    assert kill.is_tripped()
    kill.reset()
    for _ in range(5):
        risk.on_execution_error("boom")
    assert kill.is_tripped()


def test_limits_are_private_copy(limits, kill, clock):
    from tradebot.risk.engine import RiskEngine
    r = RiskEngine(limits, kill, clock)
    limits.max_position_pct = 100.0   # external mutation must not affect the engine
    d = r.evaluate(buy(900), pf(clock), mkt(clock))
    assert d.adjusted_notional_usd == pytest.approx(100)


def test_loss_streak_pause_releases_after_window(risk, clock):
    streak = dict(consecutive_losses=3, last_loss_ts=clock() - 700)   # past the 600s per-loss cooldown
    assert not risk.evaluate(buy(50), pf(clock, **streak), mkt(clock)).approved
    clock.advance(14400)                                                # past loss_streak_pause_sec
    assert risk.evaluate(buy(50), pf(clock, **streak), mkt(clock)).approved
