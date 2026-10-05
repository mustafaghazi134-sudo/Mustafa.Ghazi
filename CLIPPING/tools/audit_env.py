#!/usr/bin/env python3
"""Environment audit. Run this first on the laptop:  python tools/audit_env.py
Uses only the standard library so it works before anything is installed."""
from __future__ import annotations

import importlib
import os
import platform
import shutil
import subprocess
import sys


def _ver(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        line = (out.stdout or out.stderr).strip().splitlines()
        return line[0][:90] if line else "(no output)"
    except Exception:
        return None


def main() -> int:
    ok, todo = [], []
    print(f"OS: {platform.system()} {platform.release()}  ({platform.machine()})")
    print(f"Python: {sys.version.split()[0]}  at {sys.executable}")
    if sys.version_info < (3, 10):
        todo.append("Python 3.10+ required (3.11 or 3.12 recommended): https://www.python.org/downloads/")
    else:
        ok.append("python")

    for name, winget in (("ffmpeg", "Gyan.FFmpeg"), ("ffprobe", "Gyan.FFmpeg"), ("git", "Git.Git")):
        path = shutil.which(name)
        print(f"{name}: {path or 'NOT FOUND'}" + (f"  [{_ver([name, '--version'])}]" if path else ""))
        (ok if path else todo).append(name if path else f"{name}: winget install {winget}  (then reopen the terminal)")

    print(f"yt-dlp: {shutil.which('yt-dlp') or 'not on PATH (fine if the Python package is installed)'}")

    pkgs = {"faster_whisper": "faster-whisper", "ctranslate2": "ctranslate2", "yt_dlp": "yt-dlp",
            "numpy": "numpy", "cv2": "opencv-python-headless (optional, face framing)"}
    for mod, pipname in pkgs.items():
        try:
            m = importlib.import_module(mod)
            print(f"package {pipname}: {getattr(m, '__version__', 'installed')}")
            ok.append(pipname)
        except Exception:
            print(f"package {pipname}: not installed")
            todo.append(f"pip install {pipname.split()[0]}")
    for mod in ("whisper", "torch", "vosk", "whisperx"):
        try:
            importlib.import_module(mod)
            print(f"also present: {mod}")
        except Exception:
            pass

    print("GPU:", end=" ")
    smi = _ver(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"])
    if smi:
        print(f"NVIDIA {smi}")
        try:
            import ctranslate2
            n = ctranslate2.get_cuda_device_count()
            print(f"  CTranslate2 sees {n} CUDA device(s)" + ("" if n else " -> faster-whisper will use CPU"))
        except Exception:
            print("  (install faster-whisper to check CUDA support)")
    else:
        print("no NVIDIA GPU detected -> CPU transcription (works, just slower; use model 'small' or 'base')")
    print(f"CPU threads: {os.cpu_count()}")

    print("\nREADY:", ", ".join(ok) or "-")
    print("TO DO:" if todo else "TO DO: nothing, you're set.")
    for t in todo:
        print("  -", t)
    return 0 if not todo else 1


if __name__ == "__main__":
    sys.exit(main())
