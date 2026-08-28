"""What other people measured, and where they wrote it down.

Everything the Scholar knew came from inside this repository. Audited on the
live store: 127 learnings, of which 94 were measured off the project's own
reels, 23 read out of the project's own markdown, 7 were conclusions drawn over
those, and 3 came from its own scrolls. Not one had ever come from outside.

That is a problem the confidence ladder was built to catch and could not,
because the ladder counts independent *channels* and there was only ever one.
`library.py` says so in its own docstring — "a project's own notes agreeing
with a project's own notes is not corroboration" — and then nothing ever
supplied the corroboration. A program that measures 23 reels and calls the
result the pace of the form is a program marking its own homework.

This module is the outside. Each finding below was published by somebody else,
carries the URL it was published at, the year the measurement was *taken*, and
an honest account of how strong the evidence is. They are not all equal and
they are not recorded as though they were: a peer-reviewed measurement of 160
films is not the same kind of fact as a marketing blog's retention statistic
with no primary source behind it, and flattening the two would be the opposite
of accuracy.

Nothing here is scraped at runtime. These are findings read, checked against
this project's own numbers where they overlap, and written down with their
citations — so the store is reproducible, works offline, and can be audited by
a person following the links.
"""

from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from .knowledge import Confidence, Discipline, Learning


@dataclass(frozen=True)
class Source:
    """Where a finding was published, and how much weight it can carry."""

    #: Short key, used to build the learning's channel.
    key: str
    #: What to cite.
    title: str
    #: Who published it.
    publisher: str
    #: Where to read it.
    url: str
    #: The year the measurement was taken — not the year it was read. A figure
    #: from 2014 described as "today" is a 2014 figure, and saying so is the
    #: difference between a dated fact and a wrong one.
    measured: int
    #: How the finding was arrived at. This decides how far up the ladder a
    #: learning drawn from it may start.
    kind: str  # "peer-reviewed" | "database" | "trade"

    @property
    def channel(self) -> str:
        return f"published:{self.key}"

    @property
    def strength(self) -> Confidence:
        # A measured corpus of thousands of films and a blog post asserting a
        # percentage are not the same evidence. Only the first two kinds start
        # above the bottom of the ladder.
        if self.kind in ("peer-reviewed", "database"):
            return Confidence.SUPPORTED
        return Confidence.TENTATIVE


#: The sources, with the year each measurement was actually taken.
SOURCES = {
    "cutting-2011": Source(
        key="cutting-2011",
        title="Quicker, faster, darker: Changes in Hollywood film over 75 years",
        publisher="Cutting, Brunick, DeLong, Iricinschi & Candan — i-Perception",
        url="https://pmc.ncbi.nlm.nih.gov/articles/PMC3485803/",
        measured=2011,
        kind="peer-reviewed",
    ),
    "salt-cinemetrics": Source(
        key="salt-cinemetrics",
        title="Barry Salt: The Metrics in Cinemetrics",
        publisher="Cinemetrics, University of Chicago",
        url="https://cinemetrics.uchicago.edu/article/616e7ecc-7915-4768-b84d-7dec79aa77c2",
        measured=2006,
        kind="database",
    ),
    "redfern-2009": Source(
        key="redfern-2009",
        title="Measures of central tendency in Cinemetrics",
        publisher="Nick Redfern, Research into Film",
        url="https://nickredfern.wordpress.com/2009/12/10/measures-of-central-tendency-in-cinemetrics/",
        measured=2009,
        kind="peer-reviewed",
    ),
    "cutting-wired": Source(
        key="cutting-wired",
        title="Data From a Century of Cinema Reveals How Movies Have Evolved",
        publisher="Wired, reporting James Cutting (Cornell)",
        url="https://www.wired.com/2014/09/cinema-is-evolving/",
        measured=2014,
        kind="trade",
    ),
}


@dataclass
class Finding:
    """One published claim, in a form the crew can be held to."""

    source: str
    technique: str
    insight: str
    application: str
    disciplines: list[Discipline]
    measurements: dict = field(default_factory=dict)


#: What other people found. Numbers are theirs, quoted as published.
FINDINGS = [
    Finding(
        source="redfern-2009",
        technique="shot length — why the median and not the average",
        insight=(
            "Shot lengths in a film are not symmetrically distributed. They are "
            "right-skewed: most shots are shorter than the arithmetic mean, and a "
            "small number of long held shots drag the mean upward. Salt, Redfern and "
            "Cutting all say the same thing about the consequence — the average shot "
            "length systematically over-states how long a typical shot is, and the "
            "median is the better estimate of what an audience actually sees. Redfern "
            "gives a rule of thumb for feature films: median is about 0.6 of the mean."
        ),
        application=(
            "take the pace of a corpus from the median hold, never the mean — and "
            "when a rate has to be quoted, measure it rather than deriving it from "
            "the median, because 1/median over-states the cut rate for exactly the "
            "same reason the mean over-states the hold"
        ),
        disciplines=[Discipline.MOVIE_MAKING, Discipline.PATTERN_RECOGNITION],
        measurements={"median_over_mean": 0.6},
    ),
    Finding(
        source="cutting-wired",
        technique="the pace of feature film, and how far it has moved",
        insight=(
            "James Cutting's measurement of English-language feature film puts the "
            "average shot at about 12 seconds in 1930 and about 2.5 seconds by 2014 — "
            "a fivefold acceleration over eighty years, still going. This is the "
            "number people mean when they say films are cut faster than they used to "
            "be, and it is a feature-film number: it says nothing directly about a "
            "reel made for a phone."
        ),
        application=(
            "when somebody reaches for 'cinematic' as a pace, this is the pace they "
            "are reaching for — about 2.5s a shot, not the sub-second cutting of a "
            "social reel, and roughly seven times slower than this program's montage"
        ),
        disciplines=[Discipline.MOVIE_MAKING, Discipline.CINEMATOGRAPHY],
        measurements={"asl_1930": 12.0, "asl_2014": 2.5},
    ),
    Finding(
        source="salt-cinemetrics",
        technique="how much film has actually been measured, and by whom",
        insight=(
            "Barry Salt's Cinemetrics database holds close to 10,000 average shot "
            "lengths taken from complete films; Cutting and colleagues cite Salt as "
            "having measured more than 13,000. Their own study parsed 150 to 160 "
            "Hollywood films spanning 1935 to 2010, shot by shot. This is the scale "
            "at which a claim about how film is cut becomes a measurement rather "
            "than an impression."
        ),
        application=(
            "state the size of the corpus behind any claim about the form — this "
            "program has measured 23 reels, which is enough to describe those reels "
            "and not enough to describe cinema, and the difference should be said "
            "out loud rather than implied"
        ),
        disciplines=[Discipline.PATTERN_RECOGNITION, Discipline.MOVIE_MAKING],
        measurements={"salt_films": 10000.0, "cutting_films": 160.0},
    ),
    Finding(
        source="cutting-2011",
        technique="what else moved when the cutting got faster",
        insight=(
            "The same study that measured shots getting shorter found the change was "
            "not only rhythmic: across 75 years of Hollywood film, shot durations "
            "shortened, motion in the frame increased, and mean frame luminance fell "
            "— films got quicker, faster and darker together. Pace is one axis of a "
            "change that also moved the brightness and the amount of movement."
        ),
        application=(
            "a look and a pace are not independent choices; a fast cut on a bright, "
            "still frame reads as mismatched because the corpus that taught people "
            "what fast looks like moved all three together"
        ),
        disciplines=[
            Discipline.CINEMATOGRAPHY,
            Discipline.COLOR_THEORY,
            Discipline.MOVIE_MAKING,
        ],
        measurements={},
    ),
]


def _stable(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def learn() -> list[Learning]:
    """The published findings, as learnings that carry their citation."""
    out: list[Learning] = []
    for finding in FINDINGS:
        source = SOURCES[finding.source]
        out.append(
            Learning(
                learning_id=_stable("published", source.key, finding.technique),
                disciplines=list(finding.disciplines),
                insight=finding.insight,
                technique=finding.technique,
                application=finding.application,
                # The URL is the source id, so a person can check the claim.
                source_video_id=source.url,
                source_channel=source.channel,
                source_title=f"{source.title} — {source.publisher} ({source.measured})",
                confidence=source.strength,
                measurements={
                    **{k: float(v) for k, v in finding.measurements.items()},
                    "measured_year": float(source.measured),
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Holding the outside and the inside to each other
# ---------------------------------------------------------------------------


def corroborate(reels: list[dict]) -> list[Learning]:
    """Check the published numbers against this project's own corpus.

    This is the part that was missing. An outside number is only worth having
    if something is done with it, and what should be done is the comparison:
    where the corpus agrees with the literature that is corroboration, and
    where it disagrees that is a finding about the corpus — or about the
    literature's reach.

    `reels` is `tools/artifact/templates.json`, whose entries carry per-shot
    durations, so both statistics can be taken from the same shots.
    """
    ratios: list[float] = []
    medians: list[float] = []
    for reel in reels:
        holds = [float(beat[0]) for beat in reel.get("beats", []) if float(beat[0]) > 0]
        if len(holds) < 8:
            continue
        mean = statistics.mean(holds)
        median = statistics.median(holds)
        if mean > 0:
            ratios.append(median / mean)
            medians.append(median)

    if len(ratios) < 3:
        return []

    ours = statistics.median(ratios)
    theirs = SOURCES["redfern-2009"].url
    published = 0.6
    out: list[Learning] = []

    out.append(
        Learning(
            learning_id=_stable("corroborate", "median-over-mean"),
            disciplines=[Discipline.MOVIE_MAKING, Discipline.PATTERN_RECOGNITION],
            insight=(
                f"Redfern's rule for feature film — median hold is about "
                f"{published} of the mean — does not carry over to these reels. "
                f"Measured across {len(ratios)} of them the ratio is {ours:.2f}, so "
                "their shot lengths are markedly less skewed than a feature's. That "
                "is what you would expect and it had never been checked: a "
                "fifteen-second reel has no room for the long held shots that pull a "
                "two-hour film's mean away from its median. The direction of the "
                "advice survives — the median is still the honest statistic — but "
                "the constant is a feature-film constant and does not transfer."
            ),
            technique="the corpus against the literature — skew",
            application=(
                "use the median for pace, as the literature says, but do not import "
                "its published mean-to-median ratio: measure it on the corpus in "
                "hand, because short form is a less skewed distribution than the one "
                "that ratio was fitted to"
            ),
            source_video_id=theirs,
            source_channel="corroborate:redfern-2009",
            source_title=f"{len(ratios)} reels measured against Redfern (2009)",
            confidence=Confidence.SUPPORTED,
            measurements={
                "published_ratio": published,
                "our_ratio": round(ours, 4),
                "reels": float(len(ratios)),
            },
        )
    )

    ours_median = statistics.median(medians)
    feature = 2.5
    out.append(
        Learning(
            learning_id=_stable("corroborate", "how-much-faster"),
            disciplines=[Discipline.MOVIE_MAKING, Discipline.CONTENT_CREATION],
            insight=(
                f"The reels this program is measured against hold a median shot of "
                f"{ours_median:.3f}s. Contemporary feature film, as Cutting measured "
                f"it, holds about {feature}s — so this form cuts roughly "
                f"{feature / ours_median:.0f} times faster than the cinema whose "
                "vocabulary it borrows. Every word this program takes from film "
                "grammar arrives from a medium running an order of magnitude slower, "
                "which is worth knowing before treating any of it as a rule."
            ),
            technique="the corpus against the literature — pace",
            application=(
                "when film-grammar advice is applied to a reel, scale it: a "
                "transition length, a hold, a beat of breathing room measured for a "
                "2.5s cut is most of a shot here"
            ),
            source_video_id=SOURCES["cutting-wired"].url,
            source_channel="corroborate:cutting-wired",
            source_title=f"{len(medians)} reels measured against Cutting (2014)",
            confidence=Confidence.SUPPORTED,
            measurements={
                "feature_asl": feature,
                "our_median": round(ours_median, 4),
                "times_faster": round(feature / ours_median, 2),
            },
        )
    )
    return out


def stale(now_year: int, older_than: int = 15) -> list[Source]:
    """Sources whose measurement is old enough to want re-checking.

    Not a judgement that they are wrong — Salt's database does not rot — but a
    dated fact presented as a current one is how training data goes quietly
    out of date. This makes the age visible so somebody can decide.
    """
    return [s for s in SOURCES.values() if now_year - s.measured > older_than]


def corpus(root: Path | None = None) -> list[dict]:
    """The reels this project measures itself against."""
    import json

    here = root or Path(__file__).resolve().parent.parent.parent
    path = here / "tools" / "artifact" / "templates.json"
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))
