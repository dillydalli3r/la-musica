"""The Run scripts surface: POST /api/run and the bookkeeping behind it.

Scripts mutate the library in place, so this one route is the app's gate on
"start a run": the id -> runner registry and the busy lock live in
``server.script_runners`` (the import pipeline and the bulk queue run the same
scripts), and the frames the UI draws its bar from are relayed over
``/ws/progress`` (``server.ws``).

Its own cache invalidation lives here too: a finished run must drop the
library/album caches the changed tags fed, and announce the new state to every
open page.
"""
import os
import traceback
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server import events as events_mod
from server import mbresolve, tagcache
from server.api_common import _in_music_folder, load_config

router = APIRouter(tags=["run"])


class RunRequest(BaseModel):
    ids: List[int]
    targets: Optional[List[str]] = None
    force: Optional[dict] = None


# --------------------------------------------------------------------------- #
# Run scripts
# --------------------------------------------------------------------------- #
# Scripts mutate the library in place, so two overlapping runs (double-clicked
# Run, or an import-triggered chain landing on a UI run) would fight over the
# same files. The lock itself lives in server.script_runners, because the
# import pipeline and the bulk queue run the same scripts; a second caller
# gets 409 instead of queueing.
@router.post("/api/run")
def run_scripts(req: RunRequest):
    from server import script_runners
    try:
        return _run_scripts(req)
    except script_runners.RunBusy as e:
        raise HTTPException(409, str(e))


def _run_scripts(req: RunRequest):
    """Run the requested scripts (ids in request order) against the library.

    The id → runner registry lives in `server.script_runners` so the import
    pipeline, the bulk queue and the Soulseek auto-importer run the exact same
    scripts; this handler is the HTTP shell around it (target scoping, force
    semantics, cache invalidation, progress).
    """
    from server import script_runners

    cfg = load_config()
    if req.targets:
        targets = [os.path.normpath(t) for t in req.targets]
        folder = cfg.get("music_folder") or ""
        if folder and os.path.isdir(folder):
            for t in targets:
                if not _in_music_folder(t, folder):
                    raise HTTPException(400, f"target outside music folder: {t}")
        cfg["targets"] = targets
    # Per-script options default from saved config; the request can override.
    # A *supplied* force dict is authoritative and complete (see
    # script_runners._apply_force): the UI's one-shot Force switch sends every
    # checked key, so an unchecked key must turn the force off rather than
    # fall back to a saved-on config value.
    f = req.force or {}
    if req.force is not None:
        # image-option overrides (subset of run_process_images knobs)
        for key in ("rename_to_cover", "reencode_to_jxl", "images_convert_to_jpeg",
                    "images_convert_lossless_to_png", "convert_jxl_back", "remove_alpha",
                    "jpeg_progressive", "cover_resize_enabled", "cover_crop_enabled",
                    "cover_target_size", "cover_jpeg_quality", "jpegxl_effort",
                    "jpegxl_distance", "png_optimization_level"):
            if key in f:
                cfg[key] = f[key]

    for i in req.ids:
        if script_runners.RUNNERS.get(i, (None, None))[1] is None:
            raise HTTPException(400, f"runner {i} not available")

    results = script_runners.run_chain(
        cfg, list(req.ids), targets=None, force=req.force if req.force is not None else None,
    )
    _invalidate_run(cfg, results)
    _announce_run(req.ids, results, cfg.get("targets"))
    return {"results": results}


def _invalidate_run(cfg, results):
    """Drop the caches this run made stale — and nothing else.

    Every run used to end with ``tagcache.invalidate_all()`` +
    ``mbresolve.invalidate()``, so the library page right after a ONE-ALBUM run
    (a graded album, an import's chain, a press on one folder) re-parsed every
    track in the library with mutagen. A run that named its targets touched
    those folders — plus the folders a script MOVED an album into, which the
    runner reports as ``stats["moved_targets"]`` — and
    :func:`server.tagcache.invalidate_album` is exactly the scoped drop for
    them (it clears the tag/art entries under those folders and the one
    assembled ``/api/library`` payload; ``server.imports`` invalidates the same
    way after writing an album). Only a run with no targets at all — Run All, a
    library-wide sweep — really did touch everything, and keeps the
    library-wide drop.

    mbresolve's index is dropped either way, on purpose: it is ONE timestamp
    over the whole library and its invalidation is O(1) (the index rebuilds
    lazily), while a scoped run that renamed a folder must not keep resolving
    the old paths to it.
    """
    paths = [str(p) for p in (cfg.get("targets") or []) if str(p).strip()]
    extra = []
    for r in results or []:
        for d in ((r.get("stats") or {}).get("moved_targets") or ()):
            if str(d).strip():
                extra.append(str(d))
    mbresolve.invalidate()
    if not paths:
        # No targets at all: the run swept the library, so everything it holds
        # may have changed — the moved folders it reported are a subset of that.
        tagcache.invalidate_all()
        return
    tagcache.invalidate_album(*(paths + extra))


def _announce_run(ids, results, targets=None):
    """One notification per finished script run (see server/events.py).

    A UI run is the user's own long job — grading a big library takes minutes,
    and the request that carries it may outlive their attention — so its
    OUTCOME is announced once, on the bus, instead of only in the response.
    Exactly one frame per run: the failed kind when anything errored, the
    grader's own kind for a pure grade run, the plain kind otherwise. A run
    where every script was skipped has no outcome to report (nothing ran), so
    it says nothing.

    A skipped step is not a failure: `run_script` reports it as `skipped`,
    which is the "this feature is switched off" answer, not an error.
    """
    try:
        from server import script_runners
        failed = [r for r in results if r.get("error")]
        ran = [r for r in results if not r.get("error") and not r.get("skipped")]
        if not failed and not ran:
            return
        names = [str(r.get("label") or f"Script {r.get('id')}") for r in results]
        listing = ", ".join(names[:4]) + (f" +{len(names) - 4}" if len(names) > 4 else "")
        # Where the run's subject is: a single album the user graded is that
        # album's page, everything else is the library-wide view.
        targets = [str(t) for t in (targets or []) if str(t).strip()]
        subject = f"/album/{quote(targets[0], safe='')}" if len(targets) == 1 else "/library"
        if failed:
            first = failed[0]
            events_mod.emit(
                "script_failed",
                f"{first.get('label') or 'Script'} failed",
                "; ".join(str(r.get("error"))[:200] for r in failed[:2]),
                {"link": "/in-progress", "ids": list(ids),
                 "failed": [r.get("id") for r in failed]},
            )
        elif list(ids) == [4]:
            dist = (ran[0].get("stats") or {}).get("grade_dist") or {}
            events_mod.emit(
                "grade_done",
                "Grade finished",
                (f"{dist.get('PASS', 0)} passed, {dist.get('FAIL', 0)} failed"
                 if dist else "The library has been graded"),
                {"link": subject, "ids": list(ids), "grade_dist": dist,
                 "album_path": targets[0].replace("\\", "/") if len(targets) == 1 else ""},
            )
        else:
            events_mod.emit(
                "script_done",
                (f"{names[0]} finished" if len(names) == 1
                 else f"{len(ran)} scripts finished"),
                listing,
                {"link": "/in-progress", "ids": list(ids),
                 "ran": [r.get("id") for r in ran]},
            )
    except Exception:
        # A notification must never turn a finished run into a failed request.
        traceback.print_exc()
