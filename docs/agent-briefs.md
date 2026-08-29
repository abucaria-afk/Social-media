# Agent briefs

One page per agent: what it owns, what the data says about it, what it is
allowed to change, and where it is known to be wrong. These are written to be
argued with — every number in them came from a named export and can be checked.

Everything here is derived from the corpus in `auteur-exports/`. Re-derive it
after loading new data:

```bash
auteur insight fit ./auteur-exports/*.csv ./auteur-exports/*.jsonl --rows 0
```

---

## What the corpus is, and what it is worth

Thirteen exports, roughly 31,000 rows, in three tiers of trustworthiness.

**Tier 1 — labelled outcomes.** `optimal_viral_workflow_metadata.jsonl` (10,000
wins) and `failed_viral_workflow_metadata.jsonl` (10,000 failures). These are
the only exports that contain failures, which makes them the only ones that can
distinguish a winner from anything else. Every threshold in these briefs comes
from here.

| | winners | failures | boundary |
|---|---|---|---|
| three-second watch | 0.89 | 0.49 | **0.69** |
| completion | 0.84 | 0.28 | **0.56** |
| loop count | 2.36 | 1.08 | **1.72** |
| velocity at 1m | 0.89 | 0.06 | **0.47** |

The boundary is the midpoint between the two medians. Crude on purpose: a
fitted decision boundary would imply a confidence two medians do not support.

**Tier 2 — observed craft data.** `film_theory_virality`, `color_theory_virality`,
`music_theory_virality`, `algorithmic_analysis` (15 rows each). Small, but they
have real spread — three-second watch runs 0.56 to 0.90 — so their correlations
mean something.

**Tier 3 — generated.** `optimized_multimodal_virality_matrix` (400 rows) and
the metadata emulations (100 and 10,000 rows). Useful as targets, not as
evidence. The matrix's columns correlate at r ≈ 1.00 with each other, which is
a curve somebody drew rather than a population somebody measured; the 10k
emulation's median post is watched to completion, so there is no drop-off in it
anywhere to learn pacing from. Both are down-weighted to a tenth automatically
and flagged in every report.

**One trap, worth naming.** The two JSONL files use *disjoint* editing-style
vocabularies — "Cinematic / Seamless" appears only among the wins, "Cinematic
Minimalist" only among the failures. A naive win-rate by style reads 100% and
0%, which is an artifact of two label sets, not a finding about editing. Nothing
in these briefs uses it.

---

## Hook agent

**Owns** `three_second_watch_rate`. **Target** 0.80. **Failure boundary** 0.69.

### What the data says

- Opening shot length correlates **negatively** with three-second watch at
  **r = −0.83** across the observed craft exports. This is the strongest
  observed-backed finding in the whole corpus.
- The winners' first cut lands at **1.2s**.
- Hook styles, by three-second watch: Text Over Blank Screen 82%, Visual Pattern
  Interrupt 76%, Contrarian Statement 66%, Micro-Story 66%, Satisfying/ASMR 59%.
- "Hook Abandonment" is 1,147 of the 10,000 recorded failures, and the fix the
  export recommends is `RE_EDIT_HOOK_REPLACE` — replace the opening, not the film.

### What it may change

Trim the opening shot toward the measured ideal, trimming from the *front* so
the resolved end of the movement survives. Move an existing title to t=0. Argue
for opening on the longest take rather than saving it.

### Where it is wrong

It measures the *shape* of a hook — when the cut lands, whether words are on
screen — and never what the frame contains. A perfectly timed cut to a boring
frame scores identically to a perfectly timed cut to a good one.

---

## Share agent

**Owns** `share_to_view_ratio`. **Target** 0.05. **Completion boundary** 0.56.

### What the data says

- A share costs the sharer something, which is why ranking systems weight it far
  above a like. `Signal.amplification` puts a like at a twentieth of a share.
- Shares grow out of **completion**, not out of a strong first second: winners
  complete at 0.84, failures at 0.28. That gap is nearly three times the gap in
  any other single metric.
- Opening shot length also drags share ratio down (r = −0.63), which is the hook
  objective feeding the share objective rather than competing with it.

### What it may change

Shorten runtime toward 18s by dropping from the tail, never the head or the last
shot. Tighten the middle by a fifth, leaving the hook and the ending alone —
both are load-bearing for the other two agents.

### Where it is wrong

It has no idea whether the film gives anybody a *reason* to send it to someone.
That is the actual mechanism of a share and this agent models the precondition
for it, not the thing itself.

---

## Loop agent

**Owns** `loop_count`. **Target** 1.5. **Failure boundary** 1.72.

### What the data says

- Winners are rewatched 2.36 times; failures 1.08. Proportionally this is the
  widest gap of the four measures.
- A second watch is the cheapest view there is: it costs nothing to produce and
  counts as much as the first, and each one is another completion for the share
  objective to feed on.
- The two things that reliably break a loop are an ending that resolves and a
  fade to black — both of which are what a default template does.

### What it may change

Remove an end card. Return the final shot to the clip the first shot came from.
Force a hard cut into the last shot. Shorten a long tail.

### Where it is wrong

Seam quality is judged from the timeline — same source clip, hard cut in, short
tail — and not from the pixels. Two shots from the same clip can still join
badly, and this cannot see that.

---

## Style agent

**Owns** the match to reference footage. **No target from the corpus** — this one
is deliberately not data-driven.

### What the data says, and why it is overruled

The generated matrix says 9–10 cuts per ten seconds. Reference footage supplied
as "make it more like this" measures **21–43 cuts per ten seconds**, median shot
**0.167 s**, luma 0.11–0.34, low motion between cuts. When those disagree the
reference wins.

> **Corrected.** This brief previously said the references measured 2.7–3.3
> cuts per ten seconds. They do not, and never did. `measure()` sampled at 8
> frames a second with a 350 ms floor on how close two cuts could be, which
> capped any possible reading at under three cuts a second and reported two of
> the three reels — both cutting every four frames — as *meditative*. Re-measured
> at 24 fps with a two-frame floor, the median shot is 0.167 s: exactly a
> sixteenth note at 90 BPM. The old number is left here rather than quietly
> swapped, because a brief that has been wrong once should say so.

That is a deliberate ranking, not an oversight. "I want it to look like this" is
a statement about the work; a correlation across a population is a statement
about a population. A tool that overrules the person holding the camera on the
strength of a correlation is not being data-driven, it is being rude.

The crew still scores the style agent's proposals like anybody else's, so a
style change that wrecks the prediction is dropped.

### What it may change

Hold shots longer or shorter to move the cutting rate toward the reference. It
adjusts screen time rather than dropping material where it can, because dropping
changes what the film is about as well as how it moves.

Note what the corrected measurement does to this. When the references were
thought to cut at 2.7–3.3 per ten seconds the job was to *slow the film down*,
and this section used to say so. They cut at 21–43. The job is the opposite one,
and a reference that fast has a second consequence: at 0.167 s a shot there is
no room for a camera move, which is why those reels hold every frame dead still
and let the cut rate carry all the energy. An agent chasing their pace while
keeping this program's default Ken Burns would get the arithmetic right and the
film wrong.

### Where it is wrong

It measures rhythm and exposure. It does not measure what the footage is of, why
the reference works, or whether the joins are witty. It will happily make a bad
film with the right cutting rate.

---

## Preflight

Not an optimiser — a check against the seven failure modes the labelled data
actually recorded. What matters most here is which ones it honestly cannot see.

| mode | count | recommended fix | checkable |
|---|---|---|---|
| Low Organic Traction | 3,026 | `ARCHIVE_OR_REPURPOSE` | **no** — an outcome, not a cause |
| Bad Aspect Ratio | 1,190 | `RE_CROP_9_16_ASPECT` | yes, before render |
| Corrupt File Upload | 1,170 | `RE_RENDER_AND_REUPLOAD` | yes, after render |
| Flop Schedule Window | 1,166 | `RESCHEDULE_OPTIMAL_PEAK` | yes, before render |
| Shadowban Boundary | 1,160 | `FLAG_COMMUNITY_GUIDELINES_REVIEW` | **no** — invisible from here |
| Hook Abandonment | 1,147 | `RE_EDIT_HOOK_REPLACE` | yes, before render |
| Muted Audio Copyright | 1,141 | `RE_AUDIO_SWAP_TRENDING` | partly |

**On the audio check.** It cannot identify a song — nothing here can. It can
tell whether the bed came from `demo/make_track.py`, which is safe by
construction, or from somewhere else, which is a risk it names rather than
quietly accepts. This is the entire argument for synthesising a bed: *Muted
Audio Copyright* is 11% of recorded failures and it is the one failure mode you
can make structurally impossible.

**Posting windows.** The winners went out at 07, 08, 11, 12, 17, 18, 19, 20 and
21 hundred UTC. Nine hours out of twenty-four is a weak constraint, and the
scheduler treats it as one — a warning, not a refusal.

---

## Overlay agent

**Owns** what is drawn on the picture — rings, arrows, viewfinder brackets,
progress bars, and any transparent PNGs you supply. **No target from the
corpus**, and that is the whole story of this brief.

### What the data says

Nothing. Not "a weak signal" — nothing. No export this project has been given
records whether a post carried on-screen graphics: no overlay, sticker,
annotation or marker column exists in any of them. Adding a graphic moves the
scoring model's prediction by exactly zero, every time.

Run through the crew's usual "does this improve the score?" test, every overlay
would therefore be dropped as *no predicted gain* — which reads as the model
rejecting the idea when it has not looked at it. So these proposals are binding:
the model abstains, and the decision goes to you.

**In every mode, including `autonomous`.** That sentence was true of the brief
and not of the code. `binding` was carrying two opposite meanings — for the
style agent it means *a person already decided*, by supplying reference
footage, so no second approval is owed; here it means the reverse. And
`Gate.needs_a_person` answered on the mode before it looked at the proposal, so
in `autonomous` an overlay was applied with the model having no opinion and
nobody asked. Not auto-approved on the model's say-so: the model had not said
anything. `Proposal.needs_a_human` is the half of `binding` that means *ask*,
and the gate honours it whatever the mode. Fitting a coefficient for
overlays against a corpus that never measured them would not be data-driven, it
would be making one up.

### What it may change

Add graphics, and only where a reading of the frame supports them. A ring goes
where the eye already lands; an arrow is proposed only when the subject is more
than 0.16 off centre, and only one per film, because two is a diagram; stickers
go in the quadrant the subject is *not* in. Without readings it proposes
nothing at all.

### Where it is wrong

It cannot tell whether a mark is beautiful or whether it suits the piece — it
places marks clear of the subject and stops. Worth knowing, and it cuts against
the instinct to decorate: the reference reels this project measures its style
against use almost none of this. Two of the three carry a song credit and
nothing else.

---

## Scholar

**Owns** nothing on the timeline. It studies craft tutorials, keeps what it
learns, and hands it to the crew and to you.

### What the data says

Not applicable — the Scholar's corpus is its own, built from what it watches
rather than from performance exports.

A learning arrives **tentative**, because one person saying something on the
internet is exactly one person saying something. It reaches **supported** when
the same technique turns up from a second, unrelated channel — same channel
twice is one editor's habit, not a convention. It reaches **validated** only
when advice derived from it was applied to a real edit and the prediction moved.

### What it may change

Nothing directly. Its review findings and teaching briefs arrive as binding
proposals — they alter no frame, and go to the gate so a person sees them
beside the edit they apply to.

### Where it is wrong

It reads titles, chapters and captions. It does not watch the picture, so it
knows what a tutorial *said* and nothing about whether the tutorial was any
good. Its filter for which sentences are worth keeping is a list of phrases
that introduce a rule, which is crude and honest about being crude: it will
miss a technique explained without a signpost and will occasionally keep an
"always subscribe".

---

## What the crew remembers

Every run used to start from nothing — the same twenty proposals on the
hundredth film as on the first. A ledger now records one line per scored
proposal: who suggested it, what it was, whether it was applied, and what the
model thought it was worth.

It is used to **order** proposals, never to veto them. Changes with a track
record are tried first, which matters because each applied change alters the
timeline the next one is scored against. An untried idea scores zero rather than
a penalty — untried is not the same as unpromising.

`auteur agents` shows what has earned its place and what keeps being suggested
and turned down. `auteur agents forget` clears it.

**These are the scoring model's own verdicts, not view counts.** A change with a
long positive history is one this program has consistently believed in. That is
worth knowing and is not the same as one shown to work; only real exports
through `auteur insight fit` can say that.

---

## The rule that outranks all of the above

No agent may publish. `Gate.may_publish` requires a person in every mode
including `autonomous`, and a gate with nobody to ask returns *no* rather than
assuming yes. An agent optimising a predicted number will produce something that
scores well and is not what you meant, and the only reliable check on that is
somebody looking at it.
