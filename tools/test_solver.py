#!/usr/bin/env python
"""The Cloudflare solver: the managed FlareSolverr, its URL, and its install.

FlareSolverr is not a per-file tool — it is a SERVICE this app installs from
the same pinned-release machinery every other dependency uses and RUNS itself
(server/solver.py). Nothing here reaches the network: `health` (the only HTTP
call) is stubbed, `subprocess.Popen` is a fake, and `installed()` points at a
path that does not have to exist. What this suite proves is the wiring the app
controls — the asset a host picks, which of the four URL sources wins, that a
start is adopt-not-duplicate, that Stop only ever kills our own child, and that
`cfchallenge.solver_url` returns what the solver module resolved.

Run:  python tools/test_solver.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import fetchdeps                                                    # noqa: E402
from server import cfchallenge, solver                                       # noqa: E402

PASS = 0
FAIL = 0


def ok(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAILED: {label}")


# --------------------------------------------------------------------------- #
# 1) The install tables (mlo.fetchdeps) — what each host downloads
# --------------------------------------------------------------------------- #
ok(fetchdeps.install_kind("flaresolverr", platform="windows") == "deps",
   "Windows fetches the pinned zip")
ok(fetchdeps.install_kind("flaresolverr", platform="linux",
                          machine="x86_64") == "deps",
   "Linux x64 fetches the pinned tarball")
ok(fetchdeps.install_kind("flaresolverr", platform="linux",
                          machine="aarch64") == "unsupported",
   "Linux arm64 has no build to fetch (upstream ships x64 only)")
ok(fetchdeps.install_kind("flaresolverr", platform="macos") == "unsupported",
   "macOS has no build to fetch")

problem_mac = fetchdeps.install_problem("flaresolverr", platform="macos")
ok(problem_mac and "Docker" in problem_mac,
   f"the macOS row names Docker as the route ({problem_mac})")
ok(fetchdeps.install_problem("flaresolverr", platform="linux",
                             machine="aarch64") is not None
   and "Docker" in fetchdeps.install_problem("flaresolverr", platform="linux",
                                             machine="aarch64"),
   "the ARM row names Docker too")

# One pinned release, and the pin's face is the Windows asset — the Linux host
# takes the same tag's tarball through LINUX_BINARIES (the slskd shape).
pin = fetchdeps.PINNED["flaresolverr"]
ok(pin["tag"] == "v3.5.2" and pin["version"] == "3.5.2",
   f"the pin is the release the container also runs ({pin})")
ok(fetchdeps.installable("flaresolverr", platform="windows"),
   "a Windows host may install it")
ok(fetchdeps.installable("flaresolverr", platform="linux", machine="x86_64"),
   "a Linux x64 host may install it")

# The executable each host runs INSIDE the install folder.
ok(fetchdeps.run_name("flaresolverr", platform="windows") == "flaresolverr.exe",
   "Windows runs flaresolverr.exe")
ok(fetchdeps.run_name("flaresolverr", platform="linux") == "flaresolverr",
   "Linux runs the bare binary")
ok(fetchdeps.markers("flaresolverr", platform="linux") == ("flaresolverr",),
   "the Linux install is finished when the bare binary is there")

# The bulk pass must not fetch a ~300 MB service nobody asked for; its own row
# still can.
ok("flaresolverr" in fetchdeps.OPT_IN_TOOLS,
   "the bulk install leaves FlareSolverr alone")
ok("flaresolverr" in fetchdeps.installable_keys(),
   "its own row can still install it")


# --------------------------------------------------------------------------- #
# 2) URL resolution — config > environment > our child > something listening
# --------------------------------------------------------------------------- #
class _FakeProc:
    def __init__(self):
        self.pid = 4242
        self._rc = None
        self.terminated = False

    def poll(self):
        return self._rc

    def terminate(self):
        self.terminated = True
        self._rc = 0

    def wait(self, timeout=None):
        return self._rc

    def kill(self):
        self._rc = -9


_real = {
    "installed": solver.installed,
    "health": solver.health,
    "Popen": solver.subprocess.Popen,
    "log_path": solver._log_path,
    "kill_tree": solver._kill_tree,
    "env": dict(os.environ),
}
_fake = {"popen": None, "healthy": False, "started": 0, "killed": 0}


def _fake_popen(*args, **kwargs):
    _fake["popen"] = _FakeProc()
    _fake["started"] += 1
    return _fake["popen"]


def _fake_kill(proc):
    """Stand in for solver._kill_tree: record the call and end the fake child.

    The real one uses `taskkill /T` on Windows, which the fake Popen cannot
    observe — what matters here is that Stop asks for the tree to die and that
    the solver then treats the child as gone.
    """
    _fake["killed"] += 1
    proc.terminate()


def reset(installed=True, healthy=False):
    _fake["popen"] = None
    solver._proc["popen"] = None
    solver._proc["started_at"] = 0.0
    solver._probe["url"], solver._probe["at"] = "", 0.0
    solver.installed = (lambda: "C:/tools/flaresolverr v3.5.2/flaresolverr.exe"
                        if installed else None)
    # `healthy` models a solver that was ALREADY there; a child this test
    # spawned counts as answering from the moment it is alive, so `start`'s
    # readiness wait returns at once instead of sitting out START_TIMEOUT.
    solver.health = lambda url, timeout=2.0: bool(
        healthy or (_fake["popen"] is not None and _fake["popen"].poll() is None))
    solver._log_path = lambda: ""
    solver._kill_tree = _fake_kill
    solver.subprocess.Popen = _fake_popen
    _fake["started"] = 0
    _fake["killed"] = 0
    for key in ("MLO_SOLVER_URL", "FLARESOLVERR_URL"):
        os.environ.pop(key, None)


try:
    # A cfg with the keys `load_config` always carries. The AMBIENT sources
    # (our child, whatever answers on the port) are consulted only for such a
    # config — a hand-built dict that omits them means "ask only what I gave
    # you" (see solver.resolve), which is what keeps this suite hermetic.
    REAL = {"cf_solver_url": "", "cf_solver_autostart": True}

    # Nothing configured and nothing listening: no URL, no source.
    reset()
    ok(solver.resolve({}) == ("", ""), f"nothing -> no solver {solver.resolve({})}")

    # A configured URL wins and is never probed.
    os.environ["MLO_SOLVER_URL"] = "http://env-solver:8191"
    ok(solver.resolve({"cf_solver_url": "http://configured:9999/"})
       == ("http://configured:9999", "config"),
       "cf_solver_url beats the environment and is normalised")
    # …then the environment (what the shipped compose sets).
    ok(solver.resolve({}) == ("http://env-solver:8191", "env"),
       "MLO_SOLVER_URL is used with no config")
    os.environ.pop("MLO_SOLVER_URL")
    os.environ["FLARESOLVERR_URL"] = "http://fs:8191"
    ok(solver.resolve({}) == ("http://fs:8191", "env"),
       "FLARESOLVERR_URL is the alternate spelling")
    os.environ.pop("FLARESOLVERR_URL")

    # …then the child THIS app started.
    reset()
    ok(solver.start() == (True, "started"), "start reports started")
    ok(_fake["started"] == 1, "exactly one process was spawned")
    ok(solver.resolve(REAL) == (solver.LOCAL_URL, "managed"),
       f"a running child supplies the URL {solver.resolve(REAL)}")

    # Start is adopt-not-duplicate.
    ok(solver.start() == (True, "already running"), "a second start adopts")
    ok(_fake["started"] == 1, "no second process")

    # Stop kills only OUR child, and the URL goes with it.
    ok(solver.stop() == (True, "stopped"), "stop stops our child")
    ok(_fake["killed"] == 1, "stop asked the child's tree to die")
    ok(solver.resolve(REAL) == ("", ""), "nothing answers after Stop")
    ok(solver.stop()[0] is False, "a second Stop is a no-op")

    # Adopted case: something already listening (a sidecar). Start must not
    # spawn a second FlareSolverr, and Stop must not kill what it did not start.
    reset(healthy=True)
    ok(solver.resolve(REAL) == (solver.LOCAL_URL, "local"),
       "an already-listening solver is adopted")
    ok(solver.start() == (True, "already running"),
       "start adopts the listening solver")
    ok(_fake["started"] == 0, "no process was spawned over it")
    ok(solver.stop()[0] is False, "stop refuses to kill a solver it did not start")

    # A dead child is noticed, not reported as running.
    reset()
    solver.start()
    _fake["popen"]._rc = 1
    ok(not solver.is_running(), "a dead child is not 'running'")
    ok(solver.resolve(REAL) == ("", ""), "and supplies no URL")

    # Not installed: start is a message, never a spawn.
    reset(installed=False)
    ok(solver.start() == (False, "FlareSolverr is not installed"),
       "start without an install says so")
    ok(_fake["started"] == 0, "and spawns nothing")
    ok(solver.ensure_started({}) == (False, "FlareSolverr is not installed"),
       "ensure_started is honest when there is nothing to run")
    ok(solver.ensure_started({"cf_solver_autostart": False})
       == (False, "autostart is off"),
       "autostart off short-circuits")
    ok(solver.start_after_install({}) == (False, "FlareSolverr is not installed"),
       "and so is the post-install start")

    # …but a finished INSTALL starts it, with no press and regardless of the
    # boot switch: installing the solver IS the user saying they want it.
    reset()
    ok(solver.start_after_install({"cf_solver_autostart": False})
       == (True, "started"),
       "the install starts the solver even with autostart off")
    ok(_fake["started"] == 1, "and it really spawned one")

    # The sidecar case: something already answers, so the local install must
    # not spawn a second FlareSolverr next to it.
    reset(healthy=True)
    ok(solver.start_after_install(REAL) == (True, "already available"),
       "an install beside a running sidecar leaves it alone")
    ok(_fake["started"] == 0, "no duplicate process")

    # Status payload for the card.
    reset()
    st = solver.status({})
    ok(st["installed"] is True and st["source"] == "" and st["url"] == "",
       f"status of an idle install {st}")
    ok(st["default_url"] == solver.LOCAL_URL and st["autostart"] is True,
       "the card is told the default URL and the autostart default")

    # 3) cfchallenge.solver_url delegates to the resolution above.
    reset()
    os.environ["MLO_SOLVER_URL"] = "http://env-solver:8191"
    ok(cfchallenge.solver_url({}) == "http://env-solver:8191",
       "cfchallenge asks the solver module")
    os.environ.pop("MLO_SOLVER_URL")
    ok(cfchallenge.solver_url({"cf_solver_url": "http://only-me/"})
       == "http://only-me", "…and still honours an explicit config value")
    reset()
    ok(cfchallenge.solver_url({}) == "",
       "…and answers '' for a cfg that omits the key entirely")
    ok(cfchallenge.solver_url(REAL) == "",
       "…and for a real config with no solver running")
finally:
    solver.installed = _real["installed"]
    solver.health = _real["health"]
    solver.subprocess.Popen = _real["Popen"]
    solver._log_path = _real["log_path"]
    solver._kill_tree = _real["kill_tree"]
    solver._proc["popen"] = None
    solver._probe["url"], solver._probe["at"] = "", 0.0
    os.environ.clear()
    os.environ.update(_real["env"])

print(f"solver: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)