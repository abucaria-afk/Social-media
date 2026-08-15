"""What a post's performance looks like as numbers.

Three content forms, three sets of columns, taken from the datasets this was
built against — short-form video, B2B carousels, text threads. They measure
different things because the surfaces are different: a carousel has no
completion rate, a thread has no loop count.

Underneath the three there is one shared idea, and the agents are trained on
that rather than on any single form:

**Velocity** — how fast engagement arrives, not how much. The first ten minutes
decide whether a post leaves its seed audience, so `comment_velocity_10m` and
`views_10m` matter more than a day's total.

**Retention** — where attention stops. A completion rate is a summary of a
retention curve, and the curve is the useful object: the second people leave is
a note about pacing, and the summary is not.

**Amplification** — the actions that put a post in front of somebody who does
not follow you. A share moves a post; a like mostly does not. So shares, saves,
reposts and audio re-use are weighted far above passive engagement.

──────────────────────────────────────────────────────────────────────
Everything in this package operates on *simulated* metrics unless a real
export is loaded. See `simulate.py`. A model fitted to simulated numbers has
learned the simulator, not the platform. It is a rehearsal, not evidence.
──────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Content forms this understands, and the column that identifies each.
FORMS = (
    "short_form_video",
    "b2b_carousel",
    "text_thread",
    "film_theory",
    "color_theory",
    "music_theory",
    "algorithmic",
    "multimodal_matrix",
    "metadata_emulation",
    "metadata_emulation_wide",
    "workflow_outcome",
    "metadata_domain",
)

#: Forms that describe *how a video was made* rather than a different medium.
#: These are the ones the editing agents can act on, because every column in
#: them is something an edit can change.
CRAFT_FORMS = (
    "short_form_video",
    "film_theory",
    "color_theory",
    "music_theory",
    "multimodal_matrix",
    "metadata_emulation",
    "metadata_emulation_wide",
    "workflow_outcome",
    "metadata_domain",
)

#: Header signatures used to recognise a CSV without being told what it is.
#: Ordered most specific first — the multimodal matrix carries columns from
#: every other form, so it has to be matched before any of them.
_SIGNATURES: dict[str, tuple[str, ...]] = {
    # Emulated metadata: categorical levers plus raw counts, from which the
    # ratios the objectives are stated in have to be computed.
    "metadata_emulation": ("content_id", "hook_text", "avg_watch_time_pct"),
    "metadata_emulation_wide": (
        "content_id",
        "hook_text_metadata",
        "average_watch_time_pct",
    ),
    "multimodal_matrix": ("dataset_origin", "palette_type", "shot_composition", "tempo_bpm"),
    "film_theory": ("shot_composition", "lighting_setup", "three_second_watch_rate"),
    "color_theory": ("palette_type", "dominant_hex", "thumbnail_ctr"),
    "music_theory": ("tempo_bpm", "harmonic_key", "progression_type"),
    "algorithmic": ("seed_pool_size", "velocity_score_10m", "algorithmic_bucket_status"),
    "short_form_video": ("content_id", "hook_style", "completion_rate"),
    "b2b_carousel": ("carousel_id", "slide_1_hook", "swipe_through_rate"),
    "text_thread": ("thread_id", "opening_line_hook", "bookmark_rate"),
}

#: Ordered words for the qualitative columns, so "Extreme" sorts above "High".
VELOCITY_WORDS = ("Low", "Moderate", "High", "Extreme")
TIER_WORDS = ("Low-Tier", "Mid-Tier", "High-Viral", "Mega-Viral")

#: Hook styles seen in the short-form data, plus the ones the agents may
#: propose. Kept as a list rather than an enum because a new hook style is a
#: fact about the world, not a change to the program.
HOOK_STYLES = (
    "Visual Pattern Interrupt",
    "Contrarian Statement",
    "Micro-Story / Tension",
    "Text Over Blank Screen",
    "Satisfying / ASMR",
    "Direct Address",
    "In-Progress Action",
    "Reveal Withhold",
)

#: The three thresholds the agents are trained against. They are targets taken
#: from the brief, not laws: a post can beat all three and reach nobody.
TARGET_THREE_SECOND_WATCH = 0.80
TARGET_SHARE_TO_VIEW = 0.05
TARGET_LOOP_COUNT = 1.5


@dataclass
class Signal:
    """One post's performance, normalised across the three forms.

    Fields a given form does not measure stay at zero, and `has()` says which
    were actually observed — so an average over mixed forms cannot quietly
    treat "no loop count on a carousel" as "a loop count of nought".
    """

    post_id: str
    form: str
    hook: str = ""
    hook_style: str = ""
    #: The levers the emulated metadata varies: a theme, a philosophy, a
    #: palette, an audio anchor, a psychological trigger, a bias. Categorical
    #: and therefore rankable, which is what makes them actionable.
    theme: str = ""
    framework: str = ""
    trigger: str = ""
    cognitive_bias: str = ""
    audio_anchor: str = ""
    comment_rate: float = 0.0
    hook_duration: float = 0.0

    # -- velocity: the first ten minutes ---------------------------------
    views_10m: int = 0
    comment_velocity_10m: str = ""
    engagement_velocity: float = 0.0

    # -- retention: where attention stops --------------------------------
    three_second_watch_rate: float = 0.0
    completion_rate: float = 0.0
    avg_time_spent_sec: float = 0.0
    #: Fraction still watching, sampled at even points across the runtime.
    retention_curve: tuple[float, ...] = ()

    # -- amplification: what moves it ------------------------------------
    share_to_view_ratio: float = 0.0
    save_rate: float = 0.0
    bookmark_rate: float = 0.0
    repost_to_view_ratio: float = 0.0
    swipe_through_rate: float = 0.0
    profile_visit_rate: float = 0.0
    audio_reuse_count: int = 0
    loop_count: float = 0.0

    # -- how the picture was made (film theory) ---------------------------
    shot_composition: str = ""
    lighting_setup: str = ""
    #: When the first pattern interrupt lands. The single most actionable
    #: number in any of this: it is literally where to put the first cut.
    pattern_interrupt_sec: float = 0.0
    pacing_cuts_per_10s: float = 0.0
    rewind_events: int = 0

    # -- how it looks (colour theory) -------------------------------------
    palette_type: str = ""
    dominant_hex: str = ""
    contrast_ratio: float = 0.0
    emotional_trigger: str = ""
    thumbnail_ctr: float = 0.0
    #: How long the thumb stops before deciding, in milliseconds.
    stop_scroll_ms: float = 0.0

    # -- how it sounds (music theory) -------------------------------------
    tempo_bpm: float = 0.0
    harmonic_key: str = ""
    progression_type: str = ""
    audio_retention_sec: float = 0.0
    #: Other people using your audio — the strongest amplification there is,
    #: because it costs the copier real effort.
    remix_velocity_24h: float = 0.0
    haptic_volume_boosts: int = 0
    loop_completion_rate: float = 0.0

    # -- how the system treated it (algorithmic) --------------------------
    seed_pool_size: int = 0
    velocity_score_10m: float = 0.0
    seo_keyword_density: float = 0.0
    watch_time_multiplier: float = 0.0
    shares_weight_impact: float = 0.0
    bucket_status: str = ""
    kill_signal: bool = False
    dataset_origin: str = ""

    # -- passive, deliberately kept separate ------------------------------
    like_rate: float = 0.0

    tier: str = ""
    sentiment: str = ""
    #: "win" | "fail" | "" — the label that makes discrimination possible at
    #: all. Everything before this arrived was winners describing themselves.
    outcome: str = ""
    #: Why it failed, from the export's own taxonomy.
    error_state: str = ""
    #: What the export says to do about it. The agents map these to checks.
    recommended_action: str = ""
    editing_style: str = ""
    content_bucket: str = ""
    platform: str = ""
    schedule_time: str = ""
    seed_tier: int = 0
    observed: frozenset[str] = field(default_factory=frozenset)
    #: field -> the column it was inferred from, for fields the export did not
    #: carry. Correlating a derived field against its own source returns 1.0
    #: and means nothing, so the provenance has to survive the derivation.
    derived_from: dict[str, str] = field(default_factory=dict)

    def has(self, name: str) -> bool:
        """Was this field actually measured, rather than defaulted?"""
        return name in self.observed

    def is_circular(self, field_name: str, against: str) -> bool:
        """Would correlating these two be correlating a number with itself?"""
        return self.derived_from.get(field_name) == against or (
            self.derived_from.get(against) == field_name
        )

    @property
    def velocity_rank(self) -> int:
        """Qualitative velocity as a number, -1 when it was not measured."""
        try:
            return VELOCITY_WORDS.index(self.comment_velocity_10m)
        except ValueError:
            return -1

    @property
    def tier_rank(self) -> int:
        try:
            return TIER_WORDS.index(self.tier)
        except ValueError:
            return -1

    @property
    def failed(self) -> bool:
        return self.outcome == "fail"

    @property
    def stalled(self) -> bool:
        if self.outcome == "fail":
            return True
        """Did the system stop pushing this — the only real negative label.

        A corpus with none of these can describe what winners look like and
        cannot tell you what separates them from anything else.
        """
        return self.kill_signal or self.bucket_status.replace("_", "-").lower() in (
            "stuck-in-seed",
            "suppressed",
        )

    @property
    def amplification(self) -> float:
        """One number for "did anybody put this in front of somebody else".

        Shares and reposts are the strongest signal available — they cost the
        sharer something, which is exactly why the ranking systems treat them
        as worth more than a like. Saves and swipe-throughs are weaker but real.
        Likes are in here at a twentieth of a share's weight, which is roughly
        how much they are worth and visibly not zero.
        """
        return (
            self.share_to_view_ratio * 1.0
            + self.repost_to_view_ratio * 1.0
            + self.save_rate * 0.55
            + self.bookmark_rate * 0.55
            + self.profile_visit_rate * 0.30
            + self.swipe_through_rate * 0.10
            + self.like_rate * 0.05
        )

    def drop_off_second(self, runtime: float) -> float:
        """The moment the audience leaves fastest, in seconds.

        The steepest downward step in the retention curve — which is a more
        useful note to an editor than a completion rate, because it points at
        a cut rather than at a verdict.
        """
        if len(self.retention_curve) < 2 or runtime <= 0:
            return 0.0
        step = runtime / (len(self.retention_curve) - 1)
        worst_index, worst_drop = 1, 0.0
        for index in range(1, len(self.retention_curve)):
            drop = self.retention_curve[index - 1] - self.retention_curve[index]
            if drop > worst_drop:
                worst_index, worst_drop = index, drop
        return round(worst_index * step, 2)

    def to_json(self) -> dict:
        return {
            "post_id": self.post_id,
            "form": self.form,
            "hook": self.hook,
            "hook_style": self.hook_style,
            "hook_duration": round(self.hook_duration, 3),
            "views_10m": self.views_10m,
            "comment_velocity_10m": self.comment_velocity_10m,
            "engagement_velocity": round(self.engagement_velocity, 5),
            "three_second_watch_rate": round(self.three_second_watch_rate, 4),
            "completion_rate": round(self.completion_rate, 4),
            "avg_time_spent_sec": round(self.avg_time_spent_sec, 2),
            "retention_curve": [round(value, 4) for value in self.retention_curve],
            "share_to_view_ratio": round(self.share_to_view_ratio, 4),
            "save_rate": round(self.save_rate, 4),
            "bookmark_rate": round(self.bookmark_rate, 4),
            "repost_to_view_ratio": round(self.repost_to_view_ratio, 4),
            "swipe_through_rate": round(self.swipe_through_rate, 4),
            "profile_visit_rate": round(self.profile_visit_rate, 4),
            "audio_reuse_count": self.audio_reuse_count,
            "loop_count": round(self.loop_count, 3),
            "like_rate": round(self.like_rate, 4),
            "tier": self.tier,
            "sentiment": self.sentiment,
            "observed": sorted(self.observed),
        }


def _is_domain_export(columns: set[str]) -> bool:
    """One family, one column per domain.

    These arrive as `viral_metadata_<domain>.csv` — human condition, art
    history, art theory, cinematography, human behaviour — identical but for a
    `Primary_<domain>` column. Matching them by a fixed signature would mean a
    new edit to this file every time somebody adds a domain, so they are
    recognised by their shape instead: an id, a primary lever, and a watch time.
    """
    return (
        "metadata_id" in columns
        and "avg_watch_time_pct" in columns
        and any(name.startswith("primary_") for name in columns)
    )


def domain_of(header: list[str]) -> str:
    """Which lever a domain export varies, from its `Primary_` column."""
    for name in header:
        lowered = name.strip().lower()
        if lowered.startswith("primary_"):
            return lowered[len("primary_") :]
    return ""


def detect_form(header: list[str]) -> str:
    """Which of the three forms a CSV holds, by its columns.

    Recognising a file by its header rather than by its name means a user can
    call the export whatever they like, which is what they are going to do.
    """
    columns = {name.strip().lower() for name in header}
    if _is_domain_export(columns):
        return "metadata_domain"
    best, best_hits = "", 0
    for form, signature in _SIGNATURES.items():
        hits = sum(1 for column in signature if column in columns)
        if hits > best_hits:
            best, best_hits = form, hits
    if best_hits < 2:
        raise ValueError(
            "this does not look like a performance export — expected columns for "
            f"one of: {', '.join(FORMS)}"
        )
    return best
