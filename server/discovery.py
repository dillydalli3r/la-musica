"""Discovery — the external music APIs behind artist artwork, artist/album
descriptions, cover fallbacks, advisory/genre sources and YouTube matching.

Every provider here is keyless, and every one of them was verified against the
live service before being wired in:

* **deezer** — Deezer's public API. Related artists, artist albums/tracks,
  real popularity numbers (`nb_fan` per artist, `fans` per album, `rank` per
  track) and 1000px artist images. No key, no quota.
* **listenbrainz** — MetaBrainz' ListenBrainz sitewide statistics. MBID-native
  rows (release, release group, artist) with Cover Art Archive ids, so results
  need no identity resolution at all. 1 req/s etiquette, like MusicBrainz.
* **itunes** — Apple's Search API. Catalogue fallback for album lookups, and
  its artwork URLs can be rewritten to 3000px (`/100x100bb.jpg` →
  `/3000x3000bb.jpg`).
* **audiodb** — TheAudioDB. Artist biographies, album notes, artist thumbs /
  banners, plus MusicBrainz and Wikipedia ids to hop across.
* **wikipedia** — Wikipedia REST summaries. The description source of record
  for artists and albums; `musicbrainz` is the resolver that finds the article
  through the entity's Wikidata URL relation.

MusicBrainz stays the identity anchor for the whole app: anything that can be
added or downloaded resolves to a release-group MBID through
`resolve_release_group` (via the shared, rate-limited `integrations.search_mb`
cache), and MusicBrainz is the final fallback for every chain here — so a
discovery provider going dark degrades to "fewer, plainer results", never to
"no results".

Everything is TTL-cached in-process (`_CACHE`, 30 min for metadata, 15 min for
charts) with per-host throttling, so repeat page views never re-hit the
network.
"""
import re
import threading
import time
import unicodedata
from html.parser import HTMLParser
from urllib.parse import quote

import httpx

# Importing it installs the ONE shared client behind the module-level
# `httpx.get` this module's provider JSON seam uses (server/httpclient.py).
from server import httpclient  # noqa: F401
from server import integrations

DEEZER_BASE = "https://api.deezer.com"
LISTENBRAINZ_BASE = "https://api.listenbrainz.org/1"
ITUNES_BASE = "https://itunes.apple.com"
AUDIODB_BASE = "https://theaudiodb.com/api/v1/json"
WIKIPEDIA_BASE = "https://en.wikipedia.org"

# TheAudioDB's documented free test key ("2"). It is what the project's own
# docs hand out for evaluation; without a key TheAudioDB answers 403 and the
# chain simply moves on to the next provider.
AUDIODB_KEY = "2"

# Browses use the app UA (same contact string MusicBrainz gets); Deezer and
# TheAudioDB see a browser UA because their CDNs are picky about bots.
APP_UA = integrations.USER_AGENT
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# --------------------------------------------------------------------------- #
# Cached, throttled JSON transport
# --------------------------------------------------------------------------- #
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 600
_INFLIGHT: dict = {}

# host -> (lock, last_request). Deezer allows ~50 req/5s, ListenBrainz asks for
# 1 req/s; MusicBrainz politeness lives in integrations.
_THROTTLE: dict = {}
_THROTTLE_LOCK = threading.Lock()
_HOST_WAIT = {
    "api.listenbrainz.org": 1.05,
    "musicbrainz.org": 1.05,
    "api.deezer.com": 0.15,
    # The crowdsourced/keyed services ask for 1 req/s as well; Wikidata's
    # documented policy is the same. A missing entry means "no wait", which is
    # only right for the CDNs (Apple, TheAudioDB).
    "ws.audioscrobbler.com": 1.05,
    "api.discogs.com": 1.05,
    "www.wikidata.org": 1.05,
    # ListenBrainz Labs serves the similar-artists feed; it is the same
    # MetaBrainz service as api.listenbrainz.org, so it keeps the same 1 req/s.
    "labs.api.listenbrainz.org": 1.05,
}


# One clear line per host per process: a provider going dark must be visible
# in the import log without every track printing its own traceback.
_HOST_WARNED: set = set()
_HOST_WARNED_LOCK = threading.Lock()


def _host_unreachable(host, reason):
    """Print one line the first time *host* fails, then stay quiet."""
    with _HOST_WARNED_LOCK:
        if host in _HOST_WARNED:
            return
        _HOST_WARNED.add(host)
    print(f"[mlo] {host}: {reason} — that source is skipped this run")


# Why a host last refused, per host: the status and the body it sent back.
# `_json` answers None for every failure, which is right for a library pass —
# but a REJECTED key and "this album is not there" both arrive as None, and
# the settings panel has to tell them apart ("Discogs refused the token: HTTP
# 401 …" is a different fix from "no release matched"). One entry per host is
# enough because the only reader is a probe that just made the call itself.
_LAST_HTTP: dict = {}
_LAST_HTTP_LOCK = threading.Lock()


def last_http_error(host):
    """The last refusal *host* gave, or {} — `{status, body, url, at}`.

    `status` is None when nothing answered at all (DNS, refused, timeout);
    `body` is the first 300 characters of the response, verbatim, because a
    provider's own sentence ("Invalid API key") is the whole diagnosis."""
    with _LAST_HTTP_LOCK:
        got = _LAST_HTTP.get(str(host or ""))
        return dict(got) if got else {}


def _record_http_error(host, url, status, body):
    with _LAST_HTTP_LOCK:
        _LAST_HTTP[host] = {"status": status, "body": (body or "")[:300],
                            "url": url, "at": time.time()}


class ProviderRefused(RuntimeError):
    """A provider call that FAILED — carrying the provider's own words.

    Everything else here answers [] for "nothing matched", which is right for a
    chain that must degrade quietly. A CHART is the opposite case: an empty list
    and a refused request look identical on screen, and the difference is the
    whole diagnosis ("Last.fm said Invalid API key", "RYM served Cloudflare").
    The chart readers raise this instead, and the caller reports it verbatim."""


def _refusal(host, since):
    """Why *host* last refused, in its own words, or "" — a refusal older than
    *since* belongs to an earlier call and is not reported as this one's."""
    got = last_http_error(host)
    if not got or float(got.get("at") or 0) < float(since):
        return ""
    if got.get("status"):
        return "HTTP %s: %s" % (got["status"], str(got.get("body") or "").strip())
    return str(got.get("body") or "").strip() or "no answer"


def _rank_rows(rows):
    """Stamp each row with its 1-based position in the provider's own order: a
    chart's ranking IS its data, and the order the provider gave is kept."""
    for i, row in enumerate(rows, 1):
        row["rank"] = i
    return rows


TTL_META = 1800.0        # artist/album metadata and MBIDs
TTL_CHART = 900.0        # charts (they move)
# How long one provider request may take before it is given up on.
DEFAULT_TIMEOUT_S = 12.0


def invalidate(prefix=None):
    """Drop cached payloads (all of them, or those whose key starts with
    *prefix*). Callers use this after a lookup they know just changed."""
    with _CACHE_LOCK:
        if not prefix:
            _CACHE.clear()
            return
        for key in [k for k in _CACHE if k.startswith(prefix)]:
            _CACHE.pop(key, None)


def _throttle(host):
    with _THROTTLE_LOCK:
        entry = _THROTTLE.get(host)
        if entry is None:
            entry = [threading.Lock(), 0.0]
            _THROTTLE[host] = entry
    lock, _ = entry
    wait = _HOST_WAIT.get(host, 0.0)
    with lock:
        elapsed = time.time() - entry[1]
        if wait and elapsed < wait:
            time.sleep(wait - elapsed)
        entry[1] = time.time()


def _json(url, params=None, headers=None, timeout=None, ttl=TTL_META, host=None):
    """GET a JSON document with TTL caching, single-flight and throttling.

    Returns the decoded body, or None for any failure — discovery providers
    are best-effort and must never take a route down with them.
    """
    key = (url, tuple(sorted((k, str(v)) for k, v in (params or {}).items())))
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        flight = _INFLIGHT.get(key)
        if flight is None:
            flight = threading.Event()
            _INFLIGHT[key] = flight
            owner = True
        else:
            owner = False
    if not owner:
        flight.wait(timeout=20)
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        return hit[1] if hit else None
    try:
        target = host or (httpx.URL(url).host or "")
        _throttle(target)
        # MetaBrainz and Wikimedia both require a descriptive, contactable UA
        # (Wikimedia answers 403 to a bare browser string — Wikidata included:
        # `www.wikidata.org` is not matched by "wikimedia"); the storefront
        # APIs (Deezer, Apple, TheAudioDB) serve a browser UA.
        polite = any(tag in target for tag in
                     ("musicbrainz", "listenbrainz", "wikimedia", "wikipedia",
                      "wikidata"))
        # A caller's own headers (Spotify's bearer token, for one) win over
        # the defaults — they are required, not cosmetic.
        sent = {"User-Agent": APP_UA if polite else BROWSER_UA,
                "Accept": "application/json"}
        sent.update(headers or {})
        # A caller's own timeout wins; otherwise the module default below.
        if not timeout:
            timeout = DEFAULT_TIMEOUT_S
        resp = httpx.get(
            url,
            params=params or {},
            headers=sent,
            timeout=timeout or 12.0,
            follow_redirects=True,
        )
        if resp.status_code >= 400:
            data = None
            # 404 is an identity that does not exist there, not a dead source.
            if resp.status_code != 404:
                _host_unreachable(target, f"HTTP {resp.status_code}")
            # EVERY >=400 keeps its body: a 401/403 from a keyed provider is
            # the credential answer, and the provider's own words are the only
            # honest way to report it.
            _record_http_error(target, url, resp.status_code,
                               getattr(resp, "text", ""))
        else:
            data = resp.json()
    except Exception as e:
        data = None
        _record_http_error(host or "", url, None, f"{type(e).__name__}: {e}")
        _host_unreachable(host or "", f"no answer ({type(e).__name__})")
    finally:
        with _CACHE_LOCK:
            if data is not None:
                if len(_CACHE) >= _CACHE_MAX:
                    for old in sorted(_CACHE, key=lambda k: _CACHE[k][0])[: _CACHE_MAX // 4]:
                        _CACHE.pop(old, None)
                _CACHE[key] = (time.time(), data)
            _INFLIGHT.pop(key, None)
        flight.set()
    return data


def _norm(text):
    """Lowercase, accent-folded, punctuation-free comparison key.

    The fold is NFKD plus the combining marks dropped, i.e. the two spellings
    of a title a provider may carry are the SAME key: "Störagéd" and
    "Storaged" both read "storaged", "Björk" and "Bjork" both read "bjork".
    Doing it by stripping non-[a-z0-9] alone does not fold anything — it turns
    the letter into a SEPARATOR ("ö" → a space), which read "Störagéd" as
    "st rag d" and made "Motörhead" collide with "Mot rhead".
    """
    folded = "".join(ch for ch in unicodedata.normalize("NFKD", str(text or ""))
                     if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", folded.casefold()).strip()


# Public alias — other modules (routes, integrations) match titles/artists
# against library keys and must use the same normalization.
norm = _norm


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fans_label(count):
    if not count:
        return None
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M fans"
    if count >= 1_000:
        return f"{round(count / 1_000)}k fans"
    return f"{count} fans"


def _listens_label(count):
    if not count:
        return None
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M listens"
    if count >= 1_000:
        return f"{round(count / 1_000)}k listens"
    return f"{count} listens"


# --------------------------------------------------------------------------- #
# Deezer
# --------------------------------------------------------------------------- #
def deezer_search_artist(name, limit=5, timeout=None):
    """Artist candidates for a name, popularity-sorted by the API itself."""
    data = _json(f"{DEEZER_BASE}/search/artist",
                 {"q": name, "limit": limit}, timeout=timeout)
    rows = []
    for item in (data or {}).get("data") or []:
        rows.append({
            "id": _int(item.get("id")),
            "name": item.get("name") or "",
            "image": item.get("picture_xl") or item.get("picture_big"),
            "popularity": _int(item.get("nb_fan")) or 0,
            "albums": _int(item.get("nb_album")),
            "link": item.get("link"),
        })
    rows.sort(key=lambda r: (-r["popularity"], _norm(r["name"]) != _norm(name)))
    return rows


def deezer_artist(id_or_name, timeout=None):
    """Resolve a Deezer artist id (or the best match for a name)."""
    dz_id = _int(id_or_name)
    if dz_id is None:
        hits = deezer_search_artist(str(id_or_name), limit=1, timeout=timeout)
        dz_id = hits[0]["id"] if hits else None
    if dz_id is None:
        return None
    data = _json(f"{DEEZER_BASE}/artist/{dz_id}", timeout=timeout)
    if not data or data.get("error"):
        return None
    return {
        "id": _int(data.get("id")),
        "name": data.get("name") or "",
        "image": data.get("picture_xl") or data.get("picture_big"),
        "popularity": _int(data.get("nb_fan")) or 0,
        "albums": _int(data.get("nb_album")),
        "link": data.get("link"),
    }


def deezer_related_artists(id_or_name, limit=12, timeout=None):
    """Deezer's "fans also like" list — the similarity signal."""
    artist = deezer_artist(id_or_name, timeout=timeout)
    if not artist:
        return []
    data = _json(f"{DEEZER_BASE}/artist/{artist['id']}/related",
                 {"limit": limit}, timeout=timeout)
    rows = []
    for item in (data or {}).get("data") or []:
        rows.append({
            "id": _int(item.get("id")),
            "name": item.get("name") or "",
            "image": item.get("picture_xl") or item.get("picture_big"),
            "popularity": _int(item.get("nb_fan")) or 0,
            "link": item.get("link"),
            "source": "deezer",
        })
    return rows


def deezer_artist_albums(id_or_name, limit=50, timeout=None, albums_only=False):
    """An artist's albums, newest first, filtered of singles/compilations when
    *albums_only* is set. Rows carry Deezer's own `fans` popularity."""
    artist = deezer_artist(id_or_name, timeout=timeout)
    if not artist:
        return []
    data = _json(f"{DEEZER_BASE}/artist/{artist['id']}/albums",
                 {"limit": limit}, timeout=timeout)
    rows = []
    for item in (data or {}).get("data") or []:
        record_type = (item.get("record_type") or "").lower()
        if albums_only and record_type != "album":
            continue
        rows.append({
            "kind": "album",
            "deezer_id": _int(item.get("id")),
            "title": item.get("title") or "",
            "artist": artist["name"],
            "artist_id": artist["id"],
            "artist_image": artist["image"],
            "cover": item.get("cover_xl") or item.get("cover_big"),
            "year": (item.get("release_date") or "")[:4],
            "release_date": item.get("release_date") or "",
            "record_type": record_type,
            "popularity": _int(item.get("fans")) or 0,
            "popularity_label": _fans_label(_int(item.get("fans"))),
            "link": item.get("link"),
            "source": "deezer",
        })
    return rows


def deezer_artist_top(id_or_name, limit=20, timeout=None):
    """An artist's most popular tracks (`rank` is Deezer's per-track score)."""
    artist = deezer_artist(id_or_name, timeout=timeout)
    if not artist:
        return []
    data = _json(f"{DEEZER_BASE}/artist/{artist['id']}/top",
                 {"limit": limit}, timeout=timeout)
    rows = []
    for item in (data or {}).get("data") or []:
        album = item.get("album") or {}
        rows.append({
            "kind": "track",
            "deezer_id": _int(item.get("id")),
            "title": item.get("title") or "",
            "artist": (item.get("artist") or {}).get("name") or artist["name"],
            "artist_id": artist["id"],
            "album": album.get("title") or "",
            "album_id": _int(album.get("id")),
            "cover": album.get("cover_xl") or album.get("cover_big"),
            "duration": _int(item.get("duration")),
            "popularity": _int(item.get("rank")) or 0,
            "popularity_label": None,
            "link": item.get("link"),
            "source": "deezer",
        })
    rows.sort(key=lambda r: -r["popularity"])
    return rows


def deezer_search_album(query, limit=20, timeout=None):
    """Album search. Returns the same row shape as everything else here."""
    data = _json(f"{DEEZER_BASE}/search/album",
                 {"q": query, "limit": limit}, timeout=timeout)
    rows = []
    for item in (data or {}).get("data") or []:
        artist = item.get("artist") or {}
        rows.append({
            "kind": "album",
            "deezer_id": _int(item.get("id")),
            "title": item.get("title") or "",
            "artist": artist.get("name") or "",
            "artist_id": _int(artist.get("id")),
            "artist_image": artist.get("picture_xl") or artist.get("picture_big"),
            "cover": item.get("cover_xl") or item.get("cover_big"),
            "year": "",
            "record_type": (item.get("record_type") or "").lower(),
            "tracks": _int(item.get("nb_tracks")),
            "popularity": None,
            "link": item.get("link"),
            "source": "deezer",
        })
    return rows


def deezer_album(dz_id, timeout=None):
    """Full album detail: genres, label, tracks with per-track rank."""
    data = _json(f"{DEEZER_BASE}/album/{_int(dz_id)}", timeout=timeout)
    if not data or data.get("error"):
        return None
    artist = data.get("artist") or {}
    genres = [g.get("name") for g in ((data.get("genres") or {}).get("data") or [])
              if g.get("name")]
    tracks = []
    for item in ((data.get("tracks") or {}).get("data") or []):
        tracks.append({
            "title": item.get("title") or "",
            "duration": _int(item.get("duration")),
            "rank": _int(item.get("rank")),
        })
    return {
        "kind": "album",
        "deezer_id": _int(data.get("id")),
        "title": data.get("title") or "",
        "artist": artist.get("name") or "",
        "artist_id": _int(artist.get("id")),
        "artist_image": artist.get("picture_xl") or artist.get("picture_big"),
        "cover": data.get("cover_xl") or data.get("cover_big"),
        "year": (data.get("release_date") or "")[:4],
        "release_date": data.get("release_date") or "",
        "record_type": (data.get("record_type") or "").lower(),
        "label": data.get("label") or "",
        "tracks": _int(data.get("nb_tracks")),
        "duration": _int(data.get("duration")),
        "genres": genres,
        "popularity": _int(data.get("fans")) or 0,
        "popularity_label": _fans_label(_int(data.get("fans"))),
        "link": data.get("link"),
        "track_list": tracks,
        "source": "deezer",
    }


def deezer_album_cover(dz_id, timeout=None):
    """Just the album cover URL — used when all we have is a Deezer id."""
    detail = deezer_album(dz_id, timeout=timeout)
    return (detail or {}).get("cover")


# Deezer's chart is `/chart/0/<entity>` — one global chart per kind, no window
# and no genre, so it appears for every period the app offers only as "what is
# charting right now"; the registry says exactly that rather than pretending a
# period was applied.
_DEEZER_CHART_KINDS = ("albums", "artists", "tracks")


def deezer_chart(kind, limit=50, timeout=None):
    """Deezer's current sitewide chart for one kind (albums/artists/tracks).

    Rows come back in Deezer's own chart order and carry that position as
    `rank`; `popularity` is Deezer's stated number (fans for an album, rank for
    a track) and is labelled the way the rest of this module labels it. Raises
    `ProviderRefused` when Deezer does not answer — never an empty list that
    would read as "nothing is charting"."""
    kind = str(kind or "").strip().lower()
    if kind not in _DEEZER_CHART_KINDS:
        raise ProviderRefused("Deezer publishes no %s chart" % (kind or "such"))
    started = time.time()
    data = _json(f"{DEEZER_BASE}/chart/0/{kind}", {"limit": max(1, int(limit))},
                 timeout=timeout, ttl=TTL_CHART, host="api.deezer.com")
    if data is None:
        raise ProviderRefused(_refusal("api.deezer.com", started)
                              or "Deezer did not answer its chart route")
    rows = []
    for item in (data or {}).get("data") or []:
        artist = item.get("artist") or {}
        album = item.get("album") or {}
        if kind == "albums":
            rows.append({
                "kind": "album",
                "deezer_id": _int(item.get("id")),
                "title": item.get("title") or "",
                "artist": artist.get("name") or "",
                "artist_id": _int(artist.get("id")),
                "cover": item.get("cover_xl") or item.get("cover_big"),
                "year": (item.get("release_date") or "")[:4],
                "record_type": (item.get("record_type") or "").lower(),
                "tracks": _int(item.get("nb_tracks")),
                "popularity": _int(item.get("fans")) or 0,
                "popularity_label": _fans_label(_int(item.get("fans"))),
                "link": item.get("link"),
                "source": "deezer",
            })
        elif kind == "artists":
            rows.append({
                "kind": "artist",
                "deezer_id": _int(item.get("id")),
                "title": item.get("name") or "",
                "artist": item.get("name") or "",
                "cover": item.get("picture_xl") or item.get("picture_big"),
                "popularity": _int(item.get("nb_fan")) or 0,
                "popularity_label": _fans_label(_int(item.get("nb_fan"))),
                "link": item.get("link"),
                "source": "deezer",
            })
        else:
            rows.append({
                "kind": "track",
                "deezer_id": _int(item.get("id")),
                "title": item.get("title") or "",
                "artist": artist.get("name") or "",
                "artist_id": _int(artist.get("id")),
                "album": album.get("title") or "",
                "album_id": _int(album.get("id")),
                "cover": album.get("cover_xl") or album.get("cover_big"),
                "duration": _int(item.get("duration")),
                "popularity": _int(item.get("rank")) or 0,
                "popularity_label": None,
                "link": item.get("link"),
                "source": "deezer",
            })
    return _rank_rows(rows)


# --------------------------------------------------------------------------- #
# ListenBrainz
# --------------------------------------------------------------------------- #
# Its sitewide stats take an explicit `range`, and its own
# ALLOWED_STATISTICS_RANGE (verified in the API docs) covers every period the
# app offers: `this_week`/`this_month`/`this_year` are the running windows and
# `all_time` is the unbounded one. A period therefore maps to a REAL window
# here — it is never dropped in favour of all-time.
LB_CHART_RANGE = {"week": "this_week", "month": "this_month",
                  "year": "this_year", "all": "all_time"}
_LB_CHART_KINDS = ("albums", "artists", "tracks")


def listenbrainz_chart(kind, period="all", limit=50, timeout=None):
    """ListenBrainz' sitewide most-listened rows for one window.

    Reuses the three `listenbrainz_top_*` readers (one per kind, MBID-native
    rows with Cover Art Archive ids), which already take the range; this only
    resolves the period into the range name their endpoint documents. Raises
    `ProviderRefused` when the service does not answer."""
    kind = str(kind or "").strip().lower()
    period = str(period or "all").strip().lower()
    if kind not in _LB_CHART_KINDS:
        raise ProviderRefused("ListenBrainz publishes no %s stats" % (kind or "such"))
    if period not in LB_CHART_RANGE:
        raise ProviderRefused("ListenBrainz has no %s window" % (period or "such"))
    range_ = LB_CHART_RANGE[period]
    started = time.time()
    if kind == "albums":
        rows = listenbrainz_top_releases(range_, limit, timeout=timeout)
    elif kind == "artists":
        rows = listenbrainz_top_artists(range_, limit, timeout=timeout)
    else:
        rows = listenbrainz_top_recordings(range_, limit, timeout=timeout)
    if not rows and _refusal("api.listenbrainz.org", started):
        raise ProviderRefused(_refusal("api.listenbrainz.org", started))
    return _rank_rows(rows)


def _lb_stats(kind, range_="month", limit=25, offset=0, timeout=None):
    return _json(f"{LISTENBRAINZ_BASE}/stats/sitewide/{kind}",
                 {"range": range_, "count": limit, "offset": offset},
                 timeout=timeout, ttl=TTL_CHART)


# Cover Art Archive's thumbnail: 250 IS the smallest it serves, and a row's
# tile is 40px, so a bigger one is bytes the UI never shows. The URL is the ONE
# way to a MusicBrainz-only entity's cover — `/api/art` proxies it, and its own
# fallback then walks Cover Art Archive → Apple → Deezer by name.
CAA_THUMB = 250


def _caa_front(entity, mbid, size=CAA_THUMB):
    """Cover Art Archive's front cover for one MusicBrainz entity, or None.

    `entity` is Cover Art Archive's own path segment — "release" or
    "release-group"; a RECORDING has no cover endpoint, which is why a track
    row's cover comes from the release it was pressed on (see
    `_mb_recording_row`) and never from the recording's own MBID."""
    mbid = str(mbid or "").strip()
    if not mbid:
        return None
    return f"https://coverartarchive.org/{entity}/{mbid}/front-{int(size)}"


def _caa_group_url(release_mbid):
    """Cover Art Archive front cover for a release (250px thumbnail)."""
    return _caa_front("release", release_mbid)


# Public alias — `server.discover` fills a MusicBrainz-only row's cover from
# the same URL builder, so one convention covers every Discover arm.
caa_front_url = _caa_front


def listenbrainz_top_releases(range_="month", limit=25, timeout=None):
    """The most-listened releases sitewide — real popularity, MBID-native."""
    data = _lb_stats("releases", range_, limit, timeout=timeout)
    rows = []
    for item in ((data or {}).get("payload") or {}).get("releases") or []:
        rows.append({
            "kind": "album",
            "title": item.get("release_name") or "",
            "artist": item.get("artist_name") or "",
            "artist_mbids": item.get("artist_mbids") or [],
            "mbid": item.get("release_mbid"),
            "cover": _caa_group_url(item.get("caa_release_mbid")),
            "popularity": _int(item.get("listen_count")),
            "popularity_label": _listens_label(_int(item.get("listen_count"))),
            "source": "listenbrainz",
            "link": (f"https://musicbrainz.org/release/{item['release_mbid']}"
                     if item.get("release_mbid") else None),
        })
    return rows


def listenbrainz_top_artists(range_="month", limit=25, timeout=None):
    data = _lb_stats("artists", range_, limit, timeout=timeout)
    rows = []
    for item in ((data or {}).get("payload") or {}).get("artists") or []:
        rows.append({
            "kind": "artist",
            "name": item.get("artist_name") or "",
            "mbid": item.get("artist_mbid"),
            "popularity": _int(item.get("listen_count")),
            "popularity_label": _listens_label(_int(item.get("listen_count"))),
            "source": "listenbrainz",
            "link": (f"https://musicbrainz.org/artist/{item['artist_mbid']}"
                     if item.get("artist_mbid") else None),
        })
    return rows


def listenbrainz_top_recordings(range_="month", limit=25, timeout=None):
    data = _lb_stats("recordings", range_, limit, timeout=timeout)
    rows = []
    for item in ((data or {}).get("payload") or {}).get("recordings") or []:
        rows.append({
            "kind": "track",
            "title": item.get("track_name") or "",
            "artist": item.get("artist_name") or "",
            "artist_mbids": item.get("artist_mbids") or [],
            "album": item.get("release_name") or "",
            "mbid": item.get("recording_mbid"),
            "release_mbid": item.get("release_mbid"),
            "cover": _caa_group_url(item.get("caa_release_mbid")),
            "popularity": _int(item.get("listen_count")),
            "popularity_label": _listens_label(_int(item.get("listen_count"))),
            "source": "listenbrainz",
            "link": (f"https://musicbrainz.org/recording/{item['recording_mbid']}"
                     if item.get("recording_mbid") else None),
        })
    return rows


# --------------------------------------------------------------------------- #
# ListenBrainz genre tags
# --------------------------------------------------------------------------- #
# `/1/metadata/<entity>/?<entity>_mbids=<uuid>&inc=tag` answers a dict keyed by
# MBID whose `tag` block has one bucket per entity kind. Each row is
# `{tag, count, genre_mbid?}`:
#
#   * a row WITH `genre_mbid` is a RECOGNISED genre — ListenBrainz matched it
#     to the MusicBrainz genre taxonomy — and is imported as a genre;
#   * a row WITHOUT one is a free tag: how people describe the music. That is
#     as often a mood ("melancholic"), a scene, a language or outright spam
#     (a live sample carried `vyrzukhisuc-artiest`), so a free tag is kept only
#     when it is agreed on (count >= 2) and is not a mood word.
#
# The artist bucket of a recording lookup is deliberately NOT folded in here:
# the chain asks for the artist's own tags as its own, lower tier, and
# attribute-splitting is the caller's decision, not this function's.
_LB_MOOD_WORDS = frozenset("""
melancholic melancholy sad happy lonely anxious angry aggressive calm
chill chillout relaxing relaxed peaceful dreamy atmospheric introspective
emotional energetic powerful beautiful epic romantic sexy party summer
driving uplifting dark upbeat instrumental love night
""".split())
_LB_MIN_FREE_COUNT = 2
_LB_ENTITIES = {
    "recording": ("recording", "recording_mbids"),
    "release_group": ("release_group", "release_group_mbids"),
    "release-group": ("release_group", "release_group_mbids"),
    "artist": ("artist", "artist_mbids"),
}
# What the listeners call a genre, and what only describes a mood.
LB_MOOD_WORDS = _LB_MOOD_WORDS


def listenbrainz_genre_tags(mbid, entity="recording", timeout=None):
    """{"genres": [...], "tags": [...], "source": "listenbrainz"} or None.

    Recognised genres (`genre_mbid` rows) first, then the agreed free tags
    that are not moods — deduped case-insensitively, trimmed, in row order.
    `None` means ListenBrainz stated nothing about this MBID (or did not
    answer): the caller moves to its next tier, never invents a genre.
    """
    ident = str(mbid or "").strip().lower()
    where, param = _LB_ENTITIES.get(str(entity or "").strip().lower(), (None, None))
    if not ident or not where:
        return None
    data = _json(f"{LISTENBRAINZ_BASE}/metadata/{where}/",
                 {param: ident, "inc": "tag"}, timeout=timeout)
    if isinstance(data, list):
        # `/metadata/artist/` answers a LIST of records (the recording and
        # release-group paths answer a dict keyed by MBID); the record for this
        # MBID is picked by its own id, never by position.
        node = next((row for row in data if isinstance(row, dict)
                     and str(row.get("mbid") or row.get("artist_mbid")
                             or "").lower() == ident), None)
        if node is None:
            node = next((row for row in data if isinstance(row, dict)), {}) or {}
    else:
        node = (data or {}).get(ident) or {}
    rows = (node.get("tag") or {}).get(where) or []
    if isinstance(rows, dict):
        rows = [rows]
    genres, tags = [], []

    def add(bucket, name):
        key = str(name or "").strip().lower()
        if not key:
            return
        if any(key == seen for seen in bucket):
            return
        bucket.append(key)

    for row in rows:
        if not isinstance(row, dict) or not row.get("tag"):
            continue
        count = _int(row.get("count")) or 0
        name = str(row.get("tag")).strip()
        if row.get("genre_mbid"):
            add(genres, name)
        elif count >= _LB_MIN_FREE_COUNT and name.lower() not in _LB_MOOD_WORDS:
            add(tags, name)
    if not genres and not tags:
        return None
    return {"genres": genres, "tags": tags, "source": "listenbrainz"}


# --------------------------------------------------------------------------- #
# Wikidata
# --------------------------------------------------------------------------- #
WIKIDATA_API = "https://www.wikidata.org/w/api.php"


def _wikidata(params, timeout=None):
    sent = dict(params)
    sent["format"] = "json"
    sent["origin"] = "*"
    return _json(WIKIDATA_API, sent, timeout=timeout, host="www.wikidata.org")


def wikidata_entity(term, timeout=None):
    """The QID Wikidata's own search considers the best match for *term*."""
    term = str(term or "").strip()
    if not term:
        return None
    data = _wikidata({"action": "wbsearchentities", "search": term,
                      "language": "en", "uselang": "en", "limit": 5,
                      "type": "item"}, timeout=timeout)
    hits = (data or {}).get("search") or []
    return (hits[0] or {}).get("id") if hits else None


def wikidata_genres(qid="", term="", timeout=None):
    """Genres Wikidata states for one entity (P136), or None.

    `qid` is the entity (a release group's own Wikidata relation when the
    caller has it); `term` is the fallback search "artist album". P136 values
    are item ids, resolved to their English labels in one extra request — a
    claim whose label cannot be read is dropped, never guessed. None when the
    entity or its genre statements cannot be read.
    """
    qid = str(qid or "").strip()
    if not qid:
        qid = wikidata_entity(term, timeout=timeout) or ""
    if not qid:
        return None
    claims = _wikidata({"action": "wbgetclaims", "entity": qid,
                        "property": "P136"}, timeout=timeout)
    ids = []
    for claim in ((claims or {}).get("claims") or {}).get("P136") or []:
        value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        got = value.get("id") if isinstance(value, dict) else value
        if isinstance(got, str) and got.startswith("Q") and got not in ids:
            ids.append(got)
    if not ids:
        return None
    data = _wikidata({"action": "wbgetentities", "ids": "|".join(ids),
                      "props": "labels", "languages": "en"}, timeout=timeout)
    entities = (data or {}).get("entities") or {}
    genres = []
    for item in ids:
        labels = ((entities.get(item) or {}).get("labels") or {})
        label = (labels.get("en") or {}).get("value")
        if label and label.strip().lower() not in {g.lower() for g in genres}:
            genres.append(label.strip())
    if not genres:
        return None
    return {"genres": genres, "qid": qid, "qids": ids, "source": "wikidata"}


# --------------------------------------------------------------------------- #
# iTunes
# --------------------------------------------------------------------------- #
def itunes_artwork(url, size=3000):
    """Rewrite an iTunes artwork URL to *size* px (their URLs are templates)."""
    if not url:
        return None
    return re.sub(r"/\d+x\d+(bb|sr)?\.(jpg|png)", f"/{size}x{size}bb.jpg", url)


def itunes_search_album(artist, album, limit=5, timeout=None):
    term = " ".join(t for t in (artist, album) if t)
    data = _json(f"{ITUNES_BASE}/search",
                 {"term": term, "entity": "album", "limit": limit}, timeout=timeout)
    rows = []
    for item in (data or {}).get("results") or []:
        rows.append({
            "kind": "album",
            "itunes_id": _int(item.get("collectionId")),
            "title": item.get("collectionName") or "",
            "artist": item.get("artistName") or "",
            "artist_id": _int(item.get("artistId")),
            "cover": itunes_artwork(item.get("artworkUrl100")),
            "year": (item.get("releaseDate") or "")[:4],
            "release_date": item.get("releaseDate") or "",
            "tracks": _int(item.get("trackCount")),
            "genre": item.get("primaryGenreName") or "",
            "link": item.get("collectionViewUrl"),
            "source": "itunes",
        })
    return rows


def itunes_search_artist(name, limit=5, timeout=None):
    data = _json(f"{ITUNES_BASE}/search",
                 {"term": name, "entity": "musicArtist", "limit": limit},
                 timeout=timeout)
    rows = []
    for item in (data or {}).get("results") or []:
        rows.append({
            "kind": "artist",
            "itunes_id": _int(item.get("artistId")),
            "name": item.get("artistName") or "",
            "genre": item.get("primaryGenreName") or "",
            "link": item.get("artistLinkUrl"),
            "source": "itunes",
        })
    return rows


# --------------------------------------------------------------------------- #
# Discogs / Last.fm — crowdsourced genres, both keyed
# --------------------------------------------------------------------------- #
DISCOGS_BASE = "https://api.discogs.com"
LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"


def _discogs_release(artist, album, cfg=None, timeout=None):
    """The Discogs release detail for an album; None without a token or a hit.

    Discogs' search endpoint requires authentication, so the source is
    skipped (never guessed) until `discogs_token` is set in Settings. The
    detail object is what both the genre and the (weak) Parental Advisory
    routes read, so both are answered by ONE cached pair of requests.
    """
    token = str((cfg or {}).get("discogs_token") or "").strip()
    if not token or not (artist or album):
        return None
    data = _json(f"{DISCOGS_BASE}/database/search",
                 {"artist": artist, "release_title": album, "type": "release",
                  "token": token, "per_page": 3}, timeout=timeout,
                 host="api.discogs.com")
    results = (data or {}).get("results") or []
    if not results:
        return None
    rid = results[0].get("id")
    if not rid:
        return None
    # `host=` labels both requests as Discogs for the throttle, the "one line
    # per dead host" log AND the refusal record a probe reads — one service
    # must not split into two hostnames.
    return _json(f"{DISCOGS_BASE}/releases/{rid}", {"token": token},
                 timeout=timeout, host="api.discogs.com")


def discogs_album_genres(artist, album, cfg=None, timeout=None):
    """Discogs genres+styles for an album; [] without a configured token.

    The release's `genre` and `style` lists are both genres for tagging
    purposes (`style` is the finer one, and RYM-style tagging wants both).
    """
    detail = _discogs_release(artist, album, cfg=cfg, timeout=timeout) or {}
    out = list(detail.get("genres") or [])
    out += list(detail.get("styles") or [])
    return [g for g in out if str(g).strip()]


def discogs_parental_advisory(artist, album, cfg=None, timeout=None):
    """True when Discogs lists a "Parental Advisory" release format.

    Album-level and WEAK evidence — a sticker on the edition Discogs matched,
    not a statement about any one track — so it is only ever used as an extra
    explicit signal, and its absence states nothing. None without a token.
    """
    detail = _discogs_release(artist, album, cfg=cfg, timeout=timeout)
    if not detail:
        return None
    for fmt in detail.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        text = " ".join([str(fmt.get("name") or "")]
                        + [str(d) for d in fmt.get("descriptions") or []])
        if "parental advisory" in text.lower():
            return True
    return None


def _lastfm_key(cfg):
    return str((cfg or {}).get("lastfm_api_key") or "").strip()


# The host every Last.fm call is throttled and recorded against.
_LASTFM_HOST = "ws.audioscrobbler.com"


def _lastfm_json(method, params, cfg=None, timeout=None, ttl=TTL_META):
    """One Last.fm call, or None without a key / on any failure.

    The keyed sibling of `_json` for audioscrobbler: same transport, same TTL
    cache and same 1 req/s etiquette, with the API key and the JSON envelope
    Last.fm insists on. Every Last.fm reader in this module (the genre chain
    and the Discover tag feeds) goes through this one seam.

    A REFUSED key is recorded, not swallowed: Last.fm states an invalid key in
    its own envelope (`{"error": 10, "message": "Invalid API key"}`) which is
    an ordinary 200 or 403 carrying nothing the readers look for, so without
    this the source reported "no Last.fm tags" for a key that was never
    accepted. `lastfm_last_error()` is what the Test row reads."""
    key = _lastfm_key(cfg)
    if not key:
        return None
    sent = dict(params, method=method, api_key=key, format="json", autocorrect=1)
    data = _json(LASTFM_BASE, sent, timeout=timeout, ttl=ttl,
                 host=_LASTFM_HOST)
    if isinstance(data, dict) and data.get("error"):
        code = data.get("error")
        message = str(data.get("message") or "").strip()
        _record_http_error(_LASTFM_HOST, LASTFM_BASE, None,
                           f"error {code}: {message}" if message
                           else f"error {code}")
        return None
    return data


def lastfm_last_error(since=None):
    """Why Last.fm last refused, as its own words, or "" — see `_lastfm_json`.

    `since` (a `time.time()` stamp taken before the call) filters out a
    refusal from an earlier run: the record is per host and process-wide, so
    without it a probe could report an earlier 403 as its own answer."""
    got = last_http_error(_LASTFM_HOST)
    if not got or (since and float(got.get("at") or 0) < float(since)):
        return ""
    if got.get("status"):
        return f"HTTP {got['status']}: {got.get('body') or ''}".strip()
    return str(got.get("body") or "")


def lastfm_check(cfg=None, timeout=None):
    """Is the saved `lastfm_api_key` accepted? -> `{ok, checked, detail}`.

    `chart.gettoptags` is the cheapest call Last.fm answers for a key alone: a
    chart, so no user, no artist and no tag has to exist for it to be a real
    request. Live by construction (`ttl=0`) — a Test button must ask again
    after a key is pasted, not be served this process's cached answer."""
    key = _lastfm_key(cfg)
    if not key:
        return {"ok": False, "checked": "",
                "detail": "no lastfm_api_key is set"}
    started = time.time()
    data = _lastfm_json("chart.gettoptags", {"limit": 1}, cfg, timeout=timeout,
                        ttl=0)
    tags = (((data or {}).get("tags") or {}).get("tag")) or []
    if isinstance(tags, dict):
        tags = [tags]
    if tags:
        return {"ok": True, "detail": ("key accepted — chart.gettoptags "
                                       "answered"),
                "checked": "chart.gettoptags", "tags": len(tags)}
    reason = lastfm_last_error(started)
    return {"ok": False, "checked": "chart.gettoptags",
            "detail": (f"Last.fm rejected the API key — {reason}" if reason
                       else "Last.fm answered chart.gettoptags with no tags")}


def discogs_check(cfg=None, timeout=None):
    """Is the saved `discogs_token` accepted? -> `{ok, checked, detail}`.

    `/oauth/identity` is Discogs' own "who is this token" endpoint, and it is
    the ONE endpoint that wants the token in a header
    (`Authorization: Discogs token=<t>`) rather than the `token` query
    parameter the data endpoints take — Discogs documents both forms, and a
    token pasted from the developer page works as either. Asking identity
    separately is what tells a REJECTED token apart from "the sample release
    is not in Discogs": `database/search` answers anonymously, so a discarded
    token used to look like a working source."""
    token = str((cfg or {}).get("discogs_token") or "").strip()
    if not token:
        return {"ok": False, "checked": "",
                "detail": "no discogs_token is set"}
    started = time.time()
    data = _json(f"{DISCOGS_BASE}/oauth/identity", {}, headers={
        "Authorization": f"Discogs token={token}"}, timeout=timeout, ttl=0,
        host="api.discogs.com")
    username = str((data or {}).get("username") or "").strip()
    if username:
        return {"ok": True, "checked": "GET /oauth/identity",
                "detail": f"token accepted as {username} (GET /oauth/identity)",
                "username": username}
    got = last_http_error("api.discogs.com")
    if float(got.get("at") or 0) < started:
        got = {}
    if got.get("status"):
        return {"ok": False, "checked": "GET /oauth/identity",
                "detail": f"Discogs rejected the token — HTTP {got['status']}"
                          + (f" {got['body']}".rstrip() if got.get("body") else "")}
    return {"ok": False, "checked": "GET /oauth/identity",
            "detail": "Discogs did not answer"
                      + (f" ({got['body']})" if got.get("body") else "")}


def _lastfm_tags(method, params, cfg=None, timeout=None):
    """`{method}` top tags for one entity; [] without a key or on any failure."""
    data = _lastfm_json(method, params, cfg, timeout)
    tags = (((data or {}).get("toptags") or {}).get("tag")) or []
    if isinstance(tags, dict):
        tags = [tags]
    return [t.get("name") for t in tags if isinstance(t, dict) and t.get("name")]


def lastfm_album_genres(artist, album, cfg=None, timeout=None):
    """Last.fm top tags for an album; [] without a configured API key."""
    if not _lastfm_key(cfg) or not (artist or album):
        return []
    data = _lastfm_json("album.getinfo", {"artist": artist, "album": album},
                        cfg, timeout)
    tags = (((data or {}).get("album") or {}).get("tags") or {}).get("tag") or []
    if isinstance(tags, dict):
        tags = [tags]
    return [t.get("name") for t in tags if t.get("name")]


def lastfm_track_genres(artist, track, cfg=None, timeout=None):
    """Last.fm top tags for ONE track (the per-track tier); [] without a key."""
    if not track:
        return []
    return _lastfm_tags("track.getTopTags", {"artist": artist, "track": track},
                        cfg, timeout)


def lastfm_artist_genres(artist, cfg=None, timeout=None):
    """Last.fm top tags for an artist (the tier below the track); [] w/o key."""
    if not artist:
        return []
    return _lastfm_tags("artist.getTopTags", {"artist": artist}, cfg, timeout)


# --------------------------------------------------------------------------- #
# TheAudioDB
# --------------------------------------------------------------------------- #
def audiodb_artist(name, timeout=None):
    data = _json(f"{AUDIODB_BASE}/{AUDIODB_KEY}/search.php", {"s": name},
                 timeout=timeout)
    rows = (data or {}).get("artists") or []
    if not rows:
        return None
    item = rows[0]
    return {
        "kind": "artist",
        "name": item.get("strArtist") or name,
        "mbid": item.get("strMusicBrainzID"),
        "bio": (item.get("strBiography") or "").strip(),
        "genre": item.get("strGenre") or "",
        "style": item.get("strStyle") or "",
        "mood": item.get("strMood") or "",
        "thumb": item.get("strArtistThumb"),
        "banner": item.get("strArtistBanner"),
        "fanart": item.get("strArtistFanart"),
        "wide_thumb": item.get("strArtistWideThumb"),
        "formed": item.get("intFormedYear"),
        "country": item.get("strCountry") or "",
        "popularity": _int(item.get("intPopularity")),
        "source": "audiodb",
    }


def audiodb_track(artist, track, timeout=None):
    """TheAudioDB's own row for ONE track, or None when it states none.

    `searchtrack.php` is the source's PER-TRACK tier (the row carries
    `strGenre`/`strStyle`/`strMood`); `audiodb_album` is the caller's fallback
    tier. TheAudioDB answers the first rows it has for a title whoever
    recorded it, so a row whose own `strArtist` is not the artist we asked
    about is NO answer — never another artist's genre for our track.
    """
    artist = str(artist or "").strip()
    track = str(track or "").strip()
    if not artist or not track:
        return None
    data = _json(f"{AUDIODB_BASE}/{AUDIODB_KEY}/searchtrack.php",
                 {"s": artist, "t": track}, timeout=timeout)
    want = _norm(artist)
    for item in (data or {}).get("track") or []:
        got = _norm(item.get("strArtist"))
        if not got or not (got == want or want in got or got in want):
            continue
        return {
            "kind": "track",
            "title": item.get("strTrack") or track,
            "artist": item.get("strArtist") or artist,
            "album": item.get("strAlbum") or "",
            "genre": item.get("strGenre") or "",
            "style": item.get("strStyle") or "",
            "mood": item.get("strMood") or "",
            "source": "audiodb",
        }
    return None


def audiodb_album(artist, album, timeout=None):
    data = _json(f"{AUDIODB_BASE}/{AUDIODB_KEY}/searchalbum.php",
                 {"s": artist, "a": album}, timeout=timeout)
    rows = (data or {}).get("album") or []
    if not rows:
        return None
    item = rows[0]
    return {
        "kind": "album",
        "title": item.get("strAlbum") or album,
        "artist": item.get("strArtist") or artist,
        "description": (item.get("strDescription") or "").strip(),
        "thumbnail": item.get("strAlbumThumb"),
        "year": item.get("intYearReleased") or "",
        "genre": item.get("strGenre") or "",
        "mood": item.get("strMood") or "",
        "mbid": item.get("strMusicBrainzID"),
        "wikipedia_id": item.get("strWikipediaID"),
        "wikidata_id": item.get("strWikidataID"),
        "score": _int(item.get("intScore")),
        "popularity": _int(item.get("intPopularity")),
        "source": "audiodb",
    }


# --------------------------------------------------------------------------- #
# Spotify (artist genres — the chain's last resort — and the artist-level
# entity routes: an artist's albums and top tracks are what Spotify honestly
# states about a page, since it publishes no related-artist feed)
# --------------------------------------------------------------------------- #
SPOTIFY_API = "https://api.spotify.com/v1"


def _spotify_artist_hit(name, cfg=None, timeout=None):
    """The ONE artist search behind every artist-level Spotify source.

    One request, and the one rule that makes its hit an identity: the hit's own
    name is the name asked for — a search hit is a candidate, not an identity,
    so a same-named act cannot ride in on the term. Returns the artist OBJECT
    (its id, genres, images), or None without credentials / on no honest hit.

    Empty without `spotify_client_id`/`spotify_client_secret`: the source is
    skipped entirely, never defaulted."""
    text = str(name or "").strip()
    token = integrations._spotify_token(cfg, timeout=timeout)
    if not text or not token:
        return None
    data = _json(f"{SPOTIFY_API}/search",
                 {"q": text, "type": "artist", "limit": 1},
                 headers={"Authorization": f"Bearer {token}"},
                 timeout=timeout, host="api.spotify.com")
    want = _norm(text)
    for item in ((data or {}).get("artists") or {}).get("items") or []:
        if _norm(item.get("name")) == want:
            return item
    return None


def _spotify_market(cfg=None):
    """Spotify's `market`: the app's own region setting, in the uppercase ISO
    3166-1 alpha-2 form the Web API takes (the same `cover_country` key is
    lowercase for the cover storefronts). Anything that is not a two-letter
    region is not a market Spotify knows, so the US storefront answers."""
    code = str((cfg or {}).get("cover_country") or "").strip().upper()
    return code if len(code) == 2 else "US"


def spotify_artist_genres(artist, cfg=None, timeout=None):
    """Spotify's ARTIST genres, or [] without credentials / on any failure.

    Spotify serves no per-track genre at all (a track object carries none), so
    this source is artist-level and says so — which is why it sits last in the
    genre chain, below every album-level source. One request: the artist
    search hit IS the artist object, genres included. The hit counts only when
    its own name is the artist we asked about — a search hit is a candidate,
    not an identity — and an empty `genres` list is no answer, not a guess.

    Empty without `spotify_client_id`/`spotify_client_secret`: the source is
    skipped entirely, never defaulted.
    """
    hit = _spotify_artist_hit(artist, cfg=cfg, timeout=timeout)
    return [str(g).strip() for g in (hit or {}).get("genres") or []
            if str(g).strip()]


def spotify_artist_albums(artist, limit=25, cfg=None, timeout=None):
    """A NAMED artist's albums — Spotify's one honest answer about an entity.

    Spotify publishes no related-artist feed to apps created after 2024-11-27
    (`/artists/{id}/related-artists` and `/recommendations` are on the list of
    endpoints Spotify closed to them), so what this route can state about a
    page is "more from THIS artist": the artist NAMED here, resolved through
    the same name-matched search `spotify_artist_genres` uses. It is never a
    similarity claim. `include_groups=album` leaves singles and compilations
    out on Spotify's side — the line Deezer's entity bridge draws with
    `albums_only`; filtering the page here instead would answer with a shorter
    shelf than the artist has. Empty without credentials, like every Spotify
    wrapper."""
    token = integrations._spotify_token(cfg, timeout=timeout)
    if not token:
        return []
    hit = _spotify_artist_hit(artist, cfg=cfg, timeout=timeout)
    ident = str((hit or {}).get("id") or "")
    if not ident:
        return []
    data = _json(f"{SPOTIFY_API}/artists/{ident}/albums",
                 {"include_groups": "album", "market": _spotify_market(cfg),
                  "limit": max(1, min(50, int(limit)))},
                 headers={"Authorization": f"Bearer {token}"},
                 timeout=timeout, host="api.spotify.com")
    rows = []
    for item in (data or {}).get("items") or []:
        images = item.get("images") or []
        artists = item.get("artists") or []
        rows.append({
            "kind": "album",
            "title": item.get("name") or "",
            "artist": (((artists[0] or {}).get("name") if artists else "")
                       or hit.get("name") or ""),
            "spotify_id": item.get("id"),
            "cover": (images[0] or {}).get("url") if images else None,
            "year": (item.get("release_date") or "")[:4],
            "release_date": item.get("release_date") or "",
            "track_count": _int(item.get("total_tracks")),
            "record_type": item.get("album_type") or "",
            "link": (item.get("external_urls") or {}).get("spotify"),
            "source": "spotify",
        })
    return rows


def spotify_artist_top_tracks(artist, limit=25, cfg=None, timeout=None):
    """A NAMED artist's most popular tracks (`/artists/{id}/top-tracks`).

    The track shelf's "more from this artist", and not a similarity feed for
    the same reason `spotify_artist_albums` is not one. `market` is required
    — the ranking IS the market's — so the app's region setting is sent rather
    than a storefront hardcoded here. Rows carry Spotify's own `popularity`
    (0-100), which is the provider's number and orders them."""
    token = integrations._spotify_token(cfg, timeout=timeout)
    if not token:
        return []
    hit = _spotify_artist_hit(artist, cfg=cfg, timeout=timeout)
    ident = str((hit or {}).get("id") or "")
    if not ident:
        return []
    data = _json(f"{SPOTIFY_API}/artists/{ident}/top-tracks",
                 {"market": _spotify_market(cfg),
                  "limit": max(1, min(50, int(limit)))},
                 headers={"Authorization": f"Bearer {token}"},
                 timeout=timeout, host="api.spotify.com")
    rows = []
    for item in (data or {}).get("tracks") or []:
        album = item.get("album") or {}
        images = album.get("images") or []
        artists = item.get("artists") or []
        rows.append({
            "kind": "track",
            "title": item.get("name") or "",
            "artist": ", ".join(a.get("name") for a in artists if a.get("name"))
                      or (hit.get("name") or ""),
            "spotify_id": item.get("id"),
            "album": album.get("name") or "",
            "cover": (images[0] or {}).get("url") if images else None,
            "duration": _int(item.get("duration_ms")),
            "popularity": _int(item.get("popularity")) or 0,
            "isrc": ((item.get("external_ids") or {}).get("isrc") or ""),
            "link": (item.get("external_urls") or {}).get("spotify"),
            "source": "spotify",
        })
    return rows


# --------------------------------------------------------------------------- #
# Wikipedia
# --------------------------------------------------------------------------- #



# Parsed article HTML (`action=parse`) is the ONE Wikimedia answer that keeps
# an article's links — `prop=extracts` strips every anchor, HTML mode included
# — so it arrives with the whole page around it and is reduced back to prose
# here. Everything below is stdlib `html.parser`; no third-party HTML stack for
# one conversion.
#
# A void element never opens a subtree, so it must not be recorded as one: an
# unclosed `link`/`img` would swallow the rest of the article.
_HTML_VOID = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split())
# Subtrees that are page furniture rather than prose: citations (`sup`), tables
# (infoboxes, navboxes, discographies, award lists), styles, image captions.
_HTML_DROP = frozenset("table style script sup figure audio video".split())
# The rest of the skin names itself by class.
_HTML_DROP_CLASS = re.compile(
    r"navbox|reflist|refbegin|references|thumb|hatnote|metadata|mw-empty-elt|mw-editsection"
    r"|noprint|sidebar|infobox|ambox|tmbox|gallery|Z3988|mw-cite-backlink|cite-bracket")
# Where one block of text ends and the next begins, so paragraphs stay apart.
_HTML_BLOCK = frozenset("p br hr li ul ol dl dd dt div".split())
_HTML_HEADING = re.compile(r"h[1-6]")

















def wikipedia_summary(title, timeout=None):
    """REST summary for an article: description, extract, image, Wikidata id."""
    if not title:
        return None
    data = _json(f"{WIKIPEDIA_BASE}/api/rest_v1/page/summary/{quote(str(title).replace(' ', '_'), safe='')}",
                 timeout=timeout)
    if not data or data.get("type") == "https://mediawiki.org/wiki/HyperSwitch/errors/not_found":
        return None
    extract = (data.get("extract") or "").strip()
    if not extract:
        return None
    return {
        "title": data.get("title") or title,
        "description": data.get("description") or "",
        "extract": extract,
        "url": ((data.get("content_urls") or {}).get("desktop") or {}).get("page"),
        "image": ((data.get("originalimage") or {}).get("source")
                  or (data.get("thumbnail") or {}).get("source")),
        "wikidata_id": data.get("wikibase_item"),
        "source": "wikipedia",
    }


# --------------------------------------------------------------------------- #
# MusicBrainz identity resolution (final fallback + MBID anchor)
# --------------------------------------------------------------------------- #
def _mb_album_row(row):
    """MusicBrainz release-group search row → the shared discovery row shape."""
    mbid = row.get("id")
    return {
        "kind": "album",
        "title": row.get("title") or "",
        "artist": row.get("artist") or "",
        "mbid": mbid,
        "cover": _caa_front("release-group", mbid),
        "year": (row.get("first_release_date") or "")[:4],
        "record_type": (row.get("primary_type") or "").lower(),
        "secondary_types": row.get("secondary_types") or [],
        "disambiguation": row.get("disambiguation") or "",
        "score": _int(row.get("score")),
        "link": f"https://musicbrainz.org/release-group/{mbid}" if mbid else None,
        "source": "musicbrainz",
    }


# Public alias — `server.discover` builds the Discover album rows from the same
# mapper, so a MusicBrainz release group has one shape everywhere in the app.
mb_album_row = _mb_album_row


def resolve_release_group(artist, album, cfg=None, timeout=None):
    """Best MusicBrainz release-group for an artist+album pair.

    This is the bridge from a discovery row (Deezer/iTunes) to something the
    app can actually add and download. Cached by integrations for 30 min;
    tries the strict fielded query first and relaxes to free text after.
    """
    artist = (artist or "").strip()
    album = (album or "").strip()
    if not album:
        return None
    queries = []
    if artist:
        queries.append(f'artist:"{artist}" AND releasegroup:"{album}"')
    queries.append(album)
    for query in queries:
        try:
            result = integrations.search_mb("release-group", query, limit=5)
        except Exception:
            continue
        rows = [_mb_album_row(r) for r in (result or {}).get("rows") or []]
        if not rows:
            continue
        if artist:
            named = [r for r in rows if _norm(artist) in _norm(r["artist"])
                     or _norm(r["artist"]) in _norm(artist)]
            rows = named or rows
        exact = [r for r in rows if _norm(r["title"]) == _norm(album)]
        best = (exact or rows)[0]
        best["resolved_from"] = {"artist": artist, "album": album}
        return best
    return None


def resolve_artist_mbid(name, cfg=None, timeout=None):
    """MusicBrainz artist MBID for a name (used for description/image chains)."""
    if not name:
        return None
    try:
        result = integrations.search_mb("artist", f'artist:"{name}"', limit=3)
    except Exception:
        return None
    rows = (result or {}).get("rows") or []
    if not rows:
        return None
    exact = [r for r in rows if _norm(r.get("title")) == _norm(name)]
    return (exact or rows)[0].get("id")


def album_genres(artist, album, dz_id=None, cfg=None):
    """Genre names for an album from the discovery providers (Deezer first:
    its album detail carries a real genre list, iTunes carries one primary
    genre). Used to fill a missing GENRE tag."""
    genres = []
    detail = deezer_album(dz_id) if dz_id else None
    if not detail:
        hits = deezer_search_album(f'artist:"{artist}" album:"{album}"', limit=3) if artist else []
        match = next((h for h in hits if _norm(h["title"]) == _norm(album)), None) \
            or (hits[0] if hits else None)
        detail = deezer_album(match["deezer_id"]) if match else None
    if detail:
        genres.extend(detail.get("genres") or [])
    if not genres:
        for row in itunes_search_album(artist, album, limit=1):
            if row.get("genre"):
                genres.append(row["genre"])
    out = []
    for genre in genres:
        name = str(genre).strip()
        if name and name.lower() not in {g.lower() for g in out}:
            out.append(name)
    return out


# --------------------------------------------------------------------------- #
# Discover: genre lists, genre browse and recommendation feeds
# --------------------------------------------------------------------------- #
# What the three /api/discover/* endpoints ask a provider for. Every wrapper
# goes through one of the two seams above — `_json` (TTL cache, single-flight,
# per-host throttle) or `integrations.mb_get_cached` (MusicBrainz's own 1 req/s
# etiquette) — so Discover inherits the app's caching and politeness instead of
# opening a second, unthrottled client against the same APIs.
#
# WHICH source can answer WHICH kind is declared once, in `server.discover`'s
# registry; this module only talks to the APIs, and each browse wrapper returns
# `{"rows": [...], "total": n|None}` where `total` is the provider's own match
# count when it states one (MusicBrainz's `count`, Discogs' `pagination.items`,
# Last.fm's `@attr.total`) and None when it does not (`itunes`, Deezer charts).
LISTENBRAINZ_LABS = "https://labs.api.listenbrainz.org"
# Labs requires a named algorithm; this is its default session-based one, which
# answers with a `score` per artist (how often listeners move between the two).
LB_SIMILAR_ALGORITHM = ("session_based_days_9000_session_300_contribution_5"
                        "_threshold_15_limit_50_skip_30")

# MusicBrainz's genre taxonomy: `/genre/all` pages 100 names at a time, in name
# order, and states the total (`genre-count`: 2 202 when this was written).
MB_GENRE_PAGE = 100
# The taxonomy's page count at the size MusicBrainz states (2 202 names); the
# page ORDER below spreads over this, and a shorter taxonomy simply skips the
# indices it does not have.
_MB_GENRE_SPREAD = 24
_MB_GENRE_ROWS: dict = {}
_MB_GENRE_STATE: dict = {}
_MB_GENRE_PAGES: list = []
_MB_GENRE_LOCK = threading.Lock()


def _mb_page_order():
    """The taxonomy's page indices to fetch, midpoints first — computed once.

    `/genre/all` is ALPHABETICAL, so fetching it front to back opens a partly
    loaded list on "2 tone", "aak", "abhang": two hundred of MusicBrainz's
    2 202 names, not one of them recognisable. Taking the middle page of every
    range first (12, then 6 and 18, then 3, 9, 15, 21, …) samples the whole
    alphabet instead, so even a list that is 10% loaded is worth reading. The
    order is fixed for `_MB_GENRE_SPREAD` pages — the taxonomy's own length —
    so it does not shift under a fetch that is already in flight."""
    if not _MB_GENRE_PAGES:
        order, pending = [], [(0, _MB_GENRE_SPREAD)]
        while pending:
            following = []
            for low, high in pending:
                if low >= high:
                    continue
                mid = (low + high) // 2
                order.append(mid)
                following.append((low, mid))
                following.append((mid + 1, high))
            pending = following
        _MB_GENRE_PAGES[:] = order
    return _MB_GENRE_PAGES


def _mb_genre_page(offset):
    """One `/genre/all` page, or None when MusicBrainz did not answer."""
    try:
        data = integrations.mb_get_cached("genre/all",
                                          {"limit": MB_GENRE_PAGE, "offset": offset,
                                           "fmt": "json"})
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    rows = [{"name": g.get("name"), "id": g.get("id")}
            for g in data.get("genres") or [] if g.get("name")]
    return {"rows": rows, "count": _int(data.get("genre-count"))}


def musicbrainz_genre_list(pages=2):
    """MusicBrainz's genre taxonomy, a couple of pages at a time.

    The whole list is 23 requests at MusicBrainz's 1 req/s — minutes of waiting
    a page must not do — so the pages are fetched a few at a time and kept:
    each call costs at most *pages* requests until the taxonomy is complete,
    and a repeat call is answered from memory. The pages come in `_mb_page_order`
    (spread over the alphabet, not front to back). The answer says how much of
    the taxonomy it holds (`loaded`/`done`), so a half-fetched list is never
    presented as the whole one."""
    with _MB_GENRE_LOCK:
        order = _mb_page_order()
        for _ in range(max(0, int(pages or 0))):
            count = _MB_GENRE_STATE.get("count")
            asked = _MB_GENRE_STATE.get("pages", 0)
            # A shorter taxonomy than this module assumed has fewer pages: the
            # indices past its end are stepped over, not requested.
            size = -(-count // MB_GENRE_PAGE) if count else len(order)
            while asked < len(order) and order[asked] >= size:
                asked += 1
            if asked >= len(order):
                break
            page = _mb_genre_page(order[asked] * MB_GENRE_PAGE)
            _MB_GENRE_STATE["pages"] = asked + 1
            if page is None:
                break       # MusicBrainz is not answering: keep what we hold.
            for row in page["rows"]:
                _MB_GENRE_ROWS.setdefault(row["name"], row)
            if page["count"]:
                _MB_GENRE_STATE["count"] = page["count"]
            if not page["rows"]:
                break
        rows = sorted(_MB_GENRE_ROWS.values(), key=lambda r: str(r["name"]).lower())
        count = _MB_GENRE_STATE.get("count")
        asked = _MB_GENRE_STATE.get("pages", 0)
        return {"genres": rows, "count": count, "loaded": len(rows),
                "done": bool(count) and asked >= -(-count // MB_GENRE_PAGE)}


# MusicBrainz's own page ceiling for a search request (`SEARCH_LIMIT_MAX`);
# named here so this module does not have to import integrations' constant.
SEARCH_LIMIT = 100


def _mb_recording_row(row):
    """MusicBrainz recording search row → the shared discovery row shape.

    The cover comes from the RELEASE the recording was pressed on: Cover Art
    Archive answers by release and release group and has no recording endpoint
    at all, so `search_mb` keeps the first release's identity for exactly this.
    Without it a MusicBrainz track row rendered as the placeholder icon while
    the album row beside it showed its cover."""
    mbid = row.get("id")
    group = str(row.get("release_group_mbid") or "").strip()
    release = str(row.get("release_mbid") or "").strip()
    return {
        "kind": "track",
        "title": row.get("title") or "",
        "artist": row.get("artist") or "",
        "mbid": mbid,
        # The release group first (the app's own album identity, and what
        # `_mb_album_row` states); the release is the fallback for a recording
        # whose group has no front cover of its own.
        "cover": (_caa_front("release-group", group)
                  or _caa_front("release", release)),
        "release_group_mbid": group or None,
        "year": (row.get("first_release_date") or "")[:4],
        "duration": _int(row.get("length")),
        "score": _int(row.get("score")),
        "link": f"https://musicbrainz.org/recording/{mbid}" if mbid else None,
        "source": "musicbrainz",
    }


def musicbrainz_tag_search(kind, genre, limit=25, offset=0):
    """MusicBrainz's tag (= genre) search for ONE entity kind.

    WS/2 has no genre *browse* filter — a `genre=<mbid>` parameter is rejected
    on release groups, artists and recordings alike (verified) — so a genre
    list from MusicBrainz is the Lucene `tag:` field, which the search index
    carries for all three. `kind` is the caller's own vocabulary
    (albums/artists/tracks) and MusicBrainz's relevance `score` is kept, which
    is what orders the rows.

    Raises on a MusicBrainz failure (`MusicBrainzError`): the caller reports it
    in `notes` rather than letting an empty list look like "nothing matched"."""
    entity = {"albums": "release-group", "artists": "artist",
              "tracks": "recording"}.get(str(kind or "").strip().lower())
    term = str(genre or "").replace("\\", " ").replace('"', " ").strip()
    if not entity or not term:
        return {"rows": [], "total": None}
    result = integrations.search_mb(entity, f'tag:"{term}"',
                                    limit=max(1, min(SEARCH_LIMIT, int(limit))),
                                    offset=max(0, int(offset or 0)))
    rows = []
    for row in (result or {}).get("rows") or []:
        mbid = row.get("id")
        if entity == "release-group":
            rows.append(_mb_album_row(row))
        elif entity == "artist":
            rows.append({
                "kind": "artist",
                "title": row.get("title") or "",
                "artist": row.get("title") or "",
                "mbid": mbid,
                "country": row.get("country") or "",
                "disambiguation": row.get("disambiguation") or "",
                "tags": row.get("tags") or [],
                "score": _int(row.get("score")),
                "link": f"https://musicbrainz.org/artist/{mbid}" if mbid else None,
                "source": "musicbrainz",
            })
        else:
            rows.append(_mb_recording_row(row))
    return {"rows": rows, "total": _int((result or {}).get("total"))}


def musicbrainz_artist_recordings(artist_mbid, limit=25):
    """An artist's recordings, from MusicBrainz's own index (`arid:<id>`).

    The "more from this artist" browse for a TRACK shelf. A release-group
    browse cannot answer it — a release group is not a track — so the same
    question goes to the recording index, which filters by artist id on
    MusicBrainz's side (`search_mb`'s `artist_id` becomes the `arid:` clause).
    One request, no paging: this is one labelled row beside the artist's own
    records, not a crawl of a discography."""
    mbid = str(artist_mbid or "").strip().lower()
    if not mbid:
        return {"rows": [], "total": None}
    result = integrations.search_mb("recording", "",
                                    limit=max(1, min(SEARCH_LIMIT, int(limit))),
                                    offset=0, artist_id=mbid)
    rows = [_mb_recording_row(row) for row in (result or {}).get("rows") or []]
    return {"rows": rows, "total": _int((result or {}).get("total"))}


def deezer_genre_list(timeout=None):
    """Deezer's own genre list — 22 broad genres, id 0 ("All") dropped."""
    data = _json(f"{DEEZER_BASE}/genre", timeout=timeout)
    rows = []
    for item in (data or {}).get("data") or []:
        gid, name = _int(item.get("id")), (item.get("name") or "").strip()
        if not gid or not name:
            continue
        rows.append({"id": gid, "name": name,
                     "picture": item.get("picture_xl") or item.get("picture_big")})
    return rows


def deezer_genre_id(name, timeout=None):
    """Deezer's genre id for *name*, or None when Deezer has no such genre.

    Deezer files music under 22 broad genres ("Rock", "Rap/Hip Hop"), so a
    specific genre like "shoegaze" is NOT one of its genres and this returns
    None — the caller reports that instead of asking Deezer's text search and
    passing its answer off as a genre."""
    want = _norm(name)
    if not want:
        return None
    for row in deezer_genre_list(timeout=timeout):
        if _norm(row["name"]) == want:
            return row["id"]
    return None


def deezer_genre_browse(genre_id, kind="albums", limit=25, offset=0, timeout=None):
    """One page of a Deezer genre chart (albums/tracks) or its artist list.

    `chart/<id>/albums`, `chart/<id>/tracks` and `genre/<id>/artists`, all
    paged with Deezer's `index` (verified). A chart's own `total` is the page
    size rather than a corpus count, so no total is reported — a full page is
    what "more may follow" means there."""
    gid = _int(genre_id)
    kind = str(kind or "").strip().lower()
    path = {"albums": f"/chart/{gid}/albums", "tracks": f"/chart/{gid}/tracks",
            "artists": f"/genre/{gid}/artists"}.get(kind)
    if not gid or not path:
        return {"rows": [], "total": None}
    data = _json(f"{DEEZER_BASE}{path}",
                 {"limit": max(1, min(100, int(limit))), "index": max(0, int(offset or 0))},
                 timeout=timeout)
    rows = []
    for item in (data or {}).get("data") or []:
        if kind == "albums":
            rows.append({
                "kind": "album",
                "title": item.get("title") or "",
                "artist": (item.get("artist") or {}).get("name") or "",
                "deezer_id": _int(item.get("id")),
                "cover": item.get("cover_xl") or item.get("cover_big"),
                "year": "",
                "record_type": (item.get("record_type") or "").lower(),
                "link": item.get("link"),
                "source": "deezer",
            })
        elif kind == "artists":
            rows.append({
                "kind": "artist",
                "title": item.get("name") or "",
                "artist": item.get("name") or "",
                "deezer_id": _int(item.get("id")),
                "cover": item.get("picture_xl") or item.get("picture_big"),
                "popularity": _int(item.get("nb_fan")),
                "link": item.get("link"),
                "source": "deezer",
            })
        else:
            album = item.get("album") or {}
            rows.append({
                "kind": "track",
                "title": item.get("title") or "",
                "artist": (item.get("artist") or {}).get("name") or "",
                "album": album.get("title") or "",
                "deezer_id": _int(item.get("id")),
                "cover": album.get("cover_xl") or album.get("cover_big"),
                "duration": _int(item.get("duration")),
                "popularity": _int(item.get("rank")),
                "link": item.get("link"),
                "source": "deezer",
            })
    return {"rows": rows, "total": None}


def itunes_genre_albums(genre, limit=25, offset=0, cfg=None, timeout=None):
    """Apple's genreIndex album search — the one Apple filter that IS a genre.

    Apple ignores `offset` (verified: the same page comes back), so a deeper
    page is served by asking for `offset + limit` rows and returning this
    offset's slice, up to Apple's own 200-row ceiling. `primaryGenreName` is
    Apple's coarse genre ("Alternative" for a shoegaze record), never passed
    off as the caller's genre."""
    term = str(genre or "").strip()
    if not term:
        return {"rows": [], "total": None}
    offset = max(0, int(offset or 0))
    limit = max(1, min(200, int(limit)))
    want = max(1, min(200, offset + limit))
    data = _json(f"{ITUNES_BASE}/search",
                 {"term": term, "entity": "album", "attribute": "genreIndex",
                  "limit": want, "country": integrations._apple_country(cfg)},
                 timeout=timeout, host="itunes.apple.com")
    rows = []
    for item in (data or {}).get("results") or []:
        art = item.get("artworkUrl100") or item.get("artworkUrl60")
        rows.append({
            "kind": "album",
            "title": item.get("collectionName") or "",
            "artist": item.get("artistName") or "",
            "itunes_id": _int(item.get("collectionId")),
            "cover": itunes_artwork(art, 600) if art else None,
            "year": (item.get("releaseDate") or "")[:4],
            "track_count": _int(item.get("trackCount")),
            "genre": item.get("primaryGenreName") or "",
            "link": item.get("collectionViewUrl"),
            "source": "itunes",
        })
    seen = offset + len(rows)
    return {"rows": rows[offset:][:limit], "total": seen}


# Apple's most-played feed (the marketing RSS, not the Search API): the one
# Apple route that IS a chart. It is a ROLLING window refreshed daily — Apple
# publishes no dated chart and no earlier one — so it answers `all` and the
# registry says why the other periods are not offered here.
ITUNES_CHART_BASE = "https://rss.applemarketingtools.com/api/v2"
_ITUNES_CHART_HOST = "rss.applemarketingtools.com"


def itunes_most_played(limit=50, cfg=None, timeout=None):
    """Apple Music's most-played songs for one storefront, as a chart.

    Rows keep Apple's own order (their `rank`), use the 600px artwork the rest
    of this module rewrites to, and carry no popularity number: Apple states
    none on this feed, and inventing one would be worse than the honest null.
    Raises `ProviderRefused` when the feed does not answer."""
    country = integrations._apple_country(cfg)
    want = max(1, min(200, int(limit)))
    started = time.time()
    data = _json(f"{ITUNES_CHART_BASE}/{country}/music/most-played/{want}/songs.json",
                 None, timeout=timeout, ttl=TTL_CHART, host=_ITUNES_CHART_HOST)
    if data is None:
        raise ProviderRefused(_refusal(_ITUNES_CHART_HOST, started)
                              or "Apple did not answer its most-played feed")
    rows = []
    for item in ((data or {}).get("feed") or {}).get("results") or []:
        art = item.get("artworkUrl100")
        rows.append({
            "kind": "track",
            "itunes_id": _int(item.get("id")),
            "title": item.get("name") or "",
            "artist": item.get("artistName") or "",
            "cover": itunes_artwork(art, 600) if art else None,
            "year": (item.get("releaseDate") or "")[:4],
            "genres": [g.get("name") for g in item.get("genres") or []
                       if isinstance(g, dict) and g.get("name")],
            "popularity": None,
            "popularity_label": None,
            "link": item.get("url"),
            "source": "itunes",
        })
    return _rank_rows(rows)


def discogs_style_search(style, limit=25, offset=0, cfg=None, timeout=None):
    """Discogs release browse by style ("Shoegaze") through `database/search`.

    Anonymous search answers this endpoint (verified) — a configured
    `discogs_token` is sent when there is one and only raises Discogs' rate
    limit, so this source is keyless here. Discogs rows name an artist and a
    title in ONE string ("Ride - This Is Not A Safe Place"), which is split at
    its first " - "; the raw split is what makes a dotted artist name safe."""
    text = str(style or "").strip()
    if not text:
        return {"rows": [], "total": None}
    offset = max(0, int(offset or 0))
    limit = max(1, min(100, int(limit)))
    # `database/search` pages 100 at a time; the row this offset starts at is
    # inside that page (`start`), so one request answers a 100-row window.
    page = offset // 100 + 1
    start = offset % 100
    params = {"style": text, "type": "release", "per_page": 100, "page": page}
    token = str((cfg or {}).get("discogs_token") or "").strip()
    if token:
        params["token"] = token
    data = _json(f"{DISCOGS_BASE}/database/search", params, timeout=timeout,
                 host="api.discogs.com")
    rows = []
    for item in (data or {}).get("results") or []:
        artist, _, title = str(item.get("title") or "").partition(" - ")
        rows.append({
            "kind": "album",
            "title": title.strip(),
            "artist": artist.strip(),
            "discogs_id": _int(item.get("id")),
            "cover": item.get("cover_image") or item.get("thumb"),
            "year": str(item.get("year") or ""),
            "genres": item.get("genre") or [],
            "styles": item.get("style") or [],
            "link": (f"https://www.discogs.com/release/{item.get('id')}"
                     if item.get("id") else None),
            "source": "discogs",
        })
    total = ((data or {}).get("pagination") or {}).get("items")
    return {"rows": rows[start:][:limit], "total": _int(total)}


def _lastfm_image(item):
    """A Last.fm row's largest image URL (its image list is size-keyed)."""
    sized = {}
    for image in item.get("image") or []:
        if isinstance(image, dict) and image.get("size"):
            sized[image["size"]] = image.get("#text")
    for size in ("mega", "extralarge", "large", "medium", "small"):
        if sized.get(size):
            return sized[size]
    return None


def lastfm_top_tags(limit=100, cfg=None, timeout=None):
    """Last.fm's most-used tags sitewide (`chart.gettoptags`), as names.

    Last.fm's tag vocabulary is its genre vocabulary — the same tags
    `tag.gettopalbums` browses by — so the chart is what makes Last.fm a genre
    LIST source, and it needs `lastfm_api_key` like every other call here."""
    data = _lastfm_json("chart.gettoptags", {"limit": max(1, min(500, int(limit)))},
                        cfg, timeout)
    tags = ((data or {}).get("tags") or {}).get("tag") or []
    if isinstance(tags, dict):
        tags = [tags]
    return [t.get("name") for t in tags if isinstance(t, dict) and t.get("name")]


def lastfm_tag_top(kind, tag, limit=25, offset=0, cfg=None, timeout=None):
    """Last.fm's most-listened albums/artists/tracks under ONE tag (genre).

    `tag.gettopalbums` / `tag.gettopartists` / `tag.gettoptracks`: Last.fm's
    own ranking of the tag, which is what makes this a genre browse rather
    than a text search. Needs `lastfm_api_key`; [] without one."""
    kind = str(kind or "").strip().lower()
    method = {"albums": "tag.gettopalbums", "artists": "tag.gettopartists",
              "tracks": "tag.gettoptracks"}.get(kind)
    singular = {"albums": "album", "artists": "artist",
                "tracks": "track"}.get(kind)
    text = str(tag or "").strip()
    if not method or not text:
        return {"rows": [], "total": None}
    per_page = max(1, min(100, int(limit)))
    page = max(0, int(offset or 0)) // per_page + 1
    data = _lastfm_json(method, {"tag": text, "limit": per_page, "page": page},
                        cfg, timeout)
    block = (data or {}).get(kind) or {}
    items = block.get(singular) or []
    if isinstance(items, dict):
        items = [items]
    rows = []
    for item in items:
        artist = (item.get("artist") or {}).get("name") or ""
        rows.append({
            "kind": {"albums": "album", "artists": "artist",
                     "tracks": "track"}[kind],
            "title": item.get("name") or "",
            "artist": artist or (item.get("name") or ""),
            "cover": _lastfm_image(item),
            "duration": _int(item.get("duration")),
            "link": item.get("url"),
            "source": "lastfm",
        })
    return {"rows": rows,
            "total": _int((block.get("@attr") or {}).get("total"))}


def lastfm_similar_artists(artist, limit=25, cfg=None, timeout=None):
    """Last.fm's similar artists (`artist.getsimilar`) — the seed for a
    library-seeded recommendation. `match` is Last.fm's own 0-1 similarity."""
    name = str(artist or "").strip()
    if not name:
        return []
    data = _lastfm_json("artist.getsimilar",
                        {"artist": name, "limit": max(1, min(100, int(limit)))},
                        cfg, timeout)
    items = ((data or {}).get("similarartists") or {}).get("artist") or []
    if isinstance(items, dict):
        items = [items]
    rows = []
    for item in items:
        got = item.get("name") or ""
        if not got or _norm(got) == _norm(name):
            continue
        rows.append({
            "kind": "artist",
            "title": got,
            "artist": got,
            "cover": _lastfm_image(item),
            "match": item.get("match"),
            "link": item.get("url"),
            "source": "lastfm",
        })
    return rows


def lastfm_similar_tracks(artist, track, limit=25, cfg=None, timeout=None):
    """Last.fm's similar tracks (`track.getsimilar`), for a track seed."""
    name, title = str(artist or "").strip(), str(track or "").strip()
    if not title:
        return []
    data = _lastfm_json("track.getsimilar",
                        {"artist": name, "track": title,
                         "limit": max(1, min(100, int(limit)))}, cfg, timeout)
    items = ((data or {}).get("similartracks") or {}).get("track") or []
    if isinstance(items, dict):
        items = [items]
    rows = []
    for item in items:
        rows.append({
            "kind": "track",
            "title": item.get("name") or "",
            "artist": (item.get("artist") or {}).get("name") or "",
            "duration": _int(item.get("duration")),
            "match": item.get("match"),
            "link": item.get("url"),
            "source": "lastfm",
        })
    return rows


# Last.fm's sitewide charts — `chart.gettoptracks` / `chart.gettopartists` /
# `chart.gettopalbums` — which are keyed and ALL-TIME: Last.fm publishes no
# dated or per-period sitewide chart, so `all` is the only period the registry
# offers for it.
_LASTFM_CHART_METHOD = {"albums": "chart.gettopalbums",
                        "artists": "chart.gettopartists",
                        "tracks": "chart.gettoptracks"}
_LASTFM_CHART_BLOCK = {"albums": ("albums", "album"),
                       "artists": ("artists", "artist"),
                       "tracks": ("tracks", "track")}


def lastfm_chart(kind, limit=50, cfg=None, timeout=None):
    """Last.fm's most-scrobbled rows sitewide, with its own `playcount`.

    Rows keep Last.fm's order (their `rank`) and state `popularity` as the
    playcount it is, labelled with the same humanised form the rest of this
    module uses. Raises `ProviderRefused` — carrying Last.fm's own words — when
    the key is rejected or the service does not answer; [] when the chart is
    simply empty."""
    kind = str(kind or "").strip().lower()
    method = _LASTFM_CHART_METHOD.get(kind)
    if not method:
        raise ProviderRefused("Last.fm has no %s chart" % (kind or "such"))
    block_key, row_key = _LASTFM_CHART_BLOCK[kind]
    started = time.time()
    data = _lastfm_json(method, {"limit": max(1, min(1000, int(limit)))}, cfg,
                        timeout=timeout, ttl=TTL_CHART)
    if data is None:
        raise ProviderRefused(lastfm_last_error(started) or
                              "Last.fm did not answer %s" % method)
    items = ((data or {}).get(block_key) or {}).get(row_key) or []
    if isinstance(items, dict):
        items = [items]
    rows = []
    for item in items:
        plays = _int(item.get("playcount")) or 0
        artist = (item.get("artist") or {}).get("name") or ""
        rows.append({
            "kind": {"albums": "album", "artists": "artist",
                     "tracks": "track"}[kind],
            "title": item.get("name") or "",
            "artist": artist or (item.get("name") or ""),
            "mbid": item.get("mbid") or ((item.get("artist") or {}).get("mbid")
                                         if kind == "track" else None),
            "cover": _lastfm_image(item),
            "duration": _int(item.get("duration")),
            "popularity": plays,
            "popularity_label": _listens_label(plays),
            "link": item.get("url"),
            "source": "lastfm",
        })
    return _rank_rows(rows)


def listenbrainz_similar_artists(mbid, limit=25, timeout=None):
    """ListenBrainz Labs' similar artists for one MBID — keyless.

    The response is an array of artists with a `score` (how often listeners
    move between the two), which is a real recommendation signal and needs no
    account. LB Radio itself (the genre radio) DOES demand a user token
    (verified: 401 without one), so it is not wired."""
    ident = str(mbid or "").strip()
    if not ident:
        return []
    data = _json(f"{LISTENBRAINZ_LABS}/similar-artists/json",
                 {"artist_mbids": ident, "algorithm": LB_SIMILAR_ALGORITHM,
                  "limit": max(1, min(100, int(limit)))},
                 timeout=timeout, ttl=TTL_CHART,
                 host="labs.api.listenbrainz.org")
    rows = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        name, got = item.get("name") or "", str(item.get("artist_mbid") or "")
        if not name or got == ident:
            continue        # the seed artist is not a similar artist
        rows.append({
            "kind": "artist",
            "title": name,
            "artist": name,
            "mbid": got or None,
            "popularity": _int(item.get("score")),
            "disambiguation": item.get("comment") or "",
            "link": f"https://musicbrainz.org/artist/{got}" if got else None,
            "source": "listenbrainz",
        })
    return rows


def spotify_genre_albums(genre, limit=25, offset=0, cfg=None, timeout=None):
    """Spotify's album search filtered by ITS OWN genre names (`genre:<name>`).

    Empty without `spotify_client_id`/`spotify_client_secret` (the search needs
    a bearer token) — the source is skipped entirely, never defaulted."""
    text = str(genre or "").strip()
    token = integrations._spotify_token(cfg, timeout=timeout)
    if not text or not token:
        return {"rows": [], "total": None}
    data = _json(f"{SPOTIFY_API}/search",
                 {"q": f"genre:{text}", "type": "album",
                  "limit": max(1, min(50, int(limit))),
                  "offset": max(0, int(offset or 0))},
                 headers={"Authorization": f"Bearer {token}"},
                 timeout=timeout, host="api.spotify.com")
    block = ((data or {}).get("albums") or {})
    rows = []
    for item in block.get("items") or []:
        images = item.get("images") or []
        artists = item.get("artists") or []
        rows.append({
            "kind": "album",
            "title": item.get("name") or "",
            "artist": ((artists[0] or {}).get("name") if artists else "") or "",
            "spotify_id": item.get("id"),
            "cover": (images[0] or {}).get("url") if images else None,
            "year": (item.get("release_date") or "")[:4],
            "track_count": _int(item.get("total_tracks")),
            "link": (item.get("external_urls") or {}).get("spotify"),
            "source": "spotify",
        })
    return {"rows": rows, "total": _int(block.get("total"))}
