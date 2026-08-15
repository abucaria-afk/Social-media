"""Autonomous editing agents, and the gate that keeps a person in the loop.

    from auteur.agents import Crew, Gate, Mode, default_crew
    from auteur.insight import corpus, fit

    model = fit(corpus(["exports/short_form_video.csv"]))
    crew = Crew(default_crew(), model, gate=Gate(Mode.SUPERVISED, on_ask=ask))
    result = crew.run(edl)

Three agents, one objective each — hook, share, loop. They propose, the crew
scores every proposal against the model and keeps only what improves the
overall prediction, and `Gate` decides which of those need a human first.

Autonomy stops short of publishing, in every mode. See `base.Gate.may_publish`.
"""

from __future__ import annotations

from .base import (
    Agent,
    Change,
    Crew,
    CrewResult,
    Decision,
    Gate,
    Mode,
    Proposal,
    Risk,
    Round,
)
from .editors import HookAgent, LoopAgent, ShareAgent, default_crew
from .finalcheck import FinalCheckAgent
from .finishing import FinishingAgent
from .gaze import GazeAgent
from .overlay import OverlayAgent
from .preflight import CHECKABLE, Finding, StyleAgent, check_render, preflight, unknowable

__all__ = [
    "Agent",
    "Change",
    "Crew",
    "CrewResult",
    "Decision",
    "FinalCheckAgent",
    "Gate",
    "GazeAgent",
    "HookAgent",
    "LoopAgent",
    "Mode",
    "Proposal",
    "Risk",
    "Round",
    "ShareAgent",
    "CHECKABLE",
    "Finding",
    "FinishingAgent",
    "OverlayAgent",
    "StyleAgent",
    "check_render",
    "default_crew",
    "preflight",
    "unknowable",
]
