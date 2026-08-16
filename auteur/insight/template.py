"""Reading a reel shot by shot, and cutting your own to the same shape.

`reference.measure` answers "how is this kind of film cut" — one set of numbers
averaged over a corpus. That is the right shape for steering a director and the
wrong shape for the thing people actually ask for, which is *this* reel: its
cuts where its cuts are, its grade shot by shot, its hook, its sign-off card.

So a `Template` is the reel's timeline rather than its statistics. Every shot
keeps when it starts, how long it holds, and what it looked like — bright or
dark, flat or contrasty, still or moving, words on screen or not. Cast the
template against somebody's photographs and the result is their pictures cut to
that film's timing and graded towards its palette: the same edit, performed
again with different material.

Nothing here is a copy of anybody's footage. A template is a list of numbers
about timing and tone — the same thing a person writes down watching a reel
frame by frame — and the film it produces is made entirely of the pictures the
person supplied.
"""

from __future__ import annotations

import hashlib
import json
import logging
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the edl is imported inside the functions that build one
    from ..edl import EditDecisionList, Look

log = logging.getLogger("auteur.insight.template")

#: Frames a second the reel is decoded at. High enough to resolve a four-frame
#: cut, which short-form montages routinely use; `reference.measure` explains
#: why the dossier's usual 8fps inverts the answer on this kind of footage.
READ_FPS = 24.0

#: Width the reel is decoded to. Everything measured here is a whole-frame
#: statistic, and none of them need more than this.
READ_WIDTH = 128

#: Below this a shot is a flash frame rather than a shot. Two frames at 24fps.
SHORTEST = 2.0 / READ_FPS


@dataclass
class Beat:
    """One shot of a reel, as something to cut into rather than to watch."""

    #: Seconds from the top of the film.
    start: float
    #: How long it holds.
    duration: float
    #: Mean luma 0..1 — how bright the shot is.
    luma: float = 0.5
    #: Local contrast 0..1.
    contrast: float = 0.2
    #: Mean saturation 0..1.
    saturation: float = 0.3
    #: Warmth, -1 cool .. +1 warm, from the red/blue balance.
    warmth: float = 0.0
    #: Inter-frame motion. Near zero is a locked frame; high is handheld.
    motion: float = 0.0
    #: How much small bright detail sits in the lower third — the signature of
    #: a caption. Not read as text, only as "something is written here".
    words: float = 0.0

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class Template:
    """A reel's timeline, kept so another film can be cut to it."""

    name: str
    #: Content hash of the reel this came from. Two copies of one file under
    #: different names are one template, which is what keeps a library honest.
    fingerprint: str
    seconds: float
    beats: list[Beat] = field(default_factory=list)
    width: int = 1080
    height: int = 1920
    #: Seconds of held sign-off at the end, excluded from `beats`. Fourteen of
    #: fifteen reference reels end on one and it is not part of the cutting.
    end_card: float = 0.0
    #: True when the decode could not resolve the cutting — the numbers are a
    #: floor rather than a measurement, and a caller should say so.
    under_resolved: bool = False

    @property
    def shots(self) -> int:
        return len(self.beats)

    @property
    def cuts_per_10s(self) -> float:
        body = sum(beat.duration for beat in self.beats)
        return len(self.beats) / body * 10.0 if body > 0 else 0.0

    @property
    def shot_seconds(self) -> float:
        """Median hold. The number a person means by "how fast is it cut".

        `statistics.median`, like everything else in this package — taking the
        upper of the two middle values instead, which the first version did,
        reports a reel with an even number of shots as slower than it is.
        """
        if not self.beats:
            return 0.0
        return float(statistics.median(beat.duration for beat in self.beats))

    @property
    def hook(self) -> float:
        """How long the opening shot holds before the first cut."""
        return self.beats[0].duration if self.beats else 0.0

    def describe(self) -> str:
        return (
            f"{self.name}: {self.shots} shots in {self.seconds:.1f}s "
            f"({self.cuts_per_10s:.1f} cuts per 10s, median hold {self.shot_seconds:.3f}s, "
            f"hook {self.hook:.2f}s"
            + (f", {self.end_card:.1f}s card" if self.end_card else "")
            + ")"
        )

    # ------------------------------------------------------------ on disk

    def to_json(self) -> dict:
        data = asdict(self)
        data["beats"] = [asdict(beat) for beat in self.beats]
        return data

    @classmethod
    def from_json(cls, data: dict) -> Template:
        beats = [Beat(**beat) for beat in data.get("beats", [])]
        return cls(
            name=str(data.get("name", "untitled")),
            fingerprint=str(data.get("fingerprint", "")),
            seconds=float(data.get("seconds", 0.0)),
            beats=beats,
            width=int(data.get("width", 1080)),
            height=int(data.get("height", 1920)),
            end_card=float(data.get("end_card", 0.0)),
            under_resolved=bool(data.get("under_resolved", False)),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Template:
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8")))


def fingerprint_of(path: Path) -> str:
    """A content id for a film.

    Keyed on the bytes rather than the name, because the same reel arrives
    twice under two names more often than two different reels arrive under one.
    """
    digest = hashlib.blake2b(digest_size=8)
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def read(path: str | Path, *, name: str = "") -> Template | None:
    """Watch a reel and write down its timeline.

    One decode answers everything: the cuts come from the same shot detector
    that reads your own rushes, and each shot's tone is measured from the
    frames that decode already produced. Returns None for anything that is not
    readable video.
    """
    import numpy as np

    from .. import ffmpeg as ff
    from ..analysis.video import _detect_shots, resolves_cutting
    from ..ingest import probe_asset

    file = Path(path)
    asset = probe_asset(file)
    if asset is None or asset.kind != "video" or asset.duration <= 0:
        log.info("not readable video, skipping as a template: %s", file.name)
        return None

    # In colour, because the palette is half of what a template carries and
    # the default decode is greyscale — the first version of this reported
    # saturation 0.00 and warmth +0.00 for every shot of every reel, which is
    # not a measurement of anything.
    stream = ff.read_frames(file, width=READ_WIDTH, fps=READ_FPS, color=True, max_frames=4000)
    frames = stream.frames
    if len(frames) < 4:
        log.info("too few frames to read a timeline from: %s", file.name)
        return None

    colour = frames if frames.ndim == 4 else None
    grey = frames[..., 0] if frames.ndim == 4 else frames
    grey = grey.astype(np.float32) / 255.0

    # The detector wants the frames it was written for: a greyscale stack.
    pictures = frames[..., 0] if frames.ndim == 4 else frames
    motion = np.concatenate([[0.0], np.abs(np.diff(grey, axis=0)).mean(axis=(1, 2))]).astype(
        np.float32
    )
    cuts = [
        c
        for c in _detect_shots(pictures, motion, READ_FPS, min_gap=SHORTEST)
        if 0 < c < asset.duration
    ]
    resolved = resolves_cutting(pictures, motion)

    edges = [0.0, *cuts, asset.duration]
    lengths = [edges[i + 1] - edges[i] for i in range(len(edges) - 1)]

    # The sign-off card is a signature, not an edit — a final shot many times
    # longer than everything before it is the thing played after the cutting
    # stops. Counting it understates the cutting rate by about a third.
    from .reference import _end_card

    card = _end_card(lengths)
    if card:
        edges = edges[:-1]
        lengths = lengths[:-1]

    beats: list[Beat] = []
    for index, span in enumerate(lengths):
        if span < SHORTEST:
            continue
        first = int(edges[index] * READ_FPS)
        last = max(first + 1, int((edges[index] + span) * READ_FPS))
        window = grey[first:last]
        if not len(window):
            continue
        beats.append(
            Beat(
                start=round(edges[index], 4),
                duration=round(span, 4),
                luma=round(float(window.mean()), 4),
                # Local contrast: how much the picture varies within a frame,
                # which is what "flat" and "punchy" mean to an eye. Frame-wide
                # standard deviation, averaged over the shot.
                contrast=round(float(window.std(axis=(1, 2)).mean()), 4),
                saturation=round(_saturation(colour, first, last), 4),
                warmth=round(_warmth(colour, first, last), 4),
                motion=round(float(motion[first:last].mean()), 4),
                words=round(_caption_weight(window), 4),
            )
        )

    if not beats:
        return None

    return Template(
        name=name or file.stem[:32],
        fingerprint=fingerprint_of(file),
        seconds=round(sum(beat.duration for beat in beats), 3),
        beats=beats,
        width=int(getattr(asset, "width", 0) or 1080),
        height=int(getattr(asset, "height", 0) or 1920),
        end_card=round(card, 3),
        under_resolved=not resolved,
    )


def _saturation(colour, first: int, last: int) -> float:
    """Mean saturation over a shot, 0..1. Zero when the decode was greyscale."""
    import numpy as np

    if colour is None:
        return 0.0
    window = colour[first:last].astype(np.float32) / 255.0
    if not len(window):
        return 0.0
    high = window.max(axis=-1)
    low = window.min(axis=-1)
    # Guard the black pixels: saturation is undefined at zero and dividing
    # there fills a night shot with noise that reads as colour.
    return float(np.where(high > 0.02, (high - low) / np.maximum(high, 1e-6), 0.0).mean())


def _warmth(colour, first: int, last: int) -> float:
    """Red minus blue over a shot, -1 cool .. +1 warm."""
    import numpy as np

    if colour is None:
        return 0.0
    window = colour[first:last].astype(np.float32) / 255.0
    if not len(window):
        return 0.0
    # Gain 2.5: real footage spans about -0.33..+0.20 raw, so this fills the
    # scale without pinning the cold end at the rail, which a gain of 3 did.
    return float(np.clip((window[..., 0].mean() - window[..., 2].mean()) * 2.5, -1.0, 1.0))


def _caption_weight(window) -> float:
    """How much small bright detail sits in the lower third, 0..1.

    A caption is bright, small and near the bottom. This does not read the
    words and must not be described as though it does — it answers "is
    something written down there", which is all a template needs in order to
    put a title in the same place.
    """
    import numpy as np

    if not len(window):
        return 0.0
    band = window[:, int(window.shape[1] * 0.62) :, :]
    if band.size == 0:
        return 0.0
    bright = band > 0.75
    # Edges, not area: a white sky fills the band and is not a caption.
    edges = np.abs(np.diff(band, axis=2)).mean()
    return float(np.clip(bright.mean() * 2.0 + edges * 6.0, 0.0, 1.0))


def read_all(paths: Sequence[str | Path]) -> list[Template]:
    """Read a folder of reels, skipping whatever will not open."""
    out: list[Template] = []
    seen: set[str] = set()
    for path in paths:
        template = read(path)
        if template is None:
            continue
        if template.fingerprint in seen:
            log.info("the same reel twice, keeping one: %s", template.name)
            continue
        seen.add(template.fingerprint)
        out.append(template)
    return out


# ---------------------------------------------------------------------------
# Casting a template against somebody's own pictures
# ---------------------------------------------------------------------------


@dataclass
class Tone:
    """What one of your photographs looks like, in the template's own terms."""

    path: Path
    luma: float = 0.5
    contrast: float = 0.2
    saturation: float = 0.3
    warmth: float = 0.0
    #: Fine detail — how much there is to look at. Used to pick the opener.
    detail: float = 0.0

    def distance(self, beat: Beat) -> float:
        """How far this picture is from what the beat wants.

        Weighted by how visible the mismatch is: brightness first, because a
        dark photograph in a bright beat reads as a mistake from across a room,
        then warmth, then the rest. Grading closes some of this gap, so the
        match only has to get close enough for the correction to be gentle.
        """
        return (
            abs(self.luma - beat.luma) * 2.0
            + abs(self.warmth - beat.warmth) * 1.0
            + abs(self.saturation - beat.saturation) * 0.6
            + abs(self.contrast - beat.contrast) * 0.6
        )


def look_at(path: str | Path) -> Tone | None:
    """Measure one photograph the same way a reel's shot is measured."""
    import numpy as np

    from .. import ffmpeg as ff

    file = Path(path)
    try:
        stream = ff.read_frames(file, width=READ_WIDTH, color=True, still=True, max_frames=2)
    except Exception as exc:  # noqa: BLE001 - one unreadable picture is not fatal
        log.info("could not look at %s: %s", file.name, exc)
        return None
    frames = stream.frames
    if not len(frames):
        return None

    picture = frames[0].astype(np.float32) / 255.0
    if picture.ndim == 2:
        picture = np.stack([picture] * 3, axis=-1)
    grey = picture.mean(axis=-1)
    high = picture.max(axis=-1)
    low = picture.min(axis=-1)

    # Fine detail: a discrete Laplacian, which is edges and texture and is near
    # zero for sky or a photograph that missed focus.
    inner = grey[1:-1, 1:-1]
    detail = float(
        np.abs(
            4 * inner - grey[:-2, 1:-1] - grey[2:, 1:-1] - grey[1:-1, :-2] - grey[1:-1, 2:]
        ).mean()
    )

    return Tone(
        path=file,
        luma=float(grey.mean()),
        contrast=float(grey.std()),
        saturation=float(np.where(high > 0.02, (high - low) / np.maximum(high, 1e-6), 0.0).mean()),
        warmth=float(np.clip((picture[..., 0].mean() - picture[..., 2].mean()) * 2.5, -1.0, 1.0)),
        detail=detail,
    )


#: How far a correction is allowed to push a picture towards a beat. A grade
#: that fully matched every shot would flatten the person's own photographs
#: into the reference's palette; the point is their pictures cut like that
#: film, not their pictures turned into that film.
PULL = 0.6


def _correction(tone: Tone, beat: Beat) -> Look:
    """The per-shot grade that carries a picture towards a beat's tone."""
    from ..edl import Look

    return Look(
        preset="neutral",
        exposure=_hold((beat.luma - tone.luma) * 2.0 * PULL, 0.8),
        temperature=_hold((beat.warmth - tone.warmth) * PULL, 0.8),
        saturation=_hold((beat.saturation - tone.saturation) * 1.5 * PULL, 0.8),
        contrast=_hold((beat.contrast - tone.contrast) * 2.0 * PULL, 0.8),
    )


def _hold(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def timeline(template: Template, *, seconds: float | None = None) -> list[Beat]:
    """The template's beats, trimmed or repeated to fill a runtime.

    Repeating starts again from the top rather than reflecting, because a
    reel's shape is a run at something and then a return, and playing that
    backwards is not a second run at it.
    """
    beats = list(template.beats)
    if not beats or seconds is None or seconds <= 0:
        return beats

    out: list[Beat] = []
    at = 0.0
    index = 0
    while at < seconds - 1e-6 and len(out) < 600:
        source = beats[index % len(beats)]
        span = min(source.duration, seconds - at)
        if span < SHORTEST:
            break
        out.append(
            Beat(
                start=round(at, 4),
                duration=round(span, 4),
                luma=source.luma,
                contrast=source.contrast,
                saturation=source.saturation,
                warmth=source.warmth,
                motion=source.motion,
                words=source.words,
            )
        )
        at += span
        index += 1
    return out


def cast(
    template: Template,
    photos: Sequence[str | Path],
    *,
    seconds: float | None = None,
    title: str = "",
    words: Sequence[str] = (),
) -> EditDecisionList:
    """Cut the person's pictures to this reel's timeline.

    Every shot lands where the reference's shot landed and holds for as long,
    and each picture is graded towards what that shot looked like. Which
    picture goes where is decided by tone: a beat the reference played dark
    gets the darkest photograph that has not just been used, so the correction
    has less work to do and the film keeps its own shape.

    The opening beat is the exception. It gets the picture with the most in it
    — measured, not first in the folder — because the first frame is the whole
    hook and picking it by upload order is picking it at random.
    """
    from ..edl import EditDecisionList, Motion, Shot, TextCue

    tones = [tone for tone in (look_at(p) for p in photos) if tone is not None]
    if not tones:
        raise ValueError("none of those pictures would open")

    beats = timeline(template, seconds=seconds)
    if not beats:
        raise ValueError(f"{template.name} has no shots to cut to")

    opener = max(range(len(tones)), key=lambda i: tones[i].detail)
    used = [0] * len(tones)
    shots: list[Shot] = []
    last = -1

    for index, beat in enumerate(beats):
        if index == 0:
            pick = opener
        else:
            # Closest in tone, then least used, and never twice running: the
            # same photograph either side of a cut is not a cut.
            pick = min(
                range(len(tones)),
                key=lambda i: (
                    (i == last and len(tones) > 1) * 100.0
                    + used[i] * 0.35
                    + tones[i].distance(beat)
                ),
            )
        used[pick] += 1
        last = pick
        tone = tones[pick]

        shots.append(
            Shot(
                clip_id=f"beat{index:03d}",
                source=tone.path,
                start=0.0,
                end=beat.duration,
                is_still=True,
                look=_correction(tone, beat),
                # The reference reels hold every frame dead still and let the
                # cut rate carry the energy — at a sixth of a second there is
                # no room for a move. Only a beat the reference actually moved
                # in, and held long enough to see, gets one.
                motion=(
                    Motion(kind="push", intensity=min(0.5, beat.motion * 6.0))
                    if beat.motion > 0.03 and beat.duration > 0.5
                    else Motion(kind="none")
                ),
                note=f"beat {index + 1} of {template.name}",
            )
        )

    film = EditDecisionList(
        title=title or f"after {template.name}",
        shots=shots,
        width=template.width or 1080,
        height=template.height or 1920,
        rationale=(
            f"Cut to the timeline of {template.name}: {len(beats)} shots, "
            f"{template.cuts_per_10s:.1f} cuts per ten seconds, median hold "
            f"{template.shot_seconds:.3f}s. Each picture graded towards what that "
            f"reel's shot looked like, {int(PULL * 100)}% of the way."
        ),
    )

    # The reference put words on screen at particular beats. Put the person's
    # own words there — the placement is the template's, the words are theirs.
    if words:
        wanted = sorted(range(len(beats)), key=lambda i: -beats[i].words)
        for slot, text in zip(sorted(wanted[: len(words)]), words, strict=False):
            beat = beats[slot]
            film.texts.append(
                TextCue(
                    text=text,
                    start=round(beat.start, 3),
                    duration=round(max(0.6, beat.duration), 3),
                )
            )

    return film
