"""Ingest — find the footage, read its technical card, sort the bins.

Clips arrive in whatever order and whatever shape someone dropped them in.
This module refuses to care: it normalises everything into a MediaAsset so the
rest of the pipeline can reason about footage instead of about containers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Sequence

from . import ffmpeg
from .config import AUDIO_SUFFIXES, IMAGE_SUFFIXES, VIDEO_SUFFIXES

log = logging.getLogger("auteur.ingest")

#: A still is held for this long by default before motion is applied.
STILL_DURATION = 4.0


@dataclass
class MediaAsset:
    """One piece of source material, with rotation already reasoned about."""

    path: Path
    kind: str  # "video" | "image" | "audio"
    duration: float
    width: int = 0
    height: int = 0
    fps: float = 0.0
    rotation: int = 0
    has_audio: bool = False
    codec: str = ""
    audio_codec: str = ""
    sample_rate: int = 0
    bit_rate: int = 0
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def display_size(self) -> tuple[int, int]:
        """Frame size as a viewer sees it, after container rotation metadata."""
        if self.rotation in (90, 270):
            return self.height, self.width
        return self.width, self.height

    @property
    def aspect(self) -> float:
        w, h = self.display_size
        return (w / h) if h else 16 / 9

    @property
    def is_vertical(self) -> bool:
        return self.aspect < 0.95

    @property
    def is_visual(self) -> bool:
        return self.kind in ("video", "image")

    def summary(self) -> str:
        w, h = self.display_size
        bits = [f"{self.duration:.1f}s", f"{w}x{h}"]
        if self.fps:
            bits.append(f"{self.fps:.0f}fps")
        if self.has_audio:
            bits.append("sound")
        return f"{self.name} ({', '.join(bits)})"


def classify(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    return None


def discover(inputs: Sequence[str | Path], *, recursive: bool = True) -> list[Path]:
    """Expand files and directories into a stable, de-duplicated media list."""
    found: list[Path] = []
    seen: set[Path] = set()

    def consider(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen or not resolved.is_file():
            return
        if classify(resolved) is None:
            return
        seen.add(resolved)
        found.append(resolved)

    for entry in inputs:
        path = Path(entry).expanduser()
        if path.is_dir():
            walker = path.rglob("*") if recursive else path.glob("*")
            for child in sorted(walker):
                if not child.name.startswith("."):
                    consider(child)
        else:
            consider(path)
    return found


def _stream(info: dict, codec_type: str) -> dict | None:
    for stream in info.get("streams", []):
        if stream.get("codec_type") == codec_type:
            return stream
    return None


def _rotation(stream: dict) -> int:
    """Rotation in degrees, from either the legacy tag or a display matrix."""
    tags = stream.get("tags") or {}
    for key in ("rotate", "Rotate"):
        if key in tags:
            try:
                return int(float(tags[key])) % 360
            except (TypeError, ValueError):
                pass
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            try:
                return int(round(-float(side["rotation"]))) % 360
            except (TypeError, ValueError):
                pass
    return 0


def _fps(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = stream.get(key)
        if not value or value in ("0/0", "N/A"):
            continue
        try:
            rate = float(Fraction(value))
        except (ValueError, ZeroDivisionError):
            continue
        if 0.1 < rate < 1000:
            return rate
    return 0.0


def _duration(info: dict, stream: dict | None) -> float:
    for source in (stream or {}, info.get("format", {})):
        value = source.get("duration")
        try:
            duration = float(value)
        except (TypeError, ValueError):
            continue
        if duration > 0:
            return duration
    return 0.0


def probe_asset(path: Path) -> MediaAsset | None:
    """Read one file's technical card. Returns None if it is not usable media."""
    kind = classify(path)
    if kind is None:
        return None
    try:
        info = ffmpeg.probe(path)
    except ffmpeg.FFmpegError as exc:
        log.warning("skipping unreadable file %s (%s)", path.name, exc.stderr.strip()[:120])
        return None

    video = _stream(info, "video")
    audio = _stream(info, "audio")
    fmt = info.get("format", {})

    if kind == "audio" or (video is None and audio is not None):
        duration = _duration(info, audio)
        if duration <= 0:
            return None
        return MediaAsset(
            path=path, kind="audio", duration=duration, has_audio=True,
            audio_codec=(audio or {}).get("codec_name", ""),
            sample_rate=int((audio or {}).get("sample_rate") or 0),
            bit_rate=int(fmt.get("bit_rate") or 0), raw=info,
        )

    if video is None:
        return None

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        return None

    if kind == "image":
        duration = STILL_DURATION
        fps = 0.0
    else:
        duration = _duration(info, video)
        fps = _fps(video)
        if duration <= 0:
            log.warning("skipping %s: no usable duration", path.name)
            return None
        # A "video" that is really a single frame behaves like a still.
        if duration < 0.12:
            kind, duration, fps = "image", STILL_DURATION, 0.0

    return MediaAsset(
        path=path, kind=kind, duration=duration, width=width, height=height,
        fps=fps, rotation=_rotation(video), has_audio=audio is not None,
        codec=video.get("codec_name", ""),
        audio_codec=(audio or {}).get("codec_name", ""),
        sample_rate=int((audio or {}).get("sample_rate") or 0),
        bit_rate=int(fmt.get("bit_rate") or 0), raw=info,
    )


@dataclass
class Bin:
    """The sorted rushes: picture on one side, music and voice on the other."""

    visuals: list[MediaAsset] = field(default_factory=list)
    audio: list[MediaAsset] = field(default_factory=list)
    rejected: list[Path] = field(default_factory=list)

    @property
    def total_footage(self) -> float:
        return sum(asset.duration for asset in self.visuals)

    def __bool__(self) -> bool:
        return bool(self.visuals)

    def describe(self) -> str:
        lines = [f"{len(self.visuals)} visual source(s), {self.total_footage:.1f}s of footage"]
        for asset in self.visuals:
            lines.append(f"  · {asset.summary()}")
        for asset in self.audio:
            lines.append(f"  ♪ {asset.summary()}")
        if self.rejected:
            lines.append(f"  ! skipped {len(self.rejected)} unreadable file(s)")
        return "\n".join(lines)


def ingest(inputs: Sequence[str | Path], *, recursive: bool = True) -> Bin:
    """Turn a pile of paths into a sorted bin of usable material."""
    paths = discover(inputs, recursive=recursive)
    if not paths:
        raise FileNotFoundError(f"no readable media found in: {', '.join(map(str, inputs))}")

    bin_ = Bin()
    for path in paths:
        asset = probe_asset(path)
        if asset is None:
            bin_.rejected.append(path)
            continue
        (bin_.audio if asset.kind == "audio" else bin_.visuals).append(asset)

    if not bin_.visuals:
        raise FileNotFoundError(
            "found audio but no picture — an edit needs something to look at"
        )
    log.info("ingested %d visual asset(s), %d audio asset(s)", len(bin_.visuals), len(bin_.audio))
    return bin_
