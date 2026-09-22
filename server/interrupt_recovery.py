"""What an interrupted run leaves behind, and what this app does about it.

The bundled watchtower service replaces this container's image whenever a new
release appears, which means a SIGTERM to the server (and, when the stop grace
runs out, a SIGKILL) can land in the middle of a script, an import, a download
or a tag write. Two modules answer for that:

* :mod:`mlo.atomic` makes every individual WRITE atomic, so no single file is
  ever left torn (see the audit table in ``tools/test_interrupt_safety.py``);
* this module reconciles the WORK: on startup it finds what a killed run left
  on disk and either finishes the bookkeeping or deletes our own debris with a
  log line naming it — never the user's files. At shutdown it stops taking new
  work, waits a bounded time for what is running, says what it is waiting for
  and records what it had to abandon, so the next start (this module, again)
  can reconcile exactly those jobs.

What is NOT recoverable is documented rather than papered over: an external
tool that rewrites a file in place (``rsgain easy``, see
:func:`mlo.loudness._run_rsgain`) can still leave that one file unreadable if
the container is killed mid-write, because no arrangement of temp files can
make a third party's in-place writer atomic. Everything the app itself writes
goes through :mod:`mlo.atomic`.

One case has no journal at all, and it is the reason every write is atomic in
the first place: when Docker's stop grace expires, the SIGKILL leaves this
module no chance to record anything, so the next start has only the
FILESYSTEM to go on — leftover temp files, a staging folder, a half-filled
framework album. That is why the writes matter more than the bookkeeping: the
journal tells the operator what was interrupted, but the atomic write is what
makes the interruption harmless.
"""
import json
import os
import threading
import time
import traceback

from mlo import atomic
from mlo.paths import (app_data_dir, downloads_dir, incomplete_dir,
                       library_root, load_pending, trash_root)

# The app's own grace period at shutdown: how long it waits for running jobs
# before it gives up and records them as abandoned. It MUST stay below the
# container's stop grace (docker-compose.yml: stop_grace_period 3m) with room
# for the journal write, so Docker's SIGKILL never cuts the app's own ordered
# abort short — if it did, the difference between "abandoned and recorded" and
# "killed silently" would be lost.
GRACE_SECONDS = 120

# The journal a shutdown leaves when it could not finish what was running.
# Read and cleared by the next start.
JOURNAL_NAME = "interrupted_jobs.json"

_LOG_PREFIX = "[mlo] interrupted-run recovery:"

_shutting_down = threading.Event()
_shutdown_started_at = None
# Signal handlers this module replaced, by signal number (see
# install_signal_grace): the original is called after the flag is set.
_PREV_HANDLERS: dict = {}
_lock = threading.Lock()


def is_shutting_down() -> bool:
    """Whether this process is on its way out (new work must be refused)."""
    return _shutting_down.is_set()


def journal_path(cfg=None) -> str:
    """Path of the abandoned-work journal for this install."""
    folder = app_data_dir(_music_folder(cfg))
    return os.path.join(folder, JOURNAL_NAME) if folder else ""


def _music_folder(cfg=None) -> str:
    try:
        folder = (cfg or {}).get("music_folder")
        if not folder:
            from mlo.config import load_config
            folder = load_config().get("music_folder")
        return str(folder or "") or None
    except Exception:
        return None


def _say(log, text) -> None:
    """One line to the caller's log, or to stdout when it supplies none.

    The startup sweep and the shutdown both go through here: an app that
    recovers silently is indistinguishable from one that lost the work, so the
    default is to say it in the container log (``docker compose logs``).
    """
    try:
        (log or _default_log)(f"{_LOG_PREFIX} {text}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Startup: reconcile what a killed run left
# ---------------------------------------------------------------------------
def _sweep_roots(cfg=None) -> list:
    """(root, in_state_dir) pairs to sweep for our own leftover temp files.

    ``in_state_dir`` widens the rule to the suffix-only temps (``x.tmp``,
    ``x.part``) the app writes inside its OWN folders. Those are never applied
    to the library, where a file of the user's could legitimately end in
    ``.tmp``, and never to the download folders, which belong to slskd.
    """
    mf = _music_folder(cfg)
    out = []
    for getter, wide in ((app_data_dir, True), (trash_root, True),
                         (downloads_dir, False), (incomplete_dir, False),
                         (library_root, False)):
        try:
            path = getter(mf)
        except Exception:
            path = None
        if path:
            out.append((path, wide))
    return out


def _staging_report(cfg=None, log=None) -> list:
    """Folders waiting in the download area, and in-flight ones, reported.

    A completed transfer sits in ``.mlo/downloads`` until an import moves it
    in, and slskd keeps its unfinished transfers in ``.mlo/incomplete`` — a
    kill can land with either present. Both are REPORTED, never touched: the
    first is an album a queued import still has to pick up, the second is a
    download that resumes where it stopped (deleting it would throw away
    everything already downloaded).
    """
    out = []
    for label, folder in (("staging", downloads_dir(_music_folder(cfg))),
                          ("incomplete download",
                           incomplete_dir(_music_folder(cfg)))):
        if not folder or not os.path.isdir(folder):
            continue
        try:
            names = [n for n in sorted(os.listdir(folder))
                     if not n.startswith(".")]
        except OSError:
            continue
        if not names:
            continue
        out.append({"kind": label, "folder": folder, "items": names})
        shown = ", ".join(names[:3]) + (f" (+{len(names) - 3} more)"
                                        if len(names) > 3 else "")
        _say(log, f"{len(names)} {label} item(s) in {folder}: {shown}"
                  + (" — left as they are: an import still owns these"
                     if label == "staging" else
                     " — left as they are: slskd resumes them"))
    return out


def _pending_report(cfg=None, log=None) -> list:
    """Framework albums left in an impossible state by a kill.

    Two cases, both read through the modules that own the state:

    * a marker whose folder HOLDS AUDIO — the download landed and the process
      died before the marker was cleared, so the library would list a
      fully-populated album as PENDING forever. Cleared through the owner's
      own :func:`server.pending_albums.clear_if_filled` (placeholder cover
      and all); the log says the script chain did not run, because that is
      the one thing the sweep cannot know and the user may still want;
    * a marker whose WISH is gone — nothing will ever fill that folder, so it
      is logged as an orphan for the user to cancel. It is NOT deleted: this
      app does not remove an album folder from the library at startup on its
      own.
    """
    from server import pending_albums

    out = []
    root = library_root(_music_folder(cfg))
    if not root:
        return out
    for folder in pending_albums._scan_pending(root):
        info = load_pending(folder) or {}
        name = os.path.basename(folder)
        try:
            has_audio = bool(pending_albums._audio_files(folder))
        except Exception:
            has_audio = False
        if has_audio:
            cleared = False
            try:
                cleared = bool(pending_albums.clear_if_filled(
                    folder, cfg, chained=True))
            except Exception:
                traceback.print_exc()
            if cleared:
                out.append({"kind": "pending_filled", "folder": folder})
                _say(log, f"pending album {name!r} holds audio: marker cleared"
                          " — its script chain did not finish, so finish the"
                          " album from its page")
            continue
        wid = info.get("wish_id")
        if wid is None:
            continue
        if _wish_exists(wid):
            continue
        out.append({"kind": "orphan_pending", "folder": folder, "wish_id": wid})
        _say(log, f"framework album {name!r} waits on wish {wid}, which is"
                  " gone — nothing is searching for it; cancel it from the"
                  " queue to remove the folder")
    return out


def _wish_exists(wish_id) -> bool:
    from server import wishes

    try:
        return bool(wishes.get_wish(int(wish_id)))
    except Exception:
        # An unreadable wishes.db must not turn every framework album into an
        # orphan: report nothing rather than something wrong.
        return True


def _read_journal(cfg=None, log=None) -> list:
    """The abandoned jobs a previous shutdown recorded, then clear the file."""
    path = journal_path(cfg)
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        _say(log, "abandoned-job journal was unreadable; removing it")
        rows = []
    else:
        rows = data.get("jobs") if isinstance(data, dict) else None
        rows = rows if isinstance(rows, list) else []
    try:
        os.remove(path)
    except OSError:
        pass
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = row.get("kind") or "job"
        label = row.get("label") or row.get("job") or ""
        held = [str(p) for p in (row.get("paths") or [])]
        what = f"{kind} {label}".strip()
        if held:
            named = ", ".join(os.path.basename(p.rstrip("\\/")) or p
                              for p in held[:3])
            more = f" (+{len(held) - 3} more)" if len(held) > 3 else ""
            _say(log, f"an update interrupted {what} — it was working on"
                      f" {named}{more}; its files were left as they were and"
                      " its own temp files were swept")
        else:
            _say(log, f"an update interrupted {what} — its files were left as"
                      " they were and its own temp files were swept")
    return rows


def startup_recovery(cfg=None, log=None) -> dict:
    """Reconcile everything a killed run left; one log line per action.

    Called once from the app's startup (``server.main._lifespan``). Safe to
    call on a clean start: with nothing left behind it logs one line and does
    nothing else.
    """
    _say(log, "checking for files and jobs an interrupted run left behind")
    summary = {"temp_files": [], "staging": [], "pending": [], "jobs": []}
    for root, wide in _sweep_roots(cfg):
        if not os.path.isdir(root):
            continue
        removed = atomic.sweep_temps(
            root, in_state_dir=wide,
            log=lambda p, _r=root: _say(
                log, f"deleted leftover temp file {_short(p, _r)}"), limit=5000)
        if removed:
            summary["temp_files"].extend(removed)
    summary["staging"] = _staging_report(cfg, log)
    summary["jobs"] = _read_journal(cfg, log)
    try:
        summary["pending"] = _pending_report(cfg, log)
    except Exception:
        traceback.print_exc()
    left = (len(summary["temp_files"]) + len(summary["staging"])
            + len(summary["pending"]) + len(summary["jobs"]))
    if left:
        _say(log, f"recovery done: {len(summary['temp_files'])} temp file(s)"
                  f" deleted, {len(summary['staging'])} download-area item(s)"
                  f" reported, {len(summary['pending'])} framework album(s)"
                  f" reconciled, {len(summary['jobs'])} interrupted job(s)"
                  " reported")
    else:
        _say(log, "nothing to recover: no leftover temp files, downloads,"
                  " pending markers or interrupted jobs")
    return summary


def _short(path, root) -> str:
    """*path* relative to *root*, for a log line that names the file."""
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


# ---------------------------------------------------------------------------
# Shutdown: stop taking work, wait honestly, record what is abandoned
# ---------------------------------------------------------------------------
def running_jobs() -> list:
    """Every in-flight job, from the one registry that holds them.

    ``server.job_locks`` is where a script run, an import (single or queued)
    and every mutating route record what they hold, and ``server.import_queue``
    reports its own row for the queue view — the two sources
    ``GET /api/jobs/locks`` already merges.
    """
    jobs = []
    try:
        from server import job_locks
        for rec in job_locks.jobs():
            jobs.append({
                "kind": rec.get("kind") or "job",
                "job": rec.get("job") or rec.get("id") or "",
                "label": rec.get("label") or "",
                "started_at": rec.get("started_at"),
                "paths": [str(p) for p in (rec.get("paths") or [])],
                "status": "running",
            })
    except Exception:
        traceback.print_exc()
    try:
        from server import import_queue
        state = import_queue.status() or {}
        if (state.get("state") or state.get("status")) == "running":
            current = str(state.get("current") or "")
            jobs.append({
                "kind": "import_queue",
                "job": "import-queue",
                "label": f"import queue ({state.get('done', 0)}/"
                         f"{state.get('total', 0)})",
                "started_at": state.get("started_at"),
                "paths": [current] if current else [],
                "status": "running",
            })
    except Exception:
        pass
    return jobs


def begin_shutdown(grace=GRACE_SECONDS) -> None:
    """Mark the process as shutting down: no new run may start now."""
    global _shutdown_started_at
    with _lock:
        if _shutting_down.is_set():
            return
        _shutdown_started_at = time.time()
        _shutting_down.set()
    _say(_default_log, f"shutting down: waiting up to {int(grace)}s for work"
                       " in progress")


def wait_for_jobs(grace=GRACE_SECONDS, *, log=None, poll=0.5) -> list:
    """Wait up to *grace* seconds for the running jobs; returns what is left.

    Says what it is waiting FOR (the job, its label and how long it has been
    running) instead of blocking silently — an operator watching
    ``docker compose logs -f`` sees exactly why the container has not stopped
    yet. The wait is what gives a script the time to finish its current album
    or to abort cleanly; the compose stop grace is set above it (see
    docker-compose.yml) so this ordered abort always happens before Docker's
    SIGKILL.
    """
    walk = log if log is not None else _default_log
    deadline = time.time() + max(0, float(grace))
    jobs = running_jobs()
    said = set()
    while jobs and time.time() < deadline:
        for job in jobs:
            key = (job["kind"], job["job"])
            if key in said:
                continue
            said.add(key)
            held = len(job["paths"])
            _say(walk, f"waiting for {job['kind']} {job['job'] or ''}"
                       f"{' (' + job['label'] + ')' if job['label'] else ''}"
                       f"{f' holding {held} path(s)' if held else ''}")
        time.sleep(max(0.05, float(poll)))
        jobs = running_jobs()
    return jobs


def install_signal_grace() -> bool:
    """Set the shutdown flag the moment SIGTERM/SIGINT arrives.

    Uvicorn's own handler waits for the run's background task BEFORE the
    lifespan teardown, and `begin_shutdown` lives in that teardown — so a chain
    that ran long enough held the container open on its own: `docker stop` was
    measured waiting 96 s for one, and a chain longer than the compose stop
    grace would meet SIGKILL with the app's ordered abort never having run at
    all. This handler sets the flag on the signal itself (a running chain then
    stops at the next SCRIPT boundary — never mid-write) and hands the signal
    straight over to the handler that was already installed, so uvicorn's
    graceful shutdown is unchanged.

    Only the main thread may install signal handlers, so a test client or an
    embedding host — which runs the app on a worker thread — is left alone and
    this returns False.
    """
    import signal as _signal

    if threading.current_thread() is not threading.main_thread():
        return False
    installed = False
    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(_signal, name, None)
        if sig is None or sig in _PREV_HANDLERS:
            continue
        try:
            _PREV_HANDLERS[sig] = _signal.getsignal(sig)

            def _handler(signum, _frame, _sig=sig):
                begin_shutdown()
                previous = _PREV_HANDLERS.get(_sig)
                if callable(previous):
                    return previous(signum, _frame)
                # Nothing to hand over to: behave like the default would.
                _signal.signal(_sig, _signal.SIG_DFL)

            _signal.signal(sig, _handler)
            installed = True
        except Exception:
            continue
    return installed


def _write_journal(jobs, *, log=None) -> str:
    """Record the abandoned jobs so the next start can reconcile them."""
    path = journal_path()
    if not path or not jobs:
        return ""
    payload = {"version": 1, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "reason": "container stopped while jobs were running "
                         "(auto-update)",
               "jobs": jobs}
    try:
        atomic.write_bytes(path, json.dumps(payload, indent=1).encode("utf-8"))
    except OSError:
        traceback.print_exc()
        return ""
    _say(log if log is not None else _default_log,
         f"recorded {len(jobs)} abandoned job(s) in {path}: the next start"
         " reports them")
    return path


def shutdown(grace=GRACE_SECONDS, *, log=None) -> dict:
    """The app's whole shutdown conversation: begin, wait, mark, report.

    One call, from the lifespan's own teardown (``server.main._lifespan``),
    after the background workers have been stopped: no new run may start
    (:func:`begin_shutdown`), running ones get up to *grace* seconds
    (:func:`wait_for_jobs`), and whatever is still running is written to the
    journal (:func:`_write_journal`) so the next start's
    :func:`startup_recovery` can name it.
    """
    walk = log if log is not None else _default_log
    began = time.time()
    begin_shutdown(grace)
    left = wait_for_jobs(grace, log=walk)
    written = _write_journal(left, log=walk)
    if left:
        for job in left:
            _say(walk, f"gave up on {job['kind']} {job['job'] or ''} after"
                       f" {int(time.time() - began)}s — its files are left as"
                       " they are (every write is atomic) and the next start"
                       " reconciles it")
    else:
        _say(walk, f"all work finished within {int(time.time() - began)}s")
    return {"waited": time.time() - began, "abandoned": left,
            "journal": written}


def _default_log(text) -> None:
    try:
        print(text, flush=True)
    except Exception:
        pass
