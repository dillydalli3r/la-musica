"""Track ratings — half-star precision, per user, in the app's own database.

UNITS, the one place they are spelled out:

  * the database and the API speak half-stars as an INTEGER 0-10
    (0 = unrated, 1 = half a star, 10 = five stars) — the UI draws 0-5 stars
    with halves, and every half-star it can draw is one integer here;
  * the file tag is ``RATING`` in 0-100, the de-facto standard Picard writes
    (1 half-star = 10), so a library already tagged by Picard reads back as
    the stars its owner gave it, and what this app writes is what Picard
    shows. Only ``to_tag``/``from_tag`` convert.

Storage mirrors ``server.playlists``' likes: a row per (user, path) carrying
the track's MusicBrainz recording id, and every read resolves the CURRENT path
through ``server.mbresolve.heal_row`` (path first, then MBID) and re-points the
row, so a rating survives a rename or a reorganization. Ratings live in their
own database beside the other app databases rather than in playlists.db:
a rating is a first-class library fact with its own API, and mlo.format_all's
excess-tag strip must never be able to lose one (see the RATING entry in
mlo.audio.TAG_MAP — the allow-list the strip and the excess grade share).

The file tag is never the source of truth once a user has rated: a row WINS,
always. A file whose own ``RATING`` exists and has no row supplies the initial
value instead of showing unrated (``adopt``), which is what makes an imported
Picard library read correctly on day one.
"""
import os
import sqlite3
import threading
import time

# Reentrant: the writers below hold it and then call _conn(), whose first call
# builds the schema under this same lock (the trap server.playlists documents
# for its own RLock — a plain Lock deadlocked the very first write).
_lock = threading.RLock()

# 10 half-stars is five stars; the tag's own scale is 0-100, so one half-star
# is ten of it.
HALF_MAX = 10
TAG_STEP = 10

# Why a path cannot take a rating — the same words the endpoints report, from
# one constant, so a PUT's 404 and a bulk row's error cannot drift apart.
MISSING_FILE = "file not found"


def db_path():
    """<music folder>/.mlo/data/ratings.db, beside the other app databases."""
    from mlo.paths import app_data_dir
    return os.path.join(app_data_dir(), "ratings.db")


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


def _init():
    with _lock:
        with _conn() as c:
            # The key starts with `user`, like playlists' likes: two people on
            # one server may rate the same track and neither may see the other
            # row. `""` is the default/admin scope written before users existed.
            c.executescript("""
                CREATE TABLE IF NOT EXISTS ratings (
                    user TEXT NOT NULL DEFAULT '',
                    path TEXT NOT NULL,
                    rating INTEGER NOT NULL,
                    mbid TEXT,
                    updated REAL NOT NULL,
                    PRIMARY KEY (user, path)
                );
                CREATE INDEX IF NOT EXISTS ratings_by_mbid
                    ON ratings (user, mbid);
                """)


# --------------------------------------------------------------------------- #
# Units and normalization
# --------------------------------------------------------------------------- #
def api_path(path):
    """The path form the API and the library payload both speak (forward
    slashes), so a map key decorates ``track["path"]`` as it stands."""
    return str(path or "").strip().replace("\\", "/")


def _pathkey(path):
    """Comparison key for stored track paths — playlists' own normalization,
    so a path written by the API (backslashes on Windows) and one healed out
    of the library payload (forward slashes) are the same row."""
    return os.path.normcase(os.path.normpath(str(path)))


def parse_half_stars(value):
    """Client input as half-stars, clamped to 0-10.

    Accepts an int, a whole-number float or a numeric string ("7"), because
    JSON carries all three. Anything else — None, "", "abc", a list, a float
    with a fraction (the unit is a whole number of half-stars) — raises
    ValueError, which the endpoints turn into a 400 rather than storing a
    value nobody asked for. A number OUT of range is clamped, not refused:
    0-10 is the scale, and a client sending 12 means "as high as it goes".
    """
    if isinstance(value, bool):  # bool is an int in Python — never a rating
        raise ValueError("rating must be a number between 0 and 10")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("rating must be a whole number between 0 and 10")
        num = int(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            num = int(text)
        except ValueError:
            try:
                f = float(text)
            except ValueError:
                raise ValueError("rating must be a number between 0 and 10")
            if not f.is_integer():
                raise ValueError("rating must be a whole number between 0 and 10")
            num = int(f)
    elif isinstance(value, int):
        num = value
    else:
        raise ValueError("rating must be a number between 0 and 10")
    return max(0, min(HALF_MAX, num))


def to_tag(half):
    """Half-stars (0-10) -> the file tag's value (0-100)."""
    return int(half) * TAG_STEP


def from_tag(value):
    """A file's ``RATING`` (0-100, Picard's convention) -> half-stars.

    A blank, unparseable or non-positive value is 0 — unrated. A value that is
    not a multiple of ten (another tagger's spelling) lands on the nearest
    half-star rather than being thrown away.
    """
    try:
        num = float(str(value).strip())
    except (TypeError, ValueError):
        return 0
    if num <= 0:
        return 0
    # Half-UP, not banker's rounding: 65 sits between 3 and 3.5 stars and must
    # land on one of them the same way every time (round() would answer 6).
    return max(0, min(HALF_MAX, int(num / TAG_STEP + 0.5)))


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #
def set_rating(path, rating, mbid=None, user=""):
    """Store `rating` (half-stars 0-10) for `path`; 0 clears the row.

    Clearing DELETES rather than keeping a zero: "unrated" is a row that is
    not there, which is what the map, the counts and the MBID heal all assume
    — a kept zero would carry a stale recording id and count as a rating
    forever. Returns the stored value (0 when cleared).
    """
    path = str(path or "").strip()
    if not path:
        raise ValueError("path required")
    half = parse_half_stars(rating)
    key = os.path.normpath(path)
    with _lock:
        with _conn() as c:
            if half == 0:
                c.execute("DELETE FROM ratings WHERE user=? AND path=?", (user, key))
                return 0
            c.execute(
                "INSERT INTO ratings (user, path, rating, mbid, updated)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(user, path) DO UPDATE SET"
                # A blank id must not erase the one already stored: the caller
                # may only have had the file tag to read.
                " rating=excluded.rating,"
                " mbid=COALESCE(NULLIF(excluded.mbid,''), ratings.mbid),"
                " updated=excluded.updated",
                (user, key, half, str(mbid or "").strip() or None, time.time()))
            return half


def value(path, user=""):
    """One track's stored rating in half-stars (0 when unrated)."""
    path = str(path or "").strip()
    if not path:
        return 0
    want = _pathkey(path)
    with _conn() as c:
        # Exact hit first (the row is stored as os.path.normpath): the single
        # lookup most callers make never walks the table.
        row = c.execute("SELECT rating FROM ratings WHERE user=? AND path=?",
                        (user, os.path.normpath(path))).fetchone()
        if row is not None:
            return int(row["rating"] or 0)
        for r in c.execute("SELECT path, rating FROM ratings WHERE user=?", (user,)):
            if _pathkey(r["path"]) == want:
                return int(r["rating"] or 0)
    return 0


def map_for(paths=None, user="", adopt=True):
    """{path: half-stars} for one user — the map a client decorates from.

    Keys are the forward-slashed form the library payload uses
    (``track["path"]``), so a rating decorates a track without the client
    normalizing anything. A row whose file has moved is re-pointed through its
    MusicBrainz recording id and returned under its CURRENT path (the same
    self-healing likes and favourites do), so a rename never orphans it.

    `paths` narrows the answer to the tracks a page is about to draw. That
    narrow read is also the only one that ADOPTS a file's own ``RATING``
    (`adopt`) — the unfiltered read never opens a file, because doing it for
    the whole library would mean reading every track on a rendering request.
    """
    from server import mbresolve
    want = None
    if paths is not None:
        want = {}
        for p in paths:
            key = str(p or "").strip()
            if key:
                want.setdefault(_pathkey(key), api_path(key))
    with _conn() as c:
        rows = c.execute("SELECT path, mbid, rating FROM ratings WHERE user=?",
                         (user,)).fetchall()
    out, updates, have = {}, [], set()
    for r in rows:
        half = int(r["rating"] or 0)
        if not half:  # set_rating deletes zeros; a legacy row must not show
            continue
        stored, mbid = r["path"], (r["mbid"] or "")
        was = _pathkey(stored)
        cur = mbresolve.heal_row("track", stored, mbid)
        if cur and _pathkey(cur) != was:
            cur = os.path.normpath(cur)  # the form every writer stores
            updates.append((cur, stored))
            stored = cur
        if want is not None:
            key = _pathkey(stored)
            if key not in want:
                # The caller may still hold the PRE-move spelling: a stale
                # path in a filter must not lose the rating it is about to
                # be drawn for.
                key = was if was in want else None
            if key is None:
                continue
            out[want[key]] = half
            have.add(key)
        else:
            out[api_path(stored)] = half
    if updates:
        with _lock:
            with _conn() as c:
                for new, old in updates:
                    # OR REPLACE: the healed path may already carry a row the
                    # user made after the move, and a plain UPDATE would fail
                    # on the primary key instead of healing.
                    c.execute("UPDATE OR REPLACE ratings SET path=?"
                              " WHERE user=? AND path=?", (new, user, old))
    if want is not None and adopt:
        for key in sorted(want):
            if key in have:
                continue
            half = _adopt(want[key], user)
            if half:
                out[want[key]] = half
    return out


def counts(user=""):
    """How many tracks sit at each rating, keyed by half-stars as a string.

    Every bucket 1-10 is present, zero included, so a chart has its ten bars
    whether or not a value is in use yet. The numbers are the STORED rows, so
    the chart and the map agree after a heal. Nothing is read from files here
    — a chart must not walk the library (``adopt`` is what fills the rows).
    """
    out = {str(i): 0 for i in range(1, HALF_MAX + 1)}
    with _conn() as c:
        for r in c.execute("SELECT rating, COUNT(*) AS n FROM ratings"
                           " WHERE user=? GROUP BY rating", (user,)):
            key = str(int(r["rating"] or 0))
            if key in out:
                out[key] = int(r["n"])
    return out


# --------------------------------------------------------------------------- #
# The file tag (Picard's RATING, 0-100)
# --------------------------------------------------------------------------- #
def mbid_for(path):
    """The track's MusicBrainz recording id, for the rename heal.

    The cached tag read answers first: an imported file already carries
    MUSICBRAINZ_TRACKID, and `server.mbresolve.track_mbid_for` builds the whole
    library payload on a cold cache — a rating click must not trigger that.
    """
    try:
        from server import tagcache
        tags, _ = tagcache.read_track(path, ["MUSICBRAINZ_TRACKID"])
        mbid = str(tags.get("MUSICBRAINZ_TRACKID") or "").strip()
        if mbid:
            return mbid
    except Exception:
        pass
    try:
        from server import mbresolve
        return mbresolve.track_mbid_for(path)
    except Exception:
        return ""


def read_tag(path):
    """The file's own ``RATING`` as half-stars (0 when absent/unreadable)."""
    try:
        from server import tagcache
        tags, _ = tagcache.read_track(path, ["RATING"])
        return from_tag(tags.get("RATING"))
    except Exception:
        return 0


def _adopt(path, user):
    """Record the file's own ``RATING`` for a path the user has no row for.

    The value is never allowed to replace a stored one (that is the user's own
    rating), and the adopted row carries the recording id so it heals after a
    rename like any row this app wrote. Returns the half-star value adopted
    (0 = there was nothing to adopt).
    """
    half = read_tag(path)
    if not half:
        return 0
    set_rating(path, half, mbid=mbid_for(path), user=user)
    return half


def adopt(path, user=""):
    """`_adopt` for one path, checking the store first (the public entry)."""
    if value(path, user=user):
        return 0
    return _adopt(path, user)


def _config():
    try:
        from mlo import load_config
        return load_config()
    except Exception:
        return {}


def write_tag(path, rating, cfg=None):
    """Write (or clear) the file's ``RATING``. NEVER raises, never throws away
    the caller's DB rating.

    Returns ``{"rating100": int, "written": bool, "skipped": bool,
    "error": str|None}`` — `rating100` is the value that belongs in the tag
    (0 on a clear), `written` says the file on disk changed, `skipped` says
    the write gate (`write_rating_tags`, and the per-filetype
    `audio_tag_writes` matrix) left the file alone, and `error` is the
    container's own message. A caller stores its rating either way and reports
    `error`, so a read-only file can never silently drop what the user chose.
    """
    half = parse_half_stars(rating)
    tag_value = to_tag(half)
    out = {"rating100": tag_value, "written": False, "skipped": False,
           "error": None}
    cfg = _config() if cfg is None else cfg
    try:
        from mlo.config import should_write_audio_tag
        if not should_write_audio_tag(cfg, "RATING", filepath=path):
            out["skipped"] = True
            return out
    except Exception:
        pass  # a config that cannot answer must not block the user's rating
    try:
        from mlo.audio import AudioFile
        af = AudioFile(path)
        if af.audio is None:
            out["error"] = af.error or "cannot open the file"
            return out
        # Skip a file that already says this: rating a track must not rewrite
        # a container for a value it carries.
        if from_tag(af.get_tag("RATING")) == half:
            return out
        ok = af.delete_tag("RATING") if half == 0 else af.set_tag(
            "RATING", str(tag_value))
        if not ok or af.defer_save(False) is False:
            out["error"] = af.error or "tag write failed"
            return out
        out["written"] = True
        _invalidate(path)
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out


def _invalidate(path):
    """Drop the cached tag read for a path this module just rewrote."""
    try:
        from server import tagcache
        tagcache.invalidate_path(path)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Rating a track: store first, then the file
# --------------------------------------------------------------------------- #
def rate(path, rating, mbid=None, user="", cfg=None):
    """Store one rating and mirror it into the file. Returns the reply body.

    The row is written FIRST: the database is the source of truth, so a file
    that cannot be tagged (read-only, a full disk, a container this app cannot
    write) still keeps the rating the user just gave it, with the tag failure
    reported beside it.
    """
    half = set_rating(path, rating, mbid=mbid or mbid_for(path), user=user)
    tag = write_tag(path, half, cfg)
    return {"path": api_path(path), "rating": half, "tag": tag}


def bulk_rate(paths, rating, user="", cfg=None):
    """Rate many tracks. Per-path failures are REPORTED, never raised.

    A path the app cannot rate is one entry in `failed` (a row that was never
    stored); a path whose rating was stored but whose file tag could not be
    written counts as updated AND appears in `tags_failed` with the reason —
    the rating is the user's intent and it is never dropped for a container's
    sake, and the two lists never overlap.
    """
    half = parse_half_stars(rating)
    updated, failed, tagged, tags_failed = 0, [], 0, []
    for raw in paths or []:
        p = str(raw or "").strip()
        try:
            if not p:
                raise ValueError("path required")
            if not os.path.isfile(p):
                raise ValueError(MISSING_FILE)
            set_rating(p, half, mbid=mbid_for(p), user=user)
        except Exception as e:
            failed.append({"path": api_path(p), "error": str(e)})
            continue
        updated += 1
        res = write_tag(p, half, cfg)
        if res["error"]:
            tags_failed.append({"path": api_path(p), "error": res["error"]})
        elif res["written"]:
            tagged += 1
    return {"updated": updated, "failed": failed, "tags_written": tagged,
            "tags_failed": tags_failed}
