"""Playlists for la musica v2.

Dual storage:
  * SQLite (server/data/playlists.db) — fast UI, smart playlists, ordering.
  * .m3u8 export/import — portable standard format for other players.

Playlist kinds:
  * manual — explicit ordered list of track paths.
  * smart  — saved filter (JSON) re-evaluated against the library payload.
"""
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

_lock = threading.Lock()


def db_path():
    """Playlists + likes live in <music folder>/.data/playlists.db."""
    from mlo.paths import app_data_dir
    return os.path.join(app_data_dir(), "playlists.db")


def _conn():
    os.makedirs(os.path.dirname(db_path()), exist_ok=True)
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
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


_init()


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def list_playlists():
    with _conn() as c:
        rows = c.execute("SELECT * FROM playlists ORDER BY name COLLATE NOCASE").fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["filter"] = json.loads(item.pop("filter_json")) if item.get("filter_json") else None
            n = c.execute("SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id=?", (item["id"],)).fetchone()[0]
            item["track_count"] = n
            out.append(item)
        return out


def get_playlist(pid):
    from server import mbresolve
    with _conn() as c:
        r = c.execute("SELECT * FROM playlists WHERE id=?", (pid,)).fetchone()
        if r is None:
            return None
        item = dict(r)
        item["filter"] = json.loads(item.pop("filter_json")) if item.get("filter_json") else None
        rows = c.execute(
            "SELECT path, mbid FROM playlist_tracks WHERE playlist_id=? ORDER BY position", (pid,)
        ).fetchall()
    tracks = []
    updates = []
    for x in rows:
        path = x["path"]
        mbid = x["mbid"] or ""
        cur = mbresolve.heal_row("track", path, mbid)
        if mbid and cur and os.path.normpath(cur) != os.path.normpath(path):
            updates.append((cur, path))
            path = cur
        tracks.append(path.replace("\\", "/"))
    if updates:
        with _lock:
            with _conn() as c:
                for new, old in updates:
                    c.execute("UPDATE playlist_tracks SET path=? WHERE playlist_id=? AND path=?",
                              (new, pid, old))
    item["tracks"] = tracks
    return item


def create_playlist(name, kind="manual", filter_spec=None):
    with _lock:
        with _conn() as c:
            now = time.time()
            cur = c.execute(
                "INSERT INTO playlists (name, kind, filter_json, created, updated) VALUES (?,?,?,?,?)",
                (name, kind, json.dumps(filter_spec) if filter_spec else None, now, now),
            )
            return cur.lastrowid


def update_playlist(pid, fields):
    """Partial update from a dict of ONLY the fields being changed — rename,
    set the cover icon (None clears it back to the default), and/or grade
    the playlist complete."""
    if not fields:
        return get_playlist(pid)
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT id FROM playlists WHERE id=?", (pid,)).fetchone()
            if not row:
                return None
            sets, vals = [], []
            if "name" in fields and str(fields["name"] or "").strip():
                sets.append("name=?")
                vals.append(str(fields["name"]).strip())
            if not sets:
                return get_playlist(pid)
            sets.append("updated=?")
            vals.append(time.time())
            vals.append(pid)
            c.execute(f"UPDATE playlists SET {', '.join(sets)} WHERE id=?", vals)
            return get_playlist(pid)


def rename_playlist(pid, name):
    with _conn() as c:
        cur = c.execute("UPDATE playlists SET name=?, updated=? WHERE id=?", (name, time.time(), pid))
        return cur.rowcount > 0


def delete_playlist(pid):
    with _lock:
        with _conn() as c:
            c.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (pid,))
            cur = c.execute("DELETE FROM playlists WHERE id=?", (pid,))
            # rowcount lives on the cursor, not the connection
            return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Manual playlist tracks
# --------------------------------------------------------------------------- #
def _mbid_for(path):
    """Recording MBID for a library track path ("" when untagged)."""
    try:
        from server import mbresolve
        return mbresolve.track_mbid_for(path)
    except Exception:
        return ""


def add_tracks(pid, paths, position=None):
    """Append (or insert at position) track paths, deduplicating. The
    track's MusicBrainz recording ID is stored alongside the path so the
    entry survives later reorganizations."""
    with _lock:
        with _conn() as c:
            existing = {r["path"] for r in c.execute(
                "SELECT path FROM playlist_tracks WHERE playlist_id=?", (pid,))}
            new = [p for p in paths if p not in existing]
            if not new:
                return 0
            if position is None:
                base = c.execute("SELECT COALESCE(MAX(position),0) FROM playlist_tracks WHERE playlist_id=?",
                                 (pid,)).fetchone()[0]
                start = base + 1
                for i, p in enumerate(new):
                    c.execute("INSERT INTO playlist_tracks (playlist_id, path, position, mbid) VALUES (?,?,?,?)",
                              (pid, p, start + i, _mbid_for(p)))
            else:
                c.execute("UPDATE playlist_tracks SET position = position + ? WHERE playlist_id=? AND position >= ?",
                          (len(new), pid, position))
                for i, p in enumerate(new):
                    c.execute("INSERT INTO playlist_tracks (playlist_id, path, position, mbid) VALUES (?,?,?,?)",
                              (pid, p, position + i, _mbid_for(p)))
            c.execute("UPDATE playlists SET updated=? WHERE id=?", (time.time(), pid))
            return len(new)


def set_order(pid, paths):
    """Replace the entire ordering with `paths` (reorder / full replace)."""
    with _lock:
        with _conn() as c:
            known = {r["path"]: (r["mbid"] or "") for r in c.execute(
                "SELECT path, mbid FROM playlist_tracks WHERE playlist_id=?", (pid,))}
            c.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (pid,))
            for i, p in enumerate(paths):
                mbid = known.get(p) or _mbid_for(p)
                c.execute("INSERT INTO playlist_tracks (playlist_id, path, position, mbid) VALUES (?,?,?,?)",
                          (pid, p, i, mbid))
            c.execute("UPDATE playlists SET updated=? WHERE id=?", (time.time(), pid))


def remove_tracks(pid, paths):
    with _lock:
        with _conn() as c:
            for p in paths:
                c.execute("DELETE FROM playlist_tracks WHERE playlist_id=? AND path=?", (pid, p))
            c.execute("UPDATE playlists SET updated=? WHERE id=?", (time.time(), pid))


# --------------------------------------------------------------------------- #
# Smart playlists
# --------------------------------------------------------------------------- #
_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "lt": lambda a, b: a is not None and b is not None and a < b,
    "gt": lambda a, b: a is not None and b is not None and a > b,
    "lte": lambda a, b: a is not None and b is not None and a <= b,
    "gte": lambda a, b: a is not None and b is not None and a >= b,
    "contains": lambda a, b: a is not None and str(b).lower() in str(a).lower(),
    "missing": lambda a, b: a is None or str(a).strip() == "",
    "present": lambda a, b: a is not None and str(a).strip() != "",
}


def _track_value(track, field):
    """Extract a sortable value from an enriched track payload."""
    tags = track.get("tags") or {}
    if field in tags:
        return tags[field]
    if field in track:
        return track.get(field)
    tech = track.get("tech") or {}
    if field in tech:
        return tech[field]
    if field == "grade_pass":
        return track.get("grade_pass")
    if field == "lyrics_present":
        return track.get("lyrics_present")
    return None


def evaluate_smart(pid, library, base_paths=None):
    """Evaluate a smart playlist against the library payload.

    filter spec: {"conditions": [{field, op, value}], "match": "all"|"any"}
    Returns ordered list of matching track paths (respecting optional base_paths).
    """
    pl = get_playlist(pid)
    if pl is None or pl["kind"] != "smart":
        return None
    spec = pl.get("filter") or {}
    conditions = spec.get("conditions", [])
    match_all = spec.get("match", "all") == "all"

    hits = []
    for artist in library.get("artists", []):
        for alb in artist.get("albums", []):
            for tr in alb.get("tracks", []):
                if base_paths is not None and tr.get("path") not in base_paths:
                    continue
                results = []
                for cond in conditions:
                    op = cond.get("op", "eq")
                    field = cond.get("field")
                    value = cond.get("value")
                    fn = _OPS.get(op)
                    if fn is None:
                        results.append(False)
                        continue
                    results.append(fn(_track_value(tr, field), value))
                ok = all(results) if match_all else any(results)
                if ok:
                    hits.append(tr.get("path"))
    return hits


def set_smart_filter(pid, filter_spec):
    with _lock:
        with _conn() as c:
            c.execute("UPDATE playlists SET filter_json=?, updated=? WHERE id=?",
                      (json.dumps(filter_spec), time.time(), pid))
            return c.rowcount > 0


# --------------------------------------------------------------------------- #
# .m3u8 export / import
# --------------------------------------------------------------------------- #
def export_m3u8(pid):
    """Render a playlist as an .m3u8 string.

    Each entry carries its tagged title and — when known — the track's
    MusicBrainz recording MBID in an ``#MLO-MBID:`` comment, so re-import
    (or another app honoring the convention) can follow the recording even
    after the library has been reorganized."""
    pl = get_playlist(pid)
    if pl is None:
        return None
    with _conn() as c:
        rows = {r["path"]: (r["mbid"] or "") for r in c.execute(
            "SELECT path, mbid FROM playlist_tracks WHERE playlist_id=?", (pid,))}
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
        mbid = rows.get(path) or rows.get(norm) or ""
        if mbid:
            lines.append(f"#MLO-MBID:{mbid}")
        lines.append(norm)
    return "\n".join(lines) + "\n"


def import_m3u8(name, content, base_dir=None):
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
    pid = create_playlist(name, kind="manual")
    add_tracks(pid, found)
    # stamp explicit MBIDs + heal vanished paths via the tag index
    with _lock:
        with _conn() as c:
            for p, mbid in zip(found, mbids):
                if mbid:
                    c.execute("UPDATE playlist_tracks SET mbid=? WHERE playlist_id=? AND path=?",
                              (mbid, pid, p))
            for mbid in missing_by_mbid:
                healed = mbresolve.heal_row("track", "", mbid)
                if healed and os.path.isfile(healed):
                    maxpos = c.execute(
                        "SELECT COALESCE(MAX(position),-1)+1 FROM playlist_tracks WHERE playlist_id=?",
                        (pid,)).fetchone()[0]
                    c.execute("INSERT INTO playlist_tracks (playlist_id, path, position, mbid) VALUES (?,?,?,?)",
                              (pid, healed, maxpos, mbid))
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
def list_likes():
    """Paths of all liked tracks, newest first, forward-slash normalized.

    Rows that carry a MusicBrainz recording ID resolve against the live
    library first: if the file moved, the stored path is re-pointed so the
    like survives reorganizations.
    """
    from server import mbresolve
    out = []
    with _conn() as c:
        rows = c.execute("SELECT path, mbid FROM likes ORDER BY liked_at DESC").fetchall()
    for r in rows:
        path = r["path"]
        mbid = (r["mbid"] if "mbid" in r.keys() else None) or ""
        cur = mbresolve.heal_row("track", path, mbid)
        if mbid and cur and os.path.normpath(cur) != os.path.normpath(path):
            with _lock:
                with _conn() as c2:
                    c2.execute("UPDATE likes SET path=? WHERE path=?", (cur, path))
            path = cur
        out.append(path.replace("\\", "/"))
    return out


def is_liked(path):
    with _conn() as c:
        return c.execute("SELECT 1 FROM likes WHERE path=?", (path,)).fetchone() is not None


def toggle_like(path, mbid=None):
    path = str(path or "").strip()
    if not path:
        raise ValueError("path required")
    with _lock:
        with _conn() as c:
            if c.execute("SELECT 1 FROM likes WHERE path=?", (path,)).fetchone():
                c.execute("DELETE FROM likes WHERE path=?", (path,))
                return False
            c.execute(
                "INSERT INTO likes (path, liked_at, mbid) VALUES (?, ?, ?)",
                (path, time.time(), str(mbid or "").strip() or None),
            )
            return True


# --------------------------------------------------------------------------- #
# Favorite albums / artists / playlists (sidebar Favorites section)
# --------------------------------------------------------------------------- #
FAV_KINDS = ("album", "artist", "playlist")


def list_favorites():
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
        rows = c.execute("SELECT kind, key, mbid FROM favorites ORDER BY created_at DESC").fetchall()
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
                            "UPDATE favorites SET key=? WHERE kind=? AND key=?",
                            (cur, kind, key),
                        )
                key = cur
        key = key.replace("\\", "/")
        # The same entity may exist under both separator variants; the
        # healed path is canonical, so drop duplicates.
        dedupe = f"{kind}:{os.path.normcase(key)}"
        if dedupe in seen:
            with _lock:
                with _conn() as c2:
                    c2.execute("DELETE FROM favorites WHERE kind=? AND key=?", (kind, r["key"]))
            continue
        seen.add(dedupe)
        out[p].append(key)
    return out


def toggle_favorite(kind, key, mbid=None):
    kind = str(kind or "").strip().lower()
    key = str(key or "").strip()
    if kind not in FAV_KINDS:
        raise ValueError("kind must be one of: " + ", ".join(FAV_KINDS))
    if not key:
        raise ValueError("key required")
    with _lock:
        with _conn() as c:
            if c.execute("SELECT 1 FROM favorites WHERE kind=? AND key=?", (kind, key)).fetchone():
                c.execute("DELETE FROM favorites WHERE kind=? AND key=?", (kind, key))
                return False
            c.execute(
                "INSERT INTO favorites (kind, key, created_at, mbid) VALUES (?, ?, ?, ?)",
                (kind, key, time.time(), str(mbid or "").strip() or None),
            )
            return True
