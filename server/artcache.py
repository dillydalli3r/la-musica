"""Provider artwork, fetched by the app and cached on disk.

Recommendation rows, the cover finder and the artist-image picker are handed
artwork that lives on a provider's CDN. The browser hotlinking those URLs is
what this module exists to stop: on some networks the CDNs refuse the browser
(`cdn-images.dzcdn.net` answers 403 to every header combination the app can
send, `upload.wikimedia.org` blocks the client outright), so the user is left
looking at empty placeholders even though the API that produced the URL
answers fine.

So every remote cover is served through `/api/art` (see `server/main.py`):
the app fetches it, caches it, and — when the provider refuses us — asks a
provider that does answer (Cover Art Archive by release-group MBID, then
iTunes, then Deezer). Each answer is cached under THE URL THAT ANSWERED, so
the second view of that row is instant and offline, and no cache entry ever
holds a picture other than the one its own URL serves. `substitute=False` is
what a cover WRITE asks for (see `server.api_cover._cover_url_bytes`): the picked
picture, or nothing — never a different cover standing in for it.

Nothing here raises: a total failure is `(None, None, None)` and the UI keeps
its own placeholder.

The module also owns the second, STAT-keyed cache in the same data dir: the
LOCAL covers an album's own file is served as, shrunk to the width a surface
draws it (`cover_thumb`, reached through `GET /api/cover?w=`). That one exists
for latency, not for a refusing CDN — a 74 px bar thumb must not fetch a
1200 px master.
"""
import hashlib
import json
import os
import tempfile
import threading
import time

import httpx

from server import httpclient
from server import integrations as intg

# --------------------------------------------------------------------------- #
# Trust boundary: only these providers may be reached through the route.
# --------------------------------------------------------------------------- #
# Without a list `/api/art?url=…` would relay ANY address the server can
# reach — an open proxy, and an SSRF hole into whatever network it sits on.
# Matched by suffix, so "cdn-images.dzcdn.net" is covered by "dzcdn.net" and
# "ia801234.us.archive.org" by "archive.org".
ART_HOSTS = (
    "dzcdn.net",                    # Deezer covers
    "mzstatic.com",                 # Apple/iTunes artwork
    "itunes.apple.com",             # …and Apple's own image host (mvod.…)
    "coverartarchive.org",          # Cover Art Archive
    "archive.org",                  # …and the item host its images live on
    "wikimedia.org",                # Wikipedia/Wikidata artwork
    "lastfm.freetls.fastly.net",    # Last.fm images
    "lastfm-img2.akamaized.net",
    "theaudiodb.com",
    # The cover finder renders rows from EVERY enabled source, so each source's
    # own image CDN has to be reachable too — otherwise a result thumb would
    # 404 through the proxy where it used to render straight from the CDN.
    "qobuz.com",                    # static.qobuz.com
    "tidal.com",                    # resources.tidal.com
    "bcbits.com",                   # Bandcamp
    "scdn.co",                      # Spotify
    "sndcdn.com",                   # SoundCloud
    "discogs.com",                  # i.discogs.com
    "fanart.tv",                    # fanart.tv
    "media-amazon.com",             # Amazon Music
)

# A desktop browser's own string. The music CDNs 403 an unknown/bot agent, and
# Deezer additionally wants a same-site Referer.
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Per-host request headers — ONE table, first suffix match wins. The split is
# not cosmetic: Wikimedia and MusicBrainz publish a policy that REQUIRES a
# contact user agent (and block generic ones), while the commercial CDNs are
# the other way round and answer a contact agent with 403.
HEADER_RULES = (
    ("dzcdn.net", {"User-Agent": _BROWSER_UA, "Referer": "https://www.deezer.com/",
                   "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}),
    ("mzstatic.com", {"User-Agent": _BROWSER_UA,
                      "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}),
    ("lastfm.freetls.fastly.net", {"User-Agent": _BROWSER_UA,
                                   "Referer": "https://www.last.fm/",
                                   "Accept": "image/*,*/*;q=0.8"}),
    ("lastfm-img", {"User-Agent": _BROWSER_UA, "Referer": "https://www.last.fm/",
                    "Accept": "image/*,*/*;q=0.8"}),
    ("coverartarchive.org", {"User-Agent": intg.USER_AGENT}),
    ("archive.org", {"User-Agent": intg.USER_AGENT}),
    ("wikimedia.org", {"User-Agent": intg.USER_AGENT}),
    ("theaudiodb.com", {"User-Agent": intg.USER_AGENT}),
)

# Covers never change meaningfully, so a month is the app's usual slow-data TTL
# (the same one the genre/Apple caches use).
CACHE_TTL = 30 * 86400.0
# What a sidecar says the entry it belongs to holds. An entry holds THE BYTES
# THE URL ITSELF ANSWERED WITH — never a fallback provider's — and this stamp
# is what makes that true across the format's own history: an entry written
# when a fallback's image could be stored under the asked-for URL is not read,
# so a cover somebody else's search poisoned stops being served.
_CACHE_VERSION = 2
# A total failure is remembered for minutes only: enough that a shelf full of
# the same broken row makes ONE round of requests, short enough that a provider
# coming back is picked up without a restart.
FAIL_TTL = 300.0
# ponytail: fixed 300 MB with an oldest-first prune, same shape as the thumb
# cache; make it a config knob only if a library ever needs a bigger one.
_CACHE_BYTES = 300 * 1024 * 1024
_CACHE_KEEP = 0.8

# url key -> failure expiry. Process-local on purpose: a restart is a fine time
# to try a provider again.
_FAILS: dict = {}

# Bytes the cache dir holds, per directory, maintained from what _write() puts
# there and from the last authoritative scan. A full scan costs a listdir +
# stat of EVERY entry (176 ms measured on a 3000-image cache) and used to run
# after every single download: fetching 200 covers spent 35 s re-measuring a
# cache that had grown by 200 files. None means "never measured here" — the
# first write pays one scan, every later write a dict lookup.
_CACHE_TOTALS: dict = {}

_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF8", "image/gif"),
)


def _data_subdir(name, music_folder=None):
    """`<music>/.mlo/data/<name>` — the app's own state dir for covers."""
    if music_folder is None:
        try:
            from mlo.config import load_config

            music_folder = load_config().get("music_folder") or None
        except Exception:
            music_folder = None
    try:
        from mlo.paths import app_data_dir

        d = app_data_dir(music_folder)
    except Exception:
        d = None
    return os.path.join(d, name) if d else None


def cache_dir(music_folder=None):
    """`<music>/.mlo/data/art_cache` — provider artwork, keyed by its URL."""
    return _data_subdir("art_cache", music_folder)


def thumb_dir(music_folder=None):
    """`<music>/.mlo/data/cover_thumbs` — LOCAL covers shrunk to a surface's
    drawn width (see `cover_thumb`), keyed by the cover file's stat."""
    return _data_subdir("cover_thumbs", music_folder)


def allowed(url):
    """Whether *url* points at one of the allowlisted providers."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in ART_HOSTS)


def headers_for(url):
    """The header set to send to *url*'s host (see HEADER_RULES)."""
    from urllib.parse import urlparse

    host = (urlparse(str(url or "")).hostname or "").lower()
    for suffix, headers in HEADER_RULES:
        if host == suffix or host.endswith("." + suffix):
            return dict(headers)
    return {"User-Agent": _BROWSER_UA}


def _key(url):
    return hashlib.sha1(str(url).encode("utf-8")).hexdigest()


def _sniff(data):
    """The content type read from the image's own first bytes, or ""."""
    for magic, ctype in _MAGIC:
        if data.startswith(magic):
            return ctype
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _ctype(raw, data):
    """The response's content type when it really is an image, else sniffed."""
    ct = str(raw or "").split(";")[0].strip().lower()
    return ct if ct.startswith("image/") else _sniff(data)


def _paths(d, key):
    return os.path.join(d, key + ".bin"), os.path.join(d, key + ".json")


def _read(key, d):
    """The cached (data, content_type, source) for *key*, or None.

    *key* is the hash of the URL whose bytes the entry holds, and an entry
    whose sidecar is not in the current format is not read at all: it may have
    been written before an entry was guaranteed to be that URL's own answer.
    """
    if not d:
        return None
    fp, meta = _paths(d, key)
    try:
        if time.time() - os.path.getmtime(fp) > CACHE_TTL:
            return None
        with open(fp, "rb") as fh:
            data = fh.read()
        side = {}
        try:
            with open(meta, "r", encoding="utf-8") as fh:
                side = json.load(fh)
        except (OSError, ValueError):
            side = {}
    except OSError:
        return None
    if not data or side.get("v") != _CACHE_VERSION:
        return None
    return data, side.get("content_type") or _sniff(data) or "image/jpeg", side.get("source") or "cache"


def _write(key, d, data, ctype, source, url):
    """Store one image (+ its content-type/source sidecar), atomically."""
    if not d:
        return
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return
    fp, meta = _paths(d, key)
    written = 0
    for path, blob in ((fp, data), (meta, json.dumps(
            {"v": _CACHE_VERSION, "content_type": ctype, "source": source,
             "url": url}).encode("utf-8"))):
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
            os.replace(tmp, path)
            written += len(blob)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
    # Only these bytes changed the cache's size, so the full scan is owed only
    # when they push it past the ceiling (the first write of the process
    # measures the directory once). A repeated URL over-counts — the replaced
    # file's old bytes are still counted — which prunes a little early, never
    # late, and the scan below resets the number to the truth.
    total = _CACHE_TOTALS.get(d)
    if total is None:
        total = _scan(d)[1]
    _CACHE_TOTALS[d] = total + written
    if _CACHE_TOTALS[d] > _CACHE_BYTES:
        _prune(d)


def _scan(d):
    """(entries, total bytes) of the cache dir — the authoritative measure."""
    entries, total = [], 0
    try:
        for name in os.listdir(d):
            if not name.endswith((".bin", ".json", ".part")):
                continue
            fp = os.path.join(d, name)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, fp))
            total += st.st_size
    except OSError:
        return entries, total
    return entries, total


def _prune(d):
    """Drop the oldest entries once the cache outgrows its ceiling."""
    entries, total = _scan(d)
    _CACHE_TOTALS[d] = total
    if total <= _CACHE_BYTES:
        return
    keep = _CACHE_BYTES * _CACHE_KEEP
    for _mtime, size, fp in sorted(entries):
        if total <= keep:
            break
        try:
            os.remove(fp)
            total -= size
        except OSError:
            pass
    _CACHE_TOTALS[d] = total


def _get(url, headers, timeout):
    """One image GET → ``(status, data, content_type)``; never raises.

    The one HTTP seam every fetch goes through (the tests replace it). The body
    is STREAMED with a cap: an oversized answer is a mistake or an attack, not
    a cover, and must not be held in memory. The client is the app's shared one
    (`server.httpclient`) — entering it as a context manager would close it for
    every later caller — and the redirect/timeout stay this request's own.
    """
    try:
        with httpclient.client().stream(
                "GET", url, headers=headers, follow_redirects=True,
                timeout=httpx.Timeout(timeout, read=timeout)) as r:
            if r.status_code != 200:
                return r.status_code, b"", ""
            raw = r.headers.get("content-type")
            chunks, size = [], 0
            for chunk in r.iter_bytes(65536):
                size += len(chunk)
                if size > intg.IMAGE_MAX_BYTES:
                    return r.status_code, b"", ""
                chunks.append(chunk)
    except Exception:
        return None, b"", ""
    data = b"".join(chunks)
    if not data:
        return 200, b"", ""
    ctype = _ctype(raw, data)
    # Not an image at all (some sources list a video as a cover): refuse it, so
    # the fallback tier can answer with real artwork instead of the UI
    # rendering a broken image.
    return (200, data, ctype) if ctype.startswith("image/") else (200, b"", "")


def _caa_front(rg_mbid, size=1200):
    """Cover Art Archive's front cover for a RELEASE GROUP at *size* px.

    The ONE caller is the framework album's PLACEHOLDER cover
    (`server.pending_albums.write_placeholder_cover`): the album's own art, by
    identity, which R201 keeps. It is deliberately NOT a fallback tier any more
    — see `_fallback_candidates`.
    """
    rg = str(rg_mbid or "").strip()
    return "%s/release-group/%s/front-%d" % (intg.CAA_BASE, rg, size) if rg else ""


def _itunes_art_url(artist, album, cfg, timeout):
    """Apple's 3000×3000 artwork for artist+album, or "".

    The largest iTunes serves, and the one swap that always works (see
    `integrations._artwork_big`). Reached through the app's throttled, cached
    iTunes client like every other Apple call.
    """
    try:
        rows = intg._itunes_covers(artist, album, 1, cfg, timeout)
    except Exception:
        return ""
    return (rows[0].get("big") or rows[0].get("small") or "") if rows else ""


def _deezer_art_url(artist, album, cfg, timeout):
    """Deezer's biggest cover for artist+album, or ""."""
    try:
        rows = intg._deezer_covers(artist, album, 1, timeout)
    except Exception:
        return ""
    return (rows[0].get("big") or rows[0].get("small") or "") if rows else ""


def _fallback_candidates(artist, album, rg, cfg, timeout):
    """(url, source) pairs to try after the original URL failed — LAZILY.

    Each tier only touches the network when every tier before it has already
    failed, so a working iTunes costs Deezer nothing.

    The Cover Art Archive is deliberately NOT a tier any more: a cover WRITE is
    exact (it must store the picture it was given, not the archive's), and a
    display fallback that served one album's release-group art in place of a
    row whose own URL refused is exactly the wrong-picture bug this module
    exists to prevent. The remaining tiers are ALBUM lookups
    (`/search/album`, entity=album), so they are asked only when there is an
    album to ask about. Asked with an artist alone they answer with whatever
    that artist's most popular release is — a DIFFERENT album, which is the one
    thing this module must never hand a caller that cannot tell it apart from
    what it asked for. A cover with no album name has no fallback tier at all,
    and a write (`substitute=False`) may reach none of them.
    """
    if album:
        two = (("itunes", _itunes_art_url), ("deezer", _deezer_art_url))
        for source, find in two:
            url = find(artist, album, cfg, timeout)
            if url:
                yield url, source


def _same_artwork(url):
    """One more ``(url, source)`` for the SAME picture, or nothing.

    Apple answers a storefront URL with an empty body when that copy of the
    asset is not there while the same artwork's `image/thumb` transform is
    (`integrations._artwork_big`). Trying it is not a substitution — it cannot
    be another cover, only the picture the caller asked about at the largest
    size Apple serves — which is why this tier runs before any provider
    fallback and is the ONLY one a cover write is allowed.
    """
    bigger = intg._artwork_big(url)
    if bigger and bigger != url:
        yield bigger, "applemusic"


def _candidates(url, artist, album, rg, cfg, timeout, substitute=True):
    """The URLs to try, in order: the one asked about, the same artwork's
    largest copy, and — only when substituting is allowed — the providers that
    answer when that picture cannot be had at all."""
    yield url, "url"
    yield from _same_artwork(url)
    if substitute:
        yield from _fallback_candidates(artist, album, rg, cfg, timeout)


def fetch_art(url, *, artist="", album="", release_group_mbid="", cfg=None,
              timeout=20.0, substitute=True):
    """Bytes, content type and source for one provider artwork URL.

    `source` names who actually answered: "url" for the URL asked about, the
    same picture's largest copy for an Apple URL whose own copy is missing
    ("applemusic"), a fallback id ("itunes" or "deezer") otherwise. A URL that
    is not on the allowlist, and a total failure, both return
    ``(None, None, None)``.

    Every entry is written under THE URL THAT ANSWERED, so a cache hit is
    always that URL's own bytes: a fallback's image can never be served for the
    URL it stood in for, whoever asks for that URL next (another album's
    finder, another album's cover write) and with whatever identity. The
    fallback's own answer is still cached, so the next view of the row that
    needed it costs no request.

    `substitute=False` tries the URL and the same picture's largest copy and
    NOTHING else. A caller that must not write a different picture than the one
    it was given — a cover the user picked, a cover the policy chose for an
    album — gets nothing rather than another provider's image.
    """
    url = str(url or "").strip()
    if not allowed(url):
        return None, None, None
    key = _key(url)
    d = cache_dir()
    hit = _read(key, d)
    if hit:
        return hit
    if _FAILS.get(key, 0.0) > time.time():
        return None, None, None

    artist = str(artist or "").strip()
    album = str(album or "").strip()
    for candidate, source in _candidates(url, artist, album, release_group_mbid,
                                         cfg, timeout, substitute):
        if not allowed(candidate):
            continue
        if candidate != url:
            cached = _read(_key(candidate), d)
            if cached:
                return cached
        status, data, ctype = _get(candidate, headers_for(candidate), timeout)
        if data:
            _write(_key(candidate), d, data, ctype, source, candidate)
            _FAILS.pop(key, None)
            return data, ctype, source
    if substitute:
        # Every tier this call may ask was asked and nothing answered: remember
        # that for minutes, so a shelf full of the same broken row makes ONE
        # round of requests. An EXACT fetch (`substitute=False`) is not
        # remembered — it asks about one URL and nothing else, and a later
        # DISPLAY of that row may still be answered by the provider tiers this
        # call was not allowed to reach.
        _FAILS[key] = time.time() + FAIL_TTL
    return None, None, None


# --------------------------------------------------------------------------- #
# Local covers, shrunk to the width a surface actually draws.
#
# The provider cache above is keyed by a URL. This one is keyed by the cover
# FILE's own stat, because that is what changes when a cover is replaced in
# place (cover.jpg stays cover.jpg, so a URL-keyed entry would go on serving
# the previous picture). Every surface that draws a cover smaller than the
# master — the player bar's 74 px thumb, the fullscreen picture and its
# blurred ambient layer, the queue/track rows — used to pull the whole file
# (a 1200-3000 px JPEG, 0.3-3 MB) and let the browser shrink it: megabytes of
# transfer and an extra full-size decode between "press play" and the artwork
# appearing. A request now asks for one of THUMB_SIZES and gets bytes that
# size, encoded ONCE and then read from disk by every later request, every
# other surface and the next server run.
# --------------------------------------------------------------------------- #
# The widths a caller may ask for. Bucketed on purpose: an arbitrary `w` per
# call site would put a new file in the cache for every pixel count the UI
# ever computes, and lose the sharing between surfaces that is half the point.
THUMB_SIZES = (160, 320, 640, 1200)
# Thumbnails are drawn small and often; 88 is the app's own "invisible at this
# size" JPEG quality (the library-write path keeps its own, higher one).
THUMB_QUALITY = 88
# How long a browser may keep a sized cover WITHOUT asking again. A cover is
# replaced in place, so this is the window in which a stale thumb could still
# be shown — five minutes, and the client that did the write is not even in it:
# the URL carries the version token the write reported (`api.coverUrl`'s `v`),
# so its own surfaces fetch the new picture immediately. The point of the
# window is the opposite end: the surfaces that show the SAME album again
# (the next track, the reopened fullscreen pane, the queue row) must not spend
# a round trip between "press play" and the artwork.
THUMB_MAX_AGE = 300


def thumb_width(w):
    """*w* snapped up to the next THUMB_SIZES step (0 = no thumb asked)."""
    try:
        w = int(w or 0)
    except (TypeError, ValueError):
        return 0
    if w <= 0:
        return 0
    for size in THUMB_SIZES:
        if w <= size:
            return size
    return THUMB_SIZES[-1]


def _thumb_key(path, w, st):
    return hashlib.sha1(
        ("thumb|%s|%d|%d|%d" % (os.path.normcase(path), getattr(st, "st_mtime_ns", 0),
                                getattr(st, "st_size", 0), w)).encode("utf-8")).hexdigest()


def _encode_thumb(fp, w):
    """(bytes, ctype) for *fp* at *w* px, or ``(None, None)``.

    JPEG is decoded through Pillow's `draft()` first, which for a JPEG is the
    difference between decoding 1400×1400 to shrink it and decoding the 350×350
    the shrink is going to keep anyway. An image with transparency stays a PNG
    (the bar draws the thumb over `bg-raise`); everything else becomes a
    progressive JPEG.
    """
    import io

    from PIL import Image

    try:
        img = Image.open(fp)
        try:
            orientation = img.getexif().get(0x0112)          # 274 = Orientation
        except Exception:
            orientation = None
        if orientation and orientation != 1:
            # The browser applies EXIF orientation to the MASTER (`image-
            # orientation: from-image`), so a thumbnail that ignored it would
            # draw the cover on its side the moment the surface started asking
            # for one. `draft` is skipped here: the transpose decodes anyway.
            from PIL import ImageOps

            img = ImageOps.exif_transpose(img)
        else:
            img.draft("RGB", (w, w))
        img.load()
        img.thumbnail((w, w), Image.LANCZOS)
        buf = io.BytesIO()
        # An image with transparency stays a PNG — the bar draws the thumb over
        # `bg-raise`, and `convert("RGB")` would paint those pixels black.
        # `info["transparency"]` is only how a PALETTE image carries it; an
        # RGBA/LA image has it in the mode.
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            img.convert("RGBA").save(buf, "PNG", optimize=True)
            return buf.getvalue(), "image/png"
        img.convert("RGB").save(buf, "JPEG", quality=THUMB_QUALITY,
                                optimize=True, progressive=True)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return None, None


def _source_size(fp):
    """(width, height) of the image in *fp* without decoding it, or None."""
    try:
        from PIL import Image

        with Image.open(fp) as img:
            return img.size
    except Exception:
        return None


def cover_thumb(path, w):
    """*path* at the bucketed width *w*: ``(bytes, ctype, etag)``, or None.

    None means "there is no thumbnail here, serve the master" — the file
    cannot be decoded at all (a ``cover.jxl`` on a Pillow without the plugin):
    a cover must never fail because the shrink did. A file already at or below
    *w* is served as ITS OWN bytes (no upscale, no re-encode, no second cache
    entry — a 700 px cover asked for at 640 is still that 700 px cover).

    The etag is the hash of the bytes actually returned, and every call after
    the first reads them from this function's disk cache: the key is the
    cover file's stat, so a replaced cover is a different entry rather than a
    stale hit.
    """
    import hashlib

    w = thumb_width(w)
    if not w:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None

    d = thumb_dir()
    key = _thumb_key(path, w, st)
    hit = _read(key, d)
    if hit:
        return hit[0], hit[1], hashlib.md5(hit[0]).hexdigest()

    size = _source_size(path)
    if size is None:
        return None
    if max(size) <= w:
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            return None
        if not raw:
            return None
        return raw, _ctype(None, raw) or "image/jpeg", hashlib.md5(raw).hexdigest()

    blob, tctype = _encode_thumb(path, w)
    if not blob:
        return None
    _write(key, d, blob, tctype, "thumb", path)
    return blob, tctype, hashlib.md5(blob).hexdigest()


# --------------------------------------------------------------------------- #
# Warming the row thumbnails a library page is about to ask for.
#
# `/api/library` answers in milliseconds, but the cover requests it implies
# only START once React has committed the rows and the browser has laid them
# out: measured on a 23-album scratch library, the data ends at ~200 ms and the
# first `?w=160` request starts at ~295 ms — ~95 ms of a page whose text has
# already painted. A cold library then pays one decode per album on the
# client's own (low) priority and bounded concurrency, and the visible first
# screen waits for the tail of that queue.
#
# The sized-thumb cache (`cover_thumb`) exists exactly so those requests are a
# disk read — so it is filled HERE, in the background, the moment the library
# tree is up, at the width a row draws. Bounded twice over so a huge library
# degrades to "warm as much as is cheap": a cap on albums per pass and a wall
# budget for the pass. A thumb already on disk is skipped (the pass is
# idempotent across restarts), and covers too small to be shrunk are skipped
# too — `cover_thumb` serves those as their own bytes and never writes an
# entry, so asking would only re-read the master every start.
# --------------------------------------------------------------------------- #
# The width a track/album ROW draws — web/src/components/CoverImg.tsx's
# ROW_COVER_W. The row's cover and the player bar's thumb are the SAME request
# at this width (see THUMB_SIZES' note), which is what makes warming it warm
# both surfaces.
WARM_WIDTH = 160
# …and the width the LIBRARY page's default view asks. `/library` opens in the
# grid (`lib/libraryView.ts`) at the stored cover size, whose default is "m" =
# 320 (`AlbumCard.GRID_COVER_W`); the list/table view's rows ask 160. Both
# surfaces are one visit apart, both are small thumbs, so the pass fills both —
# measured 23 albums × 2 widths ≈ 40 ms. A single width here would leave
# whichever view the user does NOT open cold on their first visit.
WARM_WIDTHS = (WARM_WIDTH, 320)
# Albums per pass. A cold first screen asks for the first ~30 rows and a bit of
# scrolling; 500 covers the visible library comfortably, and every later pass
# skips what a former one wrote, so a bigger library warms a prefix of itself
# per start rather than walking itself into a stall.
WARM_CAP = 500
# Wall-clock ceiling for one pass, belt beside the cap above: on a slow (or
# network-mounted) library even the cap's decodes add up, and the warm thread
# must never be the thing holding a restart or a shutdown.
WARM_BUDGET_S = 20.0


def thumb_path(path, w):
    """Where the thumb for the cover FILE *path* at bucketed width *w* lives,
    or None when there can be no thumb (no width, or the file is gone).

    `cover_thumb`'s own destination, split out so the warmer can ask "is this
    already encoded?" with one stat and never decode to find out. It MUST stay
    the same key `cover_thumb` writes — both derive it from the file's stat
    (`_thumb_key`) under `thumb_dir()`.
    """
    w = thumb_width(w)
    if not w:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return os.path.join(thumb_dir(), _thumb_key(path, w, st) + ".bin")


def _warm_pairs(rows):
    """(album folder, cover file) for every album row in *rows*.

    *rows* is whatever is cheap to enumerate: the dict
    `server.library.build_library` returns (`artists` → `albums`), an iterable
    of such album ROWS, or an iterable of (folder, file) pairs. A row with no
    cover file is skipped — the client draws no cover for it either
    (`CoverImg` renders the placeholder when `coverFile` is falsy).
    """
    if isinstance(rows, dict):
        rows = (alb for ar in rows.get("artists", []) or []
                for alb in ar.get("albums", []) or [])
    for row in rows:
        if isinstance(row, dict):
            folder, file = row.get("path"), row.get("cover_file")
        else:
            folder, file = row[0], row[1]
        if folder:
            yield str(folder), (str(file) if file else None)


def warm_thumbs(tree, w=WARM_WIDTH, cap=WARM_CAP, workers=None, budget=WARM_BUDGET_S):
    """Pre-encode the row/grid thumbs for *tree*, in the background.

    *tree* is a `server.library.build_library` tree, an iterable of album rows,
    or an iterable of (folder, file) pairs (see `_warm_pairs`). *w* is one
    bucketed width or an iterable of them — the surfaces a library page draws
    ask for more than one (rows 160, the default grid 320), and warming a
    width nobody asks for is the same as warming nothing. Blocking — the CALLER
    owns the thread (server.main runs it on its own daemon thread after the
    library tree is up); this function itself is what makes the pass bounded,
    never the caller.

    Returns a summary dict:

      ``warmed``    thumbs this pass newly encoded and wrote to disk
      ``skipped``   covers that need no thumb (already here, no cover, or a
                    master at/below the asked width, served as its own bytes)
      ``failed``    covers that could not be decoded at all
      ``capped``    (cover, width) pairs this pass did not reach — over *cap*
                    or past *budget*
      ``seconds``   wall time the pass took

    Concurrency is `mlo.stats.worker_count` — the one pool-width knob, so a
    user's "Worker threads" setting bounds this pass too — capped at 8 lanes
    (the work is a small decode; more lanes only fight over one disk).
    """
    import time as _time
    from concurrent.futures import ThreadPoolExecutor

    from mlo.stats import worker_count

    start = _time.monotonic()
    deadline = start + max(0.0, float(budget or 0)) if budget else None
    try:
        widths = [thumb_width(x) for x in w]          # a sequence of widths
    except TypeError:
        widths = [thumb_width(w)]
    widths = [x for x in widths if x] or [0]
    jobs, seen = [], set()
    for folder, file in _warm_pairs(tree):
        for width in widths:
            key = (os.path.normcase(os.path.join(folder, file or "")), width)
            if key in seen:
                continue
            seen.add(key)
            jobs.append((folder, file, width))
    cap = max(1, int(cap or 1))
    capped = max(0, len(jobs) - cap)
    jobs = jobs[:cap]

    stats = {"warmed": 0, "skipped": 0, "failed": 0, "capped": capped,
             "seconds": 0.0}
    if not widths[0]:
        stats["skipped"] = len(jobs)
        stats["seconds"] = round(_time.monotonic() - start, 3)
        return stats
    lock = threading.Lock()

    def _one(job):
        folder, file, width = job
        if deadline and _time.monotonic() > deadline:
            with lock:
                stats["capped"] += 1
            return
        try:
            from server import tagcache
            p = tagcache.cover_path(folder, file)
        except Exception:
            p = None
        if not p:
            with lock:
                stats["skipped"] += 1
            return
        dest = thumb_path(p, width)
        if dest and os.path.exists(dest):
            with lock:
                stats["skipped"] += 1
            return
        size = _source_size(p)
        if size is None:
            with lock:
                stats["failed"] += 1
            return
        if max(size) <= width:
            # Served as its own bytes, no cache entry (`cover_thumb`).
            with lock:
                stats["skipped"] += 1
            return
        thumb = cover_thumb(p, width)
        with lock:
            if thumb is None:
                stats["failed"] += 1
            elif dest and os.path.exists(dest):
                stats["warmed"] += 1
            else:
                stats["skipped"] += 1

    if jobs:
        lanes = workers or worker_count(None, maximum=min(8, os.cpu_count() or 1),
                                        items=len(jobs))
        with ThreadPoolExecutor(max_workers=max(1, int(lanes))) as pool:
            list(pool.map(_one, jobs))

    stats["seconds"] = round(_time.monotonic() - start, 3)
    return stats
