"""Build the public site: the privacy policy, the terms, and a page to land on.

App Store Connect will not accept a submission without a privacy policy URL
that resolves, and an app carrying other people's content needs terms at a URL
too. Both documents already exist in this repository as markdown, and both are
already converted for the app itself — so this is the same converter pointed at
an output directory, rather than a second copy of either document that can go
stale against the first.

    python3 tools/appstore/build_pages.py [outdir]

The GitHub Actions workflow in `.github/workflows/pages.yml` runs this and
publishes the result, so the URLs in `auteur/identity.py` are addresses that
actually answer rather than ones to fill in later.

Two things it does that the in-app version does not:

* **Absolute, self-contained pages.** The app serves its stylesheets from
  `/static/`; a Pages site is served from a subdirectory and would 404 on every
  one of them, so the CSS is inlined. The result is three files that work from
  any path, including opened off a disk.
* **A landing page.** The support URL Apple asks for has to go somewhere that
  explains what the app is and how to reach somebody — a bare policy is not a
  support page.
"""

from __future__ import annotations

import html
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from auteur.identity import COMPANY, IDENTITY  # noqa: E402
from auteur.web import assets  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "pages"
STATIC = ROOT / "auteur" / "web" / "static"


#: Where these pages answer from. The same derivation as the CNAME written
#: below, because a social card that names a host the site is not served from
#: is a card that fetches nothing and renders as a bare link.
HOST = COMPANY.documents_for(IDENTITY.slug)
SITE = f"https://{HOST}"

#: The picture a link to this site unfurls with. The app's own icon: it is the
#: only image this repository has that means the product, and it is already
#: published beside these pages for the pages that reference it.
CARD = "icon-512.png"


def _card(title: str, description: str, page: str) -> str:
    """The Open Graph and Twitter tags for one page.

    Without these, a link to the privacy policy pasted into a message — which
    is exactly how a policy URL travels, into App Store Connect, into a Play
    console field, into an email to a reviewer — unfurls as a bare grey box
    with the hostname in it. The pages already know their own title and their
    own description; this is those two facts said again in the vocabulary the
    unfurlers read, plus an absolute URL for each, because Open Graph has no
    notion of a relative one and a scraper is not on this host.
    """
    where = f"{SITE}/{page}" if page != "index.html" else f"{SITE}/"
    return "\n".join(
        [
            f'<meta name="description" content="{html.escape(description)}">',
            f'<meta property="og:title" content="{html.escape(title)}">',
            f'<meta property="og:description" content="{html.escape(description)}">',
            '<meta property="og:type" content="website">',
            f'<meta property="og:url" content="{where}">',
            f'<meta property="og:site_name" content="{html.escape(IDENTITY.app_name)}">',
            f'<meta property="og:image" content="{SITE}/{CARD}">',
            '<meta name="twitter:card" content="summary">',
            f'<meta name="twitter:title" content="{html.escape(title)}">',
            f'<meta name="twitter:description" content="{html.escape(description)}">',
            f'<meta name="twitter:image" content="{SITE}/{CARD}">',
        ]
    )


def _titled(markup: str) -> str:
    """The <title> of a built page, which is where its card's title comes from."""
    start = markup.find("<title>")
    if start == -1:
        return IDENTITY.app_name
    return html.unescape(markup[start + len("<title>") : markup.find("</title>", start)])


#: What each page is, in one sentence, for the card a link to it unfurls with.
#: Said here rather than read off the page: the two documents are generated
#: from markdown and carry no description of their own, and giving all three
#: the same fallback sentence makes three links that look identical in a
#: message — which is the state this is meant to fix, not a smaller version
#: of it.
ABOUT = {
    "index.html": (
        f"{IDENTITY.app_name} — what it is, how to get help, and how to report "
        "something. An editor that cuts a film out of what is already on your "
        "phone."
    ),
    "privacy.html": (
        f"What {IDENTITY.app_name} records, where it stays, and what leaves the "
        "device. Written against the code that makes it true."
    ),
    "terms.html": (
        f"The terms of use for {IDENTITY.app_name}, including no tolerance for "
        "objectionable content or abusive behaviour."
    ),
}


def _inline(markup: str, sheets: list[str]) -> str:
    """Replace the <link> tags with the stylesheets themselves.

    A Pages site lives under `/<repo>/`, so `/static/style.css` resolves to the
    wrong place and the page arrives unstyled — which is exactly the state an
    App Store reviewer would see it in.
    """
    css = "\n".join((STATIC / name).read_text(encoding="utf-8") for name in sheets)
    for name in sheets:
        markup = markup.replace(f'<link rel="stylesheet" href="/static/{name}">\n', "")
        markup = markup.replace(f'<link rel="stylesheet" href="/static/{name}">', "")
    # settings.js is served from /static/ too, and the accessibility settings
    # are worth having on a policy page — so it is inlined rather than dropped.
    script = (STATIC / "settings.js").read_text(encoding="utf-8")
    markup = markup.replace(
        '<script src="/static/settings.js"></script>',
        f"<script>\n{script}\n</script>",
    )
    return markup.replace("</head>", f"<style>\n{css}\n</style>\n</head>")


def _landing() -> str:
    """The support page. Says what the app is, and how to reach somebody."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>{IDENTITY.app_name} — support</title>
<script>
{(STATIC / "settings.js").read_text(encoding="utf-8")}
</script>
<style>
{(STATIC / "theme.css").read_text(encoding="utf-8")}
{(STATIC / "style.css").read_text(encoding="utf-8")}
{(STATIC / "prose.css").read_text(encoding="utf-8")}
</style>
</head>
<body>
<main class="prose">
<h1>{IDENTITY.app_name}</h1>
<p>An editor that cuts a film out of what is already on your phone. It runs on
the device; there is no service behind it and no account with anybody.</p>

<h2>Getting help</h2>
<p>Write to <strong>{IDENTITY.support_email}</strong>. That address is read by a
person, and it is the right place for anything about the app itself — a fault,
a question, or a report about the app rather than about something inside it.</p>

<h2>Reporting something inside the app</h2>
<p>Every film, message and person carries a <strong>Report</strong> control, and
reporting also offers to block. <strong>Blocking is immediate</strong> and needs
nobody's permission: it takes both people out of each other's reach, in both
directions, at once.</p>
<p>A report goes to whoever runs the copy of Auteur you are using — which, for
this app, is a person you know, on a computer they own. There is no central
service that could receive it instead, and saying otherwise would be untrue.</p>

<h2>Deleting your account</h2>
<p>In the app: <strong>You &rarr; Delete my account</strong>. It removes the
account, every film you made and its files, every conversation you are part of,
your profile and picture, and your planned posts — immediately, with no copy
kept.</p>

<h2>The documents</h2>
<ul>
  <li><a href="privacy.html">Privacy policy</a></li>
  <li><a href="terms.html">Terms of use</a></li>
</ul>

<p class="fineprint">Published by {IDENTITY.developer}.</p>
</main>
</body>
</html>
"""


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)

    # Regenerate from the markdown first, so this can never publish a stale
    # copy of a document that has moved on.
    assets.ensure(STATIC)

    sheets = ["theme.css", "style.css", "prose.css"]
    for name in ("privacy.html", "terms.html"):
        page = (STATIC / name).read_text(encoding="utf-8")
        # The app's "back to the app" link goes nowhere on a public site.
        page = page.replace('<p class="prose-away"><a href="/">Back to the app</a></p>', "")
        page = _inline(page, sheets)
        page = page.replace("</head>", _card(_titled(page), ABOUT[name], name) + "\n</head>")
        (OUT / name).write_text(page, encoding="utf-8")

    landing = _landing()
    landing = landing.replace(
        "</head>", _card(_titled(landing), ABOUT["index.html"], "index.html") + "\n</head>"
    )
    (OUT / "index.html").write_text(landing, encoding="utf-8")

    # The picture those cards point at. Copied rather than linked to the
    # app's own copy: this site is served from a different host and a card
    # image that 404s is worse than no card at all.
    shutil.copyfile(STATIC / CARD, OUT / CARD)

    # Say yes, on purpose. This site is three documents that exist to be
    # found — a privacy policy nobody can look up is the same problem as one
    # that does not resolve — so nothing here is disallowed, and the sitemap
    # is named rather than left to be guessed at.
    (OUT / "robots.txt").write_text(
        "\n".join(
            [
                "# The privacy policy, the terms and a support page. All three",
                "# are meant to be findable; none of them is the app, and the",
                "# app is not on this host.",
                "User-agent: *",
                "Allow: /",
                "",
                f"Sitemap: {SITE}/sitemap.xml",
                "",
            ]
        ),
        encoding="utf-8",
    )

    pages = ["", "privacy.html", "terms.html"]
    (OUT / "sitemap.xml").write_text(
        "\n".join(
            [
                '<?xml version="1.0" encoding="UTF-8"?>',
                '<urlset xmlns="http://www.sitemaps.org/' 'schemas/sitemap/0.9">',
            ]
            + [f"  <url><loc>{SITE}/{page}</loc></url>" for page in pages]
            + ["</urlset>", ""]
        ),
        encoding="utf-8",
    )
    # Jekyll would otherwise try to process this and drop anything it does not
    # recognise; the file is the documented way to tell Pages not to.
    (OUT / ".nojekyll").write_text("", encoding="utf-8")

    # The custom domain, which Pages reads out of a file in the published
    # output and nowhere else. Without it Pages answers only at
    # <owner>.github.io/<repo>/, and the privacy policy Apple fetches during
    # review would 404 — the single most common metadata rejection there is.
    #
    # The *subdomain*, not the apex. auteurstudies.com is the company's own
    # site and this repository does not build it; putting the apex here would
    # take that site down the moment the DNS moved. The documents live beside
    # the code that makes them true, on a host of their own.
    #
    # Derived rather than typed, for the same reason the bundle identifier is.
    host = COMPANY.documents_for(IDENTITY.slug)
    (OUT / "CNAME").write_text(host + "\n", encoding="utf-8")

    for made in sorted(OUT.iterdir()):
        if made.is_file():
            print(f"{made}  {made.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
