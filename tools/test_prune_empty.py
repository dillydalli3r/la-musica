#!/usr/bin/env python3
"""Verification for mlo.paths.prune_empty_dirs and the sweep run_chain makes
after its scripts: an organize or a removal takes the last file out of a disc
folder, and the empty shell it leaves behind must not survive the run.

Run:  python tools/test_prune_empty.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo.paths import prune_empty_dirs
from server import script_runners

passed = 0


def ok(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok: {label}")


def put(*parts, body=b"x"):
    p = os.path.join(*parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(body)
    return p


tmp = tempfile.mkdtemp(prefix="mlo_prune_empty_")

# ----------------------------------------------------------------------
# prune_empty_dirs: the empty branches go, the file-bearing one stays
# ----------------------------------------------------------------------
print("== prune_empty_dirs ==")
lib = os.path.join(tmp, "lib")
track = put(lib, "Artists", "Artist", "Album (2020)", "Disc 1", "01 - Song.flac")
os.makedirs(os.path.join(lib, "Artists", "Artist", "Album (2020)", "Disc 2"))
# a chain of nothing at all: both folders are empty and both must go
os.makedirs(os.path.join(lib, "Artists", "Empty Artist", "Empty Album"))
# app state and dot-dirs are never touched, subtree included
dot_sub = os.path.join(lib, ".dependencies", "bin")
skip_sub = os.path.join(lib, "data", "cache")
os.makedirs(dot_sub)
os.makedirs(skip_sub)

removed = prune_empty_dirs(lib)
ok(sorted(removed) == sorted([os.path.join(lib, "Artists", "Artist", "Album (2020)", "Disc 2"),
                              os.path.join(lib, "Artists", "Empty Artist", "Empty Album"),
                              os.path.join(lib, "Artists", "Empty Artist")]),
   f"only the empty branches are reported as removed ({removed})")
ok(removed.index(os.path.join(lib, "Artists", "Empty Artist", "Empty Album"))
   < removed.index(os.path.join(lib, "Artists", "Empty Artist")),
   f"a chain is reported child before parent, so the log reads in removal "
   f"order ({removed})")
ok(os.path.isfile(track), "the file-bearing branch is kept")
ok(os.path.isdir(os.path.join(lib, "Artists", "Album (2020)")) is False
   or os.path.isdir(os.path.join(lib, "Artists", "Artist", "Album (2020)", "Disc 1")),
   "the disc folder holding a file survives")
ok(os.path.isdir(lib) and os.path.isdir(os.path.join(lib, "Artists")),
   "the root and its non-empty ancestors survive")
ok(os.path.isdir(dot_sub) and os.path.isdir(skip_sub),
   "a dot-dir and a SKIP_DIRS subtree are left standing")
ok(prune_empty_dirs(lib) == [],
   "a second pass finds nothing: the tree is as empty of empties as it gets")

# a root that is itself empty is still left standing (the caller's folder)
bare = os.path.join(tmp, "bare")
os.makedirs(os.path.join(bare, "Inner", "Deeper"))
_bare_removed = prune_empty_dirs(bare)
ok(_bare_removed == [os.path.join(bare, "Inner", "Deeper"),
                     os.path.join(bare, "Inner")], _bare_removed)
ok(os.path.isdir(bare), "the root itself is never removed")

ok(prune_empty_dirs(None) == [] and prune_empty_dirs("") == []
   and prune_empty_dirs(os.path.join(tmp, "does-not-exist")) == [],
   "a missing or None root is a no-op, never a crash")

# ----------------------------------------------------------------------
# run_chain sweeps the folders its scripts emptied
# ----------------------------------------------------------------------
print("== run_chain sweeps what it emptied ==")
music = os.path.join(tmp, "Music")
album = os.path.join(music, "Artists", "Artist", "Album (2020)")
disc = os.path.join(album, "Disc 2")
emptied = put(disc, "02 - Song.flac")
kept = put(album, "01 - Song.flac")
other = put(music, "Artists", "Artist", "Other Album", "01 - Song.flac")
# empty, but NOT part of the run: only the run's own targets are swept
lonely = os.path.join(music, "Artists", "Lonely")
os.makedirs(lonely)


def _empties_disc(cfg):
    """The stand-in for the organize/removal step: it takes the last file out
    of the disc folder, exactly as a real script does."""
    moved = 0
    for t in cfg.get("targets") or []:
        for base, _dirs, files in os.walk(t):
            for name in files:
                os.remove(os.path.join(base, name))
                moved += 1
    return {"modified_count": moved}


def _noop(cfg):
    return {"modified_count": 0}


real_runners = dict(script_runners.RUNNERS)
real_log = script_runners.log
lines = []
_seen = []
try:
    script_runners.RUNNERS.update({3: ("Optimize FLACs", _empties_disc)})
    script_runners.log = lambda msg, *a, **k: lines.append(str(msg))
    cfg = {"music_folder": music}
    results = script_runners.run_chain(
        cfg, [3], targets=[disc], force=None,
        progress=lambda done, total, label, res: _seen.append((done, total, label, res)))
    ok(results == [{"id": 3, "name": "_empties_disc", "label": "Optimize FLACs",
                    "stats": {"modified_count": 1}}],
       f"the per-script result shape is unchanged ({results})")
    ok(_seen == [(1, 1, "Optimize FLACs", results[0])],
       f"and it is the same dict the progress callback saw ({_seen})")
    ok(not os.path.isdir(disc), "the disc folder the run emptied is gone")
    ok(os.path.isfile(kept) and os.path.isdir(album),
       "the album that still holds a file is kept")
    ok(os.path.isfile(other), "an album outside the targets is untouched")
    ok(os.path.isdir(lonely), "an empty folder outside the targets is not swept")
    removed_lines = [l for l in lines if l.startswith("Removed ")]
    ok(removed_lines == [f"Removed 1 empty folder(s) left by the run: {disc}"],
       f"the sweep logs its one line ({lines})")

    # no targets at all: the chain is a plain run and the tree is left alone
    lines.clear()
    _seen.clear()
    script_runners.RUNNERS.update({3: ("Optimize FLACs", _noop)})
    script_runners.run_chain({"music_folder": music}, [3])
    ok(not [l for l in lines if l.startswith("Removed ")],
       f"a run with no targets prunes nothing ({lines})")
    ok(os.path.isdir(lonely) and os.path.isdir(album),
       "and leaves every folder where it was")
finally:
    script_runners.RUNNERS.clear()
    script_runners.RUNNERS.update(real_runners)
    script_runners.log = real_log

print(f"\nAll {passed} checks passed.")
shutil.rmtree(tmp, ignore_errors=True)
