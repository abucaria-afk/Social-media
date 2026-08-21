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

from .. import theme

log = logging.getLogger("auteur.web.assets")

#: Sizes Safari and Chrome actually ask for. 180 is the iPhone home-screen
#: touch icon; 192 and 512 are the manifest, and Chrome uses 512 for the
#: install dialog and the splash screen.
SIZES = (180, 192, 512)

INK = theme.rgb_of("ground")
AMBER = theme.rgb_of("ember")
PAPER = theme.rgb_of("text")


def _mix(over: tuple[int, int, int], under: tuple[int, int, int], amount: float) -> tuple[int, ...]:
    """Blend two colours by hand.

    ImageDraw *replaces* pixels rather than compositing them, so passing a
    low alpha does not tint the shape underneath — it punches a translucent
    hole in the icon, which on a white home screen reads as solid white.
    """
    return tuple(round(u + (o - u) * amount) for o, u in zip(over, under, strict=True)) + (255,)


def _draw(size: int):
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    unit = size / 100.0

    # The ground: a rounded square, because iOS masks square icons anyway and
    # Android does not — drawing the radius ourselves looks right on both.
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(22 * unit), fill=INK + (255,))

    # A film frame: two perforated edges with an aperture between them.
    perforation = _mix(PAPER, INK, 0.34)
    hole_w, hole_h = 7 * unit, 5 * unit
    for row in range(4):
        top = (20 + row * 16) * unit
        for left in (9 * unit, size - 9 * unit - hole_w):
            draw.rounded_rectangle(
                [left, top, left + hole_w, top + hole_h],
                radius=int(1.5 * unit),
                fill=perforation,
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
        fill=AMBER + (255,),
        width=int(7 * unit),
    )
    draw.ellipse(
        [size / 2 - 6 * unit, size / 2 - 6 * unit, size / 2 + 6 * unit, size / 2 + 6 * unit],
        fill=AMBER + (255,),
    )
    return image


#: The smallest markdown this project needs, which is exactly what PRIVACY.md
#: uses. Not a general converter: a privacy policy rendered by a parser nobody
#: reads is a policy that can silently stop saying what the file says.
def _as_html(markdown: str) -> str:
    import html as escaping
    import re as patterns

    def inline(text: str) -> str:
        text = escaping.escape(text)
        text = patterns.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        return patterns.sub(r"`(.+?)`", r"<code>\1</code>", text)

    out: list[str] = []
    rows: list[str] = []

    def close_table() -> None:
        if not rows:
            return
        head, *body = [r for r in rows if not set(r.replace("|", "").strip()) <= {"-", " "}]
        cells = [c.strip() for c in head.strip("|").split("|")]
        out.append("<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in cells))
        out.append("</tr></thead><tbody>")
        for row in body:
            cells = [c.strip() for c in row.strip("|").split("|")]
            out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>")
        out.append("</tbody></table>")
        rows.clear()

    para: list[str] = []

    def close_para() -> None:
        # A blank line ends a paragraph; a newline does not. Treating every
        # source line as its own <p> is what the first version did, and a
        # policy hard-wrapped at 78 characters came out as a column of
        # three-line fragments with gaps between them.
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    for line in markdown.splitlines():
        if line.startswith("|"):
            close_para()
            rows.append(line)
            continue
        close_table()
        if line.startswith("## "):
            close_para()
            out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("# "):
            close_para()
            out.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.strip():
            para.append(line.strip())
        else:
            close_para()
    close_para()
    close_table()
    return "\n".join(out)


def privacy_page(source: Path, static: Path) -> Path | None:
    """PRIVACY.md as a page anybody can open, generated rather than kept twice.

    The App Store requires a public privacy policy URL, and a policy that is
    maintained in two places is a policy that is wrong in one of them.
    """
    if not source.is_file():
        return None
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<meta name="robots" content="index">
<link rel="stylesheet" href="/static/theme.css">
<link rel="stylesheet" href="/static/style.css">
<link rel="stylesheet" href="/static/prose.css">
<title>Auteur — privacy</title>
</head>
<body>
<main class="prose">
{_as_html(source.read_text(encoding="utf-8"))}
</main>
</body>
</html>
"""
    out = Path(static) / "privacy.html"
    if not out.is_file() or out.read_text(encoding="utf-8") != page:
        out.write_text(page, encoding="utf-8")
    return out


def ensure(static: Path) -> None:
    """Write the generated assets. Safe to call on every start."""
    static = Path(static)
    static.mkdir(parents=True, exist_ok=True)
    privacy_page(Path(__file__).resolve().parents[2] / "PRIVACY.md", static)

    # The palette, written out as CSS. Always rewritten: it is cheap, and a
    # stale copy would silently pin the interface to an old theme.
    stylesheet = static / "theme.css"
    generated = theme.css_variables()
    if not stylesheet.is_file() or stylesheet.read_text(encoding="utf-8") != generated:
        stylesheet.write_text(generated, encoding="utf-8")

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
