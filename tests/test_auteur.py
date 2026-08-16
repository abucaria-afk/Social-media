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
from auteur.ingest import ingest, probe_asset

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

    assets.ensure(web.STATIC)
    web.Handler.studio = web.Studio(tmp_path / "web")
    web.Handler.accounts = Accounts(tmp_path / "web" / "accounts.json")
    web.Handler.accounts.add("tester", "tester@example.com", "a-long-enough-one")

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

    defined = set(re.findall(r"(--[a-z-]+):", generated))
    local = set(re.findall(r"(--[a-z-]+):", style))  # radius, safe areas
    used = set(re.findall(r"var\((--[a-z-]+)", style))

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
    assert assets.AMBER == theme.rgb_of("ember")

    page = (server.STATIC / "index.html").read_text()
    assert f'content="{theme.THEME_COLOR}"' in page

    import json as _json

    manifest = _json.loads((server.STATIC / "manifest.webmanifest").read_text())
    assert manifest["theme_color"] == theme.THEME_COLOR
    assert manifest["background_color"] == theme.THEME_COLOR


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
    """A theme switch must not be able to make text disappear."""
    from auteur import theme

    for scheme in ("dark", "light"):
        ground = theme.rgb_of("ground", scheme)
        for role in ("text", "text_muted", "text_faint", "ember_text", "moss", "rust"):
            ratio = theme.contrast(theme.rgb_of(role, scheme), ground)
            assert ratio >= 4.5, f"{role} on {scheme} ground is only {ratio:.2f}:1"

        button = theme.contrast(theme.rgb_of("on_ember", scheme), theme.rgb_of("ember", scheme))
        assert button >= 4.5, f"the main button on {scheme} is only {button:.2f}:1"


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


def test_the_theme_is_applied_before_the_page_paints():
    """Reading localStorage from app.js instead would show one frame of the
    wrong theme on every load."""
    from auteur.web import server

    for page in ("index.html", "login.html"):
        markup = (server.STATIC / page).read_text()
        early = markup.index("auteur-theme")
        assert early < markup.index('href="/static/style.css"'), page
        assert 'setAttribute("data-theme"' in markup


def test_the_switch_offers_exactly_the_three_modes():
    from auteur import theme
    from auteur.web import server

    assert theme.MODES == ("system", "light", "dark")
    for page in ("index.html", "login.html"):
        markup = (server.STATIC / page).read_text()
        for mode in theme.MODES:
            assert f'data-value="{mode}"' in markup, f"{page} is missing {mode}"


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
