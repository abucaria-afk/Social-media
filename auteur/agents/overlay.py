"""Marks on the picture: what to draw, where, and — mostly — whether to at all.

Overlays are the cheapest retention device there is and the easiest to overdo.
A ring around the subject buys attention in the third of a second before the
subject earns it. A bar filling along the bottom tells a viewer the end is
reachable, which is the whole argument against swiping. A sticker in dead space
gives the eye somewhere to rest between cuts.

The same devices, applied without a reason, are the visual signature of content
nobody finished watching. So every proposal here has to point at something
measured: the ring goes where the reading says the eye already went, the arrow
points from empty space at a subject that is genuinely off-centre, and nothing
at all is proposed for a frame whose subject the reading could not find.

Worth stating plainly, because it cuts against the instinct to decorate: the
reference reels this project measures its style against use almost none of this.
Two of the three carry a song credit and nothing else. Overlays earn their place
one at a time or not at all, which is why the defaults here are restrained and
why the sticker pass will not run unless somebody supplies stickers.

**Why every proposal here is binding.** Nothing in any performance export this
project has been given records whether a post carried on-screen graphics — no
overlay, sticker, annotation or marker column exists in any of them. So the
scoring model cannot have an opinion about a ring or a bar, and it does not
have one: adding a graphic moves the prediction by exactly zero, every time.
Run through the crew's usual "does this improve the score?" test, every overlay
would be dropped as *no predicted gain*, which reads as a judgement and is
actually silence. Binding says the honest thing instead — the model abstains,
so the decision goes to the person, which is what the gate is for. Fitting a
coefficient for overlays against a corpus that never measured them would not be
data-driven, it would be making one up.
"""

from __future__ import annotations

from pathlib import Path

from ..edl import EditDecisionList, GraphicCue
from ..insight import FitReport, Prediction
from ..vision import Reading, emptiest_quadrant
from .base import Proposal, Risk

#: Below this the reading did not find a subject, it found a texture. Pointing
#: at a texture is worse than pointing at nothing.
HAS_SUBJECT = 0.18

#: How far off-centre a subject has to be before an arrow is telling the viewer
#: something they would not have worked out unaided.
OFF_CENTRE = 0.16


def _shot_windows(edl: EditDecisionList) -> list[tuple[float, float, str]]:
    """(start, end, clip id) for every shot on the finished timeline."""
    return [(start, end, shot.clip_id) for start, end, shot in edl.timeline()]


def _clear_of(cue: GraphicCue, existing: list[GraphicCue], gap: float = 0.25) -> bool:
    """Would this graphic share the screen with one that is already there?

    Two marks at once is a busy frame; three is a slideshow template. Time
    overlap is what matters, not position, because the eye only has one place
    to be.
    """
    return all(cue.start >= other.end + gap or cue.end + gap <= other.start for other in existing)


class OverlayAgent:
    """Draws on the picture, from a reading of it.

    Needs `readings` — one per clip id, from `auteur.vision.read_asset`. Without
    them it proposes nothing, for the same reason the finishing agent does not:
    a mark placed from a default is a mark placed on top of the subject about
    half the time, and a ring around nothing is worse than no ring.
    """

    name = "overlay"
    objective = "retention"

    def __init__(
        self,
        readings: dict[str, Reading],
        *,
        spec=None,
        stickers: list[Path] | None = None,
    ):
        self.readings = readings or {}
        self.spec = spec
        self.stickers = list(stickers or [])

    def inspect(
        self, edl: EditDecisionList, prediction: Prediction, model: FitReport
    ) -> list[Proposal]:
        if not self.readings or not edl.shots:
            return []

        proposals: list[Proposal] = []
        windows = _shot_windows(edl)
        runtime = edl.duration
        existing = list(edl.graphics)

        # --- 1. The retention bar ----------------------------------------
        # Only worth it once the film is long enough for "how much is left?"
        # to be a question the viewer is actually asking.
        if runtime >= 8.0 and not any(c.kind == "progress" for c in existing):

            def add_bar(target: EditDecisionList, seconds: float = runtime) -> None:
                target.graphics.append(
                    GraphicCue(
                        kind="progress",
                        start=0.0,
                        duration=seconds,
                        anchor=(0.5, 0.972),
                        move="none",
                        opacity=0.85,
                        note="how much is left",
                    )
                )

            proposals.append(
                Proposal(
                    agent=self.name,
                    title="Run a progress bar along the bottom",
                    reason=(
                        f"At {runtime:.0f}s a viewer deciding whether to stay has no way to "
                        "know how much they are committing to. A bar answers that in "
                        "peripheral vision, without taking a pixel off the subject."
                    ),
                    change=add_bar,
                    objective=self.objective,
                    binding=True,
                    risk=Risk.LOW,
                )
            )

        # --- 2. Bracket the hook -----------------------------------------
        opening = windows[0]
        first = self.readings.get(opening[2])
        if first is not None and first.focus_strength >= HAS_SUBJECT:
            hold = min(1.1, max(0.4, opening[1] - opening[0]))

            def bracket(target: EditDecisionList, where=first.focus, hold=hold) -> None:
                target.graphics.append(
                    GraphicCue(
                        kind="bracket",
                        start=0.12,
                        duration=hold,
                        anchor=where,
                        size=1.15,
                        move="pop",
                        opacity=0.9,
                        note="hook: says where to look",
                    )
                )

            candidate = GraphicCue(kind="bracket", start=0.12, duration=hold)
            if _clear_of(candidate, existing):
                proposals.append(
                    Proposal(
                        agent=self.name,
                        title="Bracket the subject in the opening second",
                        reason=(
                            "The opening frame has a subject at "
                            f"({first.focus[0]:.2f}, {first.focus[1]:.2f}) and roughly a third "
                            "of a second to be found. Corner brackets resolve that before the "
                            "picture has to, which is the whole of the three-second watch rate."
                        ),
                        change=bracket,
                        objective=self.objective,
                        binding=True,
                        risk=Risk.LOW,
                    )
                )

        # --- 3. Ring the strongest subject in the body -------------------
        body = [
            (start, end, clip)
            for start, end, clip in windows[1:]
            if (self.readings.get(clip) or Reading()).focus_strength >= HAS_SUBJECT
            and end - start >= 0.9
        ]
        if body:
            start, end, clip = max(
                body, key=lambda w: self.readings[w[2]].focus_strength  # strongest subject
            )
            reading = self.readings[clip]
            ring = GraphicCue(
                kind="circle",
                start=round(start + 0.15, 3),
                duration=round(min(1.3, end - start - 0.15), 3),
                anchor=reading.focus,
                move="draw",
                opacity=0.9,
                note="the eye already goes here",
            )
            if ring.duration > 0.4 and _clear_of(ring, existing):

                def circle(target: EditDecisionList, cue: GraphicCue = ring) -> None:
                    target.graphics.append(cue)

                proposals.append(
                    Proposal(
                        agent=self.name,
                        title=f"Ring the subject in shot {windows.index((start, end, clip)) + 1}",
                        reason=(
                            f"This is the clearest subject in the film (focus strength "
                            f"{reading.focus_strength:.2f}). A ring drawn around it makes the "
                            "shot legible at a glance, which is what a viewer scrolling gives "
                            "it. Drawn rather than snapped on, so it reads as somebody marking "
                            "the frame rather than a template."
                        ),
                        change=circle,
                        objective=self.objective,
                        binding=True,
                        risk=Risk.LOW,
                    )
                )

        # --- 4. Point at a subject that is genuinely hiding ---------------
        for start, end, clip in windows:
            reading = self.readings.get(clip)
            if reading is None or reading.focus_strength < HAS_SUBJECT:
                continue
            offset = max(abs(reading.focus[0] - 0.5), abs(reading.focus[1] - 0.5))
            if offset < OFF_CENTRE or end - start < 1.0:
                continue
            tail = emptiest_quadrant(reading)
            arrow = GraphicCue(
                kind="arrow",
                start=round(start + 0.2, 3),
                duration=round(min(1.0, end - start - 0.2), 3),
                anchor=tail,
                toward=reading.focus,
                move="draw",
                opacity=0.95,
                note="the subject is not where the eye starts",
            )
            if arrow.duration > 0.4 and _clear_of(arrow, existing):

                def point(target: EditDecisionList, cue: GraphicCue = arrow) -> None:
                    target.graphics.append(cue)

                proposals.append(
                    Proposal(
                        agent=self.name,
                        title="Point at the off-centre subject",
                        reason=(
                            f"The subject sits at ({reading.focus[0]:.2f}, "
                            f"{reading.focus[1]:.2f}), far enough off centre that the eye lands "
                            "somewhere else first. An arrow from the empty side costs a third "
                            "of a second and saves a whole shot."
                        ),
                        change=point,
                        objective=self.objective,
                        binding=True,
                        risk=Risk.MEDIUM,
                    )
                )
                break  # one arrow in a film. Two is a diagram.

        # --- 5. The user's own stickers ----------------------------------
        proposals.extend(self._sticker_proposals(windows, existing))
        return proposals

    def _sticker_proposals(
        self, windows: list[tuple[float, float, str]], existing: list[GraphicCue]
    ) -> list[Proposal]:
        """Place supplied PNGs in space the subject is not using.

        Deliberately conservative: one sticker per shot at most, never on the
        opening shot (the hook has one job), and only on shots with a subject
        clear enough that "away from it" means something.
        """
        # The crew runs several rounds, and a proposal that does not look at what
        # is already on the timeline gets applied again in each one. Every other
        # pass here is guarded; without this one the stickers doubled every round.
        if not self.stickers or any(cue.kind == "sticker" for cue in existing):
            return []

        placements: list[GraphicCue] = []
        for index, (start, end, clip) in enumerate(windows[1:], start=1):
            if len(placements) >= len(self.stickers) or end - start < 0.8:
                continue
            reading = self.readings.get(clip)
            if reading is None or reading.focus_strength < HAS_SUBJECT:
                continue
            spot = emptiest_quadrant(reading)
            if self.spec is not None:
                spot = self.spec.safe.clamp(spot)
            cue = GraphicCue(
                kind="sticker",
                start=round(start + 0.12, 3),
                duration=round(min(2.0, end - start - 0.12), 3),
                anchor=spot,
                move="wiggle",
                source=self.stickers[len(placements)],
                note=f"your sticker, clear of the subject in shot {index + 1}",
            )
            if cue.duration > 0.3 and _clear_of(cue, existing + placements, gap=0.0):
                placements.append(cue)

        if not placements:
            return []

        def place(target: EditDecisionList, cues: list[GraphicCue] = placements) -> None:
            target.graphics.extend(cues)

        names = ", ".join(sorted({c.source.name for c in placements if c.source})[:3])
        return [
            Proposal(
                agent=self.name,
                title=f"Place {len(placements)} of your stickers",
                reason=(
                    f"Using {names}. Each one goes in the quadrant the reading says the "
                    "subject is not in, so it decorates the dead space rather than the "
                    "picture, and each carries a slow rotation — a sticker that sits "
                    "perfectly still reads as a watermark."
                ),
                change=place,
                objective=self.objective,
                binding=True,
                risk=Risk.LOW,
            )
        ]
