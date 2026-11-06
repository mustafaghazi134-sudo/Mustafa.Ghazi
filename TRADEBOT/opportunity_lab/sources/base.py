"""Source interfaces and the shared rate-limited HTTP client.

Three roles, each with live, synthetic and replay implementations:
  MarketData  discover() new/active candidates; refresh(mints) current snapshots
  FactsSource mint_facts(mint): authorities, Token-2022 extensions, holder concentration, indexer flags
  RouteSource route_check(mint, size_usd): read-only buy and sell-back quotes (never with a taker or wallet)
Any failure returns None / ok=False with the reason: callers treat missing data as a reason to reject.
"""
from __future__ import annotations

import logging
import time
from typing import Protocol

import httpx

from tradebot.fast.ratelimit import ApiLimit, RateLimiter

from ..models import MintFacts, RouteCheck, TokenSnapshot

log = logging.getLogger("opportunity_lab")

# Conservative budgets, below each provider's published limit (see docs/GROK_STYLE_RESEARCH.md).
LAB_LIMITS = {
    "dexscreener_slow": ApiLimit(per_sec=0.5, burst=2, base_backoff_sec=5.0),     # profiles/boosts: 60/min published
    "dexscreener_pairs": ApiLimit(per_sec=2.0, burst=4, base_backoff_sec=3.0),    # tokens/pairs: 300/min published
    "geckoterminal": ApiLimit(per_sec=0.075, burst=1, base_backoff_sec=20.0),     # keyless: about 5 calls/min (2026 docs)
    "jupiter_tokens": ApiLimit(per_sec=0.2, burst=1, base_backoff_sec=5.0),       # keyless api.jup.ag: 0.5 req/s shared
    "jupiter_swap": ApiLimit(per_sec=0.25, burst=2, base_backoff_sec=5.0),
    "solana_rpc": ApiLimit(per_sec=2.0, burst=4, base_backoff_sec=3.0),          # public RPC: 40 req / 10 s per method
    "rugcheck": ApiLimit(per_sec=0.2, burst=1, base_backoff_sec=10.0),
    "goplus": ApiLimit(per_sec=0.2, burst=1, base_backoff_sec=10.0),
}


class MarketData(Protocol):
    name: str

    def discover(self) -> list[TokenSnapshot]: ...
    def refresh(self, mints: list[str]) -> dict[str, TokenSnapshot]: ...


class FactsSource(Protocol):
    def mint_facts(self, mint: str) -> MintFacts | None: ...


class RouteSource(Protocol):
    def route_check(self, mint: str, size_usd: float) -> RouteCheck: ...


class Http:
    """JSON GET/POST through the shared RateLimiter. Returns None on throttle, 4xx/5xx or network error."""

    def __init__(self, limiter: RateLimiter | None = None, timeout: float = 10.0, user_agent: str = "",
                 transport: httpx.BaseTransport | None = None, clock=time.time):
        self.limiter = limiter or RateLimiter(LAB_LIMITS, clock=clock)
        for k, v in LAB_LIMITS.items():
            self.limiter.limits.setdefault(k, v)
        self.client = httpx.Client(timeout=timeout, headers={"User-Agent": user_agent or "tradebot-opportunity-lab"},
                                   transport=transport, follow_redirects=False)
        self.last_error: dict[str, str] = {}

    def request(self, api: str, method: str, url: str, **kw) -> object | None:
        ok, why = self.limiter.acquire(api)
        if not ok:
            self.last_error[api] = f"local throttle ({why})"
            return None
        try:
            r = self.client.request(method, url, **kw)
        except httpx.HTTPError as e:
            self.limiter.on_result(api, 0)
            self.last_error[api] = type(e).__name__
            return None
        ra = r.headers.get("Retry-After")
        self.limiter.on_result(api, r.status_code, float(ra) if ra and ra.replace(".", "", 1).isdigit() else None)
        if r.status_code != 200:
            self.last_error[api] = f"HTTP {r.status_code}"
            return None
        try:
            return r.json()
        except ValueError:
            self.last_error[api] = "invalid JSON"
            return None

    def get(self, api: str, url: str, **kw):
        return self.request(api, "GET", url, **kw)

    def post(self, api: str, url: str, **kw):
        return self.request(api, "POST", url, **kw)


def fnum(v) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def inum(v) -> int | None:
    try:
        return None if v is None or v == "" else int(v)
    except (TypeError, ValueError):
        return None
