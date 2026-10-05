"""Step 1: INGEST. Bring a local file or public URL into source/<id>/ and extract 16 kHz mono audio.

The original file is never modified. Local files are copied (default) so the project is self-contained;
set ingest.copy_local_sources=false in config.json to reference them in place instead.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .util import DIRS, find_binary, is_url, media_info, read_json, run, short_hash, slugify, write_json

LOG = logging.getLogger("clipping.ingest")
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".mp3", ".m4a", ".wav", ".flac"}


def _download_url(url: str, dest_dir: Path, fmt: str) -> tuple[Path, dict]:
    try:
        import yt_dlp  # noqa: WPS433
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("yt-dlp is not installed: pip install yt-dlp") from e

    dest_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": str(dest_dir / "original.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "writeinfojson": False,
        "restrictfilenames": True,
        "retries": 5,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        info = ydl.sanitize_info(info)
    files = [p for p in dest_dir.iterdir() if p.name.startswith("original.") and p.suffix.lower() in VIDEO_EXTS]
    if not files:
        raise RuntimeError(f"yt-dlp finished but no media file found in {dest_dir}")
    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    meta = {k: info.get(k) for k in ("id", "title", "uploader", "channel", "duration", "upload_date", "webpage_url")}
    return files[0], meta


def ingest(input_ref: str, cfg: dict, *, force_audio: bool = False) -> dict:
    """Returns a source record dict and writes it to source/<id>/source.json."""
    ffmpeg = find_binary(cfg["ffmpeg"])
    ffprobe = find_binary(cfg["ffprobe"])

    if is_url(input_ref):
        url = input_ref.strip()
        sid = None
        # Reuse an existing download of the same URL if present.
        for existing in DIRS["source"].glob("*/source.json"):
            rec = read_json(existing)
            if rec.get("origin") == url:
                sid = rec["id"]
                LOG.info("URL already ingested as %s, reusing.", sid)
                break
        if sid is None:
            tmp_dir = DIRS["source"] / f"_dl_{short_hash(url)}"
            LOG.info("Downloading with yt-dlp: %s", url)
            media_path, meta = _download_url(url, tmp_dir, cfg["ingest"]["ytdlp_format"])
            sid = f"{slugify(meta.get('title') or 'video')}-{short_hash(url)}"
            final_dir = DIRS["source"] / sid
            if final_dir.exists():
                shutil.rmtree(tmp_dir)
            else:
                tmp_dir.rename(final_dir)
            media_path = final_dir / media_path.name
            record = {"id": sid, "origin": url, "kind": "url", "title": meta.get("title") or sid, "meta": meta}
        else:
            record = read_json(DIRS["source"] / sid / "source.json")
            media_path = Path(record["media"])
    else:
        src = Path(input_ref).expanduser().resolve()
        if not src.exists():
            raise FileNotFoundError(f"Input file not found: {src}")
        if src.suffix.lower() not in VIDEO_EXTS:
            LOG.warning("Unusual extension %s; trying anyway.", src.suffix)
        sid = f"{slugify(src.stem)}-{short_hash(str(src))}"
        final_dir = DIRS["source"] / sid
        final_dir.mkdir(parents=True, exist_ok=True)
        if cfg["ingest"].get("copy_local_sources", True):
            media_path = final_dir / f"original{src.suffix.lower()}"
            if media_path.exists() and media_path.stat().st_size == src.stat().st_size:
                LOG.info("Source already copied: %s", media_path)
            else:
                LOG.info("Copying source into project (original untouched): %s", src.name)
                shutil.copy2(src, media_path)
        else:
            media_path = src
        record = {"id": sid, "origin": str(src), "kind": "local", "title": src.stem, "meta": {}}

    info = media_info(ffprobe, media_path)
    if not info["has_audio"]:
        raise RuntimeError("Source has no audio stream; nothing to transcribe.")

    audio_path = DIRS["source"] / sid / "audio.wav"
    if force_audio or not audio_path.exists():
        LOG.info("Extracting 16 kHz mono audio for transcription...")
        run([ffmpeg, "-y", "-v", "error", "-i", str(media_path), "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(audio_path)])

    record.update({"media": str(media_path), "audio": str(audio_path), "info": info})
    write_json(DIRS["source"] / sid / "source.json", record)
    LOG.info("Source %s: %.1f min, %dx%d, %s", sid, info["duration"] / 60, info["width"], info["height"],
             "video" if info["has_video"] else "audio only")
    return record
