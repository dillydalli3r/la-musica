"""Home-page payload: library highlights and the open Soulseek wishlist.

Everything is built from the library itself — stats, recent additions, best
grades, the user's own rated releases, favorites, a random rediscovery shelf,
most-collected artists and albums failing their checks — plus the wishes the
background worker is hunting, and the ratings store for the one shelf that is
the caller's own verdict rather than the library's. Best-effort and
TTL-cached: a failing sub-source degrades to a missing shelf rather than
failing the Home page.
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
    """Most-collected artists, with a representative cover for the card.

    `artist` is the library row's `display_name` (server.library builds it:
    the tag-derived name, or the folder name without its MusicBrainz id) rather
    than the folder's own basename, which is how Home came to draw
    "Radiohead [a74b1b7f-71a5-4011-9441-d0b5e4122711]".

    `has_image` answers for `GET /api/artist/image`, which 404s on a folder
    holding no artist picture: the shelf draws that picture first and the
    cover behind it, and asking for the URL anyway paints the broken-image
    glyph before the fallback replaces it. Read from the directory itself for
    the handful of rows this returns — never a walk, and never a provider.
    """
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
    rows = rows[:limit]
    # Imported here, at the point of use: only this shelf asks the filesystem
    # anything, and an artistdata that cannot answer must not cost the page.
    from mlo import artistdata
    for r in rows:
        try:
            r["has_image"] = artistdata.has_image(r["path"])
        except Exception:
            r["has_image"] = False
    return rows


def _rated(albums, user, limit):
    """The user's own rated RELEASES, best first.

    The verdict lives in the ratings store, and the store's own rule is what
    keeps this shelf cheap: a rating is refused for any path no page draws
    (`server.ratings.target_missing`), so every stored row has a library album
    behind it and both halves of the join are already in hand — the store's map
    and the album rows. One table read, no walk, no provider.

    UNRATED releases are deliberately not a shelf: "no star yet" is not a
    reason a row is here, the set has no order to give it (it is most of the
    library), and the Rediscover shelf already draws a random slice of owned
    albums for exactly that "you own it and have not looked at it" case.

    `rating` rides on the row in HALF-STARS — the store's own unit, the one
    `GET /api/ratings?scope=album` answers in and web/src/lib/ratings.ts turns
    into the stars a reader sees — so the shelf's order and the stars on its
    cards come from the same number.
    """
    if not albums:
        return []  # nothing to rate: the store read below is not even worth it
    try:
        from server import ratings as store
        got = store.map_for(user=user, scope="album")
    except Exception:
        return []  # an unreadable store loses the shelf, never the whole page
    if not got:
        return []
    # Keyed the way `_favorites` reads the same payload: the library spells a
    # path with forward slashes, a store row is whatever its writer stored.
    by_path = {os.path.normcase(os.path.normpath(p)): int(v)
               for p, v in got.items()}
    rated = []
    for alb in albums:
        half = by_path.get(os.path.normcase(os.path.normpath(str(alb.get("path") or ""))))
        if not half:
            continue  # an unrated album, or a row the caller has no verdict on
        row = _owned_row(alb)
        row["rating"] = half
        rated.append(row)
    # Highest first, then by artist and title so two albums of one value never
    # swap places between builds (the shelf is cached, not re-sorted per view).
    rated.sort(key=lambda r: (-r["rating"], r["artist"].lower(),
                              str((r.get("meta") or {}).get("ALBUM") or "").lower()))
    return rated[:limit]


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


# The most findings the banner names; everything past it is the `more` count a
# reader clicks through to the Library's own Failing filter.
_GRADE_WARNING_MAX = 12
# The grader records an album-wide failure against one of these INSTEAD of a
# file name (mlo.grader's add_issue): "album" for a check that belongs to the
# folder (its cover, its .log), "album-wide" for one that spans every track in
# it (MEDIA, the album tags).
_ALBUM_WHERE = ("album", "album-wide")


def grade_warning(lib):
    """Whether the library passes its grading checks, and — when it does not —
    what fails, specifically: the object both pages' warning strip is drawn
    from (`GET /api/grades/summary`, and `grade_warning` in the Home payload).

    Read off the library payload the pages already hold, and totalled the way
    the Home header totals it: every album's `pass_count` over its
    `total_checks`. `ok` is therefore the grader's own per-album rule (failed
    checks == 0, server.library.build_album) applied to the whole library, so
    the strip can never contradict the percentage printed beside it.

    Two albums are not a finding, and both would be the loudest row on the
    page:
    - a PENDING framework album (the release the user added whose audio has
      not arrived): nothing was graded, because there was nothing to grade,
      and its row's failed check is the empty-folder placeholder — listing it
      would report a wish as a broken album;
    - an album with NO checks (`total_checks` 0): it PASSES by the rule above
      (0 == 0), which is how an album whose checks are all switched off stops
      disagreeing with the Grade script.

    The owner's rule for what a finding is: ONE failing track in an album is
    shown AS THAT TRACK — the album is only the frame around it, and the file
    is the thing to open — while two or more are shown AS THE ALBUM, which
    carries how many tracks fail and the union of their codes, because a dozen
    rows of one album say less than its name. A failure the grader recorded
    against the album itself (no file to name) always makes an album row and
    carries the grader's own sentence as `reason`.
    """
    albums = [alb for ar in (lib.get("artists") or [])
              for alb in (ar.get("albums") or [])]
    pass_count = sum(int(a.get("pass_count") or 0) for a in albums)
    total_checks = sum(int(a.get("total_checks") or 0) for a in albums)
    items = []
    tracks_failing = 0
    for alb in albums:
        # See the docstring: neither of these was graded, so neither is wrong.
        if alb.get("pending") or not (alb.get("total_checks") or 0) or alb.get("pass"):
            continue
        bad = [tr for tr in (alb.get("tracks") or []) if tr.get("issues")]
        issues = alb.get("issues") or {}
        sentences = [s for s, where in issues.items()
                     if any(w in _ALBUM_WHERE for w in (where or ()))]
        if not bad and not sentences:
            # A folder failure with no file against it and no album-wide
            # sentence either: the grader keys those by the folder itself (an
            # album folder that holds no audio), and its sentence is the only
            # thing there is to say.
            sentences = list(issues)
        tracks_failing += len(bad)
        meta = alb.get("meta") or {}
        album_path = str(alb.get("path") or "").replace("\\", "/")
        row = {
            "album_path": album_path,
            "artist": _artist_of(alb),
            "album": (str(meta.get("ALBUM") or "").strip()
                      or album_path.rsplit("/", 1)[-1]),
            "grade_pct": alb.get("grade_pct"),
        }
        if len(bad) == 1 and not sentences:
            tr = bad[0]
            items.append(dict(row,
                              kind="track",
                              track_path=str(tr.get("path") or ""),
                              # The tag the player shows, the file name standing
                              # in when it is empty — a row named "" is a track
                              # the reader cannot match to the album's table.
                              title=(str((tr.get("tags") or {}).get("TITLE") or "").strip()
                                     or str(tr.get("file") or "")),
                              codes=sorted(set(tr.get("issues") or ()))))
            continue
        items.append(dict(row,
                          kind="album",
                          failing_tracks=len(bad),
                          codes=sorted({c for tr in bad
                                        for c in (tr.get("issues") or ())}),
                          **({"reason": sentences[0]} if sentences else {})))
    # Worst first: the lowest grade, then the album with the most failing
    # tracks (a single-track row counts as the one track it is).
    items.sort(key=lambda it: (it["grade_pct"] if it["grade_pct"] is not None else 0.0,
                               -(it.get("failing_tracks") or 1)))
    more = max(0, len(items) - _GRADE_WARNING_MAX)
    return {
        "ok": not items,
        "pass_count": pass_count,
        "total_checks": total_checks,
        "grade_pct": round(100.0 * pass_count / total_checks, 1) if total_checks else None,
        "albums_failing": len(items),
        "tracks_failing": tracks_failing,
        "items": items[:_GRADE_WARNING_MAX],
        "more": more,
    }


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
    # The grading summary is built once and read twice: the header's percentage
    # and the warning strip's totals are the same sums, counted in one place.
    grade = grade_warning(lib)
    stats["grade_pct"] = grade["grade_pct"]

    recent = _recent(albums, recent_count)
    top = _top_rated(albums, max(4, recent_count // 2))
    rated = _rated(albums, user, max(4, recent_count // 2))
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
        # The user's own stars on the releases they gave them to — the one
        # shelf whose rows the CALLER chose rather than the library's grades.
        "rated": rated,
        "favorites": favorites,
        "discover": discover,
        # Every album still waiting for its audio, in one place.
        "pending": pending,
        "top_artists": _top_artists(artists, 6),
        "wanted": _wanted(8, {os.path.normcase(os.path.normpath(a.get("path") or ""))
                              for a in albums if a.get("pending")}),
        "needs_attention": _needs_attention(albums, max(4, recent_count // 2)),
        # Whether the library passes its own checks, and what fails — the SAME
        # object `GET /api/grades/summary` answers with, so Home's strip and
        # the Library page's can never say different things (the Library page
        # has no Home payload to read it from).
        "grade_warning": grade,
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
