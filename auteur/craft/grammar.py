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

#: The subdivisions of a beat a film is allowed to cut on.
#:
#: Every rhythm pass below used to assume a shot lasts at least one beat, which
#: is true of most films and false of exactly the ones this program is measured
#: against. A montage cut at 0.334s against a 120bpm track is cutting three to
#: the beat; a hypercut at 0.167s is cutting six. Both are on the grid — just
#: not on the beat — and code that can only see beats reads them as one-beat
#: shots and stretches them onto the beat, which is how a montage came out
#: three times slower than it was planned.
SUBDIVISIONS = (1, 2, 3, 4, 6, 8)


def beat_unit(edl: EditDecisionList, beat: float) -> float:
    """The grid this film is actually cut on, which may be finer than the beat.

    Taken from the film as a whole rather than per shot, so every pass shares
    one unit: a rhythm is a property of the edit, not of each cut in it.
    """
    if beat <= 0 or not edl.shots:
        return beat
    holds = sorted(shot.duration for shot in edl.shots)

    # The quarter-point, not the median. A film with a genuine two-to-one
    # rhythm — half its shots on the eighth, half on the beat — has a median
    # sitting on the *longer* of the two, so a median-based unit calls the beat
    # the grid and the rhythm it can see disappears: every length rounds to one
    # beat, and the critic reports a varied film as metronomic. The grid is set
    # by the finest cut the film makes habitually, which is what the quarter
    # point finds while still ignoring one or two stray flash frames.
    unit = holds[len(holds) // 4]
    if unit <= 0 or unit >= beat * 0.75:
        return beat
    return beat / min(SUBDIVISIONS, key=lambda n: abs(beat / n - unit))


def subdivide(beats: list[float], spacing: float, unit: float) -> list[float]:
    """Fill in the grid lines between beats, at `unit` apart.

    Each gap is divided by its own width rather than by a nominal tempo, so a
    track that drifts keeps its subdivisions where the beats actually are.
    """
    if unit <= 0 or spacing <= 0 or not beats:
        return sorted(beats)
    divisions = max(1, round(spacing / unit))
    if divisions == 1:
        return sorted(beats)

    grid: list[float] = []
    for index, beat in enumerate(beats):
        grid.append(beat)
        gap = (beats[index + 1] - beat) if index + 1 < len(beats) else spacing
        step = gap / divisions
        grid.extend(beat + step * n for n in range(1, divisions))

    # And backwards from the first beat. A film cut on eighths puts its opening
    # cut half a beat in, before any beat has been detected — filling only
    # forwards leaves that cut with no grid line to land on and marks the
    # opening of every fast film as off the beat.
    step = (beats[1] - beats[0]) / divisions if len(beats) > 1 else spacing / divisions
    grid.extend(beats[0] - step * n for n in range(1, divisions))

    return sorted(value for value in grid if value > 0)


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

    # A film cut faster than the beat has no cut that can land on one, so
    # snapping it to beats means either leaving it alone or doubling it. Give
    # it the grid it is actually cut on: the beats, subdivided.
    spacing = 60.0 / audio.tempo if audio.tempo > 0 else (beats[1] - beats[0])
    unit = beat_unit(edl, spacing)
    if 0 < unit < spacing * 0.75:
        beats = subdivide(beats, spacing, unit)
        # And a cut may only be nudged by a fraction of its own unit. The
        # blanket 0.22s is most of a 0.25s shot.
        tolerance = min(tolerance, unit * 0.45)

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
                # Shortening a shot into a flash frame trades one fault for a
                # worse one, so the floor wins over the variation.
                if shot.duration * factor < MIN_SHOT * 1.6:
                    continue
                if _rescale_ramp(shot, shot.duration * factor):
                    changed += 1
        index = max(cursor, index + 1)

    return changed


def vary_beat_multiples(edl: EditDecisionList, beat: float, *, every: int = 3) -> int:
    """Break up one-beat-per-shot cutting *without leaving the grid*.

    Nudging shot lengths by a percentage is the wrong fix for a film cut to
    music: the beat snap that follows simply pulls them back, so the edit ends
    up unchanged while the log claims otherwise. Holding every third shot for a
    whole extra beat varies the rhythm and stays exactly on the grid.
    """
    if beat <= 0 or len(edl.shots) < every + 2:
        return 0

    # Vary in the unit the film is cut in, not in beats. `round(0.25 / 0.5)` is
    # zero, so a quarter-second shot on a 120bpm track used to be read as a
    # one-beat shot and stretched to two beats — a 0.25s cut became 1.0s, and
    # doing that to every third shot dragged the whole film with it.
    unit = beat_unit(edl, beat)
    if unit <= 0:
        return 0

    # Two lengths is not a rhythm the critic will accept, and it is right not
    # to: measured across the twenty-three reference reels, the median reel
    # uses *five* distinct multiples of its own unit, and only three of them
    # get by on two. Holding every third shot for double left the film with
    # exactly {1, 2} — one short of the three the critic asks for — so the
    # complaint could never be cleared no matter how many passes ran. A longer
    # hold every third time round gives the phrase somewhere to land.
    changed = 0
    for index in range(every, len(edl.shots), every):
        shot = edl.shots[index]
        units = max(1, round(shot.duration / unit))
        if units > 1:
            continue  # this one already breaks the pattern
        want = 4 if index % (every * 3) == 0 else 2
        if _rescale_ramp(shot, unit * want):
            changed += 1
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
            # A transition describes how a *position* on the timeline is
            # entered — it was chosen for the shots either side of it. Swapping
            # the shots must leave the joins where they were.
            here, there = edl.shots[index].transition_in, edl.shots[candidate].transition_in
            edl.shots[index], edl.shots[candidate] = edl.shots[candidate], edl.shots[index]
            edl.shots[index].transition_in, edl.shots[candidate].transition_in = here, there
            fixed += 1
            break

    if fixed:
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
        (index, shot)
        for index, shot in enumerate(edl.shots)
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
        candidates = sorted(edl.shots, key=lambda shot: -shot.duration)[
            : max(1, len(edl.shots) // 3)
        ]
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
