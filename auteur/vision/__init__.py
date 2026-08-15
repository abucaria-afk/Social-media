"""Seeing a frame, rather than measuring one.

    from auteur.vision import read_asset

    reading = read_asset("photo.jpeg")
    print(reading.describe())
    reading.focus            # where the eye lands, normalised
    reading.composition      # "Rule of Thirds", "Dutch Angle", ...

The rest of the project measures frames — brightness, motion, edge density.
This one reads them: where attention goes, how the picture is built, what the
light and the colour are doing to each other. It answers the questions an
analyst asks rather than the questions a decoder answers.

It knows nothing about what the footage is *of*. No objects, no faces, no text.
It reads structure, light and colour, and it will describe a beautifully
composed photograph of nothing as exactly that.
"""

from __future__ import annotations

from .connoisseur import (
    COMPOSITIONS,
    LIGHTING,
    PALETTES,
    Reading,
    read_asset,
    read_frame,
)

__all__ = [
    "COMPOSITIONS",
    "LIGHTING",
    "PALETTES",
    "Reading",
    "read_asset",
    "read_frame",
]
