"""Checking an edit against the ways posts are known to fail.

The other agents optimise. This one looks for the specific failures the paired
workflow exports actually recorded — 10,000 posts that worked next to 10,000
that did not, each failure carrying its own diagnosis and the fix the export
recommends. That pairing is the only thing in the corpus that can tell a winner
from a loser; everything else describes winners and hopes.

Seven failure modes appear in that data. Four can be checked before a frame is
rendered, one after, and two cannot be checked here at all — and saying which is
which is most of the value. A preflight that claims to catch everything is a
preflight nobody should trust.

| mode | when | how |
|---|---|---|
| Bad Aspect Ratio | before | the planned frame against the platform's |
| Hook Abandonment | before | predicted three-second watch against the measured boundary |
| Flop Schedule Window | before | the queued time against the windows the wins used |
| Muted Audio Copyright | before | whether the bed is something a platform will mute |
| Corrupt File Upload | after | probe what was actually written |
| Low Organic Traction | — | the catch-all; it is the outcome, not a cause |
| Shadowban Boundary | — | nothing on this machine can see it |
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..edl import EditDecisionList, Ramp
from ..insight import FitReport, Prediction
from .base import Proposal, Risk

#: Failure modes and whether this can honestly check for them. Kept as data so
#: `auteur insight` can print the same table the code acts on.
CHECKABLE = {
    "Bad Aspect Ratio": "before",
    "Hook Abandonment": "before",
    "Flop Schedule Window": "before",
    "Muted Audio Copyright": "before",
    "Corrupt File Upload": "after",
    "Low Organic Traction": "outcome",
    "Shadowban Boundary": "never",
}


@dataclass
class Finding:
    """One failure mode this edit is exposed to."""

    mode: str
    action: str
    detail: str
    #: True when the check ran and found the problem; False when the check
    #: could not run at all, which is a different thing and is reported as one.
    confirmed: bool = True

    def to_json(self) -> dict:
        return {
            "mode": self.mode,
            "action": self.action,
            "detail": self.detail,
            "confirmed": self.confirmed,
        }

    def describe(self) -> str:
        mark = "!" if self.confirmed else "?"
        return f"{mark} {self.mode} → {self.action}\n    {self.detail}"


#: Written onto shots the style agent lengthened deliberately, so the critic
#: can tell an intentional hold from a frozen one. They look identical from the
#: pixels, and only one of them is a mistake.
HELD_ON_PURPOSE = "held for the reference style"

#: Audio a platform is likely to mute. Anything the project synthesised itself
#: is safe by construction, which is the whole argument for synthesising it.
_SAFE_AUDIO = re.compile(r"(^bed_|_bed\.|silence|synth|generated)", re.IGNORECASE)


def check_hook(prediction: Prediction, model: FitReport) -> Finding | None:
    """Hook Abandonment, against the boundary the labelled data actually draws."""
    boundary = model.separation.get("three_second_watch_rate")
    if boundary is None:
        return Finding(
            mode="Hook Abandonment",
            action="RE_EDIT_HOOK_REPLACE",
            detail=(
                "no labelled failures in the corpus, so there is no boundary to check "
                "the hook against — load a workflow-outcome export to enable this"
            ),
            confirmed=False,
        )
    _, _, cut = boundary
    if prediction.hook.predicted < cut:
        return Finding(
            mode="Hook Abandonment",
            action="RE_EDIT_HOOK_REPLACE",
            detail=(
                f"predicted three-second watch {prediction.hook.predicted:.2f} is below "
                f"{cut:.2f}, the midpoint between the posts that worked and the ones that "
                "did not"
            ),
        )
    return None


def check_aspect(edl: EditDecisionList, spec) -> Finding | None:
    """Bad Aspect Ratio — the most mechanical failure in the whole taxonomy."""
    if (edl.width, edl.height) == (spec.format.width, spec.format.height):
        return None
    return Finding(
        mode="Bad Aspect Ratio",
        action="RE_CROP_9_16_ASPECT",
        detail=(
            f"the cut is {edl.width}x{edl.height}; {spec.service} {spec.surface} wants "
            f"{spec.format.width}x{spec.format.height}"
        ),
    )


def check_audio(edl: EditDecisionList) -> Finding | None:
    """Muted Audio Copyright.

    This cannot identify a song. What it can do is tell whether the bed is one
    this project synthesised — which is safe by construction — or something
    that arrived from elsewhere, which is a risk it should name rather than
    quietly accept.
    """
    source = getattr(edl.music, "source", None)
    if not source:
        return None
    name = Path(str(source)).name
    if _SAFE_AUDIO.search(name):
        return None
    return Finding(
        mode="Muted Audio Copyright",
        action="RE_AUDIO_SWAP_TRENDING",
        detail=(
            f"{name} did not come from the synthesiser, so nothing here can tell whether "
            "a platform will mute it. Swap the audio inside the app when you post — that "
            "is where trending tracks are licensed"
        ),
        confirmed=False,
    )


def check_schedule(when, model: FitReport) -> Finding | None:
    """Flop Schedule Window, against the hours the wins actually used."""
    hours = set(model.optimal_hours)
    if not hours or when is None:
        return None
    if when.hour in hours:
        return None
    best = ", ".join(f"{h:02d}:00" for h in sorted(hours)[:6])
    return Finding(
        mode="Flop Schedule Window",
        action="RESCHEDULE_OPTIMAL_PEAK",
        detail=(
            f"{when.strftime('%H:%M')} UTC is outside every window the winning posts used "
            f"({best} UTC)"
        ),
    )


def check_render(video: Path | None) -> Finding | None:
    """Corrupt File Upload — the one check that has to wait for the render."""
    from .. import ffmpeg as ff

    if video is None or not Path(video).exists():
        return Finding(
            mode="Corrupt File Upload",
            action="RE_RENDER_AND_REUPLOAD",
            detail="no file was written",
        )
    try:
        info = ff.probe(video)
        streams = info.get("streams", [])
        duration = float(info["format"]["duration"])
    except (KeyError, ValueError, OSError, ff.FFmpegError) as exc:
        return Finding(
            mode="Corrupt File Upload",
            action="RE_RENDER_AND_REUPLOAD",
            detail=f"the finished file will not probe: {exc}",
        )
    if duration <= 0.1 or not any(s.get("codec_type") == "video" for s in streams):
        return Finding(
            mode="Corrupt File Upload",
            action="RE_RENDER_AND_REUPLOAD",
            detail=f"the file probes but carries {duration:.2f}s and no usable video stream",
        )
    return None


def preflight(
    edl: EditDecisionList, prediction: Prediction, model: FitReport, spec=None, when=None
) -> list[Finding]:
    """Everything checkable, before the renderer spends three minutes."""
    findings = [
        check_hook(prediction, model),
        check_audio(edl),
        check_schedule(when, model),
    ]
    if spec is not None:
        findings.append(check_aspect(edl, spec))
    return [finding for finding in findings if finding is not None]


def unknowable() -> list[str]:
    """The failure modes nothing here can see, named so nobody assumes it can."""
    return [mode for mode, when in CHECKABLE.items() if when in ("never", "outcome")]


# ---------------------------------------------------------------------------
# Cutting toward a reference
# ---------------------------------------------------------------------------


class StyleAgent:
    """Pulls the edit toward footage the user pointed at.

    Ranked above the corpus on purpose. The performance data says nine or ten
    cuts per ten seconds; a reference reel cutting at three says three. "Make it
    like this" is a statement about the work, and a correlation across a
    population does not get to overrule it.

    It only proposes when the gap is large enough to hear — a fifth off the
    reference pace or more. Nudging an edit that is already close is churn.

    **What it cannot do.** `Shot.source_duration` is the range the director
    *selected*, not the length of the clip, and this agent has no dossiers — so
    it can slow what was chosen but cannot reach further into unused footage to
    fill a gap. A film built from short selections will therefore land slower
    than it started and still short of the reference: closer, not equal. Giving
    the agent the clip lengths would fix it, and would also let it undo choices
    the director made for reasons it cannot see.
    """

    name = "style"
    objective = "reference_match"

    def __init__(self, target):
        self.target = target

    def inspect(
        self, edl: EditDecisionList, prediction: Prediction, model: FitReport
    ) -> list[Proposal]:
        target = self.target
        if target is None or target.is_empty or not edl.shots:
            return []

        runtime = edl.duration
        if runtime <= 0:
            return []
        pace = len(edl.shots) / runtime * 10.0
        wanted = target.cuts_per_10s
        if wanted <= 0 or abs(pace - wanted) / wanted < 0.2:
            return []

        proposals: list[Proposal] = []
        if pace > wanted:
            keep = max(2, round(wanted * runtime / 10.0))

            def slow_down(
                target_edl: EditDecisionList, keep: int = keep, runtime: float = runtime
            ) -> None:
                # Fewer cuts *at the same runtime*. The reference is slower, not
                # shorter — an earlier version stretched and dropped in the same
                # pass and turned a sixteen-second film into six.
                #
                # So: thin the shots evenly, then give the survivors the whole
                # runtime back between them.
                shots = target_edl.shots
                if keep >= len(shots) or keep < 1:
                    return
                # Keep the first and the last — the hook and the loop depend on
                # them — and space the rest out across the middle.
                step = (len(shots) - 1) / (keep - 1) if keep > 1 else len(shots)
                indices = sorted({0, len(shots) - 1, *(round(i * step) for i in range(keep))})
                indices = [i for i in indices if 0 <= i < len(shots)][:keep]
                target_edl.shots = [shots[i] for i in indices]

                each = runtime / len(target_edl.shots)
                for shot in target_edl.shots:
                    # Mark it. The critic drops shots where nothing moves, and
                    # a shot held on purpose to match a reference looks exactly
                    # like one — it was deleting these as fast as this made
                    # them, turning a sixteen-second film into seven.
                    shot.note = HELD_ON_PURPOSE
                    if shot.is_still:
                        # A still can be held for as long as you like.
                        shot.end = shot.start + each
                        continue
                    # A clip cannot outrun its own footage — but it can be
                    # played slower, which is what the reference style wants
                    # anyway. Clamping to the available footage instead left
                    # the runtime collapsing as shots were dropped, so the
                    # cutting rate never actually fell.
                    available = max(0.2, shot.source_duration)
                    if available >= each:
                        shot.end = shot.start + each
                    else:
                        # Not slower than 0.4x: past that it stops reading as a
                        # held beat and starts reading as a technical fault.
                        speed = max(0.4, available / each)
                        shot.ramp = Ramp.constant(speed)
                        shot.end = shot.start + min(available, each * speed)

            proposals.append(
                Proposal(
                    agent=self.name,
                    title=f"Slow the cutting from {pace:.1f} to {wanted:.1f} per 10s",
                    reason=(
                        f"Your reference footage cuts {wanted:.1f} times every ten seconds and "
                        f"holds a shot for {target.shot_seconds:.1f}s. This is running at "
                        f"{pace:.1f}, which is a different film."
                    ),
                    change=slow_down,
                    objective=self.objective,
                    risk=Risk.HIGH,
                    binding=True,
                )
            )
        else:

            def quicken(target_edl: EditDecisionList, wanted: float = wanted) -> None:
                shrink = pace / wanted
                for shot in target_edl.shots:
                    shot.end = shot.start + max(0.2, shot.source_duration * shrink)

            proposals.append(
                Proposal(
                    agent=self.name,
                    title=f"Quicken the cutting from {pace:.1f} to {wanted:.1f} per 10s",
                    reason=(
                        f"Your reference footage cuts {wanted:.1f} times every ten seconds; "
                        f"this is holding at {pace:.1f}."
                    ),
                    change=quicken,
                    objective=self.objective,
                    risk=Risk.HIGH,
                    binding=True,
                )
            )
        return proposals
