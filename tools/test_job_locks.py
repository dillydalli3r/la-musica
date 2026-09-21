#!/usr/bin/env python3
"""The library-path lock registry — server/job_locks.py, plus the one wire that
matters: a script run holds the folders it is working on.

Script runs, imports and the organizer all mutate library files in place, so a
delete, a move, a tag write or a remux landing on the same folder at the same
time is how a library gets corrupted. job_locks is the registry that says who
is using what; these checks pin the rules it enforces:

  * a claim is visible while it is held and gone the moment the block ends;
  * a folder and anything inside it are the SAME claim, in both directions,
    while a sibling folder with a shared name prefix is not;
  * a job may re-enter its own claim (an import organizing the album it just
    imported), and a nested block never releases the outer one;
  * a second job is refused, with a message naming the running one, and a
    waiter (an import) gets in as soon as the holder finishes;
  * a raise inside the block releases everything — nothing is left locked —
    and a timed-out wait is a refusal, not a hang;
  * an async route (a cover write) keeps its hold across its await.

Over HTTP, through the real app and a temp library, each route that changes
library files is driven twice — once while a foreign job holds what it claims
(409) and once when it is free (it proceeds): tag writes, album remove,
organize (dry runs exempt), cover upload / download / clear, the lyrics
sidecar, beets, export, the album ingest and the downloads import.

No network, no ffmpeg, no real audio: the paths are plain files in a temp
library, and the one script runner is a stub.
"""
import base64
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


from server import job_locks as jl  # noqa: E402

music = tempfile.mkdtemp(prefix="mlo-locks-")
album = os.path.join(music, "Artists", "Artist", "Album")
track = os.path.join(album, "01 - Song.flac")
sibling = os.path.join(music, "Artists", "Artist", "Album 2")
os.makedirs(album, exist_ok=True)
os.makedirs(sibling, exist_ok=True)
with open(track, "w", encoding="utf-8") as fh:
    fh.write("not really audio; only the path matters here")


def other(path):
    """Who holds *path*, asked as a job that is NOT the one this thread is in.

    A job's own claim is never a conflict, so a query made from inside a held
    block has to name an asker to see the claim it is standing on.
    """
    return jl.holder(path, asker="someone-else")


def refused_for(path):
    """Whether a second job asking for *path* is refused — what every guarded
    route answers as 409."""
    try:
        with jl.holding([path], job="someone-else", label="Probe"):
            return False
    except jl.PathLocked:
        return True


print("== a claim is held, and released ==")

with jl.holding([album], kind="test", label="Job A") as job_a:
    check("the holder query answers while it is held",
          (other(album) or {}).get("job") == job_a, str(other(album)))
    check("a path inside a held folder is covered by it",
          (other(track) or {}).get("job") == job_a)
    check("a sibling folder that only shares a name prefix is NOT",
          other(sibling) is None)
    rows = jl.jobs()
    check("the job is listed once, with its kind, label and path",
          len(rows) == 1 and rows[0]["kind"] == "test" and rows[0]["label"] == "Job A"
          and rows[0]["paths"] == [album], str(rows))
    check("elapsed is measured on the server",
          rows[0]["elapsed"] >= 0 and rows[0]["started_at"] > 0, str(rows[0]))
    jl.set_progress(job_a, 2, 5, "Grade")
    check("progress is reported for a job that has some",
          jl.jobs()[0]["progress"] == {"done": 2, "total": 5, "text": "Grade"},
          str(jl.jobs()[0]["progress"]))

check("the claim is gone when the block ends", other(album) is None)
check("a finished job is no longer listed", jl.jobs() == [], str(jl.jobs()))

# One path is one claim however it is spelled: normcase(normpath(abspath(…))),
# the comparison the rest of the app makes for path identity.
with jl.holding([album.replace(os.sep, "/")], label="Job A"):
    check("a path is one claim however it is spelled",
          other(album) is not None)
    if os.name == "nt":
        check("case-folded on a case-insensitive filesystem",
              other(album.upper()) is not None)

print("== parent and child are the same claim ==")

with jl.holding([album], label="Job A"):
    try:
        with jl.holding([track], job="foreign", label="Job B"):
            check("a second job may not take a track inside the held folder", False)
    except jl.PathLocked:
        check("a second job may not take a track inside the held folder", True)
    with jl.holding([sibling], job="foreign", label="Job B"):
        check("a second job may still take a sibling folder", True)

with jl.holding([track], label="Job A"):
    try:
        with jl.holding([album], job="foreign", label="Job B"):
            check("an album-wide job may not take a folder holding a claimed track", False)
    except jl.PathLocked as e:
        check("an album-wide job may not take a folder holding a claimed track", True)
        check("the refusal names the path that was asked for",
              os.path.basename(album) in str(e), str(e))

print("== a job never blocks itself ==")

with jl.holding([album], kind="import", label="Import Album") as job:
    with jl.holding([album], job, label="Import Album"):
        check("a job may re-enter its own claim", other(album) is not None)
    check("the album is still held after the nested block", other(album) is not None)
    check("re-entering does not add a row", len(jl.jobs()) == 1, str(jl.jobs()))
    with jl.holding([track]) as inner:
        check("a nested call joins the job already running on this thread",
              inner == job, f"{inner} != {job}")
    check("the outer claim survived the nested call", other(album) is not None)
    check("the nested claim was released, the outer one kept",
          other(track) is not None and len(jl.jobs()) == 1)

check("the outer block released everything", other(album) is None and jl.jobs() == [])

print("== a second job is refused, a waiting one queues ==")

with jl.holding([album], kind="scripts", label="Optimize FLACs") as holder:
    try:
        with jl.holding([track], job="foreign", label="Job B"):
            check("a foreign job is refused", False)
    except jl.PathLocked as e:
        check("a foreign job is refused", True)
        check("the refusal names the running job", "Optimize FLACs" in str(e), str(e))
        check("the refusal carries the holder's id", e.job == holder, str(e.job))
    check("a refused job leaves nothing behind", len(jl.jobs()) == 1, str(jl.jobs()))
    # The routes do this from the request's own thread, with no job of their
    # own: a refused write must not leave a phantom row on the page.
    def foreign_tag_write():
        """What a tag-write route does: hold the track, or be refused."""
        try:
            with jl.holding([track], kind="tags", label="Tag write"):
                return "held"
        except jl.PathLocked:
            return "refused"

    refused = []
    worker = threading.Thread(
        target=lambda: refused.append(foreign_tag_write()))
    worker.start()
    worker.join(10)
    check("a write from another thread is refused while the folder is held",
          refused == ["refused"], str(refused))
    check("the refused write left no row behind", len(jl.jobs()) == 1, str(jl.jobs()))
    try:
        with jl.holding([album], job="foreign", wait=True, timeout=0.2):
            check("a wait that times out is refused, not queued forever", False)
    except jl.PathLocked:
        check("a wait that times out is refused, not queued forever", True)

waited = []


def waiting_job():
    with jl.holding([album], kind="import", label="Import Album", wait=True, timeout=10):
        waited.append(time.time())


with jl.holding([album], kind="scripts", label="Run all"):
    thread = threading.Thread(target=waiting_job)
    thread.start()
    time.sleep(0.4)
    check("a waiting job does not get the path while it is held", waited == [], str(waited))

thread.join(10)
check("the waiting job gets in as soon as the holder finishes", len(waited) == 1, str(waited))
check("nothing is left held afterwards", jl.jobs() == [], str(jl.jobs()))

print("== a raise releases everything ==")


def failing_job():
    with jl.holding([album], label="Job C"):
        raise RuntimeError("script failed")


try:
    failing_job()
except RuntimeError:
    pass
check("a raise inside the block releases the claim", other(album) is None)
check("the failed job is no longer listed", jl.jobs() == [], str(jl.jobs()))


@jl.holds(lambda path: [path], kind="tags", label="Tag write")
def tagged(path):
    return (other(path) or {}).get("kind")


@jl.holds(lambda path: [path], kind="tags", label="Tag write")
def tagged_then_raises(path):
    raise ValueError("write failed")


check("a decorated call holds the path it names", tagged(album) == "tags", str(tagged(album)))
check("the decorator released on return", other(album) is None)
try:
    tagged_then_raises(album)
except ValueError:
    pass
check("the decorator released on a raise", other(album) is None)

print("== an async route holds its path across its await ==")

import asyncio  # noqa: E402

_started = threading.Event()


@jl.holds(lambda path: [path], kind="tags", label="Async write")
async def async_write(path):
    """An async route: the hold must outlive the call, i.e. span the await."""
    _started.set()
    await asyncio.sleep(0.5)
    return "done"


_ran = []
_worker = threading.Thread(target=lambda: _ran.append(asyncio.run(async_write(album))))
_worker.start()
_started.wait(5)
check("the path is still held while the coroutine is awaiting",
      other(album) is not None, str(other(album)))
_worker.join(5)
check("the hold ends with the coroutine", other(album) is None and _ran == ["done"],
      f"{other(album)} {_ran}")

# Two async requests on ONE event loop are two jobs: the first suspends at its
# await, the second must be refused, not join it. (A thread-local job made them
# one job — the second wrote the album unchallenged, and the first's release
# dropped the second's claim mid-write.)
async def interleaved():
    first_entered = asyncio.Event()
    first_may_end = asyncio.Event()
    outcome = {}

    @jl.holds(lambda path: [path], kind="cover", label="Cover write")
    async def first(path):
        first_entered.set()
        await first_may_end.wait()
        return "first written"

    @jl.holds(lambda path: [path], kind="cover", label="Cover write")
    async def second(path):
        return "second written"

    one = asyncio.create_task(first(album))
    await first_entered.wait()
    two = asyncio.create_task(second(album))
    try:
        outcome["second"] = await two
    except jl.PathLocked as e:
        outcome["second"] = f"refused: {e.job}"
    first_may_end.set()
    outcome["first"] = await one
    return outcome


_interleaved = asyncio.run(interleaved())
check("a second async request on one loop is refused, not joined",
      str(_interleaved.get("second", "")).startswith("refused"),
      str(_interleaved))
check("the first async request still finishes", _interleaved.get("first") == "first written")
check("both async claims are gone", other(album) is None and jl.jobs() == [],
      f"{other(album)} {jl.jobs()}")

print("== a background worker keeps the claim it was handed ==")

try:
    from mlo import flac as flac_mod          # noqa: E402
    from server import imports as imports_mod  # noqa: E402
    from server import main as mlo_main        # noqa: E402  (heavy import)
except Exception as e:                                   # pragma: no cover
    print(f"  SKIP  server.main unavailable ({e})")
    flac_mod = imports_mod = mlo_main = None

if mlo_main is not None:
    _gate = threading.Event()
    _entered = threading.Event()
    _worked = []

    def slow_worker(path):
        _entered.set()
        _gate.wait(10)
        _worked.append(path)

    with jl.holding([album], kind="import", label="Import Album") as _job:
        _thread = jl.in_background([album], slow_worker, album)
        check("the block that started the worker still holds the path",
              other(album) is not None)
    check("the worker picked the work up", _entered.wait(5))
    check("the path is STILL claimed after the starting block returned",
          (other(album) or {}).get("job") == _job, str(other(album)))
    check("...under the same job the block held",
          (other(album) or {}).get("kind") == "import", str(other(album)))
    _gate.set()
    _thread.join(10)
    check("the claim ends when the worker does",
          other(album) is None and _worked == [album], f"{other(album)} {_worked}")
    check("and the job leaves the list", jl.jobs() == [], str(jl.jobs()))

    # The same window in the route that has it: _import_one_album returns
    # while its chain is still running, and the chain writes tags, covers and
    # lyrics for minutes afterwards.
    print("== an import returns with its chain still claimed ==")

    chain_album = os.path.join(music, "Artists", "Other", "Chain Album")
    os.makedirs(chain_album, exist_ok=True)
    with open(os.path.join(chain_album, "01 - Track.flac"), "w", encoding="utf-8") as fh:
        fh.write("not really audio")

    _chain_gate = threading.Event()
    _chain_entered = threading.Event()
    _chained = []

    def fake_finish_album(path, cfg, **kwargs):
        _chain_entered.set()
        _chain_gate.wait(10)
        _chained.append(path)

    _real = {
        "finish_album": imports_mod.finish_album,
        "media": mlo_main._tag_media_for_albums,
        "identity": mlo_main._stamp_import_identity,
        "organize": mlo_main.organize,
        "convert": flac_mod.convert_album_lossless,
    }
    imports_mod.finish_album = fake_finish_album
    mlo_main._tag_media_for_albums = lambda albums: 0
    mlo_main._stamp_import_identity = lambda albums: 0
    mlo_main.organize = lambda req: {"results": [{"album_root": req.paths[0]}]}
    # The steps before the chain are stubbed: this checks WHO holds the album
    # across the hand-off, not what the codec/tagger/organizer do with it.
    flac_mod.convert_album_lossless = lambda album, cfg: {"modified_count": 0}
    try:
        res = mlo_main._import_one_album(chain_album, {"music_folder": music},
                                         chain_async=True)
        check("the import reports its chain as started", res.get("chain_started") is True, str(res))
        check("the chain behind it is running", _chain_entered.wait(5))
        check("the album is claimed after the import call returned",
              other(chain_album) is not None, str(other(chain_album)))
        check("...by an import job, not a leftover",
              (other(chain_album) or {}).get("kind") == "import", str(other(chain_album)))
        check("a foreign job is refused the album the chain is on",
              refused_for(chain_album), str(other(chain_album)))
        _chain_gate.set()
        for _ in range(200):
            if other(chain_album) is None:
                break
            time.sleep(0.05)
        check("the claim ends with the chain",
              other(chain_album) is None and _chained == [chain_album],
              f"{other(chain_album)} {_chained}")
        check("no job rows left behind", jl.jobs() == [], str(jl.jobs()))
    finally:
        imports_mod.finish_album = _real["finish_album"]
        mlo_main._tag_media_for_albums = _real["media"]
        mlo_main._stamp_import_identity = _real["identity"]
        mlo_main.organize = _real["organize"]
        flac_mod.convert_album_lossless = _real["convert"]

print("== a script run holds what it works on ==")

from server import script_runners  # noqa: E402

run_album = os.path.join(music, "Artists", "Other", "Run Album")
os.makedirs(run_album, exist_ok=True)
with open(os.path.join(run_album, "01 - Track.flac"), "w", encoding="utf-8") as fh:
    fh.write("not really audio")

seen = {}


def stub_runner(cfg):
    """Stands in for a real script: what a foreign caller sees during the run."""
    seen["row"] = other(run_album) or {}
    try:
        with jl.holding([run_album], job="foreign", label="Job B"):
            seen["refused"] = False
    except jl.PathLocked:
        seen["refused"] = True
    return {"modified_count": 0}


real_runners = dict(script_runners.RUNNERS)
script_runners.RUNNERS[3] = ("Optimize FLACs", stub_runner)
try:
    results = script_runners.run_chain({"music_folder": music}, [3], targets=[run_album])
finally:
    script_runners.RUNNERS.clear()
    script_runners.RUNNERS.update(real_runners)

check("the script ran", [r.get("id") for r in results] == [3], str(results))
check("the run held its target while the script ran",
      seen.get("row", {}).get("kind") == "scripts", str(seen.get("row")))
check("the run's row is named after the script that is going",
      seen.get("row", {}).get("label") == "Optimize FLACs", str(seen.get("row")))
check("another job is refused while the run is going", seen.get("refused") is True)
check("the run released its target when it finished", other(run_album) is None)
check("and left no rows behind", jl.jobs() == [], str(jl.jobs()))

# An unscoped run (Run All) touches whatever it finds, and says so by holding
# the library root: that is the claim a delete/move/tag write hits.
check("an unscoped run holds the library root",
      script_runners.held_paths({"music_folder": music}, None)
      == [os.path.join(music, "Artists")],
      str(script_runners.held_paths({"music_folder": music}, None)))
check("a scoped run holds its own targets",
      script_runners.held_paths({"music_folder": music}, [run_album]) == [run_album])

print("== the payload MAINTAIN → In progress reads ==")

from server import api_jobs  # noqa: E402

with jl.holding([album], kind="scripts", label="Grade"):
    payload = api_jobs.job_lock_list()
    row = (payload.get("jobs") or [{}])[0]
    check("the endpoint lists the running job with its label and kind",
          row.get("label") == "Grade" and row.get("kind") == "scripts", str(payload))
    check("...the job id the page keys rows on", bool(row.get("job")), str(payload))
    check("...the paths it holds and how long it has been running",
          row.get("paths") == [album] and isinstance(row.get("elapsed"), (int, float)),
          str(payload))
    check("...and a progress slot the page can render (null here)",
          row.get("progress") is None, str(payload))
check("the endpoint is empty once the work is over",
      api_jobs.job_lock_list()["jobs"] == [], str(api_jobs.job_lock_list()))

print("== over HTTP: a locked path is refused 409 ==")

check("no job is left in this thread's context between sections",
      jl.current() is None and jl.jobs() == [], f"{jl.current()} {jl.jobs()}")

mlo_main = None
try:
    from fastapi.testclient import TestClient  # noqa: E402  (heavy import)

    from server import beetscfg, main as mlo_main, soulseek  # noqa: E402
except Exception as e:                                   # pragma: no cover
    print(f"  SKIP  TestClient unavailable ({e})")

if mlo_main is not None:
    _real = {
        "config": mlo_main.load_config,
        "cover_bytes": mlo_main._cover_url_bytes,
        "beets": beetscfg.run_beets_import,
        "export": mlo_main.exporter.export_tracks,
        "import_one": mlo_main._import_one_album,
        "pending": soulseek._pending_album_folders,
        "completed": soulseek.import_completed,
    }
    mlo_main.load_config = lambda: {**_real["config"](), "music_folder": music,
                                    "first_run_done": True, "beets_organize_after": False}
    client = TestClient(mlo_main.app)        # no lifespan: no workers, no slskd

    def with_foreign_job(held, body):
        """Run *body* while a job on ANOTHER THREAD holds *held*.

        Another thread, not this one: the in-process test client copies the
        CALLING context into the request task, so a job held here would be
        inherited by the request (driving the app in-process is the only place
        that happens — a real client arrives with a context of its own) and the
        request would join it instead of being refused."""
        holding_now, released = threading.Event(), threading.Event()

        def holder():
            with jl.holding(held, kind="scripts", label="Optimize FLACs"):
                holding_now.set()
                released.wait(20)

        thread = threading.Thread(target=holder)
        thread.start()
        holding_now.wait(10)
        try:
            return body()
        finally:
            released.set()
            thread.join(10)

    def probe(name, held, request):
        """One route's lock behaviour: 409 while a foreign job holds a path it
        claims, its own work done once that job is gone."""
        r = with_foreign_job(held, request)
        check(f"{name} is refused 409 while the path is held",
              r.status_code == 409, f"{r.status_code} {r.text[:160]}")
        r = request()
        check(f"{name} does its work once the job is gone",
              r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        return r

    auth_on = client.post("/api/tags/bulk", json={"paths": [track], "set": {"GENRE": "Rock"}})
    if auth_on.status_code in (401, 428):
        print(f"  SKIP  the auth gate is on in this checkout ({auth_on.status_code})")
    else:
        r = with_foreign_job(
            [album], lambda: client.post("/api/tags/bulk",
                                         json={"paths": [track], "set": {"GENRE": "Rock"}}))
        check("a tag write on a locked track is refused 409",
              r.status_code == 409, f"{r.status_code} {r.text[:200]}")
        check("the 409 names the job holding it",
              "Optimize FLACs" in r.json().get("detail", ""), r.text[:200])
        r = with_foreign_job([album],
                             lambda: client.post("/api/album/remove", json={"path": album}))
        check("deleting a locked album to the trash is refused 409",
              r.status_code == 409, f"{r.status_code} {r.text[:200]}")
        r = with_foreign_job(
            [album], lambda: client.post("/api/organize",
                                         json={"paths": [album], "dry_run": False}))
        check("moving/renaming a locked album is refused 409",
              r.status_code == 409, f"{r.status_code} {r.text[:200]}")
        r = with_foreign_job(
            [album], lambda: client.post("/api/organize",
                                         json={"paths": [album], "dry_run": True}))
        check("a dry run is not blocked — it changes nothing",
              r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        r = client.post("/api/tags/bulk", json={"paths": [track], "set": {"GENRE": "Rock"}})
        check("the same write goes through once the job is gone",
              r.status_code == 200, f"{r.status_code} {r.text[:200]}")

        # ---- every other route that changes library files -------------------
        # A 1x1 PNG: a real image frame, so the cover routes write a real file.
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AABAAA//8D"
            "AAGjY9NqAAAAAElFTkSuQmCC")
        staging = os.path.join(music, "Staging", "Album")
        os.makedirs(staging, exist_ok=True)
        with open(os.path.join(staging, "01 - Track.flac"), "w", encoding="utf-8") as fh:
            fh.write("not really audio")
        dest_drive = tempfile.mkdtemp(prefix="mlo-locks-dest-")

        mlo_main._cover_url_bytes = lambda *a, **k: (png, "image/png")
        probe("the cover upload", [album],
              lambda: client.post(f"/api/cover?album={album}",
                                  files={"file": ("cover.png", png, "image/png")}))
        probe("the cover download", [album],
              lambda: client.post(f"/api/cover/fromurl?album={album}"
                                  f"&url=http://127.0.0.1:1/cover.png"))
        probe("clearing cover mappings", [album],
              lambda: client.post("/api/cover/clear", json={"album": album}))
        probe("the lyrics sidecar write", [track],
              lambda: client.post("/api/lyrics/write",
                                  json={"path": track, "lrc": "[00:01.00] hello\n"}))
        beetscfg.run_beets_import = lambda paths, *a, **k: (True, "stub")
        probe("the beets import", [album],
              lambda: client.post("/api/beets/import", json={"paths": [album]}))
        mlo_main.exporter.export_tracks = lambda *a, **k: {"failed": 0, "exported": 0, "written": []}
        probe("the export", [track],
              lambda: client.post("/api/export",
                                  json={"paths": [track], "dest": dest_drive, "codec": "copy"}))
        probe("the album ingest", [staging],
              lambda: client.post("/api/import/ingest"
                                  f"?source={staging}&target=Ingested"))
        check("the ingest really moved the album",
              os.path.isdir(os.path.join(music, "Artists", "Ingested")))

        # The downloads import claims the albums it is about to move, so a
        # second import (or a run over the library) cannot take them first.
        # `import_completed` (soulseek's own mover) is stubbed to report the
        # album this temp tree holds: the mover itself is that module's test,
        # what is pinned here is that the guarded route proceeds with it.
        ready = os.path.join(music, ".mlo", "downloads", "Ready Album")
        os.makedirs(ready, exist_ok=True)
        with open(os.path.join(ready, "01 - Track.flac"), "w", encoding="utf-8") as fh:
            fh.write("not really audio")
        soulseek._pending_album_folders = lambda cfg=None: set()
        soulseek.import_completed = lambda *a, **k: [ready]
        mlo_main._import_one_album = lambda a, cfg, **k: {
            "album_root": a, "errors": [], "organized": True,
            "media_tagged": 0, "converted": 0, "identity_stamped": 0}
        r = probe("the downloads import", [ready], lambda: client.post("/api/soulseek/import"))
        check("the downloads import went on to import the album it holds",
              r.json().get("moved") == [ready], str(r.json())[:200])

        mlo_main.load_config = _real["config"]
        mlo_main._cover_url_bytes = _real["cover_bytes"]
        beetscfg.run_beets_import = _real["beets"]
        mlo_main.exporter.export_tracks = _real["export"]
        mlo_main._import_one_album = _real["import_one"]
        soulseek._pending_album_folders = _real["pending"]
        soulseek.import_completed = _real["completed"]
        shutil.rmtree(dest_drive, ignore_errors=True)

shutil.rmtree(music, ignore_errors=True)
print(f"\n{len(FAILED)} failure(s)")
sys.exit(1 if FAILED else 0)
