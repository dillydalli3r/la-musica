#!/usr/bin/env python3
"""Verify the Home page payload helpers: top artists, needs-attention and
Soulseek wishlist rows. Uses synthetic library data (no disk scan).

Run:  python tools/test_home.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import recommendations as r

artists = [
    {"path": "C:/M/A", "name": "Alpha", "aggregate": {"grade_pct": 90.0, "track_count": 20},
     "albums": [{"path": "C:/M/A/X", "cover_file": "cover.jpg"},
                {"path": "C:/M/A/Y", "cover_file": None}]},
    {"path": "C:/M/B", "name": "Beta", "aggregate": {"grade_pct": None, "track_count": 5},
     "albums": [{"path": "C:/M/B/Z"}]},
    {"path": "C:/M/C", "name": "", "albums": []},  # dropped
]
top = r._top_artists(artists, 5)
assert [a["artist"] for a in top] == ["Alpha", "Beta"], top
assert top[0]["album_count"] == 2 and top[0]["track_count"] == 20
assert top[0]["cover"] == "cover.jpg" and top[0]["cover_path"] == "C:/M/A/X"
assert top[1]["grade_pct"] is None

albums = [
    {"path": "C:/M/A/X", "total_checks": 10, "pass_count": 10, "pass": True, "grade_pct": 100.0},
    {"path": "C:/M/A/Y", "total_checks": 10, "pass_count": 4, "pass": False, "grade_pct": 40.0},
    {"path": "C:/M/A/Z", "total_checks": 0, "pass_count": 0, "pass": False, "grade_pct": None},
]
att = r._needs_attention(albums, 5)
assert [a["path"] for a in att] == ["C:/M/A/Y"], att
assert att[0]["owned"] is True and att[0]["mb_kind"] == "rg"

from server import wishes as wishes_mod

wishes_mod.list_wishes = lambda: [
    {"status": "searching", "title": "T", "artist": "Ar", "year": "1999",
     "release_mbid": "abc-123"},
    {"status": "imported", "title": "Done", "release_mbid": "zzz"},
]
wanted = r._wanted(5)
assert len(wanted) == 1, wanted
assert wanted[0]["mbid"] == "abc-123" and wanted[0]["mb_kind"] == "release"
assert wanted[0]["reason"] == "Searching Soulseek" and wanted[0]["owned"] is False

# ------------------------------------------------------------------------- #
# Home payload: the discovery-driven shelves.
# ------------------------------------------------------------------------- #
from server import discovery as disc_mod  # noqa: E402
from server import library as lib_mod  # noqa: E402


def _album(path, title, artist, tracks, genre=None, date=None):
    alb = {"path": path, "cover_file": "cover.jpg",
           "meta": {"ALBUM": title, "ALBUMARTIST": artist,
                    "DATE": date or "1999-01-01"}}
    if tracks:
        alb["tracks"] = [{"tags": {"GENRE": genre}} for _ in range(tracks)]
    return alb


LIB = {"artists": [
    {"path": "C:/M/Alpha", "name": "Alpha", "display_name": "Alpha",
     "aggregate": {"track_count": 6, "grade_pct": 90.0},
     "albums": [_album("C:/M/Alpha/X", "X", "Alpha", 2, "Rock"),
                _album("C:/M/Alpha/Y", "Y", "Alpha", 1, "Rock")]},
    {"path": "C:/M/Beta", "name": "Beta", "display_name": "Beta",
     "albums": [_album("C:/M/Beta/Z", "Z", "Beta", 1, "Rock")]},
    {"path": "C:/M/Gamma", "name": "Gamma", "display_name": "Gamma",
     "albums": [_album("C:/M/Gamma/G", "G", "Gamma", 1, "Pop"),
                _album("C:/M/Gamma/G2", "G2", "Gamma", 1, "Pop")]},
    {"path": "C:/M/Delta", "name": "Delta", "display_name": "Delta",
     "albums": [_album("C:/M/Delta/D", "D", "Delta", 5, "Jazz")]},
]}
lib_mod.build_library = lambda cfg: LIB
from server import playlists as pl_mod  # noqa: E402

pl_mod.list_favorites = lambda: {"albums": []}

NEW_ROW = {"kind": "album", "title": "New One", "artist": "Beta", "year": "2001",
           "cover": "https://cdn/new.jpg", "source": "deezer",
           "popularity": 5000, "popularity_label": "5k fans", "deezer_id": 7}
OWNED_ROW = {"kind": "album", "title": "X", "artist": "Alpha", "source": "deezer"}
POPULAR = [
    {"kind": "album", "title": "X", "artist": "Alpha", "mbid": "rg-1",
     "cover": "https://cdn/x.jpg", "source": "listenbrainz",
     "popularity": 12, "popularity_label": "12 listens",
     "reason": "Popular right now"},
    {"kind": "album", "title": "Hot", "artist": "Gamma", "mbid": "rg-2",
     "source": "listenbrainz", "reason": "Popular right now"},
]

seen = {}


def fake_recommend(seeds, limit=18, cfg=None, exclude=None):
    seen["seeds"] = seeds
    seen["exclude"] = set(exclude or ())
    return [NEW_ROW, OWNED_ROW]


old_recommend, old_popular = disc_mod.recommend_albums, disc_mod.popular_albums
disc_mod.recommend_albums = fake_recommend
disc_mod.popular_albums = lambda limit=12, cfg=None, range_="week": list(POPULAR)

r.invalidate()
data = r.build_home({"music_folder": "C:/M", "home_rec_source": "discovery"})

recommended = data["recommended"]
assert [a["album"] for a in recommended] == ["New One"], recommended  # owned dropped
assert recommended[0] == {
    "path": "", "album": "New One", "artist": "Beta", "year": "2001",
    "cover": None, "grade_pct": None, "cover_url": "https://cdn/new.jpg",
    "reason": "", "mbid": None, "mb_kind": "rg", "owned": False,
    "source": "deezer", "popularity_label": "5k fans", "popularity": 5000,
    "deezer_id": 7,
}, recommended[0]
assert data["rec_source"] == "discovery"

# Seeds: top-3 artists, then the two biggest genres via their best artist.
assert seen["seeds"] == [
    {"artist": "Alpha", "weight": 2, "reason": "Because you collect Alpha"},
    {"artist": "Gamma", "weight": 2, "reason": "Because you collect Gamma"},
    {"artist": "Beta", "weight": 1, "reason": "Because you collect Beta"},
    {"artist": "Delta", "weight": 5, "reason": "Because you like Jazz"},
], seen["seeds"]
# Owned albums are excluded from the provider query by normalized key.
assert seen["exclude"] == {"alpha|x", "alpha|y", "beta|z", "gamma|g", "gamma|g2",
                           "delta|d"}, seen["exclude"]

popular = data["popular"]
assert [a["album"] for a in popular] == ["X", "Hot"], popular
assert popular[0]["path"] == "C:/M/Alpha/X" and popular[0]["owned"] is True
assert popular[0]["reason"] == "Popular right now"
assert popular[1]["path"] == "" and popular[1]["owned"] is False
assert popular[1]["mbid"] == "rg-2" and popular[1]["mb_kind"] == "rg"
# Owned shelves are untouched by any of this.
assert len(data["recent"]) == 6, data["recent"]
assert data["stats"]["albums"] == 6 and data["top_artists"][0]["artist"] == "Alpha"

# home_rec_source="musicbrainz" keeps the old path (no provider call).
mb_calls = []
r._mb_recs = lambda albums, owned_rg, artist_count, genre_count, want: mb_calls.append(want) or [
    {"path": "", "album": "MB Pick", "artist": "Someone", "year": None, "cover": None,
     "grade_pct": None, "mbid": "mb-1", "mb_kind": "rg", "reason": "More from Alpha",
     "owned": False}]
disc_mod.recommend_albums = lambda *a, **k: (_ for _ in ()).throw(AssertionError("provider used"))
r.invalidate()
data = r.build_home({"music_folder": "C:/M", "home_rec_source": "musicbrainz"})
assert [a["album"] for a in data["recommended"]] == ["MB Pick"], data["recommended"]
assert data["rec_source"] == "musicbrainz" and mb_calls == [18]

# A raising provider degrades: empty shelf, everything else intact.
disc_mod.recommend_albums = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("provider down"))
r._mb_recs = lambda *a, **k: []
r.invalidate()
data = r.build_home({"music_folder": "C:/M", "home_rec_source": "discovery"})
assert data["recommended"] == [], data["recommended"]
assert data["rec_source"] == "musicbrainz"
assert data["popular"] and data["recent"] and data["stats"]["albums"] == 6

# discovery_enabled=False: no popular shelf, recommendations still degrade.
disc_mod.popular_albums = old_popular
r.invalidate()
data = r.build_home({"music_folder": "C:/M", "home_rec_source": "discovery",
                     "discovery_enabled": False})
assert data["popular"] == [] and data["recommended"] == [], data

# Settings changes are picked up despite the TTL cache.
disc_mod.recommend_albums = fake_recommend
disc_mod.popular_albums = lambda limit=12, cfg=None, range_="week": list(POPULAR)
r.invalidate()
r.build_home({"music_folder": "C:/M", "home_rec_source": "discovery"})
data = r.build_home({"music_folder": "C:/M", "home_rec_source": "listenbrainz"})
assert data["rec_source"] == "listenbrainz", data["rec_source"]
assert [a["album"] for a in data["recommended"]] == ["Hot"], data["recommended"]

disc_mod.recommend_albums, disc_mod.popular_albums = old_recommend, old_popular

print("ok")
