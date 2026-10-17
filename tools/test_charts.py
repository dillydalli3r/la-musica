#!/usr/bin/env python3
"""Charts — the library's own play history (`/api/top`).

Offline and deterministic: the play store is a throwaway SQLite file, the
library payload is handed in and the clock is a constant. The online charts
the page used to show beside the history are gone with the discovery feeds
(the provider's rank was never the user's play count), so this suite pins the
windows, the folds and the empty history's own sentence.
"""
import os
import shutil
import sys
import tempfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi import HTTPException  # noqa: E402

from server import api_plays  # noqa: E402
from server import plays  # noqa: E402

passed = 0


def ok(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok: {label}")


# --------------------------------------------------------------------------- #
# The seams
# --------------------------------------------------------------------------- #
CALLS = []


def stub_json(router):
    """`discovery._json` is the ONE transport every provider wrapper here goes
    through, so a router over it exercises the real Deezer / Apple / Last.fm /
    ListenBrainz readers — parsing, ranking and refusal included."""
    def fake(url, params=None, headers=None, timeout=None, ttl=None, host=None):
        CALLS.append(("json", url))
        return router(url, params or {})
    discovery._json = fake


def route(answers):
    """Match a URL fragment to an answer (a value, or a callable taking url)."""
    def go(url, params):
        for fragment, answer in answers:
            if fragment in url:
                return answer(url, params) if callable(answer) else answer
        return None
    return go


def fresh(answers):
    CALLS.clear()
    discovery._CACHE.clear()
    discovery._HOST_WARNED.clear()
    stub_json(route(answers))


def supports_period(payload, sid):
    """The payload's own support matrix for one source."""
    return next(s for s in payload["sources"] if s["id"] == sid)["supports_period"]


TMP = tempfile.mkdtemp(prefix="mlo_charts_")
plays.db_path = lambda: os.path.join(TMP, "data", "plays.db")
NOW = datetime(2026, 9, 21, 12, 0, 0).timestamp()

A1 = "C:/Music/Slowdive/Souvlaki/01 - Alison.flac"
A2 = "C:/Music/Slowdive/Souvlaki/02 - Machine Gun.flac"
B1 = "C:/Music/Ride/Nowhere/01 - Seagull.flac"

LIB = {"folder": "C:/Music", "artists": [
    {"path": "C:/Music/Slowdive", "name": "Slowdive", "albums": [
        {"path": "C:/Music/Slowdive/Souvlaki",
         "meta": {"ALBUM": "Souvlaki", "ALBUMARTIST": "Slowdive"},
         "tracks": [
             {"path": A1, "tags": {"TITLE": "Alison", "ARTIST": "Slowdive"}},
             {"path": A2, "tags": {"TITLE": "Machine Gun", "ARTIST": "Slowdive"}},
         ]},
    ]},
    {"path": "C:/Music/Ride", "name": "Ride", "albums": [
        {"path": "C:/Music/Ride/Nowhere",
         "meta": {"ALBUM": "Nowhere", "ALBUMARTIST": "Ride"},
         "tracks": [
             {"path": B1, "tags": {"TITLE": "Seagull", "ARTIST": "Ride"}},
         ]},
    ]},
]}


# --------------------------------------------------------------------------- #
# 1) /api/top — the library's own history, by kind
# --------------------------------------------------------------------------- #
print("== /api/top ==")
for path, count in ((A1, 3), (A2, 1), (B1, 2)):
    for _ in range(count):
        plays.record(path, at=NOW)

tracks = api_plays.top_charts(kind="tracks", lib=LIB, now=NOW)
ok([(r["title"], r["plays"]) for r in tracks["items"]]
   == [("Alison", 3), ("Seagull", 2), ("Machine Gun", 1)],
   "three tracks with distinct counts, most played first "
   f"({[(r['title'], r['plays']) for r in tracks['items']]})")
ok(all(r["kind"] == "track" and r["in_library"] for r in tracks["items"]),
   "every track row is a library row")
ok(tracks["window"]["start"] is None and tracks["window"]["period"] == "all",
   f"the answer echoes its own window ({tracks['window']})")

albums = api_plays.top_charts(kind="albums", lib=LIB, now=NOW)
ok([(r["title"], r["plays"]) for r in albums["items"]]
   == [("Souvlaki", 4), ("Nowhere", 2)],
   f"two albums, their tracks' plays folded ({albums['items']})")
ok(albums["items"][0]["artist"] == "Slowdive",
   "an album row carries its album artist")

artists = api_plays.top_charts(kind="artists", lib=LIB, now=NOW)
ok([(r["name"], r["plays"]) for r in artists["items"]]
   == [("Slowdive", 4), ("Ride", 2)],
   f"one artist per album artist, distinct counts ({artists['items']})")

plays.reset_history()
empty = api_plays.top_charts(kind="tracks", lib=LIB, now=NOW)
ok(empty["items"] == [] and empty["note"] == plays.EMPTY_NOTE
   and "actually starts playing" in empty["note"],
   f"an empty history is the note that explains it, never a bare zero "
   f"({empty['note']})")

# --------------------------------------------------------------------------- #
# done
# --------------------------------------------------------------------------- #
shutil.rmtree(TMP, ignore_errors=True)
print(f"\nAll {passed} checks passed.")
