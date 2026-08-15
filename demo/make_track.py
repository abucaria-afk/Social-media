"""A music bed, synthesised, in a named style.

    python demo/make_track.py ./out --style boom-bap
    python demo/make_track.py ./out --style boom-bap --bpm 86 --seconds 40

**Why this exists instead of a download.** The obvious way to score a montage
is to use the song everybody is using. This project cannot do that: the songs
that are actually going viral are copyrighted, and a tool that fetches one and
bakes it into a file you are about to publish is handing you a copyright strike
with extra steps. Platform "trending audio" is licensed *inside* the app, at the
moment you pick it there — not in a render on your laptop.

So this writes an original instrumental in the right idiom. It is a bed: it has
a real downbeat for the beat detector, a real arc for the edit to sit on, and it
sounds like the room the photographs were taken in. It is not the song.

**The intended workflow is still to use the real song.** Cut against this, then
replace the audio inside the app when you post — which is where the trending
track is licensed anyway, and where the platform gives a post distribution
weight for using it. Any audio file among the inputs is picked up as the bed, so
a track you have actually licensed drops straight in:

    auteur workflow run tiktok ./photos ./my-licensed-track.wav "crate day"
"""

from __future__ import annotations

import argparse
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 44_100


def _kick(length: int, *, punch: float = 30.0, decay: float = 11.0) -> np.ndarray:
    t = np.arange(length) / SAMPLE_RATE
    pitch = 46.0 + 95.0 * np.exp(-t * punch)
    return np.sin(2 * np.pi * np.cumsum(pitch) / SAMPLE_RATE) * np.exp(-t * decay)


def _snare(length: int, rng: np.random.Generator) -> np.ndarray:
    """Noise plus a low body. The body is what makes it sound like a record
    rather than like white noise with an envelope on it."""
    t = np.arange(length) / SAMPLE_RATE
    noise = rng.standard_normal(length) * np.exp(-t * 26.0)
    body = np.sin(2 * np.pi * 185.0 * t) * np.exp(-t * 34.0) * 0.6
    return (noise * 0.7 + body) * 0.8


def _hat(length: int, rng: np.random.Generator, decay: float = 150.0) -> np.ndarray:
    t = np.arange(length) / SAMPLE_RATE
    return rng.standard_normal(length) * np.exp(-t * decay) * 0.3


def _rhodes(length: int, frequency: float) -> np.ndarray:
    """A struck electric-piano-ish tone: a sine, a bell partial that dies fast,
    and a slow tremolo. Three oscillators is enough to stop sounding like a
    test tone and start sounding like a sample."""
    t = np.arange(length) / SAMPLE_RATE
    fundamental = np.sin(2 * np.pi * frequency * t)
    bell = np.sin(2 * np.pi * frequency * 4.0 * t) * np.exp(-t * 14.0) * 0.28
    fifth = np.sin(2 * np.pi * frequency * 1.5 * t) * 0.16
    tremolo = 1.0 + 0.09 * np.sin(2 * np.pi * 5.2 * t)
    envelope = np.minimum(1.0, t * 180.0) * np.exp(-t * 2.1)
    return (fundamental + bell + fifth) * envelope * tremolo * 0.34


def _upright(length: int, frequency: float) -> np.ndarray:
    """Bass with a bit of fret buzz on the attack."""
    t = np.arange(length) / SAMPLE_RATE
    tone = np.sin(2 * np.pi * frequency * t) * 0.55
    growl = np.sign(np.sin(2 * np.pi * frequency * t)) * 0.14 * np.exp(-t * 9.0)
    envelope = np.minimum(1.0, t * 90.0) * np.exp(-t * 2.6)
    return (tone + growl) * envelope


def _dust(total: int, rng: np.random.Generator) -> np.ndarray:
    """Vinyl noise floor and the odd crackle. Without it the loop is too clean
    to sit under photographs of a record shop."""
    floor = rng.standard_normal(total) * 0.006
    crackle = np.zeros(total, dtype=np.float64)
    for position in rng.integers(0, total, size=max(1, total // 5200)):
        span = min(int(0.004 * SAMPLE_RATE), total - position)
        if span > 0:
            t = np.arange(span) / SAMPLE_RATE
            crackle[position : position + span] += (
                rng.standard_normal(span) * np.exp(-t * 900.0) * 0.5
            )
    return floor + crackle


#: Minor-key turnarounds, four bars each. Dusty, unresolved — the chord that
#: does not go home is what makes a loop bear being heard eight times.
_PROGRESSIONS = {
    "boom-bap": [(233.08, 58.27), (207.65, 51.91), (174.61, 43.65), (196.00, 49.00)],
    "lo-fi": [(220.00, 55.00), (174.61, 43.65), (196.00, 49.00), (164.81, 41.20)],
}

STYLES = {
    "boom-bap": {
        "bpm": 88.0,
        "swing": 0.16,
        "kick_pattern": (0, 2.5, 6),
        "snare_pattern": (2, 6),
        "dust": 1.0,
        "note": "dusty, swung, unhurried — crate-digging tempo",
    },
    "lo-fi": {
        "bpm": 74.0,
        "swing": 0.12,
        "kick_pattern": (0, 4, 6),
        "snare_pattern": (2, 6),
        "dust": 1.4,
        "note": "slower, warmer, more noise floor",
    },
    "drill": {
        "bpm": 142.0,
        "swing": 0.0,
        "kick_pattern": (0, 3, 5.5, 6.5),
        "snare_pattern": (4,),
        "dust": 0.2,
        "note": "fast, sparse, hats doing the work",
    },
}


def make_track(
    destination: Path,
    *,
    style: str = "boom-bap",
    bpm: float | None = None,
    seconds: float = 40.0,
    seed: int = 7,
) -> Path:
    """Write a WAV in the requested style. Returns the path."""
    if style not in STYLES:
        raise ValueError(
            f"unknown style {style!r} (choose from {', '.join(sorted(STYLES))})"
        )
    recipe = STYLES[style]
    tempo = float(bpm or recipe["bpm"])
    rng = np.random.default_rng(seed)

    total = int(seconds * SAMPLE_RATE)
    track = np.zeros(total, dtype=np.float64)

    # An eighth-note grid, because everything below is placed on eighths.
    eighth = (60.0 / tempo) / 2.0
    eighth_samples = eighth * SAMPLE_RATE
    swing = float(recipe["swing"])
    progression = _PROGRESSIONS.get(style, _PROGRESSIONS["boom-bap"])

    def place(sound: np.ndarray, at: int, gain: float = 1.0) -> None:
        if at >= total:
            return
        span = min(len(sound), total - at)
        track[at : at + span] += sound[:span] * gain

    bar = 0
    while True:
        bar_start = bar * 8 * eighth_samples
        if bar_start >= total:
            break
        for step in range(8):
            # Swing: every second eighth lands late. This is the whole
            # difference between a drum machine and a drummer.
            offset = swing * eighth_samples if step % 2 else 0.0
            at = int(bar_start + step * eighth_samples + offset)
            if at >= total:
                break

            if float(step) in [float(s) for s in recipe["kick_pattern"]]:
                place(_kick(int(0.42 * SAMPLE_RATE)), at, 1.0 if step == 0 else 0.78)
            if step in recipe["snare_pattern"]:
                place(_snare(int(0.30 * SAMPLE_RATE), rng), at, 0.62)
            place(_hat(int(0.055 * SAMPLE_RATE), rng), at, 0.55 if step % 2 else 0.85)

        chord, root = progression[bar % len(progression)]
        place(_rhodes(int(eighth * 7.0 * SAMPLE_RATE), chord), int(bar_start), 1.0)
        place(
            _rhodes(int(eighth * 5.0 * SAMPLE_RATE), chord * 1.2), int(bar_start), 0.5
        )
        place(_upright(int(eighth * 6.5 * SAMPLE_RATE), root), int(bar_start), 0.9)
        # A pickup note into the next bar, so the loop pulls forward.
        place(
            _upright(int(eighth * 1.4 * SAMPLE_RATE), root * 1.5),
            int(bar_start + 6.5 * eighth_samples),
            0.5,
        )
        bar += 1

    track += _dust(total, rng) * float(recipe["dust"])

    # Gentle pumping against the kick, then tape-ish saturation.
    pump = 0.86 + 0.14 * np.sin(
        np.linspace(0, np.pi * 2 * (seconds * tempo / 60.0), total)
    )
    track *= pump
    track = np.tanh(track * 1.25)
    peak = float(np.abs(track).max()) or 1.0
    track = track / peak * 0.9

    destination.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destination), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes((track * 32767).astype("<i2").tobytes())
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("out", help="folder to write the track into")
    parser.add_argument("--style", default="boom-bap", choices=sorted(STYLES))
    parser.add_argument("--bpm", type=float, default=None)
    parser.add_argument("--seconds", type=float, default=40.0)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)

    recipe = STYLES[args.style]
    path = make_track(
        Path(args.out) / f"bed_{args.style}.wav",
        style=args.style,
        bpm=args.bpm,
        seconds=args.seconds,
        seed=args.seed,
    )
    print(f"  {path.name} — {args.bpm or recipe['bpm']:.0f} BPM, {recipe['note']}")
    print("  original instrumental, not a licensed track; swap yours in with --music")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
