"""Rating routes — one star row per track, half-star precision.

The store (``server.ratings``) owns both halves of a rating: the row in
ratings.db and the file's own ``RATING`` tag, gated by ``write_rating_tags``.
This module is the HTTP surface from the contract:

    GET  /api/ratings                    -> {"ratings": {...}, "counts": {...}}
    GET  /api/ratings?paths=a&paths=b    -> only those paths
    PUT  /api/ratings                    -> {"ok", "path", "rating", "tag"}
    POST /api/ratings/bulk               -> {"ok", "updated", "failed", ...}

Everything is scoped to the caller (``auth.current_user``), so two people on
one server never see each other's stars.
"""
import os
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


class RatingBulk(BaseModel):
    paths: list[str] = []
    rating: Any = None


def _parse(rating):
    try:
        return ratings.parse_half_stars(rating)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/ratings")
def ratings_get(request: Request = None, paths: Optional[list[str]] = Query(None)):
    """Ratings as {path: half-stars}, plus how many tracks sit at each value.

    With `paths` the answer covers only those tracks — the call a page makes
    for the rows it is drawing — and a requested track with no row takes its
    value from its own RATING tag (the bounded read that makes an imported
    Picard library show its stars). Without `paths` the whole store is
    returned and no file is opened.
    """
    user = auth_mod.current_user(request)
    return {"ratings": ratings.map_for(paths=paths, user=user),
            "counts": ratings.counts(user=user)}


@router.put("/api/ratings")
def ratings_put(req: RatingPut, request: Request = None):
    """Rate one track (0 clears the rating and removes the tag).

    A path whose file is not there is a 404, not a stored row: the rating is
    per track and the MBID that keeps it alive across a rename is read from
    the FILE, so a row for a vanished path could never heal — it would be a
    star the client draws on a track that no longer exists.
    """
    user = auth_mod.current_user(request)
    path = str(req.path or "").strip()
    if not path:
        raise HTTPException(400, "path required")
    _parse(req.rating)
    if not os.path.isfile(path):
        raise HTTPException(404, ratings.MISSING_FILE)
    out = ratings.rate(path, req.rating, user=user, cfg=load_config())
    return {"ok": True, **out}


@router.post("/api/ratings/bulk")
def ratings_bulk(req: RatingBulk, request: Request = None):
    """Rate many tracks at once.

    A path that cannot be rated is reported in `failed`; a path whose row was
    stored but whose file tag could not be written stays in `updated` and is
    also listed in `tags_failed`, so a container that refuses the tag never
    costs the user the rating itself.
    """
    user = auth_mod.current_user(request)
    _parse(req.rating)
    out = ratings.bulk_rate(req.paths, req.rating, user=user, cfg=load_config())
    return {"ok": True, **out}
