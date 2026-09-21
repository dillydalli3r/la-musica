#!/usr/bin/env python3
"""The emit sites behind the notification tray — one notification per outcome.

Every outcome the user asked to hear about is announced on the ONE bus
(`server/events.py`) and the client's tray turns each frame into exactly one
entry (tools/test_notifications.cjs). This suite covers the server half for the
sites that belong to this task:

* a finished import run (`server/import_queue.py`) — success and failure, each
  announced ONCE, carrying the route the tray should open;
* a finished script run (`server/main.py` `/api/run`) — the grader's own kind
  for a grade, `script_failed` when a script errored, `script_done` otherwise,
  and NOTHING for a run where every script was skipped (nothing happened);
* a newer release (`server/version.py`) — announced once per version, not once
  per check.

The client's own contract (payload shape, click targets, the badge, clear all)
lives in tools/test_notifications.cjs; this file is only about what the server
publishes.
"""
import os
import sys
import tempfile
import time
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{('  — ' + detail) if detail and not ok else ''}")
    if not ok:
        FAILED.append(name)


from server import events as events_mod  # noqa: E402
from server import import_queue  # noqa: E402
from server import version as version_mod  # noqa: E402
from server import main as main_mod  # noqa: E402


def drain():
    """Forget every frame so far, so a count is about THIS outcome."""
    with events_mod._lock:
        events_mod._events.clear()


def frames(kind=None):
    kept = events_mod.recent(0, limit=10 ** 6)
    return [e for e in kept if kind is None or e.get("event") == kind]


def shape_ok(frame, kind):
    """The payload the tray needs: kind, wording, a timestamp, the server's own
    event number, and a link the client can navigate to."""
    link = (frame.get("data") or {}).get("link")
    return (
        frame.get("event") == kind
        and bool(frame.get("title"))
        and isinstance(frame.get("body"), str)
        and float(frame.get("at") or 0) > 0
        and int(frame.get("seq") or 0) > 0
        and isinstance(link, str)
        and link.startswith("/")
    )


print("== a finished import run ==")
# The runner calls the injected importer (server.main wires the real one); a
# stub is what makes this a unit of the RUN's own behaviour.
import_queue.set_importer(lambda path: {"path": path, "album_root": path + "/linked",
                                        "errors": []})
import_queue.set_ready_provider(lambda: [])


def run_import(paths, importer):
    import_queue.set_importer(importer)
    drain()
    started = import_queue.start(paths=list(paths))
    if not started.get("ok"):
        return started
    deadline = time.time() + 30
    while import_queue.status()["state"] == "running" and time.time() < deadline:
        time.sleep(0.02)
    return import_queue.status()


run_import([r"F:\dl\One"], lambda p: {"path": p, "album_root": r"F:\Music\A\One", "errors": []})
one = frames("download_done")
check("one album imported -> exactly one frame", len(one) == 1)
check("...with the payload shape", shape_ok(one[0], "download_done") if one else False)
check("...linking to the album itself",
      bool(one) and one[0]["data"]["link"] == "/album/" + quote(r"F:\Music\A\One", safe=""),
      (one[0]["data"] if one else {}).__str__())

run_import([r"F:\dl\One", r"F:\dl\Two"],
           lambda p: {"path": p, "album_root": p + "-in", "errors": []})
many = frames("download_done")
check("several albums imported -> exactly one frame", len(many) == 1)
check("...pointing at the library, not at one album",
      bool(many) and many[0]["data"]["link"] == "/library" and many[0]["data"]["imported"] == 2)

run_import([r"F:\dl\Broken"], lambda p: {"path": p, "errors": ["tagging failed"]})
bad = frames("download_done")
check("a failed run -> exactly one frame", len(bad) == 1)
check("...carrying the error and the wizard as its link",
      bool(bad) and bad[0]["data"]["link"] == "/import" and bad[0]["data"]["errors"])

# One run, one frame — never one per album. The runs above each cleared the
# ring first, so this total is what the LAST outcome published.
check("an import run publishes the frame and nothing else", len(frames()) == 1)

print("== a wish that gives up ==")
from server import wishes  # noqa: E402

wish_dir = tempfile.mkdtemp(prefix="mlo-wish-")
wishes.db_path = lambda: os.path.join(wish_dir, "wishes.db")
wishes._initialized = False
wish = wishes.add_wish("00000000-0000-0000-0000-000000000001",
                       title="An Album", artist="An Artist", source="soulseek")
drain()
wishes.mark_wanted(wish["id"], error="no verified match yet", attempts=1)
check("a wish that is only retried says nothing", frames() == [])
wishes.mark_failed(wish["id"], "no verified match after 3 searches", 3)
wish_failed = frames("wish_failed")
check("a wish that gave up -> exactly one frame", len(wish_failed) == 1)
check("...with the payload shape", shape_ok(wish_failed[0], "wish_failed") if wish_failed else False)
check("...naming the wish and pointing at the queue",
      bool(wish_failed) and wish_failed[0]["data"]["wish_id"] == wish["id"]
      and wish_failed[0]["data"]["link"] == "/soulseek")
check("...and the wish really is failed", wishes.get_wish(wish["id"])["status"] == "failed")
wishes.mark_failed(wish["id"], "no verified match after 3 searches", 3)
check("re-marking a failed wish does not repeat the news", len(frames("wish_failed")) == 1)
wishes.mark_wanted(wish["id"], error="retry", attempts=4)
wishes.mark_failed(wish["id"], "still nothing", 4)
check("a fresh give-up after a retry is a new outcome", len(frames("wish_failed")) == 2)
wishes.mark_failed(9999, "gone", 1)
check("marking a wish that does not exist announces nothing", len(frames("wish_failed")) == 2)

print("== an import that needs a human decision ==")
from server import import_autonomy  # noqa: E402
from mlo import paths as mlo_paths  # noqa: E402

data_dir = tempfile.mkdtemp(prefix="mlo-autonomy-")
mlo_paths.app_data_dir = lambda *a, **k: data_dir
album_dir = os.path.join(tempfile.mkdtemp(prefix="mlo-album-"), "An Album")
os.makedirs(album_dir, exist_ok=True)
missing = {"cover": {"id": "cover", "label": "Covers", "state": "source",
                     "fields": ["cover"], "note": ""}}
drain()
entry = import_autonomy.raise_prompt(album_dir, {}, missing, mode="automatic", reason="missing")
needs = frames("import_needs_data")
check("an album parked on a decision -> exactly one frame", len(needs) == 1)
check("...with the payload shape", shape_ok(needs[0], "import_needs_data") if needs else False)
check("...carrying the wizard link and the reason",
      bool(needs) and needs[0]["data"]["link"].startswith("/import?album=")
      and needs[0]["data"]["reason"] == "missing"
      and needs[0]["data"]["families"] == ["cover"])
check("...and the wizard can list the prompt",
      [e.get("album") for e in import_autonomy.prompts()] == [entry["album"].replace("\\", "/")])
check("...the tray's click target is the route the app mounts",
      bool(needs) and needs[0]["data"]["link"].split("?")[0] == "/import")
import_autonomy.raise_prompt(album_dir, {}, {}, mode="automatic", reason="missing")
check("an import that resolved the gap withdraws the prompt silently", len(frames("import_needs_data")) == 1)
check("...and the prompt is gone", import_autonomy.for_album(album_dir) == {})

print("== a finished script run (/api/run) ==")
import server.script_runners as script_runners  # noqa: E402

_real_chain = script_runners.run_chain
try:
    def stub(ids_results):
        def fake(cfg, ids, targets=None, force=None, progress=None, wait=False, timeout=None):
            script_runners._last_ids = list(ids)
            return [dict(r) for r in ids_results]
        script_runners.run_chain = fake

    stub([{"id": 4, "label": "Grade", "stats": {"grade_dist": {"PASS": 12, "FAIL": 1}}}])
    drain()
    main_mod._run_scripts(main_mod.RunRequest(ids=[4]))
    graded = frames("grade_done")
    check("a grade run -> exactly one grade_done", len(graded) == 1)
    check("...with the payload shape", shape_ok(graded[0], "grade_done") if graded else False)
    check("...reporting the pass/fail split and pointing at the library",
          bool(graded) and graded[0]["data"]["grade_dist"] == {"PASS": 12, "FAIL": 1}
          and graded[0]["data"]["link"] == "/library")
    check("...and no other kind", len(frames()) == 1)

    # A targeted grade is about ONE album: the notification opens that album.
    folder = str(main_mod.load_config().get("music_folder") or "")
    target = os.path.join(folder, "Some Album") if folder and os.path.isdir(folder) else r"F:\Music\A\One"
    stub([{"id": 4, "label": "Grade", "stats": {"grade_dist": {"PASS": 3, "FAIL": 2}}}])
    drain()
    main_mod._run_scripts(main_mod.RunRequest(ids=[4], targets=[target]))
    targeted = frames("grade_done")
    want = "/album/" + quote(os.path.normpath(target), safe="")
    check("a targeted grade links to the album it graded",
          len(targeted) == 1 and targeted[0]["data"]["link"] == want,
          (targeted[0]["data"] if targeted else {}).__str__())

    stub([{"id": 1, "label": "Format lyrics", "stats": {}},
          {"id": 3, "label": "Optimize FLACs", "stats": {}}])
    drain()
    main_mod._run_scripts(main_mod.RunRequest(ids=[1, 3]))
    done = frames("script_done")
    check("a successful chain -> exactly one script_done", len(done) == 1)
    check("...listing what ran", bool(done) and done[0]["data"]["ran"] == [1, 3])
    check("...linking to the in-progress view", bool(done) and done[0]["data"]["link"] == "/in-progress")

    stub([{"id": 8, "label": "Auto tagging", "error": "beets is not installed"}])
    drain()
    main_mod._run_scripts(main_mod.RunRequest(ids=[8]))
    failed = frames("script_failed")
    check("an errored run -> exactly one script_failed", len(failed) == 1)
    check("...naming the failure", bool(failed) and "beets" in failed[0]["body"]
          and failed[0]["data"]["failed"] == [8])
    check("...and never also a script_done", len(frames()) == 1)

    stub([{"id": 9, "label": "AccurateRip", "skipped": True, "reason": "grade_check_audit is off"},
          {"id": 16, "label": "Mood & Energy", "skipped": True, "reason": "off"}])
    drain()
    main_mod._run_scripts(main_mod.RunRequest(ids=[9, 16]))
    check("a run where nothing ran says nothing", frames() == [])
finally:
    script_runners.run_chain = _real_chain

print("== a newer release ==")
tmp = os.path.join(tempfile.mkdtemp(prefix="mlo-notify-"), "update_check.json")
version_mod.cache_path = lambda: tmp
version_mod._fetch_latest = lambda: {"latest": "99.0.0", "release_url": "https://example.invalid/notes"}
drain()
first = version_mod.check(force=True)
upd = frames("update_available")
check("a fresh check that finds a release announces it once", len(upd) == 1)
check("...with the payload shape", shape_ok(upd[0], "update_available") if upd else False)
check("...naming both versions and the release page",
      bool(upd) and upd[0]["data"]["latest"] == "99.0.0" and upd[0]["data"]["url"]
      and upd[0]["data"]["link"] == "/settings")
check("...and the answer still reports the update", first.get("update_available") is True)
version_mod.check(force=True)
check("a second check does not repeat the news", len(frames("update_available")) == 1)
version_mod._fetch_latest = lambda: {"latest": "100.0.0", "release_url": ""}
version_mod.check(force=True)
check("a NEWER release is announced again", len(frames("update_available")) == 2)

print(f"\n{len(FAILED)} failure(s)")
sys.exit(1 if FAILED else 0)
