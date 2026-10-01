"""Persistent index of the tag/grade layer, keyed on what the app can trust.

ISSUE #70. Building the library is the one thing that made a page take tens of
seconds: every album payload re-parses its audio (mutagen open, vorbis-comment
copy, embedded-cover decode, path/naming checks, the integrity evidence read),
and every process restart threw all of it away — so the first page after a
restart paid the whole library again. The in-memory tag cache only ever helped
a process that was already warm.

This module is the disk half of that cache. It stores an ALBUM PAYLOAD (what
`server.library.build_album` returns, which is the tag reads AND the grade
verdicts derived from them) under the identity the app already uses for cached
tag reads — a file's own (path, mtime_ns, size) — extended to the album: every
entry of the album folder contributes (relative path, mtime_ns, size), because
the payload is a function of the WHOLE folder (tags, sidecar covers, a rip log,
the expected-tracks manifest, the framework marker, a description). A row is
served only when that signature matches EXACTLY, so:

  * a file edited, rewritten, resized or added changes the signature and the
    album is read again — a changed file is never served from the index;
  * a file deleted drops its line from the signature, and an album whose folder
    is gone is pruned;
  * a settings change moves the config key (the whole config, so no option can
    be forgotten), and entries computed under other settings are unreachable.

App state OUTSIDE the album folder also reaches a payload — `mlo.audit`'s
evidence store is one JSON file for the whole library, and it is what the
AUDIT/INTEGRITY verdicts are read from. A cheap stamp of the top level of the
app's own data folder is part of the signature for that reason, and every tag
write, import or script run additionally drops the affected rows through
`server.tagcache.invalidate_*` (see `drop`/`drop_all`) — the same hooks that
already drop the in-memory caches, because a write must not be served stale
from either.

The index is an OPTIMIZATION and never a source of truth: an unreadable or
locked database, a missing music folder or a malformed row all fall back to
building the payload directly, and the row is rewritten. It holds no file
contents, only the payload the API would have served.
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading
import time
from collections import OrderedDict

DB_NAME = "tagindex.sqlite"
_SCHEMA_VERSION = 1
# A row nobody has had any use for in a year is dropped by `prune`: it is the
# growth bound for entries written under a config the user has since changed
# (unreachable, still on disk) and for albums that were browsed once, long ago.
_MAX_AGE_S = 365 * 24 * 3600.0
# How stale a row's `built_at` may get before a HIT rewrites it (see
# `cached_album`): age is "last used", and a hit must not cost a disk write.
_TOUCH_S = 3600.0

# In-memory layer over the file: one build asks for an album once, but several
# builds in a row (the library payload, Home, an album page) ask again, and a
# sqlite read + JSON decode per album is worth skipping. Bounded because a
# payload is tens of KB.
_MEM_MAX = 256
_MEM: "OrderedDict[str, tuple]" = OrderedDict()   # key -> (sig, payload)

# Reentrant: the drop paths hold it while asking `_connect` for the connection,
# and every sqlite use is serialized behind it because the ONE connection is
# handed to every builder thread.
_LOCK = threading.RLock()           # guards _conn/_conn_path/_MEM/_state_cache
_conn = None                        # sqlite3.Connection, or None when disabled
_conn_path = None                   # the path _conn belongs to (or failed for)


def db_file(cfg) -> "str | None":
    """The index file for a config's music folder, or None when there is none."""
    try:
        from mlo.paths import app_data_dir

        folder = str(cfg.get("music_folder") or "")
        if not folder:
            return None
        return os.path.join(app_data_dir(folder), DB_NAME)
    except Exception:
        return None


def _connect(cfg):
    """The sqlite connection for *cfg*'s music folder, or None when unusable."""
    global _conn, _conn_path

    path = db_file(cfg)
    if not path:
        return None
    with _LOCK:
        if _conn_path == path:
            return _conn                    # open, or known-bad (None)
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            conn = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            # The tables first: reading the schema version from a database that
            # has never been written would raise, and a raise here used to
            # disable the index for the whole folder (a fresh install).
            conn.execute("CREATE TABLE IF NOT EXISTS meta "
                         "(k TEXT PRIMARY KEY, v TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS album_payload "
                         "(key TEXT PRIMARY KEY, sig TEXT NOT NULL, "
                         " payload TEXT NOT NULL, built_at REAL NOT NULL)")
            row = conn.execute("SELECT v FROM meta WHERE k = 'schema'").fetchone()
            if row and str(row[0]) != str(_SCHEMA_VERSION):
                # A payload shape this build does not understand is not a
                # payload: drop it rather than serve it wrong.
                conn.execute("DELETE FROM album_payload")
            conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('schema', ?)",
                         (str(_SCHEMA_VERSION),))
            conn.commit()
        except Exception:
            _conn, _conn_path = None, path     # disabled for this folder
            return None
        _conn, _conn_path = conn, path
        return _conn


def config_key(cfg) -> str:
    """The settings half of an index key: the WHOLE config, canonically.

    Not a list of the options that matter today — that list is what a future
    option gets forgotten from, and a payload computed under other settings
    would then be served as if it were this one's.
    """
    try:
        return json.dumps(cfg, sort_keys=True, default=str, separators=(",", ":"))
    except Exception:
        return ""


def _entry_stamp(path) -> str:
    try:
        st = os.stat(path)
        return f"{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        return "-"


# The per-library state stores whose content reaches an album payload, and how
# one album's share of each is read:
#
#   "keys"  — the map is keyed by a PATH (a track, an album folder, a .accurip),
#             so the album's share is the entries under it (and the entries it
#             sits under: an artist-level record its albums all read).
#   "whole" — the map's records do not state a path (the AcoustID submissions
#             are keyed by a fingerprint and hold no filename), so the album
#             takes the whole store's stamp. That store is written by the
#             submit script alone, not by the per-album import chain.
_STATE_STORES = (
    ("mlo.audit", "_evidence_path", "keys"),
    ("mlo.artistdata", "_provenance_path", "keys"),
    ("mlo.accurip", "_identity_path", "keys"),
    ("mlo.acoustid", "submissions_file", "whole"),
)

_state_cache = {}   # store path -> ((mtime_ns, size), prepared rows)


def _state_rows(path):
    """The store's records prepared ONCE per file revision, for `state_stamp`.

    Rows are ``(folded_key, raw_key, value)`` — the folded key to match against
    an album root, the raw key for the stamp text, and the JSON value already
    dumped and truncated. Sorting and dumping belong to the STORE, not to the
    album asking: doing them per album made one library build JSON-encode every
    record of every store once per album (and the audit store holds one record
    per audited file), then sort every key list the same number of times. Here
    each store is prepared once per (path, mtime, size) and each album only
    walks the rows.
    """
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return []
    hit = _state_cache.get(path)
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    rows = []
    for k in data:
        low = os.path.normcase(os.path.normpath(str(k or "")))
        if not low:
            continue
        try:
            value = json.dumps(data[k], sort_keys=True, default=str)
        except Exception:
            value = str(data[k])
        # Truncated: the stamp is an identity, not a copy — a store value big
        # enough to dominate the signature (a fingerprint) only has to keep
        # changing it when it changes.
        rows.append((low, str(k), value[:200]))
    rows.sort(key=lambda r: r[0])
    if len(_state_cache) > 8:
        _state_cache.clear()
    _state_cache[path] = (key, rows)
    return rows


def state_stamp(album_dir, cfg) -> str:
    """THIS album's share of the per-library state stores.

    Their change must make an index row unreachable: the audit evidence store
    decides the AUDIT/INTEGRITY verdicts a grade carries, and it is written by
    a run rather than by a file write, so no file stat would ever show it.

    Read PER ALBUM on purpose. It used to be one library-wide stamp, and the
    four stores are rewritten by the import chain's own scripts — Audit
    library, AccurateRip, Process images — once per album as an import
    proceeds. Measured on the owner's install (170 files, bind-mounted
    library): every one of those writes made EVERY album's row unreachable, so
    the library page after an import re-read the whole library from disk —
    14.5 s and 5.5 s rebuilds, twice inside one minute of otherwise warm
    40 ms reads. An album now pays only for the stores that speak about it.
    """
    try:
        root = os.path.normcase(os.path.normpath(os.path.abspath(album_dir)))
    except Exception:
        return ""
    parts = []
    for module, attr, how in _STATE_STORES:
        try:
            mod = __import__(module, fromlist=[attr])
            path = getattr(mod, attr)(cfg)
        except Exception:
            continue
        if not path:
            continue
        name = os.path.basename(path)
        if how == "whole":
            parts.append(f"{name}={_entry_stamp(path)}")
            continue
        # The store's share of THIS album: a key inside it, or one it sits
        # under (artist- and library-level records every album beneath them
        # reads). Both directions, exactly as `_shares_folder` states them.
        for low, key, value in _state_rows(path):
            if low == root or low.startswith(root + os.sep) or root.startswith(low + os.sep):
                parts.append(f"{name}:{key}={value}")
    return ";".join(parts)


def dir_signature(album_dir, cfg) -> "str | None":
    """The identity of everything `build_album` reads for one album.

    Every entry under the album folder as ``relpath|mtime_ns|size`` (directories
    carry their own mtime and no size), plus the app-state stamp. None when the
    folder cannot be listed — an unreadable folder is built directly, never
    cached.
    """
    rows = []
    stack = [("", album_dir)]
    try:
        while stack:
            rel, cur = stack.pop()
            with os.scandir(cur) as entries:
                for e in entries:
                    name = f"{rel}{e.name}" if not rel else f"{rel}/{e.name}"
                    try:
                        if e.is_dir(follow_symlinks=False):
                            st = e.stat(follow_symlinks=False)
                            rows.append(f"{name}/|{st.st_mtime_ns}")
                            stack.append((name, e.path))
                            continue
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        rows.append(f"{name}|-")
                        continue
                    rows.append(f"{name}|{st.st_mtime_ns}|{st.st_size}")
    except OSError:
        return None
    rows.sort()
    rows.append("state:" + state_stamp(album_dir, cfg))
    return "\n".join(rows)


def _key(album_dir, light) -> str:
    # Case-preserving on purpose: the payload carries the path it was built
    # for, so two spellings of one folder are two entries rather than one row
    # answering with the other's spelling.
    return f"{os.path.normpath(os.path.abspath(album_dir))}\x00{1 if light else 0}"


def cached_album(album_dir, cfg, light, builder, cfg_key=None):
    """`build_album`'s payload for *album_dir*, from the index when it matches.

    *builder* is called only when nothing matches (or when the index is
    unusable). A payload that is not there, or that reports an error, is built
    fresh and NOT stored: an absent album and a folder the OS refused to read
    are transient answers that must not be pinned.

    *cfg_key* is the caller's `config_key(cfg)` when a whole build already
    computed it: the key is a canonical dump of the whole config, and a build
    asking for it per album would pay that dump a thousand times for one
    answer that cannot change while the build runs.
    """
    sig = dir_signature(album_dir, cfg)
    if sig is None:
        return builder()
    key = _key(album_dir, light) + "\x00" + (cfg_key or config_key(cfg))
    with _LOCK:
        hit = _MEM.get(key)
        if hit and hit[0] == sig:
            _MEM.move_to_end(key)
            return copy.deepcopy(hit[1])
    conn = _connect(cfg)
    payload = None
    if conn is not None:
        try:
            with _LOCK:
                row = conn.execute(
                    "SELECT sig, payload, built_at FROM album_payload WHERE key = ?",
                    (key,)).fetchone()
            if row and str(row[0]) == sig:
                payload = json.loads(row[1])
                # `built_at` doubles as "last used", so `prune`'s age sweep
                # really is about rows nobody reads. Written at most once an
                # hour per album: a hit must not cost a disk write.
                if time.time() - float(row[2] or 0.0) > _TOUCH_S:
                    with _LOCK:
                        conn.execute(
                            "UPDATE album_payload SET built_at = ? WHERE key = ?",
                            (time.time(), key))
                        conn.commit()
        except Exception:
            payload = None
    if payload is not None:
        _remember(key, sig, payload)
        return copy.deepcopy(payload)
    payload = builder()
    if payload is None or payload.get("error") or payload.get("pending"):
        # No audio (a framework album's row is read from its wish queue, not
        # from any file), or an unreadable folder: cheap to build and not worth
        # pinning to a signature that says nothing about the queue.
        return payload
    if conn is not None:
        try:
            with _LOCK:
                conn.execute(
                    "INSERT OR REPLACE INTO album_payload "
                    "(key, sig, payload, built_at) VALUES (?, ?, ?, ?)",
                    (key, sig, json.dumps(payload, default=str), time.time()))
                conn.commit()
        except Exception:
            pass
    _remember(key, sig, payload)
    return payload


def _remember(key, sig, payload) -> None:
    with _LOCK:
        _MEM[key] = (sig, copy.deepcopy(payload))
        _MEM.move_to_end(key)
        while len(_MEM) > _MEM_MAX:
            _MEM.popitem(last=False)


def _drop_keys(keys) -> None:
    if not keys:
        return
    with _LOCK:
        if _conn is not None:
            try:
                _conn.executemany("DELETE FROM album_payload WHERE key = ?",
                                  [(k,) for k in keys])
                _conn.commit()
            except Exception:
                pass


def drop(folders) -> None:
    """Forget the rows of one or more folders (a write to them).

    The album-page keys are the folder itself; the artist page's payload is not
    indexed here (it is the albums' payloads that are), so overlapping either
    way is a superset of what went stale, which is the safe direction.
    """
    roots = [os.path.normcase(os.path.normpath(os.path.abspath(str(f))))
             for f in (folders or []) if str(f or "").strip()]
    if not roots:
        return
    with _LOCK:
        keys = [k for k in _MEM if _inside_key(k, roots)]
        for k in keys:
            _MEM.pop(k, None)
        if _conn is not None:
            try:
                rows = _conn.execute("SELECT key FROM album_payload").fetchall()
                stale = [str(r[0]) for r in rows if _inside_key(str(r[0]), roots)]
                if stale:
                    _conn.executemany("DELETE FROM album_payload WHERE key = ?",
                                      [(k,) for k in stale])
                    _conn.commit()
            except Exception:
                pass


def _inside_key(key, roots) -> bool:
    path = key.split("\x00", 1)[0]
    low = os.path.normcase(path)
    for r in roots:
        if low == r or low.startswith(r + os.sep):
            return True
    return False


def drop_all(cfg=None) -> None:
    """Forget every row — the Refresh button and a full invalidation."""
    with _LOCK:
        _MEM.clear()
        conn = _conn if _conn is not None else (_connect(cfg) if cfg else None)
        if conn is not None:
            try:
                conn.execute("DELETE FROM album_payload")
                conn.commit()
            except Exception:
                pass


def prune(cfg) -> None:
    """Drop rows whose album folder is gone, and rows nobody has used in a year.

    Only rows whose path no longer exists on disk go for the first rule: a
    folder that is merely not in the current walk (a staged album outside the
    library) is somebody's album and keeps its row.

    The second rule is the growth bound. Entries computed under a config the
    user has since changed are unreachable but still on disk, and a library
    that is browsed album by album for years would otherwise keep a row per
    folder it ever saw. A year-old row costs one re-read if that album comes
    back, which is cheaper than an index that grows without limit.
    """
    with _LOCK:
        keys = list(_MEM.keys())
        for k in keys:
            if not os.path.isdir(k.split("\x00", 1)[0]):
                _MEM.pop(k, None)
    conn = _connect(cfg)
    if conn is None:
        return
    try:
        with _LOCK:
            rows = conn.execute("SELECT key FROM album_payload").fetchall()
        dead = [str(r[0]) for r in rows
                if not os.path.isdir(str(r[0]).split("\x00", 1)[0])]
        _drop_keys(dead)
        with _LOCK:
            conn.execute("DELETE FROM album_payload WHERE built_at < ?",
                         (time.time() - _MAX_AGE_S,))
            conn.commit()
    except Exception:
        pass
