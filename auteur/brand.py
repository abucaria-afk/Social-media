"""What this app is, said once.

There were three answers to "what is Auteur" and they had drifted apart. The
App Store listing had the current one — a film from your camera roll, the feed,
the planning board, 12+, nothing leaves the device. `docs/index.html` still
described the command-line tool it was eighteen months ago and shipped a
palette thirteen colours out of date, under a comment claiming it was generated
from `auteur/theme.py`. Google Play had no answer at all.

Three copies of a product's story is three products as far as anybody reading
them is concerned, and the one a person meets first is whichever they meet
first. So the story lives here, once, and everything that tells it — the two
store listings, the site, the promotional stills — reads it from this file.

**The palette is not in here.** It is in `auteur/theme.py`, which is where it
has always been, and the site now generates from it rather than carrying a
hand-copied snapshot. That snapshot is exactly how the site ended up showing a
green accent for an app whose accent had been teal for months.
"""

from __future__ import annotations

from dataclasses import dataclass

from .identity import IDENTITY

#: When the store limits below were last checked against the stores' own
#: guidance. Same discipline as `workflows/platforms.py`: a number nobody
#: dates is a number nobody re-checks.
AS_OF = "2026-08"

#: What the thing is called — read from `identity.py` rather than written
#: again here.
#:
#: These were two values for one thing: `brand.NAME` said "auteur" and
#: `IDENTITY.app_name` said "Auteur", and nothing compared them. That is the
#: same defect as the site shipping a hand-copied palette under a comment
#: claiming it was generated — and it would have shipped a store listing
#: naming the app one thing while the site's wordmark named it another.
#:
#: It also stopped being lower case, and for a reason rather than a whim: the
#: app is one product under Auteur Studies now, so the name has to say which
#: product it is. It says the product and nothing else — **Atlas, never
#: "Auteur Atlas"**. Auteur Studies is the publisher, and a publisher's name
#: welded to the front of a product's is a name that sells the wrong thing.
NAME = IDENTITY.app_name

#: One line, under thirty characters, because that is the tightest box either
#: store gives you — App Store subtitle and Play title are both 30.
#:
#: It said "A film from your camera roll" for months, on every surface, and
#: that line is *true* — the app cuts a film from your camera roll and there
#: are frames in the repository proving it. It was still the wrong line,
#: because it named the most visible step rather than the thing somebody is
#: buying. What Atlas is for is the week: the shot list, the caption, the cut,
#: and the numbers back. Cutting is one of the four.
#:
#: Nothing in the repository could have found that. Every surface agreed with
#: every other, and they agreed on the wrong sentence, which is why this file
#: is the fix for drift and not the fix for being wrong.
TAGLINE = "Plan the week, read the reach"

#: The sentence that does the selling. Apple allows 170 for a promotional
#: text; Play's short description allows 80, so there are two lengths of the
#: same claim rather than two different claims.
PROMISE = (
    "Plan the week's posts, shoot the shot list, and say the film you want in "
    "a sentence — it cuts and grades it. Afterwards, read back how it did."
)

#: The same promise at Play's short-description length.
PROMISE_SHORT = "Plan the week, cut the film, read the reach."

#: The one sentence a search engine shows under the link, and the one a social
#: card shows under the title.
#:
#: Its own constant rather than a reuse of `PROMISE`, because a search snippet
#: has a job the promotional text does not: it is read cold, next to nine other
#: results, by somebody who has not seen the name yet. So it leads with the
#: product rather than with an instruction — `PROMISE` opens "Say what you
#: want", which out of context reads like a command from a stranger.
#:
#: It exists at all because there were nearly three. The generated site used
#: `PROMISE`; the live company site had a hand-written one; and writing this I
#: drafted a third before noticing. One constant, read by every surface.
#:
#: **What it says was corrected by the owner, and the correction is the
#: interesting part.** Reading only this repository, the live site's
#: description looked wrong twice over — it led with planning and reach, which
#: from in here read as two secondary features, and it sold a second product
#: called APX, and the only APX in this tree is the craft-rules work cited
#: throughout `auteur/craft/story.py`. So the site was "fixed" to match the
#: app, and the fix was the error: Atlas *is* the thing that plans the week
#: and reads the reach, APX *is* a real free product beside it, and the
#: craft rules are a feature of Atlas that happens to share a name with it.
#:
#: The general failure is worth more than the specific one. A repository can
#: check that every surface it generates agrees with every other; it cannot
#: check that the agreed sentence is true, because the fact lives with the
#: person who decided it. Consistency read as correctness, and a unanimous
#: chorus of surfaces is exactly as loud when they are all wrong.
#:
#: The length is not decoration: Google truncates around 155 characters and
#: Open Graph cards clip sooner, so a description that runs long is a sentence
#: whose end nobody reads. A test holds it inside that.
META_DESCRIPTION = (
    f"{NAME} plans the week and reads the reach — a shot list, a caption, "
    "a cut film, and the numbers back. It runs on your device."
)

#: The other product, named here so nothing in this tree can call it a
#: mistake again.
#:
#: APX is free, has no account and no server, stores nothing, and carries a
#: day's state in the link itself — the planner that forgets. It is not built
#: from this repository and nothing here should imply it is a mode of Atlas or
#: a tier of it. Two standalone brands under one publisher, the same way Atlas
#: is not "Auteur Atlas".
#:
#: It is a constant rather than a paragraph in a document because a document
#: is not read by `too_long`, by the site builder, or by anything else that
#: could catch a surface getting it wrong.
SIBLING = "APX"
SIBLING_LINE = "APX — the planner that forgets. Free, no account, nothing stored."

#: Search terms. Apple takes a comma-separated 100 characters; Play has no
#: keyword field and reads the description instead, which is why the
#: description below says these words in prose rather than listing them.
KEYWORDS = "video editor,reels,montage,film,cut,edit,grade,vhs,super 8,offline,privacy"


@dataclass(frozen=True)
class Feature:
    """One thing the app does, in the words a person would use."""

    #: Four or five words, for a headline or a screenshot caption.
    headline: str
    #: A sentence, for the site and the long store description.
    body: str
    #: True where the claim is about something a reviewer can see working in
    #: the shipped build without an account or a server. Everything shown on
    #: the site's front page has to be one of these, because a landing page
    #: that leads with a feature you cannot reach is a landing page that lies.
    on_device: bool = True


#: What it does, ordered by what somebody meets first.
FEATURES: list[Feature] = [
    Feature(
        "Plan the week before you shoot",
        "A shot list grouped into setups you can shoot in one go, a caption "
        "written with it, and a check on the things that decide whether "
        "anybody sees it — all before the photograph exists.",
    ),
    Feature(
        "Say it in a sentence",
        "Write the film you want the way you would describe it to a person — "
        "“the long way home, unhurried, 90s” — and it works out the pace, the "
        "grade and the joins from that.",
    ),
    Feature(
        "Cut to a rhythm",
        "A montage comes back at a third of a second a shot and a hypercut at a "
        "sixth, because those are the numbers the reference reels are cut at, "
        "measured rather than guessed.",
    ),
    Feature(
        "Graded for a decade",
        "Super 8, VHS, Kodak, Y2K flash, faded 2010s. The grade moves the "
        "picture, and there are frames in the repository proving it does.",
    ),
    Feature(
        "Type and stickers on the beat",
        "Anything you write in quotes lands on screen, on the cut, at the size "
        "the frame can carry.",
    ),
    Feature(
        "Every shape a platform wants",
        "Vertical, square, portrait and wide, at the runtime each surface "
        "actually recommends rather than the one it merely allows.",
    ),
    Feature(
        "It runs on your device",
        "No third-party analytics, no advertising identifier, no third-party "
        "code. Making a film works in aeroplane mode. The only thing that ever "
        "talks to another company is a TikTok or Instagram account you connect "
        "yourself, to read back how a post did.",
    ),
    Feature(
        "Read back how it did",
        "Connect a TikTok or Instagram account and the numbers come back into "
        "the planning board — watched, finished, shared — fitted against your "
        "own exports rather than a rule somebody wrote down.",
    ),
    Feature(
        "A feed that learns, on your own machine",
        "An instance measures how long its films are watched and what you "
        "finish, and ranks with it. That is how a feed stops being a shuffle. "
        "It stays on your hardware and your history is yours to delete.",
        on_device=False,
    ),
    Feature(
        "A feed, if you want one",
        "Run a copy on your own computer and the app connects to it — the "
        "feed, the messages and the planning board live on hardware you own.",
        on_device=False,
    ),
]

#: What a person is buying, in one paragraph, for the top of a page.
POSITIONING = (
    f"{NAME} plans the week and reads the reach. A shot list you can actually "
    "shoot, a caption to go with it, a film cut from your own footage — not a "
    "template you drop clips into, it reads the footage and decides what each "
    "shot is for — and afterwards the numbers back."
)

#: The honest sentence about what it is not, which belongs on a landing page
#: for the same reason it belongs in a review note: the fastest way to lose
#: somebody is to let them find out later.
CAVEAT = (
    "It does not post for you and it does not have an opinion about your "
    "follower count. Every share is something you do."
)


# ---------------------------------------------------------------------------
# What each store will carry
# ---------------------------------------------------------------------------
#
# Apple's limits live in `auteur/identity.py` because they gate the identity
# check. Play's live here because nothing else needed them until now.


@dataclass(frozen=True)
class StoreLimits:
    """The boxes a listing has to fit into."""

    store: str
    title: int
    #: Play calls it a short description; Apple calls it a subtitle. Same job.
    short: int
    #: Play's full description; Apple's description.
    full: int
    #: Apple only. Play has no keyword field.
    keywords: int = 0
    #: Apple only.
    promotional: int = 0


LIMITS = {
    "apple": StoreLimits(
        store="App Store",
        title=30,
        short=30,
        full=4000,
        keywords=100,
        promotional=170,
    ),
    "play": StoreLimits(
        store="Google Play",
        title=30,
        short=80,
        full=4000,
    ),
}


def too_long(store: str) -> list[str]:
    """Every piece of copy that will not fit that store, with the overage.

    Returns an empty list when everything fits. Called by both listing
    generators and by the preflight, so a sentence that grew past a limit is
    caught here rather than by a form that refuses to save.
    """
    limits = LIMITS[store]
    checks = [
        ("title", NAME, limits.title),
        ("short description" if store == "play" else "subtitle", TAGLINE, limits.short),
        ("full description", description(), limits.full),
    ]
    if store == "play":
        checks[1] = ("short description", PROMISE_SHORT, limits.short)
    if limits.keywords:
        checks.append(("keywords", KEYWORDS, limits.keywords))
    if limits.promotional:
        checks.append(("promotional text", PROMISE, limits.promotional))

    return [
        f"{label} is {len(text)} characters and {limits.store} allows {cap}"
        for label, text, cap in checks
        if len(text) > cap
    ]


def description() -> str:
    """The long description, built from the feature list rather than typed.

    Typed twice it drifts twice. This is the same `FEATURES` the site renders,
    so a claim added to one appears in the other or in neither.
    """
    # Every wrapped string in the list below is parenthesised. Inside a list
    # literal, an intended concatenation and a forgotten comma look exactly
    # the same — CodeQL flags the shape for that reason, and it is right to:
    # the reader cannot tell either. The brackets say which one it is.
    lines = [POSITIONING, "", "WHAT IT DOES", ""]
    lines += [f"• {feature.headline} — {feature.body}" for feature in FEATURES]
    lines += [
        "",
        "PLAN BEFORE YOU SHOOT",
        "",
        (
            "The manager plans a post before the photograph exists: a shot "
            "list grouped into setups you can shoot in one go, a caption, and "
            "a check on the things that decide whether anybody sees it."
        ),
        "",
        CAVEAT,
        "",
        "FOR EVERYONE",
        "",
        (
            "Text size, reduced motion and increased contrast are settings in "
            "the app, and the system's own accessibility settings are always "
            "honoured."
        ),
        "",
        (
            f"{NAME} is for people 12 and over. An account for somebody under "
            "18 starts with sensitive films hidden, and that can be locked "
            "with a code."
        ),
    ]
    return "\n".join(lines)


@dataclass
class Shot:
    """One promotional still: what it shows and why it is the one to show."""

    key: str
    caption: str
    #: The screen it is captured from, so the still is the app rather than a
    #: drawing of the app. Nothing here is mocked up.
    route: str
    reason: str = ""


#: The stills both stores ask for, in the order they should be shown. First
#: position is the one most people ever see, so it is the finished film rather
#: than the form that made it.
SHOTS: list[Shot] = [
    Shot(
        "film",
        "The film it just cut",
        "/",
        "Lead with the output. A first screenshot of an empty text box is a "
        "screenshot of homework.",
    ),
    Shot("say", "Say what you want", "/", "The one interaction the whole app is."),
    # "/looks" for months, which the app has never served. The decade grades
    # are the `#era` group on the home screen, so that is where the still of
    # them comes from.
    Shot("looks", "Graded for a decade", "/", "The most visual control there is."),
    Shot("feed", "Films, if you run a copy", "/feed", "Shows it is more than a tool."),
    Shot("projects", "A map for the thinking", "/projects", "Nothing else looks like this."),
    Shot("you", "Yours to set", "/profile", "Accessibility and the age setting."),
]
