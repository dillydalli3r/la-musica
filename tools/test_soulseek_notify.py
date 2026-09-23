#!/usr/bin/env python3
"""The two Soulseek lifecycle notifications: a download that starts moving and
our files starting to be shared.

Every other frame about a Soulseek job is an OUTCOME (download_done /
download_failed / import_ready / wish_not_found). These two say the job BEGAN,
which nothing said before: a transfer sitting in a peer's queue for an hour
looked exactly like one that was downloading, and a peer taking files FROM us
showed up nowhere at all.

Both rules are per SESSION, never per poll — the download wait ticks once a
second and the uploads watcher every five — so what this asserts is that the
same state never announces itself twice, and that going quiet re-arms it.

Run: python tools/test_soulseek_notify.py
"""
import os
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{('  — ' + detail) if detail and not ok else ''}")
    if not ok:
        FAILED.append(name)


from mlo.config import DEFAULT_CONFIG  # noqa: E402
from server import events as events_mod  # noqa: E402
from server import main as main_mod  # noqa: E402
from server import soulseek as slsk  # noqa: E402
from server import soulseek_auto as auto  # noqa: E402


def drain():
    """Forget every frame so far, so a count is about THIS outcome."""
    with events_mod._lock:
        events_mod._events.clear()
    try:  # the durable log as well: recent() reads both (spec R216)
        os.remove(events_mod._event_log_path())
    except OSError:
        pass


def frames(kind=None):
    kept = events_mod.recent(0, limit=10 ** 6)
    return [e for e in kept if kind is None or e.get("event") == kind]


def uploads(*users):
    """slskd's upload tree, one entry per (username, [transfer states])."""
    return [{"username": user,
             "directories": [{"files": [{"state": s} for s in states]}]}
            for user, states in users]


print("== our files starting to be shared: the dedupe on its own ==")
# Pure: no slskd, no clock, no event bus — the rule the watcher loop only feeds.
state, started = slsk.upload_start_frames({}, uploads(("peer", ["InProgress"])))
check("a peer that just started taking a file -> one frame",
      started == [{"username": "peer", "files": 1}], str(started))
check("...and the state remembers the session", state == {"peer": 1}, str(state))

state, started = slsk.upload_start_frames(state, uploads(("peer", ["InProgress"])))
check("the next poll of the same transfer says nothing", started == [], str(started))
state, started = slsk.upload_start_frames(
    state, uploads(("peer", ["InProgress", "InProgress"])))
check("a second file joining that session says nothing either", started == [], str(started))
check("...but the count follows it", state == {"peer": 2}, str(state))

state, started = slsk.upload_start_frames(
    {}, uploads(("peer", ["Completed, Succeeded"])))
check("a transfer already finished at first sight says nothing", started == [], str(started))
check("...and is not recorded as an active session", state == {}, str(state))
_, started = slsk.upload_start_frames({}, uploads(("peer", ["Completed, Errored"])))
check("a failed transfer says nothing either", started == [], str(started))
_, started = slsk.upload_start_frames({}, uploads(("peer", ["Completed, Cancelled"])))
check("a cancelled transfer says nothing either", started == [], str(started))

state, started = slsk.upload_start_frames(state, uploads(("peer", ["Completed, Succeeded"])))
check("a peer that went quiet is forgotten", state == {} and started == [], str(started))
_, started = slsk.upload_start_frames(state, uploads(("peer", ["InProgress"])))
check("...so their NEXT download is announced again",
      started == [{"username": "peer", "files": 1}], str(started))

_, started = slsk.upload_start_frames(
    {}, uploads(("a", ["Completed, Succeeded", "InProgress"]), ("b", ["Queued"])))
check("every peer is counted on its own",
      [f["username"] for f in started] == ["a", "b"], str(started))
check("...counting only what is still in flight",
      [f["files"] for f in started] == [1, 1], str(started))

print("== the two switches ==")
check("both kinds are published by default",
      events_mod._notify_configured("download_started", DEFAULT_CONFIG) is True
      and events_mod._notify_configured("upload_started", DEFAULT_CONFIG) is True)
check("both keys ship in DEFAULT_CONFIG, on",
      DEFAULT_CONFIG.get("notify_soulseek_download_start") is True
      and DEFAULT_CONFIG.get("notify_soulseek_upload_start") is True)
check("switching the download kind off silences it",
      events_mod._notify_configured(
          "download_started", {"notify_soulseek_download_start": False}) is False)
check("switching the upload kind off silences it",
      events_mod._notify_configured(
          "upload_started", {"notify_soulseek_upload_start": False}) is False)
check("the existing switches still silence their own kinds",
      events_mod._notify_configured("wish_found", {"notify_wish_found": False}) is False
      and events_mod._notify_configured("download_done",
                                        {"notify_download_done": False}) is False
      and events_mod._notify_configured("import_ready",
                                        {"notify_import_ready": False}) is False)
drain()
events_mod.emit("upload_started", "kept back", config={"notify_soulseek_upload_start": False})
check("...and the frame really never reaches the wire", frames() == [], str(frames()))
drain()
events_mod.emit("download_started", "kept back", config={"notify_soulseek_download_start": False})
check("...for the download kind too", frames() == [], str(frames()))

print("== the uploads watcher loop ==")
main_mod._ULSK_STATE = {}
drain()
real = (slsk.is_running, slsk.web_up, slsk.uploads_state)
polled = []
try:
    slsk.is_running = lambda: True
    slsk.web_up = lambda cfg=None: False

    def one_state(state):
        polled.append(state)
        return uploads(("peer", [state]))

    slsk.uploads_state = lambda cfg=None: one_state("InProgress")
    main_mod._soulseek_uploads_check(cfg={})
    check("a pass that sees a new peer emits one frame",
          len(frames("upload_started")) == 1, str(len(frames("upload_started"))))
    main_mod._soulseek_uploads_check(cfg={})
    main_mod._soulseek_uploads_check(cfg={})
    check("later passes while the same transfer runs stay silent",
          len(frames("upload_started")) == 1, str(len(frames("upload_started"))))
    check("...even though the daemon really was polled each time",
          len(polled) == 3, str(len(polled)))

    slsk.uploads_state = lambda cfg=None: one_state("Completed, Succeeded")
    main_mod._soulseek_uploads_check(cfg={})
    slsk.uploads_state = lambda cfg=None: one_state("InProgress")
    main_mod._soulseek_uploads_check(cfg={})
    check("a new session after the peer went quiet is announced again",
          len(frames("upload_started")) == 2, str(len(frames("upload_started"))))
    last = frames("upload_started")[-1]
    check("...with the wording the tray shows",
          last["title"] == "Sharing started: 1 file(s)"
          and last["body"] == "peer is downloading from you", str(last))
    check("...naming the peer, the count and the queue page",
          last["data"] == {"link": "/soulseek", "username": "peer", "files": 1},
          str(last["data"]))

    polled.clear()
    slsk.is_running = lambda: False
    main_mod._soulseek_uploads_check(cfg={})
    check("a daemon that is not running is never polled", polled == [], str(polled))
finally:
    slsk.is_running, slsk.web_up, slsk.uploads_state = real
    main_mod._ULSK_STATE = {}

print("== a download whose first bytes move ==")


class FakeSlsk:
    """The slskd surface _wait_for_files touches: the transfer tree it reads
    every tick, and the cancel it fires for the files it gave up on."""

    def __init__(self, tree):
        self.tree = tree
        self.ticks = 0
        self.cancelled = []

    def downloads_state(self, cfg=None):
        self.ticks += 1
        return self.tree

    def cancel_downloads(self, username, ids, cfg=None, failed=None):
        self.cancelled.append((username, list(ids)))
        return True


def transfer(state, filename="/music/a.flac", size=100):
    return [{"username": "peer", "directories": [{"files": [{
        "id": "t1", "filename": filename, "state": state,
        "bytesTransferred": 0, "size": size, "percentComplete": 0,
        "averageSpeed": 0, "remainingTime": "00:01:00"}]}]}]


class _Claim:
    path = r"F:\Music\An Artist - An Album"


# A real job, registered the way start() registers one, so the frame is
# composed from the job's own label and claim (nothing is stubbed out).
auto._tl.jid = 4242
auto._jobs[4242] = dict(auto._IDLE_JOB)
auto._jobs[4242].update({"id": 4242, "state": "running",
                         "label": "An Artist — An Album", "log": [], "attempts": [],
                         "_event": threading.Event(), "_answer": {"accept": False},
                         "_claim": _Claim()})
real_poll = auto._TRANSFER_POLL_S
auto._TRANSFER_POLL_S = 0.01    # the loop's own cadence: many ticks per second
ddir = tempfile.mkdtemp(prefix="mlo-dl-")
try:
    drain()
    slskd = FakeSlsk(transfer("InProgress"))
    got = auto._wait_for_files(slskd, ddir, "peer",
                               [{"filename": "/music/a.flac", "size": 100}],
                               timeout_s=0.3)
    started = frames("download_started")
    check("a candidate whose transfer is in flight -> exactly one frame",
          len(started) == 1, str(len(started)))
    check("...even though the poll loop ran many times", slskd.ticks > 5, str(slskd.ticks))
    if started:
        frame = started[0]
        check("...named after the job's release",
              frame["title"] == "Download started: An Artist — An Album", frame["title"])
        check("...saying how many files and from whom",
              frame["body"] == "1 file(s) from peer", frame["body"])
        check("...pointing at the queue, with the album folder the job claimed",
              frame["data"] == {"link": "/soulseek", "username": "peer", "files": 1,
                                "album_path": r"F:\Music\An Artist - An Album"},
              str(frame["data"]))
    check("...and nothing was accepted on disk", got == {}, str(got))
    check("...and the peer's queued transfers were dropped", slskd.cancelled == [("peer", ["t1"])],
          str(slskd.cancelled))

    # The same candidate fetched in two waits (the CD .log gate, then the
    # album) is ONE download: only the first wait that sees bytes may speak,
    # and it speaks for the whole candidate, not for its own wait.
    drain()
    announce = auto._start_once(12)
    announce("peer", 1)
    announce("peer", 12)
    started = frames("download_started")
    check("two waits over one candidate announce it once",
          len(started) == 1, str(len(started)))
    check("...with the candidate's own file count, not the gate's",
          bool(started) and started[0]["body"] == "12 file(s) from peer",
          started[0]["body"] if started else "")

    # A transfer that was already over when we first looked is never news.
    drain()
    done = FakeSlsk(transfer("Completed, Succeeded"))
    auto._wait_for_files(done, ddir, "peer",
                         [{"filename": "/music/a.flac", "size": 100}], timeout_s=0.3)
    check("a transfer already finished at first sight says nothing",
          frames("download_started") == [], str(frames("download_started")))

    drain()
    dead = FakeSlsk(transfer("Completed, Errored"))
    auto._wait_for_files(dead, ddir, "peer",
                         [{"filename": "/music/a.flac", "size": 100}], timeout_s=0.3)
    check("a transfer already failed at first sight says nothing",
          frames("download_started") == [], str(frames("download_started")))
finally:
    auto._TRANSFER_POLL_S = real_poll
    auto._jobs.pop(4242, None)
    auto._tl.jid = None

print(f"\n{len(FAILED)} failure(s)")
sys.exit(1 if FAILED else 0)
