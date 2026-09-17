"""Discovery — the external music APIs behind recommendations, catalogue
search, "more like this", artist artwork and artist/album descriptions.

Every provider here is keyless, and every one of them was verified against the
live service before being wired in:

* **deezer** — Deezer's public API. Catalogue search, related artists, artist
  albums/tracks, real popularity numbers (`nb_fan` per artist, `fans` per
  album, `rank` per track) and 1000px artist images. No key, no quota.
* **listenbrainz** — MetaBrainz' ListenBrainz sitewide statistics: what people
  are actually listening to this week/month/year. MBID-native rows (release,
  release group, artist) with Cover Art Archive ids, so results need no
  identity resolution at all. 1 req/s etiquette, like MusicBrainz.
* **itunes** — Apple's Search API. Catalogue fallback for search, and its
  artwork URLs can be rewritten to 3000px (`/100x100bb.jpg` → `/3000x3000bb.jpg`).
* **audiodb** — TheAudioDB. Artist biographies, album notes, artist thumbs /
  banners, plus MusicBrainz and Wikipedia ids to hop across.
* **wikipedia** — Wikipedia REST summaries. The description source of record
  for artists and albums; `musicbrainz` is the resolver that finds the article
  through the entity's Wikidata URL relation.

MusicBrainz stays the identity anchor for the whole app: anything that can be
wished for or downloaded resolves to a release-group MBID through
`resolve_release_group` (via the shared, rate-limited `integrations.search_mb`
cache), and MusicBrainz is the final fallback for every chain here — so a
discovery provider going dark degrades to "fewer, plainer results", never to
"no results".

Everything is TTL-cached in-process (`_CACHE`, 30 min for metadata, 15 min for
charts/recommendations) with per-host throttling, so repeat page views never
re-hit the network.
"""
import re
import threading
import time
from urllib.parse import quote

import httpx

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

# Provider ids, in the built-in order per feature. Every "sources" config key
# (discovery_rec_sources, discovery_search_sources, artist_image_sources,
# description_sources) is a subset of these, in the user's preferred order.
SOURCES = ["deezer", "listenbrainz", "itunes", "audiodb", "wikipedia", "musicbrainz"]
SOURCE_LABELS = {
    "deezer": "Deezer",
    "listenbrainz": "ListenBrainz",
    "itunes": "iTunes",
    "audiodb": "TheAudioDB",
    "wikipedia": "Wikipedia",
    "musicbrainz": "MusicBrainz",
}
SOURCE_NOTES = {
    "deezer": "Popularity-ranked catalogue, related artists and 1000px artist photos.",
    "listenbrainz": "What people are listening to right now (MetaBrainz, MBID-native).",
    "itunes": "Apple catalogue search and high-resolution artwork.",
    "audiodb": "Artist biographies, album notes and press photos.",
    "wikipedia": "Artist and album descriptions from Wikipedia summaries.",
    "musicbrainz": "The identity anchor: release-group MBIDs, used as the final fallback.",
}
REC_SOURCES = ["deezer", "listenbrainz", "musicbrainz"]
SEARCH_SOURCES = ["deezer", "itunes", "listenbrainz", "musicbrainz"]
IMAGE_SOURCES = ["deezer", "audiodb", "itunes", "wikipedia"]
DESCRIPTION_SOURCES = ["wikipedia", "audiodb", "musicbrainz"]

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


TTL_META = 1800.0        # artist/album metadata, images, descriptions, MBIDs
TTL_CHART = 900.0        # charts and recommendations (they move)

# Per-album memo for `genre_lookup`: script 8 asks per TRACK, and the genre
# chain (RYM, community sources, providers, MusicBrainz) must not run a dozen
# times for one album.
_GENRE_MEMO: dict = {}
_GENRE_MEMO_LOCK = threading.Lock()


def source_order(cfg, key, default):
    """Configured provider order for *key*, filtered to real providers.

    An empty/absent list means "the built-in order"; unknown ids are dropped
    so a hand-edited config can never wedge a feature."""
    if not cfg:
        return list(default)
    if not cfg.get("discovery_enabled", True):
        # Only a feature whose own provider list contains MusicBrainz can
        # fall back to it. Returning it blindly made the artist-image chain
        # walk a "musicbrainz" branch that does not exist and silently find
        # nothing instead of reporting "no candidate".
        return ["musicbrainz"] if "musicbrainz" in default else []
    raw = cfg.get(key) or []
    if isinstance(raw, str):
        raw = [t for t in raw.replace(";", ",").split(",") if t.strip()]
    chosen = [str(s).strip().lower() for s in raw if str(s).strip()]
    chosen = [s for s in chosen if s in SOURCES]
    return chosen or list(default)


def sources_catalog():
    """Providers for the settings UI, plus the built-in feature orders."""
    return {
        "sources": [
            {"id": sid, "label": SOURCE_LABELS[sid], "notes": SOURCE_NOTES[sid]}
            for sid in SOURCES
        ],
        "defaults": {
            "discovery_rec_sources": list(REC_SOURCES),
            "discovery_search_sources": list(SEARCH_SOURCES),
            "artist_image_sources": list(IMAGE_SOURCES),
            "description_sources": list(DESCRIPTION_SOURCES),
        },
    }


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
        else:
            data = resp.json()
    except Exception as e:
        data = None
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
    """Lowercase, punctuation-free comparison key."""
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


# Public alias — other modules (routes, recommendations) match titles/artists
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


# --------------------------------------------------------------------------- #
# ListenBrainz
# --------------------------------------------------------------------------- #
def _lb_stats(kind, range_="month", limit=25, offset=0, timeout=None):
    return _json(f"{LISTENBRAINZ_BASE}/stats/sitewide/{kind}",
                 {"range": range_, "count": limit, "offset": offset},
                 timeout=timeout, ttl=TTL_CHART)


def _caa_group_url(release_mbid):
    """Cover Art Archive front cover for a release (250px thumbnail)."""
    if not release_mbid:
        return None
    return f"https://coverartarchive.org/release/{release_mbid}/front-250"


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


def itunes_artist_artwork(name, limit=1, timeout=None):
    """Apple has no artist photo endpoint; the artist's best-known album art is
    the closest honest fallback and is only used when nothing else has an
    image."""
    data = _json(f"{ITUNES_BASE}/search",
                 {"term": name, "entity": "album", "limit": limit},
                 timeout=timeout)
    results = (data or {}).get("results") or []
    if not results:
        return None
    return itunes_artwork(results[0].get("artworkUrl100"))


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
                  "token": token, "per_page": 3}, timeout=timeout)
    results = (data or {}).get("results") or []
    if not results:
        return None
    rid = results[0].get("id")
    if not rid:
        return None
    return _json(f"{DISCOGS_BASE}/releases/{rid}", {"token": token},
                 timeout=timeout)


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


def _lastfm_tags(method, params, cfg=None, timeout=None):
    """`{method}` top tags for one entity; [] without a key or on any failure."""
    key = _lastfm_key(cfg)
    if not key:
        return []
    sent = dict(params, method=method, api_key=key, format="json", autocorrect=1)
    data = _json(LASTFM_BASE, sent, timeout=timeout,
                 host="ws.audioscrobbler.com")
    tags = (((data or {}).get("toptags") or {}).get("tag")) or []
    if isinstance(tags, dict):
        tags = [tags]
    return [t.get("name") for t in tags if isinstance(t, dict) and t.get("name")]


def lastfm_album_genres(artist, album, cfg=None, timeout=None):
    """Last.fm top tags for an album; [] without a configured API key."""
    key = _lastfm_key(cfg)
    if not key or not (artist or album):
        return []
    data = _json(LASTFM_BASE,
                 {"method": "album.getinfo", "artist": artist, "album": album,
                  "api_key": key, "format": "json", "autocorrect": 1},
                 timeout=timeout, host="ws.audioscrobbler.com")
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
# Spotify (artist genres — the chain's last resort)
# --------------------------------------------------------------------------- #
SPOTIFY_API = "https://api.spotify.com/v1"


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
    name = str(artist or "").strip()
    token = integrations._spotify_token(cfg, timeout=timeout)
    if not name or not token:
        return []
    data = _json(f"{SPOTIFY_API}/search",
                 {"q": name, "type": "artist", "limit": 1},
                 headers={"Authorization": f"Bearer {token}"},
                 timeout=timeout, host="api.spotify.com")
    want = _norm(name)
    for item in ((data or {}).get("artists") or {}).get("items") or []:
        if _norm(item.get("name")) != want:
            continue
        return [str(g).strip() for g in item.get("genres") or [] if str(g).strip()]
    return []


# --------------------------------------------------------------------------- #
# Wikipedia
# --------------------------------------------------------------------------- #
def wikipedia_search(query, limit=5, timeout=None):
    """Article titles matching a query (album articles are usually
    "Album (album)" — the search API knows)."""
    data = _json(f"{WIKIPEDIA_BASE}/w/api.php",
                 {"action": "query", "list": "search", "srsearch": query,
                  "format": "json", "srlimit": limit}, timeout=timeout)
    hits = ((data or {}).get("query") or {}).get("search") or []
    return [h.get("title") for h in hits if h.get("title")]


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
        "cover": (f"https://coverartarchive.org/release-group/{mbid}/front-250"
                  if mbid else None),
        "year": (row.get("first_release_date") or "")[:4],
        "record_type": (row.get("primary_type") or "").lower(),
        "secondary_types": row.get("secondary_types") or [],
        "disambiguation": row.get("disambiguation") or "",
        "score": _int(row.get("score")),
        "link": f"https://musicbrainz.org/release-group/{mbid}" if mbid else None,
        "source": "musicbrainz",
    }


def resolve_release_group(artist, album, cfg=None, timeout=None):
    """Best MusicBrainz release-group for an artist+album pair.

    This is the bridge from a discovery row (Deezer/iTunes) to something the
    app can actually wish for and download. Cached by integrations for 30 min;
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


# --------------------------------------------------------------------------- #
# Search chains (MusicBrainz browser + global search)
# --------------------------------------------------------------------------- #
def _dedupe(rows, keys=("title", "artist")):
    """De-duplicate rows, keeping the first (best-ranked) provider's row but
    folding in anything a later duplicate has that the first one lacks — most
    importantly the MusicBrainz release-group id, so a Deezer recommendation
    that MusicBrainz also knows is still wishable and downloadable."""
    seen: dict = {}
    out = []
    for row in rows:
        key = tuple(_norm(row.get(k)) for k in keys)
        if not any(key):
            continue
        index = seen.get(key)
        if index is None:
            seen[key] = len(out)
            out.append(row)
            continue
        target = out[index]
        for field in ("mbid", "cover", "year", "record_type", "score", "tracks"):
            if not target.get(field) and row.get(field):
                target[field] = row[field]
        if not target.get("link") and row.get("link"):
            target["link"] = row["link"]
    return out


def search_albums(query, limit=25, cfg=None, artist=None, album=None):
    """Album search across the configured discovery search sources.

    Every provider is tried in order and results are appended, not replaced,
    so a partial provider (or an offline one) still yields rows; MusicBrainz
    fills in behind them. Rows from non-MB providers carry no MBID — the UI
    resolves that only when the user acts on one.
    """
    artist = (artist or "").strip()
    album = (album or "").strip() or (query or "").strip()
    rows = []
    for source in source_order(cfg, "discovery_search_sources", SEARCH_SOURCES):
        if source == "deezer":
            artists = [artist] if artist else []
            if not artists and query:
                artists = [a["name"] for a in deezer_search_artist(query, limit=1)]
            for name in artists[:2]:
                rows.extend([r for r in deezer_search_album(f'artist:"{name}" album:"{album}"', limit=8)
                             if _norm(album) in _norm(r["title"]) or not album])
        elif source == "itunes":
            rows.extend(itunes_search_album(artist or query, album, limit=8))
        elif source == "listenbrainz":
            rows.extend([r for r in listenbrainz_top_releases("year", limit=100)
                         if album and _norm(album) in _norm(r["title"])])
        elif source == "musicbrainz":
            q = f'artist:"{artist}" AND releasegroup:"{album}"' if artist else album
            try:
                result = integrations.search_mb("release-group", q, limit=limit)
                rows.extend(_mb_album_row(r) for r in (result or {}).get("rows") or [])
            except Exception:
                pass
        if len(_dedupe(rows)) >= limit:
            break
    rows = _dedupe(rows)[:limit]
    rows.sort(key=lambda r: (r.get("source") != "deezer", -(r.get("popularity") or 0)))
    return rows


def search_artists(query, limit=25, cfg=None):
    """Artist search across the configured discovery search sources."""
    rows = []
    seen_names = set()
    for source in source_order(cfg, "discovery_search_sources", SEARCH_SOURCES):
        if source == "deezer":
            for row in deezer_search_artist(query, limit=10):
                rows.append({
                    "kind": "artist", "name": row["name"], "image": row["image"],
                    "popularity": row["popularity"],
                    "popularity_label": _fans_label(row["popularity"]),
                    "deezer_id": row["id"], "link": row["link"], "source": "deezer",
                })
        elif source == "itunes":
            for row in itunes_search_artist(query, limit=5):
                rows.append({
                    "kind": "artist", "name": row["name"], "popularity": None,
                    "itunes_id": row["itunes_id"], "link": row["link"],
                    "genre": row.get("genre"), "source": "itunes",
                })
        elif source == "listenbrainz":
            for row in listenbrainz_top_artists("year", limit=100):
                if query and _norm(query) in _norm(row["name"]):
                    rows.append({**row, "kind": "artist"})
        elif source == "musicbrainz":
            try:
                result = integrations.search_mb("artist", f'artist:"{query}"', limit=10)
                for r in (result or {}).get("rows") or []:
                    rows.append({
                        "kind": "artist", "name": r.get("title") or "",
                        "mbid": r.get("id"), "disambiguation": r.get("disambiguation"),
                        "country": r.get("country"), "type": r.get("type"),
                        "tags": r.get("tags") or [], "popularity": None,
                        "link": f"https://musicbrainz.org/artist/{r.get('id')}",
                        "source": "musicbrainz",
                    })
            except Exception:
                pass
    out = []
    for row in rows:
        key = _norm(row.get("name"))
        if not key or key in seen_names:
            continue
        seen_names.add(key)
        out.append(row)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# Recommendations
# --------------------------------------------------------------------------- #
def popular_albums(limit=12, cfg=None, range_="week"):
    """What the world is actually listening to (ListenBrainz sitewide)."""
    if not (cfg is None or cfg.get("discovery_enabled", True)):
        return []
    rows = listenbrainz_top_releases(range_, limit=limit * 2)
    rows = _dedupe(rows, keys=("title", "artist"))[:limit]
    for row in rows:
        row["reason"] = "Popular right now"
    return rows


def recommend_albums(seeds, limit=18, cfg=None, exclude=None, range_="month"):
    """Popularity-aware album suggestions built from the user's own taste.

    *seeds* is a list of dicts `{artist, mbid, weight, reason}` (top-collected
    artists and top genres resolved to artists). Each seed is expanded through
    Deezer's related-artist graph, the related artists' albums are ranked by
    Deezer's own fan counts, and the result is de-duplicated against the
    library (*exclude* = set of normalized "artist|title" keys). When Deezer
    yields nothing — offline, blocked, unknown artist — ListenBrainz's
    sitewide charts are filtered by the seed artist names, which are
    MBID-native, and MusicBrainz search is the last resort.
    """
    exclude = exclude or set()
    rows = []
    if cfg is None or cfg.get("discovery_enabled", True):
        for seed in seeds[:4]:
            name = (seed.get("artist") or "").strip()
            if not name:
                continue
            related = deezer_related_artists(name, limit=6)
            candidates = []
            for rel in related[:4]:
                for album in deezer_artist_albums(rel["id"], limit=10, albums_only=True):
                    album["reason"] = seed.get("reason") or f"Because you listen to {name}"
                    album["seed"] = name
                    candidates.append(album)
            candidates.sort(key=lambda r: -(r.get("popularity") or 0))
            rows.extend(candidates[: max(3, limit // 2)])
        if not rows:
            names = {_norm(s.get("artist")) for s in seeds if s.get("artist")}
            chart = listenbrainz_top_releases(range_, limit=100)
            for row in chart:
                if _norm(row.get("artist")) in names:
                    row["reason"] = f"Popular from {row.get('artist')}"
                    rows.append(row)
    out = []
    for row in _dedupe(rows, keys=("title", "artist")):
        if f"{_norm(row.get('artist'))}|{_norm(row.get('title'))}" in exclude:
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


def similar_artists(name, mbid=None, limit=12, cfg=None):
    """Artists similar to *name*, walking the configured sources.

    Deezer's related graph is the primary signal; MusicBrainz's tag search
    ("other artists with these genres") is the fallback when Deezer has no
    relation data for the artist, so the section is never simply empty.
    """
    rows = []
    for source in source_order(cfg, "discovery_rec_sources", REC_SOURCES):
        if source == "deezer":
            for row in deezer_related_artists(name, limit=limit):
                rows.append({**row, "kind": "artist"})
        elif source == "listenbrainz":
            continue  # no per-artist similarity endpoint on the sitewide API
        elif source == "musicbrainz":
            artist_mbid = mbid or resolve_artist_mbid(name)
            if not artist_mbid:
                continue
            try:
                genres = integrations.artist_genres(artist_mbid)
            except Exception:
                genres = []
            for genre in genres[:2]:
                try:
                    result = integrations.search_mb(
                        "artist", f'tag:"{genre}" AND NOT artist:"{name}"', limit=6)
                except Exception:
                    continue
                for r in (result or {}).get("rows") or []:
                    rows.append({
                        "kind": "artist", "name": r.get("title") or "",
                        "mbid": r.get("id"), "tags": r.get("tags") or [],
                        "country": r.get("country"), "popularity": None,
                        "reason": f"Also tagged {genre}", "source": "musicbrainz",
                    })
        if len(_dedupe(rows, keys=("name",))) >= limit:
            break
    out = [r for r in _dedupe(rows, keys=("name",))
           if _norm(r.get("name")) != _norm(name)]
    return out[:limit]


def similar_albums(artist, album=None, mbid=None, limit=12, cfg=None, exclude=None):
    """"More like this" albums: popular albums by artists similar to *artist*,
    ranked by the provider's own popularity numbers. *exclude* drops rows the
    library already owns."""
    exclude = exclude or set()
    rows = []
    for rel in similar_artists(artist, mbid=mbid, limit=8, cfg=cfg):
        if not rel.get("id"):
            continue
        for row in deezer_artist_albums(rel["id"], limit=8, albums_only=True):
            row["reason"] = f"Like {rel['name']}"
            row["similar_to"] = rel["name"]
            rows.append(row)
    if not rows and (cfg is None or cfg.get("discovery_enabled", True)):
        # No Deezer graph (offline / unknown artist): fall back to the popular
        # charts filtered by genre-ish artist matches.
        for row in listenbrainz_top_releases("month", limit=60):
            if album and _norm(album) in _norm(row.get("title")):
                continue
            row["reason"] = "Popular right now"
            rows.append(row)
    out = []
    for row in sorted(_dedupe(rows, keys=("title", "artist")),
                      key=lambda r: -(r.get("popularity") or 0)):
        if album and _norm(row.get("title")) == _norm(album) and _norm(row.get("artist")) == _norm(artist):
            continue
        if f"{_norm(row.get('artist'))}|{_norm(row.get('title'))}" in exclude:
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


def similar_tracks(artist, title=None, limit=12, cfg=None, exclude=None):
    """"More like this" tracks: the popular tracks of similar artists."""
    exclude = exclude or set()
    rows = []
    for rel in similar_artists(artist, limit=8, cfg=cfg):
        if not rel.get("id"):
            continue
        for row in deezer_artist_top(rel["id"], limit=6):
            row["reason"] = f"Like {rel['name']}"
            row["similar_to"] = rel["name"]
            rows.append(row)
    out = []
    for row in sorted(_dedupe(rows, keys=("title", "artist")),
                      key=lambda r: -(r.get("popularity") or 0)):
        if title and _norm(row.get("title")) == _norm(title) and _norm(row.get("artist")) == _norm(artist):
            continue
        if f"{_norm(row.get('artist'))}|{_norm(row.get('title'))}" in exclude:
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# Artist / album artwork and text
# --------------------------------------------------------------------------- #
def artist_image(name, mbid=None, cfg=None):
    """Best available artist photo, walking the configured image sources.

    Returns `{url, source, label, kind}` — the caller downloads and stores it
    (see `mlo.artistdata.save_image`). Deezer's 1000px photo is the primary;
    TheAudioDB adds press photos and banners; Apple's best-known album art is
    the last resort because it is not a photo of the artist.
    """
    for source in source_order(cfg, "artist_image_sources", IMAGE_SOURCES):
        if source == "deezer":
            row = deezer_artist(name)
            if row and row.get("image"):
                return {"url": row["image"], "source": "deezer",
                        "label": "Deezer artist photo", "kind": "photo"}
        elif source == "audiodb":
            row = audiodb_artist(name)
            if row:
                for key, label in (("thumb", "TheAudioDB artist thumb"),
                                   ("banner", "TheAudioDB artist banner"),
                                   ("wide_thumb", "TheAudioDB wide thumb"),
                                   ("fanart", "TheAudioDB fanart")):
                    if row.get(key):
                        return {"url": row[key], "source": "audiodb",
                                "label": label, "kind": "photo" if key == "thumb" else "wide"}
        elif source == "itunes":
            url = itunes_artist_artwork(name)
            if url:
                return {"url": url, "source": "itunes",
                        "label": "Apple album artwork (artist has no photo API)",
                        "kind": "album_art"}
        elif source == "wikipedia":
            summary = wikipedia_summary(name)
            if summary and summary.get("image"):
                return {"url": summary["image"], "source": "wikipedia",
                        "label": "Wikipedia lead image", "kind": "photo"}
    return None


def _description_from_wikipedia(kind, artist, title=None):
    """Artist or album description straight from Wikipedia.

    Artists are looked up by name; albums by "artist album" search, then by
    the "(album)" disambiguation Wikipedia uses. The returned text is the
    lead-paragraph extract, which is what a description block wants.
    """
    if kind == "artist":
        summary = wikipedia_summary(artist)
        if summary:
            return summary
        for candidate in wikipedia_search(artist, limit=3):
            summary = wikipedia_summary(candidate)
            if summary:
                return summary
        return None
    queries = [f"{artist} {title} album", f"{title} {artist}", title]
    for query in queries:
        for candidate in wikipedia_search(query, limit=3):
            if _norm(title) not in _norm(candidate):
                continue
            summary = wikipedia_summary(candidate)
            if summary and summary.get("extract"):
                return summary
    return None


def artist_description(name, mbid=None, cfg=None):
    """Artist description from the configured description sources."""
    for source in source_order(cfg, "description_sources", DESCRIPTION_SOURCES):
        if source == "wikipedia":
            summary = _description_from_wikipedia("artist", name)
            if summary:
                return {"text": summary["extract"], "title": summary["title"],
                        "source": "wikipedia", "source_url": summary.get("url"),
                        "short": summary.get("description") or ""}
        elif source == "audiodb":
            row = audiodb_artist(name)
            if row and row.get("bio"):
                return {"text": row["bio"], "title": row.get("name") or name,
                        "source": "audiodb",
                        "source_url": f"{AUDIODB_BASE}/{AUDIODB_KEY}/search.php?s={quote(name)}"}
        elif source == "musicbrainz":
            artist_mbid = mbid or resolve_artist_mbid(name)
            if not artist_mbid:
                continue
            try:
                data = integrations.mb_get_cached(
                    f"artist/{artist_mbid}", {"inc": "annotation+url-rels", "fmt": "json"})
            except Exception:
                continue
            annotation = (data or {}).get("annotation") or ""
            if annotation.strip():
                return {"text": annotation.strip(), "title": (data or {}).get("name") or name,
                        "source": "musicbrainz",
                        "source_url": f"https://musicbrainz.org/artist/{artist_mbid}"}
    return None


def album_description(artist, album, mbid=None, cfg=None):
    """Album description from the configured description sources."""
    for source in source_order(cfg, "description_sources", DESCRIPTION_SOURCES):
        if source == "wikipedia":
            summary = _description_from_wikipedia("album", artist, album)
            if summary:
                return {"text": summary["extract"], "title": summary["title"],
                        "source": "wikipedia", "source_url": summary.get("url"),
                        "short": summary.get("description") or ""}
        elif source == "audiodb":
            row = audiodb_album(artist, album)
            if row and row.get("description"):
                return {"text": row["description"], "title": row.get("title") or album,
                        "source": "audiodb", "source_url": None}
        elif source == "musicbrainz":
            rg = None
            if mbid:
                rg = {"mbid": mbid}
            else:
                rg = resolve_release_group(artist, album, cfg)
            if not rg or not rg.get("mbid"):
                continue
            try:
                data = integrations.mb_get_cached(
                    f"release-group/{rg['mbid']}",
                    {"inc": "annotation+url-rels", "fmt": "json"})
            except Exception:
                continue
            annotation = (data or {}).get("annotation") or ""
            if annotation.strip():
                return {"text": annotation.strip(), "title": (data or {}).get("title") or album,
                        "source": "musicbrainz",
                        "source_url": f"https://musicbrainz.org/release-group/{rg['mbid']}"}
    return None


def metadata_candidates(artist, album="", cfg=None):
    """Image + description candidates for an artist (and their album).

    One composition point for the reliable providers: the configured image
    chain's own best pick plus every individual provider's images (Deezer
    photo, TheAudioDB thumb/banner/wide/fanart, Apple artwork, Wikipedia lead
    image) and the first artist/album description from the configured
    description chain. Nothing is written here — callers save through
    `mlo.artistdata` (see server.imports.apply_metadata).

    Returns {"images": [{url, source, label, kind}], "artist_description":
    {text, source, ...}|None, "album_description": {...}|None}.
    """
    artist = str(artist or "").strip()
    album = str(album or "").strip()
    images = []
    seen = set()

    def add(url, source, label, kind="photo"):
        if url and url not in seen:
            seen.add(url)
            images.append({"url": url, "source": source, "label": label, "kind": kind})

    if artist:
        auto = artist_image(artist, cfg=cfg)
        if auto:
            add(auto.get("url"), auto.get("source"),
                auto.get("label") or "Automatic pick", auto.get("kind") or "photo")
        row = audiodb_artist(artist)
        if row:
            for key, label in (("thumb", "TheAudioDB artist thumb"),
                               ("banner", "TheAudioDB artist banner"),
                               ("wide_thumb", "TheAudioDB wide thumb"),
                               ("fanart", "TheAudioDB fanart")):
                add(row.get(key), "audiodb", label, "photo" if key == "thumb" else "wide")
        dz = deezer_artist(artist)
        if dz:
            add(dz.get("image"), "deezer", "Deezer artist photo", "photo")
        add(itunes_artist_artwork(artist), "itunes",
            "Apple album artwork (artist has no photo API)", "album_art")
        summary = wikipedia_summary(artist)
        if summary:
            add(summary.get("image"), "wikipedia", "Wikipedia lead image", "photo")
    return {
        "images": images,
        "artist_description": artist_description(artist, cfg=cfg) if artist else None,
        "album_description": (album_description(artist, album, cfg=cfg)
                              if (artist and album) else None),
    }


def album_genres(artist, album, dz_id=None, cfg=None):
    """Genre names for an album from the discovery providers (Deezer first:
    its album detail carries a real genre list, iTunes carries one primary
    genre). Used to fill a missing GENRE tag."""
    genres = []
    if cfg is None or cfg.get("discovery_enabled", True):
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


def genre_lookup(artist, album, track_path=None, cfg=None):
    """Genre names for one track/album — the hook script 8 fills GENRE with.

    Delegates to ``integrations.genre_chain``, the ONE genre resolver (RYM →
    community sources → streaming providers → MusicBrainz, merged, deduped,
    Title-Cased, capped at `mb_genre_count`), so a library-wide Auto tagging
    run and an import write genres the same way.

    The answer is memoised per album for TTL_META: script 8 calls this hook
    once PER TRACK, and a chain that reaches the network must not run a dozen
    times for one album. Never raises — an offline machine returns [] and the
    tag stays missing (grading then flags it, which is the honest outcome).
    """
    if cfg is None:
        try:
            from mlo.config import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
    try:
        limit = max(1, int(cfg.get("mb_genre_count") or 3))
    except (TypeError, ValueError):
        limit = 3
    key = (_norm(artist), _norm(album), limit)
    with _GENRE_MEMO_LOCK:
        hit = _GENRE_MEMO.get(key)
    if hit and time.time() - hit[0] < TTL_META:
        return list(hit[1])
    try:
        names = integrations.genre_chain(artist=artist or "", album=album or "",
                                         limit=limit, cfg=cfg).get("genres") or []
    except Exception:
        names = []
    with _GENRE_MEMO_LOCK:
        _GENRE_MEMO[key] = (time.time(), list(names))
        if len(_GENRE_MEMO) > _CACHE_MAX:
            oldest = min(_GENRE_MEMO.items(), key=lambda kv: kv[1][0])[0]
            _GENRE_MEMO.pop(oldest, None)
    return list(names)
