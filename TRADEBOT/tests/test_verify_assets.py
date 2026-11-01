"""verify-assets with mocked HTTP: the exposure mapping (BTC->cbBTC, ETH->WETH) and the executability checks."""
from types import SimpleNamespace

import httpx
import pytest

from tradebot.config import Settings
from tradebot.preflight import format_assets, verify_assets

CBBTC = "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij"
WETH = "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def test_defaults_map_validated_exposure_to_solana_mints():
    s = Settings()
    assert s.universe.symbols == ["BTC", "ETH", "SOL"] and s.universe.auto_scan is False
    assert s.universe.mints["BTC"] == CBBTC and s.universe.decimals["BTC"] == 8
    assert s.universe.mints["ETH"] == WETH and s.universe.decimals["ETH"] == 8
    assert s.universe.candle_symbols["BTC"] == "BTCUSDT" and s.universe.candle_symbols["ETH"] == "ETHUSDT"


def _resp(payload):
    return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload, status_code=200)


def test_verify_assets_round_trip_and_limits(tmp_path, monkeypatch):
    s = Settings(data_dir=str(tmp_path), initial_capital_usd=1000.0)   # order size = 10% = $100
    s.universe.symbols = ["BTC"]
    price = 100_000.0
    # LiveProvider HTTP (price v3 + tokens v2 + binance klines)
    import tradebot.data.live as live

    def fake_get_json(self, url, params=None, headers=None, retries=2):
        if url.endswith("/price/v3"):
            return {CBBTC: {"usdPrice": price * 1.001, "priceChange24h": 1.0}}
        if url.endswith("/tokens/v2/search"):
            return [{"id": CBBTC, "symbol": "cbBTC", "decimals": 8, "liquidity": 30_000_000, "isVerified": True, "organicScore": 95,
                     "stats24h": {"buyVolume": 20_000_000, "sellVolume": 18_000_000, "priceChange": 1.0}}]
        if "/api/v3/klines" in url:
            assert params["symbol"] == "BTCUSDT"
            import time
            now = int(time.time() // 14400 * 14400) - 14400 * 1000
            return [[(now + i * 14400) * 1000, price, price * 1.01, price * 0.99, price, 10.0] for i in range(1000)]
        raise AssertionError(url)

    monkeypatch.setattr(live._Http, "get_json", fake_get_json)

    def fake_httpx_get(url, timeout=None, headers=None, params=None):
        assert "/swap/v1/quote" in url
        if params["inputMint"] == USDC:
            out = int(100 / (price * 1.001) * 1e8 * 0.998)
            return _resp({"inAmount": params["amount"], "outAmount": str(out), "priceImpactPct": "0.0005",
                          "routePlan": [{"swapInfo": {"label": "Whirlpool"}}]})
        qty = int(params["amount"]) / 1e8
        return _resp({"inAmount": params["amount"], "outAmount": str(int(qty * price * 0.999 * 1e6)), "priceImpactPct": "0.0004",
                      "routePlan": [{"swapInfo": {"label": "Whirlpool"}}]})

    monkeypatch.setattr(httpx, "get", fake_httpx_get)
    rows = verify_assets(s)
    r = rows[0]
    assert r["status"] == "OK", r["problems"]
    assert r["mint"] == CBBTC and r["candles"] == 1000 and r["order_usd"] == 100.0
    assert abs(r["basis_pct"] - 0.1) < 0.01
    assert 0 < r["round_trip_cost_bps"] < 50 and r["buy_impact_bps"] == 5.0
    assert "all assets executable" in format_assets(rows)


def test_verify_assets_flags_thin_liquidity_and_high_impact(tmp_path, monkeypatch):
    s = Settings(data_dir=str(tmp_path), initial_capital_usd=1000.0)
    s.universe.symbols = ["ETH"]
    import tradebot.data.live as live
    monkeypatch.setattr(live._Http, "get_json", lambda self, url, params=None, headers=None, retries=2:
                        {WETH: {"usdPrice": 3000.0}} if url.endswith("/price/v3") else
                        ([{"id": WETH, "symbol": "WETH", "decimals": 8, "liquidity": 50_000, "isVerified": True,
                           "stats24h": {"buyVolume": 1000, "sellVolume": 1000}}] if "/tokens/v2/" in url else
                         [[1_700_000_000_000 + i * 14400_000, 3000, 3030, 2970, 3000, 1.0] for i in range(1000)]))
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None, headers=None, params=None:
                        _resp({"inAmount": params["amount"], "outAmount": "1000000", "priceImpactPct": "0.05", "routePlan": []}))
    r = verify_assets(s)[0]
    assert r["status"] in ("WARN", "FAIL")
    joined = " ".join(r["problems"])
    assert "liquidity" in joined and "price impact" in joined
