"""Artist watches — add an artist's NEW releases, never its discography.

A *watch* follows one MusicBrainz artist and adds what it releases from now on.
The point of the feature is what a watch REFUSES to do: a fresh watch never
enumerates a back catalogue. Three rules, in this order, are what make that
true, and every one of them is enforced here rather than at a call site:

1. **New only.** A release group counts as new when its FIRST release date is
   after the watch was created; a release group MusicBrainz holds no date for
   is never new (an undated group is far more often an old one nobody dated
   than a brand-new release). ``policy: backfill`` is the explicit per-watch
   opt-in that widens this rule — and only this one.
2. **A hard per-cycle cap.** ``max_per_cycle`` bounds how many albums ONE
   cycle may queue, whatever the policy: nothing about this feature can ever
   enqueue a discography in one pass.
3. **The type filter, the allow-list and the veto.** A release group must have
   one of the watch's ``release_types`` (MusicBrainz's own names — see
   ``mlo.naming``); a non-empty ``include`` list allows ONLY those groups, and
   ``exclude`` never queues a group whatever the policy.

Nothing here is a second pipeline. One MusicBrainz release-group browse per
artist per cycle (through ``server.integrations``, cached and rate-limited —
never one request per release group) is filtered against what the library
already holds (``wishes.owned_mbids``) and what the wish queue already has,
and each winner becomes a framework album + wish through
``server.pending_albums.create`` — the same helper "Add to library" uses, so
the folder, the manifest, the placeholder cover and the wish are created once,
by that code. The existing wish worker searches it; this module never searches
anything and never downloads anything.

Duplicate suppression is the WISH QUEUE, not the folder-lock registry: what a
watch must not do is queue a second download of a release, and the wish queue
holds exactly that record — a release mid-import still has its wish row, a
queued-but-unfilled release has its framework marker, and a filled one is in
``owned_mbids``. So a cycle that lands while the app is importing the release
it just found finds it and moves on (see :func:`queued_release_groups`).

The watch list lives in its own SQLite database beside wishes.db, so it
survives restarts and travels with the ``.mlo/data`` folder.
"""
import json
import os
import re
import sqlite3
import threading
import time

from mlo import import_policy
from mlo.naming import PRIMARY_RELEASE_TYPES, SECONDARY_RELEASE_TYPES

# Reentrant: add_watch()/_record() hold the lock while calling _conn(), and on
# FIRST use _conn() creates the schema under this same lock (the trap
# server.wishes documents for its own RLock).
_lock = threading.RLock()

# The two policies a watch may have. "new_only" is the shipped one and the
# only one a watch is created with unless the user asks otherwise.
POLICIES = ("new_only", "backfill")
DEFAULT_POLICY = "new_only"

# What a watch has DONE with a release group it matched. "imported"/"failed"
# are derived from the wish's own status at read time (see _items) — a stored
# row is only ever "queued" (auto_add queued it), "notified" (auto_add is off,
# so the notification was the whole output) or "retry" (a transient failure: it
# was NOT handled and the next cycle may try again).
STATUSES = ("queued", "notified", "imported", "failed", "retry")

# The statuses that mean "this watch has acted on this release group": it stays
# out of every later cycle (a cancelled framework album is not silently
# resurrected; the user removes the watch or excludes the group for that).
_HANDLED = ("queued", "notified", "imported")

# Every type name a watch may select, case-insensitively compared. One
# vocabulary, in mlo.naming, shared with the naming script's own type map.
RELEASE_TYPES = tuple(PRIMARY_RELEASE_TYPES + SECONDARY_RELEASE_TYPES)

# One browse window per artist per cycle. MusicBrainz's browse has NO
# server-side sort, so integrations walks its pages itself (one cached browse
# with the same 1 req/s etiquette, never one request per release group) and
# this module sorts the honest window: a single arbitrary 100-row page would
# hide a brand-new release behind an old one for a prolific artist.
_BROWSE_WINDOW = 300

_MBID_RX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
                      r"[0-9a-f]{12}$", re.I)


class WatchExists(ValueError):
    """This artist is already watched (the API answers 409)."""


def db_path():
    from mlo.paths import app_data_dir
    return os.path.join(app_data_dir(), "artist_watch.db")


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
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS artist_watch (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    artist_mbid TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL DEFAULT '',
                    added_at REAL NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    policy TEXT NOT NULL DEFAULT 'new_only',
                    release_types TEXT NOT NULL DEFAULT '',
                    include TEXT NOT NULL DEFAULT '',
                    exclude TEXT NOT NULL DEFAULT '',
                    max_per_cycle INTEGER NOT NULL DEFAULT 1,
                    auto_add INTEGER NOT NULL DEFAULT 1,
                    last_checked_at REAL NOT NULL DEFAULT 0,
                    last_seen_release_group TEXT NOT NULL DEFAULT '',
                    checked_count INTEGER NOT NULL DEFAULT 0,
                    queued_count INTEGER NOT NULL DEFAULT 0,
                    notified_count INTEGER NOT NULL DEFAULT 0,
                    last_result TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS artist_watch_release (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    watch_id INTEGER NOT NULL,
                    release_group_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    year TEXT NOT NULL DEFAULT '',
                    release_id TEXT NOT NULL DEFAULT '',
                    wish_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'queued',
                    reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(watch_id, release_group_id)
                );
                """
            )


# --------------------------------------------------------------------------- #
# Normalization — one place every field is made safe/sane
# --------------------------------------------------------------------------- #
def clean_mbid(value):
    mbid = str(value or "").strip().lower()
    return mbid if _MBID_RX.match(mbid) else ""


def clean_types(values):
    """Lowercased, de-duplicated release-group type names, in ask order.

    Raises ValueError for a name outside MusicBrainz's vocabulary: a filter
    that could never match anything is a user error to report, not a silent
    no-op that quietly downloads nothing.
    """
    out = []
    for raw in values or []:
        name = str(raw or "").strip().lower()
        if not name:
            continue
        if name not in RELEASE_TYPES:
            raise ValueError(f"unknown release-group type {raw!r}")
        if name not in out:
            out.append(name)
    return out


def clean_mbids(values):
    """Lowercased, de-duplicated MusicBrainz IDs; ValueError on anything else."""
    out = []
    for raw in values or []:
        text = str(raw or "").strip()
        if not text:
            continue
        mbid = clean_mbid(text)
        if not mbid:
            raise ValueError(f"not a MusicBrainz ID: {raw!r}")
        if mbid not in out:
            out.append(mbid)
    return out


def _clean_policy(value, default=DEFAULT_POLICY):
    text = str(value or default).strip().lower()
    if text not in POLICIES:
        raise ValueError("policy must be " + " or ".join(POLICIES))
    return text


def _clean_cap(value, default):
    try:
        return max(1, min(50, int(value)))
    except (TypeError, ValueError):
        return max(1, min(50, int(default or 1)))


def _json_list(raw):
    try:
        value = json.loads(raw) if raw else []
    except Exception:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


# --------------------------------------------------------------------------- #
# Policy — the anti-dump rule
# --------------------------------------------------------------------------- #
def date_parts(text):
    """(year, month, day) of a MusicBrainz date, or None when undated/invalid.

    MusicBrainz dates arrive at three precisions ("1988", "1988-04",
    "1988-04-11"); a missing part reads as the EARLIEST it could be, so a
    partial date can never outrank a fuller one it does not actually beat.
    """
    parts = str(text or "").strip().split("-")
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 and parts[1] else 1
        day = int(parts[2]) if len(parts) > 2 and parts[2] else 1
    except (TypeError, ValueError):
        return None
    if year < 1 or not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    return (year, month, day)


def is_new(first_release_date, added_at):
    """Is this release group NEW for a watch created at *added_at*?

    New means a first release date strictly AFTER the watch's creation date.
    That comparison is the anti-dump rule: an artist's back catalogue is dated
    before the watch exists, so a fresh watch cannot queue any of it, no matter
    how the type filter is set. An undated release group is never new —
    "unknown date" treated as "new" is exactly how a watch would dump a
    discography the first time MusicBrainz lacks dates for an artist.
    """
    date = date_parts(first_release_date)
    if not date:
        return False
    try:
        made = time.localtime(float(added_at or 0))[:3]
    except (TypeError, ValueError, OSError):
        return False
    return date > tuple(made)


def type_matches(primary_type, secondary_types, wanted):
    """Whether a release group's type is one of the selected *wanted* names.

    A secondary type is a QUALIFIER and it decides the match: a live album is
    Album + Live, so ticking "live" finds it even though its primary type is
    Album — and "album" alone does NOT, because that is what the qualifiers are
    for (the shipped album+EP selection must not sweep up the live record, the
    compilation or the score). So a group matches when any of its secondary
    types is selected, or when it carries NO secondary type and its primary
    type is selected. Names compare case-insensitively (MusicBrainz publishes
    "DJ-mix", the API carries "dj-mix"), and an empty selection matches nothing
    — a watch with no type selected must not fall back to "everything".

    The rule itself lives in `mlo.release_choice.type_matches`: the release
    choice applies the SAME selection to the edition it queues (a watch for
    albums must not queue a single), and one implementation is what keeps the
    group a watch accepts and the edition it takes talking about one type.
    """
    from mlo import release_choice

    return release_choice.type_matches(primary_type, secondary_types, wanted)


def evaluate(rg, watch, owned=None, queued=None):
    """One release group judged by one watch's rules — the picker's own row.

    `allowed` is the POLICY verdict alone (veto, allow-list, type, date);
    `in_library` and `queued` are the separate holdings checks a cycle also
    applies, and `reason` is the full verdict in decision order, so a UI can
    explain every row exactly as the watcher would decide it.
    """
    owned = owned or {}
    queued = queued or set()
    rg_id = str(rg.get("id") or "").strip().lower()
    date = str(rg.get("first_release_date") or "").strip()
    row = {
        "release_group_mbid": rg_id,
        "title": rg.get("title") or "",
        "year": date[:4],
        "primary_type": rg.get("primary_type") or "",
        "secondary_types": [str(s) for s in (rg.get("secondary_types") or [])],
        "first_release_date": date,
        "in_library": rg_id in owned,
        "queued": rg_id in queued,
        "allowed": False,
        "is_new": is_new(date, watch.get("added_at")),
        "reason": "",
    }
    exclude = set(watch.get("exclude") or [])
    include = set(watch.get("include") or [])
    if rg_id in exclude:
        # The user's veto, and it outranks everything else — including a
        # backfill and including the allow-list.
        row["reason"] = "excluded by this watch"
        return row
    if include and rg_id not in include:
        row["reason"] = "not in this watch's list"
        return row
    if not type_matches(row["primary_type"], row["secondary_types"],
                        watch.get("release_types")):
        row["reason"] = ("no type selected"
                         if not watch.get("release_types")
                         else "type not in this watch's selected types")
        return row
    if watch.get("policy") != "backfill" and not row["is_new"]:
        row["reason"] = ("no release date on MusicBrainz — never treated as new"
                         if not date else
                         f"released {date} — before this watch was created")
        return row
    row["allowed"] = True
    if row["in_library"]:
        row["reason"] = "already in your library"
        return row
    if row["queued"]:
        row["reason"] = "already queued"
        return row
    row["reason"] = ("new since this watch was created" if watch.get("policy") != "backfill"
                     else "not in your library (backfill)")
    return row


# --------------------------------------------------------------------------- #
# Holdings and the queue — what already exists
# --------------------------------------------------------------------------- #
def owned_mbids(cfg=None):
    """{release/release-group mbid: album path} the library already holds.

    ``wishes.owned_mbids`` is the library's own answer (an album's MBID tags),
    reused rather than re-scanned — and it deliberately ignores framework
    albums, so a pending folder does not read as "already owned".
    """
    from server import wishes
    try:
        return wishes.owned_mbids(cfg)
    except Exception:
        return {}


def handled_release_groups():
    """Release groups a watch has ALREADY acted on (queued or notified).

    The watch's own bookkeeping, and the second half of the duplicate rule: a
    release group is never queued twice by the watch even if its framework
    album was removed afterwards — otherwise cancelling an album the watcher
    added would be undone on the next cycle.
    """
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT release_group_id FROM artist_watch_release WHERE status IN "
                f"({','.join('?' * len(_HANDLED))})", _HANDLED).fetchall()
    except Exception:
        return set()
    return {str(r["release_group_id"]).lower() for r in rows
            if str(r["release_group_id"] or "").strip()}


def queued_release_groups():
    """Release-group/release MBIDs the wish queue already has, plus this
    watcher's own record of what it has already acted on.

    The arbiter for "never queue the same release twice": an open wish carries
    the release (or, when saved from a group page, the release group) it is
    looking for, and a framework album carries its release GROUP id in its
    marker. That covers a release this watch queued earlier, one the user
    queued by hand, and one whose import is running right now — in every case
    the wish row or the marker is there while the audio is not.
    """
    from mlo.paths import load_pending
    from server import wishes

    out = set(handled_release_groups())
    try:
        rows = wishes.list_wishes()
    except Exception:
        return out
    for w in rows:
        if str(w.get("status") or "") in ("imported", "available"):
            continue  # in the library by now — owned_mbids' business
        mbid = str(w.get("release_mbid") or "").strip().lower()
        if mbid:
            out.add(mbid)
        path = str(w.get("album_path") or "")
        if path:
            info = load_pending(path) or {}
            rg = str(info.get("release_group_id") or "").strip().lower()
            if rg:
                out.add(rg)
    return out


def release_groups(artist_mbid, limit=_BROWSE_WINDOW):
    """One artist's release groups — ONE cached browse, newest first."""
    from server import integrations as intg

    page = intg.artist_release_groups(artist_mbid, limit=limit, offset=0)
    return _newest_first([g for g in (page.get("release_groups") or []) if g.get("id")])


def _newest_first(rows):
    """Newest first, undated last: with a cap of one, the winner is the most
    recent release the rules allow — which is what a watch is asking about."""
    dated = [r for r in rows if date_parts(r.get("first_release_date"))]
    undated = [r for r in rows if not date_parts(r.get("first_release_date"))]
    dated.sort(key=lambda r: (date_parts(r.get("first_release_date")),
                              str(r.get("title") or "").lower()), reverse=True)
    undated.sort(key=lambda r: str(r.get("title") or "").lower())
    return dated + undated


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
def _row(r):
    d = dict(r)
    d["enabled"] = bool(d.get("enabled"))
    d["auto_add"] = bool(d.get("auto_add"))
    for key in ("release_types", "include", "exclude"):
        d[key] = _json_list(d.get(key))
    return d


def list_watches():
    with _conn() as c:
        rows = c.execute("SELECT * FROM artist_watch ORDER BY name COLLATE NOCASE, id"
                         ).fetchall()
    return [_row(r) for r in rows]


def get_watch(wid):
    try:
        wid = int(wid)
    except (TypeError, ValueError):
        return None
    with _conn() as c:
        r = c.execute("SELECT * FROM artist_watch WHERE id=?", (wid,)).fetchone()
    return _row(r) if r else None


def get_watch_for_artist(artist_mbid):
    mbid = clean_mbid(artist_mbid)
    if not mbid:
        return None
    with _conn() as c:
        r = c.execute("SELECT * FROM artist_watch WHERE artist_mbid=?", (mbid,)).fetchone()
    return _row(r) if r else None


def add_watch(artist_mbid, name="", *, policy=None, release_types=None,
              include=None, exclude=None, max_per_cycle=None, auto_add=None,
              note="", added_at=None, cfg=None):
    """Create a watch. Queues NOTHING — the creation rule of the feature.

    A watch is a standing instruction, not a request: creating one (whatever
    its policy, and even with an allow-list) enqueues no album. The first
    evaluation happens on the watch's own cycle, where the date rule and the
    per-cycle cap apply. `added_at` is the watch EPOCH the anti-dump rule
    compares dates against; it defaults to now.
    """
    from mlo.config import load_config

    cfg = cfg or load_config()
    mbid = clean_mbid(artist_mbid)
    if not mbid:
        raise ValueError("a MusicBrainz artist ID is required")
    now = float(added_at or time.time())
    row = {
        "artist_mbid": mbid,
        "name": str(name or "").strip(),
        "added_at": now,
        "enabled": 1,
        "policy": _clean_policy(policy),
        "release_types": json.dumps(
            clean_types(release_types) if release_types is not None
            else clean_types(cfg.get("artist_watch_types") or ["album", "ep"])),
        "include": json.dumps(clean_mbids(include)),
        "exclude": json.dumps(clean_mbids(exclude)),
        "max_per_cycle": _clean_cap(max_per_cycle if max_per_cycle is not None
                                    else cfg.get("artist_watch_max_per_cycle", 1), 1),
        "auto_add": 1 if (cfg.get("artist_watch_auto_add", True)
                          if auto_add is None else auto_add) else 0,
        "note": str(note or ""),
    }
    with _lock:
        with _conn() as c:
            if c.execute("SELECT id FROM artist_watch WHERE artist_mbid=?",
                         (mbid,)).fetchone():
                raise WatchExists(f"already watching {mbid}")
            cur = c.execute(
                "INSERT INTO artist_watch (artist_mbid, name, added_at, enabled,"
                " policy, release_types, include, exclude, max_per_cycle, auto_add,"
                " note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (row["artist_mbid"], row["name"], row["added_at"], row["enabled"],
                 row["policy"], row["release_types"], row["include"], row["exclude"],
                 row["max_per_cycle"], row["auto_add"], row["note"]))
            wid = cur.lastrowid
    return get_watch(wid)


def update_watch(wid, fields):
    """Patch a watch. Unknown keys are ignored; an invalid value raises."""
    allowed = {"name", "enabled", "policy", "release_types", "include", "exclude",
               "max_per_cycle", "auto_add", "note"}
    sets, vals = [], []
    for key, value in (fields or {}).items():
        if key not in allowed:
            continue
        if key == "policy":
            sets.append("policy=?")
            vals.append(_clean_policy(value))
        elif key == "release_types":
            sets.append("release_types=?")
            vals.append(json.dumps(clean_types(value)))
        elif key in ("include", "exclude"):
            sets.append(f"{key}=?")
            vals.append(json.dumps(clean_mbids(value)))
        elif key == "max_per_cycle":
            sets.append("max_per_cycle=?")
            vals.append(_clean_cap(value, 1))
        elif key in ("enabled", "auto_add"):
            sets.append(f"{key}=?")
            vals.append(1 if value else 0)
        else:
            sets.append(f"{key}=?")
            vals.append(str(value or ""))
    if not sets:
        return get_watch(wid)
    vals.append(int(wid))
    with _lock:
        with _conn() as c:
            cur = c.execute(f"UPDATE artist_watch SET {', '.join(sets)} WHERE id=?", vals)
            if cur.rowcount == 0:
                return None
    return get_watch(wid)


def delete_watch(wid):
    try:
        wid = int(wid)
    except (TypeError, ValueError):
        return False
    with _lock:
        with _conn() as c:
            rows = c.execute("DELETE FROM artist_watch_release WHERE watch_id=?", (wid,))
            cur = c.execute("DELETE FROM artist_watch WHERE id=?", (wid,))
            rows.rowcount  # the history is the watch's own; it goes with it
            return cur.rowcount > 0


def _bump(wid, **counters):
    if not counters:
        return
    sets = [f"{k}=?" for k in counters]
    vals = list(counters.values())
    vals.append(int(wid))
    with _lock:
        with _conn() as c:
            c.execute(f"UPDATE artist_watch SET {', '.join(sets)} WHERE id=?", vals)


def _record(watch_id, rg_id, *, title="", year="", release_id="", wish_id=None,
            status="queued", reason=""):
    """Remember what the watch did with one release group — once.

    The row is the watch's own bookkeeping of what it has queued/notified, and
    re-recording the same group (the same release seen twice, which cannot be
    queued twice) updates it rather than adding a second row.
    """
    now = time.time()
    with _lock:
        with _conn() as c:
            c.execute(
                "INSERT INTO artist_watch_release (watch_id, release_group_id, title,"
                " year, release_id, wish_id, status, reason, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(watch_id, release_group_id) DO UPDATE SET"
                " title=excluded.title, year=excluded.year,"
                " release_id=excluded.release_id, wish_id=excluded.wish_id,"
                " status=excluded.status, reason=excluded.reason,"
                " updated_at=excluded.updated_at",
                (int(watch_id), clean_mbid(rg_id), str(title or ""), str(year or ""),
                 str(release_id or ""), int(wish_id) if wish_id else None,
                 status if status in STATUSES else "queued", str(reason or "")[:400],
                 now, now))


def _release_rows(watch_id):
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM artist_watch_release WHERE watch_id=? ORDER BY created_at DESC",
            (int(watch_id),)).fetchall()
    return [dict(r) for r in rows]


def _wish_statuses():
    """{wish id: status} — one read of the queue for a whole payload."""
    from server import wishes
    try:
        return {w["id"]: w["status"] for w in wishes.list_wishes()}
    except Exception:
        return {}


def _items(watch_id, wish_status):
    """The watch's own release rows as API items, split queued/imported.

    A row is only ever stored as "queued"/"notified"; whether the album has
    LANDED is the wish's own status, read live, so a watch never claims a
    release is still coming after the import filled it.
    """
    queued, imported = [], []
    for r in _release_rows(watch_id):
        status = r["status"]
        live = wish_status.get(r["wish_id"]) if r["wish_id"] else None
        if live in ("imported", "available"):
            status = "imported"
        elif live == "failed":
            status = "failed"
        item = {"release_group_mbid": r["release_group_id"], "title": r["title"],
                "year": r["year"], "release_id": r["release_id"],
                "wish_id": r["wish_id"], "status": status,
                "at": r["created_at"], "note": r["reason"]}
        (imported if status == "imported" else queued).append(item)
    return queued, imported


def watch_payload(watch, cfg=None, wish_status=None):
    """A watch as the API serves it: what it queued/imported, when it checks."""
    from mlo.config import load_config

    cfg = cfg or load_config()
    d = dict(watch)
    queued, imported = _items(d["id"], _wish_statuses() if wish_status is None
                              else wish_status)
    d["queued"] = queued
    d["imported"] = imported
    d["imported_count"] = len(imported)
    interval = int(cfg.get("artist_watch_interval_hours", 24) or 24)
    checked = float(d.get("last_checked_at") or 0)
    # A watch that has never been checked is due at the next tick, and a
    # disabled one is not scheduled at all: both read as 0, and only 0 means
    # "nothing is scheduled".
    d["next_check_at"] = (checked + interval * 3600.0) if (checked and d.get("enabled")) else 0.0
    return d


def watches_payload(cfg=None):
    """Every watch, ready to serve (one queue read for the whole list)."""
    from mlo.config import load_config

    cfg = cfg or load_config()
    wish_status = _wish_statuses()
    return [watch_payload(w, cfg, wish_status) for w in list_watches()]


# --------------------------------------------------------------------------- #
# Candidates — what the current rules would do
# --------------------------------------------------------------------------- #
def candidates(artist_mbid, watch=None, cfg=None, *, artist="", policy=None,
               release_types=None, include=None, exclude=None):
    """What the rules would do with every release group an artist has.

    One cached browse, evaluated with the SAME checks a cycle uses, so the
    picker a UI renders from this is the watcher's own verdict rather than a
    second opinion about what may be downloaded. A watch's stored rules win;
    on the not-yet-created form they come from the query (defaults otherwise).
    """
    from mlo.config import load_config
    from server import integrations as intg

    cfg = cfg or load_config()
    mbid = clean_mbid(artist_mbid)
    if not mbid:
        raise ValueError("a MusicBrainz artist ID is required")
    if watch is None:
        watch = {"added_at": time.time(), "policy": _clean_policy(policy or DEFAULT_POLICY),
                 "release_types": clean_types(release_types) if release_types is not None
                 else clean_types(cfg.get("artist_watch_types") or ["album", "ep"]),
                 "include": clean_mbids(include), "exclude": clean_mbids(exclude),
                 "max_per_cycle": _clean_cap(cfg.get("artist_watch_max_per_cycle", 1), 1)}
    rows = intg.artist_release_groups(mbid, limit=_BROWSE_WINDOW, offset=0)
    groups = _newest_first([g for g in (rows.get("release_groups") or []) if g.get("id")])
    owned = owned_mbids(cfg)
    queued = queued_release_groups()
    return {
        "artist_mbid": mbid,
        "artist": str(watch.get("name") or artist or ""),
        "watch_id": watch.get("id"),
        "items": [evaluate(g, watch, owned, queued) for g in groups],
        "sources_asked": ["musicbrainz"],
        "notes": {"policy": watch.get("policy") or DEFAULT_POLICY,
                  "types": list(watch.get("release_types") or []),
                  "include": len(watch.get("include") or []),
                  "exclude": len(watch.get("exclude") or [])},
    }


# --------------------------------------------------------------------------- #
# Queueing — the one seam to "Add to library"
# --------------------------------------------------------------------------- #
def queue_release(release, cfg, *, title="", artist="", year=""):
    """The ONE call this feature makes into "Add to library".

    `server.pending_albums.create` is the helper the route uses — folder,
    manifest, release-group placeholder cover, pending marker and the wish on
    the existing queue — and the existing wish worker searches it. Isolated
    here so the watcher has exactly one line to redirect if that entry point
    ever moves, and so a test can stub the whole "add to library" step.
    """
    from server import pending_albums, wishes_worker

    row = pending_albums.create(release, cfg, queries=None, title=title,
                                artist=artist, year=year)
    if row.get("wish_id") and row.get("created"):
        # The existing worker searches it; nothing is searched here.
        wishes_worker.trigger(row["wish_id"])
    return row


def _release_for(rg_id, cfg=None, *, types=None):
    """(release, error, pick) — the edition to queue for one release group.

    The ONE release-choice policy picks the edition (``mlo.release_choice``
    through ``integrations.group_targets``: official first, then the configured
    medium order — CD, other physical, digital — then completeness, then the
    original edition) — the same choice "Add to library" and the bulk import
    make, so a watch can never queue a pressing those would refuse.

    `types` is the watch's own release-group type filter and rides INTO the
    choice, not just the group evaluation: a watch for albums is answered with
    the album edition, and a release group whose type is not selected has no
    eligible edition at all. `pick` is the policy's own candidate row (score +
    reasons) so the cycle can report WHY this edition, not just which.
    """
    from server import integrations as intg

    rows, err = intg.group_targets(rg_id, "best", types=types)
    if err or not rows:
        return None, err or "no eligible edition", None
    try:
        release, _rid = intg.resolve_release(rows[0]["mbid"])
    except Exception as e:  # a MusicBrainz outage is a reason, not a crash
        return None, str(e), None
    if not release:
        return None, "no release matches this ID", None
    return release, None, rows[0]


def _announce(watch, item, cfg, *, album_path="", wish_id=None, link=""):
    """ONE event per release the watcher acted on — and none for a quiet cycle.

    The body is the album and its year, the title names the artist, and the
    link opens what the notification is about: the album it created, or the
    wish queue when the watch only reported the release.
    """
    from server import events

    title = str(item.get("title") or "").strip() or "A new release"
    year = str(item.get("year") or "").strip()
    try:
        events.emit("watch.new_release",
                    f"New release — {watch.get('name') or watch.get('artist_mbid')}",
                    f"{title} ({year})" if year else title,
                    {"watch_id": watch.get("id"),
                     "release_group_mbid": item.get("release_group_mbid") or "",
                     "release_id": item.get("release_id") or "",
                     "wish_id": wish_id, "album_path": album_path,
                     "edition": item.get("edition") or {},
                     "link": link or (f"/album/{album_path}" if album_path else "/soulseek")},
                    config=cfg)
    except Exception:
        pass  # a notification must never be why a cycle fails


# --------------------------------------------------------------------------- #
# One cycle
# --------------------------------------------------------------------------- #
def due(watch, cfg=None, now=None):
    """Is this watch's own interval up?"""
    from mlo.config import load_config

    cfg = cfg or load_config()
    interval = int(cfg.get("artist_watch_interval_hours", 24) or 24) * 3600
    now = time.time() if now is None else float(now)
    return (now - float(watch.get("last_checked_at") or 0)) >= interval


def run_watch(watch, cfg=None, *, force=False):
    """Evaluate ONE watch: at most `max_per_cycle` releases, then stop.

    Returns the cycle's report for that watch. `force` ignores the watch's own
    interval (a user pressing "check now"); it does NOT ignore `enabled`, the
    date rule, the type/allow/veto policy, the cap, or the
    `auto_acquisition_enabled` master switch over unattended acquisition.
    """
    from mlo.config import load_config

    cfg = cfg or load_config()
    wid = watch.get("id")
    out = {"watch_id": wid, "artist": watch.get("name") or "",
           "checked": 0, "queued": [], "notified": [], "errors": [],
           "summary": ""}
    if not watch.get("enabled"):
        out["summary"] = "disabled"
        return out
    if not import_policy.auto_acquisition_enabled(cfg):
        # `auto_acquisition_enabled` is off: which release to take and which
        # edition of it is the watcher's OWN decision — made without the user
        # asking for anything in particular — so nothing is browsed and
        # nothing is queued. `force` only ignores the interval, exactly as it
        # does not ignore `enabled` above: a manual "check now" reports the
        # switch instead of queueing past it, and the watch keeps its interval
        # (last_checked_at is untouched, so turning the switch back on checks
        # it then rather than a day later).
        out["summary"] = import_policy.AUTO_OFF_NOTE
        return out
    if not force and not due(watch, cfg):
        out["summary"] = "not due"
        return out

    try:
        rows = release_groups(watch["artist_mbid"])
    except Exception as e:
        # MusicBrainz being busy is this watch's business, not the server's:
        # record it and try again next cycle.
        out["summary"] = f"MusicBrainz did not answer: {e}"
        out["errors"].append(str(e))
        _bump(wid, last_checked_at=time.time(), checked_count=int(watch.get("checked_count") or 0) + 1,
              last_error=str(e)[:400], last_result=out["summary"][:200])
        return out

    owned = owned_mbids(cfg)
    # The queue is read AFTER the browse: a cycle that took seconds must not
    # queue something another cycle (or a manual add) put in the queue while
    # it was reading MusicBrainz.
    queued = queued_release_groups()
    judged = [evaluate(g, watch, owned, queued) for g in _newest_first(rows)]
    winners = [d for d in judged if d["allowed"] and not d["in_library"]
               and not d["queued"]]
    cap = _clean_cap(watch.get("max_per_cycle"), 1)
    winners = winners[:cap]  # the anti-dump cap, whatever the policy

    for cand in winners:
        item = {"release_group_mbid": cand["release_group_mbid"],
                "title": cand["title"], "year": cand["year"],
                "release_id": "", "wish_id": None}
        if not watch.get("auto_add"):
            # Notify-only: the user keeps the choice of edition, so nothing is
            # resolved, nothing is created, and the row records the report.
            _record(wid, cand["release_group_mbid"], title=cand["title"],
                    year=cand["year"], status="notified", reason=cand["reason"])
            _announce(watch, item, cfg, link="/soulseek")
            out["notified"].append(item)
            continue
        release, err, pick = _release_for(cand["release_group_mbid"], cfg,
                                          types=watch.get("release_types"))
        if err or not release:
            # Not handled: a transient MusicBrainz failure or an edition the
            # policy refused is retried next cycle rather than being written
            # off as done.
            out["errors"].append(f"{cand['title']}: {err}")
            _record(wid, cand["release_group_mbid"], title=cand["title"],
                    year=cand["year"], status="retry",
                    reason=f"could not queue: {err}")
            continue
        try:
            row = queue_release(release, cfg, title=cand["title"],
                               artist=watch.get("name") or "",
                               year=cand["year"])
        except Exception as e:
            out["errors"].append(f"{cand['title']}: {e}")
            _record(wid, cand["release_group_mbid"], title=cand["title"],
                    year=cand["year"], status="retry",
                    reason=f"could not queue: {e}")
            continue
        item["release_id"] = row.get("release_id") or ""
        item["wish_id"] = row.get("wish_id")
        item["album_path"] = row.get("album_path") or ""
        if pick:
            # WHICH edition was taken, and the policy's own words for it — the
            # watch made this choice without the user, so the row and the
            # notification both have to be able to explain it.
            item["edition"] = {"release_mbid": pick.get("mbid") or "",
                               "title": pick.get("title") or "",
                               "score": pick.get("score"),
                               "reasons": list(pick.get("reasons") or [])}
        if row.get("already_in_library"):
            # The audio is already there: nothing was created, and the group
            # must never be looked at again.
            out["errors"].append(f"{cand['title']}: already in your library")
            _record(wid, cand["release_group_mbid"], title=cand["title"],
                    year=cand["year"], release_id=item["release_id"],
                    wish_id=item["wish_id"], status="imported",
                    reason="already in your library")
            continue
        _record(wid, cand["release_group_mbid"], title=cand["title"],
                year=cand["year"], release_id=item["release_id"],
                wish_id=item["wish_id"], status="queued", reason=cand["reason"])
        _announce(watch, item, cfg, album_path=item["album_path"],
                  wish_id=item["wish_id"])
        out["queued"].append(item)

    blocked = len([d for d in judged if not d["allowed"]
                   or d["in_library"] or d["queued"]])
    out["checked"] = len(judged)
    summary = (f"queued {len(out['queued'])}, notified {len(out['notified'])}"
               f" ({blocked} not eligible)")
    if not winners:
        summary = f"nothing new ({blocked} release groups checked)"
    out["summary"] = summary
    newest = judged[0]["release_group_mbid"] if judged else ""
    _bump(wid, last_checked_at=time.time(),
          checked_count=int(watch.get("checked_count") or 0) + 1,
          queued_count=int(watch.get("queued_count") or 0) + len(out["queued"]),
          notified_count=int(watch.get("notified_count") or 0) + len(out["notified"]),
          last_seen_release_group=newest,
          last_result=summary[:200],
          last_error=(out["errors"][0][:400] if out["errors"] else ""))
    return out


def run_cycle(watch_id=None, cfg=None, *, force=False):
    """One pass over the watches. `watch_id` targets a single watch.

    Without an id, every ENABLED watch whose own interval is up is evaluated;
    each watch is capped by its own `max_per_cycle`, so the cycle's total is
    the sum of those caps and never a discography.
    """
    from mlo.config import load_config

    cfg = cfg or load_config()
    if watch_id is not None:
        watch = get_watch(watch_id)
        if not watch:
            return {"ok": False, "error": "watch not found", "checked": 0,
                    "queued": [], "notified": [], "errors": []}
        watches = [watch]
    else:
        watches = [w for w in list_watches() if w.get("enabled") and due(w, cfg)]

    checked = 0
    queued, notified, errors, reports = [], [], [], []
    for watch in watches:
        try:
            out = run_watch(watch, cfg, force=force or watch_id is not None)
        except Exception as e:  # one bad watch must not stop the pass
            errors.append(f"{watch.get('name') or watch.get('artist_mbid')}: {e}")
            continue
        reports.append(out)
        checked += out["checked"]
        queued.extend(out["queued"])
        notified.extend(out["notified"])
        errors.extend(out["errors"])
    summary = ("; ".join(r["summary"] for r in reports) if reports
               else "no watch is due")
    return {"ok": True, "watches": len(watches), "checked": checked,
            "queued": queued, "notified": notified, "errors": errors,
            "summary": summary}
