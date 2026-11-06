import pytest

from tradebot.data.synthetic import SyntheticProvider
from tradebot.execution.paper import PaperBroker
from tradebot.models import OrderRequest, Side
from tradebot.risk.engine import MarketState, PortfolioState


def _state(prov, clock):
    t = prov.get_tickers(["SOL"])["SOL"]
    pf = PortfolioState(1000, 1000, {}, 0, 0, 0, None, 1000, 1000)
    return t, pf, MarketState(t.price, t.liquidity_usd, t.volume_24h_usd, clock())


def test_buy_fill_has_fee_and_slippage_and_requires_token(risk, clock):
    prov = SyntheticProvider(["SOL"], seed=1, history=100, total_bars=200)
    clock.t = prov.now_ts
    broker = PaperBroker(risk, prov, fee_bps=30, slippage_bps=20, clock=clock)
    t, pf, mkt = _state(prov, clock)
    o = OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=100)
    d = risk.evaluate(o, pf, mkt)
    fill = broker.execute(o, d.adjusted_qty, d.approval)
    assert fill.side == Side.BUY and fill.notional_usd == pytest.approx(100)
    assert fill.fee_usd == pytest.approx(0.30)
    assert fill.price > t.price and fill.price <= t.price * 1.002
    assert fill.qty == pytest.approx((100 - 0.30) / fill.price)
    # a second execution with the same (now mismatched) approval must fail
    o2 = OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=100)
    with pytest.raises(PermissionError):
        broker.execute(o2, d.adjusted_qty, d.approval)


def test_execute_without_risk_approval_impossible(risk, clock):
    prov = SyntheticProvider(["SOL"], seed=1, history=100, total_bars=200)
    broker = PaperBroker(risk, prov, clock=clock)
    o = OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=100)
    with pytest.raises((PermissionError, AttributeError, TypeError)):
        broker.execute(o, 1.0, None)  # type: ignore[arg-type]
