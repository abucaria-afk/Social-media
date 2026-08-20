"""The manager: plan a post before the footage for it exists, and check it.

Everything else in this program starts from footage. This starts from an
intention — a date, a surface, and a sentence about what the film is — and
works backwards to what somebody has to go and shoot. That is the part of
making things for a feed that no tool here covered: by the time you have the
photographs, most of the decisions have already been made badly.

So a :class:`Plan` is a post that does not exist yet. It carries a shot list
derived from what the Scholar has measured about how these films are actually
cut, a caption drafted from the brief, and a set of checks that each name the
number they are checking against and where that number came from.

**It never posts.** Not "does not post yet", and not "posts only with
confirmation": there is no network call in this module and no code path
anywhere in this program that publishes to a service. `mark_posted` records
that a *person* posted something, which is a different verb. The one test that
matters here is the one asserting that.

Two things it deliberately will not tell you:

* **a chance of going viral, as a number this program made up.** The scoring
  model has one, and where a fitted model exists the manager reports its
  prediction *with its provenance attached* — including, loudly, when that
  provenance is "fitted on simulated rows", which means it predicts the
  simulator and not any platform. A confident percentage over no data is the
  single most harmful thing a tool like this can produce.
* **the best time of day to post.** Nothing in the metric schema records when
  a post went out, so nothing here has ever measured it. The manager spaces
  posts and says plainly that the hour is not something it knows.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("auteur.manager")

#: What a plan can be. A plan moves left to right and never skips: a film
#: cannot be `ready` before it is `cut`, because "ready" means the checks have
#: run and the checks need something to run on.
STATUSES = ("idea", "shot", "cut", "ready", "posted", "dropped")

#: The roles a shot can play, and roughly how long each holds relative to the
#: film's median. Same vocabulary the cutting engine uses, so a shot list
#: written here and a film cut later are describing the same thing.
ROLES: dict[str, tuple[str, float]] = {
    "hook": ("the first thing anybody sees — it has to be the strongest frame", 0.9),
    "run": ("carries the middle; several of these in a row", 1.0),
    "accent": ("a beat that breaks the run — closer, faster, or louder", 0.7),
    "rest": ("one held frame so the cuts around it land", 1.8),
    "turn": ("where the film changes its mind", 1.2),
    "close": ("the last frame, held — this is the one people screenshot", 2.2),
}

#: How the roles are laid out across a film. Read as: hook, then runs with an
#: accent every few, a rest around the middle, a turn three-quarters in, close.
#: Not a rule from anywhere — it is the shape the reference reels measure out
#: to, and `shot_list` says so in the note it attaches.
SHAPE = ("hook", "run", "run", "accent", "run", "rest", "run", "accent", "turn", "run", "close")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(path.suffix + ".new")
    scratch.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(scratch, path)


# ---------------------------------------------------------------------------
# What to go and shoot
# ---------------------------------------------------------------------------


@dataclass
class PlannedShot:
    """One thing somebody has to go and photograph."""

    order: int
    role: str
    #: What to shoot. Written as an instruction, because that is what it is.
    what: str
    seconds: float

    @property
    def why(self) -> str:
        return ROLES.get(self.role, ("", 1.0))[0]


def shot_list(prompt: str, *, seconds: float, hold: float = 0.0) -> list[PlannedShot]:
    """What to go and shoot, from a sentence and a runtime.

    `hold` is the median shot length to cut at. Passed in rather than looked up
    so this stays a pure function; the caller gets it from the Scholar, which
    measures it across the reference reels rather than guessing.

    The count follows from the arithmetic — a 20 second film at a 0.4s median
    is fifty shots — and the *roles* follow from the shape above. What each
    shot should contain is the one thing this cannot know, so it says what the
    shot is for and leaves the subject to a person.
    """
    hold = hold if hold > 0.05 else 0.4
    seconds = max(1.0, float(seconds))
    # Roles have different weights, so the count is not simply seconds / hold.
    weights = [ROLES.get(role, ("", 1.0))[1] for role in SHAPE]
    per_cycle = sum(weights) * hold
    cycles = max(1, round(seconds / per_cycle))

    shots: list[PlannedShot] = []
    order = 0
    #: Per role, so consecutive runs draw different instructions. Cycling on
    #: the position within SHAPE instead put "a wide of where you are" twice in
    #: a row wherever two runs were adjacent.
    turn: dict[str, int] = {}
    for cycle in range(cycles):
        for role in SHAPE:
            # Only the first cycle opens and only the last one closes.
            if role == "hook" and cycle > 0:
                role = "run"
            if role == "close" and cycle < cycles - 1:
                role = "run"
            order += 1
            turn[role] = turn.get(role, -1) + 1
            shots.append(
                PlannedShot(
                    order=order,
                    role=role,
                    what=_what_to_shoot(prompt, role, turn[role]),
                    seconds=round(hold * ROLES.get(role, ("", 1.0))[1], 3),
                )
            )
    return shots


#: Instructions per role. Deliberately about *framing* rather than about
#: subject: the subject is the one thing a person has and this does not.
_INSTRUCTIONS: dict[str, tuple[str, ...]] = {
    "hook": (
        "the single best frame you have — closest, brightest, most movement",
        "something already in motion when the film starts",
    ),
    "run": (
        "a wide of where you are",
        "a detail, close enough that it is not obvious what it is",
        "hands doing the thing",
        "the same subject from the other side",
        "something moving through the frame",
    ),
    "accent": (
        "much closer than the shot before it",
        "the fastest thing here",
        "a hard graphic shape — a line, an edge, a shadow",
    ),
    "rest": ("one still, wide frame with nothing happening in it",),
    "turn": ("the same place looking the other way, or after something changed",),
    "close": ("the frame you would want as the cover — hold it",),
}


def _what_to_shoot(prompt: str, role: str, index: int) -> str:
    options = _INSTRUCTIONS.get(role) or ("a frame",)
    return options[index % len(options)]


@dataclass
class Capture:
    """One setup to go and get, and how hard the cut leans on it."""

    what: str
    role: str
    #: How many shots in the timeline are cut from this setup.
    times: int
    #: Total screen time it carries.
    seconds: float


def capture_list(shots: list[PlannedShot]) -> list[Capture]:
    """The distinct things to photograph, from a timeline of shots.

    A twenty second hypercut is a hundred and ten shots, and a hundred and ten
    numbered instructions is not a shot list — nobody goes out and shoots a
    hundred and ten things. They shoot a dozen setups and the edit cuts among
    them, which is how the reference reels are actually made and what makes
    them possible to make at all.

    So the timeline stays, because the cadence check needs it, and this is what
    a person is handed: the distinct setups, how many times the cut uses each,
    and how much of the film each one carries.
    """
    order: list[tuple[str, str]] = []
    tally: dict[tuple[str, str], list[float]] = {}
    for shot in shots:
        key = (shot.role, shot.what)
        if key not in tally:
            tally[key] = []
            order.append(key)
        tally[key].append(shot.seconds)
    return [
        Capture(
            what=what,
            role=role,
            times=len(tally[(role, what)]),
            seconds=round(sum(tally[(role, what)]), 2),
        )
        for role, what in order
    ]


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    """A post that does not exist yet."""

    id: str
    owner: str
    title: str
    #: A key from workflows.platforms.PLATFORMS.
    platform: str
    #: When a person intends to post it, ISO-8601 UTC.
    when: str
    #: The sentence the film will be made from — the same words the make screen
    #: takes, so a plan can become a film without being retyped.
    prompt: str
    seconds: float = 20.0
    status: str = "idea"
    shots: list[dict] = field(default_factory=list)
    #: The distinct setups behind those shots — what somebody takes out with
    #: them. Derived from `shots`, stored so the page does not have to.
    captures: list[dict] = field(default_factory=list)
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    alt_text: str = ""
    #: Set once a film has been made for this plan.
    film: str = ""
    note: str = ""
    created: float = field(default_factory=time.time)

    def public(self) -> dict:
        out = asdict(self)
        out["due"] = self.when
        return out


class Board:
    """Everybody's plans, on disk beside the accounts and the films."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.plans: dict[str, Plan] = {}
        self._load()

    @staticmethod
    def default_path(workspace: Path) -> Path:
        return Path(workspace) / "plans.json"

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, list):
            return
        known = set(Plan.__dataclass_fields__)
        for row in raw:
            if isinstance(row, dict) and row.get("id"):
                self.plans[row["id"]] = Plan(**{k: v for k, v in row.items() if k in known})

    def _save(self) -> None:
        _write(self.path, [asdict(p) for p in self._soonest_first()])

    def _soonest_first(self) -> list[Plan]:
        return sorted(self.plans.values(), key=lambda p: (p.when, p.created))

    # -- writing ---------------------------------------------------------

    def add(self, **fields) -> Plan:
        plan = Plan(id=uuid.uuid4().hex[:12], **fields)
        if plan.status not in STATUSES:
            plan.status = "idea"
        with self.lock:
            self.plans[plan.id] = plan
            self._save()
        return plan

    def update(self, plan_id: str, owner: str, **fields) -> Plan | None:
        with self.lock:
            plan = self.plans.get(plan_id)
            if plan is None or plan.owner != owner:
                return None
            for key, value in fields.items():
                if key in {"id", "owner", "created"} or not hasattr(plan, key):
                    continue
                if key == "status" and value not in STATUSES:
                    continue
                setattr(plan, key, value)
            self._save()
            return plan

    def mark_posted(self, plan_id: str, owner: str) -> Plan | None:
        """Record that a *person* posted this. Nothing here posts anything.

        The verb matters. This program has no credentials for any service and
        makes no request to one; what this records is that somebody went and
        did it themselves, so the board stops showing it as outstanding.
        """
        return self.update(plan_id, owner, status="posted")

    def drop(self, plan_id: str, owner: str) -> bool:
        with self.lock:
            plan = self.plans.get(plan_id)
            if plan is None or plan.owner != owner:
                return False
            self.plans.pop(plan_id)
            self._save()
            return True

    # -- reading ---------------------------------------------------------

    def get(self, plan_id: str) -> Plan | None:
        with self.lock:
            return self.plans.get(plan_id)

    def by(self, owner: str) -> list[Plan]:
        with self.lock:
            return [p for p in self._soonest_first() if p.owner == owner]

    def upcoming(self, owner: str, *, within_days: int = 30) -> list[Plan]:
        edge = (_now() + timedelta(days=within_days)).isoformat()
        return [
            p for p in self.by(owner) if p.status not in ("posted", "dropped") and p.when <= edge
        ]


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One check, its verdict, and where the number it used came from."""

    name: str
    #: "pass", "warn" or "fail". Nothing here is fatal — a warn is a judgement
    #: somebody may disagree with, and it says so rather than blocking.
    verdict: str
    detail: str
    #: What this was checked against: a platform's published limit, a number
    #: the Scholar measured, or the structure of the plan itself. Named so a
    #: person can tell a rule from a measurement from an opinion.
    source: str


@dataclass
class Check:
    """Everything the manager can say about a plan's chances, and its limits."""

    findings: list[Finding] = field(default_factory=list)
    #: The scoring model's own prediction, if a model has been fitted, with the
    #: model's provenance carried alongside it and never separated from it.
    predicted: float | None = None
    provenance: str = ""

    @property
    def passed(self) -> int:
        return sum(1 for f in self.findings if f.verdict == "pass")

    @property
    def problems(self) -> list[Finding]:
        return [f for f in self.findings if f.verdict != "pass"]

    def to_json(self) -> dict:
        return {
            "findings": [asdict(f) for f in self.findings],
            "passed": self.passed,
            "total": len(self.findings),
            "predicted": self.predicted,
            "provenance": self.provenance,
            "posts": False,
        }


def check(plan: Plan, *, hold: float = 0.0, first_cut: float = 0.0, others=()) -> Check:
    """Hold a plan against everything that is actually knowable about it.

    `hold` and `first_cut` come from the Scholar — the corpus median shot
    length and how long the reference reels wait before their first cut. Passed
    in for the same reason as in `shot_list`: this stays a function of its
    arguments, so the numbers it judges by are visible at the call site rather
    than fetched invisibly from a store.

    `others` is the rest of this person's board, for spacing.
    """
    from .workflows.platforms import resolve
    from .workflows.schedule import DEFAULT_GAP_HOURS, DEFAULT_PER_DAY

    out = Check()
    add = out.findings.append

    try:
        spec = resolve(plan.platform)
    except Exception:  # noqa: BLE001 - an unknown surface is the finding
        add(
            Finding(
                "surface",
                "fail",
                f"{plan.platform!r} is not a surface this knows the rules for",
                "the platform table",
            )
        )
        return out

    # -- length ----------------------------------------------------------
    trouble = spec.duration_problem(plan.seconds)
    if trouble:
        add(Finding("length", "fail", trouble, f"{spec.service}'s published limits"))
    elif abs(plan.seconds - spec.ideal_seconds) > spec.ideal_seconds * 0.5:
        add(
            Finding(
                "length",
                "warn",
                f"{plan.seconds:.0f}s against the {spec.ideal_seconds:.0f}s this surface "
                f"is built around — allowed, but not what it is shaped for",
                f"{spec.service}'s ideal runtime",
            )
        )
    else:
        add(
            Finding(
                "length",
                "pass",
                f"{plan.seconds:.0f}s sits inside {spec.min_seconds:.0f}–"
                f"{spec.max_seconds:.0f}s and near the {spec.ideal_seconds:.0f}s ideal",
                f"{spec.service}'s published limits",
            )
        )

    # -- cadence ---------------------------------------------------------
    shots = len(plan.shots)
    if shots and hold > 0:
        actual = plan.seconds / shots
        if actual > hold * 2.2:
            add(
                Finding(
                    "cadence",
                    "warn",
                    f"{shots} shots over {plan.seconds:.0f}s is {actual:.2f}s each, against "
                    f"{hold:.3f}s in the films this has measured — slower than the thing "
                    f"being chased",
                    "the Scholar's measured median hold",
                )
            )
        elif actual < hold * 0.6:
            add(
                Finding(
                    "cadence",
                    "warn",
                    f"{actual:.2f}s a shot is faster than the {hold:.3f}s median measured "
                    f"across the reference reels, and faster than any of them",
                    "the Scholar's measured median hold",
                )
            )
        else:
            add(
                Finding(
                    "cadence",
                    "pass",
                    f"{shots} shots at about {actual:.2f}s each, against a measured "
                    f"{hold:.3f}s median",
                    "the Scholar's measured median hold",
                )
            )
    elif not shots:
        add(
            Finding(
                "cadence",
                "warn",
                "no shot list yet, so there is nothing to shoot and nothing to check",
                "the plan itself",
            )
        )

    # -- the opening -----------------------------------------------------
    opener = plan.shots[0] if plan.shots else None
    if opener and str(opener.get("role")) == "hook":
        held = float(opener.get("seconds") or 0.0)
        if first_cut > 0 and held > first_cut * 2.0:
            add(
                Finding(
                    "opening",
                    "warn",
                    f"the opening holds {held:.2f}s; the reels measured here cut at "
                    f"{first_cut:.2f}s, and the opening hold is the hook's whole budget",
                    "the Scholar's measured first cut",
                )
            )
        else:
            add(
                Finding(
                    "opening",
                    "pass",
                    f"opens on a hook shot held {held:.2f}s",
                    "the Scholar's measured first cut",
                )
            )
    else:
        add(
            Finding(
                "opening",
                "warn",
                "nothing in the plan is marked as the hook, so the first frame is "
                "whatever happens to be first",
                "the plan itself",
            )
        )

    # -- the words -------------------------------------------------------
    caption = plan.caption.strip()
    if not caption:
        add(Finding("caption", "warn", "no caption drafted", "the plan itself"))
    elif len(caption) > spec.caption_limit:
        add(
            Finding(
                "caption",
                "fail",
                f"{len(caption)} characters against a {spec.caption_limit} limit — "
                f"{spec.service} truncates mid-word and does not say it did",
                f"{spec.service}'s caption box",
            )
        )
    else:
        add(
            Finding(
                "caption",
                "pass",
                f"{len(caption)} of {spec.caption_limit} characters",
                f"{spec.service}'s caption box",
            )
        )

    tags = [t for t in plan.hashtags if t.strip()]
    if len(tags) > spec.hashtag_limit:
        add(
            Finding(
                "hashtags",
                "warn",
                f"{len(tags)} tags; past {spec.hashtag_limit} this surface either refuses "
                f"them or quietly stops counting, so more is only longer",
                f"{spec.service}'s tag limit",
            )
        )
    elif not tags:
        add(Finding("hashtags", "warn", "no tags", "the plan itself"))
    else:
        add(
            Finding(
                "hashtags",
                "pass",
                f"{len(tags)} of at most {spec.hashtag_limit}",
                f"{spec.service}'s tag limit",
            )
        )

    if not plan.alt_text.strip():
        add(
            Finding(
                "alt text",
                "warn",
                "no description for anybody using a screen reader",
                "not optional in spirit",
            )
        )
    else:
        add(Finding("alt text", "pass", "written", "not optional in spirit"))

    # -- the surface's own furniture --------------------------------------
    if spec.wants_cover:
        add(
            Finding(
                "cover frame",
                "pass" if plan.status in ("cut", "ready", "posted") else "warn",
                (
                    "there is a film to choose a cover from"
                    if plan.status in ("cut", "ready", "posted")
                    else f"{spec.service} publishes a still beside the video and lets you "
                    f"choose it; there is no film yet to choose from"
                ),
                f"{spec.service} shows a cover",
            )
        )
    add(
        Finding(
            "safe area",
            "pass",
            f"words will be kept clear of {spec.service}'s own buttons "
            f"({spec.safe.describe()})",
            f"{spec.service}'s interface",
        )
    )

    # -- spacing ---------------------------------------------------------
    same_day = [
        other
        for other in others
        if other.id != plan.id
        and other.platform == plan.platform
        and other.status not in ("dropped",)
        and abs(_hours_between(other.when, plan.when)) < 24
    ]
    too_close = [o for o in same_day if abs(_hours_between(o.when, plan.when)) < DEFAULT_GAP_HOURS]
    if too_close:
        add(
            Finding(
                "spacing",
                "warn",
                f"{len(too_close)} other post to {spec.service} within {DEFAULT_GAP_HOURS}h",
                "the queue's own spacing rule",
            )
        )
    elif len(same_day) + 1 > DEFAULT_PER_DAY:
        add(
            Finding(
                "spacing",
                "warn",
                f"{len(same_day) + 1} posts to {spec.service} in a day, over the {DEFAULT_PER_DAY} "
                f"this queue will send",
                "the queue's own daily cap",
            )
        )
    else:
        add(
            Finding(
                "spacing",
                "pass",
                f"clear of every other {spec.service} post by at least {DEFAULT_GAP_HOURS}h",
                "the queue's own spacing rule",
            )
        )

    # -- and the thing it will not claim ----------------------------------
    add(
        Finding(
            "time of day",
            "warn",
            "not checked: nothing in this program's metric schema records when a post "
            "went out, so it has never measured whether the hour matters",
            "nothing — this is a gap, said out loud",
        )
    )

    return out


def _hours_between(a: str, b: str) -> float:
    try:
        first = datetime.fromisoformat(a)
        second = datetime.fromisoformat(b)
    except (TypeError, ValueError):
        return 9999.0
    return (first - second).total_seconds() / 3600.0


def predict_for(plan: Plan, film_path: str | Path | None = None) -> tuple[float | None, str]:
    """The scoring model's own number, and its provenance, together.

    Returns `(None, why)` when there is nothing honest to report — no film to
    read, or no model. The provenance is never returned separately from the
    number and never dropped: a fitted-on-simulated-rows model produces a
    perfectly confident figure that predicts the simulator, and a figure
    without that sentence attached is worse than no figure.
    """
    if not film_path:
        return None, "no film yet — the model reads a finished cut, not a plan"
    try:
        from .insight.score import fit, predict, timeline_of

        report = fit([])
        edl = timeline_of(Path(film_path))
        prediction = predict(edl, report)
    except Exception as exc:  # noqa: BLE001 - no model is a report, not a crash
        return None, f"the model could not read that film: {exc}"
    score = getattr(prediction, "score", None)
    return (
        float(score) if score is not None else None,
        getattr(report, "provenance", "") or "no provenance recorded",
    )
