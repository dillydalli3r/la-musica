"""The YouTube cookie jar — GET/POST/DELETE /api/youtube/cookies.

yt-dlp (server/youtube.py) is the app's only YouTube client, and a cookie jar
is what opens the videos an anonymous request cannot: age-gated and
members-only uploads, and the ones YouTube throttles or answers with "Sign in
to confirm your age". The jar is the USER's own session — the app never signs
in anywhere itself — so this module's whole job is to get one file onto disk
safely and say what is in it.

ONE path, owned by the app: <music>/.mlo/data/cookies.txt. It is not a
user-typed path because it is not a user-typed anything — Settings offers a
paste box and a drop target (both arrive here as text), so the jar can never
point at a file the app does not own, cannot go stale when the library moves,
and needs no path picker on a phone. The mode setting (youtube_cookies_mode:
none/file/browser) says whether that jar is USED at all, and this module only
ever reports it — the mode itself is saved by the ordinary config endpoint
like every other setting.

Mounted by server/main.py (``include_router``); this module never imports it.
"""
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mlo.config import load_config
from server import youtube

router = APIRouter(tags=["youtube"])

# The body ceiling. A cookie jar is a text file of a few dozen lines (the
# browsers write 100 KB at most for a signed-in profile), so half a megabyte
# is far above any real export and still small enough to keep one request
# from filling the disk. Bigger is refused with the number rather than
# truncated: a half-written jar fails on the next download with a confusing
# parse error, and the user would have no idea the paste was cut.
MAX_COOKIE_BYTES = 512 * 1024

# The first line curl and every browser's cookie export write, and the only
# thing that tells a cookie jar apart from any other tab-separated text.
NETSCAPE_HEADER = "# Netscape HTTP Cookie File"

# A cookie line's columns, in order: the domain (a leading dot means "any
# subdomain"), the include-subdomains flag, the path, the secure flag, the
# expiry, the name and the value. yt-dlp's own writer (http.cookiejar's
# MozillaCookieJar) and every browser export use exactly this order.
_COOKIE_COLUMNS = 7

# The expiry column, exactly as yt-dlp's own loader validates it
# (yt_dlp/cookies.py: `[0-9]+(?:\.[0-9]+)?`); empty means a session cookie.
_EXPIRY_RX = re.compile(r"^\d+(?:\.\d+)?$")

# `#HttpOnly_` is part of the DOMAIN column, not a comment: it is how a jar
# records a cookie the page's own JavaScript must not see, and it is the one
# `#`-prefixed line that carries a cookie.
_HTTPONLY_PREFIX = "#HttpOnly_"


class CookieUpload(BaseModel):
    """A pasted or dropped cookies.txt, as text (never as a path)."""
    text: str = ""


def _domain_columns(domain: str):
    """(host, initial_dot) from a cookie line's domain column.

    The column carries ``#HttpOnly_`` for a cookie the page's own JavaScript
    must not see, and a leading dot for "any subdomain of this host" — yt-dlp's
    loader asserts the include-subdomains flag agrees with that dot, so the two
    facts are read here together.
    """
    name = str(domain or "").strip()
    if name.startswith(_HTTPONLY_PREFIX):
        name = name[len(_HTTPONLY_PREFIX):].strip()
    return name.lstrip(".").lower(), name.startswith(".")


def parse_cookie_file(text: str, with_values: bool = False):
    """(cookies, error) for a Netscape cookie file's text.

    ``cookies`` is ``[(domain, name)]`` — or ``[(domain, name, value)]`` when
    *with_values* is set, for the one caller that re-serves the file's cookies
    as a `Cookie` header rather than as a jar of their own (the RateYourMusic
    credential, server/api_rym.py) — one entry per accepted line, and ``error``
    is the sentence to show the user when the text is not a cookie file at all
    (exactly one of the two is ever set). The value is only ever returned when
    asked for: it is a live credential, and the yt-dlp jar's own state is
    names, counts and domains.

    A file is accepted on EITHER proof, because both are real exports: the
    ``# Netscape HTTP Cookie File`` header (what curl, yt-dlp and every
    browser extension write), or at least one line shaped like a cookie
    (7 tab-separated columns, boolean flags, a path that starts at the root).
    The header alone is accepted too — an export of a signed-out profile is
    still a cookie file, and the caller warns about what it does not hold.

    Two column rules are enforced beyond the shape because yt-dlp's own loader
    (http.cookiejar.MozillaCookieJar, which yt_dlp.cookies wraps) REJECTS the
    whole file when they are broken: the include-subdomains flag must agree
    with a leading dot on the domain (its `assert domain_specified ==
    initial_dot`), and the expiry must be digits or empty. A jar this module
    accepted and yt-dlp then refused would fail every later download with a
    cause nowhere near the download.
    """
    raw = text or ""
    if not raw.strip():
        return [], ("the file is empty — export cookies.txt from the browser "
                    "you are signed in to YouTube with, and paste it here")
    cookies = []
    junk = 0
    seen_header = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") and not stripped.startswith(_HTTPONLY_PREFIX):
            if stripped.startswith(NETSCAPE_HEADER):
                seen_header = True
            continue
        parts = line.split("\t")
        if len(parts) != _COOKIE_COLUMNS:
            junk += 1
            continue
        domain, flag, path, secure, expiry, name, value = parts
        host, initial_dot = _domain_columns(domain)
        if not host or ("." not in host and host != "localhost"):
            junk += 1
            continue
        include_subdomains = flag.strip().upper()
        if include_subdomains not in ("TRUE", "FALSE"):
            junk += 1
            continue
        if (include_subdomains == "TRUE") != initial_dot:
            junk += 1
            continue
        if not path.startswith("/"):
            junk += 1
            continue
        if secure.strip().upper() not in ("TRUE", "FALSE"):
            junk += 1
            continue
        if expiry.strip() and not _EXPIRY_RX.match(expiry.strip()):
            junk += 1
            continue
        cookies.append((host, name.strip(), value) if with_values
                       else (host, name.strip()))
    if not cookies and not seen_header:
        detail = (" (found no cookie line at all)" if not junk
                  else f" ({junk} line(s) were not cookie lines)")
        return [], ("not a Netscape cookie file: expected the "
                    f"'{NETSCAPE_HEADER}' header or 7 tab-separated columns "
                    f"(domain, flag, path, secure, expiry, name, value){detail}")
    return cookies, None


def cookie_warnings(cookies) -> List[str]:
    """What the jar actually holds, in sentences worth acting on.

    A jar with no youtube.com cookie cannot sign a download in, and a user who
    exported the wrong profile — or exported while signed out — must hear that
    here instead of from a "Sign in to confirm your age" on the next video.
    """
    hosts = {}
    for domain, _name in cookies:
        hosts[domain] = hosts.get(domain, 0) + 1
    out = []
    yt = hosts.get("youtube.com", 0)
    google = hosts.get("google.com", 0)
    if not cookies:
        out.append("no cookie lines in the file — nothing was saved from it "
                   "for yt-dlp to read")
    elif not yt:
        out.append("no youtube.com cookie in this file — sign in to YouTube in "
                   "the browser you exported from, then export it again (a jar "
                   "from a signed-out profile carries no session)")
    elif not google:
        out.append(f"{yt} cookies for youtube.com, none for google.com — "
                   "YouTube's session usually rides with a google.com cookie "
                   "too; if a download still asks you to sign in, export from "
                   "the profile you are actually signed in with")
    else:
        out.append(f"{yt} cookies for youtube.com, {google} for google.com — "
                   "sign in to YouTube in the browser you exported from")
    return out


def _saved_at(path: str) -> Optional[str]:
    """When the jar was last written, as ISO 8601 UTC, or None."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec="seconds")


def cookie_state(cfg=None) -> dict:
    """The jar's settings and contents — the answer every route here returns.

    ``lines`` is the number of COOKIE lines that parsed (the UI's "how many
    cookies"), not the file's line count: a jar's comments are not cookies.
    ``sites`` is the distinct domains, so the UI can say what the jar is for
    without shipping the whole file to the browser.
    """
    cfg = cfg if cfg is not None else load_config()
    path = youtube.cookies_path()
    present = os.path.isfile(path)
    text = ""
    if present:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            text = ""
    cookies, _error = parse_cookie_file(text) if text else ([], None)
    size = 0
    if present:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
    return {
        "mode": youtube.cookies_mode(cfg),
        "browser": youtube.cookies_browser(cfg),
        "present": present,
        "path": path.replace("\\", "/"),
        "bytes": size,
        "lines": len(cookies),
        "sites": sorted({domain for domain, _name in cookies}),
        "saved_at": _saved_at(path) if present else None,
        "browsers": list(youtube.COOKIES_BROWSERS),
        "max_bytes": MAX_COOKIE_BYTES,
        "warnings": cookie_warnings(cookies) if present else [],
    }


def write_cookie_file(text: str) -> str:
    """Replace the jar with *text*, atomically, and return its path.

    The ``# Netscape HTTP Cookie File`` header is written as the FIRST line
    even when the paste did not carry it: http.cookiejar — which
    yt_dlp.cookies wraps — reads the first line as the file's magic and
    refuses the whole jar when it is a cookie line instead ("does not look
    like a Netscape format cookies file"). The app's job here is to hand
    yt-dlp a jar it can read, so a well-shaped headerless export is completed
    rather than stored in a form that fails on the next download.

    Written the way the config is (temp file in the target directory, fsync,
    os.replace): a download running while the user saves a new jar reads
    either the whole old file or the whole new one, never half of either.
    """
    path = youtube.cookies_path()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    body = text if text.endswith("\n") else text + "\n"
    if not any(line.strip().startswith(NETSCAPE_HEADER)
               for line in body.splitlines()):
        body = f"{NETSCAPE_HEADER}\n{body}"
    fd, temp_path = tempfile.mkstemp(prefix=".mlo_cookies_", suffix=".txt",
                                     dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        # A cookie jar is a credential: readable by the account that owns the
        # library and nobody else, on the hosts that have such a thing.
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    return path


@router.get("/api/youtube/cookies")
def youtube_cookies_get():
    """The cookie jar's state: which mode is on, what the file holds.

    Read by Settings → Videos, which shows the mode, the file's size, cookie
    count, domains and save time before the user pastes anything.
    """
    return cookie_state()


@router.post("/api/youtube/cookies")
def youtube_cookies_post(req: CookieUpload):
    """Save a pasted or dropped cookies.txt; returns the new state + warnings.

    The text is validated as a Netscape cookie file FIRST (see
    ``parse_cookie_file``): junk is refused with the reason instead of being
    written, because a jar yt-dlp cannot read turns every later download into
    a failure whose cause is nowhere near the download.
    """
    text = req.text or ""
    size = len(text.encode("utf-8", errors="replace"))
    if size > MAX_COOKIE_BYTES:
        # Rounded UP: 512 KiB + 1 byte is not "512 KiB", and the user is being
        # told their file is over the line, not shown a figure that matches it.
        raise HTTPException(
            413,
            f"the cookie file is {(size + 1023) // 1024} KiB — the limit is "
            f"{MAX_COOKIE_BYTES // 1024} KiB. Paste or drop the cookies.txt "
            "itself, not a browser profile folder",
        )
    cookies, error = parse_cookie_file(text)
    if error:
        raise HTTPException(400, error)
    write_cookie_file(text)
    state = cookie_state()
    state["warnings"] = cookie_warnings(cookies)
    return state


@router.delete("/api/youtube/cookies")
def youtube_cookies_delete():
    """Remove the jar (the mode setting is left alone: it is not this route's).

    Deleting the file rather than blanking it means `file` mode with no jar
    passes NO cookie option at all (see server/youtube.py's cookie_opts), so
    a user who deletes the jar gets plain anonymous downloads back instead of
    yt-dlp failing on an empty file.
    """
    path = youtube.cookies_path()
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError as e:
            raise HTTPException(500, f"could not remove the cookie file: {e}")
    return cookie_state()
