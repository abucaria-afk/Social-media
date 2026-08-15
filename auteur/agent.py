"""The agent loop.

Ingest, watch, direct, cut, render, watch again, fix, render again. The second
viewing is the part that matters: without it this is a script that turns a
prompt into a file, and there is no way for it to notice that what it made is
bad.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable

from . import critic, render, ui
from .analysis import ClipDossier, build_dossiers, find_music_bed
from .analysis.audio import AudioAnalysis
from .config import DeliveryFormat, Settings, Workspace
from .director import plan
from .director.brief import Brief, parse_brief
from .edl import EditDecisionList
from .ingest import Bin, MediaAsset, ingest
from .ui import NullReporter, Reporter

log = logging.getLogger("auteur.agent")


@dataclass
class Round:
    """One pass of render-and-watch."""

    index: int
    critique: critic.Critique
    revisions: list[str] = field(default_factory=list)
    outputs: dict[str, Path] = field(default_factory=dict)
    seconds: float = 0.0


@dataclass
class Production:
    """Everything the agent made, and everything it thought while making it."""

    brief: Brief
    bin: Bin
    dossiers: list[ClipDossier]
    edl: EditDecisionList
    workspace: Workspace
    rounds: list[Round] = field(default_factory=list)
    directed_by: str = "heuristic"
    fallback_reason: str = ""
    music: MediaAsset | None = None
    music_analysis: AudioAnalysis | None = None
    seconds: float = 0.0

    @property
    def outputs(self) -> dict[str, Path]:
        return self.rounds[-1].outputs if self.rounds else {}

    @property
    def primary(self) -> Path | None:
        return next(iter(self.outputs.values()), None)

    @property
    def final_critique(self) -> critic.Critique | None:
        return self.rounds[-1].critique if self.rounds else None

    def report(self) -> str:
        """The production notes — what it saw, what it decided, what it fixed."""
        lines = [
            f"# {self.edl.title}",
            "",
            f"*{self.brief.prompt}*",
            "",
            f"- Directed by: **{self.directed_by}**"
            + (
                f" (model unavailable: {self.fallback_reason})"
                if self.fallback_reason and self.directed_by == "heuristic"
                else ""
            ),
            f"- Runtime: **{self.edl.duration:.2f}s** across **{len(self.edl.shots)} shots**",
            f"- Brief read as: {self.brief.describe()}",
            f"- Total time: {self.seconds:.1f}s",
            "",
            "## The footage",
            "",
            "```",
            self.bin.describe(),
            "```",
            "",
        ]
        if self.music_analysis is not None and self.music_analysis.has_beat:
            lines += [
                f"Music: **{self.music.name if self.music else 'unknown'}** at "
                f"**{self.music_analysis.tempo:.0f} BPM** "
                f"(confidence {self.music_analysis.tempo_confidence:.2f}); "
                f"the film starts {self.edl.music.offset:.2f}s into the track.",
                "",
            ]

        lines += ["## The cut", "", "```", self.edl.describe(), "```", ""]

        if self.rounds:
            lines += ["## Watching it back", ""]
            for round_ in self.rounds:
                lines.append(f"### Pass {round_.index} — score {round_.critique.score:.2f}")
                lines.append("")
                for note in sorted(round_.critique.notes, key=lambda n: -n.severity):
                    lines.append(f"- {note}")
                if not round_.critique.notes:
                    lines.append("- nothing to fix")
                if round_.revisions:
                    lines.append("")
                    lines.append("Fixed:")
                    for revision in round_.revisions:
                        lines.append(f"- {revision}")
                lines.append("")

        if self.outputs:
            lines += ["## Delivered", ""]
            for name, path in self.outputs.items():
                lines.append(f"- **{name}** — `{path}`")
        return "\n".join(lines)


def direct(
    inputs: list[str | Path],
    prompt: str,
    *,
    settings: Settings | None = None,
    workspace: str | Path | None = None,
    formats: tuple[DeliveryFormat, ...] | None = None,
    duration: float | None = None,
    reporter: Reporter | None = None,
    on_plan: Callable[[EditDecisionList], None] | None = None,
) -> Production:
    """Turn a pile of clips and a sentence of direction into a finished film.

    `on_plan` is handed the edit after it is planned and before a single frame
    is rendered. It exists for things that know something about the film's
    destination that the director does not — the workflows use it to move
    titles out from under TikTok's buttons. Anything it changes is what gets
    rendered, so it is the last honest place to intervene.
    """
    started = time.perf_counter()
    settings = settings or Settings()
    space = Workspace(workspace or Path.cwd() / "auteur-work")
    say = reporter or NullReporter()

    # ---- 1. what have we got? -------------------------------------------
    say.step("Looking at your footage")
    log.info("ingesting %s", ", ".join(str(path) for path in inputs))
    bin_ = ingest(inputs)
    log.info("%s", bin_.describe())
    say.detail(
        f"{ui.describe_count(len(bin_.visuals), 'clip')}, "
        f"{ui.describe_duration(bin_.total_footage)} of material"
        + (
            f", plus {ui.describe_count(len(bin_.audio), 'music track')}"
            if bin_.audio
            else ", no music"
        )
    )
    if bin_.rejected:
        say.warn(f"skipped {ui.describe_count(len(bin_.rejected), 'file')} it could not read")

    # ---- 2. watch and listen to all of it --------------------------------
    say.step("Watching every clip")
    log.info("analysing %d clip(s)", len(bin_.visuals))
    dossiers = build_dossiers(
        bin_.visuals,
        analysis_fps=settings.quality.analysis_fps,
        analysis_width=settings.quality.analysis_width,
        workers=settings.threads,
    )
    usable = sum(len(dossier.takes) for dossier in dossiers)
    internal = sum(len(dossier.video.shot_boundaries) for dossier in dossiers)
    say.detail(
        f"found {ui.describe_count(usable, 'moment')} worth using"
        + (f"; {ui.describe_count(internal, 'cut')} already in the footage" if internal else "")
    )

    music, music_analysis = find_music_bed(bin_.audio)
    if music_analysis is not None and music_analysis.has_beat:
        say.step("Listening to the music")
        say.detail(f"{music_analysis.tempo:.0f} beats per minute — the cuts will land on the beat")

    # ---- 3. read the brief and cut the film -------------------------------
    brief = parse_brief(prompt, duration=duration)
    # brief.duration is the checked one — an explicit `duration=` argument used
    # to be assigned here raw, which is how a negative runtime reached the
    # planner. Anything unusable leaves the setting at its default.
    if brief.duration:
        settings.target_duration = brief.duration
    log.info("brief: %s", brief.describe())

    say.step("Planning the edit")
    direction = plan.direct(brief, dossiers, settings, music=music, music_analysis=music_analysis)
    edl = direction.edl
    if on_plan is not None:
        on_plan(edl)
    say.detail(
        f"{ui.describe_count(len(edl.shots), 'shot')} over "
        f"{ui.describe_duration(edl.duration)}, {edl.look.preset} look"
    )
    if direction.directed_by == "model":
        say.detail("shots chosen by Claude")
    else:
        message, is_problem = ui.plain_model_reason(direction.fallback_reason)
        if not message:
            say.detail("shots chosen by the built-in editor")
        elif is_problem:
            say.warn(message)
        else:
            say.detail(message)

    production = Production(
        brief=brief,
        bin=bin_,
        dossiers=dossiers,
        edl=edl,
        workspace=space,
        directed_by=direction.directed_by,
        fallback_reason=direction.fallback_reason,
        music=music,
        music_analysis=music_analysis,
    )

    by_id = {dossier.clip_id: dossier for dossier in dossiers}
    target_formats = formats if formats is not None else settings.all_formats
    music_offset = edl.music.offset if edl.music.source else 0.0

    # ---- 4. render, watch, fix, render again ------------------------------
    for index in range(settings.revision_rounds + 1):
        round_started = time.perf_counter()
        edl.save(space.root / f"edl-pass{index}.json")

        say.step("Rendering" if index == 0 else "Rendering the new cut")
        result = render.render(
            edl,
            space,
            settings,
            formats=target_formats,
            name=edl.title,
            on_progress=say.progress,
        )
        say.progress_done("done")
        for warning in result.warnings:
            say.warn(warning)

        primary = result.primary
        if primary is None:  # pragma: no cover - render always writes something
            raise RuntimeError("the renderer produced no output")

        say.step("Watching it back")
        critique = critic.review(
            edl,
            primary,
            target_duration=settings.target_duration,
            audio=music_analysis,
            music_offset=music_offset,
        )
        round_ = Round(index=index, critique=critique, outputs=dict(result.outputs))
        log.info("pass %d scored %.2f", index, critique.score)
        for note in critique.notes:
            log.info("  %s", note)

        if not critique.notes:
            say.detail("nothing to fix")
        for note in sorted(critique.notes, key=lambda n: -n.severity):
            say.found("saw", ui.plain_finding(note.rule, note.message))

        last_pass = index >= settings.revision_rounds
        if last_pass or not critique.notes:
            round_.seconds = time.perf_counter() - round_started
            production.rounds.append(round_)
            break

        round_.revisions = critic.revise(
            edl,
            critique,
            by_id,
            target_duration=settings.target_duration,
            audio=music_analysis,
            music_offset=music_offset,
            beat_sync=brief.beat_sync,
        )
        round_.seconds = time.perf_counter() - round_started
        production.rounds.append(round_)

        if not round_.revisions:
            log.info("nothing worth re-cutting; keeping this pass")
            say.detail("left as it is — nothing worth re-cutting for")
            break
        for revision in round_.revisions:
            say.found("fixed", revision)
        log.info("re-cutting: %s", "; ".join(round_.revisions))
        space.clean_segments()

    production.seconds = time.perf_counter() - started

    edl.save(space.root / "edl.json")
    (space.root / "production-notes.md").write_text(production.report(), encoding="utf-8")
    (space.root / "analysis.json").write_text(
        json.dumps([dossier.to_json() for dossier in dossiers], indent=2), encoding="utf-8"
    )
    log.info("done in %.1fs — %s", production.seconds, production.primary)
    return production
