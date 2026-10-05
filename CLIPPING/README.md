# CLIPPING - local, free clipping pipeline

Turns long-form video (local file or public URL) into captioned 1080x1920 clips plus a ranked
candidate report. Everything runs on your laptop. No paid APIs, no uploads, no publishing.

```
python clip.py "C:\videos\podcast.mp4"
python clip.py "https://www.youtube.com/watch?v=XXXX" --campaign creator-x
```

## 1. Setup on Windows (one time, all free)

1. **Python 3.10+** - https://www.python.org/downloads/ (tick "Add python.exe to PATH").
2. **FFmpeg** - in PowerShell: `winget install Gyan.FFmpeg` then close and reopen the terminal.
3. **Git** (optional, only to pull updates): `winget install Git.Git`.
4. Python packages, from inside the `CLIPPING` folder:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
5. Check everything: `python tools\audit_env.py`

The first transcription downloads the Whisper model once (about 480 MB for `small`) from Hugging Face
and caches it. After that it works offline.

GPU: if you have an NVIDIA card, faster-whisper uses it automatically (it needs the CUDA 12 runtime
libraries; `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` adds them). Without a GPU, CPU works
fine: roughly 1 minute of processing per 4-6 minutes of audio with the `small` model on a modern laptop.

## 2. Daily workflow

```
# 1. Make the candidate report and all exports in one go
python clip.py "episode.mp4" --campaign creator-x

# 2. Or report first, look at candidates/<id>/report.md, then render only the ones you like
python clip.py "episode.mp4" --no-render
python clip.py "episode.mp4" --select 1,3,5

# 3. Track what you submit
python track.py submit <clip_id> --platform tiktok --notes "submitted to campaign X"
python track.py update <clip_id> --views 12400 --payout 6.20 --status paid
python track.py list --status submitted
```

Useful flags: `--top 10`, `--min-duration 20 --max-duration 60`, `--framing face|blur|center|left|right|0.35`,
`--model base|small|medium`, `--language ar`, `--force-transcribe`, `--force-render`, `--no-captions`.

Outputs:

| Folder | Contents |
|---|---|
| `source/<id>/` | untouched copy of the original, `audio.wav`, `source.json` |
| `transcripts/<id>.json` | word-level transcript (+ `.srt`, `.txt`) |
| `candidates/<id>/report.md` | ranked candidates with hook, score, reasons, timestamps |
| `exports/<campaign>/<id>/` | `clip_id.mp4` + `.ass` (burned-in style) + `.srt` sidecar |
| `submitted/<campaign>/` | copies of clips you marked as submitted |
| `data/tracker.csv` | one row per clip; open in Excel, edit views/payout/notes freely |
| `logs/` | one log per run |

## 3. Campaigns

Each paid campaign gets a brief in `campaigns/<name>.json` (copy `example-paid-campaign.json`).
It can override duration limits, number of candidates and framing, and holds the payout terms and
rules so they are next to the clips. Run with `--campaign <name>`.

## 4. Architecture

```
clip.py                 orchestrator (the only command you normally run)
track.py                tracker CLI
config.json             all knobs (durations, framing, caption style, model)
tools/ingest.py         1. local file or yt-dlp URL -> source/, extract 16 kHz audio (FFmpeg)
tools/transcribe.py     2. faster-whisper, word timestamps, cached per source
tools/discover.py       3. sentence rebuild -> windowed scoring -> dedupe -> top N
tools/render.py         4. FFmpeg: 9:16 crop/blur/face framing, H.264 + AAC, loudness normalised
tools/captions.py       5. short word chunks -> .ass (burned in via libass) + .srt
tools/qc.py             6. ffprobe + full decode pass + subtitle presence
tools/tracker.py        7. CSV upsert, never overwrites hand-entered views/payout/notes
tools/report.py         report.md + candidates.json
tools/audit_env.py      environment check (stdlib only)
```

Clip discovery is rule-based (no LLM): hook strength of the first sentence, keyword signals
(money, controversy, surprise, emotion, argument, humour, advice, story, curiosity), pacing and
dead air, clean ending, duration fit and a standalone check. Candidates always start and end on
sentence boundaries. Tune the `LEXICON` and weights at the top of `tools/discover.py`.
`rank_candidates()` is the one place to plug in a local LLM (e.g. Ollama) later if you want it.

## 5. Captions and safe zones

Captions are 1-3 word chunks, bold, uppercase, white with a black outline, centred about 72% down
the frame with 110 px side margins. That keeps them clear of the TikTok/Reels/Shorts UI and below a
centred face. Change `captions.position` to `middle` or adjust margins/font in `config.json`.
Word highlighting can be added later in `tools/captions.py` by emitting per-word `\c` colour tags.

## 6. Arabic later

Set `"language": "ar"` (or `auto`) in `config.json`. Whisper handles Arabic; use `medium` or
`large-v3` for good accuracy. For captions set `captions.font` to a font with Arabic glyphs such as
`Segoe UI` (ships with Windows) or `Noto Sans Arabic`; libass renders right-to-left text correctly.
The discovery lexicon is English; add Arabic patterns to `LEXICON` in `tools/discover.py`.

## 7. What can cost money (nothing by default)

- Nothing in this project calls a paid service. Whisper, FFmpeg, yt-dlp and OpenCV are free and local.
- Downloading a Whisper model uses bandwidth once.
- An optional local LLM re-ranker (Ollama) would also be free. Cloud LLM APIs would not; none are wired in.

## 8. Troubleshooting

- `ffmpeg not found`: reopen the terminal after installing, or set the full path in `config.json`.
- Transcription slow: use `--model base` for drafts, `small` for final; close other heavy apps.
- Captions missing from the video but the `.ass` exists: your FFmpeg build lacks libass. The Gyan.FFmpeg
  build includes it; `ffmpeg -filters | findstr subtitles` should show it.
- No faces found with `--framing face`: it falls back to center automatically. Try `blur` for podcasts
  with two people on screen.
- yt-dlp download fails: `pip install -U yt-dlp` (sites change often).
