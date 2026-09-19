"""Recommendation route — the "More like this" shelf's one endpoint.

Thin on purpose: `server/recommend.py` owns the scoring, this only validates
the request and names the answer. An id that is not in the library is an
empty list rather than a 404 — a shelf that has nothing to suggest must not
read as a broken page.
"""
from fastapi import APIRouter, HTTPException, Query, Request

from mlo import load_config
from server import recommend
from server.auth import current_user

router = APIRouter(tags=["recommend"])


@router.get("/api/recommend")
def recommend_items(
    request: Request,
    kind: str = Query("album", description="artist | album | track | playlist"),
    id: str = Query("", description="library path or mb:<uuid> reference"),
    limit: int = Query(recommend.DEFAULT_LIMIT, description=f"1..{recommend.MAX_LIMIT}"),
):
    """Library items similar to one artist, album, track or playlist.

    Scored locally from the library's own genre / mood / energy / year /
    artist tags, so the answer explains itself: every item carries the
    `reasons` its score came from. A playlist id is read from the caller's own
    scope, like every other playlist read.
    """
    kind = str(kind or "").strip().lower()
    if kind not in recommend.KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(recommend.KINDS)}")
    try:
        items = recommend.recommend(load_config(), kind, str(id or ""), limit,
                                    user=current_user(request))
    except Exception:
        # The shelf is decoration: a broken index or playlist read degrades to
        # "no suggestions" instead of breaking the page under it.
        items = []
    return {"kind": kind, "id": str(id or ""), "items": items}
