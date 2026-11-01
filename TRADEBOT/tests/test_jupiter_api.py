"""Jupiter integration against the current API (official jup-ag/agent-skills: Price v3, Tokens v2, Swap v2), HTTP mocked.
The live endpoints could not be reached from the build sandbox; these fixtures follow the skills' documented shapes."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from solders.keypair import Keypair

from tradebot.config import DataConfig, ExecutionConfig, Settings, UniverseConfig, jupiter_endpoints
from tradebot.data.live import LiveProvider
from tradebot.execution.base import ExecutionError, Quote
from tradebot.execution.jupiter_live import RETRYABLE_EXECUTE_CODES, JupiterLiveBroker
from tradebot.execution.live_gate import LiveGate, LiveGateError
from tradebot.models import OrderRequest, Side
from tradebot.risk.engine import RiskEngine
from tradebot.risk.kill_switch import KillSwitch

SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
JUP = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"

PRICE_V3 = {SOL: {"usdPrice": 180.25, "blockId": 1, "decimals": 9, "priceChange24h": -2.1},
            JUP: {"usdPrice": 0.75, "blockId": 1, "decimals": 6, "priceChange24h": 4.0}}
TOKENS_V2 = [
    {"id": SOL, "symbol": "SOL", "decimals": 9, "liquidity": 5e8, "usdPrice": 180.2, "isVerified": True, "organicScore": 99,
     "stats24h": {"buyVolume": 1e9, "sellVolume": 9e8, "priceChange": -2.1}},
    {"id": JUP, "symbol": "JUP", "decimals": 6, "liquidity": 12_000_000, "usdPrice": 0.75, "isVerified": True, "organicScore": 90,
     "stats24h": {"buyVolume": 30_000_000, "sellVolume": 25_000_000, "priceChange": 4.0}},
]
VERIFIED = TOKENS_V2 + [
    {"id": "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij", "symbol": "cbBTC", "decimals": 8, "liquidity": 4e7, "isVerified": True,
     "organicScore": 90, "stats24h": {"buyVolume": 5e8, "sellVolume": 5e8}},      # already configured as BTC: must not re-enter as CBBTC
    {"id": "Mint5555555555555555555555555555555555555555", "symbol": "USDG", "decimals": 6, "liquidity": 9e8, "isVerified": True,
     "organicScore": 99, "stats24h": {"buyVolume": 9e9, "sellVolume": 9e9}},
    {"id": "Mint1111111111111111111111111111111111111111", "symbol": "GOOD", "decimals": 6, "liquidity": 3_000_000, "isVerified": True,
     "organicScore": 80, "stats24h": {"buyVolume": 5e6, "sellVolume": 4e6}},
    {"id": "Mint2222222222222222222222222222222222222222", "symbol": "SCAM", "decimals": 6, "liquidity": 9_000_000, "isVerified": True,
     "organicScore": 5, "stats24h": {"buyVolume": 9e7, "sellVolume": 9e7}},
    {"id": "Mint4444444444444444444444444444444444444444", "symbol": "SUS", "decimals": 6, "liquidity": 9_000_000, "isVerified": True,
     "organicScore": 70, "audit": {"isSus": True}, "stats24h": {"buyVolume": 9e7, "sellVolume": 9e7}},
    {"id": "Mint3333333333333333333333333333333333333333", "symbol": "USDT", "decimals": 6, "liquidity": 9e8, "isVerified": True,
     "organicScore": 99, "stats24h": {"buyVolume": 9e9, "sellVolume": 9e9}},
]
# Swap v2 /order with taker (transaction present) — shapes per integrating-jupiter/examples/swap.md and ultra-to-order.md
ORDER = {"inputMint": USDC, "inAmount": "100000000", "outputMint": SOL, "outAmount": "554000000", "otherAmountThreshold": "551230000",
         "slippageBps": 50, "priceImpactPct": "0.0001", "feeBps": 2, "feeMint": USDC, "router": "metis", "mode": "ultra", "swapType": "aggregator",
         "routePlan": [{"swapInfo": {"ammKey": "x", "label": "Whirlpool", "inputMint": USDC, "outputMint": SOL, "inAmount": "100000000",
                                     "outAmount": "554000000", "feeAmount": "30000", "feeMint": USDC}, "bps": 10000}],
         "transaction": "AAAA", "requestId": "req-1", "signatureFeeLamports": 5000, "prioritizationFeeLamports": 100000, "rentFeeLamports": 2039280}


def _provider(key="k"):
    return LiveProvider(DataConfig(jupiter_api_key=key), UniverseConfig(), ExecutionConfig())


def test_endpoint_resolution_matches_official_skill(monkeypatch):
    monkeypatch.delenv("JUPITER_API_KEY", raising=False)
    e = jupiter_endpoints(DataConfig(), ExecutionConfig())
    assert e.base == "https://api.jup.ag" and not e.has_key          # key required; no keyless host
    assert e.price == "https://api.jup.ag/price/v3" and e.tokens == "https://api.jup.ag/tokens/v2" and e.swap == "https://api.jup.ag/swap/v2"
    assert not hasattr(e, "ultra")
    keyed = jupiter_endpoints(DataConfig(jupiter_api_key="k"), ExecutionConfig())
    assert keyed.headers == {"x-api-key": "k"} and keyed.has_key
    monkeypatch.setenv("JUPITER_API_KEY", "envkey")
    assert jupiter_endpoints(DataConfig(), ExecutionConfig()).headers == {"x-api-key": "envkey"}
    assert jupiter_endpoints(DataConfig(), ExecutionConfig(jupiter_swap_base_url="https://my.proxy/swap/v2")).swap == "https://my.proxy/swap/v2"


def test_live_provider_parses_price_v3_and_tokens_v2_and_fails_closed_on_missing_price():
    p = _provider()
    calls = []

    def fake_get(url, params=None, headers=None, retries=2):
        calls.append(url)
        if url.endswith("/price/v3"):
            assert headers == {"x-api-key": "k"}
            return {SOL: PRICE_V3[SOL]}                 # JUP omitted => unpriced => no ticker
        if url.endswith("/tokens/v2/search"):
            return TOKENS_V2
        raise AssertionError(url)

    p.http.get_json = fake_get
    t = p.get_tickers(["SOL", "JUP"])
    assert t["SOL"].price == pytest.approx(180.25) and t["SOL"].liquidity_usd >= 1e9
    assert "JUP" in t and t["JUP"].price == pytest.approx(0.75)   # tokens v2 usdPrice is the documented fallback
    assert not any("/price/v2" in u or "/tokens/v1" in u or "/ultra/" in u or "/swap/v1" in u for u in calls)


def test_sus_tokens_are_excluded_everywhere():
    p = _provider()
    p.http.get_json = lambda url, params=None, headers=None, retries=2: VERIFIED if url.endswith("/tokens/v2/tag") else (
        VERIFIED if url.endswith("/tokens/v2/search") else {"Mint4444444444444444444444444444444444444444": {"usdPrice": 1.0}})
    syms = [t.symbol for t in p.get_universe(20)]
    assert "GOOD" in syms and "SUS" not in syms and "SCAM" not in syms and "USDT" not in syms
    assert "CBBTC" not in syms and "USDG" not in syms and syms.count("BTC") == 1
    p.mints["SUS"] = "Mint4444444444444444444444444444444444444444"
    assert "SUS" not in p.get_tickers(["SUS"])


def _broker(tmp_path, armed=True, monkeypatch=None, key="k"):
    s = Settings(mode="live", data_dir=str(tmp_path))
    s.data.jupiter_api_key = key
    gate = LiveGate(s)
    if armed and monkeypatch is not None:
        monkeypatch.setenv("TRADEBOT_LIVE_TRADING", "I_ACCEPT_THE_RISK")
        gate.arm(s.engine.live_confirm_phrase, force=True)
    risk = RiskEngine(s.risk, KillSwitch(tmp_path / "KILL"))
    return s, JupiterLiveBroker(risk, s, _provider(key), Keypair(), gate)


def test_broker_fails_fast_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("JUPITER_API_KEY", raising=False)
    with pytest.raises(ExecutionError):
        _broker(tmp_path, armed=False, key="")


def test_quote_uses_swap_v2_order_with_taker_and_raw_amounts(tmp_path, monkeypatch):
    s, b = _broker(tmp_path, monkeypatch=monkeypatch)
    seen = {}
    b._request = lambda client, method, url, **kw: (seen.setdefault("call", (method, url, kw)), ORDER)[1]
    q = b.quote("SOL", Side.BUY, 100.0)
    method, url, kw = seen["call"]
    assert method == "GET" and url.endswith("/swap/v2/order")
    assert kw["params"]["inputMint"] == USDC and kw["params"]["amount"] == "100000000" and kw["params"]["taker"] == b.pubkey
    assert q.out_amount == pytest.approx(0.554) and q.price == pytest.approx(100 / 0.554)
    assert q.fee_usd == pytest.approx(100 * 2 / 1e4)   # feeBps from the order


def test_execute_blocked_when_not_armed_before_any_network_call(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADEBOT_LIVE_TRADING", raising=False)
    s, b = _broker(tmp_path, armed=False)
    b._request = lambda *a, **k: pytest.fail("network call attempted while disarmed")
    q = Quote("SOL", Side.BUY, 180.0, 1.0, 0.05, 100.0, 0.554, raw=ORDER)
    with pytest.raises(LiveGateError):
        b._execute(OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=100), 0.554, q)


def test_execute_success_uses_actual_amounts_and_requestid(tmp_path, monkeypatch):
    s, b = _broker(tmp_path, monkeypatch=monkeypatch)
    posted = {}

    def fake_request(client, method, url, **kw):
        assert method == "POST" and url.endswith("/swap/v2/execute")
        posted.update(kw["json"])
        return {"status": "Success", "code": 0, "signature": "sig123", "inputAmountResult": "100000000", "outputAmountResult": "553000000"}

    b._request = fake_request
    b._sign = lambda tx: "signed-" + tx
    q = Quote("SOL", Side.BUY, 180.0, 1.0, 0.02, 100.0, 0.554, raw=ORDER)
    fill = b._execute(OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=100), 0.554, q)
    assert posted == {"signedTransaction": "signed-AAAA", "requestId": "req-1"}
    assert fill.tx_signature == "sig123" and fill.qty == pytest.approx(0.553) and fill.notional_usd == pytest.approx(100.0)


def test_execute_retryable_code_requotes_then_non_retryable_raises(tmp_path, monkeypatch):
    s, b = _broker(tmp_path, monkeypatch=monkeypatch)
    s.execution.execute_max_retries = 1
    calls = []

    def fake_request(client, method, url, **kw):
        calls.append((method, url.rsplit("/", 1)[-1]))
        if url.endswith("/order"):
            return {**ORDER, "requestId": "req-2"}
        return {"status": "Failed", "code": -1, "error": "expired"} if len([c for c in calls if c[1] == "execute"]) == 1 else \
               {"status": "Success", "code": 0, "signature": "sig2", "inputAmountResult": "100000000", "outputAmountResult": "554000000"}

    b._request = fake_request; b._sign = lambda tx: "s"
    monkeypatch.setattr("tradebot.execution.jupiter_live.time.sleep", lambda *_: None)
    fill = b._execute(OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=100), 0.554, Quote("SOL", Side.BUY, 180.0, 1.0, 0.02, 100.0, 0.554, raw=ORDER))
    assert [c[1] for c in calls] == ["execute", "order", "execute"] and fill.tx_signature == "sig2"
    assert -1 in RETRYABLE_EXECUTE_CODES and -2 not in RETRYABLE_EXECUTE_CODES
    b._request = lambda client, method, url, **kw: {"status": "Failed", "code": -2, "error": "invalid signed transaction"}
    with pytest.raises(ExecutionError):
        b._execute(OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=100), 0.554, Quote("SOL", Side.BUY, 180.0, 1.0, 0.02, 100.0, 0.554, raw=ORDER))


def test_preflight_quote_uses_order_without_taker(tmp_path, monkeypatch):
    import httpx
    from tradebot import preflight as pf
    seen = {}

    def fake_get(url, timeout=None, headers=None, params=None):
        seen["url"], seen["params"], seen["timeout"] = url, params, timeout
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {**ORDER, "inputMint": SOL, "outputMint": USDC,
                                                                       "inAmount": "1000000", "outAmount": "180250", "transaction": None})

    monkeypatch.setattr(httpx, "get", fake_get)
    s = Settings(data_dir=str(tmp_path)); s.data.jupiter_api_key = "k"
    rep = pf.jupiter_quote(s, "SOL", "USDC", 0.001, 50)
    assert seen["url"].endswith("/swap/v2/order") and "taker" not in seen["params"] and seen["timeout"] == 5.0
    assert rep.out_amount == pytest.approx(0.18025) and rep.sol_price_usd == pytest.approx(180.25)
    assert rep.order_info["fee_bps"] == 2 and rep.order_info["rent_fee_sol"] == pytest.approx(0.00203928)
    assert "Swap v2 order" in pf.format_quote(rep)
    s.data.jupiter_api_key = ""
    monkeypatch.delenv("JUPITER_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        pf.jupiter_quote(s, "SOL", "USDC", 0.001, 50)
