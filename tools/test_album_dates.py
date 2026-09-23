#!/usr/bin/env python3
"""The album folder's two dates — original release and release — in FULL.

The naming script names the album folder
``[Album] <ORIGINALDATE> - <DATE> - Album {...}``, so the two date TAGS are
what the folder spells. This suite covers every path that puts them there:

  * the shared sharpening rule (``mlo.naming.fuller_date`` /
    ``date_is_partial``): a bare year is completed from MusicBrainz, a value
    that already says more — or says something else — is left alone;
  * the tagging pass (``mlo.autotag._fill_release_tags`` over the cached
    MusicBrainz release): a year-only DATE/ORIGINALDATE is sharpened, an
    album whose dates are only that coarse is still worth a request, and an
    album whose dates are already full costs none;
  * the folder the naming script then produces spells BOTH dates in full;
  * RELEASECOUNTRY: the one release tag written as a LIST — every country the
    release states, ";"-joined with MusicBrainz's own first event first, so a
    release out in US, CA and XE says so instead of one code; a file holding a
    strict subset of them gains the rest, a country the release does not state
    is left alone, and a release stating none writes nothing;
  * the MP3 spelling of ORIGINALDATE is TDOR — the frame Picard and beets'
    mediafile read (the app's older TDRL is still read and is replaced on
    the next write), so the beets import computes the same album folder.

Run:  python tools/test_album_dates.py
"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mlo.autotag as autotag
from mlo.audio import AudioFile
from mlo.config import DEFAULT_CONFIG
from mlo.naming import (DEFAULT_NAMING_SCRIPT, date_is_partial, eval_script,
                        fuller_date, track_variables)

passed = 0


def ok(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok: {label}")


# --------------------------------------------------------------------------- #
# 1) The shared rule: only ever ADD precision to a date tag
# --------------------------------------------------------------------------- #
ok(fuller_date("1980", "1980-10-01") == "1980-10-01", "a year is completed to the day")
ok(fuller_date("1980-10", "1980-10-01") == "1980-10-01", "a year-month is completed to the day")
ok(fuller_date("1980-10-01", "1980-10-01") == "", "an identical value writes nothing")
ok(fuller_date("1980-10-01", "1980") == "", "a coarser value never overwrites a full one")
ok(fuller_date("1979", "1980-10-01") == "", "a different year is left alone")
ok(fuller_date("1980-11", "1980-10-01") == "", "a different month is left alone")
ok(fuller_date("", "1980-10-01") == "", "an empty tag is the caller's fill, not an upgrade")
ok(fuller_date("circa 1980", "1980-10-01") == "", "a non-ISO value is left alone")
ok(fuller_date("1980", "1980-10-01T00:00") == "", "a non-ISO new value is refused")
ok(date_is_partial("1980") and date_is_partial("1980-10"),
   "a year / year-month still has detail to gain")
ok(not date_is_partial("1980-10-01") and not date_is_partial("")
   and not date_is_partial("circa 1980"),
   "a full date, an empty tag and a non-ISO value have nothing to gain")


# --------------------------------------------------------------------------- #
# 2) The tagging pass: MusicBrainz sharpens the dates the folder is named after
# --------------------------------------------------------------------------- #
MBID = "94eeba12-c32b-412a-9ae4-20dff68b1f05"
MB_PAYLOAD = {
    "id": MBID,
    "date": "1997-03-25",
    "country": "US",
    # The release's own events, as MusicBrainz publishes them: one per country
    # the pressing appeared in, with the singular `country` above being only
    # the FIRST of them. Three codes, so the tag this pass writes has to hold
    # a list rather than the one code it used to.
    "release-events": [
        {"date": "1997-03-25", "area": {"name": "United States",
                                        "iso-3166-1-codes": ["US"]}},
        {"date": "1997-04-01", "area": {"name": "Canada",
                                        "iso-3166-1-codes": ["CA"]}},
        {"date": "1997-06-01", "area": {"name": "[Worldwide]",
                                        "iso-3166-1-codes": ["XE"]}},
    ],
    "label-info": [{"label": {"name": "Sire Records"}, "catalog-number": "6095-2"}],
    "artist-credit": [{"artist": {"id": "artist-1"}}],
    "media": [{"position": 1, "format": "CD",
               "tracks": [{"position": 1, "recording": {"id": "recording-1"},
                           "artist-credit": [{"artist": {"id": "artist-1"}}]}]}],
    "release-group": {"id": "group-1", "primary-type": "Album",
                      "secondary-types": [], "first-release-date": "1980-10-01"},
}
# The same release with NO country at all: a pressing MusicBrainz lists no
# event for states no country, and the tag must then be left exactly as it is.
MB_PAYLOAD_NO_COUNTRY = {k: v for k, v in MB_PAYLOAD.items()
                         if k not in ("country", "release-events")}
CALLS = []


def _mb_stub(calls=CALLS, payload=MB_PAYLOAD):
    """The tag pass's server seams, with nothing that can open a socket.

    The tag pass imports `mb_get_cached` and `release_countries` from
    `server.integrations`, so stubbing the module name it looks up exercises
    the REAL `_cached_release` payload mapping (both dates, the release's
    countries) and the real write rule — no network, no HTTP client.
    `release_countries` is the server's own reader of a release's events,
    imported from the real module: the countries the pass writes and the
    countries the pages show must be parsed by the same code.
    """
    from server.integrations import release_countries

    module = types.ModuleType("server.integrations")

    def mb_get_cached(entity, params=None):
        calls.append(entity)
        return payload

    module.mb_get_cached = mb_get_cached
    module.release_countries = release_countries
    return module


class FakeAF:
    """A file's tag dict with mlo.audio.AudioFile's get/set contract.

    A LIST is one answer per value — AudioFile.set_tag writes repeated
    container fields and get_tag joins them back with "; " (mlo.tagtext's
    _LIST_SEP), so the fake joins them the same way and an assertion here
    reads exactly what a file on disk reads back.
    """

    def __init__(self, path, tags):
        self.path = path
        self.tags = dict(tags)

    def get_tag(self, name):
        return self.tags.get(name)

    def set_tag(self, name, value):
        if isinstance(value, (list, tuple)):
            value = "; ".join(str(v) for v in value)
        self.tags[name] = value
        return True


TMP = tempfile.mkdtemp(prefix="mlo_album_dates_")
albums = []


def album_file(name, tags):
    """One album's track: a fake file plus its album folder."""
    album_dir = os.path.join(TMP, name)
    os.makedirs(album_dir, exist_ok=True)
    albums.append(album_dir)
    return FakeAF(os.path.join(album_dir, "1-01 Track.flac"), tags)


cfg = dict(DEFAULT_CONFIG)
saved = sys.modules.get("server.integrations")
sys.modules["server.integrations"] = _mb_stub()
try:
    release = autotag._cached_release(MBID)
finally:
    if saved is None:
        del sys.modules["server.integrations"]
    else:
        sys.modules["server.integrations"] = saved

ok(release.get("date") == "1997-03-25", "the cached release carries the release's own date")
ok(release.get("originaldate") == "1980-10-01", "the cached release carries the group's first date")
ok(release.get("country") == "US",
   "the cached release keeps the singular country (MusicBrainz's first event)")
ok([c["code"] for c in release.get("countries") or []] == ["US", "CA", "XE"],
   "…and carries EVERY country event, earliest first "
   f"({[c['code'] for c in release.get('countries') or []]})")

# Every slot this pass fills EXCEPT the two dates, so the only thing an
# album can still gain below is a sharper date. The country holds the
# release's whole set, so it has nothing to gain either — the country rules
# are exercised on their own further down.
BASE = {
    "MUSICBRAINZ_ALBUMID": MBID, "MUSICBRAINZ_RELEASEGROUPID": "group-1",
    "MUSICBRAINZ_ALBUMARTISTID": "artist-1", "MUSICBRAINZ_ARTISTID": "artist-1",
    "MUSICBRAINZ_TRACKID": "recording-1",
    "ALBUM": "Remain in Light", "ALBUMARTIST": "Talking Heads",
    "TITLE": "Crosseyed and Painless", "DISCNUMBER": "1", "TRACKNUMBER": "1",
    "LABEL": "Sire Records", "CATALOGNUMBER": "6095-2",
    "RELEASECOUNTRY": "US; CA; XE",
    "RELEASETYPE": "Album", "MEDIA": "CD",
}
COARSE = dict(BASE, DATE="1997", ORIGINALDATE="1980")
FULL = dict(BASE, DATE="1997-03-25", ORIGINALDATE="1980-10-01")
CALLS.clear()

sys.modules["server.integrations"] = _mb_stub()
try:
    af = album_file("coarse", COARSE)
    written, note = autotag._fill_release_tags(
        [{"af": af}], cfg, os.path.dirname(af.path))
finally:
    if saved is None:
        del sys.modules["server.integrations"]
    else:
        sys.modules["server.integrations"] = saved

ok(len(CALLS) == 1, "an album whose dates are a bare year IS asked about")
ok(written == 2, f"exactly the two coarse dates were written ({written}: {note})")
ok(af.get_tag("DATE") == "1997-03-25",
   "a year-only release date is sharpened to MusicBrainz's full date")
ok(af.get_tag("ORIGINALDATE") == "1980-10-01",
   "a year-only original date is sharpened to the group's full first release date")

# The folder the naming script derives from those tags — the deliverable.
folder = eval_script(DEFAULT_NAMING_SCRIPT, track_variables(af.tags)).split("/")[1]
ok(folder.startswith("[Album] 1980-10-01 - 1997-03-25 - Remain in Light"),
   f"the album folder spells BOTH dates in full ({folder!r})")

# An album whose dates are already full costs no request at all.
CALLS.clear()
sys.modules["server.integrations"] = _mb_stub()
try:
    done = album_file("full", FULL)
    written, note = autotag._fill_release_tags(
        [{"af": done}], cfg, os.path.dirname(done.path))
finally:
    if saved is None:
        del sys.modules["server.integrations"]
    else:
        sys.modules["server.integrations"] = saved
ok(written == 0 and note == "release tags: nothing to fill" and not CALLS,
   f"a fully dated album writes nothing and asks MusicBrainz nothing ({note})")

# A tag that disagrees with MusicBrainz is another tagger's value: kept.
CALLS.clear()
sys.modules["server.integrations"] = _mb_stub()
try:
    other = album_file("contradiction", dict(COARSE, DATE="1975", ORIGINALDATE="1975-01-02"))
    autotag._fill_release_tags([{"af": other}], cfg, os.path.dirname(other.path))
finally:
    if saved is None:
        del sys.modules["server.integrations"]
    else:
        sys.modules["server.integrations"] = saved
ok(other.get_tag("DATE") == "1975" and other.get_tag("ORIGINALDATE") == "1975-01-02",
   "a date that contradicts MusicBrainz is never overwritten")


# --------------------------------------------------------------------------- #
# 3) RELEASECOUNTRY: the one release tag written as a LIST
# --------------------------------------------------------------------------- #
def fill(af, payload=MB_PAYLOAD):
    """Run one album through the real pass with the stub in place."""
    CALLS.clear()
    sys.modules["server.integrations"] = _mb_stub(CALLS, payload)
    try:
        return autotag._fill_release_tags([{"af": af}], cfg, os.path.dirname(af.path))
    finally:
        if saved is None:
            del sys.modules["server.integrations"]
        else:
            sys.modules["server.integrations"] = saved


# The release is out in US, CA and XE; the file says nothing about countries.
CALLS.clear()
sys.modules["server.integrations"] = _mb_stub()
try:
    blank = album_file("countries_blank",
                       {k: v for k, v in FULL.items() if k != "RELEASECOUNTRY"})
    written, note = autotag._fill_release_tags(
        [{"af": blank}], cfg, os.path.dirname(blank.path))
finally:
    if saved is None:
        del sys.modules["server.integrations"]
    else:
        sys.modules["server.integrations"] = saved
ok(written == 1 and blank.get_tag("RELEASECOUNTRY") == "US; CA; XE",
   "an empty country is filled with EVERY country the release states "
   f"({blank.get_tag('RELEASECOUNTRY')!r})")
ok(len(CALLS) == 1, "…from one release lookup")

# A file this app tagged before RELEASECOUNTRY could hold a list: one code,
# MusicBrainz's first event. It gains the rest of the release's set.
up = album_file("countries_upgrade", dict(FULL, RELEASECOUNTRY="US"))
written, note = fill(up)
ok(written == 1 and up.get_tag("RELEASECOUNTRY") == "US; CA; XE",
   f"a file holding one of the release's countries gains the rest "
   f"({up.get_tag('RELEASECOUNTRY')!r})")
ok(len(CALLS) == 1,
   "…and a single code is the one country case that costs a request")

# The code, not its spelling, decides: another tagger's "us" is the same
# country and is upgraded (and canonicalised) the same way.
lower = album_file("countries_lower", dict(FULL, RELEASECOUNTRY="us"))
written, note = fill(lower)
ok(written == 1 and lower.get_tag("RELEASECOUNTRY") == "US; CA; XE",
   "a lower-case code counts as that country and gains the rest "
   f"({lower.get_tag('RELEASECOUNTRY')!r})")

# A country the release does NOT state is somebody else's answer: kept, whole.
foreign = album_file("countries_foreign", dict(FULL, RELEASECOUNTRY="JP"))
written, note = fill(foreign)
ok(written == 0 and foreign.get_tag("RELEASECOUNTRY") == "JP",
   "a country the release does not state is never overwritten (no downgrade)")
listed = album_file("countries_list", dict(FULL, RELEASECOUNTRY="US; JP"))
written, note = fill(listed)
ok(written == 0 and listed.get_tag("RELEASECOUNTRY") == "US; JP" and not CALLS,
   "a LIST holding an unstated country keeps every value, and asks nothing")

# A release MusicBrainz states no country for: the tag is not touched either
# way — an empty slot stays empty rather than gaining a code nobody stated.
none_blank = album_file("countries_none_blank",
                        {k: v for k, v in FULL.items() if k != "RELEASECOUNTRY"})
written, note = fill(none_blank, MB_PAYLOAD_NO_COUNTRY)
ok(written == 0 and not none_blank.get_tag("RELEASECOUNTRY"),
   "a release stating no country writes none")
none_kept = album_file("countries_none_kept", dict(FULL, RELEASECOUNTRY="JP"))
written, note = fill(none_kept, MB_PAYLOAD_NO_COUNTRY)
ok(written == 0 and none_kept.get_tag("RELEASECOUNTRY") == "JP",
   "…and never touches a value that is already there")

# The naming script reads the FIRST value of a list (mlo.naming._first_multi,
# spec R33), so the code a folder segment is built from is the same one it was
# before the tag could hold more than one.
ok(track_variables(dict(blank.tags))["releasecountry"] == "US",
   "the naming variable is still MusicBrainz's first event, not the list")


# --------------------------------------------------------------------------- #
# 4) MP3: ORIGINALDATE lives in TDOR — the frame Picard and beets read
# --------------------------------------------------------------------------- #
# Minimal valid MPEG-1 Layer III frames, the fixture the other suites use.
_MP3_FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * 413


def make_mp3(path):
    with open(path, "wb") as f:
        f.write(_MP3_FRAME * 40)
    return path


from mutagen.id3 import ID3, TDRL  # noqa: E402  (after the sys.path shim)

mp3 = make_mp3(os.path.join(TMP, "1-01 Track.mp3"))
af = AudioFile(mp3)
ok(af.set_tag("ORIGINALDATE", "1980-10-01"), "ORIGINALDATE writes to an MP3")
frames = ID3(mp3)
ok(len(frames.getall("TDOR")) == 1 and not frames.getall("TDRL"),
   "the original date lands in TDOR (what Picard and beets read), not TDRL")
ok(str(af.get_tag("ORIGINALDATE") or "") == "1980-10-01", "and reads back")

# A file this app wrote BEFORE the spelling moved: TDRL is still read, and
# the next write moves it to TDOR instead of leaving two answers on disk.
legacy = make_mp3(os.path.join(TMP, "1-02 Legacy.mp3"))
from mutagen.mp3 import MP3  # noqa: E402

_legacy_audio = MP3(legacy)
_legacy_audio.add_tags()
_legacy_audio.tags.add(TDRL(encoding=3, text=["1980"]))
_legacy_audio.save()
af = AudioFile(legacy)
ok(str(af.get_tag("ORIGINALDATE") or "") == "1980", "a legacy TDRL value is still read")
af.set_tag("ORIGINALDATE", "1980-10-01")
frames = ID3(legacy)
ok(not frames.getall("TDRL") and len(frames.getall("TDOR")) == 1,
   "writing again replaces the legacy TDRL frame with TDOR")

# The country list through a REAL container, by the pass itself: every country
# the release states reaches the file and reads back ";"-joined, which is what
# the album page's badge and the naming script see.
CALLS.clear()
sys.modules["server.integrations"] = _mb_stub()
try:
    real = AudioFile(mp3)
    # The pass keys on the release id, the same way the album's own tags do.
    real.set_tag("MUSICBRAINZ_ALBUMID", MBID)
    autotag._fill_release_tags([{"af": real}], cfg, os.path.dirname(mp3))
finally:
    if saved is None:
        del sys.modules["server.integrations"]
    else:
        sys.modules["server.integrations"] = saved
ok(str(AudioFile(mp3).get_tag("RELEASECOUNTRY") or "") == "US; CA; XE",
   "the country list reaches a real MP3 and reads back '; '-joined "
   f"({AudioFile(mp3).get_tag('RELEASECOUNTRY')!r})")

# beets' own reader sees the date the app wrote (the vendored copy, when the
# checkout has it — CI has no .dependencies folder).
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
vendored = os.path.join(root, ".dependencies")
beets_dir = next((os.path.join(vendored, d) for d in sorted(os.listdir(vendored))
                  if d.lower().startswith("beets")), "") if os.path.isdir(vendored) else ""
if beets_dir and os.path.isdir(beets_dir):
    sys.path.insert(0, beets_dir)
    try:
        from mediafile import MediaFile
        music = MediaFile(mp3)
        got = music.original_date
        ok(got is not None and (got.year, got.month, got.day) == (1980, 10, 1),
           f"beets' mediafile reads the original date back ({got})")
    except Exception as e:  # noqa: BLE001 - absent/broken vendored copy: skip
        print(f"  skipped: beets mediafile check ({e})")
else:
    print("  skipped: beets mediafile check (no vendored beets here)")

# --------------------------------------------------------------------------- #
# 5) The pass leaves the FOLDER named from the tags it just settled
# --------------------------------------------------------------------------- #
# Script 8 writes the LAST of the naming script's own inputs — the chain's only
# namer (script 14's organize, then script 14 ends with another organize) runs
# BEFORE it, so a year-only date that is sharpened here leaves the folder
# spelling the stale tags' answer and script 4 (Grade, last) reports every file
# as `PATH: expected '<what these tags imply>'` (issue #48). The runner
# therefore re-applies the naming script to exactly the albums whose release
# tags it filled (`mlo.autotag._rename_to_script`) and reports where they are
# now as `moved_targets`, which is what the rest of the chain follows.
from server import main as srv_main  # noqa: E402

run_album = os.path.join(TMP, "runner")
os.makedirs(run_album, exist_ok=True)
run_files = []
for _i, _title in enumerate(("Mysterons", "Sour Times"), 1):
    _p = make_mp3(os.path.join(run_album, f"1-0{_i} {_title}.mp3"))
    run_files.append(_p)
    _af = AudioFile(_p)
    for _k, _v in dict(COARSE, MUSICBRAINZ_TRACKID=f"recording-{_i}").items():
        _af.set_tag(_k, _v)

# The album whose dates are already full: this pass writes NOTHING to it, so the
# namer must not be run over it (the rename is scoped to what changed).
done_album = os.path.join(TMP, "runner_done")
os.makedirs(done_album, exist_ok=True)
done_file = make_mp3(os.path.join(done_album, "1-01 Track.mp3"))
_done_af = AudioFile(done_file)
for _k, _v in FULL.items():
    _done_af.set_tag(_k, _v)

renamed_to = os.path.join(os.path.dirname(run_album), "renamed by the script")
organize_calls = []
_real_organize = srv_main.organize
import shutil  # noqa: E402


def _recording_organize(req):
    # The real organize MOVES the album to the name the script gives it, and
    # only a folder that holds audio is believed (see beetscfg._organized_roots)
    # — so the stub moves it too.
    organize_calls.append([os.path.normpath(p) for p in req.paths])
    os.makedirs(os.path.dirname(renamed_to), exist_ok=True)
    shutil.move(req.paths[0], renamed_to)
    return {"results": [{"path": req.paths[0], "album_root": renamed_to}]}


srv_main.organize = _recording_organize
CALLS.clear()
sys.modules["server.integrations"] = _mb_stub()
try:
    run_stats = autotag.run_auto_tagging(dict(
        cfg, music_folder=TMP, targets=run_files + [done_file],
        # only the release-identity stage is the subject: no audio decoding
        mood_enabled=False, auto_instrumental=False,
        instrumental_auto_fetch=False, auto_advisory=False,
        auto_zero_advisory_for_instrumental=False, genre_autofill=False))
finally:
    srv_main.organize = _real_organize
    if saved is None:
        del sys.modules["server.integrations"]
    else:
        sys.modules["server.integrations"] = saved

ok(run_stats["release_tags_written"] == 4,
   "the pass sharpened both coarse dates on both tracks "
   f"({run_stats['release_tags_written']} tags)")
eq_ok = organize_calls == [[os.path.normpath(run_album)]]
ok(eq_ok, "the namer was re-applied to the album it re-tagged, and NOT to the "
          f"album it wrote nothing to ({organize_calls})")
ok([os.path.normpath(p) for p in run_stats.get("moved_targets") or []]
   == [os.path.normpath(renamed_to)],
   "and the folder it now occupies is reported as moved_targets, so the rest of "
   f"the chain follows the album ({run_stats.get('moved_targets')})")
# The tags really moved: the folder the naming script derives from them is a
# different one than the tags as they arrived produced.
_arrived = eval_script(DEFAULT_NAMING_SCRIPT, track_variables(dict(COARSE)))
_now = eval_script(DEFAULT_NAMING_SCRIPT, track_variables(dict(COARSE, DATE="1997-03-25")))
ok(_arrived != _now,
   f"and those tags name another folder ({_arrived.split('/')[1]!r} -> "
   f"{_now.split('/')[1]!r})")

print(f"\n{passed} checks passed")