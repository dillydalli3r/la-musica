#!/usr/bin/env python3
"""Discover (/api/discover/*) — the source registry, the genre list, genre
browsing and recommendations, all offline.

What this pins, with every provider seam stubbed (no network at all):

  * the registry answers the right KINDS: MusicBrainz and Deezer list all
    three, iTunes/Discogs/Spotify albums only, ListenBrainz lists nothing but
    recommends, and the description sources (TheAudioDB/Wikidata/Wikipedia)
    declare no list kind at all;
  * a credentialed source without its key is `skipped: no <key>` in `notes`,
    and is never asked;
  * `genres` merges the library's own list with the online ones
    case-insensitively, keeps the library's counts, and reports the online-only
    genres with zeroes and their own source;
  * `genre` merges the SAME album named by two sources into one row (primary
    source + `also_from`), marks what the library already holds (with its
    path), reports a failing source in `notes`, and pages with `next_offset`;
  * `recommended` drops what the library owns and records its `basis` and each
    row's `reason`;
  * an unknown genre or source is an EMPTY list with a note, never an error,
    and never a made-up row;
  * every row, in every endpoint, carries the ONE documented row shape.

Run:  python tools/test_discover.py
"""
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
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok: {label}")


ROW_KEYS = {"kind", "title", "artist", "year", "source", "source_label",
            "cover_url", "page_url", "mbid", "release_group_mbid", "path",
            "owned", "in_library", "tracks", "score", "reason", "also_from"}

CALLS = []


# --------------------------------------------------------------------------- #
# The seams. `discovery._json` is the ONE transport every public provider
# wrapper goes through, so a router over it covers Deezer, Apple, Last.fm,
# Discogs, Spotify and ListenBrainz Labs; MusicBrainz is reached through
# `integrations.search_mb` / `mb_get_cached` (its own cached, throttled access).
# --------------------------------------------------------------------------- #
def routes(rules):
    """A transport router: the rules are tried IN ORDER, most specific first
    (Deezer's `/artist/<id>` is a prefix of its `/artist/<id>/related`), and an
    unmatched url answers nothing — which is what a provider that has no such
    endpoint does."""
    def fake(url, params):
        for fragment, answer in rules:
            if fragment in url:
                return answer(url, params) if callable(answer) else answer
        return None
    return fake


def stub_json(router=None):
    def fake(url, params=None, headers=None, timeout=None, ttl=None, host=None):
        CALLS.append(("json", url, dict(params or {})))
        return router(url, params or {}) if router else None
    discovery._json = fake


def stub_mb(search=None, taxonomy=None):
    """The MusicBrainz seams: `search_mb` (the tag/genre search) and
    `mb_get_cached` (the cached `/genre/all` page)."""
    def fake_search(entity, query, limit=100, mode="free", offset=0, **kw):
        CALLS.append(("search_mb", entity, query))
        return search(entity, query, limit, offset) if callable(search) \
            else (search or {"rows": [], "total": 0})
    intg.search_mb = fake_search

    def fake_cached(endpoint, params=None, **kw):
        CALLS.append(("mb_get_cached", endpoint, dict(params or {})))
        return taxonomy(url_endpoint=endpoint, params=params or {}) \
            if callable(taxonomy) else taxonomy
    intg.mb_get_cached = fake_cached


def mb_rows(kind, rows, total=None):
    """A `search_mb` answer for one entity kind, in its own row shape."""
    query = {"albums": "release-group", "artists": "artist", "tracks": "recording"}[kind]
    return lambda entity, q, limit, offset: (
        CALLS.append(("search_mb_query", entity, q)) or
        {"rows": list(rows), "total": len(rows) if total is None else total,
         "offset": offset, "next": None, "query": q})


# --------------------------------------------------------------------------- #
# The fixture library: Slowdive/Souvlaki and Ride/Nowhere, both owned; two
# genres (shoegaze, Dream Pop) across three tracks.
# --------------------------------------------------------------------------- #
RG_SOUVLAKI = "11111111-1111-1111-1111-111111111111"
RG_LOVELESS = "22222222-2222-2222-2222-222222222222"
RG_NOWHERE = "33333333-3333-3333-3333-333333333333"
TRACK_ALISON = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

LIBRARY = {"folder": "C:/Music", "artists": [
    {"path": "C:/Music/Slowdive", "name": "Slowdive", "aggregate": {}, "albums": [
        {"path": "C:/Music/Slowdive/Souvlaki",
         "meta": {"ALBUM": "Souvlaki", "ALBUMARTIST": "Slowdive", "DATE": "1993",
                  "MUSICBRAINZ_RELEASEGROUPID": RG_SOUVLAKI},
         "tracks": [
             {"path": "C:/Music/Slowdive/Souvlaki/01 Alison.flac",
              "tags": {"TITLE": "Alison", "ARTIST": "Slowdive",
                       "GENRE": ["shoegaze", "Dream Pop"],
                       "MUSICBRAINZ_TRACKID": TRACK_ALISON,
                       "MUSICBRAINZ_ALBUMARTISTID": "44444444-4444-4444-4444-444444444444"}},
             {"path": "C:/Music/Slowdive/Souvlaki/02 Machine Gun.flac",
              "tags": {"TITLE": "Machine Gun", "ARTIST": "Slowdive",
                       "GENRE": "Shoegaze"}},
         ]}]},
    {"path": "C:/Music/Ride", "name": "Ride", "aggregate": {}, "albums": [
        {"path": "C:/Music/Ride/Nowhere",
         "meta": {"ALBUM": "Nowhere", "ALBUMARTIST": "Ride", "DATE": "1990",
                  "MUSICBRAINZ_RELEASEGROUPID": RG_NOWHERE},
         "tracks": [
             {"path": "C:/Music/Ride/Nowhere/01 Vapour Trail.flac",
              "tags": {"TITLE": "Vapour Trail", "ARTIST": "Ride", "GENRE": "Shoegaze"}},
         ]}]},
]}

CFG = {"music_folder": "C:/Music", "discovery_enabled": True}

# The library payload is passed in (and, for the route-level calls, served by
# the cached builder's own seam), so nothing here scans a disk.
discover._library = lambda cfg, lib=None: lib if lib is not None else LIBRARY
api_discover.load_config = lambda: dict(CFG)


def fresh(router=None, search=None, taxonomy=None):
    CALLS.clear()
    discovery._CACHE.clear()
    discovery._MB_GENRE_ROWS.clear()
    discovery._MB_GENRE_STATE.clear()
    discovery._MB_GENRE_PAGES.clear()
    discovery._HOST_WARNED.clear()
    stub_json(router)
    stub_mb(search=search, taxonomy=taxonomy)


# --------------------------------------------------------------------------- #
# 1) The registry: one place, and it says what each source can really do
# --------------------------------------------------------------------------- #
print("== the source registry ==")
cat = discover.catalogue(cfg={})
sources = {spec["id"]: spec for spec in cat["sources"]}
ok(cat["kinds"] == list(discover.KINDS) == ["albums", "artists", "tracks"],
   "the registry names the three kinds")
ok(sources["musicbrainz"]["kinds"] == list(discover.KINDS)
   and sources["deezer"]["kinds"] == list(discover.KINDS)
   and sources["lastfm"]["kinds"] == list(discover.KINDS),
   "MusicBrainz, Deezer and Last.fm list all three kinds")
ok(sources["itunes"]["kinds"] == ["albums"] and sources["discogs"]["kinds"] == ["albums"]
   and sources["spotify"]["kinds"] == ["albums"],
   "iTunes, Discogs and Spotify list albums only")
ok(sources["listenbrainz"]["kinds"] == []
   and sorted(sources["listenbrainz"]["rec_kinds"]) == ["albums", "artists", "tracks"],
   "ListenBrainz lists nothing but recommends all three (it has no genre filter)")
ok(sources["audiodb"]["kinds"] == [] and sources["wikidata"]["kinds"] == []
   and sources["wikipedia"]["kinds"] == [],
   "the description/verification sources declare no list kind")
ok(sources["musicbrainz"]["genres"] and sources["deezer"]["genres"]
   and sources["lastfm"]["genres"] and not sources["itunes"]["genres"],
   "the genre-list sources are the three that publish one")
ok(sources["lastfm"]["missing"] == ["lastfm_api_key"]
   and not sources["lastfm"]["ready"]
   and sources["spotify"]["needs"] == ["spotify_client_id", "spotify_client_secret"],
   "a credentialed source names the key it is missing")
ok(sources["audiodb"]["note"].startswith("States the genre and mood of a NAMED")
   and "404" in sources["audiodb"]["note"],
   "TheAudioDB's row says what it CAN answer, and why it cannot list")
ok(discover.skip_note(discover.BY_ID["lastfm"], {}) == "skipped: no lastfm_api_key"
   and discover.skip_note(discover.BY_ID["musicbrainz"], {}) == "",
   "the skip note names the key, and a keyless source has none")
ok(all(spec["id"] in discover.BY_ID for spec in discover.SOURCES)
   and len(discover.SOURCES) == len(discover.BY_ID),
   "every registry id is unique and resolvable")
ok({r.path for r in api_discover.router.routes}
   == {"/api/discover/genres", "/api/discover/genre", "/api/discover/recommended",
       "/api/discover/charts"},
   "the router serves exactly the four documented paths")

# --------------------------------------------------------------------------- #
# 2) genres — the library's list merged with the online ones
# --------------------------------------------------------------------------- #
print("== genres ==")
# MusicBrainz's taxonomy names the two genres the library has, in MusicBrainz's
# own lowercase; Deezer names those two plus its own "Rock".
TAXONOMY = {"genres": [{"name": "shoegaze", "id": "g1"},
                       {"name": "dream pop", "id": "g2"}],
            "genre-count": 2}
DEEZER_GENRES = {"data": [{"id": 0, "name": "All"},
                          {"id": 152, "name": "Rock"},
                          {"id": 165, "name": "Rap/Hip Hop"},
                          {"id": 132, "name": "Dream Pop"}]}
fresh(router=routes([
    ("api.deezer.com/genre", DEEZER_GENRES),
    ("ws.audioscrobbler.com", {"tags": {"tag": [{"name": "shoegaze"},
                                                {"name": "chill"}]}}),
]), taxonomy=TAXONOMY)

payload = api_discover.genres_list(scope="all")
by_name = {row["name"]: row for row in payload["genres"]}
ok(len(payload["genres"]) == len(by_name), "no genre is listed twice")
ok(by_name["Shoegaze"]["sources"] == ["library", "musicbrainz"]
   and by_name["Dream Pop"]["sources"] == ["library", "musicbrainz", "deezer"],
   f"a genre named by three sources is ONE row listing them "
   f"({by_name['Dream Pop']['sources']})")
ok(by_name["Shoegaze"]["track_count"] == 3 and by_name["Shoegaze"]["album_count"] == 2
   and by_name["Shoegaze"]["artist_count"] == 2,
   f"the library's own counts survive the merge ({by_name['Shoegaze']})")
ok(by_name["Dream Pop"]["track_count"] == 1 and by_name["Dream Pop"]["album_count"] == 1
   and by_name["Dream Pop"]["artist_count"] == 1,
   "…and a genre on one track of one album counts once for that album and artist")
ok(by_name["Rock"]["sources"] == ["deezer"] and by_name["Rock"]["track_count"] == 0
   and by_name["Rock"]["album_count"] == 0 and by_name["Rock"]["artist_count"] == 0,
   "an online-only genre carries zero counts and the source that named it")
ok(payload["sources_asked"] == ["musicbrainz", "deezer", "lastfm"]
   and payload["notes"] == {"lastfm": "skipped: no lastfm_api_key"},
   f"only the genre-list sources are asked, and the unconfigured one is a note "
   f"({payload['sources_asked']}, {payload['notes']})")
ok(all("library" in row["sources"] for row in payload["genres"][:2])
   and payload["genres"][0]["name"] == "Dream Pop",
   f"the library's own genres lead the list, most-named first "
   f"({[row['name'] for row in payload['genres']][:3]})")

lib_only = api_discover.genres_list(scope="library")
ok(lib_only["sources_asked"] == [] and lib_only["notes"] == {}
   and {r["name"] for r in lib_only["genres"]} == {"Shoegaze", "Dream Pop"},
   "scope=library asks no provider and returns the library's genres")
online_only = api_discover.genres_list(scope="online")
ok({r["name"] for r in online_only["genres"]} == {"Shoegaze", "Dream Pop", "Rock",
                                                 "Rap/Hip Hop"}
   and all("library" not in r["sources"] for r in online_only["genres"]),
   "scope=online returns the sources' names only — no library row, no library counts")
ok(all(row["track_count"] == 0 for row in online_only["genres"]),
   "…and every online row is honest about having no counts")

# A configured Last.fm contributes its tag chart, moods dropped.
fresh(router=routes([
    ("api.deezer.com/genre", DEEZER_GENRES),
    ("ws.audioscrobbler.com", {"tags": {"tag": [{"name": "shoegaze"},
                                                {"name": "chill"}]}}),
]), taxonomy=TAXONOMY)
CFG["lastfm_api_key"] = "key"
keys = api_discover.genres_list(scope="online")
CFG.pop("lastfm_api_key")
names = {r["name"] for r in keys["genres"]}
ok("shoegaze".title() in names and "chill" not in names
   and keys["notes"] == {},
   f"Last.fm's tag chart is asked once keyed, with its mood words dropped ({sorted(names)})")

# A half-fetched MusicBrainz taxonomy says so rather than pretending.
def long_taxonomy(url_endpoint=None, params=None):
    """A taxonomy of 24 pages, one name per page — the stub answers whichever
    page is asked, so "done" needs twelve calls at two pages each."""
    offset = int((params or {}).get("offset") or 0)
    return {"genres": [{"name": "genre-%d" % offset, "id": "g%d" % offset}],
            "genre-count": 2400}


fresh(router=None, taxonomy=long_taxonomy)
partial = discover.genres_payload(cfg={}, scope="online", lib=LIBRARY)
ok(partial["notes"]["musicbrainz"].startswith("partial: 2 of 2400 genres")
   and "done" not in partial["notes"]["musicbrainz"],
   f"a partial MusicBrainz list is reported as partial ({partial['notes']['musicbrainz']})")
ok(discover.genres_payload(cfg={}, scope="online", lib=LIBRARY)["notes"]["musicbrainz"]
   .startswith("partial: 4 of 2400"),
   "…and the next call continues the taxonomy instead of starting over")

# --------------------------------------------------------------------------- #
# 3) genre — one page, per source, merged and deduped
# --------------------------------------------------------------------------- #
print("== genre browse ==")
DEEZER_CHART = {"total": 40, "data": [
    {"id": 1, "title": "Loveless", "artist": {"name": "My Bloody Valentine"},
     "cover_xl": "https://cdn.deezer/loveless.jpg", "record_type": "album",
     "link": "https://www.deezer.com/album/1"},
    {"id": 2, "title": "Souvlaki", "artist": {"name": "Slowdive"},
     "cover_xl": "https://cdn.deezer/souvlaki.jpg", "record_type": "album",
     "link": "https://www.deezer.com/album/2"},
    {"id": 3, "title": "Going Blank Again", "artist": {"name": "Ride"},
     "cover_xl": "https://cdn.deezer/gba.jpg", "record_type": "album",
     "link": "https://www.deezer.com/album/3"},
]}
DISCOGS_HITS = {"pagination": {"items": 2}, "results": [
    {"id": 99, "title": "My Bloody Valentine - Loveless", "year": 1991,
     "cover_image": "https://img.discogs/loveless.jpg", "genre": ["Rock"],
     "style": ["Shoegaze"]},
]}
MB_ALBUM_ROWS = [
    {"id": RG_LOVELESS, "score": 100, "title": "Loveless",
     "artist": "My Bloody Valentine", "artist_mbid": "5", "first_release_date": "1991-11-04"},
]
fresh(router=routes([
    ("/genre/152/artists", {"data": []}),
    ("/chart/152/albums", DEEZER_CHART),
    ("api.deezer.com/genre", DEEZER_GENRES),
    ("api.discogs.com", DISCOGS_HITS),
]), search=mb_rows("albums", MB_ALBUM_ROWS, total=400))
discovery.itunes_genre_albums = lambda *a, **k: (_ for _ in ()).throw(
    RuntimeError("Apple is down"))

page = api_discover.genre_page(genre="rock", kind="albums", limit=25)
items = page["items"]
ok(all(set(row) == ROW_KEYS for row in items),
   f"every album row carries exactly the documented shape ({sorted(set(items[0]) ^ ROW_KEYS)})")
loveless = next(r for r in items if r["title"] == "Loveless")
ok(loveless["source"] == "musicbrainz" and loveless["source_label"] == "MusicBrainz"
   and sorted(loveless["also_from"]) == ["deezer", "discogs"],
   f"the same album from three sources is ONE row, the registry's first as "
   f"primary ({loveless['source']} + {loveless['also_from']})")
ok(len([r for r in items if r["title"] == "Loveless"]) == 1,
   "and it is not repeated")
ok(loveless["mbid"] == RG_LOVELESS and loveless["release_group_mbid"] == RG_LOVELESS
   and loveless["page_url"].startswith("https://musicbrainz.org/release-group/")
   and loveless["cover_url"].startswith("https://coverartarchive.org/"),
   "the primary (MusicBrainz) row's identity and links are kept")
ok(loveless["owned"] is False and loveless["path"] is None
   and loveless["in_library"] is False,
   "an album the library does not hold is not marked owned")
souvlaki = next(r for r in items if r["title"] == "Souvlaki")
ok(souvlaki["owned"] is True and souvlaki["in_library"] is True
   and souvlaki["path"] == "C:/Music/Slowdive/Souvlaki"
   and souvlaki["tracks"] == ["Alison", "Machine Gun"],
   f"an album the library holds is owned, with its path and tracklist "
   f"({souvlaki['path']})")
gba = next(r for r in items if r["title"] == "Going Blank Again")
ok(gba["owned"] is False and gba["in_library"] is True,
   "an album by an artist the library collects is in_library but not owned")
ok(page["notes"]["itunes"].startswith("failed: Apple is down"),
   f"a failing source is reported in notes, never swallowed ({page['notes']['itunes']})")
ok(page["notes"]["lastfm"] == "skipped: no lastfm_api_key"
   and page["notes"]["spotify"].startswith("skipped: no spotify_client_id"),
   "…and so is every unconfigured one")
ok(page["sources_asked"] == ["musicbrainz", "deezer", "itunes", "lastfm",
                            "discogs", "spotify"],
   f"every source that can list albums was asked, in registry order "
   f"({page['sources_asked']})")
ok(page["next_offset"] == 25,
   f"a full page with more behind it offers next_offset ({page['next_offset']})")

one = api_discover.genre_page(genre="rock", kind="albums", source="musicbrainz",
                                 limit=1)
ok(one["sources_asked"] == ["musicbrainz"] and len(one["items"]) == 1
   and one["notes"] == {},
   "source=<id> asks that source alone")
ok(one["items"][0]["tracks"] == [],
   "…and a row the merge never touched still carries the row shape")

# A genre no source knows: every source answers nothing (or says why it
# cannot), and the page is an empty list rather than an error.
fresh(router=routes([("api.deezer.com/genre", DEEZER_GENRES)]),
      search=mb_rows("albums", [], total=0))
discovery.itunes_genre_albums = lambda *a, **k: {"rows": [], "total": None}
unknown = api_discover.genre_page(genre="not-a-genre-at-all", kind="albums")
ok(unknown["items"] == [] and unknown["next_offset"] is None
   and unknown["notes"]["deezer"].startswith("skipped: ")
   and unknown["notes"]["lastfm"] == "skipped: no lastfm_api_key"
   and "itunes" not in unknown["notes"],
   f"an unknown genre is an empty list plus honest notes, never an error "
   f"({unknown['notes']})")
missing = api_discover.genre_page(genre="rock", kind="albums", source="nope")
ok(missing["items"] == [] and missing["notes"] == {"nope": "unknown source"}
   and missing["sources_asked"] == [],
   "an unknown source is an empty list that says so")
real = api_discover.genre_page(genre="rock", kind="albums", source="audiodb")
ok(real["items"] == [] and real["notes"]["audiodb"].startswith("cannot list albums:")
   and "404" in real["notes"]["audiodb"],
   "a real source that cannot list a kind answers with the registry's own reason")

# artists and tracks answer the same shape from the same registry.
fresh(router=routes([("api.deezer.com/genre", DEEZER_GENRES)]),
      search=mb_rows("artists", [{"id": "66666666-6666-6666-6666-666666666666",
                                 "score": 99, "title": "My Bloody Valentine",
                                 "country": "GB", "disambiguation": ""}]))
artists = api_discover.genre_page(genre="shoegaze", kind="artists")
ok(artists["items"] and set(artists["items"][0]) == ROW_KEYS
   and artists["items"][0]["kind"] == "artist"
   and artists["items"][0]["release_group_mbid"] is None,
   "an artist row carries the same shape, with no release group")
ok("listenbrainz" not in artists["sources_asked"]
   and "itunes" not in artists["sources_asked"]
   and artists["notes"]["deezer"].startswith("skipped: "),
   "a source that cannot list artists is never asked, and Deezer's own genre list "
   "is the reason it abstains")

fresh(router=routes([("api.deezer.com/genre", DEEZER_GENRES)]),
      search=mb_rows("tracks", [{"id": TRACK_ALISON, "score": 88, "title": "Alison",
                                 "artist": "Slowdive", "first_release_date": "1993-05-17",
                                 "length": 220000}]))
tracks = api_discover.genre_page(genre="shoegaze", kind="tracks")
ok(tracks["items"] and set(tracks["items"][0]) == ROW_KEYS
   and tracks["items"][0]["owned"] is True
   and tracks["items"][0]["path"] == "C:/Music/Slowdive/Souvlaki/01 Alison.flac",
   "a track the library holds is matched by its recording MBID and carries its file path")

# 4) recommended — seeded, filtered, and honest about its basis
# --------------------------------------------------------------------------- #
print("== recommended ==")
DEEZER_RELATED = {"data": [
    {"id": 7, "name": "My Bloody Valentine", "picture_xl": "https://cdn.deezer/mbv.jpg",
     "nb_fan": 900000, "link": "https://www.deezer.com/artist/7"},
    {"id": 10, "name": "Lush", "picture_xl": "https://cdn.deezer/lush.jpg",
     "nb_fan": 250000, "link": "https://www.deezer.com/artist/10"},
    {"id": 8, "name": "Slowdive", "picture_xl": "https://cdn.deezer/slowdive.jpg",
     "nb_fan": 400000, "link": "https://www.deezer.com/artist/8"},
    {"id": 9, "name": "Ride", "picture_xl": "https://cdn.deezer/ride.jpg",
     "nb_fan": 300000, "link": "https://www.deezer.com/artist/9"},
]}
# A seed artist's OWN discography: one album the library holds (dropped), one
# single (filtered by albums_only) and one it does not have (the candidate).
DEEZER_ARTIST_ALBUMS = {"data": [
    {"id": 2, "title": "Souvlaki", "artist": {"name": "Slowdive"},
     "cover_xl": "https://cdn.deezer/souvlaki.jpg", "record_type": "album",
     "release_date": "1993-05-17", "link": "https://www.deezer.com/album/2"},
    {"id": 5, "title": "Just For A Day", "artist": {"name": "Slowdive"},
     "cover_xl": "https://cdn.deezer/jfad.jpg", "record_type": "single",
     "release_date": "1991-09-01", "link": "https://www.deezer.com/album/5"},
    {"id": 6, "title": "Pygmalion", "artist": {"name": "Slowdive"},
     "cover_xl": "https://cdn.deezer/pygmalion.jpg", "record_type": "album",
     "release_date": "1995-02-06", "link": "https://www.deezer.com/album/6"},
]}
DEEZER_ARTIST_DETAIL = {"id": 8, "name": "Slowdive", "nb_fan": 400000,
                        "picture_xl": "https://cdn.deezer/slowdive.jpg",
                        "link": "https://www.deezer.com/artist/8"}
LB_SIMILAR = [
    {"artist_mbid": "44444444-4444-4444-4444-444444444444", "name": "Slowdive",
     "comment": "", "score": 12000},
    {"artist_mbid": "77777777-7777-7777-7777-777777777777", "name": "Cocteau Twins",
     "comment": "Scottish band", "score": 9000},
]
LB_CHART = {"payload": {"releases": [
    {"release_name": "Souvlaki", "artist_name": "Slowdive", "artist_mbids": [],
     "release_mbid": "99999999-9999-9999-9999-999999999999",
     "caa_release_mbid": "99999999-9999-9999-9999-999999999999",
     "listen_count": 900000},
    {"release_name": "Loveless", "artist_name": "My Bloody Valentine",
     "artist_mbids": [], "release_mbid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
     "caa_release_mbid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
     "listen_count": 800000},
    {"release_name": "Heaven or Las Vegas", "artist_name": "Cocteau Twins",
     "artist_mbids": [], "release_mbid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
     "caa_release_mbid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
     "listen_count": 700000},
]}}
MB_ARTIST_ROWS = [
    {"id": "66666666-6666-6666-6666-666666666666", "score": 99,
     "title": "My Bloody Valentine", "country": "GB", "disambiguation": ""},
    {"id": "44444444-4444-4444-4444-444444444444", "score": 98,
     "title": "Slowdive", "country": "GB", "disambiguation": ""},
    {"id": "55555555-5555-5555-5555-555555555555", "score": 90, "title": "Ride",
     "country": "GB", "disambiguation": ""},
]


def rec_stubs(search_rows, taxonomy=None):
    """Every recommendation source's own answer, all through the seams."""
    fresh(router=routes([
        ("labs.api.listenbrainz.org", LB_SIMILAR),
        ("stats/sitewide/releases", LB_CHART),
        ("/artist/8/related", DEEZER_RELATED),
        ("/artist/8/albums", DEEZER_ARTIST_ALBUMS),
        ("/artist/8", DEEZER_ARTIST_DETAIL),
        ("/genre/132/artists", {"data": []}),
        ("/genre/152/artists", {"data": []}),
        ("/chart/152/albums", {"total": 1, "data": []}),
        ("/chart/152/tracks", {"data": []}),
        ("search/artist", lambda url, params: {"data": [
            {"id": 8, "name": params.get("q") or "Slowdive",
             "picture_xl": None, "nb_fan": 1}]}),
        ("api.deezer.com/genre", DEEZER_GENRES),
        ("api.discogs.com", DISCOGS_HITS),
    ]), search=search_rows, taxonomy=taxonomy or TAXONOMY)
    discovery.itunes_genre_albums = lambda *a, **k: {"rows": [], "total": None}
    intg._spotify_token = lambda cfg=None, timeout=None: None
    intg.mb_get_cached = lambda endpoint, params=None, **kw: TAXONOMY
    discovery.resolve_artist_mbid = lambda name, cfg=None, timeout=None: (
        "44444444-4444-4444-4444-444444444444" if name == "Slowdive" else None)


# kind=artists: a genre feed, two similar-artist feeds, and the library's own
# holdings dropped from all of them.
rec_stubs(lambda entity, q, limit, offset: {
    "rows": MB_ARTIST_ROWS, "total": len(MB_ARTIST_ROWS), "offset": offset})
artists_rec = api_discover.recommended_list(seed="library", kind="artists")
titles = [row["title"] for row in artists_rec["items"]]
ok(artists_rec["items"] and all(set(row) == ROW_KEYS for row in artists_rec["items"]),
   "recommended rows carry the same one shape")
ok(not any(row["owned"] for row in artists_rec["items"])
   and "Slowdive" not in titles and "Ride" not in titles,
   f"what the library already owns is dropped, not flagged ({titles})")
ok("My Bloody Valentine" in titles and "Cocteau Twins" in titles
   and "Lush" in titles,
   f"the similar-artist feeds contribute the artists it does not own ({titles})")
by_title = {row["title"]: row for row in artists_rec["items"]}
ok(by_title["Lush"]["reason"].startswith("sounds like Slowdive (Deezer)")
   and by_title["Cocteau Twins"]["reason"].startswith("sounds like Slowdive (ListenBrainz)")
   and by_title["My Bloody Valentine"]["reason"] == "genre: Shoegaze (MusicBrainz)",
   f"each row records WHY it was suggested, its primary source's reason kept "
   f"({[r['reason'] for r in artists_rec['items']]})")
ok(by_title["My Bloody Valentine"]["also_from"] == ["deezer"],
   "a row two sources named lists the other one in also_from")
ok(artists_rec["basis"].startswith("library genres: Shoegaze")
   and artists_rec["basis"].endswith("top artists: Slowdive, Ride"),
   f"the basis names the seed the rows came from ({artists_rec['basis']})")
ok(artists_rec["sources_asked"] == ["musicbrainz", "deezer", "lastfm",
                                   "listenbrainz"]
   and not {"itunes", "discogs", "spotify"} & set(artists_rec["sources_asked"])
   and artists_rec["notes"] == {"lastfm": "skipped: no lastfm_api_key"},
   f"a source that cannot recommend this kind is never asked, and a non-empty "
   f"answer apologises for nothing ({artists_rec['notes']})")
ok(all(row["path"] is None for row in artists_rec["items"]),
   "a recommendation links nowhere in the library — it is not owned")

# kind=albums: an artist's own discography, its singles filtered, and the
# sitewide chart that is a recommendation because it says so.
rec_stubs(mb_rows("albums", MB_ALBUM_ROWS, total=1))
albums_rec = api_discover.recommended_list(seed="library", kind="albums")
album_titles = [row["title"] for row in albums_rec["items"]]
album_reasons = {row["reason"] for row in albums_rec["items"]}
ok("Souvlaki" not in album_titles and "Just For A Day" not in album_titles,
   f"an owned album and a single are both out ({album_titles})")
ok(set(album_titles) == {"Loveless", "Pygmalion", "Heaven or Las Vegas"},
   f"the artist's own records and the sitewide chart both contribute "
   f"({album_titles})")
ok("more from Slowdive (Deezer)" in album_reasons
   and "genre: Shoegaze (MusicBrainz)" in album_reasons
   and "most listened this month (ListenBrainz)" in album_reasons,
   f"…and the album feeds record which one suggested what ({sorted(album_reasons)})")
by_album = {row["title"]: row for row in albums_rec["items"]}
ok(len(by_album) == len(albums_rec["items"])
   and sorted(by_album["Loveless"]["also_from"]) == ["discogs", "listenbrainz"],
   f"the same album from three sources is still one row ({by_album['Loveless']})")

# A genre seed: MusicBrainz answers, and the sources that cannot filter by a
# genre say why instead of returning their sitewide charts.
rec_stubs(mb_rows("tracks", [
    {"id": "88888888-8888-8888-8888-888888888888", "score": 70, "title": "Soon",
     "artist": "My Bloody Valentine", "first_release_date": "1990-01-01",
     "length": 400000}]))
seeded = api_discover.recommended_list(seed="shoegaze", kind="tracks", limit=5)
ok(seeded["basis"] == "genre: shoegaze" and seeded["items"]
   and seeded["items"][0]["reason"] == "genre: shoegaze (MusicBrainz)",
   f"a genre seed records the genre as its basis and reason ({seeded['basis']})")
ok(seeded["notes"]["listenbrainz"].startswith("skipped: ListenBrainz has no keyless")
   and seeded["notes"]["deezer"].startswith("skipped: Deezer has no genre called"),
   f"a source that cannot filter by genre says so ({seeded['notes']['listenbrainz']})")

# Nothing to suggest is SAID, never invented.
fresh(router=None, search={"rows": [], "total": 0})
empty = api_discover.recommended_list(seed="ambient dub techno", kind="albums")
ok(empty["items"] == []
   and empty["notes"]["recommended"].startswith("no recommendation source had"),
   f"an empty recommendation is empty and explains itself "
   f"({empty['notes']['recommended']})")
empty_lib = discover.recommended_payload(cfg={}, seed="library", kind="albums",
                                        lib={"folder": "C:/Music", "artists": []})
ok(empty_lib["items"] == [] and empty_lib["sources_asked"] == []
   and empty_lib["notes"]["recommended"].startswith("skipped: the library has no"),
   "an empty library is refused as a seed rather than guessed at")

# --------------------------------------------------------------------------- #
# 5) The query bounds a browse UI cannot exceed
# --------------------------------------------------------------------------- #
print("== route validation ==")
for call, why in (
        (lambda: api_discover.genres_list(scope="everything"), "scope"),
        (lambda: api_discover.genre_page(genre="rock", kind="films"), "kind"),
        (lambda: api_discover.genre_page(genre="rock", offset=5000), "offset"),
        (lambda: api_discover.recommended_list(kind="films"), "recommended kind")):
    try:
        call()
    except HTTPException as exc:
        ok(exc.status_code == 400, f"an invalid {why} is a 400, not a shrug")
    else:
        raise AssertionError(f"an invalid {why} was accepted")

print(f"\nAll {passed} checks passed.")
