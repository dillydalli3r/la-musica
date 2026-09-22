#!/usr/bin/env python3
"""Verification for the layout scanner's `wrong_case` check.

`GET /api/library/layout` reports an artist folder, an album folder or a file
name that spells its name differently from the naming script the organizer
applies — but ONLY when letter case is the entire difference. The check has to
be case-sensitive to see anything at all (the filesystem is not), so this test
pins the three things that are easy to get wrong:

  * a correct library reports nothing,
  * each wrong-case name is reported once, at the music-folder-relative path,
  * a name that differs by more than case is NOT reported (the grader's PATH
    check owns that; a false row here pushes the user into renaming music to a
    name that is not actually correct),
  * nothing is ever moved.

Run:  python tools/test_layout_case.py
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# hermeticity: app paths resolve through the music folder the moment they are
# first touched, so the scope is redirected to a temp folder BEFORE server.main
# is imported. No state file is ever created (or migrated) in the developer's
# real music folder or its .mlo/data.
# --------------------------------------------------------------------------- #
REAL_MUSIC_FOLDER = ""
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        REAL_MUSIC_FOLDER = str((json.load(f) or {}).get("music_folder") or "")
except Exception:
    pass

REDIRECT = tempfile.mkdtemp(prefix="mlo-case-redirect-")
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

from server import main as mlo_main  # noqa: E402  (heavy import)

MF = tempfile.mkdtemp(prefix="mlo-case-test-")

_REAL = REAL_MUSIC_FOLDER.replace("\\", "/").rstrip("/")
if _REAL:
    assert not MF.replace("\\", "/").lower().startswith(_REAL.lower()), \
        f"temp fixture {MF} sits inside the real music folder {_REAL}"


def _cleanup():
    os.environ.pop("MLO_MUSIC_FOLDER", None)
    for d in (MF, REDIRECT):
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup)

# --------------------------------------------------------------------------- #
# a naming script with no conditional segments, so the fixture names below ARE
# the expected names: <Artist>/<Album>/<Disc>-<Track> <Title>
# --------------------------------------------------------------------------- #
SCRIPT = "%albumartist%/%album%/%discnumber%-$num(%tracknumber%,2) %title%"

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
    """A real, taggable FLAC — the scanner reads tags through the tag cache,
    so a size-only stand-in like the layout test's would not do."""
    assert FLAC_EXE, "flac.exe not found under .dependencies"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wav = path + ".wav"
    with wave.open(wav, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * 4410)
    subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", path, wav],
                   check=True, capture_output=True)
    os.remove(wav)


def album(rel_dir, tags, file_name="1-01 Song.flac"):
    """One album folder holding one tagged track named by *tags*. *file_name*
    is the name as STORED on disk — the whole point of the check, so it can be
    spelled differently from what the tags ask for."""
    fp = os.path.join(MF, rel_dir, file_name)
    make_flac(fp)
    if tags:
        from mutagen.flac import FLAC
        f = FLAC(fp)
        for k, v in tags.items():
            f[k] = [v]
        f.save()
    return fp


def tags(albumartist, alb, title="Song"):  # noqa: D103 - fixture helper
    return {"ALBUMARTIST": albumartist, "ARTIST": albumartist, "ALBUM": alb,
            "TITLE": title, "DISCNUMBER": "1", "TRACKNUMBER": "1"}


# ---- fixtures -------------------------------------------------------------- #
# every case scenario gets its own artist folder: on a case-insensitive
# filesystem "Good" and "good" would otherwise be the same directory, and the
# stored spelling would depend on creation order.
album("Artists/Good/Good Album", tags("Good", "Good Album"))          # clean
album("Artists/lower/Good Album", tags("Lower", "Good Album"))        # artist
album("Artists/lower/Second", tags("Lower", "Second"))                #   dup
album("Artists/Caps/bad album", tags("Caps", "Bad Album"))            # album
album("Artists/Caps/Other Name", tags("Caps", "Different Album"))     # not case
os.makedirs(os.path.join(MF, "Artists", "Caps", "Empty"), exist_ok=True)
with open(os.path.join(MF, "Artists", "Caps", "Empty", "cover.jpg"), "wb") as f:
    f.write(b"x")                                                     # empty_album
album("Artists/Files/My Album", tags("Files", "My Album"),
      file_name="1-01 song.flac")                                     # file
with open(os.path.join(MF, "Artists", "Good", "Good Album", "notes.txt"), "wb") as f:
    f.write(b"junk")                                                  # stray_file

# a size-only, untagged album: real audio files, no tags at all
os.makedirs(os.path.join(MF, "Artists", "Caps", "Untagged"), exist_ok=True)
with open(os.path.join(MF, "Artists", "Caps", "Untagged", "1-01 Song.flac"), "wb") as f:
    f.write(b"\0" * 8192)

# an artist folder with NO album folder in it: the artist's own image and
# description are everything it holds, and the library still lists it as an
# artist (the `empty_artist` finding, and the one row the panel may remove).
SOLO = os.path.join(MF, "Artists", "Solo")
os.makedirs(SOLO, exist_ok=True)
for _name in ("artist.jpg", "description.txt"):
    with open(os.path.join(SOLO, _name), "wb") as f:
        f.write(b"x")

# …and the near miss: a folder that holds AUDIO is never that finding — its
# audio is a problem of its own (audio_in_artist), and nothing may offer to
# remove a folder with music in it.
os.makedirs(os.path.join(MF, "Artists", "Loose"), exist_ok=True)
with open(os.path.join(MF, "Artists", "Loose", "1-01 Song.flac"), "wb") as f:
    f.write(b"x")


def listing():
    """Every relative path under the music folder, before and after the scan."""
    out = set()
    for root, dirs, names in os.walk(MF):
        for n in dirs + names:
            out.add(os.path.relpath(os.path.join(root, n), MF).replace("\\", "/"))
    return out


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #
from fastapi.testclient import TestClient  # noqa: E402

mlo_main.load_config = lambda: {"music_folder": MF, "naming_script": SCRIPT}
_client = TestClient(mlo_main.app)

before = listing()
r = _client.get("/api/library/layout")
assert r.status_code == 200, r.text
res = r.json()
after = listing()

issues = res["issues"]
cases = [i for i in issues if i["kind"] == "wrong_case"]
paths = [i["path"] for i in cases]

print("== wrong_case ==")
ok(res["artists_dir"].replace("\\", "/") == os.path.join(MF, "Artists").replace("\\", "/"),
   "scan ran against the temp music folder")
ok(sorted(paths) == ["Artists/Caps/bad album", "Artists/Files/My Album/1-01 song.flac",
                     "Artists/lower"],
   f"exactly the three wrong-case names are reported (got {sorted(paths)})")
ok(res["counts"].get("wrong_case") == 3,
   f"counts carries the new kind (got {res['counts'].get('wrong_case')})")
ok(all(not os.path.isabs(i["path"]) and "\\" not in i["path"] for i in cases),
   "wrong_case paths are music-folder-relative and forward-slashed")
ok(all(i["detail"] and i["hint"] for i in cases),
   "every wrong_case row explains itself and says how to fix it")
ok(all(os.path.isabs(i["abs"]) and os.path.exists(i["abs"]) for i in cases),
   "every wrong_case row carries the real absolute path")

# the artist row is the DEDUPED one: two albums share Artists/lower
ok(paths.count("Artists/lower") == 1,
   f"an artist folder shared by two albums is reported once (got {paths.count('Artists/lower')})")

# the correct library and the two silent cases stay silent
ok(not any(p.startswith("Artists/Good") for p in paths),
   "a correctly spelled album reports no wrong_case")
ok(not any(p.startswith("Artists/Caps/Other Name") for p in paths),
   "a name differing by more than case is not reported (that is the grader's PATH check)")
ok(not any(p.startswith("Artists/Caps/Untagged") for p in paths),
   "an album whose tags cannot be read reports nothing rather than guessing")

# the detail names the script's spelling, so the row is actionable
artist_row = next(i for i in cases if i["path"] == "Artists/lower")
ok("Lower" in artist_row["detail"], f"artist row names the expected spelling ({artist_row['detail']})")

print("== existing kinds unchanged ==")
ok(res["counts"].get("empty_album") == 1,
   f"empty_album still reported (got {res['counts'].get('empty_album')})")
ok(res["counts"].get("stray_file") == 1,
   f"stray_file still reported (got {res['counts'].get('stray_file')})")
ok("wrong_case" not in {i["kind"] for i in issues
                        if i["path"] in ("Artists/Good/Good Album", "Artists/Caps/Empty")},
   "the untouched issue kinds did not gain rows of their own")

print("== empty artist (an artist folder with no album) ==")
ea = [i for i in issues if i["kind"] == "empty_artist"]
ok(res["counts"].get("empty_artist") == 1
   and [i["path"] for i in ea] == ["Artists/Solo"],
   f"the artist folder holding no album folder is the only empty_artist row "
   f"({[i['path'] for i in ea]})")
ok(bool(ea[0]["detail"]) and "no album folder" in ea[0]["detail"]
   and "Solo" in ea[0]["detail"],
   f"the row says which folder and why ({ea[0]['detail']})")
ok("Trash" in ea[0]["hint"],
   f"the hint names the Trash — the only removal this app offers a user "
   f"({ea[0]['hint']})")
# The near miss: audio anywhere beneath keeps a folder out of this finding.
ok(not any(i["path"] == "Artists/Loose" for i in ea)
   and res["counts"].get("audio_in_artist") == 1,
   f"a folder holding audio is audio_in_artist, never an empty artist "
   f"({[i['kind'] for i in issues if i['path'] == 'Artists/Loose']})")

# The grade fails the same folder — the finding and the grade are one answer
# about one folder (mlo.layout.empty_artist, which both ask).
from mlo.grader import grade_artist  # noqa: E402

g = grade_artist(SOLO, {})
ok([i["code"] for i in g["issues"]] == ["ARTIST_EMPTY"] and g["pass"] is False,
   f"grade_artist fails it with ARTIST_EMPTY ({g['issues']})")
g2 = grade_artist(os.path.join(MF, "Artists", "Good"), {})
ok("ARTIST_EMPTY" not in [i["code"] for i in g2["issues"]] and g2["checks"] == 2,
   f"and an artist that holds an album is graded on its own artefacts, not "
   f"failed for the folder ({g2['checks']} checks, {[i['code'] for i in g2['issues']]})")

print("== removing an empty artist goes through the Trash ==")
BIN = os.path.join(MF, ".mlo", "trash")
r = _client.post("/api/library/layout/remove-empty-artist", json={"path": SOLO})
ok(r.status_code == 200, f"the route accepts it ({r.status_code}: {r.text[:160]})")
dest = str(r.json().get("trash") or "")
ok(not os.path.exists(SOLO), "the artist folder is gone from Artists/")
ok(os.path.isdir(dest)
   and os.path.normcase(dest).startswith(os.path.normcase(BIN)),
   f"…and landed in <music>/.mlo/trash/<user>/ ({dest})")
ok(os.path.isfile(os.path.join(dest, "artist.jpg"))
   and os.path.isfile(os.path.join(dest, "description.txt")),
   f"every file travelled with it — nothing was deleted "
   f"({sorted(os.listdir(dest))})")
with open(os.path.join(os.path.dirname(dest), ".mlo_manifest.json"),
          encoding="utf-8") as f:
    manifest = json.load(f)
entries = manifest.get("entries", manifest)
ok(str(entries.get(os.path.basename(dest), {}).get("origin", "")).replace("\\", "/")
   == SOLO.replace("\\", "/"),
   f"its origin is recorded, so the Trash page can put it back ({manifest})")

# The guards: the route re-derives the finding instead of trusting the panel.
for path, what in ((os.path.join(MF, "Artists", "Good"), "an artist with an album"),
                   (os.path.join(MF, "Artists", "Loose"), "a folder holding audio"),
                   (MF, "the music folder itself")):
    r2 = _client.post("/api/library/layout/remove-empty-artist", json={"path": path})
    ok(r2.status_code == 400,
       f"{what} is refused, not moved ({r2.status_code}: {r2.text[:120]})")
ok(os.path.isdir(os.path.join(MF, "Artists", "Good"))
   and os.path.isdir(os.path.join(MF, "Artists", "Loose")),
   "and both are still on disk")

print("== read-only ==")
# Read-only means the LIBRARY is untouched: the scan renames, moves and
# rewrites nothing it reports on. It writes exactly one thing of its own — the
# report the Library page warns from, under the app's state folder (.mlo/data)
# — so the delta may contain that and nothing else.
created = after - before
ok(not (before - after), f"the scan deleted nothing ({sorted(before - after)})")
ok(all(p == ".mlo" or p.startswith(".mlo/") for p in created),
   f"the only thing the scan writes is its own state ({sorted(created)})")
ok(".mlo/data/layout_report.json" in created,
   "the report lands where the Library page reads it")
for p in ("Artists/lower", "Artists/Caps/bad album",
          "Artists/Files/My Album/1-01 song.flac"):
    ok(os.path.exists(os.path.join(MF, *p.split("/"))),
       f"the wrong-case name is still on disk: {p}")

print(f"\nAll {passed} checks passed.")
