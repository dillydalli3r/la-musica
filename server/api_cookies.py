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

from server import api_rym, api_youtube

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
