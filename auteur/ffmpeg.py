"""The projector and the flatbed: every call out to ffmpeg lives here.

Nothing above this module is allowed to know what a command line looks like.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

log = logging.getLogger("auteur.ffmpeg")


class FFmpegError(RuntimeError):
    """ffmpeg exited non-zero. Carries the tail of stderr, which is the useful part."""

    def __init__(self, args: Sequence[str], returncode: int, stderr: str):
        self.args_used = list(args)
        self.returncode = returncode
        self.stderr = stderr
        tail = "\n".join(stderr.strip().splitlines()[-18:])
        super().__init__(f"ffmpeg exited {returncode}\n{tail}")


class MissingBinary(RuntimeError):
    pass


def _from_package(module: str, *relative: str) -> Path | None:
    try:
        mod = __import__(module)
    except ImportError:
        return None
    base = Path(getattr(mod, "__file__", "") or "").parent
    for rel in relative:
        candidate = base / rel
        if candidate.exists():
            return candidate
    return None


@functools.lru_cache(maxsize=None)
def _locate(tool: str) -> Path:
    """Find ffmpeg/ffprobe. Prefers builds that ship the filters we depend on."""
    env = os.environ.get(f"AUTEUR_{tool.upper()}") or os.environ.get(tool.upper())
    if env and Path(env).exists():
        return Path(env)

    # Wheel-bundled static builds first: they carry libx264, drawtext, xfade,
    # minterpolate and loudnorm, which distro builds sometimes omit.
    packaged = _from_package("ffmpeg", f"binaries/{tool}")
    if packaged:
        try:
            packaged.chmod(0o755)
        except OSError:
            pass
        return packaged

    found = shutil.which(tool)
    if found:
        return Path(found)

    if tool == "ffmpeg":
        try:
            import imageio_ffmpeg

            return Path(imageio_ffmpeg.get_ffmpeg_exe())
        except Exception:  # noqa: BLE001 - optional dependency, any failure is "not here"
            pass

    raise MissingBinary(
        f"could not find {tool!r}. Install it (`apt install ffmpeg`), or "
        f"`pip install ffmpeg-binaries`, or set AUTEUR_{tool.upper()}=/path/to/{tool}"
    )


def ffmpeg_path() -> Path:
    return _locate("ffmpeg")


def ffprobe_path() -> Path:
    return _locate("ffprobe")


def run(args: Sequence[str], *, timeout: float = 3600.0, quiet: bool = True) -> str:
    """Run ffmpeg. Returns stderr (where ffmpeg puts everything interesting)."""
    cmd = [str(ffmpeg_path()), "-hide_banner", "-nostdin", "-y", *map(str, args)]
    log.debug("ffmpeg %s", " ".join(cmd[3:]))
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, errors="replace"
    )
    if proc.returncode != 0:
        raise FFmpegError(cmd, proc.returncode, proc.stderr)
    if not quiet and proc.stderr:
        log.info(proc.stderr.strip()[-2000:])
    return proc.stderr


def probe(path: str | Path) -> dict:
    """ffprobe -> dict. Raises FFmpegError if the file is not readable media."""
    cmd = [
        str(ffprobe_path()), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise FFmpegError(cmd, proc.returncode, proc.stderr)
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:  # pragma: no cover - malformed ffprobe output
        raise FFmpegError(cmd, 0, f"unparsable ffprobe output: {exc}") from exc


# --------------------------------------------------------------------------
# Raw sample access — how the agent actually "watches" and "listens to" footage
# --------------------------------------------------------------------------


@dataclass
class FrameStream:
    frames: np.ndarray  # (n, h, w) uint8 luma, or (n, h, w, 3) uint8 rgb
    fps: float
    width: int
    height: int

    def __len__(self) -> int:
        return int(self.frames.shape[0])


def read_frames(
    path: str | Path,
    *,
    width: int = 128,
    fps: float = 6.0,
    color: bool = False,
    start: float | None = None,
    duration: float | None = None,
    max_frames: int = 4000,
    still: bool = False,
) -> FrameStream:
    """Decode a clip down to a small thumbnail stream and hand back an array.

    This is deliberately tiny: 128px wide at 6fps is plenty to measure motion,
    focus, exposure and colour, and it keeps a feature-length ingest in RAM.

    Pass ``still=True`` for single images: the fps filter discards a lone frame
    that does not span a full output period, so stills need it left out.
    """
    pix_fmt = "rgb24" if color else "gray"
    channels = 3 if color else 1

    args: list[str] = []
    if start is not None and not still:
        args += ["-ss", f"{max(0.0, start):.3f}"]
    args += ["-i", str(path)]
    if duration is not None and not still:
        args += ["-t", f"{max(0.01, duration):.3f}"]

    # Pin the height explicitly. Letting ffmpeg choose it (scale=-2) means
    # guessing it back from the byte count, and that guess is ambiguous —
    # several plausible heights divide the payload exactly, so a 1080x1920
    # stream can be read back as a 128x32 one with no error anywhere.
    height = scaled_height(path, width)
    scale = f"scale={width}:{height}:flags=bilinear"
    video_filter = scale if still else f"fps={fps},{scale}"
    args += [
        "-vf", video_filter,
        "-frames:v", "1" if still else str(max_frames),
        "-pix_fmt", pix_fmt, "-f", "rawvideo", "-",
    ]

    cmd = [str(ffmpeg_path()), "-hide_banner", "-nostdin", "-loglevel", "error", *args]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 and not proc.stdout:
        raise FFmpegError(cmd, proc.returncode, proc.stderr.decode("utf-8", "replace"))

    raw = np.frombuffer(proc.stdout, dtype=np.uint8)
    per_frame = width * height * channels
    if raw.size < per_frame:
        empty = np.zeros((0, height, width) + ((3,) if color else ()), np.uint8)
        return FrameStream(empty, fps, width, height)

    count = raw.size // per_frame
    shape = (count, height, width) + ((3,) if color else ())
    frames = raw[: count * per_frame].reshape(shape)
    return FrameStream(frames, fps, width, height)


@functools.lru_cache(maxsize=256)
def scaled_height(path: str | Path, width: int) -> int:
    """Even height that preserves a source's displayed aspect ratio at `width`.

    Rotation metadata counts: a phone clip stored 1920x1080 with a 90° flag is
    1080x1920 to everyone who watches it, and analysing it sideways would put
    the subject track on the wrong axis.
    """
    try:
        info = probe(path)
    except FFmpegError:
        return max(2, (width * 9 // 16) // 2 * 2)

    stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not stream:
        return max(2, (width * 9 // 16) // 2 * 2)

    source_w = int(stream.get("width") or 0)
    source_h = int(stream.get("height") or 0)
    if source_w <= 0 or source_h <= 0:
        return max(2, (width * 9 // 16) // 2 * 2)

    rotation = 0
    tags = stream.get("tags") or {}
    for key in ("rotate", "Rotate"):
        if key in tags:
            try:
                rotation = int(float(tags[key])) % 360
            except (TypeError, ValueError):
                rotation = 0
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            try:
                rotation = int(round(-float(side["rotation"]))) % 360
            except (TypeError, ValueError):
                pass
    if rotation in (90, 270):
        source_w, source_h = source_h, source_w

    return max(2, int(round(width * source_h / source_w)) // 2 * 2)


def read_audio(
    path: str | Path,
    *,
    sample_rate: int = 22050,
    start: float | None = None,
    duration: float | None = None,
) -> np.ndarray:
    """Decode audio to mono float32 in [-1, 1]. Returns an empty array if silent."""
    args: list[str] = []
    if start is not None:
        args += ["-ss", f"{max(0.0, start):.3f}"]
    args += ["-i", str(path)]
    if duration is not None:
        args += ["-t", f"{max(0.01, duration):.3f}"]
    args += ["-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-acodec", "pcm_s16le", "-"]

    cmd = [str(ffmpeg_path()), "-hide_banner", "-nostdin", "-loglevel", "error", *args]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 and not proc.stdout:
        return np.zeros(0, dtype=np.float32)
    pcm = np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32) / 32768.0
    return np.ascontiguousarray(pcm)


def has_audio(info: dict) -> bool:
    return any(s.get("codec_type") == "audio" for s in info.get("streams", []))


# --------------------------------------------------------------------------
# Filter-graph text helpers
# --------------------------------------------------------------------------

def escape_text(text: str) -> str:
    """Escape a string for use inside a drawtext= filter argument."""
    out = text.replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")
    return out.replace("%", r"\%").replace(",", r"\,").replace("[", r"\[").replace("]", r"\]")


def escape_path(path: str | Path) -> str:
    """Escape a filesystem path for use inside a filter argument (movie=, lut3d=...)."""
    return str(path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def chain(*links: str) -> str:
    """Join filter links, dropping the empty ones so callers can use conditionals."""
    return ",".join(link for link in links if link)


def graph(*chains: str) -> str:
    """Join filtergraph chains with ';', dropping empties."""
    return ";".join(c for c in chains if c)
