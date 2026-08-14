"""Choosing a director.

The model directs when it can be reached; the algorithm directs when it cannot.
Either way the result goes through the same repair and the same film-grammar
passes, so the rest of the system never has to know which one wrote the edit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..analysis.audio import AudioAnalysis
from ..analysis.dossier import ClipDossier
from ..config import Settings
from ..craft import grammar
from ..edl import EditDecisionList
from ..ingest import MediaAsset
from . import heuristic, llm
from .brief import Brief

log = logging.getLogger("auteur.director")


@dataclass
class Direction:
    """An edit, and the story of how it came to exist."""

    edl: EditDecisionList
    directed_by: str  # "model" | "heuristic"
    grammar_report: dict[str, int]
    fallback_reason: str = ""


def direct(
    brief: Brief,
    dossiers: list[ClipDossier],
    settings: Settings,
    *,
    music: MediaAsset | None = None,
    music_analysis: AudioAnalysis | None = None,
) -> Direction:
    """Produce a polished, renderable EDL."""
    target = brief.duration or settings.target_duration
    offset = heuristic._music_offset(music_analysis, target)

    edl: EditDecisionList | None = None
    directed_by = "heuristic"
    reason = ""

    if llm.available(settings):
        try:
            edl = llm.direct(
                brief, dossiers, settings,
                music_path=music.path if music else None,
                music_analysis=music_analysis,
                music_offset=offset,
            )
            directed_by = "model"
        except llm.DirectorUnavailable as exc:
            reason = str(exc)
            log.info("falling back to the heuristic director: %s", reason)
        except Exception as exc:  # noqa: BLE001 - a bad edit must never lose the film
            reason = f"unexpected failure in the model director: {exc}"
            log.warning("%s", reason)
    else:
        reason = "no model configured"

    if edl is None:
        edl = heuristic.cut(brief, dossiers, settings, music=music, music_analysis=music_analysis)

    report = grammar.polish(
        edl,
        audio=music_analysis,
        music_offset=edl.music.offset if edl.music.source else offset,
        target_duration=target,
        beat_sync=brief.beat_sync,
    )
    edl.repair({dossier.clip_id: dossier for dossier in dossiers}, target_duration=target)

    log.info(
        "%s directed %d shots over %.2fs (grammar: %s)",
        directed_by, len(edl.shots), edl.duration,
        ", ".join(f"{k}={v}" for k, v in report.items() if v),
    )
    return Direction(edl=edl, directed_by=directed_by, grammar_report=report, fallback_reason=reason)
