"""The analysis half of the pipeline as one reusable function, shared by clip.py (single source)
and batch.py (whole campaign library). Rendering stays in clip.py behind the approval gate."""
from __future__ import annotations

import logging
from pathlib import Path

from .compliance import description_for
from .discover import discover
from .ingest import ingest
from .report import write_report
from .speakers import diarize, speaker_context
from .transcribe import transcribe
from .util import DIRS

LOG = logging.getLogger("clipping.pipeline")


def analyze_source(input_ref: str, cfg: dict, campaign: dict, *, top_n: int | None = None,
                   force_transcribe: bool = False, force_diarize: bool = False) -> dict:
    """ingest -> transcribe -> diarize -> discover (+compliance, +speaker status) -> per-source report.
    Returns {"source", "transcript", "candidates": [dict], "rejected": [dict], "report": Path}."""
    source = ingest(input_ref, cfg)
    transcript = transcribe(source, cfg, force=force_transcribe)

    speakers = None
    if cfg.get("speakers", {}).get("enabled", True) and campaign.get("focus_speaker"):
        try:
            speakers = diarize(source, transcript, cfg, campaign, force=force_diarize)
        except Exception as e:  # never let the speaker stage kill a run
            LOG.warning("Speaker stage failed (%s); statuses will be NEEDS REVIEW.", e)
    ctx = speaker_context(source["id"], speakers, campaign) if campaign.get("focus_speaker") else None

    rejected: list = []
    cands = discover(transcript, cfg, top_n=top_n, source_duration=source["info"]["duration"],
                     campaign=campaign, rejected_out=rejected, speaker_ctx=ctx)
    src_name = Path(source["origin"]).name if source.get("kind") == "local" else source.get("title", source["id"])
    cand_dicts = []
    for c in cands:
        c.source_file = src_name
        d = c.to_dict()
        d["description_text"] = description_for(d, campaign)
        cand_dicts.append(d)
    rejected_dicts = []
    for r in rejected:
        r.source_file = src_name
        rejected_dicts.append(r.to_dict())
    report = write_report(source, campaign, cand_dicts, rejected=rejected_dicts)
    return {"source": source, "transcript": transcript, "speakers": speakers, "speaker_ctx": ctx,
            "candidates": cand_dicts, "rejected": rejected_dicts, "report": report}
