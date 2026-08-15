"""The Edit Decision List: the film, written down before it exists.

Every creative decision in the system ends up here — which frames, in what
order, how fast, how they join, how they are graded, what is written over them
and what plays underneath. The director writes an EDL; the renderer conforms
it. Nothing else is allowed to be creative.

The model is deliberately strict and self-repairing. A language model can and
will invent a shot that runs past the end of a clip; ``EditDecisionList.repair``
turns that into a legal edit rather than a crash.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Iterable

log = logging.getLogger("auteur.edl")

MIN_SHOT = 0.25
MAX_SHOT = 12.0

TRANSITIONS = {
    "cut", "dissolve", "dip-to-black", "dip-to-white", "whip-left", "whip-right",
    "whip-up", "whip-down", "glitch", "light-leak", "zoom-blur", "film-burn",
    "slide-left", "slide-right", "wipe", "morph",
}
MOTIONS = {"none", "ken-burns", "punch-in", "pull-out", "drift-left", "drift-right", "float", "shake"}
REFRAMES = {"subject", "center", "fill", "blur-pad"}
TEXT_STYLES = {"title", "kinetic", "lower-third", "caption", "end-card", "chapter"}


def _clamp(value: float, low: float, high: float) -> float:
    return float(min(max(value, low), high))


@dataclass
class Transition:
    """How one shot becomes the next. Duration is the overlap it consumes."""

    kind: str = "cut"
    duration: float = 0.0

    def normalise(self) -> Transition:
        kind = (self.kind or "cut").strip().lower()
        if kind not in TRANSITIONS:
            kind = "dissolve" if self.duration > 0 else "cut"
        duration = 0.0 if kind == "cut" else _clamp(self.duration or 0.4, 0.08, 2.0)
        return Transition(kind, round(duration, 3))

    @property
    def is_cut(self) -> bool:
        return self.kind == "cut" or self.duration <= 0


@dataclass
class Motion:
    """Camera move applied in post — the shot that moves when the footage did not."""

    kind: str = "none"
    #: 0..1. How far the move travels.
    intensity: float = 0.35
    #: Normalised frame coordinates the move is built around.
    anchor: tuple[float, float] = (0.5, 0.5)

    def normalise(self) -> Motion:
        kind = (self.kind or "none").strip().lower()
        if kind not in MOTIONS:
            kind = "none"
        return Motion(
            kind=kind,
            intensity=_clamp(self.intensity, 0.0, 1.0),
            anchor=(_clamp(self.anchor[0], 0.0, 1.0), _clamp(self.anchor[1], 0.0, 1.0)),
        )


@dataclass
class Ramp:
    """A speed curve across a shot, as (position 0..1, speed) control points.

    ``[(0, 0.4), (0.5, 1.0), (1, 2.5)]`` is the classic ramp: land slow on the
    beat, accelerate out of it. Speed is a multiplier on source time.
    """

    points: list[tuple[float, float]] = field(default_factory=list)

    @staticmethod
    def constant(speed: float) -> Ramp:
        return Ramp([(0.0, speed), (1.0, speed)])

    @staticmethod
    def slow_in(from_speed: float = 0.45, to_speed: float = 1.0) -> Ramp:
        return Ramp([(0.0, from_speed), (0.55, to_speed), (1.0, to_speed)])

    @staticmethod
    def accelerate(from_speed: float = 1.0, to_speed: float = 2.4) -> Ramp:
        return Ramp([(0.0, from_speed), (0.45, from_speed), (1.0, to_speed)])

    @staticmethod
    def hit(slow: float = 0.35, fast: float = 1.8, at: float = 0.35) -> Ramp:
        """Snap to a crawl on the beat, then whip out of it."""
        return Ramp([(0.0, fast), (max(at - 0.08, 0.01), slow), (min(at + 0.18, 0.99), slow), (1.0, fast)])

    @property
    def is_flat(self) -> bool:
        speeds = {round(speed, 4) for _, speed in self.points}
        return len(self.points) < 2 or len(speeds) == 1

    @property
    def constant_speed(self) -> float:
        return self.points[0][1] if self.points else 1.0

    def normalise(self) -> Ramp:
        cleaned: list[tuple[float, float]] = []
        for position, speed in self.points:
            cleaned.append((_clamp(position, 0.0, 1.0), _clamp(speed, 0.15, 8.0)))
        cleaned.sort(key=lambda p: p[0])
        if not cleaned:
            return Ramp.constant(1.0)
        if cleaned[0][0] > 0.0:
            cleaned.insert(0, (0.0, cleaned[0][1]))
        if cleaned[-1][0] < 1.0:
            cleaned.append((1.0, cleaned[-1][1]))
        return Ramp(cleaned)

    def speed_at(self, position: float) -> float:
        """Linear interpolation between control points."""
        points = self.points or [(0.0, 1.0), (1.0, 1.0)]
        position = _clamp(position, 0.0, 1.0)
        for index in range(len(points) - 1):
            (p0, s0), (p1, s1) = points[index], points[index + 1]
            if p0 <= position <= p1:
                if p1 - p0 < 1e-9:
                    return s1
                t = (position - p0) / (p1 - p0)
                return s0 + (s1 - s0) * t
        return points[-1][1]

    def output_duration(self, source_duration: float, slices: int = 64) -> float:
        """Elapsed screen time for a source range played through this ramp.

        Screen time is the integral of dt_source / speed(t), approximated with
        the same slicing the renderer uses so the two always agree.
        """
        if source_duration <= 0:
            return 0.0
        if self.is_flat:
            return source_duration / max(self.constant_speed, 1e-6)
        step = source_duration / slices
        total = 0.0
        for index in range(slices):
            position = (index + 0.5) / slices
            total += step / max(self.speed_at(position), 1e-6)
        return total


@dataclass
class Look:
    """Grade for a shot or the whole film."""

    #: Named film emulation, see craft.color.LOOKS.
    preset: str = "neutral"
    #: Per-shot corrections applied *before* the preset, to make shots match.
    exposure: float = 0.0     # stops, -1..+1
    temperature: float = 0.0  # -1 cool .. +1 warm
    saturation: float = 0.0   # -1..+1 relative
    contrast: float = 0.0     # -1..+1 relative
    #: 0..1 strength of the named preset.
    strength: float = 1.0

    def normalise(self) -> Look:
        return Look(
            preset=(self.preset or "neutral").strip().lower(),
            exposure=_clamp(self.exposure, -1.0, 1.0),
            temperature=_clamp(self.temperature, -1.0, 1.0),
            saturation=_clamp(self.saturation, -1.0, 1.0),
            contrast=_clamp(self.contrast, -1.0, 1.0),
            strength=_clamp(self.strength, 0.0, 1.0),
        )

    @property
    def is_identity(self) -> bool:
        return (
            self.preset in ("neutral", "none", "")
            and abs(self.exposure) < 0.01
            and abs(self.temperature) < 0.01
            and abs(self.saturation) < 0.01
            and abs(self.contrast) < 0.01
        )


@dataclass
class Shot:
    """One cut of picture on the timeline."""

    clip_id: str
    source: Path
    start: float
    end: float

    ramp: Ramp = field(default_factory=lambda: Ramp.constant(1.0))
    motion: Motion = field(default_factory=Motion)
    reframe: str = "subject"
    look: Look = field(default_factory=Look)
    transition_in: Transition = field(default_factory=Transition)

    #: Use the clip's own sound, and at what gain.
    use_source_audio: bool = False
    audio_gain: float = 1.0
    #: Audio leads (negative) or trails (positive) the picture cut, in seconds.
    audio_offset: float = 0.0

    #: Freeform note from the director. Carried through to the timeline report.
    note: str = ""
    #: Set when the shot is a still being held.
    is_still: bool = False

    @property
    def source_duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def duration(self) -> float:
        """Screen time, after speed."""
        return self.ramp.output_duration(self.source_duration)

    def normalise(self) -> Shot:
        self.ramp = self.ramp.normalise()
        self.motion = self.motion.normalise()
        self.look = self.look.normalise()
        self.transition_in = self.transition_in.normalise()
        if self.reframe not in REFRAMES:
            self.reframe = "subject"
        self.audio_gain = _clamp(self.audio_gain, 0.0, 4.0)
        self.audio_offset = _clamp(self.audio_offset, -1.5, 1.5)
        return self


@dataclass
class TextCue:
    """Words on screen. Timed against the finished timeline, not the source."""

    text: str
    start: float
    duration: float = 2.0
    style: str = "title"
    #: Normalised position of the text block's centre.
    anchor: tuple[float, float] = (0.5, 0.5)
    size: float = 1.0        # relative to the style's default
    color: str = "#FFFFFF"
    accent: str = "#FFFFFF"
    #: For kinetic captions: reveal one word at a time.
    per_word: bool = False

    def normalise(self) -> TextCue:
        self.text = (self.text or "").strip()
        self.style = (self.style or "title").strip().lower()
        if self.style not in TEXT_STYLES:
            self.style = "title"
        self.start = max(0.0, self.start)
        self.duration = _clamp(self.duration, 0.3, 20.0)
        self.anchor = (_clamp(self.anchor[0], 0.02, 0.98), _clamp(self.anchor[1], 0.02, 0.98))
        self.size = _clamp(self.size, 0.35, 3.0)
        return self

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class MusicCue:
    """The bed the whole thing is cut to."""

    source: Path | None = None
    #: Seconds into the track where the film starts.
    offset: float = 0.0
    gain: float = 0.85
    fade_in: float = 0.35
    fade_out: float = 1.2
    #: Duck the music under source dialogue.
    duck: bool = True
    duck_amount: float = 0.55


@dataclass
class SoundCue:
    """A designed effect: whoosh, impact, sub-drop, riser. Synthesised, not sampled."""

    kind: str  # whoosh | impact | sub-drop | riser | tick
    at: float
    gain: float = 0.7
    duration: float = 0.5


@dataclass
class EditDecisionList:
    """The whole film."""

    title: str = "untitled"
    shots: list[Shot] = field(default_factory=list)
    texts: list[TextCue] = field(default_factory=list)
    music: MusicCue = field(default_factory=MusicCue)
    sfx: list[SoundCue] = field(default_factory=list)
    look: Look = field(default_factory=Look)
    #: Grain / halation / bloom, 0..1.
    texture: float = 0.0
    #: Letterbox bars, as a fraction of frame height (0.11 ≈ 2.35:1 in 16:9).
    letterbox: float = 0.0
    fps: int = 30
    width: int = 1080
    height: int = 1920
    #: Whatever the director wants to say about why the edit is what it is.
    rationale: str = ""

    # ---------------------------------------------------------------- timing

    @property
    def duration(self) -> float:
        """Finished runtime, accounting for transition overlaps."""
        total = 0.0
        for index, shot in enumerate(self.shots):
            total += shot.duration
            if index > 0 and not shot.transition_in.is_cut:
                total -= shot.transition_in.duration
        return max(0.0, total)

    def timeline(self) -> list[tuple[float, float, Shot]]:
        """(start, end, shot) on the finished timeline."""
        out: list[tuple[float, float, Shot]] = []
        cursor = 0.0
        for index, shot in enumerate(self.shots):
            if index > 0 and not shot.transition_in.is_cut:
                cursor -= shot.transition_in.duration
            out.append((cursor, cursor + shot.duration, shot))
            cursor += shot.duration
        return out

    def cut_times(self) -> list[float]:
        return [start for start, _, _ in self.timeline()[1:]]

    # ------------------------------------------------------------ validation

    def repair(self, sources: dict[str, Any] | None = None, *, target_duration: float | None = None) -> list[str]:
        """Force the EDL to be renderable. Returns the list of repairs made.

        `sources` maps clip_id -> object exposing `.asset.path` and `.duration`
        (a ClipDossier). When supplied, shot ranges are clamped to what the
        footage can actually supply.
        """
        notes: list[str] = []
        legal: list[Shot] = []

        for shot in self.shots:
            shot.normalise()

            if sources is not None:
                dossier = sources.get(shot.clip_id)
                if dossier is None:
                    notes.append(f"dropped shot referencing unknown clip {shot.clip_id!r}")
                    continue
                shot.source = Path(dossier.asset.path)
                shot.is_still = dossier.asset.kind == "image"
                limit = float(dossier.duration)
                if shot.is_still:
                    # A still can be held for as long as we like.
                    shot.start = max(0.0, shot.start)
                    shot.end = max(shot.start + MIN_SHOT, shot.end)
                else:
                    if shot.end > limit:
                        notes.append(
                            f"{shot.clip_id}: out point {shot.end:.2f}s past end of clip ({limit:.2f}s)"
                        )
                    shot.start = _clamp(shot.start, 0.0, max(0.0, limit - MIN_SHOT))
                    shot.end = _clamp(shot.end, shot.start + MIN_SHOT, limit)

            if shot.end <= shot.start:
                notes.append(f"dropped zero-length shot on {shot.clip_id}")
                continue
            if shot.source_duration > MAX_SHOT:
                shot.end = shot.start + MAX_SHOT
                notes.append(f"{shot.clip_id}: trimmed to the {MAX_SHOT:.0f}s ceiling")
            if shot.duration < MIN_SHOT:
                notes.append(f"dropped {shot.clip_id}: {shot.duration:.2f}s of screen time is a flash frame")
                continue
            legal.append(shot)

        if legal:
            # Nothing can dissolve into the first shot; there is nothing behind it.
            legal[0].transition_in = Transition("cut", 0.0)
            for index in range(1, len(legal)):
                shot = legal[index]
                if shot.transition_in.is_cut:
                    continue
                # A transition cannot be longer than the shots it joins.
                ceiling = min(shot.duration, legal[index - 1].duration) * 0.5
                if shot.transition_in.duration > ceiling:
                    shot.transition_in = Transition(shot.transition_in.kind, round(max(ceiling, 0.0), 3))
                    if shot.transition_in.duration < 0.08:
                        shot.transition_in = Transition("cut", 0.0)
                        notes.append(f"shot {index + 1}: transition too long for the shots, made it a cut")

        self.shots = legal

        if not self.shots:
            raise ValueError("EDL contains no renderable shots")

        runtime = self.duration
        for cue in self.texts:
            cue.normalise()
        # Text that runs past the end of the film is text nobody sees.
        kept: list[TextCue] = []
        for cue in self.texts:
            if not cue.text:
                continue
            if cue.start >= runtime - 0.15:
                notes.append(f"dropped text {cue.text[:24]!r}: starts after the film ends")
                continue
            cue.duration = min(cue.duration, runtime - cue.start)
            kept.append(cue)
        self.texts = kept

        self.sfx = [cue for cue in self.sfx if 0.0 <= cue.at < runtime]
        self.look = self.look.normalise()
        self.texture = _clamp(self.texture, 0.0, 1.0)
        self.letterbox = _clamp(self.letterbox, 0.0, 0.25)

        if target_duration and runtime > target_duration * 3:
            notes.append(
                f"runtime {runtime:.1f}s is far over the {target_duration:.1f}s target"
            )
        return notes

    # ------------------------------------------------------------ (de)serialise

    def to_json(self) -> dict:
        def shot_json(shot: Shot) -> dict:
            return {
                "clip": shot.clip_id,
                "source": str(shot.source),
                "start": round(shot.start, 3),
                "end": round(shot.end, 3),
                "screen_time": round(shot.duration, 3),
                "ramp": [[round(p, 3), round(s, 3)] for p, s in shot.ramp.points],
                "motion": {"kind": shot.motion.kind, "intensity": round(shot.motion.intensity, 3),
                           "anchor": [round(shot.motion.anchor[0], 3), round(shot.motion.anchor[1], 3)]},
                "reframe": shot.reframe,
                "look": asdict(shot.look),
                "transition_in": {"kind": shot.transition_in.kind,
                                  "duration": round(shot.transition_in.duration, 3)},
                "source_audio": shot.use_source_audio,
                "audio_gain": round(shot.audio_gain, 3),
                "audio_offset": round(shot.audio_offset, 3),
                "note": shot.note,
                # Without this, a saved EDL read back renders every still down
                # the moving-footage path: `-ss` into a single-frame image,
                # which yields almost nothing.
                "is_still": shot.is_still,
            }

        return {
            "title": self.title,
            "duration": round(self.duration, 3),
            "fps": self.fps,
            "resolution": [self.width, self.height],
            "look": asdict(self.look),
            "texture": round(self.texture, 3),
            "letterbox": round(self.letterbox, 3),
            "rationale": self.rationale,
            "shots": [shot_json(shot) for shot in self.shots],
            "texts": [
                {"text": cue.text, "start": round(cue.start, 3), "duration": round(cue.duration, 3),
                 "style": cue.style, "anchor": list(cue.anchor), "size": cue.size,
                 "color": cue.color, "accent": cue.accent, "per_word": cue.per_word}
                for cue in self.texts
            ],
            "music": {
                "source": str(self.music.source) if self.music.source else None,
                "offset": round(self.music.offset, 3), "gain": round(self.music.gain, 3),
                "duck": self.music.duck, "duck_amount": round(self.music.duck_amount, 3),
                "fade_in": self.music.fade_in, "fade_out": self.music.fade_out,
            },
            "sfx": [{"kind": c.kind, "at": round(c.at, 3), "gain": c.gain, "duration": c.duration}
                    for c in self.sfx],
        }

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")
        return path

    def describe(self) -> str:
        """A human-readable timeline — the thing you actually read to judge a cut."""
        lines = [
            f"« {self.title} »",
            f"{self.duration:.2f}s · {len(self.shots)} shots · {self.width}x{self.height} @ {self.fps}fps"
            f" · look: {self.look.preset}",
        ]
        if self.rationale:
            lines.append(f"  {self.rationale}")
        lines.append("")
        for index, (start, end, shot) in enumerate(self.timeline(), start=1):
            join = "" if shot.transition_in.is_cut else f" ({shot.transition_in.kind} {shot.transition_in.duration:.2f}s)"
            speed = ""
            if not shot.ramp.is_flat:
                speeds = [s for _, s in shot.ramp.points]
                speed = f" ramp {min(speeds):.2f}x→{max(speeds):.2f}x"
            elif abs(shot.ramp.constant_speed - 1.0) > 0.02:
                speed = f" {shot.ramp.constant_speed:.2f}x"
            move = "" if shot.motion.kind == "none" else f" {shot.motion.kind}"
            note = f"  — {shot.note}" if shot.note else ""
            lines.append(
                f"{index:>3}. {start:6.2f}–{end:6.2f}  {shot.clip_id} "
                f"[{shot.start:.2f}–{shot.end:.2f}]{speed}{move}{join}{note}"
            )
        for cue in self.texts:
            lines.append(f'  text @{cue.start:6.2f} ({cue.style}): "{cue.text}"')
        if self.music.source:
            lines.append(f"  music: {Path(self.music.source).name} @{self.music.gain:.2f}")
        if self.sfx:
            kinds = ", ".join(f"{c.kind}@{c.at:.1f}" for c in self.sfx[:10])
            lines.append(f"  sfx: {kinds}{' …' if len(self.sfx) > 10 else ''}")
        return "\n".join(lines)


def shots_from_json(payload: Iterable[dict], sources: dict[str, Any]) -> list[Shot]:
    """Rebuild shots from a director's JSON, tolerating missing or odd fields."""
    shots: list[Shot] = []
    for raw in payload:
        clip_id = str(raw.get("clip") or raw.get("clip_id") or "").strip()
        dossier = sources.get(clip_id)
        if dossier is None:
            log.debug("skipping shot for unknown clip %r", clip_id)
            continue
        try:
            start = float(raw.get("start", 0.0))
            end = float(raw.get("end", start + 1.5))
        except (TypeError, ValueError):
            continue

        ramp_raw = raw.get("ramp")
        if isinstance(ramp_raw, (int, float)):
            ramp = Ramp.constant(float(ramp_raw))
        elif isinstance(ramp_raw, str):
            ramp = _named_ramp(ramp_raw)
        elif isinstance(ramp_raw, list) and ramp_raw:
            points: list[tuple[float, float]] = []
            for item in ramp_raw:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    try:
                        points.append((float(item[0]), float(item[1])))
                    except (TypeError, ValueError):
                        continue
            ramp = Ramp(points) if points else Ramp.constant(float(raw.get("speed", 1.0) or 1.0))
        else:
            ramp = Ramp.constant(float(raw.get("speed", 1.0) or 1.0))

        motion_raw = raw.get("motion")
        if isinstance(motion_raw, str):
            motion = Motion(kind=motion_raw, intensity=float(raw.get("motion_intensity", 0.35) or 0.35))
        elif isinstance(motion_raw, dict):
            anchor = motion_raw.get("anchor") or [0.5, 0.5]
            motion = Motion(
                kind=str(motion_raw.get("kind", "none")),
                intensity=float(motion_raw.get("intensity", 0.35) or 0.35),
                anchor=(float(anchor[0]), float(anchor[1])) if len(anchor) >= 2 else (0.5, 0.5),
            )
        else:
            motion = Motion()

        transition_raw = raw.get("transition_in") or raw.get("transition")
        if isinstance(transition_raw, str):
            transition = Transition(transition_raw, float(raw.get("transition_duration", 0.4) or 0.4))
        elif isinstance(transition_raw, dict):
            transition = Transition(
                str(transition_raw.get("kind", "cut")),
                float(transition_raw.get("duration", 0.4) or 0.4),
            )
        else:
            transition = Transition()

        look_raw = raw.get("look")
        look = Look()
        if isinstance(look_raw, str):
            look = Look(preset=look_raw)
        elif isinstance(look_raw, dict):
            look = Look(
                preset=str(look_raw.get("preset", "neutral")),
                exposure=float(look_raw.get("exposure", 0.0) or 0.0),
                temperature=float(look_raw.get("temperature", 0.0) or 0.0),
                saturation=float(look_raw.get("saturation", 0.0) or 0.0),
                contrast=float(look_raw.get("contrast", 0.0) or 0.0),
                strength=float(look_raw.get("strength", 1.0) or 1.0),
            )

        shots.append(
            Shot(
                clip_id=clip_id,
                source=Path(dossier.asset.path),
                start=start,
                end=end,
                ramp=ramp,
                motion=motion,
                reframe=str(raw.get("reframe", "subject")),
                look=look,
                transition_in=transition,
                use_source_audio=bool(raw.get("source_audio", False)),
                audio_gain=float(raw.get("audio_gain", 1.0) or 1.0),
                audio_offset=float(raw.get("audio_offset", 0.0) or 0.0),
                note=str(raw.get("note", ""))[:200],
                is_still=dossier.asset.kind == "image",
            )
        )
    return shots


def _named_ramp(name: str) -> Ramp:
    key = name.strip().lower().replace("_", "-")
    if key in ("slow-in", "ease-in", "slow"):
        return Ramp.slow_in()
    if key in ("accelerate", "speed-up", "fast-out"):
        return Ramp.accelerate()
    if key in ("hit", "impact", "snap"):
        return Ramp.hit()
    if key in ("slowmo", "slow-motion"):
        return Ramp.constant(0.5)
    if key in ("timelapse", "fast"):
        return Ramp.constant(2.5)
    return Ramp.constant(1.0)


def texts_from_json(payload: Iterable[dict]) -> list[TextCue]:
    cues: list[TextCue] = []
    for raw in payload:
        text = str(raw.get("text", "")).strip()
        if not text:
            continue
        anchor = raw.get("anchor") or [0.5, 0.5]
        try:
            cues.append(
                TextCue(
                    text=text,
                    start=float(raw.get("start", 0.0) or 0.0),
                    duration=float(raw.get("duration", 2.0) or 2.0),
                    style=str(raw.get("style", "title")),
                    anchor=(float(anchor[0]), float(anchor[1])) if len(anchor) >= 2 else (0.5, 0.5),
                    size=float(raw.get("size", 1.0) or 1.0),
                    color=str(raw.get("color", "#FFFFFF")),
                    accent=str(raw.get("accent", raw.get("color", "#FFFFFF"))),
                    per_word=bool(raw.get("per_word", False)),
                )
            )
        except (TypeError, ValueError):
            continue
    return cues
