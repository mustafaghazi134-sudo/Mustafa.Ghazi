"""Failure-injection tests for the fast engine's production-style reliability layer.

Covers: per-API rate limiting with backoff/jitter/Retry-After; WebSocket heartbeat, stall and reconnect; all-in cost
estimates; the deterministic pre-trade gateway (each limit, and fail-closed on exceptions); order-intent lifecycle and
unique ids; unknown transaction outcomes without duplicate submission; restart recovery and ledger self-checks;
structured JSON logs, latency metrics, alerts and the emergency halt; fail-closed data handling; no Claude calls.
"""
from __future__ import annotations

import json
import random
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_scanner import BAD, SOL, FakeFeed, _facts  # noqa: E402

from tradebot.config import FastRiskConfig, Settings
from tradebot.execution.base import SubmittedUnknownError
from tradebot.fast.broker import FastPaperBroker
from tradebot.fast.costs import estimate_entry, estimate_exit
from tradebot.fast.engine import FastEngine, fast_report
from tradebot.fast.gateway import EntryRequest, GatewayState, PreTradeGateway
from tradebot.fast.ratelimit import LOCAL_THROTTLE, ApiLimit, RateLimitedFeed, RateLimiter
from tradebot.fast.recovery import expected_balances, verify_balances
from tradebot.fast.telemetry import AlertManager, FastHalt, JsonEventLog
from tradebot.fast.ws import BinanceTradeWS, WSReferenceFeed
from tradebot.models import OrderRequest, Side
from tradebot.portfolio.ledger import DuplicateOrderError, IntentTransitionError, Ledger
from tradebot.risk.kill_switch import KillSwitch
from tradebot.scanner.costs import Quote
from tradebot.scanner.scanner import FeedResult
from tradebot.scanner.store import ScannerStore

ROOT = Path(__file__).resolve().parent.parent


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, sec: float) -> None:
        self.t += sec


def _settings(tmp_path, **fast_risk) -> Settings:
    s = Settings(data_dir=str(tmp_path))
    s.universe.symbols = ["SOL"]
    s.scanner.extra_mints = [BAD]
    s.scanner.min_net_edge_bps = 20.0
    for k, v in fast_risk.items():
        setattr(s.fast_risk, k, v)
    return s


def _engine(tmp_path, feed, clock, s=None, **kw):
    s = s or _settings(tmp_path)
    ledger = Ledger(tmp_path / "fast.db", mode="paper")
    eng = FastEngine(s, ScannerStore(tmp_path / "scanner.db"), feed, ledger, KillSwitch(tmp_path / "KILL"), clock=clock, **kw)
    return eng, ledger


def _feed():
    return FakeFeed({SOL: 98.0, BAD: 1.0}, {"SOLUSDT": 100.0}, _facts(), cost_bps_per_leg=5.0)   # 2% below reference


def _events(tmp_path) -> list[dict]:
    p = tmp_path / "fast_events.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def _books_agree(eng) -> bool:
    return verify_balances(eng.ledger, eng.book) == []


# ===================================================================================== 1. rate limiting
def test_backoff_is_exponential_with_jitter_and_honours_retry_after():
    c = Clock()
    lim = RateLimiter({"x": ApiLimit(per_sec=100, burst=100, base_backoff_sec=1.0, max_backoff_sec=8.0)}, c, random.Random(7))
    delays = [lim.on_result("x", 429) for _ in range(6)]
    for n, d in enumerate(delays, start=1):
        base = min(8.0, 2 ** (n - 1))
        assert 0.5 * base <= d < 1.5 * base                    # exponential, capped, jitter in [0.5, 1.5)
    assert len(set(delays)) == len(delays)                      # jitter actually varies
    assert lim.on_result("x", 503, retry_after=30) >= 30        # Retry-After is a floor
    assert not lim.acquire("x")[0] and lim.in_backoff("x")
    c.advance(31)
    assert lim.acquire("x")[0]
    lim.on_result("x", 200)
    assert 0.5 <= lim.on_result("x", 429) < 1.5                 # success reset the failure count


def test_token_bucket_and_per_api_isolation():
    c = Clock()
    lim = RateLimiter({"a": ApiLimit(per_sec=1, burst=3), "b": ApiLimit(per_sec=1, burst=3)}, c)
    assert [lim.acquire("a")[0] for _ in range(4)] == [True, True, True, False]
    c.advance(1.0)
    assert lim.acquire("a")[0] and not lim.acquire("a")[0]
    lim.on_result("a", 429)
    assert lim.in_backoff("a") and not lim.in_backoff("b") and lim.acquire("b")[0]
    st = lim.stats()
    assert st["a"]["rate_limited_429"] == 1 and st["a"]["throttled_local"] >= 2


def test_rate_limited_feed_does_not_call_upstream_during_backoff():
    c = Clock()
    feed = _feed()
    calls = []
    real = feed.prices
    feed.prices = lambda m: calls.append(1) or FeedResult(None, 5.0, 429, False, retry_after=10)
    rl = RateLimitedFeed(feed, RateLimiter(clock=c, rng=random.Random(1)))
    assert rl.prices([SOL]).status == 429 and len(calls) == 1
    for _ in range(5):
        c.advance(1)
        assert rl.prices([SOL]).status == LOCAL_THROTTLE       # suppressed locally: nothing sent
    assert len(calls) == 1
    feed.prices = real
    c.advance(10)
    assert rl.prices([SOL]).ok


def test_429_storm_never_trips_any_kill_switch_and_trading_resumes(tmp_path):
    c = Clock()
    feed = _feed()
    real, sent = feed.prices, []
    feed.prices = lambda m: sent.append(c()) or FeedResult(None, 20.0, 429, False)
    eng, ledger = _engine(tmp_path, feed, c, limiter=RateLimiter(clock=c, rng=random.Random(3)))
    for _ in range(24):                                         # 2 minutes of 429s at a 5 s poll
        out = eng.poll(); c.advance(5)
        assert out["ok"] is False and out["opened"] == 0
    assert not (tmp_path / "KILL").exists(), "a feed outage must never write the shared kill file (it stops the 4h engine)"
    assert not (tmp_path / "FAST_HALT").exists() and not eng.kill.is_tripped()
    assert len(sent) < 24 / 2, f"backoff should suppress most retries, sent {len(sent)} of 24"
    alerts = [e for e in _events(tmp_path) if e["event"] == "alert" and e["key"] == "data_outage"]
    assert len(alerts) == 1                                     # deduplicated
    assert ledger._exec("SELECT COUNT(*) n FROM intents").fetchone()["n"] == 0
    feed.prices = real
    feed.px[SOL] = 97.9                                         # market moves on; prices flow again
    opened = 0
    for _ in range(12):
        opened += eng.poll()["opened"]; c.advance(5)
        if opened:
            break
    assert opened == 1 and "SOL" in eng.book.positions and _books_agree(eng)


# ===================================================================================== 2. WebSocket
def test_websocket_detects_stalls_and_drops_and_reconnects_with_backoff():
    pytest.importorskip("websockets")
    from websockets.sync.server import serve
    conn = {"n": 0}

    def handler(ws):
        conn["n"] += 1
        n = conn["n"]
        for _ in range(3):
            ws.send(json.dumps({"stream": "solusdt@trade", "data": {"e": "trade", "s": "SOLUSDT", "p": str(100 + n), "T": int(time.time() * 1000)}}))
        if n == 1:
            time.sleep(2)            # silent half-open socket: must be detected as a stall
        elif n == 2:
            return                   # server drops the connection
        else:
            time.sleep(3)

    srv = serve(handler, "127.0.0.1", 0)
    port = srv.socket.getsockname()[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ws = BinanceTradeWS(["SOLUSDT"], url=f"ws://127.0.0.1:{port}/stream", reconnect_sec=0.05, stall_sec=0.4,
                        ping_interval=0.2, ping_timeout=1.0, proxy=None, rng=random.Random(2))
    ws.start()
    deadline = time.time() + 10
    while time.time() < deadline and ws.connects < 3:
        time.sleep(0.05)
    time.sleep(0.2)
    ws.stop(); srv.shutdown()
    st = ws.stats()
    assert st["connects"] >= 3 and st["stalls"] >= 1 and st["errors"] >= 1, st
    assert ws.prices["SOLUSDT"] >= 103                          # data from a later connection arrived
    assert all(0 < d < 61 * 1.5 for d in ws.reconnect_delays)


def test_websocket_passes_heartbeat_settings_to_the_client(monkeypatch):
    pytest.importorskip("websockets")
    seen = {}

    def fake_connect(url, **kw):
        seen.update(kw)
        raise OSError("refused")

    monkeypatch.setattr("websockets.sync.client.connect", fake_connect)
    ws = BinanceTradeWS(["SOLUSDT"], ping_interval=20, ping_timeout=10, reconnect_sec=0.01, rng=random.Random(0))
    ws.start(); time.sleep(0.1); ws.stop()
    assert seen["ping_interval"] == 20 and seen["ping_timeout"] == 10 and ws.errors >= 1


def test_stale_websocket_prices_are_never_used():
    ws = BinanceTradeWS(["SOLUSDT"])
    ws.handle(json.dumps({"data": {"e": "trade", "s": "SOLUSDT", "p": "101"}}), recv_ts=1000.0)
    assert ws.fresh_price("SOLUSDT", 5, now=1003.0) == 101.0 and ws.fresh_price("SOLUSDT", 5, now=1006.0) is None
    rest = FakeFeed({SOL: 98.0}, {"SOLUSDT": 100.0}, _facts())
    assert WSReferenceFeed(rest, ws, max_age_sec=5).reference_prices(["SOLUSDT"]).data == {"SOLUSDT": 100.0}   # REST fallback
    rng_ws = BinanceTradeWS(["X"], reconnect_sec=1, max_reconnect_sec=8, rng=random.Random(4))
    ds = [rng_ws.next_delay() for _ in range(6)]
    assert all(0.5 * min(8, 2 ** i) <= d < 1.5 * min(8, 2 ** i) for i, d in enumerate(ds))


# ===================================================================================== 3. cost estimator
def _q(inp, out, mn, impact=0.0, fee_bps=None, sig=None, prio=None, rent=None, ts=0.0):
    return Quote(inp, out, mn, impact, fee_bps, ts=ts, signature_fee_lamports=sig, priority_fee_lamports=prio, rent_fee_lamports=rent)


def test_entry_estimate_includes_network_priority_rent_and_slippage():
    # $100 -> 0.5 SOL (out) / 0.4975 (min); 0.5 SOL -> $99.50 (out) / $99.00 (min); SOL at $150
    buy = _q(100_000_000, 500_000_000, 497_500_000, 0.0005, 5, sig=5000, prio=100_000, rent=2_039_280)
    sell = _q(500_000_000, 99_500_000, 99_000_000, 0.0004, 5, sig=5000, prio=100_000)
    e = estimate_entry("SOL", buy, sell, 100.0, 9, 150.0, adverse_bps=10.0, now=1.0)
    leg_fee = (5000 + 100_000) / 1e9 * 150
    assert e.network_fee_usd == pytest.approx(2 * leg_fee) and e.rent_usd == pytest.approx(2_039_280 / 1e9 * 150)
    assert e.spread_impact_bps == pytest.approx(50.0)                         # 1 - 99.5/100
    assert e.total_expected_bps == pytest.approx(50 + (2 * leg_fee + e.rent_usd) / 100 * 1e4 + 10)
    worst_back = 99.0 * (0.4975 / 0.5)
    assert e.total_worst_bps == pytest.approx(e.total_expected_bps + (99.5 - worst_back) / 100 * 1e4)
    assert e.fee_source == "quote_lamports" and e.total_worst_usd == pytest.approx(e.total_worst_bps / 1e4 * 100)
    # without lamport fields: assumed $0.01 per transaction, no rent
    e2 = estimate_entry("SOL", _q(100_000_000, 500_000_000, 497_500_000), _q(500_000_000, 99_500_000, 99_000_000), 100.0, 9, None, 0.0, 1.0)
    assert e2.network_fee_usd == pytest.approx(0.02) and e2.rent_usd == 0 and e2.fee_source == "assumed"


def test_exit_estimate_versus_mark():
    sell = _q(500_000_000, 99_000_000, 98_500_000, 0.001, sig=5000, prio=0)
    e = estimate_exit("SOL", sell, 0.5, 200.0, 150.0, now=0.0)
    assert e.size_usd == 100.0 and e.spread_impact_bps == pytest.approx(100.0)
    assert e.total_worst_bps == pytest.approx(100.0 + 5000 / 1e9 * 150 / 100 * 1e4 + 50.0)


# ===================================================================================== 4. pre-trade gateway
def _state(**kw) -> GatewayState:
    base = dict(now=10_000.0, equity_usd=1000, cash_usd=1000, positions={}, exposure_usd=0.0, pending_symbols=frozenset(),
                realized_today_usd=0.0, unrealized_usd=0.0, peak_equity_usd=1000, entries_last_hour=0, entries_today=0,
                last_entry_ts=None, last_entry_by_symbol={}, consecutive_losses=0, last_loss_ts=None)
    base.update(kw)
    return GatewayState(**base)


def _req(**kw) -> EntryRequest:
    base = dict(symbol="SOL", notional_usd=50.0, stop_pct=1.0, entry_cost_worst_bps=60.0, price_age_sec=1.0, quote_age_sec=0.5)
    base.update(kw)
    return EntryRequest(**base)


@pytest.mark.parametrize("req_kw,state_kw,expect", [
    ({"price_age_sec": None}, {}, "stale_price"),
    ({"price_age_sec": 99}, {}, "stale_price"),
    ({"quote_age_sec": 9}, {}, "stale_quote"),
    ({"reference_age_sec": 30}, {}, "stale_reference"),
    ({}, {"realized_today_usd": -10, "unrealized_usd": -6}, "daily_loss"),
    ({}, {"peak_equity_usd": 1050, "equity_usd": 1000}, "drawdown"),
    ({}, {"consecutive_losses": 3, "last_loss_ts": 9_000.0}, "loss_streak"),
    ({}, {"entries_last_hour": 4}, "trades_per_hour"),
    ({}, {"entries_today": 20}, "trades_per_day"),
    ({}, {"last_entry_ts": 9_990.0}, "entry_spacing"),
    ({}, {"last_entry_by_symbol": {"SOL": 9_800.0}}, "symbol_cooldown"),
    ({}, {"positions": {"SOL": 1.0}}, "already_exposed_to_symbol"),
    ({}, {"pending_symbols": frozenset({"SOL"})}, "already_exposed_to_symbol"),
    ({}, {"positions": {"ETH": 1.0}, "pending_symbols": frozenset({"BTC"})}, "open_positions"),
    ({"entry_cost_worst_bps": 200}, {}, "entry_cost"),
    ({}, {"exposure_usd": 198.0}, "size_below_min"),
])
def test_gateway_rejects_each_limit(req_kw, state_kw, expect):
    d = PreTradeGateway(FastRiskConfig()).check_entry(_req(**req_kw), _state(**state_kw))
    assert not d.approved and any(r.startswith(expect) for r in d.reasons), d.reasons


def test_gateway_sizes_to_the_tightest_limit_and_bounds_loss():
    g = PreTradeGateway(FastRiskConfig(max_loss_per_trade_usd=0.5))
    d = g.check_entry(_req(notional_usd=100.0, stop_pct=1.0, entry_cost_worst_bps=100.0), _state())
    assert d.approved and d.max_notional_usd == pytest.approx(25.0) and d.max_loss_usd == pytest.approx(0.5)   # 0.5 / 2%
    assert g.check_exit("SOL", 2.0, _state(positions={"SOL": 1.0})).approved is False
    assert g.check_exit("SOL", 1.0, _state(positions={"SOL": 1.0})).approved


def test_gateway_fails_closed_on_internal_error():
    bad = _state(); bad.positions = None                        # corrupted snapshot -> TypeError inside
    d = PreTradeGateway(FastRiskConfig()).check_entry(_req(), bad)
    assert not d.approved and d.reasons[0].startswith("risk_service_unavailable")


@pytest.mark.parametrize("target", ["tradebot.fast.engine.build_state", "risk"])
def test_engine_rejects_and_alerts_when_a_risk_service_is_unavailable(tmp_path, monkeypatch, target):
    c = Clock()
    eng, ledger = _engine(tmp_path, _feed(), c)
    if target == "risk":
        monkeypatch.setattr(eng.risk, "evaluate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("risk engine down")))
    else:
        monkeypatch.setattr(target, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ledger locked")))
    out = eng.poll()
    assert out["opened"] == 0 and not eng.book.positions
    assert ledger._exec("SELECT COUNT(*) n FROM intents").fetchone()["n"] == 0
    reasons = [r for d in ledger.decisions(kind="fast_reject", limit=10) for r in json.loads(d["payload"])["reasons"]]
    assert any(r.startswith("risk_service_unavailable") for r in reasons)
    assert any(e["event"] == "alert" and e["key"] == "risk_service_unavailable" for e in _events(tmp_path))


# ===================================================================================== 5. intents and order ids
def test_intent_lifecycle_is_enforced_and_logged(tmp_path):
    led = Ledger(tmp_path / "l.db")
    o = OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=10)
    iid = led.create_intent(o, 0.1, 10.0, ts=1.0)
    with pytest.raises(DuplicateOrderError):
        led.create_intent(o, 0.1, 10.0, ts=2.0)                 # same order id: refused
    led.update_intent(iid, state="signed", tx_signature="S")
    led.update_intent(iid, state="confirmed")
    for bad in ("signed", "failed", "created"):
        with pytest.raises(IntentTransitionError):
            led.update_intent(iid, state=bad)
    assert [(r["from_state"], r["to_state"]) for r in led.intent_history(iid)] == [(None, "created"), ("created", "signed"), ("signed", "confirmed")]
    assert led.unique_order_ids
    ids = {OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=1).id for _ in range(5000)}
    assert len(ids) == 5000


# ===================================================================================== 6. unknown outcomes and timeouts
class UnknownOnceBroker(FastPaperBroker):
    """The first BUY 'times out after broadcast'; later the chain answers whatever `status` says."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.executions, self.status, self.valid = 0, "not_found", None

    def _execute(self, order, qty, q):
        self.executions += 1
        if order.side == Side.BUY and self.executions == 1:
            if self.presubmit_hook:
                self.presubmit_hook({"request_id": "r1", "tx_signature": "SIG-UNKNOWN", "signed_tx": ""})
            raise SubmittedUnknownError("timeout after broadcast", "SIG-UNKNOWN", "r1")
        return super()._execute(order, qty, q)

    def transaction_status(self, sig, signed):
        if sig == "SIG-UNKNOWN":
            return {"status": self.status, "blockhash_valid": self.valid}
        return super().transaction_status(sig, signed)


def _unknown_engine(tmp_path, c):
    s = _settings(tmp_path, symbol_cooldown_sec=0, min_seconds_between_entries=0)
    feed = _feed()
    led = Ledger(tmp_path / "fast.db")
    eng = FastEngine(s, ScannerStore(tmp_path / "scanner.db"), feed, led, KillSwitch(tmp_path / "KILL"), clock=c)
    eng.broker = UnknownOnceBroker(eng.risk, eng.provider, eng.feed, eng._resolve, eng._sol_usd, c)
    return eng, led, feed


@pytest.mark.parametrize("resolution", ["confirmed", "expired"])
def test_unknown_outcome_is_never_resubmitted_and_resolves_on_evidence(tmp_path, resolution):
    c = Clock()
    eng, led, feed = _unknown_engine(tmp_path, c)
    eng.poll()
    assert not eng.book.positions and led.pending_intents("SOL")[0]["state"] == "submitted"
    for _ in range(5):                                          # the signal persists; nothing may be resubmitted
        c.advance(5); eng.cooldown.clear(); feed.px[SOL] = 98.0 - 0.01 * _
        eng.poll()
    assert eng.broker.executions == 1 and len(led.pending_intents("SOL")) == 1
    if resolution == "confirmed":
        eng.broker.status = "confirmed"
    else:
        eng.broker.valid = False                                # blockhash expired: it can never land
    c.advance(5); eng.poll()
    row = led._exec("SELECT state FROM intents WHERE tx_signature='SIG-UNKNOWN'").fetchone()
    assert row["state"] == resolution
    assert ("SOL" in eng.book.positions) == (resolution == "confirmed")
    assert len(led._exec("SELECT * FROM fills").fetchall()) == (1 if resolution == "confirmed" else 0)
    assert _books_agree(eng)
    c.advance(5); eng.poll()                                    # reconciliation is idempotent
    assert len(led._exec("SELECT * FROM fills WHERE tx_signature='SIG-UNKNOWN'").fetchall()) == (1 if resolution == "confirmed" else 0)


def test_quote_timeouts_fail_closed_without_any_order(tmp_path):
    c = Clock()
    feed = _feed()
    feed.order = lambda *a: FeedResult(None, 5000.0, 0, False)  # network timeout
    eng, led = _engine(tmp_path, feed, c)
    out = eng.poll()
    assert out["opened"] == 0 and led._exec("SELECT COUNT(*) n FROM intents").fetchone()["n"] == 0
    reasons = [r for d in led.decisions(kind="fast_reject", limit=10) for r in json.loads(d["payload"])["reasons"]]
    assert "quote_unavailable" in reasons


def test_pre_execution_quote_failure_rejects_without_an_intent(tmp_path):
    c = Clock()
    feed = _feed()
    real = feed.order
    feed.order = lambda *a: real(*a) if len(feed.order_calls) < 2 else (feed.order_calls.append({}), FeedResult(None, 1.0, 0, False))[1]
    eng, led = _engine(tmp_path, feed, c)
    assert eng.poll()["opened"] == 0
    reasons = [r for d in led.decisions(kind="fast_reject", limit=10) for r in json.loads(d["payload"])["reasons"]]
    assert any(r.startswith("pre_exec_quote_unavailable") for r in reasons)
    assert led._exec("SELECT COUNT(*) n FROM intents").fetchone()["n"] == 0


# ===================================================================================== 7. restart recovery
def test_restart_restores_position_and_manages_it_to_exit(tmp_path):
    c = Clock()
    feed = _feed()
    eng, led = _engine(tmp_path, feed, c)
    eng.poll()
    pos = eng.book.positions["SOL"]
    tp, qty, cash = pos.take_profit, pos.qty, eng.book.cash
    del eng                                                     # "process dies"
    eng2, led2 = _engine(tmp_path, feed, c)
    assert eng2.book.positions["SOL"].qty == pytest.approx(qty) and eng2.book.cash == pytest.approx(cash)
    start = [e for e in _events(tmp_path) if e["event"] == "startup"][-1]
    assert start["balance_check"] == "ok" and start["positions"] == {"SOL": pytest.approx(qty)}
    feed.px[SOL] = tp * 1.002
    c.advance(5); eng2.poll()
    assert not eng2.book.positions and led2.trades()[-1].exit_reason == "take_profit" and _books_agree(eng2)


def test_restart_with_a_signed_but_unbooked_paper_order_expires_it(tmp_path):
    c = Clock()
    eng, led = _engine(tmp_path, _feed(), c)
    o = OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=50)
    iid = led.create_intent(o, 0.5, 50.0, ts=c())
    led.update_intent(iid, state="signed", tx_signature="paper-crashed")     # crashed after signing, before booking
    eng2, _ = _engine(tmp_path, _feed(), c)
    assert led.get_intent(iid)["state"] == "expired" and not eng2.book.positions and _books_agree(eng2)


def test_ledger_corruption_at_startup_halts_the_fast_engine_only(tmp_path):
    c = Clock()
    eng, led = _engine(tmp_path, _feed(), c)
    eng.poll()
    led.set("cash", led.get("cash") + 25.0)                     # tamper: cash no longer matches the fill history
    eng2, _ = _engine(tmp_path, _feed(), c)
    assert (tmp_path / "FAST_HALT").exists() and not (tmp_path / "KILL").exists()
    assert any(e["event"] == "alert" and e["key"] == "balance_mismatch" for e in _events(tmp_path))
    c.advance(400); eng2.cooldown.clear()
    assert eng2.poll()["opened"] == 0 and "halted" in eng2.poll()


# ===================================================================================== 8. telemetry and halt
def test_json_events_latency_and_balances_are_recorded(tmp_path):
    c = Clock()
    feed = _feed()
    eng, led = _engine(tmp_path, feed, c)
    eng.poll()
    feed.px[SOL] = eng.book.positions["SOL"].take_profit * 1.002
    c.advance(5); eng.poll()
    ev = _events(tmp_path)
    kinds = [e["event"] for e in ev]
    for k in ("startup", "entry", "fill", "exit"):
        assert k in kinds, kinds
    assert all({"ts", "iso", "level", "event"} <= set(e) for e in ev)
    fills = [e for e in ev if e["event"] == "fill"]
    assert [f["side"] for f in fills] == ["buy", "sell"] and all("cash" in f and "positions" in f for f in fills)
    entry = next(e for e in ev if e["event"] == "entry")
    assert entry["cost"]["total_worst_bps"] > 0 and "signal_to_order_ms" in entry["latency"]
    lat = led.get("latency_summary")
    assert {"quote_ms", "gateway_ms", "risk_ms", "requote_ms", "signal_to_order_ms", "order_to_fill_ms"} <= set(lat)


def test_alerts_are_deduplicated_and_notified(tmp_path):
    c = Clock()
    got = []
    am = AlertManager(JsonEventLog(tmp_path / "e.jsonl", c), None, c, dedupe_sec=60, notify=got.append)
    assert am.alert("k", "m") and not am.alert("k", "m")
    c.advance(61)
    assert am.alert("k", "m") and len(got) == 2 and am.raised == 2


def test_emergency_halt_flattens_opens_nothing_and_spares_the_4h_engine(tmp_path):
    from tradebot.cli import main
    c = Clock()
    feed = _feed()
    eng, led = _engine(tmp_path, feed, c, s=_settings(tmp_path, symbol_cooldown_sec=0, min_seconds_between_entries=0))
    eng.poll()
    assert "SOL" in eng.book.positions
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'mode = "paper"\ndata_dir = "{tmp_path.as_posix()}"\n')
    assert main(["--config", str(cfg), "fast-halt", "--reason", "operator test"]) == 0
    feed.px[SOL] = 98.1
    c.advance(5); out = eng.poll()
    assert "halted" in out and not eng.book.positions and led.trades()[-1].exit_reason.startswith("flatten")
    assert not (tmp_path / "KILL").exists()                     # the 4h engine's switch is untouched
    c.advance(400); eng.cooldown.clear(); feed.px[SOL] = 97.0
    assert eng.poll()["opened"] == 0
    assert main(["--config", str(cfg), "fast-resume"]) == 0
    c.advance(1801); eng.cooldown.clear(); feed.px[SOL] = 96.9   # past the risk engine's 30-min post-loss cooldown (the flatten lost $0.12)
    assert eng.poll()["opened"] == 1 and _books_agree(eng)
    KillSwitch(tmp_path / "KILL").trip("shared kill", source="test")   # the shared kill also stops the fast engine
    feed.px[SOL] = 97.0
    c.advance(5); assert "halted" in eng.poll() and not eng.book.positions


# ===================================================================================== 9. fail-closed data handling
def test_unchanged_price_is_treated_as_stale(tmp_path):
    c = Clock()
    feed = _feed()
    feed.px[SOL] = 99.9                                         # no signal on first poll
    eng, led = _engine(tmp_path, feed, c)
    eng.poll()
    feed.px[SOL] = 99.9
    for _ in range(4):
        c.advance(5); eng.poll()                                # price and blockId frozen for 20 s
    feed.ref_override = True
    feed.refs["SOLUSDT"] = 102.0                                # a 2% gap appears, but the venue price is frozen
    c.advance(5); eng.cooldown.clear(); out = eng.poll()
    assert out["opened"] == 0
    reasons = [r for d in led.decisions(kind="fast_reject", limit=10) for r in json.loads(d["payload"])["reasons"]]
    assert any(r.startswith("stale_price") for r in reasons)


def test_missing_token_facts_fail_closed(tmp_path):
    c = Clock()
    feed = _feed()
    feed.token_facts = lambda m: FeedResult(None, 1.0, 500, False)
    eng, led = _engine(tmp_path, feed, c)
    assert eng.poll()["opened"] == 0
    reasons = [r for d in led.decisions(kind="fast_reject", limit=10) for r in json.loads(d["payload"])["reasons"]]
    assert "no_token_facts" in reasons


# ===================================================================================== 10. no Claude in the loop
def test_fast_execution_path_has_no_claude_or_llm_calls():
    for folder in ("tradebot/fast", "tradebot/scanner"):
        for py in (ROOT / folder).glob("*.py"):
            src = py.read_text()
            assert "anthropic" not in src and "claude_analyst" not in src and "ClaudeAnalyst" not in src, py.name


# ===================================================================================== lifecycle with balance checks
def test_buy_manage_sell_lifecycle_balances_verified_after_every_step(tmp_path):
    c = Clock()
    feed = _feed()
    eng, led = _engine(tmp_path, feed, c)
    initial = eng.book.cash

    def check(step):
        exp_cash, exp_pos = expected_balances(led, eng.book.initial_capital)
        assert verify_balances(led, eng.book) == [], step
        assert eng.book.cash == pytest.approx(exp_cash, abs=1e-9) == pytest.approx(led.get("cash", initial), abs=1e-9)
        return exp_cash, exp_pos

    check("start")
    eng.poll()                                                  # BUY
    cash1, pos1 = check("after buy")
    buy = led._exec("SELECT * FROM fills WHERE side='buy'").fetchone()
    assert cash1 == pytest.approx(initial - buy["notional_usd"]) and pos1["SOL"] == pytest.approx(buy["qty"])
    tp = eng.book.positions["SOL"].take_profit
    for px in (98.3, 98.6, 99.0):                               # MANAGE: marks move, nothing is traded
        feed.px[SOL] = px
        c.advance(5); eng.poll()
        cash_m, pos_m = check(f"manage {px}")
        assert cash_m == pytest.approx(cash1) and pos_m == pos1 and px < tp
        assert eng.book.equity({"SOL": px}) == pytest.approx(cash1 + pos1["SOL"] * px)
    feed.px[SOL] = tp * 1.001                                   # SELL at take-profit
    c.advance(5); eng.poll()
    cash2, pos2 = check("after sell")
    sell = led._exec("SELECT * FROM fills WHERE side='sell'").fetchone()
    tr = led.trades()[-1]
    assert pos2 == {} and cash2 == pytest.approx(cash1 + sell["notional_usd"] - sell["fee_usd"])
    assert tr.pnl_usd == pytest.approx(cash2 - initial) and tr.exit_reason == "take_profit"
    assert [r["state"] for r in led.intents(10)] == ["confirmed", "confirmed"]
    assert "ledger self-check: OK" in fast_report(led, ScannerStore(tmp_path / "scanner.db"), since_hours=0)
