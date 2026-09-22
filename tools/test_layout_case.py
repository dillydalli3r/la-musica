#!/usr/bin/env python3
"""Verification for the layout scanner's `wrong_case` check and its apply phase.

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
  * the scan alone moves nothing.

The second half covers the apply phase (`POST /api/library/layout/apply`,
script 20): the canonical spelling is restored on an artist folder, an album
folder and a file; audio loose in an artist folder is moved into the album
folder its own tags name; an album-less artist folder goes to the Trash with
its origin recorded; `layout_apply: false` leaves everything alone; and a run
with targets touches only the target's subtree.

Run:  python tools/test_layout_case.py
"""
import atexit
import hashlib
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

def hashes(root):
    """{music-folder-relative path: sha256} for every file under *root*.

    `listing()` answers "does this path exist"; this answers "are these still
    the same bytes" — which is what a scoped run has to leave behind for
    everything it was not pointed at."""
    out = {}
    for base, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(base, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root).replace("\\", "/")] = \
                    hashlib.sha256(fh.read()).hexdigest()
    return out


def stored(parent, name):
    """Whether *parent* holds an entry spelled EXACTLY *name*.

    `exists`/`isdir` cannot answer that — the filesystem is case-insensitive,
    which is the whole reason this module exists — so every assertion about a
    name's SPELLING goes through the raw directory entries."""
    try:
        return name in os.listdir(parent)
    except OSError:
        return False


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

# --------------------------------------------------------------------------- #
# apply — what script 20 does with what the scan proved
# --------------------------------------------------------------------------- #
# The fixtures the read-only half left alone are exactly the apply's subjects,
# plus two the report alone cannot describe: audio loose in an artist folder
# that its own TAGS can place (the 1-byte stand-in above has no tags, so it
# stays a report), and a fresh album-less artist folder to remove.
LOOSE = album("Artists/Loose", tags("Loose", "Loose Album"))
NOBODY = os.path.join(MF, "Artists", "Nobody")
os.makedirs(NOBODY, exist_ok=True)
with open(os.path.join(NOBODY, "artist.jpg"), "wb") as f:
    f.write(b"x")

print("== apply fixes ==")
r = _client.post("/api/library/layout/apply")
ok(r.status_code == 200, f"the apply route accepts it ({r.status_code}: {r.text[:160]})")
res = r.json()
fixes = {f["path"]: f for f in res.get("fixes", [])}

ok(stored(os.path.join(MF, "Artists"), "Lower")
   and not stored(os.path.join(MF, "Artists"), "lower"),
   "the wrong-case ARTIST folder is now spelled the script's way (Artists/lower -> Artists/Lower)")
ok(stored(os.path.join(MF, "Artists", "Caps"), "Bad Album")
   and not stored(os.path.join(MF, "Artists", "Caps"), "bad album"),
   "the wrong-case ALBUM folder is now spelled the script's way (bad album -> Bad Album)")
ok(stored(os.path.join(MF, "Artists", "Files", "My Album"), "1-01 Song.flac")
   and not stored(os.path.join(MF, "Artists", "Files", "My Album"), "1-01 song.flac"),
   "the wrong-case FILE is now spelled the script's way (1-01 song.flac -> 1-01 Song.flac)")
ok(stored(os.path.join(MF, "Artists", "Loose"), "Loose Album")
   and stored(os.path.join(MF, "Artists", "Loose", "Loose Album"), "1-01 Song.flac")
   and not os.path.exists(os.path.join(MF, "Artists", "Loose", "1-01 Song.flac")),
   "audio loose in an artist folder moved into the album folder its own tags name")
ok(not os.path.exists(NOBODY),
   "the album-less artist folder is gone from Artists/")

# The counts, and the rows: what is fixed leaves `issues`, what is not stays.
ok(res.get("fixed") == 5 and res.get("fix_failed") == 0,
   f"five fixes, none failed (got fixed={res.get('fixed')} failed={res.get('fix_failed')})")
ok(res.get("skipped") == 2 and sorted(res["counts"]) == ["empty_album", "stray_file"],
   f"the two rows nothing may act on are still reported ({res.get('skipped')} skipped, {res['counts']})")
ok(res["total"] == len(res["issues"]) == 2,
   f"total/counts/issues agree after the fixes ({res['total']}, {len(res['issues'])})")
ok(all(f["result"] in ("fixed", "failed", "skipped") and f["action"]
       for f in res.get("fixes", [])),
   "every outcome is a result plus words, so the panel can say what happened")
ok("renamed" in fixes.get("Artists/lower", {}).get("action", ""),
   f"the rename is worded for the user ({fixes.get('Artists/lower')})")
ok("moved" in fixes.get("Artists/Loose/1-01 Song.flac", {}).get("action", "")
   and "Loose Album" in fixes.get("Artists/Loose/1-01 Song.flac", {}).get("action", ""),
   f"the move names the album folder it landed in ({fixes.get('Artists/Loose/1-01 Song.flac')})")
ok("no longer" in fixes.get("Artists/Loose/1-01 Song.flac", {}).get("action", "")
   or "Loose Album" in fixes.get("Artists/Loose/1-01 Song.flac", {}).get("action", ""),
   "…and the row is about the file, not an internal path")

# The removal goes to the app's Trash, with its origin recorded — never a delete.
r = _client.post("/api/library/layout/remove-empty-artist", json={"path": NOBODY})
ok(r.status_code == 404, f"a folder that is already gone is a 404, not a crash ({r.status_code})")
bin_dir = os.path.join(MF, ".mlo", "trash")
landed = [os.path.join(bin_dir, n, entry)
          for n in os.listdir(bin_dir)
          if os.path.isdir(os.path.join(bin_dir, n))
          for entry in os.listdir(os.path.join(bin_dir, n))
          if entry == "Nobody"]
ok(len(landed) == 1 and os.path.isfile(os.path.join(landed[0], "artist.jpg")),
   f"the artist folder landed in <music>/.mlo/trash/<scope>/Nobody with its file ({landed})")
with open(os.path.join(os.path.dirname(landed[0]), ".mlo_manifest.json"), encoding="utf-8") as f:
    entries = json.load(f).get("entries", {})
ok(str(entries.get("Nobody", {}).get("origin", "")).replace("\\", "/")
   == NOBODY.replace("\\", "/"),
   "…and the bin records where it came from, so the Trash page can restore it")

print("== layout_apply off: report only ==")
from mlo import layout as layoutmod  # noqa: E402

# Two more wrong-case album folders, for this half and the target half below:
# nothing has touched them yet, so a report-only run and a scoped run each have
# something to leave alone.
album("Artists/Quiet/case album", tags("Quiet", "Case Album"))
album("Artists/Other/case album", tags("Other", "Case Album"))

# Quietly: the runner writes to stdout, which is not what this asserts.
stats = layoutmod.run_scan_layout({"music_folder": MF, "naming_script": SCRIPT,
                                   "layout_apply": False})
ok(stored(os.path.join(MF, "Artists", "Quiet"), "case album")
   and stored(os.path.join(MF, "Artists", "Other"), "case album"),
   "with layout_apply off the run renames nothing")
ok(not stats.get("layout_fixed") and stats.get("layout_total", 0) >= 2,
   f"…and it still reports what is wrong ({stats.get('layout_fixed')} fixed, "
   f"{stats.get('layout_total')} reported)")

print("== targets confine the run ==")
# What the import chain does: script 20 per album folder. Only that subtree may
# be scanned AND fixed — the other artist's spelling is not this run's business,
# and nothing outside the target may change a single byte. Hash every file
# outside the target before and after: spelling alone would not catch a run
# that rewrote somebody else's music, and this is the check the "an import only
# costs one album" promise stands on. `.mlo` is the app's own state (the
# library-wide report, the Trash), not library content.
target = os.path.join(MF, "Artists", "Quiet", "case album")
# The target album folder IS the run's subject — its own rename (case album ->
# Case Album) is the fix being asserted below — so both spellings of it are
# dropped from the comparison. Everything else, the target's artist folder
# included, has to hash the same: the scoped run visits the artist folder and
# this one album, and nothing else.
TARGET_PARENT = os.path.relpath(os.path.dirname(target), MF).replace("\\", "/") + "/"
TARGET_NAME = os.path.basename(target).casefold()


def outside_hashes():
    def is_target(path):
        if not path.startswith(TARGET_PARENT):
            return False
        return path[len(TARGET_PARENT):].split("/")[0].casefold() == TARGET_NAME

    return {p: h for p, h in hashes(MF).items()
            if not is_target(p) and not p.startswith(".mlo/")}


before_outside = outside_hashes()
stats = layoutmod.run_scan_layout({"music_folder": MF, "naming_script": SCRIPT,
                                   "layout_apply": True, "targets": [target]})
after_outside = outside_hashes()
ok(after_outside == before_outside,
   "every file outside the target album is byte for byte identical after the "
   f"scoped run ({len(before_outside)} files; "
   f"{sorted(set(after_outside) ^ set(before_outside))[:5]} differ)")
ok(stored(os.path.join(MF, "Artists", "Quiet"), "Case Album")
   and not stored(os.path.join(MF, "Artists", "Quiet"), "case album"),
   "the targeted album folder is fixed")
ok(stored(os.path.join(MF, "Artists", "Other"), "case album"),
   "an artist outside the target is left exactly as it was")
ok(stats.get("layout_fixed") == 1 and stats.get("layout_total", 0) == 0,
   f"the scoped run counts only its own subtree ({stats.get('layout_fixed')} fixed, "
   f"{stats.get('layout_total')} left)")
ok(not stats.get("report_path"),
   f"a scoped run stores nothing — a partial report must never become 'the last "
   f"scan' ({stats.get('report_path')!r})")

# …and the whole-library run still fixes what the scoped one left alone.
stats = layoutmod.run_scan_layout({"music_folder": MF, "naming_script": SCRIPT,
                                   "layout_apply": True})
ok(stored(os.path.join(MF, "Artists", "Other"), "Case Album"),
   "an untargeted run is the whole library again — it fixed the other artist")
ok(stats.get("report_path"),
   f"…and a whole-library run stores its report ({stats.get('report_path')!r})")

print("== apply: a destination that already exists is reported, never overwritten ==")
# The one way a fix could lose music: the album folder the file's tags name
# already holds a file called that. Both files have to survive, and the run has
# to SAY so instead of picking one.
album("Artists/Twin/Album One", None, file_name="1-01 Song.flac")
TWIN_IN_ALBUM = os.path.join(MF, "Artists", "Twin", "Album One", "1-01 Song.flac")
with open(TWIN_IN_ALBUM, "wb") as f:
    f.write(b"\0" * 4096)
TWIN_LOOSE = album("Artists/Twin", tags("Twin", "Album One"))
ok(os.path.getsize(TWIN_LOOSE) != os.path.getsize(TWIN_IN_ALBUM),
   "the two same-named files differ, so an overwrite would be data loss")

report = layoutmod.apply_fixes({"music_folder": MF, "naming_script": SCRIPT,
                                "layout_apply": True})
twin = [f for f in report["fixes"] if f["path"] == "Artists/Twin/1-01 Song.flac"]
ok(len(twin) == 1 and twin[0]["result"] == "failed",
   f"the blocked move is a FAILED outcome, not a silent skip ({twin})")
ok("already exists" in twin[0]["action"],
   f"…and the words say what stopped it ({twin[0]['action']})")
ok(report["fix_failed"] == 1 and report["fixed"] == 0,
   f"the run counts it as the one failure ({report['fixed']} fixed, "
   f"{report['fix_failed']} failed)")
ok(os.path.getsize(TWIN_LOOSE) and os.path.getsize(TWIN_IN_ALBUM) == 4096,
   "both files are still there, untouched")
ok("Artists/Twin/1-01 Song.flac" in [i["path"] for i in report["issues"]],
   "…and the row is still reported for the user to settle")

print(f"\nAll {passed} checks passed.")