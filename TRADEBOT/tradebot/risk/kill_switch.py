"""Emergency kill switch. Tripped by file, environment, API, or automatically by the risk engine.

Reset is manual only. While tripped, no new positions may be opened; by default open positions
are flattened to the quote asset.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

ENV_FLAG = "TRADEBOT_KILL"


class KillSwitch:
    def __init__(self, path: Path, flatten_on_trip: bool = True):
        self.path = Path(path)
        self.flatten_on_trip = flatten_on_trip
        self._memory_tripped: str | None = None
        self._listeners: list = []

    def on_trip(self, fn) -> None:
        self._listeners.append(fn)

    def is_tripped(self) -> bool:
        return bool(self._memory_tripped) or self.path.exists() or os.environ.get(ENV_FLAG) == "1"

    def reason(self) -> str | None:
        if self._memory_tripped:
            return self._memory_tripped
        if self.path.exists():
            try:
                return json.loads(self.path.read_text()).get("reason", "file")
            except (ValueError, OSError):
                return "file"
        if os.environ.get(ENV_FLAG) == "1":
            return f"env {ENV_FLAG}=1"
        return None

    def trip(self, reason: str, source: str = "auto") -> None:
        if self.is_tripped():
            log.warning("kill switch already tripped (%s); additional reason: %s", self.reason(), reason)
            return
        self._memory_tripped = reason
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"reason": reason, "source": source, "ts": time.time(), "flatten": self.flatten_on_trip}
        try:
            self.path.write_text(json.dumps(payload, indent=2))
        except OSError as e:
            log.error("could not write kill file %s: %s", self.path, e)
        log.critical("KILL SWITCH TRIPPED (%s): %s", source, reason)
        for fn in list(self._listeners):
            try:
                fn(reason)
            except Exception:  # noqa: BLE001 - listeners must never break the trip
                log.exception("kill switch listener failed")

    def reset(self) -> None:
        """Manual reset. Refuses while the environment flag is set."""
        if os.environ.get(ENV_FLAG) == "1":
            raise RuntimeError(f"unset {ENV_FLAG} before resetting the kill switch")
        self._memory_tripped = None
        if self.path.exists():
            self.path.unlink()
        log.warning("kill switch reset")
