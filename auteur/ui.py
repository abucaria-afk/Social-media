"""What the person running this actually sees.

Editing a film takes minutes, so the two things that matter are saying what is
happening in plain words and never going quiet. Everything here is optional
decoration around the real work: a Reporter that prints nothing is a valid
Reporter, and the agent behaves identically either way.

No jargon in anything this module prints. "Cut to the beat", not "beat-snapped
EDL"; "vertical, for phones", not "9:16 primary delivery format".
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import TextIO

from . import theme

#: Left margin for everything under a step heading.
INDENT = "     "

#: The terminal's share of the palette in `theme.py`, so a run in a shell and a
#: run in the phone app are recognisably the same program. Terminals that do not
#: do 24-bit colour fall back to the plain bold/dim codes below.
INK = {
    "heading": "1;" + theme.ansi("text"),
    "accent": theme.ansi("ember"),
    "muted": theme.ansi("text_muted"),
    "good": "1;" + theme.ansi("moss"),
    "bad": "1;" + theme.ansi("rust"),
}


def _truecolor() -> bool:
    return os.environ.get("COLORTERM", "") in ("truecolor", "24bit")


def _supports_colour(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    return hasattr(stream, "isatty") and stream.isatty()


class Reporter:
    """Prints the story of the edit as it happens."""

    def __init__(self, stream: TextIO | None = None, *, enabled: bool = True):
        self.stream = stream or sys.stdout
        self.enabled = enabled
        self.colour = enabled and _supports_colour(self.stream)
        self.rich = self.colour and _truecolor()
        self.interactive = self.colour  # a bar only makes sense on a live terminal
        self._bar_open = False
        self._any_step = False

    # -- plumbing ---------------------------------------------------------

    def _paint(self, text: str, code: str) -> str:
        """Colour some text.

        `code` is a plain SGR code; a role name from INK is used instead when the
        terminal can do 24-bit colour, so the palette matches the web app exactly
        where that is possible and degrades to bold/dim where it is not.
        """
        if not self.colour:
            return text
        return f"\033[{code}m{text}\033[0m"

    def _tint(self, text: str, role: str, plain: str) -> str:
        """Palette colour where the terminal supports it, `plain` SGR otherwise."""
        return self._paint(text, INK[role] if self.rich else plain)

    def _write(self, text: str = "") -> None:
        if not self.enabled:
            return
        self._close_bar()
        self.stream.write(text + "\n")
        self.stream.flush()

    def _close_bar(self) -> None:
        if self._bar_open:
            self.stream.write("\n")
            self.stream.flush()
            self._bar_open = False

    # -- the story --------------------------------------------------------

    def banner(self, prompt: str) -> None:
        self._write()
        self._write(
            "  " + self._tint("auteur", "accent", "1") + self._paint("  ·  the edit room", "2")
        )
        self._write("  " + self._paint(f'"{prompt}"', "2"))
        self._write()

    def step(self, title: str) -> None:
        """A new stage of the work, with air around it so it can be scanned."""
        if self._any_step:
            self._write()
        self._any_step = True
        self._write("  " + self._tint(title, "heading", "1"))

    def detail(self, text: str) -> None:
        """A plain fact under the current step."""
        self._write(INDENT + self._tint(text, "muted", "2"))

    def found(self, label: str, text: str) -> None:
        """A labelled finding, e.g. what the critic saw and what it changed."""
        self._write(INDENT + self._tint(f"{label:<6}", "muted", "2") + text)

    def warn(self, text: str) -> None:
        self._write(INDENT + self._tint("note   ", "accent", "33") + text)

    def progress(self, done: int, total: int, label: str = "") -> None:
        """A live bar. Falls back to nothing at all when output is not a terminal."""
        if not self.enabled or not self.interactive or total <= 0:
            return
        width = max(10, min(28, shutil.get_terminal_size((80, 24)).columns - 40))
        filled = int(width * done / total)
        bar = "█" * filled + "░" * (width - filled)
        percent = f"{100 * done / total:3.0f}%"
        line = f"{INDENT}{self._tint(bar, 'accent', '36')} {percent}  {self._tint(label, 'muted', '2')}"
        self.stream.write("\r\033[K" + line)
        self.stream.flush()
        self._bar_open = True

    def progress_done(self, label: str = "done") -> None:
        if not self.enabled:
            return
        if self.interactive and self._bar_open:
            width = max(10, min(28, shutil.get_terminal_size((80, 24)).columns - 40))
            line = f"{INDENT}{self._tint('█' * width, 'accent', '36')} 100%  {self._tint(label, 'muted', '2')}"
            self.stream.write("\r\033[K" + line + "\n")
            self.stream.flush()
            self._bar_open = False
        else:
            self.detail(label)

    def blank(self) -> None:
        self._write()

    # -- the ending -------------------------------------------------------

    def result(self, *, headline: str, facts: list[str], files: list[tuple[str, str]]) -> None:
        self._write()
        self._write("  " + self._tint("✓  " + headline, "good", "1;32"))
        self._write()
        for fact in facts:
            self._write(INDENT + fact)
        if files:
            self._write()
            width = max(len(label) for label, _ in files)
            for label, path in files:
                self._write(INDENT + self._tint(f"{label:<{width}}  ", "muted", "2") + path)
        self._write()

    def failure(self, headline: str, hint: str = "") -> None:
        self._write()
        self._write("  " + self._tint("✗  " + headline, "bad", "1;31"))
        if hint:
            self._write(INDENT + self._tint(hint, "muted", "2"))
        self._write()


class NullReporter(Reporter):
    """Says nothing. Used by --quiet and by the library API."""

    def __init__(self) -> None:
        super().__init__(stream=sys.stdout, enabled=False)


# ---------------------------------------------------------------------------
# Turning internals into plain English
# ---------------------------------------------------------------------------


def describe_shape(width: int, height: int) -> str:
    """ "vertical, for phones" beats "9:16"."""
    ratio = width / height if height else 1.0
    if ratio < 0.95:
        return "vertical, for phones" if ratio < 0.7 else "portrait"
    if ratio > 1.05:
        return "cinematic widescreen" if ratio > 1.9 else "widescreen"
    return "square"


def describe_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} seconds"
    minutes, rest = divmod(seconds, 60)
    return f"{minutes:.0f}m {rest:02.0f}s"


def describe_count(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


#: The critic's rule names are shorthand; these are what a person should read.
PLAIN_FINDINGS = {
    "dead-air": "a stretch where nothing moves",
    "flash-frame": "a shot too quick to register",
    "exposure": "the brightness jumps at some cuts",
    "metronomic": "every shot is the same length",
    "off-beat": "the cuts drift off the music",
    "weak-hook": "the opening is quieter than the rest",
    "runtime": "it came out the wrong length",
    "black": "much of it is very dark",
    "unreadable": "the rendered file could not be read back",
}


def plain_finding(rule: str, fallback: str) -> str:
    return PLAIN_FINDINGS.get(rule, fallback)


def plain_model_reason(reason: str) -> tuple[str, bool]:
    """Why the built-in editor cut this film, and whether that is a problem.

    Not having an API key is the ordinary case, not a warning — dumping a raw
    SDK authentication error on someone who never asked for Claude reads like a
    failure when nothing has gone wrong.
    """
    lowered = reason.lower()
    if not reason or "no model configured" in lowered:
        return "", False
    if "not installed" in lowered:
        return "Claude is not installed here, so the built-in editor cut this one", False
    if any(word in lowered for word in ("authentication", "api_key", "api key", "credential")):
        return "no Claude API key set, so the built-in editor cut this one", False
    short = reason.split("\n")[0][:90]
    return f"could not reach Claude ({short}), so the built-in editor cut this one", True
