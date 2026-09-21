"""Import routes: the wizard's finish step, the bulk queue and AcoustID.

Mounted by ``server.main`` (``include_router``); every handler only guards its
paths and delegates to :mod:`server.imports`, so the engine never sees an HTTP
type. The wizard and the React pages depend on these exact paths and field
names:

    POST /api/import/acoustid         {paths: [str], apply?: bool, staged?: bool,
                                       match?: {release_group_id, recordings:
                                       [{path, recording_id, fingerprint}]}}
         -> {available, note, ok, code, albums: [{path, release_group_id,
             release_group_title, release_group_type, artists, score,
             matched, total, recordings, tagged, writes, status, code, reason,
             conflict, conflicts, skips, failures}]}
         `status` is "matched" | "no_match" | "skipped" | "error" per album:
         a fingerprint or lookup that FAILED is an "error" with its `reason`,
         never a "no_match", and `conflicts` is where the audio disagrees with
         the album's tags. `note` carries the first failure's sentence.
         `apply` writes the match into the files; passing the row the
         apply=false pass returned as `match` writes from it without a second
         fpcalc/lookup, and `writes` answers per track ({path, ok, code,
         reason}) so a container this app cannot tag is named, not silent.

    POST /api/import/acoustid/submit  {paths: [str], staged?: bool, confirm: bool}
         -> {available, note, ok, code, submitted, failed, skips,
             submissions, tracks}
         Publishes the ACOUSTID_FINGERPRINT/ID pair the files already carry to
         AcoustID (nothing is written locally). `confirm` is required — it is
         a public, outward-facing submission — and a refused `acoustid_user_key`
         answers in the service's own words.

    POST /api/import/finish           {paths: [str], force?: {script_id: bool}}
         -> {albums: [{path, scripts, chain, errors}]}

    POST /api/import/bulk             {items: [{path, move?, release?}]}
         -> {ok: True, job: {...}} | {ok: False, error: "…"}

    GET  /api/import/bulk/status      -> {id, kind, status, started, finished,
                                          total, done, label, items, error}

    POST /api/import/scripts/preview  {paths?: [str]}
         -> {chain: [ids], labels: {id: label}, count: n}

    GET  /api/import/prompts          -> {prompts: [{album, album_name, at,
                                          mode, reason, link, families: [...]}]}

    POST /api/import/prompts/dismiss  {path, staged?}
         -> {ok: bool}

Every route that IMPORTS answers 409 with ``manual_import_enabled``'s
sentence while that switch is off (``_require_manual``); the read-only ones
(the prompts list, the script preview, the bulk job's status) keep answering.

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
    # The row the apply=False pass already answered with (release group +
    # `recordings`: path, recording id, fingerprint). With it the apply pass
    # writes straight from that payload instead of re-running fpcalc and the
    # lookups — a transient failure on the second pass used to turn a displayed
    # match into zero tags.
    match: Optional[dict] = None


class AcoustidSubmitRequest(BaseModel):
    paths: List[str] = []
    staged: bool = False  # the wizard's album folder, wherever the user put it
    # Publishing to AcoustID is outward-facing and PUBLIC: the caller has to
    # say so explicitly (the two-press pattern the LRCLIB publish panel uses).
    # Without it there is no network call at all.
    confirm: bool = False


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


def _require_manual():
    """Refuse an import action while `manual_import_enabled` is off.

    The switch covers the wizard and every route that IMPORTS something — a
    script chain, a bulk run, a fingerprinting pass. Off, each of them answers
    409 with the same sentence naming the setting, so the UI can say why
    instead of offering a button that comes back refused. A user who turned it
    off wants the automatic pipeline (or nothing) to import; the routes that
    only READ or DISMISS (``/api/import/prompts``, ``scripts/preview``, the
    bulk job's status) keep answering, so the queue can still say what waits.
    """
    from mlo import import_policy
    from mlo.config import load_config

    if not import_policy.manual_import_enabled(load_config()):
        raise HTTPException(409, import_policy.MANUAL_OFF_NOTE)


@router.post("/api/import/acoustid")
def import_acoustid(req: AcoustidRequest):
    """Fingerprint albums with AcoustID and report their release groups.

    `apply: true` also writes the accepted match's identity tags into the
    files (`ACOUSTID_ID`, `ACOUSTID_FINGERPRINT`); with `match` (the row the
    apply=False pass returned) it writes straight from that payload and runs no
    fpcalc and no lookup at all.
    """
    _require_manual()
    supplied = req.match if isinstance(req.match, dict) else None
    _cap(len(req.paths), MAX_PATHS, "paths")
    # The supplied match's recordings are WRITTEN TO, so they are guarded like
    # every other path this route touches.
    _guard(list(req.paths) + _matched_paths(supplied), req.staged)
    return imports.acoustid_match(req.paths, apply=bool(req.apply),
                                  match=supplied)


@router.post("/api/import/acoustid/submit")
def import_acoustid_submit(req: AcoustidSubmitRequest):
    """Give AcoustID the fingerprints the files in these albums already carry.

    The one outward-facing route of the AcoustID path: it submits to a public
    database and writes NOTHING locally (the `ACOUSTID_ID` /
    `ACOUSTID_FINGERPRINT` pair is read back off the files with `get_tag`).
    `confirm: true` is required — the UI asks on a second, explicitly-labelled
    press — and without it this route makes no request at all. `acoustid_user_key`
    is the credential that makes it work; a key AcoustID refuses comes back in
    its own words.
    """
    _require_manual()
    _cap(len(req.paths), MAX_PATHS, "paths")
    _guard(req.paths, req.staged)
    if not req.confirm:
        raise HTTPException(
            400, "submitting to AcoustID publishes to a public database — "
                 "send confirm: true to go ahead")
    return imports.acoustid_submit(req.paths)


def _matched_paths(match):
    """Every path a supplied match would write to (for the folder guard)."""
    out = []
    for rec in (match or {}).get("recordings") or []:
        if isinstance(rec, dict) and str(rec.get("path") or "").strip():
            out.append(rec["path"])
    return out


@router.post("/api/import/finish")
def import_finish(req: FinishRequest):
    """Run the configured import chain over each album folder, synchronously."""
    _require_manual()
    _cap(len(req.paths), MAX_PATHS, "paths")
    _guard(req.paths, req.staged)
    return {"albums": [imports.finish_album(p, force=req.force)
                       for p in req.paths]}


@router.post("/api/import/bulk")
def import_bulk(req: BulkRequest):
    """Queue albums (staging folders or library folders) for import."""
    _require_manual()
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


class DismissRequest(BaseModel):
    path: str
    staged: bool = False  # the wizard's album folder, wherever the user put it


@router.get("/api/import/prompts")
def import_prompts():
    """Albums an import could not finish, and where each one is decided.

    Raised by ``server.imports.finish_album`` (see
    :mod:`server.import_autonomy`); every entry carries the wizard ``link``
    that lands on the album at the first step needing a decision. The wizard
    lists them, and this is what a client-side prompt surface reads too.
    """
    from mlo.config import load_config
    from server import import_autonomy
    return {"prompts": import_autonomy.prompts(load_config())}


@router.post("/api/import/prompts/dismiss")
def import_prompt_dismiss(req: DismissRequest):
    """Stop asking about this album (the user answered, or does not want to).

    Dismissing is not "resolved": the next import of the same album recomputes
    the gaps and raises the prompt again if the family is still missing.
    """
    from mlo.config import load_config
    from server import import_autonomy
    _guard([req.path], req.staged)
    return {"ok": import_autonomy.clear(req.path, load_config())}
