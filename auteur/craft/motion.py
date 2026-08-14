"""Movement: reframing, camera moves invented in post, and speed ramps.

Three problems, all solved by moving pixels around in time:

1. **Reframing.** Footage arrives horizontal; the film delivers vertical. A
   centre crop throws away the subject. So the crop follows the subject track
   measured during analysis.
2. **Moves.** A locked-off shot held for two seconds is dead air. A slow push
   in on the same frame is a shot.
3. **Ramps.** Screen time and source time are not the same thing, and the gap
   between them — the ramp — is most of what makes an edit feel designed.
"""

from __future__ import annotations

import math

from ..edl import Motion, Ramp
from ..ffmpeg import chain

#: Extra resolution kept around the frame so moves have somewhere to travel.
OVERSCAN = 1.28
#: Speed-ramp resolution: source slices per second of screen time.
RAMP_SLICES_PER_SECOND = 14
RAMP_MIN_SLICES = 6
RAMP_MAX_SLICES = 48


def cover_scale(source_w: int, source_h: int, target_w: int, target_h: int, *, factor: float = 1.0) -> tuple[int, int]:
    """Smallest even frame size that fully covers the target, times `factor`."""
    if source_w <= 0 or source_h <= 0:
        return int(target_w * factor) // 2 * 2, int(target_h * factor) // 2 * 2
    scale = max((target_w * factor) / source_w, (target_h * factor) / source_h)
    width = max(2, int(round(source_w * scale)) // 2 * 2)
    height = max(2, int(round(source_h * scale)) // 2 * 2)
    return width, height


def reframe_chain(
    target_w: int,
    target_h: int,
    *,
    mode: str = "subject",
    anchor: tuple[float, float] = (0.5, 0.5),
    overscan: float = 1.0,
) -> str:
    """Fit any source into the delivery frame.

    `subject` keeps the measured subject inside the crop instead of trusting
    that whatever was in the middle of a horizontal frame is still the point in
    a vertical one. `blur-pad` keeps the whole frame and fills the margins with
    a blown-up blurred copy of itself — the social-video convention.
    """
    width = max(2, int(round(target_w * overscan)) // 2 * 2)
    height = max(2, int(round(target_h * overscan)) // 2 * 2)

    if mode == "fill":
        return f"scale={width}:{height}:flags=bicubic,setsar=1"

    if mode == "blur-pad":
        # Background: cover the frame, blur it hard, darken it. Foreground: fit.
        return (
            f"split=2[pad_bg][pad_fg];"
            f"[pad_bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma={max(width, height) / 42:.1f},eq=brightness=-0.10[pad_b];"
            f"[pad_fg]scale={width}:{height}:force_original_aspect_ratio=decrease[pad_f];"
            f"[pad_b][pad_f]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )

    # Cover the frame, then choose where the crop sits.
    if mode == "subject":
        ax = min(max(anchor[0], 0.0), 1.0)
        ay = min(max(anchor[1], 0.0), 1.0)
        # Pull the crop toward the subject, but only 70% of the way: a crop
        # pinned exactly on a measured point looks nervous.
        ax = 0.5 + (ax - 0.5) * 0.7
        ay = 0.5 + (ay - 0.5) * 0.7
        x = f"max(0\\,min(iw-{width}\\,iw*{ax:.4f}-{width}/2))"
        y = f"max(0\\,min(ih-{height}\\,ih*{ay:.4f}-{height}/2))"
    else:
        x, y = "(iw-ow)/2", "(ih-oh)/2"

    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=bicubic,"
        f"crop={width}:{height}:{x}:{y},setsar=1"
    )


def motion_chain(
    motion: Motion,
    *,
    target_w: int,
    target_h: int,
    fps: int,
    duration: float,
) -> str:
    """Realise an invented camera move, ending at exactly the target size.

    The input is expected to be overscanned (see :data:`OVERSCAN`) so there is
    room to move without revealing an edge.
    """
    frames = max(2, int(round(duration * fps)))
    last = frames - 1
    intensity = min(max(motion.intensity, 0.0), 1.0)
    ax = min(max(motion.anchor[0], 0.0), 1.0)
    ay = min(max(motion.anchor[1], 0.0), 1.0)

    if motion.kind == "none" or intensity <= 0.01:
        # Still needs to land on the delivery size: take the middle of the overscan.
        return f"crop={target_w}:{target_h}:(iw-ow)/2:(ih-oh)/2"

    if motion.kind in ("drift-left", "drift-right", "float", "shake"):
        return _translation_move(motion.kind, intensity, target_w, target_h, duration)

    return _zoom_move(motion.kind, intensity, ax, ay, target_w, target_h, fps, last)


def _translation_move(kind: str, intensity: float, target_w: int, target_h: int, duration: float) -> str:
    """Pure pans and handheld float — cheaper and smoother than a zoom filter."""
    travel = 0.5 + 0.5 * intensity  # fraction of the available overscan to use
    duration = max(duration, 0.05)

    if kind == "drift-left":
        # Frame slides left across the picture, so the crop window moves right.
        x = f"(in_w-out_w)*({0.5 - travel / 2:.4f}+{travel:.4f}*t/{duration:.4f})"
        y = "(in_h-out_h)/2"
    elif kind == "drift-right":
        x = f"(in_w-out_w)*({0.5 + travel / 2:.4f}-{travel:.4f}*t/{duration:.4f})"
        y = "(in_h-out_h)/2"
    elif kind == "float":
        # A slow lissajous: reads as a handheld operator breathing.
        amp = 0.5 * intensity
        x = f"(in_w-out_w)*(0.5+{amp:.4f}*sin(t*0.9))"
        y = f"(in_h-out_h)*(0.5+{amp:.4f}*cos(t*0.7))"
    else:  # shake
        amp = 0.5 * intensity
        x = f"(in_w-out_w)*(0.5+{amp:.4f}*sin(t*23.0))"
        y = f"(in_h-out_h)*(0.5+{amp:.4f}*cos(t*19.0))"

    return f"crop={target_w}:{target_h}:{x}:{y}"


def _zoom_move(
    kind: str, intensity: float, ax: float, ay: float,
    target_w: int, target_h: int, fps: int, last: int,
) -> str:
    """Push-ins, pull-outs and Ken Burns, via zoompan.

    zoompan is driven by the output frame counter `on`, so the move always
    completes exactly at the end of the shot regardless of source frame rate.
    """
    span = max(last, 1)
    depth = 1.0 + 0.10 + 0.30 * intensity  # 1.10x .. 1.40x

    if kind == "pull-out":
        z = f"{depth:.4f}-({depth - 1.0:.4f})*on/{span}"
    elif kind == "ken-burns":
        z = f"1+({depth - 1.0:.4f})*on/{span}"
    else:  # punch-in
        z = f"1+({depth - 1.0:.4f})*on/{span}"

    if kind == "ken-burns":
        # Drift from the middle of frame toward the subject as the zoom builds.
        cx = f"(0.5+({ax - 0.5:.4f})*on/{span})"
        cy = f"(0.5+({ay - 0.5:.4f})*on/{span})"
    else:
        cx, cy = f"{0.5 + (ax - 0.5) * 0.6:.4f}", f"{0.5 + (ay - 0.5) * 0.6:.4f}"

    x = f"max(0\\,min(iw-iw/zoom\\,iw*{cx}-(iw/zoom)/2))"
    y = f"max(0\\,min(ih-ih/zoom\\,ih*{cy}-(ih/zoom)/2))"

    return (
        f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={target_w}x{target_h}:fps={fps}"
    )


# ---------------------------------------------------------------------------
# Speed
# ---------------------------------------------------------------------------

def ramp_video_graph(
    ramp: Ramp,
    *,
    source_duration: float,
    in_label: str,
    out_label: str,
    optical_flow: bool = False,
    fps: int = 30,
) -> str:
    """Filter graph applying a speed curve to video.

    A constant speed is one setpts. A curve is approximated by slicing the
    source into short constant-speed pieces and concatenating them — enough
    slices and the steps are below perception, and unlike a single expression
    it stays exact about total screen time.
    """
    ramp = ramp.normalise()

    if ramp.is_flat:
        speed = max(ramp.constant_speed, 1e-6)
        links = [f"setpts=PTS/{speed:.6f}"]
        if optical_flow and speed < 0.55:
            # Synthesise the frames slow motion is missing instead of repeating them.
            links.append(f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1")
        return f"[{in_label}]{chain(*links)}[{out_label}]"

    screen_time = ramp.output_duration(source_duration)
    slices = int(min(max(screen_time * RAMP_SLICES_PER_SECOND, RAMP_MIN_SLICES), RAMP_MAX_SLICES))
    step = source_duration / slices

    parts = [f"[{in_label}]split={slices}" + "".join(f"[rs{i}]" for i in range(slices))]
    for index in range(slices):
        start = index * step
        end = source_duration if index == slices - 1 else (index + 1) * step
        speed = max(ramp.speed_at((index + 0.5) / slices), 1e-6)
        links = [
            f"trim=start={start:.6f}:end={end:.6f}",
            "setpts=PTS-STARTPTS",
            f"setpts=PTS/{speed:.6f}",
        ]
        if optical_flow and speed < 0.55:
            links.append(f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1")
        parts.append(f"[rs{index}]{chain(*links)}[rv{index}]")

    inputs = "".join(f"[rv{i}]" for i in range(slices))
    parts.append(f"{inputs}concat=n={slices}:v=1:a=0[{out_label}]")
    return ";".join(parts)


def ramp_audio_graph(ramp: Ramp, *, source_duration: float, in_label: str, out_label: str) -> str:
    """The same speed curve, applied to sound.

    atempo preserves pitch, which is what you want for dialogue under a mild
    ramp and emphatically not what you want under a dramatic one — but a
    dramatic ramp is normally scored, not sync, so pitch preservation wins.
    """
    ramp = ramp.normalise()

    if ramp.is_flat:
        return f"[{in_label}]{_atempo(ramp.constant_speed)}[{out_label}]"

    screen_time = ramp.output_duration(source_duration)
    slices = int(min(max(screen_time * RAMP_SLICES_PER_SECOND, RAMP_MIN_SLICES), RAMP_MAX_SLICES))
    step = source_duration / slices

    parts = [f"[{in_label}]asplit={slices}" + "".join(f"[as{i}]" for i in range(slices))]
    for index in range(slices):
        start = index * step
        end = source_duration if index == slices - 1 else (index + 1) * step
        speed = max(ramp.speed_at((index + 0.5) / slices), 1e-6)
        parts.append(
            f"[as{index}]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS,"
            f"{_atempo(speed)}[aa{index}]"
        )

    inputs = "".join(f"[aa{i}]" for i in range(slices))
    parts.append(f"{inputs}concat=n={slices}:v=0:a=1[{out_label}]")
    return ";".join(parts)


def _atempo(speed: float) -> str:
    """atempo only accepts 0.5–100x, so extreme speeds are factorised."""
    speed = max(min(speed, 100.0), 0.02)
    stages: list[float] = []
    remaining = speed
    while remaining < 0.5:
        stages.append(0.5)
        remaining /= 0.5
    while remaining > 100.0:  # pragma: no cover - absurd but cheap to guard
        stages.append(100.0)
        remaining /= 100.0
    stages.append(remaining)
    return ",".join(f"atempo={stage:.6f}" for stage in stages)


def frame_of(anchor: tuple[float, float]) -> tuple[float, float]:
    """Clamp a subject anchor away from the very edge of frame."""
    return (
        min(max(anchor[0], 0.08), 0.92),
        min(max(anchor[1], 0.08), 0.92),
    )


def shake_intensity_for(energy: float) -> float:
    """Impact shake that scales with how hard the moment is meant to hit."""
    return float(min(max((energy - 0.6) * 1.6, 0.0), 0.6))


def ease(t: float, kind: str = "in-out") -> float:
    """Easing curves, for anything that interpolates over a shot."""
    t = min(max(t, 0.0), 1.0)
    if kind == "in":
        return t * t
    if kind == "out":
        return 1.0 - (1.0 - t) ** 2
    if kind == "expo":
        return 0.0 if t == 0 else 2 ** (10 * (t - 1))
    if kind == "sine":
        return 0.5 - 0.5 * math.cos(math.pi * t)
    return t * t * (3.0 - 2.0 * t)
