"""Typography: words on screen, rendered properly.

ffmpeg's drawtext cannot letter-space, cannot wrap, cannot draw a soft shadow
and cannot reveal a line one word at a time. So type is rendered here with
Pillow into full-frame RGBA plates, and ffmpeg only composites and animates
them. That buys real typographic control — tracking, optical margins, layered
shadows — and it makes kinetic captions (one word lighting up at a time, the
single most recognisable device in modern short-form) straightforward.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ..edl import TextCue

log = logging.getLogger("auteur.craft.titles")

#: Preference order per role. First readable file wins.
FONT_CANDIDATES: dict[str, tuple[str, ...]] = {
    "display": (
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ),
    "serif": (
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
    ),
    "mono": (
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    ),
}

STYLE_FONT = {
    "title": "display",
    "kinetic": "display",
    "caption": "display",
    "lower-third": "display",
    "end-card": "serif",
    "chapter": "mono",
}


@dataclass
class TextOverlay:
    """One rendered plate plus the animation that brings it on and off."""

    path: Path
    start: float
    end: float
    fade_in: float = 0.25
    fade_out: float = 0.3
    #: Vertical travel in pixels as the plate settles into place.
    rise: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.05, self.end - self.start)


def _load_font(role: str, size: int) -> ImageFont.FreeTypeFont:
    for candidate in FONT_CANDIDATES.get(role, ()):
        try:
            return ImageFont.truetype(candidate, size)
        except (OSError, ValueError):
            continue
    for group in FONT_CANDIDATES.values():
        for candidate in group:
            try:
                return ImageFont.truetype(candidate, size)
            except (OSError, ValueError):
                continue
    log.warning("no TrueType font found; falling back to the bitmap default")
    return ImageFont.load_default()


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    text = (value or "#FFFFFF").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    try:
        return tuple(int(text[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (255, 255, 255)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, tracking: int) -> int:
    if not text:
        return 0
    width = sum(draw.textlength(char, font=font) for char in text)
    return int(width + tracking * max(len(text) - 1, 0))


def _draw_tracked(
    draw: ImageDraw.ImageDraw,
    position: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int, int],
    tracking: int,
) -> None:
    """Draw a string with manual letter spacing, which Pillow does not offer."""
    x, y = position
    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        x += draw.textlength(char, font=font) + tracking


def _wrap(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, tracking: int, max_width: int
) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if _text_width(draw, candidate, font, tracking) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines[:4]


def _plate(width: int, height: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    return image, ImageDraw.Draw(image)


def _with_shadow(image: Image.Image, blur: float = 12.0, opacity: int = 150) -> Image.Image:
    """Drop a soft dark shadow behind the type so it survives a bright frame."""
    alpha = image.getchannel("A")
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow.putalpha(alpha.point(lambda value: int(value * opacity / 255)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    shadow.alpha_composite(image)
    return shadow


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------

def _render_title(cue: TextCue, width: int, height: int) -> Image.Image:
    """Big, tracked, centred. The card that opens the film."""
    base = min(width, height)
    size = max(18, int(base * 0.088 * cue.size))
    font = _load_font(STYLE_FONT["title"], size)
    tracking = max(1, int(size * 0.06))

    image, draw = _plate(width, height)
    text = cue.text.upper()
    lines = _wrap(draw, text, font, tracking, int(width * 0.84))

    line_height = int(size * 1.22)
    block_height = line_height * len(lines)
    top = cue.anchor[1] * height - block_height / 2
    fill = (*_hex_to_rgb(cue.color), 255)

    for index, line in enumerate(lines):
        line_width = _text_width(draw, line, font, tracking)
        x = cue.anchor[0] * width - line_width / 2
        _draw_tracked(draw, (x, top + index * line_height), line, font, fill, tracking)

    # A hairline under the block, the width of the type. Reads as designed.
    if lines:
        rule_width = min(int(_text_width(draw, max(lines, key=len), font, tracking) * 0.5), int(width * 0.4))
        rule_y = int(top + block_height + size * 0.34)
        accent = (*_hex_to_rgb(cue.accent), 210)
        draw.rectangle(
            [cue.anchor[0] * width - rule_width / 2, rule_y,
             cue.anchor[0] * width + rule_width / 2, rule_y + max(2, int(size * 0.035))],
            fill=accent,
        )

    return _with_shadow(image, blur=size * 0.18, opacity=165)


def _render_kinetic(cue: TextCue, width: int, height: int, highlight: int | None) -> Image.Image:
    """Caption line with one word lit. `highlight` indexes the live word."""
    base = min(width, height)
    size = max(16, int(base * 0.062 * cue.size))
    font = _load_font(STYLE_FONT["kinetic"], size)
    tracking = max(0, int(size * 0.015))

    image, draw = _plate(width, height)
    words = cue.text.upper().split()
    if not words:
        return image

    space = draw.textlength(" ", font=font) + tracking
    # Lay the words out into lines by hand so a highlight box can be positioned.
    lines: list[list[str]] = [[]]
    widths: list[float] = [0.0]
    max_width = width * 0.86
    for word in words:
        word_width = _text_width(draw, word, font, tracking)
        prospective = widths[-1] + (space if lines[-1] else 0) + word_width
        if lines[-1] and prospective > max_width:
            lines.append([word])
            widths.append(word_width)
        else:
            lines[-1].append(word)
            widths[-1] = prospective

    line_height = int(size * 1.3)
    block_height = line_height * len(lines)
    top = cue.anchor[1] * height - block_height / 2

    dim = (*_hex_to_rgb(cue.color), 236)
    accent_rgb = _hex_to_rgb(cue.accent)
    counter = 0

    for line_index, line_words in enumerate(lines):
        x = cue.anchor[0] * width - widths[line_index] / 2
        y = top + line_index * line_height
        for word in line_words:
            word_width = _text_width(draw, word, font, tracking)
            if highlight is not None and counter == highlight:
                pad_x, pad_y = size * 0.16, size * 0.1
                draw.rounded_rectangle(
                    [x - pad_x, y - pad_y * 0.4, x + word_width + pad_x, y + size * 1.12 + pad_y * 0.2],
                    radius=int(size * 0.16),
                    fill=(*accent_rgb, 235),
                )
                # Knock the word out of the highlight box.
                luminance = sum(accent_rgb) / 3
                ink = (17, 17, 20, 255) if luminance > 128 else (255, 255, 255, 255)
                _draw_tracked(draw, (x, y), word, font, ink, tracking)
            else:
                _draw_tracked(draw, (x, y), word, font, dim, tracking)
            x += word_width + space
            counter += 1

    return _with_shadow(image, blur=size * 0.14, opacity=175)


def _render_lower_third(cue: TextCue, width: int, height: int) -> Image.Image:
    base = min(width, height)
    size = max(14, int(base * 0.05 * cue.size))
    font = _load_font(STYLE_FONT["lower-third"], size)
    tracking = max(1, int(size * 0.03))

    image, draw = _plate(width, height)
    lines = _wrap(draw, cue.text, font, tracking, int(width * 0.7))
    line_height = int(size * 1.24)

    left = width * 0.08
    top = cue.anchor[1] * height - (line_height * len(lines)) / 2
    bar_width = max(3, int(size * 0.12))

    draw.rectangle(
        [left - size * 0.42, top - size * 0.16, left - size * 0.42 + bar_width,
         top + line_height * len(lines) + size * 0.06],
        fill=(*_hex_to_rgb(cue.accent), 245),
    )
    for index, line in enumerate(lines):
        _draw_tracked(draw, (left, top + index * line_height), line, font,
                      (*_hex_to_rgb(cue.color), 245), tracking)

    return _with_shadow(image, blur=size * 0.2, opacity=185)


def _render_caption(cue: TextCue, width: int, height: int) -> Image.Image:
    """Subtitle-weight type on a dark plate, for when legibility beats style."""
    base = min(width, height)
    size = max(13, int(base * 0.042 * cue.size))
    font = _load_font(STYLE_FONT["caption"], size)
    tracking = 0

    image, draw = _plate(width, height)
    lines = _wrap(draw, cue.text, font, tracking, int(width * 0.82))
    line_height = int(size * 1.3)
    block_height = line_height * len(lines)
    top = cue.anchor[1] * height - block_height / 2

    widest = max((_text_width(draw, line, font, tracking) for line in lines), default=0)
    pad = size * 0.45
    draw.rounded_rectangle(
        [cue.anchor[0] * width - widest / 2 - pad, top - pad * 0.5,
         cue.anchor[0] * width + widest / 2 + pad, top + block_height + pad * 0.4],
        radius=int(size * 0.28), fill=(0, 0, 0, 150),
    )
    for index, line in enumerate(lines):
        line_width = _text_width(draw, line, font, tracking)
        _draw_tracked(draw, (cue.anchor[0] * width - line_width / 2, top + index * line_height),
                      line, font, (*_hex_to_rgb(cue.color), 255), tracking)
    return image


def _render_end_card(cue: TextCue, width: int, height: int) -> Image.Image:
    base = min(width, height)
    size = max(16, int(base * 0.07 * cue.size))
    font = _load_font(STYLE_FONT["end-card"], size)
    tracking = max(2, int(size * 0.11))

    image, draw = _plate(width, height)
    lines = _wrap(draw, cue.text.upper(), font, tracking, int(width * 0.78))
    line_height = int(size * 1.3)
    block_height = line_height * len(lines)
    top = cue.anchor[1] * height - block_height / 2
    fill = (*_hex_to_rgb(cue.color), 255)
    accent = (*_hex_to_rgb(cue.accent), 190)

    rule_width = int(width * 0.3)
    thickness = max(1, int(size * 0.02))
    for offset in (top - size * 0.62, top + block_height + size * 0.42):
        draw.rectangle(
            [width / 2 - rule_width / 2, offset, width / 2 + rule_width / 2, offset + thickness],
            fill=accent,
        )

    for index, line in enumerate(lines):
        line_width = _text_width(draw, line, font, tracking)
        _draw_tracked(draw, (width / 2 - line_width / 2, top + index * line_height), line, font, fill, tracking)

    return _with_shadow(image, blur=size * 0.16, opacity=150)


def _render_chapter(cue: TextCue, width: int, height: int) -> Image.Image:
    base = min(width, height)
    size = max(12, int(base * 0.036 * cue.size))
    font = _load_font(STYLE_FONT["chapter"], size)
    tracking = max(2, int(size * 0.2))

    image, draw = _plate(width, height)
    text = cue.text.upper()
    line_width = _text_width(draw, text, font, tracking)
    x = cue.anchor[0] * width - line_width / 2
    y = cue.anchor[1] * height - size / 2
    _draw_tracked(draw, (x, y), text, font, (*_hex_to_rgb(cue.color), 245), tracking)
    return _with_shadow(image, blur=size * 0.3, opacity=190)


# ---------------------------------------------------------------------------

def render_cue(cue: TextCue, *, width: int, height: int, directory: Path, index: int,
               prefix: str = "") -> list[TextOverlay]:
    """Render a cue to one or more plates, with their timings.

    Kinetic captions produce one plate per word; every other style produces one.
    """
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"text-{prefix}-" if prefix else "text-"
    style = cue.style

    if style == "kinetic" and cue.per_word:
        words = cue.text.split()
        if len(words) > 1:
            overlays: list[TextOverlay] = []
            step = cue.duration / len(words)
            for word_index in range(len(words)):
                image = _render_kinetic(cue, width, height, highlight=word_index)
                path = directory / f"{stem}{index:02d}-w{word_index:02d}.png"
                image.save(path)
                overlays.append(
                    TextOverlay(
                        path=path,
                        start=round(cue.start + word_index * step, 3),
                        end=round(cue.start + (word_index + 1) * step, 3),
                        # Only the first and last plates animate; the middle ones
                        # cut, or the line flickers as each word arrives.
                        fade_in=0.14 if word_index == 0 else 0.0,
                        fade_out=0.2 if word_index == len(words) - 1 else 0.0,
                        rise=height * 0.012 if word_index == 0 else 0.0,
                    )
                )
            return overlays

    renderers = {
        "title": _render_title,
        "kinetic": lambda c, w, h: _render_kinetic(c, w, h, highlight=None),
        "lower-third": _render_lower_third,
        "caption": _render_caption,
        "end-card": _render_end_card,
        "chapter": _render_chapter,
    }
    image = renderers.get(style, _render_title)(cue, width, height)

    path = directory / f"{stem}{index:02d}.png"
    image.save(path)

    fade = min(0.32, cue.duration * 0.28)
    return [
        TextOverlay(
            path=path,
            start=round(cue.start, 3),
            end=round(cue.end, 3),
            fade_in=fade,
            fade_out=fade,
            rise=height * (0.018 if style in ("title", "end-card") else 0.01),
        )
    ]


def render_all(cues: list[TextCue], *, width: int, height: int, directory: Path,
               prefix: str = "") -> list[TextOverlay]:
    overlays: list[TextOverlay] = []
    for index, cue in enumerate(cues):
        try:
            overlays.extend(render_cue(cue, width=width, height=height,
                                       directory=directory, index=index, prefix=prefix))
        except Exception as exc:  # noqa: BLE001 - a bad title must not lose the film
            log.warning("could not render text %r: %s", cue.text[:32], exc)
    return overlays
