#!/usr/bin/env python3
"""No acquisition ends silently: outcomes are announced once and are actionable.

This is the acceptance proof for the "surface the outcome" work, and it runs
against the REAL registries — the wish db, the job registry, the import-prompt
table, the queue view and the retry policy — with slskd, the filesystem and the
event bus stubbed where the network would be:

  1. a search that found NOTHING ends the wish `not_found` (terminal: the timer
     never searches it again), announces exactly ONE `wish_not_found` frame
     carrying a link, and comes back only through a manual retry;
  2. every give-up path of a download emits exactly one frame with its reason —
     an absent slskd, a failed verification, and a search with nothing usable;
  3. a transient failure backs off and then gives up ONCE (`wish_failed`);
  4. a stalled album lands in the queue's "Needs you" section with the families
     it is missing, the wizard link that carries album+step+missing, and the
     dismiss action; dismissing clears the row, and a later import re-raises it;
  5. a wish that stalled is retryable from the queue, and a failed job's
     release goes back into the pipeline;
  6. a cancelled job says nothing, and no path ever emits two frames for one
     outcome.

Standalone: `python tools/test_outcomes.py`, temp roots, no network, no slskd.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{('  — ' + detail) if detail and not ok else ''}")
    if not ok:
        FAILED.append(name)


# --------------------------------------------------------------------------- #
# hermeticity: a throwaway music folder + config, so nothing here can touch the
# developer's library, wishes or download folder.
# --------------------------------------------------------------------------- #
REDIRECT = tempfile.mkdtemp(prefix="mlo-outcomes-")
MUSIC = os.path.join(REDIRECT, "music")
DL = os.path.join(REDIRECT, "downloads")
os.makedirs(MUSIC, exist_ok=True)
os.makedirs(DL, exist_ok=True)
os.environ["MLO_MUSIC_FOLDER"] = MUSIC

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB_CFG = os.path.join(REDIRECT, "config.json")
with open(_STUB_CFG, "w", encoding="utf-8") as f:
    json.dump({"music_folder": MUSIC}, f)
cfgmod.CONFIG_FILE = _STUB_CFG
pathmod.CONFIG_FILE = _STUB_CFG

import server.api_queue as api_queue  # noqa: E402
import server.api_imports as api_imports  # noqa: E402
import server.events as events  # noqa: E402
import server.import_autonomy as autonomy  # noqa: E402
import server.integrations as intg  # noqa: E402
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

# The import prompts live under the app data dir: same treatment.
_DATA = os.path.join(REDIRECT, "data")
os.makedirs(_DATA, exist_ok=True)
pathmod.app_data_dir = lambda *a, **k: _DATA

MBID = "0b1a2c3d-4e5f-6789-abcd-ef0123456789"
REL = {
    "id": MBID, "title": "An Album", "date": "1994", "country": "GB",
    "catalog_number": "", "release_group_id": MBID,
    "artists": [{"name": "An Artist"}],
    "medium_formats": ["Digital Media"],
    "media": [{"disc": 1, "position": i, "title": f"Track {i}", "length": 1000}
              for i in (1, 2)],
}

CFG = {
    "music_folder": MUSIC,
    "soulseek_search_concurrency": 3,
    "soulseek_auto_log_min_score": 100,
    "soulseek_auto_search_wait": 2,
    "soulseek_auto_response_limit": 5,
    "soulseek_auto_wish_prompt": True,
    "wishes_enabled": True,
    "wishes_interval_hours": 6,
    "wishes_auto_import": True,
    "wishes_max_attempts": 0,
    "wishes_not_found_attempts": 1,
    "wishes_retry_backoff_minutes": 30,
}


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


def drain():
    """Forget every frame so far, so a count is about THIS outcome."""
    with events._lock:
        events._events.clear()
    try:  # the durable log as well: recent() reads both (spec R216)
        os.remove(events._event_log_path())
    except OSError:
        pass


def frames(kind=None):
    kept = events.recent(0, limit=10 ** 6)
    return [e for e in kept if kind is None or e.get("event") == kind]


def expect_frames(kind, count=1, timeout=10.0):
    """Wait for `count` frames of a kind, then return every frame so far.

    The wait is part of the contract, not test slack: a notification is
    emitted from the worker thread that settles the item and must never block
    it, so "the item is settled" and "the frame is on the bus" are two moments
    by design."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = frames()
        if len([f for f in got if f.get("event") == kind]) >= count:
            return got
        time.sleep(0.02)
    return frames()


def shape_ok(frame, kind):
    """What the tray needs: a kind, wording, a timestamp, the server's own event
    number and a link a client can navigate to."""
    link = (frame.get("data") or {}).get("link")
    return (frame.get("event") == kind and bool(frame.get("title"))
            and isinstance(frame.get("body"), str)
            and float(frame.get("at") or 0) > 0
            and int(frame.get("seq") or 0) > 0
            and isinstance(link, str) and link.startswith("/"))


def _wait_for(predicate, timeout=20.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


# --------------------------------------------------------------------------- #
# The stubbed pipeline: every stage a scenario needs, programmable per test.
# --------------------------------------------------------------------------- #
class Pipe:
    def __init__(self):
        self.candidates = []          # what find_candidates answers
        self.responses = []           # what the search answers
        self.verify = (True, [])
        self.delivered = "all"        # "all" | "partial" (a stalled peer)
        self.search_calls = 0
        self.started = []             # release ids start_job was asked for
        self.leftover = []

    def reset(self):
        self.__init__()


PIPE = Pipe()


def _settle(state, result=None, stage="Done"):
    return {"state": state, "stage": stage, "result": result or {}}


def _candidate():
    files = [{"file": f"/remote/{REL['title']}/0{i} - Track.flac", "size": 1000}
             for i in (1, 2)]
    return [{
        "username": "peer", "dir": f"/remote/{REL['title']}/", "files": files,
        "audio": files, "logs": [], "cues": [], "matched": 2, "expected": 2,
        "complete": True, "lossless": True, "slot": False, "queue": 0,
        "speed": 0, "total_size": 2000, "score": 90,
    }]


def stub_search(slsk_, queries, wait_s, usable=None, response_limit=0):
    PIPE.search_calls += 1
    return ([("q", {"responses": list(PIPE.responses)})], [], 0)


def stub_wait(slsk_, ddir, username, wanted, timeout_s, cancel_check=None,
              phase="download", queue_budget_s=None, on_start=None):
    root = os.path.join(ddir, username, "album")
    got = {}
    take = wanted if PIPE.delivered == "all" else wanted[:1]
    for w in take:
        path = os.path.join(root, os.path.basename(w["filename"]))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"x" * 64)
        got[w["filename"]] = path
    return got


def stub_drop(slsk_, ddir, username, wanted, remove_root=None):
    # The real sweep is covered where it lives (tools/test_soulseek_candidates);
    # here it is a hook, so a scenario can say "some bytes could not be freed".
    if PIPE.leftover:
        auto._note_leftovers(list(PIPE.leftover))
    return None


def pipe_patches(cfg=None, **over):
    kw = dict(
        load_config=lambda: dict(cfg or CFG),
        _search_queries=stub_search,
        find_candidates=lambda results, release, c: list(PIPE.candidates),
        _wait_for_files=stub_wait,
        _verify_album=lambda root, c, is_cd: PIPE.verify,
        _stamp_media=lambda root, media, c: (2, []),
        _drop_candidate=stub_drop,
        _prune_downloads=lambda: None,
        _start_import_chain=lambda album_dir, c: None,
        traceback=SimpleNamespace(print_exc=lambda *a, **k: None),
    )
    kw.update(over)
    return Patch(auto, **kw)


SLSK_UP = Patch(slsk, is_running=lambda *a, **k: True,
                web_up=lambda *a, **k: True,
                server_state=lambda *a, **k: {"isLoggedIn": True},
                download_dir=lambda *a, **k: DL,
                prune_download_dirs=lambda *a, **k: None,
                # Nothing here touches a real slskd (this developer machine may
                # well be running one): the transfers a job queues are recorded,
                # not sent.
                enqueue_download=lambda *a, **k: {"ok": True},
                cancel_downloads=lambda *a, **k: [],
                clear_transfer_files=lambda *a, **k: {"files_deleted": 0})
SLSK_DOWN = Patch(slsk, is_running=lambda *a, **k: False,
                  web_up=lambda *a, **k: False,
                  server_state=lambda *a, **k: {"isLoggedIn": False},
                  download_dir=lambda *a, **k: DL,
                  prune_download_dirs=lambda *a, **k: None)


def _wait_job_state(job_id, states=("done", "error", "cancelled"), timeout=30.0):
    return _wait_for(lambda: (lambda st: st if st.get("state") in states else None)(
        auto.job_state(job_id)), timeout=timeout, what=f"job {job_id} to settle")


# --------------------------------------------------------------------------- #
# 1. a search that found nothing: terminal, one frame, one manual way back
# --------------------------------------------------------------------------- #
print("== a wish whose searches find nothing ==")

# The two routers this proof needs: the queue view and the import prompts the
# stalled-album row's dismiss calls.
app = FastAPI()
app.include_router(api_queue.router)
app.include_router(api_imports.router)
client = TestClient(app)


def queue():
    return client.get("/api/queue").json()


wish = wishes.add_wish(MBID, title="An Album", artist="An Artist", source="soulseek")
cfg_nf = dict(CFG, wishes_not_found_attempts=1)


def resolve_any(mbid):
    """integrations.resolve_release, canned: this MBID IS the release."""
    return dict(REL, id=str(mbid)), str(mbid)


with SLSK_UP, Patch(intg, resolve_release=resolve_any), \
     Patch(worker, load_config=lambda: dict(cfg_nf),
                    _wait_job=lambda job_id, cancel_check, timeout_s=0: _settle(
                        "error", {"error": "No candidate folder contained every "
                                           "track (and cue/log per disc for CD)."}),
                    ), \
     Patch(auto, start_job=lambda **kw: (PIPE.started.append(kw.get("release_mbid")),
                                         {"ok": True, "job": {"id": 99}})[1]):
    drain()
    outcome = worker._run_one(wishes.get_wish(wish["id"]), cfg_nf)
    nf = frames("wish_not_found")
    check("an empty search ends the wish as not_found", outcome == "not_found", outcome)
    check("...with exactly ONE notification", len(nf) == 1, str(len(nf)))
    check("...with the payload shape", shape_ok(nf[0], "wish_not_found") if nf else False)
    check("...naming the wish and linking to its queue",
          bool(nf) and nf[0]["data"]["wish_id"] == wish["id"]
          and nf[0]["data"]["link"] == "/soulseek"
          and nf[0]["data"]["outcome"] == "not_found")
    check("...and nothing else was emitted", len(frames()) == 1, str(len(frames())))
    stored = wishes.get_wish(wish["id"])
    check("the wish records the outcome and the reason",
          stored["status"] == "not_found" and stored["not_found"] == 1
          and "No candidate folder" in stored["last_error"], json.dumps({
              "status": stored["status"], "err": stored["last_error"]}))
    check("a terminal wish is not due again — ever",
          wishes.due_at(stored, cfg_nf) == float("inf")
          and wishes.is_terminal(stored, cfg_nf) is True)

    # The single rung of the not-found budget is spent: the timer now leaves it
    # alone (no new search at all), which is what "terminal" has to mean.
    PIPE.started.clear()
    worker.run_cycle()
    check("the timer does not search a terminal wish again", PIPE.started == [],
          str(PIPE.started))

    # ...and the user's own retry re-arms it and searches it NOW: the wish that
    # was terminal gets a fresh budget and, this time, lands.
    PIPE.started.clear()
    drain()
    with Patch(worker, _wait_job=lambda job_id, cancel_check, timeout_s=0: _settle(
            "done", {"imported": True, "album_path": os.path.join(MUSIC, "An Album")})):
        worker.run_cycle(wish["id"])
    after = wishes.get_wish(wish["id"])
    check("a manual retry re-arms the wish and searches it",
          PIPE.started == [MBID] and after["status"] == "imported"
          and after["not_found"] == 0,
          json.dumps({"started": PIPE.started, "status": after["status"]}))
    check("...and the wish it filled is announced once",
          len(frames("wish_found")) == 1, str([f["event"] for f in frames()]))

# Re-marking a not_found wish is not a new outcome: no second frame.
wish_idem = wishes.add_wish("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                            title="Idempotent", artist="An Artist")
drain()
wishes.mark_not_found(wish_idem["id"], "nothing out there", 3)
wishes.mark_not_found(wish_idem["id"], "nothing out there", 4)
check("re-marking a not-found wish does not repeat the news",
      len(frames("wish_not_found")) == 1, str(len(frames("wish_not_found"))))

# --------------------------------------------------------------------------- #
# 2. transient failures: backoff, then a single give-up
# --------------------------------------------------------------------------- #
print("== a transient failure ==")

TRANSIENT = "verification failed (2 problem(s))"
wish2 = wishes.add_wish("11111111-2222-3333-4444-555555555555",
                        title="Another Album", artist="An Artist", source="soulseek")
cfg_b = dict(CFG, wishes_max_attempts=2, wishes_retry_backoff_minutes=30)

with SLSK_UP, Patch(intg, resolve_release=resolve_any), \
     Patch(worker, load_config=lambda: dict(cfg_b),
                    _wait_job=lambda job_id, cancel_check, timeout_s=0: _settle(
                        "error", {"error": TRANSIENT})), \
     Patch(auto, start_job=lambda **kw: {"ok": True, "job": {"id": 98}}):
    drain()
    out = worker._run_one(wishes.get_wish(wish2["id"]), cfg_b)
    first = wishes.get_wish(wish2["id"])
    check("a transient failure that is not the last attempt is retried",
          out == "pending" and first["status"] == "wanted", out)
    check("...with a backoff before the next attempt (the interval has passed, "
          "the backoff has not)",
          first["retry_at"] > time.time() and not worker._due(first, cfg_b)
          and wishes.retry_delay(cfg_b, 1) == 1800)
    check("...and it says nothing on the bus while it is only retrying",
          frames() == [], str(frames()))
    check("...and the reason is recorded",
          TRANSIENT[:20] in first["last_error"], first["last_error"])
    wrow = next((r for r in queue()["sections"]["queued"]
                 if r.get("wish_id") == wish2["id"]), None)
    check("...and the row says when it will be tried again",
          bool(wrow) and "Retrying after a failure at " in (wrow.get("note") or "")
          and wrow.get("retry_at", 0) > 0, json.dumps(wrow or {}))

    drain()
    out = worker._run_one(wishes.get_wish(wish2["id"]), cfg_b)
    second = wishes.get_wish(wish2["id"])
    failed = frames("wish_failed")
    check("the attempt cap ends it as failed", out == "failed"
          and second["status"] == "failed", f"{out} / {second['status']}")
    check("...with exactly ONE frame", len(failed) == 1, str(len(failed)))
    check("...that carries the reason and a link",
          shape_ok(failed[0], "wish_failed") if failed else False
          and "verification failed" in failed[0]["body"])
    check("...and it is terminal now (the cap is spent)",
          wishes.is_terminal(second, cfg_b) and not worker._due(second, cfg_b))

# --------------------------------------------------------------------------- #
# 3. the download paths: absent slskd, verification failure, nothing found
# --------------------------------------------------------------------------- #
print("== every give-up path of a download emits once ==")


def run_job(release=None, confirm_lossy=False, *, responses=None, candidates=None,
            verify=None, leftover=None, accept=None, **over):
    """Start a real job through start_job() and return its settled state.

    The scenario is handed in HERE (and applied after the stub reset), so the
    state each stage reports belongs to this run and not to the previous one."""
    PIPE.reset()
    if responses is not None:
        PIPE.responses = responses
    if candidates is not None:
        PIPE.candidates = candidates
    if verify is not None:
        PIPE.verify = verify
    if leftover is not None:
        PIPE.leftover = leftover
    release = release or dict(REL)
    with pipe_patches(**over), SLSK_UP:
        started = auto.start_job(release_mbid=release.get("id"), release=release,
                                 confirm_lossy=confirm_lossy)
        if not started.get("ok"):
            raise AssertionError(f"job refused: {started}")
        job_id = started["job"]["id"]
        if accept is not None:
            _wait_for(lambda: auto.job_state(job_id).get("state") == "confirm",
                      what="the prompt this job parks on")
            auto.confirm(accept, job_id)
        return _wait_job_state(job_id), job_id


# (a) slskd is not there at all.
drain()
with pipe_patches(), SLSK_DOWN:
    started = auto.start_job(release_mbid=REL["id"], release=dict(REL))
    check("a job with no slskd still starts (and reports)", bool(started.get("ok")),
          str(started))
    st = _wait_job_state(started["job"]["id"])
expect_frames("download_failed")
err = frames("download_failed")
check("an absent slskd ends the job as failed", st.get("state") == "error", str(st.get("state")))
check("...announcing it exactly ONCE", len(err) == 1, str(len(err)))
check("...with the reason the user can act on",
      bool(err) and "slskd is not running" in err[0]["body"]
      and err[0]["data"]["link"] == "/soulseek"
      and err[0]["data"]["outcome"] == "transient",
      json.dumps(err[0]["data"]) if err else "")
check("...and nothing else (never also an import_ready)",
      len(frames()) == 1, str([f["event"] for f in frames()]))

# The queue row for it: failed, with the reason and a retry.
row = next((r for r in queue()["sections"]["failed"]
            if r.get("job_id") == started["job"]["id"]), None)
check("the failed job is a row in the queue's failed section",
      row is not None and "slskd is not running" in row["reason"], json.dumps(row or {}))
check("...carrying its release id and the retry affordance",
      bool(row) and row.get("retryable") is True
      and row.get("release", {}).get("id") == REL["id"]
      and row.get("outcome") == "transient", json.dumps(row or {}))

# ...and retrying it puts the release back into the pipeline (the route hands it
# to enqueue(); no real job is started here, so nothing else is disturbed).
PIPE.started.clear()
with Patch(auto, enqueue=lambda **kw: (PIPE.started.append(kw.get("release_mbid")), 0)[1]):
    r = client.post("/api/queue/retry", json={"id": row["id"]})
check("retrying a failed job re-queues its release",
      r.status_code == 200 and r.json().get("ok") is True
      and PIPE.started == [REL["id"]], f"{r.text} {PIPE.started}")

# (b) the download arrived and failed verification: nothing is imported, and
#     the partial bytes the sweep could not free are REPORTED.
drain()
LOST = os.path.join(DL, "peer", "album", "01 - Track.flac")
st_vf, _jid = run_job(candidates=_candidate(),
                      verify=(False, ["00 - Track.flac: CRC mismatch"]),
                      leftover=[LOST])
expect_frames("download_failed")
vf = frames("download_failed")
check("a verification failure ends the job as failed",
      st_vf.get("state") == "error", str(st_vf.get("state")))
check("...announcing it exactly ONCE", len(vf) == 1, str(len(vf)))
check("...with the reason and the leftover files named",
      bool(vf) and "verification failed" in str(st_vf.get("attempts"))
      and "CRC mismatch" in str(st_vf.get("log"))
      and "partial file(s)" in vf[0]["body"]
      and vf[0]["data"]["leftovers"] == [LOST],
      json.dumps({"attempts": st_vf.get("attempts"),
                  "body": (vf[0]["body"] if vf else ""),
                  "data": (vf[0]["data"] if vf else {})}))
check("...and the leftovers are on the queue row too",
      any(LOST in (r.get("leftovers") or []) for r in queue()["sections"]["failed"]),
      json.dumps([r.get("leftovers") for r in queue()["sections"]["failed"]]))
check("...and no second frame of any other kind",
      len(frames()) == 1, str([f["event"] for f in frames()]))

# (c) nothing usable on the network at all.
drain()
st_nf, _jid = run_job(responses=[], candidates=[])
expect_frames("download_failed")
notfound = frames("download_failed")
check("a search that found nothing ends the job as failed",
      st_nf.get("state") == "error" and "No candidate folder" in str(st_nf.get("result")),
      str(st_nf.get("result")))
check("...announcing it exactly ONCE", len(notfound) == 1, str(len(notfound)))
check("...CLASSIFIED as not_found (so nothing retries it by itself)",
      bool(notfound) and notfound[0]["data"]["outcome"] == "not_found"
      and notfound[0]["data"]["link"] == "/soulseek")
check("...and the queue row says so, with its retry",
      any(r.get("outcome") == "not_found" and "Nothing found" in r["note"]
          and r.get("retryable") for r in queue()["sections"]["failed"]))

# (d) the interactive path: the same dead end PARKS on the wish offer. Accepting
#     it is one outcome (the release is now watched, not a failure); an album
#     that lands nowhere must never be reported as "ready to import".
drain()
_st, _jid = run_job(release=dict(REL), confirm_lossy=True, responses=[],
                    candidates=[], accept=True)
expect_frames("wish_not_found")
wished = frames("wish_not_found")
check("a job that parks the release in the wish list settles that way",
      _st.get("state") == "done" and (_st.get("result") or {}).get("wished"),
      json.dumps(_st.get("result") or {}))
check("...with exactly ONE dedicated frame", len(wished) == 1, str(len(wished)))
check("...and NOT a misleading import_ready", frames("import_ready") == [])
check("...carrying the wish it created",
      bool(wished) and wished[0]["data"]["wish_id"]
      and wished[0]["data"]["link"] == "/soulseek")

# (e) a declined offer: the release is NOT wished, and the job reports giving up.
drain()
OTHER = "22222222-3333-4444-5555-666666666666"
_st, _jid = run_job(release=dict(REL, id=OTHER), confirm_lossy=True,
                    responses=[], candidates=[], accept=False)
expect_frames("download_failed")
check("declining the offer ends the job as not found, once",
      _st.get("state") == "error" and len(frames("download_failed")) == 1
      and frames("wish_not_found") == [], str([f["event"] for f in frames()]))

# (f) the user's own cancel says nothing.
drain()
with pipe_patches(), SLSK_UP:
    started = auto.start_job(release_mbid=REL["id"], release=dict(REL),
                             confirm_lossy=True)
    job_id = started["job"]["id"]
    _wait_for(lambda: auto.job_state(job_id).get("state") == "confirm",
              what="the wish offer")
    auto.cancel(job_id)
    _wait_job_state(job_id, states=("cancelled",))
time.sleep(0.4)   # a frame, if one were coming, is asynchronous
check("a cancelled job says nothing", frames() == [], str([f["event"] for f in frames()]))

# --------------------------------------------------------------------------- #
# 4. a stalled album: the queue shows it, names what is missing, and offers the
#    wizard link and the dismiss
# --------------------------------------------------------------------------- #
print("== a stalled album in the queue ==")

ALBUM = os.path.join(MUSIC, "Artists", "An Artist", "An Album")
os.makedirs(ALBUM, exist_ok=True)
MISSING = {
    "cover": {"id": "cover", "label": "Cover art", "state": "unsourced",
              "fields": ["cover art"], "codes": ["COVER"], "note": ""},
    "genres": {"id": "genres", "label": "Genres", "state": "unsourced",
               "fields": ["genre"], "codes": ["GENRE"], "note": ""},
}
drain()
autonomy.raise_prompt(ALBUM, dict(CFG), MISSING, mode="automatic", reason="missing")
prompts = [r for r in queue()["sections"]["completed"] if r["kind"] == "prompt"]
check("an album a finished import is short of is a row in the queue's Completed",
      len(prompts) == 1,
      str([r["kind"] for r in queue()["sections"]["completed"]]))
check("...and never a row in Needs you: nothing about it waits (spec R166)",
      not [r for r in queue()["sections"]["needs_attention"] if r["kind"] == "prompt"],
      str([r["kind"] for r in queue()["sections"]["needs_attention"]]))
row = prompts[0] if prompts else {}
check("...naming the families that are missing, in wizard order",
      row.get("missing") == ["cover", "genres"]
      and row.get("missing_labels") == ["Cover art", "Genres"],
      json.dumps({"missing": row.get("missing"), "labels": row.get("missing_labels")}))
check("...linking to the wizard AT the first missing family, with the whole list",
      "album=" + quote(row.get("album_path", ""), safe="") in (row.get("action_link") or "")
      and "step=Covers" in (row.get("action_link") or "")
      and "missing=cover,genres" in (row.get("action_link") or ""),
      str(row.get("action_link")))
check("...offering manual entry and the dismiss, and nothing to cancel",
      row.get("action") == "manual" and row.get("dismissable") is True
      and row.get("cancelable") is False, json.dumps(row))
check("...and saying in the row itself that the album is in the library",
      (row.get("needs") or {}).get("waiting") is False
      and (row.get("needs") or {}).get("reason") == "missing",
      json.dumps(row.get("needs")))
check("...and exactly one import_needs_data frame went out",
      len(frames("import_needs_data")) == 1, str(len(frames("import_needs_data"))))
check("...whose link is the same manual-completion link",
      frames("import_needs_data")[0]["data"]["link"] == row.get("action_link"))

# The same gap raised again is not a second notification.
autonomy.raise_prompt(ALBUM, dict(CFG), MISSING, mode="automatic", reason="missing")
check("re-raising the same gap does not repeat the news",
      len(frames("import_needs_data")) == 1, str(len(frames("import_needs_data"))))

# Cancelling a prompt is refused with the two actions it really has.
r = client.post("/api/queue/cancel", json={"id": row["id"]})
check("a prompt cannot be cancelled — the answer says what to do instead",
      r.status_code == 409 and "dismiss" in r.text, r.text)

# Dismiss: the row goes, and a later import re-raises it.
r = client.post("/api/import/prompts/dismiss", json={"path": ALBUM})
check("dismissing the prompt answers ok", r.status_code == 200 and r.json().get("ok") is True,
      r.text)
check("...and the row is gone from the queue",
      not [x for x in queue()["sections"]["completed"] if x["kind"] == "prompt"],
      str(queue()["counts"]))
drain()
autonomy.raise_prompt(ALBUM, dict(CFG), MISSING, mode="automatic", reason="missing")
again = [x for x in queue()["sections"]["completed"] if x["kind"] == "prompt"]
check("a later import of the same album raises the prompt again",
      len(again) == 1 and len(frames("import_needs_data")) == 1)

# A wish that stalled shows in the same section, with the reason and a retry.
wish3 = wishes.add_wish("33333333-4444-5555-6666-777777777777",
                        title="Stalled Album", artist="An Artist", source="soulseek")
wishes.mark_not_found(wish3["id"], "No candidate folder contained every track", 3)
row3 = next((x for x in queue()["sections"]["needs_attention"]
             if x.get("wish_id") == wish3["id"]), None)
check("a stalled wish shares the needs-you section, with its reason",
      row3 is not None and "Nothing found" in row3["note"]
      and "No candidate folder" in row3["reason"], json.dumps(row3 or {}))
check("...and is retryable from the queue", bool(row3) and row3.get("retryable") is True)
with Patch(worker, trigger=lambda wid=None: {"ok": True}):
    r = client.post("/api/queue/retry", json={"id": row3["id"]})
check("retrying it re-arms the wish", r.status_code == 200
      and wishes.get_wish(wish3["id"])["status"] == "wanted"
      and wishes.get_wish(wish3["id"])["not_found"] == 0, r.text)

# A FRAMEWORK album (the "Add to library" / watch seam) whose acquisition
# stalls must stay pending and still be listed — never a half-tagged "complete"
# album, and never a row that vanishes because nothing arrived.
from server import pending_albums  # noqa: E402
from mlo.paths import load_pending  # noqa: E402

framework_mbid = "44444444-5555-6666-7777-888888888888"
fw = pending_albums.create(dict(REL, id=framework_mbid), dict(CFG),
                           source="musicbrainz", cover=False)
drain()
wishes.mark_not_found(fw["wish_id"], "No candidate folder contained every track", 3)
frow = next((x for x in queue()["sections"]["needs_attention"]
             if x.get("wish_id") == fw["wish_id"]), None)
check("a framework album that stalls is a needs-you row",
      frow is not None and frow.get("pending") is True, json.dumps(frow or {}))
check("...its folder is still there and still PENDING (nothing invented)",
      os.path.isdir(fw["album_path"]) and bool(load_pending(fw["album_path"])),
      str(load_pending(fw["album_path"])))
check("...and it was announced once, as a not-found wish",
      len(frames("wish_not_found")) == 1, str([f["event"] for f in frames()]))


# --------------------------------------------------------------------------- #
# 5. nothing ends silently: every terminal row in the queue is either a state
#    the user can act on or has already been announced.
# --------------------------------------------------------------------------- #
print("== the queue never shows a dead end ==")

needs = queue()["sections"]["needs_attention"]
check("every needs-you row carries something to do",
      all(r.get("missing") or r.get("retryable") or r.get("dismissable")
          or r.get("cancelable") or r.get("action_link") for r in needs),
      json.dumps([{r["id"]: {"retryable": r.get("retryable"), "missing": r.get("missing"),
                             "cancelable": r.get("cancelable")}} for r in needs]))
check("...and nothing in the failed section is silently stuck",
      all(r.get("reason") or r.get("note") for r in queue()["sections"]["failed"]),
      json.dumps([r["id"] for r in queue()["sections"]["failed"]]))

# --------------------------------------------------------------------------- #
# 6. the page itself: the stalled row renders what a user can act on.
#
# Rendered for REAL through Vite (the same in-memory SSR harness the repo uses
# for the queue view — tools/check_queue_view.mjs), because "the row offers
# manual entry" is a claim about the browser, not about the payload. Node or
# web/node_modules missing -> skipped LOUDLY, like the other render checks.
# --------------------------------------------------------------------------- #
print("== the Soulseek queue row renders ===")

UI_PAYLOAD = {
    "sections": {
        "queued": [],
        "in_progress": [],
        "needs_attention": [
            {"id": "job:9", "kind": "job", "job_id": 9, "wish_id": None,
             "stage": "needs_attention", "source_key": "soulseek", "source": "Soulseek",
             "title": "Parked Album", "artist": "An Artist", "release_mbid": "",
             "album_path": "", "progress": None, "action": "answer",
             "action_link": "/soulseek?tab=auto", "reason": "Only lossy copies found",
             "note": "Only lossy copies found — waiting for your go-ahead",
             "created_at": 1.0, "updated_at": 1.0, "cancelable": True, "log_tail": []},
            {"id": "wish:5", "kind": "wish", "job_id": None, "wish_id": 5,
             "stage": "needs_attention", "source_key": "musicbrainz",
             "source": "MusicBrainz", "title": "Rare Album", "artist": "An Artist",
             "release_mbid": "", "album_path": "", "progress": None,
             "retryable": True, "not_found": 3,
             "reason": "No candidate folder contained every track",
             "note": "Nothing found — not searched again unless you retry it",
             "created_at": 1.0, "updated_at": 1.0, "cancelable": True, "log_tail": []},
        ],
        # An import that finished short of a family: a FINISHED row carrying the
        # warning, exactly as `build_queue` now emits it (spec R166) — the row is
        # in Completed and nothing about it waits.
        "completed": [
            {"id": "wish:4", "kind": "wish", "job_id": None, "wish_id": 4,
             "stage": "completed", "source_key": "soulseek", "source": "Soulseek",
             "title": "An Album", "artist": "An Artist", "release_mbid": "",
             "album_path": ALBUM, "progress": None, "clearable": True,
             "missing": ["cover", "genres"],
             "missing_labels": ["Cover art", "Genres"],
             "needs": {"families": ["cover", "genres"], "labels": ["Cover art", "Genres"],
                       "link": "/import?album=x&step=Covers&missing=cover,genres",
                       "detail": "Cover art — no source could supply it (cover art); "
                                 "Genres — no source could supply it (genre)",
                       "reason": "missing", "mode": "automatic", "waiting": False},
             "wizard_link": "/import?album=x&step=Covers&missing=cover,genres",
             "action": "manual", "action_link": "/import?album=x&step=Covers&missing=cover,genres",
             "dismissable": True, "retryable": False,
             "reason": "", "note": "Imported into the library",
             "created_at": 1.0, "updated_at": 1.0, "cancelable": False, "log_tail": []},
        ],
        "failed": [
            {"id": "job:8", "kind": "job", "job_id": 8, "wish_id": None,
             "stage": "failed", "source_key": "soulseek", "source": "Soulseek",
             "title": "Broken Album", "artist": "An Artist", "release_mbid": "",
             "album_path": "", "progress": None, "outcome": "not_found",
             "retryable": True, "leftovers": [os.path.join(DL, "peer", "01.flac")],
             "reason": "Every candidate was rejected (2 attempt(s) — see the log).",
             "note": "Nothing found — retry it by hand when the network has it",
             "created_at": 1.0, "updated_at": 1.0, "cancelable": False, "log_tail": []},
        ],
    },
    "counts": {"queued": 0, "in_progress": 0, "needs_attention": 2,
               "completed": 1, "failed": 1, "total": 4},
    "running": 0, "concurrency": 3, "download_slots": 3,
}

_payload_path = os.path.join(REDIRECT, "outcomes-payload.json")
with open(_payload_path, "w", encoding="utf-8") as f:
    json.dump(UI_PAYLOAD, f)

_harness = os.path.join(REDIRECT, "check_outcomes_view.mjs")
with open(_harness, "w", encoding="utf-8") as f:
    f.write(r'''
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(process.env.MLO_ROOT, "web");
const payloadPath = process.argv[2];
if (!existsSync(path.join(webDir, "node_modules"))) {
  console.error("[outcomes] web/node_modules is missing");
  process.exit(2);
}
const webRequire = createRequire(path.join(webDir, "package.json"));
const { createServer } = await import(
  `file://${path.join(webDir, "node_modules/vite/dist/node/index.js").replace(/\\/g, "/")}`);
const React = webRequire("react");
const { renderToString } = webRequire("react-dom/server");
const { MemoryRouter } = webRequire("react-router-dom");
const { QueryClient, QueryClientProvider } = await import(pathToFileURL(
  path.join(webDir, "node_modules/@tanstack/react-query/build/modern/index.js")).href);

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
  clear: () => store.clear(),
};
globalThis.window = globalThis;

const payload = JSON.parse(readFileSync(payloadPath, "utf8"));
const server = await createServer({
  configFile: path.join(webDir, "vite.config.ts"),
  root: webDir, server: { middlewareMode: true }, appType: "custom", logLevel: "error",
});
try {
  const page = await server.ssrLoadModule("/src/pages/SoulseekPage.tsx");
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["soulseekQueue"], payload);
  const html = renderToString(
    React.createElement(QueryClientProvider, { client: qc },
      React.createElement(MemoryRouter, { initialEntries: ["/soulseek"] },
        React.createElement(page.default)))
  );
  const flat = html.replace(/<!-- -->/g, "").replace(/\s+/g, " ");
  const want = [
    ["the finished album's warning", "Needs data: Cover art, Genres"],
    ["the manual-entry button", "Enter manually"],
    ["the dismiss button", "Mark complete"],
    ["a parked question's own action", "Answer"],
    ["the failure's reason", "Every candidate was rejected"],
    ["the retry on a terminal row", "not retried automatically"],
    ["the partial bytes named", "partial file(s) left in the download folder"],
    ["what a not-found wish's state means", "not searched again unless you retry it"],
    ["the import source chip", "Import"],
  ];
  const missing = want.filter(([, text]) => !flat.includes(text));
  if (missing.length) {
    console.error("[outcomes] MISSING: " + JSON.stringify(missing.map(([what]) => what)));
    console.error(flat.replace(/></g, ">\n<").split("\n")
      .filter((l) => /chip|Wand|Enter|Missing/.test(l)).slice(0, 25).join("\n"));
    process.exit(1);
  }
  console.log(`ok  the stalled row renders its families and both actions ` +
              `(${want.length} checks, ${html.length} bytes)`);
} finally {
  await server.close();
}
''')

_ui = subprocess.run(["node", _harness, _payload_path],
                     cwd=ROOT, capture_output=True, text=True,
                     env=dict(os.environ, MLO_ROOT=ROOT))
if _ui.returncode == 0:
    print("  ok   " + _ui.stdout.strip().removeprefix("ok  "))
elif _ui.returncode == 2:
    print("  SKIP the page render check (no node or web/node_modules): "
          + (_ui.stderr.strip().splitlines() or [""])[0])
else:
    check("the queue row renders what the user can act on", False,
          (_ui.stdout + _ui.stderr)[-1200:])

shutil.rmtree(REDIRECT, ignore_errors=True)
print(f"\n{len(FAILED)} failure(s)")
sys.exit(1 if FAILED else 0)