"""The finish: where the frame sits, what the words avoid, how shots join, what you hear.

The other agents argue about structure — how long the opening is, how many cuts,
whether the end returns to the start. This one does the pass a finishing editor
does last, once the cut is locked: reframe each shot around its subject, put the
words where the subject is not, choose each join from what is actually on either
side of it, and place effects on the cuts that can carry one.

Every decision here comes from `auteur.vision` — an actual reading of the frame,
not a default. That is the difference between "punch in slightly" and "punch in
toward the thing the eye already went to", and between a title centred because
titles are centred and a title placed in the half of the frame nobody is
looking at.

Four jobs, in the order they have to happen:

1. **Reframe.** A vertical crop of a horizontal frame throws away most of the
   picture. Which part it keeps should be decided by where the subject is.
2. **Overlays.** Text goes in the emptiest quadrant, then the platform's safe
   area gets the final say — a title clear of the subject and under TikTok's
   caption box is still unreadable.
3. **Transitions.** Two shots that look alike want a cut, because a dissolve
   between similar frames reads as a mistake. Two that do not can take one.
4. **Sound.** An effect on a join the picture already emphasises, and nowhere
   else. Sound design that fires on every cut is a metronome.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..edl import EditDecisionList, Motion, SoundCue, Transition
from ..insight import FitReport, Prediction
from ..vision import Reading, emptiest_quadrant
from .base import Proposal, Risk


def _much_wider_than(reading, edl) -> bool:
    """Is this source wide enough that a vertical crop would be vandalism?

    About the shape of what *arrived*, not the timeline: a 16:9 clip going into
    a 9:16 frame loses roughly two thirds of its width, and no arrangement of
    the crop avoids that. Turning it loses nothing at all.
    """
    if reading is None or reading.aspect <= 0:
        return False
    target_ratio = edl.width / max(edl.height, 1)
    # Wider than 1.3:1 going into anything taller than square.
    return reading.aspect > 1.3 and target_ratio < 1.0


@dataclass
class _Join:
    """One cut, and how different the two sides of it are."""

    index: int
    at: float
    luma_gap: float
    hue_gap: float
    focus_gap: float

    @property
    def is_hard(self) -> bool:
        """Do the two sides look different enough for the join to be felt?"""
        return self.luma_gap > 0.18 or self.hue_gap > 60.0 or self.focus_gap > 0.35


def _hue_distance(one: float, other: float) -> float:
    """Degrees apart on the colour wheel, the short way round."""
    gap = abs(one - other) % 360.0
    return min(gap, 360.0 - gap)


def _joins(edl: EditDecisionList, readings: dict[str, Reading]) -> list[_Join]:
    out: list[_Join] = []
    cursor = 0.0
    for index, shot in enumerate(edl.shots):
        if index > 0:
            before = readings.get(edl.shots[index - 1].clip_id)
            after = readings.get(shot.clip_id)
            if before is not None and after is not None:
                out.append(
                    _Join(
                        index=index,
                        at=cursor,
                        luma_gap=abs(before.luma - after.luma),
                        hue_gap=_hue_distance(before.hue, after.hue),
                        focus_gap=(
                            (before.focus[0] - after.focus[0]) ** 2
                            + (before.focus[1] - after.focus[1]) ** 2
                        )
                        ** 0.5,
                    )
                )
        cursor += shot.duration
    return out


#: Kept as a local name because this module reads better with it, but the
#: definition lives with the reading it takes — the overlay agent needs the
#: same answer, and two copies of "where is the subject not" would drift.
_emptiest_quadrant = emptiest_quadrant


class FinishingAgent:
    """Reframing, overlays, transitions and sound, from a reading of the frames.

    Needs `readings` — one per clip id, from `auteur.vision.read_asset`. Without
    them it proposes nothing at all rather than guessing, because every decision
    it makes is only as good as the reading behind it and a default-driven
    finishing pass is worse than none.
    """

    name = "finishing"
    objective = "craft"

    def __init__(self, readings: dict[str, Reading], *, spec=None):
        self.readings = readings or {}
        self.spec = spec

    def inspect(
        self, edl: EditDecisionList, prediction: Prediction, model: FitReport
    ) -> list[Proposal]:
        if not self.readings or not edl.shots:
            return []
        proposals: list[Proposal] = []
        readings = self.readings

        # -- 1. reframe around the subject --------------------------------
        wrong_anchor = [
            shot
            for shot in edl.shots
            if shot.clip_id in readings
            and readings[shot.clip_id].has_subject
            and (
                (shot.motion.anchor[0] - readings[shot.clip_id].focus[0]) ** 2
                + (shot.motion.anchor[1] - readings[shot.clip_id].focus[1]) ** 2
            )
            ** 0.5
            > 0.12
        ]
        if wrong_anchor:

            def reframe(target: EditDecisionList) -> None:
                for shot in target.shots:
                    reading = readings.get(shot.clip_id)
                    if reading is None or not reading.has_subject:
                        continue
                    # Move the move's anchor onto the subject. A punch-in
                    # toward the middle of a frame whose subject is off to one
                    # side pushes the subject out of shot.
                    shot.motion = Motion(
                        kind=shot.motion.kind if shot.motion.kind != "none" else "punch-in",
                        intensity=max(0.12, shot.motion.intensity),
                        anchor=reading.focus,
                    )
                    # A frame with real depth deserves the crop that keeps it.
                    if reading.depth_separation > 0.4:
                        shot.reframe = "subject"

            proposals.append(
                Proposal(
                    agent=self.name,
                    title=f"Reframe {len(wrong_anchor)} shot(s) onto the subject",
                    reason=(
                        "The camera move is anchored to the middle of the frame while the eye "
                        "goes somewhere else — on a vertical crop that pushes the subject out "
                        "of shot entirely."
                    ),
                    change=reframe,
                    objective=self.objective,
                    risk=Risk.MEDIUM,
                )
            )

        # -- 1b. or do not crop it at all ----------------------------------
        turnable = [
            shot
            for shot in edl.shots
            if shot.reframe != "turn" and _much_wider_than(readings.get(shot.clip_id), edl)
        ]
        if turnable:

            def turn(target: EditDecisionList) -> None:
                for shot in target.shots:
                    if _much_wider_than(readings.get(shot.clip_id), target):
                        shot.reframe = "turn"
                        # A turned frame already fills the height; a camera move
                        # on top of it would crop back into what was just saved.
                        shot.motion = Motion("none", 0.0, shot.motion.anchor)

            proposals.append(
                Proposal(
                    agent=self.name,
                    title=f"Stand {len(turnable)} wide shot(s) on end rather than cropping",
                    reason=(
                        "A vertical crop of a wide frame keeps about a third of it and throws "
                        "the rest away. Turned, the whole composition survives at full height "
                        "and the viewer rotates the phone — one wrist movement, and how several "
                        "of the reels this is measured against are delivered."
                    ),
                    change=turn,
                    objective=self.objective,
                    risk=Risk.MEDIUM,
                )
            )

        # -- 2. overlays out of the subject's way --------------------------
        clashing = []
        for cue in edl.texts:
            shot = _shot_under(edl, cue.start)
            reading = readings.get(shot.clip_id) if shot else None
            if reading is None or not reading.has_subject:
                continue
            gap = (
                (cue.anchor[0] - reading.focus[0]) ** 2 + (cue.anchor[1] - reading.focus[1]) ** 2
            ) ** 0.5
            if gap < 0.22:
                clashing.append(cue)

        if clashing:
            spec = self.spec

            def move_text(target: EditDecisionList) -> None:
                for cue in target.texts:
                    shot = _shot_under(target, cue.start)
                    reading = readings.get(shot.clip_id) if shot else None
                    if reading is None or not reading.has_subject:
                        continue
                    cue.anchor = _emptiest_quadrant(reading)
                    # The safe area still has the last word. A title clear of
                    # the subject and under the caption box is not readable.
                    if spec is not None:
                        cue.anchor = spec.safe.clamp(cue.anchor)

            proposals.append(
                Proposal(
                    agent=self.name,
                    title=f"Move {len(clashing)} title(s) off the subject",
                    reason=(
                        "Words are sitting on the exact spot the eye goes to. Moving them to "
                        "the opposite third means the viewer can read the title and see the "
                        "shot, rather than choosing."
                    ),
                    change=move_text,
                    objective=self.objective,
                    risk=Risk.LOW,
                )
            )

        # -- 3. transitions from what is on either side --------------------
        joins = _joins(edl, readings)
        soft_between_similar = [
            join
            for join in joins
            if not join.is_hard and not edl.shots[join.index].transition_in.is_cut
        ]
        if soft_between_similar:

            def fix_joins(target: EditDecisionList) -> None:
                for join in _joins(target, readings):
                    shot = target.shots[join.index]
                    if join.is_hard:
                        # A real change of place can carry a dissolve; a jump in
                        # brightness is the one thing worth softening.
                        if join.luma_gap > 0.30 and shot.transition_in.is_cut:
                            shot.transition_in = Transition(kind="dissolve", duration=0.22)
                    elif not shot.transition_in.is_cut:
                        # Two frames that already look alike do not need help
                        # blending — a dissolve between them reads as a fault.
                        shot.transition_in = Transition(kind="cut")

            proposals.append(
                Proposal(
                    agent=self.name,
                    title=f"Cut {len(soft_between_similar)} join(s) that are being dissolved",
                    reason=(
                        "A dissolve between two shots that already match in tone and colour "
                        "looks like a mistake rather than a transition. Save them for the "
                        "joins where the picture actually changes."
                    ),
                    change=fix_joins,
                    objective=self.objective,
                    risk=Risk.LOW,
                )
            )

        # -- 4. sound on the joins that can carry it -----------------------
        hard = [join for join in joins if join.is_hard]
        already = {round(cue.at, 2) for cue in edl.sfx}
        wanted = [join for join in hard if round(join.at, 2) not in already]
        if wanted:

            def add_sfx(target: EditDecisionList) -> None:
                for join in _joins(target, readings):
                    if not join.is_hard:
                        continue
                    # An impact on the join, and a riser into it only when the
                    # change is big enough to have been worth waiting for.
                    target.sfx.append(SoundCue(kind="impact", at=join.at, gain=0.45))
                    if join.luma_gap > 0.32:
                        target.sfx.append(
                            SoundCue(
                                kind="riser", at=max(0.0, join.at - 0.6), gain=0.3, duration=0.6
                            )
                        )

            proposals.append(
                Proposal(
                    agent=self.name,
                    title=f"Put an effect on {len(wanted)} join(s) the picture already marks",
                    reason=(
                        "These are the cuts where the frame genuinely changes — brightness, "
                        "colour or where the subject sits. Sound on those reads as design; "
                        "sound on every cut reads as a metronome."
                    ),
                    change=add_sfx,
                    objective=self.objective,
                    risk=Risk.MEDIUM,
                )
            )

        return proposals


def _shot_under(edl: EditDecisionList, when: float):
    """The shot playing at this point on the finished timeline."""
    for start, end, shot in edl.timeline():
        if start <= when < end:
            return shot
    return edl.shots[0] if edl.shots else None
