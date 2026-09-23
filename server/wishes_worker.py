"""Background worker that fills the Soulseek wishlist.

Runs a pass over the open wishes: every due wish is re-searched through the
normal auto-import pipeline (server/soulseek_auto.py), `soulseek_search_concurrency`
of them AT THE SAME TIME, and each wish is marked ``imported`` when its release
lands — or left ``wanted`` for the next interval. After each pass the library is
reconciled so wishes filled out-of-band (a manual download) are resolved too.

One PASS at a time (two passes would double-search the same wishes); several
WISHES inside a pass, which is what makes a queue of wishes move instead of
ticking along one album per download. The transfers that follow are still capped
by slskd's own slots. A request that arrives DURING a pass is never refused: it
is run by the pass that is already running, the moment that one ends
(``trigger``/``run_cycle``) — a pass reads its wish list when it starts, so
anything recorded after that (an "Add to library", a Search now) would otherwise
wait for the loop's next tick with nothing on screen.

A wish that ends TERMINAL (nothing found, or failed for good — see
server.wishes' retry policy) has the framework album it created taken down with
it: nothing will fill that folder once the worker stops searching the wish, and
a library left showing an album nobody has is exactly what the terminal outcome
has to clean up (see ``_drop_framework_album``).

Started from the FastAPI lifespan; the interval and master switch live in the
config (``wishes_enabled`` / ``wishes_interval_hours``). ``wishes_auto_import``
turns the downloading off: wishes are then only flagged for a manual import.
"""
import os
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
                     "release": wish.get("release") or {},
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


def _prime_identities(cfg):
    """Fill in the release identity of wishes that have none yet.

    DISPLAY data, never an acquisition: a row has to say which pressing it is
    waiting for, and the lookup is the same MusicBrainz call the pass itself
    makes. It runs in this worker's own thread — never in a route the UI polls
    — and also while the automation is off, because a wish the user has to fill
    by hand is exactly the one whose row must say what to look for."""
    try:
        wishes.prime_identities(cfg)
    except Exception:
        traceback.print_exc()


def _live_wish_ids():
    """Wish ids a RUNNING job is filling right now (a job parked on a question
    counts: it is still that wish's acquisition), plus the ones whose job is
    WAITING for a free pipeline slot.

    The job registry is process memory, so this answers "right now, in this
    app" — which is the question both callers ask: whether a 'searching' status
    is stale (the worker died mid-pass) or real, and whether a Search now should
    start a job that is already running. After a restart the registry is empty
    and nothing false-positives.
    """
    from server import soulseek_auto
    out = set()
    try:
        for j in soulseek_auto.jobs():
            if j.get("wish_id") and j.get("state") in ("running", "confirm"):
                out.add(int(j["wish_id"]))
        # A wish that took its place in the waiting queue has its acquisition in
        # hand too — as a REQUEST rather than a job, because the pipeline was
        # full when it was asked for.
        out |= soulseek_auto.queued_wish_ids()
    except Exception:
        pass
    return out


def _drop_framework_album(wish, cfg):
    """Take down the framework album of a wish whose acquisition has ENDED.

    "Add to library" creates a real folder in the library — the marker, the
    release's own tracklist, the placeholder cover — so the album is visible the
    moment it is asked for. Once the wish is terminal the worker never searches
    it again by itself, so nothing will ever fill that folder: left in place, the
    library kept showing an album nobody has, with a badge saying it is waiting
    for a download that has given up.

    The WISH ROW itself stays. It is the durable request, and the queue's own
    row — the one that says what happened and carries the retry — is drawn from
    it; deleting it would take the outcome off the screen the user has to act
    on. Only `album_path` is cleared, so the row stops linking to a folder that
    is no longer there.

    `pending_albums.remove_folder` refuses a folder that holds audio, so a
    download that partly landed (a real album by then) is never touched, and a
    wish with no folder at all is a no-op.
    """
    from server import pending_albums

    wid = wish.get("id")
    path = str(wish.get("album_path") or "")
    try:
        removed = pending_albums.remove_for_wish(wid, cfg)
    except Exception:
        traceback.print_exc()
        return
    if removed:
        wishes.log("info", "Framework album removed after a terminal outcome: "
                    + (os.path.basename(path) or "framework album"))
    if path and not os.path.isdir(path):
        try:
            wishes.update_wish(wid, {"album_path": ""})
        except Exception:
            traceback.print_exc()


def _run_one(wish, cfg):
    """Search + fill one wish. Returns 'imported' | 'pending' | 'skipped' |
    'offline' | 'not_found' | 'failed' | 'background'.

    'skipped' is contention (the pipeline is full of other jobs) and never
    burns an attempt; 'offline' means slskd itself is not there — every other
    wish in the pass would fail the same way, so the pass stops. 'not_found'
    and 'failed' are the two TERMINAL ends (see server/wishes' retry policy):
    the former is "the network does not have it" after its own budget of empty
    searches, the latter "it kept failing" once the attempts cap is spent.
    'background' is the third end and the only non-terminal one: the release
    asked every ranked candidate the walk may ask and none answered, so it
    stays in the pipeline as a standing background request (spec R153).

    ONE ATTEMPT WALKS THE RANKED CANDIDATES (spec R150-R152): the best edition
    first, then the next, each with its own bounded search window, stopping at
    the first that lands. A candidate that answers with nothing usable is spent
    for this attempt and the walk moves on; a candidate that fails for a
    TRANSIENT reason stops the walk and is settled by the retry policy, because
    a network that is down is not a candidate that is absent. The walk restarts
    at the best candidate on the next attempt.
    """
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

    if wid in _live_wish_ids():
        # A job is already filling this wish. That IS its acquisition, and
        # starting a second one is how the same album came down twice: the
        # pipeline's album-folder claim queues the second job behind the first
        # and it then searches, downloads and imports the album all over again.
        # Reachable without anything looking wrong — the queue row offers its
        # Search button while its job runs, so a press lands here.
        wishes.log("info", f"{label} is already being searched — not started again")
        return "skipped"

    walk = _walk_candidates(wish, cfg)
    if not walk:
        # A wish keyed by NAME has no release to look up at all: report it the
        # way this worker always has, through the same settle policy.
        return _settle_attempt(wish, cfg, "this wish names no MusicBrainz release")
    # A fresh attempt starts at the BEST candidate: `candidate` is where the
    # last attempt left the walk, and the ranking exists so that the edition
    # the policy prefers is the one asked for first (spec R150).
    if len(walk) > 1:
        wishes.restart_walk(wid)
    walk_label = label
    if len(walk) > 1:
        walk_label = f"{label} — {wishes.candidate_label(0, len(walk))}"
    wishes.mark_searching(wid)
    _note(wid, walk_label)
    wishes.log("info", f"Wish search: {label}"
                + (f" ({len(walk)} ranked candidate(s) to try)" if len(walk) > 1 else ""))
    # This attempt's window per candidate. A candidate's search is BOUNDED, and
    # the walk's own count bounds how many of those windows one attempt may
    # spend, so a wish can never search for ever in one pass (spec R151).
    window = max(5, int(cfg.get("soulseek_search_timeout_seconds", 60) or 60))

    last = ""
    for pos, cand in enumerate(walk):
        if pos:
            wishes.advance_candidate(wid, cfg)
            _note(wid, f"{label} — {wishes.candidate_label(pos, len(walk))}")
        outcome, detail = _try_candidate(wish, cfg, cand, window, pos, len(walk))
        if outcome == "imported":
            wishes.mark_imported(wid, detail)
            note = _chain_note(getattr(_LAST_RESULT, "result", None) or {})
            if pos:
                # The best edition was not there, so this IS another pressing:
                # say which one landed rather than announcing a plain success
                # the user would read as the edition they asked for.
                note = (f"the best edition was not available, so this is "
                        f"{wishes.candidate_label(pos, len(walk))}"
                        + (f" — {note}" if note else ""))
            _wish_found(wish, detail, note)
            # the library changed — drop caches so the UI sees the new album
            try:
                from server import tagcache, mbresolve
                tagcache.invalidate_all()
                mbresolve.invalidate()
            except Exception:
                pass
            return "imported"
        if outcome != "empty":
            # 'pending'/'skipped'/'failed' already settled the wish (a transient
            # failure or pipeline contention) and 'not_found' ended it: none of
            # them is a reason to ask the next candidate.
            return outcome
        last = detail or last
        if pos + 1 < len(walk):
            wishes.log("info", f"No copy of {cand['title'] or cand['mbid']} — "
                               f"candidate {pos + 1} of {len(walk)} is spent; "
                               f"moving on to the next ranked edition.")
    return _settle_attempt(wish, cfg, last)


# The result the last `_try_candidate` FINISHED with, per thread: the wish
# passed to `_wish_found` when a candidate landed, so the announcement can carry
# the import's own chain summary. Per thread because several wishes are in
# flight at once (the same reason soulseek_auto keeps its state thread-local).
_LAST_RESULT = threading.local()


def _walk_candidates(wish, cfg):
    """The ranked candidates THIS attempt asks, best first (spec R150-R152).

    The release-choice policy's own order, capped by the user's
    `soulseek_fallback_candidates`. A group with fewer eligible editions than
    the cap simply ends at the end of its own list — no error, no empty slot,
    nothing waiting for a candidate that does not exist — and a wish recorded
    without a ranked list falls back to the single candidate its own key names,
    exactly as every wish worked before the walk existed.

    The list is then narrowed to DISTINCT PRESSINGS
    (`mlo.release_choice.distinct_pressings`): two editions that state the same
    catalog number are one search — the number is what a CD search is keyed on,
    and MusicBrainz really does carry one pressing as two releases (a label
    change, a reissue, a country variant) — so asking the second can only find
    the folders the first already found. What was dropped is LOGGED, because a
    fallback that silently loses a ranked edition is exactly the kind of
    quiet shortcut this walk exists to avoid. Filtering here (and not only
    where the list is written) also fixes a list stored before the rule
    existed: an older wish's walk is deduplicated on its next attempt.
    """
    out = []
    for row in (wish.get("candidates") or [])[:wishes.fallback_limit(cfg)]:
        mbid = str((row or {}).get("mbid") or "").strip()
        if mbid:
            out.append({"mbid": mbid, "title": str((row or {}).get("title") or ""),
                        "catalog_numbers": [str(n) for n in
                                            ((row or {}).get("catalog_numbers") or [])]})
    if not out:
        here = wishes.candidate_of(wish) or {}
        if here.get("mbid"):
            out = [{"mbid": here["mbid"], "title": here.get("title") or "",
                    "catalog_numbers": [str(n) for n in
                                        (here.get("catalog_numbers") or [])]}]
    if len(out) > 1:
        from mlo.release_choice import distinct_pressings

        kept, duplicates = distinct_pressings(out)
        if duplicates:
            names = ", ".join(str(d.get("title") or d.get("mbid")) for d in duplicates[:3])
            wishes.log("info",
                       f"{str(wish.get('album') or wish.get('title') or '').strip() or 'Wish'}: "
                       f"{len(duplicates)} ranked edition(s) share a catalog number with "
                       f"one already being tried ({names}) — the same search, so they "
                       f"are skipped")
        out = kept
    return out


def _try_candidate(wish, cfg, cand, window, pos, total):
    """Ask the network for ONE ranked candidate.

    Returns ``(outcome, detail)``:

    * ``("imported", album_path)`` — this candidate landed; the acquisition is
      over and the caller marks the wish imported;
    * ``("empty", err)`` — the search answered and there is nothing usable for
      THIS edition, which is what makes the walk move on (the ordinary
      not-found classification, `wishes.outcome_of`);
    * ``("pending", err)`` / ``("skipped", err)`` — a transient failure or
      pipeline contention: already settled by the retry policy (a backoff, or
      nothing at all), and never a reason to ask the next candidate;
    * ``("offline", err)`` — slskd is gone, which ends the whole pass.

    One candidate costs ONE MusicBrainz lookup (its own edition) and ONE
    bounded search — never a fresh ranking, and never a second search to decide
    what to try next (spec R150).
    """
    from server import integrations as intg
    from server import soulseek_auto

    wid = wish["id"]
    release, release_mbid = intg.resolve_release(cand["mbid"])
    if not release_mbid:
        # A release MusicBrainz will not resolve is a TRANSIENT failure like any
        # other (an outage, a rate limit, a bad id the user can fix): it goes
        # through the same settle policy instead of its own private retry, so a
        # permanently unfillable wish still ends up announced rather than being
        # re-resolved forever.
        return _settled(_settle_attempt(
            wish, cfg,
            f"MusicBrainz release could not be resolved ({cand['mbid']})"))
    # Which PRESSING this attempt is about, recorded on the wish itself: the
    # release is in hand here, so the queue row never spends a MusicBrainz
    # request of its own to say it (server.wishes.RELEASE_KEYS). It follows the
    # WALK, so the row names the edition being asked for right now.
    wishes.store_identity(wid, wishes.release_identity(release, release_mbid))
    r = soulseek_auto.start_job(
        release_mbid=release_mbid,
        release=release,
        queries=(wish.get("queries") or None),
        wish_id=wid,
        source="musicbrainz",
        # This candidate's own bounded search window (spec R151): the walk
        # gives each ranked edition its own, so three candidates are three
        # windows and not one shared eternity.
        search_seconds=window,
    )
    if not r.get("ok"):
        err = str(r.get("error") or "job refused")
        if r.get("transient"):
            # The pipeline is busy with other jobs (a manual run, or the rest
            # of this pass): the wish keeps its place and costs no attempt.
            wishes.mark_wanted(wid, error=err,
                               attempts=int(wish.get("attempts") or 0))
            return ("skipped", err)
        low = err.lower()
        if "already in your library" in low:
            # honey: trust start_job's owned check; reconcile pins the path.
            resolved = wishes.reconcile_with_library(cfg)
            if not resolved:
                # The library already holds this album and reconcile could not
                # tie it to a framework folder, so the placeholder would stand
                # for ever beside the real album (and read as a second release).
                # Nothing searches this wish again: take it down, like every
                # other terminal end here does. The caller marks the wish
                # imported and announces it, in ONE place (`_run_one`).
                _drop_framework_album(wish, cfg)
            return ("imported", "")
        if "already queued" in low or "already running" in low or "being imported" in low:
            # Transient pipeline contention, not a failed attempt: leave the
            # wish open without burning an attempt.
            wishes.mark_wanted(wid, error=err,
                               attempts=int(wish.get("attempts") or 0))
            return ("skipped", err)
        wishes.mark_wanted(wid, error=err,
                           attempts=int(wish.get("attempts") or 0) + 1)
        return ("pending", err)
    if r.get("waiting"):
        # The pipeline is full and this wish has taken its place in the waiting
        # queue (see soulseek_auto.start_job): it starts BY ITSELF the moment a
        # slot frees, so the wish is left open, costs no attempt, and needs no
        # retry of its own — the queue is holding it, not the network.
        wishes.mark_wanted(
            wid, error=f"waiting for a free pipeline slot (position "
                       f"{r.get('position')})",
            attempts=int(wish.get("attempts") or 0))
        return ("skipped", "")
    job_id = (r.get("job") or {}).get("id")
    _note(wid, f"{wish.get('artist') or '?'} — {wish.get('title') or '?'}"
               + (f" — {wishes.candidate_label(pos, total)}" if total > 1 else ""),
          job_id)
    st = _wait_job(job_id, _stopped)
    if st.get("state") == "running":
        # Stopped by cancellation, or the job outlived every pipeline ceiling
        # (wedged) — re-mark wanted so the wish cannot rot in 'searching'.
        wishes.mark_wanted(wid, error="search interrupted — will retry",
                           attempts=int(wish.get("attempts") or 0))
        return ("pending", "")
    result = st.get("result") or {}
    if st.get("state") == "done" and result.get("imported"):
        _LAST_RESULT.result = result
        return ("imported", result.get("album_path") or "")
    err = (result.get("error") or st.get("stage") or "no verified match yet")
    if wishes.outcome_of(err) == "not_found":
        # The search answered, and there is nothing usable for THIS edition —
        # the walk may move on (the caller decides, with the list in hand).
        return ("empty", err)
    return (_settle_attempt(wish, cfg, err), err)


def _settle_attempt(wish, cfg, err):
    """One finished WALK: decide again-or-done, in ONE place.

    The retry policy itself lives in `server.wishes` (classified outcomes,
    budgets, backoff); this is the only caller, so every way an attempt can end
    — a job that failed, a release MusicBrainz could not resolve, a search that
    found nothing — lands here and gets the same answer:

    * a walk that found NOTHING spends a not-found attempt. With a ranked list
      behind it the release is NOT ended and its album is NOT taken down: it
      moves to the BACKGROUND, where the worker keeps walking it on its own
      ticks until one of the candidates lands (spec R153). Only a wish with no
      ranked list at all — nothing to keep asking — ends `not_found`, which is
      the terminal outcome it always was.
    * a TRANSIENT failure (a refused slskd, an outage, a failed verification)
      is retried after its backoff until the attempts cap is spent.

    Every terminal end is announced ONCE by the marking call itself; both come
    back only through the user's own retry. Returns the pass-level outcome
    ("pending" | "not_found" | "failed" | "background").

    A TERMINAL end also takes the wish's framework album down
    (`_drop_framework_album`): nothing searches that wish again by itself, so
    the album folder "Add to library" created would sit in the library for good
    looking like an album nobody has. The BACKGROUND end deliberately does NOT:
    something IS still searching for it.
    """
    wid = wish["id"]
    attempts = int(wish.get("attempts") or 0) + 1
    if wishes.outcome_of(err) == "not_found":
        empty = int(wish.get("not_found") or 0) + 1
        cap = wishes.not_found_attempts(cfg)
        if cap and empty >= cap:
            if wishes.walk_length(wish, cfg) >= 1:
                # The walk is spent, and a spent walk is not an answer: every
                # ranked candidate is still a candidate. The release keeps its
                # place (and its framework album) as a background request the
                # worker re-walks on its own ticks.
                wishes.mark_background(wid, wishes.walk_report(wish, err, cfg),
                                       attempts=attempts, not_found=empty)
                return "background"
            wishes.mark_not_found(wid, err, attempts)
            _drop_framework_album(wish, cfg)
            return "not_found"
        # The spent empty search is RECORDED here: it is the counter the cap is
        # compared against, so a wish with a budget above one really does run
        # out of it instead of re-reading the same zero forever. The walk's own
        # pointer goes back to the BEST candidate at the same time: nothing is
        # being asked right now, and the next attempt starts at the top of the
        # ranking (spec R150) — a row left pointing at the last edition the last
        # attempt reached would describe a search that is not the one about to
        # run.
        wishes.mark_wanted(wid, error=str(err)[:300], attempts=attempts,
                           not_found=empty, candidate=0)
        return "pending"
    cap = wishes.max_attempts(cfg)
    if cap and attempts >= cap:
        if wishes.walk_length(wish, cfg) >= 1:
            # A WALK does not end because the network had a bad day: every
            # ranked edition is still an edition, and the release was asked for
            # by name ("Add to library" is what records the walk — spec R175).
            # It goes to the BACKGROUND, which is what "silent" means here: the
            # row stays where it already was, keeps its framework album, and is
            # re-walked from the best edition on the worker's own ticks
            # (R153). Reported as a FAILED row it landed in the section reserved
            # for things a person has to deal with — for a search the app is
            # still perfectly able to run.
            wishes.mark_background(wid, wishes.walk_report(wish, err, cfg),
                                   attempts=attempts)
            return "background"
        wishes.mark_failed(wid, err, attempts)
        _drop_framework_album(wish, cfg)
        return "failed"
    delay = wishes.retry_delay(cfg, attempts)
    # No backoff configured (0) is not "retry immediately, forever": the row
    # then falls back to the interval, which is what `due_at` reads off an
    # absent `retry_at` (a stamp of `now` would have made every tick a retry).
    wishes.mark_wanted(wid, error=str(err)[:300], attempts=attempts,
                       retry_at=(time.time() + delay) if delay else 0)
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


# Requests that arrived while a pass was running. A pass reads its wish list
# once, when it starts, so a wish recorded after that (an "Add to library", a
# Search now) was NOT in it — kept here, the pass that is running runs another
# one the moment it ends. That is what makes `trigger` unable to be a silent
# no-op: it used to answer `{"ok": False, "error": "a wishes cycle is already
# running"}` and leave the caller's wish untouched until the loop's next tick,
# up to _TICK_S later, with nothing on screen in between.
_followups = []
_followups_lock = threading.Lock()


def _follow_up(wid):
    with _followups_lock:
        _followups.append(wid)


def run_cycle(wid=None):
    """One pass over the open wishes, `soulseek_search_concurrency` at a time.

    `wid` targets a single wish (its interval is ignored, and a terminal one is
    re-armed: that is the user's own Search now / the queue's Retry). Safe to
    call from any thread.

    Passes never overlap — they would double-search the same wishes — and a
    request that arrives during one is NOT refused: it is recorded and run by
    that pass's own tail (`_followups`), in arrival order. The return value is
    the LAST pass's answer, which is the one whose request was the caller's."""
    global _running_cycle
    with _lock:
        if _running_cycle:
            _follow_up(wid)
            return {"ok": True, "queued": True, "running": True}
        _running_cycle = True
    try:
        while True:
            out = _cycle(wid)
            if _stopped():
                break
            with _followups_lock:
                if not _followups:
                    break
                wid = _followups.pop(0)
    finally:
        _set(running=False, current=None, active=[])
        with _lock:
            _running_cycle = False
    return out


def _cycle(wid=None):
    """One pass. Only `run_cycle` calls this, one pass at a time."""
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
            _prime_identities(cfg)
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

        # A stale 'searching' status (worker crashed mid-cycle) should retry —
        # unless a job really is filling that wish right now (the user's own
        # Search now landed on a wish whose job is running), which is not stale
        # at all and must keep the status its running job gave it.
        live = _live_wish_ids()
        for w in open_wishes:
            if w["status"] == "searching" and w["id"] not in live:
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
        # A spent walk is its own outcome and its own section: the release is
        # still searched (see server.wishes' `background` status), so it is
        # neither "nothing found" nor "still wanted".
        resting = sum(1 for v in by_id.values() if v == "background")

        resolved = 0
        try:
            resolved = wishes.reconcile_with_library(cfg)
        except Exception:
            traceback.print_exc()

        # AFTER the pass: naming the pressing must never delay a search.
        _prime_identities(cfg)

        summary = (f"{imported} imported, {pending} still wanted, "
                   f"{resolved} resolved from library")
        if resting:
            summary += f", {resting} moved to the background"
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
                "not_found": empty, "failed": gave_up, "background": resting,
                "resolved": resolved}
    finally:
        # Between passes the worker is not running — unless another pass was
        # requested while this one ran, which starts immediately (run_cycle's
        # tail): keeping `running` set is what stops the status flickering off
        # and on between two back-to-back passes. run_cycle's own finally owns
        # the final state.
        with _followups_lock:
            more = bool(_followups)
        _set(current=None, active=[], **({} if more else {"running": False}))



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
    """Kick a pass now, in the background (an add, the UI's Search now).

    NEVER refuses. A pass already in flight records the request and runs it the
    moment it ends (see `run_cycle`), so the caller's wish is never left
    untouched until the loop's next tick; the answer says the request was
    taken, and `running` whether a pass was already under way.

    `wid` targets ONE wish — the user's own Search now on a row, which overrides
    its interval and re-arms a terminal one. With no id it is the worker's OWN
    pass: every wish that is due by its own policy, which is what an add asks
    for — the wish it just recorded is due immediately, while one already
    waiting out its retry backoff keeps that wait.
    """
    threading.Thread(target=run_cycle, kwargs=dict(wid=wid),
                     name="mlo-wishes-manual", daemon=True).start()
    return {"ok": True, "queued": True, "running": bool(_running_cycle)}
