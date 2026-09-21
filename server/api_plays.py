"""Play routes — the ONE seam every client reports a playback start to, and
the charts built from what it collected.

The store (`server.plays`) owns the rows, the user scope and the window maths;
this module is the HTTP surface:

    POST /api/plays            -> {"ok", "path", "album", "started_at"}
    GET  /api/top              -> the library's own most-played rows for a window

`POST /api/plays` is deliberately the only write: the web player bar calls it
the moment a track actually starts (a resume or a seek is not a new play; a
repeat is), and the mobile client calls the same route from its own play path,
so there is one definition of "a play" for every client. It answers to the
caller's own scope (`auth.current_user`), exactly like the ratings and wishes
stores — two people on one server never see each other's history.

`GET /api/top` is READ-ONLY and library-scoped: it returns the caller's own
most-played tracks, albums or artists inside one closed window, with each row's
play count and the window's bounds. The online charts a provider ranks live at
`/api/discover/charts` (server/api_discover.py) — the two are separate on
purpose, because a provider's chart is not the user's history and mixing them
would make a row's provenance a guess.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from mlo import load_config
from server import auth as auth_mod
from server import plays

router = APIRouter(tags=["plays"])


class PlayPost(BaseModel):
    path: str = ""


def record_play(path, user="", cfg=None, now=None):
    """Store one play of *path* for *user* (the plain function the route
    wraps, so the seam is testable without an app)."""
    try:
        return plays.record(path, user=user, at=now)
    except ValueError as e:
        raise HTTPException(400, str(e))


def top_charts(period="all", kind="tracks", limit=plays.DEFAULT_LIMIT, user="",
               cfg=None, lib=None, now=None):
    """The library's own most-played rows for one window (see
    `server.plays.top`). An unusable period or kind is a 400 with the list of
    what is accepted, never a silent fallback to all-time."""
    try:
        return plays.top(period=period, kind=kind, limit=limit, user=user,
                         cfg=cfg, lib=lib, now=now)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/plays")
def plays_post(req: PlayPost, request: Request = None):
    """Record one playback start: `{path}` in, what was written back.

    No file check on purpose — this sits on the playback start path, where a
    stat per play buys nothing: the row records what was PLAYED, and a file the
    user has since moved keeps its history like any other.
    """
    user = auth_mod.current_user(request)
    return {"ok": True, **record_play(req.path, user=user,
                                      cfg=load_config())}


@router.get("/api/top")
def top_get(period: str = Query("all"), kind: str = Query("tracks"),
            limit: Optional[int] = Query(None), request: Request = None):
    """The caller's own most-played rows for one period (see `top_charts`)."""
    user = auth_mod.current_user(request)
    return top_charts(period=period, kind=kind,
                      limit=plays.DEFAULT_LIMIT if limit is None else limit,
                      user=user, cfg=load_config())
