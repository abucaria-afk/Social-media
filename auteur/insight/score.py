"""Scoring a planned edit against the three objectives.

The brief names three, and they are not the same objective wearing different
hats — an edit can be excellent at one and actively bad at another, which is
why they are scored separately and reported separately.

**Hook.** Does the opening survive three seconds. Trained on the rows where
`three_second_watch_rate` clears 0.80: what those hooks have in common is a
short opening shot, movement inside the first frame, and text that lands before
the first cut rather than after it.

**Share.** Would anybody send this to somebody. Rewarded against a
`share_to_view_ratio` above 0.05. Shares are the signal that gets a post out of
its seed pool, and they come from completion and from having a reason — not
from a strong first second, which is why the hook objective cannot stand in
for it.

**Loop.** Does the end flow back into the beginning. A seamless loop turns one
view into two and doubles the completion the share objective feeds on.

The three are scored from the **edit itself** — shot lengths, where the text
lands, how the first and last frames relate — because that is what an agent can
change. A score here is a prediction about an edit, made by a model fitted to
`insight.dataset`, and it inherits every limitation of that corpus. Read
`FitReport.provenance` before believing a number.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from collections.abc import Sequence

from ..edl import EditDecisionList
from .schema import (
    CRAFT_FORMS,
    TARGET_LOOP_COUNT,
    TARGET_SHARE_TO_VIEW,
    TARGET_THREE_SECOND_WATCH,
    Signal,
)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _correlation(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Pearson r, or 0 when it cannot be computed.

    Returns 0 rather than raising for a constant column, which is exactly the
    case that matters here: a "winners only" export has constant labels, and a
    correlation against a constant is not weak evidence, it is none.
    """
    n = min(len(xs), len(ys))
    if n < 3:
        return 0.0
    mean_x, mean_y = _mean(xs[:n]), _mean(ys[:n])
    dx = [x - mean_x for x in xs[:n]]
    dy = [y - mean_y for y in ys[:n]]
    top = sum(a * b for a, b in zip(dx, dy, strict=False))
    left = math.sqrt(sum(a * a for a in dx))
    right = math.sqrt(sum(b * b for b in dy))
    if left < 1e-12 or right < 1e-12:
        return 0.0
    return top / (left * right)


#: Numeric columns worth correlating against the objectives, and what an editor
#: would actually do about each.
_DRIVERS: tuple[tuple[str, str], ...] = (
    ("pattern_interrupt_sec", "when the first cut lands"),
    ("pacing_cuts_per_10s", "cuts per ten seconds"),
    ("contrast_ratio", "contrast of the grade"),
    ("stop_scroll_ms", "how long the thumb stops"),
    ("thumbnail_ctr", "cover frame click-through"),
    ("tempo_bpm", "tempo of the bed"),
    ("audio_retention_sec", "how long the audio holds"),
    ("hook_duration", "length of the opening shot"),
)


@dataclass
class FitReport:
    """What the model learned, and from what."""

    rows: int
    simulated_rows: int
    measured_rows: int
    #: Mean three-second watch rate among the rows that cleared the threshold.
    elite_three_second: float = 0.0
    #: Hook styles ranked by mean three-second watch rate, best first.
    style_ranking: tuple[tuple[str, float], ...] = ()
    #: Seconds before the first cut, among the rows that cleared the bar.
    best_hook_duration: float = 0.0
    elite_share: float = 0.0
    elite_loop: float = 0.0
    #: (column, human name, r against the objective it drives), strongest first.
    drivers: tuple[tuple[str, str, float], ...] = ()
    #: Categorical winners: the best value of each descriptive column.
    best_of: dict[str, tuple[str, float]] = field(default_factory=dict)
    #: Choices that were offered but explain no more spread than chance. Worth
    #: printing: "your palette made no measurable difference" is a finding, and
    #: a much more useful one than a ranking of noise.
    no_signal: list[str] = field(default_factory=list)
    #: Rows the platform stopped pushing. The only real negative label there is.
    stalled_rows: int = 0
    forms: tuple[str, ...] = ()
    #: Drivers on which two exports disagree about the *direction* of the
    #: effect. The most useful thing in a multi-source corpus.
    conflicts: tuple[str, ...] = ()
    #: Exports whose columns correlate too neatly to be observations.
    generated_forms: tuple[str, ...] = ()
    #: The actual hook copy that performed, best first. The only part of this
    #: report a person can lift straight into a post.
    best_hooks: tuple[tuple[str, float], ...] = ()
    #: Exports whose numbers are the right shape but the wrong size.
    implausible: tuple[str, ...] = ()
    #: How many labelled wins and failures the corpus contains.
    wins: int = 0
    failures: int = 0
    #: objective -> (win median, fail median, the value between them). The only
    #: numbers here earned by comparison rather than by describing winners.
    separation: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    #: error state -> (how often, what the export says to do about it).
    failure_modes: tuple[tuple[str, int, str], ...] = ()
    #: UTC hours the labelled wins were scheduled into. Derived from the data
    #: rather than from folklore about the best time to post.
    optimal_hours: tuple[int, ...] = ()

    @property
    def has_negatives(self) -> bool:
        return self.stalled_rows > 0 or self.failures > 0

    @property
    def discriminative(self) -> bool:
        """Can this corpus tell a winner from a loser, or only describe winners?"""
        return self.wins >= 20 and self.failures >= 20

    @property
    def provenance(self) -> str:
        if self.measured_rows == 0:
            return (
                f"fitted on {self.rows} simulated rows and no measured ones — "
                "this predicts the simulator, not any platform"
            )
        if self.simulated_rows == 0:
            return (
                f"fitted on {self.measured_rows} measured rows across {len(self.forms)} export(s)"
            )
        return (
            f"fitted on {self.measured_rows} measured rows and "
            f"{self.simulated_rows} simulated ones — treat the numbers as a rehearsal"
        )

    @property
    def caveat(self) -> str:
        """The one thing most likely to make these numbers misleading."""
        if self.implausible:
            return (
                f"{self.implausible[0]} — the relationships may still be right, "
                "but the magnitudes are not from a real platform"
            )
        if not self.has_negatives and self.measured_rows:
            return (
                "no failures in this data — every row is a winner, so it can describe "
                "what success looks like but not what separates it from anything else"
            )
        return ""

    def describe(self) -> str:
        lines = [self.provenance]
        if self.caveat:
            lines += ["", f"⚠ {self.caveat}"]
        if self.style_ranking:
            lines += ["", "hook styles by three-second watch rate:"]
            for name, value in self.style_ranking:
                lines.append(f"    {value:.0%}  {name}")
        if self.no_signal:
            lines += [
                "",
                "made no measurable difference to amplification here: "
                + ", ".join(name.replace("_", " ") for name in sorted(self.no_signal)),
            ]
        if self.best_of:
            lines += ["", "best of each choice, by amplification:"]
            for column, (value, score) in sorted(self.best_of.items()):
                lines.append(f"    {column.replace('_', ' '):<20} {value}  ({score:.3f})")
        if self.drivers:
            lines += ["", "what actually moves the numbers (Pearson r):"]
            for _, label, r in self.drivers:
                direction = "↑" if r > 0 else "↓"
                lines.append(f"    {direction} {abs(r):.2f}  {label}")
        if self.generated_forms:
            lines += ["", "these look generated rather than observed (down-weighted to a tenth):"]
            for form in self.generated_forms:
                lines.append(f"    ~ {form} — its columns track each other too neatly")
        if self.conflicts:
            lines += ["", "your exports disagree about these — trust the observed one:"]
            for conflict in self.conflicts:
                lines.append(f"    ! {conflict}")
        if self.separation:
            lines += [
                "",
                f"winners vs failures ({self.wins} / {self.failures} labelled):",
            ]
            for name, (win, fail, cut) in sorted(self.separation.items()):
                lines.append(
                    f"    {name.replace('_', ' '):<24} {win:.2f} vs {fail:.2f}"
                    f"   → aim above {cut:.2f}"
                )
        if self.optimal_hours:
            windows = ", ".join(f"{h:02d}:00" for h in self.optimal_hours)
            lines += ["", f"the winners went out at (UTC): {windows}"]
        if self.failure_modes:
            lines += ["", "how they failed, and what the data says to do:"]
            for state, count, action in self.failure_modes:
                lines.append(f"    {count:>5}  {state:<26} → {action}")
        if self.best_hooks:
            lines += ["", "the hooks that actually travelled:"]
            for hook, amplification in self.best_hooks[:4]:
                lines.append(f"    {amplification:.3f}  {hook[:72]}")
        if self.best_hook_duration:
            lines += ["", f"best hooks cut at {self.best_hook_duration:.1f}s"]
        return "\n".join(lines)


#: How many reshuffles decide whether a categorical choice explains anything.
#: 200 resolves a one-in-twenty threshold comfortably and costs milliseconds.
_SHUFFLES = 200


def _explains_anything(groups: dict[str, list[float]], *, shuffles: int = _SHUFFLES) -> bool:
    """Does *which* option was chosen account for more spread than chance?

    A permutation test, because the alternative is what this code used to do:
    take the highest group mean and call it the winner. With sixteen options
    and a few hundred rows, the best group mean sits above the grand mean every
    time, whether or not the choice matters at all — so a dataset where the
    palette was picked by coin flip still produces "best palette: Warm
    Analogous", ranked, to two decimal places, for an agent to act on.

    Shuffling the labels across the same values destroys any real association
    and keeps everything else — group sizes, the value distribution, the number
    of options. If the observed spread is the sort of thing shuffling produces
    anyway, there is nothing here to report.
    """
    values = [value for bucket in groups.values() for value in bucket]
    sizes = [len(bucket) for bucket in groups.values()]
    if len(groups) < 2 or len(values) < len(groups) * 3:
        return False

    grand = _mean(values)
    total = sum((value - grand) ** 2 for value in values)

    def spread(pools: list[list[float]]) -> float:
        """Share of the variance explained by which group a row is in.

        Not the range between the best and worst group mean, which is what this
        used to be and is the wrong statistic whenever the groups are uneven: a
        column with a hundred and ninety options, most of them holding a
        handful of rows, produces a huge best-to-worst range from noise alone,
        so a real effect could not clear its own shuffled baseline. Variance
        explained weights each group by how much evidence it actually carries.
        """
        if total <= 0:
            return 0.0
        between = sum(len(pool) * (_mean(pool) - grand) ** 2 for pool in pools if pool)
        return between / total

    observed = spread(list(groups.values()))
    if observed <= 0:
        return False

    rng = random.Random(0x5EED)  # fixed, so the same data gives the same verdict
    shuffled = list(values)
    beaten = 0
    for _ in range(shuffles):
        rng.shuffle(shuffled)
        pools: list[list[float]] = []
        cursor = 0
        for size in sizes:
            pools.append(shuffled[cursor : cursor + size])
            cursor += size
        if spread(pools) >= observed:
            beaten += 1
    # One in twenty. Not a deep claim about significance — just a floor low
    # enough that pure noise does not clear it.
    return beaten / shuffles < 0.05


def fit(signals: Sequence[Signal]) -> FitReport:
    """Learn what the winners have in common.

    Deliberately not a regression. With this much data and three objectives the
    honest model is a conditional average plus a correlation: take the rows that
    cleared the bar and report what they did. It is interpretable, it cannot
    overfit in a way nobody notices, and every number in it can be checked by
    hand against the corpus.
    """
    craft = [signal for signal in signals if signal.form in CRAFT_FORMS]
    if not craft:
        craft = list(signals)
    simulated = sum(1 for signal in craft if signal.post_id.startswith("sim_"))

    elite = [s for s in craft if s.three_second_watch_rate >= TARGET_THREE_SECOND_WATCH]
    sharers = [s for s in craft if s.share_to_view_ratio >= TARGET_SHARE_TO_VIEW]
    loopers = [s for s in craft if s.loop_count >= TARGET_LOOP_COUNT]

    by_style: dict[str, list[float]] = {}
    for signal in craft:
        if signal.hook_style:
            by_style.setdefault(signal.hook_style, []).append(signal.three_second_watch_rate)
    ranking = tuple(
        sorted(
            ((name, _mean(values)) for name, values in by_style.items()),
            key=lambda pair: -pair[1],
        )
    )

    # Where the first cut should land. `pattern_interrupt_sec` measures exactly
    # that and is preferred over `hook_duration`, which is the same idea
    # measured less directly.
    interrupts = [s.pattern_interrupt_sec for s in elite if s.pattern_interrupt_sec]
    if not interrupts:
        interrupts = [s.hook_duration for s in elite if s.hook_duration]
    if not interrupts:
        interrupts = [s.pattern_interrupt_sec for s in craft if s.pattern_interrupt_sec]

    # Categorical winners, judged on amplification because that is the column
    # every form has something like.
    best_of: dict[str, tuple[str, float]] = {}
    no_signal: list[str] = []
    for column in (
        "shot_composition",
        "lighting_setup",
        "palette_type",
        "progression_type",
        "harmonic_key",
        "emotional_trigger",
        "theme",
        "framework",
        "trigger",
        "cognitive_bias",
        "audio_anchor",
    ):
        groups: dict[str, list[float]] = {}
        for signal in craft:
            value = getattr(signal, column, "")
            if value:
                groups.setdefault(value, []).append(signal.amplification)
        if groups:
            winner = max(groups.items(), key=lambda pair: _mean(pair[1]))
            score = _mean(winner[1])
            # A form with no amplification column produces a "winner" scoring
            # nought, which is not a winner — it is an absence of measurement.
            if score <= 0:
                continue
            # And a choice that explains nothing produces a "winner" too: with
            # sixteen options the best group mean sits above the grand mean by
            # chance every single time. Ranking that reads as a finding and is
            # a lottery result. So the spread has to beat the same labels
            # shuffled before this column is reported at all.
            if _explains_anything(groups):
                best_of[column] = (winner[0], score)
            else:
                no_signal.append(column)

    # Correlations. Two rules, both learned the hard way:
    #
    # 1. Only rows where the objective was *measured*. `three_second_watch_rate`
    #    is derived from `stop_scroll_ms` when the export lacks it, so
    #    correlating the two over derived rows returns r = 1.000 — a perfect
    #    correlation between a number and itself, reported as a finding.
    # 2. Computed per export, then combined. Pooling a 400-row synthetic matrix
    #    with a 15-row observed file lets the big one decide every answer, and
    #    where they disagree that disagreement is the most useful thing in the
    #    data — see `conflicts`.
    drivers: list[tuple[str, str, float]] = []
    conflicts: list[str] = []
    by_form: dict[str, list[Signal]] = {}
    for signal in craft:
        by_form.setdefault(signal.form, []).append(signal)

    # Which exports look generated rather than observed. Real performance data
    # is noisy: contrast ratio and tempo are set by different people on
    # different days and do not track each other. When a file shows near-perfect
    # correlation between variables that have no business being related, every
    # row is a point on one curve somebody drew — useful as a target, useless
    # as evidence, and dangerous if it is large enough to outvote the rest.
    generated: list[str] = []
    for form, rows in by_form.items():
        if len(rows) < 20:
            continue
        magnitudes: list[float] = []
        columns = [name for name, _ in _DRIVERS]
        for index, first in enumerate(columns):
            for second in columns[index + 1 :]:
                pairs = [
                    (getattr(s, first), getattr(s, second))
                    for s in rows
                    if getattr(s, first, 0.0) and getattr(s, second, 0.0)
                ]
                if len(pairs) >= 10:
                    magnitudes.append(
                        abs(_correlation([p[0] for p in pairs], [p[1] for p in pairs]))
                    )
        if magnitudes and sorted(magnitudes)[len(magnitudes) // 2] > 0.95:
            generated.append(form)

    for column, label in _DRIVERS:
        for objective in ("three_second_watch_rate", "share_to_view_ratio", "loop_count"):
            per_form: list[tuple[str, float, int]] = []
            for form, rows in by_form.items():
                pairs = [
                    (getattr(s, column), getattr(s, objective))
                    for s in rows
                    if getattr(s, column, 0.0)
                    and getattr(s, objective, 0.0)
                    # Skip only genuinely circular pairs — an objective
                    # derived from *this* column. Excluding every derived
                    # objective was too blunt: it threw away tempo against
                    # loop count, which is a unit conversion, not a tautology.
                    and not s.is_circular(objective, column)
                ]
                if len(pairs) < 5:
                    continue
                per_form.append(
                    (form, _correlation([p[0] for p in pairs], [p[1] for p in pairs]), len(pairs))
                )
            if not per_form:
                continue

            strong = [item for item in per_form if abs(item[1]) >= 0.25]
            if len({item[1] > 0 for item in strong}) > 1:
                where = ", ".join(f"{form} r={r:+.2f}" for form, r, _ in strong)
                conflicts.append(f"{label}: {where}")

            # Weight by row count, capped, and heavily discounted for an
            # export that looks generated. Without the discount the 400-row
            # synthetic matrix decides every question by itself.
            def weight(form: str, n: int) -> float:
                return min(n, 50) * (0.1 if form in generated else 1.0)

            total_weight = sum(weight(form, n) for form, _, n in per_form)
            combined = sum(r * weight(form, n) for form, r, n in per_form) / max(total_weight, 1e-9)
            if abs(combined) >= 0.25:
                # Down-weighting cannot help when the generated export is the
                # *only* contributor: a tenth of the only vote is still the
                # only vote. Say so rather than printing r=1.00 as a finding.
                only_generated = all(form in generated for form, _, _ in per_form)
                suffix = "  [generated data only]" if only_generated else ""
                drivers.append(
                    (column, f"{label} → {objective.replace('_', ' ')}{suffix}", combined)
                )
    drivers.sort(key=lambda item: ("[generated" in item[1], -abs(item[2])))

    # Numbers of the right shape and the wrong size. A 22% share rate is not a
    # share rate anybody has ever had; a corpus built around one can still teach
    # which lever beats which, and cannot be quoted as a forecast.
    implausible: list[str] = []
    for form, rows in by_form.items():
        shares = sorted(s.share_to_view_ratio for s in rows if s.share_to_view_ratio)
        if len(shares) >= 10 and shares[len(shares) // 2] > 0.15:
            implausible.append(
                f"{form} has a median share rate of {shares[len(shares) // 2]:.0%}, "
                "several times anything a platform actually sees"
            )
        # The more revealing tell. A real corpus has posts people left; one
        # where the median viewer watches the whole thing has no drop-off in
        # it at all, so nothing in it can teach an agent about retention.
        completions = sorted(s.completion_rate for s in rows if s.completion_rate)
        if len(completions) >= 10 and completions[len(completions) // 2] >= 0.99:
            implausible.append(
                f"{form}: the median post is watched to completion, so there is no "
                "drop-off anywhere in it to learn pacing from"
            )

    # Wins and failures, and the gap between them. This is the only part of the
    # report earned by comparison: everything above describes winners, and a
    # description of winners cannot tell you what a loser looks like.
    labelled = [s for s in signals if s.outcome]
    wins = [s for s in labelled if s.outcome == "win"]
    failures = [s for s in labelled if s.outcome == "fail"]
    separation: dict[str, tuple[float, float, float]] = {}
    if len(wins) >= 20 and len(failures) >= 20:
        for objective in (
            "three_second_watch_rate",
            "completion_rate",
            "loop_count",
            "velocity_score_10m",
        ):
            good = sorted(getattr(s, objective) for s in wins if s.has(objective))
            bad = sorted(getattr(s, objective) for s in failures if s.has(objective))
            if len(good) < 20 or len(bad) < 20:
                continue
            win_median = good[len(good) // 2]
            fail_median = bad[len(bad) // 2]
            # Midpoint between the medians. Crude on purpose: a fitted decision
            # boundary would imply a confidence two medians do not support.
            separation[objective] = (win_median, fail_median, (win_median + fail_median) / 2)

    # When the winners went out. Only hours carrying a real share of the wins
    # count — with 10,000 rows almost every hour appears at least once, and an
    # "optimal window" that spans the whole day is not a window.
    hour_counts: dict[int, int] = {}
    for signal in wins:
        text = (signal.schedule_time or "").strip()
        if len(text) >= 2 and text[:2].isdigit():
            hour = int(text[:2]) % 24
            hour_counts[hour] = hour_counts.get(hour, 0) + 1
    busiest = max(hour_counts.values(), default=0)
    optimal_hours = tuple(
        sorted(hour for hour, count in hour_counts.items() if count >= busiest * 0.5)
    )

    modes: dict[str, tuple[int, str]] = {}
    for signal in failures:
        if not signal.error_state:
            continue
        count, action = modes.get(signal.error_state, (0, signal.recommended_action))
        modes[signal.error_state] = (count + 1, action or signal.recommended_action)

    return FitReport(
        rows=len(craft),
        simulated_rows=simulated,
        measured_rows=len(craft) - simulated,
        elite_three_second=_mean([s.three_second_watch_rate for s in elite]),
        style_ranking=ranking,
        best_hook_duration=_mean(interrupts),
        elite_share=_mean([s.share_to_view_ratio for s in sharers]),
        elite_loop=_mean([s.loop_count for s in loopers]),
        drivers=tuple(drivers[:8]),
        best_of=best_of,
        no_signal=no_signal,
        stalled_rows=sum(1 for s in signals if s.stalled),
        forms=tuple(sorted({s.form for s in signals})),
        conflicts=tuple(conflicts),
        generated_forms=tuple(sorted(generated)),
        implausible=tuple(implausible),
        wins=len(wins),
        failures=len(failures),
        optimal_hours=optimal_hours,
        separation=separation,
        failure_modes=tuple(
            sorted(
                ((state, count, action) for state, (count, action) in modes.items()),
                key=lambda item: -item[1],
            )
        ),
        best_hooks=tuple(
            (signal.hook, signal.amplification)
            for signal in sorted(
                (s for s in craft if s.hook.strip()),
                key=lambda s: -s.amplification,
            )[:6]
        ),
    )


@dataclass
class Objective:
    """One score, and the sentence that explains it."""

    name: str
    score: float
    predicted: float
    target: float
    note: str

    @property
    def meets_target(self) -> bool:
        return self.predicted >= self.target

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "score": round(self.score, 4),
            "predicted": round(self.predicted, 4),
            "target": self.target,
            "meets_target": self.meets_target,
            "note": self.note,
        }


@dataclass
class Prediction:
    """What the model expects of an edit, objective by objective."""

    hook: Objective
    share: Objective
    loop: Objective
    retention_curve: tuple[float, ...] = ()
    runtime: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def objectives(self) -> tuple[Objective, ...]:
        return (self.hook, self.share, self.loop)

    @property
    def overall(self) -> float:
        """One number, weighted the way the brief weights them.

        Amplification leads because a share is what leaves the seed pool; the
        hook is what earns the chance to be shared; the loop multiplies both.
        """
        return self.hook.score * 0.35 + self.share.score * 0.45 + self.loop.score * 0.20

    @property
    def weakest(self) -> Objective:
        return min(self.objectives, key=lambda objective: objective.score)

    def drop_off_second(self) -> float:
        if len(self.retention_curve) < 2 or self.runtime <= 0:
            return 0.0
        step = self.runtime / (len(self.retention_curve) - 1)
        worst_index, worst_drop = 1, 0.0
        for index in range(1, len(self.retention_curve)):
            drop = self.retention_curve[index - 1] - self.retention_curve[index]
            if drop > worst_drop:
                worst_index, worst_drop = index, drop
        return round(worst_index * step, 2)

    def to_json(self) -> dict:
        return {
            "overall": round(self.overall, 4),
            "objectives": [objective.to_json() for objective in self.objectives],
            "retention_curve": [round(value, 4) for value in self.retention_curve],
            "runtime": round(self.runtime, 2),
            "drop_off_second": self.drop_off_second(),
            "notes": list(self.notes),
        }

    def describe(self) -> str:
        lines = [f"predicted overall {self.overall:.0%}"]
        for objective in self.objectives:
            mark = "✓" if objective.meets_target else "·"
            lines.append(
                f"  {mark} {objective.name:<8} {objective.predicted:.3f} "
                f"(target {objective.target:.2f}) — {objective.note}"
            )
        if self.retention_curve:
            lines.append(f"  steepest drop-off at {self.drop_off_second():.1f}s")
        return "\n".join(lines)


def timeline_of(path, *, analysis_fps: float = 24.0) -> EditDecisionList:
    """Reconstruct a timeline from a finished video, so it can be scored.

    Everything in `predict` reads the *edit* — shot lengths, where the first cut
    lands, how the ends relate. A finished file has all of that in it; it just
    has to be measured back out.

    Sampled at 24 frames a second with a two-frame floor, for the same reason
    `insight.reference.measure` is. It used to take the dossier's own boundary
    list, which is built for a different question — where may this program
    safely cut into somebody else's footage — and carries a 350ms refractory
    period that caps any reading at under three cuts a second.

    That fix was made once, in `measure`, and this second path kept the old
    behaviour: handed two reels cutting 33 times per ten seconds, it returned a
    single shot each, and every structural number derived from them — hook,
    loop, the whole benchmark — was computed from a film it believed was one
    unbroken take.

    What cannot be recovered: which source clip each shot came from. So the
    loop objective is judged on whether the last shot *resembles* the first —
    same detected scene — rather than on a clip id that no longer exists. That
    is a weaker test than the one used on a planned edit, and it is the honest
    one for a file somebody else cut.
    """
    from pathlib import Path as _Path

    import numpy as np

    from ..analysis.dossier import build_dossier
    from ..edl import Shot, TextCue, Transition
    from ..ingest import probe_asset

    file = _Path(path)
    asset = probe_asset(file)
    if asset is None or asset.duration <= 0:
        raise ValueError(f"{file.name} is not readable video")

    dossier = build_dossier(file.stem[:8], asset, analysis_fps=analysis_fps, analysis_width=160)
    video = dossier.video

    # The same fast path `measure` uses, rather than the dossier's own list.
    from .reference import _cuts_at_full_rate
    from .. import ffmpeg as _ff

    cuts, _resolved = _cuts_at_full_rate(_ff, file, asset.duration, analysis_fps)
    if not cuts:
        cuts = [float(c) for c in video.shot_boundaries]
    edges = [0.0, *cuts, asset.duration]

    # Each detected scene becomes a shot. Scenes are numbered rather than named
    # because there is no clip id to recover — but the *first* and *last* get
    # compared for similarity below, which is what the loop objective needs.
    shots: list[Shot] = []
    for index in range(len(edges) - 1):
        start, end = edges[index], edges[index + 1]
        if end - start < 0.04:
            continue
        shots.append(
            Shot(
                clip_id=f"S{index:02d}",
                source=file,
                start=start,
                end=end,
                transition_in=Transition(kind="cut"),
            )
        )

    if len(shots) >= 2 and len(video.luma):
        # Does it hand you back to the top? Compare the mean luma and the mean
        # colour of the first and last scenes. Two shots that look alike at this
        # resolution are the closest thing to "same place" available here.
        def window(shot):
            lo = int(shot.start / asset.duration * len(video.luma))
            hi = max(lo + 1, int(shot.end / asset.duration * len(video.luma)))
            return float(np.mean(video.luma[lo:hi]))

        first, last = window(shots[0]), window(shots[-1])
        if abs(first - last) < 0.06:
            shots[-1].clip_id = shots[0].clip_id

    edl = EditDecisionList(
        title=file.stem,
        shots=shots,
        width=asset.display_size[0],
        height=asset.display_size[1],
        fps=int(asset.fps or 30),
    )
    # Words on screen cannot be read without OCR, and guessing would flatter the
    # hook score of anything with a caption burned in. Left empty, and the
    # report says so.
    edl.texts = list[TextCue]()
    return edl


def _opening_seconds(edl: EditDecisionList) -> float:
    """How long before the first cut. This is the hook, mechanically."""
    return edl.shots[0].duration if edl.shots else 0.0


def _seam(edl: EditDecisionList) -> float:
    """How well the end flows back into the beginning, 0..1.

    Judged on what an editor can actually control from the timeline: whether
    the last shot returns to the clip the first shot came from, whether the
    final transition is a hard cut rather than a fade to black, and whether
    the last shot is short enough to feel like a turn rather than an ending.
    """
    if len(edl.shots) < 2:
        return 0.0
    first, last = edl.shots[0], edl.shots[-1]

    same_source = 1.0 if first.clip_id == last.clip_id else 0.0
    hard_out = 1.0 if last.transition_in.is_cut else 0.4
    # A long final shot reads as a full stop; a short one hands you back to
    # the top before you have decided to leave.
    brevity = max(0.0, min(1.0, 1.6 / max(last.duration, 0.2)))
    # A fade to black at the end is the single most loop-hostile thing an edit
    # can do, and it is the default in most templates.
    faded_out = any(cue.style == "end-card" and cue.end >= edl.duration - 0.35 for cue in edl.texts)
    penalty = 0.25 if faded_out else 0.0

    return max(0.0, same_source * 0.45 + hard_out * 0.25 + brevity * 0.30 - penalty)


def _text_lands_early(edl: EditDecisionList) -> bool:
    """Is there a word on screen before the first cut."""
    opening = _opening_seconds(edl)
    return any(cue.start < max(opening, 0.6) for cue in edl.texts)


def predict(edl: EditDecisionList, report: FitReport) -> Prediction:
    """Score a planned edit against the three objectives.

    Everything read here is a property of the timeline, so an agent that wants
    a better score has to change the edit, which is the point. Nothing here
    inspects pixels: the renderer has not run yet.
    """
    runtime = edl.duration
    opening = _opening_seconds(edl)
    notes: list[str] = []

    # -- hook ------------------------------------------------------------
    ideal = report.best_hook_duration or 1.6
    # Distance from the ideal opening length, in seconds, softened so that
    # being half a second out is a nudge rather than a verdict.
    distance = abs(opening - ideal)
    #
    # Two independent contributions rather than a bonus on top of one. Adding
    # the text bonus to a timing score that already reached 1.0 did nothing,
    # which made "land the title earlier" a proposal that could never show a
    # gain on any well-timed edit — the agent was right and the model could
    # not hear it. Splitting the weight lets each be earned separately.
    timing = math.exp(-((distance / 1.4) ** 2))
    early_text = 1.0 if _text_lands_early(edl) else 0.0
    hook_score = 0.82 * timing + 0.18 * early_text
    if not early_text:
        notes.append("nothing on screen before the first cut — the hook is carrying it alone")
    if opening > 3.0:
        notes.append(f"the first shot runs {opening:.1f}s; the winners cut by {ideal:.1f}s")

    predicted_three_second = max(
        0.05, min(0.99, (report.elite_three_second or 0.85) * (0.55 + 0.45 * hook_score))
    )
    hook = Objective(
        name="hook",
        score=hook_score,
        predicted=predicted_three_second,
        target=TARGET_THREE_SECOND_WATCH,
        note=f"first cut at {opening:.1f}s",
    )

    # -- loop ------------------------------------------------------------
    seam = _seam(edl)
    predicted_loop = 1.0 + (report.elite_loop - 1.0 if report.elite_loop else 0.8) * seam
    loop = Objective(
        name="loop",
        score=seam,
        predicted=predicted_loop,
        target=TARGET_LOOP_COUNT,
        note=(
            "ends where it started"
            if seam > 0.7
            else "the ending does not hand you back to the top"
        ),
    )

    # -- share -----------------------------------------------------------
    # Completion is what shares grow out of, and completion falls with runtime
    # and rises with pace. Both are timeline facts.
    pace = len(edl.shots) / max(runtime, 1.0)
    length_penalty = max(0.0, min(0.6, (runtime - 18.0) / 45.0))
    predicted_completion = max(
        0.05, min(0.98, predicted_three_second * (0.72 + 0.10 * min(pace, 2.5)) - length_penalty)
    )
    predicted_share = max(
        0.0, 0.004 + 0.085 * predicted_completion + 0.022 * max(0.0, predicted_loop - 1.0)
    )
    share_score = max(0.0, min(1.0, predicted_share / (TARGET_SHARE_TO_VIEW * 1.6)))
    if runtime > 30:
        notes.append(f"{runtime:.0f}s is long for a share — completion is what people pass on")
    share = Objective(
        name="share",
        score=share_score,
        predicted=predicted_share,
        target=TARGET_SHARE_TO_VIEW,
        note=f"predicted completion {predicted_completion:.0%}",
    )

    curve = tuple(
        round(
            predicted_three_second
            * math.exp(
                math.log(max(predicted_completion, 1e-4) / max(predicted_three_second, 1e-4))
                * (index / 9)
            ),
            4,
        )
        for index in range(10)
    )

    return Prediction(
        hook=hook, share=share, loop=loop, retention_curve=curve, runtime=runtime, notes=notes
    )
