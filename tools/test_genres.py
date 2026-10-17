#!/usr/bin/env python3
"""Per-track genre import (server.integrations.genre_chain) — offline contract.

What this pins, with every HTTP seam stubbed (no network at all):

  * the DEFAULT priority list is an explicit, documented promise: MusicBrainz
    alone, equal to `mlo.config.DEFAULT_CONFIG["genre_sources"]` — the only
    genre source this build knows (2026-10-12; the scraped sites are gone).
    MusicBrainz answers per RECORDING, so a release whose recordings state
    their own genres is settled by one source and nothing else is asked;
  * the sources are asked IN ORDER until every track is FULL: the writer's own
    policy decides that (`_genre_complete` — `limit` SPECIFIC genres), and a
    TRACK-level answer (the merge's `level == "track"`) settles a track by
    itself, so MusicBrainz's per-recording genres end the search; an
    album-level answer does not, and a source below it is still asked;
  * MusicBrainz answers ONE TIER per track (2026-10-12, the owner's rule):
    the RECORDING's own genres first — the entity the track IS — else the
    release GROUP's, else the ARTIST's, and the first tier that states anything
    IS the list. The release's own (edition) genres are never read: the album
    is its group, not one pressing. The record is labelled with the tier that
    answered (`level: album` for the group, `level: artist` only when the group
    stated nothing), never promoted to a track (`level_counts` totals those);
  * a track's own RECORDING genres settle it by themselves (a one-name answer
    is complete, R37), so the work is read only when the recording is silent
    and the release-wide tiers are never asked for it;
  * the cap (`mb_genre_count`) is enforced PER TRACK, and a track's own
    recording genres stay ahead of the release-wide ones;
  * a source that raises inside the module (an adapter that blew up) is
    reported as failed, with the traceback message, and contributes nothing —
    never a guess;
  * an id this build no longer knows (a stale saved list entry) returns no
    answer at all — "no data", never an album-wide guess;
  * nothing is invented: every source empty yields no genres, no per-track
    map, no provenance and no exception;
  * the 30-day disk cache answers the second run instead of the network;
  * both earlier shipped default `genre_sources` lists migrate to the current
    default; a customised list is kept as written, and is what the chain runs;
  * `sources` names the contributing sources per path IN ORDER, and `levels`
    says which tier answered;
  * the two advisory extras stay in their last place and out of the way: a
    Discogs Parental Advisory format (token only) and YouTube's 18+ gate for
    a video the track's own tags record — never a blind per-track search;
  * the route surface: `/api/genres/import` passes `sources` through, renders
    the chain's (disc, position) keys as "disc:position", and names a folder
    with no audio and a path outside the music folder in the server's own
    words.

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
# stubs — and no host throttling: the transport is stubbed here.
intg._data_cache_dir = lambda name: None
discovery._HOST_WAIT.clear()

# The priority list the module documents and ships (see the GENRE_SOURCES
# comment in server/integrations.py), spelled out here so a silent reorder
# fails the run instead of passing unnoticed.
DOCUMENTED_SOURCES = ["musicbrainz"]

# The default configuration (no saved `genre_sources`): the chain must use the
# module default. Every test below therefore runs the REAL priority list.
CFG = {"mb_genre_count": 3}

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

# The same release with MusicBrainz's per-RECORDING genres removed but its
# release-wide tier (release group → release → artist) intact: MusicBrainz
# then answers only at ALBUM level, so it does NOT settle a track and the
# chain keeps walking past it.
MB_ALBUM_RELEASE = dict(RELEASE, media=[
    {"disc": 1, "position": 1, "title": "Track One", "recording_mbid": "rec-1",
     "genres": []},
    {"disc": 1, "position": 2, "title": "Track Two", "recording_mbid": "rec-2",
     "genres": []},
])

# The release with NO MusicBrainz genres of its own at all.
BARE_RELEASE = dict(MB_ALBUM_RELEASE, genres=[])


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #
def stub_json(router):
    """Replace discovery's throttled JSON transport. Returns the call log."""
    calls = []

    def fake(url, params=None, headers=None, timeout=None, ttl=None, host=None):
        calls.append((url, dict(params or {})))
        return router(url, dict(params or {}))

    discovery._json = fake
    return calls


def stub_mb(payloads, log=None):
    """MusicBrainz's cached access seam, answering per endpoint."""
    calls = log if log is not None else []

    def fake(endpoint, params=None, timeout=None, retries=None):
        calls.append(endpoint)
        return payloads.get(endpoint, {})

    intg.mb_get = fake
    intg.mb_get_cached = fake
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
    intg._bandcamp_warned = False


def full_stack(**overrides):
    """MusicBrainz answering, with `overrides` replacing its payloads.

    Returns the transport's call log, so a test can assert what was NOT asked
    (a tier that answered is never walked past)."""
    stub_json(lambda url, params: None)
    return stub_mb(overrides.get("mb") or MB_DEFAULT)


def chain(limit=None, release=None, **kwargs):
    return intg.genre_chain(artist="Test Artist", album="Test Album",
                            release=release or RELEASE, cfg=CFG, limit=limit,
                            files=[FILE_ONE, FILE_TWO], **kwargs)


# --------------------------------------------------------------------------- #
# 1) The track-level answer settles the release; provenance and the cap
# --------------------------------------------------------------------------- #
clear()
mb_calls = full_stack()
# MusicBrainz answers every track at TRACK level (its own recording genres),
# so the ONE source settles both tracks and the search stops there.
got = chain()
assert got["asked"] == ["musicbrainz"], got["asked"]
assert got["stopped_after"] == "musicbrainz", got["stopped_after"]
# ONE TIER ANSWERS A TRACK: the recording's own genres ARE the list — the
# release group's and the artist's names are fallbacks for a track the
# recording says nothing about, never a top-up beside its own.
assert got["per_track"][(1, 1)] == ["Alternative Metal"], got["per_track"]
assert got["per_track"][(1, 2)] == ["Nu Metal"], got["per_track"]
# The recording stated genres, so neither its WORK nor the release-wide tiers
# were asked at all: the answered track cost the release-group read and
# nothing else.
assert "recording/rec-1" not in mb_calls, mb_calls
assert "artist/art-1" not in mb_calls, mb_calls
# The release-wide summary is the union (the release group's row first, then
# every track's), capped at `limit`.
assert got["genres"] == ["Progressive Rock", "Alternative Metal", "Nu Metal"], \
    got["genres"]

# Provenance: the contributing sources, in ask order, per PATH — and the
# per-track map is keyed the same way the ONE writer (`_write_album_genres`)
# looks its entries up, so the route can hand it straight over.
from server import imports

assert imports._parse_trackno(FILE_ONE) in got["per_track"]
assert got["sources"][FILE_ONE] == ["musicbrainz"], got["sources"]
assert got["per_track_sources"][(1, 1)] == got["sources"][FILE_ONE]
# `levels` reports the MOST SPECIFIC tier that answered this track: a
# per-recording answer is `track`, and that is what ended the search.
assert got["levels"][FILE_ONE] == "track", got["levels"]
assert got["levels"][FILE_TWO] == "track", got["levels"]
assert got["level_counts"] == {"track": 2, "album": 0, "artist": 0}, \
    got["level_counts"]
# …and the report says how much the source said (its release-wide row answers
# both tracks, its two per-track rows once each) plus what the cap left out.
assert got["per_source_counts"]["musicbrainz"] == {"names": 3, "tracks": 4}, \
    got["per_source_counts"]
# Nothing was trimmed: each track's tier answered alone, and the summary's
# three names are exactly the cap.
assert got["per_track_trimmed"] == {}, got["per_track_trimmed"]

# The cap is enforced PER TRACK: with one slot, each track keeps its OWN
# recording genre, never the release-wide names.
clear()
full_stack()
assert chain(limit=1)["per_track"] == {
    (1, 1): ["Alternative Metal"], (1, 2): ["Nu Metal"],
}, chain(limit=1)["per_track"]

# --------------------------------------------------------------------------- #
# 2) An ALBUM-level answer does not settle a track — and it IS the release
#    GROUP's answer: the release's own (edition) genres are never read, and the
#    artist's rung is not asked while the group states anything.
# --------------------------------------------------------------------------- #
# With the per-recording genres removed, MusicBrainz answers album-wide only:
# every track carries those names, the tier is reported as `album`, and the
# search continues (it just runs out of sources here).
clear()
mb_calls = full_stack()
got = chain(limit=6, release=MB_ALBUM_RELEASE)
# The release group's names, and ONLY them: RELEASE["genres"] = ["Rock"] (the
# edition's own vote) is not part of the answer, and neither is the artist's.
assert got["per_track"][(1, 1)] == ["Progressive Rock"], got["per_track"]
assert got["per_track"][(1, 2)] == ["Progressive Rock"], got["per_track"]
assert got["levels"] == {FILE_ONE: "album", FILE_TWO: "album"}, got["levels"]
assert got["level_counts"] == {"track": 0, "album": 2, "artist": 0}, \
    got["level_counts"]
assert "artist/art-1" not in mb_calls, mb_calls
assert got["genres"] == ["Progressive Rock"], got["genres"]
# No source answered at TRACK level, so nothing set `stopped_after`: the run
# ended because it ran out of sources.
assert got["stopped_after"] is None, got["stopped_after"]
assert got["sources"][FILE_ONE] == ["musicbrainz"], got["sources"]

# The group states NOTHING: the ARTIST answers, and the release's own genres
# still do not.
clear()
mb_calls = full_stack(mb={"release-group/rg-1": {},
                          "artist/art-1": {"genres": [{"name": "Art Rock"}]}})
got = chain(limit=6, release=MB_ALBUM_RELEASE)
assert got["per_track"][(1, 1)] == ["Art Rock"], got["per_track"]
assert got["per_track"][(1, 2)] == ["Art Rock"], got["per_track"]
assert got["levels"] == {FILE_ONE: "artist", FILE_TWO: "artist"}, got["levels"]
assert got["level_counts"] == {"track": 0, "album": 0, "artist": 2}, \
    got["level_counts"]

# Nothing above states anything: the EDITION's own genre is not a fallback at
# all — "Rock" sits on the release entity, and the answer stays empty.
clear()
mb_calls = full_stack(mb={"release-group/rg-1": {}, "artist/art-1": {}})
got = chain(limit=6, release=MB_ALBUM_RELEASE)
assert got["per_track"] == {}, got["per_track"]
assert got["genres"] == [], got["genres"]
assert got["notes"] == {"musicbrainz": "no data"}, got["notes"]

# --------------------------------------------------------------------------- #
# 3) Reliability — a raising source removes only itself
# --------------------------------------------------------------------------- #
# An adapter that raises inside the module is reported as failed, with its own
# message, and contributes nothing.
clear()
full_stack()
_real_sources = intg._genre_source_answers


def _boom(*args, **kwargs):
    raise RuntimeError("connection reset")


intg._genre_source_answers = _boom
try:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = chain(release=MB_ALBUM_RELEASE)
finally:
    intg._genre_source_answers = _real_sources
assert got["notes"]["musicbrainz"] == "failed: connection reset", got["notes"]
assert got["per_track"] == {} and got["per_source"] == {}, got
assert got["sources"] == {FILE_ONE: [], FILE_TWO: []}, got["sources"]

# An id this build no longer knows (a stale saved list entry) is asked, gets
# nothing, and is reported as "no data" — never an album-wide guess.
clear()
full_stack()
got = intg.genre_chain(artist="Test Artist", album="Test Album",
                       release=MB_ALBUM_RELEASE, cfg=CFG, limit=6,
                       files=[FILE_ONE, FILE_TWO],
                       sources=["listenbrainz", "musicbrainz"])
assert got["notes"]["listenbrainz"] == "no data", got["notes"]
assert "listenbrainz" not in got["per_source"], got["per_source"]
assert got["asked"] == ["listenbrainz", "musicbrainz"], got["asked"]

# --------------------------------------------------------------------------- #
# 4) Nothing invented when every source is empty
# --------------------------------------------------------------------------- #
clear()
stub_json(lambda url, params: None)
stub_mb({})
got = chain(release=BARE_RELEASE)
assert got["genres"] == [] and got["per_track"] == {}, got
assert got["per_track_sources"] == {} and got["per_source"] == {}, got
assert got["sources"] == {FILE_ONE: [], FILE_TWO: []}, got["sources"]
assert got["levels"] == {FILE_ONE: None, FILE_TWO: None}, got["levels"]
assert got["notes"] == {"musicbrainz": "no data"}, got["notes"]
assert got["stopped_after"] is None, got["stopped_after"]

# --------------------------------------------------------------------------- #
# 5) The advisory extras — out of the way when absent
# --------------------------------------------------------------------------- #
# The YouTube age-gate route went with yt-dlp (2026-10-17): no advisory source
# reads a video id any more, and the route's own id parser went with it.
assert not hasattr(intg, "youtube_video_id") and not hasattr(intg, "youtube_age_advisory")

# No advisory source answers here (they are stubbed to "no data"), which is
# what lets the extras be checked by `checked` alone.
intg._advisory_json = lambda url, params=None, **kwargs: None

# Nobody is asked and nothing is added: the two name-based Apple routes are the
# whole list, whatever the tags say (a stored YouTube URL is not evidence).
intg._ADVISORY_CACHE.clear()
route = intg.resolve_advisory_route(title="Track One", artist="Test Artist",
                                    album="Test Album", cfg={},
                                    tags={"SOURCE": "https://youtu.be/dQw4w9WgXcQ"})
assert route["checked"] == ["apple-album", "itunes-song"], route
assert route["answers"] == {} and route["value"] is None, route

# Discogs' Parental Advisory format is ALBUM/EDITION-level evidence, so it
# must never rate a track: one flagged edition would otherwise make every
# track of the release read explicit. The advisory route does not ask Discogs
# at all any more — even with a token, a flagged release adds NOTHING.
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
# 6) The 30-day cache answers the second run instead of the network
# --------------------------------------------------------------------------- #
clear()
import tempfile

folder = tempfile.mkdtemp(prefix="mlo-genre-cache-")
intg._data_cache_dir = lambda name: os.path.join(folder, name) if name else None
try:
    mb_calls = stub_mb(MB_DEFAULT)
    first = chain(limit=12, release=MB_ALBUM_RELEASE)
    assert first["per_track"][(1, 1)] == ["Progressive Rock"], \
        first["per_track"]
    asked_first = len(mb_calls)
    assert asked_first, mb_calls

    # Forget the in-process memos only: what answers now is the DISK cache.
    intg._GENRE_CACHE.clear()
    discovery._CACHE.clear()
    again = chain(limit=12, release=MB_ALBUM_RELEASE)
    assert again["per_track"] == first["per_track"], again["per_track"]
    assert again["sources"] == first["sources"], again["sources"]
    assert len(mb_calls) == asked_first, mb_calls[asked_first:]
finally:
    intg._data_cache_dir = lambda name: None

# --------------------------------------------------------------------------- #
# 6b) The album BROWSE — the track tier's work id, and its own genres
# --------------------------------------------------------------------------- #
# A recording that states no genre of its own needs its WORK's classification;
# reaching the work needs the id MusicBrainz holds on the recording. The album
# browse (`release_recording_index`) carries that id for EVERY track of a
# release in one page (`recording?release=<id>&inc=genres+ratings+work-rels`,
# VERIFIED live to accept `work-rels`), where the per-recording route paid an
# identity lookup per track to find it — and a browse that does not answer
# leaves that per-recording route exactly where it was.
def stub_browse(router):
    """`mb_get_cached` answering by endpoint, with a call log."""
    calls = []

    def fake(endpoint, params=None, timeout=None, retries=None):
        calls.append((endpoint, dict(params or {})))
        return router(endpoint, dict(params or {}))

    intg.mb_get_cached = fake
    intg.mb_get = fake
    return calls


# A release payload that already states every recording's genre reads NO
# browse: `track["genres"]` IS that recording's own list, from the same
# entity, so a second read of it would be one request for nothing.
clear()
mb_calls = full_stack()
got = chain(limit=6, release=RELEASE)
assert got["per_track"][(1, 1)] == ["Alternative Metal"], got["per_track"]
assert "recording" not in mb_calls, mb_calls

# The recordings are silent, so the browse names the work each performs: ONE
# browse for the album + one work request per silent track — never the
# per-recording identity lookup the fallback uses.
clear()
calls = stub_browse(lambda endpoint, params: {
    "recording": {"recording-count": 2, "recordings": [
        {"id": "rec-1", "genres": [],
         "relations": [{"type": "performance", "work": {"id": "w-1"}}]},
        {"id": "rec-2", "genres": [],
         "relations": [{"type": "performance", "work": {"id": "w-2"}}]},
    ]},
    "work/w-1": {"genres": [{"name": "Space Rock"}]},
    "work/w-2": {"genres": [{"name": "Krautrock"}]},
}.get(endpoint, {}))
got = chain(limit=6, release=MB_ALBUM_RELEASE)
assert got["per_track"][(1, 1)] == ["Space Rock"], got["per_track"]
assert got["per_track"][(1, 2)] == ["Krautrock"], got["per_track"]
assert got["levels"] == {FILE_ONE: "track", FILE_TWO: "track"}, got["levels"]
endpoints = [e for e, _p in calls]
assert endpoints.count("recording") == 1, calls
assert "recording/rec-1" not in endpoints, calls
assert "recording/rec-2" not in endpoints, calls
assert sorted(e for e in endpoints if e.startswith("work/")) == \
    ["work/w-1", "work/w-2"], calls
# …and the browse asks about the RELEASE, 100 rows a page (the params the TTL
# cache is keyed by).
assert calls[0][1]["release"] == "rel-1", calls[0]
assert calls[0][1]["limit"] == 100, calls[0]
assert calls[0][1]["inc"] == "genres+ratings+work-rels", calls[0]

# The browse also states each recording's OWN genres, and the track tier reads
# them — one read answers the release per track.
clear()
calls = stub_browse(lambda endpoint, params: {
    "recording": {"recording-count": 2, "recordings": [
        {"id": "rec-1", "genres": [{"name": "Space Rock"}]},
        {"id": "rec-2", "genres": [{"name": "Krautrock"}]},
    ]},
}.get(endpoint, {}))
got = chain(limit=6, release=MB_ALBUM_RELEASE)
assert got["per_track"][(1, 1)] == ["Space Rock"], got["per_track"]
assert got["per_track"][(1, 2)] == ["Krautrock"], got["per_track"]
assert got["levels"] == {FILE_ONE: "track", FILE_TWO: "track"}, got["levels"]
assert [e for e, _p in calls].count("recording") == 1, calls

# The browse does not answer for the release: the per-recording route is the
# fallback, exactly as it was before this read existed (its own identity
# lookup finds the work), and a track with no work keeps the album tier.
clear()
calls = stub_browse(lambda endpoint, params: {
    "recording": {},
    "recording/rec-1": {"relations": [{"type": "performance",
                                       "work": {"id": "w-1"}}]},
    "recording/rec-2": {},
    "work/w-1": {"genres": [{"name": "Space Rock"}]},
    "release-group/rg-1": {"genres": [{"name": "Progressive Rock"}]},
}.get(endpoint, {}))
got = chain(limit=6, release=MB_ALBUM_RELEASE)
assert got["per_track"][(1, 1)] == ["Space Rock"], got["per_track"]
assert got["per_track"][(1, 2)] == ["Progressive Rock"], got["per_track"]
assert got["levels"] == {FILE_ONE: "track", FILE_TWO: "album"}, got["levels"]
endpoints = [e for e, _p in calls]
assert "recording/rec-1" in endpoints, calls
assert "recording/rec-2" in endpoints, calls

# --------------------------------------------------------------------------- #
# 7) The default priority list is the documented one, both old defaults migrate
# --------------------------------------------------------------------------- #
assert list(intg.GENRE_SOURCES) == DOCUMENTED_SOURCES, intg.GENRE_SOURCES
# RATINGS carry their own registry, and it is the same one source now.
from mlo import web_ratings as _wr                                      # noqa: E402
assert set(_wr.SOURCES) == set(intg.GENRE_SOURCES), (_wr.SOURCES, intg.GENRE_SOURCES)
assert list(_wr.SOURCES) == ["musicbrainz"], _wr.SOURCES
assert list(mcfg.DEFAULT_CONFIG["web_ratings_sources"]) == list(_wr.SOURCES), \
    mcfg.DEFAULT_CONFIG["web_ratings_sources"]
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
custom = ["musicbrainz"]
assert mcfg.normalize_config({"genre_sources": list(custom)})["genre_sources"] == custom
# No saved list at all is the same "no choice" case.
assert list(mcfg.normalize_config({})["genre_sources"]) == SHIPPED_DEFAULT
# And a saved list is what the chain runs — the config default is not forced
# over a hand-picked order.
clear()
full_stack()
picked = intg.genre_chain(artist="Test Artist", album="Test Album",
                          release=RELEASE,
                          cfg=dict(CFG, genre_sources=["musicbrainz"]),
                          limit=6, files=[FILE_ONE])
assert picked["sources"][FILE_ONE] == ["musicbrainz"], picked["sources"]

# --------------------------------------------------------------------------- #
# 8) The route surface — the background job, and the inline call it replaces
# --------------------------------------------------------------------------- #
# Everything below runs the REAL routes against a temp music folder with the
# chain itself stubbed: what is pinned here is the `sources` pass-through, the
# report reaching the caller, and the verbatim error text — the chain's own
# answers are covered above.
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

JOB_NOTES = {"musicbrainz": "answered"}
# The chain's report, in the shape `_run_genre_chain` passes through: per-source
# counts, what was actually asked and where it stopped, what was skipped and
# why, which tier answered, and the cap's own trail.
JOB_RESULT = {"updated": 2, "genres": ["Shoegaze"],
              "per_source": {"musicbrainz": ["Shoegaze"]}, "notes": JOB_NOTES,
              "per_track": {(1, 1): ["Shoegaze"]}, "sources": {}, "levels": {},
              "per_source_counts": {"musicbrainz": {"names": 1, "tracks": 2}},
              "asked": ["musicbrainz"], "stopped_after": "musicbrainz",
              "order": ["musicbrainz"],
              "skipped": {},
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

    There is no genre CHAIN in the import wizard any more — its buttons each
    ask ONE source — so what this suite has to hold is that `sources` reaches
    the chain as given, and that omitting it still means "every configured
    source" (the album/track menu's call)."""
    _asked.append(None if sources is None else list(sources))
    return dict(JOB_RESULT)


intg.genre_chain = _fake_chain
try:
    # One source per call: the wizard's "Genres from MusicBrainz" button,
    # answered by the ONE source.
    one = _client.post("/api/genres/import",
                       json={"paths": [JOB_ALBUM], "sources": ["musicbrainz"]})
    assert one.status_code == 200, one.text
    body = one.json()
    assert _asked[-1] == ["musicbrainz"], _asked
    assert body["genres"] == ["Shoegaze"], body
    assert body["per_source"] == {"musicbrainz": ["Shoegaze"]}, body
    # The chain's own report reaches the caller, and it keeps its own sentence
    # for the cases a name alone cannot express: a track answered at ALBUM
    # level, and a chain that stopped early.
    assert body["per_source_counts"] == {
        "musicbrainz": {"names": 1, "tracks": 2}}, body
    assert body["asked"] == ["musicbrainz"], body
    assert body["stopped_after"] == "musicbrainz", body
    assert body["skipped"] == {}, body
    assert body["level_counts"] == {"track": 1, "album": 1, "artist": 0}, body
    assert body["notes"]["musicbrainz"] == JOB_NOTES["musicbrainz"], body
    assert "from the release group" in body["notes"]["genre level"], body
    assert "stopped after musicbrainz" in body["notes"]["genre sources"], body
    assert set(body) >= {"updated", "genres", "per_source", "notes", "per_track",
                         "sources", "levels", "per_source_counts", "asked",
                         "stopped_after", "skipped", "level_counts",
                         "trimmed_files", "trimmed_genres"}, sorted(body)
    # The trimmed map reaches the caller keyed by "disc:position", never by the
    # chain's own (disc, position) tuple — the pre-fix route answered a
    # plain-text 500 here instead of this body.
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
    # import wizard says this is the album it is editing (staged), which is how
    # its button works on a folder that is not in the library yet.
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
# 9) NO source answered: the MODEL is the fallback (the owner's ask)
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
    """The source stubbed to answer NOTHING."""
    clear()
    stub_json(lambda url, params: None)
    stub_mb({})


_ai_mod.ai_configured = _ai_configured
_genre_ai.infer_genres = _infer
try:
    # A SOURCE THAT ANSWERED AT TRACK LEVEL IS NEVER HANDED OVER (2026-10-12):
    # the recording's own answer is complete by itself (R37), so the model is
    # not asked to "fill" it — the earlier gate asked whenever the cap was not
    # full, and its answer REPLACED the recording's own one-name list.
    full_stack()
    _ai_calls.clear()
    settled = intg.genre_chain(artist="Test Artist", album="Test Album",
                               release=RELEASE, cfg=dict(CFG, ai_genre_inference=True),
                               limit=3, files=[FILE_ONE, FILE_TWO],
                               sources=["musicbrainz"])
    assert _ai_calls == [], _ai_calls
    assert settled["per_track"][(1, 1)] == ["Alternative Metal"], settled["per_track"]

    _silent_sources()
    _ai_calls.clear()
    got = intg.genre_chain(artist="Test Artist", album="Test Album",
                           release=BARE_RELEASE, cfg=dict(CFG, ai_genre_inference=True),
                           limit=3, files=[FILE_ONE, FILE_TWO],
                           sources=["musicbrainz"])
    assert _ai_calls, "the model was never asked about a track nothing answered for"
    assert got["per_track"][(1, 1)] and got["per_track"][(1, 2)], got["per_track"]
    assert "shoegaze" in [g.casefold() for g in got["per_track"][(1, 1)]], got["per_track"]
    assert "ai" in got["per_track_sources"][(1, 1)], got["per_track_sources"]
    # The ask carries the track's own identity, which is what lets the model
    # research the right song rather than the album's general sound.
    assert _ai_calls[0]["title"] == "Track One", _ai_calls[0]
    assert _ai_calls[0]["candidates"] == [], _ai_calls[0]

    # ...and with the model switched OFF nothing is invented: an album whose
    # source answered nothing stays that way.
    _silent_sources()
    _ai_calls.clear()
    off = intg.genre_chain(artist="Test Artist", album="Test Album",
                           release=BARE_RELEASE, cfg=dict(CFG, ai_genre_inference=False),
                           limit=3, files=[FILE_ONE, FILE_TWO],
                           sources=["musicbrainz"])
    assert off["per_track"] == {}, off["per_track"]
    assert _ai_calls == [], _ai_calls
finally:
    _ai_mod.ai_configured, _genre_ai.infer_genres = _real_ai_configured, _real_infer

# ---------------------------------------------------------------------------
# The AI fallback's OWN terms (server.genre_ai): what it accepts from a model,
# and what it tells the model to answer with. No model call — the gate and the
# prompt are pure functions.
# ---------------------------------------------------------------------------
print()
print("== AI genre fallback: published names only ==")
from server import genre_ai as _ga                                    # noqa: E402

# A published name survives (resolved to the published spelling), and a broad
# one is no different from any other now: nothing above a name is derived or
# dropped (2026-10-12). An invented compound is dropped however plausible it
# reads; case-duplicates collapse; the limit is the ask.
_got = _ga._specifics(["Shoegaze", "rock", "neon-gaze fusion", "shoegaze"],
                      [], 3)
assert _got == ["shoegaze", "rock"], _got

# A genre a SOURCE stated is accepted in the source's own spelling even when
# the app's vocabulary does not know the name — dropping what a source said
# would be worse than storing it (the vocable grade reports it instead).
assert _ga.canonical("funeral doom") is None, "fixture: MB does not list it"
assert _ga._specifics(["Funeral Doom"], ["Funeral Doom"], 1) == ["Funeral Doom"]

# Every slot is a real genre slot now, so a one-slot budget is a real ask for
# one name — `_prompt` is what carries the ask, and it is a pure function (no
# model call, no endpoint needed).
assert "AT MOST 1 SPECIFIC genre(s)" in \
    _ga._prompt("a", "b", "", "", [], 1, None, False)

# The prompt states the taxonomy the names must come from, the heads the app
# files them under (so a detailed answer lands INSIDE that space), and the
# track the answer is for — the model must not answer for the artist. The
# system message is what forbids an invented name.
_p = _ga._prompt("Gorillaz", "Plastic Beach", "Stylo", "", [], 2, None, True)
for _needle in ("actually PUBLISHES",
                "The app files every genre under one of these broad families",
                "rock", "electronic", "for THIS track", "Stylo"):
    assert _needle in _p, (_needle, _p)
assert "never a name you made up" in _ga.SYSTEM, _ga.SYSTEM

print("genre_ai: published-name gate and prompt assertions passed")

# ----------------------------------------------------------------------- #
# The alias pass: the stored names first, the files' aliases only if nothing
# ----------------------------------------------------------------------- #
# A source that files a release under its localized name answers only to it,
# and the alias is on the files (`ALBUMALIAS`) — so the chain runs a second
# pass under those names, and ONLY when the first found nothing at all.
_calls = []
_real_names = intg._genre_chain_names
_real_alias_pairs = intg._genre_alias_pairs
try:
    def _fake_names(a="", al="", **kw):
        _calls.append((a, al))
        if al == "Alias Album":
            return {"genres": ["Art Pop"], "per_track": {}}
        return {"genres": [], "per_track": {}}

    intg._genre_chain_names = _fake_names
    intg._genre_alias_pairs = lambda a, al, files, cfg: [("Test Artist", "Alias Album")]

    _calls.clear()
    got = intg.genre_chain(artist="Test Artist", album="Test Album", cfg=CFG,
                           files=[FILE_ONE])
    assert _calls == [("Test Artist", "Test Album"),
                      ("Test Artist", "Alias Album")], _calls
    assert got["genres"] == ["Art Pop"], got

    # A first pass that answered never pays for the alias pass.
    intg._genre_chain_names = lambda a="", al="", **kw: (
        _calls.append((a, al)), {"genres": ["Heavy Metal"], "per_track": {}})[1]
    _calls.clear()
    got = intg.genre_chain(artist="Test Artist", album="Test Album", cfg=CFG,
                           files=[FILE_ONE])
    assert _calls == [("Test Artist", "Test Album")], _calls
    assert got["genres"] == ["Heavy Metal"], got

    # No aliases on the files -> no second pass at all.
    intg._genre_alias_pairs = lambda a, al, files, cfg: []
    _calls.clear()
    intg._genre_chain_names = lambda a="", al="", **kw: (
        _calls.append((a, al)), {"genres": [], "per_track": {}})[1]
    intg.genre_chain(artist="Test Artist", album="Test Album", cfg=CFG,
                     files=[FILE_ONE])
    assert _calls == [("Test Artist", "Test Album")], _calls
finally:
    intg._genre_chain_names = _real_names
    intg._genre_alias_pairs = _real_alias_pairs

print("genres: all assertions passed")