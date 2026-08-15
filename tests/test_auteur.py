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

import subprocess
import sys
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
from auteur.edl import MIN_SHOT, EditDecisionList, Look, Motion, Ramp, Shot, Transition
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
            [binary, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source,
             "-t", str(duration), "-c:v", "libx264", "-crf", "30", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", str(directory / name)],
            check=True,
        )

    # 120 BPM: a kick every 0.5s, which the tempo estimator must recover.
    rate, bpm, seconds = 22050, 120.0, 12.0
    track = np.zeros(int(rate * seconds), dtype=np.float32)
    step = int(rate * 60.0 / bpm)
    for start in range(0, len(track) - step, step):
        t = np.arange(min(int(rate * 0.25), len(track) - start)) / rate
        pitch = 50.0 + 90.0 * np.exp(-t * 30.0)
        track[start : start + len(t)] += np.sin(2 * np.pi * np.cumsum(pitch) / rate) * np.exp(-t * 12.0)
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
    edl = EditDecisionList(shots=[
        _shot(end=2.0),
        _shot(clip="C02", end=2.0, transition_in=Transition("dissolve", 0.5)),
    ])
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
    edl = EditDecisionList(shots=[
        _shot(transition_in=Transition("dissolve", 0.5)),
        _shot(clip="C02"),
    ])
    edl.repair({"C01": _FakeDossier(4.0), "C02": _FakeDossier(4.0)})
    assert edl.shots[0].transition_in.is_cut


def test_repair_shortens_a_transition_longer_than_its_shots():
    edl = EditDecisionList(shots=[
        _shot(end=1.0),
        _shot(clip="C02", end=1.0, transition_in=Transition("dissolve", 5.0)),
    ])
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
    edl = EditDecisionList(shots=[
        _shot(clip="C01"), _shot(clip="C01"), _shot(clip="C02"), _shot(clip="C03"),
    ])
    grammar.enforce_variety(edl)
    ids = [shot.clip_id for shot in edl.shots]
    assert ids[0] != ids[1], "consecutive shots from one clip read as a mistake"


def test_transition_density_is_capped():
    edl = EditDecisionList(shots=[
        _shot(clip=f"C{i:02d}", transition_in=Transition("dissolve", 0.3)) for i in range(10)
    ])
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
    flat = motion.ramp_video_graph(Ramp.constant(2.0), source_duration=2.0,
                                   in_label="v", out_label="o")
    assert "split" not in flat
    assert "setpts=PTS/2.0" in flat

    curved = motion.ramp_video_graph(Ramp.hit(), source_duration=2.0,
                                     in_label="v", out_label="o")
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
        chain = motion.motion_chain(Motion(kind, 0.5), target_w=1080, target_h=1920,
                                    fps=30, duration=2.0)
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
    assert parse_brief("frenetic montage").base_shot_length < parse_brief("slow montage").base_shot_length


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
    for shot in edl.shots:
        assert shot.duration >= 0.2


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
        [binary, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=320x240:rate=25", "-frames:v", "1",
         "-c:v", "libx264", "-crf", "30", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", str(clip)],
        check=True,
    )

    shot = Shot(clip_id="C01", source=clip, start=0.0, end=1.5, is_still=True)
    segment = render_shot(shot, 0, Workspace(tmp_path / "w"), FORMATS["square"],
                          QUALITIES["draft"], want_audio=False)
    assert segment.exists() and segment.stat().st_size > 1000


def test_swapping_shots_leaves_the_transitions_where_they_were():
    """A transition was chosen for a position on the timeline, not for a shot."""
    edl = EditDecisionList(shots=[
        _shot(clip="C01"),
        _shot(clip="C01", transition_in=Transition("dissolve", 0.4)),
        _shot(clip="C02", transition_in=Transition("whip-left", 0.2)),
        _shot(clip="C03", transition_in=Transition("cut", 0.0)),
    ])
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

    assert "metronome" in ui.plain_finding("metronomic", "x") or "same length" in ui.plain_finding("metronomic", "x")
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
        [rushes], 'punchy montage, 6 seconds, "TEST"',
        settings=Settings(quality=QUALITIES["draft"], primary_format=FORMATS["square"],
                          target_duration=6.0, use_llm=False, revision_rounds=0),
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
        ramp, source_duration=source_duration, in_label="src", out_label="out", source_fps=source_fps
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
        clip_id="C01", source=rushes / "a_wide.mp4", start=0.0, end=0.02,
        ramp=Ramp(points=((0.0, 1.0),)),
    )
    # Force the pathology the guard exists for: a window with no frames in it.
    shot.start, shot.end = 3.999, 4.0

    try:
        path = render.render_shot(shot, 0, space, FORMATS["square"], QUALITIES["draft"],
                                  want_audio=False)
    except ffmpeg.FFmpegError as exc:
        assert "C01" in str(exc) and "no frames" in str(exc)
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

    request = Request(base + "/api/login",
                      data=_json.dumps({"username": "tester",
                                        "password": "a-long-enough-one"}).encode(),
                      headers={"Content-Type": "application/json"})
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
        base + "/api/jobs", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Cookie": cookie},
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
        "run", lambda *a, **k: type("R", (), {"returncode": 0, "stderr": ""})(),
    )

    args = argparse.Namespace(command="demo", quiet=True, verbose=0,
                              out=str(tmp_path / "demo"), prompt="a film")
    assert cli._run_demo(args, cli.NullReporter()) == 0
    assert captured["missing"] == []
    assert captured["quiet"] is True


def test_a_failure_reaches_the_phone_as_one_readable_line():
    """A render error carries the whole filter graph. That must not be what the
    page shows — it once put several thousand characters of `[12:v]settb=AVTB`
    on screen where an explanation belonged."""
    from auteur.web.server import _plain_cause

    graph_dump = "Stream specifier ':v' in filtergraph description " + "[0:v]settb=AVTB;" * 400
    assert _plain_cause(ffmpeg.FFmpegError([], 1, graph_dump)) == "One of the clips could not be used."

    assert len(_plain_cause(RuntimeError("x" * 900))) <= 160
    assert _plain_cause(RuntimeError("the folder was empty")) == "the folder was empty"
    assert _plain_cause(RuntimeError("")) == ""


def test_a_failed_job_says_something_a_person_can_read(tmp_path):
    from auteur.web.server import Studio

    studio = Studio(tmp_path / "web")
    job = studio.create("prompt", "reel", 10.0)
    studio._fail(job, "Something went wrong making the film.",
                 RuntimeError("Stream specifier ':v' in filtergraph description " + "[0:v]x;" * 500))

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
    stray = [line.strip() for line in style.splitlines()
             if re.search(r"#[0-9a-fA-F]{3,8}\b", line) or "rgba(" in line or "rgb(" in line]
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
    assert render.segment_workers(settings, 20) == 4     # one per core
    assert render.segment_workers(settings, 3) == 3      # never more than the shots
    assert render.segment_workers(settings, 1) == 1      # nothing to overlap

    monkeypatch.setattr(render.os, "cpu_count", lambda: 64)
    assert render.segment_workers(settings, 100) == 8    # capped

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
            [rushes], "punchy montage, 5 seconds",
            settings=Settings(quality=QUALITIES["draft"], primary_format=FORMATS["square"],
                              target_duration=5.0, use_llm=False, revision_rounds=0, seed=11),
            workspace=tmp_path / folder, duration=5.0,
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
    except HTTPError as exc:            # urllib raises on 304 in some versions
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
        clip_id="C01", source=rushes / "a_wide.mp4", start=0.4, end=1.5,
        ramp=Ramp(points=((0.0, 1.14), (0.45, 1.14), (1.0, 2.16))),
    )
    path = render.render_shot(shot, 0, space, FORMATS["square"], QUALITIES["draft"],
                              want_audio=False)
    measured = float(ffmpeg.probe(path)["format"]["duration"])
    assert measured == pytest.approx(shot.duration, abs=0.06), (
        f"wanted {shot.duration:.3f}s of screen time, got {measured:.3f}s"
    )


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
            round((start + 0.5 / fps) * fps), abs=1e-6)


def test_a_still_survives_being_written_out_and_read_back():
    """`is_still` decides whether a shot is looped or seeked into. A saved EDL
    that omits it renders every photo as almost nothing."""
    edl = EditDecisionList(title="t")
    edl.shots.append(Shot(clip_id="C01", source=Path("/tmp/photo.jpg"), start=0.0, end=2.0,
                          is_still=True))
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
            [binary, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", source, "-frames:v", "1", str(photos / f"still{index}.png")],
            check=True,
        )

    for name in ("draft", "standard"):
        production = direct(
            [photos], "slow and cinematic, 8 seconds",
            settings=Settings(quality=QUALITIES[name], primary_format=FORMATS["square"],
                              target_duration=8.0, use_llm=False, revision_rounds=0, seed=3),
            workspace=tmp_path / f"work-{name}", duration=8.0,
        )
        measured = float(ff.probe(production.primary)["format"]["duration"])
        assert measured == pytest.approx(production.edl.duration, abs=0.35), (
            f"{name}: planned {production.edl.duration:.2f}s, delivered {measured:.2f}s"
        )


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

@pytest.fixture
def accounts(tmp_path):
    from auteur.web.auth import Accounts

    store = Accounts(tmp_path / "accounts.json")
    store.add("streetlightseason", "streetlightseason@gmail.com", "Tacit25#")
    return store


def test_a_password_is_never_stored(accounts, tmp_path):
    raw = (tmp_path / "accounts.json").read_text()
    assert "Tacit25#" not in raw
    account = accounts.get("streetlightseason")
    assert account.password_hash != "Tacit25#"
    assert len(account.salt) == 32 and len(account.password_hash) == 64


def test_the_right_password_is_accepted_and_others_are_not(accounts):
    account = accounts.get("streetlightseason")
    assert account.check("Tacit25#")
    assert not account.check("tacit25#")
    assert not account.check("Tacit25")
    assert not account.check("")


def test_you_can_sign_in_with_either_the_username_or_the_email(accounts):
    for who in ("streetlightseason", "STREETLIGHTSEASON",
                "streetlightseason@gmail.com", "  Streetlightseason@Gmail.com  "):
        token, _ = accounts.sign_in(who, "Tacit25#")
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
    token, message = accounts.sign_in("streetlightseason", "Tacit25#")
    assert token is None
    assert "Too many" in message

    accounts.get("streetlightseason").locked_until = 0.0
    token, _ = accounts.sign_in("streetlightseason", "Tacit25#")
    assert token


def test_a_session_can_be_ended(accounts):
    token, _ = accounts.sign_in("streetlightseason", "Tacit25#")
    assert accounts.session_user(token) == "streetlightseason"
    accounts.sign_out(token)
    assert accounts.session_user(token) is None
    assert accounts.session_user("some-other-token") is None
    assert accounts.session_user(None) is None


def test_session_tokens_are_stored_hashed(accounts, tmp_path):
    """The account file must not hold anything replayable."""
    token, _ = accounts.sign_in("streetlightseason", "Tacit25#")
    assert token not in (tmp_path / "accounts.json").read_text()


def test_a_reset_link_works_once(accounts):
    started = accounts.begin_reset("streetlightseason@gmail.com")
    assert started is not None
    _, token = started

    assert accounts.finish_reset(token, "a-much-longer-one")
    assert accounts.get("streetlightseason").check("a-much-longer-one")
    assert not accounts.get("streetlightseason").check("Tacit25#")
    assert not accounts.finish_reset(token, "third-attempt-here")


def test_an_expired_reset_link_is_refused(accounts):
    _, token = accounts.begin_reset("streetlightseason")
    accounts.get("streetlightseason").reset_expires = time.time() - 1
    assert accounts.account_for_reset(token) is None
    assert not accounts.finish_reset(token, "a-much-longer-one")


def test_resetting_for_an_unknown_account_gives_nothing_away(accounts):
    assert accounts.begin_reset("someone@example.com") is None


def test_changing_the_password_signs_every_device_out(accounts):
    first, _ = accounts.sign_in("streetlightseason", "Tacit25#")
    second, _ = accounts.sign_in("streetlightseason", "Tacit25#")
    accounts.set_password(accounts.get("streetlightseason"), "a-much-longer-one")
    assert accounts.session_user(first) is None
    assert accounts.session_user(second) is None


def test_accounts_survive_a_restart(accounts, tmp_path):
    from auteur.web.auth import Accounts

    token, _ = accounts.sign_in("streetlightseason", "Tacit25#")
    reopened = Accounts(tmp_path / "accounts.json")
    assert reopened.get("streetlightseason").check("Tacit25#")
    assert reopened.session_user(token) == "streetlightseason", "a restart must not sign the phone out"


def test_weak_passwords_are_explained_not_just_refused():
    from auteur.web.auth import password_problem

    assert password_problem("short") == "Use at least 8 characters."
    assert "space" in password_problem(" has-spaces-around ")
    assert password_problem("password") != ""
    assert password_problem("a-perfectly-fine-one") == ""


def test_the_seed_account_carries_a_hash_and_not_a_password():
    """The repository must never contain the password itself."""
    from auteur.web import seed
    from auteur.web.auth import Account

    source = Path(seed.__file__).read_text()
    assert "Tacit25#" not in source

    account = Account(username=seed.SEED_USERNAME, email=seed.SEED_EMAIL,
                      salt=seed.SEED_SALT, password_hash=seed.SEED_HASH)
    assert account.check("Tacit25#"), "the seeded account must actually sign in"
    assert not account.check("wrong")


def test_the_first_run_creates_exactly_one_account(tmp_path, monkeypatch):
    from auteur.web import seed
    from auteur.web.auth import Accounts

    monkeypatch.delenv("AUTEUR_PASSWORD", raising=False)
    store = Accounts(tmp_path / "accounts.json")
    assert seed.bootstrap(store) == "streetlightseason"
    assert len(store.accounts) == 1
    # Running again must not add a second.
    assert seed.bootstrap(store) is None
    assert len(store.accounts) == 1


def test_the_environment_beats_the_committed_seed(tmp_path, monkeypatch):
    from auteur.web import seed
    from auteur.web.auth import Accounts

    monkeypatch.setenv("AUTEUR_USERNAME", "someone")
    monkeypatch.setenv("AUTEUR_EMAIL", "someone@example.com")
    monkeypatch.setenv("AUTEUR_PASSWORD", "a-much-longer-one")
    store = Accounts(tmp_path / "accounts.json")

    assert seed.bootstrap(store) == "someone"
    assert store.get("someone").check("a-much-longer-one")
    assert store.get("streetlightseason") is None


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
    web.Handler.accounts.add("streetlightseason", "streetlightseason@gmail.com", "Tacit25#")

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
    for path in ("/login", "/static/login.js", "/static/style.css",
                 "/static/theme.css", "/manifest.webmanifest", "/icon-192.png"):
        with urlopen(base + path) as response:
            assert response.status == 200, path


def test_signing_in_sets_a_cookie_a_script_cannot_read(guarded_server):
    base, _ = guarded_server

    status, payload, _ = _post(base, "/api/login",
                               {"username": "streetlightseason", "password": "wrong"})
    assert status == 401 and "error" in payload

    status, payload, headers = _post(base, "/api/login",
                                     {"username": "streetlightseason", "password": "Tacit25#"})
    assert status == 200 and payload["user"] == "streetlightseason"

    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie      # no script can read the session
    assert "SameSite=Strict" in cookie   # no other site can spend it
    assert "Path=/" in cookie


def test_a_signed_in_request_gets_through(guarded_server):
    import json as _json
    from urllib.request import Request, urlopen

    base, _ = guarded_server
    _, _, headers = _post(base, "/api/login",
                          {"username": "streetlightseason", "password": "Tacit25#"})
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
    _, _, headers = _post(base, "/api/login",
                          {"username": "streetlightseason", "password": "Tacit25#"})
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

    real_status, real, _ = _post(base, "/api/forgot",
                                 {"username": "streetlightseason@gmail.com"})
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
    assert status == 400 and "8 characters" in payload["error"]

    status, payload, _ = _post(base, "/api/reset",
                               {"token": "not-a-real-token", "password": "a-much-longer-one"})
    assert status == 400

    status, payload, _ = _post(base, "/api/reset",
                               {"token": token, "password": "a-much-longer-one"})
    assert status == 200

    status, _, _ = _post(base, "/api/login",
                         {"username": "streetlightseason", "password": "Tacit25#"})
    assert status == 401, "the old password must stop working"
    status, _, _ = _post(base, "/api/login",
                         {"username": "streetlightseason", "password": "a-much-longer-one"})
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
    assert ':root:not([data-theme])' in css
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
    monkeypatch.setattr("auteur.web.auth.send_reset",
                        lambda email, link: sent.append(link) or "console")

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
    monkeypatch.setattr("auteur.web.auth.send_reset",
                        lambda email, link: sent.append(link) or "email")
    monkeypatch.setenv("AUTEUR_PUBLIC_URL", "https://films.example.com/")

    request = Request(base + "/api/forgot",
                      data=_json.dumps({"username": "streetlightseason"}).encode(),
                      headers={"Content-Type": "application/json"})
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
    web.Handler.accounts = None          # exactly the misconfiguration

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

    request = Request(base + "/api/login",
                      data=_json.dumps({"username": "someone-else",
                                        "password": "a-long-enough-one"}).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request) as response:
        intruder = response.headers["Set-Cookie"].split(";")[0]

    for path in (f"/api/jobs/{job.id}", f"/api/jobs/{job.id}/video",
                 f"/api/jobs/{job.id}/notes"):
        with pytest.raises(HTTPError) as caught:
            urlopen(Request(base + path, headers={"Cookie": intruder}))
        assert caught.value.code == 404, path
