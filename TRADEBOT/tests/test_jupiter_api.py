"""Jupiter integration against the current API shapes (Price v3, Tokens v2, Swap v1, Ultra), with HTTP mocked.
The live endpoints could not be reached from the build sandbox; these fixtures follow the published response shapes."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from solders.keypair import Keypair

from tradebot.config import DataConfig, ExecutionConfig, Settings, UniverseConfig, jupiter_endpoints
from tradebot.data.live import LiveProvider
from tradebot.execution.base import ExecutionError
from tradebot.execution.jupiter_live import JupiterLiveBroker
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
    {"id": "Mint1111111111111111111111111111111111111111", "symbol": "GOOD", "decimals": 6, "liquidity": 3_000_000, "isVerified": True,
     "organicScore": 80, "stats24h": {"buyVolume": 5e6, "sellVolume": 4e6}},
    {"id": "Mint2222222222222222222222222222222222222222", "symbol": "SCAM", "decimals": 6, "liquidity": 9_000_000, "isVerified": True,
     "organicScore": 5, "stats24h": {"buyVolume": 9e7, "sellVolume": 9e7}},
    {"id": "Mint3333333333333333333333333333333333333333", "symbol": "USDT", "decimals": 6, "liquidity": 9e8, "isVerified": True,
     "organicScore": 99, "stats24h": {"buyVolume": 9e9, "sellVolume": 9e9}},
]
SWAP_QUOTE = {"inputMint": USDC, "inAmount": "100000000", "outputMint": SOL, "outAmount": "554000000", "otherAmountThreshold": "551230000",
              "swapMode": "ExactIn", "slippageBps": 50, "platformFee": None, "priceImpactPct": "0.0001",
              "routePlan": [{"swapInfo": {"ammKey": "x", "label": "Whirlpool", "inputMint": USDC, "outputMint": SOL, "inAmount": "100000000",
                                          "outAmount": "554000000", "feeAmount": "30000", "feeMint": USDC}, "percent": 100}], "contextSlot": 1}
ULTRA_ORDER = {**SWAP_QUOTE, "transaction": None, "requestId": "req-1", "feeBps": 5, "signatureFeeLamports": 5000,
               "prioritizationFeeLamports": 100000, "rentFeeLamports": 2039280, "router": "metis", "swapType": "aggregator"}


def _provider(key=""):
    d = DataConfig(jupiter_api_key=key)
    p = LiveProvider(d, UniverseConfig(), ExecutionConfig())
    return p


def test_endpoint_resolution_free_vs_keyed(monkeypatch):
    monkeypatch.delenv("JUPITER_API_KEY", raising=False)
    free = jupiter_endpoints(DataConfig(), ExecutionConfig())
    assert free.base == "https://lite-api.jup.ag" and free.headers == {}
    assert free.price.endswith("/price/v3") and free.tokens.endswith("/tokens/v2") and free.swap.endswith("/swap/v1") and free.ultra.endswith("/ultra/v1")
    keyed = jupiter_endpoints(DataConfig(jupiter_api_key="k"), ExecutionConfig())
    assert keyed.base == "https://api.jup.ag" and keyed.headers == {"x-api-key": "k"}
    monkeypatch.setenv("JUPITER_API_KEY", "envkey")
    assert jupiter_endpoints(DataConfig(), ExecutionConfig()).headers == {"x-api-key": "envkey"}
    override = jupiter_endpoints(DataConfig(), ExecutionConfig(jupiter_swap_base_url="https://my.proxy/swap/v1"))
    assert override.swap == "https://my.proxy/swap/v1"


def test_live_provider_parses_price_v3_and_tokens_v2(monkeypatch):
    p = _provider()
    calls = []

    def fake_get(url, params=None, headers=None, retries=2):
        calls.append((url, params))
        if url.endswith("/price/v3"):
            assert set(params["ids"].split(",")) == {SOL, JUP}
            return PRICE_V3
        if url.endswith("/tokens/v2/search"):
            return TOKENS_V2
        raise AssertionError(f"unexpected url {url}")

    p.http.get_json = fake_get
    t = p.get_tickers(["SOL", "JUP"])
    assert t["SOL"].price == pytest.approx(180.25) and t["SOL"].change_24h_pct == pytest.approx(-2.1)
    assert t["JUP"].liquidity_usd == pytest.approx(12_000_000) and t["JUP"].volume_24h_usd == pytest.approx(55_000_000)
    assert t["SOL"].liquidity_usd >= 1e9  # SOL depth floor
    assert not any("/price/v2" in u or "/tokens/v1" in u for u, _ in calls)


def test_live_provider_falls_back_to_dexscreener_when_tokens_v2_fails():
    p = _provider()

    def fake_get(url, params=None, headers=None, retries=2):
        if url.endswith("/price/v3"):
            return PRICE_V3
        if "/tokens/v2/" in url:
            raise RuntimeError("tokens down")
        if "dexscreener" in url:
            return {"pairs": [{"chainId": "solana", "baseToken": {"address": JUP}, "liquidity": {"usd": 1e6}, "volume": {"h24": 2e6},
                               "priceChange": {"h24": 1.0}, "priceUsd": "0.76"}]}
        raise AssertionError(url)

    p.http.get_json = fake_get
    t = p.get_tickers(["JUP"])
    assert t["JUP"].liquidity_usd == pytest.approx(1e6) and t["JUP"].price == pytest.approx(0.75)


def test_universe_scan_filters_unverified_low_organic_and_stables():
    p = _provider()
    p.http.get_json = lambda url, params=None, headers=None, retries=2: VERIFIED if url.endswith("/tokens/v2/tag") else None
    syms = [t.symbol for t in p.get_universe(20)]
    assert "GOOD" in syms and "SCAM" not in syms and "USDT" not in syms and "USDC" not in syms
    assert p.mints["GOOD"].startswith("Mint1")


def _broker(tmp_path, engine="swap", armed=True, monkeypatch=None):
    s = Settings(mode="live", data_dir=str(tmp_path))
    s.execution.engine = engine
    ledger_gate = LiveGate(s)
    if armed and monkeypatch is not None:
        monkeypatch.setenv("TRADEBOT_LIVE_TRADING", "I_ACCEPT_THE_RISK")
        ledger_gate.arm(s.engine.live_confirm_phrase, force=True)
    risk = RiskEngine(s.risk, KillSwitch(tmp_path / "KILL"))
    prov = _provider()
    b = JupiterLiveBroker(risk, s, prov, Keypair(), ledger_gate)
    return s, b


def test_swap_quote_and_body_use_current_swap_v1(tmp_path, monkeypatch):
    s, b = _broker(tmp_path, monkeypatch=monkeypatch)
    seen = {}
    b._get = lambda url, params: (seen.setdefault("quote", (url, params)), SWAP_QUOTE)[1]
    q = b.quote("SOL", Side.BUY, 100.0)
    assert seen["quote"][0].endswith("/swap/v1/quote") and seen["quote"][1]["inputMint"] == USDC and seen["quote"][1]["amount"] == "100000000"
    assert q.out_amount == pytest.approx(0.554) and q.price == pytest.approx(100 / 0.554)
    body = b.swap_body(q.raw)
    assert body["userPublicKey"] == b.pubkey and body["dynamicComputeUnitLimit"] is True and body["wrapAndUnwrapSol"] is True
    assert body["prioritizationFeeLamports"] == {"priorityLevelWithMaxLamports": {"maxLamports": 100_000, "priorityLevel": "high"}}


def test_live_execute_blocked_when_not_armed_before_any_network_call(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADEBOT_LIVE_TRADING", raising=False)
    s, b = _broker(tmp_path, armed=False)
    b._post = lambda *a, **k: pytest.fail("network call attempted while disarmed")
    b._rpc = lambda *a, **k: pytest.fail("rpc call attempted while disarmed")
    from tradebot.execution.base import Quote
    q = Quote("SOL", Side.BUY, 180.0, 1.0, 0.05, 100.0, 0.554, raw=SWAP_QUOTE)
    with pytest.raises(LiveGateError):
        b._execute(OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=100), 0.554, q)


def test_swap_simulation_error_aborts_before_signing(tmp_path, monkeypatch):
    s, b = _broker(tmp_path, monkeypatch=monkeypatch)
    b._post = lambda url, body: {"swapTransaction": "AAAA", "simulationError": {"error": "InsufficientFunds"}}
    b._sign = lambda tx: pytest.fail("must not sign a failing swap")
    with pytest.raises(ExecutionError):
        b._execute_swap(SWAP_QUOTE)


def test_ultra_engine_requires_transaction_and_checks_status(tmp_path, monkeypatch):
    s, b = _broker(tmp_path, engine="ultra", monkeypatch=monkeypatch)
    b._get = lambda url, params: ULTRA_ORDER  # no transaction -> cannot execute
    with pytest.raises(ExecutionError):
        b.quote("SOL", Side.BUY, 100.0)
    b._sign = lambda tx: "signed"
    b._post = lambda url, body: {"status": "Failed", "code": -1, "error": "slippage"}
    with pytest.raises(ExecutionError):
        b._execute_ultra({**ULTRA_ORDER, "transaction": "AAAA"})
    b._post = lambda url, body: {"status": "Success", "signature": "sig123"}
    assert b._execute_ultra({**ULTRA_ORDER, "transaction": "AAAA"}) == "sig123"


def test_preflight_quote_report_includes_ultra_fee_breakdown(tmp_path, monkeypatch):
    import httpx
    from tradebot import preflight as pf

    def fake_get(url, timeout=None, headers=None, params=None):
        if "/swap/v1/quote" in url:
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {**SWAP_QUOTE, "inputMint": SOL, "outputMint": USDC,
                                                                           "inAmount": "1000000", "outAmount": "180250"})
        if "/ultra/v1/order" in url:
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {**ULTRA_ORDER, "inAmount": "1000000", "outAmount": "180200"})
        if "/ultra/v1/balances/" in url:
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"SOL": {"amount": "4500000", "uiAmount": 0.0045, "slot": 1, "isFrozen": False}})
        raise AssertionError(url)

    monkeypatch.setattr(httpx, "get", fake_get)
    s = Settings(data_dir=str(tmp_path))
    rep = pf.jupiter_quote(s, "SOL", "USDC", 0.001, 50, wallet="WalletPubkey111")
    assert rep.out_amount == pytest.approx(0.18025) and rep.sol_price_usd == pytest.approx(180.25)
    assert rep.ultra["rent_fee_sol"] == pytest.approx(0.00203928) and rep.ultra["fee_bps"] == 5
    assert rep.wallet_sol == pytest.approx(0.0045) and rep.wallet_has_out_ata is False
    text = pf.format_quote(rep)
    assert "can fund this swap: YES" in text and "Ultra order estimate" in text
