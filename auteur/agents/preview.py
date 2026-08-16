"""Letting the crew see its own work.

Until now the agents argued about a film none of them had looked at. They had
readings of the *source* — the raw photographs, ungraded, uncropped — and they
had a structural score computed from shot lengths and text timings. Between
those two sits the entire question of what the edit actually looks like, and
nothing in the crew could see it.

That is a strange blindness in a program that grades footage. An agent could
propose a look, watch the structural score not move (because a grade changes no
shot length), and have no way to discover that it had turned every frame
magenta. The rehearsal loop found exactly that failure by rendering; the crew
could not, because it never rendered anything until the end.

So: three states, and the crew can see all of them.

  **source**      the photograph as it arrived, before anything was done to it
  **baseline**    the first cut, as the director left it
  **candidate**   the same film with one proposal applied

Each is a real render, measured with the same eye and scored on the same craft
yardstick as the benchmark. A proposal that improves the structure and wrecks
the picture is now visible as exactly that, and can be turned down for it.

**What it costs.** A proof is three shots at 270 by 480 — about three seconds.
Only proposals that change the picture pay it: a grade, a texture, a letterbox,
an overlay, a reframe. Retiming a shot cannot alter how a frame looks, so it is
scored structurally as before and costs nothing. Proofs are cached against a
fingerprint of the things that actually affect pixels, so two candidates that
differ only in timing share one render.

**What a proof is not.** It is small, short and fast, which is the point and
also the limit. It is a fair sample of the grade and the framing. It is not the
finished film, and a proof that looks fine is not a promise that the master
will.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("auteur.agents.preview")

#: How much of the film a proof covers. Enough shots to catch a grade that only
#: falls apart on one of them, few enough to stay under a few seconds.
PROOF_SHOTS = 3
PROOF_WIDTH = 270
PROOF_HEIGHT = 480
PROOF_SECONDS = 0.8

#: Fields that change what a frame looks like. A proposal touching none of these
#: cannot change the picture, so it never triggers a render.
VISUAL = ("look", "texture", "letterbox", "graphics", "reframe", "motion")


@dataclass
class Proof:
    """One rendered look at a film, and what the eye made of it."""

    label: str
    path: Path | None
    reading: object | None = None
    craft: object | None = None
    seconds: float = 0.0

    @property
    def score(self) -> float:
        return float(getattr(self.craft, "overall", 0.0))

    def describe(self) -> str:
        if self.craft is None:
            return f"{self.label}: could not be measured"
        return f"{self.label}: {self.craft.describe()}"


@dataclass
class Comparison:
    """The three states, side by side, with a verdict."""

    source: Proof | None = None
    baseline: Proof | None = None
    candidate: Proof | None = None

    @property
    def gain(self) -> float:
        """How much craft the candidate adds over the baseline."""
        if self.baseline is None or self.candidate is None:
            return 0.0
        return self.candidate.score - self.baseline.score

    @property
    def better(self) -> bool:
        return self.gain > 0.0

    def describe(self) -> str:
        lines = []
        for proof in (self.source, self.baseline, self.candidate):
            if proof is not None:
                lines.append("    " + proof.describe())
        if self.baseline is not None and self.candidate is not None:
            way = "better" if self.gain > 0 else "worse"
            lines.append(f"    the change makes the picture {way} by {abs(self.gain):.3f}")
        return "\n".join(lines)

    def to_json(self) -> dict:
        def one(proof: Proof | None) -> dict | None:
            if proof is None:
                return None
            return {
                "label": proof.label,
                "craft": proof.craft.to_json() if proof.craft is not None else None,
                "seconds": round(proof.seconds, 2),
            }

        return {
            "source": one(self.source),
            "baseline": one(self.baseline),
            "candidate": one(self.candidate),
            "gain": round(self.gain, 4),
        }


def picture_fingerprint(edl) -> str:
    """A hash of everything that changes a pixel, and nothing that does not.

    Two candidates differing only in when a cut lands produce the same picture,
    so they share a proof. That is most of what makes this affordable: a run
    proposing thirteen changes usually renders four or five distinct looks.
    """
    from dataclasses import asdict

    parts: list = [
        asdict(edl.look),
        round(edl.texture, 3),
        round(edl.letterbox, 3),
        edl.width,
        edl.height,
    ]
    for shot in edl.shots[:PROOF_SHOTS]:
        parts.append(
            [
                str(shot.source),
                asdict(shot.look),
                shot.reframe,
                shot.motion.kind,
                round(shot.motion.intensity, 3),
                [round(v, 3) for v in shot.motion.anchor],
            ]
        )
    for cue in edl.graphics:
        parts.append([cue.kind, round(cue.start, 2), [round(v, 3) for v in cue.anchor]])
    blob = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.blake2b(blob.encode(), digest_size=12).hexdigest()


def changes_the_picture(before, after) -> bool:
    """Did this proposal touch anything a camera would notice?"""
    return picture_fingerprint(before) != picture_fingerprint(after)


class Previewer:
    """Renders small proofs so the crew can look at what it is proposing."""

    def __init__(self, *, workspace: Path | None = None, enabled: bool = True, budget: int = 14):
        self.enabled = enabled
        self.root = Path(workspace) if workspace else Path(tempfile.mkdtemp(prefix="auteur-proof-"))
        self.root.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, Proof] = {}
        # A ceiling on how many renders one run may spend. Without it a crew
        # with many picture-changing proposals could quietly turn a two minute
        # job into ten.
        self.budget = budget
        self.spent = 0

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.budget

    def source_proof(self, sources) -> Proof:
        """What the footage looked like before anybody touched it.

        Read directly from the files rather than rendered, because that is the
        honest baseline: whatever the camera produced, with none of this
        program's decisions in it.
        """
        from ..insight.benchmark import craft_score
        from ..vision import read_asset
        from ..vision.connoisseur import _consensus

        started = time.perf_counter()
        readings = []
        for path in list(sources)[:PROOF_SHOTS]:
            try:
                readings.append(read_asset(path, samples=3))
            except (ValueError, OSError) as exc:
                log.debug("could not read %s: %s", path, exc)
        if not readings:
            return Proof(label="source", path=None)
        reading = _consensus(readings) if len(readings) > 1 else readings[0]
        return Proof(
            label="source",
            path=None,
            reading=reading,
            craft=craft_score(reading),
            seconds=time.perf_counter() - started,
        )

    def proof(self, edl, label: str) -> Proof:
        """Render a short, small look at this film and measure it."""
        if not self.enabled:
            return Proof(label=label, path=None)

        key = picture_fingerprint(edl)
        cached = self._cache.get(key)
        if cached is not None:
            return Proof(label=label, path=cached.path, reading=cached.reading, craft=cached.craft)
        if self.exhausted:
            log.info("preview budget spent; %s judged on structure alone", label)
            return Proof(label=label, path=None)

        started = time.perf_counter()
        proof = self._render(edl, key, label)
        proof.seconds = time.perf_counter() - started
        self.spent += 1
        self._cache[key] = proof
        return proof

    def _render(self, edl, key: str, label: str) -> Proof:
        import copy

        from ..config import DeliveryFormat, QUALITIES, Settings, Workspace
        from ..insight.benchmark import craft_score
        from ..render import render
        from ..vision import read_asset

        small = copy.deepcopy(edl)
        small.shots = small.shots[:PROOF_SHOTS]
        if not small.shots:
            return Proof(label=label, path=None)
        for shot in small.shots:
            # Shorten by moving the out point, so the grade and the framing are
            # untouched — a proof has to be the same picture, only briefer.
            shot.end = shot.start + min(PROOF_SECONDS, max(0.2, shot.duration))
        # Text and sound cannot change how a frame is graded, and rendering type
        # into a 270px proof only costs time.
        small.texts = []
        small.sfx = []
        small.width, small.height = PROOF_WIDTH, PROOF_HEIGHT
        try:
            small.repair()
        except ValueError:
            return Proof(label=label, path=None)

        folder = self.root / key
        try:
            result = render(
                small,
                Workspace(root=folder),
                Settings(
                    quality=QUALITIES["draft"],
                    primary_format=DeliveryFormat("proof", PROOF_WIDTH, PROOF_HEIGHT, "proof"),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - a failed proof is not a failed film
            log.info("could not render a proof of %s: %s", label, exc)
            return Proof(label=label, path=None)

        video = result.primary
        if video is None or not Path(video).exists():
            return Proof(label=label, path=None)
        try:
            reading = read_asset(video, samples=4)
        except (ValueError, OSError) as exc:
            log.info("could not read the proof of %s: %s", label, exc)
            return Proof(label=label, path=video)
        return Proof(label=label, path=video, reading=reading, craft=craft_score(reading))

    def compare(self, before, after, *, sources=None) -> Comparison:
        """The three states of one decision, measured."""
        comparison = Comparison()
        if sources:
            comparison.source = self.source_proof(sources)
        comparison.baseline = self.proof(before, "baseline")
        comparison.candidate = self.proof(after, "candidate")
        return comparison

    def sweep(self) -> None:
        """Throw the proofs away. They are evidence for one decision, not art."""
        shutil.rmtree(self.root, ignore_errors=True)


@dataclass
class NullPreviewer:
    """A previewer that renders nothing, for callers who do not want the cost."""

    enabled: bool = False
    spent: int = 0
    _cache: dict = field(default_factory=dict)

    @property
    def exhausted(self) -> bool:
        return True

    def source_proof(self, sources) -> Proof:
        return Proof(label="source", path=None)

    def proof(self, edl, label: str) -> Proof:
        return Proof(label=label, path=None)

    def compare(self, before, after, *, sources=None) -> Comparison:
        return Comparison()

    def sweep(self) -> None:
        return None
