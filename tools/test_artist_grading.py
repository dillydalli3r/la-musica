#!/usr/bin/env python3
"""Verify artist-level grading (mlo.grader.grade_artist) and the layout
scanner's matching empty_artist finding.

An artist folder is graded on the ONE thing still left to it: that it holds an
album folder at all. The artist image / description checks are gone with the
features that fetched them, so the grade never runs an album-level check here
and reports exactly two folder-level verdicts — ARTIST_FOLDER_MISSING (no
folder) and ARTIST_EMPTY (no album folder under it) — each with no checks
invented, because neither is a folder this app can grade whatever the settings
say.
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
# hermeticity: app paths resolve through the music folder the moment they are
# first touched, so the scope is redirected to a temp folder BEFORE
# server.main is imported. No state file is ever created (or migrated) in the
# developer's real music folder or its .mlo/data.
# --------------------------------------------------------------------------- #
REAL_MUSIC_FOLDER = ""
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        REAL_MUSIC_FOLDER = str((json.load(f) or {}).get("music_folder") or "")
except Exception:
    pass

REDIRECT = tempfile.mkdtemp(prefix="mlo-artist-redirect-")
os.environ["MLO_MUSIC_FOLDER"] = REDIRECT

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB = os.path.join(REDIRECT, "config.json")
with open(_STUB, "w", encoding="utf-8") as f:
    json.dump({"music_folder": REDIRECT}, f)

for _mod in (cfgmod, pathmod):
    _mod.CONFIG_FILE = _STUB
    if getattr(_mod, "LEGACY_DATA_DIR", None) is not None:
        _mod.LEGACY_DATA_DIR = os.path.join(REDIRECT, "legacy")

MF = tempfile.mkdtemp(prefix="mlo-artist-test-")
LAYOUT_MF = tempfile.mkdtemp(prefix="mlo-artist-layout-")


def _cleanup():
    os.environ.pop("MLO_MUSIC_FOLDER", None)
    shutil.rmtree(REDIRECT, ignore_errors=True)
    shutil.rmtree(MF, ignore_errors=True)
    shutil.rmtree(LAYOUT_MF, ignore_errors=True)


atexit.register(_cleanup)

_REAL = REAL_MUSIC_FOLDER.replace("\\", "/").rstrip("/")
if _REAL:
    assert not MF.replace("\\", "/").lower().startswith(_REAL.lower()), \
        f"temp fixture {MF} sits inside the real music folder {_REAL}"

from mlo.grader import _classify_file, _disallowed_files, grade_artist  # noqa: E402
from server import main as mlo_main  # noqa: E402  (heavy import)

passed = 0


def ok(cond, label):
    global passed
    if not cond:
        print(f"  FAIL: {label}")
        raise SystemExit(1)
    passed += 1
    print(f"  ok: {label}")


def write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


# --------------------------------------------------------------------------- #
# grade_artist
# --------------------------------------------------------------------------- #
print("== grade_artist ==")
ART = os.path.join(MF, "Artists", "Artist")
# An artist folder is only a graded artist while it holds an album folder: one
# audio file in one album folder is the smallest thing that makes it one.
write(os.path.join(ART, "Ripped (2020)", "01 - Song.flac"))
write(os.path.join(ART, "artist.jpg"))

res = grade_artist(ART)
ok(res["path"] == ART, "the result carries the folder it graded")
ok(res["issues"] == [], f"an artist with an album has nothing to report ({res['issues']})")
ok((res["checks"], res["pass_count"], res["failed_checks"], res["pct"], res["pass"])
   == (0, 0, 0, 100.0, True),
   f"nothing graded is nothing failed — 100%, PASS ({res})")

# The grade owns no album-level check: settings cannot make it invent one
# (tags, logs, covers and the removed artwork checks all belong to the album
# grader, never here).
res = grade_artist(ART, {"grade_check_missing_tags": True,
                         "grade_check_cover": True,
                         "grade_check_album_tags": True})
ok(res["issues"] == [] and res["checks"] == 0 and res["pass"] is True,
   f"album-level settings change nothing here ({res})")

# A folder that is not there must not raise.
missing = os.path.join(MF, "Artists", "Nobody")
res = grade_artist(missing, {})
ok([i["code"] for i in res["issues"]] == ["ARTIST_FOLDER_MISSING"],
   f"a nonexistent folder reports ARTIST_FOLDER_MISSING ({res['issues']})")
ok(res["issues"][0]["label"] == "Artist folder"
   and res["issues"][0]["where"] == "Nobody",
   f"the issue names the folder ({res['issues'][0]})")
ok((res["checks"], res["pass_count"], res["failed_checks"], res["pct"],
    res["pass"]) == (0, 0, 0, 0.0, False),
   f"and fails without inventing checks ({res})")
ok(grade_artist("", {})["issues"][0]["code"] == "ARTIST_FOLDER_MISSING",
   "an empty path fails the same way instead of raising")

# An artist folder with NO album folder in it is not a graded artist: the
# shape of an absent folder (one issue, no checks invented). A loose audio
# file does not count either — an album folder is a DIRECTORY the artist holds.
solo = os.path.join(MF, "Artists", "Solo")
os.makedirs(solo, exist_ok=True)
write(os.path.join(solo, "artist.jpg"))
res = grade_artist(solo, {})
ok([i["code"] for i in res["issues"]] == ["ARTIST_EMPTY"],
   f"an artist folder with no album folder reports ARTIST_EMPTY "
   f"({res['issues']})")
ok(res["issues"][0]["label"] == "Artist albums"
   and res["issues"][0]["where"] == "Solo",
   f"the issue names the folder ({res['issues'][0]})")
ok(res["pass"] is False and res["checks"] == 0 and res["pct"] == 0.0,
   f"and FAILS without grading any artefact ({res})")

bare = os.path.join(MF, "Artists", "Bare")
write(os.path.join(bare, "01 - Song.flac"))
res = grade_artist(bare, {})
ok([i["code"] for i in res["issues"]] == ["ARTIST_EMPTY"],
   f"a loose audio file is not an album folder ({res['issues']})")

# The album folder arrives: the same folder is a graded artist again — the
# album's own problems are the album grader's to report, not this one's.
write(os.path.join(solo, "Album (2020)", "01 - Song.flac"))
res = grade_artist(solo, {})
ok(res["issues"] == [] and res["pass"] is True and res["checks"] == 0
   and res["pct"] == 100.0,
   f"an album folder clears it ({res['pass_count']}/{res['checks']})")

# A sub-folder is an album folder whatever it holds: the grade asks the one
# question mlo.layout's empty_artist asks, so the two can never disagree.
write(os.path.join(bare, "Some Folder", "readme.txt"))
res = grade_artist(bare, {})
ok(res["issues"] == [] and res["pass"] is True,
   f"any directory under the artist is an album folder ({res})")

# --------------------------------------------------------------------------- #
# the removed description sidecar
# --------------------------------------------------------------------------- #
print("== removed description sidecar ==")
ok(_classify_file("description.txt") == "other",
   "description.txt has no special category any more")
ok(_classify_file("notes.txt") == "other", "an unknown .txt is still unclassified")
ok(_disallowed_files(ART, ["description.txt"], {}) == ["description.txt"],
   f"it is an ordinary file the album refuses ({_disallowed_files(ART, ['description.txt'], {})})")

# --------------------------------------------------------------------------- #
# layout scanner: the album-less artist folder is the same finding
# --------------------------------------------------------------------------- #
print("== layout scanner ==")
LART = os.path.join(LAYOUT_MF, "Artists", "Artist")
write(os.path.join(LART, "Ripped (2020)", "01 - Song.flac"))
write(os.path.join(LART, "artist.jpg"))
write(os.path.join(LART, "Album", "01 - Song.flac"))
write(os.path.join(LART, "Album", "cover.jpg"))
LSOLO = os.path.join(LAYOUT_MF, "Artists", "Solo")
write(os.path.join(LSOLO, "artist.jpg"))

mlo_main.load_config = lambda: {"music_folder": LAYOUT_MF}
res = mlo_main.library_layout()
ok(res["counts"].get("stray_file", 0) == 0,
   f"artist.jpg / cover.jpg are artwork, not strays ({res['counts']})")
ok(res["counts"].get("empty_artist") == 1
   and any(i["kind"] == "empty_artist"
           and os.path.basename(str(i["path"]).replace("\\", "/")) == "Solo"
           for i in res["issues"]),
   f"the album-less artist folder is the scan's empty_artist ({res['issues']})")
ok(res["total"] == 1, f"a clean library reports only that ({res['issues']})")
ok(res["albums"] == 2 and res["artists"] == 2 and res["audio_files"] == 2,
   f"the fixture was really scanned ({res['albums']} album, "
   f"{res['artists']} artist, {res['audio_files']} audio)")

# control: a genuine stray in the same album is still reported
write(os.path.join(LART, "Album", "notes.txt"))
res = mlo_main.library_layout()
ok(res["counts"].get("stray_file") == 1,
   f"notes.txt is still a stray ({res['counts']})")
os.remove(os.path.join(LART, "Album", "notes.txt"))

print(f"\nAll {passed} checks passed.")