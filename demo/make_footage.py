"""Generate a bin of synthetic rushes, so the agent can be demonstrated and
tested without shipping any footage.

The clips deliberately disagree with each other — different orientations,
frame rates, exposures, colour casts and amounts of movement — because that is
the situation the agent exists to solve. The music track is synthesised with a
real 4/4 pulse so the beat detector has something honest to find.

    python demo/make_footage.py /tmp/rushes
"""

from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auteur import ffmpeg  # noqa: E402

SAMPLE_RATE = 44100
BPM = 120.0


def _run(args: list[str]) -> None:
    subprocess.run(
        [
            str(ffmpeg.ffmpeg_path()),
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            *args,
        ],
        check=True,
    )


CLIPS: list[dict] = [
    {
        "name": "01_city_drift.mp4",
        "source": "mandelbrot=size=1280x720:rate=30:maxiter=180",
        "duration": 7.0,
        "grade": "eq=saturation=1.2:contrast=1.1",
    },
    {
        "name": "02_vertical_lights.mp4",
        "source": "testsrc2=size=720x1280:rate=30",
        "duration": 6.0,
        "grade": "hue=h=210:s=1.4,gblur=sigma=1.2",
    },
    {
        "name": "03_slow_texture.mp4",
        "source": "life=size=640x480:rate=24:mold=10:ratio=0.12:death_color=#203040:life_color=#e0c090",
        "duration": 8.0,
        "grade": "scale=1280:960,eq=brightness=-0.06:contrast=1.25",
    },
    {
        "name": "04_wide_gradient.mp4",
        "source": "gradients=size=1920x1080:rate=30:speed=0.06:n=4",
        "duration": 5.5,
        "grade": "eq=saturation=0.75:brightness=0.05",
    },
    {
        "name": "05_underexposed_handheld.mp4",
        "source": "mandelbrot=size=960x540:rate=25:start_scale=2.2",
        "duration": 6.5,
        # Deliberately dark and cold, to give shot matching something to fix.
        "grade": "eq=brightness=-0.18:contrast=0.9:saturation=0.7,colorbalance=bs=0.15:bm=0.08",
    },
    {
        "name": "06_bright_burst.mp4",
        "source": "cellauto=size=800x600:rate=30:rule=110:scroll=1",
        "duration": 4.5,
        "grade": "scale=1200:900,eq=brightness=0.14:saturation=1.35,hue=h=30",
    },
]


def make_clips(directory: Path) -> list[Path]:
    written: list[Path] = []
    for spec in CLIPS:
        destination = directory / spec["name"]
        _run(
            [
                "-f",
                "lavfi",
                "-i",
                spec["source"],
                "-t",
                f"{spec['duration']}",
                "-vf",
                f"{spec['grade']},format=yuv420p",
                "-c:v",
                "libx264",
                "-crf",
                "23",
                "-preset",
                "veryfast",
                str(destination),
            ]
        )
        written.append(destination)
        print(f"  clip  {destination.name}")
    return written


def make_still(directory: Path) -> Path:
    destination = directory / "07_still_frame.png"
    _run(
        [
            "-f",
            "lavfi",
            "-i",
            "mandelbrot=size=1920x1080:rate=1:start_scale=3.5",
            "-frames:v",
            "1",
            "-vf",
            "eq=saturation=1.3:contrast=1.15",
            str(destination),
        ]
    )
    print(f"  still {destination.name}")
    return destination


def _kick(length: int) -> np.ndarray:
    t = np.arange(length) / SAMPLE_RATE
    pitch = 48.0 + 90.0 * np.exp(-t * 30.0)
    return np.sin(2 * np.pi * np.cumsum(pitch) / SAMPLE_RATE) * np.exp(-t * 11.0)


def _hat(length: int) -> np.ndarray:
    rng = np.random.default_rng(5)
    t = np.arange(length) / SAMPLE_RATE
    return rng.standard_normal(length) * np.exp(-t * 140.0) * 0.35


def _bass(length: int, frequency: float) -> np.ndarray:
    t = np.arange(length) / SAMPLE_RATE
    tone = (
        np.sign(np.sin(2 * np.pi * frequency * t)) * 0.22
        + np.sin(2 * np.pi * frequency * t) * 0.3
    )
    envelope = np.minimum(1.0, t * 60) * np.exp(-t * 3.0)
    return tone * envelope


def make_music(directory: Path, *, duration: float = 48.0) -> Path:
    """A 120 BPM loop with a clear downbeat, so beat detection has real work."""
    destination = directory / "music_120bpm.wav"
    total = int(duration * SAMPLE_RATE)
    track = np.zeros(total, dtype=np.float32)

    beat = 60.0 / BPM
    beat_samples = int(beat * SAMPLE_RATE)
    notes = [55.0, 55.0, 73.42, 65.41]  # A1 A1 D2 C2 — a four-bar turnaround

    index = 0
    position = 0
    while position < total:
        in_bar = index % 4
        length = min(beat_samples, total - position)
        if length <= 0:
            break

        # Kick on 1 and 3, accented on the downbeat so downbeat detection works.
        if in_bar in (0, 2):
            hit = _kick(min(int(0.4 * SAMPLE_RATE), total - position))
            gain = 1.0 if in_bar == 0 else 0.72
            track[position : position + len(hit)] += hit[: len(track) - position] * gain

        hat = _hat(min(int(0.06 * SAMPLE_RATE), total - position))
        track[position : position + len(hat)] += hat[: len(track) - position]

        offbeat = position + beat_samples // 2
        if offbeat < total:
            hat2 = _hat(min(int(0.05 * SAMPLE_RATE), total - offbeat))
            track[offbeat : offbeat + len(hat2)] += hat2[: len(track) - offbeat] * 0.6

        if in_bar == 0:
            note = notes[(index // 4) % len(notes)]
            bass = _bass(min(int(beat * 3.6 * SAMPLE_RATE), total - position), note)
            track[position : position + len(bass)] += (
                bass[: len(track) - position] * 0.5
            )

        position += beat_samples
        index += 1

    # A slow filter sweep so the track has an arc worth starting the film on.
    envelope = 0.55 + 0.45 * np.sin(np.linspace(0, np.pi * 2.5, total)) ** 2
    track *= envelope.astype(np.float32)

    track = np.tanh(track * 1.3)
    track /= max(float(np.abs(track).max()), 1e-6)
    track *= 0.89

    with wave.open(str(destination), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes((track * 32767).astype("<i2").tobytes())

    print(f"  music {destination.name} ({BPM:.0f} BPM, {duration:.0f}s)")
    return destination


def main() -> int:
    directory = Path(sys.argv[1] if len(sys.argv) > 1 else "demo/rushes")
    directory.mkdir(parents=True, exist_ok=True)
    print(f"generating demo rushes in {directory}")
    make_clips(directory)
    make_still(directory)
    make_music(directory)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
