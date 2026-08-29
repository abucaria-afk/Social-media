# 🎬 Atlas — plan the week, read the reach

![Python CI](https://github.com/abucaria-afk/Social-media/actions/workflows/python-ci.yml/badge.svg?branch=main)
![CodeQL](https://github.com/abucaria-afk/Social-media/actions/workflows/codeql.yml/badge.svg?branch=main)
![License](https://img.shields.io/github/license/abucaria-afk/Social-media)

Plan the week's posts, shoot the shot list, and say the film you want in a
sentence — it cuts and grades it. Afterwards, read back how it did.

The cut is not a template you drop clips into: it reads the footage, decides
what each shot is for, and cuts. A montage comes back at a third of a second a
shot and a hypercut at a sixth, because those are the numbers the reference
reels are cut at, measured rather than chosen.

**Try it without installing anything:**
<https://claude.ai/code/artifact/11666d9b-4b2f-4c15-818b-185262d6cc2a>

That link is the app's own front end with a browser renderer standing in for
ffmpeg, so it cuts a real film from your own camera roll on your own phone.
[VERSIONS.md](VERSIONS.md) says which build is behind the link and — just as
importantly — what the published page cannot do.

It does not post for you and it has no opinion about your follower count. Every
share is something you do.

---

## Three ways in

| | |
| --- | --- |
| **A browser** | The link above. Nothing to install, and it cuts on your device. |
| **A phone** | iOS and Android builds wrap the same front end in a web view. Both are built; neither is submitted yet. |
| **A terminal** | The full ffmpeg pipeline, the agents, the Scholar and the planning board. |

```bash
pip install -r requirements.txt

python -m auteur demo                        # makes practice clips, then edits them
python -m auteur edit ./rushes 'moody neon chase, 20 seconds, "AFTER DARK"'
python -m auteur serve                       # then open the printed address on your phone
```

`serve` is the whole app: making films, a feed of what has been made, messages,
profiles, projects and the planning board — on your machine, in a folder you
chose, with no account on anybody's service.

### At a real address

```bash
docker compose up --build     # then open http://localhost:8000
```

The same server, in a container, so it can go on a host and have a URL. This
matters more than it sounds: the **published link is a single file with no
server behind it**, so it can only ever show the making half — no feed, no
messages, no schedule, because all three need somewhere to keep things. A
container is how you see the app at full capacity without running Python
yourself.

Set `AUTEUR_PUBLIC_URL`, `AUTEUR_PUBLIC_HTTPS` and `AUTEUR_TRUST_PROXY` when it
is reachable from beyond your own machine. Behind a reverse proxy the last one
is not optional — without it the app sees plain http, marks the sign-in cookie
insecure, and the sign-in fails in a way that looks exactly like a wrong
password.

---

## What it does

It measures every clip frame by frame — motion, camera move, focus, exposure,
colour, subject position — derives a beat grid from the music, cuts to it, grades
and matches the shots, mixes the sound, and then **watches its own output back
and re-cuts what it got wrong**. Claude directs when an API key is present; a
full algorithmic director takes over when there isn't one, so the film always
gets made.

See **[AUTEUR.md](AUTEUR.md)** for the full documentation: every command, how
the edit is planned, what the critic measures, and the limitations.

---

## What is called what

Three names, and they are three different things.

**Atlas** is this. It plans the week and reads the reach: a shot list, a
caption, a film cut from your own footage, and the numbers back afterwards.
Paid, at **Solo $12.49/mo** and **Studio $41.99/mo** — every price derived in
`auteur/pricing.py` from a named, dated comparison set and rounded *down*, so
"fifteen per cent under the market" is arithmetic rather than a claim. There is
a 14-day trial and `STUDIO10` takes ten per cent off the top tier. Nothing is
open yet: `pricing.open_for_business()` returns `False` until a live Stripe
link exists, and every page reads that rather than advertising a checkout that
does not answer.

**APX** is the other product — the planner that forgets. Free, no account, no
server, nothing stored; a day's state travels in the link itself. It is not
built from this repository, and nothing here is a mode or a tier of it.

**Auteur Studies** publishes both. It is not a prefix on either: the app is
**Atlas**, never the publisher's name welded to the front of it — which is what
thirty-two files said until a guard in the suite started refusing it. That
guard reads this file too, so the sentence you are reading cannot spell the
compound out; `test_the_publisher_name_is_never_welded_to_the_front_of_the_product_name`
is where it is written down. The entity itself is *not filed yet* —
`COMPANY.entity_filed` is `False`, `COMPANY.publisher` is therefore the trading
name, and a test refuses to let the "LLC" suffix appear on any shipped page,
because a suffix on a privacy policy is a claim about a legal person who does
not exist.

---


## Shipping it

One set of words describes this app. `auteur/brand.py` holds them, and the App
Store listing, the Play listing and the website all generate from it — because
when they were written separately they drifted, and the site spent months
selling a green accent for an app whose accent was teal.

One thing that discipline cannot do, learned the expensive way: it makes every
surface agree, and agreement is not truth. The generated site, both listings
and the app all described this product identically and all described it
*wrongly*, because the sentence they shared was inferred from the code rather
than known — and the live site, which was the only surface with the right
answer, was "corrected" to match the others. A repository can check itself for
consistency. It cannot check itself for accuracy, and a unanimous chorus is
exactly as loud when it is wrong.

```bash
python3 tools/site/build_site.py             # docs/index.html, palette from theme.py
python3 tools/appstore/listing.py            # every App Store Connect field
python3 tools/play/listing.py                # every Play Console field
python3 tools/appstore/preflight.py          # what either store would send back
```

The preflight covers both stores. Play asks two things Apple never does — a
Data safety declaration it will not infer from the binary, and working reviewer
access for anything behind a sign-in — and both are answered in
`tools/play/listing.py`.

Three values have to be yours before either store will take it:
`AUTEUR_BUNDLE_ID`, `AUTEUR_DEVELOPER` and `AUTEUR_SUPPORT_EMAIL`. Until they
are set the preflight fails on them, deliberately: `com.example` is a reserved
domain that Apple and Google both refuse.

---


## How it cuts

A cut is a decision, and for a long time this program made the same one every
time: every join in every film was a hard cut and every shot got the same slow
zoom. Both are defensible once and neither is a choice when it happens on all
of them.

The reference reels say otherwise, measurably. They hold each frame almost
perfectly still — a median frame-to-frame difference of 0.15 to 2.0 out of 255
— and put their energy into the join, which spikes to 100-200. So there are
now eight ways one picture becomes the next:

| join | what it does |
| --- | --- |
| `cut` | nothing, and it stays the commonest — the loud moves mean nothing without it |
| `portal` | an aperture opens in the outgoing frame over its own subject, and the next picture is behind it |
| `carry` | a soft-edged patch of the outgoing subject stays on top of the incoming picture and drifts off |
| `whip` | both frames thrown sideways with a smear, the incoming overshooting |
| `push` | one slides out as the other slides in |
| `luma` | the outgoing frame dissolves through its own highlights |
| `slice` | horizontal bands of the two shear apart |
| `flash` | a frame or two of blown white, or of the outgoing frame inverted |
| `match` | the incoming shot starts at the outgoing framing and eases to its own |

and seven things a shot can do while it is on screen — including **hold**,
which does nothing at all and is the most common answer in the references.

### The decades

`--era 1990s`, or the decade chooser in the app. Real per-pixel work rather
than a CSS filter string: tone curves, split toning, halation, grain, chroma
bleed and scanlines, in six recipes from Super 8 to phone HDR. Affordable
because a photograph is graded once — a clip pays per frame and gets the colour
but not the texture, which is stated where it happens rather than quietly
delivered as a different look.

This mattered more than it sounds. Measured against the ungraded photograph,
the look every unmatched prompt used to fall through to moved the picture by
6.6 parts in 255, which is below the threshold where anybody would call it
graded. `tools/artifact/check_grade.py` measures that rather than asserting it.

### Templates

Every reference reel is read shot by shot — where each cut falls across the
whole runtime, and how bright and saturated it was at each one. Choosing one
cuts *your* pictures to *its* timing rather than to an average of its speed,
which matters because two reels with the same median hold can be a steady pulse
and a burst-then-rest. The app draws that shape so you can tell them apart.

`auteur template ./a-reel.mp4 ./my-photos` from the command line; a tab of
their own in the app, where you can also hand it a reel of your own. An
uploaded reel is measured and then deleted — what is kept is where the cuts
fall, which is the only part anybody cuts to.

---

## Workflows

`edit` makes a film. A **workflow** makes a *post*: it cuts to the length the
surface accepts, keeps the titles out from under the app's own buttons, pulls a
cover frame that is not the first frame, drafts a caption inside the character
limit, and writes a `post.json` you or a scheduler can read.

| workflow | where it goes | shape | runtime |
| --- | --- | --- | --- |
| `instagram-reel` | Instagram Reels | 1080×1920 | 3–180s |
| `instagram-post` | Instagram feed | 1080×1350 | 3–60s |
| `instagram-story` | Instagram Stories | 1080×1920 | 1–60s |
| `tiktok` | TikTok | 1080×1920 | 3–600s |
| `tiktok-photo` | TikTok photo mode | 1080×1920 | 3–60s |
| `youtube-short` | YouTube Shorts | 1080×1920 | 1–180s |

Alongside them, `media` is a footage index — scan a folder once, and afterwards
only what changed is re-read; duplicates are found by content and confirmed byte
for byte before anything is called a copy. `schedule` is a queue with the two
rules that matter: a minimum gap between posts to the same service, and a
ceiling per day.

**Nothing posts for you.** There is no Instagram or TikTok API call anywhere in
this repository. A workflow produces a folder you can post from in a minute, and
the queue says which one is next.

---

## Insight and agents

`auteur insight` reads performance exports — eleven column shapes, recognised by
their headers — and reports what the posts that travelled had in common. Three
agents then work on the planned timeline before a frame renders, one objective
each:

| | measure | target | what the agent does |
| --- | --- | --- | --- |
| **hook** | `three_second_watch_rate` | > 0.80 | shortens the opening, lands the title before the first cut |
| **share** | `share_to_view_ratio` | > 0.05 | argues about runtime and pace, because shares grow out of completion |
| **loop** | `loop_count` | > 1.5 | removes end cards, returns the last shot to the opening frame |
| **gaze** | — | — | exposure, temperature and contrast continuity across the cut |
| **finishing** | — | — | reframes onto the measured subject, moves words off it, picks each join |
| **overlay** | — | — | rings, arrows, brackets, a retention bar, and your own stickers |
| **scholar** | — | — | reviews the cut with what it has studied, and teaches the rest |

Every proposal is applied to a copy and kept only if the overall prediction
improves, so an agent can be confidently wrong and lose nothing but a round.

**No mode lets an agent publish.** A gate with nobody to ask returns *no* rather
than assuming yes. Autonomy means an agent may restructure a cut without being
asked; it never means it may post one.

The scoring is deliberately suspicious of its own inputs: derived fields never
claim to have been measured, exports whose columns track each other too neatly
to be observations are down-weighted, disagreements between sources are surfaced
rather than averaged away, and a corpus with no failures in it is told it has
none. With no data at all it fits on simulated rows and says so every time.

That suspicion runs to the file itself. A CSV somebody points at is not evidence
it was measured from anything, and no check can tell a careful fake from a real
export — so the report says **where the rows came from** rather than calling
them measured: *"5 rows from your exports and 2000 simulated ones"*. It used to
say "5 measured rows", which is a claim about the world this program has no way
to check.

`--reference ./refs/*.mp4` measures footage you point at and pulls the edit
toward its cutting rate and exposure. A reference **outranks the corpus**: style
proposals are binding, so they skip the "does this improve the prediction?" test
that would otherwise let a population-level correlation overrule your own eye.

With a labelled outcome export loaded, every render is also checked against the
seven failure modes the data recorded — and told which two it cannot see.
See **[docs/agent-briefs.md](docs/agent-briefs.md)**.

`auteur serve` exposes all of this at **/studio** — pick a destination, see the
prediction and the retention curve, approve or reject each proposal. The studio
and the CLI build the same crew from the same function, so the page that shows
you what the agents want can never show a shorter list than the CLI would act on.

### What the crew remembers

`auteur agents` — every scored proposal is recorded across runs, so the crew
learns which changes earn their place in *your* films and tries those first.
Ordering only; it never vetoes, and an untried idea is not penalised for being
new. These are the scoring model's own verdicts, not view counts.

### The Scholar

`auteur scholar` — a study agent that watches craft tutorials (metadata,
chapters and captions; it never downloads video), keeps what it learns, and
hands it to the crew. A technique arrives *tentative*, reaches *supported* when
an unrelated channel teaches the same thing, and *validated* only once advice
derived from it was applied to a real edit and the prediction moved.

It also reads reels directly, and then says what the shelf as a whole shows.
That second step is the one that matters: per-film learnings describe a moment
inside one reel — *this one holds 0.08s before its first cut* — and none of
them describes the form. With a hundred and twenty of those filed it still
could not answer "how fast do the reels cut?", because the word *hypercut*
appeared in none of them. It now generalises over its own measurements and
writes the result down in the words people type: hypercut, the hook, pacing,
grading, holding still, how long a reel runs. Twenty-three reels agreeing makes
those *validated* where each source alone was tentative.

Ask it from the phone at **/ask**. Every answer carries where it came from and
how many films stand behind it, and when nothing it has studied touches the
question it says so rather than returning the nearest paragraph.

`auteur serve` studies in the background while the app is up. Studying from
YouTube needs `pip install auteur[scholar]`; without it, it says so rather than
reporting an empty success.

### Overlays and stickers

`--stickers ./my-stickers` — drop transparent PNGs in a folder and they are
placed in the quadrant the subject is *not* in, and animated. Rings, arrows,
viewfinder brackets, progress bars and torn tape are drawn from primitives with
Pillow, so nothing is fetched and nothing carries a licence.

Nothing in any export records whether a post carried on-screen graphics, so the
model has no opinion about them and does not pretend to: overlay proposals are
binding and go to you rather than being silently dropped as *no predicted gain*.

---

## Development

```bash
pytest -q                    # the suite; synthesises its own footage
python tests/fuzz.py         # ten thousand randomised property checks
ruff check .                 # the linter CI runs
black --check .              # the formatter CI runs — a different tool

pip install pre-commit && pre-commit install
```

CI runs **both** `ruff check .` and `black --check .`, over the whole tree
rather than over `auteur` and `tests` alone — `tools/` is linted too. They are
not the same tool and they disagree: `ruff format` reformats files black then
wants changed back, so run `black .` and never `ruff format`. A test compares
this block to the workflow, because this is exactly the paragraph that goes
stale and the person it strands is whoever is contributing for the first
time.

### Layout

```
auteur/            the package
  analysis/        what it sees in the footage
  director/        who decides the shots (Claude, or the built-in editor)
  craft/           grammar, motion, colour, transitions, sound, titles, graphics
  vision/          reading a frame the way a picture is read, not measured
  workflows/       platforms, the media index, post packaging, the queue
  insight/         performance schemas, the loader, the simulator, the scorer
  agents/          the crew, the shared builder, the ledger, the approval gate
  scholar/         the study agent: what it watches, keeps, and teaches
  publish/         linking an account, and what that does and does not mean
  gallery/         public-domain footage: search, curate, fetch
  training/        a generator for practice data with knowable ground truth
  web/             the phone app: stdlib-only server and static front end
  theme.py         the one palette, read by the app, the icons and the terminal
tools/artifact/    the published page, and the checks that measure it
demo/              make_footage.py — synthetic clips for a first run
tests/             test_auteur.py (pytest) and fuzz.py (property campaign)
.github/workflows/ CI: tests, python-ci, lint, coverage, CodeQL, pip-audit
```

`insight/template.py` reads a reel shot by shot. `tools/artifact/` builds the
published page and holds the checks that measure it against real footage rather
than asserting it works — the grade against the ungraded photograph, the cut
against the frames that came out, both renderers against each other, and every
attachment against the part of the program meant to read it:

```bash
python3 tools/artifact/build_artifact.py            # the published page
python3 tools/artifact/check_grade.py ./photos      # does the grade change anything
python3 tools/artifact/check_cutting.py page ./pics # are the joins real
python3 tools/artifact/check_eras_match.py ./photos # do both renderers agree
python3 tools/artifact/check_attachments.py ./stuff # can it still read all of it
python3 tools/artifact/walkthrough.py URL ./photos  # the whole app, photographed
python3 tools/artifact/ask_scholar.py URL           # what the Scholar says back
```

### Known gaps

Stated here rather than discovered.

The two renderers agree about a decade now, within about 3 levels out of 255,
and the desktop path has the same joins the browser one does — `check_joins.py`
and `check_eras_match.py` both pass. What is still only in the browser is the
per-shot *salience* read: the desktop path frames a shot from its subject track
rather than from an edge map, so a portal opens on the centre of the frame
rather than on whatever the eye lands on.

Sign-in with Apple is present and cannot work on an ordinary install: its
client secret is a JWT signed with a key Apple issues, which needs a crypto
library this project does not depend on, and it requires an https redirect on a
domain registered with them. The sign-in page says so rather than offering a
button that fails. Google needs only a client id.

The iOS app in `ios/` has never been compiled — there is no Mac here. Every
check that can be made without one is made by the test suite, and `ios/README.md`
lists what is most likely to need fixing first.
