#!/usr/bin/env python3
"""Tag-VALUE hygiene: the canonical spelling rule, on the write path and in grading.

What this pins (all of it observable — what lands in the FILE, what the grader
REPORTS, what a script WRITES):

  * `mlo.tagtext` is the one rule: a closed vocabulary is matched
    case-insensitively and rewritten in its canonical spelling ("cd" -> "CD",
    "album; live" -> "Album; Live"), an unknown value is returned UNCHANGED (a
    mood you typed, a SOURCE that is really a video id), and free text is never
    looked at — "AC/DC", "k.d. lang" and "mclusky" survive a write byte for
    byte. The rule is idempotent, which is what lets it run on every write AND
    again over a whole library.
  * `AudioFile.set_tag` applies it, so an import, a wizard write, a script and
    a manual edit land the same. A multi-line value (lyrics) is NEVER collapsed
    or case-changed — the whitespace inside it is the text. A LIST — several
    answers for one field, like a release's countries — is written as repeated
    container fields, canonicalised per value and read back "; "-joined.
  * the grader: `grade_check_tag_case` fails a track whose canonical-cased tag
    is spelled another way, costing exactly ONE grade point per track (issue
    code TAG_CASE), and `grade_check_tag_spaces` now also fails a run of two or
    more INTERNAL spaces. Both are toggles, and a switched-off check neither
    fails nor counts.
  * script 10 (Format all) rewrites an existing library — bad spacing and bad
    case together — reports what it changed, and a second run changes nothing.

Run:  python tools/test_tag_hygiene.py   (exit 0 = pass, 1 = failure)
"""
import os
import shutil
import subprocess
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo.config import DEFAULT_CONFIG
from mlo.format_all import run_format_all
from mlo.grader import _grade_album
from mlo.tagtext import (CANONICAL_VALUES, canonical_text, canonical_value,
                         collapse_spacing, has_internal_space_run,
                         spacing_problem)

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
    """A real (if silent) FLAC, so the tag writes below go through the real
    container writer — the same fixture recipe tools/test_grading_paths.py uses."""
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


def raw_tags(path, tags):
    """Write tags straight to the container, BYPASSING mlo.audio — the only
    way to build the "a library another tagger wrote" fixtures the writers and
    the grader are supposed to fix."""
    from mutagen.flac import FLAC
    f = FLAC(path)
    for k, v in tags.items():
        f[k] = [str(v)]
    f.save()


def tags_of(path):
    """One track's values, read back through the app's own tag layer (the
    container spells the keys its own way — mutagen lowercases them — so the
    assertion has to be about the value a reader sees)."""
    from mlo.audio import AudioFile
    af = AudioFile(path)
    return {k: af.get_tag(k) for k in
            ("TITLE", "MEDIA", "MOOD", "SOURCE", "RELEASETYPE")}


# --------------------------------------------------------------------------- #
print("== mlo.tagtext: the rule itself ==")
# A CLOSED vocabulary is folded and rewritten in its canonical spelling.
ok(canonical_value("MEDIA", "cd") == "CD", "MEDIA 'cd' -> 'CD'")
ok(canonical_value("MEDIA", "digital media") == "Digital Media",
   "MEDIA 'digital media' -> 'Digital Media'")
ok(canonical_value("RELEASETYPE", "album; live") == "Album; Live",
   "a '; '-joined RELEASETYPE is canonicalised per part")
ok(canonical_value("AUDIT", "mix") == "MIX", "AUDIT 'mix' -> 'MIX' (a verdict)")
ok(canonical_value("MOOD", "happy") == "Happy", "MOOD 'happy' -> 'Happy'")
ok(canonical_value("MOOD", "DREAMY") == "Dreamy", "MOOD folds case")
# CODE-shaped values take the code's own casing; anything else is left alone.
ok(canonical_value("RELEASECOUNTRY", "us") == "US", "RELEASECOUNTRY 'us' -> 'US'")
ok(canonical_value("RELEASECOUNTRY", "United States") == "United States",
   "a country spelled out is not a code and is left alone")
ok(canonical_value("SCRIPT", "latn") == "Latn", "SCRIPT 'latn' -> 'Latn'")
# An unknown value inside a closed vocabulary is NEVER mangled.
ok(canonical_value("MOOD", "melancholic") == "melancholic",
   "a mood outside the vocabulary is left exactly as typed")
ok(canonical_value("MEDIA", "squishy disc") == "squishy disc",
   "an unknown MEDIA is left exactly as typed")
ok(canonical_value("SOURCE", "my own rip") == "my own rip",
   "a user's own SOURCE is left alone")
ok(canonical_value("SOURCE", "soulseek") == "Soulseek",
   "the app's own SOURCE values are folded")
ok(canonical_value("RELEASETYPE", "album; bonus") == "Album; bonus",
   "an unknown part of a list keeps its spelling, the known part is fixed")
# Free text has no canonical form at all.
for _tag, _val in (("TITLE", "AC/DC"), ("ARTIST", "k.d. lang"),
                   ("ALBUMARTIST", "mclusky"), ("LABEL", "4AD"),
                   ("ALBUM", "The Lonesome  Crowded West"),
                   ("COMMENT", "my  note")):
    ok(canonical_value(_tag, _val) == _val,
       f"{_tag} is free text: {_val!r} unchanged")
# Idempotent — the property that lets the same rule run twice.
for _tag, _val in (("MEDIA", "cd"), ("MOOD", "happy"), ("RELEASETYPE", "album; live"),
                   ("AUDIT", "mix"), ("RELEASECOUNTRY", "us"), ("SCRIPT", "latn"),
                   ("TITLE", "AC/DC"), ("SOURCE", "SOULSEEK")):
    _once = canonical_value(_tag, _val)
    ok(canonical_value(_tag, _once) == _once,
       f"canonical_value is idempotent for {_tag} {_val!r}")

# --------------------------------------------------------------------------- #
print("== mlo.tagtext: spacing ==")
ok(spacing_problem("TITLE", " x ") == "has leading/trailing spaces",
   "a leading/trailing space is reported")
ok(spacing_problem("TITLE", "\tx") == "has leading/trailing spaces",
   "a leading tab is reported")
ok(spacing_problem("TITLE", "a  b") == "has a run of 2+ internal spaces",
   "a run of 2+ internal spaces is reported")
ok(spacing_problem("TITLE", "a b") == "" and spacing_problem("TITLE", "a\tb") == "",
   "one space, or a tab, is not a problem")
ok(has_internal_space_run("a  b") and not has_internal_space_run("a b"),
   "has_internal_space_run is the value-level half")
# A multi-line value is NEVER judged: its whitespace is the text.
ok(spacing_problem("LYRICS", "[00:00.00]  a\n  b") == "",
   "LYRICS is never judged, even single-line")
ok(spacing_problem("TITLE", "a  b\nc") == "",
   "any value carrying a newline is never judged")
ok(collapse_spacing("  a  b  ") == "a b", "collapse_spacing trims and collapses")
ok(collapse_spacing("line one\n   line  two") == "line one\n   line  two",
   "collapse_spacing never reaches inside a multi-line value")
# The trim happens before the spelling lookup, so padding cannot hide a value
# from its vocabulary (this is what a video tag write does).
ok(canonical_text("SOURCE", "  soulseek  ") == "Soulseek",
   "canonical_text trims BEFORE it looks the value up")
ok(canonical_text("MEDIA", "\tcd ") == "CD",
   "…so a padded known value is still canonicalised, not treated as unknown")

# --------------------------------------------------------------------------- #
print("== AudioFile.set_tag: canonical on write ==")
tmp = tempfile.mkdtemp(prefix="mlo_tagtext_")
track = os.path.join(tmp, "01 - Song.flac")
make_flac(track)

from mlo.audio import AudioFile                                        # noqa: E402

af = AudioFile(track)
af.set_tag("MEDIA", "cd")
af.set_tag("MOOD", "happy")
af.set_tag("AUDIT", "real")
af.set_tag("SOURCE", "  soulseek  ")
af.set_tag("RELEASETYPE", ["album", "live"])
af.set_tag("RELEASECOUNTRY", "us")
af.set_tag("SCRIPT", "latn")
af.set_tag("TITLE", "AC/DC")
af.set_tag("ALBUMARTIST", "k.d. lang")
af.set_tag("LABEL", " 4AD ")
af.set_tag("TITLE", "Song  Name", )  # overwritten below on purpose
af.defer_save(False)
got = AudioFile(track)
ok(got.get_tag("MEDIA") == "CD", f"MEDIA written canonical ({got.get_tag('MEDIA')!r})")
ok(got.get_tag("MOOD") == "Happy", f"MOOD written canonical ({got.get_tag('MOOD')!r})")
ok(got.get_tag("AUDIT") == "REAL", f"AUDIT written canonical ({got.get_tag('AUDIT')!r})")
ok(got.get_tag("SOURCE") == "Soulseek",
   f"SOURCE trimmed and canonical ({got.get_tag('SOURCE')!r})")
ok(got.get_tag("RELEASETYPE") == "Album; Live",
   f"a list is written canonical per value ({got.get_tag('RELEASETYPE')!r})")
ok(got.get_tag("RELEASECOUNTRY") == "US" and got.get_tag("SCRIPT") == "Latn",
   "code-shaped tags take the code's casing")
ok(got.get_tag("TITLE") == "Song Name",
   f"internal spacing is collapsed on write ({got.get_tag('TITLE')!r})")
ok(got.get_tag("LABEL") == "4AD", "a free-text value is trimmed, never re-cased")

# A LIST is the app's spelling for a tag that holds several answers: one
# repeated container field per value, read back "; "-joined. RELEASECOUNTRY
# carries every country a release came out in this way (issue #18), and each
# code takes its own canonical casing.
af = AudioFile(track)
af.set_tag("RELEASECOUNTRY", ["us", "ca", "xe"])
af.defer_save(False)
got = AudioFile(track)
ok(got.get_tag("RELEASECOUNTRY") == "US; CA; XE",
   "a country list is written canonical per code and read back joined "
   f"({got.get_tag('RELEASECOUNTRY')!r})")
from mutagen.flac import FLAC                                       # noqa: E402

ok(FLAC(track)["releasecountry"] == ["US", "CA", "XE"],
   "…as repeated fields on disk, not one 'US; CA; XE' value")

# Free text keeps its case; a multi-line value is not collapsed.
af = AudioFile(track)
af.set_tag("TITLE", "AC/DC")
af.set_tag("ARTIST", "k.d. lang")
af.set_tag("UNSYNCEDLYRICS", "line one\n   line  two  \n\nline three")
af.defer_save(False)
got = AudioFile(track)
ok(got.get_tag("TITLE") == "AC/DC" and got.get_tag("ARTIST") == "k.d. lang",
   "free text survives a write untouched ('AC/DC', 'k.d. lang')")
ok(str(got.get_tag("UNSYNCEDLYRICS")) == "line one\n   line  two  \n\nline three",
   "a multi-line value is never collapsed or trimmed inside")

# A closed-vocabulary UNKNOWN written through set_tag is stored as typed.
af = AudioFile(track)
af.set_tag("MOOD", "melancholic")
af.defer_save(False)
ok(AudioFile(track).get_tag("MOOD") == "melancholic",
   "an unknown mood is written exactly as the caller spelled it")

# --------------------------------------------------------------------------- #
print("== grader: TAG_CASE costs exactly one grade point ==")
# Everything off, then the three checks under test on: a grade of one fixture
# must not be a verdict on the dozen other things a bare album misses.
ISO = {k: False for k in DEFAULT_CONFIG if str(k).startswith("grade_check_")}
ISO.update({
    "music_folder": "",               # no naming check on a synthetic album
    "grade_include_music": True,
    "grade_check_tag_case": True,
    "grade_check_tag_spaces": True,
    "grade_check_tag_blank_lines": True,
    # The CD legs: the fixture is a CD-tagged folder with no rip artefacts (no
    # .log, no .accurip), so the readout would charge "nothing established the
    # CD verdict's '<leg>' leg" — a real failure for a real disc folder, and
    # not what this file grades. The CD evidence rules have their own suite
    # (test_cd_audit.py).
    "audit_require_accuraterip": False,
    "audit_verify_log_checksum": False,
    "audit_log_score_threshold": 0,
})

case_track = os.path.join(tmp, "Album", "01 - Case.flac")
os.makedirs(os.path.dirname(case_track), exist_ok=True)
make_flac(case_track)
album = os.path.dirname(case_track)

raw_tags(case_track, {"TITLE": "Song", "MEDIA": "CD"})
good = _grade_album(album, "EMBEDDED", ISO)
ok(not good["tracks"][0]["issues"] and good["total_checks"] == good["pass_count"],
   f"a canonical album passes the case check ({good['pass_count']}/{good['total_checks']})")

raw_tags(case_track, {"TITLE": "Song", "MEDIA": "cd"})
bad = _grade_album(album, "EMBEDDED", ISO)
ok("TAG_CASE" in bad["tracks"][0]["issues"],
   f"a lowercase MEDIA fails the track (got {bad['tracks'][0]['issues']})")
ok(any(i.startswith("Tag case:") and "'cd'" in i for i in bad["issues"]),
   f"the album issue names the tag and the values (got {bad['issues']})")
ok(bad["total_checks"] == good["total_checks"]
   and (bad["total_checks"] - bad["pass_count"])
   == (good["total_checks"] - good["pass_count"]) + 1,
   f"TAG_CASE costs exactly one grade point "
   f"({bad['pass_count']}/{bad['total_checks']} vs {good['pass_count']}/{good['total_checks']})")

# The toggle: off means neither graded nor counted.
off = _grade_album(album, "EMBEDDED", dict(ISO, grade_check_tag_case=False))
ok("TAG_CASE" not in off["tracks"][0]["issues"]
   and off["total_checks"] == good["total_checks"] - 1
   and off["pass_count"] == off["total_checks"],
   f"grade_check_tag_case=False stops grading it and stops counting it "
   f"({off['pass_count']}/{off['total_checks']})")

# Free text and an unknown vocabulary value are never the case check's business.
raw_tags(case_track, {"TITLE": "AC/DC", "MEDIA": "CD", "MOOD": "melancholic",
                      "LABEL": "k.d. lang"})
free = _grade_album(album, "EMBEDDED", ISO)
ok("TAG_CASE" not in free["tracks"][0]["issues"],
   f"free text and an unknown mood are never re-cased "
   f"(got {free['tracks'][0]['issues']})")

# --------------------------------------------------------------------------- #
print("== grader: grade_check_tag_spaces now covers internal runs ==")
raw_tags(case_track, {"TITLE": "Song  Name", "MEDIA": "CD"})
sp = _grade_album(album, "EMBEDDED", ISO)
ok("TITLE" in sp["tracks"][0]["issues"],
   f"a doubled internal space fails the spacing check (got {sp['tracks'][0]['issues']})")
ok(any("run of 2+ internal spaces" in i for i in sp["issues"]),
   f"the issue says which whitespace is wrong (got {sp['issues']})")
ok((sp["total_checks"] - sp["pass_count"])
   == (good["total_checks"] - good["pass_count"]) + 1,
   "the internal run costs exactly one grade point")
raw_tags(case_track, {"TITLE": "  Song Name  ", "MEDIA": "CD"})
lead = _grade_album(album, "EMBEDDED", ISO)
ok(any("has leading/trailing spaces" in i for i in lead["issues"]),
   f"the leading/trailing message is the one it always was (got {lead['issues']})")
spoff = _grade_album(album, "EMBEDDED", dict(ISO, grade_check_tag_spaces=False))
ok(not any("internal spaces" in i for i in spoff["issues"])
   and not any("leading/trailing" in i for i in spoff["issues"]),
   "grade_check_tag_spaces=False stops both halves")

# --------------------------------------------------------------------------- #
print("== script 10: Format all fixes an existing library ==")
lib = tempfile.mkdtemp(prefix="mlo_format_all_")
fixture = os.path.join(lib, "Album")
os.makedirs(fixture, exist_ok=True)
wrong = os.path.join(fixture, "01 - Wrong.flac")
make_flac(wrong)
raw_tags(wrong, {
    "TITLE": "Song  Name",
    "MEDIA": "digital media",
    "MOOD": "happy",
    "SOURCE": "  soulseek  ",
    "RELEASETYPE": "album; live",
})
cfg = {"music_folder": lib}
stats = run_format_all(cfg)
after = tags_of(wrong)
ok(after.get("MEDIA") == "Digital Media",
   f"script 10 wrote the canonical MEDIA ({after.get('MEDIA')!r})")
ok(after.get("MOOD") == "Happy", f"script 10 wrote the canonical MOOD ({after.get('MOOD')!r})")
ok(after.get("SOURCE") == "Soulseek", f"script 10 collapsed the SOURCE ({after.get('SOURCE')!r})")
ok(after.get("TITLE") == "Song Name", f"script 10 collapsed the TITLE ({after.get('TITLE')!r})")
ok(after.get("RELEASETYPE") == "Album; Live",
   f"script 10 canonicalised the RELEASETYPE ({after.get('RELEASETYPE')!r})")
ok(stats["modified_count"] >= 1 and stats["tags_canonicalized"] >= 4,
   f"the run reports what it changed "
   f"(modified={stats['modified_count']}, canonicalized={stats['tags_canonicalized']})")

# Idempotent: the same run over the canonical library changes nothing.
stats2 = run_format_all(cfg)
ok(stats2["tags_canonicalized"] == 0 and stats2["modified_count"] == 0,
   f"a second run is a no-op "
   f"(modified={stats2['modified_count']}, canonicalized={stats2['tags_canonicalized']})")

# …and the grader is satisfied by what the script wrote.
canon = _grade_album(fixture, "EMBEDDED", ISO)
ok("TAG_CASE" not in canon["tracks"][0]["issues"],
   f"the fixed file passes the case check (got {canon['tracks'][0]['issues']})")

# --------------------------------------------------------------------------- #
print("== the registry exposes the canonical vocabulary ==")
from server.tags_registry import registry                               # noqa: E402

reg = registry()
by_key = {t["key"]: t for t in reg["tags"]}
ok(by_key["MOOD"]["enum"] == list(CANONICAL_VALUES["MOOD"]),
   f"MOOD's enum is the spelling a file holds ({by_key['MOOD']['enum']})")
ok("CD" in by_key["MEDIA"]["enum"] and "MIX" in by_key["AUDIT"]["enum"],
   "MEDIA's and AUDIT's enums are the app's canonical values")
ok("grade_check_tag_case" in {c["key"] for c in reg["checks"]},
   "the new check is in the registry's own check list")
ok(by_key["RELEASETYPE"]["graded_by"] == ["grade_check_tag_case"],
   f"RELEASETYPE is graded by the case check "
   f"(got {by_key['RELEASETYPE']['graded_by']})")

shutil.rmtree(tmp, ignore_errors=True)
shutil.rmtree(lib, ignore_errors=True)
print(f"\nPASS — {passed} assertion(s)")
