"""The model in the director's chair.

The heuristic director has craft but no taste — it cannot know that the shot of
the door closing belongs at the end, or that the brief's "lonely" means hold on
the wide. This module hands the clip dossiers, the beat grid and the brief to
Claude and asks for an edit decision list back.

Two design choices make it safe to put a language model on the critical path:

* **Structured outputs.** The EDL schema is enforced by the API, so the reply is
  always parseable. There is no prose to strip and no JSON to repair.
* **It is never trusted.** Whatever comes back is clamped to the footage that
  actually exists (:meth:`EditDecisionList.repair`) and then run through the
  film-grammar passes. If the response is unusable — refusal, network failure,
  no readable shots — the caller falls back to the heuristic director and the
  film still gets made.

Optionally the model is also shown keyframes, so it is choosing shots from
pictures rather than from numbers.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
import os
from pathlib import Path

from .. import ffmpeg
from ..analysis.audio import AudioAnalysis
from ..analysis.dossier import ClipDossier
from ..config import Settings
from ..craft.color import LOOKS
from ..edl import (
    MOTIONS,
    TRANSITIONS,
    EditDecisionList,
    Look,
    MusicCue,
    shots_from_json,
    texts_from_json,
)
from .brief import Brief

log = logging.getLogger("auteur.director.llm")

#: Keyframes shown per clip, and across the whole request.
FRAMES_PER_CLIP = 3
MAX_FRAMES = 15
KEYFRAME_WIDTH = 512


class DirectorUnavailable(RuntimeError):
    """The model could not direct this edit. The caller should fall back."""


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def _shot_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "clip": {"type": "string", "description": "Clip id, e.g. C03"},
            "start": {"type": "number", "description": "In point in the source clip, seconds"},
            "end": {"type": "number", "description": "Out point in the source clip, seconds"},
            "speed": {
                "type": "number",
                "description": "Playback speed. 1.0 is real time, 0.5 is half speed, 2.0 is double.",
            },
            "ramp": {
                "type": "string",
                "enum": ["none", "slow-in", "accelerate", "hit"],
                "description": "Speed curve. 'hit' lands slow on the beat then whips out.",
            },
            "motion": {
                "type": "string",
                "enum": sorted(MOTIONS),
                "description": "Camera move added in post. Use 'none' when the footage already moves.",
            },
            "transition": {
                "type": "string",
                "enum": sorted(TRANSITIONS),
                "description": "How this shot begins. Prefer 'cut'.",
            },
            "transition_duration": {"type": "number", "description": "Seconds; ignored for cuts"},
            "note": {"type": "string", "description": "One short line on why this shot is here"},
        },
        "required": [
            "clip",
            "start",
            "end",
            "speed",
            "ramp",
            "motion",
            "transition",
            "transition_duration",
            "note",
        ],
        "additionalProperties": False,
    }


def _text_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "start": {"type": "number", "description": "Seconds into the finished film"},
            "duration": {"type": "number"},
            "style": {
                "type": "string",
                "enum": ["title", "kinetic", "lower-third", "caption", "end-card", "chapter"],
            },
            "per_word": {"type": "boolean", "description": "Reveal one word at a time"},
        },
        "required": ["text", "start", "duration", "style", "per_word"],
        "additionalProperties": False,
    }


def edl_schema() -> dict:
    """The JSON schema the model must fill in."""
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short title for the finished film"},
            "rationale": {
                "type": "string",
                "description": "Two or three sentences on the editorial idea behind this cut",
            },
            "look": {"type": "string", "enum": sorted(LOOKS)},
            "texture": {"type": "number", "description": "Film grain, 0 to 1"},
            "letterbox": {
                "type": "number",
                "description": "Matte height as a fraction of the frame, 0 for none, 0.11 for 2.35:1",
            },
            "shots": {"type": "array", "items": _shot_schema()},
            "texts": {"type": "array", "items": _text_schema()},
        },
        "required": ["title", "rationale", "look", "texture", "letterbox", "shots", "texts"],
        "additionalProperties": False,
    }


SYSTEM_PROMPT = """\
You are a film editor. You are given the analysed contents of a bin of footage \
and a director's brief, and you return the edit decision list for a finished \
short film.

You are choosing shots and their order — the system around you handles the \
craft: cuts are snapped to the beat grid, exposure and white balance are \
matched across shots, transitions are limited, and the whole thing is graded \
and mixed. Spend your judgement on what a machine cannot decide: which frames \
carry the idea, what order reveals it, and where the film should breathe.

How to cut:

- Open on the strongest image in the bin. The first second decides whether \
anyone sees the second one.
- Cut for rhythm. Shots get shorter as energy rises and longer where the film \
should land. A run of identically-timed shots is a slideshow.
- Never place two consecutive shots from the same clip.
- Prefer straight cuts. A dissolve or a whip has to be motivated by what is \
happening in the frames on either side of it.
- Slow motion is for beauty and impact, not for padding. Speeding up is for \
covering distance.
- Add a camera move only to a shot that is otherwise static.
- Respect the target runtime. Coming in slightly short is better than padding.
- Only place text on screen if the brief asked for it.

Choose in and out points inside the ranges the analysis marked as usable, and \
never past the end of a clip."""


# ---------------------------------------------------------------------------
# Building the request
# ---------------------------------------------------------------------------


def _keyframe(dossier: ClipDossier, at: float) -> str | None:
    """Grab one frame as a base64 JPEG, for the model to actually look at."""
    destination = (
        Path(os.environ.get("TMPDIR", "/tmp")) / f"auteur-kf-{dossier.clip_id}-{at:.2f}.jpg"
    )
    args: list[str] = []
    if dossier.asset.kind != "image":
        args += ["-ss", f"{max(at, 0.0):.3f}"]
    args += [
        "-i",
        str(dossier.asset.path),
        "-frames:v",
        "1",
        "-vf",
        f"scale={KEYFRAME_WIDTH}:-2:flags=bicubic",
        "-q:v",
        "6",
        "-f",
        "mjpeg",
        str(destination),
    ]
    try:
        ffmpeg.run(args, timeout=60)
        data = destination.read_bytes()
    except (ffmpeg.FFmpegError, OSError) as exc:
        log.debug("no keyframe for %s at %.2fs: %s", dossier.clip_id, at, exc)
        return None
    finally:
        destination.unlink(missing_ok=True)
    return base64.standard_b64encode(data).decode("ascii")


def _vision_blocks(dossiers: list[ClipDossier]) -> list[dict]:
    """Label-and-image pairs, so the model can tie a picture to a clip id."""
    blocks: list[dict] = []
    budget = MAX_FRAMES
    per_clip = max(1, min(FRAMES_PER_CLIP, MAX_FRAMES // max(len(dossiers), 1)))

    for dossier in dossiers:
        if budget <= 0:
            break
        takes = sorted(dossier.takes, key=lambda t: -t.score)[:per_clip]
        for take in takes:
            if budget <= 0:
                break
            frame = _keyframe(dossier, (take.start + take.end) / 2)
            if frame is None:
                continue
            blocks.append(
                {
                    "type": "text",
                    "text": f"{dossier.clip_id} at {(take.start + take.end) / 2:.1f}s:",
                }
            )
            blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": frame},
                }
            )
            budget -= 1
    return blocks


def _briefing(
    brief: Brief,
    dossiers: list[ClipDossier],
    settings: Settings,
    music: AudioAnalysis | None,
) -> str:
    target = brief.duration or settings.target_duration
    lines = [
        f"DIRECTOR'S BRIEF: {brief.prompt}",
        "",
        f"Target runtime: {target:.0f} seconds.",
        f"Delivery format: {settings.primary_format.label} at {settings.quality.fps}fps.",
        f"Suggested style: {brief.style}. Suggested energy shape: {brief.arc}.",
        f"Suggested average shot length: {brief.base_shot_length:.2f}s.",
    ]
    if brief.on_screen_text:
        lines.append(
            "Text the director asked to appear: "
            + "; ".join(f'"{t}"' for t in brief.on_screen_text)
        )
    else:
        lines.append("No on-screen text was requested. Return an empty texts array.")

    if music is not None and music.has_beat:
        lines.append(
            f"Music: {music.tempo:.0f} BPM. Cuts will be snapped to the beat "
            f"({60.0 / music.tempo:.2f}s per beat), so think in whole beats."
        )
    else:
        lines.append("No usable music beat grid; pace the film by feel.")

    lines += [
        "",
        "Available looks: "
        + ", ".join(f"{name} ({spec.description})" for name, spec in LOOKS.items()),
        "",
        "THE FOOTAGE (measured, not described by anyone):",
        json.dumps([dossier.to_json() for dossier in dossiers], indent=1),
        "",
        "Field guide: 'motion' is mean frame-to-frame change (0 is a locked-off "
        "still, 0.12 is vigorous); 'sharpness' and 'quality' are 0..1; 'camera' "
        "is the measured move; 'scale' is an estimate of shot size; "
        "'internal_cuts' are cuts already present in the source — never cut "
        "across one.",
        "",
        "Return the edit decision list.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------


def available(settings: Settings) -> bool:
    """True when a model director can plausibly be reached."""
    if not settings.use_llm:
        return False
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        # The SDK also resolves `ant auth login` profiles, so absence of the
        # variables is not proof; let the call itself decide.
        pass
    if importlib.util.find_spec("anthropic") is None:
        log.info("the anthropic package is not installed; using the heuristic director")
        return False
    return True


def _request(client, *, model: str, system: str, blocks: list[dict], max_tokens: int):
    """One call, with a server-side fallback when the SDK and API support it."""
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": blocks}],
        "output_config": {
            "effort": "high",
            "format": {"type": "json_schema", "schema": edl_schema()},
        },
    }
    try:
        return client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **payload
        )
    except Exception as exc:  # noqa: BLE001 - older SDK or API without fallbacks
        log.debug("server-side fallbacks unavailable (%s); using the stable endpoint", exc)
        return client.messages.create(**payload)


def _normalise_shot(raw: dict) -> dict:
    """Reshape one model shot into what the EDL parser expects."""
    shot = dict(raw)
    ramp = str(shot.get("ramp", "none")).strip().lower()
    if ramp in ("none", "", "constant"):
        # Fall through to the plain speed multiplier.
        shot.pop("ramp", None)
    shot["transition_in"] = {
        "kind": shot.pop("transition", "cut"),
        "duration": shot.pop("transition_duration", 0.0) or 0.0,
    }
    return shot


def direct(
    brief: Brief,
    dossiers: list[ClipDossier],
    settings: Settings,
    *,
    music_path: Path | None = None,
    music_analysis: AudioAnalysis | None = None,
    music_offset: float = 0.0,
    show_frames: bool = True,
) -> EditDecisionList:
    """Ask the model to cut the film. Raises DirectorUnavailable to fall back."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - normally caught by available()
        raise DirectorUnavailable("the anthropic package is not installed") from exc

    blocks: list[dict] = []
    if show_frames:
        try:
            blocks.extend(_vision_blocks(dossiers))
        except Exception as exc:  # noqa: BLE001 - never lose the edit over a thumbnail
            log.warning("could not extract keyframes: %s", exc)
    blocks.append({"type": "text", "text": _briefing(brief, dossiers, settings, music_analysis)})

    try:
        client = anthropic.Anthropic()
        response = _request(
            client, model=settings.model, system=SYSTEM_PROMPT, blocks=blocks, max_tokens=16000
        )
    except Exception as exc:  # noqa: BLE001 - auth, network, rate limit, anything
        raise DirectorUnavailable(f"could not reach the model: {exc}") from exc

    if getattr(response, "stop_reason", None) == "refusal":
        raise DirectorUnavailable("the model declined to direct this edit")

    text = next((block.text for block in response.content if block.type == "text"), "")
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise DirectorUnavailable(f"unparsable edit decision list: {exc}") from exc

    by_id = {dossier.clip_id: dossier for dossier in dossiers}
    shots = shots_from_json((_normalise_shot(raw) for raw in payload.get("shots", [])), by_id)
    if not shots:
        raise DirectorUnavailable("the model returned no usable shots")

    edl = EditDecisionList(
        title=str(payload.get("title") or brief.title)[:120],
        shots=shots,
        texts=texts_from_json(payload.get("texts", [])),
        look=Look(preset=str(payload.get("look", brief.look))),
        texture=float(payload.get("texture", brief.texture) or 0.0),
        letterbox=float(payload.get("letterbox", brief.letterbox) or 0.0),
        fps=settings.quality.fps,
        width=settings.primary_format.width,
        height=settings.primary_format.height,
        rationale=str(payload.get("rationale", ""))[:600],
    )

    if music_path is not None:
        edl.music = MusicCue(
            source=music_path,
            offset=music_offset,
            gain=0.55 if brief.keep_source_audio else 0.85,
            duck=brief.keep_source_audio,
        )

    # Match the shots to each other, exactly as the built-in editor does. The
    # model is asked for a film-wide look and is not asked — and should not be
    # asked — to guess how many stops apart two clips it cannot measure are. So
    # a Claude-directed film used to arrive with every shot ungraded relative to
    # its neighbours, while the same footage through the built-in editor came
    # back matched. Same pictures, different film, decided by which director ran.
    from .heuristic import _match_looks

    _match_looks(edl.shots, by_id, edl.look.preset, edl.look.strength)

    notes = edl.repair(by_id, target_duration=brief.duration or settings.target_duration)
    for note in notes:
        log.info("repaired the model's edit: %s", note)

    log.info("the model cut %d shots (%.2fs)", len(edl.shots), edl.duration)
    return edl
