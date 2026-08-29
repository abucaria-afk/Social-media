"""Watching the cut back.

An editor's real work starts on the second viewing. This module plays the
finished file back through the same analysis used on the rushes and looks for
the faults that only appear once everything is assembled: a stretch where
nothing happens, a cut that flashes past, a jump in exposure across a join, a
film that drifts off the beat or off its runtime.

Findings become revisions to the EDL, and the film is cut again. That loop —
render, watch, fix, re-render — is what makes this an agent rather than a
script.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import ffmpeg
from .analysis.audio import AudioAnalysis
from .analysis.dossier import ClipDossier
from .craft import grammar
from .edl import MIN_SHOT, EditDecisionList, Transition

log = logging.getLogger("auteur.critic")

#: Below this mean frame difference, nothing is happening on screen.
DEAD_AIR_MOTION = 0.006
DEAD_AIR_SECONDS = 1.6
#: Luma jump across a cut that reads as a mistake rather than a choice.
EXPOSURE_JUMP = 0.30

#: What one unit of severity costs the score.
#:
#: Named rather than left inline because the number a person actually reads is
#: derived from it — `auteur demo` prints "it rates its own work 100%" — and an
#: unnamed multiplier in the middle of a formula is a scale nobody can reason
#: about. Two consequences worth knowing before changing it:
#:
#: * Severities run 0.45 to 0.75 for the ordinary faults, so it takes roughly
#:   six of them stacked to reach zero. A film with three real faults scores
#:   about 0.66.
#: * A frozen grey rectangle at twice its target runtime — the worst film this
#:   program can produce — measures **0.66**, not something near zero. That is
#:   a compressed scale, and whether it should be is a product decision rather
#:   than a bug: the loop in `revise` reads the notes, not the number, so
#:   rescaling would change what a person is told without changing what the
#:   program does. `test_the_critic_can_actually_fail_a_film` pins the
#:   discrimination that matters — bad scores materially below good — without
#:   pinning the curve.
#:
#: The one score not computed from this is zero, set directly when the render
#: has no decodable video, because that is not a fault to weigh against others.
PENALTY_PER_SEVERITY = 0.18


@dataclass
class Note:
    """One fault found on playback."""

    rule: str
    message: str
    severity: float  # 0..1, how much it costs the film
    at: float | None = None

    def __str__(self) -> str:
        where = f" @{self.at:.2f}s" if self.at is not None else ""
        return f"[{self.rule}{where}] {self.message}"


@dataclass
class Critique:
    score: float = 1.0
    notes: list[Note] = field(default_factory=list)
    measured: dict[str, float] = field(default_factory=dict)

    def describe(self) -> str:
        lines = [f"critic score {self.score:.2f}"]
        for note in sorted(self.notes, key=lambda n: -n.severity):
            lines.append(f"  · {note}")
        if not self.notes:
            lines.append("  · nothing to fix")
        return "\n".join(lines)


def _dead_air(motion: np.ndarray, fps: float) -> list[tuple[float, float]]:
    """Runs where the picture is effectively frozen."""
    if not len(motion):
        return []
    quiet = motion < DEAD_AIR_MOTION
    spans: list[tuple[float, float]] = []
    start: int | None = None
    for index, is_quiet in enumerate(quiet):
        if is_quiet and start is None:
            start = index
        elif not is_quiet and start is not None:
            if (index - start) / fps >= DEAD_AIR_SECONDS:
                spans.append((start / fps, index / fps))
            start = None
    if start is not None and (len(quiet) - start) / fps >= DEAD_AIR_SECONDS:
        spans.append((start / fps, len(quiet) / fps))
    return spans


def review(
    edl: EditDecisionList,
    output: Path,
    *,
    target_duration: float,
    audio: AudioAnalysis | None = None,
    music_offset: float = 0.0,
) -> Critique:
    """Play the finished film back and write down what is wrong with it."""
    critique = Critique()
    sample_fps = 8.0

    stream = ffmpeg.read_frames(output, width=128, fps=sample_fps)
    if len(stream) == 0:
        critique.notes.append(Note("unreadable", "the rendered file has no decodable video", 1.0))
        critique.score = 0.0
        return critique

    frames = stream.frames.astype(np.float32) / 255.0
    luma = frames.mean(axis=(1, 2))
    motion = np.concatenate([[0.0], np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))])

    runtime = len(frames) / sample_fps
    critique.measured["runtime"] = runtime
    critique.measured["mean_motion"] = float(motion.mean())
    critique.measured["mean_luma"] = float(luma.mean())

    # ---- runtime ---------------------------------------------------------
    drift = runtime - target_duration
    if abs(drift) > max(1.5, target_duration * 0.12):
        critique.notes.append(
            Note(
                "runtime",
                f"{runtime:.1f}s against a {target_duration:.1f}s target",
                severity=min(abs(drift) / max(target_duration, 1e-6), 1.0) * 0.7,
                at=None,
            )
        )

    # ---- dead air --------------------------------------------------------
    for start, end in _dead_air(motion, sample_fps):
        critique.notes.append(
            Note("dead-air", f"{end - start:.1f}s where nothing moves", severity=0.75, at=start)
        )

    # ---- flash frames ----------------------------------------------------
    for index, (start, _, shot) in enumerate(edl.timeline(), start=1):
        # `< MIN_SHOT`, not `< MIN_SHOT * 1.2`. The 1.2 was slack around a
        # floor nobody had measured — MIN_SHOT was 0.083, two frames. It is now
        # 0.125, the fastest hold anywhere in the twenty-three reference reels,
        # so a shot at the floor is by definition something the references do
        # and flagging it says the corpus is full of flash frames.
        if shot.duration < MIN_SHOT:
            critique.notes.append(
                Note(
                    "flash-frame",
                    f"shot {index} is only {shot.duration:.2f}s",
                    severity=0.5,
                    at=start,
                )
            )

    # ---- exposure continuity across cuts ---------------------------------
    cuts = edl.cut_times()
    jumps = 0
    for cut in cuts:
        before = int((cut - 0.12) * sample_fps)
        after = int((cut + 0.12) * sample_fps)
        if 0 <= before < len(luma) and 0 <= after < len(luma):
            delta = abs(float(luma[after] - luma[before]))
            if delta > EXPOSURE_JUMP:
                jumps += 1
    if jumps:
        critique.measured["exposure_jumps"] = float(jumps)
        critique.notes.append(
            Note(
                "exposure",
                f"{jumps} cut(s) jump hard in brightness",
                severity=min(jumps / max(len(cuts), 1), 1.0) * 0.5,
            )
        )

    # ---- rhythm ----------------------------------------------------------
    # Measured against the beat when there is one. A film cut to 120 BPM has
    # very few distinct shot lengths *in seconds* by design; what matters is
    # whether it varies between one, two and four beats, or hammers the same
    # one all the way through.
    lengths = [round(shot.duration, 2) for shot in edl.shots]
    if len(lengths) >= 5:
        variety = len(set(lengths)) / len(lengths)
        critique.measured["length_variety"] = variety

        if audio is not None and audio.has_beat and audio.tempo > 0:
            # In the film's own unit, which for anything cut faster than the
            # beat is a subdivision of it. Counting whole beats collapsed a
            # montage alternating 0.25s and 0.5s — a two-to-one rhythm, plainly
            # audible — into "every shot is 1 beat long", so the critic went on
            # calling a varied film metronomic no matter what the fix did.
            beat = grammar.beat_unit(edl, 60.0 / audio.tempo)
            multiples = {max(1, int(round(length / beat))) for length in lengths}
            critique.measured["beat_multiples"] = float(len(multiples))
            # With only a handful of shots there is no room for a
            # two- or four-beat shot without gutting the cut, so uniformity is
            # a consequence of the length, not a fault.
            metronomic = len(multiples) < 3 and len(lengths) >= 8
            detail = (
                f"every shot is {multiples.pop()} beat(s) long"
                if len(multiples) == 1
                else f"only {len(multiples)} distinct shot lengths in beats"
            )
        else:
            metronomic = variety < 0.25
            detail = f"only {len(set(lengths))} distinct shot lengths across {len(lengths)} shots"

        if metronomic:
            critique.notes.append(Note("metronomic", detail, severity=0.45))

    # ---- beat accuracy ---------------------------------------------------
    if audio is not None and audio.has_beat and cuts:
        lines = [b - music_offset for b in audio.beats if b - music_offset > 0]
        if audio.tempo > 0 and lines:
            # A montage cut on eighths lands half its cuts between beats *by
            # design*. Measured against whole beats it can never score above
            # 50%, so the note fired on every pass of every fast film and no
            # fix could clear it. Measure it against the grid it is cut on.
            spacing = 60.0 / audio.tempo
            unit = grammar.beat_unit(edl, spacing)
            if 0 < unit < spacing * 0.75:
                lines = grammar.subdivide(lines, spacing, unit)
        grid = np.asarray(lines)
        if len(grid):
            errors = [float(np.min(np.abs(grid - cut))) for cut in cuts]
            on_beat = sum(1 for error in errors if error < 0.09) / len(errors)
            critique.measured["cuts_on_beat"] = on_beat
            if on_beat < 0.55:
                critique.notes.append(
                    Note(
                        "off-beat",
                        f"only {on_beat * 100:.0f}% of cuts land on the beat",
                        severity=0.55,
                    )
                )

    # ---- the hook --------------------------------------------------------
    hook_frames = int(1.5 * sample_fps)
    if len(motion) > hook_frames * 2:
        hook_motion = float(motion[1:hook_frames].mean())
        rest_motion = float(motion[hook_frames:].mean())
        critique.measured["hook_motion"] = hook_motion
        if rest_motion > 1e-6 and hook_motion < rest_motion * 0.6:
            critique.notes.append(
                Note(
                    "weak-hook",
                    "the opening is quieter than the rest of the film",
                    severity=0.6,
                    at=0.0,
                )
            )

    # ---- black frames ----------------------------------------------------
    black = luma < 0.02
    if black.mean() > 0.05:
        critique.notes.append(
            Note("black", f"{black.mean() * 100:.0f}% of the film is near black", severity=0.6)
        )

    penalty = sum(note.severity for note in critique.notes)
    critique.score = float(np.clip(1.0 - penalty * PENALTY_PER_SEVERITY, 0.0, 1.0))
    return critique


# ---------------------------------------------------------------------------
# Acting on the critique
# ---------------------------------------------------------------------------


#: Shots the style agent lengthened on purpose. Imported by name rather than by
#: string so the two files cannot drift apart.
try:  # pragma: no cover - the agents package is optional at import time
    from .agents.preflight import HELD_ON_PURPOSE
except ImportError:  # pragma: no cover
    HELD_ON_PURPOSE = "held for the reference style"


def revise(
    edl: EditDecisionList,
    critique: Critique,
    dossiers: dict[str, ClipDossier],
    *,
    target_duration: float,
    audio: AudioAnalysis | None = None,
    music_offset: float = 0.0,
    beat_sync: bool = True,
) -> list[str]:
    """Apply fixes for what the critic found. Returns what was changed."""
    changes: list[str] = []
    signature_before = _length_signature(edl, audio)

    # Flash frames: give them enough screen time to register.
    for note in critique.notes:
        if note.rule != "flash-frame" or note.at is None:
            continue
        shot = next((s for start, _, s in edl.timeline() if abs(start - note.at) < 0.02), None)
        if shot is not None and grammar._rescale_ramp(shot, MIN_SHOT * 2.0):
            changes.append(f"lengthened the flash frame at {note.at:.1f}s")

    # Dead air: lift the offending shot out entirely rather than trying to
    # rescue it. There is no filter that makes nothing happening interesting.
    timeline = edl.timeline()
    dead = [note for note in critique.notes if note.rule == "dead-air" and note.at is not None]
    for note in dead:
        if len(edl.shots) <= 3:
            break
        victim = next((shot for start, end, shot in timeline if start <= note.at < end), None)
        if victim is not None and victim.note == HELD_ON_PURPOSE:
            # Somebody asked for this hold by pointing at footage that holds.
            # From the pixels it is indistinguishable from a frozen shot, and
            # the difference is entirely intent, which is not in the pixels.
            continue
        if victim is not None and victim in edl.shots:
            edl.shots.remove(victim)
            changes.append(f"dropped the frozen shot at {note.at:.1f}s ({victim.clip_id})")
            timeline = edl.timeline()

    if any(note.rule == "weak-hook" for note in critique.notes) and len(edl.shots) > 2:
        # Promote the liveliest shot in the first third to the front.
        window = edl.shots[: max(2, len(edl.shots) // 3)]
        best = max(
            window,
            key=lambda shot: (
                dossiers[shot.clip_id].video.slice_stats(shot.start, shot.end)["motion"]
                if shot.clip_id in dossiers
                else 0.0
            ),
        )
        if best is not edl.shots[0]:
            edl.shots.remove(best)
            edl.shots.insert(0, best)
            changes.append(f"opened on {best.clip_id} instead — it has more life in it")

    if any(note.rule == "metronomic" for note in critique.notes):
        before = _length_signature(edl, audio)
        if audio is not None and audio.has_beat and audio.tempo > 0:
            # Stay on the grid: hold some shots for two beats instead of one.
            grammar.vary_beat_multiples(edl, 60.0 / audio.tempo)
        else:
            grammar.vary_pacing(edl, run_length=3, spread=0.22)
        if _length_signature(edl, audio) != before:
            changes.append("varied the shot lengths so it stops feeling like a metronome")

    # Always, not only when the critic complained about runtime: holding shots
    # for two beats instead of one makes the film longer, so a fix for one fault
    # introduces another, and the critic only gets to see it on the pass after
    # the last one it can act on.
    if changes or any(note.rule == "runtime" for note in critique.notes):
        if grammar.trim_to_duration(edl, target_duration, tolerance=0.6):
            changes.append(f"brought the runtime back toward {target_duration:.0f}s")

    if changes:
        edl.shots[0].transition_in = Transition("cut", 0.0)
        grammar.enforce_variety(edl)
        grammar.snap_cuts_to_beats(edl, audio if beat_sync else None, offset=music_offset)
        edl.repair(dossiers, target_duration=target_duration)

    # Everything above ran before the beat snap and the repair, either of which
    # can undo a change. Report only what survived them.
    if audio is not None and _length_signature(edl, audio) == signature_before:
        changes = [c for c in changes if "shot lengths" not in c]

    return changes


def _length_signature(edl: EditDecisionList, audio: AudioAnalysis | None) -> tuple:
    """How the film's shot lengths look, in beats when there is a beat."""
    if audio is not None and audio.has_beat and audio.tempo > 0:
        beat = 60.0 / audio.tempo
        return tuple(max(1, round(shot.duration / beat)) for shot in edl.shots)
    return tuple(round(shot.duration, 2) for shot in edl.shots)
