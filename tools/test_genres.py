#!/usr/bin/env python3
"""Per-track genre import (server.integrations.genre_chain) — offline contract.

What this pins, with every HTTP seam stubbed (no network at all):

  * the DEFAULT priority list is an explicit, documented promise:
    rateyourmusic → listenbrainz → musicbrainz → itunes → lastfm →
    theaudiodb → wikidata → bandcamp → discogs → deezer → spotify, equal to
    `mlo.config.DEFAULT_CONFIG["genre_sources"]`, with every per-track source
    ahead of every album-only one;
  * the sources are asked IN ORDER until every track is FULL: the writer's own
    policy decides that (`_genre_complete` — at `mb_genre_count = 2` one
    specific genre plus its derived family), a source below that point is never
    asked (`asked`/`stopped_after`), and a source that cannot answer is skipped
    BEFORE any request — no credential (RateYourMusic's cookie, Discogs'
    token, Last.fm's key, Spotify's id+secret), a RateYourMusic that already
    refused this cookie, or a documented no-op (Soulseek) — with the reason in
    `notes`/`skipped`;
  * every source that IS asked answers at its best tier: the per-track tiers
    answer per track where the source has one (MusicBrainz recording,
    ListenBrainz recording, iTunes `primaryGenreName`, Last.fm
    `track.getTopTags`, TheAudioDB `searchtrack.php`, Wikidata's recording
    P136, RYM's own rows), and a source that cannot answer per track answers at
    its album/artist tier, labelled `level: album`/`level: artist` — never
    promoted to a track (`level_counts` totals those tiers);
  * the merged order is `genre_sources` order, so a reversed list reverses the
    result, and an album-only source never outranks a per-track one;
  * ListenBrainz rows carrying a `genre_mbid` (recognised genres) beat the
    free tags, and a free tag is kept only when it is agreed on (count >= 2)
    and is not a mood word — a mood word and a count-1 tag are dropped;
  * ListenBrainz's per-recording tags come before its release-group bucket,
    which comes before its artist bucket;
  * the cap (`mb_genre_count`) is enforced PER TRACK, and a track's own
    recording genres stay ahead of the release-wide ones;
  * TheAudioDB's track row is preferred over its album row, and the album row
    is still the fallback for the tracks the track path did not answer;
  * Wikidata asks the RECORDING's own P136 first (the recording's Wikidata
    relation, then the "artist title" entity) and falls back to the release
    group / searched entity;
  * Bandcamp's album page is parsed for its tags (`data-tralbum` + the tag
    block), junk tags are filtered like ListenBrainz free tags, a page that is
    not this album is no answer, and the second run is served from the cache;
  * Spotify is artist-level (`level: artist`), asked only when its credentials
    are set, and last;
  * a source that fails (a blocked RYM, a dead host, a raising adapter) or is
    throttled removes only its own genres — the other sources still answer,
    and the failure is reported in `notes`;
  * RateYourMusic's release page is mapped onto our tracks by position, then
    title, a row that states its own genre is `level: track`, and an
    album-level (genre or descriptor) fallback is applied to every track and
    marked `level: album`;
  * both previously shipped default `genre_sources` lists migrate to the
    current default; a customised list is kept as written;
  * Last.fm and Discogs are skipped without their key/token — no request is
    made and they are reported in `notes`;
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
intg._apple_cache_dir = lambda: None
intg._APPLE_MIN_INTERVAL = 0.0
intg.RYM_MIN_INTERVAL = 0.0
intg.BANDCAMP_MIN_INTERVAL = 0.0
discovery._HOST_WAIT.clear()

# The priority list the module documents and ships (see the GENRE_SOURCES
# comment in server/integrations.py), spelled out here so a silent reorder
# fails the run instead of passing unnoticed.
DOCUMENTED_SOURCES = ["rateyourmusic", "listenbrainz", "musicbrainz", "itunes",
                      "lastfm", "theaudiodb", "wikidata", "bandcamp", "discogs",
                      "deezer", "spotify"]
# Which of them can ever state a TRACK's own genre, and which only know the
# album or the artist. The chain's promise is that the first group sits above
# the second (see the ordering test at the end).
PER_TRACK_SOURCES = ["rateyourmusic", "listenbrainz", "musicbrainz", "itunes",
                     "lastfm", "theaudiodb", "wikidata"]
WIDE_SOURCES = ["bandcamp", "discogs", "deezer", "spotify"]

# The default configuration (no saved `genre_sources`): the chain must use the
# module default. Every test below therefore runs the REAL priority list.
# The cookie is what makes RateYourMusic an ASKED source at all: without one
# the chain skips it before any request (no credential = no page, see
# `_genre_source_skip`), which is its own test in §5.
CFG = {"mb_genre_count": 3, "rym_cookie": "cf_clearance=test"}
# An explicit, hand-picked list — for the order-honouring tests (§1 reversed,
# §6) where a shorter list keeps the expectation readable.
ORDER = ["rateyourmusic", "listenbrainz", "musicbrainz", "itunes",
         "wikidata", "lastfm", "discogs"]

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
# A release page in the markup RYM actually serves — an excerpt of an archived
# capture, kept where it matters: the `+`-spaced genre slugs with RYM's own
# capitals (web.archive.org/web/20210325091401/https://rateyourmusic.com/release/
# album/grouper/dragging-a-dead-deer-up-a-hill/), the primary/secondary genre
# blocks, and the `tracklist_line` / `tracklist_num` / `tracklist_title` row
# shape a client without a logged-in cookie gets (the desktop table spells those
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

LB_ROWS = [
    # a recognised genre — the row ListenBrainz matched to its taxonomy
    {"tag": "Alternative Metal", "count": 7, "genre_mbid": "g-1"},
    {"tag": "post-metal", "count": 2, "genre_mbid": "g-2"},
    # free tags: agreed on (kept), a mood (dropped), and tag-spam (dropped)
    {"tag": "Sludge Metal", "count": 4},
    {"tag": "Melancholic", "count": 9},
    {"tag": "Dreamy", "count": 12},
    {"tag": "vyrzukhisuc-artiest", "count": 1},
]

# TheAudioDB's own answers: `searchtrack.php` answers per TRACK (its per-track
# tier) and `searchalbum.php` the album row (the fallback tier). `strMood` is a
# mood word, never a genre.
AUDIODB_TRACKS = {
    "Track One": {"strArtist": "Test Artist", "strTrack": "Track One",
                  "strGenre": "Shoegaze", "strStyle": "Dream Pop",
                  "strMood": "Dreamy"},
}
AUDIODB_ALBUM = {"strArtist": "Test Artist", "strGenre": "Post-Rock",
                 "strMood": "Epic"}

# A Bandcamp album page: the tags live in the tag block (`tralbum-tags`), the
# album title and the per-track list in `data-tralbum` (HTML-escaped JSON, the
# way Bandcamp serves it). The tag list deliberately carries the junk a real
# page carries: a punctuation-duplicate of its own slug, and a mood word.
BANDCAMP_PAGE = (
    '<html><body>'
    '<div class="tralbumData tralbum-tags tralbum-tags-nu hidden">'
    '<h3><span class="tags-inline-label">Tags</span></h3>'
    '<a class="tag" href="https://bandcamp.com/discover/doom-metal?from=tralbum"'
    '                >doom metal</a>'
    '<a class="tag" href="https://bandcamp.com/discover/doommetal?from=tralbum"'
    '                >doommetal</a>'
    '<a class="tag" href="https://bandcamp.com/discover/melancholic?from=tralbum"'
    '                >melancholic</a>'
    '<a class="tag" href="https://bandcamp.com/discover/stoner-rock?from=tralbum"'
    '                >Stoner Rock</a>'
    '</div>'
    '<script data-tralbum="{&quot;current&quot;: {&quot;title&quot;: '
    '&quot;Test Album&quot;}, &quot;trackinfo&quot;: ['
    '{&quot;track_num&quot;: 1, &quot;title&quot;: &quot;Track One&quot;}, '
    '{&quot;track_num&quot;: 2, &quot;title&quot;: &quot;Track Two&quot;}]}"'
    ' data-other="1"></script>'
    '</body></html>')


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


def stub_apple(router):
    calls = []

    def fake(path, params=None, timeout=None):
        calls.append((path, dict(params or {})))
        return router(path, dict(params or {}))

    intg._apple_json = fake
    return calls


def stub_mb(payloads):
    calls = []

    def fake(endpoint, params=None, timeout=None, retries=None):
        calls.append(endpoint)
        return payloads.get(endpoint, {})

    intg.mb_get = fake
    intg.mb_get_cached = fake
    return calls


def lb_router(rows=None, rg_rows=None, artist_rows=None):
    """ListenBrainz's three buckets of `/1/metadata/...` answers."""
    rows = LB_ROWS if rows is None else rows
    rg_rows = ([{"tag": "Progressive Rock", "count": 5, "genre_mbid": "g-3"}]
               if rg_rows is None else rg_rows)
    artist_rows = ([{"tag": "Art Rock", "count": 6, "genre_mbid": "g-4"},
                    {"tag": "Dreamy", "count": 12}]
                   if artist_rows is None else artist_rows)

    def router(url, params):
        if "metadata/recording/" in url:
            return {params.get("recording_mbids"): {"tag": {"recording": rows}}}
        if "metadata/release_group/" in url:
            return {params.get("release_group_mbids"): {"tag": {"release_group": rg_rows}}}
        if "metadata/artist/" in url:
            return {params.get("artist_mbids"): {"tag": {"artist": artist_rows}}}
        return None

    return router


def apple_router(songs):
    def router(path, params):
        if params.get("entity") == "musicArtist":
            return {"results": [{"artistName": "Test Artist", "artistId": 555}]}
        if params.get("entity") == "album":
            return {"results": [{"collectionId": 999, "artistName": "Test Artist",
                                 "collectionName": "Test Album", "trackCount": 2,
                                 "collectionExplicitness": "notExplicit"}]}
        if params.get("entity") == "song":
            return {"results": [
                {"trackName": "Track One", "discNumber": 1, "trackNumber": 1,
                 "primaryGenreName": genre} for genre in songs.get("1:1", [])
            ] + [
                {"trackName": "Track Two", "discNumber": 1, "trackNumber": 2,
                 "primaryGenreName": genre} for genre in songs.get("1:2", [])
            ]}
        return {"results": []}

    return router


def wikidata_router(labels=("Space Rock", "Art Rock"), per_qid=None):
    """Wikidata's two calls: P136 claims by entity, labels by item id.

    Every entity gets its own item ids, so `per_qid` (how the "recording
    first, release group after" test tells the two tiers apart) cannot bleed
    one entity's labels into the other's.
    """
    labels_by_qid = {"Q202996": list(labels)}
    labels_by_qid.update(per_qid or {})
    claims, item_labels = {}, {}
    for idx, (entity, names) in enumerate(labels_by_qid.items()):
        ids = [f"Q{2000 + idx * 10 + i}" for i in range(len(names))]
        item_labels.update(dict(zip(ids, names)))
        claims[entity] = ids

    def router(url, params):
        if "wikidata.org" not in url:
            return None
        if params.get("action") == "wbgetclaims":
            ids = claims.get(params.get("entity"))
            if ids is None:
                return {"claims": {}}
            return {"claims": {"P136": [
                {"mainsnak": {"datavalue": {"value": {"id": item}}}}
                for item in ids]}}
        if params.get("action") == "wbgetentities":
            wanted = [item for item in params.get("ids", "").split("|")
                      if item in item_labels]
            return {"entities": {item: {"labels": {"en": {"value": item_labels[item]}}}
                                 for item in wanted}}
        return None

    return router


def audiodb_router(tracks=None, album=None):
    """TheAudioDB's two calls: the track row (by title) and the album row."""
    tracks = AUDIODB_TRACKS if tracks is None else tracks

    def router(url, params):
        if "searchtrack.php" in url:
            row = tracks.get(params.get("t"))
            return {"track": [row] if row else []}
        if "searchalbum.php" in url:
            return {"album": [album] if album else []}
        return None

    return router


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


def both_router(*routers):
    """First router that answers — the seams here are stubbed per host."""
    def router(url, params):
        for one in routers:
            got = one(url, params)
            if got is not None:
                return got
        return None

    return router


def full_stack(**overrides):
    """Every source answering, with `overrides` replacing a router.

    RateYourMusic, ListenBrainz, MusicBrainz, iTunes, TheAudioDB (per track
    and album), Wikidata and Bandcamp all answer; Last.fm, Discogs and Spotify
    are the keyed ones and stay silent without their credentials, which is
    itself part of the contract (§5).
    """
    stub_rym({"rateyourmusic.com": overrides.get("rym", RYM_PAGE),
              "bandcamp.com/album/": overrides.get("bandcamp", BANDCAMP_PAGE)})
    stub_json(overrides.get("json")
              or both_router(lb_router(), wikidata_router(),
                             audiodb_router(tracks=overrides.get("audiodb_tracks"),
                                            album=overrides.get("audiodb_album",
                                                                AUDIODB_ALBUM))))
    stub_apple(overrides.get("apple") or apple_router({"1:1": ["Hard Rock"],
                                                       "1:2": ["Nu Metal"]}))
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

# RateYourMusic's album genres first, then ListenBrainz's per-recording tags
# (recognised genres first, then the agreed free tag) and its release-group /
# artist buckets, then MusicBrainz, then iTunes, TheAudioDB (its own per-track
# row, then the album row), Wikidata and Bandcamp.
assert got["per_track"][(1, 1)] == [
    "Heavy Metal", "Groove Metal",                    # rateyourmusic (album)
    "alternative metal", "post-metal", "sludge metal",  # listenbrainz recording
    "progressive rock", "art rock",                   # listenbrainz rg → artist
    "Rock",                                           # musicbrainz (release)
    "Hard Rock",                                      # itunes (per track)
    "Shoegaze", "Dream Pop",                          # theaudiodb (per track)
    "Post-Rock", "Epic",                              # theaudiodb (album)
    "Space Rock",                                     # wikidata (P136)
    "doom metal", "Stoner Rock",                      # bandcamp (album tags)
], got["per_track"][(1, 1)]
# The tier order inside ListenBrainz holds: the release-group bucket is ahead
# of MusicBrainz, and a later source cannot jump an earlier one.
assert got["per_track"][(1, 1)].index("progressive rock") < got["per_track"][(1, 1)].index("Rock")
# Mood words and tag-spam never become genres, at any level — the "melancholic"
# tag Bandcamp's page carries is dropped here exactly like ListenBrainz's.
for names in list(got["per_track"].values()) + list(got["per_source"].values()):
    joined = " ".join(names).lower()
    assert "melancholic" not in joined and "dreamy" not in joined, names
    assert "vyrzukhisuc" not in joined, names
    assert "doommetal" not in joined, names     # the slug duplicate of its own tag
assert "sludge metal" in got["per_track"][(1, 1)]     # count 4 free tag kept

# Provenance: the contributing sources, in ask order, per PATH — and the
# per-track map is keyed the same way the ONE writer (`_write_album_genres`)
# looks its entries up, so the route can hand it straight over. Every source
# that answered here answered per track (TheAudioDB's row is the exception,
# and it is the source's own album row, which its track row precedes).
from server import soulseek_auto

assert soulseek_auto._parse_trackno(FILE_ONE) in got["per_track"]
assert got["sources"][FILE_ONE] == ["rateyourmusic", "listenbrainz",
                                    "musicbrainz", "itunes", "theaudiodb",
                                    "wikidata", "bandcamp"], got["sources"]
assert got["per_track_sources"][(1, 1)] == got["sources"][FILE_ONE]
# `levels` reports the MOST SPECIFIC tier that answered this track — with
# ListenBrainz's recording tags and Apple's per-track genre in the list, that
# is the track level (an album-only answer is "album", see §3).
assert got["levels"][FILE_ONE] == "track", got["levels"]

# Track 2 states its own genre on the RYM page, so that row answers it — and
# the track level is what the provenance reports.
assert got["per_track"][(1, 2)][0] == "Industrial Metal", got["per_track"][(1, 2)]
assert got["levels"][FILE_TWO] == "track", got["levels"]
assert got["sources"][FILE_TWO][0] == "rateyourmusic", got["sources"]
# TheAudioDB had no row for this track, so its ALBUM row answered it instead —
# and it is labelled as such in the provenance, not as a track answer.
assert "Post-Rock" in got["per_track"][(1, 2)], got["per_track"][(1, 2)]

# The chain follows `genre_sources`: reversing the order reverses the merge.
clear()
full_stack()
reversed_got = intg.genre_chain(artist="Test Artist", album="Test Album",
                                release=RELEASE, cfg=CFG, limit=6,
                                sources=list(reversed(ORDER)),
                                files=[FILE_ONE])
assert reversed_got["per_track"][(1, 1)] == ["Space Rock", "Art Rock",
                                             "Hard Rock", "Alternative Metal",
                                             "Rock", "Progressive Rock"], \
    reversed_got["per_track"][(1, 1)]
assert reversed_got["sources"][FILE_ONE] == ["wikidata", "itunes", "musicbrainz"], \
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

# iTunes answers per TRACK (Apple's `primaryGenreName`), so its genre lands on
# the track it belongs to and nowhere else.
clear()
stub_json(lb_router(rows=[], rg_rows=[], artist_rows=[]))
stub_rym({"rateyourmusic.com": RYM_CHALLENGE})
stub_apple(apple_router({"1:2": ["Nu Metal"]}))
stub_mb({})
got = chain(limit=6)
assert got["per_track"] == {(1, 1): ["Alternative Metal", "Rock"],
                            (1, 2): ["Nu Metal", "Rock"]}, got["per_track"]
assert got["per_source"]["itunes"] == ["Nu Metal"], got["per_source"]
assert "Nu Metal" not in got["per_track"][(1, 1)], got["per_track"]
assert got["levels"][FILE_TWO] == "track", got["levels"]
assert got["levels"][FILE_ONE] == "track", got["levels"]   # MB recording genres
# A RYM that was ASKED and refused says so — the reason names the setting to
# fix, instead of an empty answer that looks like "nothing there".
assert got["notes"]["rateyourmusic"].startswith(
    "skipped: RateYourMusic refused this cookie"), got["notes"]

# --------------------------------------------------------------------------- #
# 2) Reliability — one source failing never removes the others'
# --------------------------------------------------------------------------- #
# (a) blocked RYM, everything else answering: one log line, no exception.
clear()
stub_rym({"rateyourmusic.com": RYM_CHALLENGE})
stub_json(lb_router())
stub_apple(apple_router({"1:1": ["Hard Rock"]}))
stub_mb({"release-group/rg-1": {"genres": [{"name": "Progressive Rock"}]}})
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    got = chain(limit=6)
assert got["notes"]["rateyourmusic"].startswith(
    "skipped: RateYourMusic refused this cookie"), got["notes"]
assert got["per_track"][(1, 1)][0] == "alternative metal", got["per_track"]
assert "rateyourmusic" not in got["sources"][FILE_ONE], got["sources"]
assert buf.getvalue().count("rateyourmusic") == 1, buf.getvalue()

# (b) a host that raises (timeout / no route): the others still answer.
clear()
stub_rym({"rateyourmusic.com": RYM_PAGE})

def dead_lb(url, params):
    if "listenbrainz" in url:
        raise RuntimeError("connection reset")
    return None

stub_json(dead_lb)
stub_apple(apple_router({"1:1": ["Hard Rock"]}))
stub_mb({})
got = chain(limit=6)
assert got["notes"]["listenbrainz"].startswith(("no data", "failed:")), got["notes"]
assert "listenbrainz" not in got["per_source"], got["per_source"]
assert got["per_track"][(1, 1)][:2] == ["Heavy Metal", "Groove Metal"], got["per_track"]
# The dead host is simply absent from the provenance; the others are intact.
assert got["sources"][FILE_ONE] == ["rateyourmusic", "musicbrainz", "itunes"], got["sources"]

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
assert got["per_track"][(1, 1)][0] == "alternative metal", got["per_track"]

# --------------------------------------------------------------------------- #
# 3) RateYourMusic — per-track rows, title mapping, album fallback
# --------------------------------------------------------------------------- #
# A page with a track list but no genres at all: nothing is mapped by position,
# so the descriptors carry the album-level answer.
clear()
stub_rym({"rateyourmusic.com": RYM_DESCRIPTOR_PAGE})
stub_json(lb_router(rows=[], rg_rows=[], artist_rows=[]))
stub_apple(apple_router({}))
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
stub_json(lb_router(rows=[], rg_rows=[], artist_rows=[]))
stub_apple(apple_router({}))
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
stub_json(lb_router(rows=[], rg_rows=[], artist_rows=[]))
stub_apple(apple_router({}))
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
# 4) Wikidata — the release group's own relation, QIDs resolved to labels
# --------------------------------------------------------------------------- #
clear()
calls = stub_json(wikidata_router())
stub_rym({"rateyourmusic.com": RYM_CHALLENGE})
stub_apple(apple_router({}))
mb_calls = stub_mb({"release-group/rg-1": {
    "relations": [{"type": "wikidata",
                   "url": {"resource": "https://www.wikidata.org/wiki/Q202996"}}]}})
got = chain(release=BARE_RELEASE)
assert got["per_source"]["wikidata"] == ["Space Rock", "Art Rock"], got["per_source"]
assert "release-group/rg-1" in mb_calls, mb_calls
claims = [p for url, p in calls if p.get("action") == "wbgetclaims"]
assert claims and claims[0]["entity"] == "Q202996", claims
assert got["levels"] == {FILE_ONE: "album", FILE_TWO: "album"}, got["levels"]
# No P136 statement → no Wikidata genre, and no label guessing.
clear()
stub_json(lambda url, params: {} if "wikidata" in url else None)
stub_rym({"rateyourmusic.com": RYM_CHALLENGE})
stub_apple(apple_router({}))
stub_mb({})
got = chain()
assert "wikidata" not in got["per_source"] and got["notes"]["wikidata"] == "no data", got

# --------------------------------------------------------------------------- #
# 5) Last.fm / Discogs — keyed sources are skipped, never guessed
# --------------------------------------------------------------------------- #
clear()
calls = stub_json(lb_router(rows=[], rg_rows=[], artist_rows=[]))
stub_rym({"rateyourmusic.com": RYM_PAGE})
stub_apple(apple_router({}))
stub_mb({})
got = chain(release=BARE_RELEASE)
hosts = {url.split("/")[2] for url, _p in calls}
assert "ws.audioscrobbler.com" not in hosts and "api.discogs.com" not in hosts, hosts
# RYM answered both tracks here, so the chain stopped before either of them.
assert got["per_track"][(1, 1)] == ["Heavy Metal", "Groove Metal"], got["per_track"]
assert "lastfm" not in got["asked"] and "discogs" not in got["asked"], got["asked"]

# A source without its key is skipped BEFORE any request, and the report names
# the setting instead of reporting "no data" for something never asked.
clear()
calls = stub_json(lambda url, params: None)
stub_rym({"rateyourmusic.com": RYM_CHALLENGE})
stub_apple(apple_router({}))
stub_mb({})
got = intg.genre_chain(artist="Test Artist", album="Test Album",
                       release=BARE_RELEASE, cfg=CFG, limit=6,
                       files=[FILE_ONE, FILE_TWO],
                       sources=["lastfm", "discogs", "deezer"])
assert got["notes"]["lastfm"].startswith("skipped: no lastfm_api_key"), got["notes"]
assert got["notes"]["discogs"].startswith("skipped: no discogs_token"), got["notes"]
assert set(got["skipped"]) == {"lastfm", "discogs"}, got["skipped"]
assert got["asked"] == ["deezer"], got["asked"]
hosts = {url.split("/")[2] for url, _p in calls}
assert "ws.audioscrobbler.com" not in hosts and "api.discogs.com" not in hosts, hosts

# RateYourMusic is gated the same way, on its credential: no `rym_cookie` means
# no page (RYM refuses an automated client), so the chain does not spend a
# request and a second of throttle learning that.
clear()
fake = stub_rym({"rateyourmusic.com": RYM_PAGE})
stub_json(lambda url, params: None)
stub_apple(apple_router({}))
stub_mb({})
got = intg.genre_chain(artist="Test Artist", album="Test Album",
                       release=BARE_RELEASE, cfg={"mb_genre_count": 3},
                       limit=6, files=[FILE_ONE, FILE_TWO],
                       sources=["rateyourmusic", "musicbrainz"])
assert got["notes"]["rateyourmusic"].startswith("skipped: no rym_cookie"), got["notes"]
assert "rateyourmusic" not in got["asked"], got["asked"]
assert fake.calls == [], fake.calls          # not one request to RYM

# With the keys set, Last.fm answers per TRACK (then artist) and Discogs
# answers album-level.
clear()
keyed = dict(CFG, lastfm_api_key="lf-key", discogs_token="dc-token")

def keyed_router(url, params):
    if "audioscrobbler" in url and params.get("method") == "track.getTopTags":
        return {"toptags": {"tag": [{"name": "Sludge Metal"}]}}
    if "audioscrobbler" in url and params.get("method") == "artist.getTopTags":
        return {"toptags": {"tag": [{"name": "Art Rock"}]}}
    if "discogs.com/releases" in url:
        return {"genres": ["Rock"], "styles": ["Stoner Rock"]}
    if "discogs.com/database" in url:
        return {"results": [{"id": 42}]}
    return None

calls = stub_json(keyed_router)
stub_rym({"rateyourmusic.com": RYM_CHALLENGE})
stub_apple(apple_router({}))
stub_mb({})
got = intg.genre_chain(artist="Test Artist", album="Test Album", release=RELEASE,
                       cfg=keyed, limit=6, files=[FILE_ONE, FILE_TWO])
# MusicBrainz's recording genre, then the release's, then Last.fm's per-track
# tag, its artist tags, and finally Discogs' styles.
assert got["per_track"][(1, 1)] == ["Alternative Metal", "Rock", "Sludge Metal",
                                    "Art Rock", "Stoner Rock"], got["per_track"]
assert got["per_track"][(1, 2)] == ["Nu Metal", "Rock", "Sludge Metal",
                                    "Art Rock", "Stoner Rock"], got["per_track"]
assert got["levels"][FILE_ONE] == "track" and got["levels"][FILE_TWO] == "track"
assert got["per_source"]["discogs"] == ["Rock", "Stoner Rock"], got["per_source"]
methods = [p.get("method") for _u, p in calls if p.get("method")]
assert methods.count("track.getTopTags") == 2, methods   # one call per track

# --------------------------------------------------------------------------- #
# 6) Nothing invented when every source is empty
# --------------------------------------------------------------------------- #
clear()
stub_json(lambda url, params: None)
stub_rym({}, boom=True)
stub_apple(apple_router({}))
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
assert route["answers"] == {} and route["value"] == 0, route

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

# Discogs' Parental Advisory format is album-level, token-only and WEAK: it
# can only add an explicit signal (never clear a track), and without a token
# it is not even asked.
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
assert "discogs-parental" in route["checked"], route
assert route["answers"] == {"discogs-parental": 1}, route
assert route["value"] == 1 and route["source"] == "discogs-parental", route

intg._ADVISORY_CACHE.clear()
route = intg.resolve_advisory_route(title="Track One", artist="Test Artist",
                                    album="Test Album", cfg={})
assert "discogs-parental" not in route["checked"], route
assert route["answers"] == {}, route

# A release without the sticker states nothing at all — it never says "clean".
intg._ADVISORY_CACHE.clear()
stub_json(discogs_router([{"name": "CD", "descriptions": ["Album"]}]))
route = intg.resolve_advisory_route(title="Track One", artist="Test Artist",
                                    album="Test Album", cfg={"discogs_token": "t"})
assert "discogs-parental" in route["checked"], route
assert route["answers"] == {} and route["value"] == 0, route

# --------------------------------------------------------------------------- #
# 8) The 30-day cache answers the second run instead of the network
# --------------------------------------------------------------------------- #
clear()
import tempfile

folder = tempfile.mkdtemp(prefix="mlo-genre-cache-")
intg._data_cache_dir = lambda name: os.path.join(folder, name) if name else None
try:
    rym = stub_rym({"rateyourmusic.com": RYM_PAGE})
    calls = stub_json(both_router(lb_router(), wikidata_router()))
    stub_apple(apple_router({"1:1": ["Hard Rock"], "1:2": ["Nu Metal"]}))
    stub_mb(MB_DEFAULT)
    first = chain(limit=6)
    assert first["per_track"][(1, 1)][0] == "Heavy Metal", first["per_track"]
    rym_calls, json_calls = len(rym.calls), len(calls)
    assert rym_calls and json_calls, (rym_calls, json_calls)

    # Forget the in-process memos only: what answers now is the DISK cache.
    intg._GENRE_CACHE.clear()
    discovery._CACHE.clear()
    again = chain(limit=6)
    assert again["per_track"] == first["per_track"], again["per_track"]
    assert again["sources"] == first["sources"], again["sources"]
    assert len(rym.calls) == rym_calls, rym.calls[rym_calls:]
    assert len(calls) == json_calls, calls[json_calls:]
finally:
    intg._data_cache_dir = lambda name: None

# --------------------------------------------------------------------------- #
# 9) The default priority list is the documented one, both old defaults migrate
# --------------------------------------------------------------------------- #
assert list(intg.GENRE_SOURCES) == DOCUMENTED_SOURCES, intg.GENRE_SOURCES
# The provider REGISTRY stays the full priority list — every source is still
# selectable in Settings → Discovery and listed in Sources health. The SHIPPED
# default asks only the two the app grades genres from.
SHIPPED_DEFAULT = ["rateyourmusic", "musicbrainz"]
assert list(mcfg.DEFAULT_CONFIG["genre_sources"]) == SHIPPED_DEFAULT, \
    mcfg.DEFAULT_CONFIG["genre_sources"]
# Every per-track source sits above every album-only one, so a track's own
# answer can never be outranked by an album-wide guess on a default install.
assert (max(intg.GENRE_SOURCES.index(s) for s in PER_TRACK_SOURCES)
        < min(intg.GENRE_SOURCES.index(s) for s in WIDE_SOURCES)), intg.GENRE_SOURCES
# The two chains earlier releases shipped are not a choice the user made: both
# are swapped for the current default. A list they actually edited is theirs.
for legacy in list(mcfg.LEGACY_DEFAULT_GENRE_SOURCES) + [mcfg.LEGACY_GENRE_SOURCES]:
    assert list(mcfg.normalize_config(
        {"genre_sources": list(legacy)})["genre_sources"]) == SHIPPED_DEFAULT, legacy
custom = ["musicbrainz", "itunes"]
assert mcfg.normalize_config({"genre_sources": list(custom)})["genre_sources"] == custom
# No saved list at all is the same "no choice" case.
assert list(mcfg.normalize_config({})["genre_sources"]) == SHIPPED_DEFAULT
# And a saved list is what the chain runs — the config default is not forced
# over a hand-picked order.
clear()
full_stack()
picked = intg.genre_chain(artist="Test Artist", album="Test Album",
                          release=RELEASE, cfg={"mb_genre_count": 3,
                                                "genre_sources": ["itunes", "musicbrainz"]},
                          limit=6, files=[FILE_ONE])
assert picked["sources"][FILE_ONE] == ["itunes", "musicbrainz"], picked["sources"]

# --------------------------------------------------------------------------- #
# 10) TheAudioDB — its own track row first, its album row as the fallback
# --------------------------------------------------------------------------- #
clear()
calls = stub_json(both_router(lb_router(rows=[], rg_rows=[], artist_rows=[]),
                              audiodb_router(album=AUDIODB_ALBUM)))
stub_rym({"rateyourmusic.com": RYM_CHALLENGE})
stub_apple(apple_router({}))
stub_mb({})
got = chain(limit=8, release=BARE_RELEASE)
# Track One has a track row: its genre+style, then the album row behind it.
assert got["per_track"][(1, 1)] == ["Shoegaze", "Dream Pop", "Post-Rock", "Epic"], \
    got["per_track"]
# Track Two has none, so the album row answers it — and only it.
assert got["per_track"][(1, 2)] == ["Post-Rock", "Epic"], got["per_track"]
assert got["levels"] == {FILE_ONE: "track", FILE_TWO: "album"}, got["levels"]
# Every track was asked about per track (one lookup per title), once each, and
# the album call is one per album.
assert [p.get("t") for url, p in calls if "searchtrack.php" in url] == \
    ["Track One", "Track Two"], calls
assert len([1 for url, _p in calls if "searchalbum.php" in url]) == 1, calls
assert [p.get("s") for url, p in calls if "searchtrack.php" in url] == \
    ["Test Artist", "Test Artist"], calls
# TheAudioDB's mood field is a mood, not a genre.
assert "Dreamy" not in got["per_source"]["theaudiodb"], got["per_source"]
# `per_source` is the album summary: the release-wide row first, then the
# per-track ones (the same order `genre_chain` merges a track in).
assert got["per_source"]["theaudiodb"] == ["Post-Rock", "Epic", "Shoegaze",
                                           "Dream Pop"], got["per_source"]
# A row it holds for ANOTHER artist is that artist's answer, never ours.
assert discovery.audiodb_track("Test Artist", "Somebody Else's Song") is None
stub_json(audiodb_router(tracks={"Creep": {"strArtist": "Weezer",
                                           "strGenre": "Power Pop"}}))
assert discovery.audiodb_track("Test Artist", "Creep") is None
assert discovery.audiodb_track("Weezer", "Creep")["genre"] == "Power Pop"

# --------------------------------------------------------------------------- #
# 11) Wikidata — the recording's own item first, then its WORK, then the
#     release group
# --------------------------------------------------------------------------- #
clear()
mb_calls = stub_mb({
    # Track One's recording links Wikidata directly; Track Two's does not, but
    # it performs a WORK that does (the practical route — see _mb_recording_ids).
    "recording/rec-1": {"relations": [
        {"type": "wikidata",
         "url": {"resource": "https://www.wikidata.org/wiki/Q777"}}]},
    "recording/rec-2": {"relations": [
        {"type": "performance", "work": {"id": "work-2"}}]},
    "work/work-2": {"relations": [
        {"type": "wikidata",
         "url": {"resource": "https://www.wikidata.org/wiki/Q888"}}]},
    "release-group/rg-1": {"relations": [
        {"type": "wikidata",
         "url": {"resource": "https://www.wikidata.org/wiki/Q202996"}}]},
})
calls = stub_json(wikidata_router(per_qid={"Q777": ["Shoegaze"],
                                           "Q888": ["Space Rock"]}))
stub_rym({"rateyourmusic.com": RYM_CHALLENGE})
stub_apple(apple_router({}))
got = chain(limit=8, release=BARE_RELEASE)
assert "recording/rec-1" in mb_calls and "work/work-2" in mb_calls, mb_calls
# Both tracks answered per TRACK — one from its recording item, the other from
# its work's item — and the release group's answer (Space Rock / Art Rock, with
# the album's own "Space Rock" already spent) is the fallback behind them.
assert got["per_track"][(1, 1)] == ["Shoegaze", "Space Rock", "Art Rock"], got["per_track"]
assert got["per_track"][(1, 2)] == ["Space Rock", "Art Rock"], got["per_track"]
assert got["levels"] == {FILE_ONE: "track", FILE_TWO: "track"}, got["levels"]
assert [p.get("entity") for url, p in calls
        if p.get("action") == "wbgetclaims"] == ["Q777", "Q888", "Q202996"], calls

# --------------------------------------------------------------------------- #
# 12) Bandcamp — album tags parsed, junk filtered, page verified, cached
# --------------------------------------------------------------------------- #
clear()
fake = stub_rym({"rateyourmusic.com": RYM_CHALLENGE,
                 "bandcamp.com/album/": BANDCAMP_PAGE})
stub_json(both_router(lb_router(rows=[], rg_rows=[], artist_rows=[])))
stub_apple(apple_router({}))
stub_mb({})
got = chain(limit=8, release=BARE_RELEASE)
# Bandcamp states no per-track genre at all: its tags are the album's, and the
# provenance says album — never track.
assert got["per_source"]["bandcamp"] == ["doom metal", "Stoner Rock"], got["per_source"]
assert got["per_track"][(1, 1)] == ["doom metal", "Stoner Rock"], got["per_track"]
assert got["levels"] == {FILE_ONE: "album", FILE_TWO: "album"}, got["levels"]
# One request per album; the second run is answered from the cache.
pages = [u for u in fake.calls if "bandcamp.com/album/" in u]
assert len(pages) == 1, fake.calls
got = chain(limit=8, release=BARE_RELEASE)
assert len([u for u in fake.calls if "bandcamp.com/album/" in u]) == 1, fake.calls
assert got["per_source"]["bandcamp"] == ["doom metal", "Stoner Rock"], got
# The parser itself: the slug duplicate ("doommetal") and the mood word are
# dropped, and the page's own track list is read from `data-tralbum`.
page = intg.bandcamp_album("Test Artist", "Test Album", ["Track One"])
assert page["genres"] == ["doom metal", "Stoner Rock"], page
assert [t["title"] for t in page["tracks"]] == ["Track One", "Track Two"], page
# A page for a DIFFERENT album, or a page whose tracks are not ours, is NO
# answer — never a same-named record's tags.
assert intg.bandcamp_album("Test Artist", "Other Album", ["Track One"]) is None
assert intg.bandcamp_album("Test Artist", "Test Album", ["Something Else"]) is None
# The slug candidates are the artist's subdomain with and without separators.
assert intg._bandcamp_subdomain_candidates("God Is An Astronaut") == \
    ["godisanastronaut", "god-is-an-astronaut"]

# --------------------------------------------------------------------------- #
# 13) Spotify — artist level, credentials only, and last
# --------------------------------------------------------------------------- #
clear()
calls = stub_json(both_router(
    lb_router(rows=[], rg_rows=[], artist_rows=[]),
    lambda url, params: ({"artists": {"items": [{"name": "Test Artist",
                                                 "genres": ["Alternative Rock"]}]}}
                         if "spotify" in url else None)))
stub_rym({"rateyourmusic.com": RYM_CHALLENGE})
stub_apple(apple_router({}))
stub_mb({})
_real_token = intg._spotify_token
try:
    # The credential gate is the config, not the token: without them Spotify is
    # not even asked (the patched token proves the gate is what stopped it),
    # and the report names the two settings rather than saying "no data".
    intg._spotify_token = lambda cfg=None, timeout=None: "tok"
    got = chain(limit=8, release=BARE_RELEASE)
    assert got["notes"]["spotify"].startswith(
        "skipped: no spotify_client_id/spotify_client_secret"), got["notes"]
    assert "spotify" not in got["asked"], got["asked"]
    assert not [u for u, _p in calls if "spotify" in u], calls
    # With credentials it answers at the ARTIST tier, honestly labelled.
    keyed = dict(CFG, spotify_client_id="id", spotify_client_secret="secret")
    got = intg.genre_chain(artist="Test Artist", album="Test Album",
                           release=BARE_RELEASE, cfg=keyed, limit=8,
                           files=[FILE_ONE, FILE_TWO])
    assert got["per_source"]["spotify"] == ["Alternative Rock"], got["per_source"]
    assert got["per_track"][(1, 1)] == ["Alternative Rock"], got["per_track"]
    assert got["levels"] == {FILE_ONE: "artist", FILE_TWO: "artist"}, got["levels"]
    assert len([u for u, _p in calls if "spotify" in u]) == 1, calls
    # A search hit whose own artist is somebody else is NO answer.
    intg._GENRE_CACHE.clear()
    calls = stub_json(lambda url, params: (
        {"artists": {"items": [{"name": "Some Other Band", "genres": ["Pop"]}]}}
        if "spotify" in url else None))
    got = intg.genre_chain(artist="Test Artist", album="Test Album",
                           release=BARE_RELEASE, cfg=keyed, limit=8,
                           files=[FILE_ONE, FILE_TWO])
    assert "spotify" not in got["per_source"], got["per_source"]
    assert got["notes"]["spotify"] == "no data", got["notes"]
finally:
    intg._spotify_token = _real_token

# --------------------------------------------------------------------------- #
# 14) A failing NEW source loses only itself
# --------------------------------------------------------------------------- #
clear()
full_stack()
_real_bandcamp, _real_track = intg.bandcamp_album, discovery.audiodb_track
intg.bandcamp_album = lambda *a, **k: 1 / 0
discovery.audiodb_track = lambda *a, **k: 1 / 0
try:
    got = chain(limit=20)
finally:
    intg.bandcamp_album = _real_bandcamp
    discovery.audiodb_track = _real_track
assert got["notes"]["bandcamp"].startswith("failed: "), got["notes"]
assert got["notes"]["theaudiodb"].startswith("failed: "), got["notes"]
assert "bandcamp" not in got["per_source"], got["per_source"]
assert "theaudiodb" not in got["per_source"], got["per_source"]
# Everything else answered, in order, exactly as it did before the failure.
assert got["sources"][FILE_ONE] == ["rateyourmusic", "listenbrainz",
                                    "musicbrainz", "itunes", "wikidata"], got["sources"]
assert got["per_track"][(1, 1)][0] == "Heavy Metal", got["per_track"]
assert "Space Rock" in got["per_track"][(1, 1)], got["per_track"]

# --------------------------------------------------------------------------- #
# 15) The route surface — the background job, and the inline call it replaces
# --------------------------------------------------------------------------- #
# The chain is minutes of provider traffic, so the wizard starts it as a job
# and polls. Everything below runs the REAL routes against a temp music folder
# with the chain itself stubbed: what is pinned here is the job's state machine
# (`running` → `done`/`error`), the 409 guard, and the verbatim error text — the
# chain's own answers are covered above.
import shutil as _shutil
import tempfile as _tempfile
import threading as _threading
import time as _time

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

JOB_SOURCES = ("rateyourmusic", "listenbrainz", "musicbrainz")
JOB_NOTES = {"rateyourmusic": "no data", "musicbrainz": "answered"}
# The chain's report, in the shape `_run_genre_chain` passes through: per-source
# counts, what was actually asked and where it stopped, what was skipped and
# why, which tier answered, and the cap's own trail.
JOB_RESULT = {"updated": 2, "genres": ["Shoegaze"],
              "per_source": {"musicbrainz": ["Shoegaze"]}, "notes": JOB_NOTES,
              "per_track": {(1, 1): ["Shoegaze"]}, "sources": {}, "levels": {},
              "per_source_counts": {"musicbrainz": {"names": 1, "tracks": 2}},
              "asked": ["musicbrainz"], "stopped_after": "musicbrainz",
              "order": ["rateyourmusic", "listenbrainz", "musicbrainz"],
              "skipped": {"rateyourmusic": "skipped: no rym_cookie"},
              "level_counts": {"track": 1, "album": 1, "artist": 0},
              "per_track_trimmed": {}, "trimmed": []}
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

print("genres: all assertions passed")
