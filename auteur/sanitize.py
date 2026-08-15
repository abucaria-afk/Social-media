"""Sanitizers for user / model inputs that can end up embedded in ffmpeg filters.

Keep these functions small, deterministic and conservative: clamp numeric ranges,
whitelist known tokens, and truncate text that can be displayed or embedded.
"""
from __future__ import annotations

from typing import Any

# Whitelist of editorial transitions we accept from models / users. Anything else
# becomes a plain 'cut'. Keep in sync with auteur.edl.TRANSITIONS.
ALLOWED_TRANSITIONS = {
    "cut", "dissolve", "dip-to-black", "dip-to-white", "whip-left", "whip-right",
    "whip-up", "whip-down", "glitch", "light-leak", "zoom-blur", "film-burn",
    "slide-left", "slide-right", "wipe", "morph",
}


def sanitize_transition(kind: str) -> str:
    if not kind:
        return "cut"
    k = str(kind).strip().lower()
    return k if k in ALLOWED_TRANSITIONS else "cut"


def sanitize_number(value: Any, *, low: float, high: float, default: float) -> float:
    try:
        f = float(value)
    except Exception:
        return default
    if f != f:  # NaN
        return default
    if f < low:
        return low
    if f > high:
        return high
    return f


def sanitize_text(text: Any, *, maxlen: int = 200) -> str:
    if text is None:
        return ""
    s = str(text)
    # Strip control characters and truncate
    s = "".join(ch for ch in s if ord(ch) >= 0x20)
    if len(s) > maxlen:
        return s[:maxlen]
    return s
