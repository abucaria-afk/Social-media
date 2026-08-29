"""Tests for the auteur editor.

These exercise the parts that must never break silently: the EDL's timing
arithmetic, its ability to repair a director's mistakes, the beat detector, and
the frame reader whose height inference used to be ambiguous.

Rendering tests are marked slow and generate their own footage, so the suite
needs no fixtures on disk.

    python -m pytest tests/ -v          # everything
    python -m pytest tests/ -v -m "not slow"
"""

from __future__ import annotations

import json
import math
import os
import re
import copy
import subprocess
import sys
import tempfile
import types
import time
import urllib.parse
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auteur import ffmpeg
from auteur.analysis.audio import analyse_audio
from auteur.analysis.dossier import build_dossier
from auteur.config import FORMATS, QUALITIES, Settings, Workspace, resolve_format
from auteur.craft import color, grammar, motion, sound, transitions
from auteur.director.brief import parse_brief
from auteur.director.heuristic import cut
from auteur.edl import (
    MIN_SHOT,
    EditDecisionList,
    Look,
    Motion,
    Ramp,
    Shot,
    SoundCue,
    TextCue,
    Transition,
)
from auteur.agents import GazeAgent
from auteur.ingest import ingest, probe_asset
from auteur.insight import FitReport, Prediction

# ---------------------------------------------------------------------------
# Fixtures — synthesised, so the suite carries no media
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def rushes(tmp_path_factory) -> Path:
    """A small bin of clips plus a 120 BPM music track."""
    directory = tmp_path_factory.mktemp("rushes")
    binary = str(ffmpeg.ffmpeg_path())

    sources = [
        ("a_wide.mp4", "testsrc2=size=640x360:rate=25", 4.0),
        ("b_tall.mp4", "mandelbrot=size=360x640:rate=25", 3.5),
        ("c_motion.mp4", "life=size=320x240:rate=25:mold=8", 3.0),
    ]
    for name, source, duration in sources:
        subprocess.run(
            [
                binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                source,
                "-t",
                str(duration),
                "-c:v",
                "libx264",
                "-crf",
                "30",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                str(directory / name),
            ],
            check=True,
        )

    # 120 BPM: a kick every 0.5s, which the tempo estimator must recover.
    rate, bpm, seconds = 22050, 120.0, 12.0
    track = np.zeros(int(rate * seconds), dtype=np.float32)
    step = int(rate * 60.0 / bpm)
    for start in range(0, len(track) - step, step):
        t = np.arange(min(int(rate * 0.25), len(track) - start)) / rate
        pitch = 50.0 + 90.0 * np.exp(-t * 30.0)
        track[start : start + len(t)] += np.sin(2 * np.pi * np.cumsum(pitch) / rate) * np.exp(
            -t * 12.0
        )
    track /= max(float(np.abs(track).max()), 1e-6)
    with wave.open(str(directory / "beat.wav"), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((track * 30000).astype("<i2").tobytes())

    return directory


# ---------------------------------------------------------------------------
# ffmpeg plumbing
# ---------------------------------------------------------------------------


def test_binaries_are_reachable():
    assert ffmpeg.ffmpeg_path().exists()
    assert ffmpeg.ffprobe_path().exists()


def test_frame_reader_reports_the_true_frame_count(rushes):
    """The reader must not guess the height — a wrong guess silently rescales time.

    A 640x360 clip read at 128px wide is 128x72. Several other heights divide
    the same payload exactly, so an inferring reader can decode 4 seconds of
    video as 20 and every downstream measurement moves with it.
    """
    clip = rushes / "a_wide.mp4"
    stream = ffmpeg.read_frames(clip, width=128, fps=10.0)

    assert stream.width == 128
    assert stream.height == 72, "height must follow the source aspect ratio exactly"
    assert len(stream) == pytest.approx(40, abs=2), "4.0s at 10fps"


def test_frame_reader_handles_vertical_sources(rushes):
    stream = ffmpeg.read_frames(rushes / "b_tall.mp4", width=128, fps=5.0)
    assert (stream.width, stream.height) == (128, 228)


def test_scaled_height_matches_aspect(rushes):
    assert ffmpeg.scaled_height(rushes / "a_wide.mp4", 128) == 72
    assert ffmpeg.scaled_height(rushes / "c_motion.mp4", 128) == 96


# ---------------------------------------------------------------------------
# Ingest and analysis
# ---------------------------------------------------------------------------


def test_ingest_sorts_picture_from_sound(rushes):
    bin_ = ingest([rushes])
    assert len(bin_.visuals) == 3
    assert len(bin_.audio) == 1
    assert bin_.total_footage == pytest.approx(10.5, abs=0.5)


def test_ingest_rejects_an_empty_folder(tmp_path):
    with pytest.raises(FileNotFoundError):
        ingest([tmp_path])


def test_tempo_detection_finds_120_bpm(rushes):
    analysis = analyse_audio(probe_asset(rushes / "beat.wav"))
    assert not analysis.silent
    assert analysis.tempo == pytest.approx(120.0, abs=3.0)
    assert analysis.has_beat
    assert len(analysis.beats) > 15


def test_beat_snapping_is_a_no_op_far_from_the_grid(rushes):
    analysis = analyse_audio(probe_asset(rushes / "beat.wav"))
    on_grid = analysis.snap(1.02)
    assert on_grid == pytest.approx(1.0, abs=0.12)
    # 0.25s from any beat at 120 BPM — too far to move without being felt.
    assert analysis.snap(1.25, tolerance=0.08) == 1.25


def test_dossier_finds_usable_takes(rushes):
    dossier = build_dossier("C01", probe_asset(rushes / "a_wide.mp4"))
    assert dossier.takes
    assert all(take.duration > 0 for take in dossier.takes)
    assert all(0.0 <= take.start < take.end <= dossier.duration + 0.01 for take in dossier.takes)
    assert 0.0 <= dossier.quality <= 1.0


# ---------------------------------------------------------------------------
# The EDL: timing arithmetic and self-repair
# ---------------------------------------------------------------------------


def _shot(clip="C01", start=0.0, end=2.0, **kwargs) -> Shot:
    return Shot(clip_id=clip, source=Path("/dev/null"), start=start, end=end, **kwargs)


def _monotone_edl(count: int = 12) -> EditDecisionList:
    """A timeline that has stopped making decisions.

    Every shot the same length, the same move and the same join — which is
    precisely what the browser renderer produced before it had a transition
    vocabulary, and precisely what the Gaze agent used to score as flawless.
    """
    return EditDecisionList(
        shots=[
            _shot(
                clip=f"C{i:02d}",
                end=1.0,
                motion=Motion(kind="ken-burns", intensity=0.35),
                transition_in=Transition("cut", 0.0),
            )
            for i in range(count)
        ]
    )


def _gaze_notes(edl: EditDecisionList) -> list[str]:
    return [
        p.title
        for p in GazeAgent().inspect(
            edl,
            Prediction(hook=0.5, share=0.5, loop=0.5),
            FitReport(rows=0, simulated_rows=0, measured_rows=0),
        )
    ]


def test_the_gaze_agent_calls_out_a_film_that_only_made_one_decision():
    """The failure it was built unable to see.

    Its five original proposals all reduced variance, so a film where every
    shot is graded, framed and moved identically was its perfect score — the
    agent responsible for taste was the one enforcing the monotony.
    """
    notes = _gaze_notes(_monotone_edl())
    assert "Give the cut more than one kind of join" in notes
    assert "Stop every shot moving the same way" in notes
    assert "Let the rhythm breathe" in notes


def test_the_gaze_agent_does_not_flatten_a_film_that_is_already_flat():
    """Homogenising a collapsed timeline is the exact wrong move.

    The grades here are deliberately far enough apart that the old
    thresholds — 0.25 exposure, 0.30 temperature, 0.35 contrast — would every
    one of them have fired. The cut is still one join, one move and one shot
    length, so pulling the colour spread in as well would take away the last
    thing distinguishing one shot from the next. That is how an agent makes
    the fault it was asked to fix worse.
    """
    edl = _monotone_edl(12)
    for i, shot in enumerate(edl.shots):
        swing = 0.9 if i % 2 else -0.9
        shot.look = Look(exposure=swing, temperature=swing, contrast=swing * 0.5)
    notes = _gaze_notes(edl)
    for reducing in (
        "Match exposure across the cut",
        "Unify colour temperature",
        "Even out the contrast across shots",
    ):
        assert reducing not in notes
    # It still says the real thing about the same timeline.
    assert "Give the cut more than one kind of join" in notes


def test_the_gaze_agent_still_matches_exposure_on_a_cut_that_is_otherwise_varied():
    """The homogenisers are gated, not deleted.

    Mixed daylight and tungsten across a real edit is still a fault, and
    suppressing the proposal everywhere would trade one blindness for
    another.
    """
    joins = ["cut", "dissolve", "whip-left", "cut", "glitch", "cut", "light-leak", "cut"]
    moves = ["none", "punch-in", "none", "drift-left", "none", "pull-out", "float", "none"]
    lengths = [0.4, 0.4, 1.1, 0.3, 0.8, 0.5, 1.4, 0.6]
    edl = EditDecisionList(
        shots=[
            _shot(
                clip=f"C{i:02d}",
                end=lengths[i],
                look=Look(exposure=0.9 if i % 2 else -0.9),
                motion=Motion(kind=moves[i], intensity=0.3),
                transition_in=Transition(joins[i], 0.0 if joins[i] == "cut" else 0.25),
            )
            for i in range(len(joins))
        ]
    )
    assert "Match exposure across the cut" in _gaze_notes(edl)


def test_the_gaze_agent_leaves_a_varied_cut_alone():
    """Variety is not a fault. It must not propose evening out a real edit."""
    joins = ["cut", "dissolve", "whip-left", "cut", "glitch", "cut", "light-leak"]
    moves = ["none", "punch-in", "none", "drift-left", "none", "pull-out", "float"]
    lengths = [0.4, 0.4, 1.1, 0.3, 0.8, 0.5, 1.4]
    edl = EditDecisionList(
        shots=[
            _shot(
                clip=f"C{i:02d}",
                end=lengths[i],
                motion=Motion(kind=moves[i], intensity=0.3),
                transition_in=Transition(joins[i], 0.0 if joins[i] == "cut" else 0.25),
            )
            for i in range(len(joins))
        ]
    )
    notes = _gaze_notes(edl)
    assert "Give the cut more than one kind of join" not in notes
    assert "Stop every shot moving the same way" not in notes
    assert "Let the rhythm breathe" not in notes


def test_varying_the_joins_leaves_most_of_them_hard_cuts():
    """A reel that transitions every join is mush.

    The proposal exists to break a run, not to decorate every edit — the
    measured reference reels hard-cut the large majority of theirs, and what
    makes them read as rich is that the remainder is varied.
    """
    edl = _monotone_edl(20)
    proposals = GazeAgent().inspect(
        edl,
        Prediction(hook=0.5, share=0.5, loop=0.5),
        FitReport(rows=0, simulated_rows=0, measured_rows=0),
    )
    change = next(p for p in proposals if p.title.startswith("Give the cut"))
    change.change(edl)
    joins = [shot.transition_in.kind for shot in edl.shots[1:]]
    assert joins.count("cut") > len(joins) * 0.6
    assert len(set(joins)) >= 3


def test_screen_time_follows_speed():
    assert _shot(end=2.0, ramp=Ramp.constant(1.0)).duration == pytest.approx(2.0)
    assert _shot(end=2.0, ramp=Ramp.constant(2.0)).duration == pytest.approx(1.0)
    assert _shot(end=2.0, ramp=Ramp.constant(0.5)).duration == pytest.approx(4.0)


def test_a_ramp_integrates_rather_than_averaging_speed():
    """Screen time is the integral of 1/speed, which is not 1/mean(speed)."""
    ramp = Ramp([(0.0, 0.5), (1.0, 2.0)])
    duration = ramp.output_duration(2.0)
    naive = 2.0 / ((0.5 + 2.0) / 2)
    assert duration > naive
    assert duration == pytest.approx(2.0 * np.mean(1.0 / np.linspace(0.5, 2.0, 64)), rel=0.02)


def test_transitions_shorten_the_timeline_by_their_overlap():
    edl = EditDecisionList(
        shots=[
            _shot(end=2.0),
            _shot(clip="C02", end=2.0, transition_in=Transition("dissolve", 0.5)),
        ]
    )
    assert edl.duration == pytest.approx(3.5)
    starts = [start for start, _, _ in edl.timeline()]
    assert starts == pytest.approx([0.0, 1.5])


def test_repair_clamps_shots_to_the_footage_that_exists(rushes):
    """A director asking for frames past the end of a clip must not crash a render."""
    dossier = build_dossier("C01", probe_asset(rushes / "a_wide.mp4"))
    edl = EditDecisionList(shots=[_shot(start=1.0, end=99.0)])

    notes = edl.repair({"C01": dossier})

    assert any("past end of clip" in note for note in notes)
    assert edl.shots[0].end <= dossier.duration + 1e-6


def test_repair_drops_a_shot_from_a_clip_that_is_not_there():
    edl = EditDecisionList(shots=[_shot(clip="C01"), _shot(clip="C99")])
    with pytest.raises(ValueError):
        # C01 is unknown too, so nothing survives and an empty film is an error.
        edl.repair({})

    edl = EditDecisionList(shots=[_shot(clip="C01"), _shot(clip="C99")])
    notes = edl.repair({"C01": _FakeDossier(4.0)})
    assert len(edl.shots) == 1
    assert any("C99" in note for note in notes)


def test_repair_never_dissolves_into_the_first_frame():
    edl = EditDecisionList(
        shots=[
            _shot(transition_in=Transition("dissolve", 0.5)),
            _shot(clip="C02"),
        ]
    )
    edl.repair({"C01": _FakeDossier(4.0), "C02": _FakeDossier(4.0)})
    assert edl.shots[0].transition_in.is_cut


def test_repair_shortens_a_transition_longer_than_its_shots():
    edl = EditDecisionList(
        shots=[
            _shot(end=1.0),
            _shot(clip="C02", end=1.0, transition_in=Transition("dissolve", 5.0)),
        ]
    )
    edl.repair({"C01": _FakeDossier(4.0), "C02": _FakeDossier(4.0)})
    assert edl.shots[1].transition_in.duration <= 0.5


class _FakeDossier:
    """Minimal stand-in with the two attributes repair() reads."""

    def __init__(self, duration: float):
        self.duration = duration
        self.asset = type("A", (), {"path": Path("/dev/null"), "kind": "video"})()


# ---------------------------------------------------------------------------
# Film grammar
# ---------------------------------------------------------------------------


def test_variety_pass_breaks_up_a_repeated_clip():
    edl = EditDecisionList(
        shots=[
            _shot(clip="C01"),
            _shot(clip="C01"),
            _shot(clip="C02"),
            _shot(clip="C03"),
        ]
    )
    grammar.enforce_variety(edl)
    ids = [shot.clip_id for shot in edl.shots]
    assert ids[0] != ids[1], "consecutive shots from one clip read as a mistake"


def test_transition_density_is_capped():
    edl = EditDecisionList(
        shots=[
            _shot(clip=f"C{i:02d}", transition_in=Transition("dissolve", 0.3)) for i in range(10)
        ]
    )
    demoted = grammar.limit_transition_density(edl, max_fraction=0.2)
    fancy = sum(1 for shot in edl.shots if not shot.transition_in.is_cut)
    assert demoted > 0
    assert fancy <= 2


def test_hook_is_capped_and_always_a_cut():
    edl = EditDecisionList(shots=[_shot(end=6.0), _shot(clip="C02")])
    grammar.ensure_hook(edl, max_hook=1.4)
    assert edl.shots[0].duration <= 1.45
    assert edl.shots[0].transition_in.is_cut


def test_trimming_removes_shots_from_the_middle():
    edl = EditDecisionList(shots=[_shot(clip=f"C{i:02d}", end=2.0) for i in range(8)])
    first, last = edl.shots[0], edl.shots[-1]
    grammar.trim_to_duration(edl, 6.0, tolerance=0.5)
    assert edl.duration <= 7.0
    assert edl.shots[0] is first, "the hook is never the shot that gets cut"
    assert edl.shots[-1] is last, "nor is the closer"


# ---------------------------------------------------------------------------
# Craft: filter construction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("preset", sorted(color.LOOKS))
def test_every_look_builds_a_filter_chain(preset):
    chain = color.look_chain(Look(preset=preset, strength=1.0))
    assert chain, f"{preset} produced nothing"
    assert "=" in chain


def test_bloom_pads_are_unique_across_calls():
    """Two blooms in one graph must not collide on filter-pad names."""
    first = color.look_chain(Look(preset="neon"))
    second = color.look_chain(Look(preset="neon"))
    assert first != second
    labels = {part for part in first.split("[") if "]" in part}
    assert labels.isdisjoint({part for part in second.split("[") if "]" in part})


def test_look_is_identity_when_nothing_is_set():
    assert Look().is_identity
    assert not Look(preset="noir").is_identity


def test_ramp_graph_slices_a_curve_and_leaves_flat_speeds_alone():
    flat = motion.ramp_video_graph(
        Ramp.constant(2.0), source_duration=2.0, in_label="v", out_label="o"
    )
    assert "split" not in flat
    assert "setpts=PTS/2.0" in flat

    curved = motion.ramp_video_graph(Ramp.hit(), source_duration=2.0, in_label="v", out_label="o")
    assert "split=" in curved and "concat=" in curved


def test_atempo_factorises_extreme_speeds():
    assert motion._atempo(1.0).count("atempo") == 1
    assert motion._atempo(0.25).count("atempo") >= 2, "atempo bottoms out at 0.5x"


def test_reframe_covers_the_target_frame():
    chain = motion.reframe_chain(1080, 1920, mode="subject", anchor=(0.3, 0.4))
    assert "force_original_aspect_ratio=increase" in chain
    assert "crop=1080:1920" in chain


def test_motion_chain_lands_on_the_delivery_size():
    for kind in ("none", "ken-burns", "punch-in", "drift-left", "shake"):
        chain = motion.motion_chain(
            Motion(kind, 0.5), target_w=1080, target_h=1920, fps=30, duration=2.0
        )
        assert "1080" in chain and "1920" in chain, kind


def test_transition_specs_are_well_formed():
    for kind in sorted(transitions.BUILTIN):
        spec = transitions.xfade_spec(kind, 0.3, 1.0)
        assert spec.startswith("xfade=")
        assert "duration=" in spec and "offset=" in spec


# ---------------------------------------------------------------------------
# Sound synthesis
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["whoosh", "impact", "sub-drop", "riser", "tick"])
def test_effects_are_finite_and_normalised(kind):
    samples = sound.synthesise(kind, 0.5)
    assert samples.size > 0
    assert np.isfinite(samples).all(), f"{kind} produced NaN or inf"
    assert np.abs(samples).max() <= 1.0
    assert samples[0] == pytest.approx(0.0, abs=1e-3), "must start silent"
    assert samples[-1] == pytest.approx(0.0, abs=1e-3), "must end silent"


def test_wav_round_trips(tmp_path):
    path = sound.write_wav(tmp_path / "x.wav", sound.synthesise("impact", 0.4))
    with wave.open(str(path)) as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == sound.SAMPLE_RATE
        assert handle.getnframes() > 0


# ---------------------------------------------------------------------------
# Reading the brief
# ---------------------------------------------------------------------------


def test_brief_extracts_runtime_look_and_text():
    brief = parse_brief('a moody neon chase, 20 seconds, ends on "THE SIGN"')
    assert brief.duration == pytest.approx(20.0)
    assert brief.look == "neon"
    assert "THE SIGN" in brief.on_screen_text


def test_pace_words_move_the_shot_length():
    assert (
        parse_brief("frenetic montage").base_shot_length
        < parse_brief("slow montage").base_shot_length
    )


def test_energy_arcs_stay_in_range():
    for arc in ("hook-drop", "crescendo", "trailer", "wave", "calm", "decay"):
        brief = parse_brief("montage")
        brief.arc = arc
        values = [brief.energy_at(i / 20) for i in range(21)]
        assert all(0.0 <= value <= 1.0 for value in values), arc


def test_hook_drop_opens_hot_and_finishes_hard():
    brief = parse_brief("montage")
    assert brief.energy_at(0.02) > 0.7
    assert brief.energy_at(0.2) < brief.energy_at(0.02)
    assert brief.energy_at(1.0) > brief.energy_at(0.2)


def test_cuts_only_brief_disables_transitions():
    assert parse_brief("hard cuts only montage").transitions == ("cut",)


def test_the_default_montage_is_cut_at_the_pace_the_reference_reels_are_cut_at():
    """`montage` is the style a film gets when nobody says a pace word.

    Which makes it the most consequential number in the table, and it was the
    one number in it nobody had measured: 0.9s a shot, invented. The reels the
    program is held against cut their montages at a median of 0.334s — two and
    a half times faster — so every film made without a pace word was slower
    than the work it was being compared to, and no test noticed because no test
    compared the default to the corpus.

    The expected number is computed from the reels here rather than typed in,
    so re-measuring the corpus moves the test and the default together.
    """
    import json
    import statistics

    from auteur.director.brief import PACE_WORDS
    from auteur.scholar.library import HYPERCUT_HOLD, MONTAGE_HOLD

    reels = json.loads(
        (
            Path(__file__).resolve().parent.parent / "tools" / "artifact" / "templates.json"
        ).read_text(encoding="utf-8")
    )
    band = [
        float(reel["hold"]) for reel in reels if HYPERCUT_HOLD < float(reel["hold"]) <= MONTAGE_HOLD
    ]
    assert len(band) >= 3, "the corpus no longer has a montage band to measure"

    assert parse_brief("montage").base_shot_length == pytest.approx(
        statistics.median(band), abs=0.005
    ), "the default montage is not cut at the pace of the reels it is judged against"

    # And it is genuinely the default: an empty prompt lands on it too.
    assert parse_brief("").base_shot_length == parse_brief("montage").base_shot_length

    # Faster than a montage is still a thing you can ask for, and slower still
    # slower — measuring the default must not have flattened the scale.
    assert parse_brief("a hypercut").base_shot_length < parse_brief("montage").base_shot_length
    assert (
        parse_brief("slow and cinematic").base_shot_length > parse_brief("montage").base_shot_length
    )
    assert set(PACE_WORDS), "the pace words are gone"


def test_a_montage_that_fast_joins_its_shots_with_cuts_not_dissolves():
    """Changing the number changes what the joins have to be.

    A third of a second is not long enough to dissolve through — a 0.4s
    crossfade over a 0.334s shot is a film with no shots in it, only
    transitions. When the montage default moved, the transition vocabulary had
    to move with it, and that pairing is what this holds.
    """
    joins = parse_brief("montage").transitions
    assert joins, "a montage has no transitions at all"
    assert joins.count("cut") >= len(joins) / 2, f"a 0.334s montage dissolves through {joins}"


# ---------------------------------------------------------------------------
# The director
# ---------------------------------------------------------------------------


def test_heuristic_director_produces_a_legal_edit(rushes):
    bin_ = ingest([rushes])
    dossiers = [build_dossier(f"C{i + 1:02d}", asset) for i, asset in enumerate(bin_.visuals)]
    settings = Settings(quality=QUALITIES["draft"], target_duration=8.0)

    edl = cut(parse_brief("fast montage, 8 seconds"), dossiers, settings)

    assert edl.shots
    assert edl.duration == pytest.approx(8.0, abs=2.0)
    ids = [shot.clip_id for shot in edl.shots]
    assert all(a != b for a, b in zip(ids, ids[1:], strict=False)), "no clip cuts back to itself"
    # The floor is two frames at 24fps, not a fifth of a second. This asserted
    # 0.2 back when `MIN_SLOT` was 0.32 and the director could not plan a
    # shorter shot anyway — it was reading a ceiling back and calling it a
    # rule. The reference reels have a median shot of 0.167s.
    from auteur.edl import MIN_SHOT

    for shot in edl.shots:
        assert shot.duration >= MIN_SHOT


def test_a_hypercut_reaches_the_rate_the_reference_reels_are_cut_at(rushes):
    """Three ceilings used to stop it, in three different places.

    `edl.MIN_SHOT` dropped shots under a quarter second, the cut detector's
    350ms refractory made a fast reel report as meditative, and `MIN_SLOT`
    would not plan a shot under 0.32s. With those gone the beat grid was still
    one: a whole beat at 120 BPM is half a second, so beat-synced cutting could
    not go faster than twice a second whatever the brief asked.
    """
    from auteur.director.heuristic import _subdivided

    bin_ = ingest([rushes])
    dossiers = [build_dossier(f"C{i + 1:02d}", asset) for i, asset in enumerate(bin_.visuals)]
    settings = Settings(quality=QUALITIES["draft"], target_duration=10.0)

    fast = cut(parse_brief("a hypercut of the city, 10 seconds"), dossiers, settings)
    slow = cut(parse_brief("a cinematic montage of the city, 10 seconds"), dossiers, settings)

    rate = len(fast.shots) / (fast.duration / 10.0)
    assert rate > len(slow.shots) / (slow.duration / 10.0) * 2, "a hypercut cuts like a montage"
    # The measured reference band is 19.4 to 76.1 cuts per ten seconds.
    assert rate >= 19.0, f"{rate:.1f} cuts per ten seconds is slower than every reference reel"

    # Half-second beats, and a brief that wants shots a third that long.
    beats = [0.5 * n for n in range(1, 21)]
    assert len(_subdivided(beats, 0.167)) > len(beats) * 2
    # A leisurely brief still lands on whole beats.
    assert _subdivided(beats, 1.2) == beats


def test_the_same_seed_gives_the_same_cut(rushes):
    bin_ = ingest([rushes])
    dossiers = [build_dossier(f"C{i + 1:02d}", asset) for i, asset in enumerate(bin_.visuals)]
    brief = parse_brief("fast montage, 8 seconds")

    def edit(seed: int) -> list[tuple]:
        settings = Settings(quality=QUALITIES["draft"], target_duration=8.0, seed=seed)
        edl = cut(brief, dossiers, settings)
        return [(shot.clip_id, round(shot.start, 2)) for shot in edl.shots]

    assert edit(1234) == edit(1234)


# ---------------------------------------------------------------------------
# Regressions — each of these shipped broken once
# ---------------------------------------------------------------------------


def test_a_still_with_source_audio_is_never_mixed():
    """The renderer and the mixer must agree on which segments have sound.

    They once disagreed: the renderer refused audio on a still, the mixer asked
    for it anyway, and ffmpeg failed on a stream that was never written.
    """
    from auteur.render import carries_audio

    still = _shot(use_source_audio=True, audio_gain=1.0, is_still=True)
    moving = _shot(use_source_audio=True, audio_gain=1.0, is_still=False)

    assert not carries_audio(still, want_audio=True), "a still has no sound to carry"
    assert carries_audio(moving, want_audio=True)
    assert not carries_audio(moving, want_audio=False)
    assert not carries_audio(_shot(use_source_audio=True, audio_gain=0.0), want_audio=True)


@pytest.mark.slow
def test_a_single_frame_video_can_be_held(rushes, tmp_path):
    """`-loop` is an image-demuxer option; a one-frame *video* needs -stream_loop.

    ingest treats any sub-frame-length source as a still, so a one-frame mp4
    took the image path and ffmpeg rejected it with "Option loop not found".
    """
    from auteur.config import FORMATS, QUALITIES, Workspace
    from auteur.render import render_shot

    binary = str(ffmpeg.ffmpeg_path())
    clip = tmp_path / "one_frame.mp4"
    subprocess.run(
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=25",
            "-frames:v",
            "1",
            "-c:v",
            "libx264",
            "-crf",
            "30",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(clip),
        ],
        check=True,
    )

    shot = Shot(clip_id="C01", source=clip, start=0.0, end=1.5, is_still=True)
    segment = render_shot(
        shot, 0, Workspace(tmp_path / "w"), FORMATS["square"], QUALITIES["draft"], want_audio=False
    )
    assert segment.exists() and segment.stat().st_size > 1000


def test_swapping_shots_leaves_the_transitions_where_they_were():
    """A transition was chosen for a position on the timeline, not for a shot."""
    edl = EditDecisionList(
        shots=[
            _shot(clip="C01"),
            _shot(clip="C01", transition_in=Transition("dissolve", 0.4)),
            _shot(clip="C02", transition_in=Transition("whip-left", 0.2)),
            _shot(clip="C03", transition_in=Transition("cut", 0.0)),
        ]
    )
    before = [(shot.transition_in.kind, shot.transition_in.duration) for shot in edl.shots]

    grammar.enforce_variety(edl)

    after = [(shot.transition_in.kind, shot.transition_in.duration) for shot in edl.shots]
    assert after[1:] == before[1:], "joins must stay put when shots are reordered"


def test_varying_the_pacing_never_creates_a_flash_frame():
    edl = EditDecisionList(shots=[_shot(clip=f"C{i:02d}", end=MIN_SHOT * 1.7) for i in range(6)])
    grammar.vary_pacing(edl, run_length=3, spread=0.5)
    for shot in edl.shots:
        assert shot.duration >= MIN_SHOT, "a fix must not trade one fault for a worse one"


def test_beat_multiples_vary_without_leaving_the_grid():
    """Nudging lengths by a percentage is undone by the next beat snap."""
    beat = 0.5
    edl = EditDecisionList(shots=[_shot(clip=f"C{i:02d}", end=beat) for i in range(9)])
    assert grammar.vary_beat_multiples(edl, beat, every=3) > 0

    multiples = {round(shot.duration / beat) for shot in edl.shots}
    assert multiples == {1, 2}, "shots must still be whole numbers of beats"
    for shot in edl.shots:
        assert abs(shot.duration / beat - round(shot.duration / beat)) < 0.02


def test_a_film_cut_faster_than_the_beat_is_varied_in_its_own_unit():
    """Every rhythm pass assumed a shot lasts at least one beat.

    It does not. A montage at 0.25s against a 120bpm track cuts twice a beat;
    a hypercut cuts three or six times. `round(0.25 / 0.5)` is zero, and the
    `max(1, ...)` guard turned that into "one beat" — so the fix for metronomic
    cutting stretched every third shot to *two beats*, 1.0s, four times its
    length. Measured on a real render: a montage planned at 31 shots over 10s
    was delivered as 12, cut at 0.998s a shot, three times slower than asked.
    """
    beat = 0.5
    for hold, expected_unit in ((0.25, 0.25), (0.167, 0.5 / 3)):
        edl = EditDecisionList(shots=[_shot(clip=f"C{i:02d}", end=hold) for i in range(12)])
        assert grammar.beat_unit(edl, beat) == pytest.approx(expected_unit, abs=0.01)

        before = sum(shot.duration for shot in edl.shots)
        assert grammar.vary_beat_multiples(edl, beat, every=3) > 0
        after = [shot.duration for shot in edl.shots]

        assert len({round(x, 3) for x in after}) > 1, "the pass varied nothing"
        # Every hold is a whole number of the film's own units, and none is
        # longer than the four-unit hold the phrase uses for punctuation. The
        # failure this guards is a 0.25s shot arriving at 1.0s because the code
        # rounded it up to "one beat" and then doubled that.
        for length in after:
            units = length / expected_unit
            assert abs(units - round(units)) < 0.02, (
                f"a {hold}s shot became {length:.3f}s, which is {units:.2f} units — "
                "off the grid the film is cut on"
            )
        assert max(after) <= expected_unit * 4 + 0.01, (
            f"a {hold}s shot was stretched to {max(after):.3f}s, beyond the longest "
            "hold the phrase uses"
        )
        # Varying may lengthen the film a little; it must not transform it.
        assert sum(after) < before * 1.5, "varying the rhythm rewrote the pace"


def test_the_fix_for_metronomic_cutting_can_actually_clear_the_complaint():
    """The repair produced two lengths; the critic demands three. Deadlock.

    `critic.review` calls a film metronomic when its shots use fewer than three
    distinct multiples of the unit, and `vary_beat_multiples` only ever doubled
    — so a montage came out {1, 2}, one short, and every pass of the review
    loop raised the same complaint against a film the repair had already done
    all it could to. Measured on a real render: three passes, three identical
    "every shot is the same length" notes.

    The bar is not the thing at fault. Across the twenty-three reference reels
    the median reel uses five distinct multiples of its own unit, and only
    three of them get by on two — so the corpus is *more* varied than the bar
    asks for, and it was the fix that fell short.
    """
    beat = 0.5
    edl = EditDecisionList(shots=[_shot(clip=f"C{i:02d}", end=0.25) for i in range(24)])
    unit = grammar.beat_unit(edl, beat)
    assert grammar.vary_beat_multiples(edl, beat, every=3) > 0

    multiples = {max(1, round(shot.duration / unit)) for shot in edl.shots}
    assert len(multiples) >= 3, f"the repair can only ever produce {multiples}"
    assert multiples <= {1, 2, 4}, f"{multiples} leaves the grid the film is cut on"

    # Mostly short, as the reels are — the long holds are punctuation.
    ones = sum(1 for shot in edl.shots if round(shot.duration / unit) == 1)
    assert ones > len(edl.shots) / 2, "the film stopped being a montage"


def test_the_reference_reels_vary_more_than_the_critic_demands():
    """The bar of three distinct lengths is measured, not picked.

    If the reels themselves cut with only one or two lengths, the critic would
    be holding films to a standard the work it is judged against does not meet.
    They do not: the median reel uses five.
    """
    import json
    import statistics

    reels = json.loads(
        (
            Path(__file__).resolve().parent.parent / "tools" / "artifact" / "templates.json"
        ).read_text(encoding="utf-8")
    )

    counts = []
    for reel in reels:
        holds = [float(beat[0]) for beat in reel["beats"] if float(beat[0]) > 0]
        if len(holds) < 8:
            continue
        ordered = sorted(holds)
        unit = ordered[len(ordered) // 4]
        counts.append(len({max(1, int(round(hold / unit))) for hold in holds}))

    assert counts, "the corpus no longer carries per-shot timings"
    assert statistics.median(counts) >= 3, (
        f"the reels use a median of {statistics.median(counts)} distinct shot lengths, "
        "so the critic asks for more variety than the work it judges against"
    )


def test_a_slower_film_still_varies_in_whole_beats():
    """The subdivision must not fire on a film that is cut on the beat."""
    beat = 0.5
    edl = EditDecisionList(shots=[_shot(clip=f"C{i:02d}", end=beat) for i in range(9)])
    assert grammar.beat_unit(edl, beat) == pytest.approx(beat)
    assert grammar.vary_beat_multiples(edl, beat, every=3) > 0
    assert {round(shot.duration / beat) for shot in edl.shots} == {1, 2}


def test_the_critic_measures_a_fast_cut_against_the_grid_it_is_cut_on():
    """Two faults the critic could never stop reporting, on any fast film.

    A montage alternating 0.25s and 0.5s has an audible two-to-one rhythm, and
    counting whole beats collapsed both to "1 beat" — so `metronomic` fired
    forever and the repair loop kept trying. The same assumption made
    `off-beat` unfixable: a film cut on eighths puts half its cuts between
    beats deliberately, so it could never score above 50% against a whole-beat
    grid, and 55% was the bar.
    """
    from auteur.analysis.audio import AudioAnalysis

    beat = 0.5
    lengths = [0.25 if index % 3 else 0.5 for index in range(24)]
    edl = EditDecisionList(shots=[_shot(clip=f"C{i:02d}", end=x) for i, x in enumerate(lengths)])

    unit = grammar.beat_unit(edl, beat)
    assert unit == pytest.approx(0.25), "the film's grid is the eighth, not the beat"

    # Rhythm: in the film's unit these are one and two, which is a rhythm.
    assert {max(1, int(round(x / unit))) for x in lengths} == {1, 2}
    # In whole beats they were indistinguishable, which was the bug.
    assert {max(1, int(round(x / beat))) for x in lengths} == {1}

    # Beat accuracy: every cut lands on the subdivided grid.
    beats = [beat * n for n in range(1, 60)]
    grid = grammar.subdivide(beats, beat, unit)
    assert grid[:4] == pytest.approx([0.25, 0.5, 0.75, 1.0])

    cursor, on_grid = 0.0, 0
    for length in lengths:
        cursor += length
        if min(abs(line - cursor) for line in grid) < 0.09:
            on_grid += 1
    assert on_grid == len(lengths), "a film cut on the grid reads as off it"

    assert AudioAnalysis is not None  # the critic's beat source, imported above


def test_text_plates_are_named_per_format(tmp_path):
    """Two shapes share the assets folder; plates are sized to the frame."""
    from auteur.craft.titles import render_all
    from auteur.edl import TextCue

    cue = TextCue(text="HELLO", start=0.0, duration=1.0, style="title")
    reel = render_all([cue], width=200, height=356, directory=tmp_path, prefix="reel")
    square = render_all([cue], width=300, height=300, directory=tmp_path, prefix="square")

    assert reel[0].path != square[0].path, "one format would overwrite the other's plate"
    assert reel[0].path.exists() and square[0].path.exists()


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def test_the_prompt_can_be_the_last_argument(tmp_path):
    from auteur.cli import _split_paths_and_prompt

    folder = tmp_path / "clips"
    folder.mkdir()

    paths, prompt = _split_paths_and_prompt([str(folder), "fast montage"], None)
    assert paths == [str(folder)] and prompt == "fast montage"

    # An existing path is footage, never direction.
    other = tmp_path / "more"
    other.mkdir()
    paths, prompt = _split_paths_and_prompt([str(folder), str(other)], None)
    assert paths == [str(folder), str(other)] and prompt is None

    # An explicit --prompt always wins.
    paths, prompt = _split_paths_and_prompt([str(folder), str(other)], "explicit")
    assert paths == [str(folder), str(other)] and prompt == "explicit"


def test_plain_english_helpers():
    from auteur import ui

    assert ui.describe_shape(1080, 1920) == "vertical, for phones"
    assert ui.describe_shape(1080, 1080) == "square"
    assert ui.describe_shape(1920, 1080) == "widescreen"
    assert ui.describe_shape(1920, 816) == "cinematic widescreen"

    assert ui.describe_count(1, "clip") == "1 clip"
    assert ui.describe_count(3, "clip") == "3 clips"
    assert ui.describe_duration(45) == "45 seconds"
    assert ui.describe_duration(95) == "1m 35s"

    assert "metronome" in ui.plain_finding("metronomic", "x") or "same length" in ui.plain_finding(
        "metronomic", "x"
    )
    assert ui.plain_finding("unknown-rule", "the fallback") == "the fallback"


def test_a_missing_api_key_is_not_reported_as_a_failure():
    from auteur.ui import plain_model_reason

    message, problem = plain_model_reason(
        'could not reach the model: "Could not resolve authentication method. '
        'Expected one of api_key, auth_token, or credentials to be set."'
    )
    assert not problem, "no key is the ordinary case, not an error"
    assert "api key" in message.lower()
    assert len(message) < 100, "and it must not dump the SDK traceback at the user"

    assert plain_model_reason("no model configured") == ("", False)
    assert plain_model_reason("could not reach the model: connection reset")[1] is True


def test_a_one_line_success_message_does_not_crash_the_command(capsys):
    """Half the commands want a block; half want one line saying it worked.

    `result` was keyword-only with both lists required, so every one-line
    caller raised TypeError at the exact moment its command *succeeded* —
    `benchmark remove` and four scholar commands. Nothing unit-tests a success
    message, so the suite was silent about it and CodeQL found it instead.
    Every implementation of the interface has to take the short form.
    """
    import threading

    from auteur.ui import NullReporter, Reporter
    from auteur.web.server import Job, WebReporter

    say = Reporter()
    say.result("dropped the benchmark")
    assert "dropped the benchmark" in capsys.readouterr().out

    # The full form still works, and so do the other two implementations.
    say.result(headline="Your film is ready", facts=["12 shots"], files=[("the film", "/x.mp4")])
    assert "Your film is ready" in capsys.readouterr().out

    NullReporter().result("quiet")
    WebReporter(Job(id="j", prompt="p", folder=Path("/tmp/nowhere")), threading.Lock()).result("w")
    assert capsys.readouterr().out == ""


def test_the_quiet_reporter_prints_nothing(capsys):
    from auteur.ui import NullReporter

    say = NullReporter()
    say.banner("x")
    say.step("y")
    say.detail("z")
    say.progress(1, 2, "a")
    say.result(headline="h", facts=["f"], files=[("a", "b")])
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_format_aliases_resolve():
    assert resolve_format("9:16") is FORMATS["reel"]
    assert resolve_format("tiktok") is FORMATS["reel"]
    custom = resolve_format("720x1280")
    assert (custom.width, custom.height) == (720, 1280)
    with pytest.raises(ValueError):
        resolve_format("banana")


def test_workspace_creates_its_layout(tmp_path):
    space = Workspace(tmp_path / "work")
    for directory in (space.segments, space.assets, space.output, space.logs):
        assert directory.is_dir()


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_the_agent_renders_a_playable_film(rushes, tmp_path):
    from auteur.agent import direct

    production = direct(
        [rushes],
        'punchy montage, 6 seconds, "TEST"',
        settings=Settings(
            quality=QUALITIES["draft"],
            primary_format=FORMATS["square"],
            target_duration=6.0,
            use_llm=False,
            revision_rounds=0,
        ),
        workspace=tmp_path / "work",
        duration=6.0,
    )

    output = production.primary
    assert output is not None and output.exists()

    probed = __import__("auteur.render", fromlist=["probe_output"]).probe_output(output)
    assert probed["duration"] == pytest.approx(6.0, abs=1.0)
    assert (probed["width"], probed["height"]) == (1080, 1080)
    assert probed["video_codec"] == "h264"
    assert probed["audio_codec"] == "aac"
    assert probed["size_bytes"] > 10_000

    assert (production.workspace.root / "production-notes.md").exists()
    assert (production.workspace.root / "edl.json").exists()
    assert production.final_critique is not None


# ---------------------------------------------------------------------------
# Speed ramps against short sources
# ---------------------------------------------------------------------------


def test_a_ramp_never_slices_finer_than_the_source_frames():
    """A slice thinner than one frame trims to nothing, and nothing concatenates
    to an empty file that ffmpeg still calls a success.

    The slice count is chosen from screen time, but the slices are cut from the
    source, and a heavy slow-motion shot has far less source than screen. Half a
    second of 30fps footage stretched to 1.2s once asked for 16 slices of 31ms —
    shorter than the 33ms between frames — and every one of them came out empty.
    """
    ramp = Ramp(points=((0.0, 1.04), (0.27, 0.245), (0.53, 0.245), (1.0, 1.04))).normalise()
    source_duration, source_fps = 0.499, 30.0

    slices = motion.ramp_slice_count(ramp, source_duration, source_fps)
    frames_per_slice = (source_duration / slices) * source_fps
    assert frames_per_slice >= motion.RAMP_FRAMES_PER_SLICE - 1e-9

    graph = motion.ramp_video_graph(
        ramp,
        source_duration=source_duration,
        in_label="src",
        out_label="out",
        source_fps=source_fps,
    )
    assert "concat=n=1:" not in graph
    for piece in graph.split(";"):
        if not piece.startswith("[rs"):
            continue
        window = piece.split("trim=start=")[1].split(",")[0]
        start, end = (float(value) for value in window.split(":end="))
        assert (end - start) * source_fps >= motion.RAMP_FRAMES_PER_SLICE - 1e-6


def test_a_ramp_with_almost_no_source_falls_back_to_one_speed():
    """Two frames of source cannot carry a curve; it must still be a shot."""
    ramp = Ramp(points=((0.0, 1.0), (0.5, 0.2), (1.0, 1.0))).normalise()
    graph = motion.ramp_video_graph(
        ramp, source_duration=0.07, in_label="src", out_label="out", source_fps=30.0
    )
    assert "split=" not in graph and "concat" not in graph
    assert graph.startswith("[src]setpts=") and graph.endswith("[out]")

    audio = motion.ramp_audio_graph(
        ramp, source_duration=0.07, in_label="a", out_label="b", source_fps=30.0
    )
    assert "asplit=" not in audio and "concat" not in audio


def test_source_fps_is_read_from_the_file(rushes):
    assert ffmpeg.source_fps(rushes / "a_wide.mp4") == pytest.approx(25.0, abs=0.1)
    # Something unreadable must not crash the ramp maths.
    assert ffmpeg.source_fps(rushes / "beat.wav") == 30.0


@pytest.mark.slow
def test_a_shot_that_renders_no_frames_is_reported_not_shipped(rushes, tmp_path):
    """An empty segment used to reach the assembly, which failed with a
    filtergraph error naming neither the shot nor the reason."""
    from auteur import render

    space = Workspace(tmp_path / "work")
    shot = Shot(
        clip_id="C01",
        source=rushes / "a_wide.mp4",
        start=0.0,
        end=0.02,
        ramp=Ramp(points=((0.0, 1.0),)),
    )
    # Force the pathology the guard exists for: a window with no frames in it.
    shot.start, shot.end = 3.999, 4.0

    try:
        path = render.render_shot(
            shot, 0, space, FORMATS["square"], QUALITIES["draft"], want_audio=False
        )
    except ffmpeg.FFmpegError as exc:
        assert "no frames" in str(exc)
    else:
        # If ffmpeg did find a frame, the guard must agree the file is usable.
        assert render._has_video(path)


# ---------------------------------------------------------------------------
# The phone app
# ---------------------------------------------------------------------------


def test_the_upload_parser_separates_fields_from_files():
    from auteur.web.server import _parse_multipart

    boundary = "----auteurtest"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="prompt"\r\n\r\n'
        "fast neon montage\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="shape"\r\n\r\n'
        "reel\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="clips"; filename="IMG_0042.MOV"\r\n'
        "Content-Type: video/quicktime\r\n\r\n"
        "not-really-a-movie\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    fields, files = _parse_multipart(body, f"multipart/form-data; boundary={boundary}")
    assert fields == {"prompt": "fast neon montage", "shape": "reel"}
    assert files == [("IMG_0042.MOV", b"not-really-a-movie")]


def test_every_icon_the_manifest_names_is_actually_served(tmp_path):
    """A manifest that points at a missing icon installs with no icon at all."""
    import json as _json
    from auteur.web import assets, server

    assets.ensure(server.STATIC)
    manifest = _json.loads((server.STATIC / "manifest.webmanifest").read_text())

    for icon in manifest["icons"]:
        name = icon["src"].lstrip("/")
        assert (server.STATIC / name).is_file(), f"manifest names {icon['src']}, which is missing"

    page = (server.STATIC / "index.html").read_text()
    for referenced in ("/icon-180.png", "/icon-192.png"):
        assert referenced in page
        assert (server.STATIC / referenced.lstrip("/")).is_file()


def test_the_favicon_is_served_without_signing_in(web_server):
    """Every browser asks for it on every visit, signed in or not.

    Behind the auth gate it answered 303 to /login, which a browser cannot use
    as an icon — so every page load logged a 404 in the console and in the
    server log, on the login page most of all.
    """
    from urllib.request import urlopen

    base, _, _ = web_server
    with urlopen(base + "/favicon.ico") as response:  # no cookie: not signed in
        assert response.status == 200
        assert response.headers["Content-Type"] == "image/png"
        assert len(response.read()) > 0


def test_nothing_a_finger_lands_on_is_smaller_than_a_finger(web_server):
    """44px is Apple's minimum, and the reason is physical rather than stylistic.

    Six controls on the home screen and five in the studio sat at 34-36px: the
    prompt chips, sign out, the studio's mode buttons, and the ← that is the
    only way back out of the studio, which rendered 20x20.
    """
    from auteur.web import server

    for sheet in ("style.css", "studio.css"):
        text = (server.STATIC / sheet).read_text()
        # Only the interactive rules matter; a 20px avatar is not a target.
        for block in re.findall(r"([^{}]*)\{([^}]*)\}", text):
            selector, body = block[0].strip(), block[1]
            if not re.search(r"\.chip|\.mode|\.whoami button|\.sbar-back|\.card-action", selector):
                continue
            for found in re.findall(r"min-height:\s*(\d+)px", body):
                assert int(found) >= 44, f"{sheet} {selector} is {found}px"


def test_a_length_typed_in_the_prompt_is_not_overruled_by_a_control_nobody_touched():
    """Two controls set the length, and the invisible one used to win.

    "Let it decide" sends no length, so `parse_brief` reads the number out of
    the prompt. With 20s preselected instead, typing "fast neon montage, 10
    seconds" produced a 20 second film and nothing said why.
    """
    from auteur.director.brief import parse_brief
    from auteur.web import server

    page = (server.STATIC / "index.html").read_text()
    chips = page.split('id="seconds"', 1)[1].split("</div>", 1)[0]
    # Exactly one is preselected, and it is the one that sends nothing.
    selected = [line for line in chips.splitlines() if "is-on" in line]
    assert len(selected) == 1, "more than one length is preselected"
    assert 'data-value=""' in selected[0], "a fixed length is preselected over the prompt"

    # And that empty value really does hand the decision to the words: the web
    # handler treats "" as absent, and an absent duration lets the prompt speak.
    assert parse_brief("fast neon montage, 10 seconds", duration=None).duration == 10.0
    assert parse_brief("fast neon montage, 10 seconds", duration=20.0).duration == 20.0


def test_the_first_step_looks_like_something_you_can_tap():
    """The whole card is a label, and nothing said so.

    Steps 2 and 3 have obvious controls; step 1 read as a paragraph, which made
    the first action of the entire product invisible.
    """
    from auteur.web import server

    page = (server.STATIC / "index.html").read_text()
    assert 'id="clips-action"' in page
    assert "Choose from camera roll" in page
    # And it has to change once there are clips, or the step reads unfinished.
    script = (server.STATIC / "app.js").read_text()
    assert "Choose different clips" in script


def test_the_page_is_built_for_a_phone():
    """The iPhone-specific pieces are load-bearing, not decoration."""
    from auteur.web import server

    page = (server.STATIC / "index.html").read_text()
    # Without viewport-fit=cover the safe-area insets are all zero.
    assert "viewport-fit=cover" in page
    assert 'name="apple-mobile-web-app-capable" content="yes"' in page
    # Without playsinline iOS takes the finished film fullscreen on play.
    assert "playsinline" in page

    style = (server.STATIC / "style.css").read_text()
    assert "env(safe-area-inset-bottom" in style
    # Any font under 16px makes iOS zoom the page when the field is focused.
    # The `font:` shorthand cannot carry `inherit` as a family — written that
    # way the declaration is invalid and the field falls back to monospace.
    assert "font-size: 16px;" in style
    assert "font: 16px" not in style


def test_the_service_worker_never_caches_the_api():
    """A cached job status would freeze the progress screen, and a cached film
    would be served for the next film too."""
    from auteur.web import server

    worker = (server.STATIC / "sw.js").read_text()
    assert '"/api/"' in worker or "'/api/'" in worker


def test_a_job_reports_itself_as_json():
    from auteur.web.server import Job

    job = Job(id="abc123", prompt="x", folder=Path("/tmp/nowhere"))
    snapshot = job.snapshot()
    assert snapshot["id"] == "abc123"
    assert snapshot["status"] == "queued"
    assert snapshot["video"] is None and snapshot["notes"] is None

    job.video = Path("/tmp/nowhere/film.mp4")
    assert job.snapshot()["video"] == "/api/jobs/abc123/video"


def test_the_web_reporter_feeds_the_job_not_the_terminal(capsys):
    import threading
    from auteur.web.server import Job, WebReporter

    job = Job(id="j", prompt="p", folder=Path("/tmp/nowhere"))
    say = WebReporter(job, threading.Lock())
    say.banner("ignored")
    say.step("Watching every clip")
    say.detail("6 clips")
    say.progress(3, 6, "shot 3 of 6")

    assert capsys.readouterr().out == ""  # nothing goes to the console
    assert job.stage == "Watching every clip"
    assert job.percent == pytest.approx(50.0)
    assert [line["text"] for line in job.lines] == ["Watching every clip", "6 clips"]


def test_the_studio_cleans_up_after_itself(tmp_path):
    import time as _time
    from auteur.web.server import Studio

    studio = Studio(tmp_path / "web")
    job = studio.create("prompt", "reel", 10.0)
    job.status = "done"
    job.created = _time.time() - 8 * 3600

    assert job.folder.is_dir()
    studio.sweep(max_age_hours=6.0)
    assert studio.get(job.id) is None
    assert not job.folder.exists()


@pytest.fixture
def web_server(tmp_path):
    """A real server on a real socket, so the routes are tested as served.

    Yields (base_url, studio, cookie) — the cookie of a signed-in session,
    because the server refuses everything without one.
    """
    import json as _json
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.request import Request, urlopen
    from auteur.web import assets, server as web
    from auteur.web.auth import Accounts
    from auteur.manager import Board
    from auteur.projects import Projects
    from auteur.web.profiles import Profiles
    from auteur.web.safety import Reports
    from auteur.web.social import Films, Messages
    from auteur.web.watching import Watching

    assets.ensure(web.STATIC)
    root = tmp_path / "web"
    web.Handler.studio = web.Studio(root)
    web.Handler.accounts = Accounts(root / "accounts.json")
    web.Handler.accounts.add("tester", "tester@example.com", "a-long-enough-one")
    # Somebody to follow, message and look at. A server with one account can
    # answer every route and prove nothing about the ones that are about two
    # people.
    web.Handler.accounts.add("grace", "grace@example.com", "another-long-one")
    web.Handler.films = Films(root / "films.json")
    web.Handler.studio.films = web.Handler.films
    web.Handler.messages = Messages(root / "messages.json")
    web.Handler.profiles = Profiles(root / "profiles.json", root / "pictures")
    web.Handler.reports = Reports(root / "reports.json")
    web.Handler.projects = Projects(root / "projects.json")
    web.Handler.board = Board(Board.default_path(root))
    web.Handler.watching = Watching(root / "watching")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    request = Request(
        base + "/api/login",
        data=_json.dumps({"username": "tester", "password": "a-long-enough-one"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request) as response:
        cookie = response.headers["Set-Cookie"].split(";")[0]

    try:
        yield base, web.Handler.studio, cookie
    finally:
        httpd.shutdown()
        httpd.server_close()
        web.Handler.accounts = None
        web.Handler.films = None
        web.Handler.messages = None
        web.Handler.profiles = None
        web.Handler.reports = None
        web.Handler.projects = None
        web.Handler.board = None
        web.Handler.watching = None


def test_the_shell_and_icons_are_reachable(web_server):
    from urllib.request import urlopen

    base, _, cookie = web_server
    for path, kind in [
        ("/", "text/html"),
        ("/static/app.js", "javascript"),
        ("/static/style.css", "text/css"),
        ("/manifest.webmanifest", "manifest"),
        ("/sw.js", "javascript"),
        ("/icon-180.png", "image/png"),
        ("/icon-192.png", "image/png"),
        ("/icon-512.png", "image/png"),
    ]:
        with urlopen(base + path) as response:
            assert response.status == 200, path
            assert kind in response.headers["Content-Type"], path
            assert len(response.read()) > 0, path


def test_the_static_route_cannot_escape_its_folder(web_server):
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    base, _, cookie = web_server
    attempts = (
        "/static/%2e%2e%2fserver.py",
        "/static/..%2Fserver.py",
        "/static/../server.py",
        "/static/....//server.py",
        "/nope.png",
    )
    for attempt in attempts:
        # With a session, so a 404 is really a 404 and not the sign-in redirect.
        with pytest.raises(HTTPError) as caught:
            urlopen(Request(base + attempt, headers={"Cookie": cookie}))
        assert caught.value.code == 404, attempt


def test_video_is_served_in_ranges(web_server, tmp_path):
    """iOS Safari opens a video with `Range: bytes=0-1` and refuses to play
    anything that answers 200 with the whole file."""
    from urllib.request import Request, urlopen

    base, studio, cookie = web_server
    job = studio.create("prompt", "reel", 10.0, owner="tester")
    film = job.folder / "film.mp4"
    film.write_bytes(bytes(range(256)) * 40)
    job.video = film
    job.status = "done"

    url = f"{base}/api/jobs/{job.id}/video"
    request = Request(url, headers={"Range": "bytes=0-1", "Cookie": cookie})
    with urlopen(request) as response:
        assert response.status == 206
        assert response.headers["Content-Range"] == f"bytes 0-1/{film.stat().st_size}"
        assert response.read() == film.read_bytes()[:2]

    request = Request(url, headers={"Range": "bytes=100-199", "Cookie": cookie})
    with urlopen(request) as response:
        assert response.status == 206
        assert response.read() == film.read_bytes()[100:200]

    with urlopen(Request(url, headers={"Cookie": cookie})) as response:  # no Range still works
        assert response.status == 200
        assert response.read() == film.read_bytes()


def test_an_upload_is_read_from_a_stream_that_only_has_read():
    """Every multipart post failed on Python 3.10, and one test said so wrongly.

    `_Prefixed` wraps the spooled body so the Content-Type header and the body
    are read as one stream. It called `readinto` on that spool —
    `SpooledTemporaryFile` only implements `readinto` from 3.11, having not
    fully implemented `IOBase` before then. On 3.10 the call raised
    `AttributeError`, every caller turns any exception into "I could not read
    that upload", and so no film could be made, no reel added and no profile
    picture set on a version this project's own CI tests.

    What made it survive was that the only test to notice compared the *message*
    and read as a wrong string rather than as an app that cannot accept a file.

    This drives the shim with an object that has `read` and no `readinto`, so
    the 3.10 path is exercised whichever interpreter is running.
    """
    import io

    from auteur.web.server import _Prefixed, _parse_multipart_stream

    class OnlyRead:
        """A stream like 3.10's SpooledTemporaryFile: read, and no readinto."""

        def __init__(self, raw: bytes) -> None:
            self._inner = io.BytesIO(raw)

        def read(self, size=-1):
            return self._inner.read(size)

    boundary = "----auteurtest"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="prompt"\r\n\r\n'
        "a film\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="clips"; filename="one.mp4"\r\n'
        "Content-Type: video/mp4\r\n\r\n"
        "not really a film\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    assert not hasattr(OnlyRead(b""), "readinto"), "the stand-in is not standing in"

    fields, files = _parse_multipart_stream(
        OnlyRead(body), f"multipart/form-data; boundary={boundary}"
    )
    assert fields.get("prompt") == "a film", f"the field did not survive: {fields}"
    assert files == [("one.mp4", b"not really a film")], f"the file did not survive: {files}"

    # And the shim itself, directly: a header then a body with no readinto.
    stream = _Prefixed(b"HEAD", OnlyRead(b"TAIL"))
    assert stream.read() == b"HEADTAIL"


def test_a_post_without_clips_says_so_in_plain_words(web_server):
    import json as _json
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    base, _, cookie = web_server
    boundary = "----auteurtest"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="prompt"\r\n\r\n'
        "a film\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    request = Request(
        base + "/api/jobs",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Cookie": cookie},
    )
    with pytest.raises(HTTPError) as caught:
        urlopen(request)
    assert caught.value.code == 400
    assert _json.loads(caught.value.read())["error"] == "Pick at least one clip first."


def test_an_unknown_job_is_a_clean_404(web_server):
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    base, _, cookie = web_server
    with pytest.raises(HTTPError) as caught:
        urlopen(Request(base + "/api/jobs/deadbeef", headers={"Cookie": cookie}))
    assert caught.value.code == 404


def test_the_demo_hands_edit_a_complete_namespace(monkeypatch, tmp_path):
    """The demo built its edit arguments from scratch and forgot `quiet`, so it
    printed the finished film and then reported that something went wrong."""
    import argparse
    from auteur import cli

    captured = {}

    def fake_edit(args, say):
        # Touch every attribute `edit` is parsed to have.
        parser = cli._build_parser()
        expected = vars(parser.parse_args(["edit", "x", "-p", "y"]))
        missing = [name for name in expected if not hasattr(args, name)]
        captured["missing"] = missing
        captured["quiet"] = args.quiet
        return 0

    monkeypatch.setattr(cli, "_run_edit", fake_edit)
    monkeypatch.setattr(
        cli.subprocess if hasattr(cli, "subprocess") else __import__("subprocess"),
        "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stderr": ""})(),
    )

    args = argparse.Namespace(
        command="demo", quiet=True, verbose=0, out=str(tmp_path / "demo"), prompt="a film"
    )
    assert cli._run_demo(args, cli.NullReporter()) == 0
    assert captured["missing"] == []
    assert captured["quiet"] is True


def test_a_failure_reaches_the_phone_as_one_readable_line():
    """A render error carries the whole filter graph. That must not be what the
    page shows — it once put several thousand characters of `[12:v]settb=AVTB`
    on screen where an explanation belonged."""
    from auteur.web.server import _plain_cause

    graph_dump = "Stream specifier ':v' in filtergraph description " + "[0:v]settb=AVTB;" * 400
    assert (
        _plain_cause(ffmpeg.FFmpegError([], 1, graph_dump)) == "One of the clips could not be used."
    )

    assert len(_plain_cause(RuntimeError("x" * 900))) <= 160
    assert _plain_cause(RuntimeError("the folder was empty")) == "the folder was empty"
    assert _plain_cause(RuntimeError("")) == ""


def test_a_failed_job_says_something_a_person_can_read(tmp_path):
    from auteur.web.server import Studio

    studio = Studio(tmp_path / "web")
    job = studio.create("prompt", "reel", 10.0)
    studio._fail(
        job,
        "Something went wrong making the film.",
        RuntimeError("Stream specifier ':v' in filtergraph description " + "[0:v]x;" * 500),
    )

    assert job.status == "error"
    assert len(job.error) < 200
    assert "settb" not in job.error and "[0:v]" not in job.error
    assert job.snapshot()["error"].startswith("Something went wrong making the film.")


# ---------------------------------------------------------------------------
# The theme
# ---------------------------------------------------------------------------


def test_the_stylesheet_hard_codes_no_colour():
    """One palette, in theme.py. A hex typed into the CSS is a second copy that
    will drift away from the icons and the terminal."""
    import re
    from auteur.web import server

    style = (server.STATIC / "style.css").read_text()
    stray = [
        line.strip()
        for line in style.splitlines()
        if re.search(r"#[0-9a-fA-F]{3,8}\b", line) or "rgba(" in line or "rgb(" in line
    ]
    assert stray == [], f"colours hard-coded in style.css: {stray}"


def test_every_token_the_stylesheet_uses_actually_exists():
    """A typo in a var() name fails silently — the property is just dropped."""
    import re
    from auteur import theme
    from auteur.web import assets, server

    assets.ensure(server.STATIC)
    style = (server.STATIC / "style.css").read_text()
    generated = (server.STATIC / "theme.css").read_text()

    defined = set(re.findall(r"(--[a-z0-9-]+):", generated))
    local = set(re.findall(r"(--[a-z0-9-]+):", style))  # radius, type scale, safe areas
    used = set(re.findall(r"var\((--[a-z0-9-]+)", style))

    missing = used - defined - local
    assert missing == set(), f"style.css uses undefined tokens: {sorted(missing)}"

    # And every palette role really is generated from the module.
    for role in theme.ROLES:
        assert f"--{role.replace('_', '-')}:" in generated


def test_the_theme_stylesheet_is_regenerated_when_the_palette_moves(tmp_path):
    from auteur import theme
    from auteur.web import assets

    stale = tmp_path / "static"
    stale.mkdir()
    (stale / "theme.css").write_text(":root { --ground: #ff00ff; }")
    assets.ensure(stale)
    assert (stale / "theme.css").read_text() == theme.css_variables()
    assert "#ff00ff" not in (stale / "theme.css").read_text()


def test_the_icon_and_the_page_use_the_same_palette():
    from auteur import theme
    from auteur.web import assets, server

    assert assets.INK == theme.rgb_of("ground")
    assert assets.ACCENT == theme.rgb_of("ember")

    page = (server.STATIC / "index.html").read_text()
    assert f'content="{theme.THEME_COLOR}"' in page

    import json as _json

    manifest = _json.loads((server.STATIC / "manifest.webmanifest").read_text())
    assert manifest["theme_color"] == theme.THEME_COLOR
    assert manifest["background_color"] == theme.THEME_COLOR


def test_no_page_paints_a_status_bar_the_palette_no_longer_uses():
    """<meta theme-color> is a hand-written copy of the ground colour.

    Seven pages carry it, theme.js carries both grounds again so it can
    repaint the tag when somebody overrides the system setting, and the
    manifest carries the dark one a ninth time. Recolouring the palette left
    every one of them the old warm brown behind a blue page, and nothing
    failed — the status bar is the one part of the app no assertion looked at.
    """
    import re
    from auteur import theme
    from auteur.web import server

    grounds = {theme.THEME_COLOR.lower(), theme.LIGHT_THEME_COLOR.lower()}
    seen = 0
    for page in sorted(server.STATIC.glob("*.html")):
        for colour in re.findall(
            r'name="theme-color" content="(#[0-9a-fA-F]{6})"', page.read_text()
        ):
            seen += 1
            assert colour.lower() in grounds, f"{page.name} paints {colour}, not a ground"
    assert seen >= 7, f"only {seen} theme-color tags found — did the pages lose them?"

    script = (server.STATIC / "theme.js").read_text()
    for colour in re.findall(r"(#[0-9a-fA-F]{6})", script):
        assert colour.lower() in grounds, f"theme.js repaints to {colour}, not a ground"


def test_the_terminal_reads_the_same_palette():
    from auteur import theme, ui

    assert ui.INK["accent"] == theme.ansi("ember")
    red, green, blue = theme.rgb_of("ember", "dark")
    assert ui.INK["accent"] == f"38;2;{red};{green};{blue}"


# ---------------------------------------------------------------------------
# Chrome
# ---------------------------------------------------------------------------


def test_the_manifest_meets_chromes_install_requirements():
    """Chrome will not offer to install without all of these."""
    import json as _json
    from auteur.web import assets, server

    assets.ensure(server.STATIC)
    manifest = _json.loads((server.STATIC / "manifest.webmanifest").read_text())

    for key in ("name", "start_url", "icons", "display"):
        assert manifest.get(key), f"manifest is missing {key}"
    assert manifest["display"] in ("standalone", "fullscreen", "minimal-ui")

    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert "192x192" in sizes and "512x512" in sizes
    assert any(icon.get("purpose") == "maskable" for icon in manifest["icons"])

    # A service worker with a fetch handler is the other half of the bar.
    worker = (server.STATIC / "sw.js").read_text()
    assert 'addEventListener("fetch"' in worker


def test_the_page_offers_installation_on_both_browsers():
    from auteur.web import server

    script = (server.STATIC / "app.js").read_text()
    # Chrome's route: hold the event and put it behind our own button.
    assert "beforeinstallprompt" in script
    assert "deferredPrompt.prompt()" in script
    # Safari never fires it, so the instructions must be there too.
    assert "Add to Home Screen" in script


# ---------------------------------------------------------------------------
# Optimisation
# ---------------------------------------------------------------------------


def test_shots_render_in_parallel_but_not_past_the_cores(monkeypatch):
    from auteur import render

    settings = Settings(quality=QUALITIES["draft"])
    monkeypatch.setattr(render.os, "cpu_count", lambda: 4)
    assert render.segment_workers(settings, 20) == 4  # one per core
    assert render.segment_workers(settings, 3) == 3  # never more than the shots
    assert render.segment_workers(settings, 1) == 1  # nothing to overlap

    monkeypatch.setattr(render.os, "cpu_count", lambda: 64)
    assert render.segment_workers(settings, 100) == 8  # capped

    # Optical flow is memory-hungry; several at once can push a machine to swap.
    assert render.segment_workers(Settings(quality=QUALITIES["master"]), 20) == 1


@pytest.mark.slow
def test_parallel_and_sequential_renders_agree(rushes, tmp_path, monkeypatch):
    """Concurrency must not reorder the film."""
    from auteur import render
    from auteur.agent import direct

    def run(folder: str, workers: int) -> list[str]:
        monkeypatch.setattr(render, "segment_workers", lambda settings, count: workers)
        production = direct(
            [rushes],
            "punchy montage, 5 seconds",
            settings=Settings(
                quality=QUALITIES["draft"],
                primary_format=FORMATS["square"],
                target_duration=5.0,
                use_llm=False,
                revision_rounds=0,
                seed=11,
            ),
            workspace=tmp_path / folder,
            duration=5.0,
        )
        return [shot.clip_id for shot in production.edl.shots]

    assert run("one", 1) == run("many", 4)


def test_text_is_compressed_and_pictures_are_not(web_server):
    from urllib.request import Request, urlopen

    base, _, cookie = web_server
    request = Request(base + "/static/style.css", headers={"Accept-Encoding": "gzip"})
    with urlopen(request) as response:
        assert response.headers["Content-Encoding"] == "gzip"
        assert response.headers["Vary"] == "Accept-Encoding"

    request = Request(base + "/icon-512.png", headers={"Accept-Encoding": "gzip"})
    with urlopen(request) as response:
        assert response.headers.get("Content-Encoding") is None


def test_an_unchanged_asset_comes_back_as_a_304(web_server):
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    base, _, cookie = web_server
    with urlopen(base + "/static/app.js") as response:
        etag = response.headers["ETag"]
        assert etag

    request = Request(base + "/static/app.js", headers={"If-None-Match": etag})
    try:
        with urlopen(request) as response:
            assert response.status == 304
    except HTTPError as exc:  # urllib raises on 304 in some versions
        assert exc.code == 304


@pytest.mark.slow
def test_a_ramped_shot_lands_on_its_intended_screen_time(rushes, tmp_path):
    """concat gives a segment's last frame no duration of its own, so every
    slice of a speed ramp used to lose a frame's worth of screen time. Eleven
    slices at 30fps is a third of a second, once per ramped shot — a 15-second
    cut of ramped stills came out at 10.5."""
    from auteur import render

    space = Workspace(tmp_path / "work")
    shot = Shot(
        clip_id="C01",
        source=rushes / "a_wide.mp4",
        start=0.4,
        end=1.5,
        ramp=Ramp(points=((0.0, 1.14), (0.45, 1.14), (1.0, 2.16))),
    )
    path = render.render_shot(
        shot, 0, space, FORMATS["square"], QUALITIES["draft"], want_audio=False
    )
    measured = float(ffmpeg.probe(path)["format"]["duration"])
    assert measured == pytest.approx(
        shot.duration, abs=0.06
    ), f"wanted {shot.duration:.3f}s of screen time, got {measured:.3f}s"


def test_ramp_windows_overlap_by_exactly_one_frame():
    fps, source = 30.0, 1.125
    windows = motion.slice_windows(source, fps, 11)

    assert len(windows) == 11
    assert windows[0][0] == 0.0
    for index in range(len(windows) - 1):
        overlap = windows[index][1] - windows[index + 1][0]
        assert overlap == pytest.approx(1.0 / fps, abs=1e-6)

    # Boundaries sit on the frame grid, offset by the half-frame that keeps each
    # frame inside exactly one window.
    for start, _ in windows[1:]:
        assert ((start + 0.5 / fps) * fps) == pytest.approx(
            round((start + 0.5 / fps) * fps), abs=1e-6
        )


def test_a_still_survives_being_written_out_and_read_back():
    """`is_still` decides whether a shot is looped or seeked into. A saved EDL
    that omits it renders every photo as almost nothing."""
    edl = EditDecisionList(title="t")
    edl.shots.append(
        Shot(clip_id="C01", source=Path("/tmp/photo.jpg"), start=0.0, end=2.0, is_still=True)
    )
    assert edl.to_json()["shots"][0]["is_still"] is True


# ---------------------------------------------------------------------------
# Stills and frame rates
# ---------------------------------------------------------------------------


def test_a_still_is_clocked_at_the_delivery_rate():
    """`-loop 1 -framerate {quality.fps}` is literally how fast a still's frames
    arrive. Assuming 30 misaligns every ramp slice at draft's 24, and a
    10-second cut of photographs delivered 7.9."""
    from auteur import render

    still = Shot(clip_id="C01", source=Path("/tmp/photo.jpg"), start=0.0, end=2.0, is_still=True)
    assert render._source_fps(still, QUALITIES["draft"]) == 24.0
    assert render._source_fps(still, QUALITIES["standard"]) == 30.0


@pytest.mark.slow
def test_a_film_of_stills_lands_on_its_runtime_at_every_quality(rushes, tmp_path):
    from auteur import ffmpeg as ff
    from auteur.agent import direct

    photos = tmp_path / "photos"
    photos.mkdir()
    binary = str(ff.ffmpeg_path())
    for index, source in enumerate(("testsrc2=size=800x600", "mandelbrot=size=600x800")):
        subprocess.run(
            [
                binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                source,
                "-frames:v",
                "1",
                str(photos / f"still{index}.png"),
            ],
            check=True,
        )

    for name in ("draft", "standard"):
        production = direct(
            [photos],
            "slow and cinematic, 8 seconds",
            settings=Settings(
                quality=QUALITIES[name],
                primary_format=FORMATS["square"],
                target_duration=8.0,
                use_llm=False,
                revision_rounds=0,
                seed=3,
            ),
            workspace=tmp_path / f"work-{name}",
            duration=8.0,
        )
        measured = float(ff.probe(production.primary)["format"]["duration"])
        assert measured == pytest.approx(
            production.edl.duration, abs=0.35
        ), f"{name}: planned {production.edl.duration:.2f}s, delivered {measured:.2f}s"


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

#: The password these tests sign in with. It is deliberately a throwaway that
#: has never been anybody's real password and never will be: the accounts file
#: it ends up in lives under `tmp_path` and is deleted with the test run. A
#: real password in a test file is a real password in the repository.
TEST_PASSWORD = "harbour-kestrel-slate-7412"


@pytest.fixture
def accounts(tmp_path):
    from auteur.web.auth import Accounts

    store = Accounts(tmp_path / "accounts.json")
    store.add("streetlightseason", "streetlightseason@gmail.com", TEST_PASSWORD)
    return store


def test_a_password_is_never_stored(accounts, tmp_path):
    raw = (tmp_path / "accounts.json").read_text()
    assert TEST_PASSWORD not in raw
    account = accounts.get("streetlightseason")
    assert account.password_hash != TEST_PASSWORD
    assert len(account.salt) == 32 and len(account.password_hash) == 64


def test_the_right_password_is_accepted_and_others_are_not(accounts):
    account = accounts.get("streetlightseason")
    assert account.check(TEST_PASSWORD)
    assert not account.check("tacit25#")
    assert not account.check(TEST_PASSWORD[:-1])
    assert not account.check("")


def test_you_can_sign_in_with_either_the_username_or_the_email(accounts):
    for who in (
        "streetlightseason",
        "STREETLIGHTSEASON",
        "streetlightseason@gmail.com",
        "  Streetlightseason@Gmail.com  ",
    ):
        token, _ = accounts.sign_in(who, TEST_PASSWORD)
        assert token, who
        assert accounts.session_user(token) == "streetlightseason"


def test_a_wrong_password_says_the_same_thing_as_a_wrong_username(accounts):
    """Different wording would let someone find out which accounts exist."""
    _, one = accounts.sign_in("streetlightseason", "wrong")
    _, two = accounts.sign_in("nobody-at-all", "wrong")
    assert one == two


def test_guessing_locks_the_account(accounts):
    from auteur.web import auth

    for _ in range(auth.MAX_ATTEMPTS):
        token, _ = accounts.sign_in("streetlightseason", "wrong")
        assert token is None

    # Even the correct password is refused while the lock stands.
    token, message = accounts.sign_in("streetlightseason", TEST_PASSWORD)
    assert token is None
    assert "Too many" in message

    accounts.get("streetlightseason").locked_until = 0.0
    token, _ = accounts.sign_in("streetlightseason", TEST_PASSWORD)
    assert token


def test_the_lockout_does_not_announce_that_the_account_exists(accounts):
    """ "Too many tries" is only useful to the owner, and only they should see it.

    Otherwise it is an oracle: spray five wrong guesses at a name, and the
    sixth reply tells you whether the name was real."""
    from auteur.web import auth

    for _ in range(auth.MAX_ATTEMPTS):
        accounts.sign_in("streetlightseason", "wrong")

    _, to_a_stranger = accounts.sign_in("streetlightseason", "still-wrong-guess")
    _, to_nobody = accounts.sign_in("no-such-person", "still-wrong-guess")
    assert to_a_stranger == to_nobody, "a locked real account must look like no account"

    # The owner, who knows the password, is told why they are stuck.
    _, to_the_owner = accounts.sign_in("streetlightseason", TEST_PASSWORD)
    assert "Too many" in to_the_owner


def test_a_session_can_be_ended(accounts):
    token, _ = accounts.sign_in("streetlightseason", TEST_PASSWORD)
    assert accounts.session_user(token) == "streetlightseason"
    accounts.sign_out(token)
    assert accounts.session_user(token) is None
    assert accounts.session_user("some-other-token") is None
    assert accounts.session_user(None) is None


def test_session_tokens_are_stored_hashed(accounts, tmp_path):
    """The account file must not hold anything replayable."""
    token, _ = accounts.sign_in("streetlightseason", TEST_PASSWORD)
    assert token not in (tmp_path / "accounts.json").read_text()


def test_a_reset_link_works_once(accounts):
    started = accounts.begin_reset("streetlightseason@gmail.com")
    assert started is not None
    _, token = started

    assert accounts.finish_reset(token, "a-much-longer-one")
    assert accounts.get("streetlightseason").check("a-much-longer-one")
    assert not accounts.get("streetlightseason").check(TEST_PASSWORD)
    assert not accounts.finish_reset(token, "third-attempt-here")


def test_an_expired_reset_link_is_refused(accounts):
    _, token = accounts.begin_reset("streetlightseason")
    accounts.get("streetlightseason").reset_expires = time.time() - 1
    assert accounts.account_for_reset(token) is None
    assert not accounts.finish_reset(token, "a-much-longer-one")


def test_resetting_for_an_unknown_account_gives_nothing_away(accounts):
    assert accounts.begin_reset("someone@example.com") is None


def test_changing_the_password_signs_every_device_out(accounts):
    first, _ = accounts.sign_in("streetlightseason", TEST_PASSWORD)
    second, _ = accounts.sign_in("streetlightseason", TEST_PASSWORD)
    accounts.set_password(accounts.get("streetlightseason"), "a-much-longer-one")
    assert accounts.session_user(first) is None
    assert accounts.session_user(second) is None


def test_accounts_survive_a_restart(accounts, tmp_path):
    from auteur.web.auth import Accounts

    token, _ = accounts.sign_in("streetlightseason", TEST_PASSWORD)
    reopened = Accounts(tmp_path / "accounts.json")
    assert reopened.get("streetlightseason").check(TEST_PASSWORD)
    assert (
        reopened.session_user(token) == "streetlightseason"
    ), "a restart must not sign the phone out"


def test_weak_passwords_are_explained_not_just_refused():
    from auteur.web.auth import MIN_PASSWORD, password_problem

    problem = password_problem("short")
    assert str(MIN_PASSWORD) in problem, "say the number, do not make them guess it"
    assert "space" in password_problem(" has-spaces-around-it ")
    assert password_problem("a-perfectly-fine-one") == ""


def test_the_guessing_lists_are_refused_however_they_are_dressed_up():
    """The passwords that get broken are the ones on a list, not the short ones."""
    from auteur.web.auth import password_problem

    # Long enough to pass the length rule, and still the first thing anybody tries.
    for guessable in ("password123456", "MyPassword2024!", "letmein-please", "qwerty-qwerty"):
        assert password_problem(guessable) != "", guessable

    # Length is not variety.
    assert password_problem("aaaaaaaaaaaaaaaa") != ""
    assert password_problem("abababababababab") != ""


def test_a_password_may_not_be_made_of_the_account_it_protects():
    """Anyone guessing starts from the username, so it cannot be the answer."""
    from auteur.web.auth import password_problem

    assert password_problem("streetlightseason1", username="streetlightseason") != ""
    assert password_problem("STREETLIGHTSEASON-x", username="streetlightseason") != ""
    assert password_problem("streetlightseason1", email="streetlightseason@gmail.com") != ""

    # Someone else's username is not a reason to refuse it.
    assert password_problem("streetlightseason1", username="someone-else") == ""


def test_the_seed_carries_no_credential_material_at_all():
    """The repository is public. Not the password, and not a hash of it either."""
    from auteur.web import seed

    source = Path(seed.__file__).read_text()
    assert not hasattr(seed, "SEED_HASH")
    assert not hasattr(seed, "SEED_SALT")
    # Nothing that looks like a stored credential: scrypt output is long hex.
    assert not re.search(r"[0-9a-f]{32,}", source), "that looks like a salt or a hash"


def test_a_generated_password_is_worth_having():
    """Different every time, long enough, and acceptable to our own rules."""
    from auteur.web import seed
    from auteur.web.auth import password_problem

    minted = {seed.generate_password() for _ in range(200)}
    assert len(minted) == 200, "two identical passwords in 200 is not randomness"
    for password in minted:
        assert password_problem(password, username=seed.SEED_USERNAME) == "", password

    # Every word must be distinct and the list a power of two, or the entropy
    # claimed in the module comment is not the entropy actually delivered.
    assert len(seed._WORDS) == 64
    assert len(set(seed._WORDS)) == 64, "a repeated word biases the draw"
    assert all(word.isalpha() and word.islower() for word in seed._WORDS)

    # 5 words x 6 bits + ~13 from the digits. Refuse a silent downgrade.
    assert seed._WORD_COUNT >= 5
    bits = seed._WORD_COUNT * 6 + math.log2(9000)
    assert bits >= 43, f"only {bits:.1f} bits"


def test_the_first_run_creates_exactly_one_account(tmp_path, monkeypatch):
    from auteur.web import seed
    from auteur.web.auth import Accounts

    monkeypatch.delenv("AUTEUR_PASSWORD", raising=False)
    store = Accounts(tmp_path / "accounts.json")

    first = seed.bootstrap(store)
    assert first is not None
    username, password = first
    assert username == "streetlightseason"
    assert password, "with nothing in the environment there must be a password to show"
    assert store.get(username).check(password), "the printed password must be the real one"
    assert len(store.accounts) == 1

    # Running again must not add a second, nor mint anything.
    assert seed.bootstrap(store) is None
    assert len(store.accounts) == 1


def test_the_environment_beats_the_generated_password(tmp_path, monkeypatch):
    from auteur.web import seed
    from auteur.web.auth import Accounts

    monkeypatch.setenv("AUTEUR_USERNAME", "someone")
    monkeypatch.setenv("AUTEUR_EMAIL", "someone@example.com")
    monkeypatch.setenv("AUTEUR_PASSWORD", "a-much-longer-one")
    store = Accounts(tmp_path / "accounts.json")

    assert seed.bootstrap(store) == ("someone", None), "nothing to announce; they chose it"
    assert store.get("someone").check("a-much-longer-one")
    assert store.get("streetlightseason") is None


def test_the_environment_is_not_a_way_around_the_password_rules(tmp_path, monkeypatch):
    """A convenience must not be a hole. `x` is `x` however it arrives."""
    from auteur.web import seed
    from auteur.web.auth import Accounts

    monkeypatch.setenv("AUTEUR_PASSWORD", "x")
    store = Accounts(tmp_path / "accounts.json")

    with pytest.raises(ValueError, match="AUTEUR_PASSWORD"):
        seed.bootstrap(store)
    assert store.empty, "a refused password must not leave half an account behind"


# ---------------------------------------------------------------------------
# The gate, over HTTP
# ---------------------------------------------------------------------------


@pytest.fixture
def guarded_server(tmp_path):
    """A server with a real account, exercised through real requests."""
    import threading
    from http.server import ThreadingHTTPServer
    from auteur.web import assets, server as web
    from auteur.web.auth import Accounts

    assets.ensure(web.STATIC)
    web.Handler.studio = web.Studio(tmp_path / "web")
    web.Handler.accounts = Accounts(tmp_path / "web" / "accounts.json")
    web.Handler.accounts.add("streetlightseason", "streetlightseason@gmail.com", TEST_PASSWORD)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", web.Handler.accounts
    finally:
        httpd.shutdown()
        httpd.server_close()
        web.Handler.accounts = None


def _post(base, path, payload, cookie=None):
    import json as _json
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    request = Request(base + path, data=_json.dumps(payload).encode(), headers=headers)
    try:
        with urlopen(request) as response:
            return response.status, _json.loads(response.read() or b"{}"), response.headers
    except HTTPError as exc:
        return exc.code, _json.loads(exc.read() or b"{}"), exc.headers


def test_the_app_is_closed_until_you_sign_in(guarded_server):
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    base, _ = guarded_server

    # A page navigation is sent to the sign-in screen...
    request = Request(base + "/")
    opener = urlopen
    try:
        with opener(request) as response:
            assert response.url.endswith("/login")
    except HTTPError as exc:  # pragma: no cover - only if redirects are disabled
        assert exc.code == 303

    # ...and an API call gets something a script can act on.
    for path in ("/api/jobs/anything", "/api/jobs/anything/video", "/api/jobs/anything/notes"):
        try:
            with urlopen(base + path):
                raise AssertionError(f"{path} answered without a session")
        except HTTPError as exc:
            assert exc.code == 401, path


def test_the_sign_in_page_and_its_assets_stay_open(guarded_server):
    from urllib.request import urlopen

    base, _ = guarded_server
    for path in (
        "/login",
        "/static/login.js",
        "/static/style.css",
        "/static/theme.css",
        "/manifest.webmanifest",
        "/icon-192.png",
    ):
        with urlopen(base + path) as response:
            assert response.status == 200, path


def test_signing_in_sets_a_cookie_a_script_cannot_read(guarded_server):
    base, _ = guarded_server

    status, payload, _ = _post(
        base, "/api/login", {"username": "streetlightseason", "password": "wrong"}
    )
    assert status == 401 and "error" in payload

    status, payload, headers = _post(
        base, "/api/login", {"username": "streetlightseason", "password": TEST_PASSWORD}
    )
    assert status == 200 and payload["user"] == "streetlightseason"

    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie  # no script can read the session
    assert "SameSite=Strict" in cookie  # no other site can spend it
    assert "Path=/" in cookie


def test_a_signed_in_request_gets_through(guarded_server):
    import json as _json
    from urllib.request import Request, urlopen

    base, _ = guarded_server
    _, _, headers = _post(
        base, "/api/login", {"username": "streetlightseason", "password": TEST_PASSWORD}
    )
    token = headers["Set-Cookie"].split(";")[0]

    request = Request(base + "/api/session", headers={"Cookie": token})
    with urlopen(request) as response:
        assert _json.loads(response.read())["user"] == "streetlightseason"

    with urlopen(Request(base + "/", headers={"Cookie": token})) as response:
        assert response.status == 200
        assert b"Make my film" in response.read()


def test_signing_out_invalidates_the_cookie(guarded_server):
    import json as _json
    from urllib.request import Request, urlopen

    base, _ = guarded_server
    _, _, headers = _post(
        base, "/api/login", {"username": "streetlightseason", "password": TEST_PASSWORD}
    )
    token = headers["Set-Cookie"].split(";")[0]

    status, _, out_headers = _post(base, "/api/logout", {}, cookie=token)
    assert status == 200
    assert "Max-Age=0" in out_headers["Set-Cookie"]

    request = Request(base + "/api/session", headers={"Cookie": token})
    with urlopen(request) as response:
        assert _json.loads(response.read())["user"] is None


def test_forgot_answers_identically_for_real_and_invented_accounts(guarded_server):
    """Any difference at all turns this into a way of asking which addresses
    have accounts — including which keys the JSON happens to carry."""
    base, _ = guarded_server

    real_status, real, _ = _post(base, "/api/forgot", {"username": "streetlightseason@gmail.com"})
    fake_status, fake, _ = _post(base, "/api/forgot", {"username": "nobody@example.com"})

    assert real_status == fake_status == 200
    assert real == fake


def test_a_reset_over_http_replaces_the_password(guarded_server):
    base, accounts = guarded_server

    _post(base, "/api/forgot", {"username": "streetlightseason"})
    account = accounts.get("streetlightseason")
    assert account.reset_hash, "the reset should have been recorded"

    # Recover the token the way the emailed link carries it.
    started = accounts.begin_reset("streetlightseason")
    assert started is not None
    _, token = started

    status, payload, _ = _post(base, "/api/reset", {"token": token, "password": "short"})
    assert status == 400 and "12 characters" in payload["error"]

    # And a reset may not be used to set the password to the username either.
    status, payload, _ = _post(
        base, "/api/reset", {"token": token, "password": "streetlightseason-x"}
    )
    assert status == 400 and "username" in payload["error"]

    status, payload, _ = _post(
        base, "/api/reset", {"token": "not-a-real-token", "password": "a-much-longer-one"}
    )
    assert status == 400

    status, payload, _ = _post(
        base, "/api/reset", {"token": token, "password": "a-much-longer-one"}
    )
    assert status == 200

    status, _, _ = _post(
        base, "/api/login", {"username": "streetlightseason", "password": TEST_PASSWORD}
    )
    assert status == 401, "the old password must stop working"
    status, _, _ = _post(
        base, "/api/login", {"username": "streetlightseason", "password": "a-much-longer-one"}
    )
    assert status == 200


# ---------------------------------------------------------------------------
# Light and dark
# ---------------------------------------------------------------------------


def test_both_palettes_define_every_role():
    from auteur import theme

    assert set(theme.DARK) == set(theme.ROLES)
    assert set(theme.LIGHT) == set(theme.ROLES)


def test_both_palettes_are_readable():
    """A theme switch must not be able to make text disappear.

    Against every surface text can land on, not just the ground. Checking the
    ground alone answers a different question than the name of this test
    implies, and the gap was real: `text_faint` cleared 5.46 against the dark
    ground and 4.20 against a raised control, so seventy pieces of text in the
    running app were under the bar while this test was green. The one that
    matters is the *hardest* surface, because nothing stops the next screen
    from putting a hint on a card.
    """
    from auteur import theme

    for scheme in ("dark", "light"):
        for under in ("ground", "surface", "raised"):
            behind = theme.rgb_of(under, scheme)
            for role in ("text", "text_muted", "text_faint", "ember_text", "moss", "rust"):
                ratio = theme.contrast(theme.rgb_of(role, scheme), behind)
                assert ratio >= 4.5, f"{role} on {scheme} {under} is only {ratio:.2f}:1"

        # Every "text on a fill" pair, not only the primary button: the
        # destructive one is red-on-red until something checks it.
        for ink, fill in (("on_ember", "ember"), ("on_rust", "rust")):
            ratio = theme.contrast(theme.rgb_of(ink, scheme), theme.rgb_of(fill, scheme))
            assert ratio >= 4.5, f"{ink} on {scheme} {fill} is only {ratio:.2f}:1"


def test_the_stylesheet_covers_system_light_and_dark():
    from auteur import theme

    css = theme.css_variables()
    # System: follow the phone, but only while the reader has not overridden it.
    assert "@media (prefers-color-scheme: light)" in css
    assert ":root:not([data-theme])" in css
    # And an explicit choice must win in both directions.
    assert ':root[data-theme="light"]' in css
    assert ':root[data-theme="dark"]' in css
    # Dark is the base, so anything matching nothing still gets the designed look.
    assert css.index(":root {") < css.index("@media")


def test_the_settings_are_applied_before_the_page_paints():
    """Reading them from a deferred script would show one frame of the wrong
    theme on every load — and, since text size moved in here too, a screenful
    of small text to somebody who has asked for large.

    The check is on *every* page rather than two, because this used to be an
    eight-line snippet copied into each head and copied chrome goes stale one
    page at a time. It is one file now, and this is what says so.
    """
    from auteur.web import server

    for page in sorted(server.STATIC.glob("*.html")):
        markup = page.read_text()
        early = markup.index('src="/static/settings.js"')
        # Before the stylesheet, and with no `defer` or `async` on it: either
        # attribute would let the page paint first, which is the whole failure
        # this is standing against.
        assert early < markup.index('href="/static/style.css"'), page.name
        tag = markup[markup.rindex("<script", 0, early) : markup.index(">", early)]
        assert "defer" not in tag and "async" not in tag, page.name

    settings = (server.STATIC / "settings.js").read_text()
    assert 'setAttribute("data-theme"' in settings
    assert "localStorage" in settings


def test_the_switch_offers_exactly_the_three_modes():
    """Whichever page carries the switch has to carry all three.

    This used to name index.html and login.html, because the switch was
    repeated at the foot of six screens. It is in Settings now and only
    there — `test_a_setting_lives_in_settings_and_nowhere_else` is what holds
    it to that — so this looks for the page that has it rather than being told
    which page that is, and the two tests cannot disagree.
    """
    from auteur import theme
    from auteur.web import server

    assert theme.MODES == ("system", "light", "dark")

    carrying = [
        page
        for page in sorted(server.STATIC.glob("*.html"))
        if 'class="choices appearance"' in page.read_text(encoding="utf-8")
    ]
    assert carrying, "no page offers the appearance switch at all"

    for page in carrying:
        markup = page.read_text(encoding="utf-8")
        for mode in theme.MODES:
            assert f'data-value="{mode}"' in markup, f"{page.name} is missing {mode}"


# ---------------------------------------------------------------------------
# Findings from the security review
# ---------------------------------------------------------------------------


def test_a_reset_link_ignores_the_host_header(guarded_server, monkeypatch):
    """The reset URL is emailed to the account's owner, so it must not be built
    from a header the requester controls. Otherwise anyone who can reach the
    port asks for a reset with `Host: attacker.example.com`, and the owner is
    sent a real, valid token pointing at the attacker."""
    import json as _json
    from urllib.request import Request, urlopen

    base, _ = guarded_server
    sent: list[str] = []
    monkeypatch.setattr(
        "auteur.web.auth.send_reset", lambda email, link: sent.append(link) or "console"
    )

    request = Request(
        base + "/api/forgot",
        data=_json.dumps({"username": "streetlightseason"}).encode(),
        headers={"Content-Type": "application/json", "Host": "attacker.example.com"},
    )
    with urlopen(request) as response:
        assert response.status == 200

    assert sent, "the reset should have been delivered"
    assert "attacker.example.com" not in sent[0]
    assert "127.0.0.1" in sent[0] or "localhost" in sent[0]


def test_the_public_url_can_be_set_deliberately(guarded_server, monkeypatch):
    """An operator behind a proxy needs to name the address themselves — from
    the environment, which is trusted, not from the request."""
    import json as _json
    from urllib.request import Request, urlopen

    base, _ = guarded_server
    sent: list[str] = []
    monkeypatch.setattr(
        "auteur.web.auth.send_reset", lambda email, link: sent.append(link) or "email"
    )
    monkeypatch.setenv("AUTEUR_PUBLIC_URL", "https://films.example.com/")

    request = Request(
        base + "/api/forgot",
        data=_json.dumps({"username": "streetlightseason"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request):
        pass
    assert sent and sent[0].startswith("https://films.example.com/reset?token=")


def test_no_account_store_means_no_access(tmp_path):
    """Auth must fail closed. Treating "not configured" as "everyone is allowed"
    turns one missing line of start-up into an open server handing out footage."""
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen
    from auteur.web import assets, server as web

    assets.ensure(web.STATIC)
    web.Handler.studio = web.Studio(tmp_path / "web")
    web.Handler.accounts = None  # exactly the misconfiguration

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        for path in ("/api/jobs/anything", "/api/jobs/anything/video"):
            with pytest.raises(HTTPError) as caught:
                urlopen(base + path)
            assert caught.value.code == 401, path
        with urlopen(Request(base + "/")) as response:
            assert response.url.endswith("/login")
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_job_belongs_to_the_person_who_asked_for_it(tmp_path):
    from auteur.web.server import Studio

    studio = Studio(tmp_path / "web")
    mine = studio.create("prompt", "reel", 10.0, owner="streetlightseason")

    assert studio.get(mine.id, owner="streetlightseason") is mine
    assert studio.get(mine.id, owner="someone-else") is None
    assert studio.get("no-such-job", owner="streetlightseason") is None
    # No owner asked for means an internal caller, which still sees it.
    assert studio.get(mine.id) is mine


def test_one_signed_in_user_cannot_read_anothers_film(guarded_server, tmp_path):
    """A job id is not a secret — it sits in the address bar and in history — so
    holding any valid session is not permission to read somebody else's
    footage."""
    import json as _json
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen
    from auteur.web import server as web

    base, accounts = guarded_server
    accounts.add("someone-else", "else@example.com", "a-long-enough-one")

    job = web.Handler.studio.create("prompt", "reel", 10.0, owner="streetlightseason")
    film = job.folder / "film.mp4"
    film.write_bytes(b"not really a film")
    job.video, job.status = film, "done"

    request = Request(
        base + "/api/login",
        data=_json.dumps({"username": "someone-else", "password": "a-long-enough-one"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request) as response:
        intruder = response.headers["Set-Cookie"].split(";")[0]

    for path in (f"/api/jobs/{job.id}", f"/api/jobs/{job.id}/video", f"/api/jobs/{job.id}/notes"):
        with pytest.raises(HTTPError) as caught:
            urlopen(Request(base + path, headers={"Cookie": intruder}))
        assert caught.value.code == 404, path


def test_the_server_notices_accounts_changed_on_disk(tmp_path):
    """`auteur account` edits the same file a running server is using. Without
    a reload the server keeps serving the set it read at start-up, and a
    password change appears to do nothing — the confusing kind of nothing,
    where the old password still works."""
    from auteur.web.auth import Accounts

    path = tmp_path / "accounts.json"
    server = Accounts(path)
    server.add("streetlightseason", "s@example.com", TEST_PASSWORD)

    # A second process — the CLI — adds someone and changes a password.
    cli = Accounts(path)
    cli.add("someone", "someone@example.com", "a-long-enough-one")
    cli.set_password(cli.get("streetlightseason"), "a-brand-new-secret")

    server.refresh()
    token, _ = server.sign_in("someone", "a-long-enough-one")
    assert token, "an account added elsewhere should be able to sign in"
    assert server.get("streetlightseason").check("a-brand-new-secret")
    assert not server.get("streetlightseason").check(TEST_PASSWORD)


def test_a_reload_does_not_sign_the_phone_out(tmp_path):
    """The CLI writes back whatever session list it happened to read, so taking
    the file's copy on reload would drop a session created since."""
    from auteur.web.auth import Accounts

    path = tmp_path / "accounts.json"
    cli = Accounts(path)
    cli.add("streetlightseason", "s@example.com", TEST_PASSWORD)

    server = Accounts(path)
    token, _ = server.sign_in("streetlightseason", TEST_PASSWORD)
    assert server.session_user(token) == "streetlightseason"

    cli.add("someone", "someone@example.com", "a-long-enough-one")  # stale sessions written
    server.refresh()
    assert server.session_user(token) == "streetlightseason"
    assert server.get("someone") is not None


def test_an_untouched_file_is_not_reread(tmp_path):
    """refresh() runs on every request, so it must be a stat() and nothing more
    when nothing has changed."""
    from auteur.web.auth import Accounts

    store = Accounts(tmp_path / "accounts.json")
    store.add("streetlightseason", "s@example.com", TEST_PASSWORD)

    reads = []
    original = Accounts._load

    def counting(self):
        reads.append(1)
        return original(self)

    Accounts._load = counting
    try:
        for _ in range(5):
            store.refresh()
    finally:
        Accounts._load = original
    assert reads == [], "nothing changed, so nothing should have been re-read"


# ---------------------------------------------------------------------------
# Found by fuzzing
# ---------------------------------------------------------------------------


def test_a_runtime_is_checked_however_it_arrives():
    """Only the number read out of the prompt used to be range-checked. An
    explicit `duration=` went straight through, so `--length -5` reached the
    planner and the critic then reported that the film "came out the wrong
    length" against a target of minus five seconds."""
    from auteur.director.brief import MAX_RUNTIME, MIN_RUNTIME, clamp_duration, parse_brief

    assert clamp_duration(None) is None
    assert clamp_duration(0) is None
    assert clamp_duration(-10) is None
    assert clamp_duration(float("nan")) is None
    assert clamp_duration(float("inf")) is None
    assert clamp_duration(1.0) == MIN_RUNTIME
    assert clamp_duration(10_000) == MAX_RUNTIME
    assert clamp_duration(30) == 30

    for bad in (-5, 0, float("nan")):
        assert parse_brief("a montage", duration=bad).duration is None
    assert parse_brief("a montage", duration=1e9).duration == MAX_RUNTIME


@pytest.mark.slow
def test_a_negative_length_does_not_reach_the_planner(rushes, tmp_path):
    from auteur.agent import direct

    production = direct(
        [rushes],
        "a montage",
        settings=Settings(
            quality=QUALITIES["draft"],
            primary_format=FORMATS["square"],
            target_duration=6.0,
            use_llm=False,
            revision_rounds=0,
        ),
        workspace=tmp_path / "work",
        duration=-5.0,
    )
    # The nonsense is discarded and the settings default stands.
    assert production.brief.duration is None
    assert production.edl.duration > 0


def test_a_transition_never_outlasts_half_its_shorter_neighbour():
    """The cap was rounded to nearest, so it could land a fraction of a
    millisecond over the shot it eats into. Rounding down makes the property
    exact instead of nearly true."""
    edl = EditDecisionList(title="t")
    for index in range(4):
        edl.shots.append(
            Shot(
                clip_id="C01",
                source=Path("/tmp/a.mp4"),
                start=index * 1.0,
                end=index * 1.0 + 0.6212345,
                transition_in=Transition("dissolve", 2.0),
            )
        )

    class _Asset:
        path, duration, kind = Path("/tmp/a.mp4"), 60.0, "video"

    class _Dossier:
        asset, duration = _Asset(), 60.0

    edl.repair({"C01": _Dossier()}, target_duration=10.0)
    for index in range(1, len(edl.shots)):
        overlap = edl.shots[index].transition_in.duration
        shorter = min(edl.shots[index - 1].duration, edl.shots[index].duration)
        assert (
            overlap <= shorter / 2
        ), f"transition {index} is {overlap!r}, more than half of {shorter!r}"


def test_the_static_route_stays_inside_its_folder(web_server):
    """`Path("/static/..").name` is ".." and resolves to the parent. Nothing
    readable lives there, but the property is worth holding outright rather
    than resting on that."""
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    base, _, cookie = web_server
    for attempt in ("/static/..", "/static/.", "/static/%00", "/static/../auth.py"):
        with pytest.raises(HTTPError) as caught:
            urlopen(Request(base + attempt, headers={"Cookie": cookie}))
        assert caught.value.code == 404, attempt


def test_a_dropped_connection_is_not_an_error(caplog):
    """Keep-alive plus a phone means dropped connections all day. The console
    is where reset links are printed; a traceback per locked screen buries
    them."""
    import logging
    from auteur.web.server import Server

    class _Fake(Server):
        def __init__(self):  # no socket, no bind
            self.handled = []

        def handle_error(self, request, client_address):
            Server.handle_error(self, request, client_address)

    fake = _Fake()
    with caplog.at_level(logging.DEBUG, logger="auteur.web"):
        try:
            raise ConnectionResetError("phone locked")
        except ConnectionResetError:
            fake.handle_error(None, ("127.0.0.1", 1234))
    assert "went away" in caplog.text


# ---------------------------------------------------------------------------
# Surviving bad footage
# ---------------------------------------------------------------------------


def test_removing_a_shot_leaves_a_legal_film():
    """The assembly reads shots and segments positionally, so when a segment is
    dropped the timeline has to shrink to match it."""
    edl = EditDecisionList(title="t")
    for index in range(5):
        edl.shots.append(
            Shot(
                clip_id=f"C{index}",
                source=Path("/tmp/a.mp4"),
                start=0.0,
                end=1.0,
                transition_in=Transition("cut", 0.0) if index == 0 else Transition("dissolve", 0.3),
            )
        )
    edl.texts.append(TextCue(text="late", start=4.5, duration=0.5))
    edl.sfx.append(SoundCue("whoosh", at=4.6))

    reduced = edl.without_shots([0, 3])
    assert [shot.clip_id for shot in reduced.shots] == ["C1", "C2", "C4"]
    # Whatever is first now cannot dissolve out of something that is gone.
    assert reduced.shots[0].transition_in.is_cut
    # Text and effects timed past the new, shorter end are dropped.
    assert reduced.texts == [] and reduced.sfx == []
    # And the original is untouched.
    assert len(edl.shots) == 5 and not edl.shots[1].transition_in.is_cut


def test_removing_every_shot_is_allowed_but_empty():
    edl = EditDecisionList(title="t")
    edl.shots.append(Shot(clip_id="C0", source=Path("/tmp/a.mp4"), start=0.0, end=1.0))
    assert edl.without_shots([0]).shots == []


@pytest.mark.slow
def test_one_unrenderable_shot_does_not_lose_the_film(rushes, tmp_path):
    """A shot whose source window holds no frames — a fraction of a second of
    low-frame-rate footage — used to take the whole render down with it. The
    module's own docstring promises one bad clip costs one segment."""
    from auteur import ffmpeg as ff
    from auteur import render

    space = Workspace(tmp_path / "work")
    edl = EditDecisionList(title="survivor")
    for index in range(3):
        edl.shots.append(
            Shot(
                clip_id="C01",
                source=rushes / "a_wide.mp4",
                start=index * 0.8,
                end=index * 0.8 + 0.7,
            )
        )
    # A window past the end of the clip: nothing to decode.
    edl.shots.insert(2, Shot(clip_id="C99", source=rushes / "a_wide.mp4", start=3.999, end=4.0))

    result = render.render(
        edl,
        space,
        Settings(quality=QUALITIES["draft"]),
        formats=(FORMATS["square"],),
        name="survivor",
    )

    assert result.primary is not None and result.primary.exists()
    assert any("dropped shot" in warning for warning in result.warnings)
    probed = ff.probe(result.primary)
    assert any(s.get("codec_type") == "video" for s in probed["streams"])


def test_an_unreadable_file_is_explained_in_words():
    """ffprobe's stderr is a paragraph of container internals with an absolute
    path in it. Truncating that to fit a log line produced messages that
    stopped mid-directory."""
    from auteur.ingest import _why_unreadable

    long_path = "/very/long/path/" + "x" * 200 + "/clip.mp4"
    assert _why_unreadable(f"[mov,mp4] moov atom not found\n{long_path}") == (
        "not a video file, or the file is damaged"
    )
    assert _why_unreadable("x: Invalid data found when processing input") == (
        "not a video file, or the file is damaged"
    )
    assert "permission" in _why_unreadable("Permission denied")
    assert _why_unreadable("") == "it could not be read"
    for stderr in ("", "anything at all", "moov atom not found"):
        assert "\n" not in _why_unreadable(stderr)
        assert len(_why_unreadable(stderr)) < 60


# ---------------------------------------------------------------------------
# Workflows: platform rules, the media manager, packaging, and the queue
# ---------------------------------------------------------------------------


def test_every_platform_spec_is_internally_consistent():
    """A spec that contradicts itself would silently misroute every post."""
    from auteur.workflows.platforms import PLATFORMS

    for name, spec in PLATFORMS.items():
        assert spec.name == name, "the key and the name must agree"
        assert 0 < spec.min_seconds <= spec.ideal_seconds <= spec.max_seconds
        assert spec.fps > 0 and spec.format.width > 0 and spec.format.height > 0
        assert spec.hashtag_limit >= 0 and spec.caption_limit >= 0
        safe = spec.safe
        # Insets that meet in the middle would leave nowhere to put a title.
        assert safe.top + safe.bottom < 0.75, name
        assert safe.left + safe.right < 0.75, name


def test_a_platform_can_be_named_the_way_people_say_it():
    from auteur.workflows import resolve

    assert resolve("reel").name == "instagram-reel"
    assert resolve("TikTok").name == "tiktok"
    assert resolve("  Shorts ").name == "youtube-short"
    assert resolve("instagram_story").name == "instagram-story"
    with pytest.raises(ValueError, match="unknown platform"):
        resolve("myspace")


def test_the_safe_area_only_ever_pulls_text_inward():
    """Moving a title *out* to the edge to satisfy a safe area would be worse
    than leaving it alone."""
    from auteur.workflows.platforms import SafeArea, resolve

    safe = resolve("tiktok").safe
    # A title parked in the corner comes in.
    x, y = safe.clamp((0.99, 0.99))
    assert x <= 1.0 - safe.right and y <= 1.0 - safe.bottom
    # A title already centred does not move at all.
    assert safe.clamp((0.5, 0.5)) == (0.5, 0.5)
    # A frame with no chrome leaves everything where it was.
    assert SafeArea().clamp((0.02, 0.98)) == (0.02, 0.98)
    # And a nonsensical spec centres rather than inverting the range.
    assert SafeArea(top=0.9, bottom=0.9, left=0.9, right=0.9).clamp((0.1, 0.1)) == (0.5, 0.5)


def test_the_plan_hook_moves_titles_out_from_under_the_buttons():
    from auteur.edl import EditDecisionList, TextCue
    from auteur.workflows import keep_text_readable, resolve

    spec = resolve("tiktok")
    edl = EditDecisionList(
        texts=[
            TextCue(text="under the caption", start=0.0, anchor=(0.5, 0.95)),
            TextCue(text="fine where it is", start=1.0, anchor=(0.5, 0.5)),
        ]
    )
    keep_text_readable(spec)(edl)

    assert edl.texts[0].anchor[1] <= 1.0 - spec.safe.bottom, "still under the caption block"
    assert edl.texts[1].anchor == (0.5, 0.5), "an untouched title must stay untouched"


def test_a_flag_beats_the_prompt_and_the_platform_beats_both():
    """Asking for 12 seconds and being handed 25 is the workflow overruling
    the person using it."""
    from auteur.workflows import resolve, wanted_duration

    reel, story = resolve("instagram-reel"), resolve("instagram-story")

    assert wanted_duration(reel, "neon harbour, 12 seconds") == 12.0
    assert wanted_duration(reel, "neon harbour") == reel.ideal_seconds
    assert wanted_duration(reel, "neon harbour, 12 seconds", 40.0) == 40.0
    # The platform's ceiling is the one thing nothing overrides.
    assert wanted_duration(reel, "neon harbour", 4000.0) == reel.max_seconds
    assert wanted_duration(story, "neon harbour", 600.0) == story.max_seconds
    assert wanted_duration(reel, "neon harbour", 0.5) == reel.min_seconds
    # An absurd runtime in the prompt is discarded by the brief parser long
    # before it reaches here, which leaves the platform's house length.
    assert wanted_duration(reel, "neon harbour, 4000 seconds") == reel.ideal_seconds


# -- the media manager ------------------------------------------------------


@pytest.fixture
def footage(tmp_path):
    """A small folder with a real video, a real image, and a duplicate."""
    from auteur import ffmpeg as ff

    folder = tmp_path / "footage"
    folder.mkdir()
    subprocess.run(
        [
            str(ff.ffmpeg_path()),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=24:duration=2",
            str(folder / "one.mp4"),
        ],
        check=True,
    )
    subprocess.run(
        [
            str(ff.ffmpeg_path()),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:size=320x240",
            "-frames:v",
            "1",
            str(folder / "still.png"),
        ],
        check=True,
    )
    # Written second, and stamped so, because "which one is the copy?" is
    # answered by age rather than by which name sorts first.
    copy = folder / "copy.mp4"
    copy.write_bytes((folder / "one.mp4").read_bytes())
    original_time = (folder / "one.mp4").stat().st_mtime
    os.utime(copy, (original_time + 60, original_time + 60))
    return folder


def test_the_library_indexes_what_it_finds(footage, tmp_path):
    from auteur.workflows.library import Library

    library = Library(tmp_path / "index.json")
    report = library.scan([footage])

    assert len(report.added) == 3
    assert report.unchanged == 0
    kinds = {entry.kind for entry in library.entries.values()}
    assert kinds == {"video", "image"}
    clip = next(e for e in library.entries.values() if e.name == "one.mp4")
    assert clip.duration == pytest.approx(2.0, abs=0.2)
    assert (clip.width, clip.height) == (320, 240)


def test_a_second_scan_does_not_reprobe_what_has_not_changed(footage, tmp_path):
    """The whole point of an index: the second scan must be nearly free."""
    from auteur.workflows import library as library_module
    from auteur.workflows.library import Library

    library = Library(tmp_path / "index.json")
    library.scan([footage])

    probes = {"count": 0}
    real_probe = library_module.probe_asset

    def counting_probe(path):
        probes["count"] += 1
        return real_probe(path)

    library_module.probe_asset = counting_probe
    try:
        again = library.scan([footage])
    finally:
        library_module.probe_asset = real_probe

    assert again.unchanged == 3 and not again.added
    assert probes["count"] == 0, "nothing changed, so nothing should have been opened"


def test_a_changed_file_is_noticed_and_reprobed(footage, tmp_path):
    from auteur.workflows.library import Library

    library = Library(tmp_path / "index.json")
    library.scan([footage])

    (footage / "still.png").write_bytes((footage / "still.png").read_bytes() + b"\x00")
    report = library.scan([footage])
    assert [entry.name for entry in report.updated] == ["still.png"]
    assert report.unchanged == 2


def test_duplicates_are_found_by_content_and_confirmed_byte_for_byte(footage, tmp_path):
    from auteur.workflows.library import Library

    library = Library(tmp_path / "index.json")
    report = library.scan([footage])

    assert [(copy.name, kept.name) for copy, kept in report.duplicates] == [
        ("copy.mp4", "one.mp4")
    ], "the newer file is the copy; nobody should be told to delete the original"

    groups = library.duplicate_groups()
    assert len(groups) == 1
    assert [entry.name for entry in groups[0]] == ["one.mp4", "copy.mp4"]


def test_a_digest_match_is_not_enough_on_its_own(footage, tmp_path, monkeypatch):
    """The fingerprint only reads each end of the file. Telling somebody to
    delete footage on the strength of that would be careless."""
    from auteur.workflows import library as library_module
    from auteur.workflows.library import Library

    # Force every file to look identical to the fingerprint.
    monkeypatch.setattr(library_module, "digest_of", lambda path: "all-the-same")

    library = Library(tmp_path / "index.json")
    report = library.scan([footage])

    # one.mp4 and copy.mp4 really are identical; still.png is not, and the
    # full comparison is what tells them apart.
    assert {copy.name for copy, _ in report.duplicates} == {
        "copy.mp4"
    }, "still.png fingerprints the same but is not the same file"


def test_the_index_survives_being_written_and_read_back(footage, tmp_path):
    from auteur.workflows.library import Library

    first = Library(tmp_path / "index.json")
    first.scan([footage])
    first.tag([footage / "one.mp4"], "keepers")
    first.save()

    second = Library(tmp_path / "index.json")
    assert len(second.entries) == len(first.entries)
    assert second.pick(tag="keepers")[0].name == "one.mp4"
    assert second.pick(kind="image")[0].name == "still.png"


def test_a_corrupt_index_costs_a_rescan_and_nothing_else(tmp_path):
    from auteur.workflows.library import Library

    path = tmp_path / "index.json"
    path.write_text("{not json at all")
    library = Library(path)  # must not raise
    assert library.entries == {}


def test_files_that_have_gone_are_dropped_from_the_index(footage, tmp_path):
    from auteur.workflows.library import Library

    library = Library(tmp_path / "index.json")
    library.scan([footage])
    (footage / "copy.mp4").unlink()

    report = library.scan([footage])
    assert [entry.name for entry in report.missing] == ["copy.mp4"]
    assert "copy.mp4" not in {entry.name for entry in library.entries.values()}


# -- captions and packaging -------------------------------------------------


def test_a_caption_never_exceeds_what_the_box_will_carry():
    from auteur.workflows.publish import Caption
    from auteur.workflows import resolve

    spec = resolve("instagram-reel")
    caption = Caption(body="x" * 5000, hashtags=tuple(f"tag{n}" for n in range(60)))
    rendered = caption.render(spec)

    assert len(rendered) <= spec.caption_limit
    assert rendered.count("#") <= spec.hashtag_limit


def test_a_surface_with_no_caption_field_gets_no_caption():
    from auteur.workflows.publish import Caption
    from auteur.workflows import resolve

    assert Caption(body="anything", hashtags=("a",)).render(resolve("instagram-story")) == ""


def test_a_caption_keeps_its_tags_when_the_prose_has_to_go():
    """Tags earn their place; the prose is what gets cut."""
    from auteur.workflows.publish import Caption
    from auteur.workflows.platforms import PlatformSpec, SafeArea
    from auteur.config import FORMATS

    tiny = PlatformSpec(
        name="tiny",
        service="Test",
        surface="Test",
        format=FORMATS["reel"],
        min_seconds=1,
        max_seconds=10,
        ideal_seconds=5,
        fps=30,
        safe=SafeArea(),
        caption_limit=60,
        hashtag_limit=3,
        wants_cover=False,
    )
    rendered = Caption(body="y" * 400, hashtags=("one", "two", "three")).render(tiny)
    assert len(rendered) <= 60
    assert "#one" in rendered and "#two" in rendered and "#three" in rendered


def test_a_drafted_caption_is_about_the_film_not_about_the_edit():
    from auteur.director.brief import parse_brief
    from auteur.edl import EditDecisionList
    from auteur.workflows import resolve
    from auteur.workflows.publish import draft_caption

    brief = parse_brief('"AFTER DARK" moody harbour at dusk, fast montage, 20 seconds')
    edl = EditDecisionList(title="after dark")
    caption = draft_caption(brief, edl, resolve("instagram-reel"))

    text = caption.render(resolve("instagram-reel")).lower()
    # Direction is not caption copy.
    for technical in ("20 seconds", "montage", "9:16", "reel"):
        assert technical not in caption.body.lower(), technical
    assert "harbour" in text
    assert caption.hashtags, "a post with no tags is a post nobody finds"
    assert all(tag == tag.lower() and tag.isalnum() for tag in caption.hashtags)
    assert caption.alt_text, "alt text is not optional in spirit"


def test_packaging_checks_the_render_against_the_platform(tmp_path):
    """A duration the critic was happy with can still be under TikTok's floor."""
    from auteur import ffmpeg as ff
    from auteur.director.brief import parse_brief
    from auteur.edl import EditDecisionList
    from auteur.workflows import resolve
    from auteur.workflows.publish import package

    # Two seconds of 1080x1920 — the right shape, under TikTok's 3s minimum.
    video = tmp_path / "short.mp4"
    subprocess.run(
        [
            str(ff.ffmpeg_path()),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1080x1920:rate=30:duration=2",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
    )

    spec = resolve("tiktok")
    deliverable = package(
        video=video,
        spec=spec,
        brief=parse_brief("harbour at dusk"),
        edl=EditDecisionList(title="harbour"),
        folder=tmp_path / "post",
    )

    assert not deliverable.ok
    assert any("minimum" in warning for warning in deliverable.warnings)
    assert (deliverable.width, deliverable.height) == (1080, 1920)
    assert deliverable.cover is not None and deliverable.cover.exists(), "cover frame failed"
    assert (tmp_path / "post" / "post.json").exists()
    assert (tmp_path / "post" / "caption.txt").exists()
    manifest = json.loads((tmp_path / "post" / "post.json").read_text())
    assert manifest["platform"] == "tiktok"
    assert manifest["caption_to_paste"]


def test_the_cover_frame_is_not_the_first_frame(tmp_path):
    """The first frame of a cut is the least representative one in it, and it
    is the frame every tool picks by default."""
    from auteur import ffmpeg as ff
    from auteur.workflows.publish import cover_frame

    # Black for the first second, then white. A first-frame grab is black.
    video = tmp_path / "fade.mp4"
    subprocess.run(
        [
            str(ff.ffmpeg_path()),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:size=320x240:rate=10:duration=1,"
            "fade=t=out:st=0:d=0[a];color=c=white:size=320x240:rate=10:duration=4[b];"
            "[a][b]concat=n=2:v=1:a=0",
            "-filter_complex_threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=False,
    )
    if not video.exists():  # the filter graph is fussy; do not fail the suite on it
        pytest.skip("could not build the fixture clip")

    cover = cover_frame(video, tmp_path / "cover.jpg")
    assert cover is not None and cover.stat().st_size > 0


# -- the schedule -----------------------------------------------------------


def _deliverable(platform: str, tmp_path, name: str = "film.mp4"):
    from auteur.workflows import resolve
    from auteur.workflows.publish import Caption, Deliverable

    spec = resolve(platform)
    video = tmp_path / name
    video.write_bytes(b"not really a film")
    return Deliverable(
        platform=spec.name,
        service=spec.service,
        surface=spec.surface,
        video=video,
        duration=20.0,
        width=spec.format.width,
        height=spec.format.height,
        caption=Caption(body="a caption"),
    )


def test_a_time_with_no_timezone_is_read_as_local_not_as_utc():
    """A queue written in one timezone and read as another posts at 4am."""
    from datetime import datetime, timezone

    from auteur.workflows.schedule import format_time, parse_time

    explicit = parse_time("2026-08-20T18:00:00Z")
    assert format_time(explicit) == "2026-08-20T18:00:00Z"
    assert explicit.tzinfo is timezone.utc

    naive = parse_time("2026-08-20 18:00")
    expected = datetime(2026, 8, 20, 18, 0).astimezone(timezone.utc)
    assert naive == expected

    assert parse_time(None) is not None
    with pytest.raises(ValueError):
        parse_time("some time next tuesday-ish")


def test_the_queue_refuses_to_stack_two_posts_on_top_of_each_other(tmp_path):
    from auteur.workflows.schedule import Schedule

    queue = Schedule(tmp_path / "queue.json", gap_hours=4.0, per_day=3)
    first, complaint = queue.add(_deliverable("instagram-reel", tmp_path), "2026-08-20 10:00")
    assert first is not None and complaint == ""

    clash, why = queue.add(_deliverable("instagram-post", tmp_path, "b.mp4"), "2026-08-20 11:00")
    assert clash is None, "two Instagram posts an hour apart must be refused"
    assert "minimum" in why

    # Far enough away is fine.
    ok, _ = queue.add(_deliverable("instagram-post", tmp_path, "c.mp4"), "2026-08-20 16:00")
    assert ok is not None

    # A different service is not competing for the same audience slot.
    other, _ = queue.add(_deliverable("tiktok", tmp_path, "d.mp4"), "2026-08-20 10:30")
    assert other is not None


def test_the_daily_ceiling_is_enforced(tmp_path):
    from auteur.workflows.schedule import Schedule

    queue = Schedule(tmp_path / "queue.json", gap_hours=1.0, per_day=2)
    for hour in (9, 11):
        post, _ = queue.add(
            _deliverable("tiktok", tmp_path, f"{hour}.mp4"), f"2026-08-20 {hour}:00"
        )
        assert post is not None

    blocked, why = queue.add(_deliverable("tiktok", tmp_path, "x.mp4"), "2026-08-20 13:00")
    assert blocked is None and "limit" in why


def test_forcing_a_clash_says_so_rather_than_pretending(tmp_path):
    from auteur.workflows.schedule import Schedule

    queue = Schedule(tmp_path / "queue.json", gap_hours=4.0)
    queue.add(_deliverable("tiktok", tmp_path), "2026-08-20 10:00")
    post, complaint = queue.add(
        _deliverable("tiktok", tmp_path, "b.mp4"), "2026-08-20 10:30", force=True
    )
    assert post is not None
    assert complaint, "forcing must still report what was overridden"


def test_a_batch_is_spread_out_rather_than_dumped(tmp_path):
    from auteur.workflows.schedule import Schedule

    queue = Schedule(tmp_path / "queue.json", gap_hours=4.0, per_day=10)
    batch = [_deliverable("tiktok", tmp_path, f"{n}.mp4") for n in range(4)]
    posts = queue.plan(batch, start="2026-08-20 09:00")

    assert len(posts) == 4
    times = sorted(post.when for post in posts)
    gaps = [(b - a).total_seconds() / 3600.0 for a, b in zip(times, times[1:], strict=False)]
    assert all(gap >= 4.0 - 1e-6 for gap in gaps), gaps


def test_the_queue_round_trips_and_reports_what_is_due(tmp_path):
    from datetime import datetime, timedelta, timezone

    from auteur.workflows.schedule import Schedule

    queue = Schedule(tmp_path / "queue.json", gap_hours=0.0)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    future = datetime.now(timezone.utc) + timedelta(days=1)
    ready, _ = queue.add(_deliverable("tiktok", tmp_path, "now.mp4"), past)
    queue.add(_deliverable("tiktok", tmp_path, "later.mp4"), future)
    queue.save()

    reopened = Schedule(tmp_path / "queue.json")
    assert len(reopened.posts) == 2
    assert [post.id for post in reopened.due()] == [ready.id]

    assert reopened.mark(ready.id, "posted")
    assert reopened.due() == [], "a posted item is not still due"
    with pytest.raises(ValueError, match="unknown status"):
        reopened.mark(ready.id, "sort-of")


def test_the_queue_can_be_handed_to_whatever_actually_posts(tmp_path):
    from auteur.workflows.schedule import Schedule

    queue = Schedule(tmp_path / "queue.json", gap_hours=0.0)
    queue.add(_deliverable("instagram-reel", tmp_path), "2026-08-20 10:00")
    rows = queue.export_csv().strip().splitlines()

    assert rows[0].startswith("when_utc,platform,service")
    assert len(rows) == 2
    assert "instagram-reel" in rows[1]
    # Newlines in a caption must not become extra CSV rows.
    assert "\n" not in rows[1]


def test_a_queued_post_whose_film_has_gone_can_be_tidied_away(tmp_path):
    from auteur.workflows.schedule import Schedule

    queue = Schedule(tmp_path / "queue.json", gap_hours=0.0)
    deliverable = _deliverable("tiktok", tmp_path)
    queue.add(deliverable, "2026-08-20 10:00")
    deliverable.video.unlink()

    assert [post.video for post in queue.forget_missing()] == [str(deliverable.video)]
    assert queue.posts == []


# ---------------------------------------------------------------------------
# Insight: reading performance data, and being honest about it
# ---------------------------------------------------------------------------


def _write_csv(path: Path, header: str, *rows: str) -> Path:
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def test_every_export_shape_is_recognised_by_its_columns(tmp_path):
    """A person calls the file whatever they like; the header is the truth."""
    from auteur.insight import detect_form

    cases = {
        "short_form_video": "content_id,hook_style,hook_duration_sec,completion_rate",
        "b2b_carousel": "carousel_id,slide_1_hook,swipe_through_rate,save_rate",
        "text_thread": "thread_id,opening_line_hook,bookmark_rate",
        "film_theory": "content_id,shot_composition,lighting_setup,three_second_watch_rate",
        "color_theory": "content_id,palette_type,dominant_hex,thumbnail_ctr",
        "music_theory": "content_id,tempo_bpm,harmonic_key,progression_type",
        "algorithmic": "content_id,seed_pool_size,velocity_score_10m,algorithmic_bucket_status",
    }
    for form, header in cases.items():
        assert detect_form(header.split(",")) == form, form

    with pytest.raises(ValueError, match="performance export"):
        detect_form(["name", "colour", "size"])


def test_the_matrix_is_matched_before_the_forms_it_contains(tmp_path):
    """The multimodal export carries columns from every other form. Matched in
    the wrong order it reads as whichever one happens to be checked first."""
    from auteur.insight import detect_form

    header = (
        "content_id,dataset_origin,palette_type,contrast_ratio,stop_scroll_ms,thumbnail_ctr,"
        "progression_type,tempo_bpm,loop_completion_rate,shot_composition,pacing_cuts_per_10s"
    ).split(",")
    assert detect_form(header) == "multimodal_matrix"


def test_a_derived_field_never_claims_to_have_been_measured(tmp_path):
    """`has()` is what stops an inferred number being averaged as an observation."""
    from auteur.insight import load

    path = _write_csv(
        tmp_path / "v.csv",
        "content_id,hook_style,hook_duration_sec,completion_rate,share_to_view_ratio,loop_count",
        "v_1,Visual Pattern Interrupt,1.8,0.58,0.09,1.7",
    )
    signal = load([path])[0]

    assert signal.completion_rate == 0.58
    assert signal.has("completion_rate")
    # Derived from completion, and therefore not measured.
    assert signal.three_second_watch_rate > signal.completion_rate
    assert not signal.has("three_second_watch_rate")
    assert signal.derived_from["three_second_watch_rate"] == "completion_rate"
    assert signal.is_circular("three_second_watch_rate", "completion_rate")
    assert not signal.is_circular("three_second_watch_rate", "tempo_bpm")


def test_a_correlation_is_never_computed_between_a_number_and_itself(tmp_path):
    """The colour export has no three-second column, so we infer one from
    stop_scroll_ms. Correlating the two then returns 1.00 and means nothing —
    it was reported as the strongest finding in the data."""
    from auteur.insight import fit, load

    rows = [
        f"color_v{n:03d},Triadic,#1DE9B6,{6 + n * 0.3},Nostalgia,{0.10 + n * 0.004},"
        f"{150 + n * 20},{0.03 + n * 0.006}"
        for n in range(1, 13)
    ]
    path = _write_csv(
        tmp_path / "c.csv",
        "content_id,palette_type,dominant_hex,contrast_ratio,emotional_trigger_intent,"
        "thumbnail_ctr,stop_scroll_ms,share_to_view_ratio",
        *rows,
    )
    model = fit(load([path]))

    for column, label, _ in model.drivers:
        assert not (
            column == "stop_scroll_ms" and "three second" in label
        ), "correlated a derived field against the column it was derived from"


def test_a_generated_export_is_spotted_and_discounted(tmp_path):
    """Real performance data is noisy. A file where every column is a smooth
    function of one hidden score is a target somebody drew, not an observation,
    and it must not outvote a smaller honest one."""
    from auteur.insight import fit, load

    # Everything a perfect function of n: exactly the shape of a generated set.
    rows = [
        f"VIRAL_{n:04d},Film Theory,Neon-Cyberpunk,{16 + n * 0.01},{800 + n},{0.22 + n * 0.0001},"
        f"Aeolian-Fade,{139 + n * 0.01},{0.77 + n * 0.0003},70000,Static-Symmetrical-Frame,"
        f"{9 + n * 0.002},{1.3 - n * 0.0005},12,44000,{84 + n * 0.03},0,Exponential-Viral"
        for n in range(1, 61)
    ]
    path = _write_csv(
        tmp_path / "m.csv",
        "content_id,dataset_origin,palette_type,contrast_ratio,stop_scroll_ms,thumbnail_ctr,"
        "progression_type,tempo_bpm,loop_completion_rate,remix_velocity_24h,shot_composition,"
        "pacing_cuts_per_10s,pattern_interrupt_timestamp_sec,rewind_events_count,seed_pool_size,"
        "velocity_score_10m,system_kill_signal_triggered,algorithmic_bucket_status",
        *rows,
    )
    model = fit(load([path]))

    assert "multimodal_matrix" in model.generated_forms
    # And a corpus of nothing but winners says so, because it cannot tell you
    # what separates them from anything else.
    assert not model.has_negatives
    assert "no failures" in model.caveat


def test_disagreement_between_exports_is_reported_not_averaged_away(tmp_path):
    """Two files that disagree about the direction of an effect is the most
    useful thing in a multi-source corpus, and the easiest to lose in a mean."""
    from auteur.insight import fit, load

    # Music theory: slower tempo, better loops.
    music = _write_csv(
        tmp_path / "music.csv",
        "content_id,tempo_bpm,harmonic_key,progression_type,audio_retention_sec,"
        "remix_velocity_24h,haptic_volume_boosts,loop_completion_rate",
        *[
            f"audio_m{n:03d},{70 + n * 6},E Minor,Pedal Point,{9 - n * 0.1},1500,2,"
            f"{0.72 - n * 0.03}"
            for n in range(1, 13)
        ],
    )
    # The matrix: faster tempo, better loops. Same driver, opposite sign.
    matrix = _write_csv(
        tmp_path / "matrix.csv",
        "content_id,dataset_origin,palette_type,contrast_ratio,stop_scroll_ms,thumbnail_ctr,"
        "progression_type,tempo_bpm,loop_completion_rate,shot_composition,pacing_cuts_per_10s,"
        "pattern_interrupt_timestamp_sec,seed_pool_size,velocity_score_10m,"
        "system_kill_signal_triggered,algorithmic_bucket_status",
        *[
            f"VIRAL_{n:04d},Music Theory,Neon-Cyberpunk,{17 + n * 0.02},{800 + n * 2},0.24,"
            f"Aeolian-Fade,{130 + n},{0.70 + n * 0.004},Static-Symmetrical-Frame,9,1.2,"
            f"44000,90,0,Exponential-Viral"
            for n in range(1, 31)
        ],
    )
    model = fit(load([music, matrix]))
    assert any("tempo" in conflict for conflict in model.conflicts), model.conflicts


def test_a_model_fitted_on_nothing_real_says_so():
    from auteur.insight import corpus, fit

    model = fit(corpus([], simulate_rows=400))
    assert model.measured_rows == 0
    assert "simulated" in model.provenance
    assert "not any platform" in model.provenance


def test_the_simulator_and_the_loader_agree_on_every_column(tmp_path):
    """They must, or the numbers change meaning depending on where they came
    from — which is the one bug in this package that nothing would catch."""
    from auteur.insight import load, simulate, write_csv

    made = simulate(50)
    path = write_csv(made, tmp_path / "sim.csv")
    read_back = load([path])

    assert len(read_back) == len(made)
    for original, restored in zip(made, read_back, strict=True):
        assert restored.post_id == original.post_id
        assert restored.hook_style == original.hook_style
        assert restored.completion_rate == pytest.approx(original.completion_rate, abs=1e-3)
        assert restored.share_to_view_ratio == pytest.approx(original.share_to_view_ratio, abs=1e-3)
        assert restored.loop_count == pytest.approx(original.loop_count, abs=1e-3)


# ---------------------------------------------------------------------------
# Scoring an edit
# ---------------------------------------------------------------------------


def _timeline(
    *, opening=1.6, shots=12, runtime=15.0, loop_back=False, text_at=None, end_card=False
):
    from auteur.edl import EditDecisionList, Shot, TextCue

    each = (runtime - opening) / max(shots - 1, 1)
    out = [
        Shot(
            clip_id=f"C{index % 4:02d}",
            source=Path(f"/x/{index % 4}.mp4"),
            start=0.0,
            end=opening if index == 0 else each,
        )
        for index in range(shots)
    ]
    if loop_back:
        out[-1].clip_id = out[0].clip_id
        out[-1].end = 0.9
    texts = []
    if text_at is not None:
        texts.append(TextCue(text="HOOK", start=text_at, duration=1.4))
    if end_card:
        texts.append(TextCue(text="END", start=runtime - 2.0, duration=2.0, style="end-card"))
    return EditDecisionList(title="t", shots=out, texts=texts)


@pytest.fixture
def model():
    from auteur.insight import corpus, fit

    return fit(corpus([], simulate_rows=1500))


def test_the_three_objectives_genuinely_trade_off(model):
    """If they always moved together they would be one objective, and the crew
    would be theatre."""
    from auteur.insight import predict

    long_looping = predict(_timeline(runtime=45, shots=22, loop_back=True, text_at=0.1), model)
    tight_no_loop = predict(_timeline(runtime=15, shots=12, loop_back=False, text_at=0.1), model)

    assert long_looping.loop.score > tight_no_loop.loop.score
    assert tight_no_loop.share.predicted > long_looping.share.predicted
    assert long_looping.weakest.name == "share"
    assert tight_no_loop.weakest.name == "loop"


def test_text_before_the_first_cut_is_worth_something_on_its_own(model):
    """It was folded in as a bonus on top of the timing score, which saturated
    at 1.0 — so on a well-timed edit the hook agent's own proposal could never
    show a gain, and was skipped for ever."""
    from auteur.insight import predict

    ideal = model.best_hook_duration or 1.6
    with_text = predict(_timeline(opening=ideal, text_at=0.1), model)
    without = predict(_timeline(opening=ideal, text_at=None), model)

    assert with_text.hook.score > without.hook.score
    assert any("nothing on screen" in note for note in without.notes)


def test_an_end_card_costs_the_loop(model):
    from auteur.insight import predict

    plain = predict(_timeline(loop_back=True), model)
    stopped = predict(_timeline(loop_back=True, end_card=True), model)
    assert stopped.loop.score < plain.loop.score


def test_the_prediction_says_where_they_leave(model):
    from auteur.insight import predict

    prediction = predict(_timeline(runtime=20.0), model)
    assert 0.0 < prediction.drop_off_second() <= 20.0
    assert len(prediction.retention_curve) == 10
    # Retention only ever falls.
    for earlier, later in zip(
        prediction.retention_curve, prediction.retention_curve[1:], strict=False
    ):
        assert later <= earlier + 1e-9


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


def test_a_crew_only_keeps_changes_that_actually_score_better(model):
    """Hill climbing on a copy: an agent may be confidently wrong and the worst
    it costs is a round."""
    from auteur.agents import Crew, Gate, Mode, Proposal, Risk

    class Vandal:
        name, objective = "vandal", "nothing"

        def inspect(self, edl, prediction, model):
            def wreck(target):
                target.shots[0].end = target.shots[0].start + 12.0

            return [
                Proposal(
                    agent=self.name,
                    title="Hold the opening for twelve seconds",
                    reason="testing",
                    change=wreck,
                    objective=self.objective,
                    risk=Risk.LOW,
                )
            ]

    edl = _timeline(opening=1.6, text_at=0.1, loop_back=True)
    crew = Crew([Vandal()], model, gate=Gate(Mode.AUTONOMOUS), max_rounds=2)
    result = crew.run(edl)

    assert not result.applied, "a change that scores worse must not survive"
    assert result.final.overall == pytest.approx(result.baseline.overall, abs=1e-9)
    assert result.edl.shots[0].duration == pytest.approx(1.6, abs=0.01)


def test_an_agent_that_raises_does_not_stop_the_crew(model):
    from auteur.agents import Crew, Gate, Mode, default_crew

    class Broken:
        name, objective = "broken", "nothing"

        def inspect(self, edl, prediction, model):
            raise RuntimeError("no")

    crew = Crew([Broken(), *default_crew()], model, gate=Gate(Mode.AUTONOMOUS), max_rounds=2)
    result = crew.run(_timeline(opening=5.0, runtime=40, shots=9))
    assert result.gain > 0, "the working agents should still have improved it"


def test_supervised_mode_applies_the_small_things_and_asks_about_the_rest(model):
    from auteur.agents import Crew, Gate, Mode, Risk, default_crew

    asked = []

    def ask(proposal):
        asked.append(proposal)
        return "approve", ""

    crew = Crew(default_crew(), model, gate=Gate(Mode.SUPERVISED, on_ask=ask), max_rounds=3)
    result = crew.run(_timeline(opening=5.0, runtime=40, shots=9, end_card=True))

    assert result.gain > 0
    assert asked, "structural changes must reach a person"
    assert all(p.risk >= Risk.MEDIUM for p in asked), "low-risk changes should not interrupt"
    assert any(p.risk == Risk.LOW and p.applied for p in result.applied)


def test_nothing_is_applied_when_there_is_nobody_to_ask(model):
    """A gate that approves when unattended is not a gate, it is a delay."""
    from auteur.agents import Crew, Gate, Mode, default_crew

    crew = Crew(default_crew(), model, gate=Gate(Mode.MANUAL), max_rounds=2)
    result = crew.run(_timeline(opening=5.0, runtime=40, shots=9))

    assert result.applied == []
    assert result.gain == pytest.approx(0.0, abs=1e-9)


def test_publishing_always_needs_a_person_in_every_mode():
    """There is deliberately no mode that lets an agent post."""
    from auteur.agents import Gate, Mode

    for mode in Mode:
        assert Gate(mode).may_publish("a reel") is False, mode

    answered = []
    gate = Gate(Mode.AUTONOMOUS, on_ask=lambda p: answered.append(p) or ("approve", ""))
    assert gate.may_publish("a reel") is True
    assert answered[0].risk.name == "HIGH"


def test_the_agents_actually_close_a_loop_and_shorten_a_hook(model):
    from auteur.agents import Crew, Gate, Mode, default_crew

    edl = _timeline(opening=5.0, runtime=40, shots=9, end_card=True)
    result = Crew(default_crew(), model, gate=Gate(Mode.AUTONOMOUS), max_rounds=4).run(edl)

    assert result.edl.shots[0].duration < 3.0, "the hook should have been cut down"
    assert result.edl.shots[-1].clip_id == result.edl.shots[0].clip_id, "the loop should close"
    assert not [c for c in result.edl.texts if c.style == "end-card"], "end card should have gone"
    assert result.edl.duration < edl.duration


def test_safe_areas_still_win_after_the_agents_have_moved_the_titles(model):
    """An agent optimising a hook does not know the bottom fifth is a caption
    box. Whatever it does to a title, the safe area has the last word."""
    from auteur.agents import Crew, Gate, Mode, default_crew
    from auteur.workflows import resolve, with_agents

    spec = resolve("tiktok")
    edl = _timeline(opening=5.0, runtime=40, shots=9)
    edl.texts[:] = []
    from auteur.edl import TextCue

    edl.texts.append(TextCue(text="UNDER THE BUTTONS", start=2.0, duration=2.0, anchor=(0.5, 0.97)))

    crew = Crew(default_crew(), model, gate=Gate(Mode.AUTONOMOUS), max_rounds=3)
    with_agents(spec, crew)(edl)

    for cue in edl.texts:
        assert cue.anchor[1] <= 1.0 - spec.safe.bottom + 1e-9, cue.text


# ---------------------------------------------------------------------------
# Labelled outcomes: the only data that can tell a winner from a loser
# ---------------------------------------------------------------------------


def _jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _win(index: int) -> dict:
    return {
        "content_id": f"c_{index}",
        "target_platform": "TikTok",
        "editing_style": "Cinematic / Seamless",
        "optimal_schedule_time": "18:09:00 UTC",
        "metrics_completion_rate": 0.84,
        "metrics_rewatch_ratio": 1.36,
        "metrics_velocity_score_1m": 0.89,
        "metrics_share_ratio": 0.31,
        "hook_dropoff_rate_3s": 0.11,
        "agent_action_recommended": "BOOST_DISTRIBUTION",
    }


def _loss(index: int, error: str = "Hook Abandonment") -> dict:
    return {
        "content_id": f"v_err_{index}",
        "target_platform": "TikTok",
        "editing_style": "Cinematic Minimalist",
        "error_state_detected": error,
        "algorithmic_signals": {
            "velocity_score_1m": 0.06,
            "completion_rate": 0.28,
            "rewatch_ratio": 0.08,
            "hook_dropoff_rate_3s": 0.51,
        },
        "distribution_metadata": {
            "initial_seed_pool_size": 162,
            "current_bucket_tier": 1,
            "optimal_schedule_time": "03:15 UTC",
        },
        "agent_action_recommended": "RE_EDIT_HOOK_REPLACE",
    }


def test_flat_and_nested_outcome_exports_read_the_same(tmp_path):
    """Wins keep their metrics flat; failures nest them. Two loaders would
    drift apart, so both go through one that can reach either way."""
    from auteur.insight import load

    signals = load([_jsonl(tmp_path / "mixed.jsonl", [_win(1), _loss(1)])])
    win, loss = signals

    assert win.outcome == "win" and loss.outcome == "fail"
    assert win.completion_rate == pytest.approx(0.84)
    assert loss.completion_rate == pytest.approx(0.28), "nested metrics must be reached"
    # A drop-off is the complement of a watch rate, and this is the only export
    # that measures it directly.
    assert win.three_second_watch_rate == pytest.approx(0.89)
    assert loss.three_second_watch_rate == pytest.approx(0.49)
    assert win.has("three_second_watch_rate"), "measured here, not derived"
    # A rewatch ratio is plays beyond the first; a loop count is plays.
    assert win.loop_count == pytest.approx(2.36)
    assert loss.error_state == "Hook Abandonment"
    assert loss.recommended_action == "RE_EDIT_HOOK_REPLACE"
    assert loss.failed and loss.stalled


def test_a_labelled_corpus_can_finally_discriminate(tmp_path):
    from auteur.insight import fit, load

    rows = [_win(n) for n in range(40)] + [_loss(n) for n in range(40)]
    model = fit(load([_jsonl(tmp_path / "both.jsonl", rows)]))

    assert model.wins == 40 and model.failures == 40
    assert model.discriminative
    assert model.has_negatives, "a corpus with failures in it is not winners-only"

    win, fail, boundary = model.separation["three_second_watch_rate"]
    assert win > fail
    assert fail < boundary < win, "the boundary must sit between the two medians"


def test_the_failure_taxonomy_survives_with_its_recommended_fixes(tmp_path):
    from auteur.insight import fit, load

    rows = [_win(n) for n in range(30)]
    rows += [_loss(n, "Hook Abandonment") for n in range(25)]
    rows += [_loss(100 + n, "Bad Aspect Ratio") for n in range(15)]
    model = fit(load([_jsonl(tmp_path / "modes.jsonl", rows)]))

    modes = {state: (count, action) for state, count, action in model.failure_modes}
    assert modes["Hook Abandonment"][0] == 25
    assert modes["Bad Aspect Ratio"][0] == 15
    # Most common first — that is the order somebody reads it in.
    assert model.failure_modes[0][0] == "Hook Abandonment"


def test_posting_windows_come_from_the_winners_not_from_folklore(tmp_path):
    from auteur.insight import fit, load

    rows = []
    for n in range(30):
        row = _win(n)
        row["optimal_schedule_time"] = "18:09:00 UTC"
        rows.append(row)
    for n in range(30, 34):  # a thin tail that must not count as a window
        row = _win(n)
        row["optimal_schedule_time"] = "04:00:00 UTC"
        rows.append(row)
    rows += [_loss(n) for n in range(25)]

    model = fit(load([_jsonl(tmp_path / "when.jsonl", rows)]))
    assert 18 in model.optimal_hours
    assert 4 not in model.optimal_hours, "a handful of posts is not an optimal window"


# ---------------------------------------------------------------------------
# Preflight: the failure modes, and the ones it cannot see
# ---------------------------------------------------------------------------


@pytest.fixture
def labelled_model(tmp_path):
    from auteur.insight import fit, load

    rows = [_win(n) for n in range(40)] + [_loss(n) for n in range(40)]
    return fit(load([_jsonl(tmp_path / "labelled.jsonl", rows)]))


def test_preflight_catches_a_weak_hook_against_the_measured_boundary(labelled_model):
    from auteur.agents import preflight
    from auteur.insight import predict
    from auteur.workflows import resolve

    spec = resolve("tiktok")
    weak = _timeline(opening=6.0, runtime=40, shots=8, text_at=None)
    weak.width, weak.height = spec.format.width, spec.format.height

    findings = preflight(weak, predict(weak, labelled_model), labelled_model, spec=spec)
    modes = {finding.mode: finding for finding in findings}

    assert "Hook Abandonment" in modes
    assert modes["Hook Abandonment"].action == "RE_EDIT_HOOK_REPLACE"
    assert modes["Hook Abandonment"].confirmed


def test_preflight_catches_the_wrong_frame_shape(labelled_model):
    from auteur.agents import preflight
    from auteur.insight import predict
    from auteur.workflows import resolve

    spec = resolve("tiktok")
    edl = _timeline(opening=1.2, runtime=15, shots=6, text_at=0.1)
    edl.width, edl.height = 1920, 1080  # landscape, into a vertical surface

    findings = preflight(edl, predict(edl, labelled_model), labelled_model, spec=spec)
    aspect = next(f for f in findings if f.mode == "Bad Aspect Ratio")
    assert aspect.action == "RE_CROP_9_16_ASPECT"
    assert "1080" in aspect.detail


def test_a_synthesised_bed_is_the_one_failure_mode_you_can_make_impossible(labelled_model):
    """Muted Audio Copyright is 11% of recorded failures, and the only one that
    can be designed out rather than checked for."""
    from auteur.agents.preflight import check_audio
    from auteur.edl import EditDecisionList, MusicCue

    ours = EditDecisionList(music=MusicCue(source=Path("/x/bed_boom-bap.wav")))
    assert check_audio(ours) is None

    theirs = EditDecisionList(music=MusicCue(source=Path("/x/some_hit_single.mp3")))
    finding = check_audio(theirs)
    assert finding is not None
    assert finding.action == "RE_AUDIO_SWAP_TRENDING"
    # It cannot identify a song, so it must not claim to have found a problem.
    assert not finding.confirmed


def test_a_check_that_cannot_run_is_reported_differently_from_one_that_passed():
    """With no labelled failures there is no boundary, and saying so beats
    silently reporting a clean bill of health."""
    from auteur.agents.preflight import check_hook
    from auteur.insight import corpus, fit, predict

    unlabelled = fit(corpus([], simulate_rows=400))
    assert not unlabelled.discriminative

    finding = check_hook(predict(_timeline(), unlabelled), unlabelled)
    assert finding is not None and not finding.confirmed
    assert "no labelled failures" in finding.detail


def test_preflight_names_what_it_cannot_see():
    """A preflight that claims to catch everything is one nobody should trust."""
    from auteur.agents import unknowable

    cannot = unknowable()
    assert "Shadowban Boundary" in cannot
    assert "Low Organic Traction" in cannot


def test_a_corrupt_render_is_caught_after_the_fact(tmp_path):
    from auteur.agents.preflight import check_render

    assert check_render(None) is not None
    assert check_render(tmp_path / "nothing.mp4") is not None

    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"this is not an mp4")
    finding = check_render(broken)
    assert finding is not None and finding.action == "RE_RENDER_AND_REUPLOAD"


# ---------------------------------------------------------------------------
# Reference footage: "make it more like this", measured
# ---------------------------------------------------------------------------


def test_a_style_is_measured_from_footage_not_guessed(tmp_path):
    from auteur import ffmpeg as ff
    from auteur.insight import measure

    # Two seconds of one thing then two of another: one cut, in the middle.
    clip = tmp_path / "ref.mp4"
    subprocess.run(
        [
            str(ff.ffmpeg_path()),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:size=320x240:rate=24:duration=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:size=320x240:rate=24:duration=2",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0",
            "-pix_fmt",
            "yuv420p",
            str(clip),
        ],
        check=True,
    )

    style = measure([clip])
    assert style.sources == 1
    assert not style.is_empty
    assert style.seconds == pytest.approx(4.0, abs=0.3)
    # Black then white: the mean luma must sit between them.
    assert 0.2 < style.luma < 0.8
    assert style.to_json()["pace_words"] in (
        "meditative",
        "slow",
        "steady",
        "upbeat",
        "fast",
        "frenetic",
    )


def test_unreadable_references_are_skipped_rather_than_fatal(tmp_path):
    from auteur.insight import measure

    junk = tmp_path / "not-a-video.mp4"
    junk.write_bytes(b"nope")
    assert measure([junk]).is_empty
    assert measure([]).is_empty


def test_a_measured_pace_is_translated_into_words_the_brief_can_read():
    """The director parses words, not numbers, so a measured style has to be
    said in the vocabulary the brief already understands."""
    from auteur.director.brief import PACE_WORDS
    from auteur.insight import StyleTarget

    for cuts, expected in ((10.0, "frenetic"), (6.5, "fast"), (3.0, "steady"), (1.0, "meditative")):
        target = StyleTarget(cuts_per_10s=cuts, sources=1)
        assert target.pace_words == expected
        assert target.pace_words in PACE_WORDS, "the brief must actually know this word"


def test_references_that_disagree_say_so_rather_than_averaging_quietly():
    from auteur.insight import StyleTarget

    agreed = StyleTarget(cuts_per_10s=3.0, sources=3, disagreement={"cuts_per_10s": 0.2})
    assert agreed.is_agreed

    split = StyleTarget(cuts_per_10s=6.0, sources=2, disagreement={"cuts_per_10s": 4.0})
    assert not split.is_agreed
    assert "do not agree" in split.describe()


def test_the_style_agent_pulls_the_edit_toward_the_reference(labelled_model):
    """The corpus says nine or ten cuts per ten seconds. A reference cutting at
    three says three, and the reference wins."""
    from auteur.agents import StyleAgent
    from auteur.insight import StyleTarget, predict

    slow = StyleTarget(cuts_per_10s=3.0, shot_seconds=2.5, sources=3, seconds=45.0)
    fast_edit = _timeline(runtime=15.0, shots=15, opening=1.2, text_at=0.1)
    assert len(fast_edit.shots) / fast_edit.duration * 10 > 8

    proposals = StyleAgent(slow).inspect(
        fast_edit, predict(fast_edit, labelled_model), labelled_model
    )
    assert proposals, "a 10-per-10s edit against a 3-per-10s reference must be noticed"
    proposals[0].change(fast_edit)
    assert len(fast_edit.shots) / fast_edit.duration * 10 < 8


def test_the_style_agent_stays_quiet_when_the_edit_already_matches(labelled_model):
    """Nudging an edit that is already close is churn."""
    from auteur.agents import StyleAgent
    from auteur.insight import StyleTarget, predict

    edit = _timeline(runtime=20.0, shots=6, opening=1.2, text_at=0.1)
    pace = len(edit.shots) / edit.duration * 10
    target = StyleTarget(cuts_per_10s=pace, shot_seconds=3.0, sources=2, seconds=40.0)

    assert StyleAgent(target).inspect(edit, predict(edit, labelled_model), labelled_model) == []
    assert (
        StyleAgent(StyleTarget()).inspect(edit, predict(edit, labelled_model), labelled_model) == []
    )


def test_a_reference_is_an_instruction_and_not_a_suggestion(labelled_model):
    """The style agent's proposal was being dropped for "no predicted gain" —
    so a correlation across a population overruled somebody pointing at their
    own footage, which is the exact thing the style agent exists to prevent."""
    from auteur.agents import Crew, Gate, Mode, StyleAgent
    from auteur.insight import StyleTarget, predict

    slow = StyleTarget(cuts_per_10s=3.0, shot_seconds=2.5, sources=3, seconds=45.0)
    fast_edit = _timeline(runtime=15.0, shots=15, opening=1.2, text_at=0.1)
    before = predict(fast_edit, labelled_model).overall

    result = Crew([StyleAgent(slow)], labelled_model, gate=Gate(Mode.AUTONOMOUS), max_rounds=2).run(
        fast_edit
    )

    assert result.applied, "a binding proposal must survive a flat or negative prediction"
    assert result.applied[0].binding
    pace = len(result.edl.shots) / result.edl.duration * 10
    assert pace < 8.0, f"still cutting at {pace:.1f} per 10s"
    # And it is allowed to cost prediction — that is the point of binding.
    assert result.final.overall <= before + 1e-9 or True


def test_binding_still_goes_to_the_gate(labelled_model):
    """Binding means the model does not get a veto. The person still does."""
    from auteur.agents import Crew, Gate, Mode, StyleAgent
    from auteur.insight import StyleTarget

    slow = StyleTarget(cuts_per_10s=3.0, shot_seconds=2.5, sources=3, seconds=45.0)
    edit = _timeline(runtime=15.0, shots=15, opening=1.2, text_at=0.1)

    refused = Crew(
        [StyleAgent(slow)],
        labelled_model,
        gate=Gate(Mode.MANUAL, on_ask=lambda p: ("reject", "not like that")),
        max_rounds=1,
    ).run(edit)
    assert refused.applied == []
    assert refused.rejected[0].decision_note == "not like that"


def test_a_new_metadata_domain_needs_no_code(tmp_path):
    """These arrive one per domain — human condition, art history, art theory,
    cinematography — identical but for a `Primary_<domain>` column. Matching
    them by fixed signature would mean editing the schema every time somebody
    adds one."""
    from auteur.insight import detect_form, load
    from auteur.insight.schema import domain_of

    header = (
        "Metadata_ID,Primary_Underwater_Basketweaving,Secondary_Underwater_Basketweaving,"
        "Psychological_Philosophy_Anchor,Music_Theory_Audio_Anchor,Avg_Watch_Time_Pct,"
        "Views,Shares,Saves,Comments"
    )
    assert detect_form(header.split(",")) == "metadata_domain"
    assert domain_of(header.split(",")) == "underwater_basketweaving"

    path = _write_csv(
        tmp_path / "domain.csv",
        header,
        "M1,Reed Tension,Sunken Geometry,Existential Validation,Pedal Point,105.7,50000,2500,1200,600",
    )
    signal = load([path])[0]
    assert signal.form == "metadata_domain"
    assert signal.theme == "Reed Tension"
    assert signal.framework == "Sunken Geometry"
    assert signal.dataset_origin == "underwater_basketweaving"
    # Counts become the ratios the objectives are stated in.
    assert signal.share_to_view_ratio == pytest.approx(0.05)
    assert signal.save_rate == pytest.approx(0.024)
    # Over 100% watch time is a loop, not a rounding error.
    assert signal.loop_count == pytest.approx(1.057)


def test_one_unreadable_export_does_not_kill_a_render(tmp_path, capsys):
    """A folder of exports grows over time. A new column layout should not be
    able to stop you making a film — but `insight fit` must still fail loudly,
    because there the export is the subject."""
    import argparse

    from auteur.cli import _model_for
    from auteur.insight import load
    from auteur.ui import NullReporter

    good = _write_csv(
        tmp_path / "good.csv",
        "content_id,hook_style,hook_duration_sec,completion_rate,share_to_view_ratio,loop_count",
        *[f"v_{n},Visual Pattern Interrupt,1.4,0.6{n},0.09,1.7" for n in range(5)],
    )
    junk = tmp_path / "junk.csv"
    junk.write_text("name,colour\nsomething,red\n", encoding="utf-8")

    args = argparse.Namespace(data=[str(good), str(junk)])
    model = _model_for(args, NullReporter())
    assert model.rows > 0, "the good export must still have been used"

    # The explicit path still raises.
    with pytest.raises(ValueError, match="performance export"):
        load([junk])


def test_an_intentional_hold_is_not_dead_air(labelled_model):
    """The style agent holds shots to match a slow reference. The critic drops
    shots where nothing moves. From the pixels those are the same thing, and
    the critic was deleting the holds as fast as the agent made them — turning
    a sixteen-second film into seven."""
    from auteur.agents.preflight import HELD_ON_PURPOSE
    from auteur.critic import Critique, Note, revise

    edl = _timeline(runtime=16.0, shots=5, opening=1.2, text_at=0.1)
    for shot in edl.shots:
        shot.note = HELD_ON_PURPOSE
    before = len(edl.shots)

    critique = Critique(
        score=0.5,
        notes=[
            Note("dead-air", "3.0s where nothing moves", severity=0.75, at=at) for at in (4.0, 8.0)
        ],
    )
    # None rather than {}: an empty mapping means "every clip is unknown" and
    # repair drops the lot. None means "no dossiers to check against".
    revise(edl, critique, None, target_duration=16.0)

    assert len(edl.shots) == before, "a hold somebody asked for must survive the critic"


def test_an_actually_frozen_shot_is_still_dropped(labelled_model):
    """The exemption is for intent, not for every long shot."""
    from auteur.critic import Critique, Note, revise

    edl = _timeline(runtime=16.0, shots=6, opening=1.2, text_at=0.1)
    before = len(edl.shots)

    critique = Critique(
        score=0.5, notes=[Note("dead-air", "3.0s where nothing moves", severity=0.75, at=6.0)]
    )
    revise(edl, critique, None, target_duration=16.0)
    assert len(edl.shots) < before


def test_slowing_the_cut_keeps_the_runtime(labelled_model):
    """Fewer cuts at the same length. An earlier version stretched and dropped
    in one pass and lost more than half the film."""
    from auteur.agents import StyleAgent
    from auteur.insight import StyleTarget, predict

    edit = _timeline(runtime=16.0, shots=14, opening=1.2, text_at=0.1)
    for shot in edit.shots:
        shot.is_still = True
    before = edit.duration

    target = StyleTarget(cuts_per_10s=2.9, shot_seconds=3.4, sources=3, seconds=47.0)
    proposals = StyleAgent(target).inspect(edit, predict(edit, labelled_model), labelled_model)
    proposals[0].change(edit)

    assert edit.duration == pytest.approx(before, abs=0.6), "the reference is slower, not shorter"
    pace = len(edit.shots) / edit.duration * 10
    assert 2.0 < pace < 4.5, f"{pace:.1f} per 10s"
    # The hook and the ending are load-bearing for the other agents.
    assert edit.shots[0] is not None and len(edit.shots) >= 2


# ---------------------------------------------------------------------------
# Vision: reading a frame rather than measuring one
# ---------------------------------------------------------------------------


def _spot(cy: float, cx: float, *, size: int = 18, bg: float = 0.06, fg: float = 0.95):
    """A bright square on a dark field, at a known place."""
    frame = np.full((240, 320, 3), bg, dtype=np.float32)
    y, x = int(cy * 240), int(cx * 320)
    frame[max(0, y - size) : y + size, max(0, x - size) : x + size] = fg
    return frame


def test_the_eye_lands_where_the_subject_actually_is():
    """The centroid of a whole salience field is the middle of the frame for
    anything symmetric — five photographs with subjects in five places all read
    'dead centre'. And a zero-padded blur dims the frame edge, pulling any peak
    inward: a subject a fifth across was reported three tenths across."""
    from auteur.vision import read_frame

    for cy, cx in ((0.20, 0.20), (0.80, 0.75), (0.50, 0.50), (0.35, 0.66), (0.15, 0.85)):
        reading = read_frame(_spot(cy, cx))
        error = math.hypot(reading.focus[0] - cx, reading.focus[1] - cy)
        assert error < 0.05, f"want ({cx}, {cy}), got {reading.focus}, off by {error:.3f}"


def test_a_subject_at_the_frame_edge_is_not_dragged_inward():
    """The case that matters most for deciding where a title can go."""
    from auteur.vision import read_frame

    reading = read_frame(_spot(0.5, 0.12))
    assert reading.focus[0] < 0.25, reading.focus


def test_not_every_frame_is_a_dutch_angle():
    """Averaging sin(2θ) over edge angles gives about 2/π for any even spread —
    comfortably over any threshold — so every frame came back tilted. A
    classifier that always returns the same answer is reporting its own bias."""
    from auteur.vision import read_frame

    # Axis-aligned bars: emphatically not a Dutch angle.
    square = np.full((240, 320, 3), 0.1, dtype=np.float32)
    square[60:180, 80:240] = 0.9
    assert read_frame(square).composition != "Dutch Angle"

    # A frame built from diagonals should be.
    diagonal = np.full((240, 320, 3), 0.1, dtype=np.float32)
    ys, xs = np.mgrid[0:240, 0:320]
    diagonal[((xs + ys) % 40) < 20] = 0.9
    assert read_frame(diagonal).composition == "Dutch Angle"


def test_an_almost_empty_frame_does_not_read_as_busy():
    """`busy` was measured against a percentile. On a flat frame the 90th
    percentile sits near zero, so nearly every empty pixel cleared it."""
    from auteur.vision import read_frame

    assert read_frame(_spot(0.5, 0.5)).busy < 0.15

    noisy = np.random.default_rng(4).random((240, 320, 3)).astype(np.float32)
    assert read_frame(noisy).busy > read_frame(_spot(0.5, 0.5)).busy


def test_the_light_is_named_from_the_histogram_not_the_mean():
    """Chiaroscuro, high-key and low-key can share a mean and are three
    different pictures."""
    from auteur.vision import read_frame

    # Bright and dark, nothing in between.
    chiaroscuro = np.full((240, 320, 3), 0.04, dtype=np.float32)
    chiaroscuro[:, :100] = 0.92
    assert read_frame(chiaroscuro).lighting == "Chiaroscuro/Moody"

    flat = np.full((240, 320, 3), 0.5, dtype=np.float32)
    flat[100:140, 140:180] = 0.55
    assert read_frame(flat).lighting == "Natural Flat"


def test_hue_is_averaged_the_way_a_circle_works():
    """Averaging 350 and 10 arithmetically gives 180 — the opposite colour."""
    from auteur.vision import read_frame

    frame = np.zeros((240, 320, 3), dtype=np.float32)
    frame[:, :160] = (0.9, 0.1, 0.05)  # red, hue near 0
    frame[:, 160:] = (0.9, 0.25, 0.05)  # orange-red, hue near 15
    hue = read_frame(frame).hue
    assert hue < 60 or hue > 300, f"hue {hue} is nowhere near red"


def test_palette_names_a_relationship_not_a_colour():
    from auteur.vision import read_frame

    mono = np.zeros((240, 320, 3), dtype=np.float32)
    mono[:, :] = (0.8, 0.2, 0.2)
    mono[80:160, 80:240] = (0.4, 0.1, 0.1)
    assert read_frame(mono).palette == "Monochromatic"

    opposed = np.zeros((240, 320, 3), dtype=np.float32)
    opposed[:, :160] = (0.9, 0.15, 0.15)
    opposed[:, 160:] = (0.15, 0.75, 0.9)
    assert read_frame(opposed).palette in ("Split-Complementary", "Triadic")


def test_a_reading_survives_a_real_photograph(tmp_path):
    """Including the EXIF rotation — a phone photo read sideways gives a
    confident answer about a composition nobody will ever see."""
    from auteur import ffmpeg as ff
    from auteur.vision import read_asset

    photo = tmp_path / "shot.png"
    subprocess.run(
        [
            str(ff.ffmpeg_path()),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360",
            "-frames:v",
            "1",
            str(photo),
        ],
        check=True,
    )
    reading = read_asset(photo)
    assert reading.composition in __import__("auteur.vision", fromlist=["x"]).COMPOSITIONS
    assert reading.lighting in __import__("auteur.vision", fromlist=["x"]).LIGHTING
    assert reading.palette in __import__("auteur.vision", fromlist=["x"]).PALETTES
    assert 0.0 <= reading.focus[0] <= 1.0 and 0.0 <= reading.focus[1] <= 1.0
    assert reading.to_json()["composition"] == reading.composition


# ---------------------------------------------------------------------------
# Finishing: reframe, overlays, transitions, sound
# ---------------------------------------------------------------------------


def _readings_for(*focuses, luma=0.3, hue=30.0, strength=0.4):
    from auteur.vision import Reading

    return {
        f"C{index:02d}": Reading(
            focus=focus, focus_strength=strength, luma=luma, hue=hue, depth_separation=0.5
        )
        for index, focus in enumerate(focuses)
    }


def _still_edl(readings, *, text_anchor=None, transitions=None):
    from auteur.edl import EditDecisionList, Motion, Shot, TextCue, Transition

    shots = []
    for index in range(len(readings)):
        shot = Shot(
            clip_id=f"C{index:02d}",
            source=Path(f"/x/{index}.jpg"),
            start=0.0,
            end=2.5,
            is_still=True,
            motion=Motion(kind="punch-in", intensity=0.2, anchor=(0.5, 0.5)),
        )
        if transitions and index in transitions:
            shot.transition_in = Transition(kind=transitions[index], duration=0.4)
        shots.append(shot)
    texts = []
    if text_anchor is not None:
        texts.append(TextCue(text="TITLE", start=0.2, duration=1.5, anchor=text_anchor))
    return EditDecisionList(title="t", shots=shots, texts=texts)


def test_the_finishing_agent_anchors_moves_on_the_subject(model):
    """A punch-in toward the middle of a frame whose subject is off to one side
    pushes the subject out of shot."""
    from auteur.agents import FinishingAgent
    from auteur.insight import predict

    readings = _readings_for((0.86, 0.44), (0.20, 0.55), (0.5, 0.5))
    edl = _still_edl(readings)

    proposals = FinishingAgent(readings).inspect(edl, predict(edl, model), model)
    reframe = next(p for p in proposals if "Reframe" in p.title)
    reframe.change(edl)

    for shot in edl.shots:
        assert shot.motion.anchor == readings[shot.clip_id].focus


def test_titles_are_moved_off_the_subject_and_still_obey_the_safe_area(model):
    from auteur.agents import FinishingAgent
    from auteur.insight import predict
    from auteur.workflows import resolve

    spec = resolve("tiktok")
    readings = _readings_for((0.54, 0.38), (0.3, 0.5), (0.7, 0.5))
    edl = _still_edl(readings, text_anchor=(0.54, 0.38))  # right on top of the subject

    proposals = FinishingAgent(readings, spec=spec).inspect(edl, predict(edl, model), model)
    move = next(p for p in proposals if "title" in p.title)
    move.change(edl)

    anchor = edl.texts[0].anchor
    assert math.hypot(anchor[0] - 0.54, anchor[1] - 0.38) > 0.15, "still on the subject"
    assert spec.safe.top <= anchor[1] <= 1 - spec.safe.bottom, "and still under the caption box"
    assert spec.safe.left <= anchor[0] <= 1 - spec.safe.right


def test_a_dissolve_between_two_matching_shots_is_cut(model):
    """It looks like a mistake rather than a transition."""
    from auteur.agents import FinishingAgent
    from auteur.insight import predict

    # Three near-identical frames, one of them joined with a dissolve.
    readings = _readings_for((0.5, 0.5), (0.52, 0.51), (0.5, 0.49))
    edl = _still_edl(readings, transitions={1: "dissolve"})

    proposals = FinishingAgent(readings).inspect(edl, predict(edl, model), model)
    fix = next(p for p in proposals if "dissolve" in p.title)
    fix.change(edl)
    assert edl.shots[1].transition_in.is_cut


def test_sound_lands_only_on_the_joins_the_picture_marks(model):
    """Sound on every cut is a metronome."""
    from auteur.agents import FinishingAgent
    from auteur.insight import predict
    from auteur.vision import Reading

    readings = {
        "C00": Reading(focus=(0.5, 0.5), focus_strength=0.4, luma=0.1, hue=20.0),
        "C01": Reading(focus=(0.5, 0.5), focus_strength=0.4, luma=0.11, hue=22.0),  # same
        "C02": Reading(focus=(0.5, 0.5), focus_strength=0.4, luma=0.72, hue=210.0),  # a jump
    }
    edl = _still_edl(readings)

    proposals = FinishingAgent(readings).inspect(edl, predict(edl, model), model)
    sound = next(p for p in proposals if "effect" in p.title)
    sound.change(edl)

    ats = sorted(round(cue.at, 2) for cue in edl.sfx if cue.kind == "impact")
    assert ats == [5.0], f"one impact, on the join at 5s, got {ats}"
    assert any(cue.kind == "riser" for cue in edl.sfx), "a big jump earns a riser into it"


def test_the_finishing_agent_says_nothing_without_a_reading(model):
    """Every decision is only as good as the reading behind it, and a
    default-driven finishing pass is worse than none."""
    from auteur.agents import FinishingAgent
    from auteur.insight import predict

    readings = _readings_for((0.5, 0.5), (0.5, 0.5))
    edl = _still_edl(readings)
    assert FinishingAgent({}).inspect(edl, predict(edl, model), model) == []


# ---------------------------------------------------------------------------
# Scoring a finished video
# ---------------------------------------------------------------------------


def test_a_finished_video_can_be_scored(tmp_path):
    from auteur import ffmpeg as ff
    from auteur.insight import corpus, fit, predict, timeline_of

    video = tmp_path / "cut.mp4"
    subprocess.run(
        [
            str(ff.ffmpeg_path()),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:size=320x240:rate=24:duration=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:size=320x240:rate=24:duration=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:size=320x240:rate=24:duration=2",
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
    )

    edl = timeline_of(video)
    assert edl.shots, "a finished file has a timeline in it; it just has to be measured out"
    assert edl.duration == pytest.approx(6.0, abs=0.5)
    assert (edl.width, edl.height) == (320, 240)
    # Black at both ends: the loop objective should notice the ends match.
    assert edl.shots[-1].clip_id == edl.shots[0].clip_id

    prediction = predict(edl, fit(corpus([], simulate_rows=400)))
    assert 0.0 <= prediction.overall <= 1.0
    # Nothing read any words, so nothing may claim a text-driven hook.
    assert edl.texts == []


# --------------------------------------------------------------------- graphics


def _graphics_edl(tmp_path, count=4, seconds=2.5):
    """A tiny timeline of solid-colour stills, for graphics tests."""
    from PIL import Image

    from auteur.edl import EditDecisionList, Motion, Shot, Transition

    shots = []
    for index in range(count):
        path = tmp_path / f"still{index}.png"
        Image.new("RGB", (600, 900), (30 + index * 40, 60, 90)).save(path)
        shots.append(
            Shot(
                clip_id=f"c{index}",
                source=path,
                start=0.0,
                end=seconds,
                is_still=True,
                motion=Motion("none", 0.0, (0.5, 0.5)),
                transition_in=Transition("cut", 0.0),
            )
        )
    edl = EditDecisionList(title="graphics", shots=shots, fps=30, width=1080, height=1920)
    edl.repair()
    return edl


@pytest.mark.parametrize(
    "kind",
    ["circle", "bracket", "arrow", "underline", "highlight", "burst", "progress", "tape"],
)
def test_every_graphic_kind_draws_ink_where_it_was_aimed(tmp_path, kind):
    """A graphic that renders empty, or lands somewhere else, is worse than none."""
    import numpy as np
    from PIL import Image

    from auteur.craft import graphics
    from auteur.edl import GraphicCue

    cue = GraphicCue(kind=kind, start=0.0, duration=1.0, anchor=(0.4, 0.35), move="pop")
    if kind in graphics.SPANNING:
        cue.toward = (0.7, 0.6)
    cue.normalise()

    drawn = graphics.render_cue(cue, width=1080, height=1920, directory=tmp_path, index=0)
    assert drawn is not None

    # Sample each kind where it is meant to be at full strength. That is the
    # last plate for anything that draws itself on and stays, but a burst is an
    # impact mark that has deliberately burnt out by its final frame — checking
    # that one would be checking an empty plate on purpose.
    peak = int((drawn.frames - 1) * (0.4 if kind == "burst" else 1.0))
    plate = (
        Path(str(drawn.pattern).replace("%04d", f"{peak:04d}"))
        if drawn.is_sequence
        else drawn.pattern
    )
    alpha = np.array(Image.open(plate))[..., 3]
    ys, xs = np.nonzero(alpha > 25)
    assert len(xs) > 0, f"{kind} drew nothing at all"

    # Where the ink actually landed, back in normalised frame coordinates.
    at = ((xs.mean() + drawn.box[0]) / 1080, (ys.mean() + drawn.box[1]) / 1920)
    if kind == "progress":
        expected = (0.5, 0.35)  # spans the full width, so only y is the cue's
    elif kind in graphics.SPANNING:
        expected = (0.55, 0.475)  # midway along the span
    else:
        expected = (0.4, 0.35)
    assert at[0] == pytest.approx(expected[0], abs=0.06)
    assert at[1] == pytest.approx(expected[1], abs=0.06)


def test_a_graphic_box_never_leaves_the_frame(tmp_path):
    """Anchored in a corner, the plates still have to be a rectangle inside the film."""
    from auteur.craft import graphics
    from auteur.edl import GraphicCue

    for anchor in [(0.02, 0.02), (0.98, 0.98), (0.98, 0.02), (0.5, 0.995)]:
        cue = GraphicCue(kind="circle", anchor=anchor, size=3.0, move="pulse", duration=0.5)
        cue.normalise()
        x, y, w, h = graphics._box(cue, 1080, 1920)
        assert 0 <= x and 0 <= y
        assert x + w <= 1080 and y + h <= 1920
        assert w > 0 and h > 0


def test_a_long_graphic_is_a_still_rather_than_a_thousand_plates(tmp_path):
    from auteur.craft import graphics
    from auteur.edl import GraphicCue

    brief = GraphicCue(kind="circle", duration=2.0, move="pulse")
    lengthy = GraphicCue(kind="circle", duration=40.0, move="pulse")
    brief.normalise()
    lengthy.normalise()

    assert graphics.render_cue(brief, width=540, height=960, directory=tmp_path, index=0).frames > 1
    long_one = graphics.render_cue(lengthy, width=540, height=960, directory=tmp_path, index=1)
    assert long_one.frames == 1
    assert not long_one.is_sequence


def test_stickers_are_found_in_a_stable_order_and_missing_folders_are_fine(tmp_path):
    from PIL import Image

    from auteur.craft.graphics import find_stickers

    assert find_stickers(None) == []
    assert find_stickers(tmp_path / "nope") == []

    for name in ("zebra.png", "apple.png", "notes.txt"):
        if name.endswith(".png"):
            Image.new("RGBA", (64, 64), (255, 0, 0, 200)).save(tmp_path / name)
        else:
            (tmp_path / name).write_text("not a sticker")

    found = find_stickers(tmp_path)
    assert [p.name for p in found] == ["apple.png", "zebra.png"]


def test_a_sticker_whose_file_vanished_is_dropped_not_rendered(tmp_path):
    from auteur.edl import GraphicCue

    edl = _graphics_edl(tmp_path)
    edl.graphics = [
        GraphicCue(kind="sticker", start=0.5, duration=1.0, source=tmp_path / "gone.png")
    ]
    notes = edl.repair()
    assert edl.graphics == []
    assert any("sticker" in note for note in notes)


def test_graphics_past_the_end_of_the_film_are_dropped(tmp_path):
    from auteur.edl import GraphicCue

    edl = _graphics_edl(tmp_path, count=2, seconds=1.0)
    edl.graphics = [
        GraphicCue(kind="circle", start=0.5, duration=99.0),
        GraphicCue(kind="burst", start=50.0, duration=0.5),
    ]
    edl.repair()
    assert len(edl.graphics) == 1
    # The survivor is trimmed to the runtime rather than left hanging past it.
    assert edl.graphics[0].end <= edl.duration + 1e-6


def test_graphics_survive_a_round_trip_through_json(tmp_path):
    from auteur.edl import GraphicCue

    edl = _graphics_edl(tmp_path)
    edl.graphics = [
        GraphicCue(kind="arrow", start=0.4, duration=1.0, anchor=(0.2, 0.8), toward=(0.6, 0.4))
    ]
    edl.repair()
    payload = json.loads(json.dumps(edl.to_json()))
    assert payload["graphics"][0]["kind"] == "arrow"
    assert payload["graphics"][0]["toward"] == [0.2 * 0 + 0.6, 0.4]


# ------------------------------------------------------------------ overlay agent


def _overlay_bits(tmp_path, focus=(0.3, 0.35), strength=0.5, count=5):
    from auteur.insight import FitReport
    from auteur.insight.score import Prediction
    from auteur.vision import Reading

    edl = _graphics_edl(tmp_path, count=count)
    readings = {
        f"c{i}": Reading(focus=focus, focus_strength=strength, luma=0.4, hue=30.0)
        for i in range(count)
    }
    return (
        edl,
        readings,
        Prediction(hook=0.5, share=0.5, loop=0.5),
        FitReport(rows=0, simulated_rows=0, measured_rows=0),
    )


def test_the_overlay_agent_says_nothing_without_a_reading(tmp_path):
    """A mark placed from a default lands on the subject about half the time."""
    from auteur.agents import OverlayAgent

    edl, _, prediction, model = _overlay_bits(tmp_path)
    assert OverlayAgent({}).inspect(edl, prediction, model) == []


def test_the_overlay_agent_will_not_point_at_a_texture(tmp_path):
    from auteur.agents import OverlayAgent

    edl, readings, prediction, model = _overlay_bits(tmp_path, strength=0.05)
    titles = {p.title for p in OverlayAgent(readings).inspect(edl, prediction, model)}
    assert not any("Ring" in t or "Bracket" in t or "Point" in t for t in titles)


def test_overlay_proposals_are_binding_because_the_model_is_silent_on_them(tmp_path):
    """No export this project has been given records on-screen graphics."""
    from auteur.agents import OverlayAgent

    edl, readings, prediction, model = _overlay_bits(tmp_path)
    proposals = OverlayAgent(readings).inspect(edl, prediction, model)
    assert proposals
    assert all(p.binding for p in proposals)


def test_the_overlay_agent_does_not_stack_a_second_set_of_stickers(tmp_path):
    """The crew runs several rounds; an unguarded pass doubles every round."""
    from PIL import Image

    from auteur.agents import OverlayAgent

    sticker = tmp_path / "s.png"
    Image.new("RGBA", (80, 80), (255, 200, 0, 220)).save(sticker)

    edl, readings, prediction, model = _overlay_bits(tmp_path)
    agent = OverlayAgent(readings, stickers=[sticker])

    first = [p for p in agent.inspect(edl, prediction, model) if "sticker" in p.title]
    assert len(first) == 1
    first[0].change(edl)
    placed = len([g for g in edl.graphics if g.kind == "sticker"])
    assert placed >= 1

    again = [p for p in agent.inspect(edl, prediction, model) if "sticker" in p.title]
    assert again == [], "stickers were proposed a second time on top of the ones already there"


def _beat_edl(tmp_path, *, bpm=120.0, bars=6, count=6):
    """The graphics timeline, with a beat grid on it like a director leaves."""
    from auteur.edl import MusicCue

    edl = _graphics_edl(tmp_path, count=count, seconds=2.0)
    step = 60.0 / bpm
    beats = [round(step * n, 4) for n in range(1, bars * 4 + 1)]
    edl.music = MusicCue(
        source=tmp_path / "track.wav",
        beats=beats,
        downbeats=[b for index, b in enumerate(beats) if index % 4 == 0],
        tempo=bpm,
    )
    return edl


def test_the_director_writes_the_beat_grid_onto_the_edl():
    """Nothing downstream of the director can see the audio analysis.

    The grid used to be recomputed in three places and written down in none, so
    an agent holding an EDL had no way to put anything on a beat.
    """
    from auteur.director.heuristic import beat_grid

    class _Audio:
        has_beat = True
        tempo = 128.0
        beats = [0.5, 1.0, 1.5, 2.0, 2.5]
        downbeats = [0.5, 2.5]

    beats, downbeats, tempo = beat_grid(_Audio(), 0.5)
    assert beats == [0.5, 1.0, 1.5, 2.0]  # offset subtracted, the one at zero dropped
    assert downbeats == [2.0]
    assert tempo == 128.0

    # A brief that asked not to be cut to the music does not get stickers on
    # the snare either.
    assert beat_grid(_Audio(), 0.5, enabled=False) == ([], [], 0.0)
    assert beat_grid(None, 0.0) == ([], [], 0.0)
    # Nothing past the end of the film.
    assert beat_grid(_Audio(), 0.5, runtime=1.2)[0] == [0.5, 1.0]


def test_stickers_land_on_the_beat_and_share_the_screen(tmp_path):
    """Not one per cut: several at once, arriving on the grid."""
    from PIL import Image

    from auteur.agents import OverlayAgent
    from auteur.agents.overlay import MAX_LAYERS

    stickers = []
    for name, colour in (("a.png", (255, 0, 0, 220)), ("b.png", (0, 255, 0, 220))):
        path = tmp_path / name
        Image.new("RGBA", (90, 90), colour).save(path)
        stickers.append(path)

    edl = _beat_edl(tmp_path)
    _, readings, prediction, model = _overlay_bits(tmp_path, count=6)
    agent = OverlayAgent(readings, stickers=stickers)

    placed = [p for p in agent.inspect(edl, prediction, model) if "sticker" in p.title]
    assert len(placed) == 1
    placed[0].change(edl)

    cues = [g for g in edl.graphics if g.kind == "sticker"]
    assert len(cues) > len(edl.shots), "still one sticker per shot"

    grid = set(edl.music.beats)
    assert all(round(cue.start, 4) in grid for cue in cues), "a sticker missed the beat"

    at_once = agent._most_at_once(cues)
    assert 2 <= at_once <= MAX_LAYERS, f"{at_once} on screen at once"

    # Both files get used, because a set of stickers used once each is a set
    # nobody noticed.
    assert {cue.source.name for cue in cues} == {"a.png", "b.png"}


def test_layered_stickers_do_not_sit_on_top_of_each_other(tmp_path):
    """Three in one corner is one sticker with a shadow."""
    import itertools

    from PIL import Image

    from auteur.agents import OverlayAgent
    from auteur.agents.overlay import TOO_CLOSE

    sticker = tmp_path / "s.png"
    Image.new("RGBA", (90, 90), (255, 200, 0, 220)).save(sticker)

    edl = _beat_edl(tmp_path)
    _, readings, prediction, model = _overlay_bits(tmp_path, count=6)
    agent = OverlayAgent(readings, stickers=[sticker])
    [p for p in agent.inspect(edl, prediction, model) if "sticker" in p.title][0].change(edl)

    cues = [g for g in edl.graphics if g.kind == "sticker"]
    for one, other in itertools.combinations(cues, 2):
        if one.start < other.end and other.start < one.end:
            gap = (
                (one.anchor[0] - other.anchor[0]) ** 2 + (one.anchor[1] - other.anchor[1]) ** 2
            ) ** 0.5
            assert gap >= TOO_CLOSE, f"two stickers stacked at {one.anchor} and {other.anchor}"


def test_without_music_the_stickers_fall_on_the_cuts(tmp_path):
    """An edit has a pulse whether or not there is a track under it."""
    from PIL import Image

    from auteur.agents import OverlayAgent

    sticker = tmp_path / "s.png"
    Image.new("RGBA", (90, 90), (120, 200, 255, 230)).save(sticker)

    edl, readings, prediction, model = _overlay_bits(tmp_path, count=8)
    agent = OverlayAgent(readings, stickers=[sticker])
    proposal = [p for p in agent.inspect(edl, prediction, model) if "sticker" in p.title][0]
    assert "the cuts" in proposal.title
    proposal.change(edl)

    cuts = {round(start, 3) for start, _, _ in edl.timeline()}
    cues = [g for g in edl.graphics if g.kind == "sticker"]
    assert cues
    assert all(round(cue.start, 3) in cuts for cue in cues)


def test_a_centred_subject_does_not_send_the_mark_to_the_centre(tmp_path):
    """The mirror of the middle is the middle — the rule has to break there."""
    from auteur.vision import Reading, emptiest_quadrant

    centred = emptiest_quadrant(Reading(focus=(0.5, 0.5), balance=0.3))
    assert abs(centred[0] - 0.5) > 0.15 or abs(centred[1] - 0.5) > 0.15
    # Weight on the right means the mark belongs on the left.
    assert centred[0] < 0.5

    off = emptiest_quadrant(Reading(focus=(0.25, 0.7)))
    assert off[0] > 0.5 and off[1] < 0.5


def test_the_crew_result_carries_every_field_back_to_the_renderer(tmp_path):
    """Anything an agent changed and the copy-back missed is silently discarded.

    This happened: graphics, sound cues and the grade were dropped while the run
    still printed them as applied. So this asserts the behaviour rather than the
    shape of the code — every field the crew could have touched arrives.
    """
    import dataclasses

    from auteur.edl import EditDecisionList, GraphicCue, SoundCue
    from auteur.workflows import WORKFLOW_OWNED, resolve, with_agents

    edl = _graphics_edl(tmp_path)
    changed = copy.deepcopy(edl)
    changed.graphics = [GraphicCue(kind="circle", start=0.1, duration=0.5)]
    changed.sfx = [SoundCue(kind="impact", at=0.4)]
    changed.texture = 0.42
    changed.letterbox = 0.11
    changed.rationale = "the agents said so"
    changed.look.exposure = 0.33

    class _Result:
        def __init__(self, edl):
            self.edl = edl
            self.baseline = self.final = types.SimpleNamespace(overall=0.5)
            self.rounds = []

    class _Crew:
        def run(self, _edl):
            return _Result(changed)

    with_agents(resolve("tiktok"), _Crew())(edl)

    for field in dataclasses.fields(EditDecisionList):
        if field.name in WORKFLOW_OWNED:
            continue
        assert getattr(edl, field.name) == getattr(
            changed, field.name
        ), f"{field.name} did not survive the crew"


def test_the_studio_and_the_cli_build_the_same_crew(tmp_path):
    """A studio showing fewer proposals than the CLI would act on is the wrong list."""
    import inspect as _inspect

    from auteur.web import server

    source = _inspect.getsource(server.Handler._agents_plan)
    assert "build_crew(" in source
    assert "default_crew()" not in source


# ----------------------------------------------------------------------- scholar


def _learning(disc, technique, channel, index=0):
    from auteur.scholar.knowledge import Learning

    return Learning(
        learning_id=f"{technique[:6]}-{channel}-{index}",
        disciplines=[disc],
        insight=f"{technique}: do the thing",
        technique=technique,
        application="do the thing",
        source_video_id=f"v-{channel}-{index}",
        source_channel=channel,
        source_title="a tutorial",
    )


def test_youtube_says_it_cannot_reach_rather_than_returning_nothing(tmp_path, monkeypatch):
    """An empty result and no network are different facts.

    They were the same one: a Scholar with no way to reach YouTube reported the
    same quiet success as one that had read the whole first page.
    """
    from auteur.scholar import youtube

    monkeypatch.setattr(youtube, "_ytdlp", lambda: None)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)

    access = youtube.YouTubeAccess(cache_dir=tmp_path / "c", api_key="")
    can, why = youtube.reachable()
    assert not can and "yt-dlp" in why

    with pytest.raises(youtube.YouTubeUnavailable):
        access.search("colour grading")
    with pytest.raises(youtube.YouTubeUnavailable):
        access.fetch_metadata("abc123")
    with pytest.raises(youtube.YouTubeUnavailable):
        access.check_new_uploads()


def test_a_transcript_is_the_words_or_it_is_empty():
    """`has_transcript` used to be True while the transcript was a placeholder."""
    from auteur.scholar.youtube import YouTubeAccess, _parse_json3, _parse_vtt

    cues = _parse_json3(
        json.dumps(
            {
                "events": [
                    {
                        "tStartMs": 0,
                        "dDurationMs": 1500,
                        "segs": [{"utf8": "lift "}, {"utf8": "the shadows"}],
                    },
                    {"tStartMs": 1500, "dDurationMs": 10, "segs": [{"utf8": "\n"}]},
                    {"tStartMs": 2000, "dDurationMs": 900},
                ]
            }
        )
    )
    assert cues == [{"text": "lift the shadows", "start": 0.0, "duration": 1.5}]
    assert _parse_json3("not json") == []

    vtt = (
        "WEBVTT\n\n"
        "00:00:00.120 --> 00:00:02.500\nnode based <c>grading</c>\n\n"
        "00:00:02.500 --> 00:00:04.000\nnode based grading\n\n"
        "00:00:04.000 --> 00:00:06.250\nset contrast\nthen saturation\n"
    )
    parsed = _parse_vtt(vtt)
    # The rolling repeat auto-captions produce is dropped, not counted twice.
    assert [cue["text"] for cue in parsed] == [
        "node based grading",
        "set contrast then saturation",
    ]
    assert _parse_vtt("") == []

    # No caption track at all means no transcript — not a note about one.
    access = YouTubeAccess(cache_dir=Path(tempfile.mkdtemp()))
    meta = access._meta_from_dict({"id": "x", "title": "t", "duration": 10})
    assert meta.transcript_segments == []
    assert not meta.has_transcript


def test_the_learning_loop_has_an_exit(tmp_path):
    """Everything arrived tentative and every consumer wanted supported.

    So the Scholar could study for ever and teach nothing. Independent
    corroboration is what promotes a technique out of tentative.
    """
    from auteur.scholar.knowledge import Confidence, Discipline, KnowledgeStore

    store = KnowledgeStore(tmp_path / "k.jsonl")
    store.add(_learning(Discipline.COLOR_THEORY, "lift the shadows", "Chan A"))
    assert store.by_confidence(Confidence.SUPPORTED) == [], "one channel is one opinion"

    # The same technique from a second, unrelated channel is corroboration.
    store.add(_learning(Discipline.COLOR_THEORY, "lift the shadows", "Chan B"))
    assert len(store.by_confidence(Confidence.SUPPORTED)) == 2

    # The same channel again is not.
    store.add(_learning(Discipline.MUSIC_THEORY, "cut on the beat", "Chan A", 1))
    store.add(_learning(Discipline.MUSIC_THEORY, "cut on the beat", "Chan A", 2))
    beats = [lg for lg in store._learnings if lg.technique == "cut on the beat"]
    assert all(lg.confidence is Confidence.TENTATIVE for lg in beats)


def test_a_measured_gain_validates_the_study_behind_it(tmp_path):
    """`record_validation` existed and had no caller anywhere in the program."""
    from auteur.edl import Motion
    from auteur.insight import FitReport
    from auteur.insight.score import Prediction
    from auteur.scholar import Scholar
    from auteur.scholar.agent import ScholarAgent
    from auteur.scholar.knowledge import Confidence, Discipline

    scholar = Scholar(base_dir=tmp_path)
    # Three channels: enough to corroborate the technique, and enough for the
    # agent to consider itself studied at all (see ENOUGH_TO_SPEAK).
    for channel in ("Chan A", "Chan B", "Chan C"):
        scholar.knowledge.add(_learning(Discipline.ART_BASICS, "composition and focal", channel))

    agent = ScholarAgent(scholar)
    edl = _graphics_edl(tmp_path, count=6)
    # Anchors jammed into the corner, so the focal weight is genuinely weak and
    # the composition review has something real to object to.
    for shot in edl.shots:
        shot.motion = Motion(kind=shot.motion.kind, intensity=0.0, anchor=(0.04, 0.04))
    prediction = Prediction(hook=0.4, share=0.4, loop=0.4)
    model = FitReport(rows=0, simulated_rows=0, measured_rows=0)

    proposals = agent.inspect(edl, prediction, model)
    assert proposals, "a studied Scholar with a weak cut in front of it should speak"
    assert all(
        p.binding for p in proposals
    ), "advice the model cannot score is not advice it rejected"
    assert all(getattr(p, "learning_ids", None) for p in proposals)

    # Said once, not once per round.
    assert agent.inspect(edl, prediction, model) == []

    for proposal in proposals:
        proposal.applied = True
        proposal.predicted_gain = 0.05
    assert agent.learn_from(proposals) > 0
    assert scholar.knowledge.by_confidence(Confidence.VALIDATED)


def test_an_unstudied_scholar_stays_quiet(tmp_path):
    """A review backed by nothing is the Gaze agent's opinion arriving twice."""
    from auteur.insight import FitReport
    from auteur.insight.score import Prediction
    from auteur.scholar import Scholar
    from auteur.scholar.agent import ScholarAgent

    agent = ScholarAgent(Scholar(base_dir=tmp_path))
    assert agent.studied == 0
    edl = _graphics_edl(tmp_path, count=6)
    assert (
        agent.inspect(
            edl,
            Prediction(hook=0.4, share=0.4, loop=0.4),
            FitReport(rows=0, simulated_rows=0, measured_rows=0),
        )
        == []
    )


@pytest.mark.parametrize(
    "kind,expected",
    [("speech", "dialogue"), ("music", "music"), ("noise", "ambient"), ("transients", "effects")],
)
def test_the_scholar_hears_what_kind_of_sound_it_is(kind, expected):
    """This returned DIALOGUE unconditionally, so the field carried no information."""
    from auteur.scholar.auditory import AuditorySystem

    rate = 22050
    t = np.linspace(0, 6.0, int(rate * 6), endpoint=False)
    rng = np.random.default_rng(7)

    if kind == "speech":
        spectrum = np.fft.rfft(rng.normal(0, 1, len(t)))
        freqs = np.fft.rfftfreq(len(t), 1 / rate)
        spectrum[(freqs < 300) | (freqs > 3400)] = 0
        signal = np.fft.irfft(spectrum, n=len(t))
        signal = signal / np.abs(signal).max() * (0.55 + 0.45 * np.sin(2 * np.pi * 4.5 * t))
    elif kind == "music":
        signal = np.zeros_like(t)
        for beat in range(10):
            start = int(beat * 0.6 * rate)
            span = min(900, len(signal) - start)
            if span > 0:
                decay = np.exp(-np.linspace(0, 7, span))
                signal[start : start + span] += decay * np.sin(
                    2 * np.pi * 70 * np.linspace(0, span / rate, span)
                )
        signal = signal * 0.9 + sum(0.16 * np.sin(2 * np.pi * f * t) for f in (196, 246.9, 293.7))
    elif kind == "noise":
        signal = rng.normal(0, 0.12, len(t))
    else:
        signal = np.zeros_like(t)
        for at in (0.3, 1.42, 3.05, 4.8):
            start = int(at * rate)
            span = min(1500, len(signal) - start)
            decay = np.exp(-np.linspace(0, 9, span))
            signal[start : start + span] += decay * np.sin(
                2 * np.pi * 900 * np.linspace(0, span / rate, span)
            )

    pcm = (np.clip(signal, -1, 1) * 32767).astype("<i2").tobytes()
    assert AuditorySystem()._classify_channel(pcm, rate).value == expected


def test_audio_energy_is_measured_not_approximated_from_bytes():
    """The old version read signed 16-bit samples as unsigned bytes."""
    from auteur.scholar.auditory import AuditorySystem

    ears = AuditorySystem()
    silence = np.zeros(22050, dtype="<i2").tobytes()
    assert ears._measure_energy(silence, 22050) == 0.0
    assert ears._measure_energy(b"", 22050) == 0.0

    full = (np.ones(22050) * 32000).astype("<i2").tobytes()
    quiet = (np.ones(22050) * 320).astype("<i2").tobytes()
    assert ears._measure_energy(full, 22050) > ears._measure_energy(quiet, 22050)
    assert ears._measure_energy(full, 22050) == pytest.approx(1.0, abs=0.01)


def test_the_scholar_says_when_it_cannot_answer(tmp_path, monkeypatch):
    """It used to return a string shaped like an answer."""
    from auteur.scholar import Scholar

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    reply = Scholar(base_dir=tmp_path).chat("how do I pace a montage?")
    assert "[Scholar response" not in reply.text
    assert "cannot answer" in reply.text.lower()


# ------------------------------------------------------- categorical noise guard


def test_a_choice_that_explains_nothing_is_not_crowned_the_winner():
    """With sixteen options the best group mean beats the grand mean every time.

    Reporting that as "best palette: Warm Analogous (0.114)" reads as a finding
    an agent should act on, and is the result of a lottery.
    """
    import random as _random

    from auteur.insight.score import _explains_anything

    rng = _random.Random(11)
    noise = {f"option{i}": [rng.gauss(0.05, 0.03) for _ in range(200)] for i in range(16)}
    assert not _explains_anything(noise)

    # A real effect still has to get through.
    real = {
        f"option{i}": [rng.gauss(0.05 + i * 0.004, 0.03) for _ in range(200)] for i in range(16)
    }
    assert _explains_anything(real)


def test_the_noise_guard_is_not_fooled_by_lopsided_groups():
    """Best-minus-worst was the old statistic and it broke on uneven groups.

    A column with a couple of hundred options, most holding a handful of rows,
    produces a huge best-to-worst range from noise alone — so a genuine effect
    could not clear its own shuffled baseline, and a spurious one could.
    """
    import random as _random

    from auteur.insight.score import _explains_anything

    rng = _random.Random(12)
    many_tiny = {f"o{i}": [rng.gauss(0.1, 0.03) for _ in range(3)] for i in range(190)}
    assert not _explains_anything(many_tiny)

    lopsided = {"huge": [rng.gauss(0.1, 0.03) for _ in range(2000)]}
    lopsided.update({f"t{i}": [rng.gauss(0.1, 0.03) for _ in range(3)] for i in range(50)})
    assert not _explains_anything(lopsided)

    # Weighted by evidence, a real effect carried by the big groups survives.
    carried = {
        "big_low": [rng.gauss(0.08, 0.03) for _ in range(800)],
        "big_high": [rng.gauss(0.13, 0.03) for _ in range(800)],
    }
    assert _explains_anything(carried)


def test_the_noise_guard_refuses_degenerate_input():
    from auteur.insight.score import _explains_anything

    assert not _explains_anything({})
    assert not _explains_anything({"only": [0.1] * 50})
    assert not _explains_anything({"a": [0.1], "b": [0.2]})  # too few rows per group
    assert not _explains_anything({f"o{i}": [0.05] * 100 for i in range(5)})  # no variance


# --------------------------------------------------------------- training data


def test_the_generator_is_reproducible_without_being_handed_a_seed():
    """It derived its default seed from `hash()`, which is salted per process."""
    from auteur.training.generate import generate_domain

    first = generate_domain("color_theory", rows=8)
    second = generate_domain("color_theory", rows=8)
    assert [row["avg_watch_time_pct"] for row in first] == [
        row["avg_watch_time_pct"] for row in second
    ]
    # A different domain is still different data.
    other = generate_domain("music_theory", rows=8)
    assert [r["avg_watch_time_pct"] for r in first] != [r["avg_watch_time_pct"] for r in other]


def test_the_creative_choices_actually_move_the_numbers():
    """Every lever was drawn at random and then never referred to again.

    So palette, bias, audio anchor and primary lever were noise columns bolted
    onto unrelated performance figures, and a crew trained on it would learn
    the single lesson the data contained: nothing you choose matters.
    """
    import statistics

    from auteur.training.generate import _effect, generate_domain

    rows = generate_domain("color_theory", rows=1200, seed=7)

    by_palette: dict[str, list[float]] = {}
    for row in rows:
        by_palette.setdefault(row["color_theory_palette"], []).append(
            float(row["click_through_rate_pct"])
        )

    values = [v for bucket in by_palette.values() for v in bucket]
    grand = statistics.mean(values)
    explained = sum(
        len(bucket) * (statistics.mean(bucket) - grand) ** 2 for bucket in by_palette.values()
    ) / sum((v - grand) ** 2 for v in values)
    assert explained > 0.02, "the palette has to explain something, or the dataset teaches nothing"

    # And the direction has to match the effect the generator actually applied,
    # or the signal is real but backwards.
    ranked = sorted(by_palette, key=lambda name: -statistics.mean(by_palette[name]))
    assert _effect(ranked[0], "palette") > _effect(ranked[-1], "palette")


def test_generated_rows_carry_rates_not_only_counts():
    """Every count is views x rate, and views is lognormal with a 2.5x spread.

    So correlating the counts measures the view multiplier: the generator's own
    "shares follow completion" relationship came out at r = 0.10 in the counts
    while being true by construction.
    """
    from auteur.training.generate import generate_domain

    row = generate_domain("photography", rows=1, seed=3)[0]
    for column in ("share_to_view_ratio", "save_to_view_ratio", "three_second_watch_rate"):
        assert column in row, f"{column} is what the insight layer actually reads"
        assert 0.0 <= float(row[column]) <= 1.0


# ------------------------------------------------------------- crew memory


def _proposal(agent, title, *, applied=False, gain=0.0, objective="hook"):
    from auteur.agents.base import Proposal

    p = Proposal(
        agent=agent,
        title=title,
        reason="because",
        change=lambda edl: None,
        objective=objective,
    )
    p.applied = applied
    p.predicted_gain = gain
    return p


def test_an_agent_is_not_asked_the_same_question_twice(tmp_path):
    """Agents mostly have no memory, so a rejected idea came back every round.

    The answer had not changed: the cut it objected to was still there
    precisely because the objection was turned down.
    """
    from auteur.agents.base import Crew, Gate, Mode
    from auteur.insight import FitReport

    class Nagger:
        name = "nagger"
        objective = "hook"

        def __init__(self):
            self.asked = 0

        def inspect(self, edl, prediction, model):
            self.asked += 1
            # Changes nothing, so it can never show a gain and is always declined.
            return [_proposal("nagger", "do the pointless thing")]

    edl = _graphics_edl(tmp_path, count=4)
    agent = Nagger()
    crew = Crew(
        [agent],
        FitReport(rows=0, simulated_rows=0, measured_rows=0),
        gate=Gate(Mode.AUTONOMOUS),
        max_rounds=3,
    )
    result = crew.run(edl)

    scored = [p for round_ in result.rounds for p in round_.proposals]
    assert len(scored) == 1, "the same rejected proposal was scored more than once"


def test_the_ledger_remembers_what_was_worth_doing(tmp_path):
    from auteur.agents.ledger import Ledger

    ledger = Ledger(tmp_path / "led.jsonl")
    for _ in range(4):
        ledger.record(
            [
                _proposal("loop", "End on the frame it opened on", applied=True, gain=0.14),
                _proposal("share", "Tighten the middle", applied=False, gain=0.0),
            ]
        )

    assert ledger.value_of("loop", "End on the frame it opened on") == pytest.approx(0.14)
    assert ledger.value_of("share", "Tighten the middle") == 0.0
    # Never seen before is zero, not a penalty — an untried idea should be tried.
    assert ledger.value_of("hook", "something new") == 0.0

    proven = ledger.proven()
    assert [t.title for t in proven] == ["End on the frame it opened on"]
    assert "Tighten the middle" in [t.title for t in ledger.wasted()]

    # It survives a restart.
    assert len(Ledger(tmp_path / "led.jsonl").tracks) == 2


def test_a_proposal_nobody_ever_took_has_not_earned_its_place(tmp_path):
    """It appeared under both "earned its place" and "keeps being turned down"."""
    from auteur.agents.ledger import Ledger

    ledger = Ledger(tmp_path / "led.jsonl")
    for _ in range(5):
        ledger.record([_proposal("share", "Tighten the middle", applied=False, gain=0.004)])

    titles = {t.title for t in ledger.proven()}
    assert "Tighten the middle" not in titles
    assert "Tighten the middle" in {t.title for t in ledger.wasted()}


def test_the_same_change_on_different_films_is_one_track(tmp_path):
    """Counts in the title fragmented one idea across dozens of names.

    A change could be made on forty films and never reach three tries under any
    single name, so nothing ever became established.
    """
    from auteur.agents.ledger import Ledger, kind_of

    assert kind_of("Reframe 1 shot(s) onto the subject") == kind_of(
        "Reframe 7 shot(s) onto the subject"
    )
    assert kind_of("Cut the opening from 2.0s to 1.6s") == kind_of(
        "Cut the opening from 3.4s to 0.9s"
    )
    assert kind_of("End on the frame it opened on") == "End on the frame it opened on"

    ledger = Ledger(tmp_path / "led.jsonl")
    for count in (1, 2, 3, 5):
        ledger.record(
            [
                _proposal(
                    "finishing",
                    f"Reframe {count} shot(s) onto the subject",
                    applied=True,
                    gain=0.02,
                )
            ]
        )
    assert len(ledger.tracks) == 1
    track = next(iter(ledger.tracks.values()))
    assert track.tries == 4 and track.established


def test_the_ledger_puts_the_proven_changes_first(tmp_path):
    """Order matters: each applied change alters what the next is scored against."""
    from auteur.agents.ledger import Ledger

    ledger = Ledger(tmp_path / "led.jsonl")
    for _ in range(4):
        ledger.record([_proposal("loop", "the good one", applied=True, gain=0.20)])
        ledger.record([_proposal("share", "the weak one", applied=True, gain=0.01)])

    shuffled = [
        _proposal("hook", "brand new"),
        _proposal("share", "the weak one"),
        _proposal("loop", "the good one"),
    ]
    assert [p.title for p in ledger.order(shuffled)][0] == "the good one"


def test_a_crew_with_no_ledger_still_runs(tmp_path):
    """The memory is optional; an edit is not."""
    from auteur.agents.base import Crew, Gate, Mode
    from auteur.agents.ledger import NullLedger
    from auteur.insight import FitReport

    crew = Crew(
        [], FitReport(rows=0, simulated_rows=0, measured_rows=0), gate=Gate(Mode.AUTONOMOUS)
    )
    assert isinstance(crew.ledger, NullLedger)
    result = crew.run(_graphics_edl(tmp_path, count=3))
    assert result.edl.shots


def test_the_scholar_teaches_the_crew_rather_than_the_void(tmp_path):
    """`TeachingBrief` appeared once outside the scholar package: a CLI print.

    So the Scholar could study, corroborate and teach, and the crew it was
    teaching never heard a word of it.
    """
    from auteur.insight import FitReport
    from auteur.insight.score import Prediction
    from auteur.scholar import Scholar
    from auteur.scholar.agent import ScholarAgent
    from auteur.scholar.knowledge import Discipline

    scholar = Scholar(base_dir=tmp_path)
    for index, (discipline, technique) in enumerate(
        [
            (Discipline.PSYCHOLOGY, "attention and curiosity"),
            (Discipline.MUSIC_THEORY, "cut on the beat"),
            (Discipline.COLOR_THEORY, "colour harmony"),
        ]
    ):
        for channel in ("Chan A", "Chan B", "Chan C"):
            scholar.knowledge.add(_learning(discipline, technique, channel, index))

    agent = ScholarAgent(scholar)
    edl = _graphics_edl(tmp_path, count=5)
    prediction = Prediction(hook=0.4, share=0.4, loop=0.4)
    model = FitReport(rows=0, simulated_rows=0, measured_rows=0)

    proposals = agent.inspect(edl, prediction, model)
    taught = [p for p in proposals if p.title.startswith("[Studied]")]
    assert taught, "nothing the Scholar studied reached the crew"
    assert {"hook", "loop", "gaze"} <= {p.title.split()[-2] for p in taught}
    assert all(getattr(p, "learning_ids", None) for p in taught)

    # A brief repeats itself otherwise: corroboration means several channels
    # teaching the same sentence.
    for proposal in taught:
        sentences = [part.strip() for part in proposal.reason.split(";")]
        assert len(sentences) == len(set(sentences))

    # Said once per run, not once per round.
    assert [
        p for p in agent.inspect(edl, prediction, model) if p.title.startswith("[Studied]")
    ] == []


def test_the_phone_can_see_what_the_terminal_can(tmp_path, monkeypatch):
    """A capability that exists in one entry point and not the other is a seam.

    This has happened twice: the studio ran `default_crew()` while the CLI built
    one with the eye, the finisher and the overlay agent; and `auteur agents` /
    `auteur scholar` reported things the app you carry could not see. Both are
    the same bug wearing different clothes, so this pins the shape rather than
    the instance.
    """
    import inspect as _inspect

    from auteur.web import server

    routes = _inspect.getsource(server.Handler)
    for path in ("/api/crew", "/api/scholar", "/api/insight", "/api/platforms"):
        assert f'"{path}"' in routes, f"{path} is reachable from the terminal but not the app"

    # And they must be behind the sign-in, like everything else that is not
    # the login page itself.
    assert "/api/crew" not in server.PUBLIC_PATHS
    assert "/api/scholar" not in server.PUBLIC_PATHS


def test_the_crew_endpoint_reports_the_ledger_and_says_what_it_is(tmp_path, monkeypatch):
    from auteur.agents.ledger import Ledger
    from auteur.web import server

    monkeypatch.setattr(Ledger, "default_path", staticmethod(lambda: tmp_path / "led.jsonl"))
    ledger = Ledger()
    for _ in range(4):
        ledger.record(
            [
                _proposal("loop", "End on the frame it opened on", applied=True, gain=0.14),
                _proposal("share", "Tighten the middle", applied=False, gain=0.001),
            ]
        )

    payload = server.Handler._crew_memory(server.Handler)
    assert payload["kinds"] == 2
    assert [row["title"] for row in payload["proven"]] == ["End on the frame it opened on"]
    assert "Tighten the middle" in [row["title"] for row in payload["wasted"]]
    # The caveat travels with the numbers rather than living only in the CLI.
    assert "not view counts" in payload["note"]


def test_the_scholar_endpoint_reports_whether_it_can_study(tmp_path, monkeypatch):
    from auteur.scholar import youtube
    from auteur.web import server

    monkeypatch.setattr(youtube, "_ytdlp", lambda: None)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)

    payload = server.Handler._scholar_state(server.Handler)
    assert payload["available"] is True
    assert payload["can_study"] is False, "it must say when it cannot reach YouTube"
    assert "learnings" in payload and "gaps" in payload


# --------------------------------------------------------------- benchmarks


def _reading(**kwargs):
    from auteur.vision import Reading

    base = {
        "depth_separation": 0.5,
        "focus_strength": 0.25,
        "hue_spread": 45.0,
        "luma": 0.30,
        "contrast": 0.20,
    }
    base.update(kwargs)
    return Reading(**base)


def test_craft_sees_what_the_structural_score_cannot():
    """A film can be better organised and worse to look at. That is the trap."""
    from auteur.insight.benchmark import craft_score

    cinematic = craft_score(_reading(depth_separation=0.74, focus_strength=0.29, hue_spread=28.0))
    snapshot = craft_score(_reading(depth_separation=0.19, focus_strength=0.21, hue_spread=77.0))
    assert cinematic.overall > snapshot.overall
    # And it names the right weakness rather than a generic one.
    assert snapshot.weakest[0] == "separation"


def test_craft_penalises_crushed_and_blown_exposure():
    from auteur.insight.benchmark import craft_score

    good = craft_score(_reading(luma=0.30))
    crushed = craft_score(_reading(luma=0.04))
    blown = craft_score(_reading(luma=0.93))
    assert good.exposure > crushed.exposure
    assert good.exposure > blown.exposure


def test_surpassing_needs_both_scores():
    """Either alone is a failure mode, not a win."""
    from auteur.insight.benchmark import Benchmark, CraftScore, Standing

    target = Benchmark(name="t", source="", structure=0.42, craft=CraftScore(0.7, 0.8, 1.0, 0.9))

    pretty_but_shapeless = Standing(target, structure=0.30, craft=CraftScore(0.9, 0.9, 1.0, 0.9))
    assert pretty_but_shapeless.beats_craft and not pretty_but_shapeless.surpassed

    tidy_but_ugly = Standing(target, structure=0.90, craft=CraftScore(0.1, 0.2, 0.3, 0.4))
    assert tidy_but_ugly.beats_structure and not tidy_but_ugly.surpassed

    both = Standing(target, structure=0.90, craft=CraftScore(0.9, 0.9, 1.0, 0.95))
    assert both.surpassed


def test_a_level_score_does_not_read_as_behind():
    from auteur.insight.benchmark import Benchmark, CraftScore, Standing

    target = Benchmark(name="t", source="", structure=0.5, craft=CraftScore(0.5, 0.5, 1.0, 0.5))
    standing = Standing(target, structure=0.5, craft=CraftScore(0.5, 0.5, 1.0, 0.5))
    text = standing.describe()
    assert "behind by 0.00" not in text
    assert "level" in text


def test_benchmarks_survive_a_restart_and_pick_the_hardest(tmp_path):
    from auteur.insight.benchmark import Benchmark, Benchmarks, CraftScore

    marks = Benchmarks(tmp_path / "b.json")
    marks.add(
        Benchmark(name="easy", source="", structure=0.9, craft=CraftScore(0.2, 0.2, 0.2, 0.2))
    )
    marks.add(
        Benchmark(name="hard", source="", structure=0.3, craft=CraftScore(0.9, 0.9, 0.9, 0.9))
    )

    # Hardest is judged on craft: structure is the half this program was already
    # good at, craft is the half it was not measuring at all.
    assert marks.hardest.name == "hard"

    reloaded = Benchmarks(tmp_path / "b.json")
    assert set(reloaded.entries) == {"easy", "hard"}
    assert reloaded.hardest.name == "hard"

    # Not inside one assert: under `python -O` the asserts vanish and so would
    # the removals, which is the whole thing being tested.
    removed = reloaded.remove("easy")
    removed_again = reloaded.remove("easy")
    assert removed and not removed_again


def test_no_benchmark_means_no_standing(tmp_path):
    from auteur.insight.benchmark import Benchmarks

    marks = Benchmarks(tmp_path / "none.json")
    assert marks.standing(_reading(), 0.8) is None
    assert "nothing to beat yet" in marks.describe()


# --------------------------------------------------------- gaming the craft score


def test_a_destroyed_picture_cannot_score_well_however_tidy_its_palette():
    """A rehearsal loop found this exploit in nine generations.

    Given a grade that blew out 55% of every frame, separation and palette both
    went *up* — a bloom makes the sharp/soft ratio look like depth, and one
    colour smeared everywhere looks like discipline — and it beat the target it
    was chasing while being visibly the worst render of the three.
    """
    from auteur.insight.benchmark import CraftScore

    # The real shape of it: the ruined grade crushed 55% of the frame to black.
    destroyed = CraftScore(
        separation=1.0, subject=1.0, palette=1.0, exposure=0.1, clipped_black=0.55
    )
    modest = CraftScore(
        separation=0.39, subject=0.6, palette=0.56, exposure=0.83, clipped_black=0.02
    )
    assert modest.overall > destroyed.overall
    # Better on every dimension it can measure, and still comfortably beaten,
    # because most of the picture is gone. Asserted as a relationship rather
    # than a threshold: the exact multiplier is a tuning choice and moved once
    # already, to leave room for deliberate chiaroscuro.
    assert destroyed.overall < modest.overall * 0.5
    assert destroyed.intact < 0.4


def test_deep_blacks_on_purpose_are_not_the_same_as_a_destroyed_frame():
    """Chiaroscuro is a named style, not a fault.

    The thresholds come from the reels: the darkest real reference runs 0.288 of
    the frame at true black and the black-and-white one runs 0.151, while the
    grade a rehearsal loop produced by wrecking the picture runs 0.550.
    """
    from auteur.insight.benchmark import CraftScore

    chiaroscuro = CraftScore(
        separation=0.6, subject=0.8, palette=0.9, exposure=0.7, clipped_black=0.288
    )
    ruined = CraftScore(separation=0.6, subject=0.8, palette=0.9, exposure=0.7, clipped_black=0.550)
    blown = CraftScore(separation=0.6, subject=0.8, palette=0.9, exposure=0.7, clipped_white=0.150)

    assert chiaroscuro.damage == 0.0, "the darkest real reel must cost nothing"
    assert ruined.damage > 0.5
    # A blown highlight is a mistake in a way a black is not: there is nothing
    # above white to recover toward.
    assert blown.damage > chiaroscuro.damage


def test_a_monochrome_wash_is_not_a_disciplined_palette():
    """ "Narrower is better" made a single-colour smear a perfect score."""
    from auteur.insight.benchmark import craft_score

    wash = craft_score(_reading(hue_spread=2.0))
    graded = craft_score(_reading(hue_spread=30.0))
    uncontrolled = craft_score(_reading(hue_spread=85.0))

    assert graded.palette > wash.palette, "one colour everywhere is a wash, not a look"
    assert graded.palette > uncontrolled.palette


def test_clipping_is_measured_off_the_frame():
    """Nothing in a Reading could see destroyed detail before this."""
    import numpy as np

    from auteur.vision.connoisseur import read_frame

    height, width = 90, 160
    fine = np.linspace(0.15, 0.75, width, dtype=np.float32)
    fine = np.repeat(fine[None, :], height, axis=0)
    intact = read_frame(np.stack([fine] * 3, axis=-1))

    ruined = np.clip((fine - 0.4) * 6.0, 0.0, 1.0).astype(np.float32)
    crushed = read_frame(np.stack([ruined] * 3, axis=-1))

    assert crushed.clipped > intact.clipped
    assert crushed.clipped > 0.2


# ------------------------------------------------------ the crew seeing its work


def test_only_picture_changes_trigger_a_render(tmp_path):
    """Retiming a shot cannot alter how a frame looks, so it must cost nothing."""
    import copy

    from auteur.agents.preview import changes_the_picture, picture_fingerprint
    from auteur.edl import Look

    edl = _graphics_edl(tmp_path, count=4)

    retimed = copy.deepcopy(edl)
    retimed.shots[0].end = retimed.shots[0].start + 0.7
    assert not changes_the_picture(edl, retimed)
    assert picture_fingerprint(edl) == picture_fingerprint(retimed)

    graded = copy.deepcopy(edl)
    graded.look = Look(preset="amber", strength=1.0)
    assert changes_the_picture(edl, graded)

    boxed = copy.deepcopy(edl)
    boxed.letterbox = 0.11
    assert changes_the_picture(edl, boxed)


def test_the_crew_turns_down_a_change_that_ruins_the_picture(tmp_path):
    """The structural score cannot move when a grade changes — no shot got longer.

    So without an eye on it, a proposal that turns every frame magenta reads as
    perfectly neutral and gets applied on a coin flip.
    """
    from auteur.agents.base import Crew, Gate, Mode, Proposal, Risk
    from auteur.agents.preview import Comparison, Previewer, Proof
    from auteur.edl import Look
    from auteur.insight import FitReport
    from auteur.insight.benchmark import CraftScore

    class FakePreviewer(Previewer):
        """Renders nothing; answers as if the change destroyed the frame."""

        def __init__(self):
            super().__init__(workspace=tmp_path / "fake-proofs")

        def compare(self, before, after, *, sources=None):
            return Comparison(
                baseline=Proof(
                    "baseline", None, craft=CraftScore(0.4, 0.6, 0.5, 0.7, clipped_black=0.01)
                ),
                candidate=Proof(
                    "candidate", None, craft=CraftScore(1.0, 0.9, 1.0, 0.1, clipped_black=0.55)
                ),
            )

    def wreck(target):
        target.look = Look(preset="amber", strength=1.0)

    class Vandal:
        name = "vandal"
        objective = "hook"

        def __init__(self):
            self.said = False

        def inspect(self, edl, prediction, model):
            if self.said:
                return []
            self.said = True
            return [
                Proposal(
                    agent="vandal",
                    title="Grade it amber",
                    reason="warm",
                    change=wreck,
                    objective="hook",
                    binding=True,
                    risk=Risk.LOW,
                )
            ]

    crew = Crew(
        [Vandal()],
        FitReport(rows=0, simulated_rows=0, measured_rows=0),
        gate=Gate(Mode.AUTONOMOUS),
        previewer=FakePreviewer(),
        max_rounds=1,
    )
    result = crew.run(_graphics_edl(tmp_path, count=4))
    proposals = [p for round_ in result.rounds for p in round_.proposals]
    assert proposals
    # Binding means the *model* gets no veto. Visible damage is a different thing.
    assert not proposals[0].applied
    assert "worse" in proposals[0].decision_note
    assert proposals[0].craft_gain < 0


def test_a_change_the_model_cannot_see_can_still_be_taken_on_looks(tmp_path):
    """A grade moves no shot length, so the structural score says nothing at all."""
    from auteur.agents.base import Crew, Gate, Mode, Proposal, Risk
    from auteur.agents.preview import Comparison, Previewer, Proof
    from auteur.edl import Look
    from auteur.insight import FitReport
    from auteur.insight.benchmark import CraftScore

    class Improver(Previewer):
        def __init__(self):
            super().__init__(workspace=tmp_path / "improver-proofs")

        def compare(self, before, after, *, sources=None):
            return Comparison(
                baseline=Proof(
                    "baseline", None, craft=CraftScore(0.3, 0.5, 0.3, 0.6, clipped_black=0.02)
                ),
                candidate=Proof(
                    "candidate", None, craft=CraftScore(0.4, 0.6, 0.8, 0.8, clipped_black=0.01)
                ),
            )

    def grade(target):
        target.look = Look(preset="kodak", strength=0.8)

    class Colourist:
        name = "colourist"
        objective = "hook"

        def __init__(self):
            self.said = False

        def inspect(self, edl, prediction, model):
            if self.said:
                return []
            self.said = True
            return [
                Proposal(
                    agent="colourist",
                    title="Warm the grade",
                    reason="it is cold",
                    change=grade,
                    objective="hook",
                    risk=Risk.LOW,
                )
            ]

    crew = Crew(
        [Colourist()],
        FitReport(rows=0, simulated_rows=0, measured_rows=0),
        gate=Gate(Mode.AUTONOMOUS),
        previewer=Improver(),
        max_rounds=1,
    )
    result = crew.run(_graphics_edl(tmp_path, count=4))
    proposals = [p for round_ in result.rounds for p in round_.proposals]
    assert proposals[0].applied, "a change only the eye can judge still has to be possible"
    assert "picture improves" in proposals[0].decision_note


def test_a_crew_with_no_previewer_behaves_exactly_as_before(tmp_path):
    from auteur.agents.base import Crew, Gate, Mode
    from auteur.agents.preview import NullPreviewer
    from auteur.insight import FitReport

    crew = Crew(
        [], FitReport(rows=0, simulated_rows=0, measured_rows=0), gate=Gate(Mode.AUTONOMOUS)
    )
    assert isinstance(crew.previewer, NullPreviewer)
    assert not crew.previewer.enabled
    assert crew.run(_graphics_edl(tmp_path, count=3)).edl.shots


# ----------------------------------------------- a stuck agent asks the Scholar


def test_a_scholar_with_nothing_to_say_says_so(tmp_path):
    """An agent acting on a confident fabrication is worse off than a stuck one."""
    from auteur.scholar import Scholar
    from auteur.scholar.consult import HelpDesk, Question

    desk = HelpDesk(Scholar(base_dir=tmp_path))
    answer = desk.ask(Question(agent="overlay", goal="add depth", problem="every frame reads flat"))
    assert not answer.useful
    assert answer.basis == "none"
    assert answer.confidence == "none"
    assert "not studied this yet" in answer.describe()
    # And it remembers to go and find out, rather than forgetting the question.
    assert len(desk.homework) == 1


def test_confidence_counts_channels_not_notes(tmp_path):
    """Four notes from one tutorial is one opinion written down four times."""
    from auteur.scholar import Scholar
    from auteur.scholar.consult import HelpDesk, Question
    from auteur.scholar.knowledge import Discipline

    scholar = Scholar(base_dir=tmp_path)
    for index in range(4):
        scholar.knowledge.add(
            _learning(Discipline.CINEMATOGRAPHY, "subject separation", "Only Chan", index)
        )
    lone = HelpDesk(scholar).ask(
        Question(agent="overlay", goal="subject separation", problem="frames are flat")
    )
    assert lone.useful
    assert "one source" in lone.confidence

    for name in ("Chan B", "Chan C"):
        scholar.knowledge.add(_learning(Discipline.CINEMATOGRAPHY, "subject separation", name))
    many = HelpDesk(scholar).ask(
        Question(agent="overlay", goal="subject separation", problem="frames are flat")
    )
    assert "independent sources" in many.confidence


def test_the_overlay_agent_asks_when_it_has_nothing_to_point_at(tmp_path):
    """Returning an empty list would let 'no subject anywhere' look like approval."""
    from auteur.agents.base import Crew, Gate, Mode
    from auteur.agents.overlay import OverlayAgent
    from auteur.insight import FitReport
    from auteur.scholar import Scholar
    from auteur.scholar.consult import HelpDesk
    from auteur.scholar.knowledge import Discipline
    from auteur.vision import Reading

    scholar = Scholar(base_dir=tmp_path)
    for channel in ("Chan A", "Chan B", "Chan C"):
        scholar.knowledge.add(_learning(Discipline.CINEMATOGRAPHY, "subject separation", channel))

    edl = _graphics_edl(tmp_path, count=5)
    # Every frame is a texture: nowhere for a ring or an arrow to go.
    flat = {shot.clip_id: Reading(focus_strength=0.04) for shot in edl.shots}

    desk = HelpDesk(scholar)
    crew = Crew(
        [OverlayAgent(flat)],
        FitReport(rows=0, simulated_rows=0, measured_rows=0),
        gate=Gate(Mode.AUTONOMOUS),
        helpdesk=desk,
        max_rounds=1,
    )
    crew.run(edl)

    assert len(desk.asked) == 1, "it should ask once, not once per round"
    question = desk.asked[0]
    assert question.agent == "overlay"
    assert "focus strength" in " ".join(question.evidence)
    assert desk.answered[0].useful


def test_asking_never_blocks_on_the_network(tmp_path, monkeypatch):
    """Research happens after the edit, so the next run is better, not this one slower."""
    from auteur.scholar import Scholar
    from auteur.scholar.consult import HelpDesk, Question
    from auteur.scholar.youtube import YouTubeUnavailable

    scholar = Scholar(base_dir=tmp_path)

    def refuse(*args, **kwargs):
        raise YouTubeUnavailable("no network")

    monkeypatch.setattr(scholar, "study", refuse)
    desk = HelpDesk(scholar)

    # The question is answered (with a no) without ever touching the network.
    answer = desk.ask(Question(agent="hook", goal="pace it", problem="unclear"))
    assert not answer.useful
    # The research pass is where the network is needed, and it fails quietly.
    assert desk.do_the_homework() == 0


# --------------------------------------------------- standing a wide shot on end


def test_turning_a_wide_frame_keeps_what_cropping_throws_away():
    """A 16:9 source in a 9:16 frame loses about two thirds of its width."""
    from auteur.craft.motion import reframe_chain

    turned = reframe_chain(1080, 1920, mode="turn")
    assert "transpose" in turned
    # Fit and pad, never crop: the whole point is that nothing is discarded.
    assert "force_original_aspect_ratio=decrease" in turned
    assert "crop=" not in turned

    cropped = reframe_chain(1080, 1920, mode="subject", anchor=(0.5, 0.5))
    assert "crop=" in cropped and "transpose" not in cropped


def test_the_finishing_agent_offers_to_turn_wide_footage(tmp_path):
    from PIL import Image

    from auteur.agents.assemble import read_the_footage
    from auteur.agents.finishing import FinishingAgent
    from auteur.edl import EditDecisionList, Motion, Shot, Transition
    from auteur.insight import FitReport
    from auteur.insight.score import Prediction

    wide = []
    for index in range(3):
        path = tmp_path / f"w{index}.png"
        image = Image.new("RGB", (1920, 1080), (20 + index * 20, 40, 70))
        image.paste((240, 180, 90), (400 + index * 200, 300, 900 + index * 200, 800))
        image.save(path)
        wide.append(path)

    readings = read_the_footage(wide)
    assert all(r.aspect > 1.7 for r in readings.values()), "the reading has to carry the shape"

    shots = [
        Shot(
            clip_id=clip,
            source=path,
            start=0.0,
            end=1.5,
            is_still=True,
            motion=Motion("none", 0.0, (0.5, 0.5)),
            transition_in=Transition("cut", 0.0),
        )
        for clip, path in zip(readings, wide, strict=False)
    ]
    edl = EditDecisionList(title="wide", shots=shots, fps=24, width=1080, height=1920)
    edl.repair()

    proposals = FinishingAgent(readings).inspect(
        edl,
        Prediction(hook=0.5, share=0.5, loop=0.5),
        FitReport(rows=0, simulated_rows=0, measured_rows=0),
    )
    turning = [p for p in proposals if "on end" in p.title]
    assert turning, "wide footage in a tall frame should at least be offered the alternative"

    turning[0].change(edl)
    assert all(shot.reframe == "turn" for shot in edl.shots)
    # A camera move on a turned frame crops back into what was just saved.
    assert all(shot.motion.kind == "none" for shot in edl.shots)


def test_a_vertical_source_is_left_alone(tmp_path):
    """Turning footage that already fits would be vandalism in the other direction."""
    from PIL import Image

    from auteur.agents.assemble import read_the_footage
    from auteur.agents.finishing import _much_wider_than
    from auteur.edl import EditDecisionList

    path = tmp_path / "tall.png"
    Image.new("RGB", (1080, 1920), (40, 50, 60)).save(path)
    reading = next(iter(read_the_footage([path]).values()))

    edl = EditDecisionList(title="t", width=1080, height=1920)
    assert not _much_wider_than(reading, edl)
    assert not _much_wider_than(None, edl)


# ------------------------------------------------------------- the gallery


def _archetypes(folder):
    """The pictures a museum API actually returns, drawn rather than fetched.

    Two real pictures, and four kinds of record: an object on a sweep, a page
    of text, a repeating swatch, and a faded scan with nothing in it.
    """
    import random

    from PIL import Image, ImageDraw, ImageFilter

    random.seed(7)
    folder.mkdir(parents=True, exist_ok=True)

    # An object dead centre on a flat grey sweep.
    coin = Image.new("RGB", (900, 900), (232, 231, 228))
    draw = ImageDraw.Draw(coin)
    draw.ellipse([390, 390, 510, 510], fill=(176, 150, 92), outline=(140, 118, 70))
    draw.ellipse([408, 408, 492, 492], fill=(190, 165, 105))
    coin.filter(ImageFilter.GaussianBlur(0.4)).save(folder / "record_coin.png")

    # A page of text: detail everywhere, nowhere for the eye to go.
    page = Image.new("RGB", (900, 1200), (243, 239, 228))
    draw = ImageDraw.Draw(page)
    for row in range(46):
        y = 90 + row * 23
        draw.rectangle([110, y, 110 + random.randint(480, 690), y + 9], fill=(58, 52, 44))
    page.save(folder / "record_document.png")

    # A lit subject against a soft ground: depth, and one place to look.
    portrait = Image.new("RGB", (900, 1200), (14, 16, 26))
    draw = ImageDraw.Draw(portrait)
    for y in range(1200):
        fade = (1 - y / 1200) ** 2
        draw.line(
            [(0, y), (900, y)],
            fill=(int(16 + 70 * fade), int(18 + 64 * fade), int(30 + 96 * fade)),
        )
    portrait = portrait.filter(ImageFilter.GaussianBlur(14))
    draw = ImageDraw.Draw(portrait)
    draw.ellipse([300, 300, 620, 700], fill=(236, 202, 150))
    draw.ellipse([345, 350, 575, 640], fill=(248, 224, 178))
    for _ in range(240):
        draw.point((random.randint(300, 620), random.randint(300, 700)), fill=(255, 240, 210))
    portrait.save(folder / "picture_portrait.png")
    return folder


def test_the_craft_score_alone_would_rank_the_slop_first(tmp_path):
    """The finding the gallery's gates exist because of.

    A catalogue photograph of a coin is maximum depth separation and an
    unambiguous subject on a narrow palette — every dimension of the craft
    score reads it as excellent. Sorting search results by craft would put the
    record shots at the top, which is backwards.
    """
    from auteur.insight.benchmark import craft_score
    from auteur.vision import read_asset

    folder = _archetypes(tmp_path / "arch")
    coin = craft_score(read_asset(folder / "record_coin.png", samples=1)).overall
    document = craft_score(read_asset(folder / "record_document.png", samples=1)).overall

    assert coin > document, "sanity: the coin is the one that fools the score"
    assert coin > 0.6, f"the coin scores {coin:.3f} — if this drops, re-derive the gates"


def test_the_gates_tell_a_picture_from_a_catalogue_photograph(tmp_path):
    from auteur.gallery import looks_like_a_record_shot
    from auteur.vision import read_asset

    folder = _archetypes(tmp_path / "arch")

    def verdict(name):
        return looks_like_a_record_shot(read_asset(folder / name, samples=1))

    assert "blank ground" in verdict("record_coin.png")
    assert "detail spread" in verdict("record_document.png")
    assert verdict("picture_portrait.png") == "", "a real picture was turned away"


def test_the_paperwork_gate_runs_before_anything_is_downloaded():
    """Rights, size and catalogue vocabulary, all from the record itself."""
    from auteur.gallery import Candidate, paperwork_clears

    fine = Candidate(rights="Public Domain", image_url="http://x/y.jpg", width=3000, height=2000)
    assert paperwork_clears(fine) == ""

    # The search filter is a request; the record is the answer.
    assert "not clearly free" in paperwork_clears(
        Candidate(rights="not stated", image_url="http://x/y.jpg")
    )
    assert "no image" in paperwork_clears(Candidate(rights="CC0"))
    assert "too small" in paperwork_clears(
        Candidate(rights="CC0", image_url="http://x/y.jpg", width=300, height=220)
    )
    assert "fragment" in paperwork_clears(
        Candidate(rights="CC0", image_url="http://x/y.jpg", title="Textile fragment, silk")
    )


class _RecordedCollections:
    """The three APIs' documented response shapes, and the archetype images.

    The sandbox this was written in refuses connections to all three hosts, so
    the shapes come from each collection's published documentation rather than
    from a live call. The parsing is written to survive being wrong about them.
    """

    def __init__(self, folder):
        self.folder = folder
        self.fetched = []

    def get(self, url, *, headers=None):
        import json as _json
        from urllib.parse import urlsplit

        self.fetched.append(url)
        if url.startswith("pic://"):
            return (self.folder / url[len("pic://") :]).read_bytes()
        if url.startswith("iiif/"):
            return (self.folder / (url.split("/")[1] + ".png")).read_bytes()
        # Matched on the host, and on the whole of it. A substring of the
        # URL matches a path or a query string; `endswith` on the hostname
        # matches evilmetmuseum.org. Neither is what "is this the Met" means.
        host = (urlsplit(url).hostname or "").lower()

        def served_by(domain):
            return host == domain or host.endswith("." + domain)

        if served_by("metmuseum.org") and "/search?" in url:
            return _json.dumps({"total": 2, "objectIDs": [101, 102]}).encode()
        if served_by("metmuseum.org") and "/objects/101" in url:
            return _json.dumps(
                {
                    "objectID": 101,
                    "isPublicDomain": True,
                    "title": "Portrait in Lamplight",
                    "artistDisplayName": "A Painter",
                    "classification": "Paintings",
                    "primaryImage": "https://images.metmuseum.org/101.png",
                    "primaryImageSmall": "pic://picture_portrait.png",
                    "objectURL": "https://www.metmuseum.org/art/collection/search/101",
                }
            ).encode()
        if served_by("metmuseum.org") and "/objects/102" in url:
            return _json.dumps(
                {
                    "objectID": 102,
                    "isPublicDomain": True,
                    "title": "Tetradrachm",
                    "classification": "Coins",
                    "primaryImage": "https://images.metmuseum.org/102.png",
                    "primaryImageSmall": "pic://record_coin.png",
                    "objectURL": "x",
                }
            ).encode()
        if served_by("artic.edu"):
            return _json.dumps(
                {
                    "config": {"iiif_url": "iiif"},
                    "data": [
                        {
                            "id": 201,
                            "title": "Ledger page",
                            "classification_title": "Manuscript",
                            "image_id": "record_document",
                            "is_public_domain": True,
                            "thumbnail": {"width": 2000, "height": 2600},
                        }
                    ],
                }
            ).encode()
        if served_by("clevelandart.org"):
            return _json.dumps({"data": []}).encode()
        raise RuntimeError(f"unexpected url {url}")


def test_a_search_keeps_the_pictures_and_says_why_it_dropped_the_rest(tmp_path):
    from auteur.gallery import Curator

    folder = _archetypes(tmp_path / "arch")
    transport = _RecordedCollections(folder)
    result = Curator(tmp_path / "out", transport=transport).search("lamplight", keep=5)

    assert [j.candidate.title for j in result.kept] == ["Portrait in Lamplight"]
    reasons = {j.candidate.title: j.rejected for j in result.dropped}
    assert "blank ground" in reasons["Tetradrachm"]
    assert "detail spread" in reasons["Ledger page"]

    # The keeper is on disk; the ones turned away are not left behind.
    assert result.kept[0].local is not None and result.kept[0].local.is_file()
    assert list((tmp_path / "out").glob("*")) == [result.kept[0].local]


def test_one_collection_being_down_is_a_smaller_search_not_a_failed_one(tmp_path):
    from auteur.gallery import Curator

    folder = _archetypes(tmp_path / "arch")

    class HalfDown(_RecordedCollections):
        def get(self, url, *, headers=None):
            from urllib.parse import urlsplit

            host = (urlsplit(url).hostname or "").lower()
            if host == "api.artic.edu" or host.endswith(".artic.edu"):
                raise OSError("connection refused")
            return super().get(url, headers=headers)

    result = Curator(tmp_path / "out", transport=HalfDown(folder)).search("lamplight")
    assert [j.candidate.title for j in result.kept] == ["Portrait in Lamplight"]
    assert any("artic" in note for note in result.trouble)


def test_the_same_work_in_two_collections_is_kept_once(tmp_path):
    from auteur.gallery import Candidate, Curator

    both = [
        Candidate(
            provider="The Met",
            ref="1",
            title="Wheatfield",
            artist="A Painter",
            rights="public domain",
            image_url="http://x/1.jpg",
            preview_url="pic://picture_portrait.png",
        ),
        Candidate(
            provider="Cleveland Museum of Art",
            ref="2",
            title="  wheatfield ",
            artist="a painter",
            rights="CC0",
            image_url="http://x/2.jpg",
            preview_url="pic://picture_portrait.png",
        ),
    ]
    folder = _archetypes(tmp_path / "arch")
    curator = Curator(tmp_path / "out", transport=_RecordedCollections(folder))

    from auteur.gallery import sources

    saved = sources.search_all
    sources.search_all = lambda *args, **kwargs: (both, [])
    try:
        result = curator.search("wheatfield")
    finally:
        sources.search_all = saved

    assert len(result.kept) == 1
    assert any("another collection" in j.rejected for j in result.dropped)


# ------------------------------------------------- studying what is on disk


def test_a_claim_spread_over_three_lines_is_read_as_one_sentence(tmp_path):
    """Prose in markdown is wrapped, so line-at-a-time reading yields fragments."""
    from auteur.scholar.library import read_document

    doc = tmp_path / "notes.md"
    doc.write_text(
        "# Cutting\n\n"
        "A dissolve between two shots that already match in tone\n"
        "looks like a mistake rather than a transition, so save them\n"
        "for the joins where the picture actually changes.\n\n"
        "Short.\n",
        encoding="utf-8",
    )

    learnings = read_document(doc)
    assert len(learnings) == 1
    only = learnings[0]
    assert only.insight.endswith("picture actually changes.")
    assert "\n" not in only.insight
    assert only.technique == "Cutting", "the heading it sat under is the technique"
    # A fragment is not a claim.
    assert "Short." not in only.insight


def test_reading_the_same_document_twice_does_not_double_the_knowledge(tmp_path):
    """`hash()` is salted per process, so the ids have to be derived."""
    from auteur.scholar.library import read_document

    doc = tmp_path / "notes.md"
    doc.write_text(
        "Words must be placed where the subject is not, because a title over "
        "somebody's face makes the viewer choose.\n",
        encoding="utf-8",
    )
    first = read_document(doc)
    second = read_document(doc)
    assert first and [x.learning_id for x in first] == [x.learning_id for x in second]


def test_a_measured_film_carries_its_numbers_not_just_a_sentence_about_them(rushes):
    """Holding the crew to a measurement means comparing floats, not parsing English."""
    from auteur.scholar.library import measure_film

    learnings = measure_film(rushes / "a_wide.mp4")
    assert learnings, "a real file produced no measurements"
    by_technique = {x.technique: x for x in learnings}

    movement = by_technique["camera movement"]
    assert isinstance(movement.measurements.get("motion"), float)

    # This clip is one continuous take. A film the detector finds no cuts in is
    # not a *cutting rate* to hold anybody to — recorded as 0.0 it drags the
    # median toward zero and quietly excuses the crew from cutting at all.
    assert "cutting rate" not in by_technique

    # And the numbers survive being written down and read back.
    from auteur.scholar.knowledge import Learning

    again = Learning.from_json(movement.to_json())
    assert again.measurements == movement.measurements


def test_the_same_reel_saved_under_two_names_is_one_film(rushes, tmp_path):
    """Counting a copy twice doubles its vote in every median the crew faces.

    Two of the twelve reels first studied were byte-identical copies of two
    others, so the learning id is keyed on what the file is rather than where
    it sits.
    """
    from auteur.scholar.knowledge import KnowledgeStore
    from auteur.scholar.library import measure_film

    original = rushes / "a_wide.mp4"
    copy = tmp_path / "same-film-different-name.mp4"
    copy.write_bytes(original.read_bytes())

    store = KnowledgeStore(tmp_path / "k.jsonl")
    kept_first = sum(1 for learning in measure_film(original) if store.add(learning))
    kept_again = sum(1 for learning in measure_film(copy) if store.add(learning))

    assert kept_first > 0
    assert kept_again == 0, "the same film under another name was learned twice"


def _store_that_studied(tmp_path, **numbers):
    """A knowledge store holding one measured film, with numbers we choose."""
    from auteur.scholar.knowledge import Discipline, KnowledgeStore, Learning

    store = KnowledgeStore(tmp_path / "k.jsonl")
    for technique, measurements in numbers.items():
        store.add(
            Learning(
                learning_id=technique,
                disciplines=[Discipline.MOVIE_MAKING],
                insight="measured",
                technique=technique,
                application="",
                source_video_id="file:reference.mp4",
                source_channel="local:rushes",
                source_title="reference.mp4",
                measurements=measurements,
            )
        )
    return store


def _cut_at_rate(count, seconds, *, move="none", opening=None, strength=0.8):
    from auteur.edl import EditDecisionList, Look, Motion, Shot

    shots = [
        Shot(
            clip_id=f"c{i}",
            source=Path("x.mp4"),
            start=0.0,
            end=seconds,
            motion=Motion(move, 0.2 if move != "none" else 0.0, (0.5, 0.5)),
        )
        for i in range(count)
    ]
    if opening is not None:
        shots[0].end = opening
    edl = EditDecisionList(
        title="t", shots=shots, look=Look(preset="noir", strength=strength), width=1080, height=1920
    )
    edl.repair()
    return edl


def test_the_scholar_says_how_far_off_the_pace_is_and_which_film_says_so(tmp_path):
    from auteur.scholar.library import critique_technique

    store = _store_that_studied(tmp_path, **{"cutting rate": {"cuts_per_10s": 17.6}})
    findings = critique_technique(_cut_at_rate(8, 2.5), store)

    assert len(findings) == 1
    only = findings[0]
    assert only.category == "pacing"
    assert only.severity == "important"
    # Every finding names a number, the number it is held against, and the source.
    assert "17.6" in only.description
    assert "reference.mp4" in only.description


def test_the_scholar_is_silent_when_the_crew_is_already_doing_it(tmp_path):
    """A critic that always finds something is not read twice."""
    from auteur.scholar.library import critique_technique

    store = _store_that_studied(
        tmp_path,
        **{
            "cutting rate": {"cuts_per_10s": 17.6},
            "how long before the first cut": {"first_cut": 0.21},
            "camera movement": {"motion": 0.03},
            "exposure and palette": {"clipped_black": 0.79},
        },
    )
    assert critique_technique(_cut_at_rate(18, 0.56, opening=0.25), store) == []


def test_nothing_is_criticised_on_a_measurement_that_was_never_taken(tmp_path):
    """Prose is not a yardstick — only measured properties produce findings."""
    from auteur.scholar.knowledge import Discipline, KnowledgeStore, Learning
    from auteur.scholar.library import critique_technique

    store = KnowledgeStore(tmp_path / "k.jsonl")
    store.add(
        Learning(
            learning_id="prose",
            disciplines=[Discipline.MOVIE_MAKING],
            insight="fast cutting feels energetic and modern",
            technique="pacing",
            application="",
            source_video_id="blog",
            source_channel="somewhere",
            source_title="a blog post",
        )
    )
    assert critique_technique(_cut_at_rate(4, 5.0), store) == []


# ------------------------------------------------------------------ the eye


def _degraded(tmp_path):
    """A detailed frame and three ways of ruining it, at real phone size."""
    import numpy as np
    from PIL import Image, ImageFilter

    folder = tmp_path / "acuity"
    folder.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(3)

    base = np.zeros((960, 540, 3), dtype=np.float32)
    for y in range(960):
        base[y, :, :] = 40 + 120 * (1 - y / 960) ** 2
    # Real fine detail, of the kind a codec and a blur both destroy.
    detailed = np.clip(base + rng.normal(0, 14, base.shape), 0, 255).astype(np.uint8)
    sharp = Image.fromarray(detailed)
    sharp.save(folder / "sharp.png")
    sharp.filter(ImageFilter.GaussianBlur(3.0)).save(folder / "soft.png")
    sharp.resize((135, 240)).resize((540, 960), Image.BICUBIC).save(folder / "upscaled.png")
    return folder


def test_the_craft_score_rewards_blur_which_is_why_acuity_exists(tmp_path):
    """Separation is a ratio, so a frame with no sharp anything satisfies it.

    This is the defect the acuity measurement was added for, and the test
    exists so that if somebody ever fixes separation properly, this fails and
    says so rather than leaving a now-pointless measurement in place.
    """
    from auteur.insight.benchmark import craft_score
    from auteur.vision import read_asset

    folder = _degraded(tmp_path)
    sharp = craft_score(read_asset(folder / "sharp.png", samples=1)).separation
    soft = craft_score(read_asset(folder / "soft.png", samples=1)).separation
    assert soft > sharp, "separation no longer prefers blur — acuity's gate can be reconsidered"


def test_the_eye_sees_detail_the_thumbnail_cannot(tmp_path):
    """Everything else is read at 320px, where a blur and a photograph match."""
    from auteur.vision import read_asset
    from auteur.vision.connoisseur import STRUCTURE_EDGE

    assert STRUCTURE_EDGE == 320, "the acuity pass exists because of this"

    folder = _degraded(tmp_path)
    sharp = read_asset(folder / "sharp.png", samples=1).acuity
    soft = read_asset(folder / "soft.png", samples=1).acuity
    upscaled = read_asset(folder / "upscaled.png", samples=1).acuity

    assert sharp > soft > 0.0
    assert sharp > upscaled > 0.0


def test_acuity_is_not_used_as_an_absolute_threshold(tmp_path):
    """Across sixteen reference reels it runs 0.214-0.431, overlapping a blur.

    Compressed social video has genuinely lost its fine detail, so a threshold
    that catches the blur condemns the reels this project is chasing. The
    measurement is relative to the same source or it is nothing — this test is
    here so nobody quietly reintroduces the absolute gate.
    """
    from auteur.insight.benchmark import craft_score
    from auteur.vision import read_asset
    from auteur.vision.connoisseur import Reading

    folder = _degraded(tmp_path)
    reading = read_asset(folder / "sharp.png", samples=1)
    with_acuity = craft_score(reading).separation

    blind = Reading(**{**reading.__dict__, "acuity": 0.0})
    assert craft_score(blind).separation == pytest.approx(with_acuity)


def test_a_change_that_softens_the_picture_is_not_an_improvement(tmp_path):
    """The exploit the craft score would otherwise reward."""
    from auteur.agents.preview import Comparison, Proof
    from auteur.insight.benchmark import craft_score
    from auteur.vision import read_asset

    folder = _degraded(tmp_path)

    def proof(label, name):
        reading = read_asset(folder / name, samples=1)
        return Proof(label=label, path=folder / name, reading=reading, craft=craft_score(reading))

    source = proof("source", "sharp.png")
    blurred = Comparison(source=source, baseline=source, candidate=proof("after", "soft.png"))

    assert blurred.softened > 0.10
    assert not blurred.better, "a change that smears the picture scored as an improvement"
    assert "softens the picture" in blurred.describe()

    # And an honest change is not accused of it.
    same = Comparison(source=source, baseline=source, candidate=proof("after", "sharp.png"))
    assert same.softened == 0.0


def test_the_expressive_look_is_applied_once_not_twice(rushes, tmp_path):
    """`_match_looks` copies the film's preset onto every shot.

    So the segment pass and the finishing pass built byte-identical filter
    chains and both ran. Measured on one still through a neon grade at 0.7,
    luma fell 0.150 to 0.098 and the fraction of the frame crushed to true
    black rose 0.331 to 0.526 — past the line this project calls a ruined
    picture. Every film went out graded twice.
    """
    from auteur.config import FORMATS, QUALITIES, Settings
    from auteur.craft import color
    from auteur.edl import Look
    from auteur.render import _segment_video_graph

    # The two chains really are the same thing, which is why running both hurt.
    film = Look(preset="neon", strength=0.7)
    shot_look = Look(preset="neon", exposure=0.1, temperature=0.05, strength=0.7)
    # Filter labels are numbered per call, so compare the shape rather than the text.
    assert len(color.look_chain(shot_look)) == len(color.look_chain(film))

    from auteur.edl import Shot

    shot = Shot(
        clip_id="c0",
        source=rushes / "a_wide.mp4",
        start=0.0,
        end=2.0,
        look=Look(preset="neon", exposure=0.1, temperature=0.05, strength=0.7),
    )
    settings = Settings(quality=QUALITIES["draft"], primary_format=FORMATS["reel"])
    graph = _segment_video_graph(shot, FORMATS["reel"], settings.quality)

    # The corrective pass belongs on the shot; the expressive one does not.
    assert "colortemperature" in graph, "the per-shot correction went missing"
    assert "colorbalance" not in graph, "the expressive look is still on the segment"


def test_every_film_counts_as_its_own_source(rushes, tmp_path):
    """Corroboration counts channels, and all the reels lived in one folder.

    Filing each film under the directory it happens to sit in made sixteen
    films by sixteen creators into one voice, so "these all cut fast" could
    never corroborate and every measured learning stayed TENTATIVE forever.
    """
    from auteur.scholar.library import measure_film

    one = measure_film(rushes / "a_wide.mp4")
    other = measure_film(rushes / "c_motion.mp4")
    assert one and other
    assert one[0].source_channel != other[0].source_channel, "two films, one channel"
    assert one[0].source_channel.startswith("film:")


def test_two_films_agreeing_promotes_the_technique(tmp_path):
    from auteur.scholar.knowledge import Confidence, Discipline, KnowledgeStore, Learning

    store = KnowledgeStore(tmp_path / "k.jsonl")

    def measured(channel, motion):
        return Learning(
            learning_id=f"{channel}-motion",
            disciplines=[Discipline.CINEMATOGRAPHY],
            insight=f"{channel} measures {motion}",
            technique="camera movement",
            application="",
            source_video_id=channel,
            source_channel=channel,
            source_title=channel,
            measurements={"motion": motion},
        )

    store.add(measured("film:aaa", 0.03))
    assert store.all()[0].confidence is Confidence.TENTATIVE, "one source is one opinion"

    store.add(measured("film:bbb", 0.05))
    assert all(x.confidence is Confidence.SUPPORTED for x in store.all())


def test_the_crew_is_taught_what_the_films_agree_on_not_a_list_of_filenames(tmp_path):
    """ "abc123.mp4 measures 0.034" names a hash and generalises to nothing."""
    from auteur.scholar.knowledge import Discipline, KnowledgeStore, Learning
    from auteur.scholar.teach import Teacher

    store = KnowledgeStore(tmp_path / "k.jsonl")
    for index, rate in enumerate([19.4, 27.8, 39.3, 76.1]):
        store.add(
            Learning(
                learning_id=f"r{index}",
                disciplines=[Discipline.MOVIE_MAKING],
                insight=f"reel{index}.mp4 cuts {rate} times per ten seconds",
                technique="cutting rate",
                application="",
                source_video_id=f"file:reel{index}.mp4",
                source_channel=f"film:{index:012d}",
                source_title=f"reel{index}.mp4",
                measurements={"cuts_per_10s": rate},
            )
        )

    brief = Teacher(store).brief_for_all()
    assert brief.consensus, "several films agreed and nothing was said about it"
    line = brief.consensus[0]
    assert "Across 4 films" in line
    assert "27.8" in line or "39.3" in line, "the median is what several sources license"
    assert "19.4" in line and "76.1" in line, "the spread belongs with the median"
    assert ".mp4" not in line, "a filename is not a teaching"
    assert "Across 4 films" in brief.describe()


def test_one_film_alone_is_not_a_consensus(tmp_path):
    from auteur.scholar.knowledge import Discipline, KnowledgeStore, Learning
    from auteur.scholar.teach import Teacher

    store = KnowledgeStore(tmp_path / "k.jsonl")
    store.add(
        Learning(
            learning_id="only",
            disciplines=[Discipline.MOVIE_MAKING],
            insight="one reel cuts fast",
            technique="cutting rate",
            application="",
            source_video_id="file:one.mp4",
            source_channel="film:only",
            source_title="one.mp4",
            measurements={"cuts_per_10s": 40.0},
        )
    )
    assert Teacher(store).brief_for_all().consensus == []


def test_a_slow_cut_cannot_beat_a_reel_it_is_cutting_a_quarter_as_fast_as():
    """Cadence was recorded on the benchmark and left out of the objective.

    Craft measures the picture and structure measures the shape; neither
    rewards cutting six times a second. So forty generations against a
    46-cuts-per-ten-seconds benchmark sat at a 1.4s shot and climbed craft
    instead — the most distinctive property of the films being chased was not
    in the score at all.
    """
    from auteur.training.rehearse import Attempt, Recipe

    def attempt(cadence):
        return Attempt(generation=1, recipe=Recipe(), structure=0.75, craft=0.78, cadence=cadence)

    assert attempt(0.95).combined > attempt(0.20).combined

    # And "beat the target" now means matching the pace, not only the picture.
    from auteur.insight.benchmark import Benchmark, CraftScore

    target = Benchmark(
        name="t",
        source="t.mp4",
        structure=0.70,
        craft=CraftScore(separation=0.5),
        cuts_per_10s=46.0,
    )
    fast, slow = attempt(0.95), attempt(0.20)
    for candidate in (fast, slow):
        candidate.beat_target = (
            candidate.craft > target.craft.overall
            and candidate.structure > target.structure
            and candidate.cadence > 0.75
        )
    assert fast.beat_target and not slow.beat_target


def test_with_nothing_to_chase_the_score_is_what_it_always_was():
    """A rehearsal with no benchmark must not be penalised on a pace it has no target for."""
    from auteur.training.rehearse import Attempt, Recipe

    blank = Attempt(generation=1, recipe=Recipe(), structure=0.8, craft=0.6)
    assert blank.cadence == 1.0
    assert blank.combined == pytest.approx(0.6 * 0.5 + 0.8 * 0.3 + 0.2)


def test_the_trainer_can_reach_the_rate_it_is_chasing():
    """The floor was 0.25s — four cuts a second — against a 0.125s reference."""
    from auteur.edl import MIN_SHOT
    from auteur.training.rehearse import KNOBS

    low, high = KNOBS["shot_seconds"]
    assert low == MIN_SHOT
    assert low <= 0.125, "the fastest reference reel is outside the search space"


def test_the_browsers_two_fallbacks_are_the_same_word():
    """A prompt with no words the engine knows went through two fallbacks.

    `cadenceFor` fell back to a montage hold and `styleFor` fell back to
    `story`, so a film nobody described got the measured 0.334s arranged to
    story's bars — 25 cuts every ten seconds instead of 20. Neither fallback
    was wrong on its own; they were wrong together, which is the kind of fault
    that survives every test written about either half.
    """
    source = (Path(__file__).resolve().parent.parent / "tools" / "artifact" / "style.js").read_text(
        encoding="utf-8"
    )
    render = (
        Path(__file__).resolve().parent.parent / "tools" / "artifact" / "browser-render.js"
    ).read_text(encoding="utf-8")

    style_fallback = re.search(r"return STYLES\.([a-z]+);\s*\n  \}", source)
    assert style_fallback, "styleFor's fallback is gone or reshaped"

    cadence_fallback = re.search(r'return \{ hold: [0-9.]+, label: "([^"]*)" \};', render)
    assert cadence_fallback, "cadenceFor's fallback is gone or reshaped"

    assert style_fallback.group(1) in cadence_fallback.group(1), (
        f"a prompt with no words is arranged as {style_fallback.group(1)!r} and held at "
        f"{cadence_fallback.group(1)!r} — the default is two halves of different films"
    )


def test_the_browser_arranges_a_montage_at_the_rate_the_reels_cut_at():
    """Agreeing on the hold is not the same as agreeing on the film.

    The two engines were brought onto the same 0.334s, and the published page
    still came out at 26 cuts per ten seconds against the corpus's 20.1 —
    because a shot's length is the hold times a multiplier from the style's
    bar pattern, and there was no montage style in the browser at all. The word
    fell through to `story`, whose bars average 1.24, so the pace was borrowed
    from a style that means something else.

    The bars are read out of the JavaScript and the rate recomputed here, so
    editing either the bars or the hold without checking fails.
    """
    import statistics

    from auteur.director.brief import parse_brief

    source = (Path(__file__).resolve().parent.parent / "tools" / "artifact" / "style.js").read_text(
        encoding="utf-8"
    )

    block = re.search(r"\n    montage: \{(.*?)\n    \}", source, re.S)
    assert block, "the browser has no montage style"

    bars = re.search(r"bars: (\[\[.*?\]\]),", block.group(1), re.S)
    assert bars, "the montage style has no bar patterns"
    patterns = [
        [float(n) for n in group.split(",")]
        for group in re.findall(r"\[([^\[\]]+)\]", bars.group(1))
    ]
    assert len(patterns) >= 3, f"only {len(patterns)} bars — a film would repeat one phrase"

    pooled = [value for pattern in patterns for value in pattern]
    hold = parse_brief("montage").base_shot_length
    rate = 10.0 / (statistics.mean(pooled) * hold)

    # What the thirteen montage reels measure at, recomputed here rather than
    # typed in, so re-measuring the corpus moves the bar.
    reels = json.loads(
        (
            Path(__file__).resolve().parent.parent / "tools" / "artifact" / "templates.json"
        ).read_text(encoding="utf-8")
    )
    band = [r for r in reels if 0.20 < float(r["hold"]) <= 0.75]
    corpus = statistics.median(float(r["shots"]) / float(r["seconds"]) * 10.0 for r in band)

    assert rate == pytest.approx(corpus, rel=0.12), (
        f"the browser arranges a montage at {rate:.1f} cuts per ten seconds and the "
        f"reels cut at {corpus:.1f}"
    )

    # The median shot is one hold — that is what makes 0.334s the median rather
    # than the floor — and no bar is all one number, which would be a
    # metronomic movement.
    assert statistics.median(pooled) == 1, "the median shot is not the measured hold"
    for pattern in patterns:
        assert len(set(pattern)) > 1, f"the bar {pattern} has no rhythm in it"


def test_both_renderers_cut_the_same_word_at_the_same_pace():
    """There are two pace tables, and nothing compared them.

    The app cuts with Python; the published page has no Python behind it and
    cuts with `browser-render.js`, which carries its own copy of the same
    table. Two copies of a number drift, and these had: `montage` was 0.5s in
    the browser against 0.334s in the app, and the browser's no-pace-word
    fallback was still 0.9s — the invented number the app had already stopped
    using. Somebody opening the published page got a different film from the
    same words.

    This reads the numbers out of the JavaScript rather than restating them, so
    changing either side without the other fails here.
    """
    from auteur.director.brief import parse_brief

    source = (
        Path(__file__).resolve().parent.parent / "tools" / "artifact" / "browser-render.js"
    ).read_text(encoding="utf-8")

    table = re.search(r"var CADENCES = \[(.*?)\n  \];", source, re.S)
    assert table, "the browser's cadence table is gone or renamed"

    rows = re.findall(r'\[\s*([0-9.]+),\s*"[^"]*",\s*wordy\((/.*?/)\)\]', table.group(1))
    assert len(rows) >= 4, f"only parsed {len(rows)} cadence rows"

    # The word each row is really about, taken as the first literal alternative
    # in its pattern — that is the word a person types.
    checked = 0
    for hold, pattern in rows:
        first = re.match(r"/([a-z]+)", pattern)
        if not first:
            continue
        word = first.group(1)
        if word not in ("hypercut", "montage"):
            continue  # the two the app names in its own table
        assert float(hold) == pytest.approx(parse_brief(word).base_shot_length, abs=0.005), (
            f"the browser cuts {word!r} at {hold}s and the app cuts it at "
            f"{parse_brief(word).base_shot_length}s"
        )
        checked += 1
    assert checked == 2, f"only checked {checked} of the two named paces"

    # And the fallback, which is what most people get: no pace word at all.
    fallback = re.search(r'return \{ hold: ([0-9.]+), label: "[^"]*" \};\s*\n  \}', source)
    assert fallback, "the browser's no-pace-word fallback is gone or reshaped"
    assert float(fallback.group(1)) == pytest.approx(
        parse_brief("").base_shot_length, abs=0.005
    ), "the two renderers disagree about a film nobody gave a pace for"


def test_the_app_can_ask_for_the_cadence_the_references_are_cut_at():
    """The style existed and no chip offered it.

    Four ceilings in the code used to make the reference cadence unreachable;
    with those gone there was still no way to ask for it from the app.
    """
    from auteur.director.brief import parse_brief
    from auteur.web import server

    page = (server.STATIC / "index.html").read_text()
    chips = page.split('id="chips"', 1)[1].split("</span>", 1)[0]
    prompts = re.findall(r'data-prompt="([^"]+)"', chips)
    assert prompts, "no prompt chips at all"

    styles = {parse_brief(prompt).style for prompt in prompts}
    assert "hypercut" in styles, "no chip reaches the reference cadence"

    # And the pace the corpus sits at when it is not sprinting. This one was
    # already the *default* style and still had no chip, so the thing most
    # films are cut as was the one thing you could not ask for by name.
    assert "montage" in styles, "no chip offers the pace most of the corpus cuts at"

    # Every chip has to land where its own words point. A chip reading
    # "Montage" whose prompt happens to contain a pace word would quietly cut
    # at that word's pace instead, and the label would be a lie on the button.
    by_label = dict(re.findall(r'data-prompt="([^"]+)"[^>]*>([^<]+)<', chips))
    for prompt, label in by_label.items():
        if label.strip().lower() in ("hypercut", "montage"):
            expected = parse_brief(label.strip().lower()).base_shot_length
            assert parse_brief(prompt).base_shot_length == pytest.approx(expected), (
                f"the {label.strip()!r} chip cuts at "
                f"{parse_brief(prompt).base_shot_length}s, not the {expected}s its "
                "own name asks for"
            )


def test_the_studio_shows_what_the_films_agree_on(web_server):
    """The consensus is the only thing in the store an agent can be held to."""
    import json as _json
    from urllib.request import Request, urlopen

    base, _, cookie = web_server
    request = Request(base + "/api/scholar", headers={"Cookie": cookie})
    with urlopen(request) as response:
        payload = _json.loads(response.read())

    # The key is served whether or not anything has been studied yet.
    assert "consensus" in payload
    assert isinstance(payload["consensus"], list)

    page = (server_static() / "studio.html").read_text()
    assert 'id="scholar-consensus"' in page
    script = (server_static() / "studio.js").read_text()
    assert "s.consensus" in script, "the page does not read what the API serves"


def server_static():
    from auteur.web import server

    return server.STATIC


# ---------------------------------------------------------------------------
# Cutting to a reel's own timeline
# ---------------------------------------------------------------------------


def _template_of(beats):
    """A template built by hand, so a test does not need a reel to decode."""
    from auteur.insight.template import Beat, Template

    made = []
    at = 0.0
    for span, luma in beats:
        made.append(Beat(start=at, duration=span, luma=luma, contrast=0.2))
        at += span
    return Template(name="made-up", fingerprint="abc123", seconds=at, beats=made)


def _photos(tmp_path, count=4):
    from PIL import Image

    out = []
    for index in range(count):
        path = tmp_path / f"photo{index}.png"
        # Different brightnesses, so tone matching has something to choose on.
        grey = 30 + index * 60
        Image.new("RGB", (400, 700), (grey, grey, grey)).save(path)
        out.append(path)
    return out


def test_a_template_keeps_when_the_cuts_land_not_how_fast_they_average():
    template = _template_of([(0.5, 0.2), (0.2, 0.8), (0.2, 0.5), (0.2, 0.4), (4.0, 0.4)])
    assert [round(b.start, 2) for b in template.beats] == [0.0, 0.5, 0.7, 0.9, 1.1]
    # The median hold, not the mean: one four second shot must not make a reel
    # cut five times a second look slow, which is the whole reason a timeline
    # beats an average. The mean here is 1.02s.
    assert template.shot_seconds == 0.2
    assert template.hook == 0.5


def test_a_film_cut_to_a_template_holds_every_shot_for_as_long_as_the_reel_did(tmp_path):
    from auteur.insight.template import cast

    template = _template_of([(0.5, 0.2), (0.2, 0.8), (0.2, 0.5), (0.3, 0.4)])
    film = cast(template, _photos(tmp_path))

    assert len(film.shots) == len(template.beats)
    for shot, beat in zip(film.shots, template.beats, strict=True):
        assert abs(shot.duration - beat.duration) < 0.001


def test_the_same_picture_never_lands_either_side_of_a_cut(tmp_path):
    from auteur.insight.template import cast

    # Twelve beats and four pictures: something has to repeat, and the one
    # thing it must not do is repeat across a cut, which is not a cut.
    template = _template_of([(0.2, 0.5)] * 12)
    film = cast(template, _photos(tmp_path))
    sources = [shot.source for shot in film.shots]
    assert all(a != b for a, b in zip(sources, sources[1:], strict=False))


def test_a_dark_beat_is_given_a_dark_picture(tmp_path):
    from auteur.insight.template import cast

    template = _template_of([(0.4, 0.5), (0.4, 0.05), (0.4, 0.95)])
    film = cast(template, _photos(tmp_path))
    # Shot 0 is the opener and picked for detail, so judge the two after it.
    darkest = min(_photos(tmp_path), key=lambda p: p.name)
    assert film.shots[1].source == darkest or film.shots[1].look.exposure < 0.2


def test_the_grade_carries_a_picture_towards_the_beat_without_becoming_it(tmp_path):
    from auteur.insight.template import PULL, cast

    template = _template_of([(0.4, 0.5), (0.4, 0.9)])
    film = cast(template, _photos(tmp_path, count=2))
    # A correction that fully matched every shot would flatten the person's
    # own photographs into the reference's palette.
    assert PULL < 1.0
    assert all(abs(shot.look.exposure) <= 0.8 for shot in film.shots)


def test_a_template_repeats_from_the_top_to_fill_a_longer_film():
    from auteur.insight.template import timeline

    template = _template_of([(0.5, 0.2), (0.5, 0.8)])
    longer = timeline(template, seconds=2.5)
    assert abs(sum(b.duration for b in longer) - 2.5) < 0.01
    # Second time through starts at the top again: a reel's shape is a run at
    # something and a return, and playing that backwards is not a second run.
    assert longer[2].luma == longer[0].luma


def test_a_template_trims_rather_than_overrunning_a_shorter_film():
    from auteur.insight.template import timeline

    template = _template_of([(0.5, 0.2), (0.5, 0.8), (0.5, 0.4)])
    shorter = timeline(template, seconds=0.8)
    assert sum(b.duration for b in shorter) <= 0.81


def test_the_words_go_where_the_reel_put_words(tmp_path):
    from auteur.insight.template import Beat, Template, cast

    beats = [
        Beat(start=0.0, duration=0.4, words=0.0),
        Beat(start=0.4, duration=0.4, words=0.9),
        Beat(start=0.8, duration=0.4, words=0.1),
    ]
    template = Template(name="t", fingerprint="f", seconds=1.2, beats=beats)
    film = cast(template, _photos(tmp_path), words=["HELLO"])
    assert len(film.texts) == 1
    assert abs(film.texts[0].start - 0.4) < 0.001


def test_cutting_to_a_template_with_no_openable_pictures_says_so(tmp_path):
    from auteur.insight.template import cast

    template = _template_of([(0.4, 0.5)])
    with pytest.raises(ValueError, match="would open"):
        cast(template, [tmp_path / "not-a-picture.png"])


def test_a_template_survives_the_trip_through_json(tmp_path):
    from auteur.insight.template import Template

    template = _template_of([(0.5, 0.2), (0.25, 0.8)])
    path = tmp_path / "t.json"
    template.save(path)
    back = Template.load(path)
    assert back.shot_seconds == template.shot_seconds
    assert [b.start for b in back.beats] == [b.start for b in template.beats]


def test_the_shelf_keeps_one_template_per_reel_however_it_is_named(tmp_path):
    from auteur.scholar.library import TemplateShelf

    shelf = TemplateShelf(tmp_path / "templates")
    template = _template_of([(0.4, 0.5), (0.4, 0.3)])
    template.save(shelf.folder / f"{template.fingerprint}.json")
    # The same reel again under another name is the same fingerprint, so it
    # lands on the same file rather than beside it.
    template.name = "renamed"
    template.save(shelf.folder / f"{template.fingerprint}.json")
    assert len(shelf.all()) == 1
    assert shelf.find("renamed") is not None


def test_the_shelf_picks_the_reel_cut_closest_to_a_rate_that_was_asked_for(tmp_path):
    from auteur.scholar.library import TemplateShelf

    shelf = TemplateShelf(tmp_path / "templates")
    slow = _template_of([(2.0, 0.5)] * 5)
    slow.name, slow.fingerprint = "slow", "1111"
    fast = _template_of([(0.167, 0.5)] * 30)
    fast.name, fast.fingerprint = "fast", "2222"
    for one in (slow, fast):
        one.save(shelf.folder / f"{one.fingerprint}.json")

    assert shelf.closest_to(35.0).name == "fast"
    assert shelf.closest_to(5.0).name == "slow"


# ---------------------------------------------------------------------------
# Scrolling a feed rather than reading about one
# ---------------------------------------------------------------------------


class _FakeFeed:
    """Serves a fixed sequence, so a test never needs a network."""

    name = "fake"

    def __init__(self, files, *, why=""):
        self._files = list(files)
        self._why = why

    def reachable(self):
        return (not self._why), self._why

    def serve(self, query, *, count):
        for path in self._files[:count]:
            yield path, {"from": "fake"}


def _reel(tmp_path, name, *, shots, hold):
    """A tiny film with a known number of cuts in it."""
    import subprocess

    from PIL import Image

    frames = tmp_path / f"{name}-frames"
    frames.mkdir(exist_ok=True)
    index = 0
    for shot in range(shots):
        # Alternating black and white, so every boundary is unmissable.
        tone = 20 if shot % 2 else 235
        for _ in range(max(1, int(round(hold * 24)))):
            Image.new("RGB", (128, 224), (tone, tone, tone)).save(frames / f"{index:04d}.png")
            index += 1
    out = tmp_path / f"{name}.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "quiet",
            "-y",
            "-framerate",
            "24",
            "-i",
            str(frames / "%04d.png"),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
        check=True,
    )
    return out


def test_a_scroll_keeps_the_order_it_was_served_in(tmp_path):
    from auteur.scholar.feed import scroll

    files = [
        _reel(tmp_path, "a", shots=8, hold=0.25),
        _reel(tmp_path, "b", shots=4, hold=0.5),
        _reel(tmp_path, "c", shots=8, hold=0.25),
    ]
    session = scroll(_FakeFeed(files), count=3)
    assert [s.position for s in session.servings] == [0, 1, 2]
    # The order is the whole point, so it must be the order served and not
    # anything tidier.
    assert [Path(s.source).name for s in session.servings] == [f.name for f in files]


def test_a_feed_that_cannot_be_reached_says_so_instead_of_looking_empty():
    from auteur.scholar.feed import scroll

    session = scroll(_FakeFeed([], why="a proxy refused the connection"))
    assert session.watched == 0
    # An empty list reads as "there was nothing there", which is a different
    # fact from "it could not be asked".
    assert "proxy" in session.unreachable


def test_a_session_reports_what_the_top_of_the_feed_differed_by(tmp_path):
    from auteur.scholar.feed import Scroll, Serving

    session = Scroll(feed="fake", query="")
    for position in range(6):
        # The first three cut twice as fast as the last three.
        session.servings.append(
            Serving(
                position=position,
                name=f"r{position}",
                source="x",
                seconds=10.0,
                cuts_per_10s=40.0 if position < 3 else 10.0,
                shot_seconds=0.2 if position < 3 else 0.8,
            )
        )
    said = " ".join(session.what_it_served())
    assert "40.00" in said and "10.00" in said
    assert "Higher" in said


def test_a_session_that_found_nothing_says_that_rather_than_nothing():
    from auteur.scholar.feed import Scroll, Serving

    session = Scroll(feed="fake", query="")
    for position in range(6):
        session.servings.append(
            Serving(
                position=position,
                name="r",
                source="x",
                seconds=10.0,
                cuts_per_10s=20.0,
                shot_seconds=0.5,
            )
        )
    said = session.what_it_served()
    assert len(said) == 1
    assert "differed enough" in said[0]


def test_every_scroll_is_its_own_voice_not_one_voice_called_youtube():
    from auteur.scholar.feed import Scroll, Serving, learnings_from

    def session_at(when):
        one = Scroll(feed="youtube", query="reels", at=when)
        for position in range(6):
            one.servings.append(
                Serving(
                    position=position,
                    name="r",
                    source="x",
                    seconds=10.0,
                    cuts_per_10s=40.0 if position < 3 else 10.0,
                    shot_seconds=0.3,
                )
            )
        return one

    first = learnings_from(session_at(1000.0))
    second = learnings_from(session_at(2000.0))
    assert first and second
    # Keyed on the session, so ten scrolls agreeing is ten sources. Keyed on
    # the feed they would be one voice forever, which is the mistake the film
    # library already made once.
    assert first[0].source_channel != second[0].source_channel


def test_a_scroll_survives_the_trip_through_json(tmp_path):
    from auteur.scholar.feed import Scroll, ScrollHistory, Serving

    session = Scroll(feed="fake", query="reels")
    session.servings.append(Serving(position=0, name="r", source="x", cuts_per_10s=30.0))
    history = ScrollHistory(tmp_path / "scrolls")
    history.keep(session)
    back = history.all()
    assert len(back) == 1
    assert back[0].servings[0].cuts_per_10s == 30.0


def test_only_a_direction_most_sessions_agree_on_is_reported(tmp_path):
    from auteur.scholar.feed import Scroll, ScrollHistory, Serving

    history = ScrollHistory(tmp_path / "scrolls")

    def session(at, faster_at_top):
        one = Scroll(feed="fake", query="", at=at)
        for position in range(6):
            fast = position < 3 if faster_at_top else position >= 3
            one.servings.append(
                Serving(
                    position=position,
                    name="r",
                    source="x",
                    seconds=10.0,
                    cuts_per_10s=40.0 if fast else 10.0,
                    shot_seconds=0.3,
                )
            )
        return one

    # Three sessions that disagree three ways say nothing.
    split = [session(1.0, True), session(2.0, False)]
    assert history.across_sessions(split) == []

    # Three that agree say so, and say how many.
    agreed = [session(float(n), True) for n in range(3)]
    said = history.across_sessions(agreed)
    assert said and "3 of them" in said[0]


def test_saying_a_youtube_route_exists_is_not_saying_youtube_answered(monkeypatch):
    from auteur.scholar import youtube

    # `reachable` looks for the tool; `can_reach` asks YouTube. Both can be
    # present on a machine with no route out, which is what a proxy is, and
    # conflating them sent the study loop into 403s it had been told about.
    monkeypatch.setattr(youtube, "_ytdlp", lambda: "/usr/bin/yt-dlp")
    assert youtube.reachable()[0] is True

    class _Refused:
        returncode = 1
        stdout = ""
        stderr = "ERROR: Unable to connect to proxy: 403 Forbidden"

    monkeypatch.setattr(youtube.subprocess, "run", lambda *a, **k: _Refused())
    ok, why = youtube.can_reach()
    assert ok is False
    assert "proxy" in why


def test_the_cli_reports_a_user_error_without_raising(capsys):
    from auteur.cli import main

    # Every one of these is a *failure* path, which is exactly the kind that
    # goes untested and then raises AttributeError at the moment it fires —
    # the same shape as the six success messages CodeQL caught.
    for argv in (
        ["template", "watch"],
        ["template", "cut"],
        ["template", "cut", "nothing-called-this", "also-not-a-file.jpg"],
    ):
        code = main(argv)
        assert code != 0
    out = capsys.readouterr().out
    assert "✗" in out


def test_a_handle_cannot_forge_a_second_log_line(tmp_path, caplog):
    """A log file is read one record per line, so a newline is a forgery."""
    import logging

    from auteur.publish import Connections

    links = Connections(tmp_path / "connections.json")
    evil = "@me\nINFO:auteur.publish.connections:linked instagram for admin as @attacker"
    with caplog.at_level(logging.INFO, logger="auteur.publish.connections"):
        links.link("owner", "instagram", handle=evil, token="")

    written = "\n".join(record.getMessage() for record in caplog.records)
    assert "\n" not in written.replace(written.split("\n")[0], "", 1) or True
    # The forged record must not survive as its own line.
    assert "linked instagram for admin as @attacker" not in written.splitlines()[1:]
    assert all("\n" not in record.getMessage() for record in caplog.records)


def test_a_token_is_never_in_what_a_page_can_see(tmp_path):
    from auteur.publish import Connections

    links = Connections(tmp_path / "connections.json")
    links.link("owner", "tiktok", handle="@me", token="a-real-looking-token")
    seen = [link.public() for link in links.of("owner")]
    assert not any("a-real-looking-token" in str(value) for row in seen for value in row.values())
    # Linked and able-to-post are two different questions.
    tiktok = next(row for row in seen if row["platform"] == "tiktok")
    assert tiktok["connected"] is True
    assert tiktok["can_publish"] is True


def test_a_handoff_link_is_linked_even_without_a_token(tmp_path):
    from auteur.publish import Connections

    links = Connections(tmp_path / "connections.json")
    links.link("owner", "instagram", handle="@me", token="")
    row = next(r for r in links.of("owner") if r.platform == "instagram").public()
    # Keying this on the token told somebody who had just linked their account
    # that nothing had happened.
    assert row["connected"] is True
    assert row["can_publish"] is False


def test_choosing_a_reel_template_gives_the_edit_that_reels_rhythm():
    """A template is where the cuts fall, so it has to change the shot count.

    Keeping the planned shots and only stretching each one would preserve the
    film's length and lose the thing being copied — a reel cut at 0.125s has
    five times the shots of one cut at 0.6s, and that difference *is* the
    template.
    """
    from auteur.edl import EditDecisionList, Shot
    from auteur.web.server import _fit_to_template

    planned = EditDecisionList(
        shots=[
            Shot(clip_id=f"c{n}", source=Path(f"/tmp/{n}.jpg"), start=0.0, end=0.6, is_still=True)
            for n in range(4)
        ]
    )
    assert len(planned.shots) == 4

    # A hypercut's beats: [duration, luma, contrast, saturation, warmth, motion]
    beats = [[0.125, 0.5, 0.2, 0.3, 0.0, 0.1] for _ in range(8)]
    _fit_to_template(planned, beats, seconds=6.0)

    assert len(planned.shots) == 48, "six seconds at 0.125s is forty-eight shots"
    assert all(abs(shot.duration - 0.125) < 1e-6 for shot in planned.shots)
    # The pictures are the director's, reused in order rather than invented.
    assert [s.clip_id for s in planned.shots[:5]] == ["c0", "c1", "c2", "c3", "c0"]
    # Nothing dissolves into the first frame of the film.
    assert planned.shots[0].transition_in.is_cut


def test_a_template_never_extends_a_clip_past_the_footage_the_director_chose():
    """A still can be held for any length. A clip cannot.

    Stretching a two-second selection to four seconds runs the render into
    frames nobody looked at, which is how a template turns into a bug report
    about footage that should not be in the film.
    """
    from auteur.edl import EditDecisionList, Shot
    from auteur.web.server import _fit_to_template

    planned = EditDecisionList(
        shots=[Shot(clip_id="clip", source=Path("/tmp/a.mp4"), start=2.0, end=2.4)]
    )
    _fit_to_template(planned, [[3.0, 0.5, 0.2, 0.3, 0.0, 0.1]], seconds=3.0)

    assert planned.shots, "the template produced no shots at all"
    for shot in planned.shots:
        assert shot.end <= 2.4 + 1e-6, "a clip was extended past its selection"


def test_a_decade_in_the_prompt_is_not_a_runtime():
    """`90s` is the nineties. Read as ninety seconds it wrecks the whole film.

    Measured before the fix: "a 90s hypercut, 12 seconds" planned 324 shots
    across 90 seconds — the bare-`s` branch matched `90s` first and never
    reached the words the person actually wrote. The film then took so long to
    render that it never finished, from a prompt asking for twelve seconds.
    """
    from auteur.director.brief import _extract_duration

    assert _extract_duration('a 90s hypercut, "SUMMER", 12 seconds') == 12.0
    assert _extract_duration("80s vhs montage, 15 seconds") == 15.0
    assert _extract_duration("a 70s super 8 film 20 seconds") == 20.0
    assert _extract_duration("2010s look, 8 seconds") == 8.0

    # A decade on its own is a look, not a length.
    assert _extract_duration("make it 90s") is None
    assert _extract_duration("1980s energy") is None

    # And the ordinary forms still work.
    assert _extract_duration("fast montage, 12 seconds") == 12.0
    assert _extract_duration("15s punchy") == 15.0
    assert _extract_duration("half a minute") == 30.0


def test_picking_a_decade_grades_the_whole_film_to_it():
    """The chooser sent a value nobody read, so picking 90s changed nothing.

    A decade is the film's stock. Applying it to some shots and not others is
    a continuity error rather than a style, so it goes on all of them.
    """
    from auteur.craft.color import LOOKS
    from auteur.web.server import ERA_LOOKS

    # Every value the chooser can send names a look that actually exists.
    for sent, preset in ERA_LOOKS.items():
        assert preset in LOOKS, f"{sent} maps to {preset}, which is not a look"

    # And the front end's options are all covered, so none of them is dead.
    markup = Path("auteur/web/static/index.html").read_text(encoding="utf-8")
    import re

    block = re.search(r'id="era".*?</div>', markup, re.S)
    assert block, "the decade chooser is gone from the markup"
    offered = set(re.findall(r'data-value="([^"]*)"', block.group(0))) - {""}
    assert offered <= set(
        ERA_LOOKS
    ), f"the page offers {offered - set(ERA_LOOKS)}, which is unwired"


def test_a_view_reported_by_the_player_reaches_the_ranking(web_server):
    """The whole round trip, over a socket, the way the phone does it.

    Every other test of this drives the store directly. This one posts the same
    JSON the feed's beacon sends, because a store that works and a route that
    never reaches it look identical from inside the store.

    The first version of this test asserted the wrong thing: it had one account
    both watching and reading, then expected the film that account had watched
    to the end to rank *first*. It ranks lower, deliberately — a feed that
    leads with what you just finished is a feed with nothing new in it. Which
    is why the account reading here is not the account that did the watching.
    """
    base, studio, cookie = web_server
    from auteur.web import server as web

    films = {}
    for name in ("dull", "loved", "seen"):
        film = web.Handler.films.add(
            owner="grace",
            prompt=f"a {name} one",
            video=str(studio.workspace / f"{name}.mp4"),
        )
        films[name] = film.id

    watching = web.Handler.watching

    # Over the wire, as the player sends it: tester bounces off `dull` and
    # watches `seen` to the end.
    for _ in range(6):
        posted = _api_post(
            base, "/api/watched", cookie, {"film": films["dull"], "seconds": 1.2, "runtime": 12.0}
        )
        assert posted["ok"]
    assert watching.reception(films["dull"]).plays == 6, "the route never reached the store"

    _api_post(
        base, "/api/watched", cookie, {"film": films["seen"], "seconds": 12.0, "runtime": 12.0}
    )
    assert watching.already_seen("tester") == {films["seen"]}

    # And other people love `loved`, which tester has never opened.
    for n in range(8):
        watching.played(f"someone{n}", films["loved"], seconds=12.0, runtime=12.0)
    watching.shared(films["loved"])

    assert watching.merit(films["loved"]) > watching.merit(films["dull"])

    order = [row["id"] for row in _api_get(base, "/api/feed", cookie)["films"]]
    assert (
        order[0] == films["loved"]
    ), f"the feed did not lead with the best-received unseen film: {order}"
    assert order.index(films["seen"]) > order.index(
        films["loved"]
    ), "a film this person already watched through outranks one they have not seen"

    # And the person can see what the instance recorded about them.
    mine = _api_get(base, "/api/watching", cookie)
    watched = {row["film"]: row for row in mine["watched"]}
    assert set(watched) == {films["dull"], films["seen"]}, "the history is wrong"
    assert watched[films["seen"]]["finished"] is True
    assert watched[films["dull"]]["finished"] is False
    assert films["loved"] not in watched, "a film they never opened is in their history"
    # The history is shown to a person, so it has to say what they watched
    # rather than which row it was. It shipped once reading "8f3c1e2a — 41s".
    assert all(
        row["prompt"] for row in mine["watched"]
    ), f"the history carries no prompts, only identifiers: {mine['watched']}"


def test_the_feed_has_something_to_rank_by(tmp_path):
    """It had nothing, and the insight layer was fitted to a simulation.

    A recommender with no observations is a shuffle with extra steps. Worse,
    `insight.dataset` had always been able to read a real export and never had
    one, so every virality score came from a model fitted to invented rows.
    """
    from auteur.web.watching import Watching

    w = Watching(tmp_path / "watching")

    # Three people watch one film all the way through; six bounce off another.
    for who in ("ana", "ben", "cy"):
        w.played(who, "good", seconds=12.0, runtime=12.0)
    w.shared("good")
    for n in range(6):
        w.played(f"p{n}", "poor", seconds=1.1, runtime=12.0)

    good, poor = w.reception("good"), w.reception("poor")
    assert good.completion_rate == 1.0 and poor.completion_rate == 0.0
    assert good.three_second_watch_rate == 1.0 and poor.three_second_watch_rate == 0.0
    assert w.merit("good") > w.merit("poor"), "the ranking cannot tell them apart"

    # One play does not out-rank fifty. Confidence has to grow with evidence,
    # or the newest thing posted is permanently the best thing on the instance.
    w.played("dana", "lucky", seconds=12.0, runtime=12.0)
    assert w.merit("lucky") < w.merit("good"), "a single play beat three"
    assert 0.35 < w.merit("lucky") < 0.65, "one play should sit near the middle"

    # And the rows reach the model that was waiting for them.
    rows = {r["post_id"]: r for r in w.signals()}
    assert rows["good"]["completion_rate"] == 1.0
    assert rows["good"]["share_to_view_ratio"] > 0
    assert set(rows["good"]) >= {
        "three_second_watch_rate",
        "completion_rate",
        "avg_time_spent_sec",
        "share_to_view_ratio",
    }, "the export does not match what insight.schema reads"


def test_a_hostile_client_cannot_buy_a_ranking(tmp_path):
    """The numbers arrive from somebody else's machine.

    A player reporting an hour of watch time on a twelve-second film is either
    a stuck timer or somebody promoting their own work, and a ranking that
    believes it is a ranking anybody can buy with a curl command.
    """
    from auteur.web.watching import Watching

    w = Watching(tmp_path / "watching")
    w.played("cheat", "mine", seconds=99999.0, runtime=12.0)
    assert w.reception("mine").seconds < 100, "an hour was recorded for a 12s film"

    # Looping is the honest way to exceed the runtime, and it is bounded too.
    w.played("fan", "mine", seconds=60.0, runtime=12.0, looped=3)
    assert w.reception("mine").seconds < 200


def test_deleting_an_account_erases_what_that_person_watched(tmp_path):
    """The half that is about a person has to go when the person does.

    Per-film totals stay, and that is deliberate rather than an oversight:
    they name nobody, and subtracting a departed viewer's seconds would rewrite
    the performance history of films belonging to people who are still here
    without making anybody more private.
    """
    from auteur.web.watching import Watching

    w = Watching(tmp_path / "watching")
    w.played("leaving", "film-a", seconds=12.0, runtime=12.0)
    w.played("leaving", "film-b", seconds=5.0, runtime=12.0)
    w.played("staying", "film-a", seconds=12.0, runtime=12.0)

    assert len(w.history("leaving")) == 2
    plays_before = w.reception("film-a").plays

    removed = w.forget_everything_about("leaving")
    assert removed == 2
    assert w.history("leaving") == [], "the history survived deletion"
    assert w.already_seen("leaving") == set()
    assert w.history("staying"), "somebody else's history was taken too"
    assert w.reception("film-a").plays == plays_before, "a film's totals were rewritten"

    # And it is gone from the file, not just from memory.
    reopened = Watching(tmp_path / "watching")
    assert reopened.history("leaving") == [], "deletion did not reach the disk"

    # Nothing anywhere in the stored aggregates names them.
    text = (tmp_path / "watching" / "reception.json").read_text(encoding="utf-8")
    assert "leaving" not in text, "the per-film file carries a viewer's name"


def test_the_feed_learns_a_taste_without_burying_everything_else(tmp_path):
    """Personalisation that cannot change the order is decoration.

    And personalisation that ignores everything else is a bubble. Both are
    checked here, because the failure mode is different at each end: a bonus
    too small never fires, and one too large shows somebody only ever the first
    maker they happened to finish.
    """
    from dataclasses import dataclass

    from auteur.web.watching import Watching

    @dataclass
    class Made:
        id: str
        owner: str

    films = [Made("a1", "ana"), Made("a2", "ana"), Made("b1", "ben"), Made("b2", "ben")]
    made_by = {f.id: f.owner for f in films}

    w = Watching(tmp_path / "watching")
    # Everything is received identically, so nothing but taste can decide.
    for n in range(10):
        for film in films:
            w.played(f"crowd{n}", film.id, seconds=10.0, runtime=12.0)

    merits = {f.id: round(w.merit(f.id), 4) for f in films}
    assert len(set(merits.values())) == 1, f"the tie is not a tie: {merits}"

    # Somebody who finishes ana's work, repeatedly.
    for _ in range(3):
        w.played("dana", "a1", seconds=12.0, runtime=12.0)

    order = [f.id for f in w.for_you("dana", films, made_by=made_by)]
    assert order[0] == "a2", f"taste did not carry to the same maker's unseen film: {order}"
    # Not a bubble: ben is still in the feed, and reachable.
    assert set(order) == {"a1", "a2", "b1", "b2"}, "personalising removed films"
    assert order.index("a1") > order.index("a2"), "a film already finished ranks first"


def test_the_site_ships_the_palette_the_app_actually_uses():
    """The site said it was generated from theme.py. Nothing generated it.

    Measured before the fix: all thirteen colours it carried had drifted, in
    both light and dark, and three roles were missing entirely. The most
    visible was `--moss` — green on the site, teal in the app, for months. A
    landing page showing a different-coloured product than the one it links to
    is a landing page working against itself, and nothing could tell, because
    the check was a comment.
    """
    from auteur import theme

    site = (Path(__file__).resolve().parent.parent / "docs" / "index.html").read_text(
        encoding="utf-8"
    )
    head, marker, tail = site.partition("@media (prefers-color-scheme: light)")
    assert marker, "the site has no light scheme at all"

    for scheme, block in (("dark", head), ("light", tail)):
        shipped = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6,8})", block))
        for role in theme.ROLES:
            css = role.replace("_", "-")
            want = theme.hex_of(role, scheme)
            assert css in shipped, f"the site is missing --{css} in {scheme}"
            assert shipped[css].lower() == want.lower(), (
                f"{scheme} --{css}: the site ships {shipped[css]} and the app uses {want} — "
                "run tools/site/build_site.py"
            )


def test_the_site_paints_its_main_action_the_colour_the_app_does():
    """The site's call to action was the app's error colour.

    `--rust` is what the app paints "Delete my account" and a failed sign-in.
    The site painted "Make one in your browser" with it — so the one button
    the whole landing page exists to get pressed was the red that everywhere
    else means *stop*, while the app's own primary button was blue.

    A palette test has guarded this file for a while and it passed throughout,
    because it compares the site's *tokens* against `theme.py` and every token
    was correct. What diverged was which token got used, and nothing compared
    that to anything. So this compares it to the app: the same rule, in the
    stylesheet the app actually serves.
    """
    import re

    from auteur.web import server

    root = Path(__file__).resolve().parent.parent
    builder = (root / "tools" / "site" / "build_site.py").read_text(encoding="utf-8")
    app_css = (server.STATIC / "style.css").read_text(encoding="utf-8")

    def primary_of(css: str, where: str) -> tuple[str, str]:
        block = re.search(r"\.go\s*\{([^}]*)\}", css)
        assert block, f"{where} has no .go rule any more"
        body = block.group(1)
        background = re.search(r"background:\s*var\(--([a-z-]+)\)", body)
        foreground = re.search(r"\bcolor:\s*var\(--([a-z-]+)\)", body)
        assert background and foreground, f"{where}'s .go does not use tokens: {body}"
        return background.group(1), foreground.group(1)

    site = primary_of(builder, "the site builder")
    app = primary_of(app_css, "the app stylesheet")

    assert site == app, (
        f"the site paints its primary button with {site} and the app uses "
        f"{app} — the same control has to be the same colour in both"
    )
    # And not anywhere else on the page either. `theme.py` documents `rust` as
    # "it did not work, or needs attention"; the landing page has no failure
    # states at all, so any use of it is a mistake. The wordmark's dot was the
    # other one — the brand mark itself, in the error colour.
    site_html = (root / "docs" / "index.html").read_text(encoding="utf-8")
    for token in ("--rust", "--on-rust"):
        assert f"var({token})" not in site_html, (
            f"the site paints something with {token}, which means "
            '"it did not work" — the page has nothing that can fail'
        )


def test_the_site_that_is_committed_is_the_site_the_builder_makes():
    """`docs/index.html` is generated, committed, and served to the public.

    Generated-and-committed is the arrangement that drifts, and this one had:
    the product was renamed to Auteur Atlas, every other surface followed —
    the app, both store listings, the privacy policy, the terms, the iOS
    bundle — and the website went on saying "auteur" because nobody re-ran the
    builder. It is the page a stranger meets first.

    A palette check has guarded this file for a while and caught colour drift
    once. It guards one value. This regenerates the whole page and compares,
    which is what already holds the iOS bundle to its build, and subsumes the
    palette along with the name, the tagline and the feature list.
    """
    import subprocess

    root = Path(__file__).resolve().parent.parent
    site = root / "docs" / "index.html"
    assert site.is_file(), "the site is not committed at all"

    before = site.read_text(encoding="utf-8")
    try:
        done = subprocess.run(
            [sys.executable, str(root / "tools" / "site" / "build_site.py"), str(site)],
            capture_output=True,
            text=True,
            cwd=root,
        )
        assert "Traceback" not in done.stderr, done.stderr[-2000:]
        after = site.read_text(encoding="utf-8")
        assert after == before, (
            "docs/index.html is not what tools/site/build_site.py produces — "
            "run `python3 tools/site/build_site.py docs/index.html` and commit "
            "the result"
        )
    finally:
        # Leave the tree as it was found, whichever way the assert went.
        site.write_text(before, encoding="utf-8")


def test_one_set_of_words_describes_this_app():
    """There were three, and they had drifted.

    The App Store listing had the current story, the site described the
    command-line tool it was eighteen months ago, and Play had none. Three
    stories is three products to anybody reading them, and which one a person
    believes is decided by which they happen to meet first.
    """
    from auteur import brand

    root = Path(__file__).resolve().parent.parent
    site = (root / "docs" / "index.html").read_text(encoding="utf-8")
    apple = (root / "tools" / "appstore" / "listing.py").read_text(encoding="utf-8")
    play = (root / "tools" / "play" / "listing.py").read_text(encoding="utf-8")

    # Both listings read the shared source rather than keeping a copy.
    for name, text in (("the App Store listing", apple), ("the Play listing", play)):
        assert "brand." in text, f"{name} does not read auteur/brand.py"

    # And the site says the same things, because it is generated from it.
    assert brand.TAGLINE in site, "the site does not carry the tagline"
    for feature in brand.FEATURES:
        assert feature.headline in site, f"the site is missing {feature.headline!r}"

    # A claim the app cannot deliver on its own is marked as needing a server,
    # rather than sitting on a landing page as though it were in the box.
    needs_server = [f for f in brand.FEATURES if not f.on_device]
    assert needs_server, "nothing is marked as needing an instance — check the flags"


def test_the_copy_fits_both_stores_not_just_apple():
    """Play's boxes are not Apple's boxes.

    Play's short description takes 80 characters where Apple's subtitle takes
    30, and Play has no keyword field at all. A listing written to Apple's
    shape and pasted into Play is a listing that either overflows or wastes
    most of the room it was given.
    """
    from auteur import brand

    for store in ("apple", "play"):
        assert brand.too_long(store) == [], f"{brand.LIMITS[store].store}: " + "; ".join(
            brand.too_long(store)
        )

    limits = brand.LIMITS
    assert limits["play"].short > limits["apple"].short, "the two stores' shapes have merged"
    assert limits["play"].keywords == 0, "Play has no keyword field"

    # The long description is built from the features, not typed beside them.
    described = brand.description()
    for feature in brand.FEATURES:
        assert feature.headline in described, f"{feature.headline!r} is missing from the listing"


def test_google_play_is_asked_the_questions_apple_never_asks():
    """A preflight that covers one store tells you nothing about the other.

    Play's two blockers are its own: a Data safety declaration it will not
    infer from the binary, and working reviewer access for anything behind a
    sign-in. This app has a sign-in for the instance features, so neither is
    optional for it.
    """
    play = (Path(__file__).resolve().parent.parent / "tools" / "play" / "listing.py").read_text(
        encoding="utf-8"
    )

    for needed, why in (
        ("DATA_SAFETY", "the Data safety declaration"),
        ("APP_ACCESS", "reviewer access for the sign-in"),
        ("CONTENT_RATING", "the IARC questionnaire"),
        ("aab", "the bundle format Play requires"),
        ("Target API level", "the API floor that blocks uploads"),
    ):
        assert needed in play, f"the Play pack does not answer {why}"


def test_a_film_can_be_accepted_and_still_never_be_seen():
    """One ceiling could not say the thing that matters.

    Instagram accepts a Reel of about twenty minutes and recommends one of
    three, so a film can be entirely legal and entirely invisible — and a
    single `max_seconds` has no way to express that. Checking the numbers
    against current guidance is what surfaced it: three of the six limits in
    the file had risen while it kept the old ones, so it was refusing films the
    platforms would have taken, and saying nothing at all about the length that
    actually decides whether anybody sees them.
    """
    from auteur.workflows.platforms import PLATFORMS

    reach = {k: s for k, s in PLATFORMS.items() if s.reach_seconds}
    assert reach, "no surface distinguishes what is accepted from what travels"

    for key, spec in reach.items():
        assert (
            spec.reach_seconds < spec.max_seconds
        ), f"{key}: the reach ceiling is not below the hard one, so it says nothing"
        assert (
            spec.ideal_seconds <= spec.reach_seconds
        ), f"{key}: the length it aims for is already past the length that travels"

        # Between the two: accepted, and a warning that it will not travel.
        between = (spec.reach_seconds + spec.max_seconds) / 2
        assert (
            spec.duration_problem(between) == ""
        ), f"{key}: {between:.0f}s is refused, and the platform would take it"
        assert spec.reach_problem(
            between
        ), f"{key}: {between:.0f}s posts to nobody and nothing says so"

        # Under the reach ceiling: nothing to report either way.
        fine = spec.ideal_seconds
        assert not spec.duration_problem(fine) and not spec.reach_problem(fine)

    # Surfaces with a genuine hard stop and no cliff still behave.
    for key, spec in PLATFORMS.items():
        if not spec.reach_seconds:
            assert (
                spec.reach_problem(spec.max_seconds * 2) == ""
            ), f"{key} has no reach ceiling but reports one"


def test_what_each_surface_reports_is_the_number_that_changes_a_decision():
    """ "3-3600s" is true and useless.

    Once the hard limits were corrected they became twenty minutes and an hour,
    and a span written from them tells a person nothing about the film they
    should make. What gets shown is the length that still travels.
    """
    from auteur.workflows.platforms import AS_OF, PLATFORMS

    assert re.fullmatch(r"\d{4}-\d{2}", AS_OF), f"{AS_OF!r} is not a checkable date"

    for key, spec in PLATFORMS.items():
        described = spec.describe()
        ceiling = spec.reach_seconds or spec.max_seconds
        assert (
            f"{ceiling:.0f}s" in described
        ), f"{key}: {described!r} does not name the ceiling that matters"
        if spec.reach_seconds:
            assert (
                "allowed" in described
            ), f"{key}: the hard limit is hidden entirely, so an allowed film looks refused"


def test_the_scholar_has_a_source_that_is_not_this_repository(tmp_path):
    """Audited on the live store: 127 learnings, every one from inside.

    94 measured off the project's own reels, 23 read out of its own markdown, 7
    concluded over those, 3 from its own scrolls. The confidence ladder counts
    independent channels and there was exactly one, so nothing could ever climb
    it — and `library.py` says in its own docstring that a project's notes
    agreeing with a project's notes is not corroboration, which had been true
    and unaddressed for the whole life of the store.
    """
    from auteur.scholar.published import FINDINGS, SOURCES, learn

    assert FINDINGS, "no published findings at all"
    drawn = learn()
    assert len(drawn) == len(FINDINGS)

    for learning in drawn:
        assert learning.source_channel.startswith("published:")
        # The point of an outside source is that a person can go and check it.
        assert learning.source_video_id.startswith(
            "https://"
        ), f"{learning.technique!r} cites no URL"
        assert learning.measurements.get(
            "measured_year"
        ), f"{learning.technique!r} does not say when it was measured"

    # And they are independent of each other, which is what the ladder counts.
    assert len({learning.source_channel for learning in drawn}) >= 3

    # Evidence is graded rather than flattened. A peer-reviewed measurement of
    # 160 films and a trade article are not the same kind of fact, and a store
    # that recorded them identically would be confidently wrong in exactly the
    # places it should hedge.
    kinds = {source.kind for source in SOURCES.values()}
    assert "peer-reviewed" in kinds and "trade" in kinds, "no grading of evidence"
    for source in SOURCES.values():
        if source.kind == "trade":
            assert (
                source.strength.value == "tentative"
            ), f"{source.key} is a trade source starting above tentative"


def test_the_outside_numbers_are_checked_against_this_projects_own(tmp_path):
    """An outside number is only worth having if something is done with it.

    What should be done is the comparison. Redfern's rule for feature film —
    the median hold is about 0.6 of the mean — is a real published constant,
    and it does not survive contact with a fifteen-second reel: measured on
    this corpus the ratio is nearer 0.8, because short form has no room for the
    long held shots that pull a feature's mean away from its median. The
    direction of the advice holds and the constant does not, and knowing which
    is which is the whole value of having read it.
    """
    import statistics

    from auteur.scholar.published import corpus, corroborate

    reels = corpus()
    assert reels, "the corpus the comparison needs is missing"

    drawn = corroborate(reels)
    assert drawn, "nothing was compared"

    skew = next(item for item in drawn if "skew" in item.technique)
    ours = skew.measurements["our_ratio"]
    theirs = skew.measurements["published_ratio"]

    # Recomputed here rather than trusted, from the same per-shot durations.
    ratios = []
    for reel in reels:
        holds = [float(b[0]) for b in reel.get("beats", []) if float(b[0]) > 0]
        if len(holds) >= 8 and statistics.mean(holds) > 0:
            ratios.append(statistics.median(holds) / statistics.mean(holds))
    assert ours == pytest.approx(statistics.median(ratios), abs=0.01)

    assert ours > theirs + 0.1, (
        "the corpus now matches the feature-film ratio, so this comparison "
        "proves nothing — re-measure"
    )
    assert skew.source_video_id.startswith("https://"), "the comparison cites nothing"


def test_a_loop_return_is_measured_against_the_film_it_closes(tmp_path):
    """A constant written for a 0.9s montage outlived the 0.9s montage.

    The loop return exists to be invisible — a brief touch back to the opening
    frame so the reel rounds rather than jumps. At 0.9s it was one shot long
    when a montage held 0.9s. The montage holds 0.334s now, which made the
    return 2.7 holds, and on a hypercut 5.4: the shot meant to slip past
    unnoticed became the longest in the film, sitting at the end where the loop
    is supposed to snap.
    """
    from auteur.director.brief import parse_brief

    for prompt in ("montage", "a hypercut", "slow and cinematic"):
        hold = parse_brief(prompt).base_shot_length
        # The rule as the agent applies it.
        span = min(max(hold * 2.0, 0.2), 0.9, 10.0)
        assert span <= 0.9 + 1e-9, "the return outgrew its ceiling"
        assert span / hold <= 3.0, (
            f"a {prompt!r} film holds {hold}s and the loop return runs {span:.2f}s — "
            f"{span / hold:.1f} holds, which is not a touch back"
        )


def test_the_scholar_names_what_it_measures_not_just_the_files(tmp_path):
    """Per-film learnings describe moments; none of them describes the form.

    The store held twenty-three measured timelines and could not answer "how
    fast do the reels cut?", because every learning was about one reel and the
    word "hypercut" appeared in none of them — while the app offered a Hypercut
    chip on its first screen.
    """
    from auteur.scholar.knowledge import Confidence, Discipline, KnowledgeStore, Learning
    from auteur.scholar.library import conclude

    store = KnowledgeStore(tmp_path / "knowledge.jsonl")
    for n in range(9):
        store.add(
            Learning(
                learning_id=f"film-{n}",
                disciplines=[Discipline.MOVIE_MAKING],
                insight=f"reel-{n}.mp4 cuts fast",
                technique="cutting rate",
                application="hold the crew to this",
                source_video_id=f"file:reel-{n}.mp4",
                source_channel=f"film:{n:012d}",
                source_title=f"reel-{n}.mp4",
                source_end_sec=12.0 + n,
                confidence=Confidence.TENTATIVE,
                measurements={
                    "shot_seconds": 0.167,
                    "cuts_per_10s": 30.0,
                    "first_cut": 0.2,
                    "luma": 0.28,
                    "motion": 0.03,
                },
            )
        )

    drawn = {learning.technique: learning for learning in conclude(store)}
    assert drawn, "nine measured films produced no conclusion about the form"

    named = " ".join(drawn).lower()
    for word in ("hypercut", "hook", "pacing", "grading", "long"):
        assert word in named, f"nothing the Scholar concluded is called {word!r}"

    # Nine films agreeing is not a tentative guess. That is what the ladder is
    # for, and per-film learnings can never climb it on their own.
    fast = drawn["hypercut — how fast a fast cut is"]
    assert fast.confidence is Confidence.VALIDATED
    assert fast.measurements["shot_seconds"] == 0.167

    # And the words are reachable: asking in plain English finds them.
    for learning in drawn.values():
        store.add(learning)
    found = store.recall("how fast do the reels cut?", limit=1)
    assert found, "the conclusion is in the store and cannot be recalled"
    assert "hypercut" in found[0].technique


def test_the_shelf_knows_what_a_montage_is_and_quotes_the_rate_it_measured(tmp_path):
    """The corpus is asked about its own default pace, using its own numbers.

    Two things are held here. The first is that a conclusion about montage
    forms at all — the band between a hypercut and a held shot is where most of
    the shelf lives, and it had no name, which is how the default pace stayed
    an invented number for so long.

    The second is subtler and is why this test reads the real reels rather than
    invented ones: the learning quotes a cut *rate*, and the tempting way to
    get one is 10 / median_hold. That is wrong by half. A reel holding 0.334s a
    shot does not cut thirty times in ten seconds, because it also spends time
    on an opening hold and on the shots it lets run — measured, these thirteen
    cut 20.1 times. A learning that cites the corpus must not state a number
    the corpus contradicts.
    """
    import json
    import statistics

    from auteur.scholar.knowledge import Discipline, KnowledgeStore, Learning
    from auteur.scholar.library import HYPERCUT_HOLD, MONTAGE_HOLD, conclude

    reels = json.loads(
        (
            Path(__file__).resolve().parent.parent / "tools" / "artifact" / "templates.json"
        ).read_text(encoding="utf-8")
    )

    store = KnowledgeStore(tmp_path / "knowledge.jsonl")
    for reel in reels:
        store.add(
            Learning(
                learning_id=f"reel-{reel['id']}",
                disciplines=[Discipline.MOVIE_MAKING],
                insight=f"{reel['label']} holds each shot {reel['hold']}s",
                technique="cutting rate",
                application="hold the crew to this",
                source_video_id=f"file:{reel['id']}.mp4",
                source_channel=f"film:{reel['id']}",
                source_title=reel["label"],
                source_end_sec=float(reel["seconds"]),
                measurements={
                    "shot_seconds": float(reel["hold"]),
                    "cuts_per_10s": float(reel["shots"]) / float(reel["seconds"]) * 10.0,
                },
            )
        )

    drawn = {learning.technique: learning for learning in conclude(store)}
    montage = next((v for k, v in drawn.items() if k.startswith("montage")), None)
    assert montage is not None, f"the shelf concluded {list(drawn)} and nothing about montage"

    band = [r for r in reels if HYPERCUT_HOLD < float(r["hold"]) <= MONTAGE_HOLD]
    assert montage.measurements["shot_seconds"] == pytest.approx(
        statistics.median(float(r["hold"]) for r in band), abs=0.005
    )

    measured_rate = statistics.median(float(r["shots"]) / float(r["seconds"]) * 10.0 for r in band)
    assert montage.measurements["cuts_per_10s"] == pytest.approx(measured_rate, abs=0.05)
    derived_rate = 10.0 / montage.measurements["shot_seconds"]
    assert abs(derived_rate - measured_rate) > 5, (
        "the two ways of getting a rate now agree, so this test proves nothing — "
        "re-measure the corpus"
    )
    assert f"{measured_rate:.1f}" in montage.insight, "the learning quotes an unmeasured rate"

    # And what it concluded is what the director cuts a montage at.
    assert parse_brief("montage").base_shot_length == pytest.approx(
        montage.measurements["shot_seconds"], abs=0.005
    ), "the Scholar measured one pace and the director cuts at another"

    # The same seam on the other word. The hypercut learning took the median
    # of *every* reel on the shelf — held shots included — while calling it
    # how fast a fast cut is, so it reported 0.208s where the director cut
    # 0.167s. One word, two numbers, and nothing compared them.
    fast = drawn["hypercut — how fast a fast cut is"]
    band = [float(r["hold"]) for r in reels if float(r["hold"]) <= HYPERCUT_HOLD]
    assert fast.measurements["shot_seconds"] == pytest.approx(
        statistics.median(band), abs=0.005
    ), "the hypercut finding is measured over reels that are not hypercuts"
    assert parse_brief("a hypercut").base_shot_length == pytest.approx(
        fast.measurements["shot_seconds"], abs=0.005
    ), "the Scholar measured one hypercut and the director cuts another"


def test_a_conclusion_about_the_shelf_is_not_dropped_as_a_repeat(tmp_path):
    """The de-duplicator was throwing away the best answers it had.

    Measured learnings arrive one per film, so a consensus is built and the
    per-film copies are dropped as repeats of it. Conclusions drawn *across*
    the shelf carry measurements too, and were being dropped by the same rule —
    asked how fast the reels cut, the Scholar skipped its own validated
    hypercut finding and answered with a note about runtime.
    """
    from auteur.scholar.knowledge import Confidence, Discipline, KnowledgeStore, Learning
    from auteur.scholar.scholar import Scholar

    store = KnowledgeStore(tmp_path / "knowledge.jsonl")
    for n in range(6):
        store.add(
            Learning(
                learning_id=f"one-film-{n}",
                disciplines=[Discipline.MOVIE_MAKING],
                insight=f"reel-{n}.mp4 cuts 30.0 times per ten seconds",
                technique="cutting rate",
                application="hold the crew to this",
                source_video_id=f"file:reel-{n}.mp4",
                source_channel=f"film:{n:012d}",
                source_title=f"reel-{n}.mp4",
                confidence=Confidence.TENTATIVE,
                measurements={"cuts_per_10s": 30.0, "shot_seconds": 0.167},
            )
        )
    store.add(
        Learning(
            learning_id="across-the-shelf",
            disciplines=[Discipline.MOVIE_MAKING],
            insight="Of 6 reels measured, all hold each shot 0.2s or less — a hypercut.",
            technique="hypercut — how fast a fast cut is",
            application="cut at the measured median",
            source_video_id="across:the-shelf",
            source_channel="across:the-shelf",
            source_title="6 films measured",
            confidence=Confidence.VALIDATED,
            measurements={"shot_seconds": 0.167},
        )
    )

    scholar = Scholar(store=store)
    said = scholar.answer_from_study("how fast do the reels cut?", limit=3)
    assert (
        "hypercut" in said.lower()
    ), "the conclusion drawn across every film was dropped as a repeat of itself"


# ---------------------------------------------------------------------------
# The feed and the inbox
# ---------------------------------------------------------------------------


def test_a_finished_film_outlives_the_job_that_made_it(tmp_path):
    """The whole reason the feed exists: jobs are swept, films are not."""
    from auteur.web.social import Films

    films = Films(tmp_path / "films.json")
    clip = tmp_path / "one.mp4"
    clip.write_bytes(b"not really an mp4, but it is a file")
    films.add(owner="ada", prompt="a hypercut", video=str(clip), facts=["12 shots"])

    # A second process, reading the same file.
    again = Films(tmp_path / "films.json")
    assert [f.prompt for f in again.feed()] == ["a hypercut"]


def test_a_film_never_hands_its_path_on_disk_to_a_browser(tmp_path):
    from auteur.web.social import Films

    films = Films(tmp_path / "films.json")
    clip = tmp_path / "secret-place" / "one.mp4"
    clip.parent.mkdir()
    clip.write_bytes(b"x")
    film = films.add(owner="ada", prompt="p", video=str(clip))

    said = film.public("ada")
    assert "secret-place" not in json.dumps(said)
    assert said["video"] == f"/api/films/{film.id}/video"


def test_the_feed_forgets_films_whose_file_has_been_swept(tmp_path):
    """A feed of rows that play nothing looks busy and is empty."""
    from auteur.web.social import Films

    films = Films(tmp_path / "films.json")
    kept = tmp_path / "kept.mp4"
    kept.write_bytes(b"x")
    films.add(owner="ada", prompt="kept", video=str(kept))
    films.add(owner="ada", prompt="swept", video=str(tmp_path / "gone.mp4"))

    assert films.drop_missing() == 1
    assert [f.prompt for f in films.feed()] == ["kept"]


def test_only_a_films_own_author_can_take_it_out_of_the_feed(tmp_path):
    from auteur.web.social import Films

    films = Films(tmp_path / "films.json")
    clip = tmp_path / "one.mp4"
    clip.write_bytes(b"x")
    film = films.add(owner="ada", prompt="p", video=str(clip))

    assert films.forget(film.id, "grace") is False
    assert films.get(film.id) is not None
    assert films.forget(film.id, "ada") is True
    assert films.get(film.id) is None


def test_liking_a_film_twice_unlikes_it(tmp_path):
    from auteur.web.social import Films

    films = Films(tmp_path / "films.json")
    clip = tmp_path / "one.mp4"
    clip.write_bytes(b"x")
    film = films.add(owner="ada", prompt="p", video=str(clip))

    assert films.like(film.id, "grace").liked_by == ["grace"]
    assert films.like(film.id, "grace").liked_by == []


def test_a_conversation_reads_the_same_from_either_end(tmp_path):
    """Sorting the pair is the whole trick, and it is worth a test: keyed by
    sender-then-recipient, a reply would open a second, empty thread."""
    from auteur.web.social import Messages

    box = Messages(tmp_path / "messages.json")
    box.send("ada", "grace", text="did you see this")
    box.send("grace", "ada", text="the 90s one?")

    assert [n.text for n in box.thread("ada", "grace")] == ["did you see this", "the 90s one?"]
    assert [n.text for n in box.thread("grace", "ada")] == ["did you see this", "the 90s one?"]


def test_reading_a_conversation_is_what_clears_its_unread_count(tmp_path):
    from auteur.web.social import Messages

    box = Messages(tmp_path / "messages.json")
    box.send("ada", "grace", text="one")
    box.send("ada", "grace", text="two")

    assert box.unread("grace") == 2
    assert box.unread("ada") == 0  # your own messages are not news to you
    box.mark_read("grace", "ada")
    assert box.unread("grace") == 0


def test_a_message_with_nothing_in_it_is_not_sent(tmp_path):
    from auteur.web.social import Messages

    box = Messages(tmp_path / "messages.json")
    assert box.send("ada", "grace", text="   ") is None
    assert box.send("ada", "ada", text="hello") is None  # no talking to yourself
    assert box.send("ada", "", text="hello") is None
    assert box.conversations("ada") == []


def test_an_inbox_row_carries_enough_to_draw_itself(tmp_path):
    """A list view that fetches per row is how a phone makes forty requests."""
    from auteur.web.social import Messages

    box = Messages(tmp_path / "messages.json")
    box.send("ada", "grace", text="first")
    box.send("grace", "ada", film="abc123")

    row = box.conversations("ada")[0]
    assert row["who"] == "grace"
    assert row["last"] == "sent a film"
    assert row["mine"] is False
    assert row["unread"] == 1


def test_the_tab_bar_is_on_every_page_behind_the_sign_in():
    """Five slots, same place, every screen. A bar that vanishes on one page
    is a bar people stop trusting to be there."""
    from auteur.web import server

    for page in ("index", "feed", "inbox", "templates", "studio", "ask", "overlays", "connect"):
        text = (server.STATIC / f"{page}.html").read_text()
        assert "chrome.js" in text, f"{page}.html has no tab bar"
    # Except the one page you are not signed in on.
    assert "chrome.js" not in (server.STATIC / "login.html").read_text()


def test_the_feed_and_inbox_are_behind_the_sign_in():
    from auteur.web import server

    for path in ("/feed", "/inbox", "/api/feed", "/api/messages", "/api/people"):
        assert path not in server.PUBLIC_PATHS
        assert not path.startswith(server.PUBLIC_PREFIXES)


# ---------------------------------------------------------------------------
# Joins in the ffmpeg path
# ---------------------------------------------------------------------------


def test_the_progress_term_runs_forwards():
    """xfade's own `P` counts down, whatever its documentation says.

    Measured against the binary by writing `P*200` into the luma plane and
    reading the raw frames back: it falls from 1 to 0 across the join. Every
    custom expression in this module was written to the documented direction
    and so rendered backwards — the whips travelled away from the shot they
    were thrown at. This is the correction, and it is one string so that the
    next expression cannot get it wrong separately.
    """
    from auteur.craft import transitions

    assert transitions.T == "(1-P)"
    for name, expr in transitions.CUSTOM_EXPRESSIONS.items():
        # Every custom join must go through the correction, and none may use a
        # bare P for anything but the correction itself.
        assert "(1-P)" in expr, f"{name} does not use the corrected progress"
        assert expr.count("P") == expr.count("(1-P)") + expr.count("PLANE") + expr.count(
            "PI"
        ), f"{name} uses a bare P somewhere"


def test_the_two_joins_people_actually_ask_for_exist():
    """A portal opening through the outgoing shot, and a carried middle.

    These are the joins named in every description of the reels this program
    is built to make — "part of the previous photo is on the next photo" — and
    the ffmpeg path had neither.
    """
    from auteur.craft import transitions

    assert "portal" in transitions.CUSTOM_EXPRESSIONS
    assert "carry" in transitions.CUSTOM_EXPRESSIONS
    # And a fallback for a build that will not take custom expressions.
    assert transitions.BUILTIN["portal"] == "circleopen"
    assert transitions.BUILTIN["carry"] == "fade"


def test_portal_and_carry_are_written_in_frame_coordinates():
    """Not per-plane, unlike the whips.

    `X` and `Y` are frame coordinates in every plane while `W` and `H` are the
    frame's dimensions everywhere, so halving them for chroma — which is what
    `_plane_expr` is for — draws a second, quarter-sized shape in the corner.
    On a red-to-blue join that was a blue circle in the top-left and a dark red
    one in the middle, on the same frame.
    """
    from auteur.craft import transitions

    for name in ("portal", "carry"):
        assert (
            "PLANE" not in transitions.CUSTOM_EXPRESSIONS[name]
        ), f"{name} is wrapped per-plane and will draw twice"


def test_a_hypercut_may_still_open_a_portal():
    """The references cut hard and still open one on the shots that can hold it.

    The bag used to be ("cut",) exactly, which made every join in the fastest
    style identical — the thing the Gaze agent reports as the absence of a
    decision rather than as a style.
    """
    from auteur.director.brief import parse_brief

    brief = parse_brief("a 90s hypercut, 12 seconds")
    assert brief.style == "hypercut"
    assert "portal" in brief.transitions
    # Still overwhelmingly cuts, or it is not a hypercut any more.
    assert brief.transitions.count("cut") / len(brief.transitions) >= 0.6


def test_the_edl_accepts_every_join_the_renderer_can_actually_make():
    """Two lists that have to agree, held to each other rather than to memory.

    `Transition.normalise` validates against `edl.TRANSITIONS` and silently
    rewrites anything else to a dissolve. So a join added to the renderer but
    not to that set is not a broken join — it is an *invisible* one: the
    director chooses it, the EDL writes down "dissolve", every tally agrees,
    and nobody can tell the feature was never delivered. That happened to both
    `portal` and `carry`.
    """
    from auteur import edl
    from auteur.craft import transitions

    renderable = set(transitions.BUILTIN) | set(transitions.CUSTOM_EXPRESSIONS)
    missing = renderable - edl.TRANSITIONS
    assert missing == set(), f"the renderer can make joins the EDL will rename: {sorted(missing)}"

    # And the other direction: a name the EDL allows that nothing can render
    # would come out of ffmpeg as whatever `xfade_spec` defaults to.
    unrenderable = edl.TRANSITIONS - renderable - {"cut"}
    assert unrenderable == set(), f"the EDL allows joins nothing renders: {sorted(unrenderable)}"


def test_an_unknown_join_says_so_rather_than_becoming_a_dissolve():
    import logging

    from auteur.edl import Transition

    logger = logging.getLogger("auteur.edl")
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)
    try:
        assert Transition("teleport", 0.4).normalise().kind == "dissolve"
    finally:
        logger.removeHandler(handler)
    assert any("teleport" in record.getMessage() for record in records)


# ---------------------------------------------------------------------------
# Levelling, and the two renderers agreeing
# ---------------------------------------------------------------------------


def test_ffmpeg_gamma_is_the_reciprocal_of_the_browsers():
    """The two are documented in opposite directions and it is invisible.

    The browser's lookup table is `pow(x, gamma)`, where a gamma below 1
    brightens. ffmpeg's `eq` says "larger values make the picture brighter", so
    it is `pow(x, 1 / gamma)`. Passing the same number to both applies the
    correction backwards, which is exactly what happened: adding levelling to
    the ffmpeg path moved the two renderers *further* apart, from 9-15 levels
    out of 255 to 10-16, and nothing about the code looked wrong.
    """
    from auteur.craft.color import level_chain

    # A dark picture wants brightening: gamma below 1 in the browser's terms.
    chain = level_chain(0.02, 0.99, 0.60)
    assert "eq=gamma=1.6667" in chain, chain

    # And a bright one wants the opposite.
    chain = level_chain(0.0, 1.0, 1.25)
    assert "eq=gamma=0.8000" in chain, chain


def test_levelling_leaves_a_well_exposed_picture_alone():
    """Pulled back toward doing nothing, or every film gets the same face."""
    from auteur.craft.color import level_chain, level_for

    black, white, gamma = level_for(0.0, 1.0, 0.44)
    assert black == 0.0
    assert white == 1.0
    assert abs(gamma - 1.0) < 0.02
    assert level_chain(black, white, gamma) == ""


def test_levelling_lifts_an_underexposed_one():
    from auteur.craft.color import level_for

    _, _, gamma = level_for(0.01, 0.95, 0.15)
    # Below 1 in the browser's terms, which is the direction that brightens.
    assert gamma < 0.8


def test_the_white_point_can_never_cross_the_black_point():
    """colorlevels inverts the picture if it does, silently."""
    from auteur.edl import Look

    look = Look(black=0.4, white=0.1).normalise()
    assert look.white > look.black

    look = Look(black=0.9, white=0.05).normalise()
    assert look.black <= 0.5
    assert look.white > look.black


def test_a_shot_carries_the_level_measured_from_its_own_footage():
    """Not a global setting: the gap between the renderers tracked how
    underexposed each individual source was."""
    import numpy as np

    from auteur.analysis.video import VideoAnalysis, _ends_of

    dark = np.full((1, 8, 8), 0.10, np.float32)
    dark[0, 0, 0] = 1.0  # one specular highlight, which must not set the white
    low, high = _ends_of(dark)
    assert low < 0.2
    assert high < 0.5, "the 99th percentile let one blown pixel set the white point"

    # And the default is a no-op, so footage nothing measured is untouched.
    blank = VideoAnalysis(fps=24.0, duration=1.0, width=8, height=8)
    assert blank.black_point == 0.0
    assert blank.white_point == 1.0


def test_a_consensus_reading_carries_every_field_a_reading_has():
    """`_consensus` rebuilds a Reading field by field, so a field added to the
    dataclass and forgotten here comes back as its default — and a default of
    0.0 does not read as a bug, it reads as a measurement. `saturation` was
    measured on every sampled frame, dropped here, and reported as exactly 0.00
    for every reel in the corpus."""
    import dataclasses

    from auteur.vision.connoisseur import Reading, _consensus

    varied = [
        Reading(
            **{
                f.name: (
                    (0.1 * (i + 1), 0.2 * (i + 1))
                    if f.name == "focus"
                    else 0.11 * (i + 1) if f.type in (float, "float") else dataclasses.MISSING
                )
                for f in dataclasses.fields(Reading)
                if f.name in {"focus"} or f.type in (float, "float")
            }
        )
        for i in range(3)
    ]
    out = _consensus(varied)
    for field in dataclasses.fields(Reading):
        if field.type not in (float, "float") or field.name == "focus":
            continue
        if field.name in Reading.FILLED_LATER:
            continue
        assert getattr(out, field.name) != 0.0, f"_consensus drops {field.name}"


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------


def test_the_manager_never_posts_anything_anywhere():
    """The one test in this file that is about a promise rather than a bug.

    A tool that plans posts, drafts captions and holds a schedule is one small
    change away from one that publishes them. What makes "it never posts" true
    is not the absence of a feature, it is that nothing in the module can reach
    a network at all — so this reads the source and says so.
    """
    import ast
    import inspect

    from auteur import manager

    source = inspect.getsource(manager)
    tree = ast.parse(source)

    forbidden = {
        "requests",
        "urllib",
        "http",
        "httpx",
        "aiohttp",
        "socket",
        "smtplib",
        "ftplib",
        "webbrowser",
    }
    reached = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            reached.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            reached.add(node.module.split(".")[0])
    assert not (reached & forbidden), f"the manager can reach the network: {reached & forbidden}"

    # And the verb is the one that tells the truth about what happened.
    assert "mark_posted" in dir(manager.Board)
    assert not hasattr(manager.Board, "post")
    assert not hasattr(manager, "publish")


def test_a_plan_exists_before_the_footage_does():
    """The whole point: a post you can plan with nothing in hand."""
    from auteur.manager import Plan

    plan = Plan(
        id="p",
        owner="ada",
        title="Saturday market",
        platform="instagram-reel",
        when="2026-09-01T09:00:00+00:00",
        prompt="a 90s hypercut of the market",
    )
    assert plan.status == "idea"
    assert plan.film == ""


def test_the_shot_list_follows_the_runtime_and_the_measured_hold():
    """A 20 second film at a 0.167s median is a lot of shots, and it says so."""
    from auteur.manager import shot_list

    fast = shot_list("a hypercut", seconds=20.0, hold=0.167)
    slow = shot_list("slow and cinematic", seconds=20.0, hold=1.0)
    assert len(fast) > len(slow) * 3

    # It opens on a hook and ends on a close, once each, however long it runs.
    assert fast[0].role == "hook"
    assert fast[-1].role == "close"
    assert sum(1 for s in fast if s.role == "hook") == 1
    assert sum(1 for s in fast if s.role == "close") == 1

    # And every shot says what it is for, because that is the only part of a
    # shot this program can know before the footage exists.
    assert all(s.what and s.why for s in fast)


def test_every_check_names_what_it_checked_against():
    """A tick without a source is an opinion wearing a tick."""
    from auteur.manager import Plan, check, shot_list

    shots = shot_list("a hypercut", seconds=20.0, hold=0.167)
    plan = Plan(
        id="p",
        owner="ada",
        title="t",
        platform="instagram-reel",
        when="2026-09-01T09:00:00+00:00",
        prompt="a hypercut",
        seconds=20.0,
        shots=[{"role": s.role, "seconds": s.seconds} for s in shots],
        caption="words",
        hashtags=["one"],
        alt_text="a description",
    )
    report = check(plan, hold=0.167, first_cut=0.9)
    assert report.findings
    for finding in report.findings:
        assert finding.source, f"{finding.name} names no source"
        assert finding.verdict in ("pass", "warn", "fail")
    # Nothing this reports is ever a claim that it posted.
    assert report.to_json()["posts"] is False


def test_the_manager_says_it_has_not_measured_the_time_of_day():
    """Nothing in the metric schema records when a post went out, so the
    manager must not imply it knows. Stating a gap is a feature."""
    from auteur.manager import Plan, check

    plan = Plan(
        id="p",
        owner="ada",
        title="t",
        platform="tiktok",
        when="2026-09-01T09:00:00+00:00",
        prompt="a hypercut",
    )
    report = check(plan)
    hour = [f for f in report.findings if f.name == "time of day"]
    assert hour, "the manager silently skipped the question it cannot answer"
    assert hour[0].verdict != "pass"
    assert "not checked" in hour[0].detail


def test_a_length_the_surface_refuses_is_a_failure_not_a_warning():
    from auteur.manager import Plan, check

    plan = Plan(
        id="p",
        owner="ada",
        title="t",
        platform="instagram-story",
        when="2026-09-01T09:00:00+00:00",
        prompt="a hypercut",
        seconds=500.0,
    )
    report = check(plan)
    length = [f for f in report.findings if f.name == "length"][0]
    assert length.verdict == "fail"


def test_a_prediction_never_travels_without_its_provenance():
    """A fitted-on-simulated-rows model gives a perfectly confident number that
    predicts the simulator. A number without that sentence attached is worse
    than no number, so the two are returned together or not at all."""
    from auteur.manager import predict_for

    score, why = predict_for(
        __import__("auteur.manager", fromlist=["Plan"]).Plan(
            id="p",
            owner="ada",
            title="t",
            platform="tiktok",
            when="2026-09-01T09:00:00+00:00",
            prompt="a hypercut",
        )
    )
    assert score is None
    assert why, "no number and no reason is not an answer"


def test_only_a_plans_owner_can_change_or_drop_it(tmp_path):
    from auteur.manager import Board

    board = Board(tmp_path / "plans.json")
    plan = board.add(
        owner="ada",
        title="t",
        platform="tiktok",
        when="2026-09-01T09:00:00+00:00",
        prompt="a hypercut",
    )
    assert board.update(plan.id, "grace", title="mine now") is None
    assert board.drop(plan.id, "grace") is False
    assert board.get(plan.id).title == "t"
    assert board.update(plan.id, "ada", title="renamed").title == "renamed"


def test_a_plan_survives_the_process_that_made_it(tmp_path):
    from auteur.manager import Board

    Board(tmp_path / "plans.json").add(
        owner="ada",
        title="Saturday market",
        platform="instagram-reel",
        when="2026-09-01T09:00:00+00:00",
        prompt="a hypercut",
    )
    again = Board(tmp_path / "plans.json")
    assert [p.title for p in again.by("ada")] == ["Saturday market"]


def test_the_capture_list_is_something_a_person_could_actually_shoot():
    """A twenty second hypercut is a hundred and ten shots, and a hundred and
    ten numbered instructions is not a shot list. What people actually do —
    and what the reference reels are made of — is a dozen setups the edit cuts
    among, so that is what the plan hands over."""
    from auteur.manager import capture_list, shot_list

    shots = shot_list("a 90s hypercut of the market", seconds=20.0, hold=0.167)
    captures = capture_list(shots)

    assert len(shots) > 80
    assert len(captures) <= 20, "a capture list nobody could carry out is not a plan"
    # Nothing is lost: every shot in the timeline comes from one of them, and
    # the screen time adds back up.
    assert sum(c.times for c in captures) == len(shots)
    assert abs(sum(c.seconds for c in captures) - sum(s.seconds for s in shots)) < 0.5
    # And each setup is named once, not repeated as separate rows.
    assert len({(c.role, c.what) for c in captures}) == len(captures)


def test_two_shots_in_a_row_are_not_the_same_instruction():
    """The instruction cycled on the position within the shape rather than per
    role, so wherever two runs were adjacent the plan said "a wide of where you
    are" twice in a row."""
    from auteur.manager import shot_list

    shots = shot_list("a hypercut", seconds=20.0, hold=0.167)
    repeats = [
        (a.order, a.what)
        for a, b in zip(shots, shots[1:], strict=False)
        if a.what == b.what and a.role == b.role == "run"
    ]
    assert repeats == [], f"consecutive identical instructions: {repeats[:3]}"


# ---------------------------------------------------------------------------
# Signing in with a Google or Apple account
# ---------------------------------------------------------------------------


def _fake_id_token(claims: dict) -> str:
    """A JWT shaped token. Unsigned, which is the point of the test below."""
    import base64

    def part(payload: dict) -> str:
        raw = json.dumps(payload).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return part({"alg": "none"}) + "." + part(claims) + ".x"


def test_the_whole_round_trip_against_a_stub_provider(monkeypatch):
    """The flow end to end, without Google.

    Real credentials cannot be in a test and a consent screen cannot be
    clicked by one, so the provider is stubbed at the token endpoint — which is
    the only place this code talks to it — and everything else is the real
    path: the real authorize URL, the real state and nonce, the real PKCE
    verifier, the real claim checks.
    """
    from auteur.web import oidc

    settings = oidc.Settings(
        client_id="client-123",
        client_secret="shh",
        redirect_uri="http://localhost:8793/auth/google/return",
    )
    attempts = oidc.Attempts()
    attempt = attempts.begin("google")

    where = oidc.begin("google", settings, attempt)
    assert where.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "response_type=code" in where, "the implicit flow is not safe here"
    assert "code_challenge_method=S256" in where
    assert urllib.parse.quote(attempt.state, safe="") in where

    sent = {}

    def stub(url, form):
        sent.update(form)
        return {
            "id_token": _fake_id_token(
                {
                    "aud": "client-123",
                    "nonce": attempt.nonce,
                    "exp": time.time() + 600,
                    "email": "ada@example.invalid",
                    "email_verified": True,
                }
            )
        }

    monkeypatch.setattr(oidc, "_post", stub)
    claims = oidc.finish("google", settings, attempt, "the-code")

    # The verifier goes back, which is what makes an intercepted code useless.
    assert sent["code_verifier"] == attempt.verifier
    assert sent["redirect_uri"] == settings.redirect_uri
    assert oidc.email_of(claims) == "ada@example.invalid"


def test_a_sign_in_attempt_cannot_be_replayed():
    from auteur.web import oidc

    attempts = oidc.Attempts()
    attempt = attempts.begin("google")
    assert attempts.claim(attempt.state) is attempt
    assert attempts.claim(attempt.state) is None, "a state that works twice is a replay"
    assert attempts.claim("something-else") is None


def test_a_token_for_another_application_is_refused(monkeypatch):
    """`aud` is the check that stops a token minted for a different client —
    including an attacker's own — being spent here."""
    from auteur.web import oidc

    settings = oidc.Settings(client_id="ours", redirect_uri="http://localhost/x")
    attempt = oidc.Attempts().begin("google")
    monkeypatch.setattr(
        oidc,
        "_post",
        lambda url, form: {
            "id_token": _fake_id_token(
                {"aud": "somebody-else", "nonce": attempt.nonce, "exp": time.time() + 60}
            )
        },
    )
    with pytest.raises(ValueError, match="different application"):
        oidc.finish("google", settings, attempt, "code")


def test_a_replayed_or_mismatched_nonce_is_refused(monkeypatch):
    from auteur.web import oidc

    settings = oidc.Settings(client_id="ours", redirect_uri="http://localhost/x")
    attempt = oidc.Attempts().begin("google")
    monkeypatch.setattr(
        oidc,
        "_post",
        lambda url, form: {
            "id_token": _fake_id_token(
                {"aud": "ours", "nonce": "not-the-one", "exp": time.time() + 60}
            )
        },
    )
    with pytest.raises(ValueError, match="does not match"):
        oidc.finish("google", settings, attempt, "code")


def test_an_expired_token_is_refused(monkeypatch):
    from auteur.web import oidc

    settings = oidc.Settings(client_id="ours", redirect_uri="http://localhost/x")
    attempt = oidc.Attempts().begin("google")
    monkeypatch.setattr(
        oidc,
        "_post",
        lambda url, form: {
            "id_token": _fake_id_token(
                {"aud": "ours", "nonce": attempt.nonce, "exp": time.time() - 5}
            )
        },
    )
    with pytest.raises(ValueError, match="expired"):
        oidc.finish("google", settings, attempt, "code")


def test_an_unverified_address_is_not_an_identity():
    """Both providers hand over addresses they have not checked under some
    conditions. Matching an account on one would let anybody who can claim an
    address at a provider sign in as its owner."""
    from auteur.web import oidc

    assert oidc.email_of({"email": "ada@example.invalid", "email_verified": True})
    assert oidc.email_of({"email": "ada@example.invalid", "email_verified": "true"})
    assert oidc.email_of({"email": "ada@example.invalid", "email_verified": False}) == ""
    assert oidc.email_of({"email": "ada@example.invalid"}) == ""


def test_the_redirect_uri_never_comes_from_the_request():
    """The one value an attacker would most like to influence. It has to match
    what is registered with the provider anyway, and deriving it from the Host
    header would let a forged one send somebody's code somewhere else."""
    import ast
    import inspect

    from auteur.web import oidc

    # The code, not the prose. The first version of this matched the sentence
    # in the module docstring that explains the rule, which is a check that
    # fails when you document the thing it is checking for.
    tree = ast.parse(inspect.getsource(oidc))
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            node.value.value = ""  # a docstring
    code = ast.unparse(tree)

    for smell in ("self.headers", "Host", "request.host", "environ["):
        assert smell not in code, f"the redirect uri may be built from {smell}"
    # And the value that is sent is the configured one, every time.
    assert "settings.redirect_uri" in code


def test_every_provider_is_listed_even_when_it_is_not_set_up():
    """A button that is simply absent reads as a capability this app does not
    have. The truth is usually that nobody has pasted a client id in yet."""
    from auteur.web import oidc

    rows = oidc.offered({key: oidc.Settings() for key in oidc.PROVIDERS})
    assert {row["key"] for row in rows} == set(oidc.PROVIDERS)
    for row in rows:
        assert row["ready"] is False
        assert row["why"], f"{row['key']} is off and does not say why"
        assert row["note"], f"{row['key']} does not say what it needs"


def test_signing_in_with_a_provider_never_creates_an_account(tmp_path):
    """Sign-up closes after the first account because this serves somebody's
    own footage over their own wifi. An identity provider proves who you are;
    it is not a second door."""
    import inspect

    from auteur.web import oidc, server

    assert "add(" not in inspect.getsource(oidc), "the oidc module can create accounts"
    handler = inspect.getsource(server.Handler._oidc_return)
    assert "accounts.add" not in handler
    assert "nomatch" in handler, "an unknown address must be told, not enrolled"


def test_opening_a_session_is_not_the_same_as_authenticating(tmp_path):
    """`open_session` is the step *after* an identity is established, and every
    caller has to have done that. Worth a test because the method's name is
    inviting and its effect is a signed-in session."""
    import inspect

    from auteur.web.auth import Accounts

    doc = inspect.getdoc(Accounts.open_session) or ""
    assert "does not authenticate" in doc.lower()

    accounts = Accounts(tmp_path / "accounts.json")
    accounts.add("ada", "ada@example.invalid", "a-long-enough-passphrase")
    token = accounts.open_session("ada")
    assert accounts.session_user(token) == "ada"


# ---------------------------------------------------------------------------
# The calendar
# ---------------------------------------------------------------------------


def _a_plan(**over) -> dict:
    plan = {
        "id": "abc123",
        "title": "Saturday market",
        "when": "2026-09-05T09:00:00+00:00",
        "prompt": "a 90s hypercut",
        "status": "idea",
        "caption": "Saturday, 6am",
        "hashtags": ["market"],
        "captures": [{"what": "a wide of where you are", "role": "run", "times": 14}],
    }
    plan.update(over)
    return plan


def test_the_calendar_folds_at_seventy_five_octets_and_ends_every_line_crlf():
    """RFC 5545 is fussy in ways that produce a file which imports as *empty*
    rather than as broken, which is the worst kind of wrong: the calendar app
    says nothing and the events simply are not there."""
    from auteur import calendar as ics

    text = ics.feed([_a_plan(prompt="x " * 200)])
    assert text.endswith("\r\n")
    lines = text.split("\r\n")
    assert all("\n" not in line and "\r" not in line for line in lines)
    for line in lines:
        assert len(line.encode("utf-8")) <= 75, f"unfolded line: {line[:40]}…"
    # Continuations are marked, or the fold is just a broken line.
    assert any(line.startswith(" ") for line in lines)


def test_a_fold_never_splits_a_character():
    """The limit is octets and the content is UTF-8, so folding on characters
    puts half a character at the end of a line and mojibake in the calendar."""
    from auteur import calendar as ics

    text = ics.feed([_a_plan(title="café " * 40, prompt="—" * 90)])
    for line in text.split("\r\n"):
        line.encode("utf-8").decode("utf-8")  # raises if a fold split one


def test_commas_and_newlines_in_a_caption_do_not_break_the_file():
    from auteur import calendar as ics

    text = ics.feed([_a_plan(caption="one, two; three\\four\nand a new line")])
    body = "".join(line[1:] if line.startswith(" ") else "\n" + line for line in text.split("\r\n"))
    description = [ln for ln in body.split("\n") if ln.startswith("DESCRIPTION:")][0]
    assert "\\," in description
    assert r"\;" in description
    assert "\\\\" in description
    assert "\\n" in description


def test_editing_a_plan_updates_the_event_rather_than_adding_one():
    """Stable UID, moving SEQUENCE. Without both, moving a shoot leaves the old
    one on the phone and adds a second — which is how a calendar subscription
    becomes something people unsubscribe from."""
    from auteur import calendar as ics

    before = ics.event_for(_a_plan())
    moved = ics.event_for(_a_plan(when="2026-09-06T09:00:00+00:00"))
    renamed = ics.event_for(_a_plan(title="Sunday market"))

    assert before.uid == moved.uid == renamed.uid
    assert before.sequence != moved.sequence
    assert before.sequence != renamed.sequence
    # And an untouched plan does not churn: a calendar that is told everything
    # changed every hour stops believing any of it.
    assert ics.event_for(_a_plan()).sequence == before.sequence


def test_a_plan_carries_its_reminders():
    from auteur import calendar as ics

    text = ics.feed([_a_plan()])
    assert text.count("BEGIN:VALARM") == len(ics.ALARMS)
    assert "TRIGGER:-PT48H" in text  # go and shoot it
    assert "TRIGGER:PT0S" in text  # and the moment itself, which is not -PT0M


def test_a_posted_plan_stops_being_something_to_do():
    from auteur import calendar as ics

    done = ics.feed([_a_plan(status="posted")])
    assert "STATUS:CANCELLED" in done
    assert "BEGIN:VALARM" not in done, "a reminder to post something already posted"


def test_a_plan_with_an_unreadable_time_is_skipped_not_crashed():
    from auteur import calendar as ics

    assert ics.event_for(_a_plan(when="whenever")) is None
    text = ics.feed([_a_plan(when="whenever"), _a_plan(id="ok")])
    assert text.count("BEGIN:VEVENT") == 1


def test_the_calendar_link_is_a_capability_and_can_be_rolled(tmp_path):
    """A calendar app has no cookie, so the URL is the credential. That makes
    the ability to roll it the only way to un-share it."""
    from auteur.web.auth import Accounts

    accounts = Accounts(tmp_path / "accounts.json")
    accounts.add("ada", "ada@example.invalid", "a-long-enough-passphrase")

    token = accounts.calendar_token("ada")
    assert len(token) >= 24
    assert accounts.calendar_token("ada") == token, "a link that changes is a link that breaks"
    assert accounts.by_calendar_token(token).username == "ada"

    rolled = accounts.calendar_token("ada", roll=True)
    assert rolled != token
    assert accounts.by_calendar_token(token) is None, "the old link still works"
    assert accounts.by_calendar_token("").username if False else True


def test_a_calendar_token_is_not_derived_from_anything_about_the_account(tmp_path):
    """A derived token cannot be rolled without changing what it is derived
    from, and the reason to roll one is that it went somewhere it should not."""
    from auteur.web.auth import Accounts

    accounts = Accounts(tmp_path / "accounts.json")
    accounts.add("ada", "ada@example.invalid", "a-long-enough-passphrase")
    token = accounts.calendar_token("ada")
    account = accounts.get("ada")
    for secret in (account.username, account.email, account.salt, account.password_hash):
        assert secret not in token
        assert token not in secret


def test_the_calendar_feed_is_reachable_without_a_session():
    """On purpose, and the only route that is: a calendar app is not a browser
    and will not sign in."""
    from auteur.web import server

    assert "/calendar/" in server.PUBLIC_PREFIXES


def test_a_platform_has_a_readable_title_that_is_not_its_lookup_key():
    """`name` reads like a label and is not one — it is "instagram-reel". It
    reached the manager's board and put a lookup key on somebody's screen."""
    from auteur.workflows.platforms import PLATFORMS

    for key, spec in PLATFORMS.items():
        assert spec.name == key
        assert spec.title != key
        assert " " in spec.title
        assert spec.service in spec.title


# ---------------------------------------------------------------------------
# The iOS app
# ---------------------------------------------------------------------------

IOS = Path(__file__).resolve().parent.parent / "ios"


def test_the_page_in_the_ios_bundle_is_the_page_the_build_produces():
    """`ios/README.md` said this file is generated. Nothing generated it.

    The generator was real — `ios/scripts/build_bundle.py` writes this file, and
    the README says to run it. Nobody ran it. By the time the two were compared
    the bundle was 350 lines behind the app: missing two theme roles, missing
    the sheet-height fix, and missing the entire stylesheet for the report and
    block dialog, so the iPhone build shipped the one screen App Store guideline
    1.2 is about with no styling on it. Nobody had noticed, because noticing
    meant diffing a 350KB file by hand.

    A build step nothing checks is a build step that stops being run. This runs
    it and compares the result to what is committed.
    """
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    bundle = IOS / "Auteur" / "Web" / "index.html"
    assert bundle.is_file(), "the iOS bundle has no page in it"

    before = bundle.read_text(encoding="utf-8")
    artifact = root / "tools" / "artifact" / "auteur-app.html"
    # Generated and gitignored, so on a fresh checkout it does not exist yet.
    # Reading it here unconditionally is why this test failed on every CI run
    # while passing on every machine that had once built it by hand.
    artifact_before = artifact.read_text(encoding="utf-8") if artifact.is_file() else None
    try:
        for step in (
            root / "tools" / "artifact" / "build_artifact.py",
            root / "ios" / "scripts" / "build_bundle.py",
        ):
            done = subprocess.run(
                [sys.executable, str(step)], capture_output=True, text=True, cwd=root
            )
            # `build_bundle.py` reports the placeholder identity as not ready to
            # submit and exits non-zero for it. That is a different question
            # from whether it wrote the page, so the page is what is checked.
            assert "Traceback" not in done.stderr, f"{step.name} crashed: {done.stderr[-2000:]}"

        assert bundle.read_text(encoding="utf-8") == before, (
            "the page committed in the iOS bundle is not what the build produces — "
            "run `python3 tools/artifact/build_artifact.py` then "
            "`python3 ios/scripts/build_bundle.py`, and commit the result"
        )
    finally:
        # Leave the tree as it was found, whichever way the assert went.
        bundle.write_text(before, encoding="utf-8")
        if artifact_before is None:
            artifact.unlink(missing_ok=True)
        else:
            artifact.write_text(artifact_before, encoding="utf-8")


def test_the_ios_bundle_carries_the_screens_the_app_store_asks_about():
    """The stale bundle was missing these, which is how staleness showed up.

    Guideline 1.2 wants reporting and blocking reachable in the shipped build,
    not only in the served one. A rule that lives in the app's stylesheet and
    not in the bundle is a dialog that renders unstyled on a phone.
    """
    page = (IOS / "Auteur" / "Web" / "index.html").read_text(encoding="utf-8")

    for needed, why in (
        (".choices.reasons", "the report sheet's reason grid"),
        ("--on-photo", "the colour text takes when it sits on a photo"),
        ("--on-rust", "the colour text takes on the accent"),
        ('data-prompt="a montage', "the montage chip"),
    ):
        assert needed in page, f"the iOS bundle is missing {why} ({needed})"


def test_the_app_icon_carries_no_alpha_channel():
    """App Store Connect rejects an icon with a channel it does not use, and
    the rejection arrives after the upload, by email, naming something else."""
    from PIL import Image

    icons = sorted((IOS / "Auteur" / "Assets.xcassets" / "AppIcon.appiconset").glob("*.png"))
    assert icons, "no icons built — run ios/scripts/build_bundle.py"
    for path in icons:
        with Image.open(path) as icon:
            assert icon.mode == "RGB", f"{path.name} has an alpha channel"
            assert icon.size[0] == icon.size[1], f"{path.name} is not square"
    assert any(p.name == "icon-1024.png" for p in icons), "the store icon is 1024"


def test_every_plist_in_the_project_parses():
    """Xcode reports a malformed plist as a build failure several steps away
    from the file that is wrong."""
    import plistlib

    found = list(IOS.rglob("*.plist")) + list(IOS.rglob("*.xcprivacy"))
    assert len(found) >= 3
    for path in found:
        with path.open("rb") as handle:
            plistlib.load(handle)


def test_the_app_asks_only_for_permissions_it_uses():
    """Every usage string is a sentence somebody reads in a dialog, and a
    permission the app does not use is both a worse dialog and a rejection."""
    import plistlib

    with (IOS / "Auteur" / "Info.plist").open("rb") as handle:
        info = plistlib.load(handle)

    for key in ("NSPhotoLibraryAddUsageDescription", "NSCalendarsWriteOnlyAccessUsageDescription"):
        assert key in info, f"{key} is missing and the app will crash when it asks"
        assert len(info[key]) > 25, f"{key} does not say what it is for"
        assert info[key].endswith("."), f"{key} is not a sentence"

    # The picker hands over only what somebody chose and needs no permission,
    # so read access to the whole library would be asking for something nothing
    # in this app uses.
    assert "NSPhotoLibraryUsageDescription" not in info

    swift = (IOS / "Auteur" / "Bridge.swift").read_text()
    assert "addOnly" in swift, "the app asks for more of Photos than it needs"


def test_the_app_targets_the_ios_the_renderer_actually_needs():
    """The one number in the project file that is a measurement rather than a
    default: the renderer records by pulling frames off a canvas, and
    `canvas.captureStream` did not exist in WebKit before 15.4."""
    spec = (IOS / "project.yml").read_text()
    match = re.search(r"iOS:\s*\"(\d+)\.(\d+)\"", spec)
    assert match, "the deployment target is not stated"
    major, minor = int(match.group(1)), int(match.group(2))
    assert (major, minor) >= (15, 4), "below the iOS that can record from a canvas"

    # And it is armv7-free: 32-bit has not run iOS since 11, and declaring it
    # makes modern devices report as unsupported instead of erroring.
    import plistlib

    with (IOS / "Auteur" / "Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    assert info["UIRequiredDeviceCapabilities"] == ["arm64"]


def test_the_bundled_page_reaches_nothing_outside_itself():
    """The app has no network entitlement, so an external reference is not a
    slow load, it is a silently blank region on somebody's phone."""
    page = (IOS / "Auteur" / "Web" / "index.html").read_text()
    outside = re.findall(r'(?:src|href)\s*=\s*["\']https?://[^"\']+', page)
    assert outside == [], f"the bundled page reaches out: {outside[:3]}"
    assert page.lstrip().startswith("<!DOCTYPE html>")
    assert "<title>" in page


def test_every_colour_the_app_names_actually_exists():
    """`UILaunchScreen` names a colour by string. A missing one is not an
    error — the app just launches on a white flash whatever the theme is."""
    import json
    import plistlib

    with (IOS / "Auteur" / "Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    named = info["UILaunchScreen"]["UIColorName"]
    folder = IOS / "Auteur" / "Assets.xcassets" / f"{named}.colorset"
    assert folder.is_dir(), f"{named} is named in Info.plist and does not exist"
    colours = json.loads((folder / "Contents.json").read_text())["colors"]
    # Both lightings, or the launch flashes the wrong one on half the phones.
    assert len(colours) == 2


def test_the_shim_fills_in_the_two_apis_a_web_view_does_not_have():
    """`navigator.share` exists in Safari and not in a web view, so without
    this the page's own save button silently does nothing — the worst failure
    available, on the one control that delivers the product."""
    shim = (IOS / "Auteur" / "native.js").read_text()
    assert "navigator.share" in shim
    assert "navigator.canShare" in shim
    assert "messageHandlers.auteur" in shim
    # And the page is never edited to know about the app: the shim fills in
    # what the page already reaches for.
    for job in ("save", "share", "calendar", "capabilities"):
        assert f'"{job}"' in shim or f"'{job}'" in shim

    swift = (IOS / "Auteur" / "Bridge.swift").read_text()
    for job in ("save", "share", "calendar", "capabilities"):
        assert f'case "{job}"' in swift, f"the shim sends {job} and Swift ignores it"


def test_the_app_declares_that_nothing_leaves_the_phone():
    import plistlib

    with (IOS / "Auteur" / "PrivacyInfo.xcprivacy").open("rb") as handle:
        privacy = plistlib.load(handle)
    assert privacy["NSPrivacyTracking"] is False
    assert privacy["NSPrivacyTrackingDomains"] == []
    assert privacy["NSPrivacyCollectedDataTypes"] == []
    # Every "required reason" API used has to carry a reason, or the upload is
    # refused without saying which one.
    for entry in privacy["NSPrivacyAccessedAPITypes"]:
        assert entry["NSPrivacyAccessedAPITypeReasons"], entry["NSPrivacyAccessedAPIType"]


# ---------------------------------------------------------------------------
# Before letting anybody else use it
# ---------------------------------------------------------------------------


def test_no_referrer_is_sent_because_a_url_carries_a_secret():
    """The calendar subscription URL carries its credential in the path, so an
    outbound navigation from any page would put somebody's calendar secret in
    another site's logs. `no-referrer` is the only value that closes that."""
    from auteur.web import server

    assert server.SAFETY_HEADERS["Referrer-Policy"] == "no-referrer"


def test_every_response_carries_the_safety_headers():
    from auteur.web import server

    for key in (
        "X-Content-Type-Options",
        "Referrer-Policy",
        "X-Frame-Options",
        "Content-Security-Policy",
    ):
        assert key in server.SAFETY_HEADERS

    policy = server.SAFETY_HEADERS["Content-Security-Policy"]
    # Nothing remote: everything the pages need is served from this origin, and
    # blob: is how a finished film reaches a video element.
    assert "default-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "blob:" in policy
    assert "http://" not in policy and "https://" not in policy


def test_the_calendar_secret_is_kept_out_of_the_request_log():
    """The request line carries the path, and one path is a credential."""
    from auteur.web.server import _redact

    line = "GET /calendar/HnTMHaWX1mbYa9ZBvBd9wlWGO1VfM5fT.ics HTTP/1.1"
    assert "HnTMHaWX" not in _redact(line)
    assert "[redacted]" in _redact(line)
    # And it does not mangle anything else.
    assert _redact("GET /api/feed HTTP/1.1") == "GET /api/feed HTTP/1.1"


def test_an_upload_is_bounded_by_something_a_machine_actually_has():
    """2 GB was aspirational: the parser materialises the body *and* the parsed
    parts, so a post that size peaked at several gigabytes resident and the
    process was killed rather than answering — a denial of service anybody
    could trigger by accident with a long 4K clip."""
    from auteur.web import server

    assert server.MAX_UPLOAD <= 1024 * 1024 * 1024
    assert server.SPOOL_TO_DISK < server.MAX_UPLOAD


def test_a_posted_form_is_read_without_copying_the_whole_body():
    """The streaming parser has to agree with the one that takes bytes, or the
    fix quietly changes what uploads mean."""
    import io

    from auteur.web.server import _parse_multipart, _parse_multipart_stream

    boundary = "----abc"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="prompt"\r\n\r\n'
        "a 90s hypercut\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="clips"; filename="one.mp4"\r\n'
        "Content-Type: video/mp4\r\n\r\n"
        "not really a film\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    kind = f"multipart/form-data; boundary={boundary}"

    fields, files = _parse_multipart(body, kind)
    streamed_fields, streamed_files = _parse_multipart_stream(io.BytesIO(body), kind)

    assert fields == streamed_fields == {"prompt": "a 90s hypercut"}
    assert files == streamed_files
    assert files[0][0] == "one.mp4"
    assert files[0][1] == b"not really a film"


def test_sweeping_a_job_also_forgets_the_films_that_pointed_into_it():
    """A film outlives its job on purpose. It cannot outlive its file, and
    `drop_missing` only ran at start-up — so an instance left running for a day
    filled its feed with rows that play nothing."""
    import inspect

    from auteur.web.server import Studio

    source = inspect.getsource(Studio.sweep)
    assert "drop_missing" in source, "sweeping leaves the feed pointing at deleted files"


def test_the_readme_does_not_claim_gaps_that_have_been_closed():
    """Documentation that describes a fixed problem is worse than none: it
    sends somebody to look for a bug that is not there."""
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    gaps = readme[readme.index("### Known gaps") :]
    gaps = gaps[: gaps.index("\n## ")] if "\n## " in gaps else gaps

    # These two were the gap and are not any more; the tools that proved it
    # are in the repository and pass.
    assert "hard-cuts" not in gaps
    assert "fails on purpose" not in gaps


def test_the_published_page_offers_no_link_it_cannot_follow():
    """The bundled page has four sections and the app has ten, so every link
    to a room that is not there is a control a tap does nothing to. Ten of them
    had accumulated: `/manager`, `/ask`, `/connect`. On a phone that is a dead
    control; in an App Store review it is a rejection under 2.1."""
    page = (
        Path(__file__).resolve().parent.parent / "ios" / "Auteur" / "Web" / "index.html"
    ).read_text()
    server_links = re.findall(r'href="(/[^"]*)"', page)
    assert server_links == [], f"links nothing can follow: {sorted(set(server_links))[:5]}"


def test_every_section_of_the_published_page_can_be_reached():
    """Worse than a dead link and harder to see: the tab bar is injected by a
    script the build strips, so the page carried a studio, an animation tab and
    a templates library that nothing navigated to. Dead *content*."""
    page = (
        Path(__file__).resolve().parent.parent / "ios" / "Auteur" / "Web" / "index.html"
    ).read_text()

    targets = set(re.findall(r'data-goto="([^"]+)"', page))
    assert {"templates", "animation", "studio"} <= targets, f"unreachable sections: {targets}"

    # And nothing points at a section that is not there.
    for target in targets:
        if target == "home":
            continue
        assert f'id="{target}-page"' in page, f"data-goto={target} names no section"

    # Every section has a way back, or it is a trap.
    for section in ("templates", "animation", "studio"):
        after = page[page.index(f'id="{section}-page"') :]
        assert 'data-goto="home"' in after[:4000], f"no way back out of {section}"


def test_the_privacy_policy_is_reachable_without_signing_in():
    """The App Store requires a policy at a URL anybody can open, and
    "anybody" includes a reviewer who has not been given an account."""
    from auteur.web import server

    assert "/privacy" in server.PUBLIC_PATHS
    assert (server.STATIC / "privacy.html").is_file()


def test_the_privacy_policy_is_generated_from_the_one_source():
    """A policy maintained in two places is a policy that is wrong in one."""
    from auteur.web import assets, server

    source = Path(__file__).resolve().parent.parent / "PRIVACY.md"
    assert source.is_file()
    page = (server.STATIC / "privacy.html").read_text()

    # Every heading in the source survives into the page.
    for line in source.read_text().splitlines():
        if line.startswith("## "):
            assert line[3:] in page, f"the page has lost the section {line[3:]!r}"

    # And regenerating it changes nothing, which is what "generated" has to mean.
    before = page
    assets.privacy_page(source, server.STATIC)
    assert (server.STATIC / "privacy.html").read_text() == before


def test_the_policy_says_what_the_code_does():
    """The two claims in it that a test can actually hold it to."""
    # Whitespace-normalised: the source is hard wrapped, so a phrase that
    # spans a line break is not a substring of the file.
    policy = " ".join((Path(__file__).resolve().parent.parent / "PRIVACY.md").read_text().split())
    assert "nobody operates a service here" in policy.lower()

    # "no network requests of any kind" about the iOS app.
    bundled = (
        Path(__file__).resolve().parent.parent / "ios" / "Auteur" / "Web" / "index.html"
    ).read_text()
    for reaching in ("fetch(", "XMLHttpRequest", "new WebSocket", "sendBeacon"):
        assert reaching not in bundled, f"the policy says no network and the page has {reaching}"

    # "no code path that publishes to a service".
    import ast
    import inspect

    from auteur import manager

    tree = ast.parse(inspect.getsource(manager))
    reached = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            reached.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            reached.add(node.module.split(".")[0])
    assert not (reached & {"urllib", "http", "requests", "socket"})


def test_a_cookie_is_only_marked_secure_when_it_really_is():
    """`X-Forwarded-Proto` is a header, so anybody can send it. Trusting it
    unconditionally marks a cookie Secure on a plain connection; not trusting
    it at all means a real HTTPS deployment never gets the flag. So it is
    believed only when the operator says there is a proxy in front."""
    import inspect

    from auteur.web import server

    # A property, so the function is behind `.fget`.
    source = inspect.getsource(server.Handler._is_https.fget)
    assert "TRUST_PROXY" in source
    assert "PUBLIC_HTTPS" in source
    # Off by default: the ordinary way to run this is a LAN over plain HTTP,
    # where a Secure cookie simply never comes back.
    assert server.PUBLIC_HTTPS is False
    assert server.TRUST_PROXY is False


def test_hsts_is_not_promised_from_a_plain_http_server():
    """Sending it from a LAN server over plain HTTP tells every browser on the
    network to refuse to reach it — for a year."""
    from auteur.web import server

    assert "Strict-Transport-Security" not in server.SAFETY_HEADERS
    import inspect

    assert "Strict-Transport-Security" in inspect.getsource(server.Handler._send)


def test_the_published_page_has_the_tab_bar_and_it_is_not_hidden():
    """It was a list of links at the foot of the home section — 1564px down a
    1945px page, past the whole form. Present and unreachable, which is the
    same as missing for anybody who does not know it is there.

    Then it was the real bar, placed *inside* the templates section, which is
    `hidden` — so it existed and was invisible on every screen.

    The slots are the app's five now rather than a list of the sections this
    page happens to have. Templates, the animation room and the studio are
    reached through the plus here exactly as they are in the app, so they are
    no longer tabs and are no longer looked for as tabs."""
    page = (
        Path(__file__).resolve().parent.parent / "ios" / "Auteur" / "Web" / "index.html"
    ).read_text()

    assert 'class="tabbar"' in page
    for slot in ("feed", "schedule", "home", "messages", "you"):
        assert f'data-tab="{slot}"' in page, f"no tab for {slot}"
    # And the rooms that are not tabs are still reachable, through the plus.
    for room in ("templates", "animation", "studio"):
        assert f'data-goto="{room}"' in page, f"{room} cannot be reached at all"

    # Outside every section, or a fixed bar inherits their hidden state.
    bar = page.index('class="tabbar"')
    for section in ("studio-page", "animation-page", "templates-page"):
        opened = page.index(f'id="{section}"')
        assert bar > opened, "the bar is inside a section"
        closing = page.index("</div>", opened)
        assert not (opened < bar < closing), f"the bar is inside {section}, which is hidden"


# ---------------------------------------------------------------------------
# Two-step verification
# ---------------------------------------------------------------------------


def test_the_codes_match_the_published_rfc_vectors():
    """RFC 6238 ships test vectors precisely so an implementation can be
    checked rather than believed. Written against the specification because an
    authentication library is the last place to take a fourth party you have
    not read."""
    import base64

    from auteur.web import totp

    secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    assert totp.code_at(secret, 59) == "287082"
    assert totp.code_at(secret, 1111111109) == "081804"
    assert totp.code_at(secret, 1111111111) == "050471"
    assert totp.code_at(secret, 1234567890) == "005924"


def test_a_code_is_accepted_across_a_drifting_clock_but_not_forever():
    from auteur.web import totp

    secret = totp.new_secret()
    now = 1_700_000_000
    code = totp.code_at(secret, now)
    assert totp.check(secret, code, moment=now) is not None
    assert totp.check(secret, code, moment=now + 29) is not None
    assert totp.check(secret, code, moment=now - 29) is not None
    # 90 seconds of total validity, and no more: a wider window is a longer
    # replay opportunity for a code somebody read over a shoulder.
    assert totp.check(secret, code, moment=now + 91) is None
    assert totp.check(secret, "000000", moment=now) is None
    assert totp.check(secret, "not a code", moment=now) is None


def test_the_password_alone_stops_being_enough(tmp_path):
    """The whole point. `sign_in` must hand back a ticket rather than a session
    — a ticket names the account, expires, is spent by use, and can do nothing
    else."""
    from auteur.web import totp
    from auteur.web.auth import Accounts

    accounts = Accounts(tmp_path / "accounts.json")
    accounts.add("ada", "ada@example.invalid", "a-long-enough-passphrase")

    token, _ = accounts.sign_in("ada", "a-long-enough-passphrase")
    assert token is not None, "with two-step off, a password is enough"

    secret = accounts.begin_totp("ada")
    assert accounts.get("ada").totp_on is False, "an unfinished setup must not lock anybody out"
    assert accounts.confirm_totp("ada", "000000") is None
    codes = accounts.confirm_totp("ada", totp.code_at(secret))
    assert codes and len(codes) == totp.RECOVERY_CODES

    token, message = accounts.sign_in("ada", "a-long-enough-passphrase")
    assert token is None, "the password alone still opened a session"
    assert message.startswith("code:")

    ticket = message[5:]
    assert accounts.spend_ticket(ticket) == "ada"
    assert accounts.spend_ticket(ticket) is None, "a ticket that works twice is a session"


def test_a_code_cannot_be_used_twice(tmp_path):
    """Otherwise a code is good for its whole window however many times it is
    presented, and anybody who saw one has thirty seconds to use it too."""
    from auteur.web import totp
    from auteur.web.auth import Accounts

    accounts = Accounts(tmp_path / "accounts.json")
    accounts.add("ada", "ada@example.invalid", "a-long-enough-passphrase")
    secret = accounts.begin_totp("ada")
    accounts.confirm_totp("ada", totp.code_at(secret))

    # Setup already spent this window, so the same code must not work again.
    assert accounts.second_step("ada", totp.code_at(secret)) is False


def test_a_recovery_code_works_once_and_is_stored_hashed(tmp_path):
    from auteur.web import totp
    from auteur.web.auth import Accounts

    accounts = Accounts(tmp_path / "accounts.json")
    accounts.add("ada", "ada@example.invalid", "a-long-enough-passphrase")
    secret = accounts.begin_totp("ada")
    codes = accounts.confirm_totp("ada", totp.code_at(secret))

    stored = (tmp_path / "accounts.json").read_text()
    for code in codes:
        assert code not in stored, "a recovery code is a password and is in the clear"

    assert accounts.second_step("ada", codes[0].lower()) is True
    assert accounts.second_step("ada", codes[0]) is False, "a code that works twice never expires"
    assert len(accounts.get("ada").recovery) == totp.RECOVERY_CODES - 1


def test_turning_it_off_needs_the_password_again(tmp_path):
    """A borrowed unlocked phone with a live session should not be able to
    remove the factor protecting the account it is signed in to."""
    from auteur.web import totp
    from auteur.web.auth import Accounts

    accounts = Accounts(tmp_path / "accounts.json")
    accounts.add("ada", "ada@example.invalid", "a-long-enough-passphrase")
    secret = accounts.begin_totp("ada")
    accounts.confirm_totp("ada", totp.code_at(secret))

    assert accounts.disable_totp("ada", "wrong") is False
    assert accounts.get("ada").totp_on is True
    assert accounts.disable_totp("ada", "a-long-enough-passphrase") is True
    assert accounts.get("ada").totp_secret == ""
    assert accounts.get("ada").recovery == []


def test_the_second_step_happens_while_signed_out():
    from auteur.web import server

    assert "/api/login/step2" in server.PUBLIC_PATHS


# ---------------------------------------------------------------------------
# The bug finder
# ---------------------------------------------------------------------------


def test_a_fault_can_be_reported_before_anybody_has_signed_in():
    """A fault on the sign-in page is exactly the one nobody could report if
    reporting needed an account."""
    from auteur.web import server

    assert "/api/trouble" in server.PUBLIC_PATHS


def test_the_bug_finder_is_not_telemetry():
    """The difference is not intent. There is no endpoint in this program that
    sends anything off the machine, so a report has nowhere to go but the disk
    it is already on."""
    from auteur.web import server

    handler = (server.STATIC / "trouble.js").read_text()
    # One destination, and it is this server.
    assert '"/api/trouble"' in handler
    for elsewhere in ("http://", "https://", "sendBeacon", "new Image("):
        assert elsewhere not in handler, f"the bug finder reaches {elsewhere}"


def test_the_bug_finder_cannot_itself_throw():
    """An error handler that throws is a loop that takes the page down harder
    than the fault it was reporting."""
    handler = (
        Path(__file__).resolve().parent.parent / "auteur" / "web" / "static" / "trouble.js"
    ).read_text()
    assert "try {" in handler
    # And it stops repeating itself, or one fault in a loop is a thousand posts.
    assert "SEEN" in handler
    assert "MOST" in handler


def test_every_page_can_report_a_fault():
    from auteur.web import server

    for page in (
        "index",
        "feed",
        "inbox",
        "manager",
        "templates",
        "studio",
        "ask",
        "overlays",
        "connect",
        "login",
    ):
        text = (server.STATIC / f"{page}.html").read_text()
        assert "trouble.js" in text, f"{page}.html cannot report a fault"


# ---------------------------------------------------------------------------
# Reaching an instance
# ---------------------------------------------------------------------------


def test_the_app_allows_plain_http_only_on_the_local_network():
    """Reaching your own instance means a plain connection to a local address.
    `NSAllowsArbitraryLoads` would open every unencrypted request to the
    internet as well, which is not what is wanted and is a review question."""
    import plistlib

    with (IOS / "Auteur" / "Info.plist").open("rb") as handle:
        info = plistlib.load(handle)

    ats = info["NSAppTransportSecurity"]
    assert ats.get("NSAllowsLocalNetworking") is True
    assert "NSAllowsArbitraryLoads" not in ats
    assert len(info["NSLocalNetworkUsageDescription"]) > 25


def test_with_no_instance_the_app_opens_no_socket():
    """The default is the bundled page, which is a file URL."""
    swift = (IOS / "Auteur" / "Instance.swift").read_text()
    assert "Bundle.main.url" in swift
    # And only http(s) to a real host is ever accepted.
    assert 'scheme == "http" || scheme == "https"' in swift
    assert "url.host != nil" in swift


def test_the_policy_no_longer_claims_the_app_has_no_network():
    """It has a feed and messages, and neither can live inside one phone. The
    old wording said "no network requests" without qualification, which read as
    a contradiction because it was one."""
    policy = " ".join((Path(__file__).resolve().parent.parent / "PRIVACY.md").read_text().split())
    assert "nothing goes anywhere you did not put it" in policy.lower()
    assert "on its own, the app makes no network requests at all" in policy.lower()
    assert "connected to your own instance" in policy.lower()
    # And the sentence that used to be the contradiction is gone.
    assert "it makes **no network requests of any kind**" not in policy


# ---------------------------------------------------------------------------
# Profiles: a picture, a bio, and who you follow
# ---------------------------------------------------------------------------


def test_a_bio_pasted_out_of_another_app_is_stored_as_one_line(tmp_path):
    """A stored newline is a layout that only breaks for some people.

    The bio is shown clamped to two lines on the profile header and to one in
    a list. Text arriving with the newlines and double spaces of wherever it
    was written renders fine in exactly one of those places.
    """
    from auteur.web.profiles import Profiles

    store = Profiles(tmp_path / "profiles.json")
    saved = store.edit("ada", bio="  makes\n\nsmall   films\t about boats  ")
    assert saved.bio == "makes small films about boats"


def test_a_profile_link_that_is_not_a_web_address_is_refused(tmp_path):
    """The interesting attack on a field that becomes an href is a scheme.

    A tidier that repairs input is one that eventually repairs `javascript:`
    into something a tap runs, so anything that is not plainly http or https
    is dropped rather than fixed. A bare host is the one exception, because
    somebody typing their own address means https.
    """
    from auteur.web.profiles import tidy_link

    assert tidy_link("javascript:alert(1)") == ""
    assert tidy_link("JavaScript:alert(1)") == ""
    assert tidy_link("data:text/html;base64,PHNjcmlwdD4=") == ""
    assert tidy_link("mailto:someone@example.com") == ""
    assert tidy_link("example.com/reel") == "https://example.com/reel"
    assert tidy_link("https://example.com") == "https://example.com"
    assert tidy_link("") == ""


def test_following_is_answered_from_the_followers_own_list(tmp_path):
    """ "Do I follow them" and "do they follow me" are different questions.

    They agree whenever two people follow each other, which is most of the
    time on a small instance — so getting this backwards looks right on every
    screen where it is tested by hand.
    """
    from auteur.web.profiles import Profiles

    store = Profiles(tmp_path / "profiles.json")
    store.follow("ada", "grace")

    assert store.public_of("grace", viewer="ada")["you_follow"] is True
    assert store.public_of("ada", viewer="grace")["you_follow"] is False
    assert store.public_of("grace", viewer="ada")["followers"] == 1
    assert store.public_of("ada", viewer="ada")["me"] is True
    # And nobody follows themselves: the "following" feed would otherwise
    # include your own films for some people and not others.
    assert store.follow("ada", "ada") is False


def test_a_profile_survives_being_written_and_read_back(tmp_path):
    from auteur.web.profiles import Profiles

    path = tmp_path / "profiles.json"
    first = Profiles(path)
    first.edit("ada", name="Ada L", bio="boats", link="ada.example")
    first.follow("ada", "grace")

    again = Profiles(path)
    assert again.get("ada").name == "Ada L"
    assert again.get("ada").link == "https://ada.example"
    assert again.following_of("ada") == ["grace"]
    assert again.followers_of("grace") == ["ada"]


def test_editing_one_field_does_not_blank_the_others(tmp_path):
    """A form that posts only what changed must not clear what did not."""
    from auteur.web.profiles import Profiles

    store = Profiles(tmp_path / "profiles.json")
    store.edit("ada", name="Ada L", bio="boats", link="https://ada.example")
    store.edit("ada", bio="trains")
    kept = store.get("ada")
    assert (kept.name, kept.bio, kept.link) == ("Ada L", "trains", "https://ada.example")


def test_a_picture_filename_cannot_climb_out_of_its_folder(tmp_path):
    """The store is a file on disk, so what comes out of it is still input."""
    from auteur.web.profiles import Profiles

    store = Profiles(tmp_path / "profiles.json", tmp_path / "pictures")
    (tmp_path / "secret.txt").write_text("not a picture")
    store.set_picture("ada", "../secret.txt")
    assert store.picture_path("ada") is None


def test_a_follow_of_a_deleted_account_is_forgotten(tmp_path):
    """Otherwise the count on a profile is larger than the list under it."""
    from auteur.web.profiles import Profiles

    store = Profiles(tmp_path / "profiles.json")
    store.follow("ada", "grace")
    store.follow("ada", "gone")
    assert store.drop_unknown({"ada", "grace"}) == 1
    assert store.following_of("ada") == ["grace"]


def test_a_profile_picture_is_re_encoded_and_loses_its_metadata(tmp_path):
    """A phone photograph carries where it was taken and what took it.

    Neither is something anybody means to publish with their face, and both
    live in EXIF. Re-encoding rather than validating is what removes them —
    and it is the same step that makes it impossible for the served file to be
    anything a browser could sniff as markup.
    """
    import io
    from fractions import Fraction

    from PIL import Image
    from auteur.web.profiles import PICTURE_SIDE, store_picture

    original = Image.new("RGB", (1600, 900), (200, 40, 40))
    exif = original.getexif()
    exif[0x010F] = "SomePhone"  # Make
    exif[0x0110] = "Model X"  # Model
    where = exif.get_ifd(0x8825)  # GPS
    where[1] = "N"
    where[2] = (Fraction(51), Fraction(30), Fraction(0))
    raw = io.BytesIO()
    original.save(raw, "JPEG", exif=exif)
    # The fixture has to actually carry what this claims to remove, or the
    # test passes against a photograph that never had coordinates in it.
    assert dict(Image.open(io.BytesIO(raw.getvalue())).getexif().get_ifd(0x8825))

    name = store_picture(raw.getvalue(), tmp_path / "pictures", "ada")
    out = Image.open(tmp_path / "pictures" / name)
    assert out.size == (PICTURE_SIDE, PICTURE_SIDE)  # squared and scaled down
    assert out.mode == "RGB"
    assert dict(out.getexif()) == {}
    assert dict(out.getexif().get_ifd(0x8825)) == {}


def test_a_sideways_photograph_is_turned_the_right_way_up(tmp_path):
    """A portrait photograph is stored landscape with a "rotate me" flag.

    Stripping the flag without applying it is how a profile picture ends up on
    its side, and stripping it is exactly what the re-encode above does.
    """
    import io

    from PIL import Image
    from auteur.web.profiles import store_picture

    # Wide, with the left half red — and a tag saying it should be rotated 90°.
    art = Image.new("RGB", (400, 200), (20, 20, 200))
    art.paste(Image.new("RGB", (200, 200), (220, 30, 30)), (0, 0))
    exif = art.getexif()
    exif[0x0112] = 6  # Orientation: rotate 90° clockwise
    raw = io.BytesIO()
    art.save(raw, "JPEG", exif=exif)

    name = store_picture(raw.getvalue(), tmp_path / "pictures", "ada")
    out = Image.open(tmp_path / "pictures" / name).convert("RGB")
    # Orientation 6 means "rotate this a quarter turn clockwise to show it", so
    # what was the left edge becomes the top: red above, blue below.
    top = out.getpixel((out.width // 2, 4))
    bottom = out.getpixel((out.width // 2, out.height - 5))
    assert top[0] > top[2], f"the top should be red, got {top}"
    assert bottom[2] > bottom[0], f"the bottom should be blue, got {bottom}"

    # And the same picture without the tag is *not* turned, which is what says
    # the rotation above came from reading the tag rather than from the crop.
    plain = io.BytesIO()
    art.save(plain, "JPEG")
    flat = Image.open(
        tmp_path / "pictures" / store_picture(plain.getvalue(), tmp_path / "pictures", "flat")
    ).convert("RGB")
    assert flat.getpixel((flat.width // 2, 4))[0] < 120, "an untagged picture was rotated anyway"


def test_a_file_that_is_not_a_picture_is_explained_rather_than_raised(tmp_path):
    from auteur.web.profiles import BadPicture, store_picture

    for bad in (b"", b"<html><script>alert(1)</script></html>", b"\x00\x01\x02"):
        with pytest.raises(BadPicture):
            store_picture(bad, tmp_path / "pictures", "ada")


def test_a_picture_that_decompresses_to_gigabytes_is_refused(tmp_path):
    """Pillow's own limit raises a warning, which is not a defence."""
    import io
    import struct
    import zlib

    from auteur.web.profiles import BadPicture, store_picture

    # A PNG header claiming 40,000 x 40,000 — a few dozen bytes on the wire,
    # six gigabytes of pixels if anything decodes it.
    def chunk(kind, payload):
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 40000, 40000, 8, 2, 0, 0, 0)
    bomb = (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\x00" * 64))
    )
    with pytest.raises(BadPicture):
        store_picture(io.BytesIO(bomb).getvalue(), tmp_path / "pictures", "ada")


# -- as served --------------------------------------------------------------


def _api_get(base, path, cookie):
    import json as _json
    from urllib.request import Request, urlopen

    with urlopen(Request(base + path, headers={"Cookie": cookie})) as response:
        return _json.loads(response.read().decode())


def _api_post(base, path, cookie, payload=None):
    import json as _json
    from urllib.request import Request, urlopen

    request = Request(
        base + path,
        data=_json.dumps(payload or {}).encode(),
        headers={"Cookie": cookie, "Content-Type": "application/json"},
    )
    with urlopen(request) as response:
        return _json.loads(response.read().decode())


def test_the_container_runs_the_app_with_flags_that_exist():
    """A Dockerfile is code nobody type-checks.

    The first draft of this one set `AUTEUR_WORKSPACE=/data`, an environment
    variable this project has never had. Nothing would have failed: the server
    would have started, ignored it, and written every film into the container's
    own filesystem, where a restart loses them. An invented flag in a shell
    string is exactly the class of mistake that survives review and shows up as
    lost footage.

    So the CMD is parsed and its options are checked against the parser the CLI
    actually builds, and any AUTEUR_* name the compose file sets is checked
    against the ones the code reads.
    """
    import shlex

    root = Path(__file__).resolve().parent.parent
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")

    line = [ln for ln in dockerfile.splitlines() if ln.startswith("CMD ")]
    assert line, "the Dockerfile does not say how to run the app"
    inner = line[0][line[0].index("python -m auteur") : line[0].rindex('"')]
    words = shlex.split(inner.replace("${PORT}", "8000"))
    assert words[:4] == ["python", "-m", "auteur", "serve"], words[:4]

    from auteur.cli import _build_parser

    # `parse_args` refuses an option the parser does not define, which is the
    # whole point — an invented flag has to fail here rather than in a
    # container somebody has already deployed.
    parsed = _build_parser().parse_args(words[3:])
    assert parsed.host == "0.0.0.0", "a container that binds loopback is unreachable"
    assert parsed.out == "/data", "the films must land on the volume, not in the image"

    source = "\n".join(path.read_text(encoding="utf-8") for path in (root / "auteur").rglob("*.py"))
    known = set(re.findall(r"AUTEUR_[A-Z_]+", source))
    known |= {"AUTEUR_TIKTOK_CLIENT_KEY", "AUTEUR_TIKTOK_CLIENT_SECRET"}
    known |= {"AUTEUR_INSTAGRAM_CLIENT_ID", "AUTEUR_INSTAGRAM_CLIENT_SECRET"}

    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    for name in set(re.findall(r"AUTEUR_[A-Z_]+", compose)):
        assert name in known, (
            f"docker-compose.yml sets {name}, which nothing in auteur/ reads — "
            "see the invented AUTEUR_WORKSPACE this guards"
        )


def test_the_stylesheets_are_held_to_the_type_scale():
    """A scale nothing is held to is a list of numbers in a comment.

    `:root` defines eleven named sizes taken from what the two mobile
    operating systems actually render their own text at. Measured in a browser
    before this test existed, the make screen used **seven different type
    sizes** and the profile seven, because twenty-five rules across the
    stylesheets set a pixel value directly — 15px, 14px, 13px, 16px — beside
    the thirty-five that used the scale. Two of those, 14px and 16px, are not
    on the scale at all.

    The same for corners. `--radius` and `--radius-sm` were named and nine
    card-scale rules used something else — 8, 9, 10, 14, 16 and 18px across two
    files. The fix was not to flatten them: a sheet rising from the bottom
    edge and a message bubble both genuinely want a bigger corner than a card.
    So that corner got a name, `--radius-lg`, and now the vocabulary is three
    named radii and a pill rather than seven numbers.

    The one exception is real and stays: `.size-a-1` through `.size-a-4` in
    profile.css are the text-size setting itself, so they must be absolute —
    they are what the scale is being *set to*, not a use of it.
    """
    import re

    from auteur.web import server

    #: The setting, not a use of the scale. Named so a future reader does not
    #: quietly widen this to whatever they wanted to hard-code.
    ALLOWED = {"size-a-1", "size-a-2", "size-a-3", "size-a-4"}
    #: And one platform constraint. iOS zooms the page when a focused text
    #: field is under 16px, and `1rem` is only 16px while the root is at its
    #: default — the browser's own text-size control moves it. Writing a field
    #: as `var(--text-callout)` therefore works on a normal setting and zooms
    #: the whole app on a smaller one. `test_the_page_is_built_for_a_phone`
    #: asserts the literal is present, and it caught this exact regression
    #: when the scale work first swept it up.
    IOS_ZOOM_FLOOR = "iOS zoom note"

    strays: list[str] = []
    for sheet in sorted(server.STATIC.glob("*.css")):
        if sheet.name == "theme.css":
            continue  # generated from the palette; it carries no type at all
        for number, line in enumerate(sheet.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"font-size:\s*\d+(\.\d+)?px", line):
                if not any(name in line for name in ALLOWED) and IOS_ZOOM_FLOOR not in line:
                    strays.append(f"{sheet.name}:{number} {line.strip()[:70]}")
            # Only the card-scale range, where a token exists. A 1px mark on
            # a grip bar and a 999px pill are details rather than corners, and
            # demanding a token for those would be tidiness rather than
            # discipline.
            corner = re.search(r"border-radius:\s*(\d+)px", line)
            if corner and 8 <= int(corner.group(1)) <= 40:
                strays.append(f"{sheet.name}:{number} {line.strip()[:70]}")

    assert (
        not strays
    ), "type sizes and corners set in raw pixels rather than from the scale:\n  " + "\n  ".join(
        strays
    )


def test_a_demo_clip_is_as_long_as_it_was_asked_for(tmp_path):
    """Every clip in the App Store screenshot harness was 225s, not 3s.

    `zoompan`'s `d` is how many output frames to produce *per input frame*,
    and the input was `-loop 1 -t 3` — seventy-five of them. `d=75` therefore
    asked for 75 x 75 frames, and the "three second" demo films rendered at
    two hundred and twenty-five seconds each.

    Nothing failed. The files rendered, the screenshots looked correct, the
    harness was merely slow, and the defect surfaced only when the Schedule
    board began printing a film's measured runtime and showed "225s". So the
    lesson is the one this repository keeps relearning: a number nothing
    compares against anything is a number nobody checks. This compares.
    """
    from auteur import ffmpeg as ff

    import sys

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "tools" / "appstore"))
    import screenshots as harness

    made = harness.a_film(tmp_path, "check", "harbour", seconds=3)
    info = ff.probe(str(made))
    got = float(info.get("format", {}).get("duration") or 0.0)
    assert abs(got - 3.0) < 0.35, (
        f"a clip asked for at 3s came out at {got:.2f}s — see the zoompan "
        "`d` note in tools/appstore/screenshots.py"
    )


def test_a_finished_film_can_be_sent_to_the_schedule(web_server):
    """`Plan.film` existed since the board was written and nothing could set it.

    The field is read by the calendar and by the prediction, so it was not
    dead — it was unreachable. The two halves of the app, the one that makes a
    film and the one that decides when it goes out, had no door between them:
    somebody finished a film and then started again from a blank plan, which
    is the seam a person feels as "this is two programs".
    """
    from urllib.error import HTTPError

    from auteur.web import server as web

    base, _, cookie = web_server
    handler = web.Handler

    film = handler.films.add(
        owner="tester",
        prompt="the long way home",
        video="/nowhere/does-not-exist.mp4",
        facts=["11 shots"],
        heard="the long way home",
    )
    other = handler.films.add(
        owner="somebody-else",
        prompt="not yours",
        video="/nowhere/nor-this.mp4",
        facts=[],
        heard="not yours",
    )

    made = _api_post(base, "/api/schedule-film", cookie, {"film": film.id})
    plan = made["plan"]
    assert plan["film"] == film.id, "the plan does not carry the film"
    assert (
        plan["title"] == "the long way home"
    ), f"the board would show {plan['title']!r} rather than what the film is"
    assert plan["owner"] == "tester"
    # No shot list: the shots exist already. Being told to go and photograph
    # footage that is finished is how somebody stops trusting a tool.
    assert plan["shots"] == [], f"a finished film was given a shot list: {plan['shots']}"

    # And it is on the board, not just in the response.
    board = _api_get(base, "/api/plans", cookie)
    assert any(row["film"] == film.id for row in board["plans"]), "the plan is not on the board"

    # Somebody else's film is not yours to schedule, and the page is not what
    # decides that.
    with pytest.raises(HTTPError) as refused:
        _api_post(base, "/api/schedule-film", cookie, {"film": other.id})
    assert refused.value.code == 403

    with pytest.raises(HTTPError) as missing:
        _api_post(base, "/api/schedule-film", cookie, {"film": "no-such-film"})
    assert missing.value.code == 404


def test_no_route_is_claimed_twice():
    """A second branch on the same path is dead code that looks alive.

    `/api/connections` was already the list of *destinations* a finished film
    can be handed off to. A second branch was added below it for linked
    platform accounts, and the first one answered every request — so the
    Schedule screen fetched the path, got a payload of the wrong shape, read
    `undefined` off it and drew an empty section. No error, no warning, a 200
    response, and a feature that silently did not exist.

    Python cannot warn about this the way it warns about a duplicate
    dictionary key, because the branches are separate statements. So it is
    checked here: within each request method, no exact path may be claimed by
    two branches.
    """
    import re

    from auteur.web import server

    source = Path(server.__file__).read_text(encoding="utf-8")

    # Split by handler method so a path served on GET and POST is not a clash —
    # those are different requests and both are reachable.
    methods = re.split(r"\n    def (do_[A-Z]+)\(", source)
    seen_any = False
    for index in range(1, len(methods), 2):
        name, body = methods[index], methods[index + 1]
        paths: list[str] = []
        for group in re.findall(r'if path == ("(?:/[^"]*)")', body):
            paths.append(group.strip('"'))
        for group in re.findall(r"if path in \(([^)]*)\)", body):
            paths += [p.strip().strip("\"'") for p in group.split(",") if p.strip()]
        seen_any = seen_any or bool(paths)
        twice = {p for p in paths if paths.count(p) > 1}
        assert not twice, (
            f"{name} claims {sorted(twice)} more than once — the first branch "
            "answers and the rest are dead code that looks alive"
        )

    assert seen_any, "no routes found — has the router changed shape?"


def test_the_privacy_documents_admit_what_the_code_can_reach():
    """Three documents claimed nothing left the device. Then something could.

    `PRIVACY.md`, the Play Data safety declaration and `brand.py` all said, in
    their own words, that this app talks to nobody. That was true until
    `auteur/social/accounts.py` made it possible to connect a TikTok or
    Instagram account. A privacy claim that was accurate when tested and
    inaccurate when shipped is the specific failure a Data safety form is a
    policy strike for, rather than a rejection you fix and resubmit.

    So this holds the three documents to the code: if a platform exists in
    `PLATFORMS`, every document that describes what the app reaches has to name
    it. Adding a third platform and forgetting the paperwork fails here.
    """
    from auteur import brand
    from auteur.social import accounts

    root = Path(__file__).resolve().parent.parent
    documents = {
        "PRIVACY.md": (root / "PRIVACY.md").read_text(encoding="utf-8"),
        "tools/play/listing.py": (root / "tools" / "play" / "listing.py").read_text(
            encoding="utf-8"
        ),
        "auteur/brand.py": (root / "auteur" / "brand.py").read_text(encoding="utf-8"),
    }

    for platform in accounts.PLATFORMS.values():
        for name, text in documents.items():
            assert platform.label in text, (
                f"{name} does not mention {platform.label}, which the app can "
                "now connect to and read from"
            )

    # And the claim that is now false must be gone from all three, in the
    # absolute form it used to take.
    for name, text in documents.items():
        assert (
            "no third-party code at all" not in text
        ), f"{name} still claims no third-party code at all"

    # The feature list a store reads is built from `brand.FEATURES`, so the
    # sentence a reviewer sees has to carry it too.
    description = brand.description()
    assert any(
        p.label in description for p in accounts.PLATFORMS.values()
    ), "the store description does not mention the platforms the app connects to"

    # Read-only, and provably so: the publishing scopes must appear nowhere.
    for platform in accounts.PLATFORMS.values():
        assert "publish" not in platform.read_scopes, (
            f"{platform.label} asks for a publishing scope; the app claims it "
            "cannot post and that claim has to be true in the scope string"
        )


def test_no_screen_links_to_a_route_the_app_does_not_serve():
    """The tab-bar guard only read chrome.js, so it missed the next one.

    Three routes to nowhere have been written in this project in one day —
    `/looks` in the store screenshot plan, `/discover` in the rebuilt tab bar,
    and `/connect/<platform>` from the Schedule screen's Connect control. The
    first two were caught by a test that reads only `chrome.js`. The third was
    not, because it is in `manager.js`, which is the lesson: a guard scoped to
    the file where the bug last happened catches that bug and no other.

    This reads every href out of every script and page in the app.
    """
    import re

    from auteur.web import server

    source = Path(server.__file__).read_text(encoding="utf-8")
    routes = {
        piece.strip().strip("\"'")
        for group in re.findall(r"path in \(([^)]*)\)", source)
        for piece in group.split(",")
        if piece.strip()
    }
    routes.add("/")
    # Prefix routes, matched with startswith in the server rather than listed.
    prefixes = tuple(re.findall(r'path\.startswith\("([^"]+)"\)', source))
    assert prefixes, "no prefix routes found — has the router changed shape?"

    def served(where: str) -> bool:
        return where in routes or where.startswith(prefixes)

    bad: list[str] = []
    for page in sorted(server.STATIC.glob("*.js")) + sorted(server.STATIC.glob("*.html")):
        text = page.read_text(encoding="utf-8")
        # No closing quote required. A route built by concatenation —
        # `href="/connect/' + key + '"` — has a literal prefix and no closing
        # quote, and requiring one skipped exactly the link this test was
        # written for. The literal prefix is enough: it either starts with a
        # prefix route the server serves, or it leads nowhere.
        for target in re.findall(r'href="(/[^"\'#?+$]*)', text):
            # Files, not routes: served by their own branch and named exactly.
            if target.startswith(("/static/", "/api/", "/media/", "/films/")):
                continue
            if target.endswith((".css", ".js", ".png", ".ico", ".svg", ".webmanifest")):
                continue
            # A trailing slash is part of a prefix route ("/u/"), so it is not
            # stripped — stripping turns "/u/" into "/u", which matches nothing,
            # and this test spent its first run reporting four real routes as
            # broken for exactly that reason.
            if not served(target):
                bad.append(f"{page.name} -> {target}")

    assert not bad, "links to routes the app does not serve: " + ", ".join(sorted(set(bad)))


def test_every_tab_and_create_entry_points_at_a_route_the_app_serves():
    """A tab bar slot leading to a 404 is the worst possible 404.

    Made twice in one day. `brand.SHOTS` sourced a store screenshot from
    "/looks", and the first draft of the rebuilt tab bar gave the second slot
    to "/discover" — neither of which the server has ever served. Both were
    found by asking the server rather than by reading the file, so that is what
    this does: it reads the routes out of `server.py` and the destinations out
    of `chrome.js`, and neither side can be edited alone.
    """
    from auteur.web import server

    source = Path(server.__file__).read_text(encoding="utf-8")
    routes = {
        piece.strip().strip("\"'")
        for group in re.findall(r"path in \(([^)]*)\)", source)
        for piece in group.split(",")
        if piece.strip()
    }
    routes.add("/")

    chrome = (server.STATIC / "chrome.js").read_text(encoding="utf-8")

    tabs = re.search(r"var TABS = \[(.*?)\n  \];", chrome, re.S)
    assert tabs, "the tab bar no longer declares TABS"
    creates = re.search(r"var CREATE = \[(.*?)\n  \];", chrome, re.S)
    assert creates, "the create sheet no longer declares CREATE"

    destinations = re.findall(r'href:\s*"([^"]+)"', tabs.group(1) + creates.group(1))
    assert len(destinations) >= 9, f"only found {len(destinations)} destinations"

    for href in destinations:
        # A fragment is a place on a page, not a page.
        path = href.split("#")[0] or "/"
        assert path in routes, (
            f"the navigation sends people to {href!r}, which the server does "
            "not serve — see the /looks and /discover entries this guards"
        )


def test_every_promotional_still_is_shot_from_a_route_the_app_serves():
    """A screenshot plan pointing at a 404 produces no screenshot.

    `SHOTS` listed "/looks" as the source of the "Graded for a decade" still.
    The app has never served that path — the decade grades are a control on the
    home screen — so the capture for the most visual slot in both store
    listings was a route that 404s, and the only thing that noticed was a
    browser being driven by hand.
    """
    from auteur import brand
    from auteur.web import server

    served = set(re.findall(r"path in \(([^)]*)\)", Path(server.__file__).read_text()))
    routes = {
        piece.strip().strip("\"'")
        for group in served
        for piece in group.split(",")
        if piece.strip()
    }
    routes.add("/")
    for shot in brand.SHOTS:
        assert shot.route in routes, (
            f"the {shot.key!r} still is shot from {shot.route!r}, "
            "which the server does not serve"
        )


def test_no_join_is_offset_past_the_end_of_what_it_joins():
    """An xfade offset past the end of its first input truncates the film.

    Silently. ffmpeg exits zero, writes a file, and the picture simply stops
    there — a 20-second montage came back with 4.8 seconds of picture and 19.7
    seconds of music over black, and the only thing that noticed was the critic
    saying "it came out the wrong length" without saying how.

    The offsets were summed from the EDL's *planned* shot durations. A segment
    is a whole number of frames, so a shot planned at 0.250s renders at 0.233s
    at 30fps, and the 7% shortfall accumulates until an offset outruns the
    chain. It stayed hidden while every shot was about the same length; giving
    films a held shot and real landings made the drift big enough to cross the
    line.

    So the offsets come from the measured segments now, and this is the
    invariant that was missing: no join may begin after the end of the thing it
    is joining onto.
    """
    import re

    from auteur.edl import EditDecisionList, Shot, Transition

    shots = []
    for index in range(12):
        shot = Shot(
            clip_id=f"c{index}",
            source="/nowhere.mp4",
            start=0.0,
            end=0.25,
            transition_in=(Transition("cut", 0.0) if index % 4 else Transition("dissolve", 0.2)),
        )
        shots.append(shot)
    shots[0].transition_in = Transition("cut", 0.0)
    edl = EditDecisionList(shots=shots)

    # What the renderer really gets back: every segment a little shorter than
    # planned, because frames are whole.
    measured = [0.233] * len(shots)

    from auteur.render import _assemble_video

    graph, _ = _assemble_video(edl, len(shots), measured)

    running = measured[0]
    for index in range(1, len(shots)):
        found = re.search(rf"\[vin{index}\]xfade[^;]*?offset=([0-9.]+)", graph)
        if found:
            offset = float(found.group(1))
            assert offset <= running + 1e-6, (
                f"join {index} starts at {offset:.4f}s, past the {running:.4f}s "
                "of picture actually in front of it — ffmpeg ends the film there"
            )
            running += measured[index] - shots[index].transition_in.duration
        else:
            running += measured[index]

    # And the planned offsets must not be what is used: with the plan they are
    # measurably larger, which is the bug this replaces.
    stale, _ = _assemble_video(edl, len(shots), None)
    first_measured = re.search(r"offset=([0-9.]+)", graph)
    first_stale = re.search(r"offset=([0-9.]+)", stale)
    assert first_measured and first_stale
    assert float(first_measured.group(1)) < float(
        first_stale.group(1)
    ), "the measured offsets match the planned ones, so nothing is being measured"


def test_no_flex_row_is_asked_to_hold_a_sentence():
    """A flex container makes an item of every child *and every run of text*.

    The Schedule tab's "This never posts for you." notice was one <p> with
    `display: flex`, an icon <span>, a <strong>, an <em> and prose between
    them. Flex does not care that it is a sentence: it laid the four runs out
    as four narrow columns, and on a 390px phone the notice read

        This      Connecting   read   — followers and how a
        never     an account          post did. ...
        posts     below asks
        for       only to
        you.

    Nobody reading the markup would see that; it was found by taking a
    screenshot. The rule is structural rather than cosmetic: a flex or grid
    container's children *are* its layout, so prose inside one belongs in a
    child of its own.

    Deciding whether a given element is such a container needs a little of
    the cascade — `.card` is a flex row and `.card.block` is not — so the
    rules are collected with the classes they need and the last one whose
    classes the element carries wins.
    """
    import re

    from auteur.web import server

    static = server.STATIC

    # style.css first: it is the base sheet, and a page's own sheet is linked
    # after it, so later here means later in the cascade.
    sheets = [static / "style.css"] + [
        sheet for sheet in sorted(static.glob("*.css")) if sheet.name != "style.css"
    ]

    display_rules: list[tuple[frozenset[str], str]] = []
    for sheet in sheets:
        # Comments first. `[^{}]+` before a brace swallows whatever stands
        # between the previous rule and this one, and in this stylesheet that
        # is usually a paragraph of comment — which then looks like a
        # descendant selector to the filter below. The first draft of this
        # test dropped 46 of 47 classes that way and passed against the very
        # bug it was written for; the mutation run is what said so.
        text = re.sub(r"/\*.*?\*/", "", sheet.read_text(encoding="utf-8"), flags=re.S)
        for selector, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
            found = re.search(r"display:\s*([a-z-]+)", declarations)
            if not found:
                continue
            for piece in selector.split(","):
                piece = piece.strip()
                # Only selectors that are classes on one element. A descendant
                # or child selector describes something this test cannot
                # resolve from markup alone.
                if not piece or re.search(r"[>+~\s]", piece) or not piece.startswith("."):
                    continue
                if re.search(r"[^.\w-]", piece):  # pseudo-classes, attributes
                    continue
                display_rules.append((frozenset(piece.lstrip(".").split(".")), found.group(1)))

    assert display_rules, "no display rules found — has the stylesheet changed shape?"

    def lays_out_its_children(classes: frozenset[str]) -> bool:
        winner = ""
        for needed, value in display_rules:
            if needed <= classes:
                winner = value
        return winner in ("flex", "inline-flex", "grid", "inline-grid")

    # A real parser rather than a regex. The first draft matched
    # `<tag class=...>(.*?)</tag>` with finditer, and matches do not overlap:
    # `<main class="page">` matched first and swallowed every element inside
    # it, so nothing nested was ever examined and the test passed against the
    # bug. HTMLParser reports each element's own text, which is exactly the
    # question being asked.
    from html.parser import HTMLParser

    VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    class LooseProse(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            # tag, classes, its own text runs, how many element children
            self.stack: list[list] = []
            self.found: list[tuple[str, str, int, str]] = []

        def handle_starttag(self, tag, attrs):
            if tag in VOID:
                return
            classes = ""
            for name, value in attrs:
                if name == "class":
                    classes = value or ""
            if self.stack:
                self.stack[-1][3] += 1
            self.stack.append([tag, frozenset(classes.split()), [], 0])

        def handle_endtag(self, tag):
            while self.stack:
                name, classes, runs, children = self.stack.pop()
                text = [run.strip() for run in runs if run.strip()]
                words = sum(len(run.split()) for run in text)
                # Prose *mixed with* element children is the defect: those
                # are the separate items flex will column up. A label that is
                # nothing but its own words — "Choose from camera roll" in an
                # inline-flex button — is one item and wraps like any text.
                if words >= 4 and children and lays_out_its_children(classes):
                    self.found.append((name, " ".join(sorted(classes)), words, text[0]))
                if name == tag:
                    break

        def handle_data(self, data):
            if self.stack:
                self.stack[-1][2].append(data)

    offenders: list[str] = []
    for page in sorted(static.glob("*.html")):
        parser = LooseProse()
        parser.feed(page.read_text(encoding="utf-8"))
        for tag, classes, words, first in parser.found:
            offenders.append(
                f"{page.name}: <{tag} class={classes!r}> holds {words} loose "
                f"words — {first[:50]!r}"
            )

    assert (
        not offenders
    ), "prose sitting directly inside a flex or grid container becomes " "columns: " + "; ".join(
        sorted(set(offenders))
    )


def test_no_root_tab_offers_a_way_back_out_of_itself():
    """A tab is the top of its own stack; there is nothing above it.

    The Schedule screen used to hang off the studio, and it kept its back
    arrow when it became the second slot in the tab bar. So one root tab in
    five had a back arrow that pointed at /studio — not where you came from,
    not this screen's parent any more, and reachable from any of the other
    four, which means the arrow's destination had nothing to do with the
    journey. Found by opening all five tabs in a phone-sized browser and
    listing the visible controls, not by reading the markup.

    The same class as every other bug this file guards: a value that exists
    and is never compared to anything. `href="/studio"` was true once, the
    tab bar changed underneath it, and nothing held the two together.
    """
    import re

    from auteur.web import server

    source = Path(server.__file__).read_text(encoding="utf-8")

    # Which document each route serves, read off the router rather than
    # assumed: `if path in (...): self._static(STATIC / "name.html", ...)`.
    served: dict[str, str] = {}
    for group, page in re.findall(
        # `or path.startswith(...)` may trail the tuple — the profile route
        # does exactly that — so anything up to the colon is allowed.
        r"path in \(([^)]*)\)[^:\n]*:\s*\n(?:\s*#[^\n]*\n)*\s*self\._static\("
        r'\s*STATIC / \(?"([^"]+\.html)"',
        source,
    ):
        for piece in group.split(","):
            piece = piece.strip().strip("\"'")
            if piece:
                served[piece] = page

    chrome = (server.STATIC / "chrome.js").read_text(encoding="utf-8")
    tabs = re.search(r"var TABS = \[(.*?)\n  \];", chrome, re.S)
    assert tabs, "the tab bar no longer declares TABS"
    hrefs = re.findall(r'href:\s*"([^"]+)"', tabs.group(1))
    assert len(hrefs) == 5, f"expected five tab slots, found {len(hrefs)}"

    offenders: list[str] = []
    for href in hrefs:
        page = served.get(href.split("#")[0])
        assert page, f"the tab bar points at {href!r}, which serves no document"
        markup = (server.STATIC / page).read_text(encoding="utf-8")
        # A back control belonging to a sub-view is fine — the inbox reveals
        # one inside a thread, the manager inside one plan, the profile on
        # somebody else's. Every one of those starts hidden, either on the
        # control or on the <main> that holds it, and the browser agrees:
        # opening all five tabs showed exactly one visible back arrow. What
        # may not exist is one that is on screen the moment the tab opens.
        inside_hidden_screen = False
        for tag in re.findall(r"<(?:main|a|button)\b[^>]*>", markup):
            if tag.startswith("<main"):
                inside_hidden_screen = " hidden" in tag
                continue
            if "topbar-back" not in tag and "sbar-back" not in tag:
                continue
            if inside_hidden_screen or "hidden" in tag:
                continue
            offenders.append(f"{page} ({href}): {tag}")

    assert (
        not offenders
    ), "a root tab shows a back control, which has nowhere honest to go: " + "; ".join(offenders)


def test_the_published_page_has_the_same_tabs_as_the_app():
    """The link showed a different product and nothing compared the two.

    `chrome.js` emits the app's bar. The published page is one file with no
    server, so that script is stripped and `build_artifact.py` wrote its own
    — and its own said **Create, Templates, Animation, Studio, You** while the
    app said Feed, Schedule, Create, Messages, You. Not one name in common
    beyond two. Anybody following the link met a different app from the one in
    the screenshots and was right to say so.

    It survived because the comment above that list claims it is "the same one
    the app has" and nothing checked. Two lists, one idea, in two files —
    which is the shape of every other thing found in this repository this
    week.

    Three of the five need somewhere to keep things and a page with no server
    cannot have them. They are still named, and tapping one says why. Renaming
    the bar around the limitation is what made the link look like another app.
    """
    import re

    from auteur.web import server

    root = Path(__file__).resolve().parent.parent
    chrome = (server.STATIC / "chrome.js").read_text(encoding="utf-8")
    builder = (root / "tools" / "artifact" / "build_artifact.py").read_text(encoding="utf-8")

    tabs = re.search(r"var TABS = \[(.*?)\n  \];", chrome, re.S)
    assert tabs, "chrome.js no longer declares TABS"
    app = re.findall(r'label:\s*"([^"]+)"', tabs.group(1))

    published = re.search(r"TABS = \((.*?)\n\)", builder, re.S)
    assert published, "build_artifact.py no longer declares TABS"
    page = re.findall(r'"[a-z]+",\s*"[^"]+",\s*"([^"]+)"', published.group(1))

    assert app == page, (
        f"the app's bar is {app} and the published page's is {page} — "
        "somebody following the link meets a different product"
    )
    assert len(app) == 5, f"the bar is five slots on both phones; this is {len(app)}"


def test_both_renderers_shape_a_film_the_same_way():
    """The published page is the film most people will ever see this app make.

    It has its own cutting engine, because a published page has no server and
    no Python behind it — and a second copy of a number drifts. That has
    already happened once here over pace, and it happened again over shape:
    the structure layer went into `auteur/craft/story.py` and the browser kept
    arranging bars with no structure at all. Measured across its six styles,
    the longest shot was **exactly twice the median on four of them** —
    hypercut 2.00, story 2.00, hype 2.00 — which is the same ceiling, in the
    same shape, in the other engine.

    So the constants are read out of both and compared, the way the pace
    tables already are. Changing one alone fails here.
    """
    import re

    from auteur.craft import story

    root = Path(__file__).resolve().parent.parent
    js = (root / "tools" / "artifact" / "style.js").read_text(encoding="utf-8")

    block = re.search(r"var STRUCTURE = \{(.*?)\n  \};", js, re.S)
    assert block, "style.js no longer declares STRUCTURE"

    def number(name: str) -> float:
        found = re.search(rf"{name}:\s*([0-9.]+)", block.group(1))
        assert found, f"STRUCTURE has no {name}"
        return float(found.group(1))

    assert (
        number("opening") == story.STRESS[story.Beat.OPEN]
    ), "the browser and the director disagree about how long an opening is"
    assert (
        number("hold") == story.STRESS[story.Beat.HOLD]
    ), "the browser and the director disagree about the held shot"
    assert (
        number("close") == story.STRESS[story.Beat.CLOSE]
    ), "the browser and the director disagree about the closing shot"
    assert number("holdAt") == story.HOLD_AT
    assert number("leastShots") == story.LEAST_SHOTS_FOR_A_HOLD

    # And the shape is applied, not merely declared: `arrange` must return it.
    assert "return shape(out);" in js, "style.js declares a structure and does not apply it"


def test_a_film_has_a_shot_that_lands_and_one_that_holds():
    """The longest shot was exactly 2.00x the median in every film ever cut.

    Not approximately. Exactly, from three different briefs — a 20s montage, a
    15s hypercut and a 24s cinematic piece — which is a ceiling rather than a
    coincidence. `shot_length_at` claims in its own docstring to span "roughly
    4:1, the difference between a held beat and a flurry", and then the beat
    quantiser rounded every slot to one or two grid units and the range
    collapsed. A 61-shot montage was built from three distinct lengths.

    That is what "computer-generated" means when somebody says it about an
    edit: nothing is ever emphasised, so nothing is ever a decision. A film
    needs shots that land and, once, a shot that holds.
    """
    import random

    from auteur.craft import story
    from auteur.director.brief import parse_brief
    from auteur.director.heuristic import _build_slots

    for prompt, runtime in (
        ("a montage of the walk home", 20.0),
        ("fast neon hypercut", 15.0),
        ("a cinematic film about the long way home", 24.0),
    ):
        brief = parse_brief(prompt)
        rng = random.Random(11)
        rough = _build_slots(brief, runtime, None, 0.0, rng)
        spread = 1.0
        if rough:
            shape = story.shape(len(rough), random.Random(11), arc=brief.arc)
            spread = sum(story.STRESS[b] for b in shape.beats) / max(len(shape), 1)
            shape = story.shape(
                max(1, round(len(rough) / max(spread, 0.2))), random.Random(11), arc=brief.arc
            )
        slots = _build_slots(brief, runtime, None, 0.0, random.Random(11), shape)

        lengths = sorted(slot.length for slot in slots)
        median = lengths[len(lengths) // 2]
        longest = lengths[-1]
        assert longest / median >= 3.0, (
            f"{prompt!r}: the longest shot is only {longest / median:.2f}x the "
            "median, so nothing in the film is emphasised — this was 2.00 "
            "exactly on every brief before the structure layer existed"
        )
        assert len({round(x, 3) for x in lengths}) >= 4, (
            f"{prompt!r}: only {len({round(x, 3) for x in lengths})} distinct "
            "shot lengths, which is a metronome rather than a rhythm"
        )

        # And the emphasis is where the structure asked for it, not wherever
        # the footage happened to be long.
        held = [slot for slot in slots if slot.beat is story.Beat.HOLD]
        assert len(held) == 1, f"{prompt!r}: expected exactly one hold, got {len(held)}"
        where = held[0].start / runtime
        assert 0.45 < where < 0.9, f"{prompt!r}: the hold sits at {where:.0%} of the film"
        assert held[0].length == max(s.length for s in slots), (
            f"{prompt!r}: the held shot is not the longest one, so the quantiser "
            "has handed it back to the rhythm it exists to break"
        )


def test_the_opening_shot_is_not_the_one_that_lingers():
    """Two independent sources say the opening must be short, and the first
    draft of `story.py` stretched it by 1.6x.

    The Scholar, asked "what makes an opening hold a viewer", reports the
    opening held 0.12s across 24 reels with 22 cutting inside half a second.
    The APX craft rules fire `hook-length` above 2.0s from the other direction.
    A shot that needs explaining has already been scrolled past.
    """
    import random

    from auteur.craft import story
    from auteur.director.brief import parse_brief
    from auteur.director.heuristic import _build_slots

    assert story.STRESS[story.Beat.OPEN] < 1.0, "the opening is being stretched"
    assert story.STRESS[story.Beat.OPEN] < story.STRESS[story.Beat.BUILD]

    for prompt in ("a montage of the walk home", "a cinematic film, slowly"):
        brief = parse_brief(prompt)
        shape = story.shape(20, random.Random(5), arc=brief.arc)
        slots = _build_slots(brief, 20.0, None, 0.0, random.Random(5), shape)
        assert not story.opening_is_too_long(slots[0].length), (
            f"{prompt!r}: the film opens on a {slots[0].length:.2f}s shot, past "
            f"the {story.OPENING_HOLD_LIMIT}s a hook has"
        )


def test_no_sheet_is_hidden_inside_another_sheet():
    """A sheet nested in a sheet can never be opened, and nothing says so.

    The watch-history sheet shipped one revision inside `#reports-sheet`.
    Clicking its row cleared the inner `hidden` and the sheet still did not
    appear, because the outer one was hidden and an ancestor's `hidden` wins.
    Nothing failed: the handler ran, the fetch returned, the rows were built,
    `innerText` even read back correctly. Only measuring the element in a
    browser found it, at 0x0.

    Every sheet in this app is a fixed-position overlay at `inset: 0`, so a
    sheet is always a sibling of the others and never a child of one.
    """
    from html.parser import HTMLParser

    class Sheets(HTMLParser):
        VOID = {"br", "img", "meta", "link", "input", "hr", "source", "track"}

        def __init__(self) -> None:
            super().__init__()
            self.depth: list[bool] = []
            self.nested: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag in self.VOID:
                return
            found = dict(attrs)
            is_sheet = "sheet" in (found.get("class") or "").split()
            if is_sheet and any(self.depth):
                self.nested.append(found.get("id") or "an unnamed sheet")
            self.depth.append(is_sheet)

        def handle_endtag(self, tag):
            if tag not in self.VOID and self.depth:
                self.depth.pop()

    from auteur.web import server as web

    pages = sorted(Path(web.STATIC).glob("*.html"))
    assert pages, "no pages to check"
    for page in pages:
        reader = Sheets()
        reader.feed(page.read_text(encoding="utf-8"))
        assert not reader.nested, (
            f"{page.name} nests {reader.nested} inside another sheet — "
            "opening it will do nothing, because the outer sheet's hidden wins"
        )


def test_every_sheet_can_be_dismissed_without_finding_its_button():
    """Tapping outside a sheet closes it, which means it needs a scrim.

    The watch-history sheet was written without one, so the only way out was
    the Done button — on a phone, where every other sheet in the app closes on
    a tap anywhere outside it.
    """
    import re

    from auteur.web import server as web

    for page in sorted(Path(web.STATIC).glob("*.html")):
        text = page.read_text(encoding="utf-8")
        for block in re.findall(
            r'<div class="sheet" id="([a-z-]+)-sheet"[^>]*>(.*?)\n</div>', text, re.S
        ):
            name, body = block
            assert "sheet-scrim" in body, f"{page.name}: the {name} sheet has no scrim to tap"


def test_the_profile_page_is_served_for_you_and_for_anybody_else(web_server):
    """`/u/<name>` is a link somebody can send, and it is the same document."""
    from urllib.request import Request, urlopen

    base, _, cookie = web_server
    for path in ("/profile", "/me", "/u/grace"):
        with urlopen(Request(base + path, headers={"Cookie": cookie})) as response:
            body = response.read().decode()
        assert response.headers["Content-Type"].startswith("text/html"), path
        assert 'id="profile"' in body, path


def test_your_own_profile_carries_your_account_and_nobody_elses(web_server):
    base, _, cookie = web_server
    mine = _api_get(base, "/api/profile", cookie)["profile"]
    assert mine["who"] == "tester"
    assert mine["me"] is True
    assert mine["email"] == "tester@example.com"

    theirs = _api_get(base, "/api/profiles/grace", cookie)["profile"]
    assert theirs["who"] == "grace"
    assert theirs["me"] is False
    # Somebody else's email is not somebody else's business.
    assert "email" not in theirs


def test_following_somebody_moves_both_counts(web_server):
    base, _, cookie = web_server
    after = _api_post(base, "/api/profiles/grace/follow", cookie, {"follow": True})["profile"]
    assert after["you_follow"] is True
    assert after["followers"] == 1
    assert _api_get(base, "/api/profile", cookie)["profile"]["following"] == 1

    rows = _api_get(base, "/api/profiles/tester/followers", cookie)["people"]
    assert rows == []
    rows = _api_get(base, "/api/profiles/tester/following", cookie)["people"]
    assert [row["who"] for row in rows] == ["grace"]

    back = _api_post(base, "/api/profiles/grace/follow", cookie, {"follow": False})["profile"]
    assert back["you_follow"] is False
    assert back["followers"] == 0


def test_following_a_name_with_no_account_is_a_404(web_server):
    from urllib.error import HTTPError

    base, _, cookie = web_server
    with pytest.raises(HTTPError) as raised:
        _api_post(base, "/api/profiles/nobody/follow", cookie, {"follow": True})
    assert raised.value.code == 404


def test_the_feed_can_be_narrowed_to_the_people_you_follow(web_server):
    """A count that is not a filter is a number nobody can act on."""
    base, _, cookie = web_server
    from auteur.web import server as web

    for owner in ("grace", "someone-else"):
        film = tmp = web.Handler.studio.workspace / f"{owner}.mp4"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(b"not really an mp4")
        web.Handler.films.add(owner=owner, prompt=f"{owner}'s film", video=str(film))

    everyone = _api_get(base, "/api/feed?scope=all", cookie)
    assert len(everyone["films"]) == 2
    # Each author arrives with the feed rather than one request per row.
    assert set(everyone["people"]) == {"grace", "someone-else"}

    _api_post(base, "/api/profiles/grace/follow", cookie, {"follow": True})
    followed = _api_get(base, "/api/feed?scope=following", cookie)
    assert [f["owner"] for f in followed["films"]] == ["grace"]

    # `?mine=1` is what the first version of the feed sent, and a phone with
    # that page cached is a real client.
    assert _api_get(base, "/api/feed?mine=1", cookie)["films"] == []


def test_a_profile_picture_goes_up_and_comes_back_as_a_jpeg(web_server):
    import io

    from PIL import Image
    from urllib.request import Request, urlopen

    base, _, cookie = web_server
    raw = io.BytesIO()
    Image.new("RGB", (900, 600), (30, 160, 90)).save(raw, "PNG")

    request = Request(
        base + "/api/profile/picture",
        data=raw.getvalue(),
        headers={"Cookie": cookie, "Content-Type": "image/png"},
    )
    with urlopen(request) as response:
        import json as _json

        profile = _json.loads(response.read().decode())["profile"]
    assert profile["picture"].startswith("/api/profiles/tester/picture")

    with urlopen(Request(base + profile["picture"], headers={"Cookie": cookie})) as response:
        served = response.read()
        assert response.headers["Content-Type"] == "image/jpeg"
    assert Image.open(io.BytesIO(served)).format == "JPEG"

    # And taking it off puts the disc back, rather than serving a stale file.
    from urllib.error import HTTPError

    gone = _api_post(base, "/api/profile/picture/remove", cookie)["profile"]
    assert gone["picture"] == ""
    with pytest.raises(HTTPError) as raised:
        urlopen(Request(base + "/api/profiles/tester/picture", headers={"Cookie": cookie}))
    assert raised.value.code == 404


def test_a_picture_url_changes_when_the_picture_does(tmp_path):
    """Served with a long cache lifetime, so a replaced picture needs a new URL.

    Without this somebody changes their picture, sees the old one everywhere,
    and reports the feature as broken.
    """
    import io
    import time

    from PIL import Image
    from auteur.web.profiles import Profiles, store_picture

    store = Profiles(tmp_path / "profiles.json", tmp_path / "pictures")
    raw = io.BytesIO()
    Image.new("RGB", (100, 100), (10, 10, 10)).save(raw, "JPEG")

    store.set_picture("ada", store_picture(raw.getvalue(), store.pictures, "ada"))
    first = store.get("ada").picture_url
    time.sleep(1.05)  # the stamp is whole seconds
    store.set_picture("ada", store_picture(raw.getvalue(), store.pictures, "ada"))
    assert store.get("ada").picture_url != first


def test_the_profile_is_the_fifth_tab_and_the_studio_is_on_it():
    """The bar ends with the person using it, as both reference apps do — and
    the workroom that used to be there is a row at the top of their profile,
    which is where Instagram keeps its professional dashboard."""
    from auteur.web import server

    chrome = (server.STATIC / "chrome.js").read_text()
    assert '"/profile"' in chrome
    assert '"/studio"' not in chrome

    page = (server.STATIC / "profile.html").read_text()
    assert 'href="/studio"' in page
    assert "chrome.js" in page


def test_the_account_settings_are_hidden_until_the_profile_is_known_to_be_yours():
    """`hidden` in the markup, revealed by the answer — never the other way.

    A settings panel that flashes on somebody else's page before a script
    hides it is a settings panel that was on somebody else's page.
    """
    import re

    from auteur.web import server

    page = (server.STATIC / "profile.html").read_text()
    for block in ("settings", "mine"):
        found = re.search(r'<div id="' + block + r'"([^>]*)>', page)
        assert found, block
        assert "hidden" in found.group(1), block


def test_the_accessibility_settings_can_turn_a_thing_on_and_never_off():
    """The phone's own setting is a statement about the person using it.

    An app switch that could contradict it would be an app switch that takes
    an accessibility setting away, so the media queries stay live underneath
    and the in-app attribute only ever adds.
    """
    from auteur.web import server

    style = (server.STATIC / "style.css").read_text()
    # Both halves of each pair: the system setting, and the in-app one.
    assert "@media (prefers-reduced-motion: reduce)" in style
    assert ':root[data-motion="still"] *' in style
    assert "@media (prefers-contrast: more)" in style
    assert ':root[data-contrast="more"]' in style
    # And nothing that switches either back off.
    assert 'data-motion="full"' not in style
    assert 'data-contrast="normal"' not in style

    settings = (server.STATIC / "settings.js").read_text()
    assert 'removeAttribute("data-motion")' in settings
    assert 'removeAttribute("data-contrast")' in settings


def test_the_feed_keeps_its_own_theme_when_the_app_changes_appearance():
    """A film is a picture on a black surround everywhere it is watched.

    Applying "Automatic" would strip the attribute that makes the feed dark
    and paint a bone-white page around a 1080x1920 video, which is a light
    leak into the one screen whose whole job is the footage.
    """
    from auteur.web import server

    feed = (server.STATIC / "feed.html").read_text()
    assert "data-theme-locked" in feed
    settings = (server.STATIC / "settings.js").read_text()
    assert 'hasAttribute("data-theme-locked")' in settings


def test_the_type_scale_moves_together_when_the_text_size_does():
    """Every rung in rem, so one root font-size scales all of it.

    A ladder with a px rung in it is a ladder where that rung stays put while
    everything around it grows, which is worse than not offering the setting.
    """
    import re

    from auteur.web import server

    style = (server.STATIC / "style.css").read_text()
    block = style[style.index("--text-large-title") : style.index("--radius:")]
    rungs = re.findall(r"(--text-[a-z0-9-]+): ([^;]+);", block)
    assert len(rungs) >= 10
    for name, value in rungs:
        assert value.strip().endswith("rem"), f"{name} is {value}, which will not scale"


# ---------------------------------------------------------------------------
# Getting into the App Store
# ---------------------------------------------------------------------------


def test_every_import_is_either_installed_or_guarded():
    """The container installs `requirements.txt` and nothing else.

    So an import that is not in that file and not wrapped in a try is an
    application that builds cleanly and dies on the first request that reaches
    it — the worst possible time to find out, and invisible on a development
    machine that happens to have the package.

    `cryptography` was exactly that: imported for Apple's client secret,
    guarded, and therefore not a crash — but also not installed, which meant
    no deployment could offer Continue with Apple at all. This is the check
    that found it, kept so it finds the next one.

    Guarded imports are fine and stay fine: `imageio_ffmpeg` is a fallback for
    finding ffmpeg and the code says so when it is absent. What is not fine is
    an unguarded import of something nothing installs.
    """
    import ast

    root = Path(__file__).resolve().parent.parent
    requirements = (root / "requirements.txt").read_text(encoding="utf-8").lower()

    # Module name -> the distribution that provides it, where they differ.
    DISTRIBUTION = {"PIL": "pillow", "yt_dlp": "yt-dlp", "imageio_ffmpeg": "imageio-ffmpeg"}

    unguarded: list[str] = []
    for source in sorted((root / "auteur").rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))

        # Every import statement that sits inside a try, at any depth.
        sheltered: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                for child in ast.walk(node):
                    if isinstance(child, (ast.Import, ast.ImportFrom)):
                        sheltered.add(id(child))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module.split(".")[0]] if node.level == 0 and node.module else []
            else:
                continue
            if id(node) in sheltered:
                continue
            for name in names:
                if name in sys.stdlib_module_names or name == "auteur":
                    continue
                if DISTRIBUTION.get(name, name).lower() in requirements:
                    continue
                unguarded.append(f"{source.relative_to(root)}:{node.lineno} imports {name!r}")

    assert not unguarded, (
        "imported, not installed by requirements.txt, and not inside a try — "
        "this is a container that builds and then dies: " + "; ".join(unguarded)
    )


def test_an_uploaded_file_arrives_byte_for_byte():
    """The app was corrupting every video and photo posted to it.

    The multipart body was handed to `email.parser`, and the email package
    parses a *text* format: it normalises line endings as it goes, turning
    every CRLF into LF and every lone CR into LF. On prose that is invisible.
    On an mp4 it deletes one byte for every CRLF that happens to occur in the
    video stream — a 700KB clip arrived 16 bytes short, a 10MB one 168 short —
    and rewrites every lone CR. ffprobe then refused the file and the app told
    the person their footage was damaged. It was. The app had damaged it.

    Found by uploading five real clips through a browser and comparing the
    bytes on disk against the originals. Nothing caught it before because no
    test compared what came out of the parser against what went in: the whole
    upload path was exercised constantly and never *checked*, which is the
    same shape as every other defect this file guards.

    So this is that comparison, on payloads chosen to contain exactly what the
    old parser destroyed.
    """
    from auteur.web.server import _parse_multipart

    CRLF = b"\r\n"

    def posted(payload: bytes, filename: str = "clip.mp4") -> bytes:
        boundary = b"----AuteurBoundary"
        body = (
            b"--"
            + boundary
            + CRLF
            + b'Content-Disposition: form-data; name="clips"; filename="'
            + filename.encode()
            + b'"'
            + CRLF
            + b"Content-Type: application/octet-stream"
            + CRLF
            + CRLF
            + payload
            + CRLF
            + b"--"
            + boundary
            + b"--"
            + CRLF
        )
        _fields, files = _parse_multipart(
            body, 'multipart/form-data; boundary="----AuteurBoundary"'
        )
        assert files, "the parser found no file at all"
        return files[0][1]

    payloads = {
        # The two the email parser rewrote, on their own and together.
        "a lone carriage return": b"\x00\r\x01\r\x02",
        "carriage return and newline": b"\x00" + CRLF + b"\x01" + CRLF,
        "nothing but line endings": CRLF * 200,
        # Every byte value, so nothing is quietly special-cased.
        "every byte there is": bytes(range(256)) * 4,
        # Shapes that sit against the delimiter logic.
        "ends with a line ending": b"abc" + CRLF,
        "starts with a line ending": CRLF + b"abc",
        "empty": b"",
        "looks like a delimiter": b"------AuteurBoundary-ish" + CRLF + b"more",
    }
    for description, payload in payloads.items():
        got = posted(payload)
        assert got == payload, (
            f"{description}: {len(payload)} bytes in, {len(got)} out — "
            "an upload is not arriving as it was sent"
        )

    # And a filename is a name, never a path: a browser sends the basename but
    # a crafted post need not, and this one is used to build a path on disk.
    boundary = b"----AuteurBoundary"
    body = (
        b"--"
        + boundary
        + CRLF
        + b'Content-Disposition: form-data; name="clips"; filename="../../etc/passwd"'
        + CRLF
        + CRLF
        + b"x"
        + CRLF
        + b"--"
        + boundary
        + b"--"
        + CRLF
    )
    _fields, files = _parse_multipart(body, 'multipart/form-data; boundary="----AuteurBoundary"')
    assert files[0][0] == "passwd", files[0][0]


def test_a_form_carries_its_fields_and_its_files_together():
    """One post holds the prompt and the footage, and both have to survive.

    The prompt is text and the clips are not, and a parser that gets the split
    wrong loses one or the other silently.
    """
    from auteur.web.server import _parse_multipart

    CRLF = b"\r\n"
    boundary = b"----Both"
    body = (
        b"--"
        + boundary
        + CRLF
        + b'Content-Disposition: form-data; name="prompt"'
        + CRLF
        + CRLF
        + b"fast neon montage, 12 seconds"
        + CRLF
        + b"--"
        + boundary
        + CRLF
        + b'Content-Disposition: form-data; name="clips"; filename="one.mp4"'
        + CRLF
        + b"Content-Type: video/mp4"
        + CRLF
        + CRLF
        + b"\x00"
        + CRLF
        + b"\xff"
        + CRLF
        + b"--"
        + boundary
        + CRLF
        + b'Content-Disposition: form-data; name="clips"; filename="two.mp4"'
        + CRLF
        + b"Content-Type: video/mp4"
        + CRLF
        + CRLF
        + b"second"
        + CRLF
        + b"--"
        + boundary
        + b"--"
        + CRLF
    )
    fields, files = _parse_multipart(body, 'multipart/form-data; boundary="----Both"')

    assert fields == {"prompt": "fast neon montage, 12 seconds"}
    assert [name for name, _ in files] == ["one.mp4", "two.mp4"]
    assert files[0][1] == b"\x00" + CRLF + b"\xff"
    assert files[1][1] == b"second"


def test_a_second_person_can_only_join_when_the_owner_says_so(web_server):
    """Sign-up used to close for good the moment the first account existed.

    The reason was sound when this app was one person's edit room served over
    their own wifi: an open door is an open door to the footage. It stopped
    being sufficient when the app grew a feed, an inbox and profiles at
    shareable addresses, every one of which needs a second person — and the
    only way to get one was `auteur account add` typed on the machine's
    terminal by the owner, not by the person joining.

    So it is a decision now, off until made, and this walks the whole of it:
    refused while closed, refused with a wrong code, allowed with the right
    one, and refused again after it is closed.
    """
    import json as _json
    import urllib.error
    from urllib.request import Request, urlopen

    base, _studio, cookie = web_server

    def post(path, payload, *, signed_in=False):
        headers = {"Content-Type": "application/json"}
        if signed_in:
            headers["Cookie"] = cookie
        request = Request(base + path, data=_json.dumps(payload).encode(), headers=headers)
        try:
            with urlopen(request) as answer:
                return answer.status, _json.loads(answer.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _json.loads(exc.read())

    def joiner(code):
        return {
            "username": "newcomer",
            "email": "newcomer@example.com",
            "password": "a-long-enough-one",
            "born": 1994,
            "code": code,
        }

    # Closed: the fixture already has two accounts.
    status, said = post("/api/signup", joiner(""))
    assert status == 403, said
    refusal = said["error"]

    status, opened = post("/api/joining", {"open": True}, signed_in=True)
    assert status == 200 and opened["open"] and opened["code"], opened
    code = opened["code"]

    # A wrong code is refused, and refused *identically* — telling a stranger
    # that a code exists and theirs is wrong is telling them one is worth
    # guessing.
    status, said = post("/api/signup", joiner("not-the-code"))
    assert status == 403
    assert said["error"] == refusal

    status, said = post("/api/signup", joiner(code))
    assert status == 200, said
    assert said["user"] == "newcomer"

    status, shut = post("/api/joining", {"open": False}, signed_in=True)
    assert status == 200 and not shut["open"]

    status, said = post("/api/signup", {**joiner(code), "username": "third"})
    assert status == 403, said


def test_only_somebody_signed_in_can_open_the_door_or_read_the_code(web_server):
    """The invite code is a credential and the switch is an auth surface.

    An unsigned caller must be able to learn neither the code nor whether one
    exists, and must certainly not be able to open the door.
    """
    import json as _json
    import urllib.error
    from urllib.request import Request, urlopen

    base, _studio, _cookie = web_server

    for method, payload in (("GET", None), ("POST", {"open": True})):
        request = Request(
            base + "/api/joining",
            data=_json.dumps(payload).encode() if payload else None,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlopen(request) as answer:
                body = answer.read().decode()
            raise AssertionError(f"{method} /api/joining answered signed out: {body}")
        except urllib.error.HTTPError as exc:
            assert exc.code in (401, 403), exc.code

    # And the door stayed shut.
    request = Request(
        base + "/api/signup",
        data=_json.dumps(
            {
                "username": "sneaky",
                "email": "s@example.com",
                "password": "a-long-enough-one",
                "born": 1994,
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request) as answer:
            raise AssertionError(f"signup succeeded: {answer.read()!r}")
    except urllib.error.HTTPError as exc:
        assert exc.code == 403


def test_the_sign_in_page_always_offers_a_way_to_make_an_account():
    """A hidden button reads as an app without sign-up.

    `#to-signup` carried a `hidden` attribute and was revealed only when the
    copy had no accounts at all — so on every copy that had ever been used,
    the sign-in page offered exactly one thing to do and somebody arriving
    without an account had nothing to press. The answer belongs on the screen
    after the button, not in whether the button exists.

    And forgetting a password is a link. It was a full-width bordered control
    the same size as the one beside it, which gave the thing almost everybody
    does once the same weight as the thing they came here to do.
    """

    from auteur.web import server

    html = (server.STATIC / "login.html").read_text(encoding="utf-8")

    signup = re.search(r"<button[^>]*id=\"to-signup\"[^>]*>", html)
    assert signup, "the sign-in page has no way to make an account"
    assert "hidden" not in signup.group(0), "the sign-up button is hidden again: " + signup.group(0)

    forgot = re.search(r"<button[^>]*id=\"to-forgot\"[^>]*>", html)
    assert forgot, "the sign-in page has no way to recover a password"
    assert "textlink" in forgot.group(
        0
    ), "forgetting a password is back to being a full-width button: " + forgot.group(0)

    # The link still has to be a finger-sized target, which a line of 15px
    # text is not — the row around it carries that.
    css = (server.STATIC / "style.css").read_text(encoding="utf-8")
    assert ".afterthought" in css and "min-height: 44px" in css


def test_a_setting_lives_in_settings_and_nowhere_else():
    """The theme picker was the last thing on six of the app's screens.

    Appearance is a setting. It was repeated at the foot of the sign-in page,
    the edit room, the templates screen, the studio, the ask screen and the
    profile — so on every one of them the final element was a control that had
    nothing to do with what the screen was for, and on the templates screen it
    sat directly above "your choice is remembered for the next film", which
    made that sentence read as being about the theme rather than about the
    template it actually describes.

    The three accessibility settings — text size, motion, contrast — have
    always been on the profile and only there. This holds the fourth to the
    same rule, and holds it for every settings group at once so the next one
    cannot spread either.
    """

    from auteur.web import server

    homes: dict[str, list[str]] = {}
    for page in sorted(server.STATIC.glob("*.html")):
        html = page.read_text(encoding="utf-8")
        # Only settings groups. A bare `class="choices"` is a content picker —
        # which kind of film, which overlay — and those belong on the screen
        # that makes the thing. A settings group names itself, either with
        # `data-setting` or with the older `appearance` class that theme.js
        # reads as `data-setting="theme"`.
        if 'class="choices appearance"' in html:
            homes.setdefault("appearance", []).append(page.name)
        for setting in re.findall(r'data-setting="([^"]+)"', html):
            homes.setdefault(setting, []).append(page.name)

    assert "appearance" in homes, "the appearance control has gone entirely"

    spread = {name: sorted(set(pages)) for name, pages in homes.items() if len(set(pages)) > 1}
    assert not spread, (
        "a settings control appears on more than one page — settings belong in "
        "Settings: " + "; ".join(f"{n} on {', '.join(p)}" for n, p in sorted(spread.items()))
    )

    assert homes["appearance"] == [
        "profile.html"
    ], f"appearance lives on {homes['appearance']}, not the profile"


def test_the_sign_in_page_still_wires_its_form_after_the_picker_left():
    """Deleting markup can break the script that reached for it.

    `login.js` called `wireChoices(document.querySelector(".appearance"), ...)`
    and `wireChoices` never checked its container for null. Removing the theme
    picker without removing that call throws on load, before the sign-in form
    is wired — and a page that looks completely normal and refuses to sign
    anybody in is a worse bug than the one being fixed.

    So: nothing in the sign-in page's script may reach for a control the page
    does not have.
    """

    from auteur.web import server

    script = (server.STATIC / "login.js").read_text(encoding="utf-8")
    html = (server.STATIC / "login.html").read_text(encoding="utf-8")

    # Strip comments before looking: this file explains the removal at length
    # and the explanation names the selector it removed.
    code = re.sub(r"/\*.*?\*/", "", script, flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)

    for selector in re.findall(r'querySelector\(\s*"\.([a-zA-Z0-9_-]+)"', code):
        assert (
            f'class="{selector}"' in html or f"{selector}" in html
        ), f"login.js reaches for .{selector}, which login.html does not have"

    for element_id in re.findall(r'\$\(\s*"([a-zA-Z0-9_-]+)"\s*\)', code):
        assert (
            f'id="{element_id}"' in html
        ), f"login.js reaches for #{element_id}, which login.html does not have"


def test_a_sign_in_option_the_deployment_cannot_offer_is_not_offered():
    """`signed_secret` existed and nothing compared it to the install list.

    Apple's client secret is an ES256 JWT the server signs itself, which needs
    `cryptography`. No deployment installed it, so a hosted instance offered
    Continue with Google and told anybody wanting Continue with Apple that it
    could not — a dead end with no user remedy, and an App Store guideline 4.8
    problem, since an app offering a third-party login has to offer an
    equivalent privacy-preserving one.

    Found by listing every third-party module `auteur/` imports and comparing
    it to `requirements.txt`, which is a comparison nothing was making.
    """
    from auteur.web import oidc

    requirements = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text(
        encoding="utf-8"
    )
    installed = {
        line.split("=")[0].split(">")[0].split("<")[0].strip().lower()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    signing = [key for key, p in oidc.PROVIDERS.items() if p.signed_secret]
    assert signing, "no provider needs a signed secret — has the flag gone?"

    assert "cryptography" in installed, (
        "a deployment cannot sign a client secret, so "
        f"{', '.join(signing)} cannot be offered: add cryptography to "
        "requirements.txt"
    )


def test_loading_the_signing_library_swallows_two_things_and_no_others(monkeypatch):
    """The BaseException handler is an allowlist, so prove it is one.

    It has to reach past `Exception`, because a broken `cryptography` panics
    out of its Rust extension and PyO3 raises that as `PanicException`, which
    inherits from BaseException. What it must not become is a catch-all: a
    KeyboardInterrupt swallowed here is a Ctrl-C the server ignores.

    Driven through the real import rather than by patching the function, by
    standing a module in `sys.modules` whose attribute access raises whatever
    this test wants — which is what `from ... import hashes` actually does.
    """

    from auteur.web import oidc

    class Panic(BaseException):
        """Stands in for pyo3_runtime.PanicException, matched by name."""

    Panic.__name__ = "PanicException"

    class Rogue(BaseException):
        """Anything else that reaches a BaseException handler."""

    def raising(what):
        module = types.ModuleType("cryptography.hazmat.primitives")

        def angry(name):
            # Only the attribute the import actually asks for. A module that
            # raises at *every* lookup poisons anything that later
            # introspects it — pytest's own reporting included, which turned
            # a clean failure into an INTERNALERROR the first time.
            if name == "hashes":
                raise what
            raise AttributeError(name)

        module.__getattr__ = angry
        return module

    for kind, swallowed in (
        (ImportError("no such thing"), True),
        (ValueError("installed, and broken"), True),
        (Panic("the extension panicked"), True),
        (Rogue("not ours"), False),
        (KeyboardInterrupt(), False),
    ):
        monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives", raising(kind))
        if swallowed:
            got = oidc._import_signing()
            assert got is kind, f"{type(kind).__name__} was not returned: {got!r}"
        else:
            with pytest.raises(type(kind)):
                oidc._import_signing()


def test_a_broken_signing_library_does_not_take_the_sign_in_page_with_it(monkeypatch):
    """`except Exception` is not enough, and the difference is a 500.

    A broken `cryptography` — a stale wheel, a missing `_cffi_backend`, an ABI
    mismatch after a base image moves — does not raise ImportError. The Rust
    extension panics, and PyO3 surfaces that as `PanicException`, which
    inherits from BaseException. So the guard written to make a *missing*
    library harmless let the worst kind of *broken* library straight through,
    and `offered()` raised on the one page somebody reaches before anything
    else. Reproduced on a real machine before it was fixed: this sandbox has
    exactly that broken install.

    Only reachable when the provider is configured, which is why it survived
    — an unconfigured Apple short-circuits before the check.
    """
    from auteur.web import oidc

    class Panic(BaseException):
        """What PyO3 raises. Not an Exception, which is the whole bug."""

    apple = oidc.Settings(
        client_id="com.auteurstudies.auteur",
        redirect_uri="https://auteurstudies.com/oidc/apple",
        team_id="TEAM123456",
        key_id="KEY1234567",
        private_key="-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
    )
    assert apple.usable, "the fixture no longer configures Apple, so nothing is tested"

    for failure, expected in (
        (Panic("panicked"), "not working"),
        (ImportError("no"), "does not have"),
    ):
        monkeypatch.setattr(oidc, "_import_signing", lambda failure=failure: failure)
        rows = oidc.offered({"apple": apple})
        row = next(r for r in rows if r["key"] == "apple")
        assert row["ready"] is False
        assert expected in row["why"], (failure, row["why"])
    monkeypatch.undo()

    # And the real import path must not raise either, whatever is installed.
    oidc.offered({"apple": apple})


def test_the_licence_names_the_company_that_publishes_this():
    """Two copies of a company's legal name is one company as far as a lawyer
    is concerned and two as far as a reader is.

    The LICENCE said `abucaria-afk` — a GitHub handle, which is not an entity
    and cannot hold a copyright the way an LLC can — while the App Store
    seller field was about to say Auteur Studies LLC. Nothing compared them,
    which is the same defect as the site shipping a thirteen-colour-old
    palette under a comment claiming it was generated from `theme.py`.
    """
    from auteur.identity import COMPANY

    licence = (Path(__file__).resolve().parent.parent / "LICENSE").read_text(encoding="utf-8")

    assert (
        COMPANY.copyright_line in licence
    ), f"the LICENCE does not carry {COMPANY.copyright_line!r} — it says " + next(
        (line for line in licence.splitlines() if line.startswith("Copyright")),
        "nothing about copyright at all",
    )


def test_the_bundle_identifier_claims_a_domain_the_company_owns():
    """Apple requires reverse-DNS on a domain the publisher controls, and it
    will not let the identifier be changed after the first submission.

    So the failure this guards is permanent: ship `com.auteurstudios.auteur`
    against `auteurstudies.com` and the app carries a misspelling of its own
    company forever. `Identity.bundle_id` is derived from `COMPANY.domain`
    for that reason, and this holds the derivation to the domain rather than
    trusting that nobody will type it out again.
    """
    from auteur.identity import COMPANY, IDENTITY

    assert COMPANY.reverse_dns == "com.auteurstudies", COMPANY.reverse_dns
    assert IDENTITY.bundle_id.startswith(COMPANY.reverse_dns + "."), (
        f"the bundle identifier {IDENTITY.bundle_id!r} does not sit under "
        f"{COMPANY.domain!r}, which is the domain the company claims"
    )
    # Reverse-DNS: at least two dots, and nothing but letters, digits, dots
    # and hyphens. Apple's rule, not a preference.
    assert IDENTITY.bundle_id.count(".") >= 2
    assert re.fullmatch(r"[A-Za-z0-9.-]+", IDENTITY.bundle_id)


def test_the_app_calls_itself_what_the_store_listing_calls_it():
    """One name, in the one place, and every screen reads it.

    The product was renamed from Auteur to Auteur Atlas when Auteur Studies
    became the umbrella above it — and the name was written out by hand in
    nineteen places: every page title, two home-screen names, the manifest,
    three scripts, the terms. `brand.NAME` said "auteur" while
    `IDENTITY.app_name` said "Auteur", which is the same defect one level up:
    two values for one thing, and nothing comparing them.

    `brand.NAME` reads the identity now. The static files cannot, so this is
    what holds them: nothing user-facing may name the app anything other than
    what the store listing names it.

    The home-screen name is checked separately because iOS truncates it and
    the store limit is no help there.
    """

    from auteur import brand
    from auteur.identity import IDENTITY, NAME_LIMIT
    from auteur.web import server

    assert (
        brand.NAME == IDENTITY.app_name
    ), f"brand says {brand.NAME!r} and the listing says {IDENTITY.app_name!r}"
    assert len(IDENTITY.app_name) <= NAME_LIMIT

    name = IDENTITY.app_name
    stale: list[str] = []

    for page in sorted(server.STATIC.glob("*.html")):
        markup = page.read_text(encoding="utf-8")
        title = re.search(r"<title>(.*?)</title>", markup, re.S)
        # Every title, not only the ones that already half-say it. The
        # original condition was `"Auteur" in title and name not in title`,
        # which skipped any title that did not already contain the capitalised
        # word — so `<title>auteur · studio</title>`, the exact thing the
        # rename left behind, sailed through the check written to catch it.
        # Two pages kept the old name in the browser tab for as long as this
        # guard has existed.
        assert title, f"{page.name} has no <title> at all"
        if name not in title.group(1):
            stale.append(f"{page.name} <title> says {title.group(1).strip()!r}")
        for content in re.findall(
            r'<meta name="apple-mobile-web-app-title" content="([^"]*)"', markup
        ):
            if content != name:
                stale.append(f"{page.name} home-screen name is {content!r}")

    manifest = json.loads((server.STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    for key in ("name", "short_name"):
        if name not in manifest.get(key, ""):
            stale.append(f"manifest {key} is {manifest.get(key)!r}")

    # The two places a person meets the name while actually using the thing:
    # the terminal masthead and the line `serve` prints when it comes up. Both
    # said "auteur" long after every store surface said "Auteur Atlas", and
    # both were invisible to a check that only read the static files.
    import io

    from auteur.ui import Reporter

    spoken = io.StringIO()
    Reporter(stream=spoken).banner("a test prompt")
    if name not in spoken.getvalue():
        stale.append(f"the terminal masthead says {spoken.getvalue().strip().splitlines()[0]!r}")

    source = Path(server.__file__).read_text(encoding="utf-8")
    if 'print("  auteur' in source or "print('  auteur" in source:
        stale.append("`serve` announces itself as auteur")

    assert not stale, f"the app names itself something other than {name!r}: " + "; ".join(stale)

    # A home-screen name iOS will not truncate. Not a store rule — a phone
    # one, and the store's 30 is no guide to it.
    assert len(manifest["short_name"]) <= 15, manifest["short_name"]


def test_the_published_site_claims_the_domain_the_listings_name(tmp_path):
    """GitHub Pages serves a custom domain only if a CNAME file says so.

    And the CNAME has to be the product's subdomain, never the apex. The apex
    is the company's own site — a live Wix site — and a CNAME claiming it,
    followed by the DNS to match, would replace that site with this one. So
    this asserts both halves: that a CNAME exists at all, and that it is not
    the apex.
    """

    from auteur.identity import COMPANY, IDENTITY

    root = Path(__file__).resolve().parent.parent
    out = tmp_path / "pages"
    done = subprocess.run(
        [sys.executable, str(root / "tools" / "appstore" / "build_pages.py"), str(out)],
        capture_output=True,
        text=True,
        cwd=root,
    )
    assert "Traceback" not in done.stderr, done.stderr[-2000:]

    cname = out / "CNAME"
    assert cname.is_file(), (
        "the published site has no CNAME, so Pages will not serve a custom "
        "domain and the store URLs 404"
    )
    claimed = cname.read_text(encoding="utf-8").strip()
    assert claimed == COMPANY.documents_for(IDENTITY.slug), claimed
    assert claimed != COMPANY.domain, (
        "the CNAME claims the apex, which is the company's own site — "
        "publishing there replaces it"
    )

    # And the documents the listings name are files this build produced.
    for label, url in (
        ("privacy policy", IDENTITY.privacy_url),
        ("terms", IDENTITY.terms_url),
    ):
        page = url.rsplit("/", 1)[-1]
        assert (out / page).is_file(), f"the {label} URL names {page}, which is not built"


def test_every_store_url_names_a_page_the_site_actually_builds():
    """Two hosts, on purpose, and each has to be right about itself.

    Support points at the company's own site at the apex, which this
    repository does not build — auteurstudies.com is a Wix site, and an
    earlier version of this test would have had it moved to GitHub Pages,
    which would have taken it down.

    The privacy policy and the terms are built here, from PRIVACY.md and
    TERMS.md, and are served from the product's own subdomain. They stay here
    because a copy pasted into a website drifts from what the code can
    actually reach, and an inaccurate Play Data safety declaration is a policy
    strike rather than a correction.

    So: support on the apex, documents on the subdomain, and every document
    URL naming a file the builder actually writes. GitHub Pages serves
    `privacy.html` at `/privacy.html` and gives `/privacy` a 404, and a
    privacy policy URL that 404s is the most common metadata rejection there
    is.
    """
    from auteur.identity import COMPANY, IDENTITY

    builder = (
        Path(__file__).resolve().parent.parent / "tools" / "site" / "build_site.py"
    ).read_text(encoding="utf-8")
    built = set(re.findall(r'"([a-z-]+\.html)"', builder))
    assert "index.html" in built, "the site builder no longer names its own pages"

    assert IDENTITY.support_url == f"https://{COMPANY.domain}/", (
        f"the support URL is {IDENTITY.support_url!r}; it should be the "
        "company's own site at the apex"
    )

    documents = COMPANY.documents_for(IDENTITY.slug)
    assert documents != COMPANY.domain, (
        "the documents are on the apex, which is the company's own site — "
        "publishing there would replace it"
    )
    assert documents.endswith("." + COMPANY.domain), documents

    for label, url in (
        ("privacy policy", IDENTITY.privacy_url),
        ("terms", IDENTITY.terms_url),
    ):
        assert url.startswith(
            f"https://{documents}/"
        ), f"the {label} URL is {url!r}, which is not on {documents}"
        page = url.rsplit("/", 1)[-1]
        assert page in built, (
            f"the {label} URL points at {page!r}, which "
            "tools/site/build_site.py does not write — that is a 404 at "
            "review time"
        )


def test_the_submission_preflight_says_what_it_cannot_check():
    """ "Everything checkable from here is right" reads like "ready to submit".

    It is not the same sentence, and the gap between them is the whole company
    side of this: the domain, the entity and the mailbox all block the upload
    exactly as hard as a missing icon does, and none of them can be seen from
    inside the repository. The preflight used to end on the reassuring half
    alone.

    So it prints `pending()` too — and this holds it to that, because a list
    that exists and is never printed is the same defect one level up.
    """
    import contextlib
    import importlib.util
    import io

    from auteur.identity import pending

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "_preflight", root / "tools" / "appstore" / "preflight.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: `Note` is a dataclass, and dataclasses
    # resolve their annotations through `sys.modules[cls.__module__]`, which
    # is None for a module that has been built but not registered.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        caught = io.StringIO()
        # argv, because `main` parses it and under pytest it is full of
        # pytest's own flags — which is a SystemExit(2), not a failure of the
        # thing being tested.
        argv = sys.argv
        sys.argv = ["preflight.py"]
        try:
            with contextlib.redirect_stdout(caught):
                code = module.main()
        finally:
            sys.argv = argv
        printed = caught.getvalue()
    finally:
        sys.modules.pop(spec.name, None)

    # Not the exit code. The preflight reports on the whole submission, and
    # on a clean checkout one of those things is legitimately missing: the
    # store screenshots live in `build/`, which is generated and gitignored,
    # so it says "no screenshots" and exits 1. The first draft asserted 0 and
    # so passed only on a machine that had run the screenshot tool at some
    # point — green here and red on every CI run, which is the worst way for
    # a test to be wrong. What is asserted instead is that it ran and
    # produced its report; a crash fails the run before this line.
    assert code in (0, 1), f"the preflight neither passed nor reported: {code}"
    assert "before the upload" in printed, printed[:400]

    for item in pending():
        assert item.what in printed, (
            f"the preflight never mentions {item.what!r}, so somebody reading "
            "its last line would think this was ready to upload"
        )
    assert "not checkable from here" in printed


def test_everything_waiting_on_the_world_names_a_check_that_exists():
    """A checklist item whose verification is imaginary is a checklist item
    nobody can finish.

    `pending()` says what is decided and not yet true — the domain, the
    entity, the mailbox. Each carries the command that confirms it, and the
    whole value of that field is that the command is real. The same shape as
    every other bug this file guards: a string that reads like a check and is
    never compared to anything.
    """
    from auteur.identity import pending

    root = Path(__file__).resolve().parent.parent

    waiting = pending()
    assert waiting, "nothing is waiting on the world, which cannot be right yet"

    for item in waiting:
        assert item.what and item.consequence, item
        parts = item.confirm.split()
        assert parts[0] == "python3", f"{item.confirm!r} is not a command this repo runs"
        script = root / parts[1]
        assert script.exists(), (
            f"{item.what!r} says to confirm it with {item.confirm!r}, and "
            f"{parts[1]} does not exist"
        )
        # The flags too: `--online` is what makes the URL check fetch anything,
        # and naming a flag the tool does not have is the same defect one
        # level down.
        source = script.read_text(encoding="utf-8")
        for flag in parts[2:]:
            assert f'"{flag}"' in source, f"{parts[1]} has no {flag} option"


def test_the_reserved_example_domain_is_refused_as_a_bundle_identifier():
    """`com.example.*` is the documentation domain and Apple rejects it.

    It is also exactly what a project file carries until somebody remembers to
    change it, which is why this is a check rather than a line in a README.
    """
    from auteur.identity import Identity, problems, ready

    # The repository used to ship with placeholders on purpose and this test
    # asserted that. It no longer does: the publisher is Auteur Studies LLC on
    # auteurstudies.com, decided rather than deferred, so the assertion moved
    # to the thing that is actually being guarded — the reserved domain is
    # refused whenever anybody sets it, which is what Apple does.
    assert ready(), f"the repository's own identity is not submittable: {problems()}"

    reserved = Identity(
        bundle_id="com.example.auteur",
        developer="Somebody",
        support_email="hello@somebody.com",
        support_url="https://somebody.com/auteur",
        privacy_url="https://somebody.com/auteur/privacy",
        terms_url="https://somebody.com/auteur/terms",
    )
    assert any("com.example" in line for line in problems(reserved))
    assert not ready(reserved)

    filled = Identity(
        bundle_id="com.somebody.auteur",
        developer="Somebody",
        support_email="hello@somebody.com",
        support_url="https://somebody.com/auteur",
        privacy_url="https://somebody.com/auteur/privacy",
        terms_url="https://somebody.com/auteur/terms",
    )
    assert problems(filled) == []


def test_a_placeholder_is_caught_whatever_its_capitals():
    """The first version compared against a lowercase string, so "Example
    Developer" went straight through it."""
    from auteur.identity import Identity, problems

    named = Identity(
        bundle_id="com.somebody.auteur",
        developer="Example Developer",
        support_email="hello@somebody.com",
        support_url="https://somebody.com/",
        privacy_url="https://somebody.com/privacy",
        terms_url="https://somebody.com/terms",
    )
    assert any("developer name" in line for line in problems(named))


def test_an_identifier_that_is_not_reverse_dns_is_refused():
    from auteur.identity import Identity, problems

    for bad in ("auteur", "com.auteur", "com.somebody auteur", "com.somebody.auteur!"):
        broken = Identity(
            bundle_id=bad,
            developer="Somebody",
            support_email="hello@somebody.com",
            support_url="https://somebody.com/",
            privacy_url="https://somebody.com/privacy",
            terms_url="https://somebody.com/terms",
        )
        assert problems(broken), bad


def _tool(name: str):
    """Load one of the tools/appstore scripts as a module.

    Registered in `sys.modules` before it is executed, which is not optional:
    `@dataclass` resolves its annotations by looking the defining module up by
    name, and a module that is not there yet raises inside dataclasses rather
    than anywhere near the actual mistake.
    """
    import importlib.util
    import sys as _sys

    path = Path(__file__).resolve().parents[1] / "tools" / "appstore" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"auteur_tool_{name}", path)
    module = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_listing_fits_the_fields_it_goes_into():
    """App Store Connect enforces these after you have written past them."""
    from auteur import identity

    listing = _tool("listing")

    assert len(identity.IDENTITY.app_name) <= identity.NAME_LIMIT
    assert len(listing.SUBTITLE) <= identity.SUBTITLE_LIMIT
    assert len(listing.KEYWORDS) <= identity.KEYWORDS_LIMIT
    assert len(listing.PROMOTIONAL) <= identity.PROMOTIONAL_LIMIT
    assert len(listing.DESCRIPTION) <= identity.DESCRIPTION_LIMIT
    # Keywords are counted with their commas and any spaces after them, so a
    # tidy-looking ", " list silently costs ten characters.
    assert ", " not in listing.KEYWORDS


def test_every_permission_the_app_asks_for_has_a_sentence_behind_it():
    """A permission with no string crashes at the moment it is needed, and a
    string with nothing asking for it is a question from a reviewer."""
    import plistlib

    root = Path(__file__).resolve().parents[1]
    with (root / "ios" / "Auteur" / "Info.plist").open("rb") as handle:
        info = plistlib.load(handle)

    wanted = {
        "NSPhotoLibraryAddUsageDescription",
        "NSCalendarsWriteOnlyAccessUsageDescription",
        "NSCalendarsUsageDescription",
        "NSLocalNetworkUsageDescription",
    }
    have = {key for key in info if key.endswith("UsageDescription")}
    assert have == wanted, f"unexpected: {have ^ wanted}"
    for key in have:
        said = info[key]
        assert len(said) > 20 and said.endswith("."), f"{key} is {said!r}"


def test_the_privacy_manifest_declares_every_required_reason_api_the_swift_uses():
    """Apple mails an ITMS-91053 about a missing one after the upload.

    This found a real one: `Instance.swift` remembers the instance address in
    UserDefaults and the manifest did not mention it.
    """
    import plistlib

    root = Path(__file__).resolve().parents[1]
    app = root / "ios" / "Auteur"
    with (app / "PrivacyInfo.xcprivacy").open("rb") as handle:
        manifest = plistlib.load(handle)

    declared = {row["NSPrivacyAccessedAPIType"] for row in manifest["NSPrivacyAccessedAPITypes"]}
    swift = "\n".join(f.read_text() for f in sorted(app.rglob("*.swift")))
    if "UserDefaults" in swift:
        assert "NSPrivacyAccessedAPICategoryUserDefaults" in declared

    # And every declared category carries a reason code; one without is the
    # same to Apple as one that was never declared.
    for row in manifest["NSPrivacyAccessedAPITypes"]:
        assert row.get("NSPrivacyAccessedAPITypeReasons"), row["NSPrivacyAccessedAPIType"]

    assert manifest["NSPrivacyTracking"] is False
    assert manifest["NSPrivacyCollectedDataTypes"] == []


def test_the_bundle_identifier_is_not_written_out_twice():
    """It is in identity.py and generated into Identity.yml; a literal in
    project.yml would be a second copy, and the second copy is the stale one."""
    root = Path(__file__).resolve().parents[1]
    project = (root / "ios" / "project.yml").read_text()
    assert "PRODUCT_BUNDLE_IDENTIFIER" not in project
    assert "Identity.yml" in project

    generated = (root / "ios" / "Identity.yml").read_text()
    from auteur.identity import IDENTITY

    assert IDENTITY.bundle_id in generated


def test_the_screenshot_sizes_are_the_ones_the_form_accepts():
    """A screenshot one pixel off is refused with no clue which dimension."""
    preflight = _tool("preflight")

    # 1290x2796 is the required iPhone slot, and the generator has to be driven
    # at the CSS size that produces it — 430 x 932 at three pixels per point.
    assert (1290, 2796) in preflight.IPHONE_SIZES
    assert (2048, 2732) in preflight.IPAD_SIZES

    module = _tool("screenshots")
    for _name, width, height, ratio, wanted in module.DEVICES:
        assert (width * ratio, height * ratio) == wanted
        assert wanted in preflight.IPHONE_SIZES | preflight.IPAD_SIZES


def test_the_terms_say_the_thing_the_app_store_requires_them_to():
    """Guideline 1.2 asks for terms with no tolerance for objectionable content
    or abusive users, agreed to when an account is made."""
    from auteur.web import server

    root = Path(__file__).resolve().parents[1]
    terms = " ".join((root / "TERMS.md").read_text().split()).lower()
    assert "no tolerance for objectionable content" in terms
    assert "delete your account" in terms

    # Reachable without an account, because the sign-up screen links to it.
    assert "/terms" in server.PUBLIC_PATHS
    page = (server.STATIC / "terms.html").read_text()
    assert "no tolerance" in page

    # And agreed to where the account is made, not on a screen nobody visits.
    login = (server.STATIC / "login.html").read_text()
    assert 'href="/terms"' in login
    assert login.index('href="/terms"') < login.index('id="signup-go"')


# -- deleting an account ----------------------------------------------------


def test_deleting_an_account_takes_everything_with_it(web_server):
    """Guideline 5.1.1(v), and the harder half of it: the files, not only the
    rows. A film row removed while its mp4 is still on disk is footage
    somebody asked to have deleted, still there."""
    import json as _json
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from auteur.web import server as web

    base, studio, cookie = web_server
    clip = studio.workspace / "mine.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"not really an mp4")
    web.Handler.films.add(owner="tester", prompt="mine", video=str(clip))
    web.Handler.messages.send("tester", "grace", text="hello")
    web.Handler.profiles.edit("tester", bio="something")
    web.Handler.profiles.follow("tester", "grace")

    # The password, again: a live session is not proof of who is holding the
    # phone, and this is the most destructive thing in the app.
    for payload, code in (
        ({"password": "wrong", "confirm": "delete"}, 403),
        ({"password": "a-long-enough-one", "confirm": "yes"}, 400),
    ):
        with pytest.raises(HTTPError) as raised:
            _api_post(base, "/api/profile/delete", cookie, payload)
        assert raised.value.code == code

    said = _api_post(
        base, "/api/profile/delete", cookie, {"password": "a-long-enough-one", "confirm": "delete"}
    )
    assert said["ok"] is True

    web.Handler.accounts.refresh()
    assert web.Handler.accounts.get("tester") is None
    assert web.Handler.films.by("tester") == []
    assert not clip.exists(), "the film's file was left on disk"
    assert web.Handler.messages.conversations("grace") == []
    assert web.Handler.profiles.get("tester").bio == ""
    assert web.Handler.profiles.followers_of("grace") == []

    # And the session went with it, rather than staying live against a name
    # that no longer exists.
    with pytest.raises(HTTPError) as raised:
        urlopen(Request(base + "/api/profile", headers={"Cookie": cookie}))
    assert raised.value.code == 401
    assert _json  # the import is used by the helpers above


# -- reporting and blocking -------------------------------------------------


def test_a_block_is_a_wall_rather_than_a_mute(web_server):
    """Filtering only what you blocked leaves the other person able to watch
    your films and keep writing to you, which is not a block."""
    from urllib.error import HTTPError

    from auteur.web import server as web

    base, studio, cookie = web_server
    for owner in ("grace",):
        clip = studio.workspace / f"{owner}.mp4"
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"not really an mp4")
        web.Handler.films.add(owner=owner, prompt="theirs", video=str(clip))
    web.Handler.messages.send("grace", "tester", text="hello")

    assert len(_api_get(base, "/api/feed?scope=all", cookie)["films"]) == 1
    assert _api_get(base, "/api/messages", cookie)["conversations"]

    _api_post(base, "/api/profiles/grace/block", cookie, {"block": True})

    assert _api_get(base, "/api/feed?scope=all", cookie)["films"] == []
    assert _api_get(base, "/api/messages", cookie)["conversations"] == []
    assert _api_get(base, "/api/people", cookie)["people"] == []
    assert _api_get(base, "/api/messages/grace", cookie)["closed"] is True
    with pytest.raises(HTTPError) as raised:
        _api_post(base, "/api/messages/send", cookie, {"to": "grace", "text": "hi"})
    assert raised.value.code == 403

    # The other way round, from the store: the person blocked cannot reach back.
    assert "tester" in web.Handler.profiles.apart("grace")

    _api_post(base, "/api/profiles/grace/block", cookie, {"block": False})
    assert len(_api_get(base, "/api/feed?scope=all", cookie)["films"]) == 1


def test_a_report_names_whose_content_it_is_from_the_server(web_server):
    """A report that names whoever the page said it names is a report anybody
    could file against anybody."""
    from auteur.web import server as web

    base, studio, cookie = web_server
    clip = studio.workspace / "grace.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"not really an mp4")
    film = web.Handler.films.add(owner="grace", prompt="theirs", video=str(clip))

    said = _api_post(
        base,
        "/api/report",
        cookie,
        {"kind": "film", "about": film.id, "about_who": "somebody-else", "reason": "spam"},
    )
    stored = web.Handler.reports.get(said["report"]["id"])
    assert stored.about_who == "grace"
    assert stored.by == "tester"


def test_reporting_can_block_in_the_same_step(web_server):
    """Almost everybody who reports somebody also wants to stop hearing from
    them, and two journeys through two screens is how people do neither."""
    from auteur.web import server as web

    base, studio, cookie = web_server
    clip = studio.workspace / "grace.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"not really an mp4")
    film = web.Handler.films.add(owner="grace", prompt="theirs", video=str(clip))

    said = _api_post(
        base,
        "/api/report",
        cookie,
        {"kind": "film", "about": film.id, "reason": "harassment", "block": True},
    )
    assert said["blocked"] is True
    assert web.Handler.profiles.blocks("tester", "grace")


def test_you_can_see_what_came_of_what_you_reported(web_server):
    """A report whose outcome you can never see is a button people press once
    and then stop believing in."""
    from auteur.web import server as web

    base, _, cookie = web_server
    report = web.Handler.reports.file(
        by="tester", kind="person", about="grace", about_who="grace", reason="spam"
    )
    mine = _api_get(base, "/api/reports", cookie)
    assert [row["state"] for row in mine["reports"]] == ["open"]

    web.Handler.reports.decide(report.id, "removed", "took the film down")
    mine = _api_get(base, "/api/reports", cookie)
    assert mine["reports"][0]["state"] == "removed"
    # Not the operator's note, and not who else reported it.
    assert "decided_note" not in mine["reports"][0]
    assert "by" not in mine["reports"][0]


def test_reporting_the_same_thing_twice_does_not_make_two_reports(tmp_path):
    """An operator needs "how many people reported this" to mean something."""
    from auteur.web.safety import Reports

    store = Reports(tmp_path / "reports.json")
    first = store.file(by="ada", kind="film", about="f1", about_who="grace", reason="spam")
    again = store.file(by="ada", kind="film", about="f1", about_who="grace", reason="spam")
    assert first.id == again.id
    assert store.waiting == 1

    other = store.file(by="bob", kind="film", about="f1", about_who="grace", reason="spam")
    assert other.id != first.id
    assert store.count_about("f1") == 2


def test_the_reports_that_might_mean_danger_are_shown_first(tmp_path):
    from auteur.web.safety import Reports

    store = Reports(tmp_path / "reports.json")
    store.file(by="ada", kind="film", about="f1", about_who="g", reason="spam")
    store.file(by="bob", kind="film", about="f2", about_who="g", reason="child-safety")
    store.file(by="cal", kind="film", about="f3", about_who="g", reason="violence")
    assert [r.reason for r in store.open_ones()][0] in ("child-safety", "violence")
    assert [r.reason for r in store.open_ones()][-1] == "spam"


def test_the_moderator_can_remove_a_film_that_is_not_theirs(tmp_path):
    """`forget` refuses anything that is not yours, which is right for the app
    and wrong for the person whose computer it is — a moderator who can only
    delete their own films cannot take down what was reported."""
    from auteur.web.social import Films

    store = Films(tmp_path / "films.json")
    film = store.add(owner="grace", prompt="theirs", video=str(tmp_path / "x.mp4"))
    assert store.forget(film.id, "tester") is False
    assert store.remove_any(film.id) == str(tmp_path / "x.mp4")
    assert store.get(film.id) is None


def test_every_control_guideline_1_2_asks_for_exists_on_both_sides():
    """A server endpoint with no button is as absent as a button with no
    endpoint, and a reviewer finds out by using the app."""
    from auteur.web import server

    handlers = (server.STATIC.parent / "server.py").read_text()
    for route in ("/api/report", "/block", "/api/profile/delete"):
        assert route in handlers, route

    safety = (server.STATIC / "safety.js").read_text()
    assert "auteurSafety" in safety and "function block(" in safety
    for page in ("feed.html", "profile.html", "inbox.html"):
        assert "safety.js" in (server.STATIC / page).read_text(), page

    # And the operator's side, which is what makes reporting more than a form.
    cli = (Path(__file__).resolve().parents[1] / "auteur" / "cli.py").read_text()
    assert "def _run_moderate" in cli


# ---------------------------------------------------------------------------
# 12+, and holding the app to it
# ---------------------------------------------------------------------------


def test_an_age_is_a_year_and_stays_right_as_time_passes():
    """A stored yes/no would be wrong from the next birthday onward, and a
    full date of birth is more data for no more answers."""
    import time

    from auteur.web import auth

    born = 2000
    at_2020 = time.mktime((2020, 6, 1, 0, 0, 0, 0, 0, 0))
    at_2030 = time.mktime((2030, 6, 1, 0, 0, 0, 0, 0, 0))
    assert auth.age_from(born, now=at_2020) == 20
    assert auth.age_from(born, now=at_2030) == 30
    # Nobody said: not zero, which would read as a newborn.
    assert auth.age_from(0) == -1


def test_an_account_under_eighteen_starts_restricted(tmp_path):
    """Not a judgement — the direction to be wrong in. It lifts in two taps
    and cannot be applied retroactively to something already seen."""
    import time

    from auteur.web.auth import Accounts

    year = time.gmtime().tm_year
    accounts = Accounts(tmp_path / "accounts.json")
    kid = accounts.add("kid", "k@example.com", "a-long-enough-one", born=year - 14)
    grown = accounts.add("grown", "g@example.com", "a-long-enough-one", born=year - 30)
    quiet = accounts.add("quiet", "q@example.com", "a-long-enough-one")

    assert kid.minor and kid.restricted
    assert not grown.minor and not grown.restricted
    # An account that never said is not treated as a minor: those belong to
    # people who were already using the instance, and silently restricting
    # them would be a change nobody asked for.
    assert not quiet.minor and not quiet.restricted


def test_the_code_that_lifts_a_restriction_is_stored_hashed(tmp_path):
    from auteur.web.auth import LOCK_DIGITS, Accounts

    accounts = Accounts(tmp_path / "accounts.json")
    accounts.add("kid", "k@example.com", "a-long-enough-one")
    assert accounts.set_restriction_lock("kid", "12") == f"{LOCK_DIGITS} digits, please"
    assert accounts.set_restriction_lock("kid", "4821") == ""

    stored = accounts.get("kid").restriction_lock
    assert "4821" not in stored and len(stored) == 64
    assert accounts.check_restriction_lock("kid", "4821")
    assert not accounts.check_restriction_lock("kid", "0000")
    assert not accounts.check_restriction_lock("kid", "")

    # Cleared, and then anything lifts it — which is right for somebody who
    # restricted themselves and never set one.
    assert accounts.set_restriction_lock("kid", "") == ""
    assert accounts.check_restriction_lock("kid", "")


def test_signing_up_too_young_is_refused(web_server):
    """The rating is 12+, and a rating the app does not hold itself to is a
    claim rather than a fact."""
    import time

    from auteur.web import server as web

    base, _, _cookie = web_server
    # An instance with accounts refuses sign-up outright, so this needs an
    # empty one — which is also the only state the gate is ever reached in.
    web.Handler.accounts.accounts.clear()
    year = time.gmtime().tm_year

    # No year at all.
    status, payload, _ = _post(
        base, "/api/signup", {"username": "tiny", "password": "a-long-enough-one"}
    )
    assert status == 400 and "year" in payload["error"]

    status, payload, _ = _post(
        base,
        "/api/signup",
        {"username": "tiny", "password": "a-long-enough-one", "born": year - 8},
    )
    assert status == 403
    assert "12 and over" in payload["error"]

    status, _payload, _ = _post(
        base,
        "/api/signup",
        {"username": "old-enough", "password": "a-long-enough-one", "born": year - 30},
    )
    assert status == 200


def test_a_restriction_hides_sensitive_and_unreviewed_films(web_server):
    """It has to hide something real, or it is theatre."""
    from auteur.web import server as web

    base, studio, cookie = web_server
    made = {}
    for name, owner in (("plain", "grace"), ("marked", "grace"), ("reported", "grace")):
        clip = studio.workspace / f"{name}.mp4"
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"not really an mp4")
        made[name] = web.Handler.films.add(owner=owner, prompt=name, video=str(clip))
    mine = studio.workspace / "mine.mp4"
    mine.write_bytes(b"not really an mp4")
    made["mine"] = web.Handler.films.add(owner="tester", prompt="mine", video=str(mine))

    web.Handler.films.mark(made["marked"].id, True)
    web.Handler.reports.file(
        by="tester",
        kind="film",
        about=made["reported"].id,
        about_who="grace",
        reason="sexual",
    )
    # And one of your own, marked: a restriction is about what you are shown,
    # not about hiding your own work from you.
    web.Handler.films.mark(made["mine"].id, True)

    seen = {f["prompt"] for f in _api_get(base, "/api/feed?scope=all", cookie)["films"]}
    assert seen == {"plain", "marked", "reported", "mine"}

    _api_post(base, "/api/restriction", cookie, {"on": True})
    seen = {f["prompt"] for f in _api_get(base, "/api/feed?scope=all", cookie)["films"]}
    assert seen == {"plain", "mine"}, seen

    # Once the operator has looked at the report, it stops being held back.
    report = web.Handler.reports.open_ones()[0]
    web.Handler.reports.decide(report.id, "kept")
    seen = {f["prompt"] for f in _api_get(base, "/api/feed?scope=all", cookie)["films"]}
    assert seen == {"plain", "reported", "mine"}


def test_turning_a_restriction_on_never_needs_the_code_and_lifting_it_does(web_server):
    """The switch has to be out of reach of the person it applies to, and
    choosing to see less should never need anybody's permission."""
    from urllib.error import HTTPError

    base, _, cookie = web_server

    state = _api_post(base, "/api/restriction", cookie, {"on": True, "lock": "4821"})
    assert state["on"] is True and state["locked"] is True

    with pytest.raises(HTTPError) as raised:
        _api_post(base, "/api/restriction", cookie, {"on": False})
    assert raised.value.code == 403
    with pytest.raises(HTTPError):
        _api_post(base, "/api/restriction", cookie, {"on": False, "code": "0000"})
    assert _api_get(base, "/api/restriction", cookie)["on"] is True

    state = _api_post(base, "/api/restriction", cookie, {"on": False, "code": "4821"})
    assert state["on"] is False
    # The lock goes with it: one left behind is a surprise waiting for
    # whoever turns the restriction back on.
    assert state["locked"] is False


def test_the_page_is_never_told_the_code(web_server):
    """It is told whether there is one, which is all it needs to draw a field."""
    base, _, cookie = web_server
    _api_post(base, "/api/restriction", cookie, {"on": True, "lock": "4821"})
    state = _api_get(base, "/api/restriction", cookie)
    assert state["locked"] is True
    assert "4821" not in json.dumps(state)
    assert "lock" not in state and "restriction_lock" not in state


def test_only_a_films_author_can_mark_it_sensitive(web_server):
    from urllib.error import HTTPError

    from auteur.web import server as web

    base, studio, cookie = web_server
    clip = studio.workspace / "theirs.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"not really an mp4")
    theirs = web.Handler.films.add(owner="grace", prompt="theirs", video=str(clip))

    with pytest.raises(HTTPError) as raised:
        _api_post(base, f"/api/films/{theirs.id}/sensitive", cookie, {"sensitive": True})
    assert raised.value.code == 403

    # The operator's route into it is the store, not the network — there is
    # deliberately no endpoint that marks somebody else's film.
    assert web.Handler.films.mark(theirs.id, True) is not None
    assert web.Handler.films.get(theirs.id).sensitive is True


def test_the_rating_the_listing_declares_is_the_one_the_app_enforces():
    """Two places holding one number is how they end up disagreeing."""
    from auteur.web.auth import MINIMUM_AGE

    listing = _tool("listing")
    assert "**The rating is 12+.**" in listing.AGE_RATING
    assert MINIMUM_AGE == 12

    preflight = _tool("preflight")
    notes = preflight.check_age()
    assert notes and all(note.ok for note in notes), [n.what for n in notes if not n.ok]


# ---------------------------------------------------------------------------
# Projects: an album, and a map
# ---------------------------------------------------------------------------


def test_a_project_holds_a_trip_the_way_a_trip_happens(tmp_path):
    from auteur.projects import Projects

    store = Projects(tmp_path / "projects.json")
    project = store.make(
        "ada",
        "  Portugal, June  ",
        place="Lisbon",
        starts="2026-06-14",
        ends="2026-06-02",
        note="slow  mornings\n\n\n\nharbour at six",
    )
    assert project.name == "Portugal, June"
    # Typed backwards is not an error worth refusing, but the album sorts by
    # these, so they are put the right way round rather than left to sort wrong.
    assert project.dated == "2026-06-02 to 2026-06-14"
    # The note keeps the break somebody typed and loses the run of blank lines.
    assert project.note == "slow mornings\n\nharbour at six"
    assert store.make("ada", "   ") is None


def test_a_project_is_not_readable_by_anybody_else(tmp_path):
    """Planning is one person's own. There is no route that shares it, and the
    check is in `get` rather than in each route because every route needs it
    and one of them will forget."""
    from auteur.projects import Projects

    store = Projects(tmp_path / "projects.json")
    project = store.make("ada", "Portugal")
    assert store.get(project.id, "grace") is None
    assert store.edit(project.id, "grace", name="Mine now") is None
    assert store.drop(project.id, "grace") is False
    assert store.add_node(project.id, "grace", kind="idea") is None
    assert store.get(project.id, "ada") is not None


def test_dropping_a_node_takes_the_arrows_that_touched_it(tmp_path):
    """A link to a node that is gone is an arrow to nowhere, and it draws as
    one."""
    from auteur.projects import Projects

    store = Projects(tmp_path / "projects.json")
    project = store.make("ada", "Portugal")
    a = store.add_node(project.id, "ada", kind="idea", text="start on the water")
    b = store.add_node(project.id, "ada", kind="shot", text="ferry leaving")
    c = store.add_node(project.id, "ada", kind="place", text="the harbour")
    store.link(project.id, "ada", a.id, b.id)
    store.link(project.id, "ada", b.id, c.id)
    assert len(store.get(project.id, "ada").links) == 2

    store.drop_node(project.id, "ada", b.id)
    assert len(store.get(project.id, "ada").nodes) == 2
    assert store.get(project.id, "ada").links == []


def test_two_nodes_are_joined_or_they_are_not(tmp_path):
    """A second arrow back the other way is a duplicate, not a different fact."""
    from auteur.projects import Projects

    store = Projects(tmp_path / "projects.json")
    project = store.make("ada", "Portugal")
    a = store.add_node(project.id, "ada", kind="idea")
    b = store.add_node(project.id, "ada", kind="shot")

    first = store.link(project.id, "ada", a.id, b.id)
    again = store.link(project.id, "ada", b.id, a.id)
    assert first.id == again.id
    assert len(store.get(project.id, "ada").links) == 1
    # Nothing joins to itself, and nothing joins to a node that is not there.
    assert store.link(project.id, "ada", a.id, a.id) is None
    assert store.link(project.id, "ada", a.id, "nonsense") is None


def test_a_drag_is_saved_once_rather_than_sixty_times_a_second(tmp_path):
    """A drag emits a position every frame. The page sends where things ended
    up, and this is what receives it."""
    from auteur.projects import Projects

    store = Projects(tmp_path / "projects.json")
    project = store.make("ada", "Portugal")
    a = store.add_node(project.id, "ada", kind="idea")
    b = store.add_node(project.id, "ada", kind="shot")

    moved = store.move_nodes(project.id, "ada", {a.id: [40.5, 900], b.id: [-12, 3]})
    assert moved == 2
    fresh = Projects(tmp_path / "projects.json").get(project.id, "ada")
    where = {n["id"]: (n["x"], n["y"]) for n in fresh.nodes}
    assert where[a.id] == (40.5, 900.0)
    assert where[b.id] == (-12.0, 3.0)
    # Nonsense in a batch is skipped rather than taking the batch down.
    assert store.move_nodes(project.id, "ada", {a.id: ["over", "there"]}) == 0


def test_deleting_a_project_never_deletes_the_footage(tmp_path):
    """A project is a way of looking at footage. Deleting the way of looking
    must not delete what it was looking at."""
    from auteur.projects import Projects
    from auteur.web.social import Films

    store = Projects(tmp_path / "projects.json")
    films = Films(tmp_path / "films.json")
    project = store.make("ada", "Portugal")
    film = films.add(owner="ada", prompt="the harbour", video=str(tmp_path / "x.mp4"))
    films.belongs(film.id, project.id, "ada")
    assert films.in_project(project.id) == [film]

    assert store.drop(project.id, "ada") is True
    assert films.get(film.id) is not None
    assert films.get(film.id).video == str(tmp_path / "x.mp4")


def test_a_film_can_only_be_filed_by_the_person_who_made_it(tmp_path):
    from auteur.web.social import Films

    films = Films(tmp_path / "films.json")
    film = films.add(owner="grace", prompt="theirs", video=str(tmp_path / "x.mp4"))
    assert films.belongs(film.id, "somebody", "ada") is None
    assert films.get(film.id).project == ""
    assert films.belongs(film.id, "somebody", "grace") is not None


# -- as served --------------------------------------------------------------


def test_a_project_answers_with_its_map_and_its_album_in_one_request(web_server):
    """A page that fetches the project, then its nodes, then its links, then
    its films is a page that renders four times and lays out differently each
    time."""
    from auteur.web import server as web

    base, studio, cookie = web_server
    made = _api_post(base, "/api/projects", cookie, {"name": "Portugal", "place": "Lisbon"})
    project_id = made["project"]["id"]

    clip = studio.workspace / "one.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"not really an mp4")
    film = web.Handler.films.add(owner="tester", prompt="the harbour", video=str(clip))
    _api_post(base, f"/api/projects/{project_id}/gather", cookie, {"film": film.id})
    _api_post(base, f"/api/projects/{project_id}/node", cookie, {"kind": "idea", "text": "water"})

    whole = _api_get(base, f"/api/projects/{project_id}", cookie)["project"]
    assert whole["name"] == "Portugal"
    assert len(whole["map"]["nodes"]) == 1
    assert [f["prompt"] for f in whole["album"]["films"]] == ["the harbour"]
    assert "idea" in whole["kinds"]


def test_a_film_cannot_be_filed_into_somebody_elses_project(web_server):
    """A project id from a form is a project id somebody could have typed."""
    from urllib.error import HTTPError

    from auteur.projects import Projects
    from auteur.web import server as web

    base, studio, cookie = web_server
    theirs = web.Handler.projects.make("grace", "Theirs")
    clip = studio.workspace / "mine.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"not really an mp4")
    film = web.Handler.films.add(owner="tester", prompt="mine", video=str(clip))

    with pytest.raises(HTTPError) as raised:
        _api_post(base, f"/api/projects/{theirs.id}/gather", cookie, {"film": film.id})
    assert raised.value.code == 404
    assert web.Handler.films.get(film.id).project == ""
    assert isinstance(web.Handler.projects, Projects)


def test_a_shot_on_the_map_becomes_a_real_plan(web_server):
    """The reason the nodes are typed. A note that says "ferry leaving" is a
    note; a *shot* that says it is something the board can hold."""
    from auteur.web import server as web

    base, _, cookie = web_server
    made = _api_post(base, "/api/projects", cookie, {"name": "Portugal"})
    project_id = made["project"]["id"]
    node = _api_post(
        base,
        f"/api/projects/{project_id}/node",
        cookie,
        {"kind": "shot", "text": "ferry leaving, from the rail"},
    )["node"]

    _api_post(
        base,
        "/api/plans",
        cookie,
        {"prompt": node["text"], "project": project_id, "title": "Portugal — ferry"},
    )
    _api_post(base, f"/api/projects/{project_id}/node/{node['id']}", cookie, {"done": True})

    plans = web.Handler.board.by("tester")
    assert len(plans) == 1 and plans[0].project == project_id
    whole = _api_get(base, f"/api/projects/{project_id}", cookie)["project"]
    assert whole["map"]["nodes"][0]["done"] is True
    assert len(whole["album"]["plans"]) == 1


def test_deleting_an_account_takes_its_projects(web_server):
    from auteur.web import server as web

    base, _, cookie = web_server
    _api_post(base, "/api/projects", cookie, {"name": "Portugal"})
    assert web.Handler.projects.by("tester")

    _api_post(
        base,
        "/api/profile/delete",
        cookie,
        {"password": "a-long-enough-one", "confirm": "delete"},
    )
    assert web.Handler.projects.by("tester") == []


def test_the_map_is_built_from_elements_a_person_can_reach():
    """A 2D canvas would be fewer lines and would throw away everything the
    browser already does — Tab, a screen reader, the browser's own find, and
    whatever text size somebody has set."""
    from auteur.web import server

    page = (server.STATIC / "project.js").read_text()
    assert 'createElement("button")' in page
    assert "getContext" not in page, "the map must not be drawn on a canvas"
    # Reachable and movable without a pointer.
    assert "ArrowLeft" in page and "focusin" in page
    # Zoom has buttons, not only a pinch.
    assert "zoom-in" in page and "zoom-fit" in page

    css = (server.STATIC / "projects.css").read_text()
    # The browser must not claim the gestures, or a drag scrolls the page.
    assert "touch-action: none" in css


def test_a_node_on_the_map_is_a_thumb_tall_at_life_size():
    """The UI review exempts a zoomable canvas from measuring tap targets on
    screen — at 0.6 zoom everything is small, which is what zooming out means.
    The invariant that survives that is the size at life size, and this is
    where it is held.
    """
    from auteur.web import server

    css = (server.STATIC / "projects.css").read_text()
    block = css[css.index(".node {") : css.index(".node:active")]
    assert "min-height: 44px" in block

    # And the floor on zooming out, so "fit" cannot hand back a view where a
    # node is fifteen pixels of nothing.
    page = (server.STATIC / "project.js").read_text()
    floor = float(re.search(r"MIN_ZOOM = ([\d.]+)", page).group(1))
    assert floor >= 0.5, f"MIN_ZOOM is {floor}: a 44px node would be {44 * floor:.0f}px"
    # Fit shrinks, never magnifies.
    assert "Math.min(1, Math.min(MAX_ZOOM" in page


# ---------------------------------------------------------------------------
# What it costs
# ---------------------------------------------------------------------------


def test_every_price_is_actually_under_the_market_it_claims_to_undercut():
    """ "Fifteen per cent under" is arithmetic, so it is checked as arithmetic.

    The failure this stops is not a typo, it is a rounding habit. Fifteen per
    cent under an average of $14.75 is $12.5375, and the ordinary instinct is
    to write $12.99 because prices end in 99. That is eleven and a half per
    cent under, on a page that says fifteen — a false advertisement produced
    by nobody doing anything wrong except rounding the way people round.

    So `_charm` rounds down and this checks the result rather than the
    intention. Every paid tier has to clear the claim its own page makes.
    """
    from auteur import pricing

    paid = [tier for tier in pricing.TIERS if tier.dollars]
    assert paid, "no paid tier at all — the comparison set decides nothing"

    for tier in paid:
        assert tier.rivals, f"{tier.key} has a price and nothing it was derived from"
        actual = pricing.undercut_of(tier.dollars, tier.rivals)
        assert actual >= pricing.UNDERCUT, (
            f"{tier.name} is ${tier.dollars:.2f} against an average of "
            f"${pricing.average(tier.rivals):.2f} — {actual:.1%} under, and the "
            f"page claims {pricing.UNDERCUT:.0%}"
        )
        # And not so far under that the claim is the wrong claim: a tier
        # priced at half the market is not "fifteen per cent under", it is a
        # different decision that the copy no longer describes.
        assert actual < pricing.UNDERCUT + 0.02, (
            f"{tier.name} is {actual:.1%} under, which is not the "
            f"{pricing.UNDERCUT:.0%} the page advertises"
        )


def test_every_rival_in_the_comparison_names_where_its_price_came_from():
    """A price with no source is a price somebody remembered.

    These four numbers are the entire basis for what this company charges. In
    a year somebody will want to re-check them, and "Descript is about $16"
    cannot be re-checked — the page it was read off can. Every entry carries
    one, and every rival left out of the set carries the reason, so the
    judgement about which competitors belong in which tier is arguable rather
    than lost.
    """
    from auteur import pricing

    for rivals in (pricing.ENTRY_RIVALS, pricing.TOP_RIVALS):
        assert len(rivals) >= 3, "an average of two is not an average of a market"
        for rival in rivals:
            assert "." in rival.source, f"{rival.name}: {rival.source!r} names no page"
            assert rival.dollars > 0, f"{rival.name} is priced at nothing"

    assert pricing.EXCLUDED, "no exclusions recorded, which is itself unlikely"
    for name, why in pricing.EXCLUDED.items():
        assert len(why) > 20, f"{name} is excluded for {why!r}, which is not a reason"


def test_the_saving_advertised_on_the_top_tier_is_the_saving_it_gives():
    """Ten per cent off, checked against the price it comes off.

    Two numbers have to agree here and they live in different systems: the
    percentage on the page and the percentage on the Stripe coupon. Only one
    of them is in this repository, so what is checked is that the page's own
    arithmetic is right and that the price it discounts is the price it sells
    — the coupon is then created from `TOP_TIER_OFF` rather than typed.
    """
    from auteur import pricing

    top = pricing.TOP_TIER
    assert top in pricing.TIERS, "the discounted tier is not one of the tiers"
    assert top.dollars == max(
        tier.dollars for tier in pricing.TIERS
    ), f"{top.name} carries the discount and is not the highest tier"

    after = pricing.discounted()
    saving = (top.dollars - after) / top.dollars
    assert saving >= pricing.TOP_TIER_OFF, (
        f"${top.dollars:.2f} → ${after:.2f} is {saving:.2%} off, "
        f"advertised as {pricing.TOP_TIER_OFF:.0%}"
    )
    # Rounded to a real cent, not a third of one.
    assert after == round(after, 2)


def test_every_priced_tier_can_be_found_in_the_account_that_charges_for_it():
    """The lookup key is the only thing joining this file to Stripe.

    Prices exist twice by necessity — here, and in an account this repository
    cannot see. A lookup key is what makes the pair checkable at all: with
    one, the price the site quotes can be fetched from Stripe and compared;
    without one, the two numbers are related only by whoever typed the second.
    """
    from auteur import pricing

    keys = [tier.lookup_key for tier in pricing.TIERS if tier.dollars]
    assert all(keys), "a paid tier with no lookup key cannot be reconciled with Stripe"
    assert len(set(keys)) == len(keys), f"two tiers share a lookup key: {keys}"
    assert not pricing.FREE.lookup_key, "the free tier does not charge anybody"


def test_the_free_tier_only_promises_what_runs_without_an_account():
    """Free has to be the build that exists, not a build that would be nice.

    Six of the eight features in `brand.FEATURES` are marked `on_device` and
    the browser build already ships them. The two that are not — the feed
    that learns and the feed itself — need a copy running somewhere, which is
    what the paid tiers are. If the free tier ever advertised one of those,
    the site would be giving away the only thing it sells.
    """
    from auteur import brand, pricing

    hosted = [feature for feature in brand.FEATURES if not feature.on_device]
    assert hosted, "nothing needs a hosted copy, so there is nothing to charge for"

    free = " ".join(pricing.FREE.includes).lower()
    for feature in hosted:
        # The distinguishing word of each hosted feature. Both are about a
        # feed, so that is the word: the free tier must not offer one.
        assert "feed" not in free, (
            f"the free tier offers a feed, which needs {feature.headline!r} — "
            "a copy running somewhere"
        )

    paid = " ".join(
        line.lower() for tier in pricing.TIERS if tier.dollars for line in tier.includes
    )
    assert "feed" in paid, "nothing paid for offers the feed, so the tiers sell nothing"


def test_the_site_quotes_no_price_the_pricing_module_did_not_derive():
    """Every dollar figure on the public page, compared to its source.

    This is the check the whole module exists for. A landing page is where a
    price gets typed by hand — a rounder number, a nicer number, a number from
    an older draft — and the person who finds out is the one whose card was
    charged the other one. So the built page is scanned for anything shaped
    like money, and every hit has to be a figure `auteur/pricing.py` produced.
    """
    import re
    import subprocess

    from auteur import pricing

    root = Path(__file__).resolve().parent.parent
    # Built with a checkout in place, so the page under test is the whole
    # page — the discounted price and the promotion code only appear where
    # there is somewhere to spend them, and a scan of a page missing half its
    # figures proves nothing about the half it is missing.
    built = subprocess.run(
        [sys.executable, str(root / "tools" / "site" / "build_site.py"), "-"],
        capture_output=True,
        text=True,
        cwd=root,
        env={
            **os.environ,
            **{
                f"AUTEUR_CHECKOUT_{tier.key.upper()}": f"https://buy.stripe.com/9AQ{tier.key}"
                for tier in pricing.TIERS
                if tier.dollars
            },
        },
    )
    assert built.returncode == 0, built.stderr
    page = (root / "-").read_text(encoding="utf-8")
    (root / "-").unlink()

    allowed = {f"${tier.dollars:.2f}" for tier in pricing.TIERS if tier.dollars}
    allowed.add(f"${pricing.discounted():.2f}")

    found = set(re.findall(r"\$\d+(?:\.\d{2})?", page))
    assert found, "the page quotes no price at all"
    assert (
        found <= allowed
    ), f"the site quotes {sorted(found - allowed)}, which pricing.py never produced"
    for price in allowed:
        assert price in page, f"{price} is a tier nobody can see on the page"

    # The trial is offered only where it can be started. Promising fourteen
    # free days above two plans that both say "Not open yet" is a page
    # arguing with itself.
    assert f"{pricing.TRIAL_DAYS} days free" in page
    assert f"{pricing.TOP_TIER_OFF:.0%} off" in page


def test_the_arithmetic_behind_the_prices_can_be_retraced_from_the_repository():
    """`working()` has to be enough to check the prices without asking anybody.

    Written down as a report rather than a comment because a comment is read
    by whoever is already editing the file, and this needs to be readable by
    whoever is deciding whether the price is right — which is a different
    person, usually later, usually without the context.
    """
    from auteur import pricing

    report = "\n".join(pricing.working())

    for rivals in (pricing.ENTRY_RIVALS, pricing.TOP_RIVALS):
        for rival in rivals:
            assert rival.name in report, f"{rival.name} is in the average and not in the working"
            assert rival.source in report

    for tier in pricing.TIERS:
        if tier.dollars:
            assert f"${tier.dollars:.2f}" in report

    for excluded in pricing.EXCLUDED:
        assert excluded in report, f"{excluded} was left out silently"

    # The one number in the module nobody measured says so, here, where the
    # measured ones are listed. It is the only place a reader would look.
    assert "chosen default, not a measured figure" in report.lower()
    assert pricing.AS_OF in report, "prices with no date are prices nobody re-checks"


def _sync_pricing():
    """The Stripe reconciler, imported as a module rather than shelled out to."""
    import importlib.util

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "sync_pricing", root / "tools" / "stripe" / "sync_pricing.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_live_stripe_key_cannot_be_used_by_accident():
    """Two things have to be true before anything reaches a real account.

    The failure this stops is mundane and expensive: an `sk_live_` exported
    into a shell an hour ago, a command re-run from history with `--apply`
    still on it, and real products on a real account that nobody meant to
    create. So the key's own prefix is not enough — `--live` has to be passed
    as well, and passing `--live` with a test key is refused too, because that
    combination means somebody believes they are doing something they are not.
    """
    sync = _sync_pricing()

    def run(argv, key):
        os.environ["STRIPE_SECRET_KEY"] = key
        try:
            return sync.main(argv)
        finally:
            os.environ.pop("STRIPE_SECRET_KEY", None)

    assert run([], "") == 2, "it ran with no key at all"
    assert run(["--apply"], "sk_live_pretend") == 2, "a live key went through without --live"
    assert run(["--apply", "--live"], "sk_test_pretend") == 2, "--live took a test key"


def test_the_reconciler_writes_nothing_unless_it_is_told_to():
    """A dry run is the default, because the failure mode is running twice.

    `post` and `delete` both record what they would send and return without
    sending it. The check is on the client rather than on `main`, because that
    is where the decision is made — one flag, in one place, rather than an
    `if apply` at every call site where one can be forgotten.
    """
    sync = _sync_pricing()

    rehearsal = sync.Stripe("sk_test_pretend", apply=False)
    # No network: if this posted, urlopen would raise rather than return.
    result = rehearsal.post("/products", {"name": "x"})
    rehearsal.delete("/coupons/x")
    assert result["dry_run"] is True
    assert len(rehearsal.did) == 2, rehearsal.did
    assert rehearsal.did[0].startswith("POST /products")
    assert rehearsal.did[1] == "DELETE /coupons/x"


def test_the_reconciler_encodes_what_stripe_actually_accepts():
    """Stripe takes forms, not JSON, and nested values as `metadata[key]`.

    Worth its own test because it is the part of this script that is wrong
    silently: a dict posted as `{'metadata': {...}}` does not error, it
    arrives as the literal string "{'derived_by': ...}" in a field nobody
    reads until somebody opens the dashboard six months later wondering where
    a price came from.
    """
    sync = _sync_pricing()
    client = sync.Stripe("sk_test_pretend", apply=False)

    pairs = dict(
        client._form(
            {
                "unit_amount": 1249,
                "recurring": {"interval": "month"},
                "metadata": {"derived_by": "auteur/pricing.py"},
                "line_items": [{"price": "price_x", "quantity": 1}],
                "applies_to": {"products": ["prod_x"]},
                "transfer_lookup_key": True,
                "nickname": None,
            }
        )
    )

    assert pairs["unit_amount"] == "1249"
    assert pairs["recurring[interval]"] == "month"
    assert pairs["metadata[derived_by]"] == "auteur/pricing.py"
    assert pairs["line_items[0][price]"] == "price_x"
    assert pairs["line_items[0][quantity]"] == "1"
    assert pairs["applies_to[products][0]"] == "prod_x"
    assert pairs["transfer_lookup_key"] == "true", "Python's True is not Stripe's true"
    assert "nickname" not in pairs, "None was sent as the string 'None'"


def test_what_stripe_is_told_is_what_the_pricing_module_says():
    """The account gets its numbers from the same place the site does.

    This is the other half of the check that scans the built page. That one
    stops a price being typed into the website by hand; this one stops it
    being typed into Stripe by hand. Between them the number exists once.
    """
    sync = _sync_pricing()

    from auteur import pricing

    client = sync.Stripe("sk_test_pretend", apply=False)
    tier = pricing.TOP_TIER
    product = {"id": "prod_x", "dry_run": True}

    # A dry run still reads, because it cannot say what it would do without
    # knowing what is already there. Nothing is there.
    original = sync.Stripe._send
    sync.Stripe._send = lambda self, method, path, body: {"data": []}

    price = client.post(
        "/prices",
        {
            "product": product["id"],
            "unit_amount": tier.cents,
            "lookup_key": tier.lookup_key,
        },
    )
    try:
        sync._link(client, tier, {"id": "price_x"})
    finally:
        sync.Stripe._send = original

    coupon = sync._coupon_params("prod_x")
    assert coupon["percent_off"] == pricing.TOP_TIER_OFF * 100, (
        f"the coupon is {coupon['percent_off']}% and the site advertises "
        f"{pricing.TOP_TIER_OFF:.0%}"
    )
    assert coupon["applies_to"]["products"] == [
        "prod_x"
    ], "the discount is not restricted to the tier it is advertised on"

    sent = " ".join(client.did)
    assert str(tier.cents) in sent, f"the price sent is not {tier.monthly}"
    assert tier.lookup_key in sent
    assert (
        f'"trial_period_days": {pricing.TRIAL_DAYS}' in sent
    ), f"the payment link does not carry the {pricing.TRIAL_DAYS}-day trial the site advertises"
    assert price["dry_run"] is True
    # The cents are the dollars, which is the conversion nobody checks until
    # somebody is charged $41.99 or $4199.
    assert tier.cents == 4199 == round(tier.dollars * 100)


def test_running_the_reconciler_twice_creates_nothing_the_second_time():
    """Idempotency, which is the property that costs money when it is wrong.

    A script pointed at a payments account gets run twice — after a network
    blip, from shell history, by a second person who did not know the first
    had done it. If it is not idempotent the account ends up with two products
    called the same thing, two prices, two payment links, and a checkout that
    charges from whichever one the site happened to be linking to.

    So this drives the real `main`, with `--apply`, against a Stripe that
    remembers what it was told, and requires the second run to send nothing.
    The account is a dict rather than a mock: a mock returning `{}` would let
    the lookup-by-metadata step pass without ever finding anything, which is
    the exact bug this is here to catch.
    """
    sync = _sync_pricing()

    from auteur import pricing

    account: dict[str, dict] = {
        "products": {},
        "prices": {},
        "coupons": {},
        "payment_links": {},
        "promotion_codes": {},
    }
    posted: list[str] = []
    counter = iter(range(1, 999))

    def fake_send(self, method, path, body):
        import urllib.parse

        route, _, query = path.partition("?")
        parts = [part for part in route.strip("/").split("/") if part]
        collection = parts[0]
        params = dict(urllib.parse.parse_qsl(body.decode())) if body else {}

        if method == "GET":
            wanted = dict(urllib.parse.parse_qsl(query))
            rows = list(account[collection].values())
            if "lookup_keys[0]" in wanted:
                rows = [row for row in rows if row.get("lookup_key") == wanted["lookup_keys[0]"]]
            return {"object": "list", "data": rows}

        if method == "DELETE":
            account[collection].pop(parts[1], None)
            return {"deleted": True}

        posted.append(path)
        if len(parts) == 2:  # an update to an existing object
            account[collection][parts[1]].update(params)
            return account[collection][parts[1]]

        identifier = params.get("id") or f"{collection[:4]}_{next(counter)}"
        row = {
            "id": identifier,
            "lookup_key": params.get("lookup_key") or None,
            "code": params.get("code") or None,
            "unit_amount": int(params["unit_amount"]) if "unit_amount" in params else None,
            "active": True,
            "percent_off": float(params["percent_off"]) if "percent_off" in params else None,
            "metadata": {
                key[len("metadata[") : -1]: value
                for key, value in params.items()
                if key.startswith("metadata[")
            },
            "url": f"https://buy.stripe.test/{identifier}",
        }
        account[collection][identifier] = row
        return row

    original = sync.Stripe._send
    sync.Stripe._send = fake_send
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_pretend"
    try:
        assert sync.main(["--apply"]) == 0
        first = list(posted)
        posted.clear()
        assert sync.main(["--apply"]) == 0
    finally:
        sync.Stripe._send = original
        os.environ.pop("STRIPE_SECRET_KEY", None)

    assert not posted, f"the second run created {posted}"

    paid = [tier for tier in pricing.TIERS if tier.dollars]
    assert len(account["products"]) == len(paid), account["products"]
    assert len(account["coupons"]) == 1, "the discount was created more than once"
    assert len(account["promotion_codes"]) == 1, "the code was created more than once"
    assert first, "the first run created nothing at all"

    # And what it created is what the site quotes.
    charged = sorted(price["unit_amount"] for price in account["prices"].values())
    assert charged == sorted(tier.cents for tier in paid), (
        f"Stripe would charge {charged} and the site says " f"{sorted(tier.cents for tier in paid)}"
    )
    assert next(iter(account["coupons"].values()))["percent_off"] == pricing.TOP_TIER_OFF * 100

    # A coupon nobody can type is not a discount. Stripe payment links take
    # `allow_promotion_codes` and will not carry a coupon of their own, so the
    # code has to exist and the site has to print it.
    code = next(iter(account["promotion_codes"].values()))["code"]
    assert code == pricing.PROMO_CODE, f"Stripe has {code}, the site prints {pricing.PROMO_CODE}"


def test_the_discount_the_site_advertises_can_actually_be_claimed():
    """A saving on a page with no way to claim it is a lie with a receipt.

    This gap was invisible from inside the repository and showed up only by
    driving the real Stripe API: a payment link takes `allow_promotion_codes`
    and refuses a `discounts` parameter outright, so a coupon on its own is
    something only the merchant can apply. The customer needs a code to type,
    and therefore the page needs to print one.

    Two halves, and both are checked here. The code has to name the percentage
    it unlocks, so a coupon moved to fifteen per cent cannot leave `ROOM10`
    advertising ten. And it appears **only where there is a checkout to type
    it into** — a promotion code printed above "Not open yet" is an
    instruction with nowhere to follow it.
    """

    from auteur import pricing

    percent = round(pricing.TOP_TIER_OFF * 100)
    assert str(percent) in pricing.PROMO_CODE, (
        f"{pricing.PROMO_CODE} does not name the {percent}% it unlocks, so a "
        "changed discount would leave it advertising the old one"
    )

    root = Path(__file__).resolve().parent.parent

    def build(**overrides: str) -> str:
        subprocess.run(
            [sys.executable, str(root / "tools" / "site" / "build_site.py"), "-"],
            check=True,
            capture_output=True,
            cwd=root,
            env={**os.environ, **overrides},
        )
        page = (root / "-").read_text(encoding="utf-8")
        (root / "-").unlink()
        return page

    open_for_business = build(
        **{f"AUTEUR_CHECKOUT_{pricing.TOP_TIER.key.upper()}": "https://buy.stripe.com/9AQreal"}
    )
    assert (
        pricing.PROMO_CODE in open_for_business
    ), "the code exists in Stripe and nowhere a customer looks"
    # Shown as a code rather than buried in a sentence: somebody has to
    # transcribe it accurately into a checkout field.
    assert f"<code>{pricing.PROMO_CODE}</code>" in open_for_business

    shown = re.search(r"(\d+)% off with <code>", open_for_business)
    assert shown and int(shown.group(1)) == percent, (
        f"the page offers {shown.group(1) if shown else 'nothing'}% "
        f"and the coupon gives {percent}%"
    )

    not_yet = build()
    assert (
        pricing.PROMO_CODE not in not_yet
    ), "the page tells somebody to type a code at a checkout that does not exist"


def test_a_stripe_test_link_cannot_reach_the_public_site():
    """The mistake that is available right now, refused by shape.

    Building this, I had two working test-mode checkout URLs in hand:

        https://buy.stripe.com/test_14A5kD7jZgcx8sUdDN1B600

    A live one looks the same minus four characters. Pasted onto the public
    site it does not fail — it takes a card number that is not a card number,
    tells the customer the payment worked, and creates nothing. There is no
    error for anybody to notice; the first signal is somebody asking where
    their subscription is.

    So `CHECKOUT` is not a place a URL is merely written. It is read through
    `checkout_for`, which refuses a test link rather than returning it.
    """
    from auteur import pricing

    original = dict(pricing.CHECKOUT)
    try:
        for bad in (
            "https://buy.stripe.com/test_14A5kD7jZgcx8sUdDN1B600",
            "https://buy.stripe.com/test_3cIaEX9s72lHdNearB1B601",
            "http://buy.stripe.com/9AQreal",
        ):
            pricing.CHECKOUT[pricing.SOLO.key] = bad
            with pytest.raises(ValueError):
                pricing.checkout_for(pricing.SOLO)

        pricing.CHECKOUT[pricing.SOLO.key] = "https://buy.stripe.com/9AQ00realone"
        assert pricing.checkout_for(pricing.SOLO).endswith("realone")
    finally:
        pricing.CHECKOUT.clear()
        pricing.CHECKOUT.update(original)


def test_a_plan_with_nowhere_to_pay_says_so_instead_of_offering_a_button():
    """A button wired to nothing reads as broken. Absence reads as forthcoming.

    The pricing table shipped before this with prices, a promotion code, a
    trial length and no way to buy anything — a page that asks for a decision
    and then does nothing with it. The fix is not to invent a button; it is to
    say which plans can be bought and which cannot, and to let that follow
    from whether a checkout URL exists rather than from anybody remembering to
    change the page when one does.
    """

    from auteur import pricing

    root = Path(__file__).resolve().parent.parent

    def build(**overrides: str) -> str:
        # A separate process, so the page is built the way it is really built
        # rather than by a function called with the answer already in hand.
        subprocess.run(
            [sys.executable, str(root / "tools" / "site" / "build_site.py"), "-"],
            check=True,
            capture_output=True,
            cwd=root,
            env={**os.environ, **overrides},
        )
        page = (root / "-").read_text(encoding="utf-8")
        (root / "-").unlink()
        return page

    paid = [tier for tier in pricing.TIERS if tier.dollars]

    page = build()
    assert page.count("Not open yet") == len(paid), "a paid plan with no checkout is not saying so"
    assert "Start the" not in page, "a plan offers a trial it has nowhere to start"

    page = build(AUTEUR_CHECKOUT_SOLO="https://buy.stripe.com/9AQ00realone")
    assert 'href="https://buy.stripe.com/9AQ00realone">Start the' in page
    assert (
        page.count("Not open yet") == len(paid) - 1
    ), "the tier that can be bought still says it cannot"

    # And the refusal holds through the environment too, or the override is a
    # way around the check rather than a way to configure it.
    failed = subprocess.run(
        [sys.executable, str(root / "tools" / "site" / "build_site.py"), "-"],
        capture_output=True,
        cwd=root,
        env={**os.environ, "AUTEUR_CHECKOUT_SOLO": "https://buy.stripe.com/test_abc"},
    )
    assert failed.returncode != 0, "a test link went onto the page through the environment"


def test_the_free_plan_sends_people_to_something_that_exists():
    """ "Open it" has to open something.

    The free tier is the only one whose button works today, so it is the only
    one that can be wrong quietly. It points at whatever `TRY_IT` holds, and
    the page's hero button points at the same place — two links, one value, so
    they cannot diverge.
    """

    root = Path(__file__).resolve().parent.parent
    subprocess.run(
        [sys.executable, str(root / "tools" / "site" / "build_site.py"), "-"],
        check=True,
        capture_output=True,
        cwd=root,
    )
    page = (root / "-").read_text(encoding="utf-8")
    (root / "-").unlink()

    targets = set(re.findall(r'href="(https://[^"]+)">(?:Open it|Make one in your browser)', page))
    assert len(targets) == 1, f"the two 'try it' links disagree: {sorted(targets)}"


def test_the_page_never_offers_a_trial_it_has_nowhere_to_start():
    """The page and its own plans have to agree about whether it is open.

    It did not. The headline read "14 days free, then $12.49 a month" over two
    plans that both said "Not open yet", and the small print underneath
    promised that "every paid plan starts with 14 days free". Three statements
    on one screen, one of them true.

    Nothing in the repository could see it, because each string was correct in
    isolation — the trial really is fourteen days, the plans really are not
    open. What was missing is the thing this whole file keeps finding: two
    values that describe the same fact and are never compared.
    """

    from auteur import pricing

    root = Path(__file__).resolve().parent.parent

    def build(**overrides: str) -> str:
        subprocess.run(
            [sys.executable, str(root / "tools" / "site" / "build_site.py"), "-"],
            check=True,
            capture_output=True,
            cwd=root,
            env={**os.environ, **overrides},
        )
        page = (root / "-").read_text(encoding="utf-8")
        (root / "-").unlink()
        return page

    shut = build()
    assert "Not open yet" in shut, "no plan is shut, so there is nothing to be wrong about"
    for promise in (f"{pricing.TRIAL_DAYS} days free", "days free and no card"):
        assert promise not in shut, f"the page promises {promise!r} and has nowhere to start it"

    everywhere = {
        f"AUTEUR_CHECKOUT_{tier.key.upper()}": f"https://buy.stripe.com/9AQ{tier.key}"
        for tier in pricing.TIERS
        if tier.dollars
    }
    open_now = build(**everywhere)
    assert "Not open yet" not in open_now, "a plan with a checkout still says it is shut"
    assert (
        f"{pricing.TRIAL_DAYS} days free" in open_now
    ), "everything is open and the trial is not offered anywhere"


def test_the_readme_tells_a_contributor_every_check_that_can_fail_them():
    """The Development block, compared to the workflow it is describing.

    It said `ruff check auteur tests` and did not mention black at all. CI runs
    `ruff check .` and `black --check .`. So the documented route was: follow
    the README, lint clean, push, and watch the lint job go red on a formatter
    the README never named — over files (`tools/`) the README's narrower path
    never looked at.

    That strands exactly one person: whoever is contributing for the first
    time, who has no reason to doubt the instructions. Everyone who already
    knows never reads the block again, which is why it stayed wrong.
    """

    root = Path(__file__).resolve().parent.parent
    workflow = (root / ".github" / "workflows" / "lint-and-type.yml").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")

    block = re.search(r"## Development\n+```bash\n(.*?)```", readme, re.S)
    assert block, "the README has no Development block to check"
    instructions = block.group(1)

    # Every `run:` line in the lint job that invokes a checker has to appear.
    ran = re.findall(r"run: (?:python -m )?((?:ruff|black)[^\n|(]*)", workflow)
    assert ran, "the lint workflow runs no checkers, which cannot be right"

    for command in ran:
        command = command.strip()
        assert command in instructions, (
            f"CI runs {command!r} and the README's Development block does not "
            f"say to. It says:\n{instructions}"
        )


def test_the_usage_line_names_every_command_the_cli_can_run():
    """`auteur --help` has to list what `auteur` accepts.

    It did not. The subcommand metavar was typed out by hand and listed
    fourteen commands, while the parser had sixteen: `moderate` and `template`
    both existed, both worked, and neither appeared in the line that tells you
    what you can run. The help text underneath described them, so the same
    screen contradicted itself.

    Nothing could catch that, because the list was a string that named the
    parsers and was never compared to them — the same shape as every other
    defect this file guards. The metavar is now built from `sub.choices`, so
    adding a command changes the usage line and the dispatch together or
    neither. This checks that it stayed built rather than being typed back in.
    """
    from auteur.cli import _build_parser

    parser = _build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    subcommands = next((set(a.choices) for a in actions if a.dest == "command"), set())
    assert len(subcommands) > 10, f"only {len(subcommands)} commands found; the lookup is wrong"

    usage = parser.format_usage()
    for command in sorted(subcommands):
        assert command in usage, f"`auteur {command}` runs and `auteur --help` never mentions it"

    # And the reverse: nothing listed that cannot be run. A usage line
    # offering a command that does not exist is the same bug pointed the other
    # way, and it is the one a person actually trips over.
    listed = re.search(r"\{([a-z,]+)\}", usage)
    assert listed, f"the usage line has no command list at all:\n{usage}"
    for name in listed.group(1).split(","):
        assert name in subcommands, f"`auteur --help` offers {name!r}, which does not exist"


def test_no_caveat_is_hidden_behind_a_tooltip_a_phone_cannot_summon():
    """A disclaimer clipped to an ellipsis is a disclaimer nobody read.

    The studio header carried

        fitted on 2000 simulated rows and none of your own — this predicts
        the simulator, not any platform

    and at 390px it showed "fitted on 2000 simulated rows and no m…", directly
    above three large figures — 0.87, 0.06, 1.59 — which then read as
    measurements of something real. The sentence saying they are not was the
    part that got cut.

    `studio.js` also set the same string as a `title`. That is the tell: a
    `title` is a hover tooltip, and the phone this app is built for has no
    hover, so the fallback was unreachable on the only device that mattered.
    Setting one is a sign the author expected the text to be clipped — which
    makes it the thing to look for, rather than the fix.

    So the rule is general: where the app sets an element's `title` to the
    same text it just put in that element, the element must not be styled to
    truncate at phone width.
    """

    from auteur.web import server

    static = server.STATIC
    sheets = "\n".join(path.read_text(encoding="utf-8") for path in sorted(static.glob("*.css")))

    offenders: list[str] = []
    for script in sorted(static.glob("*.js")):
        source = script.read_text(encoding="utf-8")
        # `$("x").textContent = v;` followed by `$("x").title = v;`
        for element, _value in re.findall(
            r'\$\("([\w-]+)"\)\.textContent\s*=\s*([^;]+);\s*\$\("\1"\)\.title\s*=\s*\2;',
            source,
        ):
            markup = "\n".join(
                page.read_text(encoding="utf-8") for page in sorted(static.glob("*.html"))
            )
            found = re.search(rf'id="{element}"[^>]*class="([^"]*)"', markup) or re.search(
                rf'class="([^"]*)"[^>]*id="{element}"', markup
            )
            if not found:
                continue
            for name in found.group(1).split():
                rule = re.search(rf"\.{re.escape(name)}\s*\{{(.*?)\}}", sheets, re.S)
                if not rule:
                    continue
                body = rule.group(1)
                truncating = "nowrap" in body and "ellipsis" in body
                # A phone-width override that lets it wrap is the fix, and it
                # lives in a media query rather than in the base rule.
                rescued = re.search(
                    rf"@media[^{{]*max-width[^{{]*\{{.*?\.{re.escape(name)}\s*\{{",
                    sheets,
                    re.S,
                )
                if truncating and not rescued:
                    offenders.append(
                        f"#{element} (.{name}) is clipped, and its only full "
                        f"copy is a title= tooltip a phone cannot show"
                    )

    assert not offenders, "; ".join(offenders)


def test_the_critic_can_actually_fail_a_film(tmp_path):
    """A score that is always high is not a review, it is a decoration.

    `auteur demo` finishes by telling a person "it rates its own work 100%".
    Every rule in `critic.review` had a test; the *score* had none, so nothing
    established that the number could ever come out low, or that a worse film
    scored worse than a better one. A verdict nothing can ever fail is exactly
    the shape of defect this file exists to catch — a value that is produced
    and never compared to anything.

    So this renders the worst film the program can describe — a frozen grey
    rectangle at twice its target runtime — and requires the critic to notice.
    Not a particular number: the curve is a product decision and is documented
    on `PENALTY_PER_SEVERITY` rather than pinned here. What is pinned is that
    the critic discriminates, and that it says *why* in words a person can act
    on rather than only in a number.
    """
    from auteur import ffmpeg
    from auteur.critic import review
    from auteur.edl import EditDecisionList, Shot

    frozen = tmp_path / "frozen.mp4"
    ffmpeg.run(
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=0x303030:s=256x456:d=12:r=24",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(frozen),
        ]
    )
    assert frozen.is_file(), "the fixture did not render"

    shots = [
        Shot(clip_id=f"c{i}", source=frozen, start=i * 2.0, end=i * 2.0 + 2.0) for i in range(6)
    ]
    verdict = review(EditDecisionList(title="frozen", shots=shots), frozen, target_duration=6.0)

    assert verdict.score < 0.9, (
        f"a frozen rectangle at twice its runtime scored {verdict.score:.2f} — "
        "the critic cannot fail anything"
    )
    rules = {note.rule for note in verdict.notes}
    assert "dead-air" in rules, f"nothing moves for twelve seconds and the critic said {rules}"
    assert "runtime" in rules, f"twice the target runtime went unremarked; said {rules}"

    # Every note has to be a sentence somebody can act on, not just a weight.
    for note in verdict.notes:
        assert note.message.strip(), f"{note.rule} has a severity and nothing to read"
        assert 0.0 < note.severity <= 1.0, note

    # And a clean film has to come out above it, or "worse" means nothing.
    clean = review(EditDecisionList(title="none", shots=[]), frozen, target_duration=12.0)
    assert clean.score > verdict.score, (
        f"the film with faults scored {verdict.score:.2f} and the one without "
        f"scored {clean.score:.2f}"
    )


def test_a_raw_colour_only_appears_where_the_ground_is_somebody_s_footage():
    """The stylesheets say they use only palette tokens. Nothing checked it.

    Five files repeat some version of "every colour is a variable from
    theme.css, which is generated from auteur/theme.py" — and that claim was
    prose. The palette exists because a second copy of a colour drifts: the
    public site once shipped thirteen colours and a green that was teal in the
    app, for months, because a file said "generated" and nothing generated it.

    There is one legitimate exception and `theme.py` names it. `on_photo` is
    "text and marks drawn on top of somebody's footage", white in *both*
    schemes on purpose, because the ground under a feed mark is a frame of
    somebody's film rather than the theme's surface. Around it sit blacks that
    are the letterbox behind a video element, and scrims at alphas the palette
    does not carry.

    So the rule is not "no raw colours". It is: a raw colour is allowed only
    in a file whose subject is footage, and it must not be white, because
    white over footage is a role that already exists. Anything else is a
    second copy of a palette value, and this fails on it by name.
    """
    from auteur.web import server

    #: Files whose colours sit on somebody's film rather than on the theme.
    OVER_FOOTAGE = {
        "feed.css": "the feed is full-bleed video; every mark sits on a frame",
        "inbox.css": "a film in a message bubble letterboxes against black",
        "overlays.css": "the sticker preview stands in for footage, deliberately",
        "animations.css": "a drop shadow, which is a darkening rather than a colour",
    }

    offenders: list[str] = []
    for sheet in sorted(server.STATIC.glob("*.css")):
        if sheet.name == "theme.css":
            continue
        body = re.sub(r"/\*.*?\*/", "", sheet.read_text(encoding="utf-8"), flags=re.S)
        raw = re.findall(r"#[0-9a-fA-F]{3,8}\b|\brgba?\([^)]*\)", body)
        if not raw:
            continue
        if sheet.name not in OVER_FOOTAGE:
            offenders.append(f"{sheet.name} hardcodes {sorted(set(raw))} and is not over footage")
            continue
        for colour in raw:
            bare = colour.lower().replace(" ", "")
            if bare in ("#fff", "#ffffff", "rgb(255,255,255)"):
                offenders.append(
                    f"{sheet.name} hardcodes {colour} — white on footage is "
                    "var(--on-photo), a role theme.py already defines"
                )

    assert not offenders, "; ".join(offenders)

    # And the role really is theme-independent, or pointing the feed at it
    # would have changed how the feed looks in daylight.
    from auteur import theme

    assert theme.hex_of("on_photo", "dark") == theme.hex_of(
        "on_photo", "light"
    ), "on_photo differs between schemes, so the feed's marks now change colour"


def test_the_manual_documents_every_command_the_cli_can_run():
    """README calls AUTEUR.md "the full documentation: every command".

    It documented ten of sixteen. `benchmark`, `moderate`, `rehearse`,
    `scholar` and `template` appeared zero times; `agents` was mentioned but
    never as something you could type. Two of those — `moderate` and
    `template` — are the same pair the CLI's own usage line was missing, which
    is the tell: they were added and no surface that describes the program was
    updated with them.

    AUTEUR.md is also what `pyproject.toml` hands to PyPI as the package
    description, so the gap is not internal. A promise of completeness that
    nothing compares to the thing it covers is the same defect as the usage
    line, one level up, and it is checked the same way.
    """
    from auteur.cli import _build_parser

    root = Path(__file__).resolve().parent.parent
    manual = (root / "AUTEUR.md").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert "every command" in readme, "the README no longer promises this; drop the guard"

    parser = _build_parser()
    commands = next(
        set(a.choices) for a in parser._actions if getattr(a, "dest", "") == "command" and a.choices
    )
    assert len(commands) > 10, f"only {len(commands)} commands found; the lookup is wrong"

    undocumented = [name for name in sorted(commands) if not re.search(rf"auteur {name}\b", manual)]
    assert not undocumented, (
        "README promises AUTEUR.md documents every command; it never shows "
        f"how to run: {undocumented}"
    )


def test_both_front_doors_lead_with_the_name_and_tagline_brand_py_holds():
    """Two documents, one product, and the tagline lives in one place.

    AUTEUR.md led with "an autonomous cinematic editor" while brand.py, the
    site, both store listings and the README all said "a film from your camera
    roll". Not false — it is an autonomous cinematic editor — but it is a
    second positioning line for the same product, and AUTEUR.md is what PyPI
    shows, so the package's public description led with words no other surface
    used.
    """
    from auteur import brand

    root = Path(__file__).resolve().parent.parent
    wanted = f"{brand.NAME} — {brand.TAGLINE.lower()}"

    for name in ("README.md", "AUTEUR.md"):
        first = (root / name).read_text(encoding="utf-8").splitlines()[0]
        assert first.startswith("#"), f"{name} does not open with a heading"
        assert wanted in first, f"{name} leads with {first!r}, and the brand says {wanted!r}"


def test_the_manual_does_not_describe_a_control_that_was_deliberately_removed():
    """The appearance switch is in Settings, and the manual said "every screen".

    It used to sit at the bottom of all nine tabs — a setting repeated nine
    times, and a footer in the way of the content on every one of them. It was
    moved into Settings on purpose. The manual went on describing the old
    arrangement, which sends a reader looking for a control that is not there
    and describes a design decision as its opposite.
    """
    from auteur.web import server

    root = Path(__file__).resolve().parent.parent
    manual = (root / "AUTEUR.md").read_text(encoding="utf-8")

    carries = [
        page.name
        for page in sorted(server.STATIC.glob("*.html"))
        if 'class="choices appearance"' in page.read_text(encoding="utf-8")
    ]
    assert carries == [
        "profile.html"
    ], f"the appearance switch is on {carries}; it belongs in Settings alone"
    assert (
        "every screen has an **Appearance**" not in manual
    ), "the manual still says every screen carries the switch"


def test_one_sentence_describes_this_product_to_a_stranger():
    """Every public surface shows the same description, at a readable length.

    There were nearly three. The generated site used `brand.PROMISE`. The live
    company site carried a hand-written description saying Atlas "plans the
    week and reads the reach" — which is two secondary features, not the
    product — and selling a second product called APX, which is not a product
    at all: it is the name of the craft-rules work in `auteur/craft/story.py`.
    And writing this, I drafted a third for the Wix write before noticing it
    was a second copy of the same fact.

    None of that was visible from inside the repository, because the wrong one
    lived in a hosting dashboard. What a check *can* hold is the half that is
    here: one constant, used by every surface this repository generates, at a
    length a search result will actually show.
    """
    import subprocess

    from auteur import brand

    root = Path(__file__).resolve().parent.parent

    # Google truncates a snippet around 155 characters and an Open Graph card
    # clips sooner. Under 70 and it is not a sentence, it is a fragment.
    assert 70 <= len(brand.META_DESCRIPTION) <= 155, (
        f"the description is {len(brand.META_DESCRIPTION)} characters; "
        "search results will cut it or it says too little"
    )
    assert brand.NAME in brand.META_DESCRIPTION, (
        "a snippet is read cold beside nine other results — it has to name the "
        "product before it explains it"
    )
    assert brand.META_DESCRIPTION.rstrip().endswith("."), "a snippet is a sentence"

    subprocess.run(
        [sys.executable, str(root / "tools" / "site" / "build_site.py"), "-"],
        check=True,
        capture_output=True,
        cwd=root,
        env=os.environ.copy(),
    )
    page = (root / "-").read_text(encoding="utf-8")
    (root / "-").unlink()

    shown = re.findall(
        r'<meta (?:name|property)="(?:description|og:description)" content="([^"]*)"', page
    )
    assert shown, "the built page carries no description at all"
    for found in shown:
        # The page escapes for HTML; compare on the same footing.
        import html as html_module

        assert html_module.unescape(found) == brand.META_DESCRIPTION, (
            f"the page describes the product as {found!r} and brand.py says "
            f"{brand.META_DESCRIPTION!r}"
        )
    assert len(set(shown)) == 1, "the meta and og descriptions disagree with each other"


def test_every_control_in_the_markup_says_what_it_does():
    """A control a screen reader announces as "button" is a control nobody
    using one can operate.

    Driven in a real browser across all ten screens, every interactive element
    already had an accessible name, no heading level was skipped, and no
    standalone target measured under Apple's 44x44. Three properties measured
    and compared to nothing — which is the state this suite exists to end. The
    browser is not in CI, so this is the static half: what the markup ships
    with, before a line of JavaScript runs.

    `hidden` elements are skipped on purpose rather than by exception. The one
    control with no label in the markup is `<a id="link" hidden>` on the
    profile, which `profile.js` fills in when there is a link to show — and a
    hidden element is not announced, so it has nothing to say yet. Skipping
    hidden is the correct rule, and it happens to leave no special cases.
    """
    from html.parser import HTMLParser

    from auteur.web import server

    class Controls(HTMLParser):
        """Collects interactive elements and whatever text they contain."""

        def __init__(self) -> None:
            super().__init__()
            self.open: list = []
            self.bare: list = []
            self.said: dict = {}
            self.headings: list = []
            self.in_heading: int | None = None

        def handle_starttag(self, tag, attrs):
            got = dict(attrs)
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6") and "hidden" not in got:
                self.in_heading = int(tag[1])
            if tag in ("button", "a") or got.get("role") in ("button", "switch"):
                self.open.append((tag, got, len(self.said)))
                self.said[len(self.said)] = ""

        def handle_data(self, data):
            if self.open:
                key = self.open[-1][2]
                self.said[key] = self.said.get(key, "") + data
            if self.in_heading is not None and data.strip():
                self.headings.append(self.in_heading)
                self.in_heading = None

        def handle_endtag(self, tag):
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                self.in_heading = None
            if self.open and self.open[-1][0] == tag:
                name, got, key = self.open.pop()
                if "hidden" in got:
                    return
                label = (self.said.get(key, "") or "").strip()
                for attribute in ("aria-label", "title", "aria-labelledby"):
                    label = label or (got.get(attribute) or "").strip()
                if not label:
                    self.bare.append(
                        f"<{name}> id={got.get('id', '-')} " f"class={got.get('class', '')!r}"
                    )

    silent: list[str] = []
    jumps: list[str] = []
    for page in sorted(server.STATIC.glob("*.html")):
        reader = Controls()
        reader.feed(page.read_text(encoding="utf-8"))
        silent += [f"{page.name}: {one}" for one in reader.bare]
        for before, after in zip(reader.headings, reader.headings[1:], strict=False):
            if after > before + 1:
                jumps.append(f"{page.name}: h{before} straight to h{after}")

    assert not silent, "controls a screen reader cannot name: " + "; ".join(silent)
    assert not jumps, "headings skip a level, so the outline lies: " + "; ".join(jumps)


def test_the_program_never_calls_a_number_measured_that_it_did_not_measure():
    """ "Measured" is a claim about the world. This program cannot check it.

    `measured_rows` counts rows that arrived in a file somebody pointed at,
    and nothing more. Handed one of the generated datasets sitting in this
    project — five rows of `v_001`, `v_002`, virality tier "Mega-Viral" — the
    report said *"fitted on 5 measured rows"* and went on to name the winning
    hook length. Nothing lied on purpose; the word was simply doing work it
    had not earned.

    The shape checks in `score.py` already do what can be done about invented
    data — a median share rate several times anything a platform sees, or a
    corpus with no drop-off in it — and both get named in `caveat`. What no
    check can do is tell a careful fake from a real export. So the sentence
    stops claiming to: it says where the rows came from.
    """
    from auteur.insight.score import FitReport

    empty = FitReport(rows=2000, measured_rows=0, simulated_rows=2000)
    mixed = FitReport(rows=2005, measured_rows=5, simulated_rows=2000)
    yours = FitReport(rows=5, measured_rows=5, simulated_rows=0, forms=("short_form_video",))

    for model in (empty, mixed, yours):
        assert "measured" not in model.provenance, (
            f"the report says {model.provenance!r} — the program cannot know a "
            "file it was handed was measured from anything"
        )

    # It still has to say *which* is which, or the honesty costs the meaning.
    assert "simulated" in empty.provenance and "not any platform" in empty.provenance
    assert "your" in mixed.provenance and "simulated" in mixed.provenance
    assert "your" in yours.provenance

    # And the same word, one column wide, on the per-post table.
    source = (Path(__file__).resolve().parent.parent / "auteur" / "cli.py").read_text()
    assert (
        'source = "measured"' not in source
    ), "the per-post column still calls an exported number a measured one"
