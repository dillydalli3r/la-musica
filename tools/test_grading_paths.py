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

from mlo.grader import (ALBUM_TAGS, EMPTY_FOLDER, EXPECTED_TRACKS_INCOMPLETE,
                        EXPECTED_TRACKS_MISSING,
                        PER_TRACK_TAGS, REPLAYGAIN_TAGS,
                        _grade_album, _naming_mismatch, _release_type_candidates,
                        printed_pct, run_grade_library, tag_key_allowed)
from mlo.paths import save_expected_tracks
from mlo import naming
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
    "GENRE": "shoegaze",
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
    # Tag-value CASE: every fixture here spells its GENRE the way the
    # vocabulary publishes it ("shoegaze"), and the GENRE half of this check
    # now cares — a tag is stored in the capitalization the writers produce.
    # Off for these cases, switched on in its own block below.
    "grade_check_tag_case": False,
    # The CD legs: these fixtures are synthetic CD-tagged folders with no
    # rip artefacts (no .log, no .accurip), so the readout would charge
    # "nothing established the CD verdict's '<leg>' leg" — a real failure for
    # a real disc folder, and not the subject of a single case here. Switched
    # off exactly as the other checks this file does not set up are; the CD
    # evidence rules have their own suite (test_cd_audit.py).
    "audit_require_accuraterip": False,
    "audit_verify_log_checksum": False,
    "audit_log_score_threshold": 0,
}

tmp = tempfile.mkdtemp(prefix="mlo_naming_test_")

# The grader now reads the app's RATING store (store row vs file tag). Point
# that store at this test's temp dir BEFORE the first grade, so no real
# ratings.db is created or opened.
from server import ratings as _ratings_mod               # noqa: E402
_ratings_mod.db_path = lambda: os.path.join(tmp, "grade_ratings.db")

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
# The LENGTH rule: a name that is too long gives way in its TITLE
# ----------------------------------------------------------------------
print("== the length rule ==")
# The longest allowed name is a filesystem fact (255), and the app reserves a
# little of it for the suffixes its own writers add.
ok(naming.MAX_SEGMENT_BYTES < 255,
   f"the name limit leaves room for the app's own suffixes ({naming.MAX_SEGMENT_BYTES})")

# A paragraph-long TITLE, ALBUM and ARTIST: nothing the script produces may
# exceed the limit anywhere in the path — and what gives way is the free text,
# never the structure the app identifies an album and a file by.
_long = dict(BASE_TAGS, TITLE="T" * 400, ALBUM="A" * 400,
             ARTIST="B" * 400, ALBUMARTIST="B" * 400)
_segs = eval_script(DEFAULT_NAMING_SCRIPT, track_variables(_long)).split("/")
ok(_segs and all(len(s.encode("utf-8")) <= naming.MAX_SEGMENT_BYTES for s in _segs),
   f"every segment of a paragraph-titled album fits ({[len(s) for s in _segs]})")
# ...and the extension a caller appends (+".flac") still lands under 255 bytes
ok(all(len(s.encode("utf-8")) + 5 <= 255 for s in _segs),
   "and the extension a caller appends still fits the real limit")
ok(_segs[-1].startswith("1-01 T") and _segs[0].startswith("BBB"),
   f"the numbering and the words that fit are kept ({_segs[-1][:12]!r})")
ok("A" * 10 in _segs[-2],
   "the album's own words are what was cut, not the whole segment")

# An album whose tags carry every id keeps them: the ids are what a reader
# identifies the release by, and what gave way is the text around them.
_ids = dict(_long, MUSICBRAINZ_ALBUMARTISTID=_MBID,
            MUSICBRAINZ_ALBUMID="abcdef01-1234-1234-1234-123456789abc",
            MUSICBRAINZ_RELEASEGROUPID="abcdef02-1234-1234-1234-123456789abc",
            MUSICBRAINZ_TRACKID="abcdef03-1234-1234-1234-123456789abc")
_rel_ids = eval_script(DEFAULT_NAMING_SCRIPT, track_variables(_ids)).split("/")
ok(_MBID in _rel_ids[0] and _ids["MUSICBRAINZ_ALBUMID"] in _rel_ids[1]
   and _ids["MUSICBRAINZ_RELEASEGROUPID"] in _rel_ids[1]
   and _ids["MUSICBRAINZ_TRACKID"] in _rel_ids[2],
   "every MusicBrainz id survives the cut")
ok(all(len(s.encode("utf-8")) <= naming.MAX_SEGMENT_BYTES for s in _rel_ids),
   "and the ids do not push a segment over the limit")
# ...and the SHORT-id spelling is still the short spelling: the fit is
# measured, so freeing id bytes must not be mistaken for a different path.
_short_ids = eval_script(DEFAULT_NAMING_SCRIPT, track_variables(_ids),
                         shorter_ids=True).split("/")
ok(all(len(s.encode("utf-8")) <= naming.MAX_SEGMENT_BYTES for s in _short_ids)
   and _MBID not in _short_ids[0] and _MBID[:8] in _short_ids[0],
   "the short-id spelling fits and is still short")

# An ordinary release is untouched, byte for byte: the rule only ever bites
# when a name would really not fit.
ok(eval_script(DEFAULT_NAMING_SCRIPT, track_variables(BASE_TAGS))
   == "Artist/2020 - Album/1-01 Song",
   "a normal album's path is unchanged")

# A hand-typed script whose LITERAL text is too long is cut as a whole, and
# the extension survives — a file without its ".flac" is one this app's own
# scanners stop finding.
_literal = eval_script("Z" * 400 + "/" + "W" * 400 + "/" + "q" * 400 + ".flac",
                       track_variables(BASE_TAGS))
ok(all(len(s.encode("utf-8")) <= naming.MAX_SEGMENT_BYTES
       for s in _literal.split("/")),
   "a script with a literal too long is cut to what the OS accepts too")
ok(_literal.endswith(".flac"), f"the extension is kept ({_literal[-12:]!r})")

# Bytes, not characters: a CJK title is cut on a character boundary, so the
# name that reaches the filesystem is still valid UTF-8.
_cjk = eval_script(DEFAULT_NAMING_SCRIPT,
                   track_variables(dict(BASE_TAGS, TITLE="曲" * 400,
                                        ALBUM="集" * 400)))
ok(all(len(s.encode("utf-8")) <= naming.MAX_SEGMENT_BYTES and "\ufffd" not in s
       for s in _cjk.split("/")),
   "a multi-byte title is cut between characters, never through one")

# The rule is a fixed point (organizing an organized library is a no-op) and
# the reader keys a name exactly as the writer spells it.
_long_name = naming.sanitize_segment("N" * 400 + ".flac")
ok(naming.sanitize_segment(_long_name) == _long_name,
   "the rule is a fixed point (a second pass changes nothing)")
ok(len(_long_name.encode("utf-8")) <= naming.MAX_SEGMENT_BYTES
   and _long_name.endswith(".flac"),
   f"the backstop cuts a whole name and keeps its extension ({_long_name[-10:]!r})")
ok(naming.name_key("N" * 400 + ".flac") == _long_name,
   "the reader keys a long name to what the writer would spell")

# End to end through the grader: an album named by a paragraph-long title is
# not a naming mismatch, because the expected path is fitted the same way.
_ltags = dict(BASE_TAGS, TITLE="T" * 400, ALBUM="A" * 400,
              ALBUMARTIST="B" * 400)
_lfolder = tempfile.mkdtemp(prefix="mlo_naming_long_")
try:
    _llib = os.path.join(_lfolder, "Artists")
    _lpath = album_path(_llib, _ltags)
    ok(_naming_mismatch(_lpath, _lfolder, DEFAULT_NAMING_SCRIPT, "", _ltags)
       == ("ok", None),
       "the grader accepts the fitted path for a paragraph-titled album")
    ok(all(len(part.encode("utf-8")) <= naming.MAX_SEGMENT_BYTES
           for part in os.path.relpath(_lpath, _llib).split(os.sep)),
       "and that path really is inside the limit")
finally:
    shutil.rmtree(_lfolder, ignore_errors=True)

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
# Genre count (grade_check_genre_count) — AT MOST mb_genre_count values
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

set_multi(flac, "GENRE", ["shoegaze", "dream pop"])
res = _grade_album(album, "EMBEDDED", cnt_cfg)
ok(res["tracks"][0]["issues"] == ["GENRE_COUNT"],
   f"two genres pass the presence check and fail only the count "
   f"(got {res['tracks'][0]['issues']})")
ok(any("2 genres, at most 1 allowed" in i for i in res["issues"]),
   f"the issue names the count and the cap (got {res['issues']})")
ok(res["total_checks"] - res["pass_count"] == 1,
   f"and costs exactly one grade point ({res['pass_count']}/{res['total_checks']})")
# Raising the configured value clears it — the check reads the config, it does
# not compare against a literal of its own.
res = _grade_album(album, "EMBEDDED", dict(cnt_cfg, mb_genre_count=2))
ok(res["pass_count"] == res["total_checks"],
   f"two genres pass when 2 are configured ({res['pass_count']}/{res['total_checks']})")
# The check is a CEILING, not a quota: one specific genre is a complete answer
# (the family is the writers' to derive), so there is nothing to top up and no
# lower bound to fail.
set_multi(flac, "GENRE", ["shoegaze"])
res = _grade_album(album, "EMBEDDED", dict(cnt_cfg, mb_genre_count=3))
ok("GENRE_COUNT" not in res["tracks"][0]["issues"]
   and res["pass_count"] == res["total_checks"],
   f"a lone genre passes under any cap >= 1 ({res['pass_count']}/{res['total_checks']})")
ok(not any("at most" in i for i in res["issues"]), f"and nothing is reported (got {res['issues']})")

# No genre at all is the PRESENCE check's business, not this one's: with that
# toggle off, an absent tag is nothing this check has an opinion about (there
# is no lower bound to violate).
del_tags(flac, ["GENRE"])
res = _grade_album(album, "EMBEDDED", dict(cnt_cfg, grade_check_genre=False))
ok("GENRE_COUNT" not in res["tracks"][0]["issues"],
   f"an absent genre is not a count failure (got {res['tracks'][0]['issues']})")
set_tags(flac, dict(NO_MOOD, MOOD="melancholic"))

# ----------------------------------------------------------------------
# Genre ORDER (grade_check_genre_order) — the family, if present, is FIRST
# ----------------------------------------------------------------------
print("== genre order ==")
# The count check is off in every case here: order and count are separate
# questions, and the order check must not re-report a count it was told not to
# grade (its fixtures are three genres long so the count would be clean
# anyway, which is what makes this wording independent of the count rule).
ord_cfg = dict(mood_cfg, grade_check_genre_count=False, mb_genre_count=3,
               grade_check_genre_order=True)
set_multi(flac, "GENRE", ["shoegaze", "dream pop", "rock"])
res = _grade_album(album, "EMBEDDED", ord_cfg)
ok(res["tracks"][0]["issues"] == ["GENRE_ORDER"],
   f"a family in the last slot fails the order check "
   f"(got {res['tracks'][0]['issues']})")
ok(any(i.startswith("family genre must be the first one") for i in res["issues"]),
   f"the issue names the rule (got {res['issues']})")
ok(res["total_checks"] - res["pass_count"] == 1,
   f"and costs exactly one grade point ({res['pass_count']}/{res['total_checks']})")

set_multi(flac, "GENRE", ["rock", "shoegaze", "dream pop"])
res = _grade_album(album, "EMBEDDED", ord_cfg)
ok("GENRE_ORDER" not in res["tracks"][0]["issues"]
   and res["pass_count"] == res["total_checks"],
   f"family first, specifics behind it passes ({res['pass_count']}/{res['total_checks']})")
# No family at all is fine too — it is derived, never required.
set_multi(flac, "GENRE", ["shoegaze", "dream pop"])
res = _grade_album(album, "EMBEDDED", ord_cfg)
ok("GENRE_ORDER" not in res["tracks"][0]["issues"]
   and res["pass_count"] == res["total_checks"],
   f"a list with no family passes ({res['pass_count']}/{res['total_checks']})")

# ... and the switch removes it from the grade (and from the denominator).
res_off = _grade_album(album, "EMBEDDED", dict(ord_cfg, grade_check_genre_order=False))
ok("GENRE_ORDER" not in res_off["tracks"][0]["issues"]
   and res_off["total_checks"] == res["total_checks"] - 1
   and res_off["pass_count"] == res_off["total_checks"],
   f"grade_check_genre_order=False stops grading it ({res_off['total_checks']})")

set_multi(flac, "GENRE", ["shoegaze", "shoegaze", "rock"])
res = _grade_album(album, "EMBEDDED", ord_cfg)
ok("GENRE_ORDER" in res["tracks"][0]["issues"]
   and any(i.startswith("duplicate") for i in res["issues"]),
   f"a repeated genre fails with the duplicate wording (got {res['issues']})")

# The tag as the app STORES it is the repeated field read above; the form
# another tagger leaves behind is ONE value with the names "; "-joined, and the
# order rule reads that the same way (a "; " is MLO's own multi-value
# separator, so the two spellings are the same list).
set_tags(flac, {"GENRE": "shoegaze; dream pop; rock"})
res = _grade_album(album, "EMBEDDED", ord_cfg)
ok("GENRE_ORDER" in res["tracks"][0]["issues"]
   and any(i.startswith("family genre must be the first one") for i in res["issues"]),
   f"a '; '-joined value is read as the names inside it (got {res['issues']})")
set_tags(flac, {"GENRE": "rock; shoegaze; dream pop"})
res = _grade_album(album, "EMBEDDED", ord_cfg)
ok("GENRE_ORDER" not in res["tracks"][0]["issues"],
   f"and the same names in order pass (got {res['tracks'][0]['issues']})")

# A family is the FIRST slot, so the same family twice is the same defect seen
# from the other side: the second one is not a specific genre, it is a repeat
# of the head, and a specific genre that IS the family ("rock, rock") is that
# repeat under another name. Both are reported — the hierarchy AND the
# duplicate — because a reader acting on either line must be able to see it.
set_multi(flac, "GENRE", ["rock", "rock", "shoegaze"])
res = _grade_album(album, "EMBEDDED", ord_cfg)
ok("GENRE_ORDER" in res["tracks"][0]["issues"]
   and any(i.startswith("family genre must be the first one") for i in res["issues"])
   and any(i.startswith("duplicate") for i in res["issues"]),
   f"a family in the first slot twice is both a hierarchy and a duplicate "
   f"problem (got {res['issues']})")

# The OVERFLOW is the count check's business, not this one's: two checks may
# not both fail one track over the same list.
set_multi(flac, "GENRE", ["rock", "shoegaze", "dream pop", "post-britpop"])
res = _grade_album(album, "EMBEDDED", ord_cfg)
ok("GENRE_ORDER" not in res["tracks"][0]["issues"]
   and "GENRE_COUNT" not in res["tracks"][0]["issues"],
   f"with the count check off an overflow is not re-reported as order "
   f"(got {res['tracks'][0]['issues']})")
res = _grade_album(album, "EMBEDDED", dict(ord_cfg, grade_check_genre_count=True))
ok(res["tracks"][0]["issues"] == ["GENRE_COUNT"],
   f"with it on the overflow fails that check ONLY "
   f"(got {res['tracks'][0]['issues']})")

# ----------------------------------------------------------------------
# Genre VOCABULARY (grade_check_genre_vocab) — MusicBrainz's own names
# ----------------------------------------------------------------------
print("== genre vocabulary ==")
# A name MusicBrainz does not publish is reported, one line per name: the
# writers keep what a source said (dropping evidence is worse) and this is
# where it surfaces.
set_multi(flac, "GENRE", ["shoegaze", "Nonsense"])
voc_cfg = dict(mood_cfg, grade_check_genre_count=False, grade_check_genre_vocab=True)
res = _grade_album(album, "EMBEDDED", voc_cfg)
ok(res["tracks"][0]["issues"] == ["GENRE_VOCAB"],
   f"an unknown name fails the vocabulary check (got {res['tracks'][0]['issues']})")
ok(any("Not a MusicBrainz genre: Nonsense" in i for i in res["issues"]),
   f"the issue names the genre (got {res['issues']})")
ok(res["total_checks"] - res["pass_count"] == 1,
   f"and costs exactly one grade point ({res['pass_count']}/{res['total_checks']})")
# A spelling the vocabulary DOES know is not a failure, whatever its case: the
# writers canonicalize it (mlo.genres.canonical), so a library tagged by an
# older build does not start failing.
set_multi(flac, "GENRE", ["Rock", "Shoegaze"])
res = _grade_album(album, "EMBEDDED", voc_cfg)
ok("GENRE_VOCAB" not in res["tracks"][0]["issues"]
   and res["pass_count"] == res["total_checks"],
   f"'Shoegaze' is a known name ({res['pass_count']}/{res['total_checks']})")
# The switch: with it off the same file passes and one check leaves the
# denominator.
set_multi(flac, "GENRE", ["shoegaze", "Nonsense"])
res_off = _grade_album(album, "EMBEDDED", dict(voc_cfg, grade_check_genre_vocab=False))
ok("GENRE_VOCAB" not in res_off["tracks"][0]["issues"]
   and res_off["total_checks"] == res["total_checks"] - 1
   and res_off["pass_count"] == res_off["total_checks"],
   f"grade_check_genre_vocab=False stops grading it ({res_off['total_checks']})")
# …and with the ORDER check off too, the vocabulary is not re-reported under
# the other code (a disabled check never reappears elsewhere).
res = _grade_album(album, "EMBEDDED",
                   dict(voc_cfg, grade_check_genre_vocab=False,
                        grade_check_genre_order=True))
ok("GENRE_ORDER" not in res["tracks"][0]["issues"],
   f"an unknown name is not an ORDER failure (got {res['tracks'][0]['issues']})")

# ----------------------------------------------------------------------
# Genre CAPITALIZATION (grade_check_tag_case → issue GENRE_CASE)
# ----------------------------------------------------------------------
print("== genre capitalization ==")
# GENRE is the one tag whose canonical spelling mlo.tagtext cannot state: it is
# an OPEN, MULTI-VALUE tag, so its canonical form is per NAME (one call of
# mlo.genres.display_name each) and it is deliberately absent from
# CANONICAL_CASE. Nothing else reports a name stored the vocabulary's own way —
# the vocabulary check folds case and the order check only looks at the family
# slot — so "metal; alternative metal" graded clean while every genre writer
# would have stored "Metal; Alternative Metal".
case_cfg = dict(mood_cfg, grade_check_genre_count=False,
                grade_check_tag_case=True)
set_multi(flac, "GENRE", ["metal", "alternative metal"])
res = _grade_album(album, "EMBEDDED", case_cfg)
ok("GENRE_CASE" in res["tracks"][0]["issues"],
   f"a lowercase genre fails the case check (got {res['tracks'][0]['issues']})")
ok(any("GENRE 'metal' → 'Metal'" in i for i in res["issues"])
   and any("GENRE 'alternative metal' → 'Alternative Metal'" in i
           for i in res["issues"]),
   f"the issue names each value AND the spelling the writers produce "
   f"(got {res['issues']})")
ok(res["total_checks"] - res["pass_count"] == 1,
   f"and costs exactly one grade point, like every other value this check "
   f"finds wrong ({res['pass_count']}/{res['total_checks']})")
# ONE check per track: it is the tag-case check that owns the rule, so its
# switch is what turns the genre half off — and the check then leaves the
# denominator with it.
res_off = _grade_album(album, "EMBEDDED",
                       dict(case_cfg, grade_check_tag_case=False))
ok("GENRE_CASE" not in res_off["tracks"][0]["issues"]
   and res_off["total_checks"] == res["total_checks"] - 1
   and res_off["pass_count"] == res_off["total_checks"],
   f"grade_check_tag_case=False stops grading it ({res_off['total_checks']})")
# An unknown name is the VOCABULARY check's business: the writers keep it as it
# was typed (display_name leaves "Nonsense" alone), so a case complaint about
# it would be a second failure for one defect.
set_multi(flac, "GENRE", ["Nonsense"])
res = _grade_album(album, "EMBEDDED", dict(case_cfg, grade_check_genre_vocab=True))
ok("GENRE_CASE" not in res["tracks"][0]["issues"],
   f"an unknown name is not re-reported as a case problem "
   f"(got {res['tracks'][0]['issues']})")

# …and script 10 clears it: the grade and the fixer ask the SAME function
# (mlo.genres.display_name), so a value the grade fails is one the writer
# rewrites. What is graded here is what Format all produces.
set_multi(flac, "GENRE", ["metal", "alternative metal"])
from server import script_runners as _script_runners  # noqa: E402

_script_runners.run_script(10, {"music_folder": tmp, "targets": [flac],
                                "mb_genre_count": 2, "grade_verbose": False})
from mutagen.flac import FLAC as _FLAC  # noqa: E402

_fixed_genres = list(_FLAC(flac)["GENRE"])
ok(_fixed_genres == ["Metal", "Alternative Metal"],
   f"script 10 stores the writers' capitalization, family first "
   f"({_fixed_genres})")
res = _grade_album(album, "EMBEDDED", case_cfg)
ok("GENRE_CASE" not in res["tracks"][0]["issues"]
   and res["pass_count"] == res["total_checks"],
   f"and the grade clears on it ({res['pass_count']}/{res['total_checks']})")

set_tags(flac, dict(NO_MOOD, MOOD="melancholic"))

# ----------------------------------------------------------------------
# The excess-tag vocabulary vs the tags this grader REQUIRES
# ----------------------------------------------------------------------
print("== excess tags ==")
# The invariant the two halves of the audit rest on: a tag the grader DEMANDS
# (PER_TRACK_TAGS / ALBUM_TAGS) must never be one the strip passes would
# remove. `tag_key_allowed` is what Optimize FLACs (mlo.containers) and Format
# all (mlo.format_all) apply, so a name that fails it here is a tag the app
# writes and then strips — a grade no run could ever clear.
_not_allowed = [t for t in list(PER_TRACK_TAGS) + list(ALBUM_TAGS)
                if not tag_key_allowed(t)]
ok(not _not_allowed,
   f"every graded tag survives the strip pass (got {_not_allowed})")

# …and the report names EVERY excess tag it finds, plus the script that
# removes them: the names are what the user acts on, so a truncated list (it
# used to stop at six) hides the ones that matter.
from mutagen.flac import FLAC

_junk = FLAC(flac)
_junk["VENDOR_JUNK"] = ["1"]
_junk["RIPPED_BY"] = ["some ripper"]
_junk.save()
res = _grade_album(album, "EMBEDDED", dict(cfg, grade_check_excess_tags=True))
_excess = [i for i in res["issues"] if i.startswith("Excess tags:")]
ok(len(_excess) == 1
   and "ripped_by" in _excess[0].lower() and "vendor_junk" in _excess[0].lower(),
   f"the excess report names every tag (got {_excess})")
ok("Optimize FLACs (script 3)" in _excess[0] and "Format all (script 10)" in _excess[0],
   f"and the scripts that strip it (got {_excess})")
ok("TAGS" in res["tracks"][0]["issues"],
   f"the track carries the TAGS issue code (got {res['tracks'][0]['issues']})")
set_tags(flac, dict(NO_MOOD, MOOD="melancholic"))
del_tags(flac, ["VENDOR_JUNK", "RIPPED_BY"])

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
# AcoustID pair (REQUIRED: key or no key, script 21 creates it locally)
# ----------------------------------------------------------------------
print("== acoustid pair ==")
del_tags(flac, ["ACOUSTID_ID", "ACOUSTID_FINGERPRINT"])
base = _grade_album(album, "EMBEDDED", cfg)
# No API key at all: script 21 needs none (fpcalc takes the fingerprint
# locally and the id comes off the file), so its absence must not skip the
# check — the track fails for a tag nothing has written yet.
ac_cfg = dict(cfg, grade_check_acoustid=True, acoustid_api_key="")
res = _grade_album(album, "EMBEDDED", ac_cfg)
ok(res["total_checks"] == base["total_checks"] + 1
   and res["tracks"][0]["issues"] == ["ACOUSTID_ID", "ACOUSTID_FINGERPRINT"],
   f"a track with no AcoustID pair fails with an empty api key "
   f"(got {res['tracks'][0]['issues']})")
ok(any("Missing ACOUSTID_ID and ACOUSTID_FINGERPRINT" in i
       and "Fix AcoustID pairs" in i for i in res["issues"]),
   f"the issue names both halves and the script that writes them "
   f"(got {res['issues']})")
set_tags(flac, dict(NO_MOOD, MOOD="melancholic", ACOUSTID_ID="9f4e1d2c"))
res = _grade_album(album, "EMBEDDED", ac_cfg)
ok(res["tracks"][0]["issues"] == ["ACOUSTID_FINGERPRINT"],
   f"a lone ACOUSTID_ID fails naming the fingerprint "
   f"(got {res['tracks'][0]['issues']})")
set_tags(flac, dict(NO_MOOD, MOOD="melancholic",
                    ACOUSTID_FINGERPRINT="AQADtEmS"))
del_tags(flac, ["ACOUSTID_ID"])
res = _grade_album(album, "EMBEDDED", ac_cfg)
ok(res["tracks"][0]["issues"] == ["ACOUSTID_ID"],
   f"a lone ACOUSTID_FINGERPRINT fails naming the id "
   f"(got {res['tracks'][0]['issues']})")
set_tags(flac, dict(NO_MOOD, MOOD="melancholic", ACOUSTID_ID="9f4e1d2c",
                    ACOUSTID_FINGERPRINT="AQADtEmS"))
res = _grade_album(album, "EMBEDDED", ac_cfg)
ok(res["pass_count"] == res["total_checks"]
   and not any("ACOUSTID" in i for i in res["tracks"][0]["issues"]),
   f"a complete AcoustID pair passes ({res['pass_count']}/{res['total_checks']})")
# `acoustid_enabled` off is the family's own switch: script 21 is then a no-op
# (mlo.scripts.SCRIPT_GATES), so the check stands down instead of failing every
# track for a tag no pass could write. The baseline carries the same tags, so
# only the check itself differs.
off_base = _grade_album(album, "EMBEDDED", cfg)
res = _grade_album(album, "EMBEDDED",
                   dict(ac_cfg, acoustid_enabled=False))
ok(res["total_checks"] == off_base["total_checks"]
   and res["pass_count"] == res["total_checks"]
   and not any("ACOUSTID" in i for i in res["tracks"][0]["issues"]),
   f"acoustid_enabled=False stands the check down "
   f"({res['pass_count']}/{res['total_checks']} vs "
   f"{off_base['pass_count']}/{off_base['total_checks']})")
set_tags(flac, dict(NO_MOOD, MOOD="melancholic"))
del_tags(flac, ["ACOUSTID_ID", "ACOUSTID_FINGERPRINT"])

# ----------------------------------------------------------------------
# Locale alias (grade_check_alias_needed): a non-Latin name needs its alias
# ----------------------------------------------------------------------
print("== locale alias ==")
JA_TITLE = "君の名は"
# Naming off: the fixture's path is built from its Latin tags, so changing the
# TITLE to a Japanese one would fail grade_check_naming for a reason this case
# is not about (and the alias rule never depends on the path).
alias_cfg = dict(cfg, grade_check_alias_needed=True, grade_check_naming=False)
latin = _grade_album(album, "EMBEDDED", alias_cfg)
set_tags(flac, dict(NO_MOOD, MOOD="melancholic", TITLE=JA_TITLE))
res = _grade_album(album, "EMBEDDED", alias_cfg)
ok(res["tracks"][0]["issues"] == ["TITLEALIAS"],
   f"a non-Latin TITLE without its alias fails (got {res['tracks'][0]['issues']})")
ok(res["total_checks"] - res["pass_count"] == 1,
   f"and costs exactly one grade point "
   f"({res['pass_count']}/{res['total_checks']})")
# …and it is the TITLE's script that decides it: the Latin fixture is graded
# one check lighter, with the same tag set otherwise (TITLE is a per-track tag,
# so the generic hygiene sweep does not count it).
ok(res["total_checks"] == latin["total_checks"] + 1
   and latin["pass_count"] == latin["total_checks"],
   f"a Latin name is never graded for an alias "
   f"({latin['pass_count']}/{latin['total_checks']}, non-Latin "
   f"{res['pass_count']}/{res['total_checks']})")
ok(any("Missing TITLEALIAS" in i for i in res["issues"]),
   f"the issue names the alias tag to write (got {res['issues']})")
set_tags(flac, dict(NO_MOOD, MOOD="melancholic", TITLE=JA_TITLE,
                    TITLEALIAS="Kimi no na wa"))
res = _grade_album(album, "EMBEDDED", alias_cfg)
ok(res["pass_count"] == res["total_checks"]
   and not any("ALIAS" in i for i in res["tracks"][0]["issues"]),
   f"the alias tag satisfies it ({res['pass_count']}/{res['total_checks']})")
# The Latin title keeps its own check: a Latin TITLE is never graded, so only
# the non-Latin ARTIST is (exactly one needed check) — and the ALIAS spelling
# is the second half of the rule (R16b): a tag suffixed for a locale the app
# does not write (`en` is configured here) is excess even though it does
# satisfy the missing half.
set_tags(flac, dict(NO_MOOD, MOOD="melancholic", TITLE="Song",
                    ARTIST="宇多田ヒカル",
                    **{"ARTISTALIAS-JA": "Hikaru Utada"}))
del_tags(flac, ["TITLEALIAS"])
res = _grade_album(album, "EMBEDDED", alias_cfg)
ok("ARTISTALIAS" in res["tracks"][0]["issues"]
   and any("spelled for locale JA" in i for i in res["issues"]),
   f"a Latin title is never counted and the artist alias spelled for another "
   f"locale is excess ({res['pass_count']}/{res['total_checks']})")
# The bare spelling — the one the writers produce — satisfies both halves.
set_tags(flac, dict(NO_MOOD, MOOD="melancholic", TITLE="Song",
                    ARTIST="宇多田ヒカル", ARTISTALIAS="Hikaru Utada"))
del_tags(flac, ["ARTISTALIAS-JA", "TITLEALIAS"])
res = _grade_album(album, "EMBEDDED", alias_cfg)
ok(res["pass_count"] == res["total_checks"]
   and not any("ALIAS" in i for i in res["tracks"][0]["issues"]),
   f"the bare artist alias satisfies the needed half and is never excess "
   f"({res['pass_count']}/{res['total_checks']}, "
   f"{res['tracks'][0]['issues']} / {sorted(res['issues'])})")
# The ALBUM's own alias is graded the same way (it is written by the import and
# was graded by nothing before R16b): a non-Latin ALBUM without one fails, the
# bare tag satisfies it, and a Latin album title carrying one is excess.
set_tags(flac, dict(NO_MOOD, MOOD="melancholic", ALBUM="ファーストラブ"))
del_tags(flac, ["ARTISTALIAS"])
res = _grade_album(album, "EMBEDDED", alias_cfg)
ok("ALBUMALIAS" in res["tracks"][0]["issues"]
   and any("Missing ALBUMALIAS" in i for i in res["issues"]),
   f"a non-Latin ALBUM without its alias fails (got {res['tracks'][0]['issues']})")
set_tags(flac, dict(NO_MOOD, MOOD="melancholic", ALBUM="ファーストラブ",
                    ALBUMALIAS="First Love"))
res = _grade_album(album, "EMBEDDED", alias_cfg)
ok(not any("ALIAS" in i for i in res["tracks"][0]["issues"]),
   f"…and the album alias satisfies it (got {res['tracks'][0]['issues']})")
set_tags(flac, dict(NO_MOOD, MOOD="melancholic", ALBUM="First Love",
                    ALBUMALIAS="ファーストラブ"))
res = _grade_album(album, "EMBEDDED", alias_cfg)
ok("ALBUMALIAS" in res["tracks"][0]["issues"]
   and any("Unneeded ALBUMALIAS" in i for i in res["issues"]),
   f"a Latin album title carrying an alias is excess ({res['issues']})")
# The excess half is its own toggle: with it off, an unneeded alias costs
# nothing, and with `grade_check_alias_needed` off neither does a missing one.
raw = _grade_album(album, "EMBEDDED", dict(alias_cfg,
                                            grade_check_alias_excess=False))
ok(not any("ALIAS" in i for i in raw["issues"]),
   f"grade_check_alias_excess=False stops the excess half ({raw['issues']})")
# The needed half is a toggle too: with it off a name that needs an alias is
# neither failed nor counted. Back to a non-Latin title with no alias tags at
# all, so the one check under test is the missing half.
set_tags(flac, dict(NO_MOOD, MOOD="melancholic", TITLE=JA_TITLE))
del_tags(flac, ["ALBUMALIAS", "ARTISTALIAS", "TITLEALIAS", "ARTISTALIAS-JA",
                "TITLEALIAS-JA"])
off_alias = _grade_album(album, "EMBEDDED", alias_cfg)
res = _grade_album(album, "EMBEDDED", dict(alias_cfg, grade_check_alias_needed=False))
ok(res["total_checks"] == off_alias["total_checks"] - 1
   and res["pass_count"] == res["total_checks"]
   and not any("ALIAS" in i for i in res["tracks"][0]["issues"]),
   f"grade_check_alias_needed=False stops the whole check "
   f"({res['pass_count']}/{res['total_checks']} vs "
   f"{off_alias['pass_count']}/{off_alias['total_checks']})")
# Back to the fixture every block below grades: a Latin TITLE / ARTIST / ALBUM
# and no alias tags (which the other cases' partial cfgs would otherwise fail).
set_tags(flac, dict(NO_MOOD, MOOD="melancholic"))
del_tags(flac, ["TITLEALIAS", "ARTISTALIAS-JA", "ARTISTALIAS", "ALBUMALIAS"])

# ----------------------------------------------------------------------
# COMMENT: the one allow-listed NAME whose VALUE is junk
# ----------------------------------------------------------------------
print("== comment value ==")
excess_cfg = dict(cfg, grade_check_excess_tags=True, strip_unknown_tags=True)
plain = _grade_album(album, "EMBEDDED", excess_cfg)
set_tags(flac, dict(NO_MOOD, MOOD="melancholic", COMMENT="ripped by some tool"))
res = _grade_album(album, "EMBEDDED", excess_cfg)
ok(res["tracks"][0]["issues"] == ["COMMENT"],
   f"a non-empty COMMENT fails with its own code "
   f"(got {res['tracks'][0]['issues']})")
ok(res["total_checks"] - res["pass_count"] == 1
   and any("Comment tag carries a value" in i for i in res["issues"]),
   f"the value rule costs one grade point and is named "
   f"({res['pass_count']}/{res['total_checks']}, {res['issues']})")
# It is part of the excess check, so both switches gate it — a COMMENT nothing
# can clear must not fail (same tags, the strip switched off).
res = _grade_album(album, "EMBEDDED",
                   dict(excess_cfg, strip_unknown_tags=False))
ok(res["pass_count"] == res["total_checks"]
   and not any("COMMENT" in str(i) for i in res["tracks"][0]["issues"]),
   f"strip_unknown_tags=False stands the value rule down too "
   f"({res['pass_count']}/{res['total_checks']})")
set_tags(flac, dict(NO_MOOD, MOOD="melancholic"))
del_tags(flac, ["COMMENT"])

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

# …and the same goes for a 1.x log whose `Log checksum` line is gone (spec
# R30: present ⇒ must verify, absent ⇒ not required). The reader still reports
# 'missing' so the run log can name the case, but grading charges only a
# checksum that was there and did not verify.
_discs.check_log_checksum = lambda _p: ("missing", "no 'Log checksum' line")
try:
    res = _grade_album(cd_dir, "EMBEDDED", _ck_cfg)
    ok("LOG_CHECKSUM" not in res["tracks"][0]["issues"],
       f"an absent log checksum is not required, so it is not failed "
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

# ...and a disc numbered RELEASE-wide against a PER-DISC log. The owner's own
# library: The Wall (US CD C2K 36183) numbers its second disc's files 14-26 —
# the CD's own continuous numbering, which is what the TOC states and what
# this app writes — while CD-2.log numbers that disc 1-13. The two share no
# track number, so the rows are aligned BY ORDER, guarded by the log's TOC
# playtime: 13 of 13 matched on the real album where the old rule matched 0.
cont_dir = os.path.join(music, "Artists", "Artist", "Continuous Discs (2020)")
os.makedirs(cont_dir, exist_ok=True)
for _name in ("1-01 Song A.flac", "2-05 Song B.flac", "2-06 Song C.flac"):
    _p = os.path.join(cont_dir, _name)
    make_flac(_p)
    set_tags(_p, dict(FULL, MEDIA="CD", DISCNUMBER=_name[0], DISCTOTAL="2"))
with open(os.path.join(cont_dir, "CD-1.log"), "w", encoding="utf-8") as fh:
    fh.write("disc 1\n")
with open(os.path.join(cont_dir, "CD-2.log"), "w", encoding="utf-8") as fh:
    fh.write("disc 2\n")
_orig_secs, _orig_fsecs = _discs.parse_log_track_seconds, _discs._file_seconds
try:
    _discs.read_log_text = lambda p: os.path.basename(p)
    _discs.parse_log_checksums = lambda text: (
        {1: "AAAAAAAA"} if "CD-1" in text else {1: "BBBBBBBB", 2: "CCCCCCCC"})
    # The log's TOC and the files' own playtimes agree: the order is the same
    # tracklist, so the alignment is allowed.
    _discs.parse_log_track_seconds = lambda text: (
        {1: 10.0} if "CD-1" in text else {1: 20.0, 2: 30.0})
    _discs._file_seconds = lambda p: (
        10.0 if "1-01" in p else (20.0 if "2-05" in p else 30.0))
    res = _grade_album(cont_dir, "EMBEDDED", crc_cfg)
finally:
    _discs.read_log_text = _orig_read
    _discs.parse_log_checksums = _orig_parse
    _discs.parse_log_track_seconds, _discs._file_seconds = _orig_secs, _orig_fsecs
_crc_keys = [k for k in res["issues"] if "not covered by .log CRC" in k]
ok(not _crc_keys,
   f"a disc numbered 2-05/2-06 is covered by the per-disc log's rows 1/2 "
   f"({res['issues']})")

# And the guard: the same shape with a TOC that names OTHER tracks stays
# uncovered rather than taking a checksum that belongs to another track.
try:
    _discs.read_log_text = lambda p: os.path.basename(p)
    _discs.parse_log_checksums = lambda text: (
        {1: "AAAAAAAA"} if "CD-1" in text else {1: "BBBBBBBB", 2: "CCCCCCCC"})
    _discs.parse_log_track_seconds = lambda text: (
        {1: 10.0} if "CD-1" in text else {1: 300.0, 2: 400.0})
    _discs._file_seconds = lambda p: (
        10.0 if "1-01" in p else (20.0 if "2-05" in p else 30.0))
    res = _grade_album(cont_dir, "EMBEDDED", crc_cfg)
finally:
    _discs.read_log_text = _orig_read
    _discs.parse_log_checksums = _orig_parse
    _discs.parse_log_track_seconds, _discs._file_seconds = _orig_secs, _orig_fsecs
_crc_keys = [k for k in res["issues"] if "not covered by .log CRC" in k]
ok(len(_crc_keys) == 1 and "2-05 Song B.flac" in res["issues"][_crc_keys[0]],
   f"a TOC that names other tracks refuses the order alignment "
   f"({res['issues']})")

# ----------------------------------------------------------------------
# XLD rip logs: the owner's own log, read and compared like an EAC one
# ----------------------------------------------------------------------
print("== XLD rip logs ==")
# The owner's log verbatim (Radiohead - A Moon Shaped Pool, XLD 20151214):
# its TOC separates the FRAMES field with ':' ("00:00:00" / "03:41:34") where
# EAC uses '.' ("0:00.00" / "3:13.27"), and each track prints THREE CRC32
# lines, of which only the plain `CRC32 hash` is the decoded PCM's CRC-32
# (EAC's "Copy CRC").
# `Filename` names the RIPPER's Mac folder, not the file on disk. The
# AccurateRip summary, per-track gain/peak, statistics and the XLD signature
# block are elided: no reader touches them.
XLD_SAMPLE = """\
X Lossless Decoder version 20151214 (149.1)

XLD extraction logfile from 2016-06-16 12:47:40 -0400

Radiohead / A Moon Shaped Pool

Used drive : MATSHITA DVD-R   UJ-8A8 (revision HA13)
Media type : Pressed CD

TOC of the extracted CD
     Track |   Start  |  Length  | Start sector | End sector 
    ---------------------------------------------------------
        1  | 00:00:00 | 03:41:34 |         0    |    16608   
        2  | 03:41:34 | 06:24:41 |     16609    |    45449   
        3  | 10:06:00 | 04:41:00 |     45450    |    66524   
        4  | 14:47:00 | 03:44:44 |     66525    |    83368   
        5  | 18:31:44 | 06:07:26 |     83369    |   110919   
        6  | 24:38:70 | 02:52:72 |    110920    |   123891   
        7  | 27:31:67 | 04:26:48 |    123892    |   143889   
        8  | 31:58:40 | 05:45:67 |    143890    |   169831   
        9  | 37:44:32 | 05:06:42 |   169832    |   192823   
       10  | 42:50:74 | 05:03:61 |    192824    |   215609   
       11  | 47:54:60 | 04:45:38 |    215610    |   237022   

Track 01
    Filename : /Users/baconfat/Desktop/XLD rips/Radiohead - A Moon Shaped Pool (2016) [FLAC]/01 Burn the Witch.flac
    Pre-gap length : 00:02:00

    CRC32 hash (test run)    : B03E4096
    CRC32 hash               : B03E4096
    CRC32 hash (skip zero)   : C8A48455
    AccurateRip v1 signature : 2E9F2E06

Track 02
    Filename : /Users/baconfat/Desktop/XLD rips/Radiohead - A Moon Shaped Pool (2016) [FLAC]/02 Daydreaming.flac

    CRC32 hash (test run)    : 97984411
    CRC32 hash               : 97984411
    CRC32 hash (skip zero)   : BC43CB3E
    AccurateRip v1 signature : 48690D6A

Track 03
    Filename : /Users/baconfat/Desktop/XLD rips/Radiohead - A Moon Shaped Pool (2016) [FLAC]/03 Decks Dark.flac

    CRC32 hash (test run)    : D8CA4842
    CRC32 hash               : D8CA4842
    CRC32 hash (skip zero)   : 07877CFE
    AccurateRip v1 signature : 4C49EFB9

Track 04
    Filename : /Users/baconfat/Desktop/XLD rips/Radiohead - A Moon Shaped Pool (2016) [FLAC]/04 Desert Island Disk.flac

    CRC32 hash (test run)    : FA6BE23A
    CRC32 hash               : FA6BE23A
    CRC32 hash (skip zero)   : 19571766
    AccurateRip v1 signature : 311ECF77

Track 05
    Filename : /Users/baconfat/Desktop/XLD rips/Radiohead - A Moon Shaped Pool (2016) [FLAC]/05 Ful Stop.flac

    CRC32 hash (test run)    : 98C9C7E1
    CRC32 hash               : 98C9C7E1
    CRC32 hash (skip zero)   : 40A68EB9
    AccurateRip v1 signature : 4AFC0E03

Track 06
    Filename : /Users/baconfat/Desktop/XLD rips/Radiohead - A Moon Shaped Pool (2016) [FLAC]/06 Glass Eyes.flac

    CRC32 hash (test run)    : BDF00174
    CRC32 hash               : BDF00174
    CRC32 hash (skip zero)   : 8F050E1A
    AccurateRip v1 signature : 715CBE19

Track 07
    Filename : /Users/baconfat/Desktop/XLD rips/Radiohead - A Moon Shaped Pool (2016) [FLAC]/07 Identikit.flac

    CRC32 hash (test run)    : 8FDF7B74
    CRC32 hash               : 8FDF7B74
    CRC32 hash (skip zero)   : C32AD1B6
    AccurateRip v1 signature : FA54AA56

Track 08
    Filename : /Users/baconfat/Desktop/XLD rips/Radiohead - A Moon Shaped Pool (2016) [FLAC]/08 The Numbers.flac
    Pre-gap length : 00:01:67

    CRC32 hash (test run)    : 3CFA6ED7
    CRC32 hash               : 3CFA6ED7
    CRC32 hash (skip zero)   : F28A74F9
    AccurateRip v1 signature : 9B7B80B1

Track 09
    Filename : /Users/baconfat/Desktop/XLD rips/Radiohead - A Moon Shaped Pool (2016) [FLAC]/09 Present Tense.flac
    Pre-gap length : 00:01:65

    CRC32 hash (test run)    : A3661996
    CRC32 hash               : A3661996
    CRC32 hash (skip zero)   : D1891229
    AccurateRip v1 signature : 529DB860

Track 10
    Filename : /Users/baconfat/Desktop/XLD rips/Radiohead - A Moon Shaped Pool (2016) [FLAC]/10 Tinker Tailor Soldier Sailor Rich Man Poor Man Beggar Thief.flac
    Pre-gap length : 00:01:62

    CRC32 hash (test run)    : 7A9222A7
    CRC32 hash               : 7A9222A7
    CRC32 hash (skip zero)   : 5A2FDF70
    AccurateRip v1 signature : 8069EDD4

Track 11
    Filename : /Users/baconfat/Desktop/XLD rips/Radiohead - A Moon Shaped Pool (2016) [FLAC]/11 True Love Waits.flac

    CRC32 hash (test run)    : FFFA8D15
    CRC32 hash               : FFFA8D15
    CRC32 hash (skip zero)   : B19E75A4
    AccurateRip v1 signature : A50951E4

No errors occurred

End of status report
"""
_XLD_CRCS = {1: "B03E4096", 2: "97984411", 3: "D8CA4842", 4: "FA6BE23A",
             5: "98C9C7E1", 6: "BDF00174", 7: "8FDF7B74", 8: "3CFA6ED7",
             9: "A3661996", 10: "7A9222A7", 11: "FFFA8D15"}
_XLD_SECS = {1: 221.45, 2: 384.55, 3: 281.0, 4: 224.59, 5: 367.35,
             6: 172.96, 7: 266.64, 8: 345.89, 9: 306.56, 10: 303.81, 11: 285.51}
ok(_discs.parse_log_checksums(XLD_SAMPLE) == _XLD_CRCS,
   f"an XLD log's per-track CRC is the plain `CRC32 hash` "
   f"({_discs.parse_log_checksums(XLD_SAMPLE)})")
ok(_discs.parse_log_checksums(XLD_SAMPLE)[1] != "C8A48455"
   and _discs.parse_log_checksums(XLD_SAMPLE)[1] != "2E9F2E06",
   "`CRC32 hash (skip zero)` and the AccurateRip signature are never read "
   "as the track's CRC")
ok({k: round(v, 2)
    for k, v in _discs.parse_log_track_seconds(XLD_SAMPLE).items()} == _XLD_SECS,
   "the XLD TOC (mm:ss:ff) states each playtime "
   f"({_discs.parse_log_track_seconds(XLD_SAMPLE)})")
ok(abs(_discs.parse_log_toc_seconds(XLD_SAMPLE) - 3160.3066666666666) < 1e-6,
   f"and totals the disc ({_discs.parse_log_toc_seconds(XLD_SAMPLE)})")
ok([round(r["seconds"], 2) for r in _discs.log_track_rows(XLD_SAMPLE)]
   == [_XLD_SECS[i] for i in range(1, 12)],
   "so a tracklist row read off the XLD log carries its playtime, like EAC's")
# The ripper's own Mac path still names the local file: the pairing is by the
# normalised name, so "01 Burn the Witch.flac" and "01 - Burn the Witch.flac"
# are the same track. Nothing in the CRC chain reads filenames anyway.
ok(_discs._norm_name(_discs.parse_log_track_files(XLD_SAMPLE)[1])
   == _discs._norm_name("01 - Burn the Witch.flac"),
   "the RIPPER's own Filename still names the local file once normalised")

# …and the TOC is what attributes a log to its disc when its NAME does not
# (rename_logs_for_discs step 3, which the real owner's log needs): the two
# discs below are named D-TT, the log carries no disc number, and the playtime
# totals are distinct by far more than the tolerance.
xld_dir = os.path.join(music, "Artists", "Artist", "XLD Discs (2016)")
os.makedirs(xld_dir, exist_ok=True)
for _name in ("1-01 Track.flac", "1-02 Track.flac",
              "2-01 Track.flac", "2-02 Track.flac"):
    _p = os.path.join(xld_dir, _name)
    make_flac(_p)
    set_tags(_p, dict(FULL, MEDIA="CD", DISCNUMBER=_name[0], DISCTOTAL="2"))
with open(os.path.join(xld_dir, "Radiohead - A Moon Shaped Pool.log"),
          "w", encoding="utf-8") as fh:
    fh.write(XLD_SAMPLE)
_orig_asec = _discs._audio_seconds
try:
    # disc 1 really is the log's disc (3160.31 s), disc 2 is another one
    _discs._audio_seconds = lambda paths: (
        3160.3066666666666 if any("1-01" in p for p in paths) else 2938.85)
    _xld_notes = _discs.rename_logs_for_discs(
        xld_dir, config={"discs_rename_enabled": True,
                         "discs_rename_single_fallback": True})
finally:
    _discs._audio_seconds = _orig_asec
ok(("Radiohead - A Moon Shaped Pool.log", "CD-1.log") in _xld_notes,
   f"the XLD TOC attributes the log to its own disc ({_xld_notes})")

# The grader then compares that log's CRC against the audio, exactly as it
# does an EAC one — coverage by number, then the VALUE.
if _FFMPEG:
    _real_crc_cd = _discs._audio_crc32(_FFMPEG, cd_flac)

    def _write_xld_log(plain, test_run, skip_zero):
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(
                "X Lossless Decoder version 20151214 (149.1)\n\n"
                "XLD extraction logfile from 2016-06-16 12:47:40 -0400\n\n"
                "Artist / Album\n\nTOC of the extracted CD\n"
                "     Track |   Start  |  Length  | Start sector"
                " | End sector \n"
                "    -------------------------------------------------"
                "---------\n"
                "        1  | 00:00:00 | 00:03:00 |         0    |"
                "     2249   \n\n"
                "Track 01\n"
                "    Filename : /Users/someone/Desktop/XLD rips/01 Song.flac\n\n"
                f"    CRC32 hash (test run)    : {test_run}\n"
                f"    CRC32 hash               : {plain}\n"
                f"    CRC32 hash (skip zero)   : {skip_zero}\n"
                "    AccurateRip v1 signature : DEADBEEF\n")

    _write_xld_log(_real_crc_cd, _real_crc_cd, "00000000")
    res = _grade_album(cd_dir, "EMBEDDED", _crc_cfg)
    ok("CRC" not in res["tracks"][0]["issues"],
       f"an XLD log covers and matches the track ({res['tracks'][0]['issues']})")
    _write_xld_log("00000000", "00000000", _real_crc_cd)
    res = _grade_album(cd_dir, "EMBEDDED", _crc_cfg)
    ok("CRC_MISMATCH" in res["tracks"][0]["issues"],
       "a wrong plain CRC32 hash fails even when `(skip zero)` holds the real "
       f"value ({res['tracks'][0]['issues']})")
    _write_xld_log("00000000", _real_crc_cd, "00000000")
    res = _grade_album(cd_dir, "EMBEDDED", _crc_cfg)
    ok("CRC_MISMATCH" in res["tracks"][0]["issues"],
       "and `(test run)` does not stand in for the plain line either")

# ----------------------------------------------------------------------
# A second XLD log — The King of Limbs, TICK001CD — teaches the verdict to
# say WHAT KIND of mismatch it is: same audio with different silence, or a
# different transfer of the same CD.
# ----------------------------------------------------------------------
print("== XLD log CRC mismatch: what it says ==")
# The owner's CD-1.log (XLD 20110312, ripped 2011-03-24 by another person,
# drive HL-DT-ST GS23N, read offset 667), verbatim TOC and CRC lines; its
# AccurateRip/gain/statistics blocks are elided (no reader touches them).
KOL_SAMPLE = """\
X Lossless Decoder version 20110312 (130.0)

XLD extraction logfile from 2011-03-24 16:30:03 +1100

Radiohead / The King Of Limbs

Used drive : HL-DT-ST DVDRW  GS23N (revision SB03)

Ripper mode             : CDParanoia III 10.2
Read offset correction  : 667
Gap status              : Analyzed, Appended

TOC of the extracted CD
     Track |   Start  |  Length  | Start sector | End sector 
    ---------------------------------------------------------
        1  | 00:00:00 | 05:14:38 |         0    |    23587   
        2  | 05:14:38 | 04:40:54 |     23588    |    44641   
        3  | 09:55:17 | 04:27:10 |     44642    |    64676   
        4  | 14:22:27 | 03:12:56 |     64677    |    79132   
        5  | 17:35:08 | 05:00:30 |     79133    |   101662   
        6  | 22:35:38 | 04:46:67 |    101663    |   123179   
        7  | 27:22:30 | 04:50:05 |    123180    |   144934   
        8  | 32:12:35 | 05:21:32 |    144935    |   169041   

Track 01
    Filename : /Users/hamishduncan/Desktop/Radiohead - The King Of Limbs - FLAC Log Cue - 2011/01 Radiohead - Bloom.flac
    Pre-gap length : 00:02:00

    CRC32 hash (test run)  : 15788F23
    CRC32 hash             : 15788F23
    CRC32 hash (skip zero) : 3EDA7C1D
    AccurateRip signature  : 35FFC532

Track 02
    Filename : /Users/hamishduncan/Desktop/Radiohead - The King Of Limbs - FLAC Log Cue - 2011/02 Radiohead - Morning Mr. Magpie.flac

    CRC32 hash (test run)  : 006AE096
    CRC32 hash             : 006AE096
    CRC32 hash (skip zero) : 18B4BD58
    AccurateRip signature  : 3694A865

Track 03
    Filename : /Users/hamishduncan/Desktop/Radiohead - The King Of Limbs - FLAC Log Cue - 2011/03 Radiohead - Little By Little.flac

    CRC32 hash (test run)  : B168532D
    CRC32 hash             : B168532D
    CRC32 hash (skip zero) : 737B1AEB
    AccurateRip signature  : 872974A5

Track 04
    Filename : /Users/hamishduncan/Desktop/Radiohead - The King Of Limbs - FLAC Log Cue - 2011/04 Radiohead - Feral.flac

    CRC32 hash (test run)  : 0DCADDD5
    CRC32 hash             : 0DCADDD5
    CRC32 hash (skip zero) : FC5BA282
    AccurateRip signature  : B8889C07

Track 05
    Filename : /Users/hamishduncan/Desktop/Radiohead - The King Of Limbs - FLAC Log Cue - 2011/05 Radiohead - Lotus Flower.flac
    Pre-gap length : 00:02:52

    CRC32 hash (test run)  : 991894A9
    CRC32 hash             : 991894A9
    CRC32 hash (skip zero) : 3EC9C065
    AccurateRip signature  : 357207BD

Track 06
    Filename : /Users/hamishduncan/Desktop/Radiohead - The King Of Limbs - FLAC Log Cue - 2011/06 Radiohead - Codex.flac

    CRC32 hash (test run)  : 03A384D8
    CRC32 hash             : 03A384D8
    CRC32 hash (skip zero) : 3380E15C
    AccurateRip signature  : 305B418D

Track 07
    Filename : /Users/hamishduncan/Desktop/Radiohead - The King Of Limbs - FLAC Log Cue - 2011/07 Radiohead - Give Up the Ghost.flac

    CRC32 hash (test run)  : 81B447D2
    CRC32 hash             : 81B447D2
    CRC32 hash (skip zero) : 23CD5A55
    AccurateRip signature  : 0A6CCB9D

Track 08
    Filename : /Users/hamishduncan/Desktop/Radiohead - The King Of Limbs - FLAC Log Cue - 2011/08 Radiohead - Separator.flac

    CRC32 hash (test run)  : 3910D07B
    CRC32 hash             : 3910D07B
    CRC32 hash (skip zero) : 591ADF75
    AccurateRip signature  : 0115E91B

No errors occurred

End of status report
"""
_KOL_PLAIN = {1: "15788F23", 2: "006AE096", 3: "B168532D", 4: "0DCADDD5",
              5: "991894A9", 6: "03A384D8", 7: "81B447D2", 8: "3910D07B"}
_KOL_SKIP = {1: "3EDA7C1D", 2: "18B4BD58", 3: "737B1AEB", 4: "FC5BA282",
             5: "3EC9C065", 6: "3380E15C", 7: "23CD5A55", 8: "591ADF75"}
ok(_discs.parse_log_checksums(KOL_SAMPLE) == _KOL_PLAIN,
   "the KoL log's plain CRCs are read")
ok(_discs.parse_log_skip_zero_checksums(KOL_SAMPLE) == _KOL_SKIP,
   "and its `CRC32 hash (skip zero)` variant, per track, beside them")
ok(len(_discs.parse_log_track_seconds(KOL_SAMPLE)) == 8
   and round(_discs.parse_log_track_seconds(KOL_SAMPLE)[1], 4) == 314.5067
   and round(_discs.parse_log_toc_seconds(KOL_SAMPLE), 3) == 2253.893,
   "its TOC (mm:ss:ff) is read too")
ok(not _discs.parse_log_skip_zero_checksums(XLD_SAMPLE).get(0)
   and set(_discs.parse_log_skip_zero_checksums(XLD_SAMPLE)) == set(range(1, 12)),
   "an EAC log answers {} for the variant — the caller then keeps its wording")

def _xld_wording_checks():
    """The variant's RULE and the two verdict wordings, end to end.

    XLD's `(skip zero)` is the CRC-32 of the decoded PCM with every zero
    sample omitted: pinned here independently of any log — a FLAC whose
    samples are known in this file, crc32'd here, has to come back as those
    values — and then used to drive the grader's wording through a real
    `_grade_album` run.
    """
    import struct as _struct
    import wave as _wave
    import zlib as _zlib

    pattern = ([(0, 0)] * 64
               + [(1234, -1234), (0, 0), (321, 654), (0, 0)] * 8
               + [(0, 0)] * 64)

    def frames(samples):
        return b"".join(_struct.pack("<hh", left, right)
                        for left, right in samples)

    z_plain = format(_zlib.crc32(frames(pattern)) & 0xFFFFFFFF, "08X")
    z_skip = format(_zlib.crc32(frames(
        [lr for lr in pattern if lr[0] or lr[1]])) & 0xFFFFFFFF, "08X")

    w_dir = os.path.join(music, "Artists", "Artist", "XLD Wording (2020)")
    os.makedirs(w_dir, exist_ok=True)
    w_flac = os.path.join(w_dir, "01 - Song.flac")
    wav = w_flac + ".wav"
    with _wave.open(wav, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(frames(pattern))
    subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", w_flac, wav],
                   check=True, capture_output=True)
    os.remove(wav)
    set_tags(w_flac, dict(FULL, MEDIA="CD"))
    w_log = os.path.join(w_dir, "CD-1.log")

    def write_log(plain, test_run, skip_zero):
        """A one-track XLD log over w_flac; skip_zero=None writes an
        EAC-shaped block with no such line."""
        lines = ["X Lossless Decoder version 20151214 (149.1)", "",
                 "XLD extraction logfile from 2016-06-16 12:47:40 -0400", "",
                 "Artist / Album", "", "TOC of the extracted CD",
                 "     Track |   Start  |  Length  | Start sector"
                 " | End sector ",
                 "    -------------------------------------------------"
                 "---------\n"
                 "        1  | 00:00:00 | 00:00:10 |         0    |"
                 "      749   \n\nTrack 01",
                 "    Filename : /Users/someone/Desktop/XLD rips/01 Song.flac",
                 "",
                 f"    CRC32 hash (test run)    : {test_run}",
                 f"    CRC32 hash               : {plain}"]
        if skip_zero is not None:
            lines.append(f"    CRC32 hash (skip zero)   : {skip_zero}")
        with open(w_log, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    ok(_discs._audio_crc32(_FFMPEG, w_flac) == z_plain
       and _discs._audio_crc32(_FFMPEG, w_flac, skip_zero=True) == z_skip,
       "the app's skip-zero mode is XLD's own rule — the plain CRC and the "
       "CRC with every zero sample omitted, both reproduced from known "
       f"samples ({_discs._audio_crc32(_FFMPEG, w_flac)} / "
       f"{_discs._audio_crc32(_FFMPEG, w_flac, skip_zero=True)} vs "
       f"{z_plain} / {z_skip})")

    write_log(z_plain, z_plain, z_skip)
    res = _grade_album(w_dir, "EMBEDDED", _crc_cfg)
    ok("CRC" not in res["tracks"][0]["issues"]
       and not any("silence" in i or "DIFFERENT audio" in i
                   for i in res["issues"]),
       f"a matching XLD log still passes silently "
       f"({res['tracks'][0]['issues']})")

    # Same audio, different silence: the plain CRC differs but the skip-zero
    # one agrees — the verdict says so instead of accusing the app.
    write_log("11111111", "11111111", z_skip)
    res = _grade_album(w_dir, "EMBEDDED", _crc_cfg)
    ok("CRC_MISMATCH" in res["tracks"][0]["issues"]
       and any("leading/trailing silence" in i for i in res["issues"]),
       "plain differs but skip-zero matches → the silence wording "
       f"({res['issues']})")
    ok(any(z_skip in i and "11111111" in i for i in res["issues"]),
       "and it quotes both values it compared")

    # The KoL case: neither variant matches -> the log is another transfer.
    write_log(_KOL_PLAIN[1], _KOL_PLAIN[1], _KOL_SKIP[1])
    res = _grade_album(w_dir, "EMBEDDED", _crc_cfg)
    ok("CRC_MISMATCH" in res["tracks"][0]["issues"]
       and any("DIFFERENT audio of the same length" in i for i in res["issues"])
       and any("Keep the rip whose log matches" in i for i in res["issues"]),
       f"neither variant matches → the different-transfer wording ({res['issues']})")
    ok(any(_KOL_SKIP[1] in i and _KOL_PLAIN[1] in i for i in res["issues"]),
       "naming the log's own values, and what the user can do")

    # An EAC log prints no `(skip zero)` line: its verdict is unchanged.
    write_log("11111111", "11111111", None)
    res = _grade_album(w_dir, "EMBEDDED", _crc_cfg)
    ok(any("does not match its own log" in i for i in res["issues"])
       and not any("silence" in i or "DIFFERENT audio" in i
                   for i in res["issues"]),
       "an EAC-shaped mismatch keeps the original sentence "
       f"({res['issues']})")

    # An XLD log the app cannot checksum does NOT block the CD verdict. The
    # log's own SHA256 half reads 'unsupported' (checksum_status NONE) and,
    # per spec R30, that is not a missing leg to charge: the 'checksums' leg is
    # the per-track CRC comparison the app computes itself. With that matching
    # and a REAL .accurip, all three legs are ok and the disc reads REAL — so a
    # perfectly verifiable XLD rip is not locked out of the verdict.
    from mlo.accurip import _canonical_accurip_text  # noqa: E402

    with open(os.path.join(w_dir, "CD-1.accurip"), "w", encoding="utf-8") as fh:
        fh.write(_canonical_accurip_text(
            "[CUETools log; Date: 2026-01-01 00:00:00; Version: 2.2.6]\n"
            "[AccurateRip ID: 00000001-00000001-00000001]\n\n"
            "Track   [  CRC   |   V2   ] Status\n"
            " 01     [00000001|00000002] (V1+V2/Y) Accurately ripped\n"))
    set_tags(w_flac, {"LOG_GRADE": "100"})
    write_log(z_plain, z_plain, z_skip)
    res = _grade_album(w_dir, "EMBEDDED", dict(
        _crc_cfg, audit_log_score_threshold=100,
        audit_require_accuraterip=True, audit_verify_log_checksum=True))
    tr0 = res["tracks"][0]
    ok(tr0.get("checksum_status") == "NONE"
       and tr0.get("audit_legs") == {"log-score": "ok", "checksums": "ok",
                                     "accuraterip": "ok"}
       and tr0.get("audit") == "REAL",
       "an XLD log the app cannot checksum is not a missing leg: a matching "
       "per-track CRC plus a REAL .accurip reads REAL "
       f"({tr0.get('audit_legs')} / {tr0.get('audit')} / "
       f"{tr0.get('checksum_status')})")
    ok(not any("nothing established the CD verdict's 'checksums'"
               in i for i in res["issues"]),
       "and nothing charges the disc for the log checksum it cannot read "
       f"({res['issues']})")


if _FFMPEG:
    _xld_wording_checks()
else:
    print("  skipped: no ffmpeg — the XLD mismatch wording is not graded here")

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
   f"an album WITH a manifest is not failed for the manifest "
   f"({stats_man_yes['issue_counts']})")
ok(stats_man_yes["grade_dist"] == {"PASS": 1, "FAIL": 0},
   f"and every row of it is in the folder, so it grades PASS "
   f"({stats_man_yes['grade_dist']})")

# A folder holding PART of the recorded tracklist: 1 of the manifest's 2 rows
# is on disk. The page could always say "1 of 2 tracks …"; the grade now charges
# for it, because a rip missing a track is a broken album rather than a small
# one (the owner's ask: 14 of a CD's 15 tracks must fail).
save_expected_tracks(mn_dir, "11111111-1111-1111-1111-111111111111",
                     [{"disc": 1, "position": 1, "title": "Song",
                       "recording_mbid": None},
                      {"disc": 1, "position": 2, "title": "Second Song",
                       "recording_mbid": None}])
stats_man_short, lines_man_short = graded(dict(MAN_CFG))
short_row = row_line(lines_man_short, mn_rel)
ok(stats_man_short["issue_counts"].get(EXPECTED_TRACKS_INCOMPLETE) == 1,
   f"an album holding 1 of the 2 tracks its tracklist names reports one "
   f"EXPECTED_TRACKS_INCOMPLETE ({stats_man_short['issue_counts']})")
ok(short_row is not None and "✕" in short_row and "FAIL" in short_row,
   f"the incomplete album is a FAILED row ({short_row})")
ok(stats_man_short["grade_dist"] == {"PASS": 0, "FAIL": 1},
   f"it does not grade PASS ({stats_man_short['grade_dist']})")

# The missing track arrives: the same album is whole and passes again — the
# failure is the absent file, not the manifest.
mn_flac2 = album_path(mn_music, dict(mn_tags, TRACKNUMBER="2", TITLE="Second Song"))
make_flac(mn_flac2)
set_tags(mn_flac2, dict(mn_tags, TRACKNUMBER="2", TITLE="Second Song"))
stats_man_whole, lines_man_whole = graded(dict(MAN_CFG))
ok(EXPECTED_TRACKS_INCOMPLETE not in stats_man_whole["issue_counts"],
   f"the second track's arrival clears the failure "
   f"({stats_man_whole['issue_counts']})")
ok(stats_man_whole["grade_dist"] == {"PASS": 1, "FAIL": 0},
   f"and the album grades PASS again ({stats_man_whole['grade_dist']})")

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

# ----------------------------------------------------------------------
# %genre% is the same value on both sides of an import
# ----------------------------------------------------------------------
# One file, two readers: the beets import evaluates the naming script from its
# own item (server.beets.mloplugin), the organizer evaluates it from the
# file's tags (mlo.naming.track_variables). GENRE is a LIST on the tag side —
# the tag layer joins repeated GENRE fields with "; " and track_variables
# passes that through, while RELEASECOUNTRY and LABEL are the fields that
# DELIBERATELY reduce to their first value (mlo.naming._first_multi, spec
# R33) — so a two-genre file must yield the same %genre% and the same path on
# both sides. It did not: beets' own `genre` field is mediafile's
# `genres.single_field()`, the FIRST genre, and the import used it alone.
print("== %genre% is the same value on both sides of an import ==")
from mlo.audio import AudioFile as _AudioFile  # noqa: E402

_gdir = os.path.join(tmp, "GenreAgreement")
os.makedirs(_gdir, exist_ok=True)
_gflac = os.path.join(_gdir, "1-01 Song.flac")
make_flac(_gflac)
from mutagen.flac import FLAC as _GFLAC  # noqa: E402

_gf = _GFLAC(_gflac)
_gf["GENRE"] = ["Rock", "Shoegaze"]
_gf["TITLE"] = "Song"
_gf.save()
_genre_vars = track_variables(_AudioFile(_gflac).all_tags())
ok(_genre_vars["genre"] == "Rock; Shoegaze",
   f"the organizer's %genre% is the whole list ({_genre_vars['genre']!r})")

# The import side, against the vendored/installed beets itself (CI has
# neither: the check says so and moves on, the same way the date suite does).
_beets_dir = next((os.path.join(_deps, d) for d in sorted(os.listdir(_deps))
                   if d.lower().startswith("beets")), "") if os.path.isdir(_deps) else ""
if _beets_dir:
    sys.path.insert(0, _beets_dir)
try:
    from beets.library import Item as _BeetsItem

    from server.beets import mloplugin as _mloplugin
except Exception as _beets_err:  # noqa: BLE001 - no beets here: nothing to compare
    print(f"  skipped: beets naming-variable check ({_beets_err})")
else:
    # beets' item for that file: its own genre field holds what mediafile's
    # single-value `genre` gave it — the FIRST genre, which is exactly the
    # value the import used to evaluate the script with.
    _item = _BeetsItem(path=_gflac, title="Song", album="Amnesia",
                       albumartist="Artist", artist="Artist", genre="Rock")
    _item_vars = _mloplugin._item_naming_vars(_item)
    ok(_item_vars["genre"] == _genre_vars["genre"],
       f"the beets import computes the SAME %genre% "
       f"({_item_vars['genre']!r} vs {_genre_vars['genre']!r})")
    _genre_script = "%genre% - %title%"
    ok(eval_script(_genre_script, _item_vars)
       == eval_script(_genre_script, _genre_vars),
       f"…and the same path from it "
       f"({eval_script(_genre_script, _item_vars)!r} vs "
       f"{eval_script(_genre_script, _genre_vars)!r})")
    # The item is still the fallback: a file whose own GENRE is empty (a
    # download the genre chain has not filled yet) keeps what beets states,
    # and a path beets hands over that cannot be read does not crash the
    # naming evaluation.
    _bare_flac = os.path.join(_gdir, "1-02 Bare.flac")
    make_flac(_bare_flac)
    ok(_mloplugin._item_naming_vars(
        _BeetsItem(path=_bare_flac, title="Bare", genre="Jazz"))["genre"] == "Jazz",
       "a file with no GENRE of its own keeps beets' value")
    ok(_mloplugin._item_naming_vars(
        _BeetsItem(path=os.path.join(_gdir, "gone.flac"), title="Gone",
                   genre="Jazz"))["genre"] == "Jazz",
       "and an unreadable path falls back to it instead of failing the name")

# The percentage EVERY surface prints (the library header, an album row, the
# grading strip) comes from one rule: 100 belongs to a score with nothing
# failed. The owner's own library — thousands of checks — showed the failure
# this prevents: `round(100 * 10 280 / 10 281, 1)` is `100.0`, so a strip that
# named a failing album claimed "100% of checks pass" in the same sentence, and
# a Fail badge would read "Fail · 100% of checks passed".
ok(printed_pct(10_280, 10_281) == 99.9,
   "one failed check in ten thousand is not 100 %")
ok(printed_pct(10_281, 10_281) == 100.0, "and a perfect score still is 100 %")
ok(printed_pct(3, 3) == 100.0 and printed_pct(2, 3) == 66.7,
   "ordinary scores are rounded as before")
ok(printed_pct(0, 0) is None and printed_pct(5, 0) is None,
   "a score with no checks has no percentage")

# ----------------------------------------------------------------------
# an unreadable folder is reported on its own album, not as a blanket error
# ----------------------------------------------------------------------
print("== an unreadable folder ==")
un_music = os.path.join(tmp, "Unreadable", "Music")
un_good = os.path.join(un_music, "Artists", "Okay", "Album (2020)")
os.makedirs(un_good)
_un_good_flac = os.path.join(un_good, "01 - Song.flac")
make_flac(_un_good_flac)
set_tags(_un_good_flac, BASE_TAGS)
un_bad = os.path.join(un_music, "Artists", "Denied", "Album (2020)")
os.makedirs(un_bad)
make_flac(os.path.join(un_bad, "01 - Song.flac"))   # a real album the walk finds

UN_CFG = dict(ISO_CFG, music_folder=un_music, grade_check_naming=False,
              grade_check_key_bpm=False, grade_verbose=False,
              grade_check_empty_folders=False)

_real_listdir = os.listdir
_denied = os.path.normcase(os.path.normpath(un_bad))


def _deny_bad(path):
    if os.path.normcase(os.path.normpath(path)) == _denied:
        raise PermissionError(13, "Access is denied")
    return _real_listdir(path)


os.listdir = _deny_bad
try:
    un_stats, un_lines = graded(dict(UN_CFG))
finally:
    os.listdir = _real_listdir

un_bad_rel = os.path.relpath(un_bad, un_music)
un_good_rel = os.path.relpath(un_good, un_music)
_bad_row = row_line(un_lines, un_bad_rel)
ok(un_stats["issue_counts"].get(grader.UNREADABLE_FOLDER) == 1,
   f"a folder whose listing is denied is reported on its OWN album "
   f"({un_stats['issue_counts']})")
ok(_bad_row is not None,
   f"the denied album is still a graded row, not a skipped one ({_bad_row})")
ok(any(f"issues: {grader.UNREADABLE_FOLDER}" in l for l in un_lines),
   "the denied album's row NAMES the issue instead of a blanket error")
ok(not un_stats["errors"],
   f"...and it is not a run-level error that hides the album "
   f"({un_stats['errors']})")
ok(row_line(un_lines, un_good_rel) is not None,
   "the readable sibling album is still graded")

# Directly: the result is a graded row (one failed check), NOT {'error': True}.
os.listdir = _deny_bad
try:
    un_res = _grade_album(un_bad, "EMBEDDED", dict(UN_CFG))
finally:
    os.listdir = _real_listdir
ok(not un_res.get("error") and un_res["total_checks"] == 1
   and un_res["pass_count"] == 0
   and grader.UNREADABLE_FOLDER in un_res["issues"],
   f"_grade_album returns a graded row for an unreadable folder ({un_res})")
ok(any("could not be read" in str(n) for n in un_res["notes"]),
   f"the reason travels in the row's notes ({un_res['notes']})")

# ----------------------------------------------------------------------
# the app's rating store vs the file's RATING tag
# ----------------------------------------------------------------------
print("== RATING store vs tag ==")
rt_music = os.path.join(tmp, "RatingLib", "Music")
rt_album = os.path.join(rt_music, "Artists", "Artist", "Album (2020)")
os.makedirs(rt_album)


def _rt(name, rating=None, tag=None):
    p = os.path.join(rt_album, name)
    make_flac(p)
    set_tags(p, dict(BASE_TAGS, **({"RATING": tag} if tag is not None else {})))
    if rating is not None:
        _ratings_mod.set_rating(p, rating)
    return p


_rt("1-01 Match.flac", rating=4, tag="40")   # store 4 (40) == tag 40
_rt("1-02 Wrong.flac", rating=5, tag="30")   # store 5 (50) != tag 30
_rt("1-03 Bare.flac", tag="70")              # a tag, NO store row
_rt("1-04 Missing.flac", rating=3)           # a store row, no tag

RT_CFG = dict(ISO_CFG, music_folder=rt_music, grade_check_naming=False,
              grade_check_key_bpm=False, grade_verbose=False)
_rt_res = _grade_album(rt_album, "EMBEDDED", dict(RT_CFG))
_rt_issues = {k: v for k, v in _rt_res["issues"].items() if "RATING" in k}


def _blamed(name):
    return any(name.lower() in [str(x).lower() for x in v]
               for v in _rt_issues.values())


ok(not _blamed("1-01 Match.flac"),
   f"a store row and a tag that MATCH is not a failure ({_rt_issues})")
ok(_blamed("1-02 Wrong.flac"),
   f"a store value the tag CONTRADICTS fails grading ({_rt_issues})")
ok(_blamed("1-04 Missing.flac"),
   f"a store row with NO tag fails grading ({_rt_issues})")
ok(not _blamed("1-03 Bare.flac"),
   f"a tag the app never adopted is NOT a divergence ({_rt_issues})")
ok(any("re-rate" in k for k in _rt_issues),
   f"the failure names the fix ({_rt_issues})")

# Clearing the store rows removes the requirement (convergence deletes the tag;
# a grade is not where "unrate" is enforced).
for _p in (os.path.join(rt_album, "1-01 Match.flac"),
           os.path.join(rt_album, "1-02 Wrong.flac"),
           os.path.join(rt_album, "1-04 Missing.flac")):
    _ratings_mod.set_rating(_p, 0)
_rt_res2 = _grade_album(rt_album, "EMBEDDED", dict(RT_CFG))
_rt_issues2 = {k: v for k, v in _rt_res2["issues"].items() if "RATING" in k}
ok(not _rt_issues2,
   f"with no store row nothing RATING-related fails ({_rt_issues2})")

print(f"\nAll {passed} checks passed.")
shutil.rmtree(tmp, ignore_errors=True)
shutil.rmtree(outside, ignore_errors=True)
