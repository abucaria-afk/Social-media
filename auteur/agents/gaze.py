"""The Gaze agent — a holistic visual curator that sees each frame whole.

A trained eye at the MET or the Louvre does not read a painting pixel by pixel.
It registers the composition instantly: where the weight falls, where the light
pulls you, what the dominant palette is doing to the mood, whether the depth
layers resolve or compete, and whether the frame has a focal anchor at all.

This agent does that to every shot on the timeline. It works from the analysis
numbers already measured (luma, contrast, sharpness, edges, subject position,
colour, motion) but reads them the way a curator reads a gallery wall — as
*relationships* between shots, not as absolute values.

Its objective is visual coherence — but coherence is not sameness, and the
first version of this agent could not tell the difference.

Every one of its five proposals reduced variance: match the exposure, unify
the temperature, even out the contrast, pull every anchor to a power point,
soften a hard cut. A film in which every shot is graded identically, framed
identically and moves identically was therefore its *perfect score*. The
agent whose entire job is taste was the one enforcing the monotony, and it
could not have reported the problem because the problem was its objective
function.

So it now measures both directions. A wall of clashing exposures is
incoherent; so is a wall of forty identical frames, and a curator would walk
past the second one faster. The homogenisers below fire only when the spread
is genuinely broken, and a separate set of proposals fires when the timeline
has collapsed into one repeated decision — which is the far commoner failure
for a program, because sameness is what a program does by default.
"""

from __future__ import annotations

import math

from ..edl import EditDecisionList, Motion, Transition
from ..insight import FitReport, Prediction
from .base import Proposal, Risk


def _exposure_balance(edl: EditDecisionList) -> float:
    """How far the shots' exposures drift from each other, 0 (matched) to 1."""
    if len(edl.shots) < 2:
        return 0.0
    # Use the look corrections already on the timeline as a proxy: big
    # corrections mean the grader found big gaps.
    exposures = [shot.look.exposure for shot in edl.shots]
    spread = max(exposures) - min(exposures)
    return min(spread / 1.5, 1.0)


def _palette_drift(edl: EditDecisionList) -> float:
    """Temperature spread across the cut, 0 (uniform) to 1 (all over)."""
    if len(edl.shots) < 2:
        return 0.0
    temps = [shot.look.temperature for shot in edl.shots]
    return min((max(temps) - min(temps)) / 1.5, 1.0)


def _focal_weight(edl: EditDecisionList, readings=None) -> list[float]:
    """Per-shot focal strength, 0–1.

    With `readings` from `auteur.vision`, "where the eye goes" is *measured* off
    the frame. Without them it falls back to the shot's motion anchor — which is
    a real limitation worth naming, because the anchor is whatever the director
    set, so scoring it is scoring this program's own input rather than the
    picture. The fallback can tell you an anchor is nowhere near a power point;
    it cannot tell you whether the subject is.
    """
    weights: list[float] = []
    for shot in edl.shots:
        reading = (readings or {}).get(shot.clip_id)
        cx, cy = reading.focus if reading is not None else shot.motion.anchor
        # Distance of the anchor from the power points (rule-of-thirds
        # intersections). Closer to a power point = stronger focal pull.
        thirds = [(1 / 3, 1 / 3), (2 / 3, 1 / 3), (1 / 3, 2 / 3), (2 / 3, 2 / 3)]
        best = min(math.hypot(cx - tx, cy - ty) for tx, ty in thirds)
        # Normalise: 0 at a power point, 1 at the farthest corner.
        strength = max(0.0, 1.0 - best / 0.47)
        weights.append(strength)
    return weights


def _variety(values: list) -> float:
    """How spread out a set of choices is, 0 (all identical) to 1 (all different).

    Deliberately not entropy. What matters here is not how evenly the choices
    are distributed but how much of the timeline is one repeated decision, and
    the honest statement of that is "how many distinct choices, against how
    many chances there were to make one".
    """
    if not values:
        return 0.0
    if len(values) == 1:
        return 0.0
    return (len(set(values)) - 1) / (len(values) - 1)


def _longest_run(values: list) -> int:
    """The longest stretch of the same choice repeated back to back."""
    longest = run = 1
    for i in range(1, len(values)):
        run = run + 1 if values[i] == values[i - 1] else 1
        longest = max(longest, run)
    return longest


def _metronome(edl: EditDecisionList) -> float:
    """How close the cut is to a fixed interval, 0 (varied) to 1 (a metronome).

    A reel wants a *median* shot length, not a constant one. Every shot the
    same length is the difference between a rhythm and a click track, and it
    is audible to a viewer who could not tell you why.
    """
    lengths = [round(shot.duration, 3) for shot in edl.shots]
    if len(lengths) < 3:
        return 0.0
    commonest = max(lengths.count(value) for value in set(lengths))
    return commonest / len(lengths)


class GazeAgent:
    """Owns visual coherence — the curator's eye across the whole cut.

    Does not own a single platform metric. Instead it looks at the *film* the
    way a gallerist looks at a wall: focal weight, colour continuity, exposure
    matching, and whether the composition flows from shot to shot or fights
    itself. The crew scores the overall prediction; this agent provides the
    visual taste the other three are too busy to have.
    """

    name = "gaze"
    objective = "visual_coherence"

    def __init__(self, readings=None):
        # Optional so the existing `GazeAgent()` call site keeps working; when
        # supplied, every judgement below is made about the frame rather than
        # about the timeline's own metadata.
        self.readings = readings or {}

    def inspect(
        self, edl: EditDecisionList, prediction: Prediction, model: FitReport
    ) -> list[Proposal]:
        if len(edl.shots) < 2:
            return []

        proposals: list[Proposal] = []

        # What the timeline actually consists of. Read once, because half the
        # judgements below depend on whether this cut is too varied or not
        # varied enough, and answering that question twice is how an agent
        # ends up proposing both at once.
        joins = [shot.transition_in.kind for shot in edl.shots[1:]]
        moves = [shot.motion.kind for shot in edl.shots]
        join_variety = _variety(joins)
        move_variety = _variety(moves)
        # A timeline is "collapsed" when it has stopped making decisions: one
        # join, one move, one shot length. Every homogenising proposal below
        # is suppressed in that state — pulling the spread in further is the
        # exact wrong move, and it is what this agent used to do.
        collapsed = (
            (join_variety < 0.12 or not joins) and move_variety < 0.12 and _metronome(edl) > 0.7
        )

        # --- 0. Monotony -------------------------------------------------
        #  The failure this agent was blind to, and the commoner one: a film
        #  that is perfectly consistent because it only ever made one choice.

        if len(joins) >= 6 and join_variety < 0.18:
            worst = max(set(joins), key=joins.count)

            def vary_joins(target: EditDecisionList, repeated: str = worst) -> None:
                """Give a fifth of the joins somewhere else to be.

                Every fifth one, not a random selection: a loud transition
                wants to land on a beat, and scattering them evenly is a
                closer approximation to that than scattering them randomly.
                Deliberately not *all* of them — most cuts in a good reel are
                hard cuts, and a film that transitions every join is mush.
                """
                relief = ["whip-left", "light-leak", "glitch", "zoom-blur", "dissolve"]
                picked = 0
                for i, shot in enumerate(target.shots[1:], 1):
                    if shot.transition_in.kind != repeated or i % 5:
                        continue
                    kind = relief[picked % len(relief)]
                    shot.transition_in = Transition(
                        kind=kind, duration=0.0 if kind == "cut" else 0.22
                    )
                    picked += 1

            proposals.append(
                Proposal(
                    agent=self.name,
                    title="Give the cut more than one kind of join",
                    reason=(
                        f"{joins.count(worst)} of {len(joins)} joins are "
                        f"'{worst}', and the longest unbroken run is "
                        f"{_longest_run(joins)}. One join repeated is not a style, "
                        "it is the absence of a decision — the viewer stops reading "
                        "the edit and starts waiting it out."
                    ),
                    change=vary_joins,
                    objective=self.objective,
                    risk=Risk.MEDIUM,
                )
            )

        if len(moves) >= 6 and move_variety < 0.18:
            worst_move = max(set(moves), key=moves.count)

            def vary_moves(target: EditDecisionList, repeated: str = worst_move) -> None:
                """Break the uniform move, and let some shots hold still.

                `none` is first in the rotation and it matters most. A film
                where every frame drifts has no cuts in it, only dissolves
                between wobbles — the measured reference reels hold nearly
                dead still and put their energy in the join.
                """
                relief = ["none", "punch-in", "none", "pull-out", "drift-left"]
                picked = 0
                for i, shot in enumerate(target.shots):
                    if shot.motion.kind != repeated or i % 3:
                        continue
                    shot.motion = Motion(
                        kind=relief[picked % len(relief)],
                        intensity=shot.motion.intensity,
                        anchor=shot.motion.anchor,
                    )
                    picked += 1

            proposals.append(
                Proposal(
                    agent=self.name,
                    title="Stop every shot moving the same way",
                    reason=(
                        f"{moves.count(worst_move)} of {len(moves)} shots use "
                        f"'{worst_move}'. Constant low-grade movement on every frame "
                        "reads as a slideshow with a wobble on it, and it costs every "
                        "cut its edge — a cut only lands against something still."
                    ),
                    change=vary_moves,
                    objective=self.objective,
                    risk=Risk.MEDIUM,
                )
            )

        beat = _metronome(edl)
        if len(edl.shots) >= 8 and beat > 0.75:

            def breathe(target: EditDecisionList) -> None:
                """Three on the beat and one held, rather than a click track.

                The median stays where it was, so the cadence the director
                asked for survives; what changes is that it is now a median
                rather than a constant.
                """
                for i, shot in enumerate(target.shots):
                    if i % 4 == 3:
                        shot.duration = round(shot.duration * 1.6, 3)
                    elif i % 4 == 1:
                        shot.duration = round(max(0.1, shot.duration * 0.7), 3)

            proposals.append(
                Proposal(
                    agent=self.name,
                    title="Let the rhythm breathe",
                    reason=(
                        f"{beat:.0%} of the shots are exactly the same length. That is "
                        "a click track, not a cadence — a reel wants a median shot "
                        "length with accents and rests around it, which is what the "
                        "reference reels measure as."
                    ),
                    change=breathe,
                    objective=self.objective,
                    risk=Risk.MEDIUM,
                )
            )

        # --- 1. Exposure matching ----------------------------------------
        #  Everything from here down pulls the spread *in*, so it is gated on
        #  the timeline not having already collapsed, and on the drift being
        #  bad enough to be a fault rather than merely present. The old
        #  threshold of 0.25 fired on almost every real film — footage shot on
        #  a phone across an afternoon drifts more than that by lunchtime.
        drift = _exposure_balance(edl)
        if drift > 0.55 and not collapsed:
            target_exposure = sum(s.look.exposure for s in edl.shots) / len(edl.shots)

            def match_exposure(target: EditDecisionList, mid: float = target_exposure) -> None:
                for shot in target.shots:
                    shot.look.exposure = shot.look.exposure * 0.4 + mid * 0.6

            proposals.append(
                Proposal(
                    agent=self.name,
                    title="Match exposure across the cut",
                    reason=(
                        f"The exposure drifts {drift:.0%} across the edit. A curator would "
                        "notice this before the content — shots that do not sit at the same "
                        "brightness read as ungraded dailies, not a finished piece."
                    ),
                    change=match_exposure,
                    objective=self.objective,
                    risk=Risk.LOW,
                )
            )

        # --- 2. Colour temperature continuity ----------------------------
        temp_drift = _palette_drift(edl)
        if temp_drift > 0.60 and not collapsed:
            target_temp = sum(s.look.temperature for s in edl.shots) / len(edl.shots)

            def unify_temperature(target: EditDecisionList, mid: float = target_temp) -> None:
                for shot in target.shots:
                    shot.look.temperature = shot.look.temperature * 0.35 + mid * 0.65

            proposals.append(
                Proposal(
                    agent=self.name,
                    title="Unify colour temperature",
                    reason=(
                        f"Temperature swings {temp_drift:.0%} between shots. Mixed daylight "
                        "and tungsten in the same film reads as accidental unless a scene "
                        "change earns it — and short form has no scene changes."
                    ),
                    change=unify_temperature,
                    objective=self.objective,
                    risk=Risk.LOW,
                )
            )

        # --- 3. Focal anchor — lead the eye, do not scatter it -----------
        weights = _focal_weight(edl, self.readings)
        weak = [i for i, w in enumerate(weights) if w < 0.35]
        if len(weak) > len(edl.shots) * 0.6 and not collapsed:

            def anchor_focus(target: EditDecisionList) -> None:
                for _i, shot in enumerate(target.shots):
                    cx, cy = shot.motion.anchor
                    # Pull toward the nearest rule-of-thirds intersection.
                    thirds = [
                        (1 / 3, 1 / 3),
                        (2 / 3, 1 / 3),
                        (1 / 3, 2 / 3),
                        (2 / 3, 2 / 3),
                    ]
                    best_pt = min(thirds, key=lambda p: math.hypot(cx - p[0], cy - p[1]))
                    # Gentle pull, not a snap — the footage still has to be in frame.
                    new_x = cx * 0.45 + best_pt[0] * 0.55
                    new_y = cy * 0.45 + best_pt[1] * 0.55
                    shot.motion = Motion(
                        kind=shot.motion.kind,
                        intensity=shot.motion.intensity,
                        anchor=(round(new_x, 3), round(new_y, 3)),
                    )

            proposals.append(
                Proposal(
                    agent=self.name,
                    title="Anchor the focal weight to the power points",
                    reason=(
                        f"{len(weak)} of {len(edl.shots)} shots have their focal anchor "
                        "away from every rule-of-thirds intersection. The eye has nowhere "
                        "to land, which reads as footage that was not composed — the "
                        "difference between a snapshot and a photograph."
                    ),
                    change=anchor_focus,
                    objective=self.objective,
                    risk=Risk.MEDIUM,
                )
            )

        # --- 4. Contrast coherence — the tonal voice of the piece --------
        contrasts = [shot.look.contrast for shot in edl.shots]
        contrast_range = max(contrasts) - min(contrasts)
        if contrast_range > 0.70 and not collapsed:
            target_contrast = sum(contrasts) / len(contrasts)

            def match_contrast(target: EditDecisionList, mid: float = target_contrast) -> None:
                for shot in target.shots:
                    shot.look.contrast = shot.look.contrast * 0.4 + mid * 0.6

            proposals.append(
                Proposal(
                    agent=self.name,
                    title="Even out the contrast across shots",
                    reason=(
                        f"Contrast swings {contrast_range:.2f} across the cut. A punchy shot "
                        "next to a flat one makes the flat one look like a mistake rather "
                        "than a choice."
                    ),
                    change=match_contrast,
                    objective=self.objective,
                    risk=Risk.LOW,
                )
            )

        # --- 5. Abrupt transition after a held gaze ---------------------
        #  A dissolve after a long, still shot honours the weight of the frame;
        #  a hard cut throws it away.
        for i, shot in enumerate(edl.shots[1:], 1):
            prev = edl.shots[i - 1]
            if (
                prev.duration > 2.5
                and prev.motion.kind in ("none", "ken-burns")
                and shot.transition_in.is_cut
            ):

                def soften(target: EditDecisionList, idx: int = i) -> None:
                    if idx < len(target.shots):
                        target.shots[idx].transition_in = Transition(kind="dissolve", duration=0.35)

                proposals.append(
                    Proposal(
                        agent=self.name,
                        title=f"Dissolve into shot {i + 1} after the held frame",
                        reason=(
                            f"Shot {i} holds for {prev.duration:.1f}s with almost no movement. "
                            "Cutting hard out of a still frame wastes the weight it built — a "
                            "short dissolve hands the eye to the next image instead of "
                            "dropping it."
                        ),
                        change=soften,
                        objective=self.objective,
                        risk=Risk.LOW,
                    )
                )
                break  # one dissolve proposal per round is enough

        return proposals
