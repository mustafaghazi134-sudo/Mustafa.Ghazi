"""Per-API rate limiting with exponential backoff and jitter for the fast engine's data feeds.

Each upstream API gets its own token bucket and its own backoff state, so a 429 from Jupiter's Price API never slows
Binance or the Solana RPC, and the Swap /order budget is protected separately from price polling.

Behaviour per call:
  1. If the API is inside a backoff window, or its bucket is empty, the call is NOT sent. The wrapper returns a
     FeedResult with status LOCAL_THROTTLE (-1). Callers treat it as "no data" (fail closed).
  2. After a real response: 429, 5xx, or a network error (status 0) starts or extends a backoff:
        delay = min(max_backoff, base * 2**(failures-1)) * jitter, jitter uniform in [0.5, 1.5)
     and Retry-After (seconds), when the server sends it, is a lower bound. Any other response resets the failures.

Budgets are deliberately below published limits. Jupiter Swap: 50 requests / 10 s at $0 executed volume (official
`integrating-jupiter` skill). Jupiter Price/Tokens: "managed at portal level", not quantified publicly; the defaults
assume the free tier and are conservative (UNVERIFIED, adjust in [fast.rate_limits]).
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable

LOCAL_THROTTLE = -1


@dataclass
class ApiLimit:
    per_sec: float              # sustained rate
    burst: int                  # bucket size
    base_backoff_sec: float = 1.0
    max_backoff_sec: float = 60.0


DEFAULT_LIMITS: dict[str, ApiLimit] = {
    "jupiter_swap": ApiLimit(per_sec=3.0, burst=8, base_backoff_sec=2.0),        # 30/10 s vs documented 50/10 s
    "jupiter_price": ApiLimit(per_sec=0.5, burst=3, base_backoff_sec=2.0),       # 30/min; poll every 5 s needs 12/min
    "jupiter_tokens": ApiLimit(per_sec=0.1, burst=2, base_backoff_sec=5.0),      # 6/min; facts refresh every 10 min
    "binance": ApiLimit(per_sec=5.0, burst=10, base_backoff_sec=1.0),            # far below Binance's weight limits
    "solana_rpc": ApiLimit(per_sec=1.0, burst=3, base_backoff_sec=2.0),
}


@dataclass
class _State:
    tokens: float
    last_ts: float
    failures: int = 0
    backoff_until: float = 0.0
    sent: int = 0
    throttled_local: int = 0
    rate_limited: int = 0           # 429s received
    errors: int = 0                 # 5xx / network errors received
    backoffs: list = field(default_factory=list)   # recent backoff delays (s), for tests and reports


class RateLimiter:
    def __init__(self, limits: dict[str, ApiLimit] | None = None, clock: Callable[[], float] = time.time,
                 rng: random.Random | None = None):
        self.limits = dict(DEFAULT_LIMITS)
        self.limits.update(limits or {})
        self.clock = clock
        self.rng = rng or random.Random()
        self._s: dict[str, _State] = {}

    def _state(self, api: str) -> _State:
        st = self._s.get(api)
        if st is None:
            lim = self.limits.get(api) or ApiLimit(per_sec=1.0, burst=2)
            self.limits.setdefault(api, lim)
            st = self._s[api] = _State(tokens=float(lim.burst), last_ts=self.clock())
        return st

    def acquire(self, api: str) -> tuple[bool, str]:
        """(allowed, reason). Consumes a token when allowed."""
        now = self.clock()
        st, lim = self._state(api), self.limits[api]
        if now < st.backoff_until:
            st.throttled_local += 1
            return False, f"backoff {st.backoff_until - now:.1f}s"
        st.tokens = min(float(lim.burst), st.tokens + (now - st.last_ts) * lim.per_sec)
        st.last_ts = now
        if st.tokens < 1.0:
            st.throttled_local += 1
            return False, "budget"
        st.tokens -= 1.0
        st.sent += 1
        return True, ""

    def on_result(self, api: str, status: int, retry_after: float | None = None) -> float:
        """Record an upstream response. Returns the backoff delay applied (0 if none)."""
        st, lim = self._state(api), self.limits[api]
        if status == 429 or status >= 500 or status == 0:
            st.failures += 1
            if status == 429:
                st.rate_limited += 1
            else:
                st.errors += 1
            delay = min(lim.max_backoff_sec, lim.base_backoff_sec * 2 ** (st.failures - 1)) * (0.5 + self.rng.random())
            if retry_after:
                delay = max(delay, float(retry_after))
            st.backoff_until = max(st.backoff_until, self.clock() + delay)
            st.backoffs.append(round(delay, 3))
            del st.backoffs[:-50]
            return delay
        if status != LOCAL_THROTTLE:
            st.failures = 0
        return 0.0

    def in_backoff(self, api: str) -> bool:
        return self.clock() < self._state(api).backoff_until

    def stats(self) -> dict:
        now = self.clock()
        return {api: {"sent": st.sent, "throttled_local": st.throttled_local, "rate_limited_429": st.rate_limited,
                      "errors": st.errors, "in_backoff_sec": round(max(0.0, st.backoff_until - now), 1),
                      "last_backoffs": st.backoffs[-5:]} for api, st in self._s.items()}


FEED_API = {"prices": "jupiter_price", "token_facts": "jupiter_tokens", "discover": "jupiter_tokens",
            "order": "jupiter_swap", "reference_prices": "binance", "slot": "solana_rpc"}


class RateLimitedFeed:
    """Wraps any scanner Feed (LiveFeed, ReplayFeed, test fakes). Every method goes through its API's limiter."""

    def __init__(self, inner, limiter: RateLimiter):
        self.inner = inner
        self.limiter = limiter
        self.jup = getattr(inner, "jup", None)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def _call(self, method: str, *args):
        from ..scanner.scanner import FeedResult
        api = FEED_API[method]
        ok, why = self.limiter.acquire(api)
        if not ok:
            return FeedResult(None, 0.0, LOCAL_THROTTLE, False, 0)
        res = getattr(self.inner, method)(*args)
        self.limiter.on_result(api, res.status, getattr(res, "retry_after", None))
        return res

    def prices(self, mints):
        return self._call("prices", mints)

    def reference_prices(self, pairs):
        return self._call("reference_prices", pairs)

    def slot(self):
        return self._call("slot")

    def token_facts(self, mints):
        return self._call("token_facts", mints)

    def discover(self, category, interval):
        return self._call("discover", category, interval)

    def order(self, in_mint, out_mint, amount_raw, slippage_bps):
        return self._call("order", in_mint, out_mint, amount_raw, slippage_bps)
