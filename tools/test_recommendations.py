#!/usr/bin/env python3
"""Verify the local recommendation scorer: seed exclusion, precedence,
determinism and the empty-seed / empty-library answers. Synthetic library data
(no disk scan, no network, all tags written inline).

Run:  python tools/test_recommendations.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import library as lib_mod
from server import mbresolve
from server import playlists as plist
from server import recommend

# --------------------------------------------------------------------------- #
# A five-album library: two sibling records by one artist (the shelf's obvious
# answer), one record sharing only the broad family, one sharing the numeric
# tags but no genre, and one that shares no term at all and must score zero.
# --------------------------------------------------------------------------- #
CONFIG = {"music_folder": "C:/lib"}


def album(path, artist, title, year, genre, mood, energy, names, mbid=""):
    tracks = []
    for i, name in enumerate(names):
        tracks.append({
            "file": f"{name}.flac",
            "path": f"{path}/{name}.flac",
            "cover_file": None,
            "tags": {"TITLE": name, "GENRE": genre, "MOOD": mood, "ENERGY": energy,
                     "MUSICBRAINZ_TRACKID": f"{mbid}-t{i}" if mbid else ""},
        })
    return {
        "path": path,
        "cover_file": None,
        "meta": {"ALBUM": title, "ALBUMARTIST": artist, "DATE": year, "GENRE": genre,
                 "MUSICBRAINZ_ALBUMID": mbid},
        "tracks": tracks,
    }


LIB = {
    "folder": "C:/lib",
    "artists": [
        {"path": "C:/lib/Alpha", "name": "Alpha", "albums": [
            album("C:/lib/Alpha/Black", "Alpha", "Black One", "2001",
                  "black metal", "aggressive", "85", ["One", "Two"], "alb-1"),
            album("C:/lib/Alpha/Black2", "Alpha", "Black Two", "2003",
                  "black metal", "aggressive", "88", ["Three"], "alb-2"),
        ]},
        {"path": "C:/lib/Beta", "name": "Beta", "albums": [
            album("C:/lib/Beta/Metal", "Beta", "Metal One", "2002",
                  "heavy metal", "aggressive", "80", ["One"], "alb-3"),
        ]},
        {"path": "C:/lib/Delta", "name": "Delta", "albums": [
            album("C:/lib/Delta/Loud", "Delta", "Loud One", "2002",
                  "punk", "aggressive", "86", ["One"], "alb-5"),
        ]},
        {"path": "C:/lib/Gamma", "name": "Gamma", "albums": [
            album("C:/lib/Gamma/Quiet", "Gamma", "Quiet One", "1970",
                  "ambient", "calm", "", ["One"], "alb-4"),
        ]},
    ],
}


# The scorer reads `server.library.build_library` and MusicBrainz resolution
# reads the same payload, so one patch serves both — no disk, no config file.
lib_mod.build_library = lambda cfg, progress=None: LIB


def _mb_index():
    """Minimal MusicBrainz index over the fixture — the shape
    `server/mbresolve.get_index()` builds, without the disk scan."""
    tracks, albums = {}, {}
    for a in LIB["artists"]:
        for al in a["albums"]:
            albums[al["meta"]["MUSICBRAINZ_ALBUMID"]] = al["path"]
            for t in al["tracks"]:
                mbid = t["tags"]["MUSICBRAINZ_TRACKID"]
                if mbid:
                    tracks[mbid] = t["path"]
    return {"tracks": tracks, "tracks_bypath": {}, "albums": albums,
            "albums_bypath": {}, "artists": {"art-1": "C:/lib/Alpha"},
            "artists_bypath": {}}


mbresolve.get_index = lambda cfg=None, force=False: _mb_index()
recommend.invalidate()

SEED = "C:/lib/Alpha/Black/One.flac"
SIBLING = "C:/lib/Alpha/Black2/Three.flac"
FAMILY = "C:/lib/Beta/Metal/One.flac"
LOUD = "C:/lib/Delta/Loud/One.flac"
UNRELATED = "C:/lib/Gamma/Quiet/One.flac"


def paths(rows):
    return [r["path"] for r in rows]


def calls(rows):
    return [(r["path"], r["score"], r["reasons"]) for r in rows]


# --------------------------------------------------------------------------- #
# Track shelf from a track seed: the seed and its own album are gone, the
# sibling record wins, and one unrelated record never scores at all.
# --------------------------------------------------------------------------- #
tracks = recommend.recommend(CONFIG, "track", SEED)
assert tracks, "a tagged seed must yield a track shelf"
assert all(r["kind"] == "track" for r in tracks), tracks
assert SEED not in paths(tracks) and "C:/lib/Alpha/Black/Two.flac" not in paths(tracks)
assert not any(p.startswith("C:/lib/Alpha/Black/") for p in paths(tracks))
assert paths(tracks) == [SIBLING, FAMILY, LOUD], paths(tracks)
assert UNRELATED not in paths(tracks)
assert len(tracks) <= recommend.DEFAULT_TRACK_LIMIT
assert all(r["reasons"] for r in tracks)
assert all(tracks[i]["score"] >= tracks[i + 1]["score"] for i in range(len(tracks) - 1))
# The row carries everything a player row needs, without a second lookup.
assert tracks[0]["file"] == "Three.flac"
assert tracks[0]["album_path"] == "C:/lib/Alpha/Black2"
assert tracks[0]["album"] == "Black Two" and tracks[0]["artist"] == "Alpha"
assert tracks[0]["mbid"] == "alb-2-t0"
# Two identical requests answer identically — the shelf must not reshuffle.
assert calls(tracks) == calls(recommend.recommend(CONFIG, "track", SEED))
# A library path and its MusicBrainz form are the same seed.
assert calls(tracks) == calls(recommend.recommend(CONFIG, "track", "mb:alb-1-t0"))

# --------------------------------------------------------------------------- #
# Album shelf: the same seed scored against records, the seed album excluded,
# a shared SPECIFIC genre outranking a shared family only.
# --------------------------------------------------------------------------- #
albums = recommend.recommend(CONFIG, "album", "C:/lib/Alpha/Black")
assert paths(albums) == ["C:/lib/Alpha/Black2", "C:/lib/Beta/Metal", "C:/lib/Delta/Loud"], paths(albums)
assert all(r["kind"] == "album" for r in albums), albums
assert albums[0]["reasons"][0] == "same genre: black metal", albums[0]
assert albums[1]["reasons"][0] == "same family: metal", albums[1]
assert albums[0]["score"] > albums[1]["score"] > albums[2]["score"], albums
assert albums[2]["reasons"][0] == "same mood: aggressive", albums[2]
assert albums[0]["cover_path"] == "C:/lib/Alpha/Black2"
# The record with no term in common (a different family, a different mood, no
# ENERGY, an era outside ERA_SPAN) scores zero and is dropped, not ranked last.
assert "C:/lib/Gamma/Quiet" not in paths(albums)
# Bounds: the caller's limit is honoured and the default is an album shelf's.
assert len(recommend.recommend(CONFIG, "album", "C:/lib/Alpha/Black", limit=1)) == 1
assert len(albums) <= recommend.DEFAULT_LIMIT
assert len(tracks) <= recommend.DEFAULT_TRACK_LIMIT

# An artist page suggests other artists' records, never the artist's own.
artist = recommend.recommend(CONFIG, "artist", "C:/lib/Alpha")
assert paths(artist) == ["C:/lib/Beta/Metal", "C:/lib/Delta/Loud"], paths(artist)
assert paths(recommend.recommend(CONFIG, "artist", "mb:art-1")) == paths(artist)
# The turn of the shelf is the caller's choice: tracks from an artist seed.
artist_tracks = recommend.recommend(CONFIG, "artist", "C:/lib/Alpha", target="tracks")
assert paths(artist_tracks) == [FAMILY, LOUD], paths(artist_tracks)

# --------------------------------------------------------------------------- #
# Multi-track seeds: the seed set is one profile, an album folder and an artist
# folder are seeds too, and a mixed set never claims a single artist.
# --------------------------------------------------------------------------- #
idx = recommend.index(CONFIG)
two = recommend._seed_entries(idx, [SEED, SIBLING])
assert paths(two) == [SEED, SIBLING], two
assert recommend._seed_profile(two)["artist_key"] == "alpha"
mixed = recommend._seed_entries(idx, [SEED, UNRELATED])
assert recommend._seed_profile(mixed)["artist_key"] is None
assert len(recommend._seed_entries(idx, ["C:/lib/Alpha"])) == 3
assert paths(recommend._seed_entries(idx, ["C:/lib/Beta/Metal"])) == [FAMILY]
assert recommend._seed_entries(idx, ["C:/nope", "mb:nope", ""]) == []

multi = recommend.recommend(CONFIG, "tracks", seeds=[SEED, UNRELATED])
assert paths(multi) == [SIBLING, FAMILY, LOUD], paths(multi)
assert not any(r.startswith("same artist") for r in multi[1]["reasons"]), multi[1]
assert calls(multi) == calls(recommend.recommend(CONFIG, "tracks", seeds=[UNRELATED, SEED]))
# An album shelf from an explicit seed list drops the albums it was seeded with.
assert paths(recommend.recommend(CONFIG, "albums", seeds=[SEED, SIBLING])) == \
    ["C:/lib/Beta/Metal", "C:/lib/Delta/Loud"]
# Everything returned is owned: candidates come from the library, not a provider.
owned = {t["path"] for t in idx["tracks"]}
assert set(paths(multi)) <= owned
# A playlist is the same seed set with a different origin: its own tracks and
# their albums are gone, the shelf is made of tracks.
plist.get_playlist = lambda pid, user="": {"id": pid, "kind": "manual", "tracks": [SEED]}
playlist = recommend.recommend(CONFIG, "playlist", "7")
assert paths(playlist) == [SIBLING, FAMILY, LOUD], paths(playlist)
assert all(r["kind"] == "track" for r in playlist), playlist

# --------------------------------------------------------------------------- #
# Favourites: the caller's own likes seed a track shelf, favourite albums and
# artists seed an album shelf, and the read is scoped to that caller.
# --------------------------------------------------------------------------- #
plist.list_likes = lambda user="": [SEED]
plist.list_favorites = lambda user="": {"albums": ["C:/lib/Gamma/Quiet"], "artists": ["C:/lib/Beta"]}
liked = recommend.recommend(CONFIG, "favorites", target="tracks")
assert paths(liked) == [SIBLING, FAMILY, LOUD], paths(liked)
fav_albums = recommend.recommend(CONFIG, "favorites", target="albums")
assert paths(fav_albums) == ["C:/lib/Alpha/Black", "C:/lib/Alpha/Black2", "C:/lib/Delta/Loud"], paths(fav_albums)
# Both the favourite album and the favourite artist's record are excluded.
assert "C:/lib/Gamma/Quiet" not in paths(fav_albums) and "C:/lib/Beta/Metal" not in paths(fav_albums)
assert calls(fav_albums) == calls(recommend.recommend(CONFIG, "favorites", target="albums"))

# --------------------------------------------------------------------------- #
# The route the shelf calls. Driven through the real router, over a bare app so
# the check is about the endpoint rather than the whole server's middleware.
# --------------------------------------------------------------------------- #
try:
    from fastapi import FastAPI  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402

    from server import api_recommend  # noqa: E402
except Exception as e:                                   # pragma: no cover
    print(f"SKIP: no HTTP client for the route check ({e})")
    raise SystemExit(2)

api_recommend.load_config = lambda: CONFIG
_app = FastAPI()
_app.include_router(api_recommend.router)
_http = TestClient(_app)
ALBUM_SEED = "C:/lib/Alpha/Black"

# POST is what the UI sends: a seed list of any length, the same answer the
# scorer gives, and the caller's limit honoured.
posted = _http.post("/api/recommend", json={"kind": "album", "id": ALBUM_SEED})
assert posted.status_code == 200, posted.text
assert [i["path"] for i in posted.json()["items"]] == paths(albums), posted.text
bounded = _http.post("/api/recommend", json={"kind": "tracks", "seeds": [SEED, UNRELATED], "limit": 2})
assert bounded.status_code == 200 and len(bounded.json()["items"]) == 2, bounded.text
assert [i["path"] for i in bounded.json()["items"]] == paths(multi)[:2]
assert _http.post("/api/recommend", json={"kind": "favorites", "target": "tracks"}).status_code == 200
assert len(_http.post("/api/recommend", json={"kind": "album", "id": ALBUM_SEED, "limit": 999})
           .json()["items"]) <= recommend.MAX_LIMIT
assert len(_http.post("/api/recommend", json={"kind": "album", "id": ALBUM_SEED, "limit": 0})
           .json()["items"]) == 1

# GET still answers one entity from the query string (older clients, links),
# and takes a repeated `seeds` for a short list.
got = _http.get("/api/recommend", params={"kind": "album", "id": ALBUM_SEED, "limit": 5})
assert got.status_code == 200, got.text
assert [i["path"] for i in got.json()["items"]] == paths(albums)
seeded = _http.get("/api/recommend",
                   params=[("kind", "tracks"), ("seeds", SEED), ("seeds", UNRELATED)])
assert seeded.status_code == 200 and [i["path"] for i in seeded.json()["items"]] == paths(multi)

# A request that cannot be answered is a 400 when it is malformed and an empty
# shelf when it is merely unknown — never a broken page.
assert _http.get("/api/recommend?kind=nope").status_code == 400
assert _http.post("/api/recommend", json={"kind": "album", "id": ALBUM_SEED, "target": "records"}).status_code == 400
empty = _http.post("/api/recommend", json={"kind": "album", "id": "C:/lib/nope"})
assert empty.status_code == 200 and empty.json()["items"] == []

seen = []
plist.list_likes = lambda user="": (seen.append(user), [])[1]
assert recommend.recommend(CONFIG, "favorites", target="tracks", user="bob") == []
assert seen == ["bob"], seen

# --------------------------------------------------------------------------- #
# Nothing to suggest is an empty shelf, never an error.
# --------------------------------------------------------------------------- #
plist.list_likes = lambda user="": []
plist.list_favorites = lambda user="": {"albums": [], "artists": [], "playlists": []}
assert recommend.recommend(CONFIG, "favorites", target="tracks") == []
assert recommend.recommend(CONFIG, "favorites", target="albums") == []
assert recommend.recommend(CONFIG, "tracks", seeds=[]) == []
assert recommend.recommend(CONFIG, "albums", seeds=[""]) == []
assert recommend.recommend(CONFIG, "tracks", seeds=["C:/nope"]) == []
assert recommend.recommend(CONFIG, "album", "C:/nope") == []
assert recommend.recommend(CONFIG, "artist", "mb:nope") == []
assert recommend.recommend(CONFIG, "track", "") == []
assert recommend.recommend(CONFIG, "playlist", "not-an-id") == []

for bad in (lambda: recommend.recommend(CONFIG, "nope", SEED),
            lambda: recommend.recommend(CONFIG, "track", SEED, target="records")):
    try:
        bad()
        assert False, "an unknown kind or target must raise"
    except ValueError:
        pass

# An empty library answers empty for every seed shape.
lib_mod.build_library = lambda cfg, progress=None: {"folder": "C:/none", "artists": []}
recommend.invalidate()
assert recommend.recommend(CONFIG, "track", SEED) == []
assert recommend.recommend(CONFIG, "tracks", seeds=[SEED]) == []
assert recommend.recommend(CONFIG, "favorites", target="albums") == []

print("ok")
