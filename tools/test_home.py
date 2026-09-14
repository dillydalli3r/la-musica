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

print("ok")
