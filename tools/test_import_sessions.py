#!/usr/bin/env python3
"""Verify the unfinished-import session store, its mid-import marker, and its
routes.

A manual import left mid-wizard keeps nothing in the wizard's own React state,
so `server.import_sessions` bookmarks it in `<music>/.mlo/data/
import_sessions.json`; the tray turns each bookmark into "Continue import".
This pins the store's own contract — one entry per album, a dead folder pruned
on read, newest first, dismiss-one and dismiss-all — and that the three routes
the wizard and the bell call are really mounted.

It ALSO pins the disk half that ties the bookmark to grading: every session
stamps the album FOLDER with `.mlo_importing.json` (`mlo.paths`), which survives
a restart and travels with the chain's own organize/beets rename, so a renamed
album's bookmark is relocated instead of pruned. Through
`server.imports.importing_album` — the ONE predicate — a mid-import album is
held out of the grade strip, the Needs-attention shelf and the library payload
(no checks, no issues), and `server.interrupt_recovery` keeps a session-backed
marker while clearing an abandoned one.

Nothing here touches the network or the user's library: the app's paths are
redirected to a temp folder BEFORE the server modules are imported.

Run: python tools/test_import_sessions.py  (exit 0 pass, 1 fail, 2 cannot run)
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

REDIRECT = tempfile.mkdtemp(prefix="mlo-sessions-redirect-")
MF = tempfile.mkdtemp(prefix="mlo-sessions-test-")
os.environ["MLO_MUSIC_FOLDER"] = MF

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB = os.path.join(REDIRECT, "config.json")
with open(_STUB, "w", encoding="utf-8") as f:
    json.dump({"music_folder": MF}, f)
cfgmod.CONFIG_FILE = _STUB
pathmod.CONFIG_FILE = _STUB
if getattr(cfgmod, "LEGACY_DATA_DIR", None) is not None:
    cfgmod.LEGACY_DATA_DIR = os.path.join(REDIRECT, "legacy")
if getattr(pathmod, "LEGACY_DATA_DIR", None) is not None:
    pathmod.LEGACY_DATA_DIR = os.path.join(REDIRECT, "legacy")

atexit.register(lambda: [shutil.rmtree(d, ignore_errors=True)
                         for d in (REDIRECT, MF)])
atexit.register(lambda: os.environ.pop("MLO_MUSIC_FOLDER", None))

failures = []
count = 0


def ok(cond, label):
    global count
    count += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        failures.append(label)


from server import import_sessions as svc  # noqa: E402
from server import api_imports  # noqa: E402
from server import imports as imports_mod  # noqa: E402
from server import recommendations as recommend  # noqa: E402
from server import interrupt_recovery  # noqa: E402

CFG = {"music_folder": MF}


def _make_wav(path):
    """A tiny valid WAV — enough for the grader to treat the folder as an album
    (no ffmpeg, no tags, so it grades as a failing album worth neutralising)."""
    import struct
    import wave
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<h", 0) * 800)


def main():
    # Two album folders on disk (a live session needs one), and a third we
    # create only to prove a dead folder is pruned.
    a = tempfile.mkdtemp(prefix="Album A ", dir=MF)
    b = tempfile.mkdtemp(prefix="Album B ", dir=MF)

    print("== upsert / list ==")
    row = svc.upsert(a, 3, "Album A", False, CFG)
    ok(row and row["step"] == 3 and row["album_name"] == "Album A",
       "upsert returns the stored row")
    ok(svc.sessions(CFG) and len(svc.sessions(CFG)) == 1,
       "the list carries exactly the one session")
    ok(svc.get(a, CFG) is not None and svc.get(b, CFG) is None,
       "get finds the album it knows and no other")

    print("== one album, one entry; step is refreshed ==")
    again = svc.upsert(a, 5, "Album A", False, CFG)
    ok(again["step"] == 5 and len(svc.sessions(CFG)) == 1,
       "a second upsert refreshes the same entry rather than adding one")

    print("== newest first ==")
    time.sleep(0.02)
    svc.upsert(b, 1, "Album B", True, CFG)
    listed = svc.sessions(CFG)
    ok([r["album_name"] for r in listed] == ["Album B", "Album A"],
       "the newest session is first")
    ok(listed[0]["staged"] is True, "the staged flag round-trips")

    print("== a dead folder is pruned on read ==")
    c = tempfile.mkdtemp(prefix="Album C ", dir=MF)
    svc.upsert(c, 2, "Album C", False, CFG)
    shutil.rmtree(c)
    names = [r["album_name"] for r in svc.sessions(CFG)]
    ok("Album C" not in names and len(names) == 2,
       "a session whose folder is gone is dropped")

    print("== dismiss ==")
    svc.dismiss(a, CFG)
    ok(svc.get(a, CFG) is None and len(svc.sessions(CFG)) == 1,
       "dismiss drops one album")
    svc.dismiss(None, CFG)
    ok(svc.sessions(CFG) == [], "dismiss with no album clears them all")

    print("== the marker is the durable half of 'is this album mid-import' ==")
    # A session STAMPS the album folder (`.mlo_importing.json`), so "being
    # imported" survives a restart AND travels with an organize/beets rename.
    # This is what ties the bookmark world (import_sessions.json, keyed by
    # path) to the grading world, which must not report a half-written album.
    from mlo.paths import IMPORTING_FILE, is_importing, load_importing, save_importing

    marked = tempfile.mkdtemp(prefix="Album M ", dir=MF)
    svc.upsert(marked, 2, "Album M", False, CFG)
    ok(os.path.isfile(os.path.join(marked, IMPORTING_FILE)),
       "upsert stamps the album folder with the mid-import marker")
    ok(imports_mod.importing_album(marked),
       "imports.importing_album answers yes from the disk marker alone")
    svc.dismiss(marked, CFG)
    ok(not is_importing(marked) and not imports_mod.importing_album(marked),
       "dismiss clears the marker, so the album is gradable again")

    print("== a rename relocates the bookmark instead of losing it ==")
    # The chain's organize/beets step renames the album folder. The path-keyed
    # row would then point at a folder that is gone and be pruned — no Continue
    # row, and a half-imported album the grader reports as failing. The marker
    # rode along inside the folder, so the row is found again at its NEW path.
    root = os.path.join(MF, "Artists")
    alb = os.path.join(root, "Rel Artist", "Rel Album")
    os.makedirs(alb)
    svc.upsert(alb, 4, "Rel Album", False, CFG)
    renamed = os.path.join(root, "Rel Artist", "Rel Album Renamed")
    os.rename(alb, renamed)
    rows = svc.sessions(CFG)
    ok(any(os.path.normcase(r["album"]) == os.path.normcase(renamed) for r in rows),
       "a renamed album's session is relocated through its marker")
    ok(imports_mod.importing_album(renamed),
       "the marker travelled with the rename")
    svc.dismiss(renamed, CFG)
    ok(not is_importing(renamed), "dismissing the relocated session clears the marker")

    print("== grading leaves a mid-import album out ==")
    # The whole point: a marked album is not a finding anywhere. Built from a
    # synthetic library row (a marked folder, a failing grade) so the routing
    # through imports.importing_album is what is under test, not the grader.
    mid = tempfile.mkdtemp(prefix="Album Mid ", dir=MF)
    svc.upsert(mid, 3, "Album Mid", False, CFG)
    mid_fwd = mid.replace("\\", "/")
    lib = {"artists": [{"name": "A", "albums": [{
        "path": mid_fwd, "meta": {"ALBUM": "Mid"},
        "total_checks": 10, "pass_count": 4, "pass": False, "grade_pct": 40.0,
        "tracks": [{"path": mid_fwd + "/1.flac", "file": "1.flac",
                    "issues": ["TAGS"], "tags": {}, "grade_pass": False}],
    }]}]}
    gw = recommend.grade_warning(lib)
    ok(gw["albums_failing"] == 0 and gw["albums_importing"] == 1,
       "the grade strip holds a mid-import album out and counts it")
    ok(recommend._needs_attention([a for ar in lib["artists"] for a in ar["albums"]], 5) == [],
       "the Needs-attention shelf drops it too")
    svc.dismiss(mid, CFG)

    print("== the library payload is not a grade for a marked album ==")
    from server import library as lib_mod

    graded = tempfile.mkdtemp(prefix="Album G ", dir=MF)
    _make_wav(os.path.join(graded, "01 - One.wav"))
    before = lib_mod.build_album(graded, CFG, light=True)
    ok(before and (before.get("total_checks") or 0) > 0 and not before.get("importing"),
       "a plain album is graded normally")
    save_importing(graded, {"album": graded.replace("\\", "/"), "importing": True})
    after = lib_mod.build_album(graded, CFG, light=True)
    ok(after and after.get("importing") and (after.get("total_checks") or 0) == 0,
       "the same album reads as mid-import: no checks, no issues")
    ok(after.get("pass") is True and after.get("grade_pct") is None,
       "and it is not a FAIL (the no-checks rule), with no percentage")
    from mlo.paths import clear_importing
    clear_importing(graded)
    again = lib_mod.build_album(graded, CFG, light=True)
    ok(again and not again.get("importing"),
       "grading returns once the marker is gone")
    ok((again.get("total_checks") or 0) > 0 and not again.get("importing"),
       "the album is an ordinary graded album again")

    print("== startup recovery reconciles a killed import's marker ==")
    # A graceful kill records a journal; a SIGKILL leaves only the FILESYSTEM.
    # The session-backed marker is what lets the next start know the import was
    # interrupted and can still be continued; a marker with NO session is an
    # abandoned autonomous import and is cleared, so the album is not hidden
    # from grading forever.
    abandoned = os.path.join(root, "Abandoned Album")
    os.makedirs(abandoned)
    save_importing(abandoned, {"album": abandoned.replace("\\", "/"), "importing": True})
    report = interrupt_recovery._importing_report(CFG, log=lambda *_: None)
    ok(any(r["kind"] == "importing_cleared" for r in report),
       "an abandoned marker with no session is cleared at startup")
    ok(not is_importing(abandoned), "...and its marker is gone")

    kept = os.path.join(root, "Kept Album")
    os.makedirs(kept)
    svc.upsert(kept, 5, "Kept Album", False, CFG)
    report = interrupt_recovery._importing_report(CFG, log=lambda *_: None)
    ok(any(r["kind"] == "importing"
           and os.path.normcase(r["folder"]) == os.path.normcase(kept) for r in report),
       "a session-backed marker is kept as a resumable import")
    ok(is_importing(kept) and load_importing(kept).get("step") == 5,
       "...and left in place, still carrying the step to resume at")
    svc.dismiss(kept, CFG)

    print("== a library-wide run is not an import of every album ==")
    # A Run All / library-wide Grade holds the library ROOT (kind "scripts"). If
    # that counted as "importing", every album would read as mid-import while any
    # sweep ran. Only a run SCOPED to this album counts. Asked from a FOREIGN
    # thread: a claim held by the asking job is deliberately not a conflict to
    # `job_locks.holder`, which is the real thing being modelled.
    from server import job_locks as jl

    def _with_claim(paths, kind, fn):
        import threading
        ready, release = threading.Event(), threading.Event()

        def _hold():
            with jl.holding(list(paths), kind=kind, label=kind):
                ready.set()
                release.wait(10)
        t = threading.Thread(target=_hold, daemon=True)
        t.start()
        assert ready.wait(10), "the claim was never taken"
        try:
            return fn()
        finally:
            release.set()
            t.join(10)

    wide = os.path.join(root, "Wide Artist", "Wide Album")
    os.makedirs(wide)
    ok(not _with_claim([root], "scripts", lambda: imports_mod.importing_album(wide)),
       "a Run All holding the library root does not mark an album importing")
    ok(_with_claim([wide], "scripts", lambda: imports_mod.importing_album(wide)),
       "a run scoped to the album does mark it importing")
    ok(_with_claim([os.path.dirname(wide)], "import",
                   lambda: imports_mod.importing_album(wide)),
       "an import claim above the album still counts (the registry's rule)")

    print("== the routes are mounted ==")
    paths = {getattr(r, "path", "") for r in api_imports.router.routes}
    ok("/api/import/sessions" in paths, "GET/POST /api/import/sessions is mounted")
    ok("/api/import/sessions/dismiss" in paths,
       "POST /api/import/sessions/dismiss is mounted")

    print(f"sessions: {count - len(failures)}/{count} assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    # Anything that cannot be set up on this machine exits 2, the repo's
    # "cannot run here" code, rather than looking like a real failure.
    try:
        sys.exit(main())
    except Exception as e:  # pragma: no cover
        import traceback
        traceback.print_exc()
        print(f"cannot run here: {e}")
        sys.exit(2)
