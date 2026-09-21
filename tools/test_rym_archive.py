#!/usr/bin/env python3
"""RateYourMusic's archived-snapshot fallback — offline contract.

"Import from RateYourMusic" has to produce real RYM genres on an install nobody
has pasted a `rym_cookie` into. This pins how, with the HTTP layer stubbed (no
network at all) and the module's disk caches off unless a test asks for one:

  * with no cookie and `rym_archive_fallback` on, the release page is read from
    the Wayback Machine and parsed by the SAME scrapers the live page uses: a
    row that states its own genre is `level: track` for that track, the album's
    own anchors are `level: album` for every track, and NOT ONE live
    rateyourmusic.com request is made — without a credential the live site is a
    known refusal, and asking it is exactly what the gate exists to avoid;
  * the tier chain holds end to end: the RYM track row beats the album page, the
    album page beats the artist page, and a page that states genres but NO track
    rows still fills the track map at album level;
  * a Wayback answer that comes back WRAPPED (its own toolbar, its rewritten
    `/web/<timestamp>/https://…` links) is stripped before a single scrape, so
    the wrapper cannot turn a real page into "the source states nothing";
  * the artist page is reachable the same way, so that tier does not silently
    stop existing on a default install;
  * a Cloudflare interception is NEVER parsed into genres — not the newest
    capture, not one the index lists: the capture index is asked for the page's
    200 captures instead, and the newest of those answers;
  * the report says where the genres came from: the chain's `notes` names the
    snapshot and its capture date, the answer's `source_url` is the snapshot's
    URL (never the live page's), and `rateyourmusic` stays the source;
  * `rym_archive_fallback` off — or a cfg that never came through
    `mlo.config.DEFAULT_CONFIG`, which is the only source of the key's default
    — plus no cookie is TODAY'S skip, with the rym_cookie reason and not one
    request;
  * a live-cookie page and an archived page of the same release produce the same
    genre names: the snapshot is RYM's own data, not a second source;
  * the snapshot answer is cached for 30 days — the second import makes no
    request at all, and the cached copy still says which capture it is;
  * MusicBrainz's own tiers are labelled by where their names came from —
    recording → release → release group → artist — with an artist-only row
    marked `artist` and never `album`, because a mislabelled tier both
    misreports the provenance and outranks the next source's album tier.

Run:  python tools/test_rym_archive.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import discovery
from server import integrations as intg

# The transport is stubbed, so the politeness sleep would only make this slow;
# and no disk caches by default — they belong to a real app run and would
# outlive these stubs (§7 sets one back up for its own scenario).
intg.RYM_MIN_INTERVAL = 0.0
intg._data_cache_dir = lambda name: None

FILE_ONE = "1-01 Track One.flac"
FILE_TWO = "1-02 Track Two.flac"

# A default install: nothing pasted in, and the key `mlo/config.py` ships True —
# which is what `load_config()` hands the chain.
CFG = {"mb_genre_count": 3, "rym_archive_fallback": True}
# The same install with the fallback switched off in Settings.
CFG_OFF = {"mb_genre_count": 3, "rym_archive_fallback": False}
# A pasted cookie, so the LIVE page is the route that can answer.
CFG_COOKIE = dict(CFG, rym_cookie="cf_clearance=test")

RELEASE = {
    "id": "rel-1",
    "title": "Test Album",
    "release_group_id": "rg-1",
    "genres": [],
    "artists": [{"name": "Test Artist", "mbid": "art-1"}],
    "media": [
        {"disc": 1, "position": 1, "title": "Track One",
         "recording_mbid": "rec-1", "genres": []},
        {"disc": 1, "position": 2, "title": "Track Two",
         "recording_mbid": "rec-2", "genres": []},
    ],
}

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
STAMP = "20210325091401"          # the capture that answers, when one does
OLD_STAMP = "20210210212843"      # an older capture the index may offer
TARGET = "https://rateyourmusic.com/release/album/test_artist/test_album/"
ARTIST_TARGET = "https://rateyourmusic.com/artist/test-artist"

# A release page as RYM serves it, archived or live — the SAME snippet is used
# for both routes below, which is the point of §5: the snapshot carries RYM's
# own markup, so the scrapers read it identically. The album states two genres
# and its SECOND row states one of its own, which is the per-track signal; the
# header carries the artist and the album, which is what verifies the page
# really is this release before anything is read off it.
PAGE = (
    '<html><body><h1 class="album_title">Test Album</h1>'
    '<a href="/artist/test-artist">Test Artist</a>'
    '<div class="album_genres">'
    '<a href="/genre/heavy-metal">Heavy Metal</a>'
    '<a href="/genre/groove-metal">Groove Metal</a>'
    '</div><table class="tracklist">'
    '<tr class="tracklist_row"><td class="tracklist_track_num">1</td>'
    '<td class="tracklist_track_title">Track One</td></tr>'
    '<tr class="tracklist_row"><td class="tracklist_track_num">2</td>'
    '<td class="tracklist_track_title">Track Two</td>'
    '<td><a href="/genre/industrial-metal">Industrial Metal</a></td></tr>'
    '</table></body></html>')
# The same release with genres but NO track list at all: everything the page
# states is album-level, and it still has to reach every track.
PAGE_NO_ROWS = (
    '<html><body><h1 class="album_title">Test Album</h1>'
    '<a href="/artist/test-artist">Test Artist</a>'
    '<div class="album_genres">'
    '<a href="/genre/heavy-metal">Heavy Metal</a>'
    '<a href="/genre/groove-metal">Groove Metal</a>'
    '</div></body></html>')
ARTIST_PAGE = (
    '<html><body><h1 class="page_title">Test Artist</h1>'
    '<a href="/artist/test-artist">Test Artist</a>'
    '<div class="artist_genres">'
    '<a href="/genre/alternative-metal">Alternative Metal</a>'
    '<a href="/genre/nu-metal">Nu Metal</a>'
    '</div></body></html>')
# What RYM served the CRAWLER: the interstitial, on a 200 and on a 403 alike.
# It is cached, parsed or mistaken for a page, it would be an empty answer at
# best — and the capture index is what finds a capture that IS a page instead.
CHALLENGE = ("<html><head><title>Just a moment...</title></head>"
             "<body>Checking your browser before accessing "
             "rateyourmusic.com</body></html>")
NOT_ARCHIVED = "<html><body>Wayback Machine has not archived that URL.</body></html>"


def cdx(*stamps):
    """The capture index's own JSON: a header row, then one row per capture,
    oldest first (which is the order CDX lists them in)."""
    rows = [["urlkey", "timestamp", "original", "statuscode", "digest"]]
    rows += [["com,rateyourmusic)", s, TARGET, "200", "digest-%s" % s]
             for s in stamps]
    return json.dumps(rows)


# --------------------------------------------------------------------------- #
# Stubs — no network, and the disk caches are off unless a test turns one on
# --------------------------------------------------------------------------- #
class Response:
    def __init__(self, status, text, url):
        self.status_code, self.text, self.url = status, text, url


class FakeCookies(dict):
    """`httpx.Cookies` as the RYM code needs it: the jar a cookie paste seeds."""

    def set(self, name, value, domain="", path="/"):
        self[str(name)] = value


class FakeHttpx:
    """`integrations.httpx` — the live page AND the archive in one seam.

    A router rather than a route table: what the archive answers depends on the
    request (the newest capture, one the index named, the index itself), and
    spelling that out per test is what makes each case readable.
    """

    HTTPError = RuntimeError
    Cookies = FakeCookies

    def __init__(self, router):
        self.router, self.calls = router, []

    def get(self, url, params=None, headers=None, timeout=None,
            follow_redirects=None, cookies=None):
        self.calls.append(url)
        return self.router(url, dict(params or {}))


def stub_http(router):
    intg.httpx = FakeHttpx(router)
    return intg.httpx


def never(url, params):
    """A router for the cases where no request may be made at all."""
    raise AssertionError(f"no request was expected, got {url}")


def stub_mb(payloads=None):
    """MusicBrainz's entities by endpoint, for both of its routes: the `url`
    relations that state a RYM page (`mb_get_cached`) and the genre entities
    themselves (`mb_get`). The default is NOTHING at all — no relation, no
    genre — which is the state a default install is in when RateYourMusic is
    the only source being asked."""
    data = payloads or {}
    intg.mb_get_cached = lambda *a, **k: data.get(a[0]) or {}
    intg.mb_get = lambda *a, **k: data.get(a[0]) or {}


def clear():
    """Forget every in-process memo, so one scenario cannot answer the next."""
    intg._GENRE_CACHE.clear()
    discovery._CACHE.clear()
    discovery._HOST_WARNED.clear()
    intg._rym_warned = False
    intg._rym_failures = 0
    intg._rym_route.clear()
    intg._rym_jar = None
    intg._rym_jar_paste = None
    intg._rym_warmed = None


def chain(cfg, release=None, sources=("rateyourmusic",), limit=3):
    """One chain call with the source list PINNED: the default list would walk
    every other source over the real network, which this file must never do."""
    return intg.genre_chain(artist="Test Artist", album="Test Album",
                            release=release or RELEASE, cfg=cfg, limit=limit,
                            files=[FILE_ONE, FILE_TWO], sources=list(sources))


def archive_only(url, params):
    """Every request a default install makes is a Wayback one: the newest
    capture of the release page, redirecting to its own timestamped URL (where
    the snapshot's date comes from). A LIVE rateyourmusic.com request would
    fail here — that is the assertion §1 is built on."""
    assert url.startswith("https://web.archive.org/"), url
    assert url.startswith("https://web.archive.org/web/2id_/" + TARGET), url
    return Response(200, PAGE,
                    f"https://web.archive.org/web/{STAMP}id_/{TARGET}")


# --------------------------------------------------------------------------- #
# 1) (a) No cookie, archive on: the snapshot answers, per track and per album
# --------------------------------------------------------------------------- #
clear()
stub_mb()
fake = stub_http(archive_only)
got = chain(CFG)

assert got["asked"] == ["rateyourmusic"], got["asked"]
assert got["skipped"] == {}, got["skipped"]
# One request, to the ARCHIVE. Not one to the live site: without a credential
# RYM refuses an unattended client, so the live route is not walked at all.
assert len(fake.calls) == 1, fake.calls
assert all("rateyourmusic.com" in url and "web.archive.org" in url
           for url in fake.calls), fake.calls

# Track 2 states its own genre on the archived page: that row answers it, and
# it comes FIRST because it is the track's own answer, not the album's.
assert got["per_track"][(1, 2)] == ["Industrial Metal", "Heavy Metal",
                                    "Groove Metal"], got["per_track"]
assert got["per_track_levels"][(1, 2)] == "track", got["per_track_levels"]
assert got["levels"][FILE_TWO] == "track", got["levels"]
# Track 1 has no row of its own: the album's anchors answer it — album level,
# and every track of the release gets them.
assert got["per_track"][(1, 1)] == ["Heavy Metal", "Groove Metal"], got["per_track"]
assert got["per_track_levels"][(1, 1)] == "album", got["per_track_levels"]
assert got["levels"][FILE_ONE] == "album", got["levels"]
assert got["level_counts"] == {"track": 1, "album": 1, "artist": 0}, got["level_counts"]
# The source is still RateYourMusic — the data is RYM's, the ROUTE is not.
assert got["per_track_sources"][(1, 2)] == ["rateyourmusic"], got["per_track_sources"]
assert got["sources"][FILE_ONE] == ["rateyourmusic"], got["sources"]

# The report says which route answered: an archived snapshot, its capture date,
# and why the live page was not the one that did.
note = got["notes"]["rateyourmusic"]
assert note.startswith("from an archived snapshot captured 2021-03-25"), note
assert "web.archive.org" in note and "rym_cookie" in note, note

# A page that states genres but NO track list is still an answer for every
# track, at album level — the tier that used to be dropped.
clear()
stub_mb()
fake = stub_http(lambda url, params: Response(
    200, PAGE_NO_ROWS, f"https://web.archive.org/web/{STAMP}id_/{TARGET}"))
got = chain(CFG)
assert got["per_track"] == {(1, 1): ["Heavy Metal", "Groove Metal"],
                            (1, 2): ["Heavy Metal", "Groove Metal"]}, got["per_track"]
assert got["level_counts"] == {"track": 0, "album": 2, "artist": 0}, got["level_counts"]

# --------------------------------------------------------------------------- #
# 2) The other shape a Wayback answer can take: the WRAPPER
# --------------------------------------------------------------------------- #
# Wayback's own toolbar, and its rewritten links (the page's own URLs behind a
# `/web/<timestamp>/` prefix), must not survive into the scrape: `href="/genre/
# <slug>/"` is the anchor RYM serves, and a rewritten one — absolute, prefixed —
# is an anchor the scrapers cannot see. A wrapped page that parses as "states no
# genres" would be a snapshot silently refused.
WRAPPED = (
    '<html><head>'
    '<!-- BEGIN WAYBACK TOOLBAR INSERT --><div id="wm-ipp-base"></div>'
    '<!-- END WAYBACK TOOLBAR INSERT --></head><body>'
    '<h1 class="album_title">Test Album</h1>'
    f'<a href="https://web.archive.org/web/{STAMP}/https://rateyourmusic.com/'
    'artist/test-artist">Test Artist</a>'
    f'<a href="https://web.archive.org/web/{STAMP}id_/https://rateyourmusic.com/'
    'genre/heavy-metal">Heavy Metal</a>'
    f'<a href="https://web.archive.org/web/{STAMP}id_/https://rateyourmusic.com/'
    'genre/groove-metal">Groove Metal</a>'
    '<div class="tracklist_row"><span class="tracklist_num">1</span>'
    '<span class="tracklist_title">Track One</span></div>'
    '</body></html>')
clear()
stub_mb()
stub_http(lambda url, params: Response(
    200, WRAPPED, f"https://web.archive.org/web/{STAMP}id_/{TARGET}"))
got = chain(CFG)
assert got["per_track"][(1, 1)] == ["Heavy Metal", "Groove Metal"], got["per_track"]
assert got["notes"]["rateyourmusic"].startswith("from an archived snapshot"), \
    got["notes"]

# --------------------------------------------------------------------------- #
# 3) The artist tier is reachable the same way (and stays BELOW the album)
# --------------------------------------------------------------------------- #
clear()
stub_mb()


def release_gone(url, params):
    """No capture of the release page at all; the artist page is archived."""
    if url.startswith(intg.RYM_ARCHIVE_INDEX):
        return Response(200, cdx(), url)          # the index lists no capture
    if "/artist/test-artist" in url:
        return Response(200, ARTIST_PAGE,
                        f"https://web.archive.org/web/{STAMP}id_/{ARTIST_TARGET}")
    assert "/release/album/" in url, url
    return Response(404, NOT_ARCHIVED, url)


fake = stub_http(release_gone)
got = chain(CFG)
# The album page is tried first (the tier order), then the artist page.
assert "/release/album/" in fake.calls[0], fake.calls
assert "/artist/" in fake.calls[-1], fake.calls
assert got["per_track"][(1, 1)] == ["Alternative Metal", "Nu Metal"], got["per_track"]
assert got["levels"] == {FILE_ONE: "artist", FILE_TWO: "artist"}, got["levels"]
assert got["notes"]["rateyourmusic"].startswith("from an archived snapshot"), \
    got["notes"]

# --------------------------------------------------------------------------- #
# 4) (b) A Cloudflare interception is never parsed into genres
# --------------------------------------------------------------------------- #
# The newest capture IS the interstitial (RYM answered the crawler with it), and
# so is the capture the index offers: nothing may be read from either.
clear()
stub_mb()


def all_challenged(url, params):
    if url.startswith(intg.RYM_ARCHIVE_INDEX):
        return Response(200, cdx(STAMP), url)
    return Response(200, CHALLENGE, url)


fake = stub_http(all_challenged)
got = chain(CFG)
assert got["per_track"] == {}, got["per_track"]
assert got["per_source"] == {}, got["per_source"]
assert got["per_track_sources"] == {}, got["per_track_sources"]
assert "rateyourmusic" not in got["skipped"], got["skipped"]
# The report says what was tried and came of it, instead of an empty answer.
note = got["notes"]["rateyourmusic"]
assert note.startswith("no rym_cookie in Settings → Discovery, and no archived "
                       "snapshot"), note
# Nothing from the interstitial became a genre, at any level or in any field.
blob = json.dumps(got).lower()
for word in ("just a moment", "checking your browser", "cloudflare"):
    assert word not in blob, word

# The shape this was verified against live: the NEWEST capture is the
# interstitial replayed with its original 403, while the index lists a capture
# from before RYM started challenging crawlers. That older capture answers — and
# the report carries ITS date, not the junk one's.
clear()
stub_mb()


def challenged_then_older(url, params):
    if url.startswith(intg.RYM_ARCHIVE_INDEX):
        return Response(200, cdx(OLD_STAMP, STAMP), url)
    if f"/web/{OLD_STAMP}id_/" in url:
        return Response(200, PAGE,
                        f"https://web.archive.org/web/{OLD_STAMP}id_/{TARGET}")
    assert "/web/2id_/" in url or f"/web/{STAMP}id_/" in url, url
    return Response(403, CHALLENGE, url)


fake = stub_http(challenged_then_older)
got = chain(CFG)
assert got["per_track"][(1, 1)] == ["Heavy Metal", "Groove Metal"], got["per_track"]
assert got["notes"]["rateyourmusic"].startswith(
    "from an archived snapshot captured 2021-02-10"), got["notes"]
assert any(f"/web/2id_/" in url for url in fake.calls), fake.calls
assert any(intg.RYM_ARCHIVE_INDEX in url for url in fake.calls), fake.calls
assert any(f"/web/{OLD_STAMP}id_/" in url for url in fake.calls), fake.calls

# --------------------------------------------------------------------------- #
# 5) (c) Fallback OFF and no cookie is today's skip, byte for byte
# --------------------------------------------------------------------------- #
SKIP_NO_COOKIE = ("skipped: no rym_cookie in Settings → Discovery "
                  "(RateYourMusic refuses an automated client without one)")
clear()
stub_mb()
fake = stub_http(never)
got = chain(CFG_OFF)
assert got["skipped"]["rateyourmusic"] == SKIP_NO_COOKIE, got["skipped"]
assert got["notes"]["rateyourmusic"] == SKIP_NO_COOKIE, got["notes"]
assert "rateyourmusic" not in got["asked"], got["asked"]
assert fake.calls == [], fake.calls
# …and a cfg that never came through DEFAULT_CONFIG does not get the fallback
# either: the key's absence is the pre-archive behaviour (`_rym_archive_on`),
# which is what keeps a bare cfg's "ask RYM and see" from turning into a
# Wayback request.
clear()
stub_mb()
fake = stub_http(never)
got = chain({"mb_genre_count": 3})
assert got["skipped"]["rateyourmusic"] == SKIP_NO_COOKIE, got["skipped"]
assert fake.calls == [], fake.calls

# --------------------------------------------------------------------------- #
# 6) (d) The live page and the archived page agree: one source, two routes
# --------------------------------------------------------------------------- #
clear()
stub_mb()


def live(url, params):
    assert url.startswith("https://rateyourmusic.com/"), url
    return Response(200, PAGE, url)


stub_http(live)
local = chain(CFG_COOKIE)
assert "rateyourmusic" not in local["notes"], local["notes"]   # live: nothing to say
assert "rateyourmusic" in local["asked"], local["asked"]

clear()
stub_mb()
stub_http(archive_only)
snapshot = chain(CFG)

assert snapshot["per_track"] == local["per_track"], (snapshot["per_track"],
                                                     local["per_track"])
assert snapshot["per_track_sources"] == local["per_track_sources"], \
    snapshot["per_track_sources"]
assert snapshot["per_track_levels"] == local["per_track_levels"], \
    snapshot["per_track_levels"]
assert snapshot["per_source"] == local["per_source"], (snapshot["per_source"],
                                                        local["per_source"])
# The one difference is the provenance: the snapshot says it is one.
assert local["notes"] == {}, local["notes"]

# --------------------------------------------------------------------------- #
# 7) (e) The snapshot answer is cached — the second import asks nobody
# --------------------------------------------------------------------------- #
tmp = tempfile.mkdtemp(prefix="mlo_rym_archive_")
try:
    intg._data_cache_dir = lambda name: os.path.join(tmp, name)
    clear()
    stub_mb()
    fake = stub_http(archive_only)
    first = chain(CFG)
    fetched = list(fake.calls)
    assert fetched, fetched
    # The in-process memos go; what answers now is the 30-day disk cache.
    clear()
    again = chain(CFG)
    assert again["per_track"] == first["per_track"], again["per_track"]
    assert fake.calls == fetched, fake.calls[len(fetched):]
    # The capture's identity survives the cache with the bytes it belongs to:
    # a cached answer can still say which snapshot it is and how old.
    assert again["notes"]["rateyourmusic"] == first["notes"]["rateyourmusic"], \
        again["notes"]
    assert "2021-03-25" in again["notes"]["rateyourmusic"], again["notes"]
finally:
    shutil.rmtree(tmp, ignore_errors=True)
    intg._data_cache_dir = lambda name: None

# --------------------------------------------------------------------------- #
# 8) MusicBrainz's tiers, labelled by where their names came from
# --------------------------------------------------------------------------- #
# The same release with MusicBrainz answering at every tier it has: the
# recording's own genres, the release's, its release group's, and the artist's.
MB_RELEASE = dict(RELEASE, genres=["Rock"], media=[
    {"disc": 1, "position": 1, "title": "Track One", "recording_mbid": "rec-1",
     "genres": ["Alternative Metal"]},
    {"disc": 1, "position": 2, "title": "Track Two", "recording_mbid": "rec-2",
     "genres": []},
])
clear()
stub_mb({"release-group/rg-1": {"genres": [{"name": "Progressive Rock"}]},
         "artist/art-1": {"genres": [{"name": "Art Rock"}]}})
got = chain(CFG, release=MB_RELEASE, sources=["musicbrainz"], limit=8)
# recording → release → release group → artist, in that order, per track.
assert got["per_track"][(1, 1)] == ["Alternative Metal", "Rock",
                                    "Progressive Rock", "Art Rock"], got["per_track"]
assert got["per_track_levels"][(1, 1)] == "track", got["per_track_levels"]
# No recording genre on track 2: the release and its group answer it, and that
# is the ALBUM tier (with the artist's genre behind it, not instead of it).
assert got["per_track"][(1, 2)] == ["Rock", "Progressive Rock", "Art Rock"], \
    got["per_track"]
assert got["per_track_levels"][(1, 2)] == "album", got["per_track_levels"]
# The artist tier only when nothing above it states anything — and then it is
# labelled `artist`, never `album`: the label is where the NAMES came from. A
# track whose RECORDING states a genre still holds the track tier.
clear()
stub_mb({"artist/art-1": {"genres": [{"name": "Art Rock"}]}})
bare = dict(MB_RELEASE, genres=[])
got = chain(CFG, release=bare, sources=["musicbrainz"], limit=8)
assert got["per_track"][(1, 1)] == ["Alternative Metal", "Art Rock"], got["per_track"]
assert got["per_track"][(1, 2)] == ["Art Rock"], got["per_track"]
assert got["per_track_levels"][(1, 2)] == "artist", got["per_track_levels"]
assert got["levels"] == {FILE_ONE: "track", FILE_TWO: "artist"}, got["levels"]
assert got["level_counts"] == {"track": 1, "album": 0, "artist": 1}, got["level_counts"]

print("rym archive: all assertions passed")
