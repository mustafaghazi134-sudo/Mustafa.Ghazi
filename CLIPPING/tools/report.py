"""Writes the human-readable report for a run: candidates/<source_id>/report.md (+ candidates.json)."""
from __future__ import annotations

from pathlib import Path

from .util import DIRS, fmt_ts, write_json


def write_report(source: dict, campaign: dict, candidates: list[dict], out_dir: Path | None = None) -> Path:
    out_dir = out_dir or (DIRS["candidates"] / source["id"])
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "candidates.json", {"source": source, "campaign": campaign["name"], "candidates": candidates})

    lines = [f"# Clip report: {source.get('title', source['id'])}", "",
             f"- Source: `{source['origin']}`",
             f"- Source id: `{source['id']}`",
             f"- Campaign: **{campaign['name']}**",
             f"- Length: {fmt_ts(source['info']['duration'])}  ({source['info']['width']}x{source['info']['height']})",
             f"- Candidates: {len(candidates)}", "",
             "| # | Score | Start | End | Dur | Standalone | Hook | Export | QC |",
             "|---|------:|------:|----:|----:|:----------:|------|--------|----|"]
    for c in candidates:
        exp = Path(c["exported_file"]).name if c.get("exported_file") else "-"
        qc = "-" if "qc" not in c else ("PASS" if c["qc"]["pass"] else "FAIL")
        lines.append(f"| {c['rank']} | {c['score']} | {fmt_ts(c['start'])} | {fmt_ts(c['end'])} | {c['duration']:.0f}s | "
                     f"{'yes' if c['standalone'] else 'no'} | {c['hook'].replace('|', '/')} | {exp} | {qc} |")
    lines.append("")
    for c in candidates:
        lines += [f"## {c['rank']}. {c['hook']}", "",
                  f"- **Score:** {c['score']}/100   **Time:** {fmt_ts(c['start'])} - {fmt_ts(c['end'])} ({c['duration']:.1f}s)   "
                  f"**Clip id:** `{c['clip_id']}`",
                  f"- **Why it could work:** {', '.join(c['reasons']) or 'n/a'}",
                  f"- **Signals:** {', '.join(f'{k} x{v}' for k, v in c['categories'].items()) or 'none'}",
                  f"- **Standalone:** {'yes, understandable without the full video' if c['standalone'] else 'no, may need context'}",
                  f"- **Description:** {c['description']}"]
        if c.get("exported_file"):
            lines.append(f"- **Export:** `{c['exported_file']}`")
        if c.get("qc"):
            lines.append(f"- **QC:** {'PASS' if c['qc']['pass'] else 'FAIL: ' + '; '.join(c['qc']['issues'])}")
        lines += ["", "> " + c["text"].replace("\n", " "), ""]
    path = out_dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
