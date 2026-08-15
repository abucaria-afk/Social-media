"""Learning a style by measuring footage that already has it.

"Make it more like this" is the most useful direction anybody gives, and the
hardest to act on, because the thing being pointed at is a feeling. This turns
it into numbers: point at videos you like, and it measures how fast they cut,
how dark they are, how much the camera moves, and how long it waits before the
first cut. Those four are enough to move an edit a long way toward a reference,
and they are all things the director can actually be told.

**This deliberately outranks the data.** The performance corpus says nine or ten
cuts per ten seconds; a reference reel cutting at three says three. When they
disagree the reference wins, because "I want it to look like this" is a
statement about the work and the corpus is a statement about a population. A
tool that overrules the person holding the camera on the strength of a
correlation is not being data-driven, it is being rude.

What it cannot measure: what the footage is *of*, whether the joins are witty,
or why the reference works. It measures rhythm and exposure. That is a real
part of a style and it is not the whole of one.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Sequence

log = logging.getLogger("auteur.insight.reference")


@dataclass
class StyleTarget:
    """The measurable part of "more like this"."""

    #: Cuts per ten seconds. The single most audible property of an edit.
    cuts_per_10s: float = 0.0
    #: Median screen time per shot, in seconds.
    shot_seconds: float = 0.0
    #: The shortest shot anybody used. A floor on how quick a flurry may get.
    shortest_shot: float = 0.0
    #: When the first cut lands, in seconds.
    first_cut: float = 0.0
    #: Mean luma, 0..1. Night footage sits near 0.1; a lit interior near 0.5.
    luma: float = 0.0
    #: Mean local contrast, 0..1.
    contrast: float = 0.0
    #: Mean inter-frame motion. Low is static or a slow drift; high is handheld.
    motion: float = 0.0
    #: Runtime of the references, for reference.
    seconds: float = 0.0
    #: How many clips this was measured from. One is an anecdote.
    sources: int = 0
    names: tuple[str, ...] = ()
    #: Per-source spread, so a caller can see whether the references agree.
    disagreement: dict[str, float] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return self.sources == 0

    @property
    def pace_words(self) -> str:
        """The prompt vocabulary that lands closest to this cutting speed.

        The brief parser understands words, not numbers, so a measured pace has
        to be translated back into something it can read.
        """
        if self.cuts_per_10s >= 8.0:
            return "frenetic"
        if self.cuts_per_10s >= 6.0:
            return "fast"
        if self.cuts_per_10s >= 4.0:
            return "upbeat"
        if self.cuts_per_10s >= 2.5:
            return "steady"
        if self.cuts_per_10s >= 1.5:
            return "slow"
        return "meditative"

    @property
    def look_words(self) -> str:
        """The grade that matches this exposure."""
        if self.luma < 0.18:
            return "moody"
        if self.luma < 0.30:
            return "noir" if self.contrast < 0.13 else "moody"
        if self.luma > 0.55:
            return "warm"
        return "neutral"

    @property
    def is_agreed(self) -> bool:
        """Do the references actually describe one style?

        Spread above roughly a third of the mean means they do not, and an
        average of two different styles is a third style nobody asked for.
        """
        pace = self.disagreement.get("cuts_per_10s", 0.0)
        return not (self.cuts_per_10s and pace / max(self.cuts_per_10s, 1e-6) > 0.35)

    def describe(self) -> str:
        if self.is_empty:
            return "no reference footage"
        lines = [
            f"measured from {self.sources} clip(s), {self.seconds:.0f}s in total",
            f"    pace       {self.cuts_per_10s:.1f} cuts / 10s  ({self.pace_words})",
            f"    shots      {self.shot_seconds:.2f}s typical, {self.shortest_shot:.2f}s shortest",
            f"    first cut  {self.first_cut:.2f}s",
            f"    exposure   luma {self.luma:.2f}, contrast {self.contrast:.2f}"
            f"  ({self.look_words})",
            f"    motion     {self.motion:.3f}",
        ]
        if not self.is_agreed:
            lines.append(
                "    ! these references do not agree on pace — the average is a style "
                "none of them has"
            )
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "cuts_per_10s": round(self.cuts_per_10s, 3),
            "shot_seconds": round(self.shot_seconds, 3),
            "shortest_shot": round(self.shortest_shot, 3),
            "first_cut": round(self.first_cut, 3),
            "luma": round(self.luma, 4),
            "contrast": round(self.contrast, 4),
            "motion": round(self.motion, 4),
            "seconds": round(self.seconds, 2),
            "sources": self.sources,
            "names": list(self.names),
            "pace_words": self.pace_words,
            "look_words": self.look_words,
            "agreed": self.is_agreed,
        }

    def prompt_fragment(self) -> str:
        """Words to fold into a brief, so the director cuts toward this."""
        if self.is_empty:
            return ""
        return f"{self.pace_words}, {self.look_words}"


def _median(values: Sequence[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _spread(values: Sequence[float]) -> float:
    return float(statistics.pstdev(values)) if len(values) > 1 else 0.0


def measure(paths: Sequence[str | Path], *, analysis_fps: float = 8.0) -> StyleTarget:
    """Watch some footage and describe how it was cut.

    Uses the project's own analysis rather than anything special: the same
    shot-boundary detection that finds the cuts already inside your rushes
    finds the cuts in a reference reel, because they are the same problem.
    """
    from ..analysis.dossier import build_dossier
    from ..ingest import probe_asset

    import numpy as np

    per_clip: list[dict] = []
    names: list[str] = []
    for path in paths:
        file = Path(path)
        asset = probe_asset(file)
        if asset is None or asset.kind != "video" or asset.duration <= 0:
            log.info("skipping %s as reference — not readable video", file.name)
            continue
        dossier = build_dossier(file.stem[:8], asset, analysis_fps=analysis_fps, analysis_width=160)
        video = dossier.video
        cuts = [float(c) for c in video.shot_boundaries]
        duration = asset.duration
        lengths = list(np.diff([0.0, *cuts, duration])) if cuts else [duration]

        per_clip.append(
            {
                "cuts_per_10s": len(cuts) / duration * 10.0,
                "shot_seconds": _median([float(x) for x in lengths]),
                "shortest_shot": float(min(lengths)),
                # No cut at all means the whole clip is the opening shot.
                "first_cut": cuts[0] if cuts else duration,
                "luma": float(np.mean(video.luma)) if len(video.luma) else 0.0,
                "contrast": float(np.mean(video.contrast)) if len(video.contrast) else 0.0,
                "motion": float(np.mean(video.motion)) if len(video.motion) else 0.0,
                "seconds": duration,
            }
        )
        names.append(file.name)

    if not per_clip:
        return StyleTarget()

    def across(key: str) -> float:
        return _median([clip[key] for clip in per_clip])

    return StyleTarget(
        cuts_per_10s=across("cuts_per_10s"),
        shot_seconds=across("shot_seconds"),
        # The floor is the fastest anybody actually went, not the median of the
        # floors — a style's quickest flurry is a real part of it.
        shortest_shot=min(clip["shortest_shot"] for clip in per_clip),
        first_cut=across("first_cut"),
        luma=across("luma"),
        contrast=across("contrast"),
        motion=across("motion"),
        seconds=sum(clip["seconds"] for clip in per_clip),
        sources=len(per_clip),
        names=tuple(names),
        disagreement={
            key: _spread([clip[key] for clip in per_clip])
            for key in ("cuts_per_10s", "shot_seconds", "luma", "motion")
        },
    )
