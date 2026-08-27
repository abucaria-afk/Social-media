# Privacy

**Nobody operates a service here. There is no "we" to send anything to.**

That is the whole of it, and it is a property of how this is built rather than
a promise about intent — the parts that can be checked are checked by the test
suite.

It is worth being exact about the one thing that sounds like a contradiction.
This app has a feed of films and messages between people, and neither of those
can live inside a single phone: they need a server. The server is one *you*
run, with `auteur serve`, usually on your own wifi, holding its files in a
folder you chose. So "nothing leaves your phone" is not quite the right
sentence, and the right one is longer: **nothing goes anywhere you did not put
it.** There is no account with anybody, no third-party analytics, and no
address in this program that you did not type.

## The iOS app

**On its own, the app makes no network requests at all.** It contains the
whole edit room — a page loaded from inside the app itself, plus a small amount
of native code — and it works with the phone in aeroplane mode, which is the
simplest way to confirm it. There is no analytics, no crash reporting service,
no advertising identifier and no account.

**Connected to your own instance, it reaches that and nothing else.** The feed,
the messages and the planned posts live there because they cannot live in one
phone. You type the address; it is `http://` on your own wifi, so the app
allows plain connections *only* to local addresses and never to the internet.
Leave it empty and the app never opens a socket.

What it asks for, and why:

| Permission | Asked when | What it is used for |
| --- | --- | --- |
| Add to Photos | the first time you save a film | writing the film you made into your camera roll |
| Calendar (write only) | the first time you add a shoot | creating an event with its reminders |

Nothing else. The photo picker needs no permission at all — iOS hands the app
only the items you choose and nothing else — so the app does not ask for access
to your photo library, only for permission to *add* to it.

Your photographs and clips are read on the device, cut on the device, and the
finished film is written on the device. None of it is uploaded, because there
is nowhere for it to be uploaded to.

## The self-hosted version

`auteur serve` runs a web app on your own machine, usually on your own wifi.
Everything it holds — accounts, films, messages, plans — is in files on that
machine, in a folder you chose. Nobody operates a service; there is no
"we" who could receive your data.

Three things are worth being precise about:

**Sign-in with Google.** If, and only if, you configure it, signing in sends
you to Google and Google tells this app your email address so it can match an
account that already exists here. Google will know you signed in to something.
This app tells Google nothing about you and creates no account from it.

**The calendar link.** Subscribing puts your planned shoots in your calendar
app, which may be synced by Apple or Google depending on which calendar you put
it in. That is your calendar's arrangement, not this app's. The link contains a
secret, which is why the app tells you to treat it as a password and lets you
replace it.

**The Scholar.** If you give it an API key it sends the *question you typed* to
Anthropic to answer it. It never sends your footage. Without a key it answers
only from what it has measured locally, and says so.

**What was watched.** The instance records how long each film was watched,
whether it was played to the end, whether it looped, and whether anybody tapped
share. It keeps that in two forms.

Per film, it keeps the totals — how many plays, how many lasted three seconds,
how many finished. There is no person in those numbers; they are facts about a
film, in the way its runtime is.

Per account, it keeps what you watched and how much of it. That is about you,
and it is written down here rather than folded into an aggregate to sound
smaller. It is what makes the feed learn what *you* like rather than what the
average person likes, you can see all of it on your own profile, and deleting
your account deletes it.

None of it leaves the machine. There is no endpoint in this program that sends
it anywhere, which is the same reason nothing else here does. If you never run
an instance, none of this exists at all — a film cut on the phone and saved is
not watched by anything.

The reason this is measured rather than left alone: a feed that ranks by
nothing is a shuffle, and an app that says it "learns" while refusing to
observe anything is claiming something it cannot do.

**When something breaks.** A script error is written to a file beside the
accounts on the machine running the app, and shown to you in a panel you can
copy. That is a bug finder rather than telemetry, and the difference is not
intent: there is no endpoint in this program that sends anything off the
machine, so there is nowhere for a report to go except the disk it is already
on.

**Signing in.** A password can be protected with two-step verification, which
this app implements itself to the same standard every authenticator app uses.
The shared secret never leaves the machine holding the accounts, and recovery
codes are stored hashed — so the file that survives a lost phone is not also
the file that replaces the second factor.

## The one thing it asks you for

**A year of birth, at sign-up.** A year rather than a full date, because a year
is the least that answers the only two questions this app has about it: whether
you are old enough to use it at all, and whether an account should start with
sensitive films hidden. It is written into the accounts file on the machine
running the instance, alongside your username, and it goes nowhere else — there
is no address in this program that could send it anywhere.

That is also why the App Store's privacy answers still read "data not
collected": that question is about what reaches the people who publish an app,
and nothing here does.

## Your account, and getting rid of it

**You can delete your account from inside the app.** *You &rarr; Delete my
account*, which asks for your password and for the word "delete" to be typed.
It removes the account, every film you made and the files behind them, every
conversation you are part of, your profile and picture, your planned posts and
any reels you added — from the machine running the instance, immediately, with
no copy kept and no undo.

**Reporting and blocking.** Every film, message and person carries a report
control, and reporting also offers to block. Blocking is immediate, needs
nobody's permission, and works in both directions: neither person can see the
other's films or write to them. A report is stored on the instance and shown to
whoever runs it — there is nowhere else for it to go, and it names what was
reported, who by, and what was done about it. Reports about somebody whose
account has since been deleted are removed with the account.

## Publishing

Nothing in this program posts to Instagram, TikTok, YouTube or anywhere else.
The manager plans posts, drafts captions and checks them; you post them. There
is no code path that publishes to a service and no credentials for one — a test
reads the source to keep it that way.

## Children

This app is not directed at children. Nothing reaches the people who publish it. An instance you run records what was watched on it, on your own machine, and deleting an account erases that account's history.

## Changes and contact

If this document ever stops being true, the change will be in the repository's
history alongside the code that made it untrue.

Questions about the app itself:

<!-- CONTACT -->

Anything about content on an instance goes to whoever runs that instance, from
inside the app — there is no service here that could receive it instead.
