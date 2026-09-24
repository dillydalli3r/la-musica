"""Netscape cookie files: one parser, one jar model, for every cookie login.

The app has two cookie-bearing credentials, and they are the same file:
yt-dlp's jar on disk (``server/api_youtube.py``, ``<music>/.mlo/data/
cookies.txt``) and RateYourMusic's `Cookie` header (``server/api_rym.py``,
a header string in the ``rym_cookie`` config key). Both accept a browser
extension's ``cookies.txt`` export, so the file reading, the strictness, the
sentences a refusal uses and the per-cookie notes all live HERE, once — a
second parser is how the two would drift into accepting different files.

What is in the format, and what this module insists on:

  * a line is ``domain<TAB>include-subdomains<TAB>path<TAB>secure<TAB>expiry
    <TAB>name<TAB>value``. ``#HttpOnly_`` prefixed onto the DOMAIN column is
    part of the domain, not a comment: it is how a jar records a cookie the
    page's own JavaScript must not see (the RYM `session` cookie), and it is
    the one ``#``-prefixed line that carries a cookie.
  * a file is proven either by the ``# Netscape HTTP Cookie File`` header or by
    at least one well-shaped cookie line, because both are real exports
    (browsers' own exporters leave the header out).
  * two column rules are enforced beyond the shape because yt-dlp's loader
    (``http.cookiejar.MozillaCookieJar``, which ``yt_dlp.cookies`` wraps)
    REJECTS the whole file when they are broken: the include-subdomains flag
    must agree with a leading dot on the domain (its ``assert domain_specified
    == initial_dot``), and the expiry must be digits or empty. A jar this
    module accepted and yt-dlp then refused would fail every later download
    with a cause nowhere near the download.

Beyond parsing, this module owns the three things a cookie login needs and
neither credential could have alone:

  * ``filter_jar`` — a browser extension exports the WHOLE profile, so an
    import keeps only the cookies the credential is actually sent to, and
    drops everything else (with the count, so the UI can say so).
  * ``write_jar`` — the atomic credential write (temp file in the target
    directory, fsync, ``os.replace``): a download running while the user saves
    a new jar reads either the whole old file or the whole new one.
  * the per-cookie NOTES: a comment the user attaches to one cookie, and the
    expiry the export stated for it. They are keyed by the cookie's identity
    (``domain``, ``path``, ``name`` — see ``cookie_key``), never by position,
    so a re-import leaves a comment on the cookie it was written for. The
    store is the ``cookie_notes`` config value (one JSON object per source,
    see ``set_note``); a jar-backed credential ALSO carries them into the file
    as ``# mlo-comment:`` lines (``render_jar``, read back by ``jar_comments``)
    so the file stays self-describing — and every reader ignores them, because
    every reader ignores ``#`` lines. Storing a note never touches a cookie
    line: the file is edited line by line, so the cookie data is byte for byte
    what it was.
"""
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from pydantic import BaseModel

# The body ceiling. A cookie jar is a text file of a few dozen lines (the
# browsers write 100 KB at most for a signed-in profile), so half a megabyte
# is far above any real export and still small enough to keep one request from
# filling the disk. Bigger is refused with the number rather than truncated: a
# half-written jar fails on the next download with a confusing parse error, and
# the user would have no idea the paste was cut.
MAX_COOKIE_BYTES = 512 * 1024

# The first line curl and every browser's cookie export write, and the only
# thing that tells a cookie jar apart from any other tab-separated text.
NETSCAPE_HEADER = "# Netscape HTTP Cookie File"

# Our own per-cookie comment line, written directly ABOVE the cookie line it
# describes. It is an ordinary Netscape comment (a `#` line), so curl, yt-dlp
# and http.cookiejar skip it; the marker is what tells it apart from the
# export's own comments when we read a jar back.
COMMENT_MARK = "# mlo-comment: "

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

# A comment is one line of a jar, so a newline in it would split the file; the
# cap keeps one cookie's note from being used as free storage.
MAX_COMMENT_CHARS = 300

# The empty-file sentence is the one thing this module says that a caller may
# want to replace: the RYM import answers an empty paste with its own version
# (see server/api_rym.py). It names neither credential, because it is the
# shared one.
EMPTY_FILE = ("the file is empty — export cookies.txt from the browser you "
              "are signed in with, and paste it here")


class CookieUpload(BaseModel):
    """A pasted or dropped cookies.txt, as text (never as a path)."""
    text: str = ""


class Cookie(NamedTuple):
    """One accepted cookie line, in the jar's own columns.

    ``domain`` is the host with ``#HttpOnly_`` and the leading dot stripped and
    lower-cased (that is the form http.cookiejar compares against), and
    ``initial_dot`` keeps the fact the dot carried: "this cookie travels to
    every subdomain", which is also what the include-subdomains flag must
    agree with. ``expiry`` is the column as written — "" is a session cookie.
    """
    domain: str
    path: str
    name: str
    value: str
    expiry: str
    secure: bool
    initial_dot: bool


def _domain_columns(domain: str) -> Tuple[str, bool]:
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


def _is_comment_line(stripped: str) -> bool:
    """Is a stripped line a comment (and not a ``#HttpOnly_`` cookie)?"""
    return stripped.startswith("#") and not stripped.startswith(_HTTPONLY_PREFIX)


def _parse_cookie_line(line: str) -> Optional[Cookie]:
    """The cookie *line* carries, or None when it is not a cookie line.

    Every rule yt-dlp's own loader enforces is enforced here (see the module
    docstring): the column count, a real host, boolean flags that agree with
    the domain's leading dot, a path that starts at the root, and an expiry
    that is digits or empty.
    """
    parts = line.split("\t")
    if len(parts) != _COOKIE_COLUMNS:
        return None
    domain, flag, path, secure, expiry, name, value = parts
    host, initial_dot = _domain_columns(domain)
    if not host or ("." not in host and host != "localhost"):
        return None
    include_subdomains = flag.strip().upper()
    if include_subdomains not in ("TRUE", "FALSE"):
        return None
    if (include_subdomains == "TRUE") != initial_dot:
        return None
    if not path.startswith("/"):
        return None
    secure_flag = secure.strip().upper()
    if secure_flag not in ("TRUE", "FALSE"):
        return None
    expiry = expiry.strip()
    if expiry and not _EXPIRY_RX.match(expiry):
        return None
    return Cookie(host, path, name.strip(), value, expiry,
                  secure_flag == "TRUE", initial_dot)


def _parse(text: str) -> Tuple[List[Cookie], Optional[str], int, bool]:
    """The ONE parser: (cookies, error, junk lines, header seen).

    Exactly one of ``cookies`` and ``error`` is ever set. A file is accepted on
    EITHER proof, because both are real exports: the ``# Netscape HTTP Cookie
    File`` header (what curl, yt-dlp and every browser extension write), or at
    least one line shaped like a cookie. The header alone is accepted too — an
    export of a signed-out profile is still a cookie file, and the caller warns
    about what it does not hold.
    """
    raw = text or ""
    if not raw.strip():
        return [], EMPTY_FILE, 0, False
    cookies: List[Cookie] = []
    junk = 0
    seen_header = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _is_comment_line(stripped):
            if stripped.startswith(NETSCAPE_HEADER):
                seen_header = True
            continue
        cookie = _parse_cookie_line(line)
        if cookie is None:
            junk += 1
            continue
        cookies.append(cookie)
    if not cookies and not seen_header:
        detail = (" (found no cookie line at all)" if not junk
                  else f" ({junk} line(s) were not cookie lines)")
        return [], ("not a Netscape cookie file: expected the "
                    f"'{NETSCAPE_HEADER}' header or 7 tab-separated columns "
                    "(domain, flag, path, secure, expiry, name, value)"
                    f"{detail}"), junk, seen_header
    return cookies, None, junk, seen_header


def parse_cookies(text: str) -> Tuple[List[Cookie], Optional[str]]:
    """(cookies, error) — every accepted line of a Netscape cookie file.

    The rich view: each entry keeps the path, the expiry and the flags, which
    is what a per-cookie list and a per-cookie note need. ``parse_cookie_file``
    is the same parse projected onto the columns a caller that only builds a
    ``Cookie`` header (or counts names) cares about.
    """
    cookies, error, _junk, _header = _parse(text)
    return cookies, error


def parse_cookie_file(text: str, with_values: bool = False):
    """(cookies, error) for a Netscape cookie file's text.

    ``cookies`` is ``[(domain, name)]`` — or ``[(domain, name, value)]`` when
    *with_values* is set, for the caller that re-serves the file's cookies as a
    `Cookie` header rather than as a jar of their own (the RateYourMusic
    credential, server/api_rym.py) — one entry per accepted line. The value is
    only ever returned when asked for: it is a live credential, and the yt-dlp
    jar's own state is names, counts and domains.
    """
    cookies, error = parse_cookies(text)
    if error:
        return [], error
    if with_values:
        return [(c.domain, c.name, c.value) for c in cookies], None
    return [(c.domain, c.name) for c in cookies], None


def cookie_key(domain: str, path: str, name: str) -> str:
    """A cookie's identity, as the per-cookie notes are keyed.

    Domain, path AND name, in that order, tab separated — the three columns
    that decide which cookie a request sends. Two cookies that share a name on
    two domains (or two paths) are two cookies, and a note written for one must
    never move to the other; keying by position instead is exactly the bug this
    avoids, because the next export re-orders the file.
    """
    return f"{str(domain or '').lower()}\t{path or '/'}\t{name or ''}"


def hosts_match(host: str, hosts: Sequence[str]) -> bool:
    """Is *host* one of *hosts*, or a subdomain of one?

    A suffix match on the parsed host, so `www.rateyourmusic.com` and
    `rateyourmusic.com` both count while `rateyourmusic.com.example.net`
    (a domain someone else owns) cannot.
    """
    host = str(host or "").lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


def filter_jar(text: str, hosts: Sequence[str]) -> Tuple[str, List[Cookie], int]:
    """(text, kept cookies, dropped lines) — a jar narrowed to *hosts*.

    A browser extension's export is the WHOLE profile — every site the user is
    signed in to — and a credential that is only ever sent to one provider (or
    to one family of hosts) must not store the rest. So cookie lines for any
    other host are removed; every other line is kept exactly as it was, which
    is what makes the filtered jar byte-identical to the paste whenever the
    paste held nothing else (the app's own tests hold that).

    A ``# mlo-comment:`` line is dropped with the cookie line it describes: a
    comment with no cookie under it is not a comment ABOUT anything.
    """
    lines = (text or "").split("\n")
    keep: List[bool] = []
    kept: List[Cookie] = []
    dropped = 0
    for line in lines:
        stripped = line.strip()
        cookie = (None if not stripped or _is_comment_line(stripped)
                  else _parse_cookie_line(line))
        if cookie is None:
            keep.append(True)
            continue
        if hosts_match(cookie.domain, hosts):
            keep.append(True)
            kept.append(cookie)
        else:
            keep.append(False)
            dropped += 1
    for index, line in enumerate(lines):
        if not keep[index] or not line.strip().startswith(COMMENT_MARK):
            continue
        # The cookie line this note belongs to is the next one with something
        # on it; if that line is one we dropped, the note goes too.
        after = index + 1
        while after < len(lines) and not lines[after].strip():
            after += 1
        if after < len(lines) and not keep[after]:
            keep[index] = False
    return "\n".join(line for line, k in zip(lines, keep) if k), kept, dropped


def clean_comment(text: str) -> str:
    """One jar-safe line of comment: no newlines, trimmed, capped."""
    flat = str(text or "").replace("\r", " ").replace("\n", " ")
    return flat.strip()[:MAX_COMMENT_CHARS]


def render_jar(text: str, comments: Dict[str, str]) -> str:
    """*text* with each cookie's note written above its line.

    Line by line: the note lines we wrote before are removed (they are
    re-emitted from *comments*, so this is idempotent) and one fresh
    ``# mlo-comment:`` line is inserted directly above the cookie line whose
    identity has a note. Every cookie line, and every other line, is written
    back exactly as it came in — a note can change the file's comments and
    nothing else, so a jar written with notes still parses to the identical
    cookie set and every reader (yt-dlp included) still reads it.

    A note whose cookie is not in *text* is not written: it has nothing to
    attach to in this file.
    """
    out: List[str] = []
    for line in (text or "").split("\n"):
        stripped = line.strip()
        if stripped.startswith(COMMENT_MARK):
            continue
        cookie = (None if not stripped or _is_comment_line(stripped)
                  else _parse_cookie_line(line))
        if cookie is not None:
            note = clean_comment(comments.get(
                cookie_key(cookie.domain, cookie.path, cookie.name), ""))
            if note:
                out.append(COMMENT_MARK + note)
        out.append(line)
    return "\n".join(out)


def jar_comments(text: str) -> Dict[str, str]:
    """{identity: comment} — the notes a jar carries in its own lines.

    The other half of ``render_jar``: a jar this app wrote states which cookie
    each note belongs to, so re-importing it (or moving it between installs)
    brings the notes back without the config they came from.
    """
    out: Dict[str, str] = {}
    pending = ""
    for line in (text or "").split("\n"):
        stripped = line.strip()
        if stripped.startswith(COMMENT_MARK):
            pending = clean_comment(stripped[len(COMMENT_MARK):])
            continue
        cookie = (None if not stripped or _is_comment_line(stripped)
                  else _parse_cookie_line(line))
        if cookie is not None and pending:
            out[cookie_key(cookie.domain, cookie.path, cookie.name)] = pending
        pending = ""
    return out


def write_jar(path: str, text: str) -> str:
    """Replace the jar at *path* with *text*, atomically, and return the path.

    The ``# Netscape HTTP Cookie File`` header is written as the FIRST line
    even when the text did not carry it: http.cookiejar — which yt_dlp.cookies
    wraps — reads the first line as the file's magic and refuses the whole jar
    when it is a cookie line instead ("does not look like a Netscape format
    cookies file"). The app's job here is to hand yt-dlp a jar it can read, so
    a well-shaped headerless export is completed rather than stored in a form
    that fails on the next download.

    Written the way the config is (temp file in the target directory, fsync,
    os.replace): a download running while the user saves a new jar reads either
    the whole old file or the whole new one, never half of either.
    """
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


def expiry_seconds(expiry: str) -> Optional[float]:
    """A written expiry column as a Unix timestamp, or None.

    None means "no expiry stated" (a session cookie) — and also a written 0,
    which is how several writers spell a session cookie, so a 0 must never be
    read as 1970 and reported as expired.
    """
    if not expiry:
        return None
    try:
        when = float(expiry)
    except (TypeError, ValueError):
        return None
    return when if when > 0 else None


def is_expired_at(expiry: str, now: Optional[float] = None) -> bool:
    """Has a written expiry column already passed?"""
    when = expiry_seconds(expiry)
    if when is None:
        return False
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    return when <= now


def expiry_date_of(expiry: str) -> str:
    """A written expiry column as an ISO 8601 UTC date, or ""."""
    when = expiry_seconds(expiry)
    if when is None:
        return ""
    return datetime.fromtimestamp(when, timezone.utc).date().isoformat()


def is_expired(cookie: Cookie, now: Optional[float] = None) -> bool:
    """Has the jar's OWN expiry date passed for this cookie?"""
    return is_expired_at(cookie.expiry, now)


def expiry_date(cookie: Cookie) -> str:
    """The cookie's expiry as an ISO 8601 UTC date, or ""."""
    return expiry_date_of(cookie.expiry)


def cookie_row(domain: str, path: str, name: str, expiry: str = "",
               comment: str = "") -> dict:
    """One cookie as the per-cookie view states it (GET /api/cookies/{source}).

    The columns the UI shows and edits: where the cookie goes, what it is
    called, when its own expiry column says it dies (``expires_at``/``expired``
    are computed here, so the browser is not asked to do date arithmetic on a
    timestamp the server already knows), and the comment the user attached.

    There is NO VALUE: a session cookie is a live credential, and this payload
    is read by a browser.
    """
    return {
        "domain": domain,
        "path": path,
        "name": name,
        "expiry": expiry or "",
        "expires_at": expiry_date_of(expiry),
        "expired": is_expired_at(expiry),
        "comment": comment or "",
    }


def expired_warning(cookies: Sequence[Cookie], provider: str,
                    now: Optional[float] = None) -> Optional[str]:
    """One sentence when the cookies carry expiries that have already passed.

    A stale export is the most common way a cookie login "just stops working":
    the jar parses, the panel is green, and the provider answers as a
    signed-out visitor. Only cookies whose OWN expiry has passed are named —
    a session cookie states no expiry, so it is never guessed at.
    """
    expired = [c for c in cookies if is_expired(c, now)]
    if not expired:
        return None
    return (f"{len(expired)} cookie(s) in this file expired on "
            f"{expiry_date(expired[0])} — {provider} treats them as a "
            "signed-out visitor; export cookies.txt again from a browser that "
            "is signed in")


# --------------------------------------------------------------------------- #
# The per-cookie notes. One config value (`cookie_notes`), one JSON object per
# cookie source, one entry per cookie identity:
#
#     {"youtube": {"youtube.com\t/\tSID": {"comment": "my main account"}},
#      "rym":     {"rateyourmusic.com\t/\tsession": {"comment": "…",
#                                                   "expiry": "1893456000"}}}
#
# `comment` is what the user typed, `expiry` is what the import's file stated
# for that cookie (the RYM credential is a bare `Cookie` header, which has
# nowhere to carry an expiry — the jar file keeps its own). Both are optional;
# an entry with neither is dropped.
# --------------------------------------------------------------------------- #
def _load_notes(raw: str) -> Dict[str, Dict[str, dict]]:
    """The store as a dict; anything unreadable is simply empty."""
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Dict[str, dict]] = {}
    for source, section in data.items():
        if not isinstance(section, dict):
            continue
        notes: Dict[str, dict] = {}
        for key, value in section.items():
            if not isinstance(value, dict):
                continue
            note = {}
            comment = clean_comment(value.get("comment") or "")
            if comment:
                note["comment"] = comment
            expiry = str(value.get("expiry") or "").strip()
            if expiry:
                note["expiry"] = expiry
            if note:
                notes[str(key)] = note
        if notes:
            out[str(source)] = notes
    return out


def notes_for(raw: str, source: str) -> Dict[str, dict]:
    """{identity: {"comment"?, "expiry"?}} held for one cookie source."""
    return _load_notes(raw).get(str(source), {})


def comments_of(raw: str, source: str) -> Dict[str, str]:
    """{identity: comment} held for one cookie source (empty ones left out)."""
    return {key: note["comment"] for key, note in notes_for(raw, source).items()
            if note.get("comment")}


def set_note(raw: str, source: str, key: str, comment: Optional[str] = None,
             expiry: Optional[str] = None) -> str:
    """The updated store text with one cookie's note changed.

    ``comment``/``expiry`` are set when given (an empty string CLEARS that
    half), and left alone when None — which is what makes the two edits
    independent: importing a jar refreshes the expiries the file stated
    WITHOUT touching the comments the user wrote, and editing a comment never
    forgets where the cookie came from.
    """
    data = _load_notes(raw)
    section = data.setdefault(str(source), {})
    note = dict(section.get(key) or {})
    if comment is not None:
        clean = clean_comment(comment)
        if clean:
            note["comment"] = clean
        else:
            note.pop("comment", None)
    if expiry is not None:
        stated = str(expiry).strip()
        if stated:
            note["expiry"] = stated
        else:
            note.pop("expiry", None)
    if note:
        section[key] = note
    else:
        section.pop(key, None)
    if not section:
        data.pop(str(source), None)
    return json.dumps(data, ensure_ascii=False, sort_keys=True)
