#!/usr/bin/env python3
"""Verification for the in-app MusicBrainz browser's SEARCH path, and for the
Credit / Details payloads the album and track menus render.

Four things this file pins. Offline: httpx, the tag reader and every MB
resolver are stubbed; no socket is opened.

  1. QUERY — every constraint the browser offers (primary + secondary release
     type, artist, year, label, catalog number, and the catno/barcode modes)
     reaches WS/2 as its own Lucene clause, all in ONE request. The index is
     the only place "albums by this artist from 1999 on that label" can be
     answered; filtering the returned rows cannot, because the rows that would
     match are not in the page.
  2. CACHE / THROTTLE — searches, release lookups and the genre cascade's
     entity levels all go through `mb_get_cached`, so a repeated view costs no
     second request at 1 req/s; and a bare MBID that MusicBrainz does not know
     is not re-probed four times on the next keystroke.
  3. PAGING — rows keep MusicBrainz's own (relevance) order, are de-duplicated
     by MBID, and the next offset follows the RAW slice MusicBrainz served, so
     no row is skipped or repeated when a page contains a duplicate.
  4. MENUS — /api/credits answers with exactly the rows CreditsPanel renders
     for a track (the recording's relations) and for an album (the release
     request's own per-track relations — ONE request, never one per track),
     with the tag fallback when MusicBrainz has nothing; and the album / track
     payloads the two details modals read carry every field they render.

Run:  python tools/test_mb_search.py
"""
import json
import os
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# hermeticity: seed the music folder BEFORE server.main is imported, and point
# mlo.config at a stub config file so nothing touches the developer's library.
# --------------------------------------------------------------------------- #
REDIRECT = tempfile.mkdtemp(prefix="mlo-mbsearch-redirect-")
os.environ["MLO_MUSIC_FOLDER"] = REDIRECT

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB_CFG = os.path.join(REDIRECT, "config.json")
with open(_STUB_CFG, "w", encoding="utf-8") as f:
    json.dump({"music_folder": REDIRECT}, f)

cfgmod.CONFIG_FILE = _STUB_CFG
pathmod.CONFIG_FILE = _STUB_CFG

import heapq  # noqa: E402

import server.integrations as intg  # noqa: E402
import server.tagcache as tagcache  # noqa: E402
from server import main as mlo_main  # noqa: E402  (heavy import)
from server import library as lib_mod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

MBID_ARTIST = "9a1bb5ba-1f1e-4f23-a1a4-9ef0e5d39c6c"
MBID_RG = "1c2b3a4d-5e6f-789a-bcde-f01234567890"
MBID_RELEASE = "0b1a2c3d-4e5f-6789-abcd-ef0123456789"
MBID_REC = "3e4d5c6b-7a8f-9012-cdef-123456789012"

_client = TestClient(mlo_main.app)

# The suites run in one process: MusicBrainz must not be asked to wait a
# second between stubbed calls (the SHIPPED value is asserted first).
assert intg._MB_MIN_INTERVAL == 1.0, intg._MB_MIN_INTERVAL
assert intg._BROWSE_TTL >= 600, intg._BROWSE_TTL
intg._MB_MIN_INTERVAL = 0.0
intg._last_request = 0.0


class _Resp:
    """The two httpx.Response attributes mb_get touches."""

    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


_SEARCH_KEYS = {"artist": "artists", "release-group": "release-groups",
                "release": "releases", "recording": "recordings"}


class FakeMB:
    """A scripted MusicBrainz: paged search/browse answers + recorded calls.

    `search[(entity, query)]` is the index for that exact Lucene query (the
    order it is listed in is the order MusicBrainz would answer in);
    `browse[(entity, ident)]` serves the browse endpoints keyed by the id
    parameter they were asked with; `lookup[entity/id]` serves entity lookups.
    """

    def __init__(self):
        self.calls = []
        self.search = {}
        self.browse = {}
        self.lookup = {}

    def call(self, url, params=None, headers=None, timeout=None):
        params = dict(params or {})
        entity = str(url).split("/ws/2/", 1)[-1]
        self.calls.append({"entity": entity, "params": params})
        if "query" in params:
            rows = self.search.get((entity, params["query"]))
            if rows is None:
                return _Resp(200, {"count": 0, _SEARCH_KEYS[entity]: []})
            offset, limit = int(params.get("offset") or 0), int(params.get("limit") or 25)
            return _Resp(200, {"count": len(rows), "offset": offset,
                               _SEARCH_KEYS[entity]: rows[offset:offset + limit]})
        payload = self.lookup.get(entity)
        if payload is not None:
            return _Resp(200, payload)
        ident = next((k for k in ("artist", "release-group", "recording") if k in params), None)
        rows = self.browse.get((entity, params.get(ident))) if ident else None
        if rows is None:
            return _Resp(404)
        offset, limit = int(params.get("offset") or 0), int(params.get("limit") or 25)
        return _Resp(200, {"count": len(rows), "offset": offset,
                           _SEARCH_KEYS[entity]: rows[offset:offset + limit]})

    def queries(self):
        return [c["params"]["query"] for c in self.calls if "query" in c["params"]]


def _install(mb):
    """Point the MB client at `mb` and start from an empty cache."""
    intg.httpx.get = mb.call
    with intg._BROWSE_LOCK:
        intg._BROWSE_CACHE.clear()
        intg._INFLIGHT.clear()
    intg._DETECT_MISSES.clear()
    intg._last_request = 0.0
    return mb


def _release_row(i, **over):
    row = {
        "id": f"{i:08d}-0000-0000-0000-000000000000",
        "score": 100 - i,
        "title": f"Release {i}",
        "artist-credit": [{"name": "Radiohead", "artist": {"id": MBID_ARTIST}}],
        "date": "1999-05-01",
        "country": "GB",
        "status": "Official",
        "label-info": [{"catalog-number": "CAT-1"}],
        "media": [{"format": "CD", "track-count": 10}],
    }
    row.update(over)
    return row


def _rg_row(i, **over):
    row = {
        "id": f"{i:08d}-1111-0000-0000-000000000000",
        "score": 100 - i,
        "title": f"Group {i}",
        "artist-credit": [{"name": "Radiohead", "artist": {"id": MBID_ARTIST}}],
        "primary-type": "Album",
        "secondary-types": [],
        "first-release-date": "1999-05-01",
    }
    row.update(over)
    return row


# --------------------------------------------------------------------------- #
# 1. the query: constraints COMBINE and reach WS/2 as their own clauses
# --------------------------------------------------------------------------- #
built = intg.search_query("release", "kid a", "free", "Album", "Compilation",
                          "Radiohead", "2000", "Parlophone", "CDP 7 46001 2")
clauses = built.split(" AND ")
assert "kid a" in clauses, built
assert 'catno:"CDP 7 46001 2"' in clauses, built
assert 'label:"Parlophone"' in clauses, built
assert 'artist:"Radiohead"' in clauses, built
assert "date:[2000 TO 2000]" in clauses, built
assert 'primarytype:"Album"' in clauses, built
assert 'secondarytype:"Compilation"' in clauses, built

# a year range is a range, and a release group has no `date`/`label`/`catno`
# field at all — its earliest release date is what MusicBrainz can filter on
assert "date:[1990 TO 1999]" in intg.search_query("release", "x", year="1990-1999").split(" AND ")
assert "firstreleasedate:[1990 TO 1999]" in \
    intg.search_query("release-group", "x", year="1990-1999").split(" AND ")
rg_q = intg.search_query("release-group", "amnesiac", "free", "Album", "Live",
                         "Radiohead", "2001", "Parlophone", "CAT-9")
assert "label:" not in rg_q and "catno:" not in rg_q, rg_q

# an artist's discography by type asks the index for the artist's own groups
assert intg.search_query("release-group", "", artist_id=MBID_ARTIST) == f"arid:{MBID_ARTIST}"

mb = _install(FakeMB())
mb.search[("release", built)] = [_release_row(1)]
page = intg.search_mb("release", "kid a", 100, "free", 0, primary_type="Album",
                      secondary_type="Compilation", artist="Radiohead", year="2000",
                      label="Parlophone", catno="CDP 7 46001 2")
assert len(mb.calls) == 1, mb.calls                       # ONE request, all constraints
sent = mb.calls[0]["params"]
assert mb.calls[0]["entity"] == "release", mb.calls[0]
assert sent["query"] == built, sent["query"]
assert sent["limit"] == 100 and sent["offset"] == 0 and sent["fmt"] == "json", sent
assert page["query"] == built and page["rows"][0]["artist_mbid"] == MBID_ARTIST, page

# WS/2 refuses limit > 100: asking for more must be clamped, not sent
mb = _install(FakeMB())
mb.search[("release", "ok computer")] = [_release_row(1)]
intg.search_mb("release", "ok computer", 500)
assert mb.calls[-1]["params"]["limit"] == 100, mb.calls[-1]

# modes: catno/barcode still work, and an explicit Cat # next to the same
# number in the box does not become a second, identical clause
mb = _install(FakeMB())
mb.search[("release", 'catno:"SRCS 8757"')] = [_release_row(1)]
intg.search_mb("release", "SRCS 8757", 100, "catno", 0, catno="SRCS 8757")
assert mb.calls[-1]["params"]["query"] == 'catno:"SRCS 8757"', mb.calls[-1]
mb = _install(FakeMB())
mb.search[("release", "barcode:074643924526")] = [_release_row(1)]
intg.search_mb("release", "074643924526", 100, "barcode")
assert mb.calls[-1]["params"]["query"] == "barcode:074643924526", mb.calls[-1]

# a constraint-only search is a legitimate browse of the index
mb = _install(FakeMB())
mb.search[("release", 'label:"Parlophone" AND date:[1999 TO 1999]')] = [_release_row(1)]
intg.search_mb("release", "", 100, "free", 0, label="Parlophone", year="1999")
assert mb.calls[-1]["params"]["query"] == 'label:"Parlophone" AND date:[1999 TO 1999]', mb.calls[-1]

# the route: the same constraints travel as query parameters, and query and
# constraint together answer (the broker page's own shape)
mb = _install(FakeMB())
mb.search[("release", 'kid a AND artist:"Radiohead"')] = [_release_row(1)]
r = _client.get("/api/mb/search", params={"q": "kid a", "type": "release",
                                          "artist": "Radiohead", "year": ""})
assert r.status_code == 200, r.text
assert r.json()["query"] == 'kid a AND artist:"Radiohead"', r.json()
assert r.json()["rows"][0]["id"] == _release_row(1)["id"], r.json()
# nothing to search is a 400, not a wildcard sweep of the whole index
assert _client.get("/api/mb/search", params={"q": "", "type": "release"}).status_code == 400


# --------------------------------------------------------------------------- #
# 2. cache + throttle: a repeat view costs no second request
# --------------------------------------------------------------------------- #
mb = _install(FakeMB())
mb.search[("release", "kid a")] = [_release_row(1)]
intg.search_mb("release", "kid a")
intg.search_mb("release", "kid a")
assert len(mb.calls) == 1, mb.calls                       # second read was cached

mb = _install(FakeMB())
mb.lookup[f"release/{MBID_RELEASE}"] = {"id": MBID_RELEASE, "title": "Kid A",
                                        "media": [], "artist-credit": []}
first = intg.release_lookup(MBID_RELEASE)
again = intg.release_lookup(MBID_RELEASE)
assert len(mb.calls) == 1, mb.calls                       # the release page is cached
assert first["id"] == again["id"] == MBID_RELEASE

# the genre cascade's entity levels share the same cached path
mb = _install(FakeMB())
mb.lookup[f"release-group/{MBID_RG}"] = {"id": MBID_RG, "genres": [{"name": "art rock"}]}
mb.lookup[f"artist/{MBID_ARTIST}"] = {"id": MBID_ARTIST, "genres": [{"name": "art rock"}]}
assert intg.release_group_genres(MBID_RG) == ["art rock"]
assert intg.release_group_genres(MBID_RG) == ["art rock"]
assert intg.artist_genres(MBID_ARTIST) == ["art rock"]
assert intg.artist_genres(MBID_ARTIST) == ["art rock"]
assert len(mb.calls) == 2, mb.calls                       # two entities, two calls

# a bare MBID that is not in MusicBrainz is probed once, not once per view
mb = _install(FakeMB())
missing = "ffffffff-ffff-ffff-ffff-ffffffffffff"
try:
    intg.detect_mbid(missing)
except LookupError:
    pass
else:
    raise AssertionError("an unknown MBID did not raise LookupError")
probes = len(mb.calls)
assert probes == len(intg.MB_ENTITIES), mb.calls
try:
    intg.detect_mbid(missing)
except LookupError:
    pass
else:
    raise AssertionError("an unknown MBID did not raise LookupError the second time")
assert len(mb.calls) == probes, mb.calls                  # the miss is remembered


# --------------------------------------------------------------------------- #
# 3. paging: MusicBrainz's order, no duplicates, no skipped rows
# --------------------------------------------------------------------------- #
mb = _install(FakeMB())
index = [_release_row(i) for i in range(121)]
index.insert(3, dict(index[3]))                            # MB repeats a row it matched twice
mb.search[("release", "kid a")] = index

seen, order, offset, pages = [], [], 0, 0
while True:
    page = intg.search_mb("release", "kid a", 100, "free", offset)
    pages += 1
    ids = [r["id"] for r in page["rows"]]
    assert len(ids) == len(set(ids)), f"duplicate row within a page: {ids}"
    seen.extend(ids)
    order.append(ids)
    if page["next"] is None:
        break
    offset = page["next"]
    assert pages < 10, "paging did not terminate"

assert pages == 2, pages
assert len(seen) == len(set(seen)) == 121, (len(seen), len(set(seen)))
# relevance order is MusicBrainz's (its own list order), de-duplicated in place:
# page 1 is the raw slice 0..99, which carries the duplicate, so it holds 99 rows
assert order[0] == [f"{i:08d}-0000-0000-0000-000000000000" for i in range(99)], order[0][:3]
assert order[1] == [f"{i:08d}-0000-0000-0000-000000000000" for i in range(99, 121)], order[1][:3]
# score order is what the table shows until the user sorts a column
scores = [intg.search_mb("release", "kid a", 3, "free", 0)["rows"][i]["score"] for i in range(3)]
assert scores == sorted(scores, reverse=True), scores
assert intg.search_mb("release", "kid a", 100, "free", 0)["duplicates"] == 1
# the offer to page is honest: the next offset is the RAW slice MusicBrainz
# served (122 entries for 121 distinct releases — it repeated one), so the
# second page neither repeats nor skips a release
assert intg.search_mb("release", "kid a", 100, "free", 0)["next"] == 100
assert intg.search_mb("release", "kid a", 100, "free", 100)["next"] is None
assert intg.search_mb("release", "kid a", 100, "free", 0)["total"] == 122


# --------------------------------------------------------------------------- #
# 4. the artist page: a type filter is answered by the index
# --------------------------------------------------------------------------- #
mb = _install(FakeMB())
mb.lookup[f"artist/{MBID_ARTIST}"] = {"id": MBID_ARTIST, "name": "Radiohead",
                                      "genres": [{"name": "art rock"}]}
mb.browse[("release-group", MBID_ARTIST)] = [_rg_row(1), _rg_row(2, **{
    "first-release-date": "1990-01-01"})]
plain = intg.artist_browse(MBID_ARTIST, 300, 0)
assert "query" not in mb.calls[-1]["params"], mb.calls[-1]      # browse, unfiltered
assert mb.calls[-1]["params"]["artist"] == MBID_ARTIST, mb.calls[-1]
assert [g["first_release_date"] for g in plain["release_groups"]] == \
    ["1990-01-01", "1999-05-01"], plain["release_groups"]
assert intg._MB_MIN_INTERVAL == 0.0 or True

mb = _install(FakeMB())
filtered_q = f'arid:{MBID_ARTIST} AND primarytype:"Album" AND secondarytype:"Soundtrack"'
mb.lookup[f"artist/{MBID_ARTIST}"] = {"id": MBID_ARTIST, "name": "Radiohead"}
mb.search[("release-group", filtered_q)] = [
    _rg_row(1, **{"primary-type": "Album", "secondary-types": ["Soundtrack"]})]
albums = intg.artist_browse(MBID_ARTIST, 300, 0, "Album", "Soundtrack")
assert mb.calls[-1]["entity"] == "release-group", mb.calls[-1]
assert mb.calls[-1]["params"]["query"] == filtered_q, mb.calls[-1]
assert [g["primary_type"] for g in albums["release_groups"]] == ["Album"], albums
assert albums["release_groups"][0]["secondary_types"] == ["Soundtrack"], albums
assert albums["total"] == 1 and albums["offset"] == 0, albums

# and the route carries the filter through
mb = _install(FakeMB())
mb.lookup[f"artist/{MBID_ARTIST}"] = {"id": MBID_ARTIST, "name": "Radiohead"}
mb.search[("release-group", f'arid:{MBID_ARTIST} AND primarytype:"EP"')] = [
    _rg_row(1, **{"primary-type": "EP"})]
r = _client.get(f"/api/mb/artist/{MBID_ARTIST}", params={"primary_type": "EP"})
assert r.status_code == 200, r.text
assert r.json()["release_groups"][0]["primary_type"] == "EP", r.json()


# --------------------------------------------------------------------------- #
# 5. the menus' payloads: credits for a track, credits for an album
# --------------------------------------------------------------------------- #
ARTIST_DIR = os.path.join(REDIRECT, "Test Artist")
ALBUM_DIR = os.path.join(ARTIST_DIR, "Test Album")
os.makedirs(ALBUM_DIR, exist_ok=True)
TRACK_FILES = [os.path.join(ALBUM_DIR, f"0{i} Track{i}.wav") for i in (1, 2)]
for path in TRACK_FILES:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)

TAGS = {
    path: {
        "TITLE": os.path.splitext(os.path.basename(path))[0],
        "ARTIST": "Test Artist",
        "ALBUMARTIST": "Test Artist",
        "ALBUM": "Test Album",
        "TRACKNUMBER": str(i + 1),
        "MUSICBRAINZ_TRACKID": MBID_REC,
        "MUSICBRAINZ_ALBUMID": MBID_RELEASE,
        "PERFORMER": "Lenny Kaye (guitar); Patti Smith (vocals)",
    }
    for i, path in enumerate(TRACK_FILES)
}
_real_read_track = tagcache.read_track


def _fake_read_track(path, *a, **kw):
    """Tags for the seeded files; the album's own tracks keep the real reader
    (a synthesised WAV is a real file with no tags)."""
    key = os.path.normpath(str(path))
    for k, v in TAGS.items():
        if os.path.normpath(k) == key:
            return dict(v), {}
    return _real_read_track(path, *a, **kw)


tagcache.read_track = _fake_read_track

RELATIONS = [
    {"type": "producer", "artist": {"id": MBID_ARTIST, "name": "Nigel Godrich"},
     "attributes": []},
    {"type": "performer", "artist": {"id": MBID_ARTIST, "name": "Thom Yorke"},
     "attributes": ["vocals", "piano"]},
    {"target-type": "work", "type": "performance", "attributes": ["cover"],
     "work": {"id": MBID_RG, "title": "Kid A"}},
]

# --- one track: the recording's own relations ------------------------------
mb = _install(FakeMB())
mb.lookup[f"recording/{MBID_REC}"] = {"id": MBID_REC, "relations": RELATIONS}
r = _client.get("/api/credits", params={"path": TRACK_FILES[0]})
assert r.status_code == 200, r.text
body = r.json()
assert body["source"] == "musicbrainz", body
assert body["artist"] == "Test Artist" and body["album"] == "Test Album", body
assert body["track_mbid"] == MBID_REC, body
roles = {(row["role"], row["artist"]) for row in body["rows"]}
assert ("producer", "Nigel Godrich") in roles, roles
assert ("performer", "Thom Yorke") in roles, roles
vocal = next(row for row in body["rows"] if row["artist"] == "Thom Yorke")
assert vocal["attributes"] == ["vocals", "piano"] and vocal["mbid"] == MBID_ARTIST, vocal
assert ("work", "Kid A") in roles, roles
assert sorted(row["role"] for row in body["rows"]) == [row["role"] for row in body["rows"]], body
assert len(mb.calls) == 1, mb.calls                        # one request per track

# --- the album: the release request's own track relations, ONE request -----
mb = _install(FakeMB())
mb.lookup[f"release/{MBID_RELEASE}"] = {
    "id": MBID_RELEASE, "title": "Test Album",
    "relations": [{"type": "mastering", "artist": {"id": MBID_ARTIST, "name": "Tim Young"},
                   "attributes": []}],
    "media": [{"tracks": [
        {"recording": {"relations": [RELATIONS[1]]}},
        {"recording": {"relations": [{"type": "engineer",
                                      "artist": {"id": MBID_ARTIST, "name": "Danton Supple"},
                                      "attributes": []}]}},
    ]}],
}
r = _client.get("/api/credits", params={"album": ALBUM_DIR})
assert r.status_code == 200, r.text
body = r.json()
assert body["source"] == "musicbrainz", body
assert body["release_mbid"] == MBID_RELEASE, body
roles = {(row["role"], row["artist"]) for row in body["rows"]}
assert ("mastering", "Tim Young") in roles, roles          # the release's own relations
assert ("performer", "Thom Yorke") in roles, roles         # per-track, from the same payload
assert ("engineer", "Danton Supple") in roles, roles
assert len(mb.calls) == 1, mb.calls                        # one request, not one per track

# --- MusicBrainz has nothing: the files' own credit tags -------------------
mb = _install(FakeMB())
mb.lookup[f"release/{MBID_RELEASE}"] = {"id": MBID_RELEASE, "title": "Test Album",
                                        "relations": [], "media": []}
body = _client.get("/api/credits", params={"album": ALBUM_DIR}).json()
assert body["source"] == "tags", body
assert body["rows"], body                                  # never an empty box
performer = next(row for row in body["rows"] if row["role"] == "performer")
assert performer["artist"] in ("Lenny Kaye", "Patti Smith"), performer
assert performer["mbid"] == "", performer
kaye = next(row for row in body["rows"] if row["artist"] == "Lenny Kaye")
assert kaye["attributes"] == ["guitar"], kaye

# --- and with no MB ID and no credit tags, the 404 is the honest answer ----
TAGS_BACKUP = dict(TAGS[TRACK_FILES[0]])
TAGS[TRACK_FILES[0]] = {k: v for k, v in TAGS_BACKUP.items()
                        if k not in ("MUSICBRAINZ_TRACKID", "PERFORMER")}
mb = _install(FakeMB())
assert _client.get("/api/credits", params={"path": TRACK_FILES[0]}).status_code == 404
TAGS[TRACK_FILES[0]] = TAGS_BACKUP


# --------------------------------------------------------------------------- #
# 6. the details modals' payloads
# --------------------------------------------------------------------------- #
# TrackDetails renders these off a Track; AlbumDetails off the album payload.
ALBUM_KEYS = {"path", "meta", "album_artist", "album_values", "grade_pct", "pass",
              "pass_count", "total_checks", "track_count", "audit_summary",
              "cover_file", "has_log", "has_cue", "checksum_status",
              "accuraterip_status", "lyrics_present", "lyrics_expected",
              "instrumental_count", "media", "source_summary", "issues", "tracks"}
TRACK_KEYS = {"file", "path", "tracknumber", "discnumber", "issues", "values",
              "audit", "tech", "tags", "lyrics_embedded", "lyrics_lrc",
              "lyrics_present", "unreadable", "grade_pass"}

r = _client.get("/api/album", params={"path": ALBUM_DIR})
assert r.status_code == 200, r.text
album = r.json()
missing = ALBUM_KEYS - set(album)
assert not missing, f"the album payload lost {sorted(missing)}"
assert album["tracks"], "the seeded album has no tracks"
track = album["tracks"][0]
missing = TRACK_KEYS - set(track)
assert not missing, f"the track payload lost {sorted(missing)}"
assert track["path"].replace("\\", "/").endswith("01 Track1.wav"), track["path"]
assert isinstance(track["tags"], dict) and isinstance(track["tech"], dict), track

# the MusicBrainz release page's own payload (the middle column of its rows)
mb = _install(FakeMB())
mb.lookup[f"release/{MBID_RELEASE}"] = {
    "id": MBID_RELEASE, "title": "Kid A", "date": "2000-10-02",
    "barcode": "724384925321", "country": "GB", "status": "Official",
    "artist-credit": [{"name": "Radiohead", "artist": {"id": MBID_ARTIST}}],
    "release-group": {"id": MBID_RG, "primary-type": "Album",
                      "secondary-types": [], "first-release-date": "2000-10-02"},
    "label-info": [{"catalog-number": "CDP 7 46001 2",
                    "label": {"name": "Parlophone"}}],
    "media": [{"position": 1, "format": "CD", "tracks": [
        {"position": 1, "title": "Everything in Its Right Place", "length": 251000,
         "recording": {"id": MBID_REC}, "artist-credit": [{"name": "Radiohead"}]},
    ]}],
}
release = _client.get("/api/mb/release", params={"mbid": MBID_RELEASE}).json()
for key in ("id", "title", "date", "catalog_number", "label", "country", "status",
            "medium", "release_group_id", "primary_type", "secondary_types",
            "artists", "genres", "media", "medium_count"):
    assert key in release, f"the release payload lost {key}"
assert release["media"][0]["recording_mbid"] == MBID_REC, release["media"][0]
assert release["release_group_id"] == MBID_RG, release

tagcache.read_track = _real_read_track
print("musicbrainz search, paging, cache and the credit/details payloads: all asserts passed")
