"""Recommendation routes — the "More like this" shelf and the queue's own
extension.

Thin on purpose: `server/recommend.py` owns the scoring, this only validates
the request and names the answer. An id that is not in the library is an
empty list rather than a 404 — a shelf that has nothing to suggest must not
read as a broken page.

Two verbs, one answer. GET carries a single entity in the query string, which
is what a page link can hold; POST carries a seed LIST in its body, which a
playlist (hundreds of paths) or a favourites set cannot fit into a URL, and
is what the UI's shelf uses for every context.

`/api/recommend/queue` is the same pair for infinite playback: its seeds are a
PLAYING QUEUE, so it is scored against tracks, its answer is bounded to a
handful of rows, and nothing it returns is ever a track the queue already
holds. GET carries the paths a small queue fits into a URL; POST carries the
rest. Driven by `tools/test_recommendations.py`.
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


class QueueBody(BaseModel):
    """A queue too long for a URL — the paths the GET spells out, and the same
    batch size it honours (bounded server-side either way)."""
    paths: list[str] = []
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
    the caller's own likes (track shelf) or favourite albums and artists (album
    shelf) and ignores `seeds`.
    """
    return _answer(request, body.kind, body.id, body.target, body.seeds, body.limit)


@router.get("/api/recommend/for-you")
def recommend_for_you(
    request: Request,
    limit: int = Query(12, ge=1, le=recommend.MAX_LIMIT,
                       description="rows to return"),
    exclude: list[str] | None = Query(None, description="album paths already shown"),
):
    """Owned albums picked for THIS listener (Home's "For you" shelf).

    Not a similarity shelf: the score is the person's own plays, stars and
    likes folded into an artist/genre/mood affinity (`server.recommend.
    listener_affinity`), with unplayed records ranked above worn-out ones. Every
    row carries the reason it is here, and an empty profile answers `[]` rather
    than a random list — a recommendation with no evidence behind it is not one.

    `exclude` names album paths already on another shelf, so the same card does
    not appear twice on one page. The row shape is the Home shelf's own (the
    library album row plus `reason`/`reasons`/`affinity_score`), because the
    Library's strip draws the same `AlbumCard`.
    """
    cfg = load_config()
    try:
        from server import recommendations
        return {"items": recommendations.for_you_shelf(
            cfg, user=current_user(request), limit=limit,
            exclude=exclude or ())}
    except Exception:
        return {"items": []}


@router.get("/api/recommend/acquire")
def recommend_acquire_status():
    """What the recommendation-acquisition worker last did and when it is due.

    The worker is bounded by its own switches and cap (see
    server.recommend_worker); this is its readout, not a second controller.
    """
    from server import recommend_worker
    return recommend_worker.status()


@router.post("/api/recommend/acquire")
def recommend_acquire_now():
    """Kick one acquisition cycle now, in the background.

    The worker's own interval is bypassed; its switches, type filter and cap
    are not. A cycle already in flight answers `ok: false` with its sentence
    rather than starting a second one.
    """
    import threading
    from mlo import import_policy
    from server import recommend_worker

    cfg = load_config()
    if not cfg.get("recommendation_auto_acquire_enabled", True):
        return {"ok": False,
                "reason": "recommendation acquisition is switched off "
                          "(Settings → For you)",
                "status": recommend_worker.status()}
    if not import_policy.auto_acquisition_enabled(cfg):
        return {"ok": False,
                "reason": import_policy.AUTO_OFF_NOTE,
                "status": recommend_worker.status()}
    status = recommend_worker.status()
    if status.get("running"):
        return {"ok": False, "reason": "a cycle is already running", "status": status}
    threading.Thread(target=recommend_worker.run_cycle, kwargs={"force": True},
                     daemon=True, name="recommend-acquire-now").start()
    return {"ok": True, "status": status}


def _queue_answer(request: Request, paths, limit):
    """The queue's batch, or [] when anything about the request went wrong.

    A queue that cannot be extended is not an error: the play simply ends where
    it ended before the switch existed, so a bad `limit`, a broken index or an
    empty library degrades to "nothing to add" instead of breaking the caller
    that plays music with this answer.

    The request's user rides along: the tag similarity is the floor, and the
    person's own plays/stars re-rank the batch (server.recommend's
    `recommend_for_queue`). In a loopback install with no sessions this is
    "" — the default scope, which is exactly who the plays belong to.
    """
    try:
        items = recommend.recommend_for_queue(
            load_config(), paths, limit, user=current_user(request))
    except Exception:
        items = []
    return {"items": items}


@router.get("/api/recommend/queue")
def recommend_queue(
    request: Request,
    paths: list[str] | None = Query(None, description="the playing queue's own track paths, repeatable"),
    limit: int | None = Query(None, description=f"1..{recommend.QUEUE_MAX_LIMIT} (one batch; default {recommend.QUEUE_DEFAULT_LIMIT})"),
):
    """Tracks similar to a PLAYING QUEUE, to keep it going past its last row.

    `paths` is the queue itself, and it plays both roles: the seed set the
    profile is scored from (locally, from the library's own tags, so this works
    offline) and the exclusion set — a similar track the queue already holds is
    never handed back. The answer is tracks, and it is bounded to a handful of
    rows (`recommend.QUEUE_MAX_LIMIT`); an empty library, a queue the library
    no longer holds, or a queue whose tracks share nothing with anything else
    answers `[]`, and the play then simply ends as it did before.
    """
    return _queue_answer(request, paths, limit)


@router.post("/api/recommend/queue")
def recommend_queue_body(request: Request, body: QueueBody):
    """Same answer as GET, for a queue (a playlist's worth of paths) too long
    to fit in a URL."""
    return _queue_answer(request, body.paths, body.limit)
