"""Data-access correctness for the Opportunity Lab (docs/LAB_EXPERIMENT_REPORT_20261103.md):

- a 429 on one RPC method must not back off unrelated methods (separate limiter buckets);
- a quote request that got no answer (429, local throttle, timeout, 5xx) is `route_unknown` / retry pending, never
  `no_route` or `honeypot_suspect`; only a provider answer confirming "no route" produces those;
- every lookup records source, time, HTTP status, outcome and state; every decision records its basis
  (evidence / missing_data_only / approved);
- failed lookups are not cached; deferred entries are bounded; deferred exits never start the write-off timer;
- the security thresholds are unchanged.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import httpx
import pytest

from opportunity_lab.agents.security import DATA_GAP_FLAGS, SecurityAnalyst
from opportunity_lab.config import LabConfig
from opportunity_lab.engine import _CachedFacts
from opportunity_lab.models import MintFacts, Opportunity, RouteCheck, TokenSnapshot
from opportunity_lab.sources.base import Http
from opportunity_lab.sources.onchain import OnchainFacts

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_800_000_000.0
MINT = "Tok11111111111111111111111111111111111111111"
MINT_ACCOUNT = {"value": {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "data": {"parsed": {"info": {
    "decimals": 6, "mintAuthority": None, "freezeAuthority": None, "supply": "1000000000000"}}}}}
LARGEST = {"value": [{"amount": "600000000000"}] + [{"amount": "10000000000"}] * 10}


class Api:
    """Mock RPC + Jupiter. `rpc[method]` and `quote[direction]` give the behaviour: ok | 429 | rpc429 | 500 | timeout |
    noroute400 | noroute200."""

    def __init__(self):
        self.rpc = {"getAccountInfo": "ok", "getTokenLargestAccounts": "ok"}
        self.quote = {"buy": "ok", "sell": "ok"}
        self.calls: list[str] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            body = json.loads(req.content)
            m = body["method"]
            self.calls.append(m)
            beh = self.rpc.get(m, "ok")
            if beh == "429":
                return httpx.Response(429, json={"jsonrpc": "2.0", "error": {"code": 429, "message": "Too many requests for a specific RPC call"}})
            if beh == "rpc429":
                return httpx.Response(200, json={"jsonrpc": "2.0", "error": {"code": 429, "message": "Too many requests"}, "id": 1})
            if beh == "500":
                return httpx.Response(500)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": MINT_ACCOUNT if m == "getAccountInfo" else LARGEST})
        if req.url.path.endswith("/tokens/v2/search"):
            return httpx.Response(200, json=[{"id": MINT, "organicScore": 60, "holderCount": 900,
                                              "audit": {"isSus": False, "topHoldersPercentage": 41.2, "mintAuthorityDisabled": True}}])
        if req.url.path.endswith("/swap/v2/order"):
            d = "buy" if req.url.params["inputMint"].startswith("EPjF") else "sell"
            self.calls.append(f"quote:{d}")
            beh = self.quote[d]
            if beh == "429":
                return httpx.Response(429, json={"error": "rate limited"})
            if beh == "500":
                return httpx.Response(503, json={"error": "unavailable"})
            if beh == "timeout":
                raise httpx.ReadTimeout("timed out")
            if beh == "noroute400":
                return httpx.Response(400, json={"error": "No routes found", "errorCode": "COULD_NOT_FIND_ANY_ROUTE"})
            if beh == "noroute200":
                return httpx.Response(200, json={"errorMessage": "Could not find any route", "errorCode": 1})
            amt = int(req.url.params["amount"])
            out = amt * 1000 if d == "buy" else amt // 1000 * 99 // 100
            return httpx.Response(200, json={"outAmount": str(out), "priceImpact": "0.001", "feeBps": 2, "router": "metis"})
        return httpx.Response(404)


@pytest.fixture
def env(tmp_path):
    api = Api()
    now = [T0]
    http = Http(transport=httpx.MockTransport(api.handler), clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
    facts = OnchainFacts(http, "https://rpc.example", use_jupiter=True, clock=lambda: now[0])
    cfg = LabConfig(data_dir=str(tmp_path / "lab"), main_kill_file=str(tmp_path / "KILL"))
    return {"api": api, "http": http, "facts": facts, "cfg": cfg, "now": now}


def opp(liq=200_000.0, age_min=600.0) -> Opportunity:
    s = TokenSnapshot(mint=MINT, observed_ts=T0, source="test", symbol="TOK", dex_id="raydium", quote_symbol="SOL", price_usd=1.0,
                      liquidity_usd=liq, buys_h1=100, sells_h1=80, pair_created_ts=T0 - age_min * 60)
    return Opportunity(id="o1", mint=MINT, detected_ts=T0, signals={"momentum": 1}, snapshot=s, provenance=["test"])


def assess(env, o=None):
    sec = SecurityAnalyst(env["cfg"], env["facts"], env["facts"], lambda m: [], clock=lambda: env["now"][0])
    return sec.assess(o or opp(), 25.0)


def codes(a):
    return {f["code"] for f in a.flags if f["severity"] in ("critical", "high")}


# ------------------------------------------------------------------------------------------------- A. rate limits
def test_429_on_holder_lookup_does_not_block_the_mint_lookup(env):
    env["api"].rpc["getTokenLargestAccounts"] = "429"
    for _ in range(4):                                     # repeated refusals put that ONE method into backoff
        f = env["facts"].mint_facts(MINT)
    assert f.known                                         # getAccountInfo still answered: authorities known
    assert f.data_status["authorities"]["state"] == "confirmed" and f.data_status["authorities"]["http"] == 200
    h = f.data_status["holders"]
    assert h["state"] == "failed" and h["outcome"] in ("rate_limited", "throttled_local")
    assert "solana_rpc:getTokenLargestAccounts" in env["http"].limiter.limits and "solana_rpc:getAccountInfo" in env["http"].limiter.limits


def test_rpc_level_429_inside_http_200_is_treated_as_no_answer(env):
    env["api"].rpc["getTokenLargestAccounts"] = "rpc429"
    f = env["facts"].mint_facts(MINT)
    assert f.top10_pct is None and f.data_status["holders"]["state"] == "failed" and f.data_status["holders"]["outcome"] == "rate_limited"


def test_controlled_retry_recovers_from_a_single_refusal(env):
    seq = iter(["429", "ok"])
    orig = env["api"].handler

    def flaky(req):
        if req.method == "POST" and json.loads(req.content)["method"] == "getTokenLargestAccounts":
            env["api"].rpc["getTokenLargestAccounts"] = next(seq, "ok")
        return orig(req)
    env["http"].client = httpx.Client(transport=httpx.MockTransport(flaky))
    f = env["facts"].mint_facts(MINT)
    assert f.top10_pct is not None and f.data_status["holders"]["state"] == "confirmed" and f.data_status["holders"]["attempts"] == 2


# ------------------------------------------------------------------------------------------------- B. classification
@pytest.mark.parametrize("beh", ["429", "500", "timeout"])
def test_sell_quote_without_an_answer_is_unknown_never_a_honeypot(env, beh):
    env["api"].quote["sell"] = beh
    r = env["facts"].route_check(MINT, 25.0)
    assert r.unknown and r.retry_pending and not r.ok and "retry pending" in r.error
    a = assess(env)
    assert "route_unknown" in codes(a) and "honeypot_suspect" not in codes(a) and "no_route" not in codes(a)
    assert not a.approved                                  # still fail-closed


def test_locally_throttled_sell_quote_is_unknown_never_a_honeypot(env):
    from tradebot.fast.ratelimit import ApiLimit
    env["http"].limiter.limits["jupiter_swap"] = ApiLimit(per_sec=1e-9, burst=1)   # our own budget refuses the calls
    for _ in range(10):
        r = env["facts"].route_check(MINT, 25.0)
    assert r.unknown and r.retry_pending
    a = assess(env)
    assert "honeypot_suspect" not in codes(a) and "no_route" not in codes(a) and "route_unknown" in codes(a)


@pytest.mark.parametrize("beh", ["noroute400", "noroute200"])
def test_confirmed_missing_sell_route_is_flagged_as_honeypot(env, beh):
    env["api"].quote["sell"] = beh
    r = env["facts"].route_check(MINT, 25.0)
    assert not r.ok and not r.unknown and r.buy_out_raw and r.status["sell"]["confirmed_no_route"]
    env["now"][0] += 30                                    # let the quote budget refill
    a = assess(env)
    assert "honeypot_suspect" in codes(a), (a.route.error, a.route.status)


def test_confirmed_missing_buy_route_is_no_route_and_unanswered_buy_is_unknown(env):
    env["api"].quote["buy"] = "noroute400"
    assert "no_route" in codes(assess(env))
    env["now"][0] += 30
    env["api"].quote["buy"] = "500"
    a = assess(env)
    assert "route_unknown" in codes(a) and "no_route" not in codes(a)


# ------------------------------------------------------------------------------------------------- C. provenance + basis
def test_every_lookup_records_source_time_status_and_state(env):
    f = env["facts"].mint_facts(MINT)
    for item in ("authorities", "holders", "indexer"):
        st = f.data_status[item]
        assert {"source", "ts", "http", "outcome", "state"} <= set(st) and st["state"] == "confirmed"
    assert f.indexer_audit["topHoldersPercentage"] == 41.2           # recorded for cross-checking, not used to decide
    r = env["facts"].route_check(MINT, 25.0)
    assert r.ok and r.status["buy"]["http"] == 200 and r.status["sell"]["outcome"] == "ok"


def test_decision_basis_separates_missing_data_from_evidence(env):
    a = assess(env)
    assert a.approved and a.decision_basis == "approved"
    env["now"][0] += 30
    env["api"].rpc["getTokenLargestAccounts"] = "429"
    a = assess(env)
    assert not a.approved and a.decision_basis == "missing_data_only" and codes(a) <= DATA_GAP_FLAGS
    env["now"][0] += 30
    a = assess(env, opp(liq=30_000.0))                     # a real, observed risk as well
    assert not a.approved and a.decision_basis == "evidence" and "low_liquidity" in codes(a)
    assert any(f["code"] == "decision_basis" and f["severity"] == "info" for f in a.flags)


def test_indexer_holder_figure_is_not_used_to_decide(env):
    env["api"].rpc["getTokenLargestAccounts"] = "429"
    a = assess(env)                                        # Jupiter's 41% is present, on-chain holders unknown
    assert "holders_unknown" in codes(a) and "holder_concentration" not in codes(a) and not a.approved


# ------------------------------------------------------------------------------------------------- caching / thresholds
def test_failed_holder_lookup_is_not_cached(env):
    env["api"].rpc["getTokenLargestAccounts"] = "429"
    c = _CachedFacts(env["facts"], 3600, lambda: env["now"][0])
    c.mint_facts(MINT)
    assert MINT not in c.cache
    env["api"].rpc["getTokenLargestAccounts"] = "ok"
    env["now"][0] += 120                                   # let that method's backoff expire
    f = c.mint_facts(MINT)
    assert f.top10_pct is not None and MINT in c.cache


def test_security_thresholds_are_unchanged():
    ex = tomllib.loads((ROOT / "lab.toml.example").read_text())["security"]
    assert ex == {"min_liquidity_usd": 50000, "min_age_min": 30, "max_top10_holder_pct": 35, "max_round_trip_loss_bps": 250}
    c = LabConfig().security
    assert (c.max_price_impact_bps, c.max_round_trip_loss_bps) == (150.0, 250.0)
