#!/usr/bin/env python3
"""Play history — the store, the recording seam, the window maths and /api/top.

Offline and deterministic: the database is a temp file (`plays.db_path` is
monkeypatched), the clock is injected (`plays.window(period, now=…)` /
`top(now=…)` / `record(at=…)`), and the library payload is handed in — nothing
here scans a disk, opens the network or reads the real state dir.

What this pins:

  * one INSERT per playback start, in the API's own forward-slash path form,
    with the track's album folder derived and stored beside it (which is what
    lets albums group in SQL);
  * user scoping: two people on one server never see each other's history;
  * an idempotent schema: initializing twice keeps the rows and does not
    rebuild anything;
  * the window maths, half-open in local time — a play AT a boundary belongs to
    the window that starts there and to no other, proved at the edge of all
    four windows (all / year / month / week);
  * `/api/top` grouping and ordering per kind — three tracks, two albums and
    one artist with distinct counts — most played first, with each row's `plays`
    and whether the library still holds it;
  * the empty-history note (and the different note for a window with nothing in
    it while the store holds plays);
  * the endpoint shapes: `POST /api/plays` (and its 400 for no path) and
    `GET /api/top` (and its 400 for an unknown period or kind);
  * the album/artist folds reading the LIBRARY for names, and a play whose
    album the library no longer holds still counting with the folder's own
    name rather than vanishing.

Run:  python tools/test_plays.py
"""
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi import HTTPException  # noqa: E402

from server import api_plays, plays  # noqa: E402

fails = []


def check(ok, label, extra=""):
    if not ok:
        fails.append(f"{label}{(': ' + str(extra)) if extra else ''}")


tmp = tempfile.mkdtemp(prefix="mlo_plays_")
plays.db_path = lambda: os.path.join(tmp, "data", "plays.db")

# A Monday at noon, so the week / month / year windows all start on the day or
# the first of the month the assertions name.
NOW = datetime(2026, 9, 21, 12, 0, 0).timestamp()
assert datetime.fromtimestamp(NOW).weekday() == 0, "the fixture clock is a Monday"

# ── the fixture library: three tracks, two albums, one artist ───────────────
A1 = "C:/Music/Slowdive/Souvlaki/01 - Alison.flac"
A2 = "C:/Music/Slowdive/Souvlaki/02 - Machine Gun.flac"
B1 = "C:/Music/Ride/Nowhere/01 - Seagull.flac"
GONE = "C:/Music/Gone/Unfiled/01 - Lost.flac"      # not in the library payload

LIB = {"folder": "C:/Music", "artists": [
    {"path": "C:/Music/Slowdive", "name": "Slowdive", "albums": [
        {"path": "C:/Music/Slowdive/Souvlaki",
         "meta": {"ALBUM": "Souvlaki", "ALBUMARTIST": "Slowdive"},
         "tracks": [
             {"path": A1, "tags": {"TITLE": "Alison", "ARTIST": "Slowdive",
                                   "TRACKNUMBER": "1"},
              "tech": {"length": 213.0}},
             {"path": A2, "tags": {"TITLE": "Machine Gun", "ARTIST": "Slowdive",
                                   "TRACKNUMBER": "2"},
              "tech": {"length": 300.0}},
         ]},
        {"path": "C:/Music/Slowdive/Pygmalion",
         "meta": {"ALBUM": "Pygmalion", "ALBUMARTIST": "Slowdive"},
         "tracks": [
             {"path": "C:/Music/Slowdive/Pygmalion/01 - Rutti.flac",
              "tags": {"TITLE": "Rutti", "ARTIST": "Slowdive",
                       "TRACKNUMBER": "1"}},
         ]},
    ]},
    {"path": "C:/Music/Ride", "name": "Ride", "albums": [
        {"path": "C:/Music/Ride/Nowhere",
         "meta": {"ALBUM": "Nowhere", "ALBUMARTIST": "Ride"},
         "tracks": [
             {"path": B1, "tags": {"TITLE": "Seagull", "ARTIST": "Ride",
                                   "TRACKNUMBER": "1"},
              "tech": {"length": 240.0}},
         ]},
    ]},
]}


def rec(path, count=1, user="", at=None):
    """Store *count* plays of *path*, all at the same instant (or `at`)."""
    for _ in range(count):
        plays.record(path, user=user, at=NOW if at is None else at)


def titles(payload):
    return [(row.get("title") or row.get("name"), row["plays"])
            for row in payload["items"]]


# ── 1. the store: one row per start, scoped, idempotent ─────────────────────
print("== the store ==")
check(plays.count() == 0, "a fresh database is empty")
rec(A1)
rows = [dict(r) for r in plays._conn().execute("SELECT * FROM plays")]
check(len(rows) == 1, "one play is one row", rows)
check(rows[0]["path"] == A1 and rows[0]["user"] == ""
      and rows[0]["album"] == "C:/Music/Slowdive/Souvlaki",
      "storing the API path and its derived album folder", rows[0])
check(abs(rows[0]["started_at"] - NOW) < 0.001, "and the instant it started",
      rows[0]["started_at"])

rec(A1)
rec(A1)
check(plays.count() == 3, "a repeat play is another row", plays.count())
check(plays.count(period="week") == 3,
      "and lands in the window that contains it", plays.count(period="week"))

plays.record(A1.replace("/", "\\"), user="alice")
check(plays.count() == 3, "another user's play is not the default user's",
      plays.count())
check(plays.count(user="alice") == 1, "it is that user's", plays.count(user="alice"))
check(plays.count() == 3 and plays.count(user="") == 3,
      "and the default scope never counts it", plays.count(user=""))

before = plays.count()
plays._init()
plays.reset()
plays._conn()
check(plays.count() == before,
      "initializing again is additive — the rows survive", plays.count())

# a path with no library row is still a play (the store records what was played)
rec(GONE)
check(plays.count() == before + 1, "a play of an unfiled path is stored too")

# ── 2. window maths: half-open, local, proved at every edge ─────────────────
print("== windows ==")
year_start = datetime(2026, 1, 1).timestamp()
month_start = datetime(2026, 9, 1).timestamp()
week_start = datetime(2026, 9, 21, 0, 0, 0).timestamp()
check(plays.window("all", NOW) == (None, None), "all is unbounded")
check(plays.window("year", NOW) == (year_start, datetime(2027, 1, 1).timestamp()),
      "the year window is this calendar year", plays.window("year", NOW))
check(plays.window("month", NOW) == (month_start, datetime(2026, 10, 1).timestamp()),
      "the month window is this calendar month", plays.window("month", NOW))
check(plays.window("week", NOW)
      == (week_start, (datetime(2026, 9, 21) + timedelta(days=7)).timestamp()),
      "the week window starts on Monday", plays.window("week", NOW))

plays.reset_history()
EDGE = [
    # (label, instant, what each window must say about it)
    ("a second before last year's end", datetime(2025, 12, 31, 23, 59, 59).timestamp(),
     {"all": True, "year": False, "month": False, "week": False}),
    ("exactly the first instant of the year", year_start,
     {"all": True, "year": True, "month": False, "week": False}),
    ("a second before this month began", datetime(2026, 8, 31, 23, 59, 59).timestamp(),
     {"all": True, "year": True, "month": False, "week": False}),
    ("exactly the first instant of the month", month_start,
     {"all": True, "year": True, "month": True, "week": False}),
    ("a second before this week began", datetime(2026, 9, 20, 23, 59, 59).timestamp(),
     {"all": True, "year": True, "month": True, "week": False}),
    ("exactly Monday midnight", week_start,
     {"all": True, "year": True, "month": True, "week": True}),
]
for label, at, want in EDGE:
    plays.reset_history()
    plays.record(A1, at=at)
    for period, inside in want.items():
        got = plays.count(period=period, now=NOW) == 1
        check(got == inside,
              "a play at %s is %s %s" % (label, "in" if inside else "NOT in", period),
              plays.count(period=period, now=NOW))

# the OPEN end of each window: the first instant of the NEXT one is not this
plays.reset_history()
for at, period in ((datetime(2026, 9, 28).timestamp(), "week"),
                   (datetime(2026, 10, 1).timestamp(), "month"),
                   (datetime(2027, 1, 1).timestamp(), "year")):
    plays.reset_history()
    plays.record(A1, at=at)
    check(plays.count(period=period, now=NOW) == 0,
          "the instant the next %s starts belongs to that window, not this one"
          % period, plays.count(period=period, now=NOW))
    check(plays.count(period="all", now=NOW) == 1,
          "though the store still holds it (all is unbounded)")

# ── 3. /api/top: grouping and ordering per kind ─────────────────────────────
print("== /api/top ==")
plays.reset_history()
rec(A1, 3)
rec(A2, 1)
rec(B1, 2)

tracks = api_plays.top_charts(kind="tracks", lib=LIB, now=NOW)
check(titles(tracks) == [("Alison", 3), ("Seagull", 2), ("Machine Gun", 1)],
      "tracks are ordered most played first, each with its count", titles(tracks))
check(tracks["period"] == "all" and tracks["kind"] == "tracks"
      and tracks["limit"] == plays.DEFAULT_LIMIT,
      "the payload echoes what it answered", tracks)
check(tracks["window"]["start"] is None and tracks["window"]["end"] is None,
      "an unbounded window says so in its bounds", tracks["window"])
check(all(row["kind"] == "track" and row["in_library"] for row in tracks["items"]),
      "every track row is named by the library", tracks["items"])
check(next(r for r in tracks["items"] if r["path"] == A1)["album"] == "Souvlaki",
      "a track row names its album", tracks["items"][0])
check(tracks["note"] == "", "a full answer carries no note")

albums = api_plays.top_charts(kind="albums", lib=LIB, now=NOW)
check(titles(albums) == [("Souvlaki", 4), ("Nowhere", 2)],
      "albums fold their tracks' plays, most played first", titles(albums))
check(albums["items"][0]["path"] == "C:/Music/Slowdive/Souvlaki"
      and albums["items"][0]["artist"] == "Slowdive",
      "an album row carries its folder path and album artist", albums["items"][0])
check("Pygmalion" not in [r["title"] for r in albums["items"]],
      "an album with no plays is not a row", titles(albums))

artists = api_plays.top_charts(kind="artists", lib=LIB, now=NOW)
check(titles(artists) == [("Slowdive", 4), ("Ride", 2)],
      "artists fold their albums' plays", titles(artists))
check(artists["items"][0]["path"] == "C:/Music/Slowdive",
      "an artist row links to the library's artist folder", artists["items"][0])

# the same rows again inside a window, and NOT in a window they fall outside
week = api_plays.top_charts(kind="tracks", period="week", lib=LIB, now=NOW)
check(titles(week) == titles(tracks),
      "every play here is inside this week, so the week chart matches",
      titles(week))
check(week["window"]["start"] == week_start
      and week["window"]["end"] == (datetime(2026, 9, 21) + timedelta(days=7)).timestamp(),
      "and echoes the week's bounds", week["window"])
check(week["window"]["start_iso"].startswith("2026-09-21T00:00"),
      "with an ISO form in local time", week["window"]["start_iso"])

files = api_plays.top_charts(kind="tracks", period="week", lib=LIB,
                             now=datetime(2026, 9, 28, 12).timestamp())
check(files["items"] == [] and files["note"].startswith("no plays in week"),
      "the NEXT week is empty and says why, rather than showing this one",
      (files["items"], files["note"]))
check("6 plays" in files["note"], "naming what the store does hold", files["note"])

# limit, and a kind the caller cannot ask for
limited = api_plays.top_charts(kind="tracks", limit=2, lib=LIB, now=NOW)
check(len(limited["items"]) == 2 and limited["items"][0]["title"] == "Alison",
      "limit bounds the page", titles(limited))

# The window's own totals describe the WINDOW, not the page: 3 + 1 + 2 plays of
# tracks whose measured lengths are 213 + 300 + 240 s.
check(limited["plays_total"] == 6,
      "the payload counts every play in the window, beyond the page",
      limited["plays_total"])
check(limited["listened_seconds"] == 3 * 213 + 300 + 2 * 240,
      "and sums the played tracks' own lengths as the time listened",
      limited["listened_seconds"])
check(limited["listened_plays"] == 6 and limited["listened_unknown"] == 0,
      "every one of them had a length to add",
      (limited["listened_plays"], limited["listened_unknown"]))

# a play whose album the library does not hold still counts, named by folder
rec(GONE, 5)
gone = api_plays.top_charts(kind="tracks", lib=LIB, now=NOW)
lost = next(r for r in gone["items"] if r["path"] == GONE)
check(lost["plays"] == 5 and lost["in_library"] is False
      and lost["title"] == "01 - Lost" and lost["artist"] == "Gone"
      and lost["album"] == "Unfiled",
      "a play the library can no longer name is still counted, by its folders",
      lost)
gone_albums = api_plays.top_charts(kind="albums", lib=LIB, now=NOW)
check([t for t, _ in titles(gone_albums)][0] == "Unfiled",
      "and its album folds under that same folder", titles(gone_albums))

check(gone["plays_total"] == 11 and gone["listened_seconds"] == 3 * 213 + 300 + 2 * 240
      and gone["listened_unknown"] == 5,
      "a play with no length behind it is counted, never guessed into the time",
      (gone["plays_total"], gone["listened_seconds"], gone["listened_unknown"]))

# ── 4. an empty history is a note, never a bare zero ────────────────────────
print("== empty ==")
plays.reset_history()
empty = api_plays.top_charts(kind="albums", lib=LIB, now=NOW)
check(empty["items"] == [] and empty["note"] == plays.EMPTY_NOTE,
      "an empty history answers with the sentence that explains it", empty)
check("actually starts playing" in empty["note"],
      "which says WHEN a play is recorded", empty["note"])

# ── 5. the endpoints ────────────────────────────────────────────────────────
print("== endpoints ==")
plays.reset_history()
out = api_plays.record_play(A1, user="")
check(out["path"] == A1 and out["album"] == "C:/Music/Slowdive/Souvlaki"
      and out["started_at"] > 0,
      "POST /api/plays answers what it wrote", out)
check(plays.count() == 1, "and it really is one row")
check(api_plays.record_play(A1)["path"] == A1,
      "a repeat is a second row", plays.count())
check(plays.count() == 2, "…which it is")
try:
    api_plays.record_play("   ")
    check(False, "POST /api/plays without a path is refused")
except HTTPException as e:
    check(e.status_code == 400 and e.detail == "path required",
          "no path is a 400 with its own sentence", e.detail)

for bad, why in (("tomorrow", "period"), ("films", "kind")):
    try:
        api_plays.top_charts(period=bad) if why == "period" \
            else api_plays.top_charts(kind=bad)
        check(False, f"an unknown {why} is refused")
    except HTTPException as e:
        check(e.status_code == 400 and why in e.detail,
              f"an unknown {why} is a 400 naming the choices", e.detail)
        check("all, year, month, week" in e.detail or "tracks, albums, artists" in e.detail,
              f"…and lists them ({why})", e.detail)

top = api_plays.top_get(period="all", kind="tracks", limit=None)
check(top["kind"] == "tracks" and top["limit"] == plays.DEFAULT_LIMIT,
      "GET /api/top defaults to 50", top["limit"])
check({r.path for r in api_plays.router.routes} == {"/api/plays", "/api/top"},
      "the router serves exactly the two documented paths",
      {r.path for r in api_plays.router.routes})

# ── done ────────────────────────────────────────────────────────────────────
shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print("plays: FAILED")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("plays: all assertions passed")
sys.exit(0)
