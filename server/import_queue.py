"""Sequential "import everything that finished downloading" runner.

The Downloads / Soulseek pages can have several albums sitting in the download
dir at once, each needing the SAME per-album treatment (convert lossless →
write MEDIA → stamp the MusicBrainz identity → organize → run the configured
import chain). Importing them as one batch is what this deliberately is NOT:
the chain of scripts is long, a failure in the middle of album 3 must not
leave albums 4..N half-done, and the user asking for "import all" wants each
album taken all the way through, one after another, exactly as if they had
pressed Import on each row in turn.

So: a small job object holding an ordered worklist, one worker thread, and a
status dict the UI polls (or hears about through `server.events`). The runner
owns *sequencing* only — what "import one album" means is `server.main`'s
`_import_one_album`, injected through `set_importer()` at startup. That
inversion keeps this module importable from the routes without importing
`server.main` (which imports these).

`honey: one job at a time, process-wide. Two concurrent "import all" runs
would race over the same folders — one clearing a pending marker the other is
still working on — and the second call answers "already running" with the live
status instead. That is a decision about SEQUENCING, not about the machine:
this runner takes each album all the way through before it starts the next, so
a failure in album 3 leaves 4..N untouched and reported. It costs nobody else
anything: a script run serializes on its own TARGET paths
(server.job_locks; see R94 in docs/OPTIMIZATION-GRADING-SPEC.md), so an
import-all job neither blocks nor is blocked by someone working on a different
album — it holds the album it is on while it works on it, which is the claim a
delete, a move or a tag write is refused against.

The run also registers itself in :mod:`server.job_locks`: one job for the whole
run, holding the album it is on while it works on it, so a delete, a move or a
tag write aimed at that album is refused (409) rather than racing the import —
and MAINTAIN → In progress can show the run and its progress."""
import os
import threading
import time
import traceback
from urllib.parse import quote

from server import imports

_lock = threading.RLock()
_stop = threading.Event()
_thread = None

_job = {
    "state": "idle",     # idle | running | done | error | cancelled
    "total": 0,
    "done": 0,
    "current": None,     # album path being processed
    "results": [],       # [{path, ok, error, album_root, chained, chain_off,
                         #   chain, scripts, note}] — see _run
    "errors": [],
    "started_at": 0.0,
    "finished_at": 0.0,
}

# Injected by server.main: `fn(path) -> dict` performs the whole per-album
# import (see main._import_one_album) and must never raise.
_importer = None
# Injected by server.main: `fn() -> [paths]` lists the albums waiting in the
# download dir (see server.soulseek.ready_albums).
_ready_provider = None


def set_importer(fn):
    global _importer
    _importer = fn


def set_ready_provider(fn):
    global _ready_provider
    _ready_provider = fn


def status():
    with _lock:
        st = dict(_job)
        st["results"] = [dict(r) for r in _job["results"]]
        st["errors"] = list(_job["errors"])
        return st


def running():
    with _lock:
        return _job["state"] == "running"


def cancel():
    """Ask the runner to stop after the album it is on. Never kills the
    import in progress: a half-imported album is worse than a slow one."""
    if not running():
        return False
    _stop.set()
    return True


def start(paths=None, on_done=None):
    """Import `paths` (default: everything ready in the download dir), one
    album at a time, in the background. Returns the initial status.

    `on_done(results)` runs once the run stops (finished, failed or
    cancelled) with the per-album results — how the wishes route learns that
    the album it imported really landed. It runs on the runner's thread and
    must not raise (a raising callback is reported, never fatal)."""
    global _thread
    with _lock:
        if _job["state"] == "running":
            return {"ok": False, "error": "an import run is already in progress",
                    "status": status()}
        if _importer is None:
            return {"ok": False, "error": "the importer is not wired up yet"}
        if paths is None:
            if _ready_provider is None:
                return {"ok": False, "error": "no ready-album provider"}
            try:
                paths = list(_ready_provider() or [])
            except Exception as e:
                return {"ok": False, "error": f"could not list downloads: {e}"}
        work = [str(p) for p in (paths or []) if str(p).strip()]
        if not work:
            return {"ok": False, "error": "nothing to import — no completed "
                                          "album is waiting in the download dir"}
        _stop.clear()
        _job.update(state="running", total=len(work), done=0, current=None,
                    results=[], errors=[], started_at=time.time(),
                    finished_at=0.0)
        _thread = threading.Thread(target=_run, args=(work, on_done),
                                   name="mlo-import-all", daemon=True)
        _thread.start()
        return {"ok": True, "status": status()}


def _chain_of(res):
    """The ``finish_album`` result inside an importer's own result, or ``{}``.

    ``server.main._import_one_album`` puts it under ``"chain"`` — ``None`` when
    the chain was handed to a background thread (the one-click route), in which
    case there is no outcome to report yet and the row says exactly that
    instead of claiming one.
    """
    chain = (res or {}).get("chain")
    return chain if isinstance(chain, dict) else {}


def _run_note(entries):
    """What the run's albums' chains did, in one line for the notification.

    The album's own line when the run was a single album (that is the case a
    notification links to an album page), a tally otherwise."""
    if not entries:
        return ""
    if len(entries) == 1:
        return str(entries[0].get("note") or "")
    chained = sum(1 for e in entries if e.get("chained"))
    off = sum(1 for e in entries if e.get("chain_off") and not e.get("chained"))
    note = f"{chained} of {len(entries)} albums ran the configured script chain"
    return note + (f"; {off} have no chain configured" if off else "")


def _run(work, on_done=None):
    """The worker: one album fully through the import, then the next."""
    from server import events
    from server import job_locks
    imported = 0
    # This run's own results, kept locally: reading them back off the shared
    # job would race a new run that has already reset it (the callback can land
    # after `state != running` is published), and the wishes route acts on
    # them — marking a wish imported with another album's path is not a race
    # worth tolerating.
    mine = []
    # One job for the whole run, so the in-progress list shows one row for it
    # (with the album it is on); each album is held only while it is being
    # worked on, so the rest of the library stays editable in between.
    with job_locks.holding((), kind="import",
                           label="Import all downloads") as job:
        for index, path in enumerate(work, 1):
            if _stop.is_set():
                break
            name = os.path.basename(path.rstrip("\\/")) or path
            with _lock:
                _job["current"] = path
            try:
                with job_locks.holding([path], job, wait=True):
                    job_locks.publish(index - 1, len(work),
                                      f"importing {name}", job=job)
                    res = _importer(path) or {}
                chain = _chain_of(res)
                ok = not res.get("errors")
                error = "; ".join(str(x) for x in (res.get("errors") or []))
                if ok and chain and not chain.get("scripts") \
                        and not chain.get("chain_off") \
                        and not res.get("chain_started"):
                    # The album landed but its configured chain never ran (the
                    # library lock timed out, a crash): the same rule the bulk
                    # queue applies — "imported" is only honest when the chain
                    # ran, or when this library configures none.
                    ok = False
                    error = ("imported, but the script chain did not run: "
                             + (imports.chain_summary(chain) or "no reason reported"))
                job_locks.publish(index, len(work), name, job=job)
            except Exception as e:
                traceback.print_exc()
                res = {"path": path, "errors": [str(e)]}
                chain = {}
                ok = False
                error = str(e)
            entry = {
                "path": path,
                "ok": bool(ok),
                "album_root": res.get("album_root") or path,
                "error": error,
                # What the chain did with this album, on the row: which scripts
                # ran, what failed, or that there was nothing to run. The queue
                # view reads these; so does anything reporting the run.
                "chained": bool(chain.get("chained")),
                "chain_off": bool(chain.get("chain_off")),
                "chain": list(chain.get("chain") or []),
                "scripts": [{"id": s.get("id"), "label": s.get("label"),
                             "error": str(s.get("error") or "")}
                            for s in (chain.get("scripts") or [])
                            if isinstance(s, dict)],
                "note": imports.chain_summary(chain) if chain else (
                    "the script chain is running in the background"
                    if res.get("chain_started") else ""),
            }
            mine.append(entry)
            with _lock:
                _job["results"].append(dict(entry))
                _job["done"] = len(_job["results"])
                if not ok:
                    _job["errors"].append(f"{path}: {'; '.join(str(x) for x in (res.get('errors') or []))}")
            if ok:
                imported += 1
    with _lock:
        cancelled = _stop.is_set() and _job["done"] < _job["total"]
        _job["state"] = "cancelled" if cancelled else ("error" if _job["errors"] else "done")
        _job["current"] = None
        _job["finished_at"] = time.time()
        total = _job["total"]
        failed = len(_job["errors"])
    if on_done is not None:
        try:
            on_done([dict(r) for r in mine])
        except Exception:
            # A callback is bookkeeping (marking a wish imported); it must
            # never turn a finished run into a failed one.
            traceback.print_exc()
    try:
        if imported:
            # The subject of "imported 3 albums": the album itself when the run
            # was exactly one (its own page), the library otherwise. Quoted the
            # way the UI quotes a path into a route, so the tray can navigate
            # with it verbatim.
            only = str(mine[0].get("album_root") or "") if len(mine) == 1 else ""
            note = _run_note(mine)
            events.emit(
                "download_done",
                f"Imported {imported} album" + ("s" if imported != 1 else ""),
                (f"{failed} failed" if failed else "all done")
                + f" — {imported} of {total} finished downloading and imported"
                + (f". {note[0].upper()}{note[1:]}." if note else ""),
                {"link": f"/album/{quote(only, safe='')}" if only else "/library",
                 "album_path": only.replace("\\", "/"),
                 "imported": imported, "failed": failed, "note": note},
            )
        elif failed:
            # Nothing was imported: the downloads are still waiting, so the
            # wizard (which lists them) is the page to send the user to.
            events.emit("download_done", "Import finished with errors",
                        f"{failed} of {total} album(s) could not be imported",
                        {"link": "/import",
                         "errors": [str(e)[:300] for e in (_job["errors"] or [])[:3]]})
    except Exception:
        pass
