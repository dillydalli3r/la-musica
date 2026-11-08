"""Background worker that evaluates the artist watches.

The wish worker's own shape, one level up: a daemon thread, a stop event, a
tick, and a cycle that refuses to overlap itself. Each tick asks
``server.artist_watch`` which watches are due — a watch is evaluated at most
once per ``artist_watch_interval_hours`` (24 by default), so a short tick keeps
"a watch added a minute ago" from waiting a whole interval while the network
still sees one browse per artist per interval.

What keeps a cycle from queueing a release that is already being worked on is
the WISH QUEUE, not the folder-lock registry (``server/job_locks``): the
registry arbitrates concurrent writes to the files of an album that is ALREADY
in the library, whereas what a watcher must not do is queue a SECOND download
of a release. That record lives in the wish queue — a release mid-import still
has its wish row, a queued-but-unfilled release has its framework marker, and a
filled one is in ``wishes.owned_mbids`` — and ``artist_watch`` checks all three
before anything is created (see ``queued_release_groups``).

Nothing in here searches or downloads: a matched release becomes a framework
album and a wish through ``pending_albums.create``, and the existing wish
worker does the rest.
"""
import threading
import time
import traceback

from mlo.config import load_config

from server import artist_watch

_lock = threading.Lock()

# The whole loop's controls. `_stop` is the shutdown latch (server.main's
# lifespan sets it), `_running_cycle` refuses a second pass over the watches
# (two at once would evaluate the same artist twice and could queue the same
# release twice).
_stop = threading.Event()
_worker = None
_running_cycle = False

_state = {"running": False, "cycles": 0, "last_cycle": 0.0, "next_run": 0.0,
          "last_result": "", "current": []}

# How often the loop asks which watches are due. Each watch still honours
# `artist_watch_interval_hours` between its own checks.
_TICK_S = 300
# Let the app finish booting (slskd, the config migration, the first scan)
# before the first MusicBrainz request goes out.
_SETTLE_S = 20.0


def status():
    with _lock:
        st = dict(_state)
    st["enabled"] = bool(load_config().get("artist_watch_enabled", True))
    st["interval_hours"] = int(load_config().get("artist_watch_interval_hours", 24) or 24)
    st["watches"] = len(artist_watch.list_watches())
    return st


def _set(**fields):
    with _lock:
        _state.update(fields)


def _stopped():
    return _stop.is_set()


def run_cycle(watch_id=None, force=False):
    """One pass over the due watches. `watch_id` targets a single watch.

    Safe to call from any thread; two cycles at once are refused (they would
    evaluate the same artist twice). Returns the pass's report, or a refusal
    while another cycle is running.
    """
    global _running_cycle
    with _lock:
        if _running_cycle:
            return {"ok": False, "error": "a watch cycle is already running"}
        _running_cycle = True
    _set(running=True, current=[])
    try:
        cfg = load_config()
        if not cfg.get("artist_watch_enabled", True):
            return {"ok": True, "watches": 0, "checked": 0, "queued": [],
                    "notified": [], "errors": [], "summary": "watches are off"}
        watches = [artist_watch.get_watch(watch_id)] if watch_id is not None \
            else artist_watch.list_watches()
        names = [f"{w.get('name') or w.get('artist_mbid')}" for w in watches if w]
        _set(current=names)
        out = artist_watch.run_cycle(watch_id, cfg, force=force)
        interval = int(cfg.get("artist_watch_interval_hours", 24) or 24) * 3600
        with _lock:
            _state["cycles"] = int(_state.get("cycles") or 0) + 1
        _set(last_cycle=time.time(), next_run=time.time() + interval,
             last_result=str(out.get("summary") or "")[:200])
        return out
    except Exception as e:
        traceback.print_exc()
        _set(last_result=f"cycle failed: {e}"[:200])
        return {"ok": False, "error": str(e), "checked": 0, "queued": [],
                "notified": [], "errors": [str(e)], "summary": str(e)}
    finally:
        _set(running=False, current=[])
        with _lock:
            _running_cycle = False


def _loop():
    """Check the due watches on a tick, until stopped."""
    time.sleep(_SETTLE_S)
    while not _stop.is_set():
        try:
            if load_config().get("artist_watch_enabled", True):
                run_cycle()
            else:
                _set(last_result="watches are off")
        except Exception:
            traceback.print_exc()
        for _ in range(max(1, _TICK_S // 5)):
            if _stop.is_set():
                return
            time.sleep(5.0)


def start():
    """Start the worker thread (idempotent)."""
    global _worker
    with _lock:
        if _worker and _worker.is_alive():
            return False
        _stop.clear()
        _worker = threading.Thread(target=_loop, name="mlo-artist-watch", daemon=True)
        _worker.start()
    return True


def stop():
    """Ask the worker to end (the loop notices within a few seconds)."""
    _stop.set()


def trigger(watch_id=None):
    """Kick a cycle now in the background (the UI's "check now")."""
    with _lock:
        if _running_cycle:
            return {"ok": False, "error": "a watch cycle is already running"}
    threading.Thread(target=run_cycle, kwargs=dict(watch_id=watch_id, force=True),
                     name="mlo-watch-manual", daemon=True).start()
    return {"ok": True}
