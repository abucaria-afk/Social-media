"""A film to beat, and a yardstick honest enough to tell you whether you did.

Point at a reel you want the work to reach and then surpass. This measures it —
how it is cut, and how it looks — and keeps that as a target every later run is
scored against.

**Why this needed a second score.** The reel that prompted it scores 0.417 on
the existing model and misses all three objectives, while being visibly a much
better film than anything this program had produced. That is not a verdict on
the reel. It is the model admitting what it measures: shot lengths, when text
lands, whether the ending returns to the start. Structure. It has nothing at all
to say about whether the picture is any good, so a film could beat it by being
better organised and worse to look at, which is the opposite of the point.

So a benchmark carries two numbers.

**Structure** is the existing prediction — hook, share, loop — reconstructed
from the finished file with `timeline_of`. Same yardstick as our own output, so
the comparison is like for like.

**Craft** is measured from the frames by `auteur.vision`, and it is where the
target actually wins: depth separation 0.735 against our 0.191, hue spread 28°
against our 77°. Those are not opinions, they are the difference between a sharp
subject against a soft ground and a flat snapshot, and between a graded palette
and whatever the camera happened to see.

**What craft is not.** It is not beauty and does not claim to be. Four
measurable properties that good footage tends to have stand in for a quality
nobody can measure, and a film can score well here and still be boring, badly
composed or about nothing. What it does catch is the specific way a program like
this one goes wrong: optimising a structural score until the numbers are lovely
and the film is ugly.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("auteur.insight.benchmark")

#: How much each craft measure counts. Separation leads because it is what
#: "cinematic" mostly means in practice, and because it was the largest measured
#: gap between the target and our own work by a factor of nearly four.
CRAFT_WEIGHTS = {"separation": 0.40, "subject": 0.25, "palette": 0.20, "exposure": 0.15}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


@dataclass
class CraftScore:
    """How the picture reads, from four things that can actually be measured."""

    #: A sharp subject against a soft ground. The depth cue that separates a
    #: photograph from a snapshot, and the biggest gap to the target.
    separation: float = 0.0
    #: Is there one place for the eye to go, or is the frame a field.
    subject: float = 0.0
    #: Palette discipline. A narrow hue spread reads as graded; a wide one reads
    #: as whatever the camera saw.
    palette: float = 0.0
    #: Neither crushed nor blown, with real tonal range in between.
    exposure: float = 0.0

    @property
    def overall(self) -> float:
        return (
            self.separation * CRAFT_WEIGHTS["separation"]
            + self.subject * CRAFT_WEIGHTS["subject"]
            + self.palette * CRAFT_WEIGHTS["palette"]
            + self.exposure * CRAFT_WEIGHTS["exposure"]
        )

    @property
    def weakest(self) -> tuple[str, float]:
        parts = {
            "separation": self.separation,
            "subject": self.subject,
            "palette": self.palette,
            "exposure": self.exposure,
        }
        name = min(parts, key=lambda key: parts[key])
        return name, parts[name]

    def to_json(self) -> dict:
        return {
            "separation": round(self.separation, 4),
            "subject": round(self.subject, 4),
            "palette": round(self.palette, 4),
            "exposure": round(self.exposure, 4),
            "overall": round(self.overall, 4),
        }

    def describe(self) -> str:
        return (
            f"craft {self.overall:.2f}  "
            f"(separation {self.separation:.2f}, subject {self.subject:.2f}, "
            f"palette {self.palette:.2f}, exposure {self.exposure:.2f})"
        )


def craft_score(reading) -> CraftScore:
    """Score a reading of the frames on the four craft measures.

    Every threshold here is a judgement, and every one of them is stated rather
    than buried: 30 degrees of hue spread reads as a graded palette and 90 as
    none, mid-grey sits around 0.30 for footage of this kind, and a frame with
    no local contrast at all is flat however well it is exposed.
    """
    # Separation is already 0..1 and means the right thing directly.
    separation = _clamp(reading.depth_separation)

    # Focus strength runs low even on good footage — 0.29 on the target — so it
    # is scaled against what a strong subject actually measures rather than
    # against a theoretical 1.0 nothing reaches.
    subject = _clamp(reading.focus_strength / 0.35)

    # Hue spread: under 30 degrees is a palette somebody chose, over 90 is a
    # palette nobody did.
    palette = _clamp((90.0 - reading.hue_spread) / 60.0)

    # Exposure wants headroom at both ends and real range in between. Crushed
    # blacks and blown highlights both lose the picture, and a platform's
    # compression finds a very dark frame hard.
    luma = reading.luma
    if luma < 0.10 or luma > 0.80:
        headroom = 0.0
    else:
        # Peaks around 0.30, which is where cinematic footage of this kind sits.
        headroom = _clamp(1.0 - abs(luma - 0.30) / 0.45)
    tonal = _clamp(reading.contrast / 0.22)
    exposure = _clamp(headroom * 0.6 + tonal * 0.4)

    return CraftScore(separation=separation, subject=subject, palette=palette, exposure=exposure)


@dataclass
class Benchmark:
    """A film worth beating, measured on both yardsticks."""

    name: str
    source: str
    #: Structural prediction, from the same model that scores our own edits.
    structure: float = 0.0
    hook: float = 0.0
    share: float = 0.0
    loop: float = 0.0
    craft: CraftScore = field(default_factory=CraftScore)
    #: How it was cut, for the style agent.
    cuts_per_10s: float = 0.0
    shot_seconds: float = 0.0
    seconds: float = 0.0
    shots: int = 0
    composition: str = ""
    lighting: str = ""
    added_at: float = field(default_factory=time.time)

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "structure": round(self.structure, 4),
            "hook": round(self.hook, 4),
            "share": round(self.share, 4),
            "loop": round(self.loop, 4),
            "craft": self.craft.to_json(),
            "cuts_per_10s": round(self.cuts_per_10s, 2),
            "shot_seconds": round(self.shot_seconds, 3),
            "seconds": round(self.seconds, 2),
            "shots": self.shots,
            "composition": self.composition,
            "lighting": self.lighting,
            "added_at": self.added_at,
        }

    @classmethod
    def from_json(cls, data: dict) -> Benchmark:
        craft = data.get("craft") or {}
        return cls(
            name=data["name"],
            source=data.get("source", ""),
            structure=data.get("structure", 0.0),
            hook=data.get("hook", 0.0),
            share=data.get("share", 0.0),
            loop=data.get("loop", 0.0),
            craft=CraftScore(
                separation=craft.get("separation", 0.0),
                subject=craft.get("subject", 0.0),
                palette=craft.get("palette", 0.0),
                exposure=craft.get("exposure", 0.0),
            ),
            cuts_per_10s=data.get("cuts_per_10s", 0.0),
            shot_seconds=data.get("shot_seconds", 0.0),
            seconds=data.get("seconds", 0.0),
            shots=data.get("shots", 0),
            composition=data.get("composition", ""),
            lighting=data.get("lighting", ""),
            added_at=data.get("added_at", 0.0),
        )

    def describe(self) -> str:
        return (
            f"{self.name}  ·  {self.seconds:.0f}s, {self.shots} shots, "
            f"{self.cuts_per_10s:.0f} cuts/10s\n"
            f"    structure {self.structure:.2f}  "
            f"(hook {self.hook:.2f}, share {self.share:.2f}, loop {self.loop:.2f})\n"
            f"    {self.craft.describe()}\n"
            f"    {self.composition} · {self.lighting}"
        )


def measure_benchmark(path: str | Path, *, name: str = "", model=None) -> Benchmark:
    """Watch a film and record what it would take to beat it."""
    from ..vision import read_asset
    from .reference import measure
    from .score import fit, predict, timeline_of

    file = Path(path)
    if model is None:
        from . import corpus

        model = fit(corpus([], simulate_rows=1500))

    edl = timeline_of(file)
    prediction = predict(edl, model)
    reading = read_asset(file, samples=11)
    style = measure([file])

    return Benchmark(
        name=name or file.stem[:24],
        source=str(file),
        structure=prediction.overall,
        hook=prediction.hook.score,
        share=prediction.share.score,
        loop=prediction.loop.score,
        craft=craft_score(reading),
        cuts_per_10s=style.cuts_per_10s,
        shot_seconds=style.shot_seconds,
        seconds=edl.duration,
        shots=len(edl.shots),
        composition=reading.composition,
        lighting=reading.lighting,
    )


@dataclass
class Standing:
    """Where a piece of work stands against the film it is chasing."""

    benchmark: Benchmark
    structure: float
    craft: CraftScore

    @property
    def beats_structure(self) -> bool:
        return self.structure > self.benchmark.structure

    @property
    def beats_craft(self) -> bool:
        return self.craft.overall > self.benchmark.craft.overall

    @property
    def surpassed(self) -> bool:
        """Both, or it does not count.

        Deliberately an *and*. Either alone is the failure mode worth guarding
        against: better structure and worse pictures is a program gaming its own
        score, and better pictures with worse structure is footage carrying an
        edit that did not earn it.
        """
        return self.beats_structure and self.beats_craft

    def describe(self) -> str:
        def line(label: str, ours: float, theirs: float) -> str:
            gap = ours - theirs
            # Rounded to the two decimals actually shown, so a gap that prints
            # as 0.00 does not also print as "behind by 0.00".
            if round(gap, 2) == 0:
                return f"    {label:10} {ours:.2f} vs {theirs:.2f}   level"
            mark = "ahead" if gap > 0 else "behind"
            return f"    {label:10} {ours:.2f} vs {theirs:.2f}   {mark} by {abs(gap):.2f}"

        lines = [f"against {self.benchmark.name}:"]
        lines.append(line("structure", self.structure, self.benchmark.structure))
        lines.append(line("craft", self.craft.overall, self.benchmark.craft.overall))
        lines.append(line("  separation", self.craft.separation, self.benchmark.craft.separation))
        lines.append(line("  subject", self.craft.subject, self.benchmark.craft.subject))
        lines.append(line("  palette", self.craft.palette, self.benchmark.craft.palette))
        lines.append(line("  exposure", self.craft.exposure, self.benchmark.craft.exposure))
        if self.surpassed:
            lines.append("    surpassed on both counts")
        else:
            name, _ = self.craft.weakest
            lines.append(f"    not yet — the weakest thing about the picture is {name}")
        return "\n".join(lines)


class Benchmarks:
    """The films being chased, kept between runs."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else self.default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.entries: dict[str, Benchmark] = {}
        if self.path.exists():
            self._load()

    @staticmethod
    def default_path() -> Path:
        return Path.home() / ".auteur" / "benchmarks.json"

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("could not read the benchmarks: %s", exc)
            return
        for item in data:
            try:
                benchmark = Benchmark.from_json(item)
            except (KeyError, TypeError, ValueError) as exc:
                log.debug("skipping an unreadable benchmark: %s", exc)
                continue
            self.entries[benchmark.name] = benchmark

    def save(self) -> None:
        self.path.write_text(
            json.dumps([b.to_json() for b in self.entries.values()], indent=2), encoding="utf-8"
        )

    def add(self, benchmark: Benchmark) -> Benchmark:
        self.entries[benchmark.name] = benchmark
        self.save()
        return benchmark

    def remove(self, name: str) -> bool:
        if name in self.entries:
            del self.entries[name]
            self.save()
            return True
        return False

    @property
    def hardest(self) -> Benchmark | None:
        """The one to chase: whichever is furthest ahead on craft.

        Craft rather than structure, because structure is the half this program
        was already good at and craft is the half it was not measuring at all.
        """
        if not self.entries:
            return None
        return max(self.entries.values(), key=lambda b: b.craft.overall)

    def standing(self, reading, structure: float, *, against: str = "") -> Standing | None:
        """Where a finished piece stands against a benchmark."""
        target = self.entries.get(against) if against else self.hardest
        if target is None:
            return None
        return Standing(benchmark=target, structure=structure, craft=craft_score(reading))

    def describe(self) -> str:
        if not self.entries:
            return "nothing to beat yet — add a film with `auteur benchmark add <video>`"
        lines = [f"{len(self.entries)} film(s) to beat, hardest first:"]
        for benchmark in sorted(self.entries.values(), key=lambda b: -b.craft.overall):
            lines.append("")
            lines.append("  " + benchmark.describe().replace("\n", "\n  "))
        return "\n".join(lines)
