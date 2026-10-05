"""Shared helpers: paths, config, logging, ffmpeg/ffprobe wrappers, slugs, timecodes."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIRS = {name: ROOT / name for name in
        ("campaigns", "source", "transcripts", "candidates", "exports", "submitted", "tools", "data", "logs")}

_LOG = logging.getLogger("clipping")


# ---------------------------------------------------------------- config

def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path | None = None) -> dict:
    cfg_path = path or (ROOT / "config.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Allow a local override file that is not committed (config.local.json).
    local = ROOT / "config.local.json"
    if local.exists():
        with open(local, "r", encoding="utf-8") as f:
            cfg = _deep_merge(cfg, json.load(f))
    return cfg


def load_campaign(name: str) -> dict:
    """Campaign briefs live in campaigns/<name>.json. Missing file -> sensible defaults."""
    path = DIRS["campaigns"] / f"{name}.json"
    data = {"name": name, "platforms": ["tiktok", "reels", "shorts"], "notes": ""}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data.update(json.load(f))
    data["name"] = name
    return data


# ---------------------------------------------------------------- logging

def setup_logging(run_name: str) -> Path:
    DIRS["logs"].mkdir(parents=True, exist_ok=True)
    log_path = DIRS["logs"] / f"{time.strftime('%Y%m%d-%H%M%S')}_{run_name}.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(sh)
    # Windows consoles default to cp1252; make sure non-ASCII transcript text never crashes a print.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return log_path


# ---------------------------------------------------------------- binaries

def find_binary(name_or_path: str) -> str:
    """Resolve ffmpeg/ffprobe: explicit path, PATH, or common Windows install folders."""
    if os.path.isabs(name_or_path) and os.path.exists(name_or_path):
        return name_or_path
    found = shutil.which(name_or_path)
    if found:
        return found
    exe = name_or_path if name_or_path.lower().endswith(".exe") or os.name != "nt" else name_or_path + ".exe"
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / exe,
        Path("C:/ffmpeg/bin") / exe,
        Path("C:/Program Files/ffmpeg/bin") / exe,
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    raise FileNotFoundError(
        f"'{name_or_path}' not found. Install FFmpeg (Windows: `winget install Gyan.FFmpeg`) "
        f"or set the full path in config.json.")


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    _LOG.debug("RUN: %s", " ".join(str(c) for c in cmd))
    proc = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-2000:]
        raise RuntimeError(f"Command failed ({proc.returncode}): {cmd[0]}\n{tail}")
    if not quiet and proc.returncode != 0:
        _LOG.warning("Command returned %s: %s", proc.returncode, cmd[0])
    return proc


def ffprobe_json(ffprobe: str, path: Path) -> dict:
    proc = run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    return json.loads(proc.stdout or "{}")


def media_info(ffprobe: str, path: Path) -> dict:
    """Compact summary: duration, width, height, fps, has_audio, has_video, codecs."""
    data = ffprobe_json(ffprobe, path)
    info = {"duration": float(data.get("format", {}).get("duration", 0) or 0),
            "width": 0, "height": 0, "fps": 0.0, "has_video": False, "has_audio": False,
            "vcodec": None, "acodec": None, "size": int(data.get("format", {}).get("size", 0) or 0)}
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not info["has_video"]:
            info["has_video"] = True
            info["width"] = int(s.get("width", 0) or 0)
            info["height"] = int(s.get("height", 0) or 0)
            info["vcodec"] = s.get("codec_name")
            num, _, den = (s.get("avg_frame_rate") or "0/1").partition("/")
            try:
                info["fps"] = float(num) / float(den or 1)
            except (ValueError, ZeroDivisionError):
                info["fps"] = 0.0
        elif s.get("codec_type") == "audio" and not info["has_audio"]:
            info["has_audio"] = True
            info["acodec"] = s.get("codec_name")
    return info


# ---------------------------------------------------------------- text / ids

def slugify(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "video"


def short_hash(text: str, n: int = 6) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def is_url(s: str) -> bool:
    return bool(re.match(r"^https?://", s.strip(), re.I))


def fmt_ts(seconds: float, ms: bool = False) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if ms:
        return f"{h:02d}:{m:02d}:{s:06.3f}"
    return f"{h:02d}:{m:02d}:{int(s):02d}"


def fmt_srt(seconds: float) -> str:
    return fmt_ts(seconds, ms=True).replace(".", ",")


def fmt_ass(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
