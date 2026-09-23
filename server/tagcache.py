"""Small LRU caches for the v2 server.

* Tag/tech cache: keyed on (path, mtime_ns, size) so re-scans of untouched
  files hit memory instead of re-parsing audio containers (mutagen open +
  vorbis-comment copy + Pillow decode are the expensive parts of a library
  scan).
* Library payload cache: TTL cache of the assembled /api/library tree.
* Cover cache: file bytes + dominant color, invalidated on mtime change.
"""
import os
import threading
import time
from collections import OrderedDict

from mlo.audio import AudioFile

_TAG_MAX = 16384
_LIB_TTL = 60.0  # seconds; tag writes/renames bust via invalidate calls
_COVER_MAX = 512

_lock = threading.Lock()
_tag_cache = OrderedDict()
_lib_cache = {}  # key -> (built_at, payload)
_cover_cache = OrderedDict()


def _stat_key(path):
    try:
        st = os.stat(path)
        return (os.path.normcase(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (os.path.normcase(path), 0, 0)


# Display codec names for audio file extensions. .m4a is resolved further
# below via mutagen (ALAC vs AAC live in the same container).
_EXT_CODEC = {
    ".flac": "FLAC", ".mp3": "MP3", ".m4a": "M4A", ".mp4": "M4A",
    ".aac": "AAC", ".ogg": "Vorbis", ".opus": "Opus", ".wav": "WAV",
    ".aiff": "AIFF", ".aif": "AIFF", ".ape": "APE", ".wv": "WavPack",
    ".wma": "WMA", ".alac": "ALAC",
}


def _detect_codec(path, af):
    """Human-readable audio codec for the tech panel (e.g. 'FLAC').
    Mutagen reports the real codec inside MP4 containers (ALAC vs AAC);
    other formats are identified by extension, which is exact for them."""
    ext = os.path.splitext(path)[1].lower()
    codec = _EXT_CODEC.get(ext)
    if ext in (".m4a", ".mp4"):
        try:
            inner = getattr(getattr(af, "audio", None), "info", None)
            inner = str(getattr(inner, "codec", "") or "").lower()
            if inner in ("alac", "aac"):
                codec = inner.upper()
        except Exception:
            pass
    return codec or ext.lstrip(".").upper() or None


def read_track(path, tag_list=None):
    """Return (tags dict, tech dict) for an audio file, cached.

    tag_list: subset of semantic tag names; None reads all_tags() (raw +
    semantic) — used by scan endpoints.
    """
    # The read MODE is part of the key: the library builder caches a track
    # under the TRACK_TAGS subset, and a later /api/tags read of the same file
    # (None: raw + semantic) used to be served that narrower entry — the track
    # page then showed MOOD/REPLAYGAIN/AUDIO_MD5 as untagged while the library
    # list was loaded.
    key = (*_stat_key(path), tuple(sorted(tag_list)) if tag_list else None)
    with _lock:
        hit = _tag_cache.get(key)
        if hit is not None:
            _tag_cache.move_to_end(key)
            return dict(hit[0]), dict(hit[1])
    af = AudioFile(path)
    if af.audio is None:
        tags, tech = {}, {}
    else:
        if tag_list is None:
            tags = af.all_tags() or {}
        else:
            tags = {t: af.get_tag(t) for t in tag_list}
        tech = {}
        if getattr(af, "is_video", False):
            # Video containers carry their tech from ffprobe (video codec,
            # dimensions, duration) instead of mutagen stream info.
            tech.update(getattr(af, "tech", {}) or {})
        else:
            info = af.audio.info
            if info is not None:
                for attr in ("length", "bitrate", "sample_rate", "bits_per_sample", "channels"):
                    try:
                        v = getattr(info, attr, None)
                        if v is not None:
                            tech[attr] = round(float(v), 3) if isinstance(v, (int, float)) else str(v)
                    except Exception:
                        pass
            # Codec (FLAC / MP3 / ALAC / …) shown next to bitrate and depth.
            try:
                codec = _detect_codec(path, af)
                if codec:
                    tech["codec"] = codec
            except Exception:
                pass
    with _lock:
        _tag_cache[key] = (tags, tech)
        _tag_cache.move_to_end(key)
        while len(_tag_cache) > _TAG_MAX:
            _tag_cache.popitem(last=False)
    return dict(tags), dict(tech)


def invalidate_path(path):
    """Drop cached entries for a path (after tag writes / renames)."""
    with _lock:
        norm = os.path.normcase(path)
        keys = [k for k in _tag_cache if k[0] == norm]
        for k in keys:
            del _tag_cache[k]
        for key in list(_lib_cache):
            del _lib_cache[key]


def invalidate_all():
    with _lock:
        _tag_cache.clear()
        _cover_cache.clear()
        _lib_cache.clear()


def _inside(path, folder):
    """Whether *path* is *folder* or sits under it (case/hyphen folded)."""
    p = os.path.normcase(str(path or "")).rstrip("\\/")
    f = os.path.normcase(str(folder or "")).rstrip("\\/")
    return bool(f) and (p == f or p.startswith(f + os.sep))


def invalidate_album(*folders):
    """Drop the cached tags/art of ONE album (or several) after writing it.

    The scoped sibling of `invalidate_all`, and the one an import wants: an
    import rewrites the tags of the files inside ONE album folder and writes
    that album's own cover, and those are exactly the entries that went stale.
    Clearing the whole tag cache instead (16384 entries covering the user's
    entire library) made the next library page re-parse every track in it —
    paying library-wide for one album's import.

    The assembled `/api/library` payload still goes: it is keyed by the library
    folder and the config (`library.library_cache_key`), not per album, so there
    is no scoped way to drop it — but it is ONE entry and it really does hold
    this album. A path that is not under any *folder* is left alone.
    """
    roots = [str(f or "") for f in folders if str(f or "")]
    if not roots:
        return
    with _lock:
        for key in [k for k in _tag_cache if any(_inside(k[0], r) for r in roots)]:
            del _tag_cache[key]
        for key in [k for k in _cover_cache if any(_inside(k[0], r) for r in roots)]:
            del _cover_cache[key]
        _lib_cache.clear()


def get_library(key, builder):
    """TTL-cached library payload. key = (music_folder, relevant config)."""
    now = time.time()
    with _lock:
        hit = _lib_cache.get(key)
        if hit and now - hit[0] < _LIB_TTL:
            return hit[1]
    payload = builder()
    with _lock:
        # Stamp AFTER the build: a slow scan must not be born already stale.
        _lib_cache[key] = (time.time(), payload)
    return payload


def cover_bytes(album, file=None):
    """Return (bytes, ctype, etag) for an album cover file, cached."""
    import hashlib

    p = None
    if file:
        cand = os.path.normpath(os.path.join(album, os.path.basename(file)))
        if os.path.isfile(cand):
            p = cand
    else:
        for cand in ("cover.jpg", "cover.jpeg", "cover.png", "cover.jxl", "cover.webp", "cover.bmp"):
            full = os.path.join(album, cand)
            if os.path.isfile(full):
                p = full
                break
    if p is None:
        return None, None, None
    key = _stat_key(p)
    with _lock:
        hit = _cover_cache.get(key)
        if hit is not None:
            _cover_cache.move_to_end(key)
            return hit
    try:
        with open(p, "rb") as f:
            data = f.read()
        etag = hashlib.md5(data).hexdigest()
        ext = os.path.splitext(p)[1].lower()
        ctype = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".jxl": "image/jxl", ".webp": "image/webp", ".bmp": "image/bmp",
        }.get(ext, "image/jpeg")
        hit = (data, ctype, etag)
    except OSError:
        return None, None, None
    with _lock:
        _cover_cache[key] = hit
        _cover_cache.move_to_end(key)
        while len(_cover_cache) > _COVER_MAX:
            _cover_cache.popitem(last=False)
    return hit


def cover_color(album, file=None):
    """Dominant cover color as '#rrggbb' (used for UI tinting), cached."""
    data, _, _ = cover_bytes(album, file)
    if not data:
        return None
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img = img.resize((1, 1))
        r, g, b = img.getpixel((0, 0))
        return "#%02x%02x%02x" % (r, g, b)
    except Exception:
        return None