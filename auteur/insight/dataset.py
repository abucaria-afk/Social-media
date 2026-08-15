"""Reading performance exports, and inventing them when there are none.

Two jobs in one file because they must agree on every column: `load` reads a
real CSV, `simulate` writes a fake one, and if they ever disagree about what
`share_to_view_ratio` means then every number downstream is quietly wrong.

The exports this was built against are small — three to six rows each. That is
enough to fix the *schema* and nothing like enough to fit anything, so the
simulator exists to turn a handful of observed rows into a corpus with the same
shape and the same correlations, and the agents rehearse against that.

**What a simulated corpus is worth.** It is worth exactly the assumptions in
`_MODEL` below, which are stated in the open so they can be argued with. An
agent that scores well here has learned to satisfy those assumptions. That is
useful — it means the agent is internally consistent, its objectives trade off
sanely, and the machinery works end to end — and it is not evidence about any
real platform. Replace the corpus with a real export the moment you have one;
`load` takes the same columns and everything downstream changes underneath.
"""

from __future__ import annotations

import csv
import json
import math
import random
from pathlib import Path
from collections.abc import Iterable, Sequence

from .schema import (
    HOOK_STYLES,
    TIER_WORDS,
    VELOCITY_WORDS,
    Signal,
    detect_form,
)

#: How many points the retention curve is sampled at. Ten is enough to see a
#: cliff and few enough to print on a phone.
CURVE_POINTS = 10


def _number(row: dict, *names: str) -> tuple[float, bool]:
    """A float from the first column that has one, and whether it was there."""
    for name in names:
        raw = (row.get(name) or "").strip()
        if raw:
            try:
                return float(raw), True
            except ValueError:
                continue
    return 0.0, False


def _text(row: dict, *names: str) -> tuple[str, bool]:
    for name in names:
        raw = (row.get(name) or "").strip()
        if raw:
            return raw, True
    return "", False


def _row_to_signal(row: dict, form: str, index: int) -> Signal:
    """One CSV row, whichever form it came from, as a Signal."""
    observed: set[str] = set()

    def take_number(field: str, *names: str) -> float:
        value, present = _number(row, *names)
        if present:
            observed.add(field)
        return value

    def take_text(field: str, *names: str) -> str:
        value, present = _text(row, *names)
        if present:
            observed.add(field)
        return value

    post_id = take_text("post_id", "content_id", "carousel_id", "thread_id") or f"{form}_{index}"
    signal = Signal(
        post_id=post_id,
        form=form,
        hook=take_text("hook", "slide_1_hook", "opening_line_hook", "hook"),
        hook_style=take_text("hook_style", "hook_style"),
        hook_duration=take_number("hook_duration", "hook_duration_sec"),
        comment_velocity_10m=take_text("comment_velocity_10m", "comment_velocity_10m"),
        completion_rate=take_number("completion_rate", "completion_rate"),
        avg_time_spent_sec=take_number("avg_time_spent_sec", "avg_time_spent_sec"),
        share_to_view_ratio=take_number("share_to_view_ratio", "share_to_view_ratio"),
        save_rate=take_number("save_rate", "save_rate"),
        bookmark_rate=take_number("bookmark_rate", "bookmark_rate"),
        repost_to_view_ratio=take_number("repost_to_view_ratio", "repost_to_view_ratio"),
        swipe_through_rate=take_number("swipe_through_rate", "swipe_through_rate"),
        profile_visit_rate=take_number("profile_visit_rate", "profile_visit_rate"),
        loop_count=take_number("loop_count", "loop_count", "loop_count_per_user"),
        views_10m=int(take_number("views_10m", "views_10m")),
        three_second_watch_rate=take_number("three_second_watch_rate", "three_second_watch_rate"),
        like_rate=take_number("like_rate", "like_rate"),
        audio_reuse_count=int(take_number("audio_reuse_count", "audio_reuse_count")),
        tier=take_text("tier", "virality_tier"),
        sentiment=take_text("sentiment", "reply_sentiment"),
        # -- film theory --
        shot_composition=take_text("shot_composition", "shot_composition"),
        lighting_setup=take_text("lighting_setup", "lighting_setup"),
        pattern_interrupt_sec=take_number(
            "pattern_interrupt_sec", "pattern_interrupt_timestamp_sec"
        ),
        pacing_cuts_per_10s=take_number("pacing_cuts_per_10s", "pacing_cuts_per_10s"),
        rewind_events=int(take_number("rewind_events", "rewind_events_count")),
        # -- colour theory --
        palette_type=take_text("palette_type", "palette_type"),
        dominant_hex=take_text("dominant_hex", "dominant_hex"),
        contrast_ratio=take_number("contrast_ratio", "contrast_ratio"),
        emotional_trigger=take_text("emotional_trigger", "emotional_trigger_intent"),
        thumbnail_ctr=take_number("thumbnail_ctr", "thumbnail_ctr"),
        stop_scroll_ms=take_number("stop_scroll_ms", "stop_scroll_ms"),
        # -- music theory --
        tempo_bpm=take_number("tempo_bpm", "tempo_bpm"),
        harmonic_key=take_text("harmonic_key", "harmonic_key"),
        progression_type=take_text("progression_type", "progression_type"),
        audio_retention_sec=take_number("audio_retention_sec", "audio_retention_sec"),
        remix_velocity_24h=take_number("remix_velocity_24h", "remix_velocity_24h"),
        haptic_volume_boosts=int(take_number("haptic_volume_boosts", "haptic_volume_boosts")),
        loop_completion_rate=take_number("loop_completion_rate", "loop_completion_rate"),
        # -- how the system treated it --
        seed_pool_size=int(take_number("seed_pool_size", "seed_pool_size")),
        velocity_score_10m=take_number("velocity_score_10m", "velocity_score_10m"),
        seo_keyword_density=take_number("seo_keyword_density", "seo_keyword_density"),
        watch_time_multiplier=take_number("watch_time_multiplier", "watch_time_multiplier"),
        shares_weight_impact=take_number("shares_weight_impact", "shares_weight_impact"),
        bucket_status=take_text("bucket_status", "algorithmic_bucket_status"),
        kill_signal=bool(take_number("kill_signal", "system_kill_signal_triggered")),
        dataset_origin=take_text("dataset_origin", "dataset_origin"),
        # -- the emulated metadata levers --
        theme=take_text("theme", "human_condition_theme", "core_thematic_axis"),
        framework=take_text("framework", "philosophical_framework"),
        trigger=take_text("trigger", "psychological_trigger", "psychological_philosophy_trigger"),
        cognitive_bias=take_text(
            "cognitive_bias", "cognitive_bias_exploited", "human_behavior_cognitive_bias"
        ),
        audio_anchor=take_text("audio_anchor", "music_theory_anchor", "music_theory_audio_anchor"),
    )

    if not signal.hook:
        text, present = _text(row, "hook_text", "hook_text_metadata")
        if present:
            signal.hook = text
            observed.add("hook")
    if not signal.palette_type:
        text, present = _text(row, "color_theory_palette")
        if present:
            signal.palette_type = text
            observed.add("palette_type")

    # The emulation exports carry counts, not ratios. Compute the ratios the
    # objectives are actually stated in — and treat them as measured, because
    # a division is not an inference.
    views, has_views = _number(row, "simulated_views", "total_views", "views")
    if has_views and views > 0:
        signal.views_10m = int(views)
        for target, columns in (
            ("share_to_view_ratio", ("simulated_shares", "shares")),
            ("save_rate", ("simulated_saves", "saves")),
            ("comment_rate", ("simulated_comments", "comments")),
        ):
            count, present = _number(row, *columns)
            if present:
                setattr(signal, target, count / views)
                observed.add(target)

    # Average watch time as a percentage of runtime. Over 100% is the whole
    # point: it means people watched it more than once, which is a loop count
    # by another name.
    watch, has_watch = _number(row, "avg_watch_time_pct", "average_watch_time_pct")
    if has_watch and watch > 0:
        signal.completion_rate = min(1.0, watch / 100.0)
        observed.add("completion_rate")
        if watch > 100.0:
            signal.loop_count = watch / 100.0
            observed.add("loop_count")

    # Click-through is the thumbnail winning the scroll — the closest thing
    # these exports have to a three-second measure, and not the same thing.
    ctr, has_ctr = _number(row, "click_through_rate_pct")
    if has_ctr:
        signal.thumbnail_ctr = ctr / 100.0
        observed.add("thumbnail_ctr")

    # `save_to_view_ratio` is the film-theory export's amplification column and
    # means the same thing as `save_rate`.
    if "save_rate" not in observed:
        value, present = _number(row, "save_to_view_ratio")
        if present:
            signal.save_rate = value
            observed.add("save_rate")

    # A three-second watch rate is the field the agents are trained on and the
    # field nobody exports. Where it is missing, derive it — a completion rate
    # is a lower bound on it, since nobody completes a video they left at two
    # seconds, and a swipe-through rate is the carousel's equivalent.
    #
    # These read the local `observed` set rather than `signal.has(...)`:
    # `signal.observed` is not assigned until the end of this function, so
    # asking the signal was asking an empty set, every branch here was skipped,
    # and every measured row arrived downstream with a three-second rate of
    # nought — which the model then dutifully averaged.
    # A loop completion rate is a fraction; `loop_count` is a number of plays.
    # One implies the other: if 84% of viewers reach the end and the end hands
    # them back to the start, that is 1.84 plays each.
    if "loop_count" not in observed and "loop_completion_rate" in observed:
        signal.loop_count = 1.0 + signal.loop_completion_rate
        signal.derived_from["loop_count"] = "loop_completion_rate"

    if "three_second_watch_rate" not in observed:
        if "completion_rate" in observed:
            # Completion is what survives the whole runtime; the three-second
            # mark is much earlier and always higher. The gap widens for longer
            # hooks, which is the entire argument for a short one.
            penalty = 0.06 * max(0.0, signal.hook_duration - 1.5)
            signal.three_second_watch_rate = max(
                0.0, min(0.99, signal.completion_rate**0.45 - penalty)
            )
            signal.derived_from["three_second_watch_rate"] = "completion_rate"
        elif "swipe_through_rate" in observed:
            signal.three_second_watch_rate = signal.swipe_through_rate
            signal.derived_from["three_second_watch_rate"] = "swipe_through_rate"
        elif "stop_scroll_ms" in observed:
            # A thumb that stops for 850ms has not yet reached three seconds,
            # but stopping at all is the precondition. Scaled against a second
            # of dwell, capped — this is a proxy and a rough one.
            signal.three_second_watch_rate = min(0.99, signal.stop_scroll_ms / 1000.0)
            signal.derived_from["three_second_watch_rate"] = "stop_scroll_ms"
        # Derived, not measured. It is deliberately *not* added to `observed`:
        # anything asking "was this really measured?" must still get "no".

    if signal.completion_rate:
        signal.retention_curve = _curve_from(
            signal.three_second_watch_rate or 0.9, signal.completion_rate
        )

    signal.observed = frozenset(observed)
    return signal


def _curve_from(start: float, end: float, points: int = CURVE_POINTS) -> tuple[float, ...]:
    """A plausible retention curve between two known ends.

    Real curves are not straight: they fall off a cliff in the first second and
    then decay gently. Modelling that as an exponential between the two
    measured points is a guess, and it is a much better guess than a line.
    """
    start, end = max(0.0, min(1.0, start)), max(0.0, min(1.0, end))
    if points < 2:
        return (start,)
    if end <= 0.0 or start <= 0.0:
        return tuple(start * (1 - index / (points - 1)) for index in range(points))
    rate = math.log(max(end, 1e-4) / max(start, 1e-4))
    return tuple(
        round(start * math.exp(rate * (index / (points - 1))), 4) for index in range(points)
    )


def load(paths: Sequence[str | Path]) -> list[Signal]:
    """Read one or more performance exports into a common shape."""
    signals: list[Signal] = []
    for path in paths:
        file = Path(path)
        if not file.exists():
            raise FileNotFoundError(f"no such export: {file}")
        with file.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                continue
            form = detect_form(list(reader.fieldnames))
            for index, row in enumerate(reader):
                # Headers arrive in whatever case the exporter felt like —
                # `Content_ID` here, `content_id` there. Normalise once at the
                # door so no lookup below has to care.
                lowered = {(key or "").strip().lower(): value for key, value in row.items()}
                signals.append(_row_to_signal(lowered, form, index))
    return signals


# ---------------------------------------------------------------------------
# The simulator
# ---------------------------------------------------------------------------

#: The assumptions. Every one of these is a claim about how attention works,
#: and every one is arguable — which is the point of writing them here rather
#: than scattering them through the generator as magic numbers.
_MODEL = {
    # A hook has about a second and a half before the thumb decides. Past that
    # the three-second watch rate falls away steeply.
    "hook_sweet_spot": 1.6,
    "hook_penalty_per_second": 0.11,
    # What each hook style is worth at the three-second mark, before noise.
    "style_lift": {
        "Visual Pattern Interrupt": 0.10,
        "Text Over Blank Screen": 0.12,
        "Contrarian Statement": 0.05,
        "Micro-Story / Tension": 0.02,
        "Satisfying / ASMR": -0.03,
        "Direct Address": 0.03,
        "In-Progress Action": 0.07,
        "Reveal Withhold": 0.08,
    },
    # Shares follow from *completing* and from having a reason to send it to
    # somebody, not from watching the first second.
    "share_from_completion": 0.085,
    "share_from_loop": 0.022,
    "share_floor": 0.004,
    # A tight loop is watched more than once, and each extra watch is another
    # completion, which is why looping compounds.
    "loop_from_seam": 0.9,
    "loop_base": 0.95,
    # Velocity in the first ten minutes follows the hook far more than the body.
    "velocity_from_hook": 1.5,
    "velocity_noise": 0.22,
}


def _tier_for(three_second: float, share: float, loop: float) -> str:
    """The qualitative label, from the three numbers that earn it."""
    score = three_second * 0.35 + min(share / 0.10, 1.0) * 0.45 + min(loop / 2.2, 1.0) * 0.20
    if score >= 0.78:
        return TIER_WORDS[3]
    if score >= 0.62:
        return TIER_WORDS[2]
    if score >= 0.45:
        return TIER_WORDS[1]
    return TIER_WORDS[0]


def simulate(
    count: int = 2000,
    *,
    seed: int = 0xC4A7E,
    seeded_by: Iterable[Signal] = (),
) -> list[Signal]:
    """A corpus of simulated short-form performance.

    `seeded_by` anchors the simulation to real rows where you have them: the
    observed hook styles and their measured rates set the centre of the
    distribution, and the simulator varies around it. With no seed rows it
    falls back to `_MODEL` alone.

    Every returned Signal has `post_id` beginning `sim_`, so simulated rows can
    never be mistaken for measured ones further downstream.
    """
    rng = random.Random(seed)
    seeds = [signal for signal in seeded_by if signal.form == "short_form_video"]

    # Where a real row exists for a style, believe it over the model.
    observed_lift: dict[str, float] = {}
    for signal in seeds:
        if signal.hook_style and signal.three_second_watch_rate:
            baseline = 0.72 - _MODEL["hook_penalty_per_second"] * max(
                0.0, signal.hook_duration - _MODEL["hook_sweet_spot"]
            )
            observed_lift.setdefault(signal.hook_style, signal.three_second_watch_rate - baseline)

    out: list[Signal] = []
    for index in range(count):
        style = rng.choice(HOOK_STYLES)
        hook_duration = max(0.5, rng.gauss(2.0, 0.7))
        lift = observed_lift.get(style, _MODEL["style_lift"].get(style, 0.0))

        three_second = 0.72 + lift
        three_second -= _MODEL["hook_penalty_per_second"] * max(
            0.0, hook_duration - _MODEL["hook_sweet_spot"]
        )
        three_second += rng.gauss(0.0, 0.09)
        three_second = max(0.05, min(0.99, three_second))

        # Completion is bounded by the three-second rate: you cannot finish
        # what you did not start.
        completion = three_second * max(0.15, min(1.0, rng.gauss(0.62, 0.16)))

        seam = rng.random()  # 0 = hard stop, 1 = the end flows into the start
        loop = _MODEL["loop_base"] + _MODEL["loop_from_seam"] * seam * completion
        loop += rng.gauss(0.0, 0.12)
        loop = max(1.0, loop)

        share = (
            _MODEL["share_floor"]
            + _MODEL["share_from_completion"] * completion
            + _MODEL["share_from_loop"] * (loop - 1.0)
            + rng.gauss(0.0, 0.012)
        )
        share = max(0.0, share)

        velocity_score = three_second * _MODEL["velocity_from_hook"] + rng.gauss(
            0.0, _MODEL["velocity_noise"]
        )
        velocity = VELOCITY_WORDS[max(0, min(3, int(velocity_score * 2.4)))]

        runtime = max(5.0, rng.gauss(18.0, 7.0))
        signal = Signal(
            post_id=f"sim_{index:05d}",
            form="short_form_video",
            hook_style=style,
            hook_duration=round(hook_duration, 2),
            views_10m=int(max(50, rng.lognormvariate(7.0 + three_second * 2.2, 0.8))),
            comment_velocity_10m=velocity,
            engagement_velocity=round(three_second * rng.uniform(0.02, 0.08), 5),
            three_second_watch_rate=round(three_second, 4),
            completion_rate=round(completion, 4),
            avg_time_spent_sec=round(runtime * completion * loop, 2),
            retention_curve=_curve_from(three_second, completion),
            share_to_view_ratio=round(share, 4),
            save_rate=round(max(0.0, share * rng.uniform(1.1, 2.4)), 4),
            repost_to_view_ratio=round(max(0.0, share * rng.uniform(0.3, 0.8)), 4),
            profile_visit_rate=round(max(0.0, share * rng.uniform(0.4, 1.1)), 4),
            audio_reuse_count=int(max(0, rng.gauss(share * 900, 40))),
            loop_count=round(loop, 3),
            like_rate=round(max(0.0, rng.gauss(0.09, 0.03)), 4),
            tier=_tier_for(three_second, share, loop),
        )
        signal.observed = frozenset(
            {
                "hook_style",
                "hook_duration",
                "views_10m",
                "comment_velocity_10m",
                "three_second_watch_rate",
                "completion_rate",
                "share_to_view_ratio",
                "loop_count",
                "retention_curve",
                "tier",
            }
        )
        out.append(signal)
    return out


def write_csv(signals: Sequence[Signal], destination: Path) -> Path:
    """Write a corpus back out, so it can be inspected in a spreadsheet."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "content_id",
        "hook_style",
        "hook_duration_sec",
        "views_10m",
        "comment_velocity_10m",
        "three_second_watch_rate",
        "completion_rate",
        "share_to_view_ratio",
        "save_rate",
        "repost_to_view_ratio",
        "audio_reuse_count",
        "loop_count",
        "like_rate",
        "virality_tier",
    ]
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for signal in signals:
            writer.writerow(
                [
                    signal.post_id,
                    signal.hook_style,
                    round(signal.hook_duration, 2),
                    signal.views_10m,
                    signal.comment_velocity_10m,
                    round(signal.three_second_watch_rate, 4),
                    round(signal.completion_rate, 4),
                    round(signal.share_to_view_ratio, 4),
                    round(signal.save_rate, 4),
                    round(signal.repost_to_view_ratio, 4),
                    signal.audio_reuse_count,
                    round(signal.loop_count, 3),
                    round(signal.like_rate, 4),
                    signal.tier,
                ]
            )
    return destination


def write_json(signals: Sequence[Signal], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps([signal.to_json() for signal in signals], indent=2), encoding="utf-8"
    )
    return destination
