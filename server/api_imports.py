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
         -> {available, note, ok, code, submitted, known, failed, skips,
             submissions, results, tracks}
         Gives AcoustID the fingerprint + MusicBrainz recording id these files
         state (nothing is written locally). `paths` are album folders OR single
         track paths — a file path is THAT file, not the album it sits in.
         `confirm` is required — it is a public, outward-facing submission — and
         a refused `acoustid_user_key` answers in the service's own words with
         nothing read or sent. `results` is the per-track report (accepted /
         already_known / rejected / skipped, each with its own sentence); a pair
         the service already links, or one this app already submitted, is never
         re-sent.

    POST /api/import/finish           {paths: [str], force?: {script_id: bool}}
         -> {albums: [{path, scripts, chain, errors}]}
         409 with the claim's sentence when an album is already being finished
         by another job (this press never queues behind one); a batch where
         only some albums are busy runs the rest and reports the refused ones
         in their own album entry's `errors`/`note`.

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
    """Give AcoustID the fingerprint + recording id the files in these paths state.

    The one outward-facing route of the AcoustID path: it submits to a public
    database and writes NOTHING locally. `paths` are album folders or single
    track paths — a track path is that track, and the album beside it stays out
    of it. The fingerprint is the file's own `ACOUSTID_FINGERPRINT` or, when it
    carries none, one taken from the AUDIO locally (fpcalc); the recording id is
    the one the file names (`mlo.acoustid._recording_identity`: ACOUSTID_ID,
    MUSICBRAINZ_TRACKID, or the recording MBID this app's naming script wrote
    into the file name). A pair AcoustID already links — or that this app has
    already submitted — is reported `already_known` and never re-sent, and a
    file that names no recording is a named skip (a submission stores a
    fingerprint WITH the recording it is).

    `confirm: true` is required — the UI asks on a second, explicitly-labelled
    press — and without it this route makes no request at all. `acoustid_user_key`
    is the credential that makes it work; a key AcoustID refuses, or a config
    without one, comes back in the service's own words (`available: false`,
    `note`, `code`) with nothing read, fingerprinted or sent.
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
    """Run the configured import chain over each album folder, synchronously.

    This is the person's own press (the album page's tag-actions entry), so it
    does NOT queue: an album another job is already finishing (the import that put it
    there, a script run) answers this request at once with 409 and the claim's
    own sentence naming the holder, instead of parking the press for the length
    of that run and then running the very same chain again. Every AUTONOMOUS
    import path keeps queueing (``server.imports.finish_album``'s ``wait``) —
    an album handed over to be finished must not skip its chain, and a queued
    import now says what it is waiting for while it waits.

    A batch where only SOME albums are busy still runs the free ones: the
    refused ones come back as an album entry carrying the sentence in `errors`
    and `note`, exactly like a chain that could not start. 409 is for a request
    that started nothing at all.
    """
    from server import script_runners

    _require_manual()
    _cap(len(req.paths), MAX_PATHS, "paths")
    _guard(req.paths, req.staged)
    # One entry per path, in the order they were asked for: the wizard reads
    # `albums[i]` as the album it sent at `targets[i]` (that is how it adopts
    # the folder a chain's beets/organize step moved the album to), so a
    # refusal has to sit in its own path's slot rather than being appended.
    albums, refusals = [], []
    for p in req.paths:
        try:
            albums.append(imports.finish_album(p, force=req.force, wait=False))
        except script_runners.RunBusy as e:
            refusals.append(str(e))
            albums.append({"path": p, "scripts": [], "chain": [],
                           "errors": [str(e)], "chained": False,
                           "chain_off": False,
                           "note": f"the script chain could not start: {e}"})
    if refusals and len(refusals) == len(req.paths):
        # Nothing at all started: the whole request is the refusal, answered
        # the way `/api/run` answers a double-pressed run.
        raise HTTPException(409, refusals[0])
    return {"albums": albums}


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


# --------------------------------------------------------------------------- #
# The digital release's own answers: SOURCE, the unusable lyrics, the
# description. Both routes run the SAME functions `finish_album` does
# (`server.imports.settle_digital_import` / `stamp_album_source`), because a
# release imported by hand and one imported unattended must settle the same
# way — the only difference is WHO the SOURCE question is asked of.
# --------------------------------------------------------------------------- #
class SettleRequest(BaseModel):
    path: str
    # The chain this finish is about to run — the wizard's own ticked boxes.
    # It decides whether the lyrics half may remove an untimed lyric: with no
    # fetch coming there is nothing to replace it with, and the honest report
    # is the skip itself (see `settle_digital_lyrics`). None = the configured
    # chain.
    scripts: Optional[List[int]] = None
    source: str = ""            # the SOURCE value the user just answered
    # The release the wizard confirmed, when it has one: its own store URLs are
    # the one piece of evidence `stamp_album_source` may turn into a SOURCE.
    release: Optional[dict] = None
    metadata: bool = True       # run the import's own description step too
    staged: bool = False        # the wizard's album folder, wherever it put it


class SourceRequest(BaseModel):
    path: str
    # Empty = REPORT what the pipeline would write and what the config's own
    # default is (the suggestion the Match step pre-fills). Non-empty = write
    # THAT value, which is the user's answer to the question.
    value: str = ""
    release: Optional[dict] = None
    provider: str = ""
    staged: bool = False


@router.post("/api/import/settle")
def import_settle(req: SettleRequest):
    """Settle a digital release's SOURCE, unusable lyrics and description.

    What the wizard's Finish step calls before it runs the ticked scripts, so a
    release imported by hand ends in the state an unattended import leaves:
    SOURCE written when the release or the acquisition states it (else
    reported as still to be asked, the wizard's Match step), the untimed lyrics
    this install refuses removed and counted, and the album description fetched
    through the import's OWN metadata step (`run_metadata_step` — the same
    machinery the album page's fetch and every other import path use).

    Every half reports its own state; nothing here is silent, and nothing is
    invented: a SOURCE no evidence states comes back ``asked`` with the
    config's default for the wizard to offer.
    """
    from mlo.config import load_config

    _require_manual()
    _guard([req.path], req.staged)
    cfg = load_config()
    chain = None
    if req.scripts is not None:
        chain = [int(s) for s in req.scripts]
    return imports.settle_digital_import(
        req.path, cfg, chain=chain, value=req.source,
        release=req.release, metadata=req.metadata)


@router.post("/api/import/source")
def import_source(req: SourceRequest):
    """Report or write a Digital Media album's ``SOURCE``.

    ONE entry point for both halves, and the pipeline's own function
    (`imports.stamp_album_source`) — the row `mlo.import_policy` publishes
    beside the ``source`` family, so the button and the import cannot drift:

    * no *value* → the value the pipeline would write, derived from the
      release's own store URLs (or the provider), plus the config's default
      (`digital_media_source_value`) and why the tag is required — what the
      Match step pre-fills;
    * *value* given → written to every track of the album that lacks one (the
      write gate and the fill-only rule are the pipeline's own), and nothing is
      touched on a non-digital medium.
    """
    from mlo.config import load_config

    _require_manual()
    _guard([req.path], req.staged)
    value = str(req.value or "").strip()
    return imports.stamp_album_source(req.path, load_config(), value=value,
                                      release=req.release,
                                      provider=req.provider, dry=not value)
