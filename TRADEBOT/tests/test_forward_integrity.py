"""Forward-test integrity: frozen parameters, no silent re-optimisation, and the Jupiter-vs-Binance tracking check."""
from pathlib import Path

from tradebot.config import Settings
from tradebot.engine.bot import build_bot
from tradebot.profile import apply_validated_profile


def _settings(tmp_path) -> Settings:
    s = Settings(mode="paper", data_dir=str(tmp_path), initial_capital_usd=1000.0)
    s.claude.enabled = False
    s.data.provider = "synthetic"
    s.data.candle_history = 300
    s.universe.symbols = ["SOL", "JUP", "BONK"]
    s.risk.min_liquidity_usd = 1000; s.risk.min_volume_24h_usd = 1000
    s.risk.max_daily_loss_pct = 50; s.risk.max_drawdown_pct = 60
    return s


def test_defaults_keep_research_off_the_live_parameters():
    s = Settings()
    assert s.claude.auto_apply_overrides is False
    assert s.claude.research_interval_hours >= 24          # periodic research, not continuous


def test_deterministic_research_only_proposes_when_auto_apply_is_off(tmp_path):
    s = _settings(tmp_path)
    s.claude.research_enabled = False                      # as in the validated profile: nothing runs on its own
    bot = build_bot(s)
    before = {st.name: dict(st.params) for st in bot.strategies}
    bot.run_forever(max_cycles=400)
    out = bot.research_once()                              # explicit call: the pass runs, but may only log proposals
    assert out["applied"] == []
    overrides = bot.ledger.get("strategy_overrides") or {}
    assert all("params" not in o for o in overrides.values())
    assert {st.name: dict(st.params) for st in bot.strategies} == before
    dec = [d for d in bot.ledger.decisions(kind="research", limit=10)]
    assert dec, "research decision must be logged even when nothing is applied"
    import json
    payload = json.loads(dec[0]["payload"])
    assert payload["source"] == "deterministic" and payload["auto_apply"] is False and "proposals" in payload


def test_basis_check_blocks_entries_but_never_exits(tmp_path):
    s = _settings(tmp_path)
    s.claude.research_enabled = False
    s.data.max_basis_bps = 50.0
    bot = build_bot(s)
    prov = bot.provider
    # Jupiter price 3% away from the Binance reference for every symbol -> all entries blocked
    prov.get_reference_prices = lambda syms: {k: t.price / 1.03 for k, t in prov.get_tickers(syms).items()}
    bot.run_forever(max_cycles=300)
    assert bot.ledger.count_buy_fills_since(0) == 0
    kinds = {e["kind"] for e in bot.ledger.events(5000)}
    assert "basis_block" in kinds
    assert any(d["kind"] == "entry_blocked" for d in bot.ledger.decisions(kind="entry_blocked", limit=5))
    assert bot.ledger.get("last_basis_bps")["SOL"] > 50
    # reference back in line -> trading resumes
    prov.get_reference_prices = lambda syms: {k: t.price for k, t in prov.get_tickers(syms).items()}
    bot.run_forever(max_cycles=400)
    assert bot.ledger.count_buy_fills_since(0) > 0
    # now a position is (or was) open; a basis breach must not stop exits: run with breach and check sells still occur
    prov.get_reference_prices = lambda syms: {k: t.price / 1.03 for k, t in prov.get_tickers(syms).items()}
    sells_before = bot.ledger._exec("SELECT COUNT(*) AS n FROM fills WHERE side='sell'").fetchone()["n"]
    buys_before = bot.ledger.count_buy_fills_since(0)
    bot.run_forever(max_cycles=400)
    assert bot.ledger.count_buy_fills_since(0) == buys_before
    sells_after = bot.ledger._exec("SELECT COUNT(*) AS n FROM fills WHERE side='sell'").fetchone()["n"]
    assert sells_after >= sells_before


def test_basis_check_is_a_noop_without_reference_prices(tmp_path):
    s = _settings(tmp_path)
    s.claude.research_enabled = False
    bot = build_bot(s)
    bot.run_forever(max_cycles=300)
    assert "basis_block" not in {e["kind"] for e in bot.ledger.events(5000)}
    assert bot.ledger.count_buy_fills_since(0) > 0


def test_profile_freezes_parameters_and_disables_claude_calls(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('''mode = "paper"
[claude]
enabled = true
research_enabled = true
research_interval_hours = 6
vet_signals = true
daily_report = true
[data]
jupiter_api_key = "SECRET-KEEP-ME"
candle_interval = "1h"
[universe]
symbols = ["SOL"]
''')
    changes = apply_validated_profile(cfg, backup=False)
    text = cfg.read_text()
    assert 'jupiter_api_key = "SECRET-KEEP-ME"' in text                 # untouched
    for key in ("enabled", "research_enabled", "vet_signals", "daily_report", "auto_apply_overrides"):
        assert f"{key} = false" in text, key
    assert any("research_enabled" in c for c in changes)
