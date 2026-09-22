"""MusicBrainz-driven automated Soulseek importing.

Given a specific MusicBrainz release, this module finds the best matching
folder on the Soulseek network, verifies it before committing to the full
download, downloads it, audits it, and imports it into the library — the
whole pipeline a careful human would do by hand:

  1. build search queries from the release's identifiable traits (catalog
     number + country for CD rips; title + year for digital media; every
     term is customizable in Settings → Soulseek),
  2. search slskd with every query template at once and score the candidate
     folders (completeness vs the release track list, cue/log presence per
     disc, lossless, free slot),
  3. queue the whole folder in ONE pass but grade the .log file(s) first with
     Logchecker; any disc below the configured score (default 100) drops the
     transfers again and moves on to the next candidate without waiting for
     the album,
  4. wait for the album (a file counts as arrived only once slskd itself says
     the transfer finished) and verify every track against the .log CRCs (CD)
     or decode-check each file (digital media); with AcoustID configured the
     audio is also matched against the release's group (a mismatch is logged,
     never fatal),
  5. import into the library: stamp the MusicBrainz release/recording IDs
     that drove the search into the tags, write MEDIA, organize with the
     naming script, then finish the album in the background via
     ``server.imports.finish_album`` — RateYourMusic links, artist metadata
     and cover art, AND the configured script chain (optimization, tagging,
     grading), because a download the user waited for must come out finished
     rather than half-tagged. Whatever the chain cannot supply (no cover
     found, a family the user kept for themselves) raises the album's own
     prompt there, which is the notification the queue's "Needs you" row and
     the wizard link come from.

Every settled job announces its outcome ONCE on ``server.events`` and links to
the thing it is about — the album, the download folder, or this queue (see
``_notify_finish``): no acquisition is allowed to end in silence, whether it
succeeded, found nothing, or gave up with a reason.

ONE release is not on the network at all, and this module says so before it
searches: a music video whose medium is DIGITAL MEDIA (its recordings
MusicBrainz states are videos) is a set of YouTube uploads, so it is fetched
through ``server.youtube`` instead of searched for — see ``acquisition_route``
and ``_run_youtube``. That branch lives inside the same job, so its album takes
the same import, the same queue row, the same stages and the same log as a
downloaded folder; a music video on a DISC (DVD, Blu-ray, VHS, Video CD) and
every audio release keep the Soulseek path unchanged.

Progress is reported through mlo.stats.progress_hook (the same relay the
WebSocket /ws/progress endpoint forwards to the UI) and mirrored into a
pollable job state for the Soulseek page.
"""
import os
import re
import shutil
import threading
import time
import traceback
import unicodedata
from urllib.parse import quote

from mlo.config import DEFAULT_CONFIG, load_config
# The shared worker-count policy every multi-file runner obeys (the
# `worker_limit` setting), so the per-track tag writes below fan out like the
# rest of the app instead of inventing their own lane count.
from mlo.stats import worker_count
# The medium accessor of the ONE release-choice policy: a release dict from any
# payload (browse row, normalized row, full lookup) answers with its formats,
# which is what decides whether a release is digital or a pressing — and, with
# `video_formats`, whether a music-video release is a DISC (a folder on the
# network) or a Digital Media one (a set of YouTube uploads: see
# acquisition_route).
from mlo.release_choice import media_formats, video_formats
# The library's definition of a track, for the one step that must see a
# music-video album's files as tracks (the MB stamping below).
from mlo.paths import LIB_AUDIO_EXTS
# The app's own multi-value separator: RELEASECOUNTRY is written "; "-joined
# (mlo.audio joins repeated fields with it, mlo.naming._first_multi reads the
# first entry back), so the writer and the reader cannot disagree.
from mlo.tagtext import _LIST_SEP

# --------------------------------------------------------------------------- #
# Job state
# --------------------------------------------------------------------------- #
# Reentrant: cancel() holds the lock while calling _log(), which locks again.
_lock = threading.RLock()

# The stage vocabulary the whole pipeline is spoken in: the wish queue, the
# auto-import jobs, the bulk release queue and the import runner all report one
# of these names, so server/api_queue.py — and every client drawn from it —
# render the same word for the same state.
#
# A release WAITING for a free pipeline slot is deliberately NOT a stage of its
# own, which is why there is no "waiting" in this tuple: it is `queued`, exactly
# like a wish waiting for the network to answer, and what tells the two apart is
# WHERE it waits and its PLACE in that line — the `waiting` flag and 1-based
# `position` its row carries (see queued()/server.api_queue._bulk_rows), which
# is what the page's Waiting group is drawn from. A second word here would have
# to be rendered by every registry that speaks STAGES — the wishes list, the
# import runner, the job rows — while saying nothing about the work itself.
STAGES = ("queued", "searching", "downloading", "verifying", "importing",
          "completed", "failed", "needs_attention")

# SEVERAL jobs run at once (see `soulseek_search_concurrency`): one thread per
# job. `_jobs` is the registry job_state()/jobs() read, `_order` keeps the jobs
# oldest-first and `_primary` is the job a caller WITHOUT an id means — the
# single-job API this module grew up with (start/cancel/confirm/job_state),
# which the Auto-import panel and the tests still speak.
#
# A module-level `_job[...]` read/write means "the job on THIS thread" (see
# _CurrentJob) and falls back to the primary job, which is exactly what it
# meant when there was only ever one. The post-import staging chain re-binds
# the id in its own thread so its log lines land in the job that earned them.
_jobs: dict = {}
_order: list = []
_primary = 0
_seq = 0
_tl = threading.local()

# Settled jobs the registry keeps: enough that the queue view can show what
# just finished, bounded so a server up for a week does not accumulate one dict
# per album it ever downloaded.
_KEEP_SETTLED = 40

# How many releases run at once when nothing is configured.
_CONCURRENCY_DEFAULT = 3
# How many candidate downloads of ONE release run at once when nothing is
# configured — three peers of one album transfer side by side, the first that
# verifies good ends the batch.
_CANDIDATE_SLOTS_DEFAULT = 3
# slskd's own transfer ceiling when nothing is configured: the shipped default
# is concurrency × candidate_slots (3 × 3), so the app's two ceilings can be
# honoured without slskd queueing the difference.
_DOWNLOAD_SLOTS_DEFAULT = 9

# A job parked on a prompt waits here for the user's answer (confirm()):
# "only lossy copies found" and "no usable results — add to wishes?".
# These two are the IDLE job's own pair — the objects every older caller (and
# their tests) reaches for. A real job gets its own, which is what lets three
# jobs park on three different prompts at once.
_confirm_event = threading.Event()
_confirm_answer = {"accept": False}

# The job a caller with no job of its own means, and the shape job_state()
# answers with before anything has run: what the single-job registry published
# when it was idle, plus the fields the queue view needs.
_IDLE_JOB = {
    # idle | running | confirm | done | error | cancelled
    #   confirm = waiting for the user (only lossy copies found, no rip log, or
    #             the "add to wishes?" offer)
    "id": 0,
    "state": "idle",
    "stage": "",           # human-readable current step
    "stage_key": "",       # its name in STAGES — what the queue view groups on
    "release": None,       # compact release summary
    "log": [],             # [{t, msg}] progress lines (newest last)
    "attempts": [],        # [{username, dir, reason}] rejected candidates
    "result": None,        # {album_path, imported, organized}
    "confirm": None,
    "search": None,        # live per-query progress while searching
    "progress": None,      # live download metrics while a transfer runs
    "cancel": False,
    "wish_id": None,       # the wish this job is filling, when it fills one
    "source": "",          # "", "musicbrainz", "soulseek", "auto" — who asked
    "label": "",           # "Artist — Album", for a row with no release yet
    "leftovers": [],       # partial bytes a rejected candidate could not free
    "started_at": 0.0,
    "ended_at": 0.0,
    "_event": _confirm_event,   # the prompt this job waits on
    "_answer": _confirm_answer,
    "_claim": None,             # its library folder's job_locks claim
}
_jobs[0] = _IDLE_JOB


class _CurrentJob(dict):
    """`_job` as the CALLING context's job (see the registry above).

    A dict subclass so every existing ``_job[...]`` / ``_job.get(...)`` /
    ``_job.items()`` in this file (and in the tests that poke the shape) keeps
    working unchanged while the value it resolves to depends on who is asking:
    the thread's own job, else the primary one.
    """

    def _ref(self):
        jid = getattr(_tl, "jid", None)
        if jid is None:
            jid = _primary
        return _jobs.get(jid) or _IDLE_JOB

    def __getitem__(self, key):
        return self._ref()[key]

    def __setitem__(self, key, value):
        self._ref()[key] = value

    def __delitem__(self, key):
        del self._ref()[key]

    def __contains__(self, key):
        return key in self._ref()

    def __iter__(self):
        return iter(self._ref())

    def __len__(self):
        return len(self._ref())

    def __bool__(self):
        return bool(self._ref())

    def __eq__(self, other):
        return dict(self._ref()) == other

    def __ne__(self, other):
        return dict(self._ref()) != other

    def get(self, key, default=None):
        return self._ref().get(key, default)

    def items(self):
        return self._ref().items()

    def keys(self):
        return self._ref().keys()

    def values(self):
        return self._ref().values()

    def update(self, *args, **kwargs):
        self._ref().update(*args, **kwargs)

    def pop(self, *args):
        return self._ref().pop(*args)

    def setdefault(self, *args):
        return self._ref().setdefault(*args)

    def copy(self):
        return dict(self._ref())


_job = _CurrentJob()


def _published(job):
    """A job's public state: the internals (its prompt event, its claim) never
    leave the module."""
    return {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
            for k, v in job.items() if not k.startswith("_") and k != "cancel"}


def _active_locked():
    """Every job holding the pipeline right now. Caller holds ``_lock``."""
    return [j for j in _jobs.values() if j["state"] in ("running", "confirm")]


def _clamped_int(cfg, key, default, lo, hi):
    """One of the pipeline's own ceilings, whatever the config file holds.

    Clamped here as well as in mlo.config: a saved value can be older than the
    configured range, or hand-edited."""
    try:
        n = int((cfg or load_config()).get(key) or default)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def concurrency(cfg=None):
    """How many releases the pipeline may work on at the same time (1..8).

    This is a ceiling the APP enforces: a release over it is not refused, it
    waits in the pipeline's own queue (enqueue()/_start_next) and starts by
    itself the moment one of the running ones finishes. Searches and downloads
    both count against it. The outer ceiling on what slskd itself takes off the
    network is `soulseek_download_slots` — see download_slots() for which of
    the two actually enforces what."""
    return _clamped_int(cfg, "soulseek_search_concurrency",
                        _CONCURRENCY_DEFAULT, 1, 8)


def candidate_slots(cfg=None):
    """How many candidate downloads of ONE release may be in flight at once
    (1..20).

    The ceiling on the batch _try_batch runs: that many peers of one release
    transfer at the same time, the first that verifies good ends the batch (the
    losers are cancelled and swept), and the NEXT candidate is only asked for
    when one of them lands or is rejected — so however many candidates the
    search turned up, one release never talks to more peers than this. It is
    enforced by the app's own enqueueing, never by slskd."""
    return _clamped_int(cfg, "soulseek_candidate_slots",
                        _CANDIDATE_SLOTS_DEFAULT, 1, 20)


def download_slots(cfg=None):
    """slskd's own concurrent-transfer ceiling (1..20).

    Read here, written into slskd's YAML by server.soulseek.generate_yaml. It
    is NOT how the two ceilings above are enforced — those are the app's own
    promise and the app keeps them. This one is the outer ceiling slskd puts on
    the transfers that promise produces, so the shipped default is exactly
    concurrency × candidate_slots (3 × 3 = 9) and `_batch_width` narrows the
    per-release batch when a config sets fewer slots than its other two
    settings need."""
    return _clamped_int(cfg, "soulseek_download_slots",
                        _DOWNLOAD_SLOTS_DEFAULT, 1, 20)


def job_stage(job):
    """The STAGES name a job is in — the ONE vocabulary the queue view renders.

    A settled job answers with its outcome, a running one with the stage it
    published, and a parked one with ``needs_attention``: the queue must never
    say "downloading" about a job that is really waiting for the user.
    ``cancelled`` is deliberately NOT one of STAGES (a cancelled item vanishes
    from the queue instead of pretending to be a failure) — callers detect it
    by this value and drop the row."""
    state = str(job.get("state") or "idle")
    if state == "done":
        return "completed"
    if state == "error":
        return "failed"
    if state == "cancelled":
        return "cancelled"
    if state == "confirm":
        return "needs_attention"
    key = str(job.get("stage_key") or "")
    return key if key in STAGES else ("queued" if state == "running" else "")


def _pick(job_id=None):
    """The job a call means: the id it named, else the one this thread is in,
    else the newest job parked on a prompt, else the primary job. Caller holds
    ``_lock``."""
    if job_id:
        try:
            return _jobs.get(int(job_id))
        except (TypeError, ValueError):
            return None
    jid = getattr(_tl, "jid", None)
    if jid is not None and jid in _jobs:
        return _jobs[jid]
    for jid in reversed(_order):
        if _jobs[jid]["state"] == "confirm":
            return _jobs[jid]
    return _jobs.get(_primary) or _IDLE_JOB


def _stage(key, text=None):
    """Name the stage this job is in (one of STAGES), for the queue view.

    Written where the work actually moves — a job that has finished searching
    must not still be listed as searching — and the free-text `stage` the page
    already shows follows it when `text` is given."""
    with _lock:
        _job["stage_key"] = key
        if text:
            _job["stage"] = str(text)

# Bulk import queue (artist / release-group "download all", and any release
# started while the pipeline is already full): the releases that have not
# started yet WAIT here, and _finish() starts the next one — same machinery, no
# second pipeline. The pipeline runs `soulseek_search_concurrency` of them at a
# time, so a release over that ceiling keeps its place in this queue instead of
# being refused, and starts by itself the moment one of the running ones
# finishes.
#
# An item is the whole set of start_job() arguments plus a queue ticket: the
# item IS the call, so a waiting release starts (or is cancelled) exactly as it
# was asked for — a MusicBrainz release, a release group, a whole artist, a
# peer's folder.
#
# Reentrant: enqueue() holds it while calling _start_next → start_job(), which
# reports the new job through job_state() → queued() → this same lock. With a
# plain Lock that was a self-deadlock — EVERY enqueue (the release/release-group
# /artist Auto-import button) hung the request thread forever, holding
# _queue_lock so no later job could ever start. The same trap server.wishes
# documents.
_queue = []
_queue_ticket = 0
_queue_lock = threading.RLock()


def _queue_key(item):
    """The STABLE id a waiting release is named by in the queue.

    Its release id when it has one, else the ticket it was given when it was
    queued. Stable is the whole point: the queue view re-reads this list every
    second and lets the user select rows BY this id, so an index (`#0`, `#1` …)
    would move under their finger the moment an item ahead of it started."""
    ticket = item.get("ticket")
    return (_release_key(item.get("release_mbid"), item.get("release"))
            or (f"#{ticket}" if ticket is not None else ""))


def _queue_item(**kwargs):
    """One waiting release: start_job's own arguments plus its queue ticket."""
    global _queue_ticket
    _queue_ticket += 1
    return dict(kwargs, ticket=_queue_ticket)


def _queue_item_label(item):
    """What a waiting release's queue row is called: the release it is about, or
    the peer folder a manual grab named (which has no release to name)."""
    release = item.get("release") or {}
    title = str(release.get("title") or "")
    artists = release.get("artists") or []
    artist = (str(artists[0].get("name") or "")
              if artists and isinstance(artists[0], dict) else "")
    if artist and title:
        return f"{artist} — {title}"
    if title or artist:
        return title or artist
    target = str(item.get("target_dir") or "").replace("\\", "/").rstrip("/")
    user = str(item.get("username") or "")
    folder = target.split("/")[-1] if target else ""
    if user or folder:
        return f"{user} · {folder}".strip(" ·")
    return str(item.get("release_mbid") or "")


def _append_waiting(item):
    """Put `item` at the BACK of the waiting queue, unless its release is
    already waiting there or already running; returns why not ("" when queued).

    The running ids are read BEFORE taking the queue lock: the two module locks
    are taken one at a time everywhere (see job_state()/cancel()), never
    nested."""
    key = _release_key(item.get("release_mbid"), item.get("release"))
    running = _running_keys()
    with _queue_lock:
        if not key:
            _queue.append(item)
        elif key in running:
            return "already being imported"
        elif any(_release_key(i.get("release_mbid"), i.get("release")) == key
                 for i in _queue):
            return "already queued"
        else:
            _queue.append(item)
    return ""


def _start_next(started=None):
    """Fill the pipeline: start queued releases while there is capacity.

    Popping under the queue lock and STARTING without it keeps the two module
    locks from nesting in either direction (see job_state()/cancel()), which is
    what makes concurrent jobs safe to start from a job's own finish path.

    Called from enqueue() AND from _finish(), so a release that finishes always
    pulls the queue: a waiter never has to wait for the next user action to
    start. `started` collects the job ids it launched — start_job uses it to
    tell whether the release it had just queued started after all."""
    while True:
        with _queue_lock:
            if not _queue:
                return
            item = _queue[0]
        with _lock:
            if len(_active_locked()) >= concurrency():
                return
        with _queue_lock:
            if not _queue or _queue[0] is not item:
                continue          # another thread took it
            _queue.pop(0)
        # The ticket is THIS queue's bookkeeping (the row's stable id), never
        # part of the call start_job() takes.
        res = start_job(**{k: v for k, v in item.items() if k != "ticket"})
        if res.get("ok"):
            if started is not None and (res.get("job") or {}).get("id"):
                started.append(res["job"]["id"])
            continue
        err = str(res.get("error") or "")
        if res.get("transient") and ("already running" in err
                                     or "being imported" in err
                                     or "already queued" in err):
            # A copy of this release is ALREADY in hand: another job is importing
            # it right now (a manual run overtook the queue), or another thread
            # queued it a moment ago. This copy has nothing left to do, and
            # keeping it would download the same album a second time once that
            # job settles. Drop it and carry on with the rest.
            _log(f"queue: dropped {item.get('release_mbid') or 'item'} — {err}")
            continue
        if res.get("transient"):
            # Any other transient refusal is the pipeline being busy in a way
            # that ends on its own, and the item has not failed: it goes back to
            # the head of the queue and the next finish pulls it again.
            with _queue_lock:
                if item not in _queue:
                    _queue.insert(0, item)
            _log(f"queue: waiting on {item.get('release_mbid') or 'item'}"
                 f" — {err}")
            return
        _log(f"queue: dropped {item.get('release_mbid') or item.get('target_dir') or 'item'}"
             f" — {err or 'could not start'}")


def _release_key(release_mbid, release=None):
    """The identity a release is deduped by: its MusicBrainz id, whether it was
    handed in as an mbid or inside a resolved release dict."""
    return str(release_mbid or (release or {}).get("id") or "").strip().lower()


def _running_keys():
    """Release ids the pipeline is working on right now.

    A SET: three jobs can be in flight, and a release being searched is just as
    much "already being imported" as one that is downloading."""
    with _lock:
        return {_release_key((j.get("release") or {}).get("id"))
                for j in _active_locked()} - {""}


def _queued_keys():
    """Release ids already waiting in the bulk queue."""
    with _queue_lock:
        return {_release_key(i.get("release_mbid"), i.get("release"))
                for i in _queue} - {""}


def enqueue(release_mbid=None, release=None, queries=None, kind=None, mode=None):
    """Queue one release for auto-import; starts it immediately when there is
    capacity.

    kind/mode describe an ID the HTTP route queued BEFORE resolving it (a
    release group or a whole artist): the job does that lookup — see _run.

    The same release id is never queued twice, and never while it is being
    imported: the "download all" routes hand over a target list that can
    repeat (an artist page whose groups share an edition, a release already
    queued behind it), and a second copy of the same id just downloads and
    imports the same album again. A duplicate is logged with its reason and the
    live queue depth is returned.

    Returns the queue depth (0 when the release started right away, more when
    it is waiting behind the releases already running)."""
    item = _queue_item(release_mbid=release_mbid, release=release,
                       queries=queries, kind=kind, mode=mode)
    dup = _append_waiting(item)
    if dup:
        _log(f"{release_mbid or _queue_key(item)}: {dup} — not queued again.")
        with _queue_lock:
            return len(_queue)
    _start_next()
    with _queue_lock:
        # our item is gone once it started → depth 0
        return len(_queue) if any(x is item for x in _queue) else 0


def queued():
    """Pending bulk-import releases (a running job excluded), in queue order.

    Each entry carries its `key` — the STABLE id its row is named by and the
    selection is keyed by (the release id, or `#<ticket>` for a grab that has
    none) — its 1-based `position` in the queue, and the `release` block it was
    queued with, so a row that has not started still names what it is about."""
    with _queue_lock:
        return [{"release_mbid": i.get("release_mbid"),
                 "key": _queue_key(i),
                 "release": i.get("release"),
                 "label": _queue_item_label(i),
                 "wish_id": i.get("wish_id"),
                 "source": i.get("source") or "",
                 "position": n + 1}
                for n, i in enumerate(_queue)]


def drop_queued(ref):
    """Take one waiting release out of the queue, by its queue key; True when it
    was there.

    The queue view's cancel for a row that has not started yet, keyed the same
    way the row is (`queued()`'s `key`: the release id, or `#<ticket>`). A
    release that already started is NOT in this queue and answers False:
    stopping that is cancel(job_id), which the caller does instead."""
    key = str(ref or "")
    with _queue_lock:
        for index, item in enumerate(_queue):
            if key and key == _queue_key(item):
                _queue.pop(index)
                return True
    return False


def clear_queued():
    """Empty the waiting queue — "Clear all" on the Soulseek queue.

    Everything queued or waiting goes in one press, and NOTHING else does: a
    RUNNING release is untouched (that is cancel(job_id), which stops its
    transfers), no settled row and no library album is affected. Answers the
    keys it removed, in queue order, so the caller can say what went."""
    with _queue_lock:
        keys = [_queue_key(i) for i in _queue]
        del _queue[:]
    return {"cleared": len(keys), "keys": keys}


def cancel_rows(ids):
    """Cancel exactly the queue rows named, and say which ones they were.

    `pipeline:<key>` is a release still WAITING — it is dropped before it ever
    starts, so it never downloads a byte. `job:<id>` is a running one, stopped
    the way cancel() stops it. Returns (cancelled, missed): a row that had
    already started, had already finished, or was never there is reported as
    missed instead of being counted as cancelled — a selection must act on
    exactly the rows it was given, and say how many that was."""
    done, missed = [], []
    for raw in ids or []:
        item_id = str(raw or "").strip()
        kind, _, ref = item_id.partition(":")
        if kind == "pipeline" and ref:
            (done if drop_queued(ref) else missed).append(item_id)
        elif kind == "job" and ref:
            try:
                jid = int(ref)
            except ValueError:
                missed.append(item_id)
                continue
            (done if cancel(jid) else missed).append(item_id)
        else:
            missed.append(item_id)
    return done, missed



def queued_wish_ids():
    """Wish ids whose acquisition is sitting in the waiting queue right now.

    The queue holds the whole start_job call, so a wish that had to wait for a
    free slot is still that wish's acquisition — the wishes worker reads this
    (server.wishes_worker._live_wish_ids) so it neither starts a second job for
    the same wish nor reports the wish as idle while it is really in line."""
    out = set()
    with _queue_lock:
        for item in _queue:
            try:
                if item.get("wish_id"):
                    out.add(int(item["wish_id"]))
            except (TypeError, ValueError):
                continue
    return out


def _job_search_done():
    """Clear the live search progress once a query has been scored."""
    with _lock:
        _job["search"] = None


def _job_search_progress(query, res):
    """Publish what the search stage is doing: which query, how many peers and
    files have answered.

    Deliberately NO countdown. The window is a ceiling that a usable candidate
    ends early, so a timer readout promised a duration the search does not
    serve — the UI shows the live response counts and an indeterminate
    "searching" instead, which is honest whether the wait ends in one second or
    in the full window."""
    with _lock:
        _job["search"] = {
            "query": query,
            "state": str(res.get("state") or ""),
            "responses": int(res.get("responseCount") or 0),
            "files": int(res.get("fileCount") or 0),
        }


def _job_progress(payload):
    """Publish (or clear, with None) the live download block of the job."""
    with _lock:
        _job["progress"] = payload


def job_state(job_id=None):
    """One job's state — the calling context's job, else `job_id`, else the
    primary one — plus the bulk queue behind it.

    The queue is read OUTSIDE `_lock`: `_queue_lock` and `_lock` are never held
    at the same time anywhere in this module (see _start_next), so no lock
    order exists for two threads to disagree about."""
    with _lock:
        job = _jobs.get(job_id) if job_id else _pick()
        st = _published(job or _IDLE_JOB)
        st["running"] = len(_active_locked())
        st["concurrency"] = concurrency()
        st["jobs"] = [_published(_jobs[j]) for j in list(_order)
                      if _jobs[j]["state"] != "idle"]
    st["queue"] = queued()
    return st


def jobs():
    """Every job the registry holds, oldest first — the queue view's rows.

    Unlike job_state() this is not "the" job: three downloads can be in flight
    and the page shows all of them. Idle placeholders are left out — a row with
    nothing behind it is noise."""
    with _lock:
        return [_published(_jobs[j]) for j in list(_order)
                if _jobs[j]["state"] != "idle"]


def confirm(accept, job_id=None):
    """Answer a job's pending prompt (`job_id`, else the parked/primary one).

    Both prompts a job can park on end here — "only lossy copies found" and
    "no usable results — add to wishes?" — because the waiter is one event
    either way; which question was asked is in job_state()["confirm"]["reason"]
    and the answer is only ever yes/no. Returns False when no prompt is pending
    (the job moved on, or was cancelled) — the caller must not read that as an
    accepted download."""
    with _lock:
        job = _pick(job_id)
        if job is None or job["state"] != "confirm" or job["cancel"]:
            return False
        job["state"] = "running"
        job["confirm"] = None
        job["_answer"]["accept"] = bool(accept)
    job["_event"].set()
    return True


def cancel(job_id=None):
    """Stop one job (`job_id`) — or, with no id, the whole pipeline.

    The queue view cancels a single row: that download's transfers are dropped
    and its thread stops at the next step, leaving the other jobs alone. The
    Auto-import panel's Stop means "stop the pipeline", which with several jobs
    in flight is all of them plus everything still waiting in the bulk queue.
    A job parked on a prompt is woken either way, so its thread does not sit on
    an answer nobody will give."""
    with _lock:
        if job_id:
            job = _pick(job_id)
            targets = [job] if job and job["state"] in ("running", "confirm") else []
        else:
            targets = list(_active_locked())
        for job in targets:
            job["cancel"] = True
            job["_event"].set()
        if targets:
            _log("Cancellation requested — will stop after the current step.")
    if not targets:
        return False
    if not job_id:
        # Stop the bulk queue too: the user pressed stop, so the whole run
        # stops (there is one Stop button, and it is not a per-row cancel).
        # Taken AFTER releasing _lock: the two locks never nest.
        with _queue_lock:
            dropped = len(_queue)
            del _queue[:]
        if dropped:
            with _lock:
                _log(f"Stopped {dropped} queued release(s).")
    return True


def _log(msg):
    with _lock:
        _job["log"].append({"t": time.strftime("%H:%M:%S"), "msg": str(msg)})
        del _job["log"][:-400]  # keep the ring buffer bounded
        if msg:
            _job["stage"] = str(msg)


def _prune_downloads():
    """Drop the empty shells a finished job left in the download dir.

    The job's rejected/cancelled candidates and its imported album all take
    files away; slskd never removes a directory it created, so without this
    the `<user>/<batch id>/<album>` chains pile up. rmdir-only (see
    soulseek.prune_download_dirs) — nothing that still holds a byte goes.

    Only when NO other job is running: slskd creates a transfer's destination
    directories before it writes the first byte, so a sweep here would delete
    the empty tree another job is about to download into."""
    with _lock:
        if _active_locked():
            return
    try:
        from server import soulseek as slsk
        slsk.prune_download_dirs(slsk.download_dir())
    except Exception:
        pass


def _finish(state, result=None):
    with _lock:
        release = dict(_job.get("release") or {})
        # The result is what every surface reads afterwards (the queue row, the
        # notification, the manual retry): the partial bytes this job could not
        # free and, for a failure, its CLASSIFICATION ("not_found" is not
        # something to retry) are added here, in the one place a job settles.
        result = dict(result or {})
        leftovers = list(_job.get("leftovers") or [])
        if leftovers:
            result["leftovers"] = leftovers
        if state == "error" and "outcome" not in result:
            from server import wishes
            result["outcome"] = wishes.outcome_of(result.get("error"))
        _job["state"] = state
        _job["result"] = result
        _job["progress"] = None   # nothing is downloading any more
        _job["ended_at"] = time.time()
        claim = _job.get("_claim")
        _job["_claim"] = None
        if state == "done":
            _job["stage"] = "Done"
            _job["stage_key"] = "completed"
        elif state == "error":
            _job["stage_key"] = "failed"
        else:
            _job["stage_key"] = "cancelled"
    if claim is not None:
        # The album's library folder is free again — BEFORE the queue refills,
        # so a release that has been waiting on this same folder can claim it.
        claim.release()
    _notify_finish(state, result or {}, release)
    _prune_downloads()
    # Bulk import: this release is over, so start the next one in the queue
    # (which may be several, up to the ceiling). A CANCELLED job pulls the
    # queue too: cancelling one row frees its slot exactly like a finish does,
    # and leaving the waiters behind a cancelled row would be the wait that
    # never ends. Only the GLOBAL stop empties the queue (see cancel()), so
    # there is nothing left to start after it.
    _start_next()


def _notify_finish(state, result, release):
    """Announce a settled auto-import job (see server/events.py).

    EVERY terminal state is announced, exactly once, with a `link` that opens
    the thing the notification is about. That is the whole rule:

    * it landed in the library          -> download_done   (/album/…)
    * it only landed in the download
      folder                            -> import_ready    (/import?album=…)
    * nothing usable was found and the
      release went to the wishlist      -> wish_not_found  (/soulseek)
    * the job gave up — an absent or
      refused slskd, a MusicBrainz
      outage, a failed verification     -> download_failed (/soulseek)

    A CANCELLED job says nothing: the user just cancelled it themselves, and a
    frame about their own button press is noise. A job that is merely retried
    (or re-queued behind another) has not settled and says nothing either.

    The BEGINNING is announced separately and much earlier: `download_started`
    goes out from _wait_for_files the moment a candidate's first bytes move.

    Never raises: a notification must not fail the download that earned it.
    """
    if state == "cancelled":
        return
    try:
        from server import events

        artist = ""
        artists = release.get("artists") or []
        if artists and isinstance(artists[0], dict):
            artist = str(artists[0].get("name") or "")
        title = str(release.get("title") or "")
        label = f"{artist} — {title}" if artist and title else (
            title or artist or "Soulseek download")
        mbid = str(release.get("id") or "")
        album_path = str(result.get("album_path") or "")
        staging = str(result.get("staging_path") or "")

        if state == "error":
            # Nothing else on the bus says "your download gave up": without
            # this frame a refused slskd, a dead peer or a verification that
            # failed all looked identical to a job that was still running.
            err = str(result.get("error") or "the download did not finish")
            leftover = [str(p) for p in (result.get("leftovers") or [])]
            body = err
            if leftover:
                body += (f" — {len(leftover)} partial file(s) could not be "
                         f"removed and are still in the download folder")
            events.emit("download_failed", f"Download failed: {label}", body,
                        {"link": "/soulseek", "release_mbid": mbid,
                         "outcome": str(result.get("outcome") or ""),
                         "error": err[:400],
                         "album_path": album_path,
                         "leftovers": leftover[:20]})
            return

        if result.get("wished"):
            # The search found nothing and the release was parked in the wish
            # list. "Kept looking" is not the same outcome as a failure, so it
            # is not reported as one.
            events.emit("wish_not_found", f"Nothing found: {label}",
                        "No usable copy was on the network — the release is in "
                        "your wish list and the worker keeps searching it there.",
                        {"link": "/soulseek", "release_mbid": mbid,
                         "wish_id": result.get("wish_id"), "outcome": "not_found"})
            return

        if result.get("imported"):
            body = "Downloaded and imported into your library."
            # An album that is missing tracks must not read as a whole one.
            # Only the YouTube branch ever sets `error_count` (its per-track
            # failures), so this adds a sentence for it and changes nothing for
            # a path that has no such count.
            missing = int(result.get("error_count") or 0)
            if missing:
                body += (f" {missing} track(s) could not be fetched "
                         f"({result.get('note') or 'see the job log'}).")
            events.emit("download_done", label, body,
                        {"link": f"/album/{quote(album_path, safe='')}" if album_path
                                 else "/soulseek",
                         "release_mbid": mbid, "album_path": album_path,
                         "imported": True,
                         "error_count": missing})
            return

        events.emit("import_ready", label,
                    result.get("error")
                    or "Downloaded — it is in the download folder, ready to import.",
                    {"link": f"/import?album={quote(staging, safe='')}" if staging
                             else "/soulseek",
                     "release_mbid": mbid, "album_path": album_path or staging,
                     "staging_path": staging, "imported": False})
    except Exception:
        traceback.print_exc()


def _notify_download_start(username, files):
    """Announce that a candidate's transfer has actually started moving bytes.

    The outcome frames say how a job ended; until now nothing said that it
    ever began, so a job sitting in a peer's queue for an hour looked exactly
    like one that was downloading (the queue row says "Downloading …" from the
    moment the files were requested). One frame per candidate, thrown the
    first tick a transfer is really in flight.

    Never raises: a notification must not fail the download that earned it.
    """
    try:
        from server import events

        label = str(_job.get("label") or "") or "Soulseek download"
        count = int(files or 0)
        data = {"link": "/soulseek", "username": str(username or ""), "files": count}
        # Where the album is HEADED, when the job already knows: the folder is
        # claimed before a byte is searched for (see _AlbumClaim/_album_claim),
        # so it is known for every release the library can name.
        claim = _job.get("_claim")
        album_path = str(getattr(claim, "path", "") or "")
        if album_path:
            data["album_path"] = album_path
        events.emit("download_started", f"Download started: {label}",
                    f"{count} file(s) from {username}", data)
    except Exception:
        traceback.print_exc()


def _start_once(files):
    """A per-candidate "download started" announcer.

    A CD candidate is fetched in TWO waits — the .log gate, then the album —
    and both are the same candidate's transfers: only the first one that sees
    bytes move may announce it, so the announcer swallows every call after its
    first. `files` is the CANDIDATE's own file count, not the count of the wait
    that happens to speak first: the .log gate fetches one file, and announcing
    the whole album as "1 file(s)" is exactly the kind of wrong number a
    notification must not carry."""
    announced = []

    def announce(username, _wait_files):
        if announced:
            return
        announced.append(True)
        _notify_download_start(username, files)

    return announce


# --------------------------------------------------------------------------- #
# Query building (customizable trait templates)
# --------------------------------------------------------------------------- #
def _norm_text(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip()


# NFKD (above) folds the FULL-WIDTH forms of ASCII punctuation onto their ASCII
# selves — ！ -> !, ／ -> /, １２３ -> 123 — but leaves the typographic forms
# alone, so a title spelled with a curly apostrophe or an en dash must still
# produce the SAME search text as one spelled with ASCII. The quote forms fold
# onto their ASCII selves (which the keep-set below then decides about), the
# dash forms onto "-", and the separators CJK pages write between names (、 ・ ·)
# onto a space.
_SEARCH_FOLD = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-", "\u2212": "-", "\u301c": "-", "\uff5e": "-",
    "\u3001": " ", "\u30fb": " ", "\u00b7": " ",
})

# The punctuation a share folder name DOES carry and a search should keep: the
# hyphen of a catalog number ("WPCL-1234" — mangling it searches for a number
# the peer does not have), the dot and underscore of a file name, and the
# apostrophe and ampersand of a title. Everything else outside letters, digits
# and spaces becomes a SPACE: quotes, brackets, slashes, "! ? *", emoji and the
# like are what a MusicBrainz title has and a share folder name is not allowed
# to — but they separate words ("Album (Remastered)", "Live/2"), and deleting
# them outright would search for "Album" + "Remastered" glued into one term no
# share carries.
_SEARCH_KEEP = "-_.&'"


# A combining mark is an ACCENT when it sits on a Latin letter — "é" is "e"
# plus one, and folding it away is what makes a title's two spellings one
# query — and a LETTER of its own on any other base: NFKD splits the Japanese
# voiced kana the same way ("ぴ" is "ひ" plus the semi-voiced mark), so dropping
# that mark would search for a different artist entirely.
_ACCENT_BASE_RE = re.compile(r"[A-Za-z]")


def _search_text(s):
    """The text slskd should be handed for ONE rendered query.

    soulseek.search POSTs `searchText` verbatim, so a title's punctuation
    reaches the network as typed. Letters and digits of EVERY script are kept —
    a Japanese or Cyrillic title is as searchable as an English one, which is
    why the filename normalisation that deletes CJK (server.discovery.norm) is
    deliberately NOT reused here, and why the accent-folding above stops at the
    Latin letters `_norm_text` folds everywhere. A term that was nothing but
    punctuation ("!!!") would vanish altogether, so the plain normalized text
    is the fallback rather than an empty query.
    """
    text = unicodedata.normalize("NFKD", str(s or ""))
    kept = []
    for ch in text:
        if (unicodedata.combining(ch) and kept
                and _ACCENT_BASE_RE.fullmatch(kept[-1])):
            continue                 # an accent on a Latin letter: folded away
        kept.append(ch)
    # NFC puts the marks that survived back onto their base (ひ + ゜ -> ぴ), so
    # the term is one character per letter again and survives the keep-set.
    text = unicodedata.normalize("NFC", "".join(kept)).translate(_SEARCH_FOLD)
    out = "".join(ch if (ch.isalnum() or ch == " " or ch in _SEARCH_KEEP) else " "
                  for ch in text)
    return re.sub(r"\s+", " ", out).strip() or text


# A release can carry several catalog numbers (one per label/pressing). Each is
# searched as its OWN query — the caller runs every returned query as a
# separate search and merges the responses — but a release with a dozen numbers
# must not flood the network, so the expansion stops here.
_MAX_CATALOG_QUERIES = 4


def _catalog_numbers(release):
    """Every distinct, normalized catalog number of a release, in MB order.

    Older payloads carry only the singular `catalog_number`; it is used as the
    sole entry when the plural key is missing or empty.
    """
    raw = release.get("catalog_numbers") or [release.get("catalog_number", "")]
    out = []
    for v in raw:
        v = _norm_text(v)
        if v and v not in out:
            out.append(v)
        if len(out) >= _MAX_CATALOG_QUERIES:
            break
    return out


def _expand_template(tpl, fields, catalogs):
    """Render one template. A template naming `catalognumber` yields one query
    per catalog number (so `["catalognumber"]` on a two-number CD is two
    queries); any other template yields exactly one, as it always has. Every
    rendered query goes through `_search_text`, so the single place a query is
    assembled is also the single place it is cleaned."""
    names = [f.strip().lower() for f in str(tpl).split()]
    if "catalognumber" not in names:
        q = _search_text(" ".join(p for p in (fields.get(f, "") for f in names) if p))
        return [q] if q else []
    out = []
    for cn in catalogs:
        parts = [cn if f == "catalognumber" else fields.get(f, "") for f in names]
        q = _search_text(" ".join(p for p in parts if p))
        if q and q not in out:
            out.append(q)
    return out


# --------------------------------------------------------------------------- #
# Which templates search which kind of release
# --------------------------------------------------------------------------- #
# The one medium in mlo.tagtext.MEDIA_VALUES that is not a physical carrier: a
# release made ENTIRELY of it has no pressing trait to search by, and the broad
# artist/album/year wording is all it has. Everything else the vocabulary names
# (CD, CD-R, SHM-CD, Blu-spec CD, Vinyl, Cassette, SACD, DVD-Audio, …) IS a
# pressing, and a pressing is searched by what identifies THAT pressing.
_DIGITAL_MEDIA = "Digital Media"

# A CD is physical like every other pressing, but it owns a settings key of its
# own — the one every CD was searched with until the physical key existed. A
# config that really sets it keeps using it (`_templates_for`).
_CD_QUERIES_KEY = "soulseek_auto_cd_queries"
_PHYSICAL_QUERIES_KEY = "soulseek_auto_physical_queries"
_DIGITAL_QUERIES_KEY = "soulseek_auto_digital_queries"


def _is_digital(release):
    """True when every STATED medium of the release is Digital Media.

    mlo.release_choice.media_formats reads the format out of whichever payload
    the caller holds (a browse row, a normalized row, a full release lookup),
    so the split holds on every path into the job. An empty or unknown medium
    list is NOT digital: a release whose format nobody stated must not be
    handed the broad query a real digital release earns.
    """
    formats = [str(f).strip() for f in media_formats(release)]
    return bool(formats) and all(f == _DIGITAL_MEDIA for f in formats)


# Which network fetches a release — the two names `acquisition_route` answers
# with. `soulseek` is the default and the ONLY answer for anything that is not
# a Digital Media music-video release, which is what keeps an audio release (and
# a music video on a disc) on exactly the path it had before YouTube was an
# option.
_ROUTE_SOULSEEK = "soulseek"
_ROUTE_YOUTUBE = "youtube"


def video_tracks(release):
    """The release's own recordings that MusicBrainz states are VIDEOS.

    server.integrations.release_lookup carries the recording's `video` flag
    into every entry of `media`; a payload that states nothing (a browse row, a
    hand-built dict, a release with no track list) answers [] — an unstated
    medium never makes a release a video one.
    """
    out = []
    media = release.get("media") if isinstance(release, dict) else None
    if not isinstance(media, list):
        return out
    for entry in media:
        if not isinstance(entry, dict):
            continue
        if entry.get("video"):
            out.append(entry)
            continue
        # A payload whose `media` is a list of DISCS (`media[].tracks[]`) is
        # read too: the flag lands wherever the payload puts the recordings.
        for track in (entry.get("tracks") or ()):
            if isinstance(track, dict) and track.get("video"):
                out.append(track)
    return out


def acquisition_route(release):
    """Which network fetches *release*: "soulseek" or "youtube".

    THE ROUTING RULE, in one place, read where the release and its medium are
    both in hand (server.soulseek_auto._run, before any search):

    * a VIDEO release — one whose recordings MusicBrainz states are videos
      (`video_tracks`) — published as DIGITAL MEDIA is fetched from YouTube:
      those uploads are not folders on the Soulseek network, so a search for
      one would only ever spend the user's time and end in "nothing found";
    * a music video on a DISC (DVD / Blu-ray / VHS / Video CD — the formats
      mlo.release_choice.is_video_format classifies) is a folder like any
      pressing and keeps the Soulseek path, byte for byte;
    * an AUDIO release is untouched: its recordings are not videos, so it is
      never routed anywhere but Soulseek, digital medium or not.

    A payload that cannot answer (no release yet, no medium, no track list)
    answers "soulseek": the caller's job then behaves exactly as every job did
    before this routing existed.
    """
    if not isinstance(release, dict) or not release:
        return _ROUTE_SOULSEEK
    if not video_tracks(release):
        return _ROUTE_SOULSEEK
    if video_formats(release):
        return _ROUTE_SOULSEEK
    return _ROUTE_YOUTUBE if _is_digital(release) else _ROUTE_SOULSEEK


def _templates(cfg, key, default):
    """One configured template list: a ";"-separated string is accepted (the
    settings UI edits the list as one line), blanks are dropped, and an unset
    or empty value falls back to `default` ([] when it has none)."""
    value = cfg.get(key)
    if isinstance(value, str):
        value = [t for t in value.split(";") if t.strip()]
    if not isinstance(value, (list, tuple)):
        return list(default or [])
    out = [str(t).strip() for t in value if str(t).strip()]
    return out or list(default or [])


def _templates_for(cfg, digital, is_cd):
    """The configured templates this release's kind is searched with.

    Digital media — and only digital media — keeps the broad wording: it has no
    pressing trait. A physical release is searched by the traits unique to its
    pressing (`soulseek_auto_physical_queries`: catalog number and barcode),
    except a CD whose `soulseek_auto_cd_queries` a config actually sets: that
    key is what a CD was searched with before this one existed, and a user who
    widened or narrowed it made a decision the new default must not silently
    overwrite. Its SHIPPED default (the catalog number alone) is not such a
    decision, so an untouched install follows the physical default.
    """
    if digital:
        return _templates(cfg, _DIGITAL_QUERIES_KEY, ["artist album year"])
    if is_cd:
        cd = _templates(cfg, _CD_QUERIES_KEY, None)
        if cd and cd != list(DEFAULT_CONFIG[_CD_QUERIES_KEY]):
            return cd
    return _templates(cfg, _PHYSICAL_QUERIES_KEY, ["catalognumber", "barcode"])


def _pressing_fallback(fields, digital):
    """The one query a physical pressing with no catalog number/barcode gets.

    NEVER the artist/title wording: that is the query that asks the network for
    every other pressing of the same album, which is what made a CD job take a
    WEB rip instead of the pressing it was asked for. The label, the country
    and the year are what a folder of THAT pressing carries. A release stating
    neither a label nor a country has nothing left to identify it by, so it
    gets no query at all — the caller reports "nothing to search by" instead of
    searching blind.
    """
    if digital:
        return []
    used = [(name, fields[name]) for name in ("label", "country", "year")
            if fields[name]]
    if not any(fields[name] for name in ("label", "country")):
        return []
    q = _search_text(" ".join(v for _n, v in used))
    if not q:
        return []
    _log("this pressing has no catalog number or barcode — searching by its "
         f"{' + '.join(n for n, _v in used)} (“{q}”) instead")
    return [q]


# --------------------------------------------------------------------------- #
# MusicBrainz aliases: the other name a release is filed under
# --------------------------------------------------------------------------- #
# The scripts whose names are not Latin (CJK, Hangul, Kana, Cyrillic, Greek,
# Hebrew, Arabic, Devanagari, Thai) and the locales whose own names ARE Latin.
# This is the beets plugin's own pair — server/beets/mloplugin._NON_LATIN_RE /
# _LATIN_LOCALES, which ask the same question for the TAGS — restated here
# because that module imports beets, an OPTIONAL external tool (it is not in
# server/requirements.txt; fetchdeps installs it at runtime), while the search
# side has to work without it. The two rules answer ONE question — "can a
# MusicBrainz alias translate this name into the reader's locale?" — so they
# must stay equal.
_NON_LATIN_RE = re.compile(
    "[\u0370-\u03ff\u0400-\u04ff\u0590-\u05ff\u0600-\u06ff\u0900-\u097f"
    "\u0e00-\u0e7f\u1100-\u11ff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
    "\uf900-\ufaff\uac00-\ud7af]")
_LATIN_LOCALES = {
    "en", "de", "fr", "es", "it", "pt", "nl", "sv", "no", "da", "fi", "is",
    "pl", "cs", "sk", "hu", "ro", "tr", "vi", "id", "ms", "tl", "hr", "sl",
    "lt", "lv", "et", "ca", "gl", "eu", "af", "sq",
}


def _alias_needed(value, locale):
    """False when a locale alias lookup cannot change `value` anyway.

    A name already written in the reader's locale's script has nothing to
    translate, and the lookup costs one of MusicBrainz' rate-limited requests —
    so a Latin-script library asks MusicBrainz nothing at all.
    """
    if not value:
        return False
    if (str(locale or "en").split("-")[0].lower() in _LATIN_LOCALES
            and not _NON_LATIN_RE.search(str(value))):
        return False
    return True


def _mb_aliases(entity, mbid):
    """MusicBrainz' aliases for one entity — [] when there is nothing to ask,
    when MusicBrainz holds none, or when it cannot answer.

    Through `integrations.mb_get_cached`: cached, single-flight and rate-limited
    like every other MusicBrainz read, so a release page showing the same entity
    costs no second request. An alias never fails a job — the configured
    templates are already in hand.
    """
    if not mbid:
        return []
    from server import integrations as intg
    try:
        data = intg.mb_get_cached(f"{entity}/{mbid}", {"inc": "aliases", "fmt": "json"})
    except Exception as e:
        _log(f"  MusicBrainz alias lookup failed for {entity}/{mbid}: {e}")
        return []
    return [a for a in ((data or {}).get("aliases") or []) if isinstance(a, dict)]


def _alias_name(aliases, value, locale):
    """The alias that translates `value` for a reader in `locale`, "" if none.

    The locale's own aliases win, a primary one first (Picard's own
    preference). When that locale has none and the value is written in another
    script, any Latin-script alias beats nothing: an artist's page carries the
    romanized name under whatever locale tag the editor happened to use, "en"
    as often as not.
    """
    want = str(locale or "").strip().lower()
    raw = str(value or "").strip()

    def in_locale(only_primary):
        out = []
        for a in aliases:
            name = str(a.get("name") or "").strip()
            if not name or name == raw:
                continue
            if str(a.get("locale") or "").strip().lower() != want:
                continue
            if only_primary and not a.get("primary"):
                continue
            out.append(name)
        return out

    for group in (in_locale(True), in_locale(False)):
        if group:
            return group[0]
    if raw and _NON_LATIN_RE.search(raw):
        for a in aliases:
            name = str(a.get("name") or "").strip()
            if name and not _NON_LATIN_RE.search(name):
                return name
    return ""


def _alias_queries(release, cfg, templates, fields, catalogs, queries):
    """Extra queries rendered from the release's MusicBrainz aliases.

    A release MusicBrainz files under a translated name is unfindable on
    Soulseek under the name the release itself carries: the folders on the
    network are named with the ALIAS (a Japanese artist's romanized name, say)
    while every configured template looks for the original. Two lookups answer
    it, both through the cached client — the artist's aliases (`artists[].mbid`)
    and the release group's (`release_group_id`) — and a name already in the
    reader's script costs no request at all (`_alias_needed`).

    The extra queries are the SAME templates rendered with the alias in place of
    the artist, and of the album when the release group carries an alias title;
    they are capped at `_MAX_CATALOG_QUERIES` like the catalog expansion, and
    they are built BEFORE the job offers the wish list — a wish replays the
    queries it was stored with (server/wishes_worker), so an alias query that
    arrived later would never be searched again.
    """
    locale = str(cfg.get("locale") or "en").strip()
    artists = release.get("artists") or [{}]
    artist_mbid = (artists[0].get("mbid") if artists else "") or ""
    alias_artist = ""
    if _alias_needed(fields["artist"], locale):
        alias_artist = _alias_name(_mb_aliases("artist", artist_mbid),
                                   fields["artist"], locale)
    alias_album = ""
    if _alias_needed(fields["album"], locale):
        alias_album = _alias_name(
            _mb_aliases("release-group", release.get("release_group_id")),
            fields["album"], locale)
    if not (alias_artist or alias_album):
        return []
    named = alias_artist or fields["artist"]
    _log(f"MusicBrainz also files this release under “{named}”"
         f"{' / “' + alias_album + '”' if alias_album else ''} ({locale}) — "
         f"searching that name too")
    alt = dict(fields)
    if alias_artist:
        alt["artist"] = alias_artist
    if alias_album:
        alt["album"] = alias_album
    out = []
    for tpl in templates:
        for q in _expand_template(tpl, alt, catalogs):
            if q not in queries and q not in out:
                out.append(q)
                if len(out) >= _MAX_CATALOG_QUERIES:
                    return out
    return out


def release_queries(release, cfg, templates=None):
    """Search queries from the release's traits, in configured priority order.

    Each template is a space-separated list of field names; supported fields:
    artist, album, year, date, country, catalognumber, barcode, label.

    DIGITAL media (every stated medium is "Digital Media") is searched by
    `soulseek_auto_digital_queries` — "artist album year" by default — because
    it has no pressing trait to be identified by. A PHYSICAL release is searched
    by `soulseek_auto_physical_queries` (catalog number and barcode by default),
    the traits that name the exact pressing, and falls back to its label and
    country when MusicBrainz states neither of the first two — never to an
    artist/title query, which asks the network for every other pressing of the
    same album.

    A release carrying several catalog numbers (one per label/pressing) yields
    one query per number for every template that names `catalognumber`, capped
    at `_MAX_CATALOG_QUERIES` — the same cap the MusicBrainz alias queries
    (`_alias_queries`) stop at.

    `templates` renders an explicit set instead of the configured one: the job's
    own stored queries, and its broad second pass. Those are queries a caller
    already decided, so they are rendered exactly as given and expand no
    aliases.
    """
    formats = [str(f).strip() for f in media_formats(release)]
    digital = _is_digital(release)
    explicit = templates is not None
    if explicit:
        if isinstance(templates, str):
            templates = [templates]
        templates = [str(t).strip() for t in templates if str(t).strip()]
    if not explicit:
        templates = _templates_for(cfg, digital, "CD" in formats)

    # The RAW spellings: `_expand_template` is the one place a query is
    # assembled, and `_search_text` normalizes there — running `_norm_text`
    # first would fold Japanese voicing marks ("ぴ" -> "ひ") long before the
    # sanitizer could tell an accent from a letter.
    fields = {
        "artist": str((release.get("artists") or [{}])[0].get("name") or ""),
        "album": str(release.get("title") or ""),
        "year": str(release.get("date") or "")[:4],
        "date": str(release.get("date") or ""),
        "country": str(release.get("country") or ""),
        "barcode": str(release.get("barcode") or ""),
        "label": str(release.get("label") or ""),
    }
    catalogs = _catalog_numbers(release)
    queries = []
    for tpl in templates:
        for q in _expand_template(tpl, fields, catalogs):
            if q not in queries:
                queries.append(q)
    if not queries:
        queries.extend(_pressing_fallback(fields, digital))
    if not explicit:
        queries.extend(_alias_queries(release, cfg, templates, fields, catalogs, queries))
    return queries


# --------------------------------------------------------------------------- #
# Candidate folders from search results
# --------------------------------------------------------------------------- #
_AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".oga", ".opus",
               ".wav", ".wma", ".aiff", ".aif", ".alac", ".ape", ".wv", ".shn",
               ".tta", ".mpc", ".mp2", ".mka", ".dsf", ".dff"}
# The library's own definition of a track — audio PLUS the music-video
# containers (mlo.paths.LIB_AUDIO_EXTS, the set server.main.is_audio_file and
# the organizer/graders read). Stamping is the one place that wants it: a
# music-video album's files must be stamped like any other album's, while the
# SEARCH above keeps asking the network for audio (a peer folder is not made a
# candidate by holding videos — see find_candidates).
_LIB_AUDIO_EXTS = set(LIB_AUDIO_EXTS)
# Lossless codecs a candidate folder can hold; everything else the network
# offers (mp3, m4a/aac, ogg, opus, wma...) is lossy. Auto-import prefers
# these folders, and only downloads lossy after the user says so.
# `.m4a`/`.mp4` are listed under both — the container holds ALAC or AAC and
# only a probe of the downloaded file can tell them apart (mlo.flac.is_alac).
_LOSSLESS_EXTS = {".flac", ".wav", ".aiff", ".aif", ".alac", ".ape", ".wv",
                  ".shn", ".tta", ".dsf", ".dff"}
# slskd only hands back a search's responses once the search has ENDED (while
# it runs the API reports counts only), so a poll window that closes as the
# search's own timeout fires returns an empty result set for a search that
# saw hundreds of hits. Poll past the requested window until slskd reports a
# terminal state.
_SEARCH_GRACE_S = 45.0

_LEAD_NUM_RE = re.compile(r"^(\d{1,2})(?:\s*[-._]|\s+|\))\s*(\d{1,3})(?:\s*[-._]|\s+|\)|$)")  # 1-02 style
_TRACKNO_RE = re.compile(r"^(\d{1,3})(?:\s*[-._]|\s+|\)|$)")
# "Album - 01 - Title.flac": the flat share layout, track number mid-name.
_MID_NUM_RE = re.compile(r"[-_.]\s*(\d{1,3})\s*[-_.]\s*\S")
_TAIL_NUM_RE = re.compile(r"(\d{1,3})\s*$")   # "track01"
# The disc token a folder or file name can carry ("CD1", "Disc 2", "Disk1",
# "Volume 1"), ignoring the decoration shares wrap their folders in
# ("CD1 [FLAC]").
_DISC_NUM_RE = re.compile(r"(?:^|[^A-Za-z0-9])(?:cd|disc|disk|dvd|bd|volume|vol)"
                          r"\s*[-_.]?\s*(\d{1,2})(?![0-9])", re.IGNORECASE)
_DISC_DIR_RE = re.compile(r"(?:cd|disc|disk|dvd|bd|volume|vol)\s*[-_.]?\s*\d{1,2}",
                          re.IGNORECASE)
_DECOR_RE = re.compile(r"[\s._-]*[\(\[]([^\)\]]*)[\)\]][\s._-]*$")
_LEAD_DISC_RE = re.compile(r"^(\d{1,2})\s*-\s*\d{1,3}(?:\D|$)")     # 1-03 style
_BRACKET_DISC_RE = re.compile(r"^[\(\[]\s*(\d{1,2})\s*[\)\]]\s*")   # (1) 01 - Title


def _strip_decor(name):
    """"CD1 [FLAC]" -> "CD1"; "Album (1994)" -> "Album"."""
    name = str(name or "")
    while True:
        m = _DECOR_RE.search(name)
        if not m:
            return name.strip()
        name = name[:m.start()]


def _is_disc_dir(name):
    """True when a directory name is a disc folder — "CD1", "CD 1", "Disk1",
    "Disk 2", "Volume 1", "CD1 [FLAC]" — rather than an album folder that
    merely carries a decoration ("Album [FLAC]", "Album (1994)")."""
    bare = _strip_decor(name)
    return bool(bare) and _DISC_DIR_RE.fullmatch(bare) is not None


def _disc_number(name):
    """Disc number a folder or file NAME claims ("CD1", "Disc 2 [FLAC]",
    "1-03 rip", "(2) 01 x"), or None when the name says nothing."""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    stem = _strip_decor(os.path.splitext(base)[0] or base)
    m = _DISC_NUM_RE.search(stem)
    if m:
        return int(m.group(1))
    m = _LEAD_DISC_RE.match(stem)
    if m:
        return int(m.group(1))
    m = _BRACKET_DISC_RE.match(stem)
    return int(m.group(1)) if m else None


def _album_root(path):
    """Fold per-disc subfolders (…/CD1/01 - x.flac, …/Disc 1/…,
    …/Disk 2/…, …/CD1 [FLAC]/…) into one album root."""
    d = os.path.dirname(path.replace("\\", "/"))
    parts = d.rstrip("/").split("/")
    if len(parts) >= 2 and _is_disc_dir(parts[-1]):
        d = "/".join(parts[:-1])
    return d if d.endswith("/") else d + "/"


def _parent_dir(root):
    """The directory above an album root (…/Album/{Log+Cue,Music} → …/Album/);
    "" at the top of the share."""
    p = root.replace("\\", "/").rstrip("/")
    parent = os.path.dirname(p)
    if not parent:
        return ""
    return parent if parent.endswith("/") else parent + "/"


def _ancestors(root, levels=2):
    """The root itself, then up to `levels` directories above it — the
    covering directories a release can hide in (per-track folders, a
    Log+Cue/Music split)."""
    out = [root]
    p = root
    for _ in range(levels):
        parent = _parent_dir(p)
        if not parent or parent == p:
            break
        out.append(parent)
        p = parent
    return out


def _named_disc(path):
    """Disc a folder/file path explicitly names — its folder first, then its
    own name (…/CD2/01 - x.flac, …/CD1.log, …/1-02 x.flac) — or None."""
    p = str(path).replace("\\", "/")
    return _disc_number(os.path.dirname(p)) or _disc_number(os.path.basename(p))


def _disc_of(path):
    """Disc a file belongs to (1 when nothing names one)."""
    return _named_disc(path) or 1


def _parse_trackno(path):
    """(disc, position) from a filename; the folder names the disc when the
    file name does not ("01 - x.flac" inside …/CD2/ is disc 2, track 1)."""
    p = str(path).replace("\\", "/")
    disc = _disc_of(p)
    base = os.path.basename(p)
    m = _BRACKET_DISC_RE.match(base)          # "(1) 01 - Title.flac"
    if m:
        disc = int(m.group(1))
        base = base[m.end():]
    m = _LEAD_NUM_RE.match(base)              # "1-02 Title" names the disc
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _TRACKNO_RE.match(base)               # "01 Title", "01.Title", "01_Title"
    if m:
        return disc, int(m.group(1))
    m = _MID_NUM_RE.search(os.path.splitext(base)[0])    # "Album - 01 - Title"
    if m:
        return disc, int(m.group(1))
    m = _TAIL_NUM_RE.search(os.path.splitext(base)[0])   # "track01.flac"
    return (disc, int(m.group(1))) if m else (disc, None)


def _rank(c):
    """Candidate order: lossless first, then the score, then the FASTEST peer,
    its queue, and finally the peer+folder names so the order is total.

    Lossless outranks everything: a lossy folder that scores perfectly is still
    worse than a lossless one that only just matched — the download is the
    irreversible part. The score is next, and it already carries the peer's
    speed (find_candidates adds up to 4.5 points for it, deliberately under one
    matched track), so a fast wrong folder can never buy its way past a slower
    complete one.

    The speed key is the user's "download from the fastest good source first":
    it decides between folders the score rates equal (a verdict that has to come
    from the search response, because two equally scored folders are otherwise
    indistinguishable). `speed` is the folder's SLOWEST file — a folder is only
    as fast as its worst peer (see find_candidates) — and a peer that reported
    none ranks last among equals rather than first. The peer's own queue
    position only breaks a speed tie: _est_timeout/_queue_budget already budget
    that wait from the candidate's own numbers, and a folder that is still the
    best copy tomorrow is worth waiting in line for today.

    The names are the last key so the order is TOTAL and reproducible run to
    run: two candidates the search reports identically (same user, same figures)
    still come back in one fixed order, which is what makes a job's attempts —
    and the tests that pin them — deterministic."""
    return (not c["lossless"], -c["score"], -float(c.get("speed") or 0),
            int(c.get("queue") or 0), str(c.get("username") or ""),
            str(c.get("dir") or ""))


def _dedupe_candidates(candidates):
    """One entry per peer+folder, best-ranked first.

    ONE job scores its candidates from two SEPARATE searches — the configured
    templates and the broader second pass (`_run`) — and find_candidates groups
    within one response set only, so a folder that answered both is scored
    twice. A job that walked both copies would download that folder twice from
    the same peer, and when the first attempt failed it spent a batch slot on
    the very bytes that just failed instead of on a peer that has not been
    tried. The first entry in rank order wins, so the better of the two scores
    is the one that stands."""
    out, seen = [], set()
    for c in candidates:
        key = (str(c.get("username") or "").lower(),
               str(c.get("dir") or "").replace("\\", "/").rstrip("/").lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _take_batch(pool, width):
    """The next batch: up to `width` candidates, never two from one peer.

    Two folders of one peer are two copies of the same album on one machine, so
    asking for both at once buys nothing but bandwidth and one more transfer to
    cancel. The second keeps its place and comes back in a later batch, which is
    where it belongs if the first one fails."""
    batch, rest, seen = [], [], set()
    for c in pool:
        who = str(c.get("username") or "")
        if len(batch) < width and who not in seen:
            seen.add(who)
            batch.append(c)
        else:
            rest.append(c)
    return batch, rest


def _same_folder(a, b):
    """Whether two slskd folder paths name the same directory (case- and
    separator-insensitive). Plain endswith() accepts …/NotAlbum for …/Album."""
    a = str(a or "").replace("\\", "/").rstrip("/").lower()
    b = str(b or "").replace("\\", "/").rstrip("/").lower()
    return a == b


def _under(path, folder):
    """Whether `path` sits inside `folder` (both slskd paths, component-aware:
    …/Album/CD1 is under …/Album, …/NotAlbum is not)."""
    p = str(path or "").replace("\\", "/").lower()
    f = str(folder or "").replace("\\", "/").rstrip("/").lower()
    return bool(f) and p.startswith(f + "/")


def _expected_tracks(release):
    """[{disc, pos, title, length}] from a release's track list."""
    return [{
        "disc": int(t.get("disc") or 1),
        "pos": int(t.get("position") or 0),
        "title": _norm_text(t.get("title") or ""),
        "length": float(t.get("length") or 0) / 1000.0,
    } for t in (release.get("media") or [])]


def _disc_numbers(release):
    """The disc numbers a release expects ([1] when it names none)."""
    return sorted({int(t.get("disc") or 1)
                   for t in (release.get("media") or [])}) or [1]


def _select_logs(logs, discs, root):
    """ONE .log per expected disc, picked deterministically.

    A log that names its disc (CD1 / Disc 2 / Disk 1 / Volume 1, "1-03 …", or
    the folder it sits in) wins for that disc; then a log in the album root;
    then any log that names no disc. Ties break on the shortest path, then
    lexicographically — so a folder holding two logs for one disc enqueues
    exactly one of them, and never a second log for the same disc."""
    picked, used = [], set()
    loose = [l for l in logs if _named_disc(l["file"]) is None]
    root_logs = [l for l in loose
                 if _same_folder(os.path.dirname(l["file"]), root)]
    for d in discs:
        named = [l for l in logs if _named_disc(l["file"]) == d]
        for tier in (named, root_logs, loose):
            fresh = [l for l in tier if l["file"] not in used]
            if fresh:
                pick = min(fresh, key=lambda l: (len(l["file"]), l["file"].lower()))
                used.add(pick["file"])
                picked.append(pick)
                break
    return picked


def _title_in(f, t):
    """Whether the expected track's title shows up in a file's name."""
    if not t["title"]:
        return False
    stem = _norm_text(os.path.splitext(os.path.basename(f["file"]))[0])
    return t["title"] in stem


def _image_rip_audio(audio, cues, expected):
    """The audio files of a disc-image rip ("Album.flac" + "Album.cue",
    "CD1.flac" + "CD1.cue"), or None.

    A share that offers one big file per disc plus its cue carries no
    per-track file names to match, so it is accepted on shape: exactly ONE
    audio file per expected disc — nothing else in the folder — and a cue
    sheet for each of them. Whether that file really is the release is
    settled later by the graded .log and the decode check (see _verify_album).
    ponytail: a search result carries no cue CONTENT, so the cue's TRACK
    count cannot be read here; fetch the cue and count its TRACK rows if a
    wrong-cue image rip ever slips through."""
    discs = sorted({t["disc"] for t in expected})
    if not cues or len(audio) != len(discs):
        return None
    out = []
    for d in discs:
        per_disc = ([f for f in audio if _named_disc(f["file"]) == d]
                    if len(discs) > 1 else list(audio))
        if len(per_disc) != 1:
            return None
        stem = os.path.splitext(os.path.basename(per_disc[0]["file"]))[0]
        if (_TRACKNO_RE.match(stem) or _LEAD_NUM_RE.match(stem)
                or _MID_NUM_RE.search(stem)):
            return None          # a stray track file, not a disc image
        out.append(per_disc[0])
    return out


def find_candidates(results, release, cfg):
    """Score search results against the release track list.

    Returns candidates sorted best-first: [{username, dir, files, audio,
    logs, cues, complete, score, lossless, slot, queue}]. A candidate is
    complete when every expected track (across all discs) has a matching
    file (matched by track number, falling back to normalized title +
    duration) and — for CD releases — every disc carries a .log and .cue.

    Files are grouped per directory, folded per disc (…/CD1/, …/Disc 1/,
    …/CD1 [FLAC]/), and then per covering directory: a group that does not
    carry the release on its own is re-evaluated one directory at a time
    upwards (per-track folders, a Log+Cue/Music split), so the candidate is
    the DEEPEST directory whose file set covers the expected tracklist.
    `files` is what stage 2 downloads: the matched tracks plus the selected
    logs and cues, so a covering root that holds more audio (a flat
    Artist/ folder) never pulls the rest of it in."""
    min_ratio = float(cfg.get("soulseek_auto_complete_ratio", 1.0) or 1.0)
    expected = _expected_tracks(release)
    discs_expected = _disc_numbers(release)
    is_cd = "CD" in (release.get("medium_formats") or [])
    if not expected:
        # matched == len(expected) and matched >= min_ratio * len(expected)
        # are both vacuously true for an empty track list, so any single-file
        # folder "completed" a release that has no tracks at all.
        raise ValueError("the release has no track list — a candidate folder "
                         "cannot be matched against it")

    def ext_of(f):
        return os.path.splitext(f.get("file") or "")[1].lower()

    groups = {}
    for f in results:
        if ext_of(f) not in (_AUDIO_EXTS | {".log", ".cue"}):
            continue
        groups.setdefault((f["username"], _album_root(f["file"])), []).append(f)

    by_user = {}   # username -> {root: files}
    for (username, root), files in groups.items():
        by_user.setdefault(username, {})[root] = files

    def files_under(username, prefix):
        out = []
        for root, flist in by_user[username].items():
            if _same_folder(root, prefix) or _under(root, prefix):
                out.extend(flist)
        return out

    def evaluate(username, root, files):
        audio = [f for f in files if ext_of(f) in _AUDIO_EXTS]
        logs = _select_logs([f for f in files if f["file"].lower().endswith(".log")],
                            discs_expected, root)
        cues = [f for f in files if f["file"].lower().endswith(".cue")]
        if not audio:
            return None
        log_discs = {_disc_of(f["file"]) for f in logs}
        cue_discs = {_disc_of(f["file"]) for f in cues}
        missing_logs = [d for d in discs_expected if d not in log_discs] if is_cd else []
        missing_cues = [d for d in discs_expected if d not in cue_discs] if is_cd else []

        # match expected tracks to files. A file carrying BOTH the track
        # number and the expected title wins over one that only carries the
        # number: a flat Artist/ folder is full of "01 - …" files belonging
        # to the artist's other albums.
        image = _image_rip_audio(audio, cues, expected) if not missing_cues else None
        used = set()
        matched_files = []
        for t in expected:
            by_num = [f for f in audio
                      if id(f) not in used
                      and _parse_trackno(f["file"]) == (t["disc"], t["pos"])]
            hit = next((f for f in by_num if _title_in(f, t)), None)
            if hit is None and by_num:
                hit = by_num[0]
            if hit is None:
                for f in audio:
                    if id(f) in used or not _title_in(f, t):
                        continue
                    dur = float(f.get("duration") or 0)
                    if not t["length"] or not dur or abs(dur - t["length"]) <= 15:
                        hit = f
                        break
            if hit is not None:
                used.add(id(hit))
                matched_files.append(hit)
        matched = len(matched_files)
        if image and matched < len(expected):
            matched_files = image
            matched = len(expected)
        complete = (matched == len(expected)) and not missing_logs and not missing_cues
        acceptable = matched >= min_ratio * len(expected) and not missing_logs and not missing_cues
        if not acceptable:
            return None
        lossless = any(ext_of(f) in _LOSSLESS_EXTS for f in audio)
        slot = bool(files[0].get("slot"))
        queue = min(int(f.get("queue") or 0) for f in files)
        # the peer's advertised upload rate (slskd reports one per response):
        # the SLOWEST figure of the folder, so the wait is never cut short
        speeds = [float(f.get("speed") or 0) for f in files]
        speeds = [s for s in speeds if s > 0]
        # Stage 2 takes the release's own files only: when the covering root
        # holds audio beyond the tracklist (a flat Artist/ folder, the merged
        # parent of a split layout) the rest of the folder stays unbought.
        plans = matched_files if len(matched_files) < len(audio) else audio
        if plans is audio:
            plan_cues = cues
        else:
            plan_dirs = {os.path.dirname(f["file"]) for f in plans}
            plan_cues = [f for f in cues if os.path.dirname(f["file"]) in plan_dirs]
        downloads, seen = [], set()
        for f in plans + logs + plan_cues:
            if f["file"] not in seen:
                seen.add(f["file"])
                downloads.append(f)
        total = sum(int(f.get("size") or 0) for f in downloads)
        # A fast peer both finishes sooner and is likelier to serve the whole
        # folder, so the rate the network advertised decides between otherwise
        # equal folders: the folder's SLOWEST file (a folder is only as fast as
        # its worst peer) earns up to 4.5 points at 1 MiB/s per point —
        # deliberately under one matched track (5), so speed breaks ties
        # between equally complete folders instead of buying an incomplete one.
        speed_mib = (min(speeds) if speeds else 0) / (1024 * 1024)
        score = (5 * matched + 2 * len(logs) + len(cues)
                 + 8 * lossless + 4 * slot - queue / 100.0
                 + min(4.5, speed_mib))
        return {
            "username": username, "dir": root, "files": downloads, "audio": plans,
            "logs": logs, "cues": cues, "matched": matched, "expected": len(expected),
            "complete": complete, "lossless": lossless, "slot": slot,
            "queue": queue, "speed": min(speeds) if speeds else 0,
            "total_size": total, "score": round(score, 1),
        }

    candidates = []
    covered = {}
    for (username, root) in sorted(groups):
        if any(_same_folder(root, r) or _under(root, r)
               for r in covered.get(username, ())):
            continue
        # deepest covering directory first: the group's own root, then up
        for cand_root in _ancestors(root):
            c = evaluate(username, cand_root, files_under(username, cand_root))
            if c:
                candidates.append(c)
                covered.setdefault(username, set()).add(cand_root)
                break

    candidates.sort(key=_rank)
    return candidates


# --------------------------------------------------------------------------- #
# slskd helpers (search, wait, locate local files)
# --------------------------------------------------------------------------- #
_SEARCH_POLL_S = 0.75      # one loop polls EVERY outstanding search this often
_TRANSFER_POLL_S = 1.0     # download poll cadence


def _search_queries(slsk, queries, wait_s, usable=None, response_limit=0):
    """Run every query template AT ONCE and poll them in one loop.

    All templates are POSTed up front, so the wall time is one window
    (`wait_s` + the grace tail) instead of one window per template — up to
    three minutes before the first byte became one minute. The per-request
    `timeout` is passed in MILLISECONDS because slskd counts it that way (see
    soulseek.search), and it is QUIET time: a search only ends when the
    network stops answering. That is why `response_limit` matters — slskd
    serves `/searches/{id}/responses` only once a search has ENDED, so a
    popular album (peers replying for a minute straight) used to hand back
    nothing until the whole ceiling had elapsed even though hundreds of files
    were already counted. The limit ends the search as soon as enough peers
    have answered, which is what makes a good copy start downloading in
    seconds instead of a minute.

    Returns (results, errors, skipped): results = [(query, response DTO)] for
    the searches that reached a terminal state, errors = one line per query
    that failed or never terminated, skipped = how many queries were still
    running when a usable candidate ended the wait.

    How long the wait REALLY was is kept for the caller on this thread
    (`_search_seconds`) — this function is the one place that knows when the
    wait ended, and the number it measures is what the job reports, instead of
    the configured ceiling it used to be mistaken for."""
    started = time.time()
    deadline = started + wait_s + _SEARCH_GRACE_S
    display = " · ".join(queries)
    watch, errors = [], []
    for q in queries:
        try:
            watch.append([slsk.search(q, timeout_ms=int(wait_s * 1000),
                                      response_limit=response_limit), q, None])
        except Exception as e:
            errors.append(f"Soulseek search could not be started for “{q}”: {e}")
    early = False
    while watch and time.time() < deadline:
        for e in watch:              # probe first: no dead sleep before it
            try:
                e[2] = slsk.search_results(e[0])
            except Exception:
                pass
        pending = [e for e in watch if not slsk.is_search_done(e[2] or {})]
        _job_search_progress(display, {
            "state": "InProgress" if pending else "Completed",
            "responseCount": sum(int((e[2] or {}).get("responseCount") or 0) for e in watch),
            "fileCount": sum(int((e[2] or {}).get("fileCount") or 0) for e in watch),
        })
        merged = [f for e in watch for f in ((e[2] or {}).get("responses") or [])]
        if usable is not None and merged and usable(merged):
            early = True
            break
        if not pending:
            break
        time.sleep(_SEARCH_POLL_S)

    results, skipped = [], 0
    for sid, q, res in watch:
        if not slsk.is_search_done(res or {}):
            # Still running when the window closed. Nothing readable will come
            # out of it (responses are served only after a search ends), so it
            # is cancelled rather than left occupying slskd's search slots.
            try:
                slsk.cancel_search(sid)
            except Exception:
                pass
            if early:
                skipped += 1     # in hand already — never waited out
            else:
                errors.append(f"Soulseek search did not finish within "
                              f"{int(wait_s + _SEARCH_GRACE_S)}s for “{q}”")
            continue
        err = slsk.search_error(res)
        if err:
            errors.append(f"Soulseek search failed ({err}) for “{q}”")
        else:
            results.append((q, res))
    _tl.search_seconds = time.time() - started
    return results, errors, skipped


def _search_seconds():
    """How long the last `_search_queries` call on THIS thread really took.

    0.0 when this thread never searched (a browsed folder, a stubbed search),
    which is the truthful answer there: nothing was waited out."""
    return float(getattr(_tl, "search_seconds", 0.0) or 0.0)


def _remote_rel(remote_path):
    """slskd's local relative path for a remote file (see
    _local_download_candidates for where that lands in the download dir).

    Two things the plain "strip the drive letter" version got wrong: the
    leading separator survived ('C:\\Music\\x' -> '/Music/x'), so joining the
    result onto the download dir produced an absolute path OUTSIDE it; and a
    '..' segment escaped the same way. Everything joining this path
    (<downloads>/<user>/<rel>) is later moved or rmtree'd, so it must stay a
    plain relative path.
    """
    p = remote_path.replace("\\", "/")
    if re.match(r"^[A-Za-z]:", p):
        p = p[2:]
    return "/".join(x for x in p.lstrip("/").split("/") if x not in ("", ".", ".."))


def _inside(path, folder):
    """True when `path` is strictly inside `folder` (boundary-aware).

    Same test server/main.py applies to its music-folder guard: a
    startswith() prefix check accepts both '..' paths and siblings that
    merely share a name prefix — and this one gates an rmtree()."""
    try:
        p = os.path.abspath(os.path.normpath(path))
        f = os.path.abspath(os.path.normpath(folder))
    except (OSError, ValueError, TypeError):
        return False
    if p == f:
        return False
    if os.path.normcase(os.path.splitdrive(p)[0]) != os.path.normcase(os.path.splitdrive(f)[0]):
        return False
    try:
        return os.path.normcase(os.path.commonpath([p, f])) == os.path.normcase(f)
    except (OSError, ValueError, TypeError):
        return False


def _leaf_of(remote):
    """The remote file's own album folder name ("" at the top of the share) —
    the directory slskd recreates under the download dir."""
    parts = _remote_rel(remote).split("/")
    return parts[-2] if len(parts) >= 2 else ""


def _index_download_tree(ddir, username, leaves):
    """basename -> [local path] under the candidate's OWN album trees.

    slskd keeps the remote folder structure under its download dir, but the
    layout depends on version/settings: the generated config pins slskd's
    destination template to `${SOURCE_USERNAME}/${BATCH_ID}/${SOURCE_PATH}`
    (see soulseek.DESTINATION_SUBDIR), so a download lands at
    `<ddir>/<user>/<batch id>/<remote path>/…`; a pre-upgrade install still has
    `<ddir>/<leaf>/…` (slskd's old default) or `<ddir>/<username>/<leaf>/…`.
    The caller builds this ONCE per poll tick instead of walking the tree once
    per pending file; a same-named file from another album must never satisfy a
    pending download, so the scan never walks the whole download dir.

    Dot-directories are pruned: partials now stage in a SIBLING `incomplete/`
    (see soulseek._incomplete_dir), but a pre-migration install still has them
    under `<ddir>/.incomplete`, and a half-written file must never be indexed
    as a completed one."""
    index = {}
    incomplete = (os.sep + ".incomplete").lower()
    unreached = []
    for leaf in leaves:
        found = False
        for scan_root in (os.path.join(ddir, leaf), os.path.join(ddir, username, leaf)):
            if not os.path.isdir(scan_root):
                continue
            for root, dirs, files in os.walk(scan_root):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                if incomplete in root.lower():
                    continue  # still downloading — never a completed file
                for f in files:
                    index.setdefault(f, []).append(
                        os.path.normpath(os.path.join(root, f)))
                    found = True
        if not found:
            # the batch id sits between the username and the leaf, so neither
            # scan root can name this download's tree
            unreached.append(leaf)
    if unreached:
        _index_user_tree(ddir, username, index)
    return index


def _index_user_tree(ddir, username, index):
    """Index THIS user's own subtree into `index` (basename -> [paths]).

    The batch-dir layout puts the download below the username but above the
    folder leaf, which the leaf-rooted scans in _index_download_tree cannot
    reach. One walk of that user alone — never of the whole download dir —
    finds it, with dot-dirs and `incomplete` pruned exactly as there."""
    user = str(username or "").strip()
    user_root = os.path.join(ddir, user)
    if not user or not _inside(user_root, ddir) or not os.path.isdir(user_root):
        return
    incomplete = (os.sep + ".incomplete").lower()
    for root, dirs, files in os.walk(user_root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if incomplete in root.lower():
            continue
        for f in files:
            index.setdefault(f, []).append(
                os.path.normpath(os.path.join(root, f)))


def _local_download_candidates(ddir, username, remote, size, index=None):
    """All plausible local locations for a remote file.

    The exact batch-dir shape (`<ddir>/<username>/<batch id>/<rel>`) cannot be
    named here — the batch id is slskd's own — so the two fixed shapes
    (`<ddir>/<rel>`, `<ddir>/<username>/<rel>`) are checked first and the
    candidate's own album tree from `index` (see _index_download_tree) then
    catches the file wherever the batch id put it, or where slskd sanitised a
    share name; without an index the tree of THIS file's leaf is walked, so the
    function stays usable on its own. A missing file (slskd renamed or removed
    it between the isfile test and the size read) or an unreadable one counts
    as not-yet-present rather than raising out of the job."""
    rel = _remote_rel(remote)
    out = []
    for exact in (os.path.join(ddir, rel), os.path.join(ddir, username, rel)):
        exact = os.path.normpath(exact)  # rel uses "/" — keep one spelling
        if os.path.isfile(exact) and exact not in out:
            out.append(exact)
    leaf = _leaf_of(remote)
    if not leaf:
        return out
    base = os.path.basename(remote.replace("\\", "/"))
    if index is None:
        index = _index_download_tree(ddir, username, [leaf])
    # The batch-dir layout keeps the remote path INTACT below the batch id, so
    # a hit ending in the full remote relative path is that very file; a hit
    # sharing only the basename is the fallback for the layouts that dropped
    # the remote parent directories. Callers take cands[0], so the exact ones
    # are offered first — a same-named file of the user's other album must not
    # win on tree order.
    full_tail = os.path.normcase(os.sep + rel.replace("/", os.sep))
    hits = sorted(index.get(base, ()),
                  key=lambda p: not os.path.normcase(p).endswith(full_tail))
    for p in hits:
        if p in out:
            continue
        # a size mismatch means a different (or truncated) file — only when
        # the peer actually reported a size
        try:
            if size and os.path.getsize(p) != size:
                continue
        except OSError:
            continue     # gone/renamed under us: not present yet
        out.append(p)
    return out


def _local_album_root(ddir, files):
    """The album root on disk for a candidate: the highest directory below
    `ddir` that still holds EVERY downloaded file of it.

    Files live under `<ddir>/<user>/<batch id>/<remote path>/…` (the pinned
    destination template, see soulseek.DESTINATION_SUBDIR), or at the older
    `<ddir>/<leaf>/…` / `<ddir>/<user>/<leaf>/…` shapes. The common parent of
    the candidate's own files is the album either way: taking the first file's
    directory instead half-imported a multi-disc release (it verified one disc
    folder and stranded the rest), and slskd's leaf-only default stranded them
    above `ddir` entirely. Returns None when the files do not share one
    directory below `ddir` (they landed loose in it) — the caller rejects the
    candidate with a clear reason rather than verifying a directory it cannot
    name."""
    paths = [os.path.abspath(os.path.normpath(p)) for p in files or []]
    if not paths:
        return None
    try:
        common = os.path.commonpath(paths)
    except (ValueError, OSError):
        return None       # different drives
    if os.path.isfile(common):
        # a single downloaded file (an image rip): its own folder is the root
        common = os.path.dirname(common)
    return common if _inside(common, ddir) else None


_FAILED_TRANSFER_STATES = ("Cancelled", "TimedOut", "Errored", "Rejected",
                           "FileNotFound", "Aborted", "Failed")


def _transfer_ok(state):
    """slskd reports this transfer finished SUCCESSFULLY — the terminal states
    minus the failures (finished_transfer() alone is true for Errored too)."""
    from server.soulseek import finished_transfer
    st = str(state or "")
    return (finished_transfer(st)
            and not any(x in st for x in _FAILED_TRANSFER_STATES))


def _transfer_active(state):
    """slskd still owes us this file: a state that is neither terminal nor a
    failure (Queued, Initializing, InProgress, Requested). This is the state a
    complete local file must NOT be sitting under for the wait to trust it."""
    from server.soulseek import finished_transfer
    st = str(state or "")
    return (not finished_transfer(st)
            and not any(x in st for x in _FAILED_TRANSFER_STATES))



# An InProgress transfer with no byte movement for this long is dead (a peer
# that stopped responding); waiting out the full per-candidate timeout for it
# is what made a stuck candidate look like a hung import.
_STALL_AFTER_S = 180.0
# The .log gate is a few kB: it must not be waited for as if it were the album.
_LOG_TIMEOUT_S = 180.0
# The old fixed guess, used only when the search never reported a peer speed.
_MIN_RATE = 200 * 1024


def _est_timeout(cand):
    """Per-candidate download timeout from the candidate's OWN numbers.

    The fixed 200 kB/s guess gave a 2 h ceiling to an album a 5 MB/s peer
    fetches in two minutes, and cut off a slow-but-honest peer mid-file. The
    search response carries the peer's upload speed and the number of files
    queued ahead of us, so the wait is the album at that speed plus what the
    queue costs at the same rate (each file ahead the size of an average file
    of the folder). Floor 900 s, cap 7200 s, as before."""
    size = float(cand.get("total_size") or 0)
    rate = float(cand.get("speed") or 0) or _MIN_RATE
    files = len(cand.get("files") or []) or 1
    queued = int(cand.get("queue") or 0) * (size / files)
    return int(min(7200, max(900, (size + queued) / rate)))


def _queue_budget(cand):
    """How long this candidate's own queue is worth waiting for: the files the
    search reported queued ahead of us, at the peer's rate — the same figures
    _est_timeout budgets the queued part of its total from. Floored at
    _STALL_AFTER_S so a peer that reported no queue at all still gets the old
    grace, and capped by the wait's own deadline either way."""
    rate = float(cand.get("speed") or 0) or _MIN_RATE
    size = float(cand.get("total_size") or 0)
    files = len(cand.get("files") or []) or 1
    queued = int(cand.get("queue") or 0) * (size / files)
    return max(_STALL_AFTER_S, queued / rate)


def _progress_snapshot(username, wanted, got, transfers, phase, speed=None):
    """The live download block the Soulseek page renders.

    Aggregate counters plus one row per wanted file, straight from slskd's own
    transfer records (`_user_transfers`, consumed per candidate user+files —
    never unrelated downloads). A file slskd has not reported a size for
    contributes only what is known; nothing is invented, and `percent` is
    clamped so a peer's own bogus counter cannot print 240 %.

    The two file counts are deliberately different units and are labelled as
    such by the UI: `files_done` is what SLSKD calls complete (a terminal
    success, or 100 % of a known size) and `files_arrived` is what this job's
    wait has formally accepted on disk (a file's `done` flag). `percent`/
    `bytes`/`size` stay byte-weighted.

    `speed` is the instantaneous aggregate rate the wait measured from the
    byte delta between its own ticks. When there is no measurement yet (the
    first tick, the call made before any poll) the fallback is slskd's
    per-transfer `averageSpeed` summed over the ACTIVE transfers only — a
    finished file's frozen average is not throughput. `eta_s` is the remaining
    bytes over that rate, so it is null/nothing rather than the largest
    per-file guess (a queued file's `remainingTime` guess used to win) when no
    rate is known."""
    by_name = {t["filename"]: t for t in transfers}
    files, nbytes, nsize, arrived, complete, fallback = [], 0, 0, 0, 0, 0.0
    for w in wanted:
        name = w["filename"]
        t = by_name.get(name) or {}
        done = name in got
        size = int(t.get("size") or w.get("size") or 0)
        b = size if done else int(t.get("bytes") or 0)
        state = str(t.get("state") or "")
        slsk_pct = t.get("percent")
        pct = slsk_pct
        if done:
            pct = 100
        elif pct is None:
            pct = (b * 100 / size) if size else 0
        # `complete` is slskd's verdict, never ours: a file this wait accepted
        # from disk alone (b) has no transfer record to vouch for it.
        is_complete = (bool(t) and _transfer_ok(state)) or (
            slsk_pct is not None and float(slsk_pct) >= 100 and size > 0)
        files.append({
            "name": os.path.basename(name.replace("\\", "/")),
            "bytes": b, "size": size,
            # One decimal, not a whole percent: a big file's bar has to move
            # between two of slskd's own samples.
            "percent": round(max(0.0, min(100.0, float(pct))), 1),
            "speed": float(t.get("speed") or 0),
            "state": "Done" if done else state,
            "done": done, "complete": is_complete,
        })
        nbytes += b
        nsize += size
        arrived += 1 if done else 0
        complete += 1 if is_complete else 0
        if _transfer_active(state):
            fallback += float(t.get("speed") or 0)
    if speed is None:
        speed = fallback
    remaining = max(0, nsize - nbytes)
    return {
        "phase": phase,
        "username": username,
        "dir": os.path.dirname(wanted[0]["filename"].replace("\\", "/")) if wanted else "",
        "files_done": complete,
        "files_arrived": arrived,
        "files_total": len(files),
        "bytes": nbytes,
        "size": nsize,
        "percent": round(max(0.0, min(100.0, nbytes * 100 / nsize)), 1) if nsize else 0,
        "speed": speed,
        "eta_s": int(remaining / speed) if speed > 0 and remaining else None,
        "files": files,
    }


# A cancelled transfer is not instant. slskd answers the DELETE while a request
# is still in flight and flushes the bytes it already received, which re-created
# the very files the rejection had just deleted — and the next candidate, often
# the same album from another peer, downloaded the whole thing again. Settle
# before each sweep, then keep sweeping until a pass removes nothing (live runs
# showed temp writes still landing seconds after the cancel).
_CANCEL_SETTLE_S = 1.0
# Pass ceiling for that convergence loop: a peer that keeps dribbling bytes
# must not hold the job, and 3 passes of settle+delete cover the observed
# window (the writes landed within a second or two of the DELETE).
_CANCEL_SWEEPS = 3
# slskd's temp write: the target name plus `_<ticks>` before the extension
# ("01 - Bombtrack_639252322461155685.flac"). 6+ digits is deliberately looser
# than the 18-19 it emits today, and the stem still has to be a wanted file.
_TEMP_SUFFIX_RE = re.compile(r"^(.*)_(\d{6,})$")


def _cancel_candidate(slsk, username, wanted):
    """Cancel this candidate's transfers and confirm slskd really dropped them.

    soulseek.cancel_downloads reports the ids it did NOT confirm through its
    `failed` collector (a real refusal, or an unreachable slskd — never the
    404/400 of a transfer that was already gone). Those are retried once; the
    caller deletes only afterwards, because a transfer slskd still tracks would
    just re-fetch the files being removed. Returns the ids left unconfirmed."""
    names = {w["filename"] for w in wanted}
    if not names:
        return []
    from server.soulseek import _user_transfers
    try:
        ids = [t["id"] for t in _user_transfers(slsk, username, names) if t.get("id")]
    except Exception:
        return []
    for _ in range(2):
        if not ids:
            return []
        failed = []
        try:
            slsk.cancel_downloads(username, ids, failed=failed)
        except Exception:
            return []
        if not failed:
            return []
        ids = failed          # one retry for the ids slskd did not confirm
    return ids


def _candidate_dirs(ddir, username, wanted, known_files=()):
    """The candidate's OWN album folder(s) on disk — where slskd writes the
    temp-suffixed partials of this candidate's files.

    Named from the remote path, not from whatever happens to be on disk: the
    remote folder's own components must be a path's tail below this user's tree
    (the batch id sits in between, see _index_user_tree), which keeps the scan
    on the candidate's album and nowhere else. The parents of files already
    resolved (`known_files`) are added for the one layout that has no remote
    folder to match — a candidate whose files sit at the top of the share."""
    tails = set()
    for w in wanted:
        tail = _remote_rel(w["filename"]).split("/")[:-1]
        if tail:
            tails.add(tuple(os.path.normcase(x) for x in tail))
    out = []
    for tail in sorted(tails):
        for base in (os.path.join(ddir, *tail), os.path.join(ddir, username, *tail)):
            if os.path.isdir(base) and base not in out:
                out.append(base)
    user_root = os.path.join(str(ddir), str(username or "").strip())
    if str(username or "").strip() and _inside(user_root, ddir) and os.path.isdir(user_root):
        for root, dirs, _files in os.walk(user_root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            parts = os.path.normcase(os.path.normpath(root)).replace("\\", "/").split("/")
            for tail in tails:
                if tuple(parts[-len(tail):]) == tail and root not in out:
                    out.append(root)
    for p in known_files:
        d = os.path.dirname(str(p))
        if os.path.isdir(d) and d not in out:
            out.append(d)
    return out


def _slskd_partial(name, wanted):
    """Whether `name` is slskd's temp write of one of THIS candidate's files:
    `<wanted stem>_<digits><same extension>` (`01 - Bombtrack_639252322461155685.flac`).

    slskd writes a transfer's bytes into the DESTINATION dir under that name
    while it runs and renames it on success. A cancelled transfer leaves the
    temp file behind — seconds after the DELETE answered — so a candidate that
    looked clean still held near-complete copies. The resolver cannot name them
    (the suffix breaks the basename test) and clear_transfer_files refuses
    download-dir bytes with no size to compare, so they are matched here: the
    stripped stem and the extension must be one of the candidate's own wanted
    files, and nothing else. A stem that is not wanted (another album's) never
    matches, whatever its suffix."""
    stem, ext = os.path.splitext(str(name))
    m = _TEMP_SUFFIX_RE.match(stem)
    if not m:
        return False
    base = m.group(1)
    for w in wanted:
        wstem, wext = os.path.splitext(os.path.basename(_remote_rel(w["filename"])))
        if (os.path.normcase(ext) == os.path.normcase(wext)
                and os.path.normcase(base) == os.path.normcase(wstem)):
            return True
    return False


def _drop_temp_partials(ddir, username, wanted, known_files=()):
    """Delete slskd's temp-suffixed partials of this candidate's own files.

    Only inside the candidate's own album folder(s) (see _candidate_dirs) and
    only for a stem in the candidate's wanted set (see _slskd_partial): a
    foreign peer's identically named temp file, and a same-suffix file of
    another album, are never touched. Returns how many files it removed."""
    removed = 0
    for d in _candidate_dirs(ddir, username, wanted, known_files):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not _slskd_partial(name, wanted):
                continue
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            try:
                os.remove(p)
                removed += 1
            except OSError:
                pass
    return removed


def _sweep_candidate(slsk, ddir, username, wanted, remove_root=None):
    """ONE delete pass over a rejected candidate: its own files, its staged
    partials under `incomplete/`, and slskd's temp-suffixed partials in its own
    album folder. Returns how many files it removed — 0 means the candidate is
    gone, which is what ends _drop_candidate's convergence loop."""
    files = _local_wanted_files(ddir, username, wanted)
    removed = _cleanup_partial(ddir, files, remove_root=remove_root)
    for w in wanted:
        # only the STAGING dirs (size 0 makes the helper's download-dir test
        # refuse every file there): its staged shapes are derived from this
        # file's own remote path, while its basename scan of the download dir
        # would reach a sibling candidate's leftover.
        try:
            removed += int(slsk.clear_transfer_files(ddir, username,
                                                     w["filename"], 0)["files_deleted"])
        except Exception:
            pass
    return removed + _drop_temp_partials(ddir, username, wanted, files)


def _drop_candidate(slsk, ddir, username, wanted, remove_root=None):
    """Make a rejection final: cancel, then delete — verified, until it holds.

    Cancelling first (and confirming it) stops slskd from re-requesting the
    files the next lines delete. Each pass then removes the candidate's WHOLE
    set — album audio, cue and the graded log — resolved by layout
    (`<ddir>/<user>/<batch id>/<remote path>` as well as the older shapes, see
    _local_wanted_files), its staged partials under `incomplete/`
    (clear_transfer_files, which owns that path) and slskd's temp-suffixed
    partials in the candidate's own album folder (_drop_temp_partials).

    The passes repeat over a settle until one removes NOTHING: slskd keeps
    flushing a cancelled transfer's bytes for a moment, and a live run showed
    temp writes landing in the destination dir seconds after the cancel — a
    fixed two passes missed those. _CANCEL_SWEEPS bounds it, and a pass count
    above one is logged so a peer that keeps dribbling bytes is visible.

    Scope is strictly the candidate's own remote paths under its own user tree:
    a sibling candidate's same-named file and an already-completed download are
    never touched (see _in_candidate_folder for why the resolver's basename
    fallback is not used on a delete)."""
    unconfirmed = _cancel_candidate(slsk, username, wanted)
    if unconfirmed:
        _log(f"  slskd did not confirm {len(unconfirmed)} cancelled transfer(s) — "
             f"removing what is on disk anyway")
    passes = 0
    for _ in range(_CANCEL_SWEEPS):
        time.sleep(_CANCEL_SETTLE_S)
        if not _sweep_candidate(slsk, ddir, username, wanted, remove_root):
            break            # nothing left: the candidate is really gone
        passes += 1          # this pass had work to do
    if passes > 1:
        _log(f"  candidate removed in {passes} pass(es) — slskd kept writing "
             f"after the cancel")
    # Whatever survived the sweeps is partial bytes of a REJECTED candidate: a
    # locked file, or a peer still flushing. It is recorded on the job (and so
    # reported in its outcome and its queue row) rather than left in the
    # download folder for someone to find by accident.
    _note_leftovers(_local_wanted_files(ddir, username, wanted))


def _note_leftovers(paths):
    """Record partial bytes a rejected candidate could not be freed of.

    A sweep that did not remove everything must not pass in silence: the paths
    ride into the job's result — and from there into the failure notification
    and the queue row — so "a partial download was left behind" is something
    the user is told, not something they discover."""
    if not paths:
        return
    with _lock:
        seen = _job.setdefault("leftovers", [])
        for p in paths:
            if p not in seen:
                seen.append(p)
    _log(f"  {len(paths)} partial file(s) could not be removed — still in "
         f"{os.path.dirname(paths[0])}")


def _in_candidate_folder(path, remote):
    """Whether a LOCAL path is where THIS remote file lives: its last two
    components — "<album folder>/<file>", or just the file at the top of the
    share — must be the remote file's own.

    _local_download_candidates answers "has this file arrived?" and its
    basename fallback is right for that. "May I DELETE this file?" needs the
    candidate's own path back: on the second sweep the rejected candidate's
    bytes are already gone, so the fallback matched the SAME user's OTHER album
    — same basename, even the same size — and deleted a candidate that had not
    been tried yet. Kept case-insensitive, like the resolver's own comparisons;
    the last two components still hold when slskd drops the remote's parent
    directories below the batch id, which is why the tail, not the whole
    relative path, is the test."""
    tail = _remote_rel(remote).split("/")[-2:]
    parts = os.path.normpath(str(path)).replace("\\", "/").split("/")
    return [os.path.normcase(x) for x in parts[-len(tail):]] == \
           [os.path.normcase(x) for x in tail]


def _local_wanted_files(ddir, username, wanted):
    """Local paths of this candidate's OWN files that are on disk already —
    the partials a rejection must delete, the WHOLE set included.

    The .log gate queues the album in the same pass as the logs, so a rejected
    gate used to delete the logs only and leave the album bytes that had landed
    as orphans for the next candidate to download again. One entry per remote
    file, and two deliberate differences from what the download wait accepts:
    no size test (a truncated leftover IS what a rejected candidate leaves
    behind, and the peer's reported size is what the wait, not the delete,
    needs), and the candidate's own remote folder (see _in_candidate_folder —
    nothing outside the candidate's own tree may be removed)."""
    leaves = {_leaf_of(w["filename"]) for w in wanted} - {""}
    index = _index_download_tree(ddir, username, leaves)
    out = []
    for w in wanted:
        for p in _local_download_candidates(ddir, username, w["filename"], 0,
                                            index=index):
            if _in_candidate_folder(p, w["filename"]):
                if p not in out:
                    out.append(p)
                break
    return out


def _log_fail_reason(name, score, state, min_score):
    """Why one rip log did not clear the bar.

    The reason names the required score, so the bar this run is judging by
    (Settings → Auto-import) is discoverable from the attempt itself instead of
    a bare number the user has to guess at."""
    if score is None:
        return f"{name} — unscorable"
    if state == "invalid":
        return f"{name} — checksum invalid"
    return f"{name} — score {score} is below the required {min_score}"


def _wait_for_files(slsk, ddir, username, wanted, timeout_s, cancel_check=None,
                    phase="download", queue_budget_s=None, on_start=None):
    """Poll until every wanted remote path is present locally AND slskd vouches
    for it.

    A pending file is accepted when EITHER

      (a) slskd reports a successful terminal state for it AND a local file
          matches — path shapes first, then this candidate's own album tree at
          exactly the expected size — or
      (b) slskd has NO record in an ACTIVE state for it (_transfer_active: a
          transfer list pruned or cleared mid-job, or a record in a failed
          state) AND a local file sits at the expected size inside the
          candidate's OWN album trees with an mtime older than two poll
          intervals. Disk presence is not proof of a live transfer, so (b) is
          fenced: never while a recorded state is active, never on a size
          mismatch, never for a file still being written. It is logged per file
          so a missing slskd record is visible rather than silent. Without (b) a
          finished album whose slskd record was dropped was rejected, its files
          deleted, and the album downloaded again from the next peer.

    A file on disk is otherwise not proof on its own: the peer may never have
    reported a size for it (0/unknown must not satisfy the wait), and a
    same-named file left behind by an earlier attempt would otherwise count as
    arrived.

    slskd's transfer states are read alongside the filesystem, so a peer that
    rejects the slot (or an errored transfer) costs a poll interval instead of
    the whole per-candidate timeout.

    Stall rules, in order:
      * records exist and some are InProgress — a transfer that has moved no
        bytes for `_STALL_AFTER_S` while EVERY live transfer is InProgress (a
        peer that stopped sending) is dead. A peer with a mix of moving and
        waiting files, or a transfer that is still moving, is never abandoned
        early.
      * no live transfer is InProgress at all (every one Queued, a dropped
        request, or no record) — the peer is simply busy, and `_est_timeout`
        has always budgeted that queue wait, so the candidate is held for
        `queue_budget_s` (its own queue budget, see _queue_budget) instead of
        180 s. Defaults to `_STALL_AFTER_S` when the caller has no candidate
        budget (the .log gate, which is deliberately cheap and bounded).

    Each tick publishes the download progress payload (phase 'logging' while
    the .log gate is being fetched, 'download' for the album) and clears it
    again on the way out — search, verify and import have no download in
    flight. `speed` in that payload is the INSTANTANEOUS rate measured here
    from the byte delta between ticks, never slskd's per-transfer lifetime
    average. wanted = [{filename, size}]. Returns {remote: local_path} for the
    files that arrived complete; missing entries are absent from the dict.

    The progress block SHOWS exactly the `wanted` set: the wait is only ever
    called once the transfers it waits for are the only ones queued (the .log
    gate's album is requested after the gate passes, see _run), so there is
    nothing further to display.

    `on_start` is the "download started" announcer (see _notify_download_start),
    called ONCE — on the first tick where a transfer of this wait is really
    InProgress — with (username, the number of files asked for). It defaults to
    publishing the frame itself; a caller that runs TWO waits over ONE candidate
    (the CD .log gate, then the album) passes an announcer of its own so that
    only the first of them speaks (see _start_once)."""
    deadline = time.time() + timeout_s
    started = time.time()
    from server.soulseek import _user_transfers
    shown = list(wanted)
    pending = {w["filename"]: w for w in wanted}
    # Transfers are fetched for the DISPLAY set and frozen at entry, so every
    # row in the progress block keeps its live state even after its file lands
    # (a row fetched per tick lost its bytes/speed the moment it completed).
    fetch_names = {w["filename"] for w in shown}
    leaves = {_leaf_of(w["filename"]) for w in wanted} - {""}
    got = {}
    live = []
    started_announced = False
    last_bytes = -1
    last_progress = time.time()
    rate = None            # measured bytes/s between the last two ticks
    disp_bytes = None
    disp_time = None
    queue_ceiling = _STALL_AFTER_S if queue_budget_s is None else queue_budget_s
    try:
        while time.time() < deadline and pending:
            tick = time.time()
            if cancel_check and cancel_check():
                break
            # one transfer-tree fetch per tick, and one tree walk per tick too
            # (only when slskd actually says a file is done, or when a file has
            # no live record and its local counterpart has to be looked for)
            live = _user_transfers(slsk, username, fetch_names)
            done_states = {t["filename"] for t in live if _transfer_ok(t["state"])}
            active = {t["filename"] for t in live if _transfer_active(t["state"])}
            ready = [r for r in pending if r in done_states]
            orphans = [r for r in pending if r not in done_states and r not in active]
            if ready or orphans:
                index = _index_download_tree(ddir, username, leaves)
                for remote in ready:
                    cands = _local_download_candidates(
                        ddir, username, remote, int(pending[remote].get("size") or 0),
                        index=index)
                    if cands:
                        got[remote] = cands[0]
                        del pending[remote]
                for remote in orphans:
                    if remote not in pending:
                        continue
                    size = int(pending[remote].get("size") or 0)
                    if not size:
                        continue     # an unknown size proves nothing
                    for p in _local_download_candidates(ddir, username, remote, size,
                                                        index=index):
                        try:
                            st = os.stat(p)
                        except OSError:
                            continue
                        if st.st_size != size or tick - st.st_mtime <= 2 * _TRANSFER_POLL_S:
                            continue     # another file, or one still being written
                        got[remote] = p
                        del pending[remote]
                        _log(f"  {os.path.basename(remote)}: slskd has no live "
                             f"transfer record — accepted the local file "
                             f"({size / (1024 * 1024):.1f} MB, unchanged for "
                             f"{int(2 * _TRANSFER_POLL_S)}s)")
                        break
            # Instantaneous throughput from the byte delta between ticks: slskd's
            # per-transfer averageSpeed is a LIFETIME average, so finished files
            # kept inflating the reported rate long after they stopped moving.
            moved_shown = sum(t["bytes"] for t in live)
            if disp_bytes is not None and tick > disp_time:
                rate = max(0.0, (moved_shown - disp_bytes) / (tick - disp_time))
            disp_bytes, disp_time = moved_shown, tick
            _job_progress(_progress_snapshot(username, shown, got, live, phase,
                                             speed=rate))
            if not pending:
                break
            moving = [t for t in live if t["filename"] in pending]
            if any(any(x in t["state"] for x in _FAILED_TRANSFER_STATES) for t in moving):
                break
            moved = sum(t["bytes"] for t in moving)
            inprog = [t for t in moving if "InProgress" in t["state"]]
            if inprog and not started_announced:
                # Bytes are actually moving for this candidate — the one
                # moment the tray can say "it started"; every later tick is
                # the same news (see _notify_download_start).
                started_announced = True
                (on_start or _notify_download_start)(username, len(wanted))
            if moved > last_bytes:
                last_bytes, last_progress = moved, time.time()
            elif inprog:
                # every live transfer InProgress and none moving = a peer that
                # stopped sending. A mix (one file moving, another still
                # queued) is a working peer and is left alone.
                if (time.time() - last_progress > _STALL_AFTER_S
                        and len(inprog) == len(moving)):
                    _log(f"  transfer stalled for {int(_STALL_AFTER_S)}s with no "
                         f"progress — moving on")
                    break
            elif tick - started > queue_ceiling:
                # nothing InProgress at all: the peer is intact, we are just
                # behind its queue (or slskd never started the transfer). The
                # candidate's own queue budget — the wait _est_timeout already
                # paid for — is the only reason to give up on it.
                _log(f"  still queued after {int(queue_ceiling)}s (this peer's own "
                     f"queue budget) — moving on")
                break
            time.sleep(_TRANSFER_POLL_S)
    finally:
        _job_progress(None)
    if pending:
        # giving up on this candidate: the caller deletes its partial files
        # next, and slskd re-downloads exactly those from its queue
        try:
            slsk.cancel_downloads(username,
                                  [t["id"] for t in _user_transfers(slsk, username, pending)])
        except Exception:
            pass
    return got


# --------------------------------------------------------------------------- #
# Verification helpers
# --------------------------------------------------------------------------- #
def _log_passes(score, state, min_score):
    """Whether a log on its own is good enough to accept the album: scorable,
    at or above the bar, and not a failed checksum (state 'invalid' means the
    log's own SHA256 does not verify — real evidence, never junk)."""
    return score is not None and score >= min_score and state != "invalid"


def _score_logs(local_logs, cfg):
    """Grade rip logs. Returns [(path, score_or_None, checksum_state, detail)]."""
    from mlo.discs import score_disc_log, check_log_checksum, read_log_text

    out = []
    for p in sorted(local_logs):
        try:
            score = score_disc_log(p)
        except Exception:
            score = None
        state, detail = check_log_checksum(p)
        if state is None and not re.search(r"====\s*Log checksum", read_log_text(p) or "", re.IGNORECASE):
            state = "unsupported"
        out.append((p, score, state, detail))
    return out


_CUE_TRACK_RE = re.compile(r"^\s*TRACK\s+\d+", re.IGNORECASE | re.MULTILINE)


def _has_image_rip(album_dir, audio):
    """True when the download is a disc-image rip (Album.flac + Album.cue):
    a cue sheet listing more TRACKs than there are audio files. The log's
    per-track CRCs describe tracks spliced out of ONE image, so they cannot
    be matched against a file — such a rip is verified by decoding it."""
    from mlo.discs import read_log_text
    for root, _dirs, files in os.walk(album_dir):
        for f in files:
            if not f.lower().endswith(".cue"):
                continue
            try:
                text = read_log_text(os.path.join(root, f))
            except Exception:
                continue
            if len(_CUE_TRACK_RE.findall(text or "")) > len(audio):
                return True
    return False


def _verify_album(album_dir, cfg, is_cd):
    """Post-download verification. Returns (ok, problems[]).

    CD: every track's decoded-PCM CRC must match its .log checksum
    (mlo.discs.verify_album_checksums) — except for a disc-image rip, whose
    cue splits one file into the log's tracks, so it is decode-checked like
    digital media. Digital media: every file must decode cleanly (flac -t /
    ffmpeg decode-to-null). The full AudioAuditor pass runs later as part of
    the normal pipeline — only .log matching (CD) and decodability matter for
    accepting or rejecting a download.
    """
    problems = []
    from mlo.tools import detect_all_tools

    audio = []
    for root, _dirs, files in os.walk(album_dir):
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in _AUDIO_EXTS:
                audio.append(os.path.join(root, f))
    if not audio:
        return False, ["no audio files found in the download"]

    if is_cd and not _has_image_rip(album_dir, audio):
        # verify_album_checksums keys off MEDIA=CD — the tags were just
        # written for exactly this reason.
        ffmpeg = (detect_all_tools().get("ffmpeg") or {}).get("ffmpeg_exe")
        if not ffmpeg:
            return False, ["ffmpeg not available for CRC verification"]
        from mlo.discs import verify_album_checksums
        verdicts, unverified = verify_album_checksums(ffmpeg, album_dir, audio, cfg)
        if not verdicts and not unverified:
            # verify_album_checksums keys off the MEDIA tag: with none written
            # it reports nothing at all, which used to read as "verified".
            problems.append("no track's CRC was checked against the rip log "
                            "(the MEDIA tag is missing?)")
        for p, v in verdicts.items():
            if v != "REAL":
                problems.append(f"{os.path.basename(p)}: CRC mismatch vs .log (FAKE)")
        for p, reason in unverified.items():
            problems.append(f"{os.path.basename(p)}: not verifiable ({reason})")
        return not problems, problems
    if is_cd:
        _log("  disc-image rip (one file per disc plus its cue) — verifying by "
             "decode; per-track .log CRCs do not apply")

    # digital media / other: decode-check each file
    tools = detect_all_tools()
    ffmpeg = (tools.get("ffmpeg") or {}).get("ffmpeg_exe")
    flac = (tools.get("flac") or {}).get("flac_exe")
    if not (flac or ffmpeg):
        # every check_one() would return None and the album would be reported
        # as "Verification passed" without a single file being decoded
        return False, ["no decoder available (flac/ffmpeg) — the download "
                       "cannot be verified"]
    from concurrent.futures import ThreadPoolExecutor
    from mlo.subproc import run_tool
    import subprocess

    def check_one(p):
        try:
            if p.lower().endswith(".flac") and flac:
                proc = run_tool([flac, "-t", "-s", p],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                timeout=120)
                return None if proc.returncode == 0 else f"flac -t failed (rc={proc.returncode})"
            if ffmpeg:
                proc = run_tool([ffmpeg, "-v", "error", "-i", p, "-f", "null", "-"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                timeout=120, text=True, errors="replace")
                err = (proc.stderr or "").strip()
                if proc.returncode != 0 or err:
                    return f"decode error: {err[:120]}" if err else f"decode rc={proc.returncode}"
            return None
        except Exception as e:
            return str(e)[:120]

    with ThreadPoolExecutor(max_workers=min(4, len(audio))) as ex:
        for p, err in zip(audio, ex.map(check_one, audio)):
            if err:
                problems.append(f"{os.path.basename(p)}: {err}")
    return not problems, problems


# --------------------------------------------------------------------------- #
# Tag stamping + import
# --------------------------------------------------------------------------- #
def _mb_release_type(rel):
    """'Album; Live' — MusicBrainz release types in Picard's casing.

    Same rule as server/beets/mloplugin.py (EP uppercased, the rest Title
    Case, '; '-joined) so the beets pass that may follow is a no-op."""
    types = [rel.get("primary_type") or ""]
    types += list(rel.get("secondary_types") or [])
    if not any(str(t).strip() for t in types):
        # a release dict built without the structured pair (e.g. a browsed
        # folder) — fall back to the historical lowercase '+'-joined string
        types = str(rel.get("release_type") or "").split("+")
    out = []
    for t in types:
        t = str(t).strip()
        if t:
            out.append("EP" if t.lower() == "ep" else t.title())
    return "; ".join(out)


def _release_country_tag(release):
    """Every ISO code the pressing was released in, "; "-joined.

    MusicBrainz keeps a release's events as a LIST (`release['countries']`,
    server.integrations.release_countries — each entry's own `code`), while
    the entity's singular `country` is only the FIRST of them: a worldwide
    digital reissue of a CD carries both, and a tag written from the singular
    loses one. The singular leads the list — it is the country the rest of the
    app already stamps (autotag, the pending rows) and the FIRST value of a
    multi-value field is the one the naming script reads back for
    $releasecountry, so the album folder must not depend on which of two
    same-day events MusicBrainz' own area names happen to sort first. An event
    without a code (a historic area) states no country and is skipped; a
    release dict carrying no list at all falls back to the singular key, which
    is then all there is.
    """
    codes = []
    for event in (release.get("countries") or []):
        code = str((event or {}).get("code") or "").strip()
        if code and code not in codes:
            codes.append(code)
    singular = str(release.get("country") or "").strip()
    if singular in codes:
        codes.remove(singular)
        codes.insert(0, singular)
    return _LIST_SEP.join(codes) or singular


def _stamp_mb_tags(album_dir, release):
    """Write the exact MusicBrainz release identity into the tags so beets /
    grading work with the release that drove the search.

    Identity tags (release/group/artist IDs, date, country, status, type,
    catalog number, label) are FORCE-written: a download that arrived
    carrying another pressing's IDs would otherwise keep them and drive beets
    matching, the naming script and grading off the wrong release. Per-track
    number/title/artist tags are corrected only when they contradict the
    chosen release, so a matching uploader's spelling survives.

    Every track is ONE container rewrite, and the tracks are stamped side by
    side — see `_stamp_one`."""
    from concurrent.futures import ThreadPoolExecutor
    from mlo.audio import AudioFile

    tracks_meta = release.get("media") or []
    artists = release.get("artists") or []
    artist_mbid = (artists[0].get("mbid") if artists else "") or ""
    artist_name = (artists[0].get("name") if artists else "") or ""
    album_title = str(release.get("title") or "")

    identity = {
        "MUSICBRAINZ_ALBUMID": release.get("id", ""),
        "MUSICBRAINZ_RELEASEGROUPID": release.get("release_group_id", ""),
        # the release's artist credit IS the album artist (Picard semantics)
        "MUSICBRAINZ_ARTISTID": artist_mbid,
        "MUSICBRAINZ_ALBUMARTISTID": artist_mbid,
        "CATALOGNUMBER": release.get("catalog_number", ""),
        "LABEL": release.get("label", ""),
        "BARCODE": release.get("barcode", ""),
        "DATE": release.get("date", ""),
        "ORIGINALDATE": release.get("originaldate", ""),
        # canonical spelling: mlo.audio maps RELEASECOUNTRY, and the naming
        # script's $releasecountry reads it (COUNTRY is a raw container key).
        # EVERY country of the pressing, not MusicBrainz' first release event:
        # the singular `country` is one event of the list, and the app's own
        # tag layer writes/reads a multi-value field "; "-joined (mlo.tagtext
        # _LIST_SEP, mlo.naming._first_multi picks the first for the path).
        "RELEASECOUNTRY": _release_country_tag(release),
        "RELEASESTATUS": release.get("status", ""),
        "RELEASETYPE": _mb_release_type(release),
    }

    audio = []
    for root, _dirs, files in os.walk(album_dir):
        for f in sorted(files):
            # LIB_AUDIO_EXTS, not the search's own _AUDIO_EXTS: the list of
            # files to STAMP is the library's definition of a track (audio +
            # music-video containers), which is what organize, grading and MB
            # matching read back. A music-video album arrives here either way —
            # from the YouTube branch or from a peer folder of videos — and an
            # album whose files nothing stamps is an album with no identity.
            if os.path.splitext(f)[1].lower() in _LIB_AUDIO_EXTS:
                audio.append(os.path.join(root, f))

    # match release tracks -> files (position first, then title+duration)
    assign = {}
    used = set()
    for t in tracks_meta:
        hit = None
        for p in audio:
            if p in used:
                continue
            d, pos = _parse_trackno(p)
            if d == int(t.get("disc") or 1) and pos == int(t.get("position") or 0):
                hit = p
                break
        if hit is None:
            title = _norm_text(t.get("title") or "")
            for p in audio:
                if p in used:
                    continue
                stem = _norm_text(os.path.splitext(os.path.basename(p))[0])
                if title and title in stem:
                    hit = p
                    break
        if hit is not None:
            used.add(hit)
            assign[hit] = t

    def _missing(af, path):
        """The tags THIS file is missing or contradicts, as one mapping.

        The rule `_write` applies per tag, lifted out so both write paths below
        can share it: a value the file already carries is left alone (a
        matching uploader's spelling survives), an empty one is never written.
        """
        want = {}

        def add(key, value):
            value = "" if value is None else str(value).strip()
            if not value:
                return
            if str(af.get_tag(key) or "").strip() == value:
                return
            want[key] = value

        for k, v in identity.items():
            add(k, v)
        # Album-level spelling of the chosen release (corrected when the
        # uploader's tags say something else).
        add("ALBUM", album_title)
        add("ALBUMARTIST", artist_name)
        t = assign.get(path)
        if t:
            add("MUSICBRAINZ_TRACKID", t.get("recording_mbid"))
            add("TRACKNUMBER", t.get("position"))
            add("DISCNUMBER", t.get("disc"))
            add("TITLE", t.get("title"))
            add("ARTIST", t.get("artist_credit"))
        return want

    def _stamp_one(path):
        """Stamp ONE track's tags and land them in a SINGLE rewrite.

        Each of these tags used to be its own write, and every write was a
        whole-file copy: mutagen saves beside the original and that temp is
        renamed over it (mlo.atomic.rewrite_via), so ~18 tags meant ~18 copies
        of the track, per track, one after another. The deferral holds them
        for the one flush at the end instead. It is turned off even when a
        write raised, so a file is never left holding changes nobody saved.

        A VIDEO container has no deferral to lean on — every set_tag on one is
        an ffmpeg stream copy of its own, eighteen of them for this stamp, on a
        file that can be gigabytes — so its whole block goes through
        set_video_tags, the one-pass writer the video pipeline uses. That path
        can also re-emit a raw container as MKV (af.tag_output_path), which is
        the file the album now holds.

        True = this file carries the release identity now. The flush's own
        verdict IS that answer: deferred, a write that cannot land (a full
        disk, a read-only file) no longer raises out of set_tag, so calling it
        stamped would report a tag the file does not have."""
        try:
            af = AudioFile(path)
            if af.audio is None:
                return False
            want = _missing(af, path)
            if not want:
                return True
            if str(getattr(af, "kind", "")) == "video":
                return bool(af.set_video_tags(want))
            # A stand-in for AudioFile (a test double) cannot defer: it writes
            # per tag, exactly as it did before. Same guard mlo.autotag,
            # mlo.audiometa and mlo.moods keep.
            defer = hasattr(af, "defer_save")
            if defer:
                af.defer_save(True)
            stamped = False
            try:
                for k, v in want.items():
                    af.set_tag(k, v)
                stamped = True
            finally:
                if defer and af.defer_save(False) is False:
                    stamped = False
            return stamped
        except Exception:
            return False

    # Distinct FILES are independent — each rewrite_via works on a temp of its
    # own beside its target and swaps it in with os.replace — so an album's
    # tracks stamp side by side instead of one after another; nothing here is
    # shared but the read-only identity above. Inside one file the writes stay
    # strictly ordered.
    workers = worker_count(load_config(), default=8, maximum=8,
                           items=len(audio))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return sum(1 for ok in ex.map(_stamp_one, audio) if ok)


# --------------------------------------------------------------------------- #
# The orchestrator
# --------------------------------------------------------------------------- #
# How many candidates of ONE release are attempted at once: the app's own
# `soulseek_candidate_slots`, and every candidate's bytes are asked for in one
# go — up to that many peers transfer in parallel, the first that verifies good
# becomes the import, and every other candidate's bytes stop and are swept
# rather than left to keep arriving. The next candidate goes out only when one
# of them lands or fails (see _try_batch and _run's batch loop), and the ceiling
# is narrowed when slskd's transfer slots cannot carry it — see _batch_width.


# The orchestrator
# --------------------------------------------------------------------------- #
def _batch_width(cfg=None):
    """Candidates of ONE release attempted at once — the app's per-release cap
    (`soulseek_candidate_slots`), narrowed when slskd cannot take the load.

    The three numbers, and which one really enforces what:

    * `soulseek_search_concurrency` — how many RELEASES run at once. Enforced by
      the app: a release over the ceiling waits in the pipeline's queue and
      starts by itself when one finishes (see _start_next).
    * `soulseek_candidate_slots` — how many candidates of ONE release may be in
      flight. Enforced by the app, here: _try_batch is handed this many peers
      and asks for the next only when one of them lands or fails.
    * `soulseek_download_slots` — the ceiling slskd itself enforces on
      transfers. The app never relies on it (the two above are the promise) but
      it must not be the smaller number either: the shipped default is exactly
      concurrency × candidate_slots — 3 × 3 = 9 — and a config that sets fewer
      slots than its other two settings need gets its per-release width
      narrowed to fit (`slots // releases`), so the product can never be a claim
      the network layer refuses to serve.

    So the app's answer to "how many of one release's candidates download at
    once" is `min(candidate_slots, download_slots // releases)` — the
    user-configured per-release cap, unless the slots they configured for slskd
    make the full product impossible."""
    cfg = cfg or load_config()
    return max(1, min(candidate_slots(cfg),
                      max(1, download_slots(cfg) // concurrency(cfg))))


def _batch_hint(ddir, attempt):
    """Whether every file of `attempt` already sits on disk at the size the peer
    reported, inside THAT candidate's own album trees — the CHEAP half of the
    batch's readiness test, asking slskd nothing.

    Cheap on purpose: it is what keeps a tick of a batch wait from making a
    transfer-tree request per candidate. It is not the verdict — a file can be
    on disk and unclaimed (a leftover of an earlier attempt, a peer slskd still
    reports as Queued) — so _batch_arrived asks slskd on top of it."""
    index = _index_download_tree(ddir, attempt["username"], attempt["leaves"])
    for w in attempt["wanted"]:
        if w["filename"] in attempt["got"]:
            continue
        if not _local_download_candidates(ddir, attempt["username"], w["filename"],
                                          int(w.get("size") or 0), index=index):
            return False
    return True


def _batch_arrived(slsk, ddir, attempt):
    """Whether every file of `attempt` has really arrived: on disk (see
    _batch_hint) AND vouched for by slskd's transfer record.

    The vouch is what makes this usable as a batch's readiness test: a peer
    whose transfers are still Queued has files on disk only if somebody else
    put them there (leftovers), and handing the batch over to such a candidate
    would move the job from a peer that HAS the album to one that merely looks
    like it does. It is not the full acceptance test of _wait_for_files — a file
    with no live record at all is left to that function's own fenced rule (b) —
    only the "this peer is really done" signal the batch needs to pick a
    winner."""
    if not _batch_hint(ddir, attempt):
        return False
    from server.soulseek import _user_transfers
    left = {w["filename"] for w in attempt["wanted"]
            if w["filename"] not in attempt["got"]}
    ok = {t["filename"] for t in _user_transfers(slsk, attempt["username"], left)
          if _transfer_ok(t["state"])}
    return left <= ok


def _batch_cancel_check(slsk, ddir, current, others):
    """The cancel_check that lets the FIRST good copy win inside a batch.

    `_wait_for_files` calls this on every tick, so it answers the two questions
    that can end a candidate's wait early: the user's cancel button, and "is
    another candidate of this batch already holding the whole album?". The
    second is what makes a batch worth having — without it the job sat out the
    slowest peer in rank order while a faster one had the album on disk a minute
    in.

    `current`'s own files are tested first, and a peer that has really arrived
    keeps the wait going (returns False): _wait_for_files resolves it on this
    very tick and returns normally, so a candidate that is itself complete can
    never be handed to the "another peer won" path — which would sweep the album
    it just downloaded. Its test is the same authoritative one (disk AND slskd's
    vouch), so a slower peer whose files merely lie on disk cannot hold the batch
    either."""
    def check():
        if _cancelled():
            return True
        if _batch_arrived(slsk, ddir, current):
            return False
        for o in others:
            if o["state"] in ("downloading", "ready") and _batch_arrived(slsk, ddir, o):
                o["state"] = "ready"     # its album is really here: take it next
                return True
        return False
    return check


def _try_batch(slsk, ddir, batch, release, cfg, is_cd, min_score):
    """Work up to `len(batch)` candidates of ONE release at once.

    EVERY candidate of the batch is queued before any of them is waited for —
    the .log gate alone on a CD, its album once that log passed — so the peers
    transfer in parallel instead of one after the other. The batch is then
    waited candidate by candidate in rank order (`_rank`: lossless, score,
    fastest peer), and each wait ends early the moment ANOTHER candidate's album
    is already on disk, so the first good copy wins rather than the first in
    rank order that happens to finish (see _batch_cancel_check).

    "Good" is exactly what it always was: every file arrived with slskd
    vouching for it (_wait_for_files), the album root resolves under the
    download dir, MEDIA stamps and _verify_album passes — the CD .log/CRC gate
    included, which is the step a CD rip most often fails, so a candidate that
    cannot produce its evidence is rejected and the NEXT candidate is the one
    that imports. Nothing here weakens a check to declare a win.

    A candidate that fails at any step is rejected with its own reason (the
    attempt names the peer, the reason the cause) and swept by _drop_candidate —
    cancel, then delete, verified — and the rest of the batch carries on. The
    candidates the winning one ends are swept the same way: "if one is good that
    it just deletes the others".

    The .log gate itself is graded one candidate at a time and NOT probed (its
    wait is over a few kB and bounded by _LOG_TIMEOUT_S): the logs of the whole
    batch are queued at once, so they arrive in parallel, and the few seconds one
    peer is slower than another cost nothing next to the album transfer the
    probe exists for.

    Returns {"candidate", "username", "dir", "wanted", "got", "root"} for the
    winner, the string "cancelled" when the job was cancelled mid-flight, or
    None when every candidate in the batch was tried and rejected."""
    attempts = []
    for cand in batch:
        uname, folder = cand["username"], cand["dir"]
        _log(f"Candidate: {uname} · …{folder[-60:]} ({cand['matched']}/"
             f"{cand['expected']} tracks matched)")
        wanted, seen = [], set()
        for f in cand["files"]:
            if f["file"] in seen:
                continue
            seen.add(f["file"])
            wanted.append({"filename": f["file"], "size": f["size"]})
        if not wanted:
            _reject(uname, folder, "no files listed for this folder")
            continue
        logs = ([{"filename": f["file"], "size": f["size"]} for f in cand["logs"]]
                if is_cd and cand["logs"] else [])
        log_names = {w["filename"] for w in logs}
        attempts.append({
            "cand": cand, "username": uname, "dir": folder,
            "wanted": wanted, "logs": logs,
            "album_wanted": [w for w in wanted if w["filename"] not in log_names],
            "got": {}, "state": "queued",
            "leaves": {_leaf_of(w["filename"]) for w in wanted} - {""},
            # One "download started" frame per CANDIDATE, however many waits it
            # takes to fetch it (see _start_once).
            "announce": _start_once(len(wanted)),
        })
    if not attempts:
        return None

    # --- queue the whole batch --------------------------------------------
    # The gate's own files go out ALONE on a CD — queueing the album in the same
    # call sent a peer whose log graded below the bar the album's bytes — and the
    # album follows per candidate once ITS log passed. But all of the batch is
    # asked at once: that is the parallelism the batching buys.
    for a in attempts:
        first = a["logs"] or a["wanted"]
        try:
            slsk.enqueue_download(a["username"], first)
        except Exception as e:
            _reject(a["username"], a["dir"], f"download failed to queue: {e}")
            # slskd refuses a folder one file at a time: the peers that DID
            # accept are already queued, so the attempt is dropped the same way a
            # rejected one is instead of leaving those bytes behind.
            _drop_candidate(slsk, ddir, a["username"], a["wanted"])
            a["state"] = "dropped"
            continue
        # The job is DOWNLOADING from this point, so say so immediately: the
        # search bar is cleared and a transfer block is published before the
        # first poll, so the UI switches to "downloading" the moment the peer
        # has the files. On a CD only the log is in flight yet, so the block
        # shows the log alone.
        _job_search_done()
        _job_progress(_progress_snapshot(a["username"], first, set(), [], "queued"))
        a["state"] = "logging" if a["logs"] else "downloading"

    # --- the .log gate (CD): each candidate's own log decides for itself ---
    for a in attempts:
        if a["state"] != "logging":
            continue
        # exactly one log per disc (candidate selection), so this wait is over
        # the wanted logs alone: it returns the moment they are all local
        # instead of waiting out a junk extra log's timeout. They are also the
        # ONLY transfers this candidate has queued so far, so the block shows
        # them and nothing else.
        _stage("downloading", "Downloading the rip log(s) first…")
        _log("Downloading .log file(s) first for a quality check…")
        got_logs = _wait_for_files(slsk, ddir, a["username"], a["logs"],
                                   timeout_s=_LOG_TIMEOUT_S,
                                   cancel_check=_cancelled, phase="logging",
                                   on_start=a["announce"])
        if len(got_logs) < len(a["logs"]):
            _reject(a["username"], a["dir"],
                    f"{len(got_logs)} of {len(a['logs'])} log(s) arrived within "
                    f"{int(_LOG_TIMEOUT_S)}s — trying the next candidate")
            # The log is the only thing this candidate queued, but the WHOLE
            # wanted set is dropped: a partial log, a transfer record or a temp
            # file the peer is still flushing all live under this candidate's own
            # album tree, and the next candidate downloaded them again.
            _drop_candidate(slsk, ddir, a["username"], a["wanted"])
            a["state"] = "dropped"
            continue
        if _cancelled():
            return "cancelled"
        _log("Grading rip log(s) with Logchecker…")
        scores = _score_logs(list(got_logs.values()), cfg)
        good, bad = [], []
        for p, score, state, detail in scores:
            _log(f"  {os.path.basename(p)}: score "
                 f"{score if score is not None else '?'}, checksum {state or '?'}")
            if _log_passes(score, state, min_score):
                good.append(os.path.basename(p))
            else:
                bad.append((os.path.basename(p), score, state))
        # A folder can hold a second, junk log for the same disc (and a
        # manual/browse pick can hold several): the album rides on the log that
        # grades well, the rest are reported and ignored. A checksum MISMATCH
        # still costs the album its evidence — with no good log left, the
        # candidate is rejected and the log that just landed is dropped again.
        if not good:
            _reject(a["username"], a["dir"],
                    "log rejected: " + "; ".join(
                        _log_fail_reason(n, s, st, min_score)
                        for n, s, st in bad)
                    + " (Settings → Auto-import); trying the next candidate")
            _drop_candidate(slsk, ddir, a["username"], a["wanted"])
            a["state"] = "dropped"
            continue
        for n, s, st in bad:
            _log(f"  ignoring {_log_fail_reason(n, s, st, min_score)}")
        _log(f"{len(good)} log(s) pass — downloading the full album…")
        # The log cleared the bar, so the album has earned its bytes: it is
        # requested NOW, in a second call, and only its own files — the logs are
        # already local. A queue failure here is a rejected candidate like any
        # other, reason named as such.
        try:
            slsk.enqueue_download(a["username"], a["album_wanted"])
        except Exception as e:
            _reject(a["username"], a["dir"], f"album failed to queue: {e}")
            _drop_candidate(slsk, ddir, a["username"], a["wanted"])
            a["state"] = "dropped"
            continue
        a["got"] = dict(got_logs)          # the logs are already on disk
        a["state"] = "downloading"

    # --- the album: every peer's bytes are in flight, the first good one wins --
    order = [a for a in attempts if a["state"] == "downloading"]
    while order:
        a = order[0]
        est_timeout = _est_timeout(a["cand"])
        _stage("downloading",
               f"Downloading {len(a['wanted'])} file(s) from {a['username']}…")
        _log(f"Downloading {len(a['wanted'])} file(s) "
             f"({a['cand']['total_size'] / (1024 * 1024):.0f} MB, up to {est_timeout}s)…")
        got = _wait_for_files(slsk, ddir, a["username"],
                              a["album_wanted"] or a["wanted"],
                              timeout_s=est_timeout, cancel_check=_batch_cancel_check(
                                  slsk, ddir, a,
                                  [o for o in order if o is not a]),
                              queue_budget_s=_queue_budget(a["cand"]),
                              on_start=a["announce"])
        # The CD gate's logs are already on disk with terminal transfers, and the
        # album wait above did not cover them (they went out in the first call,
        # not the second): they are folded back in so the album root,
        # verification and import still see the whole candidate.
        got = {**got, **a["got"]}
        if _cancelled():
            return "cancelled"
        if len(got) < len(a["wanted"]):
            missing = [w["filename"] for w in a["wanted"] if w["filename"] not in got]
            # A peer that already has the whole album is reason enough to stop
            # WAITING for this one — and to stop downloading from it: its
            # partials have no import to serve, so they go with it. The reason
            # keeps the old taxonomy (this candidate's own download did not
            # deliver) and adds WHY the job stopped waiting on it.
            mate = next((o for o in order
                         if o is not a and o["state"] == "ready"), None)
            _reject(a["username"], a["dir"],
                    f"download incomplete: {len(missing)} file(s) missing/timed out"
                    + (f" — cancelled because {mate['username']} delivered the "
                       f"album first" if mate is not None else ""))
            if mate is not None:
                _log(f"  {mate['username']} already has the album — dropping "
                     f"{a['username']} and removing its partial files")
            # the files that DID arrive are this candidate's, and the rest of
            # its transfers are still queued: both are dropped together
            _drop_candidate(slsk, ddir, a["username"], a["wanted"])
            order.remove(a)
            continue

        # --- stage 3: audit ---------------------------------------------------
        # The album root on disk is the highest directory below the download dir
        # holding EVERY file of this candidate: the pinned
        # `<ddir>/<user>/<batch id>/<remote path>/…` layout lands there, and so
        # do the older shapes; a multi-disc tree split over one parent verifies
        # as the union instead of half-importing.
        resolved = list(got.values())
        local_root = _local_album_root(ddir, resolved)
        if not local_root:
            _reject(a["username"], a["dir"],
                    "downloaded files did not land in one album folder under the "
                    "download dir")
            _drop_candidate(slsk, ddir, a["username"], a["wanted"])
            order.remove(a)
            continue
        _stage("verifying", "Verifying downloads against the rip log / decoders…")
        _log("Verifying downloads against the rip log / decoders…")
        media = "CD" if is_cd else "Digital Media"
        _stamped, tag_problems = _stamp_media(local_root, media, cfg)
        ok, problems = _verify_album(local_root, cfg, is_cd)
        # A MEDIA tag that could not be written makes verify_album_checksums
        # skip every file, so it is a verification problem, never a warning.
        problems = list(tag_problems) + list(problems)
        ok = ok and not tag_problems
        if not ok:
            for pr in problems[:6]:
                _log("  ✕ " + pr)
            # The reason names the cause, not just the count: a CD rip is judged
            # on its log/CRC evidence, so "which candidate failed and why" is
            # the difference between a peer worth retrying and one that never
            # had the pressing.
            _reject(a["username"], a["dir"],
                    f"verification failed ({len(problems)} problem(s))"
                    + (f": {problems[0]}" if problems else "")
                    + " — trying the next candidate")
            # the album root is this candidate's own (resolved from its own files
            # below the download dir) — a junk file it also dropped there goes
            # with it, and the sweep still runs afterwards
            _drop_candidate(slsk, ddir, a["username"], a["wanted"],
                            remove_root=local_root)
            order.remove(a)
            continue

        # --- the batch is decided: this album is the import --------------------
        for o in order:
            if o is a:
                continue
            _log(f"  dropping {o['username']} — {a['username']} verified first")
            _reject(o["username"], o["dir"],
                    f"{a['username']} delivered the album first — this candidate "
                    f"was cancelled and its partial files removed")
            _drop_candidate(slsk, ddir, o["username"], o["wanted"])
        return {"candidate": a["cand"], "username": a["username"], "dir": a["dir"],
                "wanted": a["wanted"], "got": got, "root": local_root}
    return None


def job_active():
    """True while ANY job holds the pipeline (running or parked on a prompt).

    No longer "the pipeline is taken": several jobs run side by side, so the
    wishes worker uses this to know whether a USER-started job is in flight,
    not to stand down (it fills its own slots — see server/wishes_worker.py)."""
    with _lock:
        return bool(_active_locked())


def _prune_jobs_locked():
    """Drop the oldest SETTLED jobs past the keep window. Caller holds _lock.

    Running jobs are never dropped, and neither is the primary one: it is what
    a caller with no id (the Auto-import panel) is looking at."""
    settled = [j for j in _order if _jobs[j]["state"] not in ("running", "confirm")]
    for jid in settled[:-_KEEP_SETTLED] if len(settled) > _KEEP_SETTLED else []:
        if jid == _primary:
            continue
        _jobs.pop(jid, None)
        _order.remove(jid)


def forget(job_id):
    """Take ONE SETTLED job out of the registry — the queue's own "clear".

    A job that finished is history: the queue row for it is what the user reads
    after the fact, and this is how they take it off the list (the bulk of the
    registry was already dropping the oldest, see _prune_jobs_locked). Nothing
    else changes: the album it imported is in the library, its transfers are
    slskd's own, and a job the wish worker owns is re-created by the wish's
    next search. A job that is still RUNNING (or parked on a question) is
    refused — that one is cancelled, which is a different action, and pretending
    to forget a live job would leave its thread writing into a registry entry
    nobody holds."""
    try:
        jid = int(job_id)
    except (TypeError, ValueError):
        return False
    with _lock:
        job = _jobs.get(jid)
        if job is None or job["state"] in ("running", "confirm"):
            return False
        _jobs.pop(jid, None)
        if jid in _order:
            _order.remove(jid)
        return True


def start_job(release_mbid=None, release=None, queries=None, username=None,
              target_dir=None, confirm_lossy=False, kind=None, mode=None,
              wish_id=None, source=""):
    """Kick off an auto-import job in a daemon thread; returns the job state.

    release — a full release dict (from integrations.release_lookup); when
    only release_mbid is given it is fetched here. queries overrides the
    configured search templates for this run. username+target_dir downloads
    that exact user/folder without searching (manual entry).
    confirm_lossy — the interactive path (server.main's HTTP start route).
    When no lossless folder matched but lossy ones did, park the job and wait
    for confirm() instead of downloading, and likewise park when the search
    found no usable folder at all and the release could be wished instead.
    False (the background wishes worker) means lossy copies are never taken
    silently — and a wish is never asked to become a wish.
    wish_id — the wish this job is filling, so the queue view shows ONE row for
    the release instead of a wish and its job side by side.
    source — who asked ("musicbrainz" / "soulseek" / "auto"), for that row.

    Up to `soulseek_search_concurrency` releases run at once. A release that
    arrives while they all do TAKES ITS PLACE in the pipeline's waiting queue
    instead of being refused: it keeps that place, it is cancellable there
    without ever starting, and it starts by itself the moment one of the
    running releases finishes (see _start_next). The answer says so —
    {"ok": True, "waiting": True, "position": n, "queue_key": …} — and no
    `job`, because there is no job yet.

    A release id already waiting in the bulk queue is refused: it is about to
    run anyway, and letting it through here both downloaded it twice and left
    the duplicate behind in the queue to run a third time."""
    global _seq, _primary
    # The queue lock is taken on its own (never while holding _lock): _finish
    # holds _lock and calls _start_next, which takes this one, so queue → job
    # is the order this module locks in and the reverse would deadlock.
    key = _release_key(release_mbid, release)
    if key and key in _queued_keys():
        return {"ok": False, "error": "this release is already queued for import",
                "job": job_state()}
    # …or already RUNNING right now. `enqueue` has always asked this and
    # `start_job` never did, so a second request for a release that was mid
    # download/import got its own job: the album-folder claim only made it
    # WAIT, and when the wait ended it searched, downloaded and imported the
    # same album again — the duplicate the claim was supposed to prevent.
    # `transient` is the honest classification: the pipeline is busy, not the
    # release failed, so the wishes worker waits rather than spending an
    # attempt. `_running_keys()` counts ACTIVE jobs only, so a settled job
    # never blocks a legitimate retry.
    if key and key in _running_keys():
        return {"ok": False, "transient": True,
                "error": "this release is already being imported",
                "job": job_state()}
    # Already in the library: nothing to import, and re-downloading it is what
    # left a second copy of the same album beside the first (observed live —
    # the HTTP start route reached this function with a release the library
    # already held). The bulk paths skip owned releases; so does this one now.
    # A folder grab (username + target_dir, no MusicBrainz id) has nothing to
    # compare and stays untouched.
    if key:
        try:
            from server import wishes
            if key in wishes.owned_mbids(load_config()):
                return {"ok": False, "error": "this release is already in your "
                                              "library — nothing to download",
                        "job": job_state()}
        except Exception:
            pass      # a library that cannot be read must not block the job
    jid = 0
    with _lock:
        if len(_active_locked()) < concurrency():
            _seq += 1
            jid = _seq
            job = dict(_IDLE_JOB)
            job.update({
                "id": jid,
                "state": "running", "stage": "Starting…", "stage_key": "queued",
                "log": [], "attempts": [], "result": None, "confirm": None,
                "search": None, "progress": None, "cancel": False,
                # Its OWN list, not the one _IDLE_JOB carries: dict(_IDLE_JOB) is a
                # shallow copy, so every job would otherwise share one list.
                "leftovers": [],
                "release": {"id": release_mbid} if release_mbid else None,
                "wish_id": int(wish_id) if wish_id else None,
                "source": str(source or ""), "label": "",
                "started_at": time.time(), "ended_at": 0.0,
                # Its OWN prompt plumbing: three jobs can park on three prompts.
                "_event": threading.Event(), "_answer": {"accept": False}, "_claim": None,
            })
            _jobs[jid] = job
            _order.append(jid)
            _primary = jid
            _prune_jobs_locked()
    if jid:
        threading.Thread(target=_run, name=f"mlo-soulseek-auto-{jid}",
                         kwargs=dict(release_mbid=release_mbid, release=release,
                                     queries=queries, username=username,
                                     target_dir=target_dir,
                                     confirm_lossy=confirm_lossy,
                                     kind=kind, mode=mode, job_id=jid),
                         daemon=True).start()
        return {"ok": True, "job": job_state(jid)}
    # The pipeline is full: WAIT, do not refuse. The release goes into the same
    # queue a bulk download uses, carrying the exact call it was asked for, so
    # it starts (or is cancelled) unchanged whenever its turn comes. This is
    # what makes "three at once, the rest wait" true for EVERY entry point —
    # the search box, a bulk "download all", the wishes worker — rather than
    # only for the callers that happened to go through enqueue().
    item = _queue_item(release_mbid=release_mbid, release=release, queries=queries,
                       username=username, target_dir=target_dir,
                       confirm_lossy=confirm_lossy, kind=kind, mode=mode,
                       wish_id=wish_id, source=source)
    dup = _append_waiting(item)
    if dup:
        return {"ok": False, "transient": True,
                "error": f"this release is {dup}", "job": job_state()}
    started = []
    _start_next(started)      # a slot may have freed while we decided
    with _queue_lock:
        position = next((n + 1 for n, x in enumerate(_queue) if x is item), 0)
    if not position:
        # It did not have to wait after all: the race is one release wide, and
        # the honest answer is the job that started.
        return {"ok": True, "job": job_state(started[-1] if started else None)}
    label = _queue_item_label(item) or "release"
    _log(f"{label}: waiting for a free slot (position {position}).")
    return {"ok": True, "waiting": True, "position": position,
            "queue_key": _queue_key(item)}


def _cancelled():
    with _lock:
        return _job["cancel"]


def _release_from_folder(username, target_dir, slsk):
    """A minimal release dict describing one browsed folder.

    Lets "download this folder I found by browsing" run the whole
    verify → import → organize pipeline without a MusicBrainz release: every
    audio file in the folder is one expected track, so completeness still
    means "every file arrived", and the medium is Digital Media (no .log/.cue
    requirement — a browsed folder is not a verified CD rip).
    """
    files = []
    try:
        for d in slsk.browse(username) or []:
            dpath = str(d.get("directory") or "")
            want = str(target_dir).replace("\\", "/").rstrip("/")
            # boundary-aware: …/NotAlbum must not answer for …/Album
            if _same_folder(dpath, want) or _under(dpath, want):
                files = list(d.get("files") or [])
                break
    except Exception:
        files = []
    media = []
    for i, f in enumerate(files, start=1):
        name = str(f.get("filename") or "")
        if os.path.splitext(name)[1].lower() not in _AUDIO_EXTS:
            continue
        media.append({
            "disc": 1, "position": len(media) + 1,
            "title": os.path.splitext(os.path.basename(name))[0],
            "length": (f.get("length") or 0) and int(f["length"]) * 1000 or None,
        })
    return {
        "id": None,
        "title": str(target_dir).replace("\\", "/").rstrip("/").split("/")[-1],
        "date": "", "country": "", "catalog_number": "", "label": "",
        "release_group_id": None, "artists": [],
        "medium_formats": ["Digital Media"],
        "media": media,
        "barcode": "",
    }


def _reject(username, folder, reason):
    with _lock:
        _job["attempts"].append({"username": username, "dir": folder, "reason": str(reason)[:200]})


def _record_wish_attempt(wid, error, cfg):
    """Record one finished attempt on the wish a failed job just offered.

    The retry policy lives in ONE place (server/wishes: classified outcomes,
    empty-search budget, doubling backoff, attempt caps) and this is the same
    answer its own worker gives a job that ended the same way: a search that
    found nothing spends an empty-search count, anything else spends an attempt,
    and both get `retry_at` set to the backoff's own end. `wishes.due_at`
    compares against exactly that, which is what makes the next pass wait
    instead of searching — and downloading — the release that just failed on
    every candidate.

    Counts are RAISED, never reset: a wish that already failed twice keeps the
    two behind it, so its own budget (and its cap) still means something. A wish
    that is somehow already `imported` is left alone — the library has the album
    and reviving the row would only ask for it again.

    Returns the seconds the next attempt waits (0.0 when it may run at the next
    pass); never raises, because a wish store that cannot be written must not
    undo the download that already succeeded.
    """
    delay = 0.0
    try:
        from server import wishes
        cur = wishes.get_wish(wid) or {}
        if str(cur.get("status") or "") == "imported":
            return 0.0
        attempts = max(1, int(cur.get("attempts") or 0))
        delay = wishes.retry_delay(cfg, attempts)
        not_found = (int(cur.get("not_found") or 0) + 1
                     if wishes.outcome_of(error) == "not_found" else None)
        wishes.mark_wanted(wid, error=str(error or "")[:300], attempts=attempts,
                           retry_at=time.time() + delay, not_found=not_found)
        if delay:
            _log(f"  it is not searched again immediately: one failed attempt is "
                 f"recorded on the wish and the next pass waits "
                 f"{int(delay / 60) or 1} minute(s) (wishes_retry_backoff_minutes).")
    except Exception:
        traceback.print_exc()
    return delay


def _ask_to_wish(release, queries, waited, cfg, confirm_lossy, error="",
                 source=""):
    """Park the job on the "add to wishes?" prompt; add the wish if accepted.

    `waited` is the seconds the job's searches REALLY took (`_search_seconds`,
    every window summed), never the configured ceiling they were allowed: the
    prompt used to report `soulseek_auto_search_wait + _SEARCH_GRACE_S` as if
    the search had spent it.

    `error` is why the job got here — the same message its failure ends on. The
    wish the user accepts RECORDS it as a finished attempt (see
    _record_wish_attempt), which is what stops the worker's next pass from
    re-running this very search, and re-downloading from this very set of peers,
    minutes after they all failed.

    A job that ends with nothing imported used to leave the user a bare error
    and nothing else, even though the background wishes worker keeps searching
    for any release in the wish list — with these very queries. So the two
    dead ends (no usable folder at all, and every candidate rejected) both offer
    the SAME parking spot, through this one helper: one prompt per job, one
    payload shape (reason "no_results"), one place that decides when the offer
    does not apply — a release with no MusicBrainz id has nothing to wish for,
    and the background wishes path (confirm_lossy False) must never ask a wish
    to become a wish.

    Returns the finished-job result when the wish was added, else None (the
    prompt did not apply, or the user declined); a cancelled job is reported by
    the caller's own `_cancelled()` check, which is the same check every other
    prompt in this file uses.

    `source` is where this offer came FROM, recorded on the wish (server.wishes
    SOURCES): "" for the two Soulseek dead ends, which the row labels by their
    release as it always has, and "youtube" for the branch that looked on
    YouTube — a wish the user accepts there is not one the network can answer
    for, and the row must say so."""
    if not (confirm_lossy and release.get("id")
            and cfg.get("soulseek_auto_wish_prompt", True)):
        return None
    from server import wishes
    _log("No usable result — asking whether to add this release to the wishes list.")
    with _lock:
        _job["state"] = "confirm"
        _job["stage"] = "No usable results — add to wishes?"
        _job["stage_key"] = "needs_attention"
        _job["confirm"] = {
            "reason": "no_results",
            "waited": int(waited),
            "queries": list(queries),
            "formats": [],
            "candidates": [],
        }
    _job["_event"].wait()  # released by confirm(job_id) or cancel(job_id)
    _job["_event"].clear()
    with _lock:
        _job["confirm"] = None
        _job["state"] = "running"
        accepted = _job["_answer"]["accept"]
    if not accepted:
        return None
    wish = wishes.add_wish(
        release.get("id"), title=release.get("title") or "",
        artist=((release.get("artists") or [{}])[0].get("name", "")),
        year=str(release.get("date") or "")[:4],
        queries=list(queries),
        source=source,
        # The release the job just resolved, so the new wish's own row can name
        # the exact pressing it is waiting for without a second lookup.
        release=release)
    # add_wish is idempotent: a wish from an earlier attempt comes back
    # unchanged, so refresh its queries — the worker hunts with the stored
    # set, and stale ones would silently drop this job's better templates.
    try:
        wishes.update_wish(wish["id"], {"queries": list(queries)})
    except Exception:
        pass
    # The attempt that got here is recorded ON the wish before anything else can
    # read it: an accepted offer used to add a wish that was born due
    # (`last_search` 0, `retry_at` 0), so the worker's next pass — at most two
    # minutes later — re-ran the same search against the same peers that had
    # just failed. The retry policy's own answer (server/wishes: attempts,
    # empty searches, backoff) now applies to a job the USER ran exactly as it
    # does to one the worker ran itself.
    _record_wish_attempt(wish["id"], error, cfg)
    _log(f"Added to wishes (#{wish['id']}) — the worker keeps searching for this "
         f"release in the background with the same queries, so nothing is lost "
         f"by parking this job.")
    # Nothing landed in the library: report the wish, never a download. The
    # import path's result keys are kept (as None/0) so a caller reading
    # result["album_path"] sees one shape for every finished job.
    return {"wished": True, "wish_id": wish["id"],
            "album_path": None, "staging_path": None,
            "imported": 0, "organized": 0, "organize_error": None}


def _album_name(release):
    """The album's folder name from its own tags — the ONE spelling _import()
    and the folder claim both use, so they can never disagree about which
    folder a release is heading for."""
    return f"{(release.get('artists') or [{}])[0].get('name', '')} - {release.get('title', '')}".strip(" -")


def _album_dir_name(release):
    """That name with the characters a filesystem refuses removed."""
    return _safe_component(_album_name(release))


def _album_claim(release, cfg):
    """The library folder this release will be imported into, for job_locks.

    THE arbiter for "two items must never land in one library folder at once":
    the folder is named exactly as _import() names it — `<library root>/<Artist
    - Album>`, BEFORE the `(2)` suffix a second import into an existing folder
    gets — so two jobs heading for the same folder (two editions of one album,
    an artist run that picked the same edition twice, a wish and a manual grab
    of the same release) serialize here instead of racing to move their files
    in and splitting one album across two jobs. The claim is held for the whole
    job (search, download, verify, import) and released when the job ends."""
    from mlo.paths import library_root
    folder = str(cfg.get("music_folder") or "").strip()
    if not folder or not _album_name(release):
        return ""
    return os.path.join(library_root(folder), _album_dir_name(release))


class _AlbumClaim:
    """One job's claim on its album folder, held for the job's whole life.

    ``job_locks.holding()`` is a context manager and a job here is a long
    function with a dozen exits, so the claim lives on a small thread of its
    own: it takes the folder through the public API (WAITING for whoever holds
    it, up to the registry's own budget), reports back so the job can carry on
    — or fail with the refusal — and lets go the moment the job ends. Nothing
    here re-implements the locking; job_locks remains the one arbiter.
    """

    def __init__(self, path, label):
        self.path = path
        self.label = label
        self.job = ""
        self.error = ""
        self._ready = threading.Event()
        self._done = threading.Event()

    def start(self):
        def keeper():
            from server import job_locks
            try:
                with job_locks.holding([self.path], kind="auto-import",
                                       label=self.label, wait=True) as job:
                    self.job = job
                    self._ready.set()
                    # A job that gave up while it was still waiting sets this
                    # immediately, so the folder is handed straight back.
                    self._done.wait(24 * 3600)
            except job_locks.PathLocked as e:
                self.error = str(e)
                self._ready.set()
            except Exception as e:      # pragma: no cover - never leave a job waiting
                self.error = str(e)
                self._ready.set()

        threading.Thread(target=keeper, name="mlo-album-claim",
                         daemon=True).start()

    def wait(self, cancel_check=None):
        """Block until the folder is ours; "" when it never became ours."""
        while not self._ready.wait(1.0):
            if cancel_check is not None and cancel_check():
                return ""
        return self.job

    def release(self):
        self._done.set()


# --------------------------------------------------------------------------- #
# YouTube — a DIGITAL music-video release
# --------------------------------------------------------------------------- #
# The YouTube route is the release's own medium's answer (acquisition_route
# above) and it runs INSIDE the same job as the Soulseek path: the queue row,
# the stages and the log are the ones the page already renders, and the album
# goes through the same _import — so a music-video release the user asked for
# comes out named, tagged and chained like any other download instead of in a
# folder only yt-dlp knows about.
#
# Only the "search and download" half is different: there is no folder to find,
# so every track of the release is looked up on YouTube by artist + title
# (server.youtube.best_candidate — the artist's own channel preferred, lyric /
# cover / tribute re-uploads rejected, the track's own length a ±5 s filter)
# and downloaded with the app's one quality policy (best video+audio stream,
# merged into MKV by the app's own ffmpeg). A track that cannot be found or
# downloaded is THAT track's failure: the rest of the album is still imported,
# and the job reports every one of them by name.


def _safe_component(name, fallback="Soulseek Import"):
    """One path segment with the characters a filesystem refuses removed."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name or "")).strip() or fallback


def _youtube_album_dir(release, cfg):
    """Where a YouTube album's tracks are staged before the import moves them.

    The download dir's own `YouTube/` branch — under the folder the app shares
    nothing from and sweeps, NOT the library: _import() moves the folder in, so
    what stays here afterwards is a download whose import did not land, which a
    retry can still use (exactly what the Soulseek path leaves behind)."""
    from server import soulseek as slsk
    return os.path.join(slsk.download_dir(cfg), "YouTube",
                        _album_dir_name(release))


def _youtube_filename(track, ext):
    """The name a downloaded VIDEO gets inside the album folder.

    `<disc>-<position> <title><ext>` — the shape the rest of the pipeline reads
    a track's place out of: _parse_trackno takes the leading `1-02` as disc 2
    track 1, and the naming script writes the same
    `%discnumber%-$num(%tracknumber%,2) %title%` prefix. The UPLOAD's own title
    is deliberately not used — "Artist - Song (Official Video)" is not a track
    list, and MB stamping matches a file to its track by exactly this
    number/title pair."""
    disc = int(track.get("disc") or 1)
    pos = int(track.get("position") or 0)
    ext = str(ext or "")
    if not ext:
        ext = ".mkv"
    elif not ext.startswith("."):
        ext = "." + ext
    return f"{disc}-{pos:02d} {_safe_component(track.get('title'), 'track')}{ext.lower()}"


def _youtube_progress(done, total, text):
    """The live block the queue renders while the YouTube branch works.

    The same keys _progress_snapshot publishes, because it is the same queue
    card — but the counts are TRACKS, not bytes: one yt-dlp call per track finds
    AND downloads it, so there is no total size to weight a byte bar with and
    inventing one would be a bar that means nothing."""
    return {
        "phase": str(text or ""), "username": "YouTube", "dir": "",
        "files_done": int(done), "files_arrived": int(done),
        "files_total": int(total), "bytes": 0, "size": 0,
        "percent": round(done * 100.0 / total, 1) if total else 0,
        "speed": None, "eta_s": None, "files": [],
    }


def _drop_youtube_staging(dest):
    """Remove a cancelled YouTube job's staged files.

    `dest` is this job's own folder — named after the album, whose library
    folder the album claim serialises job against job — so nothing of anybody
    else's is in it, and a cancelled music-video album does not leave gigabytes
    of half a download behind. A FAILED job keeps its files instead: they are
    what its retry downloads from, exactly like the Soulseek path's."""
    try:
        shutil.rmtree(dest, ignore_errors=True)
    except Exception:
        pass


def _youtube_fetch(release, dest, cfg):
    """Find and download every track of *release* from YouTube.

    Returns (got, problems): one entry per downloaded track, in the release's
    own track order, and one line per track that could not be found or
    downloaded. A per-track failure is REPORTED, never raised — the other
    tracks of a music-video collection are worth having, and the caller ends
    the job on the count.

    Tracks are fetched `worker_limit` at a time (the same setting every other
    multi-file runner in the app obeys), each on a thread that re-binds the job
    it reports into, so a few videos download side by side and their log lines
    still land in THIS job."""
    from concurrent.futures import ThreadPoolExecutor
    from server import youtube

    tracks = [t for t in (release.get("media") or []) if isinstance(t, dict)]
    total = len(tracks)
    artists = release.get("artists") or [{}]
    artist = str((artists[0] or {}).get("name") or "") if artists else ""
    owner = int(_job.get("id") or 0)
    got = [None] * total
    problems = []
    lock = threading.Lock()
    done = [0]

    def one(i):
        track = tracks[i]
        _tl.jid = owner          # this thread's _log/_stage are THIS job's
        title = str(track.get("title") or "")
        by = str(track.get("artist_credit") or artist or "")
        if _cancelled():
            return
        try:
            seconds = float(track.get("length") or 0) / 1000.0 or None
        except (TypeError, ValueError):
            seconds = None
        label = f"{by} — {title}".strip(" —") or f"track {i + 1}"
        with lock:
            _stage("downloading", f"YouTube: track {i + 1}/{total} — {label}")
            _job["progress"] = _youtube_progress(done[0], total, label)
        candidate = youtube.best_candidate(by, title, seconds, cfg)
        if not candidate:
            with lock:
                problems.append(f"{label}: no usable YouTube upload found")
            return
        try:
            fetched = youtube.download(candidate["url"], dest, cfg)
        except Exception as e:
            with lock:
                problems.append(f"{label}: YouTube download failed ({e})")
            return
        path = str((fetched or {}).get("path") or "")
        target = os.path.join(dest, _youtube_filename(
            track, os.path.splitext(path)[1]))
        try:
            if path and os.path.abspath(path) != os.path.abspath(target):
                os.replace(path, target)
        except OSError as e:
            with lock:
                problems.append(f"{label}: could not name the download ({e})")
            return
        try:
            size = os.path.getsize(target)
        except OSError:
            size = 0
        if not size:
            with lock:
                problems.append(f"{label}: the download left no file")
            return
        got[i] = {"path": target, "track": track,
                  "video_id": str(candidate.get("id") or ""),
                  "url": str(candidate.get("url") or ""),
                  "channel": str(candidate.get("channel") or ""),
                  "height": (fetched or {}).get("height"),
                  "abr": (fetched or {}).get("abr"), "size": size}
        with lock:
            done[0] += 1
            quality = ""
            if (fetched or {}).get("height"):
                quality = f" ({int(fetched['height'])}p"
                quality += (f", {int(fetched['abr'])} kbps audio)"
                            if fetched.get("abr") else ")")
            _log(f"  {done[0]}/{total} {os.path.basename(target)} — from "
                 f"{candidate.get('channel') or 'YouTube'}{quality}")
            _job["progress"] = _youtube_progress(done[0], total, label)

    workers = worker_count(cfg, default=3, maximum=4, items=total)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, range(total)))
    return [g for g in got if g], problems


def _run_youtube(release, cfg, confirm_lossy):
    """Fetch a Digital Media music-video release from YouTube and import it.

    Runs INSTEAD of the search, inside the same job (see _run). Its outcomes
    are the Soulseek path's own:

    * every track downloaded — _import() (MB stamping, the naming script,
      imports.finish_album) and a done job;
    * SOME tracks — a music-video collection is routinely uploaded as a dozen
      separate videos, so the album is imported from what came back and every
      missing track is named in the log and counted in the result;
    * NOTHING found — the same dead end a search that found nothing ends on
      (_ask_to_wish: park and offer the wish list), never a silent success.
    """
    from server import youtube

    total = len([t for t in (release.get("media") or []) if isinstance(t, dict)])
    _log(f"{total} track(s) of this release are music videos published as "
         f"Digital Media — fetching them from YouTube instead of searching "
         f"Soulseek for a folder that cannot be there.")
    if not youtube.enabled(cfg):
        raise RuntimeError("YouTube downloads are disabled in Settings → Videos "
                           "— enable them to fetch this music-video release")
    if not youtube.ytdlp_available(cfg):
        raise RuntimeError("yt-dlp is not available — install it under "
                           "Dependencies to fetch this music-video release")

    dest = _youtube_album_dir(release, cfg)
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"cannot create the album folder {dest}: {e}")
    _stage("searching", f"Finding {total} track(s) on YouTube…")
    _log(f"YouTube: looking for {total} track(s) in "
         f"{os.path.basename(dest)!r}…")
    got, problems = _youtube_fetch(release, dest, cfg)
    if _cancelled():
        # The user said stop: what this job already fetched goes, exactly as
        # the Soulseek path sweeps a cancelled candidate's bytes. A FAILED job
        # keeps its files — they are what its retry downloads from.
        _drop_youtube_staging(dest)
        return _finish("cancelled")
    if not got:
        # Phrased in the app's own dead-end vocabulary ("nothing usable", see
        # wishes._NOT_FOUND_HINTS) on purpose: this is "the network does not
        # have it", not a transient outage, so the wish policy classifies the
        # attempt as an empty search and stops re-asking on a timer.
        msg = (f"Nothing usable found for “{release.get('title') or ''}” on "
               f"YouTube — no usable upload for any of its {total} track(s) "
               f"(see the log).")
        for line in problems[:20]:
            _log("  ✕ " + line)
        wished = _ask_to_wish(release, [], 0, cfg, confirm_lossy, error=msg,
                              source="youtube")
        if _cancelled():
            return _finish("cancelled")
        if wished:
            return _finish("done", wished)
        raise RuntimeError(msg)

    _stage("verifying", f"Checking {len(got)}/{total} file(s)…")
    _log(f"  {len(got)}/{total} track file(s) are in the album folder.")
    for line in problems[:20]:
        _log("  ✕ " + line)
    _stage("importing", "Importing into the library…")
    result = _import(dest, release, cfg, _DIGITAL_MEDIA, source="YouTube")
    # What did NOT come back, in the three places the user looks: the job log
    # (below), the job's own note, and `error_count` for a caller that counts —
    # an album missing two of its twelve videos must not read as a whole one.
    result["error_count"] = len(problems)
    result["problems"] = problems[:20]
    if problems:
        # The note names the tracks, not just the count: "2 of 12 missing" is
        # not something a user can act on, "Second Video: no usable YouTube
        # upload found" is.
        result["note"] = (f"{len(problems)} of {total} track(s) were not "
                          f"fetched from YouTube: "
                          + "; ".join(problems[:3])
                          + (f" (+{len(problems) - 3} more)"
                             if len(problems) > 3 else ""))
        _log(f"{len(problems)} of {total} track(s) could not be fetched from "
             f"YouTube — the rest of the album is in the library.")
    _finish("done", result)


def _run(release_mbid=None, release=None, queries=None, username=None,
         target_dir=None, confirm_lossy=False, kind=None, mode=None, job_id=0):
    from server import soulseek as slsk
    from server import integrations as intg

    # This thread IS this job: every module-level `_job[...]` in the pipeline
    # below (and in the threads it spawns, which re-bind it) resolves to it.
    # 0 = the idle/primary job, which is what a direct _run() call in a test
    # means.
    _tl.jid = int(job_id or 0)
    cfg = load_config()
    try:
        # WHICH NETWORK FETCHES THIS RELEASE (acquisition_route): a Digital
        # Media music-video release is fetched from YouTube, so the slskd
        # precondition below — and every search this job would otherwise run —
        # does not apply to it. Read from the payload the caller already holds
        # when there is one; a payload that cannot answer yet (the release has
        # to be resolved first) answers "soulseek", which is the conservative
        # path every release took before this routing existed.
        route = acquisition_route(release)
        if route != _ROUTE_YOUTUBE:
            if not (slsk.is_running() or slsk.web_up(cfg)):
                raise RuntimeError("slskd is not running — start Soulseek first")
            server = slsk.server_state(cfg)
            if not (server or {}).get("isLoggedIn"):
                raise RuntimeError("Soulseek is not logged in — set your Soulseek "
                                   "username and password in Settings → Soulseek, "
                                   "then restart slskd")
        min_score = int(cfg.get("soulseek_auto_log_min_score", 100) or 100)
        ddir = slsk.download_dir(cfg)

        # ---- release identity ------------------------------------------------
        if release is None and username and target_dir:
            # Browsed-folder grab: there is no MusicBrainz release to match
            # against, so the folder's own audio files ARE the track list. The
            # identity is filled in from their tags by the tagging chain.
            release = _release_from_folder(username, target_dir, slsk)
            _log(f"No MusicBrainz release — treating the folder as the track "
                 f"list ({len(release.get('media') or [])} file(s))")
        if release is None and (kind or "") not in ("", "release"):
            # The HTTP route queued this ID before MusicBrainz resolved it
            # (it refuses to hold a request on a lookup): resolve what it
            # could not wait for — a release group to its edition(s), or a
            # whole artist to one best release per release group. The extra
            # editions go back on the queue and run after this job.
            _log("Resolving the MusicBrainz release(s) to import…")
            rows, skipped = intg.auto_import_targets(release_mbid, kind, mode or "best")
            for s in skipped[:5]:
                _log(f"  - skipped {s.get('mbid')}: {s.get('reason')}")
            if not rows:
                raise RuntimeError(skipped[0]["reason"] if skipped else
                                   "MusicBrainz resolved nothing to import")
            for extra in rows[1:]:
                enqueue(release_mbid=extra["mbid"])
            if len(rows) > 1:
                _log(f"  {len(rows) - 1} more release(s) queued behind this one")
            release_mbid = rows[0]["mbid"]
        if release is None:
            _log("Looking up the MusicBrainz release…")
            release, rid = intg.resolve_release(release_mbid)
            if not release or not rid:
                raise RuntimeError(
                    f"MusicBrainz has no importable release for {release_mbid} "
                    "— it is neither a release nor a release group, or every "
                    "edition of it is ineligible for auto-import")
            release_mbid = rid
        # The release is in hand now: ask the routing rule again (a job that
        # arrived with only a MusicBrainz id could not answer before). A job
        # that already took the slskd precondition above keeps it — that check
        # is what every job has always passed — and this only ADDS the YouTube
        # route for a release that could not be read earlier.
        route = acquisition_route(release)
        is_cd = "CD" in (release.get("medium_formats") or [])
        # DIGITAL decides whether the broad `artist album year` second pass may
        # run at all (see the search block below): a pressing is searched by
        # what identifies the pressing and nothing else, found or not.
        digital = _is_digital(release)
        label = (f"{(release.get('artists') or [{}])[0].get('name') or ''} — "
                 f"{release.get('title') or ''}").strip(" —")
        with _lock:
            _job["release"] = {
                "id": release.get("id"), "title": release.get("title"),
                "artist": ((release.get("artists") or [{}])[0].get("name", "")),
                "date": release.get("date"), "country": release.get("country"),
                "catalog_number": release.get("catalog_number"),
                "media": is_cd and "CD" or "Digital Media",
                # What the release's OWN data says, for a row that shows which
                # release is being fetched: the medium(s) it is pressed on and
                # how many tracks it has, plus the two facts that tell two
                # pressings of the same album apart — MusicBrainz's own
                # disambiguation comment and its release status (Official /
                # Promotion / Bootleg …) — and the label that put it out. All
                # of them are already in hand here — the queue view must never
                # spend a MusicBrainz request per row to learn them (the
                # network is throttled to 1 req/s and the view is polled every
                # few seconds).
                "media_formats": list(release.get("medium_formats") or []),
                "tracks": len(release.get("media") or []),
                "status": release.get("status") or "",
                "disambiguation": release.get("disambiguation") or "",
                "label": release.get("label") or "",
            }
            _job["label"] = label
        _log(f"Target: {(release.get('artists') or [{}])[0].get('name', '?')} — "
             f"{release.get('title')} ({release.get('date') or 'n/a'})"
             f"{', ' + release.get('catalog_number') if release.get('catalog_number') else ''}"
             f" · {len(release.get('media') or [])} track(s) · {'CD' if is_cd else 'Digital Media'}")

        # One album folder, one job: claimed BEFORE a single byte is searched
        # for or downloaded. A job that shadows this folder waits here instead
        # of racing this one into the same library folder — and the wait is
        # visible (stage "queued", with the reason) rather than looking like a
        # search that never answers.
        claim_path = _album_claim(release, cfg)
        if claim_path:
            _stage("queued",
                   f"Waiting for the album folder {os.path.basename(claim_path)}…")
            claim = _AlbumClaim(claim_path, label)
            claim.start()
            # Blocking is the point (the job in front of us owns the folder),
            # and cancel has to get through, hence the cancel_check.
            if not claim.wait(_cancelled):
                if _cancelled():
                    return _finish("cancelled")
                _log(f"Could not take {os.path.basename(claim_path)}: {claim.error}")
                _finish("error", {"error": claim.error
                                  or "the album folder stayed busy"})
                return
            with _lock:
                _job["_claim"] = claim
            # The job we queued behind may have IMPORTED the album while we
            # waited: the claim serialises two jobs, it never says the folder is
            # now full. Re-asking here is what stops this job from searching
            # for — and downloading — an album the library just received.
            # `imported` is true because the LIBRARY holds the album (the note
            # says who put it there), which is the signal the wish worker reads
            # to settle its own row; nothing is cleared here, because this job
            # downloaded nothing.
            try:
                from server import wishes
                _key = _release_key(None, release)
                if _key and _key in wishes.owned_mbids(cfg):
                    _log("The album landed while this job waited for its folder "
                         "— another job imported it, so there is nothing left to "
                         "download.")
                    return _finish("done", {
                        "album_path": claim_path, "imported": True,
                        "staging_path": None, "organized": True,
                        "organize_error": None,
                        "note": "already imported by another job while this one "
                                "waited for the album folder",
                    })
            except Exception:
                pass      # a library that cannot be read must not block the job

        # ---- the network this release comes from -----------------------------
        # A Digital Media music-video release is fetched from YouTube INSIDE
        # this job — no search is sent to slskd at all, and the album it
        # produces goes through the same _import below (MB stamping, the
        # naming script, imports.finish_album) as a downloaded folder does.
        # Everything else falls through to the search unchanged.
        if route == _ROUTE_YOUTUBE:
            return _run_youtube(release, cfg, confirm_lossy)

        # ---- candidates ------------------------------------------------------
        # The queries this job searched with and the window it waited out are
        # reported by the wish prompt, and the candidate loop below can reach
        # both on EITHER path (a browsed-folder grab searches with nothing).
        # `searched_s` accumulates what each `_search_queries` window really
        # spent (the configured one and, on a digital release, the broader
        # second pass) — the number both wish offers publish as `waited`.
        queries_built, search_wait, searched_s = [], 0, 0.0
        if username and target_dir:
            _stage("searching", f"Browsing {username}…")
            _log(f"Manual entry: {username} · {target_dir}")
            entries = slsk.browse(username)
            want = str(target_dir).replace("\\", "/").rstrip("/")
            files = []
            for d in entries or []:
                dpath = str(d.get("directory") or "")
                # boundary-aware (…/NotAlbum must not answer for …/Album) and
                # folder-recursive (the browsed folder plus its subfolders)
                if _same_folder(dpath, want) or _under(dpath, want):
                    for f in d.get("files") or []:
                        files.append({
                            "username": username,
                            "file": f.get("filename") or "",
                            "size": int(f.get("size") or 0),
                            "duration": f.get("duration"),
                            "slot": False, "queue": 0, "speed": 0,
                        })
            root = _album_root(files[0]["file"]) if files else (
                target_dir if target_dir.endswith("/") else target_dir + "/")
            audio = [f for f in files if os.path.splitext(f["file"])[1].lower() in _AUDIO_EXTS]
            logs = _select_logs([f for f in files if f["file"].lower().endswith(".log")],
                                _disc_numbers(release), root)
            # same rule as the searched path: one log per disc, and never a
            # second, unselected log for a disc already covered
            picked = {l["file"] for l in logs}
            plan = [f for f in files
                    if not f["file"].lower().endswith(".log") or f["file"] in picked]
            candidates = [{
                "username": username, "dir": root, "files": plan, "audio": audio,
                "logs": logs,
                "cues": [f for f in files if f["file"].lower().endswith(".cue")],
                "matched": 0, "expected": len(release.get("media") or []),
                "complete": False, "lossless": True, "slot": False, "queue": 0,
                "speed": 0,
                "total_size": sum(f["size"] for f in plan), "score": 0,
            }]
        else:
            if queries:
                plans = [queries] if isinstance(queries, str) else list(queries)
                queries_built = _build_from_templates(plans, release)
            else:
                queries_built = release_queries(release, cfg)
            if not queries_built:
                raise RuntimeError(
                    "Nothing to search by: this release has no trait the "
                    "configured query templates can use — a pressing needs a "
                    "catalog number, a barcode or a label + country. Add a "
                    "template in Settings → Auto-import.")
            # How long a search may run, and how many peers it needs before it
            # is worth scoring. The window is QUIET time (slskd ends a search
            # when the network stops answering) and the response limit ends it
            # outright once that many peers have replied — without the limit a
            # popular album never goes quiet, so nothing is readable until the
            # whole ceiling has elapsed. A usable candidate ends the wait even
            # sooner, so both values are ceilings, never floors.
            search_wait = int(cfg.get("soulseek_auto_search_wait", 10) or 10)
            response_limit = int(cfg.get("soulseek_auto_response_limit", 15) or 15)
            # Every template goes out at once and the merged responses are
            # scored on each tick: the first complete+lossless folder ends the
            # wait for ALL of them, so a weak template never costs another full
            # window. `usable` is the same test the loop below applies.
            def _usable(merged):
                return [c for c in find_candidates(merged, release, cfg)
                        if c["complete"] and c["lossless"]]

            _stage("searching",
                   f"Searching Soulseek with {len(queries_built)} query template(s)…")
            _log(f"Searching Soulseek with {len(queries_built)} query template(s) in "
                 f"parallel: “{'” · “'.join(queries_built)}” … (a good folder ends "
                 f"the search at once, otherwise {response_limit} responses or "
                 f"{int(search_wait + _SEARCH_GRACE_S)}s)")
            results, search_failed, skipped = _search_queries(
                slsk, queries_built, search_wait, usable=_usable,
                response_limit=response_limit)
            searched_s += _search_seconds()
            _job_search_done()
            for line in search_failed:
                # a query slskd errored on is not "no results": it is reported
                # whether or not the other templates found something.
                _log(f"  ✕ {line}")
            responses = [f for _q, res in results for f in (res.get("responses") or [])]
            candidates = find_candidates(responses, release, cfg)
            # The seconds this window really took, not the ceiling the line above
            # advertises: a usable folder (or a quiet network) ends a search long
            # before `search_wait + _SEARCH_GRACE_S`, and the wish prompt reports
            # this same measured number.
            _log(f"  {len(candidates)} candidate folder(s) from {len(responses)} "
                 f"result file(s) across {len(results)} search(es) in "
                 f"{int(searched_s)}s")
            candidates.sort(key=_rank)
            best = [c for c in candidates if c["complete"] and c["lossless"]]
            if best and skipped:
                _log(f"  Usable candidate found ({len(best)} complete lossless) — "
                     f"a good copy is in hand, so the search stopped here and the "
                     f"remaining {skipped} query template(s) were not waited out")

            # ---- ONE broader query, run after the configured one(s) -----------
            # A DIGITAL release whose configured templates name it too
            # precisely answered with hundreds of files and no usable folder.
            # This is not a return of the removed templates: it fires only as a
            # SECOND attempt, sequentially, when the first produced nothing
            # usable, and never runs alongside it (a competing window would
            # only cost the user another search).
            #
            # DIGITAL ONLY, because "artist album year" is exactly what a
            # physical release must never be searched with: it asks the network
            # for every other pressing of the same album, and a CD job that ran
            # it took a WEB rip instead of the pressing its catalog number
            # named. A pressing gets its own templates and nothing else, found
            # or not — see release_queries and _pressing_fallback.
            all_responses = list(responses)
            broad = []
            if digital:
                broad = [q for q in _build_from_templates(["artist album year"], release)
                         if q not in queries_built]
            if not best and broad:
                _log(f"No usable folder from the configured template(s) — one broader "
                     f"search: “{'” · “'.join(broad)}”")
                fb_results, fb_failed, _fb_skipped = _search_queries(
                    slsk, broad, search_wait, response_limit=response_limit)
                fb_s = _search_seconds()
                searched_s += fb_s
                _job_search_done()
                for line in fb_failed:
                    _log(f"  ✕ {line}")
                fb_responses = [f for _q, res in fb_results
                                for f in (res.get("responses") or [])]
                all_responses += fb_responses
                candidates.extend(find_candidates(fb_responses, release, cfg))
                candidates.sort(key=_rank)
                best = [c for c in candidates if c["complete"] and c["lossless"]]
                _log(f"  broader query: {len(fb_responses)} result file(s) in "
                     f"{int(fb_s)}s, {len(candidates)} candidate folder(s) in total")

            # ---- a complete lossless copy that simply has no rip log ----------
            # (CD only.) The CD gate needs one .log + one .cue per disc, so a
            # WEB rip of a CD is dropped before it is even a candidate and the
            # release could never import at all. The folders are scored a second
            # time as the Digital Media release they would become — the same
            # view the rest of the job takes if the user says yes — and only
            # when NO gate-passing candidate exists, so a real rip with a log
            # always keeps the log gate.
            loose = []
            if is_cd and not best:
                loose = [c for c in find_candidates(
                    all_responses, {**release, "medium_formats": ["Digital Media"]}, cfg)
                    if c["complete"] and c["lossless"]]
                loose.sort(key=_rank)
            declined = False
            if loose and confirm_lossy:
                with _lock:
                    _job["state"] = "confirm"
                    _job["stage"] = "No rip log — import as Digital Media?"
                    _job["stage_key"] = "needs_attention"
                    _job["confirm"] = {
                        "reason": "no_logs",
                        "media": "Digital Media",
                        "candidates": [{
                            "username": c["username"],
                            "dir": c["dir"],
                            "format": os.path.splitext(c["audio"][0]["file"])[1].lstrip(".").upper()
                                      if c["audio"] else "",
                            "matched": c["matched"],
                            "expected": c["expected"],
                            "size": c["total_size"],
                            "score": c["score"],
                        } for c in loose[:5]],
                    }
                _log(f"No CD rip with a .log/.cue found — {len(loose)} complete "
                     f"lossless folder(s) hold every track but no log to grade. "
                     f"Waiting for your go-ahead to take one as Digital Media.")
                _job["_event"].wait()  # released by confirm(job_id) or cancel(job_id)
                _job["_event"].clear()
                with _lock:
                    _job["confirm"] = None
                    _job["state"] = "running"
                    accepted = _job["_answer"]["accept"]
                if _cancelled():
                    return _finish("cancelled")
                if accepted:
                    _log("no CD rip with logs found — downloading the complete "
                         "lossless copy and stamping it as Digital Media")
                    # The one line that changes what this job believes it is
                    # importing: everything downstream reads medium_formats —
                    # _verify_album's CD log/CRC audit, _stamp_media's medium and
                    # _import's — so the switch happens BEFORE verify/import and
                    # nowhere later. Nothing else in the release dict is touched:
                    # the MusicBrainz ids, country and catalog number stay, so the
                    # album still carries its release identity.
                    release = {**release, "medium_formats": ["Digital Media"]}
                    is_cd = False
                    # The WHOLE offered set stays the working list, best first
                    # (already `_rank`-sorted): the accepted peer can still
                    # stall, fail to queue or be rejected, and a live run that
                    # kept only `loose[0]` ended with "Every candidate was
                    # rejected (1 attempt(s))" while three usable peers sat
                    # untouched. The next candidate is simply the next in this
                    # list; nothing is re-searched and no second prompt fires.
                    candidates = list(loose)
                else:
                    declined = True

            if not candidates and search_failed:
                raise RuntimeError("; ".join(search_failed[:3]))
            if not candidates or declined:
                # slskd answered, but no folder held the whole album. That used
                # to be a hard stop ("No candidate folder contained every
                # track"), which threw away a search the user just waited out
                # for a release that is merely rare right now. On the
                # interactive path the job instead parks and offers to add the
                # release to the wishes list (see _ask_to_wish): the background
                # worker then keeps searching for it with the very queries this
                # job used, so nothing is lost by parking the job — the user
                # only decides whether it is worth watching for.
                #
                # The window a search may run for is decided in ONE place —
                # soulseek_auto_search_wait (the requested window) plus
                # _SEARCH_GRACE_S (the tail slskd needs to hand back responses
                # once a search ended) — and those are CEILINGS: a usable
                # folder, or a network that went quiet, ends the wait long
                # before them. `waited` reports the seconds the search really
                # took (`_search_seconds`, both passes summed), never the cap:
                # the prompt used to announce 55s about searches that had ended
                # in two, which is a duration nobody waited.
                no_folder_msg = ("No candidate folder contained every track "
                                 "(and cue/log per disc for CD). Try the "
                                 "manual entry or different search terms.")
                wished = _ask_to_wish(release, queries_built, searched_s, cfg,
                                      confirm_lossy, error=no_folder_msg)
                if _cancelled():
                    return _finish("cancelled")
                if wished:
                    return _finish("done", wished)
                raise RuntimeError(no_folder_msg)
            # ---- lossless preference ----------------------------------------
            # `_rank` already puts lossless folders first, so a lossless match
            # wins whenever one exists. When every candidate is lossy the
            # download changes what lands in the library — that decision is
            # the user's (interactive jobs ask; background wishes decline).
            if not any(c["lossless"] for c in candidates):
                formats = sorted({os.path.splitext(f["file"])[1].lstrip(".").upper()
                                  for c in candidates for f in c["audio"]})
                shown = ", ".join(f for f in formats if f)[:60] or "lossy"
                if not confirm_lossy:
                    raise RuntimeError(
                        f"Only lossy copies found ({shown}) — a lossless copy is "
                        f"preferred, so nothing was downloaded.")
                _log(f"Only lossy copies found ({shown}) — waiting for your go-ahead.")
                with _lock:
                    _job["state"] = "confirm"
                    _job["stage"] = "Waiting: only lossy copies found"
                    _job["stage_key"] = "needs_attention"
                    _job["confirm"] = {
                        "reason": "lossy_only",
                        "formats": formats,
                        "candidates": [{
                            "username": c["username"],
                            "dir": c["dir"],
                            "format": os.path.splitext(c["audio"][0]["file"])[1].lstrip(".").upper()
                                      if c["audio"] else "",
                            "matched": c["matched"],
                            "expected": c["expected"],
                            "size": c["total_size"],
                            "score": c["score"],
                        } for c in candidates[:5]],
                    }
                _job["_event"].wait()  # released by confirm(job_id) or cancel(job_id)
                _job["_event"].clear()
                with _lock:
                    _job["confirm"] = None
                    _job["state"] = "running"
                    accepted = _job["_answer"]["accept"]
                if _cancelled():
                    return _finish("cancelled")
                if not accepted:
                    raise RuntimeError("Lossy-only download declined — nothing "
                                       "was downloaded.")
                _log("Lossy download approved — continuing with the lossy copy.")

        # ---- try candidates in batches ---------------------------------------
        # Up to `_batch_width` candidates of THIS release are attempted at once
        # (see _try_batch): three peers transferring in parallel, the first that
        # verifies good ends the batch — the losers are cancelled AND swept, not
        # left to keep arriving — and the next batch of up to three runs only
        # when every peer in it was rejected. One entry per peer+folder
        # (_dedupe_candidates), one attempt per entry: a folder this job already
        # tried is never tried again.
        pool = _dedupe_candidates(candidates)
        width = _batch_width(cfg)
        while pool:
            if _cancelled():
                return _finish("cancelled")
            batch, pool = _take_batch(pool, width)
            _log(f"Batch of {len(batch)} candidate(s), downloading together: "
                 + " · ".join(f"{c['username']} (…{c['dir'][-40:]})" for c in batch))
            found = _try_batch(slsk, ddir, batch, release, cfg, is_cd, min_score)
            if found == "cancelled":
                return _finish("cancelled")
            if not found:
                continue
            if pool:
                _log(f"  {len(pool)} candidate(s) were never started — a good copy "
                     f"is in hand.")
            _stage("importing", "Importing into the library…")
            _log("Verification passed — importing into the library…")

            # --- stage 4: import -------------------------------------------------
            result = _import(found["root"], release, cfg, is_cd)
            # The library holds the album now, so the download dir is only
            # staging: `soulseek_clear_downloads` decides whether what THIS job
            # downloaded there goes with it (see _clear_downloads). Only for an
            # import that really landed — a failed one keeps its files, and they
            # are what its retry downloads from.
            if result.get("imported"):
                result.update(_clear_downloads(slsk, ddir, found, cfg))
            _finish("done", result)
            return

        # ---- every candidate was tried ---------------------------------------
        # Five peers, each one offline or never delivering its .log, and the job
        # died on this bare error with the release forgotten again. Same offer
        # as the no-usable-folder dead end (_ask_to_wish): the release goes into
        # the wish list — with the queries this job already searched with — and
        # the background worker keeps looking for it. The offer carries the
        # seconds the searches REALLY took, like the other dead end.
        rejected_msg = ("Every candidate was rejected "
                        f"({len(_job['attempts'])} attempt(s) — see the log).")
        wished = _ask_to_wish(release, queries_built, searched_s, cfg, confirm_lossy,
                              error=rejected_msg)
        if _cancelled():
            return _finish("cancelled")
        if wished:
            _finish("done", wished)
            return
        raise RuntimeError(rejected_msg)

    except intg.MusicBrainzError as e:
        # MusicBrainz itself is down or rate-limiting — a per-item failure with
        # a reason the queue/UI shows ("MusicBrainz is busy…"), not a wedged
        # job: _finish frees the single-job slot so the next release runs. No
        # traceback: the reason says everything, and this is an outage, not a
        # bug in here.
        _log(f"MusicBrainz did not answer: {e}")
        _finish("error", {"error": f"MusicBrainz: {e}"})
    except Exception as e:
        traceback.print_exc()
        _log(f"ERROR: {e}")
        _finish("error", {"error": str(e)})


def _build_from_templates(templates, release):
    """Render the caller's OWN templates for one release: the queries a job was
    handed (a wish replays what it stored) and its broad second pass. They are
    rendered exactly as given — no configured set is consulted, and no
    MusicBrainz alias is expanded: those are queries somebody already decided."""
    return release_queries(release, {}, templates=templates)


def _stamp_media(album_dir, media, cfg):
    """Write MEDIA (and clear SOURCE on a CD) before verification.

    Returns (stamped, problems). A file whose tag cannot be written is a
    problem, never a silent skip: verify_album_checksums keys off MEDIA, so a
    swallowed failure would let a CD rip "verify" without one CRC check."""
    from mlo.audio import AudioFile
    from server.main import is_audio_file
    n, problems = 0, []
    for root, _dirs, files in os.walk(album_dir):
        for f in sorted(files):
            if not is_audio_file(f):
                continue
            p = os.path.join(root, f)
            try:
                af = AudioFile(p)
                if af.audio is None:
                    problems.append(f"{f}: cannot be read for tagging")
                    continue
                if not str(af.get_tag("MEDIA") or "").strip():
                    af.set_tag("MEDIA", media)
                if media == "CD" and str(af.get_tag("SOURCE") or "").strip():
                    # DELETE it: set_tag("SOURCE", "") wrote an empty tag that
                    # stays on the file forever (nothing re-clears a blank
                    # one) and reads as a cleared tag only to this app.
                    af.delete_tag("SOURCE")
                n += 1
            except Exception as e:
                problems.append(f"{f}: MEDIA tag not written ({str(e)[:80]})")
    return n, problems


def _stamp_source(album_dir, source):
    """Write SOURCE on the tracks of an album this app did not download from
    the network.

    MEDIA is written for every download by `_stamp_media`; WHERE the copy came
    from normally is not this module's to decide — a Soulseek download is
    tagged by the MEDIA/SOURCE pass (server.main) and a digital source by the
    configured `digital_media_source_value` (mlo.lyrics' normalisation). An
    album the YouTube branch fetched is neither, and the album must say so
    itself rather than let a later pass call it "Digital": SOURCE is written
    only where it is EMPTY, so a value the user chose is never overwritten.

    Returns (stamped, problems), the same contract as `_stamp_media`: a tag
    that cannot be written is reported, never swallowed."""
    from mlo.audio import AudioFile
    from server.main import is_audio_file
    n, problems = 0, []
    for root, _dirs, files in os.walk(album_dir):
        for f in sorted(files):
            if not is_audio_file(f):
                continue
            p = os.path.join(root, f)
            try:
                af = AudioFile(p)
                if af.audio is None:
                    problems.append(f"{f}: cannot be read for tagging")
                    continue
                if not str(af.get_tag("SOURCE") or "").strip():
                    af.set_tag("SOURCE", source)
                n += 1
            except Exception as e:
                problems.append(f"{f}: SOURCE tag not written ({str(e)[:80]})")
    return n, problems


def _cleanup_partial(ddir, local_files, remove_root=None):
    """Remove rejected partials so they don't linger in the download dir.

    Returns how many files (and, when `remove_root` went, the directory) it
    removed — 0 is what tells the caller's sweep loop the candidate is gone."""
    removed = 0
    for p in local_files:
        try:
            if os.path.isfile(p):
                os.remove(p)
                removed += 1
        except OSError:
            pass
    if remove_root:
        try:
            if os.path.isdir(remove_root) and _inside(remove_root, ddir):
                shutil.rmtree(remove_root, ignore_errors=True)
                removed += 1
        except Exception:
            pass
    # the album/batch/user shells the deleted files leave behind — slskd never
    # removes a directory it created (rmdir only takes the empty ones, so a
    # sibling download keeps its own)
    from server.soulseek import prune_download_dirs
    prune_download_dirs(ddir)
    return removed


def _file_stats(path):
    """(size, exists) of one local file — 0/False for anything unreadable."""
    try:
        return os.path.getsize(path), True
    except OSError:
        return 0, False


def _tree_stats(root):
    """(files, bytes) under a directory — what removing it is about to free.

    Dot-dirs are skipped, like every other walk here: `.incomplete` is slskd's
    staging tree, staged bytes are counted by the sweep that owns them."""
    n = b = 0
    for dp, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            size, ok = _file_stats(os.path.join(dp, f))
            if ok:
                n += 1
                b += size
    return n, b


def _foreign_files(root, wanted):
    """Files under `root` that are NOT this candidate's own download.

    Everything under the download dir belongs to somebody, and a delete scoped
    to ONE download may only take that download's bytes: a folder that also
    holds a file of another download is left standing (the caller still deletes
    this candidate's own files out of it, one by one — the honest half)."""
    out = []
    for dp, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            p = os.path.join(dp, f)
            if _slskd_partial(f, wanted):
                continue                     # this candidate's own temp write
            if any(_in_candidate_folder(p, w["filename"]) for w in wanted):
                continue
            out.append(p)
    return out


def _clear_downloads(slsk, ddir, found, cfg):
    """Delete what the album that just imported left in the download dir.

    `soulseek_clear_downloads` (Settings → Soulseek, ON by default) is the
    user's switch: the import MOVES the album into the library
    (mlo.paths.move_path — a real move, never a silent copy), so the download
    dir is only staging, and everything this job put there is residue — a copy
    an interrupted move left behind, slskd's temp-suffixed partials, the staged
    halves of its transfers, and the `<user>/<batch id>/<album>` shells a
    finished job used to leave to `_prune_downloads`, which stands down while
    other jobs are running.

    Scoped three ways, because this is the one destructive step of a SUCCESSFUL
    job: only the directories THIS candidate's own remote paths put the download
    in (see _candidate_dirs), only inside the configured download dir
    (`soulseek_download_dir` may be a path the user picked, and nothing above
    the download dir is ever touched), and only after an import that really
    landed — the caller checks `imported`, so a failed import keeps its files
    for the retry that must not download them again.

    The result says what happened rather than passing in silence: how many files
    and bytes went, and what was kept with the reason (the setting off, a path
    outside the download dir, a folder that also holds another download, a file
    a peer still holds open). Nothing here raises — a cleanup that fails is
    reported, never turned into a failed import."""
    out = {"download_cleared": False,
           "download_removed": {"files": 0, "bytes": 0, "dirs": 0},
           "download_kept": []}
    if not cfg.get("soulseek_clear_downloads", True):
        _log("Keeping the downloaded copy: Settings → Soulseek → \"Delete the "
             "downloaded copy after a successful import\" is off.")
        out["download_kept"] = ["the download is kept: the setting is off"]
        return out
    try:
        username, wanted = found["username"], found["wanted"]
        # The candidate's own leftovers first (a truncated partial, a copy the
        # move left behind), then slskd's temp writes and the staged halves —
        # each of them matched against THIS candidate's own file set.
        on_disk = _local_wanted_files(ddir, username, wanted)
        before = {p: _file_stats(p) for p in on_disk}
        # _candidate_dirs can name ONE folder twice — its normcased remote tail
        # and the parent of a resolved file are the same directory on a
        # case-insensitive filesystem — and counting it twice would overstate
        # what the step removed.
        dirs, _seen = [], set()
        for d in _candidate_dirs(ddir, username, wanted, on_disk):
            if os.path.normcase(d) not in _seen:
                _seen.add(os.path.normcase(d))
                dirs.append(d)
        _cleanup_partial(ddir, on_disk)
        gone = [p for p, (_s, ok) in before.items() if ok and not os.path.exists(p)]
        files, freed = len(gone), sum(before[p][0] for p in gone)
        files += _drop_temp_partials(ddir, username, wanted, on_disk)
        for w in wanted:
            try:
                files += int(slsk.clear_transfer_files(
                    ddir, username, w["filename"], 0)["files_deleted"])
            except Exception:
                pass
        # …then the folder(s) the download lived in, deepest first. Each one is
        # removed WHOLE only when everything in it is this candidate's own.
        for root in sorted(dirs, key=len, reverse=True):
            if not os.path.isdir(root):
                continue
            if not _inside(root, ddir):
                out["download_kept"].append(f"{root} (outside the download dir)")
                continue
            foreign = _foreign_files(root, wanted)
            if foreign:
                out["download_kept"].append(
                    f"{root} ({len(foreign)} file(s) of another download)")
                continue
            n, b = _tree_stats(root)
            shutil.rmtree(root, ignore_errors=True)
            if os.path.isdir(root):
                out["download_kept"].append(f"{root} (a file is still open)")
                continue
            files += n
            freed += b
        # the shells the deletions left behind, now that nothing holds them
        from server.soulseek import prune_download_dirs
        prune_download_dirs(ddir)
        # Counted AFTER the prune: a folder that was already empty goes with the
        # shells, and it is still a folder this job's download lived in — the
        # number the result reports is "how many of them are gone", however the
        # bytes that freed them went.
        out["download_removed"]["dirs"] = sum(
            1 for d in dirs if not os.path.isdir(d))
        out["download_cleared"] = True
        out["download_removed"]["files"] = files
        out["download_removed"]["bytes"] = freed
        if files or out["download_removed"]["dirs"]:
            _log(f"Cleared the download: {files} file(s), "
                 f"{freed / (1024 * 1024):.1f} MB removed from the download dir "
                 f"({out['download_removed']['dirs']} folder(s)).")
        else:
            _log("The download directory was already clear — the import moved the "
                 "whole album into the library.")
        for why in out["download_kept"]:
            _log(f"  kept {why}")
    except Exception:
        traceback.print_exc()
        out["download_cleared"] = False
        out["download_kept"].append("the cleanup itself failed — see the log")
    return out


def _start_import_chain(album_dir, cfg, release=None):
    """Finish the freshly imported album in the background: links, metadata,
    cover art — and the configured script chain.

    Delegates to ``server.imports.finish_album(album_dir, cfg)``: the album was
    moved, AcoustID-checked, converted, MB/MEDIA-stamped and named by
    ``_import`` before this runs, and the chain the user configured
    (optimization, tagging, grading) runs over it now that it is in the
    library — a download the user waited for must come out finished, not
    sitting half-tagged in the library.

    It runs in a daemon thread (a job's result is where the album is, not
    twenty minutes of re-encoding) and is failure-tolerant by design: a
    failure is written into the job log and the import still succeeds — the
    album is already in the library.

    Whatever the chain could not supply is reported by `finish_album` itself
    (``_report_gaps`` → ``import_autonomy.raise_prompt``): the album's missing
    families go out as ONE ``import_needs_data`` notification and land in
    ``GET /api/import/prompts``, which is what the queue's "Needs you" row and
    its wizard link are made of. That is the download path's half of "stop and
    ask for help", through the same seam every other import path uses rather
    than a second opinion about what is missing.

    ``_account_metadata`` runs after it: the artist image and the artist/album
    descriptions are the three things an unattended import has to account for,
    so the log always names each one's outcome even when the chain's own
    metadata step was switched off or staged them for review.
    """
    from server import imports

    # The job this album belongs to, captured on ITS thread: the chain runs on
    # a thread of its own long after _run() returned, and its log lines (and
    # the metadata progress it reports) are the job's, not whichever job
    # happens to be primary by then.
    owner = int(_job.get("id") or 0)

    def chain():
        _tl.jid = owner
        try:
            result = imports.finish_album(album_dir, cfg, release=release)
            for err in result.get("errors") or []:
                _log("  ! " + err)
            # The chain's own one-line report ("ran 12 of 14 scripts — 2
            # failed"), then what the album is still short of. The missing
            # families were already announced (import_needs_data, one frame)
            # by the import that computed them; this is the job log's copy.
            summary = imports.chain_summary(result)
            if summary:
                _log(summary)
            missing = ((result.get("autonomy") or {}).get("missing") or {})
            if missing:
                names = ", ".join(sorted(str((v or {}).get("label") or k)
                                         for k, v in missing.items()))
                _log(f"Still missing after the chain: {names} — reported for a "
                     f"decision (Soulseek queue → Needs you).")
        except Exception:
            traceback.print_exc()
            _log("Import pipeline crashed: "
                 f"{traceback.format_exc().strip().splitlines()[-1]}")
        _account_metadata(album_dir, cfg)

    _log("Running the import chain in the background (RateYourMusic links, "
         "metadata, cover art, then the configured scripts).")
    threading.Thread(target=chain, name="mlo-soulseek-import-chain",
                     daemon=True).start()


def _account_metadata(album_dir, cfg):
    """Find and account for the album's artist image and descriptions.

    One line per item — fetched from where, already there, switched off, or
    nothing found — the same step (and the same wording) the import menu
    drives, so an unattended import states what it did about all three
    instead of leaving them a silent gap the artist page later shows as
    missing. Never fatal: a provider being down is not an import failure.

    With the metadata step switched off, or set to stage candidates for
    review, the user's choice is reported instead of overruled — writing here
    would be exactly the automatic behaviour they turned off.
    """
    if not cfg.get("metadata_auto_fetch", True):
        _log("Artist image / descriptions: the metadata step is off "
             "(Settings → Metadata), so none were fetched.")
        return
    if cfg.get("metadata_review", False):
        _log("Artist image / descriptions: metadata review is on — candidates "
             "were staged for you instead of written. Apply them from the "
             "album's page.")
        return
    from server.api_discovery import ensure_artist_album_metadata, meta_line
    try:
        result = ensure_artist_album_metadata(album_dir, cfg,
                                              progress=_metadata_progress)
        for item, res in result.items():
            _log("  " + meta_line(item, res))
    except Exception:
        traceback.print_exc()
        _log("Artist image / descriptions: the metadata step failed — "
             "the album itself is fine.")


def _metadata_progress(done, total, label):
    """Live stage text while the metadata step is on the network.

    Sets the job's stage only — the per-item outcome is what the log carries,
    so the pending/finished pair would just double every line."""
    with _lock:
        _job["stage"] = f"Artist image / descriptions {done}/{total}: {label}"


def _verify_acoustid(album_dir, release, cfg):
    """Check the downloaded audio against the release the job searched for.

    AcoustID fingerprints what actually arrived and names its release group;
    a different pressing/edition downloads fine, passes the log/CRC gate and
    is still not the release the tags claim. That is a WARNING in the job log
    (``server.imports.release_group_mismatch``), never a rejected download —
    the user can see it on the Soulseek page and decide. The tag-derived
    candidate (`release`) goes in as *expect*, so the fingerprint is
    CROSS-CHECKED rather than trusted, and it never writes anything: the
    release the job matched stays the identity of the album.

    A check that could not be RUN says so by name (no fpcalc, a timeout, a
    rejected key) — "no release group matched" is only ever the service's own
    answer.
    """
    want = str(release.get("release_group_id") or "").strip()
    if not want or not cfg.get("import_acoustid"):
        return
    try:
        from mlo import acoustid
        from server import imports

        if not acoustid.available(cfg):
            _log(f"AcoustID check skipped: {acoustid.acoustid_enabled_note(cfg)}")
            return
        result = imports.acoustid_match([album_dir], cfg, expect=release)
        row = (result.get("albums") or [{}])[0]
        status = str(row.get("status") or "")
        if status == "error":
            _log("WARNING: the AcoustID check could not answer: "
                 f"{row.get('reason') or row.get('code')} — the download is "
                 "unverified, not rejected.")
            return
        warning = imports.release_group_mismatch(row, want)
        if warning:
            _log("WARNING: " + warning + " — the download may be a different "
                 "pressing or edition.")
        elif row.get("release_group_id"):
            _log(f"AcoustID confirmed the release group "
                 f"({row.get('matched')}/{row.get('total')} tracks).")
        elif status == "skipped":
            _log(f"AcoustID check skipped: "
                 f"{row.get('reason') or row.get('code')} (not a rejection).")
        else:
            _log("AcoustID check: no release group matched the audio "
                 "(inconclusive, not a rejection).")
    except Exception:
        traceback.print_exc()
        _log("WARNING: the AcoustID check crashed — the download is "
             "unverified, not rejected.")


def _import(local_root, release, cfg, media, source=""):
    """Move the verified download into the library and run the pipeline.

    `media` is the medium already detected for this candidate — MEDIA is
    stamped from it instead of re-detecting the medium of the whole folder.
    `source` is where the album came from, for the one caller whose origin the
    rest of the app cannot infer (the YouTube branch): it is written as SOURCE
    where the file does not already say something."""
    from mlo.paths import library_root, move_path
    from server import main as srv
    from server.main import OrganizeRequest

    folder = str(cfg.get("music_folder") or "").strip()
    if not folder or not os.path.isdir(folder):
        raise RuntimeError("music_folder is not configured")

    name = _album_name(release)
    safe = _album_dir_name(release)
    # the library lives in <music folder>/Artists — the music folder root is
    # what the Soulseek network is shared from, not where albums belong.
    dest = os.path.join(library_root(folder), safe)
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(library_root(folder), f"{safe} ({n})")
        n += 1

    if not move_path(local_root, dest, log=_log):
        # mlo.paths.move_path never copies-then-fails: it retries a locked file
        # (slskd still holding one) and gives up without half-moving the album.
        raise RuntimeError(
            f"could not move {os.path.basename(local_root)} into the library — "
            f"a file is still locked (Soulseek holding it open?); the download "
            f"is left intact at {local_root}")
    _log(f"Moved into the library: {os.path.basename(dest)}")

    # AcoustID verification of what actually arrived, against the release this
    # job searched for: a warning in the job log, never a rejection.
    _verify_acoustid(dest, release, cfg)

    # Any lossless source the folder arrived in (WAV/APE/ALAC...) becomes the
    # configured lossless codec BEFORE it is named and graded, so the naming
    # script and the grader both see the final extension.
    try:
        from mlo.flac import convert_album_lossless, target_codec
        s = convert_album_lossless(dest, cfg)
        if s.get("modified_count"):
            _log(f"Converted {s['modified_count']} lossless file(s) to "
                 f"{target_codec(cfg).upper()}.")
    except Exception:
        traceback.print_exc()

    # exact MusicBrainz identity before organizing; MEDIA itself was written
    # from the detected medium before verification (the CRC step needs it) and
    # only gets re-stamped if the conversion above rewrote files
    try:
        _stamp_mb_tags(dest, release)
    except Exception:
        traceback.print_exc()
    _stamped, tag_problems = _stamp_media(dest, media, cfg)
    for pr in tag_problems[:4]:
        _log("  ! " + pr)
    if source:
        _sourced, source_problems = _stamp_source(dest, source)
        for pr in source_problems[:4]:
            _log("  ! " + pr)
        _log(f"Tagged the album's origin: SOURCE={source}.")

    organized = False
    organize_error = None
    album_path = dest
    try:
        r = srv.organize(OrganizeRequest(paths=[dest], dry_run=False))
        res = (r.get("results") or [{}])[0] if isinstance(r, dict) else r.results[0]
        if isinstance(res, dict) and res.get("error"):
            organize_error = res["error"]
        else:
            organized = True
            # the naming script moved the album — report where it actually is
            # now, or the UI names a folder that no longer exists
            album_path = str(res.get("album_root") or dest)
            _log("Organized with the naming script.")
    except Exception as e:
        organize_error = str(e)

    _start_import_chain(album_path, cfg, release)
    return {"album_path": album_path, "imported": True,
            "staging_path": dest, "organized": organized,
            "organize_error": organize_error}
