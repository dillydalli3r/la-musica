"""MusicBrainz / LRCLIB / RateYourMusic integrations for the import wizard.

MusicBrainz is queried with proper rate limiting (1 req/s) and a UA string
per their API etiquette. RYM has no public API — links are user-supplied
URLs stored as tags, but we validate/parse them here.
"""
import asyncio
import html as _html
import json
import os
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


def mb_get(endpoint, params=None, timeout=30.0, retries=3):
    """Rate-limited MusicBrainz WS/2 GET returning parsed JSON.

    Retries 429/5xx (MusicBrainz rate-limits and has occasional 503s) with
    Retry-After-aware backoff, while keeping the 1 request/second etiquette.
    """
    global _last_request
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    for attempt in range(retries):
        with _mb_lock:
            elapsed = time.time() - _last_request
            if elapsed < 1.0:
                time.sleep(1.0 - elapsed)
            try:
                r = httpx.get(
                    f"{MB_BASE}/{endpoint}",
                    params=params or {},
                    headers=headers,
                    timeout=timeout,
                )
            except httpx.HTTPError:
                _last_request = time.time()
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
            _last_request = time.time()
        if r.status_code in (429, 500, 502, 503, 504):
            wait = 1.5 * (attempt + 1)
            retry_after = r.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = max(wait, float(retry_after))
                except ValueError:
                    pass
            if attempt < retries - 1:
                time.sleep(wait)
                continue
        r.raise_for_status()
        return r.json()


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

    # labels -> label name + first catalog number
    catalog_number = ""
    label_name = ""
    for lab in data.get("label-info", []) or []:
        if not label_name:
            label_name = str(((lab.get("label") or {}).get("name")) or "").strip()
        cn = (lab.get("catalog-number") or "").strip()
        if cn and not catalog_number:
            catalog_number = cn
        if label_name and catalog_number:
            break
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
# Sources, asked in this order — the first one that STATES a value wins, and
# a route that cannot state one contributes nothing:
#
#   deezer-isrc    api.deezer.com/track/isrc:<ISRC>      (verified working)
#   spotify-isrc   Spotify search by ISRC                (needs client id+secret)
#   apple-album    iTunes artist → its albums → THE EXPLICIT EDITION →
#                  lookup?entity=song                    (verified working)
#   itunes-song    iTunes song search, exact title only  (last resort)
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
# Deezer's ISRC endpoint DOES work and is per-track, so it is asked first: it
# needs no title guessing at all. Spotify is optional and never load-bearing;
# it is skipped entirely until `spotify_client_id`/`spotify_client_secret` are
# set in Settings.
#
# Honesty rules, which every route below obeys:
#   * only 0/1/2 is ever returned — never a default, never a guess;
#   * a `cleaned` Apple entry (or a `Clean` contentAdvisoryRating) is a CLEANED
#     EDITION, states nothing about the original master and is NO ANSWER — it
#     must never become 0 or 2;
#   * None means "unrated": the caller leaves the tag absent.
# Every value a route may report as `source`. A caller may only write a value
# it can attribute to one of these.
ADVISORY_SOURCES = frozenset({"deezer-isrc", "spotify-isrc", "apple-album",
                              "itunes-song"})

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
    decides — another track's rating is not this file's), falling back to an
    exact normalized title match. `positions_ok=False` disables the position
    mapping entirely: that collection does not hold the album's track count,
    so the same disc/track number may be a different song and only a title
    match may be trusted.

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
    want_title = _norm_compare(title)
    if want_title:
        for item in tracks:
            if _norm_compare(item.get("trackName")) != want_title:
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


def _itunes_song_advisory(title, artist="", timeout=None):
    """(value, source) from Apple's song search — the LAST resort.

    A search hit is a candidate, not an identity, so a hit is only accepted
    when its normalized track title equals the file's exactly AND its own
    artist credit contains the file's artist. A `cleaned` hit is never
    accepted (see `_apple_value`).
    """
    title = str(title or "").strip()
    if not title:
        return None
    data = _advisory_json(f"{_ITUNES_LOOKUP}/search",
                          {"term": " ".join(t for t in (artist, title) if t),
                           "entity": "song", "limit": 10}, timeout=timeout)
    want_title = _norm_compare(title)
    want_artist = _norm_compare(artist)
    for item in (data or {}).get("results") or []:
        if _norm_compare(item.get("trackName")) != want_title:
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


def resolve_advisory_route(isrc="", recording_mbid="", title="", artist="",
                           album="", disc=None, track=None, track_count=None,
                           cfg=None):
    """{"value": 0|1|2|None, "source": key|None, "checked": [key, ...]}.

    The whole point of the three-part answer: a caller may only write a value
    it can attribute, so `value` is never set without a `source` naming the
    provider that stated it. `checked` lists the routes that were actually
    asked (in order), which is what the UI shows when nothing answered.
    """
    if cfg is None:
        try:
            from mlo.config import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
    codes = [str(isrc).strip()] if str(isrc or "").strip() else []
    if not codes and recording_mbid:
        codes = recording_isrcs(recording_mbid)
    spotify_on = _spotify_configured(cfg)
    checked = []
    for code in codes:
        checked.append("deezer-isrc")
        answer = _advisory_cached(("deezer-isrc", code.upper()),
                                  lambda c=code: _deezer_advisory(c))
        if answer is not None:
            return {"value": answer[0], "source": answer[1], "checked": checked}
        if spotify_on:
            checked.append("spotify-isrc")
            answer = _advisory_cached(("spotify-isrc", code.upper()),
                                      lambda c=code: _spotify_advisory(c, cfg))
            if answer is not None:
                return {"value": answer[0], "source": answer[1], "checked": checked}
    if artist and album:
        checked.append("apple-album")
        key = ("apple-album", _norm_compare(artist), _norm_compare(album),
               _advisory_int(disc), _advisory_int(track), _norm_compare(title))
        answer = _advisory_cached(key, lambda: _apple_album_advisory(
            artist, album, title, disc, track, track_count, cfg=cfg))
        if answer is not None:
            return {"value": answer[0], "source": answer[1], "checked": checked}
    if title:
        checked.append("itunes-song")
        key = ("itunes-song", _norm_compare(artist), _norm_compare(title))
        answer = _advisory_cached(key,
                                  lambda: _itunes_song_advisory(title, artist))
        if answer is not None:
            return {"value": answer[0], "source": answer[1], "checked": checked}
    return {"value": None, "source": None, "checked": checked}


def resolve_advisory(isrc="", recording_mbid="", **context):
    """ITUNESADVISORY (0/1/2) for one track, or None when nobody states one.

    Asked in order, first stated answer wins (`resolve_advisory_route`):

      1. Deezer by ISRC — per-track identity, no title guessing (primary);
      2. Spotify by ISRC — only when client id + secret are configured;
      3. Apple's artist route — artist → its album list → the explicit
         edition → track (disc/track, then exact title);
      4. Apple's song search — exact normalized title only.

    `context` may carry `title`, `artist`, `album`, `disc`, `track`,
    `track_count` and `cfg` (routes 3 and 4 need them; routes 1 and 2 do not).
    MusicBrainz supplies the ISRC when the caller has none.

    None means "unrated" and must never be written as 0 — a clean rating is a
    claim about the audio, not a default — and no route ever answers from a
    `cleaned` edition.
    """
    return resolve_advisory_route(isrc=isrc, recording_mbid=recording_mbid,
                                  **context)["value"]


def release_advisories(mbid, sources=None):
    """{f'{disc}:{position}': 0|1|2} for every track of a release a provider
    rates. Empty when nothing is known — callers must not fill the gaps.

    `sources` (optional) is filled with the answering source key per entry, so
    the caller can report provenance alongside the value.
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
# Scraping is polite and cheap: one request per second, and a 30-day disk
# cache under <music>/.mlo/data/rym_cache so repeat imports never re-fetch.
# (Deezer and Apple, by contrast, are keyless public APIs and their advisory
# routes are verified working — see ADVISORY_SOURCES.)
RYM_BASE = "https://rateyourmusic.com"
# What a normal Chrome window sends. A bare library UA gets a challenge, so
# these are the cheapest thing that can make an allowed request succeed.
RYM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Referer": RYM_BASE + "/",
    "Upgrade-Insecure-Requests": "1",
}
RYM_MIN_INTERVAL = 1.0        # seconds between requests, per their etiquette
RYM_CACHE_TTL = 30 * 86400.0  # genre data moves slowly
_rym_lock = threading.Lock()
_rym_last = 0.0
_rym_warned = False           # one concise line per process, not per album
_rym_failures = 0             # how often RYM failed to answer at all
# Cloudflare's interstitial instead of a release page. Cached or parsed it
# would be an empty page at best, so it counts as unreachable.
_RYM_CHALLENGE_RE = re.compile(
    r"just a moment|cf-?challenge|_cf_chl|checking your browser|"
    r"enable javascript and cookies", re.I)
# RYM renders genres, styles and descriptors as /genre/<slug> anchors, in that
# order on a release page (primary genres first) — the anchors are the whole
# scrape, so a markup change degrades to "no genres", never to wrong ones.
_RYM_GENRE_RE = re.compile(r'href="/genre/([a-z0-9%\-]+)"[^>]*>([^<]{1,60})</a>', re.I)
_RYM_ARTIST_LINK_RE = re.compile(r'href="(/artist/[^"]+)"', re.I)


def _rym_cookie(cfg=None):
    """The user's `rym_cookie` — a browser session cookie, or "".

    Read from the live config on every request so pasting one into Settings
    takes effect without a restart. An empty cookie is not an error: it is
    the documented "RYM is skipped" state."""
    try:
        if cfg is None:
            from mlo.config import load_config
            cfg = load_config()
        return str((cfg or {}).get("rym_cookie") or "").strip()
    except Exception:
        return ""


def _rym_headers(cfg=None):
    """Browser-like headers, plus the user's cookie session when configured."""
    headers = dict(RYM_HEADERS)
    cookie = _rym_cookie(cfg)
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _rym_unreachable(reason):
    """One concise line per process when RYM cannot answer.

    A per-album traceback would bury the import log for a source that is
    simply unavailable, so this is logged once and the chain moves on. The
    counter is what lets a caller tell "RYM is not answering" (stop asking —
    the next candidate cannot do better) from "that slug was wrong" (try the
    next one)."""
    global _rym_warned, _rym_failures
    _rym_failures += 1
    if _rym_warned:
        return
    _rym_warned = True
    print(f"[mlo] rateyourmusic: {reason} — skipping RYM (set rym_cookie in "
          "Settings with a logged-in browser session to enable it)")


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


def _rym_get(path, params=None, cfg=None, expect=None):
    """Polite GET: 1 req/s, disk-cached, browser-like headers, None on any
    failure (a blocked RYM is logged once — see `_rym_unreachable`).

    *expect* is the path the caller asked for: a 404 or a redirect to
    somewhere else (RYM sends an unknown slug to search/home) then counts as
    "no such page" — a miss the caller can move on from, not a sign that RYM
    is unreachable, so neither is logged as one."""
    global _rym_last
    import hashlib
    from urllib.parse import urlencode, urlsplit
    key = hashlib.sha1(
        (path + "?" + urlencode(sorted((params or {}).items()))).encode("utf-8")
    ).hexdigest()
    hit = _rym_cache_read(key, RYM_CACHE_TTL)
    if hit is not None:
        return hit
    with _rym_lock:
        wait = RYM_MIN_INTERVAL - (time.time() - _rym_last)
        if wait > 0:
            time.sleep(wait)
        _rym_last = time.time()
        try:
            r = httpx.get(
                f"{RYM_BASE}{path}", params=params or {},
                headers=_rym_headers(cfg),
                timeout=20.0, follow_redirects=True,
            )
        except httpx.HTTPError as e:
            _rym_unreachable(f"no connection ({type(e).__name__})")
            return None
    if r.status_code != 200:
        # 404 is a slug that does not exist, not a blocked source: the caller
        # tries its next candidate instead of declaring RYM unreachable.
        if r.status_code != 404:
            _rym_unreachable(f"HTTP {r.status_code}")
        return None
    if not r.text:
        _rym_unreachable("empty response")
        return None
    if _RYM_CHALLENGE_RE.search(r.text[:4000]):
        _rym_unreachable("Cloudflare challenge instead of a page")
        return None
    if expect is not None:
        final = urlsplit(str(getattr(r, "url", "") or "")).path
        if not final.startswith(expect):
            return None
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
    out = []
    for _slug, label in _RYM_GENRE_RE.findall(html or ""):
        name = re.sub(r"\s+", " ", label).strip()
        if name and name.lower() not in {g.lower() for g in out}:
            out.append(name)
    return out


def rym_genres(artist, album):
    """RateYourMusic genres for an album, or None when RYM cannot answer.

    Tries the release URL RYM derives from the artist+album slugs (its
    canonical `/release/album/<artist>/<album>/` shape) first and its search
    page second, so a punctuation-heavy title still resolves. Returns
    {"genres": [...], "source_url": ...} or None — see RYM_BASE's note on the
    blocked-by-RYM failure mode. Chart data is NOT scraped: nothing in the app
    consumes a RYM chart, so only the genre path is implemented.
    """
    artist = str(artist or "").strip()
    album = str(album or "").strip()
    if not artist or not album:
        return None
    url = f"/release/album/{_rym_slug(artist)}/{_rym_slug(album)}/"
    html = _rym_get(url)
    if html:
        genres = _rym_genres_from(html)
        if genres:
            return {"genres": genres, "source_url": f"{RYM_BASE}{url}"}
    html = _rym_get("/search", {"searchterm": f"{artist} {album}", "type": "a"})
    if not html:
        return None
    m = _RYM_ARTIST_LINK_RE.search(html)
    m2 = re.search(r'href="(/release/album/[^"]+)"', html, re.I)
    if not m2:
        return None
    rel_url = m2.group(1)
    page = _rym_get(rel_url)
    genres = _rym_genres_from(page or "")
    if not genres:
        return None
    return {"genres": genres, "source_url": f"{RYM_BASE}{rel_url}",
            "artist_page": f"{RYM_BASE}{m.group(1)}" if m else ""}


def rym_artist_genres(artist):
    """RateYourMusic genres for an artist (its /artist/ page), or None."""
    artist = str(artist or "").strip()
    if not artist:
        return None
    url = f"/artist/{_rym_slug(artist)}"
    html = _rym_get(url)
    genres = _rym_genres_from(html or "")
    if not genres:
        return None
    return {"genres": genres, "source_url": f"{RYM_BASE}{url}"}


# --------------------------------------------------------------------------- #
# RYM link resolution (album + artist)
# --------------------------------------------------------------------------- #
# RYM's URLs are derived from the names, so a link can usually be resolved
# without scraping anything but the page that proves it exists:
#
#   /release/album/<artist-slug>/<album-slug>/     /artist/<artist-slug>
#
# A candidate is fetched once (1 req/s, 30-day cache) and accepted only when
# the page that comes back IS that page: HTTP 200 (a 404 or a redirect to
# search/home means the slug is wrong), no Cloudflare interstitial, and the
# page states the artist — and for a release, the album too. Nothing is
# guessed from a partial page, so a candidate that cannot be confirmed yields
# NO link and the user pastes their own (the manual editor is unchanged).
_RYM_RELEASE_LINK_RE = re.compile(r'href="(/release/album/[^"]+)"', re.I)


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


def rym_links(artist="", album="", cfg=None):
    """Verified RateYourMusic links for an album:
    ``{"album", "artist", "note"}``.

    The album link is tried first (its page also names the artist's own URL),
    as `/release/album/<artist>/<album>/` with the exact slugs, then with the
    de-`the`-ed ones, then from RYM's own search page — each candidate
    confirmed before it is accepted. The artist link comes from the album
    page's own `/artist/` link when it is one of the artist's slugs, else from
    `/artist/<slug>` directly.

    Either link is None when it could not be confirmed, and `note` says so
    ("could not resolve …") — that is the user-pastes-the-URL state, never an
    error. Gated by `rym_links_auto` (mlo.config, default True): off means no
    request at all.
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

    artist_slugs = _rym_slug_candidates(artist)
    page = None
    # A failure that is not a miss (no connection, a challenge) is counted:
    # once RYM has refused to answer, the remaining candidates cannot do
    # better, so this album costs ONE request and the ladder stops.
    fails = _rym_failures
    if artist and album:
        for a in artist_slugs:
            for b in _rym_slug_candidates(album):
                path = f"/release/album/{a}/{b}/"
                page = _rym_verified(path, cfg, artist, album)
                if page:
                    out["album"] = f"{RYM_BASE}{path}"
                    break
            if out["album"] or _rym_failures != fails:
                break
        if not out["album"] and _rym_failures == fails:
            # RYM's own search: the first release hits for the query, each
            # confirmed the same way (so a cover version cannot slip through).
            index = _rym_get("/search", {"searchterm": f"{artist} {album}",
                                         "type": "a"}, cfg=cfg)
            for rel in _RYM_RELEASE_LINK_RE.findall(index or "")[:3]:
                page = _rym_verified(rel, cfg, artist, album)
                if page:
                    out["album"] = f"{RYM_BASE}{rel}"
                    break
                if _rym_failures != fails:
                    break

    if artist and _rym_failures == fails:
        # The album page links its own artist: try RYM's own answer first,
        # but only inside the slug set this name can legitimately produce.
        slugs = list(artist_slugs)
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
            if _rym_failures != fails:
                break

    if not out["album"] and not out["artist"]:
        out["note"] = ("no artist name to look up" if not artist
                       else "could not resolve on RateYourMusic")
    elif artist and album and not out["album"]:
        out["note"] = "could not resolve the album link on RateYourMusic"
    elif artist and not out["artist"]:
        out["note"] = "could not resolve the artist link on RateYourMusic"
    return out


# --------------------------------------------------------------------------- #
# Genre chain — one merge point for every genre source
# --------------------------------------------------------------------------- #
# Order used when mlo.config genre_sources is empty. Same list as the config
# default: scraped RYM, then peer/community sources, then streaming providers,
# with MusicBrainz (the app's identity anchor) last.
GENRE_SOURCES = ["rateyourmusic", "soulseek", "discogs", "lastfm",
                 "theaudiodb", "musicbrainz", "deezer", "itunes"]


def _genre_source_names(source, artist, album, release, cfg):
    """One source's raw genre names (never raises — wrapped by genre_chain)."""
    from server import discovery

    if source == "rateyourmusic":
        return list((rym_genres(artist, album) or {}).get("genres") or [])
    if source == "soulseek":
        # Peers advertise folders and file names, not genres. Nothing here can
        # state a genre, so this source is a no-op by design — it stays in the
        # order so a saved config listing it keeps working.
        return []
    if source == "discogs":
        return discovery.discogs_album_genres(artist, album, cfg)
    if source == "lastfm":
        return discovery.lastfm_album_genres(artist, album, cfg)
    if source == "theaudiodb":
        row = discovery.audiodb_album(artist, album) or {}
        return [g for g in (row.get("genre"), row.get("mood")) if g]
    if source == "musicbrainz":
        names = []
        if release:
            names += list(release.get("genres") or [])
            rg = release.get("release_group_id")
            if rg:
                names += release_group_genres(rg)
            for a in release.get("artists") or []:
                if a.get("mbid"):
                    names += artist_genres(a["mbid"])
                    break
        else:
            rg = None
            try:
                rg = discovery.resolve_release_group(artist, album, cfg)
            except Exception:
                rg = None
            if rg and rg.get("mbid"):
                names += release_group_genres(rg["mbid"])
            # the artist's own genres: what a genre cascade usually has to
            # fall back on when the release itself carries none
            ar = discovery.resolve_artist_mbid(artist) if artist else None
            if ar:
                names += artist_genres(ar)
        return names
    if source == "deezer":
        return discovery.album_genres(artist, album, cfg=cfg)
    if source == "itunes":
        return [r["genre"] for r in discovery.itunes_search_album(artist, album, limit=1)
                if r.get("genre")]
    return []


def genre_chain(artist="", album="", release=None, limit=None, sources=None,
                cfg=None):
    """Genres for an album, merged from the configured sources in order.

    RateYourMusic → those with only a no-op/needs-a-key → TheAudioDB →
    MusicBrainz → Deezer → iTunes (mlo.config `genre_sources`). Every source
    that answers contributes; the merged list is deduped case-insensitively,
    Title-Cased and capped at `limit` (default `mb_genre_count`, now 3).
    A source that cannot answer (RYM blocked, no Discogs/Last.fm token, no
    peer genre signal) contributes nothing and is reported in `notes` — it is
    never filled in from a guess.

    Returns {"genres": [...], "per_source": {source: [...]}, "notes": {source:
    reason}}.
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

    per_source, notes = {}, {}
    for source in order:
        try:
            names = _genre_source_names(source, artist, album, release, cfg) or []
        except Exception as e:
            notes[source] = f"failed: {e}"
            continue
        clean = []
        for name in names:
            text = str(name).strip()
            if text and text.lower() not in {g.lower() for g in clean}:
                clean.append(text)
        if clean:
            per_source[source] = clean
        else:
            notes[source] = "no data"

    merged = []
    for source in order:
        for name in per_source.get(source, []):
            text = str(name).strip().title()
            if text and text.lower() not in {g.lower() for g in merged}:
                merged.append(text)
    return {"genres": merged[:limit], "per_source": per_source, "notes": notes}


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


def artist_browse(mbid, limit=300, offset=0):
    """Artist page: identity + genres + full discography (release groups).

    Discography comes from the *browse* endpoint (release-group?artist=…)
    rather than a lookup's inc= subquery — lookups silently cap the related
    list. Pages are collected (up to `limit`) so type filters and the
    chronological order are honest across MusicBrainz's unsorted pages."""
    data = mb_get_cached(f"artist/{mbid}", {"inc": "genres", "fmt": "json"})
    rgs, total = _browse_collect(
        "release-group", {"artist": mbid}, "release-groups", "release-group-count",
        limit=limit, offset=offset,
    )
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


def _release_policy():
    """(avoid_promo, medium_order) from config, with safe defaults."""
    from mlo.config import DEFAULT_CONFIG, load_config
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    avoid = bool(cfg.get("auto_import_avoid_promo", True))
    order = cfg.get("auto_import_medium_order") or DEFAULT_CONFIG["auto_import_medium_order"]
    return avoid, [str(x).strip().lower() for x in order if str(x).strip()]


# MusicBrainz release statuses that must never be auto-picked while
# avoid-promo is on: a promo/bootleg/pseudo edition is not the album.
_PROMO_STATUSES = {"promotion", "bootleg", "pseudo-release", "pseudo release"}


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


def release_choice_key(rel, avoid_promo=True, medium_order=None):
    """Sort key: Official first, then medium preference, then earliest date.

    Promotional editions sort last (and `pick_releases` drops them entirely)
    when avoid-promo is on."""
    status = str(rel.get("status") or "").strip().lower()
    if status == "official":
        rank = 0
    elif status in _PROMO_STATUSES and avoid_promo:
        rank = 2
    else:
        rank = 1
    return (rank, release_medium_rank(rel, medium_order or []),
            rel.get("date") or "9999")


def pick_releases(releases, cfg=None):
    """Releases ordered by the auto-import release policy (best first).

    Promotional / bootleg / pseudo editions are dropped entirely while
    mlo.config `auto_import_avoid_promo` is on, so no auto-import path can
    queue one."""
    if cfg is None:
        avoid, order = _release_policy()
    else:
        avoid = bool(cfg.get("auto_import_avoid_promo", True))
        order = [str(x).strip().lower()
                 for x in (cfg.get("auto_import_medium_order") or []) if str(x).strip()]
    kept = [r for r in (releases or [])
            if not (avoid and str(r.get("status") or "").strip().lower() in _PROMO_STATUSES)]
    kept.sort(key=lambda r: release_choice_key(r, avoid, order))
    return kept


def pick_release(releases, cfg=None):
    """The single best release of a group per `pick_releases`, or None."""
    kept = pick_releases(releases, cfg)
    return kept[0] if kept else None


def resolve_release(mbid):
    """(release, release_mbid) for a release id, a release-group id or a URL.

    Auto-import works on a *release* — a concrete pressing with a track list
    — but the links discovery, MoreLikeThis and Home hand out are release
    GROUPS, and a group id passed to the release endpoint 404s. Group ids
    are resolved to their best edition via the release-choice policy so no
    caller (HTTP route, wishes worker, bulk import) can queue a group job.
    Returns (None, mbid) when the id is a group with no usable edition, and
    (None, None) when nothing matches at all."""
    rid = _mbid(mbid)
    if not rid:
        return None, None
    try:
        return release_lookup(rid), rid
    except Exception:
        pass
    try:
        rg = release_group_browse(rid, limit=100, offset=0)
    except Exception:
        return None, None
    best = pick_release(rg.get("releases") or []) if rg.get("id") else None
    if not best or not best.get("id"):
        return None, rid
    try:
        return release_lookup(best["id"]), best["id"]
    except Exception:
        return None, rid


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
    avoid, medium_order = _release_policy()
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


def recording_browse(mbid, limit=300, offset=0):
    """Recording ('track') page: identity + releases carrying it (browsed,
    with media, for the same reasons as the release-group page)."""
    data = mb_get_cached(
        f"recording/{mbid}",
        {"inc": "artist-credits+isrcs+genres", "fmt": "json"},
    )
    rel_rows, total = _browse_collect(
        "release",
        {"recording": mbid, "inc": "media+artist-credits+release-groups"},
        "releases", "release-count",
        limit=limit, offset=offset,
    )
    releases = []
    for r in sorted(
        rel_rows,
        key=lambda r: r.get("date") or "9999",
    ):
        track_count, track_breakdown = _release_counts(r)
        rg_primary, rg_secondary = _rg_types(r)
        releases.append({
            "id": r.get("id"),
            "title": r.get("title"),
            "date": r.get("date") or "",
            "country": r.get("country") or "",
            "status": r.get("status") or "",
            "formats": _media_summary(r),
            "disc_count": len(r.get("media") or []),
            "track_count": track_count,
            "track_breakdown": track_breakdown,
            # the release group's full type: primary (Album/EP/Single/...) plus
            # secondary (Soundtrack/Live/Compilation/...), so a score album
            # reads "Album + Soundtrack" instead of a bare "Album".
            "primary_type": rg_primary,
            "secondary_types": rg_secondary,
        })
    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "disambiguation": data.get("disambiguation") or "",
        "artist": _credit(data),
        "artist_mbid": next(
            (ac["artist"]["id"] for ac in data.get("artist-credit") or [] if "artist" in ac), None
        ),
        "length": data.get("length"),
        "genres": _title_genres(data),
        # a recording lookup returns bare ISRC strings ("USRC17607839") while
        # some other entities wrap them in {"isrc": ...} — .get() on a string
        # raised AttributeError and 502'd the whole recording page.
        "isrcs": [v for v in (_isrc(i) for i in data.get("isrcs") or []) if v],
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
            for m in release_media:
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

    At least one of plain/synced must be non-empty; both may be sent.
    Returns (ok, message). The public API requires a descriptive
    User-Agent, which _LRCLIB_HEADERS already carries."""
    artist = (artist or "").strip()
    track = (track or "").strip()
    album = (album or "").strip()
    plain = (plain or "").strip() or None
    synced = (synced or "").strip() or None
    if not artist or not track:
        return False, "artist and track name are required"
    if not plain and not synced:
        return False, "nothing to publish — add plain or synced lyrics"
    try:
        duration = int(duration or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration <= 0:
        return False, "track duration is required for publishing"
    params = {
        "artist_name": artist,
        "track_name": track,
        "album_name": album or track,
        "duration": duration,
    }
    body = {"plainLyrics": plain or "", "syncedLyrics": synced or ""}
    try:
        r = httpx.post(f"{LRCLIB_BASE}/publish", params=params, json=body,
                       headers=_LRCLIB_HEADERS, timeout=20)
    except Exception as e:
        return False, f"publish failed: {e}"
    if r.status_code in (200, 201):
        return True, "published to LRCLIB — thank you for contributing!"
    if r.status_code == 429:
        return False, "LRCLIB is rate-limiting this IP — try again in a minute"
    detail = (r.text or "").strip()[:200]
    return False, f"LRCLIB refused ({r.status_code}): {detail or 'unknown error'}"



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


def cover_search(artist, album, limit=40, timeout=60.0, sources=None,
                 country=None, cfg=None):
    """Search COV for album covers. Returns a list of
    {source, small, big, title, artist, tracks, url} dicts sorted by the
    site's relevance order, capped at *limit*.

    `sources` and `country` override the saved defaults for this one search
    (the finder's source picker and region dropdown)."""
    if not artist and not album:
        raise ValueError("artist or album is required")
    src_ids, ctry = resolve_cov_search(sources, country, cfg)
    body = {"country": ctry, "sources": src_ids}
    if artist:
        body["artist"] = artist
    if album:
        body["album"] = album
    headers = {
        "User-Agent": COV_UA,
        "Referer": f"{COV_BASE}/",
        "Origin": COV_BASE,
        "X-Session": uuid.uuid4().hex,
    }
    results = []
    with httpx.Client(timeout=httpx.Timeout(timeout, read=timeout)) as client:
        with client.stream("POST", f"{COV_BASE}/api/search", json=body,
                           headers=headers) as r:
            r.raise_for_status()
            for line in r.iter_lines():
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
                results.append({
                    "source": ev.get("source"),
                    "small": ev.get("smallCoverUrl"),
                    "big": ev.get("bigCoverUrl"),
                    "title": rel.get("title"),
                    "artist": rel.get("artist"),
                    "tracks": rel.get("tracks"),
                    "url": rel.get("url"),
                })
                if len(results) >= limit:
                    break
    return results


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
    """First provider hit for a track across the lyrics chain (LRCLIB, NetEase,
    lyrics.ovh, Kugou) — same dict as mlo.lyrics_providers.fetch_lyrics, or
    None. Imported lazily so the engine module stays out of this header."""
    from mlo.lyrics_providers import fetch_lyrics
    return fetch_lyrics(cfg, artist, track, album, duration)
