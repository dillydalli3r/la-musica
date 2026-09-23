#!/usr/bin/env python3
"""Advisory sources (server.integrations) — offline contract.

What this pins, with the HTTP layer stubbed (no network at all):

  * EVERY applicable source is asked in one pass and none short-circuits:
    Deezer's per-track ISRC lookup, Spotify's ISRC search (configured only),
    Apple's album route and Apple's song search — and every ISRC MusicBrainz
    holds for the recording feeds both ISRC sources (Apple serves no ISRC
    lookup);
  * the merge rule lives in ONE place, `integrations.merge_advisory`: explicit
    anywhere → 1, else clean anywhere → 0, else 0 (an unstated advisory is
    written as 0, the user's policy) — with the per-source `answers` map as
    the only signal that a source actually spoke;
  * Deezer's `explicit_content_lyrics` / `explicit_lyrics` map to 0/1 — and an
    unclassified field is NO answer, not a guess;
  * Apple's album route resolves the EDITION through the artist
    (`search?entity=musicArtist` → artist id → `lookup?entity=album`), prefers
    the `collectionExplicitness: explicit` edition over the cleaned one, and
    maps the file to a track by disc/track, then by title — a `cleaned` entry
    is NO ANSWER from that edition only (never 0, never 2);
  * Apple's song search accepts only a hit `title_matches` accepts, and a
    `cleaned` hit is rejected;
  * a variant is never the track: an instrumental/karaoke/demo/cover/tribute
    candidate is refused for an original search, and the original is refused
    for a file whose own name says it is the variant
    (`title_variant_kind` / `title_matches`);
  * a provider value is only written when a source stated it; when none did,
    `fetch_advisories` runs the ladder in mlo.advisory (instrumental → AI →
    lyrics scan → `advisory_fallback`) and reports which stage answered;
  * an existing valid 0/1/2 is ECHOED, never overwritten behind the user's
    back, and the echo says what it is (`sources` = "existing-tag", `status` =
    "existing") instead of showing a value with no provenance; `force=True`
    re-rates it, and even then only evidence may lower a stored rating;
  * one source asked once per pressing keeps its STRONGEST answer, so a later
    pressing's explicit flag cannot lose to an earlier one's clean answer;
  * RateYourMusic sends the full Chrome header set and carries the configured
    `rym_cookie` in a cookie jar (so RYM's own Set-Cookie can join it, after
    one warm-up navigation per paste), treats a blocked/challenge answer as
    unreachable, logs ONE line per process, records the response it refused
    (`rym_last_response`) and returns None so the genre chain falls through.

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
        # `(body, error)` — the seam keeps a refusal's own words, so a stub has
        # to answer both halves (a stubbed call never refuses here).
        return _route(url, data or {}), ""

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
    # nothing classified either way → no answer from Deezer: the merge rule
    # states nothing, and what an unstated track becomes is the caller's
    # decision (mlo.advisory.decide_advisory), not a value invented here
    ({"explicit_content_lyrics": 3}, None),
    ({"error": {"type": "DataException", "message": "no data"}}, None),
]
for payload, want in DEEZER_CASES:
    clear()
    calls = stub_http({"api.deezer.com": payload})
    route = intg.resolve_advisory_route(isrc=ISRC, cfg=CFG)
    merged = None if want is None else want
    assert route["value"] == merged, (payload, route)
    assert route["answers"] == ({} if want is None
                                else {"deezer-isrc": merged}), (payload, route)
    assert route["source"] == ("deezer-isrc" if want is not None else None), (payload, route)
    assert intg.resolve_advisory(isrc=ISRC, cfg=CFG) == merged, (payload, route)
    assert f"track/isrc:{ISRC}" in gets(calls, "api.deezer.com")[0][1]
    # the same track is asked once: the memo (not the network) answers again
    assert len(gets(calls, "api.deezer.com")) == 1, calls

# An ISRC Deezer does not hold falls through to the album route; nobody states
# anything here, so the value is None with an EMPTY answers map — and no
# source, because nothing stated it. Writing a value is the caller's job
# (mlo.advisory.decide_advisory), not the merge rule's.
clear()
stub_http({"api.deezer.com": {"error": {"type": "DataException"}},
           "itunes.apple.com/search": {"results": []}})
route = intg.resolve_advisory_route(isrc=ISRC, artist="Rihanna", album="Loud",
                                    title="S&M", disc=1, track=1, cfg=CFG)
assert route == {"value": None, "source": None, "answers": {},
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
# Section 6 rebinds the module-level ALBUM to a temp folder; the Apple album
# title is pinned here so later sections still ask about the real album.
APPLE_ALBUM = ALBUM
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
rated = {}
for n, name in enumerate(TRACK_NAMES, 1):
    route, _ = apple(title=name, disc=1, track=n)
    want = 1 if n in EXPLICIT_TRACKS else 0
    assert route["value"] == want and route["source"] == "apple-album", (n, route)
    rated[n] = route["value"]
# the whole album in one place, so "which of the 16 end explicit, and from
# whom" is a pinned answer and not a matter of counting rows by eye: exactly
# the five Apple flags on the explicit edition, every one attributed to it
assert len(rated) == 16 and sorted(rated) == list(range(1, 17)), rated
assert {n for n, v in rated.items() if v == 1} == EXPLICIT_TRACKS == {4, 6, 7, 8, 12}
assert {name: rated[n] for n, name in enumerate(TRACK_NAMES, 1) if rated[n] == 1} \
    == {"Boom!": 1, "A.D.D. (American Dream Denial)": 1, "Mr. Jack": 1,
        "I-E-A-I-A-I-O": 1, "F**k the System": 1}

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

# only a CLEANED edition exists → the edition states no rating for any track,
# so no source answered and the merged value is 0 with an EMPTY answers map
# (never a value attributed to `cleaned`)
clear()
calls = stub_http(apple_routes(
    editions={"results": [dict(EDITION, collectionId=CLEANED_ID,
                               collectionExplicitness="cleaned")]}))
route = intg.resolve_advisory_route(artist=ARTIST, album=ALBUM, title="Boom!",
                                    disc=1, track=4, track_count=16, cfg=CFG)
assert route["value"] is None and route["source"] is None and route["answers"] == {}, route
route = intg.resolve_advisory_route(artist=ARTIST, album=ALBUM, title="Boom!",
                                    cfg=CFG)
assert route["value"] is None and route["source"] is None and route["answers"] == {}, route

# an artist Apple does not know, or an album the artist does not have, states
# nothing — and never raises
clear()
calls = stub_http(apple_routes(artist_hits={"results": []}))
route = intg.resolve_advisory_route(artist="Nobody At All", album=ALBUM,
                                    title="Boom!", disc=1, track=4, cfg=CFG)
assert route["value"] is None and route["source"] is None and route["answers"] == {}, route
assert not gets(calls, "itunes.apple.com/lookup"), calls
route = intg.resolve_advisory_route(artist=ARTIST, album="", title="Boom!",
                                    disc=1, track=4, cfg=CFG)
assert route["value"] is None and route["source"] is None, route
route = intg.resolve_advisory_route(artist=ARTIST, album=ALBUM, cfg=CFG)
assert route["value"] is None and route["source"] is None and route["answers"] == {}, route

# a release the artist does not have (every row credits another artist) is not
# this album: the name match alone never rates a track
clear()
stub_http(apple_routes(editions={"results": [
    dict(EDITION, collectionId=EXPLICIT_ID, collectionExplicitness="explicit",
         artistName="Some Cover Band")]}))
route = intg.resolve_advisory_route(artist=ARTIST, album=ALBUM, title="Boom!",
                                    disc=1, track=4, track_count=16, cfg=CFG)
assert route["value"] is None and route["source"] is None and route["answers"] == {}, route


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

# configured: the Deezer miss is answered by Spotify, with the bearer token —
# and the provenance map carries exactly who said what
calls, route = spotify([HIT])
assert route == {"value": 1, "source": "spotify-isrc",
                 "answers": {"spotify-isrc": 1},
                 "checked": ["deezer-isrc", "spotify-isrc"]}, route
assert gets(calls, "api.spotify.com")[0][3].get("Authorization") == "Bearer tok", calls
assert "isrc:" + ISRC in gets(calls, "api.spotify.com")[0][2]["q"]
calls, route = spotify([{"explicit": False, "external_ids": {"isrc": ISRC}}])
assert route["value"] == 0 and route["source"] == "spotify-isrc", route
assert route["answers"] == {"spotify-isrc": 0}, route

# a hit for a DIFFERENT isrc is not this track: nothing answered, merged 0
calls, route = spotify([{"explicit": True, "external_ids": {"isrc": "GBAYE0000001"}}])
assert route["value"] is None and route["source"] is None and route["answers"] == {}, route

# unconfigured: Spotify is never called, and its absence changes nothing
clear()
calls = stub_http({"api.deezer.com": {"error": {"type": "DataException"}}})
route = intg.resolve_advisory_route(isrc=ISRC, cfg=CFG)
assert route == {"value": None, "source": None, "answers": {},
                 "checked": ["deezer-isrc"]}, route
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
assert route["value"] is None and route["source"] is None and route["answers"] == {}, route

# exact title + stated explicitness → accepted
calls, route = song_search(SONG_SEARCH["results"] + [
    {"trackName": "S&M", "artistName": "Rihanna", "trackExplicitness": "notExplicit"}])
assert route["value"] == 0 and route["source"] == "itunes-song", route
assert route["answers"] == {"itunes-song": 0}, route

# ... but a CLEANED hit never is (it is an edited master, not a rating), so
# Apple's song search states nothing at all
calls, route = song_search([{"trackName": "S&M", "artistName": "Rihanna",
                             "trackExplicitness": "cleaned",
                             "contentAdvisoryRating": "Clean"}])
assert route["value"] is None and route["source"] is None and route["answers"] == {}, route
assert route["checked"] == ["itunes-song"], route

# a same-title hit by another artist is not this track
calls, route = song_search([{"trackName": "S&M", "artistName": "Some Cover Band",
                             "trackExplicitness": "explicit"}])
assert route["value"] is None and route["source"] is None and route["answers"] == {}, route

# --------------------------------------------------------------------------- #
# 5b) A variant is never the track: neither an instrumental/karaoke hit for an
# original-file search, nor the original for a file that IS the variant
# --------------------------------------------------------------------------- #
def artist_of(title):
    return "Rihanna" if title == "S&M" else ARTIST


for title, variant in [
        ("S&M", "S&M (Instrumental)"),
        ("S&M", "S&M (Karaoke Version)"),
        ("Boom!", "Boom! (Demo)"),
        ("Boom!", "Boom! (Cover Version)"),
        ("Boom!", "Boom! (Made Famous By System Of A Down)")]:
    clear()
    calls = stub_http({"itunes.apple.com/search": {"results": [
        {"trackName": variant, "artistName": artist_of(title),
         "trackExplicitness": "explicit"}]}})
    route = intg.resolve_advisory_route(title=title, artist=artist_of(title), cfg=CFG)
    assert route["value"] is None and route["source"] is None, (title, variant, route)
    assert route["answers"] == {}, (title, variant, route)

# ... and the reverse: a file that IS the variant never takes the original's
# rating, even though the search hit is the same song by the same artist
clear()
stub_http({"itunes.apple.com/search": {"results": [
    {"trackName": "S&M", "artistName": "Rihanna", "trackExplicitness": "explicit"}]}})
route = intg.resolve_advisory_route(title="S&M (Instrumental)", artist="Rihanna",
                                    cfg=CFG)
assert route["value"] is None and route["source"] is None and route["answers"] == {}, route
# the same-kind variant of the same title IS the same title; a cross-kind
# pair never is
assert intg.title_matches("S&M (Instrumental)", "S&M (Instrumental)")
assert not intg.title_matches("S&M (Karaoke)", "S&M (Instrumental)")
assert intg.title_variant_kind("Karaoke - S&M") == "karaoke"
assert intg.title_variant_kind("Instrumental Version") == "instrumental"
assert intg.title_variant_kind("S&M") is None

# The guard has to be its own check, not just "the names differ": these two
# normalize to the SAME key, so only the variant kind separates them — and a
# search hit named the variant (no parentheses) is refused for a file whose
# own name says "(Instrumental)".
assert intg._norm_compare("Boom! (Instrumental)") == intg._norm_compare("Boom! Instrumental")
assert not intg.title_matches("Boom! (Instrumental)", "Boom! Instrumental")
clear()
stub_http({"itunes.apple.com/search": {"results": [
    {"trackName": "Boom! Instrumental", "artistName": ARTIST,
     "trackExplicitness": "explicit"}]}})
route = intg.resolve_advisory_route(title="Boom! (Instrumental)", artist=ARTIST, cfg=CFG)
assert route["value"] is None and route["answers"] == {}, route

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
    # nobody states anything → the ladder's last resort (advisory_fallback,
    # 0 by default) is what a track with no value gets, and it says so: the
    # stage is reported in `sources`, while `answers` stays empty because no
    # PROVIDER stated anything. The track already carrying a valid 2 is
    # reported, not rewritten — and the echo carries its OWN provenance (the
    # file's tag) plus a status saying nobody was asked, so the readout can
    # tell "a source stated this" from "the file did, and this run did not
    # look" instead of showing a value with no source at all.
    clear()
    stub_http({})
    out = imports.fetch_advisories([ALBUM], {"advisory_auto_fetch": True})
    assert out["updated"] == 2, out
    assert out["values"] == {FILES[0]: 0, FILES[1]: 2, FILES[2]: 0}, out
    assert out["sources"] == {FILES[0]: "fallback", FILES[1]: "existing-tag",
                              FILES[2]: "fallback"}, out
    assert out["status"] == {FILES[0]: "written", FILES[1]: "existing",
                             FILES[2]: "written"}, out
    assert out["answers"] == {}, out
    assert FakeAudio.written[FILES[0]]["ITUNESADVISORY"] == "0", FakeAudio.written
    assert FakeAudio.written[FILES[1]]["ITUNESADVISORY"] == "2", FakeAudio.written
    assert FakeAudio.written[FILES[2]]["ITUNESADVISORY"] == "0", FakeAudio.written

    # Deezer answers for the first track only: that one is written with its
    # source AND its provenance map, the user's existing 2 is echoed with the
    # file's own provenance (never overwritten, never counted as a write), and
    # the track nobody answered for still merges to 0
    for path in FILES:
        FakeAudio.written[path].pop("ITUNESADVISORY", None)
    FakeAudio.written[FILES[1]]["ITUNESADVISORY"] = "2"
    clear()
    calls = stub_http({"api.deezer.com/track/isrc:" + ISRC:
                       {"explicit_lyrics": True, "explicit_content_lyrics": 1},
                       "api.deezer.com": {"error": {"type": "DataException"}},
                       "itunes.apple.com/search": {"results": []},
                       "itunes.apple.com/lookup": {"results": []}})
    out = imports.fetch_advisories([ALBUM], {"advisory_auto_fetch": True})
    assert out["updated"] == 2, out
    assert out["values"] == {FILES[0]: 1, FILES[1]: 2, FILES[2]: 0}, out
    assert out["sources"] == {FILES[0]: "deezer-isrc",
                              FILES[1]: "existing-tag",
                              FILES[2]: "fallback"}, out
    assert out["status"] == {FILES[0]: "written", FILES[1]: "existing",
                             FILES[2]: "written"}, out
    assert out["answers"] == {FILES[0]: {"deezer-isrc": 1}}, out
    assert FakeAudio.written[FILES[0]]["ITUNESADVISORY"] == "1", FakeAudio.written
    assert FakeAudio.written[FILES[1]]["ITUNESADVISORY"] == "2", FakeAudio.written
    assert FakeAudio.written[FILES[2]]["ITUNESADVISORY"] == "0", FakeAudio.written
    # the existing valid value was never looked up or rewritten
    assert len(gets(calls, "api.deezer.com")) == 2, calls

    # ----------------------------------------------------------------------- #
    # 6b) force: the re-rate — a pre-rated file IS asked, and the sources'
    #     answer is written. Without it a 0 that an earlier run invented (the
    #     fallback writes 0 for a track nobody rated) outlived every provider
    #     that later knew better, which is exactly how an explicit track kept
    #     reading "0 (not explicit)".
    # ----------------------------------------------------------------------- #
    FakeAudio.written[FILES[1]]["ITUNESADVISORY"] = "0"     # the invented 0
    clear()
    calls = stub_http({"api.deezer.com/track/isrc:USUM71000001":
                       {"explicit_lyrics": True, "explicit_content_lyrics": 1},
                       "api.deezer.com": {"error": {"type": "DataException"}},
                       "itunes.apple.com/search": {"results": []},
                       "itunes.apple.com/lookup": {"results": []}})
    out = imports.fetch_advisories([ALBUM], {"advisory_auto_fetch": True},
                                   force=True)
    assert gets(calls, "api.deezer.com/track/isrc:USUM71000001"), calls
    assert out["values"][FILES[1]] == 1, out
    assert out["sources"][FILES[1]] == "deezer-isrc", out
    assert out["status"][FILES[1]] == "written", out
    assert out["answers"][FILES[1]] == {"deezer-isrc": 1}, out
    assert out["updated"] == 1, out
    assert FakeAudio.written[FILES[1]]["ITUNESADVISORY"] == "1", FakeAudio.written
    # FILES[0] and FILES[2] already read 1 and 0 and Deezer does not hold
    # their ISRCs: a re-rate asked, heard nothing, and left them alone
    assert out["status"][FILES[0]] == "unchanged", out
    assert out["sources"][FILES[0]] == "existing-tag", out

    # ... and a re-rate only ever rewrites EVIDENCE: with nobody stating
    # anything anywhere, the invented `advisory_fallback` does NOT overwrite
    # the stored rating — the file keeps its 1, the run says nothing changed
    # (updated 0 with `status` describing every file, so "0 value(s) written"
    # can never be read as a silent re-rate that found nothing)
    clear()
    stub_http({})
    out = imports.fetch_advisories([ALBUM], {"advisory_auto_fetch": True},
                                   force=True)
    assert out["updated"] == 0, out
    assert out["values"][FILES[1]] == 1, out
    assert out["status"][FILES[1]] == "unchanged", out
    assert out["sources"][FILES[1]] == "existing-tag", out
    assert set(out["status"].values()) == {"unchanged"}, out
    assert FakeAudio.written[FILES[1]]["ITUNESADVISORY"] == "1", FakeAudio.written

    # advisory_auto_fetch off → nothing at all
    out = imports.fetch_advisories([ALBUM], {"advisory_auto_fetch": False})
    assert out["updated"] == 0 and out["values"] == {}, out
finally:
    mlo_audio.AudioFile = _real_audiofile

# --------------------------------------------------------------------------- #
# 7) RateYourMusic: honest about being blocked
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status=200, text="", url=""):
        self.status_code, self.text, self.url = status, text, url


class FakeCookies(dict):
    """`httpx.Cookies` as the RYM code needs it: a jar the paste seeds, which
    the fake transport reads back off every request it serves."""

    def set(self, name, value, domain="", path="/"):
        self[str(name)] = value


class FakeHttpx:
    HTTPError = RuntimeError
    Cookies = FakeCookies

    def __init__(self, response):
        self.response = response
        self.calls = []
        self.jars = []

    def get(self, url, params=None, headers=None, timeout=None,
            follow_redirects=None, cookies=None):
        self.calls.append({"url": url, "params": dict(params or {}),
                           "headers": dict(headers or {})})
        self.jars.append(dict(cookies or {}))
        # The caller reads response.url back to confirm the page it landed on
        # is the release it asked for — a stub that never states it makes
        # every scrape look unverifiable.
        self.response.url = url
        return self.response


_real_httpx = intg.httpx
_real_cache_dir = intg._rym_cache_dir
_real_cookie = intg._rym_cookie
_real_jar = (intg._rym_jar, intg._rym_jar_paste, intg._rym_warmed,
             intg._rym_last_info)
try:
    intg._rym_cache_dir = lambda: None          # never touch the real cache
    intg.RYM_MIN_INTERVAL = 0.0                 # no politeness sleep in a test
    intg._rym_cookie = lambda cfg=None: ""      # not configured
    intg._rym_warned = False
    intg._rym_jar, intg._rym_jar_paste = None, None
    intg._rym_warmed, intg._rym_last_info = None, {}
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
    # one probe, and no warm-up: there is no cookie whose paste could be
    # warmed, so the FIRST request is the page itself
    assert [c["url"] for c in fake.calls] == [
        "https://rateyourmusic.com/release/album/rihanna/loud/"], fake.calls
    sent = fake.calls[0]["headers"]
    assert "Mozilla/5.0" in sent.get("User-Agent", ""), sent
    assert "Accept-Language" in sent and "Referer" in sent, sent
    # the whole Chrome set, not just a UA: the client hints and the fetch
    # metadata are what a browser always sends alongside it
    assert sent.get("sec-ch-ua-platform") == '"Windows"', sent
    assert sent.get("Sec-Fetch-Dest") == "document", sent
    assert sent.get("Sec-Fetch-Mode") == "navigate", sent
    assert sent.get("Sec-Fetch-Site") == "same-origin", sent
    # and the refusal is recorded, so the panel can say WHICH one it was
    record = intg.rym_last_response()
    assert record["status"] == 403 and record["challenge"] is False, record
    assert record["url"].endswith("/release/album/rihanna/loud/"), record

    # a Cloudflare interstitial is not a page either
    intg._rym_warned = False
    fake = FakeHttpx(FakeResponse(200, "<html><title>Just a moment...</title>"))
    intg.httpx = fake
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        assert intg.rym_genres("Rihanna", "Loud") is None
    assert "challenge" in log.getvalue(), log.getvalue()
    assert intg.rym_last_response()["challenge"] is True, intg.rym_last_response()

    # with the user's session cookie the same request carries it, and a real
    # release page still parses. The credential rides in a cookie JAR, not a
    # hand-built `Cookie:` header (that is what lets RYM's own Set-Cookie join
    # it), and the first request a paste makes is ONE warm-up navigation to
    # RYM's home page — the request that makes the WAF hand those over.
    intg._rym_warned = False
    intg._rym_cookie = lambda cfg=None: "cf_clearance=abc; session=xyz"
    page = ('<html><body><h1 class="album_title">Loud</h1>'
            '<a href="/artist/rihanna">Rihanna</a>'
            '<a href="/genre/pop">Pop</a>'
            '<a href="/genre/r&amp;b">R&amp;B</a></body></html>')
    fake = FakeHttpx(FakeResponse(200, page))
    intg.httpx = fake
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        got = intg.rym_genres("Rihanna", "Loud")
    assert log.getvalue() == "", log.getvalue()
    assert got and got["genres"] == ["Pop"], got
    assert [c["url"] for c in fake.calls] == [
        "https://rateyourmusic.com/",
        "https://rateyourmusic.com/release/album/rihanna/loud/"], fake.calls
    assert fake.jars[0] == {"cf_clearance": "abc", "session": "xyz"}, fake.jars
    assert fake.jars[1] == fake.jars[0], fake.jars
    assert "Cookie" not in fake.calls[1]["headers"], fake.calls[1]["headers"]
finally:
    intg.httpx = _real_httpx
    intg._rym_cache_dir = _real_cache_dir
    intg._rym_cookie = _real_cookie
    (intg._rym_jar, intg._rym_jar_paste, intg._rym_warmed,
     intg._rym_last_info) = _real_jar

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
    assert route["value"] in (0, 1, None), route
    # a value only carries a source when a source actually stated something,
    # and the whole provenance map is empty exactly then
    assert (route["source"] is None) == (not route["answers"]), route
    if route["source"]:
        assert route["source"] in intg.ADVISORY_SOURCES, route
        assert route["source"] in route["answers"], route

# --------------------------------------------------------------------------- #
# 9) Cross-reference: explicit ANYWHERE wins, a clean statement is still heard,
# and every source is asked even when the first one already answered
# --------------------------------------------------------------------------- #
DZ_CLEAN = {"explicit_lyrics": False, "explicit_content_lyrics": 0}

# Deezer says clean, Apple's explicit edition says explicit → 1, and BOTH
# statements are in the provenance map
clear()
calls = stub_http(dict(apple_routes(), **{"api.deezer.com": DZ_CLEAN}))
route = intg.resolve_advisory_route(isrc=ISRC, artist=ARTIST, album=APPLE_ALBUM,
                                    title="Boom!", disc=1, track=4,
                                    track_count=16, cfg=CFG)
assert route["value"] == 1 and route["source"] == "apple-album", route
assert route["answers"] == {"deezer-isrc": 0, "apple-album": 1}, route
assert route["checked"] == ["deezer-isrc", "apple-album", "itunes-song"], route
# the first source answering did NOT stop the others: Deezer was asked, and
# Apple's artist route + explicit edition were followed too
assert gets(calls, "api.deezer.com"), calls
assert [c[2]["id"] for c in gets(calls, "itunes.apple.com/lookup")] == [462715, EXPLICIT_ID], calls

# ... and with Spotify configured, all four routes are asked, not three
clear()
calls = stub_http(dict(apple_routes(), **{
    "api.deezer.com": DZ_CLEAN,
    "accounts.spotify.com": TOKEN,
    "api.spotify.com": {"tracks": {"items": [
        {"explicit": False, "external_ids": {"isrc": ISRC}}]}}}))
route = intg.resolve_advisory_route(isrc=ISRC, artist=ARTIST, album=APPLE_ALBUM,
                                    title="Boom!", disc=1, track=4,
                                    track_count=16, cfg=SPOTIFY_CFG)
assert route["value"] == 1, route
assert route["answers"] == {"deezer-isrc": 0, "spotify-isrc": 0,
                            "apple-album": 1}, route
assert route["checked"] == ["deezer-isrc", "spotify-isrc", "apple-album",
                            "itunes-song"], route

# every source clean, none explicit → 0, attributed to the first one that
# said so
clear()
stub_http(dict(apple_routes(songs={EXPLICIT_ID: tracks(EXPLICIT_ID)}),
               **{"api.deezer.com": DZ_CLEAN}))
route = intg.resolve_advisory_route(isrc=ISRC, artist=ARTIST, album=APPLE_ALBUM,
                                    title="Boom!", disc=1, track=4,
                                    track_count=16, cfg=CFG)
assert route["value"] == 0 and route["source"] == "deezer-isrc", route
assert route["answers"] == {"deezer-isrc": 0, "apple-album": 0}, route

# the merge rule itself, in one place: 1 > 0 > 2, and "nobody stated
# anything" is None — the caller decides what that becomes
# (mlo.advisory.decide_advisory), the rule invents nothing.
assert intg.merge_advisory({}) is None
assert intg.merge_advisory({}, fallback=0) == 0
assert intg.merge_advisory({"a": 0}) == 0              # clean only
assert intg.merge_advisory({"a": 2}) == 2              # a clean edition, alone
assert intg.merge_advisory({"a": 0, "b": 1}) == 1      # explicit beats clean
assert intg.merge_advisory({"a": 1, "b": 0}) == 1      # either order
assert intg.merge_advisory({"a": 2, "b": 1}) == 1      # explicit beats the edition
# 0 outranks 2 in the clean family: most sources may state "clean edition",
# one source stating the track itself is not explicit settles it
assert intg.merge_advisory({"a": 2, "b": 2, "c": 0}) == 0
assert intg.merge_advisory({"a": 2, "b": 2}) == 2

# ISRC completeness: with no ISRC tag, every ISRC MusicBrainz holds for the
# recording feeds BOTH ISRC sources — Apple has no ISRC lookup at all
_real_recording_isrcs = intg.recording_isrcs
intg.recording_isrcs = lambda mbid: ["AAA11111111", "BBB22222222"]
try:
    clear()
    calls = stub_http({"api.deezer.com": {"error": {"type": "DataException"}},
                       "accounts.spotify.com": TOKEN,
                       "api.spotify.com": {"tracks": {"items": []}}})
    route = intg.resolve_advisory_route(recording_mbid="rec-1", cfg=SPOTIFY_CFG)
    assert route["checked"] == ["deezer-isrc", "spotify-isrc"] * 2, route
    assert [c[1].rsplit("isrc:", 1)[-1]
            for c in gets(calls, "api.deezer.com")] == ["AAA11111111",
                                                        "BBB22222222"], calls
    assert [c[2]["q"] for c in gets(calls, "api.spotify.com")
            if "q" in c[2]] == ["isrc:AAA11111111", "isrc:BBB22222222"], calls
finally:
    intg.recording_isrcs = _real_recording_isrcs

# --------------------------------------------------------------------------- #
# 10) One source, several pressings: its STRONGEST answer is the source's
# --------------------------------------------------------------------------- #
# The two ISRCs of one recording (two pressings; `ISRC` above is the
# single-code case the rest of this file asks about).
PRESSING_A, PRESSING_B = ISRC, "USSM10213523"
# Apple answers nothing here, so only the Deezer legs can state anything.
NO_APPLE = {"itunes.apple.com/search": {"results": []},
            "itunes.apple.com/lookup": {"results": []}}
# A file may state several ISRCs for the same recording, and every one of them
# is asked. Ask order must not decide the answer: the first pressing's clean
# answer used to be kept (`setdefault`) and the second pressing's explicit one
# dropped, so a track Deezer itself flags explicit was written 0. The rank is
# `merge_advisory`'s own, applied per source: 1 > 0 > 2.
assert intg._strongest_advisory(None, 0) == 0
assert intg._strongest_advisory(None, None) is None
assert intg._strongest_advisory(0, None) == 0
assert intg._strongest_advisory(0, 1) == 1 and intg._strongest_advisory(1, 0) == 1
assert intg._strongest_advisory(2, 0) == 0 and intg._strongest_advisory(0, 2) == 0
assert intg._strongest_advisory(2, 1) == 1 and intg._strongest_advisory(1, 2) == 1
assert intg._strongest_advisory(2, 2) == 2

DZ = {
    "api.deezer.com/track/isrc:" + PRESSING_A: DZ_CLEAN,
    "api.deezer.com/track/isrc:" + PRESSING_B: {
        "explicit_lyrics": True, "explicit_content_lyrics": 1},
    "api.deezer.com": {"error": {"type": "DataException"}},
}
clear()
calls = stub_http(dict(NO_APPLE, **DZ))
route = intg.resolve_advisory_route(isrc=[PRESSING_A, PRESSING_B], cfg=CFG)
assert route["value"] == 1 and route["source"] == "deezer-isrc", route
assert route["answers"] == {"deezer-isrc": 1}, route
assert len(gets(calls, "api.deezer.com")) == 2, calls

# ... and the other way round says exactly the same thing: the rule is the
# rank, not which pressing the file happened to name first
clear()
stub_http(dict(NO_APPLE, **DZ))
route = intg.resolve_advisory_route(isrc=[PRESSING_B, PRESSING_A], cfg=CFG)
assert route["value"] == 1 and route["answers"] == {"deezer-isrc": 1}, route

# a source that only ever says clean still answers 0 — the strongest rule is
# not "invent an explicit"
clear()
stub_http(dict(NO_APPLE, **{
    "api.deezer.com/track/isrc:" + PRESSING_A: DZ_CLEAN,
    "api.deezer.com/track/isrc:" + PRESSING_B: DZ_CLEAN,
    "api.deezer.com": {"error": {"type": "DataException"}}}))
route = intg.resolve_advisory_route(isrc=[PRESSING_A, PRESSING_B], cfg=CFG)
assert route["value"] == 0 and route["answers"] == {"deezer-isrc": 0}, route

# the release path reports the same provenance: `release_advisories` fills the
# caller's `sources`/`answers` maps from this route, keyed by "disc:position" —
# and it asks every ISRC MusicBrainz lists for the track, so a clean first
# pressing cannot hide the explicit second one
_real_release_lookup = intg.release_lookup
intg.release_lookup = lambda mbid: {
    "title": APPLE_ALBUM, "artists": [{"name": ARTIST}],
    "media": [{"disc": 1, "position": 1, "title": "Boom!",
               "recording_mbid": "", "artist_credit": ARTIST,
               "isrcs": [PRESSING_A, PRESSING_B]}]}
try:
    clear()
    stub_http(dict(NO_APPLE, **DZ))
    release_sources, release_answers = {}, {}
    values = intg.release_advisories("rel-1", sources=release_sources,
                                     answers=release_answers)
    assert values == {"1:1": 1}, values
    assert release_sources == {"1:1": "deezer-isrc"}, release_sources
    assert release_answers == {"1:1": {"deezer-isrc": 1}}, release_answers
finally:
    intg.release_lookup = _real_release_lookup

print("advisory sources: all assertions passed")
