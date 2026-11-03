"""The live gate: three independent conditions must hold before any real-money order is sent.

1. config.mode == "live"
2. env TRADEBOT_LIVE_TRADING == "I_ACCEPT_THE_RISK"
3. an armed file written by `tradebot arm-live`, which embeds a hash of the risk limits
   (changing [risk] or [live] limits disarms) and required a minimum paper-trading record (or --force).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time

from ..config import Settings
from ..portfolio.ledger import Ledger

log = logging.getLogger(__name__)

ENV_LIVE = "TRADEBOT_LIVE_TRADING"
ENV_LIVE_VALUE = "I_ACCEPT_THE_RISK"


class LiveGateError(PermissionError):
    pass


class LiveGate:
    def __init__(self, settings: Settings, ledger: Ledger | None = None):
        self.s = settings
        self.ledger = ledger

    def limits_hash(self) -> str:
        # [risk] and [live] limits: editing either disarms.
        payload = json.dumps({"risk": self.s.risk.model_dump(), "live": self.s.live.model_dump()}, sort_keys=True).encode()
        return hashlib.sha256(payload).hexdigest()

    def status(self) -> dict:
        armed_file = self.s.armed_file
        info = {"mode": self.s.mode, "env_ok": os.environ.get(ENV_LIVE) == ENV_LIVE_VALUE,
                "armed_file": armed_file.exists(), "limits_match": False, "armed_by": None, "armed_ts": None}
        if armed_file.exists():
            try:
                data = json.loads(armed_file.read_text())
                info["limits_match"] = data.get("limits_hash") == self.limits_hash()
                info["armed_by"] = data.get("confirm")
                info["armed_ts"] = data.get("ts")
            except (ValueError, OSError):
                pass
        info["armed"] = bool(self.s.is_live and info["env_ok"] and info["armed_file"] and info["limits_match"])
        return info

    def is_armed(self) -> bool:
        return self.status()["armed"]

    def assert_armed(self) -> None:
        st = self.status()
        if not st["armed"]:
            missing = []
            if not self.s.is_live:
                missing.append('config mode != "live"')
            if not st["env_ok"]:
                missing.append(f"env {ENV_LIVE} != {ENV_LIVE_VALUE}")
            if not st["armed_file"]:
                missing.append("not armed (run: tradebot arm-live)")
            elif not st["limits_match"]:
                missing.append("risk limits changed since arming; re-run arm-live")
            raise LiveGateError("live trading blocked: " + "; ".join(missing))

    def paper_record(self) -> tuple[int, float]:
        """(closed paper trades, days of paper history)."""
        if self.ledger is None:
            return 0, 0.0
        trades = self.ledger.count_trades(mode="paper")
        first = self.ledger.first_event_ts(mode="paper")
        days = (time.time() - first) / 86400 if first else 0.0
        return trades, days

    def paper_coverage(self) -> float:
        """Share of the paper period during which the engine was running, from its equity snapshots (0..1)."""
        if self.ledger is None:
            return 0.0
        first = self.ledger.first_event_ts(mode="paper")
        if not first:
            return 0.0
        elapsed = time.time() - first
        if elapsed <= 0:
            return 0.0
        snaps = self.ledger.count_equity(mode="paper")
        return min(1.0, snaps * self.s.engine.equity_snapshot_interval_sec / elapsed)

    def arm(self, confirm: str, force: bool = False) -> None:
        phrase = self.s.engine.live_confirm_phrase
        if confirm.strip() != phrase:
            raise LiveGateError(f"confirmation phrase mismatch; expected exactly: {phrase}")
        trades, days = self.paper_record()
        cov = self.paper_coverage()
        e = self.s.engine
        need_t, need_d, need_c = e.min_paper_trades_before_live, e.min_paper_days_before_live, e.min_paper_coverage_before_live
        if not force and (trades < need_t or days < need_d or cov < need_c):
            raise LiveGateError(f"paper record insufficient: {trades} trades / {days:.1f} days / {cov:.0%} running; "
                                f"need {need_t} / {need_d} / {need_c:.0%}. Use --force to override deliberately.")
        self.s.armed_file.parent.mkdir(parents=True, exist_ok=True)
        self.s.armed_file.write_text(json.dumps({"ts": time.time(), "confirm": confirm, "limits_hash": self.limits_hash(),
                                                 "paper_trades": trades, "paper_days": round(days, 2),
                                                 "paper_coverage": round(cov, 3), "forced": force}, indent=2))
        log.warning("LIVE TRADING ARMED (paper record: %d trades, %.1f days, forced=%s)", trades, days, force)

    def disarm(self) -> None:
        if self.s.armed_file.exists():
            self.s.armed_file.unlink()
        log.warning("live trading disarmed")
