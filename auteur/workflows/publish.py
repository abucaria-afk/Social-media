"""Turning a finished film into something you can actually post.

The render is the middle of the job, not the end of it. What a post needs is
the video, a cover frame chosen rather than defaulted, a caption that fits in
the box, tags that are not just noise, and a written record of all of it so the
same post can be made again next week without anybody remembering anything.

Nothing here uploads. There is no Instagram or TikTok API call in this file and
none anywhere in the project: posting on someone's behalf needs their
credentials, and this runs on a laptop on a home wifi. What it produces is a
folder you can post from in under a minute, and a `post.json` any scheduler —
including the one next door in `schedule.py` — can read.

The caption is a **draft**. It is assembled from the brief and what the edit
turned out to be, by rules, not by a model. It is a first line to rewrite, and
it says so.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import ffmpeg as ff
from ..director.brief import Brief
from ..edl import EditDecisionList
from .platforms import PlatformSpec

log = logging.getLogger("auteur.workflows.publish")

#: Words that belong to the making of the film rather than to the film, and so
#: have no business in a caption. "20 seconds" is direction; "harbour at dusk"
#: is the post.
_TECHNICAL = re.compile(
    r"\b("
    r"\d+\s*(?:seconds?|secs?|s)\b"
    r"|\d+\s*(?:fps|bpm)\b"
    r"|vertical|horizontal|square|widescreen|anamorphic|9:16|16:9|1:1|4:5"
    r"|reel|reels|tiktok|short|shorts|instagram|youtube"
    r"|montage|edit|cut|render|footage|clips?|b-?roll"
    r"|beat[- ]sync(?:ed)?|slow ?mo(?:tion)?|speed ?ramps?"
    r")\b",
    re.IGNORECASE,
)

#: Look → the words people actually search for it under.
_LOOK_TAGS: dict[str, tuple[str, ...]] = {
    "blockbuster": ("cinematic", "colorgrading", "filmlook"),
    "noir": ("blackandwhite", "noir", "monochrome"),
    "moody": ("moody", "moodygrams", "darkaesthetic"),
    "steel": ("cold", "minimal", "cinematography"),
    "warm": ("goldenhour", "warmtones", "cozy"),
    "vintage": ("vintage", "retro", "filmgrain"),
    "neon": ("neon", "nightvibes", "cyberpunk"),
    "neutral": ("cinematography", "videography"),
}

#: Tags worth carrying on every post to a given service. Deliberately short —
#: a wall of generic tags is how a post reads as spam to a human and as
#: low-quality to a ranker.
_SERVICE_TAGS: dict[str, tuple[str, ...]] = {
    "Instagram": ("reels", "reelsinstagram"),
    "TikTok": ("fyp", "foryou"),
    "YouTube": ("shorts",),
}


#: Words long enough to look like nouns and empty enough to be worthless as
#: tags. `#after` finds nothing anybody wanted to find.
_NOT_WORTH_TAGGING = frozenset("""
    after also away back been before being both come does down each else even
    ever from have here into just like made make many more most much must
    near next once only over said same show some such than that them then
    there these they this those through very want well were what when where
    which while will with within would your
    """.split())


def _sentence(text: str) -> str:
    """Tidy a prompt into something that reads like a person wrote it."""
    cleaned = _TECHNICAL.sub("", text or "")
    cleaned = re.sub(r'["""\']', "", cleaned)
    cleaned = re.sub(r"[,;]\s*(?=[,;]|$)", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;:-—")
    if not cleaned:
        return ""
    return cleaned[0].upper() + cleaned[1:]


def _tag(word: str) -> str:
    """A hashtag from a word, or "" if nothing usable survives."""
    stripped = re.sub(r"[^0-9a-z]", "", (word or "").lower())
    # A tag that starts with a digit is legal but reads as a number, and a
    # one-letter tag matches everything, which is the same as matching nothing.
    return stripped if len(stripped) >= 3 and not stripped[0].isdigit() else ""


@dataclass
class Caption:
    """The words that go in the box, and the tags under them."""

    body: str
    hashtags: tuple[str, ...] = ()
    #: Description for people using a screen reader. Not optional in spirit.
    alt_text: str = ""

    def render(self, spec: PlatformSpec) -> str:
        """The caption as it should be pasted, trimmed to fit the box.

        Trimmed here rather than at the platform, because the platform trims
        mid-word and mid-hashtag and does not tell you it did.
        """
        if spec.caption_limit <= 0:
            return ""
        tags = [f"#{tag}" for tag in self.hashtags[: spec.hashtag_limit]]
        body = self.body.strip()
        if not tags:
            return body[: spec.caption_limit]

        block = "\n\n" + " ".join(tags)
        if len(body) + len(block) <= spec.caption_limit:
            return body + block
        # Tags earn their place; the prose is what gets cut. Drop tags only
        # once there is no prose left to give up.
        room = spec.caption_limit - len(block)
        if room > 20:
            return body[: room - 1].rstrip() + "…" + block
        while tags and len(" ".join(tags)) > spec.caption_limit:
            tags.pop()
        return " ".join(tags)[: spec.caption_limit]

    def to_json(self) -> dict:
        return {"body": self.body, "hashtags": list(self.hashtags), "alt_text": self.alt_text}


def draft_caption(brief: Brief, edl: EditDecisionList, spec: PlatformSpec) -> Caption:
    """A caption to rewrite, not a caption to post.

    Built from what was asked for and what was made. Anything the director put
    in quotes is already on screen, so it leads; the rest of the prompt becomes
    the line under it. There is no model in this path and it is not trying to
    sound like one.
    """
    on_screen = [text.strip() for text in (brief.on_screen_text or []) if text.strip()]
    lead = on_screen[0] if on_screen else (edl.title or "").strip()
    body_line = _sentence(brief.prompt)

    lines: list[str] = []
    if lead and lead.lower() not in ("untitled", ""):
        lines.append(lead)
        # The quoted text is already the first line; leaving it at the front of
        # the second line as well printed "AFTER DARK / AFTER DARK neon harbour"
        # — the title, and then the title again.
        if body_line.lower().startswith(lead.lower()):
            body_line = _sentence(body_line[len(lead) :])
    if body_line and body_line.lower() != lead.lower():
        lines.append(body_line)
    if not lines:
        lines.append("Shot and cut this week.")

    tags: list[str] = []
    seen: set[str] = set()

    def offer(word: str) -> None:
        tag = _tag(word)
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)

    for word in _LOOK_TAGS.get(edl.look.preset, _LOOK_TAGS["neutral"]):
        offer(word)
    # Nouns out of the prompt, longest first: the specific words are the ones
    # worth finding a post by, and the long ones are usually the specific ones.
    words = {
        word
        for word in re.findall(r"[A-Za-z]{4,}", _TECHNICAL.sub("", brief.prompt or ""))
        if word.lower() not in _NOT_WORTH_TAGGING
    }
    for word in sorted(words, key=lambda word: (-len(word), word.lower()))[:6]:
        offer(word)
    for word in _SERVICE_TAGS.get(spec.service, ()):
        offer(word)

    alt = body_line or lead or "A short film"
    alt_text = (
        f"{alt}. {len(edl.shots)} shots over {edl.duration:.0f} seconds, {edl.look.preset} grade."
    )
    return Caption(
        body="\n".join(lines), hashtags=tuple(tags[: spec.hashtag_limit]), alt_text=alt_text
    )


def cover_frame(video: Path, destination: Path, *, at: float | None = None) -> Path | None:
    """Pull one frame out to use as the cover.

    Not the first frame. The first frame of a cut is the least representative
    one in it — often a fade-up from black — and it is the frame every tool
    picks by default, which is why so many posts have a black thumbnail. A
    fifth of the way in the film has started but has not given itself away.
    """
    try:
        duration = float(ff.probe(video)["format"]["duration"])
    except (KeyError, ValueError, ff.FFmpegError, OSError):
        duration = 0.0
    moment = at if at is not None else (duration * 0.2 if duration else 0.5)
    moment = max(0.0, min(moment, max(0.0, duration - 0.05)))

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        # ff.run supplies the binary, -hide_banner, -nostdin and -y itself;
        # passing the binary again made it the output filename, and ffmpeg
        # said so in the one message nobody was reading.
        ff.run(
            [
                "-ss",
                f"{moment:.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(destination),
            ]
        )
    except (ff.FFmpegError, ff.MissingBinary, OSError) as exc:
        # A missing cover is a smaller problem than a failed workflow.
        log.warning("could not extract a cover frame: %s", exc)
        return None
    return destination if destination.exists() else None


@dataclass
class Deliverable:
    """One post, ready to make: the file, the frame, the words, the checks."""

    platform: str
    service: str
    surface: str
    video: Path
    duration: float
    width: int
    height: int
    caption: Caption
    cover: Path | None = None
    folder: Path | None = None
    #: Ways in which what we made does not match what the platform wants.
    warnings: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return not self.warnings

    def to_json(self, spec: PlatformSpec | None = None) -> dict:
        return {
            "platform": self.platform,
            "service": self.service,
            "surface": self.surface,
            "video": str(self.video),
            "cover": str(self.cover) if self.cover else None,
            "duration": round(self.duration, 3),
            "width": self.width,
            "height": self.height,
            "caption": self.caption.to_json(),
            "caption_to_paste": self.caption.render(spec) if spec else self.caption.body,
            "warnings": list(self.warnings),
            "created": self.created,
        }

    def describe(self) -> str:
        lines = [
            f"{self.service} · {self.surface}",
            f"{self.width}x{self.height}, {self.duration:.1f}s",
        ]
        if self.cover:
            lines.append(f"cover: {self.cover.name}")
        for warning in self.warnings:
            lines.append(f"! {warning}")
        return "\n".join(lines)


def package(
    *,
    video: Path,
    spec: PlatformSpec,
    brief: Brief,
    edl: EditDecisionList,
    folder: Path,
) -> Deliverable:
    """Assemble the post folder beside a finished render.

    Checks the render against the platform rather than trusting it: a duration
    the critic was happy with can still be three seconds over TikTok's floor,
    and a format override can leave the frame the wrong shape. Anything that
    does not match is a warning on the deliverable, not an exception — the file
    exists either way and the person holding it should be told, not stopped.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)

    duration, width, height = 0.0, 0, 0
    try:
        info = ff.probe(video)
        duration = float(info["format"]["duration"])
        stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
        width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
    except (KeyError, ValueError, StopIteration, ff.FFmpegError, OSError) as exc:
        log.warning("could not probe the finished film: %s", exc)

    warnings: list[str] = []
    if duration:
        problem = spec.duration_problem(duration)
        if problem:
            warnings.append(problem)
    if width and height and (width, height) != (spec.format.width, spec.format.height):
        warnings.append(
            f"{width}x{height} is not the {spec.format.width}x{spec.format.height} "
            f"{spec.service} wants for {spec.surface}"
        )

    caption = draft_caption(brief, edl, spec)
    cover = None
    if spec.wants_cover:
        cover = cover_frame(video, folder / "cover.jpg")

    deliverable = Deliverable(
        platform=spec.name,
        service=spec.service,
        surface=spec.surface,
        video=video,
        duration=duration,
        width=width,
        height=height,
        caption=caption,
        cover=cover,
        folder=folder,
        warnings=warnings,
    )

    (folder / "post.json").write_text(
        json.dumps(deliverable.to_json(spec), indent=2), encoding="utf-8"
    )
    caption_text = caption.render(spec)
    if caption_text:
        (folder / "caption.txt").write_text(caption_text + "\n", encoding="utf-8")
    (folder / "alt-text.txt").write_text(caption.alt_text + "\n", encoding="utf-8")
    return deliverable
