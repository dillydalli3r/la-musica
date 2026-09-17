#!/usr/bin/env python3
"""Advisory sources (server.integrations) — offline contract.

What this pins, with the HTTP layer stubbed (no network at all):

  * Deezer's per-track ISRC lookup is the PRIMARY source and maps
    explicit_content_lyrics / explicit_lyrics to 0/1/2 — and an unclassified
    field is NO answer, not a guess;
  * Apple's album route resolves the EDITION through the artist
    (`search?entity=musicArtist` → artist id → `lookup?entity=album`), prefers
    the `collectionExplicitness: explicit` edition over the cleaned one, and
    maps the file to a track by disc/track, then by exact title — with a
    `cleaned` entry as NO ANSWER (never 0, never 2) from that edition only;
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
# The iTunes disk cache belongs to a real app run (and would outlive these
# stubs); no politeness sleep either — the transport is stubbed here.
intg._apple_cache_dir = lambda: None
intg._APPLE_MIN_INTERVAL = 0.0


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
# 2) Apple artist route — artist → editions → the track
# --------------------------------------------------------------------------- #
# Payloads shaped exactly like Apple's (System Of A Down, "Steal This
# Album!", us storefront): the artist's album list carries BOTH editions — the
# explicit master (193126473) and the cleaned re-release (193535804) — and the
# explicit one's real per-track flags mark five tracks explicit. Apple's album
# *search* is deliberately not used: it answers the cleaned edition alone,
# whose every track reads `cleaned`/Clean.
ARTIST = "System Of A Down"
ALBUM = "Steal This Album!"
ARTIST_HITS = {"results": [
    {"wrapperType": "artist", "artistId": 462715, "artistName": ARTIST}]}
EXPLICIT_ID, CLEANED_ID = 193126473, 193535804
EDITION = {"wrapperType": "collection", "artistName": ARTIST,
           "collectionName": ALBUM, "collectionCensoredName": ALBUM,
           "trackCount": 16, "country": "USA", "primaryGenreName": "Hard Rock"}
EDITIONS = {"results": [
    dict(EDITION, collectionId=EXPLICIT_ID, collectionExplicitness="explicit"),
    dict(EDITION, collectionId=CLEANED_ID, collectionExplicitness="cleaned"),
]}
TRACK_NAMES = [
    "Chic 'N' Stu", "Innervision", "Bubbles", "Boom!", "Nüguns",
    "A.D.D. (American Dream Denial)", "Mr. Jack", "I-E-A-I-A-I-O", "36",
    "Pictures", "Highway Song", "F**k the System", "Ego Brain", "Thetawaves",
    "Roulette", "Streamline",
]
EXPLICIT_TRACKS = {4, 6, 7, 8, 12}


def tracks(cid, explicit=(), explicitness="notExplicit"):
    rows = []
    for n, name in enumerate(TRACK_NAMES, 1):
        exp = "explicit" if n in explicit else explicitness
        row = {"wrapperType": "track", "collectionId": cid, "discNumber": 1,
               "trackNumber": n, "trackName": name, "trackExplicitness": exp}
        if exp == "cleaned":
            row["contentAdvisoryRating"] = "Clean"   # Apple's own pairing
        rows.append(row)
    return {"results": rows}


def apple_routes(artist_hits=None, editions=None, songs=None):
    artist_hits = ARTIST_HITS if artist_hits is None else artist_hits
    editions = EDITIONS if editions is None else editions
    songs = songs if songs is not None else {
        EXPLICIT_ID: tracks(EXPLICIT_ID, EXPLICIT_TRACKS),
        CLEANED_ID: tracks(CLEANED_ID, explicitness="cleaned"),
    }
    return {
        "api.deezer.com": {"error": {"type": "DataException"}},
        "itunes.apple.com/search": lambda p: (artist_hits
                                              if p.get("entity") == "musicArtist"
                                              else {"results": []}),
        "itunes.apple.com/lookup": lambda p: (editions
                                              if p.get("entity") == "album"
                                              else songs.get(p.get("id"),
                                                             {"results": []})),
    }


def apple(**ctx):
    clear()
    calls = stub_http(apple_routes())
    ctx.setdefault("isrc", "")
    ctx.setdefault("artist", ARTIST)
    ctx.setdefault("album", ALBUM)
    ctx.setdefault("track_count", 16)
    route = intg.resolve_advisory_route(cfg=CFG, **ctx)
    return route, calls


# the five explicit tracks answer 1, the other eleven answer 0 — read off the
# EXPLICIT edition, by the file's own disc/track position
for n, name in enumerate(TRACK_NAMES, 1):
    route, _ = apple(title=name, disc=1, track=n)
    want = 1 if n in EXPLICIT_TRACKS else 0
    assert route["value"] == want and route["source"] == "apple-album", (n, route)

# ... and that is the edition that was read: artist search → artist id, then
# the artist's album list, and the clean edition is never looked up
route, calls = apple(title="Boom!", disc=1, track=4)
assert route["value"] == 1, route
search = gets(calls, "itunes.apple.com/search")
assert search[0][2] == {"term": ARTIST, "entity": "musicArtist", "limit": 5}, search
lookups = gets(calls, "itunes.apple.com/lookup")
assert [c[2]["id"] for c in lookups] == [462715, EXPLICIT_ID], lookups
assert lookups[0][2]["entity"] == "album" and lookups[0][2]["country"] == "us", lookups
assert lookups[1][2]["entity"] == "song", lookups

# disc/track wins over the title: a mistitled file still gets ITS track's flag
route, _ = apple(title="Wrong Title", disc=1, track=4)
assert route["value"] == 1, route

# exact normalized title fallback when the file states no position
route, _ = apple(title="Mr. Jack")
assert route["value"] == 1, route

# only a CLEANED edition exists → NO value (never 0, never 2), even though
# that edition reports a rating for every track
clear()
calls = stub_http(apple_routes(
    editions={"results": [dict(EDITION, collectionId=CLEANED_ID,
                               collectionExplicitness="cleaned")]}))
route = intg.resolve_advisory_route(artist=ARTIST, album=ALBUM, title="Boom!",
                                    disc=1, track=4, track_count=16, cfg=CFG)
assert route["value"] is None and route["source"] is None, route
route = intg.resolve_advisory_route(artist=ARTIST, album=ALBUM, title="Boom!",
                                    cfg=CFG)
assert route["value"] is None and route["source"] is None, route

# an artist Apple does not know, or an album the artist does not have, is NO
# value — and never an exception
clear()
calls = stub_http(apple_routes(artist_hits={"results": []}))
route = intg.resolve_advisory_route(artist="Nobody At All", album=ALBUM,
                                    title="Boom!", disc=1, track=4, cfg=CFG)
assert route["value"] is None and route["source"] is None, route
assert not gets(calls, "itunes.apple.com/lookup"), calls
route = intg.resolve_advisory_route(artist=ARTIST, album="", title="Boom!",
                                    disc=1, track=4, cfg=CFG)
assert route["value"] is None, route
route = intg.resolve_advisory_route(artist=ARTIST, album=ALBUM, cfg=CFG)
assert route["value"] is None and route["source"] is None, route

# a release the artist does not have (every row credits another artist) is not
# this album: the name match alone never rates a track
clear()
stub_http(apple_routes(editions={"results": [
    dict(EDITION, collectionId=EXPLICIT_ID, collectionExplicitness="explicit",
         artistName="Some Cover Band")]}))
route = intg.resolve_advisory_route(artist=ARTIST, album=ALBUM, title="Boom!",
                                    disc=1, track=4, track_count=16, cfg=CFG)
assert route["value"] is None and route["source"] is None, route


# An edition that can only state `cleaned` is not the end of the road: the
# next candidate (same album, same track count, so the position still
# identifies the track) answers instead.
FALLBACK = {"api.deezer.com": {"error": {"type": "DataException"}},
            "itunes.apple.com/search": {"results": [
                {"wrapperType": "artist", "artistId": 1039,
                 "artistName": "Rihanna"}]},
            "itunes.apple.com/lookup": lambda params: (
                {"results": [
                    {"wrapperType": "collection", "collectionId": 501,
                     "collectionName": "Loud", "artistName": "Rihanna",
                     "trackCount": 4},
                    {"wrapperType": "collection", "collectionId": 502,
                     "collectionName": "Loud", "artistName": "Rihanna",
                     "trackCount": 4}]}
                if params.get("entity") == "album" else
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
assert [c[2]["id"] for c in gets(calls, "itunes.apple.com/lookup")] == [1039, 501, 502], calls

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
stub_http(dict(apple_routes(), **{
    "api.deezer.com": {"explicit_lyrics": False, "explicit_content_lyrics": 0}}))
for kwargs in ({"isrc": ISRC},
               {"artist": ARTIST, "album": ALBUM, "title": "Boom!",
                "disc": 1, "track": 4},
               {"title": "Boom!", "artist": ARTIST}):
    route = intg.resolve_advisory_route(cfg=CFG, **kwargs)
    assert (route["value"] is None) == (route["source"] is None), route
    if route["source"]:
        assert route["source"] in intg.ADVISORY_SOURCES, route

print("advisory sources: all assertions passed")
