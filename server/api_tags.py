"""MAINTAIN → Tags routes: freshness of the MusicBrainz data in the files.

The store is the FILES themselves (`MUSICBRAINZ_UPDATED` on every track, see
`server.tag_refresh`), so these routes only read the library tree and start
the refresh queue:

    GET  /api/tags/status[?days=]      -> counts, artists, albums, job, schedule
    GET  /api/tags/album?path=         -> one album's per-track stamps
    POST /api/tags/refresh             -> {paths?: [], due?: bool} starts a job
    POST /api/tags/scan                -> alias of status (shape is the same)
    GET  /api/tags/job                 -> the running/last job alone

Scheduling is configuration (`tag_refresh_enabled`, `tag_refresh_days`,
`tag_refresh_workers`) through the normal `/api/config` route — these routes
never write settings.
"""
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from mlo import load_config
from server import tag_refresh

router = APIRouter(tags=["tags"])


class RefreshBody(BaseModel):
    paths: List[str] = []
    due: bool = True


def _cfg():
    return load_config()


@router.get("/api/tags/status")
def tags_status(days: Optional[float] = Query(None), refresh: int = Query(0)):
    """Every album's MusicBrainz freshness and the refresh queue's state.

    `days` overrides the configured age for THIS read (the page's own
    "older than…" preview); it never writes the setting. `refresh=1` drops the
    status memo so a press sees a tag write it just made.
    """
    cfg = dict(_cfg())
    if days is not None:
        try:
            cfg["tag_refresh_days"] = max(1.0, float(days))
        except (TypeError, ValueError):
            raise HTTPException(400, "days must be a number")
    return tag_refresh.status(cfg, force=bool(refresh))


@router.get("/api/tags/album")
def tags_album(path: str = Query("")):
    """One album's per-track pull stamps (what a row expands into)."""
    if not str(path or "").strip():
        raise HTTPException(400, "path required")
    row = tag_refresh.album_detail(_cfg(), path)
    if row is None:
        raise HTTPException(404, "album not found in the library")
    return row


@router.post("/api/tags/refresh")
def tags_refresh(req: RefreshBody):
    """Queue whole albums for a MusicBrainz refresh.

    `paths` names albums; without paths ``due`` (default) refreshes only the
    albums past their configured age, and `due: false` refreshes every linked
    album. One job at a time — a second press answers with the running job
    instead of putting a second set of workers on the same albums.
    """
    return tag_refresh.refresh(paths=req.paths, due_only=bool(req.due),
                               cfg=_cfg(), source="manual")


@router.get("/api/tags/job")
def tags_job():
    """The refresh queue: progress, per-album skip reasons and errors."""
    return {"job": tag_refresh.job_state()}