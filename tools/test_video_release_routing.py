#!/usr/bin/env python3
"""Video releases: what the edition-ranking rule and the tag vocabulary say.

WHAT SURVIVES from the routing suite, after the download network was removed:

  * the MEDIA/SOURCE vocabularies know the video spellings — "Web" for a
    download-only video release and "YouTube" as a source tag, both canonical
    in `mlo.tagtext`;
  * the VIDEO-EDITION ranking rule (issue #67): MusicBrainz states which
    recordings an edition holds, and `media[].video` says a recording IS a
    music video — an edition whose recordings are ALL videos must never be the
    better pick while an edition carrying the album's own audio is on offer.
    The tier RANKS, it does not forbid: a group offering only the video edition
    still gets it, and a payload stating no media facts keeps its place
    (silence is not evidence).

No network, no downloads: fixtures only, with the music folder redirected so no
developer config is ever read.

Run:  python tools/test_video_release_routing.py   (exit 0 pass, 1 fail)
"""
import atexit
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# hermeticity: the app's paths resolve through the music folder the moment they
# are first touched, and `release_choice` reads the app config when no config
# is handed to it — so the scope is redirected BEFORE server.* is imported.
# --------------------------------------------------------------------------- #
REAL_MUSIC_FOLDER = ""
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        REAL_MUSIC_FOLDER = str((json.load(f) or {}).get("music_folder") or "")
except Exception:
    pass

REDIRECT = tempfile.mkdtemp(prefix="mlo-vidroute-redirect-")
MF = tempfile.mkdtemp(prefix="mlo-vidroute-music-")
os.environ["MLO_MUSIC_FOLDER"] = MF

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB = os.path.join(REDIRECT, "config.json")
with open(_STUB, "w", encoding="utf-8") as f:
    json.dump({"music_folder": MF}, f)
for _mod in (cfgmod, pathmod):
    _mod.CONFIG_FILE = _STUB
    if getattr(_mod, "LEGACY_DATA_DIR", None) is not None:
        _mod.LEGACY_DATA_DIR = os.path.join(REDIRECT, "legacy")

atexit.register(lambda: [shutil.rmtree(d, ignore_errors=True)
                         for d in (REDIRECT, MF)])
atexit.register(lambda: os.environ.pop("MLO_MUSIC_FOLDER", None))

REAL = REAL_MUSIC_FOLDER.replace("\\", "/").rstrip("/")
if REAL:
    assert not MF.replace("\\", "/").lower().startswith(REAL.lower()), \
        f"the temp fixture {MF} sits inside the real music folder {REAL}"

from mlo import release_choice as rc  # noqa: E402
from mlo import tagtext               # noqa: E402

FAILED = []


def ok(cond, label, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{f' — {extra}' if extra else ''}")
        FAILED.append(label)


def eq(got, want, label):
    ok(got == want, label, f"got {got!r}, want {want!r}")


# --------------------------------------------------------------------------- #
# the fixture: a release whose medium and recordings say what it is
# --------------------------------------------------------------------------- #
TITLES = ["First Video", "Second Video", "Third Video", "Fourth Video"]
MBID = "11111111-2222-3333-4444-555555555555"


def release(medium="Digital Media", tracks=3, video=True, mbid=MBID):
    return {
        "id": mbid,
        "title": "Video Singles",
        "date": "2004-05-01",
        "country": "US",
        "release_group_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "catalog_number": "", "barcode": "", "label": "",
        "status": "Official", "primary_type": "Album", "secondary_types": [],
        "release_type": "album",
        "artists": [{"name": "Test Artist", "mbid": "ffffffff-0000-1111-2222-333333333333"}],
        "medium_formats": [medium],
        "media": [{"position": i + 1, "disc": 1, "title": TITLES[i],
                   "length": 200000, "recording_mbid": f"rec-{i + 1:08d}",
                   "artist_credit": "Test Artist", "video": video}
                  for i in range(tracks)],
    }


# --------------------------------------------------------------------------- #
print("\n(1) the vocabularies know the video spellings")
# --------------------------------------------------------------------------- #
ok("YouTube" in tagtext.SOURCE_VALUES, "SOURCE_VALUES carries YouTube",
   tagtext.SOURCE_VALUES)
eq(tagtext.canonical_text("SOURCE", "youtube"), "YouTube",
   "the canonical spelling of a source tag written in any case")
ok("Web" in tagtext.MEDIA_VALUES, "MEDIA_VALUES knows the Web spelling",
   tagtext.MEDIA_VALUES)
eq(tagtext.canonical_text("MEDIA", "web"), "Web",
   "and canonicalises it like every other medium")

# --------------------------------------------------------------------------- #
print("\n(2) an edition of nothing but music videos ranks below the album's audio")
# --------------------------------------------------------------------------- #
# Issue #67: MusicBrainz states which recordings an edition holds — the flat
# `media` list a release lookup builds, or each disc's own `tracks` in a raw
# payload — and `media[].video` says a recording IS a music video. An edition
# whose recordings are ALL videos must never be the better pick while an
# edition carrying the album's own audio is on offer. The tier RANKS, it does
# not forbid: a group offering only the video edition still gets it.
vid = release("Digital Media", video=True, mbid="vid")
aud = release("CD", video=False, mbid="aud")
eq(rc.video_only(vid), True,
   "the video edition's own flat recordings say they are videos")
eq(rc.video_only(aud), False,
   "and the audio edition's say they are not")
eq(rc.choose_release({}, [vid, aud]).release_mbid, "aud",
   "so the audio edition is the pick")
ranked = {c.release_mbid: c for c in rc.rank_releases({}, [vid, aud])}
ok(any("video only" in r for r in ranked["vid"].reasons),
   "the video edition's own reasons say why it lost", ranked["vid"].reasons)
ok(any("the video edition rule" in r for r in ranked["aud"].reasons),
   "and the winner's deciding reason names the video tier", ranked["aud"].reasons)
eq(rc.choose_release({}, [vid]).release_mbid, "vid",
   "an only-video group still gets it — the rule ranks, it does not forbid")

# …and the video rule outranks the MEDIUM: a video edition on the album's own
# preferred medium (a CD) still loses to an audio edition published as a
# download, because this is the FIRST tier and no lower one may outvote it.
carrier_video = release("CD", video=True, mbid="cd-video")
download_audio = release("Digital Media", video=False, mbid="dig-aud")
eq(rc.choose_release({}, [carrier_video, download_audio]).release_mbid, "dig-aud",
   "a video CD loses to the album's audio on a worse medium")
eq(rc.choose_release({}, [download_audio, carrier_video]).release_mbid, "dig-aud",
   "whatever order the two arrived in")

# a payload stating NO media/video facts is not penalised: silence is not
# evidence, so an edition whose recordings the data does not describe keeps
# its place against an audio one instead of dropping a tier with it.
quiet = {"id": "quiet", "title": "Video Singles", "status": "Official",
         "date": "2004-05-01", "country": "US", "medium_formats": ["CD"]}
eq(rc.video_only(quiet), False, "a payload with no media facts is not video-only")
eq(rc.video_only({}), False, "and neither is an empty payload")
quiet_rows = rc.rank_releases({}, [quiet, release("CD", video=False, mbid="plain")])
ok(not any("video only" in r for c in quiet_rows for r in c.reasons),
   "neither edition is penalised on the video tier", [c.reasons for c in quiet_rows])
ok(not any("the video edition rule" in r for r in quiet_rows[0].reasons),
   "so the video tier is not what decided them", quiet_rows[0].reasons)
eq(rc.choose_release({}, [quiet, vid]).release_mbid, "quiet",
   "and the unstated edition still outranks a stated all-video one")

# the rule reads the NESTED disc shape too — a raw payload's `media` are
# DISCS, each with its own track list — and ONE audio recording among the
# videos is enough to clear the edition.
eq(rc.video_only({"media": [{"format": "DVD", "tracks": [{"video": True}]}]}), True,
   "a disc of nothing but video tracks is video-only")
eq(rc.video_only({"media": [{"format": "CD", "tracks": [{"video": False}]}]}), False,
   "a disc of audio tracks is not")
eq(rc.video_only({"media": [{"format": "DVD", "tracks": [{"video": True},
                                                         {"video": False}]}]}), False,
   "one audio recording among the videos clears the edition")
eq(rc.stated_recordings({"media": [{"format": "DVD", "tracks": [{"video": True},
                                                                 {"video": False}]}]}),
   [{"video": True}, {"video": False}],
   "stated_recordings flattens a disc's own track list")
eq(rc.stated_recordings(vid), vid["media"],
   "and reads the flat release-lookup shape as its own recordings")
eq(rc.stated_recordings({"media": [{"format": "CD", "tracks": []}]}), [],
   "an empty stated track list states no recordings")
eq(rc.stated_recordings({}), [],
   "and neither does a payload with no media at all")

# --------------------------------------------------------------------------- #
print()
if FAILED:
    print(f"video release vocabulary and ranking: {len(FAILED)} FAILED")
    for label in FAILED:
        print(f"  - {label}")
    sys.exit(1)
print("video release vocabulary and ranking: all assertions passed")
