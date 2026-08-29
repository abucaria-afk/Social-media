"""The Final Check agent — automated quality control before export.

An edit can pass every creative test and still fail mechanically: the frame
size is wrong for the platform, the audio is missing or too quiet, a title
sits where the app's own buttons will cover it, or the opening frame is black.
These are the failures that happen *after* the cut is good, and they are the
ones nobody wants to debug by eye.

This agent inspects the planned timeline the way a QC department inspects a
deliverable: resolution, safe zones, text placement, audio presence, overlay
readability, and frame-rate consistency. It proposes corrections — moving a
title into the safe zone, adjusting audio gain, fixing letterbox for the
target format — and each proposal is scored and gated like any other.

Implementation note: the requirement describes a Blender-native pipeline
(compositor nodes, VSE tracks, bpy). This codebase renders with ffmpeg. The
agent therefore checks and corrects the same concerns — overlays, audio sync,
resolution, safe zones — through the EDL and the craft modules that the
renderer actually uses.
"""

from __future__ import annotations

from ..edl import EditDecisionList, TextCue
from ..workflows.platforms import PLATFORMS
from ..insight import FitReport, Prediction
from .base import Proposal, Risk


# ---------------------------------------------------------------------------
# Safe-zone margins — derived, not typed
# ---------------------------------------------------------------------------
#
# These were `0.08 / 0.12 / 0.05`, written here by hand, while
# `workflows/platforms.py` held the real per-platform figures — dated, sourced,
# and re-checked against what the platforms publish. Two copies of one fact
# about somebody else's user interface, and the hand-written copy was the
# looser one. Every platform had a band this gate passed and the app covers:
#
#     tiktok           a title anywhere in y 0.78-0.88 or x 0.84-0.95
#     instagram-reel   y 0.80-0.88, y 0.08-0.10, x 0.86-0.95
#     youtube-short    y 0.82-0.88, x 0.88-0.95
#
# The module docstring above promises this agent catches "a title sits where
# the app's own buttons will cover it". It caught a looser version of that and
# passed the rest, which is the failure mode a QC gate cannot have: the whole
# value of the check is that a pass means something.
#
# An `EditDecisionList` carries `width` and `height` and no platform name, so
# this cannot know where the film is going. A gate that does not know its
# destination has to assume the worst one — so these are the strictest margin
# of every platform in the table, taken from the table.
def _strictest(edge: str) -> float:
    """The largest margin any platform claims on that edge."""
    return max(getattr(spec.safe, edge) for spec in PLATFORMS.values())


_SAFE_TOP = _strictest("top")
_SAFE_BOTTOM = _strictest("bottom")
#: One figure for both sides, and it is the wider of the two: the right edge is
#: where TikTok and Instagram stack their action buttons, so the asymmetry is
#: real. Keeping one number means a title mirrored left-to-right is still safe.
_SAFE_SIDE = max(_strictest("left"), _strictest("right"))


def _texts_outside_safe(edl: EditDecisionList) -> list[tuple[int, TextCue]]:
    """Find text cues whose anchors sit under the platform's UI chrome."""
    unsafe: list[tuple[int, TextCue]] = []
    for i, cue in enumerate(edl.texts):
        ax, ay = cue.anchor
        if (
            ay < _SAFE_TOP
            or ay > (1.0 - _SAFE_BOTTOM)
            or ax < _SAFE_SIDE
            or ax > (1.0 - _SAFE_SIDE)
        ):
            unsafe.append((i, cue))
    return unsafe


def _opening_is_black(edl: EditDecisionList) -> bool:
    """True when the first shot has a deep exposure correction — likely black."""
    if not edl.shots:
        return False
    return edl.shots[0].look.exposure < -0.6


def _audio_is_present(edl: EditDecisionList) -> bool:
    """True when the edit has a music bed or at least one sound effect."""
    return edl.music.source is not None or len(edl.sfx) > 0


def _sfx_coverage(edl: EditDecisionList) -> float:
    """Fraction of cuts that have a sound effect within 0.3s of them."""
    cuts = edl.cut_times()
    if not cuts or not edl.sfx:
        return 0.0
    covered = 0
    sfx_times = [s.at for s in edl.sfx]
    for cut in cuts:
        if any(abs(t - cut) < 0.3 for t in sfx_times):
            covered += 1
    return covered / len(cuts)


def _music_gain_low(edl: EditDecisionList) -> bool:
    """True when the music bed is set quieter than most platforms want."""
    return edl.music.source is not None and edl.music.gain < 0.35


def _resolution_matches(edl: EditDecisionList) -> bool:
    """True when width and height are standard delivery dimensions."""
    standard = {
        (1080, 1920),
        (1080, 1080),
        (1920, 1080),
        (1080, 1350),
        (1920, 816),
        (720, 1280),
        (640, 480),
    }
    return (edl.width, edl.height) in standard


class FinalCheckAgent:
    """Owns export readiness — the QC gate before the file leaves the machine.

    Checks the concerns a compositing department checks: safe zones for text
    overlays, audio presence and levels, resolution conformance, opening-frame
    quality, and sound-effect coverage at cuts. Every proposal is a concrete
    fix to the EDL, not a note to a person.

    This is the last agent to run. The other agents decide what the film *is*;
    this one makes sure it *ships*.
    """

    name = "final_check"
    objective = "export_readiness"

    def inspect(
        self, edl: EditDecisionList, prediction: Prediction, model: FitReport
    ) -> list[Proposal]:
        if not edl.shots:
            return []

        proposals: list[Proposal] = []

        # --- 1. Text safe zones ------------------------------------------
        unsafe = _texts_outside_safe(edl)
        if unsafe:
            indices = [i for i, _ in unsafe]

            def fix_safe_zones(target: EditDecisionList, bad: list[int] = indices) -> None:
                for i in bad:
                    if i >= len(target.texts):
                        continue
                    cue = target.texts[i]
                    ax = max(_SAFE_SIDE + 0.02, min(cue.anchor[0], 1.0 - _SAFE_SIDE - 0.02))
                    ay = max(_SAFE_TOP + 0.02, min(cue.anchor[1], 1.0 - _SAFE_BOTTOM - 0.02))
                    cue.anchor = (round(ax, 3), round(ay, 3))

            names = ", ".join(f'"{edl.texts[i].text[:20]}"' for i, _ in unsafe[:3])
            proposals.append(
                Proposal(
                    agent=self.name,
                    title=f"Move {len(unsafe)} text cue(s) into the safe zone",
                    reason=(
                        f"{names} sit where the app's own buttons will cover them. "
                        "A title nobody can read is worse than no title."
                    ),
                    change=fix_safe_zones,
                    objective=self.objective,
                    risk=Risk.LOW,
                )
            )

        # --- 2. Black opening frame --------------------------------------
        if _opening_is_black(edl):

            def lift_opening(target: EditDecisionList) -> None:
                target.shots[0].look.exposure = max(target.shots[0].look.exposure, -0.15)

            proposals.append(
                Proposal(
                    agent=self.name,
                    title="Lift the opening frame out of black",
                    reason=(
                        "The first frame is near-black. On a phone feed a dark thumbnail "
                        "is invisible — the cover frame needs enough contrast to read at "
                        "the size of a postage stamp."
                    ),
                    change=lift_opening,
                    objective=self.objective,
                    risk=Risk.LOW,
                )
            )

        # --- 3. Audio presence -------------------------------------------
        if not _audio_is_present(edl):

            def add_sfx_at_cuts(target: EditDecisionList) -> None:
                from ..edl import SoundCue

                for t in target.cut_times()[:8]:
                    target.sfx.append(SoundCue(kind="tick", at=round(t, 3), gain=0.5))

            proposals.append(
                Proposal(
                    agent=self.name,
                    title="Add sound markers at cuts",
                    reason=(
                        "The edit has no audio at all — no music bed, no effects. Silent "
                        "video on a feed autoplays muted, but the render still needs a "
                        "sound track for the people who do tap to listen."
                    ),
                    change=add_sfx_at_cuts,
                    objective=self.objective,
                    risk=Risk.MEDIUM,
                )
            )

        # --- 4. Music gain too low ---------------------------------------
        if _music_gain_low(edl):

            def raise_music(target: EditDecisionList) -> None:
                target.music.gain = max(target.music.gain, 0.70)

            proposals.append(
                Proposal(
                    agent=self.name,
                    title="Raise the music bed to a usable level",
                    reason=(
                        f"Music gain is {edl.music.gain:.2f}, which will be inaudible after "
                        "platform loudness normalisation. A bed that is too quiet is the "
                        "same as no bed."
                    ),
                    change=raise_music,
                    objective=self.objective,
                    risk=Risk.LOW,
                )
            )

        # --- 5. Resolution sanity ----------------------------------------
        if not _resolution_matches(edl):

            def snap_resolution(target: EditDecisionList) -> None:
                # Pick the nearest standard resolution.
                standards = [
                    (1080, 1920),
                    (1080, 1080),
                    (1920, 1080),
                    (1080, 1350),
                    (1920, 816),
                ]
                current_aspect = target.width / max(target.height, 1)
                best = min(
                    standards,
                    key=lambda s: abs(s[0] / s[1] - current_aspect),
                )
                target.width, target.height = best

            proposals.append(
                Proposal(
                    agent=self.name,
                    title=f"Snap {edl.width}×{edl.height} to a standard delivery size",
                    reason=(
                        f"{edl.width}×{edl.height} is not a standard delivery resolution. "
                        "Non-standard sizes get re-encoded by the platform, and the second "
                        "encode is never kind."
                    ),
                    change=snap_resolution,
                    objective=self.objective,
                    risk=Risk.MEDIUM,
                )
            )

        # --- 6. SFX coverage at cuts -------------------------------------
        coverage = _sfx_coverage(edl)
        cuts = edl.cut_times()
        if coverage < 0.3 and len(cuts) > 3 and _audio_is_present(edl):

            def fill_sfx(target: EditDecisionList) -> None:
                from ..edl import SoundCue

                sfx_times = {round(s.at, 2) for s in target.sfx}
                for t in target.cut_times():
                    if round(t, 2) not in sfx_times:
                        target.sfx.append(
                            SoundCue(kind="whoosh", at=round(t, 3), gain=0.4, duration=0.3)
                        )

            proposals.append(
                Proposal(
                    agent=self.name,
                    title=f"Add sound design at {len(cuts) - int(coverage * len(cuts))} uncovered cuts",
                    reason=(
                        f"Only {coverage:.0%} of cuts have a sound effect near them. Cuts "
                        "without audio support feel like mistakes — a whoosh or a tick at "
                        "the join sells the transition."
                    ),
                    change=fill_sfx,
                    objective=self.objective,
                    risk=Risk.LOW,
                )
            )

        # --- 7. Frame rate sanity ----------------------------------------
        if edl.fps not in (24, 25, 30, 50, 60):

            def fix_fps(target: EditDecisionList) -> None:
                target.fps = 30

            proposals.append(
                Proposal(
                    agent=self.name,
                    title=f"Set frame rate from {edl.fps} to 30fps",
                    reason=(
                        f"{edl.fps}fps is non-standard for social delivery. Platforms will "
                        "re-encode it to 30, and the re-encode adds a generation of loss."
                    ),
                    change=fix_fps,
                    objective=self.objective,
                    risk=Risk.LOW,
                )
            )

        return proposals
