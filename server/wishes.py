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

STATUSES = ("wanted", "searching", "imported", "failed", "available", "not_found",
            "background")

# `background` is the FALLBACK WALK's own resting place (spec R150-R153): the
# acquisition asked every ranked candidate it was allowed and none answered, so
# the release is NOT dropped and its framework album is NOT taken down — the
# wish keeps its place in the pipeline as a standing background request the
# worker re-searches on its own ticks until one of the candidates lands. It is
# deliberately NOT terminal (nothing about it is an answer), and it is not
# `wanted` either: the queue shows these rows in their own section, apart from
# the releases being actively searched and from the ones that need the user.
#
# Terminal statuses: the worker never searches one of these again on its own.
# `not_found` always is — "the network does not have it" is an ANSWER, and
# re-asking the same question on a timer is the silent stalling the retry
# policy below exists to stop. `failed` becomes terminal once its attempt cap
# is reached (see is_terminal).
TERMINAL_STATUSES = ("imported", "not_found")

# Where a wish came from — one field, so every surface labels a row the same
# way: "musicbrainz" is a framework album ("Add to library" on a MusicBrainz
# entity), "soulseek" a want saved by hand from the Soulseek page, "auto" the
# entry an auto-import job offers to save, "youtube" the one an auto-import job
# for a Digital Media music-video release offers (the release is fetched from
# YouTube, not searched for on the network — server.soulseek_auto). "" is a
# wish saved before this was recorded.
SOURCES = ("musicbrainz", "soulseek", "auto", "youtube")


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
                    release_json TEXT NOT NULL DEFAULT '',
                    -- The FALLBACK list: every eligible edition of the release
                    -- group, ranked best first by the ONE release-choice policy
                    -- (`mlo.release_choice`, spec R150) as JSON
                    -- [{mbid,title,score}]. The album and its wish stay ONE
                    -- (R142); what the search does with nothing found for its
                    -- best candidate is move to the next entry instead of
                    -- giving the album up.
                    candidates TEXT NOT NULL DEFAULT '',
                    -- Which entry of `candidates` is being asked for RIGHT NOW
                    -- (0-based). Reset to 0 by rearm(): a fresh request starts a
                    -- fresh walk. See the fallback section below.
                    candidate INTEGER NOT NULL DEFAULT 0
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
            # Same rule for the fallback walk (spec R150): an older database
            # gets an EMPTY list and index 0, i.e. exactly the single-candidate
            # behaviour every wish had before the walk existed — its own key is
            # the one candidate to ask the network for.
            if "candidates" not in cols:
                c.execute("ALTER TABLE wishes ADD COLUMN candidates TEXT NOT NULL DEFAULT ''")
            if "candidate" not in cols:
                c.execute("ALTER TABLE wishes ADD COLUMN candidate INTEGER NOT NULL DEFAULT 0")


# --------------------------------------------------------------------------- #
# The release identity — WHICH pressing a row is about
# --------------------------------------------------------------------------- #
# ONE block with the same keys wherever it appears (a job row, a wish row, a
# notification), so no surface has to reconstruct a release's facts from
# whatever spelling it happens to hold. The order is what identifies a
# PRESSING: its catalog number and the medium it is on first, then where and
# when it came out — `country` as MusicBrainz's singular first release event
# and `countries` as every one of them — and how much it carries, then the
# edition's own disambiguation and the status MusicBrainz states for it
# (Official / Promotion / Bootleg / …).
#
# EVERY key is always present. A fact the pipeline could not resolve is empty
# and renders as absent — a MusicBrainz outage, or a wish whose release was
# never looked up, leaves a row that still says what it does know and never a
# row that fails to render.
RELEASE_KEYS = ("id", "title", "artist", "date", "country", "countries",
                "status", "media", "track_count", "disambiguation",
                "catalog_number", "label")


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
    # Every country the release came out in: `countries` is release_lookup's
    # per-event list ({code, date, …}), and a payload stating only the singular
    # country (a job's own compact summary) answers with that one code — a row
    # that named one country claimed the release was out in one country. A
    # payload that states neither answers with no countries at all.
    country = str(rel.get("country") or "").strip()
    countries = []
    for event in rel.get("countries") or []:
        code = str((event.get("code") if isinstance(event, dict) else event)
                   or "").strip()
        if code and code.upper() not in {c.upper() for c in countries}:
            countries.append(code)
    if not countries and country:
        countries = [country]
    return {
        "id": release_choice.release_id(rel) or str(release_mbid or "").strip(),
        "title": str(rel.get("title") or "").strip(),
        "artist": artist,
        "date": str(rel.get("date") or "").strip(),
        "country": country,
        "countries": countries,
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
    # The fallback walk (see the section below): the ranked candidates this
    # acquisition may ask the network for, and which of them is being asked for
    # now. ALWAYS a list and an index — an empty list is a wish with ONE
    # candidate (its own key), so every reader sees the documented shape and
    # the walk is a no-op.
    walked = []
    stored = str(d.pop("candidates", "") or "")
    if stored:
        try:
            known = json.loads(stored)
            if isinstance(known, list):
                walked = [c for c in known
                          if isinstance(c, dict) and str(c.get("mbid") or "")]
        except Exception:
            pass                        # a corrupt column is not a failed row
    d["candidates"] = walked
    try:
        d["candidate"] = max(0, int(d.get("candidate") or 0))
    except (TypeError, ValueError):
        d["candidate"] = 0
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
            "WHEN 'background' THEN 2 WHEN 'failed' THEN 3 ELSE 4 END, "
            "added_at DESC"
        ).fetchall()
    from mlo.config import load_config

    cfg = load_config()                 # ONE read for the whole batch
    return [_with_terminal(_row(r), cfg) for r in rows]


def get_wish(wid):
    with _conn() as c:
        r = c.execute("SELECT * FROM wishes WHERE id=?", (int(wid),)).fetchone()
    return _with_terminal(_row(r)) if r else None


def find_for_release(release_mbid, release_group_mbid=""):
    """The wish already standing for this release, whichever ID keys it.

    A release reaches this store under whichever id its caller held:
    "Add to library" resolves an EDITION and keys the wish by its release id,
    while a wish saved from a musicbrainz.org album link carries the RELEASE
    GROUP id (that is what the link holds) and the auto-importer's own offer
    carries the release id. Two ids for one pressing were two wish rows, and
    two rows are two searches — each with its own job, both downloading the
    same album. So a caller that is about to record a release asks here first
    and reuses what it finds.

    The keys are tried first (a wish saved from a group page and then re-added
    by its edition), then the stored identity: `release` names the edition the
    row is actually waiting for, which is what an add holding the release id
    has to match a group-keyed row by.
    """
    ids = {str(x or "").strip().lower() for x in (release_mbid, release_group_mbid)}
    ids.discard("")
    if not ids:
        return None
    rows = list_wishes()
    for w in rows:
        if str(w.get("release_mbid") or "").strip().lower() in ids:
            return w
    for w in rows:
        if str((w.get("release") or {}).get("id") or "").strip().lower() in ids:
            return w
    return None


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
            try:
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
            except sqlite3.IntegrityError:
                # `release_mbid` is UNIQUE and the read above is a CHECK: a
                # second writer (another app instance on this same wishes.db)
                # can insert between the two, which used to raise out of an
                # add that simply wanted what is now there. That row IS this
                # wish, so the answer is that row.
                row = c.execute("SELECT * FROM wishes WHERE release_mbid=?",
                                (release_mbid,)).fetchone()
                if row is None:
                    raise
                return _row(row)
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


def mark_wanted(wid, error="", attempts=None, retry_at=None, not_found=None,
                candidate=None):
    """Back in the queue for another automatic attempt.

    *retry_at* is when the NEXT attempt may run: the worker passes the
    backoff's own end after a transient failure, and 0 (the default) means
    "as soon as the interval allows".

    *not_found* is the empty-search counter after THIS search (the worker
    passes it for a search that found nothing): without it the count only ever
    moved when the wish ended, so `wishes_not_found_attempts: 3` could never be
    reached — every retry read the same stored 0, decided "1 of 3" and searched
    the network again forever instead of ending `not_found` as the policy says.

    *candidate* is where the FALLBACK WALK stands (spec R150-R153), when the
    caller owns that position: an attempt that walked the ranked editions and
    found nothing puts the pointer back on the BEST one, because the next
    attempt starts there and a row left pointing at the last edition reached
    would describe a search that is not the one about to run."""
    fields = {"status": "wanted", "last_error": str(error or "")[:400]}
    if attempts is not None:
        fields["attempts"] = attempts
    # ALWAYS written, with the default 0 — and that is the fix for a live stall:
    # `retry_at` is the TRANSIENT-failure backoff and nothing else (`due_at`
    # reads its presence as "the last attempt failed"), so a settle that has no
    # backoff to give must not leave a previous failure's stamp behind. It did,
    # because the column was skipped when the argument was None — and the two
    # settles that pass none (`_settle_attempt`'s empty-search branch and
    # `mark_background`, which also writes it now) run AFTER a failure often
    # enough. The stale stamp is in the past by then, `due_at` returns it as
    # "due", and the wish is re-searched on every tick (~2 min) instead of on
    # its interval — the opposite of what the paragraph above promises.
    fields["retry_at"] = float(retry_at or 0)
    if not_found is not None:
        fields["not_found"] = int(not_found)
    if candidate is not None:
        fields["candidate"] = max(0, int(candidate))
    _mark(wid, **fields)


# --------------------------------------------------------------------------- #
# The fallback walk — WHICH ranked candidate the search asks the network for
# --------------------------------------------------------------------------- #
# "Add to library" on a release group resolves ONE edition per the release-choice
# policy — and the group's OTHER eligible editions are ranked behind it by that
# same policy. This is where that ranking lives, so an album whose best pressing
# the network does not have is no longer given up on while four other pressings
# of it sit in the same ranking (spec R150-R152).
#
# The stored list is written ONCE, when the add is recorded, from the editions
# the provider already returned (`integrations.group_targets`) — but what is
# stored is the EDITIONS AND THEIR FACTS, never the order they were ranked in:
# every read re-derives the order from those facts with the policy in force then
# (`walked_rows` → `mlo.release_choice.rank_stored`), so a release queued before
# a rule changed is searched by the rule in force now. No read spends a
# MusicBrainz request, and no read writes back. The walk itself is FORWARD only,
# one entry per exhausted candidate, so it terminates by construction — and its
# end is the ordinary not-found outcome, reported with every edition it asked
# for.
#
# `release_mbid` stays the wish's KEY (the release the add was for; `UNIQUE`, and
# what a re-add matches on), so the index — not the key — is what moves.
def fallback_limit(cfg=None):
    """How many ranked candidates ONE walk may ask (`soulseek_fallback_candidates`).

    The user's own number (1 = the best edition and nothing behind it, exactly
    the behaviour before the walk existed). The walk clamps it to the list it
    was handed, so a group with fewer eligible editions than this simply ends
    at the end of its own list — never an error and never a wait for a
    candidate that does not exist.
    """
    return max(1, _int(cfg, "soulseek_fallback_candidates", 3))


def walked_rows(wish, cfg=None):
    """The wish's stored candidates in the order the CURRENT policy puts them.

    The stored list is a snapshot of the editions an add resolved — never an
    authority on their order. It is re-ranked here from the facts each row
    carries (`mlo.release_choice.rank_stored`: the same rules the release-group
    page shows, no MusicBrainz request), so a release queued before a rule
    changed is searched by the rule in force now. EVERY reader of the walk goes
    through this function — the order the worker asks in, the row's "Release 2
    of 5", and the `tried` list behind it — because one pure, deterministic
    order over one stored list is what keeps those three from disagreeing.

    A list whose rows state no facts comes back exactly as stored (there is
    nothing to re-rank by), and no row is ever dropped.
    """
    rows = [r for r in ((wish or {}).get("candidates") or []) if isinstance(r, dict)]
    if len(rows) < 2:
        return rows
    from mlo import release_choice

    return release_choice.rank_stored(rows, cfg)


def walk_length(wish, cfg=None):
    """How many candidates this wish's walk really has, capped by the setting.

    Len < 2 is not a walk: the wish's own key is its one candidate, and the row
    shows nothing about positions (see `candidate_state`).
    """
    rows = walked_rows(wish, cfg)
    return min(len(rows), fallback_limit(cfg)) if rows else 0


def candidate_state(wish, cfg=None):
    """Which candidate this wish is being searched for, as ONE block or None.

    None when the walk has nothing to say: no list at all, or a single entry.
    "Release 1 of 1" is noise, and a caller that shows nothing is right about a
    wish whose list has one step. With a real walk every key is present:

        {index, total, label, mbid, title, tried: [{mbid, title}, ...]}

    *total* is how many candidates the walk may ask (`walk_length`: the ranked
    list capped by `soulseek_fallback_candidates`), *label* is the sentence the
    surfaces show ("Release 2 of 5") — one wording, built here so the queue row,
    the album's page and the notifications cannot disagree about where the
    search is — and *tried* is what already came back empty in THIS attempt, so
    an end-of-walk report names the whole walk rather than only its last step.
    """
    total = walk_length(wish, cfg)
    if total < 2:
        return None
    rows = walked_rows(wish, cfg)
    try:
        index = max(0, int((wish or {}).get("candidate") or 0))
    except (TypeError, ValueError):
        index = 0
    index = min(index, total - 1)
    here = rows[index] or {}
    return {
        "index": index,
        "total": total,
        "label": candidate_label(index, total),
        "mbid": str(here.get("mbid") or ""),
        "title": str(here.get("title") or ""),
        "tried": [{"mbid": str(r.get("mbid") or ""),
                   "title": str(r.get("title") or "")} for r in rows[:index]],
    }


def candidate_of(wish):
    """The one ranked candidate this attempt must ask for.

    The wish's own key is the fallback of the fallback: a wish recorded without
    a list — every wish saved before the walk existed, and every NAME-keyed wish
    — is a single-candidate acquisition and answers exactly as it always did.
    """
    state = candidate_state(wish)
    if state:
        return {"mbid": state["mbid"], "title": state["title"]}
    mbid = str((wish or {}).get("release_mbid") or "")
    if not mbid:
        return None
    return {"mbid": mbid, "title": str((wish or {}).get("title") or "")}


def set_candidates(wid, rows):
    """Record the ranked candidate list of a wish that has none yet.

    Only ever FILLS an empty list. A wish already walking keeps the ROWS it was
    recorded with — a second add cannot replace the editions behind a search
    that is in flight — but their ORDER is not stored state: every read derives
    it afresh with the policy in force then (`walked_rows`), so nothing about a
    queued release is frozen at add time. `rearm` is what starts a fresh walk.

    ONE entry is stored as readily as three: a list is what tells the store this
    wish carries the ranked editions of an album request, and that is what keeps
    a spent walk in the BACKGROUND instead of ending the wish (spec R153) —
    including a release group whose only eligible edition is the one it was
    added for. A wish with NO list is the other thing: a wishlist row that names
    no edition, which still ends `not_found` when its searches run out.

    Never raises: the wish exists either way, and a list that cannot be stored
    leaves the single-candidate behaviour.
    """
    kept = []
    for r in (rows or []):
        mbid = str((r or {}).get("mbid") or "").strip()
        if not mbid:
            continue
        row = {"mbid": mbid, "title": str((r or {}).get("title") or "")}
        score = (r or {}).get("score")
        if score is not None:
            row["score"] = score
        # The catalog numbers the edition states, kept so the WALK can tell two
        # releases that are one search apart (`mlo.release_choice
        # .distinct_pressings` — the number is what a CD search is keyed on).
        catalogs = [str(n) for n in ((r or {}).get("catalog_numbers") or []) if str(n).strip()]
        if catalogs:
            row["catalog_numbers"] = catalogs
        # …and the edition's OWN facts, kept for the same reason the number is:
        # the WALK ranks this list every time it reads it
        # (`mlo.release_choice.rank_stored`), so the facts the policy decides on
        # have to travel with the row. The stored list is a snapshot of the
        # editions; the ORDER is the reader's, and never the snapshot's. Only
        # what a row supplies is kept — a fact nobody stated is absent, and the
        # ranking then answers with the rules that row CAN answer.
        for key in ("date", "status", "country", "disambiguation"):
            value = str((r or {}).get(key) or "").strip()
            if value:
                row[key] = value
        formats = [str(f).strip() for f in ((r or {}).get("medium_formats") or [])
                   if str(f).strip()]
        if formats:
            row["medium_formats"] = formats
        try:
            tracks = int((r or {}).get("track_count") or 0)
        except (TypeError, ValueError):
            tracks = 0
        if tracks > 0:
            row["track_count"] = tracks
        kept.append(row)
    if not kept:
        return None
    try:
        with _lock:
            with _conn() as c:
                cur = c.execute(
                    "UPDATE wishes SET candidates=?, updated_at=? "
                    "WHERE id=? AND candidates=''",
                    (json.dumps(kept), time.time(), int(wid)))
                if cur.rowcount == 0:
                    return None
        return get_wish(wid)
    except Exception:
        return None     # a list that cannot be stored is not a failed add


def advance_candidate(wid, cfg=None):
    """Move this wish on to the NEXT ranked candidate — or answer None.

    The single step of the walk, and the whole of it: None means the walk is
    spent (no list, one candidate, or `soulseek_fallback_candidates` already
    asked), and the caller then settles the attempt — which is what makes the
    walk finite rather than a loop.

    The empty-search counter is deliberately NOT touched here: it counts the
    empty WALKS of this acquisition (one per attempt, since an attempt asks
    every candidate it may), and `_settle_attempt` is where that one empty
    search is recorded — exactly as it always was, so a walk cannot run out of
    the user's budget by walking. `attempts` — the transient-failure counter,
    and the acquisition's own — is not reset either: this is the same
    acquisition.

    *retry_at* is cleared so the new candidate is asked at the interval's own
    pace rather than inheriting the last one's backoff, and the status goes back
    to `wanted`: the search that found nothing has ended, and the row has to say
    so instead of sitting in `searching` while the walk moves on.
    """
    before = get_wish(int(wid)) or {}
    rows = walked_rows(before, cfg)
    total = walk_length(before, cfg)
    try:
        index = max(0, int(before.get("candidate") or 0))
    except (TypeError, ValueError):
        index = 0
    if len(rows) < 2 or total < 2 or index + 1 >= total:
        return None
    nxt = rows[index + 1] or {}
    now, nxt_title = rows[index] or {}, (nxt.get("title") or nxt.get("mbid") or "")
    _mark(wid, status="wanted", candidate=index + 1, retry_at=0,
          last_error=f"no copy of {now.get('title') or now.get('mbid')} was found — "
                     f"trying {candidate_label(index + 1, total)}: {nxt_title}")
    log("info", f"Wish candidate {index + 1} of {total} came back empty — "
                f"trying {candidate_label(index + 1, total)}: {nxt_title}")
    return candidate_state(get_wish(wid), cfg)


def restart_walk(wid, error=""):
    """Put the walk back on its BEST candidate, keeping the status.

    One attempt walks the ranked editions in order, and the next attempt starts
    at the top again: the network that had none of them an hour ago may have the
    first one now, and the ranking exists precisely so the best edition is the
    one asked for first (spec R150-R153). Called where an attempt begins
    (`wishes_worker._run_one`), so a row interrupted mid-walk does not resume
    halfway down a list nobody is looking at any more. The empty-search counter
    is left alone — it belongs to the acquisition, and `_settle_attempt` is what
    advances it.
    """
    _mark(wid, candidate=0, last_error=str(error or "")[:400])


def mark_background(wid, error="", attempts=None, not_found=None):
    """The walk is spent: keep the release as a BACKGROUND request.

    Nothing about an empty walk is an answer — the album is the same album and
    the editions are the same ranked editions — so the wish is NOT ended and its
    framework album is NOT taken down (compare `mark_not_found`, which is the
    terminal "the network does not have it" for a wish with no walk). It moves
    to `background`: still non-terminal, still searched on the worker's own
    ticks, but shown in its own section instead of among the releases being
    actively walked, so a row that has asked everything it may does not read as
    a search that is stuck.

    The counters keep their meanings (`attempts` = transient attempts burned,
    `not_found` = empty searches) so the retry/backoff policy still paces the
    background passes exactly as it paces everything else.
    """
    fields = {"status": "background", "last_error": str(error or "")[:400],
              # The spent backoff goes with it, for the same reason
              # `mark_wanted` always writes one: a background pass is paced by
              # the INTERVAL, and inheriting a failure's `retry_at` (in the past
              # by now) made `due_at` say "due" on every tick, so a spent walk
              # was re-walked every ~2 minutes instead of every
              # `wishes_interval_hours`.
              "retry_at": 0}
    if attempts is not None:
        fields["attempts"] = int(attempts)
    if not_found is not None:
        fields["not_found"] = int(not_found)
    _mark(wid, **fields)
    log("info", f"Wish moved to the background after a spent walk: "
                f"{error[:160] or 'every ranked candidate came back empty'}")


def candidate_label(index, total):
    """"Release 2 of 5" — the ONE wording for a position in the walk."""
    return f"Release {int(index) + 1} of {int(total)}"


def walk_report(wish, err, cfg=None):
    """The report for a wish whose walk asked every candidate it may.

    "Nothing found" is the same statement it always was, but a walk that asked
    the network for three editions owes the user the fact that the walk is
    SPENT and which editions it asked for — the row then says the release stays
    in the background rather than reading as a search that gave up. The caller's
    own *err* is kept inside the sentence, so what the search actually answered
    — and its classification (`outcome_of`) — survives.
    """
    state = candidate_state(wish, cfg)
    rows = walked_rows(wish, cfg)
    total = walk_length(wish, cfg) or len(rows)
    if state:
        asked = state["tried"] + [{"mbid": state["mbid"], "title": state["title"]}]
    else:
        asked = [{"mbid": str(r.get("mbid") or ""), "title": str(r.get("title") or "")}
                 for r in rows[:1]]
    names = ", ".join(str(t.get("title") or t.get("mbid")) for t in asked[:6])
    more = f" (+{len(asked) - 6} more)" if len(asked) > 6 else ""
    if not asked:
        return err
    return (f"all {len(asked)} of {total} ranked edition(s) the walk may ask were "
            f"searched and none was found: {names}{more} — {err}")


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
#                 With a ranked list (the fallback walk above) that same budget
#                 is what exhausts ONE CANDIDATE — the search then moves on to
#                 the next ranked edition, and only the LAST candidate's spent
#                 budget ends the wish. One policy, asked per candidate; the
#                 walk adds no scheme of its own.
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
# A search that FOUND copies and had every one of them refused. The distinction
# matters because the two are different facts with different ends: "not found"
# is the network having nothing (the not-found budget's own case, and the one a
# background walk keeps asking about), while "rejected" is the network having
# copies this pipeline would not accept — every candidate in the batch was
# refused on grading, on the `.log` a CD folder must carry, or on verification.
# It is NOT a transient failure: retrying the same folder with the same settings
# refuses it again, so classifying it as transient is what left a release-group
# walk re-asking edition 1 for ever instead of moving on to the next ranked
# edition (see `_settle_attempt`, and R151/R153).
_REJECTED_HINTS = ("every candidate was rejected",)


def outcome_of(error):
    """Classify why an acquisition did not land: ``"not_found"``,
    ``"rejected"`` or ``"transient"`` (see the policy above)."""
    low = str(error or "").lower()
    if any(h in low for h in _REJECTED_HINTS):
        return "rejected"
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

    A FAILED attempt keeps its own schedule — `retry_at`, the doubling backoff
    from `wishes_retry_backoff_minutes` — because that is what the backoff is
    for: a peer that is down, or a MusicBrainz that is rate-limiting, is
    retried when it has had a moment, not when the next periodic look is due.
    The interval (`wishes_interval_hours`) is the OTHER case, and the common
    one: a wish whose search simply found nothing yet, which is re-asked on that
    cadence so the network is polled at a sane rate.

    Both used to be `max(interval, retry_at)`, which read as "retrying at 12:10"
    five hours after a download started: a 30-minute backoff was swallowed whole
    by the 6-hour interval, so a failure waited the interval anyway and the
    number the row showed had nothing to do with the failure it followed.
    `retry_at` is only ever set by that failure path, so its presence IS "the
    last attempt failed" — and with the backoff turned off (0 minutes) nothing
    sets it, which is what keeps the interval as the floor in that case."""
    if is_terminal(wish, cfg):
        return float("inf")
    retry = float(wish.get("retry_at") or 0)
    if retry:
        return retry
    interval = max(1, _int(cfg, "wishes_interval_hours", 6)) * 3600.0
    return float(wish.get("last_search") or 0) + interval


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
    `failed` — which is exactly what makes those states terminal.

    A fresh request also starts a FRESH WALK: the candidate index goes back to
    the top of the list (`candidate=0`). Retrying an album whose every ranked
    edition was already asked for must ask its best edition again — the network
    that had none of them yesterday may have the first one today — not resume
    at the last one it happened to reach (spec R150)."""
    _mark(wid, status="wanted", attempts=0, not_found=0, retry_at=0,
          candidate=0, last_error="")
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
            count = alb.get("track_count")
            if alb.get("pending") or (count is not None and not count):
                # A framework album is a REQUEST on disk, not the album: the
                # folder "Add to library" creates before any audio exists,
                # carrying the release's MBIDs in its marker — which is exactly
                # what makes counting it as "owned" so easy and so wrong. The
                # pipeline then refuses to download the very album that is meant
                # to fill it ("already in your library"), and the add ends
                # terminal with nothing behind it. `pending` says so in the
                # library's own words (server.library's framework rows), and a
                # row that STATES zero tracks says the same thing off its own
                # files — it covers a payload whose rows predate that flag. A
                # row that states nothing is left alone: guessing there would
                # refuse a real album its own download.
                #
                # Counting it as owned would refuse the very download that is
                # meant to fill it.
                continue
            meta = alb.get("meta") or {}
            for key in ("MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_RELEASEGROUPID"):
                val = str(meta.get(key) or "").strip().lower()
                if val:
                    owned.setdefault(val, alb.get("path"))
    return owned


def owned_path(owned, *mbids):
    """The library folder that holds one of *mbids*, or "".

    The question every caller of `owned_mbids` ends up asking — "is this
    release here, and where?" — answered once, so an id list and the map are
    the only things a caller needs. Empty ids are skipped (a wish that names no
    release has no folder to find) and the first id the library knows wins.
    """
    for mbid in mbids:
        key = str(mbid or "").strip().lower()
        if key and key in (owned or {}):
            return str(owned[key] or "")
    return ""


def reconcile_with_library(cfg=None):
    """Mark open wishes whose MusicBrainz release is already in the library as
    imported. Returns the count of freshly resolved wishes.

    Matches the release ID against the library's ``MUSICBRAINZ_ALBUMID`` tags
    (or its release-group id) so a wish saved from a release-group page still
    resolves. Cheap enough to run at the end of every worker cycle.

    A folder with NO AUDIO is not the album, and this is the check that keeps
    the difference: a framework album (the "Add to library" folder — a marker,
    a placeholder cover, no tracks) carries its release's MBIDs in its marker,
    and marking the wish imported on that evidence is what turned an add into a
    terminal wish with an empty folder standing behind it: the worker never
    searched it again, and the library showed an album that was never there.
    The audio is the test (`server.pending_albums.is_placeholder` — one listing
    per candidate wish, never one per album in the library), and a folder that
    fails it leaves its wish open for the search that will fill it.
    """
    from server import pending_albums

    owned = owned_mbids(cfg)
    resolved = 0
    for w in list_wishes():
        if w["status"] in ("imported",):
            continue
        mbid = str(w["release_mbid"]).strip().lower()
        if mbid and mbid in owned and not pending_albums.is_placeholder(owned[mbid]):
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
