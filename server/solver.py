"""FlareSolverr as a managed dependency: the app's own Cloudflare solver.

Both scraped sources (RateYourMusic, Album of the Year) sit behind Cloudflare's
challenge, and only a cleared session cookie or a real browser gets past it
(see server/cfchallenge.py — a plain client, curl_cffi, headless Chromium and a
headed real Chrome were all measured getting the interstitial). This module is
the second half of that answer: FlareSolverr is installed from Dependencies (a
pinned upstream release — see mlo.fetchdeps), RUN here as a supervised child
process, and reached through the same `cf_solver_url` contract `cfchallenge`
already speaks, so nothing about the Wire format had to change.

Three ways a solver can exist, and this module resolves them in one order:

  1. `cf_solver_url` — the user pointed the app somewhere explicitly. It wins,
     always, and is never probed or overridden.
  2. `MLO_SOLVER_URL` / `FLARESOLVERR_URL` — the environment. This is how the
     shipped Docker setup wires the sidecar (`docker-compose.yml`): the app and
     the `flaresolverr` service share a network, and the env var is the URL of
     that service. No probe, no guess.
  3. The MANAGED process — this module's own child (`start`), which is what a
     host install (the desktop app, `dev.py`) uses.
  4. Anything already answering on the default local port — a Docker sidecar
     publishing 127.0.0.1:8191 on the host, or an orphaned child from a
     previous backend run. Probed only when 1–3 gave nothing, and CACHED
     (`_PROBE_TTL`) because `cfchallenge.solver_url` is asked once per album by
     the genre chain's skip checks.

`start`/`stop` mirror server/soulseek.py's slskd supervisor (the same
CREATE_NO_WINDOW rule — this process runs windowed and owns no console — and
the same "a spawn that never happened is a MESSAGE, not a 500" rule), minus
everything slskd needs and a stateless HTTP service does not: FlareSolverr
holds no config file this app must write, and any instance answering on the
port is usable, so there is nothing to adopt-vs-restart.

FlareSolverr reads `HOST`/`PORT` from its environment (VERIFIED against its
own `src/flaresolverr.py`: `int(os.environ.get('PORT', 8191))`), so the managed
child is pinned to loopback on MANAGED_PORT and never exposed.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

# The managed child's own address. Loopback on purpose: FlareSolverr drives a
# real browser and can be abused as an open proxy (upstream's own warning), so
# it must never be reachable from off this machine.
MANAGED_HOST = "127.0.0.1"
MANAGED_PORT = 8191
LOCAL_URL = f"http://{MANAGED_HOST}:{MANAGED_PORT}"

# CREATE_NO_WINDOW on Windows: the server runs windowed and owns no console, so
# any child without it allocates one and a terminal flashes up (mlo/subproc.py).
CREATE_NO_WINDOW = 0x08000000

# How long `start` waits for `/health` before calling it started-but-warming:
# FlareSolverr boots a WSGI server and TEST-LAUNCHES its browser at startup, so
# a cold start (first run, a cold filesystem cache) can take tens of seconds.
START_TIMEOUT = 90.0

# How long a "something answers on the local port" answer is trusted. Asked
# only when nothing explicit is configured, and never more than once per TTL,
# so the per-album skip checks in the genre chain cost at most one fast GET.
_PROBE_TTL = 20.0

_proc_lock = threading.RLock()
_proc = {"popen": None, "started_at": 0.0}
_probe = {"url": "", "at": 0.0}


def _cfg(cfg=None):
    if cfg is not None:
        return cfg or {}
    try:
        from mlo.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def installed():
    """The FlareSolverr executable this host could run, or None.

    Detection is mlo.tools', like every other dependency: it reads the app's
    tools folders and only reports a build THIS host can execute (a shared
    library folder holding a Linux `flaresolverr` must not be spawned by a
    Windows host — that wrong answer is the 500 slskd's own exe() guards).
    """
    try:
        from mlo.tools import detect_all_tools
        entry = (detect_all_tools() or {}).get("flaresolverr") or {}
        exe = entry.get("flaresolverr_exe")
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return None


def installed_version():
    """The installed FlareSolverr's version label, or None."""
    try:
        from mlo.tools import detect_all_tools
        entry = (detect_all_tools() or {}).get("flaresolverr") or {}
        return entry.get("version")
    except Exception:
        return None


def _log_path():
    try:
        from mlo.paths import app_data_dir
        folder = app_data_dir()
        if not folder:
            return ""
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, "flaresolverr.log")
    except Exception:
        return ""


def _last_log_line(path, limit=220):
    """The last non-empty line of a log file, trimmed — "" when unreadable."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()[-8192:]
        for line in reversed(data.decode("utf-8", "replace").splitlines()):
            if line.strip():
                return line.strip()[-limit:]
    except OSError:
        pass
    return ""


def health(url, timeout=2.0):
    """True when a FlareSolverr answers `/health` at *url*.

    `/health` is FlareSolverr's own probe endpoint (VERIFIED against its
    source: `@app.route('/health')`), chosen over `/` because `/health` is the
    one route upstream documents as not printing traces — a health poll must
    not fill the solver's log.
    """
    if not url:
        return False
    try:
        import httpx
        r = httpx.get(str(url).rstrip("/") + "/health", timeout=timeout)
    except Exception:
        return False
    return r.status_code == 200


def is_running():
    """Whether the process THIS app started is still alive."""
    with _proc_lock:
        p = _proc.get("popen")
        if p is None:
            return False
        if p.poll() is None:
            return True
        _proc["popen"] = None
    # The child died: a cached "something answers locally" from the probe may
    # be the child's own answer, so it must not outlive it (a stale URL would
    # send the scraped sources at a port nothing listens on any more).
    _probe["url"], _probe["at"] = "", 0.0
    return False


def run_pid():
    """The managed child's PID, or None."""
    with _proc_lock:
        p = _proc.get("popen")
    return p.pid if (p is not None and p.poll() is None) else None


def _managed_url():
    """The managed child's URL while it is alive, else ""."""
    return LOCAL_URL if is_running() else ""


def local_answer(timeout=1.5, ttl=_PROBE_TTL):
    """The default local URL when SOMETHING answers there, cached — else "".

    This is the adopted case: a Docker sidecar publishing 127.0.0.1:8191 on the
    host, or a FlareSolverr left running by a previous backend run. Cached for
    `ttl` so the per-album SOLVER checks never pay a probe each; pass `ttl=0`
    to force a fresh look (a user pressing Test or Start wants the truth).
    """
    now = time.time()
    if ttl and _probe["at"] and now - _probe["at"] < ttl:
        return _probe["url"]
    url = LOCAL_URL if health(LOCAL_URL, timeout=timeout) else ""
    _probe["url"], _probe["at"] = url, now
    return url


def resolve(cfg=None):
    """`(url, source)` — the solver to use and WHERE it came from.

    `source` is "config" | "env" | "managed" | "local" | "" and exists for the
    UI: a card that says which of the four decided the URL is what turns "no
    solver" into something the user can act on. `url` is "" when there is none.
    """
    cfg = _cfg(cfg)
    explicit = str((cfg or {}).get("cf_solver_url") or "").strip()
    if explicit:
        return explicit.rstrip("/"), "config"
    env = str(os.environ.get("MLO_SOLVER_URL")
              or os.environ.get("FLARESOLVERR_URL") or "").strip()
    if env:
        return env.rstrip("/"), "env"
    # Our own child and "something already listening on the default port" are
    # AMBIENT: consulted only for a config that came through the config layer,
    # which always carries these keys. A hand-built cfg that omits them means
    # "ask only what I gave you" — the rule `_rym_archive_on` applies to its
    # own key — so a unit test can never be answered by a solver that happens
    # to be running on the developer's machine.
    if "cf_solver_url" not in cfg and "cf_solver_autostart" not in cfg:
        return "", ""
    managed = _managed_url()
    if managed:
        return managed, "managed"
    found = local_answer()
    if found:
        return found, "local"
    return "", ""


def base_url(cfg=None):
    """The solver URL to ask, or "" — `resolve(cfg)[0]`, for cfchallenge."""
    return resolve(cfg)[0]


def autostart_enabled(cfg=None):
    """Whether the app may bring the managed solver up by itself.

    Ships ON: unlike slskd (a whole download client the user opts into), the
    solver has nothing to configure and only removes the cookie work the user
    said they hated. It does nothing at all when FlareSolverr is not installed.
    """
    value = _cfg(cfg).get("cf_solver_autostart")
    return True if value is None else bool(value)


def wait_until_ready(url=None, timeout=START_TIMEOUT, popen=None):
    """Poll `/health` until it answers, the child dies, or the budget runs out."""
    url = url or LOCAL_URL
    deadline = time.time() + max(1.0, float(timeout))
    while time.time() < deadline:
        if health(url, timeout=1.5):
            return True
        if popen is not None and popen.poll() is not None:
            return False
        time.sleep(0.5)
    return health(url, timeout=1.5)


def _kill_tree(proc):
    """Stop *proc* and, on Windows, the Chromium it may have spawned."""
    if proc is None:
        return
    if os.name == "nt":
        try:
            from mlo.subproc import run_tool
            # run_tool adds CREATE_NO_WINDOW itself: a bare taskkill would
            # flash a terminal window (mlo/subproc.py).
            run_tool(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                     capture_output=True, timeout=15)
            return
        except Exception:
            pass
    try:
        proc.terminate()
        proc.wait(timeout=15)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def start(cfg=None):
    """Start the managed FlareSolverr; returns `(started, message)`.

    Idempotent, and it ADOPTS rather than duplicates: when anything already
    answers on the managed address (a sidecar, an orphaned child), that is a
    running solver and no second one is spawned — one FlareSolverr per port.
    """
    with _proc_lock:
        if is_running():
            return True, "already running"
        if local_answer(ttl=0):
            return True, "already running"
        exe = installed()
        if not exe:
            return False, "FlareSolverr is not installed"
        env = dict(os.environ)
        env["HOST"] = MANAGED_HOST
        env["PORT"] = str(MANAGED_PORT)
        env.setdefault("LOG_LEVEL", "info")
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        log = _log_path()
        try:
            logf = open(log, "wb") if log else subprocess.DEVNULL
        except OSError:
            logf = subprocess.DEVNULL
        try:
            proc = subprocess.Popen(
                [exe], cwd=os.path.dirname(exe), env=env,
                stdout=logf, stderr=subprocess.STDOUT, **kwargs)
        except OSError as e:
            # A spawn that never happened is a MESSAGE, not a 500 (the slskd
            # rule): a tools folder shared with another host can hold a build
            # this one cannot execute.
            try:
                logf.close()
            except Exception:
                pass
            return False, (f"FlareSolverr could not be started "
                           f"({type(e).__name__}: {e}) — {exe} may be another "
                           f"host's build; install it again from Dependencies")
        try:
            logf.close()  # the child keeps its own handle
        except Exception:
            pass
        _proc["popen"] = proc
        _proc["started_at"] = time.time()
    if wait_until_ready(LOCAL_URL, START_TIMEOUT, popen=proc):
        _probe["url"], _probe["at"] = LOCAL_URL, time.time()
        return True, "started"
    if proc.poll() is not None:
        detail = _last_log_line(log)
        with _proc_lock:
            _proc["popen"] = None
        return False, (f"FlareSolverr exited immediately ({detail})"
                       if detail else
                       "FlareSolverr exited immediately — see its log")
    # Alive but silent (a very slow first boot, a browser still unpacking):
    # a valid answer, and the caller's first real solve will wait like any
    # other.
    return True, "started (still warming up)"


def stop(cfg=None):
    """Stop the process THIS app started; returns `(stopped, message)`.

    Only ever the child we spawned: an adopted solver (the Docker sidecar, or
    a service the user runs by hand) is not ours to kill, and the message says
    so rather than pretending.
    """
    with _proc_lock:
        proc = _proc.get("popen")
        _proc["popen"] = None
    if proc is None or proc.poll() is not None:
        if local_answer(ttl=0):
            return False, "not started by this app (leave it running)"
        return False, "not running"
    _kill_tree(proc)
    _probe["url"], _probe["at"] = "", 0.0
    return True, "stopped"


def ensure_started(cfg=None):
    """Bring the managed solver up for the app's own use, best effort.

    Called at boot (server/main.py) and from the UI's Start. Returns
    `(available, message)`: `available` is true when SOME solver answers —
    configured, adopted or just started — so a caller never has to care which
    of the four it was.
    """
    cfg = _cfg(cfg)
    if base_url(cfg):
        return True, "already available"
    if not autostart_enabled(cfg):
        return False, "autostart is off"
    if not installed():
        return False, "FlareSolverr is not installed"
    return start(cfg)


def status(cfg=None):
    """The Solver card's payload (Settings → Discovery)."""
    cfg = _cfg(cfg)
    exe = installed()
    url, source = resolve(cfg)
    return {
        "installed": bool(exe),
        "executable": exe or "",
        "version": installed_version(),
        "install_kind": _install_kind(),
        "install_note": _install_problem(),
        "running": is_running() or bool(local_answer(ttl=0)),
        "managed": is_running(),
        "pid": run_pid(),
        "url": url,
        "source": source,
        "autostart": autostart_enabled(cfg),
        "default_url": LOCAL_URL,
        "timeout_ms": _timeout_ms(cfg),
        "log": _log_path(),
    }


def _timeout_ms(cfg):
    try:
        from server import cfchallenge
        return int(cfchallenge.solver_timeout(cfg) * 1000)
    except Exception:
        return 60000


def _install_kind():
    try:
        from mlo.fetchdeps import install_kind
        return install_kind("flaresolverr")
    except Exception:
        return "unsupported"


def _install_problem():
    try:
        from mlo.fetchdeps import install_problem
        return install_problem("flaresolverr")
    except Exception:
        return None