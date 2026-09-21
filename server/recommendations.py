"""Home-page payload: library highlights and the open Soulseek wishlist.

Everything is built from the library itself — stats, recent additions, best
grades, favorites, a random rediscovery shelf, most-collected artists and
albums failing their checks — plus the wishes the background worker is
hunting. Best-effort and TTL-cached: a failing sub-source degrades to a
missing shelf rather than failing the Home page.
"""
import os
import random
import threading
import time

_lock = threading.Lock()
_cache = {"t": 0.0, "key": None, "data": None}
_TTL = 900.0


def _artist_of(alb, fallback=""):
    meta = alb.get("meta") or {}
    return (str(meta.get("ALBUMARTIST") or "").strip()
            or str(alb.get("album_artist") or "").strip()
            or str(meta.get("ARTIST") or "").strip() or fallback)


def _owned_row(alb, fallback_artist="", reason="", owned=True):
    """One Home shelf row: the LIBRARY's own album row, plus the shelf's reason.

    The shelves draw the shared album card (web/src/components/AlbumCard) — the
    same card the Library grid draws — so the row has to BE the library's row:
    its tracks (the format chip and the play button), `meta` (release country,
    original year, dynamic range), media, grade and audit all come off it, as
    does the framework album's marker. A reduced row would be a second album
    shape for one card to understand, and a Home card that showed less than the
    same album shows in the Library.
    """
    meta = alb.get("meta") or {}
    row = dict(alb)
    row["reason"] = reason
    row["owned"] = owned
    # The card's own artist field (the library grid builds the same one when it
    # flattens the payload): ALBUMARTIST → album_artist → ARTIST.
    row["artist"] = _artist_of(alb, fallback_artist)
    row["mbid"] = (str(meta.get("MUSICBRAINZ_ALBUMID") or "").strip()
                   or str(meta.get("MUSICBRAINZ_RELEASEGROUPID") or "").strip() or None)
    row["mb_kind"] = "rg"
    return row


def _added_at(alb):
    """A row's addition time — the folder's own mtime, the one fact that says
    "just added" for a framework album with no file to read it from."""
    try:
        return os.path.getmtime(alb.get("path") or "")
    except OSError:
        return 0.0


def _recent(albums, limit):
    ordered = sorted(albums, key=_added_at, reverse=True)
    return [_owned_row(a, reason="Recently added") for a in ordered[:limit]]


def _pending(albums, limit):
    """The albums that are added but not downloaded yet, newest first.

    This is the one place a user can see EVERYTHING still waiting: the shelf
    above lists them where they would otherwise be (recent, their artist's
    shelf), but a skeleton that only rides along with other shelves is a
    skeleton a reader has to hunt for.
    """
    waiting = [a for a in albums if a.get("pending")]
    waiting.sort(key=_added_at, reverse=True)
    return [_owned_row(a, reason="Waiting for its audio") for a in waiting[:limit]]


def _top_rated(albums, limit):
    rated = [a for a in albums if a.get("grade_pct") is not None and (a.get("total_checks") or 0) >= 5]
    rated.sort(key=lambda a: (-(a.get("grade_pct") or 0), not a.get("pass")))
    return [_owned_row(a, reason="Best graded") for a in rated[:limit]]


def _favorites(lib, limit, user=""):
    """The signed-in user's favourite ALBUMS.

    Scoped, like every other reader of that table: the sidebar's Favourites
    page shows this user's albums, so a Home shelf built from the default
    scope would be a different person's list beside it — and a cross-user read
    the per-user work exists to prevent.
    """
    try:
        from server import playlists as pl
        favs = (pl.list_favorites(user) or {}).get("albums", [])
    except Exception:
        favs = []
    if not favs:
        return []
    by_path = {os.path.normcase(os.path.normpath(a.get("path") or "")): a
               for a in (alb for ar in lib.get("artists", []) for alb in ar.get("albums", []))}
    out = []
    for p in favs[:limit]:
        alb = by_path.get(os.path.normcase(os.path.normpath(str(p))))
        if alb:
            out.append(_owned_row(alb, reason="Favorite"))
        else:
            name = os.path.basename(str(p).replace("\\", "/"))
            # The library no longer holds this path (the folder was removed or
            # moved): there is no album row to read tags, tracks or a grade
            # from. The row keeps the path the user favourited — and `owned`
            # is what tells the card not to draw a grade, a play button or a
            # link the library cannot answer.
            out.append({
                "path": str(p).replace("\\", "/"),
                "owned": False,
                "artist": "",
                "reason": "Favorite",
                "mbid": None,
                "mb_kind": "rg",
                "tracks": [],
                "cover_file": None,
                "meta": {"ALBUM": name},
            })
    return out


def _top_artists(artists, limit):
    """Most-collected artists, with a representative cover for the card."""
    rows = []
    for ar in artists:
        name = str(ar.get("display_name") or ar.get("name") or "").strip()
        albs = ar.get("albums") or []
        if not name or not albs:
            continue
        agg = ar.get("aggregate") or {}
        first = sorted(albs, key=lambda a: str(a.get("path") or "").lower())[0]
        rows.append({
            "path": str(ar.get("path") or "").replace("\\", "/"),
            "artist": name,
            "album_count": len(albs),
            "track_count": agg.get("track_count") or 0,
            "grade_pct": agg.get("grade_pct"),
            "cover_path": first.get("path") or "",
            "cover": first.get("cover_file"),
        })
    rows.sort(key=lambda r: (-r["album_count"], r["artist"].lower()))
    return rows[:limit]


_WISH_REASON = {"wanted": "Wishlist", "searching": "Searching Soulseek",
                "failed": "Search failed", "available": "Available now"}


def _wanted(limit, skip_paths=()):
    """Open Soulseek wishes — releases the background worker is hunting.

    A wish whose album is ALREADY in the library as a framework album is left
    out: that release is a library card of its own now (`_pending`, and the
    shelf it would appear on), and listing its wish here as well would draw the
    same album twice on one page — once with a link and once without.
    """
    try:
        from server import wishes
        items = wishes.list_wishes() or []
    except Exception:
        return []
    out = []
    for w in items:
        if str(w.get("status") or "") == "imported":
            continue
        path = str(w.get("album_path") or "")
        if path and os.path.normcase(os.path.normpath(path)) in skip_paths:
            continue
        out.append({
            # A wish is NOT a library album: no folder, no tags, nothing
            # graded. The row carries the identity the card draws — title,
            # artist and year in the album-tag shape the card already reads —
            # and nothing that reads as a library fact: no tracks to play, no
            # pass / audit for a status dot. `owned` false is what the card
            # keys that on.
            "path": "",
            "owned": False,
            "reason": _WISH_REASON.get(str(w.get("status") or ""), "Wishlist"),
            "mbid": str(w.get("release_mbid") or "").strip() or None,
            "mb_kind": "release",
            "tracks": [],
            "cover_file": None,
            "meta": {
                "ALBUM": str(w.get("title") or "").strip() or "Untitled release",
                "ARTIST": str(w.get("artist") or "").strip(),
                "DATE": str(w.get("year") or "")[:4] or None,
            },
        })
        if len(out) >= limit:
            break
    return out


def _needs_attention(albums, limit):
    """Owned albums that fail at least one check — lowest grade first."""
    bad = [a for a in albums
           if (a.get("total_checks") or 0) > 0 and not a.get("pass")]
    bad.sort(key=lambda a: (a.get("grade_pct") if a.get("grade_pct") is not None else 0.0,
                            -(a.get("total_checks") or 0)))
    return [_owned_row(a, reason="Needs attention") for a in bad[:limit]]


def build_home(cfg, user=""):
    """Full Home payload for the given config and user (TTL-cached).

    The cache key carries the user: the payload holds that person's favourites
    and their playlist count, so a shared entry would serve the first caller's
    rows to everyone else for the TTL.
    """
    from server import library as lib_mod

    user = str(user or "")
    folder = str(cfg.get("music_folder") or "")
    recent_count = int(cfg.get("home_recent_count", 12) or 12)
    cache_key = (folder, recent_count, user)
    now = time.time()
    with _lock:
        if _cache["data"] and _cache["key"] == cache_key and now - _cache["t"] < _TTL:
            return _cache["data"]

    lib = lib_mod.build_library(cfg)
    artists = lib.get("artists", [])
    albums = [alb for ar in artists for alb in ar.get("albums", [])]

    stats = {
        "artists": len(artists),
        "albums": len(albums),
        "tracks": sum(a.get("track_count") or len(a.get("tracks") or []) for a in albums),
    }
    try:
        from server import playlists as pl
        stats["playlists"] = len(pl.list_playlists(user) or [])
    except Exception:
        stats["playlists"] = 0
    passes = sum((a.get("pass_count") or 0) for a in albums)
    checks = sum((a.get("total_checks") or 0) for a in albums)
    stats["grade_pct"] = round(100.0 * passes / checks, 1) if checks else None

    recent = _recent(albums, recent_count)
    top = _top_rated(albums, max(4, recent_count // 2))
    favorites = _favorites(lib, max(4, recent_count // 2), user)
    pending = _pending(albums, max(4, recent_count))

    # Discover: a random slice of the library that isn't already featured.
    # "Rediscover" means "you own it and forgot it", so the albums whose audio
    # has not arrived are left out — they have a shelf of their own below.
    featured = {os.path.normcase(os.path.normpath(r["path"])) for r in recent + top}
    pool = [a for a in albums
            if not a.get("pending")
            and os.path.normcase(os.path.normpath(a.get("path") or "")) not in featured]
    random.shuffle(pool)
    discover = [_owned_row(a, reason="Rediscover") for a in pool[:recent_count]]

    data = {
        "stats": stats,
        "recent": recent,
        "top_rated": top,
        "favorites": favorites,
        "discover": discover,
        # Every album still waiting for its audio, in one place.
        "pending": pending,
        "top_artists": _top_artists(artists, 6),
        "wanted": _wanted(8, {os.path.normcase(os.path.normpath(a.get("path") or ""))
                              for a in albums if a.get("pending")}),
        "needs_attention": _needs_attention(albums, max(4, recent_count // 2)),
    }
    with _lock:
        _cache.update({"t": now, "key": cache_key, "data": data})
    return data


def invalidate():
    with _lock:
        _cache["t"] = 0.0
        _cache["data"] = None
    # The "more like this" index reads the same library payload the shelves
    # below are built from, so one invalidation covers both.
    try:
        from server import recommend
        recommend.invalidate()
    except Exception:
        pass
