"""Conform: turn the EDL into pixels and air.

Two passes, deliberately.

**Pass one** renders every shot to its own normalised segment — trimmed, speed
ramped, reframed, moved and corrected, all at the delivery frame size and rate.
Doing this per shot keeps each filter graph small enough to reason about and
means a single unusable clip fails one segment instead of the whole film.

**Pass two** assembles: transitions between segments, film texture, letterbox,
type, then the mix — music, ducking, designed effects and any source sound,
placed on its own timeline so audio can cross the picture cut.

Every extra delivery format is a full re-render rather than a crop of the
master, because the reframe has to see the original footage to know what to
keep.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable

from . import ffmpeg
from .config import IMAGE_SUFFIXES, DeliveryFormat, Quality, Settings, Workspace
from .craft import (
    color,
    graphics,
    motion as motion_craft,
    sound as sound_craft,
    titles,
    transitions,
)
from .edl import EditDecisionList, Shot
from .ffmpeg import chain, graph

log = logging.getLogger("auteur.render")


@dataclass
class RenderResult:
    outputs: dict[str, Path] = field(default_factory=dict)
    duration: float = 0.0
    segments: dict[str, list[Path]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def primary(self) -> Path | None:
        return next(iter(self.outputs.values()), None)


# ---------------------------------------------------------------------------
# Pass one: shots
# ---------------------------------------------------------------------------


def _source_fps(shot: Shot, quality: Quality) -> float:
    """The rate at which frames actually arrive from this shot's input.

    For a still that is the delivery rate, not some nominal 30: the image
    demuxer is opened with `-loop 1 -framerate {quality.fps}`, so that is
    literally how fast its frames come. Guessing wrong misaligns every ramp
    slice from the real frame grid, and the shot renders short — a 10-second
    cut of stills at draft's 24fps delivered 7.9.
    """
    if shot.source.suffix.lower() in IMAGE_SUFFIXES:
        return float(quality.fps)
    return ffmpeg.source_fps(shot.source)


def _segment_video_graph(shot: Shot, fmt: DeliveryFormat, quality: Quality) -> str:
    """The filter graph for one shot, from source frames to a delivery frame."""
    moving = shot.motion.kind != "none" and shot.motion.intensity > 0.01
    overscan = motion_craft.OVERSCAN if moving else 1.0

    ramp = motion_craft.ramp_video_graph(
        shot.ramp,
        source_duration=shot.source_duration,
        in_label="src",
        out_label="ramped",
        optical_flow=quality.optical_flow,
        fps=quality.fps,
        source_fps=_source_fps(shot, quality),
    )

    post = chain(
        f"fps={quality.fps}",
        motion_craft.reframe_chain(
            fmt.width,
            fmt.height,
            mode=shot.reframe,
            anchor=motion_craft.frame_of(shot.motion.anchor),
            overscan=overscan,
        ),
        (
            motion_craft.motion_chain(
                shot.motion,
                target_w=fmt.width,
                target_h=fmt.height,
                fps=quality.fps,
                duration=shot.duration,
            )
            if moving
            else ""
        ),
        # Guarantee the delivery size even when a filter rounded against us.
        f"scale={fmt.width}:{fmt.height}:flags=bicubic",
        # Correction only, per shot: exposure, white balance and saturation
        # nudged so the clips agree with each other before anything expressive
        # touches them. The *look* goes on the finished film, once, in
        # `_assemble` — see the note there.
        color.correction_chain(shot.look),
        "setsar=1",
        "format=yuv420p",
    )

    return graph(ramp, f"[ramped]{post}[vout]")


def carries_audio(shot: Shot, *, want_audio: bool) -> bool:
    """Whether this shot's segment is rendered with an audio track.

    One predicate, used by both the segment renderer and the mixer. When the two
    disagreed, the mixer asked for an audio stream the segment never got and the
    whole assembly failed — a still frame with source audio was enough to do it.
    """
    return want_audio and shot.use_source_audio and shot.audio_gain > 0.001 and not shot.is_still


def _segment_audio_graph(shot: Shot, quality: Quality) -> str:
    ramp = motion_craft.ramp_audio_graph(
        shot.ramp,
        source_duration=shot.source_duration,
        in_label="asrc",
        out_label="aramped",
        source_fps=_source_fps(shot, quality),
    )
    post = chain(
        f"volume={shot.audio_gain:.3f}",
        f"aformat=sample_fmts=fltp:sample_rates={sound_craft.SAMPLE_RATE}:channel_layouts=stereo",
    )
    return graph(ramp, f"[aramped]{post}[aout]")


def render_shot(
    shot: Shot,
    index: int,
    workspace: Workspace,
    fmt: DeliveryFormat,
    quality: Quality,
    *,
    want_audio: bool,
) -> Path:
    """Render one shot to a self-contained segment file."""
    destination = workspace.segments / f"{fmt.name}-{index:03d}.mp4"

    args: list[str] = []
    if shot.is_still:
        # Hold the frame for as long as the shot needs. `-loop` belongs to the
        # image demuxer, so it only works on an actual image file — a video that
        # happens to contain one frame has to be looped at the stream level.
        if shot.source.suffix.lower() in IMAGE_SUFFIXES:
            args += ["-loop", "1", "-framerate", str(quality.fps)]
        else:
            args += ["-stream_loop", "-1"]
        args += ["-t", f"{shot.source_duration:.4f}", "-i", str(shot.source)]
    else:
        args += [
            "-ss",
            f"{shot.start:.4f}",
            "-t",
            f"{shot.source_duration:.4f}",
            "-i",
            str(shot.source),
        ]

    has_source_audio = carries_audio(shot, want_audio=want_audio)
    filtergraph = _segment_video_graph(shot, fmt, quality)
    maps = ["-map", "[vout]"]

    if has_source_audio:
        filtergraph = graph(filtergraph, _segment_audio_graph(shot, quality))
        maps += ["-map", "[aout]"]

    args += [
        "-filter_complex",
        graph("[0:v]null[src]", *(["[0:a]anull[asrc]"] if has_source_audio else []), filtergraph),
        *maps,
        # The ramp decides the length; trim to it so the assembly maths holds.
        "-t",
        f"{shot.duration:.4f}",
        "-c:v",
        "libx264",
        "-crf",
        str(quality.crf),
        "-preset",
        quality.preset,
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(quality.fps),
        "-video_track_timescale",
        "90000",
    ]
    if has_source_audio:
        args += ["-c:a", "aac", "-b:a", quality.audio_bitrate, "-ar", str(sound_craft.SAMPLE_RATE)]
    else:
        args += ["-an"]
    args += [str(destination)]

    ffmpeg.run(args)

    # ffmpeg exits 0 for a graph that produced no frames, leaving a valid,
    # streamless file. The assembly then asks it for [N:v] and fails with a
    # filtergraph error naming no shot and no reason. Catch it at the source.
    if not _has_video(destination):
        raise ffmpeg.FFmpegError(
            args,
            0,
            # The caller names the shot; this says only why.
            f"no frames in {shot.source.name} between {shot.start:.2f}s and {shot.end:.2f}s",
        )
    return destination


def _has_video(path: Path) -> bool:
    """Whether a rendered segment actually contains picture."""
    if not path.is_file() or path.stat().st_size < 1024:
        return False
    try:
        info = ffmpeg.probe(path)
    except ffmpeg.FFmpegError:
        return False
    return any(stream.get("codec_type") == "video" for stream in info.get("streams", []))


# ---------------------------------------------------------------------------
# Pass two: assembly
# ---------------------------------------------------------------------------


def _assemble_video(
    edl: EditDecisionList,
    segment_count: int,
    measured: list[float] | None = None,
) -> tuple[str, str]:
    """Chain segments together with their transitions.

    Returns (filtergraph, final_label). Consecutive hard cuts are concatenated
    (free); anything else costs an xfade and shortens the timeline by its
    overlap, which the running offset has to account for.

    **`measured` is the length each segment actually came out at**, and passing
    it is not an optimisation. An xfade's `offset` says where in the *first*
    input the overlap begins, and if that offset lands past the end of the
    first input, ffmpeg ends the output there — silently, with no error and a
    zero exit code. The rest of the film simply is not in the file.

    The offsets used to be summed from the EDL's *planned* durations, which
    are what the director asked for and not what came back. A shot planned at
    0.250s renders at 0.233s, because a segment is a whole number of frames
    and 0.250s at 30fps is seven and a half of them. That is a 7% shortfall
    per shot, it accumulates, and the first xfade whose offset outruns it
    truncates everything after it.

    It went unnoticed for as long as every shot was about the same length: the
    drift stayed small and the first xfade happened to land inside it. Giving
    the films a held shot and real landings made the shots longer and more
    varied, the drift crossed the line, and a 20-second film came back with
    4.8 seconds of picture and 19.7 seconds of music over black. The bug was
    already there; the change only made it reachable.
    """
    # concat and xfade disagree about time bases (1/1000000 against the
    # segments' 1/90000), and xfade refuses to join links that disagree. Pinning
    # every link to AVTB up front makes the chain composable in any order.
    parts = [
        f"[{index}:v]settb=AVTB,setpts=PTS-STARTPTS[vin{index}]" for index in range(segment_count)
    ]

    if segment_count == 1:
        return ";".join(parts), "vin0"

    def actual(index: int) -> float:
        """What segment `index` is really this long, falling back to the plan."""
        if measured is not None and index < len(measured) and measured[index] > 0:
            return measured[index]
        return edl.shots[index].duration

    current = "vin0"
    length = actual(0)

    for index in range(1, segment_count):
        shot = edl.shots[index]
        label = f"vj{index}"
        if shot.transition_in.is_cut:
            parts.append(f"[{current}][vin{index}]concat=n=2:v=1:a=0,settb=AVTB[{label}]")
            length += actual(index)
        else:
            overlap = shot.transition_in.duration
            # Never past the end of what is actually there. An offset one frame
            # beyond the incoming chain silently ends the film.
            offset = max(min(length - overlap, length), 0.0)
            spec = transitions.xfade_spec(shot.transition_in.kind, overlap, offset)
            parts.append(f"[{current}][vin{index}]{spec},settb=AVTB[{label}]")
            length += actual(index) - overlap
        current = label

    return ";".join(parts), current


@dataclass(frozen=True)
class _Composite:
    """One thing to lay over the picture, ready for the overlay filter.

    Text plates and drawn graphics are different upstream — one is a full-frame
    RGBA plate, the other a cropped PNG sequence with a position — and identical
    from here down, so they share one chain rather than two that drift apart.
    """

    label: str
    x: str
    y: str
    start: float
    end: float


def _text_composites(overlays: list[titles.TextOverlay]) -> list[_Composite]:
    out: list[_Composite] = []
    for order, overlay in enumerate(overlays):
        settle = max(overlay.fade_in, 0.2)
        if overlay.rise > 0.5:
            # Ease the plate up into its resting position as it fades in.
            y = f"{overlay.rise:.1f}*(1-min(1\\,max(0\\,(t-{overlay.start:.3f})/{settle:.3f})))"
        else:
            y = "0"
        out.append(
            _Composite(f"txt{order}", "0", y, round(overlay.start, 3), round(overlay.end, 3))
        )
    return out


def _graphic_composites(drawn: list[graphics.Graphic]) -> list[_Composite]:
    return [
        _Composite(f"gfx{order}", str(item.box[0]), str(item.box[1]), item.start, item.end)
        for order, item in enumerate(drawn)
    ]


def _overlay_chain(composites: list[_Composite], video_label: str) -> tuple[str, str]:
    """Composite each plate over the picture, animating it into place."""
    if not composites:
        return "", video_label

    parts: list[str] = []
    current = video_label
    for order, item in enumerate(composites):
        label = f"vov{order}"
        parts.append(
            f"[{current}][{item.label}]overlay=x={item.x}:y='{item.y}'"
            f":enable='between(t,{item.start:.3f},{item.end:.3f})'"
            f":eof_action=pass[{label}]"
        )
        current = label
    return ";".join(parts), current


def _assemble_audio(
    edl: EditDecisionList,
    *,
    segment_count: int,
    music_input: int | None,
    sfx_inputs: dict[str, int],
    duration: float,
    source_audio_shots: list[tuple[int, float]],
) -> tuple[str, str]:
    """Build the mix.

    Source audio is placed on its own timeline with `adelay` rather than being
    carried along with the picture, which is what allows a shot's sound to start
    before or after its first frame — the J and L cuts that stop an edit
    sounding like a series of blocks.
    """
    parts: list[str] = []
    stems: list[str] = []

    voice_stem: str | None = None
    if source_audio_shots:
        voice_parts: list[str] = []
        for order, (segment_index, at) in enumerate(source_audio_shots):
            delay = max(0, int(round(at * 1000)))
            voice_parts.append(
                f"[{segment_index}:a]adelay={delay}|{delay},"
                f"aformat=sample_fmts=fltp:sample_rates={sound_craft.SAMPLE_RATE}:channel_layouts=stereo"
                f"[voice{order}]"
            )
        parts.extend(voice_parts)
        joined = "".join(f"[voice{order}]" for order in range(len(source_audio_shots)))
        if len(source_audio_shots) > 1:
            parts.append(
                f"{joined}amix=inputs={len(source_audio_shots)}:normalize=0:dropout_transition=0[voicemix]"
            )
            voice_stem = "voicemix"
        else:
            parts.append(f"{joined}anull[voicemix]")
            voice_stem = "voicemix"

    if music_input is not None:
        parts.append(f"[{music_input}:a]{sound_craft.music_chain(edl.music, duration)}[musicraw]")
        if edl.music.duck and voice_stem:
            # Split the voice: one copy drives the compressor, one goes to the mix.
            parts.append(f"[{voice_stem}]asplit=2[voicekey][voiceout]")
            parts.append(
                sound_craft.duck_graph("musicraw", "voicekey", "music", edl.music.duck_amount)
            )
            stems += ["music", "voiceout"]
            voice_stem = None
        else:
            parts.append("[musicraw]anull[music]")
            stems.append("music")

    if voice_stem:
        stems.append(voice_stem)

    for order, cue in enumerate(edl.sfx):
        index = sfx_inputs.get(sound_craft.effect_key(cue))
        if index is None:
            continue
        delay = max(0, int(round(cue.at * 1000)))
        parts.append(
            f"[{index}:a]volume={cue.gain:.3f},adelay={delay}|{delay},"
            f"aformat=sample_fmts=fltp:sample_rates={sound_craft.SAMPLE_RATE}:channel_layouts=stereo"
            f"[sfx{order}]"
        )
        stems.append(f"sfx{order}")

    if not stems:
        # A film with no sound still needs a silent track, or players misbehave.
        parts.append(f"{sound_craft.silence_chain(duration)}[silence]")
        stems.append("silence")

    joined = "".join(f"[{stem}]" for stem in stems)
    if len(stems) > 1:
        parts.append(
            f"{joined}amix=inputs={len(stems)}:normalize=0:dropout_transition=0[premaster]"
        )
    else:
        parts.append(f"{joined}anull[premaster]")

    parts.append(f"[premaster]{sound_craft.master_chain()}[aout]")
    return ";".join(parts), "aout"


def _assemble(
    edl: EditDecisionList,
    segments: list[Path],
    workspace: Workspace,
    fmt: DeliveryFormat,
    quality: Quality,
    destination: Path,
    *,
    want_audio: bool,
) -> Path:
    """The single ffmpeg call that produces a finished film."""
    duration = edl.duration
    inputs: list[str] = []
    for segment in segments:
        inputs += ["-i", str(segment)]

    next_index = len(segments)

    music_input: int | None = None
    if edl.music.source and Path(edl.music.source).exists():
        music_duration_needed = duration + edl.music.offset
        loop_args: list[str] = []
        try:
            info = ffmpeg.probe(edl.music.source)
            available = float(info.get("format", {}).get("duration") or 0.0)
            if available and available < music_duration_needed:
                loop_args = ["-stream_loop", "-1"]
        except ffmpeg.FFmpegError:
            loop_args = []
        inputs += [*loop_args, "-i", str(edl.music.source)]
        music_input = next_index
        next_index += 1

    sfx_files = sound_craft.render_effects(edl.sfx, workspace.assets)
    sfx_inputs: dict[str, int] = {}
    for key, path in sfx_files.items():
        inputs += ["-i", str(path)]
        sfx_inputs[key] = next_index
        next_index += 1

    # Namespaced by format: two formats share the assets directory, and plates
    # are rendered at the frame size, so a shared name lets one overwrite the other.
    text_overlays = titles.render_all(
        edl.texts,
        width=fmt.width,
        height=fmt.height,
        directory=workspace.assets,
        prefix=fmt.name,
    )
    text_chains: list[str] = []
    for order, overlay in enumerate(text_overlays):
        inputs += [
            "-loop",
            "1",
            "-t",
            f"{overlay.start + overlay.duration + 0.5:.3f}",
            "-i",
            str(overlay.path),
        ]
        links = ["format=rgba"]
        if overlay.fade_in > 0.01:
            links.append(f"fade=t=in:st=0:d={overlay.fade_in:.3f}:alpha=1")
        if overlay.fade_out > 0.01:
            links.append(
                f"fade=t=out:st={max(overlay.duration - overlay.fade_out, 0.0):.3f}"
                f":d={overlay.fade_out:.3f}:alpha=1"
            )
        links.append(f"setpts=PTS+{overlay.start:.4f}/TB")
        text_chains.append(f"[{next_index}:v]{chain(*links)}[txt{order}]")
        next_index += 1

    drawn = graphics.render_all(
        edl.graphics,
        width=fmt.width,
        height=fmt.height,
        directory=workspace.assets,
        prefix=fmt.name,
    )
    graphic_chains: list[str] = []
    for order, item in enumerate(drawn):
        if item.is_sequence:
            # One input for the whole animation: the image demuxer reads the
            # printf pattern as a stream, so a 90-frame graphic costs one input
            # rather than ninety.
            inputs += ["-framerate", f"{item.fps:g}", "-i", str(item.pattern)]
        else:
            inputs += ["-loop", "1", "-t", f"{item.duration + 0.5:.3f}", "-i", str(item.pattern)]
        links = ["format=rgba"]
        if item.is_sequence:
            # The sequence was drawn at its own rate; conform it to the film's
            # or ffmpeg holds each plate for a whole source frame period.
            links.append(f"fps={edl.fps}")
        if item.fade_in > 0.01:
            links.append(f"fade=t=in:st=0:d={item.fade_in:.3f}:alpha=1")
        if item.fade_out > 0.01:
            links.append(
                f"fade=t=out:st={max(item.duration - item.fade_out, 0.0):.3f}"
                f":d={item.fade_out:.3f}:alpha=1"
            )
        links.append(f"setpts=PTS+{item.start:.4f}/TB")
        graphic_chains.append(f"[{next_index}:v]{chain(*links)}[gfx{order}]")
        next_index += 1

    # Measure what the segments actually came out at rather than trusting the
    # plan — see `_assemble_video` for what a stale offset does to a film.
    measured: list[float] = []
    for segment in segments:
        try:
            info = ffmpeg.probe(str(segment))
            streams = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
            measured.append(float(streams[0].get("duration") or 0.0) if streams else 0.0)
        except (ffmpeg.FFmpegError, ValueError, IndexError):
            # A segment that cannot be probed falls back to its planned length,
            # which is what the code did for all of them until now.
            measured.append(0.0)

    video_graph, video_label = _assemble_video(edl, len(segments), measured)

    # The grade, in two passes that do different jobs: each shot was corrected
    # toward the others on its way in, and the expressive look lands here, on
    # the assembled film, once.
    #
    # It used to land twice. `_match_looks` copies the film's preset and
    # strength onto every shot, so `look_chain(shot.look)` in the segment pass
    # and `look_chain(edl.look)` here built byte-identical filter chains and
    # both ran. Measured on one still through a neon grade at 0.7: luma fell
    # from 0.150 to 0.098 and the fraction of the frame crushed to true black
    # rose from 0.331 to 0.526 — past the 0.45 line this project calls a ruined
    # picture, and close to the 0.550 of the wrecked render a rehearsal loop
    # produced while scoring itself perfect. Every film went out like that.
    finish = chain(
        color.look_chain(edl.look) if not edl.look.is_identity else "",
        color.texture_chain(edl.texture, width=fmt.width),
        color.letterbox_chain(edl.letterbox, fmt.width, fmt.height),
    )
    if finish:
        video_graph = graph(video_graph, f"[{video_label}]{finish}[vfinish]")
        video_label = "vfinish"

    # Graphics go under the type: a ring is there to point at the picture, and a
    # title that ends up behind one is a title nobody reads.
    overlay_graph, video_label = _overlay_chain(
        _graphic_composites(drawn) + _text_composites(text_overlays), video_label
    )

    source_audio_shots: list[tuple[int, float]] = []
    for index, (start, _, shot) in enumerate(edl.timeline()):
        if index < len(segments) and carries_audio(shot, want_audio=want_audio):
            source_audio_shots.append((index, max(0.0, start + shot.audio_offset)))

    audio_graph, audio_label = _assemble_audio(
        edl,
        segment_count=len(segments),
        music_input=music_input,
        sfx_inputs=sfx_inputs,
        duration=duration,
        source_audio_shots=source_audio_shots,
    )

    full_graph = graph(
        video_graph,
        *text_chains,
        *graphic_chains,
        overlay_graph,
        f"[{video_label}]format=yuv420p,setsar=1[vout]",
        audio_graph,
    )

    args = [
        *inputs,
        "-filter_complex",
        full_graph,
        "-map",
        "[vout]",
        "-map",
        f"[{audio_label}]",
        "-t",
        f"{duration:.4f}",
        "-c:v",
        "libx264",
        "-crf",
        str(quality.crf),
        "-preset",
        quality.preset,
        "-profile:v",
        "high",
        "-level",
        "4.1",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(quality.fps),
        "-g",
        str(quality.fps * 2),
        "-keyint_min",
        str(quality.fps),
        "-c:a",
        "aac",
        "-b:a",
        quality.audio_bitrate,
        "-ar",
        "48000",
        "-ac",
        "2",
        # Metadata at the front: the file starts playing before it finishes downloading.
        "-movflags",
        "+faststart",
        str(destination),
    ]
    (workspace.logs / f"filtergraph-{fmt.name}.txt").write_text(full_graph, encoding="utf-8")
    ffmpeg.run(args)
    return destination


# ---------------------------------------------------------------------------


def render(
    edl: EditDecisionList,
    workspace: Workspace,
    settings: Settings,
    *,
    formats: tuple[DeliveryFormat, ...] | None = None,
    name: str | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> RenderResult:
    """Render the EDL to every requested delivery format.

    `on_progress(done, total, label)` is called as each shot lands, because a
    render takes minutes and silence is indistinguishable from a hang.
    """
    result = RenderResult(duration=edl.duration)
    targets = formats if formats is not None else settings.all_formats
    quality = settings.quality
    stem = _slug(name or edl.title)

    want_audio = any(shot.use_source_audio for shot in edl.shots)

    # One unit per shot, plus one for each format's final assembly.
    total = (len(edl.shots) + 1) * len(targets)
    done = 0

    def report(label: str) -> None:
        if on_progress is not None:
            on_progress(done, total, label)

    lock = threading.Lock()

    for fmt in targets:
        log.info(
            "rendering %s (%dx%d, %.2fs, %d shots)",
            fmt.name,
            fmt.width,
            fmt.height,
            edl.duration,
            len(edl.shots),
        )
        suffix = f" · {fmt.name}" if len(targets) > 1 else ""
        shots = list(enumerate(edl.shots))
        segments: list[Path] = [Path()] * len(shots)

        # Shots are independent by construction — each is its own ffmpeg call
        # writing its own file — so they render concurrently. libx264 threads a
        # single 1080x1920 segment poorly, and the segments are short, so the
        # machine sits mostly idle doing these one at a time.
        workers = segment_workers(settings, len(shots))
        report(f"shot 1 of {len(shots)}{suffix}")

        # `fmt` is bound as a default rather than captured: the pool is joined
        # before the loop advances, so the closure is correct today, but a
        # closure over a loop variable is one refactor away from rendering
        # every format into the last format's frame.
        def one(item: tuple[int, Shot], fmt: DeliveryFormat = fmt) -> tuple[int, Path | None]:
            """Render one shot, or report that it could not be rendered.

            A failure here returns None rather than raising. The module docstring
            promises that one unusable clip costs one segment and not the whole
            film, and it did not: a single shot whose source window happened to
            contain no frames — a 0.44s cut of 1fps footage, say — took the
            entire render down with it.
            """
            index, shot = item
            try:
                path = render_shot(shot, index, workspace, fmt, quality, want_audio=want_audio)
            except ffmpeg.FFmpegError as exc:
                reason = (exc.stderr or str(exc)).strip().splitlines()
                message = (
                    f"dropped shot {index + 1}: "
                    f"{reason[-1][:160] if reason else 'it would not render'}"
                )
                log.info("%s", message)
                with lock:
                    result.warnings.append(message)
                return index, None
            return index, path

        rendered: list[Path | None] = [None] * len(shots)
        if workers > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                for index, path in pool.map(one, shots):
                    rendered[index] = path
                    with lock:
                        done += 1
                        finished = done
                    report(f"shot {min(finished, len(shots))} of {len(shots)}{suffix}")
        else:
            for item in shots:
                index, path = one(item)
                rendered[index] = path
                done += 1
                report(f"shot {index + 1} of {len(shots)}{suffix}")

        # Assemble from whatever survived. The EDL is reduced to match, because
        # the assembly reads shots and segments positionally.
        kept = [index for index, path in enumerate(rendered) if path is not None]
        if not kept:
            raise RuntimeError("no shot could be rendered; the footage may be unreadable")
        segments = [rendered[index] for index in kept]  # type: ignore[misc]
        cut = (
            edl
            if len(kept) == len(shots)
            else edl.without_shots([index for index in range(len(shots)) if index not in set(kept)])
        )

        report(f"putting it together{suffix}")
        destination = workspace.output / f"{stem}-{fmt.name}.mp4"
        _assemble(cut, segments, workspace, fmt, quality, destination, want_audio=want_audio)
        done += 1
        report(f"putting it together{suffix}")

        result.outputs[fmt.name] = destination
        result.segments[fmt.name] = segments
        log.info("wrote %s", destination)

    return result


def segment_workers(settings: Settings, shot_count: int) -> int:
    """How many shots to render at once.

    Each worker is a whole ffmpeg process, so this is bounded by cores rather
    than by threads. One per core, measured rather than assumed: on a 4-core box
    a 21-shot reel took 74s sequentially, 58s with two workers, 54s with four,
    and 77s with six. Past the core count every segment slows down and the batch
    finishes later than it would have with fewer.

    Optical flow is excluded — `minterpolate` is memory-hungry enough that
    running several at once can push a laptop into swap, which is far worse than
    rendering them in turn.
    """
    if shot_count <= 1 or settings.quality.optical_flow:
        return 1
    cores = os.cpu_count() or 2
    return max(1, min(shot_count, cores, 8))


def _slug(text: str) -> str:
    keep = [char.lower() if char.isalnum() else "-" for char in text.strip()]
    slug = "".join(keep)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:60] or "film"


def probe_output(path: Path) -> dict:
    """Read back what we actually wrote — the only honest way to report success."""
    info = ffmpeg.probe(path)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    fmt = info.get("format", {})
    return {
        "path": str(path),
        "duration": float(fmt.get("duration") or 0.0),
        "size_bytes": int(fmt.get("size") or 0),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "video_codec": video.get("codec_name", ""),
        "audio_codec": audio.get("codec_name", ""),
        "frames": int(video.get("nb_frames") or 0),
    }
