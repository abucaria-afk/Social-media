# Atlas — company brief

**One file. Everything a collaborator outside this repository needs in order to
help build the company around the product.**

Last updated 2026-08-28. Product state verified against a 587-test suite and a
real browser at a 390×844 phone viewport on the same day.

**The umbrella corp is Auteur Studies LLC, on auteurstudies.com.** That is
decided and now carried by the code — see §5.1. What it is still waiting on is
§5.1a.

---

## 0. To the AI reading this

You are being asked to help with **the company**, not the code. The software
exists, works, and is being built by someone else. Your side of the line is
everything that needs a human with a bank account, a legal name and a signature:
entity, publisher identity, developer accounts, platform approvals, hosting,
pricing, positioning, launch.

Four rules, because the value of this file is that it is true:

1. **Do not invent facts about the product.** Everything in sections 1–4 was
   read out of the running code. If you need a product fact that is not here,
   say so and ask — do not fill the gap with a plausible guess.
2. **Say when something has changed under you.** Prices, store policies and API
   gates move. Every external figure below is dated. Treat an undated claim of
   yours as a claim you have to check.
3. **Never ask for, and never repeat, a credential.** No passwords, tokens or
   client secrets belong in a chat transcript, a document, or a repository. See
   §7.
4. **Distinguish a decision from a task.** A decision needs the founder's
   judgement and should be put to them as a question with options and a
   recommendation. A task is work you can draft. Label which is which.

The most useful thing you can produce is in §9.

---

## 1. What the product is

**Atlas** — it plans the week and reads the reach. A shot list you can actually
shoot, a caption to go with it, a film cut from your own footage, and
afterwards the numbers back. It behaves like a social app, not a timeline tool.

> Plan the week's posts, shoot the shot list, and say the film you want in a
> sentence — it cuts and grades it. Afterwards, read back how it did.

**Corrected 2026-08-29, by the owner.** This document, the app, both store
listings, the generated site and the README previously led with "a film from
your camera roll" — the cutting, alone. Every surface agreed, and every surface
was wrong in the same way: cutting is one of four steps and the most visible
one, not the one somebody is buying. Worth recording as a method note, not just
a copy change: this repository can prove its surfaces agree with each other and
cannot prove the sentence they agree on is true, because that fact lives with
the person who decided it. A unanimous chorus is exactly as loud when it is
wrong.

**APX** is the second product, and it is a real one — free, no account, no
server, nothing stored, a day's state carried in the link itself. The planner
that forgets. It is not built from this repository. It is also not a mode or a
tier of Atlas, and the two are standalone brands: **Atlas**, never "Auteur
Atlas". (The craft rules cited throughout `auteur/craft/story.py` share the APX
name and are a *feature of Atlas*, which is exactly the collision that led an
earlier pass here to conclude APX was not a product at all.)

### What the live site actually says

Read through the Wix API on **2026-08-31** — the saved revision, which is what
the SEO API returns for a static page; unpublished edits would not show.

- One site, `www.auteurstudies.com`, published, custom domain. **One page.**
- Title: *"Auteur Studies — Atlas and the craft rules"*
- Description: *"Auteur Studies makes tools for short-form video. Plan the
  week, check the craft before the reel goes out, and read the reach without
  inventing a number."*
- Focus keywords: `daily planning app`, `Auteur Studies`, **`Auteur Atlas`**,
  `short form video craft`

Three things follow, and the site is public, so they are public problems:

1. **No LLC claim anywhere in the metadata.** That constraint is holding.
2. **`Auteur Atlas` is a live focus keyword** — the one construction that is
   never the product's name. It is buying search traffic for a name that does
   not exist.
3. **APX is gone from the description.** On 2026-08-29 it read *"Atlas plans
   the week and reads the reach, APX checks the craft before the reel goes
   out."* The pass that wrongly concluded APX was fictional removed it, the
   owner corrected the repository, and **the correction never reached the
   site**. The live page sells one product where there are two.

Also unresolved: there is no privacy or terms page — `PRIVACY.md` and
`TERMS.md` are published nowhere, while **Wix Forms is installed**, and both
app stores require a hosted privacy URL before they will take a submission.
And `atlasauteurstudies.com` is not in this Wix account, which holds one site.

**Auteur Studies** publishes both. The entity is **not filed** —
`COMPANY.entity_filed` is `False`, so `COMPANY.publisher` resolves to the
trading name and a guard refuses to let the "LLC" suffix onto any shipped page
until a state says otherwise.

The distinguishing claim, and the one worth protecting in every piece of
marketing: **it is not a template you drop clips into.** It reads the footage,
decides what each shot is *for*, and cuts. A montage comes back at a third of a
second a shot and a hypercut at a sixth, because those are the numbers the
reference reels are cut at — measured, not chosen. It builds structure above the
beat: an opening that holds, a phrase that lands, one shot that stays still
while everything around it moves, and a rhyme back to the first image near the
end.

### The features, in the words the app itself uses

| Headline | What it means |
| --- | --- |
| Say it in a sentence | Describe the film the way you would to a person. |
| Cut to a rhythm | Montage ≈ 0.33s/shot, hypercut ≈ 0.17s/shot, from measured reference reels. |
| Graded for a decade | Super 8, VHS, Kodak, Y2K flash, faded 2010s — the grade genuinely moves the picture. |
| Type and stickers on the beat | Anything written "in quotes" lands on screen, on the cut. |
| Every shape a platform wants | Vertical, square, portrait, wide — at each surface's recommended runtime. |
| It runs on your device | No third-party analytics, no ad identifier, no third-party code. |
| A feed that learns, on your own machine | An instance ranks by how long its films are actually watched. |
| A feed, if you want one | Run a copy on your own computer and the app connects to it. |

### The shape of the app

Five tabs, modelled on the two apps everybody already knows how to use:

- **Feed** — the films made on this instance (Everyone / Following / Yours).
- **Schedule** — the social-media-manager half: plan a post before the footage
  exists, connect TikTok and Instagram to read back how posts did, subscribe the
  board to a real calendar.
- **Create (+)** — does not navigate, it *opens*: Make a film / Cut to a
  template / Type and stickers / Meet the crew.
- **Messages** — person to person, on the instance.
- **You** — profile, and the Studio (projects, crew, plan board) behind it.

---

## 2. What is actually built and verified

Everything here has a test behind it, and anything visual has been checked in a
real browser at a real phone size. This project's rule is that claims about
behaviour are checked by running the thing.

- **The editor.** Full pipeline: ingest → analysis → director → craft (grammar,
  motion, colour, transitions, sound, titles, graphics) → render → self-review.
  Two renderers — ffmpeg and an in-browser one — held to the same cutting
  behaviour by a test that compares them.
- **The app.** Sign-in with password reset and optional two-step, light and dark
  themes, accessibility settings that are real app-wide, offline-capable PWA,
  installable to an iPhone home screen.
- **The social layer.** Feed with watch-time ranking, following, profiles at
  shareable `/u/<name>` addresses, direct messages with unread badges, blocking,
  reporting, and account deletion that actually takes everything with it.
- **The Schedule tab.** Planning board, calendar subscription, TikTok and
  Instagram connection flow with tokens stored `0600` in a file separate from the
  connection list.
- **Store readiness.** App Store submission pack, Google Play listing, generated
  screenshots, a 12+ age gate with a lockable content restriction, published
  privacy policy and terms.
- **Engineering hygiene.** 587 tests green. CI runs pytest, ruff, black, CodeQL,
  pip-audit and coverage on every push. MIT licensed.
- **Deployment.** `Dockerfile`, `docker-compose.yml`, `render.yaml` and
  `fly.toml` are all in the repository and all point at the same container.

---

## 3. What is *not* built

Plan against this list, not against optimism.

- **Nothing is deployed.** There is no live instance at a public address. The
  hosting files exist; nobody has clicked the button. (§5.3)
- **No native iOS or Android binary has been submitted.** The pack is written;
  no build has been uploaded to either store.
- **No real platform numbers.** TikTok and Instagram connection works, but until
  approved developer applications exist, every insight figure is a simulation.
  The app says so on screen rather than showing invented charts. (§5.4)
- **No search.** You cannot find a person by typing their name — the instance
  lists everybody, which is fine for tens of people and wrong for thousands.
- **No push notifications** beyond an app badge on an installed PWA.
- **No payment, subscription or billing of any kind.**
- **No entity yet, and no domain yet.** The name is decided — Auteur Studies
  LLC on auteurstudies.com — and the code carries it, but nothing has been
  filed and nothing has been registered. No bank account. (§5.1a)
- **Single-instance by design.** Every deployment is somebody's own server.
  There is no multi-tenant hosted product, and whether there should be is the
  biggest open strategic question in this document. (§8.1)

---

## 4. The privacy position — read before writing any marketing

This is the product's spine and it is unusually strict. It is also a liability
if a single piece of copy overstates it, because an inaccurate Google Play Data
safety declaration is a policy strike, not a correction.

What is true today, exactly:

- **There is no "we".** Nobody operates a service. There is no company server
  that receives anything.
- **No third-party analytics, no advertising identifier, no third-party code.**
- **The feed and messages need a server — and it is one the user runs**, on
  their own machine, holding files in a folder they chose. So the honest
  sentence is not "nothing leaves your phone", it is **"nothing goes anywhere
  you did not put it."** Marketing must use the second sentence.
- **One exception, deliberately:** the user may connect a TikTok or Instagram
  account to read back how a post did. Consent happens on the platform's own
  site. The scopes requested are **read-only** — publishing is a separate
  permission on both platforms and this app does not request it, so it *cannot*
  post as anybody even if something went wrong. Disconnecting deletes the token.
- **The iOS app in aeroplane mode makes no network requests at all.** That is
  the simplest way to verify the claim, and it is a good demo.

**Rule for all copy:** if a sentence about privacy is not on this list, it does
not ship until it has been checked against the code.

---

## 5. The company work — this is your side

### 5.1 Publisher identity — decided, and now in the code

**The umbrella corp is Auteur Studies LLC, on auteurstudies.com.** As of
2026-08-28 that is no longer a placeholder anywhere: the repository carries it,
the App Store and Play listings regenerate from it, the in-app privacy and terms
pages name it, the iOS project's bundle identifier is built from it, and the
LICENCE copyright line reads it.

The structure matters and is worth keeping. A **company** publishes; a
**product** is what it publishes. The company owns the legal name, the domain,
the support address, the policy documents and the copyright line — a second
product under the umbrella inherits all of them. `auteur` owns only its bundle
identifier, its app name and its version.

| Value | Now | Where it comes from |
| --- | --- | --- |
| Legal name | `Auteur Studies LLC` | the company; both stores show the enrolled entity name as the seller, suffix included |
| Domain | `auteurstudies.com` | the company |
| Bundle identifier | `com.auteurstudies.auteur` | **derived** from the domain, so it cannot name a domain the company does not claim |
| Support address | `support@auteurstudies.com` | the company |
| Support URL | `https://auteurstudies.com/` | the company |
| Privacy policy | `https://auteurstudies.com/privacy.html` | the company |
| Terms | `https://auteurstudies.com/terms.html` | the company |
| Copyright | `Copyright (c) 2026 Auteur Studies LLC` | the company |

Two details that are easy to get wrong and are now held by tests:

- **The bundle identifier can never be changed after the first submission.**
  Ship `com.auteurstudios.auteur` against `auteurstudies.com` and the app
  carries a misspelling of its own company forever. It is derived rather than
  typed for exactly that reason.
- **The policy URLs end in `.html`.** GitHub Pages serves `privacy.html` at
  `/privacy.html` and gives `/privacy` a 404, and a privacy policy URL that
  404s is the most common metadata rejection there is. The filenames in the
  URLs are checked against the filenames the site builder actually writes.

### 5.1a What is now waiting on you — in this order

These are decided and not yet true, and none of them can be fixed by editing
code. `python3 tools/appstore/preflight.py` prints this list every time it runs,
so it cannot quietly stop being true.

1. **Register `auteurstudies.com`.** Everything else waits on it: the bundle
   identifier claims a domain the publisher must control, and all three store
   URLs 404 until it resolves. Check the name is clear for a trademark in the
   relevant class before registering, not after.
2. **File Auteur Studies LLC.** Both stores show the enrolled entity name as
   the seller, and organisation enrolment cannot start without one. §8.2 has
   what still needs deciding here.
3. **Make `support@auteurstudies.com` receive mail.** Apple guideline 1.2
   requires published contact information for an app carrying other people's
   content, and review does write to it.
4. **Point the domain at the published site.** The policy documents are already
   generated and deployed by `.github/workflows/pages.yml`; until the domain
   resolves there, they are published at an address no listing names. This also
   needs GitHub Pages switched on: Settings → Pages → Source: GitHub Actions.

Confirm all four at once with `python3 tools/appstore/preflight.py --online`,
which fetches the three URLs and fails on anything that is not a 200.

### 5.2 Accounts and registrations to open

Ordered by how long they take to clear, not by cost.

| What | Why it is needed | Note |
| --- | --- | --- |
| Trademark search — "Auteur Studies" | Everything below is expensive to undo | **Do this first.** §8.6 |
| `auteurstudies.com` | The bundle identifier claims it, and the store URLs live on it | Blocks all three store URLs |
| **Auteur Studies LLC** | Owns the app, the domain, the developer accounts and any revenue | Name decided; jurisdiction is §8.2 |
| `support@auteurstudies.com` | Apple guideline 1.2 published contact information | Follows the domain |
| Apple Developer Program | Any App Store submission | Annual fee; organisation enrolment needs the filed entity and a D-U-N-S number, and can take weeks |
| Google Play Console | Any Play submission | One-time fee; personal accounts now face identity verification and, for new personal developers, a closed-testing requirement before production |
| TikTok for Developers | Reading back TikTok numbers | See §5.4 |
| Meta / Instagram developer app | Reading back Instagram numbers | See §5.4 |
| Business bank account | Store payouts, hosting bills | Follows the entity |

*Every fee, timeline and policy in this table moves. Check each against the
provider's current page before acting, and date what you find.*

### 5.3 Hosting — the smallest real decision

The repository already contains three ways to deploy the same container. This is
one afternoon, and it unblocks having a link to show anyone.

- **Render** — fewest steps. Dashboard → New → Blueprint → pick the repo. It
  reads `render.yaml`, builds the Dockerfile, gives an HTTPS address. The 10 GB
  disk mounted at `/data` matters: without it every deploy loses every film
  anybody made.
- **Fly** — `fly volumes create auteur_data --size 10`, then `fly deploy`. Same
  container; the volume matters for the same reason.
- **Any Docker host** — `docker-compose.yml` works as-is.

Two environment variables are already set correctly in both config files and
should not be removed: `AUTEUR_TRUST_PROXY=1` and `AUTEUR_PUBLIC_HTTPS=1`.
Behind a proxy without them the sign-in cookie is not marked Secure, the browser
drops it, and signing in fails in a way indistinguishable from a wrong password.

The first account's username and password are set **in the host's dashboard**,
never in the repository — both config files mark them `sync: false` for exactly
that reason.

### 5.4 Platform approvals — the long poles

Both of these gate the Schedule tab's whole reason for existing, and both take
outside review. Start them early.

**TikTok** — requires a developer application and an **audit** before it may
leave sandbox. In sandbox, only accounts explicitly added as testers can
authorise it. Practical consequence: **it works for your own account
immediately**, and for anybody else's only after the audit clears.
Scopes requested: `user.info.basic`, `user.info.stats`, `video.list`.
Credentials: `AUTEUR_TIKTOK_CLIENT_KEY`, `AUTEUR_TIKTOK_CLIENT_SECRET`.
Source: <https://developers.tiktok.com/doc/login-kit-manage-user-access-tokens/>

**Instagram** — requires a Meta app, and the connected account must be a
**Business or Creator** account, not a personal one. Instagram returns no
insights at all for a personal account, which is the single most common reason
this looks broken when it is working. Reading anybody else's numbers needs App
Review.
Scopes requested: `instagram_business_basic`,
`instagram_business_manage_insights`.
Credentials: `AUTEUR_INSTAGRAM_CLIENT_ID`, `AUTEUR_INSTAGRAM_CLIENT_SECRET`.
Source: <https://developers.facebook.com/documentation/instagram-platform/insights>

Both were verified against the platforms' own documentation in **2026-08**.
Re-check before relying on them.

### 5.5 Store submission facts

- **Age rating: 12+.** Accounts under 18 start with sensitive films hidden, and
  that setting is lockable with a code. Do not let any listing copy imply a
  younger audience.
- **User-generated content obligations** (Apple guideline 1.2) are met and must
  stay met: content filtering, a report mechanism, blocking, and published
  contact information.
- **Account deletion inside the app** is required by Apple 5.1.1(v) and is
  built.
- **Apple listing limits**, enforced in code so copy cannot be written past
  them: name 30, subtitle 30, keywords 100, promotional text 170, description
  4000 characters.
- **Google Play Data safety** must match what the code can actually reach. It
  currently declares the TikTok/Instagram connections. Any change to what the
  app touches has to change this declaration **in the same release**.

### 5.6 Pricing — set, derived, and partly ahead of §8.1

There are now prices, and they live in `auteur/pricing.py` rather than on a
page somebody edits. The instruction was: fifteen per cent under the market
average, a free trial, ten per cent off the highest tier.

| | **Solo** | **Studio** |
|---|---|---|
| market average | $14.75 | $49.67 |
| **monthly** | **$12.49** (15.3% under) | **$41.99** (15.5% under) |
| after the advertised 10% | — | **$37.79**, with the code `STUDIO10` |

The tiers were called "A copy that is yours" and "A copy for the room" — good
lines, and the wrong things to put on an invoice or a card statement. They kept
their job one field down, as each tier's blurb. `STUDIO10` is derived from the
tier and the percentage together, so neither half can go stale on its own; it
was `ROOM10`, and it outlived the name it came from by one commit.

Free is the browser build, which already ships and needs no account.

**Where the averages come from.** Entry: Runway Standard $12, CapCut Pro $15,
Descript Hobbyist $16, Kapwing Pro $16. Top: VEED Pro $49, Descript Business
$50, Kapwing Business $50. Each carries the page it was read off and the month
it was read (2026-08) in the module. Three rivals are excluded on purpose,
with reasons recorded: Runway Max $76 is priced around generative credits and
this app renders on the customer's own device, so there is no per-film cost to
price against; CapCut Team $24.99 is a small-team plan rather than a top tier;
Adobe Premiere is not an AI-first consumer editor.

**Three things to push back on if you disagree.**

1. **The 14-day trial is chosen, not measured.** Nothing in the comparison set
   produced it. It is labelled as chosen in the module, the report and a test
   so it does not sit there looking like the sourced numbers.
2. **Which rivals belong in which tier was a judgement call.** Adding Runway
   Max would move the top average to $56 and the price to $47.99.
3. **The prices presume the answer to §8.1.** A subscription only makes sense
   for a hosted instance — every feature marked `on_device` runs free on the
   customer's phone and always will. Setting these prices therefore leans
   towards "run a hosted instance as the default and keep self-hosting as an
   option". That is not a decision the code can make; it is one the founder
   should confirm or overturn, and §8.1 stays open until they do.

**The discount needs a code, and the site prints it.** A Stripe payment link
accepts `allow_promotion_codes` and refuses a `discounts` parameter outright —
this was tried against the real API — so a coupon on its own is something only
the merchant can apply. A 10% saving advertised with no code to type is a
saving nobody can claim. `STUDIO10` is derived from the tier and the percentage, so a discount
changed to fifteen per cent cannot leave the old code redeemable beside it.

**Nothing can be bought yet, and the page says so.** The live payment links do
not exist — the Stripe key available to the code here is read-only in livemode,
so the products are in test mode only. Every paid plan therefore reads "Not
open yet", the headline leads with "Free in your browser today" rather than
offering a trial, and the promotion code is not printed at all, because a code
to type at a checkout that does not exist is an instruction with nowhere to
follow it. Run `tools/stripe/sync_pricing.py --apply --live`, put the two URLs
it prints into `pricing.CHECKOUT` (or set `AUTEUR_CHECKOUT_SOLO` and
`AUTEUR_CHECKOUT_STUDIO`), and the whole page turns on together. It is item
three on the §5.1a waiting list.

**Nothing is typed twice.** Prices round *down* to the largest ordinary price
that still satisfies the claim — fifteen per cent under $14.75 is $12.5375, and
writing $12.99 because prices end in 99 would be 11.9% under on a page saying
15%. A test scans the built website for anything shaped like money and requires
every hit to be a figure the module produced.
`tools/stripe/sync_pricing.py` pushes the same numbers into Stripe, so the
account is not hand-typed either; it shows before it does, needs `--apply` to
write and `--live` as well as a live key to touch a real account, and running
it twice creates nothing the second time.

---

---

## 6. Positioning — what to build the story on

Not decided. This is where a collaborator earns their keep. The raw material:

**The real differentiator** is that the cut is *authored*, not templated. Every
competitor in the "AI video editor" category assembles clips to a beat. This
reads the footage and builds a structure — an opening hold, a phrase that lands,
one still shot, a rhyme back to the first image. The measurable version: shot
lengths vary by a factor of 4.5–6× within one film, where a template-driven
editor sits at 2×.

**The second differentiator** is the privacy posture, which is architectural
rather than promised.

**The third** is that the Create half and the Schedule half are the same app.
Make it, then plan where it goes and see whether the last one landed — without
exporting to something else.

**The tension to resolve:** those three arguments point at different buyers.
"Authored cuts" is a creator's argument. "Runs on your own machine" is a
privacy-conscious individual's argument. "Plan and measure across platforms" is
a social-media-manager's argument. Picking one to lead with is a real decision
and it is §8.3.

---

## 7. Hard constraints — do not violate these

1. **No plaintext password and no credential hash may ever live in this
   repository.** Passwords are set in the host's dashboard (`sync: false` in
   `render.yaml`, `fly secrets` on Fly).
2. **A password was committed to git history in an early commit (`b657dee`) and
   must be treated as permanently compromised.** It must not be reused anywhere,
   for anything, ever. Do not ask what it was.
3. **Platform access tokens** are written with `0600` permissions to a file kept
   separate from the list of connections. Keep that separation.
4. **The app must never request publishing scopes** on TikTok or Instagram
   unless that becomes a deliberate, announced product decision with its own
   privacy-document change.
5. **Every privacy claim in marketing must be checkable against the code.** See
   §4.
6. **Never put a credential in a chat, a document or a screenshot.**

---

## 8. Open decisions — put these to the founder

Two of these are now closed. The rest change what gets built next; each needs a
recommendation, not a survey.

**8.1 One product or two?** Today every deployment is somebody's own server. A
hosted multi-tenant version would be a different company — different costs,
different privacy story, different obligations. Options: stay self-hosted and
sell the app; run a hosted instance as the default and keep self-hosting as an
option; or both, deliberately. This decision shapes pricing, the privacy copy,
and the store listings. **This is now the largest open question in the
document** — and §5.6 has now leaned on it: there are prices, and a
subscription only makes sense for a hosted instance. Confirm or overturn that
lean before anything links to a checkout.

**8.2 Entity — half decided.** The name is **Auteur Studies LLC**. What is not
decided is the jurisdiction it is filed in, which depends on where the founder
is resident and on whether outside money is ever likely. Note that "LLC" as a
suffix presumes a US-style filing; if the answer to the jurisdiction question is
somewhere that does not have LLCs, the suffix has to change and so does every
place it appears — which is one edit, in `Company.legal_name`, because the code
derives the rest. Worth confirming before the filing, not after.

**8.3 Who is it for, first?** Creator, privacy-conscious individual, or social
media manager (§6). Pick one for the first launch; the other two stay in the
product but out of the headline.

**8.4 How does it make money?** ~~Nothing is built.~~ **Both halves are now
built** (2026-08-31), and the shape is a subscription on a hosted instance.

- **Selling.** `tools/stripe/sync_pricing.py` creates the products, prices,
  coupon, promotion code and payment links from `auteur/pricing.py`, and
  reconciles rather than duplicating on a re-run.
- **Entitlement.** `auteur/web/billing.py` verifies the Stripe webhook
  signature; `Account.plan` / `plan_until` / `paying` decide what somebody may
  use. Before this the two were never introduced: a customer could be charged
  and nothing whatsoever happened to their account.
- **What it gates.** The top tier is sold as *"the same instance with more
  than one person on it"*, so that is what it guards — opening a copy to other
  people needs Studio, on a copy Auteur Studies hosts (`AUTEUR_HOSTED=1`).
  Never on somebody's own machine: charging for a friend to sign in to
  software on your own laptop is renting you something you already own.

Three things remain and none of them is code: the payment-link URLs have to
land in `pricing.CHECKOUT`, livemode needs `--apply --live` with an `sk_live_`
key, and the deployed instance needs `STRIPE_WEBHOOK_SECRET`.

**8.5 Launch order.** iOS first, Android first, or web instance first? The web
instance is the only one that needs no store approval and could be live this
week. That argues for it as the beachhead regardless of the answer to 8.1.

**8.6 The name — decided.** **Auteur Studies**, trading, **Auteur Studies LLC**
as the entity, on **auteurstudies.com**. It reads deliberately: auteur theory,
film studies, and the app's own Scholar that studies reference reels to derive
the cutting rhythms. One task remains and it is not a decision: **clear the
trademark before registering the domain**, in whichever class covers downloadable
software, and check it is not confusable with an existing mark. Do that first;
everything in §5.1a is downstream of it.

---

## 9. What to send back

The most useful output is something the founder can act on without translation.
Please return, in this order:

1. **A dated list of anything in §5 that has changed** — fees, timelines,
   policies — with the source you checked and when. Include a trademark and
   domain-availability read on "Auteur Studies" (§8.6): that one is on the
   critical path and everything else in §5.1a is downstream of it.
2. **A recommendation on each still-open decision in §8** (8.1, 8.2's
   jurisdiction, 8.3, 8.4, 8.5), one short paragraph each: the call, the single
   strongest reason, and the one thing that would change your mind. 8.2's name
   and 8.6 are closed — do not reopen them, and do not propose alternative
   company names.
3. **A critical-path plan** to the first thing a stranger can use, with owners
   and rough durations. Mark each step *decision* or *task*.
4. **Draft copy** for whatever you can write without new product facts —
   positioning line, store description within the §5.5 limits, a launch note.
   Flag every claim that needs verifying against §4 before it ships.
5. **A list of questions you could not answer from this file.** That list is
   how this file gets better.

Keep it in one document. It comes back to a working repository, and something
that can be read in one pass is worth more than something exhaustive.
