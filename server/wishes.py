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

Every outcome of that search is announced exactly once and the wish ends in a
state that says the truth about it (see the retry policy below): an album that
keeps being retried says nothing on the bus, an album that gave up says so
once, with the reason and a link to its own row in the queue.
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

STATUSES = ("wanted", "searching", "imported", "failed", "available", "not_found")

# Terminal statuses: the worker never searches one of these again on its own.
# `not_found` always is — "the network does not have it" is an ANSWER, and
# re-asking the same question on a timer is the silent stalling the retry
# policy below exists to stop. `failed` becomes terminal once its attempt cap
# is reached (see is_terminal).
TERMINAL_STATUSES = ("imported", "not_found")

# Where a wish came from — one field, so every surface labels a row the same
# way: "musicbrainz" is a framework album ("Add to library" on a MusicBrainz
# entity), "soulseek" a want saved by hand from the Soulseek page, "auto" the
# entry an auto-import job offers to save. "" is a wish saved before this was
# recorded.
SOURCES = ("musicbrainz", "soulseek", "auto")


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
                    album_path TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    -- When the next AUTOMATIC search may run: the interval's
                    -- own end, or a transient failure's backoff, whichever is
                    -- later (see the retry policy). Stored rather than derived
                    -- so the wait is visible to the UI and survives a restart.
                    retry_at REAL NOT NULL DEFAULT 0,
                    -- Empty searches so far: the count `wishes_not_found_attempts`
                    -- is compared against, kept apart from `attempts` because a
                    -- transient failure must not spend a not-found attempt.
                    not_found INTEGER NOT NULL DEFAULT 0,
                    -- Which PRESSING this wish is about, as the identity block
                    -- in RELEASE_KEYS — resolved from the release the pipeline
                    -- looked up and kept here so the queue row can name the
                    -- edition without a MusicBrainz request of its own.
                    release_json TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS wish_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    t REAL NOT NULL,
                    level TEXT NOT NULL DEFAULT 'info',
                    msg TEXT NOT NULL
                );
                """
            )
            # Existing databases predate `source`: the column is additive, so
            # an ALTER is all the migration this needs — and a database that
            # already has it (a fresh _init created it above) is left alone.
            cols = {r["name"] for r in
                    c.execute("PRAGMA table_info(wishes)").fetchall()}
            if "source" not in cols:
                c.execute("ALTER TABLE wishes ADD COLUMN source TEXT NOT NULL DEFAULT ''")
            # Same rule for the retry policy's own two columns: an older
            # database gets them as 0/0, i.e. "no backoff and no empty
            # searches" — a wish that has been retried forever keeps being
            # retried, exactly as it was before this existed.
            if "retry_at" not in cols:
                c.execute("ALTER TABLE wishes ADD COLUMN retry_at REAL NOT NULL DEFAULT 0")
            if "not_found" not in cols:
                c.execute("ALTER TABLE wishes ADD COLUMN not_found INTEGER NOT NULL DEFAULT 0")
            # Same for the identity block: an older database gets it empty, so
            # its rows show what they know until a cycle resolves the release.
            if "release_json" not in cols:
                c.execute("ALTER TABLE wishes ADD COLUMN release_json TEXT NOT NULL DEFAULT ''")


# --------------------------------------------------------------------------- #
# The release identity — WHICH pressing a row is about
# --------------------------------------------------------------------------- #
# ONE block with the same keys wherever it appears (a job row, a wish row, a
# notification), so no surface has to reconstruct a release's facts from
# whatever spelling it happens to hold. The order is what identifies a
# PRESSING: its catalog number and the medium it is on first, then where and
# when it came out and how much it carries, then the edition's own
# disambiguation and the status MusicBrainz states for it (Official /
# Promotion / Bootleg / …).
#
# EVERY key is always present. A fact the pipeline could not resolve is empty
# and renders as absent — a MusicBrainz outage, or a wish whose release was
# never looked up, leaves a row that still says what it does know and never a
# row that fails to render.
RELEASE_KEYS = ("id", "title", "artist", "date", "country", "status",
                "media", "track_count", "disambiguation", "catalog_number",
                "label")


def release_identity(release, release_mbid=""):
    """The identity block of one MusicBrainz payload, in any of its spellings.

    Read from `mlo.release_choice`, the ONE module that already normalizes
    MusicBrainz's own field names for the rest of the app (medium formats,
    track counts), plus the two keys this app's own compact job summary spells
    differently (`tracks` for the count, `media` for the format). No key is
    ever invented: what the payload does not state comes back empty.
    """
    from mlo import release_choice

    rel = release or {}
    artists = rel.get("artists") or []
    artist = str(rel.get("artist") or "").strip()
    if not artist and artists and isinstance(artists[0], dict):
        artist = str(artists[0].get("name") or "").strip()
    media = [m for m in release_choice.media_formats(rel) if m]
    if not media and isinstance(rel.get("media"), str) and rel["media"].strip():
        # The compact job summary states one medium as a plain string; only
        # used when nothing richer is there.
        media = [rel["media"].strip()]
    count = release_choice.track_count(rel)
    if not count:
        try:
            count = int(rel.get("tracks") or rel.get("track_count") or 0)
        except (TypeError, ValueError):
            count = 0
    catalog = str(rel.get("catalog_number") or "").strip()
    if not catalog:
        numbers = rel.get("catalog_numbers") or []
        catalog = str(numbers[0] if numbers else "").strip()
    return {
        "id": release_choice.release_id(rel) or str(release_mbid or "").strip(),
        "title": str(rel.get("title") or "").strip(),
        "artist": artist,
        "date": str(rel.get("date") or "").strip(),
        "country": str(rel.get("country") or "").strip(),
        "status": str(rel.get("status") or "").strip(),
        "media": media,
        "track_count": int(count or 0),
        "disambiguation": str(rel.get("disambiguation") or "").strip(),
        "catalog_number": catalog,
        "label": str(rel.get("label") or "").strip(),
    }


def identity_of(release_mbid, cfg=None):
    """Resolve *release_mbid* to an identity block, or None.

    Through `integrations.resolve_release` — the same lookup the wish worker
    and every "Add to library" route make, so a release the app has just
    fetched costs no second request (that layer's cache is what makes this
    cheap). A MusicBrainz outage, an unknown id or a group with no eligible
    edition is None: a row says what it knows instead of failing.
    """
    rid = str(release_mbid or "").strip()
    if not rid:
        return None
    from server import integrations as intg

    try:
        release, resolved = intg.resolve_release(rid)
    except Exception:
        return None
    if not release:
        return None
    return release_identity(release, resolved or rid)


def store_identity(wid, block):
    """Record which pressing a wish is about (its row's own JSON column)."""
    if not block:
        return
    _mark(wid, release_json=json.dumps(block))


# How many wishes one worker cycle resolves an identity for: MusicBrainz
# answers one request per second, so a long wish list must not turn a tick
# into minutes of lookups (the rest are filled in by the following cycles).
IDENTITY_PRIME_LIMIT = 25


def prime_identities(cfg=None, limit=IDENTITY_PRIME_LIMIT):
    """Fill in the identity of every wish that has none yet; returns the count.

    The row's display data, not an acquisition: a wish nobody has searched yet
    (or one the user must fill by hand) still has to say WHICH release it is
    waiting for. Called from the worker's own cycle — never from a route the
    UI polls — and bounded per call (see IDENTITY_PRIME_LIMIT). A wish whose
    release cannot be resolved keeps an empty identity and is retried on a
    later cycle.
    """
    filled = 0
    for w in list_wishes():
        if filled >= int(limit or 0):
            break
        if not str(w.get("release_mbid") or "").strip():
            continue
        if any(w["release"].get(k) for k in RELEASE_KEYS if k != "id"):
            continue                    # already known — never re-fetched
        block = identity_of(w["release_mbid"], cfg)
        if block:
            store_identity(w["id"], block)
            filled += 1
    return filled


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
    # The identity block (RELEASE_KEYS), ALWAYS a block: a wish whose release
    # has not been looked up yet (or could not be) renders its empty facts
    # rather than a missing field every caller would have to guard.
    stored = str(d.pop("release_json", "") or "")
    block = release_identity({}, d.get("release_mbid") or "")
    if stored:
        try:
            known = json.loads(stored)
            if isinstance(known, dict):
                block.update({k: known[k] for k in RELEASE_KEYS if k in known})
        except Exception:
            pass                        # a corrupt column is not a failed row
    d["release"] = block
    # A wish whose album is a FRAMEWORK album: the folder exists (it is in the
    # library, listed as pending) but no audio has arrived yet. Derived from the
    # marker rather than stored, so the two can never disagree — the import
    # that fills the folder clears the marker and this flips by itself. One
    # stat per row, and there are only ever a handful of wishes.
    d["pending"] = False
    path = str(d.get("album_path") or "")
    if path and os.path.isdir(path):
        try:
            from mlo.paths import load_pending
            d["pending"] = bool(load_pending(path))
        except Exception:
            d["pending"] = False
    return d


def _with_terminal(d, cfg=None):
    """Mark a row with the store's own terminal verdict (one config read per
    row BATCH, see list_wishes): a terminal wish is one the worker will never
    search again on its own, which is what makes it safe to take off the list
    (the Wishes tab's "Clear finished", the queue's clear)."""
    from mlo.config import load_config

    d["terminal"] = is_terminal(d, cfg if cfg is not None else load_config())
    return d


def list_wishes():
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM wishes ORDER BY "
            "CASE status WHEN 'wanted' THEN 0 WHEN 'searching' THEN 1 "
            "WHEN 'failed' THEN 2 ELSE 3 END, added_at DESC"
        ).fetchall()
    from mlo.config import load_config

    cfg = load_config()                 # ONE read for the whole batch
    return [_with_terminal(_row(r), cfg) for r in rows]


def get_wish(wid):
    with _conn() as c:
        r = c.execute("SELECT * FROM wishes WHERE id=?", (int(wid),)).fetchone()
    return _with_terminal(_row(r)) if r else None


def add_wish(release_mbid, title="", artist="", year="", note="",
             target_dir="", queries=None, source="", release=None):
    """Save a release to the wishlist. *release* is the MusicBrainz payload the
    caller already holds (an interactive job's own release): its identity is
    recorded with the wish, so the row can name the pressing immediately —
    without a second lookup of a release that was just fetched."""
    release_mbid = str(release_mbid or "").strip()
    if not release_mbid:
        raise ValueError("release_mbid required")
    source = str(source or "").strip().lower()
    if source and source not in SOURCES:
        source = ""
    block = release_identity(release, release_mbid) if release else None
    now = time.time()
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT * FROM wishes WHERE release_mbid=?",
                            (release_mbid,)).fetchone()
            if row:
                # Both updates run on THIS connection and the row is read back
                # from it: a second connection would have to wait for this
                # one's write transaction, which nothing releases until the
                # block exits.
                if source and not row["source"]:
                    # a wish saved before this was recorded: the caller that
                    # asked for it now (a framework album) says what it is
                    c.execute("UPDATE wishes SET source=? WHERE id=?",
                              (source, row["id"]))
                if block and not row["release_json"]:
                    c.execute("UPDATE wishes SET release_json=? WHERE id=?",
                              (json.dumps(block), row["id"]))
                if (source and not row["source"]) or (block and not row["release_json"]):
                    return _row(c.execute("SELECT * FROM wishes WHERE id=?",
                                          (row["id"],)).fetchone())
                return _row(row)  # already wished — idempotent
            cur = c.execute(
                "INSERT INTO wishes (release_mbid, title, artist, year, status, note,"
                " target_dir, quotes, added_at, updated_at, source, release_json)"
                " VALUES (?,?,?,?,'wanted',?,?,?,?,?,?,?)",
                (release_mbid, str(title or ""), str(artist or ""), str(year or ""),
                 str(note or ""), str(target_dir or ""),
                 json.dumps(queries) if queries else "", now, now, source,
                 json.dumps(block) if block else ""),
            )
            wid = cur.lastrowid
    log("info", f"Wish added: {artist} — {title} ({release_mbid[:8]})")
    return get_wish(wid)


def update_wish(wid, fields):
    allowed = {"note", "target_dir", "status", "queries", "title", "artist", "year",
               "album_path", "source"}
    sets, vals = [], []
    for k, v in (fields or {}).items():
        if k not in allowed:
            continue
        if k == "queries":
            sets.append("quotes=?")
            vals.append(json.dumps(v) if v else "")
        elif k == "source":
            if str(v or "").lower() not in SOURCES:
                continue
            sets.append("source=?")
            vals.append(str(v).lower())
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
    """Give up on a wish — and announce that outcome, once.

    This is the ONE place a wish is declared failed (`wishes_max_attempts` in
    the worker is the only caller), so the notification lives here rather than
    at the call sites: every path reaches it exactly once. A wish merely
    re-marked `wanted` for another attempt stays silent on purpose — the user
    asked to hear about the outcome, not about every try (see server/events.py).

    Re-marking a wish that is ALREADY failed is not a new outcome and does not
    repeat the frame.
    """
    err = str(error or "")
    before = get_wish(wid) or {}
    if not before:
        return  # no such wish: there is nothing to mark and nothing to announce
    if before.get("status") == "failed":
        _mark(wid, status="failed", last_error=err[:400], attempts=attempts)
        return
    _mark(wid, status="failed", last_error=err[:400], attempts=attempts)
    artist = str(before.get("artist") or "").strip()
    title = str(before.get("title") or "").strip()
    label = f"{artist} — {title}" if artist and title else (title or artist or "Wish")
    # A notification must never be the reason wish bookkeeping fails.
    try:
        from server import events
        events.emit("wish_failed", f"Wish failed: {label}",
                    f"Gave up after {attempts} attempt(s): {err[:200]}",
                    {"link": "/soulseek", "wish_id": wid,
                     "release_mbid": str(before.get("release_mbid") or ""),
                     "release": before.get("release") or {},
                     "error": err[:300]})
    except Exception:
        pass


def mark_not_found(wid, error, attempts=None):
    """No usable copy exists on the network — the terminal end of a NOT-FOUND
    acquisition, announced once as `wish_not_found`.

    This is the outcome the user never used to hear: the search ran, found
    nothing, and the row sat there being re-searched on a timer with no word
    about it. A not-found wish is NOT retried automatically (see the retry
    policy at the bottom of this module) — re-asking a network that already
    answered costs an interval each time and changes nothing, while a manual
    retry from the queue is one press and re-arms it (rearm()).

    Re-marking a wish that is already `not_found` is not a new outcome and
    does not repeat the frame, exactly like mark_failed.
    """
    err = str(error or "")
    before = get_wish(wid) or {}
    if not before:
        return  # no such wish: nothing to mark and nothing to announce
    fields = {"status": "not_found", "last_error": err[:400],
              "not_found": int(before.get("not_found") or 0) + 1}
    if attempts is not None:
        fields["attempts"] = attempts
    already = before.get("status") == "not_found"
    _mark(wid, **fields)
    log("warn", f"Wish not found: {before.get('artist')} — "
                f"{before.get('title')} ({err[:120] or 'nothing usable'})")
    if already:
        return
    artist = str(before.get("artist") or "").strip()
    title = str(before.get("title") or "").strip()
    label = f"{artist} — {title}" if artist and title else (title or artist or "Wish")
    # A notification must never be the reason wish bookkeeping fails.
    try:
        from server import events
        events.emit("wish_not_found", f"Nothing found: {label}",
                    f"Searched {int(fields['not_found'])} time(s) and found no "
                    f"usable copy — it is not searched again until you retry it "
                    f"from the queue. {err[:160]}".strip(),
                    {"link": "/soulseek", "wish_id": wid, "outcome": "not_found",
                     "attempts": int(fields.get("attempts") or 0),
                     "release_mbid": str(before.get("release_mbid") or ""),
                     "release": before.get("release") or {},
                     "error": err[:300]})
    except Exception:
        pass


def mark_wanted(wid, error="", attempts=None, retry_at=None, not_found=None):
    """Back in the queue for another automatic attempt.

    *retry_at* is when the NEXT attempt may run: the worker passes the
    backoff's own end after a transient failure, and 0 (the default) means
    "as soon as the interval allows".

    *not_found* is the empty-search counter after THIS search (the worker
    passes it for a search that found nothing): without it the count only ever
    moved when the wish ended, so `wishes_not_found_attempts: 3` could never be
    reached — every retry read the same stored 0, decided "1 of 3" and searched
    the network again forever instead of ending `not_found` as the policy says."""
    fields = {"status": "wanted", "last_error": str(error or "")[:400]}
    if attempts is not None:
        fields["attempts"] = attempts
    if retry_at is not None:
        fields["retry_at"] = float(retry_at or 0)
    if not_found is not None:
        fields["not_found"] = int(not_found)
    _mark(wid, **fields)


# --------------------------------------------------------------------------- #
# Retry policy — the ONE place an acquisition decides "again" or "done"
# --------------------------------------------------------------------------- #
# Outcomes are CLASSIFIED, never guessed from a status: a search that came back
# with nothing usable is not a failure to try harder (the network does not have
# it), while a refused/absent slskd, a MusicBrainz outage or a failed
# verification is transient and worth another attempt. What that buys:
#
#   * not found — the download item is not retried AT ALL: the release is handed
#                 to the wishlist, which is a standing request the user made
#                 rather than a retry of a failure. After
#                 `wishes_not_found_attempts` empty searches (default 3, 0 =
#                 never give up) the WISH ends `not_found`: terminal, no
#                 further automatic searches, announced once as
#                 `wish_not_found`, re-armed only by a manual retry.
#   * transient — retried with backoff (`wishes_retry_backoff_minutes`,
#                 default 30, doubling per attempt, capped at a day, 0 = no
#                 extra wait) up to `wishes_max_attempts` (0 = retry forever).
#                 At the cap the wish ends `failed` — terminal, announced once
#                 as `wish_failed` — and the row's retry re-arms it.
#
# The numbers are the user's (config keys with validators in mlo/config.py);
# the policy itself lives here so the worker, the queue view and the CLI all
# answer the same question the same way.
_NOT_FOUND_HINTS = (
    "no candidate folder", "nothing found", "nothing usable", "no usable",
    "nothing was found", "no verified match", "not on soulseek", "no results",
)


def outcome_of(error):
    """Classify why an acquisition did not land: ``"not_found"`` or
    ``"transient"`` (see the policy above)."""
    low = str(error or "").lower()
    return "not_found" if any(h in low for h in _NOT_FOUND_HINTS) else "transient"


def _int(cfg, key, default):
    try:
        return int((cfg or {}).get(key, default))
    except (TypeError, ValueError):
        return default


def not_found_attempts(cfg):
    """Empty searches a wish may have before it ends `not_found` (0 = never)."""
    return max(0, _int(cfg, "wishes_not_found_attempts", 3))


def max_attempts(cfg):
    """Transient attempts a wish may burn before it ends `failed` (0 = forever)."""
    return max(0, _int(cfg, "wishes_max_attempts", 0))


def retry_delay(cfg, attempts):
    """Seconds to wait before the next automatic attempt after a failure.

    Doubling backoff from `wishes_retry_backoff_minutes`, capped at a day: a
    peer that is down, or a MusicBrainz that is rate-limiting, should not be
    hammered once per interval — and a week-long outage must not push the
    retry out by weeks either."""
    step = max(0, _int(cfg, "wishes_retry_backoff_minutes", 30)) * 60
    if not step:
        return 0.0
    n = max(1, min(int(attempts or 1), 8))
    return float(min(86400, step * (2 ** (n - 1))))


def due_at(wish, cfg):
    """When this wish may be searched again ON ITS OWN.

    The interval's own end, or a pending transient backoff, whichever is
    later — so a failure waits its backoff even on a tick that lands right
    after it."""
    interval = max(1, _int(cfg, "wishes_interval_hours", 6)) * 3600.0
    if is_terminal(wish, cfg):
        return float("inf")
    return max(float(wish.get("last_search") or 0) + interval,
               float(wish.get("retry_at") or 0))


def is_terminal(wish, cfg):
    """Will the worker search this wish again on its own? (See the policy.)

    `imported`/`not_found` never are; `failed` is terminal once its attempt cap
    has been spent, and with no cap configured it keeps being retried — which
    is what `wishes_max_attempts: 0` has always meant."""
    status = str((wish or {}).get("status") or "")
    if status in TERMINAL_STATUSES:
        return True
    if status == "failed":
        cap = max_attempts(cfg)
        return bool(cap) and int(wish.get("attempts") or 0) >= cap
    return False


def rearm(wid):
    """Undo a terminal outcome: the user asked for this wish to be searched
    again. Manual retry is the ONLY way back from `not_found` / a spent
    `failed` — which is exactly what makes those states terminal."""
    _mark(wid, status="wanted", attempts=0, not_found=0, retry_at=0, last_error="")
    return get_wish(wid)


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
            if alb.get("pending"):
                # A framework album carries the release id but holds NO audio:
                # counting it as owned would refuse the very download that is
                # meant to fill it ("already in your library").
                continue
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
            # A wish the user filled by hand lands here, so this path notifies
            # too — otherwise "found" would only ever mean "the pipeline did
            # it" and a manual download would arrive silently.
            try:
                from server import events
                artist = str(w.get("artist") or "").strip()
                title = str(w.get("title") or "").strip()
                label = f"{artist} — {title}" if artist and title else (title or artist or "Wish")
                events.emit("wish_found", f"Wish found: {label}",
                            "It is in your library now.",
                            {"wish_id": w["id"], "release_mbid": mbid,
                             "release": w.get("release") or {}},
                            config=cfg)
            except Exception:
                pass
    return resolved
