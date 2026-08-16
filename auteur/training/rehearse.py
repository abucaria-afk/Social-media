"""Rehearsal: make a film, measure it against the target, change something, again.

The crew argues about one edit for three rounds and stops. That is the right
shape for someone waiting on a render, and the wrong shape for getting better at
this. Getting better needs the other loop — build, measure, change, rebuild —
run far more times than anybody would sit through.

So this runs it. Given footage and a benchmark, it renders a candidate, scores
it on both yardsticks, mutates the settings that produced it, and keeps whatever
wins. It does not stop when it beats the target; it raises the bar to what it
just achieved and carries on, which is the only way a target stays useful after
the first time it is passed.

**What it can and cannot move.** It searches the space of decisions this program
actually makes: the grade, the pace, the letterbox, the texture, how much is
drawn on top. It cannot make the footage better. If the source has no depth
separation, no amount of rehearsal invents any — and the loop will say so rather
than grinding away at a number it has no lever for.

**Why the result is not a treadmill.** Every generation is written to the
recipe, so the next ordinary `auteur workflow run` on similar footage starts
from the best settings found rather than from the defaults. The rehearsal is
where the search happens; a normal run is where the answer gets used.

**On mutation.** Deliberate small random changes to a handful of numbers, keeping
what scores better — hill climbing with noise, not a metaphor. It has the
weaknesses of hill climbing: it can settle on a local best and sit there, which
is why the step size grows when nothing improves for a while and shrinks again
when something does.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..edl import MIN_SHOT

log = logging.getLogger("auteur.training.rehearse")

#: The knobs the loop is allowed to turn, and the range each may take. Only
#: things that change the finished picture and that this program can actually
#: set — there is no point mutating something the renderer ignores.
KNOBS: dict[str, tuple[float, float]] = {
    "exposure": (-0.45, 0.45),
    "temperature": (-0.5, 0.5),
    "saturation": (-0.5, 0.5),
    "contrast": (-0.3, 0.6),
    "strength": (0.0, 1.0),
    "texture": (0.0, 0.6),
    "letterbox": (0.0, 0.18),
    # The floor is the EDL's, not a rounder number. At 0.25 the loop could not
    # explore anything faster than four cuts a second, while the reference
    # reels this trains against have a median shot of 0.167s and a fastest of
    # 0.125s — so the cadence being chased was outside the search space and no
    # number of generations would have found it. Fifth place the same ceiling
    # has turned up: MIN_SHOT, the cut detector's refractory, MIN_SLOT, the
    # beat grid, and here.
    "shot_seconds": (MIN_SHOT, 3.0),
}

#: Named grades the loop may choose between. A preset moves the palette far
#: more than any single slider, so it is mutated as a choice rather than a dial.
PRESETS = (
    "neutral",
    "moody",
    "blockbuster",
    "steel",
    "amber",
    "kodak",
    "desert",
    "aqua",
    "bleach-bypass",
)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class Recipe:
    """One set of settings, and how it did."""

    preset: str = "moody"
    exposure: float = 0.0
    temperature: float = 0.0
    saturation: float = 0.0
    contrast: float = 0.0
    strength: float = 1.0
    texture: float = 0.0
    letterbox: float = 0.0
    shot_seconds: float = 1.4

    def mutated(self, rng: random.Random, *, step: float) -> Recipe:
        """A copy with a few knobs nudged.

        Two or three at a time rather than all of them: changing everything at
        once tells you the combination was better without telling you which part
        of it was, and the loop has to be able to walk back.
        """
        child = Recipe(**asdict(self))
        names = list(KNOBS)
        rng.shuffle(names)
        for name in names[: rng.randint(2, 3)]:
            low, high = KNOBS[name]
            span = (high - low) * step
            value = getattr(child, name) + rng.gauss(0.0, span)
            setattr(child, name, _clamp(value, low, high))
        # The preset changes rarely — it is the largest single move available,
        # so trying a new one every generation would drown out everything else.
        if rng.random() < 0.2:
            child.preset = rng.choice(PRESETS)
        return child


@dataclass
class Attempt:
    """One rendered candidate and what it scored."""

    generation: int
    recipe: Recipe
    structure: float = 0.0
    craft: float = 0.0
    separation: float = 0.0
    palette: float = 0.0
    exposure_score: float = 0.0
    beat_target: bool = False
    #: How close the finished cut lands to the benchmark's cutting rate, 0..1.
    #: 1.0 when there is no benchmark, so a rehearsal with nothing to chase is
    #: scored exactly as it was before this existed.
    cadence: float = 1.0
    seconds: float = 0.0

    @property
    def combined(self) -> float:
        """One number to climb.

        Craft leads, because structure was the half this program was already
        good at — it was ahead of the target there before the loop existed —
        and craft is the half it was losing by 0.28.

        Cadence is here because without it the loop had no reason to cut at
        the rate it was chasing. Craft measures the picture and structure
        measures the shape; neither rewards cutting six times a second, so
        forty generations against a 46-cuts-per-ten-seconds benchmark sat at a
        1.4s shot and climbed craft instead. The most distinctive property of
        the films being chased was not in the objective at all.
        """
        return self.craft * 0.5 + self.structure * 0.3 + self.cadence * 0.2

    def to_json(self) -> dict:
        return {
            "generation": self.generation,
            "recipe": asdict(self.recipe),
            "structure": round(self.structure, 4),
            "craft": round(self.craft, 4),
            "separation": round(self.separation, 4),
            "palette": round(self.palette, 4),
            "exposure_score": round(self.exposure_score, 4),
            "combined": round(self.combined, 4),
            "beat_target": self.beat_target,
            "cadence": round(self.cadence, 4),
            "seconds": round(self.seconds, 1),
        }


@dataclass
class Progress:
    """Where the rehearsal has got to."""

    generations: int = 0
    best: Attempt | None = None
    history: list[Attempt] = field(default_factory=list)
    #: How many times the bar has been raised after being passed.
    surpassed: int = 0
    stalled: int = 0

    def describe(self) -> str:
        if self.best is None:
            return "no generation has finished yet"
        best = self.best
        lines = [
            f"generation {self.generations}  ·  best combined {best.combined:.3f} "
            f"(craft {best.craft:.3f}, structure {best.structure:.3f}, "
            f"cadence {best.cadence:.3f})",
            f"    grade: {best.recipe.preset} "
            f"exp{best.recipe.exposure:+.2f} temp{best.recipe.temperature:+.2f} "
            f"sat{best.recipe.saturation:+.2f} con{best.recipe.contrast:+.2f}",
            f"    texture {best.recipe.texture:.2f}  letterbox {best.recipe.letterbox:.2f}  "
            f"shots {best.recipe.shot_seconds:.2f}s",
        ]
        if self.surpassed:
            lines.append(f"    target passed {self.surpassed}x — the bar moved each time")
        return "\n".join(lines)


class Rehearsal:
    """The loop: build, measure, mutate, repeat, and never call it finished."""

    def __init__(
        self,
        footage: list[Path],
        *,
        benchmark=None,
        workspace: Path | None = None,
        recipe_path: Path | None = None,
        seed: int = 0xB0A7,
        seconds: float = 8.0,
    ):
        self.footage = [Path(p) for p in footage]
        self.benchmark = benchmark
        self.workspace = Path(workspace) if workspace else Path.home() / ".auteur" / "rehearsal"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.recipe_path = Path(recipe_path) if recipe_path else self.default_recipe_path()
        self.rng = random.Random(seed)
        self.seconds = seconds
        self.progress = Progress()
        # Grows when nothing improves, shrinks when something does. A fixed step
        # either crawls forever or never settles.
        self.step = 0.12

    @staticmethod
    def default_recipe_path() -> Path:
        return Path.home() / ".auteur" / "best-recipe.json"

    # ------------------------------------------------------------------ recipe

    def load_recipe(self) -> Recipe:
        """The best settings found so far, or the defaults."""
        if not self.recipe_path.exists():
            return Recipe()
        try:
            data = json.loads(self.recipe_path.read_text(encoding="utf-8"))
            # Only the fields a Recipe actually has, so a file written by a
            # later version with an extra knob still loads here.
            saved = data.get("recipe", {})
            known = {name: saved[name] for name in Recipe().__dict__ if name in saved}
            return Recipe(**known)
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            log.info("could not read the recipe (%s) — starting from the defaults", exc)
            return Recipe()

    def save_recipe(self, attempt: Attempt) -> None:
        """Write the winner where an ordinary run will find it."""
        self.recipe_path.parent.mkdir(parents=True, exist_ok=True)
        self.recipe_path.write_text(
            json.dumps(
                {
                    "recipe": asdict(attempt.recipe),
                    "craft": round(attempt.craft, 4),
                    "structure": round(attempt.structure, 4),
                    "combined": round(attempt.combined, 4),
                    "generation": attempt.generation,
                    "found_at": time.time(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------- round

    def _build_and_score(self, recipe: Recipe, generation: int) -> Attempt | None:
        """Render one candidate small and fast, and measure what came out."""
        from ..config import FORMATS, QUALITIES, Settings, Workspace
        from ..edl import EditDecisionList, Look, Motion, Shot, Transition
        from ..insight import corpus, fit
        from ..insight.benchmark import craft_score
        from ..insight.score import predict
        from ..render import render
        from ..vision import read_asset

        started = time.perf_counter()
        count = max(1, len(self.footage))
        hold = _clamp(recipe.shot_seconds, *KNOBS["shot_seconds"])

        shots = [
            Shot(
                clip_id=f"R{index:02d}",
                source=path,
                start=0.0,
                end=hold,
                is_still=path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".heic"},
                motion=Motion("none", 0.0, (0.5, 0.5)),
                transition_in=Transition("cut", 0.0),
                look=Look(
                    preset=recipe.preset,
                    exposure=recipe.exposure,
                    temperature=recipe.temperature,
                    saturation=recipe.saturation,
                    contrast=recipe.contrast,
                    strength=recipe.strength,
                ),
            )
            for index, path in enumerate(self.footage)
        ]
        # Repeat the footage until the film is long enough to be worth scoring.
        while sum(s.duration for s in shots) < self.seconds and len(shots) < count * 6:
            source = shots[len(shots) % count]
            clone = Shot(**{**source.__dict__, "clip_id": f"R{len(shots):02d}"})
            shots.append(clone)

        edl = EditDecisionList(
            title=f"rehearsal-{generation:04d}",
            shots=shots,
            fps=24,
            width=540,
            height=960,
            look=Look(preset=recipe.preset, strength=recipe.strength),
            texture=recipe.texture,
            letterbox=recipe.letterbox,
        )
        try:
            edl.repair()
        except ValueError as exc:
            log.debug("generation %d produced no renderable film: %s", generation, exc)
            return None

        room = Workspace(root=self.workspace / f"gen-{generation:04d}")
        settings = Settings(quality=QUALITIES["draft"], primary_format=FORMATS["reel"])
        try:
            result = render(edl, room, settings)
        except Exception as exc:  # noqa: BLE001 - a failed candidate is data, not a crash
            log.info("generation %d failed to render: %s", generation, exc)
            return None

        video = result.primary
        if video is None or not Path(video).exists():
            return None

        try:
            reading = read_asset(video, samples=7)
            craft = craft_score(reading)
            structure = predict(edl, fit(corpus([], simulate_rows=400))).overall
            cadence = self._cadence_of(video)
        except Exception as exc:  # noqa: BLE001
            log.info("generation %d could not be measured: %s", generation, exc)
            return None
        finally:
            # Each generation is a whole render; keeping them all fills a disk
            # in an afternoon. The recipe is what carries forward, not the file.
            self._sweep(room.root)

        return Attempt(
            generation=generation,
            recipe=recipe,
            structure=structure,
            craft=craft.overall,
            separation=craft.separation,
            palette=craft.palette,
            exposure_score=craft.exposure,
            cadence=cadence,
            seconds=time.perf_counter() - started,
        )

    def _sweep(self, folder: Path) -> None:
        import shutil

        # `ignore_errors` already swallows everything this could raise; the
        # try/except that used to wrap it caught nothing.
        shutil.rmtree(folder, ignore_errors=True)

    def _cadence_of(self, video) -> float:
        """How close this cut lands to the benchmark's rate, 0..1.

        A ratio rather than a difference, because the same two cuts a second
        adrift means something different at 4 cuts per ten seconds than at 40.
        Measured off the rendered file by the same code that measured the
        benchmark, so the two numbers are the same kind of number.
        """
        target = getattr(self.benchmark, "cuts_per_10s", 0.0) if self.benchmark else 0.0
        if target <= 0:
            return 1.0
        from ..insight.reference import measure

        try:
            got = measure([video]).cuts_per_10s
        except Exception as exc:  # noqa: BLE001 - unmeasurable is not fatal
            log.debug("could not measure the cadence: %s", exc)
            return 1.0
        if got <= 0:
            return 0.0
        ratio = min(got, target) / max(got, target)
        return float(ratio)

    def generation(self) -> Attempt | None:
        """Run one: mutate from the best so far, render it, keep it if better."""
        index = self.progress.generations
        parent = self.progress.best.recipe if self.progress.best else self.load_recipe()

        # Try every grade once before tuning any of them. A preset is a discrete
        # choice that moves the palette further than every slider put together,
        # and hill climbing from whichever one happened to be the default will
        # spend its whole life polishing the wrong grade. Nine cheap renders buy
        # the right starting point.
        if index < len(PRESETS):
            recipe = Recipe(**asdict(parent))
            recipe.preset = PRESETS[index]
        else:
            recipe = parent.mutated(self.rng, step=self.step)

        attempt = self._build_and_score(recipe, index)
        self.progress.generations += 1
        if attempt is None:
            return None

        if self.benchmark is not None:
            # Cutting a quarter as fast as the film you are chasing is not
            # beating it, however good the frames look.
            attempt.beat_target = (
                attempt.craft > self.benchmark.craft.overall
                and attempt.structure > self.benchmark.structure
                and attempt.cadence > 0.75
            )

        self.progress.history.append(attempt)
        best = self.progress.best
        if best is None or attempt.combined > best.combined:
            self.progress.best = attempt
            self.save_recipe(attempt)
            # Something worked: search closer to it.
            self.step = max(0.05, self.step * 0.8)
            self.progress.stalled = 0
            if attempt.beat_target:
                self.progress.surpassed += 1
                self._raise_the_bar(attempt)
            log.info(
                "generation %d improved: craft %.3f structure %.3f",
                index,
                attempt.craft,
                attempt.structure,
            )
        else:
            # Nothing worked: look further afield.
            self.progress.stalled += 1
            if self.progress.stalled >= 4:
                self.step = min(0.45, self.step * 1.5)
                self.progress.stalled = 0
        return attempt

    def ceiling(self) -> str:
        """What is capping the score that no amount of rehearsal will move.

        A grade can move exposure and it can move palette. It cannot put depth
        into a frame that has none — separation comes from a lens and a distance,
        and if the footage was shot flat then every generation from here will
        trade a hundredth of a point back and forth and call it progress.

        Saying so is the difference between a loop that is searching and a loop
        that is just warm.
        """
        history = self.progress.history
        if len(history) < 8 or self.benchmark is None:
            return ""

        recent = history[-8:]
        spread = max(a.separation for a in recent) - min(a.separation for a in recent)
        behind = self.benchmark.craft.separation - recent[-1].separation
        if spread < 0.02 and behind > 0.15:
            return (
                f"separation is stuck at {recent[-1].separation:.2f} against "
                f"{self.benchmark.craft.separation:.2f} and has not moved in eight "
                "generations. No grade can fix that — it comes from a longer lens, a "
                "wider aperture, or standing closer. The rehearsal can keep tuning "
                "palette and exposure, and it will not close this gap."
            )
        return ""

    def _raise_the_bar(self, attempt: Attempt) -> None:
        """Once the target is passed, the target becomes what we just did.

        Without this the loop would beat the benchmark once and then have
        nothing left to climb toward, which is how a goal stops being useful the
        moment it is met.
        """
        if self.benchmark is None:
            return
        from ..insight.benchmark import CraftScore

        self.benchmark.structure = attempt.structure
        self.benchmark.craft = CraftScore(
            separation=attempt.separation,
            subject=self.benchmark.craft.subject,
            palette=attempt.palette,
            exposure=attempt.exposure_score,
        )
        log.info("target passed — the bar is now craft %.3f", attempt.craft)

    def run(self, *, generations: int = 20, stop=None, on_generation=None) -> Progress:
        """Rehearse, repeatedly. `stop()` ends it between generations."""
        for _ in range(max(1, generations)):
            if stop is not None and stop():
                break
            attempt = self.generation()
            if on_generation is not None and attempt is not None:
                on_generation(attempt, self.progress)
        return self.progress

    def forever(self, *, stop=None, on_generation=None, pause: float = 0.0) -> None:
        """Never finish. The loop is the point, not any one film it makes."""
        while not (stop is not None and stop()):
            try:
                self.generation()
                if on_generation is not None and self.progress.best is not None:
                    on_generation(self.progress.best, self.progress)
            except Exception:  # noqa: BLE001 - a bad generation must not end the loop
                log.debug("a generation failed", exc_info=True)
            if pause:
                time.sleep(pause)
