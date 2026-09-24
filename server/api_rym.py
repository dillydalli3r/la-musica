"""The RateYourMusic credential — GET/POST /api/rym/cookies.

RateYourMusic has no API: server/integrations.py reads its release pages the
way a browser would, and what makes RYM answer with the real page instead of a
challenge is the USER's own signed-in session cookie. `rym_cookie`
(mlo/config.py) IS that credential — one value, in one place, normalised by
`integrations._rym_cookie` and sent to rateyourmusic.com and nowhere else
(`integrations._rym_cookiejar`).

Settings has always had a paste box for it, which is fine for a header a user
can read off devtools — but RYM's `session` cookie is HttpOnly, so no script
(and no "copy the Cookie header" instruction) can ever see it. A browser
extension's Netscape `cookies.txt` export is therefore the ONLY way most users
can hand this credential over at all, and that is what this route is for.

This module adds no storage for the credential itself: the imported cookies are
written into `rym_cookie` through the app's own config writer, as exactly the
`name=value; name=value` string a user could have typed into that box — so the
box and the import cannot disagree, and both `_rym_cookie` and the warm-up jar
pick the value up as they always did.

What a bare header CANNOT carry is per-cookie detail: no domain, no path, no
expiry, and nowhere to hang a comment. So the notes the user attaches to an
imported cookie, and the expiry the export stated for it, live in the shared
per-cookie notes store (`cookie_notes` — see server/cookies.py), keyed by the
identity this credential actually has: the one host it is sent to, the path a
header implies (`/`), and the cookie's name. One identity per stored pair, and
a re-import leaves the comments exactly where they were.

The file format, the strictness and the sentences are the SHARED ones
(server/cookies.py, `parse_cookies`) — the same file the yt-dlp jar accepts.

No route here ever returns, logs or toasts a cookie VALUE: a session cookie is
a live credential, so the settings panel is told names, counts and sentences.

Mounted by server/main.py (``include_router``); this module never imports it.
"""
from typing import List, Tuple

from fastapi import APIRouter, HTTPException

from mlo.config import load_config, save_config
# One Netscape parser and one request model for the whole app: the yt-dlp jar
# and this credential accept the same file, so the file-or-junk rules (and the
# sentences that explain them) are the ones already proven on `/api/youtube`.
from server.cookies import (MAX_COOKIE_BYTES, CookieUpload, cookie_key,
                            cookie_row, expired_warning, hosts_match,
                            notes_for, parse_cookies, set_note)
from server.integrations import _rym_cookie

router = APIRouter(tags=["rym"])

# The only host these cookies are ever sent to (see integrations.
# `_rym_cookiejar`), and the host the import filters on: the extension exports
# the WHOLE browser profile, so a jar is mostly other sites' cookies.
RYM_HOST = "rateyourmusic.com"

# The config key the credential lives in — the source of truth, and the only
# place it is stored.
CONFIG_KEY = "rym_cookie"

# The shared per-cookie notes store, and which slice of it is this credential's
# (server/cookies.py; the same key holds the yt-dlp jar's notes).
NOTES_KEY = "cookie_notes"
SOURCE = "rym"

# The cookie RYM recognises a signed-in account by. It is HttpOnly, which is
# the whole reason this import exists.
SESSION_COOKIE = "session"


def _is_rym_host(host: str) -> bool:
    """Is *host* rateyourmusic.com itself, or a subdomain of it?

    A suffix match on the parsed host, so `www.rateyourmusic.com` and
    `rateyourmusic.com` both count while `rateyourmusic.com.example.net`
    (a domain someone else owns) cannot.
    """
    return hosts_match(host, (RYM_HOST,))


def rym_pairs(cookies) -> List[Tuple[str, str]]:
    """[(name, value)] — the rateyourmusic.com cookies of a parsed file.

    ``cookies`` is what ``parse_cookie_file(text, with_values=True)`` accepted,
    in file order, and file order is the order kept: a `Cookie` header sends
    its pairs as written, and re-ordering them would make the stored value
    differ from the paste it came from. A line with no name is dropped — a
    header cannot carry a pair without one, and `_rym_cookie`/`_rym_cookiejar`
    skip it too.
    """
    return [(name, value) for host, name, value in cookies
            if _is_rym_host(host) and name]


def stored_pairs(value: str) -> List[Tuple[str, str]]:
    """[(name, value)] of a stored `rym_cookie` header string."""
    out = []
    for part in str(value or "").split(";"):
        name, _, val = part.strip().partition("=")
        if name.strip():
            out.append((name.strip(), val))
    return out


def _note_text(cfg: dict) -> str:
    """The raw per-cookie notes store, as saved."""
    return str(cfg.get(NOTES_KEY) or "")


def _save_notes(cfg: dict, text: str) -> None:
    """Write the store through the app's own config writer."""
    if str(cfg.get(NOTES_KEY) or "") == text:
        return
    cfg[NOTES_KEY] = text
    if not save_config(cfg):
        reason = getattr(save_config, "last_error", "") or ""
        raise HTTPException(
            500, f"Failed to save config{(': ' + reason) if reason else ''}")


def stored_warnings(names) -> List[str]:
    """What the stored credential is worth, in sentences worth acting on.

    `session` is the pair rateyourmusic.com uses to recognise a signed-in
    account: without it — or with nothing stored at all — RYM answers as a
    guest, and a release it will not serve falls back to the archived page
    instead of the live one. Empty when there is nothing to say.
    """
    if not names:
        return ["no RateYourMusic cookie saved — rateyourmusic.com is asked as "
                "a guest, so a release it will not serve is read from the "
                "archived copy instead"]
    if SESSION_COOKIE not in names:
        return [f"{len(names)} rateyourmusic.com cookie(s) saved, none named "
                f"`{SESSION_COOKIE}` — that is the pair rateyourmusic.com uses "
                "to recognise a signed-in account, so RYM still answers as a "
                "guest; export cookies.txt again while signed in"]
    return []


def file_warnings(cookies) -> List[str]:
    """What a file that stored NOTHING was worth, in one sentence.

    The case this covers is a well-formed export with no rateyourmusic.com
    cookie in it: the extension writes the whole profile, so a jar from a
    signed-out tab (or from a profile signed in to some other site) parses
    fine and is worth nothing here. Nothing is stored from it, and this says
    which browser state to export from instead.
    """
    return ["no rateyourmusic.com cookie in this file — it is probably "
            "exported from a signed-out tab; the \"cookies.txt\" extension "
            "(Rob W's) writes every cookie the profile holds, so be signed in "
            "to rateyourmusic.com in that browser, then export it again"]


def cookie_state(cfg=None) -> dict:
    """What the STORED credential holds — the answer both routes return.

    ``names`` are the cookie names, in the order they are sent, and that is
    all the UI is ever given: a `session` cookie is a live credential, so no
    route here returns a VALUE. ``sites`` is the one host these cookies are
    ever sent to — the stored value is a bare `Cookie` header, which carries
    no domain of its own.

    There is no `saved_at`: the config holds no per-key timestamp, and the
    config FILE's mtime moves for every unrelated setting, so a date derived
    from it would be wrong exactly when the user looks for it.
    """
    cfg = cfg if cfg is not None else load_config()
    names = [name for name, _value in stored_pairs(_rym_cookie(cfg))]
    return {
        "present": bool(names),
        "lines": len(names),
        "sites": [RYM_HOST] if names else [],
        "names": names,
        "max_bytes": MAX_COOKIE_BYTES,
        "warnings": stored_warnings(names),
    }


def cookie_list(cfg=None) -> List[dict]:
    """The stored cookies, one row per pair — the per-cookie view.

    The identity is the one this credential HAS: the host the header is sent to,
    the path a `Cookie` header implies (`/`), and the name. The expiry and the
    comment come from the shared notes store — the file stated the expiry at
    import time (`rym_cookies_post`), and the comment is the user's own. No
    VALUE is ever here.
    """
    cfg = cfg if cfg is not None else load_config()
    notes = notes_for(_note_text(cfg), SOURCE)
    out = []
    for name, _value in stored_pairs(_rym_cookie(cfg)):
        note = notes.get(cookie_key(RYM_HOST, "/", name), {})
        out.append(cookie_row(RYM_HOST, "/", name,
                              str(note.get("expiry") or ""),
                              str(note.get("comment") or "")))
    return out


def set_cookie_comment(domain: str, path: str, name: str, comment: str,
                       cfg=None) -> List[dict]:
    """Attach (or clear) one stored cookie's comment.

    The stored credential has one identity per pair — the host it is sent to,
    `/`, and the name — so `domain`/`path` are the credential's own, not the
    caller's; a name the stored header does not carry is refused, because a
    comment nothing can carry would be a note the panel cannot show against
    any cookie.

    Written to the shared notes store, so it survives a re-import: the next
    export of the same session holds the same cookies, and the note finds them
    by identity. Nothing else is touched, so the credential the scraper sends
    is exactly what it was.
    """
    cfg = cfg if cfg is not None else load_config()
    name = str(name or "").strip()
    if name not in [n for n, _value in stored_pairs(_rym_cookie(cfg))]:
        raise HTTPException(
            404, f"no {name or 'unnamed'} cookie in the stored RateYourMusic "
                 "credential — the comment was not saved")
    _save_notes(cfg, set_note(_note_text(cfg), SOURCE,
                              cookie_key(RYM_HOST, "/", name), comment=comment))
    return cookie_list(cfg)


def store(value: str) -> None:
    """Write the credential through the app's own config writer.

    ``value`` is exactly what a user could have typed into the settings box
    (`name=value; name=value` — what `integrations._rym_cookie` normalises a
    paste to), so the box, the import and the scraper all agree, and the
    credential survives a restart.
    """
    cfg = load_config()
    cfg[CONFIG_KEY] = value
    if not save_config(cfg):
        reason = getattr(save_config, "last_error", "") or ""
        raise HTTPException(
            500, f"Failed to save config{(': ' + reason) if reason else ''}")


@router.get("/api/rym/cookies")
def rym_cookies_get():
    """The stored credential: which cookie names it holds, and any warnings.

    Read by Settings → Discovery, which shows the names before and after a
    paste. The names come from the CURRENT `rym_cookie` value, through the
    same normaliser the scraper reads it with, so the panel describes what RYM
    is actually sent.
    """
    return cookie_state()


@router.post("/api/rym/cookies")
def rym_cookies_post(req: CookieUpload):
    """Save a pasted or dropped cookies.txt — its rateyourmusic.com cookies.

    The text is validated as a Netscape cookie file FIRST, exactly as the
    yt-dlp jar is (`server.cookies.parse_cookies`): junk is refused with the
    reason instead of replacing a credential that works, and the body is
    refused BY SIZE before anything is parsed. A well-formed export that holds
    no rateyourmusic.com cookie replaces NOTHING — a signed-out tab's export
    must not cost the user the session that was doing the job — and comes back
    with `stored: 0` plus the sentence saying why.

    For the cookies it does store, the expiry the file stated is remembered in
    the notes store (the header has nowhere to carry it) WITHOUT touching any
    comment the user has written, and an already-expired file says so: a stale
    export is the most common way this credential silently stops working.
    """
    text = req.text or ""
    # The shared parser's empty-file sentence is credential-neutral ("export
    # cookies.txt from the browser you are signed in with"), so this route
    # answers an empty paste in RYM's own terms instead.
    if not text.strip():
        raise HTTPException(
            400, "the file is empty — export the cookies of a signed-in "
                 "rateyourmusic.com session to a cookies.txt (the "
                 "\"cookies.txt\" extension writes it), then paste it here")
    size = len(text.encode("utf-8", errors="replace"))
    if size > MAX_COOKIE_BYTES:
        # Rounded UP, as the yt-dlp jar is: the user is being told their file
        # is over the line, not shown a figure that matches it.
        raise HTTPException(
            413,
            f"the cookie file is {(size + 1023) // 1024} KiB — the limit is "
            f"{MAX_COOKIE_BYTES // 1024} KiB. Paste or drop the cookies.txt "
            "itself, not a browser profile folder",
        )
    records, error = parse_cookies(text)
    if error:
        raise HTTPException(400, error)
    cookies = [(record.domain, record.name, record.value) for record in records]
    pairs = rym_pairs(cookies)
    if not pairs:
        state = cookie_state()
        state["stored"] = 0
        state["session"] = SESSION_COOKIE in state["names"]
        state["warnings"] = file_warnings(cookies)
        state["filtered"] = len(records)
        return state
    store("; ".join(f"{name}={value}" for name, value in pairs))
    # What the file said about the cookies it just gave us: the expiry column
    # (kept) and any expired one (warned about). Comments are the user's, and
    # set_note leaves them alone.
    cfg = load_config()
    notes = _note_text(cfg)
    kept = []
    for record in records:
        if not (_is_rym_host(record.domain) and record.name):
            continue
        kept.append(record)
        notes = set_note(notes, SOURCE,
                         cookie_key(RYM_HOST, "/", record.name),
                         expiry=record.expiry)
    _save_notes(cfg, notes)
    state = cookie_state()
    state["stored"] = len(pairs)
    state["session"] = SESSION_COOKIE in state["names"]
    state["filtered"] = len(records) - len(kept)
    expired = expired_warning(kept, "RateYourMusic")
    if expired:
        state["warnings"] = list(state["warnings"]) + [expired]
    return state
