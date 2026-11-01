import os

import pytest

from tradebot.risk.kill_switch import ENV_FLAG, KillSwitch


def test_trip_writes_file_and_notifies(tmp_path):
    ks = KillSwitch(tmp_path / "KILL")
    seen = []
    ks.on_trip(seen.append)
    assert not ks.is_tripped()
    ks.trip("manual", source="cli")
    assert ks.is_tripped() and (tmp_path / "KILL").exists() and seen == ["manual"]
    assert ks.reason() == "manual"
    ks.reset()
    assert not ks.is_tripped() and not (tmp_path / "KILL").exists()


def test_file_from_another_process_is_honoured(tmp_path):
    (tmp_path / "KILL").write_text("{}")
    assert KillSwitch(tmp_path / "KILL").is_tripped()


def test_env_flag(tmp_path, monkeypatch):
    ks = KillSwitch(tmp_path / "KILL")
    monkeypatch.setenv(ENV_FLAG, "1")
    assert ks.is_tripped()
    with pytest.raises(RuntimeError):
        ks.reset()
    monkeypatch.delenv(ENV_FLAG)
    assert not ks.is_tripped()
