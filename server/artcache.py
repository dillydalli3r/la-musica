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
what a cover WRITE asks for (see `server.main._cover_url_bytes`): the picked
picture, or nothing — never a different cover standing in for it.

Nothing here raises: a total failure is `(None, None, None)` and the UI keeps
its own placeholder.
"""
import hashlib
import json
import os
import tempfile
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


def cache_dir(music_folder=None):
    """`<music>/.mlo/data/art_cache` — the app's own state dir for covers."""
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
    return os.path.join(d, "art_cache") if d else None


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
    """Cover Art Archive's front cover for a RELEASE GROUP at *size* px."""
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
    failed, so a working Cover Art Archive costs Apple and Deezer nothing.

    The last two tiers are ALBUM lookups (`/search/album`, entity=album), so
    they are asked only when there is an album to ask about. Asked with an
    artist alone they answer with whatever that artist's most popular release
    is — a DIFFERENT album, which is the one thing this module must never
    hand a caller that cannot tell it apart from what it asked for. A cover
    with no album name has no album tier to fall back to; the Cover Art
    Archive's release-group tier above is the one that answers by identity, and
    only a display path may reach either (a write passes `substitute=False`).
    """
    if rg:
        yield _caa_front(rg), "coverartarchive"
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
    ("applemusic"), a fallback id ("coverartarchive", "itunes", "deezer")
    otherwise. A URL that is not on the allowlist, and a total failure, both
    return ``(None, None, None)``.

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
