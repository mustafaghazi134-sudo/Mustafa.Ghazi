import json
import time

import pytest
from gate_helpers import arm_for_test, full_arm, paper_history

from tradebot.config import Settings
from tradebot.execution.live_gate import ENV_LIVE, ENV_LIVE_VALUE, LiveGate, LiveGateError
from tradebot.models import ClosedTrade
from tradebot.portfolio.ledger import Ledger


def _settings(tmp_path, mode="live") -> Settings:
    return Settings(mode=mode, data_dir=str(tmp_path))


def test_default_settings_are_paper():
    assert Settings().mode == "paper" and not Settings().is_live


def test_gate_requires_all_three_conditions(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    gate = LiveGate(s, Ledger(s.db_path, "paper"))
    with pytest.raises(LiveGateError):
        gate.assert_armed()
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    with pytest.raises(LiveGateError):            # still not armed: no armed file
        gate.assert_armed()
    arm_for_test(s, gate=gate)
    gate.assert_armed()                            # all three hold
    monkeypatch.delenv(ENV_LIVE)
    with pytest.raises(LiveGateError):            # env gone -> blocked again
        gate.assert_armed()


def test_arm_refuses_thin_paper_record(tmp_path):
    s = _settings(tmp_path)
    gate = LiveGate(s, Ledger(s.db_path, "paper"))
    with pytest.raises(LiveGateError):
        gate.begin_arm()                          # 0 paper trades, no snapshots
    assert not s.armed_file.exists()


def test_changing_risk_limits_disarms(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    gate = arm_for_test(s, Ledger(s.db_path, "paper"))
    assert gate.is_armed()
    s.risk.max_position_pct = 50.0
    assert not gate.is_armed()


def test_paper_mode_never_armed_even_with_file_and_env(tmp_path, monkeypatch):
    s = _settings(tmp_path, mode="paper")
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    gate = arm_for_test(s)
    assert not gate.is_armed()


def test_force_paper_env_overrides_live_config(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADEBOT_FORCE_PAPER", "1")
    assert Settings(mode="live").mode == "paper"


def test_replay_runs_do_not_count_toward_paper_record(tmp_path):
    s = _settings(tmp_path)
    replay = Ledger(s.db_path, "replay")
    for i in range(40):
        replay.record_trade(ClosedTrade("SOL", "m", 1, 1, 2, 0, 1, 0, 1, "x"))
    replay.log_event("start", "replay")
    gate = LiveGate(s, Ledger(s.db_path, "paper"))
    assert gate.paper_record() == (0, 0.0)


def test_existing_numeric_readiness_thresholds_are_unchanged():
    e = Settings().engine
    assert (e.min_paper_trades_before_live, e.min_paper_days_before_live, e.min_paper_coverage_before_live) == (5, 60.0, 0.8)


def test_arm_accepts_sixty_covered_days_with_a_few_trades(tmp_path):
    s = _settings(tmp_path)
    gate = LiveGate(s, paper_history(s, days=61, snapshot_share=0.95, trades=5))
    assert gate.paper_coverage() > 0.9
    rec = full_arm(gate)
    assert rec["readiness"]["closed_trades"] == 5 and rec["readiness"]["source"] == "local_ledger"
    assert "forced" not in rec and gate.status()["record_valid"]


@pytest.mark.parametrize("days,share,trades", [(30, 0.95, 10), (61, 0.5, 10), (61, 0.95, 2)])
def test_arm_refuses_short_gappy_or_tradeless_records(tmp_path, days, share, trades):
    s = _settings(tmp_path)
    gate = LiveGate(s, paper_history(s, days=days, snapshot_share=share, trades=trades))
    with pytest.raises(LiveGateError):
        gate.begin_arm()
    assert not s.armed_file.exists()
    assert json.loads(s.audit_log_file.read_text().splitlines()[-1])["kind"] == "arm_refused"
