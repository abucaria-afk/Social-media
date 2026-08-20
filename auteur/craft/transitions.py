"""Joins: how one shot becomes the next.

Most of these map onto ffmpeg's built-in xfade transitions. Four do not, because
the built-ins do not contain them and they are exactly the joins that read as
expensive:

* **whip** — a directional slide with a real motion smear, so it reads as the
  camera having thrown itself at the next shot rather than the two shots
  sliding past each other.
* **light leak** — a warm flash blooming through the join, the way film does
  when the magazine is opened.
* **portal** — the next shot opens through a hole in this one, so for a few
  frames part of the outgoing frame is still on screen around the incoming
  one. `circleopen` is the nearest built-in and it is not the same thing: it
  is always centred on the frame, and it eases, which means the aperture is
  half open one frame in and the join reads as a wipe rather than as an
  opening.
* **carry** — the frame does not change all at once. The edges are already the
  next shot while the middle is still the last one, and the middle leaves
  after. This is the join people mean when they say part of the previous photo
  is still on the next one.

All four are written as xfade custom expressions. Support for those is probed once
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
    # Fallbacks only. Neither built-in is the join — see the module docstring —
    # but a build that rejects custom expressions should still get something
    # shaped roughly right rather than a default cross-dissolve.
    "portal": "circleopen",
    "carry": "fade",
}

#: Planes in yuv420p, and the width divisor each one uses.
_PLANES = ((0, 1), (1, 2), (2, 2))

#: How far through the join we are, 0 at the first frame and 1 at the last.
#:
#: **Not** xfade's own `P`. The documentation says `P` is "progress of the
#: transition effect, 0.0 to 1.0"; measured against the actual binary, by
#: writing `P*200` into the luma plane and reading the raw frames back, it
#: falls from 1 to 0 across the join. Every custom expression here was written
#: to the documented direction, so every one of them — all four whips, the
#: light leak and the film burn — was rendering backwards: the whip travelled
#: away from the shot it was meant to be thrown at, and the leak dissolved from
#: the incoming shot back to the outgoing one. Symmetric terms like
#: `sin(T*PI)` hid it, which is why it survived this long.
#:
#: Written once, here, so the next expression cannot get it wrong on its own.
T = "(1-P)"


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
        smear = f"({extent}*0.045*sin({T}*PI))"

        # Outgoing shot travels a full frame in the first half; incoming shot
        # arrives from the opposite side across the second half.
        sign = "+" if forward else "-"
        counter = "-" if forward else "+"
        out_pos = f"({axis}{sign}{extent}*{T}*2)"
        in_pos = f"({axis}{counter}{extent}*(1-{T})*2)"

        def tap(source: str, position: str, offset: str) -> str:
            coord = f"{position}{offset}"
            return (
                f"{source}{plane}({coord}\\,{other})"
                if horizontal
                else f"{source}{plane}({other}\\,{coord})"
            )

        outgoing = f"({tap('a', out_pos, f'-{smear}')}+{tap('a', out_pos, '')}+{tap('a', out_pos, f'+{smear}')})/3"
        incoming = f"({tap('b', in_pos, f'-{smear}')}+{tap('b', in_pos, '')}+{tap('b', in_pos, f'+{smear}')})/3"
        return f"if(lt({T}\\,0.5)\\,{outgoing}\\,{incoming})"

    return _plane_expr(build)


def _light_leak_expr(warm: bool) -> str:
    """Cross-dissolve with a flash blooming through the middle of the join."""

    def build(plane: int, divisor: int) -> str:
        mix = f"(A*(1-{T})+B*{T})"
        flash = f"sin({T}*PI)"
        if plane == 0:
            return f"min(255\\,{mix}+{160 if warm else 190}*{flash})"
        # Push chroma toward amber (U down, V up) for a film-burn feel.
        if warm:
            shift = -46 if plane == 1 else 54
            return f"max(0\\,min(255\\,{mix}+{shift}*{flash}))"
        return mix

    return _plane_expr(build)


def _portal_expr(fx: float = 0.5, fy: float = 0.46) -> str:
    """An aperture opening from a point, with the outgoing shot around it.

    Squared rather than eased, which is the same correction the browser
    renderer needed: an ease-out aperture is 47% open one frame into the join,
    so nobody ever sees it open. Squared, it starts small and accelerates, and
    the first frames are the ones that read as a hole.

    `fy` sits above centre by default because the subject of a phone photograph
    usually does, and an aperture that opens on somebody's chest is a wipe.

    No `_plane_expr` wrapper, unlike the whips. Measured against the binary:
    `X` and `Y` are *frame* coordinates in every plane, while `W` and `H` are
    the frame's dimensions everywhere — so geometry written in X, Y, W and H is
    already plane-independent. Halving them for chroma, which is what the
    wrapper is for, drew a second aperture at quarter width: a blue circle in
    the top-left corner and a dark red one in the middle, on the same frame.
    """
    cx = f"(W*{fx:.4f})"
    cy = f"(H*{fy:.4f})"
    # Far enough to clear the furthest corner, whichever corner that is once
    # the centre is off-centre.
    radius = f"({T}*{T}*hypot(W\\,H))"
    distance = f"hypot(X-{cx}\\,Y-{cy})"
    # A soft edge, or the hole has stair-stepped sides on every diagonal.
    edge = "(W*0.02+1)"
    share = f"clip(({radius}-{distance})/{edge}\\,0\\,1)"
    return f"A+(B-A)*{share}"


def _carry_expr() -> str:
    """The edges change first and the middle carries, then leaves.

    Not segmentation — nothing here knows what the subject is. What it knows is
    that the middle of a frame is where the subject of a phone photograph
    almost always is, so holding the middle a beat longer than the edges is the
    cheap version of holding the subject, and it reads as the expensive one.

    Frame coordinates throughout, for the reason given in `_portal_expr`.
    """
    # How central this pixel is: 1 in the middle, 0 outside a soft ellipse
    # covering a bit under half the frame.
    reach = "hypot((X-W/2)/(W*0.44)\\,(Y-H/2)/(H*0.44))"
    hold = f"clip(1-{reach}\\,0\\,1)"
    # Each pixel gets its own window on the progress. The edges are done inside
    # the first quarter of the join; the middle does not start until halfway.
    lo = f"(0.5*{hold})"
    hi = f"({lo}+0.25+0.25*{hold})"
    share = f"clip(({T}-{lo})/({hi}-{lo})\\,0\\,1)"
    return f"A+(B-A)*{share}"


CUSTOM_EXPRESSIONS: dict[str, str] = {
    "portal": _portal_expr(),
    "carry": _carry_expr(),
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


def describe() -> str:
    lines = []
    for name in sorted(set(BUILTIN) | set(CUSTOM_EXPRESSIONS)):
        how = "custom" if name in CUSTOM_EXPRESSIONS else f"xfade:{BUILTIN[name]}"
        lines.append(f"  {name:<14} {how}")
    return "\n".join(lines)
