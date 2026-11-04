"""Second-round review tests for the live gate (docs/LIVE_GATE_REVIEW_2.md).

Part A proves invalidation WITHOUT re-arming: arm once through the real challenge flow, then change a setting and check
the very next validation refuses. Part B pins what is NOT protected today (tests named KNOWN_LIMITATION): they pass
because the limitation exists, and they must be deleted or inverted when the matching control is built.
"""
import argparse
import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest
from conftest import FakeClock
from gate_helpers import full_arm, paper_history

from tradebot import cli
from tradebot.config import Settings, load_settings
from tradebot.execution import attestation as A
from tradebot.execution.evidence_import import export_bundle
from tradebot.execution.live_gate import ENV_LIVE, ENV_LIVE_VALUE, LiveGate, LiveGateError
from tradebot.execution.readiness import PaperEvidence, evaluate, operating_report, strategy_report
from tradebot.portfolio.ledger import Ledger

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0
DAY = 86400.0
SP = 304.0                         # measured spacing: 296 cycles in 300 min on the GitHub paper chain


# ======================================================================================================================
# A. invalidation without re-arming
# ======================================================================================================================

@pytest.fixture(scope="module")
def armed_dir(tmp_path_factory):
    """One ledger with a 61-day record, armed through begin_arm/complete_arm with default settings."""
    d = tmp_path_factory.mktemp("armed")
    s = Settings(mode="live", data_dir=str(d))
    led = paper_history(s, 61, 0.97, 6)
    full_arm(LiveGate(s, led))
    led.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    led.conn.commit()
    led.conn.close()
    return d


def _copy(armed_dir, tmp_path) -> Path:
    dst = tmp_path / "copy"
    shutil.copytree(armed_dir, dst)
    return dst


def _gate_for(d: Path, settings: Settings | None = None) -> tuple[Settings, LiveGate]:
    s = settings or Settings(mode="live", data_dir=str(d))
    return s, LiveGate(s, Ledger(s.db_path, "paper"))


def test_fixture_is_validly_armed_before_any_edit(armed_dir, tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    s, gate = _gate_for(_copy(armed_dir, tmp_path))
    gate.assert_armed()
    assert gate.status()["record_valid"] and gate.status()["evidence_source"] == "local_ledger"


EDITS = {
    # risk
    "risk.max_position_pct": lambda s: setattr(s.risk, "max_position_pct", 9.0),
    "risk.max_total_exposure_pct": lambda s: setattr(s.risk, "max_total_exposure_pct", 60.0),
    "risk.max_order_notional_usd": lambda s: setattr(s.risk, "max_order_notional_usd", 501.0),
    "risk.max_drawdown_pct": lambda s: setattr(s.risk, "max_drawdown_pct", 20.0),
    "risk.max_slippage_bps": lambda s: setattr(s.risk, "max_slippage_bps", 200),
    "risk.allowlist": lambda s: setattr(s.risk, "allowlist", ["SOL"]),
    # live limits
    "live.max_order_usd": lambda s: setattr(s.live, "max_order_usd", 20.0),
    "live.max_wallet_exposure_usd": lambda s: setattr(s.live, "max_wallet_exposure_usd", 100.0),
    "live.max_daily_loss_usd": lambda s: setattr(s.live, "max_daily_loss_usd", 50.0),
    "live.allowed_symbols": lambda s: setattr(s.live, "allowed_symbols", ["SOL"]),
    "live.wallet_pubkey": lambda s: setattr(s.live, "wallet_pubkey", "SomeOtherWallet11111111111111111111111111111"),
    # execution
    "execution.live_slippage_bps": lambda s: setattr(s.execution, "live_slippage_bps", 80),
    "execution.execute_max_retries": lambda s: setattr(s.execution, "execute_max_retries", 7),
    "execution.paper_fee_bps": lambda s: setattr(s.execution, "paper_fee_bps", 1),
    "execution.rpc_url": lambda s: setattr(s.execution, "rpc_url", "https://rpc.example.invalid"),
    # strategies
    "strategies.param_changed": lambda s: s.strategies[0].params.update(lookback_days=31),
    "strategies.param_added": lambda s: s.strategies[0].params.update(extra=1),
    "strategies.enabled_flag": lambda s: setattr(s.strategies[2], "enabled", True),
    "strategies.disabled": lambda s: setattr(s.strategies[0], "enabled", False),
    "strategies.removed": lambda s: s.strategies.pop(),
    "strategies.reordered": lambda s: s.strategies.reverse(),
    "strategies.added": lambda s: s.strategies.append(type(s.strategies[0])(name="breakout")),
    # universe and capital
    "universe.symbols": lambda s: setattr(s.universe, "symbols", ["SOL"]),
    "universe.mints": lambda s: s.universe.mints.update(SOL="So11111111111111111111111111111111111111113"),   # NOT the real SOL mint
    "initial_capital": lambda s: setattr(s, "initial_capital_usd", 2000.0),
    # readiness settings (editing the bar after arming must not keep the activation alive)
    "engine.min_paper_trades": lambda s: setattr(s.engine, "min_paper_trades_before_live", 1),
    "engine.min_paper_days": lambda s: setattr(s.engine, "min_paper_days_before_live", 30.0),
    "engine.min_paper_coverage": lambda s: setattr(s.engine, "min_paper_coverage_before_live", 0.5),
    "engine.snapshot_interval": lambda s: setattr(s.engine, "equity_snapshot_interval_sec", 900.0),
    "engine.gap_tolerance": lambda s: setattr(s.engine, "paper_gap_tolerance_intervals", 12.0),
    "engine.max_single_outage": lambda s: setattr(s.engine, "max_single_outage_hours", 48.0),
    "engine.require_attestation": lambda s: setattr(s.engine, "require_external_attestation", True),
    "engine.accept_imported": lambda s: setattr(s.engine, "accept_imported_paper_evidence", True),
    "engine.allowed_repos": lambda s: setattr(s.engine, "evidence_allowed_repositories", ["a/b"]),
}


@pytest.mark.parametrize("name", sorted(EDITS))
def test_editing_a_setting_after_arming_blocks_the_next_order_check(armed_dir, tmp_path, monkeypatch, name):
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    d = _copy(armed_dir, tmp_path)
    s, gate = _gate_for(d)
    gate.assert_armed()                                    # armed
    EDITS[name](s)                                         # ONE edit, no re-arm
    assert not gate.is_armed()
    with pytest.raises(LiveGateError, match="configuration changed"):
        gate.assert_armed()
    # a fresh process re-reading the same files reaches the same verdict (nothing cached in memory)
    assert not LiveGate(s, Ledger(s.db_path, "paper")).is_armed()


@pytest.mark.parametrize("name", ["risk.max_position_pct", "live.max_order_usd", "strategies.param_changed", "engine.gap_tolerance"])
def test_reverting_the_edit_restores_validity_because_binding_is_exact(armed_dir, tmp_path, monkeypatch, name):
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    d = _copy(armed_dir, tmp_path)
    s, gate = _gate_for(d)
    EDITS[name](s)
    assert not gate.is_armed()
    s2, gate2 = _gate_for(d)                               # original configuration again
    assert gate2.is_armed()


def test_every_bound_section_is_covered_by_an_edit_above():
    covered = {k.split(".")[0] for k in EDITS}
    assert {"risk", "live", "execution", "strategies", "universe", "engine", "initial_capital"} <= covered
    bound = set(A.config_binding(Settings()))
    assert bound == {"v", "risk", "live", "execution", "strategies", "universe", "initial_capital_usd", "readiness"}
    assert set(A.config_binding(Settings())["readiness"]) == {
        "min_paper_trades_before_live", "min_paper_days_before_live", "min_paper_coverage_before_live",
        "equity_snapshot_interval_sec", "paper_gap_tolerance_intervals", "max_single_outage_hours",
        "require_external_attestation", "accept_imported_paper_evidence", "evidence_allowed_repositories"}


def _toml(d: Path, extra: str = "") -> Path:
    p = d / "cfg.toml"
    p.write_text(f'mode = "live"\ndata_dir = "{d.as_posix()}"\n{extra}')
    return p


TOML_EDITS = [
    ("[risk]\nmax_position_pct = 9.0\n"), ("[risk]\nmax_order_notional_usd = 400\n"), ("[live]\nmax_order_usd = 20.0\n"),
    ("[execution]\nlive_slippage_bps = 80\n"), ("[execution]\nexecute_max_retries = 6\n"),
    ("[engine]\nmin_paper_trades_before_live = 1\n"), ("[engine]\nmin_paper_days_before_live = 30\n"),
    ("[engine]\nmin_paper_coverage_before_live = 0.5\n"), ("[engine]\npaper_gap_tolerance_intervals = 9\n"),
    ("[engine]\nmax_single_outage_hours = 48\n"),
    ('[universe]\nsymbols = ["SOL"]\n'),
    '[[strategies]]\nname = "tsmom"\nparams = {lookback_days = 31, ema_filter_days = 100, stop_pct = 25.0}\n',
]


def test_unmodified_config_file_loads_to_the_armed_digest(armed_dir, tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    monkeypatch.delenv("TRADEBOT_DATA_DIR", raising=False)
    d = _copy(armed_dir, tmp_path)
    s = load_settings(_toml(d))
    assert s.mode == "live" and LiveGate(s, Ledger(s.db_path, "paper")).is_armed()


@pytest.mark.parametrize("extra", TOML_EDITS)
def test_editing_the_config_file_after_arming_disarms_on_next_load(armed_dir, tmp_path, monkeypatch, extra):
    """The realistic path: the owner (or a script) edits config.toml; the engine reloads it on its next start."""
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    monkeypatch.delenv("TRADEBOT_DATA_DIR", raising=False)
    d = _copy(armed_dir, tmp_path)
    s = load_settings(_toml(d, extra))
    assert s.mode == "live"
    gate = LiveGate(s, Ledger(s.db_path, "paper"))
    assert not gate.is_armed()
    assert any("configuration changed" in p for p in gate.status()["problems"])


def test_unrelated_config_changes_do_not_disarm(armed_dir, tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    monkeypatch.delenv("TRADEBOT_DATA_DIR", raising=False)
    d = _copy(armed_dir, tmp_path)
    s = load_settings(_toml(d, 'log_level = "DEBUG"\n[engine]\nscan_interval_sec = 30\n'))
    assert LiveGate(s, Ledger(s.db_path, "paper")).is_armed()


# ======================================================================================================================
# B. coverage review: long outages, repeated snapshots, kill-switch periods
# ======================================================================================================================

def _ev(ts, trades=6, halts=()):
    tr = tuple({"entry_ts": 1.0, "exit_ts": 2.0, "pnl_usd": 1.0, "fees_usd": 0.0, "symbol": "SOL", "strategy": "t"} for _ in range(trades))
    return PaperEvidence("local_ledger", tuple((t, 1000.0) for t in ts), tr, tuple(halts))


def _run(start, end, step=SP):
    out, t = [], start
    while t <= end:
        out.append(t)
        t += step
    return out


def test_a_single_eleven_day_outage_passes_today_and_the_optional_cap_catches_it():
    ts = _run(NOW - 61 * DAY, NOW - 61 * DAY + 38 * DAY) + _run(NOW - 12 * DAY, NOW)       # 38 d + 11 d hole + 12 d
    s = Settings()
    res = evaluate(s, _ev(ts), now=NOW)
    assert res.ok and res.operating.outage_count == 1 and res.operating.longest_gap_sec > 10.9 * DAY   # the weakness
    s.engine.max_single_outage_hours = 48.0
    capped = evaluate(s, _ev(ts), now=NOW)
    assert not capped.ok and any("longest single outage" in r for r in capped.reasons)


def test_outage_cap_boundary_and_default_is_unset():
    assert Settings().engine.max_single_outage_hours is None
    s = Settings()
    s.engine.max_single_outage_hours = 24.0
    base = _run(NOW - 61 * DAY, NOW - 30 * DAY)
    exactly = base + _run(base[-1] + 24 * 3600, NOW)
    over = base + _run(base[-1] + 24 * 3600 + 5, NOW)
    assert evaluate(s, _ev(exactly), now=NOW).ok
    assert not evaluate(s, _ev(over), now=NOW).ok
    with pytest.raises(Exception):
        Settings(engine={"max_single_outage_hours": 0})


def test_repeated_and_flooded_snapshots_add_nothing():
    base = _run(NOW - 10 * DAY, NOW - 9 * DAY)
    flood = base + [base[-1]] * 50_000 + [base[-1] + i * 0.001 for i in range(1, 5000)]
    a = operating_report(_ev(base), now=NOW, interval_sec=300, tolerance_intervals=3)
    b = operating_report(_ev(flood), now=NOW, interval_sec=300, tolerance_intervals=3)
    assert abs(a.coverage - b.coverage) < 1e-4 and a.outage_count == b.outage_count == 1 and b.coverage < 0.11


def test_one_missed_snapshot_is_tolerated_two_consecutive_misses_are_an_outage():
    """Spacing is ~304 s, tolerance 900 s: a gap of 2 x 304 = 608 s (one missed snapshot) is covered; 3 x 304 = 912 s is not."""
    one = operating_report(_ev([NOW - 4 * SP, NOW - 2 * SP, NOW]), now=NOW, interval_sec=300, tolerance_intervals=3)
    two = operating_report(_ev([NOW - 3 * SP, NOW]), now=NOW, interval_sec=300, tolerance_intervals=3)
    assert one.outage_count == 0 and one.coverage == 1.0
    assert two.outage_count == 1 and two.longest_gap_sec == 3 * SP


def test_kill_switch_time_is_removed_but_daily_halt_is_not():
    ts = _run(NOW - 10 * DAY, NOW)
    halts = [(NOW - 6 * DAY, "kill_switch"), (NOW - 3 * DAY, "kill_reset")]
    killed = operating_report(_ev(ts, halts=halts), now=NOW, interval_sec=300, tolerance_intervals=3)
    assert 0.69 < killed.coverage < 0.71 and abs(killed.halted_days - 3) < 0.01
    # daily_halt / loss-streak pauses are risk pauses of a RUNNING engine; they are not in HALT_KINDS and cost nothing
    from tradebot.execution.readiness import HALT_KINDS
    assert HALT_KINDS == ("kill_switch", "kill_reset")


def test_a_kill_switch_never_reset_blocks_arming_until_it_is_reset(tmp_path):
    s = Settings(mode="live", data_dir=str(tmp_path))
    led = paper_history(s, 61, 0.99, 6)
    led.log_event("kill_switch", "test trip", ts=NOW)       # placeholder; real timestamp set below
    led._exec("UPDATE events SET ts=? WHERE kind='kill_switch'", (__import__("time").time() - 20 * DAY,))
    led.conn.commit()
    gate = LiveGate(s, led)
    assert not gate.evaluate_readiness().ok                 # 20 of 61 days halted -> coverage ~0.67
    led.log_event("kill_reset", "resume", ts=__import__("time").time() - 19 * DAY)
    assert gate.evaluate_readiness().ok                     # one day halted


def test_snapshot_cadence_assumption_matches_the_engine_loop():
    """The tolerance is defined in snapshot intervals; this pins the engine facts it relies on."""
    src = (ROOT / "tradebot" / "engine" / "bot.py").read_text()
    assert "now - self.last_equity_snapshot_ts >= self.s.engine.equity_snapshot_interval_sec" in src
    e = Settings().engine
    assert e.equity_snapshot_interval_sec == 300.0 and e.scan_interval_sec == 60.0 and e.paper_gap_tolerance_intervals == 3.0


# ======================================================================================================================
# C. profitability: what the gate does and does not stop (item 1)
# ======================================================================================================================

def _losing_ledger(tmp_path):
    from tradebot.models import ClosedTrade, EquityPoint
    s = Settings(mode="live", data_dir=str(tmp_path))
    led = Ledger(s.db_path, "paper")
    now = __import__("time").time()
    start = now - 61 * DAY
    n = int(61 * DAY / 300)
    for i in range(n):
        eq = 1000.0 - 200.0 * i / n                          # steadily falls 20%: far beyond the 10% drawdown limit
        led.add_equity(EquityPoint(start + i * 300, eq, eq, 0, 0, 0))
    for k in range(8):
        led.record_trade(ClosedTrade("SOL", "tsmom", 1, 100, 90, start + k, now - k, 1.0, -26.0, "stop"))
    led.log_event("kill_switch", "drawdown 10%", ts=start + 20 * DAY)
    led.log_event("kill_reset", "resume", ts=start + 21 * DAY)
    return s, led


def test_negative_paper_returns_can_still_pass_every_operational_condition_and_arm(tmp_path, monkeypatch):
    """KNOWN_LIMITATION (by design until a strategy-validation gate is approved): see docs/LIVE_GATE_REVIEW_2.md section 1."""
    s, led = _losing_ledger(tmp_path)
    gate = LiveGate(s, led)
    res = gate.evaluate_readiness()
    assert res.ok, res.reasons                                   # operational conditions all met
    rec = full_arm(gate)                                         # ... and the engine arms
    assert rec["readiness"]["strategy_report"]["net_pnl_usd"] < 0
    sr = res.strategy
    assert sr["ci95_mean_pnl_usd"][1] < 0 and sr["max_drawdown_pct"] > 19 and sr["enforced"] is False
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    gate.assert_armed()


def test_strategy_report_is_computed_but_ignored_by_evaluate_ok(tmp_path):
    s, led = _losing_ledger(tmp_path)
    ev_ok = LiveGate(s, led).evaluate_readiness()
    assert ev_ok.strategy["net_pnl_usd"] < 0 and ev_ok.ok
    # the evaluate() decision depends only on trades / days / coverage / optional outage cap
    src = (ROOT / "tradebot" / "execution" / "readiness.py").read_text()
    body = src.split("def evaluate", 1)[1]
    assert "strategy_report(ev)" in body and "reasons.append" in body
    assert "pnl" not in body.split("return ReadinessResult")[0]


# ======================================================================================================================
# D. activation authentication: what is and is not protected (item 2)
# ======================================================================================================================

def test_KNOWN_LIMITATION_a_process_with_write_access_can_forge_an_unsigned_activation(tmp_path, monkeypatch):
    """Anything that can write data/LIVE_ARMED (the bot's own Windows account can) produces a record this gate accepts
    when no Attestor is configured: no challenge, no readiness, no audit entry. record_hash is not authentication."""
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    s = Settings(mode="live", data_dir=str(tmp_path))
    gate = LiveGate(s)                                           # empty ledger: readiness is nowhere near met
    forged = A.build_record(digest=A.config_digest(s), readiness={"source": "forged"}, ttl_hours=24, now=gate.clock())
    s.armed_file.write_text(json.dumps(forged))
    assert gate.is_armed() and gate.status()["authenticated"] is False
    assert not s.audit_log_file.exists()                         # the forgery leaves no trace in the audit chain


def test_the_same_forgery_fails_when_an_independent_attestor_is_configured(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    s = Settings(mode="live", data_dir=str(tmp_path))

    class VerifyOnly:                                            # holds only a verification half; cannot sign
        scheme = "ed25519-test"

        def sign(self, payload):
            raise PermissionError("signing key is not available to this process")

        def verify(self, payload, signature):
            return False                                         # nothing this process can write verifies

    gate = LiveGate(s, attestor=VerifyOnly())
    forged = A.build_record(digest=A.config_digest(s), readiness={"source": "forged"}, ttl_hours=24, now=gate.clock())
    forged["attestation"] = {"scheme": "ed25519-test"}
    forged["signature"] = "00" * 32
    forged["record_hash"] = A.sha256_hex(A.canonical({k: v for k, v in forged.items() if k != "record_hash"}))
    s.armed_file.write_text(json.dumps(forged))
    assert not gate.is_armed() and any("signature" in p for p in gate.status()["problems"])


def test_no_attestor_is_wired_into_production_code_paths():
    """Honest status: the signed-record path exists as an interface but nothing in production constructs one."""
    for p in (ROOT / "tradebot").rglob("*.py"):
        text = p.read_text()
        if p.name == "live_gate.py":
            continue
        assert "attestor=" not in text.replace("attestor: Attestor", ""), p
    assert "LiveGate(settings, ledger)" in (ROOT / "tradebot" / "engine" / "bot.py").read_text()


def test_the_engine_can_only_be_authorised_by_the_gate_it_owns_but_never_submits_by_itself(tmp_path, monkeypatch):
    from tradebot.execution.submission import submission_enabled
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    s = Settings(mode="live", data_dir=str(tmp_path))
    gate = LiveGate(s)
    s.armed_file.write_text(json.dumps(A.build_record(digest=A.config_digest(s), readiness={}, ttl_hours=1, now=gate.clock())))
    assert gate.is_armed() and submission_enabled() is False


# ======================================================================================================================
# E. evidence trust (item 6)
# ======================================================================================================================

def test_no_provenance_verifier_exists_in_production_code():
    """Only the Protocol (an interface with no body) may mention a ProvenanceVerifier; no class implements one."""
    import ast
    for p in (ROOT / "tradebot").rglob("*.py"):
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.ClassDef):
                is_protocol = any(getattr(b, "id", getattr(b, "attr", "")) == "Protocol" for b in node.bases)
                for fn in node.body:
                    if isinstance(fn, ast.FunctionDef) and fn.name == "verify" and [a.arg for a in fn.args.args] == ["self", "content_sha256", "provenance"]:
                        assert is_protocol, f"{p}: class {node.name} implements a ProvenanceVerifier"
    assert "verifier=" not in (ROOT / "tradebot" / "cli.py").read_text()          # the CLI never passes one


def _cfg_with_import_enabled(d: Path, repo: str) -> Path:
    return _toml(d, f'[engine]\naccept_imported_paper_evidence = true\nevidence_allowed_repositories = ["{repo}"]\n')


def test_cli_cannot_arm_from_imported_evidence_even_with_everything_enabled(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TRADEBOT_DATA_DIR", raising=False)
    d = tmp_path / "m"
    d.mkdir()
    (tmp_path / "src").mkdir()
    src_settings = Settings(mode="live", data_dir=str(tmp_path / "src"))
    src_settings.engine.evidence_allowed_repositories = ["owner/runner"]
    led = paper_history(src_settings, 61, 0.97, 6)
    bundle = export_bundle(led, src_settings, {"source": "github_actions", "repository": "owner/runner", "workflow": "p.yml",
                                               "run_id": "1", "commit_sha": "a" * 40})
    f = d / "bundle.json"
    f.write_text(json.dumps(bundle))
    cfg = _cfg_with_import_enabled(d, "owner/runner")
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("must be refused before any challenge"))
    ns = argparse.Namespace(config=str(cfg), ttl_hours=None, evidence=str(f), evidence_sha256=bundle["content_sha256"])
    assert cli.cmd_arm_live(ns) == 2
    out = capsys.readouterr().out
    assert "not accepted" in out and "provenance.authenticated" in out
    assert not (d / "LIVE_ARMED").exists()


# ======================================================================================================================
# F. what a locally editable ledger and audit log can and cannot prove (item 6)
# ======================================================================================================================

def test_KNOWN_LIMITATION_a_hand_built_ledger_passes_the_local_readiness_check(tmp_path):
    """A writer of data/ledger.sqlite can fabricate a record that meets every operational threshold. Nothing local can
    tell. Only independent provenance (a trusted verifier) or a ledger the bot account cannot write would."""
    s = Settings(mode="live", data_dir=str(tmp_path))
    Ledger(s.db_path, "paper").conn.close()
    con = sqlite3.connect(s.db_path)
    now = __import__("time").time()
    rows = [(now - 61 * DAY + i * 300, "paper", 1000.0, 1000.0, 0.0, 0.0, 0.0) for i in range(int(61 * DAY / 300))]
    con.executemany("INSERT INTO equity VALUES(?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    led = Ledger(s.db_path, "paper")
    from tradebot.models import ClosedTrade
    for k in range(5):
        led.record_trade(ClosedTrade("SOL", "fake", 1, 1, 2, now - 100 + k, now - 50 + k, 0, 1, "x"))
    res = LiveGate(s, led).evaluate_readiness()
    assert res.ok and res.coverage > 0.99                         # fabricated, and indistinguishable locally


def test_KNOWN_LIMITATION_audit_log_rewritten_wholesale_verifies_without_an_anchor(tmp_path):
    from tradebot.execution.audit_log import HashChainedAuditLog
    real = HashChainedAuditLog(tmp_path / "real.jsonl")
    real.append("armed", {"x": 1})
    fake = HashChainedAuditLog(tmp_path / "fake.jsonl")
    fake.append("disarmed", {"x": 2})
    real.path.write_text(fake.path.read_text())
    assert real.verify()["ok"] and real.verify()["anchored"] is False
