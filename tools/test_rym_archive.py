#!/usr/bin/env python3
"""RateYourMusic's scrape contract — live genre reads and the archive route.

"Import from RateYourMusic" has to produce real RYM genres, and the owner's
rule is that the scrape reads the LIVE page: a cleared `rym_cookie` (or a
Cloudflare solver, `cf_solver_url`) is what makes a request answer, and a
config with neither SKIPS the source before any request rather than paying one
to confirm the refusal. The Wayback Machine is no longer part of the GENRE
chain — but it still backs the RYM CHARTS the Discover surface reads, and this
file pins that surviving archive route too.

With the HTTP layer stubbed (no network at all) and the module's disk caches
off unless a test asks for one, this pins:

  * a cookie makes RateYourMusic asked LIVE: the release page is scraped by the
    same readers as always — a row that states its own genre is `level: track`
    for that track, the album's own anchors are `level: album` for every track,
    and NOT ONE web.archive.org request is made;
  * the tier chain holds end to end: the RYM track row beats the album page, the
    album page beats the artist page, and a page that states genres but NO track
    rows still fills the track map at album level;
  * a config with NO cookie and NO solver — including a bare cfg that never went
    through `mlo.config.DEFAULT_CONFIG` — SKIPS RYM with the `rym_cookie`
    reason and makes not one request;
  * MusicBrainz's own tiers are labelled by where their names came from —
    recording → release group → release → artist — with an artist-only row
    marked `artist` and never `album`, because a mislabelled tier both
    misreports the provenance and outranks the next source's album tier;
  * RateYourMusic is the FIRST source asked and its names LEAD the merged,
    capped list — the shipped `genre_sources` (and `web_ratings_sources`) put
    it in slot 1 — and a track RYM alone completes stops the chain before the
    next source is paid a request;
  * the RYM CHARTS' archive route still works: with `rym_archive_fallback` on
    and no cookie, a chart page is read from the Wayback Machine, a wrapped
    capture is stripped before the scrape, a Cloudflare interception is NEVER
    parsed (the capture index is asked for a page instead), and the answer is
    cached for 30 days;
  * the refusal is SURFACED, not only logged: `server.sources_health`'s RYM
    genre row is `skipped` with the `rym_cookie` reason when nothing can answer
    it, `ok` (and probed live) with a cookie, `ok` but cookie-less when a solver
    is set, and reports RYM's own sentence plus the `rym_last` response (403,
    challenge marker, URL) when a saved cookie is refused.

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

# The transport is stubbed, so the politeness sleeps would only make this slow;
# and no disk caches by default — they belong to a real app run and would
# outlive these stubs (§6 sets one back up for its own scenario).
intg.RYM_MIN_INTERVAL = 0.0
intg.RYM_ARCHIVE_MIN_INTERVAL = 0.0
intg._data_cache_dir = lambda name: None

FILE_ONE = "1-01 Track One.flac"
FILE_TWO = "1-02 Track Two.flac"

# Nothing pasted in, and no solver either: the two scraped genre sources have no
# route, so RYM is SKIPPED before a request.
CFG = {"mb_genre_count": 3}
# A pasted cookie, so the LIVE page is the route that answers.
CFG_COOKIE = {"mb_genre_count": 3, "rym_cookie": "cf_clearance=test"}
# The chart archive route is the one the fallback key still governs.
CFG_ARCHIVE = {"mb_genre_count": 3, "rym_archive_fallback": True}

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

# A release page as RYM serves it, live or archived — the SAME snippet is used
# for both, which is the point: the snapshot carries RYM's own markup, so the
# scrapers read it identically. The album states two genres and its SECOND row
# states one of its own, which is the per-track signal; the header carries the
# artist and the album, which is what verifies the page really is this release
# before anything is read off it.
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

# A chart page as RYM serves it — the surviving SCRAPE the archive route reads.
CHART_PATH = "/charts/top/song/all-time"
CHART_TARGET = intg.RYM_BASE + CHART_PATH
CHART_PAGE = (
    '<html><head><title>Best songs of all time - Rate Your Music</title></head>'
    '<body><div id="pos1" class="chart_item">'
    '<a class="page_charts_section_charts_item_link chart_item_link" '
    'href="/song/slowdive/sugar-for-the-pill/">'
    '<span class="ui_name_locale_original">Sugar for the Pill</span></a>'
    '<a class="artist" href="/artist/slowdive">Slowdive</a></div>'
    '<div id="pos2" class="chart_item">'
    '<a class="page_charts_section_charts_item_link chart_item_link" '
    'href="/song/slowdive/star-roving/">'
    '<span class="ui_name_locale_original">Star Roving</span></a>'
    '<a class="artist" href="/artist/slowdive">Slowdive</a></div>'
    '</body></html>')


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


def live(page):
    """A router that answers the LIVE site with one page, in its own bytes."""
    def router(url, params):
        assert url.startswith("https://rateyourmusic.com/"), url
        return Response(200, page, url)
    return router


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


# --------------------------------------------------------------------------- #
# 1) A cookie: RYM is read LIVE, per track and per album
# --------------------------------------------------------------------------- #
clear()
stub_mb()
fake = stub_http(live(PAGE))
got = chain(CFG_COOKIE)

assert got["asked"] == ["rateyourmusic"], got["asked"]
assert got["skipped"] == {}, got["skipped"]
# Not one request to the archive: the genre chain has no Wayback route.
assert fake.calls and all(url.startswith("https://rateyourmusic.com/")
                          for url in fake.calls), fake.calls
assert not any("web.archive.org" in url for url in fake.calls), fake.calls

# Track 2 states its own genre on the page: that row answers it, and it comes
# FIRST because it is the track's own answer, not the album's.
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
# The source is still RateYourMusic, and a live answer has nothing to explain.
assert got["per_track_sources"][(1, 2)] == ["rateyourmusic"], got["per_track_sources"]
assert got["sources"][FILE_ONE] == ["rateyourmusic"], got["sources"]
assert "rateyourmusic" not in got["notes"], got["notes"]

# A page that states genres but NO track list is still an answer for every
# track, at album level.
clear()
stub_mb()
stub_http(live(PAGE_NO_ROWS))
got = chain(CFG_COOKIE)
assert got["per_track"] == {(1, 1): ["Heavy Metal", "Groove Metal"],
                            (1, 2): ["Heavy Metal", "Groove Metal"]}, got["per_track"]
assert got["level_counts"] == {"track": 0, "album": 2, "artist": 0}, got["level_counts"]

# --------------------------------------------------------------------------- #
# 2) The artist tier is reachable the same way (and stays BELOW the album)
# --------------------------------------------------------------------------- #
clear()
stub_mb()


def release_gone(url, params):
    """The release page is not there; the artist page is."""
    if "/artist/" in url:
        return Response(200, ARTIST_PAGE, url)
    return Response(404, NOT_ARCHIVED, url)


fake = stub_http(release_gone)
got = chain(CFG_COOKIE)
# The album page is tried first (the tier order), then the artist page.
assert any("/release/album/" in url for url in fake.calls), fake.calls
assert "/artist/" in fake.calls[-1], fake.calls
assert got["per_track"][(1, 1)] == ["Alternative Metal", "Nu Metal"], got["per_track"]
assert got["levels"] == {FILE_ONE: "artist", FILE_TWO: "artist"}, got["levels"]

# --------------------------------------------------------------------------- #
# 3) No cookie and no solver is TODAY'S skip, byte for byte
# --------------------------------------------------------------------------- #
SKIP_NO_COOKIE = ("skipped: no rym_cookie in Settings → Discovery "
                  "(RateYourMusic refuses an automated client without one; "
                  "or set cf_solver_url)")
clear()
stub_mb()
fake = stub_http(never)
got = chain(CFG)
assert got["skipped"]["rateyourmusic"] == SKIP_NO_COOKIE, got["skipped"]
assert got["notes"]["rateyourmusic"] == SKIP_NO_COOKIE, got["notes"]
assert "rateyourmusic" not in got["asked"], got["asked"]
assert fake.calls == [], fake.calls
# …and a cfg that never came through DEFAULT_CONFIG is the same state: the
# absence of a cookie is the absence of a route, never an "ask RYM and see"
# that would spend a request the site is known to refuse.
clear()
stub_mb()
fake = stub_http(never)
got = chain({"mb_genre_count": 3})
assert got["skipped"]["rateyourmusic"] == SKIP_NO_COOKIE, got["skipped"]
assert fake.calls == [], fake.calls

# --------------------------------------------------------------------------- #
# 4) MusicBrainz's tiers, labelled by where their names came from
# --------------------------------------------------------------------------- #
# The same release with MusicBrainz answering at every tier it has: the
# recording's own genres, its release group's, the release's, and the artist's.
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
# recording → release group → release → artist, in that order, per track: the
# release GROUP leads the album tier (a release group is what "the album"
# means to MusicBrainz; the release's own names are merged behind it).
assert got["per_track"][(1, 1)] == ["Alternative Metal", "Progressive Rock",
                                    "Rock", "Art Rock"], got["per_track"]
assert got["per_track_levels"][(1, 1)] == "track", got["per_track_levels"]
# No recording genre on track 2: the group and the release answer it, and that
# is the ALBUM tier (with the artist's genre behind it, not instead of it).
assert got["per_track"][(1, 2)] == ["Progressive Rock", "Rock", "Art Rock"], \
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

# --------------------------------------------------------------------------- #
# 5) RateYourMusic is the FIRST source asked, and its names LEAD the merge
# --------------------------------------------------------------------------- #
# The shipped default is a PRIORITY list, and RateYourMusic holds slot 1 of it:
# the chain asks it first and the merged, capped list keeps its names ahead of
# every later source's. This is what "preferred" means here — not "its answer
# is the only one", which the family-first merge rules would contradict.
from mlo.config import DEFAULT_CONFIG           # noqa: E402
from mlo import web_ratings                     # noqa: E402

# The three lists a preference could live in, all pinned to the same order:
# the genre registry the chain reads (`server.integrations.GENRE_SOURCES`), the
# shipped config the Settings form writes, and the web-rating registry (album
# AND the track-level subset).
assert intg.GENRE_SOURCES[0] == "rateyourmusic", intg.GENRE_SOURCES
assert list(DEFAULT_CONFIG["genre_sources"]) == intg.GENRE_SOURCES, \
    DEFAULT_CONFIG["genre_sources"]
assert web_ratings.SOURCES[0] == "rateyourmusic", web_ratings.SOURCES
assert list(DEFAULT_CONFIG["web_ratings_sources"]) == web_ratings.SOURCES, \
    DEFAULT_CONFIG["web_ratings_sources"]
assert web_ratings.TRACK_SOURCES[0] == "rateyourmusic", web_ratings.TRACK_SOURCES

# Two sources, both able to answer. The cap is set ABOVE what RYM alone states
# (4 slots vs its two names) so the chain cannot stop before MusicBrainz:
# RYM is first in `asked`, and its genres sit ahead of MusicBrainz's in the
# merged per-track list (the merge walks the ask order).
MB_PAYLOADS = {"release-group/rg-1": {"genres": [{"name": "Progressive Rock"}]},
               "artist/art-1": {"genres": []}}
clear()
stub_mb(MB_PAYLOADS)
stub_http(live(PAGE))
got = chain(CFG_COOKIE, sources=["rateyourmusic", "musicbrainz"], limit=4)
assert got["asked"] == ["rateyourmusic", "musicbrainz"], got["asked"]
names = got["per_track"][(1, 1)]
assert names[:2] == ["Heavy Metal", "Groove Metal"], names
assert "Progressive Rock" in names, names
assert names.index("Heavy Metal") < names.index("Progressive Rock"), names
# The report keeps the per-source provenance the payload carries: RYM's answer
# is attributed to rateyourmusic, MusicBrainz's to musicbrainz.
assert got["per_track_sources"][(1, 1)][:2] == ["rateyourmusic", "musicbrainz"], \
    got["per_track_sources"]

# And RYM FIRST means the later sources are not paid for an answer RYM already
# completed: at the shipped two-genre cap, one source's family+genre fills the
# track and MusicBrainz is never asked at all.
clear()
stub_mb(MB_PAYLOADS)
stub_http(live(PAGE))
filled = chain(CFG_COOKIE, sources=["rateyourmusic", "musicbrainz"], limit=2)
assert filled["asked"] == ["rateyourmusic"], filled["asked"]
assert filled["per_track"][(1, 1)] == ["Heavy Metal", "Groove Metal"], \
    filled["per_track"]

# --------------------------------------------------------------------------- #
# 6) The RYM CHARTS still read the archived snapshot (the genre chain does not)
# --------------------------------------------------------------------------- #
# The genre chain above never asks archive.org. The CHART scrape does: with
# `rym_archive_fallback` on and no cookie, the chart page is replayed from the
# Wayback Machine, and the archive's stamp travels with the answer.
def chart_archive(url, params):
    """Every chart request a cookie-less install makes is a Wayback one."""
    assert url.startswith("https://web.archive.org/"), url
    return Response(200, CHART_PAGE,
                    f"https://web.archive.org/web/{STAMP}id_/{CHART_TARGET}")


clear()
stub_mb()
fake = stub_http(chart_archive)
got = intg.rym_charts(kind="tracks", period="all", limit=10, cfg=CFG_ARCHIVE)
assert [row["title"] for row in got["rows"]] == ["Sugar for the Pill",
                                                 "Star Roving"], got["rows"]
assert got["chart"] == "Best songs of all time", got["chart"]
assert got["archive"]["snapshot"] == STAMP, got["archive"]
assert fake.calls and all(url.startswith("https://web.archive.org/")
                          for url in fake.calls), fake.calls

# A WRAPPED capture — Wayback's toolbar and its rewritten, prefixed links — is
# stripped before the scrape, so a wrapped page is not read as "no chart rows".
WRAPPED_CHART = (
    '<html><head><!-- BEGIN WAYBACK TOOLBAR INSERT -->'
    '<div id="wm-ipp-base"></div><!-- END WAYBACK TOOLBAR INSERT -->'
    '<title>Best songs of all time - Rate Your Music</title></head><body>'
    '<div id="pos1" class="chart_item">'
    '<a class="page_charts_section_charts_item_link chart_item_link" '
    f'href="https://web.archive.org/web/{STAMP}id_/https://rateyourmusic.com/'
    'song/slowdive/sugar-for-the-pill/">'
    '<span class="ui_name_locale_original">Sugar for the Pill</span></a>'
    f'<a class="artist" href="https://web.archive.org/web/{STAMP}/'
    'https://rateyourmusic.com/artist/slowdive">Slowdive</a></div>'
    '</body></html>')
clear()
stub_mb()
stub_http(lambda url, params: Response(
    200, WRAPPED_CHART, f"https://web.archive.org/web/{STAMP}id_/{CHART_TARGET}"))
got = intg.rym_charts(kind="tracks", period="all", limit=10, cfg=CFG_ARCHIVE)
assert [row["title"] for row in got["rows"]] == ["Sugar for the Pill"], got["rows"]

# The newest capture is the INTERSTITIAL: it is never parsed, the capture index
# is asked instead, and the older capture that IS a page answers — carrying ITS
# own date, not the junk one's.
clear()
stub_mb()


def challenged_then_older(url, params):
    if url.startswith(intg.RYM_ARCHIVE_INDEX):
        return Response(200, cdx(OLD_STAMP, STAMP), url)
    if f"/web/{OLD_STAMP}id_/" in url:
        return Response(200, CHART_PAGE,
                        f"https://web.archive.org/web/{OLD_STAMP}id_/{CHART_TARGET}")
    return Response(200, CHALLENGE, url)


fake = stub_http(challenged_then_older)
got = intg.rym_charts(kind="tracks", period="all", limit=10, cfg=CFG_ARCHIVE)
assert [row["title"] for row in got["rows"]] == ["Sugar for the Pill",
                                                 "Star Roving"], got["rows"]
assert got["archive"]["snapshot"] == OLD_STAMP, got["archive"]
blob = json.dumps(got).lower()
for word in ("just a moment", "checking your browser", "cloudflare"):
    assert word not in blob, word
assert any(intg.RYM_ARCHIVE_INDEX in url for url in fake.calls), fake.calls
assert any(f"/web/{OLD_STAMP}id_/" in url for url in fake.calls), fake.calls

# The chart answer is cached — the second call asks nobody, and the cached
# copy still says which capture it is.
tmp = tempfile.mkdtemp(prefix="mlo_rym_archive_")
try:
    intg._data_cache_dir = lambda name: os.path.join(tmp, name)
    clear()
    stub_mb()
    fake = stub_http(chart_archive)
    first = intg.rym_charts(kind="tracks", period="all", limit=10, cfg=CFG_ARCHIVE)
    fetched = list(fake.calls)
    assert fetched, fetched
    # The in-process memos go; what answers now is the 30-day disk cache.
    clear()
    again = intg.rym_charts(kind="tracks", period="all", limit=10, cfg=CFG_ARCHIVE)
    assert [row["title"] for row in again["rows"]] == \
        [row["title"] for row in first["rows"]], again["rows"]
    assert fake.calls == fetched, fake.calls[len(fetched):]
    assert again["archive"]["snapshot"] == STAMP, again["archive"]
finally:
    shutil.rmtree(tmp, ignore_errors=True)
    intg._data_cache_dir = lambda name: None

# --------------------------------------------------------------------------- #
# 7) The refusal is SURFACED, not only logged: the Sources row carries it
# --------------------------------------------------------------------------- #
# The reported bug the owner feels: RYM refuses (a stale cookie, a challenged
# network) and the only account of it is a log line. `sources_health` is the
# surface the Settings/Dependencies panel and the wizard read, and its RYM row
# must carry the refusal itself. The HTTP layer is stubbed (no network).
import server.sources_health as sh              # noqa: E402

_ALL_SPECS = sh._specs
# Probe ONLY the RYM rows: the other genre sources would each reach the real
# network, which this hermetic file must never do.
sh._specs = lambda kind=None: [s for s in _ALL_SPECS(kind)
                               if s["id"] == "rateyourmusic"]

# Nothing to ask with: no cookie, no solver. The cookie is ASKED for (`needs`
# drives the panel's paste field), the row is `skipped`, and nothing is probed
# (the `never` router would fail on any request).
clear()
stub_mb({})
fake = stub_http(never)
payload = sh.health_payload(CFG, kind="genre", probe=True)
row = payload["sources"][0]
assert row["id"] == "rateyourmusic" and row["rank"] == 1, row
assert row["needs"] == ["rym_cookie"], row["needs"]
assert row["configured"] is False, row
assert "optional_needs" not in row, row
assert row["status"] == "skipped", row
assert row["detail"] == "needs rym_cookie", row["detail"]
assert fake.calls == [], fake.calls

# A pasted cookie: the row is configured and probed LIVE for real.
PROBE_PAGE = (
    '<html><body><h1 class="album_title">Pablo Honey</h1>'
    '<a href="/artist/radiohead">Radiohead</a>'
    '<div class="album_genres">'
    '<a href="/genre/alternative-rock">Alternative Rock</a>'
    '<a href="/genre/grunge">Grunge</a>'
    '</div></body></html>')
clear()
stub_mb({})
fake = stub_http(live(PROBE_PAGE))
payload = sh.health_payload(CFG_COOKIE, kind="genre", probe=True)
row = payload["sources"][0]
assert row["configured"] is True, row
assert row["status"] == "ok", row
assert row["detail"] == "2 genres", row["detail"]
assert fake.calls and all("rateyourmusic.com" in url for url in fake.calls), fake.calls

# A solver stands in for the cookie: `configured` is still False (nothing was
# pasted), but the row is RUNNABLE and says so through `optional_needs`.
clear()
stub_mb({})
payload = sh.health_payload({"cf_solver_url": "http://solver:8191"},
                            kind="genre", probe=False)
row = payload["sources"][0]
assert row["configured"] is False, row
assert row["status"] == "ok", row
assert row["optional_needs"] == ["rym_cookie"], row
assert "cf_solver_url" in row["detail"], row["detail"]

# The owner's case: a saved cookie RYM then refuses. The row reports `skipped`
# with RYM's own sentence AND the response behind it (`rym_last`) — status,
# the challenge marker and the URL — which is what separates a stale cookie
# from a blocked network on the panel.
clear()
stub_mb({})
stub_http(lambda url, params: Response(
    403, CHALLENGE, "https://rateyourmusic.com/release/album/radiohead/pablo_honey/"))
payload = sh.health_payload(CFG_COOKIE, kind="genre", probe=True)
row = payload["sources"][0]
assert row["status"] == "skipped", row
assert "Cloudflare challenge instead of a page (HTTP 403)" in row["detail"], row
assert "rym_cookie" in row["detail"], row["detail"]
assert row["rym_last"]["status"] == 403, row["rym_last"]
assert row["rym_last"]["challenge"] is True, row["rym_last"]
assert "rateyourmusic.com" in row["rym_last"]["url"], row["rym_last"]
assert "rym_cookie" in row["rym_last"]["reason"], row["rym_last"]

print("rym archive: all assertions passed")
