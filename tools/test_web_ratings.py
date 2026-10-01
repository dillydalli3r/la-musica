#!/usr/bin/env python3
"""Web ratings (`mlo/web_ratings.py`, script 24) — the whole chain, OFFLINE.

What this pins, and what it refuses to guess:

  * the SCALE conversion: every source publishes on its own scale (MusicBrainz
    0-5, RateYourMusic 0.5-5 whose maximum is what matters, Discogs 0-5) and
    all of them land on the app's 0-100 Picard scale — the same one RATING
    uses — through ONE helper, with halves rounded UP (a 4.125/5 that read 82
    because the digit above it was even is not a rule anyone can predict from
    the page).
  * the PARSERS, against payloads SAVED FROM THE LIVE SOURCES (nothing here
    touches the network): the MusicBrainz rating node as
    `release-group/<mbid>?inc=ratings` really answers it, the recording node
    with its work fallback, the RateYourMusic release page's schema.org
    microdata (the exact markup of a real page), and Discogs' `community
    .rating`. A payload that states no rating — MusicBrainz's
    `{"value": null, "votes-count": 0}`, Discogs' `{"count": 0}` — is a MISS
    and contributes nothing, never a zero.
  * the AGGREGATION: a vote-count-weighted mean over the sources that answered,
    a source with no count counting once, and None when nobody answered.
  * the WRITER: fill-only. A value already on the file survives a second run
    byte for byte unless the run is forced, and a switched-off family writes
    nothing. Proven on a REAL FLAC (tags read back off disk with mutagen) and
    on the runner itself.
  * the RUNNER (script 24): album-scoped, one album answer written to every
    track of the folder, a track whose own sources said nothing keeping
    ALBUMWEBRATING and no WEBRATING — nothing is invented from an album value.
    Its network is an INJECTED fetcher, so this suite can prove the whole run
    without a socket.

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
# The fixtures — payloads SAVED from the live sources (2026-10-01)
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

# RateYourMusic: the byte-for-byte microdata of a real release page (In
# Rainbows, from the archived capture this project's RYM ladder reads).
RYM_HTML = (
    '<div class="release_page" itemprop="mainEntity" itemscope '
    'itemtype="http://schema.org/MusicAlbum">\n'
    '     <div itemprop="aggregateRating" itemscope '
    'itemtype="http://schema.org/AggregateRating">\n'
    '     <meta itemprop="ratingValue" content="4.18" />\n'
    '     <meta itemprop="bestRating" content="5.0" />\n'
    '     <meta itemprop="worstRating" content="0.5" />\n'
    '     <meta itemprop="ratingCount" content="49366" /><meta '
    'itemprop="reviewCount" content="755" /></div>\n'
    '   <meta content="In Rainbows" itemprop="name" />\n'
)
# …and the visual twin, for a page whose microdata is missing.
RYM_HTML_AVG_ONLY = ('<th class="info_hdr">RYM Rating</td><td colspan="2">'
                     '<span class="avg_rating" >\n 3.77\n </span>'
                     '<span class="max_rating">/ <span>5.0</span>')

# Discogs, `releases/1174296` (Radiohead, In Rainbows) — the live document.
DISCOGS = {"id": 1174296, "title": "In Rainbows",
           "genres": ["Electronic", "Rock"],
           "styles": ["Alternative Rock", "Art Rock"],
           "community": {"have": 38869, "want": 19012,
                         "rating": {"count": 3809, "average": 4.72}}}
DISCOGS_UNRATED = {"community": {"rating": {"count": 0, "average": 0.0}}}


# Album of the Year, the album page of Neko Case's Middle Cyclone — the exact
# markup of a real capture (the score box, and the Details row that carries the
# genre anchors). The page states the user score twice: the exact average in
# the anchor's `title`, the rounded integer a visitor sees as its text.
AOTY_HTML = (
    '<div class="albumTopBox"><div class="albumCriticScoreBox">'
    '<div class="heading">Critic Score</div>'
    '<div class="albumCriticScore"><a href="#critics" title="78.0537">78</a>'
    '<div class="ratingBar green"><div class="green" style="width:78%;">'
    '</div></div></div><div class="text numReviews">Based on <strong>27'
    '</strong> reviews</div></div><div class="albumUserScoreBox">'
    '<div class="heading">User Score</div>'
    '<div class="albumUserScore"><a href="#users" title="76.7">77</a>'
    '<div class="ratingBar green"><div class="green" style="width:77%;">'
    '</div></div></div><div class="text numReviews">Based on <a href='
    '"/album/1-neko-case-middle-cyclone/user-reviews/?type=ratings">'
    '<strong>152</strong>&nbspratings</a></div></div>'
    '<div class="albumTopBox info"><div class="sectionHeading">'
    '<h2>Details</h2></div>'
    '<div class="detailRow"><a href="/genre/17-alt-country/">Alt-Country</a>, '
    '<a href="/genre/37-singer-songwriter/">Singer-Songwriter</a><br />'
    '<a href="/genre/70-americana/"><div class="secondary">Americana</div></a>'
    ', <a href="/genre/301-field-recordings/"><div class="secondary">'
    'Field Recordings</div></a> <span>/&nbsp;Genre</span></div></div>'
)
# The same page with the user score hidden — the critic number must NOT be
# picked up as the answer.
AOTY_CRITICS_ONLY = (
    '<div class="albumCriticScore"><a href="#critics" title="78.0537">78</a>'
    '</div><div class="text numReviews">Based on <strong>27</strong> reviews'
    '</div>'
)

print("== scale conversion ==")
# The one conversion, on every scale a source publishes. 4.18/5 = 83.6 -> 84;
# 4.75/5 = 95; 2.5/5 = 50; 4.125/5 = 82.5 -> 83 (halves UP, never to even).
eq(wr.scale_to_100(5), 100, "5/5 is 100")
eq(wr.scale_to_100(0), 0, "0/5 is 0")
eq(wr.scale_to_100(4.35), 87, "4.35/5 is 87")
eq(wr.scale_to_100(4.18), 84, "4.18/5 is 84")
eq(wr.scale_to_100(2.5), 50, "2.5/5 is 50")
eq(wr.scale_to_100(4.125), 83, "a half rounds UP (82.5 -> 83, never 82)")
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

eq(wr.rym_rating_from_html(RYM_HTML), {"value": 4.18, "count": 49366},
   "the RYM page's schema.org microdata parses")
eq(wr.rym_rating_from_html(RYM_HTML_AVG_ONLY), {"value": 3.77, "count": 0},
   "…and the visual avg_rating is the fallback, with no count stated")
eq(wr.rym_rating_from_html("<html><body>nothing</body></html>"), None,
   "a page that states no rating parses to None")
eq(wr.parse_rateyourmusic(RYM_HTML), (84, 49366, "RateYourMusic"),
   "the RYM page HTML -> 84, weighted by its 49 366 votes")
eq(wr.parse_rateyourmusic({"rating": {"value": 4.18, "count": 49366}}),
   (84, 49366, "RateYourMusic"),
   "…and the parsed answer the genre ladder hands over gives the same number")
eq(wr.parse_rateyourmusic({"genres": ["art rock"]}), None,
   "an answer with no rating sub-dict is a miss")

eq(wr.parse_discogs(DISCOGS), (94, 3809, "Discogs"),
   "Discogs: 4.72/5 over 3 809 votes")
eq(wr.parse_discogs(DISCOGS_UNRATED), None,
   "a Discogs release nobody rated is a miss, never a 0")
eq(wr.parse_aoty(AOTY_HTML), (77, 152, "Album of the Year"),
   "AOTY: the user score box, 0-100 as published, weighted by its ratings")
eq(wr.parse_aoty(AOTY_CRITICS_ONLY), None,
   "…and the CRITIC score is never read as the user score")
eq(wr.parse_aoty(""), None, "an empty page is a miss")
eq(wr.parse_source("musicbrainz", MB_ALBUM, "album"), (95, 105, "MusicBrainz"),
   "the one dispatch reads a MusicBrainz album payload")
eq(wr.parse_source("musicbrainz", MB_TRACK_OWN, "track"), (84, 18, "MusicBrainz"),
   "…and is level-aware for a track")
eq(wr.parse_source("albumoftheyear", AOTY_HTML, "album"),
   (77, 152, "Album of the Year"),
   "…and reads an AOTY page through the same dispatch")
eq(wr.parse_source("albumoftheyear", "2024-06: not an album page", "album"), None,
   "a payload with no user score contributes nothing")

# The AOTY GENRE side is the same page parsed by the genre provider layer, so
# its own parser is pinned to the same saved markup.
from server import integrations as intg                                  # noqa: E402
eq(intg.aoty_genres_from_html(AOTY_HTML),
   ["Alt-Country", "Singer-Songwriter", "Americana", "Field Recordings"],
   "the AOTY Details row's genre anchors, headline names first")
eq(intg.aoty_genres_from_html("<html><body>no genres here</body></html>"), [],
   "…and a page without them yields no names")
eq(intg.aoty_genres_from_html(""), [], "…and neither does an empty page")

print("== aggregation ==")
# 87 and 84 over 31 and 49 366 votes: the big one decides the number.
agg = wr.aggregate([(87, 31, "MusicBrainz"), (84, 49366, "RateYourMusic")])
eq(agg["value"], 84, "the mean is weighted by votes, not by source count")
eq(agg["sources"], ["MusicBrainz", "RateYourMusic"],
   "…and the contributing sources are named, in the order asked")
eq(wr.aggregate([(90, None, "A"), (80, "", "B")])["value"], 85,
   "a source with no count counts exactly ONCE (90+80)/2")
eq(wr.aggregate([(50, 1, "A"), (51, 1, "B")])["value"], 51,
   "a .5 tie rounds UP, so any two reads of the same answers agree")
eq(wr.aggregate([None, None]), None, "no answers at all aggregates to None")
eq(wr.aggregate([]), None, "…and so does an empty list")
eq(wr.aggregate([(120, 1, "A"), (200, 1, "B")])["value"], 100,
   "the mean is clamped into 0-100")

print("== the source order ==")
eq(wr.provider_order({}), [s for s in wr.SOURCES if s in wr.SOURCE_PARSERS],
   "no saved list = the built-in rank (minus any source without a parser)")
eq(wr.provider_order({"web_ratings_sources": ["discogs", "musicbrainz"]}),
   ["discogs", "musicbrainz"], "a saved order wins, as written")
eq(wr.provider_order({"web_ratings_sources": ["discogs", "bogus", "musicbrainz"]}),
   ["discogs", "musicbrainz"], "an unknown id is dropped, not fatal")
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

    ALBUM_ANSWER = {"value": 90, "sources": ["MusicBrainz", "RateYourMusic"]}
    TRACK_ANSWER = {"value": 80, "sources": ["MusicBrainz"]}

    res = wr.write_ratings(track, album=ALBUM_ANSWER, track=TRACK_ANSWER,
                           cfg={})
    ok(res["wrote"].get("WEBRATING") == 80, "WEBRATING is written")
    ok(res["wrote"].get("ALBUMWEBRATING") == 90, "ALBUMWEBRATING is written")
    ok(res["wrote"].get("WEBRATING_SOURCE") == "MusicBrainz",
       "the track's sources are written beside it")
    ok(res["wrote"].get("ALBUMWEBRATING_SOURCE") == "MusicBrainz; RateYourMusic",
       "the album's sources are \"; \"-joined")
    f = FLAC(track)
    eq(f["WEBRATING"], ["80"], "the value is on disk as the integer")
    eq(f["ALBUMWEBRATING"], ["90"], "…and so is the album value")
    eq(f["ALBUMWEBRATING_SOURCE"], ["MusicBrainz; RateYourMusic"],
       "…and its joined provenance")

    # A SECOND run must not touch a value the file already holds.
    res = wr.write_ratings(track, album={"value": 10, "sources": ["Discogs"]},
                           track={"value": 10, "sources": ["Discogs"]},
                           cfg={})
    ok(not res["wrote"], "a second run writes nothing")
    eq(res["skipped"].get("WEBRATING"), "already set",
       "…and says the value was already set")
    f = FLAC(track)
    eq(f["WEBRATING"], ["80"], "the existing WEBRATING is untouched")
    eq(f["ALBUMWEBRATING"], ["90"], "the existing ALBUMWEBRATING is untouched")
    eq(f["ALBUMWEBRATING_SOURCE"], ["MusicBrainz; RateYourMusic"],
       "the provenance that belongs to the kept value is untouched too")

    # Forced, the same call replaces them.
    res = wr.write_ratings(track, album={"value": 10, "sources": ["Discogs"]},
                           track={"value": 10, "sources": ["Discogs"]},
                           cfg={}, force=True)
    eq(res["wrote"].get("WEBRATING"), 10, "a forced run replaces the value")
    f = FLAC(track)
    eq(f["WEBRATING"], ["10"], "…on disk")
    eq(f["WEBRATING_SOURCE"], ["Discogs"], "…and its provenance")

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

    f = FLAC(os.path.join(album_dir, "01 Speak to Me.flac"))
    eq(f["ALBUMWEBRATING"], ["95"], "the album's own score lands on the track")
    eq(f["ALBUMWEBRATING_SOURCE"], ["MusicBrainz"], "…with its provenance")
    eq(f["WEBRATING"], ["84"], "…and the track's own score beside it")

    f = FLAC(os.path.join(album_dir, "02 Breathe.flac"))
    eq(f["ALBUMWEBRATING"], ["95"],
       "a track whose OWN sources said nothing still carries the album's")
    ok("WEBRATING" not in f, "…and gets NO WEBRATING — nothing is invented")
    ok("WEBRATING_SOURCE" not in f, "…and no provenance for a value it lacks")

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

print(f"\n{'PASS' if not failed else 'FAIL'} — {passed} ok, {failed} failed")
sys.exit(1 if failed else 0)
