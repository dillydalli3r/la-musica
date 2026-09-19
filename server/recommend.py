"""Local "more like this" scoring over the library's own tags.

Pure and offline: every comparison is between two sets of tags the library
payload already carries — genres (`mlo/genres.py` spellings, family included),
the MOOD/ENERGY pair the grading scripts write (`mlo/moods.py`), the release
year and the artist name. No provider, no model, no network, so the same
library answers the same list every time and every row can say why it is
there (`reasons`).

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
from mlo.genres import canonical, split_stored

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
MAX_LIMIT = 50

KINDS = ("artist", "album", "track", "playlist")

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
    still say "same family: rock"; a file that tags both keeps them apart so
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
    return {
        "kind": "track",
        "path": path,
        "mbid": str(tags.get("MUSICBRAINZ_TRACKID") or "").strip() or None,
        "title": str(tags.get("TITLE") or "").strip() or _stem(path),
        "subtitle": _display_artist(tags, album_artist),
        "artist_key": _display_artist(tags, album_artist).casefold(),
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
    artist_albums, artist_tracks = {}, {}
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
            artist_albums.setdefault(artist_dir, []).append(entry)
            for t in entries:
                tracks.append(t)
                track_by_path[t["path"]] = t
                artist_tracks.setdefault(artist_dir, []).append(t)
    return {
        "albums": albums,
        "tracks": tracks,
        "album_by_path": album_by_path,
        "track_by_path": track_by_path,
        "artist_albums": artist_albums,
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
        out.append(f"same genre: {name}")
    for name in sorted(src["fams"] & cand["fams"]):
        out.append(f"same family: {name}")
    if src["mood"] and src["mood"] == cand["mood"]:
        out.append(f"same mood: {cand['mood']}")
    if src["energy"] is not None and cand["energy"] is not None:
        diff = abs(src["energy"] - cand["energy"])
        if diff <= NEAR_ENERGY:
            out.append(f"energy {round(cand['energy'])} near {round(src['energy'])}")
        else:
            out.append(f"energy {round(cand['energy'])}")
    if src["year"] and cand["year"]:
        out.append(f"both {cand['year']}" if src["year"] == cand["year"]
                   else f"{src['year']} near {cand['year']}")
    if src["artist_key"] and src["artist_key"] == cand["artist_key"]:
        out.append(f"same artist: {cand['subtitle']}")
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


def _source(idx, kind, ref, cfg, lib, user=""):
    """(profile, candidates) for one request, or None when nothing matches."""
    from server import mbresolve

    if kind == "playlist":
        paths = _playlist_paths(ref, lib, cfg, user)
        if not paths:
            return None
        entries = [idx["track_by_path"][_norm(p)] for p in paths
                   if _norm(p) in idx["track_by_path"]]
        if not entries:
            return None
        skip = frozenset(e["path"] for e in entries)
        candidates = [t for t in idx["tracks"] if t["path"] not in skip]
        # A playlist has no single artist, so artist affinity is dropped and
        # its weight spread over the tags the playlist's tracks do share.
        return {**_aggregate(entries), "artist_key": None}, candidates

    if kind == "artist":
        path = _norm(mbresolve.resolve_artist(ref) or "")
        entries = idx["artist_tracks"].get(path)
        if not entries:
            return None
        own = frozenset(a["path"] for a in idx["artist_albums"].get(path, []))
        candidates = [a for a in idx["albums"] if a["path"] not in own]
        return {**_aggregate(entries), "artist_key": entries[0]["artist_key"]}, candidates

    if kind == "album":
        source = idx["album_by_path"].get(_norm(mbresolve.resolve_album(ref) or ""))
        if source is None:
            return None
        entries = [idx["track_by_path"][p] for p in source["track_paths"]
                   if p in idx["track_by_path"]]
        profile = _aggregate(entries) if entries else source
        candidates = [a for a in idx["albums"] if a["path"] != source["path"]]
        return {**profile, "artist_key": source["artist_key"]}, candidates

    if kind == "track":
        source = idx["track_by_path"].get(_norm(mbresolve.resolve_track(ref) or ""))
        if source is None:
            return None
        # The rest of the source's own album is not a discovery — every one of
        # those tracks would match on artist and genre alone.
        candidates = [t for t in idx["tracks"] if t["album_path"] != source["album_path"]]
        return source, candidates

    raise ValueError(f"unknown kind: {kind}")


def recommend(cfg, kind, ref, limit=DEFAULT_LIMIT, user=""):
    """Scored library items similar to the entity `ref` addresses.

    `ref` is a library path or a `mb:<uuid>` reference — `server/mbresolve.py`
    resolves either against the same payload this index is built from. An
    unknown id, an empty library or a playlist that no longer exists returns
    an empty list: "nothing to suggest" is not an error. `user` selects whose
    playlists a `kind=playlist` request may read ("" is the default scope).
    """
    kind = str(kind or "").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    from server import library as lib_mod
    lib = lib_mod.build_library(cfg)
    picked = _source(index(cfg), kind, ref, cfg, lib, user)
    if picked is None:
        return []
    profile, candidates = picked
    scored = [(s, c) for s, c in ((_score(profile, c), c) for c in candidates) if s > 0]
    # Path last, so two rows with the same score always come back in the same
    # order — the shelf must not reshuffle between two identical requests.
    scored.sort(key=lambda pair: (-pair[0], pair[1]["path"]))
    return [_item(profile, cand, score) for score, cand in scored[:limit]]
