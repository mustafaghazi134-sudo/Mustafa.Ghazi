import json

import pytest

from tradebot.config import Settings
from tradebot.execution.live_gate import ENV_LIVE, ENV_LIVE_VALUE, LiveGate, LiveGateError
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
    gate.arm(s.engine.live_confirm_phrase, force=True)
    gate.assert_armed()                            # all three hold
    monkeypatch.delenv(ENV_LIVE)
    with pytest.raises(LiveGateError):            # env gone -> blocked again
        gate.assert_armed()


def test_arm_refuses_wrong_phrase_and_thin_paper_record(tmp_path):
    s = _settings(tmp_path)
    gate = LiveGate(s, Ledger(s.db_path, "paper"))
    with pytest.raises(LiveGateError):
        gate.arm("yes please")
    with pytest.raises(LiveGateError):
        gate.arm(s.engine.live_confirm_phrase)    # 0 paper trades
    assert not s.armed_file.exists()


def test_changing_risk_limits_disarms(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    gate = LiveGate(s, Ledger(s.db_path, "paper"))
    gate.arm(s.engine.live_confirm_phrase, force=True)
    assert gate.is_armed()
    s.risk.max_position_pct = 50.0
    assert not gate.is_armed()


def test_paper_mode_never_armed_even_with_file_and_env(tmp_path, monkeypatch):
    s = _settings(tmp_path, mode="paper")
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    gate = LiveGate(s)
    gate.arm(s.engine.live_confirm_phrase, force=True)
    assert not gate.is_armed()


def test_force_paper_env_overrides_live_config(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADEBOT_FORCE_PAPER", "1")
    assert Settings(mode="live").mode == "paper"


def test_replay_runs_do_not_count_toward_paper_record(tmp_path):
    s = _settings(tmp_path)
    replay = Ledger(s.db_path, "replay")
    from tradebot.models import ClosedTrade
    for i in range(40):
        replay.record_trade(ClosedTrade("SOL", "m", 1, 1, 2, 0, 1, 0, 1, "x"))
    replay.log_event("start", "replay")
    gate = LiveGate(s, Ledger(s.db_path, "paper"))
    assert gate.paper_record() == (0, 0.0)
