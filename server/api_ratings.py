"""Rating routes — one star row per entity, half-star precision.

The store (``server.ratings``) owns the whole rating: the row in ratings.db and
— for a TRACK only — the file's own ``RATING`` tag, gated by
``write_rating_tags``. Every rating names the SCOPE it belongs to:

    track   a file, keyed by its path (the default, and the only scope that
            carries a tag)
    album   an album folder, keyed by that folder's path
    artist  an artist folder, keyed by that folder's path

An album or artist rating is the caller's verdict on that ENTITY, never the
average of its tracks — the album page draws both, labelled, and the store keeps
the three scopes in rows of their own, so one can never stand in for another.
A folder has no file to tag, so the reply says so instead of pretending a tag
was written.

This module is the HTTP surface from the contract:

    GET  /api/ratings[?scope=track|album|artist][&paths=a&paths=b]
         -> {"scope", "ratings": {...}, "counts": {...}}
    PUT  /api/ratings       -> {"ok", "scope", "path", "rating", "tag"}
    POST /api/ratings/bulk  -> {"ok", "updated", "failed", ...}   (tracks only)

Everything is scoped to the caller (``auth.current_user``), so two people on
one server never see each other's stars.
"""
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from mlo import load_config
from server import auth as auth_mod
from server import ratings

router = APIRouter(tags=["ratings"])


class RatingPut(BaseModel):
    path: str = ""
    # `Any`, not `int`: the store's own parser is what validates the value, so
    # junk answers the contract's 400 with a sentence instead of pydantic's
    # 422 with a schema dump.
    rating: Any = None
    # Absent means "track": every caller written before the folder scopes
    # existed keeps its meaning exactly.
    scope: str = "track"


class RatingBulk(BaseModel):
    paths: list[str] = []
    rating: Any = None


def _parse(rating):
    try:
        return ratings.parse_half_stars(rating)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _scope(value):
    """The scope a request names, or a 400 listing the three that exist."""
    try:
        return ratings.parse_scope(value)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/ratings")
def ratings_get(request: Request = None, paths: Optional[list[str]] = Query(None),
                scope: str = "track"):
    """Ratings as {path: half-stars}, plus how many rows sit at each value.

    `scope` picks what those paths NAME and defaults to `track`. With `paths`
    the answer covers only those entities — the call a page makes for the rows
    it is drawing — and in the track scope a requested track with no row takes
    its value from its own RATING tag (the bounded read that makes an imported
    Picard library show its stars). Without `paths` the whole scope is returned
    and no file is opened. `counts` is the requested scope's own histogram.
    """
    kind = _scope(scope)
    user = auth_mod.current_user(request)
    return {"scope": kind,
            "ratings": ratings.map_for(paths=paths, user=user, scope=kind),
            "counts": ratings.counts(user=user, scope=kind)}


@router.put("/api/ratings")
def ratings_put(req: RatingPut, request: Request = None):
    """Rate one entity (0 clears the rating; in the track scope it removes the
    file's tag too).

    The target has to be there: a track path whose file is gone is a 404, and
    so is an album or artist path that is no folder of this library — the MBID
    that keeps a row alive across a rename is read from the FILE for a track and
    out of the payload for a folder, so a row for a target that does not exist
    could never heal. It would be a star the client draws on something that is
    not there.

    Only the track scope writes a file tag, and the reply says which happened:
    `tag` is the tag write's own report for a track, and None for a folder,
    which has no tag to carry.
    """
    kind = _scope(req.scope)
    user = auth_mod.current_user(request)
    path = str(req.path or "").strip()
    if not path:
        raise HTTPException(400, "path required")
    _parse(req.rating)
    cfg = load_config()
    reason = ratings.target_missing(kind, path, cfg)
    if reason:
        raise HTTPException(404, reason)
    out = ratings.rate(path, req.rating, user=user, cfg=cfg, scope=kind)
    return {"ok": True, "scope": kind, **out}


@router.post("/api/ratings/bulk")
def ratings_bulk(req: RatingBulk, request: Request = None):
    """Rate many tracks at once.

    A path that cannot be rated is reported in `failed`; a path whose row was
    stored but whose file tag could not be written stays in `updated` and is
    also listed in `tags_failed`, so a container that refuses the tag never
    costs the user the rating itself.

    Tracks only, deliberately: this is the many-FILES call — a selection of
    rows, a whole playlist — and a selection of files names no album or artist
    folder. An album or artist rating is one verdict on one folder, made
    through the single call that can also refuse an unknown folder.
    """
    user = auth_mod.current_user(request)
    _parse(req.rating)
    out = ratings.bulk_rate(req.paths, req.rating, user=user, cfg=load_config())
    return {"ok": True, "scope": "track", **out}
