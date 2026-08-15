"""The palette, in one place.

Every surface the program shows — the terminal, the web app, the home-screen
icon — reads from this module. Colours defined twice drift apart, and a phone
app that does not look like the tool it fronts reads as a different product.

The palette is sampled from the footage this editor was built for: torchlit
night photography, where a warm subject sits in a near-black frame. The
dominant clusters in that material are an almost neutral black ground
(#080808–#282828), a warm cream-amber subject at hue ~30–36 (#e8c8a8, #f8d8a8),
low-saturation forest green at hue ~120 (#283828, #384838), and a pure silver
highlight (#f8f8f8). Those five families are the five roles below.

Nothing here is decoration. `ground` is what the frame is, `ember` is what the
light picks out, and the interface uses them the same way the footage does:
one warm accent against a dark field, and nothing else competing for the eye.
"""

from __future__ import annotations

#: role -> (hex, what it is for)
PALETTE: dict[str, tuple[str, str]] = {
    # The frame. Very slightly warm rather than pure black, because the
    # footage's shadows are warm and true #000 next to them looks like a hole.
    "ground":    ("#0c0b0a", "the page behind everything"),
    "surface":   ("#171614", "cards and panels lifted off the ground"),
    "raised":    ("#201e1b", "a control sitting on a surface"),
    "line":      ("#2e2b26", "borders and separators"),

    # The subject. Sampled from the mushroom cap under a torch.
    "ember":     ("#e9a85c", "the one accent: primary action, progress, focus"),
    "ember_dim": ("#8a6132", "the accent at rest, and its own borders"),
    "cream":     ("#e8c8a8", "the accent's lighter tint, for large soft fills"),

    # Text. Warm white, not blue-white: the silver of a web at night.
    "paper":     ("#f2ede4", "primary text"),
    "muted":     ("#a9a49a", "secondary text, hints, and detail lines"),
    "faint":     ("#6d6960", "the quietest text that is still readable"),

    # Outcomes. Both taken from the material rather than from a UI convention:
    # the moss on the forest floor, and the rust of dry pine needles.
    "moss":      ("#8fb283", "it worked"),
    "rust":      ("#d4785a", "it did not work, or needs attention"),

    # Text that sits *on* the accent, so it must be dark.
    "on_ember":  ("#241703", "text on an ember-filled button"),
}

#: The browser tab / status bar colour, and the icon's ground.
THEME_COLOR = PALETTE["ground"][0]


def hex_of(role: str) -> str:
    """The hex string for a role, e.g. `hex_of("ember")`."""
    return PALETTE[role][0]


def rgb_of(role: str) -> tuple[int, int, int]:
    """The role as an (r, g, b) triple, for Pillow."""
    value = hex_of(role).lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def css_variables() -> str:
    """The palette as a CSS file.

    Generated rather than hand-written so the stylesheet cannot fall out of step
    with the icons or the terminal — there is no second copy to forget.
    """
    lines = [
        "/* Generated from auteur/theme.py. Do not edit by hand — edit the palette. */",
        ":root {",
    ]
    for role, (value, purpose) in PALETTE.items():
        lines.append(f"  --{role.replace('_', '-')}: {value};  /* {purpose} */")
    lines.append("}")
    return "\n".join(lines) + "\n"


def ansi(role: str) -> str:
    """The role as a 24-bit terminal escape, so the CLI matches the app."""
    red, green, blue = rgb_of(role)
    return f"38;2;{red};{green};{blue}"
