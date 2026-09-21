"""Background worker that fills the Soulseek wishlist.

Runs a pass over the open wishes: every due wish is re-searched through the
normal auto-import pipeline (server/soulseek_auto.py), `soulseek_search_concurrency`
of them AT THE SAME TIME, and each wish is marked ``imported`` when its release
lands — or left ``wanted`` for the next interval. After each pass the library is
reconciled so wishes filled out-of-band (a manual download) are resolved too.

One PASS at a time (two passes would double-search the same wishes); several
WISHES inside a pass, which is what makes a queue of wishes move instead of
ticking along one album per download. The transfers that follow are still capped
by slskd's own slots.

Started from the FastAPI lifespan; the interval and master switch live in the
config (``wishes_enabled`` / ``wishes_interval_hours``). ``wishes_auto_import``
turns the downloading off: wishes are then only flagged for a manual import.
"""
import threading
import time
import traceback

from mlo import import_policy
from mlo.config import load_config
from server import wishes

_lock = threading.Lock()
_stop = threading.Event()
_worker = None
_running_cycle = False

_state = {
    "running": False,
    "current": None,      # first label being searched (older clients show one)
    "active": [],         # [{wish_id, label, job_id}] — one row per search
    "cycle_started": 0.0,
    "last_cycle": 0.0,
    "last_result": "",    # human summary of the last cycle
    "next_run": 0.0,
}

# Shown on a wish the user has to fill by hand.
_MANUAL_NOTE = ("Auto-import is off — import this release manually from the "
                "Soulseek page (or turn the switch back on in Settings).")


def _width(cfg=None):
    """How many wishes one pass searches at the same time.

    The pipeline's own ceiling (`soulseek_search_concurrency`): the worker must
    not open more searches than the pipeline will accept, or every extra thread
    does nothing but collect a refusal."""
    from server import soulseek_auto
    return max(1, soulseek_auto.concurrency(cfg))


def status():
    with _lock:
        st = dict(_state)
    st["active"] = [dict(a) for a in _state["active"]]
    st["interval_hours"] = int(load_config().get("wishes_interval_hours", 6) or 6)
    st["enabled"] = bool(load_config().get("wishes_enabled", True))
    st["concurrency"] = _width()
    return st


def _set(**fields):
    with _lock:
        _state.update(fields)


def _note(wish_id, label, job_id=None):
    """Record that a wish is being searched right now (the UI lists them).

    Several wishes run at once, so this is a list — and `current` mirrors the
    first one for the clients that predate it."""
    with _lock:
        active = [a for a in _state["active"] if a["wish_id"] != wish_id]
        active.append({"wish_id": wish_id, "label": label, "job_id": job_id})
        _state["active"] = active
        _state["current"] = active[0]["label"]


def _note_done(wish_id):
    with _lock:
        active = [a for a in _state["active"] if a["wish_id"] != wish_id]
        _state["active"] = active
        _state["current"] = active[0]["label"] if active else None


def _wish_found(wish, album_path="", note=""):
    """Announce a wish that the pipeline just filled.

    This is the notification the whole feature exists for: the user added a
    release to the wishlist days ago and it just landed, so every client is
    told (see server/events.py) and the phone in the other room can say so.
    Never raises — a notification must not fail the import that earned it.

    *note* is what the import's own script chain did, when the pipeline
    reported it by the time the wish settled (``imports.chain_summary``; "" and
    the plain wording when it did not — the auto-importer runs the chain on a
    thread of its own and reports its outcome where it finishes, not here).
    """
    try:
        from server import events
        artist = str(wish.get("artist") or "").strip()
        title = str(wish.get("title") or "").strip()
        label = f"{artist} — {title}" if artist and title else (title or artist or "Wish")
        album_path = str(album_path or wish.get("album_path") or "")
        body = "It downloaded and imported into your library."
        if note:
            body = f"It downloaded and imported into your library — {note}."
        events.emit("wish_found", f"Wish found: {label}", body,
                    {"wish_id": wish.get("id"),
                     "release_mbid": str(wish.get("release_mbid") or ""),
                     "album_path": album_path,
                     "chain": note,
                     "link": f"/album/{album_path}" if album_path else "/soulseek"})
    except Exception:
        traceback.print_exc()


def _chain_note(result):
    """The import's own report of its script chain, "" when it carries none.

    `imports.chain_summary` is the one wording for it, and it returns "" for a
    result that says nothing about a chain — a stub, or the auto-importer's job
    result while its chain thread is still running (that thread reports its own
    outcome where it finishes)."""
    try:
        from server import imports
        return imports.chain_summary(result)
    except Exception:
        return ""


def _wait_job(job_id, cancel_check, timeout_s=3 * 3600 + 600):
    """Block until THIS job settles; returns its state.

    The job carries its own per-candidate ceilings (up to 2 h for a download
    plus the log stage), so a fixed wait here would abandon a download that is
    still progressing and charge the wish a bogus attempt. `cancel_check` lets
    a stop / disable break the wait instead. The wait itself is still bounded
    (default just past the longest job the pipeline can run): a wedged job
    must not park the worker forever — the wish is re-marked wanted so the
    next cycle retries it instead of rotting in 'searching'.

    Addressed BY ID: several wishes are in flight at once and job_state() with
    no id answers for whichever job the caller means, not for this wish's."""
    from server import soulseek_auto
    time.sleep(0.5)  # let the job transition to running first
    deadline = time.time() + timeout_s
    while True:
        st = soulseek_auto.job_state(job_id)
        if st.get("state") != "running" or cancel_check():
            return st
        if time.time() >= deadline:
            return st  # still running past every pipeline ceiling: give up waiting
        time.sleep(2.0)


def _run_one(wish, cfg):
    """Search + fill one wish. Returns 'imported' | 'pending' | 'skipped' |
    'offline' | 'not_found' | 'failed'.

    'skipped' is contention (the pipeline is full of other jobs) and never
    burns an attempt; 'offline' means slskd itself is not there — every other
    wish in the pass would fail the same way, so the pass stops. 'not_found'
    and 'failed' are the two TERMINAL ends (see server/wishes' retry policy):
    the former is "the network does not have it" after its own budget of empty
    searches, the latter "it kept failing" once the attempts cap is spent."""
    wid = wish["id"]
    if not cfg.get("wishes_auto_import", True):
        # The user turned auto-import off: never search or download for them —
        # the wish stays open with a note so they can fill it by hand.
        if wish.get("last_error") != _MANUAL_NOTE:
            wishes.mark_wanted(wid, error=_MANUAL_NOTE)
        return "skipped"

    from server import soulseek
    from server import soulseek_auto

    label = f"{wish.get('artist') or '?'} — {wish.get('title') or '?'}"
    if not (soulseek.is_running() or soulseek.web_up(cfg)):
        wishes.log("warn", "slskd is not running — skipping this cycle")
        return "offline"
    server = soulseek.server_state(cfg)
    if not (server or {}).get("isLoggedIn"):
        wishes.log("warn", "Soulseek is not logged in — skipping this cycle")
        return "offline"

    wishes.mark_searching(wid)
    _note(wid, label)
    wishes.log("info", f"Wish search: {label}")
    # A wish normally stores the release GROUP id (that is what a
    # musicbrainz.org album link carries), and the release endpoint 404s on a
    # group — resolve it exactly like the HTTP route does, so no wish is
    # permanently unfillable.
    from server import integrations as intg
    release, release_mbid = intg.resolve_release(wish["release_mbid"])
    if not release_mbid:
        # A release MusicBrainz will not resolve is a TRANSIENT failure like any
        # other (an outage, a rate limit, a bad id the user can fix): it goes
        # through the same settle policy instead of its own private retry, so a
        # permanently unfillable wish still ends up announced rather than being
        # re-resolved forever.
        return _settle_attempt(wish, cfg,
                               "MusicBrainz release could not be resolved "
                               f"({wish['release_mbid']})")
    r = soulseek_auto.start_job(
        release_mbid=release_mbid,
        release=release,
        queries=(wish.get("queries") or None),
        wish_id=wid,
        source="musicbrainz",
    )
    if not r.get("ok"):
        err = str(r.get("error") or "job refused")
        if r.get("transient"):
            # The pipeline is busy with other jobs (a manual run, or the rest
            # of this pass): the wish keeps its place and costs no attempt.
            wishes.mark_wanted(wid, error=err,
                               attempts=int(wish.get("attempts") or 0))
            return "skipped"
        low = err.lower()
        if "already in your library" in low:
            # honey: trust start_job's owned check; reconcile pins the path.
            resolved = wishes.reconcile_with_library(cfg)
            if not resolved:
                wishes.mark_imported(wid, "")
                _wish_found(wish)
            return "imported"
        if "already queued" in low or "already running" in low or "being imported" in low:
            # Transient pipeline contention, not a failed attempt: leave the
            # wish open without burning an attempt.
            wishes.mark_wanted(wid, error=err,
                               attempts=int(wish.get("attempts") or 0))
            return "skipped"
        wishes.mark_wanted(wid, error=err,
                           attempts=int(wish.get("attempts") or 0) + 1)
        return "pending"
    job_id = (r.get("job") or {}).get("id")
    _note(wid, label, job_id)
    st = _wait_job(job_id, _stopped)
    if st.get("state") == "running":
        # Stopped by cancellation, or the job outlived every pipeline ceiling
        # (wedged) — re-mark wanted so the wish cannot rot in 'searching'.
        wishes.mark_wanted(wid, error="search interrupted — will retry",
                           attempts=int(wish.get("attempts") or 0))
        return "pending"
    result = st.get("result") or {}
    if st.get("state") == "done" and result.get("imported"):
        wishes.mark_imported(wid, result.get("album_path") or "")
        _wish_found(wish, result.get("album_path") or "", _chain_note(result))
        # the library changed — drop caches so the UI sees the new album
        try:
            from server import tagcache, mbresolve
            tagcache.invalidate_all()
            mbresolve.invalidate()
        except Exception:
            pass
        return "imported"

    return _settle_attempt(wish, cfg,
                           result.get("error") or st.get("stage")
                           or "no verified match yet")


def _settle_attempt(wish, cfg, err):
    """One finished attempt: decide again-or-done, in ONE place.

    The retry policy itself lives in `server.wishes` (classified outcomes,
    budgets, backoff); this is the only caller, so every way an attempt can end
    — a job that failed, a release MusicBrainz could not resolve, a search that
    found nothing — lands here and gets the same answer:

    * a search that found NOTHING spends a not-found attempt and ends the wish
      when that budget is gone (`wishes_not_found_attempts`),
    * a TRANSIENT failure (a refused slskd, an outage, a failed verification)
      is retried after its backoff until the attempts cap is spent.

    Both ends are terminal and both are announced ONCE by the marking call
    itself; both come back only through the user's own retry. Returns the
    pass-level outcome ("pending" | "not_found" | "failed")."""
    wid = wish["id"]
    attempts = int(wish.get("attempts") or 0) + 1
    if wishes.outcome_of(err) == "not_found":
        empty = int(wish.get("not_found") or 0) + 1
        cap = wishes.not_found_attempts(cfg)
        if cap and empty >= cap:
            wishes.mark_not_found(wid, err, attempts)
            return "not_found"
        wishes.mark_wanted(wid, error=str(err)[:300], attempts=attempts)
        return "pending"
    cap = wishes.max_attempts(cfg)
    if cap and attempts >= cap:
        wishes.mark_failed(wid, err, attempts)
        return "failed"
    wishes.mark_wanted(wid, error=str(err)[:300], attempts=attempts,
                       retry_at=time.time() + wishes.retry_delay(cfg, attempts))
    return "pending"


def _due(wish, cfg):
    """Is this wish due for its own next search? (Interval + any backoff.)"""
    return time.time() >= wishes.due_at(wish, cfg)


def _run_pass(open_wishes, cfg):
    """Run one pass over `open_wishes`, several at a time.

    A pool of `_width(cfg)` threads, each owning one wish from search to
    settled job: searching is almost entirely waiting on the network, so one
    wish at a time left the queue moving one row while the other wishes sat
    idle. The width is the pipeline's own concurrency — the transfers that
    follow are still capped by slskd's slots, which is what keeps this honest
    rather than a stampede.

    Returns the per-wish outcomes, in the order the wishes were given."""
    from concurrent.futures import ThreadPoolExecutor

    width = min(_width(cfg), max(1, len(open_wishes)))
    offline = threading.Event()
    outcomes = {}

    def work(wish):
        wid = wish["id"]
        if _stopped() or offline.is_set():
            return "skipped"
        try:
            out = _run_one(wish, cfg)
        except Exception as e:
            traceback.print_exc()
            try:
                wishes.mark_wanted(wid, error=str(e),
                                   attempts=int(wish.get("attempts") or 0) + 1)
            except Exception:
                pass
            return "pending"
        finally:
            _note_done(wid)
        if out == "offline":
            # slskd is not there: every other wish would burn its own attempt
            # learning that, so the pass stops where it is.
            offline.set()
        return out

    with ThreadPoolExecutor(max_workers=width,
                            thread_name_prefix="mlo-wish") as pool:
        futures = [pool.submit(work, w) for w in open_wishes]
        for wish, fut in zip(open_wishes, futures):
            try:
                outcomes[wish["id"]] = fut.result()
            except Exception:
                traceback.print_exc()
                outcomes[wish["id"]] = "pending"
    return outcomes


def run_cycle(wid=None):
    """One pass over the open wishes, `soulseek_search_concurrency` at a time.

    `wid` targets a single wish (its interval is ignored). Safe to call from any
    thread; two cycles at once are refused (they would double-search the same
    wishes — the passes never overlap, the WISHES inside one do)."""
    global _running_cycle
    with _lock:
        if _running_cycle:
            return {"ok": False, "error": "a wishes cycle is already running"}
        _running_cycle = True
    _set(running=True, cycle_started=time.time(), last_result="",
         active=[], current=None)
    try:
        cfg = load_config()
        if wid is None and not import_policy.auto_acquisition_enabled(cfg):
            # `auto_acquisition_enabled` is off, and this pass is the app's own
            # searching — so nothing is searched and nothing is downloaded. The
            # wishes themselves are untouched: they are the user's record of
            # what they want, and they keep their state (a wish marked
            # "not found" or "failed" must not be rewritten by a switch). An
            # explicit id is NOT refused: `trigger(wid)` is the user's own
            # "Search now" on one wish, which is the way back the note names.
            # The cycle says the switch is off rather than reporting "0
            # imported", which would read as "nothing was found".
            _set(last_result=import_policy.AUTO_OFF_NOTE, last_cycle=time.time(),
                 next_run=0.0)
            return {"ok": False, "error": import_policy.AUTO_OFF_NOTE,
                    "automation": False, "imported": 0, "pending": 0,
                    "not_found": 0, "failed": 0, "resolved": 0}
        pool = wishes.list_wishes()
        if wid is not None:
            # An explicit id IS the user's own retry: it overrides the
            # interval, and a TERMINAL wish (not_found, or failed with its
            # attempts spent) starts over rather than silently re-failing its
            # spent cap — see wishes.rearm.
            target = [w for w in pool if w["id"] == int(wid)]
            if target and wishes.is_terminal(target[0], cfg):
                wishes.rearm(int(wid))
                target = [wishes.get_wish(int(wid))]
            open_wishes = target
        else:
            # Terminal rows are not searched again by the timer (that is what
            # terminal means); everything else waits out its own interval and
            # any transient backoff (wishes.due_at).
            open_wishes = [w for w in pool
                           if not wishes.is_terminal(w, cfg) and _due(w, cfg)]

        # A stale 'searching' status (worker crashed mid-cycle) should retry.
        for w in open_wishes:
            if w["status"] == "searching":
                wishes.mark_wanted(w["id"])

        by_id = _run_pass(open_wishes, cfg) if open_wishes else {}
        imported = sum(1 for v in by_id.values() if v == "imported")
        pending = sum(1 for v in by_id.values() if v == "pending")
        skipped = sum(1 for v in by_id.values() if v == "skipped")
        # The terminal ends, named for what they are: "nothing was found" and
        # "it failed for good" are different answers, and both are already
        # announced on the bus by the marking call (see marks in server/wishes).
        empty = sum(1 for v in by_id.values() if v == "not_found")
        gave_up = sum(1 for v in by_id.values() if v == "failed")

        resolved = 0
        try:
            resolved = wishes.reconcile_with_library(cfg)
        except Exception:
            traceback.print_exc()

        summary = (f"{imported} imported, {pending} still wanted, "
                   f"{resolved} resolved from library")
        if empty:
            summary += f", {empty} not found (no further searches)"
        if gave_up:
            summary += f", {gave_up} failed for good"
        if skipped:
            summary += f" ({skipped} not started)"
        _set(last_result=summary, last_cycle=time.time(),
             next_run=time.time() + int(cfg.get("wishes_interval_hours", 6) or 6) * 3600)
        if open_wishes:
            wishes.log("info", "Wishes cycle done — " + summary)
        return {"ok": True, "imported": imported, "pending": pending,
                "not_found": empty, "failed": gave_up, "resolved": resolved}
    finally:
        _set(running=False, current=None, active=[])
        with _lock:
            _running_cycle = False



def _stopped():
    return _stop.is_set()


def _loop():
    """Search constantly: one pass IMMEDIATELY, then keep polling for wishes
    that have come due.

    Previously the loop ran one cycle and then slept the WHOLE interval, so a
    wish added a minute after a cycle waited up to `wishes_interval_hours`
    before it was ever looked for. `_due()` is now consulted on a short tick,
    which makes the searching effectively continuous: a wish is picked up as
    soon as its own interval has elapsed (a brand-new one has `last_search`
    0, so it is due on the very next tick), without hammering the network —
    each wish still has its own interval between its searches.
    """
    time.sleep(20.0)   # initial settle: let the app finish booting
    while not _stop.is_set():
        cfg = load_config()
        if not cfg.get("wishes_enabled", True):
            time.sleep(30.0)
            continue
        try:
            # run_cycle() only picks up wishes that are actually due, so
            # calling it on every tick is safe and is what keeps searching
            # continuous instead of bursty.
            run_cycle()
        except Exception:
            traceback.print_exc()
        for _ in range(_TICK_S // 5):
            if _stop.is_set():
                return
            time.sleep(5.0)
            if not load_config().get("wishes_enabled", True):
                break


# How often the loop looks for wishes that have come due. Each individual
# wish still honours `wishes_interval_hours` between its own searches.
_TICK_S = 120


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
