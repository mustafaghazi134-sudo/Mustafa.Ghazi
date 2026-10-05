#!/usr/bin/env python3
"""CLIPPING - local, free, long-form -> short-form clipping pipeline.

    python clip.py "C:\\videos\\podcast.mp4"
    python clip.py "https://www.youtube.com/watch?v=XXXX" --campaign creator-x --top 10
    python clip.py "podcast.mp4" --no-render            # transcript + candidate report only
    python clip.py "podcast.mp4" --select 1,3,5         # render just those ranks from the report
    python clip.py "podcast.mp4" --framing face         # face-aware crop (needs opencv)

Outputs
    source/<id>/            original copy + audio.wav + source.json
    transcripts/<id>.json   word-level transcript (+ .srt/.txt)
    candidates/<id>/        report.md + candidates.json
    exports/<campaign>/<id>/<clip_id>.mp4 (+ .ass, .srt)
    data/tracker.csv        one row per clip
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools import tracker  # noqa: E402
from tools.captions import build_captions  # noqa: E402
from tools.discover import discover  # noqa: E402
from tools.ingest import ingest  # noqa: E402
from tools.qc import check_export  # noqa: E402
from tools.render import render_clip  # noqa: E402
from tools.report import write_report  # noqa: E402
from tools.transcribe import transcribe  # noqa: E402
from tools.compliance import description_for  # noqa: E402
from tools.util import DIRS, is_url, load_campaign, load_config, setup_logging, slugify  # noqa: E402


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Generate short-form clip candidates from a long video.")
    p.add_argument("input", help="local video/audio file or public video URL")
    p.add_argument("--campaign", default="default", help="campaign name (campaigns/<name>.json optional)")
    p.add_argument("--top", type=int, help="how many candidates to keep (default from config)")
    p.add_argument("--min-duration", type=float, help="seconds")
    p.add_argument("--max-duration", type=float, help="seconds")
    p.add_argument("--framing", help="center | left | right | face | blur | 0.0-1.0")
    p.add_argument("--model", help="whisper model: tiny | base | small | medium | large-v3")
    p.add_argument("--language", help="en | ar | auto ...")
    p.add_argument("--no-render", action="store_true", help="stop after the candidate report")
    p.add_argument("--no-captions", action="store_true")
    p.add_argument("--select", help="comma-separated ranks to render, e.g. 1,3,5 (default: all)")
    p.add_argument("--force-transcribe", action="store_true", help="ignore cached transcript")
    p.add_argument("--force-render", action="store_true", help="re-render clips that already exist")
    p.add_argument("--approve", help="comma-separated ranks you approved for rendering (required for campaigns "
                                     "with require_approval_before_render), or 'all'")
    p.add_argument("--library-url", action="store_true",
                   help="confirm that a URL input is the official campaign content library (library-only campaigns)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    cfg = load_config()
    if args.model:
        cfg["transcription"]["model"] = args.model
    if args.language:
        cfg["transcription"]["language"] = args.language
    if args.min_duration:
        cfg["discovery"]["min_duration"] = args.min_duration
    if args.max_duration:
        cfg["discovery"]["max_duration"] = args.max_duration
    if args.no_captions:
        cfg["captions"]["enabled"] = False

    campaign = load_campaign(args.campaign)
    # Campaign briefs can narrow durations/platforms without touching config.json.
    for key in ("min_duration", "max_duration", "top_n"):
        if key in campaign:
            cfg["discovery"][key] = campaign[key]
    if "framing" in campaign and not args.framing:
        cfg["render"]["framing"] = campaign["framing"]

    log_path = setup_logging(slugify(Path(args.input).stem if not args.input.startswith("http") else "url"))
    t0 = time.time()
    print(f"CLIPPING  campaign={campaign['name']}  log={log_path.name}")
    if campaign.get("source_policy") == "library_only":
        lib = DIRS["source"].parent / campaign.get("library_dir", "source/library/" + campaign["name"])
        if is_url(args.input) and not args.library_url:
            print("This campaign allows ONLY footage from the official content library. URL inputs are refused.\n"
                  "Download the file from the library, put it in", lib, "and pass the file path, or add --library-url "
                  "if this URL *is* the official library link.")
            return 3
        if not is_url(args.input):
            src_path = Path(args.input).expanduser().resolve()
            if lib.resolve() not in src_path.parents:
                print(f"NOTE: library-only campaign. Make sure this file came from the official content library "
                      f"(recommended folder: {lib}).")

    # 1. ingest
    source = ingest(args.input, cfg)

    # 2. transcribe
    transcript = transcribe(source, cfg, force=args.force_transcribe)

    # 3. discover
    rejected: list = []
    cands = discover(transcript, cfg, top_n=args.top, source_duration=source["info"]["duration"], campaign=campaign,
                     rejected_out=rejected)
    rejected_dicts = [r.to_dict() for r in rejected]
    if not cands:
        print("No candidates found. Try lowering --min-duration or check the transcript in transcripts/.")
        return 2
    cand_dicts = [c.to_dict() for c in cands]
    for c in cand_dicts:
        c["description_text"] = description_for(c, campaign)
    report_path = write_report(source, campaign, cand_dicts, rejected=rejected_dicts)
    print(f"\nTop candidates ({len(cands)}):")
    for c in cands:
        flag = "" if c.standalone else "  [needs context]"
        comp = f"  {c.compliance}" if c.compliance else ""
        extra = f"  T1 {c.t1_appeal}/10  SA {c.standalone_score}/10" if campaign.get("priorities") else ""
        print(f"  {c.rank:>2}. {c.score:>3}/100{extra}{comp}  {c.start:7.1f}s - {c.end:7.1f}s ({c.duration:4.0f}s)  {c.hook}{flag}")
    print(f"Report: {report_path}")

    gate = campaign.get("require_approval_before_render", False)
    if args.no_render or (gate and not args.approve and not args.select):
        for c in cand_dicts:
            tracker.upsert(_track_row(campaign, source, c))
        if gate and not args.no_render:
            print("\nApproval gate: nothing rendered. Review the report, then re-run with --approve 1,3,5 (or --approve all).")
        return 0
    if args.approve and not args.select:
        args.select = None if args.approve.strip().lower() == "all" else args.approve

    # 4-7. render, captions, QC, tracking
    wanted = None
    if args.select:
        wanted = {int(x) for x in args.select.split(",") if x.strip()}
    out_dir = DIRS["exports"] / campaign["name"] / source["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered = 0
    for c in cand_dicts:
        if wanted and c["rank"] not in wanted:
            continue
        stem = out_dir / c["clip_id"]
        mp4 = stem.with_suffix(".mp4")
        ass = srt = None
        if cfg["captions"]["enabled"]:
            caps = build_captions(transcript, c["start"], c["end"], stem, cfg)
            ass, srt = caps["ass"], caps["srt"]
        if mp4.exists() and not args.force_render:
            print(f"  exists, skipping render: {mp4.name} (use --force-render to redo)")
        else:
            try:
                render_clip(source, c["start"], c["end"], mp4, cfg, ass_path=ass, framing=args.framing)
                rendered += 1
            except Exception as e:  # keep going; one bad clip shouldn't kill the batch
                print(f"  RENDER FAILED for {c['clip_id']}: {e}")
                c["qc"] = {"pass": False, "issues": [f"render failed: {e}"][:1], "info": {}}
                tracker.upsert(_track_row(campaign, source, c))
                continue
        c["exported_file"] = str(mp4)
        if c.get("description_text"):
            stem.with_suffix(".description.txt").write_text(c["description_text"], encoding="utf-8")
        c["qc"] = check_export(mp4, c["duration"], cfg, ass_path=ass, srt_path=srt)
        tracker.upsert(_track_row(campaign, source, c))

    report_path = write_report(source, campaign, cand_dicts, rejected=rejected_dicts)
    passed = sum(1 for c in cand_dicts if c.get("qc", {}).get("pass"))
    print(f"\nDone in {time.time() - t0:.0f}s. Rendered {rendered}, QC passed {passed}/{len([c for c in cand_dicts if 'qc' in c])}.")
    print(f"Exports: {out_dir}\nReport:  {report_path}\nTracker: {tracker.TRACKER}")
    return 0


def _track_row(campaign: dict, source: dict, c: dict) -> dict:
    return {"campaign": campaign["name"], "source_id": source["id"], "source_title": source.get("title", ""),
            "clip_id": c["clip_id"], "start": c["start"], "end": c["end"], "duration": c["duration"],
            "hook": c["hook"], "score": c["score"], "standalone": "yes" if c["standalone"] else "no",
            "exported_file": c.get("exported_file", ""), "t1_appeal": c.get("t1_appeal", ""),
            "standalone_10": c.get("standalone_score", ""), "compliance": c.get("compliance", ""),
            "qc_pass": "" if "qc" not in c else ("yes" if c["qc"]["pass"] else "no: " + "; ".join(c["qc"]["issues"]))}


if __name__ == "__main__":
    sys.exit(main())
