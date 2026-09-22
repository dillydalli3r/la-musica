#!/usr/bin/env python3
"""Verify artist-level grading (mlo.grader.grade_artist) and the sidecar
tolerance the artwork / description features depend on.

Artist grading covers ONLY what applies to an artist folder: its artist.jpg
and its description.txt. The image check reads the decoded file (its size,
aspect and pixels, mlo.artistdata's policy), so the fixtures below are REAL
images: a junk byte string named artist.jpg is a corrupt image, which is a
failure of its own. The same files — plus the album's own
description.txt — are legitimate library content for the album grader and for
the read-only layout scanner, so neither may report them as stray.

Run: python tools/test_artist_grading.py  (exit 0 pass, 1 fail)
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


def _cleanup():
    os.environ.pop("MLO_MUSIC_FOLDER", None)
    shutil.rmtree(REDIRECT, ignore_errors=True)
    shutil.rmtree(MF, ignore_errors=True)


atexit.register(_cleanup)

_REAL = REAL_MUSIC_FOLDER.replace("\\", "/").rstrip("/")
if _REAL:
    assert not MF.replace("\\", "/").lower().startswith(_REAL.lower()), \
        f"temp fixture {MF} sits inside the real music folder {_REAL}"

from mlo.grader import (  # noqa: E402
    ARTIST_CHECKS, _classify_file, _disallowed_files, _extra_images, grade_artist,
)
from server import main as mlo_main  # noqa: E402  (heavy import)

passed = 0


def ok(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok: {label}")


def write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def image_bytes(w=100, h=100, fmt="PNG"):
    """A real (if flat) image, so the image check can decode and measure it."""
    import io

    from mlo.deps import Image, HAS_PIL
    assert HAS_PIL, "these fixtures need Pillow"
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(buf, fmt)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# grade_artist
# --------------------------------------------------------------------------- #
print("== grade_artist ==")
ART = os.path.join(MF, "Artists", "Artist")
# An artist folder is only a graded artist while it holds an album: a folder
# with nothing but the artist's own image and description IS the ARTIST_EMPTY
# finding (asserted on its own folder below). One audio file in one album
# folder is the smallest thing that makes this one an artist with music.
write(os.path.join(ART, "Ripped (2020)", "01 - Song.flac"))
IMAGE = write(os.path.join(ART, "artist.jpg"), image_bytes(fmt="JPEG"))
DESC = write(os.path.join(ART, "description.txt"), b"A band from nowhere.\n")

ok([c["key"] for c in ARTIST_CHECKS]
   == ["grade_check_artist_image", "grade_check_artist_description"],
   "ARTIST_CHECKS lists the image check first")
ok([c["label"] for c in ARTIST_CHECKS] == ["Artist image", "Artist description"],
   "ARTIST_CHECKS carries the UI labels")
ok(all(c.get("description") for c in ARTIST_CHECKS),
   "every entry documents itself")

res = grade_artist(ART)
ok(res["path"] == ART, "the result carries the folder it graded")
ok((res["checks"], res["pass_count"], res["failed_checks"]) == (2, 2, 0),
   f"image + description grade 2/2 ({res['pass_count']}/{res['checks']})")
ok(res["pct"] == 100.0 and res["pass"] is True and res["issues"] == [],
   f"a complete artist folder passes ({res})")
ok(res["artwork"] == {"image": True, "image_file": IMAGE, "description": True},
   f"artwork reports the image file on disk ({res['artwork']})")

# each artefact missing fails only its own check
os.remove(IMAGE)
res = grade_artist(ART, {})
ok(res["checks"] == 2 and res["failed_checks"] == 1 and res["pass"] is False,
   f"a missing artist image fails exactly one check ({res['pass_count']}/{res['checks']})")
ok([i["code"] for i in res["issues"]] == ["ARTIST_IMAGE_MISSING"],
   f"only ARTIST_IMAGE_MISSING is raised ({res['issues']})")
ok(res["issues"][0]["label"] == "Artist image"
   and res["issues"][0]["where"] == "Artist",
   f"the issue names the check and the folder ({res['issues'][0]})")
ok(res["pct"] == 50.0, f"pct follows the graded checks ({res['pct']})")
ok(res["artwork"]["image"] is False and res["artwork"]["image_file"] is None
   and res["artwork"]["description"] is True,
   "artwork says which half is missing")

os.remove(DESC)
write(os.path.join(ART, "artist.png"), image_bytes())
res = grade_artist(ART, {})
ok([i["code"] for i in res["issues"]] == ["ARTIST_DESCRIPTION_MISSING"],
   f"only ARTIST_DESCRIPTION_MISSING is raised ({res['issues']})")
ok(res["artwork"]["image"] is True
   and res["artwork"]["image_file"].endswith("artist.png"),
   f"artist.png counts as the artist image ({res['artwork']})")

# one toggle off: that artefact is neither required nor counted
res = grade_artist(ART, {"grade_check_artist_description": False})
ok(res["checks"] == 1 and res["pass_count"] == 1 and res["pass"] is True,
   "a disabled check is not counted (1/1)")
res = grade_artist(ART, {"grade_check_artist_image": False})
ok([i["code"] for i in res["issues"]] == ["ARTIST_DESCRIPTION_MISSING"]
   and res["pct"] == 0.0,
   f"the other toggle still grades its own artefact ({res})")

# both off: nothing graded is nothing failed — and nothing graded is 100%, not
# 0%: the album rule (format_grade_report) reads the same state as a full
# score, so a passing artist folder must not display an empty one.
res = grade_artist(ART, {"grade_check_artist_image": False,
                         "grade_check_artist_description": False})
ok((res["checks"], res["pass_count"], res["failed_checks"], res["pct"],
    res["pass"], res["issues"])
   == (0, 0, 0, 100.0, True, []),
   f"both checks disabled → checks=0, pct=100, pass=True ({res})")

# a folder that is not there must not raise
missing = os.path.join(MF, "Artists", "Nobody")
res = grade_artist(missing, {})
ok([i["code"] for i in res["issues"]] == ["ARTIST_FOLDER_MISSING"],
   f"a nonexistent folder reports ARTIST_FOLDER_MISSING ({res['issues']})")
ok((res["checks"], res["pass_count"], res["failed_checks"], res["pct"],
    res["pass"]) == (0, 0, 0, 0.0, False),
   f"and fails without inventing checks ({res})")
ok(res["artwork"] == {"image": False, "image_file": None, "description": False},
   "and reports no artwork")
ok(grade_artist("", {})["issues"][0]["code"] == "ARTIST_FOLDER_MISSING",
   "an empty path fails the same way instead of raising")

# An artist folder with NO album folder in it is not a graded artist: the same
# shape as an absent folder (one issue, no checks invented), because a perfect
# image and description describe an artist, never an album. The removal the
# layout panel offers for it goes through the Trash.
solo = os.path.join(MF, "Artists", "Solo")
os.makedirs(solo, exist_ok=True)
write(os.path.join(solo, "artist.jpg"), image_bytes(fmt="JPEG"))
write(os.path.join(solo, "description.txt"), b"A band from nowhere.\n")
res = grade_artist(solo, {})
ok([i["code"] for i in res["issues"]] == ["ARTIST_EMPTY"],
   f"an artist folder with no album folder reports ARTIST_EMPTY "
   f"({res['issues']})")
ok(res["pass"] is False and res["checks"] == 0 and res["pct"] == 0.0,
   f"and FAILS without grading the artefacts as if it held an album ({res})")
ok(res["artwork"]["image"] is True and res["artwork"]["description"] is True,
   f"…while its artwork is still reported ({res['artwork']})")

# An album folder with audio under it is what makes it a graded artist again:
# the album's own problems are the album grader's to report, not this one's.
write(os.path.join(solo, "Album (2020)", "01 - Song.flac"))
res = grade_artist(solo, {})
ok(res["issues"] == [] and res["pass"] is True and res["checks"] == 2,
   f"an album folder clears it ({res['pass_count']}/{res['checks']})")

# --------------------------------------------------------------------------- #
# grader file classification (album side)
# --------------------------------------------------------------------------- #
print("== grader sidecar tolerance ==")
ok(_classify_file("description.txt") == "description",
   "description.txt is its own (allowed) category")
ok(_classify_file("notes.txt") == "other", "an unknown .txt is still unclassified")
ok(_disallowed_files(ART, ["description.txt", "notes.txt"], {}) == ["notes.txt"],
   f"description.txt is not a disallowed file ({_disallowed_files(ART, ['description.txt', 'notes.txt'], {})})")
ok(_disallowed_files(ART, ["description.txt"], {"grade_include_description": False})
   == ["description.txt"],
   "the category can still be turned off explicitly")
ok(_extra_images(ART, ["artist.jpg", "artist.png", "description.txt"], []) == [],
   "artist images are not stray album art")
ok(_extra_images(ART, ["front.jpg"], []) == ["front.jpg"],
   f"other loose images are still strays ({_extra_images(ART, ['front.jpg'], [])})")

# --------------------------------------------------------------------------- #
# layout scanner: the app's own sidecars are expected, not strays
# --------------------------------------------------------------------------- #
print("== layout scanner ==")
ALBUM = os.path.join(MF, "Artists", "Artist", "Album")
write(os.path.join(ALBUM, "01 - Song.flac"))
write(os.path.join(ALBUM, "cover.jpg"))
write(os.path.join(ALBUM, "description.txt"), b"Album notes.\n")
write(os.path.join(ART, "artist.jpg"), image_bytes(fmt="JPEG"))
write(os.path.join(ART, "description.txt"), b"A band from nowhere.\n")

mlo_main.load_config = lambda: {"music_folder": MF}
res = mlo_main.library_layout()
ok(res["counts"].get("stray_file", 0) == 0,
   f"no stray_file for artist.jpg / description.txt ({res['counts']})")
ok(not {i["kind"] for i in res["issues"]}
   & {"stray_file", "unexpected_subfolder", "stray_in_artists",
      "audio_in_artist", "audio_at_root", "unexpected_folder"},
   f"no shape issues for the sidecars ({res['issues']})")
ok(res["total"] == 0, f"a clean library reports nothing at all ({res['issues']})")
# Three album folders under two artists: the fixtures above (Artist's Ripped,
# Artist's Album from this block, and Solo's) are all part of this tree.
ok(res["albums"] == 3 and res["artists"] == 2 and res["audio_files"] == 3,
   f"the fixture was really scanned ({res['albums']} album, "
   f"{res['artists']} artist, {res['audio_files']} audio)")

# control: a genuine stray in the same album is still reported
write(os.path.join(ALBUM, "notes.txt"))
res = mlo_main.library_layout()
ok(res["counts"].get("stray_file") == 1
   and any(i["detail"].endswith("known sidecar") for i in res["issues"]),
   f"notes.txt is still a stray ({res['issues']})")
os.remove(os.path.join(ALBUM, "notes.txt"))

print(f"\nAll {passed} checks passed.")
