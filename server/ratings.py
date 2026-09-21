"""Star ratings — half-star precision, per user, in the app's own database.

THREE SCOPES, one store. A rating names the entity it belongs to:

  * ``track``  — one file, keyed by its path. The only scope that carries a
    file tag, and the only one that adopts a value out of a file;
  * ``album``  — the album FOLDER, keyed by that folder's path;
  * ``artist`` — the artist FOLDER, keyed by that folder's path.

An album or artist rating is the user's own verdict on that entity, NOT the
average of its tracks: the album header draws both, labelled, so the two can
never be read as one number. A folder has no file to tag, so those two scopes
live here and nowhere else — the API says so in its reply and the UI in its
tooltip, instead of pretending a tag was written.

UNITS, the one place they are spelled out:

  * the database and the API speak half-stars as an INTEGER 0-10
    (0 = unrated, 1 = half a star, 10 = five stars) — the UI draws 0-5 stars
    with halves, and every half-star it can draw is one integer here. All
    three scopes share the unit;
  * the file tag is ``RATING`` in 0-100, the de-facto standard Picard writes
    (1 half-star = 10), so a library already tagged by Picard reads back as
    the stars its owner gave it, and what this app writes is what Picard
    shows. Only ``to_tag``/``from_tag`` convert, and only track rows reach
    them.

Storage mirrors ``server.playlists``' likes: a row per (user, scope, path)
carrying the entity's MusicBrainz id, and every read resolves the CURRENT path
through ``server.mbresolve.heal_row`` (path first, then MBID) and re-points the
row, so a rating survives a rename or a reorganization — a folder rating heals
on the album's release id or the artist's id exactly as a track's heals on its
recording id. The three scopes never share a row: an album folder and an artist
folder are different verdicts even in the unlikely case they name one path.
Ratings live in their own database beside the other app databases rather than
in playlists.db: a rating is a first-class library fact with its own API, and
mlo.format_all's excess-tag strip must never be able to lose one (see the
RATING entry in mlo.audio.TAG_MAP — the allow-list the strip and the excess
grade share).

The file tag is never the source of truth once a user has rated: a row WINS,
always. A file whose own ``RATING`` exists and has no row supplies the initial
value instead of showing unrated (``adopt`` — track scope only, a folder has no
tag to adopt from), which is what makes an imported Picard library read
correctly on day one.
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

# The three kinds of entity a rating may name: a track, the album folder that
# holds it, the artist folder above that. The names are `mbresolve.heal_row`'s
# own kinds, so one scope word drives the heal as well as the row key.
SCOPES = ("track", "album", "artist")
FOLDER_SCOPES = ("album", "artist")

# Why a path cannot take a rating — the same words the endpoints report, from
# one constant per scope, so a PUT's 404 and a bulk row's error cannot drift
# apart. A track needs its FILE on disk; an album or artist needs its folder to
# be one the library payload knows, because a star on a path no page will ever
# draw is a rating nobody can see — or clear.
MISSING = {
    "track": "file not found",
    "album": "album folder is not in the library",
    "artist": "artist folder is not in the library",
}
MISSING_FILE = MISSING["track"]


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


def _column_names(c, table):
    """The columns a table actually has (empty when the table is not there)."""
    return {str(r["name"]) for r in c.execute(f"PRAGMA table_info({table})")}


def _table_exists(c, table):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                     (table,)).fetchone() is not None


def _init():
    with _lock:
        with _conn() as c:
            # The key starts with `user`, like playlists' likes: two people on
            # one server may rate the same track and neither may see the other
            # row. `""` is the default/admin scope written before users existed.
            # `scope` completes the key rather than sitting beside it: one user
            # holds three INDEPENDENT verdicts, and their paths can even
            # coincide (an artist with exactly one album has an album folder
            # that IS the artist folder), so a row keyed by path alone could
            # not hold both.
            #
            # The one migration this store has ever needed: rows written
            # before scopes existed are keyed (user, path) and carry no `scope`
            # column. They are all TRACK ratings, so they are re-keyed as such.
            # A leftover `ratings_legacy` is also picked up by name, so a
            # migration interrupted after the rename is finished on the next
            # start rather than stranded.
            legacy = _table_exists(c, "ratings_legacy")
            if _column_names(c, "ratings") and "scope" not in _column_names(c, "ratings"):
                c.execute("ALTER TABLE ratings RENAME TO ratings_legacy")
                legacy = True
            c.executescript("""
                CREATE TABLE IF NOT EXISTS ratings (
                    user TEXT NOT NULL DEFAULT '',
                    scope TEXT NOT NULL DEFAULT 'track',
                    path TEXT NOT NULL,
                    rating INTEGER NOT NULL,
                    mbid TEXT,
                    updated REAL NOT NULL,
                    PRIMARY KEY (user, scope, path)
                );
                CREATE INDEX IF NOT EXISTS ratings_by_mbid
                    ON ratings (user, mbid);
                """)
            if legacy:
                c.execute(
                    "INSERT OR IGNORE INTO ratings"
                    " (user, scope, path, rating, mbid, updated)"
                    " SELECT user, 'track', path, rating, mbid, updated"
                    " FROM ratings_legacy")
                c.execute("DROP TABLE ratings_legacy")


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


def parse_scope(value):
    """Client input as one of ``SCOPES``.

    Absent/blank means "track" — the scope every caller meant before the other
    two existed, so an old client's body keeps working. Anything else raises
    ValueError, which the endpoints turn into a 400 naming what is allowed,
    rather than silently rating the wrong entity."""
    text = str(value).strip().lower() if value is not None else ""
    if not text:
        return "track"
    if text not in SCOPES:
        raise ValueError("scope must be track, album or artist")
    return text


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
def set_rating(path, rating, mbid=None, user="", scope="track"):
    """Store `rating` (half-stars 0-10) for `path` in `scope`; 0 clears the row.

    Clearing DELETES rather than keeping a zero: "unrated" is a row that is
    not there, which is what the map, the counts and the MBID heal all assume
    — a kept zero would carry a stale id and count as a rating forever.
    Returns the stored value (0 when cleared).

    What `path` names is the scope's business (a track's file, an album's or
    an artist's folder) and nothing here checks: this is the plain writer the
    endpoints have already validated against (``target_missing``), and the
    scope only decides which row it lands on."""
    scope = parse_scope(scope)
    path = str(path or "").strip()
    if not path:
        raise ValueError("path required")
    half = parse_half_stars(rating)
    key = os.path.normpath(path)
    with _lock:
        with _conn() as c:
            if half == 0:
                c.execute("DELETE FROM ratings WHERE user=? AND scope=? AND path=?",
                          (user, scope, key))
                return 0
            c.execute(
                "INSERT INTO ratings (user, scope, path, rating, mbid, updated)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(user, scope, path) DO UPDATE SET"
                # A blank id must not erase the one already stored: the caller
                # may only have had the file tag to read.
                " rating=excluded.rating,"
                " mbid=COALESCE(NULLIF(excluded.mbid,''), ratings.mbid),"
                " updated=excluded.updated",
                (user, scope, key, half, str(mbid or "").strip() or None, time.time()))
            return half


def value(path, user="", scope="track"):
    """One entity's stored rating in half-stars (0 when unrated)."""
    scope = parse_scope(scope)
    path = str(path or "").strip()
    if not path:
        return 0
    want = _pathkey(path)
    with _conn() as c:
        # Exact hit first (the row is stored as os.path.normpath): the single
        # lookup most callers make never walks the table.
        row = c.execute("SELECT rating FROM ratings WHERE user=? AND scope=? AND path=?",
                        (user, scope, os.path.normpath(path))).fetchone()
        if row is not None:
            return int(row["rating"] or 0)
        for r in c.execute("SELECT path, rating FROM ratings WHERE user=? AND scope=?",
                           (user, scope)):
            if _pathkey(r["path"]) == want:
                return int(r["rating"] or 0)
    return 0


def map_for(paths=None, user="", adopt=True, scope="track"):
    """{path: half-stars} for one user and one scope — the map a client
    decorates from.

    Keys are the forward-slashed form the library payload uses (``track["path"]``
    for tracks, ``album["path"]``/``artist["path"]`` for the folder scopes), so
    a star decorates its row without the client normalizing anything. A row
    whose entity has moved is re-pointed through its MusicBrainz id and
    returned under its CURRENT path (the same self-healing likes and favourites
    do), so a rename never orphans it.

    `paths` narrows the answer to the entities a page is about to draw. In the
    track scope that narrow read is also the only one that ADOPTS a file's own
    ``RATING`` (`adopt`) — the unfiltered read never opens a file, because doing
    it for the whole library would mean reading every track on a rendering
    request. A folder scope has no file to read, so `adopt` does nothing there
    and the row is the whole truth.
    """
    from server import mbresolve
    scope = parse_scope(scope)
    want = None
    if paths is not None:
        want = {}
        for p in paths:
            key = str(p or "").strip()
            if key:
                want.setdefault(_pathkey(key), api_path(key))
    with _conn() as c:
        rows = c.execute("SELECT path, mbid, rating FROM ratings"
                         " WHERE user=? AND scope=?", (user, scope)).fetchall()
    out, updates, have = {}, [], set()
    for r in rows:
        half = int(r["rating"] or 0)
        if not half:  # set_rating deletes zeros; a legacy row must not show
            continue
        stored, mbid = r["path"], (r["mbid"] or "")
        was = _pathkey(stored)
        # The scope word IS the mbresolve kind (track/album/artist): a track
        # heals on its recording id, an album on its release id, an artist on
        # its id — one lookup, three entities.
        cur = mbresolve.heal_row(scope, stored, mbid)
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
                              " WHERE user=? AND scope=? AND path=?",
                              (new, user, scope, old))
    if want is not None and adopt and scope == "track":
        for key in sorted(want):
            if key in have:
                continue
            half = _adopt(want[key], user)
            if half:
                out[want[key]] = half
    return out


def counts(user="", scope="track"):
    """How many rows of one scope sit at each rating, keyed by half-stars as a
    string.

    Every bucket 1-10 is present, zero included, so a chart has its ten bars
    whether or not a value is in use yet. The numbers are the STORED rows of
    the requested scope — a track count never mixes the album or artist rows
    in — so the chart and the map agree after a heal. Nothing is read from
    files here: a chart must not walk the library (``adopt`` is what fills the
    rows).
    """
    scope = parse_scope(scope)
    out = {str(i): 0 for i in range(1, HALF_MAX + 1)}
    with _conn() as c:
        for r in c.execute("SELECT rating, COUNT(*) AS n FROM ratings"
                           " WHERE user=? AND scope=? GROUP BY rating", (user, scope)):
            key = str(int(r["rating"] or 0))
            if key in out:
                out[key] = int(r["n"])
    return out


# --------------------------------------------------------------------------- #
# The folder scopes (album / artist)
# --------------------------------------------------------------------------- #
def folder_paths(scope, cfg=None):
    """Every folder path the library payload knows for `scope`, path-keyed.

    The payload IS the definition of an album folder and an artist folder in
    this app (``server.library.build_library`` builds both levels: an artist's
    path is the folder holding its albums), so a rating is refused for anything
    else. That refusal is the point — a star stored against a path no page will
    ever draw can never be seen, changed or cleared again.

    Read from the cached payload (``build_library`` is TTL-cached and the page
    that asked for the rating has already built it), so a browser session pays
    for this at most once. An empty answer — no music folder configured — makes
    every folder rating a 404 with a reason, which is the truth: there is no
    album to rate.
    """
    scope = parse_scope(scope)
    if scope == "track":
        raise ValueError("track ratings are keyed by file path, not by folder")
    from server.library import build_library
    lib = build_library(cfg if cfg is not None else _config())
    out = set()
    for artist in lib.get("artists") or ():
        if scope == "artist":
            out.add(_pathkey(artist.get("path")))
            continue
        for album in artist.get("albums") or ():
            out.add(_pathkey(album.get("path")))
    return out


def target_missing(scope, path, cfg=None):
    """"" when `path` can take a rating in `scope`, else the reason why not.

    One function for the sentence, so a PUT's 404 and a bulk row's error can
    never describe the same refusal differently (the same reason `MISSING` is a
    table). A blank path is `path required`."""
    scope = parse_scope(scope)
    text = str(path or "").strip()
    if not text:
        return "path required"
    if scope == "track":
        return "" if os.path.isfile(text) else MISSING["track"]
    return "" if _pathkey(text) in folder_paths(scope, cfg) else MISSING[scope]


def entity_mbid(scope, path, cfg=None):
    """The MusicBrainz id the library payload carries for a folder, so a folder
    rating heals across a move exactly as a track's does.

    The payload only knows what the tags carry, so a library with no release id
    (or no artist id) on a folder answers "" — the row is then keyed by its
    path alone and the heal simply keeps the path it has."""
    scope = parse_scope(scope)
    if scope == "track":
        raise ValueError("track ratings heal on a recording id, not a folder's")
    try:
        from server import mbresolve
        idx = mbresolve.get_index(cfg)
        return str((idx.get(f"{scope}s_bypath") or {}).get(api_path(path)) or "")
    except Exception:
        return ""


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
# Rating an entity: store first, then (tracks only) the file
# --------------------------------------------------------------------------- #
def rate(path, rating, mbid=None, user="", cfg=None, scope="track"):
    """Store one rating and, for a track, mirror it into the file. Returns the
    reply body.

    The row is written FIRST: the database is the source of truth, so a file
    that cannot be tagged (read-only, a full disk, a container this app cannot
    write) still keeps the rating the user just gave it, with the tag failure
    reported beside it.

    An album or artist rating writes NO tag — a folder has no file to carry one
    — so its reply says ``"tag": None`` rather than inventing a write that
    cannot happen. The caller has already checked the target exists
    (``target_missing``); this stores what it was given.
    """
    scope = parse_scope(scope)
    if scope == "track":
        half = set_rating(path, rating, mbid=mbid or mbid_for(path), user=user)
        return {"path": api_path(path), "rating": half, "tag": write_tag(path, half, cfg)}
    half = set_rating(path, rating, user=user, scope=scope,
                      mbid=mbid or entity_mbid(scope, path, cfg))
    return {"path": api_path(path), "rating": half, "tag": None}


def bulk_rate(paths, rating, user="", cfg=None):
    """Rate many tracks. Per-path failures are REPORTED, never raised.

    A path the app cannot rate is one entry in `failed` (a row that was never
    stored); a path whose rating was stored but whose file tag could not be
    written counts as updated AND appears in `tags_failed` with the reason —
    the rating is the user's intent and it is never dropped for a container's
    sake, and the two lists never overlap.

    Tracks only, deliberately: a bulk call is a multi-select of FILES (a
    selection of rows, a whole playlist), and no surface multi-selects album or
    artist folders — an album or artist rating is one verdict on one folder,
    made through the one call (``rate``) that can also refuse an unknown
    folder.
    """
    half = parse_half_stars(rating)
    updated, failed, tagged, tags_failed = 0, [], 0, []
    for raw in paths or []:
        p = str(raw or "").strip()
        try:
            reason = target_missing("track", p)
            if reason:
                raise ValueError(reason)
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
