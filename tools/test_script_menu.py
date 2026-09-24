#!/usr/bin/env python3
"""The details menu's script list (server/script_menu.py) — one source, no drift.

What this pins, over a TEMP config (the developer's real config.json is never
read or written):

  * the applicability table is COMPLETE against the registry: every id in
    ``server.script_runners.RUNNERS`` has a scope and a reason, and the reason
    is prose a reader can follow. Adding a script to the registry and not to the
    table FAILS here — and the failure is shown to be real, not theoretical, by
    handing the builder a registry with an extra id and reading the answer: the
    id is REPORTED (``unclassified``) and offered everywhere rather than
    silently dropped from every menu.
  * ``applies_to`` is the table, executed: the album and track sets differ by
    exactly the FOLDER-scoped scripts, frozen below, so the two menus cannot
    quietly converge on "everything" or on "the ten the old menu typed in".
  * the force flag per script is the one ``/api/run`` accepts, and the menu can
    therefore offer every force switch ``web/src/lib/force.ts`` defines — and no
    other. The union of the aliases the payload emits is compared against that
    file, so the menu and the Optimization page's Force switch cannot drift.
  * a script's feature switch and its skip sentence are the RUNNER's: the
    payload's ``gate.reason`` is compared, character for character, with what
    ``script_runners.run_script`` answers for the same config — and 17's
    any-of rule (transliterate OR translate) is checked, not assumed.
  * the payload is ordered like the stack (run_all_order) and each label is
    ``mlo.cli.SCRIPTS``', the table README.md and web/src/lib/scripts.ts mirror.
  * GET /api/script-menu answers, and server/main.py mounts its router.

Run: python tools/test_script_menu.py   (exit 0 pass, 1 fail, 2 skip)
"""
import atexit
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# Hermeticity: app paths resolve through the music folder the moment they are
# first touched, so the scope is redirected to a temp folder BEFORE mlo.config
# is imported (the recipe tools/test_check_stack.py uses). Nothing is ever
# written to the developer's own music folder or repo config.json.
# --------------------------------------------------------------------------- #
REAL_MUSIC_FOLDER = ""
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        REAL_MUSIC_FOLDER = str((json.load(fh) or {}).get("music_folder") or "")
except Exception:
    pass

REDIRECT = tempfile.mkdtemp(prefix="mlo-scriptmenu-redirect-")
os.environ["MLO_MUSIC_FOLDER"] = REDIRECT

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB = os.path.join(REDIRECT, "config.json")
with open(_STUB, "w", encoding="utf-8") as fh:
    json.dump({"music_folder": REDIRECT}, fh)

for _mod in (cfgmod, pathmod):
    _mod.CONFIG_FILE = _STUB
    if getattr(_mod, "LEGACY_DATA_DIR", None) is not None:
        _mod.LEGACY_DATA_DIR = os.path.join(REDIRECT, "legacy")


def _cleanup():
    os.environ.pop("MLO_MUSIC_FOLDER", None)
    shutil.rmtree(REDIRECT, ignore_errors=True)


atexit.register(_cleanup)

_REAL = REAL_MUSIC_FOLDER.replace("\\", "/").rstrip("/")
assert not REDIRECT.replace("\\", "/").lower().startswith(_REAL.lower() or "\0"), \
    "the redirect landed inside the real music folder"

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except Exception as e:  # pragma: no cover - a missing extra is a SKIP
    print(f"SKIP: TestClient unavailable: {e}")
    raise SystemExit(2)

from mlo.cli import SCRIPTS, SCRIPT_GATES  # noqa: E402
from mlo.config import DEFAULT_RUN_ALL_ORDER  # noqa: E402
from server import script_menu, script_runners  # noqa: E402
from server.script_menu import BECAUSE, KINDS, SCOPES  # noqa: E402
from server.script_runners import RUNNERS, _DISABLED  # noqa: E402

# --------------------------------------------------------------------------- #
# The frozen expectations. They live HERE, not in the module under test: a gate
# that reads its expectation out of the code it checks cannot notice that code
# changing its mind about where a script belongs.
# --------------------------------------------------------------------------- #
# A FILE-scoped script's work unit is one file, so its menu is any menu that
# holds paths. A FOLDER-scoped script's is the album/artist folder — its
# sidecars, its cover art, its release manifest, its per-album measurement, the
# subtree it fixes — so it is offered where a folder is in hand.
EXPECTED_FOLDER_SCOPED = {
    2,   # the .cue sidecar                            (mlo/cue.py)
    4,   # album grading, a track has no checks         (mlo/grader.py)
    5,   # the album's image files                      (mlo/images.py)
    7,   # one album's DR / ReplayGain                  (mlo/loudness.py)
    8,   # tags + renames the album                     (mlo/autotag.py)
    9,   # one .accurip per CD album                    (mlo/accurip.py)
    10,  # the album's final pass + its sidecars        (mlo/format_all.py)
    14,  # beets imports album folders only             (server/beetscfg.py)
    15,  # one release manifest per album               (server/script_runners.py)
    19,  # the artist folder's image                    (mlo/artistdata.py)
    20,  # the layout of a subtree                      (mlo/layout.py)
}
# Kinds whose menu holds folders; the other two hold files.
EXPECTED_FOLDER_KINDS = {"album", "artist", "library"}
EXPECTED_FILE_KINDS = {"track", "playlist"}
# Ids whose one force flag makes a forced re-run entry; the composite (10)
# is deliberately absent, exactly as web/src/lib/force.ts has no such switch.
EXPECTED_FORCED = {1, 2, 3, 5, 6, 7, 8, 9, 12, 13, 15, 16, 17, 18, 20}

FAILED: list = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}"
          f"{('  — ' + str(detail)) if detail and not ok else ''}")
    if not ok:
        FAILED.append(name)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


app = FastAPI()
app.include_router(script_menu.router)
client = TestClient(app)

MENU = {sid: (name, desc) for sid, name, desc in SCRIPTS}
BY_ID = {}


def payload(cfg=None, **kw):
    return script_menu.script_menu(cfg=cfg, **kw)


# --------------------------------------------------------------------------- #
# The table and the registry
# --------------------------------------------------------------------------- #
print("== the applicability table ==")
data = payload()
for row in data["scripts"]:
    BY_ID[row["id"]] = row

check("every script in RUNNERS has an applicability entry",
      sorted(SCOPES) == sorted(RUNNERS),
      f"unclassified: {sorted(set(RUNNERS) - set(SCOPES))} · "
      f"stale: {sorted(set(SCOPES) - set(RUNNERS))}")
check("every applicability entry states why",
      all(str(BECAUSE.get(sid, "")).strip() for sid in RUNNERS),
      f"missing: {sorted(sid for sid in RUNNERS if not str(BECAUSE.get(sid, '')).strip())}")
check("a scope is one of the two the model has",
      set(SCOPES.values()) <= {"file", "folder"}, str(sorted(set(SCOPES.values()))))
check("the endpoint's scripts are the registry, label for label",
      [r["id"] for r in data["scripts"]] == sorted(RUNNERS) or
      sorted(r["id"] for r in data["scripts"]) == sorted(RUNNERS),
      str([r["id"] for r in data["scripts"]]))
check("every label is mlo.cli.SCRIPTS' own",
      all(row["label"] == MENU[row["id"]][0] and
          row["description"] == MENU[row["id"]][1]
          for row in data["scripts"]))
check("no script is left unclassified by the real registry",
      data["unclassified"] == [], str(data["unclassified"]))

# The anti-drift guarantee, EXECUTED: hand the builder a registry with one more
# script and read what the menu does with it. It must be REPORTED and offered
# (fail open — silently dropping it is exactly the drift), so the test's
# completeness check above is what fails, loudly, for a human to fix.
grown = dict(RUNNERS)
grown[99] = ("A brand new script", None)
grown_menu = payload(runners=grown, scopes=SCOPES)
row99 = next((r for r in grown_menu["scripts"] if r["id"] == 99), None)
check("a script added to the registry without a scope is REPORTED",
      grown_menu["unclassified"] == [99], str(grown_menu["unclassified"]))
check("...and is offered everywhere rather than hidden from every menu",
      row99 is not None and sorted(row99["applies_to"]) == sorted(KINDS),
      str(row99))
check("...and its absence is what the completeness check above fails on",
      sorted(SCOPES) != sorted(grown), "the registry grew and the table did not")

# --------------------------------------------------------------------------- #
# applies_to, derived
# --------------------------------------------------------------------------- #
print("== what each entity's menu offers ==")
kinds_of = lambda kind: {row["id"] for row in data["scripts"] if kind in row["applies_to"]}
album, track = kinds_of("album"), kinds_of("track")
artist, playlist, library = kinds_of("artist"), kinds_of("playlist"), kinds_of("library")

check("the album menu offers every script", album == set(RUNNERS),
      str(sorted(set(RUNNERS) - album)))
check("a track row does NOT offer a folder-scoped script",
      track == set(RUNNERS) - EXPECTED_FOLDER_SCOPED,
      f"offered anyway: {sorted(track & EXPECTED_FOLDER_SCOPED)} · "
      f"missing: {sorted((set(RUNNERS) - EXPECTED_FOLDER_SCOPED) - track)}")
check("album minus track is exactly the folder-scoped set",
      album - track == EXPECTED_FOLDER_SCOPED, str(sorted(album - track)))
check("the two sets really differ (an album-only script exists to hide)",
      track < album and len(EXPECTED_FOLDER_SCOPED) >= 1,
      f"track={len(track)} album={len(album)}")
check("the folder-scoped ids are the ones the test froze",
      {sid for sid, scope in SCOPES.items() if scope == "folder"} == EXPECTED_FOLDER_SCOPED,
      str(sorted({sid for sid, scope in SCOPES.items() if scope == "folder"})))
check("an artist menu offers the album-shaped scrips too", artist == album,
      str(sorted(album ^ artist)))
check("the library offers every script", library == set(RUNNERS),
      str(sorted(set(RUNNERS) - library)))
check("a playlist selection is a file list, like a track row", playlist == track,
      str(sorted(track ^ playlist)))
check("the kinds are the five the model names and every one is decided",
      sorted(KINDS) == sorted(EXPECTED_FOLDER_KINDS | EXPECTED_FILE_KINDS), str(KINDS))
check("a file-scoped script reaches every kind",
      all(set(row["applies_to"]) == set(KINDS)
          for row in data["scripts"] if row["scope"] == "file"))

# --------------------------------------------------------------------------- #
# Force flags
# --------------------------------------------------------------------------- #
print("== force flags ==")
src = read("web/src/lib/force.ts")
force_keys = set(re.findall(r'key:\s*"(\w+)"', src))
emitted = {alias for row in data["scripts"] for alias in row["force"]["keys"]}
check("the menu can run every force switch the app defines",
      force_keys <= emitted, f"unreachable: {sorted(force_keys - emitted)}")
check("...and emits no force switch the app does not define",
      emitted <= force_keys, f"invented: {sorted(emitted - force_keys)}")
check("a forced re-run is offered exactly for the scripts with ONE force flag",
      {row["id"] for row in data["scripts"] if row["force"]["key"]} == EXPECTED_FORCED,
      str(sorted({row["id"] for row in data["scripts"] if row["force"]["key"]})))
check("a forced entry names the key /api/run accepts",
      all(row["force"]["key"] in row["force"]["keys"]
          for row in data["scripts"] if row["force"]["key"]))

# --------------------------------------------------------------------------- #
# Feature switches — the runner's rule, not a second opinion about it
# --------------------------------------------------------------------------- #
print("== feature switches ==")
check("the gate table is the runner's own", SCRIPT_GATES == _DISABLED,
      "mlo/cli.SCRIPT_GATES and script_runners._DISABLED must stay mirrors")

off = payload(cfg={"music_folder": REDIRECT, "mood_enabled": False})
mood = next(r for r in off["scripts"] if r["id"] == 16)
check("a script whose feature is off is still offered, with the reason",
      mood["applies_to"] and mood["gate"]["enabled"] is False
      and mood["gate"]["reason"] == "mood_enabled is off", str(mood["gate"]))
run = script_runners.run_script(16, {"music_folder": REDIRECT, "mood_enabled": False})
check("...and the reason is the RUN's own sentence",
      run.get("skipped") is True and run.get("reason") == mood["gate"]["reason"],
      f"run={run.get('reason')!r} menu={mood['gate']['reason']!r}")

# 17 does transliteration OR translation: one switch on keeps it alive (the
# any-of rule script_runners.run_script applies). A menu that read that rule as
# "all of them" would disable a script the run would have performed.
half = payload(cfg={"music_folder": REDIRECT, "lyrics_xlit_enabled": True,
                    "lyrics_translate_enabled": False})
xlit = next(r for r in half["scripts"] if r["id"] == 17)
half_run = script_runners.run_script(17, {"music_folder": REDIRECT,
                                          "lyrics_xlit_enabled": True,
                                          "lyrics_translate_enabled": False},
                                     targets=[__file__])
check("a script with two switches is alive while ANY is on, like the run",
      xlit["gate"]["enabled"] is True and xlit["gate"]["reason"] == ""
      and half_run.get("skipped") is not True, str((xlit["gate"], half_run)))
check("the switches are named, so the menu can say which one to turn on",
      xlit["gate"]["keys"] == ["lyrics_xlit_enabled", "lyrics_translate_enabled"],
      str(xlit["gate"]))
check("an un-gated script says nothing at all",
      next(r for r in data["scripts"] if r["id"] == 3)["gate"]
      == {"keys": [], "enabled": True, "reason": ""})

# --------------------------------------------------------------------------- #
# Order and availability
# --------------------------------------------------------------------------- #
print("== order ==")
in_order = [i for i in data["scripts"] if i["in_order"]]
check("the menu's order is the stack's run_all_order",
      [i["id"] for i in in_order] == [s for s in DEFAULT_RUN_ALL_ORDER if s in RUNNERS]
      or [i["id"] for i in in_order] == list(DEFAULT_RUN_ALL_ORDER),
      str([i["id"] for i in in_order]))
check("each script's order is its slot in the chain",
      all(row["order"] == DEFAULT_RUN_ALL_ORDER.index(row["id"])
          for row in in_order if row["id"] in DEFAULT_RUN_ALL_ORDER))
check("every script carries its group id and the payload names its groups",
      {row["group"] for row in data["scripts"]} == {g["id"] for g in data["groups"]}
      and [g["id"] for g in data["groups"]] == ["scripts"]
      and data["forced_group"]["id"] == "force"
      and bool(data["forced_group"]["title"]),
      str(data["groups"]) + " " + str(data.get("forced_group")))
check("availability follows the runner's existence (a stripped checkout)",
      all(row["available"] == bool(RUNNERS[row["id"]][1]) for row in data["scripts"]))

# --------------------------------------------------------------------------- #
# The endpoint and its wiring
# --------------------------------------------------------------------------- #
print("== the endpoint ==")
r = client.get("/api/script-menu")
check("GET /api/script-menu answers", r.status_code == 200, f"HTTP {r.status_code}")
body = r.json() if r.status_code == 200 else {}
check("...with every script, and nothing unclassified",
      len(body.get("scripts") or []) == len(RUNNERS) and body.get("unclassified") == [],
      str(body.get("unclassified")))
check("...and the kinds a client may ask about",
      body.get("kinds") == list(KINDS), str(body.get("kinds")))
check("server/main.py mounts the router",
      "script_menu" in read("server/main.py") and
      "script_menu.router" in read("server/main.py"))

print(f"\n{'PASS' if not FAILED else 'FAIL'} — {len(FAILED)} problem(s)")
for name in FAILED:
    print(f"  - {name}")
sys.exit(1 if FAILED else 0)
