"""MusicBrainz / LRCLIB / RateYourMusic integrations for the import wizard.

MusicBrainz is queried with proper rate limiting (1 req/s) and a UA string
per their API etiquette. RYM has no public API — links are user-supplied
URLs stored as tags, but we validate/parse them here.
"""
import asyncio
import contextlib
from datetime import datetime, timezone
import html as _html
import json
import os
import random
import re
import threading
import time
import unicodedata
import uuid

import httpx

MB_BASE = "https://musicbrainz.org/ws/2"
LRCLIB_BASE = "https://lrclib.net/api"
USER_AGENT = "la-musica/2.0 (https://github.com/dillydalli3r/la-musica)"

_last_request = 0.0
_mb_lock = threading.Lock()


class MusicBrainzError(RuntimeError):
    """MusicBrainz did not answer, after its retries (429/5xx/connection).

    Typed so a caller can report it as a PER-ITEM reason — "MusicBrainz is
    busy, try again" — instead of a request or a job that sits on an outage.
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


_MB_MIN_INTERVAL = 1.0     # MusicBrainz etiquette: one request per second
_MB_RETRY_STATUS = (429, 500, 502, 503, 504)
_MB_BACKOFF_BASE = 1.0     # retry wait: base * 2**(attempt-1), plus jitter
_MB_BACKOFF_MAX = 8.0
# One mb_get — throttle, retries and backoff included — never runs longer than
# this. A 503ing MusicBrainz used to hold its caller (and, for an auto-import
# job, the single-job slot) for as long as the retry loop felt like it.
MB_RETRY_DEADLINE = 45.0


def mb_get(endpoint, params=None, timeout=30.0, retries=3, deadline=MB_RETRY_DEADLINE):
    """Rate-limited MusicBrainz WS/2 GET returning parsed JSON.

    Retries 429/5xx and connection errors with exponential backoff + jitter,
    keeping MusicBrainz's 1 request/second etiquette and a hard overall
    `deadline`; when the retry budget or the deadline runs out it raises
    MusicBrainzError — a typed, reportable reason. Any other status still
    raises httpx's own error, so a 404 keeps meaning "no such entity".
    """
    global _last_request
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    started = time.time()
    attempts = 0
    status = None
    reason = "no response"
    while attempts < max(1, int(retries)):
        attempts += 1
        retry_after = None
        with _mb_lock:
            elapsed = time.time() - _last_request
            if elapsed < _MB_MIN_INTERVAL:
                time.sleep(min(_MB_MIN_INTERVAL - elapsed,
                               max(0.0, deadline - (time.time() - started))))
            try:
                r = httpx.get(
                    f"{MB_BASE}/{endpoint}",
                    params=params or {},
                    headers=headers,
                    # never sit on the socket past the deadline either
                    timeout=min(timeout, max(1.0, deadline - (time.time() - started))),
                )
            except httpx.HTTPError as e:
                status, reason = None, f"connection error ({e.__class__.__name__})"
            else:
                if r.status_code not in _MB_RETRY_STATUS:
                    r.raise_for_status()
                    return r.json()
                status, reason = r.status_code, f"HTTP {r.status_code}"
                retry_after = r.headers.get("Retry-After")
            _last_request = time.time()
        if attempts >= retries:
            break
        wait = min(_MB_BACKOFF_BASE * (2 ** (attempts - 1)), _MB_BACKOFF_MAX)
        wait += random.uniform(0.0, wait / 2.0)   # jitter: retries must not sync
        if retry_after:
            try:
                wait = max(wait, float(retry_after))
            except ValueError:
                pass
        if time.time() - started + wait > deadline:
            break
        time.sleep(wait)
    raise MusicBrainzError(
        f"MusicBrainz is busy ({reason}) for {endpoint} after {attempts} "
        f"attempt(s) in {int(time.time() - started)}s — try again in a moment",
        status=status,
    )


# --------------------------------------------------------------------------- #
# Tiny TTL cache for browse endpoints (release/artist pages get re-fetched on
# every navigation; MB etiquette caps us at 1 req/s, so repeat views must not
# re-hit the network). Keyed by (endpoint, sorted params), 10-minute TTL.
# --------------------------------------------------------------------------- #
_BROWSE_CACHE: dict = {}
_BROWSE_LOCK = threading.Lock()
_INFLIGHT: dict = {}
# MB data moves slowly; an hour of TTL keeps repeat views instant (and well
# within the 1 req/s etiquette) without serving anything meaningfully stale.
_BROWSE_TTL = 1800.0


def mb_get_cached(endpoint, params=None, timeout=30.0, retries=5):
    """Cached MB GET with stale-while-revalidate and single-flight.

    A fresh cached copy returns instantly. A stale copy ALSO returns
    instantly while a background thread refreshes it — only genuinely
    unknown payloads block on the rate-limited network, and identical
    concurrent requests share one flight instead of queueing several
    1-second-spaced calls."""
    key = (endpoint, tuple(sorted((k, str(v)) for k, v in (params or {}).items())))
    now = time.time()
    with _BROWSE_LOCK:
        hit = _BROWSE_CACHE.get(key)
        flight = _INFLIGHT.get(key)
    if hit and now - hit[0] < _BROWSE_TTL:
        return hit[1]
    if hit and flight is None:
        # serve stale, refresh in the background
        def _refresh():
            try:
                data = mb_get(endpoint, params, timeout=timeout, retries=retries)
                with _BROWSE_LOCK:
                    _BROWSE_CACHE[key] = (time.time(), data)
            except Exception:
                pass  # keep the stale copy on refresh failure
            finally:
                with _BROWSE_LOCK:
                    ev = _INFLIGHT.pop(key, None)
                if ev is not None:
                    ev.set()  # wake waiters — they read the cache we just wrote

        with _BROWSE_LOCK:
            if _INFLIGHT.get(key) is None:
                _INFLIGHT[key] = threading.Event()
            else:
                return hit[1]  # someone else is already refreshing
        threading.Thread(target=_refresh, daemon=True).start()
        return hit[1]
    if flight is not None:
        # an identical request is already on the wire — wait for it instead
        # of queueing a second rate-limited call behind it
        flight.wait(timeout=45)
        with _BROWSE_LOCK:
            hit = _BROWSE_CACHE.get(key)
        if hit:
            return hit[1]
        raise RuntimeError("concurrent MusicBrainz request did not complete")
    with _BROWSE_LOCK:
        flight = _INFLIGHT.get(key)
        if flight is None:
            flight = _INFLIGHT[key] = threading.Event()
    try:
        data = mb_get(endpoint, params, timeout=timeout, retries=retries)
        with _BROWSE_LOCK:
            # Write the cache BEFORE waking waiters: a woken reader that
            # found an empty cache would 500 a fetch that actually worked.
            _BROWSE_CACHE[key] = (time.time(), data)
            # keep the cache from growing without bound
            if len(_BROWSE_CACHE) > 600:
                for k in list(_BROWSE_CACHE)[:200]:
                    _BROWSE_CACHE.pop(k, None)
    finally:
        with _BROWSE_LOCK:
            _INFLIGHT.pop(key, None)
        flight.set()
    return data


def detect_mbid(mbid):
    """Which MusicBrainz entity kind does this MBID belong to?

    Tries a minimal lookup per browsable entity (cache-shared with the
    entity pages) and reports the first hit — lets the UI route a pasted
    bare ID without the user picking a type."""
    for entity in MB_ENTITIES:
        try:
            data = mb_get_cached(f"{entity}/{mbid}", {"fmt": "json"})
        except Exception:
            continue
        return {
            "type": entity,
            "id": data.get("id") or mbid,
            "title": data.get("title") or data.get("name") or "",
        }
    raise LookupError("no MusicBrainz entity found for this ID")


def _browse_collect(endpoint, extra_params, list_key, count_key, limit=300, offset=0):
    """Browse rows across MusicBrainz's 100-per-request pages.

    Browse has NO server-side sort, so a single arbitrary 100-row slice
    misrepresents a discography (one page can be all albums, the next all
    singles) and any date ordering would be a lie. This walks the pages
    (still 1 req/s) up to `limit` rows starting at `offset` so the caller
    can sort and filter over an honest window. Returns (rows, total)."""
    items = []
    pos = offset
    total = None
    while pos < offset + limit:
        data = mb_get_cached(
            endpoint,
            {**extra_params, "limit": min(100, offset + limit - pos), "offset": pos, "fmt": "json"},
        )
        batch = data.get(list_key) or []
        total = data.get(count_key) or total
        items.extend(batch)
        pos += len(batch)
        if not batch or pos >= min(total or 0, offset + limit):
            break
    if total is None:
        total = len(items)
    return items, total


def _mbid(value):
    """Extract a MusicBrainz ID from an ID or a musicbrainz.org URL."""
    if not value:
        return None
    m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value, re.I)
    return m.group(0).lower() if m else None


def _genres(node):
    return [g["name"] for g in (node.get("genres") or [])]


def _title_genres(node):
    """Genres in Title Case ('nu metal' -> 'Nu Metal') for tag import."""
    seen, out = set(), []
    for g in _genres(node):
        key = g.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(g.strip().title())
    return out


# --------------------------------------------------------------------------- #
# Release lookups
# --------------------------------------------------------------------------- #
def release_lookup(mbid):
    """Full release: media/discs, recordings, artist credits, genres and
    labels (catalog numbers). Country comes from the release entity."""
    data = mb_get(
        f"release/{mbid}",
        {"inc": "artists+recordings+media+release-groups+artist-credits+genres+labels+isrcs", "fmt": "json"},
    )
    # Normalize media into a flat list of {disc, position, title, length, recording mbid, artist mbids}
    tracks = []
    for medium in data.get("media", []):
        disc = medium.get("position", 1)
        for trk in medium.get("tracks", []):
            rec = trk.get("recording", {})
            artists = []
            for ac in trk.get("artist-credit", []):
                if "artist" in ac:
                    artists.append({
                        "name": ac.get("name", ""),
                        "mbid": ac["artist"].get("id"),
                    })
            tracks.append({
                "position": trk.get("position"),
                "disc": disc,
                "title": trk.get("title"),
                "length": trk.get("length"),
                "recording_mbid": rec.get("id"),
                "artist_mbids": [a["mbid"] for a in artists],
                "artist_credit": "".join(
                    (ac.get("name", "") + (ac.get("joinphrase", "") or ""))
                    for ac in trk.get("artist-credit", [])
                ),
                "genres": _title_genres(rec),
                # ISRCs come free with this request (inc=isrcs) and are what
                # the advisory fetch looks a track up by.
                "isrcs": [v for v in (_isrc(i) for i in (rec.get("isrcs") or [])) if v],
            })
    release_artists = [
        {"name": ac.get("name", ""), "mbid": ac["artist"].get("id")}
        for ac in data.get("artist-credit", []) if "artist" in ac
    ]
    rg_obj = data.get("release-group") or {}
    primary = (rg_obj.get("primary-type") or "").lower()
    secondary = [s.lower() for s in (rg_obj.get("secondary-types") or [])]
    release_type = "+".join([primary] + secondary) if primary else ""

    # labels -> label name + every catalog number. A release can carry several
    # (one per label/pressing) and auto-import searches each as its OWN query,
    # so keeping only the first lost every other pressing's number. Order is
    # MusicBrainz's own; blanks and duplicates dropped. `catalog_number` stays
    # the first one for the callers that only ever wanted one.
    label_name = ""
    catalog_numbers = []
    for lab in data.get("label-info", []) or []:
        if not label_name:
            label_name = str(((lab.get("label") or {}).get("name")) or "").strip()
        cn = (lab.get("catalog-number") or "").strip()
        if cn and cn not in catalog_numbers:
            catalog_numbers.append(cn)
    catalog_number = catalog_numbers[0] if catalog_numbers else ""
    country = data.get("country") or ""

    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "date": (data.get("date") or ""),
        # the release-group's first-release-date — the "original" release
        # date shown next to this specific release's own date
        "originaldate": (rg_obj.get("first-release-date") or ""),
        "barcode": (data.get("barcode") or ""),
        "country": country,
        # Status (Official/Promotion/Bootleg/…) + the medium of the first
        # medium — the release page meta line shows both, and the auto-import
        # policy keys on them.
        "status": data.get("status") or "",
        "medium": next((m.get("format") or "" for m in data.get("media", [])), ""),
        "catalog_number": catalog_number,
        # All of them, in MusicBrainz order — the auto-import search expands
        # this into one query per number.
        "catalog_numbers": catalog_numbers,
        "label": label_name,
        "release_group_id": (data.get("release-group") or {}).get("id"),
        # `release_type` keeps its historical lowercase "+"-joined spelling —
        # it is written into the RELEASETYPE tag and drives the naming script
        # and grading. The structured pair below is for display: MusicBrainz
        # splits release types into primary (Album/EP/Single/Broadcast/Other)
        # and secondary (Soundtrack/Live/Compilation/Remix/Demo/...).
        "release_type": release_type,
        "primary_type": rg_obj.get("primary-type") or "",
        "secondary_types": [s for s in (rg_obj.get("secondary-types") or [])],
        "artists": release_artists,
        "genres": _title_genres(data),
        "media": tracks,
        "medium_count": len(data.get("media", [])),
        "medium_formats": [m.get("format") or "" for m in data.get("media", [])],
    }


def recording_isrcs(recording_mbid):
    """ISRCs MusicBrainz holds for a recording (usually exactly one)."""
    if not recording_mbid:
        return []
    try:
        data = mb_get_cached(f"recording/{recording_mbid}",
                             {"inc": "isrcs", "fmt": "json"})
    except Exception:
        return []
    return [v for v in (_isrc(i) for i in (data.get("isrcs") or [])) if v]


# --------------------------------------------------------------------------- #
# Content advisory (ITUNESADVISORY): 0 = not explicit, 1 = explicit, 2 = safe.
# --------------------------------------------------------------------------- #
# EVERY applicable source is asked for EVERY track — one pass, no short
# circuit — and the answers are collected per source and MERGED by a single
# documented rule (`merge_advisory`), so a source that states "not explicit"
# is heard even after another one already said "explicit", and the caller can
# show what each source said:
#
#   deezer-isrc    api.deezer.com/track/isrc:<ISRC>      (verified working)
#   spotify-isrc   Spotify search by ISRC                (needs client id+secret)
#   apple-album    iTunes artist → its albums → THE EXPLICIT EDITION →
#                  lookup?entity=song                    (verified working)
#   itunes-song    iTunes song search, exact title only  (last resort)
#   discogs-parental  Discogs edition format/description (token only, WEAK,
#                  album level — explicit-only, ranks last)
#   youtube-age    yt-dlp `age_limit` >= 18 for the track's OWN video id
#                  (explicit-only, last, never a blind per-track search)
#
# ISRC path: the two ISRC sources are asked for every ISRC the track has —
# the file's own tag, or every ISRC MusicBrainz holds for its recording.
# APPLE HAS NO ISRC LOOKUP, which is why it is reached by artist → album
# edition instead; it is still cross-referenced with both ISRC sources.
#
# VERIFIED, on this machine: Apple's own `lookup?isrc=` and `lookup?upc=`
# endpoints answer resultCount 0 — Apple does not serve identity lookups, so
# Apple is reached through the artist's album list (or the exact-title song
# search). APPLE'S ALBUM *SEARCH* IS NOT USABLE FOR THIS: `search?entity=album`
# answers ONE edition — for System Of A Down's "Steal This Album!" the CLEANED
# one — whose tracks then read `cleaned`/`notExplicit` over the whole album,
# i.e. an explicitly clean re-release of an explicit master. The artist route
# (`lookup?id=<artistId>&entity=album`) lists every edition with its own
# `collectionExplicitness`, so the explicit edition is the one whose tracks are
# read, and the cleaned edition is only a cross-check.
#
# Deezer's ISRC endpoint is per-track and needs no title guessing at all.
# Spotify is optional and never load-bearing; it is skipped entirely until
# `spotify_client_id`/`spotify_client_secret` are set in Settings.
#
# Honesty rules, which every route below obeys:
#   * a route only ever returns an answer it can attribute to itself, and an
#     answer it cannot state is NO answer (never a guess, never a default);
#   * a `cleaned` Apple entry (or a `Clean` contentAdvisoryRating) is a CLEANED
#     EDITION, states nothing about the original master and is NO ANSWER — it
#     must never become 0 or 2;
#   * a name-based match never accepts a variant of the track, and a variant
#     never accepts the original (`title_matches`);
#   * the merged value is 0 when nobody stated anything at all: an unstated
#     advisory is written as "not explicit", the user's policy, and the
#     `answers` map is what says whether any source actually spoke.
# Every value a route may report as `source`. A caller may only write a value
# it can attribute to one of these.
ADVISORY_SOURCES = frozenset({"deezer-isrc", "spotify-isrc", "apple-album",
                              "itunes-song", "discogs-parental",
                              "youtube-age"})

_ITUNES_LOOKUP = "https://itunes.apple.com"
_DEEZER_TRACK_ISRC = "https://api.deezer.com/track/isrc:"
_SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SPOTIFY_SEARCH = "https://api.spotify.com/v1/search"
# Album titles are compared with difflib below this ratio = not this release.
_APPLE_ALBUM_SIMILARITY = 0.6
# iTunes rate-limits hard: hammering it earns an EMPTY body (not JSON), and a
# library pass re-reads the same artist and the same album's tracks over and
# over. Calls are spaced out AND cached — in memory by the transport, on disk
# below so a restart does not re-ask what Apple already told us.
_APPLE_MIN_INTERVAL = 3.0
_APPLE_CACHE_TTL = 30 * 86400.0
_APPLE_CACHE_NAME = "apple_cache"
_apple_lock = threading.Lock()
_apple_last = 0.0

# One answer per (source, identity). A library pass asks each track once;
# `None` (asked, nobody stated a value) is cached too, so a rerun that found
# nothing does not re-hit the APIs either. Bounded — it is a memo, not a store.
_ADVISORY_CACHE: dict = {}
_ADVISORY_LOCK = threading.Lock()
_ADVISORY_MISS = object()
_SPOTIFY_TOKEN: dict = {}
_ADVISORY_CACHE_MAX = 20000


def _advisory_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _advisory_json(url, params=None, headers=None, timeout=None, host=None):
    """The one HTTP seam every advisory route goes through: discovery's
    per-host-throttled, TTL-cached JSON client, so a library pass is polite
    and a repeat ask costs no request. None on any failure."""
    from server import discovery
    return discovery._json(url, params, headers=headers, timeout=timeout, host=host)


def _advisory_post(url, data=None, headers=None, timeout=None):
    """POST sibling of `_advisory_json` (only Spotify's token endpoint needs
    one). None on any failure."""
    try:
        r = httpx.post(url, data=data, headers=headers, timeout=timeout or 15.0)
        if r.status_code >= 400:
            return None
        return r.json()
    except Exception:
        return None


def _advisory_cached(key, producer):
    """Memoized `producer()` — answer or None, cached either way."""
    with _ADVISORY_LOCK:
        hit = _ADVISORY_CACHE.get(key)
    if hit is not None:
        return None if hit is _ADVISORY_MISS else hit
    answer = producer()
    with _ADVISORY_LOCK:
        if len(_ADVISORY_CACHE) >= _ADVISORY_CACHE_MAX:
            _ADVISORY_CACHE.clear()
        _ADVISORY_CACHE[key] = _ADVISORY_MISS if answer is None else answer
    return answer


def _deezer_advisory(isrc, timeout=None):
    """(value, source) for one track from Deezer's ISRC lookup, or None.

    Deezer states two things and both are read:
      * `explicit_content_lyrics` 1 (explicit lyrics) or 2 (explicit content /
        artwork), or `explicit_lyrics: true` → 1. BOTH are explicit statements:
        this app's 2 means "safe" (mlo/autotag.py), so mapping an explicit flag
        to 2 would relabel an explicit track as safe.
      * `explicit_lyrics: false` (whatever the content flag says) or
        `explicit_content_lyrics: 0` → 0 — Deezer is stating the track is not
        explicit.
      * anything else (the unclassified value Deezer reports as 3) is NO
        ANSWER, and never 0.
    """
    code = str(isrc or "").strip()
    if not code:
        return None
    data = _advisory_json(_DEEZER_TRACK_ISRC + code, timeout=timeout,
                          host="api.deezer.com")
    if not isinstance(data, dict) or data.get("error"):
        return None
    content = _advisory_int(data.get("explicit_content_lyrics"))
    lyrics = data.get("explicit_lyrics")
    if content in (1, 2) or lyrics is True:
        return (1, "deezer-isrc")
    if content == 0 or lyrics is False:
        return (0, "deezer-isrc")
    return None


def _spotify_token(cfg, timeout=None):
    """Client-credentials token, or None when Spotify is unconfigured/down.

    `spotify_client_id` + `spotify_client_secret` are optional; without them
    this returns None and the route is skipped, never defaulted.
    """
    cid = str((cfg or {}).get("spotify_client_id") or "").strip()
    secret = str((cfg or {}).get("spotify_client_secret") or "").strip()
    if not cid or not secret:
        return None
    with _ADVISORY_LOCK:
        token = _SPOTIFY_TOKEN.get("token")
        if token and _SPOTIFY_TOKEN.get("expires", 0) > time.time():
            return token
    import base64
    body = _advisory_post(
        _SPOTIFY_TOKEN_URL,
        data={"grant_type": "client_credentials"},
        headers={"Authorization": "Basic " + base64.b64encode(
            f"{cid}:{secret}".encode("utf-8")).decode("ascii"),
            "Content-Type": "application/x-www-form-urlencoded"},
        timeout=timeout)
    token = (body or {}).get("access_token")
    if not token:
        return None
    with _ADVISORY_LOCK:
        _SPOTIFY_TOKEN["token"] = token
        _SPOTIFY_TOKEN["expires"] = (
            time.time() + float((body or {}).get("expires_in") or 3600) - 60)
    return token


def _spotify_advisory(isrc, cfg, timeout=None):
    """(value, source) from Spotify's ISRC search, or None.

    Only a hit whose own `external_ids.isrc` is the ISRC we asked about
    counts — a search hit is a candidate, not an identity.
    """
    code = str(isrc or "").strip()
    token = _spotify_token(cfg, timeout=timeout) if code else None
    if not token:
        return None
    data = _advisory_json(
        _SPOTIFY_SEARCH,
        {"q": f"isrc:{code}", "type": "track", "limit": 1},
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout, host="api.spotify.com")
    for item in (((data or {}).get("tracks") or {}).get("items") or []):
        got = str((item.get("external_ids") or {}).get("isrc") or "").strip()
        if got.upper() != code.upper():
            continue
        return (1 if item.get("explicit") else 0, "spotify-isrc")
    return None


def _data_cache_dir(name):
    """<music>/.mlo/data/<name> — the app's folder-state dir for *name*."""
    from mlo.paths import app_data_dir
    music = ""
    try:
        from mlo.config import load_config
        music = str(load_config().get("music_folder") or "")
    except Exception:
        music = ""
    d = app_data_dir(music or None) or ""
    return os.path.join(d, name) if d else None


def _apple_cache_dir():
    """Disk cache for the artist→albums and album→tracks payloads."""
    return _data_cache_dir(_APPLE_CACHE_NAME)


def _apple_json(path, params, timeout=None):
    """One iTunes GET: ~1 per _APPLE_MIN_INTERVAL, disk-cached for a month.

    Apple rate-limits hard — rapid repeats answer an EMPTY body, not JSON —
    and these payloads (an artist's whole album list, one album's tracks) are
    what a library pass re-reads for every file. None on any failure; this
    never raises.
    """
    global _apple_last
    import hashlib
    from urllib.parse import urlencode
    key = hashlib.sha1((path + "?" + urlencode(
        sorted((str(k), str(v)) for k, v in (params or {}).items())
    )).encode("utf-8")).hexdigest()
    d = _apple_cache_dir()
    fp = os.path.join(d, key + ".json") if d else None
    if fp:
        try:
            if (os.path.isfile(fp)
                    and time.time() - os.path.getmtime(fp) < _APPLE_CACHE_TTL):
                with open(fp, encoding="utf-8") as fh:
                    return json.load(fh)
        except (OSError, ValueError):
            pass
    with _apple_lock:
        wait = _APPLE_MIN_INTERVAL - (time.time() - _apple_last)
        if wait > 0:
            time.sleep(wait)
        _apple_last = time.time()
        data = _advisory_json(f"{_ITUNES_LOOKUP}{path}", dict(params or {}),
                              timeout=timeout, host="itunes.apple.com")
    if data is None or not fp:
        return data
    try:
        os.makedirs(d, exist_ok=True)
        tmp = fp + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh)
        os.replace(tmp, fp)
    except OSError:
        pass
    return data


def _apple_country(cfg=None):
    """The storefront Apple is asked about — the app's region setting."""
    return (str((cfg or {}).get("cover_country") or "").strip().lower()
            or "us")


def _apple_artist_id(artist, timeout=None):
    """Apple's artist id for a name, or None.

    Only the artist route lists EVERY edition of a release, so this is where
    the advisory route starts.
    """
    name = str(artist or "").strip()
    if not name:
        return None
    data = _apple_json("/search", {"term": name, "entity": "musicArtist",
                                   "limit": 5}, timeout=timeout)
    want = _norm_compare(name)
    for row in (data or {}).get("results") or []:
        if _norm_compare(row.get("artistName")) == want:
            return _advisory_int(row.get("artistId"))
    return None


def _apple_editions(artist, album, track_count=None, cfg=None, timeout=None):
    """Apple's editions of one album, the EXPLICIT edition first.

    `lookup?id=<artistId>&entity=album` lists every edition Apple holds, each
    with its own `collectionExplicitness`. An edition counts as this album
    when its normalized `collectionName` matches — a `trackCount` match only
    corroborates (it sorts such an edition up, and lets a near-identical title
    through). Every failure — no artist, no editions, a rate-limited empty
    body — returns [].
    """
    artist_id = _apple_artist_id(artist, timeout=timeout)
    if not artist_id:
        return []
    data = _apple_json("/lookup",
                       {"id": artist_id, "entity": "album", "limit": 200,
                        "country": _apple_country(cfg)}, timeout=timeout)
    want_artist = _norm_compare(artist)
    want_album = _norm_compare(album)
    want_count = _advisory_int(track_count)
    rows = []
    for row in (data or {}).get("results") or []:
        cid = _advisory_int(row.get("collectionId"))
        if not cid:
            continue
        got_artist = _norm_compare(row.get("artistName"))
        if want_artist and got_artist and want_artist not in got_artist:
            continue
        name = _norm_compare(row.get("collectionName"))
        # An instrumental/karaoke edition of the album is not the album: its
        # tracks would rate the wrong recording, so a variant mismatch is out
        # for both the name match and the similarity fallback below.
        kind = title_variant_kind(album)
        got_kind = title_variant_kind(row.get("collectionName"))
        if (kind or got_kind) and kind != got_kind:
            continue
        count_ok = bool(want_count
                        and _advisory_int(row.get("trackCount")) == want_count)
        named = bool(want_album and name == want_album)
        if not (named or (count_ok and _similarity(
                album, str(row.get("collectionName") or ""))
                >= _APPLE_ALBUM_SIMILARITY)):
            continue
        explicit = str(row.get("collectionExplicitness") or "").strip().lower()
        rows.append((explicit == "explicit", count_ok, named, row))
    rows.sort(key=lambda r: (r[0], r[1], r[2]), reverse=True)
    return [row for *_rank, row in rows]


def _apple_value(item):
    """Apple's track payload → 1/0/None.

    `trackExplicitness` is the field that matters. `cleaned` is a CLEANED
    EDITION — an edited master — and `Clean` in `contentAdvisoryRating` marks
    the same edition; neither states anything about the original, so both are
    NO ANSWER. Without that guard the clean re-release of an explicit album
    would quietly relabel every track safe.
    """
    exp = str(item.get("trackExplicitness") or "").strip().lower()
    rating = str(item.get("contentAdvisoryRating") or "").strip().lower()
    if exp == "cleaned" or rating == "clean":
        return None
    if exp == "explicit" or rating == "explicit":
        return 1
    if exp == "notexplicit":
        return 0
    return None


def _apple_collection_value(cid, title="", disc=None, track=None,
                            positions_ok=True, timeout=None, cfg=None):
    """The advisory Apple states for the file inside one collection.

    The file is mapped to its track by discNumber/trackNumber (the position
    decides — another track's rating is not this file's), falling back to a
    title match that refuses variants (`title_matches`): an instrumental or
    karaoke version of the track sits in the same album and is a different
    recording. `positions_ok=False` disables the position mapping entirely:
    that collection does not hold the album's track count, so the same
    disc/track number may be a different song and only a title match may be
    trusted.

    Returns None when the matched track is `cleaned` or carries no rating:
    that collection simply cannot answer, and the CALLER may still ask the
    album's other editions.
    """
    songs = _apple_json("/lookup",
                        {"id": cid, "entity": "song", "limit": 200,
                         "country": _apple_country(cfg)}, timeout=timeout)
    tracks = [item for item in (songs or {}).get("results") or []
              if str(item.get("wrapperType") or "").lower() == "track"]
    want_disc, want_track = _advisory_int(disc), _advisory_int(track)
    if positions_ok and want_disc is not None and want_track is not None:
        for item in tracks:
            if (_advisory_int(item.get("discNumber")) == want_disc
                    and _advisory_int(item.get("trackNumber")) == want_track):
                return _apple_value(item)
    if str(title or "").strip():
        for item in tracks:
            if not title_matches(title, item.get("trackName")):
                continue
            value = _apple_value(item)
            if value is not None:
                return value
            break   # the title matched a `cleaned` entry — that is the answer
    return None


def _apple_album_advisory(artist, album, title="", disc=None, track=None,
                          track_count=None, timeout=None, cfg=None):
    """(value, source) from the album's editions on Apple, or None.

    The long way round, on purpose: artist search → artist id → the artist's
    album list → the EXPLICIT edition first (`_apple_editions`), then the
    cleaned / other editions as a cross-check. Apple's album *search* answers
    one edition only — often the cleaned one, whose tracks then read
    `cleaned`/`notExplicit` even where the master is explicit.

    A `cleaned` match never produces a value — it only proves that THIS
    edition cannot rate the track — so the next candidate is tried (bounded to
    three track lookups) before giving up. Candidates whose track count is
    known and differs are title-matched only, because position alone would
    then be matching a different song.
    """
    artist = str(artist or "").strip()
    album = str(album or "").strip()
    if not artist or not album:
        return None
    want_count = _advisory_int(track_count)
    for row in _apple_editions(artist, album, track_count, cfg,
                               timeout=timeout)[:3]:
        cid = _advisory_int(row.get("collectionId"))
        if not cid:
            continue
        count_ok = bool(want_count
                        and _advisory_int(row.get("trackCount")) == want_count)
        value = _apple_collection_value(
            cid, title, disc, track,
            positions_ok=want_count is None or count_ok,
            timeout=timeout, cfg=cfg)
        if value is not None:
            return (value, "apple-album")
    return None


def _norm_compare(text):
    """Comparison key for titles/artists.

    discovery's own normalizer, deliberately: the app must not grow a second
    opinion about when two titles are the same string.
    """
    from server import discovery
    return discovery.norm(text)


# --------------------------------------------------------------------------- #
# Title variants: an instrumental / karaoke / demo / cover / tribute version
# is NOT the track itself. A per-track value may never be taken from one of
# them (`title_variant_kind`), and a file whose own name says it is a variant
# may never take the original's data. `title_matches` is that guard, and every
# name-based match in this module goes through it.
# --------------------------------------------------------------------------- #
# (kind, pattern) — first hit wins; checked in this order on purpose, so
# "(Karaoke Instrumental Version)" reads instrumental.
_VARIANT_MARKERS = (
    ("instrumental", r"\(\s*instrumental\s*\)"),
    ("instrumental", r"\binstrumental\s+version\b"),
    ("instrumental", r"^\s*instrumental\s*[-–—:]"),
    ("karaoke", r"\bkaraoke\b"),
    ("karaoke", r"\bbacking\s+track\b"),
    ("demo", r"\(\s*demo\s*\)"),
    ("cover", r"\bcover\s+version\b"),
    ("tribute", r"\btribute\b"),
    ("tribute", r"\bmade\s+famous\s+by\b"),
    ("tribute", r"\bin\s+the\s+style\s+of\b"),
)


def title_variant_kind(title):
    """None, or which kind of variant this title says it is.

    "karaoke", "instrumental", "demo", "cover" or "tribute" — the markers are
    the ones releases actually carry: `(Instrumental)`, `Instrumental
    Version`, a leading `Instrumental -`/`Karaoke -`, `Karaoke`, `Backing
    Track`, `(Demo)`, `Cover Version`, `Tribute`, `Made Famous By`, `In The
    Style Of`. Anything else (including a plain title) is None.
    """
    text = str(title or "")
    for kind, pattern in _VARIANT_MARKERS:
        if re.search(pattern, text, re.I):
            return kind
    return None


def title_matches(query_title, candidate_title, *, allow_variant=False):
    """True when `candidate_title` is the same title as `query_title`.

    Normalized comparison (discovery's normalizer), with the variant guard on
    top: a candidate that is an instrumental/karaoke/… version of the query is
    NOT the query, and a variant query does not accept the original either —
    unless `allow_variant`, which ignores the guard and compares names only.
    """
    query = _norm_compare(query_title)
    candidate = _norm_compare(candidate_title)
    if not query or not candidate:
        return False
    if not allow_variant:
        want = title_variant_kind(query_title)
        got = title_variant_kind(candidate_title)
        if (want or got) and want != got:
            return False
    return query == candidate


def merge_advisory(answers):
    """The ONE merge rule for ITUNESADVISORY — `{source: 0|1}` → 0|1.

    The user's policy, in this order: if ANY source states explicit → 1; else
    if ANY source states clean → 0; else (nobody said anything) → 0 as well.
    The last two branches are the same value on purpose: an unstated advisory
    is written as "not explicit", and only the empty answer map tells a caller
    that no source actually spoke.
    """
    for value in (answers or {}).values():
        if _advisory_int(value) == 1:
            return 1
    return 0


def _winning_source(answers, value):
    """The first source (in ask order) whose answer is the merged `value`."""
    for source, answer in (answers or {}).items():
        if _advisory_int(answer) == value:
            return source
    return None


def _itunes_song_advisory(title, artist="", timeout=None):
    """(value, source) from Apple's song search — the LAST resort.

    A search hit is a candidate, not an identity, so a hit is only accepted
    when `title_matches` says it is the file's title AND its own artist credit
    contains the file's artist. `title_matches` is also what refuses an
    instrumental/karaoke/cover hit (and the reverse): search answers those
    generously, and a variant's rating is not this track's. A `cleaned` hit is
    never accepted (see `_apple_value`).
    """
    title = str(title or "").strip()
    if not title:
        return None
    data = _advisory_json(f"{_ITUNES_LOOKUP}/search",
                          {"term": " ".join(t for t in (artist, title) if t),
                           "entity": "song", "limit": 10}, timeout=timeout)
    want_artist = _norm_compare(artist)
    for item in (data or {}).get("results") or []:
        if not title_matches(title, item.get("trackName")):
            continue
        if want_artist and want_artist not in _norm_compare(item.get("artistName")):
            continue
        value = _apple_value(item)
        if value is not None:
            return (value, "itunes-song")
    return None


def _spotify_configured(cfg):
    cfg = cfg or {}
    return bool(str(cfg.get("spotify_client_id") or "").strip()
                and str(cfg.get("spotify_client_secret") or "").strip())


def _discogs_parental_advisory(artist, album, cfg=None):
    """(1, "discogs-parental") when Discogs lists a Parental Advisory format.

    Album-level and WEAK — a sticker on the edition Discogs matched, not a
    per-track statement — so it ranks LAST and can only ever add an explicit
    signal: it never clears a track it did not rate. Requires `discogs_token`,
    like every Discogs route; without one (or without a hit) it answers None.
    """
    from server import discovery

    if not str((cfg or {}).get("discogs_token") or "").strip():
        return None
    if not (artist and album):
        return None
    try:
        flagged = discovery.discogs_parental_advisory(artist, album, cfg)
    except Exception:
        return None
    return (1, "discogs-parental") if flagged else None


# The tags a track's own video origin is recorded in: the video pipeline
# writes SOURCE, and YOUTUBEID/VIDEOID are the explicit spellings.
_YOUTUBE_TAG_KEYS = ("YOUTUBEID", "YOUTUBE_ID", "VIDEOID", "SOURCE")
_YOUTUBE_ID_RX = re.compile(r"[A-Za-z0-9_-]{11}")


def youtube_video_id(tags):
    """The YouTube id a track's OWN tags record, or "".

    Only a recorded origin counts: an `11`-character id, or a URL carrying
    `v=`/`/shorts/`/`youtu.be/`. Nothing here searches YouTube — a search's
    first hit is not this track, and rating the wrong video is precisely the
    failure this rule exists to prevent.
    """
    if not isinstance(tags, dict):
        return ""
    for key in _YOUTUBE_TAG_KEYS:
        value = str(tags.get(key) or "").strip()
        if not value:
            continue
        if "youtube.com" in value or "youtu.be" in value:
            match = re.search(r"(?:v=|/shorts/|youtu\.be/)([A-Za-z0-9_-]{11})",
                              value)
            if match:
                return match.group(1)
            continue
        if _YOUTUBE_ID_RX.fullmatch(value):
            return value
    return ""


def youtube_age_advisory(video_id, cfg=None):
    """(1, "youtube-age") when YouTube itself flags the video 18+, else None.

    Only asked about a video the caller already knows belongs to this track
    (see `youtube_video_id`) or passes itself. `age_limit >= 18` states
    explicit; anything else — a normal video, an unavailable one, yt-dlp not
    installed, YouTube disabled — states NOTHING, because "no age gate" is not
    a statement about the music.
    """
    from server import youtube

    ident = str(video_id or "").strip()
    if not _YOUTUBE_ID_RX.fullmatch(ident):
        return None
    if not youtube._enabled(cfg):
        return None
    module = youtube._load_ytdlp()
    if module is None:
        return None
    try:
        with module.YoutubeDL(youtube._ydl_opts(cfg)) as ydl:
            info = ydl.extract_info(youtube._watch_url(ident), download=False)
    except Exception:
        return None
    if not isinstance(info, dict):
        return None
    try:
        limit = int(info.get("age_limit") or 0)
    except (TypeError, ValueError):
        return None
    return (1, "youtube-age") if limit >= 18 else None


def resolve_advisory_route(isrc="", recording_mbid="", title="", artist="",
                           album="", disc=None, track=None, track_count=None,
                           cfg=None, youtube_id="", tags=None):
    """{"value": 0|1, "source": key|None, "checked": [key, ...],
        "answers": {key: 0|1}}.

    EVERY applicable source is asked, in one pass, and none of them
    short-circuits:

      1. Deezer by ISRC, then 2. Spotify by ISRC — for EVERY ISRC the track
         has: the file's own tag plus every ISRC MusicBrainz holds for its
         recording (Apple serves no ISRC lookup, so it is reached by edition
         instead and is still cross-referenced with both);
      3. Apple's artist route — artist → its album list → the explicit
         edition → track (disc/track, then a title match);
      4. Apple's song search — title match only;
      5. Discogs' edition (`format`/`description` "Parental Advisory", token
         only) and 6. YouTube's 18+ gate for a video the track records — two
         LAST, explicit-only, album/edition-level signals that never clear a
         track and stay silent when their input is absent.

    `answers` is the per-source map in ask order, `source` the first source
    that stated the merged value, `checked` every route that was asked. The
    merge itself is `merge_advisory` — one rule, one place — so `value` is 0
    or 1 and an empty `answers` is the only signal that nobody stated
    anything.
    """
    if cfg is None:
        try:
            from mlo.config import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
    codes = []
    if str(isrc or "").strip():
        codes.append(str(isrc).strip())
    if recording_mbid:
        # The file's own ISRC is asked first, then every ISRC MusicBrainz
        # holds for the recording — both ISRC sources see all of them.
        for code in recording_isrcs(recording_mbid):
            if code.upper() not in {c.upper() for c in codes}:
                codes.append(code)
    spotify_on = _spotify_configured(cfg)
    checked = []
    answers = {}
    for code in codes:
        checked.append("deezer-isrc")
        answer = _advisory_cached(("deezer-isrc", code.upper()),
                                  lambda c=code: _deezer_advisory(c))
        if answer is not None:
            answers.setdefault(answer[1], answer[0])
        if spotify_on:
            checked.append("spotify-isrc")
            answer = _advisory_cached(("spotify-isrc", code.upper()),
                                      lambda c=code: _spotify_advisory(c, cfg))
            if answer is not None:
                answers.setdefault(answer[1], answer[0])
    if artist and album:
        checked.append("apple-album")
        key = ("apple-album", _norm_compare(artist), _norm_compare(album),
               _advisory_int(disc), _advisory_int(track), _norm_compare(title))
        answer = _advisory_cached(key, lambda: _apple_album_advisory(
            artist, album, title, disc, track, track_count, cfg=cfg))
        if answer is not None:
            answers.setdefault(answer[1], answer[0])
    if title:
        checked.append("itunes-song")
        key = ("itunes-song", _norm_compare(artist), _norm_compare(title))
        answer = _advisory_cached(key,
                                  lambda: _itunes_song_advisory(title, artist))
        if answer is not None:
            answers.setdefault(answer[1], answer[0])
    # The last two are extra EXPLICIT-only signals, both album/edition level
    # and both silent when their input is missing: a Discogs Parental
    # Advisory sticker (a configured token only), and YouTube's own 18+ gate
    # for a video the track already records. Neither can clear a track.
    if artist and album and str((cfg or {}).get("discogs_token") or "").strip():
        checked.append("discogs-parental")
        key = ("discogs-parental", _norm_compare(artist), _norm_compare(album))
        answer = _advisory_cached(
            key, lambda: _discogs_parental_advisory(artist, album, cfg))
        if answer is not None:
            answers.setdefault(answer[1], answer[0])
    video = str(youtube_id or "").strip() or youtube_video_id(tags)
    if video:
        checked.append("youtube-age")
        answer = _advisory_cached(("youtube-age", video),
                                  lambda: youtube_age_advisory(video, cfg))
        if answer is not None:
            answers.setdefault(answer[1], answer[0])
    value = merge_advisory(answers)
    return {"value": value, "source": _winning_source(answers, value),
            "checked": checked, "answers": answers}


def resolve_advisory(isrc="", recording_mbid="", **context):
    """ITUNESADVISORY (0/1) for one track — the merged answer of every source.

    Deezer by ISRC, Spotify by ISRC when configured, Apple's artist→album
    route and Apple's song search are all asked (see
    `resolve_advisory_route`), and the merged value is 1 when ANY of them says
    explicit, 0 otherwise (`merge_advisory`, the user's policy — an unstated
    advisory is a 0, not an absent tag).

    `context` may carry `title`, `artist`, `album`, `disc`, `track`,
    `track_count` and `cfg` (the Apple routes need them; the ISRC sources do
    not). MusicBrainz supplies the ISRCs when the caller has only a recording
    ID. Use `resolve_advisory_route` when the per-source answers matter too.
    """
    return resolve_advisory_route(isrc=isrc, recording_mbid=recording_mbid,
                                  **context)["value"]


def release_advisories(mbid, sources=None, answers=None):
    """{f'{disc}:{position}': 0|1} for every track of a release.

    Every track gets the merged value of the sources that were asked (0 when
    none of them stated anything — the merge rule, not a gap to fill in).
    `sources` (optional) is filled with the source that stated each value and
    `answers` (optional) with the whole per-track `{source: 0|1}` map, so a
    caller can report provenance alongside the value.
    """
    release = release_lookup(mbid)
    fallback_artist = next((a.get("name") for a in release.get("artists") or []
                            if a.get("name")), "")
    track_count = len(release.get("media") or [])
    out = {}
    for t in release.get("media") or []:
        key = f"{int(t.get('disc') or 1)}:{int(t.get('position') or 0)}"
        route = resolve_advisory_route(
            isrc=(t.get("isrcs") or [""])[0],
            recording_mbid=t.get("recording_mbid") or "",
            title=t.get("title") or "",
            artist=t.get("artist_credit") or fallback_artist,
            album=release.get("title") or "",
            disc=t.get("disc"),
            track=t.get("position"),
            track_count=track_count,
        )
        if route["value"] is not None:
            out[key] = route["value"]
            if sources is not None and route["source"]:
                sources[key] = route["source"]
            if answers is not None and route.get("answers"):
                answers[key] = route["answers"]
    return out


def release_group_genres(rg_mbid):
    try:
        data = mb_get(f"release-group/{rg_mbid}", {"inc": "genres", "fmt": "json"})
        return _genres(data)
    except Exception:
        return []


def artist_genres(artist_mbid):
    try:
        data = mb_get(f"artist/{artist_mbid}", {"inc": "genres", "fmt": "json"})
        return _genres(data)
    except Exception:
        return []


def genre_cascade(release, limit=None):
    """Cascading genre import: track -> release -> release-group -> artist.

    Genres are merged across levels (deduped, in popularity order, Title
    Case) and capped at `limit` per track. limit=None imports everything.
    Returns per-track genres plus the fallback chain used for each track.
    """
    rg = release.get("release_group_id")
    rg_genres = release_group_genres(rg) if rg else []
    artist_genres_all = []
    for a in release.get("artists", []):
        if a.get("mbid"):
            artist_genres_all.extend(artist_genres(a["mbid"]))
    artist_genres_all = list(dict.fromkeys(artist_genres_all))
    release_genres = release.get("genres", [])

    per_track = []
    for trk in release.get("media", []):
        ordered = []
        sources = []
        for level, lst in (
            ("track", trk.get("genres") or []),
            ("release", release_genres),
            ("release-group", rg_genres),
            ("artist", artist_genres_all),
        ):
            if lst and not sources:
                sources.append(level)
            for g in lst:
                if g not in ordered:
                    ordered.append(g)
        merged = ordered[:limit] if limit else ordered
        per_track.append({
            "position": trk["position"],
            "disc": trk["disc"],
            "title": trk["title"],
            "genres": merged,
            "source": sources[0] if sources else None,
            "levels_used": sources,
        })
    return {
        "per_track": per_track,
        "levels": {
            "track": any(t.get("genres") for t in release.get("media", [])),
            "release": bool(release_genres),
            "release_group": bool(rg_genres),
            "artist": bool(artist_genres_all),
        },
    }


# --------------------------------------------------------------------------- #
# RateYourMusic (scraped — no public API)
# --------------------------------------------------------------------------- #
# VERIFIED, on the machines this app has run on: rateyourmusic.com refuses an
# automated client outright — Cloudflare answers the TLS handshake/challenge
# and both urllib and curl get no usable page. A plain scraper therefore does
# NOT work from an unattended install and cannot be made to: this source is
# unverifiable from automated access, and it is kept FIRST in the genre chain
# only because the order is the user's own preference.
#
# What a user must do to make it work: paste the `Cookie` header of a
# logged-in rateyourmusic.com browser tab into Settings (`rym_cookie`); that
# cookie carries Cloudflare's cf_clearance for their IP/session, and with it
# the same requests below do return real pages. Without it — or behind a
# datacenter IP, where Cloudflare blocks regardless — this module sends
# browser-like headers, gets nothing, logs ONE line and the genre chain falls
# through to the next source. It never invents a genre from a partial page.
#
# A good cookie is not enough by itself, and that is the difference this
# module is built around: the WAF also hands out cookies of its OWN on a plain
# top-level navigation, and it refuses a cold client that never made one. So
# the paste seeds a cookie jar (`_rym_cookiejar`) that every request carries,
# the FIRST request under a paste is ONE warm-up navigation to RYM's home page
# (`_rym_warm`) whose Set-Cookie joins that jar, and every request sends the
# full Chrome header set (client hints and fetch metadata included) — see
# RYM_HEADERS and `_rym_headers`. None of it is a credential or a trick: it is
# what a browser does before a page it is allowed to read.
#
# Scraping is polite and cheap: one request per second, and a 30-day disk
# cache under <music>/.mlo/data/rym_cache so repeat imports never re-fetch.
# (Deezer and Apple, by contrast, are keyless public APIs and their advisory
# routes are verified working — see ADVISORY_SOURCES.)
#
# That ONE line is also the whole cost: a refusal latches the source off
# (`_rym_warned`), and every later request returns "no answer" without going
# out (see `_rym_get`), so a blocked RYM costs the import ONE probe instead
# of a walk through every slug candidate of every album. The latch is keyed
# to the credential that earned it and expires, though — see `_rym_blocked` —
# so a cookie the user has since replaced (or a block that lifted on its own)
# is asked again instead of looking blocked until the backend is restarted.
# And the LINKS the app tags from RYM come from MusicBrainz, which states the
# same pages as url relations — a blocked RYM no longer costs them at all
# (see "RYM link
# resolution" below).
RYM_BASE = "https://rateyourmusic.com"
# What a normal Chrome window sends, field for field. Everything here is a
# header a browser ALWAYS sends, so nothing in it is a claim the client cannot
# back up: a bare library UA — or the UA without the client hints, the fetch
# metadata and the wide Accept set that go with it — is what the WAF filters
# on, and a request that succeeds with a good cookie must not be refused for
# looking like an unattended scraper.
RYM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Referer": RYM_BASE + "/",
    "Upgrade-Insecure-Requests": "1",
    # Client hints. Chrome sends the same three on every request; the UA above
    # without them is a combination no real Chrome ever produces.
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", '
                 '"Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    # Fetch metadata: a document navigation, same-site (RYM is the Referer).
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
}
RYM_MIN_INTERVAL = 1.0        # seconds between requests, per their etiquette
RYM_CACHE_TTL = 30 * 86400.0  # genre data moves slowly
# A whole RYM lookup — the slug ladder included — never runs longer than this.
# One hung socket (each request carries a 20s timeout) or a long run of
# candidates must not hold an import behind a source that is not answering.
RYM_MAX_WALL = 20.0
# A refusal is a politeness guard for a run, never a verdict on the cookie:
# it stops standing after this long, so a session that went stale, a transient
# block, or a server that has been up for days all recover without a restart.
RYM_BLOCK_TTL = 300.0
# 429/5xx/timeout are transient — RYM is busy or rate-limiting, not refusing —
# so each gets a retry, spaced by the same 1 req/s as any other request. A
# Cloudflare interstitial is the refusal itself and is NOT retried.
RYM_RETRIES = 2
_rym_lock = threading.Lock()
_rym_last = 0.0
_rym_warned = False           # RYM refused since `_rym_blocked_at`, under
_rym_blocked_cookie = ""      # this cookie — the latch `_rym_get` reads via
_rym_blocked_at = 0.0         # `_rym_blocked` (the test harnesses reset these)
_rym_failures = 0             # how often RYM failed to answer at all
_rym_jar = None               # httpx.Cookies for `_rym_jar_paste`: the user's
_rym_jar_paste = None         # own pairs PLUS whatever RYM's Set-Cookie added
_rym_warmed = None            # the paste whose warm-up navigation has run
_rym_last_info = {}           # what RYM last answered — `rym_last_response()`
# Cloudflare's interstitial instead of a release page. Cached or parsed it
# would be an empty page at best, so it counts as unreachable.
_RYM_CHALLENGE_RE = re.compile(
    r"just a moment|cf-?challenge|_cf_chl|checking your browser|"
    r"enable javascript and cookies", re.I)
# RYM renders genres, styles and descriptors as /genre/<slug> anchors, in that
# order on a release page (primary genres first) — the anchors are the whole
# scrape, so a markup change degrades to "no genres", never to wrong ones.
_RYM_GENRE_RE = re.compile(r'href="/genre/([a-z0-9%\-]+)"[^>]*>([^<]{1,60})</a>', re.I)
# Descriptors ("Concept Album", "Death", "Lo-Fi") are RYM's other album-level
# classification. They are not genres, but they are the only classification a
# release page carries when it carries no /genre/ anchor, so they are read as
# a LAST-RESORT album-level answer and labelled as such in the provenance.
_RYM_DESCRIPTOR_RE = re.compile(
    r'href="/descriptor/([a-z0-9%\-]+)"[^>]*>([^<]{1,60})</a>', re.I)
# The track list: each row is a <tr class="tracklist_row">. A row that carries
# its own /genre/ anchor (some releases do tag a track) is that track's
# genre — everything else is album-level and says so.
_RYM_TRACK_ROW_RE = re.compile(r"<tr[^>]*tracklist_row[^>]*>(.*?)</tr>", re.I | re.S)
_RYM_TRACK_NUM_RE = re.compile(r"tracklist_track_num[^>]*>\s*(\d+)", re.I)
_RYM_TRACK_TITLE_RE = re.compile(
    r"tracklist_track_title[^>]*>(.*?)</t[dh]>", re.I | re.S)
_RYM_ARTIST_LINK_RE = re.compile(r'href="(/artist/[^"]+)"', re.I)


def _rym_labels(pattern, html):
    """Trimmed, de-duplicated anchor labels for one RYM link pattern."""
    out = []
    for _slug, label in pattern.findall(html or ""):
        name = re.sub(r"\s+", " ", label).strip()
        if name and name.lower() not in {g.lower() for g in out}:
            out.append(name)
    return out


def _rym_genres_from(html):
    """Genres (the /genre/ anchors) from a RYM release or artist page."""
    return _rym_labels(_RYM_GENRE_RE, html)


def _rym_tracks_from(html):
    """[{position, title, genres}] from a release page's track list.

    RYM states genres per RELEASE, not per track, so `genres` is normally
    empty and the release's own list is what applies (the caller marks that
    `level: album`). A row that does carry a /genre/ anchor is used for that
    row, mapped onto our tracks by position first and by title second — and a
    title that matches nothing is dropped rather than guessed at.
    """
    out = []
    for row in _RYM_TRACK_ROW_RE.findall(html or ""):
        match = _RYM_TRACK_TITLE_RE.search(row)
        if not match:
            continue
        title = re.sub(r"\s+", " ",
                       re.sub(r"<[^>]+>", "", match.group(1))).strip()
        if not title:
            continue
        num = _RYM_TRACK_NUM_RE.search(row)
        out.append({
            "position": int(num.group(1)) if num else len(out) + 1,
            "title": title,
            "genres": _rym_labels(_RYM_GENRE_RE, row),
        })
    return out


def _rym_cookie(cfg=None):
    """The user's `rym_cookie` — a browser session cookie, or "".

    Read from the live config on every request so pasting one into Settings
    takes effect without a restart. An empty cookie is not an error: it is
    the documented "RYM is skipped" state.

    The paste is normalised: people copy the value from wherever their
    browser shows it, so a leading "Cookie:" (the devtools row label), a
    wrapped line, or the newlines a terminal adds must not silently produce a
    header RYM refuses. Every separator is re-emitted as the single "; " the
    header grammar wants."""
    try:
        if cfg is None:
            from mlo.config import load_config
            cfg = load_config()
        raw = str((cfg or {}).get("rym_cookie") or "")
    except Exception:
        return ""
    raw = raw.replace("\r", "\n")
    if "\n" in raw:
        # A copied header wraps: keep the pairs, drop the line breaks.
        lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
        raw = "; ".join(ln.rstrip(";") for ln in lines)
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    pairs = [p.strip() for p in raw.split(";") if p.strip()]
    return "; ".join(pairs)


def _rym_cookiejar(cfg=None):
    """The cookie jar for the current paste: the user's pairs, plus every
    Set-Cookie RYM has answered with since (see `_rym_warm`).

    The credential travels in THIS jar rather than in a `Cookie:` header for
    one reason: the WAF's own cookies join it on the warm-up, and a header
    built once would never carry them. Rebuilt whenever the paste changes —
    the value the user pastes in Settings is the whole credential — so nothing
    from a refused cookie leaks into the next one."""
    global _rym_jar, _rym_jar_paste
    paste = _rym_cookie(cfg)
    if _rym_jar is None or _rym_jar_paste != paste:
        jar = httpx.Cookies()
        for pair in paste.split(";"):
            name, _, value = pair.strip().partition("=")
            if name:
                # RYM's own host: the paste is a rateyourmusic.com session and
                # must never be sent anywhere else by accident.
                jar.set(name, value, domain="rateyourmusic.com")
        _rym_jar, _rym_jar_paste = jar, paste
    return _rym_jar


def _rym_headers(warm=False):
    """The full Chrome header set — see RYM_HEADERS.

    The cookie is NOT part of this: it belongs to the jar (`_rym_jar`), which
    is what lets the WAF's own cookies ride along. `warm=True` is the
    top-level navigation to RYM's home page, which a browser sends with no
    Referer, `Sec-Fetch-Site: none` and `Sec-Fetch-User: ?1` — a request that
    claims RYM referred it while asking for RYM's root is a shape no browser
    produces."""
    headers = dict(RYM_HEADERS)
    if warm:
        headers.pop("Referer", None)
        headers["Sec-Fetch-Site"] = "none"
        headers["Sec-Fetch-User"] = "?1"
    return headers


def _rym_fetch(url, params, headers, jar):
    """One GET at RYM's etiquette: the 1 req/s spacing and the request itself
    under the same lock, so the Sources panel's parallel probes (genre and
    links rows) can never have two requests in flight at once."""
    global _rym_last
    with _rym_lock:
        wait = RYM_MIN_INTERVAL - (time.time() - _rym_last)
        if wait > 0:
            time.sleep(wait)
        _rym_last = time.time()
        return httpx.get(url, params=params or {}, headers=headers,
                         cookies=jar, timeout=20.0, follow_redirects=True)


def _rym_warm(cfg=None):
    """ONE navigation to RYM's home page per cookie paste, before the first
    request that paste makes anywhere else.

    This is the difference between a page and an interstitial for a cookie
    that is otherwise fine: the pasted session cookie (cf_clearance) is the
    user's half of the handshake, but the WAF also hands out cookies of its
    own (`__cf_bm`, `_cfuvid`) on a plain top-level navigation, and it treats
    a cold client that never made one as a scraper. Those Set-Cookie values go
    into the jar, so the request the caller actually wanted carries the whole
    set Chrome would send.

    Best effort, and per PASTE rather than per call: the request below reports
    its own failure if RYM still refuses (and no second warm-up can rescue a
    paste that is stale), so nothing is raised or logged here. Two probes
    starting at the same moment can both see an unwarmed paste and both
    navigate once — a duplicated polite request, never a wrong answer."""
    global _rym_warmed
    paste = _rym_cookie(cfg)
    if not paste or _rym_warmed == paste:
        return
    _rym_warmed = paste
    try:
        r = _rym_fetch(RYM_BASE + "/", None, _rym_headers(warm=True),
                       _rym_cookiejar(cfg))
    except httpx.HTTPError:
        return
    # `Response.cookies` is httpx's own parse of this answer's Set-Cookie.
    _rym_cookiejar(cfg).update(getattr(r, "cookies", None) or {})


def _rym_blocked(cfg=None):
    """Whether an earlier refusal still stands for the CURRENT credential.

    The latch is what makes a blocked RYM cost an import one probe instead of
    a walk through every slug candidate of every album. It must not outlive
    its cause, though: a refusal found with a different cookie — the user
    pasted a fresh one in Settings — or one older than RYM_BLOCK_TTL is
    forgotten here, so the next lookup really asks RYM again instead of
    answering "blocked" from a state the credential has moved past."""
    global _rym_warned
    if not _rym_warned:
        return False
    if _rym_blocked_cookie != _rym_cookie(cfg) or \
            time.time() - _rym_blocked_at > RYM_BLOCK_TTL:
        _rym_warned = False
        return False
    return True


def _rym_clear_block():
    """Forget a refusal — what a user-initiated probe does before it asks."""
    global _rym_warned
    _rym_warned = False


def _rym_record(status, url, challenge=False, reason=""):
    """What RYM last answered, in module state — `rym_last_response()` serves
    it to the Sources panel.

    `status` None means nothing answered at all (a timeout, a refused
    connection); `challenge` is whether the Cloudflare interstitial was in the
    body; `reason` is the sentence `_rym_reason` wrote for a refusal ("" when
    RYM answered); `url` and `at` say WHICH request got this answer, which is
    what turns "RYM refused the request" into something a user can act on."""
    global _rym_last_info
    _rym_last_info = {"status": status, "challenge": bool(challenge),
                      "url": str(url or ""), "at": time.time(),
                      "reason": str(reason or "")}


# How to fix a refusal, in one place: the health row and the last-response
# record both quote it, so the two cannot drift apart. Kept short and free of
# the site name: it lands in a log line that is already long, and the panel
# prints it next to the field it names.
_RYM_HOWTO = ("paste the whole `Cookie:` header (every name=value pair, not "
              "just one token) into Settings → Discovery → rym_cookie")


def _rym_reason(cfg=None, status=None, challenge=False, tries=1):
    """WHY RYM would not answer. Five cases, five different fixes, so one
    "403/challenge" sentence is not enough:

      * no cookie configured — nothing the WAF could accept was ever sent;
      * a challenge page on a 200 — the cookie is stale (or belongs to another
        network), and a fresh paste is what fixes it;
      * a 403 with NO challenge marker — the WAF refused the client outright:
        a datacenter/VPN network is blocked whatever the cookie says;
      * a 429 — RYM is throttling this network; and
      * a 5xx — RYM itself is unwell. The last two are transient, and both
        were already retried (RYM_RETRIES) before this sentence was written.

    The cookie advice is only appended where a cookie could help: telling a
    user to paste one while RYM answers 503 sends them after the wrong thing."""
    cookie = _rym_cookie(cfg)
    if challenge:
        why = "Cloudflare challenge instead of a page (HTTP 200) — " + \
            ("the rym_cookie has expired or is not for this network" if cookie
             else "no rym_cookie is set")
        return why + "; " + _RYM_HOWTO
    if status == 429:
        return ("HTTP 429 — RYM is throttling this network (retried %dx)"
                % tries)
    if status is not None and status >= 500:
        return "HTTP %d — RYM server error (retried %dx)" % (status, tries)
    if status == 403:
        why = ("HTTP 403 refused without a Cloudflare challenge — the WAF is "
               "blocking this network")
        why += (", or the cookie is for another session" if cookie
                else ", and no rym_cookie is set")
        return why + "; " + _RYM_HOWTO
    return "HTTP %s refused the request" % status


def rym_last_response():
    """RYM's last answer, for the Sources panel — {} before the first request.

    `status` is the HTTP status (None = nothing answered), `challenge` whether
    the Cloudflare interstitial was in the body, `url` what was asked for,
    `at`/`at_iso` when, and `reason` why it was refused ("" when RYM
    answered). It exists because `_rym_get`'s latch makes a refusal cost ONE
    probe: the log line is over by the time the user looks, and this is what
    the panel prints instead of a second guess at what went wrong."""
    info = dict(_rym_last_info)
    if info:
        info["at_iso"] = datetime.fromtimestamp(info["at"], timezone.utc) \
            .isoformat(timespec="seconds")
    return info


def _rym_unreachable(reason, cfg=None, status=None, challenge=False, url=""):
    """Record the last response and log ONE concise line per refusal — not per
    album, not per candidate.

    A per-album traceback would bury the import log for a source that is
    simply unavailable, so this is logged once and the chain moves on. The
    counter is what lets a caller tell "RYM is not answering" (stop asking —
    the next candidate cannot do better) from "that slug was wrong" (try the
    next one); `_rym_warned`, set here, is the same "stop asking": one refused
    request and `_rym_get` answers None without going out again, which is what
    keeps a blocked RYM off the import's critical path. The refusal records
    the cookie it was found with and when — `_rym_blocked` is what reads
    those, and a cookie the user replaces clears the latch on the spot.

    The response that earned the refusal is recorded (`_rym_record`) BEFORE
    the latch can answer the next caller from state: the line is printed once,
    but the panel has to be able to say WHICH refusal this was."""
    global _rym_warned, _rym_failures, _rym_blocked_cookie, _rym_blocked_at
    _rym_record(status, url, challenge, reason)
    _rym_failures += 1
    if _rym_warned:
        return
    _rym_warned = True
    _rym_blocked_cookie = _rym_cookie(cfg)
    _rym_blocked_at = time.time()
    print(f"[mlo] rateyourmusic: {reason} — skipping RYM (rym_cookie in "
          "Settings → Discovery is the credential)")


def _rym_cache_dir():
    """<music>/.mlo/data/rym_cache — the app's folder-state dir."""
    return _data_cache_dir("rym_cache")


def _rym_cache_read(key, ttl):
    d = _rym_cache_dir()
    if not d:
        return None
    fp = os.path.join(d, key + ".html")
    try:
        if os.path.isfile(fp) and time.time() - os.path.getmtime(fp) < ttl:
            with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
    except OSError:
        return None
    return None


def _rym_cache_write(key, text):
    d = _rym_cache_dir()
    if not d:
        return
    try:
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, key + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, os.path.join(d, key + ".html"))
    except OSError:
        pass


def _rym_expired(started):
    """Whether a lookup that began at *started* has spent its whole budget."""
    return time.time() - started > RYM_MAX_WALL


def _rym_get(path, params=None, cfg=None, expect=None):
    """Polite GET: 1 req/s, disk-cached, browser-like headers, None on any
    failure (a blocked RYM is logged once — see `_rym_unreachable`).

    A request under a NEW cookie is preceded by ONE warm-up navigation
    (`_rym_warm`) — RYM's home page — which is what makes the WAF hand over
    its own cookies before the page the caller actually wants.

    *expect* is the path the caller asked for: a 404 or a redirect to
    somewhere else (RYM sends an unknown slug to search/home) then counts as
    "no such page" — a miss the caller can move on from, not a sign that RYM
    is unreachable, so neither is logged as one.

    A 429, a 5xx or a timeout is RYM being busy, not refusing, so each gets a
    retry (RYM_RETRIES of them) — spaced by the same 1 req/s as every other
    request, and only then does the source count as unreachable. A user-
    initiated Test clears the latch first (`_rym_clear_block`), because "does
    this cookie work?" is a question the panel has to ask RYM for real."""
    import hashlib
    from urllib.parse import urlencode, urlsplit
    key = hashlib.sha1(
        (path + "?" + urlencode(sorted((params or {}).items()))).encode("utf-8")
    ).hexdigest()
    hit = _rym_cache_read(key, RYM_CACHE_TTL)
    if hit is not None:
        return hit
    if _rym_blocked(cfg):
        # RYM refused an earlier request under this same cookie (a challenge,
        # a 403, no connection). The candidates left cannot do better, and
        # asking them at 1 req/s is how an import of a few dozen albums used
        # to burn minutes on a source that was never going to answer.
        return None
    url = f"{RYM_BASE}{path}"
    _rym_warm(cfg)
    jar, headers = _rym_cookiejar(cfg), _rym_headers()
    reason = ""
    for attempt in range(RYM_RETRIES + 1):
        try:
            r = _rym_fetch(url, params, headers, jar)
        except httpx.HTTPError as e:
            r, reason = None, f"no connection ({type(e).__name__})"
        busy = r is None or r.status_code == 429 or r.status_code >= 500
        if not busy or attempt >= RYM_RETRIES:
            break
    tried = attempt + 1
    if r is None:
        _rym_unreachable(reason, cfg, url=url)
        return None
    if r.status_code != 200:
        # 404 is a slug that does not exist, not a blocked source: the caller
        # tries its next candidate instead of declaring RYM unreachable.
        if r.status_code != 404:
            _rym_unreachable(_rym_reason(cfg, r.status_code, tries=tried), cfg,
                             status=r.status_code, url=url)
        return None
    if not r.text:
        _rym_unreachable("empty response", cfg, status=r.status_code, url=url)
        return None
    if _RYM_CHALLENGE_RE.search(r.text[:4000]):
        _rym_unreachable(_rym_reason(cfg, r.status_code, challenge=True), cfg,
                         status=r.status_code, challenge=True, url=url)
        return None
    if expect is not None:
        final = urlsplit(str(getattr(r, "url", "") or "")).path
        if not final.startswith(expect):
            return None
    # A usable answer is also "the last response": the panel must not keep
    # showing a refusal RYM has since moved past.
    _rym_record(r.status_code, url)
    _rym_cache_write(key, r.text)
    return r.text


def _rym_slug(value):
    """RYM path slug: lowercase, `&` → `and` (RYM's own spelling), accents
    transliterated, apostrophes dropped ("Sgt. Pepper's" → `sgt-peppers`),
    other punctuation/whitespace collapsed to dashes."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"['`]", "", text.replace("&", " and "))
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text


def _rym_genres_from(html):
    """Genres (the /genre/ anchors) from a RYM release or artist page."""
    return _rym_labels(_RYM_GENRE_RE, html)


def rym_genres(artist, album):
    """RateYourMusic genres for an album, or None when RYM cannot answer.

    Tries the release URL RYM derives from the artist+album slugs (its
    canonical `/release/album/<artist>/<album>/` shape) first and its search
    page second, so a punctuation-heavy title still resolves. Every candidate
    is VERIFIED before it is read — the same rule the link resolver uses
    (`_rym_verified`): the answer must have stayed on the path that was asked
    for, and the page must state the artist AND the album. A genre list lifted
    from a same-named cover version is worse than no genres at all. Returns
    {"genres": [...], "descriptors": [...], "level": "album", "tracks": [...],
    "source_url": ...} or None — see RYM_BASE's note on the blocked-by-RYM
    failure mode. Chart data is NOT scraped: nothing in the app consumes a RYM
    chart, so only the genre path is implemented.

    `level` is always "album" here: RYM classifies releases, and a release's
    genres are applied to every one of its tracks — the caller records that in
    the provenance rather than pretending the answer was per-track.
    `descriptors` are the page's /descriptor/ anchors, used only when the page
    carries no /genre/ anchor at all, and `tracks` carries the track list so a
    row that DOES state its own genre can be mapped by position, then title.
    A user-initiated check (the Sources panel's Test) clears the refusal
    latch first — `_rym_clear_block` — so the saved cookie is really put to
    RYM instead of being answered "blocked" from an earlier run.
    """
    artist = str(artist or "").strip()
    album = str(album or "").strip()
    if not artist or not album:
        return None

    def answer(html, url):
        # The track list is read first and then REMOVED: a row that states its
        # own genre must not have that genre promoted to the whole release.
        rows = _rym_tracks_from(html)
        head = _RYM_TRACK_ROW_RE.sub("", html or "")
        genres = _rym_genres_from(head)
        descriptors = [] if genres else _rym_labels(_RYM_DESCRIPTOR_RE, head)
        if not genres and not descriptors:
            return None
        return {"genres": genres or descriptors, "descriptors": descriptors,
                "level": "album", "tracks": rows,
                "source_url": f"{RYM_BASE}{url}", "source": "rym"}

    url = f"/release/album/{_rym_slug(artist)}/{_rym_slug(album)}/"
    started = time.time()
    html = _rym_verified(url, None, artist, album)
    if html:
        got = answer(html, url)
        if got:
            return got
    # A few more requests at most, and only inside the wall clock: a blocked
    # RYM answers the FIRST one with a challenge, so the rest are spent misses.
    if _rym_expired(started) or _rym_blocked():
        return None
    html = _rym_get("/search", {"searchterm": f"{artist} {album}", "type": "a"},
                    expect="/search")
    if not html:
        return None
    m = _RYM_ARTIST_LINK_RE.search(html)
    if _rym_expired(started):
        return None
    # The search page answers a QUERY, not a question: each hit is confirmed
    # as the release asked about (a cover version on the same query supplies
    # no genres) before its genre anchors are read.
    for rel_url in _RYM_RELEASE_LINK_RE.findall(html)[:3]:
        page = _rym_verified(rel_url, None, artist, album)
        if not page:
            if _rym_expired(started) or _rym_blocked():
                return None
            continue
        got = answer(page, rel_url)
        if not got:
            continue
        got["artist_page"] = f"{RYM_BASE}{m.group(1)}" if m else ""
        return got
    return None


def rym_artist_genres(artist):
    """RateYourMusic genres for an artist (its /artist/ page), or None.

    Confirmed as that artist's page before its genres are read, exactly like
    the album path — a label or another act's page must not supply them."""
    artist = str(artist or "").strip()
    if not artist:
        return None
    url = f"/artist/{_rym_slug(artist)}"
    html = _rym_verified(url, None, artist)
    genres = _rym_genres_from(html or "")
    if not genres:
        return None
    return {"genres": genres, "source_url": f"{RYM_BASE}{url}"}


# --------------------------------------------------------------------------- #
# RYM link resolution (album + artist)
# --------------------------------------------------------------------------- #
# MusicBrainz is the FIRST source: it holds the rateyourmusic.com page as a
# `url` relation ("other databases") on the release GROUP and on the artist —
# VERIFIED live: release-group/6e335887… (In Rainbows) →
# /release/album/radiohead/in_rainbows/, artist/a74b1b7f… (Radiohead) →
# /artist/radiohead, and the same for Nirvana / MTV Unplugged in New York. A
# link MusicBrainz states IS that page, so it costs no RYM request, needs no
# cookie, and nothing has to be confirmed or guessed.
#
# Only what MusicBrainz could not state is scraped — with the `rym_cookie`
# configured, if there is one: without a cookie rateyourmusic.com challenges
# the request (there is no cookie gate, the source simply refuses), which
# latches RYM off for a while — see `_rym_blocked`. Its URLs are derived from
# the names and a candidate is fetched once to prove it exists:
#
#   /release/album/<artist-slug>/<album-slug>/     /artist/<artist-slug>
#
# A candidate is fetched once (1 req/s, 30-day cache) and accepted only when
# the page that comes back IS that page: HTTP 200 (a 404 or a redirect to
# search/home means the slug is wrong), no Cloudflare interstitial, and the
# page states the artist — and for a release, the album too. Nothing is
# guessed from a partial page, so a candidate that cannot be confirmed yields
# NO link and the user pastes their own (the manual editor is unchanged).
# After a refusal the ladder is not walked again for a while — RYM costs ONE
# probe at most, never minutes — but a new cookie (or the same one once the
# refusal has aged out) re-asks, so a corrected credential is never stuck.
_RYM_RELEASE_LINK_RE = re.compile(r'href="(/release/album/[^"]+)"', re.I)
# What the user can act on when nothing resolved because RYM would not answer.
_RYM_BLOCKED_NOTE = ("could not resolve on RateYourMusic — blocked by "
                     "Cloudflare; set rym_cookie in Settings, or MusicBrainz "
                     "links are used")


def _rym_slug_candidates(name):
    """The slugs *name* may use on RYM, best first: its own slug, then the
    de-`the`-ed one ("The Beatles" → "beatles")."""
    slug = _rym_slug(name)
    out = [slug]
    if slug.startswith("the-"):
        out.append(slug[4:])
    return [s for s in out if s]


def _rym_ref(text):
    """Comparison form of a name or page: accents folded, lowercase, `&`→and,
    alphanumerics only — so "Simon & Garfunkel" matches "Simon &amp;
    Garfunkel" on a page and "BJÖRK" matches "Bjork"."""
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = t.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "", t.replace("&", " and "))


def _rym_page_text(page):
    """A page's visible text: tags stripped, entities decoded."""
    return _html.unescape(re.sub(r"<[^>]+>", " ", page or ""))


def _rym_mentions(page, *names):
    """Whether the page states every one of *names* (normalised compare)."""
    text = _rym_ref(_rym_page_text(page))
    return all(_rym_ref(n) in text for n in names if str(n or "").strip())


def _rym_verified(path, cfg, *names):
    """The page at *path*, or None when it is not that page (see above)."""
    page = _rym_get(path, cfg=cfg, expect=path)
    if not page or not _rym_mentions(page, *names):
        return None
    return page


# A rateyourmusic.com page anywhere in MusicBrainz's relations. `www.` and the
# http scheme are accepted because MB stores whatever the editor typed.
_MB_RYM_URL_RE = re.compile(r"^https?://(?:www\.)?rateyourmusic\.com/", re.I)


def _mb_query(value):
    """A name as a MusicBrainz Lucene term: a quote or backslash inside the
    quoted phrase would otherwise break the whole query."""
    return re.sub(r'["\\]', " ", str(value or "")).strip()


def _mb_rym_relations(entity, mbid, inc="url-rels"):
    """(RYM urls, entity data) for one MusicBrainz MBID.

    ([], {}) when MusicBrainz cannot answer — this is a source that may be
    busy, never a reason to fail the lookup."""
    try:
        data = mb_get_cached(f"{entity}/{mbid}", {"inc": inc, "fmt": "json"})
    except Exception:
        return [], {}
    data = data or {}
    urls = []
    for rel in data.get("relations") or []:
        url = str((rel.get("url") or {}).get("resource") or "").strip()
        if _MB_RYM_URL_RE.match(url):
            urls.append(url)
    # A release-group carries the /release/ page; prefer it over anything else
    # (a /label/ or /artist/ relation) when MB states both.
    urls.sort(key=lambda u: "/release/" not in u)
    return urls, data


def _mb_credit_id(entity):
    """The first credited artist's MBID of a release/release-group payload."""
    credit = (entity or {}).get("artist-credit") or [{}]
    return ((credit[0].get("artist") or {}).get("id") or "")


def _mb_release_group_ids(artist, album, limit=5):
    """Release-group MBIDs MusicBrainz's own search returns for this album.

    Only a row whose TITLE is the album asked for is kept: a same-titled
    release by another artist must never contribute a link."""
    q = f'releasegroup:"{_mb_query(album)}"'
    if artist:
        q += f' AND artist:"{_mb_query(artist)}"'
    try:
        rows = (search_mb("release-group", q, limit=limit) or {}).get("rows") or []
    except Exception:
        return []
    return [r["id"] for r in rows
            if r.get("id") and _rym_ref(r.get("title")) == _rym_ref(album)]


def _mb_rym_links(artist="", album="", mbid=None):
    """The RYM pages MusicBrainz itself states: ``{"album", "artist"}``.

    A link MusicBrainz states needs no confirmation — it IS the canonical
    page — so this is the resolver's first source and the one that works with
    no `rym_cookie` at all. `mbid` is the caller's own MusicBrainz ID: the
    release GROUP's where the caller has one (`release["release_group_id"]`),
    a release's otherwise (a release usually carries no RYM relation where its
    group does, so the group is asked first). Without an MBID the group is
    found with MusicBrainz's search. Either key of the result may be missing.
    """
    out, group = {}, {}
    if mbid:
        for entity in ("release-group", "release"):
            urls, data = _mb_rym_relations(entity, mbid,
                                           "url-rels+artist-credits")
            if data:
                group = data
            if urls:
                out["album"] = urls[0]
                break
    if not out.get("album") and album:
        for group_mbid in _mb_release_group_ids(artist, album):
            urls, data = _mb_rym_relations("release-group", group_mbid,
                                           "url-rels+artist-credits")
            if urls:
                out["album"], group = urls[0], data
                break
    if artist and not out.get("artist"):
        artist_mbid = _mb_credit_id(group)
        if not artist_mbid:
            try:
                from server import discovery
                artist_mbid = discovery.resolve_artist_mbid(artist) or ""
            except Exception:
                artist_mbid = ""
        if artist_mbid:
            urls, _data = _mb_rym_relations("artist", artist_mbid)
            if urls:
                out["artist"] = urls[0]
    return out


def rym_links(artist="", album="", cfg=None, mbid=None):
    """Verified RateYourMusic links for an album:
    ``{"album", "artist", "note"}``.

    MusicBrainz is asked FIRST (see `_mb_rym_links`): it states the RYM page
    as a url relation on the release group and on the artist, which resolves
    both links with no cookie and no scraping on an install where
    rateyourmusic.com refuses an automated client (RYM_BASE).

    Only a link MusicBrainz did not state is scraped — the `rym_cookie` is
    what makes that scrape answer, not a gate this function applies (there is
    no cookie check here: with no cookie, or a stale one, RYM challenges the
    request and the ladder stops, see `_rym_blocked`): the album as
    `/release/album/<artist>/<album>/` with the exact slugs, then with the
    de-`the`-ed ones, then from RYM's own search page — each candidate
    confirmed before it is accepted. The artist link comes from the album
    page's own `/artist/` link when it is one of the artist's slugs, else from
    `/artist/<slug>` directly.

    Either link is None when it could not be confirmed, and `note` says so
    ("could not resolve …") — that is the user-pastes-the-URL state, never an
    error: when the reason is a blocked RYM the note says what to do about it.
    Gated by `rym_links_auto` (mlo.config, default True): off means no request
    at all. `mbid` (optional) is the release group's MusicBrainz ID, or a
    release's. A user-initiated check (the Sources panel's Test) clears the
    refusal latch first — `_rym_clear_block` — so the configured cookie is
    really put to RYM instead of being answered "blocked" from an earlier run.
    """
    if cfg is None:
        try:
            from mlo.config import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
    out = {"album": None, "artist": None, "note": ""}
    artist = str(artist or "").strip()
    album = str(album or "").strip()
    if not cfg.get("rym_links_auto", True):
        out["note"] = "automatic lookup is off"
        return out
    if not artist and not album:
        out["note"] = "nothing to look up"
        return out

    # 1) MusicBrainz, which states the RYM page itself.
    try:
        stated = _mb_rym_links(artist, album, mbid)
    except Exception:
        stated = {}
    out["album"] = stated.get("album")
    out["artist"] = stated.get("artist")

    # 2) Scrape only what MB could not state. A failure that is not a miss (no
    # connection, a challenge) is counted, and `_rym_get` refuses everything
    # after the first one: RYM costs ONE probe, never a slug walk.
    started = time.time()
    fails = _rym_failures
    page = None

    def stop():
        """Whether RYM has already answered for this lookup — a refusal
        (`_rym_failures`, or `_rym_blocked`, the latch `_rym_get` reads) or
        the wall clock."""
        return _rym_blocked(cfg) or _rym_failures != fails or _rym_expired(started)

    if artist and album and not out["album"]:
        for a in _rym_slug_candidates(artist):
            for b in _rym_slug_candidates(album):
                path = f"/release/album/{a}/{b}/"
                page = _rym_verified(path, cfg, artist, album)
                if page:
                    out["album"] = f"{RYM_BASE}{path}"
                    break
            if out["album"] or stop():
                break
        if not out["album"] and not stop():
            # RYM's own search: the first release hits for the query, each
            # confirmed the same way (so a cover version cannot slip through).
            index = _rym_get("/search", {"searchterm": f"{artist} {album}",
                                         "type": "a"}, cfg=cfg)
            for rel in _RYM_RELEASE_LINK_RE.findall(index or "")[:3]:
                page = _rym_verified(rel, cfg, artist, album)
                if page:
                    out["album"] = f"{RYM_BASE}{rel}"
                    break
                if stop():
                    break

    if artist and not out["artist"] and not stop():
        # The album page links its own artist: try RYM's own answer first,
        # but only inside the slug set this name can legitimately produce.
        slugs = list(_rym_slug_candidates(artist))
        if page:
            m = _RYM_ARTIST_LINK_RE.search(page)
            slug = m.group(1).rstrip("/").rsplit("/", 1)[-1] if m else ""
            if slug in slugs:
                slugs.remove(slug)
                slugs.insert(0, slug)
        for a in slugs:
            path = f"/artist/{a}"
            if _rym_verified(path, cfg, artist):
                out["artist"] = f"{RYM_BASE}{path}"
                break
            if stop():
                break

    if not out["album"] and not out["artist"]:
        if not artist:
            out["note"] = "no artist name to look up"
        elif _rym_failures != fails or _rym_blocked(cfg) or not _rym_cookie(cfg):
            # RYM refused (this call, or earlier in the process) and nothing
            # on MusicBrainz either: say what the user can do instead of
            # "no link found".
            out["note"] = _RYM_BLOCKED_NOTE
        else:
            out["note"] = "could not resolve on RateYourMusic"
    elif artist and album and not out["album"]:
        out["note"] = "could not resolve the album link on RateYourMusic"
    elif artist and not out["artist"]:
        out["note"] = "could not resolve the artist link on RateYourMusic"
    return out


# --------------------------------------------------------------------------- #
# Bandcamp (scraped — no public API)
# --------------------------------------------------------------------------- #
# An album page carries the release's crowd-sourced tags as `.tralbum-tags`
# anchors, and its own JSON blob (`data-tralbum`) holds the album title plus
# one `trackinfo` entry per track. Bandcamp states NO per-track genre at all
# — every tag it has is the album's — so this source is an ALBUM-level
# crowdsourced tier and the chain labels its row `level: album`.
#
# VERIFIED here: bandcamp.com's own search/tag pages answer this client with
# Cloudflare's interstitial, but the per-artist album pages do NOT — and the
# album URL is derivable from the names
# (`https://<artist-slug>.bandcamp.com/album/<album-slug>`), so the source
# costs one request per album. A page is accepted only when it states THIS
# album: the tralbum's own title matches, and (when the caller knows its own
# track titles) the page's track titles line up with them — a wrong album's
# tags are NO answer. Politeness is the same as RYM's: browser headers, 1
# req/s, one line per process on a hard failure, and the caller's 30-day cache
# wraps the whole source so a repeat import re-fetches nothing.
BANDCAMP_BASE = "https://bandcamp.com"
BANDCAMP_MIN_INTERVAL = 1.0
_bandcamp_lock = threading.Lock()
_bandcamp_last = 0.0
_bandcamp_warned = False
_BANDCAMP_HEADERS = {
    "User-Agent": RYM_HEADERS["User-Agent"],
    "Accept": RYM_HEADERS["Accept"],
    "Accept-Language": RYM_HEADERS["Accept-Language"],
    "Referer": BANDCAMP_BASE + "/",
}
# The attribute value is HTML-escaped (`&quot;`), so no raw `"` can appear
# inside it and a plain non-greedy match is safe.
_BANDCAMP_TRALBUM_RE = re.compile(r'data-tralbum="([^"]*)"', re.S)
# Tags are `<a class="tag" href=".../discover/<slug>">Label</a>` inside the
# tag block. Read as labels; the block is found by its own class so no anchor
# elsewhere on the page can be mistaken for a tag.
_BANDCAMP_TAG_RE = re.compile(r'<a class="tag"[^>]*>([^<]+)</a>', re.I)


def _bandcamp_unreachable(reason):
    """One concise line per process when Bandcamp cannot answer at all."""
    global _bandcamp_warned
    if _bandcamp_warned:
        return
    _bandcamp_warned = True
    print(f"[mlo] bandcamp.com: {reason} — that source is skipped this run")


def _bandcamp_get(url, timeout=20.0):
    """One polite GET of a Bandcamp page (browser UA, 1 req/s), or "".

    "" covers everything that is not that page: a 404 slug, an empty body, a
    connection failure. A 404 is a miss the caller moves on from, so only a
    connection failure is logged.
    """
    global _bandcamp_last
    with _bandcamp_lock:
        wait = BANDCAMP_MIN_INTERVAL - (time.time() - _bandcamp_last)
        if wait > 0:
            time.sleep(wait)
        _bandcamp_last = time.time()
        try:
            r = httpx.get(url, headers=_BANDCAMP_HEADERS, timeout=timeout,
                          follow_redirects=True)
        except httpx.HTTPError as e:
            _bandcamp_unreachable(f"no connection ({type(e).__name__})")
            return ""
    if r.status_code != 200 or not r.text:
        return ""
    return r.text


def _bandcamp_slug(value, sep):
    """`value` as a Bandcamp slug: folded, `&` → `and`, other junk → *sep*."""
    text = str(value or "").strip().lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", sep, text).strip(sep)


def _bandcamp_subdomain_candidates(artist):
    """The Bandcamp subdomains *artist* may use, best first.

    Bandcamp subdomains are written without separators ("King Buffalo" →
    `kingbuffalo`); the hyphenated form is kept as a second candidate because
    some artists do register that way.
    """
    out = [_bandcamp_slug(artist, ""), _bandcamp_slug(artist, "-")]
    return [s for i, s in enumerate(out) if s and s not in out[:i]]


def _bandcamp_tralbum(page):
    """The page's `data-tralbum` JSON, or None when the page carries none."""
    match = _BANDCAMP_TRALBUM_RE.search(page or "")
    if not match:
        return None
    try:
        data = json.loads(_html.unescape(match.group(1)))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _bandcamp_tracks(tralbum):
    """[{position, title}] from a tralbum's `trackinfo` (its own track list)."""
    out = []
    for row in (tralbum or {}).get("trackinfo") or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if title:
            out.append({"position": row.get("track_num"), "title": title})
    return out


def _bandcamp_tags(page):
    """The album's own tags, junk filtered like ListenBrainz free tags.

    A tag is kept when it is not a mood word (`LB_MOOD_WORDS`) and is not the
    same word as one already kept once punctuation and spacing are ignored —
    Bandcamp lists literal duplicates of its own slugs side by side ("doom
    metal" and "doommetal"), and those are one tag, not two.
    """
    from server import discovery

    start = page.find("tralbum-tags tralbum-tags-nu")
    if start < 0:
        start = page.find("tralbumData tralbum-tags")
    if start < 0:
        return []
    end = page.find("</div>", start)
    block = page[start:end if end > start else len(page)]
    out, seen = [], set()
    for match in _BANDCAMP_TAG_RE.finditer(block):
        name = _html.unescape(match.group(1)).strip()
        key = re.sub(r"[^a-z0-9]+", "", name.lower())
        if not name or not key or key in seen:
            continue
        if name.lower() in discovery.LB_MOOD_WORDS:
            continue
        seen.add(key)
        out.append(name)
    return out


def _bandcamp_is_album(tralbum, album, titles=None):
    """Whether a tralbum is the album we asked about (never a guess)."""
    from server import discovery

    if album:
        stated = discovery.norm((tralbum.get("current") or {}).get("title"))
        if stated != discovery.norm(album):
            return False
    page_titles = {discovery.norm(t["title"]) for t in _bandcamp_tracks(tralbum)}
    ours = [discovery.norm(t) for t in titles or [] if str(t or "").strip()]
    if ours and page_titles and not (set(ours) & page_titles):
        return False
    return True


def bandcamp_album(artist="", album="", titles=None):
    """Bandcamp's tags for one album: {"genres", "tracks", "url"} or None.

    `titles` (optional) are the caller's own track titles for that album: they
    are what confirms the page that came back is this release, so a
    same-named record by another band cannot donate its tags. None means "no
    such album for this client" (a wrong slug, no Bandcamp presence, the
    interstitial) — never an empty answer dressed up as one.
    """
    name = str(album or "").strip()
    if not name:
        return None
    for sub in _bandcamp_subdomain_candidates(artist):
        url = f"https://{sub}.bandcamp.com/album/{_bandcamp_slug(name, '-')}"
        page = _bandcamp_get(url)
        if not page:
            continue
        tralbum = _bandcamp_tralbum(page)
        if not tralbum or not _bandcamp_is_album(tralbum, name, titles):
            continue
        return {"genres": _bandcamp_tags(page),
                "tracks": _bandcamp_tracks(tralbum), "url": url}
    return None


# --------------------------------------------------------------------------- #
# Genre chain — one merge point for every genre source
# --------------------------------------------------------------------------- #
# Order used when mlo.config `genre_sources` is empty. It is a PRIORITY LIST:
# every source is asked for EVERY track (see `_genre_source_answers`), and the
# merged answer is taken in this order, so the position of a source is what a
# track's genre picks look like. `mlo.config.DEFAULT_CONFIG["genre_sources"]`
# is this list and `normalize_config` migrates both previously shipped
# defaults into it (`tools/test_genres.py` asserts the two are equal).
#
#   a. rateyourmusic  release page — per TRACK where the page states one, else
#                     album. FIRST by the user's own requirement: its curated
#                     genre + descriptor classification is the one they want
#                     (needs `rym_cookie`; blocked, it logs one line and is
#                     skipped — see RYM_BASE).
#   b. listenbrainz   recording tags → release-group → artist. Crowdsourced
#                     PER RECORDING, free, no key, MBID-native (no title
#                     guessing for a release MusicBrainz knows).
#   c. musicbrainz    recording → release → release group → artist. Curated
#                     per recording, and the app's identity anchor, but its
#                     genre coverage is spotty.
#   d. itunes         per-track `primaryGenreName`. Free, keyless, and its
#                     per-track genre is reliable for mainstream releases.
#   e. lastfm         track.getTopTags → artist.getTopTags. Crowdsourced per
#                     track and broad, but needs a free API key.
#   f. theaudiodb     searchtrack.php per track → album genre/mood. Per-track
#                     plus album metadata (biography, art), keyless.
#   g. wikidata       P136 on the recording entity, then the release group /
#                     searched entity. Curated but sparse, per recording where
#                     one is stated.
#   h. bandcamp       album page tags. Crowdsourced and strong for
#                     indie/self-released records; ALBUM level (Bandcamp
#                     states no per-track genre at all), keyless.
#   i. discogs        release styles + genres. Curated, album level, needs a
#                     token.
#   j. deezer         album genres only — album level, keyless.
#   k. spotify        ARTIST genres — artist level, needs client id+secret,
#                     the last resort (the chain only ever adds a source's
#                     answer, so the weakest one goes last).
#
# Every per-track source sits ABOVE every album-only one (bandcamp, discogs,
# deezer, spotify), which is what keeps a track's own answer ahead of an
# album-wide guess (`tools/test_genres.py` asserts that property of this list).
#
# `soulseek` is NOT in the default list: peers advertise folders and file
# names, not genres, so it can never state one — it stays handled as a
# documented no-op so a saved config listing it keeps working.
GENRE_SOURCES = ["rateyourmusic", "listenbrainz", "musicbrainz", "itunes",
                 "lastfm", "theaudiodb", "wikidata", "bandcamp", "discogs",
                 "deezer", "spotify"]


# The key an ALBUM- or ARTIST-wide answer is filed under: it applies to every
# track of the release, and the row's `level` is what says so.
_ALL_TRACKS = ""
_LEVEL_RANK = {"track": 0, "album": 1, "artist": 2}


def _genre_track_key(disc, position):
    """`"disc:position"` — the key one track's answer is filed under."""
    return f"{int(disc or 1)}:{int(position or 0)}"


def _genre_names(names):
    """Trimmed, case-insensitively deduped names, original spelling kept."""
    out = []
    for name in names or []:
        text = str(name or "").strip()
        if text and text.lower() not in {g.lower() for g in out}:
            out.append(text)
    return out


def _genre_row(level, names, title=""):
    """{"level", "genres"[, "title"]} for one answer, or None when empty."""
    clean = _genre_names(names)
    if not clean:
        return None
    row = {"level": level, "genres": clean}
    if title:
        row["title"] = str(title)
    return row


# --------------------------------------------------------------------------- #
# 30-day value cache — genre answers (per album / recording) and, through the
# same helper, the cover-search image dimension probes (per image URL)
# --------------------------------------------------------------------------- #
# Genre data moves slowly, one album pass asks the same album (and the same
# recording) over and over, and every crowdsourced source here is throttled to
# ~1 req/s; an image's size likewise never changes for a URL. Answers —
# including "nothing", which must not be re-asked either — are kept in memory
# and on disk under <music>/.mlo/data/genre_cache, keyed by kind so the two
# users never collide.
_GENRE_CACHE_TTL = 30 * 86400.0
_GENRE_CACHE: dict = {}
_GENRE_CACHE_LOCK = threading.Lock()
_GENRE_CACHE_MAX = 5000
_GENRE_MISS = object()


def _genre_cache_file(ident):
    import hashlib

    folder = _data_cache_dir("genre_cache")
    if not folder:
        return None
    return os.path.join(folder, hashlib.sha1(ident.encode("utf-8")).hexdigest()
                        + ".json")


def _genre_cached(kind, key, producer):
    """`producer()` memoized 30 days by (kind, key) — memory, then disk.

    The app's one 30-day disk cache. `kind` is "album" (identity: artist +
    album, or a release-group/artist MBID), "recording" (identity: a recording
    MBID or artist + title) or "cover_dim" (identity: an image URL).
    `producer` must return JSON-serializable data or None and must never
    raise: a failing source is not allowed to take the chain down, and "no
    answer" is cached like any other answer so a rerun costs no request.
    """
    ident = f"{kind}|{key}"
    with _GENRE_CACHE_LOCK:
        hit = _GENRE_CACHE.get(ident)
    if hit is not None:
        return None if hit is _GENRE_MISS else hit
    path = _genre_cache_file(ident)
    if path:
        try:
            if (os.path.isfile(path)
                    and time.time() - os.path.getmtime(path) < _GENRE_CACHE_TTL):
                with open(path, encoding="utf-8") as fh:
                    value = json.load(fh)
                with _GENRE_CACHE_LOCK:
                    _GENRE_CACHE[ident] = _GENRE_MISS if value is None else value
                return value
        except (OSError, ValueError):
            pass
    value = producer()
    with _GENRE_CACHE_LOCK:
        if len(_GENRE_CACHE) >= _GENRE_CACHE_MAX:
            _GENRE_CACHE.clear()
        _GENRE_CACHE[ident] = _GENRE_MISS if value is None else value
    if path:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(value, fh)
            os.replace(tmp, path)
        except OSError:
            pass
    return value


# --------------------------------------------------------------------------- #
# Per-source answers
# --------------------------------------------------------------------------- #
def _mb_wikidata_qid(mbid, entity="release-group"):
    """The Wikidata entity a MusicBrainz entity points at, or None.

    MusicBrainz holds the relation itself (`inc=url-rels`), which is what
    makes the Wikidata tier an identity lookup instead of a title guess.
    `entity` is a MusicBrainz endpoint name ("release-group", "recording",
    "work"): the genre chain asks a RECORDING first, then the WORK it performs
    (see `_mb_recording_ids`), because a track's own item carries P136 where
    the release group carries none.
    """
    if not mbid:
        return None
    try:
        data = mb_get_cached(f"{entity}/{mbid}",
                             {"inc": "url-rels", "fmt": "json"})
    except Exception:
        return None
    return _mb_wikidata_qid_in((data or {}).get("relations"))


def _mb_wikidata_qid_in(relations):
    """The Wikidata QID of a MusicBrainz relation list, or ""."""
    for rel in relations or []:
        if str(rel.get("type") or "").strip().lower() != "wikidata":
            continue
        match = re.search(r"Q\d+",
                          str((rel.get("url") or {}).get("resource") or ""))
        if match:
            return match.group(0)
    return ""


def _mb_recording_ids(recording_mbid):
    """{"qid", "work"} for one MusicBrainz recording, both "" when none.

    ONE request (`inc=url-rels+work-rels`) answers both questions: whether the
    recording itself links a Wikidata item (rare — every popular recording
    probed on this machine had no such link), and which WORK it performs. The
    work is the practical route and it is still an identity, never a title
    guess: VERIFIED here, Queen's "Bohemian Rhapsody" recording → work
    `41c94a08…` → Wikidata Q187745, whose P136 is progressive rock / hard rock
    / progressive pop, and Pink Floyd's "Time" → work → Q641913 ("popular
    music"). A multi-word ENTITY SEARCH is not usable for this at all —
    `wbsearchentities` matches labels by prefix, so "artist title" answers
    nothing (VERIFIED: no hits for four-word research queries).
    """
    if not recording_mbid:
        return {"qid": "", "work": ""}
    try:
        data = mb_get_cached(f"recording/{recording_mbid}",
                             {"inc": "url-rels+work-rels", "fmt": "json"})
    except Exception:
        return {"qid": "", "work": ""}
    relations = (data or {}).get("relations") or []
    work = ""
    for rel in relations:
        if work:
            break
        work = str((rel.get("work") or {}).get("id") or "")
    return {"qid": _mb_wikidata_qid_in(relations), "work": work}


# --------------------------------------------------------------------------- #
# Credits / performers
# --------------------------------------------------------------------------- #
# MusicBrainz keeps the players on the RECORDING: `artist-rels` returns one
# relation per person — the relation TYPE is the role (performer, instrument,
# vocal, producer, engineer, mix, mastering, arranger, composer, lyricist,
# conductor, remixer, …), the instrument or vocal part rides in `attributes`
# ("double bass", "lead vocals"), and the person's name + MBID come along in
# the same relation, so no second request is needed to name them. `work-rels`
# says which WORK the recording performs. ONE request per recording, ONE per
# release (`recording-level-rels` + `work-level-rels` bring each track's own
# relations with the release, VERIFIED against MB while writing this), both
# through mb_get_cached so the cache and the 1 req/s etiquette apply like any
# other MB call.
CREDITS_MAX_RECORDINGS = 200   # a box set must not become 200 requests' worth
CREDITS_MAX_ROWS = 500         # …and the UI gets a capped list either way
# The tags a tagger writes when MB has nothing (Vorbis PERFORMER is the
# common one, per player, as "Name (instrument)").
CREDIT_TAG_ROLES = ("PERFORMER", "COMPOSER", "LYRICIST", "ARRANGER",
                    "CONDUCTOR", "REMIXER", "ENGINEER", "PRODUCER")


def _credit_rows(relations, rows=None):
    """Append one row per MusicBrainz relation to *rows*:
    ``{role, attributes, artist, mbid}``.

    Every artist-target relation is kept whatever its type — MB reports the
    role itself, and a release's players are split across `performer` (Vorbis
    era) and `instrument` (current schema), so filtering by type would drop
    half of them. A relation pointing at a WORK becomes a `work` row whose
    `artist` is the work's title (its own type, usually "performance", rides
    in `attributes`), which is how the recording's work stays visible.
    """
    rows = [] if rows is None else rows
    for rel in relations or []:
        if len(rows) >= CREDITS_MAX_ROWS:
            break
        attributes = [str(a).strip() for a in (rel.get("attributes") or [])
                      if str(a).strip()]
        if rel.get("target-type") == "work":
            work = rel.get("work") or {}
            title = str(work.get("title") or "").strip()
            if title:
                rows.append({"role": "work", "attributes": attributes or
                             [str(rel.get("type") or "").strip()],
                             "artist": title, "mbid": work.get("id") or ""})
            continue
        artist = rel.get("artist") or {}
        name = str(artist.get("name") or rel.get("target-credit") or "").strip()
        if not name:
            continue
        rows.append({"role": str(rel.get("type") or "credit").lower(),
                     "attributes": attributes, "artist": name,
                     "mbid": artist.get("id") or ""})
    return rows


def tidy_credit_rows(rows):
    """De-duplicated, role-grouped, capped credit rows — what the UI renders.

    Albums repeat the same player on every track and the fallback repeats the
    same tag on every file, so the same (role, person, instrument) is kept
    once; sorting by role groups the list the way it is displayed.
    """
    seen = set()
    out = []
    for row in rows or []:
        key = (row.get("role"), row.get("artist", "").lower(), row.get("mbid"),
               tuple(row.get("attributes") or []))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    out.sort(key=lambda r: (r.get("role") or "", r.get("artist", "").lower()))
    return out[:CREDITS_MAX_ROWS]


def credit_rows_from_tags(tags):
    """Credit rows from a file's OWN tags, for when MusicBrainz has nothing.

    `tags` is a raw tag dump (mlo.audio all_tags, keys already canonicalised to
    PERFORMER/COMPOSER/… when the container maps them). Vorbis writes
    `PERFORMER=Name (instrument)` once per player and some taggers join several
    names with "; ", so both shapes are split back apart. ponytail: a
    multi-value Vorbis tag collapses to its first value in all_tags — enough
    for a fallback, read af.audio.tags directly if every player must show.
    """
    rows = []
    if not isinstance(tags, dict):
        return rows
    by_key = {str(k).upper(): v for k, v in tags.items()}
    for role in CREDIT_TAG_ROLES:
        for part in re.split(r"\s*;\s*", str(by_key.get(role) or "")):
            part = part.strip()
            if not part:
                continue
            m = re.match(r"^(.*?)\s*\(([^()]*)\)$", part)
            name, attributes = ((m.group(1).strip(), [m.group(2).strip()])
                                if m else (part, []))
            if name:
                rows.append({"role": role.lower(), "attributes": attributes,
                             "artist": name, "mbid": ""})
    return tidy_credit_rows(rows)


def recording_credits(recording_mbid):
    """Credit rows for ONE recording (its players, plus its work).

    Raises MusicBrainzError (or httpx's own error) when MB cannot answer — the
    caller reports that reason rather than an empty result.
    """
    data = mb_get_cached(f"recording/{recording_mbid}",
                         {"inc": "artist-rels+work-rels", "fmt": "json"})
    return tidy_credit_rows(_credit_rows((data or {}).get("relations")))


def release_credits(release_mbid):
    """Credit rows for a whole RELEASE — every track's recording.

    The release request carries each track's own relations, so this stays ONE
    request per album; only the aggregate is capped, not the request count.
    """
    data = mb_get_cached(
        f"release/{release_mbid}",
        {"inc": "recordings+artist-rels+recording-level-rels+work-rels"
                "+work-level-rels", "fmt": "json"})
    rows = _credit_rows((data or {}).get("relations"))
    recordings = 0
    for medium in (data or {}).get("media") or []:
        for track in medium.get("tracks") or []:
            if (recordings >= CREDITS_MAX_RECORDINGS
                    or len(rows) >= CREDITS_MAX_ROWS):
                break
            recordings += 1
            _credit_rows((track.get("recording") or {}).get("relations"), rows)
    return tidy_credit_rows(rows)


def _itunes_track_genres(artist, album, cfg=None):
    """{"disc:position": [genre]} from Apple's per-track `primaryGenreName`.

    Free and streaming-tier: the album's edition (explicit edition first — the
    same resolution the advisory route uses) is looked up with `entity=song`,
    and every row carries its own genre. Apple serves it per track, which is
    why this source beats the album-level ones below it. {} when Apple holds
    no such album.
    """
    editions = _apple_editions(artist, album, cfg=cfg)
    cid = _advisory_int((editions[0] if editions else {}).get("collectionId"))
    if not cid:
        return {}
    data = _apple_json("/lookup", {"id": cid, "entity": "song", "limit": 200,
                                   "country": _apple_country(cfg)})
    out = {}
    for row in (data or {}).get("results") or []:
        genre = str(row.get("primaryGenreName") or "").strip()
        position = _advisory_int(row.get("trackNumber"))
        if not genre or not position:
            continue
        key = _genre_track_key(_advisory_int(row.get("discNumber")) or 1, position)
        out[key] = _genre_names((out.get(key) or []) + [genre])
    return out


def _release_artist_mbids(artist, album, release, cfg):
    """The release's artist MBIDs, resolved from the name when it has none."""
    from server import discovery

    ids = [a.get("mbid") for a in (release or {}).get("artists") or []
           if a.get("mbid")]
    if not ids and artist:
        try:
            resolved = discovery.resolve_artist_mbid(artist)
        except Exception:
            resolved = None
        if resolved:
            ids.append(resolved)
    return ids


def _genre_source_answers(source, artist, album, release, cfg, tracks):
    """One source's answers: {key: {"level", "genres"[, "title"]}}.

    `key` is a track key for a per-track answer, `_ALL_TRACKS` for an
    album/artist-wide one; both may be present and `genre_chain` merges the
    track's own answer first. Every source is asked at its best available
    level — a per-track source that cannot answer per track answers nothing
    rather than promoting an album guess to a track. Never raises: the chain
    wraps the call, and every network path inside returns "no answer".
    """
    from server import discovery

    if source == "rateyourmusic":
        data = rym_genres(artist, album) or rym_artist_genres(artist)
        if not data:
            return {}
        wide = _genre_row(data.get("level") or "artist",
                          (data.get("genres") or []) + (data.get("descriptors") or []))
        answers = {_ALL_TRACKS: wide} if wide else {}
        for row in data.get("tracks") or []:
            # RYM renders one medium per page, so the row number is the
            # position on disc 1; `genre_chain` re-maps by title when that
            # does not line up with this release's own numbering.
            own = _genre_row("track", row.get("genres"), row.get("title"))
            if own:
                answers[_genre_track_key(1, row.get("position"))] = own
        return answers

    if source == "listenbrainz":
        answers = {}
        for track in tracks:
            recording = track.get("recording_mbid")
            if not recording:
                continue
            got = _genre_cached(
                "recording", f"listenbrainz|{recording}",
                lambda r=recording: discovery.listenbrainz_genre_tags(r, "recording"))
            row = _genre_row("track", ((got or {}).get("genres") or [])
                             + ((got or {}).get("tags") or []))
            if row:
                answers[_genre_track_key(track.get("disc"), track.get("position"))] = row
        rg_names = []
        rg = (release or {}).get("release_group_id")
        if rg:
            got = _genre_cached(
                "album", f"listenbrainz|release_group|{rg}",
                lambda: discovery.listenbrainz_genre_tags(rg, "release_group"))
            rg_names = ((got or {}).get("genres") or []) + ((got or {}).get("tags") or [])
        artist_names = []
        for mbid in _release_artist_mbids(artist, album, release, cfg):
            got = _genre_cached(
                "album", f"listenbrainz|artist|{mbid}",
                lambda m=mbid: discovery.listenbrainz_genre_tags(m, "artist"))
            artist_names += ((got or {}).get("genres") or []) + ((got or {}).get("tags") or [])
            break
        wide = _genre_row("album" if rg_names else "artist", rg_names + artist_names)
        if wide:
            answers[_ALL_TRACKS] = wide
        return answers

    if source == "musicbrainz":
        answers = {}
        for track in tracks:
            row = _genre_row("track", track.get("genres"))
            if row:
                answers[_genre_track_key(track.get("disc"), track.get("position"))] = row
        wide, level = [], "album"
        if release:
            wide += list(release.get("genres") or [])
            rg = release.get("release_group_id")
            if rg:
                wide += _genre_cached("album", f"musicbrainz|release_group|{rg}",
                                      lambda r=rg: release_group_genres(r)) or []
            for mbid in _release_artist_mbids(artist, album, release, cfg):
                wide += _genre_cached("album", f"musicbrainz|artist|{mbid}",
                                      lambda m=mbid: artist_genres(m)) or []
                break
        else:
            # No release in hand: the release group and the artist are what a
            # genre cascade normally falls back on, resolved from the names.
            rg = None
            try:
                rg = discovery.resolve_release_group(artist, album, cfg)
            except Exception:
                rg = None
            if rg and rg.get("mbid"):
                wide += _genre_cached("album", f"musicbrainz|release_group|{rg['mbid']}",
                                      lambda m=rg["mbid"]: release_group_genres(m)) or []
            for mbid in _release_artist_mbids(artist, album, release, cfg):
                wide += _genre_cached("album", f"musicbrainz|artist|{mbid}",
                                      lambda m=mbid: artist_genres(m)) or []
                if not (release or {}).get("artists"):
                    level = "artist"
                break
        row = _genre_row(level, wide)
        if row:
            answers[_ALL_TRACKS] = row
        return answers

    if source == "itunes":
        def look():
            per_track = _itunes_track_genres(artist, album, cfg)
            if per_track:
                return {"tracks": per_track}
            # Apple's album search is the fallback when its per-track lookup
            # has nothing: one primary genre for the album (still better than
            # no answer, and the caller marks it album level).
            names = _genre_names([r.get("genre") for r in
                                  discovery.itunes_search_album(artist, album,
                                                                limit=1)])
            return {"album": names} if names else None

        got = _genre_cached(
            "album", f"itunes|album|{_norm_compare(artist)}|{_norm_compare(album)}",
            look) or {}
        answers = {}
        for key, names in (got.get("tracks") or {}).items():
            row = _genre_row("track", names)
            if row:
                answers[key] = row
        row = _genre_row("album", got.get("album"))
        if row:
            answers[_ALL_TRACKS] = row
        return answers

    if source == "wikidata":
        answers = {}
        # The TRACK's own item first, and by identity: the recording's
        # Wikidata relation when it has one, else the Wikidata item of the
        # WORK that recording performs (see `_mb_recording_ids`). Both are
        # `level: track` answers.
        for track in tracks:
            recording = str(track.get("recording_mbid") or "").strip()
            title = str(track.get("title") or "").strip()
            if not recording:
                continue
            ids = _genre_cached("recording", f"wikidata|recording|{recording}",
                                lambda r=recording: _mb_recording_ids(r)) or {}
            qid = str(ids.get("qid") or "")
            work = str(ids.get("work") or "")
            if not qid and work:
                qid = _genre_cached("recording", f"wikidata|work|{work}",
                                    lambda w=work: _mb_wikidata_qid(w, "work")) or ""
            if not qid:
                continue
            got = _genre_cached("recording", f"wikidata|qid|{qid}",
                                lambda q=qid: discovery.wikidata_genres(qid=q)) or {}
            row = _genre_row("track", got.get("genres"), title)
            if row:
                answers[_genre_track_key(track.get("disc"),
                                         track.get("position"))] = row
        # Then the release group's own entity (or the searched "artist album"
        # entity) as today — album level, the fallback tier.
        rg = (release or {}).get("release_group_id") or ""
        term = " ".join(t for t in (artist, album) if t)
        got = _genre_cached(
            "album", f"wikidata|{rg}|{_norm_compare(term)}",
            lambda: discovery.wikidata_genres(qid=_mb_wikidata_qid(rg), term=term)) or {}
        row = _genre_row("album", got.get("genres"))
        if row:
            answers[_ALL_TRACKS] = row
        return answers

    if source == "lastfm":
        answers = {}
        for track in tracks:
            title = track.get("title")
            if not title:
                continue
            names = _genre_cached(
                "recording", f"lastfm|track|{_norm_compare(artist)}|{_norm_compare(title)}",
                lambda t=title: discovery.lastfm_track_genres(artist, t, cfg)) or []
            row = _genre_row("track", names)
            if row:
                answers[_genre_track_key(track.get("disc"), track.get("position"))] = row
        names = _genre_cached("album", f"lastfm|artist|{_norm_compare(artist)}",
                              lambda: discovery.lastfm_artist_genres(artist, cfg)) or []
        row = _genre_row("artist", names)
        if row:
            answers[_ALL_TRACKS] = row
        return answers

    if source == "discogs":
        names = _genre_cached("album", f"discogs|{_norm_compare(artist)}|{_norm_compare(album)}",
                              lambda: discovery.discogs_album_genres(artist, album, cfg)) or []
        row = _genre_row("album", names)
        return {_ALL_TRACKS: row} if row else {}

    if source == "theaudiodb":
        answers = {}
        # `searchtrack.php` first — TheAudioDB serves a track row (genre,
        # style, mood) per track, and it is the source's per-track tier.
        for track in tracks:
            title = str(track.get("title") or "").strip()
            if not title:
                continue
            names = _genre_cached(
                "recording",
                f"theaudiodb|track|{_norm_compare(artist)}|{_norm_compare(title)}",
                lambda t=title: _audiodb_track_genre_names(artist, t)) or []
            row = _genre_row("track", names, title)
            if row:
                answers[_genre_track_key(track.get("disc"),
                                         track.get("position"))] = row
        # The album row stays as the fallback tier for the tracks it did not
        # answer for (and for a release whose tracks have no names yet).
        names = _genre_cached("album", f"theaudiodb|{_norm_compare(artist)}|{_norm_compare(album)}",
                              lambda: _audiodb_genre_names(artist, album)) or []
        row = _genre_row("album", names)
        if row:
            answers[_ALL_TRACKS] = row
        return answers

    if source == "bandcamp":
        # Album-level by nature (see the Bandcamp section above): its own
        # track titles confirm the page is this release, and the tags are the
        # album's. One request per album, cached 30 days like every source.
        titles = [t.get("title") for t in tracks if str(t.get("title") or "").strip()]
        got = _genre_cached(
            "album", f"bandcamp|{_norm_compare(artist)}|{_norm_compare(album)}",
            lambda: bandcamp_album(artist, album, titles)) or {}
        row = _genre_row("album", got.get("genres"))
        return {_ALL_TRACKS: row} if row else {}

    if source == "deezer":
        names = _genre_cached("album", f"deezer|{_norm_compare(artist)}|{_norm_compare(album)}",
                              lambda: discovery.album_genres(artist, album, cfg=cfg)) or []
        row = _genre_row("album", names)
        return {_ALL_TRACKS: row} if row else {}

    if source == "spotify":
        # Artist-level, honest about it: Spotify states no per-track genre at
        # all. Skipped entirely (no request) until its credentials are set.
        if not _spotify_configured(cfg):
            return {}
        names = _genre_cached(
            "album", f"spotify|artist|{_norm_compare(artist)}",
            lambda: discovery.spotify_artist_genres(artist, cfg)) or []
        row = _genre_row("artist", names)
        return {_ALL_TRACKS: row} if row else {}

    if source == "soulseek":
        # Peers advertise folders and file names, not genres. Nothing here can
        # state a genre, so this source is a no-op by design — it stays in the
        # order so a saved config listing it keeps working.
        return {}
    return {}


def _audiodb_genre_names(artist, album):
    """TheAudioDB's album genre + mood (its two genre-ish fields)."""
    from server import discovery

    row = discovery.audiodb_album(artist, album) or {}
    return [g for g in (row.get("genre"), row.get("mood")) if g]


def _audiodb_track_genre_names(artist, title):
    """TheAudioDB's genre + style for ONE track, [] when it states none.

    `strMood` is a mood word ("Sad") and never a genre, so it is left out of
    the per-track row exactly as ListenBrainz's mood tags are.
    """
    from server import discovery

    row = discovery.audiodb_track(artist, title) or {}
    return [g for g in (row.get("genre"), row.get("style")) if g]


def _answer_by_title(answers, title):
    """The per-track answer whose own title is ours (the fallback mapping).

    RYM's rows carry the release page's titles; when its numbering does not
    line up with ours the title is what maps them. An unmatched title yields
    None — never a neighbouring track's genres.
    """
    want = _norm_compare(title)
    if not want:
        return None
    for key, row in answers.items():
        if key == _ALL_TRACKS or not row.get("title"):
            continue
        if _norm_compare(row["title"]) == want:
            return row
    return None


def genre_chain(artist="", album="", release=None, limit=None, sources=None,
                cfg=None, files=None, progress=None):
    """Per-track genres for a release, merged from the configured sources.

    Every source is asked for EVERY track; the default order (the priority
    list documented above `GENRE_SOURCES`) is: RateYourMusic (per track where
    its page states one, else album) → ListenBrainz (recording → release group
    → artist) → MusicBrainz (recording → release → release group → artist) →
    iTunes (`primaryGenreName`, per track) → Last.fm (track → artist) →
    TheAudioDB (`searchtrack.php` per track → album) → Wikidata (the
    recording's P136 → release group/entity) → Bandcamp (album tags) →
    Discogs (release styles) → Deezer (album genres) → Spotify (artist
    genres). `mlo.config` migrates both previously shipped default lists onto
    this one, so an install that never chose an order gets it; a customised
    list is honoured as written.

    Every source that answers contributes; the merged list is deduped
    case-insensitively, Title-Cased and capped at `limit` (default
    `mb_genre_count`, now 3) **per track**.

    Each track's own answer is merged first, then the release-wide one, so a
    track that states its own genre keeps it ahead of the album's fallback.
    A source that only knows album- or artist-wide answers (Bandcamp, Discogs,
    Deezer, Spotify, and RYM/TheAudioDB/Wikidata when their per-track tier is
    silent) is marked `level: "album"`/`"artist"` in the provenance, never
    `track` — nothing pretends to be per-track.

    `files` (optional) are the album's audio files; they key the provenance
    maps by path (`sources`, `levels`), which is what the UI shows. A source
    that cannot answer (RYM blocked, no Discogs/Last.fm key, no Wikidata
    statement, a timeout) contributes nothing and is reported in `notes` — it
    is never filled in from a guess.

    `progress` (optional) is called as ``progress(i, total, source)`` before
    each source is asked, so a caller can show which source the chain is
    waiting on; a hook that raises is ignored.

    Returns {"genres": [...], "per_track": {(disc, position): [...]},
    "per_track_sources": {...}, "per_track_levels": {...},
    "sources": {path: [source, ...]}, "levels": {path: "track"|"album"|
    "artist"}, "per_source": {source: [...]}, "notes": {source: reason}}.
    """
    if cfg is None:
        try:
            from mlo.config import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
    order = [str(s).strip().lower()
             for s in (sources if sources is not None
                       else (cfg.get("genre_sources") or GENRE_SOURCES))
             if str(s).strip()]
    if limit is None:
        try:
            limit = max(1, int(cfg.get("mb_genre_count") or 3))
        except (TypeError, ValueError):
            limit = 3
    artist = str(artist or "").strip()
    album = str(album or "").strip()
    tracks = list((release or {}).get("media") or [])

    answers_by_source, per_source, notes = {}, {}, {}
    for i, source in enumerate(order, 1):
        if progress is not None:
            # "source i/N", for a caller that shows a live bar: a source can
            # spend seconds on the network, and the hook must never be able
            # to break the chain.
            try:
                progress(i, len(order), source)
            except Exception:
                pass
        try:
            answers = _genre_source_answers(source, artist, album, release,
                                            cfg, tracks) or {}
        except Exception as e:
            notes[source] = f"failed: {e}"
            continue
        if not answers:
            notes[source] = "no data"
            continue
        answers_by_source[source] = answers
        # The release-wide answer first, then the per-track ones: this list is
        # the album summary the caller may still want for a release whose
        # tracks carry no identity of their own.
        every = []
        for key in sorted(answers, key=lambda k: (k != _ALL_TRACKS, k)):
            every += answers[key].get("genres") or []
        per_source[source] = _genre_names(every)

    def merge(track):
        """(genres, sources, level) in ask order.

        `track=None` is the release-wide answer (every row every source has,
        the release-wide ones first); for a track it is that track's own
        answer, then the release-wide one.
        """
        pairs = []
        for source in order:
            answers = answers_by_source.get(source) or {}
            if track is None:
                rows = [answers[key] for key in
                        sorted(answers, key=lambda k: (k != _ALL_TRACKS, k))]
            else:
                rows = []
                key = _genre_track_key(track.get("disc"), track.get("position"))
                row = answers.get(key) or _answer_by_title(answers, track.get("title"))
                if row:
                    rows.append(row)
                wide = answers.get(_ALL_TRACKS)
                if wide and wide is not row:
                    rows.append(wide)
            for row in rows:
                for name in row.get("genres") or []:
                    pairs.append((str(name).strip().title(), source,
                                  row.get("level") or "album"))
        kept, seen = [], set()
        for name, source, level in pairs:
            low = name.lower()
            if not name or low in seen:
                continue
            seen.add(low)
            kept.append((name, source, level))
        kept = kept[:limit]
        level = None
        for _name, _source, got in kept:
            if level is None or _LEVEL_RANK.get(got, 9) < _LEVEL_RANK.get(level, 9):
                level = got
        return ([n for n, _s, _l in kept],
                list(dict.fromkeys(s for _n, s, _l in kept)), level)

    per_track, per_track_sources, per_track_levels = {}, {}, {}
    for track in tracks:
        genres, contributors, level = merge(track)
        if not genres:
            continue
        key = (int(track.get("disc") or 1), int(track.get("position") or 0))
        per_track[key] = genres
        per_track_sources[key] = contributors
        per_track_levels[key] = level

    merged, album_contributors, album_level = merge(None)
    out_sources, out_levels = {}, {}
    for path in files or []:
        key = None
        try:
            from server import soulseek_auto
            key = soulseek_auto._parse_trackno(str(path))
        except Exception:
            key = None
        if key in per_track_sources:
            out_sources[str(path)] = list(per_track_sources[key])
            out_levels[str(path)] = per_track_levels.get(key)
        else:
            # No release identity to key a track on (the Auto-tagging hook has
            # only the file): the release-wide answer is what that file gets,
            # and it says so.
            out_sources[str(path)] = list(album_contributors)
            out_levels[str(path)] = album_level

    return {"genres": merged[:limit], "per_track": per_track,
            "per_track_sources": per_track_sources,
            "per_track_levels": per_track_levels,
            "sources": out_sources, "levels": out_levels,
            "per_source": per_source, "notes": notes}



# Fixed genre buckets for GET /api/genres/facets. Every library genre lands in
# exactly one bucket (the first keyword match wins, "Other" for the rest) so
# the UI can offer coarse filters without carrying a full taxonomy. Order
# matters: "folk metal" must count as Metal before Folk sees it.
GENRE_CATEGORIES = [
    ("Metal", ("metal", "doom", "sludge", "djent", "thrash", "grindcore",
               "deathcore", "metalcore", "blackgaze")),
    ("Rock", ("rock", "grunge", "punk", "shoegaze", "psychedelic", "emo",
              "indie", "britpop", "garage", "gothic", "post-punk", "surf")),
    ("Electronic", ("electronic", "techno", "house", "trance", "ambient",
                    "idm", "drum and bass", "dnb", "dubstep", "synth", "electro",
                    "breakbeat", "edm", "downtempo", "dub", "jungle", "glitch")),
    ("Hip-Hop", ("hip hop", "hip-hop", "rap", "trap", "grime", "boom bap")),
    ("Jazz", ("jazz", "bebop", "swing", "fusion", "bossa")),
    ("Classical", ("classical", "baroque", "romantic", "opera", "chamber",
                   "symphony", "orchestral", "choral", "medieval", "renaissance")),
    ("Folk", ("folk", "country", "bluegrass", "celtic", "world", "americana",
              "traditional")),
    ("Soul & Funk", ("soul", "funk", "r&b", "rhythm and blues", "disco",
                     "motown", "gospel")),
    ("Pop", ("pop", "vocal", "ballad", "schlager")),
]
OTHER_CATEGORY = "Other"


def genre_category(name):
    """The fixed bucket a genre name belongs to (never None)."""
    text = str(name or "").strip().lower()
    if not text:
        return OTHER_CATEGORY
    for category, keys in GENRE_CATEGORIES:
        if any(k in text for k in keys):
            return category
    return OTHER_CATEGORY


def search_releases(query, limit=10, mode="release"):
    """Release search with multiple strategies.

    mode:
      * release  — free-text title/artist search
      * track    — search by track title
      * catno    — search by catalog number  (catno:"CK 62240")
      * barcode  — search by barcode         (barcode:074643924526)
    """
    q = (query or "").strip()
    if not q:
        return []
    if mode == "catno":
        q = f'catno:"{q}"'
    elif mode == "barcode":
        q = f"barcode:{q}"
    elif mode == "track":
        q = f'track:"{q}"'
    try:
        data = mb_get_cached("release", {"query": q, "limit": limit, "fmt": "json"})
        out = []
        for r in data.get("releases", []):
            credit = "".join(
                (ac.get("name", "") + (ac.get("joinphrase", "") or ""))
                for ac in r.get("artist-credit", [])
            )
            label = ""
            for li in r.get("label-info", []) or []:
                if li.get("catalog-number"):
                    label = li["catalog-number"]
                    break
            out.append({
                "id": r.get("id"),
                "title": r.get("title"),
                "date": r.get("date"),
                "artist": credit,
                "country": r.get("country"),
                "status": r.get("status"),
                "catalog_number": label,
                "barcode": r.get("barcode") or "",
            })
        return out
    except Exception as e:
        return {"error": str(e)}


def search_artists(query, limit=5):
    try:
        data = mb_get_cached("artist", {"query": query, "limit": limit, "fmt": "json"})
        return [{"id": a.get("id"), "name": a.get("name"), "type": a.get("type")}
                for a in data.get("artists", [])]
    except Exception as e:
        return {"error": str(e)}


# --------------------------------------------------------------------------- #
# Generic browse: search + artist / release-group / recording pages
# (used by the in-app MusicBrainz browser)
# --------------------------------------------------------------------------- #
MB_ENTITIES = ("artist", "release-group", "release", "recording")


def _credit(node):
    return "".join(
        (ac.get("name", "") + (ac.get("joinphrase", "") or ""))
        for ac in (node.get("artist-credit") or [])
    )


def _rg_types(node):
    """(primary_type, secondary_types) of a node's release group.

    MusicBrainz embeds the release group as an OBJECT for release SEARCH
    results but as a bare id STRING for browse results (inc=release-groups);
    an id, or anything else unexpected, must read as "type unknown" instead
    of raising AttributeError and failing the whole page."""
    rg = node.get("release-group") if isinstance(node, dict) else None
    if not isinstance(rg, dict):
        return "", []
    return ((rg.get("primary-type") or ""),
            [s for s in (rg.get("secondary-types") or []) if s])


def _isrc(node):
    """The ISRC string of an isrcs entry (an entity wraps it, a recording
    lookup returns it bare)."""
    if isinstance(node, dict):
        return str(node.get("isrc") or "")
    return str(node or "")


def _media_summary(node):
    """'2×CD + DVD' style summary of an entity's media list."""
    parts = []
    for m in node.get("media") or []:
        fmt = m.get("format") or "Unknown"
        if parts and parts[-1][0] == fmt:
            parts[-1][1] += 1
        else:
            parts.append([fmt, 1])
    return " + ".join((f"{n}×{f}" if n > 1 else f) for f, n in parts)


def _release_counts(node):
    """Track/disc numbers for a release: (total, per-disc breakdown).

    The breakdown keeps one number per medium joined with ' + ' — a two-disc
    edition with 10 then 11 tracks reads '10 + 11'; a single disc collapses
    to its plain count."""
    counts = [(m.get("track-count") or 0) for m in node.get("media") or []]
    total = sum(counts)
    breakdown = " + ".join(str(c) for c in counts) if len(counts) > 1 else str(total)
    return total, breakdown


def search_mb(entity, query, limit=100, mode="free", offset=0,
              primary_type="", secondary_type=""):
    """Normalized MB search rows for the four browsable entities.

    mode="free" is the plain full-text search; for releases, mode="catno" /
    "barcode" search by catalog number / barcode (catalog numbers like
    'SRCS 8757' are how pressings are identified). primary_type /
    secondary_type narrow releases and release groups with MusicBrainz's own
    type qualifiers — the only way to ask the index for "albums that are
    soundtracks" instead of filtering the rows afterwards. Returns
    {rows, total} — total is MusicBrainz's match count so the UI can offer
    deeper paging (searches cap at 100 rows per request)."""
    if entity not in MB_ENTITIES:
        raise ValueError("entity must be artist, release-group, release or recording")
    q = query
    if entity == "release" and mode == "catno":
        q = f'catno:"{query}"'
    elif entity == "release" and mode == "barcode":
        q = f"barcode:{query}"
    if entity in ("release", "release-group"):
        # quoted: several secondary types are multi-word ("Audio drama",
        # "DJ-mix", "Field recording")
        if primary_type:
            q = f'{q} AND primarytype:"{primary_type}"'
        if secondary_type:
            q = f'{q} AND secondarytype:"{secondary_type}"'
    data = mb_get_cached(entity, {"query": q, "limit": limit, "offset": offset, "fmt": "json"})
    # MB search responses use plural collection keys
    key = {"artist": "artists", "release-group": "release-groups",
           "release": "releases", "recording": "recordings"}[entity]
    rows = []
    for item in data.get(key, []):
        row = {
            "id": item.get("id"),
            "score": item.get("score"),
            "title": item.get("title") or item.get("name"),
            "disambiguation": item.get("disambiguation") or "",
        }
        if entity == "artist":
            area = item.get("area") or {}
            row.update({
                "type": item.get("type") or "",
                "country": area.get("name") or "",
                "life_span": [
                    (item.get("life-span") or {}).get("begin") or "",
                    (item.get("life-span") or {}).get("end") or "",
                ],
                "tags": [t.get("name") for t in (item.get("tags") or [])[:3]],
            })
        elif entity == "release-group":
            row.update({
                "artist": _credit(item),
                "primary_type": item.get("primary-type") or "",
                "secondary_types": [s for s in (item.get("secondary-types") or [])],
                "first_release_date": item.get("first-release-date") or "",
            })
        elif entity == "release":
            catalog_number = ""
            for li in item.get("label-info") or []:
                if li.get("catalog-number"):
                    catalog_number = li["catalog-number"]
                    break
            # MB's release search embeds the release group with its PRIMARY
            # type only (the search index carries no secondary types) — that
            # is still what separates an album pressing from a single/EP.
            # `_rg_types` keeps a bare-id embed from crashing the request.
            rg_primary, rg_secondary = _rg_types(item)
            row.update({
                "artist": _credit(item),
                "date": item.get("date") or "",
                "country": item.get("country") or "",
                "status": item.get("status") or "",
                "formats": _media_summary(item),
                "track_count": sum((m.get("track-count") or 0) for m in item.get("media") or []),
                "catalog_number": catalog_number,
                "primary_type": rg_primary,
                "secondary_types": rg_secondary,
            })
        else:  # recording
            row.update({
                "artist": _credit(item),
                "length": item.get("length"),
                "first_release_date": item.get("first-release-date") or "",
            })
        rows.append(row)
    return {"rows": rows, "total": data.get("count") or len(rows)}


def artist_identity(mbid):
    """An artist's identity: name, area, life span, genres, tags.

    Deliberately split from the discography: MusicBrainz answers one request
    per second, so the artist page paints this header while the release
    groups are still being collected."""
    data = mb_get_cached(f"artist/{mbid}", {"inc": "genres", "fmt": "json"})
    area = data.get("area") or {}
    return {
        "id": data.get("id"),
        "name": data.get("name"),
        "disambiguation": data.get("disambiguation") or "",
        "type": data.get("type") or "",
        "country": area.get("name") or "",
        "life_span": [
            (data.get("life-span") or {}).get("begin") or "",
            (data.get("life-span") or {}).get("end") or "",
        ],
        "genres": _title_genres(data),
        "tags": [t.get("name") for t in (data.get("tags") or [])[:8]],
    }


def artist_release_groups(mbid, limit=100, offset=0):
    """One page of an artist's release groups, oldest first.

    The discography comes from the *browse* endpoint (release-group?artist=…)
    rather than a lookup's inc= subquery — lookups silently cap the related
    list. It has NO server-side sort, so a single arbitrary 100-row slice
    misrepresents a discography: `_browse_collect` walks the pages (still
    1 req/s) up to `limit` rows so the caller sorts an honest window."""
    rgs, total = _browse_collect(
        "release-group", {"artist": mbid}, "release-groups", "release-group-count",
        limit=limit, offset=offset,
    )
    return {
        "total": total,
        "offset": offset,
        "release_groups": [
            {
                "id": rg.get("id"),
                "title": rg.get("title"),
                "primary_type": rg.get("primary-type") or "",
                "secondary_types": rg.get("secondary-types") or [],
                "first_release_date": rg.get("first-release-date") or "",
            }
            for rg in sorted(
                rgs,
                key=lambda g: g.get("first-release-date") or "9999",
            )
        ],
    }


def artist_browse(mbid, limit=300, offset=0):
    """Identity + a page of release groups together — the auto-import path
    wants both at once, the artist page does not (see artist_identity)."""
    return {**artist_identity(mbid), **artist_release_groups(mbid, limit, offset)}


def _release_policy():
    """(avoid_promo, medium_order, require_country) from config, safe defaults."""
    from mlo.config import DEFAULT_CONFIG, load_config
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    avoid = bool(cfg.get("auto_import_avoid_promo", True))
    country = bool(cfg.get("auto_import_require_country", True))
    order = cfg.get("auto_import_medium_order") or DEFAULT_CONFIG["auto_import_medium_order"]
    return avoid, [str(x).strip().lower() for x in order if str(x).strip()], country


# MusicBrainz release statuses that must never be auto-picked while
# avoid-promo is on: a promo/bootleg/pseudo edition is not the album.
_PROMO_STATUSES = {"promotion", "bootleg", "pseudo-release", "pseudo release"}
# Withdrawn/expired/cancelled editions still exist on MusicBrainz, but the
# label pulled them: they sort below a plain release and above a promo.
_NEGATIVE_STATUSES = {"withdrawn", "expired", "cancelled", "canceled"}


def _medium_names(rel):
    """Format names a release carries ('CD', 'Digital Media')."""
    media = rel.get("media")
    if media:
        return [str(m.get("format") or "") for m in media if isinstance(m, dict)]
    fmts = rel.get("formats")
    if isinstance(fmts, list):
        return [str(f) for f in fmts]
    out = []
    for part in str(fmts or "").split(" + "):
        part = part.strip()
        if "×" in part:
            part = part.split("×", 1)[-1]
        if part:
            out.append(part)
    return out


def release_medium_rank(rel, medium_order):
    """Index of the release's best medium in the preference order.

    Unknown formats rank after every configured one but still sort among
    themselves by date."""
    names = [n.lower() for n in _medium_names(rel)]
    best = len(medium_order)
    for n in names:
        for i, pref in enumerate(medium_order):
            if pref and (n == pref or pref in n):
                best = min(best, i)
    return best


def _date_rank(date):
    """(year, precision) for the edition sort — the earlier and the FULLER
    date wins.

    The album folder is named after the release's own date ("[Album]
    1980-10-01 - 1983-09-13 - …"), so an edition MusicBrainz dates only to the
    year leaves the folder with a year for good. Comparing the date STRING
    put "1983" before "1983-09-13" (a prefix sorts first), which preferred
    exactly the edition that cannot fill the folder in. The year stays the
    primary term — an earlier pressing still wins — and precision breaks the
    tie: full date, then year-month, then year, then an edition with no date
    at all.
    """
    text = str(date or "").strip()
    if not text:
        return (9999, 3)
    year = text.split("-")[0]
    try:
        y = int(year)
    except ValueError:
        return (9999, 3)
    return (y, {10: 0, 7: 1}.get(len(text), 2))


def release_choice_key(rel, avoid_promo=True, medium_order=None):
    """Sort key: Official first, then medium preference, then earliest date.

    Negative traits are counted against a release instead of being invisible
    to the sort: `official` ranks 0, a release whose status MusicBrainz does
    not state ranks 1, a withdrawn/expired/cancelled edition ranks 2, and a
    promotional/bootleg/pseudo edition ranks 3 (`pick_releases` drops those
    entirely while avoid-promo is on). An edition carrying a RELEASECOUNTRY
    beats one that does not, whichever status the two share. The date term
    prefers the earlier edition, and among editions of the same year the one
    that states its date in full (see `_date_rank`)."""
    status = str(rel.get("status") or "").strip().lower()
    if status == "official":
        rank = 0
    elif status in _PROMO_STATUSES and avoid_promo:
        rank = 3
    elif status in _NEGATIVE_STATUSES:
        rank = 2
    else:
        rank = 1
    date = rel.get("date") or ""
    return (rank, 0 if str(rel.get("country") or "").strip() else 1,
            release_medium_rank(rel, medium_order or []),
            _date_rank(date), date or "9999")


def pick_releases(releases, cfg=None):
    """Releases ordered by the auto-import release policy (best first).

    The ineligible are dropped rather than ranked so no auto-import path can
    queue one: promotional / bootleg / pseudo editions while mlo.config
    `auto_import_avoid_promo` is on, and editions with no RELEASECOUNTRY while
    `auto_import_require_country` is on."""
    if cfg is None:
        avoid, order, country = _release_policy()
    else:
        avoid = bool(cfg.get("auto_import_avoid_promo", True))
        country = bool(cfg.get("auto_import_require_country", True))
        order = [str(x).strip().lower()
                 for x in (cfg.get("auto_import_medium_order") or []) if str(x).strip()]

    def usable(rel):
        status = str(rel.get("status") or "").strip().lower()
        if avoid and status in _PROMO_STATUSES:
            return False
        if country and not str(rel.get("country") or "").strip():
            return False
        return True

    kept = [r for r in (releases or []) if usable(r)]
    kept.sort(key=lambda r: release_choice_key(r, avoid, order))
    return kept


def pick_release(releases, cfg=None):
    """The single best release of a group per `pick_releases`, or None."""
    kept = pick_releases(releases, cfg)
    return kept[0] if kept else None


def resolve_release(mbid):
    """(release, release_mbid) for a release id, a release-group id or a URL.

    Auto-import works on a *release* — a concrete pressing with a track list
    — while the ids pasted into a wish or an import are usually release
    GROUPS, and a group id passed to the release endpoint 404s. Group ids
    are resolved to their best edition via the release-choice policy so no
    caller (HTTP route, wishes worker, bulk import) can queue a group job.
    Returns (None, mbid) when the id is a group with no usable edition, and
    (None, None) when nothing matches at all. A MusicBrainz OUTAGE is not
    "nothing matches" — that raises MusicBrainzError so the caller reports
    "MusicBrainz is busy" instead of claiming the release does not exist."""
    rid = _mbid(mbid)
    if not rid:
        return None, None
    try:
        return release_lookup(rid), rid
    except MusicBrainzError:
        raise
    except Exception:
        pass
    try:
        rg = release_group_browse(rid, limit=100, offset=0)
    except MusicBrainzError:
        raise
    except Exception:
        return None, None
    best = pick_release(rg.get("releases") or []) if rg.get("id") else None
    if not best or not best.get("id"):
        return None, rid
    try:
        return release_lookup(best["id"]), best["id"]
    except MusicBrainzError:
        raise
    except Exception:
        return None, rid


_NO_EDITION = ("no edition eligible for auto-import (promotional/bootleg "
               "editions are skipped while auto_import_avoid_promo is on, and "
               "editions with no release country while "
               "auto_import_require_country is on)")
# Release groups one bulk call expands: each costs a MusicBrainz browse
# (1 req/s), so an artist with hundreds of groups would take minutes — the
# remainder is reported as skipped instead of silently dropped or wedging.
BULK_MAX_GROUPS = 50


def group_targets(rg_mbid, mode):
    """([{mbid,title}], error) — the release ids of a release group to queue.

    Ordered by the auto-import policy (Official, then medium preference, then
    earliest date); `mode` "best" keeps only the best edition, "all" keeps
    every eligible edition. `error` is a readable reason and never an
    exception, so one unusable group cannot abort a whole discography."""
    try:
        rg = release_group_browse(rg_mbid, limit=100, offset=0)
    except Exception as e:
        return [], f"MusicBrainz release-group lookup failed: {e}"
    if not rg.get("id"):
        return [], "not a MusicBrainz release group"
    rows = pick_releases(rg.get("releases") or [])
    if not rows:
        return [], _NO_EDITION
    if mode != "all":
        rows = rows[:1]
    return [{"mbid": r.get("id"), "title": r.get("title") or ""} for r in rows], None


def _kind_for(mbid):
    """release / release_group / artist for an ID that did not say which."""
    t = (detect_mbid(mbid) or {}).get("type") or ""
    return {"release-group": "release_group"}.get(t, t) or None


def auto_import_targets(mbid, kind=None, mode="best"):
    """([{mbid,title}], [{mbid,reason}]) — what a bulk auto-import should queue.

    ONE resolution path, shared by the HTTP route's bounded quick attempt and
    by the auto-import job that redoes the whole thing when that attempt did
    not finish: a release resolves to the edition the policy picks, a release
    group to its best (or every eligible) edition, an artist to one best
    release per release group it does not already own. A MusicBrainz outage
    raises MusicBrainzError — reported per item, never as "does not exist".
    """
    mode = "all" if str(mode or "").lower() == "all" else "best"
    kind = str(kind or "auto").strip().lower()
    if kind in ("", "auto"):
        kind = _kind_for(mbid) or "release"
    # "Already in the library" is checked for every kind, not just artists:
    # queuing a release or a group the library already holds downloaded the
    # same album a second time, and the duplicate then landed beside it.
    from mlo.config import load_config
    from server import wishes
    owned = wishes.owned_mbids(load_config())
    if kind == "release":
        rel, rid = resolve_release(mbid)
        if not rid:
            return [], [{"mbid": mbid, "reason": "no MusicBrainz release or "
                                                 "release group matches this ID"}]
        if not rel:
            return [], [{"mbid": rid, "reason": _NO_EDITION}]
        rg = str(rel.get("release_group_id") or "").strip().lower()
        if rg and rg in owned:
            return [], [{"mbid": rid, "reason": "already in the library"}]
        return [{"mbid": rid, "title": rel.get("title") or ""}], []
    if kind == "release_group":
        if str(_mbid(mbid) or "").lower() in owned:
            return [], [{"mbid": mbid, "reason": "already in the library"}]
        rows, err = group_targets(mbid, mode)
        return rows, ([{"mbid": mbid, "reason": err}] if err else [])
    if kind != "artist":
        return [], [{"mbid": mbid, "reason": f"unknown MusicBrainz kind {kind!r}"}]

    artist = artist_browse(mbid, limit=500, offset=0)
    groups = artist.get("release_groups") or []
    if not groups:
        return [], [{"mbid": mbid,
                     "reason": "this artist has no release groups on MusicBrainz"}]
    rows, skipped, done = [], [], 0
    for g in groups:
        gid = str(g.get("id") or "")
        if not gid:
            continue
        if gid.lower() in owned:
            skipped.append({"mbid": gid, "reason": "already in the library"})
            continue
        if done >= BULK_MAX_GROUPS:
            skipped.append({"mbid": gid,
                            "reason": f"per-call limit of {BULK_MAX_GROUPS} "
                                      "release groups reached — call again"})
            continue
        sub, err = group_targets(gid, "best")
        done += 1
        if err:
            skipped.append({"mbid": gid, "reason": err})
            continue
        rows.extend(sub)
    return rows, skipped


def release_group_browse(mbid, limit=300, offset=0):
    """Release-group page: identity + its releases (editions), each with
    media so every row carries format, disc count and its '10 + 11' track
    breakdown. Releases come from the browse endpoint (collected across
    pages) because the lookup's release subquery both truncates and omits
    media."""
    data = mb_get_cached(
        f"release-group/{mbid}",
        {"inc": "artist-credits+genres", "fmt": "json"},
    )
    rel_rows, total = _browse_collect(
        "release", {"release-group": mbid, "inc": "media"}, "releases", "release-count",
        limit=limit, offset=offset,
    )
    releases = []
    avoid, medium_order, _require_country = _release_policy()
    for r in sorted(rel_rows,
                    key=lambda r: release_choice_key(r, avoid, medium_order)):
        track_count, track_breakdown = _release_counts(r)
        releases.append({
            "id": r.get("id"),
            "title": r.get("title"),
            "date": r.get("date") or "",
            "country": r.get("country") or "",
            "status": r.get("status") or "",
            "medium": r.get("format") or "",
            "formats": _media_summary(r),
            "disc_count": len(r.get("media") or []),
            "track_count": track_count,
            "track_breakdown": track_breakdown,
            "barcode": r.get("barcode") or "",
        })
    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "disambiguation": data.get("disambiguation") or "",
        "artist": _credit(data),
        "artist_mbid": next(
            (ac["artist"]["id"] for ac in data.get("artist-credit") or [] if "artist" in ac), None
        ),
        "primary_type": data.get("primary-type") or "",
        "secondary_types": data.get("secondary-types") or [],
        "genres": _title_genres(data),
        "first_release_date": data.get("first-release-date") or "",
        "total": total,
        "offset": offset,
        "releases": releases,
    }


# --------------------------------------------------------------------------- #
# Track matching (local files <-> release media)
# --------------------------------------------------------------------------- #
def match_tracks(local_tracks, release):
    """Suggest release track/disc for each local track.

    local_tracks: list of {path, file, tracknumber, discnumber, title, duration}
    Matching: exact (disc,position) hit first, then title-similarity fallback.
    """
    release_media = release.get("media", [])
    by_pos = {(m["disc"], m["position"]): m for m in release_media}
    suggestions = []
    for lt in local_tracks:
        tn = lt.get("tracknumber")
        dn = lt.get("discnumber") or 1
        match = None
        score = 0.0
        if tn is not None:
            m = by_pos.get((dn, tn)) or by_pos.get((1, tn))
            if m:
                match, score = m, 1.0
        if match is None and lt.get("title"):
            best, best_score = None, 0.0
            kind = title_variant_kind(lt["title"])
            for m in release_media:
                # A variant of the track (instrumental/karaoke/…) is another
                # recording: it may never be suggested as this file's track,
                # whatever the name similarity says.
                got = title_variant_kind(m.get("title"))
                if (kind or got) and kind != got:
                    continue
                s = _similarity(lt["title"], m.get("title", ""))
                if s > best_score:
                    best, best_score = m, s
            if best and best_score >= 0.6:
                match, score = best, best_score
        suggestions.append({
            "local": lt.get("path"),
            "file": lt.get("file"),
            "matched": match is not None,
            "confidence": score,
            "release_track": match,
        })
    return suggestions


def _similarity(a, b):
    import difflib
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


# --------------------------------------------------------------------------- #
# LRCLIB
# --------------------------------------------------------------------------- #
_LRCLIB_HEADERS = {"User-Agent": USER_AGENT}
_lrclib_last = 0.0
_lrclib_lock = threading.Lock()


def _lrclib_get(endpoint, params, timeout=15, retries=3):
    """Rate-throttled LRCLIB GET with retry on 429/5xx (they throttle IPs)."""
    global _lrclib_last
    for attempt in range(retries):
        with _lrclib_lock:
            elapsed = time.time() - _lrclib_last
            if elapsed < 0.4:
                time.sleep(0.4 - elapsed)
            r = httpx.get(f"{LRCLIB_BASE}/{endpoint}", params=params,
                          headers=_LRCLIB_HEADERS, timeout=timeout)
            _lrclib_last = time.time()
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
            time.sleep(2.0 * (attempt + 1))
            continue
        return r
    return r


def lrclib_search(artist, track, album=None, duration=None):
    params = {"track_name": track, "artist_name": artist}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = duration
    r = _lrclib_get("search", params)
    if r.status_code == 200:
        return r.json()
    if r.status_code in (400, 404):
        return []
    raise httpx.HTTPStatusError(f"lrclib search {r.status_code}", request=r.request, response=r)


def lrclib_get(artist, track, album=None, duration=None):
    """Exact-match lyrics lookup with a search fallback.

    Falls back to /search (preferring synced lyrics, then closest duration)
    when the exact /get comes back empty; 400/404 are treated as not-found.
    """
    params = {"artist_name": artist, "track_name": track}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = duration
    r = _lrclib_get("get", params)
    if r.status_code == 200:
        return r.json()
    if r.status_code in (400, 404):
        # retry the search without the album filter — it can hurt matches
        for album_filter in (None, album):
            try:
                hits = lrclib_search(artist, track, album_filter, duration)
            except Exception:
                hits = []
            if isinstance(hits, list) and hits:
                synced = [h for h in hits if h.get("syncedLyrics")]
                pool = synced or hits
                if duration:
                    pool = sorted(pool, key=lambda h: abs(int(h.get("duration") or 0) - int(duration)))
                return pool[0]
        return None
    raise httpx.HTTPStatusError(f"lrclib get {r.status_code}", request=r.request, response=r)

def lrclib_publish(artist, track, album, duration, plain=None, synced=None):
    """Submit lyrics to LRCLIB (POST /api/publish).

    Delegates to `mlo.lyrics_providers.lrclib_publish` — the engine client
    script 18 publishes with — so the request body, the required User-Agent
    and the throttle that keeps this IP out of LRCLIB's rate limit exist once
    for both the automatic and the manual path. Returns (ok, message);
    "LRCLIB already has this track" is the duplicate answer, not an error.
    """
    from mlo.lyrics_providers import lrclib_publish as _publish

    return _publish(artist, track, album, duration, plain=plain, synced=synced)



# --------------------------------------------------------------------------- #
# RateYourMusic (no public API — link helpers only)
# --------------------------------------------------------------------------- #
# RYM slugs vary (artist/album, album/format, %-encoding, apostrophes…).
# Keep it lenient: any rateyourmusic.com URL is a valid link to store.
RYM_RE = re.compile(r"^https?://(?:www\.)?rateyourmusic\.com/.+$", re.I)


def parse_rym_album_url(url):
    """Validate a RYM URL, returning the canonical URL or None."""
    url = (url or "").strip()
    if RYM_RE.match(url):
        return url
    return None


# What a RYM URL points AT. Every one of these is a valid URL, but only an
# album page belongs in RATEYOURMUSIC_ALBUM: a pasted artist or song page
# stored there would look like a resolved link forever (the import stamp never
# overwrites an existing one) and would block the automatic album lookup.
# `/release/song/` is matched before `/release/`, or a song page would pass as
# an album; every other release type (single, EP, comp…) IS an album.
_RYM_KIND_RES = (
    ("song", re.compile(r"^/(?:release/)?song/", re.I)),
    ("album", re.compile(r"^/release/[^/]+/", re.I)),
    ("artist", re.compile(r"^/artist/", re.I)),
)


def rym_url_kind(url):
    """What a RYM URL points at: "album", "artist", "song" or "other".

    None means it is not a rateyourmusic.com URL at all — the caller's signal
    to refuse it outright; "other" is a real page (a label, a list, a genre)
    that is simply not one of the three link fields the UI writes.
    """
    url = (url or "").strip()
    if not RYM_RE.match(url):
        return None
    path = re.split(r"[?#]", re.sub(r"^https?://(?:www\.)?rateyourmusic\.com",
                                    "", url, count=1, flags=re.I))[0]
    for kind, rx in _RYM_KIND_RES:
        if rx.match(path):
            return kind
    return "other"


# --------------------------------------------------------------------------- #
# covers.musichoarders.xyz (COV) — album cover meta-search
# --------------------------------------------------------------------------- #
# COV aggregates cover art from streaming services and databases. Its search
# endpoint is the same one the website's frontend calls: a POST that streams
# newline-delimited JSON events (source/cover/count/done/error).
COV_BASE = "https://covers.musichoarders.xyz"
# The site's API gate rejects non-browser User-Agents (401), so COV
# requests use a plain browser UA while MB/LRCLIB keep the app UA.
COV_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
# The API allows at most 9 active sources per search; prefer high-quality
# art sources first and fill up with whatever else is enabled.
COV_SOURCE_PRIORITY = [
    "qobuz", "applemusic", "tidal", "bandcamp", "deezer", "spotify",
    "itunes", "discogs", "musicbrainz",
]
COV_MAX_SOURCES = 9
COV_FALLBACK_SOURCES = [
    "qobuz", "applemusic", "tidal", "bandcamp", "deezer", "spotify",
    "itunes", "discogs", "musicbrainz",
]

_cov_info_cache = {"at": 0.0, "info": {}}
_cov_sources_cache = {"at": 0.0, "ids": []}
# COV's own default when a request omits `country`.
COV_DEFAULT_COUNTRY = "us"


def cov_catalog(timeout=15.0):
    """Everything the UI needs to configure a cover search: the selectable
    sources, the regions, and COV's own active-source cap.

    Cached for an hour — it is static metadata, and the UI asks for it every
    time the finder opens. A failure falls back to the built-in source list so
    the finder still works offline (with the region list reduced to the one
    default, since the real list comes from the server)."""
    import time as _time
    now = _time.time()
    if _cov_info_cache["info"] and now - _cov_info_cache["at"] < 3600:
        return _cov_info_cache["info"]
    try:
        raw = httpx.get(f"{COV_BASE}/api/info",
                        headers={"User-Agent": COV_UA},
                        timeout=timeout).json()
    except Exception:
        raw = {}
    sources = []
    for s in raw.get("sources") or []:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        sources.append({
            "id": s["id"],
            "name": s.get("name") or s["id"],
            "enabled": bool(s.get("enabled", True)),
            "color": s.get("color"),
            "countries": [str(c).lower() for c in (s.get("countries") or [])],
        })
    if not sources:
        sources = [{"id": i, "name": i, "enabled": True, "color": None,
                    "countries": []} for i in COV_FALLBACK_SOURCES]
    countries = [str(c).lower() for c in (raw.get("countries") or [])]
    info = {
        "sources": sources,
        "countries": countries or [COV_DEFAULT_COUNTRY],
        "active_source_limit": int(raw.get("activeSourceLimit") or COV_MAX_SOURCES),
    }
    _cov_info_cache.update(at=now, info=info)
    return info


def _cov_enabled_ids(timeout=15.0):
    """Source ids COV reports as enabled, in priority order."""
    enabled = [s["id"] for s in cov_catalog(timeout)["sources"] if s.get("enabled", True)]
    ordered = [s for s in COV_SOURCE_PRIORITY if s in enabled]
    ordered += [s for s in enabled if s not in ordered]
    return ordered or list(COV_FALLBACK_SOURCES)


def _cov_sources(timeout=15.0):
    """Default source ids for a search, capped at COV's active limit."""
    ids = _cov_enabled_ids(timeout)[:cov_catalog(timeout)["active_source_limit"]]
    return ids or list(COV_FALLBACK_SOURCES)


def resolve_cov_search(sources=None, country=None, cfg=None):
    """(source_ids, country) for a search, honouring the caller, then the
    saved defaults, then COV's own defaults.

    `sources`/`country` come from the finder UI (a per-search override);
    `cfg` holds the SAVED defaults (`cover_sources`, `cover_country`) used
    when the caller passes nothing. Every id is validated against the catalog
    so a stale saved setting cannot silently search nothing, and the list is
    trimmed to COV's cap — it rejects a longer one outright."""
    cat = cov_catalog()
    known = {s["id"] for s in cat["sources"]}
    limit = cat["active_source_limit"]

    chosen = [str(s).strip() for s in (sources or []) if str(s).strip()]
    if not chosen:
        try:
            from mlo.config import load_config
            chosen = [str(s).strip() for s in
                      ((cfg or load_config()).get("cover_sources") or [])
                      if str(s).strip()]
        except Exception:
            chosen = []
    chosen = [s for s in chosen if s in known]
    if not chosen:
        chosen = _cov_enabled_ids()

    c = str(country or "").strip().lower()
    if not c:
        try:
            from mlo.config import load_config
            c = str((cfg or load_config()).get("cover_country") or "").strip().lower()
        except Exception:
            c = ""
    if c not in cat["countries"]:
        c = COV_DEFAULT_COUNTRY
    return chosen[:limit], c


def _cover_row(source, small, big, title=None, artist=None, tracks=None,
               url=None, width=None, height=None):
    """One search result. Every provider (COV and each fallback) answers this
    exact shape, so the finder never has to know which one answered: the keys
    it already reads (`source`, `small`, `big`, `title`, `artist`, `tracks`,
    `url`) plus the REAL pixel size — `None` means unknown, never a guess."""
    return {"source": source, "small": small or None, "big": big or None,
            "title": title or None, "artist": artist or None, "tracks": tracks,
            "url": url or None, "width": width, "height": height}


def _cov_headers():
    """The exact header set COV's own frontend sends.

    The API gate 401s without it, and it must be these headers ONLY: adding
    `Accept: application/json` makes the endpoint answer 200 with an EMPTY
    stream (verified live), which reads as "no covers exist" — the silent
    nothing this module exists to avoid.
    """
    return {"User-Agent": COV_UA, "Referer": f"{COV_BASE}/", "Origin": COV_BASE,
            "X-Session": uuid.uuid4().hex}


@contextlib.contextmanager
def _cov_stream(body, headers, timeout=60.0):
    """POST a search and yield COV's streamed JSON lines (the HTTP seam)."""
    with httpx.Client(timeout=httpx.Timeout(timeout, read=timeout)) as client:
        with client.stream("POST", f"{COV_BASE}/api/search", json=body,
                           headers=headers) as r:
            r.raise_for_status()
            yield r.iter_lines()


def _cov_results(artist, album, limit, timeout, src_ids, ctry):
    """COV's own covers for one query, in the site's relevance order."""
    body = {"country": ctry, "sources": src_ids}
    if artist:
        body["artist"] = artist
    if album:
        body["album"] = album
    rows = []
    with _cov_stream(body, _cov_headers(), timeout) as lines:
        for line in lines:
            line = (line or "").strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") != "cover":
                continue
            rel = ev.get("releaseInfo") or {}
            rows.append(_cover_row(
                ev.get("source"), ev.get("smallCoverUrl"), ev.get("bigCoverUrl"),
                rel.get("title"), rel.get("artist"), rel.get("tracks"),
                rel.get("url"), ev.get("width"), ev.get("height")))
            if len(rows) >= limit:
                break
    return rows


# --------------------------------------------------------------------------- #
# Real image dimensions
# --------------------------------------------------------------------------- #
# COV's streamed lines carry no dimensions at all, and a CDN URL's own
# "500x0w"/"-250" is a REQUEST hint, not the size the URL answers with — so the
# size is read from the image's own header bytes. A ranged GET of the first
# 64 KB is enough for every JPEG SOF marker (they sit near the front), a PNG
# IHDR and all three WebP headers; anything larger is a download, and a search
# must not become one.
COVER_PROBE_BYTES = 65536
# Only the first results are probed — 40 covers must not become 40 image
# fetches. The rest keep `width`/`height` None, which the finder reads as
# "unknown" and shows as such.
COVER_PROBE_LIMIT = 24
COVER_PROBE_WORKERS = 8


def _image_size(data):
    """(width, height) read from an image's own header bytes, or None.

    JPEG (SOI + SOFn), PNG (IHDR) and WebP (VP8X / VP8 / VP8L) only — those
    are what every cover provider here serves; anything else reads unknown
    rather than being guessed at.
    """
    if len(data) < 26:
        return None
    if data[:2] == b"\xff\xd8":                              # JPEG
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if marker == 0xDA:                               # scan: no SOF after
                break
            seg = int.from_bytes(data[i + 2:i + 4], "big")
            if seg < 2:
                break
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return (int.from_bytes(data[i + 7:i + 9], "big"),
                        int.from_bytes(data[i + 5:i + 7], "big"))
            i += 2 + seg
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return (int.from_bytes(data[16:20], "big"),
                int.from_bytes(data[20:24], "big"))
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = data[12:16]
        if kind == b"VP8X":                                  # extended
            return (int.from_bytes(data[24:27], "little") + 1,
                    int.from_bytes(data[27:30], "little") + 1)
        if kind == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":   # lossy
            return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                    int.from_bytes(data[28:30], "little") & 0x3FFF)
        if kind == b"VP8L" and data[20] == 0x2F:             # lossless
            bits = int.from_bytes(data[21:25], "little")
            return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    return None


def _probe_get(url, nbytes=COVER_PROBE_BYTES, timeout=10.0):
    """The first *nbytes* of an image URL (a Range request), or b"".

    Range is a request, not a promise: a server may answer 200 with the whole
    file, so the body is read in chunks and dropped once *nbytes* are in hand.
    Never raises — a fetch that cannot answer is "unknown", not a failed
    search.
    """
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, read=timeout),
                          follow_redirects=True) as client:
            with client.stream("GET", url,
                               headers={"Range": f"bytes=0-{nbytes - 1}",
                                        "User-Agent": COV_UA}) as r:
                r.raise_for_status()
                out = bytearray()
                for chunk in r.iter_bytes(nbytes):
                    out += chunk
                    if len(out) >= nbytes:
                        break
                return bytes(out[:nbytes])
    except Exception:
        return b""


def _probe_dimensions(url, timeout=10.0):
    """One probe of one URL: header bytes only, public hosts only, no raise.

    Nothing here may raise — `_genre_cached` requires its producer to be total,
    and an unknown size is a valid answer while a dead search is not.
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if (parsed.scheme not in ("http", "https")
                or not _public_host(parsed.hostname)):
            return None                 # the same trust boundary as the download
        size = _image_size(_probe_get(url, timeout=timeout))
        return {"width": size[0], "height": size[1]} if size else None
    except Exception:
        return None


def image_dimensions(url, timeout=10.0):
    """`{"width", "height"}` of the image at *url*, or None when unknown.

    Memoized 30 days (memory + <music>/.mlo/data, the app's shared value
    cache) — a probe that found nothing is cached too, so a rerun never re-asks
    a host that already said no.
    """
    url = str(url or "").strip()
    if not url:
        return None
    got = _genre_cached("cover_dim", url,
                        lambda: _probe_dimensions(url, timeout))
    return got if isinstance(got, dict) else None


def _attach_dimensions(rows, limit=COVER_PROBE_LIMIT, timeout=10.0):
    """Fill `width`/`height` on the first *limit* rows from their own image.

    Probed in a small pool: these are 20+ different CDNs, and a search that
    waits on them one at a time is a search that hangs on one slow host.
    """
    todo = [r for r in rows[:limit] if r.get("width") is None]
    if not todo:
        return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=COVER_PROBE_WORKERS) as pool:
        sizes = list(pool.map(
            lambda r: image_dimensions(r.get("big") or r.get("small"), timeout),
            todo))
    for row, size in zip(todo, sizes):
        if size:
            row["width"], row["height"] = size["width"], size["height"]


# --------------------------------------------------------------------------- #
# Fallbacks — asked ONLY when the meta-search returns nothing
# --------------------------------------------------------------------------- #
# Each is a single album lookup (not a meta-search) and each answers the same
# row shape, with its own id as `source` and the dimensions probed the same
# way. Order: the caller's release-group MBID first (an identity — no name
# guessing at all), then the two keyless catalogues the app already talks to.
CAA_BASE = "https://coverartarchive.org"
DEEZER_API = "https://api.deezer.com"
COVER_FALLBACKS = ("coverartarchive", "deezer", "itunes")


def _artwork_big(url):
    """`…/100x100bb.jpg` → `…/3000x3000bb.jpg` (Apple's largest artwork).

    `artworkUrl100` is the only artwork URL Apple's search returns, and the
    same asset is served at any size in that segment.
    """
    return re.sub(r"/\d+x\d+bb\.", "/3000x3000bb.", str(url or "")) or None


def _name_rank(name, want):
    """0 when *name* is exactly *want* (folded), else 1 — sorting only."""
    return 0 if (want and _norm_compare(name) == want) else 1


def _caa_covers(rg_mbid, limit, artist="", album="", timeout=30.0):
    """Cover Art Archive images for a RELEASE GROUP, front cover first.

    Asked about the release-group MBID the caller already has (the import
    wizard holds one): CAA is browsed BY IDENTITY — its search is per release,
    and matching it by name is the name-guessing this app does not do. The big
    URL is the original upload; CAA publishes no dimensions, which is exactly
    why the probe exists.
    """
    rg = str(rg_mbid or "").strip()
    if not rg:
        return []
    data = _advisory_json(f"{CAA_BASE}/release-group/{rg}",
                          headers={"User-Agent": USER_AGENT},
                          timeout=timeout, host="coverartarchive.org")
    images = [i for i in ((data or {}).get("images") or []) if i.get("image")]
    images.sort(key=lambda i: not i.get("front"))
    page = f"{CAA_BASE}/release-group/{rg}"
    rows = []
    for img in images[:limit]:
        th = img.get("thumbnails") or {}
        rows.append(_cover_row("coverartarchive",
                               th.get("large") or th.get("small") or img["image"],
                               img["image"], album, artist, None, page))
    return rows


def _deezer_covers(artist, album, limit, timeout=30.0):
    """Deezer's album cover — `cover_xl` is the biggest Deezer serves
    (1000×1000), so nothing here is expected to be larger than that.

    Rows are ORDERED, never dropped: a tribute/karaoke album that carries the
    same title is a bad first hit, not a reason to hide the real covers.
    """
    term = " ".join(x for x in (f'artist:"{artist}"' if artist else "",
                                f'album:"{album}"' if album else "") if x)
    if not term:
        return []
    data = _advisory_json(f"{DEEZER_API}/search/album",
                          {"q": term, "limit": max(1, min(limit, 25))},
                          timeout=timeout, host="api.deezer.com")
    want_artist, want_album = _norm_compare(artist), _norm_compare(album)
    rows = [a for a in ((data or {}).get("data") or [])
            if a.get("cover_xl") or a.get("cover_big")]
    rows.sort(key=lambda a: (_name_rank((a.get("artist") or {}).get("name"),
                                        want_artist),
                             _name_rank(a.get("title"), want_album)))
    return [_cover_row("deezer", a.get("cover_big") or a.get("cover_medium"),
                       a.get("cover_xl") or a.get("cover_big"),
                       a.get("title"), (a.get("artist") or {}).get("name"),
                       a.get("nb_tracks"), a.get("link"))
            for a in rows[:limit]]


def _itunes_covers(artist, album, limit, cfg=None, timeout=30.0):
    """Apple's album artwork at its 3000×3000 size (see `_artwork_big`).

    Reached through the app's cached, throttled iTunes client — this is the
    same endpoint the advisory routes already use, and Apple rate-limits.
    """
    term = " ".join(x for x in (artist, album) if x)
    if not term:
        return []
    data = _apple_json("/search", {"term": term, "entity": "album",
                                   "limit": max(1, min(limit, 25)),
                                   "country": _apple_country(cfg)},
                       timeout=timeout)
    want_artist, want_album = _norm_compare(artist), _norm_compare(album)
    rows = [a for a in ((data or {}).get("results") or [])
            if a.get("artworkUrl100")]
    rows.sort(key=lambda a: (_name_rank(a.get("artistName"), want_artist),
                             _name_rank(a.get("collectionName"), want_album)))
    return [_cover_row("itunes", a["artworkUrl100"],
                       _artwork_big(a["artworkUrl100"]),
                       a.get("collectionName"), a.get("artistName"),
                       a.get("trackCount"), a.get("collectionViewUrl"))
            for a in rows[:limit]]


def _cover_fallback(artist, album, limit, cfg, rg_mbid, timeout):
    """(results, provider) from the FIRST fallback that answers, else ([], None).

    Only ever reached when the meta-search came back empty or failed; one
    lookup per provider in COVER_FALLBACKS order, stopping at the first
    non-empty list. A provider that errors is skipped, not fatal.
    """
    for pid in COVER_FALLBACKS:
        try:
            if pid == "coverartarchive":
                rows = _caa_covers(rg_mbid, limit, artist, album, timeout)
            elif pid == "deezer":
                rows = _deezer_covers(artist, album, limit, timeout)
            else:
                rows = _itunes_covers(artist, album, limit, cfg, timeout)
        except Exception:
            rows = []
        if rows:
            return rows, pid
    return [], None


def cover_search(artist, album, limit=40, timeout=60.0, sources=None,
                 country=None, cfg=None, release_group_mbid=""):
    """Album covers for artist/album → ``{"results": [...], "provider": id}``.

    Every row is ``{source, small, big, title, artist, tracks, url, width,
    height}``: the keys the finder already reads, plus the image's REAL pixel
    size, read from the file itself for the first ``COVER_PROBE_LIMIT`` rows
    (``None`` means unknown — a dimension probe can never fail the search).

    ``provider`` names who actually answered: ``"cov"`` for the meta-search, a
    fallback id otherwise, and ``None`` when nobody had anything at all — an
    empty answer is STATED, never left as a silent zero-result. The fallbacks
    run only when COV returns nothing (or refuses/errors: a dead meta-search
    is a fallback case, not an error the user has to understand).

    ``sources``/``country`` override the SAVED defaults (`cover_sources`,
    `cover_country`) for this one search only — nothing here writes config.
    ``release_group_mbid``, when the caller has one, is the identity the Cover
    Art Archive fallback is asked about.
    """
    if not artist and not album:
        raise ValueError("artist or album is required")
    src_ids, ctry = resolve_cov_search(sources, country, cfg)
    try:
        results = _cov_results(artist, album, limit, timeout, src_ids, ctry)
    except Exception:
        results = []
    provider = "cov"
    if not results:
        results, provider = _cover_fallback(artist, album, limit, cfg,
                                            release_group_mbid, timeout)
    _attach_dimensions(results)
    return {"results": results, "provider": provider}


# Image downloads are the one place a caller supplies a URL the server then
# fetches. Provider CDNs do redirect (Cover Art Archive → archive.org), so
# redirects are followed — but each hop is re-validated, the destination must
# be a public host, and the body is capped: artwork that is not a few
# megabytes is a mistake or an attack, not a cover.
IMAGE_MAX_BYTES = 20 * 1024 * 1024
IMAGE_MAX_REDIRECTS = 5


def _public_host(host):
    """False for localhost, loopback, private, link-local, reserved, multicast
    and unspecified addresses — the SSRF guard for caller-supplied URLs."""
    import ipaddress
    import socket
    name = str(host or "").strip().strip("[]")
    if not name or name.lower().endswith(".local"):
        return False
    if name.lower() in ("localhost", "localhost.localdomain", "ip6-localhost"):
        return False
    try:
        infos = socket.getaddrinfo(name, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def fetch_image_bytes(url, timeout=60.0):
    """Download an image URL and return (data, content_type).

    Only http(s) to a *public* host is allowed, every redirect hop is
    re-checked (a redirect chain must not reach an internal address after the
    first check), and the body is capped at IMAGE_MAX_BYTES so a huge or slow
    endpoint cannot be used as a memory/time hold.
    """
    from urllib.parse import urljoin, urlparse
    headers = {"User-Agent": COV_UA, "Referer": f"{COV_BASE}/"}
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("invalid image url")
    if not _public_host(parsed.hostname):
        raise ValueError("image url is not a public host")
    with httpx.Client(timeout=httpx.Timeout(timeout, read=timeout),
                      follow_redirects=False) as client:
        for _hop in range(IMAGE_MAX_REDIRECTS + 1):
            with client.stream("GET", url, headers=headers) as r:
                if r.is_redirect:
                    target = urljoin(url, str(r.headers.get("location") or ""))
                    hop = urlparse(target)
                    if hop.scheme not in ("http", "https") or not hop.netloc:
                        raise ValueError("invalid image redirect")
                    if not _public_host(hop.hostname):
                        raise ValueError("image redirect leaves the public internet")
                    url = target
                    continue
                r.raise_for_status()
                ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                chunks, size = [], 0
                for chunk in r.iter_bytes(65536):
                    size += len(chunk)
                    if size > IMAGE_MAX_BYTES:
                        raise ValueError("image is too large")
                    chunks.append(chunk)
                return b"".join(chunks), ctype
    raise ValueError("too many image redirects")


def lyrics_chain(cfg, artist, track, album=None, duration=None):
    """First provider hit for a track across the lyrics chain (the order lives
    in ``mlo.lyrics_providers.SOURCES``: LRCLIB, NetEase, QQ Music, Kuwo,
    Kugou, YouTube captions) — same dict as
    ``mlo.lyrics_providers.fetch_lyrics``, or None. Imported lazily so the
    engine module stays out of this header."""
    from mlo.lyrics_providers import fetch_lyrics
    return fetch_lyrics(cfg, artist, track, album, duration)
