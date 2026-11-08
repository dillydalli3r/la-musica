"""Local "more like this" scoring over the library's own tags.

Pure and offline: every comparison is between two sets of tags the library
payload already carries — genres (`mlo/genres.py` spellings, family included),
the MOOD/ENERGY pair the grading scripts write (`mlo/moods.py`), the release
year and the artist name. No provider, no model, no network, so the same
library answers the same list every time and every row can say why it is
there (`reasons`).

A request names its seed set one of two ways: an entity (`artist`, `album`,
`track`, `playlist`) or an explicit list of references (`tracks`, `albums`) —
a playlist page, a favourites page and a "more like this" shelf on a track
all reach the same scorer, they only differ in what seeds them and whether
the shelf is made of albums or of tracks. `favorites` reads the caller's own
favourites and likes as its seed set, so the favourites page needs no client
round trip to assemble one.

One index of the payload is built per call site and reused while that payload
lives: `server/library.py` already TTL-caches the tree, so holding its own
object is the honest freshness check — a rebuilt payload (a tag write, a
config change, the TTL expiring) yields a new index, and nothing else does.
The per-flight work is then one comparison per candidate, and reasons are
built only for the handful of rows that are actually returned.
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections import Counter

from mlo.genre_vocab import is_parent, parent_of, top_of
from mlo.genres import canonical, display_name, split_stored

# What each signal is worth. Genre names the music, so it carries almost half;
# mood (the coarse label) and energy (the 0-100 arousal it was scored from)
# describe the sound and together weigh 0.35 — enough to outrank a shared
# broad family, never enough to outrank a shared specific genre. Era and
# artist sit at the tail as tie-breakers. A term with no data on either side
# is dropped and its weight redistributed over the rest (the same rule
# `mlo.moods._weighted` uses), so an untagged pair is never scored as if a
# missing tag meant "different".
WEIGHTS = {"genre": 0.45, "mood": 0.20, "energy": 0.15, "era": 0.10, "artist": 0.10}

# Years apart at which two records are simply different eras, and the energy
# difference at which the "near" reason stops being worth saying.
ERA_SPAN = 25
ENERGY_SPAN = 100.0
NEAR_ENERGY = 20

DEFAULT_LIMIT = 12
# ONE default for both targets, and the web asks for the same 12 (the
# `SHELF_LIMIT` every "Recommended" shelf renders with). A page shows the local
# shelf and the online shelf side by side: two lists of the same thing, and the
# reader counts them, so a track shelf carrying 20 rows beside an album shelf
# bearing 12 read as a bug in the shelf that happened to be shorter. A caller
# may still ask for more than the default; `MAX_LIMIT` is the only ceiling.
MAX_LIMIT = 50

# Infinite playback's own batch: what ONE extension of a PLAYING queue adds
# (`recommend_for_queue` / GET+POST /api/recommend/queue). A queue a listener is
# listening to is not a shelf — a handful of rows keeps the music going without
# dumping the library into it — so the route clamps to these whatever the caller
# asks, and the web asks for the default by name (web/src/lib/recommend.ts'
# QUEUE_BATCH, pinned equal by tools/test_recommendations.py).
QUEUE_DEFAULT_LIMIT = 5
QUEUE_MAX_LIMIT = 10

KINDS = ("artist", "album", "track", "playlist", "tracks", "albums", "favorites")
TARGETS = ("albums", "tracks")

# What each seed shape is scored against unless the caller says otherwise: a
# page about one record suggests other records, a list of tracks suggests
# other tracks.
_DEFAULT_TARGET = {"artist": "albums", "album": "albums", "albums": "albums",
                   "track": "tracks", "playlist": "tracks", "tracks": "tracks",
                   "favorites": "tracks"}

_lock = threading.Lock()
_cache = {"lib": None, "index": None}

_YEAR_RE = re.compile(r"(\d{4})")


def _norm(path):
    """Library payload paths use forward slashes; the index keys on that."""
    return str(path or "").replace("\\", "/")


def _year(*sources):
    """First plausible 4-digit year across the given tag dicts."""
    for tags in sources:
        if not tags:
            continue
        for key in ("ORIGINALDATE", "ORIGINALYEAR", "DATE"):
            raw = str(tags.get(key) or "").strip()
            if not raw:
                continue
            m = _YEAR_RE.search(raw)
            if m:
                year = int(m.group(1))
                if 1000 <= year <= 2999:
                    return year
    return None


def _genres(tags):
    """(specific genres, families) for one tag dict, canonically folded.

    A file that tags only `shoegaze` gets its family derived, so a shelf can
    still say "Same family: rock"; a file that tags both keeps them apart so
    a shared family never counts as much as a shared specific genre.
    """
    raw = (tags or {}).get("GENRE")
    if not raw:
        return frozenset(), frozenset()
    values = raw if isinstance(raw, (list, tuple, set)) else [raw]
    specs, fams = set(), set()
    for value in values:
        for name in split_stored(value):
            known = canonical(name)
            folded = " ".join(str(known or name).split()).casefold()
            if not folded:
                continue
            specs.add(folded)
            # The HEAD of every stored name, not a slot read off position: the
            # tag holds specifics (mlo.genres), so the family a shelf groups by
            # is derived here — a `deep house` track lands under electronic
            # through house, and a track whose only genre IS a head keeps it.
            head = top_of(name)
            if head:
                fams.add(head.casefold())
    return frozenset(specs), frozenset(fams)


def _mood(tags):
    value = str((tags or {}).get("MOOD") or "").strip().casefold()
    return value or None


def _energy(tags):
    """The 0-100 ENERGY tag as a float, or None when it was never written."""
    raw = str((tags or {}).get("ENERGY") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, min(100.0, float(raw.split("/")[0].strip())))
    except ValueError:
        return None


def _display_artist(tags, fallback=""):
    tags = tags or {}
    return (str(tags.get("ALBUMARTIST") or "").strip()
            or str(tags.get("ARTIST") or "").strip() or fallback)


def _stem(path):
    return os.path.basename(_norm(path)) or _norm(path)


def _aggregate(entries):
    """The comparison fields of a set of tracks: union of the tags they carry.

    Mood is the most common label and the year the median — both deterministic,
    which is what keeps two calls on the same library identical. ENERGY is the
    mean, so one loud track does not relabel a quiet album.
    """
    specs, fams = set(), set()
    moods, energies, years = Counter(), [], []
    for e in entries:
        specs |= e["specs"]
        fams |= e["fams"]
        if e["mood"]:
            moods[e["mood"]] += 1
        if e["energy"] is not None:
            energies.append(e["energy"])
        if e["year"]:
            years.append(e["year"])
    moods_sorted = sorted(moods)
    years.sort()
    return {
        "specs": frozenset(specs),
        "fams": frozenset(fams),
        "mood": max(moods_sorted, key=lambda m: moods[m]) if moods_sorted else None,
        "energy": (sum(energies) / len(energies)) if energies else None,
        "year": years[len(years) // 2] if years else None,
    }


def _track_entry(track, album, album_artist, artist_path=""):
    tags = track.get("tags") or {}
    meta = (album or {}).get("meta") or {}
    specs, fams = _genres(tags)
    path = _norm(track.get("path"))
    artist = _display_artist(tags, album_artist)
    return {
        "kind": "track",
        "path": path,
        # What a track shelf needs to queue a row without a second lookup:
        # the file inside the folder, the folder, and the release it came from.
        "file": str(track.get("file") or "") or _stem(path),
        "album_name": str(meta.get("ALBUM") or "").strip() or None,
        "mbid": str(tags.get("MUSICBRAINZ_TRACKID") or "").strip() or None,
        "title": str(tags.get("TITLE") or "").strip() or _stem(path),
        "subtitle": artist,
        "artist_key": artist.casefold(),
        "artist_path": _norm(artist_path),
        "album_path": _norm((album or {}).get("path")),
        "cover_path": _norm((album or {}).get("path")) or os.path.dirname(path),
        "cover": track.get("cover_file") or (album or {}).get("cover_file"),
        "specs": specs,
        "fams": fams,
        "mood": _mood(tags),
        "energy": _energy(tags),
        "year": _year(tags, meta),
    }


def _album_entry(album, artist_name, tracks, artist_path=""):
    meta = album.get("meta") or {}
    path = _norm(album.get("path"))
    artist = str(meta.get("ALBUMARTIST") or "").strip() or artist_name
    entry = {
        "kind": "album",
        "path": path,
        "mbid": (str(meta.get("MUSICBRAINZ_ALBUMID") or "").strip()
                 or str(meta.get("MUSICBRAINZ_RELEASEGROUPID") or "").strip() or None),
        "title": str(meta.get("ALBUM") or "").strip() or _stem(path),
        "subtitle": artist,
        "artist_key": artist.casefold(),
        "artist_path": _norm(artist_path),
        "album_path": path,
        "cover_path": path,
        "cover": album.get("cover_file"),
        "pending": bool(album.get("pending")),
        "track_paths": frozenset(t["path"] for t in tracks),
    }
    entry.update(_aggregate(tracks))
    return entry


def _build(lib):
    """Every scorable album and track of one library payload.

    Built once per payload: the per-candidate work is a comparison, never a
    walk of the tree.
    """
    albums, tracks = [], []
    album_by_path, track_by_path = {}, {}
    artist_tracks = {}
    for artist in lib.get("artists", []):
        artist_name = str(artist.get("name") or "").strip()
        artist_dir = _norm(artist.get("path"))
        for album in artist.get("albums", []):
            path = _norm(album.get("path"))
            if not path:
                continue
            entries = [_track_entry(t, album, artist_name, artist_dir)
                       for t in album.get("tracks", [])]
            entry = _album_entry(album, artist_name, entries, artist_dir)
            albums.append(entry)
            album_by_path[path] = entry
            for t in entries:
                tracks.append(t)
                track_by_path[t["path"]] = t
                artist_tracks.setdefault(artist_dir, []).append(t)
    return {
        "albums": albums,
        "tracks": tracks,
        "album_by_path": album_by_path,
        "track_by_path": track_by_path,
        "artist_tracks": artist_tracks,
    }


def index(cfg=None):
    """The index for the current library payload (rebuilt when it changes)."""
    from mlo import load_config
    from server import library as lib_mod

    lib = lib_mod.build_library(cfg or load_config())
    with _lock:
        if _cache["index"] is not None and _cache["lib"] is lib:
            return _cache["index"]
    built = _build(lib)
    with _lock:
        _cache["lib"] = lib
        _cache["index"] = built
    return built


def invalidate():
    """Drop the index (config save, library invalidation)."""
    with _lock:
        _cache["lib"] = None
        _cache["index"] = None
    with _affinity_lock:
        _affinity_cache.update({"lib": None, "user": None, "data": None,
                                "t": 0.0})


# --------------------------------------------------------------------------- #
# Listener affinity — what the person actually plays, rates and likes
# --------------------------------------------------------------------------- #
# The similarity scorer above answers "what sounds like this"; it deliberately
# knows nothing about who is listening. Both Home's "For you" shelf and the
# queue's extension need the other half: the artists, genres and moods this
# listener returns to. The stores already hold that evidence (plays, stars,
# likes, favorites), so the affinity is folded from them once per library
# payload and reused for every row a request scores.
#
# Weights are log-scaled, because one 200-play album must not make an artist
# unbeatable, and recent plays count for more than old ones
# (`plays.counts_since(90)`). A rating is a deliberate statement, so it carries
# a bigger weight than a play; a heart carries one bigger still. Everything is
# normalised to 0..1 per dimension, so one very heavy listener cannot make
# every score in the library look weak.
_affinity_lock = threading.Lock()
_affinity_cache = {"lib": None, "user": None, "data": None, "t": 0.0}
AFFINITY_RECENT_DAYS = 90
# How long one profile is reused. The library payload can stay the same object
# for hours while a person keeps playing; a profile that only changed when the
# TREE changed would recommend from last week's listening. Five minutes is the
# balance: a new favourite becomes visible while listening, and a queue
# extension does not re-read the stores on every batch.
AFFINITY_TTL = 300.0


def _norm_map(mapping):
    return {_norm(k): v for k, v in (mapping or {}).items() if _norm(k)}


def listener_affinity(cfg=None, user="", lib=None):
    """The listener's taste profile, folded from the stores (cached).

    -> {"artists": {artist_key: 0..1}, "genres": {folded: 0..1},
        "moods": {mood: 0..1}, "album_plays": {path: n},
        "track_plays": {path: n}, "album_stars": {path: half},
        "liked": set(paths)}

    The store reads are best-effort: a library with no plays/ratings (a new
    install, a broken database) answers an empty profile rather than an error,
    and every caller then behaves exactly as it did before affinity existed.
    """
    from math import log1p

    from mlo import load_config
    from server import library as lib_mod

    if lib is None:
        lib = lib_mod.build_library(cfg or load_config())
    with _affinity_lock:
        hit = _affinity_cache
        if (hit["data"] is not None and hit["lib"] is lib
                and hit["user"] == str(user or "")
                and (time.time() - float(hit.get("t") or 0.0)) < AFFINITY_TTL):
            return hit["data"]

    idx = _index_for(lib, cfg)
    empty = {"artists": {}, "genres": {}, "moods": {},
             "album_plays": {}, "track_plays": {}, "album_stars": {},
             "liked": set()}
    # A caller building a SYNTHETIC library (the recommendation suite, a
    # wizard preview) names a folder that is not on this machine; the stores
    # belong to the install, not to that fixture, so they are not consulted
    # for it. A real library always has its folder.
    folder = str((cfg or {}).get("music_folder") or "")
    if folder and not os.path.isdir(folder):
        return empty
    try:
        from server import plays as plays_store
        album_plays = _norm_map(plays_store.counts("album", user=user))
        track_plays = _norm_map(plays_store.counts("path", user=user))
        recent_albums = _norm_map(plays_store.counts_since(
            "album", AFFINITY_RECENT_DAYS, user=user))
        recent_tracks = _norm_map(plays_store.counts_since(
            "path", AFFINITY_RECENT_DAYS, user=user))
    except Exception:
        album_plays, track_plays, recent_albums, recent_tracks = {}, {}, {}, {}
    try:
        from server import ratings as ratings_store
        album_stars = _norm_map(ratings_store.map_for(user=user, scope="album"))
        track_stars = _norm_map(ratings_store.map_for(user=user, scope="track"))
        artist_stars = _norm_map(ratings_store.map_for(user=user, scope="artist"))
    except Exception:
        album_stars, track_stars, artist_stars = {}, {}, {}
    try:
        from server import playlists as pl
        liked = {_norm(p) for p in (pl.list_likes(user) or []) if _norm(p)}
        favs = pl.list_favorites(user) or {}
        fav_albums = {_norm(p) for p in (favs.get("albums") or []) if _norm(p)}
        fav_artists = {_norm(p) for p in (favs.get("artists") or []) if _norm(p)}
    except Exception:
        liked, fav_albums, fav_artists = set(), set(), set()

    artists, genres, moods = Counter(), Counter(), Counter()
    artist_of = {}
    for album in idx["albums"]:
        path = album["path"]
        key = album["artist_key"]
        if not key:
            continue
        # Every artist the library holds is known here, even one the stores
        # have no play for yet: a FAVOURITED artist (or a favourite album) is
        # evidence in its own right, and it is applied below.
        artist_of.setdefault(key, album.get("artist_path") or "")
        plays = int(album_plays.get(path, 0)) or sum(
            int(track_plays.get(tp, 0)) for tp in album["track_paths"])
        recent = int(recent_albums.get(path, 0)) or sum(
            int(recent_tracks.get(tp, 0)) for tp in album["track_paths"])
        stars = int(album_stars.get(path, 0))
        liked_here = sum(1 for tp in album["track_paths"] if tp in liked)
        track_stars_here = sum(int(track_stars.get(tp, 0))
                               for tp in album["track_paths"])
        # What one album contributes to its artist and its tags. A play is
        # worth log1p; a recent play half again; a star is a deliberate
        # verdict; a heart is a stronger one.
        weight = (log1p(plays + 0.6 * recent) + 0.28 * stars
                  + 0.9 * liked_here + 0.06 * track_stars_here)
        if path in fav_albums:
            weight += 2.0
        if weight <= 0:
            continue
        artists[key] += weight
        for spec in album["specs"]:
            genres[spec] += weight
        for fam in album["fams"]:
            genres[fam] += weight * 0.45
        if album["mood"]:
            moods[album["mood"]] += weight
        # Per-track evidence: a rated or hearted track says something about the
        # artist even when the album as a whole was never played.
        for track_path in album["track_paths"]:
            tr = idx["track_by_path"].get(track_path)
            if tr is None:
                continue
            tw = (0.12 * int(track_plays.get(track_path, 0))
                  + 0.9 * (1 if track_path in liked else 0)
                  + 0.12 * int(track_stars.get(track_path, 0)))
            if tw <= 0:
                continue
            artists[key] += tw * 0.5
            for spec in tr["specs"]:
                genres[spec] += tw
            for fam in tr["fams"]:
                genres[fam] += tw * 0.45
            if tr["mood"]:
                moods[tr["mood"]] += tw
    # A followed artist / an artist-level star is a statement about the whole
    # artist, so it lifts every genre they are already associated with.
    for key, folder in list(artist_of.items()):
        if not folder:
            continue
        bonus = 0.0
        if folder in fav_artists:
            bonus += 2.5
        if artist_stars.get(folder):
            bonus += 0.3 * int(artist_stars[folder])
        if bonus:
            artists[key] += bonus
    # An artist whose albums the library holds but the store knows nothing
    # about has no affinity yet; nothing is invented for them.
    def norm(counter):
        top = max(counter.values()) if counter else 0
        if not top:
            return {}
        return {k: round(v / top, 4) for k, v in counter.items() if v > 0}

    data = {
        "artists": norm(artists),
        "genres": norm(genres),
        "moods": norm(moods),
        "album_plays": album_plays,
        "track_plays": track_plays,
        "album_stars": album_stars,
        "liked": liked,
    }
    with _affinity_lock:
        _affinity_cache.update({"lib": lib, "user": str(user or ""),
                                "data": data, "t": time.time()})
    return data


def _index_for(lib, cfg=None):
    """The scorable index for ONE library payload (object-identity cached)."""
    with _lock:
        if _cache["index"] is not None and _cache["lib"] is lib:
            return _cache["index"]
    built = _build(lib)
    with _lock:
        _cache["lib"] = lib
        _cache["index"] = built
    return built


def _affinity_genre(aff, entry):
    """The strongest genre signal an album carries, specifics weighted over
    families (a shared family is a weaker reason than one shared genre)."""
    best = 0.0
    for spec in entry["specs"]:
        best = max(best, aff["genres"].get(spec, 0.0))
    for fam in entry["fams"]:
        best = max(best, aff["genres"].get(fam, 0.0) * 0.6)
    return best


def for_you(cfg, user="", limit=DEFAULT_LIMIT, exclude=(), lib=None):
    """Owned albums picked for THIS listener, with the reason each is here.

    The shelf answers the gap the similarity scorer cannot: it is not "more
    like the thing you opened", it is "the next record you are likely to love".
    A candidate qualifies only on a real affinity signal — a played/starred
    artist, a genre the listener returns to, or a mood they favour — and an
    unplayed record is ranked above one already on repeat (the point of a
    recommendation is music you have not worn out).

    `exclude` is the set of album paths already on another Home shelf, so the
    same card does not appear twice on one page. Rows are returned library
    data first: `path`, `title`, `artist`, `score`, `reason`, and a short
    `reasons` list for a tooltip.
    """
    from mlo import load_config
    from mlo.genres import display_name

    try:
        limit = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    from server import library as lib_mod
    if lib is None:
        lib = lib_mod.build_library(cfg or load_config())
    idx = _index_for(lib, cfg)
    aff = listener_affinity(cfg, user=user, lib=lib)
    if not (aff["artists"] or aff["genres"] or aff["moods"]):
        return []
    skip = {_norm(p) for p in (exclude or ()) if _norm(p)}
    scored = []
    for album in idx["albums"]:
        path = album["path"]
        if not path or path in skip or album.get("pending"):
            continue
        artist = aff["artists"].get(album["artist_key"], 0.0)
        genre = _affinity_genre(aff, album)
        mood = aff["moods"].get(album["mood"], 0.0) if album["mood"] else 0.0
        # Strong signal required: a candidate carried only by a broad family
        # or by the artist's sheer presence is not a recommendation.
        if artist < 0.2 and genre < 0.25 and mood < 0.35:
            continue
        plays = int(aff["album_plays"].get(path, 0))
        novelty = 1.0 if plays <= 0 else 0.45 + 0.55 / (1.0 + plays / 3.0)
        score = ((0.50 * artist + 0.32 * genre + 0.18 * mood)
                 * (0.55 + 0.45 * novelty))
        if score <= 0.02:
            continue
        components = [("artist", artist), ("genre", genre), ("mood", mood)]
        kind, _value = max(components, key=lambda pair: pair[1])
        reasons = []
        if artist >= 0.2:
            reasons.append(f"Because you play {album['subtitle']}")
        best_spec = max(album["specs"], key=lambda s: aff["genres"].get(s, 0),
                        default=None) if album["specs"] else None
        if best_spec and aff["genres"].get(best_spec, 0) >= 0.2:
            reasons.append(f"A genre you love: {display_name(best_spec)}")
        if mood >= 0.35:
            reasons.append(f"Your {album['mood']} mood")
        if plays <= 0:
            reasons.append("Not played yet")
        reason = reasons[0] if reasons else "Picked for you"
        scored.append({
            "kind": "album",
            "path": path,
            "title": album["title"],
            "artist": album["subtitle"],
            "score": round(score, 4),
            "reason": reason,
            "reasons": reasons[:3],
            "plays": plays,
        })
    scored.sort(key=lambda r: (-r["score"], r["path"]))
    # One record per artist first, then fill the rest: a prolific favourite
    # would otherwise own the whole shelf.
    picked, seen, rest = [], set(), []
    for row in scored:
        key = (row.get("artist") or "").casefold()
        if key and key not in seen:
            seen.add(key)
            picked.append(row)
        else:
            rest.append(row)
        if len(picked) >= limit:
            break
    if len(picked) < limit:
        picked.extend(rest[:limit - len(picked)])
    return picked[:limit]


def _score(src, cand):
    """Weighted similarity of one candidate to the source profile, 0..1.

    Only the terms both sides actually carry are counted, and the weights of
    those are renormalized, so one untagged album in a tagged library is
    ranked on what it has instead of being pushed to the bottom of the shelf.
    """
    total, weighted = 0.0, 0.0
    if src["specs"] or src["fams"] or cand["specs"] or cand["fams"]:
        shared = 2 * len(src["specs"] & cand["specs"]) + len(src["fams"] & cand["fams"])
        union = 2 * len(src["specs"] | cand["specs"]) + len(src["fams"] | cand["fams"])
        if union:
            total += WEIGHTS["genre"]
            weighted += WEIGHTS["genre"] * (shared / union)
    if src["mood"] and cand["mood"]:
        total += WEIGHTS["mood"]
        if src["mood"] == cand["mood"]:
            weighted += WEIGHTS["mood"]
    if src["energy"] is not None and cand["energy"] is not None:
        total += WEIGHTS["energy"]
        weighted += WEIGHTS["energy"] * max(
            0.0, 1.0 - abs(src["energy"] - cand["energy"]) / ENERGY_SPAN)
    if src["year"] and cand["year"]:
        total += WEIGHTS["era"]
        weighted += WEIGHTS["era"] * max(
            0.0, 1.0 - abs(src["year"] - cand["year"]) / ERA_SPAN)
    if src["artist_key"] and cand["artist_key"]:
        total += WEIGHTS["artist"]
        if src["artist_key"] == cand["artist_key"]:
            weighted += WEIGHTS["artist"]
    return weighted / total if total else 0.0


# The owner's rule (2026-10-17): a shelf must be able to say WHY each row is
# there, and a list that lists everything ranked by a weak similarity is not a
# recommendation. A candidate qualifies only on a STRONG listening signal —
# a shared specific genre, the same mood, close energy, or the same artist —
# never on era or a family alone; a score floor catches the pairs several
# weak terms agree on. Below `SHELF_MIN_ROWS` qualifying rows the shelf is
# EMPTY rather than padded: "nothing in your library sounds like this" is an
# answer, and a one-row shelf is noise. Two is a shelf (a small library must
# still get recommendations); one related row beside an unrelated grid is not.
STRONG_ENERGY = 25.0
SHELF_MIN_ROWS = 2


def _relevant(src, cand, score):
    """Does this candidate earn a place on a listener's shelf?"""
    if src["specs"] & cand["specs"]:
        return True                                   # a shared specific genre
    if src["mood"] and src["mood"] == cand["mood"]:
        return True                                   # the same mood
    if (src["energy"] is not None and cand["energy"] is not None
            and abs(src["energy"] - cand["energy"]) <= STRONG_ENERGY):
        return True                                   # close energy
    if src["artist_key"] and src["artist_key"] == cand["artist_key"]:
        return True                                   # the same artist
    return score >= 0.5


def _reasons(src, cand):
    """Why this candidate is on the shelf, strongest signal first.

    Built for the returned rows only; every row that scored above zero has at
    least one of these, because a term only reaches the score through one.
    """
    out = []
    for name in sorted(src["specs"] & cand["specs"]):
        out.append(f"Same genre: {display_name(name)}")
    for name in sorted(src["fams"] & cand["fams"]):
        out.append(f"Same family: {display_name(name)}")
    if src["mood"] and src["mood"] == cand["mood"]:
        out.append(f"Same mood: {cand['mood']}")
    if src["energy"] is not None and cand["energy"] is not None:
        diff = abs(src["energy"] - cand["energy"])
        if diff <= NEAR_ENERGY:
            out.append(f"Energy {round(cand['energy'])} near {round(src['energy'])}")
        else:
            out.append(f"Energy {round(cand['energy'])}")
    if src["year"] and cand["year"]:
        out.append(f"Both {cand['year']}" if src["year"] == cand["year"]
                   else f"{src['year']} near {cand['year']}")
    if src["artist_key"] and src["artist_key"] == cand["artist_key"]:
        out.append(f"Same artist: {cand['subtitle']}")
    return out[:4]


def _item(src, cand, score):
    return {
        "kind": cand["kind"],
        "id": f"mb:{cand['mbid']}" if cand["mbid"] else cand["path"],
        "path": cand["path"],
        "mbid": cand["mbid"],
        "title": cand["title"],
        "subtitle": cand["subtitle"],
        "score": round(score, 4),
        "reasons": _reasons(src, cand),
        "cover_path": cand["cover_path"],
        "cover": cand["cover"],
        # Enough to build a queue entry for a track row, or to look the album
        # up in the client's cached payload — the shelf adds no extra fetch.
        "file": cand.get("file"),
        "album_path": cand.get("album_path") or cand["path"],
        "album": cand.get("album_name") or cand["title"],
        "artist": cand["subtitle"],
    }


def _playlist_paths(ref, lib, cfg, user=""):
    """Track paths of a playlist, smart ones evaluated against the payload."""
    from server import playlists as pl
    try:
        pid = int(ref)
    except (TypeError, ValueError):
        return None
    playlist = pl.get_playlist(pid, user=user)
    if playlist is None:
        return None
    if playlist.get("kind") == "smart":
        evaluated = pl.evaluate_smart(pid, lib, user=user)
        if evaluated is not None:
            return evaluated
    return playlist.get("tracks") or []


def _seed_entries(idx, refs):
    """The track entries a set of seed references stands for.

    A reference is resolved against the same payload the index was built from,
    and may be a track file, an album folder or an artist folder: a favourites
    set mixes all three, a playlist hands over track paths. References the
    library no longer holds are skipped — a stale favourite must thin the seed
    set, never empty the shelf or raise.
    """
    from server import mbresolve

    out, seen = [], set()
    for raw in refs or []:
        ref = str(raw or "").strip()
        if not ref:
            continue
        entry = idx["track_by_path"].get(_norm(mbresolve.resolve_track(ref) or ""))
        if entry is not None:
            entries = [entry]
        else:
            album = idx["album_by_path"].get(_norm(mbresolve.resolve_album(ref) or ""))
            if album is not None:
                entries = [idx["track_by_path"][p] for p in album["track_paths"]
                           if p in idx["track_by_path"]]
            else:
                entries = idx["artist_tracks"].get(
                    _norm(mbresolve.resolve_artist(ref) or "")) or []
        for e in entries:
            if e["path"] not in seen:
                seen.add(e["path"])
                out.append(e)
    return out


def _favorite_entries(idx, target, user=""):
    """The caller's own favourites as seeds — likes for a track shelf,
    favourite albums and artists for an album shelf.

    Favourite PLAYLISTS are deliberately not seeds of an album shelf: a smart
    playlist can hold the whole library, and excluding "the albums it already
    contains" from the shelf would leave nothing to suggest.
    """
    from server import playlists as pl

    if target == "tracks":
        refs = pl.list_likes(user)
    else:
        favs = pl.list_favorites(user)
        refs = list(favs.get("albums") or []) + list(favs.get("artists") or [])
    return _seed_entries(idx, refs)


def _seed_profile(entries):
    """The profile of a seed SET (a playlist, a favourites set, explicit refs).

    One artist is claimed only when every seed agrees on it: a mixed set that
    pretended to have one would put "Same artist" on rows that do not share
    one, and its weight is better spent on the tags the seeds do share.
    """
    keys = {e["artist_key"] for e in entries if e["artist_key"]}
    return {**_aggregate(entries), "artist_key": keys.pop() if len(keys) == 1 else None}


def _candidates(idx, entries, target, extra_albums=frozenset()):
    """Everything the seeds are not.

    A seed's own album is never a suggestion — the user already holds it, and
    on a track shelf every remaining track on it would match on artist and
    genre alone. An album shelf loses the album itself that way, a track shelf
    loses the rest of the record. `extra_albums` covers a seed album the
    payload holds no tracks for.
    """
    albums = frozenset(e["album_path"] for e in entries if e["album_path"]) | extra_albums
    paths = frozenset(e["path"] for e in entries)
    if target == "albums":
        return [a for a in idx["albums"] if a["path"] not in albums]
    return [t for t in idx["tracks"]
            if t["path"] not in paths and t["album_path"] not in albums]


def _source(idx, kind, ref, cfg, lib, user="", seeds=None, target="tracks"):
    """(profile, seed entries, album to exclude) for one request, or None.

    Every kind is reduced to the same pair — a profile to compare against and
    the tracks that seeded it — so one exclusion rule serves all of them.
    """
    from server import mbresolve

    if kind == "playlist":
        paths = _playlist_paths(ref, lib, cfg, user)
        if not paths:
            return None
        entries = [idx["track_by_path"][_norm(p)] for p in paths
                   if _norm(p) in idx["track_by_path"]]
        if not entries:
            return None
        # A playlist has no single artist, so artist affinity is dropped and
        # its weight spread over the tags the playlist's tracks do share.
        return _seed_profile(entries), entries, frozenset()

    if kind == "artist":
        path = _norm(mbresolve.resolve_artist(ref) or "")
        entries = idx["artist_tracks"].get(path)
        if not entries:
            return None
        return ({**_aggregate(entries), "artist_key": entries[0]["artist_key"]},
                entries, frozenset())

    if kind == "album":
        source = idx["album_by_path"].get(_norm(mbresolve.resolve_album(ref) or ""))
        if source is None:
            return None
        entries = [idx["track_by_path"][p] for p in source["track_paths"]
                   if p in idx["track_by_path"]]
        profile = _aggregate(entries) if entries else source
        return {**profile, "artist_key": source["artist_key"]}, entries, \
            frozenset({source["path"]})

    if kind == "track":
        source = idx["track_by_path"].get(_norm(mbresolve.resolve_track(ref) or ""))
        if source is None:
            return None
        return source, [source], frozenset()

    if kind in ("tracks", "albums"):
        entries = _seed_entries(idx, seeds)
        return (_seed_profile(entries), entries, frozenset()) if entries else None

    if kind == "favorites":
        entries = _favorite_entries(idx, target, user)
        return (_seed_profile(entries), entries, frozenset()) if entries else None

    raise ValueError(f"unknown kind: {kind}")


def _target(kind, target):
    """Which kind of library item the caller wants back."""
    want = str(target or "").strip().lower()
    if want and want not in TARGETS:
        raise ValueError(f"unknown target: {want}")
    return want or _DEFAULT_TARGET[kind]


def recommend(cfg, kind, ref="", limit=None, user="", seeds=None, target=None,
              min_rows=SHELF_MIN_ROWS):
    """Scored library items similar to a seed set.

    `kind` picks the seed set: `artist` / `album` / `track` / `playlist` name
    one library entity, `tracks` / `albums` take explicit `seeds` (paths or
    `mb:<uuid>` refs, each a track file, album folder or artist folder), and
    `favorites` reads the caller's own favourites and likes. `target` says
    whether the shelf is made of albums or tracks and defaults per kind;
    `limit` defaults to 12 rows of EITHER kind — the local shelf and the online
    shelf beside it are read as a pair, so they carry one number.

    `ref` is a library path or a `mb:<uuid>` reference — `server/mbresolve.py`
    resolves either against the same payload this index is built from. An
    unknown id, an empty seed set, an empty library or a playlist that no
    longer exists returns an empty list: "nothing to suggest" is not an error.
    `user` selects whose playlists, likes and favourites a request may read
    ("" is the default scope).
    """
    kind = str(kind or "").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    target = _target(kind, target)
    if limit is None:
        limit = DEFAULT_LIMIT
    limit = max(1, min(int(limit), MAX_LIMIT))
    from server import library as lib_mod
    lib = lib_mod.build_library(cfg)
    idx = index(cfg)
    picked = _source(idx, kind, ref, cfg, lib, user, seeds, target)
    if picked is None:
        return []
    profile, entries, extra = picked
    candidates = _candidates(idx, entries, target, extra)
    scored = [(s, c) for s, c in ((_score(profile, c), c) for c in candidates)
              if s > 0 and _relevant(profile, c, s)]
    # Below the minimum a shelf is not drawn at all (an empty list is the
    # page's own "nothing like this" state). Queue extensions pass their own
    # minimum — one good row still keeps the music going.
    if len(scored) < (1 if min_rows is None else min_rows):
        return []
    # Path last, so two rows with the same score always come back in the same
    # order — the shelf must not reshuffle between two identical requests.
    scored.sort(key=lambda pair: (-pair[0], pair[1]["path"]))
    return [_item(profile, cand, score) for score, cand in scored[:limit]]


def recommend_for_queue(cfg, paths, limit=None, user=""):
    """Rows to KEEP A PLAYING QUEUE GOING: tracks similar to the queue's own
    set, and never a track it already holds.

    `paths` is the queue itself — the seed set AND the exclusion set. Each entry
    is a track file, or an album / artist folder, which `_seed_entries` resolves
    the same way it does for a shelf: a queue seeded by a folder seeds from
    every track of it. A queue is a seed LIST built by playing, so a duplicate
    entry is one seed (never two rows), and a path the library no longer holds
    is skipped rather than fatal — a queue holding a file that was since deleted
    still gets its extension.

    The tag similarity is the FLOOR (the same `_relevant` gate the shelves
    use); the listener's own plays and stars are a re-rank on top, so the next
    batch leans towards the artists and genres this person returns to instead
    of being a purely acoustic match. A track already on repeat is gently
    down-ranked so a queue does not keep replaying the same favourites. With
    no play/rating history the affinity is empty and the answer is exactly the
    similarity list it always was.

    The answer is BOUNDED by `QUEUE_MAX_LIMIT` whatever the caller asks, and
    defaults to `QUEUE_DEFAULT_LIMIT`: this extends a queue someone is
    listening to, it is not a shelf, and nothing about it is worth a stall. An
    empty or unmatched queue, or a library with nothing similar in it, is an
    empty list — the play then simply ends as it did before the switch existed.
    """
    if limit is None:
        limit = QUEUE_DEFAULT_LIMIT
    limit = max(1, min(int(limit), QUEUE_MAX_LIMIT))
    from mlo import load_config
    from server import library as lib_mod

    lib = lib_mod.build_library(cfg or load_config())
    idx = _index_for(lib, cfg)
    picked = _source(idx, "tracks", "", cfg, lib, user, list(paths or []), "tracks")
    if picked is None:
        return []
    profile, entries, extra = picked
    candidates = _candidates(idx, entries, "tracks", extra)
    aff = listener_affinity(cfg, user=user, lib=lib)
    listened = bool(aff["artists"] or aff["genres"] or aff["moods"])
    scored = []
    for cand in candidates:
        base = _score(profile, cand)
        if base <= 0 or not _relevant(profile, cand, base):
            continue
        if not listened:
            scored.append((base, cand, []))
            continue
        artist = aff["artists"].get(cand["artist_key"], 0.0)
        genre = _affinity_genre(aff, cand)
        mood = aff["moods"].get(cand["mood"], 0.0) if cand["mood"] else 0.0
        affinity = 0.50 * artist + 0.30 * genre + 0.20 * mood
        plays = int(aff["track_plays"].get(cand["path"], 0))
        novelty = 1.0 if plays <= 0 else 0.45 + 0.55 / (1.0 + plays / 6.0)
        final = (base * (0.80 + 0.20 * affinity) * (0.85 + 0.15 * novelty)
                 + 0.08 * affinity)
        reasons = []
        if artist >= 0.2:
            reasons.append(f"Because you play {cand['subtitle']}")
        best = max((aff["genres"].get(g, 0.0) for g in cand["specs"]),
                   default=0.0)
        if best >= 0.2:
            reasons.append("A genre you keep playing")
        scored.append((final, cand, reasons))
    if not scored:
        return []
    scored.sort(key=lambda pair: (-pair[0], pair[1]["path"]))
    out = []
    for score, cand, reasons in scored[:limit]:
        item = _item(profile, cand, min(1.0, score))
        if reasons:
            item["reasons"] = (reasons + list(item["reasons"]))[:4]
        out.append(item)
    return out
