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
iTunes, then Deezer). The winner is cached under the ORIGINAL key, so the
second view of that row is instant and offline.

Nothing here raises: a total failure is `(None, None, None)` and the UI keeps
its own placeholder.
"""
import hashlib
import json
import os
import tempfile
import time

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
    """The cached (data, content_type, source) for *key*, or None."""
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
    if not data:
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
            {"content_type": ctype, "source": source, "url": url}).encode("utf-8"))):
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
    a cover, and must not be held in memory.
    """
    import httpx

    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, read=timeout),
                          follow_redirects=True) as client:
            with client.stream("GET", url, headers=headers) as r:
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
    is — a DIFFERENT album, which is the one thing a cover fetch must never
    return: a row whose own URL failed would then be replaced by another
    album's artwork, cached under the row's URL for a month and written to the
    library by the next "Use this cover". A cover with no album name has no
    album tier to fall back to; the Cover Art Archive's release-group tier
    above is the one that answers by identity.
    """
    if rg:
        yield _caa_front(rg), "coverartarchive"
    if album:
        two = (("itunes", _itunes_art_url), ("deezer", _deezer_art_url))
        for source, find in two:
            url = find(artist, album, cfg, timeout)
            if url:
                yield url, source


def _candidates(url, artist, album, rg, cfg, timeout):
    """The original URL first, then the fallback tiers — LAZILY."""
    yield url, "url"
    yield from _fallback_candidates(artist, album, rg, cfg, timeout)


def fetch_art(url, *, artist="", album="", release_group_mbid="", cfg=None,
              timeout=20.0):
    """Bytes, content type and source for one provider artwork URL.

    `source` names who actually answered: "url" for the URL asked about, a
    fallback id ("coverartarchive", "itunes", "deezer") otherwise — the winner
    is cached under the original key, so the caller never learns of the swap on
    the next view. A URL that is not on the allowlist, and a total failure,
    both return ``(None, None, None)``.
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
                                         cfg, timeout):
        if not allowed(candidate):
            continue
        status, data, ctype = _get(candidate, headers_for(candidate), timeout)
        if data:
            _write(key, d, data, ctype, source, url)
            _FAILS.pop(key, None)
            return data, ctype, source
    _FAILS[key] = time.time() + FAIL_TTL
    return None, None, None
