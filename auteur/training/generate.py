"""Generate natural training metadata for all sixteen disciplines.

Usage:
    python -m auteur.training.generate [--output-dir DIR] [--rows-per-domain N] [--seed S]

Each domain produces a CSV named `viral_metadata_<domain>.csv` with the schema
that `auteur.insight.dataset.load` already recognises as `metadata_domain`.
"""

from __future__ import annotations

import csv
import hashlib
import math
import random
from pathlib import Path

# ---------------------------------------------------------------------------
# Domain definitions: primary levers, secondary frameworks, and hooks
# ---------------------------------------------------------------------------

DOMAINS: dict[str, dict] = {
    "color_theory": {
        "primary": [
            "Complementary Tension",
            "Analogous Harmony",
            "Triadic Energy",
            "Split-Complementary Depth",
            "Monochromatic Mood",
            "Warm-Cool Contrast",
            "Saturation Gradient",
            "Value Hierarchy",
            "Chromatic Aberration",
            "Color Temperature Shift",
            "Simultaneous Contrast",
            "Color Weight Distribution",
        ],
        "secondary": [
            "Itten's Seven Contrasts",
            "Albers Interaction Model",
            "Munsell Value System",
            "Goethe's Sensory Framework",
            "Chevreul's Law of Simultaneous Contrast",
            "Bezold Effect Application",
        ],
        "hooks": [
            "This one color ratio stops the scroll every time",
            "The color pairing nobody talks about",
            "Why warm shadows outperform cold ones 3:1",
            "A single hue shift that doubled my retention",
            "The palette mistake killing your watch time",
        ],
    },
    "music_theory": {
        "primary": [
            "Tension-Release Cycle",
            "Rhythmic Syncopation",
            "Harmonic Suspension",
            "Modal Interchange",
            "Polyrhythmic Layering",
            "Dynamic Crescendo Arc",
            "Melodic Contour Matching",
            "Chord Inversion Depth",
            "Timbral Contrast",
            "Tempo Modulation",
            "Silence as Emphasis",
            "Overtone Resonance",
        ],
        "secondary": [
            "Schenkerian Reduction",
            "Lerdahl-Jackendoff Grouping",
            "Hindemith Craft Framework",
            "Bernstein's Unanswered Question",
            "Rick Beato's Harmonic Analysis",
            "Schoenberg's Emancipation of Dissonance",
        ],
        "hooks": [
            "This chord progression makes people replay instinctively",
            "The tempo sweet spot that holds attention for 18 seconds",
            "Why silence at 1.2 seconds creates more shares",
            "One bass frequency that triggers physical response",
            "The key change nobody hears but everyone feels",
        ],
    },
    "human_behavior": {
        "primary": [
            "Social Proof Cascade",
            "Loss Aversion Trigger",
            "Reciprocity Loop",
            "Scarcity Framing",
            "Authority Anchoring",
            "Commitment Escalation",
            "In-Group Signalling",
            "Novelty-Seeking Response",
            "Status Comparison Drive",
            "Mirror Neuron Activation",
            "Dopamine Anticipation Gap",
            "Cognitive Ease Preference",
        ],
        "secondary": [
            "Kahneman's Dual Process",
            "Cialdini's Influence Model",
            "Fogg Behavior Model",
            "Self-Determination Theory",
            "Maslow's Hierarchy Applied",
            "Festinger's Cognitive Dissonance",
        ],
        "hooks": [
            "The behavior loop that turns viewers into sharers",
            "Why people save content they never watch again",
            "One tribal signal that triples comment velocity",
            "The status gap that makes a reel go viral",
            "How scarcity framing holds attention past 8 seconds",
        ],
    },
    "human_condition": {
        "primary": [
            "Mortality Awareness",
            "Loneliness Recognition",
            "Purpose Seeking",
            "Impermanence Acceptance",
            "Belonging Hunger",
            "Identity Construction",
            "Suffering Reframe",
            "Joy in Mundane",
            "Vulnerability as Strength",
            "Growth Through Adversity",
            "Connection Longing",
            "Legacy Anxiety",
        ],
        "secondary": [
            "Existentialist Tradition",
            "Buddhist Impermanence",
            "Stoic Acceptance",
            "Absurdist Embrace",
            "Humanistic Psychology",
            "Phenomenological Presence",
        ],
        "hooks": [
            "The universal feeling nobody names out loud",
            "Why this 12-second clip made 40,000 people save it",
            "One sentence that captures what everyone is afraid of",
            "The loneliness paradox that drives shares",
            "A truth so obvious it stops the scroll",
        ],
    },
    "pattern_recognition": {
        "primary": [
            "Visual Rhythm Break",
            "Expectation Violation",
            "Symmetry Disruption",
            "Repetition-with-Variation",
            "Gestalt Completion",
            "Fractal Self-Similarity",
            "Odd-Number Grouping",
            "Temporal Pattern Interrupt",
            "Figure-Ground Reversal",
            "Hierarchical Nesting",
            "Fibonacci Proportion",
            "Golden Ratio Composition",
        ],
        "secondary": [
            "Gestalt Laws of Organisation",
            "Information Theory Surprise",
            "Bayesian Prediction Error",
            "Miller's Chunking Principle",
            "Weber-Fechner Threshold",
            "Apophenia Channel",
        ],
        "hooks": [
            "Your brain finishes this pattern before you decide to watch",
            "The visual rhythm that resets attention every 3 seconds",
            "Why odd numbers hold the eye longer",
            "A symmetry break that doubled my loop count",
            "The hidden pattern making top creators' content sticky",
        ],
    },
    "psychology": {
        "primary": [
            "Peak-End Rule",
            "Zeigarnik Effect",
            "Serial Position Primacy",
            "Mere Exposure Lift",
            "Anchoring Bias",
            "Availability Heuristic",
            "Framing Effect",
            "Dunning-Kruger Tension",
            "Paradox of Choice",
            "Flow State Entry",
            "Emotional Contagion",
            "Attentional Blink",
        ],
        "secondary": [
            "Kahneman's Prospect Theory",
            "Csikszentmihalyi's Flow",
            "Ekman's Basic Emotions",
            "Bandura's Social Learning",
            "Festinger's Social Comparison",
            "Zajonc's Mere Exposure",
        ],
        "hooks": [
            "The psychological trick hiding in every viral video's last second",
            "Why unfinished loops keep people watching",
            "The framing shift that turns scrollers into commenters",
            "One anchoring technique that lifts share rate by 40%",
            "How the peak-end rule decides if your content gets saved",
        ],
    },
    "philosophy": {
        "primary": [
            "Socratic Questioning",
            "Absurdist Juxtaposition",
            "Dialectical Tension",
            "Phenomenological Reduction",
            "Existential Authenticity",
            "Stoic Reframe",
            "Epicurean Simplicity",
            "Nietzschean Affirmation",
            "Taoist Non-Action",
            "Pragmatist Consequence",
            "Heraclitean Flux",
            "Platonic Ideal Contrast",
        ],
        "secondary": [
            "Aristotelian Poetics",
            "Kantian Aesthetic Judgment",
            "Wittgenstein's Language Games",
            "Derrida's Deconstruction",
            "Deleuze's Difference",
            "Heidegger's Dasein",
        ],
        "hooks": [
            "A 2,400-year-old question that still stops people mid-scroll",
            "The philosophical paradox that generates the most comments",
            "Why absurdist content outperforms motivational 2:1",
            "One Stoic principle that makes content feel timeless",
            "The dialectic structure behind every viral essay",
        ],
    },
    "psychological_philosophy": {
        "primary": [
            "Meaning-Making Under Uncertainty",
            "Authentic Self vs Social Self",
            "Freedom and Responsibility Tension",
            "Consciousness of Mortality",
            "Will to Meaning",
            "Absurd Acceptance",
            "Radical Self-Honesty",
            "Ego Dissolution Moment",
            "Collective Unconscious Tap",
            "Shadow Integration",
            "Liminality Navigation",
            "Transcendence Through Suffering",
        ],
        "secondary": [
            "Frankl's Logotherapy",
            "Jung's Individuation",
            "Kierkegaard's Leap",
            "Camus' Absurd Hero",
            "Nietzsche's Amor Fati",
            "Lacan's Mirror Stage",
        ],
        "hooks": [
            "The meaning gap that makes people share before they finish watching",
            "Why content about freedom creates the most saves",
            "One shadow concept that drives comment wars — productively",
            "The self-honesty frame that hooks in under a second",
            "How liminal content earns 3x more profile visits",
        ],
    },
    "art_history": {
        "primary": [
            "Renaissance Proportion",
            "Baroque Dynamism",
            "Impressionist Light Capture",
            "Expressionist Distortion",
            "Minimalist Reduction",
            "Surrealist Displacement",
            "Pop Art Repetition",
            "Abstract Expressionist Gesture",
            "Constructivist Geometry",
            "Romantic Sublime",
            "Art Nouveau Organic Line",
            "Bauhaus Functional Form",
        ],
        "secondary": [
            "Gombrich's Schema & Correction",
            "Berger's Ways of Seeing",
            "Benjamin's Mechanical Reproduction",
            "Greenberg's Flatness",
            "Panofsky's Iconology",
            "Warburg's Pathosformel",
        ],
        "hooks": [
            "A 500-year-old composition trick that still stops the scroll",
            "Why Baroque diagonal energy outperforms centred shots",
            "The Impressionist technique that doubles thumbnail CTR",
            "One art movement that predicted how algorithms rank content",
            "The Bauhaus principle that makes minimalist reels loop",
        ],
    },
    "art_basics": {
        "primary": [
            "Line Weight Hierarchy",
            "Negative Space Activation",
            "Value Contrast Drama",
            "Shape Language Character",
            "Texture-Depth Illusion",
            "Proportion and Scale Play",
            "Rhythm Through Repetition",
            "Balance Asymmetry",
            "Unity-Variety Tension",
            "Focal Point Direction",
            "Emphasis Through Isolation",
            "Movement Path Design",
        ],
        "secondary": [
            "Arnheim's Visual Thinking",
            "Dondis' Primer of Visual Literacy",
            "McCloud's Understanding Comics",
            "Itten's Design and Form",
            "Kandinsky's Point and Line to Plane",
            "Wong's Principles of Form and Design",
        ],
        "hooks": [
            "The one element of design that controls where eyes go first",
            "Why negative space creates more engagement than filling the frame",
            "A line weight trick that makes thumbnails pop at any size",
            "The shape language that signals 'watch this' to the brain",
            "How visual rhythm keeps viewers past the 3-second mark",
        ],
    },
    "art_theory": {
        "primary": [
            "Formalist Tension",
            "Semiotic Layering",
            "Relational Aesthetics",
            "Institutional Critique Reframe",
            "Affect Theory Engagement",
            "Object-Oriented Ontology",
            "Post-Internet Vernacular",
            "Participatory Authorship",
            "Glitch Aesthetics",
            "Haptic Visuality",
            "Speculative Design Provocation",
            "Situationist Détournement",
        ],
        "secondary": [
            "Rancière's Distribution of the Sensible",
            "Bourriaud's Relational Frame",
            "Krauss's Expanded Field",
            "Bishop's Participatory Tension",
            "Steyerl's Poor Image",
            "Groys' Art Power",
        ],
        "hooks": [
            "The art theory concept that explains why shitposts outperform polished content",
            "Why participatory framing triples comment rate",
            "One semiotic layer that turns a reel into a save-worthy reference",
            "The glitch aesthetic principle behind every viral edit style",
            "How affect theory explains the 3-second watch rate",
        ],
    },
    "photography": {
        "primary": [
            "Decisive Moment Capture",
            "Light Direction Drama",
            "Depth-of-Field Isolation",
            "Leading Line Pull",
            "Framing Within Framing",
            "Juxtaposition Narrative",
            "Tonal Range Mapping",
            "Color Grading Mood",
            "Perspective Distortion",
            "Gesture and Timing",
            "Environmental Context",
            "Negative Space Breath",
        ],
        "secondary": [
            "Cartier-Bresson's Geometry",
            "Adams' Zone System",
            "Salgado's Light Philosophy",
            "Eggleston's Democratic Camera",
            "Avedon's Directness",
            "Leibovitz's Narrative Staging",
        ],
        "hooks": [
            "The light angle that increases thumbnail click-through by 60%",
            "Why shallow depth of field keeps people watching longer",
            "One compositional pull that directs attention for 8 full seconds",
            "The gesture timing that turns a still into a share",
            "How framing within framing creates the loop impulse",
        ],
    },
    "cinematography": {
        "primary": [
            "Camera Movement as Emotion",
            "Lens Choice as Character",
            "Lighting Ratio Mood",
            "Blocking Geometry",
            "Frame Depth Layering",
            "Colour Palette Storytelling",
            "Aspect Ratio Tension",
            "Handheld vs Locked Intention",
            "Practical Light Authenticity",
            "Rack Focus Revelation",
            "Silhouette Reduction",
            "Overhead Flattening",
        ],
        "secondary": [
            "Deakins' Natural Light Approach",
            "Lubezki's Continuous Take",
            "Kaminski's Expressionist Shadow",
            "Richardson's Grain Texture",
            "Storaro's Colour Philosophy",
            "Delbonnel's Desaturation Grammar",
        ],
        "hooks": [
            "The one camera move that holds attention 40% longer than a static shot",
            "Why practical lighting outperforms studio setups in retention",
            "A single lens choice that signals authenticity in under a second",
            "The blocking pattern that creates rewatch compulsion",
            "How Deakins-style lighting works at 9:16",
        ],
    },
    "content_creation": {
        "primary": [
            "Hook-Body-Payoff Structure",
            "Pattern Interrupt Cadence",
            "Information Density Control",
            "Emotional Arc Compression",
            "Call-to-Action Placement",
            "Authenticity Signal",
            "Controversy Calibration",
            "Series Cliffhanger",
            "Value-First Framing",
            "Parasocial Intimacy",
            "Platform-Native Format",
            "Remix and Response Culture",
        ],
        "secondary": [
            "MrBeast's Retention Engineering",
            "Ali Abdaal's Value Ladder",
            "Gary Vee's Volume Doctrine",
            "Hormozi's Offer Frame",
            "Paddy Galloway's Structure Analysis",
            "Colin and Samir's Creator Economy Model",
        ],
        "hooks": [
            "The content structure that turns viewers into followers in one video",
            "Why posting at volume beats posting perfect — with data",
            "One pacing technique that lifts completion by 30%",
            "The authenticity signal that converts saves into follows",
            "How the hook-payoff ratio decides your algorithmic bucket",
        ],
    },
    "movie_making": {
        "primary": [
            "Three-Act Compression",
            "Visual Storytelling Priority",
            "Sound Design Immersion",
            "Production Value Efficiency",
            "Casting as Character Shorthand",
            "Location as Character",
            "Practical Effects Presence",
            "Pacing Through Editing",
            "Score-Picture Sync",
            "Genre Convention Subversion",
            "Opening Image Promise",
            "Final Image Resolution",
        ],
        "secondary": [
            "McKee's Story Principles",
            "Snyder's Save the Cat Beats",
            "Mamet's Practical Aesthetics",
            "Rodriguez's Rebel Approach",
            "Nolan's Structural Complexity",
            "Gerwig's Character-First Method",
        ],
        "hooks": [
            "The opening image rule that predicts viral completion rate",
            "Why 18-second films follow three-act structure unconsciously",
            "One sound design trick that adds 0.8 loops per viewer",
            "The production shortcut that looks expensive for nothing",
            "How genre subversion drives the comment-to-view ratio",
        ],
    },
    "directing": {
        "primary": [
            "Performance Extraction",
            "Blocking as Subtext",
            "Rhythm and Pace Control",
            "Tone Consistency Management",
            "Collaboration as Amplifier",
            "Constraint as Creativity",
            "Emotional Throughline",
            "Visual Motif Repetition",
            "Audience Expectation Navigation",
            "Improvisation Permission",
            "Coverage Efficiency",
            "Decisive Restraint",
        ],
        "secondary": [
            "Kubrick's Obsessive Control",
            "Spielberg's Empathy Engine",
            "Fincher's Precision Framework",
            "Zhao's Naturalism",
            "Villeneuve's Scale-Intimacy Balance",
            "Peele's Genre Inversion",
        ],
        "hooks": [
            "The directing choice that makes non-actors look professional in 3 seconds",
            "Why constraint creates more shareable content than freedom",
            "One rhythm decision that determines if a video loops or dies",
            "The emotional throughline technique that earns saves",
            "How restraint signals quality faster than spectacle",
        ],
    },
}

# ---------------------------------------------------------------------------
# Cognitive biases and triggers shared across all domains
# ---------------------------------------------------------------------------

COGNITIVE_BIASES = [
    "Anchoring Effect",
    "Availability Cascade",
    "Bandwagon Effect",
    "Confirmation Bias",
    "Curiosity Gap",
    "FOMO Trigger",
    "Halo Effect",
    "IKEA Effect",
    "Loss Aversion",
    "Mere Exposure Effect",
    "Peak-End Rule",
    "Recency Bias",
    "Scarcity Principle",
    "Social Proof",
    "Sunk Cost Framing",
    "Zeigarnik Tension",
]

PALETTES = [
    "Warm Analogous",
    "Cool Monochromatic",
    "High-Contrast Complementary",
    "Muted Triadic",
    "Earth Tone Gradient",
    "Neon on Dark",
    "Desaturated Pastel",
    "Split Complementary Bold",
]

AUDIO_ANCHORS = [
    "Ascending Piano Motif",
    "Sub-Bass Pulse",
    "Vinyl Crackle Texture",
    "Silence Break",
    "Rhythmic Vocal Chop",
    "Ambient Pad Swell",
    "Percussive Stab",
    "Lo-Fi Tape Hiss",
]


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


#: How far a single creative choice may move the underlying quality, on a 0..1
#: scale where the run-to-run noise is 0.18. Small on purpose: a lever that
#: explained most of the outcome would make the dataset a lookup table, and a
#: lever that explained none of it — which is what this generator used to
#: produce — makes it useless for its stated job.
LEVER_EFFECT = 0.16


def _effect(option: str, salt: str) -> float:
    """This option's effect on quality, in -1..1. Stable for a given name.

    Derived from a hash of the name rather than stored in a table, so the
    vocabularies above can grow without anybody hand-assigning an effect to
    every new entry. Uses blake2b rather than `hash()` because `hash()` on a
    string is salted per process, which is the bug that made this generator
    produce different data on every run while documenting a fixed seed.
    """
    digest = hashlib.blake2b(f"{salt}:{option}".encode(), digest_size=8).digest()
    return (int.from_bytes(digest, "big") / 2**63) - 1.0


def _natural_watch_time(rng: random.Random, hook_quality: float) -> float:
    """Average watch time as a percentage of the video's length.

    Above 100% is not an error: on looping platforms a viewer who watches twice
    is counted twice, and that is exactly the behaviour the loop objective
    exists to chase. The median lands near 86% with a long left tail.
    """
    base = 55.0 + hook_quality * 45.0
    noise = rng.gauss(0.0, 12.0)
    return _clamp(base + noise, 15.0, 145.0)


def _natural_share_rate(rng: random.Random, watch_pct: float) -> float:
    """Share rate derived from watch time — shares follow completion."""
    base = 0.01 + (watch_pct / 100.0) * 0.06
    return _clamp(base + rng.gauss(0.0, 0.012), 0.0, 0.18)


def _natural_save_rate(rng: random.Random, share: float, domain_depth: float) -> float:
    """Save rate — domain depth increases save tendency (reference material)."""
    base = share * 1.4 + domain_depth * 0.02
    return _clamp(base + rng.gauss(0.0, 0.008), 0.0, 0.15)


def _natural_comment_rate(rng: random.Random, controversy: float) -> float:
    """Comment rate driven by controversy/engagement tension."""
    base = 0.02 + controversy * 0.05
    return _clamp(base + rng.gauss(0.0, 0.01), 0.0, 0.12)


def _natural_ctr(rng: random.Random, visual_strength: float) -> float:
    """Click-through rate from thumbnail/initial frame strength."""
    base = 3.0 + visual_strength * 8.0
    return _clamp(base + rng.gauss(0.0, 1.5), 0.5, 18.0)


def generate_domain(
    domain_key: str,
    *,
    rows: int = 200,
    seed: int | None = None,
) -> list[dict]:
    """Generate rows for a single domain."""
    domain = DOMAINS[domain_key]
    # `hash()` on a string is salted per process, so a default seed derived from
    # it made this function return different data on every run while its
    # signature promised determinism.
    if seed is None:
        seed = int.from_bytes(hashlib.blake2b(domain_key.encode(), digest_size=4).digest(), "big")
    rng = random.Random(seed)

    results: list[dict] = []
    for i in range(rows):
        primary = rng.choice(domain["primary"])
        secondary = rng.choice(domain["secondary"])
        hook = rng.choice(domain["hooks"])
        bias = rng.choice(COGNITIVE_BIASES)
        palette = rng.choice(PALETTES)
        audio = rng.choice(AUDIO_ANCHORS)

        # The creative choices have to *do* something, or this dataset cannot
        # do the job it exists for. Every one of these was previously drawn by
        # `rng.choice` and then never referred to again, so palette, bias,
        # audio anchor and primary lever were noise columns bolted onto
        # unrelated performance numbers. A crew trained on that learns the one
        # lesson the data actually contained: nothing you choose matters.
        #
        # Each lever now shifts the underlying quality by a fixed amount that
        # depends on the option, and different levers feed different qualities
        # — the palette moves how the frame looks, the audio anchor moves how
        # long people stay, the bias moves whether they argue about it.
        hook_quality = _clamp(
            rng.gauss(0.65, 0.18)
            + _effect(hook, "hook") * LEVER_EFFECT
            + _effect(bias, "bias") * LEVER_EFFECT * 0.6
        )
        domain_depth = _clamp(
            rng.gauss(0.55, 0.20)
            + _effect(primary, "primary") * LEVER_EFFECT
            + _effect(secondary, "secondary") * LEVER_EFFECT * 0.5
        )
        visual_strength = _clamp(
            rng.gauss(0.60, 0.20)
            + _effect(palette, "palette") * LEVER_EFFECT
            + _effect(primary, "primary") * LEVER_EFFECT * 0.4
        )
        controversy = _clamp(rng.gauss(0.35, 0.20) + _effect(bias, "bias") * LEVER_EFFECT)
        retention = _clamp(rng.gauss(0.0, 0.10) + _effect(audio, "audio") * LEVER_EFFECT)

        watch_pct = _natural_watch_time(rng, hook_quality + retention * 0.5)
        share = _natural_share_rate(rng, watch_pct)
        save = _natural_save_rate(rng, share, domain_depth)
        comment = _natural_comment_rate(rng, controversy)
        ctr = _natural_ctr(rng, visual_strength)

        # Simulated view counts with lognormal distribution
        views = int(rng.lognormvariate(math.log(8000), 0.9))

        results.append(
            {
                "metadata_id": f"{domain_key}_{i:04d}",
                f"primary_{domain_key}": primary,
                f"secondary_{domain_key}": secondary,
                "hook_text": hook,
                "cognitive_bias_exploited": bias,
                "color_theory_palette": palette,
                "music_theory_audio_anchor": audio,
                "avg_watch_time_pct": round(watch_pct, 1),
                "simulated_views": views,
                "simulated_shares": int(views * share),
                "simulated_saves": int(views * save),
                "simulated_comments": int(views * comment),
                "click_through_rate_pct": round(ctr, 2),
                # The rates as well as the counts. Every count here is
                # `views × rate`, and views is lognormal with a 2.5x spread, so
                # correlating the counts measures the view multiplier and
                # nothing else — the generator's own "shares follow completion"
                # relationship came out at r = 0.10 in the counts while being
                # true by construction. The ratios are what the insight layer
                # actually wants, and what a real export reports.
                "share_to_view_ratio": round(share, 4),
                "save_to_view_ratio": round(save, 4),
                "three_second_watch_rate": round(_clamp(0.45 + hook_quality * 0.5), 3),
            }
        )

    return results


def generate_all(
    output_dir: Path,
    *,
    rows_per_domain: int = 200,
    seed: int = 0xA7E5,
) -> list[Path]:
    """Generate CSVs for all sixteen domains."""
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    for i, domain_key in enumerate(DOMAINS):
        rows = generate_domain(domain_key, rows=rows_per_domain, seed=seed + i)
        path = output_dir / f"viral_metadata_{domain_key}.csv"

        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

        paths.append(path)

    return paths


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate natural training metadata for all sixteen creative disciplines."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("auteur-exports/training"),
        help="Directory to write the CSV files into (default: auteur-exports/training)",
    )
    parser.add_argument(
        "--rows-per-domain",
        type=int,
        default=200,
        help="Number of rows to generate per domain (default: 200)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0xA7E5,
        help="Random seed for reproducibility",
    )
    args = parser.parse_args()

    paths = generate_all(args.output_dir, rows_per_domain=args.rows_per_domain, seed=args.seed)
    total = args.rows_per_domain * len(DOMAINS)
    print(f"Generated {total:,} rows across {len(paths)} domains:")
    for path in paths:
        print(f"  {path}")


if __name__ == "__main__":
    main()
