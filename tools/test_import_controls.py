#!/usr/bin/env python3
"""Verify the two acquisition switches, at the seam each one owns.

`auto_acquisition_enabled` is the master switch over what the app does on its
own — the wishes worker searching, an artist watch queueing a new release, an
"Add to library" request starting the download — and `manual_import_enabled` is
the switch over the path the USER drives (the wizard and the importing
`POST /api/import/*` routes). Both default on, which is how the app has always
behaved, and that is the no-regression half of this test: with both on, the
same calls succeed.

Nothing here touches the network. The pipeline's remote seams (the release
resolver, the wish worker's slskd job, MusicBrainz's browse, the "Add to
library" helper) are stubbed, the music folder is a temp directory and the two
SQLite stores (wishes, watches) are redirected into it, so the developer's
library is never opened.

Pinned here, one case each:
  * manual importing off — the importing routes answer 409 with a sentence
    naming the setting, while the routes that only read (the prompts list, the
    script preview, the bulk job's status) and the surfaces that are not the
    manual path (Add to library) keep answering;
  * auto acquisition off — a wish pass searches nothing and says why, the wish
    itself is left exactly as the user left it, and the user's own "Search now"
    on one wish still searches (that is them acting, not the app);
  * auto acquisition off — a watch check queues nothing, browses nothing and
    says why, and keeps its interval so turning the switch back on checks it
    then rather than a day later;
  * auto acquisition off — Add to library records the album and its wish but
    starts no download, and says so in `note`;
  * both on — every one of those does what it always did.

The wizard's own half (its minimum-entry mode and the switch's refusal, both
rendered from the real component) is the last section, through
tools/check_import_minimum.mjs — the same arrangement tools/test_queue_view.py
has with its render check.

Run: python tools/test_import_controls.py  (exit 0 pass, 1 fail)
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# hermeticity: the app's paths resolve through the music folder the moment they
# are first touched, so the scope is redirected BEFORE server.main is imported.
# --------------------------------------------------------------------------- #
REAL_MUSIC_FOLDER = ""
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        REAL_MUSIC_FOLDER = str((json.load(f) or {}).get("music_folder") or "")
except Exception:
    pass

REDIRECT = tempfile.mkdtemp(prefix="mlo-controls-redirect-")
MF = tempfile.mkdtemp(prefix="mlo-controls-test-")
os.environ["MLO_MUSIC_FOLDER"] = MF

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB = os.path.join(REDIRECT, "config.json")
with open(_STUB, "w", encoding="utf-8") as f:
    json.dump({"music_folder": MF}, f)
for _mod in (cfgmod, pathmod):
    _mod.CONFIG_FILE = _STUB
    if getattr(_mod, "LEGACY_DATA_DIR", None) is not None:
        _mod.LEGACY_DATA_DIR = os.path.join(REDIRECT, "legacy")

atexit.register(lambda: [shutil.rmtree(d, ignore_errors=True)
                         for d in (REDIRECT, MF)])
atexit.register(lambda: os.environ.pop("MLO_MUSIC_FOLDER", None))

_REAL = REAL_MUSIC_FOLDER.replace("\\", "/").rstrip("/").lower()
assert not MF.replace("\\", "/").lower().startswith(_REAL or "\0"), \
    f"temp fixture {MF} sits inside the real music folder {REAL_MUSIC_FOLDER}"

pathmod._warn_if_temp_folder = lambda mf: None

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from mlo import import_policy  # noqa: E402
from server import artist_watch, events, integrations, pending_albums  # noqa: E402
from server import api_add, api_imports, soulseek, soulseek_auto, wishes  # noqa: E402
import server.wishes_worker as wishes_worker  # noqa: E402

# The two stores live beside the real ones — redirect them BEFORE anything
# opens a connection.
wishes.db_path = lambda: os.path.join(REDIRECT, "wishes.db")
wishes._initialized = False
artist_watch.db_path = lambda: os.path.join(REDIRECT, "artist_watch.db")
artist_watch._initialized = False

# The routers server/main.py registers; mounted here on their own app so this
# test does not depend on those lines (or on whatever else lands in main.py).
APP = FastAPI()
APP.include_router(api_imports.router)
APP.include_router(api_add.router)
CLIENT = TestClient(APP)

FAILED = []


def ok(cond, label, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{f' — {extra}' if extra else ''}")
        FAILED.append(label)


def eq(got, want, label, extra=""):
    ok(got == want, label, extra or f"got {got!r}, want {want!r}")


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


class PatchAll:
    """Several Patch objects as one `with`."""

    def __init__(self, patches):
        self.patches = patches

    def __enter__(self):
        for p in self.patches:
            p.__enter__()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.__exit__(*exc)
        return False


class Clock:
    """The wish worker's clock: real time, but its 2 s job polls become 20 ms."""

    def __init__(self):
        self.real = time
        self.time = time.time

    def sleep(self, s):
        self.real.sleep(min(s, 0.02))


CLOCK = Clock()

# --------------------------------------------------------------------------- #
# stubs: one record of what the pipeline was ASKED to do, and nothing else
# --------------------------------------------------------------------------- #
ARTIST = "11111111-3333-3333-3333-333333333333"
GROUP = "22222222-2222-2222-2222-222222222222"
RELEASE = "33333333-1111-1111-1111-111111111111"
ALBUM = "Staged Artist - Staged Album"

searches = []       # every soulseek_auto.start_job the worker made
triggers = []       # every wish id handed to the worker's trigger
browses = []        # every MusicBrainz artist browse the watcher made
queued = []         # every release the watcher queued
created = []        # every album "Add to library" created
emitted = []        # every event emitted

RELEASE_DICT = {
    "id": RELEASE, "title": "Test Album", "date": "2026-03-01",
    "originaldate": "2026", "country": "GB", "status": "Official",
    "medium": "CD", "label": "Test Label", "catalog_number": "CAT-1",
    "release_group_id": GROUP, "release_type": "album",
    "primary_type": "Album", "secondary_types": [],
    "artists": [{"name": "Staged Artist", "mbid": ARTIST}],
    "medium_count": 1,
    "media": [{"disc": 1, "position": 1, "title": "One",
               "recording_mbid": "44444444-4444-4444-4444-444444444444",
               "artist_credit": "Staged Artist"}],
}
GROUP_ROW = {"id": GROUP, "title": "Test Album", "primary_type": "album",
             "secondary_types": [], "first_release_date": "2026-03-01"}
TARGETS = [{"mbid": RELEASE, "title": "Test Album"}]
# A SECOND release for the "Download all" half of the two-button split: the
# first Add leaves a framework folder behind (deleting the wish does not delete
# the folder), and "Download all" starts only what THIS call created.
RELEASE_B = "44444444-2222-2222-2222-222222222222"
RELEASE_B_DICT = dict(RELEASE_DICT, id=RELEASE_B, title="Second Album")
TARGETS_B = [{"mbid": RELEASE_B, "title": "Second Album"}]


def fake_resolve_release(mbid):
    rid = str(mbid or "").lower()
    if rid == RELEASE_B:
        return dict(RELEASE_B_DICT), rid
    return (dict(RELEASE_DICT) if rid == RELEASE else None), rid


def fake_start_job(**kw):
    searches.append(kw)
    return {"ok": True, "job": {"id": "job-1"}}


def fake_job_state(job_id=None):
    return {"state": "done", "result": {"imported": True, "album_path": ALBUM,
                                        "scripts": []}}


def fake_trigger(wid=None):
    triggers.append(wid)
    return {"ok": True}


def fake_create(release, cfg=None, **kw):
    created.append({"release": release.get("id"), **kw})
    return {"album_path": ALBUM, "title": kw.get("title") or release.get("title"),
            "artist": kw.get("artist") or "", "year": kw.get("year") or "",
            "release_id": release.get("id"),
            "release_group_id": release.get("release_group_id"),
            "wish_id": len(created), "cover": None, "created": True, "existing": False}


def fake_queue_release(release, cfg_, **kw):
    queued.append(kw)
    return {"album_path": ALBUM, "release_id": RELEASE, "wish_id": 1,
            "created": True, "already_in_library": False}


def fake_emit(kind, title, body="", data=None, config=None):
    emitted.append({"event": kind, "title": title, "body": body, "data": data or {}})
    return {"event": kind}


integrations.resolve_release = fake_resolve_release
soulseek_auto.start_job = fake_start_job
soulseek_auto.job_state = fake_job_state
soulseek.is_running = lambda *a, **k: True
soulseek.web_up = lambda *a, **k: True
soulseek.server_state = lambda cfg=None: {"isLoggedIn": True}
events.emit = fake_emit
pending_albums.create = fake_create
wishes_worker.trigger = fake_trigger

WISH_MBID = GROUP

# The config these runs read: the shipped defaults, with the switch under test
# turned over. `wishes_enabled` on, so only the switch can stop a pass.
BASE_CFG = dict(cfgmod.DEFAULT_CONFIG)
BASE_CFG["music_folder"] = MF
BASE_CFG["wishes_interval_hours"] = 0


def cfg(**over):
    out = dict(BASE_CFG)
    out.update(over)
    return out


def reset():
    """A clean queue, a clean watch store, and no stub history."""
    for w in wishes.list_wishes():
        wishes.delete_wish(w["id"])
    for table in ("artist_watch_release", "artist_watch"):
        with artist_watch._conn() as c:
            c.execute(f"DELETE FROM {table}")
    searches.clear()
    triggers.clear()
    browses.clear()
    queued.clear()
    created.clear()
    emitted.clear()


def one_wish():
    w = wishes.add_wish(WISH_MBID, title="Test Album", artist="Staged Artist",
                        year="2026")
    return wishes.get_wish(w["id"])


def wish_worker_env(config):
    """The worker's own seams: its config and clock, the config the ROUTES
    resolve inside their handlers (`from mlo.config import load_config`, so the
    module is the thing to stand in for), and the library reconcile — which is
    not this test's business."""
    return [Patch(wishes_worker, load_config=lambda: config, time=CLOCK),
            Patch(cfgmod, load_config=lambda: config),
            Patch(wishes, reconcile_with_library=lambda cfg=None: 0)]


def add_env(config):
    """Add to library's own seams: the config it reads, and the release
    resolution — MusicBrainz's answers are not this test's business."""
    return [Patch(api_add, load_config=lambda: config,
                  _targets=lambda *a, **k: (list(TARGETS), []))]


WATCH = {"id": 7, "artist_mbid": ARTIST, "name": "Staged Artist", "enabled": 1,
         "added_at": time.time() - 365 * 86400, "policy": "new_only",
         "release_types": ["album"], "include": [], "exclude": [],
         "max_per_cycle": 1, "auto_add": 1, "last_checked_at": 0.0,
         "checked_count": 0, "queued_count": 0, "notified_count": 0,
         "last_result": "", "last_error": ""}


def fake_browse(artist_mbid, limit=0):
    browses.append(artist_mbid)
    return [dict(GROUP_ROW)]


def watch_env():
    """The watcher's remote seams: the MusicBrainz browse, the edition picker
    and the one call into "Add to library"."""
    return [Patch(artist_watch, release_groups=fake_browse,
                  queue_release=fake_queue_release,
                  _release_for=lambda rg, cfg_, **kw: (
                      dict(RELEASE_DICT), None,
                      {"mbid": RELEASE, "title": "Test Album", "score": 1.0,
                       "reasons": ["official release"]})),
            Patch(artist_watch, owned_mbids=lambda cfg=None: {},
                  queued_release_groups=lambda: set())]


# --------------------------------------------------------------------------- #
# 1. the config keys themselves
# --------------------------------------------------------------------------- #
print("\nconfig")
base = cfgmod.normalize_config({})
eq(base["auto_acquisition_enabled"], True, "automatic acquisition is on by default")
eq(base["manual_import_enabled"], True, "manual importing is on by default")
eq(import_policy.auto_acquisition_enabled({}), True,
   "a config written before the key reads as on")
eq(import_policy.manual_import_enabled({}), True, "the manual path too")
eq(import_policy.auto_acquisition_enabled({"auto_acquisition_enabled": False}), False,
   "the switch is honoured when it is set")
eq(import_policy.manual_import_enabled({"manual_import_enabled": False}), False,
   "the manual switch too")
ok("auto_acquisition_enabled" in import_policy.AUTO_OFF_NOTE
   and "manual_import_enabled" in import_policy.MANUAL_OFF_NOTE,
   "each refusal names the setting it is about")

# --------------------------------------------------------------------------- #
# 2. manual importing off: the importing routes refuse, nothing else does
# --------------------------------------------------------------------------- #
print("\nmanual_import_enabled off")
reset()
OFF = cfg(manual_import_enabled=False)

with PatchAll(wish_worker_env(OFF) + add_env(OFF)):
    for path, body in (("/api/import/finish", {"paths": []}),
                       ("/api/import/acoustid", {"paths": []}),
                       # The submission route is outward-facing, so it has its
                       # own confirm gate — but the manual switch is asked
                       # FIRST: a user who turned manual importing off never
                       # even reaches the confirmation.
                       ("/api/import/acoustid/submit", {"paths": []}),
                       ("/api/import/bulk", {"items": []})):
        r = CLIENT.post(path, json=body)
        eq(r.status_code, 409, f"{path} refuses with 409")
        ok(import_policy.MANUAL_OFF_NOTE in str((r.json() or {}).get("detail") or ""),
           f"{path}'s refusal is the named sentence", str(r.json())[:200])
    # Reads and the queue's own view are not import actions: a user who turned
    # manual importing off still has to see what is waiting on them.
    eq(CLIENT.get("/api/import/prompts").status_code, 200, "the prompts list still answers")
    eq(CLIENT.post("/api/import/scripts/preview", json={}).status_code, 200,
       "the script preview still answers")
    eq(CLIENT.get("/api/import/bulk/status").status_code, 200,
       "the bulk job's status still answers")
    # And the surfaces that are NOT the manual path keep working: Add to
    # library records the album (its own switch is the master one below).
    r = CLIENT.post("/api/library/add", json={"mbid": RELEASE, "kind": "release"})
    eq(r.status_code, 200, "Add to library is not refused by the manual switch")
    eq(len(created), 1, "and it recorded the album")
    # EVERY add starts the search it recorded, on the worker's own pass (no
    # wish id — the pass reads the store and searches what is due). The two
    # buttons are one action now: "Add to library" used to wait for the
    # automation's next pass, up to two minutes of nothing happening.
    eq(triggers, [None], "Add to library starts the search it recorded")
    # A second, NEW release: one kick, one queue, one wish.
    reset()
    created.clear()
    with PatchAll([Patch(api_add, _targets=lambda *a, **k: (list(TARGETS_B), []))]):
        r = CLIENT.post("/api/library/add",
                        json={"mbid": RELEASE_B, "kind": "release", "download": True})
        eq(r.status_code, 200, "Download all answers too")
        eq(len(created), 1, "and it recorded the album as well")
        eq(triggers, [None], "and it starts the search the same way")

print("\nmanual_import_enabled on (the no-regression half)")
reset()
ON = cfg()

with PatchAll(wish_worker_env(ON) + add_env(ON)):
    r = CLIENT.post("/api/import/finish", json={"paths": []})
    eq(r.status_code, 200, "finish still runs")
    eq(r.json(), {"albums": []}, "and reports the albums it was given")
    eq(CLIENT.post("/api/import/acoustid", json={"paths": []}).status_code, 200,
       "acoustid still runs")
    eq(CLIENT.post("/api/import/acoustid/submit", json={"paths": []}).status_code, 400,
       "the AcoustID submission still needs its own explicit confirm")
    eq(CLIENT.post("/api/import/acoustid/submit",
                   json={"paths": [], "confirm": True}).status_code, 200,
       "…and runs, keyless, once it is given")
    eq(CLIENT.post("/api/import/bulk", json={"items": []}).status_code, 200,
       "the bulk queue still takes work")
    r = CLIENT.post("/api/library/add", json={"mbid": RELEASE, "kind": "release"})
    eq(r.status_code, 200, "Add to library still works")
    eq(r.json().get("note"), "Soulseek is searching for them now.",
       "and says the search it started")

# --------------------------------------------------------------------------- #
# 3. auto acquisition off: nothing searches, nothing queues, and both say why
# --------------------------------------------------------------------------- #
print("\nauto_acquisition_enabled off: the wish queue")
reset()
AUTO_OFF = cfg(auto_acquisition_enabled=False)
wid = one_wish()["id"]

with PatchAll(wish_worker_env(AUTO_OFF)):
    out = wishes_worker.run_cycle()
    eq(out.get("automation"), False, "the wish pass reports itself as switched off")
    ok(import_policy.AUTO_OFF_NOTE in str(out.get("error") or ""),
       "and says which setting is off", str(out)[:200])
    eq(searches, [], "no search was started")
    ok(import_policy.AUTO_OFF_NOTE in str(wishes_worker.status().get("last_result") or ""),
       "the worker's own status says the same thing")
    row = wishes.get_wish(wid)
    eq(row["status"], "wanted", "the wish is left exactly as the user left it")
    eq(row["last_error"] or "", "", "and gains no error it did not have")

    # The user's OWN search of one wish is not the switch's business: that is
    # them acting, which is the way back the note names.
    out_one = wishes_worker.run_cycle(wid=wid)
    eq(len(searches), 1, "an explicit Search now still searches")
    eq(out_one.get("imported"), 1, "and reports the wish it filled")
    eq(wishes.get_wish(wid)["status"], "imported", "the wish is imported")

print("\nauto_acquisition_enabled off: an artist watch")
reset()
with PatchAll(watch_env()):
    out = artist_watch.run_watch(WATCH, AUTO_OFF, force=True)
    eq(out["summary"], import_policy.AUTO_OFF_NOTE,
       "the watch check reports the switch rather than 'nothing new'")
    eq(out["queued"], [], "it queued nothing")
    eq(out["notified"], [], "and notified nothing")
    eq(browses, [], "it did not even browse MusicBrainz")
    eq(queued, [], "nothing reached Add to library")
    eq(WATCH["last_checked_at"], 0.0, "the watch keeps its interval (nothing was checked)")

print("\nauto_acquisition_enabled off: Add to library")
reset()
with PatchAll(wish_worker_env(AUTO_OFF) + add_env(AUTO_OFF)):
    r = CLIENT.post("/api/library/add", json={"mbid": RELEASE, "kind": "release"})
    eq(r.status_code, 200, "the album is still added")
    eq(len(created), 1, "the framework album and its wish are recorded")
    eq(triggers, [], "but nothing starts a download")
    ok(import_policy.AUTO_OFF_NOTE in str(r.json().get("note") or ""),
       "and the answer says why nothing is searching", str(r.json())[:200])

# --------------------------------------------------------------------------- #
# 4. auto acquisition on (the no-regression half)
# --------------------------------------------------------------------------- #
print("\nauto_acquisition_enabled on")
reset()
wid = one_wish()["id"]

with PatchAll(wish_worker_env(ON)):
    out = wishes_worker.run_cycle()
    eq(len(searches), 1, "a wish pass searches again")
    eq(out.get("imported"), 1, "and reports the wish it filled")
    eq(wishes.get_wish(wid)["status"], "imported", "the wish is imported")

reset()
with PatchAll(watch_env()):
    out = artist_watch.run_watch(WATCH, ON, force=True)
    eq(len(queued), 1, "a watch check queues the new release again")
    eq(len(out["queued"]), 1, "and reports it")
    eq(browses, [ARTIST], "after browsing the artist it watches")
    eq(len(emitted), 1, "and announces what it queued")

reset()
with PatchAll(wish_worker_env(ON) + add_env(ON)):
    r = CLIENT.post("/api/library/add", json={"mbid": RELEASE, "kind": "release"})
    eq(r.status_code, 200, "Add to library works")
    eq(triggers, [None], "Add to library starts the search it recorded")
    started = len(triggers)
    with PatchAll([Patch(api_add, _targets=lambda *a, **k: (list(TARGETS_B), []))]):
        r = CLIENT.post("/api/library/add",
                        json={"mbid": RELEASE_B, "kind": "release", "download": True})
        eq(r.status_code, 200, "Download all works")
        eq(len(triggers), started + 1, "and adds ONE kick for the whole call")

# --------------------------------------------------------------------------- #
# 5. the wizard's own half: minimum entry and the switch, rendered for real
# --------------------------------------------------------------------------- #
print("\nthe wizard's page render")
ui = subprocess.run(["node", os.path.join("tools", "check_import_minimum.mjs")],
                    cwd=ROOT, capture_output=True, text=True)
if ui.returncode == 0:
    print("ok  " + ui.stdout.strip().removeprefix("ok  "))
elif ui.returncode == 2:
    print("SKIPPED  the page render check (node or web/node_modules missing): "
          + (ui.stderr.strip().splitlines() or [""])[0])
else:
    raise AssertionError("the page render check failed:\n" + ui.stdout + ui.stderr)

# --------------------------------------------------------------------------- #
reset()
print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    for name in FAILED:
        print(f"  - {name}")
    sys.exit(1)
print("all checks passed")
