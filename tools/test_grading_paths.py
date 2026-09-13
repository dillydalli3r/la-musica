#!/usr/bin/env python3
"""Verification for the path-grading (grade_check_naming) and Key/BPM
(grade_check_key_bpm) checks added to mlo/grader.py, plus the mlo.naming
evaluator move (server.naming re-exports it).

Run:  python tools/test_grading_paths.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo.grader import _grade_album, _naming_mismatch
from mlo.naming import DEFAULT_NAMING_SCRIPT

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

passed = 0


def ok(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok: {label}")


def make_flac(path):
    assert FLAC_EXE, "flac.exe not found under .dependencies"
    wav = path + ".wav"
    with wave.open(wav, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * 4410)
    subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", path, wav],
                   check=True, capture_output=True)
    os.remove(wav)


def set_tags(path, tags):
    from mutagen.flac import FLAC
    f = FLAC(path)
    for k, v in tags.items():
        f[k] = [v]
    f.save()


def del_tags(path, keys):
    from mutagen.flac import FLAC
    f = FLAC(path)
    for k in keys:
        if k in f:
            del f[k]
    f.save()


BASE_TAGS = {
    "TITLE": "Song",
    "ARTIST": "Artist",
    "ALBUMARTIST": "Artist",
    "ALBUM": "Album",
    "DATE": "2020",
    "TRACKNUMBER": "1",
    "DISCNUMBER": "1",
    "GENRE": "Test",
    "INSTRUMENTAL": "0",
}

# isolate the two new checks from everything else
ISO_CFG = {
    "music_folder": "",
    "grade_check_naming": True,
    "grade_check_key_bpm": True,
    "grade_check_missing_tags": False,
    "grade_check_lyrics": False,
    "grade_check_instrumental": False,
    "grade_check_cover": False,
    "grade_check_encoder": False,
    "grade_check_media": False,
    "grade_check_source": False,
    "grade_check_album_tags": False,
    "grade_check_unreadable": False,
    "grade_check_disallowed": False,
    "grade_check_sidecar_cover": False,
    "grade_check_ext_case": False,
    "grade_check_filename_case": False,
    "grade_check_excess_tags": False,
    "grade_check_audit": False,
    "grade_check_mb_links": False,
    "grade_check_rym_links": False,
}

tmp = tempfile.mkdtemp(prefix="mlo_naming_test_")

# ----------------------------------------------------------------------
# _naming_mismatch pure cases
# ----------------------------------------------------------------------
print("== _naming_mismatch ==")
folder = tempfile.mkdtemp(prefix="mlo_naming_pure_")
os.makedirs(os.path.join(folder, "Artist", "2020 - Album"), exist_ok=True)
good = os.path.join(folder, "Artist", "2020 - Album", "1-01 Song.flac")
ok(_naming_mismatch(good, folder, DEFAULT_NAMING_SCRIPT, "", BASE_TAGS) == ("ok", None),
   "exact match returns ('ok', None)")
ok(_naming_mismatch(os.path.join(folder, "Wrong", "1-01 Song.flac"), folder,
                    DEFAULT_NAMING_SCRIPT, "", BASE_TAGS)
   == ("path", "Artist/2020 - Album/1-01 Song.flac"), "wrong folder returns expected path")

tags_m = dict(BASE_TAGS, MUSICBRAINZ_ALBUMARTISTID="12345678-1234-1234-1234-123456789abc")
# the default script folds the MBID into ONE folder segment: "Artist [uuid]"
full = os.path.join(folder, "Artist [12345678-1234-1234-1234-123456789abc]", "2020 - Album", "1-01 Song.flac")
short = os.path.join(folder, "Artist [12345678]", "2020 - Album", "1-01 Song.flac")
ok(_naming_mismatch(full, folder, DEFAULT_NAMING_SCRIPT, "", tags_m) == ("ok", None),
   "full MBID path matches")
ok(_naming_mismatch(short, folder, DEFAULT_NAMING_SCRIPT, "", tags_m) == ("ok", None),
   "short MBID accepted too (ID length can't cause false fails)")
ok(_naming_mismatch(full, folder, DEFAULT_NAMING_SCRIPT, "", BASE_TAGS)[0] == "path",
   "MBID folder mismatches when the tag is absent")
# RELEASETYPE feeds the script like the organizer does: "[album] 2020 - Album"
with_type = os.path.join(folder, "Artist [12345678-1234-1234-1234-123456789abc]", "[album] 2020 - Album", "1-01 Song.flac")
ok(_naming_mismatch(with_type, folder, DEFAULT_NAMING_SCRIPT, "album", tags_m) == ("ok", None),
   "release type joins the album folder segment")
ok(_naming_mismatch(good.replace("2020 - Album", "2020 - ALBUM"), folder,
                    DEFAULT_NAMING_SCRIPT, "", BASE_TAGS)[0] == "case",
   "case-only difference reports 'case' (PATH_CASE check)")
shutil.rmtree(folder, ignore_errors=True)

# ----------------------------------------------------------------------
# End-to-end: _grade_album on a synthetic album
# ----------------------------------------------------------------------
print("== _grade_album naming + key/bpm ==")
music = os.path.join(tmp, "Music")
album = os.path.join(music, "Artist", "2020 - Album")
os.makedirs(album, exist_ok=True)
flac = os.path.join(album, "1-01 Song.flac")
make_flac(flac)
set_tags(flac, dict(BASE_TAGS, INITIALKEY="B♭ min", BPM="120"))

cfg = dict(ISO_CFG, music_folder=music)
res = _grade_album(album, "EMBEDDED", cfg)
tr = res["tracks"][0]
ok("PATH" not in tr["issues"], "organized path passes naming check")
ok("INITIALKEY" not in tr["issues"] and "BPM" not in tr["issues"], "present INITIALKEY/BPM pass")

# missing INITIALKEY -> issue
set_tags(flac, dict(BASE_TAGS, BPM="120"))
del_tags(flac, ["INITIALKEY"])
res = _grade_album(album, "EMBEDDED", cfg)
tr = res["tracks"][0]
ok("INITIALKEY" in tr["issues"], "missing INITIALKEY fails")
ok("PATH" not in tr["issues"], "naming still passes")

# wrong folder -> PATH issue with expected path
wrong_dir = os.path.join(music, "Wrong Place", "2020 - Album")
os.makedirs(wrong_dir, exist_ok=True)
wrong = os.path.join(wrong_dir, "1-01 Song.flac")
shutil.copy(flac, wrong)
res = _grade_album(wrong_dir, "EMBEDDED", cfg)
tr = res["tracks"][0]
ok("PATH" in tr["issues"], "misplaced file fails naming check")
ok(any(i.startswith("PATH: expected 'Artist/2020 - Album/1-01 Song.flac'") for i in res["issues"]),
   f"issue names the expected path (got {res['issues']})")

# check disabled -> no PATH issue
cfg_off = dict(cfg, grade_check_naming=False)
res = _grade_album(wrong_dir, "EMBEDDED", cfg_off)
ok("PATH" not in res["tracks"][0]["issues"],
   "grade_check_naming=False disables the check")

# album outside music_folder -> check skipped entirely
outside = tempfile.mkdtemp(prefix="mlo_outside_")
oalb = os.path.join(outside, "Someone", "Album")
os.makedirs(oalb, exist_ok=True)
shutil.copy(flac, os.path.join(oalb, "1-01 Song.flac"))
res = _grade_album(oalb, "EMBEDDED", cfg)
ok("PATH" not in res["tracks"][0]["issues"],
   "album outside music folder skips the naming check")

print(f"\nAll {passed} checks passed.")
shutil.rmtree(tmp, ignore_errors=True)
shutil.rmtree(outside, ignore_errors=True)
