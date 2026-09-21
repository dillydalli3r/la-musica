"""Play history — one row per playback start, per user, in the app's own
database.

A play is written when a track ACTUALLY STARTS (the web player bar and the
mobile client both call `POST /api/plays` at that moment), never when a page is
opened, a queue is built or a seek happens — so the store answers "what have I
really listened to", which is the only thing a chart can be built from.

The store is `<music>/.mlo/data/plays.db`, the same shape and the same rules as
`server.ratings`/`server.wishes`: additive schema on first use, user-scoped
rows (`""` is the default/admin scope written before users existed), one row per
event, no ORM. A path is stored in the API's own forward-slash form
(`api_path`), and every row carries the track's ALBUM FOLDER as a derived
column — computed at insert from the path alone (pure string work, no file
I/O), which is what lets albums group in SQL instead of by loading the history
into Python.

`top` is the read side: the library's own most-played tracks, albums or artists
for one closed window. Grouping and ordering happen in SQL (tracks and albums);
only the artist fold needs the library payload, because an album's ARTIST exists
in the library's tags and nowhere in a file path.

Windows are HALF-OPEN — `[start, end)` — and computed in LOCAL time (the
calendar the user reads: "this week" is Monday 00:00 to next Monday 00:00):
`all` is unbounded, `year`/`month`/`week` start at the first instant of the
current one. `now` is injectable (`window(period, now=…)`) so the boundaries are
testable without a clock.
"""
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

# Reentrant: `record` holds it and then calls `_conn()`, whose first call
# builds the schema under this same lock (the trap server.playlists documents
# for its own RLock).
_lock = threading.RLock()

# The four windows the charts offer, and the three row kinds — hidden API
# vocabulary, not config keys. Both orders are the UI's own order.
PERIODS = ("all", "year", "month", "week")
KINDS = ("tracks", "albums", "artists")
ROW_KIND = {"tracks": "track", "albums": "album", "artists": "artist"}
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# Why an empty history is empty — one sentence, from one constant, so the
# library endpoint and the Charts page cannot explain it two ways.
EMPTY_NOTE = ("no plays recorded yet — a play is written when a track actually "
              "starts playing (the web player and the mobile client both report "
              "it), so this fills up as you listen")


def db_path():
    """<music folder>/.mlo/data/plays.db, beside the other app databases."""
    from mlo.paths import app_data_dir
    return os.path.join(app_data_dir(), "plays.db")


_init_lock = threading.Lock()
_initialized = False


def _conn():
    global _initialized
    os.makedirs(os.path.dirname(db_path()), exist_ok=True)
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    # Schema on FIRST USE, not on import: creating the file at import time
    # planted an empty database in the state dir before the migration could
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


def reset():
    """Forget that the schema was built (tests that repoint the music folder).
    The next `_conn()` re-runs `_init`, which is additive — no data is lost."""
    global _initialized
    with _init_lock:
        _initialized = False


def _init():
    with _lock:
        with _conn() as c:
            # The key starts with `user`, like ratings and playlists' likes: two
            # people on one server may listen to the same track and neither may
            # see the other's history. `""` is the default/admin scope written
            # before users existed. A path may repeat — a play is an EVENT, not
            # a state, so this table has a surrogate id rather than a key.
            c.executescript("""
                CREATE TABLE IF NOT EXISTS plays (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user TEXT NOT NULL DEFAULT '',
                    path TEXT NOT NULL,
                    album TEXT NOT NULL DEFAULT '',
                    started_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS plays_by_user
                    ON plays (user, started_at);
                CREATE INDEX IF NOT EXISTS plays_by_path
                    ON plays (user, path);
                """)


# --------------------------------------------------------------------------- #
# Pathways
# --------------------------------------------------------------------------- #
def api_path(path):
    """The path form the API, the library payload and this store all speak
    (forward slashes), so a row can be joined to ``track["path"]`` as it
    stands."""
    return str(path or "").strip().replace("\\", "/")


def album_of(path):
    """The track's ALBUM FOLDER — its parent directory, in the same forward-
    slash form. Derived here and stored, so albums group in SQL; it is the same
    fold the library payload's `album.path` uses (one folder per album)."""
    text = api_path(path)
    head, sep, _tail = text.rpartition("/")
    return head if sep else ""


def record(path, user="", at=None):
    """Store ONE play of *path* and answer what was written.

    Deliberately ONE insert and no file I/O: this runs on the playback start
    path, where opening the track to check it still exists would cost a stat on
    every play. A path whose file is gone keeps its history like any other —
    the store is a record of what was played, not a view of the library.
    """
    p = api_path(path)
    if not p:
        raise ValueError("path required")
    started = float(at) if at is not None else time.time()
    album = album_of(p)
    with _lock:
        with _conn() as c:
            c.execute("INSERT INTO plays (user, path, album, started_at) "
                      "VALUES (?, ?, ?, ?)", (str(user or ""), p, album, started))
    return {"path": p, "album": album, "started_at": started}


def count(user="", period="all", now=None):
    """How many plays the store holds for *user* in one window."""
    start, end = window(period, now)
    sql = "SELECT COUNT(*) FROM plays WHERE user = ?"
    args = [str(user or "")]
    if start is not None:
        sql += " AND started_at >= ?"
        args.append(start)
    if end is not None:
        sql += " AND started_at < ?"
        args.append(end)
    with _lock:
        with _conn() as c:
            return int(c.execute(sql, args).fetchone()[0])


def reset_history(user=None):
    """Drop the stored plays — all of them, or one user's. Answers the count
    removed. For the tests and for a user starting their history over; no route
    calls it."""
    with _lock:
        with _conn() as c:
            if user is None:
                cur = c.execute("DELETE FROM plays")
            else:
                cur = c.execute("DELETE FROM plays WHERE user = ?", (str(user),))
            return cur.rowcount


# --------------------------------------------------------------------------- #
# The window maths
# --------------------------------------------------------------------------- #
def _stamp(dt):
    """A naive local datetime as epoch seconds (the calendar the user reads)."""
    return dt.timestamp()


def window(period, now=None):
    """The `(start, end)` bounds of one period, HALF-OPEN, in epoch seconds.

    `all` is unbounded (`(None, None)`) because there is no first play to start
    from. `year`, `month` and `week` start at the first instant of the CURRENT
    one in local time (the week on Monday, ISO's own first day) and end at the
    first instant of the next — so a play exactly at a boundary belongs to the
    window that STARTS there and to no other, which is what keeps two adjacent
    windows from both claiming (or both dropping) an edge play.
    """
    period = str(period or "all").strip().lower() or "all"
    if period not in PERIODS:
        raise ValueError("period must be one of: " + ", ".join(PERIODS))
    now = time.time() if now is None else float(now)
    if period == "all":
        return None, None
    today = datetime.fromtimestamp(now)
    if period == "year":
        start = datetime(today.year, 1, 1)
        end = datetime(today.year + 1, 1, 1)
    elif period == "month":
        start = datetime(today.year, today.month, 1)
        end = datetime(today.year + (1 if today.month == 12 else 0),
                       1 if today.month == 12 else today.month + 1, 1)
    else:
        start = datetime(today.year, today.month, today.day) - timedelta(
            days=today.weekday())
        end = start + timedelta(days=7)
    return _stamp(start), _stamp(end)


def window_payload(period, now=None, bounds=None):
    """The window as the API echoes it: the bounds, plus their ISO forms for a
    reader that wants to print them. The ISO stamps are LOCAL time with an
    explicit offset, because that is the calendar the bounds were computed in."""
    start, end = bounds if bounds is not None else window(period, now)
    return {
        "period": str(period or "all").strip().lower() or "all",
        "start": start,
        "end": end,
        "start_iso": (datetime.fromtimestamp(start).astimezone().isoformat()
                      if start is not None else None),
        "end_iso": (datetime.fromtimestamp(end).astimezone().isoformat()
                    if end is not None else None),
    }


# --------------------------------------------------------------------------- #
# The top-by-window read
# --------------------------------------------------------------------------- #
def _library_index(lib):
    """The library payload folded to what the charts need: what each track,
    album and artist path IS, so a stored play can be named and linked.

    Both directions are path-keyed (`api_path`), because a play row only knows
    a path. An artist is looked up by its NAME as well, since the artists fold
    has an album's album-artist and no artist path."""
    tracks, albums, artists = {}, {}, {}
    for artist in (lib or {}).get("artists") or []:
        apath = api_path(artist.get("path"))
        aname = str(artist.get("name") or "").strip()
        if aname:
            artists.setdefault(aname.casefold(), {"name": aname, "path": apath})
        if apath:
            artists.setdefault(("path", apath), {"name": aname, "path": apath})
        for alb in artist.get("albums") or []:
            meta = alb.get("meta") or {}
            bpath = api_path(alb.get("path"))
            album_artist = str(meta.get("ALBUMARTIST") or meta.get("ARTIST")
                               or aname or "").strip()
            albums[bpath] = {
                "title": str(meta.get("ALBUM") or "").strip()
                         or bpath.rstrip("/").rsplit("/", 1)[-1],
                "artist": album_artist,
                "artist_path": api_path(artist.get("path")),
            }
            for tr in alb.get("tracks") or []:
                tags = tr.get("tags") or {}
                tracks[api_path(tr.get("path"))] = {
                    "title": str(tags.get("TITLE") or "").strip(),
                    "artist": str(tags.get("ARTIST") or album_artist).strip(),
                    "album": albums[bpath]["title"],
                    "album_path": bpath,
                }
    return {"tracks": tracks, "albums": albums, "artists": artists}


def _library(cfg, lib):
    """The library payload, built through the app's own cached builder."""
    if lib is not None:
        return lib
    from server import library as library_mod
    return library_mod.build_library(cfg or {})


def _folder_name(path):
    """A path's last segment — the honest fallback name for a play whose album
    the library no longer holds."""
    return str(path or "").rstrip("/").rsplit("/", 1)[-1]


def _file_stem(path):
    """A track file's name without its extension — what a player shows for a
    file the library cannot name."""
    name = _folder_name(path)
    stem, dot, _ext = name.rpartition(".")
    return stem if dot and stem else name


def _unknown_track(path):
    """(title, artist, album) for a play the library no longer names.

    Read off the library's OWN folder convention — `<artist>/<album>/<file>` —
    instead of inventing anything: the file's own name, then the folders it sat
    in. A row is still a row (the play happened), and saying where the file was
    beats a blank or guessed name."""
    album = _folder_of(path)
    return _file_stem(path), _folder_name(_folder_of(album)), _folder_name(album)


def _grouped(conn, column, start, end, user, limit=None):
    """One SQL group-by over the window: `(value, plays)`, most played first.

    Ordering and the LIMIT both happen in SQL — the history is never read into
    Python to be counted or sorted there. Ties break on the value so two rows
    with the same count always print in the same order."""
    sql = ("SELECT %s AS grp, COUNT(*) AS plays FROM plays WHERE user = ?" % column)
    args = [str(user or "")]
    if start is not None:
        sql += " AND started_at >= ?"
        args.append(start)
    if end is not None:
        sql += " AND started_at < ?"
        args.append(end)
    sql += " GROUP BY grp ORDER BY plays DESC, grp ASC"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(int(limit))
    with _lock:
        rows = conn.execute(sql, args).fetchall()
    return [(str(r["grp"]), int(r["plays"])) for r in rows]


def _folder_of(path):
    """The parent folder of an album path — the artist folder in the library's
    own layout (`<artist>/<album>/…`)."""
    text = api_path(path)
    head, sep, _tail = text.rpartition("/")
    return head if sep else ""


def top(period="all", kind="tracks", limit=DEFAULT_LIMIT, user="", cfg=None,
        lib=None, now=None):
    """The library's own most-played rows for one window.

    `kind=tracks` counts each track, `albums` folds the plays of every track in
    an album folder, `artists` folds them to the album artist. Every row carries
    its `plays` for THIS window and whether the library still holds it; the
    window's own bounds are echoed alongside. An empty history is an empty list
    plus the one sentence that explains why (`note`), never a bare zero — and a
    window with nothing in it says so while still naming what the store holds.
    """
    period = str(period or "all").strip().lower() or "all"
    kind = str(kind or "tracks").strip().lower() or "tracks"
    if period not in PERIODS:
        raise ValueError("period must be one of: " + ", ".join(PERIODS))
    if kind not in KINDS:
        raise ValueError("kind must be one of: " + ", ".join(KINDS))
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(MAX_LIMIT, limit))
    start, end = window(period, now)
    out = {"period": period, "kind": kind, "limit": limit,
           "window": window_payload(period, bounds=(start, end)), "items": [],
           "note": ""}

    conn = _conn()
    if kind == "tracks":
        groups = _grouped(conn, "path", start, end, user, limit)
    elif kind == "albums":
        groups = _grouped(conn, "album", start, end, user, limit)
    else:
        # Artists fold through the library's album-artist map, so the SQL
        # groups by album (the only grouping a path can state) and the fold
        # below sums those album groups — over the ALBUMS played in this
        # window, never over the history itself.
        groups = _grouped(conn, "album", start, end, user)

    if not groups:
        total = count(user=user)
        out["note"] = EMPTY_NOTE if not total else (
            "no plays in %s — the store holds %s in total"
            % (period, "%d play%s" % (total, "" if total == 1 else "s")))
        return out

    index = _library_index(_library(cfg, lib))
    if kind == "tracks":
        for path, plays in groups:
            known = index["tracks"].get(path) or {}
            title, artist, album = (known.get("title"), known.get("artist"),
                                    known.get("album"))
            if not known:
                title, artist, album = _unknown_track(path)
            out["items"].append({
                "kind": "track", "path": path,
                "title": title or _file_stem(path),
                "artist": artist or "",
                "album": album or "",
                "album_path": known.get("album_path") or _folder_of(path),
                "plays": plays,
                "in_library": bool(known),
            })
    elif kind == "albums":
        for path, plays in groups:
            known = index["albums"].get(path) or {}
            out["items"].append({
                "kind": "album", "path": path,
                "title": known.get("title") or _folder_name(path),
                "artist": known.get("artist") or _folder_name(_folder_of(path)),
                "plays": plays,
                "in_library": bool(known),
            })
    else:
        folded, order = {}, []
        for album_path, plays in groups:
            known = index["albums"].get(album_path) or {}
            name = known.get("artist") or _folder_name(_folder_of(album_path))
            artist = index["artists"].get(name.casefold())
            key = name.casefold()
            if key not in folded:
                folded[key] = {
                    "kind": "artist", "name": name,
                    "path": (artist or {}).get("path") or _folder_of(album_path),
                    "plays": 0, "in_library": bool(artist),
                }
                order.append(key)
            folded[key]["plays"] += plays
        ranked = sorted((folded[k] for k in order),
                        key=lambda r: (-r["plays"], r["name"].casefold()))
        out["items"] = ranked[:limit]
    return out
