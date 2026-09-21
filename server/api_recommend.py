"""Recommendation route — the "More like this" shelf's one endpoint.

Thin on purpose: `server/recommend.py` owns the scoring, this only validates
the request and names the answer. An id that is not in the library is an
empty list rather than a 404 — a shelf that has nothing to suggest must not
read as a broken page.

Two verbs, one answer. GET carries a single entity in the query string, which
is what a page link can hold; POST carries a seed LIST in its body, which a
playlist (hundreds of paths) or a favourites set cannot fit into a URL, and
is what the UI's shelf uses for every context.
"""
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from mlo import load_config
from server import recommend
from server.auth import current_user

router = APIRouter(tags=["recommend"])


class RecommendBody(BaseModel):
    kind: str = "album"
    id: str = ""
    target: str = ""
    # Paths or `mb:<uuid>` refs; each may name a track file, an album folder or
    # an artist folder (see `recommend._seed_entries`).
    seeds: list[str] = []
    limit: int | None = None


def _check(kind: str, target: str):
    kind = str(kind or "").strip().lower()
    if kind not in recommend.KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(recommend.KINDS)}")
    target = str(target or "").strip().lower()
    if target and target not in recommend.TARGETS:
        raise HTTPException(400, f"target must be one of {', '.join(recommend.TARGETS)}")
    return kind, target


def _answer(request: Request, kind: str, ref: str, target: str, seeds, limit):
    """Scored items, or [] when anything about the request went wrong.

    The shelf is decoration: a broken index or playlist read degrades to "no
    suggestions" instead of breaking the page under it.
    """
    kind, _ = _check(kind, target)
    try:
        items = recommend.recommend(load_config(), kind, str(ref or ""), limit,
                                    user=current_user(request), seeds=seeds,
                                    target=target)
    except Exception:
        items = []
    return {"kind": kind, "id": str(ref or ""), "target": target, "items": items}


@router.get("/api/recommend")
def recommend_items(
    request: Request,
    kind: str = Query("album", description=f"one of {', '.join(recommend.KINDS)}"),
    id: str = Query("", description="library path or mb:<uuid> reference"),
    target: str = Query("", description="albums | tracks (defaults per kind)"),
    seeds: list[str] | None = Query(None, description="seed refs, repeatable"),
    limit: int | None = Query(None, description=f"1..{recommend.MAX_LIMIT}"),
):
    """Library items similar to one artist, album, track, playlist or seed set.

    Scored locally from the library's own genre / mood / energy / year /
    artist tags, so the answer explains itself: every item carries the
    `reasons` its score came from. A playlist id and the favourites a
    `kind=favorites` request reads are the caller's own scope, like every
    other playlist read.
    """
    return _answer(request, kind, id, target, seeds, limit)


@router.post("/api/recommend")
def recommend_body(request: Request, body: RecommendBody):
    """Same answer as GET, for a seed list too long to be a URL.

    `kind=tracks` (or `albums`) scores the explicit `seeds`; `favorites` reads
    the caller's own likes (track shelf) or favourite albums and artists
    (album shelf) and ignores `seeds`.
    """
    return _answer(request, body.kind, body.id, body.target, body.seeds, body.limit)
