#!/usr/bin/env python3
"""Podcasts: the MusicBrainz SHAPE, the app's derived type, the chain's write,
the payload and the grading rule.

MusicBrainz has no "Podcast" release-group type. A podcast is a SERIES of type
Podcast (…/ws/2/series/<id> → "type":"Podcast"), and an episode is a release
group linked to that series with a `part of` relationship — the relation lives
on the GROUP, never on the release, and it rides along on a release-group
BROWSE too. Every case below is pinned against that shape, with stubbed
providers (no network):

  * `server.integrations.podcast_series_of` reads the series out of a payload:
    the Podcast one when the group is `part of` several series, the numbered
    one when several are Podcasts, None when it is in none (a plain Broadcast
    is NOT an episode), and `release_group_series` asks for exactly
    `inc=series-rels` on the release group. A selection of the derived type
    reads the BROWSE (which carries series-rels), never the search index.
  * the vocabulary: "podcast" is a first-class comparable type beside
    MusicBrainz's own names, matched against the derived identity rather than
    against a release-group type, and never sent to MusicBrainz as a query.
  * the tagging chain writes PODCASTSERIES / PODCASTSERIESMBID /
    PODCASTEPISODE onto the files, through the app's own tag layer, and a
    Broadcast album without the tag makes the prescan ask about one more time.
  * `server.library.podcast_info` turns the tags into the album row's
    `podcast` block a scan reads (never MusicBrainz again).
  * a podcast episode is NOT graded as a music CD: the music-only checks
    (lyrics, RateYourMusic link, album description, genre) are off for it —
    and a release that is not an episode is graded with its config UNCHANGED,
    which is the half that proves no music check was weakened.

Run:  python tools/test_podcast.py   (exit 0 = pass, 1 = failure)
"""
import os
import shutil
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo import naming, release_choice                    # noqa: E402
from mlo.autotag import _podcast_slot_open, mb_track_tags, write_mb_tags  # noqa: E402
from mlo.audio import AudioFile                           # noqa: E402
from mlo.grader import _PODCAST_MUSIC_ONLY_CHECKS, _grade_album  # noqa: E402
from server import integrations as intg                   # noqa: E402
from server import library as lib_mod                     # noqa: E402

FLAC_EXE = None
_deps = os.path.join(ROOT, ".dependencies")
if os.path.isdir(_deps):
    for entry in os.listdir(_deps):
        if entry.lower().startswith("flac"):
            cand = os.path.join(_deps, entry, "flac.exe")
            if os.path.isfile(cand):
                FLAC_EXE = cand
                break

passed = 0


def ok(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok: {label}")


def eq(got, want, label):
    ok(got == want, f"{label} ({got!r})")


# --------------------------------------------------------------------------- #
print("== the MusicBrainz shape: a Podcast SERIES, not a release-group type ==")


def _series_rel(name, number="", kind="Podcast", mbid="", **rel):
    return {
        "type": "part of", "target-type": "series",
        "attribute-values": {"number": number} if number else {},
        "series": {"id": mbid or f"{name}-mbid", "name": name, "type": kind,
                   "disambiguation": rel.get("disambiguation", "")},
    }


podcast = _series_rel("New Sounds", "2515", disambiguation="WNYC radio show")
eq(intg.podcast_series_of({"relations": [podcast]}),
   {"mbid": "New Sounds-mbid", "name": "New Sounds",
    "disambiguation": "WNYC radio show", "title": "New Sounds (WNYC radio show)",
    "number": "2515"},
   "the series of a plain episode, with MusicBrainz's disambiguation in the title")
# Several series on one release group: the PODCAST one wins, whatever order
# MusicBrainz lists them in — a radio show is often part of both its own
# programme series and the podcast series.
both = [{"type": "part of", "target-type": "series", "attribute-values": {},
         "series": {"id": "prog", "name": "New Sounds", "type": "Radio series",
                    "disambiguation": ""}},
        podcast]
eq(intg.podcast_series_of({"relations": both})["mbid"], "New Sounds-mbid",
   "the Podcast series is picked out of several 'part of' relations")
eq(intg.podcast_series_of({"relations": list(reversed(both))})["mbid"], "New Sounds-mbid",
   "…in either order")
# Two Podcast series (a re-published episode): the one MusicBrainz states an
# episode NUMBER for is the episode's own.
two_pods = [_series_rel("An Archive", "", mbid="a"), _series_rel("The Show", "12", mbid="b")]
eq(intg.podcast_series_of({"relations": two_pods})["mbid"], "b",
   "with two Podcast series the numbered one is the episode's own")
# The type is matched by its MusicBrainz ID as well as its name: a renamed or
# translated label cannot make the app stop seeing podcasts.
by_id = _series_rel("Un Show", "3", kind="", mbid="c")
by_id["series"]["type-id"] = intg.PODCAST_SERIES_TYPE_ID
eq(intg.podcast_series_of({"relations": [by_id]})["mbid"], "c",
   "the series type is matched by ID, not by its label alone")
# No series at all: an ordinary release group, a Broadcast the app cannot link
# to anything, and a node MusicBrainz answered without relations.
for empty in ({}, {"relations": []}, {"relations": None}, None,
              {"relations": [{"type": "producer", "target-type": "artist"}]},
              {"relations": [_series_rel("A Radio Show", kind="Radio series")]}):
    ok(intg.podcast_series_of(empty) is None,
       f"no Podcast series -> None ({str(empty)[:44]})")

# release_group_series: ONE cached request, asking the group for exactly the
# relation the fact lives in.
_asked = []
_real_cached = intg.mb_get_cached
intg.mb_get_cached = lambda endpoint, params=None, **kw: (
    _asked.append((endpoint, dict(params or {}))), {"relations": [podcast]})[1]
try:
    eq(intg.release_group_series("63ea5dff-b9c5-415b-8b5c-75ceb7c3d4f3")["name"], "New Sounds",
       "release_group_series reads the group's series")
    eq(_asked[-1], ("release-group/63ea5dff-b9c5-415b-8b5c-75ceb7c3d4f3",
                    {"inc": "series-rels", "fmt": "json"}),
       "asking release-group/<id>?inc=series-rels (the relation is not on the release)")
    eq(intg.release_group_series(""), None, "no id -> no request, no series")
    eq(len(_asked), 1, "…and the empty id asked MusicBrainz nothing")
finally:
    intg.mb_get_cached = _real_cached

# A release payload asks about the series for a BROADCAST group only — the
# type MusicBrainz gives every podcast episode — so a music library pays
# nothing for this feature. `release_lookup`'s own gate is exercised through
# the same helper the payload uses.
eq(intg.PODCAST_EPISODE_PRIMARY_TYPE, "Broadcast",
   "the episode primary type the release lookup gates on is MusicBrainz's own")

# --------------------------------------------------------------------------- #
print("\n== the discography paths carry it at no extra request ==")
# The browse asks for `inc=…+series-rels` and each row carries its own
# `podcast` block; the SEARCH index cannot, which is why a derived-type
# selection never takes that branch.
_calls = {"browse": 0, "search": 0}
_real_browse, _real_search = intg._browse_collect, intg.search_mb
_rows = [{"id": "rg-1", "title": "Episode 1", "primary-type": "Broadcast",
          "secondary-types": [], "first-release-date": "2008-09-13",
          "relations": [podcast]},
         {"id": "rg-2", "title": "A Record", "primary-type": "Album",
          "secondary-types": [], "first-release-date": "1999-01-01"}]
_seen_params = []


def _fake_browse(endpoint, extra_params, list_key, count_key, limit=300, offset=0):
    _calls["browse"] += 1
    _seen_params.append(dict(extra_params))
    return list(_rows), len(_rows), len(_rows)


intg._browse_collect = _fake_browse
intg.search_mb = lambda *a, **kw: (_calls.__setitem__("search", _calls["search"] + 1), {})[1]
try:
    page = intg.artist_release_groups("artist-1", limit=10)
    by_id = {rg["id"]: rg for rg in page["release_groups"]}
    eq(by_id["rg-1"]["podcast"]["name"], "New Sounds",
       "the browse rows carry the series (aliases ride the same request)")
    eq(by_id["rg-2"]["podcast"], None, "a row in no series carries None")
    ok("series-rels" in _seen_params[-1].get("inc", ""),
       "the browse inc asks for series-rels")
    eq(_calls["search"], 0, "an unfiltered discography still asks the browse")
    # A derived-type filter: the browse again (the index could not answer it).
    page = intg.artist_release_groups("artist-1", limit=10, primary_type="podcast")
    eq(_calls["search"], 0, "a podcast filter never asks the search index")
    eq(len(page["release_groups"]), 2, "…it answers from the browse window")
    # A MusicBrainz type still takes the index (that is what it is for).
    intg.search_mb = lambda *a, **kw: (_calls.__setitem__("search", _calls["search"] + 1),
                                       {"total": 0, "next": None, "rows": []})[1]
    intg.artist_release_groups("artist-1", limit=10, primary_type="broadcast")
    eq(_calls["search"], 1, "a MusicBrainz type still reads the search index")
finally:
    intg._browse_collect, intg.search_mb = _real_browse, _real_search

# --------------------------------------------------------------------------- #
print("\n== the derived type is a comparable selection ==")
ok("podcast" in naming.RELEASE_TYPES, "podcast is in the published vocabulary")
ok("podcast" not in naming.PRIMARY_RELEASE_TYPES
   and "podcast" not in naming.SECONDARY_RELEASE_TYPES,
   "…and NOT among MusicBrainz's own types (it is not one)")
eq(release_choice.type_names(["Podcast", "podcast"]), ["podcast"],
   "a selection may name it, and it normalizes like any other")
ok(release_choice.type_matches("Broadcast", [], ["podcast"], ("podcast",)),
   "an episode matches a podcast selection")
ok(not release_choice.type_matches("Broadcast", [], ["podcast"]),
   "a plain Broadcast (no series relation read) does NOT")
ok(release_choice.type_matches("Broadcast", [], ["broadcast"], ("podcast",)),
   "and the two selections are independent: broadcast still matches an episode")
eq(release_choice.derived_types({"podcast": {"series": "X"}}), ("podcast",),
   "derived_types reads the marker off a payload")
eq(release_choice.derived_types({"tags": {"PODCASTSERIES": "X"}}), ("podcast",),
   "…or off a library row's own tag")
eq(release_choice.derived_types({"meta": {"ALBUM": "X"}}), (), "a music album carries none")

# The watch gate applies it (server.artist_watch delegates to the one rule), so
# a watch may ask for podcasts and a Broadcast radio play does not fall in.
from server import artist_watch  # noqa: E402

_ep_rg = {"id": "rg-1", "title": "Episode 1", "primary_type": "Broadcast",
          "secondary_types": [], "first_release_date": "2026-09-01",
          "podcast": {"name": "New Sounds", "number": "2515"}}
_play_rg = {"id": "rg-2", "title": "A Play", "primary_type": "Broadcast",
            "secondary_types": [], "first_release_date": "2026-09-02"}
_watch = {"added_at": 0, "release_types": ["podcast"], "exclude": [], "include": [],
          "policy": "new"}
eq(artist_watch.evaluate(_ep_rg, _watch)["allowed"], True, "a watch may queue an episode")
eq(artist_watch.evaluate(_play_rg, _watch)["allowed"], False,
   "…and leaves a broadcast that is no episode alone")
_watch_b = dict(_watch, release_types=["broadcast"])
eq(artist_watch.evaluate(_ep_rg, _watch_b)["allowed"], True,
   "an episode is still a Broadcast to a broadcast selection")

# --------------------------------------------------------------------------- #
print("\n== the tagging chain writes the identity ==")
_release = {"title": "Episode 1", "release_type": "broadcast", "primary_type": "Broadcast",
            "podcast": {"mbid": "sg-1", "name": "New Sounds",
                        "title": "New Sounds (WNYC radio show)", "number": "2515"}}
_tags = dict(mb_track_tags(_release, {}))
eq(_tags.get("PODCASTSERIES"), "New Sounds (WNYC radio show)", "the series title is written")
eq(_tags.get("PODCASTSERIESMBID"), "sg-1", "…with the MusicBrainz series id")
eq(_tags.get("PODCASTEPISODE"), "2515", "…and the episode number")
eq(_tags.get("RELEASETYPE"), "broadcast",
   "RELEASETYPE keeps MusicBrainz's own type — the series is a separate fact")
_music_release = {k: v for k, v in _release.items() if k != "podcast"}
_plain = dict(mb_track_tags(_music_release, {}))
ok(not any(k.startswith("PODCAST") for k in _plain),
   "a release that is no episode writes no podcast tag at all")

# The prescan's own rule: a Broadcast without the tag is worth one more ask
# (the series is a fact about the release group's relations, which no tag of
# the album states) — and every other release keeps the old contract.
class _FakeFile:
    def __init__(self, tags):
        self._tags = tags

    def get_tag(self, name):
        return self._tags.get(name)


ok(_podcast_slot_open(_FakeFile({"RELEASETYPE": "Broadcast"})),
   "a Broadcast with no series is an open slot (one more look)")
ok(not _podcast_slot_open(_FakeFile({"RELEASETYPE": "Broadcast", "PODCASTSERIES": "X"})),
   "a Broadcast that carries the series is finished")
ok(not _podcast_slot_open(_FakeFile({"RELEASETYPE": "Album"})),
   "a music album is never asked about a podcast it cannot be")

if not FLAC_EXE:
    print("  SKIP the FLAC round-trip and the grader cases (no flac.exe under .dependencies)")
    print(f"\n{passed} checks passed (flac.exe missing — container cases skipped)")
    sys.exit(0)

_tmp = tempfile.mkdtemp(prefix="mlo-podcast-")


def make_flac(path):
    """A real (silent) FLAC through the real container writer — the fixture
    recipe tools/test_tag_hygiene.py uses."""
    wav = path + ".wav"
    with wave.open(wav, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * 4410)
    subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", path, wav],
                   check=True, capture_output=True)
    os.remove(wav)


try:
    _af_path = os.path.join(_tmp, "episode.flac")
    make_flac(_af_path)
    af = AudioFile(_af_path)
    written, refused = write_mb_tags(af, mb_track_tags(_release, {}))
    ok(written and not refused, f"the chain writes the tags ({written} written, {refused})")
    read = AudioFile(_af_path)
    eq(read.get_tag("PODCASTSERIES"), "New Sounds (WNYC radio show)",
       "PODCASTSERIES round-trips through the app's own tag layer")
    eq(read.get_tag("PODCASTSERIESMBID"), "sg-1", "PODCASTSERIESMBID round-trips")
    eq(read.get_tag("PODCASTEPISODE"), "2515", "PODCASTEPISODE round-trips")
    # The tag vocabulary the UI serves is not broken by the new tags: an
    # unknown-but-allowed name is what the excess-tag check reads.
    from mlo.grader import tag_key_allowed
    ok(all(tag_key_allowed(k) for k in
           ("PODCASTSERIES", "PODCASTSERIESMBID", "PODCASTEPISODE")),
       "the three tags are part of the app's own tag vocabulary (never 'excess')")

    # ----------------------------------------------------------------------- #
    print("\n== the library payload reads it off the files ==")
    eq(lib_mod.podcast_info({"PODCASTSERIES": "New Sounds (WNYC radio show)",
                             "PODCASTSERIESMBID": "sg-1", "PODCASTEPISODE": "2515"}),
       {"series": "New Sounds (WNYC radio show)", "series_mbid": "sg-1", "episode": 2515},
       "podcast_info turns the tags into the album row's block")
    eq(lib_mod.podcast_info({"PODCASTSERIES": ""}), None, "no series -> no block")
    eq(lib_mod.podcast_info({"ALBUM": "X"}), None, "a music album -> no block")
    eq(lib_mod.podcast_info({"PODCASTSERIES": "Show", "PODCASTEPISODE": ""}),
       {"series": "Show", "series_mbid": None, "episode": None},
       "a missing episode number is None, not 0")
    for tag in ("PODCASTSERIES", "PODCASTSERIESMBID", "PODCASTEPISODE"):
        ok(tag in lib_mod.TRACK_TAGS and tag in lib_mod.ALBUM_LEVEL_TAGS,
           f"{tag} is read into the payload (track and album level)")
    # The real payload: one episode folder, graded and enriched by the app's
    # own builder, carrying the block the shelf and the series page read.
    ep_dir = os.path.join(_tmp, "WNYC", "2008-09-13 - New Sounds #2515")
    os.makedirs(ep_dir)
    ep_path = os.path.join(ep_dir, "01 Episode 1.flac")
    make_flac(ep_path)
    ep_af = AudioFile(ep_path)
    write_mb_tags(ep_af, [
        ("TITLE", "Episode 1"), ("ARTIST", "John Schaefer"),
        ("ALBUMARTIST", "John Schaefer"), ("ALBUM", "New Sounds #2515"),
        ("DATE", "2008-09-13"), ("RELEASETYPE", "Broadcast"),
        ("MEDIA", "Digital Media"), ("MUSICBRAINZ_ALBUMID", "rel-1"),
        ("INSTRUMENTAL", "0"),
        ("PODCASTSERIES", "New Sounds (WNYC radio show)"),
        ("PODCASTSERIESMBID", "sg-1"), ("PODCASTEPISODE", "2515"),
    ])
    row = lib_mod.build_album(ep_dir, {"music_folder": _tmp}, light=True)
    eq(row["podcast"], {"series": "New Sounds (WNYC radio show)",
                        "series_mbid": "sg-1", "episode": 2515},
       "build_album's row carries the podcast block")
    eq(row["meta"]["PODCASTSERIES"], "New Sounds (WNYC radio show)",
       "…and the tags reach the row's meta")

    # ----------------------------------------------------------------------- #
    print("\n== grading: an episode is not graded as a music CD ==")
    cfg = {"music_folder": ""}  # no naming check: this is about the tag families
    res = _grade_album(ep_dir, "EMBEDDED", dict(cfg))
    codes = {text for text in (res.get("issues") or {})}
    music_only = ("Missing lyrics", "RateYourMusic", "Album description",
                  "GENRE_MISSING", "Missing GENRE")
    for needle in music_only:
        ok(not any(needle in text for text in codes),
           f"an episode is not failed for {needle!r}")
    # What it IS still graded on is the ordinary tag/artefact set (this fixture
    # has no cover, no mood, no track number) — and none of those is a music
    # rule, and none is a CD-rip expectation: the episode's medium is Digital
    # Media, so the rip-log family never applies to it.
    failing = sorted(codes)
    ok(all(text.startswith(("Missing", "EXPECTED_TRACKS")) for text in failing),
       f"its failures are missing-tag artefacts ({failing})")
    ok(not any(("log" in text.lower() or "cue" in text.lower()
                or "Accur" in text or text.startswith("Rip "))
               for text in failing),
       "no CD-rip artefact is demanded of a Digital Media episode")
    # The SAME folder without the marker — the music release it would otherwise
    # be — fails exactly those checks, so the rule is a podcast rule and not a
    # weakened check.
    plain_dir = os.path.join(_tmp, "Band", "2008 - Album")
    os.makedirs(plain_dir)
    plain_path = os.path.join(plain_dir, "01 Track.flac")
    make_flac(plain_path)
    plain_af = AudioFile(plain_path)
    plain_tags = [(k, v) for k, v in (
        ("TITLE", "Track"), ("ARTIST", "Band"), ("ALBUMARTIST", "Band"),
        ("ALBUM", "Album"), ("DATE", "2008-09-13"), ("RELEASETYPE", "Album"),
        ("MEDIA", "CD"), ("MUSICBRAINZ_ALBUMID", "rel-2"), ("INSTRUMENTAL", "0"))]
    write_mb_tags(plain_af, plain_tags)
    plain = _grade_album(plain_dir, "EMBEDDED", dict(cfg))
    plain_codes = {text for text in (plain.get("issues") or {})}
    ok(any("Missing lyrics" in text for text in plain_codes),
       "the same folder without the marker still fails for missing lyrics")
    ok(any("RateYourMusic" in text for text in plain_codes),
       "…and for the RateYourMusic link")
    ok(any("Album description" in text for text in plain_codes),
       "…and for the missing album description")
    ok(any("GENRE" in text for text in plain_codes),
       "…and for the missing genre")
    # And a CD rip is still asked for everything a podcast episode is spared —
    # the exemption is the podcast MARKER's, not a weakened rule.
    ok(any(".log file" in text for text in plain_codes)
       and any(".cue file" in text for text in plain_codes),
       "a CD release is still asked for its rip log and cue sheet")
    # The exemption is a LIST of keys, and the CD-rip family is NOT in it: those
    # are gated on MEDIA=CD by the grader itself (a Digital Media episode is
    # never asked for a rip log or an AccurateRip verdict).
    ok("grade_check_cd_log" not in _PODCAST_MUSIC_ONLY_CHECKS
       and "grade_check_accuraterip" not in _PODCAST_MUSIC_ONLY_CHECKS,
       "the CD-rip checks are not exempted (they are gated on MEDIA=CD already)")
    ok(all(k.startswith("grade_check_") for k in _PODCAST_MUSIC_ONLY_CHECKS),
       "every exempted key is a real check toggle")
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

print(f"\n{passed} checks passed")
