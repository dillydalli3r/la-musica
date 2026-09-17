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
from server.beetscfg import generate_config

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
lib = os.path.join(folder, "Artists")
os.makedirs(os.path.join(lib, "Artist", "2020 - Album"), exist_ok=True)
good = os.path.join(lib, "Artist", "2020 - Album", "1-01 Song.flac")
ok(_naming_mismatch(good, folder, DEFAULT_NAMING_SCRIPT, "", BASE_TAGS) == ("ok", None),
   "exact match below <music>/Artists returns ('ok', None)")
ok(_naming_mismatch(os.path.join(lib, "Wrong", "1-01 Song.flac"), folder,
                    DEFAULT_NAMING_SCRIPT, "", BASE_TAGS)
   == ("path", "Artist/2020 - Album/1-01 Song.flac"), "wrong folder returns expected path")
# the SAME relative layout one level up (music folder root) is not a match
ok(_naming_mismatch(os.path.join(folder, "Artist", "2020 - Album", "1-01 Song.flac"),
                    folder, DEFAULT_NAMING_SCRIPT, "", BASE_TAGS)[0] == "path",
   "library layout in the music folder root fails (base is Artists/)")

tags_m = dict(BASE_TAGS, MUSICBRAINZ_ALBUMARTISTID="12345678-1234-1234-1234-123456789abc")
# the default script folds the MBID into ONE folder segment: "Artist [uuid]"
full = os.path.join(lib, "Artist [12345678-1234-1234-1234-123456789abc]", "2020 - Album", "1-01 Song.flac")
short = os.path.join(lib, "Artist [12345678]", "2020 - Album", "1-01 Song.flac")
ok(_naming_mismatch(full, folder, DEFAULT_NAMING_SCRIPT, "", tags_m) == ("ok", None),
   "full MBID path matches")
ok(_naming_mismatch(short, folder, DEFAULT_NAMING_SCRIPT, "", tags_m) == ("ok", None),
   "short MBID accepted too (ID length can't cause false fails)")
ok(_naming_mismatch(full, folder, DEFAULT_NAMING_SCRIPT, "", BASE_TAGS)[0] == "path",
   "MBID folder mismatches when the tag is absent")
# RELEASETYPE feeds the script like the organizer does: "[album] 2020 - Album"
with_type = os.path.join(lib, "Artist [12345678-1234-1234-1234-123456789abc]", "[album] 2020 - Album", "1-01 Song.flac")
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
album = os.path.join(music, "Artists", "Artist", "2020 - Album")
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

# the naming-script layout one level up (music folder ROOT) must fail: the
# library root is <music>/Artists, the same base organize() moves into
root_dir = os.path.join(music, "Artist", "2020 - Album")
os.makedirs(root_dir, exist_ok=True)
shutil.copy(flac, os.path.join(root_dir, "1-01 Song.flac"))
res = _grade_album(root_dir, "EMBEDDED", cfg)
ok("PATH" in res["tracks"][0]["issues"],
   "unorganized album in the music folder root fails the naming check")

# album outside music_folder -> check skipped entirely
outside = tempfile.mkdtemp(prefix="mlo_outside_")
oalb = os.path.join(outside, "Someone", "Album")
os.makedirs(oalb, exist_ok=True)
shutil.copy(flac, os.path.join(oalb, "1-01 Song.flac"))
res = _grade_album(oalb, "EMBEDDED", cfg)
ok("PATH" not in res["tracks"][0]["issues"],
   "album outside music folder skips the naming check")

# ----------------------------------------------------------------------
# Case-only differences end-to-end (PATH_CASE)
# ----------------------------------------------------------------------
print("== case-only naming (PATH_CASE) ==")
# Every scenario gets its OWN music root: on a case-insensitive filesystem
# (Windows) "2020 - ALBUM" and "2020 - Album" are THE SAME directory, so a
# shared tree would silently reuse the correctly-cased folder (makedirs on an
# existing path is a no-op, and writing "1-01 song.flac" next to
# "1-01 Song.flac" reuses the existing file) — the check under test would
# never see a case difference at all.
EXPECTED_REL = "Artist/2020 - Album/1-01 Song.flac"


def case_scenario(tag, album_name, file_name):
    """Grade a fresh album whose folder/file names are spelled as given."""
    root = os.path.join(tmp, f"Case_{tag}", "Music")
    d = os.path.join(root, "Artists", "Artist", album_name)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, file_name)
    make_flac(p)
    set_tags(p, dict(BASE_TAGS, INITIALKEY="B♭ min", BPM="120"))
    return root, d, dict(ISO_CFG, music_folder=root, grade_check_filename_case=True)


good_root, good_dir, good_cfg = case_scenario("exact", "2020 - Album", "1-01 Song.flac")
good = _grade_album(good_dir, "EMBEDDED", good_cfg)
ok("PATH" not in good["tracks"][0]["issues"] and "PATH_CASE" not in good["tracks"][0]["issues"],
   "exact-case album passes both naming checks")
ok(good["total_checks"] - good["pass_count"] == 0,
   f"exact-case album fails no check ({good['pass_count']}/{good['total_checks']})")

# album DIRECTORY differs only in case
_bad_root, bad_dir, bad_cfg = case_scenario("dir", "2020 - ALBUM", "1-01 Song.flac")
res = _grade_album(bad_dir, "EMBEDDED", bad_cfg)
ok("PATH_CASE" in res["tracks"][0]["issues"],
   "wrong-case album folder fails the track (PATH_CASE)")
ok(any(i.startswith(f"PATH CASE: expected '{EXPECTED_REL}'") for i in res["issues"]),
   f"PATH CASE names the script's path (got {res['issues']})")
ok(res["total_checks"] == good["total_checks"] + 1
   and res["pass_count"] == good["pass_count"],
   f"wrong-case folder costs exactly one grade point ({res['pass_count']}/{res['total_checks']})")

# file NAME differs only in case
_fdir_root, fdir, fcfg = case_scenario("file", "2020 - Album", "1-01 song.flac")
res = _grade_album(fdir, "EMBEDDED", fcfg)
ok("PATH_CASE" in res["tracks"][0]["issues"],
   "wrong-case file name fails the track (PATH_CASE)")
ok(any(i.startswith(f"PATH CASE: expected '{EXPECTED_REL}'") for i in res["issues"]),
   f"PATH CASE names the script's path (got {res['issues']})")
ok(res["total_checks"] == good["total_checks"] + 1
   and res["pass_count"] == good["pass_count"],
   f"wrong-case file name costs exactly one grade point ({res['pass_count']}/{res['total_checks']})")

# The grade follows the case the DISK stores, never the caller's spelling:
# Windows resolves "2020 - Album" to a folder stored as "2020 - ALBUM", so a
# caller-supplied path used to decide the case verdict on its own. Only a
# case-INSENSITIVE filesystem reaches that code path at all — on Linux the
# other spelling is not a directory, so there is no caller spelling for the
# grader to be fooled by and nothing here to assert.
if os.path.exists(os.path.join(_bad_root, "Artists", "Artist", "2020 - Album")):
    res = _grade_album(os.path.join(_bad_root, "Artists", "Artist", "2020 - Album"),
                       "EMBEDDED", bad_cfg)
    ok("PATH_CASE" in res["tracks"][0]["issues"],
       "canonically-spelled caller path still reports the folder's real case")
    res = _grade_album(os.path.join(good_root, "Artists", "Artist", "2020 - album"),
                       "EMBEDDED", good_cfg)
    ok("PATH_CASE" not in res["tracks"][0]["issues"]
       and res["pass_count"] == res["total_checks"],
       "loosely-spelled caller path is not graded against its own spelling")

# switches: PATH_CASE has its own, PATH keeps its own
res = _grade_album(bad_dir, "EMBEDDED", dict(bad_cfg, grade_check_filename_case=False))
ok("PATH_CASE" not in res["tracks"][0]["issues"]
   and res["pass_count"] == res["total_checks"],
   "grade_check_filename_case=False suppresses PATH_CASE (check not counted)")
res = _grade_album(bad_dir, "EMBEDDED",
                   dict(bad_cfg, grade_check_naming=False, grade_check_filename_case=True))
ok("PATH" not in res["tracks"][0]["issues"]
   and "PATH_CASE" not in res["tracks"][0]["issues"]
   and res["pass_count"] == res["total_checks"],
   "grade_check_naming=False disables the whole naming block: no PATH, no "
   "PATH_CASE, check not counted")

# ----------------------------------------------------------------------
# beets config: directory: is the library root, not the music folder
# ----------------------------------------------------------------------
print("== beets config ==")
slashes = music.replace("\\", "/")
beets_txt = generate_config(dict(ISO_CFG, music_folder=music))
ok(f'directory: "{slashes}/Artists"' in beets_txt,
   "beets directory: points at <music>/Artists")
ok(f'directory: "{slashes}"\n' not in beets_txt,
   "beets directory: is not the music folder itself")

print(f"\nAll {passed} checks passed.")
shutil.rmtree(tmp, ignore_errors=True)
shutil.rmtree(outside, ignore_errors=True)
