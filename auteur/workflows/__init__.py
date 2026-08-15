"""Workflows: footage in one end, something you can post out of the other.

`auteur edit` makes a film. That is not the same as making a post, and the
difference is all the small work nobody enjoys: cutting to the length the
surface accepts, keeping the words out from under the app's own buttons,
pulling a cover frame, drafting a caption, and writing down what was made so it
can be found again next week.

A workflow is that whole run, named after where it is going:

    auteur workflow run instagram-reel ./clips "harbour at dusk"
    auteur workflow run tiktok ./clips "harbour at dusk" --schedule "friday 18:00"

Each one is the same three pieces in a different order — a `PlatformSpec` that
says what the destination wants, `agent.direct` to make the film, and
`publish.package` to turn the render into a post folder. There is deliberately
very little logic here: a workflow that needed clever code would be a workflow
that disagreed with the editor, and the editor should win.

Nothing in this package uploads anything. See `publish` for why.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import QUALITIES, Quality, Settings
from ..edl import EditDecisionList
from ..ui import NullReporter, Reporter
from .platforms import AS_OF, PLATFORMS, PlatformSpec, SafeArea, resolve
from .publish import Caption, Deliverable, cover_frame, draft_caption, package
from .schedule import Post, Schedule

log = logging.getLogger("auteur.workflows")

__all__ = [
    "AS_OF",
    "Caption",
    "Deliverable",
    "Post",
    "PlatformSpec",
    "SafeArea",
    "Schedule",
    "Workflow",
    "WORKFLOWS",
    "catalogue",
    "cover_frame",
    "draft_caption",
    "package",
    "resolve",
    "run",
    "wanted_duration",
    "with_agents",
]


@dataclass(frozen=True)
class Workflow:
    """A named end-to-end run, and the destination that shapes it."""

    name: str
    spec: PlatformSpec
    summary: str

    def describe(self) -> str:
        return f"{self.name:<18} {self.summary}"


def _summary(spec: PlatformSpec) -> str:
    return f"{spec.service} {spec.surface} — {spec.describe()}"


#: One workflow per destination. The registry is derived from the platform
#: table rather than written twice, so adding a platform adds a workflow.
WORKFLOWS: dict[str, Workflow] = {
    name: Workflow(name=name, spec=spec, summary=_summary(spec))
    for name, spec in PLATFORMS.items()
}


def catalogue() -> str:
    """Every workflow, for `auteur workflow list`."""
    lines = [
        "",
        f"  Workflows (platform rules as of {AS_OF} — check them before you rely on them)",
        "",
    ]
    for workflow in WORKFLOWS.values():
        spec = workflow.spec
        lines.append(f"      {workflow.name}")
        lines.append(f"          {_summary(spec)}")
        lines.append(f"          {spec.safe.describe()}")
        if spec.caption_limit:
            lines.append(
                f"          caption up to {spec.caption_limit} characters, "
                f"{spec.hashtag_limit} hashtags"
            )
        if spec.note:
            lines.append(f"          {spec.note}")
        lines.append("")
    return "\n".join(lines)


def keep_text_readable(spec: PlatformSpec) -> callable:
    """A plan hook that moves every title inside the platform's safe area.

    The director places text for the composition, which is the right instinct
    and the wrong frame: it does not know that the bottom fifth of a TikTok is
    a caption and a row of buttons. This runs after the cut is planned and
    before anything renders, and only ever pulls text inward — a title already
    in a safe place is left exactly where the director put it.
    """

    def adjust(edl: EditDecisionList) -> None:
        if spec.safe.is_empty:
            return
        moved = 0
        for cue in edl.texts:
            before = cue.anchor
            cue.anchor = spec.safe.clamp(before)
            if cue.anchor != before:
                moved += 1
        if moved:
            log.info("moved %d title(s) clear of the %s interface", moved, spec.service)

    return adjust


def wanted_duration(
    spec: PlatformSpec, prompt: str, length: float | None = None
) -> float:
    """How long this film should be, in the order the request actually implies.

    A flag beats the prompt, the prompt beats the platform's house length, and
    the platform's limits beat all three. The middle step is the one that is
    easy to lose: asking for "12 seconds" and being handed 25 because Reels
    prefers 25 is the workflow overruling the person using it, which it has no
    business doing while 12 seconds is a legal length for a Reel.
    """
    asked = length
    if asked is None:
        from ..director.brief import parse_brief

        asked = parse_brief(prompt).duration
    return spec.fit_duration(asked)


def with_agents(spec: PlatformSpec, crew, *, on_result=None):
    """A plan hook that lets the agents re-cut before anything renders.

    Composed with `keep_text_readable`, and deliberately running *before* it:
    the agents move titles around to win the first three seconds, and the safe
    area gets the last word on where a title may actually sit. An agent
    optimising a hook does not know that the bottom fifth of the frame is a
    caption box, and it should not have to.
    """
    readable = keep_text_readable(spec)

    def adjust(edl: EditDecisionList) -> None:
        result = crew.run(edl)
        # The crew works on a copy so a bad proposal cannot damage the original.
        # Copy the survivor back onto the EDL the renderer is holding.
        edl.shots = result.edl.shots
        edl.texts = result.edl.texts
        log.info(
            "agents: predicted %.0f%% -> %.0f%% over %d round(s)",
            result.baseline.overall * 100,
            result.final.overall * 100,
            len(result.rounds),
        )
        readable(edl)
        if on_result is not None:
            on_result(result)

    return adjust


def run(
    platform: str,
    inputs: list[str | Path],
    prompt: str,
    *,
    out: str | Path | None = None,
    quality: Quality | str = "standard",
    length: float | None = None,
    reporter: Reporter | None = None,
    settings: Settings | None = None,
    crew=None,
    on_agents=None,
) -> tuple[Deliverable, object]:
    """Make one post, start to finish. Returns (deliverable, production).

    The production comes back too because everything interesting about *why*
    the film is the way it is lives on it, and a caller that only wants the
    file can ignore it.

    Pass a `crew` to let the agents re-cut the edit before it renders. They work
    on the planned timeline, keep only what improves the prediction, and ask a
    person about anything their gate says needs one.
    """
    from ..agent import direct  # imported here to keep `auteur --help` fast

    spec = resolve(platform)
    say = reporter or NullReporter()
    tier = (
        QUALITIES[quality]
        if isinstance(quality, str) and quality in QUALITIES
        else quality
    )
    if isinstance(tier, str):
        raise ValueError(f"unknown quality tier: {tier!r}")

    seconds = wanted_duration(spec, prompt, length)
    root = Path(out) if out else Path.cwd() / "auteur-posts"
    workspace = root / spec.name

    settings = settings or Settings()
    settings.quality = tier
    settings.primary_format = spec.format
    settings.extra_formats = ()
    settings.target_duration = seconds

    production = direct(
        inputs,
        prompt,
        settings=settings,
        workspace=workspace,
        formats=(spec.format,),
        duration=seconds,
        reporter=say,
        on_plan=(
            with_agents(spec, crew, on_result=on_agents)
            if crew is not None
            else keep_text_readable(spec)
        ),
    )

    video = production.primary
    if video is None:  # pragma: no cover - direct raises before this
        raise RuntimeError("the workflow produced no film")

    deliverable = package(
        video=video,
        spec=spec,
        brief=production.brief,
        edl=production.edl,
        folder=workspace / "post",
    )
    return deliverable, production
