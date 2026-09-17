"""MusicBrainz-ID resolution for stable references.

Favorites, likes and playlists survive file moves because every entry
stores BOTH its current path and the MusicBrainz ID (track recording /
album release / artist). This module builds a lightweight index from the
cached library scan and answers two questions:

* resolve — "mb:<uuid>" reference -> current path (for URLs and reads)
* heal    — stored path -> current path when the MBID moved (self-repair)
"""
import os
import threading
import time

from . import library as lib_mod

_LOCK = threading.Lock()
_CACHE = {"index": None, "built": 0.0, "forced_at": 0.0}
_TTL_S = 120.0


def get_index(cfg=None, force=False):
    """MBID <-> path index built from the TTL-cached library payload."""
    with _LOCK:
        fresh = (not force) and _CACHE["index"] is not None \
            and (time.time() - _CACHE["built"]) < _TTL_S
        if fresh:
            return _CACHE["index"]
        if not cfg or not cfg.get("music_folder"):
            from mlo.config import load_config
            cfg = load_config()
        lib = lib_mod.build_library(cfg)
        index = {
            "tracks": {}, "tracks_bypath": {},
            "albums": {}, "albums_bypath": {},
            "artists": {}, "artists_bypath": {},
        }
        for a in lib.get("artists", []):
            artist_dir = a.get("path") or ""
            artist_mbid = ""
            for al in a.get("albums", []):
                meta = al.get("meta") or {}
                album_mbid = str(meta.get("MUSICBRAINZ_ALBUMID") or "").strip()
                if album_mbid:
                    index["albums"][album_mbid.lower()] = al.get("path")
                    index["albums_bypath"][al.get("path")] = album_mbid
                artist_mbid = artist_mbid or str(
                    meta.get("MUSICBRAINZ_ALBUMARTISTID") or "").strip()
                if artist_mbid:
                    index["artists"][artist_mbid.lower()] = artist_dir
                    index["artists_bypath"][artist_dir] = artist_mbid
                for t in al.get("tracks", []):
                    tags = t.get("tags") or {}
                    tmbid = str(tags.get("MUSICBRAINZ_TRACKID") or "").strip()
                    if tmbid:
                        index["tracks"][tmbid.lower()] = t.get("path")
                        index["tracks_bypath"][t.get("path")] = tmbid
        _CACHE["index"] = index
        _CACHE["built"] = time.time()
        return index


def invalidate():
    """Drop the cached index (call after organize/remux moves files)."""
    with _LOCK:
        _CACHE["index"] = None
        _CACHE["built"] = 0.0


def resolve_track(ref):
    """Track reference ("mb:<id>" or path) -> current file path, or None."""
    if not ref:
        return None
    ref = str(ref)
    if ref.lower().startswith("mb:"):
        return get_index()["tracks"].get(ref[3:].strip().lower())
    return ref


def resolve_album(ref):
    if not ref:
        return None
    ref = str(ref)
    if ref.lower().startswith("mb:"):
        return get_index()["albums"].get(ref[3:].strip().lower())
    return ref


def resolve_artist(ref):
    if not ref:
        return None
    ref = str(ref)
    if ref.lower().startswith("mb:"):
        return get_index()["artists"].get(ref[3:].strip().lower())
    return ref


def _norm(p):
    """Library payload paths use forward slashes; normalize lookups."""
    return str(p or "").replace("\\", "/")


def track_mbid_for(path):
    return get_index()["tracks_bypath"].get(_norm(path)) or ""


def heal_row(kind, key, mbid):
    """Return the CURRENT path for a stored (key, mbid) pair.

    Resolution order: stored path while it still exists (fast, no I/O
    beyond one stat) -> MBID from the cached index -> forced re-scan when
    the stored path has vanished, so entries follow reorganizations.
    """
    if kind not in ("track", "album", "artist"):
        return key
    table = f"{kind}s"
    key = _norm(key)
    if os.path.exists(key):
        return key
    if mbid:
        idx = get_index()
        cur = idx.get(table, {}).get(str(mbid).lower())
        if cur and os.path.exists(cur):
            return cur
        # The move is newer than the cached index — rescan, but only once
        # per TTL window: a whole playlist of moved files must not trigger
        # K full library scans back to back.
        with _LOCK:
            rebuild = (time.time() - _CACHE["forced_at"]) > _TTL_S
            if rebuild:
                _CACHE["forced_at"] = time.time()
        if rebuild:
            try:
                from server import tagcache
                tagcache.invalidate_all()
            except Exception:
                pass
            idx = get_index(force=True)
        else:
            idx = get_index()
        cur = idx.get(table, {}).get(str(mbid).lower())
        if cur and os.path.exists(cur):
            return cur
    return key
