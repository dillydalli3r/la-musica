#!/usr/bin/env python3
"""Verify the import switches, at the seam each one owns.

`manual_import_enabled` is the switch over the path the USER drives (the wizard
and the importing `POST /api/import/*` routes); it defaults on, which is the
no-regression half of this test. `auto_acquisition_enabled` is the master
switch's own vocabulary in `mlo.import_policy` — its key, its reader and its
refusal sentence are pinned with the manual one.

Nothing here touches the network: "Add to library"'s release resolution and
album creation are stubbed and the music folder is a temp directory, so the
developer's library is never opened.

Pinned here, one case each:
  * the switch keys and their refusal sentences, from the policy module;
  * manual importing off — the importing routes answer 409 with a sentence
    naming the setting, while the routes that only read (the prompts list, the
    script preview, the bulk job's status) and the surfaces that are not the
    manual path (Add to library) keep answering;
  * manual importing on — every one of those does what it always did.

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
from server import integrations, pending_albums  # noqa: E402
from server import api_add, api_imports  # noqa: E402

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


# --------------------------------------------------------------------------- #
# stubs: one record of what the pipeline was ASKED to do, and nothing else
# --------------------------------------------------------------------------- #
ARTIST = "11111111-3333-3333-3333-333333333333"
GROUP = "22222222-2222-2222-2222-222222222222"
RELEASE = "33333333-1111-1111-1111-111111111111"
ALBUM = "Staged Artist - Staged Album"

created = []        # every album "Add to library" created

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
TARGETS = [{"mbid": RELEASE, "title": "Test Album"}]


def fake_resolve_release(mbid):
    rid = str(mbid or "").lower()
    return (dict(RELEASE_DICT) if rid == RELEASE else None), rid


def fake_create(release, cfg=None, **kw):
    created.append({"release": release.get("id"), **kw})
    return {"album_path": ALBUM, "title": kw.get("title") or release.get("title"),
            "artist": kw.get("artist") or "", "year": kw.get("year") or "",
            "release_id": release.get("id"),
            "release_group_id": release.get("release_group_id"),
            "cover": None, "created": True, "existing": False}


integrations.resolve_release = fake_resolve_release
pending_albums.create = fake_create
pending_albums.prefetch_content = lambda *a, **k: None


# The config these runs read: the shipped defaults with the switch under test
# turned over.
BASE_CFG = dict(cfgmod.DEFAULT_CONFIG)
BASE_CFG["music_folder"] = MF


def cfg(**over):
    out = dict(BASE_CFG)
    out.update(over)
    return out


def reset():
    """No stub history."""
    created.clear()


def route_env(config):
    """The config the ROUTES resolve inside their handlers (`from mlo.config
    import load_config`, so the module is the thing to stand in for)."""
    return [Patch(cfgmod, load_config=lambda: config)]


def add_env(config):
    """Add to library's own seams: the config it reads, and the release
    resolution — MusicBrainz's answers are not this test's business."""
    return [Patch(api_add, load_config=lambda: config,
                  _targets=lambda *a, **k: (list(TARGETS), []))]


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

with PatchAll(route_env(OFF) + add_env(OFF)):
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
    # library records the album — its own switch is the master one.
    r = CLIENT.post("/api/library/add", json={"mbid": RELEASE, "kind": "release"})
    eq(r.status_code, 200, "Add to library is not refused by the manual switch")
    eq(len(created), 1, "and it recorded the album")

print("\nmanual_import_enabled on (the no-regression half)")
reset()
ON = cfg()

with PatchAll(route_env(ON) + add_env(ON)):
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
    eq(r.json().get("note"), "Added to your library — add its audio when you have it.",
       "and reports the album it recorded")

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
