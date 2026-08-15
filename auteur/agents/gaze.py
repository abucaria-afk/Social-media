"""The Gaze agent — a holistic visual curator that sees each frame whole.

A trained eye at the MET or the Louvre does not read a painting pixel by pixel.
It registers the composition instantly: where the weight falls, where the light
pulls you, what the dominant palette is doing to the mood, whether the depth
layers resolve or compete, and whether the frame has a focal anchor at all.

This agent does that to every shot on the timeline. It works from the analysis
numbers already measured (luma, contrast, sharpness, edges, subject position,
colour, motion) but reads them the way a curator reads a gallery wall — as
*relationships* between shots, not as absolute values.

Its objective is visual coherence: how consistently the film's frames read as
a single authored piece rather than a shuffled stack of footage. It proposes
changes that improve composition flow, colour continuity, exposure matching,
and focal weight — the things a gallerist would notice before a viewer does.
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


def _focal_weight(edl: EditDecisionList) -> list[float]:
    """Per-shot focal strength, 0–1.  Shots with motion anchors near the
    centre and no competing movement carry more weight."""
    weights: list[float] = []
    for shot in edl.shots:
        cx, cy = shot.motion.anchor
        # Distance of the anchor from the power points (rule-of-thirds
        # intersections). Closer to a power point = stronger focal pull.
        thirds = [(1 / 3, 1 / 3), (2 / 3, 1 / 3), (1 / 3, 2 / 3), (2 / 3, 2 / 3)]
        best = min(math.hypot(cx - tx, cy - ty) for tx, ty in thirds)
        # Normalise: 0 at a power point, 1 at the farthest corner.
        strength = max(0.0, 1.0 - best / 0.47)
        weights.append(strength)
    return weights


def _composition_score(edl: EditDecisionList) -> float:
    """Overall compositional coherence, 0–1."""
    if not edl.shots:
        return 1.0
    weights = _focal_weight(edl)
    avg = sum(weights) / len(weights) if weights else 0.5
    exposure = 1.0 - _exposure_balance(edl)
    palette = 1.0 - _palette_drift(edl)
    return (avg * 0.4 + exposure * 0.35 + palette * 0.25)


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

    def inspect(
        self, edl: EditDecisionList, prediction: Prediction, model: FitReport
    ) -> list[Proposal]:
        if len(edl.shots) < 2:
            return []

        proposals: list[Proposal] = []

        # --- 1. Exposure matching ----------------------------------------
        drift = _exposure_balance(edl)
        if drift > 0.25:
            target_exposure = sum(s.look.exposure for s in edl.shots) / len(edl.shots)

            def match_exposure(
                target: EditDecisionList, mid: float = target_exposure
            ) -> None:
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
        if temp_drift > 0.30:
            target_temp = sum(s.look.temperature for s in edl.shots) / len(edl.shots)

            def unify_temperature(
                target: EditDecisionList, mid: float = target_temp
            ) -> None:
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
        weights = _focal_weight(edl)
        weak = [i for i, w in enumerate(weights) if w < 0.35]
        if len(weak) > len(edl.shots) * 0.4:

            def anchor_focus(target: EditDecisionList) -> None:
                for _i, shot in enumerate(target.shots):
                    cx, cy = shot.motion.anchor
                    # Pull toward the nearest rule-of-thirds intersection.
                    thirds = [
                        (1 / 3, 1 / 3), (2 / 3, 1 / 3),
                        (1 / 3, 2 / 3), (2 / 3, 2 / 3),
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
        if contrast_range > 0.35:
            target_contrast = sum(contrasts) / len(contrasts)

            def match_contrast(
                target: EditDecisionList, mid: float = target_contrast
            ) -> None:
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
                        target.shots[idx].transition_in = Transition(
                            kind="dissolve", duration=0.35
                        )

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
