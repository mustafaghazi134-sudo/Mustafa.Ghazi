"""Speaker confirmation stage. Free and local (sherpa-onnx, ONNX models from GitHub releases, no torch).

Nothing is ever *assumed* to be the focus speaker. The flow is:

1. diarize()  - label who-speaks-when as SPEAKER_00, SPEAKER_01, ... and tag every transcript word.
                Output: transcripts/<id>.speakers.json  (+ speaker labels inside the transcript words)
2. You map labels to people once per source:  python speakers.py <source> --map SPEAKER_01="Ben Affleck"
                The CLI prints sample lines + timestamps per label so you can identify voices quickly.
                Mapping is stored in transcripts/<id>.speakermap.json
3. Each candidate gets a status:
      CONFIRMED     mapping exists and >= 70% of the clip's words are the focus speaker
      LIKELY        no mapping yet, but the clip's dominant voice is also the file's dominant voice
                    (the guest talks most in an interview) and the text is first-person storytelling
      NEEDS REVIEW  anything else, or diarization unavailable
      NOT <name>    mapping exists and the focus speaker has < 50% of the words -> candidate is dropped

If sherpa-onnx or its models are missing, the stage degrades to NEEDS REVIEW for everything.
"""
from __future__ import annotations

import io
import json
import logging
import re
import tarfile
import urllib.request
import wave
from collections import Counter
from pathlib import Path

from .util import DIRS, ROOT, read_json, write_json

LOG = logging.getLogger("clipping.speakers")
MODELS_DIR = ROOT / "models"
SEG_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
EMB_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_speakerverification_speakernet.onnx"
SEG_MODEL = MODELS_DIR / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
EMB_MODEL = MODELS_DIR / "nemo_en_speakerverification_speakernet.onnx"
FIRST_PERSON = re.compile(r"\b(?:i|i'm|i've|i'd|my|me|we|our)\b", re.I)


# ----------------------------------------------------------------- models

def ensure_models() -> bool:
    """Download the two ONNX models (~30 MB) once. Returns False if unavailable."""
    MODELS_DIR.mkdir(exist_ok=True)
    try:
        if not SEG_MODEL.exists():
            LOG.info("Downloading speaker segmentation model (7 MB)...")
            with urllib.request.urlopen(SEG_URL, timeout=120) as r:
                buf = io.BytesIO(r.read())
            with tarfile.open(fileobj=buf, mode="r:bz2") as tf:
                members = [m for m in tf.getmembers() if m.name.endswith(("model.onnx", "LICENSE", "README.md"))]
                tf.extractall(MODELS_DIR, members=members)
        if not EMB_MODEL.exists():
            LOG.info("Downloading speaker embedding model (23 MB)...")
            urllib.request.urlretrieve(EMB_URL, EMB_MODEL)
        return SEG_MODEL.exists() and EMB_MODEL.exists()
    except Exception as e:
        LOG.warning("Could not fetch diarization models (%s). Speaker stage will mark everything NEEDS REVIEW.", e)
        return False


# ----------------------------------------------------------------- diarization

def _load_wav16k(path: str):
    import numpy as np
    with wave.open(path) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1, "audio.wav must be 16 kHz mono (ingest makes it so)"
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def diarize(source: dict, transcript: dict, cfg: dict, campaign: dict | None = None, *, force: bool = False) -> dict | None:
    """Runs diarization (cached), tags transcript words with 'spk', returns the speakers record or None."""
    out = DIRS["transcripts"] / f"{source['id']}.speakers.json"
    if out.exists() and not force:
        rec = read_json(out)
        _tag_words(transcript, rec["turns"])
        return rec
    try:
        import sherpa_onnx
    except ImportError:
        LOG.warning("sherpa-onnx not installed (pip install sherpa-onnx). Speaker stage disabled.")
        return None
    if not ensure_models():
        return None

    scfg = cfg.get("speakers", {})
    expected = int((campaign or {}).get("expected_speakers") or scfg.get("expected_speakers") or -1)
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(SEG_MODEL))),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB_MODEL)),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=expected, threshold=float(scfg.get("threshold", 0.5))),
        min_duration_on=0.3, min_duration_off=0.5)
    if not config.validate():
        LOG.warning("Diarization config invalid; skipping speaker stage.")
        return None
    LOG.info("Diarizing %s (%s)...", source["id"], f"{expected} speakers" if expected > 0 else "auto speaker count")
    sd = sherpa_onnx.OfflineSpeakerDiarization(config)
    result = sd.process(_load_wav16k(source["audio"])).sort_by_start_time()
    turns = [{"start": round(r.start, 2), "end": round(r.end, 2), "spk": f"SPEAKER_{r.speaker:02d}"} for r in result]
    _tag_words(transcript, turns)

    talk = Counter()
    for t in turns:
        talk[t["spk"]] += t["end"] - t["start"]
    total = sum(talk.values()) or 1.0
    labels = {spk: {"talk_seconds": round(sec, 1), "share": round(sec / total, 3),
                    "samples": _samples(transcript, spk)} for spk, sec in talk.most_common()}
    rec = {"source_id": source["id"], "engine": "sherpa-onnx pyannote-seg-3.0 + nemo speakernet",
           "turns": turns, "labels": labels}
    write_json(out, rec)
    LOG.info("Speakers found: %s", ", ".join(f"{k} ({v['share']:.0%})" for k, v in labels.items()))
    return rec


def _tag_words(transcript: dict, turns: list[dict]) -> None:
    """Assign each word the label of the turn that overlaps its midpoint most (nearest turn as fallback)."""
    if not turns:
        return
    i = 0
    n = len(turns)
    for seg in transcript["segments"]:
        for w in seg.get("words", []):
            mid = (w["start"] + w["end"]) / 2
            while i + 1 < n and turns[i]["end"] < mid:
                i += 1
            j = i
            if not (turns[j]["start"] <= mid <= turns[j]["end"]):
                # fallback: nearest turn edge
                cands = [turns[k] for k in (max(0, j - 1), j, min(n - 1, j + 1))]
                j_turn = min(cands, key=lambda t: min(abs(t["start"] - mid), abs(t["end"] - mid)))
                w["spk"] = j_turn["spk"]
            else:
                w["spk"] = turns[j]["spk"]
        if seg.get("words"):
            seg["spk"] = Counter(w.get("spk", "?") for w in seg["words"]).most_common(1)[0][0]


def _samples(transcript: dict, spk: str, n: int = 4) -> list[dict]:
    segs = [s for s in transcript["segments"] if s.get("spk") == spk and len(s.get("text", "").split()) >= 6]
    if not segs:
        return []
    step = max(1, len(segs) // n)
    return [{"t": round(s["start"], 1), "text": s["text"][:110]} for s in segs[::step][:n]]


# ----------------------------------------------------------------- mapping + status

def map_path(source_id: str) -> Path:
    return DIRS["transcripts"] / f"{source_id}.speakermap.json"


def load_map(source_id: str) -> dict:
    p = map_path(source_id)
    return read_json(p) if p.exists() else {}


def save_map(source_id: str, mapping: dict) -> None:
    write_json(map_path(source_id), mapping)


def speaker_context(source_id: str, speakers: dict | None, campaign: dict) -> dict:
    """Everything discover() needs to judge a window: focus labels, dominant label, availability."""
    focus = (campaign.get("focus_speaker") or "").strip()
    mapping = load_map(source_id)
    focus_labels = {k for k, v in mapping.items() if v.strip().lower() == focus.lower()} if focus else set()
    dominant = None
    if speakers and speakers.get("labels"):
        dominant = next(iter(speakers["labels"]))  # labels are ordered by talk time
    return {"focus": focus, "mapping": mapping, "focus_labels": focus_labels,
            "dominant": dominant, "available": bool(speakers)}


def judge_window(words: list[dict], text: str, ctx: dict) -> tuple[str, float, str]:
    """Returns (status, focus_share, dominant_label_in_window)."""
    if not ctx.get("available") or not words:
        return "NEEDS REVIEW", 0.0, ""
    counts = Counter(w.get("spk", "?") for w in words)
    total = sum(counts.values()) or 1
    dom, dom_n = counts.most_common(1)[0]
    if ctx["focus_labels"]:
        share = sum(n for k, n in counts.items() if k in ctx["focus_labels"]) / total
        if share >= 0.7:
            return "CONFIRMED", share, dom
        if share >= 0.5:
            return "NEEDS REVIEW", share, dom
        return f"NOT {ctx['focus'].upper()}", share, dom
    # unmapped: dominant voice of the window == dominant voice of the file, and it's first-person talk
    share = dom_n / total
    first_person = len(FIRST_PERSON.findall(text)) >= 3
    if ctx.get("dominant") and dom == ctx["dominant"] and share >= 0.7 and first_person:
        return "LIKELY", share, dom
    return "NEEDS REVIEW", share, dom
