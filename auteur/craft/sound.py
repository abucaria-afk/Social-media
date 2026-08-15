"""Sound design: the half of film nobody credits and everybody feels.

Effects here are synthesised from scratch rather than sampled — there is no
library to ship, no licence to clear, and the result can be tuned to the cut
(a whoosh whose sweep matches the length of the whip it sits under).

The music chain is the other half: level, ducking under dialogue, and the
loudness normalisation that stops the film being the loudest thing in someone's
feed.
"""

from __future__ import annotations

import logging
import math
import wave
from pathlib import Path

import numpy as np

from ..edl import MusicCue, SoundCue
from ..ffmpeg import chain

log = logging.getLogger("auteur.craft.sound")

SAMPLE_RATE = 48000
#: Platform target. Instagram, TikTok and YouTube all normalise to about here.
TARGET_LUFS = -14.0


def _svf_bandpass(
    signal: np.ndarray, cutoff: np.ndarray, resonance: float, sample_rate: int
) -> np.ndarray:
    """State-variable filter with a per-sample cutoff.

    A sweeping resonant band is what separates a whoosh from a hiss, and it
    needs the cutoff to move sample by sample rather than in blocks.
    """
    q = 1.0 / max(resonance, 0.35)
    f = 2.0 * np.sin(np.pi * np.clip(cutoff, 20.0, sample_rate * 0.45) / sample_rate)

    low = band = 0.0
    out = np.empty_like(signal)
    for index in range(len(signal)):
        high = signal[index] - low - q * band
        band += f[index] * high
        low += f[index] * band
        out[index] = band
    return out


def _envelope(
    length: int, attack: float, decay: float, sample_rate: int, curve: float = 2.0
) -> np.ndarray:
    attack_samples = max(1, int(attack * sample_rate))
    decay_samples = max(1, length - attack_samples)
    rise = np.linspace(0.0, 1.0, attack_samples) ** 0.6
    fall = np.linspace(1.0, 0.0, decay_samples) ** curve
    return np.concatenate([rise, fall])[:length]


def _whoosh(duration: float, sample_rate: int) -> np.ndarray:
    """Filtered noise on a rising-then-falling sweep. Sits under a whip pan."""
    length = int(duration * sample_rate)
    rng = np.random.default_rng(11)
    noise = rng.standard_normal(length).astype(np.float32)

    t = np.linspace(0.0, 1.0, length, dtype=np.float32)
    # Clip before the fractional power: in float32, sin(pi) lands just below
    # zero, and a negative base to a fractional exponent is NaN.
    sweep = np.clip(np.sin(t * math.pi), 0.0, None)
    cutoff = 320.0 + 4200.0 * sweep**1.4
    body = _svf_bandpass(noise, cutoff, resonance=1.9, sample_rate=sample_rate)
    return body * _envelope(length, 0.12 * duration, duration, sample_rate, curve=1.4) * 0.9


def _impact(duration: float, sample_rate: int) -> np.ndarray:
    """Click, body, tail. The sound of a cut landing on a downbeat."""
    length = int(duration * sample_rate)
    t = np.arange(length, dtype=np.float32) / sample_rate
    rng = np.random.default_rng(23)

    # Pitch-dropping body: 110 Hz falling to 45 Hz over the first 120 ms.
    pitch = 45.0 + 65.0 * np.exp(-t * 26.0)
    phase = 2 * np.pi * np.cumsum(pitch) / sample_rate
    body = np.sin(phase) * np.exp(-t * 9.0)

    transient = rng.standard_normal(length).astype(np.float32) * np.exp(-t * 220.0)
    transient = _svf_bandpass(transient, np.full(length, 2600.0, np.float32), 1.2, sample_rate)

    return (body * 0.85 + transient * 0.35).astype(np.float32)


def _sub_drop(duration: float, sample_rate: int) -> np.ndarray:
    """A sine falling into the sub. The sound of the bottom dropping out."""
    length = int(duration * sample_rate)
    t = np.arange(length, dtype=np.float32) / sample_rate
    pitch = 30.0 + 75.0 * np.exp(-t * 3.2)
    phase = 2 * np.pi * np.cumsum(pitch) / sample_rate
    return (np.sin(phase) * np.exp(-t * 1.9)).astype(np.float32)


def _riser(duration: float, sample_rate: int) -> np.ndarray:
    """Noise and pitch climbing together — tension, ending exactly on the hit."""
    length = int(duration * sample_rate)
    t = np.linspace(0.0, 1.0, length, dtype=np.float32)
    rng = np.random.default_rng(37)

    noise = rng.standard_normal(length).astype(np.float32)
    cutoff = 400.0 * np.exp(t * 2.6)
    air = _svf_bandpass(noise, cutoff, resonance=1.3, sample_rate=sample_rate)

    pitch = 180.0 * np.exp(t * 1.5)
    tone = np.sin(2 * np.pi * np.cumsum(pitch) / sample_rate) * 0.35

    return ((air * 0.8 + tone) * (t**2.2)).astype(np.float32)


def _tick(duration: float, sample_rate: int) -> np.ndarray:
    """A short, bright transient for glitch cuts."""
    length = int(duration * sample_rate)
    t = np.arange(length, dtype=np.float32) / sample_rate
    rng = np.random.default_rng(53)
    noise = rng.standard_normal(length).astype(np.float32)
    shaped = _svf_bandpass(noise, np.full(length, 5200.0, np.float32), 2.4, sample_rate)
    return (shaped * np.exp(-t * 90.0)).astype(np.float32)


_SYNTHS = {
    "whoosh": _whoosh,
    "impact": _impact,
    "sub-drop": _sub_drop,
    "riser": _riser,
    "tick": _tick,
}


def synthesise(kind: str, duration: float, *, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Render one effect to mono float samples in [-1, 1]."""
    synth = _SYNTHS.get(kind, _whoosh)
    duration = float(min(max(duration, 0.05), 6.0))
    samples = synth(duration, sample_rate)

    # Soft-clip rather than hard-limit: distortion on a whoosh is a giveaway.
    samples = np.tanh(samples * 1.15)
    peak = float(np.abs(samples).max())
    if peak > 0:
        samples = samples / peak * 0.92

    # Short fades at both ends so nothing clicks when it is mixed in.
    edge = max(1, int(0.004 * sample_rate))
    samples[:edge] *= np.linspace(0.0, 1.0, edge)
    samples[-edge:] *= np.linspace(1.0, 0.0, edge)
    return samples.astype(np.float32)


def write_wav(path: Path, samples: np.ndarray, *, sample_rate: int = SAMPLE_RATE) -> Path:
    """Write mono float samples as 16-bit PCM."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(samples, -1.0, 1.0)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes((pcm * 32767.0).astype("<i2").tobytes())
    return path


def render_effects(
    cues: list[SoundCue], directory: Path, *, sample_rate: int = SAMPLE_RATE
) -> dict[str, Path]:
    """Render every distinct (kind, duration) once and reuse it across the film."""
    directory.mkdir(parents=True, exist_ok=True)
    rendered: dict[str, Path] = {}
    for cue in cues:
        key = f"{cue.kind}-{cue.duration:.2f}"
        if key in rendered:
            continue
        path = directory / f"sfx-{key.replace('.', '_')}.wav"
        if not path.exists():
            write_wav(
                path,
                synthesise(cue.kind, cue.duration, sample_rate=sample_rate),
                sample_rate=sample_rate,
            )
        rendered[key] = path
    return rendered


def effect_key(cue: SoundCue) -> str:
    return f"{cue.kind}-{cue.duration:.2f}"


# ---------------------------------------------------------------------------
# The mix
# ---------------------------------------------------------------------------


def music_chain(cue: MusicCue, duration: float, *, sample_rate: int = SAMPLE_RATE) -> str:
    """Trim, level and fade the bed to exactly the length of the film.

    Looping a short track is handled by ``-stream_loop`` on the input rather
    than by the ``aloop`` filter, which would have to buffer the whole track.
    """
    fade_out_start = max(0.0, duration - cue.fade_out)
    return chain(
        f"atrim=start={max(cue.offset, 0.0):.3f}",
        "asetpts=PTS-STARTPTS",
        f"atrim=duration={duration:.3f}",
        "asetpts=PTS-STARTPTS",
        f"volume={max(cue.gain, 0.0):.3f}",
        f"afade=t=in:st=0:d={max(cue.fade_in, 0.01):.3f}",
        f"afade=t=out:st={fade_out_start:.3f}:d={max(cue.fade_out, 0.01):.3f}",
        f"aformat=sample_fmts=fltp:sample_rates={sample_rate}:channel_layouts=stereo",
    )


def duck_graph(music_label: str, voice_label: str, out_label: str, amount: float) -> str:
    """Sidechain the music off the dialogue.

    Ducking by level rather than by hand is the difference between a mix where
    you can hear what is said and one where you cannot.
    """
    ratio = 2.0 + 14.0 * min(max(amount, 0.0), 1.0)
    return (
        f"[{music_label}][{voice_label}]sidechaincompress="
        f"threshold=0.05:ratio={ratio:.2f}:attack=12:release=340:makeup=1:detection=rms[{out_label}]"
    )


def master_chain(*, sample_rate: int = SAMPLE_RATE, target_lufs: float = TARGET_LUFS) -> str:
    """Final bus: normalise loudness, then catch anything still over the ceiling."""
    return chain(
        f"loudnorm=I={target_lufs:.1f}:TP=-1.5:LRA=11",
        f"aformat=sample_fmts=fltp:sample_rates={sample_rate}:channel_layouts=stereo",
        "alimiter=limit=0.97:level=disabled",
    )


def silence_chain(duration: float, *, sample_rate: int = SAMPLE_RATE) -> str:
    """A silent bed, for shots with no sound of their own."""
    return f"anullsrc=channel_layout=stereo:sample_rate={sample_rate}:d={max(duration, 0.05):.3f}"
