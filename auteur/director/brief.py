"""Reading the brief.

A prompt like *"moody neon chase, 20 seconds, ends on the sign"* carries an
enormous amount of editorial instruction. This module makes that instruction
explicit — pace, energy shape, palette, transition vocabulary, runtime — so the
rest of the system is never guessing at intent.

It is intentionally a shallow parser: it extracts what it is confident about
and leaves the rest at sensible cinematic defaults. When a model is available
it fills in the interpretation; when it is not, these defaults still produce a
coherent film.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

#: Named energy shapes, sampled over the normalised runtime.
ARCS: dict[str, str] = {
    "hook-drop": "open hot, pull back, build, finish hardest — the short-form default",
    "crescendo": "start quiet, end loud",
    "trailer": "slow burn, hard escalation, sting",
    "wave": "repeated swells",
    "calm": "even and unhurried throughout",
    "decay": "start loud, settle into stillness",
}

#: Words that move the pace dial, and the average shot length they imply.
PACE_WORDS: dict[str, float] = {
    "frenetic": 0.42,
    "frantic": 0.42,
    "hyper": 0.45,
    "chaotic": 0.45,
    "rapid": 0.5,
    "fast": 0.55,
    "punchy": 0.55,
    "snappy": 0.55,
    "energetic": 0.6,
    "hype": 0.55,
    "kinetic": 0.55,
    "quick": 0.6,
    "upbeat": 0.7,
    "brisk": 0.7,
    "steady": 1.1,
    "measured": 1.3,
    "calm": 1.8,
    "slow": 2.2,
    "languid": 2.6,
    "meditative": 2.8,
    "contemplative": 2.8,
    "gentle": 2.0,
    "patient": 2.4,
    "epic": 1.4,
    "cinematic": 1.3,
    "dramatic": 1.2,
    "moody": 1.6,
    "atmospheric": 1.8,
}

#: Look vocabulary → grade preset (see craft.color.LOOKS).
LOOK_WORDS: dict[str, str] = {
    "teal": "blockbuster",
    "orange": "blockbuster",
    "blockbuster": "blockbuster",
    "action": "blockbuster",
    "summer": "blockbuster",
    "noir": "noir",
    "black and white": "noir",
    "monochrome": "noir",
    "b&w": "noir",
    "moody": "moody",
    "brooding": "moody",
    "somber": "moody",
    "sombre": "moody",
    "cold": "steel",
    "steel": "steel",
    "clinical": "steel",
    "bleak": "steel",
    "nolan": "steel",
    "heist": "steel",
    "thriller": "steel",
    "warm": "amber",
    "amber": "amber",
    "golden": "amber",
    "nostalgic": "amber",
    "spielberg": "amber",
    "wonder": "amber",
    "summer evening": "amber",
    "neon": "neon",
    "cyberpunk": "neon",
    "synthwave": "neon",
    "night city": "neon",
    "vintage": "kodak",
    "film": "kodak",
    "16mm": "kodak",
    "35mm": "kodak",
    "retro": "kodak",
    "analog": "kodak",
    "analogue": "kodak",
    "bleach": "bleach-bypass",
    "gritty": "bleach-bypass",
    "war": "bleach-bypass",
    "dreamy": "bloom",
    "ethereal": "bloom",
    "soft": "bloom",
    "romantic": "bloom",
    "vibrant": "punch",
    "punchy": "punch",
    "bold": "punch",
    "pop": "punch",
    "desert": "desert",
    "arid": "desert",
    "western": "desert",
    "underwater": "aqua",
    "ocean": "aqua",
    "aquatic": "aqua",
}

STYLE_WORDS: dict[str, str] = {
    "trailer": "trailer",
    "teaser": "trailer",
    "music video": "music-video",
    "montage": "montage",
    "recap": "montage",
    "documentary": "documentary",
    "doc": "documentary",
    "interview": "documentary",
    "vlog": "vlog",
    "day in the life": "vlog",
    "advert": "commercial",
    "ad": "commercial",
    "commercial": "commercial",
    "brand": "commercial",
    "travel": "travel",
    "highlight": "highlights",
    "highlights": "highlights",
    "tutorial": "explainer",
    "explainer": "explainer",
    "how to": "explainer",
    # The cadence the reference reels are actually cut at. Named for what
    # people call it rather than for anything technical.
    "hypercut": "hypercut",
    "hyper cut": "hypercut",
    "flurry": "hypercut",
    "rapid fire": "hypercut",
    "rapid-fire": "hypercut",
    "machine gun": "hypercut",
    "reel": "hypercut",
}


def _energy(arc: str, position: float) -> float:
    """Sample a named energy curve at a normalised position."""
    p = min(max(position, 0.0), 1.0)
    if arc == "crescendo":
        return 0.18 + 0.82 * p**1.4
    if arc == "trailer":
        # Long quiet, then a hard turn at two thirds, then a held sting.
        if p < 0.55:
            return 0.15 + 0.25 * (p / 0.55)
        if p < 0.9:
            return 0.4 + 0.6 * ((p - 0.55) / 0.35) ** 0.7
        return 0.85 - 0.35 * ((p - 0.9) / 0.1)
    if arc == "wave":
        return 0.5 + 0.42 * math.sin(p * math.tau * 1.5 - math.pi / 2)
    if arc == "calm":
        return 0.32 + 0.12 * math.sin(p * math.tau)
    if arc == "decay":
        return 0.92 - 0.7 * p**0.8
    # hook-drop: the shape almost every good short-form edit actually has.
    if p < 0.12:
        return 0.88  # the hook, at full tilt
    if p < 0.28:
        return 0.88 - 0.42 * ((p - 0.12) / 0.16)  # let it breathe
    return 0.46 + 0.5 * ((p - 0.28) / 0.72) ** 1.25  # build to the finish


@dataclass
class Brief:
    """Structured intent, derived from the prompt."""

    prompt: str
    title: str = "untitled"
    style: str = "montage"
    arc: str = "hook-drop"
    #: Average screen time per shot, in seconds, before the energy curve bends it.
    base_shot_length: float = 0.9
    look: str = "neutral"
    look_strength: float = 1.0
    texture: float = 0.12
    letterbox: float = 0.0
    duration: float | None = None
    #: Transition vocabulary this film is allowed to use, in preference order.
    transitions: tuple[str, ...] = (
        "cut",
        "cut",
        "cut",
        "dissolve",
        "portal",
        "whip-left",
        "dip-to-black",
    )
    #: Strings the director asked to appear on screen, in order.
    on_screen_text: list[str] = field(default_factory=list)
    #: Explicit instructions we recognised but did not consume.
    notes: list[str] = field(default_factory=list)
    #: Cut hard to the music when a beat grid exists.
    beat_sync: bool = True
    #: Allow speed ramping.
    ramps: bool = True
    #: Prefer keeping the clips' own audio (dialogue-led edits).
    keep_source_audio: bool = False

    def energy_at(self, position: float) -> float:
        return _energy(self.arc, position)

    def shot_length_at(self, position: float) -> float:
        """Screen time for a shot starting at this point in the film.

        High energy means short shots. The curve is steep on purpose: a shot
        length that only varies between, say, 0.4s and 0.7s reads as metronomic
        no matter how well chosen the frames are. Across a full arc this spans
        roughly 4:1, which is the difference between a held beat and a flurry.
        """
        energy = self.energy_at(position)
        return float(self.base_shot_length * (2.3 - 1.9 * energy) ** 1.6)

    def describe(self) -> str:
        bits = [
            f"style={self.style}",
            f"arc={self.arc}",
            f"look={self.look}",
            f"~{self.base_shot_length:.2f}s/shot",
        ]
        if self.duration:
            bits.append(f"target={self.duration:.0f}s")
        if self.on_screen_text:
            bits.append(f"{len(self.on_screen_text)} text cue(s)")
        return " · ".join(bits)


#: What a runtime may be. Below three seconds there is no room for an edit;
#: above fifteen minutes this is not the tool for the job.
MIN_RUNTIME = 3.0
MAX_RUNTIME = 900.0


def clamp_duration(value: float | None) -> float | None:
    """A runtime, or None when there isn't a usable one.

    Both routes to a duration end here — the number read out of the prompt and
    the one passed in by hand — because only one of them used to be checked.
    `--length -5` went straight through to the planner, which made a film of
    whatever length it could and then reported that it "came out the wrong
    length" against a target of minus five seconds.
    """
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN, ±inf
        return None
    if value <= 0:
        return None
    return min(max(value, MIN_RUNTIME), MAX_RUNTIME)


#: Decades. `90s` in "a 90s hypercut" is the nineties, not ninety seconds.
#:
#: Read as a runtime it produced a 324-shot, 90-second film from a prompt that
#: also said "12 seconds" — the bare-`s` branch matched `90s` first and never
#: reached the words the person actually wrote. Every era this program offers
#: is spelled this way, so the token is claimed by the era vocabulary and the
#: runtime parser must not claim it a second time.
#:
#: This does cost "make it 30s" as a bare runtime, which is a real if small
#: loss. It is the right trade: `30s` is equally the 1930s, "30 seconds" still
#: parses, and reading a decade as a minute of footage is the louder failure.
_DECADE = re.compile(r"\b(?:19|20)?\d0s\b")


def _extract_duration(text: str) -> float | None:
    """Pull a runtime out of the prompt: '30 seconds', '15s', 'a minute and a half'."""
    lowered = text.lower()
    # An explicit unit beats a bare `s`, always — and it is how anyone naming
    # both a decade and a length writes the length.
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:seconds?|secs?)\b", lowered)
    if not match:
        match = re.search(r"(\d+(?:\.\d+)?)\s*s\b", _DECADE.sub(" ", lowered))
    if match:
        value = float(match.group(1))
        if MIN_RUNTIME <= value <= MAX_RUNTIME:
            return value
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|m)\b", lowered)
    if match:
        value = float(match.group(1)) * 60
        if MIN_RUNTIME <= value <= MAX_RUNTIME:
            return value
    if "minute and a half" in lowered or "90 second" in lowered:
        return 90.0
    if "half a minute" in lowered:
        return 30.0
    if re.search(r"\ba minute\b", lowered):
        return 60.0
    return None


def _extract_quoted(text: str) -> list[str]:
    """On-screen text is whatever the director put in quotes."""
    found: list[str] = []
    for pattern in (r'"([^"]{1,80})"', r"“([^”]{1,80})”", r"'([^']{2,80})'"):
        found.extend(match.strip() for match in re.findall(pattern, text))
    seen: set[str] = set()
    unique: list[str] = []
    for item in found:
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[:8]


def _first_match(text: str, table: dict[str, str]) -> str | None:
    """Pick the keyword nearest the end of the phrase; longest wins ties.

    English stacks modifiers with the most defining one last: in "a moody neon
    chase" the look is neon and moody is the mood, and in "warm nostalgic film"
    it is the film stock that decides the grade. Preferring the later match
    reads that structure. Length still breaks ties, so "black and white" beats
    the "white" inside it.
    """
    lowered = f" {text.lower()} "
    best: tuple[int, int, str] | None = None
    for keyword, value in table.items():
        # The *last* occurrence wins: in "a moody neon chase" the look is neon
        # and moody is the mood, so a later word is the more specific one.
        found = list(re.finditer(rf"(?<![a-z]){re.escape(keyword)}(?![a-z])", lowered))
        match = found[-1] if found else None
        if match is None:
            continue
        candidate = (match.start(), len(keyword), value)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    return best[2] if best else None


def _pace(text: str) -> float | None:
    lowered = f" {text.lower()} "
    hits = [
        length
        for word, length in PACE_WORDS.items()
        if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", lowered)
    ]
    if not hits:
        return None
    # Several pace words: take the geometric mean so they temper each other.
    product = 1.0
    for value in hits:
        product *= value
    return product ** (1.0 / len(hits))


def _title_from(prompt: str) -> str:
    """A short slug for the file name and the report header."""
    cleaned = re.sub(r"[\"“”']", "", prompt).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return "untitled"
    words = cleaned.split(" ")[:7]
    return " ".join(words).rstrip(".,;:").title()


def _arc_for(text: str, style: str) -> str:
    lowered = text.lower()
    for name in ARCS:
        if name.replace("-", " ") in lowered:
            return name
    if any(word in lowered for word in ("build", "builds", "crescendo", "escalat")):
        return "crescendo"
    if any(word in lowered for word in ("wind down", "settle", "ends quiet", "fades out")):
        return "decay"
    if style == "trailer":
        return "trailer"
    if style in ("documentary", "explainer", "travel"):
        return "wave"
    if style == "vlog":
        return "calm"
    return "hook-drop"


def parse_brief(prompt: str, *, duration: float | None = None) -> Brief:
    """Turn a sentence of direction into something the system can execute."""
    prompt = (prompt or "").strip() or "a cinematic montage"
    lowered = prompt.lower()

    style = _first_match(prompt, STYLE_WORDS) or "montage"
    look = _first_match(prompt, LOOK_WORDS) or "neutral"
    arc = _arc_for(prompt, style)

    base = _pace(prompt)
    if base is None:
        base = {
            "trailer": 1.1,
            "documentary": 2.0,
            "explainer": 2.2,
            "vlog": 1.6,
            "commercial": 0.8,
            "music-video": 0.6,
            "highlights": 0.7,
            "travel": 1.4,
            "montage": 0.9,
            # Measured, not chosen. Across seventeen reference reels — with
            # their sign-off cards excluded, since a held card is not an edit —
            # the median shot runs 0.167s and the median rate is 27.8 cuts per
            # ten seconds. The fastest style before this was music-video at
            # 0.6s, which is three and a half times slower than the thing the
            # work is measured against.
            "hypercut": 0.167,
        }.get(style, 0.9)

    brief = Brief(
        prompt=prompt,
        title=_title_from(prompt),
        style=style,
        arc=arc,
        base_shot_length=base,
        look=look,
        duration=clamp_duration(duration if duration is not None else _extract_duration(prompt)),
        on_screen_text=_extract_quoted(prompt),
    )

    # Texture: film words earn grain, clean words remove it.
    if any(
        word in lowered
        for word in ("grain", "film", "16mm", "35mm", "analog", "analogue", "vintage")
    ):
        brief.texture = 0.45
    elif any(word in lowered for word in ("clean", "crisp", "digital", "sharp", "pristine")):
        brief.texture = 0.0

    if any(
        word in lowered
        for word in ("anamorphic", "widescreen", "2.35", "scope", "letterbox", "cinemascope")
    ):
        brief.letterbox = 0.11

    if any(
        word in lowered for word in ("no transitions", "hard cuts", "straight cuts", "cuts only")
    ):
        brief.transitions = ("cut",)
    elif style == "hypercut":
        # The references cut hard, and at a 0.167s median a shot has no room
        # for a transition — a 0.22s dissolve would outlast it. But the
        # reference reels are not *only* fast: they open a portal on the
        # handful of shots that are long enough to hold one, and cut
        # everything else. Leaving these in the bag and letting the length
        # ceiling below decide is what produces that, rather than a film
        # where every join is the same and the whole thing reads as a
        # slideshow on fast-forward.
        brief.transitions = ("cut", "cut", "cut", "cut", "cut", "portal", "carry")
    elif style in ("music-video", "highlights") or base < 0.7:
        brief.transitions = (
            "cut",
            "cut",
            "cut",
            "whip-left",
            "whip-right",
            "portal",
            "carry",
            "zoom-blur",
            "glitch",
        )
    elif style in ("documentary", "explainer", "travel") or base > 1.6:
        brief.transitions = ("cut", "cut", "dissolve", "dissolve", "carry", "dip-to-black")
    elif look in ("kodak", "bloom", "amber"):
        brief.transitions = ("cut", "cut", "dissolve", "light-leak", "carry", "film-burn")

    if any(word in lowered for word in ("no music", "silent", "natural sound", "no soundtrack")):
        brief.beat_sync = False
    if any(
        word in lowered
        for word in ("dialogue", "interview", "talking", "speech", "voice", "what they say")
    ):
        brief.keep_source_audio = True
    if any(
        word in lowered
        for word in ("no speed", "real time", "realtime", "no slow motion", "constant speed")
    ):
        brief.ramps = False

    for phrase in ("ends on", "end on", "open on", "opens on", "start with", "finish on"):
        if phrase in lowered:
            brief.notes.append(prompt[lowered.index(phrase) :][:90].strip())

    return brief
