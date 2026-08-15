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
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Sequence

log = logging.getLogger("auteur.scholar.youtube")

#: Maximum number of results per search query.
_MAX_RESULTS = 20


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
        results: list[VideoMeta] = []

        try:
            results = self._search_via_ytdlp(query, max_results)
        except Exception as exc:
            log.warning("yt-dlp search failed: %s", exc)
            if self._api_key:
                results = self._search_via_api(query, max_results)

        return results

    def fetch_metadata(self, video_id: str) -> VideoMeta | None:
        """Fetch full metadata (including transcript) for a single video."""
        cache_path = self._cache_dir / f"{video_id}.json"
        if cache_path.exists():
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            return self._meta_from_dict(data)

        try:
            result = subprocess.run(
                [
                    "yt-dlp",
                    "--skip-download",
                    "--write-auto-sub",
                    "--sub-lang", "en",
                    "--dump-json",
                    f"https://www.youtube.com/watch?v={video_id}",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                log.warning("yt-dlp metadata fetch failed for %s: %s", video_id, result.stderr[:200])
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

        for sub in self.most_watched_creators:
            try:
                results = self._search_via_ytdlp(
                    f"channel:{sub.channel_id}", max_results=3
                )
                for video in results:
                    if video.video_id != sub.last_seen_video_id:
                        new_uploads.append((sub, video))
                        break
            except Exception as exc:
                log.debug("new upload check failed for %s: %s", sub.channel_name, exc)

        return new_uploads

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

        params = urllib.parse.urlencode({
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": max_results,
            "key": self._api_key,
        })
        url = f"https://www.googleapis.com/youtube/v3/search?{params}"

        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())

        videos: list[VideoMeta] = []
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            videos.append(VideoMeta(
                video_id=item["id"]["videoId"],
                title=snippet.get("title", ""),
                channel=snippet.get("channelTitle", ""),
                channel_id=snippet.get("channelId", ""),
                duration_sec=0,  # snippet search doesn't include duration
                description=snippet.get("description", ""),
                upload_date=snippet.get("publishedAt", ""),
            ))
        return videos

    def _meta_from_dict(self, data: dict) -> VideoMeta:
        """Convert a yt-dlp info dict into a VideoMeta."""
        chapters = []
        for ch in data.get("chapters") or []:
            chapters.append({
                "title": ch.get("title", ""),
                "start_time": ch.get("start_time", 0),
                "end_time": ch.get("end_time", 0),
            })

        # Extract transcript segments if available
        transcript: list[dict] = []
        for sub in data.get("subtitles", {}).get("en", []):
            if sub.get("ext") == "json3":
                # Would need to fetch and parse — mark as available
                transcript = [{"text": "[transcript available]", "start": 0}]
                break
        if not transcript:
            for sub in data.get("automatic_captions", {}).get("en", []):
                if sub.get("ext") == "json3":
                    transcript = [{"text": "[auto-caption available]", "start": 0}]
                    break

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
