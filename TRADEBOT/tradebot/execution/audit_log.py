"""Append-only, hash-chained audit trail for arm / disarm / refusal events, with external anchoring.

What the local file gives you: each line commits to the previous line's hash, so an accidental edit, truncation in the
middle, or a deleted line is detected by `verify()`.

What it does NOT give you: protection against someone who can rewrite the whole file. They can recompute every hash.
Only a copy of the chain head kept somewhere that person cannot write (an "anchor") detects that. `anchor()` returns the
(seq, hash) to preserve; `verify(anchor=...)` then proves the log still contains exactly that history. Where to keep
anchors, and the fully independent sink, are owner decisions (docs/LIVE_GATE_HARDENING.md section 4). `AuditSink` is the
interface for such a sink; none is shipped.

Entries must never contain secrets: they carry digests, counts and outcomes only.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Protocol

from .attestation import canonical, sha256_hex

GENESIS = "0" * 64


class AuditSink(Protocol):
    """An independently protected destination (separate account, append-only store, remote service)."""

    def emit(self, record: dict) -> None: ...


class AuditError(RuntimeError):
    pass


def _entry_hash(rec: dict) -> str:
    return sha256_hex(canonical({k: v for k, v in rec.items() if k != "hash"}))


class HashChainedAuditLog:
    def __init__(self, path: str | Path, sinks: list[AuditSink] | None = None):
        self.path = Path(path)
        self.sinks = sinks or []

    def _tail(self) -> tuple[int, str]:
        if not self.path.exists():
            return 0, GENESIS
        last = None
        with open(self.path, "rb") as f:
            for line in f:
                if line.strip():
                    last = line
        if last is None:
            return 0, GENESIS
        rec = json.loads(last)
        return int(rec["seq"]), str(rec["hash"])

    def append(self, kind: str, payload: dict[str, Any], *, ts: float | None = None) -> dict:
        try:
            seq, prev = self._tail()
        except (ValueError, KeyError, OSError) as e:
            raise AuditError(f"audit log unreadable ({type(e).__name__}); refusing to extend a damaged chain") from e
        rec = {"seq": seq + 1, "ts": time.time() if ts is None else ts, "kind": kind, "payload": payload, "prev": prev}
        rec["hash"] = _entry_hash(rec)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "ab") as f:
            f.write(canonical(rec) + b"\n")
            f.flush()
            os.fsync(f.fileno())
        for sink in self.sinks:
            sink.emit(rec)                      # a failing sink raises: the caller treats that as "do not proceed"
        return rec

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def anchor(self) -> dict:
        seq, head = self._tail()
        return {"seq": seq, "hash": head}

    def verify(self, anchor: dict | None = None) -> dict:
        """{"ok", "entries", "head", "anchored", "error"}. `anchored` is True only when an external anchor was supplied and
        matched; without one a whole-file rewrite is undetectable (and the result says so)."""
        res: dict[str, Any] = {"ok": False, "entries": 0, "head": GENESIS, "anchored": False, "error": None}
        try:
            recs = self.records()
        except (ValueError, OSError) as e:
            res["error"] = f"unreadable ({type(e).__name__})"
            return res
        prev = GENESIS
        by_seq: dict[int, str] = {}
        for i, r in enumerate(recs, start=1):
            if not isinstance(r, dict) or r.get("seq") != i or r.get("prev") != prev or r.get("hash") != _entry_hash(r):
                res["error"] = f"chain broken at entry {i}"
                return res
            prev = r["hash"]
            by_seq[i] = prev
        res.update(entries=len(recs), head=prev)
        if anchor is not None:
            try:
                s, h = int(anchor["seq"]), str(anchor["hash"])
            except (KeyError, ValueError, TypeError):
                res["error"] = "anchor malformed"
                return res
            if s == 0 and h == GENESIS:
                res["anchored"] = True
            elif by_seq.get(s) != h:
                res["error"] = f"anchor mismatch: entry {s} is not the preserved one (log rewritten, truncated or replaced)"
                return res
            else:
                res["anchored"] = True
        res["ok"] = True
        return res
