"""Writes the human-readable report for a run: candidates/<source_id>/report.md (+ candidates.json)."""
from __future__ import annotations

from pathlib import Path

from .util import DIRS, fmt_ts, write_json


def write_report(source: dict, campaign: dict, candidates: list[dict], out_dir: Path | None = None,
                 rejected: list[dict] | None = None) -> Path:
    out_dir = out_dir or (DIRS["candidates"] / source["id"])
    out_dir.mkdir(parents=True, exist_ok=True)
    rejected = rejected or []
    write_json(out_dir / "candidates.json", {"source": source, "campaign": campaign["name"], "candidates": candidates,
                                             "auto_rejected": rejected})

    lines = [f"# Clip report: {source.get('title', source['id'])}", "",
             f"- Source: `{source['origin']}`",
             f"- Source id: `{source['id']}`",
             f"- Campaign: **{campaign['name']}**",
             f"- Length: {fmt_ts(source['info']['duration'])}  ({source['info']['width']}x{source['info']['height']})",
             f"- Candidates: {len(candidates)}", "",
             "| # | Viral | T1 | Standalone | Compliance | Speaker | Start | End | Dur | Hook | Export | QC |",
             "|---|------:|---:|-----------:|:----------:|:-------:|------:|----:|----:|------|--------|----|"]
    if campaign.get("mandatory") or campaign.get("prohibited"):
        lines[7:7] = ["**Campaign rules (hard):** " + "; ".join(campaign.get("mandatory", [])),
                      "**Prohibited:** " + ", ".join(campaign.get("prohibited", [])),
                      "**Speaker:** statuses come from local diarization + your voice mapping (python speakers.py). Only CONFIRMED means the focus speaker was verified.", ""]
    for c in candidates:
        exp = Path(c["exported_file"]).name if c.get("exported_file") else "-"
        qc = "-" if "qc" not in c else ("PASS" if c["qc"]["pass"] else "FAIL")
        lines.append(f"| {c['rank']} | {c['score']} | {c.get('t1_appeal', '-')}/10 | {c.get('standalone_score', '-')}/10 | "
                     f"{c.get('compliance') or '-'} | {c.get('speaker_status') or '-'} | {fmt_ts(c['start'])} | {fmt_ts(c['end'])} | {c['duration']:.0f}s | "
                     f"{c['hook'].replace('|', '/')} | {exp} | {qc} |")
    lines.append("")
    for c in candidates:
        lines += [f"## {c['rank']}. {c['hook']}", "",
                  f"- **Start:** {fmt_ts(c['start'], ms=True)}   **End:** {fmt_ts(c['end'], ms=True)}   **Duration:** {c['duration']:.1f}s   "
                  f"**Clip id:** `{c['clip_id']}`",
                  f"- **Viral potential:** {c['score']}/100   **Tier-1 appeal:** {c.get('t1_appeal', '-')}/10   "
                  f"**Standalone context:** {c.get('standalone_score', '-')}/10",
                  f"- **Compliance:** {c.get('compliance') or 'not screened'}" +
                  (f" - {c['compliance_reason']}" if c.get('compliance_reason') else ""),
                  f"- **Speaker:** {c.get('speaker_status', 'NEEDS REVIEW')} (focus voice share {c.get('speaker_share', 0):.0%})",
                  f"- **Why it could work:** {', '.join(c['reasons']) or 'n/a'}",
                  f"- **Signals:** {', '.join(f'{k} x{v}' for k, v in c['categories'].items()) or 'none'}",
                  f"- **Standalone:** {'yes, understandable without the full video' if c['standalone'] else 'no, may need context'}",
                  f"- **Description:** {c['description']}"]
        if c.get("description_text"):
            lines += ["- **Post description (copy as-is):**", "", "```", c["description_text"], "```"]
        if c.get("exported_file"):
            lines.append(f"- **Export:** `{c['exported_file']}`")
        if c.get("qc"):
            lines.append(f"- **QC:** {'PASS' if c['qc']['pass'] else 'FAIL: ' + '; '.join(c['qc']['issues'])}")
        lines += ["", "> " + c["text"].replace("\n", " "), ""]
    if rejected:
        lines += ["## Auto-rejected by compliance (strongest first, for your information only)", "",
                  "| Viral | Start | End | Hook | Reason |", "|------:|------:|----:|------|--------|"]
        for r in rejected:
            lines.append(f"| {r['score']} | {fmt_ts(r['start'])} | {fmt_ts(r['end'])} | {r['hook'].replace('|', '/')} | "
                         f"{r['compliance_reason'].replace('|', '/')} |")
        lines.append("")
    path = out_dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
