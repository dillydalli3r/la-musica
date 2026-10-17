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
    ``mlo.scripts.SCRIPTS``', the table README.md and web/src/lib/scripts.ts mirror.
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

from mlo.scripts import SCRIPTS, SCRIPT_GATES  # noqa: E402
from mlo.config import DEFAULT_RUN_ALL_ORDER  # noqa: E402
from server import script_menu, script_runners  # noqa: E402
from server.script_menu import BECAUSE, KINDS, SCOPES  # noqa: E402
from server.script_runners import (OPT_IN_SCRIPTS, RUNNERS, _DISABLED,  # noqa: E402
                                   _FORCE_ALIASES, _FORCE_KEYS)

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
    24,  # one album's public rating, and each track's  (mlo/web_ratings.py)
    25,  # the artist folder's missing image            (server/script_runners.py)
    26,  # the artist folder's missing description      (server/script_runners.py)
}
# Kinds whose menu holds folders; the other two hold files.
EXPECTED_FOLDER_KINDS = {"album", "artist", "library"}
EXPECTED_FILE_KINDS = {"track", "playlist"}
# Every script whose own _FORCE_KEYS is non-empty, frozen. 10 is here because
# it owns a COMPOSITE flag set (the four passes it re-runs) — and each of those
# four must be reachable from the menu, one entry per flag. 13 is NOT (since
# v4.4.0): a lyrics run fills what is missing and never replaces stored words,
# so it has no re-run to force (R330) — replacing one track's lyrics is
# `POST /api/lyrics/auto` with force, not a library-wide script run. 24 is here
# since its flag joined the UI force tables (R362): the pass fills only, and
# forcing it re-fetches a rating the file already carries. 25/26 own the
# OVERWRITE flags of the artist-image / description fetches (R379).
EXPECTED_FORCED = {1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 15, 16, 17, 20, 24, 25, 26}

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
check("every label is mlo.scripts.SCRIPTS' own",
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
# The tag strip (23, mlo/taghygiene.py) is the SCOPED entry point to the same
# deletion script 10 performs as a step of its own pass: it is file-scoped, so
# an album's files can be cleaned from the album's menu and a track's from its
# row — and it is in the track row's Run-all list too, because a file menu
# really can run it.
check("23 (Optimize tags) is offered by an album's menu and a track row's",
      BY_ID[23]["scope"] == "file" and set(BY_ID[23]["applies_to"]) == set(KINDS)
      and 23 in album and 23 in track
      and 23 in data["run_all"]["by_kind"]["track"],
      f"scope={BY_ID[23]['scope']} kinds={BY_ID[23]['applies_to']}")
check("...states what it deletes, and offers no forced re-run",
      "excess" in BECAUSE[23] and not BY_ID[23]["force"]["options"],
      str((BECAUSE[23], BY_ID[23]["force"])))
check("...is gated on the switch its whole rule is gated on",
      BY_ID[23]["gate"]["keys"] == ["strip_unknown_tags"]
      and BY_ID[23]["gate"]["enabled"] is True,
      str(BY_ID[23]["gate"]))

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
check("a forced re-run is offered exactly for the scripts that own a flag",
      {row["id"] for row in data["scripts"] if row["force"]["options"]} == EXPECTED_FORCED,
      str(sorted({row["id"] for row in data["scripts"] if row["force"]["options"]})))
# ONE ENTRY PER FLAG: a script with a composite flag (10 re-runs what the flags
# above it force) must expose each of them, or the registry knows a force option
# the menu cannot reach.
check("every flag a script owns has its own option",
      all(len(row["force"]["options"]) == len(_FORCE_KEYS.get(row["id"], ()))
          for row in data["scripts"]),
      str([(row["id"], len(row["force"]["options"]), len(_FORCE_KEYS.get(row["id"], ())))
           for row in data["scripts"] if len(row["force"]["options"]) != len(_FORCE_KEYS.get(row["id"], ()))]))
check("...naming the config key and the short key /api/run accepts",
      all(o["config"] in _FORCE_KEYS.get(row["id"], ())
          and _FORCE_ALIASES.get(o["key"]) == o["config"]
          for row in data["scripts"] for o in row["force"]["options"]),
      str([(row["id"], o) for row in data["scripts"] for o in row["force"]["options"]
           if o["config"] not in _FORCE_KEYS.get(row["id"], ())
           or _FORCE_ALIASES.get(o["key"]) != o["config"]]))
check("...and saying which pass it re-runs, in the registry's own words",
      all(o["owner"] in RUNNERS and str(o["owner_label"]).strip()
          for row in data["scripts"] for o in row["force"]["options"]),
      str([(row["id"], o) for row in data["scripts"] for o in row["force"]["options"]
           if o["owner"] not in RUNNERS or not str(o["owner_label"]).strip()]))
check("script 13 offers NO forced twin — a lyrics run fills, it never replaces (R330)",
      not next(r for r in data["scripts"] if r["id"] == 13)["force"]["options"],
      str(next(r for r in data["scripts"] if r["id"] == 13)["force"]))
check("...and a script's own flag is labelled with the pass it re-runs",
      next(o for o in next(r for r in data["scripts"] if r["id"] == 1)["force"]["options"])["owner"] == 1,
      str(next(r for r in data["scripts"] if r["id"] == 1)["force"]))
check("a composite script's options are all of its flags, in the registry's order",
      [o["config"] for o in next(r for r in data["scripts"] if r["id"] == 10)["force"]["options"]]
      == list(_FORCE_KEYS[10]), str(next(r for r in data["scripts"] if r["id"] == 10)["force"]))

# --------------------------------------------------------------------------- #
# Run all — the chain, once, over this entity
# --------------------------------------------------------------------------- #
print("== run all ==")
# The chain this scratch config runs: the configured order, opt-ins aside.
chain = [sid for sid in DEFAULT_RUN_ALL_ORDER
         if sid in RUNNERS and sid not in OPT_IN_SCRIPTS]
check("run_all.order is the chain without the opt-in scripts",
      data["run_all"]["order"] == chain, str(data["run_all"]["order"]))
check("the opt-in scripts are NAMED, not merely missing",
      [e["id"] for e in data["run_all"]["excluded"]] == sorted(OPT_IN_SCRIPTS & set(RUNNERS))
      and all(str(e["why"]).strip() and str(e["label"]).strip() for e in data["run_all"]["excluded"]),
      str(data["run_all"]["excluded"]))
check("NO run-all list sweeps an opt-in script up",
      not (set(data["run_all"]["order"]) & OPT_IN_SCRIPTS)
      and not any(set(v) & OPT_IN_SCRIPTS for v in data["run_all"]["by_kind"].values()),
      str(data["run_all"]["by_kind"]))
check("one list per entity kind",
      sorted(data["run_all"]["by_kind"]) == sorted(KINDS), str(sorted(data["run_all"]["by_kind"])))
for kind in KINDS:
    want = [sid for sid in chain if kind in next(r for r in data["scripts"] if r["id"] == sid)["applies_to"]]
    got = data["run_all"]["by_kind"][kind]
    check(f"the {kind} list is the chain scoped to what that menu offers",
          got == want, f"{got} != {want}")
    check(f"the {kind} list holds each script once, in the chain's order",
          len(got) == len(set(got))
          and got == sorted(got, key=lambda s: chain.index(s)), str(got))
check("an album runs every chain script, a track row only the file-scoped ones",
      data["run_all"]["by_kind"]["album"] == chain
      and set(data["run_all"]["by_kind"]["track"]) < set(data["run_all"]["by_kind"]["album"])
      and data["run_all"]["by_kind"]["track"] == [s for s in chain if s in set(RUNNERS) - EXPECTED_FOLDER_SCOPED],
      str(data["run_all"]["by_kind"]["track"]))
check("a script out of the chain is not swept up by a run-all press",
      not (set(payload(runners={**RUNNERS, 99: ("A brand new script", None)},
                       scopes=SCOPES)["run_all"]["order"]) & {99}))

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

# 23's switch is `strip_unknown_tags` — the one script 10's strip and the
# excess-tag grade read: with it off nothing in this app is excess, so the
# hygiene pass really does nothing and a chain skips it.
strip = payload(cfg={"music_folder": REDIRECT, "strip_unknown_tags": False})
hygiene = next(r for r in strip["scripts"] if r["id"] == 23)
strip_run = script_runners.run_script(
    23, {"music_folder": REDIRECT, "strip_unknown_tags": False},
    targets=[__file__])
check("23 is skipped on the strip switch, and says so",
      hygiene["applies_to"] and hygiene["gate"]["enabled"] is False
      and hygiene["gate"]["reason"] == "strip_unknown_tags is off"
      and strip_run.get("skipped") is True
      and strip_run.get("reason") == hygiene["gate"]["reason"],
      f"menu={hygiene['gate']} run={strip_run.get('reason')!r}")

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

# --------------------------------------------------------------------------- #
# Every entry RUNS — the press, through the app's own route
# --------------------------------------------------------------------------- #
# The payload above is a LIST. This half presses it: for every entry the album
# menu and the track menu offer — and every forced twin — it posts /api/run
# with the payload's ids, the targets the CLIENT's own scope rule picks
# (`web/src/lib/scriptMenu.targetsFor`: the entity's folder for a
# folder-scoped script, the selection's files otherwise) and the entry's force
# key, then reads the answer. A listing that cannot run is the drift this menu
# keeps being rescued from: a runner that is gone, a scope handing a script the
# wrong shape, a force key /api/run does not accept, or a crash on a real
# album (the owner's ask, 2026-10-13: "make sure that when the details / ...
# menu on items is clicked, the scripts listed are up to date and actually
# work when used properly").
#
# The network is SHUT for this half (the socket, so urllib, requests, beets and
# every provider are covered at once): suites do not call MusicBrainz,
# Wikipedia or a lyric host. What the fetching scripts do with a dead network —
# report their own failure inside their stats — is their own suites' business;
# what this pins is that the PRESS reaches the right runner and comes back with
# a run's own shape. 22 is included: its runner answers "no API key" BEFORE it
# posts anything anywhere, which is also why a test never gives it a key.
print("== every entry runs ==")


def _flac_exe():
    deps = os.path.join(ROOT, ".dependencies")
    if not os.path.isdir(deps):
        return None
    for entry in sorted(os.listdir(deps)):
        cand = os.path.join(deps, entry, "flac.exe")
        if os.path.isfile(cand):
            return cand
    return None


FLAC_EXE = _flac_exe()
if not FLAC_EXE:
    print("  (no flac.exe under .dependencies: the press half is not exercised)")
else:
    import socket
    import subprocess
    import wave

    from fastapi.testclient import TestClient as _TestClient
    from server import main as server_main

    # Retry backoff is REAL time, and a shut network makes every fetch spend
    # its whole budget on it — measured with the socket patch alone: 24 · Web
    # ratings 142 s, 25/26 70 s each, 8 · Auto tagging 36 s, and the sweep took
    # 7.7 minutes. The sweep is about the PRESS, so the clock is skipped too:
    # every attempt still runs, every failure is still its own, and the suite
    # is seconds.
    import time

    _sleep = time.sleep
    time.sleep = lambda *a, **kw: None

    _connect = socket.socket.connect

    def _no_network(self, address, *a, **kw):
        host = address[0] if isinstance(address, tuple) else str(address)
        if str(host) not in ("127.0.0.1", "::1", "localhost"):
            raise OSError("network is shut for this suite")
        return _connect(self, address, *a, **kw)

    socket.socket.connect = _no_network

    ALBUMID = "7d192da4-1427-4ca9-baa4-38c78ce25db0"
    RGID = "066f840f-e357-436f-bb0a-47ad7cfcd84a"
    ARTISTID = "e21857d5-3256-4547-afb3-4b6ded592596"
    RECS = ["b8175f9f-340b-403f-97c7-c193a212cd39",
            "81aa9d02-af86-47cd-8969-95e4750e0e88"]
    ART = os.path.join(REDIRECT, "Artists", f"Tester [{ARTISTID}]")
    ALBUM = os.path.join(ART, f"Menu Probe [{ALBUMID}] [{RGID}]")
    BASE = {
        "ALBUM": "Menu Probe", "ALBUMARTIST": "Tester", "ARTIST": "Tester",
        "DATE": "2026-01-02", "ORIGINALDATE": "2026-01-02",
        "ORIGINALYEAR": "2026", "TRACKTOTAL": str(len(RECS)),
        "DISCTOTAL": "1", "MEDIA": "CD", "RELEASETYPE": "Album",
        "RELEASESTATUS": "Official", "RELEASECOUNTRY": "US", "LABEL": "Label",
        "CATALOGNUMBER": "CAT-1", "BARCODE": "0123456789012",
        "MUSICBRAINZ_ALBUMID": ALBUMID, "MUSICBRAINZ_RELEASEGROUPID": RGID,
        "MUSICBRAINZ_ALBUMARTISTID": ARTISTID, "MUSICBRAINZ_ARTISTID": ARTISTID,
        "GENRE": "Trip Hop", "SCRIPT": "Latn", "LANGUAGE": "eng",
    }

    from mlo.subproc import tool_path

    def _flac(path, seconds=14):
        """A silent FLAC at *path*, through the app's own long-path bridge.

        These fixtures live under a temp folder whose name (plus the album's
        own) can pass Windows' 260-character MAX_PATH, which is where
        flac.exe stops — `mlo.subproc.tool_path` is the bridge every runner
        already runs its tools through, so the suite uses it too."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        wav = path + ".wav"
        with wave.open(wav, "w") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x00\x00\x00" * int(44100 * seconds))
        subprocess.run([tool_path(FLAC_EXE), "-s", "-f", "-8",
                        "-o", tool_path(path), tool_path(wav)],
                       check=True, capture_output=True)
        os.remove(wav)

    from mlo.audio import AudioFile

    def _tag(path, **kw):
        af = AudioFile(path)
        af.defer_save(True)
        for k, v in kw.items():
            af.set_tag(k, str(v))
        af.defer_save(False)

    for n, rec in enumerate(RECS, 1):
        p = os.path.join(ALBUM, f"1-0{n} Song {n} [{rec}] [{RGID}].flac")
        _flac(p)
        _tag(p, **dict(BASE, TITLE=f"Song {n}", TRACKNUMBER=str(n),
                       DISCNUMBER="1", MUSICBRAINZ_TRACKID=rec,
                       LYRICS=f"[00:01.00]la la {n}\n[00:05.00]la la la\n"))
    # A cue and a description: the album-shaped files 2 / 10 / 26 work on.
    with open(os.path.join(ALBUM, "CD-1.cue"), "w", encoding="utf-8") as fh:
        fh.write("PERFORMER \"Tester\"\nTITLE \"Menu Probe\"\n" + "".join(
            f'FILE "1-0{n} Song {n} [{r}] [{RGID}].wav" WAVE\n'
            f'  TRACK 0{n} AUDIO\n    TITLE "Song {n}"\n'
            f'    PERFORMER "Tester"\n    INDEX 01 00:00:00\n'
            for n, r in enumerate(RECS, 1)))
    with open(os.path.join(ALBUM, "description.txt"), "w", encoding="utf-8") as fh:
        fh.write("A probe album, for the details menu.\n")

    CLIENT = _TestClient(server_main.app)

    def _album_now():
        """The album folder, wherever the press before this one left it.

        The chain's own steps (14 beets, 20 layout) rename and move albums — a
        pass doing its job, and a stale path would make the NEXT press look
        broken."""
        for root, dirs, _files in os.walk(os.path.join(REDIRECT, "Artists")):
            for d in dirs:
                if RGID in d or ALBUMID in d:
                    return os.path.join(root, d)
        return ALBUM

    def _tracks_now():
        d = _album_now()
        if not os.path.isdir(d):
            return []
        return sorted(os.path.join(d, f) for f in os.listdir(d)
                      if f.lower().endswith(".flac"))

    def _targets(scope, kind, paths, album):
        if scope != "folder":
            return list(paths)
        if kind == "album":
            return [album]
        return sorted({os.path.dirname(p) for p in paths})

    pressed = 0
    problems = []
    outcomes = {}
    slow = []
    for kind in ("album", "track"):
        for script in [s for s in data["scripts"] if kind in s["applies_to"]]:
            presses = [(None, None)] + [
                (o["key"], o.get("owner_label") or o["key"])
                for o in (script.get("force", {}).get("options") or [])
                if o.get("key")]
            for key, _owner in presses:
                targets = _targets(script["scope"], kind, _tracks_now(), _album_now())
                body = {"ids": [script["id"]], "targets": targets}
                if key:
                    body["force"] = {key: True}
                where = f"{kind} {script['id']}" + (f" (force {key})" if key else "")
                t0 = time.monotonic()
                try:
                    r = CLIENT.post("/api/run", json=body)
                except Exception as e:  # a raise here is the app's, not ours
                    problems.append(f"{where}: raised {e!r}")
                    continue
                pressed += 1
                seconds = time.monotonic() - t0
                if seconds > 5.0:
                    slow.append((round(seconds, 1), where))
                if r.status_code != 200:
                    problems.append(f"{where}: HTTP {r.status_code} {r.text[:120]}")
                    continue
                res = (r.json().get("results") or [{}])[0]
                outcomes[(kind, script["id"], key)] = res
                error = str(res.get("error") or "")
                if error and not (script["id"] == 22 and "no API key" in error):
                    problems.append(f"{where}: {error[:160]}")
                elif not error and res.get("stats") is None and not res.get("skipped"):
                    problems.append(f"{where}: answered neither stats nor a skip")

    check(f"every menu entry presses through /api/run — album and track, forced "
          f"twins included ({pressed} presses)",
          not problems and pressed >= 2 * len(RUNNERS),
          " · ".join(problems[:5]) or f"only {pressed} presses")
    check("22 (Submit fingerprints) answers the runner's own sentence, never a "
          "silent skip",
          "no API key" in str((outcomes.get(("album", 22, None)) or {}).get("error")),
          str(outcomes.get(("album", 22, None)))[:160])
    # What the presses PROVED, not just that they answered: the passes that
    # write a tag wrote it, on a real album, through the menu's own request.
    def wrote(sid, key="modified_count"):
        """What one album press reported for *key*, or 0."""
        res = outcomes.get(("album", sid, None)) or {}
        return int(((res.get("stats") or {}).get(key)) or 0)
    # A pass whose TOOL this machine does not have cannot write anything, and
    # that is not a failure — it is the same "cannot run here" a suite reports
    # with exit 2, so it is SAID OUT LOUD here rather than dropped from the
    # loop. `mlo.deps.capabilities` is the app's OWN answer to "which tools are
    # here" (the Dependencies page renders it), so the sweep asks it instead of
    # keeping a second list of what each script needs. On a CI runner — no
    # Chromaprint, no vendored librosa — that is 12 and 21; on a machine with
    # both, every one of these assertions runs.
    from mlo import deps as _deps
    _caps = _deps.capabilities()
    _tool_feature = {12: "keybpm", 21: "acoustid"}
    for sid, what in ((12, "Key & BPM"), (21, "the AcoustID pair"),
                      (3, "the FLAC re-encode"), (1, "the lyrics format")):
        feature = _tool_feature.get(sid)
        row = _caps.get(feature) or {}
        if feature and not row.get("available"):
            print(f"  SKIP {sid} ({what}) cannot write here: "
                  f"{row.get('reason') or 'its tool is not installed'}")
            continue
        check(f"{sid} ({what}) really wrote something ({wrote(sid)})",
              wrote(sid) >= 1,
              str((outcomes.get(("album", sid, None)) or {}).get("stats"))[:160])
    graded = (outcomes.get(("album", 4, None)) or {}).get("stats") or {}
    check("4 (Grade) really graded the album",
          int(graded.get("total_scanned") or 0) >= 1
          and int(graded.get("albums_passed") or 0) + int(graded.get("albums_failed") or 0) >= 1,
          str(graded)[:200])
    if slow:
        print("  slowest presses: "
              + " · ".join(f"{w} {s}s" for s, w in sorted(slow, reverse=True)[:5]))
    socket.socket.connect = _connect
    time.sleep = _sleep
print(f"\n{'PASS' if not FAILED else 'FAIL'} — {len(FAILED)} problem(s)")
for name in FAILED:
    print(f"  - {name}")
sys.exit(1 if FAILED else 0)
