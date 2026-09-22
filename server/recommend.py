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
from collections import Counter

from mlo.genre_vocab import is_parent, parent_of
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
# A track shelf is read row by row rather than scanned like covers, so it
# carries a few more rows than an album shelf carries covers.
DEFAULT_TRACK_LIMIT = 20
MAX_LIMIT = 50

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
            (fams if is_parent(folded) else specs).add(folded)
    if not fams:
        for name in sorted(specs):
            parent = parent_of(name)
            if parent:
                fams.add(parent.casefold())
                break
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


def _track_entry(track, album, album_artist):
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
        "album_path": _norm((album or {}).get("path")),
        "cover_path": _norm((album or {}).get("path")) or os.path.dirname(path),
        "cover": track.get("cover_file") or (album or {}).get("cover_file"),
        "specs": specs,
        "fams": fams,
        "mood": _mood(tags),
        "energy": _energy(tags),
        "year": _year(tags, meta),
    }


def _album_entry(album, artist_name, tracks):
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
        "album_path": path,
        "cover_path": path,
        "cover": album.get("cover_file"),
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
            entries = [_track_entry(t, album, artist_name)
                       for t in album.get("tracks", [])]
            entry = _album_entry(album, artist_name, entries)
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


def recommend(cfg, kind, ref="", limit=None, user="", seeds=None, target=None):
    """Scored library items similar to a seed set.

    `kind` picks the seed set: `artist` / `album` / `track` / `playlist` name
    one library entity, `tracks` / `albums` take explicit `seeds` (paths or
    `mb:<uuid>` refs, each a track file, album folder or artist folder), and
    `favorites` reads the caller's own favourites and likes. `target` says
    whether the shelf is made of albums or tracks and defaults per kind;
    `limit` defaults to 12 album covers or 20 track rows.

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
        limit = DEFAULT_LIMIT if target == "albums" else DEFAULT_TRACK_LIMIT
    limit = max(1, min(int(limit), MAX_LIMIT))
    from server import library as lib_mod
    lib = lib_mod.build_library(cfg)
    idx = index(cfg)
    picked = _source(idx, kind, ref, cfg, lib, user, seeds, target)
    if picked is None:
        return []
    profile, entries, extra = picked
    candidates = _candidates(idx, entries, target, extra)
    scored = [(s, c) for s, c in ((_score(profile, c), c) for c in candidates) if s > 0]
    # Path last, so two rows with the same score always come back in the same
    # order — the shelf must not reshuffle between two identical requests.
    scored.sort(key=lambda pair: (-pair[0], pair[1]["path"]))
    return [_item(profile, cand, score) for score, cand in scored[:limit]]
