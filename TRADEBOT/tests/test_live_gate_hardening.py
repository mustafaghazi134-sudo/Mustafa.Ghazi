"""Regression tests for the live-gate hardening (docs/LIVE_GATE_HARDENING.md). One block per security requirement."""
import hashlib
import hmac
import json
import math
import re
from pathlib import Path

import pytest
from conftest import FakeClock
from gate_helpers import arm_for_test, full_arm, paper_history

from tradebot import cli
from tradebot.config import Settings
from tradebot.execution import attestation as A
from tradebot.execution.audit_log import GENESIS, AuditError, HashChainedAuditLog
from tradebot.execution.evidence_import import export_bundle, verify_bundle, content_hash
from tradebot.execution.live_gate import ENV_LIVE, ENV_LIVE_VALUE, LiveGate, LiveGateError
from tradebot.execution.readiness import PaperEvidence, operating_report, strategy_report
from tradebot.execution.submission import MAINNET_SUBMISSION_ENABLED, submission_enabled
from tradebot.portfolio.ledger import Ledger

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0
DAY = 86400.0


def live(tmp_path) -> Settings:
    return Settings(mode="live", data_dir=str(tmp_path))


def ev(ts, trades=(), halts=()):
    return PaperEvidence("local_ledger", tuple((t, 1000.0) for t in ts), tuple(trades), tuple(halts))


def op(ts, halts=(), now=NOW):
    return operating_report(ev(ts, halts=halts), now=now, interval_sec=300.0, tolerance_intervals=3.0)


def run_ts(start, end, step=300.0):
    return [start + i * step for i in range(int((end - start) / step) + 1)]


# ======================================================================================================================
# R1  no force / override
# ======================================================================================================================

def test_arm_has_no_force_or_override_parameter():
    import inspect
    for fn in (LiveGate.begin_arm, LiveGate.complete_arm):
        names = set(inspect.signature(fn).parameters)
        assert not names & {"force", "override", "skip_checks", "confirm"}


def test_cli_rejects_force_and_confirm_flags():
    for flag in (["--force"], ["--confirm", "x"]):
        with pytest.raises(SystemExit) as e:
            cli.main(["arm-live", *flag])
        assert e.value.code == 2


def test_only_the_live_gate_writes_the_armed_file():
    pat = re.compile(r"armed_file\.(write_text|write_bytes|open|touch)|os\.replace\(.*armed")
    offenders = [p.name for p in (ROOT / "tradebot").rglob("*.py") if p.name != "live_gate.py" and pat.search(p.read_text())]
    assert offenders == []


@pytest.mark.parametrize("days,share,trades", [(5, 0.99, 50), (61, 0.4, 50), (200, 0.99, 1)])
def test_thin_gappy_or_tradeless_record_cannot_be_armed_any_way(tmp_path, days, share, trades):
    s = live(tmp_path)
    gate = LiveGate(s, paper_history(s, days, share, trades))
    with pytest.raises(LiveGateError):
        gate.begin_arm()
    with pytest.raises(LiveGateError):
        gate.complete_arm(A.new_challenge(gate.config_digest(), 12, gate.clock()), "anything")
    assert not s.armed_file.exists()


# ======================================================================================================================
# R2  interactive-only, one-time challenge
# ======================================================================================================================

def _args(tmp_path, **kw):
    import argparse
    return argparse.Namespace(config=str(tmp_path / "none.toml"), ttl_hours=kw.get("ttl_hours"), evidence=None, evidence_sha256=None)


def _good_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADEBOT_DATA_DIR", str(tmp_path))
    s = Settings(data_dir=str(tmp_path))
    paper_history(s, 61, 0.97, 6)
    return s


def test_cli_refuses_without_a_terminal(tmp_path, monkeypatch, capsys):
    _good_env(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("must not prompt without a TTY"))
    assert cli.cmd_arm_live(_args(tmp_path)) == 2
    assert "interactive terminal" in capsys.readouterr().out
    assert not (tmp_path / "LIVE_ARMED").exists()


def test_cli_arms_only_when_the_exact_challenge_is_typed(tmp_path, monkeypatch, capsys):
    _good_env(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "I UNDERSTAND THIS TRADES REAL MONEY WITHOUT APPROVAL")  # the old static phrase
    assert cli.cmd_arm_live(_args(tmp_path)) == 2
    assert not (tmp_path / "LIVE_ARMED").exists()

    def type_back(prompt=""):
        return re.search(r"ARM-[0-9A-F]{8}-[0-9A-F]{8}-\d+H", prompt).group(0)
    monkeypatch.setattr("builtins.input", type_back)
    assert cli.cmd_arm_live(_args(tmp_path)) == 0
    rec = json.loads((tmp_path / "LIVE_ARMED").read_text())
    assert rec["schema"] == A.SCHEMA and rec["ttl_hours"] == 12.0 and "forced" not in rec
    out = capsys.readouterr().out
    assert "REPORT ONLY" in out and "authenticated by an independent attestor: False" in out


def test_challenge_is_unique_single_use_and_short_lived(tmp_path):
    s = live(tmp_path)
    clock = FakeClock(NOW)
    gate = LiveGate(s, paper_history(s, 61, 0.97, 6, now=NOW), clock=clock)
    c1, c2 = gate.begin_arm(), gate.begin_arm()
    assert c1.code != c2.code
    with pytest.raises(LiveGateError, match="did not match"):
        gate.complete_arm(c1, "ARM-WRONG")
    with pytest.raises(LiveGateError, match="already used"):
        gate.complete_arm(c1, c1.code)                      # a failed attempt burns the challenge
    clock.advance(A.CHALLENGE_TTL_SEC + 1)
    with pytest.raises(LiveGateError, match="expired"):
        gate.complete_arm(c2, c2.code)
    assert not s.armed_file.exists()
    c3 = gate.begin_arm()
    gate.complete_arm(c3, c3.code)
    assert s.armed_file.exists()
    with pytest.raises(LiveGateError, match="already used"):
        gate.complete_arm(c3, c3.code)


def test_config_change_between_challenge_and_confirmation_refuses(tmp_path):
    s = live(tmp_path)
    gate = LiveGate(s, paper_history(s, 61, 0.97, 6))
    ch = gate.begin_arm()
    s.risk.max_order_notional_usd = 499.0
    with pytest.raises(LiveGateError, match="configuration changed"):
        gate.complete_arm(ch, ch.code)
    assert not s.armed_file.exists()


# ======================================================================================================================
# R3  fail-closed validation of malformed / forged records
# ======================================================================================================================

def _valid(s, now=None):
    gate = LiveGate(s)
    return A.build_record(digest=gate.config_digest(), readiness={"source": "test_fixture"}, ttl_hours=12, now=NOW if now is None else now), gate


def _rehash(rec):
    rec = {k: v for k, v in rec.items() if k != "record_hash"}
    rec["record_hash"] = A.sha256_hex(A.canonical(rec))
    return rec


def test_a_valid_record_validates(tmp_path):
    rec, gate = _valid(live(tmp_path))
    assert A.validate_record(rec, now=NOW + 60, expected_digest=gate.config_digest()) == []


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(forced=True),
    lambda r: r.update(force=True),
    lambda r: r.update(schema=1),
    lambda r: r.update(kind="other"),
    lambda r: r.pop("nonce"),
    lambda r: r.update(nonce="short"),
    lambda r: r.pop("expires_at"),
    lambda r: r.update(expires_at="never"),
    lambda r: r.update(expires_at=float("nan")),
    lambda r: r.update(expires_at=float("inf")),
    lambda r: r.update(expires_at=True),
    lambda r: r.update(issued_at=None),
    lambda r: r.update(expires_at=r["issued_at"] - 1),
    lambda r: r.update(issued_at=NOW + 10 * DAY, expires_at=NOW + 10.5 * DAY),          # issued in the future
    lambda r: r.update(expires_at=r["issued_at"] + 48 * 3600, ttl_hours=48),            # over the 24 h cap
    lambda r: r.update(ttl_hours=1),                                                    # ttl disagrees with timestamps
    lambda r: r.update(config_digest="0" * 64),
    lambda r: r.update(readiness="all good"),
    lambda r: r.pop("attestation"),
    lambda r: r.update(record_hash="0" * 64),
    lambda r: r.pop("record_hash"),
])
def test_malformed_or_tampered_records_are_rejected(tmp_path, mutate):
    rec, gate = _valid(live(tmp_path))
    mutate(rec)
    candidates = [rec]
    if "record_hash" in rec and rec["record_hash"] != "0" * 64:
        try:
            candidates.append(_rehash(rec))              # an attacker who also fixes the hash (not possible for NaN/inf)
        except ValueError:
            pass
    for candidate in candidates:
        assert A.validate_record(candidate, now=NOW + 60, expected_digest=gate.config_digest()), mutate


@pytest.mark.parametrize("junk", [None, [], "armed", 7, {}, {"limits_hash": "x", "ts": 1, "confirm": "y", "forced": False}, [1, 2]])
def test_non_record_values_never_validate_and_never_raise(tmp_path, junk):
    assert A.validate_record(junk, now=NOW, expected_digest="d")


def test_extending_expiry_by_hand_breaks_the_record_hash(tmp_path):
    rec, gate = _valid(live(tmp_path))
    rec["expires_at"] += 3600
    rec["ttl_hours"] += 1
    problems = A.validate_record(rec, now=NOW + 60, expected_digest=gate.config_digest())
    assert any("hash mismatch" in p for p in problems)


@pytest.mark.parametrize("content", ["", "not json", "{", '{"a": 1}', "[]", "\x00\x01\x02", '{"limits_hash": "abc", "ts": 1}'])
def test_unreadable_or_legacy_armed_files_are_not_armed(tmp_path, monkeypatch, content):
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    s = live(tmp_path)
    s.armed_file.write_text(content)
    gate = LiveGate(s)
    assert not gate.is_armed() and not gate.status()["record_valid"]
    with pytest.raises(LiveGateError):
        gate.assert_armed()


def test_truncated_record_is_not_armed(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    s = live(tmp_path)
    gate = arm_for_test(s)
    text = s.armed_file.read_text()
    s.armed_file.write_text(text[: len(text) // 2])
    assert not gate.is_armed()


class HmacAttestor:
    """Stand-in for an independent attestor (the real one must keep its key where the trading user cannot read it)."""
    scheme = "test-hmac"

    def __init__(self, key=b"k" * 32):
        self.key = key

    def sign(self, payload):
        return hmac.new(self.key, payload, hashlib.sha256).hexdigest()

    def verify(self, payload, signature):
        return hmac.compare_digest(self.sign(payload), signature)


def test_attestor_signature_is_enforced_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    s = live(tmp_path)
    gate = LiveGate(s, attestor=HmacAttestor())
    arm_for_test(s, gate=gate)                                   # unsigned record from a process without the key
    assert not gate.is_armed()
    rec = A.build_record(digest=gate.config_digest(), readiness={"source": "test_fixture"}, ttl_hours=12, now=gate.clock(),
                         attestor=HmacAttestor())
    gate._write_record(rec)
    assert gate.is_armed() and gate.status()["authenticated"]
    rec["expires_at"] += 1000
    gate._write_record(_rehash(rec))                             # edited and re-hashed, but cannot re-sign
    assert not gate.is_armed()
    other = LiveGate(s, attestor=HmacAttestor(b"x" * 32))
    gate._write_record(A.build_record(digest=gate.config_digest(), readiness={"source": "test_fixture"}, ttl_hours=12,
                                      now=gate.clock(), attestor=HmacAttestor()))
    assert not other.is_armed()                                  # wrong key


def test_require_external_attestation_blocks_everything_until_an_attestor_exists(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    s = live(tmp_path)
    s.engine.require_external_attestation = True
    gate = arm_for_test(s)
    assert not gate.is_armed()
    assert any("attestation is required" in p for p in gate.status()["problems"])


# ======================================================================================================================
# R4  expiry
# ======================================================================================================================

def test_default_and_maximum_lifetime():
    assert Settings().engine.live_arm_ttl_hours == 12.0 and A.MAX_TTL_HOURS == 24.0
    with pytest.raises(Exception):
        Settings(engine={"live_arm_ttl_hours": 25})            # config cannot raise the cap
    with pytest.raises(Exception):
        Settings(engine={"live_arm_ttl_hours": 0})


@pytest.mark.parametrize("ttl", [24.01, 25, 1000, 0, -1, float("nan"), float("inf"), True, "12"])
def test_begin_arm_rejects_bad_ttl(tmp_path, ttl):
    s = live(tmp_path)
    gate = LiveGate(s, paper_history(s, 61, 0.97, 6))
    with pytest.raises(LiveGateError):
        gate.begin_arm(ttl)


def test_activation_expires_exactly_at_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    s = live(tmp_path)
    clock = FakeClock(NOW)
    gate = LiveGate(s, clock=clock)
    arm_for_test(s, gate=gate, now=NOW, ttl_hours=12)
    assert s.armed_file.exists()
    clock.t = NOW + 12 * 3600 - 1
    gate.assert_armed()
    assert 0 < gate.status()["seconds_remaining"] <= 1
    clock.t = NOW + 12 * 3600
    with pytest.raises(LiveGateError, match="expired"):
        gate.assert_armed()
    assert not gate.is_armed() and gate.status()["seconds_remaining"] is None


def test_24_hour_activation_is_the_longest_that_validates(tmp_path):
    s = live(tmp_path)
    rec = A.build_record(digest=LiveGate(s).config_digest(), readiness={}, ttl_hours=24, now=NOW)
    assert A.validate_record(rec, now=NOW, expected_digest=LiveGate(s).config_digest()) == []
    with pytest.raises(ValueError):
        A.build_record(digest="d", readiness={}, ttl_hours=24.5, now=NOW)


def test_clock_set_back_cannot_extend_or_resurrect(tmp_path, monkeypatch):
    s = live(tmp_path)
    gate = LiveGate(s, clock=FakeClock(NOW))
    arm_for_test(s, gate=gate, now=NOW + 3 * DAY)             # "issued" three days after this clock: forged or clock error
    assert not gate.status()["record_valid"]


# ======================================================================================================================
# R5  binding to risk / live / execution / strategy / universe / readiness settings
# ======================================================================================================================

@pytest.mark.parametrize("edit", [
    lambda s: setattr(s.risk, "max_position_pct", 11.0),
    lambda s: setattr(s.risk, "max_daily_loss_pct", 2.0),
    lambda s: setattr(s.live, "max_order_usd", 11.0),
    lambda s: setattr(s.live, "allowed_symbols", ["SOL"]),
    lambda s: setattr(s.execution, "execute_max_retries", 5),
    lambda s: setattr(s.execution, "live_slippage_bps", 99),
    lambda s: s.strategies[0].params.update(lookback_days=31),
    lambda s: setattr(s.strategies[2], "enabled", True),
    lambda s: s.strategies.pop(),
    lambda s: setattr(s.universe, "symbols", ["SOL"]),
    lambda s: setattr(s, "initial_capital_usd", 5000.0),
    lambda s: setattr(s.engine, "min_paper_trades_before_live", 1),
    lambda s: setattr(s.engine, "min_paper_days_before_live", 1.0),
    lambda s: setattr(s.engine, "min_paper_coverage_before_live", 0.1),
    lambda s: setattr(s.engine, "paper_gap_tolerance_intervals", 50.0),
    lambda s: setattr(s.engine, "equity_snapshot_interval_sec", 600.0),
    lambda s: setattr(s.engine, "require_external_attestation", True),
    lambda s: setattr(s.engine, "accept_imported_paper_evidence", True),
    lambda s: setattr(s.engine, "evidence_allowed_repositories", ["a/b"]),
])
def test_editing_any_bound_setting_disarms(tmp_path, edit):
    s = live(tmp_path)
    gate = arm_for_test(s)
    assert gate.status()["record_valid"]
    edit(s)
    assert not gate.status()["record_valid"]


@pytest.mark.parametrize("edit", [
    lambda s: setattr(s, "log_level", "DEBUG"),
    lambda s: setattr(s, "data_dir", s.data_dir),
    lambda s: setattr(s.claude, "enabled", True),
    lambda s: setattr(s.engine, "live_arm_ttl_hours", 6.0),
    lambda s: setattr(s.engine, "scan_interval_sec", 120.0),
])
def test_unbound_settings_do_not_disarm(tmp_path, edit):
    s = live(tmp_path)
    gate = arm_for_test(s)
    edit(s)
    assert gate.status()["record_valid"]


# ======================================================================================================================
# R6  order-time re-validation
# ======================================================================================================================

def test_assert_armed_rechecks_the_local_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    s = live(tmp_path)
    led = paper_history(s, 61, 0.97, 6)
    gate = LiveGate(s, led)
    full_arm(gate)
    gate.assert_armed()
    led._exec("DELETE FROM equity WHERE ts < ?", (gate.clock() - 10 * DAY,))      # history truncated after arming
    led.conn.commit()
    with pytest.raises(LiveGateError, match="readiness no longer met"):
        gate.assert_armed()


def test_assert_armed_notices_deleted_trades(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    s = live(tmp_path)
    led = paper_history(s, 61, 0.97, 6)
    gate = LiveGate(s, led)
    full_arm(gate)
    led._exec("DELETE FROM trades WHERE mode='paper'")
    led.conn.commit()
    with pytest.raises(LiveGateError):
        gate.assert_armed()


# ======================================================================================================================
# R7/R8  gap-based coverage and operational state
# ======================================================================================================================

def test_dense_snapshots_in_one_hour_do_not_inflate_coverage():
    ts = run_ts(NOW - 10 * DAY, NOW - 10 * DAY + 3600, step=1.0)       # 3,600 snapshots in the first hour
    ts.append(NOW)
    r = op(ts)
    assert r.snapshots_used > 3000 and r.coverage < 0.01 and r.outage_count == 1


def test_a_forty_day_hole_costs_forty_days():
    ts = run_ts(NOW - 100 * DAY, NOW - 70 * DAY) + run_ts(NOW - 30 * DAY, NOW)
    r = op(ts)
    assert r.outage_count == 1 and abs(r.longest_gap_sec - 40 * DAY) < 600
    assert 0.59 < r.coverage < 0.61 and 99.9 < r.span_days < 100.1


def test_one_long_outage_is_visible_even_when_average_coverage_is_high():
    ts = run_ts(NOW - 90 * DAY, NOW - 45 * DAY) + run_ts(NOW - 43 * DAY, NOW)       # a 2-day hole in 90 days
    r = op(ts)
    assert r.coverage > 0.97 and r.longest_gap_sec > 1.9 * DAY and r.outage_count == 1


def test_gap_tolerance_boundary():
    tol = 900.0                                                       # 3 x the 300 s snapshot interval
    assert op([NOW - 2 * tol, NOW - tol, NOW]).outage_count == 0      # steps of exactly the tolerance are covered
    r = op([NOW - 2 * tol - 1, NOW - tol, NOW])                       # one second over is an outage and earns no credit
    assert r.outage_count == 1 and r.longest_gap_sec == tol + 1
    assert op([NOW - 2 * tol, NOW - tol, NOW]).coverage == 1.0 and r.coverage < 0.5


def test_kill_switch_time_is_not_operating_time():
    ts = run_ts(NOW - 10 * DAY, NOW)
    base = op(ts)
    halted = op(ts, halts=[(NOW - 8 * DAY, "kill_switch"), (NOW - 4 * DAY, "kill_reset")])
    assert base.coverage > 0.99 and 0.59 < halted.coverage < 0.61 and abs(halted.halted_days - 4) < 0.01
    forever = op(ts, halts=[(NOW - 2 * DAY, "kill_switch")])                                # tripped and never reset
    assert 0.79 < forever.coverage < 0.81


def test_stale_tail_counts_against_coverage():
    r = op(run_ts(NOW - 10 * DAY, NOW - 2 * DAY))
    assert r.stale_tail_sec > 1.99 * DAY and 0.79 < r.coverage < 0.81 and r.outage_count == 1


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), NOW + 5 * DAY, True])
def test_invalid_snapshot_timestamps_are_rejected(bad):
    r = op(run_ts(NOW - 5 * DAY, NOW) + [bad])
    assert r.snapshots_rejected == 1 and r.coverage > 0.99


def test_empty_and_unsorted_and_duplicate_input():
    assert op([]).coverage == 0.0 and op([]).span_days == 0.0
    ts = run_ts(NOW - 5 * DAY, NOW)
    shuffled = list(reversed(ts)) + ts
    assert abs(op(shuffled).coverage - op(ts).coverage) < 1e-9


def test_backdated_start_event_cannot_start_the_clock(tmp_path):
    s = live(tmp_path)
    led = Ledger(s.db_path, "paper")
    led.log_event("start", "paper", ts=NOW - 200 * DAY)            # old behaviour: this alone made 200 days of "history"
    gate = LiveGate(s, led, clock=FakeClock(NOW))
    assert gate.paper_record() == (0, 0.0)
    for i in range(3):
        led.add_equity(__import__("tradebot.models", fromlist=["EquityPoint"]).EquityPoint(NOW - 1000 + i * 300, 1, 1, 0, 0, 0))
    assert gate.paper_record()[1] < 0.02


def test_snapshot_count_no_longer_decides_coverage(tmp_path):
    s = live(tmp_path)
    clock = FakeClock(NOW)
    from tradebot.models import EquityPoint
    led = Ledger(s.db_path, "paper")
    # 61 "days" but only the first and last few hours carry snapshots, at 1 s spacing: the OLD formula
    # count x interval / elapsed would exceed 1.0 and report full coverage.
    for t in run_ts(NOW - 61 * DAY, NOW - 61 * DAY + 4 * 3600, 1.0) + run_ts(NOW - 4 * 3600, NOW, 1.0):
        led.add_equity(EquityPoint(t, 1, 1, 0, 0, 0))
    assert led.count_equity("paper") * 300 / (61 * DAY) > 0.8
    assert LiveGate(s, led, clock=clock).paper_coverage() < 0.01


def test_existing_style_history_still_passes_unchanged_thresholds(tmp_path):
    s = live(tmp_path)
    gate = LiveGate(s, paper_history(s, 61, 0.95, 5))
    assert gate.evaluate_readiness().ok


# ======================================================================================================================
# R9  strategy evidence is reported, never enforced, and never confused with operations
# ======================================================================================================================

def _trades(pnls):
    return [{"entry_ts": 1.0, "exit_ts": 2.0, "pnl_usd": p, "fees_usd": 0.01, "symbol": "SOL", "strategy": "t"} for p in pnls]


def test_strategy_report_is_report_only_and_deterministic():
    e = ev([NOW - 10, NOW], trades=_trades([1, -1, 2, -0.5, 0.2]))
    a, b = strategy_report(e), strategy_report(e)
    assert a == b and a["enforced"] is False and "REPORT ONLY" in a["note"] and "not evaluated" in a["out_of_sample"]
    assert a["closed_trades"] == 5 and a["ci95_mean_pnl_usd"][0] < 0 < a["ci95_mean_pnl_usd"][1] and a["ci_excludes_zero_above"] is False


def test_five_wins_still_come_with_the_sample_size_warning_not_a_pass():
    r = strategy_report(ev([NOW - 10, NOW], trades=_trades([1, 1, 1, 1, 1])))
    assert r["closed_trades"] == 5 and r["enforced"] is False and r["top_trade_share_of_gains"] == 0.2


def test_strategy_report_handles_empty_and_garbage():
    assert strategy_report(ev([NOW]))["mean_pnl_usd"] is None
    bad = [{"pnl_usd": float("nan")}, {"pnl_usd": "x"}, {"pnl_usd": None}]
    assert strategy_report(ev([NOW], trades=bad))["closed_trades"] == 0


def test_losing_paper_strategy_is_not_blocked_by_an_unapproved_criterion(tmp_path):
    """Documented gap, not a feature: until the owner approves numeric criteria nothing enforces profitability."""
    s = live(tmp_path)
    led = paper_history(s, 61, 0.97, 6)
    led._exec("UPDATE trades SET pnl_usd=-5 WHERE mode='paper'")
    led.conn.commit()
    gate = LiveGate(s, led)
    res = gate.evaluate_readiness()
    assert res.ok and res.strategy["net_pnl_usd"] < 0 and res.strategy["enforced"] is False


def test_max_drawdown_is_reported():
    e = PaperEvidence("x", ((1.0, 100.0), (2.0, 120.0), (3.0, 90.0), (4.0, 110.0)), (), ())
    assert strategy_report(e)["max_drawdown_pct"] == 25.0


# ======================================================================================================================
# R10  audit log
# ======================================================================================================================

def _log(tmp_path, n=4):
    log = HashChainedAuditLog(tmp_path / "audit.jsonl")
    for i in range(n):
        log.append("e", {"i": i})
    return log


def test_chain_verifies_and_detects_edits_and_deletions(tmp_path):
    log = _log(tmp_path)
    assert log.verify()["ok"] and log.verify()["entries"] == 4
    lines = log.path.read_text().splitlines()
    log.path.write_text("\n".join(lines[:1] + [lines[1].replace('"i":1', '"i":9')] + lines[2:]) + "\n")
    assert "broken at entry 2" in log.verify()["error"]
    log.path.write_text("\n".join(lines[:1] + lines[2:]) + "\n")
    assert not log.verify()["ok"]


def test_whole_file_rewrite_is_only_detected_with_an_external_anchor(tmp_path):
    log = _log(tmp_path)
    anchor = log.anchor()
    # attacker rebuilds a perfectly consistent chain with different content
    forged = HashChainedAuditLog(tmp_path / "forged.jsonl")
    for i in range(4):
        forged.append("e", {"i": i + 100})
    log.path.write_text(forged.path.read_text())
    plain = log.verify()
    assert plain["ok"] and plain["anchored"] is False                     # honest about the limit
    anchored = log.verify(anchor)
    assert not anchored["ok"] and "anchor mismatch" in anchored["error"]


def test_truncation_is_detected_with_an_anchor(tmp_path):
    log = _log(tmp_path)
    anchor = log.anchor()
    lines = log.path.read_text().splitlines()
    log.path.write_text("\n".join(lines[:2]) + "\n")
    assert log.verify()["ok"] and not log.verify(anchor)["ok"]
    assert log.verify({"seq": 2, "hash": json.loads(lines[1])["hash"]})["anchored"]


def test_extension_keeps_a_previous_anchor_valid(tmp_path):
    log = _log(tmp_path, 3)
    anchor = log.anchor()
    log.append("more", {})
    assert log.verify(anchor)["ok"] and log.verify(anchor)["anchored"]
    assert log.verify({"seq": 0, "hash": GENESIS})["ok"]
    assert not log.verify({"seq": "x"})["ok"]


def test_sinks_receive_records_and_a_failing_sink_raises(tmp_path):
    got = []

    class Sink:
        def emit(self, rec):
            got.append(rec["seq"])

    class Broken:
        def emit(self, rec):
            raise OSError("remote store down")

    log = HashChainedAuditLog(tmp_path / "a.jsonl", [Sink()])
    log.append("x", {})
    assert got == [1]
    with pytest.raises(OSError):
        HashChainedAuditLog(tmp_path / "b.jsonl", [Broken()]).append("x", {})


def test_damaged_chain_refuses_to_be_extended(tmp_path):
    log = _log(tmp_path)
    log.path.write_text(log.path.read_text() + "{garbage\n")
    with pytest.raises(AuditError):
        log.append("x", {})


def test_arming_and_disarming_are_audited_without_secrets(tmp_path):
    s = live(tmp_path)
    gate = LiveGate(s, paper_history(s, 61, 0.97, 6))
    full_arm(gate)
    gate.disarm("test")
    recs = gate.audit.records()
    assert [r["kind"] for r in recs] == ["arm_challenge_issued", "armed", "disarmed"] and gate.audit.verify()["ok"]
    blob = json.dumps(recs)
    assert "ARM-" not in blob and "jup_" not in blob and "private" not in blob.lower()
    assert not s.armed_file.exists()


def test_arming_is_refused_when_the_audit_log_cannot_be_written(tmp_path):
    s = live(tmp_path)
    s.audit_log_file.mkdir()                                       # a directory where the file should be
    gate = LiveGate(s, paper_history(s, 61, 0.97, 6))
    with pytest.raises(LiveGateError, match="audit log"):
        gate.begin_arm()
    assert not s.armed_file.exists()


def test_disarm_succeeds_even_if_the_audit_log_is_broken(tmp_path):
    s = live(tmp_path)
    arm_for_test(s)
    s.audit_log_file.mkdir()
    LiveGate(s).disarm()
    assert not s.armed_file.exists()


# ======================================================================================================================
# C  imported (GitHub) evidence: read-only, distrusted by default
# ======================================================================================================================

REPO = "owner/paper-runner"
PROV = {"source": "github_actions", "repository": REPO, "workflow": "paper.yml", "run_id": "123", "commit_sha": "a" * 40}


class YesVerifier:
    def verify(self, content_sha256, provenance):
        return provenance.get("run_id") == "123"


class NoVerifier:
    def verify(self, content_sha256, provenance):
        return False


def _bundle(tmp_path, **settings_edit):
    s = live(tmp_path)
    s.engine.evidence_allowed_repositories = [REPO]
    led = paper_history(s, 61, 0.97, 6)
    b = export_bundle(led, s, PROV)
    return s, led, b


def _verify(b, s, sha=None, verifier=YesVerifier()):
    return verify_bundle(b, s, expected_sha256=b["content_sha256"] if sha is None else sha, verifier=verifier)


def test_a_complete_bundle_passes_every_check(tmp_path):
    s, _, b = _bundle(tmp_path)
    v = _verify(b, s)
    assert v.accepted and v.authenticated and not v.failures and v.evidence.source == "imported_bundle"


def test_without_an_independent_verifier_the_bundle_is_never_accepted(tmp_path):
    s, _, b = _bundle(tmp_path)
    v = verify_bundle(b, s, expected_sha256=b["content_sha256"], verifier=None)
    assert not v.accepted and not v.authenticated and any("provenance.authenticated" in f for f in v.failures)
    assert v.evidence is not None                                         # still inspectable, just not trusted


def test_verifier_rejection_wins(tmp_path):
    s, _, b = _bundle(tmp_path)
    assert not _verify(b, s, verifier=NoVerifier()).accepted


def test_pinned_hash_is_required_and_must_match(tmp_path):
    s, _, b = _bundle(tmp_path)
    for sha in (None, "", "0" * 64, "xyz"):
        v = verify_bundle(b, s, expected_sha256=sha, verifier=YesVerifier())
        assert not v.accepted and any("integrity.pinned_hash" in f for f in v.failures)


def test_tampered_content_fails_integrity_even_if_rehashed_by_the_attacker(tmp_path):
    s, _, b = _bundle(tmp_path)
    pinned = b["content_sha256"]
    b["trades"][0]["pnl_usd"] = 9999.0
    v = verify_bundle(b, s, expected_sha256=pinned, verifier=YesVerifier())
    assert any("integrity.self_hash" in f for f in v.failures) and not v.accepted
    b["content_sha256"] = content_hash(b)                                     # attacker fixes the inner hash
    v = verify_bundle(b, s, expected_sha256=pinned, verifier=YesVerifier())
    assert any("integrity.pinned_hash" in f for f in v.failures) and not v.accepted


def test_evidence_from_a_different_strategy_or_config_is_rejected(tmp_path):
    s, _, b = _bundle(tmp_path)
    s.strategies[0].params["lookback_days"] = 31
    v = _verify(b, s)
    assert not v.accepted and any("correspondence.config" in f for f in v.failures)
    s2, _, b2 = _bundle(tmp_path / "x") if (tmp_path / "x").mkdir() is None else (None, None, None)
    s2.risk.max_position_pct = 25.0
    assert not _verify(b2, s2).accepted


def test_repository_allow_list_is_enforced_and_empty_means_none(tmp_path):
    s, _, b = _bundle(tmp_path)
    s.engine.evidence_allowed_repositories = ["someone/else"]
    assert not _verify(b, s).accepted
    s.engine.evidence_allowed_repositories = []
    v = _verify(b, s)
    assert not v.accepted and any("allow-list is empty" in f for f in v.failures)


def test_missing_provenance_fields_are_rejected(tmp_path):
    s, led, _ = _bundle(tmp_path)
    b = export_bundle(led, s, {"source": "github_actions", "repository": REPO})
    assert not _verify(b, s).accepted


def test_future_dated_and_nonfinite_and_oversized_bundles_are_rejected(tmp_path):
    s, led, b = _bundle(tmp_path)
    future = dict(b, as_of=b["as_of"] + 10 * DAY)
    future["content_sha256"] = content_hash(future)
    assert not _verify(future, s).accepted
    nan = dict(b, equity=b["equity"] + [[float("nan"), 1.0]])
    nan["content_sha256"] = "0" * 64
    assert not verify_bundle(nan, s, expected_sha256="0" * 64, verifier=YesVerifier()).accepted
    late = dict(b, equity=b["equity"] + [[b["as_of"] + 5 * DAY, 1.0]])
    late["content_sha256"] = content_hash(late)
    assert not _verify(late, s).accepted
    reorder = dict(b, trades=[dict(b["trades"][0], entry_ts=b["trades"][0]["exit_ts"] + 5)])
    reorder["content_sha256"] = content_hash(reorder)
    assert not _verify(reorder, s).accepted


@pytest.mark.parametrize("junk", [None, [], "bundle", 3, {}, {"schema": "tradebot.paper_evidence.v1"}, {"schema": "x", "mode": "live"}])
def test_junk_bundles_never_raise_and_never_pass(tmp_path, junk):
    s = live(tmp_path)
    v = verify_bundle(junk, s, expected_sha256="0" * 64, verifier=YesVerifier())
    assert not v.accepted and v.evidence is None


def test_export_and_verify_do_not_modify_the_ledger(tmp_path):
    s, led, b = _bundle(tmp_path)
    before = (led.count_equity("paper"), led.count_trades("paper"), len(led.events(1000)))
    _verify(b, s)
    export_bundle(led, s, PROV)
    assert (led.count_equity("paper"), led.count_trades("paper"), len(led.events(1000))) == before


def test_imported_evidence_is_ignored_for_arming_unless_enabled_and_authenticated(tmp_path):
    s, led, b = _bundle(tmp_path)
    empty_local = tmp_path / "other"
    empty_local.mkdir()
    s2 = Settings(mode="live", data_dir=str(empty_local))
    s2.engine.evidence_allowed_repositories = [REPO]
    b2 = export_bundle(led, s2, PROV)          # same strategies/universe/risk -> same evidence digest
    good = verify_bundle(b2, s2, expected_sha256=b2["content_sha256"], verifier=YesVerifier())
    assert good.accepted
    gate = LiveGate(s2, Ledger(s2.db_path, "paper"))                      # this machine has no local paper history
    with pytest.raises(LiveGateError, match="disabled"):
        gate.begin_arm(verdict=good)                                      # switch off by default
    s2.engine.accept_imported_paper_evidence = True
    unauth = verify_bundle(b2, s2, expected_sha256=b2["content_sha256"], verifier=None)
    with pytest.raises(LiveGateError, match="not accepted"):
        gate.begin_arm(verdict=unauth)
    good = verify_bundle(b2, s2, expected_sha256=b2["content_sha256"], verifier=YesVerifier())
    rec = gate.complete_arm(*(lambda ch: (ch, ch.code))(gate.begin_arm(verdict=good)))
    assert rec["readiness"]["source"] == "imported_bundle" and rec["readiness"]["closed_trades"] == 6


def test_stale_imported_evidence_fails_the_operational_check(tmp_path):
    s, led, _ = _bundle(tmp_path)
    s.engine.accept_imported_paper_evidence = True
    b = export_bundle(led, s, PROV)
    gate = LiveGate(s, clock=FakeClock(b["as_of"] + 20 * DAY))
    v = verify_bundle(b, s, expected_sha256=b["content_sha256"], verifier=YesVerifier(), now=b["as_of"] + 20 * DAY)
    with pytest.raises(LiveGateError, match="insufficient"):
        gate.begin_arm(verdict=v)                                          # 20 days of nothing after as_of is an outage


# ======================================================================================================================
# R11  disabled live execution and existing controls are preserved
# ======================================================================================================================

def test_build_switch_is_still_disabled_and_independent_of_the_gate(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    s = live(tmp_path)
    gate = arm_for_test(s)
    gate.assert_armed()                                    # a perfectly valid activation...
    assert MAINNET_SUBMISSION_ENABLED is False and submission_enabled() is False   # ...does not enable submission


def test_gate_module_never_touches_submission_or_signer():
    src = (ROOT / "tradebot" / "execution" / "live_gate.py").read_text()
    for needle in ("MAINNET_SUBMISSION_ENABLED", "tradebot_signer", "SOLANA_PRIVATE_KEY", "Keypair"):
        assert needle not in src.split('"""', 2)[2]


def test_hardening_modules_import_no_wallet_or_signer_code():
    for name in ("attestation", "readiness", "audit_log", "evidence_import", "live_gate"):
        src = (ROOT / "tradebot" / "execution" / f"{name}.py").read_text()
        body = src.split('"""', 2)[2]
        assert "solders" not in body and "tradebot_signer" not in body and "private_key" not in body.lower()


def test_status_keys_used_by_existing_callers_are_still_present(tmp_path):
    st = LiveGate(live(tmp_path)).status()
    assert {"armed", "env_ok", "armed_file", "limits_match", "armed_by", "armed_ts", "mode"} <= set(st)
    assert st["armed"] is False


def test_cli_status_and_readiness_run_read_only(tmp_path, monkeypatch, capsys):
    s = _good = Settings(data_dir=str(tmp_path))
    monkeypatch.setenv("TRADEBOT_DATA_DIR", str(tmp_path))
    paper_history(s, 61, 0.97, 6)
    import argparse
    ns = argparse.Namespace(config=str(tmp_path / "none.toml"), mode=None)
    assert cli.cmd_status(ns) == 0
    assert cli.cmd_readiness(ns) == 0
    out = capsys.readouterr().out
    assert "gap-free snapshots" in out and "MET" in out and "REPORT ONLY" in out
    assert not (tmp_path / "LIVE_ARMED").exists()
