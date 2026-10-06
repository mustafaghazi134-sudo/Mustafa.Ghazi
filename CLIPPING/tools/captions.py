"""Step 5: CAPTIONS. Builds short word-chunk subtitles from Whisper word timestamps and writes
an .ass file (burned in by FFmpeg/libass) plus an .srt sidecar.

Safe zone: by default captions sit about 72% down the 1080x1920 frame, centred, with generous side
margins, which keeps them clear of TikTok/Reels/Shorts UI overlays and below a centred face.
Arabic later: set captions.font to a font with Arabic glyphs (e.g. "Segoe UI" or "Noto Sans Arabic");
libass handles right-to-left shaping.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .util import fmt_ass, fmt_srt

LOG = logging.getLogger("clipping.captions")


def words_in_range(transcript: dict, start: float, end: float) -> list[dict]:
    out = []
    for seg in transcript["segments"]:
        if seg["end"] < start or seg["start"] > end:
            continue
        for w in seg.get("words", []):
            mid = (w["start"] + w["end"]) / 2
            if start <= mid <= end:
                out.append(w)
    return out


def chunk_words(words: list[dict], max_words: int, max_chars: int, gap_break: float = 0.7) -> list[dict]:
    """Group words into short caption chunks. Breaks on punctuation, long pauses, or size limits."""
    chunks: list[dict] = []
    cur: list[dict] = []

    def flush():
        if cur:
            text = "".join(w["word"] for w in cur).strip()
            text = re.sub(r"\s+", " ", text)
            chunks.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": text})
            cur.clear()

    for i, w in enumerate(words):
        token = w["word"].strip()
        if cur:
            prev = cur[-1]
            cur_len = len("".join(x["word"] for x in cur).strip())
            if (w["start"] - prev["end"] > gap_break or len(cur) >= max_words
                    or cur_len + 1 + len(token) > max_chars):
                flush()
        cur.append(w)
        if re.search(r"[.!?]$", token) or (re.search(r"[,;:]$", token) and len(cur) >= 2):
            flush()
    flush()
    # Enforce a minimum on-screen time and no overlaps.
    for i, c in enumerate(chunks):
        nxt_start = chunks[i + 1]["start"] if i + 1 < len(chunks) else None
        min_end = c["start"] + 0.45
        c["end"] = max(c["end"], min_end)
        if nxt_start is not None:
            c["end"] = min(c["end"], nxt_start - 0.02) if nxt_start - 0.02 > c["start"] else c["end"]
    return chunks


def _ass_escape(text: str) -> str:
    return text.replace("{", "(").replace("}", ")").replace("\\", "/").replace("\n", "\\N")


def write_ass(chunks: list[dict], path: Path, ccfg: dict, clip_start: float, width: int, height: int,
              banner: str | None = None, clip_end: float | None = None) -> None:
    """banner: optional small text pinned to the top of the frame for the whole clip (used for review drafts)."""
    margin_v = int(ccfg["margin_v_middle"] if ccfg.get("position") == "middle" else ccfg["margin_v_lower"])
    style = (f"Style: Default,{ccfg['font']},{int(ccfg['font_size'])},{ccfg['primary_color']},&H000000FF,"
             f"{ccfg['outline_color']},&H80000000,-1,0,0,0,100,100,0,0,1,{int(ccfg['outline'])},{int(ccfg['shadow'])},"
             f"2,{int(ccfg['margin_h'])},{int(ccfg['margin_h'])},{margin_v},1")
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
             "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
             "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
             "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
             "MarginR, MarginV, Encoding",
             style]
    if banner:
        lines.append(f"Style: Banner,{ccfg['font']},34,&H0000D7FF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,0,"
                     f"8,{int(ccfg['margin_h'])},{int(ccfg['margin_h'])},220,1")
    lines += ["", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    if banner:
        end = (clip_end - clip_start) if clip_end else (max((c["end"] for c in chunks), default=clip_start) - clip_start + 5)
        lines.append(f"Dialogue: 1,{fmt_ass(0)},{fmt_ass(end)},Banner,,0,0,0,,{_ass_escape(banner)}")
    upper = bool(ccfg.get("uppercase", True))
    for c in chunks:
        text = c["text"].upper() if upper else c["text"]
        lines.append(f"Dialogue: 0,{fmt_ass(c['start'] - clip_start)},{fmt_ass(c['end'] - clip_start)},Default,,0,0,0,,{_ass_escape(text)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_srt(chunks: list[dict], path: Path, clip_start: float) -> None:
    lines = []
    for i, c in enumerate(chunks, 1):
        lines += [str(i), f"{fmt_srt(c['start'] - clip_start)} --> {fmt_srt(c['end'] - clip_start)}", c["text"], ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def build_captions(transcript: dict, start: float, end: float, out_stem: Path, cfg: dict, banner: str | None = None) -> dict:
    ccfg = cfg["captions"]
    rcfg = cfg["render"]
    words = words_in_range(transcript, start, end)
    chunks = chunk_words(words, int(ccfg["max_words_per_chunk"]), int(ccfg["max_chars_per_chunk"]))
    ass_path = out_stem.with_suffix(".ass")
    srt_path = out_stem.with_suffix(".srt")
    write_ass(chunks, ass_path, ccfg, start, int(rcfg["width"]), int(rcfg["height"]), banner=banner, clip_end=end)
    write_srt(chunks, srt_path, start)
    LOG.info("Captions: %d chunks -> %s", len(chunks), ass_path.name)
    return {"ass": ass_path, "srt": srt_path, "chunks": len(chunks)}
