"""Reading a frame the way a picture is read, not the way a file is read.

Everything else in this project measures a frame: mean brightness, motion
between frames, edge density. Those are real and they are the wrong unit. A
person standing in front of a painting does not compute the average luminance;
they find where the picture *sends their eye*, notice how it is built, and read
what the light and the colour are doing. That is a different set of questions
and it has different answers.

So this asks the questions an analyst asks:

- **Where does the eye go?** A saliency map from local contrast, edge density
  and colour distinctiveness across three scales, and the centroid of what
  survives. Multi-scale because attention is: you see the bright shape before
  you see the texture in it.
- **How is it built?** Where that focal point sits against the thirds and the
  centre, and which way the dominant edges run — which is how a Dutch angle
  announces itself.
- **What is the light doing?** The *shape* of the tonal histogram rather than
  its mean. Bimodal with an empty middle is chiaroscuro; compressed and high is
  high-key; compressed and low is low-key. A single mean cannot tell those apart
  and they are three different pictures.
- **What are the colours doing to each other?** Hue relationships — a spread of
  fifteen degrees is monochrome, a hundred and eighty is complementary. This is
  a statement about the *relationship*, which is the only thing colour theory
  is ever about.
- **What is in front of what?** Sharpness is a depth cue: a lens can only focus
  at one distance, so the sharp region is the subject and the soft region is
  where it is standing. This is the closest thing to two eyes a single frame
  allows, and it is a real one — it is how every photograph indicates depth.

**What this is not.** It does not know what the footage is *of*. There is no
object recognition here, no faces, no text. It reads structure, light and
colour — the formal properties — and it will describe a beautifully composed
photograph of nothing as a beautifully composed photograph. A curator brings
knowledge of the world; this brings an eye and no memory.

The vocabulary is deliberately the vocabulary of the performance exports —
`Rule of Thirds`, `Chiaroscuro/Moody`, `Triadic` — so a reading can be looked up
against what those compositions actually scored, rather than being a description
nobody can act on.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

#: Composition names, matching `film_theory_virality.shot_composition`.
COMPOSITIONS = (
    "Rule of Thirds",
    "Center Framed",
    "Extreme Close-Up",
    "Low-Angle Hero",
    "Dutch Angle",
)

#: Lighting names, matching `film_theory_virality.lighting_setup`.
LIGHTING = ("Chiaroscuro/Moody", "High-Key Neon", "Natural Flat", "Cyberpunk Accent")

#: Palette names, matching `color_theory_virality.palette_type`.
PALETTES = ("Monochromatic", "Analogous", "Split-Complementary", "Triadic")


@dataclass
class Reading:
    """What one frame is doing, in the language of the exports."""

    #: Where the eye lands, as normalised (x, y).
    focus: tuple[float, float] = (0.5, 0.5)
    #: How concentrated attention is. Near 1 means a single subject; near 0
    #: means the frame is a field with no obvious way in.
    focus_strength: float = 0.0
    composition: str = "Center Framed"
    lighting: str = "Natural Flat"
    palette: str = "Monochromatic"
    #: Dominant hue in degrees, and how far the hues spread.
    hue: float = 0.0
    hue_spread: float = 0.0
    #: 0..1. High means a sharp subject against a soft ground — depth.
    depth_separation: float = 0.0
    #: How the visual weight sits left-to-right; 0 is balanced.
    balance: float = 0.0
    #: Fraction of the frame that carries most of the detail.
    busy: float = 0.0
    contrast: float = 0.0
    luma: float = 0.0
    #: Secondary places the eye goes, best first.
    secondary: tuple[tuple[float, float], ...] = ()
    notes: list[str] = field(default_factory=list)

    @property
    def has_subject(self) -> bool:
        """Is there something to point at, or is this a texture?"""
        return self.focus_strength >= 0.18

    def describe(self) -> str:
        where = _quadrant(self.focus)
        lines = [
            f"{self.composition} · {self.lighting} · {self.palette}",
            f"    eye lands {where} at ({self.focus[0]:.2f}, {self.focus[1]:.2f})"
            f", strength {self.focus_strength:.2f}",
            f"    hue {self.hue:.0f}° spread {self.hue_spread:.0f}°"
            f" · depth {self.depth_separation:.2f} · balance {self.balance:+.2f}",
        ]
        lines.extend(f"    {note}" for note in self.notes)
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "focus": [round(v, 4) for v in self.focus],
            "focus_strength": round(self.focus_strength, 4),
            "composition": self.composition,
            "lighting": self.lighting,
            "palette": self.palette,
            "hue": round(self.hue, 1),
            "hue_spread": round(self.hue_spread, 1),
            "depth_separation": round(self.depth_separation, 4),
            "balance": round(self.balance, 4),
            "busy": round(self.busy, 4),
            "contrast": round(self.contrast, 4),
            "luma": round(self.luma, 4),
            "secondary": [[round(x, 4), round(y, 4)] for x, y in self.secondary],
            "notes": list(self.notes),
        }


def _quadrant(point: tuple[float, float]) -> str:
    x, y = point
    across = "left" if x < 0.4 else "right" if x > 0.6 else "centre"
    down = "high" if y < 0.4 else "low" if y > 0.6 else "middle"
    return f"{down} {across}" if across != "centre" or down != "middle" else "dead centre"


def _blur(field_: np.ndarray, radius: int) -> np.ndarray:
    """A cheap box blur, corrected at the edges.

    `np.convolve(..., mode="same")` zero-pads, so every pixel within a radius of
    the border is averaged against imaginary black. That dims the whole frame
    edge, which pulls any peak-finding inward: a bright subject a fifth of the
    way across the frame was being reported three tenths of the way across,
    and every photograph came back centre-composed.

    Dividing by the same blur of an all-ones array turns the sum back into a
    mean over however many real samples each pixel actually had.
    """
    if radius < 1:
        return field_
    kernel = np.ones(radius, dtype=np.float32)

    def smear(data: np.ndarray) -> np.ndarray:
        out = data
        for axis in (0, 1):
            out = np.apply_along_axis(lambda row: np.convolve(row, kernel, mode="same"), axis, out)
        return out

    counts = smear(np.ones_like(field_, dtype=np.float32))
    return smear(field_.astype(np.float32)) / np.maximum(counts, 1e-6)


def _saliency(gray: np.ndarray, color: np.ndarray) -> np.ndarray:
    """Where the eye goes.

    Centre-surround at three scales: each pixel's distance from its own
    neighbourhood, which is what "stands out" means. Summed across scales
    because attention works at more than one — a bright shape draws you from
    across the room, its texture only once you are close.
    """
    height, width = gray.shape
    salience = np.zeros_like(gray, dtype=np.float32)
    for scale in (
        max(2, min(height, width) // 24),
        max(3, min(height, width) // 10),
        max(5, min(height, width) // 5),
    ):
        surround = _blur(gray, scale)
        salience += np.abs(gray - surround)

    # Colour distinctiveness: distance from the frame's average colour. A red
    # coat in a grey street is salient in a way no luminance measure sees.
    average = color.reshape(-1, 3).mean(axis=0)
    salience += np.linalg.norm(color - average, axis=2) * 0.6

    # Edge density: detail attracts, and a flat gradient does not.
    dy, dx = np.gradient(gray)
    salience += _blur(np.hypot(dx, dy), max(2, min(height, width) // 30)) * 1.2

    peak = float(salience.max()) or 1.0
    return salience / peak


def _hues(color: np.ndarray, weights: np.ndarray) -> tuple[float, float]:
    """Dominant hue and spread, in degrees, weighted by saturation.

    Hue is circular, so the average is taken as a vector — averaging 350° and
    10° arithmetically gives 180°, which is the opposite colour.
    """
    r, g, b = color[..., 0], color[..., 1], color[..., 2]
    high, low = color.max(axis=2), color.min(axis=2)
    delta = high - low
    saturation = np.where(high > 1e-6, delta / np.maximum(high, 1e-6), 0.0)

    hue = np.zeros_like(high)
    mask = delta > 1e-6
    with np.errstate(invalid="ignore"):
        rmax = mask & (high == r)
        gmax = mask & (high == g) & ~rmax
        bmax = mask & ~rmax & ~gmax
        hue[rmax] = ((g - b)[rmax] / delta[rmax]) % 6
        hue[gmax] = ((b - r)[gmax] / delta[gmax]) + 2
        hue[bmax] = ((r - g)[bmax] / delta[bmax]) + 4
    hue = hue * 60.0

    weight = saturation * weights
    total = float(weight.sum())
    if total < 1e-6:
        return 0.0, 0.0

    radians = np.deg2rad(hue)
    x = float((np.cos(radians) * weight).sum() / total)
    y = float((np.sin(radians) * weight).sum() / total)
    dominant = math.degrees(math.atan2(y, x)) % 360.0

    # Spread as circular deviation: 0 is one hue, 180 is hues opposite each other.
    resultant = math.hypot(x, y)
    spread = math.degrees(math.sqrt(max(0.0, -2.0 * math.log(max(resultant, 1e-6)))))
    return dominant, min(spread, 180.0)


def _palette_name(spread: float) -> str:
    if spread < 25:
        return "Monographic" if False else "Monochromatic"
    if spread < 60:
        return "Analogous"
    if spread < 110:
        return "Triadic"
    return "Split-Complementary"


def _lighting_name(gray: np.ndarray, luma: float, contrast: float, saturation: float) -> str:
    """Named from the *shape* of the histogram, not its average."""
    histogram, _ = np.histogram(gray, bins=16, range=(0.0, 1.0), density=True)
    dark = float(histogram[:5].sum())
    mid = float(histogram[5:11].sum())
    bright = float(histogram[11:].sum())

    # An empty middle with weight at both ends is the definition of chiaroscuro.
    if mid < (dark + bright) * 0.35 and contrast > 0.12:
        return "Chiaroscuro/Moody"
    if luma < 0.22:
        return "Cyberpunk Accent" if saturation > 0.28 else "Chiaroscuro/Moody"
    if luma > 0.62 and contrast < 0.20:
        return "High-Key Neon" if saturation > 0.30 else "Natural Flat"
    return "Natural Flat"


def _composition_name(focus: tuple[float, float], strength: float, busy: float, tilt: float) -> str:
    if tilt > 0.22:
        return "Dutch Angle"
    if busy > 0.55 and strength > 0.3:
        return "Extreme Close-Up"
    x, y = focus
    # Distance to the nearest third intersection.
    thirds = min(math.hypot(x - tx, y - ty) for tx in (1 / 3, 2 / 3) for ty in (1 / 3, 2 / 3))
    centre = math.hypot(x - 0.5, y - 0.5)
    if y > 0.62 and strength > 0.25:
        return "Low-Angle Hero"
    if thirds < centre and thirds < 0.16:
        return "Rule of Thirds"
    return "Center Framed"


def _tilt(gray: np.ndarray) -> float:
    """How far the dominant edges are off horizontal and vertical.

    Returns the *excess* of diagonal energy over axis-aligned energy, so 0 means
    "as diagonal as an ordinary picture" and high means genuinely tilted.

    The first version averaged sin(2θ) over the strong edges. For anything like
    a uniform spread of angles that averages to about 2/π ≈ 0.64 — comfortably
    over any sensible threshold — so every frame came back a Dutch angle. A
    classifier that always returns the same answer is not classifying; it is
    reporting its own bias with extra steps.
    """
    dy, dx = np.gradient(gray)
    magnitude = np.hypot(dx, dy)
    if magnitude.size < 64:
        return 0.0
    strong = magnitude > np.percentile(magnitude, 88)
    if strong.sum() < 32:
        return 0.0

    # Edge *orientation* is perpendicular to the gradient. Folded into
    # [0, 90) because an edge at 10° and one at 190° are the same edge.
    angles = (np.degrees(np.arctan2(dy[strong], dx[strong])) + 90.0) % 90.0
    weights = magnitude[strong]

    near_axis = (angles < 15.0) | (angles > 75.0)
    near_diagonal = (angles > 30.0) & (angles < 60.0)
    axis_energy = float(weights[near_axis].sum())
    diagonal_energy = float(weights[near_diagonal].sum())
    total = axis_energy + diagonal_energy
    if total < 1e-6:
        return 0.0
    # Both bands span 30 degrees, so an untilted picture with no preference
    # scores 0 and one built entirely on diagonals scores 1.
    return float(max(0.0, (diagonal_energy - axis_energy) / total))


def read_frame(frame: np.ndarray) -> Reading:
    """Read one RGB frame, float 0..1 or uint8, shape (h, w, 3)."""
    color = frame.astype(np.float32)
    if color.max() > 1.5:
        color = color / 255.0
    if color.ndim == 2:
        color = np.stack([color] * 3, axis=-1)
    gray = color.mean(axis=2)
    height, width = gray.shape

    salience = _saliency(gray, color)
    total = float(salience.sum()) or 1.0

    # The centroid of the *whole* salience field is the centre of the frame for
    # anything roughly symmetric, which is how five photographs with their
    # subjects in five different places all read "dead centre". So: smooth,
    # find the peak, and take the centroid of the bright region around it.
    #
    # A region rather than a window: a window gets clipped at the frame border,
    # so its centroid drifts inward exactly when the subject is near an edge —
    # the case that matters most for deciding where a title can go.
    smoothed = _blur(salience, max(3, min(height, width) // 12))
    peak_index = int(np.argmax(smoothed))
    peak_y, peak_x = divmod(peak_index, width)
    peak_value = float(smoothed[peak_y, peak_x]) or 1.0

    bright = smoothed >= peak_value * 0.7
    if bright.sum() >= 4:
        bys, bxs = np.nonzero(bright)
        weights = smoothed[bright]
        mass = float(weights.sum()) or 1.0
        focus = (
            float((bxs * weights).sum() / mass / width),
            float((bys * weights).sum() / mass / height),
        )
    else:
        focus = ((peak_x + 0.5) / width, (peak_y + 0.5) / height)

    # How concentrated is it? The share of salience inside the top decile of
    # pixels — a single subject packs it, a texture spreads it evenly.
    threshold = np.percentile(salience, 90)
    strength = float(salience[salience >= threshold].sum() / total)

    # Secondary focal points: the strongest salience peaks away from the first.
    secondary: list[tuple[float, float]] = []
    grid = 6
    cells = []
    for gy in range(grid):
        for gx in range(grid):
            block = salience[
                gy * height // grid : (gy + 1) * height // grid,
                gx * width // grid : (gx + 1) * width // grid,
            ]
            cells.append((float(block.mean()), (gx + 0.5) / grid, (gy + 0.5) / grid))
    cells.sort(key=lambda c: -c[0])
    for _, cx, cy in cells[:4]:
        if math.hypot(cx - focus[0], cy - focus[1]) > 0.2:
            secondary.append((cx, cy))

    saturation_map = (color.max(axis=2) - color.min(axis=2)) / np.maximum(color.max(axis=2), 1e-6)
    hue, spread = _hues(color, salience)

    # Depth: sharpness where the eye lands, against sharpness elsewhere. A lens
    # focuses at one distance, so this is a real depth cue rather than a proxy.
    dy, dx = np.gradient(gray)
    sharp = np.hypot(dx, dy)
    fx, fy = int(focus[0] * width), int(focus[1] * height)
    radius = max(4, min(height, width) // 6)
    near = sharp[max(0, fy - radius) : fy + radius, max(0, fx - radius) : fx + radius]
    far_mean = float(sharp.mean()) or 1e-6
    depth = float(np.clip((near.mean() / far_mean - 1.0), 0.0, 2.0) / 2.0) if near.size else 0.0

    left = float(salience[:, : width // 2].sum())
    right = float(salience[:, width // 2 :].sum())
    balance = (right - left) / max(left + right, 1e-6)

    # Against a fraction of the *peak*, not of a percentile. A percentile
    # threshold on a flat frame with one bright shape sits near zero, so nearly
    # every empty pixel cleared it and an almost-empty frame read as busy.
    busy = float((salience > float(salience.max()) * 0.35).mean())
    contrast = float(gray.std())
    luma = float(gray.mean())
    tilt = _tilt(gray)

    reading = Reading(
        focus=focus,
        focus_strength=strength,
        composition=_composition_name(focus, strength, busy, tilt),
        lighting=_lighting_name(gray, luma, contrast, float(saturation_map.mean())),
        palette=_palette_name(spread),
        hue=hue,
        hue_spread=spread,
        depth_separation=depth,
        balance=balance,
        busy=busy,
        contrast=contrast,
        luma=luma,
        secondary=tuple(secondary[:2]),
    )

    if not reading.has_subject:
        reading.notes.append(
            "no single subject — the frame reads as a field, so a title can go anywhere"
        )
    if depth > 0.35:
        reading.notes.append("a sharp subject against a soft ground: real depth to protect")
    if abs(balance) > 0.35:
        side = "right" if balance > 0 else "left"
        reading.notes.append(f"weight sits {side}; the other side is where text belongs")
    if luma < 0.12:
        reading.notes.append("very dark — a platform's compression will find this hard")
    return reading


def read_asset(path: str | Path, *, samples: int = 5) -> Reading:
    """Read a still, or a video by sampling across it.

    Sampling rather than averaging every frame: a reading is about structure,
    and structure that changes shot to shot should be reported as the dominant
    one rather than smeared into a mean of two different pictures.
    """
    from .. import ffmpeg as ff
    from ..ingest import probe_asset

    file = Path(path)
    asset = probe_asset(file)
    if asset is None:
        raise ValueError(f"{file.name} is not readable media")

    if asset.kind == "image":
        frames = [_load_image(file)]
    else:
        frames = []
        stream = ff.read_frames(file, fps=max(0.2, samples / max(asset.duration, 1.0)), width=320)
        for frame in stream:
            frames.append(frame)
            if len(frames) >= samples:
                break
    if not frames:
        raise ValueError(f"no frames could be read from {file.name}")

    readings = [read_frame(frame) for frame in frames]
    return _consensus(readings)


def _load_image(path: Path) -> np.ndarray:
    from PIL import Image, ImageOps

    with Image.open(path) as handle:
        # Honour the EXIF orientation: a phone photo analysed sideways gives a
        # confident reading of a composition nobody will ever see.
        image = ImageOps.exif_transpose(handle).convert("RGB")
        image.thumbnail((320, 320))
        return np.asarray(image, dtype=np.float32) / 255.0


def _consensus(readings: list[Reading]) -> Reading:
    """One reading from several: the most common name, the median number."""
    from collections import Counter

    def common(attribute: str) -> str:
        return Counter(getattr(r, attribute) for r in readings).most_common(1)[0][0]

    def median(attribute: str) -> float:
        values = sorted(getattr(r, attribute) for r in readings)
        return float(values[len(values) // 2])

    focus_x = sorted(r.focus[0] for r in readings)[len(readings) // 2]
    focus_y = sorted(r.focus[1] for r in readings)[len(readings) // 2]

    out = Reading(
        focus=(focus_x, focus_y),
        focus_strength=median("focus_strength"),
        composition=common("composition"),
        lighting=common("lighting"),
        palette=common("palette"),
        hue=median("hue"),
        hue_spread=median("hue_spread"),
        depth_separation=median("depth_separation"),
        balance=median("balance"),
        busy=median("busy"),
        contrast=median("contrast"),
        luma=median("luma"),
        secondary=readings[0].secondary,
    )
    seen: set[str] = set()
    for reading in readings:
        for note in reading.notes:
            if note not in seen:
                seen.add(note)
                out.notes.append(note)
    if len({r.composition for r in readings}) > 2:
        out.notes.append("the composition changes shot to shot; this is the most common one")
    return out
