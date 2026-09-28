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
  * script 23 (Optimize tags, `mlo/taghygiene.py`) is that strip on its own and
    scoped: it deletes exactly the grader's excess list (junk names, a valued
    COMMENT, unneeded aliases — `mlo.format_all.excess_tags`), leaves a NEEDED
    alias alone, and does not write a clean file at all (mtime + bytes).

Run:  python tools/test_tag_hygiene.py   (exit 0 = pass, 1 = failure)
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo.config import DEFAULT_CONFIG
from mlo.format_all import excess_tags, run_format_all
from mlo.grader import _grade_album
from mlo.taghygiene import run_tag_hygiene
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


def drop_tags(path, keys):
    """Remove tags straight from the container — `raw_tags`' mirror. A fixture
    is ONE file reused by many cases, and `raw_tags` only ever ADDS, so a case
    that leaves an alias behind would otherwise grade the next one."""
    from mutagen.flac import FLAC
    f = FLAC(path)
    for k in keys:
        if k in f:
            del f[k]
    f.save()


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
# The DERIVED release type (mlo.naming.DERIVED_RELEASE_TYPES) is inside the
# RELEASETYPE vocabulary, so the value the app derives and compares has one
# canonical spelling — MusicBrainz itself states no such release-group type
# (an episode is Broadcast `part of` a Podcast series, see mlo.autotag), so
# this is the one value in the list that is the APP's, not MusicBrainz's.
ok(canonical_value("RELEASETYPE", "podcast") == "Podcast",
   "RELEASETYPE 'podcast' -> 'Podcast' (the app's derived type)")
ok(canonical_value("RELEASETYPE", "Podcast; Broadcast") == "Podcast; Broadcast",
   "and a combined value keeps it beside the MusicBrainz type")
ok(canonical_value("RELEASETYPE", "podcast special") == "podcast special",
   "while a value that only starts with it is not a type and is left alone")
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
print("== COMMENT: a name the vocabulary holds whose VALUE is junk ==")
# COMMENT is free text canonicalisation must leave alone (above) — which is
# exactly why a VALUE there is always somebody else's note. Grading fails it
# with its own issue code and script 10 clears it, both under the excess-tag
# switch the rest of the strip follows.
EXCESS = dict(ISO, grade_check_excess_tags=True, strip_unknown_tags=True)
assert EXCESS["grade_check_excess_tags"], "the case below grades the excess check"
raw_tags(wrong, {"TITLE": "Song Name", "MEDIA": "Digital Media",
                 "COMMENT": "ripped by some tool"})
commented = _grade_album(fixture, "EMBEDDED", EXCESS)
ok("COMMENT" in commented["tracks"][0]["issues"],
   f"a non-empty COMMENT fails the track (got {commented['tracks'][0]['issues']})")
ok(any("Comment tag carries a value" in i and "COMMENT" in i
       for i in commented["issues"]),
   f"the album issue names the tag and the script that clears it "
   f"(got {commented['issues']})")

stats3 = run_format_all(cfg)
comment_after = AudioFile(wrong).get_tag("COMMENT")
ok(not comment_after,
   f"script 10 cleared the COMMENT value ({comment_after!r})")
ok(comment_after is None and stats3["modified_count"] >= 1,
   f"and reported the file as modified (modified={stats3['modified_count']})")
clean = _grade_album(fixture, "EMBEDDED", EXCESS)
ok("COMMENT" not in clean["tracks"][0]["issues"],
   f"the cleared file passes the same check (got {clean['tracks'][0]['issues']})")
# The check is gated like the rest of the strip: with strip_unknown_tags off,
# nothing in the pipeline can clear a COMMENT, so grading does not demand it.
no_strip = _grade_album(fixture, "EMBEDDED",
                        dict(EXCESS, strip_unknown_tags=False))
ok("COMMENT" not in no_strip["tracks"][0]["issues"],
   "strip_unknown_tags=False stops both the strip and the grade")

# --------------------------------------------------------------------------- #
print("== aliases: written only where the locale needs them (R16a / R16b) ==")
# The alias family is in the vocabulary — a legitimate alias is never a
# FOREIGN tag — but it is graded BOTH ways, and by the configured locale's own
# script: `grade_check_alias_needed` fails a name that needs an alias and has
# none, `grade_check_alias_excess` fails an alias nothing needs (or a spelling
# the app does not write). The mirror of "Radiohead needs no ARTISTALIAS".
ALIASES = dict(ISO, grade_check_alias_needed=True,
               grade_check_alias_excess=True, strip_unknown_tags=True)

ALIAS_KEYS = ("TITLEALIAS", "ARTISTALIAS", "ALBUMALIAS", "TITLEALIAS-JA",
              "ARTISTALIAS-JA", "ALBUMALIAS-EN", "TITLEALIAS-EN")

drop_tags(wrong, ALIAS_KEYS + ("ARTIST",))
raw_tags(wrong, {"TITLE": "君の名は", "MEDIA": "Digital Media"})
ja_title = _grade_album(fixture, "EMBEDDED", ALIASES)
ok("TITLEALIAS" in ja_title["tracks"][0]["issues"]
   and any("Missing TITLEALIAS" in i for i in ja_title["issues"]),
   f"a name the locale cannot read without its alias fails "
   f"(got {ja_title['tracks'][0]['issues']})")
drop_tags(wrong, ALIAS_KEYS)
raw_tags(wrong, {"TITLE": "君の名は", "MEDIA": "Digital Media",
                 "TITLEALIAS": "Your Name"})
with_needed = _grade_album(fixture, "EMBEDDED", ALIASES)
ok(not any("ALIAS" in i for i in with_needed["tracks"][0]["issues"])
   and not any("ALIAS" in i for i in with_needed["issues"]),
   f"…and the alias it needs passes BOTH halves "
   f"(got {with_needed['tracks'][0]['issues']})")

# The mirror: a name the locale reads needs NOTHING, and an alias there fails.
drop_tags(wrong, ALIAS_KEYS)
raw_tags(wrong, {"TITLE": "Radiohead", "MEDIA": "Digital Media",
                 "TITLEALIAS": "レディオヘッド"})
radiohead = _grade_album(fixture, "EMBEDDED", ALIASES)
ok("TITLEALIAS" in radiohead["tracks"][0]["issues"],
   f"an alias on a name the locale reads fails "
   f"(got {radiohead['tracks'][0]['issues']})")
ok(any("Unneeded TITLEALIAS" in i for i in radiohead["issues"]),
   f"the issue names the tag and why (got {radiohead['issues']})")
ok(not any(i.startswith("Excess tags:") for i in radiohead["issues"]),
   "…as its own check, not as a foreign tag — the family stays in the vocabulary")

# A spelling for a locale the app does not write (the configured one is `en`
# here) is excess even when the name DOES need an alias, and so is a value
# that is the name itself.
drop_tags(wrong, ALIAS_KEYS + ("ARTIST",))
raw_tags(wrong, {"TITLE": "Song Name", "MEDIA": "Digital Media",
                 "ARTIST": "宇多田ヒカル", "ARTISTALIAS-JA": "Utada Hikaru"})
suffixed = _grade_album(fixture, "EMBEDDED", ALIASES)
ok("ARTISTALIAS" in suffixed["tracks"][0]["issues"]
   and any("spelled for locale JA" in i for i in suffixed["issues"])
   and any("Missing ARTISTALIAS" in i for i in suffixed["issues"]),
   f"a suffix for another locale fails BOTH halves — it is not the alias the "
   f"reader needs, and it is one the app does not write ({suffixed['issues']})")
drop_tags(wrong, ALIAS_KEYS + ("ARTIST",))
raw_tags(wrong, {"TITLE": "君の名は", "MEDIA": "Digital Media",
                 "TITLEALIAS": "君の名は"})
same = _grade_album(fixture, "EMBEDDED", ALIASES)
ok("TITLEALIAS" in same["tracks"][0]["issues"]
   and any("the name itself" in i for i in same["issues"]),
   f"an alias equal to the name fails (X (X) says nothing) ({same['issues']})")

# Script 10 clears exactly what the grade flags — "the files hold only what is
# required afterwards" — and a NEEDED alias survives it untouched.
drop_tags(wrong, ALIAS_KEYS)
raw_tags(wrong, {"TITLE": "Song Name", "ALBUM": "Song Name",
                 "MEDIA": "Digital Media", "ALBUMALIAS": "Song Name (EN)"})
stats4 = run_format_all(cfg)
ok(AudioFile(wrong).get_tag("ALBUMALIAS") is None and stats4["modified_count"] >= 1,
   "script 10 cleared an unneeded ALBUMALIAS "
   f"({AudioFile(wrong).get_tag('ALBUMALIAS')!r}, modified={stats4['modified_count']})")
cleared = _grade_album(fixture, "EMBEDDED", ALIASES)
ok(not any("ALIAS" in i for i in cleared["tracks"][0]["issues"]),
   f"and the grade passes afterwards (got {cleared['tracks'][0]['issues']})")
drop_tags(wrong, ALIAS_KEYS)
raw_tags(wrong, {"TITLE": "君の名は", "MEDIA": "Digital Media",
                 "TITLEALIAS": "Your Name"})
run_format_all(cfg)
ok(AudioFile(wrong).get_tag("TITLEALIAS") == "Your Name",
   "…while a NEEDED alias is never stripped")

# Both halves are toggles like every other check.
drop_tags(wrong, ALIAS_KEYS)
raw_tags(wrong, {"TITLE": "Radiohead", "MEDIA": "Digital Media",
                 "TITLEALIAS": "レディオヘッド"})
off_excess = _grade_album(fixture, "EMBEDDED",
                          dict(ALIASES, grade_check_alias_excess=False))
ok("TITLEALIAS" not in off_excess["tracks"][0]["issues"],
   "grade_check_alias_excess=False stops the excess half")
drop_tags(wrong, ALIAS_KEYS)
raw_tags(wrong, {"TITLE": "君の名は", "MEDIA": "Digital Media"})
off_needed = _grade_album(fixture, "EMBEDDED",
                          dict(ALIASES, grade_check_alias_needed=False))
ok("TITLEALIAS" not in off_needed["tracks"][0]["issues"],
   "grade_check_alias_needed=False stops the missing half")
drop_tags(wrong, ALIAS_KEYS)
raw_tags(wrong, {"TITLE": "Song Name", "MEDIA": "Digital Media"})

# --------------------------------------------------------------------------- #
print("== script 23: Optimize tags — that same strip, on its own and scoped ==")
# The details menus offer ONE entry per registry id, and the passes the
# excess-tag grades pointed at are whole passes: 3 re-encodes a file, 10
# formats the library. 23 (mlo/taghygiene.py) is the SCOPED entry point to the
# SAME list and the SAME deletion — `mlo.format_all.excess_tags` /
# `strip_excess_tags`, which script 10's own tag pass calls too — so one
# album's junk tags can be cleared from that album's menu. It writes nothing
# but the deletion: a file with nothing excess is not written at all, and a
# NEEDED alias is never touched.
hyg = tempfile.mkdtemp(prefix="mlo_tag_hygiene_")
album = os.path.join(hyg, "Album")
os.makedirs(album)
HYG = dict(DEFAULT_CONFIG, music_folder=hyg, locale="en",
           strip_unknown_tags=True)

dirty = os.path.join(album, "01 - Dirty.flac")
make_flac(dirty)
raw_tags(dirty, {"TITLE": "Lost Umbrella", "ARTIST": "Radiohead",
                 "ALBUM": "The Album", "GENRE": "Shoegaze",
                 "ARTISTALIAS": "Radiohead",          # the name itself
                 "TITLEALIAS-JA": "ロストアンブレラ",  # another locale
                 "COMMENT": "ripped by some tool",    # a value nothing writes
                 "MYJUNKTAG": "vendor junk"})         # outside the vocabulary
needed = os.path.join(album, "02 - Needed.flac")
make_flac(needed)
# The alias a reader of `en` NEEDS: the name is in a script that locale cannot
# read, and the alias is the app's own bare spelling of it.
raw_tags(needed, {"TITLE": "君の名は", "ARTIST": "RADWIMPS",
                  "ALBUM": "君の名は", "TITLEALIAS": "Your Name"})
untouched = os.path.join(album, "03 - Clean.flac")
make_flac(untouched)
raw_tags(untouched, {"TITLE": "Song", "ARTIST": "Radiohead",
                     "ALBUM": "The Album", "GENRE": "Rock"})


def _state(path):
    """The two things a write cannot leave alone: mtime and the bytes."""
    with open(path, "rb") as fh:
        return (os.stat(path).st_mtime_ns, hashlib.sha256(fh.read()).hexdigest())


# What the grader calls excess, from the script's OWN list (not a
# re-derivation here): the four tags above and nothing else.
expect = sorted(str(k).upper() for k in excess_tags(AudioFile(dirty), HYG))
ok(expect == ["ARTISTALIAS", "COMMENT", "MYJUNKTAG", "TITLEALIAS-JA"],
   f"the shared list names exactly the four excess tags ({expect})")
before = {p: _state(p) for p in (dirty, needed, untouched)}

st23 = run_tag_hygiene(dict(HYG, targets=[album]))
held = {str(k).upper() for k in (AudioFile(dirty).all_tags() or {})}
ok(not held & set(expect),
   f"script 23 deleted them all (still holding {sorted(held)})")
ok(all(AudioFile(dirty).get_tag(t) for t in ("TITLE", "ARTIST", "ALBUM", "GENRE")),
   "…and left every tag that is not excess alone, GENRE included")
ok(AudioFile(needed).get_tag("TITLEALIAS") == "Your Name"
   and _state(needed) == before[needed],
   "a NEEDED alias is never stripped — and the file is not written at all")
ok(_state(untouched) == before[untouched],
   "a clean file is not written at all (mtime and bytes unmoved)")
ok(st23["modified_count"] == 1 and st23["skipped_count"] == 2
   and st23["tags_removed"] == len(expect) and st23["error_count"] == 0,
   f"the run reports what it did ({st23['modified_count']} cleaned, "
   f"{st23['tags_removed']} tags removed, {st23['skipped_count']} skipped)")
ok(st23["total_scanned"] == st23["modified_count"] + st23["skipped_count"]
   + st23["error_count"],
   "every scanned file leaves exactly one verdict (R10a)")

# The whole library, with no targets at all (what Run All posts): the pass is
# idempotent and touches no file — the promise the clean file above made, now
# over every file of the run.
state2 = {p: _state(p) for p in (dirty, needed, untouched)}
st23b = run_tag_hygiene(dict(HYG))
ok(st23b["modified_count"] == 0 and st23b["tags_removed"] == 0
   and st23b["skipped_count"] == 3 and st23b["error_count"] == 0,
   f"a library-wide re-run is a no-op ({st23b['modified_count']} cleaned)")
ok({p: _state(p) for p in (dirty, needed, untouched)} == state2,
   "…and not one file's mtime or bytes moved")

# The switch the whole rule is gated on (`strip_unknown_tags`, the one the
# excess-tag grade and script 10's strip read): off, nothing is excess and the
# pass deletes nothing — and a chain skips it, which tools/test_script_menu.py
# pins from the menu's side.
raw_tags(dirty, {"MYJUNKTAG": "vendor junk"})
st23_off = run_tag_hygiene(dict(HYG, targets=[album],
                                strip_unknown_tags=False))
held_off = {str(k).upper() for k in (AudioFile(dirty).all_tags() or {})}
ok("MYJUNKTAG" in held_off and st23_off["modified_count"] == 0
   and st23_off["tags_removed"] == 0,
   f"strip_unknown_tags=False deletes nothing ({sorted(held_off)})")

# What the grade reported BEFORE, it reports nothing of AFTER: the album the
# script cleaned is clean of both excess families on the next grade.
raw_tags(dirty, {"TITLE": "Lost Umbrella", "ARTIST": "Radiohead",
                 "ARTISTALIAS": "Radiohead"})
HYG_GRADE = dict(HYG, grade_check_excess_tags=True,
                 grade_check_alias_excess=True, grade_include_music=True)
run_tag_hygiene(dict(HYG, targets=[album]))
graded = _grade_album(album, "EMBEDDED", HYG_GRADE)
ok(not [i for i in graded["issues"]
        if i.startswith(("Unneeded", "Excess tags"))],
   f"the cleaned album carries no excess/alias failure any more "
   f"({graded['issues']})")

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
# A tag nothing writes reports no writer, and the alias family is a row of its
# own so the Grading page can show what the alias check grades.
ok(by_key["COMMENT"]["writer"].startswith("nothing"),
   f"COMMENT reports no writer instead of the import's own name "
   f"(got {by_key['COMMENT']['writer']!r})")
ok(by_key["COMMENT"]["graded_by"] == ["grade_check_excess_tags"]
   and "COMMENT" in by_key["COMMENT"]["issue_codes"],
   f"COMMENT is graded by the excess check with its own code "
   f"(got {by_key['COMMENT']['graded_by']}/{by_key['COMMENT']['issue_codes']})")
ok(by_key["TITLEALIAS"]["graded_by"] == ["grade_check_alias_excess",
                                         "grade_check_alias_needed"]
   and by_key["ALBUMALIAS"]["graded_by"] == ["grade_check_alias_excess",
                                             "grade_check_alias_needed"]
   and "grade_check_alias_needed" in by_key["ALBUM"]["graded_by"]
   and "TITLEALIAS-" in reg["allowed_prefixes"],
   f"the alias family is a registry row behind BOTH alias checks "
   f"(got {by_key['TITLEALIAS']['graded_by']} / "
   f"{by_key['ALBUMALIAS']['graded_by']} / {by_key['ALBUM']['graded_by']} / "
   f"{reg['allowed_prefixes']})")
ok("grade_check_alias_excess" in {c["key"] for c in reg["checks"]}
   and not reg.get("checks_unlabelled")
   and all(c["label"] for c in reg["checks"]),
   "…and the new check is a labelled row of the registry's check list")

shutil.rmtree(tmp, ignore_errors=True)
shutil.rmtree(lib, ignore_errors=True)
shutil.rmtree(hyg, ignore_errors=True)
print(f"\nPASS — {passed} assertion(s)")
