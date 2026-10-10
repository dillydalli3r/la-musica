#!/usr/bin/env python3
"""Per-track genre import (server.integrations.genre_chain) — offline contract.

What this pins, with every HTTP seam stubbed (no network at all):

  * the DEFAULT priority list is an explicit, documented promise:
    rateyourmusic → albumoftheyear → musicbrainz, equal to
    `mlo.config.DEFAULT_CONFIG["genre_sources"]` — the only three genre sources
    this build knows;
  * the sources are asked IN ORDER until every track is FULL: the writer's own
    policy decides that (`_genre_complete` — at `mb_genre_count = 2` one
    specific genre plus its derived family), a source below that point is never
    asked (`asked`/`stopped_after`), and a source that cannot answer is skipped
    BEFORE any request — no credential (RateYourMusic's `rym_cookie`,
    Album of the Year's `aoty_cookie`, or a Cloudflare solver URL) — with the
    reason in `notes`/`skipped`;
  * every source that IS asked answers at its best tier: MusicBrainz answers
    per track (recording genres, then the work's) and album-wide
    (release group → release → artist), while RYM answers per track where its
    page states one and Album of the Year answers album-wide — labelled
    `level: album`/`level: artist` where that is what answered, never promoted
    to a track (`level_counts` totals those tiers);
  * the merged order is `genre_sources` order, so a reversed list reverses the
    result;
  * the two scraped sources are read LIVE only — the genre chain has no
    archive/Wayback route (`_rym_archive_on`/`_aoty_archive_on` belong to the
    RYM links/charts features, not here);
  * the cap (`mb_genre_count`) is enforced PER TRACK, and a track's own
    recording genres stay ahead of the release-wide ones;
  * a source that fails (a blocked RYM, a raising adapter) removes only its
    own genres — the other sources still answer, and the failure is reported in
    `notes`;
  * RateYourMusic's release page is mapped onto our tracks by position, then
    title, a row that states its own genre is `level: track`, and an
    album-level (genre or descriptor) fallback is applied to every track and
    marked `level: album`;
  * Album of the Year is fetched through `intg.aoty_page` (monkeypatchable) and
    parsed with `intg.aoty.parse_album`; its album page is the album-level
    answer and its artist page the fallback;
  * both previously shipped default `genre_sources` lists migrate to the
    current default; a customised list is kept as written;
  * an id this build no longer knows (a stale saved list entry) returns no
    answer at all — never an album-wide guess;
  * `sources` names the contributing sources per path IN ORDER, and
    `levels` says which tier answered;
  * nothing is invented: every source empty yields no genres, no per-track
    map, no provenance and no exception;
  * the two advisory extras stay in their last place and out of the way: a
    Discogs Parental Advisory format (token only) and YouTube's 18+ gate for
    a video the track's own tags record — never a blind per-track search.

Run:  python tools/test_genres.py
"""
import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import config as mcfg
from server import discovery
from server import integrations as intg

# No disk caches — they belong to a real app run and would outlive these
# stubs — no politeness sleeps and no host throttling: the transport is
# stubbed here, so the wait would only make the run slow.
intg._data_cache_dir = lambda name: None
intg.RYM_MIN_INTERVAL = 0.0
discovery._HOST_WAIT.clear()

# The priority list the module documents and ships (see the GENRE_SOURCES
# comment in server/integrations.py), spelled out here so a silent reorder
# fails the run instead of passing unnoticed.
DOCUMENTED_SOURCES = ["rateyourmusic", "albumoftheyear", "musicbrainz"]

# The default configuration (no saved `genre_sources`): the chain must use the
# module default. Every test below therefore runs the REAL priority list.
# A cookie is what makes a scraped source an ASKED source at all: without one
# the chain skips it before any request (no credential = no page, see
# `_genre_source_skip`), which is its own test in §5.
CFG = {"mb_genre_count": 3, "rym_cookie": "cf_clearance=test",
       "aoty_cookie": "cf_clearance=aoty"}
# An explicit, hand-picked list — for the order-honouring tests (§1 reversed,
# §9) where a shorter list keeps the expectation readable.
ORDER = ["rateyourmusic", "albumoftheyear", "musicbrainz"]

FILE_ONE = "1-01 Track One.flac"
FILE_TWO = "1-02 Track Two.flac"

RELEASE = {
    "id": "rel-1",
    "title": "Test Album",
    "release_group_id": "rg-1",
    "genres": ["Rock"],
    "artists": [{"name": "Test Artist", "mbid": "art-1"}],
    "media": [
        {"disc": 1, "position": 1, "title": "Track One",
         "recording_mbid": "rec-1", "genres": ["Alternative Metal"]},
        {"disc": 1, "position": 2, "title": "Track Two",
         "recording_mbid": "rec-2", "genres": ["Nu Metal"]},
    ],
}

# The same release with NO MusicBrainz genres of its own: what a release whose
# only answer is another source's album-level one looks like.
BARE_RELEASE = dict(RELEASE, genres=[], media=[
    {"disc": 1, "position": 1, "title": "Track One", "recording_mbid": "rec-1",
     "genres": []},
    {"disc": 1, "position": 2, "title": "Track Two", "recording_mbid": "rec-2",
     "genres": []},
])

# A release page: album genres in the header block, and a track list whose
# SECOND row carries a genre of its own (which is the per-track signal).
# The title/artist header is part of the stub because the fetch VERIFIES the
# page is the release it asked for: an unverifiable page yields nothing.
RYM_PAGE = (
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
# The same page with descriptors instead of genres: descriptors are the only
# classification such a page carries, so they are the album-level answer.
RYM_DESCRIPTOR_PAGE = (
    '<html><body><h1 class="album_title">Test Album</h1>'
    '<a href="/artist/test-artist">Test Artist</a>'
    '<div class="album_descriptors">'
    '<a href="/descriptor/concept-album">Concept Album</a>'
    '</div><table class="tracklist">'
    '<tr class="tracklist_row"><td class="tracklist_track_num">1</td>'
    '<td class="tracklist_track_title">Track One</td></tr>'
    '</table></body></html>')
# A release page in the markup RYM actually serves — an excerpt of a live
# capture, kept where it matters: the `+`-spaced genre slugs with RYM's own
# capitals, the primary/secondary genre blocks, and the `tracklist_line` /
# `tracklist_num` / `tracklist_title` row shape a client without a logged-in
# cookie gets (the desktop table spells those
# `tracklist_row`/`tracklist_track_num`/`tracklist_track_title`, which the other
# fixtures here cover). A lowercase-and-dash-only genre class drops every
# multi-word genre on this page, which is most of RYM's value.
RYM_LIVE_PAGE = (
    '<html><head><title>Dragging a Dead Deer Up a Hill by Grouper (Album, '
    'Psychedelic Folk)</title></head><body>'
    '<h1 class="album_title_main">Dragging a Dead Deer Up a Hill</h1>'
    '<a href="/artist/grouper">Grouper</a>'
    '<span class="release_pri_genres"><a  class="genre" '
    'href="/genre/Psychedelic+Folk/">Psychedelic Folk</a>, '
    '<a  class="genre" href="/genre/Ambient/">Ambient</a></span>'
    '<span class="release_sec_genres"><a  class="genre" '
    'href="/genre/Dream+Pop/">Dream Pop</a>, '
    '<a  class="genre" href="/genre/Drone/">Drone</a>, '
    '<a  class="genre" href="/genre/Ethereal+Wave/">Ethereal Wave</a></span>'
    '<ul id="tracks_mobile" class="tracks tracklisting">'
    '<li class="track"><div class="tracklist_line" style="width:100%;">'
    '<span class="tracklist_num">                      1                   </span>'
    '<span class="tracklist_title"><span><span class="rendered_text">Disengaged'
    '</span></span><span class="tracklist_duration" data-inseconds="256">'
    '                      4:16                   </span></span>'
    '<div style="clear:both;"></div></div></li>'
    '<li class="track"><div class="tracklist_line" style="width:100%;">'
    '<span class="tracklist_num">                      2                   </span>'
    '<span class="tracklist_title"><span><span class="rendered_text">Heavy Water '
    '/ I&#39;d Rather Be Sleeping'
    '</span></span></span><div style="clear:both;"></div></div></li>'
    '</ul></body></html>')
RYM_CHALLENGE = ("<html><head><title>Just a moment...</title></head>"
                 "<body>Checking your browser before accessing "
                 "rateyourmusic.com</body></html>")

# An Album of the Year album page: the genre row is `Genre`-labelled and the
# genre links are read in the order AOTY prints them (headline names, then the
# `secondary` ones). `aoty.parse_album` reads this into `{"genres", ...}`, and
# the genre chain takes that album-level row as AOTY's answer.
AOTY_ALBUM_PAGE = (
    '<div class="detailRow"><a href="/genre/5-shoegaze/">Shoegaze</a>, '
    '<a href="/genre/34-dream-pop/">Dream Pop</a> '
    '<span>/&nbsp;Genre</span></div>')
# The artist page, AOTY's fallback when an album page states no genre row.
AOTY_ARTIST_PAGE = (
    '<div class="detailRow"><a href="/genre/9-alternative-rock/">'
    'Alternative Rock</a> <span>/&nbsp;Genre</span></div>')


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #
class Response:
    def __init__(self, status, text, url):
        self.status_code, self.text, self.url = status, text, url


class FakeCookies(dict):
    """`httpx.Cookies` as the RYM code needs it: the jar a cookie paste seeds,
    which the transport reads back off every request it serves."""

    def set(self, name, value, domain="", path="/"):
        self[str(name)] = value


class FakeHttpx:
    """RateYourMusic's seam (`integrations.httpx`)."""

    HTTPError = RuntimeError
    Cookies = FakeCookies

    def __init__(self, routes, boom=False):
        self.routes, self.boom, self.calls = dict(routes), boom, []

    def get(self, url, params=None, headers=None, timeout=None,
            follow_redirects=None, cookies=None):
        self.calls.append(url)
        if self.boom:
            raise RuntimeError("no connection")
        for key, payload in self.routes.items():
            if key in url:
                return Response(200, payload, url)
        return Response(404, "not found", url)


def stub_rym(routes=None, boom=False):
    fake = FakeHttpx(routes or {}, boom=boom)
    intg.httpx = fake
    return fake


def stub_json(router):
    """Replace discovery's throttled JSON transport. Returns the call log."""
    calls = []

    def fake(url, params=None, headers=None, timeout=None, ttl=None, host=None):
        calls.append((url, dict(params or {})))
        return router(url, dict(params or {}))

    discovery._json = fake
    return calls


def stub_mb(payloads):
    calls = []

    def fake(endpoint, params=None, timeout=None, retries=None):
        calls.append(endpoint)
        return payloads.get(endpoint, {})

    intg.mb_get = fake
    intg.mb_get_cached = fake
    return calls


def stub_aoty(album=None, artist=None):
    """Album of the Year's page seam (`integrations.aoty_page`).

    The genre chain reaches the scraped page through `aoty_page` (and its
    `aoty_album_page` wrapper), so replacing that one function stubs the whole
    source: `kind` says which page was asked for, and the return is the HTML
    `aoty.parse_album` / `aoty.parse_artist` will read. Returns the call log
    (list of `kind`s).
    """
    calls = []

    def fake(artist_name, album_name="", cfg=None, kind="album"):
        calls.append(kind)
        return {"album": album, "artist": artist}.get(kind)

    intg.aoty_page = fake
    return calls


# MusicBrainz holds BOTH relations for one release group: the Wikidata link
# (inc=url-rels) and the genre list (inc=genres).
MB_DEFAULT = {
    "release-group/rg-1": {
        "relations": [{"type": "wikidata",
                       "url": {"resource": "https://www.wikidata.org/wiki/Q202996"}}],
        "genres": [{"name": "Progressive Rock"}]},
    "artist/art-1": {"genres": [{"name": "Art Rock"}]},
}


def clear():
    intg._GENRE_CACHE.clear()
    intg._ADVISORY_CACHE.clear()
    discovery._CACHE.clear()
    discovery._HOST_WARNED.clear()
    intg._rym_warned = False
    intg._rym_failures = 0
    intg._bandcamp_warned = False


def full_stack(**overrides):
    """Every source answering, with `overrides` replacing a page/router.

    RateYourMusic (its live release page), Album of the Year (its album page)
    and MusicBrainz all answer; a caller can silence any of them by passing
    `rym`, `aoty_album`/`aoty_artist` or `mb`.
    """
    stub_rym({"rateyourmusic.com": overrides.get("rym", RYM_PAGE)})
    stub_aoty(overrides.get("aoty_album", AOTY_ALBUM_PAGE),
              overrides.get("aoty_artist", AOTY_ARTIST_PAGE))
    stub_json(lambda url, params: None)
    stub_mb(overrides.get("mb") or MB_DEFAULT)


def chain(limit=None, release=None, **kwargs):
    return intg.genre_chain(artist="Test Artist", album="Test Album",
                            release=release or RELEASE, cfg=CFG, limit=limit,
                            files=[FILE_ONE, FILE_TWO], **kwargs)


# --------------------------------------------------------------------------- #
# 1) Source order, per-track answers, provenance and the cap
# --------------------------------------------------------------------------- #
clear()
full_stack()
got = chain(limit=20)

# RateYourMusic's album genres first, then Album of the Year's album row, then
# MusicBrainz's own per-recording answer and its release-group/release/artist
# tiers — the shipped order, source by source.
assert got["per_track"][(1, 1)] == [
    "Heavy Metal", "Groove Metal",                    # rateyourmusic (album)
    "Shoegaze", "Dream Pop",                          # albumoftheyear (album)
    "Alternative Metal",                              # musicbrainz (recording)
    "Progressive Rock",                               # musicbrainz (release group)
    "Rock",                                           # musicbrainz (release)
    "Art Rock",                                       # musicbrainz (artist)
], got["per_track"][(1, 1)]
# The source order holds across the whole merge: a source asked later can
# never jump one asked earlier.
assert (got["per_track"][(1, 1)].index("Heavy Metal")
        < got["per_track"][(1, 1)].index("Shoegaze"))
assert (got["per_track"][(1, 1)].index("Shoegaze")
        < got["per_track"][(1, 1)].index("Alternative Metal"))

# Provenance: the contributing sources, in ask order, per PATH — and the
# per-track map is keyed the same way the ONE writer (`_write_album_genres`)
# looks its entries up, so the route can hand it straight over.
from server import imports

assert imports._parse_trackno(FILE_ONE) in got["per_track"]
assert got["sources"][FILE_ONE] == ["rateyourmusic", "albumoftheyear",
                                    "musicbrainz"], got["sources"]
assert got["per_track_sources"][(1, 1)] == got["sources"][FILE_ONE]
# `levels` reports the MOST SPECIFIC tier that answered this track — with
# MusicBrainz's per-recording genres in the list, that is the track level (an
# album-only answer is "album", see §3).
assert got["levels"][FILE_ONE] == "track", got["levels"]

# Track 2 states its own genre on the RYM page, so that row answers it — and
# the track level is what the provenance reports.
assert got["per_track"][(1, 2)][0] == "Industrial Metal", got["per_track"][(1, 2)]
assert got["levels"][FILE_TWO] == "track", got["levels"]
assert got["sources"][FILE_TWO][0] == "rateyourmusic", got["sources"]

# The chain follows `genre_sources`: reversing the order reverses the merge.
clear()
full_stack()
reversed_got = intg.genre_chain(artist="Test Artist", album="Test Album",
                                release=RELEASE, cfg=CFG, limit=6,
                                sources=list(reversed(ORDER)),
                                files=[FILE_ONE])
assert reversed_got["per_track"][(1, 1)] == ["Alternative Metal",
                                             "Progressive Rock", "Rock",
                                             "Art Rock", "Shoegaze",
                                             "Dream Pop"], \
    reversed_got["per_track"][(1, 1)]
# MusicBrainz and Album of the Year together fill the 6-slot list, so the
# chain stops before RateYourMusic — the early stop, honoured per the list.
assert reversed_got["sources"][FILE_ONE] == ["musicbrainz", "albumoftheyear"], \
    reversed_got["sources"]

# The cap is enforced PER TRACK, not across the album — and once every track
# holds a list the writer would write in full (RYM answered two specifics, and
# the family is derived from the first), the chain STOPS: the sources below it
# could only repeat what is already there, so they are never asked.
clear()
full_stack()
got = chain()
assert got["per_track"][(1, 1)] == ["Heavy Metal", "Groove Metal"], got["per_track"]
assert got["per_track"][(1, 2)] == ["Industrial Metal", "Heavy Metal",
                                    "Groove Metal"], got["per_track"]
assert got["genres"] == ["Heavy Metal", "Groove Metal",
                         "Industrial Metal"], got["genres"]
assert got["asked"] == ["rateyourmusic"], got["asked"]
assert got["stopped_after"] == "rateyourmusic", got["stopped_after"]
# The report says how much each source said (its release-wide row answers both
# tracks, and its one per-track row answers a third time), and what the cap
# left out.
assert got["per_source_counts"]["rateyourmusic"] == {
    "names": 3, "tracks": 3}, got["per_source_counts"]
assert got["per_track_trimmed"] == {}, got["per_track_trimmed"]
# A different cap is honoured too.
clear()
full_stack()
assert len(chain(limit=1)["per_track"][(1, 2)]) == 1

# --------------------------------------------------------------------------- #
# 2) Reliability — one source failing never removes the others'
# --------------------------------------------------------------------------- #
# (a) blocked RYM, everything else answering: one log line, no exception.
clear()
stub_rym({"rateyourmusic.com": RYM_CHALLENGE})
stub_aoty(AOTY_ALBUM_PAGE, AOTY_ARTIST_PAGE)
stub_mb({"release-group/rg-1": {"genres": [{"name": "Progressive Rock"}]}})
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    got = chain(limit=6)
assert got["notes"]["rateyourmusic"].startswith(
    "skipped: RateYourMusic refused this cookie"), got["notes"]
# Album of the Year is asked before MusicBrainz in the shipped order, so its
# own album answer is what leads the merged list.
assert got["per_track"][(1, 1)][0] == "Shoegaze", got["per_track"]
assert "rateyourmusic" not in got["sources"][FILE_ONE], got["sources"]
assert buf.getvalue().count("rateyourmusic") == 1, buf.getvalue()

# (b) a host that raises (timeout / no route): the others still answer, and
# the dead source is simply absent from the provenance.
clear()
stub_rym({"rateyourmusic.com": RYM_PAGE})

def dead_aoty(artist, album="", cfg=None, kind="album"):
    raise RuntimeError("connection reset")

intg.aoty_page = dead_aoty
stub_mb({})
got = chain(limit=6)
assert got["notes"]["albumoftheyear"].startswith("failed: "), got["notes"]
assert "albumoftheyear" not in got["per_source"], got["per_source"]
assert got["per_track"][(1, 1)][:2] == ["Heavy Metal", "Groove Metal"], got["per_track"]
assert got["sources"][FILE_ONE] == ["rateyourmusic", "musicbrainz"], got["sources"]

# (c) a source that raises inside the module (a scrape that blew up) is
# reported as failed, with the traceback message, and removes only itself.
clear()
full_stack()
_real_rym = intg.rym_genres
intg.rym_genres = lambda artist, album: 1 / 0
try:
    got = chain(limit=6)
finally:
    intg.rym_genres = _real_rym
assert got["notes"]["rateyourmusic"].startswith("failed: "), got["notes"]
# Album of the Year is asked before MusicBrainz in the shipped order, so its
# own album answer is what leads the merged list.
assert got["per_track"][(1, 1)][0] == "Shoegaze", got["per_track"]

# --------------------------------------------------------------------------- #
# 3) RateYourMusic — per-track rows, title mapping, album fallback
# --------------------------------------------------------------------------- #
# A page with a track list but no genres at all: nothing is mapped by position,
# so the descriptors carry the album-level answer.
clear()
stub_rym({"rateyourmusic.com": RYM_DESCRIPTOR_PAGE})
stub_aoty(None, None)
stub_mb({})
got = chain(release=BARE_RELEASE)
assert got["per_source"]["rateyourmusic"] == ["Concept Album"], got["per_source"]
assert got["per_track"] == {(1, 1): ["Concept Album"], (1, 2): ["Concept Album"]}, got["per_track"]
assert got["levels"] == {FILE_ONE: "album", FILE_TWO: "album"}, got["levels"]
assert got["sources"][FILE_TWO] == ["rateyourmusic"], got["sources"]

# RYM's own track order need not line up with ours: when the position does
# not match, the TITLE is what maps a row onto a track.
clear()
titled = RYM_PAGE.replace('tracklist_track_num">1<', 'tracklist_track_num">7<')
stub_rym({"rateyourmusic.com": titled})
stub_aoty(None, None)
stub_mb({})
got = chain(limit=6, release=BARE_RELEASE)
assert got["per_track"][(1, 2)] == ["Industrial Metal", "Heavy Metal",
                                    "Groove Metal"], got["per_track"]
assert got["levels"][FILE_TWO] == "track", got["levels"]
assert got["levels"][FILE_ONE] == "album", got["levels"]

# The RELEASE URLs the module derives, and the order it tries them in. RYM's
# release slugs are UNDERSCORE-separated and keep their punctuation runs (every
# spelling here is one MusicBrainz states on the release group); its artist
# pages are dash-separated — and asking for an album with the artist spelling
# is what made every multi-word album 404 on its first candidate.
assert intg._rym_release_slug("Sgt. Pepper's Lonely Hearts Club Band") == \
    "sgt__peppers_lonely_hearts_club_band"
assert intg._rym_release_slug("In Rainbows") == "in_rainbows"
assert intg._rym_release_slug("The Beatles") == "the_beatles"
assert intg._rym_release_slug("Simon & Garfunkel") == "simon_and_garfunkel"
assert intg._rym_slug("The Beatles") == "the-beatles"      # the ARTIST page
# RYM's older pages kept dashes and were never rewritten, so both are tried.
assert intg._rym_release_candidates("The Dark Side of the Moon") == \
    ["the_dark_side_of_the_moon", "the-dark-side-of-the-moon"]

# The page MusicBrainz states is asked for AS STATED — no slug guessed at all.
# (`cfg={}` keeps these deterministic: the live config is not read.)
clear()
fake = stub_rym({"rateyourmusic.com": RYM_PAGE})
page = intg.rym_genres(
    "Test Artist", "Test Album", cfg={},
    album_url="https://rateyourmusic.com/release/album/test-artist/test_album/")
assert page["genres"] == ["Heavy Metal", "Groove Metal"], page
assert fake.calls == ["https://rateyourmusic.com/release/album/test-artist/test_album/"], \
    fake.calls
# With no stated page, the ladder starts at RYM's own underscore spelling.
clear()
fake = stub_rym({"rateyourmusic.com": RYM_PAGE})
page = intg.rym_genres("Test Artist", "Test Album", cfg={})
assert page["genres"] == ["Heavy Metal", "Groove Metal"], page
assert fake.calls[0] == \
    "https://rateyourmusic.com/release/album/test_artist/test_album/", fake.calls
# An artist or song URL is not a release page, so it is never read as one.
assert intg._rym_path_from_url("https://rateyourmusic.com/artist/radiohead") == ""
assert intg._rym_path_from_url("https://rateyourmusic.com/release/song/x/y/") == ""
assert intg._rym_path_from_url(
    "https://rateyourmusic.com/release/album/radiohead/in_rainbows/") == \
    "/release/album/radiohead/in_rainbows/"

# RYM's own page markup, parsed by the source's own entry points: every genre
# on it comes back (the multi-word ones included) and the mobile track list is
# read row by row.
assert intg._rym_genres_from(RYM_LIVE_PAGE) == [
    "Psychedelic Folk", "Ambient", "Dream Pop", "Drone", "Ethereal Wave"], \
    intg._rym_genres_from(RYM_LIVE_PAGE)
rows = intg._rym_tracks_from(RYM_LIVE_PAGE)
assert [r["title"] for r in rows] == ["Disengaged",
                                      "Heavy Water / I'd Rather Be Sleeping"], rows
assert [r["position"] for r in rows] == [1, 2], rows
# …and the decoded title is what maps a row onto our own track, exactly as the
# page's own entity spelling would otherwise fail to (`_rym_ref` folds it).
assert intg._rym_ref(rows[1]["title"]) == \
    intg._rym_ref("Heavy Water / I\u2019d Rather Be Sleeping"), rows[1]

clear()
fake = stub_rym({"rateyourmusic.com": RYM_LIVE_PAGE})
stub_aoty(None, None)
stub_mb({})
live_release = {"id": "rel-g", "release_group_id": "rg-g", "genres": [],
                "artists": [{"name": "Grouper", "mbid": "art-g"}],
                "media": [{"disc": 1, "position": 1, "title": "Disengaged",
                           "genres": []}]}
got = intg.genre_chain(artist="Grouper", album="Dragging a Dead Deer Up a Hill",
                       release=live_release, cfg=CFG, limit=6, files=[FILE_ONE])
assert got["per_source"]["rateyourmusic"] == [
    "Psychedelic Folk", "Ambient", "Dream Pop", "Drone", "Ethereal Wave"], \
    got["per_source"]
assert got["per_track"][(1, 1)] == [
    "Psychedelic Folk", "Ambient", "Dream Pop", "Drone", "Ethereal Wave"], \
    got["per_track"]
assert got["levels"][FILE_ONE] == "album", got["levels"]

# --------------------------------------------------------------------------- #
# 4) Album of the Year — album row parsed through `aoty_page`, artist fallback
# --------------------------------------------------------------------------- #
# The album page's `Genre` row is the album-level answer, applied to every
# track and labelled `album` — AOTY classifies the release, not a track.
clear()
aoty = stub_aoty(AOTY_ALBUM_PAGE, AOTY_ARTIST_PAGE)
stub_rym({"rateyourmusic.com": RYM_CHALLENGE})
stub_mb({})
got = chain(limit=6, release=BARE_RELEASE)
assert got["per_source"]["albumoftheyear"] == ["Shoegaze", "Dream Pop"], got["per_source"]
assert got["per_track"][(1, 1)] == ["Shoegaze", "Dream Pop"], got["per_track"]
assert got["levels"] == {FILE_ONE: "album", FILE_TWO: "album"}, got["levels"]
assert aoty.count("album") == 1, aoty

# An album page with no genre row falls back to the ARTIST page — and the row
# is labelled `artist`, never promoted to the album.
clear()
aoty = stub_aoty(None, AOTY_ARTIST_PAGE)
stub_rym({"rateyourmusic.com": RYM_CHALLENGE})
stub_mb({})
got = chain(limit=6, release=BARE_RELEASE)
assert got["per_source"]["albumoftheyear"] == ["Alternative Rock"], got["per_source"]
assert got["per_track"][(1, 1)] == ["Alternative Rock"], got["per_track"]
assert got["levels"] == {FILE_ONE: "artist", FILE_TWO: "artist"}, got["levels"]
assert aoty == ["album", "artist"], aoty

# --------------------------------------------------------------------------- #
# 5) Scraped sources gated on their credentials; unknown ids never guessed
# --------------------------------------------------------------------------- #
# RateYourMusic is gated on its credential: no `rym_cookie` means no page (RYM
# refuses an automated client), so the chain does not spend a request and a
# second of throttle learning that.
clear()
fake = stub_rym({"rateyourmusic.com": RYM_PAGE})
stub_aoty(None, None)
stub_mb({})
got = intg.genre_chain(artist="Test Artist", album="Test Album",
                       release=BARE_RELEASE, cfg={"mb_genre_count": 3},
                       limit=6, files=[FILE_ONE, FILE_TWO],
                       sources=["rateyourmusic", "albumoftheyear", "musicbrainz"])
assert got["notes"]["rateyourmusic"].startswith("skipped: no rym_cookie"), got["notes"]
assert "rateyourmusic" not in got["asked"], got["asked"]
assert fake.calls == [], fake.calls          # not one request to RYM

# Album of the Year is gated the same way, on `aoty_cookie` (or a solver).
clear()
aoty = stub_aoty(AOTY_ALBUM_PAGE, AOTY_ARTIST_PAGE)
stub_rym({"rateyourmusic.com": RYM_CHALLENGE})
stub_mb({})
got = intg.genre_chain(artist="Test Artist", album="Test Album",
                       release=BARE_RELEASE, cfg={"mb_genre_count": 3},
                       limit=6, files=[FILE_ONE, FILE_TWO],
                       sources=["albumoftheyear", "musicbrainz"])
assert got["notes"]["albumoftheyear"].startswith("skipped: no aoty_cookie"), got["notes"]
assert "albumoftheyear" not in got["asked"], got["asked"]
assert aoty == [], aoty                       # not one page fetched

# An id this build no longer knows (a stale saved list entry) is asked, gets
# nothing, and is reported as "no data" — never an album-wide guess.
clear()
stub_rym({"rateyourmusic.com": RYM_CHALLENGE})
stub_aoty(None, None)
stub_mb({})
got = intg.genre_chain(artist="Test Artist", album="Test Album",
                       release=BARE_RELEASE, cfg=CFG, limit=6,
                       files=[FILE_ONE, FILE_TWO],
                       sources=["listenbrainz", "musicbrainz"])
assert got["notes"]["listenbrainz"] == "no data", got["notes"]
assert "listenbrainz" not in got["per_source"], got["per_source"]
assert got["asked"] == ["listenbrainz", "musicbrainz"], got["asked"]

# --------------------------------------------------------------------------- #
# 6) Nothing invented when every source is empty
# --------------------------------------------------------------------------- #
clear()
stub_rym({}, boom=True)
stub_aoty(None, None)
stub_mb({})
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    got = chain(release=BARE_RELEASE, sources=ORDER)
assert got["genres"] == [] and got["per_track"] == {}, got
assert got["per_track_sources"] == {} and got["per_source"] == {}, got
assert got["sources"] == {FILE_ONE: [], FILE_TWO: []}, got["sources"]
assert got["levels"] == {FILE_ONE: None, FILE_TWO: None}, got["levels"]
assert set(got["notes"]) == set(ORDER), got["notes"]

# --------------------------------------------------------------------------- #
# 7) The two advisory extras — last, explicit-only, out of the way when absent
# --------------------------------------------------------------------------- #
# A YouTube id is read from the track's OWN tags only: an 11-character id or a
# YouTube URL, never a token scraped out of an unrelated SOURCE value.
assert intg.youtube_video_id({"YOUTUBEID": "dQw4w9WgXcQ"}) == "dQw4w9WgXcQ"
assert intg.youtube_video_id({"SOURCE": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}) == "dQw4w9WgXcQ"
assert intg.youtube_video_id({"SOURCE": "https://youtu.be/dQw4w9WgXcQ?t=1"}) == "dQw4w9WgXcQ"
assert intg.youtube_video_id({"SOURCE": "CD"}) == ""
assert intg.youtube_video_id({"SOURCE": "Vinyl"}) == ""
assert intg.youtube_video_id(None) == ""


# No advisory source answers here (they are stubbed to "no data"), which is
# what lets the two extras be checked by `checked` alone.
intg._advisory_json = lambda url, params=None, **kwargs: None

# Nobody is asked and nothing is added when no id is known.
intg._ADVISORY_CACHE.clear()
route = intg.resolve_advisory_route(title="Track One", artist="Test Artist",
                                    album="Test Album", cfg={})
assert route["checked"] == ["apple-album", "itunes-song"], route
assert route["answers"] == {} and route["value"] is None, route

# A known video id is asked, and only ever adds an explicit signal.
_real_age = intg.youtube_age_advisory
intg.youtube_age_advisory = lambda vid, cfg=None: (1, "youtube-age")
try:
    intg._ADVISORY_CACHE.clear()
    route = intg.resolve_advisory_route(title="Track One", artist="Test Artist",
                                        album="Test Album", cfg={},
                                        tags={"SOURCE": "https://youtu.be/dQw4w9WgXcQ"})
finally:
    intg.youtube_age_advisory = _real_age
assert route["checked"][-1] == "youtube-age", route
assert route["answers"]["youtube-age"] == 1 and route["value"] == 1, route
assert route["source"] == "youtube-age", route

# Discogs' Parental Advisory format is ALBUM/EDITION-level evidence, so it
# must never rate a track: one flagged edition would otherwise make every
# track of the release read explicit (the owner's "tagging tracks as explicit
# even when they aren't"). The advisory route does not ask Discogs at all any
# more — even with a token, a flagged release adds NOTHING to a track.
def discogs_router(formats):
    def router(url, params):
        if "api.discogs.com/releases" in url:
            return {"genres": ["Rock"], "formats": formats}
        if "api.discogs.com/database" in url:
            return {"results": [{"id": 42}]}
        return None

    return router


sticker = [{"name": "CD", "descriptions": ["Album", "Parental Advisory"]}]
intg._ADVISORY_CACHE.clear()
stub_json(discogs_router(sticker))
route = intg.resolve_advisory_route(title="Track One", artist="Test Artist",
                                    album="Test Album", cfg={"discogs_token": "t"})
assert "discogs-parental" not in route["checked"], route
assert route["answers"] == {} and route["value"] is None, route
assert route["source"] is None, route
# ... the album-level flag is not a source any caller may attribute a value to
assert "discogs-parental" not in intg.ADVISORY_SOURCES, intg.ADVISORY_SOURCES

# --------------------------------------------------------------------------- #
# 8) The 30-day cache answers the second run instead of the network
# --------------------------------------------------------------------------- #
clear()
import tempfile

folder = tempfile.mkdtemp(prefix="mlo-genre-cache-")
intg._data_cache_dir = lambda name: os.path.join(folder, name) if name else None
try:
    rym = stub_rym({"rateyourmusic.com": RYM_PAGE})
    aoty = stub_aoty(AOTY_ALBUM_PAGE, AOTY_ARTIST_PAGE)
    stub_mb(MB_DEFAULT)
    first = chain(limit=12)
    assert first["per_track"][(1, 1)][0] == "Heavy Metal", first["per_track"]
    rym_calls, aoty_calls = len(rym.calls), len(aoty)
    # The cap is what keeps the other sources in play: RateYourMusic alone
    # fills only a part of a list of 12, and a cache test with nothing cached
    # beyond it would prove nothing about them.
    assert rym_calls and aoty_calls, (rym_calls, aoty_calls)

    # Forget the in-process memos only: what answers now is the DISK cache.
    intg._GENRE_CACHE.clear()
    discovery._CACHE.clear()
    again = chain(limit=12)
    assert again["per_track"] == first["per_track"], again["per_track"]
    assert again["sources"] == first["sources"], again["sources"]
    assert len(rym.calls) == rym_calls, rym.calls[rym_calls:]
    assert len(aoty) == aoty_calls, aoty[aoty_calls:]
finally:
    intg._data_cache_dir = lambda name: None

# --------------------------------------------------------------------------- #
# 9) The default priority list is the documented one, both old defaults migrate
# --------------------------------------------------------------------------- #
assert list(intg.GENRE_SOURCES) == DOCUMENTED_SOURCES, intg.GENRE_SOURCES
# The registry and the SHIPPED default are the same list: every source is both
# selectable in Settings → Discovery / listed in Sources health AND asked by
# default, because the chain stops as soon as a track's list is complete.
SHIPPED_DEFAULT = list(DOCUMENTED_SOURCES)
assert list(mcfg.DEFAULT_CONFIG["genre_sources"]) == SHIPPED_DEFAULT, \
    mcfg.DEFAULT_CONFIG["genre_sources"]
# The chains earlier releases shipped are not a choice the user made: both
# are swapped for the current default. A list they actually edited is theirs.
for legacy in list(mcfg.LEGACY_DEFAULT_GENRE_SOURCES) + [mcfg.LEGACY_GENRE_SOURCES]:
    assert list(mcfg.normalize_config(
        {"genre_sources": list(legacy)})["genre_sources"]) == SHIPPED_DEFAULT, legacy
custom = ["albumoftheyear", "musicbrainz"]
assert mcfg.normalize_config({"genre_sources": list(custom)})["genre_sources"] == custom
# No saved list at all is the same "no choice" case.
assert list(mcfg.normalize_config({})["genre_sources"]) == SHIPPED_DEFAULT
# And a saved list is what the chain runs — the config default is not forced
# over a hand-picked order.
clear()
full_stack()
picked = intg.genre_chain(artist="Test Artist", album="Test Album",
                          release=RELEASE,
                          cfg=dict(CFG, genre_sources=["albumoftheyear",
                                                       "musicbrainz"]),
                          limit=6, files=[FILE_ONE])
assert picked["sources"][FILE_ONE] == ["albumoftheyear", "musicbrainz"], picked["sources"]

# --------------------------------------------------------------------------- #
# 10) The route surface — the background job, and the inline call it replaces
# --------------------------------------------------------------------------- #
# The chain is minutes of provider traffic, so the wizard starts it as a job
# and polls. Everything below runs the REAL routes against a temp music folder
# with the chain itself stubbed: what is pinned here is the job's state machine
# (`running` → `done`/`error`), the 409 guard, and the verbatim error text — the
# chain's own answers are covered above.
import shutil as _shutil
import tempfile as _tempfile

from fastapi.testclient import TestClient  # noqa: E402  (heavy import)

from server import main as mlo_main  # noqa: E402

JOB_MUSIC = _tempfile.mkdtemp(prefix="mlo-genre-job-")
JOB_ALBUM = os.path.join(JOB_MUSIC, "Artists", "Job Artist", "2020 - Job Album")
JOB_EMPTY = os.path.join(JOB_MUSIC, "Artists", "Empty Album")
JOB_OUTSIDE = _tempfile.mkdtemp(prefix="mlo-genre-outside-")
os.makedirs(JOB_ALBUM)
os.makedirs(JOB_EMPTY)
# A real (if silent) MP3 frame sequence: the routes name audio by extension and
# open the first file, and a junk byte string would be an unreadable track.
_MP3_FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * 413


def _write_mp3(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(_MP3_FRAME * 40)
    return path


JOB_TRACK = _write_mp3(os.path.join(JOB_ALBUM, "1-01 One.mp3"))
_write_mp3(os.path.join(JOB_ALBUM, "1-02 Two.mp3"))
OUTSIDE_TRACK = _write_mp3(os.path.join(JOB_OUTSIDE, "1-01 Out.mp3"))

mlo_main.load_config = lambda *a, **k: {"music_folder": JOB_MUSIC,
                                        "mb_genre_count": 3}
_client = TestClient(mlo_main.app)   # no lifespan: no workers, no slskd boot

JOB_SOURCES = ("rateyourmusic", "albumoftheyear", "musicbrainz")
JOB_NOTES = {"rateyourmusic": "no data", "musicbrainz": "answered"}
# The chain's report, in the shape `_run_genre_chain` passes through: per-source
# counts, what was actually asked and where it stopped, what was skipped and
# why, which tier answered, and the cap's own trail.
JOB_RESULT = {"updated": 2, "genres": ["Shoegaze"],
              "per_source": {"musicbrainz": ["Shoegaze"]}, "notes": JOB_NOTES,
              "per_track": {(1, 1): ["Shoegaze"]}, "sources": {}, "levels": {},
              "per_source_counts": {"musicbrainz": {"names": 1, "tracks": 2}},
              "asked": ["musicbrainz"], "stopped_after": "musicbrainz",
              "order": ["rateyourmusic", "albumoftheyear", "musicbrainz"],
              "skipped": {"rateyourmusic": "skipped: no rym_cookie"},
              "level_counts": {"track": 1, "album": 1, "artist": 0},
              # The chain keys this by (disc, position) TUPLES. A tuple key
              # used to blow up FastAPI's jsonable_encoder (plain-text 500 on
              # every import that trimmed a genre), so the route must render
              # it with the app's own "disc:position" string key.
              "per_track_trimmed": {(1, 1): ["Shoegaze", "Dream Pop"]},
              "trimmed": []}
_asked = []
_real_genre_chain = intg.genre_chain


def _fake_chain(sources=None, **kw):
    """The chain stand-in: it records which sources it was asked for.

    There is no genre CHAIN in the import wizard any more — its two buttons
    each ask ONE source — so what this suite has to hold is that `sources`
    reaches the chain as given, and that omitting it still means "every
    configured source" (the album/track menu's call)."""
    _asked.append(None if sources is None else list(sources))
    return dict(JOB_RESULT)


intg.genre_chain = _fake_chain
try:
    # One source per call: the wizard's "Genres from MusicBrainz" and
    # "Genres from RateYourMusic" buttons, each answered by the ONE source.
    for source in ("musicbrainz", "rateyourmusic"):
        one = _client.post("/api/genres/import",
                           json={"paths": [JOB_ALBUM], "sources": [source]})
        assert one.status_code == 200, one.text
        body = one.json()
        assert _asked[-1] == [source], _asked
        assert body["genres"] == ["Shoegaze"], body
        assert body["per_source"] == {"musicbrainz": ["Shoegaze"]}, body
        # The chain's own report reaches the caller, and it keeps its own
        # sentence for the cases a name alone cannot express: a track answered
        # at ALBUM level, and a chain that stopped early.
        assert body["per_source_counts"] == {
            "musicbrainz": {"names": 1, "tracks": 2}}, body
        assert body["asked"] == ["musicbrainz"], body
        assert body["stopped_after"] == "musicbrainz", body
        assert body["skipped"] == {"rateyourmusic": "skipped: no rym_cookie"}, body
        assert body["level_counts"] == {"track": 1, "album": 1, "artist": 0}, body
        assert body["notes"]["rateyourmusic"] == JOB_NOTES["rateyourmusic"], body
        assert body["notes"]["musicbrainz"] == JOB_NOTES["musicbrainz"], body
        assert "ALBUM or ARTIST" in body["notes"]["genre level"], body
        assert "stopped after musicbrainz" in body["notes"]["genre sources"], body
        assert set(body) >= {"updated", "genres", "per_source", "notes", "per_track",
                             "sources", "levels", "per_source_counts", "asked",
                             "stopped_after", "skipped", "level_counts",
                             "trimmed_files", "trimmed_genres"}, sorted(body)
        # The trimmed map reaches the caller keyed by "disc:position", never
        # by the chain's own (disc, position) tuple — the pre-fix route
        # answered a plain-text 500 here instead of this body.
        assert body["trimmed_genres"] == {"1:1": ["Shoegaze", "Dream Pop"]}, body

    # No `sources` at all still means every configured source, in order.
    every = _client.post("/api/genres/import", json={"paths": [JOB_ALBUM]})
    assert every.status_code == 200, every.text
    assert _asked[-1] is None, _asked

    # The chain JOB is gone: the wizard polls nothing, so nothing answers.
    assert _client.get("/api/genres/import/job").status_code in (404, 405)
    assert _client.post("/api/genres/import/job",
                        json={"paths": [JOB_ALBUM]}).status_code in (404, 405)

    # A folder with no audio is the SERVER's own message, verbatim.
    empty = _client.post("/api/genres/import", json={"paths": [JOB_EMPTY]})
    assert empty.status_code == 400, empty.text
    assert empty.json()["detail"] == "no audio files found", empty.text

    # A path outside the music folder is named — and accepted only when the
    # import wizard says this is the album it is editing (staged), which is
    # how its two buttons work on a folder that is not in the library yet.
    outside = _client.post("/api/genres/import", json={"paths": [OUTSIDE_TRACK]})
    assert outside.status_code == 400, outside.text
    assert outside.json()["detail"] == f"path outside music folder: {OUTSIDE_TRACK}", outside.text
    staged = _client.post("/api/genres/import",
                          json={"paths": [OUTSIDE_TRACK], "staged": True})
    assert staged.status_code == 200, staged.text
finally:
    intg.genre_chain = _real_genre_chain
    _shutil.rmtree(JOB_MUSIC, ignore_errors=True)
    _shutil.rmtree(JOB_OUTSIDE, ignore_errors=True)

# --------------------------------------------------------------------------- #
# 11) NO source answered: the MODEL is the fallback (the owner's ask)
# --------------------------------------------------------------------------- #
# The sources are the first move and the model is the second, and an empty
# merged list is the case that made it unreachable: `genre_chain` used to skip
# a track nothing answered for, so a model configured to research a genre was
# never asked about exactly the track that needed it. With `ai_genre_research`
# (on by default) the model may NAME the genres, and its answer must be a real
# MusicBrainz genre to survive (`server.genre_ai`).
print()
print("== no source answered: the model researches the genre ==")
from server import ai as _ai_mod
from server import genre_ai as _genre_ai
_real_ai_configured, _real_infer = _ai_mod.ai_configured, _genre_ai.infer_genres
_ai_calls = []


def _ai_configured(cfg=None):
    return bool((cfg or {}).get("ai_genre_inference", True))


def _infer(*, artist="", album="", title="", candidates=None, count=2, extra=None):
    _ai_calls.append({"artist": artist, "album": album, "title": title,
                      "candidates": list(candidates or []), "count": count})
    # The model's own order, most specific first — what a researching answer
    # looks like next to the empty candidate list it was asked with.
    return ["Shoegaze", "Dream Pop"]


def _silent_sources():
    """Every source stubbed to answer NOTHING (RYM's page included: no
    cookie, so not even one request)."""
    clear()
    stub_rym({})
    stub_aoty(None, None)
    stub_mb({})


_ai_mod.ai_configured = _ai_configured
_genre_ai.infer_genres = _infer
try:
    _silent_sources()
    _ai_calls.clear()
    got = intg.genre_chain(artist="Test Artist", album="Test Album",
                           release=BARE_RELEASE, cfg=dict(CFG, ai_genre_inference=True),
                           limit=3, files=[FILE_ONE, FILE_TWO],
                           sources=["musicbrainz", "albumoftheyear"])
    assert _ai_calls, "the model was never asked about a track nothing answered for"
    assert got["per_track"][(1, 1)] and got["per_track"][(1, 2)], got["per_track"]
    assert "shoegaze" in [g.casefold() for g in got["per_track"][(1, 1)]], got["per_track"]
    assert "ai" in got["per_track_sources"][(1, 1)], got["per_track_sources"]
    # The ask carries the track's own identity, which is what lets the model
    # research the right song rather than the album's general sound.
    assert _ai_calls[0]["title"] == "Track One", _ai_calls[0]
    assert _ai_calls[0]["candidates"] == [], _ai_calls[0]

    # ...and with the model switched OFF nothing is invented: an album whose
    # sources answered nothing stays that way.
    _silent_sources()
    _ai_calls.clear()
    off = intg.genre_chain(artist="Test Artist", album="Test Album",
                           release=BARE_RELEASE, cfg=dict(CFG, ai_genre_inference=False),
                           limit=3, files=[FILE_ONE, FILE_TWO],
                           sources=["musicbrainz", "albumoftheyear"])
    assert off["per_track"] == {}, off["per_track"]
    assert _ai_calls == [], _ai_calls
finally:
    _ai_mod.ai_configured, _genre_ai.infer_genres = _real_ai_configured, _real_infer

print("genres: all assertions passed")
