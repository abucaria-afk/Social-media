"""The clip dossier: everything known about one piece of footage, plus the
ranges inside it actually worth using.

An editor does not think "clip 4". They think "the good bit of clip 4, the two
seconds before he turns". A Take is that thought, made explicit and scored, so
the director — human, heuristic or model — chooses from candidates rather than
from raw files.
"""

from __future__ import annotations

import concurrent.futures
import logging
from dataclasses import dataclass, field

import numpy as np

from ..ingest import MediaAsset
from .audio import AudioAnalysis, analyse_audio
from .video import VideoAnalysis, analyse_video

log = logging.getLogger("auteur.analysis.dossier")

#: Shortest and longest a candidate take may be, in seconds.
MIN_TAKE = 0.6
MAX_TAKE = 6.0


@dataclass
class Take:
    """A usable range inside a clip, with the traits that decide where it goes."""

    clip_id: str
    start: float
    end: float
    score: float = 0.0

    motion: float = 0.0
    motion_peak: float = 0.0
    sharpness: float = 0.0
    exposure: float = 0.5
    contrast: float = 0.0
    energy: float = 0.0  # audio energy under this range
    subject: tuple[float, float] = (0.5, 0.5)
    subject_drift: float = 0.0
    camera: str = "static"  # static | pan-left | pan-right | tilt | push-in | pull-out | handheld
    scale: str = "medium"  # wide | medium | close  (estimated, see _estimate_scale)
    stability: float = 1.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_json(self) -> dict:
        return {
            "clip": self.clip_id,
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "duration": round(self.duration, 2),
            "score": round(self.score, 3),
            "motion": round(self.motion, 3),
            "sharpness": round(self.sharpness, 3),
            "exposure": round(self.exposure, 3),
            "camera": self.camera,
            "scale": self.scale,
            "subject": [round(self.subject[0], 2), round(self.subject[1], 2)],
        }


@dataclass
class ClipDossier:
    """One clip, fully read."""

    clip_id: str
    asset: MediaAsset
    video: VideoAnalysis
    audio: AudioAnalysis
    takes: list[Take] = field(default_factory=list)
    #: Description of what is in frame, when a vision pass has run.
    description: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.asset.duration

    @property
    def best_take(self) -> Take | None:
        return max(self.takes, key=lambda t: t.score, default=None)

    @property
    def quality(self) -> float:
        """Overall usability, 0..1 — the number that decides what gets binned."""
        if not self.takes:
            return 0.0
        top = sorted((t.score for t in self.takes), reverse=True)[:3]
        return float(np.mean(top))

    def to_json(self, *, max_takes: int = 6) -> dict:
        """Compact form handed to the director. Small enough to fit many clips."""
        best = sorted(self.takes, key=lambda t: -t.score)[:max_takes]
        payload = {
            "id": self.clip_id,
            "file": self.asset.name,
            "duration": round(self.duration, 2),
            "kind": self.asset.kind,
            "orientation": "vertical" if self.asset.is_vertical else "horizontal",
            "quality": round(self.quality, 3),
            "look": {
                "brightness": round(float(np.mean(self.video.luma)) if len(self.video.luma) else 0.5, 3),
                "contrast": round(float(np.mean(self.video.contrast)) if len(self.video.contrast) else 0.0, 3),
                "saturation": round(self.video.saturation, 3),
                "warmth": round(self.video.warmth, 3),
                "palette": ["#%02x%02x%02x" % c for c in self.video.palette[:4]],
            },
            "sound": {
                "present": not self.audio.silent,
                "speech_likelihood": round(self.audio.speechiness, 2),
                "loudness_db": round(self.audio.loudness, 1),
            },
            "internal_cuts": self.video.shot_boundaries[:12],
            "takes": [take.to_json() for take in best],
        }
        if self.description:
            payload["seen"] = self.description
        if self.tags:
            payload["tags"] = self.tags
        return payload


def _estimate_scale(edges: float, motion: float, subject_drift: float) -> str:
    """Guess shot size from detail density and how fast the frame changes.

    This is a proxy, not a depth estimate: wide shots carry dense fine detail
    and move little in frame; close-ups are smoother and swing further when the
    camera or subject shifts. It is right often enough to build a size rhythm
    from, and nothing downstream breaks when it is wrong.
    """
    if edges > 0.62 and motion < 0.08:
        return "wide"
    if edges < 0.32 or subject_drift > 0.22:
        return "close"
    return "medium"


def _describe_camera(pan: float, tilt: float, zoom: float, motion_variance: float) -> str:
    if motion_variance > 0.035 and abs(pan) < 0.25 and abs(tilt) < 0.25:
        return "handheld"
    if abs(zoom) > 0.10 and abs(zoom) > max(abs(pan), abs(tilt)):
        return "push-in" if zoom > 0 else "pull-out"
    if abs(pan) > 0.06 and abs(pan) >= abs(tilt):
        return "pan-right" if pan > 0 else "pan-left"
    if abs(tilt) > 0.06:
        return "tilt-down" if tilt > 0 else "tilt-up"
    return "static"


def _score_window(stats: dict, audio_energy: float) -> float:
    """How much an editor would want this second and a half of footage.

    Sharpness and clean exposure earn points; crushed blacks, blown highlights
    and dead-still frames lose them. Motion is rewarded on a curve — some is
    life, too much is unusable.
    """
    sharpness = stats["sharpness"]
    exposure = stats["luma"]
    contrast = stats["contrast"]
    motion = stats["motion"]

    # Penalise both ends of the exposure range, gently around a broad middle.
    exposure_penalty = max(0.0, abs(exposure - 0.48) - 0.18) / 0.32
    clipping = stats["shadows"] * 1.2 + stats["highlights"] * 1.6

    # Peaks around a lively-but-controlled 0.05 mean frame difference.
    motion_reward = float(np.exp(-((motion - 0.05) ** 2) / (2 * 0.045**2)))

    score = (
        sharpness * 0.34
        + motion_reward * 0.24
        + min(contrast * 3.0, 1.0) * 0.16
        + min(audio_energy * 1.5, 1.0) * 0.08
        + 0.18
    )
    score -= exposure_penalty * 0.30 + min(clipping, 1.0) * 0.25
    return float(np.clip(score, 0.0, 1.0))


def _segments(dossier_video: VideoAnalysis, duration: float) -> list[tuple[float, float]]:
    """Split a clip at the cuts it already contains, so we never cut across one."""
    marks = [0.0, *[b for b in dossier_video.shot_boundaries if 0.2 < b < duration - 0.2], duration]
    return [
        (marks[i], marks[i + 1])
        for i in range(len(marks) - 1)
        if marks[i + 1] - marks[i] >= MIN_TAKE * 0.8
    ]


def _find_takes(clip_id: str, video: VideoAnalysis, audio: AudioAnalysis, duration: float) -> list[Take]:
    """Slide a window over each continuous segment and keep the local peaks."""
    takes: list[Take] = []
    window = 1.5
    hop = 0.5

    for seg_start, seg_end in _segments(video, duration):
        seg_length = seg_end - seg_start
        if seg_length < MIN_TAKE:
            continue

        # A segment shorter than the analysis window is simply taken whole.
        if seg_length <= window:
            positions = [seg_start]
            span = seg_length
        else:
            count = max(1, int((seg_length - window) / hop) + 1)
            positions = [seg_start + i * hop for i in range(count)]
            span = window

        scored: list[Take] = []
        for start in positions:
            end = min(start + span, seg_end)
            if end - start < MIN_TAKE * 0.8:
                continue
            stats = video.slice_stats(start, end)
            energy = audio.energy_over(start, end) if not audio.silent else 0.0

            i0, i1 = video.index_of(start), max(video.index_of(end), video.index_of(start) + 1)
            motion_slice = video.motion[i0:i1]
            variance = float(motion_slice.var()) if motion_slice.size else 0.0
            stability = float(np.clip(1.0 - variance * 12.0, 0.0, 1.0))

            scored.append(
                Take(
                    clip_id=clip_id,
                    start=round(start, 3),
                    end=round(end, 3),
                    score=_score_window(stats, energy) * (0.75 + 0.25 * stability),
                    motion=stats["motion"],
                    motion_peak=stats["motion_peak"],
                    sharpness=stats["sharpness"],
                    exposure=stats["luma"],
                    contrast=stats["contrast"],
                    energy=energy,
                    subject=(stats["subject_x"], stats["subject_y"]),
                    subject_drift=stats["subject_drift"],
                    camera=_describe_camera(stats["pan"], stats["tilt"], stats["zoom"], variance),
                    scale=_estimate_scale(stats["edges"], stats["motion"], stats["subject_drift"]),
                    stability=stability,
                )
            )

        takes.extend(_pick_peaks(scored, seg_start, seg_end))

    takes.sort(key=lambda t: -t.score)
    return takes[:24]


def _pick_peaks(scored: list[Take], seg_start: float, seg_end: float) -> list[Take]:
    """Greedy non-overlapping selection, best first, then widened to breathe.

    Widening matters: the strongest 1.5s window is a peak, but a cut needs a
    little runway on either side of it or it feels clipped.
    """
    chosen: list[Take] = []
    for take in sorted(scored, key=lambda t: -t.score):
        if any(take.start < other.end and other.start < take.end for other in chosen):
            continue
        chosen.append(take)
        if len(chosen) >= 8:
            break

    widened: list[Take] = []
    for take in chosen:
        others = [t for t in chosen if t is not take]
        lower = max([t.end for t in others if t.end <= take.start], default=seg_start)
        upper = min([t.start for t in others if t.start >= take.end], default=seg_end)
        take.start = round(max(lower, take.start - 0.75), 3)
        take.end = round(min(upper, take.end + min(MAX_TAKE, 2.0)), 3)
        if take.duration >= MIN_TAKE:
            widened.append(take)

    widened.sort(key=lambda t: t.start)
    return widened


def build_dossier(
    clip_id: str, asset: MediaAsset, *, analysis_fps: float = 6.0, analysis_width: int = 128
) -> ClipDossier:
    """Watch and listen to one clip, then work out which parts of it are usable."""
    video = analyse_video(asset, analysis_fps=analysis_fps, width=analysis_width)
    audio = analyse_audio(asset) if asset.has_audio else AudioAnalysis(duration=asset.duration)
    takes = _find_takes(clip_id, video, audio, asset.duration)

    if not takes:
        # Nothing scored well, but footage is footage: keep the middle of it.
        span = min(asset.duration, 3.0)
        start = max(0.0, (asset.duration - span) / 2)
        takes = [Take(clip_id=clip_id, start=round(start, 3), end=round(start + span, 3), score=0.2)]

    return ClipDossier(clip_id=clip_id, asset=asset, video=video, audio=audio, takes=takes)


def build_dossiers(
    assets: list[MediaAsset], *, analysis_fps: float = 6.0, analysis_width: int = 128, workers: int = 4
) -> list[ClipDossier]:
    """Analyse the whole bin, in parallel. Order of the input list is preserved."""
    numbered = [(f"C{index + 1:02d}", asset) for index, asset in enumerate(assets)]
    results: dict[str, ClipDossier] = {}

    def work(item: tuple[str, MediaAsset]) -> ClipDossier:
        clip_id, asset = item
        log.info("analysing %s (%s)", clip_id, asset.name)
        return build_dossier(clip_id, asset, analysis_fps=analysis_fps, analysis_width=analysis_width)

    if workers > 1 and len(numbered) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(numbered))) as pool:
            for dossier in pool.map(work, numbered):
                results[dossier.clip_id] = dossier
    else:
        for item in numbered:
            dossier = work(item)
            results[dossier.clip_id] = dossier

    return [results[clip_id] for clip_id, _ in numbered if clip_id in results]
