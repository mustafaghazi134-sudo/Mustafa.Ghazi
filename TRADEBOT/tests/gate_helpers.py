"""Test-only arming. Production code has no way to arm without readiness and a typed challenge; tests that need an
armed gate (adapter, crash-protection, signer wiring) build a valid record directly. This file must never be imported
from tradebot/."""
from __future__ import annotations

import time

from tradebot.execution import attestation as A
from tradebot.execution.live_gate import LiveGate
from tradebot.models import ClosedTrade, EquityPoint
from tradebot.portfolio.ledger import Ledger


def arm_for_test(settings, ledger=None, *, ttl_hours: float = 12.0, now: float | None = None, readiness: dict | None = None,
                 gate: LiveGate | None = None) -> LiveGate:
    gate = gate or LiveGate(settings, ledger)
    rec = A.build_record(digest=gate.config_digest(), readiness=readiness or {"source": "test_fixture"}, ttl_hours=ttl_hours,
                         now=time.time() if now is None else now)
    gate._write_record(rec)
    return gate


def paper_history(s, days: float, snapshot_share: float, trades: int, *, now: float | None = None):
    """A paper ledger whose equity snapshots start `days` ago and run, gap-free, for `snapshot_share` of that time."""
    led = Ledger(s.db_path, "paper")
    now = time.time() if now is None else now
    start = now - days * 86400
    interval = s.engine.equity_snapshot_interval_sec
    n = int(days * 86400 * snapshot_share / interval)
    for i in range(n):
        led.add_equity(EquityPoint(start + i * interval, 1000, 1000, 0, 0, 0))
    for k in range(trades):
        led.record_trade(ClosedTrade("SOL", "tsmom", 1, 1, 2, start + k, now - k, 0, 1, "signal"))
    return led


def full_arm(gate: LiveGate, ttl_hours=None, verdict=None):
    ch = gate.begin_arm(ttl_hours, verdict)
    return gate.complete_arm(ch, ch.code)
