"""Step 6: QUALITY CONTROL. Verifies an export is actually submission-ready."""
from __future__ import annotations

import logging
from pathlib import Path

from .util import find_binary, media_info, run

LOG = logging.getLogger("clipping.qc")


def check_export(video: Path, expected_duration: float, cfg: dict, *, ass_path: Path | None = None,
                 srt_path: Path | None = None) -> dict:
    ffmpeg = find_binary(cfg["ffmpeg"])
    ffprobe = find_binary(cfg["ffprobe"])
    want_w, want_h = int(cfg["render"]["width"]), int(cfg["render"]["height"])
    issues: list[str] = []

    if not video.exists() or video.stat().st_size < 10_000:
        return {"pass": False, "issues": ["file missing or empty"], "info": {}}

    info = media_info(ffprobe, video)
    if not info["has_video"]:
        issues.append("no video stream")
    if not info["has_audio"]:
        issues.append("no audio stream")
    if (info["width"], info["height"]) != (want_w, want_h):
        issues.append(f"resolution {info['width']}x{info['height']} != {want_w}x{want_h}")
    if info["vcodec"] != "h264":
        issues.append(f"video codec {info['vcodec']} != h264")
    if info["acodec"] != "aac":
        issues.append(f"audio codec {info['acodec']} != aac")
    if abs(info["duration"] - expected_duration) > 1.0:
        issues.append(f"duration {info['duration']:.2f}s vs expected {expected_duration:.2f}s")
    if info["duration"] < 3:
        issues.append("too short")

    # Full decode pass: catches corrupt frames / truncated files. Any decoder error is reported.
    proc = run([ffmpeg, "-v", "error", "-xerror", "-i", str(video), "-f", "null", "-"], check=False)
    if proc.returncode != 0 or (proc.stderr or "").strip():
        issues.append("decode errors: " + (proc.stderr or "").strip().splitlines()[0][:160])

    if cfg["captions"].get("enabled", True):
        for label, p in (("ass", ass_path), ("srt", srt_path)):
            if p is None or not p.exists() or p.stat().st_size == 0:
                issues.append(f"{label} subtitle file missing")

    ok = not issues
    LOG.info("QC %s: %s", "PASS" if ok else "FAIL", video.name if ok else "; ".join(issues))
    return {"pass": ok, "issues": issues, "info": info}
