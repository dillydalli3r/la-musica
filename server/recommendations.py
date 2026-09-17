"""Home-page payload: album recommendations, recent additions and highlights.

Recommendations are seeded from the library's own taste (most-collected
artists + most-tagged genres) and resolved through the discovery chain
(popularity-ranked Deezer/ListenBrainz results, MusicBrainz release groups)
— which chain drives the shelf is `home_rec_source`. Everything is
best-effort and TTL-cached: a provider hiccup degrades to the MusicBrainz
path and then to library-only highlights rather than failing the Home page.
"""
import os
import random
import threading
import time

from server import discovery

_lock = threading.Lock()
_cache = {"t": 0.0, "key": None, "data": None}
_TTL = 900.0
_MB_BUDGET = 6  # max MusicBrainz searches per build (1 req/s etiquette)


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


def _rec_row(row, reason):
    return {
        "path": "",
        "album": str(row.get("title") or "").strip(),
        "artist": str(row.get("artist") or "").strip(),
        "year": str(row.get("first_release_date") or "")[:4] or None,
        "cover": None,
        "grade_pct": None,
        "mbid": row.get("id"),
        "mb_kind": "rg",
        "reason": reason,
        "owned": False,
    }


def _seeds(artist_count, genre_count, genre_artist):
    """Taste tallies → discovery seeds `{artist, weight, reason}`.

    The three deepest artist collections lead; the two biggest genres follow,
    each represented by the artist that carries that genre hardest in this
    library. An artist already seeded is skipped, so the list stays signals
    rather than repeats.
    """
    seeds = []
    for name, n in sorted(artist_count.items(), key=lambda kv: (-kv[1], kv[0]))[:3]:
        seeds.append({"artist": name, "weight": n,
                      "reason": f"Because you collect {name}"})
    for g, _n in sorted(genre_count.items(), key=lambda kv: (-kv[1], kv[0]))[:2]:
        best = max((genre_artist.get(g) or {}).items(), key=lambda kv: (kv[1], kv[0]),
                   default=None)
        if not best or any(s["artist"] == best[0] for s in seeds):
            continue
        seeds.append({"artist": best[0], "weight": best[1],
                      "reason": f"Because you like {g.title()}"})
    return seeds


def _owned_index(albums):
    """`"artist|title"` → library album, for spotting rows we already own."""
    index = {}
    for alb in albums:
        meta = alb.get("meta") or {}
        key = f"{discovery.norm(_artist_of(alb))}|{discovery.norm(meta.get('ALBUM'))}"
        if key != "|":
            index.setdefault(key, alb)
    return index


def _home_row(row, owned, reason=None):
    """A discovery row as a Home shelf card (see `HomeAlbum` in types.ts).

    A row the library already owns keeps its `path` + `owned=True` so the UI
    can link to the album page instead of dead-ending on MusicBrainz.
    """
    artist = str(row.get("artist") or "").strip()
    title = str(row.get("title") or "").strip()
    alb = owned.get(f"{discovery.norm(artist)}|{discovery.norm(title)}")
    path = str((alb or {}).get("path") or row.get("owned_path") or "")
    year = str(row.get("year") or row.get("release_date") or "")[:4]
    return {
        "path": path,
        "album": title,
        "artist": artist,
        "year": year or None,
        "cover": (alb or {}).get("cover_file"),
        "grade_pct": (alb or {}).get("grade_pct"),
        "cover_url": row.get("cover"),
        "reason": reason or str(row.get("reason") or ""),
        "mbid": row.get("mbid"),
        "mb_kind": "rg",
        "owned": bool(path),
        "source": row.get("source"),
        "popularity_label": row.get("popularity_label"),
        "popularity": row.get("popularity"),
        "deezer_id": row.get("deezer_id"),
    }


def _recommended(cfg, albums, artist_count, genre_count, want,
                 owned=None, owned_rg=None, genre_artist=None):
    """Recommendation shelf for `cfg["home_rec_source"]`.

    Returns `(rows, source_actually_used)`. Discovery providers are tried
    first; anything that comes back empty or raises falls back to the
    MusicBrainz search path, so the shelf degrades rather than vanishing.
    """
    src = str(cfg.get("home_rec_source") or "discovery").strip().lower()
    if src not in ("discovery", "listenbrainz"):
        src = "musicbrainz"
    owned = owned or {}
    if src != "musicbrainz":
        try:
            if src == "listenbrainz":
                rows = discovery.popular_albums(limit=want, cfg=cfg)
            else:
                rows = discovery.recommend_albums(
                    _seeds(artist_count, genre_count, genre_artist or {}),
                    limit=want, cfg=cfg, exclude=set(owned))
        except Exception:
            rows = []
        # Owned albums are dropped: the shelf is for music not in the library.
        rows = [_home_row(r, owned) for r in rows]
        rows = [r for r in rows if r["album"] and r["artist"] and not r["owned"]]
        if rows:
            return rows, src
    try:
        return _mb_recs(albums, owned_rg or set(), artist_count, genre_count, want), \
            "musicbrainz"
    except Exception:
        return [], src


def _popular(cfg, owned, limit):
    """Sitewide-listening chart as Home cards (empty when discovery is off)."""
    if not (cfg.get("home_recommendations", True)
            and cfg.get("discovery_enabled", True)):
        return []
    try:
        rows = discovery.popular_albums(limit=limit, cfg=cfg)
    except Exception:
        return []
    out = [_home_row(r, owned) for r in rows]
    return [r for r in out if r["album"] and r["artist"]]


def _collect(lib):
    """Flatten the library into albums + owned-id sets + genre/artist tallies.

    `genre_artist` is the per-genre collected-artist tally (genre → artist →
    track count), which lets a genre seed point at the artist that best
    represents it in this library.
    """
    albums, owned_rg, artist_count, genre_count, genre_artist = [], set(), {}, {}, {}
    for ar in lib.get("artists", []):
        for alb in ar.get("albums", []):
            albums.append(alb)
            meta = alb.get("meta") or {}
            rg = str(meta.get("MUSICBRAINZ_RELEASEGROUPID") or "").strip().lower()
            if rg:
                owned_rg.add(rg)
            name = ar.get("display_name") or ar.get("name") or _artist_of(alb)
            if name:
                artist_count[name] = artist_count.get(name, 0) + 1
            for tr in alb.get("tracks", []):
                g = str((tr.get("tags") or {}).get("GENRE") or "").strip()
                for part in g.replace(";", ",").split(","):
                    part = part.strip().lower()
                    if not part:
                        continue
                    genre_count[part] = genre_count.get(part, 0) + 1
                    if name:
                        bucket = genre_artist.setdefault(part, {})
                        bucket[name] = bucket.get(name, 0) + 1
    return albums, owned_rg, artist_count, genre_count, genre_artist


def _mb_recs(albums, owned_rg, artist_count, genre_count, want):
    """MusicBrainz release-group suggestions the library doesn't own."""
    from server import integrations as intg

    seeds = []
    for name, _n in sorted(artist_count.items(), key=lambda kv: -kv[1])[:3]:
        seeds.append((f'artist:"{name}"', f"More from {name}"))
    for g, _n in sorted(genre_count.items(), key=lambda kv: -kv[1])[:3]:
        if g and g not in ("", "unknown"):
            seeds.append((f'tag:"{g}"', f"Because you like {g.title()}"))

    recs, used = [], 0
    for query, reason in seeds:
        if used >= _MB_BUDGET or len(recs) >= want:
            break
        used += 1
        try:
            data = intg.search_mb("release-group", query, limit=25)
        except Exception:
            continue
        for row in data.get("rows", []):
            if len(recs) >= want:
                break
            rg = str(row.get("id") or "").lower()
            if not rg or rg in owned_rg:
                continue
            rtype = str(row.get("primary_type") or "").lower()
            if rtype and rtype not in ("album", "ep", "single"):
                continue
            rec = _rec_row(row, reason)
            if not rec["album"] or not rec["artist"]:
                continue
            if any(x["mbid"] == rec["mbid"] for x in recs):
                continue
            recs.append(rec)
    return recs


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


def _favorites(lib, limit):
    try:
        from server import playlists as pl
        favs = (pl.list_favorites() or {}).get("albums", [])
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


def build_home(cfg):
    """Full Home payload for the given config (TTL-cached)."""
    from server import library as lib_mod

    folder = str(cfg.get("music_folder") or "")
    rec_count = int(cfg.get("home_rec_count", 18) or 18)
    recent_count = int(cfg.get("home_recent_count", 12) or 12)
    popular_count = int(cfg.get("home_popular_count", 12) or 12)
    rec_source = str(cfg.get("home_rec_source") or "discovery")
    discovery_on = bool(cfg.get("discovery_enabled", True))
    cache_key = (folder, rec_count, recent_count, popular_count, rec_source,
                 discovery_on, bool(cfg.get("home_recommendations", True)))
    now = time.time()
    with _lock:
        if _cache["data"] and _cache["key"] == cache_key and now - _cache["t"] < _TTL:
            return _cache["data"]

    lib = lib_mod.build_library(cfg)
    artists = lib.get("artists", [])
    albums, owned_rg, artist_count, genre_count, genre_artist = _collect(lib)

    stats = {
        "artists": len(artists),
        "albums": len(albums),
        "tracks": sum(a.get("track_count") or len(a.get("tracks") or []) for a in albums),
    }
    try:
        from server import playlists as pl
        stats["playlists"] = len(pl.list_playlists() or [])
    except Exception:
        stats["playlists"] = 0
    passes = sum((a.get("pass_count") or 0) for a in albums)
    checks = sum((a.get("total_checks") or 0) for a in albums)
    stats["grade_pct"] = round(100.0 * passes / checks, 1) if checks else None

    recent = _recent(albums, recent_count)
    top = _top_rated(albums, max(4, recent_count // 2))
    favorites = _favorites(lib, max(4, recent_count // 2))

    owned = _owned_index(albums)
    recommended, used_source = [], rec_source
    popular = []
    if cfg.get("home_recommendations", True):
        recommended, used_source = _recommended(
            cfg, albums, artist_count, genre_count, rec_count,
            owned=owned, owned_rg=owned_rg, genre_artist=genre_artist)
        popular = _popular(cfg, owned, popular_count)

    # Discover: a random slice of the library that isn't already featured.
    featured = {os.path.normcase(os.path.normpath(r["path"])) for r in recent + top}
    pool = [a for a in albums
            if os.path.normcase(os.path.normpath(a.get("path") or "")) not in featured]
    random.shuffle(pool)
    discover = [_owned_row(a, reason="Rediscover") for a in pool[:recent_count]]

    data = {
        "stats": stats,
        "recent": recent,
        "recommended": recommended,
        "rec_source": used_source,
        "popular": popular,
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
