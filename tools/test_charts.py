#!/usr/bin/env python3
"""Charts — the library's own play history (`/api/top`) and the online charts
(`/api/discover/charts`).

Offline by construction: every provider is stubbed at `server.discovery`'s ONE
transport seam (`_json`, which Deezer, Apple, Last.fm and ListenBrainz all go
through) and the RateYourMusic scrape is stubbed at `integrations.rym_charts`,
so nothing here opens a socket. The play store is a temp database
(`plays.db_path` is monkeypatched) with an injected clock, the library payload
is handed in, and the config is passed to the plain functions (`cfg=`) rather
than read from disk.

What this pins:

  * `/api/top` groups and orders per kind — three tracks, two albums and one
    artist with distinct counts — most played first, each row carrying its
    `plays`, and an empty history answering with the note that explains it;
  * the charts registry: which sources chart which kinds, for which windows
    (RateYourMusic tracks-only, all-time + year; ListenBrainz all four; Deezer,
    iTunes and Last.fm all-time only), and that RateYourMusic is FIRST for
    tracks;
  * a source that answers (Deezer's, Last.fm's, Apple's and ListenBrainz's real
    wrappers over the stubbed transport) — rows with their own rank and score;
  * a source that needs a key: `skipped: no lastfm_api_key`, with `needs`/
    `ready` on the payload's own source list;
  * a source that refuses: `failed: <the provider's own words>`, verbatim;
  * a window a source does not publish: `unsupported: …`, never its all-time
    chart passed off as this week's;
  * RateYourMusic first for tracks, its refusal surfaced verbatim while the
    other sources still answer, its archived-snapshot answer saying so, and its
    cookie/archive rule reused from the genre chain (neither cookie nor archive
    skips it, naming the key to set);
  * the library marking: a chart row the library owns carries its path, one it
    does not offers the add action;
  * the route's validation and the exact set of paths it serves.

Run:  python tools/test_charts.py
"""
import os
import shutil
import sys
import tempfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi import HTTPException  # noqa: E402

from server import api_discover, api_plays, discover, discovery  # noqa: E402
from server import integrations as intg  # noqa: E402
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


def stub_rym(rows=None, error=None):
    """The RateYourMusic scrape seam — the chart source with no API."""
    payload = rows if rows is not None else RYM_ROWS

    def fake(kind="tracks", period="all", limit=50, cfg=None, now=None):
        CALLS.append(("rym", kind, period))
        if error:
            raise error
        return payload
    intg.rym_charts = fake


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

# No library walk, no config file: the payload is handed in and the keys travel
# as arguments.
discover._library = lambda cfg, lib=None: lib if lib is not None else LIB

FULL_CFG = {"discovery_enabled": True, "rym_archive_fallback": True,
            "lastfm_api_key": "key"}
NO_KEY_CFG = {"discovery_enabled": True, "rym_archive_fallback": True}
NO_COOKIE_CFG = {"discovery_enabled": True}

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
# 2) The registry: who charts what, for which windows
# --------------------------------------------------------------------------- #
print("== the charts registry ==")
ok(discover.CHART_ORDER[0] == "rym",
   f"RateYourMusic leads the chart order ({discover.CHART_ORDER})")
ok(discover.CHART_PERIODS == ("all", "year", "month", "week"),
   "the four windows are the app's own")
cat = discover.catalogue(cfg={})
specs = {s["id"]: s for s in cat["sources"]}
ok(specs["rym"]["charts"] == ["tracks"]
   and specs["rym"]["chart_periods"] == ["all", "year"],
   f"RYM charts tracks only, all-time and per year ({specs['rym']})")
ok(specs["listenbrainz"]["chart_periods"] == ["all", "year", "month", "week"],
   "ListenBrainz answers every window (its stats take the range)")
ok(specs["deezer"]["chart_periods"] == ["all"]
   and specs["itunes"]["chart_periods"] == ["all"]
   and specs["lastfm"]["chart_periods"] == ["all"],
   "Deezer, Apple and Last.fm publish no window beyond their current chart")
ok(specs["musicbrainz"]["charts"] == [] and specs["discogs"]["charts"] == [],
   "a source with no chart capability declares none, so it is never asked")
ok(cat["chart_periods"] == list(discover.CHART_PERIODS),
   "the catalogue carries the windows for the settings surface")

# --------------------------------------------------------------------------- #
# 3) The online charts, one outcome at a time
# --------------------------------------------------------------------------- #
print("== /api/discover/charts ==")

DEEZER_TRACKS = {"data": [
    {"id": 1, "title": "Starless", "artist": {"name": "King Crimson"},
     "album": {"title": "Red", "cover_xl": "https://cdn.deezer/red.jpg"},
     "duration": 720, "rank": 900000, "link": "https://www.deezer.com/track/1"},
    {"id": 2, "title": "Alison", "artist": {"name": "Slowdive"},
     "album": {"title": "Souvlaki", "cover_xl": "https://cdn.deezer/souvak.jpg"},
     "duration": 220, "rank": 800000, "link": "https://www.deezer.com/track/2"},
]}
APPLE_TRACKS = {"feed": {"results": [
    {"id": "3", "name": "Seagull", "artistName": "Ride",
     "artworkUrl100": "https://is1.mzstatic/x/100x100bb.jpg",
     "releaseDate": "1990-10-15", "url": "https://music.apple.com/album/3"},
]}}
LASTFM_TRACKS = {"tracks": {"track": [
    {"name": "Machine Gun", "artist": {"name": "Slowdive"},
     "playcount": "1234567", "url": "https://www.last.fm/music/x/_/Machine+Gun",
     "image": [{"size": "mega", "#text": "https://lastfm/mg.jpg"}]},
]}}
LB_TRACKS = {"payload": {"recordings": [
    {"track_name": "When You Sleep", "artist_name": "my bloody valentine",
     "release_name": "Loveless", "recording_mbid": "aaaaaaaa-0000-0000-0000-000000000001",
     "listen_count": 987654,
     "caa_release_mbid": "bbbbbbbb-0000-0000-0000-000000000001"},
]}}
RYM_ROWS = {"chart": "Best songs of all time", "total": 40, "rows": [
    {"kind": "track", "title": "Alison", "artist": "Slowdive",
     "link": "https://rateyourmusic.com/song/slowdive/alison/", "rank": 1,
     "popularity": None, "popularity_label": None, "source": "rym"},
    {"kind": "track", "title": "Starless", "artist": "King Crimson",
     "link": "https://rateyourmusic.com/song/king-crimson/starless/", "rank": 2,
     "popularity": None, "popularity_label": None, "source": "rym"},
]}

ANSWERS = [
    ("api.deezer.com/chart/0/tracks", DEEZER_TRACKS),
    ("rss.applemarketingtools.com", APPLE_TRACKS),
    ("ws.audioscrobbler.com", LASTFM_TRACKS),
    ("api.listenbrainz.org", LB_TRACKS),
]

# ── everything answers ──────────────────────────────────────────────────────
fresh(ANSWERS)
stub_rym()
all_tracks = api_discover.charts_list(period="all", kind="tracks", cfg=FULL_CFG)
by_source = {}
for row in all_tracks["items"]:
    by_source.setdefault(row["source"], []).append(row)
ok(all_tracks["sources_asked"][0] == "rym",
   f"RateYourMusic is asked FIRST for tracks ({all_tracks['sources_asked']})")
ok([r["source"] for r in all_tracks["items"][:2]] == ["rym", "rym"]
   and all_tracks["items"][0]["rank"] == 1,
   "and its rows lead the page in RYM's own order")
ok(all_tracks["items"][0]["reason"] == "Chart #1 · Best songs of all time",
   f"a RYM row says which chart and where it sits "
   f"({all_tracks['items'][0]['reason']})")
ok(set(by_source) == {"rym", "deezer", "itunes", "lastfm", "listenbrainz"},
   f"every chart source that answered contributed ({sorted(by_source)})")
ok([r["rank"] for r in by_source["deezer"]] == [1, 2],
   "Deezer rows keep Deezer's order as their rank")
ok(by_source["lastfm"][0]["score_label"] == "1.2M listens"
   and by_source["lastfm"][0]["score"] == 1234567,
   f"a Last.fm row carries Last.fm's own playcount "
   f"({by_source['lastfm'][0]['score_label']})")
ok(by_source["listenbrainz"][0]["mbid"]
   == "aaaaaaaa-0000-0000-0000-000000000001",
   "a ListenBrainz row is MBID-native, so the add action has a real id")
ok(by_source["itunes"][0]["score"] is None,
   "Apple states no number on its feed, so its score is null — not invented")
ok(by_source["deezer"][1]["owned"] is True
   and by_source["deezer"][1]["path"] == A1,
   f"a chart row the library already holds carries its path "
   f"({by_source['deezer'][1]['path']})")
ok(by_source["deezer"][0]["owned"] is False
   and by_source["deezer"][0]["path"] is None,
   "…and one it does not own offers the add action")
ok(all_tracks["notes"] == {},
   f"a full answer carries no notes ({all_tracks['notes']})")
ok(set(by_source["deezer"][0]) >= {"rank", "score", "score_label"},
   "every chart row carries its rank and score on top of the shared shape")
matrix = {s["id"]: s for s in all_tracks["sources"]}
ok(matrix["rym"]["supports_period"] is True
   and matrix["rym"]["periods"] == ["all", "year"]
   and matrix["lastfm"]["missing"] == []
   and matrix["lastfm"]["ready"] is True,
   f"the payload's source list is the support matrix for THIS request "
   f"({matrix['rym']})")

# ── a source that needs a key ───────────────────────────────────────────────
fresh(ANSWERS)
stub_rym()
keyless = api_discover.charts_list(period="all", kind="tracks", cfg=NO_KEY_CFG)
ok(keyless["notes"]["lastfm"] == "skipped: no lastfm_api_key",
   f"an unconfigured source is skipped by name ({keyless['notes']})")
ok(not any(r["source"] == "lastfm" for r in keyless["items"]),
   "and contributes no rows")
ok({s["id"]: s for s in keyless["sources"]}["lastfm"]["missing"]
   == ["lastfm_api_key"],
   "while the source list still names what it needs")
ok("rym" not in keyless["notes"],
   "RYM IS asked — with the archive fallback on it has a route without a cookie")

# ── a source that refuses: the provider's own words, verbatim ───────────────
def refuse_apple(url, params):
    discovery._record_http_error("rss.applemarketingtools.com", url, 403,
                                 "Forbidden: this storefront is not available")
    return None


fresh([("rss.applemarketingtools.com", refuse_apple)]
      + [(f, a) for f, a in ANSWERS if "rss.applemarketingtools.com" not in f])
stub_rym()
refused = api_discover.charts_list(period="all", kind="tracks", cfg=FULL_CFG)
ok(refused["notes"]["itunes"].startswith(
    "failed: HTTP 403: Forbidden: this storefront is not available"),
   f"a refusal is reported in the provider's own words ({refused['notes']['itunes']})")
ok(refused["items"] and {r["source"] for r in refused["items"]}
   == {"rym", "deezer", "lastfm", "listenbrainz"},
   "…and the other sources still answer")
ok(not any(r["source"] == "itunes" for r in refused["items"]),
   "a refused source contributes no rows, never a placeholder")

# ── a window a source does not publish ──────────────────────────────────────
fresh(ANSWERS)
stub_rym()
weekly = api_discover.charts_list(period="week", kind="tracks", cfg=FULL_CFG)
ok(weekly["notes"]["lastfm"] == "unsupported: Last.fm publishes no weekly chart "
                               "— it charts all-time",
   f"a provider with no such window says so ({weekly['notes']['lastfm']})")
ok(weekly["notes"]["rym"].startswith("unsupported: RateYourMusic publishes no "
                                     "weekly chart"),
   f"…RYM too, rather than an all-time list passed off as this week's "
   f"({weekly['notes']['rym']})")
ok(weekly["notes"]["deezer"].startswith("unsupported: Deezer")
   and weekly["notes"]["itunes"].startswith("unsupported: iTunes"),
   "…and each of them lists the windows it DOES publish "
   f"({weekly['notes']['deezer']})")
ok([r["source"] for r in weekly["items"]] == ["listenbrainz"],
   f"and the one source with a real weekly window answered "
   f"({[r['source'] for r in weekly['items']]})")
ok(weekly["period"] == "week" and weekly["kind"] == "tracks"
   and weekly["limit"] == 50,
   "the payload echoes the window it answered")
ok(supports_period(weekly, "listenbrainz") is True
   and supports_period(weekly, "rym") is False,
   "the support matrix says which source can answer THIS window")
ok([c for c in CALLS if c[0] == "rym"] == [],
   "and no unsupported source was even asked")

# ── RYM refusing: its own words, and the rest of the page survives ──────────
fresh([("api.deezer.com/chart/0/tracks", DEEZER_TRACKS),
       ("api.listenbrainz.org", LB_TRACKS)])
stub_rym(error=RuntimeError(
    "RateYourMusic refused: HTTP 403; Cloudflare challenge instead of a page; "
    "the rym_cookie has expired or is not for this network "
    "(https://rateyourmusic.com/charts/top/song/all-time)"))
rym_refused = api_discover.charts_list(period="all", kind="tracks", cfg=FULL_CFG)
ok(rym_refused["notes"]["rym"].startswith("failed: RateYourMusic refused: "
                                          "HTTP 403; Cloudflare challenge"),
   f"RYM's refusal is surfaced verbatim ({rym_refused['notes']['rym']})")
ok("rym_cookie" in rym_refused["notes"]["rym"],
   "including what the user can do about it")
ok({r["source"] for r in rym_refused["items"]} == {"deezer", "listenbrainz"},
   "and the other sources still answer — one blocked source is not the page")

# ── RYM answering from the archive ──────────────────────────────────────────
fresh([("api.deezer.com/chart/0/tracks", DEEZER_TRACKS)])
stub_rym(rows={"chart": "Best songs of 2025", "total": None,
               "rows": RYM_ROWS["rows"],
               "archive": {"snapshot": "2021-03-25",
                           "url": "https://web.archive.org/x"}})
archived = api_discover.charts_list(period="year", kind="tracks", cfg=FULL_CFG)
ok(archived["notes"]["rym"].startswith("answered from an archived snapshot"),
   f"an archived answer says so rather than looking like a live one "
   f"({archived['notes']['rym']})")
ok([r["source"] for r in archived["items"]][:2] == ["rym", "rym"],
   "…and its rows still lead the tracks page")

# ── RYM with neither a cookie nor the archive route ─────────────────────────
fresh([("api.deezer.com/chart/0/tracks", DEEZER_TRACKS)])
stub_rym()
no_cookie = api_discover.charts_list(period="all", kind="tracks", cfg=NO_COOKIE_CFG)
ok(no_cookie["notes"]["rym"].startswith("skipped: no rym_cookie"),
   f"without a cookie and with the archive off, RYM is skipped by name "
   f"({no_cookie['notes']['rym']})")
ok([c for c in CALLS if c[0] == "rym"] == [],
   "…without spending a request on it")

# ── a kind a source cannot chart, an unknown source, and nothing at all ─────
fresh([("api.deezer.com/chart/0/albums", {"data": []})])
stub_rym()
one = api_discover.charts_list(kind="albums", source="rym", cfg=FULL_CFG)
ok(one["items"] == [] and one["notes"]["rym"].startswith("cannot chart albums:"),
   f"a source asked for a kind it does not chart says so ({one['notes']})")
ok("Tracks only" in one["notes"]["rym"],
   "with the registry's own note about what it does chart")
missing = api_discover.charts_list(kind="tracks", source="nope", cfg=FULL_CFG)
ok(missing["items"] == [] and missing["notes"] == {"nope": "unknown source"}
   and missing["sources_asked"] == [],
   f"an unknown source is an empty answer that says so ({missing['notes']})")

fresh([("api.deezer.com/chart/0/tracks", {"data": []}),
       ("rss.applemarketingtools.com", {"feed": {"results": []}}),
       ("ws.audioscrobbler.com", {"tracks": {"track": []}}),
       ("api.listenbrainz.org", {"payload": {"recordings": []}})])
stub_rym(rows={"chart": "Best songs of all time", "total": None, "rows": []})
nothing = api_discover.charts_list(period="all", kind="tracks", cfg=FULL_CFG)
ok(nothing["items"] == []
   and nothing["notes"]["charts"].startswith("no chart source had anything"),
   f"an empty page explains itself rather than showing a blank panel "
   f"({nothing['notes']})")

# --------------------------------------------------------------------------- #
# 4) the route's own contract
# --------------------------------------------------------------------------- #
print("== route validation ==")
for call, why in (
        (lambda: api_discover.charts_list(period="decade", cfg=FULL_CFG), "period"),
        (lambda: api_discover.charts_list(kind="films", cfg=FULL_CFG), "kind")):
    try:
        call()
        raise AssertionError(f"an invalid {why} was accepted")
    except HTTPException as e:
        ok(e.status_code == 400 and why in e.detail,
           f"an invalid {why} is a 400 naming the choices ({e.detail})")
ok({r.path for r in api_discover.router.routes}
   == {"/api/discover/genres", "/api/discover/genre", "/api/discover/recommended",
       "/api/discover/charts"},
   "the discover router serves the charts route beside the other three")

# --------------------------------------------------------------------------- #
# done
# --------------------------------------------------------------------------- #
shutil.rmtree(TMP, ignore_errors=True)
print(f"\nAll {passed} checks passed.")
