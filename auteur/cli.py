"""Command line for the editor.

    auteur edit ./rushes --prompt "moody neon chase, 20 seconds"
    auteur analyse ./rushes
    auteur looks
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import __version__


def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING if verbosity <= 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)
    if verbosity < 2:
        logging.getLogger("auteur.ffmpeg").setLevel(logging.WARNING)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auteur",
        description="An autonomous cinematic editor: clips in, finished film out.",
    )
    parser.add_argument("--version", action="version", version=f"auteur {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=1,
                        help="repeat for more detail (-vv shows every ffmpeg call)")
    parser.add_argument("-q", "--quiet", action="store_true", help="only report problems")

    sub = parser.add_subparsers(dest="command", required=True)

    edit = sub.add_parser("edit", help="cut a film from a folder of clips")
    edit.add_argument("inputs", nargs="+", help="clips, folders of clips, and music")
    edit.add_argument("-p", "--prompt", required=True,
                      help='the direction, e.g. "fast neon montage, 20s, \\"AFTER DARK\\""')
    edit.add_argument("-d", "--duration", type=float, default=None, help="target runtime in seconds")
    edit.add_argument("-f", "--format", default="reel",
                      help="delivery formats, comma separated: reel, square, wide, cinema, portrait")
    edit.add_argument("--quality", default="standard", choices=["draft", "standard", "master"])
    edit.add_argument("-o", "--out", default=None, help="working directory (default ./auteur-work)")
    edit.add_argument("--seed", type=int, default=None, help="make a different cut of the same brief")
    edit.add_argument("--rounds", type=int, default=None,
                      help="how many times to watch it back and re-cut (default 1)")
    edit.add_argument("--no-llm", action="store_true", help="use the heuristic director only")
    edit.add_argument("--model", default=None, help="model id for the director")

    analyse = sub.add_parser("analyse", help="report what the agent sees in the footage")
    analyse.add_argument("inputs", nargs="+")
    analyse.add_argument("--json", action="store_true", help="emit the raw dossiers")

    sub.add_parser("looks", help="list the available film emulations and transitions")
    return parser


def _run_edit(args: argparse.Namespace) -> int:
    from .agent import direct
    from .config import Settings, resolve_format, resolve_quality

    try:
        formats = tuple(resolve_format(name) for name in args.format.split(",") if name.strip())
        quality = resolve_quality(args.quality)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not formats:
        print("error: no delivery format requested", file=sys.stderr)
        return 2

    settings = Settings(
        quality=quality,
        primary_format=formats[0],
        extra_formats=formats[1:],
        target_duration=args.duration or 30.0,
        use_llm=not args.no_llm,
    )
    if args.seed is not None:
        settings.seed = args.seed
    if args.rounds is not None:
        settings.revision_rounds = max(0, args.rounds)
    if args.model:
        settings.model = args.model

    try:
        production = direct(
            args.inputs, args.prompt, settings=settings,
            workspace=args.out, formats=formats, duration=args.duration,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print()
    print(production.edl.describe())
    print()
    critique = production.final_critique
    if critique is not None:
        print(critique.describe())
        print()
    print(f"directed by the {production.directed_by} director in {production.seconds:.1f}s")
    for name, path in production.outputs.items():
        print(f"  {name:<9} {path}")
    print(f"  notes     {production.workspace.root / 'production-notes.md'}")
    return 0


def _run_analyse(args: argparse.Namespace) -> int:
    from .analysis import build_dossiers, find_music_bed
    from .ingest import ingest

    try:
        bin_ = ingest(args.inputs)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    dossiers = build_dossiers(bin_.visuals)
    if args.json:
        print(json.dumps([dossier.to_json() for dossier in dossiers], indent=2))
        return 0

    print(bin_.describe())
    print()
    for dossier in dossiers:
        video = dossier.video
        print(f"{dossier.clip_id}  {dossier.asset.name}")
        print(f"    quality {dossier.quality:.2f} · saturation {video.saturation:.2f} · "
              f"warmth {video.warmth:+.2f} · {len(dossier.takes)} usable take(s)")
        if video.shot_boundaries:
            print(f"    contains {len(video.shot_boundaries)} cut(s) of its own")
        if not dossier.audio.silent:
            print(f"    sound: {dossier.audio.loudness:.1f} dBFS"
                  + (f" · {dossier.audio.tempo:.0f} BPM" if dossier.audio.has_beat else "")
                  + (" · likely speech" if dossier.audio.speechiness > 0.5 else ""))
        for take in sorted(dossier.takes, key=lambda t: -t.score)[:3]:
            print(f"      {take.start:6.2f}–{take.end:6.2f}  score {take.score:.2f}  "
                  f"{take.scale}/{take.camera}")

    music, analysis = find_music_bed(bin_.audio)
    if music and analysis:
        print()
        print(f"music bed: {music.name} — {analysis.tempo:.0f} BPM "
              f"(confidence {analysis.tempo_confidence:.2f}), {len(analysis.beats)} beats")
    return 0


def _run_looks() -> int:
    from .craft import color, transitions

    print("Film emulations:")
    print(color.describe_looks())
    print()
    print("Transitions:")
    print(transitions.describe())
    print()
    print(f"custom motion-blurred transitions: "
          f"{'available' if transitions.supports_custom() else 'unavailable on this ffmpeg build'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(0 if args.quiet else args.verbose)

    try:
        if args.command == "edit":
            return _run_edit(args)
        if args.command == "analyse":
            return _run_analyse(args)
        if args.command == "looks":
            return _run_looks()
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - the CLI is the last line of defence
        logging.getLogger("auteur").debug("unhandled failure", exc_info=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
