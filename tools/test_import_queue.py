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

# A running transfer is reported by slskd as a peer plus a remote folder path,
# and the local folder is `<peer>/<batch id>/<folder path>` —
# soulseek._pending_album_folders() reads that tree (see _still_downloading for
# the shapes it matches).
soulseek._pending_album_folders = lambda cfg=None: {("peer", ("still downloading",))}
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

print("== the run's completion frame names the ALBUM, not the folder ==")
# The owner's rule: a notice names the album's identity (artist — album (year)),
# never a raw folder name or a path with UUIDs. The run's own frame is emitted
# from _run with the album already in the library, so the album's OWN TAGS are
# what it reads — and the stub importer above reports a path that does not
# exist, which is why this case plants a real, tagged album.
import wave as _wave  # noqa: E402
from mlo.audio import AudioFile  # noqa: E402
from server import events as events_mod  # noqa: E402

identity_music = tempfile.mkdtemp(prefix="mlo-import-identity-")
identity_cfg = {"music_folder": identity_music, "soulseek_download_dir": ddir}
identity_album = os.path.join(identity_music, "5be1a1e2-0f0f-4a1b-9c3d-deadbeef0000")
os.makedirs(identity_album, exist_ok=True)
_wav = os.path.join(identity_album, "01 - Track.wav")
with _wave.open(_wav, "wb") as _w:
    _w.setnchannels(1)
    _w.setsampwidth(2)
    _w.setframerate(8000)
    _w.writeframes(b"\0\0" * 400)
_af = AudioFile(_wav)
_af.set_tag("ALBUMARTIST", "Identity Artist")
_af.set_tag("ALBUM", "Identity Album")
_af.set_tag("DATE", "1996-06-11")

import_queue.set_importer(lambda path: {"path": path, "album_root": identity_album,
                                        "errors": [], "chained": True,
                                        "chain_off": False,
                                        "scripts": [{"id": 1, "label": "x", "error": ""}]})
said = []
_real_emit = events_mod.emit
events_mod.emit = lambda kind, title, body, data=None, **kw: said.append(
    (kind, title, body, data))
try:
    started = import_queue.start(paths=[identity_album])
    check("the single-album run starts", started.get("ok") is True, str(started))
    # The run's frame is emitted AFTER its state flips to done, so waiting on
    # the STATE races the notice: join the runner's own thread instead.
    thread = import_queue._thread
    if thread is not None:
        thread.join(20)
finally:
    events_mod.emit = _real_emit
    import_queue.set_importer(None)
done = [e for e in said if e[0] == "download_done"]
check("the run ends with ONE completion frame", len(done) == 1, str([e[0] for e in said]))
if done:
    check("...naming the album's identity, not the folder",
          done[0][1] == "Imported Identity Artist — Identity Album (1996)",
          done[0][1])
    check("...and carrying it as the album, so a client needs no second read",
          done[0][3].get("album") == "Identity Artist — Identity Album (1996)",
          str(done[0][3].get("album")))
    check("...while the LINK stays the path the router opens",
          done[0][3].get("link", "").startswith("/album/"), str(done[0][3].get("link")))
shutil.rmtree(identity_music, ignore_errors=True)

# …and a run of SEVERAL albums is a tally, not one album's name (the run has no
# single subject — the frames above are the per-album ones).
import_queue.set_importer(lambda path: {"path": path, "album_root": path,
                                        "errors": [], "chained": True,
                                        "chain_off": False,
                                        "scripts": [{"id": 1, "label": "x", "error": ""}]})
said = []
events_mod.emit = lambda kind, title, body, data=None, **kw: said.append(
    (kind, title, body, data))
try:
    import_queue.start(paths=[one, two])
    thread = import_queue._thread
    if thread is not None:
        thread.join(20)
finally:
    events_mod.emit = _real_emit
done = [e for e in said if e[0] == "download_done"]
check("a multi-album run ends with one tally frame", len(done) == 1, str([e[0] for e in said]))
if done:
    check("...counting the albums it imported",
          done[0][1] == "Imported 2 albums", done[0][1])

shutil.rmtree(music, ignore_errors=True)
print(f"\n{len(FAILED)} failure(s)")
sys.exit(1 if FAILED else 0)
