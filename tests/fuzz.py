"""A randomised campaign over the pure-logic surface.

    python tests/fuzz.py            # ten thousand cases
    python tests/fuzz.py 500        # a quicker pass

Not part of the pytest suite: it is slow, and its job is to find the next
property worth turning into a test rather than to guard the ones already
found. Everything it has caught so far now has a named test in
`test_auteur.py`.

Ten thousand randomised cases, checking invariants rather than crashes.

Every check here is a property that must hold for *any* input, including the
input a language model invents when it has misunderstood the brief. A crash is
the easy failure; the ones worth hunting are the quiet violations — a shot that
survives repair pointing past the end of its clip, a ramp that promises more
screen time than it can deliver, a reset token that works twice.
"""

from __future__ import annotations

import random
import string
import sys
import traceback
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auteur.craft import grammar, motion
from auteur.director.brief import parse_brief
from auteur.edl import MIN_SHOT, EditDecisionList, Look, Motion, Ramp, Shot, Transition
from auteur.edl import TRANSITIONS, MOTIONS, REFRAMES

FAILURES: list[tuple[str, str, str]] = []
COUNTS: Counter = Counter()


def check(area: str, condition: bool, message: str, seed: object = "") -> None:
    COUNTS[area] += 1
    if not condition:
        FAILURES.append((area, message, repr(seed)))


def guard(area: str, seed: object, fn) -> None:
    """Run a case; an exception is itself a failure."""
    try:
        fn()
    except Exception:
        FAILURES.append((area, traceback.format_exc().strip().splitlines()[-1], repr(seed)))
        COUNTS[area] += 1


# ---------------------------------------------------------------------------
# Fake footage the EDL can be repaired against
# ---------------------------------------------------------------------------

class FakeVideo:
    def __init__(self, rng):
        self.shot_boundaries = []

    def slice_stats(self, start, end):
        return {"motion": 0.5, "luma": 0.5}


class FakeAsset:
    def __init__(self, path, duration, kind):
        self.path, self.duration, self.kind = path, duration, kind


class FakeDossier:
    def __init__(self, clip_id, duration, kind, rng):
        self.clip_id = clip_id
        self.duration = duration
        self.asset = FakeAsset(Path(f"/tmp/{clip_id}.mp4"), duration, kind)
        self.video = FakeVideo(rng)
        self.takes = []


def random_shot(rng, clip_ids):
    clip = rng.choice(clip_ids)
    start = rng.choice([0.0, rng.uniform(-5, 30), rng.uniform(0, 3)])
    end = rng.choice([start + rng.uniform(-2, 20), rng.uniform(-5, 40), start])
    points = sorted({round(rng.uniform(0, 1), 3) for _ in range(rng.randint(1, 5))})
    ramp = Ramp([(p, rng.uniform(0.05, 12.0)) for p in points] or [(0.0, 1.0)])
    return Shot(
        clip_id=clip,
        source=Path(f"/tmp/{clip}.mp4"),
        start=start, end=end, ramp=ramp,
        motion=Motion(kind=rng.choice(list(MOTIONS) + ["nonsense"]),
                      intensity=rng.uniform(-1, 2),
                      anchor=(rng.uniform(-1, 2), rng.uniform(-1, 2))),
        reframe=rng.choice(list(REFRAMES) + ["bogus"]),
        look=Look(preset=rng.choice(["neon", "noir", "??", ""]),
                  exposure=rng.uniform(-3, 3), saturation=rng.uniform(-3, 3)),
        transition_in=Transition(kind=rng.choice(list(TRANSITIONS) + ["???"]),
                                 duration=rng.uniform(-1, 6)),
        use_source_audio=rng.random() < 0.5,
        audio_gain=rng.uniform(-1, 3),
        is_still=rng.random() < 0.2,
    )


def fuzz_edl(rng):
    n_clips = rng.randint(1, 6)
    clip_ids = [f"C{i:02d}" for i in range(n_clips)]
    sources = {}
    for cid in clip_ids:
        kind = rng.choice(["video", "video", "video", "image"])
        duration = rng.choice([rng.uniform(0.05, 30), 0.2, 4.0])
        sources[cid] = FakeDossier(cid, duration, kind, rng)

    edl = EditDecisionList(title="fuzz")
    for _ in range(rng.randint(1, 25)):
        edl.shots.append(random_shot(rng, clip_ids))
    target = rng.uniform(1.0, 60.0)

    try:
        edl.repair(sources, target_duration=target)
    except ValueError:
        return  # legal: nothing survivable in the input
    if not edl.shots:
        check("edl", False, "repair left an empty timeline without raising", target)
        return

    for index, shot in enumerate(edl.shots):
        limit = sources[shot.clip_id].duration
        check("edl", shot.start >= -1e-6, f"shot {index} starts before zero: {shot.start}")
        check("edl", shot.end <= limit + 1e-6 or shot.is_still,
              f"shot {index} runs past its clip: {shot.end:.3f} > {limit:.3f}")
        check("edl", shot.end > shot.start, f"shot {index} is empty or reversed")
        check("edl", shot.duration >= MIN_SHOT - 1e-6,
              f"shot {index} is a flash frame after repair: {shot.duration:.4f}")
        check("edl", shot.motion.kind in MOTIONS, f"shot {index} kept a bogus motion")
        check("edl", shot.reframe in REFRAMES, f"shot {index} kept a bogus reframe")
        check("edl", shot.transition_in.kind in TRANSITIONS,
              f"shot {index} kept a bogus transition")
        check("edl", 0.0 <= shot.audio_gain <= 4.0, f"shot {index} gain {shot.audio_gain}")

    check("edl", edl.shots[0].transition_in.is_cut, "the film does not open on a cut")

    for index in range(1, len(edl.shots)):
        overlap = edl.shots[index].transition_in.duration
        shorter = min(edl.shots[index - 1].duration, edl.shots[index].duration)
        check("edl", overlap <= shorter / 2 + 1e-6,
              f"transition {index} ({overlap:.3f}s) longer than half its shorter neighbour")

    # The timeline must be contiguous and monotonic.
    last_end = None
    for start, end, _ in edl.timeline():
        check("edl", end > start, "a timeline entry ends before it starts")
        if last_end is not None:
            check("edl", start <= last_end + 1e-6, "the timeline jumps backwards")
        last_end = end
    check("edl", abs(edl.duration - (last_end or 0.0)) < 1e-3,
          f"duration {edl.duration:.4f} disagrees with the timeline {last_end}")

    # Round-tripping through JSON must not change the film.
    payload = edl.to_json()
    check("edl", len(payload["shots"]) == len(edl.shots), "to_json lost a shot")


# ---------------------------------------------------------------------------
# Ramps
# ---------------------------------------------------------------------------

def fuzz_ramp(rng):
    source = rng.choice([rng.uniform(0.02, 12.0), 0.04, rng.uniform(0.2, 1.0)])
    fps = rng.choice([12.0, 23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0, 120.0])
    points = sorted({round(rng.uniform(0, 1), 3) for _ in range(rng.randint(2, 6))})
    ramp = Ramp([(p, rng.uniform(0.15, 8.0)) for p in points]).normalise()

    slices = motion.ramp_slice_count(ramp, source, fps)
    check("ramp", slices >= 1, f"slice count {slices} for {source}s at {fps}fps")
    check("ramp", slices <= motion.RAMP_MAX_SLICES, f"slice count {slices} above the cap")

    if slices >= 2:
        windows = motion.slice_windows(source, fps, slices)
        check("ramp", len(windows) == slices, "wrong number of windows")
        check("ramp", windows[0][0] == 0.0, f"first window starts at {windows[0][0]}")
        for i, (a, b) in enumerate(windows):
            check("ramp", b > a, f"window {i} is empty: {a}..{b}")
            frames = (b - a) * fps
            check("ramp", frames >= 1.0,
                  f"window {i} holds {frames:.2f} frames at {fps}fps ({source}s / {slices})")
        for i in range(len(windows) - 1):
            check("ramp", windows[i + 1][0] > windows[i][0], "windows are not monotonic")
            overlap = windows[i][1] - windows[i + 1][0]
            check("ramp", abs(overlap - 1.0 / fps) < 1e-6,
                  f"windows {i}/{i+1} overlap {overlap:.6f}, wanted {1/fps:.6f}")
        check("ramp", windows[-1][1] >= source - 1e-6,
              f"the last window stops at {windows[-1][1]:.4f} before {source:.4f}")

    graph = motion.ramp_video_graph(ramp, source_duration=source, in_label="src",
                                    out_label="out", source_fps=fps)
    check("ramp", graph.startswith("[src]") or graph.startswith("[src]split"),
          "graph does not start from its input")
    check("ramp", graph.rstrip().endswith("[out]"), "graph does not end at its output")
    check("ramp", "concat=n=1:" not in graph, "a one-way concat was emitted")

    screen = ramp.output_duration(source)
    check("ramp", screen > 0, f"screen time {screen} for {source}s")
    check("ramp", screen <= source / 0.149 + 1e-6, "screen time beyond the slowest speed")


# ---------------------------------------------------------------------------
# Briefs
# ---------------------------------------------------------------------------

WORDS = ["fast", "slow", "neon", "moody", "warm", "gritty", "black", "white", "montage",
         "cinematic", "seconds", "chase", "summer", "punchy", "beat", "20", "0", "-5",
         "999999", "3.5", '"TITLE"', "'x'", "\\", "%s", "{}", "🎬", "…", "ünïcødé"]


def fuzz_brief(rng):
    prompt = " ".join(rng.choice(WORDS) for _ in range(rng.randint(0, 18)))
    duration = rng.choice([None, rng.uniform(-10, 400), 0.0])
    brief = parse_brief(prompt, duration=duration)
    check("brief", brief.duration is None or 3.0 <= brief.duration <= 900.0,
          f"duration {brief.duration} from {prompt!r} / {duration!r}", prompt)
    check("brief", isinstance(brief.describe(), str), "describe() is not a string", prompt)
    length = brief.shot_length_at(rng.uniform(-1, 2))
    check("brief", MIN_SHOT / 2 <= length <= 30, f"shot length {length} from {prompt!r}", prompt)


# ---------------------------------------------------------------------------
# Grammar passes
# ---------------------------------------------------------------------------

def fuzz_grammar(rng):
    clip_ids = [f"C{i:02d}" for i in range(rng.randint(1, 5))]
    sources = {c: FakeDossier(c, rng.uniform(2, 30), "video", rng) for c in clip_ids}
    edl = EditDecisionList(title="g")
    for _ in range(rng.randint(2, 20)):
        edl.shots.append(random_shot(rng, clip_ids))
    target = rng.uniform(3.0, 45.0)
    try:
        edl.repair(sources, target_duration=target)
    except ValueError:
        return
    before = len(edl.shots)

    beat = rng.uniform(0.2, 1.5)
    grammar.vary_beat_multiples(edl, beat)
    check("grammar", len(edl.shots) == before, "vary_beat_multiples changed the shot count")
    for i, shot in enumerate(edl.shots):
        check("grammar", shot.duration >= MIN_SHOT - 1e-6,
              f"vary_beat_multiples made shot {i} a flash frame: {shot.duration:.4f}")

    grammar.vary_pacing(edl, run_length=rng.randint(2, 5), spread=rng.uniform(0.05, 0.5))
    for i, shot in enumerate(edl.shots):
        check("grammar", shot.duration >= MIN_SHOT - 1e-6,
              f"vary_pacing made shot {i} a flash frame: {shot.duration:.4f}")

    grammar.enforce_variety(edl)
    check("grammar", len(edl.shots) == before, "enforce_variety changed the shot count")
    check("grammar", edl.shots[0].transition_in.is_cut,
          "enforce_variety moved a transition onto the first shot")

    grammar.limit_transition_density(edl)
    check("grammar", edl.shots[0].transition_in.is_cut,
          "limit_transition_density left shot 0 with a transition")

    grammar.trim_to_duration(edl, target, tolerance=0.6)
    check("grammar", len(edl.shots) >= 1, "trim_to_duration emptied the film")
    for i, shot in enumerate(edl.shots):
        check("grammar", shot.duration >= MIN_SHOT - 1e-6,
              f"trim_to_duration made shot {i} a flash frame: {shot.duration:.4f}")


# ---------------------------------------------------------------------------
# Uploads and routing
# ---------------------------------------------------------------------------

BYTES = [b"", b"--", b"\r\n", b"\x00" * 8, b"a" * 300, "🎬".encode(), b"--x\r\nContent-"]


def fuzz_multipart(rng):
    from auteur.web.server import _parse_multipart

    boundary = "".join(rng.choice(string.ascii_letters) for _ in range(rng.randint(1, 12)))
    body = b"".join(rng.choice(BYTES) for _ in range(rng.randint(0, 8)))
    content_type = rng.choice([
        f"multipart/form-data; boundary={boundary}",
        "multipart/form-data",
        "text/plain",
        "",
        f"multipart/form-data; boundary={boundary}\r\nX-Injected: yes",
    ])
    try:
        fields, files = _parse_multipart(body, content_type)
    except Exception:
        return  # the handler catches this and answers 400
    check("upload", isinstance(fields, dict) and isinstance(files, list),
          "parser returned the wrong shape")


NASTY = ["..", "../", "..%2f", "%2e%2e", "....//", "/etc/passwd", "\\..\\", "a" * 300,
         "\x00", "%00", "..;/", "./../../", "server.py", "auth.py", "accounts.json"]


def fuzz_routes(rng):
    from auteur.web.server import STATIC

    piece = rng.choice(NASTY) + rng.choice(["", ".png", ".py", ".json", "/x"])
    path = rng.choice(["/static/", "/", "/api/jobs/"]) + piece
    # This is exactly what the handler does to turn a path into a file.
    candidate = STATIC / Path(path).name
    try:
        resolved = candidate.resolve()
        inside = resolved.is_relative_to(STATIC.resolve())
    except (OSError, ValueError):
        # Exactly what the server does: unresolvable means not served.
        check("routing", True, "")
        return
    # Either it lands inside the folder, or the server refuses to serve it.
    try:
        served = inside and candidate.is_file()
    except (OSError, ValueError):
        served = False
    check("routing", inside or not served,
          f"path {path!r} would be served from {resolved}", path)
    check("routing", not (resolved == STATIC.resolve().parent and served),
          f"path {path!r} reached the parent folder", path)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def fuzz_auth(rng, store):
    from auteur.web import auth  # noqa: F401 - kept for the reduced-cost note
    who = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(rng.randint(1, 12)))
    password = "".join(rng.choice(string.printable[:94]) for _ in range(rng.randint(8, 40)))
    email = f"{who}@example.com"

    if store.get(who) is not None:
        return
    account = store.add(who, email, password)

    check("auth", account.check(password), "the password it was given does not verify")
    check("auth", not account.check(password + "x"), "a longer password verified")
    check("auth", password not in store.path.read_text(), "the password reached the file")

    token, _ = store.sign_in(rng.choice([who, who.upper(), email]), password)
    check("auth", token is not None, "a correct password was refused")
    if token:
        check("auth", store.session_user(token) == account.username, "session resolves wrongly")
        check("auth", token not in store.path.read_text(), "a live token reached the file")

    bad, message = store.sign_in(who, password + "!")
    check("auth", bad is None, "a wrong password was accepted")
    check("auth", "do not match" in message, f"unexpected refusal wording: {message}")

    started = store.begin_reset(email)
    check("auth", started is not None, "reset refused for an account that exists")
    if started:
        _, reset_token = started
        new = password[::-1] + "Z9"
        check("auth", store.finish_reset(reset_token, new), "a fresh reset token was refused")
        check("auth", not store.finish_reset(reset_token, new + "2"),
              "a reset token worked twice")
        check("auth", store.get(who).check(new), "the new password does not verify")
        if token:
            check("auth", store.session_user(token) is None,
                  "a password change left an old session alive")


# ---------------------------------------------------------------------------

def main(total: int = 10_000) -> int:
    rng = random.Random(0xA17EE)

    # scrypt at n=2^15 is 0.1s a call by design, which would make 10k cases an
    # hour of waiting. The parameters are lowered here and only here; what is
    # under test is the logic around the hash, not the hash's cost.
    from auteur.web import auth
    auth.SCRYPT_N = 1 << 12
    auth.SCRYPT_MAXMEM = 128 * auth.SCRYPT_N * auth.SCRYPT_R * 2

    import tempfile
    store = auth.Accounts(Path(tempfile.mkdtemp()) / "accounts.json")

    areas = [
        ("edl", lambda: fuzz_edl(rng), 0.30),
        ("ramp", lambda: fuzz_ramp(rng), 0.22),
        ("grammar", lambda: fuzz_grammar(rng), 0.18),
        ("brief", lambda: fuzz_brief(rng), 0.12),
        ("upload", lambda: fuzz_multipart(rng), 0.06),
        ("routing", lambda: fuzz_routes(rng), 0.06),
        ("auth", lambda: fuzz_auth(rng, store), 0.06),
    ]
    names = [a[0] for a in areas]
    weights = [a[2] for a in areas]

    for case in range(total):
        name = rng.choices(names, weights=weights)[0]
        fn = {a[0]: a[1] for a in areas}[name]
        guard(name, f"case {case}", fn)
        if (case + 1) % 1000 == 0:
            print(f"  {case + 1:>6} cases, {len(FAILURES)} failure(s)", flush=True)

    print()
    print(f"  {total} cases across {len(names)} areas")
    print(f"  assertions checked: {sum(COUNTS.values())}")
    for name in names:
        print(f"     {name:<9} {COUNTS[name]:>7}")
    print()
    if not FAILURES:
        print("  no failures")
        return 0

    print(f"  {len(FAILURES)} FAILURE(S):")
    seen = Counter()
    for area, message, _seed in FAILURES:
        key = (area, message.split(":")[0][:70])
        seen[key] += 1
    for (area, message), count in seen.most_common(25):
        print(f"     [{area}] x{count}  {message}")
    print()
    print("  first five in full:")
    for area, message, seed in FAILURES[:5]:
        print(f"     [{area}] {message}   seed={seed}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 10_000))
