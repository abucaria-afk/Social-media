"""Three collections that give their public-domain work away, and no key.

- **The Metropolitan Museum of Art.** `isPublicDomain=true` narrows the search;
  the object record carries the flag again, which is the one this trusts.
  Search returns ids only, so each keeper costs a second request.
- **The Art Institute of Chicago.** One request returns records with fields;
  images are assembled from a IIIF base and an image id. Their documentation
  asks for an identifying user agent, so one is sent.
- **The Cleveland Museum of Art.** CC0, one request, images inline.

**Everything here is defensive by design.** A record missing a field is skipped
rather than raising: these are three separate institutions' idea of a JSON
document, they change without notice, and one odd record should cost one
result rather than the whole search.

**Why the transport is injectable.** The sandbox this was written in refuses
outbound connections to all three hosts, so the parsing, the gates and the
ranking are tested against recorded response shapes rather than against a live
API. That is worth stating plainly: the shapes are taken from each
collection's published documentation and the code is written to survive being
wrong about them, but the first run against the real endpoints is the first
real test of the shapes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

log = logging.getLogger("auteur.gallery.sources")

#: Sent on every request. Two of the three ask for it; none of them require a
#: key, and none of them are sent anything about the user.
AGENT = "auteur/1.0 (public-domain art search; https://github.com/abucaria-afk/Social-media)"

#: How long to wait on any one collection before giving up on it.
TIMEOUT = 20.0


@dataclass
class Candidate:
    """One record from a collection, before anybody has looked at the picture."""

    provider: str = ""
    ref: str = ""
    title: str = ""
    artist: str = ""
    date: str = ""
    medium: str = ""
    classification: str = ""
    #: The largest image the collection offers, and a smaller one to judge from.
    image_url: str = ""
    preview_url: str = ""
    #: The record's own page, for a person who wants to check.
    page_url: str = ""
    #: The provider's own rights statement. Never inferred from a search filter.
    rights: str = ""
    width: int = 0
    height: int = 0
    credit: str = ""

    @property
    def key(self) -> str:
        """What makes two records the same work, across two collections."""
        return f"{self.artist.strip().lower()}|{self.title.strip().lower()}"


class Transport(Protocol):
    """Anything that can fetch bytes. Real network, or a test's recording."""

    def get(self, url: str, *, headers: dict | None = None) -> bytes:
        raise NotImplementedError


class Web:
    """The real one."""

    def get(self, url: str, *, headers: dict | None = None) -> bytes:
        import urllib.request

        request = urllib.request.Request(url, headers={"User-Agent": AGENT, **(headers or {})})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read()


class Unavailable(RuntimeError):
    """A collection could not be reached. Never fatal — the others are tried."""


def _json_from(transport: Transport, url: str, **headers) -> dict:
    try:
        raw = transport.get(url, headers=headers or None)
    except Exception as exc:  # noqa: BLE001 - network, DNS, TLS, policy, anything
        raise Unavailable(f"{url}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise Unavailable(f"{url}: unreadable response") from exc
    return payload if isinstance(payload, dict) else {}


def _text(value, limit: int = 200) -> str:
    """Whatever the record held, as a plain string of sane length."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = ", ".join(_text(item, limit) for item in value if item)
    elif isinstance(value, dict):
        value = value.get("description") or value.get("name") or ""
    return str(value).strip()[:limit]


# ------------------------------------------------------------------ the Met

MET = "https://collectionapi.metmuseum.org/public/collection/v1"


def search_met(query: str, transport: Transport, *, limit: int = 20) -> list[Candidate]:
    found = _json_from(
        transport,
        f"{MET}/search?q={quote(query)}&isPublicDomain=true&hasImages=true",
    )
    ids = [i for i in (found.get("objectIDs") or []) if isinstance(i, int)]
    out: list[Candidate] = []
    for object_id in ids[: max(0, limit)]:
        try:
            record = _json_from(transport, f"{MET}/objects/{object_id}")
        except Unavailable as exc:
            log.debug("skipping Met object %s: %s", object_id, exc)
            continue
        image = _text(record.get("primaryImage"), 500)
        if not image:
            continue
        out.append(
            Candidate(
                provider="The Met",
                ref=str(object_id),
                title=_text(record.get("title")),
                artist=_text(record.get("artistDisplayName")),
                date=_text(record.get("objectDate"), 60),
                medium=_text(record.get("medium")),
                classification=_text(record.get("classification"), 80),
                image_url=image,
                preview_url=_text(record.get("primaryImageSmall"), 500) or image,
                page_url=_text(record.get("objectURL"), 500),
                # The record's own answer, not the search filter's.
                rights="public domain" if record.get("isPublicDomain") else "not stated",
                credit=_text(record.get("creditLine")),
            )
        )
    return out


# ------------------------------------------------- the Art Institute of Chicago

AIC = "https://api.artic.edu/api/v1"
AIC_FIELDS = (
    "id,title,artist_title,date_display,medium_display,classification_title,"
    "image_id,is_public_domain,thumbnail"
)


def search_artic(query: str, transport: Transport, *, limit: int = 20) -> list[Candidate]:
    payload = _json_from(
        transport,
        f"{AIC}/artworks/search?q={quote(query)}&limit={max(1, limit)}&fields={AIC_FIELDS}",
        **{"AIC-User-Agent": AGENT},
    )
    base = (payload.get("config") or {}).get("iiif_url") or "https://www.artic.edu/iiif/2"
    out: list[Candidate] = []
    for record in payload.get("data") or []:
        if not isinstance(record, dict):
            continue
        image_id = _text(record.get("image_id"), 120)
        if not image_id:
            continue
        thumb = record.get("thumbnail") or {}
        out.append(
            Candidate(
                provider="Art Institute of Chicago",
                ref=_text(record.get("id"), 40),
                title=_text(record.get("title")),
                artist=_text(record.get("artist_title")),
                date=_text(record.get("date_display"), 60),
                medium=_text(record.get("medium_display")),
                classification=_text(record.get("classification_title"), 80),
                image_url=f"{base}/{image_id}/full/1686,/0/default.jpg",
                preview_url=f"{base}/{image_id}/full/843,/0/default.jpg",
                page_url=f"https://www.artic.edu/artworks/{_text(record.get('id'), 40)}",
                rights="public domain" if record.get("is_public_domain") else "not stated",
                width=int(thumb.get("width") or 0) if isinstance(thumb, dict) else 0,
                height=int(thumb.get("height") or 0) if isinstance(thumb, dict) else 0,
            )
        )
    return out


# ------------------------------------------------ the Cleveland Museum of Art

CMA = "https://openaccess-api.clevelandart.org/api/artworks"


def search_cleveland(query: str, transport: Transport, *, limit: int = 20) -> list[Candidate]:
    payload = _json_from(
        transport,
        f"{CMA}/?q={quote(query)}&cc0=1&has_image=1&limit={max(1, limit)}",
    )
    out: list[Candidate] = []
    for record in payload.get("data") or []:
        if not isinstance(record, dict):
            continue
        images = record.get("images") or {}
        if not isinstance(images, dict):
            continue
        big = images.get("print") or images.get("web") or images.get("full") or {}
        small = images.get("web") or big
        url = _text(big.get("url") if isinstance(big, dict) else "", 500)
        if not url:
            continue
        creators = record.get("creators") or []
        artist = ""
        if isinstance(creators, list) and creators:
            first = creators[0]
            artist = _text(first.get("description") if isinstance(first, dict) else first, 120)
        out.append(
            Candidate(
                provider="Cleveland Museum of Art",
                ref=_text(record.get("id"), 40),
                title=_text(record.get("title")),
                artist=artist,
                date=_text(record.get("creation_date"), 60),
                medium=_text(record.get("technique")),
                classification=_text(record.get("type"), 80),
                image_url=url,
                preview_url=_text(small.get("url") if isinstance(small, dict) else "", 500) or url,
                page_url=_text(record.get("url"), 500),
                rights=_text(record.get("share_license_status"), 60) or "not stated",
                width=int((big.get("width") or 0) if isinstance(big, dict) else 0),
                height=int((big.get("height") or 0) if isinstance(big, dict) else 0),
            )
        )
    return out


#: Every collection this knows how to ask, by the name a person would type.
COLLECTIONS = {
    "met": search_met,
    "artic": search_artic,
    "cleveland": search_cleveland,
}


def search_all(
    query: str,
    *,
    transport: Transport | None = None,
    limit: int = 20,
    collections: list[str] | None = None,
) -> tuple[list[Candidate], list[str]]:
    """Ask every collection, and report which ones could not be reached.

    One institution being down is not a failed search — it is a smaller one.
    """
    transport = transport or Web()
    wanted = collections or list(COLLECTIONS)
    found: list[Candidate] = []
    trouble: list[str] = []
    for name in wanted:
        search = COLLECTIONS.get(name)
        if search is None:
            trouble.append(f"no collection called {name!r}")
            continue
        try:
            found.extend(search(query, transport, limit=limit))
        except Unavailable as exc:
            log.info("%s could not be reached: %s", name, exc)
            trouble.append(f"{name}: {exc}")
    return found, trouble
