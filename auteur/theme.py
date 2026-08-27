"""The palette, in one place, in two lightings.

Every surface the program shows — the terminal, the web app, the home-screen
icon, the site, the store listings — reads from this module. Colours defined
twice drift apart, and a phone app that does not look like the tool it fronts
reads as a different product.

**The two schemes are two white balances.** Cinema has exactly two, and every
piece of footage this app touches was shot under one of them: daylight at
5600K, which is blue, and tungsten at 3200K, which is orange. So daylight is
the light scheme — blues and light blues on white — and tungsten is the dark
one, an orange on near-black. The app is white-balanced to the light you are
holding it in.

That is why `ember`, the accent, is a blue in one scheme and an orange in the
other, which is not the usual arrangement and is the point. The palette this
replaced ran an orange accent against blue-grey neutrals in *both* schemes, and
an orange-on-blue-grey app with grey cards is a colourway a very large shop
already owns; it read as that shop rather than as an editing tool. Splitting
the two hues across the two lightings means they are never on screen together,
so the resemblance is gone and each scheme gets a single temperature to be.

**No greys.** A neutral with no hue in it is the colour of something nobody
chose. Daylight's neutrals carry the blue — the ground is a paper with sky in
it and the cards are actually white — and tungsten's carry the warmth, so its
near-blacks sit under the orange rather than fighting it.

Two roles exist because a colour cannot always do both jobs. `ember` is the
accent as a *fill* — the primary button, the progress bar — and it is free to
be saturated, because what has to be legible is the text sitting on it.
`ember_text` is the accent as *text*, and it has to darken on white paper or it
cannot be read.

`moss` is the "it worked" role and is deliberately not on either temperature
axis: a green in both schemes, so success never reads as just more accent.

Every role that carries text clears WCAG AA against **every surface it can sit
on** — the ground, a card, and a control on a card — in both lightings, which
is a test rather than a claim. It used to be checked against the ground alone,
and that is a different question: `text_faint` cleared 5.46 on the ground and
4.20 on a raised control, so seventy pieces of text in the app were under the
bar while the palette test was green. Anything that only ever sits on the
ground still has to clear the hardest case, because nothing stops the next
screen from putting it on a card. The worst pair in this palette is 4.99:1 in
daylight and 5.81:1 in tungsten.
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
    "moss": "it worked — a green in both schemes, on neither temperature axis",
    "rust": "it did not work, or needs attention",
    "on_rust": "text sitting on a rust fill — the one destructive button",
    "scrim": "the dim behind a sheet — the one role that carries its own alpha",
    "on_photo": "text and marks drawn on top of somebody's footage",
}

#: Tungsten, 3200K. Orange on near-black — the light a room is lit by
#: after dark, and the warmer of cinema's two white balances.
DARK: dict[str, str] = {
    # Tungsten. A near-black with warmth in it rather than a blue-grey ink:
    # the accent above is an orange, and an orange on a cool grey is the
    # combination this palette exists to stop looking like.
    "ground": "#131110",
    "surface": "#1d1917",
    "raised": "#282320",
    "line": "#39322d",
    "text": "#f8f4ef",
    "text_muted": "#bfb3a8",
    "text_faint": "#a99c91",
    "ember": "#f0a55f",
    "ember_text": "#f5b779",
    "on_ember": "#2a1606",
    "cream": "#efd9bd",
    # Green, not the light blue of the daylight scheme. "It worked" has to be
    # its own colour in both lightings, and a blue here would put blue and
    # orange back on the same screen.
    "moss": "#79c48d",
    "rust": "#f08878",
    # A near-black on the light salmon rust wears in the dark scheme, at
    # 6.99:1. The same ink as `on_ember`, because both are "text on a bright
    # fill" and two different near-blacks a shade apart is a distinction
    # nobody can see and everybody has to maintain.
    "on_rust": "#2a1606",
    # Eight digits: the last pair is the alpha. A scrim is not a colour the
    # page can be painted in, it is a dim over whatever is already there, so
    # it is the one role stored translucent. It lives here rather than as an
    # rgba() in style.css for the same reason everything else does — one
    # palette, in one file, or the stylesheet drifts away from it.
    "scrim": "#0000008c",
    # White in both schemes, and that is the point rather than an oversight.
    # The ground under this is a frame of somebody's film, not the theme's
    # surface — a play mark that went dark in daylight would be a dark mark on
    # whatever the footage happens to be, which is a coin toss. It is paired
    # with a shadow built from `scrim` so it holds on a bright frame too.
    "on_photo": "#ffffff",
}

#: Daylight, 5600K. Blue and light blue on white — the cooler of cinema's
#: two white balances, and the one a phone is usually held in.
LIGHT: dict[str, str] = {
    # Daylight. The ground is a paper with sky in it and the cards are
    # actually white, so the lift off the page is real rather than a grey
    # rectangle on a slightly different grey.
    "ground": "#eef4fb",
    "surface": "#ffffff",
    "raised": "#dfeaf7",
    "line": "#c6d8ec",
    # Ink, not black: the same blue carried all the way down.
    "text": "#0d1b2c",
    "text_muted": "#48596e",
    "text_faint": "#536477",
    "ember": "#0b62c4",
    # Darker than the fill by a long way. A button blue is a fine button and
    # an unreadable link — this clears 8.01:1 on a white card.
    "ember_text": "#0a5099",
    "on_ember": "#ffffff",
    # The light blue, doing the job a soft tint does: large gentle fills.
    "cream": "#d5e6fa",
    # A deep teal-green. Distinct from the blue accent at a glance, which a
    # lighter green next to this much blue would not be.
    "moss": "#0f6b5c",
    "rust": "#b3261e",
    # White, at 6.54:1 on the red rust wears in daylight. This is the button
    # that deletes an account and it can afford to be plain.
    "on_rust": "#ffffff",
    # Lighter than the dark scheme's, and tinted with the ink rather than pure
    # black. The same 55% black that reads as a dim over a near-black page
    # reads as a blackout over a bright one, and the content behind a sheet is
    # meant to still be visible.
    "scrim": "#0d1b2c66",
    "on_photo": "#ffffff",
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
