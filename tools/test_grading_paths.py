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

from mlo.grader import (EMPTY_FOLDER, EXPECTED_TRACKS_MISSING, REPLAYGAIN_TAGS,
                        _grade_album, _naming_mismatch, _release_type_candidates,
                        run_grade_library, tag_key_allowed)
from mlo.paths import save_expected_tracks
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


def set_multi(path, key, values):
    """set_tags() writes ONE value per tag; this writes REPEATED fields
    (two GENREs), which is how the app stores a multi-value tag."""
    from mutagen.flac import FLAC
    f = FLAC(path)
    f[key] = list(values)
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

_MBID = "12345678-1234-1234-1234-123456789abc"


def layout(tags, release_type=None, shorter=False):
    """(artist dir, album dir, file stem) the SHIPPED naming script makes of
    *tags*. The fixtures below are spelled this way instead of hand-typed:
    the default layout is free to change, and a literal here would only pin
    the spelling it had when this file was written."""
    rel = eval_script(DEFAULT_NAMING_SCRIPT,
                      track_variables(tags, release_type=release_type),
                      shorter_ids=shorter)
    return tuple(rel.split("/"))


def album_path(root, tags, release_type=None, shorter=False):
    """Where the shipped script puts *tags* under *root* (with extension)."""
    return os.path.join(root, *layout(tags, release_type, shorter)) + ".flac"


# isolate the two new checks from everything else
ISO_CFG = {
    "music_folder": "",
    "grade_check_naming": True,
    # No .mlo_expected.json in these fixtures: the expected-tracklist check is
    # its own area (see grade_check_expected_tracks) and would fail every
    # synthetic album here for a reason this file never set up.
    "grade_check_expected_tracks": False,
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
    # The Genre COUNT check compares a track against mb_genre_count, whose
    # shipped default is 2 while every fixture here carries one genre: off
    # for these cases and switched on in its own block below.
    "grade_check_genre_count": False,
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
_a, _alb, _name = layout(BASE_TAGS)
os.makedirs(os.path.join(lib, _a, _alb), exist_ok=True)
good = os.path.join(lib, _a, _alb, _name + ".flac")
ok(_naming_mismatch(good, folder, DEFAULT_NAMING_SCRIPT, "", BASE_TAGS) == ("ok", None),
   "exact match below <music>/Artists returns ('ok', None)")
ok(_naming_mismatch(os.path.join(lib, "Wrong", _name + ".flac"), folder,
                    DEFAULT_NAMING_SCRIPT, "", BASE_TAGS)
   == ("path", f"{_a}/{_alb}/{_name}.flac"),
   "wrong folder returns expected path")
# the SAME relative layout one level up (music folder root) is not a match
ok(_naming_mismatch(os.path.join(folder, _a, _alb, _name + ".flac"),
                    folder, DEFAULT_NAMING_SCRIPT, "", BASE_TAGS)[0] == "path",
   "library layout in the music folder root fails (base is Artists/)")
# label / country / release type / catalog number land in the folder segment
# exactly as the script spells them, and a missing one drops its segment
rich = dict(BASE_TAGS, RELEASETYPE="Album", LABEL="Label", RELEASECOUNTRY="US",
            CATALOGNUMBER="CAT-1")
rich_path = album_path(lib, rich)
ok(_naming_mismatch(rich_path, folder, DEFAULT_NAMING_SCRIPT, "", rich) == ("ok", None),
   "release type, label and country join the folder segment")
ok(_naming_mismatch(good, folder, DEFAULT_NAMING_SCRIPT, "", rich)[0] == "path",
   "dropping them from the folder fails (script defines the path)")
_bare_rel = eval_script(DEFAULT_NAMING_SCRIPT, track_variables(BASE_TAGS))
ok("[" not in _bare_rel and "{" not in _bare_rel and f"/{_alb}/" in _bare_rel,
   "missing label/country/type leave NO dangling brackets")

tags_m = dict(BASE_TAGS, MUSICBRAINZ_ALBUMARTISTID=_MBID)
# the default script folds the MBID into ONE folder segment: "Artist [uuid]"
full = album_path(lib, tags_m)
short = album_path(lib, tags_m, shorter=True)
ok(full != short
   and os.path.basename(os.path.dirname(short))
   == os.path.basename(os.path.dirname(full)).replace(_MBID, _MBID[:8]),
   f"the short form really is the truncated id, bracket intact ({short} vs {full})")
ok(_naming_mismatch(full, folder, DEFAULT_NAMING_SCRIPT, "", tags_m) == ("ok", None),
   "full MBID path matches")
ok(_naming_mismatch(short, folder, DEFAULT_NAMING_SCRIPT, "", tags_m) == ("ok", None),
   "short MBID accepted too (ID length can't cause false fails)")
ok(_naming_mismatch(full, folder, DEFAULT_NAMING_SCRIPT, "", BASE_TAGS)[0] == "path",
   "MBID folder mismatches when the tag is absent")
# RELEASETYPE feeds the script like the organizer does — the tag's own
# spelling and the MusicBrainz spelling name the SAME album folder segment
with_type = album_path(lib, tags_m, "album")
capped_type = album_path(lib, tags_m, "Album; Live")
ok(_naming_mismatch(with_type, folder, DEFAULT_NAMING_SCRIPT, "album", tags_m) == ("ok", None),
   "a lowercase release type joins the album folder segment")
ok(capped_type != with_type
   and _naming_mismatch(capped_type, folder, DEFAULT_NAMING_SCRIPT, "album+live",
                        tags_m) == ("ok", None),
   "the same type in MusicBrainz casing matches too (no false fail)")
ok(_naming_mismatch(good.replace(_alb, _alb.upper()), folder,
                    DEFAULT_NAMING_SCRIPT, "", BASE_TAGS)[0] == "case",
   "case-only difference reports 'case' (PATH_CASE check)")
shutil.rmtree(folder, ignore_errors=True)

# ----------------------------------------------------------------------
# End-to-end: _grade_album on a synthetic album
# ----------------------------------------------------------------------
print("== _grade_album naming + key/bpm ==")
music = os.path.join(tmp, "Music")
# The folder is spelled the way the shipped script lays it out when the
# RELEASETYPE tag is present — the identity-tag check demands that tag, so a
# clean album states it. Anything graded with the tag absent still matches
# through the wildcard.
flac = album_path(os.path.join(music, "Artists"),
                  dict(BASE_TAGS, RELEASETYPE="Album"))
album = os.path.dirname(flac)
os.makedirs(album, exist_ok=True)
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
   [f"PATH: expected '{_mis_expected}' (run organize)"],
   f"the PATH issue names the script's expected path and the fix "
   f"(got {res['issues']})")

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
# with the RELEASETYPE tag present — the grader's identity-tag check demands
# that tag, so a clean album states it.
_ca, ALBUM_DIR, _cname = layout(dict(BASE_TAGS, RELEASETYPE="Album"))
BAD_ALBUM_DIR = ALBUM_DIR.upper()
EXPECTED_REL = f"{_ca}/{ALBUM_DIR}/{_cname}.flac"


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
# Genre count (grade_check_genre_count) — EXACTLY mb_genre_count values
# ----------------------------------------------------------------------
print("== genre count ==")
# The fixture carries one genre, so these cases pin the configured number to
# 1 — the check reads it from the config in every case below (the value has
# one home, and the tests must not accidentally pin today's shipped default).
cnt_cfg = dict(mood_cfg, grade_check_genre_count=True, mb_genre_count=1)
res = _grade_album(album, "EMBEDDED", cnt_cfg)
ok("GENRE_COUNT" not in res["tracks"][0]["issues"]
   and res["pass_count"] == res["total_checks"],
   f"one genre passes the count check ({res['pass_count']}/{res['total_checks']})")
# ... and it is COUNTED while on: switching it off drops one check from the
# denominator (a check that never counts is the bug this key class had).
res_off = _grade_album(album, "EMBEDDED",
                       dict(cnt_cfg, grade_check_genre_count=False))
ok(res_off["total_checks"] == res["total_checks"] - 1
   and res_off["pass_count"] == res_off["total_checks"],
   f"the check counts while on and disappears with the toggle off "
   f"({res['total_checks']} vs {res_off['total_checks']})")

set_multi(flac, "GENRE", ["Rock", "Alternative Rock"])
res = _grade_album(album, "EMBEDDED", cnt_cfg)
ok(res["tracks"][0]["issues"] == ["GENRE_COUNT"],
   f"two genres pass the presence check and fail only the count "
   f"(got {res['tracks'][0]['issues']})")
ok(any("2 genres, 1 expected" in i for i in res["issues"]),
   f"the issue names both numbers (got {res['issues']})")
ok(res["total_checks"] - res["pass_count"] == 1,
   f"and costs exactly one grade point ({res['pass_count']}/{res['total_checks']})")
# Raising the configured value clears it — the check reads the config, it does
# not compare against a literal of its own.
res = _grade_album(album, "EMBEDDED", dict(cnt_cfg, mb_genre_count=2))
ok(res["pass_count"] == res["total_checks"],
   f"two genres pass when 2 are configured ({res['pass_count']}/{res['total_checks']})")
# ... and a single genre then fails naming the OTHER number too.
set_tags(flac, dict(NO_MOOD, MOOD="melancholic"))
res = _grade_album(album, "EMBEDDED", dict(cnt_cfg, mb_genre_count=2))
ok(any("1 genre, 2 expected" in i for i in res["issues"]),
   f"a lone genre fails when 2 are configured (got {res['issues']})")

# No genre at all is graded by the count check too — independently of the
# presence toggle, with its own wording (never a second 'Missing GENRE').
del_tags(flac, ["GENRE"])
res = _grade_album(album, "EMBEDDED", dict(cnt_cfg, grade_check_genre=False))
ok(res["tracks"][0]["issues"] == ["GENRE_COUNT"],
   f"an absent genre fails the count check on its own "
   f"(got {res['tracks'][0]['issues']})")
ok(any("no genre, 1 expected" in i for i in res["issues"]),
   f"and says 'no genre' (got {res['issues']})")
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
# still owns DYNAMIC RANGE, which this fixture does not carry either). The
# baseline for the count is the same album with the family toggle OFF but the
# sweep ON — the generic sweep answers to its own key, so it adds its checks to
# the denominator as soon as it is switched on, and the family must add only
# its own four on top.
_rg_both_base = _grade_album(album, "EMBEDDED",
                             dict(cfg, grade_check_missing_tags=True))
res = _grade_album(album, "EMBEDDED",
                   dict(cfg, grade_check_replaygain=True,
                        grade_check_missing_tags=True))
_rg_issues = [i for i in res["tracks"][0]["issues"]
              if i.startswith("REPLAYGAIN")]
ok(res["total_checks"] == _rg_both_base["total_checks"] + 4
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
rt_dir = os.path.join(music, "Artists",
                      *layout(dict(BASE_TAGS, RELEASETYPE="album"))[:2])
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
rt_capped = os.path.join(music, "Artists",
                         *layout(dict(BASE_TAGS, RELEASETYPE="Album; Live"))[:2])
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

mc_dir = os.path.join(music, "Artists",
                      *layout(dict(BASE_TAGS, RELEASETYPE="Album",
                                   RELEASECOUNTRY="US", LABEL="Label A",
                                   CATALOGNUMBER="CAT-1"))[:2])
os.makedirs(mc_dir, exist_ok=True)
mc_flac = os.path.join(mc_dir, "1-01 Song.flac")
make_flac(mc_flac)
for sep in ("; ", " / ", "+"):
    # every separator spelling of the SAME countries yields the SAME path
    set_tags(mc_flac, dict(BASE_TAGS, INITIALKEY="B♭ min", BPM="120",
                           RELEASETYPE="Album", CATALOGNUMBER="CAT-1",
                           RELEASECOUNTRY="US" + sep + "GB",
                           LABEL="Label A" + sep + "Label B"))
    res = _grade_album(mc_dir, "EMBEDDED", cfg)
    ok("PATH" not in res["tracks"][0]["issues"],
       f"multi-value tags joined with {sep!r} still produce this path")
    ok(res["pass_count"] == res["total_checks"],
       f"and cost no grade point ({res['pass_count']}/{res['total_checks']})")
set_tags(mc_flac, dict(BASE_TAGS, INITIALKEY="B♭ min", BPM="120",
                       RELEASETYPE="Album", CATALOGNUMBER="CAT-1",
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
# grade_check_crc and grade_check_log_checksum are OFF here on purpose: this
# block is about which logs COUNT as rip logs, and the two integrity rules
# that read a real log's checksums are its own section below.
log_cfg = dict(ISO_CFG, music_folder="", grade_check_naming=False,
               grade_check_log_grade=False,
               grade_check_cd_cue=False, grade_check_disc_naming=False,
               grade_check_cd_format=False, grade_check_crc=False,
               grade_check_log_checksum=False,
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

# ---- the log's own checksums are GRADED -------------------------------
# A log is the CD's only integrity evidence, so a checksum in it that does
# not match the rip — or an EAC SHA256 that does not verify — has to cost the
# album its PASS. Coverage alone ("a CRC exists for track N") let a log from a
# different rip, or audio altered after the rip, grade clean.
print("== log checksums are graded ==")
from mlo.tools import detect_all_tools as _detect_all  # noqa: E402

# Each rule gets its own config: the CRC value pass is graded in isolation
# from the log's own SHA256, exactly like the rest of this file isolates a
# check before asserting what it costs.
_crc_cfg = dict(log_cfg, grade_check_crc=True)
_ck_cfg = dict(log_cfg, grade_check_log_checksum=True)
_FFMPEG = (_detect_all().get("ffmpeg") or {}).get("ffmpeg_exe")
if not _FFMPEG:
    print("  skipped: no ffmpeg — the CRC value pass cannot decode audio here")
else:
    _real_crc = _discs._audio_crc32(_FFMPEG, cd_flac)

    def _write_log(crc):
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write("Exact Audio Copy v1.6\n\nTrack  1\n"
                     f"     Copy CRC {crc}\n")

    _write_log(_real_crc)
    res = _grade_album(cd_dir, "EMBEDDED", _crc_cfg)
    ok("CRC_MISMATCH" not in res["tracks"][0]["issues"],
       f"a log CRC that matches the audio passes "
       f"({res['tracks'][0]['issues']})")

    _write_log("00000000")
    res = _grade_album(cd_dir, "EMBEDDED", _crc_cfg)
    ok("CRC_MISMATCH" in res["tracks"][0]["issues"],
       f"a log CRC that does NOT match the audio fails the track "
       f"({res['tracks'][0]['issues']})")
    ok(any("does not match the track's audio CRC" in i for i in res["issues"]),
       f"and the album issue says which values disagreed ({res['issues']})")
    ok(res["pass_count"] < res["total_checks"],
       f"so the album does not pass ({res['pass_count']}/{res['total_checks']})")
    res = _grade_album(cd_dir, "EMBEDDED", dict(_crc_cfg, grade_check_crc=False))
    ok("CRC_MISMATCH" not in res["tracks"][0]["issues"],
       "grade_check_crc=False stops the value pass (check not counted)")
    _write_log(_real_crc)

# The EAC SHA256 the log carries (==== Log checksum … ====): an INVALID one is
# a log that cannot be trusted about anything it says, including its CRCs.
_ck_orig = _discs.check_log_checksum
_discs.check_log_checksum = lambda _p: ("invalid", "expected 1111 computed 2222")
try:
    res = _grade_album(cd_dir, "EMBEDDED", _ck_cfg)
    ok("LOG_CHECKSUM" in res["tracks"][0]["issues"],
       f"an unverifiable log checksum fails the track "
       f"({res['tracks'][0]['issues']})")
    ok(any("Rip .log checksum does not verify" in i for i in res["issues"]),
       f"the album names the failure ({res['issues']})")
    ok(res["pass_count"] < res["total_checks"],
       f"and costs the album its PASS ({res['pass_count']}/{res['total_checks']})")
    _discs.check_log_checksum = lambda _p: ("ok", None)
    res = _grade_album(cd_dir, "EMBEDDED", _ck_cfg)
    ok("LOG_CHECKSUM" not in res["tracks"][0]["issues"],
       "a verifying log checksum passes the same check")
finally:
    _discs.check_log_checksum = _ck_orig
res = _grade_album(cd_dir, "EMBEDDED", dict(_ck_cfg, grade_check_log_checksum=False))
ok("LOG_CHECKSUM" not in res["tracks"][0]["issues"],
   "grade_check_log_checksum=False stops the check (not counted)")

# An XLD / old-EAC log has no checksum concept at all: 'unsupported' must
# never be read as "wrong" — nothing claimed, nothing refuted.
_discs.check_log_checksum = lambda _p: ("unsupported", "XLD log has no EAC checksum")
try:
    res = _grade_album(cd_dir, "EMBEDDED", _ck_cfg)
    ok("LOG_CHECKSUM" not in res["tracks"][0]["issues"],
       f"a log with no checksum concept is not failed for having none "
       f"({res['tracks'][0]['issues']})")
finally:
    _discs.check_log_checksum = _ck_orig

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

# ----------------------------------------------------------------------
# Empty folders count against grading (grade_check_empty_folders)
# ----------------------------------------------------------------------
print("== empty folders ==")
import mlo.grader as grader  # noqa: E402

# One music root holding a real album plus every shape of emptiness the sweep
# must tell apart: a folder with nothing at all, a folder whose only child is
# empty, and one whose DEEPEST level holds a single file (not empty, however
# bare). The music root itself, a dot-dir and a SKIP_DIRS subtree are here too:
# none of them is ever a row.
el_music = os.path.join(tmp, "EmptyLib", "Music")
el_album = os.path.join(el_music, "Artists", "Artist", "Album (2020)")
os.makedirs(el_album)
_el_flac = os.path.join(el_album, "01 - Song.flac")
make_flac(_el_flac)
set_tags(_el_flac, BASE_TAGS)
_bare = os.path.join(el_music, "Artists", "Nobody")
os.makedirs(_bare)                                   # (a) nothing at all
_holder = os.path.join(el_music, "Artists", "Holder")
_inner = os.path.join(_holder, "Inner")
os.makedirs(_inner)                                  # (b) only an empty child
_deep = os.path.join(el_music, "Artists", "Deep", "Inner")
os.makedirs(_deep)
with open(os.path.join(_deep, "notes.txt"), "w", encoding="utf-8") as fh:
    fh.write("x")                                    # (c) one file, no audio
os.makedirs(os.path.join(el_music, ".hidden", "Inner"))
os.makedirs(os.path.join(el_music, "data", "Inner"))

# Everything but the sweep is off: these runs are about which folders become
# rows, not about what the album's own checks say.
EMPTY_CFG = dict(ISO_CFG, music_folder="", grade_check_naming=False,
                 grade_check_key_bpm=False, grade_verbose=False)


def graded(cfg):
    """run_grade_library with its per-row log lines captured."""
    lines = []
    real_log = grader.log
    grader.log = lambda msg, *a, **k: lines.append(str(msg))
    try:
        return run_grade_library(dict(cfg)), lines
    finally:
        grader.log = real_log


def row_line(lines, where):
    """The ONE summary line the run printed for `where` (None when it skipped)."""
    hits = [l for l in lines if l.startswith(("✓ ", "✕ ")) and f" {where} " in l]
    assert len(hits) <= 1, hits
    return hits[0] if hits else None


ALBUM_REL = os.path.join("Artists", "Artist", "Album (2020)")
BARE_REL = os.path.join("Artists", "Nobody")
HOLDER_REL = os.path.join("Artists", "Holder")
INNER_REL = os.path.join("Artists", "Holder", "Inner")
DEEP_REL = os.path.join("Artists", "Deep", "Inner")

stats_on, lines_on = graded(dict(EMPTY_CFG, music_folder=el_music))
_present_on = [l for l in lines_on if l.startswith(("✓ ", "✕ "))]
ok(len(_present_on) == 4,
   f"the album plus the three empty folders are the only rows ({_present_on})")
ok(stats_on["issue_counts"].get(EMPTY_FOLDER) == 3,
   f"each empty folder is one EMPTY_FOLDER issue ({stats_on['issue_counts']})")
ok(stats_on["grade_dist"] == {"PASS": 1, "FAIL": 3},
   f"an empty folder is a graded row, so it lands in grade_dist "
   f"({stats_on['grade_dist']})")
ok(stats_on["total_scanned"] == 4,
   f"'graded N' stays in step with grade_dist ({stats_on['total_scanned']})")
ok(sum(1 for l in lines_on if "issues: EMPTY_FOLDER" in l) == 3,
   "all three empty rows report the EMPTY_FOLDER issue")
for _rel in (BARE_REL, HOLDER_REL, INNER_REL):
    _row = row_line(lines_on, _rel)
    ok(_row is not None and "FAIL 0/1" in _row and "0 tr" in _row,
       f"{_rel} is a failed row with no tracks ({_row})")
ok(row_line(lines_on, ALBUM_REL) is not None, "the real album is still graded")
ok(not any("EMPTY_FOLDER" in l for l in lines_on if ALBUM_REL in l),
   "the real album's row carries no EMPTY_FOLDER issue")
ok(not any(x in l for l in lines_on
           for x in (os.path.join(".hidden", "Inner"), os.path.join("data", "Inner"))),
   "a dot-dir and a SKIP_DIRS subtree are never reported")
ok(row_line(lines_on, DEEP_REL) is None,
   "a folder whose deepest level holds a file is not empty")

# ...and with the key OFF the sweep does not run at all: no row, no issue, and
# the album's own row is byte-for-byte the one it had with the key on.
stats_off, lines_off = graded(dict(EMPTY_CFG, music_folder=el_music,
                                   grade_check_empty_folders=False))
ok(EMPTY_FOLDER not in stats_off["issue_counts"], stats_off["issue_counts"])
ok(stats_off["grade_dist"] == {"PASS": 1, "FAIL": 0},
   f"nothing is reported with the key off ({stats_off['grade_dist']})")
ok(stats_off["total_scanned"] == 1, stats_off["total_scanned"])
ok(row_line(lines_off, ALBUM_REL) == row_line(lines_on, ALBUM_REL),
   "the album's row is unchanged by the empty-folder sweep")
ok(not any(row_line(lines_off, r) for r in (BARE_REL, HOLDER_REL, INNER_REL)),
   "no empty folder becomes a row with the key off")

# A targeted run grades the selection, not the tree: no library-wide walk, so
# no empty-folder sweep either.
stats_targeted, lines_targeted = graded(dict(EMPTY_CFG, music_folder=el_music,
                                             targets=[_el_flac]))
ok(EMPTY_FOLDER not in stats_targeted["issue_counts"],
   f"a targeted run reports no empty folders ({stats_targeted['issue_counts']})")
ok(stats_targeted["total_scanned"] == 1, stats_targeted["total_scanned"])
ok(row_line(lines_targeted, ALBUM_REL) is not None
   and not any(row_line(lines_targeted, r)
               for r in (BARE_REL, HOLDER_REL, INNER_REL)),
   "the targeted run grades the album alone")

# A music root holding NOTHING but empty folders: the folders are rows, the
# root itself never is — an empty music folder is not a folder to clean up, and
# reporting it would bury the real ones under a row for the library itself.
only_empty = os.path.join(tmp, "OnlyEmpty", "Music")
os.makedirs(os.path.join(only_empty, "Lonely"))
stats_only, lines_only = graded(dict(EMPTY_CFG, music_folder=only_empty))
_only_rows = [l for l in lines_only if l.startswith(("✓ ", "✕ "))]
ok(stats_only["issue_counts"].get(EMPTY_FOLDER) == 1
   and len(_only_rows) == 1
   and row_line(lines_only, "Lonely") is not None,
   f"the empty folder is the only row — the music folder root is not one "
   f"({_only_rows})")

# ----------------------------------------------------------------------
# expected release tracklist (grade_check_expected_tracks)
# ----------------------------------------------------------------------
# ISO_CFG ships the key OFF (its synthetic albums have no manifest and the
# check is not what those cases are about), so it doubles as the "toggled
# off" case here; MAN_CFG turns it on over its own album tree.
print("== grade_check_expected_tracks ==")
mn_music = os.path.join(tmp, "Manifest", "Music")
# The album states a MusicBrainz release id: the manifest is only required
# where script 15 could have written one (it refuses to fabricate a tracklist
# for a release it cannot name), and that is what makes the missing manifest a
# failure below — and a pass once the manifest exists.
mn_tags = dict(BASE_TAGS, MUSICBRAINZ_ALBUMID=_MBID)
mn_flac = album_path(mn_music, mn_tags)
os.makedirs(os.path.dirname(mn_flac), exist_ok=True)
make_flac(mn_flac)
set_tags(mn_flac, mn_tags)
mn_dir = os.path.dirname(mn_flac)
mn_rel = os.path.relpath(mn_dir, mn_music)

MAN_CFG = dict(ISO_CFG, music_folder=mn_music, grade_check_naming=False,
               grade_check_key_bpm=False, grade_verbose=False,
               grade_check_expected_tracks=True)

stats_man_no, lines_man_no = graded(dict(MAN_CFG))
mn_row = row_line(lines_man_no, mn_rel)
ok(stats_man_no["issue_counts"].get(EXPECTED_TRACKS_MISSING) == 1,
   f"an album with no manifest reports one EXPECTED_TRACKS_MISSING "
   f"({stats_man_no['issue_counts']})")
ok(mn_row is not None and "✕" in mn_row and "FAIL" in mn_row,
   f"the album is a FAILED row ({mn_row})")
ok(any("issues: EXPECTED_TRACKS_MISSING" in l for l in lines_man_no),
   "the row names the missing manifest")
ok(stats_man_no["grade_dist"] == {"PASS": 0, "FAIL": 1},
   f"the check costs the album its PASS ({stats_man_no['grade_dist']})")

# The release's own tracklist — the files on disk match it exactly, so the
# album is complete and the check has nothing to fail.
save_expected_tracks(mn_dir, "11111111-1111-1111-1111-111111111111",
                     [{"disc": 1, "position": 1, "title": "Song",
                       "recording_mbid": "22222222-2222-2222-2222-222222222222"}])
stats_man_yes, lines_man_yes = graded(dict(MAN_CFG))
ok(EXPECTED_TRACKS_MISSING not in stats_man_yes["issue_counts"],
   f"an album WITH a manifest is never failed by the check "
   f"({stats_man_yes['issue_counts']})")
ok(stats_man_yes["grade_dist"] == {"PASS": 1, "FAIL": 0},
   f"it grades PASS ({stats_man_yes['grade_dist']})")

# Key OFF: the same album (manifest deleted again) is not graded on it at all
# — no issue, no row change, no extra check in the denominator.
os.remove(os.path.join(mn_dir, ".mlo_expected.json"))
stats_man_off, lines_man_off = graded(dict(MAN_CFG,
                                           grade_check_expected_tracks=False))
ok(EXPECTED_TRACKS_MISSING not in stats_man_off["issue_counts"],
   f"nothing is reported with the key off ({stats_man_off['issue_counts']})")
ok(row_line(lines_man_off, mn_rel) is not None
   and "PASS" in row_line(lines_man_off, mn_rel),
   "the album grades without the manifest while the check is off")

# No release id on the album: script 15 writes NO manifest for it (one line
# says so), so the check is not graded there at all — the album is reported on
# what it can be fixed on, never on a file nothing can produce.
nm_music = os.path.join(tmp, "NoId", "Music")
nm_flac = album_path(nm_music, dict(BASE_TAGS))
os.makedirs(os.path.dirname(nm_flac), exist_ok=True)
make_flac(nm_flac)
set_tags(nm_flac, dict(BASE_TAGS))
stats_nom, _lines_nom = graded(dict(MAN_CFG, music_folder=nm_music))
ok(EXPECTED_TRACKS_MISSING not in stats_nom["issue_counts"],
   f"an album with no MusicBrainz release id is not failed for a manifest it "
   f"can never have ({stats_nom['issue_counts']})")
ok(stats_nom["grade_dist"] == {"PASS": 1, "FAIL": 0},
   f"and it grades PASS ({stats_nom['grade_dist']})")

print(f"\nAll {passed} checks passed.")
shutil.rmtree(tmp, ignore_errors=True)
shutil.rmtree(outside, ignore_errors=True)
