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

TEMPLATE_EXAMPLES = """examples:
  auteur template watch reel.mp4          read a reel's timing and keep it
  auteur template list                    what it has watched
  auteur template cut 21cb photos/*.jpg   your pictures, cut to that reel

A template is when the cuts land and what each shot looked like — timing and
tone, not footage. The film it makes is your pictures and nobody else's.
"""

WORKFLOW_EXAMPLES = """examples:
  auteur workflow list
  auteur workflow run instagram-reel ./clips "harbour at dusk"
  auteur workflow run tiktok ./clips "harbour at dusk" --schedule next
  auteur workflow run youtube-short ./clips "how it was built" -l 45

what a workflow does that `edit` does not:
  cuts to the length that place accepts, keeps titles out from under the
  app's own buttons, pulls a cover frame, and writes a caption to rewrite.

it does not post anything. it makes a folder you can post from.
"""

SCHOLAR_EXAMPLES = """examples:
  auteur scholar                            what it knows and what it wants next
  auteur scholar study colour grading       watch and learn about one thing
  auteur scholar teach --agent hook         what it would tell the hook agent
  auteur scholar subscribe UC... Chan name  follow a channel for new uploads
  auteur scholar ask "how do I pace a montage?"
  auteur scholar watch --every 30           keep studying, in the foreground

`auteur serve` starts the same background study loop on its own, so the
Scholar learns whenever the app is up. Turn that off with --no-scholar.

Studying needs yt-dlp (`pip install auteur[scholar]`). It reads titles,
chapters and captions — it never downloads video. Asking needs ANTHROPIC_API_KEY.
Without either it tells you so rather than reporting an empty success.
"""

REHEARSE_EXAMPLES = """examples:
  auteur rehearse ./footage -n 30
  auteur rehearse ./footage --forever
  auteur rehearse ./footage --against pulkitxx -l 6

The crew argues about one edit for three rounds and stops, which is right for
someone waiting on a render and wrong for getting better at this. This runs the
other loop: build a candidate, measure it on both yardsticks, change something,
build again — far more times than anybody would sit through.

It does not stop when it passes the target. It raises the bar to what it just
achieved and carries on, because a goal stops being useful the moment it is met.

What it cannot do is make the footage better. If the source has no depth in it,
no amount of rehearsal invents any, and the loop says so rather than grinding.
"""

BENCHMARK_EXAMPLES = """examples:
  auteur benchmark add ./thegoal.mp4 --name pulkitxx
  auteur benchmark add a=./one.mp4 b=./two.mp4 --name the-goal
  auteur benchmark                          what is being chased, hardest first
  auteur benchmark remove pulkitxx

Two scores, and both have to be beaten.

  structure   the same hook/share/loop model that scores your own edits
  craft       measured off the frames: subject separation, how clear the
              subject is, palette discipline, exposure headroom

Craft exists because the first film added here scored 0.42 on structure while
being plainly better than anything this program had made. A yardstick that
cannot see that can be beaten by a worse-looking film, which is backwards.
"""

MEDIA_EXAMPLES = """examples:
  auteur media scan ./footage         index everything, once
  auteur media list --kind video      what is in the index
  auteur media duplicates             the same clip, saved twice
  auteur media tag ./footage/a.mp4 --label keepers

The second scan of a folder only looks at what changed, so it is quick.
The index is one JSON file; deleting it costs a rescan and nothing else.
"""

INSIGHT_EXAMPLES = """examples:
  auteur insight fit ./exports/*.csv       what the winners have in common
  auteur insight simulate --rows 5000 -o practice.csv
  auteur insight fit                       fit on simulated data alone

the three objectives:
  hook    three_second_watch_rate  above 0.80
  share   share_to_view_ratio      above 0.05
  loop    loop_count               above 1.5

With no exports it will fit a model to numbers it invented, because
rehearsing the machinery is worth doing before there is data to run it on.
It says so every time. Do not quote those numbers as evidence.
"""

SCHEDULE_EXAMPLES = """examples:
  auteur schedule                     what is queued
  auteur schedule due                 what should go out now
  auteur schedule done 4f2a1c9d       mark one as posted
  auteur schedule export > posts.csv

Posts are spaced out: one every few hours per service, a few a day at most.
Nothing here posts for you -- it says what to post and when.
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
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="print nothing but the finished path"
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="show what it is doing internally (-vv for every ffmpeg call)",
    )

    # The metavar is set at the bottom of this function, from the commands
    # that were actually added. It used to be typed out here, and by the time
    # anybody compared the two, `auteur --help` was listing fourteen commands
    # above a description of sixteen: `moderate` and `template` both existed,
    # both worked, and neither appeared in the line that tells you what you
    # can run. Nothing could catch it, because the list was a string that
    # named the parsers and was never compared to them.
    sub = parser.add_subparsers(dest="command", required=True)

    edit = sub.add_parser(
        "edit",
        help="make a film",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    edit.add_argument(
        "paths",
        nargs="+",
        metavar="FOOTAGE",
        help="folder or files to edit; the last one may be the prompt instead",
    )
    edit.add_argument(
        "-p", "--prompt", default=None, help="what kind of film you want, in your own words"
    )
    edit.add_argument(
        "-l",
        "--length",
        type=float,
        default=None,
        metavar="SECONDS",
        help="how long it should be (you can also just say it in the prompt)",
    )
    edit.add_argument(
        "-s",
        "--shape",
        default="vertical",
        metavar="SHAPE",
        help="vertical (default), square, widescreen, cinematic — comma-separate to get several",
    )
    edit.add_argument(
        "--quality",
        default="standard",
        choices=["draft", "standard", "best"],
        help="draft is quick and rough, best is slow and beautiful",
    )
    edit.add_argument(
        "-o",
        "--out",
        default=None,
        metavar="FOLDER",
        help="where to put everything (default ./auteur-work)",
    )
    edit.add_argument("--details", action="store_true", help="also print the full shot list")
    edit.add_argument(
        "--revisions",
        type=int,
        default=None,
        metavar="N",
        help="how many times to watch it back and improve it (default 1)",
    )
    edit.add_argument("--seed", type=int, default=None, help="change this for a different cut")
    edit.add_argument(
        "--no-ai", action="store_true", help="never call Claude; use the built-in editor"
    )
    edit.add_argument("--model", default=None, help=argparse.SUPPRESS)
    edit.add_argument("--rounds", type=int, default=None, help=argparse.SUPPRESS)  # old name

    demo = sub.add_parser("demo", help="make practice footage and edit it, to see how this works")
    demo.add_argument("-o", "--out", default="auteur-demo", metavar="FOLDER")
    demo.add_argument("-p", "--prompt", default='fast neon montage, 12 seconds, "AFTER DARK"')

    serve = sub.add_parser(
        "serve", help="open the edit room in a browser, so you can use it from your phone"
    )
    serve.add_argument("--port", type=int, default=8000, help="default 8000")
    serve.add_argument(
        "--claim",
        action="store_true",
        help="leave it unclaimed so the first person to open it makes the account",
    )
    serve.add_argument(
        "--host",
        default="0.0.0.0",
        help="0.0.0.0 lets your phone reach it; 127.0.0.1 keeps it to this computer",
    )
    serve.add_argument(
        "-o",
        "--out",
        default=None,
        metavar="FOLDER",
        help="where uploads and finished films go (default ./auteur-web)",
    )
    serve.add_argument(
        "--quality",
        default="draft",
        choices=["draft", "standard", "best"],
        help="draft keeps phone renders quick (default)",
    )
    serve.add_argument(
        "--no-scholar",
        action="store_true",
        help="do not let the study agent learn in the background while serving",
    )
    serve.add_argument(
        "--scholar-every",
        type=float,
        default=60.0,
        metavar="MINUTES",
        help="how often the background study agent looks for something new (default 60)",
    )

    account = sub.add_parser("account", help="who can sign in to the phone app")
    account.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=["show", "add", "password"],
        help="show (default), add a person, or change a password",
    )
    account.add_argument(
        "-o",
        "--out",
        default=None,
        metavar="FOLDER",
        help="the serve folder the accounts live in (default ./auteur-web)",
    )
    account.add_argument("-u", "--user", default=None, help="username")
    account.add_argument("-e", "--email", default=None, help="email, for password resets")
    account.add_argument(
        "--born",
        default=None,
        metavar="YEAR",
        help=(
            "the year they were born. Under 18 starts with sensitive films "
            "hidden; under 12 is refused, which is the rating this app ships at"
        ),
    )
    account.add_argument(
        "--restrict",
        default=None,
        choices=["on", "off"],
        help="hide sensitive and reported films from this account",
    )
    account.add_argument(
        "--lock",
        default=None,
        metavar="CODE",
        help="four digits, needed to lift the restriction. Use --lock '' to remove it",
    )

    moderate = sub.add_parser(
        "moderate",
        help="what people have reported on your instance, and what to do about it",
    )
    moderate.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=["show", "remove", "hide", "keep", "close"],
        help=(
            "show what is waiting (default), remove the film a report is about, "
            "hide it from restricted accounts, keep it, or close somebody's account"
        ),
    )
    moderate.add_argument("target", nargs="?", help="a report id, or a username for `close`")
    moderate.add_argument(
        "-o",
        "--out",
        default=None,
        metavar="FOLDER",
        help="the serve folder this instance uses (default ./auteur-web)",
    )
    moderate.add_argument("--note", default="", help="why, kept with the report")
    moderate.add_argument("--all", action="store_true", help="include reports already decided")

    analyse = sub.add_parser("analyse", help="show what the agent sees in your footage")
    analyse.add_argument("paths", nargs="+", metavar="FOOTAGE")
    analyse.add_argument("--json", action="store_true", help="machine-readable output")

    sub.add_parser("looks", help="list the film looks and transitions you can ask for")

    # -- templates --------------------------------------------------------

    template = sub.add_parser(
        "template",
        help="read a reel shot by shot, then cut your own pictures to its timing",
        epilog=TEMPLATE_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    template.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "watch", "cut"],
        help="list what it has watched (default), watch a reel, or cut to one",
    )
    template.add_argument(
        "paths",
        nargs="*",
        help="reels to watch, or — for `cut` — the template then your pictures",
    )
    template.add_argument("--name", default="", help="what to call a reel it watches")
    template.add_argument(
        "--seconds", type=float, default=0.0, help="runtime to fill; the reel's own by default"
    )
    template.add_argument("--words", default="", help="words to put on screen, comma separated")
    template.add_argument("--out", default="", help="where to write the film")

    # -- workflows --------------------------------------------------------

    workflow = sub.add_parser(
        "workflow",
        help="make a post for a particular place — Reels, TikTok, Shorts",
        epilog=WORKFLOW_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    workflow.add_argument(
        "action", nargs="?", default="list", choices=["list", "run"], help="list (default) or run"
    )
    workflow.add_argument(
        "platform",
        nargs="?",
        metavar="WHERE",
        help="instagram-reel, instagram-post, instagram-story, tiktok, tiktok-photo, youtube-short",
    )
    workflow.add_argument("paths", nargs="*", metavar="FOOTAGE")
    workflow.add_argument("-p", "--prompt", default=None, help="what the post is about")
    workflow.add_argument(
        "-l", "--length", type=float, default=None, metavar="SECONDS", help="override the runtime"
    )
    workflow.add_argument("--quality", default="standard", choices=["draft", "standard", "best"])
    workflow.add_argument(
        "-o",
        "--out",
        default=None,
        metavar="FOLDER",
        help="where posts go (default ./auteur-posts)",
    )
    workflow.add_argument(
        "--schedule",
        default=None,
        metavar="WHEN",
        help='queue it — "friday 18:00", "2026-08-20 09:00", or "next" for the first free slot',
    )
    workflow.add_argument(
        "--no-ai", action="store_true", help="never call Claude; use the built-in editor"
    )
    workflow.add_argument("--seed", type=int, default=None, help="change this for a different cut")
    workflow.add_argument(
        "--agents",
        default="off",
        choices=["off", "manual", "supervised", "autonomous"],
        help="let the hook/share/loop agents re-cut before rendering; "
        "supervised asks you about anything structural",
    )
    workflow.add_argument(
        "--data",
        action="append",
        default=None,
        metavar="FILE",
        help="a performance export to train the agents on, .csv or .jsonl (repeatable)",
    )
    workflow.add_argument(
        "--reference",
        action="append",
        default=None,
        metavar="VIDEO",
        help="footage to cut like — measured for pace, exposure and motion (repeatable)",
    )
    workflow.add_argument(
        "--look",
        action="store_true",
        help="let the agents render and look at what they propose, not just score it",
    )
    workflow.add_argument(
        "--stickers",
        default=None,
        metavar="DIR",
        help="a folder of your own transparent PNGs, placed clear of the subject",
    )

    insight = sub.add_parser(
        "insight",
        help="what your performance data says about hooks, shares and loops",
        epilog=INSIGHT_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    insight.add_argument(
        "action",
        nargs="?",
        default="fit",
        choices=["fit", "simulate", "score"],
        help="fit (default) reads your exports, simulate writes a practice corpus, score scores a single video",
    )
    insight.add_argument("paths", nargs="*", metavar="CSV", help="performance exports")
    insight.add_argument(
        "--rows", type=int, default=2000, help="simulated rows to add (0 for measured only)"
    )
    insight.add_argument("-o", "--out", default=None, metavar="FILE", help="where to write")
    insight.add_argument("--json", action="store_true", help="machine-readable output")

    rehearse = sub.add_parser(
        "rehearse",
        help="build, measure, change, rebuild — until the target is passed, then again",
        epilog=REHEARSE_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    rehearse.add_argument("paths", nargs="+", metavar="FOOTAGE")
    rehearse.add_argument(
        "-n",
        "--generations",
        type=int,
        default=20,
        help="how many candidates to build and measure (default 20)",
    )
    rehearse.add_argument(
        "--forever",
        action="store_true",
        help="never stop — the loop is the point, not any one film it makes",
    )
    rehearse.add_argument(
        "--against", default=None, metavar="NAME", help="which benchmark to chase"
    )
    rehearse.add_argument(
        "-l",
        "--length",
        type=float,
        default=8.0,
        metavar="SECONDS",
        help="how long each candidate is (short is faster and enough to measure)",
    )
    rehearse.add_argument("--seed", type=int, default=None)

    bench = sub.add_parser(
        "benchmark",
        help="films to reach and surpass — measured on structure and on craft",
        epilog=BENCHMARK_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    bench.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=["show", "add", "remove"],
        help="show (default) what is being chased, add a film, or drop one",
    )
    bench.add_argument("paths", nargs="*", metavar="VIDEO")
    bench.add_argument("--name", default=None, help="what to call it (default: the filename)")
    bench.add_argument("--json", action="store_true", help="machine-readable output")

    agents_cmd = sub.add_parser(
        "agents", help="what the crew has learned about which changes are worth making"
    )
    agents_cmd.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=["show", "forget"],
        help="show (default) what has earned its place, or forget it all and start over",
    )
    agents_cmd.add_argument("--json", action="store_true", help="machine-readable output")

    scholar = sub.add_parser(
        "scholar",
        help="the study agent — what it has learned, and what it wants to watch next",
        epilog=SCHOLAR_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    scholar.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=[
            "status",
            "study",
            "watch",
            "teach",
            "subscribe",
            "ask",
            "read",
            "critique",
            "scroll",
        ],
        help=(
            "status (default), study once, watch (keep studying), teach, subscribe, ask, "
            "read (study files on disk), critique (hold a cut against what it studied), "
            "scroll (be served reels and measure what arrived)"
        ),
    )
    scholar.add_argument(
        "--feed",
        default="youtube",
        help="where to scroll: youtube, or a folder of reels to serve in order",
    )
    scholar.add_argument(
        "--every",
        type=float,
        default=60.0,
        metavar="MINUTES",
        help="for `watch`: how often to look for something new (default 60)",
    )
    scholar.add_argument("words", nargs="*", metavar="TEXT", help="the topic, channel or question")
    scholar.add_argument(
        "--agent",
        default=None,
        metavar="NAME",
        help="teach one agent rather than the whole crew (hook, share, loop, gaze, overlay)",
    )
    scholar.add_argument(
        "--videos", type=int, default=5, metavar="N", help="how many videos one session watches"
    )
    scholar.add_argument("--json", action="store_true", help="machine-readable output")
    scholar.add_argument(
        "--from",
        dest="study_from",
        nargs="*",
        default=None,
        metavar="PATH",
        help="for `read`: folders or files to study (documents and films). Default: docs and .",
    )

    media = sub.add_parser(
        "media",
        help="the media manager — index your footage once, find the duplicates",
        epilog=MEDIA_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    media.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["scan", "list", "duplicates", "tag"],
        help="scan a folder, list what is known, find duplicates, or tag files",
    )
    media.add_argument("paths", nargs="*", metavar="FOLDER")
    media.add_argument(
        "-i", "--index", default=None, metavar="FILE", help="the index file (default in the folder)"
    )
    media.add_argument("--kind", default=None, choices=["video", "image", "audio"])
    media.add_argument("--label", default=None, help="the tag to apply, for `media tag`")
    media.add_argument("--json", action="store_true", help="machine-readable output")

    schedule = sub.add_parser(
        "schedule",
        help="when each finished post goes out",
        epilog=SCHEDULE_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    schedule.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "due", "done", "skip", "remove", "export", "tidy"],
        help="list (default), what is due now, mark one done or skipped, remove, or export",
    )
    schedule.add_argument(
        "post_id", nargs="?", metavar="ID", help="which post, for done/skip/remove"
    )
    schedule.add_argument(
        "-o",
        "--out",
        default=None,
        metavar="FOLDER",
        help="the folder the queue lives in (default ./auteur-posts)",
    )
    schedule.add_argument("--gap", type=float, default=None, metavar="HOURS")
    schedule.add_argument("--per-day", type=int, default=None, metavar="N")

    # Now that every command exists, say so. `sub.choices` is the registry
    # argparse dispatches on, so the usage line and the dispatch cannot
    # disagree — adding a command changes both or neither.
    sub.metavar = "{" + ",".join(sub.choices) + "}"
    return parser


# ---------------------------------------------------------------------------
# Plain words in, internal names out
# ---------------------------------------------------------------------------

SHAPES = {
    "vertical": "reel",
    "portrait": "portrait",
    "square": "square",
    "widescreen": "wide",
    "wide": "wide",
    "cinematic": "cinema",
    "cinema": "cinema",
    "phone": "reel",
    "tiktok": "reel",
    "reel": "reel",
    "youtube": "wide",
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
            "try:  auteur edit "
            + (paths[0] if paths else "./clips")
            + ' "fast neon montage, 20 seconds"',
        )
        return 2

    try:
        names = [
            SHAPES.get(part.strip().lower(), part.strip())
            for part in args.shape.split(",")
            if part.strip()
        ]
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
            paths,
            prompt,
            settings=settings,
            workspace=args.out,
            formats=formats,
            duration=args.length,
            reporter=say,
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
    result = subprocess.run(
        [sys.executable, str(script), str(rushes)], capture_output=True, text=True
    )
    if result.returncode != 0:
        say.failure("could not make the practice footage", result.stderr.strip()[-300:])
        return 1
    say.detail(f"made 7 clips and a 120 BPM track in {rushes}")

    # Built from the demo's own arguments rather than from scratch, so the global
    # flags (--quiet, --verbose) carry through and a new `edit` option cannot
    # leave this namespace missing an attribute — which it did, and the demo
    # reported "something went wrong" straight after printing the finished film.
    namespace = argparse.Namespace(
        **{
            **vars(args),
            "paths": [str(rushes)],
            "prompt": args.prompt,
            "length": None,
            "shape": "vertical",
            "quality": "draft",
            "out": str(out / "work"),
            "details": False,
            "revisions": 1,
            "seed": None,
            "no_ai": False,
            "model": None,
            "rounds": None,
        }
    )
    return _run_edit(namespace, say)


def _start_scholar_in_background(say: Reporter, *, minutes: float = 60.0):
    """Let the Scholar study while the app is up.

    A study agent that only runs when somebody types a command is a study agent
    that never runs. Serving is the natural moment: the machine is already on
    and already going to sit there.

    A daemon thread, so quitting the server does not hang waiting for it, and
    the loop swallows its own failures — the Scholar not being able to reach
    YouTube must never be a reason the app will not serve.
    """
    import threading

    from .scholar.youtube import reachable

    can_study, how = reachable()
    if not can_study:
        say.detail(f"the Scholar is idle — {how}")
        return None

    from .scholar import Scholar

    scholar = Scholar()

    def study() -> None:
        try:
            scholar.run_forever(every_seconds=minutes * 60.0)
        except Exception:  # noqa: BLE001 - a background learner never takes the app down
            logging.getLogger("auteur.scholar").debug("the Scholar stopped", exc_info=True)

    thread = threading.Thread(target=study, name="auteur-scholar", daemon=True)
    thread.start()
    say.detail(
        f"the Scholar is studying in the background ({scholar.knowledge.total_learnings} learnings so far)"
    )
    return scholar


def _run_serve(args: argparse.Namespace, say: Reporter) -> int:
    from .web.server import serve

    if not args.no_scholar:
        _start_scholar_in_background(say, minutes=args.scholar_every)

    try:
        serve(
            host=args.host,
            port=args.port,
            workspace=Path(args.out) if args.out else None,
            quality=QUALITIES.get(args.quality, args.quality),
            claimable=bool(getattr(args, "claim", False)),
        )
    except OSError as exc:
        say.failure(
            f"could not open port {args.port}",
            f"{exc}\n{' ' * 5}something else may be using it — try --port 8080",
        )
        return 1
    except ValueError as exc:
        # A refused AUTEUR_PASSWORD. Say what is wrong with it, not a traceback.
        say.failure("that password will not do", str(exc))
        return 1
    return 0


def _run_account(args: argparse.Namespace, say: Reporter) -> int:
    """Manage who can sign in, without anyone having to edit a JSON file."""
    import getpass

    from .web.auth import MINIMUM_AGE, Accounts, age_from, password_problem

    root = Path(args.out) if args.out else Path.cwd() / "auteur-web"
    accounts = Accounts(Accounts.default_path(root))

    if args.action == "show":
        print()
        print(f"  accounts in {accounts.path}")
        print()
        if accounts.empty:
            print("     nobody yet — `auteur serve` creates the first one")
        for account in accounts.accounts.values():
            marks = []
            if account.locked:
                marks.append("locked out")
            if account.age >= 0:
                marks.append(f"{account.age}")
            if account.restricted:
                marks.append("restricted" + (" 🔒" if account.restriction_lock else ""))
            # What they are paying, if anything. An operator running a
            # business off this could not see who was on a plan at all: the
            # webhook wrote `plan` and nothing ever printed it.
            if account.paying:
                marks.append(f"{account.tier.name} · {account.tier.monthly}")
            elif account.plan != "free":
                marks.append(f"{account.plan} lapsed")
            state = f"  ({', '.join(marks)})" if marks else ""
            print(f"     {account.username:<24} {account.email}{state}")
        # Money that arrived for somebody who is not here. Printed because a
        # payment nobody can see is a payment nobody will act on.
        for held in accounts.unclaimed:
            print(f"     {'(paid, no account)':<24} {held.get('email', '?')}  ({held.get('plan')})")
        print()
        return 0

    username = args.user or input("username: ").strip()
    if not username:
        say.failure("a username is needed")
        return 2

    # Setting the restriction is not a password change and must not ask for
    # one — a parent turning it on for a child does not have the child's
    # password and should not need it.
    if args.action == "password" and (args.restrict is not None or args.lock is not None):
        account = accounts.get(username)
        if account is None:
            say.failure(f"no account called {username!r}")
            return 1
        if args.restrict is not None:
            accounts.set_restriction(account.username, args.restrict == "on")
        if args.lock is not None:
            problem = accounts.set_restriction_lock(account.username, args.lock)
            if problem:
                say.failure(problem)
                return 1
        again = accounts.get(username)
        print(
            f"\n  {again.username}: sensitive films are "
            f"{'hidden' if again.restricted else 'shown'}"
            f"{', and lifting it needs the code' if again.restriction_lock else ''}\n"
        )
        return 0

    born = 0
    if args.action == "add":
        if accounts.get(username) is not None:
            say.failure(f"{username} already exists", "use `auteur account password` instead")
            return 1
        # The age, before the password prompt. Asking somebody to type a
        # password twice and *then* telling them the account cannot exist is
        # the wrong order to find that out in.
        if args.born:
            try:
                born = int(args.born)
            except ValueError:
                say.failure(f"{args.born!r} is not a year")
                return 2
            years = age_from(born)
            if years < MINIMUM_AGE:
                say.failure(
                    f"that is {years}, and this app ships at {MINIMUM_AGE}+",
                    "the App Store rating and this check have to agree",
                )
                return 1
        email = args.email or input("email (for password resets): ").strip()
    else:
        existing = accounts.get(username)
        if existing is None:
            say.failure(f"no account called {username!r}")
            return 1
        username, email = existing.username, existing.email

    # getpass so the password never lands in the shell history or the screen.
    password = getpass.getpass("new password: ")
    if password != getpass.getpass("again: "):
        say.failure("those did not match")
        return 1
    problem = password_problem(password, username=username, email=email)
    if problem:
        say.failure("that password will not do", problem)
        return 1

    if args.action == "add":
        made = accounts.add(username, email, password, born=born)
        note = ""
        if made.restricted:
            note = (
                "\n  they are under 18, so sensitive and reported films start hidden"
                "\n  set a code with:  auteur account password --user "
                f"{username} --lock 1234"
            )
        print(f"\n  added {username} <{email}>{note}\n")
    else:
        accounts.set_password(accounts.get(username), password)
        print(
            f"\n  changed the password for {username}"
            f"\n  every signed-in device has been signed out\n"
        )
    return 0


def _run_moderate(args: argparse.Namespace, say: Reporter) -> int:
    """The operator's side of reporting.

    On an instance this size the moderator is the person whose computer it is,
    and this is their whole set of tools: see what was reported, remove the
    film, or close the account. That is a small set on purpose — it is also
    exactly the set the App Store asks for, and every one of these does the
    thing it says rather than filing a ticket somewhere.
    """
    import time

    from .manager import Board
    from .web.auth import Accounts
    from .web.profiles import Profiles
    from .web.safety import REASONS, Reports
    from .web.social import Films, Messages

    root = Path(args.out) if args.out else Path.cwd() / "auteur-web"
    reports = Reports(Reports.default_path(root))
    films = Films(Films.default_path(root))

    def ago(stamp: float) -> str:
        gap = max(0.0, time.time() - stamp)
        if gap < 3600:
            return f"{int(gap // 60)}m ago"
        if gap < 86400:
            return f"{int(gap // 3600)}h ago"
        return f"{int(gap // 86400)}d ago"

    if args.action == "show":
        rows = list(reports.reports.values()) if args.all else reports.open_ones()
        rows = sorted(rows, key=lambda r: (r.state != "open", not r.urgent, -r.at))
        print()
        print(f"  reports on {reports.path}")
        print()
        if not rows:
            print("     nothing reported")
            print()
            return 0
        for report in rows:
            mark = "  !" if report.urgent and report.state == "open" else "   "
            print(f"  {mark} {report.id}  {REASONS.get(report.reason, report.reason)}")
            # "a film by bob" and "a message by bob" both read correctly;
            # "a person by bob" does not. A report about a person is about
            # them, not about something they made.
            whose = (
                report.about_who
                if report.kind == "person"
                else f"a {report.kind} by {report.about_who}"
            )
            print(f"        {whose}, reported by {report.by}, {ago(report.at)}")
            if report.note:
                print(f"        “{report.note}”")
            if report.kind == "film":
                film = films.get(report.about)
                where = film.video if film is not None else "gone already"
                print(f"        {where}")
            if report.state != "open":
                print(
                    f"        -> {report.state}{', ' + report.decided_note if report.decided_note else ''}"
                )
        print()
        print("     auteur moderate remove <id>     take the film down")
        print("     auteur moderate hide <id>       leave it up, out of restricted accounts")
        print("     auteur moderate keep <id>       leave it, and say so")
        print("     auteur moderate close <person>  close their account entirely")
        print()
        return 0

    if args.action == "close":
        who = args.target or ""
        if not who:
            say.failure("which account?", "auteur moderate close <username>")
            return 2
        accounts = Accounts(Accounts.default_path(root))
        if accounts.get(who) is None:
            say.failure(f"no account called {who!r}")
            return 1
        who = accounts.get(who).username
        gone = films.forget_everything_by(who)
        for path in gone:
            for candidate in (Path(path), Path(path).with_suffix(".poster.jpg")):
                candidate.unlink(missing_ok=True)
        Messages(Messages.default_path(root)).forget_everything_with(who)
        Profiles(Profiles.default_path(root), root / "pictures").forget(who)
        Board(Board.default_path(root)).forget_everyones(who)
        reports.forget_everything_about(who)
        accounts.remove(who)
        print(f"\n  closed {who}: {len(gone)} film(s), their messages, plans and profile\n")
        return 0

    report = reports.get(args.target or "")
    if report is None:
        say.failure("no report with that id", "auteur moderate show")
        return 1

    if args.action == "hide":
        # The middle answer, and the one most reports actually deserve: not
        # "this should not exist" but "this is not for everybody". Anybody
        # with the content restriction on stops seeing it; everyone else does
        # not notice.
        if report.kind != "film":
            say.failure("only a film can be hidden", f"this report is about a {report.kind}")
            return 1
        if films.mark(report.about, True) is None:
            print("\n  that film had already gone\n")
        else:
            print("\n  hidden from restricted accounts, and left up for everybody else\n")
        reports.decide(report.id, "kept", args.note or "hidden from restricted accounts")
        return 0

    if args.action == "remove":
        if report.kind == "film":
            where = films.remove_any(report.about)
            if where:
                for candidate in (Path(where), Path(where).with_suffix(".poster.jpg")):
                    candidate.unlink(missing_ok=True)
                print("\n  removed the film and its file\n")
            else:
                print("\n  that film had already gone\n")
        else:
            print(
                f"\n  marked removed. A {report.kind} is not something this can delete for you —"
                f"\n  `auteur moderate close {report.about_who}` closes the account behind it.\n"
            )
        reports.decide(report.id, "removed", args.note)
        return 0

    reports.decide(report.id, "kept", args.note)
    print("\n  marked kept. The person who reported it can see that in the app.\n")
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
    print(
        f"  {describe_count(len(bin_.visuals), 'clip')}, "
        f"{describe_duration(bin_.total_footage)} of footage"
    )
    print()
    for dossier in dossiers:
        stars = "*" * max(1, round(dossier.quality * 5))
        print(f"  {dossier.asset.name}")
        print(f"      worth using: {stars:<5}  ({dossier.quality:.0%})")
        print(
            f"      {describe_count(len(dossier.takes), 'good moment')}"
            + (
                f", {describe_count(len(dossier.video.shot_boundaries), 'cut')} already in it"
                if dossier.video.shot_boundaries
                else ""
            )
        )
        best = dossier.best_take
        if best:
            print(
                f"      best bit: {best.start:.1f}s to {best.end:.1f}s ({best.scale} shot, {best.camera})"
            )
        if not dossier.audio.silent:
            print(
                "      has sound"
                + (" — sounds like talking" if dossier.audio.speechiness > 0.5 else "")
            )
        print()

    music, analysis = find_music_bed(bin_.audio)
    if music and analysis:
        print(f"  music: {music.name} at {analysis.tempo:.0f} beats per minute")
        print()
    return 0


def _run_template(args, say) -> int:
    """Read a reel's timing, or cut somebody's pictures to one already read."""
    from .insight import template as tpl
    from .scholar.library import TemplateShelf

    shelf = TemplateShelf()
    action = getattr(args, "action", "list")
    paths = list(getattr(args, "paths", []) or [])

    if action == "list":
        held = shelf.all()
        if not held:
            say.result(
                "Nothing watched yet.",
                facts=["auteur template watch <reel.mp4> reads one and keeps its timing"],
            )
            return 0
        say.result(
            f"{len(held)} " + ("reel" if len(held) == 1 else "reels") + " watched",
            facts=[t.describe() for t in held],
        )
        return 0

    if action == "watch":
        if not paths:
            say.failure("Give it a reel to watch.")
            return 2
        kept = []
        for path in paths:
            template = shelf.watch(path, name=args.name)
            if template is None:
                say.note(f"could not read {Path(path).name}")
                continue
            kept.append(template)
        if not kept:
            say.failure("None of those would open as video.")
            return 1
        say.result(
            f"Watched {len(kept)} " + ("reel" if len(kept) == 1 else "reels"),
            facts=[t.describe() for t in kept]
            + (
                ["the decode could not resolve some of this cutting — the rates are a floor"]
                if any(t.under_resolved for t in kept)
                else []
            ),
        )
        return 0

    # cut
    if len(paths) < 2:
        say.failure("Give it a template to follow and some pictures to cut.")
        return 2

    template = shelf.find(paths[0])
    if template is None:
        # Not on the shelf — maybe they handed over the reel itself.
        template = tpl.read(paths[0])
        if template is None:
            say.failure(f"No template called {paths[0]!r}, and it is not a readable reel either.")
            return 1

    words = [w.strip() for w in (args.words or "").split(",") if w.strip()]
    try:
        film = tpl.cast(
            template,
            paths[1:],
            seconds=args.seconds or None,
            words=words,
        )
    except ValueError as exc:
        say.failure(str(exc))
        return 1

    from .config import FORMATS, Settings, Workspace
    from .render import render

    workspace = Workspace(Path(args.out or "auteur-work") / "template")
    result = render(film, workspace, Settings(), formats=(FORMATS["reel"],), name=film.title)
    made = result.primary
    if made is None:
        say.failure("The render produced nothing.")
        return 1

    say.result(
        "Cut to " + template.name,
        facts=[
            f"{len(film.shots)} shots, median hold {template.shot_seconds:.3f}s",
            f"{template.cuts_per_10s:.1f} cuts per ten seconds",
            f"from {len({s.source for s in film.shots})} of your pictures",
        ],
        files=[("your film", str(made))],
    )
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
        print("      " + "".join(f"{n:<16}" for n in names[row : row + 4]))
    print()
    print("  It prefers straight cuts, and only uses these when the shots invite it.")
    print()
    return 0


def _run_workflow(args: argparse.Namespace, say: Reporter) -> int:
    """Make a post for one particular place."""
    from . import workflows

    if args.action == "list":
        print(workflows.catalogue())
        return 0

    if not args.platform:
        say.failure(
            "which place is this for?",
            'try:  auteur workflow run instagram-reel ./clips "harbour at dusk"\n'
            f"{' ' * 5}or:   auteur workflow list",
        )
        return 2
    try:
        spec = workflows.resolve(args.platform)
    except ValueError as exc:
        say.failure(str(exc), "run `auteur workflow list` to see them")
        return 2

    paths, prompt = _split_paths_and_prompt(list(args.paths), args.prompt)
    if not paths:
        say.failure("I need some footage", f'try:  auteur workflow run {spec.name} ./clips "..."')
        return 2
    if not prompt:
        say.failure(
            "I need to know what the post is about",
            f'try:  auteur workflow run {spec.name} {paths[0]} "harbour at dusk"',
        )
        return 2

    seconds = workflows.wanted_duration(spec, prompt, args.length)
    if args.length and abs(seconds - args.length) > 0.01:
        say.warn(
            f"{spec.service} {spec.surface} takes "
            f"{spec.min_seconds:.0f}-{spec.max_seconds:.0f}s — cutting to {seconds:.0f}s instead"
        )

    style = None
    if args.reference:
        from .insight import measure

        say.step("Watching the footage you want it to look like")
        style = measure(args.reference)
        for line in style.describe().splitlines():
            print(f"     {line}")
        if not style.is_agreed:
            say.warn("those references do not agree on pace — using the median of them")

    from .config import Settings

    settings = Settings(use_llm=not args.no_ai)
    if args.seed is not None:
        settings.seed = args.seed
    if style is not None and not style.is_empty:
        # Fold the measured style into the prompt the director reads before it
        # is parsed. The brief only understands words, so a measured pace has
        # to be said in them.
        prompt = f"{prompt}, {style.prompt_fragment()}"

    crew = None
    agent_result: dict = {}
    model = None
    if args.agents != "off":
        from .agents.assemble import build_crew, crew_summary

        say.step("Reading what your data says")
        model = _model_for(args, say)
        # The eye goes first. Reframing, overlay placement, joins and sound all
        # depend on knowing where the subject is, and nothing else in the crew
        # looks at a pixel.
        readings = _read_the_footage(paths, say)
        from .craft.graphics import find_stickers

        stickers = find_stickers(args.stickers)
        if stickers:
            say.detail(f"{len(stickers)} of your stickers are available to place")
        previewer = None
        if getattr(args, "look", False):
            from .agents.preview import Previewer

            previewer = Previewer()
            say.detail("the agents will render what they propose and look at it")
        crew = build_crew(
            model,
            gate=_terminal_gate(args.agents),
            readings=readings,
            spec=spec,
            style=style,
            stickers=stickers,
            previewer=previewer,
            sources=list(paths),
        )
        say.detail(
            f"{crew_summary(crew)} agents running {args.agents}"
            + (
                " — you will be asked about anything structural"
                if args.agents != "autonomous"
                else ""
            )
        )

    say.banner(f"{prompt}  ·  for {spec.service} {spec.surface}")
    try:
        deliverable, production = workflows.run(
            spec.name,
            list(paths),
            prompt,
            out=args.out,
            quality=QUALITIES.get(args.quality, args.quality),
            length=args.length,
            reporter=say,
            settings=settings,
            crew=crew,
            on_agents=lambda result: agent_result.setdefault("result", result),
        )
    except FileNotFoundError as exc:
        say.failure("I could not find any footage to edit", str(exc))
        return 1

    for warning in deliverable.warnings:
        say.warn(warning)

    queued = None
    if args.schedule:
        from .workflows.schedule import Schedule, describe_time

        root = Path(args.out) if args.out else Path.cwd() / "auteur-posts"
        queue = Schedule(Schedule.default_path(root))
        when = None if args.schedule.strip().lower() in ("next", "auto", "") else args.schedule
        try:
            queued, complaint = queue.add(deliverable, when)
        except ValueError as exc:
            say.warn(str(exc))
            queued, complaint = None, ""
        if queued is None and complaint:
            say.warn(f"not queued: {complaint}")
        elif queued is not None:
            queue.save()
            say.detail(f"queued for {describe_time(queued.when)} (id {queued.id})")

    facts = [
        f"{describe_duration(deliverable.duration)}  ·  "
        f"{deliverable.width}x{deliverable.height}  ·  "
        f"{describe_count(len(production.edl.shots), 'shot')}",
        (
            f"caption drafted, {len(deliverable.caption.hashtags)} hashtags"
            " — rewrite it before posting"
            if spec.caption_limit
            # Stories have no caption box at all; promising one there would be
            # a line of output that describes a file the run did not write.
            else "no caption box on this surface — the words go on the frame"
        ),
    ]
    if queued is not None:
        from .workflows.schedule import describe_time

        facts.append(f"queued for {describe_time(queued.when)}")

    if model is not None:
        from .agents import check_render, preflight, unknowable
        from .insight import predict

        findings = preflight(production.edl, predict(production.edl, model), model, spec=spec)
        rendered = check_render(deliverable.video)
        if rendered is not None:
            findings.append(rendered)
        for finding in findings:
            say.warn(finding.describe().replace("\n", f"\n{' ' * 5}"))
        if deliverable.folder:
            (deliverable.folder / "preflight.json").write_text(
                json.dumps(
                    {
                        "findings": [f.to_json() for f in findings],
                        "cannot_be_checked_here": unknowable(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

    crewed = agent_result.get("result")
    if crewed is not None:
        facts.insert(
            0,
            f"agents: predicted {crewed.baseline.overall:.0%} → {crewed.final.overall:.0%}"
            f" ({len(crewed.applied)} change(s) applied)",
        )
        say.blank()
        for line in crewed.describe().splitlines():
            print(f"     {line}")
        if deliverable.folder:
            (deliverable.folder / "agents.json").write_text(
                json.dumps(crewed.to_json(), indent=2), encoding="utf-8"
            )

    files = [(f"{spec.service} {spec.surface}", str(deliverable.video))]
    if deliverable.cover:
        files.append(("cover frame", str(deliverable.cover)))
    if deliverable.folder:
        files.append(("caption and manifest", str(deliverable.folder)))

    say.result(headline=f"Ready for {spec.service}", facts=facts, files=files)
    _report_standing(deliverable.video, model, say)
    if args.quiet:
        print(deliverable.video)
    return 0


def _report_standing(video, model, say: Reporter) -> None:
    """Where the finished film stands against the ones it is chasing.

    Measured off the rendered file rather than the timeline, so it is judged on
    what actually came out — the grade, the letterbox, the overlays and all —
    and on the same two yardsticks the benchmark itself was measured on.
    """
    from .insight.benchmark import Benchmarks

    marks = Benchmarks()
    if not marks.entries or video is None:
        return
    try:
        from .insight import corpus, fit
        from .insight.score import predict, timeline_of
        from .vision import read_asset

        reading = read_asset(video, samples=9)
        scored = predict(timeline_of(video), model or fit(corpus([], simulate_rows=800))).overall
        standing = marks.standing(reading, scored)
    except Exception as exc:  # noqa: BLE001 - never lose a finished film to a scoreboard
        logging.getLogger("auteur").debug("could not score against the benchmark", exc_info=True)
        say.detail(f"could not measure against the benchmark: {exc}")
        return
    if standing is None:
        return
    print()
    for line in standing.describe().splitlines():
        print(f"     {line}")


def _read_the_footage(paths: list[str], say: Reporter) -> dict:
    """Read every clip the way a picture is read, keyed by clip id.

    Clip ids are assigned by `ingest` in discovery order, so this walks the same
    order to agree with the edit. A clip that will not read is skipped rather
    than guessed at — the finishing agent proposes nothing for what it has not
    seen, which is the right answer.
    """
    from .ingest import ingest
    from .vision import read_asset

    try:
        bin_ = ingest(paths)
    except FileNotFoundError:
        return {}

    readings: dict = {}
    say.step("Looking at the frames")
    for index, asset in enumerate(bin_.visuals):
        try:
            readings[f"C{index:02d}"] = read_asset(asset.path)
        except (ValueError, OSError) as exc:
            say.warn(f"could not read {asset.name}: {exc}")
    if readings:
        from collections import Counter

        common = Counter(r.composition for r in readings.values()).most_common(1)[0]
        lit = Counter(r.lighting for r in readings.values()).most_common(1)[0]
        say.detail(f"{len(readings)} frame(s) read — mostly {common[0]}, {lit[0]}")
    return readings


def _terminal_gate(mode_name: str):
    """A gate that asks at the terminal, and refuses if nobody is there.

    `input()` on a pipe raises EOFError, which is exactly the situation where
    silently approving would be worst: an unattended run deciding on its own
    behalf. So that case is a no.
    """
    from .agents import Gate, Mode

    def ask(proposal) -> tuple[str, str]:
        print()
        print(f"  {proposal.describe()}")
        print()
        try:
            answer = input("     apply this? [y/N or a note] ").strip()
        except (EOFError, KeyboardInterrupt):
            print("     (nobody there — leaving it alone)")
            return "reject", "no reviewer at the terminal"
        if answer.lower() in ("y", "yes"):
            return "approve", ""
        if answer.lower() in ("", "n", "no"):
            return "reject", "declined"
        return "reject", answer

    return Gate(Mode(mode_name), on_ask=ask)


def _model_for(args: argparse.Namespace, say: Reporter):
    """Fit the virality model from whatever data this run was given.

    Each export is loaded separately so one unrecognised file is a warning
    rather than a dead render — a folder of exports grows over time and it
    should not be possible for a new column layout to stop you making a film.
    `auteur insight fit` still fails loudly, because there the export *is* the
    subject.
    """
    from .insight import corpus, fit, load

    usable: list[str] = []
    for export in getattr(args, "data", None) or []:
        try:
            load([export])
        except (FileNotFoundError, ValueError) as exc:
            say.warn(f"skipping {Path(export).name}: {exc}")
            continue
        usable.append(export)

    model = fit(corpus(usable, simulate_rows=0 if usable else 2000))
    say.detail(model.provenance)
    return model


def _run_score(args: argparse.Namespace, say: Reporter) -> int:
    """Score a finished video against the model.

    The first path is the video; anything else is training data. Scoring a file
    somebody else cut is the fastest way to find out whether the model agrees
    with your own eye — and where it does not, the model is the one to doubt.
    """
    from .insight import corpus, fit, predict, timeline_of

    videos = [p for p in args.paths if Path(p).suffix.lower() in (".mp4", ".mov", ".m4v", ".webm")]
    exports = [p for p in args.paths if p not in videos]
    if not videos:
        say.failure(
            "I need a video to score", "try:  auteur insight score reel.mp4 ./exports/*.csv"
        )
        return 2

    model = fit(corpus(exports, simulate_rows=0 if exports else 2000))
    say.detail(model.provenance)

    for video in videos:
        try:
            edl = timeline_of(video)
        except (ValueError, OSError) as exc:
            say.failure(f"could not read {Path(video).name}", str(exc))
            continue
        prediction = predict(edl, model)
        pace = len(edl.shots) / max(edl.duration, 1e-6) * 10

        print()
        print(f"  {Path(video).name}")
        print(
            f"     {edl.duration:.1f}s · {edl.width}x{edl.height} · "
            f"{len(edl.shots)} scenes · {pace:.1f} cuts / 10s"
        )
        print()
        print("     " + prediction.describe().replace("\n", "\n     "))
        if model.separation:
            print()
            print("     against the labelled boundary between winners and failures:")
            for name, (win, fail, cut) in sorted(model.separation.items()):
                got = {
                    "three_second_watch_rate": prediction.hook.predicted,
                    "loop_count": prediction.loop.predicted,
                }.get(name)
                if got is None:
                    continue
                verdict = "over" if got >= cut else "under"
                print(
                    f"       {name.replace('_', ' '):<24} {got:.2f} — {verdict} {cut:.2f}"
                    f"   (winners {win:.2f}, failures {fail:.2f})"
                )
        print()
        print("     read from the cut alone: no words on screen were read, and nothing")
        print("     here knows what the footage is of.")
        print()
    return 0


def _run_rehearse(args: argparse.Namespace, say: Reporter) -> int:
    """Build, measure, change, rebuild. The other loop."""
    from .insight.benchmark import Benchmarks
    from .training.rehearse import Rehearsal

    footage: list[Path] = []
    for entry in args.paths:
        path = Path(entry)
        if path.is_dir():
            footage.extend(sorted(p for p in path.iterdir() if p.is_file()))
        elif path.exists():
            footage.append(path)
    if not footage:
        say.failure("nothing to rehearse with", "point at a folder or some files")
        return 2

    marks = Benchmarks()
    target = marks.entries.get(args.against) if args.against else marks.hardest
    if target is None:
        say.warn("no benchmark to chase — add one with `auteur benchmark add <video>`")
    else:
        say.detail(
            f"chasing {target.name}: craft {target.craft.overall:.2f}, "
            f"structure {target.structure:.2f}"
        )

    loop = Rehearsal(
        footage,
        benchmark=target,
        seconds=max(3.0, args.length),
        seed=args.seed if args.seed is not None else 0xB0A7,
    )

    def report(attempt, progress) -> None:
        best = progress.best is attempt
        flag = "  ← best" if best else ""
        if attempt.beat_target:
            flag = "  ← PASSED THE TARGET, bar raised"
        print(
            f"     gen {attempt.generation:3}  {attempt.recipe.preset:13} "
            f"craft {attempt.craft:.3f}  structure {attempt.structure:.3f}"
            f"  combined {attempt.combined:.3f}{flag}",
            flush=True,
        )

    say.banner(
        f"rehearsing on {len(footage)} file(s)"
        + (
            "  ·  forever, ctrl-c to stop"
            if args.forever
            else f"  ·  {args.generations} generations"
        )
    )
    try:
        if args.forever:
            loop.forever(on_generation=lambda a, p: None)
        else:
            loop.run(generations=args.generations, on_generation=report)
    except KeyboardInterrupt:
        say.detail("stopped")

    print()
    for line in loop.progress.describe().splitlines():
        print(f"     {line}")
    ceiling = loop.ceiling()
    if ceiling:
        say.warn(ceiling)
    say.detail(f"the winning settings are saved to {loop.recipe_path}")
    return 0


def _run_benchmark(args: argparse.Namespace, say: Reporter) -> int:
    """The films the work is chasing, and what it would take to pass them."""
    import json as _json

    from .insight.benchmark import Benchmarks, measure_benchmark

    marks = Benchmarks()

    if args.action == "add":
        if not args.paths:
            say.failure("point at a video", "auteur benchmark add ./thegoal.mp4")
            return 2
        from .insight.benchmark import combine

        measured = []
        for entry in args.paths:
            # `handle=./file.mp4` names a reel. Without it a composite goal
            # reports its parts as upload UUIDs, which makes "separation set by
            # 3a41839d" useless as an instruction to go and look at one.
            label, _, raw = entry.rpartition("=")
            target = Path(raw or entry)
            if not target.exists():
                say.warn(f"{target.name} is not there")
                continue
            say.step(f"Watching {label or target.name}")
            measured.append(measure_benchmark(target, name=label))

        if not measured:
            say.failure("nothing could be measured", "check the paths")
            return 1

        # Several reels named together are one goal, not several. The bar each
        # dimension is set at is the best any of them managed, so the target is
        # harder than every reel in it — which is what "reach and then surpass"
        # has to mean once there is more than one thing to reach.
        if len(measured) > 1 and args.name:
            benchmark = marks.add(combine(measured, name=args.name))
            for line in benchmark.describe().splitlines():
                print(f"     {line}")
            if benchmark.led_by:
                print()
                for dimension, who in sorted(benchmark.led_by.items()):
                    print(f"     {dimension:12} bar set by {who}")
            return 0

        for index, benchmark in enumerate(measured):
            if args.name and len(measured) == 1:
                benchmark.name = args.name
            marks.add(benchmark)
            for line in benchmark.describe().splitlines():
                print(f"     {line}")
            if index < len(measured) - 1:
                print()
        return 0

    if args.action == "remove":
        for name in args.paths:
            say.result(f"dropped {name}" if marks.remove(name) else f"no benchmark called {name}")
        return 0

    if args.json:
        print(_json.dumps([b.to_json() for b in marks.entries.values()], indent=2))
        return 0

    for line in marks.describe().splitlines():
        print(line)
    if marks.entries:
        say.detail(
            "structure is the same model that scores your edits; craft is measured "
            "off the frames. Both have to be beaten for it to count."
        )
    return 0


def _run_agents(args: argparse.Namespace, say: Reporter) -> int:
    """What the crew has found to be worth doing, across every film so far."""
    import json as _json

    from .agents.ledger import Ledger

    ledger = Ledger()
    if args.action == "forget":
        if ledger.path.exists():
            ledger.path.unlink()
        say.result("the crew has forgotten everything it learned")
        return 0

    if args.json:
        print(
            _json.dumps(
                {
                    "proven": [t.__dict__ for t in ledger.proven()],
                    "wasted": [t.__dict__ for t in ledger.wasted()],
                },
                indent=2,
            )
        )
        return 0

    for line in ledger.describe().splitlines():
        print(line)
    say.detail(
        "these are the scoring model's own verdicts, not view counts — "
        "load real exports with `auteur insight fit` to check them against reality"
    )
    return 0


def _scholar_scroll(args: argparse.Namespace, say: Reporter, text: str) -> int:
    """Be served reels, watch every one, and report what arrived in what order.

    The order is the finding. Nothing here reads an article about a ranking;
    it looks at what was put in front of it first and what was put in front of
    it tenth, and says the difference — for this session, on this account,
    which is exactly as much as one session is worth.
    """
    from .scholar.feed import LocalFeed, ScrollHistory, YouTubeFeed, learnings_from, scroll
    from .scholar.knowledge import KnowledgeStore

    wanted = (getattr(args, "feed", "") or "youtube").strip()
    if wanted.lower() in ("youtube", "yt"):
        feed = YouTubeFeed()
    else:
        feed = LocalFeed(wanted)

    ok, why = feed.reachable()
    if not ok:
        say.failure(f"Cannot scroll {feed.name}: {why}")
        say.detail("`--feed <folder>` scrolls a folder of reels instead, in order.")
        return 1

    say.step(f"scrolling {feed.name}" + (f" for {text!r}" if text else ""))
    session = scroll(feed, text, count=max(2, int(getattr(args, "videos", 5) or 5)))

    if session.unreachable and not session.servings:
        say.failure(f"The scroll stopped: {session.unreachable}")
        return 1

    kept = ScrollHistory().keep(session)
    store = KnowledgeStore()
    added = sum(1 for learning in learnings_from(session) if store.add(learning))

    facts = [
        f"{x.position + 1:2d}.  {x.seconds:5.1f}s   {x.cuts_per_10s:5.1f} cuts/10s   "
        f"hold {x.shot_seconds:.3f}s   hook {x.hook:.2f}s"
        for x in session.servings
    ]
    facts.append("")
    facts.extend(session.what_it_served())
    if session.unreachable:
        facts.append(f"(it stopped early: {session.unreachable})")
    if feed.name == "library":
        facts.append(
            "a folder has no ranking in it, so this measures your own library "
            "rather than anybody's feed"
        )

    say.result(
        f"Watched {session.watched} from {feed.name}",
        facts=facts,
        files=[("the session", str(kept))],
    )
    if added:
        say.detail(f"{added} kept as learnings — tentative, because one session is one voice")
    return 0


def _scholar_sources(scholar, say: Reporter) -> None:
    """Where what it knows came from, and how old the outside numbers are.

    An audit of the live store found 127 learnings and not one from outside
    this repository — every one of them measured off its own reels, read out of
    its own markdown, or concluded over those. The confidence ladder counts
    independent channels and there was only ever one, so nothing could ever
    climb it and nothing said so. This puts the mix on the screen, because a
    store that only agrees with itself looks exactly like a store that knows a
    lot until somebody counts the channels.
    """
    import datetime
    from collections import Counter

    from .scholar.published import stale

    rows = scholar.knowledge._learnings
    if not rows:
        return

    kinds = Counter(row.source_channel.split(":")[0] for row in rows)
    inside = sum(n for k, n in kinds.items() if k in ("local", "film", "across", "scroll"))
    outside = sum(n for k, n in kinds.items() if k in ("published", "corroborate", "yt"))

    say.detail(f"sources: {', '.join(f'{k} {n}' for k, n in kinds.most_common())}")
    if not outside:
        say.detail(
            "all of it from inside this project — nothing has corroborated it. "
            "run `auteur scholar study` to take in the published measurements"
        )
    else:
        say.detail(f"{inside} learned here, {outside} from outside")

    old = stale(datetime.date.today().year)
    if old:
        which = ", ".join(f"{s.key} ({s.measured})" for s in old)
        say.detail(f"outside numbers worth re-checking: {which}")


def _run_scholar(args: argparse.Namespace, say: Reporter) -> int:
    """The study agent: what it knows, what it wants to watch, what it teaches."""
    import json as _json

    from .scholar import Scholar
    from .scholar.youtube import YouTubeUnavailable, reachable

    scholar = Scholar()
    text = " ".join(args.words).strip()

    if args.action == "scroll":
        return _scholar_scroll(args, say, text)

    if args.action == "status":
        if args.json:
            print(_json.dumps(scholar.status(), indent=2))
            return 0
        for line in scholar.describe().splitlines():
            print(line)
        can_study, how = reachable()
        say.detail(f"YouTube: {how}" if can_study else f"cannot study — {how}")
        _scholar_sources(scholar, say)
        return 0

    if args.action == "subscribe":
        if len(args.words) < 1:
            say.failure("give me a channel id", "auteur scholar subscribe UC123... Channel Name")
            return 2
        channel_id, *name = args.words
        scholar.youtube.subscribe(channel_id, " ".join(name) or channel_id, ["content_creation"])
        scholar.youtube.save_subscriptions()
        say.result(f"following {' '.join(name) or channel_id}")
        return 0

    if args.action == "ask":
        if not text:
            say.failure("ask me something", 'auteur scholar ask "how do I pace a montage?"')
            return 2
        reply = scholar.chat(text)
        print(reply.text)
        return 0

    if args.action == "teach":
        brief = scholar.teach(args.agent) if args.agent else scholar.teach_all()
        if args.json:
            print(_json.dumps(brief.to_json(), indent=2))
            return 0
        print(brief.summary)
        # The consensus when several films agree, the raw learnings when they
        # do not — a list of "abc123.mp4 measures 0.034" teaches nobody.
        if brief.consensus:
            for line in brief.consensus:
                print(f"  · {line}")
        else:
            for learning in brief.learnings[:12]:
                print(f"  · {learning.insight}")
        if not brief.learnings:
            say.detail("nothing studied yet — run `auteur scholar study <topic>`")
        return 0

    if args.action == "read":
        # No network needed: the material that matters most is usually already
        # on the disk — the notes about how this works, and the reels it is
        # measured against.
        roots = args.study_from or ["docs", "."]
        say.step(f"Reading {', '.join(roots)}")
        session = scholar.study_files(roots)
        say.result(
            f"{session.videos_watched} file(s) studied, "
            f"{session.learnings_extracted} learning(s) kept"
        )
        if session.learnings_extracted == 0:
            say.detail("nothing new — everything there had already been read")
        return 0

    if args.action == "critique":
        # A finished film rather than an edit decision list: the timeline is
        # recovered from the cuts, which is the same reconstruction a benchmark
        # uses, and it means this works on anything — including somebody
        # else's reel.
        if not args.words:
            say.failure("no film to critique", "auteur scholar critique <film.mp4>")
            return 2
        source = Path(args.words[0])
        if not source.is_file():
            say.failure(f"no such file: {source}")
            return 2

        from .insight.score import timeline_of

        try:
            edl = timeline_of(source)
        except Exception as exc:  # noqa: BLE001 - unreadable media, said plainly
            say.failure("that film could not be read", str(exc))
            return 2

        findings = scholar.critique(edl)
        if args.json:
            print(_json.dumps([f.to_json() for f in findings], indent=2))
            return 0
        if not findings:
            say.result("nothing to say — this matches what it has studied")
            return 0
        say.step(f"{len(findings)} thing(s) the studied films do differently")
        for finding in findings:
            say.detail(f"[{finding.severity}] {finding.description}")
            say.detail(f"    → {finding.suggestion}")
        return 0

    if args.action == "watch":
        can_study, how = reachable()
        if not can_study:
            say.failure("the Scholar cannot reach YouTube", how)
            return 1
        minutes = max(1.0, args.every)
        say.banner(f"the Scholar is studying · checking every {minutes:.0f} min · ctrl-c to stop")

        def report(session) -> None:
            say.result(
                f"{session.videos_watched} video(s), "
                f"{session.learnings_extracted} learning(s) — "
                f"{scholar.knowledge.total_learnings} known"
            )

        try:
            scholar.run_forever(
                every_seconds=minutes * 60.0, max_videos=args.videos, on_session=report
            )
        except KeyboardInterrupt:
            say.detail(f"stopped — {scholar.knowledge.total_learnings} learnings kept")
        return 0

    # study
    can_study, how = reachable()
    if not can_study:
        say.failure("the Scholar cannot reach YouTube", how)
        return 1
    say.step(f"Studying{f' {text}' if text else ''}")
    try:
        session = scholar.study(max_videos=max(1, args.videos))
    except YouTubeUnavailable as exc:
        say.failure("the Scholar could not study", str(exc))
        return 1
    say.result(
        f"{session.videos_watched} video(s) watched, {session.learnings_extracted} learning(s) kept"
    )
    if session.videos_watched == 0:
        say.detail("nothing new — everything it found had already been watched")
    return 0


def _run_insight(args: argparse.Namespace, say: Reporter) -> int:
    """What the performance data says about hooks, shares and loops."""
    from .insight import corpus, fit, load, simulate, write_csv

    if args.action == "simulate":
        rows = simulate(max(1, args.rows))
        destination = Path(args.out) if args.out else Path.cwd() / "auteur-practice-data.csv"
        write_csv(rows, destination)
        say.result(
            headline="Practice data written",
            facts=[
                f"{len(rows)} simulated rows — every id starts `sim_`",
                "invented numbers for rehearsing on, not observations of anything",
            ],
            files=[("the corpus", str(destination))],
        )
        return 0

    if args.action == "score":
        return _run_score(args, say)

    try:
        measured = load(args.paths) if args.paths else []
        model = fit(corpus(args.paths, simulate_rows=max(0, args.rows)))
    except (FileNotFoundError, ValueError) as exc:
        say.failure("I could not read that export", str(exc))
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "provenance": model.provenance,
                    "rows": model.rows,
                    "measured_rows": model.measured_rows,
                    "simulated_rows": model.simulated_rows,
                    "elite_three_second": round(model.elite_three_second, 4),
                    "elite_share": round(model.elite_share, 4),
                    "elite_loop": round(model.elite_loop, 4),
                    "best_hook_duration": round(model.best_hook_duration, 3),
                    "style_ranking": [
                        [name, round(value, 4)] for name, value in model.style_ranking
                    ],
                },
                indent=2,
            )
        )
        return 0

    print()
    print("  " + model.describe().replace("\n", "\n  "))
    print()
    if measured:
        print("  your posts, most amplified first:")
        print()
        for signal in sorted(measured, key=lambda s: -s.amplification)[:8]:
            source = "exported" if signal.has("three_second_watch_rate") else "derived "
            print(
                f"      {signal.post_id:<8} {signal.form:<17} "
                f"3s {signal.three_second_watch_rate:.2f} ({source})  "
                f"amp {signal.amplification:.3f}   {signal.hook[:36]}"
            )
        print()
    print("  targets:   hook 0.80 at three seconds  ·  share 0.05 of views  ·  loop 1.5 plays")
    print()
    return 0


def _library_for(args: argparse.Namespace, paths: list[str]) -> object:
    """Open the index a `media` command should use.

    Defaults to one beside the folder being scanned, which is what makes
    `auteur media scan ./footage` need no second argument and no state
    anywhere else on the machine.
    """
    from .workflows.library import Library

    if args.index:
        return Library(args.index)
    root = Path(paths[0]) if paths else Path.cwd()
    if root.is_file():
        root = root.parent
    return Library(Library.default_path(root))


def _run_media(args: argparse.Namespace, say: Reporter) -> int:
    """The media manager."""
    paths = [str(path) for path in args.paths]
    library = _library_for(args, paths)

    if args.action == "scan":
        if not paths:
            say.failure("which folder?", "try:  auteur media scan ./footage")
            return 2
        say.step("Looking through your footage")
        try:
            report = library.scan(paths)
        except FileNotFoundError as exc:
            say.failure("I could not find that", str(exc))
            return 1
        library.save()

        say.detail(report.describe().replace("\n", f"\n{' ' * 5}"))
        for copy, original in report.duplicates:
            say.found("copy", f"{copy.name} is the same file as {original.name}")
        for entry in report.unreadable:
            say.warn(f"could not open {entry.name} — {entry.why_unreadable}")
        facts = [library.describe()]
        if report.seconds >= 1.0:
            facts.append(f"took {describe_duration(report.seconds)}")
        say.result(headline="Indexed", facts=facts, files=[("the index", str(library.path))])
        return 0

    if not library.entries:
        say.failure("nothing indexed yet", "try:  auteur media scan ./footage")
        return 1

    if args.action == "duplicates":
        groups = library.duplicate_groups()
        print()
        if not groups:
            print("  no duplicates — every file in the index is its own footage")
            print()
            return 0
        from .workflows.library import describe_bytes

        wasted = sum(entry.size for group in groups for entry in group[1:])
        print(
            f"  {describe_count(len(groups), 'duplicated file')}, "
            f"{describe_bytes(wasted)} of copies"
        )
        print()
        for group in groups:
            print(f"      {group[0].summary()}")
            for copy in group[1:]:
                print(f"          also at  {copy.path}")
            print()
        print("  Nothing has been deleted. These are the copies; you choose.")
        print()
        return 0

    if args.action == "tag":
        if not paths or not args.label:
            say.failure("what, and what with?", "try:  auteur media tag ./clip.mp4 --label keepers")
            return 2
        touched = library.tag(paths, args.label)
        library.save()
        print(f"\n  tagged {describe_count(touched, 'file')} as {args.label!r}\n")
        return 0

    entries = library.pick(kind=args.kind)
    if args.json:
        print(json.dumps([entry.to_json() for entry in entries], indent=2))
        return 0

    print()
    print(f"  {library.describe()}")
    print(f"  index: {library.path}")
    print()
    for entry in entries:
        tags = f"   [{', '.join(entry.tags)}]" if entry.tags else ""
        print(f"      {entry.summary()}{tags}")
    print()
    return 0


def _run_schedule(args: argparse.Namespace, say: Reporter) -> int:
    """The posting queue."""
    from .workflows.schedule import Schedule

    root = Path(args.out) if args.out else Path.cwd() / "auteur-posts"
    queue = Schedule(Schedule.default_path(root))
    if args.gap is not None:
        queue.gap_hours = max(0.0, args.gap)
    if args.per_day is not None:
        queue.per_day = max(1, args.per_day)

    if args.action == "export":
        print(queue.export_csv(), end="")
        return 0

    if args.action in ("done", "skip", "remove"):
        if not args.post_id:
            say.failure("which post?", "run `auteur schedule` to see the ids")
            return 2
        if args.action == "remove":
            changed = queue.remove(args.post_id)
        else:
            changed = queue.mark(args.post_id, "posted" if args.action == "done" else "skipped")
        if not changed:
            say.failure(f"no post with id {args.post_id!r}")
            return 1
        queue.save()
        print(f"\n  {args.post_id} marked {args.action}\n")
        return 0

    if args.action == "tidy":
        gone = queue.forget_missing()
        queue.save()
        print(f"\n  dropped {describe_count(len(gone), 'post')} whose film is no longer there\n")
        return 0

    posts = queue.due() if args.action == "due" else sorted(queue.posts, key=lambda p: p.when)
    print()
    print(f"  {queue.describe()}")
    print(f"  queue: {queue.path}")
    print()
    if not posts:
        print(
            "      nothing due now"
            if args.action == "due"
            else "      nothing queued — `auteur workflow run ... --schedule next` adds one"
        )
        print()
        return 0
    for post in posts:
        print(f"      {post.id}  {post.describe()}")
    print()
    if args.action == "due":
        print("  Post these, then:  auteur schedule done <id>")
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
        if args.command == "account":
            return _run_account(args, say)
        if args.command == "moderate":
            return _run_moderate(args, say)
        if args.command == "analyse":
            return _run_analyse(args, say)
        if args.command == "looks":
            return _run_looks()
        if args.command == "template":
            return _run_template(args, say)
        if args.command == "workflow":
            return _run_workflow(args, say)
        if args.command == "media":
            return _run_media(args, say)
        if args.command == "schedule":
            return _run_schedule(args, say)
        if args.command == "insight":
            return _run_insight(args, say)
        if args.command == "scholar":
            return _run_scholar(args, say)
        if args.command == "agents":
            return _run_agents(args, say)
        if args.command == "benchmark":
            return _run_benchmark(args, say)
        if args.command == "rehearse":
            return _run_rehearse(args, say)
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
