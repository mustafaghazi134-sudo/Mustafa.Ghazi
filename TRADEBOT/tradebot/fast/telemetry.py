"""Structured JSON event log, per-stage latency metrics, alerts and the fast engine's emergency halt.

- JsonEventLog: one JSON object per line in data/fast_events.jsonl ({"ts", "iso", "level", "event", ...fields}).
- LatencyMetrics: per-stage samples (ms) with p50 / p95 / max / count; snapshot persisted in the fast ledger.
- AlertManager: deduplicated alerts (same key at most once per `dedupe_sec`), written to the event log, the
  fast ledger's events table (kind "alert") and the Python log at WARNING/CRITICAL, plus an optional callback.
- EmergencyHalt: file data/FAST_HALT. While it exists the fast engine opens nothing and flattens what it holds.
  It stops ONLY the fast engine (the 4h engine is stopped by data/KILL, which the fast engine also obeys).
  Reset is manual (`tradebot fast-resume`).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable

from ..risk.kill_switch import KillSwitch

log = logging.getLogger("tradebot.fast")


class JsonEventLog:
    def __init__(self, path: str | Path | None, clock: Callable[[], float] = time.time):
        self.path = Path(path) if path else None
        self.clock = clock
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, level: str = "info", **fields) -> dict:
        ts = self.clock()
        rec = {"ts": round(ts, 3), "iso": dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z",
               "level": level, "event": event, **fields}
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str, separators=(",", ":")) + "\n")
        return rec


class LatencyMetrics:
    def __init__(self, keep: int = 2000):
        self.keep = keep
        self.samples: dict[str, list[float]] = {}

    def record(self, stage: str, ms: float) -> None:
        xs = self.samples.setdefault(stage, [])
        xs.append(float(ms))
        if len(xs) > self.keep:
            del xs[: len(xs) - self.keep]

    def summary(self) -> dict[str, dict]:
        out = {}
        for stage, xs in self.samples.items():
            s = sorted(xs)
            out[stage] = {"n": len(s), "p50_ms": round(s[len(s) // 2], 2), "p95_ms": round(s[max(0, int(len(s) * 0.95) - 1)], 2),
                          "max_ms": round(s[-1], 2)}
        return out


class AlertManager:
    def __init__(self, events: JsonEventLog, ledger=None, clock: Callable[[], float] = time.time, dedupe_sec: float = 300.0,
                 notify: Callable[[dict], None] | None = None):
        self.events = events
        self.ledger = ledger
        self.clock = clock
        self.dedupe_sec = dedupe_sec
        self.notify = notify
        self._last: dict[str, float] = {}
        self.active: dict[str, dict] = {}
        self.raised = 0

    def alert(self, key: str, message: str, severity: str = "warning", **ctx) -> bool:
        """Returns True if the alert was emitted, False if suppressed as a duplicate."""
        now = self.clock()
        last = self._last.get(key)
        self.active[key] = {"ts": now, "severity": severity, "message": message, **ctx}
        if last is not None and now - last < self.dedupe_sec:
            return False
        self._last[key] = now
        self.raised += 1
        rec = self.events.emit("alert", level=severity, key=key, message=message, **ctx)
        if self.ledger is not None:
            self.ledger.log_event("alert", f"[{severity}] {key}: {message}", ts=now)
        (log.critical if severity == "critical" else log.warning)("ALERT %s: %s", key, message)
        if self.notify:
            try:
                self.notify(rec)
            except Exception:  # noqa: BLE001 - a broken notifier must never break trading safety
                log.exception("alert notifier failed")
        return True

    def resolve(self, key: str) -> None:
        if self.active.pop(key, None) is not None:
            self.events.emit("alert_resolved", key=key)


class FastHalt(KillSwitch):
    """The fast engine's emergency halt: file data/FAST_HALT (or env TRADEBOT_FAST_HALT=1).

    It is a KillSwitch, so the fast engine's RiskEngine trips THIS file on its automatic triggers (daily loss,
    drawdown, repeated execution errors) instead of the shared data/KILL, which would also stop the 4h engine. It
    still reads as tripped whenever the shared kill switch is tripped, so `tradebot kill` stops both engines.
    `tradebot fast-resume` resets only this file."""

    ENV = "TRADEBOT_FAST_HALT"

    def __init__(self, path: str | Path, shared: KillSwitch | None = None, flatten_on_trip: bool = True):
        super().__init__(Path(path), flatten_on_trip=flatten_on_trip)
        self.shared = shared

    def own_tripped(self) -> bool:
        return KillSwitch.is_tripped(self) or os.environ.get(self.ENV) == "1"

    def is_tripped(self) -> bool:
        return self.own_tripped() or (self.shared is not None and self.shared.is_tripped())

    def is_active(self) -> bool:
        return self.is_tripped()

    def reason(self) -> str | None:
        if KillSwitch.is_tripped(self):
            return KillSwitch.reason(self)
        if os.environ.get(self.ENV) == "1":
            return f"env {self.ENV}=1"
        if self.shared is not None and self.shared.is_tripped():
            return f"shared kill switch: {self.shared.reason()}"
        return None

    def trip(self, reason: str, source: str = "auto") -> None:
        if self.own_tripped():
            return
        self._memory_tripped = reason
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.write_text(json.dumps({"reason": reason, "source": source, "ts": time.time()}, indent=2))
        except OSError as e:
            log.error("could not write %s: %s", self.path, e)
        log.critical("FAST ENGINE EMERGENCY HALT (%s): %s", source, reason)
        for fn in list(self._listeners):
            try:
                fn(reason)
            except Exception:  # noqa: BLE001
                log.exception("halt listener failed")

    def reset(self) -> None:
        if os.environ.get(self.ENV) == "1":
            raise RuntimeError(f"unset {self.ENV} before resetting the fast halt")
        self._memory_tripped = None
        if self.path.exists():
            self.path.unlink()


EmergencyHalt = FastHalt
