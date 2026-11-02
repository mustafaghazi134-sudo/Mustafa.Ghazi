"""Fast engine: full automatic BUY -> MANAGE -> SELL cycle through the real RiskEngine and PaperBroker, with the
kill switch, edge re-check before execution, risk rejections, replay over bar data, and the WebSocket parser."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_scanner import BAD, SOL, FakeFeed, _facts  # noqa: E402

from tradebot.config import Settings
from tradebot.fast.engine import FastEngine, fast_report
from tradebot.fast.replay import ReplayFeed, load_bars
from tradebot.fast.ws import BinanceTradeWS, WSReferenceFeed
from tradebot.portfolio.ledger import Ledger
from tradebot.risk.kill_switch import KillSwitch
from tradebot.scanner.store import ScannerStore


def _settings(tmp_path, **fast) -> Settings:
    s = Settings(data_dir=str(tmp_path))
    s.universe.symbols = ["SOL"]
    s.scanner.extra_mints = [BAD]
    s.scanner.probe_interval_sec = 1e12
    s.scanner.min_net_edge_bps = 20.0
    s.scanner.check_slot = False   # the fake feed's slot never advances; staleness is covered in test_scanner
    s.fast.initial_capital_usd = 1000.0
    s.fast.size_usd = 50.0
    for k, v in fast.items():
        setattr(s.fast, k, v)
    return s


def _clock():
    t = [1_800_000_000.0]
    return (lambda: t[0]), (lambda sec: t.__setitem__(0, t[0] + sec))


def _engine(tmp_path, feed, s=None, clock=None):
    s = s or _settings(tmp_path)
    now = clock or _clock()[0]
    ledger = Ledger(tmp_path / "fast.db", mode="paper")
    store = ScannerStore(tmp_path / "scanner.db")
    kill = KillSwitch(tmp_path / "KILL")
    return FastEngine(s, store, feed, ledger, kill, clock=now), ledger, store, kill


def test_refuses_live_mode(tmp_path):
    s = _settings(tmp_path); s.mode = "live"
    with pytest.raises(PermissionError):
        _engine(tmp_path, FakeFeed({SOL: 98.0, BAD: 1.0}, {"SOLUSDT": 100.0}, _facts()), s)


def test_automatic_buy_then_take_profit_sell(tmp_path):
    now, advance = _clock()
    feed = FakeFeed({SOL: 98.0, BAD: 1.0}, {"SOLUSDT": 100.0}, _facts(), cost_bps_per_leg=5.0)   # 2% below reference
    eng, ledger, store, kill = _engine(tmp_path, feed, clock=now)
    out = eng.poll()
    assert out["ok"] and out["opened"] == 1 and out["positions"] == 1
    pos = eng.book.positions["SOL"]
    assert pos.stop_loss < pos.avg_cost < pos.take_profit
    buys = ledger.fills_since(0)
    assert len(buys) == 1 and buys[0]["side"] == "buy" and abs(buys[0]["notional_usd"] - 50.0) < 1e-6
    risk = [json.loads(d["payload"]) for d in ledger.decisions(kind="risk", limit=5)]
    assert risk and risk[0]["approved"] is True
    lat = json.loads(ledger.decisions(kind="latency", limit=1)[0]["payload"])
    assert lat["signal_to_order_ms"] >= 0 and "order_to_fill_ms" in lat
    # price converges: take-profit triggers an automatic reduce-only SELL through risk engine + broker
    feed.px[SOL] = pos.take_profit * 1.001
    advance(5); eng.poll()
    assert "SOL" not in eng.book.positions
    trades = ledger.trades()
    assert len(trades) == 1 and trades[0].exit_reason == "take_profit" and trades[0].pnl_usd > 0 and trades[0].fees_usd > 0
    assert trades[0].exit_ts > trades[0].entry_ts
    txt = fast_report(ledger, store, since_hours=0)
    assert "BUY" in txt and "SELL" in txt and "take_profit" in txt and "signal->order" in txt


def test_stop_loss_and_max_hold_exits(tmp_path):
    now, advance = _clock()
    feed = FakeFeed({SOL: 98.0, BAD: 1.0}, {"SOLUSDT": 100.0}, _facts())
    eng, ledger, store, kill = _engine(tmp_path, feed, clock=now)
    eng.poll(); pos = eng.book.positions["SOL"]
    feed.px[SOL] = pos.stop_loss * 0.999
    advance(5); eng.poll()
    t = ledger.trades()
    assert len(t) == 1 and t[0].exit_reason == "stop_loss" and t[0].pnl_usd < 0
    # new entry after cooldown (loss cooldown is 30 min by default): advance, re-trigger, then let the horizon expire
    advance(1900); feed.px[SOL] = 98.0; eng.cooldown.clear(); eng.poll()
    assert "SOL" in eng.book.positions
    feed.px[SOL] = 98.2
    advance(601); eng.poll()                                  # horizon 300 s x max_hold_mult 2
    assert ledger.trades()[-1].exit_reason == "max_hold"


def test_edge_vanished_at_execution_quote_is_rejected(tmp_path):
    now, _ = _clock()
    feed = FakeFeed({SOL: 98.0, BAD: 1.0}, {"SOLUSDT": 100.0}, _facts(), cost_bps_per_leg=5.0)
    s = _settings(tmp_path)
    s.execution.paper_slippage_bps = 400           # the broker's execution quote is far worse than the scanner's quote
    s.risk.max_slippage_bps = 10_000
    eng, ledger, store, kill = _engine(tmp_path, feed, s, clock=now)
    eng.provider.jup = None                        # force the parametric execution quote
    eng.broker._jup = None
    out = eng.poll()
    assert out["opened"] == 0 and not eng.book.positions
    reasons = [r for d in ledger.decisions(kind="fast_reject", limit=10) for r in json.loads(d["payload"])["reasons"]]
    assert any(r.startswith("edge_vanished") for r in reasons)


def test_risk_engine_blocks_entries_and_kill_switch_flattens(tmp_path):
    now, advance = _clock()
    feed = FakeFeed({SOL: 98.0, BAD: 1.0}, {"SOLUSDT": 100.0}, _facts())
    s = _settings(tmp_path)
    s.risk.max_trades_per_day = 1
    eng, ledger, store, kill = _engine(tmp_path, feed, s, clock=now)
    eng.poll(); assert "SOL" in eng.book.positions
    # kill switch tripped externally (file) -> position flattened, no new entries
    kill.trip("operator", source="test")
    advance(5); eng.poll()
    assert eng.book.positions == {} and ledger.trades()[-1].exit_reason.startswith("flatten")
    eng.cooldown.clear(); advance(5); eng.poll()
    assert ledger.count_buy_fills_since(0) == 1
    kill.reset()
    advance(5); eng.cooldown.clear(); eng.poll()
    reasons = [r for d in ledger.decisions(kind="fast_reject", limit=50) for r in json.loads(d["payload"])["reasons"]]
    assert any("daily trade cap" in r for r in reasons)        # independent risk engine, not the scanner, said no


def test_low_edge_candidates_are_rejected_not_forced(tmp_path):
    now, _ = _clock()
    feed = FakeFeed({SOL: 99.7, BAD: 1.0}, {"SOLUSDT": 100.0}, _facts())   # 30 bps gap -> expected 15 bps < cost
    eng, ledger, store, kill = _engine(tmp_path, feed, clock=now)
    out = eng.poll()
    assert out["opened"] == 0 and ledger.count_buy_fills_since(0) == 0
    reasons = [r for d in ledger.decisions(kind="fast_reject", limit=10) for r in json.loads(d["payload"])["reasons"]]
    assert any(r.startswith("net_edge<") for r in reasons)


def test_replay_runs_full_cycle_on_bar_data(tmp_path):
    # real-format 1m bars: a quiet stretch, a sharp 3% dip (dip_reversion candidate), a recovery
    import math
    t0 = 1_800_000_000.0 - (1_800_000_000.0 % 60)
    rows = []
    px = 100.0
    for i in range(120):
        drift = math.sin(i / 7) * 0.0004
        o = px; px = px * (1 + drift); rows.append((t0 + i * 60, o, max(o, px) * 1.0003, min(o, px) * 0.9997, px))
    for i in range(120, 123):
        o = px; px = px * 0.99; rows.append((t0 + i * 60, o, o, px, px))
    for i in range(123, 150):
        o = px; px = px * 1.0025; rows.append((t0 + i * 60, o, px, o, px))
    csv_path = tmp_path / "SOL-1m.csv"
    with open(csv_path, "w") as f:
        for ts, o, h, l, c in rows:
            f.write(f"{int(ts*1000)},{o},{h},{l},{c},10,{int(ts*1000)+59999},0,1,0,0,0\n")
    bars = load_bars([str(csv_path)])
    assert len(bars) == 150 and bars[0][0] == t0
    s = _settings(tmp_path); s.scanner.extra_mints = []; s.scanner.check_slot = False; s.scanner.poll_sec = 60
    feed = ReplayFeed({"SOL": bars}, {"SOL": s.universe.mints["SOL"]}, s.universe.decimals)
    ledger = Ledger(tmp_path / "fast.db", mode="paper"); store = ScannerStore(tmp_path / "scanner.db"); kill = KillSwitch(tmp_path / "KILL")
    eng = FastEngine(s, store, feed, ledger, kill, clock=feed.now)
    while not feed.finished():
        eng.poll(); feed.advance()
    cands = store.conn.execute("SELECT signal, status, reasons FROM candidates").fetchall()
    assert cands, "replay must at least produce candidates from real-format bars"
    # whatever the outcome, nothing was forced: every fill has a risk approval and every trade is net of fees
    fills = ledger.fills_since(0)
    for f in fills:
        assert f["fee_usd"] > 0
    assert all(json.loads(d["payload"])["approved"] for d in ledger.decisions(kind="risk", limit=1000)) or not fills
    assert "fast engine (PAPER)" in fast_report(ledger, store, since_hours=0)


def test_ws_parser_and_fallback():
    ws = BinanceTradeWS(["SOLUSDT"])
    ws.handle(json.dumps({"stream": "solusdt@trade", "data": {"e": "trade", "s": "SOLUSDT", "p": "101.5", "T": 1_800_000_000_000}}), recv_ts=1_800_000_000.25)
    assert ws.prices["SOLUSDT"] == 101.5 and ws.events == 1 and abs(ws.latency_ms[0] - 250) < 1e-6
    st = ws.stats(); assert st["events"] == 1 and st["latency_p50_ms"] == 250.0
    inner = FakeFeed({SOL: 98.0}, {"SOLUSDT": 100.0}, _facts())
    wrapped = WSReferenceFeed(inner, ws, max_age_sec=1e9)
    assert wrapped.reference_prices(["SOLUSDT"]).data == {"SOLUSDT": 101.5}       # fresh WS price wins
    stale = BinanceTradeWS(["SOLUSDT"]); stale.handle(json.dumps({"data": {"e": "trade", "s": "SOLUSDT", "p": "1"}}), recv_ts=1.0)
    assert WSReferenceFeed(inner, stale, max_age_sec=1.0).reference_prices(["SOLUSDT"]).data == {"SOLUSDT": 100.0}   # REST fallback
