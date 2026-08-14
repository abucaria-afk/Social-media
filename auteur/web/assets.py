"""The app's icons, drawn rather than shipped.

An iPhone home-screen web app needs real PNG icons — Safari will not use an
SVG or an emoji. Checking binaries into the repository for something this
simple is worse than drawing it at startup, so the icons are generated once
into the static folder and left alone afterwards.

If Pillow is missing the app still runs; it just looks generic on the home
screen, which is not worth failing a render over.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("auteur.web.assets")

#: Sizes Safari and Android actually ask for.
#: 180 is the iPhone home-screen touch icon; 192 and 512 are the manifest.
SIZES = (180, 192, 512)

INK = (14, 14, 18)
AMBER = (255, 176, 74)
PAPER = (247, 244, 238)


def _mix(over: tuple[int, int, int], under: tuple[int, int, int], amount: float) -> tuple[int, ...]:
    """Blend two colours by hand.

    ImageDraw *replaces* pixels rather than compositing them, so passing a
    low alpha does not tint the shape underneath — it punches a translucent
    hole in the icon, which on a white home screen reads as solid white.
    """
    return tuple(round(u + (o - u) * amount) for o, u in zip(over, under)) + (255,)


def _draw(size: int):
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    unit = size / 100.0

    # The ground: a rounded square, because iOS masks square icons anyway and
    # Android does not — drawing the radius ourselves looks right on both.
    draw.rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(22 * unit), fill=INK + (255,)
    )

    # A film frame: two perforated edges with an aperture between them.
    perforation = _mix(PAPER, INK, 0.34)
    hole_w, hole_h = 7 * unit, 5 * unit
    for row in range(4):
        top = (20 + row * 16) * unit
        for left in (9 * unit, size - 9 * unit - hole_w):
            draw.rounded_rectangle(
                [left, top, left + hole_w, top + hole_h],
                radius=int(1.5 * unit), fill=perforation,
            )

    draw.rounded_rectangle(
        [24 * unit, 22 * unit, size - 24 * unit, size - 22 * unit],
        radius=int(4 * unit),
        fill=_mix(PAPER, INK, 0.09),
        outline=_mix(PAPER, INK, 0.30),
        width=max(1, int(unit)),
    )

    # The cut: one clean amber stroke straight through the frame. The whole
    # program is about where to put this line, so it is the whole icon.
    draw.line(
        [(34 * unit, size - 26 * unit), (size - 34 * unit, 26 * unit)],
        fill=AMBER + (255,), width=int(7 * unit),
    )
    draw.ellipse(
        [size / 2 - 6 * unit, size / 2 - 6 * unit, size / 2 + 6 * unit, size / 2 + 6 * unit],
        fill=AMBER + (255,),
    )
    return image


def ensure(static: Path) -> None:
    """Draw any icon the static folder is missing. Safe to call every start."""
    static = Path(static)
    static.mkdir(parents=True, exist_ok=True)

    wanted = {static / f"icon-{size}.png": size for size in SIZES}
    wanted[static / "icon.png"] = 512
    missing = {path: size for path, size in wanted.items() if not path.is_file()}
    if not missing:
        return

    try:
        from PIL import Image  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - an icon is not worth a crash
        log.warning("no Pillow, so the home-screen icon will be generic (%s)", exc)
        return

    for path, size in missing.items():
        try:
            _draw(size).save(path, "PNG")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not draw %s (%s)", path.name, exc)
