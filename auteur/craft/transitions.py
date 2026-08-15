"""Joins: how one shot becomes the next.

Most of these map onto ffmpeg's built-in xfade transitions. Two do not, because
the built-ins do not contain them and they are exactly the joins that read as
expensive:

* **whip** — a directional slide with a real motion smear, so it reads as the
  camera having thrown itself at the next shot rather than the two shots
  sliding past each other.
* **light leak** — a warm flash blooming through the join, the way film does
  when the magazine is opened.

Both are written as xfade custom expressions. Support for those is probed once
against the actual binary; if the probe fails, the built-in nearest neighbour is
used instead and nothing downstream notices.
"""

from __future__ import annotations

import functools
import logging
import subprocess

from .. import ffmpeg

log = logging.getLogger("auteur.craft.transitions")

#: Editorial name -> nearest built-in xfade transition.
BUILTIN: dict[str, str] = {
    "dissolve": "fade",
    "dip-to-black": "fadeblack",
    "dip-to-white": "fadewhite",
    "whip-left": "slideleft",
    "whip-right": "slideright",
    "whip-up": "slideup",
    "whip-down": "slidedown",
    "glitch": "pixelize",
    "light-leak": "fadewhite",
    "film-burn": "fadewhite",
    "zoom-blur": "zoomin",
    "slide-left": "smoothleft",
    "slide-right": "smoothright",
    "wipe": "wiperight",
    "morph": "distance",
}

#: Planes in yuv420p, and the width divisor each one uses.
_PLANES = ((0, 1), (1, 2), (2, 2))


def _plane_expr(builder) -> str:
    """Build a per-plane expression, since chroma planes are half width.

    xfade evaluates the expression once per plane and exposes W as the frame
    width, not the plane width — so a shift written in frame pixels moves chroma
    twice as far as luma unless it is corrected here.
    """
    expr = builder(0, 1)
    for plane, divisor in _PLANES[1:]:
        expr = f"if(eq(PLANE\\,{plane})\\,{builder(plane, divisor)}\\,{expr})"
    return expr


def _whip_expr(direction: str) -> str:
    """Directional slide with a three-tap smear that peaks mid-transition."""
    horizontal = direction in ("whip-left", "whip-right")
    forward = direction in ("whip-left", "whip-up")

    def build(plane: int, divisor: int) -> str:
        extent = f"(W/{divisor})" if horizontal else f"(H/{divisor})"
        axis = "X" if horizontal else "Y"
        other = "Y" if horizontal else "X"
        smear = f"({extent}*0.045*sin(P*PI))"

        # Outgoing shot travels a full frame in the first half; incoming shot
        # arrives from the opposite side across the second half.
        sign = "+" if forward else "-"
        counter = "-" if forward else "+"
        out_pos = f"({axis}{sign}{extent}*P*2)"
        in_pos = f"({axis}{counter}{extent}*(1-P)*2)"

        def tap(source: str, position: str, offset: str) -> str:
            coord = f"{position}{offset}"
            return (
                f"{source}{plane}({coord}\\,{other})"
                if horizontal
                else f"{source}{plane}({other}\\,{coord})"
            )

        outgoing = f"({tap('a', out_pos, f'-{smear}')}+{tap('a', out_pos, '')}+{tap('a', out_pos, f'+{smear}')})/3"
        incoming = f"({tap('b', in_pos, f'-{smear}')}+{tap('b', in_pos, '')}+{tap('b', in_pos, f'+{smear}')})/3"
        return f"if(lt(P\\,0.5)\\,{outgoing}\\,{incoming})"

    return _plane_expr(build)


def _light_leak_expr(warm: bool) -> str:
    """Cross-dissolve with a flash blooming through the middle of the join."""

    def build(plane: int, divisor: int) -> str:
        mix = "(A*(1-P)+B*P)"
        flash = "sin(P*PI)"
        if plane == 0:
            return f"min(255\\,{mix}+{160 if warm else 190}*{flash})"
        # Push chroma toward amber (U down, V up) for a film-burn feel.
        if warm:
            shift = -46 if plane == 1 else 54
            return f"max(0\\,min(255\\,{mix}+{shift}*{flash}))"
        return mix

    return _plane_expr(build)


CUSTOM_EXPRESSIONS: dict[str, str] = {
    "whip-left": _whip_expr("whip-left"),
    "whip-right": _whip_expr("whip-right"),
    "whip-up": _whip_expr("whip-up"),
    "whip-down": _whip_expr("whip-down"),
    "light-leak": _light_leak_expr(warm=False),
    "film-burn": _light_leak_expr(warm=True),
}


@functools.lru_cache(maxsize=1)
def supports_custom() -> bool:
    """Probe whether this ffmpeg build accepts xfade custom expressions.

    Cheap enough to run once per process, and it removes a whole class of
    render failures on unfamiliar builds.
    """
    expr = CUSTOM_EXPRESSIONS["whip-left"]
    command = [
        str(ffmpeg.ffmpeg_path()),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=red:s=64x64:r=10:d=1",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=64x64:r=10:d=1",
        "-filter_complex",
        f"[0:v][1:v]xfade=transition=custom:duration=0.4:offset=0.2:expr='{expr}',format=yuv420p",
        "-frames:v",
        "4",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment dependent
        return False
    ok = result.returncode == 0
    if not ok:
        log.info("this ffmpeg build rejects xfade custom expressions; using built-in transitions")
    return ok


def xfade_spec(kind: str, duration: float, offset: float) -> str:
    """The xfade filter for a named join, ready to drop into a filtergraph."""
    duration = max(duration, 0.04)
    offset = max(offset, 0.0)

    if kind in CUSTOM_EXPRESSIONS and supports_custom():
        expr = CUSTOM_EXPRESSIONS[kind]
        return f"xfade=transition=custom:duration={duration:.4f}:offset={offset:.4f}:expr='{expr}'"

    builtin = BUILTIN.get(kind, "fade")
    return f"xfade=transition={builtin}:duration={duration:.4f}:offset={offset:.4f}"


def audio_join(duration: float) -> str:
    """The matching sound join. A picture dissolve with a hard audio cut is a tell."""
    return f"acrossfade=d={max(duration, 0.02):.4f}:c1=tri:c2=tri"


def describe() -> str:
    lines = []
    for name in sorted(set(BUILTIN) | set(CUSTOM_EXPRESSIONS)):
        how = "custom" if name in CUSTOM_EXPRESSIONS else f"xfade:{BUILTIN[name]}"
        lines.append(f"  {name:<14} {how}")
    return "\n".join(lines)
