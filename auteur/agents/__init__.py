"""Autonomous editing agents, and the gate that keeps a person in the loop.

    from auteur.agents import Crew, Gate, Mode, default_crew
    from auteur.insight import corpus, fit

    model = fit(corpus(["exports/short_form_video.csv"]))
    crew = Crew(default_crew(), model, gate=Gate(Mode.SUPERVISED, on_ask=ask))
    result = crew.run(edl)

Three agents, one objective each — hook, share, loop. They propose, the crew
scores every proposal against the model and keeps only what improves the
overall prediction, and `Gate` decides which of those need a human first.

Autonomy stops short of publishing, in every mode — and it is worth being exact
about *why*, because this line used to imply something that was not true.

`base.Gate.may_publish` is correct: it consults no mode, refuses when there is
nobody to ask, and a test exercises every mode including `AUTONOMOUS`. It is
also **called by nothing**. The reason nothing here posts is not that the gate
stops it; it is that no module in `auteur/agents/`, `auteur/publish/` or
`auteur/workflows/` can reach a network at all. `workflows/schedule.py` says so
in its own first paragraph — it schedules, it holds no credential, and
`export_csv` hands the queue to whatever does the posting.

That distinction matters to exactly one reader: whoever wires posting up. They
will find a gate, a docstring pointing at it, and a green test, and reasonably
conclude it is already in the path. It is not — it is a loaded safety that is
not connected to a trigger, and connecting it is their job.
`test_the_publish_gate_is_either_in_the_path_or_has_no_path_to_be_in` is what
tells them so: while nothing can publish it asserts the gate has no callers,
and the moment a publisher appears it demands the gate be in that module.
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
