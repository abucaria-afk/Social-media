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
    if amount <= 0.001:
        return ""
    # Larger angle = subtler falloff; map 0..1 onto a tasteful range.
    angle = 1.5 - 0.85 * min(amount, 1.0)
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
