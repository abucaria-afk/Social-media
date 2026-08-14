"""The algorithmic editor.

This is a complete director that never calls a model. It reads the brief, the
beat grid and the clip dossiers, and cuts the film with the rules a working
editor internalises:

* shots get shorter as energy rises, and every cut lands on a beat
* never two consecutive shots from the same clip, and never the same shot size twice
* a shot that cannot fill its slot is slowed down rather than padded
* cuts by default; a transition has to earn its place
* the first two seconds are the hook and are chosen for impact, not for order
* exposure and white balance are matched across the cut so shots belong together

The model-backed director produces better *taste*; this produces reliable
*craft*, and it is the fallback whenever the model is unavailable or wrong.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, replace

import numpy as np

from ..analysis.audio import AudioAnalysis
from ..analysis.dossier import ClipDossier, Take
from ..config import Settings
from ..edl import EditDecisionList, Look, Motion, MusicCue, Ramp, Shot, SoundCue, TextCue, Transition
from ..ingest import MediaAsset
from .brief import Brief

log = logging.getLogger("auteur.director.heuristic")

MIN_SLOT = 0.32
MAX_SLOT = 5.0
#: Frame difference that counts as "fully energetic" when matching shots to the arc.
MOTION_FULL_SCALE = 0.12


@dataclass
class _Slot:
    index: int
    start: float
    end: float
    energy: float
    on_downbeat: bool = False

    @property
    def length(self) -> float:
        return self.end - self.start


#: Shot lengths an editor actually uses, counted in beats. Anything else reads
#: as a mistake rather than a choice.
MUSICAL_MULTIPLES = (1, 2, 4, 8)


def _build_slots(
    brief: Brief, target: float, audio: AudioAnalysis | None, offset: float, rng: random.Random
) -> list[_Slot]:
    """Lay out the rhythm of the film before choosing a single frame.

    With music, cuts are quantised to the beat grid — the single change that
    most separates an edit that feels intentional from one that feels arbitrary.
    But quantising to the *nearest* beat makes every shot one beat long, which
    is its own kind of monotony. So shot lengths are chosen from musical
    multiples — one beat, two, four — biased by the energy curve. That is how a
    section breathes while still landing on the grid.
    """
    beats: list[float] = []
    downbeats: set[float] = set()
    if audio is not None and audio.has_beat and brief.beat_sync:
        beats = [b - offset for b in audio.beats if b - offset > 0]
        downbeats = {round(b - offset, 3) for b in audio.downbeats if b - offset > 0}

    slots: list[_Slot] = []
    cursor = 0.0
    index = 0
    while cursor < target - MIN_SLOT:
        position = cursor / max(target, 1e-6)
        energy = brief.energy_at(position)
        want = float(np.clip(brief.shot_length_at(position), MIN_SLOT, MAX_SLOT))
        # A little deliberate imprecision, so the same energy does not always
        # resolve to the same shot length.
        want *= 0.86 + 0.28 * rng.random()

        end = cursor + want
        if beats:
            upcoming = [b for b in beats if b > cursor + MIN_SLOT * 0.9]
            if upcoming:
                best, best_cost = None, float("inf")
                for count, beat_time in enumerate(upcoming[:8], start=1):
                    length = beat_time - cursor
                    if length > MAX_SLOT:
                        break
                    cost = abs(length - want)
                    if count not in MUSICAL_MULTIPLES:
                        cost += 0.3
                    if cost < best_cost:
                        best, best_cost = beat_time, cost
                if best is not None:
                    end = best

        end = min(float(np.clip(end, cursor + MIN_SLOT, cursor + MAX_SLOT)), target)
        if target - end < MIN_SLOT:  # absorb a runt tail into the final shot
            end = target

        slots.append(
            _Slot(index=index, start=round(cursor, 4), end=round(end, 4), energy=energy,
                  on_downbeat=round(end, 3) in downbeats)
        )
        cursor = end
        index += 1
        if index > 400:  # pathological guard
            break

    return slots


def _take_pool(dossiers: list[ClipDossier]) -> list[tuple[ClipDossier, Take]]:
    pool: list[tuple[ClipDossier, Take]] = []
    for dossier in dossiers:
        for take in dossier.takes:
            if take.duration >= MIN_SLOT * 0.8:
                pool.append((dossier, take))
    return pool


def _scale_rank(scale: str) -> int:
    return {"wide": 0, "medium": 1, "close": 2}.get(scale, 1)


def _fit_score(
    take: Take,
    slot: _Slot,
    *,
    previous_take: Take | None,
    used_seconds: dict[str, float],
    used_ranges: dict[str, list[tuple[float, float]]],
    recent_clips: list[str],
    is_hook: bool,
) -> float:
    """How well this take serves this slot. Higher is better."""
    score = take.score * 1.0

    # Re-using frames already on the timeline is the fastest way to make a
    # montage look padded. The penalty escalates rather than saturating, so a
    # third pass over the same footage costs more than the second did.
    spent = 0.0
    for start, end in used_ranges.get(take.clip_id, ()):
        spent += max(0.0, min(take.end, end) - max(take.start, start))
    if take.duration > 0:
        score -= min(spent / take.duration, 3.0) * 1.1

    # Energy match: the arc wants a certain amount of movement right here.
    motion_norm = float(np.clip(take.motion / MOTION_FULL_SCALE, 0.0, 1.0))
    score += (1.0 - abs(motion_norm - slot.energy)) * 0.55

    # Coverage: the take should be able to fill the slot without absurd speeds.
    implied_speed = take.duration / max(slot.length, 1e-6)
    if implied_speed < 0.32:      # would need extreme slow motion
        score -= 0.45
    elif implied_speed > 3.2:     # plenty of material, no penalty, slight bonus
        score += 0.05

    # Variety: the same clip twice in a row reads as a mistake.
    if previous_take is not None:
        if take.clip_id == previous_take.clip_id:
            score -= 1.4
            # Overlapping the shot we just used is worse still.
            if take.start < previous_take.end and previous_take.start < take.end:
                score -= 1.2
        if take.scale == previous_take.scale:
            score -= 0.22
        else:
            # Wide → close is a stronger progression than a sideways step.
            score += 0.10 * min(abs(_scale_rank(take.scale) - _scale_rank(previous_take.scale)), 2)

        # Match cut: two shots moving the same way join invisibly.
        if take.camera == previous_take.camera and take.camera != "static":
            score += 0.18
        # Cutting from a locked-off shot to a moving one is a jolt — good at peaks.
        if previous_take.camera == "static" and take.camera != "static":
            score += 0.12 * slot.energy

    # Spread the film across the available footage rather than mining one clip.
    score -= min(used_seconds.get(take.clip_id, 0.0) * 0.09, 0.9)
    if take.clip_id in recent_clips[-3:]:
        score -= 0.35

    if is_hook:
        # The opening frame has one job: stop the scroll.
        score += take.sharpness * 0.5 + motion_norm * 0.6 + take.contrast * 0.4

    return score


def _pick_subrange(
    take: Take, needed: float, used_ranges: dict[str, list[tuple[float, float]]]
) -> Take:
    """Choose *which* seconds of a take to use, avoiding frames already spent.

    When footage is scarce a clip has to appear several times. Showing the same
    two seconds each time is what makes that read as padding; sliding along to
    the next unused stretch is what an editor does instead.
    """
    if take.duration <= needed + 0.05:
        return take

    spent = used_ranges.get(take.clip_id, ())
    span = take.duration - needed
    steps = 12

    best_start, best_cost = take.start, float("inf")
    for step in range(steps + 1):
        start = take.start + span * step / steps
        end = start + needed
        overlap = sum(max(0.0, min(end, e) - max(start, s)) for s, e in spent)
        # Mild bias toward later in the take: action usually resolves late.
        cost = overlap - 0.02 * (step / steps)
        if cost < best_cost:
            best_start, best_cost = start, cost

    return replace(take, start=round(best_start, 3), end=round(best_start + needed, 3))


def _choose_speed(take: Take, slot: _Slot, brief: Brief) -> tuple[float, Ramp]:
    """Pick the speed for a shot, and whether it ramps.

    Speed is source seconds per screen second. A short take is slowed to fill
    its slot; a long one is either trimmed or pushed faster when the arc is hot.
    """
    available = take.duration
    needed_at_1x = slot.length

    if available < needed_at_1x * 0.98:
        # Not enough footage: slow it down rather than hold a freeze.
        speed = float(np.clip(available / max(needed_at_1x, 1e-6), 0.3, 1.0))
    elif slot.energy > 0.72 and available > needed_at_1x * 1.6 and brief.ramps:
        speed = float(np.clip(1.0 + (slot.energy - 0.72) * 2.2, 1.0, 2.2))
    elif slot.energy < 0.35 and take.motion > 0.03 and brief.ramps:
        speed = 0.62  # let a beautiful, moving shot breathe
    else:
        speed = 1.0

    if not brief.ramps:
        return speed, Ramp.constant(speed)

    # Ramps are seasoning. Reserve them for beats that can carry one.
    if slot.on_downbeat and slot.energy > 0.6 and available > needed_at_1x * 1.5:
        return speed, Ramp.hit(slow=max(0.35, speed * 0.4), fast=min(2.6, speed * 1.7))
    if slot.energy > 0.82 and available > needed_at_1x * 1.4:
        return speed, Ramp.accelerate(from_speed=speed, to_speed=min(speed * 1.9, 3.0))
    if slot.energy < 0.3 and available > needed_at_1x:
        return speed, Ramp.slow_in(from_speed=max(0.4, speed * 0.7), to_speed=speed)
    return speed, Ramp.constant(speed)


def _choose_motion(take: Take, slot: _Slot, is_still: bool, rng: random.Random) -> Motion:
    """Give static frames a reason to be on screen."""
    anchor = (float(np.clip(take.subject[0], 0.12, 0.88)), float(np.clip(take.subject[1], 0.12, 0.88)))

    if is_still:
        kind = "ken-burns" if slot.energy < 0.6 else "punch-in"
        return Motion(kind=kind, intensity=0.35 + 0.3 * slot.energy, anchor=anchor)

    if take.camera == "static" and take.motion < 0.02:
        # A locked-off shot with nothing moving in it needs help.
        kind = rng.choice(["punch-in", "ken-burns", "drift-left", "drift-right"])
        return Motion(kind=kind, intensity=0.22 + 0.25 * slot.energy, anchor=anchor)

    if slot.energy > 0.8 and take.camera in ("static", "handheld"):
        return Motion(kind="punch-in", intensity=0.18, anchor=anchor)

    # The footage already moves. Adding more just fights it.
    return Motion(kind="none", intensity=0.0, anchor=anchor)


def _choose_transition(
    take: Take, previous_take: Take | None, slot: _Slot, brief: Brief, rng: random.Random,
    act_break: bool,
) -> Transition:
    """Cuts are the default. Everything else must be motivated."""
    if previous_take is None:
        return Transition("cut", 0.0)
    if brief.transitions == ("cut",):
        return Transition("cut", 0.0)

    if act_break and slot.energy < 0.42:
        return Transition("dip-to-black", min(0.45, slot.length * 0.4))

    # A whip pan only works when both shots are actually whipping.
    if (
        previous_take.camera.startswith("pan")
        and take.camera.startswith("pan")
        and previous_take.camera == take.camera
        and slot.energy > 0.55
    ):
        direction = "whip-left" if take.camera == "pan-left" else "whip-right"
        if direction in brief.transitions:
            return Transition(direction, min(0.22, slot.length * 0.35))

    if (
        previous_take.camera in ("push-in", "pull-out")
        and slot.energy > 0.8
        and "zoom-blur" in brief.transitions
        and rng.random() < 0.35  # motivated, but not every single time
    ):
        return Transition("zoom-blur", min(0.2, slot.length * 0.3))

    # Otherwise sample the brief's vocabulary, which is mostly the word "cut".
    kind = rng.choice(list(brief.transitions))
    if kind == "cut":
        return Transition("cut", 0.0)

    ceiling = min(slot.length * 0.4, 0.6)
    if ceiling < 0.1:
        return Transition("cut", 0.0)
    duration = min(ceiling, 0.5 if kind in ("dissolve", "film-burn", "light-leak") else 0.25)
    return Transition(kind, duration)


def _match_looks(shots: list[Shot], dossiers: dict[str, ClipDossier], preset: str, strength: float) -> None:
    """Shot matching: nudge every shot toward a common exposure and white balance.

    Without this, footage from different clips reads as a collage. With it, the
    same clips read as one photographed piece. It is the least visible and most
    important part of a grade.
    """
    if not shots:
        return

    samples = []
    for shot in shots:
        dossier = dossiers.get(shot.clip_id)
        if dossier is None:
            continue
        luma = float(np.mean(dossier.video.luma)) if len(dossier.video.luma) else 0.5
        samples.append((shot, luma, dossier.video.warmth, dossier.video.saturation))

    if not samples:
        return

    target_luma = float(np.median([s[1] for s in samples]))
    target_warmth = float(np.median([s[2] for s in samples]))
    target_saturation = float(np.median([s[3] for s in samples]))

    for shot, luma, warmth, saturation in samples:
        # Correct part of the way, not all of it: full correction flattens the film.
        shot.look = Look(
            preset=preset,
            exposure=float(np.clip((target_luma - luma) * 1.5, -0.5, 0.5)),
            temperature=float(np.clip((target_warmth - warmth) * 0.6, -0.4, 0.4)),
            saturation=float(np.clip((target_saturation - saturation) * 0.5, -0.3, 0.3)),
            contrast=0.0,
            strength=strength,
        )


def _music_offset(audio: AudioAnalysis | None, duration: float) -> float:
    """Start the film at the strongest downbeat the track can offer.

    Music rarely opens at its best moment. Finding the drop and starting there
    is free production value.
    """
    if audio is None or audio.silent or not len(audio.envelope):
        return 0.0
    grid = audio.downbeats or audio.beats
    if not grid:
        return 0.0

    best_offset, best_energy = 0.0, -1.0
    for candidate in grid:
        if candidate + duration > audio.duration + 0.5:
            continue
        energy = audio.energy_over(candidate, min(candidate + duration, audio.duration))
        # Prefer an early strong section over a late one of equal power.
        energy -= candidate / max(audio.duration, 1e-6) * 0.08
        if energy > best_energy:
            best_offset, best_energy = float(candidate), energy
    return best_offset


def _place_texts(brief: Brief, duration: float, look_accent: str) -> list[TextCue]:
    """Title, mid-cards and end card, spaced so they never collide with each other."""
    lines = brief.on_screen_text
    if not lines:
        return []

    cues: list[TextCue] = []
    first, *rest = lines

    cues.append(
        TextCue(text=first, start=0.35, duration=min(2.4, duration * 0.28), style="title",
                anchor=(0.5, 0.46), size=1.0, color="#FFFFFF", accent=look_accent)
    )

    if rest:
        end_card = rest[-1] if len(rest) > 1 or duration > 12 else None
        middles = rest[:-1] if end_card else rest

        if middles:
            span_start = duration * 0.34
            span_end = duration * 0.78
            step = (span_end - span_start) / max(len(middles), 1)
            for index, line in enumerate(middles):
                cues.append(
                    TextCue(text=line, start=round(span_start + index * step, 2),
                            duration=min(2.0, step * 0.8), style="kinetic",
                            anchor=(0.5, 0.8), size=0.8, color="#FFFFFF",
                            accent=look_accent, per_word=True)
                )

        if end_card:
            cues.append(
                TextCue(text=end_card, start=round(max(duration - 2.6, duration * 0.8), 2),
                        duration=2.4, style="end-card", anchor=(0.5, 0.5), size=1.1,
                        color="#FFFFFF", accent=look_accent)
            )
    return cues


def _design_sound(edl: EditDecisionList, slots: list[_Slot], brief: Brief) -> list[SoundCue]:
    """Whooshes on the whips, impacts on the hard cuts, a riser into the climax."""
    cues: list[SoundCue] = []
    timeline = edl.timeline()

    for (start, _, shot), slot in zip(timeline, slots):
        kind = shot.transition_in.kind
        if kind in ("whip-left", "whip-right", "whip-up", "whip-down", "zoom-blur", "slide-left", "slide-right"):
            cues.append(SoundCue("whoosh", at=round(max(0.0, start - 0.12), 3), gain=0.5, duration=0.45))
        elif kind == "glitch":
            cues.append(SoundCue("tick", at=round(start, 3), gain=0.4, duration=0.18))
        elif slot.on_downbeat and slot.energy > 0.75 and start > 0.5:
            cues.append(SoundCue("impact", at=round(start, 3), gain=0.42, duration=0.7))

    duration = edl.duration
    if duration > 6 and brief.arc in ("hook-drop", "crescendo", "trailer"):
        climax = duration * (0.62 if brief.arc == "trailer" else 0.74)
        cues.append(SoundCue("riser", at=round(max(0.0, climax - 1.6), 3), gain=0.34, duration=1.6))
        cues.append(SoundCue("sub-drop", at=round(climax, 3), gain=0.5, duration=1.1))

    # Two effects on top of each other is mud; keep the louder one.
    cues.sort(key=lambda c: (c.at, -c.gain))
    spaced: list[SoundCue] = []
    for cue in cues:
        if spaced and cue.at - spaced[-1].at < 0.14 and cue.kind == spaced[-1].kind:
            continue
        spaced.append(cue)
    return spaced[:40]


def cut(
    brief: Brief,
    dossiers: list[ClipDossier],
    settings: Settings,
    *,
    music: MediaAsset | None = None,
    music_analysis: AudioAnalysis | None = None,
) -> EditDecisionList:
    """Cut the film. Deterministic for a given seed, brief and footage."""
    if not dossiers:
        raise ValueError("nothing to cut: no clips were analysed")

    rng = random.Random(settings.seed)
    by_id = {dossier.clip_id: dossier for dossier in dossiers}

    available = sum(dossier.duration for dossier in dossiers)
    target = brief.duration or settings.target_duration
    # Never promise more film than the footage can honestly supply.
    target = float(np.clip(target, 3.0, max(4.0, available * 1.6)))

    offset = _music_offset(music_analysis, target)
    slots = _build_slots(brief, target, music_analysis, offset, rng)
    if not slots:
        slots = [_Slot(0, 0.0, target, 0.6)]

    pool = _take_pool(dossiers)
    if not pool:
        raise ValueError("nothing to cut: no usable takes were found in the footage")

    # Energy troughs are act breaks; they are where a transition is allowed.
    energies = [slot.energy for slot in slots]
    act_breaks = {
        index for index in range(1, len(slots) - 1)
        if energies[index] < energies[index - 1] and energies[index] <= energies[index + 1]
    }

    shots: list[Shot] = []
    used_seconds: dict[str, float] = {}
    used_ranges: dict[str, list[tuple[float, float]]] = {}
    recent_clips: list[str] = []
    previous_take: Take | None = None

    for slot in slots:
        best: tuple[float, ClipDossier, Take] | None = None
        for dossier, take in pool:
            score = _fit_score(
                take, slot, previous_take=previous_take, used_seconds=used_seconds,
                used_ranges=used_ranges, recent_clips=recent_clips, is_hook=slot.index == 0,
            )
            if best is None or score > best[0]:
                best = (score, dossier, take)

        assert best is not None
        _, dossier, take = best

        speed, ramp = _choose_speed(take, slot, brief)
        source_needed = min(slot.length * speed, take.duration)
        chosen = _pick_subrange(take, source_needed, used_ranges)

        # Re-derive the ramp against what we actually took, so screen time lands
        # on the slot exactly and the next cut stays on its beat.
        actual_speed = chosen.duration / max(slot.length, 1e-6)
        if ramp.is_flat:
            ramp = Ramp.constant(actual_speed)
        else:
            # Screen time is source_duration x mean(1/speed). Scaling every
            # control point by k divides that by k, so k = actual_speed x mean(1/speed)
            # makes the curve span the slot while keeping its shape.
            mean_reciprocal = ramp.output_duration(1.0)
            factor = actual_speed * mean_reciprocal
            ramp = Ramp([(position, speed * factor) for position, speed in ramp.points])

        shot = Shot(
            clip_id=chosen.clip_id,
            source=dossier.asset.path,
            start=chosen.start,
            end=chosen.end,
            ramp=ramp,
            motion=_choose_motion(chosen, slot, dossier.asset.kind == "image", rng),
            reframe="subject",
            transition_in=_choose_transition(
                chosen, previous_take, slot, brief, rng, act_break=slot.index in act_breaks
            ),
            use_source_audio=brief.keep_source_audio and not dossier.audio.silent,
            audio_gain=1.0 if brief.keep_source_audio else 0.0,
            is_still=dossier.asset.kind == "image",
            note=f"{chosen.scale}/{chosen.camera} @e{slot.energy:.2f}",
        )
        shots.append(shot)

        used_seconds[chosen.clip_id] = used_seconds.get(chosen.clip_id, 0.0) + chosen.duration
        used_ranges.setdefault(chosen.clip_id, []).append((chosen.start, chosen.end))
        recent_clips.append(chosen.clip_id)
        previous_take = chosen

    edl = EditDecisionList(
        title=brief.title,
        shots=shots,
        look=Look(preset=brief.look, strength=brief.look_strength),
        texture=brief.texture,
        letterbox=brief.letterbox,
        fps=settings.quality.fps,
        width=settings.primary_format.width,
        height=settings.primary_format.height,
        rationale=(
            f"{brief.style} on a {brief.arc} arc; "
            f"{len(shots)} shots averaging {target / max(len(shots), 1):.2f}s"
            + (
                f", cut to {music_analysis.tempo:.0f} BPM"
                if music_analysis is not None and music_analysis.has_beat and brief.beat_sync
                else ", cut to the arc (no beat grid available)"
            )
        ),
    )

    _match_looks(edl.shots, by_id, brief.look, brief.look_strength)

    if music is not None:
        edl.music = MusicCue(
            source=music.path, offset=offset,
            gain=0.55 if brief.keep_source_audio else 0.85,
            duck=brief.keep_source_audio,
        )

    edl.texts = _place_texts(brief, edl.duration, look_accent="#FFFFFF")
    edl.sfx = _design_sound(edl, slots, brief)

    notes = edl.repair(by_id, target_duration=target)
    for note in notes:
        log.debug("edl repair: %s", note)
    return edl
