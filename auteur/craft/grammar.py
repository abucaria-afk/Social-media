"""Film grammar: the rules that get applied to an edit *after* it is written.

The director — heuristic or model — decides intent. These passes enforce craft
on whatever intent came back, which matters most when the EDL came from a
language model: models have taste but no clock, and they will happily write
four consecutive shots from the same clip, or cuts that miss every beat.

Every function here takes an EDL and mutates it in place, and every one of them
is safe to run twice.
"""

from __future__ import annotations

import logging

from ..analysis.audio import AudioAnalysis
from ..edl import MIN_SHOT, EditDecisionList, Ramp, Shot, Transition

log = logging.getLogger("auteur.craft.grammar")


def _rescale_ramp(shot: Shot, new_duration: float) -> bool:
    """Change a shot's screen time by adjusting speed, not by re-trimming.

    Keeps the chosen frames — the director picked those — and changes only how
    long they take to play.
    """
    new_duration = max(new_duration, MIN_SHOT)
    source = shot.source_duration
    if source <= 0:
        return False

    if shot.ramp.is_flat:
        speed = source / new_duration
        if not 0.2 <= speed <= 6.0:
            return False
        shot.ramp = Ramp.constant(speed)
        return True

    current = shot.duration
    if current <= 0:
        return False
    factor = current / new_duration
    points = [(position, speed * factor) for position, speed in shot.ramp.points]
    if any(not 0.15 <= speed <= 8.0 for _, speed in points):
        return False
    shot.ramp = Ramp(points).normalise()
    return True


def snap_cuts_to_beats(
    edl: EditDecisionList,
    audio: AudioAnalysis | None,
    *,
    offset: float = 0.0,
    tolerance: float = 0.22,
) -> int:
    """Nudge every cut onto the nearest beat.

    Works forward along the timeline so each correction is measured from the
    already-corrected cursor — otherwise small errors accumulate and the back
    half of the film drifts off the grid.
    """
    if audio is None or not audio.has_beat:
        return 0

    beats = [beat - offset for beat in audio.beats if beat - offset > 0]
    if len(beats) < 2:
        return 0

    snapped = 0
    cursor = 0.0
    for index, shot in enumerate(edl.shots):
        if index > 0 and not shot.transition_in.is_cut:
            cursor -= shot.transition_in.duration

        end = cursor + shot.duration
        nearest = min(beats, key=lambda beat: abs(beat - end))
        delta = nearest - end

        if 0 < abs(delta) <= tolerance and nearest - cursor >= MIN_SHOT:
            if _rescale_ramp(shot, nearest - cursor):
                snapped += 1
                end = cursor + shot.duration

        cursor = end

    if snapped:
        log.debug("snapped %d/%d cuts to the beat grid", snapped, len(edl.shots))
    return snapped


def vary_pacing(edl: EditDecisionList, *, run_length: int = 4, spread: float = 0.16) -> int:
    """Break up metronomic cutting.

    A run of identically-timed shots reads as a slideshow no matter how good the
    footage is. Alternate shots in such a run are lengthened and shortened
    slightly, which keeps the section's total length but restores a pulse.
    """
    if len(edl.shots) < run_length:
        return 0

    changed = 0
    index = 0
    while index < len(edl.shots):
        run = [index]
        base = edl.shots[index].duration
        cursor = index + 1
        while cursor < len(edl.shots) and abs(edl.shots[cursor].duration - base) < 0.06:
            run.append(cursor)
            cursor += 1

        if len(run) >= run_length:
            for offset, shot_index in enumerate(run):
                shot = edl.shots[shot_index]
                factor = 1.0 + (spread if offset % 2 else -spread)
                if _rescale_ramp(shot, shot.duration * factor):
                    changed += 1
        index = max(cursor, index + 1)

    return changed


def enforce_variety(edl: EditDecisionList, *, lookback: int = 2) -> int:
    """Never cut from a clip back to itself.

    Repairs by swapping the offending shot with the nearest later shot that
    breaks the run, which preserves every chosen frame and every duration.
    """
    fixed = 0
    for index in range(1, len(edl.shots)):
        window = [shot.clip_id for shot in edl.shots[max(0, index - lookback) : index]]
        if edl.shots[index].clip_id not in window:
            continue

        for candidate in range(index + 1, len(edl.shots)):
            if edl.shots[candidate].clip_id in window:
                continue
            # The swap must not create a new repeat where the candidate lands.
            after = edl.shots[index].clip_id
            neighbours = [
                edl.shots[position].clip_id
                for position in (candidate - 1, candidate + 1)
                if 0 <= position < len(edl.shots) and position != index
            ]
            if after in neighbours:
                continue
            edl.shots[index], edl.shots[candidate] = edl.shots[candidate], edl.shots[index]
            fixed += 1
            break

    if fixed:
        # Transitions belong to positions on the timeline, not to the shots that
        # were swapped through them; the opening must still be a hard cut.
        edl.shots[0].transition_in = Transition("cut", 0.0)
    return fixed


def ensure_hook(edl: EditDecisionList, *, max_hook: float = 1.4) -> bool:
    """Guarantee the film opens on a cut, fast.

    Nothing dissolves into the first frame, and an opening shot that outstays
    1.4 seconds has already lost a scrolling audience.
    """
    if not edl.shots:
        return False

    changed = False
    first = edl.shots[0]
    if not first.transition_in.is_cut:
        first.transition_in = Transition("cut", 0.0)
        changed = True
    if first.duration > max_hook and _rescale_ramp(first, max_hook):
        changed = True
    return changed


def apply_j_l_cuts(edl: EditDecisionList, *, amount: float = 0.24) -> int:
    """Let sound cross the picture cut.

    A J cut brings the next scene's audio in early; an L cut holds the previous
    scene's audio over the new picture. Alternating them through a dialogue
    sequence is what makes an edit stop feeling like a series of blocks. Only
    meaningful for shots carrying their own sound.
    """
    speaking = [shot for shot in edl.shots if shot.use_source_audio and shot.audio_gain > 0.01]
    if len(speaking) < 2:
        return 0

    applied = 0
    for order, shot in enumerate(speaking):
        if order == 0:
            continue
        # Alternate: audio leads the picture, then trails it.
        shot.audio_offset = -amount if order % 2 else amount * 0.6
        applied += 1
    return applied


def limit_transition_density(edl: EditDecisionList, *, max_fraction: float = 0.2) -> int:
    """Turn surplus transitions back into cuts.

    An edit where everything dissolves has no punctuation left. The weakest
    transitions — the ones on the shortest shots, where they read as mush — are
    demoted first.
    """
    if not edl.shots:
        return 0
    # Nothing precedes the first shot, so a transition there is meaningless
    # however many the film is allowed.
    if not edl.shots[0].transition_in.is_cut:
        edl.shots[0].transition_in = Transition("cut", 0.0)

    fancy = [
        (index, shot) for index, shot in enumerate(edl.shots)
        if index > 0 and not shot.transition_in.is_cut
    ]
    allowance = int(len(edl.shots) * max_fraction)
    if len(fancy) <= allowance:
        return 0

    fancy.sort(key=lambda item: item[1].duration)
    demoted = 0
    for _, shot in fancy[: len(fancy) - allowance]:
        shot.transition_in = Transition("cut", 0.0)
        demoted += 1
    return demoted


def trim_to_duration(edl: EditDecisionList, target: float, *, tolerance: float = 1.0) -> bool:
    """Bring the film to length.

    Over-length: drop the weakest shots (shortest first, from the middle, never
    the hook or the closer) until it fits. Under-length: stretch the slowest
    shots slightly rather than repeating footage.
    """
    if target <= 0 or not edl.shots:
        return False

    changed = False
    guard = 0
    while edl.duration > target + tolerance and len(edl.shots) > 2 and guard < 200:
        guard += 1
        middle = edl.shots[1:-1]
        if not middle:
            break
        victim = min(middle, key=lambda shot: shot.duration)
        edl.shots.remove(victim)
        edl.shots[0].transition_in = Transition("cut", 0.0)
        changed = True

    if edl.duration < target - tolerance:
        deficit = target - edl.duration
        # Spread the shortfall over the slowest shots — they absorb it invisibly.
        candidates = sorted(edl.shots, key=lambda shot: -shot.duration)[: max(1, len(edl.shots) // 3)]
        share = deficit / len(candidates)
        for shot in candidates:
            if _rescale_ramp(shot, shot.duration + share):
                changed = True

    return changed


def polish(
    edl: EditDecisionList,
    *,
    audio: AudioAnalysis | None = None,
    music_offset: float = 0.0,
    target_duration: float | None = None,
    beat_sync: bool = True,
) -> dict[str, int]:
    """Run the full grammar pass. Returns what each rule changed."""
    on_grid = beat_sync and audio is not None and audio.has_beat
    report = {
        "variety": enforce_variety(edl),
        "transitions": limit_transition_density(edl),
        "hook": int(ensure_hook(edl)),
        # Pacing variation and beat snapping pull in opposite directions. When
        # there is a grid, the grid wins: variety comes from choosing one, two
        # or four beats per shot, not from nudging shots off the beat.
        "pacing": 0 if on_grid else vary_pacing(edl),
        "j_l_cuts": apply_j_l_cuts(edl),
    }
    if target_duration:
        report["length"] = int(trim_to_duration(edl, target_duration))
    # Snap last: every rule above changes durations, and the beat grid is what
    # the audience actually hears.
    report["beat_snap"] = snap_cuts_to_beats(edl, audio if beat_sync else None, offset=music_offset)
    return report
