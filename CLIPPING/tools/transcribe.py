"""Step 2: TRANSCRIPTION with faster-whisper (local, free). Word-level timestamps are kept because
clip discovery and caption chunking both depend on them.

Output: transcripts/<source_id>.json  (+ .srt and .txt for humans)
Language: set transcription.language in config.json ('en' now, 'ar' later, 'auto' to detect).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from .util import DIRS, fmt_srt, read_json, write_json

LOG = logging.getLogger("clipping.transcribe")


def _pick_device(cfg: dict) -> tuple[str, str]:
    device = cfg.get("device", "auto")
    compute = cfg.get("compute_type", "auto")
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    return device, compute


def transcribe(source: dict, cfg: dict, *, force: bool = False) -> dict:
    out_json = DIRS["transcripts"] / f"{source['id']}.json"
    tcfg = cfg["transcription"]
    if out_json.exists() and not force:
        data = read_json(out_json)
        if data.get("model") == tcfg["model"]:
            LOG.info("Transcript cached: %s", out_json.name)
            return data
        LOG.info("Transcript exists but was made with model=%s; re-transcribing with %s.",
                 data.get("model"), tcfg["model"])

    try:
        from faster_whisper import WhisperModel
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("faster-whisper is not installed: pip install faster-whisper") from e

    device, compute = _pick_device(tcfg)
    LOG.info("Loading Whisper '%s' on %s (%s). First run downloads the model (~%s).",
             tcfg["model"], device, compute,
             {"tiny": "75MB", "base": "145MB", "small": "480MB", "medium": "1.5GB"}.get(tcfg["model"], "a few GB"))
    try:
        model = WhisperModel(tcfg["model"], device=device, compute_type=compute)
    except Exception as e:
        # Offline or blocked network: fall back to the already-downloaded copy if there is one.
        LOG.warning("Model download/check failed (%s). Trying the local cache...", str(e).splitlines()[0][:120])
        model = WhisperModel(tcfg["model"], device=device, compute_type=compute, local_files_only=True)

    language = tcfg.get("language", "en")
    lang_arg = None if language in (None, "", "auto") else language
    t0 = time.time()
    segments_iter, info = model.transcribe(
        source["audio"], language=lang_arg, beam_size=int(tcfg.get("beam_size", 5)),
        word_timestamps=True, vad_filter=bool(tcfg.get("vad_filter", True)),
        vad_parameters={"min_silence_duration_ms": 500}, condition_on_previous_text=False)

    segments = []
    total = float(source.get("info", {}).get("duration") or 0) or None
    last_report = 0.0
    for seg in segments_iter:
        words = [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word, "prob": round(w.probability, 3)}
                 for w in (seg.words or [])]
        segments.append({"start": round(seg.start, 3), "end": round(seg.end, 3),
                         "text": seg.text.strip(), "words": words})
        if total and seg.end - last_report > 120:
            last_report = seg.end
            LOG.info("  ...%.0f%% (%.1f min of audio in %.0fs)", 100 * seg.end / total, seg.end / 60, time.time() - t0)

    data = {"source_id": source["id"], "model": tcfg["model"], "language": info.language,
            "language_probability": round(float(info.language_probability or 0), 3),
            "duration": info.duration, "segments": segments}
    write_json(out_json, data)
    _write_srt(DIRS["transcripts"] / f"{source['id']}.srt", segments)
    (DIRS["transcripts"] / f"{source['id']}.txt").write_text(
        "\n".join(f"[{s['start']:8.1f}] {s['text']}" for s in segments), encoding="utf-8")
    LOG.info("Transcribed %d segments (%s) in %.0fs -> %s", len(segments), info.language, time.time() - t0, out_json.name)
    return data


def _write_srt(path: Path, segments: list[dict]) -> None:
    lines = []
    for i, s in enumerate(segments, 1):
        lines += [str(i), f"{fmt_srt(s['start'])} --> {fmt_srt(s['end'])}", s["text"], ""]
    path.write_text("\n".join(lines), encoding="utf-8")
