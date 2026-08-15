"""The palette, in one place, in two lightings.

Every surface the program shows — the terminal, the web app, the home-screen
icon — reads from this module. Colours defined twice drift apart, and a phone
app that does not look like the tool it fronts reads as a different product.

Both palettes are sampled from the footage this editor was built for: torchlit
night photography, where a warm subject sits in a near-black frame. The dark
palette is that frame — the shadow clusters (#080808–#282828), the cream-amber
subject at hue 30–36, low-saturation forest green, a silver highlight. The
light palette is the *lit side* of the same photographs: the torch-struck cream
at hue 30–40 (#f6ead2, #ead2ae, #d2ae8a). One scene, two exposures, rather than
a dark theme and an unrelated white one.

Two roles exist only because a colour cannot always do both jobs. `ember` is
the accent as a *fill* — the primary button, the progress bar — and it stays
the same warm amber in both lightings, because it is the thing people
recognise. `ember_text` is the accent as *text*, and on a pale ground it has to
darken sharply or it cannot be read.
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
    "moss": "it worked",
    "rust": "it did not work, or needs attention",
}

#: Night. The frame the footage is shot in, and the program's own look.
DARK: dict[str, str] = {
    "ground": "#0c0b0a",
    "surface": "#171614",
    "raised": "#201e1b",
    "line": "#2e2b26",
    "text": "#f2ede4",
    "text_muted": "#a9a49a",
    "text_faint": "#8b857a",
    "ember": "#e9a85c",
    "ember_text": "#e9a85c",
    "on_ember": "#241703",
    "cream": "#e8c8a8",
    "moss": "#8fb283",
    "rust": "#d4785a",
}

#: The lit side of the same photographs. Warm paper, not clinical white.
LIGHT: dict[str, str] = {
    "ground": "#f6f1e6",
    "surface": "#fffdf7",
    "raised": "#efe6d5",
    "line": "#d8cab2",
    "text": "#1c1815",
    "text_muted": "#5c554b",
    "text_faint": "#6f675b",
    "ember": "#e9a85c",
    "ember_text": "#7f4f11",
    "on_ember": "#241703",
    "cream": "#ead2ae",
    "moss": "#37613d",
    "rust": "#9b3618",
}

SCHEMES: dict[str, dict[str, str]] = {"dark": DARK, "light": LIGHT}

#: The browser tab / status bar colour. Only the dark one is needed here: the
#: page carries both in media-scoped <meta theme-color> tags.
THEME_COLOR = DARK["ground"]

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
        f"{indent}--{role.replace('_', '-')}: {SCHEMES[scheme][role]};"
        for role in ROLES
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


def contrast(
    foreground: tuple[int, int, int], background: tuple[int, int, int]
) -> float:
    """WCAG contrast ratio between two colours, 1.0 (identical) to 21.0."""

    def luminance(rgb: tuple[int, int, int]) -> float:
        channels = []
        for value in rgb:
            portion = value / 255
            channels.append(
                portion / 12.92
                if portion <= 0.03928
                else ((portion + 0.055) / 1.055) ** 2.4
            )
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    first, second = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (first + 0.05) / (second + 0.05)
