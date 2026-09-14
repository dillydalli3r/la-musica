"""Background worker that fills the Soulseek wishlist.

Runs a single serialized cycle: every open wish is re-searched through the
normal auto-import pipeline (server/soulseek_auto.py), one at a time, and the
wish is marked ``imported`` when the release lands — or left ``wanted`` for the
next interval. After each cycle the library is reconciled so wishes filled
out-of-band (a manual download) are resolved too.

Started from the FastAPI lifespan; the interval and master switch live in the
config (``wishes_enabled`` / ``wishes_interval_hours``).
"""
import threading
import time
import traceback

from mlo.config import load_config
from server import wishes

_lock = threading.Lock()
_stop = threading.Event()
_worker = None
_running_cycle = False

_state = {
    "running": False,
    "current": None,      # wish title being searched
    "cycle_started": 0.0,
    "last_cycle": 0.0,
    "last_result": "",    # human summary of the last cycle
    "next_run": 0.0,
}

# One wish can legitimately download for a while; cap so the worker stays
# responsive to cancellation / config changes between wishes.
_WISH_TIMEOUT_S = 45 * 60


def status():
    with _lock:
        st = dict(_state)
    st["interval_hours"] = int(load_config().get("wishes_interval_hours", 6) or 6)
    st["enabled"] = bool(load_config().get("wishes_enabled", True))
    return st


def _set(**fields):
    with _lock:
        _state.update(fields)


def _job_running():
    try:
        from server import soulseek_auto
        return soulseek_auto.job_state().get("state") == "running"
    except Exception:
        return False


def _wait_job(cancel_check, timeout_s=_WISH_TIMEOUT_S):
    """Block until the current auto-import job finishes; returns its state."""
    from server import soulseek_auto
    deadline = time.time() + timeout_s
    time.sleep(1.0)  # let the job transition to running first
    while time.time() < deadline:
        st = soulseek_auto.job_state()
        if st.get("state") != "running":
            return st
        if cancel_check():
            return st
        time.sleep(2.0)
    return soulseek_auto.job_state()


def _run_one(wish, cfg):
    """Search + fill one wish. Returns 'imported' | 'pending' | 'skipped'."""
    from server import soulseek
    from server import soulseek_auto

    wid = wish["id"]
    label = f"{wish.get('artist') or '?'} — {wish.get('title') or '?'}"
    if soulseek_auto.job_state().get("state") == "running":
        # a user-initiated job owns the pipeline right now
        return "skipped"
    if not (soulseek.is_running() or soulseek.web_up(cfg)):
        wishes.log("warn", "slskd is not running — skipping this cycle")
        return "skipped"
    server = soulseek.server_state(cfg)
    if not (server or {}).get("isLoggedIn"):
        wishes.log("warn", "Soulseek is not logged in — skipping this cycle")
        return "skipped"

    wishes.mark_searching(wid)
    _set(current=label)
    wishes.log("info", f"Wish search: {label}")
    r = soulseek_auto.start_job(
        release_mbid=wish["release_mbid"],
        queries=(wish.get("queries") or None),
    )
    if not r.get("ok"):
        wishes.mark_wanted(wid, error=r.get("error", "job refused"),
                           attempts=int(wish.get("attempts") or 0) + 1)
        return "pending"
    st = _wait_job(_stopped)
    result = st.get("result") or {}
    if st.get("state") == "done" and result.get("imported"):
        wishes.mark_imported(wid, result.get("album_path") or "")
        # the library changed — drop caches so the UI sees the new album
        try:
            from server import tagcache, mbresolve
            tagcache.invalidate_all()
            mbresolve.invalidate()
        except Exception:
            pass
        return "imported"

    attempts = int(wish.get("attempts") or 0) + 1
    max_attempts = int(cfg.get("wishes_max_attempts", 0) or 0)
    err = result.get("error") or st.get("stage") or "no verified match yet"
    if max_attempts and attempts >= max_attempts:
        wishes.mark_failed(wid, err, attempts)
    else:
        wishes.mark_wanted(wid, error=str(err)[:300], attempts=attempts)
    return "pending"


def _due(wish, cfg):
    interval = int(cfg.get("wishes_interval_hours", 6) or 6) * 3600
    return (time.time() - float(wish.get("last_search") or 0)) >= interval


def run_cycle(wid=None):
    """One serialized pass over the open wishes. `wid` targets a single wish
    (its interval is ignored). Safe to call from any thread."""
    global _running_cycle
    with _lock:
        if _running_cycle:
            return {"ok": False, "error": "a wishes cycle is already running"}
        _running_cycle = True
    _set(running=True, cycle_started=time.time(), last_result="")
    try:
        cfg = load_config()
        open_wishes = [w for w in wishes.list_wishes()
                       if w["status"] in ("wanted", "failed") or w["status"] == "searching"]
        if wid is not None:
            open_wishes = [w for w in open_wishes if w["id"] == int(wid)]
        else:
            open_wishes = [w for w in open_wishes if _due(w, cfg)]

        # A stale 'searching' status (worker crashed mid-cycle) should retry.
        for w in open_wishes:
            if w["status"] == "searching":
                wishes.mark_wanted(w["id"])

        imported = pending = skipped = 0
        for w in open_wishes:
            if _stopped.is_set():
                break
            try:
                outcome = _run_one(w, cfg)
            except Exception as e:
                traceback.print_exc()
                wishes.mark_wanted(w["id"], error=str(e),
                                   attempts=int(w.get("attempts") or 0) + 1)
                outcome = "pending"
            imported += outcome == "imported"
            pending += outcome == "pending"
            skipped += outcome == "skipped"
            if skipped and wid is None:
                break  # slskd down / user job running — abort the whole pass
            time.sleep(2.0)

        resolved = 0
        try:
            resolved = wishes.reconcile_with_library(cfg)
        except Exception:
            traceback.print_exc()

        summary = (f"{imported} imported, {pending} still wanted, "
                   f"{resolved} resolved from library")
        _set(last_result=summary, last_cycle=time.time(),
             next_run=time.time() + int(cfg.get("wishes_interval_hours", 6) or 6) * 3600)
        if open_wishes:
            wishes.log("info", "Wishes cycle done — " + summary)
        return {"ok": True, "imported": imported, "pending": pending,
                "resolved": resolved}
    finally:
        _set(running=False, current=None)
        with _lock:
            _running_cycle = False


def _stopped():
    return _stop.is_set()


def _loop():
    # initial settle, then cycle on the configured interval
    time.sleep(20.0)
    while not _stop.is_set():
        try:
            cfg = load_config()
            if cfg.get("wishes_enabled", True):
                run_cycle()
        except Exception:
            traceback.print_exc()
        interval = int(load_config().get("wishes_interval_hours", 6) or 6) * 3600
        # wake often enough to notice a disabled worker / stop signal
        slept = 0
        while slept < interval and not _stop.is_set():
            time.sleep(30.0)
            slept += 30
            cfg = load_config()
            if not cfg.get("wishes_enabled", True):
                slept = interval  # park until re-enabled


def start():
    global _worker
    with _lock:
        if _worker and _worker.is_alive():
            return False
        _stop.clear()
        _worker = threading.Thread(target=_loop, name="mlo-wishes", daemon=True)
        _worker.start()
    return True


def stop():
    _stop.set()


def trigger(wid=None):
    """Kick a cycle now in the background (the UI's Search now / Search all)."""
    if _running_cycle:
        return {"ok": False, "error": "a wishes cycle is already running"}
    threading.Thread(target=run_cycle, kwargs=dict(wid=wid),
                     name="mlo-wishes-manual", daemon=True).start()
    return {"ok": True}
