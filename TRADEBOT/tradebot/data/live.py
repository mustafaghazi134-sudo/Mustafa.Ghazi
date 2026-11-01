"""Live market data.

Jupiter (current API surface):
  * Price API v3      GET {base}/price/v3?ids=<mint,...>           -> {mint: {usdPrice, blockId, decimals, priceChange24h}}
  * Tokens API v2     GET {base}/tokens/v2/search?query=<mint,...> -> [ {id, symbol, decimals, liquidity, usdPrice, isVerified,
                                                                         organicScore, stats24h{buyVolume, sellVolume, priceChange}, ...} ]
                      GET {base}/tokens/v2/tag?query=verified      -> same shape, the verified list
  base = https://api.jup.ag with an API key (x-api-key), else https://lite-api.jup.ag (free tier).
  (Price v2 and Tokens v1 are deprecated and no longer served.)
Binance public klines provide OHLCV history; CoinGecko OHLC is the fallback. DexScreener is a secondary
source for liquidity/volume if the Jupiter token endpoint fails.

All HTTP goes through httpx with short timeouts; failures raise so the engine can mark data stale.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from ..config import DataConfig, ExecutionConfig, UniverseConfig, jupiter_endpoints
from ..models import Candle, Ticker, TokenInfo
from .base import MarketDataProvider, interval_seconds

log = logging.getLogger(__name__)

_BINANCE_INTERVALS = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}
_COINGECKO_IDS = {"SOL": "solana", "JUP": "jupiter-exchange-solana", "RAY": "raydium", "BONK": "bonk",
                  "WIF": "dogwifcoin", "PYTH": "pyth-network", "JTO": "jito-governance-token"}
_STABLES = {"USDC", "USDT", "USDS", "PYUSD", "USDE", "DAI", "FDUSD", "USD1", "UXD", "USDH"}


class _Http:
    def __init__(self, timeout: float):
        self.client = httpx.Client(timeout=timeout, headers={"User-Agent": "tradebot/0.1"})

    def get_json(self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
                 retries: int = 2) -> Any:
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                r = self.client.get(url, params=params, headers=headers)
                if r.status_code == 429:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as e:  # ValueError: bad JSON
                last = e
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"GET {url} failed: {last}")


class LiveProvider(MarketDataProvider):
    def __init__(self, data_cfg: DataConfig, universe_cfg: UniverseConfig, execution_cfg: ExecutionConfig | None = None):
        self.cfg = data_cfg
        self.universe_cfg = universe_cfg
        self.jup = jupiter_endpoints(data_cfg, execution_cfg)
        self.http = _Http(data_cfg.request_timeout_sec)
        self.mints: dict[str, str] = dict(universe_cfg.mints)
        self.decimals: dict[str, int] = dict(universe_cfg.decimals)
        self._candle_cache: dict[tuple[str, str], tuple[float, list[Candle]]] = {}
        self._token_cache: tuple[float, dict[str, dict]] = (0.0, {})

    # ---- helpers -----------------------------------------------------------------------------
    def mint_for(self, symbol: str) -> str | None:
        return self.mints.get(symbol)

    # ---- Jupiter price v3 --------------------------------------------------------------------
    def _jupiter_prices(self, symbols: list[str]) -> dict[str, dict[str, float]]:
        ids = [self.mints[s] for s in symbols if s in self.mints]
        if not ids:
            return {}
        out: dict[str, dict[str, float]] = {}
        for i in range(0, len(ids), 50):
            data = self.http.get_json(self.jup.price, params={"ids": ",".join(ids[i:i + 50])}, headers=self.jup.headers) or {}
            by_mint = data.get("data", data) if isinstance(data, dict) else {}
            for s in symbols:
                m = self.mints.get(s)
                e = by_mint.get(m) if m else None
                if e and e.get("usdPrice") is not None:
                    out[s] = {"price": float(e["usdPrice"]), "change_24h_pct": float(e.get("priceChange24h") or 0.0)}
        return out

    # ---- Jupiter tokens v2 (liquidity / volume / verification) -------------------------------
    def _jupiter_token_stats(self, symbols: list[str]) -> dict[str, dict[str, float]]:
        now = time.time()
        if now - self._token_cache[0] < 120 and all(s in self._token_cache[1] for s in symbols):
            return {s: self._token_cache[1][s] for s in symbols}
        mints = [self.mints[s] for s in symbols if s in self.mints]
        by_mint: dict[str, dict] = {}
        for i in range(0, len(mints), 100):
            data = self.http.get_json(f"{self.jup.tokens}/search", params={"query": ",".join(mints[i:i + 100])}, headers=self.jup.headers) or []
            for t in data:
                by_mint[t.get("id")] = t
        out: dict[str, dict[str, float]] = {}
        for s in symbols:
            t = by_mint.get(self.mints.get(s))
            if not t:
                continue
            st = t.get("stats24h") or {}
            out[s] = {"liquidity_usd": float(t.get("liquidity") or 0.0),
                      "volume_24h_usd": float(st.get("buyVolume") or 0.0) + float(st.get("sellVolume") or 0.0),
                      "change_24h_pct": float(st.get("priceChange") or 0.0), "price": float(t.get("usdPrice") or 0.0),
                      "verified": 1.0 if t.get("isVerified") else 0.0, "organic_score": float(t.get("organicScore") or 0.0)}
            if t.get("decimals") is not None:
                self.decimals[s] = int(t["decimals"])
        self._token_cache = (now, {**self._token_cache[1], **out})
        return out

    def _dexscreener_stats(self, symbols: list[str]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        mints = [self.mints[s] for s in symbols if s in self.mints and s != "SOL"]
        for i in range(0, len(mints), 30):
            chunk = mints[i:i + 30]
            try:
                data = self.http.get_json(f"{self.cfg.dexscreener_base_url}/latest/dex/tokens/{','.join(chunk)}")
            except RuntimeError as e:
                log.warning("dexscreener failed: %s", e)
                continue
            best: dict[str, dict[str, float]] = {}
            for pair in (data or {}).get("pairs", []) or []:
                if pair.get("chainId") != "solana":
                    continue
                mint = (pair.get("baseToken") or {}).get("address")
                liq = float(((pair.get("liquidity") or {}).get("usd")) or 0.0)
                cur = best.get(mint)
                if cur is None or liq > cur["liquidity_usd"]:
                    best[mint] = {"liquidity_usd": liq, "volume_24h_usd": float(((pair.get("volume") or {}).get("h24")) or 0.0),
                                  "change_24h_pct": float(((pair.get("priceChange") or {}).get("h24")) or 0.0),
                                  "price": float(pair.get("priceUsd") or 0.0)}
            for s in symbols:
                if self.mints.get(s) in best:
                    out[s] = best[self.mints[s]]
        return out

    def get_tickers(self, symbols: list[str]) -> dict[str, Ticker]:
        ts = time.time()
        prices = self._jupiter_prices(symbols)
        try:
            stats = self._jupiter_token_stats(symbols)
        except RuntimeError as e:
            log.warning("jupiter tokens v2 failed (%s); falling back to dexscreener", e)
            stats = self._dexscreener_stats(symbols)
        out: dict[str, Ticker] = {}
        for s in symbols:
            st = stats.get(s, {})
            px = prices.get(s, {}).get("price") or st.get("price")
            if not px:
                continue
            liq, vol = float(st.get("liquidity_usd", 0.0)), float(st.get("volume_24h_usd", 0.0))
            if s == "SOL":  # SOL depth is effectively unbounded relative to our sizes
                liq, vol = max(liq, 1e9), max(vol, 1e9)
            chg = prices.get(s, {}).get("change_24h_pct") or st.get("change_24h_pct", 0.0)
            out[s] = Ticker(symbol=s, price=float(px), ts=ts, volume_24h_usd=vol, liquidity_usd=liq, change_24h_pct=float(chg))
        return out

    # ---- candles -----------------------------------------------------------------------------
    def _binance_klines(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        if interval not in _BINANCE_INTERVALS:
            raise ValueError(f"interval {interval} not supported by binance klines")
        data = self.http.get_json(f"{self.cfg.binance_base_url}/api/v3/klines",
                                  params={"symbol": f"{symbol}USDT", "interval": interval, "limit": min(limit, 1000)})
        return [Candle(ts=k[0] / 1000.0, open=float(k[1]), high=float(k[2]), low=float(k[3]), close=float(k[4]),
                       volume=float(k[5])) for k in data]

    def _coingecko_ohlc(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        cg_id = _COINGECKO_IDS.get(symbol)
        if not cg_id:
            raise RuntimeError(f"no coingecko id for {symbol}")
        days = 30 if interval in ("4h", "1h") else 365
        data = self.http.get_json(f"{self.cfg.coingecko_base_url}/coins/{cg_id}/ohlc", params={"vs_currency": "usd", "days": days})
        return [Candle(ts=k[0] / 1000.0, open=float(k[1]), high=float(k[2]), low=float(k[3]), close=float(k[4])) for k in data][-limit:]

    def get_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        key = (symbol, interval)
        isec = interval_seconds(interval)
        cached = self._candle_cache.get(key)
        now = time.time()
        if cached and now - cached[0] < isec * 0.25:
            return cached[1][-limit:]
        try:
            candles = self._binance_klines(symbol, interval, limit + 1)
        except (RuntimeError, ValueError) as e:
            log.info("binance klines unavailable for %s (%s); trying coingecko", symbol, e)
            candles = self._coingecko_ohlc(symbol, interval, limit + 1)
        if candles and candles[-1].ts + isec > now:   # drop the still-forming bar
            candles = candles[:-1]
        self._candle_cache[key] = (now, candles)
        return candles[-limit:]

    # ---- universe ----------------------------------------------------------------------------
    def get_universe(self, max_symbols: int) -> list[TokenInfo]:
        """Configured tokens first, then Jupiter-verified tokens ranked by 24h volume (organic score >= 50)."""
        out: list[TokenInfo] = [TokenInfo(s, m, self.decimals.get(s, 6)) for s, m in self.mints.items() if s not in _STABLES]
        try:
            tokens = self.http.get_json(f"{self.jup.tokens}/tag", params={"query": "verified"}, headers=self.jup.headers) or []
        except RuntimeError as e:
            log.warning("jupiter verified token list unavailable: %s", e)
            return out[:max_symbols]

        def vol(t: dict) -> float:
            st = t.get("stats24h") or {}
            return float(st.get("buyVolume") or 0.0) + float(st.get("sellVolume") or 0.0)

        for t in sorted(tokens, key=vol, reverse=True):
            sym = str(t.get("symbol", "")).upper()
            if not sym or sym in self.mints or sym in _STABLES or not t.get("isVerified"):
                continue
            if float(t.get("organicScore") or 0.0) < 50 or float(t.get("liquidity") or 0.0) < self.universe_cfg_min_liquidity():
                continue
            if len(out) >= max_symbols:
                break
            self.mints[sym] = t["id"]
            self.decimals[sym] = int(t.get("decimals", 6))
            out.append(TokenInfo(sym, t["id"], self.decimals[sym], t.get("name", "")))
        return out[:max_symbols]

    def universe_cfg_min_liquidity(self) -> float:
        return 250_000.0
