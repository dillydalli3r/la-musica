"""Import routes: the wizard's finish step, the bulk queue and AcoustID.

Mounted by ``server.main`` (``include_router``); every handler only guards its
paths and delegates to :mod:`server.imports`, so the engine never sees an HTTP
type. The wizard and the React pages depend on these exact paths and field
names:

    POST /api/import/acoustid         {paths: [str]}
         -> {available, note, albums: [{path, release_group_id,
             release_group_title, release_group_type, artists, score,
             matched, total, recordings}]}

    POST /api/import/finish           {paths: [str], force?: {script_id: bool}}
         -> {albums: [{path, scripts, chain, errors}]}

    POST /api/import/bulk             {items: [{path, move?, release?}]}
         -> {ok: True, job: {...}} | {ok: False, error: "…"}

    GET  /api/import/bulk/status      -> {id, kind, status, started, finished,
                                          total, done, label, items, error}

    POST /api/import/scripts/preview  {paths?: [str]}
         -> {chain: [ids], labels: {id: label}, count: n}

Path containment mirrors ``server.main._in_music_folder`` — imported inside
the handler on purpose, since main imports this module to mount the router.
"""
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server import imports

router = APIRouter()


class AcoustidRequest(BaseModel):
    paths: List[str] = []
    # Write the accepted match into the files as ACOUSTID_ID /
    # ACOUSTID_FINGERPRINT (what the wizard sends when the user accepts).
    apply: bool = False
    staged: bool = False  # the wizard's album folder, wherever the user put it


class FinishRequest(BaseModel):
    paths: List[str] = []
    # script id -> force flag; a supplied dict is authoritative (see
    # server.script_runners._apply_force)
    force: Optional[Dict[str, bool]] = None
    staged: bool = False  # the wizard's album folder, wherever the user put it


class BulkItem(BaseModel):
    path: str
    move: Optional[bool] = None
    release: Optional[dict] = None


class BulkRequest(BaseModel):
    items: List[BulkItem] = []


class PreviewRequest(BaseModel):
    paths: List[str] = []


# Every item here costs real work (a full script chain, up to 12 fpcalc
# subprocesses + AcoustID lookups, or a folder move), and these handlers are
# synchronous, so they occupy the API's worker threads. Cap the batch sizes:
# beyond this the work belongs in the bulk queue, which has its own job state
# and a bounded worker pool.
MAX_PATHS = 50
MAX_ITEMS = 200


def _cap(count, limit, what):
    if count > limit:
        raise HTTPException(
            413, f"too many {what} in one request ({count} > {limit}) — "
                 f"split it into several calls or use the bulk queue")


def _guard(paths, staged=False):
    """Refuse any path outside the music folder (400), like every other route.

    `staged` is the import wizard's opt-in allowance for its own album: the
    wizard runs on albums that are not in the library yet (a finished
    download, a folder the user pointed it at), and the path still has to
    exist. Nothing else passes it, so a library-facing call stays as strict as
    before.

    An empty list guards nothing, so it returns before touching the music
    folder: the wizard asks `/api/import/scripts/preview` with no paths on
    first run, where no music folder is configured yet and the preview is
    still perfectly answerable.
    """
    wanted = [p for p in (paths or []) if str(p).strip()]
    if not wanted:
        return
    from server.main import _allow_staged, _in_music_folder, _music_folder
    folder = _music_folder()
    for p in wanted:
        if not _in_music_folder(p, folder) and not _allow_staged(p, staged):
            raise HTTPException(400, f"path outside music folder: {p}")


@router.post("/api/import/acoustid")
def import_acoustid(req: AcoustidRequest):
    """Fingerprint albums with AcoustID and report their release groups.

    `apply: true` also writes the accepted match's identity tags into the
    files (`ACOUSTID_ID`, `ACOUSTID_FINGERPRINT`).
    """
    _cap(len(req.paths), MAX_PATHS, "paths")
    _guard(req.paths, req.staged)
    return imports.acoustid_match(req.paths, apply=bool(req.apply))


@router.post("/api/import/finish")
def import_finish(req: FinishRequest):
    """Run the configured import chain over each album folder, synchronously."""
    _cap(len(req.paths), MAX_PATHS, "paths")
    _guard(req.paths, req.staged)
    return {"albums": [imports.finish_album(p, force=req.force)
                       for p in req.paths]}


@router.post("/api/import/bulk")
def import_bulk(req: BulkRequest):
    """Queue albums (staging folders or library folders) for import."""
    _cap(len(req.items), MAX_ITEMS, "items")
    _guard([item.path for item in req.items])
    items = [{"path": item.path, "move": item.move, "release": item.release}
             for item in req.items]
    return imports.start_bulk(items)


@router.get("/api/import/bulk/status")
def import_bulk_status():
    """Poll the bulk job started by POST /api/import/bulk."""
    return imports.job_state()


@router.post("/api/import/scripts/preview")
def import_scripts_preview(req: PreviewRequest = PreviewRequest()):
    """Exactly which scripts a finish/import will run, in order."""
    _guard(req.paths)
    from mlo.config import load_config
    from server.script_runners import RUNNERS
    chain = imports.chain_for(load_config())
    return {"chain": chain,
            "labels": {sid: RUNNERS[sid][0] for sid in chain if sid in RUNNERS},
            "count": len(chain)}
