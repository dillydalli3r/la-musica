#!/usr/bin/env python3
"""Album leftovers: graded as a failure, and swept to the Trash.

The owner's report, 2026-10-13: *"the app isn't recognizing extra files / folder
properly as incorrect for grading nor is it getting rid of them automatically
whenever scripts / importing are run"* — an artist folder holding a gone album's
`CD-1.cue`/`.log`/`.accurip`, `cover.jpg`, `description (2).txt`, a
`.mb_expected` and a `.mlo_importing.json`, with the grade calling it fine and
the chain never touching any of it.

Pinned here, on a temp library:

  * the predicate (`mlo.leftovers`): an album's file is a leftover in a folder
    that is not an album, and the artist's own `artist.jpg`/`description.txt`
    never is;
  * a folder holding AUDIO of its own is an album: nothing in it is a leftover;
  * a LIVE import marker is nobody's debris, a stale one is;
  * the layout scan (script 20) reports each leftover with a Trash fix, and the
    apply moves them — artist files, album folders and the rip sidecars INSIDE
    an album folder all untouched, a multi-disc album's sheets beside
    `CD1`/`CD2` included;
  * the grade: `LEFT_OVER_FILES` on a folder that is not an album, nothing on
    an album (its own sidecars) or on a framework album, and the switch
    (`grade_check_leftovers`) that turns it off;
  * the import's own sweep (`server.imports.sweep_leftovers`) on the album
    folder's PARENT — and its refusal while an import marker there is live.

Run: py tools/test_leftovers.py
Exit 0 = pass, 1 = a case failed, 2 = cannot run here.
"""
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Hermeticity first: every app path resolves through the music folder the moment
# it is touched, so redirect the scope to a temp folder BEFORE importing the app
# modules — no state file is ever created in the developer's real library.
REDIRECT = tempfile.mkdtemp(prefix="mlo-leftovers-redirect-")
os.environ["MLO_MUSIC_FOLDER"] = REDIRECT

passed = 0


def ok(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok: {label}")


def write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def age(path, seconds):
    old = time.time() - seconds
    os.utime(path, (old, old))


MF = tempfile.mkdtemp(prefix="mlo-leftovers-lib-")
ARTIST = os.path.join(MF, "Artists", "Gorillaz [e21857d5]")
ALBUM = os.path.join(ARTIST, "[Album] 2001-03-24 - Gorillaz {US - CD}")
DISC = os.path.join(ARTIST, "[Album] 2005-05-11 - Demon Days {JP - CD}")

# an artist folder: its own two files, one real album, and one DISCOURSE album
# whose sheets sit beside its disc folders (the false positive this must never
# report: that folder IS an album).
write(os.path.join(ARTIST, "artist.jpg"))
write(os.path.join(ARTIST, "description.txt"), "Gorillaz are a British virtual band.\n")
write(os.path.join(ALBUM, "1-01 Re-Hash.flac"), "\0" * 4096)
write(os.path.join(ALBUM, "CD-1.cue"), 'FILE "1-01 Re-Hash.flac" WAVE\n')
write(os.path.join(ALBUM, "CD-1.log"), "Exact Audio Copy\n")
write(os.path.join(ALBUM, "cover.jpg"))
write(os.path.join(DISC, "CD1", "1-01 Intro.flac"), "\0" * 4096)
write(os.path.join(DISC, "CD2", "2-01 Last Living Souls.flac"), "\0" * 4096)
write(os.path.join(DISC, "CD-1.cue"), 'FILE "x.flac" WAVE\n')
write(os.path.join(DISC, "CD-2.cue"), 'FILE "y.flac" WAVE\n')

# the leftovers: a gone album's own files, one level above any album.
LEFTOVERS = {
    ".mb_expected": '{"version": 1, "release_id": "f525d01a", "tracks": []}',
    "CD-1.accurip": "AccurateRip summary\n",
    "CD-1.cue": 'FILE "x.flac" WAVE\n',
    "CD-1.log": "Exact Audio Copy\n",
    "Gorillaz - The Mountain.log": "Exact Audio Copy\n\nAlbum: The Mountain\n",
    "The Mountain.cue": 'PERFORMER "Gorillaz"\n',
    "cover.jpg": "jpeg",
    "description (2).txt": "The Mountain is the ninth studio album.\n",
}
for name, text in LEFTOVERS.items():
    write(os.path.join(ARTIST, name), text)
    age(os.path.join(ARTIST, name), 24 * 3600)

# a live import marker in a SIBLING artist folder: an import is working there,
# so none of its files are debris — and its stale twin one level up is.
LIVE = os.path.join(MF, "Artists", "Live")
write(os.path.join(LIVE, "cover.jpg"))
write(os.path.join(LIVE, ".mlo_importing.json"), '{"album": "x", "step": 4, "session": true}\n')

# a framework album (pending): its manifest and cover are the app's own
# anticipation of an album, not leftovers.
PENDING = os.path.join(MF, "Artists", "Pending", "The Mountain")
write(os.path.join(PENDING, ".mb_expected"), '{"version": 1, "release_id": "x", "tracks": []}')
write(os.path.join(PENDING, ".mlo_pending.json"), '{"release": "x"}\n')
write(os.path.join(PENDING, "cover.jpg"))

from mlo import grader, layout, leftovers  # noqa: E402

print("predicate")
ok(leftovers.is_leftover("CD-1.cue") and leftovers.is_leftover(".mb_expected")
   and leftovers.is_leftover("cover.jpg") and leftovers.is_leftover("description (2).txt")
   and leftovers.is_leftover(".mlo_importing.json")
   and not leftovers.is_leftover("artist.jpg")
   and not leftovers.is_leftover("description.txt")
   and not leftovers.is_leftover("1-01 Song.flac")
   and not leftovers.is_leftover("notes.txt"),
   "an album's file is a leftover; the artist's own files, audio and notes are not")
ok(leftovers.leftover_names(ARTIST) == sorted(LEFTOVERS),
   f"the artist folder's leftovers are found: {leftovers.leftover_names(ARTIST)}")
ok(leftovers.leftover_names(ALBUM) == [] and leftovers.leftover_names(DISC) == [],
   "an album folder's own sidecars are never leftovers (disc structure included)")
ok(leftovers.import_running(LIVE) and not leftovers.import_running(ARTIST),
   "a fresh import marker is live, a folder without one is not")

print("grading")
scan2 = {}
albums = grader._find_albums(MF, scan2)
found = dict((p, names) for p, names in grader._find_leftover_folders(MF, scan2, albums))
ok(ARTIST in found and not any(ALBUM == p or DISC == p or PENDING in p or LIVE in p for p in found),
   f"LEFT_OVER_FILES is charged to the artist folder only: {sorted(found)}")
row = grader._leftover_folder_result(ARTIST, MF, found[ARTIST])
ok(row["issues"].get(grader.LEFT_OVER_FILES) and row["total_checks"] == 1 and row["pass_count"] == 0,
   "the row is a failed check, like the empty-folder one")
ok(grader.LEFT_OVER_FILES == "LEFT_OVER_FILES" and "script 20" in row["notes"][0],
   "the row's note names the files and what removes them")
print("the layout scan (script 20)")
scan = layout.scan_library({"music_folder": MF, "naming_script": ""})
rows = [i for i in scan["issues"] if i["kind"] == "leftover_file"]
paths = sorted(os.path.basename(i["abs"]) for i in rows)
ok(paths == sorted(LEFTOVERS), f"each leftover is reported: {paths}")
ok(all(i.get("fix", {}).get("action") == "trash" for i in rows),
   "every leftover row carries a Trash fix")
ok(not [i for i in scan["issues"]
        if i["kind"] == "leftover_file" and ("Demon Days" in i["path"] or "Gorillaz {US" in i["path"])],
   "an album folder's own sidecars are not reported, disc folders included")
ok(not [i for i in scan["issues"] if i["kind"] == "leftover_file" and "Live" in i["path"]],
   "a folder an import is running in is left alone")
ok(not [i for i in scan["issues"] if i["kind"] == "leftover_file" and "Pending" in i["path"]],
   "a framework album's manifest and cover are its own, not leftovers")

report = layout.apply_fixes({"music_folder": MF, "naming_script": ""})
fixed = [r for r in report["fixes"] if r["kind"] == "leftover_file" and r["result"] == "fixed"]
ok(len(fixed) == len(LEFTOVERS), f"the apply moves them all: {len(fixed)}/{len(LEFTOVERS)}")
left = sorted(os.listdir(ARTIST))
ok(left == sorted(["[Album] 2001-03-24 - Gorillaz {US - CD}", "artist.jpg", "description.txt",
                   "[Album] 2005-05-11 - Demon Days {JP - CD}"]),
   f"the artist folder keeps only its own files and its albums: {left}")
ok(os.path.isdir(os.path.join(ALBUM, "CD-1.cue")) is False
   and os.path.isfile(os.path.join(ALBUM, "CD-1.cue"))
   and os.path.isfile(os.path.join(DISC, "CD-2.cue")),
   "the albums keep their own sidecars")
bin_dir = os.path.join(MF, ".mlo", "trash", "default")
ok(all(os.path.exists(os.path.join(bin_dir, n)) for n in LEFTOVERS),
   f"every leftover is in the Trash (restorable): {sorted(os.listdir(bin_dir))}")

print("after the sweep")
scan3 = {}
albums3 = grader._find_albums(MF, scan3)
ok(grader._find_leftover_folders(MF, scan3, albums3) == [],
   "the folders the apply swept are no longer findings")
write(os.path.join(ARTIST, "cover.jpg"), "jpeg")
age(os.path.join(ARTIST, "cover.jpg"), 24 * 3600)
scan4 = {}
albums4 = grader._find_albums(MF, scan4)
ok([p for p, _ in grader._find_leftover_folders(MF, scan4, albums4)] == [ARTIST],
   "one leftover file brings the folder back")

print("the import's own sweep")
write(os.path.join(ARTIST, "CD-1.cue"), 'FILE "x.flac" WAVE\n')
age(os.path.join(ARTIST, "CD-1.cue"), 24 * 3600)
from server import imports  # noqa: E402  (after mlo: the server imports mlo)
moved = imports.sweep_leftovers(ALBUM,   # the ALBUM folder, as an import hands it over
                                cfg={"music_folder": MF, "grade_check_leftovers": True})
ok(sorted(moved) == ["CD-1.cue", "cover.jpg"] or sorted(moved) == ["cover.jpg", "CD-1.cue"],
   f"an import sweeps its album folder's PARENT: {moved}")
ok(not os.path.exists(os.path.join(ARTIST, "CD-1.cue")),
   "the swept file is gone from the artist folder")
ok(imports.sweep_leftovers(os.path.join(LIVE, "x.flac"),
                           cfg={"music_folder": MF, "grade_check_leftovers": True}) == []
   and os.path.exists(os.path.join(LIVE, "cover.jpg")),
   "a folder an import is running in is refused, its files left")
ok(imports.sweep_leftovers(os.path.join(ALBUM, "x.flac"),
                           cfg={"music_folder": MF, "grade_check_leftovers": False}) == [],
   "the sweep obeys the same switch the grade reads")

print("the library lists a shell ONCE")
# A two-disc AUTO-import's shell: the chain moved the disc folders' audio into
# the album folder and left the rip's sheets in the staging folder it renamed.
# That folder is BOTH "no audio anywhere beneath it" (EMPTY_FOLDER) and "holds
# an album's leftovers" (LEFT_OVER_FILES), and the library payload appended one
# row per finding — so the artist grid showed the same folder TWICE, as two
# identical failing tiles captioned with its name (the owner's screenshot,
# 2026-10-13: two "Disc 1" tiles on a two-disc auto-import).
from server import library as library_mod  # noqa: E402

SHELL = os.path.join(MF, "Artists", "Gorillaz [e21857d5]", "Disc 1")
write(os.path.join(SHELL, "cover.jpg"))
write(os.path.join(SHELL, "CD-1.cue"), 'FILE "x.flac" WAVE\n')
picked = {"music_folder": MF, "grade_check_empty_folders": True,
          "grade_check_leftovers": True}
_scan_shell = {}
_albums_shell = grader._find_albums(MF, _scan_shell)
ok(SHELL in grader._find_empty_folders(MF, _scan_shell)
   and SHELL in [p for p, _n in grader._find_leftover_folders(MF, _scan_shell, _albums_shell)],
   "the two finders BOTH name it (which is why the rows must be deduped)")
tree = library_mod._library_builder(MF, picked)()
shell_rows = [a for ar in tree["artists"] for a in ar["albums"]
              if os.path.normcase(a.get("path") or "") == os.path.normcase(SHELL)]
ok(len(shell_rows) == 1,
   f"one row for one folder: {[sorted((r.get('issues') or {})) for r in shell_rows]}")
ok(bool(shell_rows) and "LEFT_OVER_FILES" in (shell_rows[0].get("issues") or {}),
   "...and it is the LEFT_OVER_FILES row, which names the files that are there: "
   f"{shell_rows[0].get('issues') if shell_rows else None}")

shutil.rmtree(MF, ignore_errors=True)
shutil.rmtree(REDIRECT, ignore_errors=True)
print(f"\nPASS — {passed} checks")