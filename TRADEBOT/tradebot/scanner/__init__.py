"""Read-only real-time opportunity scanner (PAPER ONLY).

A separate process from the 4h trading engine: its own SQLite file (data/scanner.db), its own poll loop, no wallet,
no signing module, no `taker` parameter on any Jupiter request. It measures data latency, builds 1-minute bars from
polled prices, evaluates deterministic short-horizon signals, prices every candidate with size-specific round-trip
quotes, rejects unsafe or unprofitable candidates, and tracks hypothetical outcomes so that false positives and
missed opportunities are counted. Claude is not involved anywhere in this loop.
"""
