"""Listening: loudness, onsets, tempo, and the beat grid every cut snaps to.

Cutting to picture alone gets you a slideshow. The single biggest difference
between an edit that feels professional and one that does not is that the cuts
land on the music. So the agent derives a real beat grid — tempo *and* phase —
and treats it as the timeline's ruler.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from .. import ffmpeg
from ..ingest import MediaAsset

log = logging.getLogger("auteur.analysis.audio")

SAMPLE_RATE = 22050
HOP = 512
WINDOW = 2048
#: Frames per second of the onset/energy envelopes.
ENVELOPE_FPS = SAMPLE_RATE / HOP  # ≈43 Hz


@dataclass
class AudioAnalysis:
    duration: float
    #: True when the track is effectively silent — a clip with a dead mic.
    silent: bool = True
    envelope: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0, np.float32))
    onset: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0, np.float32))
    envelope_fps: float = ENVELOPE_FPS

    rms: float = 0.0
    peak: float = 0.0
    #: Integrated loudness proxy, dBFS.
    loudness: float = -70.0

    tempo: float = 0.0  # BPM, 0 when nothing periodic was found
    tempo_confidence: float = 0.0
    beats: list[float] = field(default_factory=list)
    downbeats: list[float] = field(default_factory=list)

    #: Loud transients — impacts, hits, door slams. Good places to cut.
    accents: list[float] = field(default_factory=list)
    #: Ranges of near-silence, as (start, end).
    silences: list[tuple[float, float]] = field(default_factory=list)
    #: 0..1 likelihood the track carries speech rather than music.
    speechiness: float = 0.0

    @property
    def has_beat(self) -> bool:
        return len(self.beats) >= 4 and self.tempo_confidence > 0.12

    def energy_at(self, seconds: float) -> float:
        if not len(self.envelope):
            return 0.0
        index = int(np.clip(seconds * self.envelope_fps, 0, len(self.envelope) - 1))
        return float(self.envelope[index])

    def energy_over(self, start: float, end: float) -> float:
        if not len(self.envelope):
            return 0.0
        i0 = int(np.clip(start * self.envelope_fps, 0, len(self.envelope) - 1))
        i1 = int(np.clip(end * self.envelope_fps, i0 + 1, len(self.envelope)))
        return float(self.envelope[i0:i1].mean())

    def snap(self, seconds: float, *, strong: bool = False, tolerance: float = 0.28) -> float:
        """Move a time to the nearest beat, if one is close enough to be felt."""
        grid = self.downbeats if (strong and self.downbeats) else self.beats
        if not grid:
            return seconds
        array = np.asarray(grid, dtype=np.float64)
        nearest = array[int(np.abs(array - seconds).argmin())]
        return float(nearest) if abs(nearest - seconds) <= tolerance else float(seconds)


def _stft_magnitude(samples: np.ndarray) -> np.ndarray:
    """Short-time Fourier magnitudes. Rows are frames, columns are bins."""
    if len(samples) < WINDOW:
        samples = np.pad(samples, (0, WINDOW - len(samples)))
    frame_count = 1 + (len(samples) - WINDOW) // HOP
    if frame_count < 1:
        return np.zeros((0, WINDOW // 2 + 1), np.float32)

    indices = np.arange(WINDOW)[None, :] + HOP * np.arange(frame_count)[:, None]
    frames = samples[indices] * np.hanning(WINDOW).astype(np.float32)
    return np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)


def _onset_envelope(magnitude: np.ndarray) -> np.ndarray:
    """Spectral flux: how much new energy appeared since the previous frame."""
    if len(magnitude) < 2:
        return np.zeros(len(magnitude), np.float32)
    # Log compression keeps quiet passages legible next to loud ones.
    compressed = np.log1p(magnitude * 8.0)
    flux = np.diff(compressed, axis=0).clip(min=0).sum(axis=1)
    flux = np.concatenate([[0.0], flux]).astype(np.float32)

    # Subtract a local median so a loud section does not swamp a quiet one.
    window = 21
    padded = np.pad(flux, (window // 2, window // 2), mode="edge")
    baseline = np.array(
        [np.median(padded[i : i + window]) for i in range(len(flux))], dtype=np.float32
    )
    flux = (flux - baseline).clip(min=0)
    peak = float(flux.max())
    return (flux / peak).astype(np.float32) if peak > 0 else flux


def _estimate_tempo(onset: np.ndarray, fps: float) -> tuple[float, float, float]:
    """Tempo, phase and confidence, by autocorrelating the onset envelope.

    Returns (bpm, first_beat_seconds, confidence).
    """
    if len(onset) < int(fps * 4):
        return 0.0, 0.0, 0.0

    signal = onset - onset.mean()
    spectrum = np.fft.rfft(signal, n=2 * len(signal))
    autocorr = np.fft.irfft(spectrum * np.conj(spectrum))[: len(signal)].astype(np.float32)
    if autocorr[0] <= 0:
        return 0.0, 0.0, 0.0
    autocorr /= autocorr[0]

    # 60–190 BPM is where nearly all cuttable music lives.
    min_lag = max(1, int(fps * 60.0 / 190.0))
    max_lag = min(len(autocorr) - 1, int(fps * 60.0 / 60.0))
    if max_lag <= min_lag:
        return 0.0, 0.0, 0.0

    window = autocorr[min_lag : max_lag + 1]
    lag = int(window.argmax()) + min_lag
    confidence = float(window.max())
    if lag <= 0 or confidence <= 0:
        return 0.0, 0.0, 0.0

    period = lag / fps
    bpm = 60.0 / period
    # Fold into a musically sane range; 75 BPM and 150 BPM autocorrelate alike.
    while bpm < 70:
        bpm *= 2
    while bpm > 180:
        bpm /= 2
    period = 60.0 / bpm

    phase = _estimate_phase(onset, fps, period)
    return float(bpm), float(phase), float(np.clip(confidence, 0.0, 1.0))


def _estimate_phase(onset: np.ndarray, fps: float, period: float) -> float:
    """Slide a pulse train across the onset envelope; keep the best alignment."""
    lag = period * fps
    if lag < 2:
        return 0.0
    offsets = np.linspace(0.0, lag, num=min(48, max(8, int(lag))), endpoint=False)
    positions = np.arange(0, len(onset), lag)

    best_offset, best_score = 0.0, -1.0
    for offset in offsets:
        indices = np.round(positions + offset).astype(int)
        indices = indices[indices < len(onset)]
        if not len(indices):
            continue
        score = float(onset[indices].mean())
        if score > best_score:
            best_offset, best_score = float(offset), score
    return best_offset / fps


def _pick_downbeats(beats: list[float], onset: np.ndarray, fps: float) -> list[float]:
    """Assume 4/4 and choose the bar phase whose beats hit hardest."""
    if len(beats) < 4:
        return list(beats)
    best_phase, best_score = 0, -1.0
    for phase in range(4):
        candidates = beats[phase::4]
        indices = np.clip((np.asarray(candidates) * fps).astype(int), 0, len(onset) - 1)
        score = float(onset[indices].mean()) if len(indices) else 0.0
        if score > best_score:
            best_phase, best_score = phase, score
    return beats[best_phase::4]


def _find_accents(onset: np.ndarray, fps: float) -> list[float]:
    """Isolated loud transients — the moments an editor instinctively cuts on."""
    if len(onset) < 3:
        return []
    threshold = float(np.percentile(onset, 96))
    if threshold <= 0.05:
        return []

    accents: list[float] = []
    last = -1e9
    for index in range(1, len(onset) - 1):
        value = onset[index]
        if value >= threshold and value >= onset[index - 1] and value >= onset[index + 1]:
            time = index / fps
            if time - last > 0.2:
                accents.append(round(time, 3))
                last = time
    return accents


def _find_silences(envelope: np.ndarray, fps: float, floor: float = 0.02) -> list[tuple[float, float]]:
    if not len(envelope):
        return []
    quiet = envelope < floor
    silences: list[tuple[float, float]] = []
    start: int | None = None
    for index, is_quiet in enumerate(quiet):
        if is_quiet and start is None:
            start = index
        elif not is_quiet and start is not None:
            if (index - start) / fps >= 0.4:
                silences.append((round(start / fps, 3), round(index / fps, 3)))
            start = None
    if start is not None and (len(quiet) - start) / fps >= 0.4:
        silences.append((round(start / fps, 3), round(len(quiet) / fps, 3)))
    return silences


def _speechiness(magnitude: np.ndarray, envelope: np.ndarray) -> float:
    """Rough voice detector: vocal-band dominance plus syllable-rate modulation."""
    if not len(magnitude) or not len(envelope):
        return 0.0

    freqs = np.fft.rfftfreq(WINDOW, 1.0 / SAMPLE_RATE)
    voice_band = (freqs >= 300) & (freqs <= 3400)
    total = magnitude.sum(axis=1).clip(1e-6)
    band_ratio = float(np.mean(magnitude[:, voice_band].sum(axis=1) / total))

    # Speech modulates its own loudness at roughly 4 Hz — the syllable rate.
    centred = envelope - envelope.mean()
    if len(centred) > 32:
        spectrum = np.abs(np.fft.rfft(centred))
        mod_freqs = np.fft.rfftfreq(len(centred), 1.0 / ENVELOPE_FPS)
        syllabic = spectrum[(mod_freqs > 2.0) & (mod_freqs < 8.0)].sum()
        modulation = float(syllabic / spectrum[1:].sum().clip(1e-6))
    else:
        modulation = 0.0

    # Both cues have to agree. Percussion alone puts plenty of energy in the
    # vocal band, and plenty of music modulates near the syllable rate, so
    # either signal on its own produces false positives on instrumentals.
    return float(np.clip(band_ratio * modulation * 3.2, 0.0, 1.0))


def analyse_audio(asset: MediaAsset) -> AudioAnalysis:
    """Listen to a track end to end and derive its rhythmic structure."""
    samples = ffmpeg.read_audio(asset.path, sample_rate=SAMPLE_RATE)
    duration = asset.duration

    if samples.size < SAMPLE_RATE // 8:
        return AudioAnalysis(duration=duration, silent=True)

    peak = float(np.abs(samples).max())
    rms = float(np.sqrt(np.mean(samples**2)))
    if peak < 1e-3 or rms < 1e-4:
        return AudioAnalysis(duration=duration, silent=True, peak=peak, rms=rms)

    magnitude = _stft_magnitude(samples)
    if not len(magnitude):
        return AudioAnalysis(duration=duration, silent=True, peak=peak, rms=rms)

    frame_rms = np.sqrt((samples[: len(magnitude) * HOP].reshape(-1, HOP) ** 2).mean(axis=1))
    envelope = frame_rms.astype(np.float32)
    envelope = envelope / max(float(envelope.max()), 1e-6)

    onset = _onset_envelope(magnitude)
    size = min(len(envelope), len(onset))
    envelope, onset = envelope[:size], onset[:size]

    bpm, phase, confidence = _estimate_tempo(onset, ENVELOPE_FPS)
    beats: list[float] = []
    if bpm > 0:
        period = 60.0 / bpm
        count = int((duration - phase) / period) + 1
        beats = [round(phase + i * period, 4) for i in range(max(count, 0)) if phase + i * period <= duration]

    return AudioAnalysis(
        duration=duration,
        silent=False,
        envelope=envelope,
        onset=onset,
        rms=rms,
        peak=peak,
        loudness=float(20 * np.log10(max(rms, 1e-7))),
        tempo=bpm,
        tempo_confidence=confidence,
        beats=beats,
        downbeats=_pick_downbeats(beats, onset, ENVELOPE_FPS),
        accents=_find_accents(onset, ENVELOPE_FPS),
        silences=_find_silences(envelope, ENVELOPE_FPS),
        speechiness=_speechiness(magnitude, envelope),
    )


def find_music_bed(candidates: list[MediaAsset]) -> tuple[MediaAsset | None, AudioAnalysis | None]:
    """Choose the track to cut to: the longest, most rhythmic, least speechy one."""
    best: tuple[float, MediaAsset, AudioAnalysis] | None = None
    for asset in candidates:
        analysis = analyse_audio(asset)
        if analysis.silent:
            continue
        score = (
            analysis.tempo_confidence * 3.0
            + min(analysis.duration / 30.0, 1.5)
            - analysis.speechiness * 2.0
        )
        if best is None or score > best[0]:
            best = (score, asset, analysis)

    if best is None:
        return None, None
    log.info(
        "music bed: %s (%.0f BPM, confidence %.2f)", best[1].name, best[2].tempo, best[2].tempo_confidence
    )
    return best[1], best[2]
