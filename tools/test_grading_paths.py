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

from mlo.grader import (REPLAYGAIN_TAGS, _grade_album, _naming_mismatch,
                        _release_type_candidates, tag_key_allowed)
from mlo.naming import (DEFAULT_NAMING_SCRIPT, UNKNOWN_RELEASE_TYPE,
                        eval_script, track_variables)
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
    # Checks added after this file was written: off here (the fixtures carry
    # no MOOD / ReplayGain tags and no description.txt) and switched on in
    # the dedicated cases below.
    "grade_check_mood": False,
    "grade_check_energy": False,
    "grade_check_replaygain": False,
    "grade_check_acoustid": False,
    "grade_check_album_description": False,
}

tmp = tempfile.mkdtemp(prefix="mlo_naming_test_")

# ----------------------------------------------------------------------
# _naming_mismatch pure cases
# ----------------------------------------------------------------------
print("== _naming_mismatch ==")
folder = tempfile.mkdtemp(prefix="mlo_naming_pure_")
lib = os.path.join(folder, "Artists")
os.makedirs(os.path.join(lib, "Artist", "Album (2020)"), exist_ok=True)
good = os.path.join(lib, "Artist", "Album (2020)", "1-01 Song.flac")
ok(_naming_mismatch(good, folder, DEFAULT_NAMING_SCRIPT, "", BASE_TAGS) == ("ok", None),
   "exact match below <music>/Artists returns ('ok', None)")
ok(_naming_mismatch(os.path.join(lib, "Wrong", "1-01 Song.flac"), folder,
                    DEFAULT_NAMING_SCRIPT, "", BASE_TAGS)
   == ("path", "Artist/Album (2020)/1-01 Song.flac"),
   "wrong folder returns expected path")
# the SAME relative layout one level up (music folder root) is not a match
ok(_naming_mismatch(os.path.join(folder, "Artist", "Album (2020)", "1-01 Song.flac"),
                    folder, DEFAULT_NAMING_SCRIPT, "", BASE_TAGS)[0] == "path",
   "library layout in the music folder root fails (base is Artists/)")
# label / country / release type land in the folder segment exactly as the
# script spells them, and a missing one drops its segment (no dangling [])
rich = dict(BASE_TAGS, RELEASETYPE="Album", LABEL="Label", RELEASECOUNTRY="US")
rich_path = os.path.join(lib, "Artist", "Album (Album, 2020) [Label] [US]",
                         "1-01 Song.flac")
ok(_naming_mismatch(rich_path, folder, DEFAULT_NAMING_SCRIPT, "", rich) == ("ok", None),
   "release type, label and country join the folder segment")
ok(_naming_mismatch(good, folder, DEFAULT_NAMING_SCRIPT, "", rich)[0] == "path",
   "dropping them from the folder fails (script defines the path)")
ok(eval_script(DEFAULT_NAMING_SCRIPT, track_variables(BASE_TAGS)).count("[") == 0
   and "/Album (2020)/" in eval_script(DEFAULT_NAMING_SCRIPT, track_variables(BASE_TAGS)),
   "missing label/country/type leave NO dangling brackets")

tags_m = dict(BASE_TAGS, MUSICBRAINZ_ALBUMARTISTID="12345678-1234-1234-1234-123456789abc")
# the default script folds the MBID into ONE folder segment: "Artist [uuid]"
full = os.path.join(lib, "Artist [12345678-1234-1234-1234-123456789abc]",
                    "Album (2020)", "1-01 Song.flac")
short = os.path.join(lib, "Artist [12345678]", "Album (2020)", "1-01 Song.flac")
ok(_naming_mismatch(full, folder, DEFAULT_NAMING_SCRIPT, "", tags_m) == ("ok", None),
   "full MBID path matches")
ok(_naming_mismatch(short, folder, DEFAULT_NAMING_SCRIPT, "", tags_m) == ("ok", None),
   "short MBID accepted too (ID length can't cause false fails)")
ok(_naming_mismatch(full, folder, DEFAULT_NAMING_SCRIPT, "", BASE_TAGS)[0] == "path",
   "MBID folder mismatches when the tag is absent")
# RELEASETYPE feeds the script like the organizer does — "[album] 2020 - Album"
# became "Album (album, 2020)" in the shipped default
with_type = os.path.join(lib, "Artist [12345678-1234-1234-1234-123456789abc]",
                         "Album (album, 2020)", "1-01 Song.flac")
ok(_naming_mismatch(with_type, folder, DEFAULT_NAMING_SCRIPT, "album", tags_m) == ("ok", None),
   "a lowercase release type joins the album folder segment")
ok(_naming_mismatch(with_type.replace("(album,", "(Album; Live,"), folder,
                    DEFAULT_NAMING_SCRIPT, "album+live", tags_m) == ("ok", None),
   "the same type in MusicBrainz casing matches too (no false fail)")
ok(_naming_mismatch(good.replace("Album (2020)", "ALBUM (2020)"), folder,
                    DEFAULT_NAMING_SCRIPT, "", BASE_TAGS)[0] == "case",
   "case-only difference reports 'case' (PATH_CASE check)")
shutil.rmtree(folder, ignore_errors=True)

# ----------------------------------------------------------------------
# End-to-end: _grade_album on a synthetic album
# ----------------------------------------------------------------------
print("== _grade_album naming + key/bpm ==")
music = os.path.join(tmp, "Music")
# The folder is spelled the way the shipped script lays it out when the
# RELEASETYPE tag is present ("%album% (%releasetype%, %year%)"), so the
# sections that assert a CLEAN album (no failed check at all) satisfy the
# grader's identity-tag check too. Anything graded with the tag absent still
# matches through the wildcard.
album = os.path.join(music, "Artists", "Artist", "Album (Album, 2020)")
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
wrong_dir = os.path.join(music, "Wrong Place", "Album (2020)")
os.makedirs(wrong_dir, exist_ok=True)
wrong = os.path.join(wrong_dir, "1-01 Song.flac")
shutil.copy(flac, wrong)
res = _grade_album(wrong_dir, "EMBEDDED", cfg)
tr = res["tracks"][0]
ok("PATH" in tr["issues"], "misplaced file fails naming check")
# The expected path is what the SHIPPED script makes of these tags — the same
# evaluation the grader runs — never a hand-typed literal (the default layout
# is free to change). No RELEASETYPE tag is present, so the grader wildcards
# that token as '?' and reports the missing tag separately.
_mis_kind, _mis_expected = _naming_mismatch(
    wrong, music, DEFAULT_NAMING_SCRIPT, UNKNOWN_RELEASE_TYPE,
    dict(BASE_TAGS, BPM="120"))
ok(_mis_kind == "path", f"misplaced file is a real mismatch (got {_mis_kind})")
ok([i for i in res["issues"] if i.startswith("PATH")] ==
   [f"PATH: expected '{_mis_expected}'"],
   f"the PATH issue names the script's expected path (got {res['issues']})")

# check disabled -> no PATH issue
cfg_off = dict(cfg, grade_check_naming=False)
res = _grade_album(wrong_dir, "EMBEDDED", cfg_off)
ok("PATH" not in res["tracks"][0]["issues"],
   "grade_check_naming=False disables the check")

# the naming-script layout one level up (music folder ROOT) must fail: the
# library root is <music>/Artists, the same base organize() moves into
root_dir = os.path.join(music, "Artist", "Album (2020)")
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
# (Windows) the two spellings are THE SAME directory, so a shared tree would
# silently reuse the correctly-cased folder (makedirs on an existing path is a
# no-op, and writing "1-01 song.flac" next to "1-01 Song.flac" reuses the
# existing file) — the check under test would never see a case difference at
# all.
#
# The folder carries the release type, exactly as the shipped script spells it
# ("%album% (%releasetype%, %year%)") with the RELEASETYPE tag present — the
# grader's identity-tag check demands that tag, so a clean album states it.
ALBUM_DIR = "Album (Album, 2020)"
BAD_ALBUM_DIR = "ALBUM (ALBUM, 2020)"
EXPECTED_REL = f"Artist/{ALBUM_DIR}/1-01 Song.flac"


def case_scenario(tag, album_name, file_name):
    """Grade a fresh album whose folder/file names are spelled as given."""
    root = os.path.join(tmp, f"Case_{tag}", "Music")
    d = os.path.join(root, "Artists", "Artist", album_name)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, file_name)
    make_flac(p)
    set_tags(p, dict(BASE_TAGS, RELEASETYPE="Album",
                     INITIALKEY="B♭ min", BPM="120"))
    return root, d, dict(ISO_CFG, music_folder=root, grade_check_filename_case=True)


good_root, good_dir, good_cfg = case_scenario("exact", ALBUM_DIR, "1-01 Song.flac")
good = _grade_album(good_dir, "EMBEDDED", good_cfg)
ok("PATH" not in good["tracks"][0]["issues"] and "PATH_CASE" not in good["tracks"][0]["issues"],
   "exact-case album passes both naming checks")
ok(good["total_checks"] - good["pass_count"] == 0,
   f"exact-case album fails no check ({good['pass_count']}/{good['total_checks']})")

# album DIRECTORY differs only in case
_bad_root, bad_dir, bad_cfg = case_scenario("dir", BAD_ALBUM_DIR, "1-01 Song.flac")
res = _grade_album(bad_dir, "EMBEDDED", bad_cfg)
ok("PATH_CASE" in res["tracks"][0]["issues"],
   "wrong-case album folder fails the track (PATH_CASE)")
ok(any(i.startswith(f"PATH CASE: expected '{EXPECTED_REL}'") for i in res["issues"]),
   f"PATH CASE names the script's path (got {res['issues']})")
ok(res["total_checks"] == good["total_checks"] + 1
   and res["pass_count"] == good["pass_count"],
   f"wrong-case folder costs exactly one grade point ({res['pass_count']}/{res['total_checks']})")

# file NAME differs only in case
_fdir_root, fdir, fcfg = case_scenario("file", ALBUM_DIR, "1-01 song.flac")
res = _grade_album(fdir, "EMBEDDED", fcfg)
ok("PATH_CASE" in res["tracks"][0]["issues"],
   "wrong-case file name fails the track (PATH_CASE)")
ok(any(i.startswith(f"PATH CASE: expected '{EXPECTED_REL}'") for i in res["issues"]),
   f"PATH CASE names the script's path (got {res['issues']})")
ok(res["total_checks"] == good["total_checks"] + 1
   and res["pass_count"] == good["pass_count"],
   f"wrong-case file name costs exactly one grade point ({res['pass_count']}/{res['total_checks']})")

# The grade follows the case the DISK stores, never the caller's spelling:
# Windows resolves the correct spelling to a folder stored in caps, so a
# caller-supplied path used to decide the case verdict on its own. Only a
# case-INSENSITIVE filesystem reaches that code path at all — on Linux the
# other spelling is not a directory, so there is no caller spelling for the
# grader to be fooled by and nothing here to assert.
if os.path.exists(os.path.join(_bad_root, "Artists", "Artist", ALBUM_DIR)):
    res = _grade_album(os.path.join(_bad_root, "Artists", "Artist", ALBUM_DIR),
                       "EMBEDDED", bad_cfg)
    ok("PATH_CASE" in res["tracks"][0]["issues"],
       "canonically-spelled caller path still reports the folder's real case")
    res = _grade_album(os.path.join(good_root, "Artists", "Artist", BAD_ALBUM_DIR),
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
# Mood & genre presence (grade_check_mood / grade_check_genre)
# ----------------------------------------------------------------------
print("== mood / genre presence ==")
# Clean fixture in the album graded above: everything ISO_CFG isolates is
# satisfied, MOOD deliberately absent.
NO_MOOD = dict(BASE_TAGS, RELEASETYPE="Album", INITIALKEY="B♭ min", BPM="120")
set_tags(flac, NO_MOOD)
del_tags(flac, ["MOOD"])
mood_cfg = dict(cfg, grade_check_mood=True, grade_check_genre=True)
res = _grade_album(album, "EMBEDDED", mood_cfg)
tr = res["tracks"][0]
ok(tr["issues"] == ["MOOD_MISSING"],
   f"GENRE present + MOOD missing fails only the mood check (got {tr['issues']})")
ok(res["total_checks"] - res["pass_count"] == 1,
   f"the missing MOOD costs exactly one grade point "
   f"({res['pass_count']}/{res['total_checks']})")
ok("Missing MOOD" in res["issues"], f"album issue names the tag (got {res['issues']})")

set_tags(flac, dict(NO_MOOD, MOOD="melancholic"))
res = _grade_album(album, "EMBEDDED", mood_cfg)
ok(res["pass_count"] == res["total_checks"],
   f"a MOOD tag clears the check ({res['pass_count']}/{res['total_checks']})")

del_tags(flac, ["GENRE"])
res = _grade_album(album, "EMBEDDED", mood_cfg)
ok(res["tracks"][0]["issues"] == ["GENRE_MISSING"],
   f"missing GENRE fails with its own code (got {res['tracks'][0]['issues']})")
ok("Missing GENRE" in res["issues"], f"album issue names GENRE (got {res['issues']})")

res = _grade_album(album, "EMBEDDED", dict(mood_cfg, grade_check_genre=False))
ok("GENRE_MISSING" not in res["tracks"][0]["issues"]
   and res["pass_count"] == res["total_checks"],
   "grade_check_genre=False stops grading the missing GENRE")
res = _grade_album(album, "EMBEDDED", dict(mood_cfg, grade_check_mood=False))
ok("GENRE_MISSING" in res["tracks"][0]["issues"]
   and "MOOD_MISSING" not in res["tracks"][0]["issues"],
   "the two toggles are independent (genre on, mood off)")
set_tags(flac, dict(NO_MOOD, MOOD="melancholic"))

# ----------------------------------------------------------------------
# ReplayGain family (grade_check_replaygain) — opt-in like AcoustID
# ----------------------------------------------------------------------
print("== replaygain presence ==")
# No RG tags at all: not graded, not counted — a library that never ran
# script 7 (the player measures on the fly) must not be failed.
del_tags(flac, list(REPLAYGAIN_TAGS))
rg_base = _grade_album(album, "EMBEDDED", cfg)
res = _grade_album(album, "EMBEDDED", dict(cfg, grade_check_replaygain=True))
ok(res["total_checks"] == rg_base["total_checks"]
   and res["pass_count"] == res["total_checks"],
   f"a file with no ReplayGain tags is never graded for them "
   f"({res['pass_count']}/{res['total_checks']})")

# ... but a half-written set is: the missing three fail, the present one passes.
set_tags(flac, dict(NO_MOOD, MOOD="melancholic",
                    REPLAYGAIN_TRACK_GAIN="-3.00 dB"))
rg_cfg = dict(cfg, grade_check_replaygain=True)
res = _grade_album(album, "EMBEDDED", rg_cfg)
ok(sorted(res["tracks"][0]["issues"])
   == ["REPLAYGAIN_ALBUM_GAIN", "REPLAYGAIN_ALBUM_PEAK",
       "REPLAYGAIN_TRACK_PEAK"],
   f"a lone REPLAYGAIN_TRACK_GAIN fails the other three "
   f"(got {res['tracks'][0]['issues']})")
ok(res["total_checks"] - res["pass_count"] == 3,
   f"the incomplete set costs exactly three grade points "
   f"({res['pass_count']}/{res['total_checks']})")
# a complete set is graded and passes (proves the family IS counted once one
# tag exists, i.e. the opt-in skip does not disable the check wholesale)
set_tags(flac, dict(NO_MOOD, MOOD="melancholic",
                    REPLAYGAIN_TRACK_GAIN="-3.00 dB",
                    REPLAYGAIN_TRACK_PEAK="0.98",
                    REPLAYGAIN_ALBUM_GAIN="-2.50 dB",
                    REPLAYGAIN_ALBUM_PEAK="1.00"))
res = _grade_album(album, "EMBEDDED", rg_cfg)
ok(res["total_checks"] == rg_base["total_checks"] + 4
   and res["pass_count"] == res["total_checks"],
   f"a complete ReplayGain set is graded and passes "
   f"({res['pass_count']}/{res['total_checks']})")

# toggle off: even a half-written set is not graded (nor counted)
del_tags(flac, list(REPLAYGAIN_TAGS))
set_tags(flac, dict(NO_MOOD, MOOD="melancholic",
                    REPLAYGAIN_TRACK_GAIN="-3.00 dB"))
res = _grade_album(album, "EMBEDDED", cfg)
ok(res["total_checks"] == rg_base["total_checks"]
   and not any("REPLAYGAIN" in i for i in res["tracks"][0]["issues"]),
   f"grade_check_replaygain=False does not grade them "
   f"({res['pass_count']}/{res['total_checks']})")
# both toggles on: exactly one penalty per missing RG tag (the generic sweep
# still owns DYNAMIC RANGE, which this fixture does not carry either)
res = _grade_album(album, "EMBEDDED",
                   dict(cfg, grade_check_replaygain=True,
                        grade_check_missing_tags=True))
_rg_issues = [i for i in res["tracks"][0]["issues"]
              if i.startswith("REPLAYGAIN")]
ok(res["total_checks"] == rg_base["total_checks"] + 4
   and _rg_issues == ["REPLAYGAIN_TRACK_PEAK", "REPLAYGAIN_ALBUM_GAIN",
                      "REPLAYGAIN_ALBUM_PEAK"]
   and res["tracks"][0]["issues"].count("DYNAMIC RANGE") == 1,
   f"missing_tags cannot double-penalize the family "
   f"({res['pass_count']}/{res['total_checks']}, "
   f"{res['tracks'][0]['issues']})")
set_tags(flac, dict(NO_MOOD, MOOD="melancholic"))
del_tags(flac, list(REPLAYGAIN_TAGS))

# ----------------------------------------------------------------------
# AcoustID pair (opt-in: never graded when the file carries neither tag)
# ----------------------------------------------------------------------
print("== acoustid pair ==")
base = _grade_album(album, "EMBEDDED", cfg)
res = _grade_album(album, "EMBEDDED", dict(cfg, grade_check_acoustid=True))
ok(res["total_checks"] == base["total_checks"]
   and res["pass_count"] == res["total_checks"],
   "a file with neither AcoustID tag is never graded (check not counted)")
set_tags(flac, dict(NO_MOOD, MOOD="melancholic", ACOUSTID_ID="9f4e1d2c"))
res = _grade_album(album, "EMBEDDED", dict(cfg, grade_check_acoustid=True))
ok(res["tracks"][0]["issues"] == ["ACOUSTID_FINGERPRINT"],
   f"a lone ACOUSTID_ID fails the pair check (got {res['tracks'][0]['issues']})")
set_tags(flac, dict(NO_MOOD, MOOD="melancholic", ACOUSTID_ID="9f4e1d2c",
                    ACOUSTID_FINGERPRINT="AQADtEmS"))
res = _grade_album(album, "EMBEDDED", dict(cfg, grade_check_acoustid=True))
ok(res["pass_count"] == res["total_checks"],
   f"a complete AcoustID pair passes ({res['pass_count']}/{res['total_checks']})")
set_tags(flac, dict(NO_MOOD, MOOD="melancholic"))

# ----------------------------------------------------------------------
# Album description (grade_check_album_description)
# ----------------------------------------------------------------------
print("== album description ==")
desc_cfg = dict(cfg, grade_check_album_description=True)
res = _grade_album(album, "EMBEDDED", desc_cfg)
ok(any("Album description missing" in i for i in res["issues"]),
   f"an album without description.txt fails the check (got {res['issues']})")
ok(res["total_checks"] - res["pass_count"] == 1,
   f"the missing description costs one grade point "
   f"({res['pass_count']}/{res['total_checks']})")
desc = os.path.join(album, "description.txt")
with open(desc, "w", encoding="utf-8") as fh:
    fh.write("Recorded in a shed, 1997.\n")
res = _grade_album(album, "EMBEDDED", desc_cfg)
ok(res["pass_count"] == res["total_checks"],
   f"a non-blank description.txt passes ({res['pass_count']}/{res['total_checks']})")
with open(desc, "w", encoding="utf-8") as fh:
    fh.write("   \n\n")
res = _grade_album(album, "EMBEDDED", desc_cfg)
ok(res["total_checks"] - res["pass_count"] == 1,
   "a blank description.txt does not count as a description")
with open(desc, "w", encoding="utf-8") as fh:
    fh.write("Recorded in a shed, 1997.\n")
# ... and the file itself is legitimate library content, not a stray
strict_cfg = dict(cfg, grade_check_album_description=True,
                  grade_check_disallowed=True, grade_check_extra_images=True)
res = _grade_album(album, "EMBEDDED", strict_cfg)
ok(not any("Disallowed" in i for i in res["issues"]),
   f"description.txt is not a disallowed file type (got {res['issues']})")
ok(not any("Extra artwork" in i for i in res["issues"]),
   f"and not stray artwork either (got {res['issues']})")
ok(res["pass_count"] == res["total_checks"],
   f"description.txt costs no grade points ({res['pass_count']}/{res['total_checks']})")

# ----------------------------------------------------------------------
# Release type: absent tag, spelling variants (no live MusicBrainz call)
# ----------------------------------------------------------------------
print("== release type ==")
ok(_release_type_candidates("") == [""], "an absent release type has one candidate")
ok(_release_type_candidates("album+live") == ["album+live", "Album; Live"],
   f"the lowercase form also tries MusicBrainz casing "
   f"({_release_type_candidates('album+live')})")
ok(_release_type_candidates("Album; Live") == ["Album; Live", "album+live"],
   f"and the capped form tries the lookup spelling "
   f"({_release_type_candidates('Album; Live')})")
ok(_release_type_candidates("ep") == ["ep", "EP"], "EP keeps its MusicBrainz casing")
ok(_release_type_candidates(UNKNOWN_RELEASE_TYPE)[0] == UNKNOWN_RELEASE_TYPE
   and "" in _release_type_candidates(UNKNOWN_RELEASE_TYPE),
   "an unknown type also tries the no-type layout")

# an album the organizer laid out ONLINE, graded with no tag and no network:
# the folder is still accepted (the type token is a wildcard) and the missing
# tag is REPORTED instead of turning into a bogus PATH failure.
rt_dir = os.path.join(music, "Artists", "Artist", "Album (album, 2020)")
os.makedirs(rt_dir, exist_ok=True)
rt_flac = os.path.join(rt_dir, "1-01 Song.flac")
make_flac(rt_flac)
set_tags(rt_flac, dict(BASE_TAGS, INITIALKEY="B♭ min", BPM="120"))
res = _grade_album(rt_dir, "EMBEDDED", cfg)
ok("PATH" not in res["tracks"][0]["issues"],
   f"no RELEASETYPE tag + no MusicBrainz call is not a path failure "
   f"({res['tracks'][0]['issues']})")
ok(any("Missing RELEASETYPE" in i for i in res["issues"]),
   f"the missing tag is graded instead (got {res['issues']})")
ok(res["total_checks"] - res["pass_count"] == 1,
   f"and costs exactly one grade point ({res['pass_count']}/{res['total_checks']})")
# ... but the rest of the layout is still verified: a wrong album name fails
rt_wrong = os.path.join(music, "Artists", "Artist", "Amnesiac (2020)")
os.makedirs(rt_wrong, exist_ok=True)
shutil.copy(rt_flac, os.path.join(rt_wrong, "1-01 Song.flac"))
res = _grade_album(rt_wrong, "EMBEDDED", cfg)
ok("PATH" in res["tracks"][0]["issues"],
   "an unknown type does not excuse a wrong album name")
# an album organized under the OTHER spelling of the same type also matches
rt_capped = os.path.join(music, "Artists", "Artist", "Album (Album; Live, 2020)")
os.makedirs(rt_capped, exist_ok=True)
shutil.copy(rt_flac, os.path.join(rt_capped, "1-01 Song.flac"))
set_tags(os.path.join(rt_capped, "1-01 Song.flac"),
         dict(BASE_TAGS, INITIALKEY="B♭ min", BPM="120", RELEASETYPE="album+live"))
res = _grade_album(rt_capped, "EMBEDDED", cfg)
ok("PATH" not in res["tracks"][0]["issues"],
   f"a capped folder matches a lookup-spelled tag ({res['tracks'][0]['issues']})")

# ----------------------------------------------------------------------
# Multi-country / multi-label naming: FIRST value wins, deterministically
# ----------------------------------------------------------------------
print("== multi-country naming ==")
ok(track_variables(dict(BASE_TAGS, RELEASECOUNTRY="US; GB"))["releasecountry"] == "US",
   "'; ' keeps the first country")
ok(track_variables(dict(BASE_TAGS, RELEASECOUNTRY="US+GB"))["releasecountry"] == "US",
   "'+' keeps the first country")
ok(track_variables(dict(BASE_TAGS, RELEASECOUNTRY="EU / UK"))["releasecountry"] == "EU",
   "' / ' keeps the first country")
ok(track_variables(dict(BASE_TAGS, COUNTRY="JP"))["releasecountry"] == "JP",
   "beets' COUNTRY spelling feeds %releasecountry%")
ok(track_variables(dict(BASE_TAGS, LABEL="Label A + Label B"))["label"] == "Label A",
   "a multi-label release keeps the first label")
mc_vars = track_variables(dict(BASE_TAGS, RELEASECOUNTRY="US; GB",
                               LABEL="Label A + Label B"))
ok(eval_script(DEFAULT_NAMING_SCRIPT, mc_vars)
   == eval_script(DEFAULT_NAMING_SCRIPT,
                  track_variables(dict(BASE_TAGS, RELEASECOUNTRY="US",
                                       LABEL="Label A"))),
   "a multi-value album produces the SINGLE-value path (stable, not arbitrary)")

mc_dir = os.path.join(music, "Artists", "Artist", "Album (Album, 2020) [Label A] [US]")
os.makedirs(mc_dir, exist_ok=True)
mc_flac = os.path.join(mc_dir, "1-01 Song.flac")
make_flac(mc_flac)
for sep in ("; ", " / ", "+"):
    # every separator spelling of the SAME countries yields the SAME path
    set_tags(mc_flac, dict(BASE_TAGS, INITIALKEY="B♭ min", BPM="120",
                           RELEASETYPE="Album",
                           RELEASECOUNTRY="US" + sep + "GB",
                           LABEL="Label A" + sep + "Label B"))
    res = _grade_album(mc_dir, "EMBEDDED", cfg)
    ok("PATH" not in res["tracks"][0]["issues"],
       f"multi-value tags joined with {sep!r} still produce this path")
    ok(res["pass_count"] == res["total_checks"],
       f"and cost no grade point ({res['pass_count']}/{res['total_checks']})")
set_tags(mc_flac, dict(BASE_TAGS, INITIALKEY="B♭ min", BPM="120",
                       RELEASETYPE="Album",
                       RELEASECOUNTRY="GB; US", LABEL="Label A + Label B"))
res = _grade_album(mc_dir, "EMBEDDED", cfg)
ok("PATH" in res["tracks"][0]["issues"],
   "swapping the country order CHANGES the path — the rule is first-wins, "
   "not 'any of them'")

# ----------------------------------------------------------------------
# Identity tags + ENERGY presence (grade_check_missing_tags / _energy)
# ----------------------------------------------------------------------
print("== identity tags + energy ==")


def fresh_album(name, tags):
    """A one-track album under the shared music root, named as given."""
    d = os.path.join(music, "Artists", "Artist", name)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "1-01 Song.flac")
    make_flac(p)
    set_tags(p, tags)
    return d, p


FULL = dict(BASE_TAGS, INITIALKEY="B♭ min", BPM="120", MOOD="melancholic",
            ENERGY="50", **{"DYNAMIC RANGE": "8"})
# No naming check here: every presence case is graded on the tags alone, so a
# deleted TITLE cannot also fail the path %title% feeds.
pres_cfg = dict(ISO_CFG, music_folder="", grade_check_naming=False,
                grade_check_missing_tags=True,
                grade_check_genre=True, grade_check_mood=True,
                grade_check_energy=True, grade_check_key_bpm=False)
pres_dir, pres_flac = fresh_album("Album (2020)", FULL)
res = _grade_album(pres_dir, "EMBEDDED", pres_cfg)
ok(res["pass_count"] == res["total_checks"],
   f"a fully tagged track passes every presence check "
   f"({res['pass_count']}/{res['total_checks']}, {res['tracks'][0]['issues']})")

del_tags(pres_flac, ["ENERGY"])
res = _grade_album(pres_dir, "EMBEDDED", pres_cfg)
ok(res["tracks"][0]["issues"] == ["ENERGY_MISSING"],
   f"a missing ENERGY fails its own check (got {res['tracks'][0]['issues']})")
ok(res["total_checks"] - res["pass_count"] == 1,
   f"and costs exactly one grade point ({res['pass_count']}/{res['total_checks']})")
res = _grade_album(pres_dir, "EMBEDDED", dict(pres_cfg, grade_check_energy=False))
ok("ENERGY_MISSING" not in res["tracks"][0]["issues"]
   and res["pass_count"] == res["total_checks"],
   "grade_check_energy=False stops grading ENERGY (check not counted)")

for _tag in ("TITLE", "ARTIST", "ALBUM", "ALBUMARTIST", "DATE", "TRACKNUMBER"):
    set_tags(pres_flac, FULL)
    del_tags(pres_flac, [_tag])
    res = _grade_album(pres_dir, "EMBEDDED", pres_cfg)
    ok(res["tracks"][0]["issues"] == [_tag]
       and res["total_checks"] - res["pass_count"] == 1,
       f"a missing {_tag} is graded by the missing-tag sweep "
       f"({res['tracks'][0]['issues']})")
    res = _grade_album(pres_dir, "EMBEDDED",
                       dict(pres_cfg, grade_check_missing_tags=False))
    ok(_tag not in res["tracks"][0]["issues"],
       f"grade_check_missing_tags=False stops grading {_tag}")

# DISCNUMBER only matters when the album really has more than one disc
set_tags(pres_flac, FULL)
del_tags(pres_flac, ["DISCNUMBER"])
res = _grade_album(pres_dir, "EMBEDDED", pres_cfg)
ok("DISCNUMBER" not in res["tracks"][0]["issues"],
   "a single-disc album is not required to carry DISCNUMBER")
set_tags(pres_flac, dict(FULL, DISCTOTAL="2"))
del_tags(pres_flac, ["DISCNUMBER"])
res = _grade_album(pres_dir, "EMBEDDED", pres_cfg)
ok("DISCNUMBER" in res["tracks"][0]["issues"],
   "but a multi-disc album is")
set_tags(pres_flac, FULL)

# ----------------------------------------------------------------------
# Excess tags: the app's own tags and Picard's spellings are never excess
# ----------------------------------------------------------------------
print("== excess tags ==")
for _key in ("TXXX:MusicBrainz Album Type", "TXXX:MusicBrainz Album Status",
             "TXXX:MusicBrainz Disc Id", "TXXX:MusicBrainz Album Artist Id",
             "----:com.apple.iTunes:MusicBrainz Album Type",
             "----:com.apple.iTunes:MusicBrainz Album Status",
             "AUDIOAUDITOR_OVERRIDE", "TXXX:AUDIOAUDITOR_OVERRIDE",
             "ENERGY", "MOOD", "TSSE", "TRANSLATION-EN"):
    ok(tag_key_allowed(_key), f"{_key} is part of the shared vocabulary")
for _key in ("PRIV:com.apple.iTunes", "POPM:user@example.com",
             "GEOB:mo3.tag", "MusicBrainz Junk Field", "FooBarJunk"):
    ok(not tag_key_allowed(_key), f"{_key} is excess")

ex_dir, ex_flac = fresh_album("Excess (2020)", FULL)
ex_cfg = dict(ISO_CFG, music_folder="", grade_check_naming=False,
              grade_check_excess_tags=True)
set_tags(ex_flac, {"MusicBrainz Album Type": "Album",
                   "MusicBrainz Album Status": "Official",
                   "AUDIOAUDITOR_OVERRIDE": "REAL",
                   "TRANSLATION-EN": "translated"})
res = _grade_album(ex_dir, "EMBEDDED", ex_cfg)
ok("TAGS" not in res["tracks"][0]["issues"],
   f"a Picard-tagged FLAC passes the excess check ({res['issues']})")
ok(res["tracks"][0]["values"].get("AUDIOAUDITOR_OVERRIDE") == "REAL",
   "and the override is actually READ out of the file "
   f"({res['tracks'][0]['values'].get('AUDIOAUDITOR_OVERRIDE')!r})")
del_tags(ex_flac, ["AUDIOAUDITOR_OVERRIDE", "audioauditor_override"])
set_tags(ex_flac, {"audioauditor_override": "fake"})
res = _grade_album(ex_dir, "EMBEDDED", ex_cfg)
ok(res["tracks"][0]["values"].get("AUDIOAUDITOR_OVERRIDE") == "FAKE",
   "a lowercase spelling of the override still counts "
   f"({res['tracks'][0]['values'].get('AUDIOAUDITOR_OVERRIDE')!r})")
ok("TAGS" not in res["tracks"][0]["issues"],
   "and is not flagged as an excess tag")
set_tags(ex_flac, {"FooBarJunk": "1"})
res = _grade_album(ex_dir, "EMBEDDED", ex_cfg)
ok("TAGS" in res["tracks"][0]["issues"],
   f"vendor junk is still excess ({res['issues']})")

# ----------------------------------------------------------------------
# A failing inner check is RECORDED, never silently dropped
# ----------------------------------------------------------------------
print("== no silently dropped checks ==")
import mlo.discs as _discs  # noqa: E402

cd_dir, cd_flac = fresh_album("Ripped (2020)",
                             dict(FULL, MEDIA="CD"))
cd_cfg = dict(ISO_CFG, music_folder="", grade_check_naming=False,
              grade_check_log_grade=False,
              grade_check_cd_log=False, grade_check_cd_cue=False,
              grade_check_crc=False, grade_check_cd_format=False,
              grade_check_disc_naming=True)
base_cd = _grade_album(cd_dir, "EMBEDDED", cd_cfg)
_orig_pat_fn = _discs._disc_pattern_for
try:
    _discs._disc_pattern_for = lambda cfg: (_ for _ in ()).throw(
        RuntimeError("boom"))
    res = _grade_album(cd_dir, "EMBEDDED", cd_cfg)
finally:
    _discs._disc_pattern_for = _orig_pat_fn
ok(base_cd["pass_count"] == base_cd["total_checks"],
   f"the disc-naming check passes on a clean CD album "
   f"({base_cd['pass_count']}/{base_cd['total_checks']})")
ok(res["total_checks"] == base_cd["total_checks"],
   "a check that throws is still COUNTED (not dropped from the denominator)")
ok(res["total_checks"] - res["pass_count"] == 1,
   "and it fails instead of silently disappearing")
ok(any("could not be evaluated" in i for i in res["issues"]),
   f"the failure names itself (got {res['issues']})")

# ----------------------------------------------------------------------
# CD .log must be USABLE, and CRC coverage is per DISC
# ----------------------------------------------------------------------
print("== CD log quality + multi-disc CRC ==")
log_cfg = dict(ISO_CFG, music_folder="", grade_check_naming=False,
               grade_check_log_grade=False,
               grade_check_cd_cue=False, grade_check_disc_naming=False,
               grade_check_cd_format=False, grade_check_crc=False,
               grade_check_cd_log=True)
log_path = os.path.join(cd_dir, "CD-1.log")
with open(log_path, "w", encoding="utf-8") as fh:
    fh.write("")
res = _grade_album(cd_dir, "EMBEDDED", log_cfg)
ok(any("not a usable rip log" in i for i in res["issues"]),
   f"an empty .log does not satisfy grade_check_cd_log (got {res['issues']})")
with open(log_path, "w", encoding="utf-8") as fh:
    fh.write("Exact Audio Copy v1.6\n\nTrack  1\n     Copy CRC 12345678\n")
res = _grade_album(cd_dir, "EMBEDDED", log_cfg)
ok(res["pass_count"] == res["total_checks"],
   f"a real rip log does ({res['pass_count']}/{res['total_checks']})")
os.remove(log_path)

md_dir = os.path.join(music, "Artists", "Artist", "Two Discs (2020)")
os.makedirs(md_dir, exist_ok=True)
for _name in ("1-01 Song A.flac", "1-02 Song B.flac", "2-01 Song C.flac"):
    _p = os.path.join(md_dir, _name)
    make_flac(_p)
    set_tags(_p, dict(FULL, MEDIA="CD", DISCNUMBER=_name[0], DISCTOTAL="2"))
for _n in (1, 2):
    with open(os.path.join(md_dir, f"CD-{_n}.log"), "w", encoding="utf-8") as fh:
        fh.write(f"disc {_n}\n")
crc_cfg = dict(ISO_CFG, music_folder="", grade_check_naming=False,
               grade_check_crc=True,
               grade_check_log_grade=False, grade_check_cd_log=False,
               grade_check_cd_cue=False, grade_check_disc_naming=False,
               grade_check_cd_format=False)
_orig_read = _discs.read_log_text
_orig_parse = _discs.parse_log_checksums
try:
    _discs.read_log_text = lambda p: os.path.basename(p)
    # parse_log_checksums keys are track NUMBERS (ints)
    _discs.parse_log_checksums = lambda text: (
        {1: "AAAAAAAA"} if "CD-1" in text
        else {1: "BBBBBBBB", 2: "CCCCCCCC"})
    res = _grade_album(md_dir, "EMBEDDED", crc_cfg)
finally:
    _discs.read_log_text = _orig_read
    _discs.parse_log_checksums = _orig_parse
_crc_keys = [k for k in res["issues"] if "not covered by .log CRC" in k]
ok(len(_crc_keys) == 1 and "1-02 Song B.flac" in res["issues"][_crc_keys[0]] and
   "2-01 Song C.flac" not in res["issues"][_crc_keys[0]],
   f"each disc is covered by ITS OWN log — disc 2's track 2 CRC cannot "
   f"cover disc 1's track 2 ({res['issues']})")

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
