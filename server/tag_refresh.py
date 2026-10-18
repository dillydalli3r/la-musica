"""MAINTAIN → Tags: when each album's MusicBrainz data was last pulled.

The stamp lives IN THE FILES (``MUSICBRAINZ_UPDATED``, written by every writer
that lands MusicBrainz values — script 8's per-track pass, the import's
release stamp, this module's own refresh), never in a table: a library moved
to another machine, or restored from a backup, keeps its history. This module
is the read side (an album's age is its OLDEST track's stamp) and the write
side (a queue that refreshes whole albums, a scheduler that starts it when the
oldest stamps are past their configured age).

Deliberately album-granular on the WRITE side: a track's metadata is one row
of a release, so a refresh re-reads the release once and stamps every file —
pulling per track would ask MusicBrainz the same question n times and could
leave one album half-updated.

The queue is bounded and multi-threaded on purpose: a large library must not
start a walk of every album at once. Callers name paths or "everything due";
workers take albums off the queue (default 8, since the MusicBrainz client no
longer serializes its callers behind the socket), each under the library's own
per-album claim, so a refresh can never race an import or a script run. The
MusicBrainz client's own 1 req/s etiquette still holds globally.
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

DEFAULT_DAYS = 7
# 8, not 4: MusicBrainz is one request a second and (since the client reserves
# its slot and releases the lock before the socket, `integrations.mb_get`) a
# slow answer no longer serializes the other callers behind it — so a worker
# blocked in a lookup costs no sibling anything, and the extra workers overlap
# the tag reads/writes that used to be the queue's real width.
DEFAULT_WORKERS = 8
DEFAULT_INTERVAL_MINUTES = 60

# The stamp every MusicBrainz writer puts on a file (mlo.autotag
# `mb_updated_stamp`): ISO-8601 UTC, so a lexical compare is a time compare.
STAMP_TAG = "MUSICBRAINZ_UPDATED"

_lock = threading.Lock()
_status_cache = {"key": None, "t": 0.0, "data": None}
_STATUS_TTL = 30.0

_job_lock = threading.Lock()
# Albums this process refreshed, {normpath: ISO stamp}: the library tree is
# stale-while-revalidate, so the row a job just moved would otherwise read its
# OLD stamp until the next rebuild. The override is what the Tags page sees
# immediately; an hour is long past any rebuild.
_fresh = {}
_job = None            # the running/last job's state (see `_new_job`)
_job_pool = None
_last_auto = 0.0
_started = False


# --------------------------------------------------------------------------- #
# Reading the stamps
# --------------------------------------------------------------------------- #
def _norm(path):
    return str(path or "").replace("\\", "/")


def _stamp_times(tracks):
    """(oldest, newest, missing) across one album's tracks.

    Only a parseable stamp counts as pulled: a hand-edited value the app did
    not write is not evidence of a pull, and ``missing`` is how it stays
    visible instead of becoming a very old date.
    """
    values, missing = [], 0
    for tr in tracks or []:
        raw = str((tr.get("tags") or {}).get(STAMP_TAG) or "").strip()
        # The writer's own format is fixed-width ISO; accept the plain date
        # form a hand tagger may produce too.
        if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
            values.append(raw)
        else:
            missing += 1
    if not values:
        return None, None, missing
    return min(values), max(values), missing


def _days(cfg):
    try:
        value = float((cfg or {}).get("tag_refresh_days", DEFAULT_DAYS) or 0)
    except (TypeError, ValueError):
        value = DEFAULT_DAYS
    return max(1.0, value)


def _cutoff(cfg, now=None):
    """The ISO stamp older than which an album is due, in the same format."""
    # Clamped to the config's own ceiling (10 years): a hand-edited value past
    # it would ask time.gmtime for a date before the epoch, which Windows
    # refuses outright.
    seconds = min(_days(cfg), 3650.0) * 86400.0
    moment = time.time() if now is None else float(now)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment - seconds))


def _payload(cfg):
    """The library tree, built through its one cached builder."""
    from server import library as lib_mod
    return lib_mod.build_library(cfg)


def status(cfg, now=None, force=False):
    """Every album's freshness, and the queue's own state.

    ``albums`` is one row per album that holds a release id (an album with no
    MusicBrainz link has nothing to refresh FROM and is counted as
    ``unlinked``), with the artist/album names, the oldest/newest stamp, how
    many tracks have none, and whether it is ``due``. ``artists`` rolls the
    same rows up per artist, so the page can show staleness at an artist
    level without a second walk.

    Cached for `_STATUS_TTL`: the page polls it while a job runs, and a job's
    own completion drops the cache (`invalidate`), so the poll is cheap and
    still tells the truth at the moment that matters.
    """
    folder = str((cfg or {}).get("music_folder") or "")
    days = _days(cfg)
    key = (folder, days)
    moment = time.time() if now is None else float(now)
    with _lock:
        hit = _status_cache
        if (not force and hit["data"] is not None and hit["key"] == key
                and moment - hit["t"] < _STATUS_TTL):
            return hit["data"]
    cutoff = _cutoff(cfg, moment)
    stale_after = moment - 3600
    with _lock:
        for key, stamp in list(_fresh.items()):
            try:
                seen = time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
            except (TypeError, ValueError):
                seen = 0
            if seen and seen < stale_after:
                _fresh.pop(key, None)
        fresh_now = dict(_fresh)
    lib = _payload(cfg)
    albums = []
    unlinked = 0
    for artist in lib.get("artists") or []:
        artist_name = str(artist.get("display_name") or artist.get("name") or "")
        for alb in artist.get("albums") or []:
            tracks = alb.get("tracks") or []
            meta = alb.get("meta") or {}
            release = str(meta.get("MUSICBRAINZ_ALBUMID")
                          or meta.get("MUSICBRAINZ_RELEASEGROUPID") or "").strip()
            oldest, newest, missing = _stamp_times(tracks)
            just = fresh_now.get(os.path.normcase(os.path.normpath(
                str(alb.get("path") or ""))))
            if just:
                oldest = newest = just
                missing = 0
            if not release:
                unlinked += 1
                continue
            if not tracks:
                continue
            due = bool(missing) or (oldest is None) or oldest < cutoff
            albums.append({
                "path": _norm(alb.get("path")),
                "artist": artist_name,
                "album": str(meta.get("ALBUM") or alb.get("name") or ""),
                "release": release,
                "tracks": len(tracks),
                "missing": missing,
                "oldest": oldest,
                "newest": newest,
                "due": due,
                "pending": bool(alb.get("pending")),
            })
    albums.sort(key=lambda r: (r["artist"].lower(), r["album"].lower()))
    by_artist = {}
    for row in albums:
        group = by_artist.setdefault(row["artist"], {
            "artist": row["artist"], "albums": 0, "due": 0,
            "tracks": 0, "oldest": None, "newest": None, "missing": 0,
        })
        group["albums"] += 1
        group["due"] += 1 if row["due"] else 0
        group["tracks"] += row["tracks"]
        group["missing"] += row["missing"]
        if row["oldest"] and (group["oldest"] is None or row["oldest"] < group["oldest"]):
            group["oldest"] = row["oldest"]
        if row["newest"] and (group["newest"] is None or row["newest"] > group["newest"]):
            group["newest"] = row["newest"]
    artists = sorted(by_artist.values(),
                     key=lambda g: (not g["due"], g["artist"].lower()))
    due = [r for r in albums if r["due"]]
    with _job_lock:
        job = dict(_job) if _job else None
    data = {
        "days": days,
        "cutoff": cutoff,
        "albums": albums,
        "artists": artists,
        "counts": {
            "albums": len(albums),
            "tracks": sum(r["tracks"] for r in albums),
            "due": len(due),
            "never": sum(1 for r in albums if r["oldest"] is None),
            "unlinked": unlinked,
        },
        "oldest": min((r["oldest"] for r in albums if r["oldest"]), default=None),
        "job": job,
        "scheduled": bool((cfg or {}).get("tag_refresh_enabled", False)),
        "interval_minutes": _interval(cfg),
        "last_auto": _last_auto,
    }
    with _lock:
        _status_cache.update({"key": key, "t": moment, "data": data})
    return data


def album_detail(cfg, album_path):
    """One album's per-track stamps — what the page expands a row into."""
    path = os.path.normpath(str(album_path or ""))
    if not path:
        return None
    lib = _payload(cfg)
    for artist in lib.get("artists") or []:
        for alb in artist.get("albums") or []:
            if os.path.normcase(os.path.normpath(str(alb.get("path") or ""))) != os.path.normcase(path):
                continue
            meta = alb.get("meta") or {}
            oldest, newest, missing = _stamp_times(alb.get("tracks") or [])
            return {
                "path": _norm(alb.get("path")),
                "artist": str(artist.get("display_name") or artist.get("name") or ""),
                "album": str(meta.get("ALBUM") or alb.get("name") or ""),
                "release": str(meta.get("MUSICBRAINZ_ALBUMID")
                               or meta.get("MUSICBRAINZ_RELEASEGROUPID") or ""),
                "oldest": oldest, "newest": newest, "missing": missing,
                "due": bool(missing) or oldest is None
                or (oldest or "") < _cutoff(cfg),
                "tracks": [{
                    "path": _norm(tr.get("path")),
                    "title": str((tr.get("tags") or {}).get("TITLE")
                                 or tr.get("file") or ""),
                    "pulled": str((tr.get("tags") or {}).get(STAMP_TAG) or ""),
                } for tr in (alb.get("tracks") or [])],
            }
    return None


def invalidate():
    """Drop the assembled payloads — the CHEAP half only.

    A refresh changes tags, never the folder layout, so the layout rescan a
    full `library_refresh.refresh_now` runs is work with nothing to find — and
    on a large library it walks every directory. The next `/api/library` read
    re-derives the tree from the per-album tag caches this pass invalidated.
    """
    with _lock:
        _status_cache.update({"t": 0.0, "data": None})
    # NOTHING else: the per-album tag caches were already invalidated by
    # `refresh_release_tags` (which marks the assembled tree dirty too — see
    # `tagcache.invalidate_album`), so dropping every payload here would make
    # the next read rebuild the WHOLE library synchronously — the exact cost a
    # large install pays once already, with nothing new to learn: the
    # freshness the page shows comes from `_fresh`, not the tree.


# --------------------------------------------------------------------------- #
# The queue
# --------------------------------------------------------------------------- #
def _interval(cfg):
    try:
        value = int(float((cfg or {}).get("tag_refresh_interval_minutes",
                                          DEFAULT_INTERVAL_MINUTES) or 0))
    except (TypeError, ValueError):
        value = DEFAULT_INTERVAL_MINUTES
    return max(5, value)


def _workers(cfg):
    try:
        value = int(float((cfg or {}).get("tag_refresh_workers",
                                          DEFAULT_WORKERS) or 0))
    except (TypeError, ValueError):
        value = DEFAULT_WORKERS
    return max(1, min(16, value))


def _new_job(paths, cfg, source):
    return {
        "id": f"tags-{int(time.time() * 1000)}",
        "source": source,
        "status": "running",
        "started": time.time(),
        "finished": None,
        "total": len(paths),
        "done": 0,
        "refreshed": 0,
        "written": 0,
        "skipped": [],
        "errors": [],
        "current": "",
        "workers": _workers(cfg),
    }


def job_state():
    with _job_lock:
        return dict(_job) if _job else None


def refresh(paths=None, cfg=None, due_only=False, source="manual"):
    """Start (or describe) a refresh job over *paths* — whole albums.

    With no paths and ``due_only`` the album list comes from `status`: every
    album past its configured age. Nothing due starts NO job (the caller
    gets ``{"job": None, "reason": "..."}``) instead of an empty run the UI
    would show as a spinner that never moved.

    A job already running is returned as it stands: two presses must not put
    two workers on the same albums (the per-album claim would make the second
    wait, which is exactly the invisible queue this feature must not build).
    """
    global _job, _job_pool
    cfg = cfg or _load_cfg()
    with _job_lock:
        if _job and _job.get("status") == "running":
            return {"job": dict(_job), "started": False,
                    "reason": "a refresh is already running"}
    want = [os.path.normpath(p) for p in (paths or []) if str(p or "").strip()]
    if not want:
        # The cached read: the library tree is already memoized, and a press
        # must not wait on a fresh walk of a large library.
        rows = status(cfg)["albums"]
        if due_only:
            want = [r["path"] for r in rows if r["due"]]
        else:
            want = [r["path"] for r in rows]
    # One album, one entry; keep the caller's order for named paths.
    seen, ordered = set(), []
    for p in want:
        key = os.path.normcase(p)
        if key in seen or not os.path.isdir(p):
            continue
        seen.add(key)
        ordered.append(p)
    if not ordered:
        return {"job": None, "started": False,
                "reason": "every album's MusicBrainz data is up to date"}
    job = _new_job(ordered, cfg, source)
    with _job_lock:
        _job = job
    _status_cache.update({"t": 0.0, "data": None})
    # ONE orchestration thread; the album workers are created inside it so a
    # press returns immediately and the job's own pool can be sized by config
    # at the moment it starts.
    threading.Thread(target=_run_job, args=(job, ordered, cfg), daemon=True,
                     name="tag-refresh").start()
    return {"job": dict(job), "started": True, "reason": ""}


def _run_job(job, paths, cfg):
    """Process the queue; ONE bad album is recorded and the rest run."""
    global _job_pool
    try:
        def one(path):
            with _job_lock:
                job["current"] = path
            try:
                from server import script_runners
                with script_runners.claim_paths(
                        [path], kind="tags", wait=True,
                        label="Tag refresh: " + os.path.basename(path)):
                    from server import imports
                    out = imports.refresh_release_tags(path, cfg=cfg)
                if out.get("skipped"):
                    with _job_lock:
                        job["skipped"].append({"path": path,
                                               "reason": out.get("reason") or ""})
                else:
                    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    with _lock:
                        _fresh[os.path.normcase(os.path.normpath(path))] = stamp
                    with _job_lock:
                        job["refreshed"] += 1
                        job["written"] += int((out.get("identity") or {}).get("written") or 0)
            except Exception as e:  # noqa: BLE001 — reported per album
                with _job_lock:
                    job["errors"].append({"path": path, "error": str(e)})
            finally:
                with _job_lock:
                    job["done"] += 1
        pool = ThreadPoolExecutor(max_workers=job["workers"],
                                  thread_name_prefix="tag-refresh")
        with _job_lock:
            _job_pool = pool
        try:
            list(pool.map(one, paths))
        finally:
            pool.shutdown(wait=True)
        with _job_lock:
            job["status"] = "done"
            job["finished"] = time.time()
            job["current"] = ""
    except Exception as e:  # noqa: BLE001 — the job's own failure
        traceback.print_exc()
        with _job_lock:
            job["status"] = "error"
            job["finished"] = time.time()
            job["errors"].append({"path": "", "error": str(e)})
    finally:
        # ONE status-cache reset (and one `_fresh` sweep) per job, at its end,
        # not per album: `refresh` already dropped it when the job started and
        # the page polls the job state while it runs, so a per-album reset
        # bought nothing and only made the next read rebuild the status.
        invalidate()
        with _job_lock:
            _job_pool = None


# --------------------------------------------------------------------------- #
# The scheduler
# --------------------------------------------------------------------------- #
def _load_cfg():
    try:
        from mlo.config import load_config
        return load_config()
    except Exception:
        return {}


def tick(now=None):
    """One scheduler pass: start a due-only refresh when the switch is on.

    Called on a timer; the album scan itself is the cached status read (the
    library tree is memoized), so an idle tick costs one pass over rows that
    are already in memory. Never starts a second job; never re-runs a full
    library sooner than the configured interval.
    """
    global _last_auto
    cfg = _load_cfg()
    if not bool(cfg.get("tag_refresh_enabled", False)):
        return {"started": False, "reason": "scheduled refresh is off"}
    now = time.time() if now is None else float(now)
    gap = _interval(cfg) * 60.0
    if _last_auto and now - _last_auto < gap:
        return {"started": False, "reason": "not time yet"}
    with _job_lock:
        if _job and _job.get("status") == "running":
            return {"started": False, "reason": "a refresh is already running"}
    data = status(cfg, now=now)
    if not data["counts"]["due"]:
        _last_auto = now
        return {"started": False, "reason": "nothing is due"}
    out = refresh(due_only=True, cfg=cfg, source="schedule")
    _last_auto = now
    return out


def start():
    """Start the scheduler thread (lifespan's own worker, once per process)."""
    global _started
    if _started:
        return
    _started = True

    def loop():
        # Wake every minute; `tick` decides whether the interval and the ages
        # say a run is due. A minute is short enough that "every week" lands
        # within an hour of the configured age, and cheap: a disabled switch
        # returns before any read.
        while True:
            try:
                tick()
            except Exception as e:  # noqa: BLE001
                print(f"[mlo] tag refresh scheduler: {e}")
            time.sleep(60)

    threading.Thread(target=loop, daemon=True,
                     name="tag-refresh-scheduler").start()