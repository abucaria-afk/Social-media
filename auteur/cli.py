"""Command line for the editor.

    auteur edit ./clips "moody neon chase, 20 seconds"
    auteur demo                    # makes practice footage, then edits it
    auteur serve                   # open it on your phone
    auteur analyse ./clips         # what the agent sees in your footage
    auteur looks                   # the film looks you can ask for

Everything printed here is written for someone who has never read the source.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .ui import NullReporter, Reporter, describe_count, describe_duration, describe_shape

EXAMPLES = """examples:
  auteur edit ./clips "fast neon montage, 20 seconds"
  auteur edit ./clips "warm summer memories" --shape square
  auteur edit ./clips ./song.mp3 --prompt "slow and cinematic, 30 seconds"
  auteur demo
  auteur serve                          then open the address on your phone

tips:
  Put your clips and your music in one folder and point at the folder.
  Anything you put "in quotes" inside the prompt appears on screen.
  Say how long you want it -- "20 seconds" -- and it will hit that length.
"""


def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING if verbosity <= 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="  %(message)s", stream=sys.stderr)
    logging.getLogger("auteur.ffmpeg").setLevel(logging.WARNING if verbosity < 2 else logging.DEBUG)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auteur",
        description="Turns a folder of clips into a finished film.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"auteur {__version__}")
    parser.add_argument("-q", "--quiet", action="store_true", help="print nothing but the finished path")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="show what it is doing internally (-vv for every ffmpeg call)")

    sub = parser.add_subparsers(dest="command", required=True,
                                metavar="{edit,demo,serve,analyse,looks}")

    edit = sub.add_parser(
        "edit", help="make a film", epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    edit.add_argument("paths", nargs="+", metavar="FOOTAGE",
                      help="folder or files to edit; the last one may be the prompt instead")
    edit.add_argument("-p", "--prompt", default=None,
                      help="what kind of film you want, in your own words")
    edit.add_argument("-l", "--length", type=float, default=None, metavar="SECONDS",
                      help="how long it should be (you can also just say it in the prompt)")
    edit.add_argument("-s", "--shape", default="vertical", metavar="SHAPE",
                      help="vertical (default), square, widescreen, cinematic — "
                           "comma-separate to get several")
    edit.add_argument("--quality", default="standard", choices=["draft", "standard", "best"],
                      help="draft is quick and rough, best is slow and beautiful")
    edit.add_argument("-o", "--out", default=None, metavar="FOLDER",
                      help="where to put everything (default ./auteur-work)")
    edit.add_argument("--details", action="store_true", help="also print the full shot list")
    edit.add_argument("--revisions", type=int, default=None, metavar="N",
                      help="how many times to watch it back and improve it (default 1)")
    edit.add_argument("--seed", type=int, default=None, help="change this for a different cut")
    edit.add_argument("--no-ai", action="store_true", help="never call Claude; use the built-in editor")
    edit.add_argument("--model", default=None, help=argparse.SUPPRESS)
    edit.add_argument("--rounds", type=int, default=None, help=argparse.SUPPRESS)  # old name

    demo = sub.add_parser("demo", help="make practice footage and edit it, to see how this works")
    demo.add_argument("-o", "--out", default="auteur-demo", metavar="FOLDER")
    demo.add_argument("-p", "--prompt", default='fast neon montage, 12 seconds, "AFTER DARK"')

    serve = sub.add_parser(
        "serve", help="open the edit room in a browser, so you can use it from your phone")
    serve.add_argument("--port", type=int, default=8000, help="default 8000")
    serve.add_argument("--host", default="0.0.0.0",
                       help="0.0.0.0 lets your phone reach it; 127.0.0.1 keeps it to this computer")
    serve.add_argument("-o", "--out", default=None, metavar="FOLDER",
                       help="where uploads and finished films go (default ./auteur-web)")
    serve.add_argument("--quality", default="draft", choices=["draft", "standard", "best"],
                       help="draft keeps phone renders quick (default)")

    analyse = sub.add_parser("analyse", help="show what the agent sees in your footage")
    analyse.add_argument("paths", nargs="+", metavar="FOOTAGE")
    analyse.add_argument("--json", action="store_true", help="machine-readable output")

    sub.add_parser("looks", help="list the film looks and transitions you can ask for")
    return parser


# ---------------------------------------------------------------------------
# Plain words in, internal names out
# ---------------------------------------------------------------------------

SHAPES = {
    "vertical": "reel", "portrait": "portrait", "square": "square",
    "widescreen": "wide", "wide": "wide", "cinematic": "cinema", "cinema": "cinema",
    "phone": "reel", "tiktok": "reel", "reel": "reel", "youtube": "wide",
}
QUALITIES = {"draft": "draft", "standard": "standard", "best": "master"}


def _split_paths_and_prompt(paths: list[str], prompt: str | None) -> tuple[list[str], str | None]:
    """Let the prompt be the last argument, so quoting a sentence just works.

    `auteur edit ./clips "fast montage"` is what people type first. Only treat
    the last argument as a prompt when it is not a path that exists — otherwise
    a folder called "summer" would be mistaken for direction.
    """
    if prompt is not None or len(paths) < 2:
        return paths, prompt
    candidate = paths[-1]
    if Path(candidate).exists():
        return paths, prompt
    return paths[:-1], candidate


def _run_edit(args: argparse.Namespace, say: Reporter) -> int:
    from .agent import direct
    from .config import Settings, resolve_format, resolve_quality

    paths, prompt = _split_paths_and_prompt(args.paths, args.prompt)

    if not prompt:
        say.failure(
            "I need to know what kind of film to make",
            'try:  auteur edit ' + (paths[0] if paths else "./clips") + ' "fast neon montage, 20 seconds"',
        )
        return 2

    try:
        names = [SHAPES.get(part.strip().lower(), part.strip()) for part in args.shape.split(",") if part.strip()]
        formats = tuple(resolve_format(name) for name in names)
        quality = resolve_quality(QUALITIES.get(args.quality, args.quality))
    except ValueError:
        say.failure(
            f"I do not know the shape {args.shape!r}",
            "choose from: vertical, square, widescreen, cinematic, portrait",
        )
        return 2
    if not formats:
        say.failure("no shape requested", "try --shape vertical")
        return 2

    settings = Settings(
        quality=quality,
        primary_format=formats[0],
        extra_formats=formats[1:],
        target_duration=args.length or 30.0,
        use_llm=not args.no_ai,
    )
    if args.seed is not None:
        settings.seed = args.seed
    revisions = args.revisions if args.revisions is not None else args.rounds
    if revisions is not None:
        settings.revision_rounds = max(0, revisions)
    if args.model:
        settings.model = args.model

    say.banner(prompt)

    try:
        production = direct(
            paths, prompt, settings=settings, workspace=args.out,
            formats=formats, duration=args.length, reporter=say,
        )
    except FileNotFoundError as exc:
        say.failure("I could not find any footage to edit", str(exc))
        return 1

    if args.details:
        say.blank()
        print(production.edl.describe())

    fmt = settings.primary_format
    critique = production.final_critique
    facts = [
        f"{describe_duration(production.edl.duration)}  ·  "
        f"{describe_count(len(production.edl.shots), 'shot')}  ·  "
        f"{describe_shape(fmt.width, fmt.height)} {fmt.width}x{fmt.height}",
    ]
    if critique is not None:
        facts.append(f"it rates its own work {critique.score:.0%}")
    facts.append(f"took {describe_duration(production.seconds)}")

    files = [(name, str(path)) for name, path in production.outputs.items()]
    files.append(("what it did and why", str(production.workspace.root / "production-notes.md")))

    say.result(headline="Your film is ready", facts=facts, files=files)

    if args.quiet and production.primary:
        print(production.primary)
    return 0


def _run_demo(args: argparse.Namespace, say: Reporter) -> int:
    """Make footage and edit it, so a first run needs nothing but the command."""
    import subprocess

    out = Path(args.out)
    rushes = out / "rushes"
    say.banner("a first run, using footage made for the occasion")
    say.step("Making some practice footage")

    script = Path(__file__).resolve().parent.parent / "demo" / "make_footage.py"
    if not script.exists():
        say.failure("the demo footage generator is missing", f"expected it at {script}")
        return 1
    result = subprocess.run([sys.executable, str(script), str(rushes)], capture_output=True, text=True)
    if result.returncode != 0:
        say.failure("could not make the practice footage", result.stderr.strip()[-300:])
        return 1
    say.detail(f"made 7 clips and a 120 BPM track in {rushes}")

    # Built from the demo's own arguments rather than from scratch, so the global
    # flags (--quiet, --verbose) carry through and a new `edit` option cannot
    # leave this namespace missing an attribute — which it did, and the demo
    # reported "something went wrong" straight after printing the finished film.
    namespace = argparse.Namespace(**{
        **vars(args),
        "paths": [str(rushes)], "prompt": args.prompt, "length": None,
        "shape": "vertical", "quality": "draft", "out": str(out / "work"),
        "details": False, "revisions": 1, "seed": None, "no_ai": False,
        "model": None, "rounds": None,
    })
    return _run_edit(namespace, say)


def _run_serve(args: argparse.Namespace, say: Reporter) -> int:
    from .web.server import serve

    try:
        serve(
            host=args.host, port=args.port,
            workspace=Path(args.out) if args.out else None,
            quality=QUALITIES.get(args.quality, args.quality),
        )
    except OSError as exc:
        say.failure(
            f"could not open port {args.port}",
            f"{exc}\n{' ' * 5}something else may be using it — try --port 8080",
        )
        return 1
    return 0


def _run_analyse(args: argparse.Namespace, say: Reporter) -> int:
    from .analysis import build_dossiers, find_music_bed
    from .ingest import ingest

    try:
        bin_ = ingest(args.paths)
    except FileNotFoundError as exc:
        say.failure("I could not find any footage", str(exc))
        return 1

    dossiers = build_dossiers(bin_.visuals)
    if args.json:
        print(json.dumps([dossier.to_json() for dossier in dossiers], indent=2))
        return 0

    print()
    print(f"  {describe_count(len(bin_.visuals), 'clip')}, "
          f"{describe_duration(bin_.total_footage)} of footage")
    print()
    for dossier in dossiers:
        stars = "*" * max(1, round(dossier.quality * 5))
        print(f"  {dossier.asset.name}")
        print(f"      worth using: {stars:<5}  ({dossier.quality:.0%})")
        print(f"      {describe_count(len(dossier.takes), 'good moment')}"
              + (f", {describe_count(len(dossier.video.shot_boundaries), 'cut')} already in it"
                 if dossier.video.shot_boundaries else ""))
        best = dossier.best_take
        if best:
            print(f"      best bit: {best.start:.1f}s to {best.end:.1f}s ({best.scale} shot, {best.camera})")
        if not dossier.audio.silent:
            print("      has sound" + (" — sounds like talking" if dossier.audio.speechiness > 0.5 else ""))
        print()

    music, analysis = find_music_bed(bin_.audio)
    if music and analysis:
        print(f"  music: {music.name} at {analysis.tempo:.0f} beats per minute")
        print()
    return 0


def _run_looks() -> int:
    from .craft import color, transitions

    print()
    print("  Film looks — say one of these in your prompt:")
    print()
    for name, spec in color.LOOKS.items():
        print(f"      {name:<15} {spec.description}")
    print()
    print("  Transitions it can use between shots:")
    print()
    names = sorted(set(transitions.BUILTIN) | set(transitions.CUSTOM_EXPRESSIONS))
    for row in range(0, len(names), 4):
        print("      " + "".join(f"{n:<16}" for n in names[row:row + 4]))
    print()
    print("  It prefers straight cuts, and only uses these when the shots invite it.")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    say: Reporter = NullReporter() if args.quiet else Reporter()

    try:
        if args.command == "edit":
            return _run_edit(args, say)
        if args.command == "demo":
            return _run_demo(args, say)
        if args.command == "serve":
            return _run_serve(args, say)
        if args.command == "analyse":
            return _run_analyse(args, say)
        if args.command == "looks":
            return _run_looks()
    except KeyboardInterrupt:
        say.failure("stopped")
        return 130
    except Exception as exc:  # noqa: BLE001 - the CLI is the last line of defence
        logging.getLogger("auteur").debug("unhandled failure", exc_info=True)
        say.failure("something went wrong", f"{exc}\n{' ' * 5}run again with -vv to see the detail")
        return 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
