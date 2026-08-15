"""The phone-facing side of the editor.

`auteur serve` runs this: a small stdlib-only web app that takes clips from a
camera roll, runs the same agent the command line runs, and hands back a film.
"""

from __future__ import annotations

from .server import Studio, serve

__all__ = ["serve", "Studio"]
