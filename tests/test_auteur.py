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
from auteur.edl import EditDecisionList, Look, Motion, Ramp, Shot, Transition
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
    assert all(a != b for a, b in zip(ids, ids[1:])), "no clip cuts back to itself"
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
