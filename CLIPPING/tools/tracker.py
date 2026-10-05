"""Step 7: TRACKING. A single CSV (data/tracker.csv) that opens in Excel/Google Sheets.
One row per clip. Rows are upserted by clip_id so re-running the pipeline never duplicates them,
and manual edits to views/payout/notes are kept.
"""
from __future__ import annotations

import csv
import logging
import time
from pathlib import Path

from .util import DIRS

LOG = logging.getLogger("clipping.tracker")
TRACKER = DIRS["data"] / "tracker.csv"
COLUMNS = ["campaign", "source_id", "source_title", "clip_id", "start", "end", "duration", "hook", "score",
           "t1_appeal", "standalone_10", "compliance", "speaker", "standalone", "exported_file", "qc_pass", "platform",
           "submission_status", "views", "payout", "notes", "created_at", "updated_at"]
MANUAL_FIELDS = {"platform", "submission_status", "views", "payout", "notes"}


def load() -> list[dict]:
    if not TRACKER.exists():
        return []
    with open(TRACKER, "r", encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def save(rows: list[dict]) -> None:
    TRACKER.parent.mkdir(parents=True, exist_ok=True)
    with open(TRACKER, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLUMNS})


def upsert(record: dict) -> None:
    rows = load()
    now = time.strftime("%Y-%m-%d %H:%M")
    for r in rows:
        if r["clip_id"] == record["clip_id"] and r.get("campaign") == record.get("campaign"):
            for k, v in record.items():
                if k in MANUAL_FIELDS and r.get(k):
                    continue  # never clobber hand-entered data
                if (v is None or v == "") and r.get(k):
                    continue  # never blank out a value we already have (e.g. export path on a --no-render re-run)
                r[k] = v
            r["updated_at"] = now
            save(rows)
            return
    record.setdefault("submission_status", "candidate")
    record.setdefault("platform", "")
    record.setdefault("views", "")
    record.setdefault("payout", "")
    record.setdefault("notes", "")
    record["created_at"] = now
    record["updated_at"] = now
    rows.append(record)
    save(rows)


def update(clip_id: str, **fields) -> bool:
    rows = load()
    hit = False
    for r in rows:
        if r["clip_id"] == clip_id:
            r.update({k: v for k, v in fields.items() if v is not None})
            r["updated_at"] = time.strftime("%Y-%m-%d %H:%M")
            hit = True
    if hit:
        save(rows)
    return hit
