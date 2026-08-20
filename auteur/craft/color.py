"""Grade: film emulation, shot matching, and the texture that sells it.

Two separate jobs live here and they must not be confused:

*Shot matching* is corrective. It pulls every shot toward a shared exposure and
white balance so a bin of clips shot on different days reads as one film. It is
invisible when it works, and it is the reason amateur montages look like
collages and professional ones do not.

*The look* is expressive. It is applied on top, after everything already
matches, and it is what the audience actually notices.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from ..edl import Look
from ..ffmpeg import chain

#: Bloom needs named filter pads. They must be unique within a filtergraph, and
#: a single render can stack several blooms (per-shot look plus film texture).
_pad_counter = itertools.count()


@dataclass(frozen=True)
class LookSpec:
    name: str
    description: str
    #: Filter chain at full strength. Built by the functions below.
    build: object  # Callable[[float], str]


def _blend(neutral: float, target: float, strength: float) -> float:
    return neutral + (target - neutral) * strength


def _eq(
    *,
    contrast: float = 1.0,
    brightness: float = 0.0,
    saturation: float = 1.0,
    gamma: float = 1.0,
    strength: float = 1.0,
) -> str:
    contrast = _blend(1.0, contrast, strength)
    brightness = _blend(0.0, brightness, strength)
    saturation = _blend(1.0, saturation, strength)
    gamma = _blend(1.0, gamma, strength)
    return (
        f"eq=contrast={contrast:.4f}:brightness={brightness:.4f}"
        f":saturation={saturation:.4f}:gamma={gamma:.4f}"
    )


def _balance(
    shadows: tuple[float, float, float],
    mids: tuple[float, float, float],
    highs: tuple[float, float, float],
    strength: float = 1.0,
) -> str:
    rs, gs, bs = (_blend(0.0, v, strength) for v in shadows)
    rm, gm, bm = (_blend(0.0, v, strength) for v in mids)
    rh, gh, bh = (_blend(0.0, v, strength) for v in highs)
    return (
        f"colorbalance=rs={rs:.4f}:gs={gs:.4f}:bs={bs:.4f}"
        f":rm={rm:.4f}:gm={gm:.4f}:bm={bm:.4f}"
        f":rh={rh:.4f}:gh={gh:.4f}:bh={bh:.4f}"
    )


def _bloom(radius: float, opacity: float, threshold: float = 0.62) -> str:
    """Halation: blur what is bright, screen it back over the picture.

    This is the single filter that makes digital footage read as film. Real
    highlights bleed into the emulsion around them; sensors clip instead.
    """
    if opacity <= 0.001:
        return ""
    tag = next(_pad_counter)
    base, bright, blurred = f"bl{tag}a", f"bl{tag}b", f"bl{tag}c"
    return (
        f"split=2[{base}][{bright}];"
        f"[{bright}]curves=all='0/0 {threshold:.2f}/0 1/1',gblur=sigma={radius:.2f}[{blurred}];"
        f"[{base}][{blurred}]blend=all_mode=screen:all_opacity={opacity:.3f}"
    )


def _vignette(amount: float) -> str:
    """Darken the corners. `amount` is 0 (none) to 1 (as far as this goes).

    The mapping used to run the other way. ffmpeg's `angle` is the lens angle
    and a *larger* one vignettes harder — the comment here claimed the
    opposite, so every look in this file asked for a subtle vignette and got a
    near-maximum one. Measured on a photograph with a mean luma of 121: the
    2020s look's nominal 0.16 produced angle 1.36 and a mean of 45, which is
    62% of the light in the frame removed by the gentlest vignette on offer.
    That is most of why graded frames came out looking heavy and dark.

    Calibrated rather than guessed. On the same photograph this maps 0.16 to
    about -3% mean luma, 0.30 to -5%, and 1.0 to -24%, which is a strong
    vignette that still leaves a picture behind it.
    """
    if amount <= 0.001:
        return ""
    angle = 0.15 + 0.55 * min(amount, 1.0)
    return f"vignette=angle={angle:.3f}:mode=forward"


# ---------------------------------------------------------------------------
# The looks
# ---------------------------------------------------------------------------


def _neutral(s: float) -> str:
    return chain(_eq(contrast=1.06, saturation=1.05, strength=s))


def _blockbuster(s: float) -> str:
    """Teal shadows, orange skin. The summer tentpole grade."""
    return chain(
        _balance(
            shadows=(-0.09, 0.02, 0.13),
            mids=(0.03, 0.0, -0.02),
            highs=(0.10, 0.03, -0.09),
            strength=s,
        ),
        _eq(contrast=1.18, saturation=1.14, gamma=0.97, strength=s),
        _vignette(0.35 * s),
    )


def _steel(s: float) -> str:
    """Cold, hard, desaturated — the grade for concrete and consequence."""
    return chain(
        _balance(
            shadows=(-0.10, -0.02, 0.14),
            mids=(-0.04, 0.0, 0.06),
            highs=(-0.03, 0.0, 0.05),
            strength=s,
        ),
        _eq(contrast=1.22, saturation=0.72, gamma=0.94, strength=s),
        "curves=all='0/0 0.28/0.20 0.75/0.80 1/1'" if s > 0.5 else "",
        _vignette(0.42 * s),
    )


def _amber(s: float) -> str:
    """Warm, lifted, nostalgic. Late sun through a window."""
    return chain(
        _balance(
            shadows=(0.05, 0.02, -0.06),
            mids=(0.07, 0.02, -0.07),
            highs=(0.12, 0.05, -0.10),
            strength=s,
        ),
        _eq(contrast=1.04, saturation=1.10, gamma=1.05, strength=s),
        _bloom(radius=14.0, opacity=0.22 * s),
        _vignette(0.25 * s),
    )


def _noir(s: float) -> str:
    return chain(
        f"hue=s={_blend(1.0, 0.0, s):.3f}",
        _eq(contrast=1.42, saturation=1.0, gamma=0.92, strength=s),
        "curves=all='0/0 0.22/0.10 0.78/0.92 1/1'" if s > 0.4 else "",
        _vignette(0.55 * s),
    )


def _neon(s: float) -> str:
    """Magenta and cyan, blown highlights, wet streets."""
    return chain(
        _balance(
            shadows=(0.06, -0.06, 0.15),
            mids=(0.02, -0.03, 0.08),
            highs=(0.10, -0.04, 0.12),
            strength=s,
        ),
        _eq(contrast=1.26, saturation=1.42, gamma=0.93, strength=s),
        _bloom(radius=18.0, opacity=0.34 * s, threshold=0.55),
        _vignette(0.45 * s),
    )


def _kodak(s: float) -> str:
    """A print-film curve: green in the shadows, warm highlights, soft toe."""
    return chain(
        _balance(
            shadows=(-0.04, 0.06, -0.02),
            mids=(0.03, 0.0, -0.03),
            highs=(0.08, 0.02, -0.06),
            strength=s,
        ),
        (
            "curves=r='0/0.02 0.5/0.52 1/0.98':g='0/0.01 0.5/0.5 1/0.99':b='0/0.04 0.5/0.48 1/0.95'"
            if s > 0.3
            else ""
        ),
        _eq(contrast=1.05, saturation=0.96, gamma=1.03, strength=s),
        _bloom(radius=10.0, opacity=0.16 * s),
        _vignette(0.30 * s),
    )


def _bleach_bypass(s: float) -> str:
    """Silver retained: crushed, grey, brutal."""
    return chain(
        _eq(contrast=1.5, saturation=0.42, gamma=0.9, strength=s),
        "curves=all='0/0 0.3/0.18 0.7/0.86 1/1'" if s > 0.4 else "",
        _vignette(0.5 * s),
    )


def _bloom_look(s: float) -> str:
    """Diffusion filter on the lens: soft, dreamy, lifted."""
    return chain(
        _bloom(radius=24.0, opacity=0.42 * s, threshold=0.45),
        _balance(
            shadows=(0.04, 0.02, 0.06), mids=(0.02, 0.0, 0.02), highs=(0.04, 0.02, 0.04), strength=s
        ),
        _eq(contrast=0.94, saturation=1.06, gamma=1.08, strength=s),
    )


def _punch(s: float) -> str:
    return chain(
        _eq(contrast=1.2, saturation=1.35, gamma=0.98, strength=s),
        "unsharp=5:5:0.7:5:5:0.0" if s > 0.5 else "",
    )


def _desert(s: float) -> str:
    return chain(
        _balance(
            shadows=(0.06, 0.02, -0.08),
            mids=(0.08, 0.04, -0.10),
            highs=(0.14, 0.08, -0.12),
            strength=s,
        ),
        _eq(contrast=1.14, saturation=0.92, gamma=1.02, strength=s),
        _vignette(0.36 * s),
    )


def _aqua(s: float) -> str:
    return chain(
        _balance(
            shadows=(-0.12, 0.04, 0.12),
            mids=(-0.08, 0.04, 0.10),
            highs=(-0.04, 0.06, 0.08),
            strength=s,
        ),
        _eq(contrast=1.1, saturation=1.05, gamma=1.04, strength=s),
        _bloom(radius=16.0, opacity=0.2 * s),
    )


# ---------------------------------------------------------------------------
# The decades
#
# Ports of the six era recipes in `tools/artifact/era.js`, which the browser
# renderer applies per-pixel because a photograph is graded once. A clip pays
# per frame, so the same look has to be built out of ffmpeg filters instead —
# and it is worth being exact about what survives that translation and what
# does not, rather than shipping two things called "1980s" that do not match.
#
# Survives: the tone curve (as `curves` control points computed from the same
# lift/gain/gamma), the split tone (as `colorbalance`), contrast and
# saturation, halation (as a thresholded bloom), grain (as `noise`), the
# chroma bleed that makes VHS look like VHS (as `rgbashift`), and the
# vignette.
#
# Does not: the scanlines. Interlacing needs a per-pixel expression, `geq` is
# far too slow to run over every frame of a film, and a wrong-but-fast
# approximation of scanlines reads as a screen-door artefact rather than as
# 1985. Left out and said so.
# ---------------------------------------------------------------------------


def _film_curve(
    lift: tuple[float, float, float],
    gain: tuple[float, float, float],
    gamma: tuple[float, float, float],
) -> str:
    """Where black and white land per channel, as `curves` control points.

    The same arithmetic the browser does in a lookup table: an input `x` comes
    out at `lift + (gain - lift) * x ** gamma`. Sampled at five points, which
    `curves` interpolates between smoothly enough that the difference from the
    full table is well under a quantisation step.
    """
    stops = (0.0, 0.25, 0.5, 0.75, 1.0)
    parts = []
    for name, low, high, power in zip("rgb", lift, gain, gamma, strict=True):
        points = " ".join(
            f"{x:.2f}/{max(0.0, min(1.0, low + (high - low) * (x**power))):.4f}" for x in stops
        )
        parts.append(f"{name}='{points}'")
    return "curves=" + ":".join(parts)


def _grain(amount: float) -> str:
    """Film grain. Temporal, so it moves — static grain reads as sensor dirt."""
    if amount <= 0.01:
        return ""
    return f"noise=alls={max(1, round(amount * 42))}:allf=t+u"


def _chroma_bleed(pixels: float) -> str:
    """Colour drawn to the side of the thing it belongs to, which is composite
    video's single most recognisable artefact."""
    if pixels <= 0.05:
        return ""
    shift = max(1, round(pixels))
    return f"rgbashift=rh={shift}:bh=-{shift}"


def _era(
    *,
    lift: tuple[float, float, float],
    gain: tuple[float, float, float],
    gamma: tuple[float, float, float],
    contrast: float,
    saturation: float,
    shadows: tuple[float, float, float],
    highs: tuple[float, float, float],
    halation: float,
    grain: float,
    chroma: float,
    vignette: float,
    strength: float,
) -> str:
    """One decade, built from the same numbers the browser grades with."""
    s = max(0.0, min(1.0, strength))
    return chain(
        _film_curve(lift, gain, gamma) if s > 0.3 else "",
        _balance(shadows=shadows, mids=(0.0, 0.0, 0.0), highs=highs, strength=s),
        _eq(contrast=contrast, saturation=saturation, gamma=1.0, strength=s),
        _bloom(radius=14.0, opacity=halation * 0.5 * s, threshold=0.62),
        _chroma_bleed(chroma * s),
        _grain(grain * s),
        _vignette(vignette * s),
    )


def _seventies(s: float) -> str:
    """Super 8 — orange, heavy grain, soft blacks."""
    return _era(
        lift=(0.085, 0.055, 0.030),
        gain=(1.0, 0.965, 0.885),
        gamma=(0.94, 1.0, 1.08),
        contrast=0.82,
        saturation=0.92,
        shadows=(0.030, 0.010, -0.010),
        highs=(0.055, 0.022, -0.030),
        halation=0.55,
        grain=0.30,
        chroma=0.0,
        vignette=0.46,
        strength=s,
    )


def _eighties(s: float) -> str:
    """VHS — chroma bleeding sideways and blown highlights."""
    return _era(
        lift=(0.055, 0.045, 0.078),
        gain=(0.96, 0.92, 0.97),
        gamma=(1.0, 1.02, 0.96),
        contrast=1.14,
        saturation=1.26,
        shadows=(0.006, -0.008, 0.038),
        highs=(0.020, 0.014, -0.004),
        halation=0.30,
        grain=0.13,
        chroma=2.6,
        vignette=0.34,
        strength=s,
    )


def _nineties(s: float) -> str:
    """Kodak Gold — golden, milky blacks, grain you can see."""
    return _era(
        lift=(0.070, 0.062, 0.048),
        gain=(1.02, 0.99, 0.925),
        gamma=(0.96, 1.0, 1.06),
        contrast=0.90,
        saturation=1.05,
        shadows=(0.008, 0.018, 0.010),
        highs=(0.048, 0.028, -0.018),
        halation=0.40,
        grain=0.22,
        chroma=0.0,
        vignette=0.30,
        strength=s,
    )


def _y2k(s: float) -> str:
    """Point-and-shoot flash — hard, cool, and clipped."""
    return _era(
        lift=(0.012, 0.016, 0.030),
        gain=(0.99, 0.98, 0.97),
        gamma=(1.06, 1.04, 1.0),
        contrast=1.24,
        saturation=1.14,
        shadows=(-0.012, 0.004, 0.030),
        highs=(0.020, 0.020, 0.012),
        halation=0.18,
        grain=0.07,
        chroma=0.8,
        vignette=0.20,
        strength=s,
    )


def _tens(s: float) -> str:
    """The filter era — faded blacks, teal shadows, warm skin."""
    return _era(
        lift=(0.090, 0.098, 0.104),
        gain=(0.985, 0.985, 0.965),
        gamma=(1.0, 1.0, 1.0),
        contrast=1.06,
        saturation=0.94,
        shadows=(-0.030, 0.012, 0.046),
        highs=(0.050, 0.026, -0.020),
        halation=0.22,
        grain=0.06,
        chroma=0.0,
        vignette=0.42,
        strength=s,
    )


def _twenties(s: float) -> str:
    """Phone HDR — everything visible, nothing hidden."""
    return _era(
        lift=(0.0, 0.0, 0.004),
        gain=(1.04, 1.04, 1.05),
        gamma=(0.98, 0.98, 0.98),
        contrast=1.16,
        saturation=1.16,
        shadows=(-0.008, 0.0, 0.016),
        highs=(0.012, 0.010, 0.008),
        halation=0.10,
        grain=0.0,
        chroma=0.0,
        vignette=0.16,
        strength=s,
    )


LOOKS: dict[str, LookSpec] = {
    "neutral": LookSpec("neutral", "clean, barely touched", _neutral),
    "blockbuster": LookSpec("blockbuster", "teal shadows, orange highlights", _blockbuster),
    "steel": LookSpec("steel", "cold, hard, desaturated", _steel),
    "amber": LookSpec("amber", "warm, lifted, nostalgic", _amber),
    "noir": LookSpec("noir", "high-contrast monochrome", _noir),
    "neon": LookSpec("neon", "magenta and cyan, blown highlights", _neon),
    "kodak": LookSpec("kodak", "print film emulation", _kodak),
    "bleach-bypass": LookSpec("bleach-bypass", "crushed and colourless", _bleach_bypass),
    "bloom": LookSpec("bloom", "soft diffusion, dreamlike", _bloom_look),
    "punch": LookSpec("punch", "vivid and sharp", _punch),
    "desert": LookSpec("desert", "sun-baked yellows", _desert),
    "aqua": LookSpec("aqua", "cool green-blue depth", _aqua),
    # The decades, matching the browser renderer's recipes.
    "1970s": LookSpec("1970s", "Super 8 — orange, grainy, soft blacks", _seventies),
    "1980s": LookSpec("1980s", "VHS — chroma bleed and blown highlights", _eighties),
    "1990s": LookSpec("1990s", "Kodak Gold — golden, milky blacks", _nineties),
    "2000s": LookSpec("2000s", "point-and-shoot flash — hard and cool", _y2k),
    "2010s": LookSpec("2010s", "the filter era — faded, teal and warm", _tens),
    "2020s": LookSpec("2020s", "phone HDR — everything visible", _twenties),
}


def look_chain(look: Look) -> str:
    """The expressive grade for a look. Empty string when nothing to do."""
    spec = LOOKS.get(look.preset)
    if spec is None or look.strength <= 0.001:
        return ""
    return spec.build(look.strength)  # type: ignore[operator]


def correction_chain(look: Look) -> str:
    """The corrective pass: exposure, white balance and saturation matching.

    Applied per shot, before the look, so that the look lands on footage that
    already agrees with itself.
    """
    links: list[str] = []

    if abs(look.exposure) > 0.005 or abs(look.contrast) > 0.005 or abs(look.saturation) > 0.005:
        links.append(
            f"eq=brightness={look.exposure * 0.35:.4f}"
            f":contrast={1.0 + look.contrast * 0.3:.4f}"
            f":saturation={1.0 + look.saturation * 0.6:.4f}"
        )

    if abs(look.temperature) > 0.005:
        # colortemperature works in kelvin; 6500K is neutral daylight.
        kelvin = 6500 - look.temperature * 2200
        links.append(f"colortemperature=temperature={kelvin:.0f}:mix=0.85:pl=0.05")

    return chain(*links)


def texture_chain(amount: float, *, width: int = 1080) -> str:
    """Grain and a whisper of bloom. The last 5% that reads as 'shot on film'."""
    if amount <= 0.01:
        return ""
    links: list[str] = []
    # Grain strength is perceptual: scale it against the frame so 4K and 1080
    # end up looking the same rather than the larger one looking clean.
    strength = max(1, int(round(amount * 11 * (width / 1080) ** 0.5)))
    links.append(f"noise=alls={strength}:allf=t+u")
    if amount > 0.3:
        links.append(_bloom(radius=8.0, opacity=0.12 * amount))
    return chain(*links)


def letterbox_chain(fraction: float, width: int, height: int) -> str:
    """Hard mattes top and bottom, drawn on the frame rather than cropping it."""
    if fraction <= 0.001:
        return ""
    bar = max(2, int(round(height * fraction)) // 2 * 2)
    return (
        f"drawbox=x=0:y=0:w={width}:h={bar}:color=black@1.0:t=fill,"
        f"drawbox=x=0:y={height - bar}:w={width}:h={bar}:color=black@1.0:t=fill"
    )
