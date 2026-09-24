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

The file FORMAT, the strictness and the atomic write are
``server/cookies.py``'s (it is the same cookies.txt the RYM import accepts);
what is here is what makes the jar YouTube's: the hosts a YouTube request is
actually sent to (the import keeps those and drops the rest of a browser
profile's export), and the sentences only YouTube can say.

Mounted by server/main.py (``include_router``); this module never imports it.
"""
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException

from mlo.config import load_config, save_config
# One Netscape parser, one upload model and one jar model for the whole app:
# the yt-dlp jar and the RateYourMusic credential accept the same file, so the
# file-or-junk rules (and the sentences that explain them) are stated once.
from server import youtube
from server.cookies import (MAX_COOKIE_BYTES, Cookie, CookieUpload, cookie_key,
                            cookie_row, comments_of, expired_warning,
                            filter_jar, hosts_match, jar_comments, notes_for,
                            parse_cookies, render_jar, set_note, write_jar)

router = APIRouter(tags=["youtube"])

# The hosts a YouTube request is sent this jar's cookies to, and therefore the
# hosts an import keeps: youtube.com and its subdomains (m.youtube.com,
# www.youtube.com, consent.youtube.com), the media hosts (googlevideo.com), and
# Google's own (google.com, googleapis.com), where YouTube's sign-in cookies
# ride along. A browser export is the WHOLE profile, so everything else in it —
# every other site the user is signed in to — is dropped rather than stored:
# the app has no business holding those cookies, and yt-dlp is never sent them.
COOKIE_HOSTS = ("youtube.com", "googlevideo.com", "google.com", "googleapis.com")

# The one host whose absence means the file cannot sign anything in. Google's
# cookies alone are not a YouTube session, and storing a jar that cannot work
# would leave the user with a panel that says "saved" and a download that asks
# them to sign in.
REQUIRED_HOST = "youtube.com"

# The config key the per-cookie notes live under (a comment the user attaches
# to one cookie, and the expiry the file stated for it). The jar is where the
# cookie DATA lives; the notes are the one thing a jar cannot be edited into,
# because they are keyed by identity, not by line.
NOTES_KEY = "cookie_notes"

# Which cookie source the notes belong to. RYM's notes share the same store
# (see server/api_rym.py) — one store, one key, one reader.
SOURCE = "youtube"


def _note_text(cfg: Optional[dict] = None) -> str:
    """The raw `cookie_notes` store, as saved."""
    cfg = cfg if cfg is not None else load_config()
    return str(cfg.get(NOTES_KEY) or "")


def _save_notes(cfg: dict, text: str) -> None:
    """Write the store back through the app's own config writer."""
    if str(cfg.get(NOTES_KEY) or "") == text:
        return
    cfg[NOTES_KEY] = text
    if not save_config(cfg):
        reason = getattr(save_config, "last_error", "") or ""
        raise HTTPException(
            500, f"Failed to save config{(': ' + reason) if reason else ''}")


def cookie_warnings(cookies) -> List[str]:
    """What the jar actually holds, in sentences worth acting on.

    A jar with no youtube.com cookie cannot sign a download in, and a user who
    exported the wrong profile — or exported while signed out — must hear that
    here instead of from a "Sign in to confirm your age" on the next video. An
    expired export is the same failure wearing a green chip, so it is named
    too.

    Accepts either shape the module parses into — the rich ``Cookie`` records
    (which carry the expiry) or the ``(domain, name)`` pairs — because the
    pairs are what a caller that only counts names has.
    """
    hosts: Dict[str, int] = {}
    records: List[Cookie] = []
    for item in cookies or []:
        if isinstance(item, Cookie):
            domain, name, record = item.domain, item.name, item
        else:
            domain, name, record = item[0], item[1], None
        hosts[domain] = hosts.get(domain, 0) + 1
        if record is not None:
            records.append(record)
    out = []
    yt = hosts.get(REQUIRED_HOST, 0)
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
    expired = expired_warning(records, "YouTube")
    if expired:
        out.append(expired)
    return out


def _saved_at(path: str) -> Optional[str]:
    """When the jar was last written, as ISO 8601 UTC, or None."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec="seconds")


def _jar_text(path: str) -> str:
    """The jar's text, or "" when there is no readable jar."""
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def cookie_state(cfg=None) -> dict:
    """The jar's settings and contents — the answer every route here returns.

    ``lines`` is the number of COOKIE lines that parsed (the UI's "how many
    cookies"), not the file's line count: a jar's comments — ours included —
    are not cookies. ``sites`` is the distinct domains, so the UI can say what
    the jar is for without shipping the whole file to the browser.
    """
    cfg = cfg if cfg is not None else load_config()
    path = youtube.cookies_path()
    present = os.path.isfile(path)
    text = _jar_text(path)
    cookies, _error = parse_cookies(text) if text else ([], None)
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
        "sites": sorted({cookie.domain for cookie in cookies}),
        "saved_at": _saved_at(path) if present else None,
        "browsers": list(youtube.COOKIES_BROWSERS),
        "max_bytes": MAX_COOKIE_BYTES,
        "warnings": cookie_warnings(cookies) if present else [],
    }


def cookie_list(cfg=None) -> List[dict]:
    """The jar's cookies, one row per cookie — the per-cookie view.

    Each row is what the UI needs to show and to edit: where the cookie goes
    (``domain`` and ``path``), what it is called, when the file says it
    expires, whether that has passed, and the comment the user attached. NO
    VALUE is ever here: a session cookie is a live credential, and this list is
    read by a browser.

    The comment comes from the ``cookie_notes`` store — the one a re-import
    leaves alone (see ``set_cookie_comment``).
    """
    cfg = cfg if cfg is not None else load_config()
    cookies, _error = parse_cookies(_jar_text(youtube.cookies_path()))
    notes = notes_for(_note_text(cfg), SOURCE)
    return [cookie_row(
        cookie.domain, cookie.path, cookie.name, cookie.expiry,
        str(notes.get(
            cookie_key(cookie.domain, cookie.path, cookie.name),
            {}).get("comment") or ""))
        for cookie in cookies]


def set_cookie_comment(domain: str, path: str, name: str, comment: str,
                       cfg=None) -> List[dict]:
    """Attach (or clear) one cookie's comment, and mirror it into the jar.

    The comment is written to the notes store — keyed by the cookie's identity,
    which is why it survives a re-import: the next export of the same profile
    holds the same cookie, and the note finds it again by domain/path/name
    rather than by its position in a file that has just been rewritten.

    The jar itself is then re-rendered from those notes, which ADDS a
    ``# mlo-comment:`` line above the cookie line and changes nothing else: a
    comment can never reorder, edit or drop a cookie. Readers that do not know
    our marker skip it, because it is an ordinary Netscape comment line.

    A cookie the jar does not hold is refused: a comment no cookie can carry
    would be stored for a cookie that is not there, and the UI would have to
    explain a note it cannot attach.
    """
    cfg = cfg if cfg is not None else load_config()
    path_ = youtube.cookies_path()
    text = _jar_text(path_)
    cookies, _error = parse_cookies(text)
    key = cookie_key(domain, path, name)
    if not any(cookie_key(c.domain, c.path, c.name) == key for c in cookies):
        raise HTTPException(
            404, f"no {name or 'unnamed'} cookie for {domain}{path} in the "
                 "YouTube jar — the comment was not saved")
    notes = _note_text(cfg)
    _save_notes(cfg, set_note(notes, SOURCE, key, comment=comment))
    write_jar(path_, render_jar(text, comments_of(_note_text(cfg), SOURCE)))
    return cookie_list(cfg)


def write_cookie_file(text: str) -> str:
    """Replace the jar with *text*, atomically, and return its path."""
    return write_jar(youtube.cookies_path(), text)


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
    ``parse_cookies``): junk is refused with the reason instead of being
    written, because a jar yt-dlp cannot read turns every later download into a
    failure whose cause is nowhere near the download.

    The file then loses every cookie for a host YouTube is not sent (a browser
    export is the whole profile), and a file with no youtube.com cookie in it
    is refused rather than stored: it cannot sign anything in, and replacing a
    jar that worked with one that cannot would cost the user their session.
    ``filtered`` says how many cookie lines stayed out.
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
    cookies, error = parse_cookies(text)
    if error:
        raise HTTPException(400, error)
    filtered_text, kept, dropped = filter_jar(text, COOKIE_HOSTS)
    if not any(hosts_match(cookie.domain, (REQUIRED_HOST,)) for cookie in kept):
        raise HTTPException(
            400, "no youtube.com cookie in this file — sign in to YouTube in "
                 "the browser you exported from, then export it again (a jar "
                 "from a signed-out profile carries no session); nothing was "
                 "saved")
    cfg = load_config()
    notes = _note_text(cfg)
    # A jar written by this app carries its notes in its own lines: importing
    # one back absorbs them, so the comments survive a machine move or a config
    # that lost them. The store wins where both have a note — that is the one
    # the user last edited.
    for key, comment in jar_comments(text).items():
        if key not in comments_of(notes, SOURCE):
            notes = set_note(notes, SOURCE, key, comment=comment)
    file_notes = comments_of(notes, SOURCE)
    write_cookie_file(render_jar(filtered_text, file_notes))
    _save_notes(cfg, notes)
    state = cookie_state()
    state["warnings"] = cookie_warnings(kept)
    state["filtered"] = dropped
    return state


@router.delete("/api/youtube/cookies")
def youtube_cookies_delete():
    """Remove the jar (the mode setting is left alone: it is not this route's).

    Deleting the file rather than blanking it means `file` mode with no jar
    passes NO cookie option at all (see server/youtube.py's cookie_opts), so a
    user who deletes the jar gets plain anonymous downloads back instead of
    yt-dlp failing on an empty file.

    The per-cookie notes are left where they are: they are keyed by identity,
    and re-importing the same profile must bring the comments back.
    """
    path = youtube.cookies_path()
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError as e:
            raise HTTPException(500, f"could not remove the cookie file: {e}")
    return cookie_state()
