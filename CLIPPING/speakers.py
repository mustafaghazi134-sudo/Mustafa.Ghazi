#!/usr/bin/env python3
"""Identify and map speakers for a source (run after clip.py / batch.py has transcribed it).

    python speakers.py "source\\library\\<campaign>\\CLIP 1.mov"            # show voices with sample lines
    python speakers.py "source\\library\\<campaign>\\CLIP 1.mov" --map SPEAKER_01="Ben Affleck" SPEAKER_00=Host
    python speakers.py --all ben-affleck-street-we-grew-up-on              # show every source in a campaign library

Listen at the printed timestamps, decide which label is Ben, then --map. Re-run batch.py / clip.py
afterwards: statuses become CONFIRMED / NOT BEN instead of LIKELY / NEEDS REVIEW.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools import speakers as sp  # noqa: E402
from tools.util import DIRS, fmt_ts, load_campaign, read_json, short_hash, slugify  # noqa: E402


def _source_id_for(path: str) -> str:
    src = Path(path).expanduser().resolve()
    return f"{slugify(src.stem)}-{short_hash(str(src))}"


def show(source_id: str) -> int:
    p = DIRS["transcripts"] / f"{source_id}.speakers.json"
    if not p.exists():
        print(f"no diarization yet for {source_id} (run clip.py/batch.py first)")
        return 1
    rec = read_json(p)
    mapping = sp.load_map(source_id)
    print(f"\n== {source_id} ==")
    for label, info in rec["labels"].items():
        who = mapping.get(label, "?")
        print(f"{label}  talks {info['share']:.0%} ({info['talk_seconds']:.0f}s)  ->  {who}")
        for s in info["samples"]:
            print(f"    [{fmt_ts(s['t'])}] {s['text']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", nargs="?", help="source file path (as given to clip.py) or source id")
    ap.add_argument("--all", metavar="CAMPAIGN", help="show every source in the campaign library folder")
    ap.add_argument("--map", nargs="*", metavar="LABEL=NAME", help='e.g. SPEAKER_01="Ben Affleck" SPEAKER_00=Host')
    a = ap.parse_args(argv)

    if a.all:
        camp = load_campaign(a.all)
        lib = DIRS["source"].parent / camp.get("library_dir", f"source/library/{a.all}")
        files = sorted(p for p in lib.glob("*") if p.suffix.lower() in (".mov", ".mp4", ".mkv", ".m4a", ".mp3", ".wav"))
        if not files:
            print(f"no media in {lib}")
            return 1
        for f in files:
            show(_source_id_for(str(f)))
        return 0

    if not a.source:
        ap.print_help()
        return 1
    sid = a.source if (DIRS["transcripts"] / f"{a.source}.json").exists() else _source_id_for(a.source)
    if a.map:
        mapping = sp.load_map(sid)
        for item in a.map:
            label, _, name = item.partition("=")
            mapping[label.strip().upper()] = name.strip().strip('"')
        sp.save_map(sid, mapping)
        print("saved mapping:", mapping)
    return show(sid)


if __name__ == "__main__":
    sys.exit(main())
