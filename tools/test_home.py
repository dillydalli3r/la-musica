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
        "wanted", "needs_attention"} <= set(_home), sorted(_home)
assert _home["stats"]["albums"] == 0 and _home["recent"] == [], _home["stats"]

print("ok")
