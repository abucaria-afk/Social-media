"""auteur — an autonomous cinematic editor.

Point it at a pile of unsorted clips, give it a sentence of direction, and it
returns a finished, graded, beat-cut, sound-designed short film.

The pipeline mirrors how a cutting room actually works:

    ingest    -> what footage do we have?
    analyse   -> watch every frame; log motion, focus, exposure, sound
    direct    -> read the brief, choose the shots, write the edit decision list
    craft     -> apply grammar: rhythm, ramps, reframing, grade, sound, titles
    render    -> conform the EDL to pixels
    critique  -> watch the result, find what is wrong, cut it again

Public entry point is :func:`auteur.agent.direct`, or the ``auteur`` CLI.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "direct",
    "Brief",
    "EditDecisionList",
]


def __getattr__(name: str):  # pragma: no cover - thin lazy import shim
    # Keep import of the package cheap; numpy/PIL only load when actually used.
    if name == "direct":
        from .agent import direct

        return direct
    if name == "Brief":
        from .director.brief import Brief

        return Brief
    if name == "EditDecisionList":
        from .edl import EditDecisionList

        return EditDecisionList
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
