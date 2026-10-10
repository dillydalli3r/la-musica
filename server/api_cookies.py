"""One per-cookie view for every cookie login — GET/POST /api/cookies/{source}.

The two cookie credentials (`youtube`, RYM) are different on disk — one is a
jar file, the other a header string in the config — but what a user does with
them is the same: import a cookies.txt, look at the cookies it kept, and write
a note against one of them. This module is that shared surface, so the UI has
ONE panel for both (web/src/components/CookieJarPanel.tsx) instead of two that
drift, and so a third cookie login would need no new UI at all.

It reads and writes nothing itself: the sources own their own storage
(`server/api_youtube.cookie_list`/`set_cookie_comment`, and the same pair in
`server/api_rym`), and this module only names them. What it adds is the shape
the panel reads:

  * ``GET /api/cookies/{source}`` — the cookies that are STORED, one row each
    (domain, path, name, expiry, expired, comment) and ``hosts``: the hosts that
    credential is actually sent to, which is what the import filters on. No
    cookie VALUE is ever in it — a `session` cookie is a live credential.
  * ``POST /api/cookies/{source}/comments`` — attach (or clear, with an empty
    ``comment``) one cookie's note, keyed by the cookie's identity rather than
    its position, so a re-import leaves it where it was.

A comment is refused with 404 when the source does not hold that cookie: a note
nothing can carry is a note the panel could not show against anything.

Mounted by server/main.py (``include_router``); this module never imports it.
"""
from typing import Callable, Dict, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server import api_aoty, api_rym, api_youtube
from server.cookies import CookieUpload

router = APIRouter(tags=["cookies"])


class CookieComment(BaseModel):
    """One cookie's identity plus the comment to write against it.

    The identity is the one the list gave back (``domain``, ``path``,
    ``name``); an empty ``comment`` clears the note.
    """
    domain: str = ""
    path: str = "/"
    name: str = ""
    comment: str = ""


# The cookie-bearing credentials, by the id the UI knows them by. Each entry
# names the hosts that credential is sent to — the panel says so next to the
# import box, because it is the reason a whole browser export does not end up
# in the app — plus the two functions its own module provides. There is no
# entry for any other credential: a token or an API key is not a cookie, and
# inventing a jar for one would be a surface that cannot work.
SOURCES: Dict[str, dict] = {
    "youtube": {
        "hosts": list(api_youtube.COOKIE_HOSTS),
        "list": api_youtube.cookie_list,
        "comment": api_youtube.set_cookie_comment,
    },
    "rym": {
        "hosts": [api_rym.RYM_HOST],
        "list": api_rym.cookie_list,
        "comment": api_rym.set_cookie_comment,
        # The source's own UPLOAD route, called with the jar read out of the
        # browser: it validates (all four shapes), keeps only this host's
        # cookies, stores the header `integrations._rym_cookie` reads, and
        # reports what a signed-out or expired export is worth — so a browser
        # read and a paste cannot drift apart.
        "upload": api_rym.rym_cookies_post,
        # The UA half of the credential: Cloudflare binds `cf_clearance` to the
        # browser that earned it, so a jar read out of a browser is only half
        # of what that browser sends (see `server/browsercookies.py`).
        "ua_key": "rym_user_agent",
    },
    "aoty": {
        "hosts": [api_aoty.AOTY_HOST],
        "list": api_aoty.cookie_list,
        "comment": api_aoty.set_cookie_comment,
        "upload": api_aoty.aoty_cookies_post,
        "ua_key": "aoty_user_agent",
    },
}


def _source(name: str) -> dict:
    """The registry entry for *name*, or a 404 naming what exists."""
    spec = SOURCES.get(str(name or ""))
    if spec is None:
        raise HTTPException(
            404, f"unknown cookie source {name!r} — this app keeps cookies for "
                 + ", ".join(sorted(SOURCES)))
    return spec


def _payload(name: str, spec: dict, cookies: List[dict]) -> dict:
    """The answer both routes give: what is stored, and where it is sent."""
    return {
        "source": name,
        "hosts": list(spec["hosts"]),
        "present": bool(cookies),
        "cookies": cookies,
    }


@router.get("/api/cookies/browsers")
def cookies_browsers():
    """The browsers this machine has a signed-in profile for.

    Declared BEFORE `/api/cookies/{source}` so "browsers" is a route and not a
    source name that 404s. Each row carries the User-Agent that browser sends —
    which is half of what a cleared session is (see
    `server/browsercookies.py`) — so the panel can offer "read it from Firefox"
    instead of asking the user to export a file.
    """
    from server import browsercookies

    return {"browsers": browsercookies.summary(),
            "installed": bool(browsercookies.available())}


class BrowserImport(BaseModel):
    """Which browser to read the session out of."""
    browser: str = "firefox"


@router.post("/api/cookies/{source}/from-browser")
def cookies_from_browser(source: str, req: BrowserImport):
    """Read the credential out of a browser the user is already signed in to.

    The whole point of the panel, one step further: rather than exporting a
    `cookies.txt` by hand and pasting it, this asks the yt-dlp the app already
    ships to dump that browser's cookies, keeps only the ones this source is
    sent, stores them, and stores that browser's User-Agent with them.

    Both halves matter. MEASURED, 2026-10-10: RateYourMusic answers `503` to an
    anonymous client on EVERY release page but `200` with the owner's own jar,
    and the same jar with the app's built-in Chrome UA is a `403` — the cookie
    counts only alongside the UA that earned it.
    """
    spec = _source(source)
    upload = spec.get("upload")
    if not callable(upload):
        raise HTTPException(
            400, f"{source} has no browser-readable jar — its own settings "
                 "choose the browser instead (see Settings → Sources)")
    from server import browsercookies
    from server import cookies as cookies_mod

    text = browsercookies.jar(req.browser)
    if not text:
        raise HTTPException(
            400, f"could not read {req.browser}'s cookies — install yt-dlp "
                 "from Dependencies, or pick another browser (a profile that "
                 "has never visited the site has nothing to give)")
    # A whole browser profile's jar is ~2000 cookies and megabytes; this source
    # is sent a handful. Filter to its hosts FIRST — the upload route's own
    # size guard is about the user pasting a password store, not about us —
    # then hand it the slice, so its validation, storage and warnings are the
    # ones a paste would get.
    filtered, kept_rows, _dropped = cookies_mod.filter_jar(text, spec["hosts"])
    if not kept_rows:
        raise HTTPException(
            400, f"{req.browser} holds no {'/'.join(spec['hosts'])} cookie — "
                 "open the site in that browser once, signed in, and read it "
                 "again")
    state = upload(CookieUpload(text=filtered))
    kept = int((state or {}).get("stored") or 0)
    if not kept:
        raise HTTPException(
            400, f"{req.browser} holds no usable {'/'.join(spec['hosts'])} "
                 "cookie — open the site in that browser once, signed in, and "
                 "read it again")
    ua = browsercookies.user_agent(req.browser)
    key = spec.get("ua_key")
    if ua and key:
        from mlo.config import load_config, save_config
        cfg = load_config() or {}
        if cfg.get(key) != ua:
            cfg[key] = ua
            if not save_config(cfg):
                reason = getattr(save_config, "last_error", "") or ""
                raise HTTPException(
                    500, f"the cookies were saved but the User-Agent was not"
                         f"{(': ' + reason) if reason else ''}")
    out = _payload(source, spec, spec["list"]())
    out.update({"browser": req.browser, "user_agent": ua or "",
                "stored": kept,
                "warnings": list((state or {}).get("warnings") or [])})
    return out


@router.get("/api/cookies/{source}")
def cookies_get(source: str):
    """One cookie credential's cookies, with each one's comment.

    Read by the shared cookie panel, which shows the list inline under the
    import box (domain, name, expiry, comment) — so the user can see what was
    kept, and note WHICH account a cookie belongs to before a second import
    replaces it.
    """
    spec = _source(source)
    list_cookies: Callable[[], List[dict]] = spec["list"]
    return _payload(source, spec, list_cookies())


@router.post("/api/cookies/{source}/comments")
def cookies_comment(source: str, req: CookieComment):
    """Write one cookie's comment; answers with the new list.

    The comment is stored against the cookie's identity (domain, path, name) in
    the shared notes store, which is what makes it survive a re-import: the next
    export of the same profile holds the same cookie, and the note finds it
    again rather than by the line number it happened to sit on. For the jar
    credential the note is also written into the file itself, as a Netscape
    comment line above the cookie it describes.
    """
    spec = _source(source)
    set_comment: Callable[..., List[dict]] = spec["comment"]
    cookies = set_comment(req.domain, req.path, req.name, req.comment)
    return _payload(source, spec, cookies)
