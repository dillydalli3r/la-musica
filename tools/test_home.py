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

# --------------------------------------------------------------------------- #
# A shelf row IS the library's own album row plus the shelf's reason, not a
# reduced copy of it: the shared card (web/src/components/AlbumCard) reads the
# row's tracks, meta, cover, grade and audit, so a Home card that carried less
# would say less about an album than the Library does about the same one.
# --------------------------------------------------------------------------- #
lib_row = {
    "path": "C:/M/A/X", "cover_file": "cover.jpg", "media": "CD",
    "pass": True, "audit_summary": "REAL", "grade_pct": 100.0, "track_count": 1,
    "meta": {"ALBUM": "X", "ALBUMARTIST": "Alpha", "DATE": "1999"},
    "tracks": [{"path": "C:/M/A/X/1.flac", "file": "1.flac"}],
}
row = r._owned_row(lib_row, reason="Recently added")
assert row["reason"] == "Recently added" and row["owned"] is True, row
assert row["artist"] == "Alpha", row
assert row["meta"]["ALBUM"] == "X" and row["tracks"] == lib_row["tracks"], row
assert row["cover_file"] == "cover.jpg" and row["pass"] is True, row
assert row["audit_summary"] == "REAL" and row["grade_pct"] == 100.0, row
assert row["mbid"] is None and row["mb_kind"] == "rg", row
# …and nothing of the old reduced row: a second album shape is what let the
# two pages draw the same album differently.
assert {"album", "year", "cover"}.isdisjoint(row), sorted(row)

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
# A wish is NOT a library album: the row carries the identity a card draws and
# nothing that reads as a library fact, so no surface can claim the release was
# graded or has audio to play (the card keys that on `owned`).
assert wanted[0]["tracks"] == [], wanted[0]
assert wanted[0]["meta"] == {"ALBUM": "T", "ARTIST": "Ar", "DATE": "1999"}, wanted[0]
assert {"pass", "audit_summary", "grade_pct", "media"}.isdisjoint(wanted[0]), sorted(wanted[0])
# ------------------------------------------------------------------------- #
# The recommendation shelves are GONE: the Home payload carries library-derived
# data only, and no provider chain is consulted for it any more.
# ------------------------------------------------------------------------- #
_home = r.build_home({"music_folder": "C:/definitely-not-a-library"})
assert {"recommended", "popular", "rec_source"}.isdisjoint(_home), sorted(_home)
assert {"stats", "recent", "top_rated", "favorites", "discover", "top_artists",
        "wanted", "needs_attention", "rated"} <= set(_home), sorted(_home)
assert _home["stats"]["albums"] == 0 and _home["recent"] == [], _home["stats"]

# --------------------------------------------------------------------------- #
# The artist shelf draws the library's DISPLAY name — the naming script puts
# the MusicBrainz id in the folder name, and that is library identity, not a
# caption (the owner's shelf read "Radiohead [a74b1b7f-…]"). The picture flag
# rides along with the row so the client never asks for a URL that 404s.
# --------------------------------------------------------------------------- #
folders = [
    {"path": "C:/M/Radiohead [a74b1b7f-71a5-4011-9441-d0b5e4122711]",
     "name": "Radiohead [a74b1b7f-71a5-4011-9441-d0b5e4122711]",
     "display_name": "Radiohead",
     "albums": [{"path": "C:/M/A/X", "cover_file": "cover.jpg"}],
     "aggregate": {"track_count": 3}},
]
top = r._top_artists(folders, 6)
assert top[0]["artist"] == "Radiohead", top
assert top[0]["has_image"] is False, top  # a folder that is not there holds no picture

# --------------------------------------------------------------------------- #
# The rated shelf is the ratings store joined with the library's own album rows:
# the user's verdicts, best first, each row carrying the half-star value it was
# ranked by — so the order and the stars on the cards are one number. A value
# the store no longer holds (a cleared rating) is not a row, and a store that
# cannot be read loses the shelf rather than the page.
# --------------------------------------------------------------------------- #
from server import ratings as store

store.map_for = lambda **kw: {"C:/M/A/X": 6, "C:/M/A/Y": 9, "C:/M/A/Z": 0}
rated = r._rated(albums, user="", limit=5)
assert [x["path"] for x in rated] == ["C:/M/A/Y", "C:/M/A/X"], rated
assert [x["rating"] for x in rated] == [9, 6], rated
assert all(x["owned"] is True and x["artist"] == "" for x in rated), rated
assert r._rated(albums, user="", limit=1) == rated[:1]

store.map_for = lambda **kw: 1 / 0
assert r._rated(albums, user="", limit=5) == []
assert r._rated([], user="", limit=5) == []

# --------------------------------------------------------------------------- #
# The grading strip (server.recommendations.grade_warning — the object
# `GET /api/grades/summary` answers with and the Home payload carries as
# `grade_warning`, drawn by web/src/components/GradeWarning on both pages): the
# owner's shape for a finding, the two albums that are never one, the totals
# printed beside it, and the cap that keeps a strip from becoming a page.
# --------------------------------------------------------------------------- #
def _tr(path, **issues):
    return {"path": path, "file": path.rsplit("/", 1)[-1],
            "tags": {"TITLE": path.rsplit("/", 1)[-1]}, "issues": issues}


gw_lib = {"artists": [{"path": "C:/M/A", "name": "Alpha", "albums": [
    # ONE failing track: the finding is the TRACK, and the album is its frame.
    {"path": "C:/M/A/One", "pass": False, "pass_count": 9, "total_checks": 10,
     "grade_pct": 90.0, "meta": {"ALBUM": "One"},
     "tracks": [_tr("C:/M/A/One/1.flac", COVER="track")]},
    # TWO failing tracks: the finding is the ALBUM, with the count and the
    # union of their codes — a dozen rows of one album say less than its name.
    {"path": "C:/M/A/Two", "pass": False, "pass_count": 4, "total_checks": 10,
     "grade_pct": 40.0, "meta": {"ALBUM": "Two"},
     "tracks": [_tr("C:/M/A/Two/1.flac", COVER="track"),
                _tr("C:/M/A/Two/2.flac", AUDIT="track")]},
    # A failure recorded against the album itself names no file, so it is an
    # album row carrying the grader's own sentence.
    {"path": "C:/M/A/Three", "pass": False, "pass_count": 3, "total_checks": 4,
     "grade_pct": 75.0, "meta": {"ALBUM": "Three"}, "tracks": [],
     "issues": {"Missing MEDIA": ["album-wide"]}},
    # NOT findings: a passing album, a PENDING framework album (nothing was
    # graded because its audio has not arrived — listing it would report a wish
    # as a broken album) and an album with no checks (0 == 0 passes).
    {"path": "C:/M/A/Fine", "pass": True, "pass_count": 10, "total_checks": 10,
     "grade_pct": 100.0, "meta": {"ALBUM": "Fine"}, "tracks": []},
    {"path": "C:/M/A/Wait", "pass": False, "pending": True, "pass_count": 0,
     "total_checks": 1, "grade_pct": 0.0, "meta": {"ALBUM": "Wait"}, "tracks": []},
    {"path": "C:/M/A/Off", "pass": False, "pass_count": 0, "total_checks": 0,
     "grade_pct": None, "meta": {"ALBUM": "Off"}, "tracks": []},
]}]}
gw = r.grade_warning(gw_lib)
assert gw["ok"] is False and gw["albums_failing"] == 3, gw
assert gw["tracks_failing"] == 3, gw
# The totals are the SAME sums the Home header prints, so the strip and the
# percentage beside it cannot disagree: 26 of 35 checks.
assert (gw["pass_count"], gw["total_checks"], gw["grade_pct"]) == (26, 35, 74.3), gw
assert [(i["kind"], i["album"]) for i in gw["items"]] == \
    [("album", "Two"), ("album", "Three"), ("track", "One")], gw["items"]
two = gw["items"][0]
assert two["failing_tracks"] == 2 and two["codes"] == ["AUDIT", "COVER"], two
three = gw["items"][1]
assert three["reason"] == "Missing MEDIA" and three["failing_tracks"] == 0, three
one = gw["items"][2]
assert one["track_path"] == "C:/M/A/One/1.flac" and one["title"] == "1.flac", one
assert one["codes"] == ["COVER"] and "failing_tracks" not in one, one
# Worst first, by the same key for both kinds.
assert [i["grade_pct"] for i in gw["items"]] == [40.0, 75.0, 90.0], gw["items"]
# A library that passes says so, with nothing to list.
assert r.grade_warning({"artists": []})["ok"] is True
# The cap: 12 rows listed, the rest COUNTED — the Library's Failing filter is
# where a reader goes past a screenful.
many = {"artists": [{"path": "C:/M/A", "albums": [
    {"path": f"C:/M/A/A{n}", "pass": False, "pass_count": 0, "total_checks": 1,
     "grade_pct": float(n), "meta": {"ALBUM": f"A{n}"}, "tracks": [],
     "issues": {"album folder holds no audio": ["folder"]}} for n in range(20)]}]}
cap = r.grade_warning(many)
assert len(cap["items"]) == 12 and cap["more"] == 8, (len(cap["items"]), cap["more"])
assert cap["albums_failing"] == 20, cap["albums_failing"]
# …and the Home payload carries THAT object, not a second count of the library.
assert _home["grade_warning"] == r.grade_warning({"artists": []}), _home["grade_warning"]

print("ok")
