"""Who somebody is here: a picture, a name they chose, and who they follow.

Until now a person on this instance was a username and nothing else. Every
screen that showed them drew a coloured disc with their first letter in it,
which is a reasonable stand-in for eight rows in a list and not a person. And
the feed was everybody's films in one column with no way to say *these* are the
people I came for — following was not a weak feature, it was an absent one.

So one more small JSON store beside the accounts, the films and the messages:

* the parts of somebody that are theirs to write — a display name, a bio, a
  link, a picture;
* and the one part that is about somebody else — who they follow.

Three decisions worth stating.

**The profile is separate from the account.** The account file holds a salt, a
password hash, a TOTP secret and recovery codes; the profile holds a bio and a
picture. They have different readers: a profile is handed to every signed-in
person who looks at the feed, and an account is never handed to anybody at all.
Keeping them in one record is how a `asdict()` on the wrong object one day puts
a password hash in a JSON response.

**Following is stored on the follower.** `profile.following` is the list you
control; followers are computed by asking who has you in theirs. That costs a
scan of the store per query, which on an instance with the number of accounts a
home server has is nothing, and it means there is exactly one place a follow is
recorded. Two lists that have to agree eventually do not.

**A picture is a file, not a blob in the JSON.** Photographs are hundreds of
kilobytes and this store is rewritten on every edit; base64 in a JSON file that
is read into memory on every request would make each profile change a megabyte
of churn. The JSON holds a filename, the file lives beside it, and
:meth:`Profiles.forget_picture` is what stops the folder growing forever.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: Longest a display name may be. Long enough for a real name with a middle
#: one; short enough that it cannot push a film's author line onto three lines
#: on a phone, which is the actual constraint.
LONGEST_NAME = 40

#: Longest a bio may be. Instagram allows 150 and people fill it; this is the
#: same, so a bio written there pastes in whole.
LONGEST_BIO = 150

#: Longest a link may be. Not a URL validator — see `tidy_link`.
LONGEST_LINK = 120


def _write(path: Path, payload: object) -> None:
    """Write JSON through a temporary file, as the other stores do.

    Same reason as `social._write`: a half-written store is not a damaged
    profile, it is *no* profiles, and everybody comes back as a blank disc.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(path.suffix + ".new")
    scratch.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(scratch, path)


def _read(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _one_line(text: str, limit: int) -> str:
    """Trim to length, and collapse the whitespace people paste in.

    A bio pasted out of another app arrives with newlines and runs of spaces in
    it. Those are not wrong to type, they are wrong to *store*: the places a
    bio is shown are a two-line clamp on a profile header and a single line in
    a list, and a stored newline is a layout that only breaks for some people.
    """
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def tidy_link(link: str) -> str:
    """A link somebody can follow, or "".

    Deliberately narrow. Anything that is not plainly `http://` or `https://`
    is rejected rather than repaired, because the interesting attack on a
    profile field that becomes an `<a href>` is `javascript:` — and a tidier
    that tries to *fix* input is a tidier that eventually fixes that into
    something that runs. A bare `example.com` gets an `https://` because that
    is what somebody typing their own address means, and nothing else does.
    """
    raw = _one_line(link, LONGEST_LINK)
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered.startswith(("http://", "https://")):
        return raw
    if ":" in raw.split("/", 1)[0]:
        # Some other scheme — javascript:, data:, mailto:. Not a web address.
        return ""
    return "https://" + raw


@dataclass
class Profile:
    """One person, as everybody else on the instance sees them."""

    who: str
    #: What they would like to be called. Empty means "use the username",
    #: which is what every screen falls back to — see `display`.
    name: str = ""
    bio: str = ""
    link: str = ""
    #: Filename of the picture inside the pictures folder, or "" for the disc
    #: with their initial on it. A name, never a path: a stored path is a
    #: stored way out of the folder.
    picture: str = ""
    #: Usernames this person follows.
    following: list = field(default_factory=list)
    #: Usernames this person has blocked. Stored on the blocker, like
    #: `following`, and for the same reason: there is one place a block is
    #: recorded, and it is the person who decided it.
    blocked: list = field(default_factory=list)
    updated: float = field(default_factory=time.time)

    @property
    def display(self) -> str:
        return self.name or self.who

    @property
    def picture_url(self) -> str:
        """Where the page fetches the picture, or "" if there is not one.

        Never the filename. Pictures are addressed through a route keyed on the
        username, for the same reason a film is: the moment a filename crosses
        into a page it is a filename somebody can ask for directly, and then
        the folder it lives in is the security boundary.

        The `v=` is the profile's own timestamp, and it is not decoration. The
        route sends a long cache lifetime — a 44px disc fetched on every screen
        is exactly what a cache is for — so without something in the URL that
        changes, somebody who replaces their picture keeps seeing the old one
        until the cache expires, which is the single most common way this
        feature is reported broken.
        """
        return f"/api/profiles/{self.who}/picture?v={int(self.updated)}" if self.picture else ""

    def public(
        self,
        *,
        viewer: str = "",
        you_follow: bool = False,
        you_block: bool = False,
        followers: int = 0,
        films: int = 0,
    ) -> dict:
        """What a browser is allowed to know about somebody.

        `you_follow` is passed in rather than worked out here, because the
        answer lives in the *viewer's* following list and this object is the
        person being looked at. Reading it off `self` would mean answering
        "do I follow them" with "do they follow me", which is a wrong answer
        that looks right on every screen where two people follow each other.
        """
        return {
            "who": self.who,
            "name": self.display,
            "bio": self.bio,
            "link": self.link,
            "picture": self.picture_url,
            "following": len(self.following),
            "followers": followers,
            "films": films,
            "you_follow": bool(viewer) and viewer != self.who and you_follow,
            "you_block": bool(viewer) and viewer != self.who and you_block,
            "me": bool(viewer) and viewer == self.who,
        }


class Profiles:
    """Everybody's profile, and the follow graph."""

    def __init__(self, path: str | Path, pictures: str | Path | None = None):
        self.path = Path(path)
        self.pictures = Path(pictures) if pictures else self.path.parent / "pictures"
        self.lock = threading.Lock()
        self.profiles: dict[str, Profile] = {}
        self._load()

    @staticmethod
    def default_path(workspace: Path) -> Path:
        return Path(workspace) / "profiles.json"

    def _load(self) -> None:
        raw = _read(self.path)
        if not isinstance(raw, dict):
            return
        known = set(Profile.__dataclass_fields__)
        for who, row in raw.items():
            if not isinstance(row, dict):
                continue
            fields = {k: v for k, v in row.items() if k in known}
            fields["who"] = who
            self.profiles[who] = Profile(**fields)

    def _save(self) -> None:
        _write(
            self.path,
            {who: asdict(p) for who, p in sorted(self.profiles.items())},
        )

    # -- reading ---------------------------------------------------------

    def get(self, who: str) -> Profile:
        """Somebody's profile, inventing an empty one rather than returning None.

        Everybody has a profile the moment they have an account, whether or not
        they have ever opened the page. A `None` here would mean every caller
        needing the same three lines of "and if they have not filled it in",
        which is how half of them end up not having them.
        """
        with self.lock:
            found = self.profiles.get(who)
            return found if found is not None else Profile(who=who)

    def followers_of(self, who: str) -> list[str]:
        with self.lock:
            return sorted(p.who for p in self.profiles.values() if who in p.following)

    def following_of(self, who: str) -> list[str]:
        return sorted(self.get(who).following)

    def follows(self, who: str, other: str) -> bool:
        return other in self.get(who).following

    # -- blocking --------------------------------------------------------

    def block(self, who: str, other: str) -> bool:
        """Block somebody. True if this changed anything.

        Following is undone at the same time, in both directions. A block that
        left a follow in place would be a person still listed as a follower on
        the profile of the person who blocked them, which is exactly the row
        somebody blocking wanted to stop seeing.
        """
        if not who or not other or who == other:
            return False
        with self.lock:
            mine = self._mutable(who)
            theirs = self._mutable(other)
            changed = other not in mine.blocked
            if changed:
                mine.blocked.append(other)
            for profile, name in ((mine, other), (theirs, who)):
                if name in profile.following:
                    profile.following.remove(name)
                    changed = True
            if changed:
                mine.updated = time.time()
                self._save()
            return changed

    def unblock(self, who: str, other: str) -> bool:
        with self.lock:
            profile = self._mutable(who)
            if other not in profile.blocked:
                return False
            profile.blocked.remove(other)
            profile.updated = time.time()
            self._save()
            return True

    def blocks(self, who: str, other: str) -> bool:
        """Has `who` blocked `other`?"""
        return bool(who) and other in self.get(who).blocked

    def blocked_of(self, who: str) -> list[str]:
        return sorted(self.get(who).blocked)

    def apart(self, who: str) -> set[str]:
        """Everybody who should not see `who`, or be seen by them.

        Both directions in one set, which is the only way this stays right.
        Filtering on "people I blocked" alone leaves somebody able to watch the
        films of the person who blocked them and to keep writing to them —
        which is not a block, it is a mute. Blocking has to be a wall, and a
        wall has two sides.
        """
        if not who:
            return set()
        with self.lock:
            mine = self.profiles.get(who)
            out = set(mine.blocked) if mine is not None else set()
            out |= {p.who for p in self.profiles.values() if who in p.blocked}
        return out

    def public_of(self, who: str, *, viewer: str = "", films: int = 0) -> dict:
        """One profile as a page sees it, with the counts filled in."""
        return self.get(who).public(
            viewer=viewer,
            you_follow=bool(viewer) and self.follows(viewer, who),
            you_block=bool(viewer) and self.blocks(viewer, who),
            followers=len(self.followers_of(who)),
            films=films,
        )

    def cards(self, names: list[str]) -> dict[str, dict]:
        """Name, and a picture if there is one, for a list of people.

        This is what the feed and the inbox need: enough to draw somebody in a
        row, in one request rather than one request per row. It is deliberately
        not `public_of` for each name — that counts followers, which means a
        scan of the store per person, to render a 30px disc.
        """
        out: dict[str, dict] = {}
        for name in names:
            profile = self.get(name)
            out[name] = {"who": name, "name": profile.display, "picture": profile.picture_url}
        return out

    # -- writing ---------------------------------------------------------

    def _mutable(self, who: str) -> Profile:
        """The stored profile, created on first write. Call under the lock."""
        found = self.profiles.get(who)
        if found is None:
            found = Profile(who=who)
            self.profiles[who] = found
        return found

    def edit(
        self,
        who: str,
        *,
        name: str | None = None,
        bio: str | None = None,
        link: str | None = None,
    ) -> Profile:
        """Change the parts of a profile that are somebody's own to write.

        Each field is optional and `None` means "leave it": a form that posts
        only the field it changed should not blank the other two, which is what
        a signature of plain strings would quietly do.
        """
        with self.lock:
            profile = self._mutable(who)
            if name is not None:
                profile.name = _one_line(name, LONGEST_NAME)
            if bio is not None:
                profile.bio = _one_line(bio, LONGEST_BIO)
            if link is not None:
                profile.link = tidy_link(link)
            profile.updated = time.time()
            self._save()
            return profile

    def set_picture(self, who: str, filename: str) -> Profile:
        """Record a picture that has already been written to the folder."""
        with self.lock:
            profile = self._mutable(who)
            old = profile.picture
            profile.picture = filename
            profile.updated = time.time()
            self._save()
        if old and old != filename:
            self._unlink(old)
        return profile

    def forget_picture(self, who: str) -> Profile:
        """Back to the disc with their initial on it, and delete the file."""
        with self.lock:
            profile = self._mutable(who)
            old = profile.picture
            profile.picture = ""
            profile.updated = time.time()
            self._save()
        if old:
            self._unlink(old)
        return profile

    def picture_path(self, who: str) -> Path | None:
        """Where somebody's picture is, or None.

        The filename comes out of the store, but it is still resolved and
        checked against the folder before it is opened. The store is a file on
        disk: treating what comes out of it as trusted input is how a path
        traversal survives a restart.
        """
        name = self.get(who).picture
        if not name:
            return None
        candidate = (self.pictures / name).resolve()
        try:
            candidate.relative_to(self.pictures.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def _unlink(self, filename: str) -> None:
        try:
            candidate = (self.pictures / filename).resolve()
            candidate.relative_to(self.pictures.resolve())
            candidate.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    def follow(self, who: str, other: str) -> bool:
        """Follow somebody. True if this changed anything.

        Nobody follows themselves — not as a rule about vanity but because the
        feed's "following" scope would then include your own films for some
        people and not others, and "why is my film here" is a bug nobody can
        describe.
        """
        if not who or not other or who == other:
            return False
        with self.lock:
            profile = self._mutable(who)
            if other in profile.following:
                return False
            profile.following.append(other)
            profile.updated = time.time()
            self._save()
            return True

    def unfollow(self, who: str, other: str) -> bool:
        with self.lock:
            profile = self._mutable(who)
            if other not in profile.following:
                return False
            profile.following.remove(other)
            profile.updated = time.time()
            self._save()
            return True

    def forget(self, who: str) -> None:
        """Somebody's profile, their picture, and their name out of every
        following list. Part of deleting an account.

        The second half is the part that is easy to miss: removing the profile
        alone leaves the name in everybody else's `following`, so their counts
        stay one too high and the list under them has a row with nothing
        behind it.
        """
        picture = self.get(who).picture
        with self.lock:
            self.profiles.pop(who, None)
            for profile in self.profiles.values():
                if who in profile.following:
                    profile.following.remove(who)
                if who in profile.blocked:
                    profile.blocked.remove(who)
            self._save()
        if picture:
            self._unlink(picture)

    def drop_unknown(self, known: set[str]) -> int:
        """Forget follows of accounts that no longer exist, and say how many.

        The same failure the feed had with swept films, one level up: a deleted
        account leaves a name in everybody's following list, and the profile
        page then shows a following count larger than the list under it.
        """
        removed = 0
        with self.lock:
            for profile in self.profiles.values():
                gone = [n for n in profile.following if n not in known]
                for name in gone:
                    profile.following.remove(name)
                removed += len(gone)
            if removed:
                self._save()
        return removed


# ---------------------------------------------------------------------------
# Pictures
# ---------------------------------------------------------------------------

#: How big a stored picture is, on a side. A profile picture is shown at 96px
#: on the profile header and 44px everywhere else, so 512 is already twice what
#: a 3x phone screen asks for at the largest size it appears — and it means the
#: file is tens of kilobytes rather than the four megabytes a phone camera
#: hands over.
PICTURE_SIDE = 512

#: The largest upload accepted, before decoding. The page downscales in a
#: canvas first so a normal upload is well under a hundred kilobytes; this is
#: the ceiling for everything else, including somebody posting to the endpoint
#: directly.
LARGEST_UPLOAD = 8 * 1024 * 1024

#: Pillow will happily start decoding a 40,000 x 40,000 PNG that compresses to
#: a few kilobytes and ask the machine for six gigabytes on the way. It has its
#: own limit and raises a *warning* by default, which is not a defence. This is
#: checked against the header before any pixels are read.
LARGEST_PIXELS = 50_000_000


class BadPicture(Exception):
    """What went wrong, in words the person who chose the file can act on."""


def store_picture(raw: bytes, folder: Path, who: str) -> str:
    """Turn an uploaded file into a square JPEG in `folder`. Returns its name.

    Every part of this is re-encoding rather than validation, and that is the
    point: the bytes that arrive are never the bytes that get served. A file
    that decodes as an image and re-encodes as one cannot also be an HTML page
    that a browser sniffs and runs, and it cannot carry the two things a phone
    photograph carries that nobody means to publish — the GPS coordinates of
    where it was taken, and the serial number of the camera that took it. Both
    live in EXIF, and neither survives this.

    The orientation tag is the exception that has to be *read* before it is
    dropped, because a photograph taken in portrait on a phone is stored
    landscape with a "rotate me" flag, and stripping the flag without applying
    it is how a profile picture ends up on its side.
    """
    import io

    from PIL import Image, ImageOps, UnidentifiedImageError

    if not raw:
        raise BadPicture("That file was empty.")
    if len(raw) > LARGEST_UPLOAD:
        raise BadPicture("That picture is too large. Try one under 8MB.")

    try:
        probe = Image.open(io.BytesIO(raw))
        width, height = probe.size
        if width * height > LARGEST_PIXELS:
            raise BadPicture("That picture has too many pixels to open safely.")
        probe.load()
        image = ImageOps.exif_transpose(probe)
    except BadPicture:
        raise
    except UnidentifiedImageError:
        raise BadPicture("That file is not a picture this can read.") from None
    except Exception as exc:  # noqa: BLE001 - a broken file is not a crash
        raise BadPicture("That picture could not be opened.") from exc

    # Square, from the middle, then down to size. Cropping first means the
    # resize never has to think about aspect ratio, and cropping from the
    # middle is what every app does with a picture somebody has not framed.
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side))
    if side > PICTURE_SIDE:
        image = image.resize((PICTURE_SIDE, PICTURE_SIDE), Image.LANCZOS)

    # JPEG has no alpha, and a PNG with a transparent corner otherwise
    # composites onto black — which on a light-mode page is a black wedge
    # nobody asked for. Flattening onto white is what the alternative formats
    # would do anyway when the disc is drawn.
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        ground = Image.new("RGB", image.size, (255, 255, 255))
        ground.paste(image, mask=image.split()[-1])
        image = ground
    elif image.mode != "RGB":
        image = image.convert("RGB")

    folder.mkdir(parents=True, exist_ok=True)
    # The name carries the account and a timestamp, so replacing a picture
    # writes a new file rather than overwriting one a request may be reading.
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", who)[:32] or "who"
    filename = f"{safe}-{int(time.time() * 1000):x}.jpg"
    image.save(folder / filename, format="JPEG", quality=86, optimize=True)
    return filename
