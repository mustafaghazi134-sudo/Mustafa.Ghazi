"""Paper fills from size-specific Jupiter quotes (mocked HTTP), with parametric fallback."""
from types import SimpleNamespace

import httpx
import pytest

from tradebot.config import DataConfig, ExecutionConfig, UniverseConfig
from tradebot.data.live import LiveProvider
from tradebot.execution.paper import SOLANA_TX_FEE_USD, PaperBroker
from tradebot.models import OrderRequest, Side
from tradebot.risk.engine import MarketState, PortfolioState

SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _provider_with_price(price=180.0):
    p = LiveProvider(DataConfig(jupiter_api_key="k"), UniverseConfig(), ExecutionConfig())
    p.http.get_json = lambda url, params=None, headers=None, retries=2: (
        {SOL: {"usdPrice": price}} if url.endswith("/price/v3") else
        [{"id": SOL, "symbol": "SOL", "decimals": 9, "liquidity": 1e9, "isVerified": True, "stats24h": {"buyVolume": 1e9, "sellVolume": 1e9}}])
    return p


def test_paper_buy_and_sell_use_size_specific_quotes_and_min_out(risk, clock, monkeypatch):
    prov = _provider_with_price(180.0)
    seen = []

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.append(params)
        assert url.endswith("/swap/v2/order") and "taker" not in params and headers == {"x-api-key": "k"} and timeout == 5.0
        if params["inputMint"] == USDC:                      # buy $100 of SOL at mid 180 (0.5556): route gives 0.5540, guarantees 0.5525
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"outAmount": "554000000", "otherAmountThreshold": "552500000", "router": "metis", "feeBps": 2})
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"outAmount": "99200000", "otherAmountThreshold": "99000000", "router": "jupiterz", "feeBps": 0})   # sell 0.5525 SOL (~$99.45 at mid)

    monkeypatch.setattr(httpx, "get", fake_get)
    b = PaperBroker(risk, prov, clock=clock)
    pf = PortfolioState(1000, 1000, {}, 0, 0, 0, None, 1000, 1000)
    o = OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=100)
    d = risk.evaluate(o, pf, MarketState(180.0, 1e9, 1e9, clock()))
    fill = b.execute(o, d.adjusted_qty, d.approval)
    assert b.quote_source == "jupiter" and seen[0]["amount"] == "100000000"
    assert fill.qty == pytest.approx(0.5525)                               # guaranteed minimum, not the headline outAmount
    assert fill.price == pytest.approx((100 - SOLANA_TX_FEE_USD) / 0.5525) and fill.price > 180.0   # worse than mid
    assert fill.slippage_bps > 0 and fill.fee_usd == SOLANA_TX_FEE_USD
    # sell path: amount is token qty, output is USDC min-out minus network fee
    from tradebot.models import Position
    pf2 = PortfolioState(1000, 900, {"SOL": Position("SOL", 0.5525, fill.price, 0)}, 1, 0, 0, None, 1000, 1000)
    o2 = OrderRequest(symbol="SOL", side=Side.SELL, notional_usd=99, qty=0.5525, reduce_only=True)
    d2 = risk.evaluate(o2, pf2, MarketState(180.0, 1e9, 1e9, clock()))
    fill2 = b.execute(o2, d2.adjusted_qty, d2.approval)
    assert seen[1]["inputMint"] == SOL and seen[1]["amount"] == str(int(round(0.5525 * 1e9)))
    assert fill2.notional_usd == pytest.approx(99.0 - SOLANA_TX_FEE_USD) and fill2.price < 180.0


def test_paper_falls_back_to_parametric_when_quote_fails_or_no_key(risk, clock, monkeypatch):
    prov = _provider_with_price(180.0)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))
    b = PaperBroker(risk, prov, fee_bps=30, slippage_bps=20, clock=clock)
    pf = PortfolioState(1000, 1000, {}, 0, 0, 0, None, 1000, 1000)
    o = OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=100)
    d = risk.evaluate(o, pf, MarketState(180.0, 1e9, 1e9, clock()))
    fill = b.execute(o, d.adjusted_qty, d.approval)
    assert b.quote_source == "parametric" and fill.fee_usd == pytest.approx(0.30) and 180.0 < fill.price <= 180.0 * 1.002
    # no key: never calls Jupiter
    prov2 = LiveProvider(DataConfig(jupiter_api_key=""), UniverseConfig(), ExecutionConfig()); prov2.http.get_json = prov.http.get_json
    monkeypatch.delenv("JUPITER_API_KEY", raising=False)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("must not call Jupiter without a key"))
    b2 = PaperBroker(risk, prov2, clock=clock)
    o2 = OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=100)
    d2 = risk.evaluate(o2, pf, MarketState(180.0, 1e9, 1e9, clock()))
    assert b2.execute(o2, d2.adjusted_qty, d2.approval).qty > 0 and b2.quote_source == "parametric"


def test_keyless_quotes_only_when_explicitly_enabled(risk, clock, monkeypatch):
    """paper_keyless_quotes: Jupiter quotes without a key (rate-limited public access); still no taker, still min-out."""
    prov = LiveProvider(DataConfig(jupiter_api_key=""), UniverseConfig(), ExecutionConfig())
    prov.http.get_json = _provider_with_price(180.0).http.get_json
    monkeypatch.delenv("JUPITER_API_KEY", raising=False)
    seen = []

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.append((params, headers))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"outAmount": "554000000", "otherAmountThreshold": "552500000"})
    monkeypatch.setattr(httpx, "get", fake_get)
    b = PaperBroker(risk, prov, clock=clock, allow_keyless_quotes=True)
    pf = PortfolioState(1000, 1000, {}, 0, 0, 0, None, 1000, 1000)
    o = OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=100)
    d = risk.evaluate(o, pf, MarketState(180.0, 1e9, 1e9, clock()))
    fill = b.execute(o, d.adjusted_qty, d.approval)
    assert b.quote_source == "jupiter" and fill.qty == pytest.approx(0.5525)
    assert seen and "taker" not in seen[0][0] and seen[0][1] == {}
