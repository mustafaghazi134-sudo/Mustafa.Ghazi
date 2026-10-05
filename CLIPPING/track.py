#!/usr/bin/env python3
"""Update the tracker from the command line (or just edit data/tracker.csv in Excel).

    python track.py list                                   # show all clips
    python track.py list --status submitted
    python track.py submit CLIP_ID --platform tiktok --notes "campaign X, posted 5 Oct"
    python track.py update CLIP_ID --views 12000 --payout 6.50 --status paid
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools import tracker  # noqa: E402
from tools.util import DIRS  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--status")
    ls.add_argument("--campaign")
    sb = sub.add_parser("submit", help="mark as submitted and copy the export into submitted/<campaign>/")
    sb.add_argument("clip_id")
    sb.add_argument("--platform", required=True)
    sb.add_argument("--notes")
    up = sub.add_parser("update")
    up.add_argument("clip_id")
    up.add_argument("--status", dest="submission_status")
    up.add_argument("--platform")
    up.add_argument("--views")
    up.add_argument("--payout")
    up.add_argument("--notes")
    a = p.parse_args(argv)

    if a.cmd == "list":
        rows = tracker.load()
        rows = [r for r in rows if (not a.status or r["submission_status"] == a.status)
                and (not a.campaign or r["campaign"] == a.campaign)]
        print(f"{'clip_id':38} {'score':>5} {'dur':>4} {'status':10} {'platform':9} {'views':>7} {'payout':>7}  hook")
        for r in rows:
            print(f"{r['clip_id']:38} {r['score']:>5} {float(r['duration'] or 0):4.0f} {r['submission_status']:10} "
                  f"{r['platform']:9} {r['views']:>7} {r['payout']:>7}  {r['hook'][:50]}")
        print(f"{len(rows)} clip(s)")
        return 0

    if a.cmd == "submit":
        rows = [r for r in tracker.load() if r["clip_id"] == a.clip_id]
        if not rows:
            print("unknown clip id"); return 1
        r = rows[0]
        src = Path(r["exported_file"]) if r["exported_file"] else None
        if src and src.exists():
            dest = DIRS["submitted"] / r["campaign"] / src.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copy2(src, dest)
            print(f"copied to {dest}")
        tracker.update(a.clip_id, submission_status="submitted", platform=a.platform, notes=a.notes)
        print("marked submitted")
        return 0

    if a.cmd == "update":
        fields = {k: getattr(a, k) for k in ("submission_status", "platform", "views", "payout", "notes")}
        ok = tracker.update(a.clip_id, **fields)
        print("updated" if ok else "unknown clip id")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
