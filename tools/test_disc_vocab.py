#!/usr/bin/env python3
"""The browser disc vocabulary agrees with `mlo/discs.py`, name for name.

The wizard's album detection used to carry its own, narrower disc regex
(`/^(cd|disc|disk)\\s*\\d+$/i` in `web/src/pages/ImportWizard.tsx`), so a
multi-disc release whose folders were spelled "CD_1", "Disc 2 (FLAC)",
"Volume 1", "BD1" or the bare "1"/"2" split into an album per disc — the very
folders the LIBRARY folds into one album. `web/src/lib/discs.ts` is now the
mirror of `mlo/discs.py`'s one vocabulary.

This pins the two engines together: `tools/disc_vocab_probe.mjs` imports the
TypeScript module (Node 24 strips the types natively, no build), and every
answer is compared with the Python original. A drift in either direction — a
name one engine reads as a disc the other does not — fails here.

Covered, name for name:
  * `disc_dir_number` — the strict/loose directory reading, including the
    decoration strip ("CD1 [FLAC]", "Disc 2 (FLAC)") and the D-TT filename
    convention ("1-03 rip");
  * `bare_disc_index` — the whole-name index ("1", "02"), and its rejection of
    "123", "CD1", "1-03 rip";
  * `is_disc_parent` — a named disc child, and the numbered-set rule for bare
    indices ("1"+"2" yes, "25"+"30" no);
  * `disc_number_of_path` — the file's own name then its folder, the bare-index
    parent included (a real temp tree, so Python's sibling listing runs).

Run:  python tools/test_disc_vocab.py   (exit 0 = pass, 1 = failure,
      2 = cannot run here — no node / no type-stripping).

The probe reads a case list from a JSON file and prints one JSON object; see
the module docstring in `tools/disc_vocab_probe.mjs` for its shape."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo import discs as D  # noqa: E402

PROBE = os.path.join(ROOT, "tools", "disc_vocab_probe.mjs")

# Directory names — the STRICT/loose reading (`disc_dir_number`). The accept
# list is the whole vocabulary the owner's rips use; the reject list is the
# album-folder spelling that must NOT read as a disc.
DIRS = [
    "CD1", "cd2", "CD 3", "CD-4", "CD_5", "Disc 6", "Disk7", "Volume 2", "vol.3",
    "DVD 2", "BD1", "CD1 [FLAC]", "Disc 2 (FLAC)", "1-03 rip",
    # bare indices: answered by bare_disc_index, so disc_dir_number says None
    "01", "1", "2", "3", "25", "30",
    "Album [FLAC]", "Extras", "Log+Cue", "Scans",
    "CD", "Disc", "disc 12", "cd100", "Disk 1 bonus",
]

# Bare indices — the WHOLE name is the index (`bare_disc_index`).
BARES = ["1", "01", "2", "3", "25", "30", "123", "CD1", "1-03 rip", ""]

# Child-name sets — `is_disc_parent`.
PARENTS = [
    ["CD1", "CD2"],
    ["1", "2"],
    ["1", "2", "3"],
    ["1"],
    ["2"],
    ["25", "30"],
    ["CD1", "Bonus"],
    ["Album [FLAC]", "Extras"],
    ["CD1", "CD2", "Artwork"],
]

# File paths — `disc_number_of_path`, over a real temp tree so Python's
# bare-index sibling listing runs (the "Album/1/..." case below).
PATHS = [
    "Album/CD2/01 - x.flac",
    "Album/CD1 [FLAC]/track.flac",
    "Album/Disc 2 (FLAC)/03 x.flac",
    "Album/1-03 Track.flac",
    "Album/(2) 01 - x.flac",
    "Album/01 - Title.flac",       # says nothing -> None
    "Album/1/01 - x.flac",         # bare-index parent in a numbered set
]

FAILS = []


def check(ok, label, extra=""):
    print(f"{'ok  ' if ok else 'FAIL'} {label}{(' — ' + str(extra)) if extra and not ok else ''}")
    if not ok:
        FAILS.append(label)


if not shutil.which("node"):
    print("SKIP: node not installed")
    raise SystemExit(2)

# The case list, the temp filesystem tree the path cases read, and the probe
# run itself all live under one temp dir cleaned up on the way out.
tree = tempfile.mkdtemp(prefix="mlo-disc-vocab-")
cases_path = os.path.join(tree, "cases.json")
try:
    with open(cases_path, "w", encoding="utf-8") as fh:
        json.dump({"dirs": DIRS, "bares": BARES, "parents": PARENTS,
                   "paths": PATHS}, fh)
    for rel in PATHS:
        os.makedirs(os.path.join(tree, os.path.dirname(rel)), exist_ok=True)
    # "Album/1" claims disc 1 only because its sibling "2" makes the pair a
    # numbered set — the rule Python reads off the listing.
    os.makedirs(os.path.join(tree, "Album", "2"), exist_ok=True)

    run = subprocess.run(["node", PROBE, cases_path], cwd=ROOT,
                         capture_output=True, text=True, timeout=120)
    if run.returncode != 0:
        err = (run.stderr or "").strip()
        print("SKIP: the probe could not run")
        print(err or run.stdout.strip())
        # A node that cannot strip TypeScript is "cannot run here", not a
        # parity failure.
        if any(s in err for s in ("ERR_UNKNOWN_FILE_EXTENSION", "type stripping",
                                  "Unsupported", "SyntaxError")):
            raise SystemExit(2)
        raise SystemExit(1)

    got = json.loads(run.stdout)
    # Python's own answers for the path cases are read while the tree is still
    # on disk (the bare-index branch lists a folder's siblings).
    path_want = {rel: D.disc_number_of_path(os.path.join(tree, rel)) for rel in PATHS}
finally:
    shutil.rmtree(tree, ignore_errors=True)


def py_parent_key(kids):
    """The key the probe builds with JSON.stringify(childNames)."""
    return json.dumps(kids, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# dirs -> disc_dir_number
# --------------------------------------------------------------------------- #
for name in DIRS:
    want = D.disc_dir_number(name)
    have = got.get("dirs", {}).get(name, "<missing>")
    check(have == want, f"disc_dir_number({name!r})", f"python={want!r} js={have!r}")

# --------------------------------------------------------------------------- #
# bares -> bare_disc_index
# --------------------------------------------------------------------------- #
for name in BARES:
    want = D.bare_disc_index(name)
    have = got.get("bares", {}).get(name, "<missing>")
    check(have == want, f"bare_disc_index({name!r})", f"python={want!r} js={have!r}")

# --------------------------------------------------------------------------- #
# parents -> is_disc_parent
# --------------------------------------------------------------------------- #
for kids in PARENTS:
    want = D.is_disc_parent(kids)
    have = got.get("parents", {}).get(py_parent_key(kids), "<missing>")
    check(have == want, f"is_disc_parent({kids!r})", f"python={want!r} js={have!r}")

# --------------------------------------------------------------------------- #
# paths -> disc_number_of_path
# --------------------------------------------------------------------------- #
for rel in PATHS:
    want = path_want[rel]
    have = got.get("paths", {}).get(rel, "<missing>")
    check(have == want, f"disc_number_of_path({rel!r})", f"python={want!r} js={have!r}")

total = len(DIRS) + len(BARES) + len(PARENTS) + len(PATHS)
if FAILS:
    print(f"\nFAIL {len(FAILS)} of {total} case(s): " + ", ".join(FAILS))
    raise SystemExit(1)
print(f"\nPASS — {total} cases agree with mlo/discs.py "
      f"({len(DIRS)} dirs, {len(BARES)} bares, {len(PARENTS)} parents, {len(PATHS)} paths)")
raise SystemExit(0)