"""Studying what is already on the disk: the documents, and the films.

The Scholar's other source is YouTube, which needs a network, a search, and
somebody else to have made a video about the thing. Meanwhile the material
that matters most is sitting in the working directory: the notes written about
how this program is supposed to work, and the reels it is measured against.

Two kinds, learned two different ways.

**Documents** — markdown, plain text — are read for statements that assert
something. Not summarised: a summary of a document is a worse document. What is
extracted is the sentences that make a claim strong enough to act on, keyed to
the heading they sat under, so a learning carries the section it came from.

**Films** are not read at all, they are *measured* — by the same code that
measures a benchmark. How fast it cuts, how long the first shot holds, what the
frames are doing. Those are numbers the crew can be held against, which is the
point: "the reels you were told to study cut six times a second and you cut
twice" is a criticism an agent can act on. "This video seemed energetic" is not.

**Everything learned here is marked as coming from one source.** The Scholar's
confidence rule counts independent channels, not notes, so a hundred learnings
from one folder stay TENTATIVE until something else agrees with them. That is
deliberate: a project's own notes agreeing with a project's own notes is not
corroboration.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .knowledge import Confidence, Discipline, Learning

log = logging.getLogger("auteur.scholar.library")

#: What counts as a document worth reading.
DOCUMENTS = (".md", ".markdown", ".txt", ".rst")

#: What counts as a film worth measuring.
FILMS = (".mp4", ".mov", ".m4v", ".webm")

#: Folders never worth walking into.
SKIP = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}

#: A sentence shorter than this is a fragment; longer than this is a paragraph
#: somebody forgot to break up, and neither is a claim.
SHORTEST_CLAIM = 40
LONGEST_CLAIM = 320

#: Words that mark a sentence as asserting something rather than describing it.
#: Deliberately plain: this is not natural language understanding, it is a
#: filter that keeps imperative and comparative sentences and drops the rest.
ASSERTS = (
    " must ",
    " should ",
    " never ",
    " always ",
    " because ",
    " so that ",
    " rather than ",
    " instead of ",
    " means ",
    " matters ",
    " is why ",
    " the point ",
)


def _stable_id(*parts: str) -> str:
    """A learning id that is the same next run. `hash()` is salted per process."""
    digest = hashlib.blake2b("|".join(parts).encode("utf-8"), digest_size=8)
    return digest.hexdigest()


def _content_id(path: Path, *, sample: int = 1 << 20) -> str:
    """What this file *is*, cheaply — size plus the first and last megabyte.

    Not the whole file: these are reels, and hashing forty of them end to end
    to notice a duplicate costs more than the measuring does. Size plus both
    ends is enough to tell two different films apart while still recognising a
    copy under another name.
    """
    try:
        size = path.stat().st_size
        digest = hashlib.blake2b(str(size).encode(), digest_size=16)
        with path.open("rb") as handle:
            digest.update(handle.read(sample))
            if size > sample * 2:
                handle.seek(-sample, 2)
                digest.update(handle.read(sample))
        return digest.hexdigest()
    except OSError:
        return str(path)


@dataclass
class StudyMaterial:
    """What is on the disk, sorted into the two things that can be done with it."""

    documents: list[Path] = field(default_factory=list)
    films: list[Path] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.documents) + len(self.films)


def find_study_material(roots, *, most: int = 400) -> StudyMaterial:
    """Walk the given folders for documents and films.

    Deliberately bounded. Pointed at a home directory this would otherwise
    walk it all, and a Scholar that spends four minutes listing files before
    it learns anything is a Scholar nobody leaves running.
    """
    found = StudyMaterial()
    for root in [Path(r) for r in (roots or [])]:
        if root.is_file():
            _sort_one(root, found)
            continue
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if found.total >= most:
                log.info("stopping at %d files — pointed at somewhere very large", most)
                return found
            if any(part in SKIP for part in path.parts):
                continue
            if path.is_file():
                _sort_one(path, found)
    return found


def _sort_one(path: Path, into: StudyMaterial) -> None:
    suffix = path.suffix.lower()
    if suffix in DOCUMENTS:
        into.documents.append(path)
    elif suffix in FILMS:
        into.films.append(path)


def _sentences(text: str):
    """Split prose into sentences, keeping it simple and predictable."""
    for raw in re.split(r"(?<=[.!?])\s+", text):
        cleaned = " ".join(raw.split())
        if cleaned:
            yield cleaned


def read_document(path: Path, *, most: int = 12) -> list[Learning]:
    """Pull the claims out of a document, keyed to the heading they sat under.

    A claim is a sentence that asserts something — the vocabulary in `ASSERTS`.
    That is a crude test and it is meant to be: the alternative is inventing a
    summary, and a Scholar that paraphrases its sources learns its own opinions.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.debug("could not read %s: %s", path, exc)
        return []

    learnings: list[Learning] = []
    heading = path.stem.replace("-", " ").replace("_", " ")
    disciplines = _disciplines_for(path, text)

    # Paragraph at a time, not line at a time. Prose in markdown is wrapped at
    # some column, so a sentence is routinely spread over three lines — split
    # per line and every claim comes out as a fragment ending mid-clause.
    def claims_in(paragraph: str, under: str):
        for sentence in _sentences(paragraph):
            if not SHORTEST_CLAIM <= len(sentence) <= LONGEST_CLAIM:
                continue
            padded = f" {sentence.lower()} "
            if not any(word in padded for word in ASSERTS):
                continue
            yield Learning(
                learning_id=_stable_id("doc", str(path), sentence),
                disciplines=disciplines,
                insight=sentence,
                technique=under[:120],
                application=f"from {path.name}, under “{under[:60]}”",
                source_video_id=f"file:{path.name}",
                source_channel=f"local:{path.parent.name}",
                source_title=path.name,
                confidence=Confidence.TENTATIVE,
            )

    buffer: list[str] = []

    def flush(under: str) -> bool:
        """Returns True when there is no room for more."""
        if not buffer:
            return False
        paragraph = " ".join(buffer)
        buffer.clear()
        for learning in claims_in(paragraph, under):
            learnings.append(learning)
            if len(learnings) >= most:
                return True
        return False

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if flush(heading):
                return learnings
            heading = stripped.lstrip("#").strip() or heading
            continue
        if not stripped or stripped.startswith(("```", "|", "    ")):
            # Blank lines end a paragraph; fenced code and tables are not prose.
            if flush(heading):
                return learnings
            continue
        buffer.append(stripped)
    flush(heading)
    return learnings


#: Words in a path or a document that place it in a discipline. A document
#: that matches nothing is filed under content creation rather than dropped —
#: an unclassified claim is still a claim.
HINTS: tuple[tuple[str, Discipline], ...] = (
    ("colour", Discipline.COLOR_THEORY),
    ("color", Discipline.COLOR_THEORY),
    ("grade", Discipline.COLOR_THEORY),
    ("music", Discipline.MUSIC_THEORY),
    ("beat", Discipline.MUSIC_THEORY),
    ("audio", Discipline.MUSIC_THEORY),
    ("sound", Discipline.MUSIC_THEORY),
    ("compos", Discipline.ART_BASICS),
    ("frame", Discipline.CINEMATOGRAPHY),
    ("camera", Discipline.CINEMATOGRAPHY),
    ("shot", Discipline.CINEMATOGRAPHY),
    ("cut", Discipline.MOVIE_MAKING),
    ("edit", Discipline.MOVIE_MAKING),
    ("hook", Discipline.PSYCHOLOGY),
    ("retention", Discipline.PSYCHOLOGY),
    ("attention", Discipline.PSYCHOLOGY),
    ("algorithm", Discipline.PLATFORM_ALGORITHM),
    ("analytic", Discipline.ANALYTICS),
    ("metric", Discipline.ANALYTICS),
    ("schedul", Discipline.SCHEDULING),
    ("post", Discipline.SCHEDULING),
    ("accessib", Discipline.ACCESSIBILITY),
    ("web", Discipline.WEB_DESIGN),
    ("app", Discipline.APP_DEVELOPMENT),
    ("shopify", Discipline.ECOMMERCE),
)


def _disciplines_for(path: Path, text: str) -> list[Discipline]:
    haystack = f"{path} {text[:2000]}".lower()
    found = [discipline for word, discipline in HINTS if word in haystack]
    # Deduplicate, keep order, and cap: a document about everything is filed
    # under everything, which makes the gap counter useless.
    seen, out = set(), []
    for discipline in found:
        if discipline not in seen:
            seen.add(discipline)
            out.append(discipline)
    return out[:3] or [Discipline.CONTENT_CREATION]


def measure_film(path: Path) -> list[Learning]:
    """Measure a film, and record what it does as numbers the crew can be held to.

    The same measurement the benchmark uses, so a film studied here and a film
    set as a target are described in one vocabulary rather than two.
    """
    from ..insight.reference import measure
    from ..insight.score import timeline_of
    from ..vision import read_asset

    try:
        style = measure([path])
        edl = timeline_of(path)
        reading = read_asset(path, samples=7)
    except Exception as exc:  # noqa: BLE001 - unreadable media is a skipped file
        log.info("could not measure %s: %s", path.name, exc)
        return []

    if style.is_empty or edl.duration <= 0:
        return []

    # Keyed on what the file *is*, not where it sits. The same reel saved under
    # two names is one film, and counting it twice quietly doubles its vote in
    # every median the crew is held against — which happened: two of twelve
    # studied files were byte-identical copies of two others.
    fingerprint = _content_id(path)

    def note(technique, insight, application, disciplines, **numbers) -> Learning:
        return Learning(
            learning_id=_stable_id("film", fingerprint, technique),
            disciplines=list(disciplines),
            insight=insight,
            technique=technique,
            application=application,
            source_video_id=f"file:{path.name}",
            # Each film is its own source. Corroboration counts *channels*, and
            # filing every reel under the folder it happens to sit in made
            # sixteen films by sixteen creators into one voice — so "these all
            # cut fast" could never corroborate, and every film learning stayed
            # TENTATIVE forever however many reels were measured.
            #
            # Documents are the opposite case and keep the folder: a project's
            # own notes agreeing with a project's own notes is not two people
            # agreeing, it is one document written twice.
            source_channel=f"film:{fingerprint[:12]}",
            source_title=path.name,
            source_end_sec=edl.duration,
            confidence=Confidence.TENTATIVE,
            # The numbers, not just the sentence about them. Holding the crew
            # to a measurement means comparing floats, not parsing English.
            measurements={k: round(float(v), 4) for k, v in numbers.items()},
        )

    learnings = []
    # A film the detector found no cuts in is a single take, or a failed
    # detection. Either way it is not a *cutting rate* to be held against:
    # recorded as 0.0 it drags the median toward zero and quietly excuses the
    # crew from cutting at all. One of the twelve reels studied did this.
    if style.cuts_per_10s > 0:
        learnings.append(
            note(
                "cutting rate",
                f"{path.name} cuts {style.cuts_per_10s:.1f} times per ten seconds "
                f"({style.shot_seconds:.3f}s median shot, shortest {style.shortest_shot:.3f}s)",
                "hold the crew's cutting rate against this rather than against a preference",
                [Discipline.MOVIE_MAKING, Discipline.PATTERN_RECOGNITION],
                cuts_per_10s=style.cuts_per_10s,
                shot_seconds=style.shot_seconds,
                shortest_shot=style.shortest_shot,
            )
        )
        learnings.append(
            note(
                "how long before the first cut",
                f"{path.name} holds its opening shot {style.first_cut:.2f}s before cutting",
                "the opening hold is the hook's whole budget",
                [Discipline.PSYCHOLOGY, Discipline.CONTENT_CREATION],
                first_cut=style.first_cut,
            )
        )
    learnings += [
        note(
            "exposure and palette",
            f"{path.name} runs at luma {style.luma:.2f}, contrast {style.contrast:.2f}, "
            f"{reading.hue_spread:.0f}° of hue spread, "
            f"{reading.clipped_black:.0%} of the frame at true black",
            "match the grade to this rather than to a preset name",
            [Discipline.COLOR_THEORY, Discipline.CINEMATOGRAPHY],
            luma=style.luma,
            contrast=style.contrast,
            hue_spread=reading.hue_spread,
            clipped_black=reading.clipped_black,
        ),
        note(
            "camera movement",
            f"{path.name} measures {style.motion:.3f} inter-frame motion — "
            + ("handheld or moving" if style.motion > 0.12 else "largely locked off"),
            "a move added to footage that does not move reads as a filter",
            [Discipline.CINEMATOGRAPHY],
            motion=style.motion,
        ),
    ]
    return learnings


def _measured(store, key: str) -> list[tuple[float, str]]:
    """Every measured value for one property, with the film it came from."""
    out: list[tuple[float, str]] = []
    for learning in store.all():
        value = (learning.measurements or {}).get(key)
        if isinstance(value, (int, float)):
            out.append((float(value), learning.source_title))
    return out


def critique_technique(edl, store) -> list:
    """Hold what the crew actually did against what the studied films do.

    This is the part that makes studying worth doing. The Scholar reads and
    measures; without this it accumulates. Here it compares — the crew's own
    finished timeline against the numbers taken off the films it was given,
    and says where they differ and by how much.

    Deliberately only about properties that were *measured*. There is no
    finding here that says "this feels slow": every one of them names a number,
    the number it is being held against, and the film that number came from.
    Anything the Scholar has only read about in prose stays out, because a
    sentence from a blog is not a yardstick.
    """
    from .review import ReviewFinding

    findings: list[ReviewFinding] = []
    shots = list(edl.shots)
    if not shots or edl.duration <= 0:
        return findings

    def median(values: list[float]) -> float:
        ordered = sorted(values)
        middle = len(ordered) // 2
        if not ordered:
            return 0.0
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2.0

    # -- cutting rate ------------------------------------------------------
    studied = _measured(store, "cuts_per_10s")
    if studied:
        ours = len(shots) / (edl.duration / 10.0)
        target = median([value for value, _ in studied])
        source = min(studied, key=lambda pair: abs(pair[0] - target))[1]
        if target > 0 and ours < target * 0.6:
            findings.append(
                ReviewFinding(
                    category="pacing",
                    description=(
                        f"This cuts {ours:.1f} times per ten seconds. The films studied "
                        f"cut {target:.1f} times — {source} among them. That is not a "
                        f"stylistic gap, it is {target / max(ours, 0.01):.1f}x."
                    ),
                    suggestion=(
                        f"Shorten shots toward a {10.0 / max(target, 0.1):.2f}s median. "
                        "The floor is two frames, so the rate is reachable."
                    ),
                    severity="important",
                    discipline=Discipline.MOVIE_MAKING,
                )
            )
        elif target > 0 and ours > target * 1.9:
            findings.append(
                ReviewFinding(
                    category="pacing",
                    description=(
                        f"This cuts {ours:.1f} times per ten seconds against {target:.1f} "
                        f"in the studied films. Faster than the thing being chased."
                    ),
                    suggestion="Let some shots hold — a cut that surprises nobody is a tic.",
                    severity="opportunity",
                    discipline=Discipline.MOVIE_MAKING,
                )
            )

    # -- the opening hold --------------------------------------------------
    studied = _measured(store, "first_cut")
    if studied:
        target = median([value for value, _ in studied])
        source = min(studied, key=lambda pair: abs(pair[0] - target))[1]
        ours = shots[0].duration
        if ours > max(target * 2.5, target + 0.6):
            findings.append(
                ReviewFinding(
                    category="hook",
                    description=(
                        f"The opening shot holds {ours:.2f}s before the first cut. The "
                        f"studied films cut at {target:.2f}s — {source} among them."
                    ),
                    suggestion=(
                        "Cut the opening shot shorter, or open on a later moment of it. "
                        "The hook's budget is the time before somebody scrolls."
                    ),
                    severity="important",
                    shot_indices=[0],
                    discipline=Discipline.PSYCHOLOGY,
                )
            )

    # -- camera movement ---------------------------------------------------
    studied = _measured(store, "motion")
    if studied:
        target = median([value for value, _ in studied])
        moving = [shot for shot in shots if shot.motion.kind != "none"]
        if target < 0.06 and len(moving) > len(shots) * 0.7:
            findings.append(
                ReviewFinding(
                    category="camera",
                    description=(
                        f"{len(moving)} of {len(shots)} shots carry a camera move, while "
                        f"the studied films measure {target:.3f} inter-frame motion — "
                        "close to locked off throughout."
                    ),
                    suggestion=(
                        "Take the move off most shots. A push added to footage that does "
                        "not move reads as a filter rather than as camera work."
                    ),
                    severity="opportunity",
                    discipline=Discipline.CINEMATOGRAPHY,
                )
            )

    # -- exposure ----------------------------------------------------------
    studied = _measured(store, "clipped_black")
    if studied:
        target = median([value for value, _ in studied])
        if target > 0.45 and edl.look.strength < 0.5:
            findings.append(
                ReviewFinding(
                    category="grade",
                    description=(
                        f"The studied films run {target:.0%} of the frame at true black. "
                        f"This is graded at strength {edl.look.strength:.2f}, which will "
                        "not get near that."
                    ),
                    suggestion=(
                        "Crush the blacks further. That much shadow is a lighting choice "
                        "with a century behind it, not clipping to be avoided."
                    ),
                    severity="opportunity",
                    discipline=Discipline.COLOR_THEORY,
                )
            )

    return findings
