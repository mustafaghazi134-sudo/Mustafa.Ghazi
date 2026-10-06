#!/usr/bin/env python3
"""REVIEW PREVIEWS: cut 9:16 draft versions of an approved shortlist from the TOP-N report.

    python preview.py --campaign ben-affleck-street-we-grew-up-on --ranks 1,3,4,5,8,9,10,13,18,19

Reuses the existing pipeline pieces only: tools.render.render_clip (FFmpeg), tools.captions.build_captions
(draft captions from the cached transcript, wording untouched), tools.qc.check_export.
- Reads candidates/<campaign>/TOP20.json. No Whisper, no diarization, no downloads.
- Cuts from the local source file (the library original if still present, else the project's untouched copy).
- Renders ONLY the ranks you list. Speaker status is forced to NEEDS REVIEW on every output.
- Output: exports/<campaign>/REVIEW_PREVIEWS/R01_CLIP2_preview.mp4 (+ .ass/.srt) and REVIEW_MANIFEST.md/.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools.captions import build_captions  # noqa: E402
from tools.qc import check_export  # noqa: E402
from tools.render import render_clip  # noqa: E402
from tools.util import DIRS, fmt_ts, load_campaign, load_config, read_json, setup_logging  # noqa: E402

BANNER = "REVIEW DRAFT - NOT FOR SUBMISSION - speaker NEEDS REVIEW"


def _tag(source_file: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", Path(source_file).stem).upper() or "SRC"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--ranks", required=True, help="comma-separated GLOBAL ranks from TOP20.md, e.g. 1,3,4")
    ap.add_argument("--top-file", help="default: candidates/<campaign>/TOP20.json")
    ap.add_argument("--framing", help="override framing (center | face | blur | left | right | 0-1)")
    ap.add_argument("--no-captions", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-render previews that already exist")
    a = ap.parse_args(argv)

    cfg = load_config()
    campaign = load_campaign(a.campaign)
    if "framing" in campaign and not a.framing:
        cfg["render"]["framing"] = campaign["framing"]
    cfg["render"]["preset"] = "fast"  # drafts: speed over size
    top_path = Path(a.top_file) if a.top_file else DIRS["candidates"] / campaign["name"] / "TOP20.json"
    if not top_path.exists():
        print(f"Report not found: {top_path}. Run run_campaign.bat first.")
        return 1
    top = read_json(top_path)
    by_rank = {int(c["global_rank"]): c for c in top["top"]}
    try:
        ranks = [int(x) for x in a.ranks.split(",") if x.strip()]
    except ValueError:
        print("--ranks must be numbers, e.g. 1,3,4")
        return 1
    missing = [r for r in ranks if r not in by_rank]
    if missing:
        print(f"Ranks not in {top_path.name}: {missing}. Nothing rendered.")
        return 1

    out_dir = DIRS["exports"] / campaign["name"] / "REVIEW_PREVIEWS"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = setup_logging(f"preview_{campaign['name']}")
    print(f"REVIEW PREVIEWS  campaign={campaign['name']}  ranks={ranks}  out={out_dir}  log={log_path.name}")

    rows = []
    t0 = time.time()
    for rank in ranks:
        c = by_rank[rank]
        sid = c["source_id"]
        src_rec = read_json(DIRS["source"] / sid / "source.json")
        origin = Path(src_rec["origin"])
        media = origin if (src_rec.get("kind") == "local" and origin.exists()) else Path(src_rec["media"])
        source = dict(src_rec, media=str(media))
        stem = out_dir / f"R{rank:02d}_{_tag(c['source_file'])}_preview"
        mp4 = stem.with_suffix(".mp4")
        row = {"global_rank": rank, "source_file": c["source_file"], "cut_from": str(media),
               "start": c["start"], "end": c["end"], "start_tc": fmt_ts(c["start"], ms=True), "end_tc": fmt_ts(c["end"], ms=True),
               "duration": c["duration"], "hook": c["hook"], "output": mp4.name, "speaker_status": "NEEDS REVIEW",
               "compliance": c.get("compliance", ""), "compliance_reason": c.get("compliance_reason", ""),
               "viral": c.get("score"), "t1": c.get("t1_appeal"), "standalone": c.get("standalone_score"), "captions": "none"}
        ass = srt = None
        if not a.no_captions:
            tpath = DIRS["transcripts"] / f"{sid}.json"
            if tpath.exists():
                caps = build_captions(read_json(tpath), c["start"], c["end"], stem, cfg, banner=BANNER)
                ass, srt = caps["ass"], caps["srt"]
                row["captions"] = f"draft, {caps['chunks']} chunks from cached transcript"
            else:
                row["captions"] = "skipped (transcript not found; not re-running Whisper)"
        if mp4.exists() and not a.force:
            print(f"  R{rank:02d}: exists, skipping ({mp4.name})")
        else:
            try:
                render_clip(source, c["start"], c["end"], mp4, cfg, ass_path=ass, framing=a.framing)
            except Exception as e:
                print(f"  R{rank:02d}: RENDER FAILED: {e}")
                row["qc"] = f"FAIL: render error {str(e)[:120]}"
                rows.append(row)
                continue
        qc = check_export(mp4, c["duration"], cfg, ass_path=ass, srt_path=srt)
        row["qc"] = "PASS" if qc["pass"] else "FAIL: " + "; ".join(qc["issues"])
        rows.append(row)

    _manifest(out_dir, campaign, rows, top_path)
    ok = sum(1 for r in rows if r.get("qc") == "PASS")
    print(f"\nDone in {time.time() - t0:.0f}s. {ok}/{len(rows)} previews passed QC.")
    print(f"Folder:   {out_dir}\nManifest: {out_dir / 'REVIEW_MANIFEST.md'}")
    return 0 if ok == len(rows) else 2


def _manifest(out_dir: Path, campaign: dict, rows: list[dict], top_path: Path) -> None:
    (out_dir / "REVIEW_MANIFEST.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    L = [f"# REVIEW MANIFEST - {campaign.get('title', campaign['name'])}", "",
         f"Generated {time.strftime('%Y-%m-%d %H:%M')} from `{top_path.name}`. These are REVIEW DRAFTS, not submission files. "
         "Speaker identity is NOT confirmed on any of them: every clip stays NEEDS REVIEW until watched.", "",
         "| Rank | Source file | Start | End | Dur | Hook | Output | Speaker | Compliance | QC |",
         "|--:|---|---|---|--:|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['global_rank']} | {r['source_file']} | {r['start_tc']} | {r['end_tc']} | {r['duration']:.1f}s | "
                 f"{r['hook'].replace('|', '/')} | `{r['output']}` | {r['speaker_status']} | {r['compliance']} | {r['qc']} |")
    L.append("")
    for r in rows:
        L += [f"## R{r['global_rank']:02d} - {r['hook']}", "",
              f"- **Source file:** `{r['source_file']}`  (cut from `{r['cut_from']}`)",
              f"- **Original start/end:** {r['start_tc']} - {r['end_tc']}  ({r['start']}s - {r['end']}s)   **Duration:** {r['duration']:.1f}s",
              f"- **Output:** `{r['output']}`",
              f"- **Speaker status:** {r['speaker_status']}",
              f"- **Compliance:** {r['compliance']}" + (f" - {r['compliance_reason']}" if r.get("compliance_reason") else ""),
              f"- **Scores (from analysis):** viral {r['viral']}/100, Tier-1 {r['t1']}/10, standalone {r['standalone']}/10",
              f"- **Captions:** {r['captions']}",
              f"- **QC:** {r['qc']}", ""]
    (out_dir / "REVIEW_MANIFEST.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
