"""Marks on the picture: what to draw, where, and — mostly — whether to at all.

Overlays are the cheapest retention device there is and the easiest to overdo.
A ring around the subject buys attention in the third of a second before the
subject earns it. A bar filling along the bottom tells a viewer the end is
reachable, which is the whole argument against swiping. A sticker in dead space
gives the eye somewhere to rest between cuts.

The same devices, applied without a reason, are the visual signature of content
nobody finished watching. So every proposal here has to point at something
measured: the ring goes where the reading says the eye already went, the arrow
points from empty space at a subject that is genuinely off-centre, and nothing
at all is proposed for a frame whose subject the reading could not find.

Worth stating plainly, because it cuts against the instinct to decorate: the
reference reels this project measures its style against use almost none of this.
Two of the three carry a song credit and nothing else. Overlays earn their place
one at a time or not at all, which is why the defaults here are restrained and
why the sticker pass will not run unless somebody supplies stickers.

**Why every proposal here is binding.** Nothing in any performance export this
project has been given records whether a post carried on-screen graphics — no
overlay, sticker, annotation or marker column exists in any of them. So the
scoring model cannot have an opinion about a ring or a bar, and it does not
have one: adding a graphic moves the prediction by exactly zero, every time.
Run through the crew's usual "does this improve the score?" test, every overlay
would be dropped as *no predicted gain*, which reads as a judgement and is
actually silence. Binding says the honest thing instead — the model abstains,
so the decision goes to the person, which is what the gate is for. Fitting a
coefficient for overlays against a corpus that never measured them would not be
data-driven, it would be making one up.
"""

from __future__ import annotations

from pathlib import Path

from ..edl import EditDecisionList, GraphicCue
from ..insight import FitReport, Prediction
from ..vision import Reading, emptiest_quadrant
from .base import Proposal, Risk

#: Below this the reading did not find a subject, it found a texture. Pointing
#: at a texture is worse than pointing at nothing.
HAS_SUBJECT = 0.18

#: How far off-centre a subject has to be before an arrow is telling the viewer
#: something they would not have worked out unaided.
OFF_CENTRE = 0.16

#: How many stickers may share the screen at once. One is a watermark. Two or
#: three read as a layer over the film, which is the thing the reference reels
#: do and the thing a single sticker per shot cannot ever be. Four is a mood
#: board — at that point the picture is competing with the decoration.
MAX_LAYERS = 3

#: Off the downbeat, fire on every other beat. Firing on all of them at three
#: layers puts something new on screen ten times a second, which stops reading
#: as rhythm and starts reading as noise.
BEAT_STRIDE = 2

#: A ceiling on the whole pass, so a four-minute track cannot turn into two
#: hundred plates of PNG for marks nobody is still watching.
MOST_STICKERS = 36

#: Places a sticker can sit that are not the middle and not the caption band.
#: Scored against the subject at placement time; this is only the shortlist.
LANES: tuple[tuple[float, float], ...] = (
    (0.20, 0.22),
    (0.50, 0.16),
    (0.80, 0.22),
    (0.15, 0.47),
    (0.85, 0.47),
    (0.22, 0.72),
    (0.78, 0.72),
)


def _shot_windows(edl: EditDecisionList) -> list[tuple[float, float, str]]:
    """(start, end, clip id) for every shot on the finished timeline."""
    return [(start, end, shot.clip_id) for start, end, shot in edl.timeline()]


def _clear_of(cue: GraphicCue, existing: list[GraphicCue], gap: float = 0.25) -> bool:
    """Would this graphic share the screen with one that is already there?

    Two marks at once is a busy frame; three is a slideshow template. Time
    overlap is what matters, not position, because the eye only has one place
    to be.

    Applies to the *drawn* marks — the ring, the bracket, the arrow. Stickers
    are deliberately exempt and use `_lanes_free_at` instead: layering is the
    whole point of them.
    """
    return all(cue.start >= other.end + gap or cue.end + gap <= other.start for other in existing)


#: Two stickers closer together than this are one sticker with a shadow.
TOO_CLOSE = 0.14


def _lane_spots(
    reading: Reading | None,
    count: int,
    spec=None,
    avoid: list[tuple[float, float]] | None = None,
) -> list[tuple[float, float]]:
    """`count` places to put a sticker that are not on the subject or on each other.

    The first is the emptiest quadrant, which is the same answer the titles get
    and the best single spot in the frame. The rest are chosen greedily from the
    shortlist to be far from the subject *and* far from the spots already taken,
    because three stickers stacked in one corner is one sticker with a shadow.

    `avoid` is where the stickers *already on screen* are sitting. Without it a
    sticker arriving on the third beat lands on top of one that arrived on the
    downbeat and is still there — the spots are recomputed per beat, so lane
    numbers alone do not keep them apart.
    """

    def apart(spot: tuple[float, float], others: list[tuple[float, float]]) -> float:
        if not others:
            return 1.0
        return min(((spot[0] - o[0]) ** 2 + (spot[1] - o[1]) ** 2) ** 0.5 for o in others)

    taken = list(avoid or [])
    subject = reading.focus if reading is not None else (0.5, 0.5)
    chosen: list[tuple[float, float]] = []
    if reading is not None:
        best_spot = emptiest_quadrant(reading)
        if apart(best_spot, taken) >= TOO_CLOSE:
            chosen.append(best_spot)

    while len(chosen) < count:
        best, best_score = None, -1.0
        for spot in (s for s in LANES if s not in chosen):
            # Distance from the subject matters most; distance from the other
            # stickers stops them clumping. Both, or they pile into one corner.
            score = apart(spot, [subject]) + 1.4 * apart(spot, chosen + taken)
            if score > best_score:
                best, best_score = spot, score
        if best is None:
            break
        chosen.append(best)

    if spec is not None:
        chosen = [spec.safe.clamp(spot) for spot in chosen]
    return chosen[:count]


class OverlayAgent:
    """Draws on the picture, from a reading of it.

    Needs `readings` — one per clip id, from `auteur.vision.read_asset`. Without
    them it proposes nothing, for the same reason the finishing agent does not:
    a mark placed from a default is a mark placed on top of the subject about
    half the time, and a ring around nothing is worse than no ring.
    """

    name = "overlay"
    objective = "retention"

    def __init__(
        self,
        readings: dict[str, Reading],
        *,
        spec=None,
        stickers: list[Path] | None = None,
    ):
        self.readings = readings or {}
        self.spec = spec
        self.stickers = list(stickers or [])
        #: Set by the crew when a Scholar is available. This agent knows where a
        #: subject is; it does not know what to do about footage that has no
        #: subject in it, and that is a question worth asking rather than
        #: silently proposing nothing.
        self.helpdesk = None
        self._asked = False

    def inspect(
        self, edl: EditDecisionList, prediction: Prediction, model: FitReport
    ) -> list[Proposal]:
        if not self.readings or not edl.shots:
            return []

        proposals: list[Proposal] = []
        windows = _shot_windows(edl)
        runtime = edl.duration
        existing = list(edl.graphics)

        # Nothing here works on footage where the eye has nowhere to land: the
        # ring, the arrow and the sticker placement all need a subject. Rather
        # than return an empty list and let that look like approval, ask.
        weak = [r for r in self.readings.values() if r.focus_strength < HAS_SUBJECT]
        if self.helpdesk is not None and not self._asked and len(weak) > len(self.readings) * 0.6:
            self._asked = True
            answer = self._ask_about_flat_footage(weak)
            if answer is not None:
                proposals.append(answer)

        # --- 1. The retention bar ----------------------------------------
        # Only worth it once the film is long enough for "how much is left?"
        # to be a question the viewer is actually asking.
        if runtime >= 8.0 and not any(c.kind == "progress" for c in existing):

            def add_bar(target: EditDecisionList, seconds: float = runtime) -> None:
                target.graphics.append(
                    GraphicCue(
                        kind="progress",
                        start=0.0,
                        duration=seconds,
                        anchor=(0.5, 0.972),
                        move="none",
                        opacity=0.85,
                        note="how much is left",
                    )
                )

            proposals.append(
                Proposal(
                    agent=self.name,
                    title="Run a progress bar along the bottom",
                    reason=(
                        f"At {runtime:.0f}s a viewer deciding whether to stay has no way to "
                        "know how much they are committing to. A bar answers that in "
                        "peripheral vision, without taking a pixel off the subject."
                    ),
                    change=add_bar,
                    objective=self.objective,
                    binding=True,
                    risk=Risk.LOW,
                )
            )

        # --- 2. Bracket the hook -----------------------------------------
        opening = windows[0]
        first = self.readings.get(opening[2])
        if first is not None and first.focus_strength >= HAS_SUBJECT:
            hold = min(1.1, max(0.4, opening[1] - opening[0]))

            def bracket(target: EditDecisionList, where=first.focus, hold=hold) -> None:
                target.graphics.append(
                    GraphicCue(
                        kind="bracket",
                        start=0.12,
                        duration=hold,
                        anchor=where,
                        size=1.15,
                        move="pop",
                        opacity=0.9,
                        note="hook: says where to look",
                    )
                )

            candidate = GraphicCue(kind="bracket", start=0.12, duration=hold)
            if _clear_of(candidate, existing):
                proposals.append(
                    Proposal(
                        agent=self.name,
                        title="Bracket the subject in the opening second",
                        reason=(
                            "The opening frame has a subject at "
                            f"({first.focus[0]:.2f}, {first.focus[1]:.2f}) and roughly a third "
                            "of a second to be found. Corner brackets resolve that before the "
                            "picture has to, which is the whole of the three-second watch rate."
                        ),
                        change=bracket,
                        objective=self.objective,
                        binding=True,
                        risk=Risk.LOW,
                    )
                )

        # --- 3. Ring the strongest subject in the body -------------------
        body = [
            (start, end, clip)
            for start, end, clip in windows[1:]
            if (self.readings.get(clip) or Reading()).focus_strength >= HAS_SUBJECT
            and end - start >= 0.9
        ]
        if body:
            start, end, clip = max(
                body,
                key=lambda w: self.readings[w[2]].focus_strength,  # strongest subject
            )
            reading = self.readings[clip]
            ring = GraphicCue(
                kind="circle",
                start=round(start + 0.15, 3),
                duration=round(min(1.3, end - start - 0.15), 3),
                anchor=reading.focus,
                move="draw",
                opacity=0.9,
                note="the eye already goes here",
            )
            if ring.duration > 0.4 and _clear_of(ring, existing):

                def circle(target: EditDecisionList, cue: GraphicCue = ring) -> None:
                    target.graphics.append(cue)

                proposals.append(
                    Proposal(
                        agent=self.name,
                        title=f"Ring the subject in shot {windows.index((start, end, clip)) + 1}",
                        reason=(
                            f"This is the clearest subject in the film (focus strength "
                            f"{reading.focus_strength:.2f}). A ring drawn around it makes the "
                            "shot legible at a glance, which is what a viewer scrolling gives "
                            "it. Drawn rather than snapped on, so it reads as somebody marking "
                            "the frame rather than a template."
                        ),
                        change=circle,
                        objective=self.objective,
                        binding=True,
                        risk=Risk.LOW,
                    )
                )

        # --- 4. Point at a subject that is genuinely hiding ---------------
        for start, end, clip in windows:
            reading = self.readings.get(clip)
            if reading is None or reading.focus_strength < HAS_SUBJECT:
                continue
            offset = max(abs(reading.focus[0] - 0.5), abs(reading.focus[1] - 0.5))
            if offset < OFF_CENTRE or end - start < 1.0:
                continue
            tail = emptiest_quadrant(reading)
            arrow = GraphicCue(
                kind="arrow",
                start=round(start + 0.2, 3),
                duration=round(min(1.0, end - start - 0.2), 3),
                anchor=tail,
                toward=reading.focus,
                move="draw",
                opacity=0.95,
                note="the subject is not where the eye starts",
            )
            if arrow.duration > 0.4 and _clear_of(arrow, existing):

                def point(target: EditDecisionList, cue: GraphicCue = arrow) -> None:
                    target.graphics.append(cue)

                proposals.append(
                    Proposal(
                        agent=self.name,
                        title="Point at the off-centre subject",
                        reason=(
                            f"The subject sits at ({reading.focus[0]:.2f}, "
                            f"{reading.focus[1]:.2f}), far enough off centre that the eye lands "
                            "somewhere else first. An arrow from the empty side costs a third "
                            "of a second and saves a whole shot."
                        ),
                        change=point,
                        objective=self.objective,
                        binding=True,
                        risk=Risk.MEDIUM,
                    )
                )
                break  # one arrow in a film. Two is a diagram.

        # --- 5. The user's own stickers ----------------------------------
        proposals.extend(self._sticker_proposals(edl, windows, existing))
        return proposals

    def _ask_about_flat_footage(self, weak: list) -> Proposal | None:
        """Put the problem to the Scholar and hand back whatever comesise.

        The answer is advisory and binding: advisory because it changes no
        frame, binding because the scoring model has nothing to say about a
        sentence. It goes to the gate like everything else, which is where a
        person can decide whether the advice is worth acting on.
        """
        from ..scholar.consult import Question

        strengths = [r.focus_strength for r in weak]
        question = Question(
            agent=self.name,
            goal="place marks that draw the eye to the subject",
            problem=(
                f"{len(weak)} of {len(self.readings)} frames have no subject the "
                "reading can find, so a ring or an arrow would be pointing at nothing"
            ),
            evidence={
                "mean focus strength": f"{sum(strengths) / len(strengths):.3f}",
                "threshold": f"{HAS_SUBJECT}",
                "compositions": ", ".join(sorted({r.composition for r in weak})),
            },
        )
        answer = self.helpdesk.ask(question)
        return Proposal(
            agent=self.name,
            title="Asked the Scholar about flat footage",
            reason=f"{question.problem}. {answer.describe()}",
            change=lambda edl: None,
            objective=self.objective,
            binding=True,
            risk=Risk.LOW,
        )

    def _grid(self, edl: EditDecisionList, windows: list[tuple[float, float, str]]):
        """The times to hang stickers on, and whether each one is a downbeat.

        The music's beat grid if the director left one — it does now, which is
        the whole reason this pass can exist. Failing that, the cuts: an edit
        has a pulse whether or not there is a track under it, and landing on
        the cut is the next best thing to landing on the snare.
        """
        beats = list(getattr(edl.music, "beats", []) or [])
        if beats:
            downbeats = {round(b, 2) for b in getattr(edl.music, "downbeats", []) or []}
            return [(b, round(b, 2) in downbeats) for b in beats], "the beat grid"
        # No music. The cuts are the rhythm — every fourth one stands in for a
        # downbeat, which is what a four-bar phrase would have given us anyway.
        return [(start, index % 4 == 0) for index, (start, _, _) in enumerate(windows)], "the cuts"

    def _sticker_proposals(
        self,
        edl: EditDecisionList,
        windows: list[tuple[float, float, str]],
        existing: list[GraphicCue],
    ) -> list[Proposal]:
        """Hang the supplied PNGs on the beat, several at a time, in layers.

        The old rule here was one sticker per shot, at most one on screen at any
        moment, each file used once. That is a caption, not a layer — and it is
        not what the reels being chased do: they carry two or three marks at
        once, arriving on the beat and leaving on the next one, the same few
        images recurring until they read as the film's own vocabulary.

        So: lanes rather than slots. Up to `MAX_LAYERS` stickers live at a time,
        each in a place the reading says the subject is not, each popping onto a
        beat and holding for a musical length. Downbeats get the bigger, more
        opaque arrival; the beats between get something smaller that wiggles.
        The supplied files cycle, because a set of stickers used once each is a
        set of stickers nobody noticed.
        """
        # The crew runs several rounds, and a proposal that does not look at what
        # is already on the timeline gets applied again in each one. Every other
        # pass here is guarded; without this one the stickers doubled every round.
        if not self.stickers or any(cue.kind == "sticker" for cue in existing):
            return []

        marks, grid_name = self._grid(edl, windows)
        if not marks:
            return []

        placements: list[GraphicCue] = []
        #: When each lane next comes free. A lane is a place on screen, so a
        #: lane that is busy is a lane whose sticker is still there.
        free_at = [0.0] * MAX_LAYERS
        since_downbeat = 0

        for index, (at, is_downbeat) in enumerate(marks):
            if len(placements) >= MOST_STICKERS:
                break
            # The hook has one job. Nothing decorates the first half second.
            if at < 0.5:
                continue
            if not is_downbeat:
                since_downbeat += 1
                if since_downbeat % BEAT_STRIDE:
                    continue
            else:
                since_downbeat = 0

            # A downbeat is a hit: several arrive together and hold through the
            # phrase, so they are still there when the beats between them fire.
            # That overlap *is* the layering — one lane filled at a time would
            # just be the old one-at-a-time pass with better timing.
            ahead = marks[index + 1 :]
            if is_downbeat:
                nxt = next((t for t, down in ahead if down), None)
                span = (nxt - at) if nxt is not None else 1.6
                # One lane is deliberately left open, for the beats in between.
                want = MAX_LAYERS - 1
            else:
                span = (ahead[0][0] - at) if ahead else 0.6
                want = 1
            duration = max(0.25, min(2.4, span * 0.95))

            free = [n for n in range(MAX_LAYERS) if free_at[n] <= at + 1e-6]
            if not free:
                continue  # every lane still occupied — this beat gets nothing

            reading = self._reading_at(at, windows)
            live = [cue.anchor for cue in placements if cue.end > at]
            spots = _lane_spots(reading, len(free[:want]), self.spec, avoid=live)
            for slot, lane in enumerate(free[:want]):
                if len(placements) >= MOST_STICKERS or slot >= len(spots):
                    break
                cue = GraphicCue(
                    kind="sticker",
                    start=round(at, 3),
                    duration=round(duration, 3),
                    anchor=spots[slot],
                    # A downbeat is an arrival: overshoot in. The beats between
                    # are already-there things reacting, so they wiggle.
                    move="pop" if is_downbeat else "wiggle",
                    size=1.15 if is_downbeat else 0.85,
                    opacity=1.0 if is_downbeat else 0.82,
                    source=self.stickers[len(placements) % len(self.stickers)],
                    note=("downbeat" if is_downbeat else "beat") + f", layer {lane + 1}",
                )
                placements.append(cue)
                free_at[lane] = cue.end

        if not placements:
            return []

        def place(target: EditDecisionList, cues: list[GraphicCue] = placements) -> None:
            target.graphics.extend(cues)

        names = ", ".join(sorted({c.source.name for c in placements if c.source})[:3])
        layered = self._most_at_once(placements)
        tempo = getattr(edl.music, "tempo", 0.0)
        return [
            Proposal(
                agent=self.name,
                title=f"Layer {len(placements)} sticker hits onto {grid_name}",
                reason=(
                    f"Using {names}, cycling, "
                    + (f"on {tempo:.0f} BPM. " if tempo else "on the cuts. ")
                    + f"Up to {layered} on screen at once, in lanes the reading says the "
                    "subject is not using. Downbeats pop in bigger and hold through the "
                    "phrase; the beats between wiggle in smaller underneath. One sticker "
                    "sitting still through a shot is a watermark — this is the film "
                    "keeping time with itself."
                ),
                change=place,
                objective=self.objective,
                binding=True,
                risk=Risk.LOW,
            )
        ]

    def _reading_at(self, when: float, windows) -> Reading | None:
        """The reading of whatever is on screen at this moment on the timeline."""
        for start, end, clip in windows:
            if start <= when < end:
                return self.readings.get(clip)
        return self.readings.get(windows[-1][2]) if windows else None

    @staticmethod
    def _most_at_once(cues: list[GraphicCue]) -> int:
        """The busiest instant, for the sentence that has to justify it."""
        edges = sorted([(c.start, 1) for c in cues] + [(c.end, -1) for c in cues])
        live = most = 0
        for _, delta in edges:
            live += delta
            most = max(most, live)
        return most
