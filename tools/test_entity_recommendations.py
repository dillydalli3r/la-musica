#!/usr/bin/env python3
"""Entity recommendations — the ONLINE shelf beside the local "More like this".

`GET /api/discover/recommended?seed_kind=artist|album|track&seed_mbid=…` seeds
the same shelf from ONE page entity instead of the library's genre mix, so this
file pins what that shelf promises, offline and with the providers stubbed at
their transport:

* the seed comes from the entity's own identity — its MusicBrainz id first,
  else artist+title — and a page that names nothing is refused in words rather
  than answered with somebody else's rows;
* every row names the provider it came from (`source`) and the relationship
  that provider states (`reason`), plus the provider's OWN number in `score`
  (null when it states none);
* per-source outcomes keep the registry's vocabulary: `skipped: no <key>` for a
  source without its credential, `skipped: <its own limit>` for an answer a
  source that COULD speak about the entity did not have, `failed: <the
  provider's own words>` for a call that raised;
* a source whose entity feed belongs to another kind of page (Last.fm's
  similar-artists/tracks feeds on an album) is not a failure at all: it is not
  asked, and it is reported once, compactly, in `not_applicable` — a short
  marker plus the provider's own sentence — so a capability is never rendered
  as an error and no pill is cut off mid-sentence;
* two sources naming the same identity are ONE row, with the preferred
  source's reason kept and the other listed in `also_from`;
* an entity shelf is "what this page is LIKE", so a row the library owns is
  KEPT and marked (`owned`, its library `path`) — unlike the library-seeded
  shelf, where an owned row is not a recommendation and is dropped;
* a seed with no id still works: the name is what the sources are asked about;
* the DISCOVER page's own call (`seed=library`) answers exactly as before.

Synthetic library (no disk scan) and stubbed providers (no network, all calls
routed through `server.discovery`'s one transport seam and the two MusicBrainz
seams), so the file runs offline and deterministically.

Run:  python tools/test_entity_recommendations.py
"""
import inspect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi import HTTPException  # noqa: E402

from server import api_discover, discover, discovery  # noqa: E402
from server import integrations as intg  # noqa: E402

passed = 0


def ok(cond, label):
    global passed
    if not cond:
        raise AssertionError(f"FAIL: {label}")
    passed += 1
    print(f"  ok: {label}")


# The row shape `server/discover.py`'s docstring promises every Discover view.
ROW_KEYS = {"kind", "title", "artist", "year", "source", "source_label",
            "cover_url", "page_url", "mbid", "release_group_mbid", "path",
            "owned", "in_library", "tracks", "score", "reason", "also_from"}

CALLS = []

# --------------------------------------------------------------------------- #
# The fixture library: ONE owned album (Slowdive / Souvlaki, its release group
# and two tracks tagged) — so a row the entity shelf states as owned has a real
# library path behind it, and every other fixture row is genuinely unowned.
# --------------------------------------------------------------------------- #
RG_SOUVLAKI = "11111111-1111-1111-1111-111111111111"
TRACK_ALISON = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ARTIST_SLOWDIVE = "44444444-4444-4444-4444-444444444444"
ARTIST_LUSH = "55555555-5555-5555-5555-555555555555"
ARTIST_BROADCAST = "77777777-7777-7777-7777-777777777777"

LIBRARY = {"folder": "C:/Music", "artists": [
    {"path": "C:/Music/Slowdive", "name": "Slowdive", "aggregate": {}, "albums": [
        {"path": "C:/Music/Slowdive/Souvlaki",
         "meta": {"ALBUM": "Souvlaki", "ALBUMARTIST": "Slowdive", "DATE": "1993",
                  "MUSICBRAINZ_RELEASEGROUPID": RG_SOUVLAKI},
         "tracks": [
             {"path": "C:/Music/Slowdive/Souvlaki/01 Alison.flac",
              "tags": {"TITLE": "Alison", "ARTIST": "Slowdive", "GENRE": "Shoegaze",
                       "MUSICBRAINZ_TRACKID": TRACK_ALISON,
                       "MUSICBRAINZ_ALBUMARTISTID": ARTIST_SLOWDIVE}},
             {"path": "C:/Music/Slowdive/Souvlaki/02 Machine Gun.flac",
              "tags": {"TITLE": "Machine Gun", "ARTIST": "Slowdive",
                       "GENRE": "Shoegaze"}},
         ]}]},
]}

CFG = {"music_folder": "C:/Music", "discovery_enabled": True}

# The library payload is passed in (and, for the route-level calls, served by
# the cached builder's own seam), so nothing here scans a disk.
discover._library = lambda cfg, lib=None: lib if lib is not None else LIBRARY
api_discover.load_config = lambda: dict(CFG)

# --------------------------------------------------------------------------- #
# The providers' own answers, in their own dialects.
# --------------------------------------------------------------------------- #
DEEZER_IDS = {"Slowdive": 8, "Lush": 61, "Cocteau Twins": 1041,
              "my bloody valentine": 2465}
DEEZER_NAMES = {v: k for k, v in DEEZER_IDS.items()}
# Deezer's "fans also like" list for Slowdive.
DEEZER_RELATED = {"data": [
    {"id": 61, "name": "Lush", "picture_xl": "https://cdn.deezer/lush.jpg",
     "nb_fan": 12994, "link": "https://www.deezer.com/artist/61"},
    {"id": 1041, "name": "Cocteau Twins", "picture_xl": None,
     "nb_fan": 134179, "link": "https://www.deezer.com/artist/1041"},
    {"id": 2465, "name": "my bloody valentine", "picture_xl": None,
     "nb_fan": 97013, "link": "https://www.deezer.com/artist/2465"},
]}
# One album per related artist, plus the seed artist's own records (one of them
# the album the library holds) and a SINGLE, which `albums_only` must drop.
DEEZER_ALBUMS = {
    61: [{"id": 900, "title": "Spooky", "record_type": "album",
          "release_date": "1992-01-27", "fans": 40000,
          "cover_xl": "https://cdn.deezer/spooky.jpg",
          "link": "https://www.deezer.com/album/900"},
         {"id": 901, "title": "Split", "record_type": "album",
          "release_date": "1994-01-31", "fans": 20000, "cover_xl": None,
          "link": "https://www.deezer.com/album/901"},
         {"id": 902, "title": "Sweetness and Light", "record_type": "single",
          "release_date": "1990-01-01", "fans": 5000, "cover_xl": None,
          "link": "https://www.deezer.com/album/902"}],
    1041: [{"id": 910, "title": "Heaven or Las Vegas", "record_type": "album",
            "release_date": "1990-09-17", "fans": 200000, "cover_xl": None,
            "link": "https://www.deezer.com/album/910"}],
    2465: [{"id": 920, "title": "Loveless", "record_type": "album",
            "release_date": "1991-11-04", "fans": 300000, "cover_xl": None,
            "link": "https://www.deezer.com/album/920"}],
    8: [{"id": 930, "title": "Souvlaki", "record_type": "album",
         "release_date": "1993-06-01", "fans": 250000, "cover_xl": None,
         "link": "https://www.deezer.com/album/930"},
        {"id": 931, "title": "Pygmalion", "record_type": "album",
         "release_date": "1995-02-06", "fans": 120000, "cover_xl": None,
         "link": "https://www.deezer.com/album/931"}],
}
DEEZER_TOP = {
    61: [{"id": 1, "title": "Sweetness and Light",
          "artist": {"name": "Lush"}, "rank": 500000, "duration": 200000,
          "album": {"title": "Gala", "cover_big": None},
          "link": "https://www.deezer.com/track/1"}],
    8: [{"id": 2, "title": "Alison", "artist": {"name": "Slowdive"},
         "rank": 900000, "duration": 220000,
         "album": {"title": "Souvlaki", "cover_big": None},
         "link": "https://www.deezer.com/track/2"},
        {"id": 3, "title": "Machine Gun", "artist": {"name": "Slowdive"},
         "rank": 800000, "duration": 195000,
         "album": {"title": "Souvlaki", "cover_big": None},
         "link": "https://www.deezer.com/track/3"}],
}
# Last.fm's similar tracks for the track seed: one unowned, one the library has.
LASTFM_SIMILAR_TRACKS = {"similartracks": {"track": [
    {"name": "Vapour Trail", "artist": {"name": "Ride"}, "duration": 250000,
     "match": 0.92, "url": "https://www.last.fm/music/Ride/_/Vapour+Trail"},
    {"name": "Machine Gun", "artist": {"name": "Slowdive"}, "duration": 195000,
     "match": 0.83, "url": "https://www.last.fm/music/Slowdive/_/Machine+Gun"},
]}}
# ListenBrainz Labs' similar artists — MBID-native, with its own score.
LB_SIMILAR = {
    ARTIST_SLOWDIVE: [
        {"artist_mbid": ARTIST_LUSH, "name": "Lush", "comment": "",
         "type": "Group", "score": 1234, "reference_mbid": ARTIST_SLOWDIVE},
        {"artist_mbid": ARTIST_BROADCAST, "name": "Broadcast", "comment": "",
         "type": "Group", "score": 987, "reference_mbid": ARTIST_SLOWDIVE},
    ],
}
# Apple has no related feed: its search answers the NAMED artist's albums, and
# a same-named act rides in on the term exactly as Apple returns it.
ITUNES_ALBUMS = {"results": [
    {"collectionId": 1, "collectionName": "Souvlaki", "artistName": "Slowdive",
     "artistId": 1, "artworkUrl100": "https://is1.mzstatic/x/100x100bb.jpg",
     "releaseDate": "1993-06-01T07:00:00Z", "trackCount": 10,
     "primaryGenreName": "Alternative",
     "collectionViewUrl": "https://music.apple.com/album/1"},
    {"collectionId": 2, "collectionName": "Pygmalion", "artistName": "Slowdive",
     "artistId": 1, "artworkUrl100": "https://is1.mzstatic/y/100x100bb.jpg",
     "releaseDate": "1995-02-06T07:00:00Z", "trackCount": 10,
     "primaryGenreName": "Alternative",
     "collectionViewUrl": "https://music.apple.com/album/2"},
    {"collectionId": 3, "collectionName": "Slowdive", "artistName": "Kalabi",
     "artistId": 3, "artworkUrl100": None, "releaseDate": "2014-01-01T07:00:00Z",
     "trackCount": 8, "primaryGenreName": "Electronic",
     "collectionViewUrl": "https://music.apple.com/album/3"},
]}

# --------------------------------------------------------------------------- #
# MusicBrainz's own answers. Its tag search is the whole entity capability, so
# the fixtures below are what an entity shelf is built from: the genres it
# states per entity (a release group states three, to show the per-request cap;
# an artist states one; a recording states one), the tag search per kind, the
# artist's release-group browse and the recordings it files under the artist.
# --------------------------------------------------------------------------- #
RG_PYGMALION = "33333333-3333-3333-3333-333333333333"
RG_JUST_FOR_A_DAY = "99999999-9999-9999-9999-999999999999"
TRACK_MACHINE_GUN = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
TRACK_SOON = "cccccccc-cccc-cccc-cccc-cccccccccccc"
ARTIST_RIDE = "88888888-8888-8888-8888-888888888888"
ARTIST_MBV = "66666666-6666-6666-6666-666666666666"

# What MusicBrainz states per entity. An entity with NO entry states no genres
# at all, which is a skip with its own sentence — and the reason an album or a
# track seed falls back to its artist's genres.
MB_GENRES = {
    ARTIST_SLOWDIVE: ["shoegaze"],
    RG_SOUVLAKI: ["shoegaze", "dream pop", "ambient"],
    TRACK_ALISON: ["dream pop"],
}
# MusicBrainz's tag search per entity kind. The ARTIST search names the seed
# back — MusicBrainz's tag index does not know which artist the page is about —
# so the shelf has to drop it, and it names one artist no other feed names.
MB_TAG = {
    "artist": [
        {"id": ARTIST_SLOWDIVE, "score": 100, "title": "Slowdive",
         "country": "GB", "disambiguation": ""},
        {"id": ARTIST_RIDE, "score": 82, "title": "Ride", "country": "GB",
         "disambiguation": ""},
    ],
    "release-group": [
        {"id": RG_JUST_FOR_A_DAY, "score": 91, "title": "Just For A Day",
         "artist": "Slowdive", "artist_mbid": ARTIST_SLOWDIVE,
         "first_release_date": "1991-09-02", "primary_type": "Album",
         "secondary_types": []},
    ],
    "recording": [
        {"id": TRACK_SOON, "score": 77, "title": "Soon",
         "artist": "My Bloody Valentine", "artist_mbid": ARTIST_MBV,
         "first_release_date": "1990-11-05", "length": 400000},
    ],
}
# The artist's own records, through the release-group browse (`artist=`). The
# keys are the ones the browse really answers with (measured live against
# MusicBrainz: `primary_type`/`first_release_date`, oldest first).
MB_RELEASE_GROUPS = {ARTIST_SLOWDIVE: [
    {"id": RG_SOUVLAKI, "title": "Souvlaki", "primary_type": "Album",
     "secondary_types": [], "first_release_date": "1993-06-01"},
    {"id": RG_PYGMALION, "title": "Pygmalion", "primary_type": "Album",
     "secondary_types": [], "first_release_date": "1995-02-06"},
]}
# …and the recordings its index files under the artist (`arid:`).
MB_ARTIST_RECORDINGS = {ARTIST_SLOWDIVE: [
    {"id": TRACK_ALISON, "score": 100, "title": "Alison", "artist": "Slowdive",
     "artist_mbid": ARTIST_SLOWDIVE, "first_release_date": "1993-06-01",
     "length": 220000},
    {"id": TRACK_MACHINE_GUN, "score": 100, "title": "Machine Gun",
     "artist": "Slowdive", "artist_mbid": ARTIST_SLOWDIVE,
     "first_release_date": "1993-06-01", "length": 195000},
]}

# Spotify's artist-level entity routes: the search hit IS the artist (the same
# one request `spotify_artist_genres` reads), then its albums and top tracks.
SPOTIFY_ARTIST = {"artists": {"items": [
    {"id": "sp-slowdive", "name": "Slowdive", "genres": ["shoegaze"]},
]}}
SPOTIFY_ALBUMS = {"items": [
    {"id": "sp-souvlaki", "name": "Souvlaki", "album_type": "album",
     "release_date": "1993-06-01", "total_tracks": 10,
     "artists": [{"name": "Slowdive"}],
     "images": [{"url": "https://i.scdn.co/souvlaki.jpg"}],
     "external_urls": {"spotify": "https://open.spotify.com/album/sp-souvlaki"}},
    {"id": "sp-pygmalion", "name": "Pygmalion", "album_type": "album",
     "release_date": "1995-02-06", "total_tracks": 10,
     "artists": [{"name": "Slowdive"}], "images": [],
     "external_urls": {"spotify": "https://open.spotify.com/album/sp-pygmalion"}},
]}
SPOTIFY_TOP = {"tracks": [
    {"id": "sp-alison", "name": "Alison", "duration_ms": 220000,
     "popularity": 55, "artists": [{"name": "Slowdive"}],
     "album": {"name": "Souvlaki", "images": [{"url": "https://i.scdn.co/s.jpg"}]},
     "external_ids": {"isrc": "GBAAA9300001"},
     "external_urls": {"spotify": "https://open.spotify.com/track/sp-alison"}},
]}


def deezer_answer(url, params):
    """One Deezer endpoint's own JSON, or None for a route the stub does not
    serve (which is how a provider with no such endpoint answers)."""
    if "search/artist" in url:
        name = str(params.get("q") or "")
        ident = DEEZER_IDS.get(name)
        if ident is None:
            return {"data": []}
        return {"data": [{"id": ident, "name": name, "picture_xl": None,
                          "nb_fan": 400000, "nb_album": 4,
                          "link": f"https://www.deezer.com/artist/{ident}"}]}
    if url.endswith("/related"):
        return DEEZER_RELATED
    if url.endswith("/albums"):
        return {"data": DEEZER_ALBUMS.get(int(url.split("/artist/")[1].split("/")[0]), [])}
    if url.endswith("/top"):
        return {"data": DEEZER_TOP.get(int(url.split("/artist/")[1].split("/")[0]), [])}
    if "/artist/" in url:
        ident = int(url.rsplit("/", 1)[1])
        return {"id": ident, "name": DEEZER_NAMES.get(ident, ""),
                "picture_xl": None, "nb_fan": 400000,
                "link": f"https://www.deezer.com/artist/{ident}"}
    return None


def lb_answer(params):
    return LB_SIMILAR.get(str(params.get("artist_mbids") or ""), [])


def lastfm_answer(url, params):
    method = str(params.get("method") or "")
    if method == "track.getsimilar":
        return LASTFM_SIMILAR_TRACKS
    return {}


def spotify_answer(url, params):
    """Spotify's artist-level routes: the search hit (which counts only when
    its own name is the name asked for), the artist's albums and its top
    tracks."""
    if url.endswith("/search"):
        return SPOTIFY_ARTIST if str(params.get("q") or "") == "Slowdive" \
            else {"artists": {"items": []}}
    if url.endswith("/albums"):
        return SPOTIFY_ALBUMS
    if url.endswith("/top-tracks"):
        return SPOTIFY_TOP
    return None


def route(url, params):
    """Every stubbed provider, by URL — the transport seam's own router."""
    if "api.deezer.com" in url:
        return deezer_answer(url, params)
    if "labs.api.listenbrainz.org" in url:
        return lb_answer(params)
    if "itunes.apple.com" in url:
        return ITUNES_ALBUMS
    if "ws.audioscrobbler.com" in url:
        return lastfm_answer(url, params)
    if "api.spotify.com" in url:
        return spotify_answer(url, params)
    return None


def stub_json():
    def fake(url, params=None, headers=None, timeout=None, ttl=None, host=None):
        CALLS.append(("json", url, dict(params or {})))
        return route(url, params or {})
    discovery._json = fake


def stub_mb(search=None):
    """The MusicBrainz seams, in the three places the shelf reads them.

    `search_mb` is the index: the entity tag searches, the artist's recordings
    (`arid:`) and the library-seeded path all come through it. `mb_get_cached`
    is the cached entity lookup the genres come from (`artist/{id}`,
    `release-group/{id}`, `recording/{id}`) AND the release-group browse that
    answers with an artist's records — one stub, because that is one seam. The
    name resolve ListenBrainz needs when a page carries no id is the third. A
    case may pass its own `search` (the library-seeded path does)."""
    def fake_search(entity, query, limit=100, mode="free", offset=0, **kw):
        artist_id = str(kw.get("artist_id") or "")
        CALLS.append(("search_mb", entity,
                      query or ("arid:%s" % artist_id if artist_id else ""),
                      limit))
        if callable(search):
            return search(entity, query, limit, offset)
        if search:
            return search
        rows = MB_ARTIST_RECORDINGS.get(artist_id, []) if artist_id \
            else MB_TAG.get(entity, [])
        return {"rows": rows, "total": len(rows), "offset": offset,
                "next": None, "query": query}
    intg.search_mb = fake_search

    def fake_cached(endpoint, params=None, **kw):
        CALLS.append(("mb_get_cached", endpoint, dict(params or {})))
        params = params or {}
        if endpoint == "release-group" and params.get("artist"):
            rows = MB_RELEASE_GROUPS.get(params["artist"], [])
            return {"release-groups": rows, "release-group-count": len(rows)}
        genres = MB_GENRES.get(str(endpoint).rsplit("/", 1)[-1], [])
        return {"genres": [{"name": name} for name in genres]}
    intg.mb_get_cached = fake_cached

    def fake_resolve(name, cfg=None, timeout=None):
        CALLS.append(("resolve_artist_mbid", name))
        return ARTIST_SLOWDIVE if name == "Slowdive" else None
    discovery.resolve_artist_mbid = fake_resolve


# The real wrapper a raising-provider case replaces, kept so `fresh()` can put
# it back — a patch on `discovery` would otherwise leak into later cases.
REAL_DEEZER_RELATED = discovery.deezer_related_artists


def fresh(search=None):
    CALLS.clear()
    discovery._CACHE.clear()
    discovery._HOST_WARNED.clear()
    CFG.pop("lastfm_api_key", None)
    CFG.pop("spotify_client_id", None)
    CFG.pop("spotify_client_secret", None)
    CFG.pop("cover_country", None)
    # A case that patches a provider WRAPPER (to raise) puts the real one back
    # here: the module attribute would otherwise leak into every later case.
    discovery.deezer_related_artists = REAL_DEEZER_RELATED
    stub_json()
    stub_mb(search=search)
    intg._spotify_token = lambda cfg=None, timeout=None: None


def seed_rows(**kwargs):
    """One entity-shelf request, through the ROUTE (so the query the UI sends
    is the query under test)."""
    return api_discover.recommended_list(**kwargs)


def by_title(payload):
    return {row["title"]: row for row in payload["items"]}


# --------------------------------------------------------------------------- #
# 1) The seed: identity first, name second, and "nothing to seed from" said
# --------------------------------------------------------------------------- #
print("== the entity seed ==")
fresh()
seed, why = discover.entity_seed("artist", ARTIST_SLOWDIVE, "Slowdive")
ok(seed == {"kind": "artist", "mbid": ARTIST_SLOWDIVE, "name": "Slowdive",
            "artist": "Slowdive"} and why == "",
   f"an artist seed keeps its own id and answers to its own name ({seed})")
seed, why = discover.entity_seed("album", RG_SOUVLAKI, "Souvlaki", "Slowdive")
ok(seed["mbid"] == RG_SOUVLAKI and seed["artist"] == "Slowdive"
   and seed["name"] == "Souvlaki" and why == "",
   "an album seed carries its release-group id beside artist and title")
seed, why = discover.entity_seed("track", TRACK_ALISON, "Alison", "Slowdive")
ok(seed["mbid"] == TRACK_ALISON and why == "",
   "a track seed carries its recording id")
seed, why = discover.entity_seed("artist", name="", mbid="")
ok(why == "this page names no artist to seed from",
   f"a page that named nothing says so ({why})")
seed, why = discover.entity_seed("album", "", "Souvlaki", "")
ok(why.startswith("this page names no artist for its album"),
   f"an album with no artist is refused, not guessed ({why})")
ok(discover.SEED_KINDS == ("artist", "album", "track")
   and discover.entity_seed("film", "x", "y")[1].startswith("unknown seed kind"),
   "the registry names the three seed kinds and refuses anything else")

empty_seed = seed_rows(seed_kind="album", seed_name="", seed_artist="Slowdive",
                       kind="albums")
ok(empty_seed["items"] == [] and empty_seed["sources_asked"] == []
   and empty_seed["notes"]["recommended"].startswith("skipped: this page names no album"),
   f"an unusable seed asks no source and explains itself "
   f"({empty_seed['notes']['recommended']})")

# --------------------------------------------------------------------------- #
# 2) An ARTIST page: similar artists from Deezer and ListenBrainz, with the
#    keyed source, the genre-only source and the refusal all reported
# --------------------------------------------------------------------------- #
print("== an artist page ==")
fresh()
artist = seed_rows(seed_kind="artist", seed_mbid=ARTIST_SLOWDIVE,
                   seed_name="Slowdive", kind="artists", limit=20)
titles = [row["title"] for row in artist["items"]]
ok(artist["items"] and all(set(row) == ROW_KEYS for row in artist["items"]),
   f"every row carries exactly the documented shape "
   f"({sorted(set(artist['items'][0]) ^ ROW_KEYS)})")
ok(all(row["source"] and row["reason"] for row in artist["items"]),
   f"every row names its provider and the reason it is here "
   f"({sorted({(r['source'], r['reason']) for r in artist['items']})})")
ok(set(titles) == {"Lush", "Cocteau Twins", "my bloody valentine", "Broadcast",
                   "Ride"},
   f"Deezer's related feed, ListenBrainz' similar artists and MusicBrainz's tag "
   f"search all contribute ({titles})")
ok(by_title(artist)["Lush"]["reason"] == "sounds like Slowdive (Deezer)"
   and by_title(artist)["Broadcast"]["reason"] == "sounds like Slowdive (ListenBrainz)",
   "each row's reason names the source that stated the relationship")
ok(by_title(artist)["Ride"]["reason"] == "genre: shoegaze (MusicBrainz)"
   and by_title(artist)["Ride"]["source"] == "musicbrainz",
   f"MusicBrainz answers an entity shelf with the entity's OWN genre, through "
   f"the same tag search the genre shelves use "
   f"({by_title(artist)['Ride']['reason']})")
ok("Slowdive" not in titles,
   "the seed itself is not its own neighbour, however the tag search names it")
ok(by_title(artist)["Lush"]["score"] == 12994.0
   and by_title(artist)["Broadcast"]["score"] == 987.0,
   "the provider's own number rides along as `score`")
ok(artist["basis"] == "artist: Slowdive (%s)" % ARTIST_SLOWDIVE,
   f"the basis names the seed AND the identity it came from ({artist['basis']})")
ok(artist["sources_asked"] == ["musicbrainz", "deezer", "lastfm", "listenbrainz"],
   f"only the sources that can recommend artists are asked ({artist['sources_asked']})")
ok(artist["notes"]["lastfm"] == "skipped: no lastfm_api_key",
   f"a source without its key is skipped by name ({artist['notes']['lastfm']})")
ok("musicbrainz" not in artist["notes"],
   "MusicBrainz answered about the entity, so it carries no note")
ok("deezer" not in artist["notes"] and "listenbrainz" not in artist["notes"],
   "a source that answered carries no note")
ok(all(row["owned"] is False and row["path"] is None for row in artist["items"]),
   "an artist the library does not hold links nowhere")

# An entity that states NO genres is a skip in MusicBrainz's own words — the
# blanket "no similar-entity feed" sentence is gone, because the feed exists.
fresh()
bare = seed_rows(seed_kind="artist", seed_mbid=ARTIST_LUSH, seed_name="Lush",
                 kind="artists")
ok(bare["notes"]["musicbrainz"] == 'skipped: MusicBrainz states no genres for "Lush"',
   f"an entity MusicBrainz states no genres for says so "
   f"({bare['notes']['musicbrainz']})")
ok(not [c for c in CALLS if c[0] == "search_mb" and "tag:" in c[2]],
   "…and no tag search is asked on its behalf")

# The same artist from two sources is ONE row: the preferred source's reason
# is kept, the id a later source stated is not lost, and it is listed as such.
fresh()
lush = by_title(seed_rows(seed_kind="artist", seed_mbid=ARTIST_SLOWDIVE,
                          seed_name="Slowdive", kind="artists"))
ok(len([r for r in lush.values() if r["title"] == "Lush"]) == 1
   and lush["Lush"]["source"] == "deezer"
   and lush["Lush"]["reason"] == "sounds like Slowdive (Deezer)"
   and lush["Lush"]["also_from"] == ["listenbrainz"]
   and lush["Lush"]["mbid"] == ARTIST_LUSH,
   f"two sources naming one artist are one row — preferred source's reason, "
   f"the other in also_from, the id kept ({lush['Lush']})")

# A source that RAISES is reported with its own words, never as "nothing".
fresh()
discovery.deezer_related_artists = lambda *a, **k: (_ for _ in ()).throw(
    RuntimeError('Deezer refused: 403 "Quota exceeded"'))
failed = seed_rows(seed_kind="artist", seed_mbid=ARTIST_SLOWDIVE,
                   seed_name="Slowdive", kind="artists")
ok(failed["notes"]["deezer"] == 'failed: Deezer refused: 403 "Quota exceeded"',
   f"a refusing provider is reported in its own words ({failed['notes']['deezer']})")
ok(sorted(r["title"] for r in failed["items"]) == ["Broadcast", "Lush", "Ride"],
   "…and the sources that DID answer still fill the shelf")

# --------------------------------------------------------------------------- #
# 3) A page with NO id: the name is the seed, and ListenBrainz resolves it
# --------------------------------------------------------------------------- #
print("== a seed with no id ==")
fresh()
by_name = seed_rows(seed_kind="artist", seed_name="Slowdive", kind="artists")
ok(by_name["basis"] == "artist: Slowdive (by name)"
   and "Broadcast" in [row["title"] for row in by_name["items"]],
   f"a page whose tags carry no id still seeds by name ({by_name['basis']})")
ok(("resolve_artist_mbid", "Slowdive") in CALLS,
   "the MBID-native feed is fed the name resolved through MusicBrainz")
fresh()
no_id_track = seed_rows(seed_kind="track", seed_name="Alison",
                        seed_artist="Slowdive", kind="tracks")
ok(no_id_track["items"] and "musicbrainz" not in no_id_track["notes"]
   and any(c[0] == "search_mb" and "tag:" in c[2] for c in CALLS),
   "a track page carrying no recording id still gets MusicBrainz's genre rows, "
   "read off the artist its name resolves to")
fresh()
unresolved = seed_rows(seed_kind="artist", seed_name="Nobody At All",
                       kind="artists")
ok(unresolved["notes"]["listenbrainz"].startswith("skipped: could not resolve")
   and unresolved["notes"]["deezer"].startswith("skipped: Deezer knows no related"),
   f"a name nothing resolves is skipped by both feeds, not guessed at "
   f"({unresolved['notes']})")

# --------------------------------------------------------------------------- #
# 4) An ALBUM page: the artist's own records beside its neighbours', the row
#    the library owns KEPT with its path, and a bounded fan-out
# --------------------------------------------------------------------------- #
print("== an album page ==")
fresh()
album = seed_rows(seed_kind="album", seed_mbid=RG_SOUVLAKI, seed_name="Souvlaki",
                  seed_artist="Slowdive", kind="albums", limit=20)
albums = by_title(album)
ok(album["items"] and all(set(row) == ROW_KEYS for row in album["items"]),
   "album rows carry the same one shape")
ok("Spooky" in albums and "Heaven or Las Vegas" in albums and "Loveless" in albums,
   f"the related artists' records are the bridge to album rows ({sorted(albums)})")
ok(albums["Spooky"]["reason"].startswith("more from Lush — sounds like Slowdive (Deezer)"),
   f"…and each says which relationship brought it ({albums['Spooky']['reason']})")
ok("Sweetness and Light" not in albums,
   "a single is not passed off as an album (Deezer's albums_only)")
ok(albums["Souvlaki"]["owned"] is True
   and albums["Souvlaki"]["path"] == "C:/Music/Slowdive/Souvlaki"
   and albums["Souvlaki"]["tracks"] == ["Alison", "Machine Gun"],
   f"a row the library OWNS is kept and carries its library path "
   f"({albums['Souvlaki']['owned']}, {albums['Souvlaki']['path']})")
ok(albums["Pygmalion"]["source"] == "musicbrainz"
   and albums["Pygmalion"]["reason"] == "more release groups by Slowdive (MusicBrainz)",
   f"MusicBrainz's browse request states the artist's own records, and says "
   f"exactly that — never that they are similar "
   f"({albums['Pygmalion']['reason']})")
ok(albums["Pygmalion"]["owned"] is False and albums["Pygmalion"]["path"] is None
   and albums["Pygmalion"]["in_library"] is True,
   "an unowned row by an artist the library collects has no path, only the weaker flag")
ok("deezer" in albums["Pygmalion"]["also_from"]
   and albums["Pygmalion"]["score"] == 120000.0,
   f"the seed's own artist from Deezer is ONE row with MusicBrainz's, and "
   f"Deezer's own number survives the merge ({albums['Pygmalion']})")
ok(albums["Just For A Day"]["reason"] == "genre: shoegaze (MusicBrainz)"
   and albums["Just For A Day"]["mbid"] == RG_JUST_FOR_A_DAY,
   f"…and the release-group tag search adds what the artist's neighbours do "
   f"not hold ({albums['Just For A Day']['reason']})")
ok(len([c for c in CALLS if c[0] == "search_mb" and "tag:" in c[2]])
   == discover.ENTITY_GENRES,
   f"the entity states three genres and MusicBrainz — one request per second — "
   f"is asked about {discover.ENTITY_GENRES}")
ok(all(c[3] == discover.ENTITY_GENRE_ROWS
       for c in CALLS if c[0] == "search_mb" and "tag:" in c[2])
   and [c[2]["limit"] for c in CALLS if c[0] == "mb_get_cached"
        and c[1] == "release-group"][-1] == discover.ENTITY_BROWSE_ROWS,
   f"…with bounded windows, so MusicBrainz's rows cannot BE the shelf it leads "
   f"({discover.ENTITY_GENRE_ROWS} per genre, "
   f"{discover.ENTITY_BROWSE_ROWS} for the browse)")
ok(len([r for r in album["items"] if r["title"] == "Souvlaki"]) == 1
   and "itunes" in albums["Souvlaki"]["also_from"],
   f"Deezer and Apple naming one album is ONE row ({albums['Souvlaki']['also_from']})")
ok("Kalabi" not in {row["artist"] for row in album["items"]},
   "Apple's term search is filtered to the artist Apple NAMED, so a same-named act is out")
ok(album["basis"] == "album: Slowdive — Souvlaki (%s)" % RG_SOUVLAKI,
   f"an album basis names artist and title ({album['basis']})")
ok(album["notes"]["spotify"].startswith("skipped: no spotify_client_id")
   and not [sid for sid in ("musicbrainz", "deezer", "itunes") if sid in album["notes"]],
   f"every source that WAS asked and could not answer says why ({sorted(album['notes'])})")
ok([one["id"] for one in album["not_applicable"]]
   == ["lastfm", "listenbrainz", "discogs"]
   and not [one for one in album["not_applicable"] if one["id"] in album["notes"]],
   f"…while the artist-level feeds are NOT failures on an album: there is no "
   f"similar-ALBUM feed to ask them about "
   f"({[o['id'] for o in album['not_applicable']]})")
fan_out = [c for c in CALLS if c[1].endswith("/albums")]
ok(len(fan_out) == discover.RELATED_FANOUT + 1,
   f"the related-artist bridge fans out over {discover.RELATED_FANOUT} artists "
   f"plus the seed's own, not the whole list ({len(fan_out)} album calls)")
bounded = seed_rows(seed_kind="album", seed_mbid=RG_SOUVLAKI, seed_name="Souvlaki",
                    seed_artist="Slowdive", kind="albums", limit=3)
ok(len(bounded["items"]) == 3,
   "the shelf is the size it was asked for")

# --------------------------------------------------------------------------- #
# 4b) What a page's own LEVEL can be answered: a source whose feed belongs to
#     another kind of page is INFORMATION, not a failure — and a source that
#     really failed on the same shelf is still reported as one
# --------------------------------------------------------------------------- #
print("== what this page's level can be answered ==")
fresh()
noted = seed_rows(seed_kind="album", seed_mbid=RG_SOUVLAKI, seed_name="Souvlaki",
                  seed_artist="Slowdive", kind="albums")
cannot = {one["id"]: one for one in noted["not_applicable"]}
ok(set(cannot) == {"lastfm", "listenbrainz", "discogs"}
   and all(set(one) == {"id", "label", "short", "why"} for one in cannot.values()),
   f"an album page names the artist-level feeds once each, compactly "
   f"({sorted(cannot)})")
ok(not [sid for sid in cannot if sid in noted["notes"]]
   and not [sid for sid in cannot if sid in noted["sources_asked"]],
   "…they are not asked at all, so they are neither a `skipped:` note nor in "
   "`sources_asked`")
ok(cannot["lastfm"]["short"] == "artist & track pages only"
   and cannot["lastfm"]["why"] == ("Last.fm's entity feeds are similar ARTISTS "
                                   "and similar TRACKS — it has no "
                                   "similar-ALBUMS feed"),
   f"the marker says which pages the feed answers; the full sentence — the "
   f"provider's own limit — is the tooltip ({cannot['lastfm']})")
ok(cannot["listenbrainz"]["short"] == "artist pages only"
   and cannot["discogs"]["short"] == "no similar-entity feed"
   and cannot["discogs"]["why"].startswith("Discogs browses by style"),
   f"ListenBrainz publishes the artists feed only, Discogs states no "
   f"similarity at all ({[one['short'] for one in cannot.values()]})")
ok(discover.BY_ID["lastfm"]["entity_kinds"] == ("artists", "tracks")
   and discover.BY_ID["listenbrainz"]["entity_kinds"] == ("artists",)
   and discover.BY_ID["discogs"]["entity_kinds"] == (),
   "…and the registry says so: `entity_kinds` is the ROW kinds a feed really "
   "produces, not the genre `rec_kinds` it was defaulted from")
# The seed KIND is the rule's other half: a similar-tracks feed answers a track
# page, and a chart narrowed by an artist answers an artist page.
ok(discover.entity_limit(discover.BY_ID["lastfm"], "album", "tracks")[0]
   == "track pages only"
   and discover.entity_limit(discover.BY_ID["rym"], "album", "tracks")[1]
   == ("RateYourMusic charts filter by GENRE and by ARTIST — it states no "
       "similar-tracks feed")
   and discover.entity_limit(discover.BY_ID["lastfm"], "album", "albums")[0]
   == "artist & track pages only",
   "a feed that does exist but is seeded by another page says which page that "
   "is (Last.fm's similar-tracks feed, RYM's artist chart)")
# `not_applicable` is the entity shelf's own field: the genre and library
# seeds keep the payload they always had.
fresh()
genre_seed = api_discover.recommended_list(seed="shoegaze", kind="albums", limit=5)
ok("not_applicable" not in genre_seed,
   "a genre seed is not a page, so it carries no capability line")

# An artist page — the level every similar-artist feed answers — says nothing.
fresh()
ok(seed_rows(seed_kind="artist", seed_mbid=ARTIST_SLOWDIVE, seed_name="Slowdive",
             kind="artists")["not_applicable"] == [],
   "an artist page reports no capability line at all")

# A TRACK page: the artist-only feeds are information, and the sources that CAN
# answer it still do.
fresh()
track_page = seed_rows(seed_kind="track", seed_mbid=TRACK_ALISON,
                       seed_name="Alison", seed_artist="Slowdive", kind="tracks")
ok([one["id"] for one in track_page["not_applicable"]] == ["listenbrainz", "rym"]
   and not [one for one in track_page["not_applicable"]
            if one["id"] in track_page["notes"]]
   and track_page["items"],
   f"a track page reports the artist-only feeds as information and still "
   f"answers ({[o['id'] for o in track_page['not_applicable']]})")

# The same album shelf, with a provider that really failed: `failed:` stays a
# note in the provider's own words, named in `sources_asked`, and never folded
# into the capability line.
fresh()
discovery.deezer_related_artists = lambda *a, **k: (_ for _ in ()).throw(
    RuntimeError('Deezer refused: 403 "Quota exceeded"'))
failed_album = seed_rows(seed_kind="album", seed_mbid=RG_SOUVLAKI,
                         seed_name="Souvlaki", seed_artist="Slowdive",
                         kind="albums")
ok(failed_album["notes"]["deezer"] == 'failed: Deezer refused: 403 "Quota exceeded"'
   and "deezer" in failed_album["sources_asked"]
   and "deezer" not in [one["id"] for one in failed_album["not_applicable"]],
   f"a source that really failed keeps its own note on the very same shelf "
   f"({failed_album['notes']['deezer']})")

# --------------------------------------------------------------------------- #
# 5) A TRACK page: similar tracks when the key is there, the artist's own top
#    tracks when it is not — and an owned track kept with its file path
# --------------------------------------------------------------------------- #
print("== a track page ==")
fresh()
unkeyed = seed_rows(seed_kind="track", seed_mbid=TRACK_ALISON, seed_name="Alison",
                    seed_artist="Slowdive", kind="tracks")
tracks = by_title(unkeyed)
ok(set(tracks) == {"Sweetness and Light", "Alison", "Machine Gun", "Soon"},
   f"Deezer's related-top-tracks bridge and MusicBrainz's two track answers "
   f"fill the shelf unkeyed ({sorted(tracks)})")
ok(tracks["Alison"]["owned"] is True
   and tracks["Alison"]["path"] == "C:/Music/Slowdive/Souvlaki/01 Alison.flac",
   f"a track the library holds keeps its FILE path ({tracks['Alison']['path']})")
ok(tracks["Sweetness and Light"]["owned"] is False
   and tracks["Sweetness and Light"]["path"] is None,
   "a track it does not hold links nowhere")
ok(tracks["Alison"]["source"] == "musicbrainz"
   and tracks["Alison"]["reason"] == "more recordings by Slowdive (MusicBrainz)"
   and "deezer" in tracks["Alison"]["also_from"],
   f"the recordings MusicBrainz files under the artist are a labelled row of "
   f"their own, one row with Deezer's ({tracks['Alison']['reason']})")
ok(tracks["Soon"]["reason"] == "genre: dream pop (MusicBrainz)",
   f"the recording's OWN genre drives the tag search, not its artist's "
   f"({tracks['Soon']['reason']})")
ok(unkeyed["notes"]["lastfm"] == "skipped: no lastfm_api_key"
   and "musicbrainz" not in unkeyed["notes"]
   and [one["id"] for one in unkeyed["not_applicable"]] == ["listenbrainz", "rym"],
   "a keyed source without its key is still skipped by name, while the "
   "artist-only feeds are information")

# A recording that states no genres of its own falls back to its artist's — the
# page still gets its genre rows rather than a blanket skip.
fresh()
fallback = seed_rows(seed_kind="track", seed_mbid=TRACK_MACHINE_GUN,
                     seed_name="Machine Gun", seed_artist="Slowdive", kind="tracks")
ok(any(c[0] == "mb_get_cached" and c[1] == "recording/%s" % TRACK_MACHINE_GUN
       for c in CALLS)
   and ("mb_get_cached", "artist/%s" % ARTIST_SLOWDIVE) in
       [(c[0], c[1]) for c in CALLS]
   and "musicbrainz" not in fallback["notes"],
   "a recording stating no genres of its own falls back to its artist's")

CFG["lastfm_api_key"] = "a-key"
keyed = seed_rows(seed_kind="track", seed_mbid=TRACK_ALISON, seed_name="Alison",
                  seed_artist="Slowdive", kind="tracks")
keyed_rows = by_title(keyed)
ok(keyed_rows["Vapour Trail"]["source"] == "lastfm"
   and keyed_rows["Vapour Trail"]["reason"] == "sounds like Alison (Last.fm)"
   and keyed_rows["Vapour Trail"]["score"] == 0.92,
   f"a keyed Last.fm answers a track seed with its own match "
   f"({keyed_rows['Vapour Trail']['score']})")
ok(keyed_rows["Machine Gun"]["source"] == "musicbrainz"
   and sorted(keyed_rows["Machine Gun"]["also_from"]) == ["deezer", "lastfm"]
   and len([r for r in keyed["items"] if r["title"] == "Machine Gun"]) == 1,
   "the same track from three sources is one row with the others named")
ok("lastfm" not in keyed["notes"],
   "a keyed source that answered carries no note")
CFG.pop("lastfm_api_key", None)

# --------------------------------------------------------------------------- #
# 6) Spotify on an entity shelf: a NAMED artist's albums and top tracks — never
#    a similarity feed (its own related-artists/recommendations endpoints are
#    closed to apps created after 2024-11-27), and never a hit whose name is
#    not the name asked for
# --------------------------------------------------------------------------- #
print("== Spotify on an entity shelf ==")
ok(discover.BY_ID["spotify"]["entity_kinds"] == ("albums", "tracks")
   and discover.BY_ID["spotify"]["rec_kinds"] == ("albums",)
   and discover.BY_ID["itunes"]["entity_kinds"] == ("albums",),
   f"the registry separates what a source recommends for a GENRE from what it "
   f"can say about an ENTITY "
   f"({discover.BY_ID['spotify']['rec_kinds']} vs "
   f"{discover.BY_ID['spotify']['entity_kinds']})")
fresh()
CFG["spotify_client_id"] = "an-id"
CFG["spotify_client_secret"] = "a-secret"
intg._spotify_token = lambda cfg=None, timeout=None: "a-token"
spotify_albums = seed_rows(seed_kind="album", seed_mbid=RG_SOUVLAKI,
                           seed_name="Souvlaki", seed_artist="Slowdive",
                           kind="albums")
albums = by_title(spotify_albums)
ok("spotify" not in spotify_albums["notes"]
   and "spotify" in albums["Pygmalion"]["also_from"],
   f"a configured Spotify answers an album shelf with the artist's own "
   f"records ({albums['Pygmalion']['also_from']})")
calls = [c for c in CALLS if c[0] == "json" and "api.spotify.com" in c[1]]
ok([c[1] for c in calls] == ["https://api.spotify.com/v1/search",
                             "https://api.spotify.com/v1/artists/sp-slowdive/albums"],
   f"one artist search, then that artist's albums ({[c[1] for c in calls]})")
ok(calls[-1][2].get("include_groups") == "album"
   and calls[-1][2].get("market") == "US",
   f"singles are left out on Spotify's side, and the app's own region setting "
   f"is the market ({calls[-1][2]})")
fresh()
CFG["spotify_client_id"] = "an-id"
CFG["spotify_client_secret"] = "a-secret"
CFG["cover_country"] = "gb"
intg._spotify_token = lambda cfg=None, timeout=None: "a-token"
seed_rows(seed_kind="album", seed_mbid=RG_SOUVLAKI, seed_name="Souvlaki",
          seed_artist="Slowdive", kind="albums")
ok([c for c in CALLS if c[0] == "json" and c[1].endswith("/albums")
    and "api.spotify.com" in c[1]][-1][2]["market"] == "GB",
   "the configured region is sent as an upper-case market code")
CFG.pop("cover_country", None)

fresh()
CFG["spotify_client_id"] = "an-id"
CFG["spotify_client_secret"] = "a-secret"
intg._spotify_token = lambda cfg=None, timeout=None: "a-token"
spotify_tracks = seed_rows(seed_kind="track", seed_mbid=TRACK_ALISON,
                           seed_name="Alison", seed_artist="Slowdive",
                           kind="tracks")
tracks = by_title(spotify_tracks)
ok([c[1] for c in CALLS if c[0] == "json" and "api.spotify.com" in c[1]]
   == ["https://api.spotify.com/v1/search",
       "https://api.spotify.com/v1/artists/sp-slowdive/top-tracks"]
   and "spotify" in tracks["Alison"]["also_from"],
   f"a track shelf reads the artist's top tracks — its own popularity rides "
   f"along as the score ({tracks['Alison']['also_from']})")

fresh()
CFG["spotify_client_id"] = "an-id"
CFG["spotify_client_secret"] = "a-secret"
intg._spotify_token = lambda cfg=None, timeout=None: "a-token"
artist_shelf = seed_rows(seed_kind="artist", seed_mbid=ARTIST_SLOWDIVE,
                         seed_name="Slowdive", kind="artists")
ok("spotify" not in artist_shelf["sources_asked"]
   and "itunes" not in artist_shelf["sources_asked"],
   f"an ARTIST shelf is left to the sources that publish a related feed "
   f"({artist_shelf['sources_asked']})")

fresh()
CFG["spotify_client_id"] = "an-id"
CFG["spotify_client_secret"] = "a-secret"
intg._spotify_token = lambda cfg=None, timeout=None: "a-token"
unknown = seed_rows(seed_kind="album", seed_mbid=RG_SOUVLAKI, seed_name="Souvlaki",
                    seed_artist="Lush", kind="albums")
ok(unknown["notes"]["spotify"] == 'skipped: Spotify knows no artist called "Lush"'
   and not [c for c in CALLS if c[0] == "json" and "api.spotify.com" in c[1]
            and not c[1].endswith("/search")],
   f"a search hit whose own name is not the name asked for is no hit at all "
   f"({unknown['notes']['spotify']})")
CFG.pop("spotify_client_id", None)
CFG.pop("spotify_client_secret", None)

# --------------------------------------------------------------------------- #
# 6b) RateYourMusic — an artist's OWN ranked songs, and nothing else
# --------------------------------------------------------------------------- #
print("== RateYourMusic as an entity source ==")
# `integrations.rym_chart_rows`' own row shape, as the scrape states it.
RYM_SONGS = [
    {"kind": "track", "title": "Star Roving", "artist": "Slowdive",
     "link": "https://rateyourmusic.com/song/slowdive/star-roving/",
     "rym_path": "/song/slowdive/star-roving/", "rank": 1,
     "popularity": None, "popularity_label": None, "source": "rym"},
]


def stub_rym(rows=None, chart="Best Slowdive songs of all time", error=None):
    """The RateYourMusic scrape seam. That SCRAPE is pinned by
    `tools/test_discover.py` and `tools/test_rym_archive.py`; what is pinned
    here is what an entity SHELF does with it."""
    payload = {"chart": chart, "total": None,
               "rows": RYM_SONGS if rows is None else rows}

    def fake(kind="tracks", period="all", limit=50, cfg=None, now=None,
             genre="", artist=""):
        CALLS.append(("rym_charts", kind, period, genre, artist))
        if error:
            raise error
        return payload
    intg.rym_charts = fake


def rym_calls():
    return [call for call in CALLS if call[0] == "rym_charts"]


fresh()
CFG["rym_archive_fallback"] = True
stub_rym()
artist_tracks = seed_rows(seed_kind="artist", seed_mbid=ARTIST_SLOWDIVE,
                          seed_name="Slowdive", kind="tracks")
ok(rym_calls() == [("rym_charts", "tracks", "all", "", "Slowdive")],
   f"an artist page asks RateYourMusic for THAT ARTIST's own chart "
   f"({rym_calls()})")
rym_rows = [row for row in artist_tracks["items"] if row["source"] == "rym"]
ok(rym_rows and all(set(row) == ROW_KEYS for row in rym_rows)
   and rym_rows[0]["reason"] == "more from Slowdive (RateYourMusic chart)",
   f"…and its rows arrive in the shelf's shape, naming the relationship they "
   f"state ({rym_rows[0]['reason'] if rym_rows else None})")

# A chart that came back withOUT the artist filter names other artists: it is
# not "more from this page", so it is refused rather than relabelled.
fresh()
stub_rym(rows=[{"kind": "track", "title": "Teardrop", "artist": "Massive Attack",
                "link": "https://rateyourmusic.com/song/massive-attack/teardrop/",
                "rym_path": "/song/massive-attack/teardrop/", "rank": 1,
                "popularity": None, "popularity_label": None, "source": "rym"}])
wrong_artist = seed_rows(seed_kind="artist", seed_mbid=ARTIST_SLOWDIVE,
                         seed_name="Slowdive", kind="tracks")
ok(not [row for row in wrong_artist["items"] if row["source"] == "rym"]
   and wrong_artist["notes"]["rym"].startswith(
       'skipped: RateYourMusic knows no artist called "Slowdive"'),
   f"a chart naming other artists is not 'more from this page' "
   f"({wrong_artist['notes'].get('rym')})")

# RYM's chart filters are a genre and an ARTIST, so a page that is neither is
# told its own limit instead of being answered with somebody else's rows.
fresh()
stub_rym()
album_tracks = seed_rows(seed_kind="album", seed_mbid=RG_SOUVLAKI,
                         seed_name="Souvlaki", seed_artist="Slowdive",
                         kind="tracks")
ok(rym_calls() == []
   and [one for one in album_tracks["not_applicable"] if one["id"] == "rym"]
   and album_tracks["not_applicable"][-1]["why"].startswith(
       "RateYourMusic charts filter by GENRE and by ARTIST")
   and "rym" not in album_tracks["notes"],
   f"an album page's track shelf states RYM's own limit as information "
   f"({album_tracks['not_applicable'][-1]['why']})")
CFG.pop("rym_archive_fallback", None)

# --------------------------------------------------------------------------- #
# 7) The merge rule itself: an mbid is an identity, and a reason is never lost
# --------------------------------------------------------------------------- #
print("== merging two sources ==")
shared = [
    {"kind": "artist", "title": "Lush", "artist": "Lush", "mbid": ARTIST_LUSH,
     "_source": "deezer", "_reason": "sounds like Slowdive (Deezer)"},
    {"kind": "artist", "title": "Lush", "artist": "Lush", "mbid": ARTIST_LUSH,
     "_source": "listenbrainz", "_reason": "sounds like Slowdive (ListenBrainz)"},
]
merged = discover.merge_rows(shared)
ok(len(merged) == 1 and merged[0]["_source"] == "deezer"
   and merged[0]["_reason"] == "sounds like Slowdive (Deezer)"
   and merged[0]["_also"] == ["listenbrainz"],
   "two sources sharing an MBID are ONE row, the preferred source's reason kept")
quiet = [
    {"kind": "artist", "title": "Broadcast", "artist": "Broadcast",
     "mbid": ARTIST_BROADCAST, "_source": "deezer"},
    {"kind": "artist", "title": "Broadcast", "artist": "Broadcast",
     "mbid": ARTIST_BROADCAST, "_source": "listenbrainz",
     "_reason": "sounds like Slowdive (ListenBrainz)"},
]
ok(discover.merge_rows(quiet)[0]["_reason"] == "sounds like Slowdive (ListenBrainz)",
   "a row whose winner stated no reason takes the duplicate's rather than none")

# --------------------------------------------------------------------------- #
# 7) The DISCOVER page's own call, unchanged
# --------------------------------------------------------------------------- #
print("== the existing callers ==")
fresh(search=lambda entity, q, limit, offset: {
    "rows": [{"id": "22222222-2222-2222-2222-222222222222", "score": 100,
              "title": "Loveless", "artist": "My Bloody Valentine",
              "artist_mbid": "5", "first_release_date": "1991-11-04"}],
    "total": 1, "offset": offset, "next": None, "query": q})
library_rec = api_discover.recommended_list(seed="library", kind="albums", limit=5)
ok(set(library_rec) == {"items", "sources_asked", "notes", "basis"}
   and library_rec["items"]
   and all(set(row) == ROW_KEYS for row in library_rec["items"])
   and library_rec["basis"].startswith("library genres:"),
   f"seed=library answers the same payload and row shape as before "
   f"({library_rec['basis']})")
ok(all(row["owned"] is False for row in library_rec["items"]),
   "…and still drops what the library already holds (the shopping-list rule)")
ok(discover.recommended_payload(cfg={}, seed_kind="", lib=LIBRARY)["basis"].startswith(
    "library genres:"),
   "an empty seed_kind is the library seed, exactly as the old call sent it")

route_params = set(inspect.signature(api_discover.discover_recommended).parameters)
ok({"seed_kind", "seed_mbid", "seed_name", "seed_artist"} <= route_params,
   f"the HTTP route takes the entity seed as query arguments ({sorted(route_params)})")
ok({r.path for r in api_discover.router.routes}
   == {"/api/discover/genres", "/api/discover/genre", "/api/discover/recommended",
       "/api/discover/charts"},
   "the router still serves exactly the four documented paths")
for call, why in ((lambda: api_discover.recommended_list(seed_kind="film"),
                   "seed_kind"),
                  (lambda: api_discover.recommended_list(seed_kind="artist",
                                                         kind="films"),
                   "kind")):
    try:
        call()
    except HTTPException as exc:
        ok(exc.status_code == 400, f"an invalid {why} is a 400, not a shrug")
    else:
        raise AssertionError(f"an invalid {why} was accepted")

print(f"\nAll {passed} checks passed.")
