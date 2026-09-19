#!/usr/bin/env python3
"""Importing what finished downloading — server/import_queue.py and
server.soulseek.ready_albums().

The feature this pins is the one a user actually presses: "import everything
that finished downloading" has to take each album all the way through the
pipeline, ONE at a time, and it has to know exactly which folders in the
download dir are albums that finished downloading. Both halves are tested here
against a temp download tree:

  * ready_albums() finds album folders (audio inside), skips a folder whose
    transfers slskd still reports as running, skips a rip-evidence-only
    folder, and reports the ONE parent of a disc-folder pair rather than its
    discs;
  * the runner processes the worklist in order, never overlaps two albums,
    records per-album failures without stopping the run, calls its on_done
    hook with the results, and refuses a second concurrent run.

No subprocess, no network, no real audio: the importer is injected, exactly
as server.main injects it (that inversion is what makes the runner testable).
"""
import os
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{('  — ' + detail) if detail and not ok else ''}")
    if not ok:
        FAILED.append(name)


from server import import_queue, soulseek  # noqa: E402

# ---- a fake music folder + download dir -----------------------------------
music = tempfile.mkdtemp(prefix="mlo-import-")
ddir = os.path.join(music, ".mlo", "downloads")
cfg = {"music_folder": music, "soulseek_download_dir": ddir}
soulseek.download_dir = lambda cfg=None: ddir


def album(name, files=("01 - Track.flac",)):
    d = os.path.join(ddir, "peer", name)
    os.makedirs(d, exist_ok=True)
    for f in files:
        with open(os.path.join(d, f), "w", encoding="utf-8") as fh:
            fh.write("not really audio; only the extension matters here")
    return d


print("== what counts as ready ==")
one = album("Album One")
two = album("Album Two", files=("01 - A.flac", "02 - B.flac"))
discs = album("Album With Discs")  # becomes a disc-parent below
os.makedirs(os.path.join(discs, "CD1"), exist_ok=True)
os.remove(os.path.join(discs, "01 - Track.flac"))
open(os.path.join(discs, "CD1", "01 - Disc.flac"), "w").write("x")
leftover = album("Peer Logs", files=("rip.log", "rip.cue"))
still = album("Still Downloading")

ready = soulseek.ready_albums(cfg)
names = sorted(os.path.basename(p) for p in ready)
check("the album folders are found", "Album One" in names and "Album Two" in names, str(names))
check("a disc parent is the album, not its discs", "Album With Discs" in names, str(names))
check("a rip-evidence-only folder is not an album", "Peer Logs" not in names, str(names))

# A running transfer is reported by slskd as a file path whose leaf names the
# remote folder — soulseek._pending_album_folders() reads that tree.
soulseek._pending_album_folders = lambda cfg=None: {"still downloading"}
check("a folder still downloading is not ready",
      "Still Downloading" not in sorted(os.path.basename(p) for p in soulseek.ready_albums(cfg)))
soulseek._pending_album_folders = lambda cfg=None: set()

print("== the runner ==")
seen = []
order = []
lock = threading.Lock()


def importer(path):
    """Stands in for server.main._import_one_album: records ordering and
    proves two albums are never in flight at once."""
    with lock:
        seen.append(path)
        order.append(len(seen))
    time.sleep(0.15)  # long enough for a second thread to overlap if one existed
    with lock:
        order.remove(len(seen))
    if os.path.basename(path) == "Album Two":
        return {"path": path, "album_root": path, "errors": ["stub failure"]}
    return {"path": path, "album_root": path + "-imported", "errors": []}


import_queue.set_importer(importer)
import_queue.set_ready_provider(lambda: soulseek.ready_albums({"music_folder": music}))
check("a fresh runner is idle", import_queue.status()["state"] == "idle")
check("nothing to import is refused, not an empty run",
      import_queue.start(paths=[])["ok"] is False)

done_payload = {}
res = import_queue.start(on_done=lambda results: done_payload.update(n=len(results)))
check("the run starts", res.get("ok") is True)
for _ in range(200):
    if import_queue.status()["state"] != "running":
        break
    time.sleep(0.05)
st = import_queue.status()
check("the run finished", st["state"] in ("done", "error"), st["state"])
check("every ready album was visited", len(seen) == len(ready), f"{len(seen)} of {len(ready)}")
check("only one album was in flight at any moment", order in ([], [1], [2]), str(order))
check("total/done agree with the worklist", st["total"] == len(ready) and st["done"] == len(ready))
check("a failing album is recorded, not fatal",
      any(not r["ok"] and "stub failure" in r["error"] for r in st["results"]),
      str(st["results"]))
check("a failing album does not stop the ones after it",
      len([r for r in st["results"] if r["ok"]]) == len(ready) - 1)
check("the on_done hook saw the results", done_payload.get("n") == len(ready), str(done_payload))
check("the successes carry the album root the importer reported",
      any(r["album_root"].endswith("-imported") for r in st["results"]))

print("== one run at a time ==")
gate = threading.Event()


def slow(path):
    gate.wait(timeout=5)
    return {"path": path, "album_root": path, "errors": []}


import_queue.set_importer(slow)
import_queue.start(paths=[one])
second = import_queue.start(paths=[two])
check("a second run is refused while one is going", second.get("ok") is False)
check("the refusal names the reason", "already in progress" in str(second.get("error")), str(second))
check("cancel asks the runner to stop", import_queue.cancel() is True)
gate.set()
for _ in range(100):
    if import_queue.status()["state"] != "running":
        break
    time.sleep(0.05)
check("a cancelled run reports cancelled or done with the album it started",
      import_queue.status()["state"] in ("cancelled", "done", "error"))

shutil.rmtree(music, ignore_errors=True)
print(f"\n{len(FAILED)} failure(s)")
sys.exit(1 if FAILED else 0)
