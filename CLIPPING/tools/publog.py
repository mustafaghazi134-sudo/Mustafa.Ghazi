"""Append-only log of published links. Never rewrites or deletes earlier entries.

    python -m tools.publog add --file published/ben-affleck-youtube-links.md \
        --title "..." --clip-id y8yk4LNeXi --url https://youtube.com/shorts/... --status posted
    python -m tools.publog list --file published/ben-affleck-youtube-links.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HEADER = "# Published links\n\nAppend-only. One line per publish. Do not edit earlier lines.\n\n"


def add(file: Path, title: str, clip_id: str, url: str, status: str, project_id: str = "", platform: str = "youtube") -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    if not file.exists():
        file.write_text(HEADER, encoding="utf-8")
    existing = file.read_text(encoding="utf-8")
    if url and url in existing:
        print("already logged:", url)
        return
    n = len(re.findall(r"^\d+\. ", existing, re.M)) + 1
    meta = json.dumps({"clip_id": clip_id, "project_id": project_id, "platform": platform, "status": status,
                       "logged_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}, ensure_ascii=False)
    with open(file, "a", encoding="utf-8") as f:
        f.write(f"{n}. {title} — {url}  <!-- {meta} -->\n")
    print(f"logged #{n}: {title} — {url}")


def listing(file: Path) -> list[str]:
    if not file.exists():
        return []
    return [re.sub(r"\s*<!--.*?-->\s*$", "", ln) for ln in file.read_text(encoding="utf-8").splitlines() if re.match(r"^\d+\. ", ln)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--file", required=True); a.add_argument("--title", required=True); a.add_argument("--clip-id", required=True)
    a.add_argument("--url", required=True); a.add_argument("--status", default="posted"); a.add_argument("--project-id", default="")
    a.add_argument("--platform", default="youtube")
    ls = sub.add_parser("list"); ls.add_argument("--file", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "add":
        add(Path(args.file), args.title, args.clip_id, args.url, args.status, args.project_id, args.platform)
    else:
        print("\n".join(listing(Path(args.file))) or "(empty)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
