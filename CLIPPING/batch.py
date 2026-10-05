#!/usr/bin/env python3
"""Process a whole campaign library in one go and produce a cross-file TOP-N report.

    python batch.py --campaign ben-affleck-street-we-grew-up-on
    python batch.py --campaign ben-affleck-street-we-grew-up-on --top 20 --force-transcribe

Steps per file in source/library/<campaign>/: ffprobe verification -> ingest -> transcribe
(faster-whisper) -> diarize (sherpa-onnx) -> discover + compliance + speaker status -> per-source report.
Then all candidates are ranked together into candidates/<campaign>/TOP<N>.md (+ .json, .csv).
Nothing is rendered. Nothing is uploaded.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools import tracker  # noqa: E402
from tools.pipeline import analyze_source  # noqa: E402
from tools.speakers import load_map  # noqa: E402
from tools.util import DIRS, ROOT, find_binary, fmt_ts, load_campaign, load_config, media_info, setup_logging  # noqa: E402

MEDIA = {".mov", ".mp4", ".mkv", ".webm", ".m4v", ".m4a", ".mp3", ".wav"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--top", type=int, default=20, help="size of the cross-file shortlist")
    ap.add_argument("--per-source", type=int, default=15, help="candidates kept per file before global ranking")
    ap.add_argument("--library", help="override the library folder")
    ap.add_argument("--model", help="whisper model override: tiny | base | small | medium | large-v3")
    ap.add_argument("--force-transcribe", action="store_true")
    ap.add_argument("--force-diarize", action="store_true")
    a = ap.parse_args(argv)

    cfg = load_config()
    if a.model:
        cfg["transcription"]["model"] = a.model
    campaign = load_campaign(a.campaign)
    for key in ("min_duration", "max_duration", "top_n"):
        if key in campaign:
            cfg["discovery"][key] = campaign[key]
    lib = Path(a.library) if a.library else ROOT / campaign.get("library_dir", f"source/library/{a.campaign}")
    files = sorted(p for p in lib.glob("*") if p.suffix.lower() in MEDIA)
    log_path = setup_logging(f"batch_{a.campaign}")
    print(f"BATCH  campaign={campaign['name']}  library={lib}  files={len(files)}  log={log_path.name}")
    if not files:
        print(f"No media files found. Put the official library files in:\n  {lib}")
        return 1

    # 1. verify every file with ffprobe before doing anything heavy
    ffprobe = find_binary(cfg["ffprobe"])
    verified = []
    for f in files:
        try:
            info = media_info(ffprobe, f)
            ok = info["has_audio"] and info["duration"] > 1
            verified.append({"file": f.name, "bytes": f.stat().st_size, "ok": ok, **info})
            print(f"  {'OK ' if ok else 'BAD'} {f.name}: {fmt_ts(info['duration'])}  {info['width']}x{info['height']} "
                  f"{info['fps']:.3g}fps  v={info['vcodec']} a={info['acodec']}")
        except Exception as e:
            verified.append({"file": f.name, "bytes": f.stat().st_size, "ok": False, "error": str(e)[:200]})
            print(f"  BAD {f.name}: {e}")

    # 2-5. analyze each file
    t0 = time.time()
    all_cands, all_rejected, per_source = [], [], []
    for v, f in zip(verified, files):
        if not v["ok"]:
            continue
        print(f"\n=== {f.name} ===")
        try:
            res = analyze_source(str(f), cfg, campaign, top_n=a.per_source,
                                 force_transcribe=a.force_transcribe, force_diarize=a.force_diarize)
        except Exception as e:
            print(f"  FAILED: {e}")
            per_source.append({"file": f.name, "error": str(e)[:300]})
            continue
        sid = res["source"]["id"]
        spk = res["speakers"]
        per_source.append({"file": f.name, "source_id": sid, "report": str(res["report"]),
                           "candidates": len(res["candidates"]), "transcript": f"transcripts/{sid}.json",
                           "speakers": {k: {"share": v2["share"], "samples": v2["samples"]} for k, v2 in (spk or {}).get("labels", {}).items()},
                           "speaker_map": load_map(sid)})
        for c in res["candidates"]:
            c["source_id"] = sid
            tracker.upsert(_row(campaign, res["source"], c))
        all_cands += res["candidates"]
        all_rejected += [dict(r, source_id=sid) for r in res["rejected"]]

    # 6. global ranking
    all_cands.sort(key=lambda c: (-c["score"], -c["t1_appeal"], -c["standalone_score"]))
    top = all_cands[:a.top]
    for i, c in enumerate(top, 1):
        c["global_rank"] = i
    out_dir = DIRS["candidates"] / campaign["name"]
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir / f"TOP{a.top}.json", {"campaign": campaign["name"], "generated": time.strftime("%Y-%m-%d %H:%M"),
                                              "files": verified, "sources": per_source, "top": top,
                                              "auto_rejected": sorted(all_rejected, key=lambda r: -r["score"])[:20]})
    _write_csv(out_dir / f"TOP{a.top}.csv", top)
    md = _write_md(out_dir / f"TOP{a.top}.md", campaign, verified, per_source, top, all_rejected, a.top)
    print(f"\nDone in {(time.time() - t0) / 60:.1f} min. {len(all_cands)} candidates across {len(per_source)} files.")
    print(f"TOP {a.top} report: {md}")
    needs = sum(1 for c in top if c["speaker_status"] != "CONFIRMED")
    if needs:
        print(f"{needs} of the top {a.top} are not speaker-CONFIRMED. Map voices with:  python speakers.py --all {campaign['name']}")
    return 0


def _row(campaign, source, c):
    return {"campaign": campaign["name"], "source_id": source["id"], "source_title": source.get("title", ""),
            "clip_id": c["clip_id"], "start": c["start"], "end": c["end"], "duration": c["duration"], "hook": c["hook"],
            "score": c["score"], "t1_appeal": c["t1_appeal"], "standalone_10": c["standalone_score"],
            "compliance": c["compliance"], "speaker": c["speaker_status"], "standalone": "yes" if c["standalone"] else "no"}


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv(path, top):
    cols = ["global_rank", "source_file", "clip_id", "start", "end", "duration", "speaker_status", "speaker_share",
            "score", "t1_appeal", "standalone_score", "compliance", "compliance_reason", "hook", "text"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for c in top:
            w.writerow(c)


def _write_md(path, campaign, verified, per_source, top, rejected, n):
    L = [f"# TOP {n} candidates - {campaign.get('title', campaign['name'])}", "",
         f"Generated {time.strftime('%Y-%m-%d %H:%M')}. Nothing rendered, nothing published. Pick your 10 and reply with the ranks.", "",
         "## Source files (ffprobe)", "", "| File | Size | Duration | Video | Audio | Status |", "|---|---:|---:|---|---|---|"]
    for v in verified:
        if v.get("ok"):
            L.append(f"| {v['file']} | {v['bytes'] / 1e6:.0f} MB | {fmt_ts(v['duration'])} | {v['width']}x{v['height']} {v['fps']:.3g}fps {v['vcodec']} | {v['acodec']} | OK |")
        else:
            L.append(f"| {v['file']} | {v['bytes'] / 1e6:.0f} MB | - | - | - | FAILED: {v.get('error', 'no audio')} |")
    L += ["", "## Speaker confirmation", "",
          f"Focus speaker: **{campaign.get('focus_speaker', '-')}**. Statuses: CONFIRMED = you mapped the voice and it carries >=70% of the clip; "
          "LIKELY = unmapped, but the clip's dominant voice is the file's dominant voice and it is first-person talk; "
          "NEEDS REVIEW = listen before using. Map voices with `python speakers.py \"<file>\" --map SPEAKER_01=\"Ben Affleck\"` and re-run the batch.", ""]
    for s in per_source:
        if "error" in s:
            L.append(f"- **{s['file']}**: FAILED - {s['error']}")
            continue
        L.append(f"- **{s['file']}** (`{s['source_id']}`), {s['candidates']} candidates. Voices:")
        if not s["speakers"]:
            L.append("  - diarization not available for this file")
        for label, info in s["speakers"].items():
            who = s["speaker_map"].get(label, "unmapped")
            L.append(f"  - `{label}` talks {info['share']:.0%} -> **{who}**")
            for smp in info["samples"][:3]:
                L.append(f"    - [{fmt_ts(smp['t'])}] {smp['text']}")
    L += ["", f"## Top {n}", "",
          "| # | File | Start | End | Dur | Ben | Viral | T1 | SA | Compliance | Hook |",
          "|--:|---|---|---|--:|---|--:|--:|--:|---|---|"]
    for c in top:
        L.append(f"| {c['global_rank']} | {c['source_file']} | {fmt_ts(c['start'], ms=True)} | {fmt_ts(c['end'], ms=True)} | {c['duration']:.0f}s | "
                 f"{c['speaker_status']} | {c['score']} | {c['t1_appeal']} | {c['standalone_score']} | {c['compliance']} | {c['hook'].replace('|', '/')} |")
    L.append("")
    for c in top:
        L += [f"### {c['global_rank']}. {c['hook']}", "",
              f"- **Source file:** `{c['source_file']}`   **Clip id:** `{c['clip_id']}`",
              f"- **Start:** {fmt_ts(c['start'], ms=True)}   **End:** {fmt_ts(c['end'], ms=True)}   **Duration:** {c['duration']:.1f}s",
              f"- **Speaker / Ben confirmation:** {c['speaker_status']} (focus voice share {c['speaker_share']:.0%}, dominant label `{c['speaker_label'] or '-'}`)",
              f"- **Viral potential:** {c['score']}/100   **Tier-1 appeal:** {c['t1_appeal']}/10   **Standalone context:** {c['standalone_score']}/10",
              f"- **Compliance:** {c['compliance']} - {c['compliance_reason']}",
              f"- **Why:** {', '.join(c['reasons'])}",
              f"- **Suggested opening hook:** {c['hook']}",
              "- **Exact transcript excerpt:**", "", "> " + c["text"], ""]
    rej = sorted(rejected, key=lambda r: -r["score"])[:15]
    if rej:
        L += ["## Auto-rejected (strongest first, for information)", "", "| Viral | File | Start | End | Hook | Reason |", "|--:|---|---|---|---|---|"]
        for r in rej:
            L.append(f"| {r['score']} | {r['source_file']} | {fmt_ts(r['start'])} | {fmt_ts(r['end'])} | {r['hook'].replace('|', '/')} | {r['compliance_reason'].replace('|', '/')} |")
        L.append("")
    path.write_text("\n".join(L), encoding="utf-8")
    return path


if __name__ == "__main__":
    sys.exit(main())
