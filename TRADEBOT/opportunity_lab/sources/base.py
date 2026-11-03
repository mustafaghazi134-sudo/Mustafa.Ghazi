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
from dataclasses import dataclass
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


@dataclass
class CallResult:
    """What actually happened to one request. `outcome` is one of:
    ok | http_error (4xx/5xx other than 429; `body` holds the parsed JSON body if any) | rate_limited (HTTP 429) |
    throttled_local (our own limiter refused to send it) | network_error | invalid_json.
    Only `ok` and `http_error` are answers from the provider; the others mean "no answer"."""
    api: str
    bucket: str
    ts: float
    outcome: str
    status: int | None = None
    data: object | None = None
    body: object | None = None
    error: str = ""
    attempts: int = 1

    @property
    def answered(self) -> bool:
        return self.outcome in ("ok", "http_error")

    def record(self, source: str) -> dict:
        """Provenance entry: source, request time, HTTP status and outcome."""
        return {"source": source, "ts": round(self.ts, 3), "http": self.status, "outcome": self.outcome,
                "error": self.error[:160], "attempts": self.attempts}


class Http:
    """JSON GET/POST through the shared RateLimiter. Returns None on throttle, 4xx/5xx or network error.

    `call()` returns a CallResult instead and supports a separate limiter bucket per endpoint or RPC method
    (bucket="solana_rpc:getTokenLargestAccounts"), so one refused method no longer puts unrelated lookups into
    backoff, plus a small number of retries with a hard cap on the total wait."""

    def __init__(self, limiter: RateLimiter | None = None, timeout: float = 10.0, user_agent: str = "",
                 transport: httpx.BaseTransport | None = None, clock=time.time, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self.calls: list[CallResult] = []
        self.limiter = limiter or RateLimiter(LAB_LIMITS, clock=clock)
        for k, v in LAB_LIMITS.items():
            self.limiter.limits.setdefault(k, v)
        self.client = httpx.Client(timeout=timeout, headers={"User-Agent": user_agent or "tradebot-opportunity-lab"},
                                   transport=transport, follow_redirects=False)
        self.last_error: dict[str, str] = {}

    def _bucket(self, api: str, bucket: str | None) -> str:
        b = bucket or api
        if b not in self.limiter.limits:
            self.limiter.limits[b] = self.limiter.limits.get(api) or LAB_LIMITS.get(api) or ApiLimit(per_sec=1.0, burst=2)
        return b

    def _once(self, api: str, b: str, method: str, url: str, **kw) -> CallResult:
        now = self.clock()
        ok, why = self.limiter.acquire(b)
        if not ok:
            return CallResult(api, b, now, "throttled_local", error=f"local throttle ({why})")
        try:
            r = self.client.request(method, url, **kw)
        except httpx.HTTPError as e:
            self.limiter.on_result(b, 0)
            return CallResult(api, b, now, "network_error", error=type(e).__name__)
        ra = r.headers.get("Retry-After")
        self.limiter.on_result(b, r.status_code, float(ra) if ra and ra.replace(".", "", 1).isdigit() else None)
        try:
            payload = r.json()
        except ValueError:
            payload = None
        if r.status_code == 429:
            return CallResult(api, b, now, "rate_limited", status=429, body=payload, error="HTTP 429")
        if r.status_code != 200:
            return CallResult(api, b, now, "http_error", status=r.status_code, body=payload, error=f"HTTP {r.status_code}")
        if payload is None:
            return CallResult(api, b, now, "invalid_json", status=200, error="invalid JSON")
        return CallResult(api, b, now, "ok", status=200, data=payload)

    def call(self, api: str, method: str, url: str, *, bucket: str | None = None, retries: int = 0,
             max_wait_sec: float = 4.0, **kw) -> CallResult:
        b = self._bucket(api, bucket)
        waited, res = 0.0, None
        for attempt in range(retries + 1):
            res = self._once(api, b, method, url, **kw)
            res.attempts = attempt + 1
            if res.answered or attempt == retries:
                break
            # wait for this bucket's own backoff (or token refill) if that fits in the cap, else give up now
            st = self.limiter._state(b)
            need = max(st.backoff_until - self.clock(), 0.0) + 0.05
            if st.tokens < 1.0:
                need = max(need, (1.0 - st.tokens) / max(self.limiter.limits[b].per_sec, 1e-9))
            pause = max(need, 1.0 + attempt)
            if waited + pause > max_wait_sec:
                break
            self.sleep(pause)
            waited += pause
        if not (res.outcome == "ok"):
            self.last_error[b] = res.error or res.outcome
        self.calls.append(res)
        if len(self.calls) > 500:
            del self.calls[:250]
        return res

    def request(self, api: str, method: str, url: str, **kw) -> object | None:
        res = self.call(api, method, url, **kw)
        if res.outcome != "ok":
            self.last_error[api] = res.error or res.outcome
            return None
        return res.data

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
