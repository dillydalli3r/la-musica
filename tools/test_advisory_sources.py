#!/usr/bin/env python3
"""Advisory sources (server.integrations) — offline contract.

What this pins, with the HTTP layer stubbed (no network at all):

  * Deezer's per-track ISRC lookup is the PRIMARY source and maps
    explicit_content_lyrics / explicit_lyrics to 0/1/2 — and an unclassified
    field is NO answer, not a guess;
  * Apple's album route resolves the collection (artist match + title
    similarity + trackCount) and maps the file to a track by disc/track, then
    by exact title — and a `cleaned` entry is NO ANSWER (never 0, never 2);
  * Apple's song search accepts only an exact normalized title (and artist)
    hit, and a `cleaned` hit is rejected;
  * Spotify is asked ONLY when `spotify_client_id`/`spotify_client_secret`
    are configured, and only a hit whose own external_ids.isrc matches;
  * a value is never returned without the source that stated it, and an
    unknown track writes NO ITUNESADVISORY (fetch_advisories reports
    updated=0) — an existing valid value is left alone;
  * RateYourMusic sends browser-like headers plus the configured `rym_cookie`,
    treats a blocked/challenge answer as unreachable, logs ONE line per
    process and returns None so the genre chain falls through.

Run:  python tools/test_advisory_sources.py
"""
import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import integrations as intg

ISRC = "USRC17607839"
# No Spotify credentials, and no RYM cookie: the documented "source skipped"
# state. Tests that need them build their own dict.
CFG = {}
# The real transport seam, saved before any stub replaces it.
_REAL_ADVISORY_JSON = intg._advisory_json


def stub_http(routes):
    """Replace integrations' two HTTP seams.

    `routes` maps a URL substring to a payload (or to a callable taking the
    request's params/data). Returns the call log: (METHOD, url, params,
    headers) tuples.
    """
    calls = []

    def _route(url, arg):
        for key, payload in routes.items():
            if key in url:
                return payload(arg) if callable(payload) else payload
        return None

    def fake_get(url, params=None, headers=None, timeout=None, host=None):
        calls.append(("GET", url, dict(params or {}), dict(headers or {})))
        return _route(url, params or {})

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append(("POST", url, dict(data or {}), dict(headers or {})))
        return _route(url, data or {})

    intg._advisory_json, intg._advisory_post = fake_get, fake_post
    return calls


def clear():
    intg._ADVISORY_CACHE.clear()
    intg._SPOTIFY_TOKEN.clear()
    return stub_http({})


def gets(calls, url_part):
    return [c for c in calls if c[0] == "GET" and url_part in c[1]]


# --------------------------------------------------------------------------- #
# 1) Deezer ISRC — the primary source
# --------------------------------------------------------------------------- #
# (payload, expected value) — every one of these is a real shape Deezer
# returns; `explicit_content_lyrics` is observed as 0, 1 and 3 in the wild.
DEEZER_CASES = [
    ({"explicit_lyrics": True, "explicit_content_lyrics": 1}, 1),
    ({"explicit_lyrics": True, "explicit_content_lyrics": 0}, 1),
    # explicit content (artwork) is still an explicit statement — this app's 2
    # means "safe", so it may never be the value for an explicit flag
    ({"explicit_lyrics": False, "explicit_content_lyrics": 2}, 1),
    ({"explicit_lyrics": False, "explicit_content_lyrics": 0}, 0),
    # Deezer leaves it unclassified (3) but states the lyrics are not explicit
    ({"explicit_lyrics": False, "explicit_content_lyrics": 3}, 0),
    # nothing classified either way → no answer
    ({"explicit_content_lyrics": 3}, None),
    ({"error": {"type": "DataException", "message": "no data"}}, None),
]
for payload, want in DEEZER_CASES:
    clear()
    calls = stub_http({"api.deezer.com": payload})
    route = intg.resolve_advisory_route(isrc=ISRC, cfg=CFG)
    assert route["value"] == want, (payload, route)
    assert bool(route["source"]) == (want is not None), route
    if want is not None:
        assert route["source"] == "deezer-isrc", route
    assert intg.resolve_advisory(isrc=ISRC, cfg=CFG) == want, (payload, route)
    assert f"track/isrc:{ISRC}" in gets(calls, "api.deezer.com")[0][1]
    # the same track is asked once: the memo (not the network) answers again
    assert len(gets(calls, "api.deezer.com")) == 1, calls

# An ISRC Deezer does not hold falls through to the album route rather than
# inventing a value.
clear()
stub_http({"api.deezer.com": {"error": {"type": "DataException"}},
           "itunes.apple.com/search": {"results": []}})
route = intg.resolve_advisory_route(isrc=ISRC, artist="Rihanna", album="Loud",
                                    title="S&M", disc=1, track=1, cfg=CFG)
assert route == {"value": None, "source": None,
                 "checked": ["deezer-isrc", "apple-album", "itunes-song"]}, route

# --------------------------------------------------------------------------- #
# 2) Apple album route — album → collection → track
# --------------------------------------------------------------------------- #
ALBUM_HITS = {"results": [
    {"collectionId": 9, "collectionName": "Loud", "artistName": "Some Cover Band",
     "trackCount": 4},
    {"collectionId": 111, "collectionName": "Loud", "artistName": "Rihanna",
     "trackCount": 3},
    {"collectionId": 222, "collectionName": "Loud", "artistName": "Rihanna",
     "trackCount": 4},
]}
SONGS = {"results": [
    {"wrapperType": "collection", "collectionId": 222, "trackCount": 4},
    {"wrapperType": "track", "collectionId": 222, "discNumber": 1, "trackNumber": 1,
     "trackName": "S&M", "trackExplicitness": "explicit",
     "contentAdvisoryRating": "Explicit"},
    {"wrapperType": "track", "collectionId": 222, "discNumber": 1, "trackNumber": 2,
     "trackName": "What's My Name?", "trackExplicitness": "cleaned",
     "contentAdvisoryRating": "Clean"},
    {"wrapperType": "track", "collectionId": 222, "discNumber": 2, "trackNumber": 1,
     "trackName": "Love the Way You Lie", "trackExplicitness": "notExplicit"},
]}
APPLE = {"api.deezer.com": {"error": {"type": "DataException"}},
         "itunes.apple.com/search": ALBUM_HITS,
         "itunes.apple.com/lookup": SONGS}


def apple(**ctx):
    clear()
    calls = stub_http(APPLE)
    ctx.setdefault("isrc", "")
    route = intg.resolve_advisory_route(cfg=CFG, **ctx)
    return route, calls


# disc/track wins over the title: the file's own position decides, so a
# mistitled file still gets ITS track's rating, and the collection whose
# trackCount matches (222, not 111) is the one looked up first.
route, calls = apple(artist="Rihanna", album="Loud", title="Wrong Title",
                     disc=2, track=1, track_count=4)
assert route["value"] == 0 and route["source"] == "apple-album", route
lookup = gets(calls, "itunes.apple.com/lookup")[0]
assert lookup[2]["id"] == 222, lookup

# exact normalized title fallback when the file states no position
route, _ = apple(artist="Rihanna", album="Loud", title="S&M")
assert route["value"] == 1 and route["source"] == "apple-album", route

# `cleaned` is NO ANSWER: not 0, not 2, and no other track's rating either
route, _ = apple(artist="Rihanna", album="Loud", title="What's My Name?",
                 disc=1, track=2)
assert route["value"] is None and route["source"] is None, route
route, _ = apple(artist="Rihanna", album="Loud", title="What's My Name?")
assert route["value"] is None and route["source"] is None, route

# a different artist's collection is not this album
route, _ = apple(artist="Nobody At All", album="Loud", title="S&M",
                 disc=1, track=1)
assert route["value"] is None and route["source"] is None, route


# A collection that can only state `cleaned` is not the end of the road: the
# next candidate (same album, same track count, so the position still
# identifies the track) answers instead.
FALLBACK = {"api.deezer.com": {"error": {"type": "DataException"}},
            "itunes.apple.com/search": {"results": [
                {"collectionId": 501, "collectionName": "Loud",
                 "artistName": "Rihanna", "trackCount": 4},
                {"collectionId": 502, "collectionName": "Loud",
                 "artistName": "Rihanna", "trackCount": 4}]},
            "itunes.apple.com/lookup": lambda params: (
                {"results": [
                    {"wrapperType": "track", "discNumber": 1, "trackNumber": 2,
                     "trackName": "What's My Name?",
                     "trackExplicitness": "cleaned"}]}
                if params.get("id") == 501 else
                {"results": [
                    {"wrapperType": "track", "discNumber": 1, "trackNumber": 2,
                     "trackName": "What's My Name?",
                     "trackExplicitness": "notExplicit"}]})}
clear()
calls = stub_http(FALLBACK)
route = intg.resolve_advisory_route(artist="Rihanna", album="Loud",
                                    title="What's My Name?", disc=1, track=2,
                                    track_count=4, cfg=CFG)
assert route["value"] == 0 and route["source"] == "apple-album", route
assert [c[2]["id"] for c in gets(calls, "itunes.apple.com/lookup")] == [501, 502], calls

# --------------------------------------------------------------------------- #
# 3) Spotify — optional, configured only
# --------------------------------------------------------------------------- #
SPOTIFY_CFG = {"spotify_client_id": "cid", "spotify_client_secret": "sec"}
TOKEN = {"access_token": "tok", "expires_in": 3600}


def spotify(items, dz=None):
    clear()
    calls = stub_http({"accounts.spotify.com": TOKEN,
                       "api.spotify.com": {"tracks": {"items": items}},
                       "api.deezer.com": dz or {"error": {"type": "DataException"}}})
    return calls, intg.resolve_advisory_route(isrc=ISRC, cfg=SPOTIFY_CFG)


HIT = {"explicit": True, "external_ids": {"isrc": ISRC}}

# configured: the Deezer miss is answered by Spotify, with the bearer token
calls, route = spotify([HIT])
assert route == {"value": 1, "source": "spotify-isrc",
                 "checked": ["deezer-isrc", "spotify-isrc"]}, route
assert gets(calls, "api.spotify.com")[0][3].get("Authorization") == "Bearer tok", calls
assert "isrc:" + ISRC in gets(calls, "api.spotify.com")[0][2]["q"]
calls, route = spotify([{"explicit": False, "external_ids": {"isrc": ISRC}}])
assert route["value"] == 0 and route["source"] == "spotify-isrc", route

# an explicit track wins over the clean edition's rating only by identity: a
# hit for a DIFFERENT isrc is not this track
calls, route = spotify([{"explicit": True, "external_ids": {"isrc": "GBAYE0000001"}}])
assert route["value"] is None and route["source"] is None, route

# unconfigured: Spotify is never called, and its absence changes nothing
clear()
calls = stub_http({"api.deezer.com": {"error": {"type": "DataException"}}})
route = intg.resolve_advisory_route(isrc=ISRC, cfg=CFG)
assert route == {"value": None, "source": None, "checked": ["deezer-isrc"]}, route
assert not gets(calls, "accounts.spotify.com"), calls
assert not gets(calls, "api.spotify.com"), calls

# --------------------------------------------------------------------------- #
# 4) The transport really carries a caller's headers (Spotify's bearer token)
# --------------------------------------------------------------------------- #
from server import discovery as disc


class _FakeURL:
    def __init__(self, url):
        self.host = url.split("/")[2]


class _FakeResp:
    status_code = 200

    def json(self):
        return {"ok": True}


class _FakeDiscoveryHttpx:
    def __init__(self):
        self.sent = []

    def URL(self, url):
        return _FakeURL(url)

    def get(self, url, params=None, headers=None, timeout=None,
            follow_redirects=None):
        self.sent.append({"url": url, "headers": dict(headers or {})})
        return _FakeResp()


_real_disc_httpx = disc.httpx
try:
    fake = _FakeDiscoveryHttpx()
    disc.httpx = fake
    # the real transport, not the stub the sections above installed
    intg._advisory_json = _REAL_ADVISORY_JSON
    disc.invalidate()
    assert intg._advisory_json(
        "https://api.spotify.com/v1/search",
        {"q": "isrc:" + ISRC},
        headers={"Authorization": "Bearer tok"},
        host="api.spotify.com") == {"ok": True}
    sent = fake.sent[-1]["headers"]
    assert sent.get("Authorization") == "Bearer tok", sent
    assert sent.get("Accept") == "application/json", sent
    # and the storefronts still get the browser UA
    disc.invalidate()
    intg._advisory_json("https://api.deezer.com/track/isrc:" + ISRC)
    assert "Mozilla" in fake.sent[-1]["headers"].get("User-Agent", ""), fake.sent
finally:
    disc.httpx = _real_disc_httpx
    disc.invalidate()

# --------------------------------------------------------------------------- #
# 5) iTunes song search — exact title only, never a cleaned hit
# --------------------------------------------------------------------------- #
SONG_SEARCH = {"results": [
    {"trackName": "S&M (Live)", "artistName": "Rihanna",
     "trackExplicitness": "explicit"},
]}


def song_search(results, artist="Rihanna", title="S&M"):
    clear()
    calls = stub_http({"api.deezer.com": {"error": {"type": "DataException"}},
                       "itunes.apple.com/search": {"results": results}})
    return calls, intg.resolve_advisory_route(title=title, artist=artist, cfg=CFG)


# a near-miss title is NOT rated (a search hit is a candidate, not identity)
calls, route = song_search(SONG_SEARCH["results"])
assert route["value"] is None and route["source"] is None, route

# exact title + stated explicitness → accepted
calls, route = song_search(SONG_SEARCH["results"] + [
    {"trackName": "S&M", "artistName": "Rihanna", "trackExplicitness": "notExplicit"}])
assert route["value"] == 0 and route["source"] == "itunes-song", route

# ... but a CLEANED hit never is (it is an edited master, not a rating)
calls, route = song_search([{"trackName": "S&M", "artistName": "Rihanna",
                             "trackExplicitness": "cleaned",
                             "contentAdvisoryRating": "Clean"}])
assert route["value"] is None and route["source"] is None, route
assert route["checked"] == ["itunes-song"], route

# a same-title hit by another artist is not this track
calls, route = song_search([{"trackName": "S&M", "artistName": "Some Cover Band",
                             "trackExplicitness": "explicit"}])
assert route["value"] is None and route["source"] is None, route

# --------------------------------------------------------------------------- #
# 6) fetch_advisories: only a stated value is written
# --------------------------------------------------------------------------- #
from mlo import audio as mlo_audio
from server import imports


class FakeAudio:
    """Tag-reading stand-in for mlo.audio.AudioFile."""

    written = {}

    def __init__(self, path):
        self.path = path
        self.audio = object()
        self.tags = dict(FakeAudio.written.get(path) or {})

    def get_tag(self, name):
        return self.tags.get(name)

    def set_tag(self, name, value):
        self.tags[name] = value
        FakeAudio.written[self.path] = dict(self.tags)
        return True


ROOT = tempfile.mkdtemp(prefix="mlo_advisory_")
ALBUM = os.path.join(ROOT, "Loud")
os.makedirs(ALBUM)
FILES = []
for i, (title, isrc, current) in enumerate([
        ("S&M", ISRC, ""),                     # Deezer answers 1
        ("Love the Way You Lie", "USUM71000001", "2"),  # user's edit wins
        ("Complicated", "USUM71000002", "")], 1):
    path = os.path.join(ALBUM, f"{i:02d} - {title}.wav")
    with open(path, "wb") as fh:
        fh.write(b"")
    tags = {"TITLE": title, "ARTIST": "Rihanna", "ALBUM": "Loud",
            "DISCNUMBER": "1", "TRACKNUMBER": str(i)}
    if isrc:
        tags["ISRC"] = isrc
    if current:
        tags["ITUNESADVISORY"] = current
    FakeAudio.written[path] = tags
    FILES.append(path)

_real_audiofile = mlo_audio.AudioFile
mlo_audio.AudioFile = FakeAudio
try:
    # nobody states a value → no tag at all (an absent advisory is "unrated");
    # the track that already carries a valid 2 is reported, not rewritten
    clear()
    stub_http({})
    out = imports.fetch_advisories([ALBUM], {"advisory_auto_fetch": True})
    assert out == {"updated": 0, "values": {FILES[1]: 2}, "sources": {}}, out
    assert all("ITUNESADVISORY" not in (FakeAudio.written.get(p) or {})
               or (FakeAudio.written[p].get("ITUNESADVISORY") == "2")
               for p in FILES), FakeAudio.written

    # Deezer answers for the first track only: exactly that one is written,
    # the user's existing 2 is reported but never overwritten
    clear()
    calls = stub_http({"api.deezer.com/track/isrc:" + ISRC:
                       {"explicit_lyrics": True, "explicit_content_lyrics": 1},
                       "api.deezer.com": {"error": {"type": "DataException"}},
                       "itunes.apple.com/search": {"results": []},
                       "itunes.apple.com/lookup": {"results": []}})
    out = imports.fetch_advisories([ALBUM], {"advisory_auto_fetch": True})
    assert out["updated"] == 1, out
    assert out["values"] == {FILES[0]: 1, FILES[1]: 2}, out
    assert out["sources"] == {FILES[0]: "deezer-isrc"}, out
    assert FakeAudio.written[FILES[0]]["ITUNESADVISORY"] == "1", FakeAudio.written
    assert FakeAudio.written[FILES[1]]["ITUNESADVISORY"] == "2", FakeAudio.written
    assert "ITUNESADVISORY" not in FakeAudio.written[FILES[2]], FakeAudio.written
    # the existing valid value was never looked up or rewritten
    assert len(gets(calls, "api.deezer.com")) == 2, calls

    # advisory_auto_fetch off → nothing at all
    out = imports.fetch_advisories([ALBUM], {"advisory_auto_fetch": False})
    assert out["updated"] == 0 and out["values"] == {}, out
finally:
    mlo_audio.AudioFile = _real_audiofile

# --------------------------------------------------------------------------- #
# 7) RateYourMusic: honest about being blocked
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status=200, text=""):
        self.status_code, self.text = status, text


class FakeHttpx:
    HTTPError = RuntimeError

    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None,
            follow_redirects=None):
        self.calls.append({"url": url, "params": dict(params or {}),
                           "headers": dict(headers or {})})
        return self.response


_real_httpx = intg.httpx
_real_cache_dir = intg._rym_cache_dir
_real_cookie = intg._rym_cookie
try:
    intg._rym_cache_dir = lambda: None          # never touch the real cache
    intg.RYM_MIN_INTERVAL = 0.0                 # no politeness sleep in a test
    intg._rym_cookie = lambda cfg=None: ""      # not configured
    intg._rym_warned = False
    fake = FakeHttpx(FakeResponse(403, ""))
    intg.httpx = fake
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        assert intg.rym_genres("Rihanna", "Loud") is None
        assert intg.rym_genres("Rihanna", "Loud") is None   # not per album
        assert intg.rym_artist_genres("Rihanna") is None
    lines = [ln for ln in log.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1 and "rateyourmusic" in lines[0], lines
    assert "rym_cookie" in lines[0], lines
    sent = fake.calls[0]["headers"]
    assert "Mozilla/5.0" in sent.get("User-Agent", ""), sent
    assert "Accept-Language" in sent and "Referer" in sent, sent

    # a Cloudflare interstitial is not a page either
    intg._rym_warned = False
    fake = FakeHttpx(FakeResponse(200, "<html><title>Just a moment...</title>"))
    intg.httpx = fake
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        assert intg.rym_genres("Rihanna", "Loud") is None
    assert "challenge" in log.getvalue(), log.getvalue()

    # with the user's session cookie the same request carries it, and a real
    # release page still parses
    intg._rym_warned = False
    intg._rym_cookie = lambda cfg=None: "cf_clearance=abc; session=xyz"
    page = ('<html><body><a href="/genre/pop">Pop</a>'
            '<a href="/genre/r&amp;b">R&amp;B</a></body></html>')
    fake = FakeHttpx(FakeResponse(200, page))
    intg.httpx = fake
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        got = intg.rym_genres("Rihanna", "Loud")
    assert log.getvalue() == "", log.getvalue()
    assert got and got["genres"] == ["Pop"], got
    assert fake.calls[0]["headers"].get("Cookie") == "cf_clearance=abc; session=xyz"
finally:
    intg.httpx = _real_httpx
    intg._rym_cache_dir = _real_cache_dir
    intg._rym_cookie = _real_cookie

# --------------------------------------------------------------------------- #
# 8) Provenance is mandatory: no value without the source that stated it
# --------------------------------------------------------------------------- #
assert {"deezer-isrc", "apple-album"} <= intg.ADVISORY_SOURCES
clear()
stub_http({"api.deezer.com": {"explicit_lyrics": False, "explicit_content_lyrics": 0},
           "itunes.apple.com/search": ALBUM_HITS,
           "itunes.apple.com/lookup": SONGS})
for kwargs in ({"isrc": ISRC},
               {"artist": "Rihanna", "album": "Loud", "title": "S&M",
                "disc": 1, "track": 1},
               {"title": "S&M", "artist": "Rihanna"}):
    route = intg.resolve_advisory_route(cfg=CFG, **kwargs)
    assert (route["value"] is None) == (route["source"] is None), route
    if route["source"]:
        assert route["source"] in intg.ADVISORY_SOURCES, route

print("advisory sources: all assertions passed")
