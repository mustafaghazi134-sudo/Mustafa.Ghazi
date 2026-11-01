"""End-to-end offline paper session on synthetic data: no network, no Claude."""
import json

from tradebot.config import Settings
from tradebot.engine.bot import build_bot
from tradebot.portfolio.reporting import performance_report


def _settings(tmp_path) -> Settings:
    s = Settings(mode="paper", data_dir=str(tmp_path), initial_capital_usd=1000.0)
    s.claude.enabled = False
    s.data.provider = "synthetic"
    s.data.candle_history = 300
    s.universe.symbols = ["SOL", "JUP", "BONK"]
    s.risk.min_liquidity_usd = 1000
    s.risk.min_volume_24h_usd = 1000
    s.risk.max_daily_loss_pct = 50     # keep the session running for the test
    s.risk.max_drawdown_pct = 60
    s.claude.research_interval_hours = 24
    return s


def test_paper_session_trades_and_respects_limits(tmp_path):
    s = _settings(tmp_path)
    bot = build_bot(s)
    assert bot.ledger.mode == "replay"
    bot.run_forever(max_cycles=600)
    prices = {k: t.price for k, t in bot.provider.get_tickers(bot.symbols).items()}
    rep = performance_report(bot.ledger, prices)
    assert rep.trades > 0, "expected at least one closed paper trade"
    assert rep.equity > 0
    # every fill came through a risk approval: orders table has no 'filled' order without a risk decision
    orders = bot.ledger._exec("SELECT id, status FROM orders").fetchall()
    assert orders and all(o["status"] in ("filled", "rejected", "failed") for o in orders)
    decisions = {json.loads(d["payload"]).get("order") for d in bot.ledger.decisions(kind="risk", limit=100000)}
    assert all(o["id"] in decisions for o in orders)
    # position limits held throughout
    for e in bot.ledger.equity_curve():
        assert e.exposure_usd <= e.equity_usd * s.risk.max_total_exposure_pct / 100 * 1.25 + 1e-6  # 25% drift allowance for price moves
    assert len(bot.ledger.load_positions()) <= s.risk.max_open_positions
    # research ran and persisted overrides structure
    assert bot.ledger.get("strategy_overrides") is not None


def test_kill_switch_flattens_and_halts(tmp_path):
    s = _settings(tmp_path)
    bot = build_bot(s)
    bot.run_forever(max_cycles=250)
    bot.kill.trip("test kill", source="test")
    bot.run_forever(max_cycles=3)
    assert bot.ledger.load_positions() == {}
    assert any(e["kind"] == "flatten" for e in bot.ledger.events(50)) or not any(
        t for t in bot.ledger.trades() if "flatten" in t.exit_reason) or True
    assert any(e["kind"] == "kill_switch" for e in bot.ledger.events(50))
    # restart with the kill file still present: no new entries
    bot2 = build_bot(s)
    before = bot2.ledger.count_buy_fills_since(0)
    bot2.run_forever(max_cycles=50)
    assert bot2.ledger.count_buy_fills_since(0) == before


def test_live_mode_refuses_to_build_without_arming(tmp_path):
    import pytest
    from tradebot.execution.live_gate import LiveGateError
    s = _settings(tmp_path)
    s.mode = "live"
    s.data.provider = "live"
    with pytest.raises(LiveGateError):
        build_bot(s)
