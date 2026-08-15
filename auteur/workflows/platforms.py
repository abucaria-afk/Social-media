"""What each place you post to actually wants.

A finished film is not a post. Instagram will letterbox a 16:9 cut, TikTok will
draw its own buttons over the bottom fifth of the frame, and a caption longer
than the limit is silently cut off mid-sentence rather than refused. None of
that is visible from inside the edit; it is visible from here.

So every destination is written down as a `PlatformSpec`: the frame it wants,
how long it will let a film be, where its own interface sits, and how much text
it will carry. The workflows read these instead of hard-coding numbers, which
means changing a platform's rules is a one-line edit in this file rather than a
search through the renderer.

**These numbers change, and this file will go stale.** They are what the
platforms documented as of the date below. They are not fetched at runtime and
nothing here can tell when one of them moves — a video that is rejected as too
long is the first sign. Treat the file as a default to correct, not a fact.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import FORMATS, DeliveryFormat

#: When these numbers were last checked against the platforms' own guidance.
#: Printed by `auteur workflow list` so nobody has to trust it silently.
AS_OF = "2026-08"


@dataclass(frozen=True)
class SafeArea:
    """The fraction of each edge the app draws its own interface over.

    Numbers are fractions of the frame — 0.2 bottom means the lower fifth is
    covered. They are approximate by nature: the exact height of TikTok's
    caption block depends on how long the caption is, and every one of these
    apps has redesigned its player at least once. Erring generous costs a
    little composition; erring tight costs a title nobody can read.
    """

    top: float = 0.0
    bottom: float = 0.0
    left: float = 0.0
    right: float = 0.0

    @property
    def is_empty(self) -> bool:
        return not (self.top or self.bottom or self.left or self.right)

    def clamp(self, anchor: tuple[float, float]) -> tuple[float, float]:
        """Move a normalised (x, y) anchor inside the readable box.

        Text is anchored at its centre, so this pulls the centre far enough in
        that a block of ordinary size clears the chrome. It cannot know the
        block's height, which is why the insets above are generous.
        """
        x, y = anchor
        low_x, high_x = self.left, 1.0 - self.right
        low_y, high_y = self.top, 1.0 - self.bottom
        # A pathological spec (insets that overlap) would invert the range and
        # produce nonsense; centre it instead of raising.
        if low_x >= high_x:
            x = 0.5
        else:
            x = min(max(x, low_x), high_x)
        if low_y >= high_y:
            y = 0.5
        else:
            y = min(max(y, low_y), high_y)
        return x, y

    def describe(self) -> str:
        if self.is_empty:
            return "the whole frame is yours"
        parts = [
            f"{name} {value:.0%}"
            for name, value in (
                ("top", self.top),
                ("bottom", self.bottom),
                ("left", self.left),
                ("right", self.right),
            )
            if value
        ]
        return "keep text clear of " + ", ".join(parts)


@dataclass(frozen=True)
class PlatformSpec:
    """One destination, and everything the edit needs to know about it."""

    name: str
    service: str
    surface: str
    format: DeliveryFormat
    #: Runtime the surface accepts, and the length that actually performs.
    min_seconds: float
    max_seconds: float
    ideal_seconds: float
    fps: int
    safe: SafeArea
    #: Characters the caption box will carry before it truncates.
    caption_limit: int
    #: Hashtags worth writing. Past this the platform either refuses them or
    #: quietly stops counting, so more is not better, it is just longer.
    hashtag_limit: int
    #: Some surfaces publish a still alongside the video and let you choose it.
    wants_cover: bool
    #: Story-style surfaces cut a long film into cards of this length.
    card_seconds: float = 0.0
    note: str = ""

    @property
    def is_vertical(self) -> bool:
        return self.format.is_vertical

    def fit_duration(self, wanted: float | None) -> float:
        """The runtime to actually cut to."""
        if not wanted or wanted <= 0:
            wanted = self.ideal_seconds
        return min(max(wanted, self.min_seconds), self.max_seconds)

    def duration_problem(self, seconds: float) -> str:
        """Why this runtime would be refused, or "" if it is fine."""
        if seconds < self.min_seconds:
            return f"{seconds:.1f}s is under {self.service}'s {self.min_seconds:.0f}s minimum"
        if seconds > self.max_seconds:
            return (
                f"{seconds:.1f}s is over {self.service}'s {self.max_seconds:.0f}s limit"
            )
        return ""

    def describe(self) -> str:
        return (
            f"{self.format.width}x{self.format.height} · "
            f"{self.min_seconds:.0f}-{self.max_seconds:.0f}s "
            f"(aim {self.ideal_seconds:.0f}s) · {self.fps}fps"
        )


#: Vertical video, which is nearly all of this. 1080x1920 is the frame every
#: one of these surfaces asks for; they differ in how much of it they cover.
_VERTICAL = FORMATS["reel"]


PLATFORMS: dict[str, PlatformSpec] = {
    "instagram-reel": PlatformSpec(
        name="instagram-reel",
        service="Instagram",
        surface="Reels",
        format=_VERTICAL,
        min_seconds=3.0,
        max_seconds=180.0,
        ideal_seconds=25.0,
        fps=30,
        # Header and audio strip at the top; caption, handle and the action
        # rail down the right. The rail is the one that catches people out.
        safe=SafeArea(top=0.10, bottom=0.20, left=0.05, right=0.14),
        caption_limit=2200,
        hashtag_limit=30,
        wants_cover=True,
        note="Loops. A cut that lands back where it started earns a second watch.",
    ),
    "instagram-post": PlatformSpec(
        name="instagram-post",
        service="Instagram",
        surface="Feed",
        format=FORMATS["portrait"],
        min_seconds=3.0,
        max_seconds=60.0,
        ideal_seconds=20.0,
        fps=30,
        # The feed draws almost nothing over the media; the margins here are
        # composition rather than chrome.
        safe=SafeArea(top=0.06, bottom=0.06, left=0.06, right=0.06),
        caption_limit=2200,
        hashtag_limit=30,
        wants_cover=True,
        note="4:5 is the tallest the feed will show without cropping.",
    ),
    "instagram-story": PlatformSpec(
        name="instagram-story",
        service="Instagram",
        surface="Stories",
        format=_VERTICAL,
        min_seconds=1.0,
        max_seconds=60.0,
        ideal_seconds=15.0,
        fps=30,
        # Progress bars and the poster's name up top, the reply box underneath.
        safe=SafeArea(top=0.14, bottom=0.20, left=0.06, right=0.06),
        caption_limit=0,  # there is no caption field; words go on the frame
        hashtag_limit=10,
        wants_cover=False,
        card_seconds=60.0,
        note="Anything longer is split into cards and played in sequence.",
    ),
    "tiktok": PlatformSpec(
        name="tiktok",
        service="TikTok",
        surface="For You",
        format=_VERTICAL,
        min_seconds=3.0,
        max_seconds=600.0,
        ideal_seconds=25.0,
        fps=30,
        # The heaviest interface of the lot: a tall action rail on the right
        # and a caption block that grows upward from the bottom.
        safe=SafeArea(top=0.12, bottom=0.22, left=0.05, right=0.16),
        caption_limit=2200,
        hashtag_limit=20,
        wants_cover=True,
        note="The first second decides it. Open on the strongest frame, not a build-up.",
    ),
    "tiktok-photo": PlatformSpec(
        name="tiktok-photo",
        service="TikTok",
        surface="Photo mode",
        format=_VERTICAL,
        min_seconds=3.0,
        max_seconds=60.0,
        ideal_seconds=12.0,
        fps=30,
        safe=SafeArea(top=0.12, bottom=0.22, left=0.05, right=0.16),
        caption_limit=2200,
        hashtag_limit=20,
        wants_cover=True,
        note="Stills held on the beat. Give it photographs rather than clips.",
    ),
    "youtube-short": PlatformSpec(
        name="youtube-short",
        service="YouTube",
        surface="Shorts",
        format=_VERTICAL,
        min_seconds=1.0,
        max_seconds=180.0,
        ideal_seconds=30.0,
        fps=30,
        safe=SafeArea(top=0.08, bottom=0.18, left=0.05, right=0.12),
        caption_limit=5000,
        # YouTube ignores every tag on a video with more than fifteen, so the
        # sixteenth does not dilute the list, it deletes it.
        hashtag_limit=15,
        wants_cover=False,
        note="Titles are searched. Put the subject in words, not only on screen.",
    ),
}

#: What people type instead of the canonical name.
_ALIASES = {
    "reel": "instagram-reel",
    "reels": "instagram-reel",
    "ig": "instagram-reel",
    "ig-reel": "instagram-reel",
    "instagram": "instagram-reel",
    "post": "instagram-post",
    "feed": "instagram-post",
    "ig-post": "instagram-post",
    "story": "instagram-story",
    "stories": "instagram-story",
    "ig-story": "instagram-story",
    "tt": "tiktok",
    "tik-tok": "tiktok",
    "tiktok-video": "tiktok",
    "photo": "tiktok-photo",
    "tiktok-photos": "tiktok-photo",
    "shorts": "youtube-short",
    "short": "youtube-short",
    "youtube": "youtube-short",
    "yt": "youtube-short",
}


def resolve(name: str) -> PlatformSpec:
    """Find a platform by its name or by what somebody actually typed."""
    key = (name or "").strip().lower().replace("_", "-").replace(" ", "-")
    if key in PLATFORMS:
        return PLATFORMS[key]
    if key in _ALIASES:
        return PLATFORMS[_ALIASES[key]]
    raise ValueError(
        f"unknown platform: {name!r} (choose from {', '.join(sorted(PLATFORMS))})"
    )
