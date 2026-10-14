"""The rating UPLOAD QUEUE — the app's own ratings, batched back to MusicBrainz.

``server.mb_submit`` owns the one MusicBrainz WRITE call; this module owns WHEN
it happens. Two events feed it: a rating the user just made (``server.ratings``
calls :func:`enqueue` after the store write) and the bulk "upload everything"
action (``POST /api/mb/ratings/submit-all`` -> :func:`enqueue_all`). Both land a
FOLDER in one process-wide deduped pending set, so a burst of ratings across an
album coalesces into the one request the ``/api/mb/ratings/submit`` route would
have built for that folder.

The worker is a lazily-started daemon thread: it waits until the pending set has
been untouched for ``COALESCE_SECONDS`` (so rating twelve tracks of an album is
ONE submission, not twelve), then hands each folder to :func:`album_submission`
— the ONE place that decides what an album contributes to MusicBrainz, shared
with the submit route so the two callers cannot drift — and appends the outcome
to a bounded in-memory list the panel reads.

Two rules hold across everything here:

  * NOTHING raises to a caller. A rating write must never fail because the
    upload could not be queued, so :func:`enqueue` is total and silent; and one
    folder failing to submit never stops the others.
  * NOTHING touches the network on the request path. A rating returns before a
    socket is opened; the daemon thread is the only thing that submits.
"""
import os
import threading
import time
from collections import deque

from mlo.config import load_config
from server import mb_submit

# How long the pending set must sit untouched before a submission runs. A user
# rating an album track by track types for a while; 5 s of quiet is the signal
# that the burst is over, and coalescing it into one request is the point.
COALESCE_SECONDS = 5.0
# How often the worker re-checks while it is waiting out the coalesce window.
# Small enough that a test (which shortens COALESCE_SECONDS) is not slow, and
# small enough that a shutdown does not hang on a long sleep.
_POLL_SECONDS = 0.2
# The rating owner this queue submits for. The store is multi-user, but the
# queue has no request to read a user from (and a rating row written by the
# default/admin scope is what an unauthenticated install holds), so it uploads
# the default scope's rows — the same ones `ratings.rate`/`bulk_rate` store.
USER = ""
# How many recent outcomes the panel can read back; the newest 20 only.
_RECENT_MAX = 20

# The deduped pending folders, and the wall-clock stamp of the last enqueue
# (both guarded by _PEND_LOCK). A fresh enqueue resets the coalesce window.
_PENDING = set()
_LAST_ENQUEUE = 0.0
_PEND_LOCK = threading.Lock()

# The worker thread and the event that wakes it out of a poll (guarded by
# _WORKER_LOCK for the handle; the event is thread-safe on its own).
_WORKER = None
_WORKER_LOCK = threading.Lock()
_WAKE = threading.Event()

# The outcome rows, newest first (guarded by _RECENT_LOCK).
_RECENT = deque(maxlen=_RECENT_MAX)
_RECENT_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# The gate: is there a login that can actually receive a rating?
# --------------------------------------------------------------------------- #
def _enabled(cfg):
    """The submission switch — a saved login is not consent on its own."""
    return bool((cfg or {}).get(mb_submit.SUBMIT_KEY, True))


def _connected(cfg):
    """Is a MusicBrainz login saved that could submit? A client id plus a
    refresh (or access) token is enough; a blank login refuses. The client
    SECRET is deliberately not required — a PKCE public client has none."""
    cfg = cfg or {}
    cid = str(cfg.get(mb_submit.CLIENT_ID_KEY) or "").strip()
    refresh = str(cfg.get(mb_submit.REFRESH_KEY) or "").strip()
    access = str(cfg.get(mb_submit.ACCESS_KEY) or "").strip()
    return bool(cid and (refresh or access))


# --------------------------------------------------------------------------- #
# What one album folder contributes — the ONE builder the route shares
# --------------------------------------------------------------------------- #
def _album_release_group(files):
    """The release-group MBID the files of one album carry, or "".

    Read off the files' own tags (MUSICBRAINZ_RELEASEGROUPID) — the same
    evidence the import wrote — and the first file that states one decides for
    the folder."""
    from server import tagcache
    for path in files:
        try:
            tags, _ = tagcache.read_track(path, ["MUSICBRAINZ_RELEASEGROUPID"])
        except Exception:
            continue
        got = str((tags or {}).get("MUSICBRAINZ_RELEASEGROUPID") or "").strip()
        if got:
            return got
    return ""


def album_submission(folder, files, user=""):
    """``(owners, items)`` for one album folder — the one decision point.

    `owners` is ``[(kind, path, value_0_100), …]`` and `items` is
    ``[(entity, mbid, value_0_100), …]``, aligned index for index; the route
    turns owners into per-file rows while the queue only needs items, but both
    travel through HERE so a change to what an album submits cannot reach one
    caller and miss the other. Recordings first, then the album's release group
    (when the files state a release-group id OR the folder has its own album
    rating)."""
    from server import ratings
    values = ratings.map_for(paths=list(files), user=user, adopt=True,
                             scope="track")
    owners, items = [], []
    for path in files:
        half = int(values.get(ratings.api_path(path)) or 0)
        value = half * ratings.TAG_STEP
        items.append(("recording", ratings.mbid_for(path), value))
        owners.append(("track", path, value))
    rg_mbid = _album_release_group(files)
    album_half = int(ratings.value(folder, user=user, scope="album") or 0)
    if rg_mbid or album_half:
        items.append(("release_group", rg_mbid, album_half * ratings.TAG_STEP))
        owners.append(("album", folder, album_half * ratings.TAG_STEP))
    return owners, items


# --------------------------------------------------------------------------- #
# The submission the worker runs
# --------------------------------------------------------------------------- #
def _submit_folder(folder):
    """Build and send ONE folder's submission, then record the outcome.

    Never raises: a folder whose files cannot be read, whose items cannot be
    built, or whose request is refused is recorded as a not-ok row with the
    reason, and the worker moves on to the next folder."""
    try:
        from mlo.discs import album_audio
        from mlo.paths import LIB_AUDIO_EXTS
        files = album_audio(folder, LIB_AUDIO_EXTS)
    except Exception:
        files = []
    try:
        _, items = album_submission(folder, files, user=USER)
        result = mb_submit.submit_ratings(items, load_config())
    except Exception as e:                                    # never escapes
        result = {"ok": False, "sent": 0, "skipped": 0,
                  "detail": f"{type(e).__name__}: {e}"}
    _record({
        "path": folder,
        "name": os.path.basename(os.path.normpath(folder)),
        "ok": bool(result.get("ok")),
        "sent": int(result.get("sent") or 0),
        "skipped": int(result.get("skipped") or 0),
        "detail": str(result.get("detail") or ""),
        "at": time.time(),
    })


def _record(row):
    with _RECENT_LOCK:
        _RECENT.appendleft(row)          # newest first; the bounded deque drops
                                          # the oldest when it is full


def _worker_loop():
    """The daemon: coalesce a burst, then submit each pending folder."""
    global _WORKER
    while True:
        with _PEND_LOCK:
            if not _PENDING:
                _WORKER = None
                return
            wait = COALESCE_SECONDS - (time.time() - _LAST_ENQUEUE)
        if wait > 0:
            # Sleep out the rest of the coalesce window; a fresh enqueue sets
            # the event, which shortens the wait to the next poll so the window
            # restarts from the newest rating.
            _WAKE.wait(min(wait, _POLL_SECONDS))
            _WAKE.clear()
            continue
        with _PEND_LOCK:
            folders = list(_PENDING)
            _PENDING.clear()
        for folder in folders:
            try:
                _submit_folder(folder)
            except Exception:
                continue


def _ensure_worker():
    """Start the daemon if it is not already running (idempotent)."""
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return
        _WORKER = threading.Thread(target=_worker_loop, name="mb-ratings-sync",
                                   daemon=True)
        _WORKER.start()


# --------------------------------------------------------------------------- #
# The public surface
# --------------------------------------------------------------------------- #
def enqueue(folder):
    """Hand ONE folder to the upload queue. Idempotent, total, and silent.

    Returns True when the folder joined the pending set, False otherwise
    (already pending, blank, or no login/switch to submit with). Never raises:
    a caller on the rating-write path must not fail because of this."""
    try:
        raw = str(folder or "").strip()
        if not raw:
            return False
        cfg = load_config() or {}
        if not _enabled(cfg) or not _connected(cfg):
            return False
        key = os.path.normpath(raw)
        global _LAST_ENQUEUE
        with _PEND_LOCK:
            if key in _PENDING:
                return False
            _PENDING.add(key)
            _LAST_ENQUEUE = time.time()
        _WAKE.set()
        _ensure_worker()
        return True
    except Exception:
        return False


def enqueue_all(cfg=None):
    """Every rated folder the app holds, handed to the queue. -> count queued.

    Walks the ratings store for folders that carry ANY rating — an album
    scope's own rows plus the folder of each track-scope row — and keeps only
    the ones that still exist on disk. Artist rows are NOT submitted (a
    MusicBrainz rating here is per recording / release group). Never raises."""
    try:
        from server import ratings
        folders = ratings.rated_folders()
    except Exception:
        return 0
    count = 0
    for folder in folders:
        try:
            if not os.path.isdir(folder):
                continue
        except Exception:
            continue
        if enqueue(folder):
            count += 1
    return count


def state():
    """What the panel shows: the switch, the login, and the queue's own life.

    -> ``{connected, enabled, pending, running, recent}`` with `recent` newest
    first (each row ``{path, name, ok, sent, skipped, detail, at}``)."""
    try:
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    with _PEND_LOCK:
        pending = len(_PENDING)
    with _RECENT_LOCK:
        recent = list(_RECENT)
    return {
        "connected": _connected(cfg),
        "enabled": _enabled(cfg),
        "pending": pending,
        "running": bool(_WORKER is not None and _WORKER.is_alive()),
        "recent": recent,
    }