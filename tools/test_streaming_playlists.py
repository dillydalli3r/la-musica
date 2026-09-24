#!/usr/bin/env python3
"""Verify importing a playlist from a streaming service.

Fixture-driven and completely offline: the one HTTP seam
(`server.streaming_playlists._get`) answers from a table and FAILS LOUDLY on
any URL the table does not hold, and the YouTube path is the app's own yt-dlp
probe with `server.youtube.flat_playlist` stubbed — so a test that reaches the
network (or shells out to yt-dlp) cannot pass unnoticed. The library is a
synthetic payload and the music folder is a temp directory, so the developer's
real library and playlists are never opened.

One fixture per service, and each one covers what that service's own reading
needs on top of the shared ground:

  * Deezer     — two pages through `tracks.next`, a duplicate across the pages,
                 a `next` that points back at a page already read, an ISRC
                 match, a name match and two unmatched rows.
  * Spotify    — a page plus its paging `next`, the bearer token on both
                 requests, the `fields` narrowing, and the credential error
                 that names `spotify_client_id`.
  * YouTube    — the channel-derived artist ("… - Topic"), the title-only
                 fallback, and the missing-yt-dlp / probe-failed errors.
  * Apple      — the embedded `serialized-server-data` document (header +
                 trackLockup sections) and the page-facts error when the block
                 is not there.

Plus URL recognition (each service, a track/album link pasted where a playlist
link belongs, and a URL pasted for the WRONG service), the report shape, the
create/empty/duplicate rules, and the parent-album path queueing an ALBUM (never
a track).

Run:  python tools/test_streaming_playlists.py   (exit 0 pass, 1 fail)
"""
import atexit
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# hermeticity: mlo.paths resolves the music folder from the config on first use,
# so both are redirected BEFORE server.main (and mlo.paths) is imported. The
# playlists this suite creates land in the temp folder's .mlo/data.
# --------------------------------------------------------------------------- #
REAL_MUSIC_FOLDER = ""
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        REAL_MUSIC_FOLDER = str((json.load(f) or {}).get("music_folder") or "")
except Exception:
    pass

REDIRECT = tempfile.mkdtemp(prefix="mlo-stream-redirect-")
MF = tempfile.mkdtemp(prefix="mlo-stream-test-").replace("\\", "/")
os.makedirs(os.path.join(MF, ".mlo", "data"), exist_ok=True)
os.environ["MLO_MUSIC_FOLDER"] = MF

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB = os.path.join(REDIRECT, "config.json")
with open(_STUB, "w", encoding="utf-8") as f:
    json.dump({"music_folder": MF}, f)

for _mod in (cfgmod, pathmod):
    _mod.CONFIG_FILE = _STUB
    if getattr(_mod, "LEGACY_DATA_DIR", None) is not None:
        _mod.LEGACY_DATA_DIR = os.path.join(REDIRECT, "legacy")

atexit.register(lambda: [shutil.rmtree(d, ignore_errors=True)
                         for d in (REDIRECT, MF)])
atexit.register(lambda: os.environ.pop("MLO_MUSIC_FOLDER", None))

_REAL = REAL_MUSIC_FOLDER.replace("\\", "/").rstrip("/").lower()
if _REAL:
    assert not MF.lower().startswith(_REAL), \
        f"temp fixture {MF} sits inside the real music folder {REAL_MUSIC_FOLDER}"

from server import integrations as intg_mod  # noqa: E402
from server import library as lib_mod  # noqa: E402
from server import playlists as pl_mod  # noqa: E402
from server import streaming_playlists as sp  # noqa: E402
from server import youtube as yt_mod  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}"
          + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


# --------------------------------------------------------------------------- #
# the library fixture — four tracks the import has to find
# --------------------------------------------------------------------------- #
def _track(path, title, artist, album, isrc=""):
    tags = {"TITLE": title, "ARTIST": artist, "ALBUM": album}
    if isrc:
        tags["ISRC"] = isrc
    return {"path": path, "file": os.path.basename(path), "tags": tags}


LIBRARY = {
    "folder": MF,
    "artists": [
        {
            "path": f"{MF}/Alpha", "name": "Alpha",
            "albums": [
                {
                    "path": f"{MF}/Alpha/Album One",
                    "album": "Album One", "album_artist": "Alpha",
                    "meta": {"ALBUM": "Album One", "ALBUMARTIST": "Alpha"},
                    "tracks": [
                        _track(f"{MF}/Alpha/Album One/01 - One.flac", "One",
                               "Alpha", "Album One", "USAAA0000001"),
                        _track(f"{MF}/Alpha/Album One/02 - Two.flac", "Two",
                               "Alpha", "Album One"),
                    ],
                },
            ],
        },
    ],
}

lib_mod.build_library = lambda cfg, progress=None: LIBRARY

# --------------------------------------------------------------------------- #
# the HTTP table: one seam, every fixture, and a hard failure for anything the
# table does not hold (a live request must never look like a pass)
# --------------------------------------------------------------------------- #
ROUTES = {}
SENT = []


class _Live(Exception):
    pass


def fake_get(url, params=None, headers=None, timeout=None, expect="json"):
    SENT.append({"url": url, "params": params or {}, "headers": headers or {}})
    page = ROUTES.get(url)
    if page is None:
        raise _Live(f"the suite has no fixture for {url} — a live request")
    body, error = page
    meta = {"status": 404 if error else 200, "content_type":
            "text/html" if expect == "text" else "application/json",
            "bytes": len(body or ""), "url": url}
    return (body or "" if expect == "text" else body), error, meta


sp._get = fake_get


def track(title, artist, album, isrc="", duration=200, url=""):
    return {"title": title, "artist": artist, "album": album,
            "duration": duration, "isrc": isrc, "url": url, "mbid": ""}


ALBUMS_QUEUED = []
TRACKS_QUEUED = []
sp._queue_album = lambda title, artist, source, page_url: (
    ALBUMS_QUEUED.append({"title": title, "artist": artist, "source": source,
                          "page_url": page_url})
    or {"ok": True, "matched": True, "queued": 1, "albums": [{"mbid": "rel-1"}]})
sp._queue_track = lambda title, artist, source, page_url: (
    TRACKS_QUEUED.append({"title": title, "artist": artist, "source": source})
    or {"ok": True, "matched": False, "by_name": True, "queued": 0,
        "wish_id": 7, "note": "on the queue to be searched by name"})


def reset_queues():
    del ALBUMS_QUEUED[:]
    del TRACKS_QUEUED[:]
    del SENT[:]


BASE_CFG = {"music_folder": MF, "playlist_import_parent_albums": False,
            "playlist_import_unmatched": "skip",
            "playlist_import_create_empty": True}

# --------------------------------------------------------------------------- #
# 1. URL recognition
# --------------------------------------------------------------------------- #
print("\n[1] URL recognition")
CASES = [
    ("https://www.deezer.com/en/playlist/12345", "deezer"),
    ("https://api.deezer.com/playlist/9", "deezer"),
    ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", "spotify"),
    ("https://open.spotify.com/intl-de/playlist/xyz123", "spotify"),
    ("https://music.youtube.com/playlist?list=OLAK5uy_abc", "youtube"),
    ("https://www.youtube.com/playlist?list=PLabc", "youtube"),
    ("https://music.apple.com/us/playlist/todays-hits/pl.f4d106fed2bd41149aaacabb233eb5eb", "apple"),
]
for url, want in CASES:
    got = sp.service_of(url)
    check(f"service_of({url}) → {want}", got == want, got)

for url in ("https://example.com/playlist/1", "not a url",
            "https://open.spotify.com/album/1abc", "https://www.youtube.com/watch?v=abc",
            "https://www.deezer.com/track/116348632"):
    got = sp.service_of(url)
    check(f"service_of({url}) → not a playlist", got == "", got)

try:
    sp.fetch("https://www.deezer.com/track/116348632", BASE_CFG)
    check("a Deezer track link is refused", False, "no error")
except sp.StreamingError as e:
    msg = str(e)
    check("a Deezer track link is refused, as a track link",
          "track link" in msg and "PLAYLIST" in msg, msg)

try:
    sp.fetch("https://example.com/x", BASE_CFG)
    check("an unknown host is refused", False, "no error")
except sp.StreamingError as e:
    check("an unknown host names all four services",
          all(s in str(e) for s in ("Deezer", "Spotify", "YouTube Music", "Apple Music")),
          str(e))

try:
    sp.fetch("https://open.spotify.com/playlist/abc", BASE_CFG, service="deezer")
    check("a Spotify URL pasted for the Deezer endpoint is refused", False, "no error")
except sp.StreamingError as e:
    msg = str(e)
    check("a Spotify URL pasted for the wrong service says whose URL it is",
          "Spotify playlist URL" in msg and "deezer.com/playlist/" in msg, msg)

try:
    sp.fetch("https://open.spotify.com/playlist/abc", BASE_CFG, service="tidal")
    check("an unknown service is refused", False, "no error")
except sp.StreamingError as e:
    check("an unknown service names the four that exist",
          "Deezer, Spotify, YouTube Music or Apple Music" in str(e), str(e))

# --------------------------------------------------------------------------- #
# 2. Deezer: pagination, the loop guard, ISRC / name matching
# --------------------------------------------------------------------------- #
print("\n[2] Deezer")
DZ_URL = "https://www.deezer.com/en/playlist/12345"
DZ_API = "https://api.deezer.com/playlist/12345"
DZ_NEXT = "https://api.deezer.com/playlist/12345/tracks?limit=2&index=2"


def dz_row(title, artist, album, isrc=""):
    return {"id": 1, "title": title, "isrc": isrc, "duration": 210,
            "link": f"https://www.deezer.com/track/{title[:3]}",
            "artist": {"name": artist}, "album": {"title": album}}


ROUTES[DZ_API] = ({
    "id": 12345, "title": "Deezer Mix",
    "tracks": {"data": [
        # matched by ISRC: the title and artist Deezer states are BOTH wrong
        dz_row("Wrong Title", "Wrong Artist", "Album One", isrc="USAAA0000001"),
        # matched by name
        dz_row("Two", "Alpha", "Album One"),
    ], "next": DZ_NEXT},
}, "")
ROUTES[DZ_NEXT] = ({
    # the same track again, in the playlist twice → a duplicate row
    "data": [dz_row("Two", "Alpha", "Album One"),
             dz_row("Missing Song", "Nobody", "Album Two")],
    "next": None,
}, "")

reset_queues()
payload = sp.fetch(DZ_URL, BASE_CFG)
check("Deezer: pagination walks to the second page", len(payload["tracks"]) == 4,
      len(payload["tracks"]))
check("Deezer: the page title is read", payload["title"] == "Deezer Mix",
      payload["title"])
check("Deezer: the service is named", payload["service"] == "deezer"
      and payload["service_label"] == "Deezer", payload["service"])
check("Deezer: the API needs no credential (two requests, none with a token)",
      len(SENT) == 2 and all(not s["headers"] for s in SENT), SENT)

rows = sp.match_tracks(payload["tracks"], library=LIBRARY)
check("Deezer: the ISRC match wins over a wrong title/artist",
      rows[0]["matched"] and rows[0]["path"].endswith("01 - One.flac"), rows[0])
check("Deezer: a name+artist match is a match", rows[1]["matched"], rows[1])
check("Deezer: the unmatched row says why, with the reason the library gives",
      not rows[3]["matched"]
      and "no track in the library has this title" in rows[3]["reason"],
      rows[3]["reason"])
check("Deezer: a row matched by ISRC reports the library's own identity",
      rows[0]["library_title"] == "One" and rows[0]["library_artist"] == "Alpha",
      rows[0])

# the wrong-artist reason: the same title exists, by somebody else
rows2 = sp.match_tracks([track("Two", "Somebody Else", "Album One")], library=LIBRARY)
check("Deezer: a title the library has by another artist says who has it",
      not rows2[0]["matched"] and "by Alpha" in rows2[0]["reason"]
      and "not by Somebody Else" in rows2[0]["reason"], rows2[0]["reason"])
rows3 = sp.match_tracks([{"title": "", "artist": "", "album": ""}], library=LIBRARY)
check("a playlist row with no title says so",
      "names no track title" in rows3[0]["reason"], rows3[0]["reason"])

# the `next` loop guard: page 2's next points back at page 2
ROUTES[DZ_NEXT] = ({"data": [dz_row("Missing Song", "Nobody", "Album Two")],
                    "next": DZ_NEXT}, "")
reset_queues()
looped = sp.fetch(DZ_URL, BASE_CFG)
check("Deezer: a `next` that points back at a page already read stops the walk",
      len(looped["tracks"]) == 3 and len(SENT) == 2, (len(looped["tracks"]), len(SENT)))
ROUTES[DZ_NEXT] = ({
    "data": [dz_row("Two", "Alpha", "Album One"),
             dz_row("Missing Song", "Nobody", "Album Two")],
    "next": None,
}, "")

# a refusal from Deezer itself
ROUTES[DZ_API] = ({"error": {"type": "DataException", "message": "no data",
                             "code": 800}}, "")
try:
    sp.fetch(DZ_URL, BASE_CFG)
    check("Deezer: its own error payload becomes an error", False, "no error")
except sp.StreamingError as e:
    check("Deezer: its own error payload becomes an error, in Deezer's words",
          "no data" in str(e) and "800" in str(e), str(e))
ROUTES[DZ_API] = (None, "HTTP 500: boom")
try:
    sp.fetch(DZ_URL, BASE_CFG)
    check("Deezer: a failed request is an error", False, "no error")
except sp.StreamingError as e:
    check("Deezer: a failed request reports the status and Deezer's words",
          "HTTP 500" in str(e) and "boom" in str(e), str(e))
ROUTES[DZ_API] = ({
    "id": 12345, "title": "Deezer Mix",
    "tracks": {"data": [
        dz_row("Wrong Title", "Wrong Artist", "Album One", isrc="USAAA0000001"),
        dz_row("Two", "Alpha", "Album One"),
    ], "next": DZ_NEXT},
}, "")

# --------------------------------------------------------------------------- #
# 3. Spotify: the token, the fields, the paging, the credential error
# --------------------------------------------------------------------------- #
print("\n[3] Spotify")
SP_URL = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
SP_API = "https://api.spotify.com/v1/playlists/37i9dQZF1DXcBWIGoYBM5M"
SP_PAGE2 = ("https://api.spotify.com/v1/playlists/37i9dQZF1DXcBWIGoYBM5M/tracks"
            "?offset=2&limit=100")
_reported_refusal = {"reason": ""}
intg_mod._spotify_token = lambda cfg, timeout=None: (
    "TOKEN" if (cfg or {}).get("spotify_client_id") else None)
intg_mod.spotify_last_error = lambda since=None: _reported_refusal["reason"]


def sp_item(title, artist, album, isrc="", ms=200000):
    return {"track": {"name": title, "duration_ms": ms,
                      "artists": [{"name": artist}],
                      "album": {"name": album},
                      "external_ids": {"isrc": isrc},
                      "external_urls": {"spotify": "https://open.spotify.com/track/x"}}}


ROUTES[SP_API] = ({
    "name": "Spotify Mix",
    "tracks": {"items": [sp_item("Two", "Alpha", "Album One"),
                         {"track": None}],
               "next": SP_PAGE2},
}, "")
ROUTES[SP_PAGE2] = ({"items": [sp_item("Missing Song", "Nobody", "Album Two")],
                     "next": None}, "")

SP_CFG = dict(BASE_CFG, spotify_client_id="cid", spotify_client_secret="secret")
reset_queues()
sp_payload = sp.fetch(SP_URL, SP_CFG)
check("Spotify: the page and its paging page are both read",
      len(sp_payload["tracks"]) == 3, len(sp_payload["tracks"]))
check("Spotify: the bearer token is sent on both requests, and the fields are narrowed",
      all(s["headers"].get("Authorization") == "Bearer TOKEN" for s in SENT)
      and SENT[0]["params"].get("fields"), SENT)
check("Spotify: a row that names no track is kept as an empty row",
      sp_payload["tracks"][1]["title"] == "", sp_payload["tracks"][1])
check("Spotify: the paging `next` is top-level on the second page",
      len(SENT) == 2, SENT)

try:
    sp.fetch(SP_URL, BASE_CFG)          # no credentials in this config
    check("Spotify without credentials is refused", False, "no error")
except sp.StreamingError as e:
    msg = str(e)
    check("Spotify without credentials names BOTH settings",
          "spotify_client_id" in msg and "spotify_client_secret" in msg
          and "Settings" in msg, msg)

intg_mod._spotify_token = lambda cfg, timeout=None: None
_reported_refusal["reason"] = "HTTP 400: {\"error\": \"invalid_client\"}"
try:
    sp.fetch(SP_URL, SP_CFG)
    check("Spotify with a refused credential is refused", False, "no error")
except sp.StreamingError as e:
    check("Spotify with a refused credential reports Spotify's own words",
          "invalid_client" in str(e), str(e))
intg_mod._spotify_token = lambda cfg, timeout=None: "TOKEN"

ROUTES[SP_API] = (None, "HTTP 404: {\"error\":{\"status\":404,\"message\":\"Not found.\"}}")
try:
    sp.fetch(SP_URL, SP_CFG)
    check("Spotify 404 is an error", False, "no error")
except sp.StreamingError as e:
    check("Spotify's 404 keeps its body and hints at the private-playlist case",
          "Not found." in str(e) and "private" in str(e), str(e))
ROUTES[SP_API] = ({
    "name": "Spotify Mix",
    "tracks": {"items": [sp_item("Two", "Alpha", "Album One"),
                         {"track": None}],
               "next": SP_PAGE2},
}, "")

# --------------------------------------------------------------------------- #
# 4. YouTube Music: the app's own yt-dlp probe
# --------------------------------------------------------------------------- #
print("\n[4] YouTube Music")
YT_URL = "https://music.youtube.com/playlist?list=OLAK5uy_abc"
_youtube = {"entries": None, "error": None, "installed": True,
            "title": "A Music Playlist"}
yt_mod.ytdlp_available = lambda config=None: _youtube["installed"]
yt_mod.flat_playlist = lambda url, config=None: (
    (_youtube["title"], _youtube["entries"]) if _youtube["error"] is None
    else (_ for _ in ()).throw(RuntimeError(_youtube["error"])))
_youtube["entries"] = [
    {"id": "v1", "title": "Two", "channel": "Alpha - Topic", "duration": 200},
    {"id": "v2", "title": "Nobody - Missing Song", "uploader": "",
     "duration": 180},
]
reset_queues()
yt_payload = sp.fetch(YT_URL, BASE_CFG)
check("YouTube: both entries are read", len(yt_payload["tracks"]) == 2,
      yt_payload["tracks"])
check("YouTube: the playlist's own title comes back from the probe",
      yt_payload["title"] == "A Music Playlist", yt_payload["title"])
check("YouTube: a '- Topic' channel is the artist, and the title is kept",
      yt_payload["tracks"][0]["artist"] == "Alpha"
      and yt_payload["tracks"][0]["title"] == "Two", yt_payload["tracks"][0])
check("YouTube: with no channel, 'Artist - Song' splits into both fields",
      yt_payload["tracks"][1]["artist"] == "Nobody"
      and yt_payload["tracks"][1]["title"] == "Missing Song",
      yt_payload["tracks"][1])
check("YouTube: the watch URL is derived from the id",
      yt_payload["tracks"][0]["url"] == "https://www.youtube.com/watch?v=v1",
      yt_payload["tracks"][0]["url"])
yt_rows = sp.match_tracks(yt_payload["tracks"], library=LIBRARY)
check("YouTube: the channel artist is what matches the library",
      yt_rows[0]["matched"] and not yt_rows[1]["matched"], yt_rows)

_youtube["installed"] = False
try:
    sp.fetch(YT_URL, BASE_CFG)
    check("YouTube without yt-dlp is refused", False, "no error")
except sp.StreamingError as e:
    check("YouTube without yt-dlp names Dependencies",
          "yt-dlp" in str(e) and "Dependencies" in str(e), str(e))
_youtube["installed"] = True
_youtube["error"] = "ERROR: Private playlist"
try:
    sp.fetch(YT_URL, BASE_CFG)
    check("a failed probe is refused", False, "no error")
except sp.StreamingError as e:
    check("a failed probe reports yt-dlp's own words and the cookie jar",
          "Private playlist" in str(e) and "cookie" in str(e), str(e))
_youtube["error"] = None
_youtube["entries"] = []
try:
    sp.fetch(YT_URL, BASE_CFG)
    check("an empty playlist is refused", False, "no error")
except sp.StreamingError as e:
    check("an empty playlist says it was read and had no entries",
          "found no entries" in str(e), str(e))
_youtube["entries"] = [{"id": "v1", "title": "Two", "channel": "Alpha - Topic"}]

# --------------------------------------------------------------------------- #
# 5. Apple Music: the page's embedded JSON
# --------------------------------------------------------------------------- #
print("\n[5] Apple Music")
AP_URL = ("https://music.apple.com/us/playlist/todays-hits/"
          "pl.f4d106fed2bd41149aaacabb233eb5eb")


def apple_html(block):
    return ("<!doctype html><html><head><title>x</title></head><body>"
            f'<script id="serialized-server-data" type="application/json">{block}</script>'
            "</body></html>")


APPLE_DOC = json.dumps({"data": [{"data": {"sections": [
    {"id": "playlist-detail-header-section", "itemKind": "containerDetailHeaderLockup",
     "items": [{"id": "header", "title": "Today's Hits"}]},
    {"id": "track-list", "itemKind": "trackLockup", "items": [
        {"id": "t1", "title": "Two", "artistName": "Alpha", "duration": 201000,
         "subtitleLinks": [{"title": "Alpha"}],
         "tertiaryLinks": [{"title": "Album One"}],
         "contentDescriptor": {"kind": "song",
                               "url": "https://music.apple.com/us/album/x?i=1"}},
        {"id": "t2", "title": "Missing Song", "artistName": "Nobody",
         "duration": 180000, "subtitleLinks": [{"title": "Nobody"}],
         "tertiaryLinks": [{"title": "Album Two"}],
         "contentDescriptor": {"kind": "song",
                               "url": "https://music.apple.com/us/album/y?i=2"}},
    ]},
]}}]})
ROUTES[AP_URL] = (apple_html(APPLE_DOC), "")

reset_queues()
ap_payload = sp.fetch(AP_URL, BASE_CFG)
check("Apple: the embedded JSON is parsed into tracks",
      len(ap_payload["tracks"]) == 2, ap_payload["tracks"])
check("Apple: the playlist name comes from the header lockup",
      ap_payload["title"] == "Today's Hits", ap_payload["title"])
check("Apple: duration is milliseconds on the page and seconds here",
      ap_payload["tracks"][0]["duration"] == 201, ap_payload["tracks"][0])
check("Apple: artist and album come from the item's own links",
      ap_payload["tracks"][0]["artist"] == "Alpha"
      and ap_payload["tracks"][0]["album"] == "Album One", ap_payload["tracks"][0])
ap_rows = sp.match_tracks(ap_payload["tracks"], library=LIBRARY)
check("Apple: the tracks match the library like any other service",
      ap_rows[0]["matched"] and not ap_rows[1]["matched"], ap_rows)

ROUTES[AP_URL] = (apple_html("not json at all"), "")
try:
    sp.fetch(AP_URL, BASE_CFG)
    check("Apple: a non-JSON block is an error", False, "no error")
except sp.StreamingError as e:
    check("Apple: a non-JSON block is reported as such",
          "not JSON" in str(e), str(e))

ROUTES[AP_URL] = ("<html><body>a page without the block</body></html>", "")
try:
    sp.fetch(AP_URL, BASE_CFG)
    check("Apple: a page without the block is an error", False, "no error")
except sp.StreamingError as e:
    msg = str(e)
    check("Apple: a page without the block says exactly what came back",
          "serialized-server-data" in msg and "HTTP 200" in msg
          and "text/html" in msg and "KB" in msg, msg)

ROUTES[AP_URL] = (None, "HTTP 403: forbidden")
try:
    sp.fetch(AP_URL, BASE_CFG)
    check("Apple: a refused page is an error", False, "no error")
except sp.StreamingError as e:
    check("Apple: a refused page keeps Apple's own answer, and says why the page is read",
          "HTTP 403" in str(e) and "developer token" in str(e), str(e))
ROUTES[AP_URL] = (apple_html(APPLE_DOC), "")

# --------------------------------------------------------------------------- #
# 6. The report shape and the playlist it makes
# --------------------------------------------------------------------------- #
print("\n[6] the report and the playlist")
reset_queues()
res = sp.import_playlist(DZ_URL, cfg=BASE_CFG)
report = res["report"]
check("the report carries exactly the documented keys",
      set(report) == {"service", "service_label", "source_url", "title", "total",
                      "matched", "unmatched", "duplicates", "tracks",
                      "parent_albums", "unmatched_tracks", "note"}, sorted(report))
check("the report's counts add up",
      report["total"] == 4 and report["matched"] == 3
      and report["unmatched"] == 1 and report["duplicates"] == 1
      and report["matched"] + report["unmatched"] == report["total"], report)
check("every row carries the fetched identity, the match and the reason",
      all(set(r) == {"index", "title", "artist", "album", "duration", "isrc",
                     "url", "matched", "path", "library_title", "library_artist",
                     "library_album", "mbid", "duplicate", "reason", "queued"}
          for r in report["tracks"]), report["tracks"][0])
check("the duplicate row is the SECOND one, and it says so",
      report["tracks"][2]["duplicate"] and not report["tracks"][1]["duplicate"],
      [r["duplicate"] for r in report["tracks"]])
check("nothing is queued with the parent-album option off, and the note says so",
      not ALBUMS_QUEUED and not TRACKS_QUEUED
      and not report["parent_albums"]["enabled"], report["note"])
check("the playlist is created from the matched paths, in order, deduplicated",
      res["created"] and len(res["playlist"]["tracks"]) == 2
      and res["playlist"]["tracks"][0].endswith("01 - One.flac")
      and res["playlist"]["tracks"][1].endswith("02 - Two.flac"),
      res["playlist"])
check("the playlist carries the service and URL it came from",
      res["playlist"]["origin"] == "deezer"
      and res["playlist"]["origin_url"] == DZ_URL, res["playlist"])
check("the playlist name falls back to the service's own title",
      res["playlist"]["name"] == "Deezer Mix", res["playlist"]["name"])

named = sp.import_playlist(DZ_URL, name="Mine", cfg=BASE_CFG)
check("a name the import gives is the name used",
      named["playlist"]["name"] == "Mine", named["playlist"]["name"])

# --------------------------------------------------------------------------- #
# 7. Parent albums — an ALBUM per missing album, never a track
# --------------------------------------------------------------------------- #
print("\n[7] parent albums")
reset_queues()
on = sp.import_playlist(DZ_URL, cfg=dict(BASE_CFG, playlist_import_parent_albums=True))
check("with the option on, the PARENT ALBUM is queued (not the track)",
      [q["title"] for q in ALBUMS_QUEUED] == ["Album Two"], ALBUMS_QUEUED)
check("and no track is queued by name", not TRACKS_QUEUED, TRACKS_QUEUED)
check("the queued album carries the service's own label as its source",
      ALBUMS_QUEUED[0]["source"] == "Deezer", ALBUMS_QUEUED[0])
check("the row of the track whose album was queued says 'album'",
      on["report"]["tracks"][3]["queued"] == "album", on["report"]["tracks"][3])
check("a matched track queues nothing (its album IS the library album it matched)",
      [r["queued"] for r in on["report"]["tracks"][:3]] == ["", "", ""],
      [r["queued"] for r in on["report"]["tracks"][:3]])
check("only the album is reported as queued, once",
      len(on["report"]["parent_albums"]["queued"]) == 1
      and on["report"]["parent_albums"]["queued"][0]["title"] == "Album Two"
      and on["report"]["parent_albums"]["enabled"], on["report"]["parent_albums"])

# an unmatched track whose album IS in the library queues nothing, and says why
ROUTES[DZ_API] = ({
    "title": "Half here",
    "tracks": {"data": [dz_row("Missing Song", "Alpha", "Album One")], "next": None},
}, "")
reset_queues()
in_lib = sp.import_playlist(DZ_URL, cfg=dict(BASE_CFG, playlist_import_parent_albums=True))
check("an album already in the library queues nothing at all",
      not ALBUMS_QUEUED and not TRACKS_QUEUED, ALBUMS_QUEUED)
check("...and the report says the album is already in the library",
      [q["reason"] for q in in_lib["report"]["parent_albums"]["queued"]]
      == ["already in the library"], in_lib["report"]["parent_albums"])
check("...and the row itself queues nothing",
      in_lib["report"]["tracks"][0]["queued"] == "", in_lib["report"]["tracks"][0])

# one album, two missing tracks off it → ONE add
ROUTES[DZ_API] = ({
    "title": "Two off one album",
    "tracks": {"data": [dz_row("Missing One", "Nobody", "Album Two"),
                        dz_row("Missing Two", "Nobody", "Album Two")], "next": None},
}, "")
reset_queues()
twice = sp.import_playlist(DZ_URL, cfg=dict(BASE_CFG, playlist_import_parent_albums=True))
check("one album is queued ONCE, though two rows are off it",
      len(ALBUMS_QUEUED) == 1, ALBUMS_QUEUED)
check("...and both rows report the album that was queued",
      [r["queued"] for r in twice["report"]["tracks"]] == ["album", "album"],
      [r["queued"] for r in twice["report"]["tracks"]])

# a row that names no album (a YouTube row) is reported, not silently dropped
_youtube["entries"] = [{"id": "v9", "title": "Missing Song", "channel": "Nobody"}]
reset_queues()
no_album = sp.import_playlist(YT_URL, cfg=dict(BASE_CFG, playlist_import_parent_albums=True))
check("a row with no album says so instead of queueing nothing quietly",
      any(q.get("reason") == "the playlist row names no album"
          for q in no_album["report"]["parent_albums"]["queued"])
      and not ALBUMS_QUEUED,
      (no_album["report"]["parent_albums"], ALBUMS_QUEUED))
_youtube["entries"] = [{"id": "v1", "title": "Two", "channel": "Alpha - Topic"}]

# restore the main Deezer fixture
ROUTES[DZ_API] = ({
    "id": 12345, "title": "Deezer Mix",
    "tracks": {"data": [
        dz_row("Wrong Title", "Wrong Artist", "Album One", isrc="USAAA0000001"),
        dz_row("Two", "Alpha", "Album One"),
    ], "next": DZ_NEXT},
}, "")

# the per-import override wins over the config, in both directions
reset_queues()
sp.import_playlist(DZ_URL, cfg=dict(BASE_CFG, playlist_import_parent_albums=True),
                   parent_albums=False)
check("the per-import override (off) beats the configured on",
      not ALBUMS_QUEUED, ALBUMS_QUEUED)
reset_queues()
sp.import_playlist(DZ_URL, cfg=BASE_CFG, parent_albums=True)
check("the per-import override (on) beats the configured off",
      [q["title"] for q in ALBUMS_QUEUED] == ["Album Two"], ALBUMS_QUEUED)

# an add that fails must not lose the playlist
def _explode(title, artist, source, page_url):
    from fastapi import HTTPException
    raise HTTPException(503, "MusicBrainz did not answer: timeout")


_real_queue_album = sp._queue_album
sp._queue_album = _explode
reset_queues()
broken = sp.import_playlist(DZ_URL, cfg=dict(BASE_CFG, playlist_import_parent_albums=True))
check("an add that fails is reported, and the playlist is still made",
      broken["created"] and "MusicBrainz did not answer" in
      broken["report"]["parent_albums"]["queued"][0]["error"],
      broken["report"]["parent_albums"])
check("...and the row does not claim it was queued",
      broken["report"]["tracks"][3]["queued"] == "",
      broken["report"]["tracks"][3])
sp._queue_album = _real_queue_album

# --------------------------------------------------------------------------- #
# 8. Unmatched tracks, the empty rule, and the dry run
# --------------------------------------------------------------------------- #
print("\n[8] unmatched / empty / dry run")
reset_queues()
wished = sp.import_playlist(DZ_URL, cfg=dict(BASE_CFG, playlist_import_unmatched="wish"))
check("`unmatched: wish` queues each unmatched TRACK by name",
      [q["title"] for q in TRACKS_QUEUED] == ["Missing Song"], TRACKS_QUEUED)
check("...and the row says a wish was saved",
      wished["report"]["tracks"][3]["queued"] == "wish",
      wished["report"]["tracks"][3])

reset_queues()
both = sp.import_playlist(DZ_URL, cfg=dict(
    BASE_CFG, playlist_import_unmatched="wish", playlist_import_parent_albums=True))
check("with both on, a track whose album was queued is not ALSO queued by name",
      [q["title"] for q in ALBUMS_QUEUED] == ["Album Two"] and not TRACKS_QUEUED,
      (ALBUMS_QUEUED, TRACKS_QUEUED))
check("...and the track row says the album was queued",
      both["report"]["tracks"][3]["queued"] == "album",
      both["report"]["tracks"][3])

ROUTES[DZ_API] = ({
    "title": "Nothing here",
    "tracks": {"data": [dz_row("Missing Song", "Nobody", "Album Two")],
               "next": None},
}, "")
reset_queues()
before = len(pl_mod.list_playlists(""))
empty_off = sp.import_playlist(DZ_URL, cfg=dict(BASE_CFG, playlist_import_create_empty=False))
check("nothing matched + create_empty off → no playlist, and the note says why",
      not empty_off["created"] and empty_off["playlist"] is None
      and "not created empty" in empty_off["report"]["note"]
      and len(pl_mod.list_playlists("")) == before, empty_off["report"]["note"])
empty_on = sp.import_playlist(DZ_URL, cfg=BASE_CFG)
check("nothing matched + the default → the empty playlist is created",
      empty_on["created"] and empty_on["playlist"]["tracks"] == []
      and empty_on["playlist"]["origin"] == "deezer", empty_on["playlist"])

dry = sp.import_playlist(DZ_URL, cfg=BASE_CFG, dry_run=True)
check("a dry run matches and reports, and writes nothing",
      not dry["created"] and dry["playlist"] is None and dry["dry_run"]
      and dry["report"]["total"] == 1 and dry["report"]["matched"] == 0
      and "this was a check" in dry["report"]["note"], dry["report"]["note"])
ROUTES[DZ_API] = ({
    "id": 12345, "title": "Deezer Mix",
    "tracks": {"data": [
        dz_row("Wrong Title", "Wrong Artist", "Album One", isrc="USAAA0000001"),
        dz_row("Two", "Alpha", "Album One"),
    ], "next": DZ_NEXT},
}, "")

# --------------------------------------------------------------------------- #
# 9. the route, over the real app
# --------------------------------------------------------------------------- #
print("\n[9] POST /api/playlists/import/streaming")
try:
    from fastapi.testclient import TestClient
    from server import main as main_mod

    client = TestClient(main_mod.app)
    reset_queues()
    r = client.post("/api/playlists/import/streaming",
                    json={"url": DZ_URL, "name": "From the API"})
    body = r.json() if r.status_code == 200 else {}
    check("the endpoint answers 200 with the report and the playlist",
          r.status_code == 200 and set(body) == {"ok", "report", "playlist",
                                                 "created", "dry_run"},
          f"{r.status_code} {r.text[:200]}")
    check("...the report is the same one the module builds",
          body.get("report", {}).get("total") == 4
          and body["report"]["service_label"] == "Deezer", body.get("report"))
    pid = (body.get("playlist") or {}).get("id")
    check("...the playlist exists and says where it came from",
          bool(pid) and body["playlist"]["origin"] == "deezer"
          and body["playlist"]["origin_url"] == DZ_URL, body.get("playlist"))

    got = client.get(f"/api/playlists/{pid}")
    check("...and GET /api/playlists/{id} carries the origin too",
          got.status_code == 200 and got.json().get("origin") == "deezer"
          and got.json().get("origin_url") == DZ_URL, got.text[:200])
    check("...the store lists it with its origin as well",
          any(p["id"] == pid and p["origin"] == "deezer"
              for p in client.get("/api/playlists").json()),
          client.get("/api/playlists").text[:200])

    r = client.post("/api/playlists/import/streaming",
                    json={"url": "https://example.com/not-a-playlist"})
    check("an unreadable URL is a 400 carrying the sentence",
          r.status_code == 400 and "Deezer" in r.json().get("detail", ""),
          f"{r.status_code} {r.text[:200]}")

    r = client.post("/api/playlists/import/streaming",
                    json={"url": SP_URL, "service": "deezer"})
    check("a URL pasted for the wrong service is a 400 that says so",
          r.status_code == 400 and "Spotify playlist URL" in r.json().get("detail", ""),
          f"{r.status_code} {r.text[:200]}")

    r = client.post("/api/playlists/import/streaming", json={"url": DZ_URL, "dry_run": True})
    check("the endpoint's dry run creates nothing",
          r.status_code == 200 and r.json()["created"] is False
          and r.json()["playlist"] is None, r.text[:200])
except ImportError as e:                                     # pragma: no cover
    print(f"  SKIP  TestClient unavailable: {e}")

# --------------------------------------------------------------------------- #
print(f"\nstreaming playlist import: {'FAILED — ' + ', '.join(FAILED) if FAILED else 'all checks passed'}")
sys.exit(1 if FAILED else 0)
