"""Step 4: RENDERING with FFmpeg. Produces 1080x1920 H.264/AAC MP4 with captions burned in.

Framing (render.framing or --framing):
  center | left | right   fixed crop position on horizontal sources
  face                    OpenCV face detection on sampled frames picks the crop position (falls back to center)
  blur                    full-width video letterboxed over a blurred, zoomed copy (classic podcast look)
  0.0 - 1.0               manual horizontal centre of the crop, as a fraction of source width
The source file is read only; nothing is written back to it.
"""
from __future__ import annotations

import logging
import statistics
import subprocess
from pathlib import Path

from .util import find_binary, run

LOG = logging.getLogger("clipping.render")


def detect_face_center(ffmpeg: str, src: Path, start: float, duration: float, samples: int = 8) -> float | None:
    """Returns the horizontal centre of the dominant face as a 0-1 fraction, or None."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        LOG.info("OpenCV not installed; face framing unavailable, using center.")
        return None
    try:
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if cascade.empty():
            raise RuntimeError("cascade file missing")
    except Exception as e:
        LOG.warning("Face detector unavailable (%s). Install opencv-python-headless<5. Using center.", e)
        return None
    centers = []
    for k in range(samples):
        t = start + duration * (k + 0.5) / samples
        raw = subprocess.run([ffmpeg, "-v", "error", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
                              "-vf", "scale=960:-2", "-f", "image2pipe", "-vcodec", "png", "-"],
                             capture_output=True).stdout
        if not raw:
            continue
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.05, minNeighbors=4, minSize=(36, 36))
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            centers.append((x + w / 2) / img.shape[1])
    if not centers:
        LOG.info("No face found in sampled frames; using center.")
        return None
    c = statistics.median(centers)
    LOG.info("Face framing: centre at %.0f%% of width (%d/%d frames had a face).", c * 100, len(centers), samples)
    return c


def _video_filter(src_w: int, src_h: int, out_w: int, out_h: int, framing: str, face_x: float | None) -> str:
    target_ratio = out_w / out_h
    src_ratio = src_w / src_h if src_h else target_ratio

    if framing == "blur":
        # Blurred zoomed background + full-width foreground centred.
        return (f"split[bg][fg];"
                f"[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h},"
                f"gblur=sigma=30,eq=brightness=-0.08[bgb];"
                f"[fg]scale={out_w}:-2[fgs];[bgb][fgs]overlay=(W-w)/2:(H-h)/2,format=yuv420p")

    if src_ratio <= target_ratio + 0.01:
        # Already vertical or square-ish: scale to width and pad top/bottom.
        return (f"scale={out_w}:-2,pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p")

    # Horizontal source: crop a 9:16 column then scale.
    crop_w = int(src_h * target_ratio) // 2 * 2
    if framing == "left":
        frac = 0.25
    elif framing == "right":
        frac = 0.75
    elif framing == "face" and face_x is not None:
        frac = face_x
    else:
        try:
            frac = float(framing)
        except ValueError:
            frac = 0.5
    frac = min(max(frac, 0.0), 1.0)
    x = int(frac * src_w - crop_w / 2)
    x = max(0, min(src_w - crop_w, x))
    return f"crop={crop_w}:{src_h}:{x}:0,scale={out_w}:{out_h}:flags=lanczos,format=yuv420p"


def render_clip(source: dict, start: float, end: float, out_path: Path, cfg: dict, *,
                ass_path: Path | None = None, framing: str | None = None) -> Path:
    ffmpeg = find_binary(cfg["ffmpeg"])
    rcfg = cfg["render"]
    info = source["info"]
    framing = framing or rcfg.get("framing", "center")
    out_w, out_h = int(rcfg["width"]), int(rcfg["height"])
    duration = end - start
    src = Path(source["media"])

    face_x = None
    if framing == "face" and info["has_video"]:
        face_x = detect_face_center(ffmpeg, src, start, duration)

    if info["has_video"]:
        vf = _video_filter(info["width"], info["height"], out_w, out_h, framing, face_x)
    else:
        vf = None

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-y", "-hide_banner", "-v", "error", "-stats",
           "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src)]
    if not info["has_video"]:
        # Audio-only source: solid background so we still get a valid vertical video.
        cmd += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", f"color=c=0x101010:s={out_w}x{out_h}:r={int(rcfg['fps'])}"]
        vf = "format=yuv420p"
        maps = ["-map", "1:v:0", "-map", "0:a:0"]
    else:
        maps = ["-map", "0:v:0", "-map", "0:a:0"]

    if ass_path is not None:
        # libass is picky about Windows paths (drive-letter colons). We run FFmpeg from the clip's
        # folder and pass only the file name, which avoids escaping entirely.
        assert ass_path.parent == out_path.parent, "subtitle file must live next to the output"
        vf = (vf + "," if vf else "") + f"subtitles=filename={ass_path.name}"

    af = "loudnorm=I=-16:TP=-1.5:LRA=11" if rcfg.get("loudness_normalize", True) else None

    cmd += maps + ["-vf", vf, "-r", str(int(rcfg["fps"])),
                   "-c:v", "libx264", "-preset", rcfg.get("preset", "medium"), "-crf", str(rcfg.get("crf", 20)),
                   "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", rcfg.get("audio_bitrate", "160k"), "-ar", "48000", "-ac", "2"]
    if af:
        cmd += ["-af", af]
    cmd += ["-movflags", "+faststart", "-shortest", out_path.name]

    LOG.info("Rendering %s  [%s -> %s, framing=%s]", out_path.name, f"{start:.1f}s", f"{end:.1f}s", framing)
    run(cmd, cwd=out_path.parent)
    return out_path
