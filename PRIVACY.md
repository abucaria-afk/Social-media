# Privacy

**Auteur collects nothing, tracks nothing, and sends nothing anywhere.**

That is not a promise about intent. It is a property of how the app is built,
and the parts of it that can be checked are checked by the test suite.

## The iOS app

The app on the App Store contains a page loaded from inside the app itself and
a small amount of native code. It makes **no network requests of any kind** —
there is no analytics, no crash reporting, no advertising identifier, no
account, and no server. It works with the phone in aeroplane mode, which is the
simplest way to confirm it.

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

## Publishing

Nothing in this program posts to Instagram, TikTok, YouTube or anywhere else.
The manager plans posts, drafts captions and checks them; you post them. There
is no code path that publishes to a service and no credentials for one — a test
reads the source to keep it that way.

## Children

This app is not directed at children and collects no data from anybody.

## Changes and contact

If this document ever stops being true, the change will be in the repository's
history alongside the code that made it untrue. Questions go to whoever
publishes your copy of the app; if that is you, this file is yours to keep
accurate.
