"""Telling a picture from a catalogue photograph of an object.

Museum open-access collections are enormous and mostly not pictures. They are
records: a coin on a grey sweep, a page of text, a textile swatch, a buckle
photographed square-on so a curator can identify it later. Those are the right
way to photograph an object and the wrong thing to cut into a film. "Search for
public domain art" returns them by the thousand, and finding the handful worth
using is the whole problem.

**The obvious approach fails, and fails backwards.** This project already has a
craft score — separation, subject, palette, exposure — used to measure the
reels it is chasing. Run it over the archetypes and the catalogue photograph of
a coin scores 0.671, ahead of a real still frame at 0.631 and more than double
a real handheld clip at 0.301. It is not a bug in the score. A small sharp
object on a flat ground *is* maximum depth separation and *is* an unambiguous
subject, and a neutral grey sweep *is* a narrow palette. Every dimension reads
the record shot as excellent. Ranking candidates by craft would sort the slop
to the top.

So the gates here are measured separately, and one of them runs the palette
signal in the opposite direction from craft:

**A blank ground** — hue spread under 25° *and* contrast under 0.12. The
measured record shots run 0.0–4.4° of hue at 0.019–0.039 contrast; the real
pictures run 73–127° at 0.09–0.26. Both halves are load-bearing. A graded film
narrows its palette on purpose, and a black and white photograph has no hue at
all, so hue alone would throw away exactly the photographs worth having. What a
record shot has that they do not is a *uniform* ground: no hue and no tonal
range either.

**A texture rather than a picture** — detail spread over more than 35% of the
frame. The document scan sits at 0.44 and the swatch at 0.93, because every
part of them is equally busy. Every real picture measured came in under 0.21.
There is nowhere for the eye to go in a page of text.

These were set from measurement, not taste, and they classify all ten measured
images correctly. They are also deliberately coarse: they throw out what is
not a picture at all, and then craft ranks what remains. Judging *art* is not
something this can do and does not claim to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("auteur.gallery.curator")

#: Below this much hue variety the frame has no palette. Record shots measured
#: 0.0-4.4 degrees; the real pictures measured 73-127.
FLAT_HUE = 25.0

#: ...but only together with this. A black and white photograph also has no
#: hue, and it is not a record shot: it has tonal range. Records measured
#: 0.019-0.039 contrast against 0.09-0.26 for real pictures.
FLAT_CONTRAST = 0.12

#: Detail spread wider than this is a texture, not a composition. Documents
#: measured 0.44 and swatches 0.93; no real picture measured above 0.21.
ALL_OVER = 0.35

#: A picture that will be cropped to 9:16 and shown on a phone needs pixels.
#: Below this it is a thumbnail of a painting rather than a painting.
LEAST_PIXELS = 700


@dataclass
class Candidate:
    """One record from a collection, before anybody has looked at the picture."""

    provider: str = ""
    ref: str = ""
    title: str = ""
    artist: str = ""
    date: str = ""
    medium: str = ""
    classification: str = ""
    #: The largest image the collection offers, and a smaller one to judge from.
    image_url: str = ""
    preview_url: str = ""
    #: The record's own page, for a person who wants to check.
    page_url: str = ""
    #: The provider's own rights statement. Never inferred from a search filter.
    rights: str = ""
    width: int = 0
    height: int = 0
    credit: str = ""

    @property
    def key(self) -> str:
        """What makes two records the same work, across two collections."""
        return f"{self.artist.strip().lower()}|{self.title.strip().lower()}"


@dataclass
class Judgement:
    """What the eye made of one candidate."""

    candidate: Candidate
    craft: float = 0.0
    hue_spread: float = 0.0
    contrast: float = 0.0
    busy: float = 0.0
    luma: float = 0.0
    #: Empty when it passed. Otherwise why it did not.
    rejected: str = ""
    local: Path | None = None

    @property
    def kept(self) -> bool:
        return not self.rejected

    def describe(self) -> str:
        who = f"{self.candidate.artist}, " if self.candidate.artist else ""
        head = f"{self.candidate.title[:56]} — {who}{self.candidate.provider}"
        if self.rejected:
            return f"  ✗ {head}\n      {self.rejected}"
        return (
            f"  ✓ {head}\n"
            f"      craft {self.craft:.3f} · hue {self.hue_spread:.0f}° · "
            f"contrast {self.contrast:.2f} · detail over {self.busy:.0%} of frame"
        )


#: Words that mean the record is a *piece of* something rather than a picture
#: of it. These are catalogue vocabulary, not judgements of taste — a fragment
#: is genuinely a fragment.
NOT_A_PICTURE = (
    "fragment",
    "sherd",
    "shard",
    "swatch",
    "sample book",
    "sample card",
    "trade card",
    "specimen",
    "off-cut",
)

#: Rights strings the collections use for work that is genuinely free. Checked
#: on the record itself rather than trusted from the search filter, because a
#: filter is a request and this is the answer.
FREE = ("public domain", "cc0", "no known copyright", "open access")


def paperwork_clears(candidate: Candidate) -> str:
    """Empty if this record may be used at all. Otherwise why not.

    Cheap, and it runs before anything is downloaded — most of what a search
    returns can be dismissed without fetching a single pixel.
    """
    rights = (candidate.rights or "").strip().lower()
    if not any(word in rights for word in FREE):
        return f"rights say {candidate.rights!r}, which is not clearly free"
    if not candidate.image_url:
        return "the record has no image"
    if candidate.width and candidate.height:
        if min(candidate.width, candidate.height) < LEAST_PIXELS:
            return f"only {candidate.width}x{candidate.height} — too small to fill a phone"
    haystack = f"{candidate.title} {candidate.classification} {candidate.medium}".lower()
    for word in NOT_A_PICTURE:
        if word in haystack:
            return f"catalogued as a {word}, which is a piece of a thing rather than a picture"
    return ""


def looks_like_a_record_shot(reading) -> str:
    """Empty if there is a picture here. Otherwise what it is instead.

    Takes a `auteur.vision.Reading`. See the module docstring for why this does
    not simply ask the craft score.
    """
    if reading.hue_spread < FLAT_HUE and reading.contrast < FLAT_CONTRAST:
        return (
            f"an object on a blank ground — {reading.hue_spread:.0f}° of hue at "
            f"{reading.contrast:.2f} contrast is a catalogue photograph, not a picture"
        )
    if reading.busy > ALL_OVER:
        return (
            f"detail spread over {reading.busy:.0%} of the frame — a texture or a page "
            "of text, with nowhere for the eye to go"
        )
    return ""


@dataclass
class Curation:
    """The result of a search: what was kept, what was not, and why."""

    query: str = ""
    kept: list[Judgement] = field(default_factory=list)
    dropped: list[Judgement] = field(default_factory=list)
    trouble: list[str] = field(default_factory=list)

    @property
    def looked_at(self) -> int:
        return len(self.kept) + len(self.dropped)

    def describe(self) -> str:
        lines = [
            f'"{self.query}" — kept {len(self.kept)} of {self.looked_at}',
        ]
        for judgement in self.kept:
            lines.append(judgement.describe())
        if self.dropped:
            lines.append(f"  and {len(self.dropped)} turned away:")
            for judgement in self.dropped[:6]:
                lines.append(judgement.describe())
        for note in self.trouble:
            lines.append(f"  ! {note}")
        return "\n".join(lines)


class Curator:
    """Search public-domain collections and keep only what is a picture.

    Two stages, in the order that costs least. The paperwork gate runs on the
    records and throws out everything that cannot be used or cannot be a
    picture, without fetching a pixel. Only what survives is downloaded, and
    only then does the eye look at it.
    """

    def __init__(self, into: Path, *, transport=None):
        self.into = Path(into)
        self.into.mkdir(parents=True, exist_ok=True)
        self.transport = transport

    def search(
        self,
        query: str,
        *,
        keep: int = 8,
        consider: int = 24,
        collections: list[str] | None = None,
    ) -> Curation:
        from .sources import Web, search_all

        transport = self.transport or Web()
        found, trouble = search_all(
            query, transport=transport, limit=consider, collections=collections
        )
        result = Curation(query=query, trouble=list(trouble))

        # -- stage one: the paperwork, and duplicates across collections ---
        seen: set[str] = set()
        shortlist: list[Candidate] = []
        for candidate in found:
            why = paperwork_clears(candidate)
            if why:
                result.dropped.append(Judgement(candidate=candidate, rejected=why))
                continue
            if candidate.key in seen:
                result.dropped.append(
                    Judgement(candidate=candidate, rejected="the same work from another collection")
                )
                continue
            seen.add(candidate.key)
            shortlist.append(candidate)

        # -- stage two: look at it ------------------------------------------
        for candidate in shortlist:
            judgement = self._look(candidate, transport)
            (result.kept if judgement.kept else result.dropped).append(judgement)

        # Craft only ranks what is already known to be a picture. On its own it
        # sorts catalogue photographs to the top — see the module docstring.
        result.kept.sort(key=lambda j: -j.craft)
        for extra in result.kept[keep:]:
            extra.rejected = "good, but further down than you asked to keep"
            result.dropped.append(extra)
        result.kept = result.kept[:keep]
        return result

    def _look(self, candidate: Candidate, transport) -> Judgement:
        """Fetch the preview and read it."""
        from ..insight.benchmark import craft_score
        from ..vision import read_asset

        judgement = Judgement(candidate=candidate)
        suffix = ".jpg" if ".jpg" in candidate.preview_url.lower() else ".png"
        local = self.into / f"{candidate.provider.split()[0].lower()}-{candidate.ref}{suffix}"
        try:
            local.write_bytes(transport.get(candidate.preview_url))
        except Exception as exc:  # noqa: BLE001 - a dead image is a dropped result
            judgement.rejected = f"the image could not be fetched ({exc})"
            return judgement

        try:
            reading = read_asset(local, samples=1)
        except Exception as exc:  # noqa: BLE001 - unreadable media, same treatment
            judgement.rejected = f"the image could not be read ({exc})"
            local.unlink(missing_ok=True)
            return judgement

        judgement.hue_spread = reading.hue_spread
        judgement.contrast = reading.contrast
        judgement.busy = reading.busy
        judgement.luma = reading.luma
        judgement.craft = craft_score(reading).overall
        judgement.local = local

        why = looks_like_a_record_shot(reading)
        if why:
            judgement.rejected = why
            local.unlink(missing_ok=True)
            judgement.local = None
        return judgement
