"""In-flight jobs and the library paths each one holds.

Mounted by ``server.main`` (``include_router``). One route:

    GET /api/jobs/locks
        -> {jobs: [{job, kind, label, started_at, elapsed, paths,
                    progress: {done, total, text} | null}]}

The sidebar page under MAINTAIN ("In progress") polls it: which job is running,
what it is doing, which folders it holds and how far it has got. The registry
behind it is :mod:`server.job_locks` — script runs
(:mod:`server.script_runners`), the download import queue
(:mod:`server.import_queue`), single-album imports and every mutating route
claim their paths there, so this is the one place that answers what the library
is busy with right now.

Nothing here offers a "force release": a claim belongs to the work holding it,
and dropping it early would only mean the files are unprotected while the job
is still writing to them. A job that has no reason to keep running is stopped
through its own surface (the import queue's cancel, the shim in front of a
script run) — this list only reports.
"""
from fastapi import APIRouter

from server import job_locks

router = APIRouter(tags=["jobs"])


@router.get("/api/jobs/locks")
def job_lock_list():
    """Every in-flight job and the paths it holds, oldest first.

    ``elapsed`` is computed by the server so every client reads the same figure
    off one clock. A job that has reported progress (a chain step, an import's
    current album) carries ``progress``; one that has not — a tag write, an
    organize — carries null, and the page shows it without a bar rather than
    inventing 0%.
    """
    return {"jobs": job_locks.jobs()}
