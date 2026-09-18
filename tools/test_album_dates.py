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
    "label-info": [{"label": {"name": "Sire Records"}, "catalog-number": "6095-2"}],
    "artist-credit": [{"artist": {"id": "artist-1"}}],
    "media": [{"position": 1, "format": "CD",
               "tracks": [{"position": 1, "recording": {"id": "recording-1"},
                           "artist-credit": [{"artist": {"id": "artist-1"}}]}]}],
    "release-group": {"id": "group-1", "primary-type": "Album",
                      "secondary-types": [], "first-release-date": "1980-10-01"},
}
CALLS = []


def _mb_stub(calls=CALLS, payload=MB_PAYLOAD):
    """server.integrations.mb_get_cached, without server.integrations.

    The tag pass imports that one function, so stubbing the module name it
    looks up exercises the REAL `_cached_release` payload mapping (both
    dates) and the real write rule — no network, no HTTP client.
    """
    module = types.ModuleType("server.integrations")

    def mb_get_cached(entity, params=None):
        calls.append(entity)
        return payload

    module.mb_get_cached = mb_get_cached
    return module


class FakeAF:
    """A file's tag dict with mlo.audio.AudioFile's get/set contract."""

    def __init__(self, path, tags):
        self.path = path
        self.tags = dict(tags)

    def get_tag(self, name):
        return self.tags.get(name)

    def set_tag(self, name, value):
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

# Every slot this pass fills EXCEPT the two dates, so the only thing an
# album can still gain below is a sharper date.
BASE = {
    "MUSICBRAINZ_ALBUMID": MBID, "MUSICBRAINZ_RELEASEGROUPID": "group-1",
    "MUSICBRAINZ_ALBUMARTISTID": "artist-1", "MUSICBRAINZ_ARTISTID": "artist-1",
    "MUSICBRAINZ_TRACKID": "recording-1",
    "ALBUM": "Remain in Light", "ALBUMARTIST": "Talking Heads",
    "TITLE": "Crosseyed and Painless", "DISCNUMBER": "1", "TRACKNUMBER": "1",
    "LABEL": "Sire Records", "CATALOGNUMBER": "6095-2", "RELEASECOUNTRY": "US",
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
# 3) MP3: ORIGINALDATE lives in TDOR — the frame Picard and beets read
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

print(f"\n{passed} checks passed")
