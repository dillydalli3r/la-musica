#!/usr/bin/env python3
"""The affected-suite index (tools/affected_tests.py) itself.

It is the tool an agent trusts to shrink the handover gate from ~140 suites to
the handful that reach a change, so a wrong answer is a change that ships
untested. What is pinned:

  * `module_of` reads a repo-relative path to its import name, and refuses
    anything that is not a flat `server/*.py` / `mlo/*.py` module;
  * `modules_in` sees BOTH import spellings the suites use (`import server.x`
    and `from server import a, b`);
  * the built index carries the two known edges: `server/plays.py` is reached
    by test_charts.py, `mlo/discs.py` by test_grading_paths.py;
  * an unindexed path is REPORTED, never silently dropped, and a run with no
    arguments prints usage instead of an empty list.
"""

import io
import importlib.util
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILED = []


def check(label, cond, detail=""):
    if cond:
        print(f"ok: {label}")
    else:
        FAILED.append(label)
        print(f"FAIL {label}" + (f" — {detail}" if detail else ""))


spec = importlib.util.spec_from_file_location("affected_tests", ROOT / "tools" / "affected_tests.py")
affected = importlib.util.module_from_spec(spec)
spec.loader.exec_module(affected)

# --------------------------------------------------------------------------- #
print("== path -> module ==")
check("server/plays.py is server.plays", affected.module_of("server/plays.py") == "server.plays")
check("mlo/discs.py is mlo.discs", affected.module_of("mlo/discs.py") == "mlo.discs")
check("a web file is not a module", affected.module_of("web/src/api.ts") is None)
check("a nested server file is not a flat module",
      affected.module_of("server/beets/plugin.py") is None,
      repr(affected.module_of("server/beets/plugin.py")))

# --------------------------------------------------------------------------- #
print("\n== both import spellings ==")
dotted = affected.modules_in("import server.plays as plays\n")
names = affected.modules_in("from mlo import grader, discs\n")
check("`import server.plays` is read", "server.plays" in dotted, repr(dotted))
check("`from mlo import grader, discs` names both",
      {"mlo.grader", "mlo.discs"} <= names, repr(names))

# --------------------------------------------------------------------------- #
print("\n== the index's known edges ==")
by_module, by_stem = affected.build_index()
check("test_charts.py reaches server.plays",
      "test_charts.py" in by_module.get("server.plays", set()),
      repr(sorted(by_module.get("server.plays", set()))))
check("test_grading_paths.py reaches mlo.discs",
      "test_grading_paths.py" in by_module.get("mlo.discs", set()),
      repr(sorted(by_module.get("mlo.discs", set()))))
check("the same-name fallback keeps test_plays.py for plays",
      "test_plays.py" in by_stem.get("plays", set()),
      repr(sorted(by_stem.get("plays", set()))))

# --------------------------------------------------------------------------- #
print("\n== the command line ==")
buf = io.StringIO()
with redirect_stdout(buf):
    code = affected.main(["affected_tests.py", "server/plays.py", "docs/README.md"])
out = buf.getvalue()
check("a known module lists a suite", "tools/test_charts.py" in out, out[:200])
check("a non-module path is reported as such",
      "not a flat server/ or mlo/ module" in out and "docs/README.md" in out, out[:300])
check("that run still exits 0", code == 0, str(code))

buf = io.StringIO()
with redirect_stdout(buf):
    affected.main(["affected_tests.py", "server/no_such_store.py"])
out = buf.getvalue()
check("a real module with no suite is reported as unindexed",
      "no suite imports these" in out and "server/no_such_store.py" in out, out[:300])

buf = io.StringIO()
with redirect_stdout(buf):
    code = affected.main(["affected_tests.py"])
check("no arguments prints usage and exits 2",
      code == 2 and "Usage:" in buf.getvalue(), str(code))

# --------------------------------------------------------------------------- #
print(f"\n{'FAILED: ' + ', '.join(FAILED) if FAILED else 'all checks passed'}")
sys.exit(1 if FAILED else 0)