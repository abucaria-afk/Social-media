"""Structure above the beat: phrases, landings, and one held shot.

Every decision this editor made was a function of one number. `slot.energy`
comes from `brief.energy_at(position)`, a smooth curve over the film, and it
chose the shot length, which take went there, the speed, the camera gesture and
the transition. Five decisions, one input, all moving together — which is the
definition of a film where nothing surprises you, because every choice is the
same choice wearing a different hat.

Measured on the output, before this module existed:

    a 20s montage      61 shots, 3 distinct lengths, longest/median 2.00
    a 15s hypercut     25 shots, 3 distinct lengths, longest/median 2.00
    a 24s cinematic    25 shots, 6 distinct lengths, longest/median 2.00

Exactly 2.00 in all three, from three different briefs, which is not a
coincidence — it is a ceiling. `shot_length_at` says in its own docstring that
the arc "spans roughly 4:1, which is the difference between a held beat and a
flurry", and then the beat quantiser rounds every slot to one or two grid
units and the range collapses. So no shot in any film was ever emphasised, and
a 61-shot montage was built out of three lengths. That is a metronome, and a
metronome is what "computer-generated" sounds like.

What this adds is the thing an editor does that a curve cannot: **grouping**.

**Phrases.** Shots come in groups of three to five that end on a longer one.
This is how prose works and it is why prose has rhythm — short, short, short,
then a sentence that lands. A film cut in a continuous stream of equal shots
has no sentences, only syllables. The landing is not decoration; it is what
makes the shots before it read as a unit rather than as a queue.

**One hold.** Somewhere near the film's peak, a single shot much longer than
anything around it, and — this is the part that matters — *chosen for
stillness rather than for motion*. The old scoring did the exact opposite:
`(1.0 - abs(motion_norm - slot.energy))` rewarded a busy shot when the curve
was high, so the loudest moment got the busiest picture. That is redundancy,
not editing. The power of the withheld cut is that everything around it is
fast: the same still frame is unremarkable in a slow film and enormous after
twelve quick ones. Cutting *against* the curve at one chosen moment is worth
more than cutting with it everywhere.

**A rhyme.** The last phrase returns to something from the first, changed. It
costs one shot and it is the single cheapest way to make an edit feel authored,
because it is proof that the film remembers its own beginning. Nothing else
here says "somebody decided this" as loudly.

None of these are style settings. They are the shape a film has when a person
cut it, and their absence is what the eye reads as a machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: Shots per phrase. Three to five, because two is not a group and six stops
#: being heard as one — the same reason a list in prose runs to three or four
#: items before it needs punctuation to stay a list.
PHRASE_MIN = 3
PHRASE_MAX = 5

#: Where the held shot goes, as a fraction of the film. Not at the exact peak:
#: slightly after it, because a hold that arrives before the build has finished
#: reads as a stall, and one that arrives just past the top reads as a landing.
HOLD_AT = 0.68

#: A film shorter than this has no room for structure — it is one phrase, and
#: imposing a hold on it would spend a third of the runtime on one frame.
LEAST_SHOTS_FOR_A_HOLD = 9

#: How long the last phrase's rhyme reaches back. The opening two shots are
#: what a viewer actually remembers, so those are what can be rhymed with.
OPENING_SHOTS = 2


class Beat(Enum):
    """What a shot is *for*, which is a different question from how long it is.

    The director already knew where every shot went. It never knew what any of
    them was doing, and a shot with no job is a shot that can be swapped for any
    other — which is why the films read as interchangeable.
    """

    #: The first shot. Its whole job is to be legible: somebody has to know what
    #: they are looking at before anything can be done to them. It is *not*
    #: stretched — see STRESS.
    OPEN = "open"
    #: The first hard change, early and deliberate. A film that opens and then
    #: continues is a film somebody scrolls past; the interrupt is the moment it
    #: stops being what it looked like. This is King's opening move — the
    #: ordinary, and then the thing that is wrong with it — and it has a
    #: measured window rather than a feeling. See INTERRUPT_WINDOW.
    TURN = "turn"
    #: The ordinary shots inside a phrase. Most of the film is these, and that
    #: is correct — emphasis is only emphasis because most things are not.
    BUILD = "build"
    #: The last shot of a phrase, longer, so the phrase reads as a sentence
    #: rather than as more syllables.
    LANDING = "landing"
    #: The one long, still shot. See the module docstring.
    HOLD = "hold"
    #: A late return to something from the opening.
    RHYME = "rhyme"
    #: The last shot, which needs somewhere to put the viewer down.
    CLOSE = "close"


#: Where the first hard change has to land, in seconds. Not invented here: this
#: is the `pattern-interrupt` rule from the APX craft-rules work, which fires
#: when the first hard change falls outside this window and again when there is
#: no hard change at all before the upper bound.
INTERRUPT_WINDOW = (1.8, 3.8)

#: The longest the opening shot may hold before something changes. Also from
#: that work, as `hook-length`, and it is the rule that corrected this module:
#: the first draft here gave the opening a 1.6x stretch on the reasoning that an
#: establishing shot needs room. That is true of a feature and false of
#: short-form, where holding the first shot is the single most common way to
#: lose somebody. An opening can be the liveliest shot in the film and still
#: lose the viewer by sitting on it.
OPENING_HOLD_LIMIT = 2.0

#: How much longer than the plain shot length each job runs. These are
#: multipliers on what the arc already asked for, so a fast film stays fast and
#: a slow one stays slow — what changes is the *ratio* between its own shots,
#: which is the thing that was stuck at 2.00.
#:
#: The hold at 5 is the number that does the work. It has to clear the landing
#: at 2 by enough that nobody reads it as another landing; four times the
#: median is roughly where a shot stops feeling like part of the rhythm and
#: starts feeling like a decision.
STRESS: dict[Beat, float] = {
    # Deliberately below 1, and this number has two independent sources that
    # agree against the first draft of this file.
    #
    # Ask the Scholar "what makes an opening hold a viewer": across the 24
    # reference reels the opening shot is held **0.12s** before the first cut,
    # and 22 of the 24 cut inside half a second. Against a montage median of
    # 0.334s that is a stress of about 0.36. The APX craft rules reach the
    # same conclusion from the other end, firing `hook-length` whenever the
    # opening holds past 2.0s.
    #
    # 0.6 rather than the measured 0.36, stated rather than quietly split: the
    # corpus is reels, and this app also cuts 24-second pieces where 0.36 of an
    # already-long shot is still a flash frame that reads as a glitch. The
    # direction is the corpus's; the magnitude is pulled back for the briefs
    # the corpus does not cover.
    Beat.OPEN: 0.6,
    Beat.TURN: 1.0,
    Beat.BUILD: 1.0,
    Beat.LANDING: 2.0,
    Beat.HOLD: 5.0,
    Beat.RHYME: 1.5,
    Beat.CLOSE: 2.5,
}


@dataclass(frozen=True)
class Structure:
    """The job of every shot in the film, worked out before any are chosen."""

    beats: tuple[Beat, ...]
    #: Index of the held shot, or None in a film too short to hold anything.
    hold: int | None
    #: Index of the rhyming shot, or None.
    rhyme: int | None

    def __len__(self) -> int:
        return len(self.beats)

    def at(self, index: int) -> Beat:
        if 0 <= index < len(self.beats):
            return self.beats[index]
        return Beat.BUILD

    def stress(self, index: int) -> float:
        return STRESS[self.at(index)]

    def describe(self) -> str:
        counts: dict[str, int] = {}
        for beat in self.beats:
            counts[beat.value] = counts.get(beat.value, 0) + 1
        return ", ".join(f"{n} {name}" for name, n in counts.items())


def _phrase_lengths(count: int, rng) -> list[int]:
    """Break `count` shots into phrases of three to five.

    Uneven on purpose. Phrases of a constant length are a longer metronome,
    which is the fault this exists to fix rather than a smaller version of it.
    """
    lengths: list[int] = []
    left = count
    while left > 0:
        if left <= PHRASE_MAX:
            lengths.append(left)
            break
        want = rng.randint(PHRASE_MIN, PHRASE_MAX)
        # Never leave a runt: a trailing phrase of one or two shots has no
        # room for a landing, so the film would end mid-sentence.
        if left - want < PHRASE_MIN:
            want = max(PHRASE_MIN, left - PHRASE_MIN)
        lengths.append(want)
        left -= want
    return lengths


def shape(count: int, rng, *, arc: str = "hook-drop") -> Structure:
    """Give every one of `count` shots a job.

    `rng` is the director's own generator, passed in rather than made here, so
    a film is reproducible from its seed — the same brief and the same footage
    have to give the same cut or nothing about the edit can be tested.
    """
    if count <= 0:
        return Structure(beats=(), hold=None, rhyme=None)
    if count == 1:
        return Structure(beats=(Beat.OPEN,), hold=None, rhyme=None)

    beats = [Beat.BUILD] * count
    beats[0] = Beat.OPEN
    # The turn: the first hard change, placed as early as the second shot so it
    # lands inside the interrupt window on any pace this app cuts at.
    if count >= 4:
        beats[1] = Beat.TURN

    # Landings: the last shot of every phrase.
    at = 0
    for length in _phrase_lengths(count, rng):
        at += length
        end = at - 1
        if 0 < end < count - 1:
            beats[end] = Beat.LANDING

    beats[-1] = Beat.CLOSE

    # The hold. Placed just past the arc's peak, and never on the first or last
    # shot: a film that opens on a held frame has not earned it, and one that
    # ends on it has a close already doing that job.
    hold: int | None = None
    if count >= LEAST_SHOTS_FOR_A_HOLD:
        # A trailer peaks late and a crescendo later still, so the hold moves
        # with the arc rather than sitting at a fixed fraction for all of them.
        where = {"trailer": 0.74, "crescendo": 0.80}.get(arc, HOLD_AT)
        hold = min(count - 2, max(2, int(round(count * where))))
        beats[hold] = Beat.HOLD

    # The rhyme: one shot in the last phrase that reaches back to the opening.
    # Never adjacent to the hold — two consecutive shots both asking to be
    # noticed cancel each other out.
    rhyme: int | None = None
    if count >= LEAST_SHOTS_FOR_A_HOLD:
        candidate = count - 2
        if beats[candidate] is Beat.BUILD and (hold is None or candidate - hold >= 2):
            beats[candidate] = Beat.RHYME
            rhyme = candidate

    return Structure(beats=tuple(beats), hold=hold, rhyme=rhyme)


def wants_stillness(beat: Beat) -> bool:
    """Where the edit should cut *against* the energy curve rather than with it.

    Only the hold, and only because it is the one place the contrast can be
    heard. Doing this everywhere would just invert the old fault: a film that
    is uniformly against its music is as predictable as one uniformly with it.
    """
    return beat is Beat.HOLD


def opening_is_too_long(first_shot_seconds: float) -> bool:
    """Whether the opening holds past the point people leave.

    Kept here as a function rather than as a bare comparison at the call site,
    because a threshold with no name is a magic number and this one has a
    source.
    """
    return first_shot_seconds > OPENING_HOLD_LIMIT


def interrupt_is_late(first_change_seconds: float) -> bool:
    """Whether the first hard change misses its window."""
    low, high = INTERRUPT_WINDOW
    return not (low <= first_change_seconds <= high)


def wants_legibility(beat: Beat) -> bool:
    """Shots that have to be *read*, not just seen.

    The opening, because nobody can be moved by a picture they have not
    understood yet, and the hold, because it is on screen long enough for a
    viewer to notice anything wrong with it.
    """
    return beat in (Beat.OPEN, Beat.HOLD)
