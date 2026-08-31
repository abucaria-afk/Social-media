"""YouTube access layer — search, subscribe, and fetch video metadata.

This module provides the Scholar's interface to YouTube. It does *not* download
copyrighted content. It works through:

1. The YouTube Data API v3 (when a key is configured) for search and
   subscription monitoring.
2. `yt-dlp` metadata extraction (no download) for video details, chapters,
   descriptions, and auto-generated captions/transcripts.

The Scholar decides *what* to watch based on its discipline interests and its
subscription list. This module handles *how* to reach it.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("auteur.scholar.youtube")

#: Maximum number of results per search query.
_MAX_RESULTS = 20


def _parse_json3(raw: str) -> list[dict]:
    """YouTube's own caption JSON: events carrying timed runs of text."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    cues: list[dict] = []
    for event in payload.get("events") or []:
        text = "".join(segment.get("utf8", "") for segment in event.get("segs") or [])
        text = text.replace("\n", " ").strip()
        if not text:
            continue
        cues.append(
            {
                "text": text,
                "start": round(event.get("tStartMs", 0) / 1000.0, 3),
                "duration": round(event.get("dDurationMs", 0) / 1000.0, 3),
            }
        )
    return cues


_VTT_TIME = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})"
)
_VTT_TAG = re.compile(r"<[^>]+>")


def _parse_vtt(raw: str) -> list[dict]:
    """WebVTT, the format yt-dlp hands back when json3 is not offered."""

    def seconds(h: str, m: str, s: str, ms: str) -> float:
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

    cues: list[dict] = []
    lines = raw.splitlines()
    for index, line in enumerate(lines):
        match = _VTT_TIME.search(line)
        if not match:
            continue
        start = seconds(*match.group(1, 2, 3, 4))
        end = seconds(*match.group(5, 6, 7, 8))
        body: list[str] = []
        for follow in lines[index + 1 :]:
            if not follow.strip() or _VTT_TIME.search(follow):
                break
            body.append(_VTT_TAG.sub("", follow).strip())
        text = " ".join(part for part in body if part).strip()
        # Auto-captions repeat the previous line as a rolling caption; keeping
        # both would double every word in the transcript.
        if text and (not cues or cues[-1]["text"] != text):
            cues.append({"text": text, "start": round(start, 3), "duration": round(end - start, 3)})
    return cues


class YouTubeUnavailable(RuntimeError):
    """There is no way to reach YouTube from here.

    Raised rather than returning an empty list, because those are different
    facts and the Scholar acts on them differently: "I searched and there was
    nothing" means try another query, "I cannot search at all" means say so to
    whoever asked. An empty list for both meant a Scholar with no network
    reported the same clean, quiet, entirely fictional success as one that had
    genuinely read the whole first page of results.
    """


def _ytdlp() -> str | None:
    """The yt-dlp executable, or None if it is not installed."""
    return shutil.which("yt-dlp")


def reachable() -> tuple[bool, str]:
    """Is there a *route* to YouTube installed — a tool or a key?

    Deliberately not named for what it was documented as. It used to say "can
    this machine reach YouTube", which it never asked: it looks for yt-dlp on
    PATH and an API key in the environment, and both can be present on a
    machine with no route out. That is exactly the case behind a proxy, and
    the study loop spent whole sessions being told it could search and then
    failing on a 403 it had already been told about.

    `can_reach` below actually asks. This stays because deciding whether the
    feature is *configured* is a real question too, and it is cheap.
    """
    if _ytdlp() is not None:
        return True, "yt-dlp"
    if os.environ.get("YOUTUBE_API_KEY"):
        return True, "YouTube Data API key"
    return False, "yt-dlp is not installed and YOUTUBE_API_KEY is not set"


def can_reach(*, timeout: float = 25.0) -> tuple[bool, str]:
    """Ask YouTube for one thing, and report what actually happened.

    Costs a few seconds and is worth them: everything downstream of a wrong
    answer here is a long download that was never going to work.
    """
    tool = _ytdlp()
    if tool is None:
        return False, "yt-dlp is not installed"
    try:
        got = subprocess.run(
            [tool, "--no-warnings", "--flat-playlist", "-J", "ytsearch1:test"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"YouTube did not answer within {timeout:.0f}s"
    except OSError as exc:
        return False, f"could not run yt-dlp: {exc}"
    if got.returncode == 0:
        return True, ""
    why = (got.stderr or "").strip().splitlines()
    last = why[-1] if why else "yt-dlp failed"
    if "403" in last or "proxy" in last.lower():
        return False, "blocked on the way out — a proxy refused the connection"
    return False, last[:200]


class SearchStrategy(enum.Enum):
    """How the Scholar decides what to look for next."""

    #: Follow the discipline curriculum — systematic, breadth-first.
    CURRICULUM = "curriculum"
    #: Chase a specific technique it encountered in a previous video.
    DEEP_DIVE = "deep_dive"
    #: Check subscriptions for new uploads.
    SUBSCRIPTION_CHECK = "subscription_check"
    #: Explore adjacent topics the knowledge store flags as gaps.
    GAP_FILL = "gap_fill"


@dataclass(frozen=True)
class VideoMeta:
    """Metadata for one YouTube video — never the content itself."""

    video_id: str
    title: str
    channel: str
    channel_id: str
    duration_sec: float
    description: str = ""
    chapters: list[dict] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    upload_date: str = ""
    view_count: int = 0
    like_count: int = 0
    transcript_segments: list[dict] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def has_transcript(self) -> bool:
        return len(self.transcript_segments) > 0

    def chapter_at(self, seconds: float) -> str:
        """Which chapter covers the given timestamp."""
        current = ""
        for chapter in self.chapters:
            if chapter.get("start_time", 0) <= seconds:
                current = chapter.get("title", "")
        return current


@dataclass
class Subscription:
    """A channel the Scholar follows for new uploads."""

    channel_id: str
    channel_name: str
    #: Why the Scholar follows this channel — which discipline(s) it serves.
    disciplines: list[str] = field(default_factory=list)
    #: How many videos the Scholar has watched from this channel.
    watched_count: int = 0
    #: The last video ID the Scholar saw, for new-upload detection.
    last_seen_video_id: str = ""
    #: Priority: higher means check more often.
    priority: float = 1.0

    @property
    def is_most_watched(self) -> bool:
        """Top-tier creator — the Scholar should run when they upload."""
        return self.watched_count >= 10 or self.priority >= 3.0


class YouTubeAccess:
    """The Scholar's YouTube interface.

    Searches, fetches metadata, and monitors subscriptions. Does not download
    video content — it works from metadata, transcripts, and (when available)
    frame-level analysis via yt-dlp's info extraction.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        subscriptions_path: Path | None = None,
        cache_dir: Path | None = None,
    ):
        self._api_key = api_key or os.environ.get("YOUTUBE_API_KEY", "")
        self._subscriptions_path = subscriptions_path
        self._cache_dir = cache_dir or Path.home() / ".auteur" / "scholar" / "yt_cache"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._subscriptions: list[Subscription] = []
        if subscriptions_path and subscriptions_path.exists():
            self._load_subscriptions(subscriptions_path)

    def _load_subscriptions(self, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in data:
            self._subscriptions.append(Subscription(**item))

    def save_subscriptions(self, path: Path | None = None) -> None:
        target = path or self._subscriptions_path
        if not target:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        data = [
            {
                "channel_id": s.channel_id,
                "channel_name": s.channel_name,
                "disciplines": s.disciplines,
                "watched_count": s.watched_count,
                "last_seen_video_id": s.last_seen_video_id,
                "priority": s.priority,
            }
            for s in self._subscriptions
        ]
        target.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @property
    def subscriptions(self) -> list[Subscription]:
        return list(self._subscriptions)

    @property
    def most_watched_creators(self) -> list[Subscription]:
        """Channels the Scholar considers high-priority — triggers autonomous runs."""
        return [s for s in self._subscriptions if s.is_most_watched]

    def subscribe(self, channel_id: str, channel_name: str, disciplines: list[str]) -> None:
        """Add a channel to the watch list."""
        existing = next((s for s in self._subscriptions if s.channel_id == channel_id), None)
        if existing:
            existing.disciplines = list(set(existing.disciplines + disciplines))
            return
        self._subscriptions.append(
            Subscription(channel_id=channel_id, channel_name=channel_name, disciplines=disciplines)
        )

    def search(self, query: str, *, max_results: int = _MAX_RESULTS) -> list[VideoMeta]:
        """Search YouTube for videos matching the query.

        Uses yt-dlp's search extraction which does not require an API key.
        Falls back to the Data API v3 if a key is configured and yt-dlp fails.
        """
        log.info("searching YouTube: %r (max %d)", query, max_results)

        if _ytdlp() is not None:
            try:
                return self._search_via_ytdlp(query, max_results)
            except Exception as exc:  # noqa: BLE001 - fall through to the API
                log.warning("yt-dlp search failed: %s", exc)
                if not self._api_key:
                    raise YouTubeUnavailable(f"yt-dlp search failed: {exc}") from exc

        if self._api_key:
            return self._search_via_api(query, max_results)

        raise YouTubeUnavailable(reachable()[1])

    def fetch_metadata(self, video_id: str) -> VideoMeta | None:
        """Fetch full metadata (including transcript) for a single video."""
        cache_path = self._cache_dir / f"{video_id}.json"
        if cache_path.exists():
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            return self._meta_from_dict(data)

        if _ytdlp() is None:
            raise YouTubeUnavailable(reachable()[1])

        try:
            result = subprocess.run(
                [
                    "yt-dlp",
                    "--skip-download",
                    "--write-auto-sub",
                    "--sub-lang",
                    "en",
                    "--dump-json",
                    f"https://www.youtube.com/watch?v={video_id}",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                log.warning(
                    "yt-dlp metadata fetch failed for %s: %s", video_id, result.stderr[:200]
                )
                return None

            data = json.loads(result.stdout)
            cache_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return self._meta_from_dict(data)

        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as exc:
            log.warning("metadata fetch failed for %s: %s", video_id, exc)
            return None

    def check_new_uploads(self) -> list[tuple[Subscription, VideoMeta]]:
        """Check most-watched creators for new uploads.

        Returns (subscription, video) pairs for any new videos found.
        This is the trigger for autonomous Scholar runs.
        """
        new_uploads: list[tuple[Subscription, VideoMeta]] = []
        if _ytdlp() is None:
            raise YouTubeUnavailable(reachable()[1])

        for sub in self.most_watched_creators:
            try:
                results = self._channel_uploads(sub.channel_id, limit=3)
                for video in results:
                    if video.video_id != sub.last_seen_video_id:
                        new_uploads.append((sub, video))
                        break
            except Exception as exc:  # noqa: BLE001 - one dead channel is not a failure
                log.debug("new upload check failed for %s: %s", sub.channel_name, exc)

        return new_uploads

    def _channel_uploads(self, channel_id: str, *, limit: int = 3) -> list[VideoMeta]:
        """The channel's most recent uploads, newest first.

        This used to run `ytsearch3:channel:UC…`, which is a full-text search
        for the literal words — it returns whatever YouTube thinks matches that
        phrase, which is usually not the channel and never reliably its newest
        video. The uploads tab is the actual list, and `--playlist-end` stops
        yt-dlp walking a decade of back catalogue to answer "anything new?".
        """
        target = channel_id if channel_id.startswith("http") else f"channel/{channel_id}"
        url = target if target.startswith("http") else f"https://www.youtube.com/{target}/videos"
        result = subprocess.run(
            [
                "yt-dlp",
                "--skip-download",
                "--dump-json",
                "--flat-playlist",
                "--playlist-end",
                str(max(1, limit)),
                url,
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp channel listing failed: {result.stderr[:200]}")
        return [
            self._meta_from_dict(json.loads(line))
            for line in result.stdout.strip().splitlines()
            if line.strip()
        ]

    def _search_via_ytdlp(self, query: str, max_results: int) -> list[VideoMeta]:
        """Search using yt-dlp's ytsearch extractor."""
        result = subprocess.run(
            [
                "yt-dlp",
                "--skip-download",
                "--dump-json",
                "--flat-playlist",
                f"ytsearch{max_results}:{query}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp search failed: {result.stderr[:200]}")

        videos: list[VideoMeta] = []
        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            videos.append(self._meta_from_dict(data))
        return videos

    def _search_via_api(self, query: str, max_results: int) -> list[VideoMeta]:
        """Fallback search using YouTube Data API v3."""
        import urllib.request
        import urllib.parse

        params = urllib.parse.urlencode(
            {
                "part": "snippet",
                "q": query,
                "type": "video",
                "maxResults": max_results,
                "key": self._api_key,
            }
        )
        url = f"https://www.googleapis.com/youtube/v3/search?{params}"

        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())

        videos: list[VideoMeta] = []
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            videos.append(
                VideoMeta(
                    video_id=item["id"]["videoId"],
                    title=snippet.get("title", ""),
                    channel=snippet.get("channelTitle", ""),
                    channel_id=snippet.get("channelId", ""),
                    duration_sec=0,  # snippet search doesn't include duration
                    description=snippet.get("description", ""),
                    upload_date=snippet.get("publishedAt", ""),
                )
            )
        return videos

    def _fetch_transcript(self, data: dict) -> list[dict]:
        """Real caption text with timings, or nothing.

        This used to write the literal string "[auto-caption available]" into
        the transcript and return it, so `has_transcript` said yes while the
        transcript was a note to a future programmer. Anything downstream
        looking for what was said got that sentence instead. Either the words
        are here or the list is empty — those are the only two honest answers.

        Author-written subtitles are preferred over machine captions: they are
        punctuated, and the Scholar reads them for technique names.
        """
        for key in ("subtitles", "automatic_captions"):
            tracks = (data.get(key) or {}).get("en") or []
            for track in tracks:
                if track.get("ext") not in ("json3", "vtt"):
                    continue
                url = track.get("url")
                if not url:
                    continue
                try:
                    import urllib.request

                    with urllib.request.urlopen(url, timeout=20) as response:
                        raw = response.read().decode("utf-8", "replace")
                except Exception as exc:  # noqa: BLE001 - a missing caption is not a failure
                    log.info("could not fetch captions: %s", exc)
                    continue
                cues = _parse_json3(raw) if track.get("ext") == "json3" else _parse_vtt(raw)
                if cues:
                    log.info("read %d caption cues from the %s track", len(cues), key)
                    return cues
        return []

    def _meta_from_dict(self, data: dict) -> VideoMeta:
        """Convert a yt-dlp info dict into a VideoMeta."""
        chapters = []
        for ch in data.get("chapters") or []:
            chapters.append(
                {
                    "title": ch.get("title", ""),
                    "start_time": ch.get("start_time", 0),
                    "end_time": ch.get("end_time", 0),
                }
            )

        transcript = self._fetch_transcript(data)

        return VideoMeta(
            video_id=data.get("id", data.get("url", "")),
            title=data.get("title", ""),
            channel=data.get("channel", data.get("uploader", "")),
            channel_id=data.get("channel_id", data.get("uploader_id", "")),
            duration_sec=float(data.get("duration") or 0),
            description=data.get("description", ""),
            chapters=chapters,
            tags=data.get("tags") or [],
            upload_date=data.get("upload_date", ""),
            view_count=int(data.get("view_count") or 0),
            like_count=int(data.get("like_count") or 0),
            transcript_segments=transcript,
        )
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

log = logging.getLogger("auteur.scholar.youtube")

@dataclass
class VideoMeta:
    video_id: str
    title: str
    channel: str
    duration_seconds: int = 0
    view_count: int = 0
    url: str = ""
    thumbnail: str = ""
    description: str = ""

    def __post_init__(self):
        if not self.url:
            self.url = f"https://www.youtube.com/watch?v={self.video_id}"

class SearchStrategy(Enum):
    API = "api"
    YTDLP = "yt-dlp"
    AUTO = "auto"

class YouTubeAccess:
    def __init__(self, strategy: SearchStrategy = SearchStrategy.AUTO):
        self.api_key: Optional[str] = os.environ.get("YOUTUBE_API_KEY")
        self.strategy = strategy
        self._ytdlp_available = self._check_ytdlp()

        if not self.api_key and not self._ytdlp_available:
            log.warning("YouTubeAccess: no API key and yt-dlp not installed. YouTube features unavailable.")

    def search(self, query: str, max_results: int = 5) -> list[VideoMeta]:
        if self.strategy == SearchStrategy.API or (self.strategy == SearchStrategy.AUTO and self.api_key):
            try:
                return self._search_api(query, max_results)
            except Exception as exc:
                log.warning("YouTube API search failed (%s), falling back to yt-dlp", exc)
        if self._ytdlp_available:
            return self._search_ytdlp(query, max_results)
        log.error("YouTube search unavailable.")
        return []

    def get_metadata(self, video_id: str) -> Optional[VideoMeta]:
        if self.api_key:
            try:
                return self._get_metadata_api(video_id)
            except Exception as exc:
                log.warning("YouTube API metadata fetch failed (%s)", exc)
        if self._ytdlp_available:
            return self._get_metadata_ytdlp(video_id)
        return None

    def _check_ytdlp(self) -> bool:
        try:
            import yt_dlp  # noqa: F401
            return True
        except ImportError:
            return False

    def _search_api(self, query: str, max_results: int) -> list[VideoMeta]:
        import json, urllib.parse, urllib.request
        params = urllib.parse.urlencode({"part": "snippet", "q": query, "type": "video", "maxResults": max_results, "key": self.api_key})
        with urllib.request.urlopen(f"https://www.googleapis.com/youtube/v3/search?{params}", timeout=10) as r:
            data = json.loads(r.read())
        return [VideoMeta(video_id=i["id"].get("videoId",""), title=i["snippet"].get("title",""), channel=i["snippet"].get("channelTitle",""), thumbnail=i["snippet"].get("thumbnails",{}).get("default",{}).get("url",""), description=i["snippet"].get("description","")) for i in data.get("items",[])]

    def _get_metadata_api(self, video_id: str) -> Optional[VideoMeta]:
        import json, urllib.parse, urllib.request
        params = urllib.parse.urlencode({"part": "snippet,contentDetails,statistics", "id": video_id, "key": self.api_key})
        with urllib.request.urlopen(f"https://www.googleapis.com/youtube/v3/videos?{params}", timeout=10) as r:
            data = json.loads(r.read())
        items = data.get("items", [])
        if not items:
            return None
        i = items[0]
        return VideoMeta(video_id=video_id, title=i["snippet"].get("title",""), channel=i["snippet"].get("channelTitle",""), description=i["snippet"].get("description",""), thumbnail=i["snippet"].get("thumbnails",{}).get("default",{}).get("url",""), view_count=int(i.get("statistics",{}).get("viewCount",0)))

    def _search_ytdlp(self, query: str, max_results: int) -> list[VideoMeta]:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True, "playlist_items": f"1:{max_results}"}) as ydl:
            info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
        return [VideoMeta(video_id=e.get("id",""), title=e.get("title",""), channel=e.get("channel", e.get("uploader","")), duration_seconds=e.get("duration",0), view_count=e.get("view_count",0), thumbnail=e.get("thumbnail","")) for e in (info or {}).get("entries",[])]

    def _get_metadata_ytdlp(self, video_id: str) -> Optional[VideoMeta]:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        if not info:
            return None
        return VideoMeta(video_id=video_id, title=info.get("title",""), channel=info.get("channel", info.get("uploader","")), duration_seconds=info.get("duration",0), view_count=info.get("view_count",0), thumbnail=info.get("thumbnail",""), description=info.get("description",""))
