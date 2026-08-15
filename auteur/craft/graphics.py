"""Drawn graphics: rings, arrows, brackets, bursts, bars, tape, stickers.

Type had to leave ffmpeg because drawtext cannot letter-space. Graphics leave
for a related reason: ffmpeg can composite a still and animate its position,
but it cannot draw a ring that draws itself, jitter a sticker on a rotation, or
overshoot a scale and settle. Those are the movements that read as *made* rather
than as a filter, and they are exactly the ones short-form uses to hold a viewer.

So each graphic is drawn here, one frame at a time, into a PNG sequence, and
ffmpeg only composites it. Two things keep that affordable:

  - the shape is drawn on a small layer at its natural size, transformed, and
    pasted — never a full-frame redraw per frame;
  - the sequence is cropped to a box computed from the geometry, so the plates
    are the size of the graphic rather than the size of the film, and the whole
    sequence enters ffmpeg as *one* input via the image demuxer.

Nothing here is fetched. Every mark is drawn from primitives, so the finished
film carries no asset licence — with one exception, which is the user's own
sticker files, and those are theirs.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from ..edl import GraphicCue
from .titles import _hex_to_rgb

log = logging.getLogger("auteur.craft.graphics")

#: Frames per second the drawn sequences are rendered at. Overlays are read
#: peripherally, so they do not need the film's rate; 15 is smooth enough that
#: a pop does not step, and half the plates of 30.
GRAPHIC_FPS = 15

#: Any single graphic longer than this is drawn as a still and animated only by
#: its fade. A four-second wiggle is 60 plates; a forty-second one is 600, which
#: is a lot of PNGs for a mark nobody is looking at by then.
MAX_ANIMATED = 6.0

#: File types accepted from the user's sticker folder.
STICKER_SUFFIXES = (".png", ".webp", ".gif")

#: The kinds that span two points rather than sitting on one.
SPANNING = {"arrow", "underline", "highlight"}


@dataclass
class Graphic:
    """A rendered graphic and where it goes.

    `pattern` is either a single PNG or a printf pattern for a sequence; `fps`
    of 0 means the former. `box` is the rectangle in frame pixels the plates
    occupy — the renderer overlays them at `box[0], box[1]` and does not have to
    know anything else about the drawing.
    """

    pattern: Path
    box: tuple[int, int, int, int]
    start: float
    end: float
    fps: float = 0.0
    frames: int = 1
    fade_in: float = 0.12
    fade_out: float = 0.18

    @property
    def duration(self) -> float:
        return max(0.05, self.end - self.start)

    @property
    def is_sequence(self) -> bool:
        return self.fps > 0 and self.frames > 1


# --------------------------------------------------------------------- stickers


def find_stickers(directory: str | Path | None) -> list[Path]:
    """Every transparent image the user dropped in a folder, in a stable order.

    Sorted by name rather than by mtime so a re-render of the same film picks
    the same stickers in the same places. Files without an alpha channel are
    kept — a sticker on a white square is a bad sticker, not a broken one, and
    refusing it silently would be worse than showing the user what they get.
    """
    if not directory:
        return []
    folder = Path(directory)
    if not folder.is_dir():
        return []
    found = [
        path
        for path in sorted(folder.iterdir())
        if path.is_file() and path.suffix.lower() in STICKER_SUFFIXES
    ]
    if not found:
        log.info("sticker folder %s has no %s files", folder, "/".join(STICKER_SUFFIXES))
    return found


# ------------------------------------------------------------------- movement


def _ease_out(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def _motion(move: str, progress: float) -> tuple[float, float, float, float, float]:
    """(scale, rotation°, dx, dy, alpha) at `progress` through the cue, 0..1.

    dx and dy are fractions of the graphic's own size, so the caller can turn
    them into pixels without knowing which movement it asked for.
    """
    p = min(1.0, max(0.0, progress))

    if move == "pop":
        # In over the first fifth, overshooting once. The overshoot is what
        # separates a graphic that arrived from one that was switched on.
        if p < 0.2:
            t = _ease_out(p / 0.2)
            return (0.55 + 0.55 * t, 0.0, 0.0, 0.0, min(1.0, t * 1.6))
        settle = min(1.0, (p - 0.2) / 0.12)
        return (1.10 - 0.10 * _ease_out(settle), 0.0, 0.0, 0.0, 1.0)

    if move == "pulse":
        return (1.0 + 0.055 * math.sin(p * math.tau * 3.0), 0.0, 0.0, 0.0, 1.0)

    if move == "drift":
        return (1.0, 0.0, 0.10 * p, -0.06 * p, 1.0)

    if move == "wiggle":
        angle = 7.0 * math.sin(p * math.tau * 2.5)
        return (1.0 + 0.03 * math.cos(p * math.tau * 2.5), angle, 0.0, 0.0, 1.0)

    if move in ("sweep", "draw"):
        # The reveal happens in the drawing, not the transform.
        return (1.0, 0.0, 0.0, 0.0, 1.0)

    return (1.0, 0.0, 0.0, 0.0, 1.0)


def _reveal(move: str, progress: float) -> float:
    """How much of a progressive shape is drawn yet, 0..1."""
    if move == "draw":
        return _ease_out(min(1.0, progress / 0.35))
    if move == "sweep":
        return _ease_out(min(1.0, progress / 0.3))
    return 1.0


def _animated(cue: GraphicCue) -> bool:
    if cue.kind == "progress":
        return True  # the whole point of it is that it fills
    if cue.move == "none":
        return False
    return cue.duration <= MAX_ANIMATED


# ------------------------------------------------------------------- geometry


def _points(cue: GraphicCue, width: int, height: int) -> tuple[tuple[float, float], ...]:
    """The cue's anchor, and its second point if the kind spans one, in pixels."""
    ax, ay = cue.anchor[0] * width, cue.anchor[1] * height
    if cue.kind not in SPANNING:
        return ((ax, ay),)
    if cue.toward is not None:
        return ((ax, ay), (cue.toward[0] * width, cue.toward[1] * height))
    # A default span: a short horizontal run, which is what an underline or a
    # highlight almost always wants, and a sensible arrow if nobody said where.
    run = min(width, height) * 0.3 * cue.size
    return ((ax - run / 2, ay), (ax + run / 2, ay))


def _span_pad(cue: GraphicCue, base: float) -> float:
    """Margin around a two-point shape.

    The layer for a spanning kind is the bounding box of its two points plus
    this on every side, so the drawing code has to inset by exactly the same
    amount or the arrow lands somewhere other than where it was aimed.
    """
    return base * (0.06 if cue.kind == "arrow" else 0.04) * cue.size


def _natural(cue: GraphicCue, width: int, height: int) -> tuple[int, int]:
    """The size of the layer the shape is drawn on, before any transform."""
    base = min(width, height)
    if cue.kind == "progress":
        return (width, max(4, int(base * 0.012 * cue.size)))
    if cue.kind in SPANNING:
        (x0, y0), (x1, y1) = _points(cue, width, height)
        pad = _span_pad(cue, base)
        return (
            max(8, int(abs(x1 - x0) + pad * 2)),
            max(8, int(abs(y1 - y0) + pad * 2)),
        )
    if cue.kind == "burst":
        d = base * 0.5 * cue.size
        return (int(d), int(d))
    if cue.kind == "tape":
        return (int(base * 0.34 * cue.size), int(base * 0.10 * cue.size))
    if cue.kind == "sticker":
        return _sticker_size(cue, base)
    # circle, bracket
    d = base * 0.44 * cue.size
    return (int(d), int(d * 0.86))


def _sticker_size(cue: GraphicCue, base: float) -> tuple[int, int]:
    target = base * 0.26 * cue.size
    try:
        with Image.open(cue.source) as image:  # type: ignore[arg-type]
            sw, sh = image.size
    except Exception:  # noqa: BLE001 - a missing sticker must not lose the film
        return (int(target), int(target))
    if sw <= 0 or sh <= 0:
        return (int(target), int(target))
    scale = target / max(sw, sh)
    return (max(8, int(sw * scale)), max(8, int(sh * scale)))


def _box(cue: GraphicCue, width: int, height: int) -> tuple[int, int, int, int]:
    """The rectangle the plates cover, in frame pixels.

    Generous on purpose: a pop overshoots to 1.10, a wiggle rotates, a drift
    travels. Anything that leaves the box is cropped, and a clipped corner on a
    graphic is far more noticeable than a few wasted pixels.
    """
    nw, nh = _natural(cue, width, height)
    growth = 1.30 if cue.move in ("pop", "pulse", "wiggle") else 1.02
    travel = 0.14 if cue.move == "drift" else 0.0
    bw = int(nw * (growth + travel)) + 8
    bh = int(nh * (growth + travel)) + 8

    if cue.kind == "progress":
        cx, cy = width / 2, cue.anchor[1] * height
        bw = width
    elif cue.kind in SPANNING:
        (x0, y0), (x1, y1) = _points(cue, width, height)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    else:
        cx, cy = cue.anchor[0] * width, cue.anchor[1] * height

    # Clamp the size before placing it, or a box wider than the film lands at a
    # negative x and then gets shrunk, which moves the graphic off its anchor.
    bw = min(bw, width)
    bh = min(bh, height)
    x = max(0, min(int(round(cx - bw / 2)), width - bw))
    y = max(0, min(int(round(cy - bh / 2)), height - bh))
    # ffmpeg's yuv420p path wants even dimensions for the overlay source.
    return (x - x % 2, y - y % 2, bw - bw % 2, bh - bh % 2)


# -------------------------------------------------------------------- drawing


def _stroke(cue: GraphicCue, base: float) -> int:
    return max(2, int(base * 0.010 * math.sqrt(cue.size)))


def _rgba(cue: GraphicCue, alpha: float = 1.0) -> tuple[int, int, int, int]:
    return (*_hex_to_rgb(cue.color), int(255 * max(0.0, min(1.0, alpha * cue.opacity))))


def _arc_points(w: float, h: float, span: float, wobble: float) -> list[tuple[float, float]]:
    """Points along an ellipse, radius-jittered so the ring looks hand-drawn.

    A perfect ellipse reads as a UI element and viewers skip it; a ring with a
    little tremble in it reads as somebody marking up the frame, which is the
    thing that actually holds attention for the half-second it needs.
    """
    steps = max(24, int(96 * span))
    cx, cy = w / 2, h / 2
    rx, ry = w / 2 * 0.92, h / 2 * 0.92
    out: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = (i / steps) * span * math.tau - math.pi * 0.35
        jitter = 1.0 + wobble * math.sin(t * 3.1 + 0.7) * 0.5
        out.append((cx + math.cos(t) * rx * jitter, cy + math.sin(t) * ry * jitter))
    return out


def _draw_circle(layer: Image.Image, cue: GraphicCue, reveal: float, base: float) -> None:
    draw = ImageDraw.Draw(layer)
    w, h = layer.size
    width_px = _stroke(cue, base)
    points = _arc_points(w, h, max(0.03, reveal * 1.06), wobble=0.035)
    draw.line(points, fill=_rgba(cue), width=width_px, joint="curve")


def _draw_bracket(layer: Image.Image, cue: GraphicCue, reveal: float, base: float) -> None:
    """Viewfinder corners. Says "look here" without covering anything."""
    draw = ImageDraw.Draw(layer)
    w, h = layer.size
    width_px = _stroke(cue, base)
    arm_x, arm_y = w * 0.26 * reveal, h * 0.26 * reveal
    inset = width_px
    fill = _rgba(cue)
    corners = [
        ((inset, inset), (inset + arm_x, inset), (inset, inset + arm_y)),
        ((w - inset, inset), (w - inset - arm_x, inset), (w - inset, inset + arm_y)),
        ((inset, h - inset), (inset + arm_x, h - inset), (inset, h - inset - arm_y)),
        ((w - inset, h - inset), (w - inset - arm_x, h - inset), (w - inset, h - inset - arm_y)),
    ]
    for corner, across, down in corners:
        draw.line([corner, across], fill=fill, width=width_px)
        draw.line([corner, down], fill=fill, width=width_px)


def _draw_arrow(layer: Image.Image, cue: GraphicCue, reveal: float, base: float) -> None:
    draw = ImageDraw.Draw(layer)
    w, h = layer.size
    pad = _span_pad(cue, base)
    # The layer is the bbox of the two points; work out which corners they are.
    (x0, y0), (x1, y1) = _points(cue, 1000, 1000)  # direction only
    tail = (pad if x1 >= x0 else w - pad, pad if y1 >= y0 else h - pad)
    head_full = (w - pad if x1 >= x0 else pad, h - pad if y1 >= y0 else pad)
    head = (
        tail[0] + (head_full[0] - tail[0]) * reveal,
        tail[1] + (head_full[1] - tail[1]) * reveal,
    )
    width_px = _stroke(cue, base)
    fill = _rgba(cue)

    # A gentle bow in the shaft. A ruler-straight arrow reads as a diagram.
    mx, my = (tail[0] + head[0]) / 2, (tail[1] + head[1]) / 2
    dx, dy = head[0] - tail[0], head[1] - tail[1]
    length = math.hypot(dx, dy) or 1.0
    bow = length * 0.10
    control = (mx - dy / length * bow, my + dx / length * bow)
    curve = [
        (
            (1 - t) ** 2 * tail[0] + 2 * (1 - t) * t * control[0] + t**2 * head[0],
            (1 - t) ** 2 * tail[1] + 2 * (1 - t) * t * control[1] + t**2 * head[1],
        )
        for t in [i / 24 for i in range(25)]
    ]
    draw.line(curve, fill=fill, width=width_px, joint="curve")

    if reveal > 0.55:
        # Head angle from the tangent at the end of the curve, not from the
        # straight line — otherwise the head sits crooked on the bowed shaft.
        ax, ay = curve[-1][0] - curve[-3][0], curve[-1][1] - curve[-3][1]
        angle = math.atan2(ay, ax)
        size = width_px * 4.5
        for spread in (2.6, -2.6):
            draw.line(
                [
                    head,
                    (
                        head[0] + math.cos(angle + spread) * size,
                        head[1] + math.sin(angle + spread) * size,
                    ),
                ],
                fill=fill,
                width=width_px,
            )


def _draw_underline(layer: Image.Image, cue: GraphicCue, reveal: float, base: float) -> None:
    draw = ImageDraw.Draw(layer)
    w, h = layer.size
    width_px = _stroke(cue, base)
    pad = _span_pad(cue, base)
    y = h / 2
    # A slight rise across the run, like a pen stroke that lifted.
    draw.line(
        [(pad, y + width_px * 0.4), (pad + (w - pad * 2) * reveal, y - width_px * 0.3)],
        fill=_rgba(cue),
        width=width_px,
        joint="curve",
    )


def _draw_highlight(layer: Image.Image, cue: GraphicCue, reveal: float, base: float) -> None:
    """A marker swipe. Sits *behind* type, so it is deliberately soft-edged."""
    draw = ImageDraw.Draw(layer)
    w, h = layer.size
    band = h * 0.62
    pad = _span_pad(cue, base) * 0.4
    draw.rounded_rectangle(
        [pad, (h - band) / 2, max(pad + 2.0, pad + (w - pad * 2) * reveal), (h + band) / 2],
        radius=band * 0.18,
        fill=_rgba(cue, 0.55),
    )


def _draw_burst(layer: Image.Image, cue: GraphicCue, reveal: float, base: float) -> None:
    """Radiating lines. One hit, on an impact — it should not linger."""
    draw = ImageDraw.Draw(layer)
    w, h = layer.size
    cx, cy = w / 2, h / 2
    width_px = max(2, _stroke(cue, base) // 2)
    inner = min(w, h) * (0.16 + 0.24 * reveal)
    outer = min(w, h) * (0.22 + 0.28 * reveal)
    fade = 1.0 - reveal * 0.85  # burns out as it expands
    for spoke in range(12):
        angle = spoke / 12 * math.tau + 0.13
        long_one = spoke % 2 == 0
        end = outer * (1.0 if long_one else 0.72)
        draw.line(
            [
                (cx + math.cos(angle) * inner, cy + math.sin(angle) * inner),
                (cx + math.cos(angle) * end, cy + math.sin(angle) * end),
            ],
            fill=_rgba(cue, fade),
            width=width_px,
        )


def _draw_progress(layer: Image.Image, cue: GraphicCue, reveal: float, base: float) -> None:
    """The bar that tells a viewer the end is reachable.

    `reveal` is ignored: this one fills with the cue's own clock, which is
    handled by the caller passing progress straight through.
    """
    draw = ImageDraw.Draw(layer)
    w, h = layer.size
    radius = h / 2
    draw.rounded_rectangle([0, 0, w, h], radius=radius, fill=(*_hex_to_rgb(cue.color), 55))
    filled = max(h, w * reveal)
    draw.rounded_rectangle([0, 0, filled, h], radius=radius, fill=_rgba(cue))


def _draw_tape(layer: Image.Image, cue: GraphicCue, reveal: float, base: float) -> None:
    """A torn strip. Decorative — it gives type somewhere to sit."""
    draw = ImageDraw.Draw(layer)
    w, h = layer.size
    right = max(8.0, w * reveal)
    teeth = 9
    # Proportional to the strip, not a fixed pixel count — a 4px notch on a
    # 150px strip is not a torn edge, it is a rounding error.
    bite = h * 0.13
    top = [(x / teeth * right, bite * (0.9 if x % 2 else 0.1)) for x in range(teeth + 1)]
    bottom = [
        (x / teeth * right, h - bite * (0.15 if x % 2 else 0.95)) for x in range(teeth, -1, -1)
    ]
    draw.polygon(top + bottom, fill=_rgba(cue, 0.82))


DRAWERS = {
    "circle": _draw_circle,
    "bracket": _draw_bracket,
    "arrow": _draw_arrow,
    "underline": _draw_underline,
    "highlight": _draw_highlight,
    "burst": _draw_burst,
    "progress": _draw_progress,
    "tape": _draw_tape,
}


#: Kinds that get a soft dark halo behind them.
HALOED = {"circle", "arrow", "bracket", "underline"}


def _shape_layer(cue: GraphicCue, size: tuple[int, int], reveal: float, base: float) -> Image.Image:
    """The graphic at its natural size, on transparent, before any transform."""
    if cue.kind == "sticker":
        return _sticker_layer(cue, (max(2, size[0]), max(2, size[1])))

    layer = Image.new("RGBA", (max(2, size[0]), max(2, size[1])), (0, 0, 0, 0))
    DRAWERS.get(cue.kind, _draw_circle)(layer, cue, reveal, base)
    if cue.kind not in HALOED:
        return layer

    # A soft dark halo under the mark, so a white ring survives a white sky.
    # Cheap, and the difference between legible and invisible. The layer is
    # padded first because the blur spreads outward and a tight canvas would
    # clip the halo off exactly where the stroke meets the edge; padding is
    # harmless because the caller places the layer by its centre.
    blur = max(1.5, base * 0.005)
    pad = int(blur * 3)
    padded = Image.new("RGBA", (layer.width + pad * 2, layer.height + pad * 2), (0, 0, 0, 0))
    padded.alpha_composite(layer, (pad, pad))
    halo = Image.new("RGBA", padded.size, (0, 0, 0, 0))
    spread = padded.split()[3].filter(ImageFilter.GaussianBlur(blur))
    halo.putalpha(spread.point(lambda v: int(v * 0.7)))
    return Image.alpha_composite(halo, padded)


def _sticker_layer(cue: GraphicCue, size: tuple[int, int]) -> Image.Image:
    try:
        with Image.open(cue.source) as image:  # type: ignore[arg-type]
            sticker = image.convert("RGBA").resize(size, Image.LANCZOS)
    except Exception as exc:  # noqa: BLE001 - a bad sticker must not lose the film
        log.warning("could not read sticker %s: %s", cue.source, exc)
        return Image.new("RGBA", size, (0, 0, 0, 0))
    if cue.opacity < 0.999:
        alpha = sticker.split()[3].point(lambda v: int(v * cue.opacity))
        sticker.putalpha(alpha)
    return sticker


def _transform(layer: Image.Image, scale: float, rotation: float, alpha: float) -> Image.Image:
    if abs(rotation) > 0.05:
        layer = layer.rotate(rotation, resample=Image.BICUBIC, expand=True)
    if abs(scale - 1.0) > 0.005:
        new = (max(2, int(layer.width * scale)), max(2, int(layer.height * scale)))
        layer = layer.resize(new, Image.LANCZOS)
    if alpha < 0.999:
        layer.putalpha(layer.split()[3].point(lambda v: int(v * alpha)))
    return layer


# --------------------------------------------------------------------- render


def render_cue(
    cue: GraphicCue,
    *,
    width: int,
    height: int,
    directory: Path,
    index: int,
    prefix: str = "",
) -> Graphic | None:
    """Draw one cue to a still or a PNG sequence, and say where it goes."""
    directory.mkdir(parents=True, exist_ok=True)
    base = float(min(width, height))
    stem = f"gfx-{prefix}-{index:02d}" if prefix else f"gfx-{index:02d}"

    box = _box(cue, width, height)
    if box[2] < 2 or box[3] < 2:
        return None
    natural = _natural(cue, width, height)
    # Where the shape's own centre sits inside the box.
    if cue.kind == "progress":
        centre = (width / 2 - box[0], cue.anchor[1] * height - box[1])
    elif cue.kind in SPANNING:
        (x0, y0), (x1, y1) = _points(cue, width, height)
        centre = ((x0 + x1) / 2 - box[0], (y0 + y1) / 2 - box[1])
    else:
        centre = (cue.anchor[0] * width - box[0], cue.anchor[1] * height - box[1])

    def plate(progress: float) -> Image.Image:
        canvas = Image.new("RGBA", (box[2], box[3]), (0, 0, 0, 0))
        reveal = progress if cue.kind == "progress" else _reveal(cue.move, progress)
        scale, rotation, dx, dy, alpha = _motion(cue.move, progress)
        layer = _transform(_shape_layer(cue, natural, reveal, base), scale, rotation, alpha)
        canvas.alpha_composite(
            layer,
            (
                int(centre[0] - layer.width / 2 + dx * natural[0]),
                int(centre[1] - layer.height / 2 + dy * natural[1]),
            ),
        )
        return canvas

    fade = min(0.22, cue.duration * 0.25)
    if not _animated(cue):
        path = directory / f"{stem}.png"
        plate(1.0).save(path)
        return Graphic(
            pattern=path,
            box=box,
            start=round(cue.start, 3),
            end=round(cue.end, 3),
            fade_in=fade,
            fade_out=fade,
        )

    count = max(2, int(round(cue.duration * GRAPHIC_FPS)))
    for frame in range(count):
        plate(frame / max(1, count - 1)).save(directory / f"{stem}-{frame:04d}.png")
    return Graphic(
        pattern=directory / f"{stem}-%04d.png",
        box=box,
        start=round(cue.start, 3),
        end=round(cue.end, 3),
        fps=GRAPHIC_FPS,
        frames=count,
        fade_in=fade,
        fade_out=fade,
    )


def render_all(
    cues: list[GraphicCue], *, width: int, height: int, directory: Path, prefix: str = ""
) -> list[Graphic]:
    out: list[Graphic] = []
    for index, cue in enumerate(cues):
        try:
            drawn = render_cue(
                cue, width=width, height=height, directory=directory, index=index, prefix=prefix
            )
        except Exception as exc:  # noqa: BLE001 - one bad graphic must not lose the film
            log.warning("could not render %s graphic: %s", cue.kind, exc)
            continue
        if drawn is not None:
            out.append(drawn)
    return out
