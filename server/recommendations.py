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
    meta = alb.get("meta") or {}
    return {
        "path": alb.get("path") or "",
        "album": str(meta.get("ALBUM") or "").strip(),
        "artist": _artist_of(alb, fallback_artist),
        "year": str(meta.get("DATE") or "")[:4] or None,
        "cover": alb.get("cover_file"),
        "grade_pct": alb.get("grade_pct"),
        "mbid": (str(meta.get("MUSICBRAINZ_ALBUMID") or "").strip()
                 or str(meta.get("MUSICBRAINZ_RELEASEGROUPID") or "").strip() or None),
        "mb_kind": "rg",
        "reason": reason,
        "owned": owned,
    }


def _recent(albums, limit):
    def mtime(a):
        try:
            return os.path.getmtime(a.get("path") or "")
        except OSError:
            return 0.0
    ordered = sorted(albums, key=mtime, reverse=True)
    return [_owned_row(a, reason="Recently added") for a in ordered[:limit]]


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
            out.append({"path": str(p).replace("\\", "/"), "album": name, "artist": "",
                        "year": None, "cover": None, "grade_pct": None,
                        "mbid": None, "mb_kind": "rg", "reason": "Favorite", "owned": True})
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


def _wanted(limit):
    """Open Soulseek wishes — releases the background worker is hunting."""
    try:
        from server import wishes
        items = wishes.list_wishes() or []
    except Exception:
        return []
    out = []
    for w in items:
        if str(w.get("status") or "") == "imported":
            continue
        out.append({
            "path": "",
            "album": str(w.get("title") or "").strip() or "Untitled release",
            "artist": str(w.get("artist") or "").strip(),
            "year": str(w.get("year") or "")[:4] or None,
            "cover": None,
            "grade_pct": None,
            "mbid": str(w.get("release_mbid") or "").strip() or None,
            "mb_kind": "release",
            "reason": _WISH_REASON.get(str(w.get("status") or ""), "Wishlist"),
            "owned": False,
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

    # Discover: a random slice of the library that isn't already featured.
    featured = {os.path.normcase(os.path.normpath(r["path"])) for r in recent + top}
    pool = [a for a in albums
            if os.path.normcase(os.path.normpath(a.get("path") or "")) not in featured]
    random.shuffle(pool)
    discover = [_owned_row(a, reason="Rediscover") for a in pool[:recent_count]]

    data = {
        "stats": stats,
        "recent": recent,
        "top_rated": top,
        "favorites": favorites,
        "discover": discover,
        "top_artists": _top_artists(artists, 6),
        "wanted": _wanted(8),
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
