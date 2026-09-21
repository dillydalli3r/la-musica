#!/usr/bin/env python3
"""Verification for the ONE download queue and the parallelism behind it.

Covers, against the REAL registries (wish db, job registry, bulk queue, import
runner) and a stubbed slskd pipeline:

  1. /api/queue reports every state of the pipeline from data the registries
     actually hold: a wish waiting, two searches running at the same time, a
     finished download with its import outcome, a release still waiting in the
     bulk queue, a failed job with its reason and a download sitting in the
     download folder.
  2. Several wishes are searched/downloaded AT ONCE (two provably overlap).
  3. Two items heading for the SAME library folder cannot both run: the second
     one waits on the first one's job_locks claim and only starts once it is
     gone.

Standalone: `python tools/test_queue_view.py`, temp roots, no network, no slskd.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time as real_time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# hermeticity: a throwaway music folder + config file, so nothing here can
# touch the developer's library.
# --------------------------------------------------------------------------- #
REDIRECT = tempfile.mkdtemp(prefix="mlo-queue-redirect-")
os.environ["MLO_MUSIC_FOLDER"] = REDIRECT

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB_CFG = os.path.join(REDIRECT, "config.json")
with open(_STUB_CFG, "w", encoding="utf-8") as f:
    json.dump({"music_folder": REDIRECT}, f)
cfgmod.CONFIG_FILE = _STUB_CFG
pathmod.CONFIG_FILE = _STUB_CFG

import server.api_queue as api_queue  # noqa: E402
import server.import_queue as import_queue  # noqa: E402
import server.integrations as intg  # noqa: E402
import server.job_locks as job_locks  # noqa: E402
import server.soulseek as slsk  # noqa: E402
import server.soulseek_auto as auto  # noqa: E402
import server.wishes as wishes  # noqa: E402
import server.wishes_worker as worker  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# The wish list lives in its own sqlite file next to the real one — redirect it
# BEFORE anything opens it.
wishes.db_path = lambda: os.path.join(REDIRECT, "wishes.db")
wishes._initialized = False

WISHES_DB = wishes.db_path()
CONCURRENCY = 3
CFG = {
    "music_folder": REDIRECT,
    "soulseek_search_concurrency": CONCURRENCY,
    "soulseek_auto_log_min_score": 100,
    "wishes_enabled": True,
    "wishes_interval_hours": 6,
    "wishes_auto_import": True,
    "wishes_max_attempts": 0,
    "soulseek_download_slots": 3,
}

MBID = "0b1a2c3d-4e5f-6789-abcd-ef0123456789"


class Patch:
    """Set attributes for the block, restore them however it ends."""

    def __init__(self, obj, **kw):
        self.obj = obj
        self.kw = kw
        self.saved = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.saved[k] = getattr(self.obj, k, None)
            setattr(self.obj, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.obj, k, v)
        return False


def _release(mbid, artist, title, tracks=2):
    return {
        "id": mbid, "title": title, "date": "1994", "country": "GB",
        "catalog_number": "", "release_group_id": MBID,
        "artists": [{"name": artist}],
        "medium_formats": ["Digital Media"],
        "media": [{"disc": 1, "position": i, "title": f"Track {i}",
                   "length": 1000} for i in range(1, tracks + 1)],
    }


class Pipeline:
    """A stubbed slskd pipeline that records when each job was where.

    Every stage appends a window; `peak` keeps the highest number of jobs seen
    inside a stage at the same time, which is how "several at once" is proven
    from the server's own behaviour rather than from a sleep. A stage can also
    WAIT until a second job has arrived (`expect_two`) — a serial pipeline then
    fails on the timeout instead of passing by luck on a slow machine.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.windows = []          # (stage, job_id, label, t0, t1)
        self.peak = {}
        self.inside = {}
        self.expect_two = None     # stage name during the concurrency act
        self.starved = []          # stages where the wait for a second timed out
        self.imports = {}          # label -> result dict (or "raise")
        self.ready = set()         # labels whose download finished (no import)

    # -- the stages ------------------------------------------------------- #
    def _enter(self, stage):
        job_id = int(auto._job.get("id") or 0)
        label = str(auto._job.get("label") or "")
        with self._lock:
            n = self.inside.get(stage, 0) + 1
            self.inside[stage] = n
            self.peak[stage] = max(self.peak.get(stage, 0), n)
        return job_id, label, real_time.time()

    def _leave(self, stage, job_id, label, t0):
        with self._lock:
            self.inside[stage] -= 1
            self.windows.append((stage, job_id, label, t0, real_time.time()))

    def _gate(self, stage):
        """Wait (briefly) for a SECOND job in this stage, when asked to."""
        if self.expect_two != stage:
            return
        deadline = real_time.time() + 8.0
        while real_time.time() < deadline:
            with self._lock:
                if self.inside.get(stage, 0) >= 2:
                    return
            real_time.sleep(0.02)
        with self._lock:
            self.starved.append(stage)

    def search(self, slsk_, queries, wait_s, usable=None, response_limit=0):
        job_id, label, t0 = self._enter("searching")
        try:
            self._gate("searching")
            real_time.sleep(0.05)
        finally:
            self._leave("searching", job_id, label, t0)
        return ([("q", {"responses": []})], [], 0)

    def candidates(self, results, release, cfg):
        """One complete, lossless folder titled after the release, so each job
        claims the folder its own release names."""
        name = f"{release['artists'][0]['name']} - {release['title']}"
        files = [{"file": f"/remote/{name}/0{i} - Track.flac", "size": 1000}
                 for i in range(1, 3)]
        return [{
            "username": "peer", "dir": f"/remote/{name}/", "files": files,
            "audio": files, "logs": [], "cues": [], "matched": 2, "expected": 2,
            "complete": True, "lossless": True, "slot": False, "queue": 0,
            "speed": 0, "total_size": 2000, "score": 90,
        }]

    def wait_for_files(self, slsk_, ddir, username, wanted, timeout_s,
                       cancel_check=None, phase="download", queue_budget_s=None,
                       on_start=None):
        job_id, label, t0 = self._enter("downloading")
        try:
            self._gate("downloading")
            # A real transfer takes minutes; the stub takes just long enough
            # that jobs landing at the same moment are observably side by side.
            real_time.sleep(0.05)
            root = os.path.join(ddir, username, f"album-{job_id}")
            got = {}
            for w in wanted:
                path = os.path.join(root, os.path.basename(w["filename"]))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(b"x" * 64)
                got[w["filename"]] = path
            return got
        finally:
            self._leave("downloading", job_id, label, t0)

    def verify(self, root, cfg, is_cd):
        job_id, label, t0 = self._enter("verifying")
        try:
            real_time.sleep(0.02)
            return True, []
        finally:
            self._leave("verifying", job_id, label, t0)

    def import_album(self, root, release, cfg, media):
        job_id, label, t0 = self._enter("importing")
        try:
            real_time.sleep(0.05)
            if label in self.ready:
                return {"album_path": "", "imported": False,
                        "staging_path": root, "organized": False,
                        "organize_error": None}
            for key, value in self.imports.items():
                if key and key in label:
                    if value == "raise":
                        raise RuntimeError(f"no match for {label}")
                    return dict(value, album_path=value.get("album_path") or root)
            return {"album_path": os.path.join(REDIRECT, "Artists", label),
                    "imported": True, "staging_path": root, "organized": True,
                    "organize_error": None}
        finally:
            self._leave("importing", job_id, label, t0)

    # -- assertions ------------------------------------------------------- #
    def overlap(self, stage):
        """The most jobs seen in `stage` at the same time."""
        return int(self.peak.get(stage, 0))

    def window(self, stage, job_id):
        for w in self.windows:
            if w[0] == stage and w[1] == job_id:
                return w
        return None


PIPE = Pipeline()
RESOLVED = {}


def _resolve(mbid):
    """integrations.resolve_release, canned: (release, release id)."""
    rel = RESOLVED.get(str(mbid))
    if rel is None:
        return None, ""
    return rel, rel["id"]


def _fast_clock():
    """wishes_worker's clock: real time, but its 2 s job polls become 20 ms."""
    return SimpleNamespace(time=real_time.time,
                           sleep=lambda s: real_time.sleep(min(s, 0.02)))


def _app():
    """A tiny app with the queue router alone (server/main.py registers it in
    the real app — this proves the router itself)."""
    app = FastAPI()
    app.include_router(api_queue.router)
    return TestClient(app)


def _queue(client=None):
    return (client or _app()).get("/api/queue").json()


def _wait_for(predicate, timeout=20.0, what="condition"):
    deadline = real_time.time() + timeout
    while real_time.time() < deadline:
        value = predicate()
        if value:
            return value
        real_time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


# --------------------------------------------------------------------------- #
# 1. the queue view's mapping, straight off the registries
# --------------------------------------------------------------------------- #
client = _app()
empty = _queue(client)
assert empty["counts"]["total"] == 0, empty["counts"]
assert sorted(empty["sections"]) == ["completed", "failed", "in_progress",
                                     "needs_attention", "queued"], sorted(empty["sections"])
assert empty["concurrency"] == CONCURRENCY, empty["concurrency"]
assert empty["download_slots"] == 3, empty["download_slots"]

with Patch(auto, load_config=lambda: dict(CFG),
           _search_queries=PIPE.search,
           find_candidates=PIPE.candidates,
           _wait_for_files=PIPE.wait_for_files,
           _verify_album=PIPE.verify,
           _stamp_media=lambda root, media, c: (2, []),
           _import=PIPE.import_album,
           traceback=SimpleNamespace(print_exc=lambda *a, **k: None)), \
     Patch(slsk, is_running=lambda *a, **k: True, web_up=lambda *a, **k: True,
           server_state=lambda *a, **k: {"isLoggedIn": True},
           download_dir=lambda *a, **k: os.path.join(REDIRECT, "downloads"),
           enqueue_download=lambda *a, **k: None, ready_albums=lambda *a, **k: [],
           prune_download_dirs=lambda *a, **k: None), \
     Patch(wishes, owned_mbids=lambda cfg=None: {}), \
     Patch(intg, resolve_release=_resolve), \
     Patch(worker, time=_fast_clock()):
    # A wish per wish: three MusicBrainz releases the worker will fill.
    for i, (artist, title) in enumerate((("Alan Braxe", "Intro"),
                                         ("Bicep", "Isles"),
                                         ("Crimson", "Nova"))):
        rel = _release(f"11111111-0000-0000-0000-00000000000{i}", artist, title)
        RESOLVED[rel["id"]] = rel
        RESOLVED[rel["release_group_id"]] = rel
        wishes.add_wish(rel["id"], title=title, artist=artist)

    # --- a wish that is waiting: queued, sourced, cancelable --------------- #
    payload = _queue(client)
    assert payload["counts"]["queued"] == 3, payload["counts"]
    rows = payload["sections"]["queued"]
    assert all(r["kind"] == "wish" and r["stage"] == "queued" for r in rows), rows
    assert all(r["source"] == "MusicBrainz" for r in rows), rows
    assert all(r["cancelable"] for r in rows), rows
    assert {r["title"] for r in rows} == {"Intro", "Isles", "Nova"}, rows

    # --- a release waiting in the PIPELINE's own queue -------------------- #
    # (its row is a different kind: nothing has started it yet)
    with Patch(auto, concurrency=lambda cfg=None: 0):
        auto.enqueue(release=_release("22222222-0000-0000-0000-000000000001",
                                      "Daft Punk", "Homework"))
    thin = _queue(client)
    pipeline_rows = [r for r in thin["sections"]["queued"] if r["kind"] == "pipeline"]
    assert len(pipeline_rows) == 1, thin["sections"]["queued"]
    assert pipeline_rows[0]["title"] == "Homework", pipeline_rows[0]
    assert pipeline_rows[0]["source"] == "MusicBrainz", pipeline_rows[0]
    assert auto.drop_queued("22222222-0000-0000-0000-000000000001") is True

    # --- a download sitting in the download folder ------------------------ #
    ready_path = os.path.join(REDIRECT, "downloads", "peer", "Some Album")
    os.makedirs(ready_path, exist_ok=True)
    with Patch(slsk, ready_albums=lambda *a, **k: [ready_path]):
        done = _queue(client)
        ready_rows = [r for r in done["sections"]["completed"] if r["kind"] == "ready"]
        assert len(ready_rows) == 1, done["sections"]["completed"]
        assert ready_rows[0]["note"] == "In the download folder — ready to import"
        assert ready_rows[0]["path"] == ready_path, ready_rows[0]
        assert ready_rows[0]["cancelable"] is False, ready_rows[0]

    # --- an import run in progress --------------------------------------- #
    import_queue.set_importer(lambda path: {"album_root": path, "errors": []})
    gate = threading.Event()
    import_queue.set_ready_provider(lambda: [ready_path])

    def _slow_import(path):
        gate.wait(5)
        return {"album_root": path, "errors": []}

    import_queue._importer = _slow_import
    started = import_queue.start()
    assert started["ok"], started
    try:
        running = _queue(client)
        imp = [r for r in running["sections"]["in_progress"] if r["kind"] == "import"]
        assert len(imp) == 1, running["sections"]["in_progress"]
        assert imp[0]["stage"] == "importing", imp[0]
        assert imp[0]["progress"]["total"] == 1, imp[0]
    finally:
        gate.set()
        import_queue._stop.set()
        _wait_for(lambda: not import_queue.running(), 10, "the import run to stop")
        import_queue._stop.clear()

    # ------------------------------------------------------------------- #
    # 2. several wishes at once — the worker fills the pipeline
    # ------------------------------------------------------------------- #
    PIPE.expect_two = "searching"     # a serial pipeline starves here
    result = worker.run_cycle()
    PIPE.expect_two = None
    assert result["ok"], result
    assert PIPE.starved == [], f"stages never reached two jobs at once: {PIPE.starved}"
    assert PIPE.overlap("searching") >= 2, PIPE.peak
    assert PIPE.overlap("downloading") >= 2, PIPE.peak
    assert result["imported"] == 3, result

    # ...and the queue view said so while it ran: three wishes, none of them
    # duplicated into a separate job row.
    finished = _queue(client)
    assert finished["counts"]["queued"] == 0, finished["counts"]
    assert finished["counts"]["completed"] == 3, finished["counts"]
    assert finished["running"] == 0, finished["running"]
    completed = finished["sections"]["completed"]
    assert all(r["kind"] == "wish" for r in completed), completed
    assert all(r["stage"] == "completed" for r in completed), completed
    assert all("Imported into the library" in r["note"] for r in completed), completed
    assert all(r["album_path"] for r in completed), completed
    assert all(r["job_id"] for r in completed), completed
    assert {r["job_id"] for r in completed} == set(range(1, 4)) or \
        len({r["job_id"] for r in completed}) == 3, completed
    assert [w["status"] for w in wishes.list_wishes()] == ["imported"] * 3, wishes.list_wishes()
    # exactly one row per album: no wish row duplicated by its job
    assert len(completed) == 3, completed
    # the job carries the release's own facts (medium, track count) so a row can
    # say WHICH release is being fetched without a MusicBrainz request per poll
    done_job = auto.job_state(completed[0]["job_id"])["release"]
    assert done_job["media_formats"] == ["Digital Media"], done_job
    assert done_job["tracks"] == 2, done_job

    # ------------------------------------------------------------------- #
    # 3. one album folder, one job: the second waits, then lands
    # ------------------------------------------------------------------- #
    # Two DIFFERENT releases that would import into the SAME folder (two
    # editions of one album) — the claim is what keeps them out of each
    # other's way.
    twin_a = _release("33333333-0000-0000-0000-00000000000a", "Twin", "Album")
    twin_b = _release("33333333-0000-0000-0000-00000000000b", "Twin", "Album")
    folder = auto._album_claim(twin_a, CFG)
    assert folder.endswith(os.path.join("Artists", "Twin - Album")), folder

    before = len(PIPE.windows)
    first = auto.start_job(release=twin_a, source="musicbrainz")
    second = auto.start_job(release=twin_b, source="musicbrainz")
    first_id = first["job"]["id"]
    second_id = second["job"]["id"]
    assert first_id != second_id, (first_id, second_id)

    # while the first one owns the folder, the second one is QUEUED (waiting on
    # the claim) — never searching, never downloading.
    _wait_for(lambda: PIPE.window("downloading", first_id), 10, "the first job to download")
    assert job_locks.holder(folder), f"nothing claimed {folder}"
    assert auto.job_state(second_id)["stage_key"] == "queued", auto.job_state(second_id)
    with PIPE._lock:
        second_early = [w for w in PIPE.windows[before:] if w[1] == second_id]
    assert second_early == [], f"the second job started alongside the first: {second_early}"

    # it starts once the first is done, and the two never overlap
    _wait_for(lambda: auto.job_state(second_id)["state"] in ("done", "error", "cancelled"),
              30, "the second job to run and settle")
    assert auto.job_state(second_id)["state"] == "done", auto.job_state(second_id)
    a_down = PIPE.window("downloading", first_id)
    b_search = PIPE.window("searching", second_id)
    b_down = PIPE.window("downloading", second_id)
    assert b_search and b_down, PIPE.windows[before:]
    assert b_down[3] >= a_down[4], (a_down, b_down)
    assert not job_locks.holder(folder), "the claim was not released"

    # ------------------------------------------------------------------- #
    # 4. a completed download reports its own import outcome
    # ------------------------------------------------------------------- #
    half = _release("44444444-0000-0000-0000-000000000001", "Half", "Downloaded")
    PIPE.ready.add("Half — Downloaded")          # downloads, never imports
    auto.start_job(release=half, source="soulseek")
    _wait_for(lambda: [j for j in auto.jobs()
                       if (j.get("release") or {}).get("id") == half["id"]
                       and j["state"] != "running"], 20, "the half-done job")
    row = [r for r in _queue(client)["sections"]["completed"]
           if r["title"] == "Downloaded"]
    assert len(row) == 1, _queue(client)["sections"]["completed"]
    assert row[0]["stage"] == "completed", row[0]
    assert row[0]["note"] == "Downloaded — waiting in the download folder to be imported", row[0]
    assert row[0]["source"] == "Soulseek", row[0]

    # a failure keeps its reason, in the failed section
    PIPE.imports["Broken"] = "raise"
    bad = _release("55555555-0000-0000-0000-000000000001", "Broken", "Release")
    auto.start_job(release=bad, source="soulseek")
    _wait_for(lambda: [r for r in _queue(client)["sections"]["failed"]
                       if r["title"] == "Release"], 20, "the failed row")
    fail = [r for r in _queue(client)["sections"]["failed"] if r["title"] == "Release"][0]
    assert fail["stage"] == "failed", fail
    assert "no match for" in fail["reason"], fail
    assert fail["release_mbid"] == bad["id"], fail

    # ------------------------------------------------------------------- #
    # 5. cancelling rows goes through the primitive that owns them
    # ------------------------------------------------------------------- #
    cancelme = _release("66666666-0000-0000-0000-000000000001", "Gone", "Soon")
    RESOLVED[cancelme["id"]] = cancelme
    w = wishes.add_wish(cancelme["id"], title="Soon", artist="Gone")
    q = _queue(client)["sections"]["queued"]
    row = [r for r in q if r["wish_id"] == w["id"]][0]
    assert row["cancelable"] and row["id"] == f"wish:{w['id']}", row
    r = client.post("/api/queue/cancel", json={"id": row["id"]})
    assert r.status_code == 200, r.text
    assert wishes.get_wish(w["id"]) is None, "the wish survived its cancel"
    assert client.post("/api/queue/cancel", json={"id": "wish:999999"}).status_code == 404
    assert client.post("/api/queue/cancel", json={"id": "nope"}).status_code == 400
    assert client.post("/api/queue/cancel", json={"id": "job:999999"}).status_code == 409

    # a running job is cancelled by id, and the queue says it is gone
    hold = threading.Event()
    PIPE.expect_two = None

    def _blocking_search(slsk_, queries, wait_s, usable=None, response_limit=0):
        hold.wait(10)
        return ([("q", {"responses": []})], [], 0)

    with Patch(auto, _search_queries=_blocking_search):
        live = _release("77777777-0000-0000-0000-000000000001", "Live", "Now")
        started = auto.start_job(release=live, source="musicbrainz")
        job_id = started["job"]["id"]
        _wait_for(lambda: auto.job_state(job_id)["stage_key"] == "searching", 10,
                  "the live job to search")
        running_rows = _queue(client)["sections"]["queued"]
        assert [r for r in running_rows if r["job_id"] == job_id], running_rows
        assert _queue(client)["running"] == 1, _queue(client)["running"]
        # A job that has outlived its wish row still shows: hiding it because
        # the row it belonged to went away would hide real work.
        with Patch(wishes, list_wishes=lambda: []):
            orphaned = _queue(client)
            assert [r for r in orphaned["sections"]["queued"] if r["job_id"] == job_id], orphaned
        done = client.post("/api/queue/cancel", json={"id": f"job:{job_id}"})
        assert done.status_code == 200, done.text
        hold.set()
        _wait_for(lambda: auto.job_state(job_id)["state"] == "cancelled", 15,
                  "the cancelled job to settle")
    # a cancelled job is not a failure and not a completion: it leaves the queue
    after = _queue(client)
    assert not [r for r in after["sections"]["in_progress"] + after["sections"]["failed"]
                if r["job_id"] == job_id], after

    # ------------------------------------------------------------------- #
    # 6. every stage, in one payload — and the page renders it
    # ------------------------------------------------------------------- #
    from server import main as mlo_main      # heavy import, last on purpose
    wired = TestClient(mlo_main.app).get("/api/queue")
    assert wired.status_code == 200, (wired.status_code, wired.text)
    assert "sections" in wired.json(), wired.json()

# A controlled payload with one row in EVERY stage: the states a live run only
# passes through (a download in flight, a job parked on the user, an import
# run) are what the page has to draw, and a fixture is the only way to hold
# them still. The mapping itself is the real build_queue().
STAGE_ROWS = [
    ("queued", "11111111", "Wish", "Waiting", "musicbrainz"),
    ("searching", "bbbbbbbb", "Crimson", "Nova", "soulseek"),
    ("downloading", "aaaaaaaa", "Bicep", "Isles", "musicbrainz"),
    ("importing", "99999999", "Run", "Importing", "soulseek"),
    ("needs_attention", "cccccccc", "Twin", "Parked", "musicbrainz"),
    ("completed", "dddddddd", "Daft Punk", "Homework", "musicbrainz"),
    ("failed", "eeeeeeee", "Broken", "Release", "soulseek"),
]
FIXTURE_JOBS = [
    {"id": 7, "state": "running", "stage": "Downloading 12 file(s) from peer…",
     "stage_key": "downloading",
     # The release facts a real job carries (server/soulseek_auto._run writes
     # this block from the MusicBrainz release it resolved): the pressing's own
     # catalogue number and medium first, then where/when it is from, its track
     # count, the edition's disambiguation and MusicBrainz's status.
     "release": {"id": "aaaaaaaa", "artist": "Bicep", "title": "Isles",
                 "date": "2018-04-20", "country": "GB",
                 "catalog_number": "ZEN-124", "media": "CD",
                 "media_formats": ["CD"], "tracks": 12,
                 "status": "Official", "disambiguation": "Deluxe Edition",
                 "label": "Ninja Tune"},
     "log": [{"t": "00:00:01", "msg": "Target: Bicep — Isles"}], "attempts": [],
     "result": None, "confirm": None, "search": None,
     "progress": {"dir": "Music/Isles", "bytes": 41_000_000, "size": 82_000_000,
                  "percent": 50.0, "files_done": 6, "files_total": 12,
                  "speed": 512_000, "eta_s": 80},
     "wish_id": 2, "source": "musicbrainz", "label": "Bicep — Isles",
     "started_at": 1.0, "ended_at": 0.0},
    {"id": 8, "state": "running", "stage": "Searching Soulseek…",
     "stage_key": "searching",
     "release": {"id": "bbbbbbbb", "artist": "Crimson", "title": "Nova"},
     "log": [], "attempts": [], "result": None, "confirm": None,
     "search": {"query": "Crimson Nova", "state": "InProgress", "responses": 5,
                "files": 91},
     "progress": None, "wish_id": None, "source": "soulseek",
     "label": "Crimson — Nova", "started_at": 2.0, "ended_at": 0.0},
    {"id": 9, "state": "confirm", "stage": "Waiting: only lossy copies found",
     "stage_key": "needs_attention",
     "release": {"id": "cccccccc", "artist": "Twin", "title": "Parked"},
     "log": [], "attempts": [], "result": None,
     "confirm": {"reason": "lossy_only", "formats": ["mp3"], "candidates": []},
     "search": None, "progress": None, "wish_id": None, "source": "musicbrainz",
     "label": "Twin — Parked", "started_at": 3.0, "ended_at": 0.0},
    {"id": 10, "state": "done", "stage": "Done", "stage_key": "completed",
     "release": {"id": "dddddddd", "artist": "Daft Punk", "title": "Homework"},
     "log": [], "attempts": [],
     "result": {"album_path": "F:/Music/Artists/Daft Punk - Homework",
                "imported": True, "organized": True},
     "confirm": None, "search": None, "progress": None, "wish_id": None,
     "source": "musicbrainz", "label": "Daft Punk — Homework",
     "started_at": 4.0, "ended_at": 5.0},
    {"id": 11, "state": "error", "stage": "Every candidate was rejected",
     "stage_key": "failed",
     "release": {"id": "eeeeeeee", "artist": "Broken", "title": "Release"},
     "log": [], "attempts": [],
     "result": {"error": "every candidate was rejected (3 attempts)"},
     "confirm": None, "search": None, "progress": None, "wish_id": None,
     "source": "soulseek", "label": "Broken — Release",
     "started_at": 6.0, "ended_at": 7.0},
]
FIXTURE_WISHES = [
    {"id": 1, "release_mbid": "11111111", "title": "Waiting", "artist": "Wish",
     "year": "2004", "status": "wanted", "note": "", "target_dir": "",
     "queries": [], "attempts": 0, "added_at": 10.0, "updated_at": 10.0,
     "last_search": 0.0, "last_error": "", "album_path": "",
     "source": "musicbrainz", "pending": True},
    {"id": 3, "release_mbid": "33333333", "title": "Gone", "artist": "Solo",
     "year": "1999", "status": "failed", "note": "", "target_dir": "",
     "queries": [], "attempts": 4, "added_at": 11.0, "updated_at": 12.0,
     "last_search": 11.0, "last_error": "no verified match yet",
     "album_path": "", "source": "soulseek"},
    # A wish the network had nothing for: terminal, so it needs the USER — the
    # queue row must say so rather than pretend it is still being searched. It
    # carries the identity the store keeps for it (server/wishes.RELEASE_KEYS):
    # the pressing this wish is actually waiting for.
    {"id": 5, "release_mbid": "55555555", "title": "Absent", "artist": "Void",
     "year": "1990", "status": "not_found", "note": "", "target_dir": "",
     "queries": [], "attempts": 3, "added_at": 13.5, "updated_at": 14.5,
     "last_search": 14.0, "last_error": "no results at all", "not_found": 3,
     "album_path": "", "source": "musicbrainz",
     "release": {"id": "55555555", "title": "Absent", "artist": "Void",
                 "date": "1990-03", "country": "US", "status": "Promotion",
                 "media": ["CD"], "track_count": 3,
                 "disambiguation": "promo", "catalog_number": "PRO-CD-1",
                 "label": "Void Recordings"}},
]
FIXTURE_READY = os.path.join(REDIRECT, "downloads", "peer", "Some Album")
os.makedirs(FIXTURE_READY, exist_ok=True)

with Patch(auto, jobs=lambda: [dict(j) for j in FIXTURE_JOBS],
           queued=lambda: [{"release_mbid": "22222222", "key": "22222222",
                            "release": {"id": "22222222", "title": "Waiting Too",
                                        "artists": [{"name": "Queue"}]}}]), \
     Patch(wishes, list_wishes=lambda: [dict(w) for w in FIXTURE_WISHES]), \
     Patch(import_queue, status=lambda: {
         "state": "running", "total": 2, "done": 1, "current": FIXTURE_READY,
         "results": [], "errors": [], "started_at": 8.0, "finished_at": 0.0}), \
     Patch(slsk, ready_albums=lambda *a, **k: [FIXTURE_READY]):
    fixture = _queue(client)
    for stage in ("queued", "searching", "downloading", "importing",
                  "needs_attention", "completed", "failed"):
        assert stage in {r["stage"] for rows in fixture["sections"].values()
                         for r in rows}, (stage, fixture["sections"])
    # 'searching' and 'queued' share a section; downloading/importing live in
    # in-progress; completed rows carry their outcome; failed rows their reason
    assert fixture["counts"]["in_progress"] == 2, fixture["counts"]
    assert fixture["sections"]["completed"], fixture["sections"]["completed"]
    assert fixture["sections"]["failed"][0]["reason"], fixture["sections"]["failed"]
    done_row = [r for r in fixture["sections"]["completed"] if r["title"] == "Homework"][0]
    assert done_row["note"] == "Imported into the library", done_row
    assert done_row["album_path"] == "F:/Music/Artists/Daft Punk - Homework", done_row
    park = [r for r in fixture["sections"]["needs_attention"] if r["title"] == "Absent"]
    assert park and "unless you retry it" in park[0]["note"], fixture["sections"]["needs_attention"]
    assert park[0]["stage"] == "needs_attention", park[0]
    live = [r for r in fixture["sections"]["in_progress"] if r["title"] == "Isles"][0]
    assert live["progress"]["percent"] == 50.0, live
    # its release is also a wish in this fixture, so ONE row carries both
    assert live["wish_id"] == 2 and live["kind"] == "job", live
    # EVERY row carries the release identity block, with the documented keys,
    # built from what the row knows (a job's own release summary, a wish's
    # stored identity, the payload a queued release was queued with) — and the
    # rows that cannot know one carry it empty rather than missing.
    KEYS = set(wishes.RELEASE_KEYS)
    for rows in fixture["sections"].values():
        for row in rows:
            assert set(row["release"]) == KEYS, (row["id"], sorted(row["release"]))
    facts = live["release"]
    assert (facts["catalog_number"], facts["media"], facts["track_count"]) == \
        ("ZEN-124", ["CD"], 12), facts
    assert (facts["status"], facts["disambiguation"], facts["date"],
            facts["country"]) == ("Official", "Deluxe Edition", "2018-04-20", "GB"), facts
    absent = [r for r in fixture["sections"]["needs_attention"] if r["title"] == "Absent"][0]
    assert absent["release"]["catalog_number"] == "PRO-CD-1", absent["release"]
    assert absent["stage"] == "needs_attention", absent
    # The rows that cannot name a release (a stalled album, a finished download
    # in the folder) carry the same block with nothing in it.
    empty = [r for rows in fixture["sections"].values() for r in rows
             if r["kind"] in ("prompt", "ready", "import")]
    assert empty and all(not any(r["release"].values()) for r in empty), empty
    # The rows the page's "Clear finished" button counts and clears: the ones
    # whose work is over (a settled job, a wish nothing was found for). The
    # running job, the parked one, the wanted wish and the waiting release are
    # not among them — clearing never touches a row that is still going.
    clearable = sorted(r["id"] for rows in fixture["sections"].values() for r in rows
                       if r["clearable"])
    assert clearable == ["job:10", "job:11", "wish:5"], clearable

    # the payload the page is rendered with
    payload_path = os.path.join(REDIRECT, "queue-payload.json")
    with open(payload_path, "w", encoding="utf-8") as f:
        json.dump(fixture, f)

# The page itself: node + Vite render the REAL component with that payload.
# Exit 2 is this repo's "the tooling is not installed" (see check_player_state.cjs).
ui = subprocess.run(["node", os.path.join("tools", "check_queue_view.mjs"), payload_path],
                    cwd=ROOT, capture_output=True, text=True)
if ui.returncode == 0:
    print("ok  " + ui.stdout.strip().removeprefix("ok  "))
elif ui.returncode == 2:
    print("SKIPPED  the page render check (node or web/node_modules missing): "
          + (ui.stderr.strip().splitlines() or [""])[0])
else:
    raise AssertionError("the page render check failed:\n" + ui.stdout + ui.stderr)

PIPE.expect_two = None
auto.cancel()

print("ok  /api/queue reports queued, in-progress, completed and failed from the "
      "wish db, the job registry, the bulk queue, the import run and the download folder")
print(f"ok  {CONCURRENCY} wishes searched/downloaded at once "
      f"(peak {PIPE.overlap('searching')} searching, {PIPE.overlap('downloading')} downloading)")
print("ok  a completed download reports whether it was imported, and a failed job its reason")
print("ok  two items for the same library folder never run together (claim waited, then released)")
print("ok  per-item cancel goes through the wish list / the job / the bulk queue")
print("ok  one payload carries every stage, in its own section, with its outcome")

shutil.rmtree(REDIRECT, ignore_errors=True)
