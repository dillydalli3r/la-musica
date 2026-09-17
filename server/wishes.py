"""Wishes — a MusicBrainz release wishlist that auto-fills from Soulseek.

A *wish* is a MusicBrainz release the user wants but that isn't available on
the network right now. Saving it to the library as a wish records the release
identity without downloading anything; a background worker then re-searches
Soulseek for every open wish on an interval and, the moment a verified match
appears, runs the exact same find → verify → download → audit → import
pipeline as the one-shot auto-importer (see server/soulseek_auto.py). The wish
flips to ``imported`` when it lands.

The wishlist lives in its own SQLite database next to playlists.db, so it
survives restarts and travels with the ``.mlo/data`` folder.
"""
import json
import os
import sqlite3
import threading
import time

# Reentrant: add_wish()/delete_wish() hold the lock while calling _conn(), and
# on FIRST use _conn() creates the schema, which takes this same lock. With a
# plain Lock that was a self-deadlock — the first wish ever added in a process
# (a fresh install, or an auto-import job whose user accepted the "add to
# wishes?" offer) parked its thread forever, holding the lock every later
# wishes call needed. Same trap server.soulseek_auto documents for its RLock.
_lock = threading.RLock()

STATUSES = ("wanted", "searching", "imported", "failed", "available")


def db_path():
    from mlo.paths import app_data_dir
    return os.path.join(app_data_dir(), "wishes.db")


_init_lock = threading.Lock()
_initialized = False


def _conn():
    global _initialized
    os.makedirs(os.path.dirname(db_path()), exist_ok=True)
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    # Schema on FIRST USE, not on import: creating the file at import time
    # planted an empty wishes.db in the state dir before the migration could
    # move the user's real one in (see mlo.config._move_state_dir).
    if not _initialized:
        with _init_lock:
            if not _initialized:
                _initialized = True  # set first — _init() re-enters _conn()
                try:
                    _init()
                except Exception:
                    _initialized = False
                    raise
    return conn


def _init():
    with _lock:
        with _conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS wishes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    release_mbid TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL DEFAULT '',
                    artist TEXT NOT NULL DEFAULT '',
                    year TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'wanted',
                    note TEXT NOT NULL DEFAULT '',
                    target_dir TEXT NOT NULL DEFAULT '',
                    quotes TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    added_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_search REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    album_path TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS wish_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    t REAL NOT NULL,
                    level TEXT NOT NULL DEFAULT 'info',
                    msg TEXT NOT NULL
                );
                """
            )


# --------------------------------------------------------------------------- #
# Log
# --------------------------------------------------------------------------- #
_MAX_LOG = 300


def log(level, msg):
    with _lock:
        with _conn() as c:
            c.execute("INSERT INTO wish_log (t, level, msg) VALUES (?,?,?)",
                      (time.time(), str(level), str(msg)[:500]))
            c.execute(
                "DELETE FROM wish_log WHERE id NOT IN "
                "(SELECT id FROM wish_log ORDER BY id DESC LIMIT ?)", (_MAX_LOG,))


def read_log(limit=80):
    with _conn() as c:
        rows = c.execute("SELECT t, level, msg FROM wish_log ORDER BY id DESC LIMIT ?",
                         (int(limit),)).fetchall()
    return [{"t": r["t"], "level": r["level"], "msg": r["msg"]} for r in reversed(rows)]


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def _row(r):
    d = dict(r)
    if d.get("quotes"):
        try:
            d["queries"] = json.loads(d["quotes"])
        except Exception:
            d["queries"] = []
    else:
        d["queries"] = []
    d.pop("quotes", None)
    return d


def list_wishes():
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM wishes ORDER BY "
            "CASE status WHEN 'wanted' THEN 0 WHEN 'searching' THEN 1 "
            "WHEN 'failed' THEN 2 ELSE 3 END, added_at DESC"
        ).fetchall()
    return [_row(r) for r in rows]


def get_wish(wid):
    with _conn() as c:
        r = c.execute("SELECT * FROM wishes WHERE id=?", (int(wid),)).fetchone()
    return _row(r) if r else None


def add_wish(release_mbid, title="", artist="", year="", note="",
             target_dir="", queries=None):
    release_mbid = str(release_mbid or "").strip()
    if not release_mbid:
        raise ValueError("release_mbid required")
    now = time.time()
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT * FROM wishes WHERE release_mbid=?",
                            (release_mbid,)).fetchone()
            if row:
                return _row(row)  # already wished — idempotent
            cur = c.execute(
                "INSERT INTO wishes (release_mbid, title, artist, year, status, note,"
                " target_dir, quotes, added_at, updated_at)"
                " VALUES (?,?,?,?,'wanted',?,?,?,?,?)",
                (release_mbid, str(title or ""), str(artist or ""), str(year or ""),
                 str(note or ""), str(target_dir or ""),
                 json.dumps(queries) if queries else "", now, now),
            )
            wid = cur.lastrowid
    log("info", f"Wish added: {artist} — {title} ({release_mbid[:8]})")
    return get_wish(wid)


def update_wish(wid, fields):
    allowed = {"note", "target_dir", "status", "queries", "title", "artist", "year"}
    sets, vals = [], []
    for k, v in (fields or {}).items():
        if k not in allowed:
            continue
        if k == "queries":
            sets.append("quotes=?")
            vals.append(json.dumps(v) if v else "")
        elif k == "status":
            if v not in STATUSES:
                continue
            sets.append("status=?")
            vals.append(v)
        else:
            sets.append(f"{k}=?")
            vals.append(str(v or ""))
    if not sets:
        return get_wish(wid)
    sets.append("updated_at=?")
    vals.append(time.time())
    vals.append(int(wid))
    with _lock:
        with _conn() as c:
            cur = c.execute(f"UPDATE wishes SET {', '.join(sets)} WHERE id=?", vals)
            if cur.rowcount == 0:
                return None
    return get_wish(wid)


def delete_wish(wid):
    with _lock:
        with _conn() as c:
            cur = c.execute("DELETE FROM wishes WHERE id=?", (int(wid),))
            return cur.rowcount > 0


def _mark(wid, **fields):
    if not fields:
        return
    sets = [f"{k}=?" for k in fields]
    vals = list(fields.values())
    sets.append("updated_at=?")
    vals.append(time.time())
    vals.append(int(wid))
    with _lock:
        with _conn() as c:
            c.execute(f"UPDATE wishes SET {', '.join(sets)} WHERE id=?", vals)


def mark_searching(wid):
    _mark(wid, status="searching", last_search=time.time())


def mark_imported(wid, album_path):
    _mark(wid, status="imported", album_path=str(album_path or ""), last_error="")
    w = get_wish(wid)
    if w:
        log("ok", f"Wish filled: {w['artist']} — {w['title']} → {album_path}")


def mark_failed(wid, error, attempts):
    _mark(wid, status="failed", last_error=str(error or "")[:400], attempts=attempts)


def mark_wanted(wid, error="", attempts=None):
    fields = {"status": "wanted", "last_error": str(error or "")[:400]}
    if attempts is not None:
        fields["attempts"] = attempts
    _mark(wid, **fields)


# --------------------------------------------------------------------------- #
# Reconciliation — a wish may be filled out-of-band (manual download/import)
# --------------------------------------------------------------------------- #
def owned_mbids(cfg=None):
    """{mbid: album path} for every MusicBrainz release/release-group the
    library already holds (matched on the album's MBID tags). Used by wish
    reconciliation and by artist-level bulk import to skip what is owned."""
    from mlo.config import load_config

    cfg = cfg or load_config()
    try:
        from server import library as lib_mod
        lib = lib_mod.build_library(cfg)
    except Exception:
        return {}
    owned = {}
    for artist in lib.get("artists", []):
        for alb in artist.get("albums", []):
            meta = alb.get("meta") or {}
            for key in ("MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_RELEASEGROUPID"):
                val = str(meta.get(key) or "").strip().lower()
                if val:
                    owned.setdefault(val, alb.get("path"))
    return owned


def reconcile_with_library(cfg=None):
    """Mark open wishes whose MusicBrainz release is already in the library as
    imported. Returns the count of freshly resolved wishes.

    Matches the release ID against the library's ``MUSICBRAINZ_ALBUMID`` tags
    (or its release-group id) so a wish saved from a release-group page still
    resolves. Cheap enough to run at the end of every worker cycle.
    """
    owned = owned_mbids(cfg)
    resolved = 0
    for w in list_wishes():
        if w["status"] in ("imported",):
            continue
        mbid = str(w["release_mbid"]).strip().lower()
        if mbid and mbid in owned:
            mark_imported(w["id"], owned[mbid] or "")
            resolved += 1
    return resolved
