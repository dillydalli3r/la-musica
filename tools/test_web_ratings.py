#!/usr/bin/env python3
"""Web ratings (`mlo/web_ratings.py`, script 24) — the whole chain, OFFLINE.

What this pins, and what it refuses to guess:

  * the SCALE conversion: MusicBrainz publishes on a 0-5 scale and its number
    lands on the app's 0-100 Picard scale — the same one RATING uses — through
    ONE helper, TRUNCATED to the app's grid (a 4.125/5 that read 82 because the
    digit above it was even is not a rule anyone can predict from the page).
  * the PARSERS, against a payload SAVED FROM THE LIVE SOURCE (nothing here
    touches the network): the MusicBrainz rating node as
    `release-group/<mbid>?inc=ratings` really answers it, and the recording
    node with its work fallback. A payload that states no rating —
    MusicBrainz's `{"value": null, "votes-count": 0}` — is a MISS and
    contributes nothing, never a zero.
  * the CHOICE: the configured order IS the hierarchy, so the value is the
    FIRST source that answered — at the BEST LEVEL it could (a track's
    recording beats the album, which beats the artist), never a mean: the file
    carries a score a reader can open the site and check.
  * the LADDER each source is asked down (the owner's rule, 2026-10-12):
    track -> album -> artist, with the album being the release GROUP and the
    artist's own MBID the last rung — a rung with no id is skipped without a
    request, and the rung each value came from is reported (`levels`, and the
    run's `track values by rung` line).
  * the WRITER: fill-only. A value already on the file survives a second run
    byte for byte unless the run is forced, and a switched-off family writes
    nothing. Proven on a REAL FLAC (tags read back off disk with mutagen) and
    on the runner itself.
  * the RUNNER (script 24): album-scoped, one album answer written to every
    track of the folder, and a track whose own source said nothing falling back
    to that source's ALBUM answer (recorded at album level in `levels`). Its
    network is an INJECTED fetcher, so this suite can prove the whole run
    without a socket.

MusicBrainz is the ONLY source (2026-10-12, the owner's decision — the scraped
sites are gone; see `mlo/config.py`), so the parsed source list, the saved
order and the parsers are all MusicBrainz-shaped.

Run:  python tools/test_web_ratings.py   (exit 0 = pass, 1 = failure, 2 = skip)
"""
import os
import shutil
import subprocess
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import web_ratings as wr                                     # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLAC_EXE = None
_deps = os.path.join(ROOT, ".dependencies")
if os.path.isdir(_deps):
    for entry in os.listdir(_deps):
        if entry.lower().startswith("flac"):
            cand = os.path.join(_deps, entry, "flac.exe")
            if os.path.isfile(cand):
                FLAC_EXE = cand
                break
if FLAC_EXE is None:
    FLAC_EXE = shutil.which("flac") or None

passed = 0
failed = 0


def ok(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}")


def eq(got, want, label):
    ok(got == want, f"{label}  (got {got!r}, want {want!r})")


# --------------------------------------------------------------------------- #
# The fixtures — a payload SAVED from the live source (2026-10-01)
# --------------------------------------------------------------------------- #
# MusicBrainz, `release-group/f5093c06-…?inc=ratings` (The Dark Side of the
# Moon) — the album-level shape the owner asked for, trimmed to what a parser
# reads.
MB_ALBUM = {"id": "f5093c06-23e3-404f-aeaa-40f72885ee3a",
            "title": "The Dark Side of the Moon",
            "rating": {"value": 4.75, "votes-count": 105},
            "genres": [{"name": "art rock"}, {"name": "progressive rock"}]}
# `recording/…?inc=ratings+genres+work-rels`, two real tracks: one the
# recording itself is rated for, one where only the WORK is.
MB_TRACK_OWN = {"recording": {"rating": {"value": 4.2, "votes-count": 18}},
                "work": {"rating": {"value": None, "votes-count": 0}}}
MB_TRACK_WORK_ONLY = {"recording": {"rating": {"value": None, "votes-count": 0}},
                      "work": {"rating": {"value": 5, "votes-count": 1}}}
# What an entity nobody rated answers — a MISS, not a zero.
MB_UNRATED = {"rating": {"value": None, "votes-count": 0}}
# `artist/<mbid>?inc=ratings` — the ladder's LAST rung (Pink Floyd, live).
MB_ARTIST = {"id": "83d91898-7763-47d7-b03b-b92132375c47",
             "rating": {"value": 4.45, "votes-count": 81}}

print("== scale conversion ==")
# The one conversion. The conversion TRUNCATES to the app's grid (0.05 on the
# 0-5 scale): the tag never holds a number ABOVE what the source published,
# because the number a reader sees is that grid value printed to one decimal
# and a rounding UP would carry it past the tenth's boundary the source itself
# rounded to. The grid points ARE those boundaries, so truncating makes the
# displayed one decimal equal the source's own (R358/R358e): 4.54 -> 90 ->
# "4.5" (not 91 -> "4.6"), 4.45 -> 89 -> "4.5", 4.35 -> 87 -> "4.4" — the last
# one is exactly ON the grid, and the tolerance in `scale_to_100` is what keeps
# binary noise from truncating it to 86 ("4.3", a wrong number).
eq(wr.scale_to_100(5), 100, "5/5 is 100")
eq(wr.scale_to_100(0), 0, "0/5 is 0")
eq(wr.scale_to_100(4.35), 87, "4.35/5 is 87 (on the grid, not 86 from float noise)")
eq(wr.scale_to_100(4.18), 83, "4.18/5 is 83 (the grid value at or below it)")
eq(wr.scale_to_100(2.5), 50, "2.5/5 is 50")
eq(wr.scale_to_100(4.125), 82, "82.5 truncates to 82 — a source's 4.125 reads "
                              "\"4.1\" at one decimal, and 83 would read \"4.2\"")
eq(wr.scale_to_100(5.5), 100, "above the scale clamps to 100, it is not dropped")
eq(wr.scale_to_100(None), None, "no value is no conversion")
eq(wr.scale_to_100("nonsense"), None, "a non-number is no conversion")
eq(wr.scale_to_100(82, 100), 82, "the 0-100 scale passes through unchanged")

print("== the per-source parsers (saved payloads, no network) ==")
eq(wr.parse_musicbrainz(MB_ALBUM), (95, 105, "MusicBrainz"),
   "MusicBrainz album: 4.75/5 with 105 votes")
eq(wr.parse_musicbrainz(MB_UNRATED), None,
   "an unrated MusicBrainz entity is a MISS, not a zero")
eq(wr.parse_musicbrainz({"rating": {"value": 4.2, "count": 18}}),
   (84, 18, "MusicBrainz"), "the older `count` spelling is read too")
eq(wr.parse_musicbrainz_track(MB_TRACK_OWN), (84, 18, "MusicBrainz"),
   "a track uses the RECORDING's own rating")
eq(wr.parse_musicbrainz_track(MB_TRACK_WORK_ONLY), (100, 1, "MusicBrainz"),
   "…and the WORK's only when the recording states none")
eq(wr.parse_musicbrainz_track({"recording": MB_UNRATED, "work": None}), None,
   "neither rated is a miss")

eq(wr.SOURCES, ["musicbrainz"],
   "MusicBrainz is the ONE shipped source (2026-10-12 — the scraped sites "
   "are gone)")
eq(list(wr.ALBUM_SOURCES), ["musicbrainz"], "it answers for an album")
eq(list(wr.TRACK_SOURCES), ["musicbrainz"],
   "…and for a track (an album answer may stand in)")

eq(wr.parse_source("musicbrainz", MB_ALBUM, "album"), (95, 105, "MusicBrainz"),
   "the one dispatch reads a MusicBrainz album payload")
eq(wr.parse_source("musicbrainz", MB_TRACK_OWN, "track"), (84, 18, "MusicBrainz"),
   "…and is level-aware for a track")
eq(wr.parse_source("musicbrainz", MB_TRACK_WORK_ONLY, "track"),
   (100, 1, "MusicBrainz"),
   "…where the recording's own rating falls back to the work's")
eq(wr.parse_source("musicbrainz", {"genres": ["art rock"]}, "album"), None,
   "a payload with no rating contributes nothing")
eq(wr.parse_source("nope", MB_ALBUM, "album"), None,
   "a source with no parser contributes nothing, never a guess")

print("== the choice: the order IS the hierarchy ==")
# One source: MusicBrainz's recording for the track, at TRACK level.
_asked = []


def _mb_track_fetch(source, kind, ident, cfg):
    _asked.append((source, kind))
    if source == "musicbrainz":
        return MB_TRACK_OWN
    raise AssertionError(f"{source} is not a shipped source ({kind})")


track = wr.track_rating("Radiohead", "Paranoid Android", "rec-mbid", {},
                        _mb_track_fetch)
eq(track["value"], 84, "MusicBrainz's own 4.2/5 -> 84 (truncated to the grid)")
eq(track["sources"], ["MusicBrainz"],
   "the source whose number the file carries is named")
eq(track["asked"], ["musicbrainz"], "…and exactly one request was made")
eq(track["levels"], {"musicbrainz": "track"}, "…at track level")
eq(_asked, [("musicbrainz", "track")], "one TRACK request, no album fallback")

# A source that states NOTHING for the track itself falls back to its ALBUM
# answer — still that source's data, marked `album` in `levels`.
def _mb_album_fallback_fetch(source, kind, ident, cfg):
    """MusicBrainz states nothing for this track; its release group does."""
    if source != "musicbrainz":
        return None
    if kind == "track":
        return {"recording": MB_UNRATED, "work": None}
    return MB_ALBUM


fb = wr.track_rating("Radiohead", "A Track Nobody Rated", "rec-mbid", {},
                     _mb_album_fallback_fetch,
                     album={"artist": "Radiohead", "album": "In Rainbows",
                            "mbid": "rg-mbid"})
eq(fb["levels"], {"musicbrainz": "album"},
   "no track-level answer -> the album answer stands, marked `album`")
eq(fb["value"], 95, "…and its number is the file's")
eq(fb["sources"], ["MusicBrainz"], "…attributed to the source that answered")

# …and when nobody states anything, the track has no web rating at all.
none_row = wr.track_rating("Radiohead", "Unrated", "rec-mbid", {},
                           lambda source, kind, ident, cfg: None,
                           album={"artist": "Radiohead", "album": "In Rainbows",
                                  "mbid": "rg-mbid"})
eq(none_row, None, "no answer at all is None, never a zero")

# …and an album that is unrated too falls to the ARTIST — the ladder's last
# rung (the owner's rule, 2026-10-12: track, then album, then artist).
print("== the ladder: recording, then the release group, then the artist ==")
_ladder_asked = []


def _mb_artist_only_fetch(source, kind, ident, cfg):
    """Nothing is rated except the ARTIST (the track, its work and the group
    only answer `{"value": null}`)."""
    _ladder_asked.append((kind, dict(ident or {})))
    if source != "musicbrainz":
        return None
    if kind == "track":
        return {"recording": MB_UNRATED, "work": None}
    if kind == "artist":
        return MB_ARTIST
    return MB_UNRATED


art = wr.track_rating("Pink Floyd", "A Track Nobody Rated", "rec-mbid", {},
                      _mb_artist_only_fetch,
                      album={"artist": "Pink Floyd", "album": "The Dark Side "
                             "of the Moon", "mbid": "rg-mbid",
                             "artist_mbid": "art-mbid"})
eq(art["levels"], {"musicbrainz": "artist"},
   "recording and group unrated -> the ARTIST's rating stands, marked `artist`")
eq(art["value"], 89, "…4.45/5 -> 89, on the app's scale like any other rung")
eq(art["sources"], ["MusicBrainz"], "…attributed to the source that answered")
eq([kind for kind, _ident in _ladder_asked], ["track", "album", "artist"],
   "the rungs are asked in that order, once each")
eq(_ladder_asked[-1][1].get("mbid"), "art-mbid",
   "…and the artist rung names the ALBUM ARTIST's own MBID")

# With no artist MBID to name, the last rung is SKIPPED — no request, no
# answer, never a crash.
_skipped_kinds = []


def _no_artist_fetch(source, kind, ident, cfg):
    _skipped_kinds.append(kind)
    if kind == "track":
        return {"recording": MB_UNRATED, "work": None}
    return MB_UNRATED


eq(wr.track_rating("Pink Floyd", "x", "rec-mbid", {}, _no_artist_fetch,
                   album={"artist": "Pink Floyd", "album": "x",
                          "mbid": "rg-mbid"}),
   None, "no artist MBID -> no artist rung, and so no answer at all")
ok("artist" not in _skipped_kinds, f"…and no request was made ({_skipped_kinds})")

# An ALBUM's own ladder ends in the same rung.
_album_asked = []


def _album_artist_fetch(source, kind, ident, cfg):
    _album_asked.append(kind)
    return MB_ARTIST if kind == "artist" else MB_UNRATED


alb = wr.album_rating("Pink Floyd", "The Dark Side of the Moon", "rg-mbid", {},
                      _album_artist_fetch, artist_mbid="art-mbid")
eq(alb["levels"], {"musicbrainz": "artist"},
   "an album whose release group is unrated takes the artist's score")
eq(alb["value"], 89, "…on the same scale")
eq(_album_asked, ["album", "artist"], "…asked group first, artist last")

# …and with no ids at all the ladder is a single rung with no requests.
_calls = []
eq(wr.track_rating("Pink Floyd", "x", "", {},
                   lambda source, kind, ident, cfg: _calls.append(kind)),
   None, "no MBIDs at all -> no answer")
eq(_calls, ["track"], "…and no rung below the recording to ask")

print("== the release id: the track rung carries it to the source ==")
# The album identity may name the RELEASE (`MUSICBRAINZ_ALBUMID`): it is what
# lets MusicBrainz answer the whole album from ONE browse instead of a lookup
# per track, so it rides on the TRACK rung's identity — the rung the source
# reads it from. Nothing else about the ladder changes: the levels words, the
# value and the provenance are exactly what the per-track path produces.
_seen_ident = []


def _release_aware_fetch(source, kind, ident, cfg):
    _seen_ident.append((kind, dict(ident)))
    if kind == "track":
        return MB_TRACK_OWN
    return MB_UNRATED


rel_got = wr.track_rating("Pink Floyd", "Time", "rec-1", {},
                          _release_aware_fetch,
                          album={"artist": "Pink Floyd", "album": "x",
                                 "mbid": "rg-1", "artist_mbid": "art-1",
                                 "release_mbid": "rel-1"})
eq(_seen_ident[0],
   ("track", {"artist": "Pink Floyd", "title": "Time", "mbid": "rec-1",
              "release_mbid": "rel-1"}),
   "the RELEASE id rides on the track rung's own identity")
eq(rel_got["levels"], {"musicbrainz": "track"},
   "…and the rung word is the one the per-track path reports: `track` "
   "(printed as the recording — the work fallback is INSIDE this rung)")
eq(rel_got["level"], "track", "…and it is the winning level")
eq(rel_got["value"], 84, "…with the recording's own 4.2/5 -> 84")
eq(rel_got["sources"], ["MusicBrainz"], "…attributed to the source that answered")

# No release id in the album identity = no release id on the ident: the
# source then takes the per-track path (see the browse cases below).
_seen_ident.clear()
wr.track_rating("Pink Floyd", "Time", "rec-1", {}, _release_aware_fetch,
                album={"artist": "Pink Floyd", "album": "x", "mbid": "rg-1",
                       "artist_mbid": "art-1"})
eq(_seen_ident[0][1],
   {"artist": "Pink Floyd", "title": "Time", "mbid": "rec-1"},
   "no release id -> the identity names none (the per-track path)")

print("== the choice function ==")
eq(wr.choose([((84, 31, "MusicBrainz"), "track")]),
   {"value": 84, "sources": ["MusicBrainz"]},
   "one answer is that answer, whatever its count")
eq(wr.choose([((90, 10, "Other"), "album"),
              ((84, 18, "MusicBrainz"), "track")]),
   {"value": 84, "sources": ["MusicBrainz"]},
   "a TRACK-level answer beats an album-level one stated above it")
eq(wr.choose([((90, 10, "Other"), "album"),
              ((95, 105, "MusicBrainz"), "album")]),
   {"value": 90, "sources": ["Other"]},
   "two album-level answers: the FIRST in the configured order wins")
eq(wr.choose([((89, 81, "MusicBrainz"), "artist"),
              ((95, 105, "MusicBrainz"), "album")]),
   {"value": 95, "sources": ["MusicBrainz"]},
   "an ALBUM-level answer beats an artist-level one stated above it")
eq(wr.choose([((89, 81, "MusicBrainz"), "artist")]),
   {"value": 89, "sources": ["MusicBrainz"]},
   "the artist rung is the last resort, not a miss")
eq(wr.choose([((4, 1, "A"), "album")]), {"value": 4, "sources": ["A"]},
   "a low count is not a weaker answer — the order decides, never the votes")
eq(wr.choose([]), None, "no answers at all chooses None")
eq(wr.choose([(None, "album")]), None, "…and a missing entry is skipped")

print("== the source order ==")
eq(wr.provider_order({}), [s for s in wr.SOURCES if s in wr.SOURCE_PARSERS],
   "no saved list = the built-in rank (minus any source without a parser)")
eq(wr.provider_order({"web_ratings_sources": ["musicbrainz"]}),
   ["musicbrainz"], "a saved list naming the source wins, as written")
eq(wr.provider_order({"web_ratings_sources": ["bogus", "musicbrainz"]}),
   ["musicbrainz"], "an unknown id is dropped, not fatal")
eq(wr.provider_order({"web_ratings_sources": ["bogus"]}),
   [s for s in wr.SOURCES if s in wr.SOURCE_PARSERS],
   "a list naming nothing real falls back to the built-in rank")

print("== the writer (fill only) ==")
if FLAC_EXE is None:
    print("  skip: flac.exe not found under .dependencies or on PATH")
else:
    from mutagen.flac import FLAC

    def make_flac(path):
        wav = path + ".wav"
        with wave.open(wav, "w") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x00\x00\x00" * 4410)
        subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", path, wav],
                       check=True, capture_output=True)
        os.remove(wav)

    tmp = tempfile.mkdtemp(prefix="mlo_web_ratings_")
    track = os.path.join(tmp, "01 Song.flac")
    make_flac(track)
    f = FLAC(track)
    f["TITLE"] = ["Song"]
    f["ARTIST"] = ["Artist"]
    f["ALBUM"] = ["Album"]
    f.save()

    ALBUM_ANSWER = {"value": 90, "sources": ["MusicBrainz"]}
    TRACK_ANSWER = {"value": 80, "sources": ["MusicBrainz"]}

    res = wr.write_ratings(track, album=ALBUM_ANSWER, track=TRACK_ANSWER,
                           cfg={})
    ok(res["wrote"].get("WEBRATING") == 80, "WEBRATING is written")
    ok(res["wrote"].get("ALBUMWEBRATING") == 90, "ALBUMWEBRATING is written")
    ok(res["wrote"].get("WEBRATING_SOURCE") == "MusicBrainz",
       "the track's sources are written beside it")
    ok(res["wrote"].get("ALBUMWEBRATING_SOURCE") == "MusicBrainz",
       "the album's sources are written beside it")
    f = FLAC(track)
    eq(f["WEBRATING"], ["80"], "the value is on disk as the integer")
    eq(f["ALBUMWEBRATING"], ["90"], "…and so is the album value")
    eq(f["ALBUMWEBRATING_SOURCE"], ["MusicBrainz"],
       "…and its provenance")

    # A SECOND run must not touch a value the file already holds.
    res = wr.write_ratings(track, album={"value": 10, "sources": ["MusicBrainz"]},
                           track={"value": 10, "sources": ["MusicBrainz"]},
                           cfg={})
    ok(not res["wrote"], "a second run writes nothing")
    eq(res["skipped"].get("WEBRATING"), "already set",
       "…and says the value was already set")
    f = FLAC(track)
    eq(f["WEBRATING"], ["80"], "the existing WEBRATING is untouched")
    eq(f["ALBUMWEBRATING"], ["90"], "the existing ALBUMWEBRATING is untouched")
    eq(f["ALBUMWEBRATING_SOURCE"], ["MusicBrainz"],
       "the provenance that belongs to the kept value is untouched too")

    # Forced, the same call replaces them.
    res = wr.write_ratings(track, album={"value": 10, "sources": ["MusicBrainz"]},
                           track={"value": 10, "sources": ["MusicBrainz"]},
                           cfg={}, force=True)
    eq(res["wrote"].get("WEBRATING"), 10, "a forced run replaces the value")
    f = FLAC(track)
    eq(f["WEBRATING"], ["10"], "…on disk")
    eq(f["WEBRATING_SOURCE"], ["MusicBrainz"], "…and its provenance")

    # The feature switch is the family's master switch, and it really gates.
    res = wr.write_ratings(track, album=ALBUM_ANSWER, track=TRACK_ANSWER,
                           cfg={"web_ratings_enabled": False}, force=True)
    ok(not res["wrote"], "with web_ratings_enabled off nothing is written")
    eq(res["skipped"].get("WEBRATING"), "write gate is off",
       "…and the skip says the gate, not 'no answer'")
    f = FLAC(track)
    eq(f["WEBRATING"], ["10"], "the file is left exactly as it was")

    # No answer is not an erasure: a missing track value writes nothing.
    res = wr.write_ratings(track, album=None, track=None, cfg={})
    eq(res["skipped"].get("WEBRATING"), "no answer",
       "an entity no source answered for is reported as no answer")

    print("== the runner (script 24), with an injected fetcher ==")

    def make_album(folder, tracks):
        os.makedirs(folder, exist_ok=True)
        for i, (title, rec_mbid) in enumerate(tracks, 1):
            path = os.path.join(folder, f"{i:02d} {title}.flac")
            make_flac(path)
            g = FLAC(path)
            g["TITLE"] = [title]
            g["ARTIST"] = ["Artist"]
            g["ALBUMARTIST"] = ["Artist"]
            g["ALBUM"] = [folder_name]
            g["MUSICBRAINZ_RELEASEGROUPID"] = [rg]
            if rec_mbid:
                g["MUSICBRAINZ_TRACKID"] = [rec_mbid]
            g.save()
        return folder

    calls = []

    def fake_fetch(source, kind, ident, cfg):
        """A fetcher with the app's contract and NO socket."""
        calls.append((source, kind, dict(ident)))
        if source != "musicbrainz" or not ident.get("mbid"):
            return None
        if kind == "track":
            return MB_TRACK_OWN
        return MB_ALBUM

    library = tempfile.mkdtemp(prefix="mlo_web_ratings_lib_")
    rg = "f5093c06-23e3-404f-aeaa-40f72885ee3a"
    folder_name = "The Dark Side of the Moon"
    album_dir = make_album(os.path.join(library, "Pink Floyd", folder_name),
                           [("Speak to Me", "bef3fddb-5aca-49f5-b2fd-d56a23268d63"),
                            ("Breathe", None)])
    # A second album, so the per-album identity is really per album.
    rg2 = "11111111-2222-3333-4444-555555555555"
    folder_name = "Another Album"
    rg = rg2
    album_dir2 = make_album(os.path.join(library, "Artist", folder_name),
                            [("Only Song", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")])

    stats = wr.run_web_ratings({"targets": [album_dir, album_dir2],
                                "web_ratings_sources": ["musicbrainz"],
                                "worker_limit": 2},
                               fetch=fake_fetch)
    ok(stats["album_count"] == 2, "both albums got an album rating")
    ok(stats["modified_count"] == 3, "all three tracks were written")
    ok(stats["error_count"] == 0, f"no errors (got {stats['errors']})")
    # `by_source` counts the TRACKS whose ALBUM answer a source contributed to
    # (the number the log prints beside "tracks written"), not the albums: the
    # album answer is fetched once and lands on every track, so "MusicBrainz: 3"
    # reads as "three tracks carry a MusicBrainz-sourced score".
    eq(stats["by_source"], {"musicbrainz": 3},
       "the per-source count is the tracks it answered for")
    # The RUNG each value came from is reported, not hidden: two tracks were
    # answered by their own recording and one by its album (that track names no
    # recording id, so its ladder walked down to the group) — and no album had
    # to fall to the artist rung.
    eq(stats["levels"], {"track": 2, "album": 1},
       "the run reports the rung every track's value came from")
    eq(stats["album_level_artist"], 0, "…and no album fell to the artist")

    f = FLAC(os.path.join(album_dir, "01 Speak to Me.flac"))
    eq(f["ALBUMWEBRATING"], ["95"], "the album's own score lands on the track")
    eq(f["ALBUMWEBRATING_SOURCE"], ["MusicBrainz"], "…with its provenance")
    eq(f["WEBRATING"], ["84"], "…and the track's own score beside it")

    f = FLAC(os.path.join(album_dir, "02 Breathe.flac"))
    eq(f["ALBUMWEBRATING"], ["95"],
       "a track whose OWN sources said nothing still carries the album's")
    eq(f["WEBRATING"], ["95"],
       "…and its WEBRATING falls back to that same source's ALBUM answer")
    eq(f["WEBRATING_SOURCE"], ["MusicBrainz"],
       "…attributed to the source that answered, at album level")

    # A second run over the same library changes nothing (fill-only, end to
    # end), and a source that never answers costs nothing.
    before = {}
    for root, _dirs, files in os.walk(library):
        for name in files:
            if name.endswith(".flac"):
                before[os.path.join(root, name)] = os.path.getmtime(
                    os.path.join(root, name))
    stats2 = wr.run_web_ratings({"targets": [album_dir, album_dir2],
                                 "web_ratings_sources": ["musicbrainz"],
                                 "worker_limit": 2}, fetch=fake_fetch)
    eq(stats2["modified_count"], 0, "a second run writes nothing at all")
    eq(stats2["skipped_count"], 3, "…and books every track as skipped")
    for path, stamp in before.items():
        eq(os.path.getmtime(path), stamp, f"{os.path.basename(path)} untouched")

    # THE FETCHES OBEY THE WRITES' OWN FILL-ONLY RULE. A second run over an
    # album whose tags are already on disk costs ZERO requests and still books
    # the album and its tracks; a forced run asks again.
    print("== a second run costs no requests (fill-only fetches) ==")
    fill_base = tempfile.mkdtemp(prefix="mlo_web_ratings_fill_")
    folder_name = "Rated Album"
    rg = "99999999-8888-7777-6666-555555555555"
    fill_dir = make_album(os.path.join(fill_base, "Artist", folder_name),
                          [("One", "rec-one"), ("Two", "rec-two")])
    fetch_log = []

    def tracking_fetch(source, kind, ident, cfg):
        fetch_log.append((source, kind))
        if source != "musicbrainz" or not ident.get("mbid"):
            return None
        return MB_ALBUM if kind == "album" else MB_TRACK_OWN

    fill_cfg = {"targets": [fill_dir], "web_ratings_sources": ["musicbrainz"],
                "worker_limit": 2}
    first = wr.run_web_ratings(fill_cfg, fetch=tracking_fetch)
    eq(first["modified_count"], 2, "the first run writes both tracks")
    ok(len(fetch_log) > 0, "…and asks the network")
    fetch_log.clear()
    second = wr.run_web_ratings(fill_cfg, fetch=tracking_fetch)
    eq(len(fetch_log), 0, "a second run makes ZERO fetch calls")
    eq(second["modified_count"], 0, "…writes nothing")
    eq(second["track_count"], 2, "…and still accounts for both tracks")
    eq(second["total_scanned"], 1, "…and for the album")
    eq(second["skipped_count"], 2, "…and books both tracks as skipped")
    fetch_log.clear()
    forced = wr.run_web_ratings(dict(fill_cfg, force_web_ratings=True),
                                fetch=tracking_fetch)
    ok(len(fetch_log) > 0, "a forced run fetches again")
    eq(forced["modified_count"], 2, "…and rewrites both tracks")
    shutil.rmtree(fill_base, ignore_errors=True)

    # The feature switch stops the run before a single request is made.
    asked = len(calls)

    def counting_fetch(*args, **kwargs):
        raise AssertionError("a switched-off run must not call the fetcher")

    stats3 = wr.run_web_ratings({"targets": [album_dir],
                                 "web_ratings_enabled": False,
                                 "web_ratings_sources": ["musicbrainz"]},
                                fetch=counting_fetch)
    eq(stats3["modified_count"], 0, "web_ratings_enabled off writes nothing")
    ok(len(calls) == asked, "…and asks no source")

    for d in (tmp, library):
        shutil.rmtree(d, ignore_errors=True)

print("== the album browse (server.integrations): ONE read for the folder ==")
# The other half of the contract: the source `server.integrations.
# web_rating_fetchers()` really is. The MusicBrainz seam is stubbed (no
# socket), and every case pins WHICH requests were made — the point of the
# change being that the album's browse answers every track at once.
from server import integrations as intg                              # noqa: E402

_real_cached = intg.mb_get_cached
_real_data_cache_dir = intg._data_cache_dir
intg._data_cache_dir = lambda name: None                             # no disk cache


def use_mb(router):
    """Install a stubbed `mb_get_cached`; returns the endpoint call log."""
    seen = []

    def fake(endpoint, params=None, timeout=None, retries=None):
        seen.append((endpoint, dict(params or {})))
        return router(endpoint, dict(params or {}))

    intg.mb_get_cached = fake
    return seen


BROWSE_ROW = {"id": "rec-1",
              "rating": {"value": 4.2, "votes-count": 18},
              "genres": [{"name": "progressive rock"}],
              "relations": [{"type": "performance",
                             "work": {"id": "work-1"}}]}
BROWSE_UNRATED = {"recording-count": 1, "recordings": [
    {"id": "rec-2", "rating": {"value": None, "votes-count": 0},
     "relations": [{"type": "performance", "work": {"id": "work-2"}}]}]}


def browse_router(endpoint, params):
    if endpoint == "recording":
        return {"recording-count": 1, "recordings": [BROWSE_ROW]}
    if endpoint == "recording/rec-1":
        return {"rating": {"value": 3.0, "votes-count": 9}}
    if endpoint == "recording/rec-9":
        return {"rating": {"value": 3.0, "votes-count": 9}}
    raise AssertionError(f"unexpected request: {endpoint}")


# ONE browse, paged while `recording-count` says there are more rows.
paged = use_mb(lambda endpoint, params: (
    {"recording-count": 130,
     "recordings": [{"id": f"r{i:03d}"} for i in range(100)]}
    if params.get("offset") == 0 else
    {"recording-count": 130,
     "recordings": [{"id": f"r{i:03d}"} for i in range(100, 130)]}))
index = intg.release_recording_index("rel-1")
eq(len(index), 130, "the album browse is paged until every row is read")
eq([p.get("offset") for _e, p in paged], [0, 100],
   "…100 rows at a time, following `recording-count`")
eq(index["r129"], {"id": "r129"}, "…each row indexed by its recording id")
eq(intg.release_recording_index(""), {}, "no release id -> no browse at all")

# THE BROWSE PATH: a release id is known and the browse answered for this
# recording, so its own rating is the answer and NOTHING else is read.
seen = use_mb(browse_router)
fetch = intg.web_rating_fetchers()
got = fetch("musicbrainz", "track",
            {"artist": "A", "title": "T", "mbid": "rec-1",
             "release_mbid": "rel-1"}, {})
eq(wr.parse_source("musicbrainz", got, "track"), (84, 18, "MusicBrainz"),
   "the browse's recording rating is the track's answer")
eq([e for e, _p in seen], ["recording"],
   "…and the album's ONE browse is the only request made")
eq(seen[0][1].get("inc"), "genres+ratings+work-rels",
   "…carrying genres, ratings and the work relations in one read")
eq(seen[0][1].get("limit"), 100, "…100 recordings per page")

# …through the LADDER, the rung word is exactly the per-track path's.
seen.clear()
lv = wr.track_rating("A", "T", "rec-1", {}, fetch,
                     album={"artist": "A", "album": "Al", "mbid": "rg-1",
                            "artist_mbid": "art-1", "release_mbid": "rel-1"})
eq(lv["levels"], {"musicbrainz": "track"},
   "the browse path reports the SAME `levels` word as the per-track path")
eq(lv["level"], "track", "…the winning level is `track` too")
eq(lv["value"], 84, "…and the value is the recording's own")
eq([e for e, _p in seen], ["recording"], "…one request for the whole album")

# THE FALLBACK: no release id -> the per-track lookup, exactly as before.
seen.clear()
got = fetch("musicbrainz", "track", {"artist": "A", "title": "T",
                                     "mbid": "rec-1"}, {})
eq([e for e, _p in seen], ["recording/rec-1"],
   "no release id -> the per-track lookup, the only path there is")
eq(wr.parse_source("musicbrainz", got, "track"), (60, 9, "MusicBrainz"),
   "…whose own rating is the answer")

# THE FALLBACK: the browse answered but does not list THIS recording.
seen.clear()
got = fetch("musicbrainz", "track",
            {"artist": "A", "title": "T", "mbid": "rec-9",
             "release_mbid": "rel-1"}, {})
eq([e for e, _p in seen], ["recording", "recording/rec-9"],
   "a track the browse does not list falls back to its own lookup")
eq(wr.parse_source("musicbrainz", got, "track"), (60, 9, "MusicBrainz"),
   "…which then answers")

# THE FALLBACK: the browse itself failed (a refused read is a miss, never fatal).
def boom_router(endpoint, params):
    if endpoint == "recording":
        raise RuntimeError("MusicBrainz is busy")
    return {"rating": {"value": 4.2, "votes-count": 18}}


seen = use_mb(boom_router)
got = fetch("musicbrainz", "track",
            {"artist": "A", "title": "T", "mbid": "rec-1",
             "release_mbid": "rel-1"}, {})
eq([e for e, _p in seen], ["recording", "recording/rec-1"],
   "a browse that failed is a miss -> the per-track lookup")
eq(wr.parse_source("musicbrainz", got, "track"), (84, 18, "MusicBrainz"),
   "…and the track still gets its rating")

# THE WORK FALLBACK: the recording is unrated, and its WORK's id rides ON the
# browse — so only the work itself is asked (no identity lookup to find it).
seen = use_mb(lambda endpoint, params: (
    BROWSE_UNRATED if endpoint == "recording" else
    {"rating": {"value": 5, "votes-count": 1}}))
got = fetch("musicbrainz", "track",
            {"artist": "A", "title": "T", "mbid": "rec-2",
             "release_mbid": "rel-1"}, {})
eq([e for e, _p in seen], ["recording", "work/work-2"],
   "the browse's own work relation is what the work lookup uses")
eq(wr.parse_source("musicbrainz", got, "track"), (100, 1, "MusicBrainz"),
   "…and the work's rating is the track's, as the per-track path gave it")

intg.mb_get_cached = _real_cached
intg._data_cache_dir = _real_data_cache_dir

print(f"\n{'PASS' if not failed else 'FAIL'} — {passed} ok, {failed} failed")
sys.exit(1 if failed else 0)