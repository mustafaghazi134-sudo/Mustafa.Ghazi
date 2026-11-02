"""Apply the research-validated settings to an EXISTING config.toml without regenerating it.

Only the listed keys are changed; every other line, comment and value (API keys, risk limits, capital,
paths) is preserved. A timestamped backup is written first. Python's stdlib can read TOML but not write
it, so edits are line-based and section-aware.
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

VALIDATED = {
    ("claude", "enabled"): "false",
    ("claude", "research_enabled"): "false",      # forward test = frozen parameters; no periodic re-optimisation
    ("claude", "vet_signals"): "false",
    ("claude", "daily_report"): "false",
    ("claude", "auto_apply_overrides"): "false",
    ("universe", "symbols"): '["BTC", "ETH", "SOL"]',
    ("universe", "auto_scan"): "false",
    ("data", "candle_interval"): '"4h"',
    ("data", "candle_history"): "1000",
}
STRATEGY_ENABLED = {"tsmom": True, "momentum": True, "mean_reversion": False, "breakout": False}
TSMOM_BLOCK = '''
[[strategies]]                 # PRIMARY (added by apply-profile): daily-evaluated TSMOM with 100-day EMA regime filter
name = "tsmom"
enabled = true
[strategies.params]
lookback_days = 30
ema_filter_days = 100
stop_pct = 25.0
trail_pct = 0.0
'''
MOMENTUM_BLOCK = '''
[[strategies]]                 # SECONDARY (added by apply-profile): EMA 12/26 cross above EMA 100, 2.5-ATR stop, no take-profit
name = "momentum"
enabled = true
[strategies.params]
fast = 12
slow = 26
trend = 100
atr_mult = 2.5
tp_mult = 0.0
trailing = false
'''

_SECTION = re.compile(r"^\s*\[\[?(?P<name>[^\]]+)\]\]?\s*(#.*)?$")   # [table] or [[array.of.tables]]
_KEY = re.compile(r"^(\s*)(?P<key>[A-Za-z0-9_]+)\s*=\s*(?P<val>.*?)(?P<comment>\s+#.*)?$")


def apply_validated_profile(path: Path, backup: bool = True) -> list[str]:
    """Returns a list of human-readable changes made."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; run `tradebot init` once to create it")
    if backup:
        bak = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, bak)
    lines = path.read_text().splitlines()
    changes: list[str] = []
    section = ""
    strategy_name = None
    seen_keys: set[tuple[str, str]] = set()
    seen_strategies: dict[str, bool] = {}
    out: list[str] = []
    for line in lines:
        m = _SECTION.match(line)
        if m:
            section = m.group("name").strip()
            if section == "strategies":          # a new [[strategies]] block
                strategy_name = None
            out.append(line)
            continue
        k = _KEY.match(line)
        if k:
            key, val, comment = k.group("key"), k.group("val").strip(), k.group("comment") or ""
            if section == "strategies" and key == "name":
                strategy_name = val.strip('"')
                seen_strategies[strategy_name] = True
            elif section == "strategies" and key == "enabled" and strategy_name in STRATEGY_ENABLED:
                want = "true" if STRATEGY_ENABLED[strategy_name] else "false"
                if val != want:
                    out.append(f"{k.group(1)}enabled = {want}{comment}")
                    changes.append(f"strategy {strategy_name}: enabled {val} -> {want}")
                    continue
            elif (section, key) in VALIDATED:
                seen_keys.add((section, key))
                want = VALIDATED[(section, key)]
                if val != want:
                    out.append(f"{k.group(1)}{key} = {want}{comment}")
                    changes.append(f"[{section}] {key}: {val} -> {want}")
                    continue
        out.append(line)
    text = "\n".join(out) + "\n"
    # keys that did not exist at all: insert right after their section header
    for (sec, key), want in VALIDATED.items():
        if (sec, key) not in seen_keys:
            pattern = re.compile(rf"^\[{re.escape(sec)}\][^\n]*\n", re.M)
            if pattern.search(text):
                text = pattern.sub(lambda m: m.group(0) + f"{key} = {want}\n", text, count=1)
            else:
                text += f"\n[{sec}]\n{key} = {want}\n"
            changes.append(f"[{sec}] {key}: added = {want}")
    if "tsmom" not in seen_strategies:
        text += TSMOM_BLOCK
        changes.append("strategy tsmom: added (validated params)")
    if "momentum" not in seen_strategies:
        text += MOMENTUM_BLOCK
        changes.append("strategy momentum: added (validated params)")
    path.write_text(text)
    return changes
