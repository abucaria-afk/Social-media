"""Building the crew, in one place, so every entry point gets the same one.

There are three ways into this program — the CLI, the studio's web API, and a
direct call — and for a while they did not agree on who was in the room. The
CLI assembled the eye, the finisher, the overlay agent and a reference-style
agent; the studio called `default_crew()` and got five. Same footage, same
prompt, fewer proposals, and no way for anyone to tell why.

That is a worse failure than a missing feature, because the studio's whole
purpose is to show a person what the agents want before three minutes are spent
acting on it. A studio that shows a shorter list than the CLI would have acted
on is showing the wrong list.

So the crew is assembled here and nowhere else. If an agent should exist, it
exists for everybody.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from ..edl import EditDecisionList
from .base import Crew, Gate

log = logging.getLogger("auteur.agents.assemble")


def read_the_footage(sources: Sequence[Path | str], *, ids: Sequence[str] | None = None) -> dict:
    """Read each asset the way a picture is read, keyed by clip id.

    A clip that will not read is skipped rather than guessed at: the agents that
    depend on a reading propose nothing for what they have not seen, which is
    the right answer and much better than a confident default.
    """
    from ..vision import read_asset

    readings: dict = {}
    for index, source in enumerate(sources):
        clip_id = ids[index] if ids is not None and index < len(ids) else f"C{index:02d}"
        try:
            readings[clip_id] = read_asset(source)
        except (ValueError, OSError) as exc:
            log.info("could not read %s: %s", source, exc)
    return readings


def readings_for(edl: EditDecisionList) -> dict:
    """Readings for a cut that already exists, taken from its own shots.

    The studio holds a finished EDL rather than a folder of rushes, so this is
    how the web path gets the same measured subjects the CLI path has. Each shot
    names its source, and duplicates are read once — a still used three times is
    the same picture all three times.
    """
    seen: dict[str, str] = {}
    sources: list[Path] = []
    ids: list[str] = []
    for shot in edl.shots:
        key = str(shot.source)
        if key in seen:
            continue
        seen[key] = shot.clip_id
        sources.append(shot.source)
        ids.append(shot.clip_id)

    readings = read_the_footage(sources, ids=ids)
    # Shots that reuse a source share its reading rather than going unread.
    for shot in edl.shots:
        first = seen.get(str(shot.source))
        if first is not None and first in readings:
            readings.setdefault(shot.clip_id, readings[first])
    return readings


def build_crew(
    model,
    *,
    gate: Gate,
    readings: dict | None = None,
    spec=None,
    style=None,
    stickers: list[Path] | None = None,
    scholar=None,
    ledger=None,
    max_rounds: int = 3,
) -> Crew:
    """The full crew, in the order they should speak.

    Order matters in one place only: a style measured from footage the user
    pointed at goes first, because a reference outranks a correlation drawn
    across a population. Everything after it is scored normally.
    """
    from . import default_crew
    from .finishing import FinishingAgent
    from .gaze import GazeAgent
    from .overlay import OverlayAgent
    from .preflight import StyleAgent

    agents = list(default_crew())

    if readings:
        # The curator's focal judgement reads the motion anchor when it has no
        # reading — an anchor this program set, so it would be scoring its own
        # input rather than the picture. Swap in the one that can see.
        agents = [GazeAgent(readings) if agent.name == "gaze" else agent for agent in agents]
        agents.append(FinishingAgent(readings, spec=spec))
        agents.append(OverlayAgent(readings, spec=spec, stickers=list(stickers or [])))

    # The Scholar reviews the cut with whatever it has actually studied. It
    # returns nothing until it has studied enough to be worth a round, so an
    # unused Scholar costs one attribute read and stays silent — but a Scholar
    # that has been studying finally has a path to a frame, which it did not
    # have at all: nothing in the program constructed one.
    if scholar is not False:
        try:
            from ..scholar.agent import ScholarAgent

            student = ScholarAgent() if scholar is None else ScholarAgent(scholar)
            if student.studied:
                agents.append(student)
        except Exception as exc:  # noqa: BLE001 - the Scholar is optional, the edit is not
            log.info("the Scholar is not available: %s", exc)

    if style is not None and not style.is_empty:
        agents.insert(0, StyleAgent(style))

    if ledger is None:
        from .ledger import Ledger

        ledger = Ledger()

    return Crew(agents, model, gate=gate, max_rounds=max_rounds, ledger=ledger)


def crew_summary(crew: Crew) -> str:
    """What to tell someone about who is about to work on their film."""
    return ", ".join(agent.name.replace("_", " ") for agent in crew.agents)
