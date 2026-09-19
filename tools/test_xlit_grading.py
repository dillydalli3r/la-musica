#!/usr/bin/env python3
"""Grading of script 17's lyric transforms (mlo.grader) — the XLIT_* codes.

What this pins, on a real (tiny) FLAC per case and no network / no AI:

  * a LATIN-script track carrying a TRANSLITERATION tag fails XLIT_UNNEEDED —
    romanizing English tells the reader nothing;
  * non-Latin lyrics WITHOUT a stored transform fail XLIT_MISSING, and the
    same lyrics WITH one pass;
  * a TRANSLATION tag on lyrics that are already the reader's own language
    (the first entry of `lyrics_translation_langs`) fails XLIT_UNNEEDED,
    while foreign lyrics without one fail XLIT_MISSING;
  * the sidecars count exactly like the tags (`.en.lrc`, `.romaji.lrc`), so a
    transform stored next to the audio is as real as an embedded one;
  * INSTRUMENTAL=1 never fails either check — an instrumental has no lyrics
    to transform by definition;
  * the two toggles are independent, and every case asks the grader the same
    way the existing grading suites do.

Run:  python tools/test_xlit_grading.py
Exit: 0 all checks pass, 1 a check failed, 2 the harness could not run.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import lyrics_xlit as xl
from mlo.config import DEFAULT_CONFIG
from mlo.grader import _grade_album
from server import ai

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
if not FLAC_EXE:
    print("SKIP: flac.exe not found under .dependencies")
    sys.exit(2)

passed = 0


def ok(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok: {label}")


def make_flac(path):
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


# Everything this file does not test is switched off (or satisfied), so an
# issue code below can only have come from the checks under test. The base is
# DEFAULT_CONFIG so the keys this file does not name keep their shipped value.
CFG = dict(DEFAULT_CONFIG,
           music_folder="",
           grade_check_expected_tracks=False,
           grade_check_missing_tags=False,
           grade_check_mood=False,
           grade_check_energy=False,
           grade_check_key_bpm=False,
           grade_check_encoder=False,
           grade_check_album_tags=False,
           grade_check_mb_links=False,
           grade_check_rym_links=False,
           grade_check_cover=False,
           grade_check_sidecar_cover=False,
           grade_check_extra_images=False,
           grade_check_excess_tags=False,
           grade_check_replaygain=False,
           grade_check_acoustid=False,
           grade_check_media=False,
           grade_check_source=False,
           grade_check_unreadable=False,
           grade_check_disallowed=False,
           grade_check_empty_folders=False,
           grade_check_genre_count=False,
           grade_check_genre_order=False,
           grade_check_lyrics_format=False,
           grade_check_lyrics_lang_tags=False,
           grade_check_instrumental=False,
           write_dynamic_range_tags=False,
           lyrics_translation_langs="en")

BASE_TAGS = {
    "TITLE": "Song",
    "ARTIST": "Artist",
    "ALBUMARTIST": "Artist",
    "ALBUM": "Album",
    "DATE": "2020",
    "TRACKNUMBER": "1",
    "DISCNUMBER": "1",
    "GENRE": "Rock",
    "INSTRUMENTAL": "0",
}

# Two lyric lines in each language: one line is a placeholder, not a lyric
# (see mlo.lyrics_xlit.xlit_needs), so every fixture below is a real one.
EN_LYRICS = "[00:01.00] Hello there, my old friend\n[00:03.50] I sing for you"
DE_LYRICS = ("[00:01.00] Der Himmel ist blau und ich bin hier\n"
             "[00:03.50] Ich singe nicht mehr")
JA_LYRICS = "[00:01.00] 君の名は\n[00:03.50] 忘れられない"

TMP = tempfile.mkdtemp(prefix="mlo_xlit_grade_")
_seq = [0]
LAST = {}


def make_album(tags, sidecars=()):
    """One fresh album folder carrying *tags*; returns its audio path.

    *sidecars* are (file name, text) pairs written next to the audio, so the
    storage-place cases (a transform in a sidecar alone) are exercised the way
    the script would leave them. The folder and path are remembered in LAST so
    a case can run the script over the very file it just graded.
    """
    _seq[0] += 1
    folder = os.path.join(TMP, f"album{_seq[0]}")
    os.makedirs(folder)
    path = os.path.join(folder, "01 Song.flac")
    make_flac(path)
    set_tags(path, dict(BASE_TAGS, **tags))
    for name, text in sidecars:
        with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    LAST["folder"], LAST["path"] = folder, path
    return path


def regrade(extra=None):
    """Re-grade the folder make_album() built last."""
    res = _grade_album(LAST["folder"], "EMBEDDED", dict(CFG, **(extra or {})))
    return res["tracks"][0]["issues"]


def grade(tags, extra=None, sidecars=()):
    """Grade one fresh album carrying *tags* and return the track's issues."""
    make_album(tags, sidecars)
    return regrade(extra)


try:
    print("== transliteration ==")
    issues = grade({"LYRICS": EN_LYRICS, "TRANSLITERATION-LATN": "Hello there"})
    ok("XLIT_UNNEEDED" in issues,
       f"a TRANSLITERATION on Latin-script lyrics fails (got {issues})")
    ok("XLIT_MISSING" not in issues, "and it is not also reported as missing")

    issues = grade({"LYRICS": JA_LYRICS})
    ok("XLIT_MISSING" in issues,
       f"non-Latin lyrics without a transliteration fail (got {issues})")

    issues = grade({"LYRICS": JA_LYRICS, "TRANSLITERATION-JA-LATN": "Kimi no na wa",
                    "TRANSLATION-EN": "What is your name"})
    ok("XLIT_MISSING" not in issues and "XLIT_UNNEEDED" not in issues,
       f"the same lyrics WITH both transforms pass ({issues})")

    # The sidecar counts exactly like the tag — script 17 writes .romaji.lrc
    # for the LRC/BOTH layouts and nothing else.
    issues = grade({"LYRICS": JA_LYRICS},
                   extra={"grade_check_xlit_translation": False},
                   sidecars=[("01 Song.romaji.lrc", "[00:01.00] Kimi no na wa")])
    ok("XLIT_MISSING" not in issues,
       f"a .romaji.lrc sidecar satisfies the check ({issues})")
    issues = grade({"LYRICS": EN_LYRICS},
                   sidecars=[("01 Song.romaji.lrc", "[00:01.00] Hello there")])
    ok("XLIT_UNNEEDED" in issues,
       f"an unneeded romanization in a sidecar fails too ({issues})")

    print("== translation ==")
    issues = grade({"LYRICS": EN_LYRICS, "TRANSLATION-EN": "Hello there"})
    ok("XLIT_UNNEEDED" in issues,
       f"a TRANSLATION of lyrics already in the reader's language fails "
       f"(got {issues})")

    issues = grade({"LYRICS": DE_LYRICS})
    ok("XLIT_MISSING" in issues,
       f"German lyrics under lyrics_translation_langs=en fail (got {issues})")

    issues = grade({"LYRICS": DE_LYRICS, "TRANSLATION-EN": "The sky is blue"})
    ok("XLIT_MISSING" not in issues and "XLIT_UNNEEDED" not in issues,
       f"the English translation of them passes ({issues})")

    # A translation stored under ANOTHER language is a mismatch, not a pass.
    issues = grade({"LYRICS": DE_LYRICS, "TRANSLATION-DE": "Der Himmel ist blau"})
    ok("XLIT_MISSING" in issues,
       f"a translation for the wrong language is still missing the right one "
       f"(got {issues})")
    issues = grade({"LYRICS": DE_LYRICS},
                   sidecars=[("01 Song.de.lrc", "[00:01.00] Der Himmel ist blau")])
    ok("XLIT_MISSING" in issues,
       f"the same mismatch in a sidecar is caught ({issues})")
    issues = grade({"LYRICS": DE_LYRICS},
                   sidecars=[("01 Song.en.lrc", "[00:01.00] The sky is blue")])
    ok("XLIT_MISSING" not in issues,
       f"the right sidecar language passes ({issues})")
    issues = grade({"LYRICS": EN_LYRICS},
                   sidecars=[("01 Song.en.lrc", "[00:01.00] Hello there")])
    ok("XLIT_UNNEEDED" in issues,
       f"a translation sidecar for the reader's own language fails ({issues})")

    print("== instrumental & toggles ==")
    issues = grade({"INSTRUMENTAL": "1", "LYRICS": JA_LYRICS})
    ok("XLIT_MISSING" not in issues and "XLIT_UNNEEDED" not in issues,
       f"INSTRUMENTAL=1 never fails either check (got {issues})")

    issues = grade({"LYRICS": EN_LYRICS, "TRANSLITERATION-LATN": "Hello there"},
                   extra={"grade_check_xlit_transliteration": False})
    ok("XLIT_UNNEEDED" not in issues and "XLIT_MISSING" not in issues,
       f"grade_check_xlit_transliteration=False stops grading it (got {issues})")
    issues = grade({"LYRICS": DE_LYRICS},
                   extra={"grade_check_xlit_translation": False})
    ok("XLIT_MISSING" not in issues,
       f"grade_check_xlit_translation=False stops grading it (got {issues})")
    issues = grade({"LYRICS": DE_LYRICS, "TRANSLATION-EN": "The sky is blue"},
                   extra={"grade_check_xlit_transliteration": True})
    ok("XLIT_MISSING" not in issues,
       "the translation check is independent of the transliteration one")

    print("== a stale transform is clearable ==")
    # The whole point of the rules above: a token an older, laxer rule wrote
    # must be REMOVABLE by running script 17, or the album could never grade
    # PASS again. This runs the real script over the graded file and re-grades.
    path = make_album({"LYRICS": EN_LYRICS,
                       "TRANSLITERATION-LATN": "Hello there",
                       "TRANSLATION-EN": "Hello there"},
                      sidecars=[("01 Song.romaji.lrc", "[00:01.00] Hello there"),
                                ("01 Song.en.lrc", "[00:01.00] Hello there")])
    stale = regrade()
    ok(stale.count("XLIT_UNNEEDED") == 2,
       f"both stale transforms are reported as unneeded (got {stale})")

    real_chat = ai.ai_chat

    def no_model(*_a, **_k):
        raise AssertionError("the model must not be asked to clear a transform")

    ai.ai_chat = no_model
    try:
        stats = xl.run_lyrics_xlit({
            "music_folder": LAST["folder"], "targets": [path],
            "ai_base_url": "http://x/v1", "ai_model": "m",
            "lyrics_format": "EMBEDDED", "lyrics_translation_langs": "en",
            "lyrics_xlit_enabled": True, "lyrics_translate_enabled": True,
        })
    finally:
        ai.ai_chat = real_chat
    ok(stats.get("stale_removed") == 2 and stats.get("error_count") == 0,
       f"the run removes both without touching the model ({stats})")
    clean = regrade()
    ok(not [i for i in clean if i.startswith("XLIT_")],
       f"and the track then grades clean ({clean})")
    ok(not os.path.exists(os.path.join(LAST["folder"], "01 Song.romaji.lrc"))
       and not os.path.exists(os.path.join(LAST["folder"], "01 Song.en.lrc")),
       "the stale sidecars are gone as well")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
print(f"all {passed} checks passed")
sys.exit(0)
