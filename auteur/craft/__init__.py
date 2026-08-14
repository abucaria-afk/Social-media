"""Craft: the difference between footage in order and a film.

Each module turns an editorial decision into the filter graph that realises it.
Nothing here decides *what* happens — only how it looks, sounds and moves once
the director has decided.
"""

from . import color, grammar, motion, sound, titles, transitions

__all__ = ["color", "grammar", "motion", "sound", "titles", "transitions"]
