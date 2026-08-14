"""Delivery formats, render quality tiers, and workspace layout."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

VIDEO_SUFFIXES = {
    ".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mts", ".m2ts",
    ".mpg", ".mpeg", ".wmv", ".flv", ".3gp", ".ts", ".mxf",
}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg", ".opus", ".aif", ".aiff"}


@dataclass(frozen=True)
class DeliveryFormat:
    """A finishing format — the shape of the frame we deliver in."""

    name: str
    width: int
    height: int
    label: str

    @property
    def aspect(self) -> float:
        return self.width / self.height

    @property
    def is_vertical(self) -> bool:
        return self.height > self.width


FORMATS: dict[str, DeliveryFormat] = {
    "reel": DeliveryFormat("reel", 1080, 1920, "9:16 vertical (Reels / Shorts / TikTok)"),
    "square": DeliveryFormat("square", 1080, 1080, "1:1 square (feed)"),
    "wide": DeliveryFormat("wide", 1920, 1080, "16:9 landscape (YouTube)"),
    "cinema": DeliveryFormat("cinema", 1920, 816, "2.35:1 anamorphic"),
    "portrait": DeliveryFormat("portrait", 1080, 1350, "4:5 portrait feed"),
}


@dataclass(frozen=True)
class Quality:
    """Encoder + analysis effort. Trades render time against polish."""

    name: str
    crf: int
    preset: str
    fps: int
    audio_bitrate: str
    #: Optical-flow frame interpolation for slow motion. Gorgeous, expensive.
    optical_flow: bool
    #: Frames per second sampled during footage analysis.
    analysis_fps: float
    #: Long edge of the analysis thumbnail (pixels).
    analysis_width: int


QUALITIES: dict[str, Quality] = {
    "draft": Quality("draft", crf=28, preset="veryfast", fps=24, audio_bitrate="128k",
                     optical_flow=False, analysis_fps=4.0, analysis_width=96),
    "standard": Quality("standard", crf=20, preset="medium", fps=30, audio_bitrate="192k",
                        optical_flow=False, analysis_fps=6.0, analysis_width=128),
    "master": Quality("master", crf=16, preset="slow", fps=30, audio_bitrate="320k",
                      optical_flow=True, analysis_fps=8.0, analysis_width=160),
}


@dataclass
class Workspace:
    """Scratch space for one edit. Everything the agent writes lives here."""

    root: Path
    keep_intermediates: bool = False

    segments: Path = field(init=False)
    cache: Path = field(init=False)
    assets: Path = field(init=False)
    output: Path = field(init=False)
    logs: Path = field(init=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()
        self.segments = self.root / "segments"
        self.cache = self.root / "cache"
        self.assets = self.root / "assets"
        self.output = self.root / "output"
        self.logs = self.root / "logs"
        for path in (self.root, self.segments, self.cache, self.assets, self.output, self.logs):
            path.mkdir(parents=True, exist_ok=True)

    def clean_segments(self) -> None:
        if self.keep_intermediates:
            return
        shutil.rmtree(self.segments, ignore_errors=True)
        self.segments.mkdir(parents=True, exist_ok=True)


@dataclass
class Settings:
    """Everything the agent needs to know before it starts cutting."""

    quality: Quality = field(default_factory=lambda: QUALITIES["standard"])
    primary_format: DeliveryFormat = field(default_factory=lambda: FORMATS["reel"])
    extra_formats: tuple[DeliveryFormat, ...] = ()
    #: Hard ceiling on the finished runtime, in seconds.
    target_duration: float = 30.0
    seed: int = 0x5CE7E
    #: Rounds of watch-and-recut the critic is allowed. Each one is a full
    #: re-render, so the default is deliberately modest.
    revision_rounds: int = 1
    #: Ask Claude to direct, when an API key is present.
    use_llm: bool = True
    model: str = "claude-opus-5"
    threads: int = max(1, (os.cpu_count() or 4))

    @property
    def all_formats(self) -> tuple[DeliveryFormat, ...]:
        seen: dict[str, DeliveryFormat] = {self.primary_format.name: self.primary_format}
        for fmt in self.extra_formats:
            seen.setdefault(fmt.name, fmt)
        return tuple(seen.values())


def resolve_format(name: str) -> DeliveryFormat:
    key = name.strip().lower()
    if key in FORMATS:
        return FORMATS[key]
    aliases = {
        "9:16": "reel", "vertical": "reel", "tiktok": "reel", "shorts": "reel",
        "1:1": "square", "16:9": "wide", "landscape": "wide", "youtube": "wide",
        "2.35:1": "cinema", "scope": "cinema", "anamorphic": "cinema",
        "4:5": "portrait", "feed": "portrait",
    }
    if key in aliases:
        return FORMATS[aliases[key]]
    if "x" in key:  # explicit "1080x1920"
        try:
            w, h = (int(part) for part in key.split("x", 1))
            return DeliveryFormat(key, w, h, f"custom {w}x{h}")
        except ValueError:
            pass
    raise ValueError(f"unknown delivery format: {name!r}")


def resolve_quality(name: str) -> Quality:
    key = name.strip().lower()
    if key in QUALITIES:
        return QUALITIES[key]
    raise ValueError(f"unknown quality tier: {name!r} (choose from {sorted(QUALITIES)})")
