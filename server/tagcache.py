"""Small LRU caches for the v2 server.

* Tag/tech cache: keyed on (path, mtime_ns, size) so re-scans of untouched
  files hit memory instead of re-parsing audio containers (mutagen open +
  vorbis-comment copy + Pillow decode are the expensive parts of a library
  scan).
* Library payload cache: TTL cache of the assembled /api/library tree.
* Cover cache: file bytes + dominant color, invalidated on mtime change.
"""
import hashlib
import json
import os
import threading
import time
import traceback
from collections import OrderedDict

from mlo.atomic import read_bytes_locked
from mlo.audio import AudioFile
from mlo.paths import LIB_AUDIO_EXTS

_TAG_MAX = 16384
_LIB_TTL = 60.0  # seconds; tag writes/renames bust via invalidate calls
_COVER_MAX = 512
# A page payload (album, artist) is derived from the files AND from the app's
# own state for them — the grade verdicts included — so it is memoized for a
# short window only, and every `invalidate_*` clears it outright. The window is
# what bounds a change made OUTSIDE the app (a script, a tag editor, another
# machine on the share): an in-app write goes through an invalidate hook and is
# never served stale.
_PAYLOAD_TTL = 30.0
_PAYLOAD_MAX = 128

_lock = threading.Lock()
# signalled when a library build finishes, so concurrent first-paint requests
# share ONE build instead of each re-deriving the whole library
_build_done = threading.Condition(_lock)
_tag_cache = OrderedDict()
_lib_cache = {}  # key -> (built_at, payload)
# key -> (payload object, etag, json bytes): the tree's OWN serialization, so
# `/api/library` does not re-encode it per request (and can answer 304).
_lib_body = {}
_lib_building = set()  # keys a thread is currently building
# Keys whose cached tree an in-app write made stale, and the keys a background
# thread is rebuilding right now. See `_refresh_library`.
_lib_dirty = set()
_lib_refreshing = set()
# Generation counters for the two ways the tree goes stale, so a background
# rebuild that STARTED before a change can tell whether its result still
# describes the library (see `_refresh_library`). `_lib_write_gen` is bumped by
# every mark-dirty (a write to a file); `_lib_drop_gen` by every outright drop
# (a settings change, the Refresh button). Without them a rebuild that finished
# while a script was still writing stored its PRE-write rows and cleared the
# dirty flag, so nothing rebuilt again and the page sat on the stale tree —
# the write that landed mid-build was never the one the page ended on.
_lib_write_gen = 0
_lib_drop_gen = 0
_payload_cache = {}  # (kind, path, config key) -> (built_at, payload)
_cover_cache = OrderedDict()
_color_cache = OrderedDict()  # cover stat key -> "#rrggbb"
# audio file stat key -> (bytes, ctype) of its embedded picture, or () for
# "this file was read and carries none" (see `embedded_cover_bytes`).
_embedded_cache = OrderedDict()


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
        # An UNREADABLE file is not an empty one. A lock held by an antivirus
        # pass, a scanner or a concurrent replace made this read fail; caching
        # the empty result (as this used to) served a stale "untagged" row for
        # the track and its album until the file's stat happened to change —
        # the read error the owner saw as missing tags rather than as an error.
        # Answer THIS call empty, cache NOTHING so the next read tries again,
        # and carry the reason so a caller can tell a failed read from a
        # genuinely tagless track (the empty `tags` stays the same shape every
        # caller already handles).
        return {}, ({"error": str(af.error)} if af.error else {})
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
    """Drop cached entries for a path (after tag writes / renames).

    The assembled `/api/library` tree is NOT dropped here: it is marked dirty
    and re-derived behind the next request (see `get_library`), because a tag
    write to one file made the next page load rebuild every row in it.
    """
    global _lib_write_gen
    with _lock:
        norm = os.path.normcase(path)
        keys = [k for k in _tag_cache if k[0] == norm]
        for k in keys:
            del _tag_cache[k]
        for key in [k for k in _embedded_cache if k[0] == norm]:
            del _embedded_cache[key]
        _lib_dirty.update(_lib_cache)
        _lib_write_gen += 1
        _payload_cache.clear()
    _drop_index([path])


def _drop_index(paths):
    """Drop the persistent index rows an in-app write made stale.

    The same paths the in-memory caches are dropped for: `server.tagindex`
    keys an album on its files' stats, so a write already misses there — but a
    write that does NOT move a stat (a tag written back byte-identically, a
    rename inside the folder, an evidence store rewritten for those files) must
    not be left to a time window. Cheap: the index is a few hundred rows.
    """
    try:
        from server import tagindex
        tagindex.drop(paths)
    except Exception:
        pass


def invalidate_all():
    global _lib_drop_gen
    with _lock:
        _tag_cache.clear()
        _cover_cache.clear()
        _color_cache.clear()
        _embedded_cache.clear()
        _lib_cache.clear()
        _lib_body.clear()
        _lib_dirty.clear()
        _lib_drop_gen += 1
        _payload_cache.clear()
        _build_done.notify_all()
    try:
        from server import tagindex
        tagindex.drop_all()
    except Exception:
        pass


def invalidate_library_payloads():
    """Drop the ASSEMBLED payloads, keep everything keyed on the files.

    This is the Refresh button's own drop (`server.main._refresh_library_caches`).
    Refresh asks the app to re-derive the library FROM THE FILES — and every
    cache between the files and the tree is keyed on the files themselves: the
    tag cache on ``(path, mtime_ns, size)``, an album's indexed payload on the
    folder's own signature, the grade inputs on the config and the state
    stores' stamps. A file added, removed, rewritten or retagged since the tree
    was built is therefore DETECTED by the rebuild, never hidden by a cached
    entry.

    Clearing that whole ladder first (what `invalidate_all` does) proved
    nothing extra and cost the point of Refresh: the rebuild re-opened every
    container and re-graded every album — seconds to tens of seconds on the
    owner's install — to arrive at the same rows for every unchanged file.
    The tree itself IS dropped rather than marked dirty: the press promises the
    rows it answers with are the fresh ones, and with the caches below it the
    rebuild is stat-cheap.

    `invalidate_all` stays for what really invalidates everything: a settings
    change (the config rides every key) and a cold sweep.
    """
    global _lib_drop_gen
    with _lock:
        _lib_cache.clear()
        _lib_dirty.clear()
        _lib_body.clear()
        _lib_drop_gen += 1
        _payload_cache.clear()
        _build_done.notify_all()


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

    The assembled `/api/library` payload still goes stale — it is keyed by the
    library folder and the config (`library.library_cache_key`), not per album,
    so there is no scoped way to keep it — but it is marked DIRTY rather than
    dropped: the next request is served the tree as it stands and the refresh
    happens behind it (`get_library`). A path that is not under any *folder* is
    left alone.
    """
    roots = [str(f or "") for f in folders if str(f or "")]
    if not roots:
        return
    global _lib_write_gen
    with _lock:
        for key in [k for k in _tag_cache if any(_inside(k[0], r) for r in roots)]:
            del _tag_cache[key]
        for key in [k for k in _cover_cache if any(_inside(k[0], r) for r in roots)]:
            del _cover_cache[key]
        for key in [k for k in _embedded_cache if any(_inside(k[0], r) for r in roots)]:
            del _embedded_cache[key]
        for key in [k for k in _color_cache if any(_inside(k[0], r) for r in roots)]:
            del _color_cache[key]
        _lib_dirty.update(_lib_cache)
        _lib_write_gen += 1
        _payload_cache.clear()
        _build_done.notify_all()
    _drop_index(roots)


def _json_document(payload):
    """A payload's `(etag, json bytes)` — the one serialization of it.

    Used for the library tree, the app's largest payload: FastAPI re-encodes a
    returned dict on every request (`jsonable_encoder` + `json.dumps`, then
    gzip), always to the same bytes for the same cached tree. The bytes are
    derived where the tree is derived instead, so every request after the first
    — and every conditional revalidation — costs a hash comparison.
    """
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                      default=str).encode("utf-8")
    return hashlib.sha1(body).hexdigest(), body


def json_document(payload):
    """`(etag, json bytes)` for a payload built OUTSIDE the library cache
    (the "music folder not set" answer, which is never cached)."""
    return _json_document(payload)


def _refresh_library(key, builder, notify=False):
    """Rebuild one cached tree OFF the request path (single-flight).

    Never runs while the key is being built for a first paint, never twice at
    once, and never raises: a refresh that fails leaves the cached tree in
    place and the next request tries again. The stamp is written AFTER the
    build, like the first paint's, so a slow scan is not born already stale.

    `notify` says a write marked the tree dirty (rather than the TTL simply
    lapsing): the rebuilt tree is announced on the event bus, because every
    client has just been served the PRE-write rows (the stale-while-revalidate
    contract above) and this frame is what makes it ask again for the fresh
    ones. Without it a page would keep the old rows until the next visit.
    """
    with _lock:
        if key in _lib_refreshing or key in _lib_building:
            return
        _lib_refreshing.add(key)
        write_gen = _lib_write_gen
        drop_gen = _lib_drop_gen

    def run():
        payload = None
        document = None
        try:
            payload = builder()
        except BaseException as e:
            print(f"[mlo] library background refresh failed: {e}")
        if payload is not None:
            document = (payload, *_json_document(payload))
        with _lock:
            # Store only when nothing invalidated the tree while we built. A
            # DROP (a settings change, Refresh) means this payload was derived
            # from a library the caller has already thrown away; a WRITE means
            # it may predate that write — whose own frame brings a client back,
            # so `_lib_dirty` stays set and the next request rebuilds again
            # rather than clearing the flag on a tree that does not include it.
            if payload is not None and drop_gen == _lib_drop_gen:
                _lib_cache[key] = (time.time(), payload)
                _lib_body[key] = document
                if write_gen == _lib_write_gen:
                    _lib_dirty.discard(key)
            _lib_refreshing.discard(key)
            _build_done.notify_all()
        if payload is not None and notify:
            try:
                from server import events
                events.note_library_write()
            except Exception:
                traceback.print_exc()

    threading.Thread(target=run, daemon=True, name="library-refresh").start()


def get_library(key, builder):
    """The assembled library tree: served from memory, refreshed behind it.

    SINGLE-FLIGHT for the first paint. The first paint of every page asks for
    this tree — Home, the Library page, its facets and query engine, the grade
    summary, the discovery shelves — and they arrive at once, from several
    request threads. Building the tree used to be done by whoever found the
    entry expired, so a cold process paid the whole library once PER concurrent
    request (measured: a restart made a small library take tens of seconds to
    answer, then the same seconds again for the next tab). The first caller
    builds and the others wait for that one result.

    A tree that is merely OLD (`_LIB_TTL`) or that an in-app write marked dirty
    is STALE-WHILE-REVALIDATE instead of a rebuild: it is returned as it stands
    and `_refresh_library` re-derives it in the background, so no page load
    ever waits for a walk it did not ask for. Measured on the owner's install
    (170 files, bind-mounted library, Docker Desktop on Windows): the tree took
    0.9-14.5 s to rebuild IN the request that found it stale — every minute the
    TTL lapsed, and again after every import, tag write and script run — which
    is exactly the "Loading… that takes a while" this exists to remove. The
    contract that pays for it: the rows a user sees right after an in-app write
    are the pre-write ones for as long as the refresh takes (one album build
    per changed folder, ~1 s for a whole library this size), while every
    PER-PAGE payload (album, artist, grades) is dropped outright by the same
    invalidation and rebuilt fresh. A change made OUTSIDE the app is bounded by
    the TTL, as before.

    A builder that raises wakes the waiters and does not stamp the cache: the
    next caller builds instead of waiting for a result that is not coming.
    """
    while True:
        with _lock:
            hit = _lib_cache.get(key)
            if hit is not None:
                dirty = key in _lib_dirty
                stale = dirty or time.time() - hit[0] >= _LIB_TTL
                payload = hit[1]
                break
            if key not in _lib_building:
                _lib_building.add(key)
                payload = None
                break
            _build_done.wait(timeout=300.0)
    if payload is not None:
        if stale:
            _refresh_library(key, builder, notify=dirty)
        return payload
    try:
        payload = builder()
    except BaseException:
        with _lock:
            _lib_building.discard(key)
            _build_done.notify_all()
        raise
    document = (payload, *_json_document(payload))
    with _lock:
        # Stamp AFTER the build: a slow scan must not be born already stale.
        _lib_cache[key] = (time.time(), payload)
        _lib_body[key] = document
        _lib_dirty.discard(key)
        _lib_building.discard(key)
        _build_done.notify_all()
    return payload


def get_library_document(key, builder):
    """`(etag, json bytes)` for the tree `get_library` serves.

    The same cache entry — this is a second VIEW of one build, not a second
    build — so the bytes stored when the tree was built are returned, and the
    request path never serializes the tree (`_json_document`).
    """
    payload = get_library(key, builder)
    with _lock:
        hit = _lib_body.get(key)
        if hit is not None and hit[0] is payload:
            return hit[1], hit[2]
    etag, body = _json_document(payload)
    with _lock:
        _lib_body[key] = (payload, etag, body)
    return etag, body


def cached_payload(kind, path, cfg, builder, ttl=_PAYLOAD_TTL):
    """Short-TTL memo for a page payload derived from one folder.

    The album and artist routes re-derive everything they serve per request —
    the grade verdicts included — so revisiting a page re-graded the album it
    had just shown. `kind` names the payload ("album", "artist"), *path* is the
    folder it is about, and the config rides the key, so a settings change can
    never serve a payload computed under other settings.

    Cleared outright by every `invalidate_*` call, which is how every in-app
    write (tag write, cover, import, script run) busts it — the TTL is only the
    bound on a change made outside the app.
    """
    try:
        from server import tagindex
        key = (kind, os.path.normcase(str(path)), tagindex.config_key(cfg))
    except Exception:
        return builder()
    now = time.time()
    with _lock:
        hit = _payload_cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    payload = builder()
    with _lock:
        _payload_cache[key] = (time.time(), payload)
        while len(_payload_cache) > _PAYLOAD_MAX:
            _payload_cache.pop(next(iter(_payload_cache)))
    return payload


# The canonical album cover names, the order the folder's own art is picked in,
# and the content type each container is served as.
_COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png", "cover.jxl",
                "cover.webp", "cover.bmp")
_COVER_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".jxl": "image/jxl", ".webp": "image/webp", ".bmp": "image/bmp"}


def cover_path(album, file=None):
    """The cover image FILE an album serves: *file* when it is really in the
    folder, else the first of the standard names that exists, else None.

    *file* is a HINT, not a demand. It arrives off a payload that can predate
    what is on disk: every writer here replaces a cover in place under the
    canonical name — `server.api_cover._write_cover_bytes` drops the old
    extension after a PNG→JPEG re-encode, and script 5 renames the folder's own
    cover candidate (`mlo.images._rename_to_cover`: `front.jpg`/`folder.jpg`/
    `cover.png` → `cover.jpg`) — so a queue row, a page or an offline cache that
    still names `cover.png` after the write that turned it into `cover.jpg` was
    asking for a file that no longer exists. Answering None there left the one
    surface that cannot recover on its own — the player's artwork — on the disc
    placeholder for the whole track, however well the album's cover sat on
    disk. A missing *file* therefore falls through to the album's own cover:
    the stale name still resolves, to the album's own art.

    Split out of `cover_bytes` because the sized-thumb path (see
    `artcache.cover_thumb`) has to know WHICH file it is shrinking — the
    bytes' cache key is that file's stat.
    """
    if file:
        cand = os.path.normpath(os.path.join(album, os.path.basename(file)))
        if os.path.isfile(cand):
            return cand
    for cand in _COVER_NAMES:
        full = os.path.join(album, cand)
        if os.path.isfile(full):
            return full
    return None


def cover_bytes(album, file=None):
    """Return (bytes, ctype, etag) for an album cover file, cached.

    An unreadable file is NOT an empty one. The read is retried through a
    transient denial — the window in which a writer has just replaced the file,
    a scanner or an anti-malware pass holds it open (`mlo.atomic
    .read_bytes_locked`, the read-side twin of the retry every writer's final
    swap already goes through) — and a read that really fails caches NOTHING,
    so the next request asks again instead of serving a failure that a later,
    healthy read could never displace.
    """
    p = cover_path(album, file)
    if p is None:
        return None, None, None
    key = _stat_key(p)
    with _lock:
        hit = _cover_cache.get(key)
        if hit is not None:
            _cover_cache.move_to_end(key)
            return hit
    try:
        data = read_bytes_locked(p)
    except OSError:
        return None, None, None
    if not data:
        return None, None, None
    hit = (data, _COVER_TYPES.get(os.path.splitext(p)[1].lower(), "image/jpeg"),
           hashlib.md5(data).hexdigest())
    with _lock:
        _cover_cache[key] = hit
        _cover_cache.move_to_end(key)
        while len(_cover_cache) > _COVER_MAX:
            _cover_cache.popitem(last=False)
    return hit


def _image_ctype(data, mime=""):
    """The content type for embedded image bytes: the container's own mime when
    the tagger states one, else the one the bytes really are."""
    m = str(mime or "").split(";")[0].strip().lower()
    if m.startswith("image/"):
        return "image/jpeg" if m == "image/jpg" else m
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _cover_audio_candidates(album, file=None):
    """The album's audio files to read embedded art from, best first.

    The track the caller named leads: a per-track sidecar is that track's
    basename plus an image extension, so its audio file is the same stem with
    an audio extension. The album's own tracks follow, in name order.
    """
    out, seen = [], set()
    stem = os.path.splitext(os.path.basename(str(file or "")))[0] if file else ""
    if stem:
        for ext in LIB_AUDIO_EXTS:
            p = os.path.join(album, stem + ext)
            if os.path.isfile(p):
                out.append(p)
                seen.add(os.path.normcase(p))
    try:
        names = sorted(n for n in os.listdir(album)
                       if os.path.splitext(n)[1].lower() in LIB_AUDIO_EXTS)
    except OSError:
        names = []
    for n in names:
        p = os.path.join(album, n)
        if os.path.normcase(p) not in seen:
            out.append(p)
    return out


def embedded_cover_bytes(album, file=None):
    """(bytes, ctype, etag) for the album's EMBEDDED cover art, cached, or None.

    The last resort BEHIND `cover_path`/`cover_bytes`, for the case those
    cannot answer while the UI still has a track to draw: the art lives in the
    audio files (the library's `embed_covers` policy allows either way), the
    import's cover step has not written the sidecar yet, or the folder is being
    filled right now. A song whose own art is inside it beats a blank slot, and
    it can never mask a sidecar, because callers ask for this ONLY after
    `cover_bytes` found no file at all.

    Cached on the AUDIO file's stat, so a re-tagged file is a miss and a file
    whose picture was stripped stops answering it. The cache distinguishes the
    two reasons a file yields no picture: a file that was READ and carries none
    is a fact about that file (cached; the file's own stat expires it), while a
    file that could not be read at all (`AudioFile.audio is None` — a lock, a
    denied share) caches NOTHING, so the next ask reads it again instead of
    remembering a failure.
    """
    for path in _cover_audio_candidates(album, file):
        key = _stat_key(path)
        with _lock:
            hit = _embedded_cache.get(key)
            if hit is not None:
                _embedded_cache.move_to_end(key)
        if hit is None:
            try:
                af = AudioFile(path)
            except Exception:
                continue                      # not even openable: next track
            if af.audio is None:
                continue                      # unreadable ≠ pictureless
            try:
                pics = af.embedded_pictures() or []
            except Exception:
                pics = []
            hit = ()
            for mime, blob in pics:
                if blob:
                    blob = bytes(blob)
                    hit = (blob, _image_ctype(blob, mime))
                    break
            with _lock:
                _embedded_cache[key] = hit
                _embedded_cache.move_to_end(key)
                while len(_embedded_cache) > _COVER_MAX:
                    _embedded_cache.popitem(last=False)
        if hit:
            data, ctype = hit
            return data, ctype, hashlib.md5(data).hexdigest()
    return None, None, None


def cover_color(album, file=None):
    """Dominant cover color as '#rrggbb' (used for UI tinting), cached."""
    p = cover_path(album, file)
    if p is None:
        return None
    key = _stat_key(p)
    with _lock:
        hit = _color_cache.get(key)
        if hit is not None:
            _color_cache.move_to_end(key)
            return hit
    data, _, _ = cover_bytes(album, file)
    if not data:
        return None
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img = img.resize((1, 1))
        r, g, b = img.getpixel((0, 0))
        color = "#%02x%02x%02x" % (r, g, b)
    except Exception:
        return None
    with _lock:
        _color_cache[key] = color
        _color_cache.move_to_end(key)
        while len(_color_cache) > _COVER_MAX:
            _color_cache.popitem(last=False)
    return color