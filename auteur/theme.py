"""The palette, in one place, in two lightings.

Every surface the program shows — the terminal, the web app, the home-screen
icon — reads from this module. Colours defined twice drift apart, and a phone
app that does not look like the tool it fronts reads as a different product.

Both palettes are taken from the reels this editor is built to make. Sampling
the coloured pixels of thirty of them — dropping anything under 18% saturation,
which is most of the frame — gives a strongly bimodal distribution: a warm lobe
across 0-50 degrees holding 37% of them, and a teal lobe across 170-220 holding
42%. What matters more than either hue is the *saturation*: the median coloured
pixel sits at 0.40, nowhere near the neon these films are usually assumed to
be. They are muted, and they are bright — mean luma 0.249 on frames that are
mostly dark, which means the colour that is there carries.

So the palette is muted and bright rather than saturated and dark. `ember` is
the warm lobe at the lightness it reads at, an apricot rather than the amber it
used to be; `moss` is the teal lobe, doing the work a green usually does; the
grounds are a soft ink and a bone paper, neither of them the black-and-white
the palette reached for before. A near-black ground and a pure-white one are
the two easiest colours to pick and the two that look least like anything.

Two roles exist only because a colour cannot always do both jobs. `ember` is
the accent as a *fill* — the primary button, the progress bar — and it is free
to be bright, because what has to be legible is the text sitting on it.
`ember_text` is the accent as *text*, and on bone paper it has to darken
sharply or it cannot be read. Every role that carries text clears WCAG AA
against its own ground in both lightings, which is a test, not a claim.
"""

from __future__ import annotations

#: What each role is for. The two palettes below must define exactly these.
ROLES: dict[str, str] = {
    "ground": "the page behind everything",
    "surface": "cards and panels lifted off the ground",
    "raised": "a control sitting on a surface",
    "line": "borders and separators",
    "text": "primary text",
    "text_muted": "secondary text, hints, and detail lines",
    "text_faint": "the quietest text that is still readable",
    "ember": "the accent as a fill: primary button, progress, focus ring",
    "ember_text": "the accent as text, dark enough to read on this ground",
    "on_ember": "text sitting on an ember fill",
    "cream": "the accent's soft tint, for large gentle fills",
    "moss": "it worked — the teal lobe of the footage, doing a green's job",
    "rust": "it did not work, or needs attention",
}

#: Night. A soft ink rather than a black — the ground a modern phone app
#: sits on, and the one the reels' own shadows sit closest to.
DARK: dict[str, str] = {
    "ground": "#19181b",
    "surface": "#232127",
    "raised": "#2f2d34",
    "line": "#3b3842",
    "text": "#f5f2ef",
    "text_muted": "#b5acb9",
    "text_faint": "#978b9c",
    "ember": "#eca669",
    "ember_text": "#f1b47e",
    "on_ember": "#2c1807",
    "cream": "#edd6c0",
    "moss": "#72bccb",
    "rust": "#ee8777",
}

#: Daylight. Bone paper, not white: the same warm lobe the accent comes
#: from, taken up to a ground. text_muted and text_faint are darkened to
#: clear AA on it, and ember_text darkened much further — a bright apricot
#: is a fine button and an unreadable link.
LIGHT: dict[str, str] = {
    "ground": "#f7f5f2",
    "surface": "#ffffff",
    "raised": "#ede9e3",
    "line": "#ded7cf",
    "text": "#1c1a17",
    "text_muted": "#5d5751",
    "text_faint": "#68615a",
    "ember": "#efa15d",
    "ember_text": "#964b13",
    "on_ember": "#2c1807",
    "cream": "#f7e7d4",
    "moss": "#286571",
    "rust": "#ab3321",
}

SCHEMES: dict[str, dict[str, str]] = {"dark": DARK, "light": LIGHT}

#: The browser tab / status bar colour, in both lightings. Every page carries
#: these twice — once in a media-scoped <meta theme-color> tag and once in
#: theme.js, which repaints the tag when somebody overrides the system setting
#: — and the manifest carries the dark one again. They are hand-written copies
#: of these two constants, so they are held to them by test rather than by
#: anybody remembering: recolouring the palette without this check left every
#: status bar the old warm brown while the page behind it went blue.
THEME_COLOR = DARK["ground"]
LIGHT_THEME_COLOR = LIGHT["ground"]

#: What the appearance switch offers. "system" is the default: follow the phone.
MODES = ("system", "light", "dark")


def hex_of(role: str, scheme: str = "dark") -> str:
    """The hex string for a role, e.g. `hex_of("ember")`."""
    return SCHEMES[scheme][role]


def rgb_of(role: str, scheme: str = "dark") -> tuple[int, int, int]:
    """The role as an (r, g, b) triple, for Pillow."""
    value = hex_of(role, scheme).lstrip("#")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def _block(scheme: str, indent: str = "  ") -> str:
    return "\n".join(
        f"{indent}--{role.replace('_', '-')}: {SCHEMES[scheme][role]};" for role in ROLES
    )


def css_variables() -> str:
    """The palette as a CSS file, covering all three appearance settings.

    Generated rather than hand-written so the stylesheet cannot fall out of step
    with the icons or the terminal — there is no second copy to forget.

    The order matters. Dark is the base, because that is the designed look and
    it is what anything unexpected should fall back to. The media query then
    follows the phone's own setting, which is what "system" means, and is
    skipped once the reader has chosen for themselves. The two `data-theme`
    rules come last so an explicit choice beats both.
    """
    lines = [
        "/* Generated from auteur/theme.py. Do not edit by hand — edit the palette. */",
        "",
        "/* Base: night. Also the fallback for anything that matches nothing else. */",
        ":root {",
        "  color-scheme: dark light;",
        _block("dark"),
        "}",
        "",
        "/* System setting, unless the reader has overridden it. */",
        "@media (prefers-color-scheme: light) {",
        "  :root:not([data-theme]) {",
        _block("light", indent="    "),
        "  }",
        "}",
        "",
        "/* An explicit choice wins over the system in both directions. */",
        ':root[data-theme="light"] {',
        "  color-scheme: light;",
        _block("light"),
        "}",
        "",
        ':root[data-theme="dark"] {',
        "  color-scheme: dark;",
        _block("dark"),
        "}",
    ]
    return "\n".join(lines) + "\n"


def ansi(role: str) -> str:
    """The role as a 24-bit terminal escape, so the CLI matches the app.

    Always the dark palette: a terminal is a dark frame whatever the desktop
    around it is set to, and there is no way to ask it which.
    """
    red, green, blue = rgb_of(role, "dark")
    return f"38;2;{red};{green};{blue}"


def contrast(foreground: tuple[int, int, int], background: tuple[int, int, int]) -> float:
    """WCAG contrast ratio between two colours, 1.0 (identical) to 21.0."""

    def luminance(rgb: tuple[int, int, int]) -> float:
        channels = []
        for value in rgb:
            portion = value / 255
            channels.append(
                portion / 12.92 if portion <= 0.03928 else ((portion + 0.055) / 1.055) ** 2.4
            )
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    first, second = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (first + 0.05) / (second + 0.05)
