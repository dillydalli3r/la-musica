#!/usr/bin/env python3
"""The check / script stack (server/api_stack.py) — described from code, edited in one place.

What this pins, over a TEMP config (the developer's real config.json is never
written):

  * the description is DERIVED, not a second list: the script ids are
    ``server.script_runners.RUNNERS``, the labels/descriptions the menu table
    ``mlo.cli.SCRIPTS``, the check ids ``server.tags_registry.registry()`` —
    itself built from DEFAULT_CONFIG — and the grader's own gate set
    (``mlo.grader.check_gates()``, read out of mlo/grader.py's source). The
    three sets must be equal, with no orphans on either side: a check the
    grader reads that nothing can set, or a check DEFAULT_CONFIG holds that no
    grader step reads, fails here instead of hiding.
  * the groups and the "relaxed" preset match web/src/pages/GradingPage.tsx
    key for key, so the MAINTAIN page and the Grading page can never disagree
    about where a check belongs or what a preset turns off.
  * every audit step DEFAULT_CONFIG holds is READ by mlo.audit, and the
    detector steps are mlo.audit's own DETECTOR_NO_FLAGS table.
  * PUT writes the config keys the grader itself reads (`grade_check_*`,
    `run_all_order`, the per-script feature switch) and an unknown id is
    refused with 400 while nothing at all is written.

Run: python tools/test_check_stack.py   (exit 0 pass, 1 fail, 2 skip)
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
# is imported — the same recipe tools/test_artist_grading.py uses. Nothing is
# ever written to the developer's own music folder or repo config.json.
# --------------------------------------------------------------------------- #
REAL_MUSIC_FOLDER = ""
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        REAL_MUSIC_FOLDER = str((json.load(fh) or {}).get("music_folder") or "")
except Exception:
    pass

REDIRECT = tempfile.mkdtemp(prefix="mlo-stack-redirect-")
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
from mlo.config import DEFAULT_CONFIG, load_config  # noqa: E402
from mlo.grader import check_gates  # noqa: E402
from server import api_stack, script_runners  # noqa: E402
from server.script_runners import RUNNERS  # noqa: E402
from server.tags_registry import registry  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}"
          f"{('  — ' + str(detail)) if detail and not ok else ''}")
    if not ok:
        FAILED.append(name)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def words(text):
    return set(re.findall(r"[a-z0-9]+", str(text).lower().replace("&", "and")))


app = FastAPI()
app.include_router(api_stack.router)
client = TestClient(app)

MENU = {sid: (name, desc) for sid, name, desc in SCRIPTS}


def ts_groups():
    """GradingPage.tsx's own groups: id -> (title, check keys)."""
    src = read("web/src/pages/GradingPage.tsx")
    block = src[src.index("const GROUPS"):src.index("/** What each check means")]
    out = {}
    for m in re.finditer(r'\{\s*id:\s*"(\w+)",\s*title:\s*"([^"]+)",(.*?)\n  \},',
                         block, re.S):
        out[m.group(1)] = (m.group(2), re.findall(r'"(grade_[a-z_]+)"', m.group(3)))
    return out


def ts_relaxed_off():
    src = read("web/src/pages/GradingPage.tsx")
    m = re.search(r"const relaxedOff = \[(.*?)\];", src, re.S)
    return set(re.findall(r'"(grade_[a-z_]+)"', m.group(1))) if m else set()


def put(payload):
    return client.put("/api/stack", json=payload)


def row(resp, key):
    """One check row out of a stack payload."""
    return [c for c in resp["stack"]["checks"] if c["key"] == key][0]


def srow(resp, sid):
    return [s for s in resp["stack"]["scripts"] if s["id"] == sid][0]


# --------------------------------------------------------------------------- #
print("== GET: the described stack is the code's stack ==")
r = client.get("/api/stack")
check("GET /api/stack answers", r.status_code == 200, f"HTTP {r.status_code}")
stack = r.json()
cfg = load_config()

scripts = stack["scripts"]
ids = sorted(s["id"] for s in scripts)
check("script ids == RUNNERS", ids == sorted(RUNNERS), f"{ids} vs {sorted(RUNNERS)}")
check("script ids == mlo.cli.SCRIPTS", ids == sorted(MENU), f"{ids} vs {sorted(MENU)}")
check("script ids cover the default Run All order",
      sorted(DEFAULT_CONFIG["run_all_order"]) == ids)
check("every script carries the menu label and a description",
      all(s["label"] == MENU[s["id"]][0] and s["description"] == MENU[s["id"]][1]
          for s in scripts))
check("every script's label names the script RUNNERS labels",
      all(words(s["label"]) & words(RUNNERS[s["id"]][0]) for s in scripts),
      [s["id"] for s in scripts if not words(s["label"]) & words(RUNNERS[s["id"]][0])])
order = stack["run_all_order"]
check("the chain order is the live run_all_order", order == cfg["run_all_order"],
      f"{order} vs {cfg['run_all_order']}")
check("in_order/order agree with the chain",
      all((s["in_order"] is (s["id"] in order)) and
          (s["order"] == (order.index(s["id"]) if s["id"] in order else None))
          for s in scripts))
check("enabled == in the chain AND the feature switch is on",
      all(s["enabled"] == (s["in_order"] and s["gate"]["enabled"])
          for s in scripts))
check("only the ids the app re-anchors are marked non-removable",
      sorted(s["id"] for s in scripts if not s["removable"])
      == sorted(api_stack._anchored_ids())
      and 4 in [s["id"] for s in scripts if s["removable"]],
      [s["id"] for s in scripts if not s["removable"]])
check("gates are mlo.cli.SCRIPT_GATES and match the server's own runner table",
      SCRIPT_GATES == script_runners._DISABLED,
      f"{SCRIPT_GATES} vs {script_runners._DISABLED}")
check("script 18 reports its feature switch",
      any(s["id"] == 18 and s["gate"]["keys"] == ["lrclib_auto_publish"]
          for s in scripts))

checks = stack["checks"]
keys = [c["key"] for c in checks]
reg = registry()
reg_keys = [c["key"] for c in reg["checks"]]
check("check ids == the registry (DEFAULT_CONFIG)", keys == reg_keys,
      f"only in stack: {sorted(set(keys) - set(reg_keys))}, "
      f"only in registry: {sorted(set(reg_keys) - set(keys))}")
check("check ids == the gates mlo.grader reads out of its own source",
      sorted(keys) == check_gates(),
      f"only in stack: {sorted(set(keys) - set(check_gates()))}, "
      f"only in grader: {sorted(set(check_gates()) - set(keys))}")
check("the payload carries the grader's gate set too",
      stack["grader_gates"] == check_gates())
check("no check is unlabelled in the registry",
      "checks_unlabelled" not in reg, reg.get("checks_unlabelled"))
check("every check has a label, a description, a default and an enabled flag",
      all(c["label"] and c["description"] and isinstance(c["default"], bool)
          and c["enabled"] == bool(cfg.get(c["key"], c["default"]))
          for c in checks),
      [c["key"] for c in checks if not (c["label"] and c["description"])])
check("defaults are DEFAULT_CONFIG's",
      all(c["default"] == bool(DEFAULT_CONFIG[c["key"]]) for c in checks))

groups = {g["id"]: g for g in stack["groups"]}
check("8 groups are described", len(groups) == 8, sorted(groups))
check("every check is in exactly one group",
      sorted(k for g in groups.values() for k in g["keys"]) == sorted(keys),
      [c["key"] for c in checks if c["group"] not in groups])

tg = ts_groups()
check("the Grading page's own groups were parsed", len(tg) == 8, sorted(tg))
ts_map = {k: gid for gid, (_, ks) in tg.items() for k in ks}
ts_titles = {gid: title for gid, (title, _) in tg.items()}
check("group ids match the Grading page", sorted(tg) == sorted(groups), sorted(tg))
check("group titles match the Grading page",
      {gid: groups[gid]["title"] for gid in groups} == ts_titles,
      {gid: (groups[gid]["title"], ts_titles.get(gid)) for gid in groups})
mismatch = [(c["key"], c["group"], ts_map.get(c["key"], "other"))
            for c in checks if c["group"] != ts_map.get(c["key"], "other")]
check("every check sits in the same group as on the Grading page",
      not mismatch, mismatch)

presets = {p["id"]: p for p in stack["presets"]}
check("presets are strict/balanced/relaxed",
      sorted(presets) == ["balanced", "relaxed", "strict"], sorted(presets))
check("preset membership is preset_value's answer",
      all((c["key"] in presets[pid]["keys"])
          == api_stack.preset_value(pid, c["key"], c["default"])
          for c in checks for pid in presets))
check("the relaxed preset turns off exactly GradingPage's relaxedOff list",
      api_stack.RELAXED_OFF == ts_relaxed_off(),
      f"stack-only: {sorted(api_stack.RELAXED_OFF - ts_relaxed_off())}, "
      f"page-only: {sorted(ts_relaxed_off() - api_stack.RELAXED_OFF)}")

from mlo.audit import DETECTOR_NO_FLAGS  # noqa: E402

steps = stack["audit"]["steps"]
step_keys = [s["key"] for s in steps]
dflt_audit = sorted(k for k in DEFAULT_CONFIG if str(k).startswith("audit_"))
check("every audit_* key of DEFAULT_CONFIG is a step",
      sorted(step_keys) == sorted(dflt_audit),
      f"only in stack: {sorted(set(step_keys) - set(dflt_audit))}, "
      f"only in config: {sorted(set(dflt_audit) - set(step_keys))}")
check("every audit step is READ by mlo.audit", all(s["used"] for s in steps),
      [s["key"] for s in steps if not s["used"]])
check("the detector steps are mlo.audit's own table",
      sorted(s["key"] for s in steps if s["flag"]) == sorted(DETECTOR_NO_FLAGS),
      [s["key"] for s in steps if s["flag"]])
check("every audit step carries the live value",
      all(s["enabled"] == cfg.get(s["key"], s["default"]) for s in steps))
check("the audit section names script 6",
      stack["audit"]["script"]["id"] == 6
      and stack["audit"]["script"]["label"] == MENU[6][0])

# --------------------------------------------------------------------------- #
print("== PUT: edits land on the keys the code reads ==")
r = put({"checks": {"grade_check_mood": False}})
check("a check toggle is accepted", r.status_code == 200, r.text[:200])
saved = load_config()
check("the grader's own key now reads False",
      saved["grade_check_mood"] is False
      and saved.get("grade_check_mood", True) is False)
check("the response says which key changed",
      r.json()["changed"] == ["grade_check_mood"], r.json()["changed"])
check("the returned stack reflects it",
      [c for c in r.json()["stack"]["checks"] if c["key"] == "grade_check_mood"][0]
      ["enabled"] is False)
check("the toggle survives a re-read", row(put({}).json(), "grade_check_mood")
      ["enabled"] is False)

# The list is what a client PUTs as a whole chain, so it has to be a complete,
# anchor-consistent order: normalize_config re-inserts every registry id the
# saved order does not name (20 and 21 among them, at their anchors), and the
# check below is that a full order survives verbatim.
order_new = [4, 1, 11, 3, 14, 15, 2, 13, 18, 17, 8, 5, 19, 6, 7, 9, 12, 16, 10, 20, 21]
r = put({"order": order_new})
check("a new chain order is accepted", r.status_code == 200, r.text[:200])
check("run_all_order now holds exactly that order",
      load_config()["run_all_order"] == order_new, load_config()["run_all_order"])
check("the returned stack agrees",
      r.json()["stack"]["run_all_order"] == order_new)

r = put({"order": [4, 1]})
got = load_config()["run_all_order"]
check("a partial order keeps its sequence and completes the rest",
      got[:2] == [4, 1] and sorted(got) == sorted(RUNNERS), got)

r = put({"scripts": {"7": {"enabled": False}}})
check("a gated script is switched off through its own feature switch",
      r.status_code == 200 and load_config()["dr_replaygain_enabled"] is False
      and 7 in load_config()["run_all_order"], r.text[:200])
check("the returned stack marks it off", srow(r.json(), 7)["enabled"] is False)
put({"scripts": {"7": {"enabled": True}}})
check("and back on", load_config()["dr_replaygain_enabled"] is True
      and srow(put({}).json(), 7)["enabled"] is True)

r = put({"scripts": {"16": {"enabled": False}}})
check("an anchored script with a feature switch goes through it too",
      r.status_code == 200 and load_config()["mood_enabled"] is False
      and 16 in load_config()["run_all_order"], r.text[:200])
put({"scripts": {"16": {"enabled": True}}})

r = put({"scripts": {"4": {"enabled": False}}})
check("an un-gated script leaves the chain", r.status_code == 200
      and 4 not in load_config()["run_all_order"], r.text[:200])
put({"scripts": {"4": {"enabled": True}}})
check("and rejoins it", 4 in load_config()["run_all_order"])

r = put({"scripts": {"13": {"enabled": False}}})
check("a script the app re-anchors and cannot switch off is refused",
      r.status_code == 400, r.text[:200])

r = put({"scripts": {"4": {"order": 0}}})
check("a script can be positioned", r.status_code == 200
      and load_config()["run_all_order"][0] == 4,
      load_config()["run_all_order"][:3])

r = put({"scripts": {"18": {"gate_enabled": False}}})
check("a script's feature switch is writable",
      r.status_code == 200 and load_config()["lrclib_auto_publish"] is False,
      r.text[:200])
put({"scripts": {"18": {"gate_enabled": True}}})
check("and the script is no longer gated off",
      load_config()["lrclib_auto_publish"] is True)
r = put({"scripts": {"4": {"gate_enabled": False}}})
check("a script with no feature switch refuses the edit", r.status_code == 400,
      r.text[:200])

r = put({"audit": {"audit_thorough": False}})
check("an audit step toggles the key mlo.audit reads",
      r.status_code == 200 and load_config()["audit_thorough"] is False,
      r.text[:200])
put({"audit": {"audit_thorough": True}})

r = put({"preset": "strict"})
strict = load_config()
check("strict turns every real check on",
      all(strict[c["key"]] for c in checks
          if not c["key"].startswith("grade_include_")))
check("strict leaves the file-category permissions alone",
      all(strict[c["key"]] is c["default"] for c in checks
          if c["key"].startswith("grade_include_")))
r = put({"preset": "balanced"})
check("balanced restores the factory defaults",
      all(load_config()[c["key"]] is c["default"] for c in checks))
r = put({"preset": "relaxed"})
relaxed = load_config()
check("relaxed switches off exactly its own list",
      all(relaxed[k] is False for k in api_stack.RELAXED_OFF)
      and all(relaxed[c["key"]] is c["default"] for c in checks
              if c["key"] not in api_stack.RELAXED_OFF))

print("== PUT: an unknown id is refused, and nothing is written ==")
before = load_config()
for label, payload in (
    ("check", {"checks": {"grade_check_nonsense": True}}),
    ("script id", {"order": [4, 99]}),
    ("script entry", {"scripts": {"99": {"enabled": False}}}),
    ("script entry", {"scripts": {"nope": {"enabled": False}}}),
    ("script id", {"scripts": {"99": {"order": 0}}}),
    ("audit step", {"audit": {"audit_nonsense": False}}),
    ("preset", {"preset": "loose"}),
):
    r = put(payload)
    check(f"unknown {label} is a 400", r.status_code == 400, f"HTTP {r.status_code}")
check("a refused edit wrote nothing", load_config() == before,
      [k for k in before if before[k] != load_config().get(k)])

r = put({})
check("an empty edit is a no-op that still answers",
      r.status_code == 200 and r.json()["changed"] == [], r.text[:200])
check("the config file written is the temp one",
      cfgmod.active_config_file().startswith(REDIRECT),
      cfgmod.active_config_file())

print("== wiring ==")
main_src = read("server/main.py")
if "api_stack" not in main_src:
    print("  NOTE server/main.py does not register server.api_stack's router yet"
          " — the coordinator wires it into the include_router block")

print(f"\n{'PASS' if not FAILED else 'FAIL'} — {len(FAILED)} problem(s)")
sys.exit(1 if FAILED else 0)
