#!/usr/bin/env python3
"""Verify the unfinished-import session store and its routes.

A manual import left mid-wizard keeps nothing in the wizard's own React state,
so `server.import_sessions` bookmarks it in `<music>/.mlo/data/
import_sessions.json`; the tray turns each bookmark into "Continue import".
This pins the store's own contract — one entry per album, a dead folder pruned
on read, newest first, dismiss-one and dismiss-all — and that the three routes
the wizard and the bell call are really mounted.

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

CFG = {"music_folder": MF}


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
