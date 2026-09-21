"""Playlists for la musica v2.

Dual storage:
  * SQLite (<music folder>/.mlo/data/playlists.db) — fast UI, smart
    playlists, ordering.
  * .m3u8 export/import — portable standard format for other players.

Playlist kinds:
  * manual — explicit ordered list of track paths.
  * smart  — saved filter (JSON) re-evaluated against the library payload.

Every row (playlists, their tracks, likes, favorites) belongs to a user and
every read/write filters on it, so two people on one server never see each
other's data. `user=""` is the default/admin scope: the rows written before
users existed, and what an install that has never claimed a user works in.
"""
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

# Re-entrant: a write holds it and then opens the connection, whose first call
# runs `_init()` — under this same lock. A plain Lock deadlocked the very first
# write on a fresh database (it waited out sqlite's 30 s timeout, then hung).
_lock = threading.RLock()


def db_path():
    """Playlists + likes live in <music folder>/.mlo/data/playlists.db."""
    from mlo.paths import app_data_dir
    return os.path.join(app_data_dir(), "playlists.db")


_init_lock = threading.Lock()
_initialized = False


def _conn():
    global _initialized
    os.makedirs(os.path.dirname(db_path()), exist_ok=True)
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    # Schema on FIRST USE, not on import: creating the file at import time
    # planted an empty playlists.db in the state dir before the migration
    # could move the user's real one in (see mlo.config._move_state_dir).
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
                CREATE TABLE IF NOT EXISTS playlists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'manual',
                    filter_json TEXT,
                    created REAL NOT NULL,
                    updated REAL NOT NULL
                );
                """)
            # lightweight migration: the old custom-icon + complete-grade
            # columns are dropped — covers are derived from the tracks now.
            cols = {r[1] for r in c.execute("PRAGMA table_info(playlists)")}
            for legacy in ("icon", "complete"):
                if legacy in cols:
                    c.execute(f"ALTER TABLE playlists DROP COLUMN {legacy}")
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS playlist_tracks (
                    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    PRIMARY KEY (playlist_id, path)
                );
                CREATE TABLE IF NOT EXISTS likes (
                    path TEXT PRIMARY KEY,
                    liked_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS favorites (
                    kind TEXT NOT NULL,
                    key TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (kind, key)
                );
                """
            )
            # MusicBrainz identity columns — added in-place so existing
            # databases keep working. Entries with an MBID survive file
            # moves: reads resolve the current path from the MBID and
            # re-point the stored path (self-healing).
            for stmt in (
                "ALTER TABLE likes ADD COLUMN mbid TEXT",
                "ALTER TABLE favorites ADD COLUMN mbid TEXT",
                "ALTER TABLE playlist_tracks ADD COLUMN mbid TEXT",
            ):
                try:
                    c.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # column already exists
            # Per-user scope, added in place: every row predating it has no
            # scope of its own and becomes the default one (''), so an
            # existing install keeps its playlists, likes and favorites.
            for table in ("playlists", "playlist_tracks", "likes", "favorites"):
                try:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN user TEXT NOT NULL DEFAULT ''")
                except sqlite3.OperationalError:
                    pass  # column already exists
            # `likes`/`favorites` were keyed by the entity alone, and on a
            # shared server two users may hold the same track or album, so the
            # key has to start with the user. SQLite cannot change a key in
            # place, so those two are rebuilt once, empty scope carried over.
            _ensure_user_key(c, "likes", ("path", "liked_at", "mbid"), """
                user TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL,
                liked_at REAL NOT NULL,
                mbid TEXT,
                PRIMARY KEY (user, path)
            """)
            _ensure_user_key(c, "favorites", ("kind", "key", "created_at", "mbid"), """
                user TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL,
                key TEXT NOT NULL,
                created_at REAL NOT NULL,
                mbid TEXT,
                PRIMARY KEY (user, kind, key)
            """)


def _ensure_user_key(c, table, columns, body):
    """Rebuild `table` when its primary key does not start with `user`.

    Called once per process on the tables above; an already-migrated database
    matches `user` in the key and is left alone.
    """
    if any(r[1] == "user" and r[5] for r in c.execute(f"PRAGMA table_info({table})")):
        return
    new = f"{table}_user"
    cols = ", ".join(columns)
    c.execute(f"CREATE TABLE {new} ({body})")
    c.execute(f"INSERT INTO {new} (user, {cols}) SELECT '', {cols} FROM {table}")
    c.execute(f"DROP TABLE {table}")
    c.execute(f"ALTER TABLE {new} RENAME TO {table}")


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def list_playlists(user=""):
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM playlists WHERE user=? ORDER BY name COLLATE NOCASE", (user,)
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["filter"] = json.loads(item.pop("filter_json")) if item.get("filter_json") else None
            n = c.execute("SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id=? AND user=?",
                          (item["id"], user)).fetchone()[0]
            item["track_count"] = n
            out.append(item)
        return out


def get_playlist(pid, user=""):
    from server import mbresolve
    with _conn() as c:
        r = c.execute("SELECT * FROM playlists WHERE id=? AND user=?", (pid, user)).fetchone()
        if r is None:
            return None
        item = dict(r)
        item["filter"] = json.loads(item.pop("filter_json")) if item.get("filter_json") else None
        rows = c.execute(
            "SELECT path, mbid FROM playlist_tracks WHERE playlist_id=? AND user=? ORDER BY position",
            (pid, user),
        ).fetchall()
    tracks = []
    updates = []
    for x in rows:
        path = x["path"]
        mbid = x["mbid"] or ""
        cur = mbresolve.heal_row("track", path, mbid)
        if mbid and cur and _pathkey(cur) != _pathkey(path):
            cur = os.path.normpath(cur)  # store the same form the API writes
            updates.append((cur, path))
            path = cur
        tracks.append(path.replace("\\", "/"))
    if updates:
        with _lock:
            with _conn() as c:
                for new, old in updates:
                    c.execute("UPDATE playlist_tracks SET path=?"
                              " WHERE playlist_id=? AND path=? AND user=?",
                              (new, pid, old, user))
    item["tracks"] = tracks
    return item


def create_playlist(name, kind="manual", filter_spec=None, user=""):
    with _lock:
        with _conn() as c:
            now = time.time()
            cur = c.execute(
                "INSERT INTO playlists (name, kind, filter_json, created, updated, user)"
                " VALUES (?,?,?,?,?,?)",
                (name, kind, json.dumps(filter_spec) if filter_spec else None, now, now, user),
            )
            return cur.lastrowid


def update_playlist(pid, fields, user=""):
    """Partial update from a dict of ONLY the fields being changed — rename,
    set the cover icon (None clears it back to the default), and/or grade
    the playlist complete."""
    if not fields:
        return get_playlist(pid, user)
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT id FROM playlists WHERE id=? AND user=?",
                            (pid, user)).fetchone()
            if not row:
                return None
            sets, vals = [], []
            if "name" in fields and str(fields["name"] or "").strip():
                sets.append("name=?")
                vals.append(str(fields["name"]).strip())
            if not sets:
                return get_playlist(pid, user)
            sets.append("updated=?")
            vals.append(time.time())
            vals.extend((pid, user))
            c.execute(f"UPDATE playlists SET {', '.join(sets)} WHERE id=? AND user=?", vals)
            return get_playlist(pid, user)


def rename_playlist(pid, name, user=""):
    with _conn() as c:
        cur = c.execute("UPDATE playlists SET name=?, updated=? WHERE id=? AND user=?",
                        (name, time.time(), pid, user))
        return cur.rowcount > 0


def delete_playlist(pid, user=""):
    with _lock:
        with _conn() as c:
            c.execute("DELETE FROM playlist_tracks WHERE playlist_id=? AND user=?", (pid, user))
            cur = c.execute("DELETE FROM playlists WHERE id=? AND user=?", (pid, user))
            # rowcount lives on the cursor, not the connection
            return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Manual playlist tracks
# --------------------------------------------------------------------------- #
def _pathkey(path):
    """Comparison key for stored track paths.

    Rows written by the API hold ``os.path.normpath`` (backslash on
    Windows), while paths healed from the library payload are forward
    slashed — every match/delete compares under one normalization."""
    return os.path.normcase(os.path.normpath(str(path)))


def _owned(c, pid, user):
    """Whether `pid` is a playlist of `user`'s.

    The track writers check this first: a playlist id from another user must
    leave no row behind, on top of being invisible to every read.
    """
    return c.execute("SELECT 1 FROM playlists WHERE id=? AND user=?",
                     (pid, user)).fetchone() is not None


def _mbid_for(path):
    """Recording MBID for a library track path ("" when untagged)."""
    try:
        from server import mbresolve
        return mbresolve.track_mbid_for(path)
    except Exception:
        return ""


def add_tracks(pid, paths, position=None, user=""):
    """Append (or insert at position) track paths, deduplicating. The
    track's MusicBrainz recording ID is stored alongside the path so the
    entry survives later reorganizations."""
    with _lock:
        with _conn() as c:
            if not _owned(c, pid, user):
                return 0
            existing = {_pathkey(r["path"]) for r in c.execute(
                "SELECT path FROM playlist_tracks WHERE playlist_id=? AND user=?", (pid, user))}
            new = []
            for p in dict.fromkeys(paths):  # dedupe within the request too
                k = _pathkey(p)
                if k in existing:
                    continue
                existing.add(k)
                new.append(p)
            if not new:
                return 0
            if position is None:
                base = c.execute("SELECT COALESCE(MAX(position),0) FROM playlist_tracks"
                                 " WHERE playlist_id=? AND user=?",
                                 (pid, user)).fetchone()[0]
                start = base + 1
                for i, p in enumerate(new):
                    c.execute("INSERT INTO playlist_tracks"
                              " (playlist_id, path, position, mbid, user) VALUES (?,?,?,?,?)",
                              (pid, p, start + i, _mbid_for(p), user))
            else:
                c.execute("UPDATE playlist_tracks SET position = position + ?"
                          " WHERE playlist_id=? AND position >= ? AND user=?",
                          (len(new), pid, position, user))
                for i, p in enumerate(new):
                    c.execute("INSERT INTO playlist_tracks"
                              " (playlist_id, path, position, mbid, user) VALUES (?,?,?,?,?)",
                              (pid, p, position + i, _mbid_for(p), user))
            c.execute("UPDATE playlists SET updated=? WHERE id=? AND user=?",
                      (time.time(), pid, user))
            return len(new)


def set_order(pid, paths, user=""):
    """Replace the entire ordering with `paths` (reorder / full replace)."""
    with _lock:
        with _conn() as c:
            if not _owned(c, pid, user):
                return
            known = {}
            for r in c.execute("SELECT path, mbid FROM playlist_tracks"
                               " WHERE playlist_id=? AND user=?", (pid, user)):
                known.setdefault(_pathkey(r["path"]), r["mbid"] or "")
            c.execute("DELETE FROM playlist_tracks WHERE playlist_id=? AND user=?", (pid, user))
            for i, p in enumerate(paths):
                mbid = known.get(_pathkey(p)) or _mbid_for(p)
                c.execute("INSERT INTO playlist_tracks"
                          " (playlist_id, path, position, mbid, user) VALUES (?,?,?,?,?)",
                          (pid, p, i, mbid, user))
            c.execute("UPDATE playlists SET updated=? WHERE id=? AND user=?",
                      (time.time(), pid, user))


def remove_tracks(pid, paths, user=""):
    with _lock:
        with _conn() as c:
            if not _owned(c, pid, user):
                return
            stored = [r["path"] for r in c.execute(
                "SELECT path FROM playlist_tracks WHERE playlist_id=? AND user=?", (pid, user))]
            for p in paths:
                k = _pathkey(p)
                for s in stored:
                    if _pathkey(s) == k:
                        # DELETE with the value as stored, not as requested
                        c.execute("DELETE FROM playlist_tracks"
                                  " WHERE playlist_id=? AND path=? AND user=?",
                                  (pid, s, user))
            c.execute("UPDATE playlists SET updated=? WHERE id=? AND user=?",
                      (time.time(), pid, user))


# --------------------------------------------------------------------------- #
# Smart playlists
# --------------------------------------------------------------------------- #
# The evaluator lives in `mlo.query` now (the condition language, the operator
# semantics, the walk) because the library browser asks the same questions of
# the same payload — a saved smart playlist and an ad-hoc query may never
# disagree about what matches. This module keeps only playlist storage.
def _uses_rating(spec):
    """Whether the spec compares one of the rating fields.

    `tags.RATING` is a TEXT tag comparison (Picard's 0-100 scale) and must not
    drag the store in: only the fields the store answers for do — the track's
    own `rating` and the two folder scopes (`album.rating`, `artist.rating`,
    the user's verdict on that album or artist)."""
    from mlo import query as query_mod
    for cond in (spec or {}).get("conditions") or ():
        if str((cond or {}).get("field") or "") in query_mod.RATING_FIELDS:
            return True
    return False


def _rating_of(user, spec):
    """The rating store adapter, for a spec that actually asks about rating.

    Built lazily and only then: a spec with no rating condition must not pay
    for a store read (the engine calls this per row, and the first call pays
    for the whole map). It answers all three scopes — the callable for tracks
    and its `folder` half for `album.rating`/`artist.rating` — so a saved rule
    filters on the user's own verdict whichever entity it names."""
    if not _uses_rating(spec):
        return None
    from server.api_query import rating_source
    return rating_source(user)


def evaluate_smart(pid, library, base_paths=None, user=""):
    """Evaluate a smart playlist against the library payload.

    filter spec: {"conditions": [{field, op, value}], "match": "all"|"any"}
    Returns ordered list of matching track paths (respecting optional base_paths).
    """
    from mlo import query as query_mod
    from server.api_query import bool_fields

    pl = get_playlist(pid, user)
    if pl is None or pl["kind"] != "smart":
        return None
    spec = pl.get("filter") or {}
    return query_mod.match_paths(library, spec, base_paths,
                                 rating_of=_rating_of(user, spec),
                                 bool_fields=bool_fields())


def set_smart_filter(pid, filter_spec, user=""):
    with _lock:
        with _conn() as c:
            cur = c.execute("UPDATE playlists SET filter_json=?, updated=? WHERE id=? AND user=?",
                            (json.dumps(filter_spec), time.time(), pid, user))
            return cur.rowcount > 0  # rowcount lives on the cursor


# --------------------------------------------------------------------------- #
# .m3u8 export / import
# --------------------------------------------------------------------------- #
def export_m3u8(pid, user=""):
    """Render a playlist as an .m3u8 string.

    Each entry carries its tagged title and — when known — the track's
    MusicBrainz recording MBID in an ``#MLO-MBID:`` comment, so re-import
    (or another app honoring the convention) can follow the recording even
    after the library has been reorganized."""
    pl = get_playlist(pid, user)
    if pl is None:
        return None
    with _conn() as c:
        # Stored rows use os.path.normpath (backslash on Windows) while
        # pl["tracks"] is forward-slashed — match under one normalization.
        rows = {_pathkey(r["path"]): (r["mbid"] or "") for r in c.execute(
            "SELECT path, mbid FROM playlist_tracks WHERE playlist_id=? AND user=?",
            (pid, user))}
    lines = ["#EXTM3U"]
    for path in pl["tracks"]:
        dur = _duration_of(path)
        title = ""
        try:
            from mlo.audio import AudioFile
            af = AudioFile(path)
            if af.audio is not None:
                artist = str(af.get_tag("ARTIST") or "").strip()
                t = str(af.get_tag("TITLE") or "").strip()
                if t:
                    title = f"{artist} - {t}" if artist else t
        except Exception:
            pass
        title = title or os.path.splitext(os.path.basename(path))[0]
        lines.append(f"#EXTINF:{dur:.0f},{title}")
        norm = path.replace("\\", "/")
        mbid = rows.get(_pathkey(path)) or ""
        if mbid:
            lines.append(f"#MLO-MBID:{mbid}")
        lines.append(norm)
    return "\n".join(lines) + "\n"


def import_m3u8(name, content, base_dir=None, user=""):
    """Parse .m3u8 content into a new manual playlist. Returns playlist id.

    Relative paths resolve against base_dir; ``#MLO-MBID:`` comments attach
    the recording MBID to each entry so the playlist survives moves."""
    from server import mbresolve

    base_dir = base_dir or ""
    found = []           # paths that exist right now
    mbids = []           # parallel MBID list ("" when unknown)
    missing_by_mbid = []  # lines whose file is gone but carry an MBID
    pending_mbid = ""
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#MLO-MBID:"):
            pending_mbid = line.split(":", 1)[1].strip()
            continue
        if line.startswith("#"):
            continue
        p = line
        if not os.path.isabs(p):
            p = os.path.join(base_dir, p)
        if os.path.isfile(p):
            found.append(os.path.normpath(p))
            mbids.append(pending_mbid)
        elif pending_mbid:
            missing_by_mbid.append(pending_mbid)
        pending_mbid = ""
    pid = create_playlist(name, kind="manual", user=user)
    add_tracks(pid, found, user=user)
    # stamp explicit MBIDs + heal vanished paths via the tag index
    with _lock:
        with _conn() as c:
            for p, mbid in zip(found, mbids):
                if mbid:
                    c.execute("UPDATE playlist_tracks SET mbid=?"
                              " WHERE playlist_id=? AND path=? AND user=?",
                              (mbid, pid, p, user))
            for mbid in missing_by_mbid:
                healed = mbresolve.heal_row("track", "", mbid)
                if healed and os.path.isfile(healed):
                    healed = os.path.normpath(healed)  # same form as other writers
                    maxpos = c.execute(
                        "SELECT COALESCE(MAX(position),-1)+1 FROM playlist_tracks"
                        " WHERE playlist_id=? AND user=?",
                        (pid, user)).fetchone()[0]
                    c.execute("INSERT INTO playlist_tracks"
                              " (playlist_id, path, position, mbid, user) VALUES (?,?,?,?,?)",
                              (pid, healed, maxpos, mbid, user))
    return pid


def _duration_of(path):
    try:
        from mlo.audio import AudioFile
        af = AudioFile(path)
        if af.audio is not None and af.audio.info is not None:
            return float(af.audio.info.length)
    except Exception:
        pass
    return 0.0

# --------------------------------------------------------------------------- #
# Liked tracks (heart)
# --------------------------------------------------------------------------- #
def list_likes(user=""):
    """Paths of all liked tracks, newest first, forward-slash normalized.

    Rows that carry a MusicBrainz recording ID resolve against the live
    library first: if the file moved, the stored path is re-pointed so the
    like survives reorganizations.
    """
    from server import mbresolve
    out = []
    with _conn() as c:
        rows = c.execute("SELECT path, mbid FROM likes WHERE user=? ORDER BY liked_at DESC",
                         (user,)).fetchall()
    for r in rows:
        path = r["path"]
        mbid = (r["mbid"] if "mbid" in r.keys() else None) or ""
        cur = mbresolve.heal_row("track", path, mbid)
        if mbid and cur and os.path.normpath(cur) != os.path.normpath(path):
            with _lock:
                with _conn() as c2:
                    c2.execute("UPDATE likes SET path=? WHERE path=? AND user=?",
                               (cur, path, user))
            path = cur
        out.append(path.replace("\\", "/"))
    return out


def is_liked(path, user=""):
    with _conn() as c:
        return c.execute("SELECT 1 FROM likes WHERE path=? AND user=?",
                         (path, user)).fetchone() is not None


def toggle_like(path, mbid=None, user=""):
    path = str(path or "").strip()
    if not path:
        raise ValueError("path required")
    with _lock:
        with _conn() as c:
            if c.execute("SELECT 1 FROM likes WHERE path=? AND user=?",
                         (path, user)).fetchone():
                c.execute("DELETE FROM likes WHERE path=? AND user=?", (path, user))
                return False
            c.execute(
                "INSERT INTO likes (path, liked_at, mbid, user) VALUES (?, ?, ?, ?)",
                (path, time.time(), str(mbid or "").strip() or None, user),
            )
            return True


# --------------------------------------------------------------------------- #
# Favorite albums / artists / playlists (sidebar Favorites section)
# --------------------------------------------------------------------------- #
FAV_KINDS = ("album", "artist", "playlist")


def list_favorites(user=""):
    """Favorites per kind, newest first, keyed by current path (or playlist
    id as a string). Response keys are plural (albums/artists/playlists) to
    match the frontend contract; stored kinds are singular. Rows carrying a
    MusicBrainz ID resolve against the live library and self-heal their
    stored path after the files were reorganized."""
    from server import mbresolve
    plural = {"album": "albums", "artist": "artists", "playlist": "playlists"}
    out = {p: [] for p in plural.values()}
    seen = set()
    with _conn() as c:
        rows = c.execute("SELECT kind, key, mbid FROM favorites WHERE user=? ORDER BY created_at DESC",
                         (user,)).fetchall()
    for r in rows:
        kind = r["kind"]
        p = plural.get(kind)
        if not p:
            continue
        key = r["key"]
        mbid = r["mbid"] or ""
        if kind in ("album", "artist"):
            cur = mbresolve.heal_row(kind, key, mbid)
            if mbid and cur and os.path.normpath(cur) != os.path.normpath(key):
                with _lock:
                    with _conn() as c2:
                        c2.execute(
                            "UPDATE favorites SET key=? WHERE kind=? AND key=? AND user=?",
                            (cur, kind, key, user),
                        )
                key = cur
        key = key.replace("\\", "/")
        # The same entity may exist under both separator variants; the
        # healed path is canonical, so drop duplicates.
        dedupe = f"{kind}:{os.path.normcase(key)}"
        if dedupe in seen:
            with _lock:
                with _conn() as c2:
                    c2.execute("DELETE FROM favorites WHERE kind=? AND key=? AND user=?",
                               (kind, r["key"], user))
            continue
        seen.add(dedupe)
        out[p].append(key)
    return out


def toggle_favorite(kind, key, mbid=None, user=""):
    kind = str(kind or "").strip().lower()
    key = str(key or "").strip()
    if kind not in FAV_KINDS:
        raise ValueError("kind must be one of: " + ", ".join(FAV_KINDS))
    if not key:
        raise ValueError("key required")
    with _lock:
        with _conn() as c:
            if c.execute("SELECT 1 FROM favorites WHERE kind=? AND key=? AND user=?",
                         (kind, key, user)).fetchone():
                c.execute("DELETE FROM favorites WHERE kind=? AND key=? AND user=?",
                          (kind, key, user))
                return False
            c.execute(
                "INSERT INTO favorites (kind, key, created_at, mbid, user) VALUES (?, ?, ?, ?, ?)",
                (kind, key, time.time(), str(mbid or "").strip() or None, user),
            )
            return True
