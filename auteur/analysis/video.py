"""Frame-level analysis: what is actually happening inside a shot.

Everything here runs on a small greyscale proxy of the footage (128px wide,
6fps by default). That is more than enough to answer the questions an editor
asks while scrubbing: is it sharp, is it exposed, is it moving, which way is it
moving, where is the subject, and where does one shot end and the next begin.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from .. import ffmpeg
from ..ingest import MediaAsset

log = logging.getLogger("auteur.analysis.video")

_LAPLACIAN = np.array([[0.0, 1.0, 0.0], [1.0, -4.0, 1.0], [0.0, 1.0, 0.0]], dtype=np.float32)


@dataclass
class VideoAnalysis:
    """Per-frame measurements plus the conclusions drawn from them."""

    fps: float
    duration: float
    width: int
    height: int

    #: One value per sampled frame.
    luma: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0, np.float32))
    contrast: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0, np.float32))
    sharpness: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0, np.float32))
    edges: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0, np.float32))
    motion: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0, np.float32))
    #: Global camera motion, normalised to frame widths per second.
    pan: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0, np.float32))
    tilt: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0, np.float32))
    zoom: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0, np.float32))
    #: Subject position in normalised frame coordinates, one row per frame.
    subject: np.ndarray = field(repr=False, default_factory=lambda: np.zeros((0, 2), np.float32))
    #: Clipping, as a fraction of pixels crushed or blown.
    shadows: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0, np.float32))
    highlights: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0, np.float32))

    #: Colour, measured on a coarse RGB proxy.
    mean_rgb: tuple[float, float, float] = (0.5, 0.5, 0.5)
    saturation: float = 0.0
    warmth: float = 0.0  # -1 cold/blue .. +1 warm/amber
    palette: list[tuple[int, int, int]] = field(default_factory=list)

    #: Detected cut points inside the source, in seconds.
    shot_boundaries: list[float] = field(default_factory=list)

    def index_of(self, seconds: float) -> int:
        if not len(self.luma):
            return 0
        return int(np.clip(round(seconds * self.fps), 0, len(self.luma) - 1))

    def slice_stats(self, start: float, end: float) -> dict[str, float]:
        """Summarise a time range — the numbers a director actually asks for."""
        i0, i1 = self.index_of(start), max(self.index_of(end), self.index_of(start) + 1)
        window = slice(i0, i1)

        def mean(array: np.ndarray, default: float = 0.0) -> float:
            chunk = array[window]
            return float(chunk.mean()) if chunk.size else default

        def peak(array: np.ndarray, default: float = 0.0) -> float:
            chunk = array[window]
            return float(chunk.max()) if chunk.size else default

        subject = self.subject[window]
        anchor = subject.mean(axis=0) if subject.size else np.array([0.5, 0.5], np.float32)
        drift = float(np.linalg.norm(subject[-1] - subject[0])) if len(subject) > 1 else 0.0

        return {
            "luma": mean(self.luma, 0.5),
            "contrast": mean(self.contrast),
            "sharpness": mean(self.sharpness),
            "edges": mean(self.edges),
            "motion": mean(self.motion),
            "motion_peak": peak(self.motion),
            "pan": mean(self.pan),
            "tilt": mean(self.tilt),
            "zoom": mean(self.zoom),
            "subject_x": float(anchor[0]),
            "subject_y": float(anchor[1]),
            "subject_drift": drift,
            "shadows": mean(self.shadows),
            "highlights": mean(self.highlights),
        }


def _convolve3(frames: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """3x3 valid convolution over a stack of frames, without scipy."""
    n, h, w = frames.shape
    if h < 3 or w < 3:
        return np.zeros((n, max(h - 2, 1), max(w - 2, 1)), np.float32)
    out = np.zeros((n, h - 2, w - 2), np.float32)
    for dy in range(3):
        for dx in range(3):
            weight = kernel[dy, dx]
            if weight:
                out += weight * frames[:, dy : dy + h - 2, dx : dx + w - 2]
    return out


def _normalise(values: np.ndarray) -> np.ndarray:
    """Scale to 0..1 using robust percentiles, so one flash cannot dominate."""
    if values.size == 0:
        return values
    lo = float(np.percentile(values, 5))
    hi = float(np.percentile(values, 95))
    if hi - lo < 1e-6:
        return np.zeros_like(values)
    return np.clip((values - lo) / (hi - lo), 0.0, 1.0)


def _global_motion(prev: np.ndarray, curr: np.ndarray) -> tuple[float, float, float]:
    """Fit pan / tilt / zoom between two frames.

    Brightness constancy gives Ix·u + Iy·v + It = 0. Model the flow as a
    translation plus a uniform scale about the frame centre:

        u = tx + s·(x - cx)
        v = ty + s·(y - cy)

    and solve the resulting 3x3 least-squares system. Cheap, and it tells the
    difference between a whip pan, a push-in and a locked-off tripod — which is
    exactly what decides whether two shots can cut together.
    """
    h, w = prev.shape
    if h < 8 or w < 8:
        return 0.0, 0.0, 0.0

    iy, ix = np.gradient(prev)
    it = curr - prev

    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    xs = (xs - (w - 1) / 2.0) / max(w, 1)
    ys = (ys - (h - 1) / 2.0) / max(h, 1)
    radial = ix * xs + iy * ys

    design = np.stack([ix.ravel(), iy.ravel(), radial.ravel()], axis=1)
    target = -it.ravel()

    ata = design.T @ design
    atb = design.T @ target
    # Ridge term keeps flat, textureless frames from producing wild fits.
    ata += np.eye(3, dtype=np.float32) * (1e-3 * max(float(np.trace(ata)), 1e-6) / 3.0 + 1e-8)
    try:
        tx, ty, scale = np.linalg.solve(ata, atb)
    except np.linalg.LinAlgError:  # pragma: no cover - singular textureless frame
        return 0.0, 0.0, 0.0

    return (
        float(np.clip(tx / max(w, 1), -1.0, 1.0)),
        float(np.clip(ty / max(h, 1), -1.0, 1.0)),
        float(np.clip(scale, -1.0, 1.0)),
    )


def _subject_track(frames: np.ndarray, energy: np.ndarray) -> np.ndarray:
    """Where the eye goes: centre of mass of edge detail weighted by movement.

    Not a face detector, but it reliably finds the moving, detailed thing in the
    frame — which is what a reframe needs to keep inside the crop.
    """
    n, h, w = frames.shape
    if n == 0 or h < 4 or w < 4:
        return np.full((max(n, 0), 2), 0.5, np.float32)

    weight = energy + 1e-6
    weight = weight / weight.max(axis=(1, 2), keepdims=True).clip(1e-6)

    eh, ew = weight.shape[1], weight.shape[2]
    xs = (np.arange(ew, dtype=np.float32) + 0.5) / ew
    ys = (np.arange(eh, dtype=np.float32) + 0.5) / eh

    mass = weight.sum(axis=(1, 2)).clip(1e-6)
    cx = (weight.sum(axis=1) @ xs) / mass
    cy = (weight.sum(axis=2) @ ys) / mass

    track = np.stack([cx, cy], axis=1).astype(np.float32)
    return _smooth_track(track, window=max(3, int(n * 0.08) | 1))


def _smooth_track(track: np.ndarray, window: int = 5) -> np.ndarray:
    """Moving average — a reframe that jitters is worse than one that lags."""
    if len(track) < 3 or window < 3:
        return track
    window = min(window, len(track) | 1)
    kernel = np.ones(window, np.float32) / window
    padded = np.pad(track, ((window // 2, window // 2), (0, 0)), mode="edge")
    return np.stack(
        [np.convolve(padded[:, axis], kernel, mode="valid")[: len(track)] for axis in range(2)],
        axis=1,
    ).astype(np.float32)


#: The lowest a shot boundary can score and still be one. Below this the
#: difference between two frames is grain, exposure drift or a camera move —
#: not a cut — whatever the rest of the clip's distribution looks like.
FLOOR = 0.28


def _detect_shots(
    frames: np.ndarray, motion: np.ndarray, fps: float, *, min_gap: float = 0.35
) -> list[float]:
    """Find hard cuts already present in the source.

    Handed a pre-edited clip, the agent must not treat it as one continuous
    shot — it would cut across someone else's cut. Compares both frame
    difference and luma histograms, and only fires where both agree.

    `min_gap` is the refractory period: two "cuts" closer together than this are
    one cut plus its rattle. The default suits rushes. Measuring a *reference*
    reel needs it much smaller, because short-form montages really do cut every
    four frames, and a 350ms floor caps what can be reported at under three cuts
    a second — which would quietly misdescribe the very styles worth copying.

    The threshold is a median plus a MAD rather than a mean plus a standard
    deviation. With a mean, a clip where most sampled frames are boundaries
    pulls its own threshold above every one of them and the function returns no
    cuts at all — confidently claiming a six-cuts-a-second montage is one
    continuous take, which is the exact mistake this exists to prevent.
    """
    n = len(frames)
    if n < 4 or fps <= 0:
        return []

    hist = np.stack(
        [np.bincount((f.ravel() // 8).astype(np.int64), minlength=32)[:32] for f in frames]
    )
    hist = hist.astype(np.float32)
    hist /= hist.sum(axis=1, keepdims=True).clip(1e-6)
    hist_delta = np.abs(np.diff(hist, axis=0)).sum(axis=1) * 0.5  # 0..1

    pixel_delta = motion[1:] if len(motion) > 1 else np.zeros(0, np.float32)
    size = min(len(hist_delta), len(pixel_delta))
    if size == 0:
        return []
    hist_delta, pixel_delta = hist_delta[:size], _normalise(pixel_delta[:size])

    score = hist_delta * 0.6 + pixel_delta * 0.4
    median = float(np.median(score))
    mad = float(np.median(np.abs(score - median))) or 1e-6

    def crossings(threshold: float) -> list[float]:
        found: list[float] = []
        last = -1e9
        for index in np.flatnonzero(score > threshold):
            time = float(index + 1) / fps
            if time - last > min_gap:
                found.append(round(time, 3))
                last = time
        return found

    boundaries = crossings(max(median + 4.0 * mad * 1.4826, FLOOR))

    # The derived threshold has to be sanity-checked against the floor.
    #
    # A median plus a MAD is robust to a *few* outliers and this is not that
    # case: on a montage a large minority of frame pairs are boundaries, so
    # they enter the spread estimate and push the threshold above the very
    # population it is meant to select. Measured on three of the reference
    # reels, the derivation landed at 0.85 — above their 99th percentile —
    # and reported a fifteen-second montage as two shots.
    #
    # One-sided estimators do not fix it, because on those reels the quiet bed
    # is genuinely high: their cuts fall between shots that share a palette,
    # so the histogram barely moves and the floor is doing the real work.
    #
    # So the test is on the outcome rather than on the distribution: if
    # raising the threshold above the floor costs more than three quarters of
    # the boundaries, the derivation overshot and the floor is the better
    # answer. On unedited footage both counts are zero and nothing fires,
    # which is the case this whole function exists to protect.
    plain = crossings(FLOOR)
    # Dense enough to be a montage at all. A ratio test on its own fires on a
    # continuous take that happens to produce one floor crossing from a camera
    # move — 0 is less than a quarter of 1 — and reports a cut in footage with
    # none in it, which is the mistake in the opposite direction and the more
    # damaging of the two. Eight boundaries and at least one a second is the
    # bar; the reels this was written for clear it by a factor of two.
    span = len(score) / fps
    montage = len(plain) >= 8 and span > 0 and len(plain) / span >= 1.0
    if montage and len(boundaries) < 0.25 * len(plain):
        log.debug(
            "shot threshold overshot (%d cuts against %d at the floor), using the floor",
            len(boundaries),
            len(plain),
        )
        boundaries = plain
    return boundaries


def resolves_cutting(frames: np.ndarray, motion: np.ndarray) -> bool:
    """Is the sample rate fast enough to describe how this was cut?

    When most consecutive sampled frames come from different shots there is no
    quiet bed left for a boundary to stand out against, and any detector — this
    one included — will under-report. The answer is to sample faster, so say so
    rather than hand back a number that reads as measurement.
    """
    if len(frames) < 4 or len(motion) < 2:
        return True
    hist = np.stack(
        [np.bincount((f.ravel() // 8).astype(np.int64), minlength=32)[:32] for f in frames]
    ).astype(np.float32)
    hist /= hist.sum(axis=1, keepdims=True).clip(1e-6)
    delta = np.abs(np.diff(hist, axis=0)).sum(axis=1) * 0.5
    return float(np.mean(delta > 0.28)) < 0.4


def _colour_profile(asset: MediaAsset, analysis_fps: float) -> tuple:
    """Mean colour, saturation, warmth and a small dominant palette."""
    stream = ffmpeg.read_frames(
        asset.path,
        width=48,
        fps=min(analysis_fps, 2.0),
        color=True,
        max_frames=240,
        still=asset.kind == "image",
    )
    if len(stream) == 0:
        return (0.5, 0.5, 0.5), 0.0, 0.0, []

    pixels = stream.frames.reshape(-1, 3).astype(np.float32) / 255.0
    mean_rgb = tuple(float(v) for v in pixels.mean(axis=0))

    hi = pixels.max(axis=1)
    lo = pixels.min(axis=1)
    saturation = float(np.mean((hi - lo) / hi.clip(1e-6)))
    warmth = float(np.clip((mean_rgb[0] - mean_rgb[2]) * 3.0, -1.0, 1.0))

    return mean_rgb, saturation, warmth, _palette(pixels)


def _palette(
    pixels: np.ndarray, colours: int = 5, iterations: int = 8
) -> list[tuple[int, int, int]]:
    """k-means on a subsample. Used to keep the grade sympathetic to the footage."""
    if len(pixels) > 20000:
        step = len(pixels) // 20000
        pixels = pixels[::step]
    if len(pixels) < colours:
        return [tuple(int(c * 255) for c in pixel) for pixel in pixels]

    rng = np.random.default_rng(7)
    centres = pixels[rng.choice(len(pixels), colours, replace=False)].copy()
    for _ in range(iterations):
        distance = ((pixels[:, None, :] - centres[None, :, :]) ** 2).sum(axis=2)
        labels = distance.argmin(axis=1)
        for k in range(colours):
            members = pixels[labels == k]
            if len(members):
                centres[k] = members.mean(axis=0)

    counts = np.bincount(labels, minlength=colours)
    order = np.argsort(-counts)
    return [tuple(int(np.clip(c, 0, 1) * 255) for c in centres[k]) for k in order]


def analyse_video(
    asset: MediaAsset, *, analysis_fps: float = 6.0, width: int = 128
) -> VideoAnalysis:
    """Watch a clip end to end and write down everything measurable about it."""
    if asset.kind == "image":
        return _analyse_still(asset, analysis_fps, width)

    stream = ffmpeg.read_frames(asset.path, width=width, fps=analysis_fps)
    n = len(stream)
    if n == 0:
        log.warning("no frames decoded from %s", asset.name)
        return VideoAnalysis(fps=analysis_fps, duration=asset.duration, width=0, height=0)

    frames = stream.frames.astype(np.float32) / 255.0

    luma = frames.mean(axis=(1, 2))
    contrast = frames.std(axis=(1, 2))
    shadows = (frames < 0.04).mean(axis=(1, 2))
    highlights = (frames > 0.96).mean(axis=(1, 2))

    laplacian = _convolve3(frames, _LAPLACIAN)
    sharpness = laplacian.var(axis=(1, 2))
    edges = np.abs(laplacian).mean(axis=(1, 2))

    if n > 1:
        difference = np.abs(np.diff(frames, axis=0))
        motion = np.concatenate([[0.0], difference.mean(axis=(1, 2))]).astype(np.float32)
        energy = np.concatenate([difference[:1], difference], axis=0)
    else:
        motion = np.zeros(1, np.float32)
        energy = np.abs(frames - frames.mean())

    pan = np.zeros(n, np.float32)
    tilt = np.zeros(n, np.float32)
    zoom = np.zeros(n, np.float32)
    for i in range(1, n):
        pan[i], tilt[i], zoom[i] = _global_motion(frames[i - 1], frames[i])
    # Report camera movement per second, not per sampled frame.
    pan *= analysis_fps
    tilt *= analysis_fps
    zoom *= analysis_fps

    subject = _subject_track(frames, energy * (np.abs(_pad_like(laplacian, frames)) + 0.05))
    mean_rgb, saturation, warmth, palette = _colour_profile(asset, analysis_fps)

    return VideoAnalysis(
        fps=analysis_fps,
        duration=asset.duration,
        width=stream.width,
        height=stream.height,
        luma=luma.astype(np.float32),
        contrast=contrast.astype(np.float32),
        sharpness=_normalise(sharpness).astype(np.float32),
        edges=_normalise(edges).astype(np.float32),
        motion=motion,
        pan=pan,
        tilt=tilt,
        zoom=zoom,
        subject=subject,
        shadows=shadows.astype(np.float32),
        highlights=highlights.astype(np.float32),
        mean_rgb=mean_rgb,
        saturation=saturation,
        warmth=warmth,
        palette=palette,
        shot_boundaries=_detect_shots(stream.frames, motion, analysis_fps),
    )


def _pad_like(small: np.ndarray, reference: np.ndarray) -> np.ndarray:
    """Pad a 'valid'-convolution result back to the reference frame size."""
    if small.shape[1:] == reference.shape[1:]:
        return small
    return np.pad(small, ((0, 0), (1, 1), (1, 1)), mode="edge")


def _analyse_still(asset: MediaAsset, analysis_fps: float, width: int) -> VideoAnalysis:
    """A still has no motion, but it still has exposure, detail and a subject."""
    stream = ffmpeg.read_frames(asset.path, width=width, fps=1.0, max_frames=1, still=True)
    duration = asset.duration
    frame_count = max(1, int(duration * analysis_fps))

    if len(stream) == 0:
        return VideoAnalysis(fps=analysis_fps, duration=duration, width=0, height=0)

    frame = stream.frames[:1].astype(np.float32) / 255.0
    laplacian = _convolve3(frame, _LAPLACIAN)
    energy = np.abs(_pad_like(laplacian, frame)) + 0.05
    anchor = _subject_track(frame, energy)[0] if len(frame) else np.array([0.5, 0.5], np.float32)

    def constant(value: float) -> np.ndarray:
        return np.full(frame_count, value, np.float32)

    mean_rgb, saturation, warmth, palette = _colour_profile(asset, 1.0)
    return VideoAnalysis(
        fps=analysis_fps,
        duration=duration,
        width=stream.width,
        height=stream.height,
        luma=constant(float(frame.mean())),
        contrast=constant(float(frame.std())),
        sharpness=constant(0.7),
        edges=constant(float(np.abs(laplacian).mean())),
        motion=np.zeros(frame_count, np.float32),
        pan=np.zeros(frame_count, np.float32),
        tilt=np.zeros(frame_count, np.float32),
        zoom=np.zeros(frame_count, np.float32),
        subject=np.tile(anchor, (frame_count, 1)).astype(np.float32),
        shadows=constant(float((frame < 0.04).mean())),
        highlights=constant(float((frame > 0.96).mean())),
        mean_rgb=mean_rgb,
        saturation=saturation,
        warmth=warmth,
        palette=palette,
        shot_boundaries=[],
    )
