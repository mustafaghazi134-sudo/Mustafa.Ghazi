from pathlib import Path

from tradebot.config import EXAMPLE_CONFIG, load_settings
from tradebot.profile import apply_validated_profile

OLD = '''mode = "paper"
initial_capital_usd = 2500.0
data_dir = "data"

[risk]
max_position_pct = 7.5

[[strategies]]
name = "momentum"
enabled = true
[strategies.params]
fast = 12
slow = 26
trend = 100
atr_mult = 2.5

[[strategies]]
name = "mean_reversion"
enabled = true

[[strategies]]
name = "breakout"
enabled = true

[claude]
enabled = true
model = "claude-opus-5-5"

[data]
provider = "live"
candle_interval = "1h"
candle_history = 400
jupiter_api_key = "jup_SECRET_DO_NOT_TOUCH"   # keep

[universe]
symbols = ["SOL", "JUP", "RAY", "BONK", "WIF", "PYTH", "JTO"]
auto_scan = true
max_symbols = 12
'''


def test_apply_profile_preserves_user_settings_and_key(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(OLD)
    changes = apply_validated_profile(cfg)
    text = cfg.read_text()
    assert 'jupiter_api_key = "jup_SECRET_DO_NOT_TOUCH"   # keep' in text          # untouched, comment kept
    assert "initial_capital_usd = 2500.0" in text and "max_position_pct = 7.5" in text
    s = load_settings(cfg)
    assert s.claude.enabled is False and s.universe.symbols == ["BTC", "ETH", "SOL"] and s.universe.auto_scan is False
    assert s.data.candle_interval == "4h" and s.data.candle_history == 1000 and s.data.jupiter_api_key == "jup_SECRET_DO_NOT_TOUCH"
    enabled = {c.name: c.enabled for c in s.strategies}
    assert enabled == {"momentum": True, "mean_reversion": False, "breakout": False, "tsmom": True}
    assert s.mode == "paper"
    assert any("tsmom: added" in c for c in changes) and any("[claude] enabled" in c for c in changes)
    assert list(tmp_path.glob("config.toml.bak-*")), "backup must be written"


def test_apply_profile_is_idempotent_on_generated_config(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(EXAMPLE_CONFIG)
    first = apply_validated_profile(cfg, backup=False)
    second = apply_validated_profile(cfg, backup=False)
    assert first == [] and second == []
    s = load_settings(cfg)
    assert [c.name for c in s.strategies if c.enabled] == ["tsmom", "momentum"]
