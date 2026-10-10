"""Live pages through a real browser the user already has (Chrome, or Edge).

WHY THIS EXISTS — MEASURED, 2026-10-10, on the owner's machine:

  * FlareSolverr's bundled Chromium CANNOT pass either scraped host's
    challenge. "Error solving the challenge. Timeout after 60.0 seconds", every
    attempt, on a clean process with 20 GB free — the module-level note in
    `server/cfchallenge.py` said so, and re-measuring said so again.
  * A CLEARED COOKIE alone is not enough for RateYourMusic. Read out of the
    owner's own Firefox — fresh, unexpired, from a visit minutes old — and sent
    with that browser's exact User-Agent, the RYM request is still `403 Just a
    moment...`; over `httpx`, and over `curl_cffi` impersonating Firefox 135,
    144 and 157 alike. AOTY, on the same setup, answers `200` with the real
    page — so the two hosts are not the same problem, and cookies fix only one.
  * A REAL browser passes both, with no human in the loop: a plain Chrome, a
    plain Firefox, launched at the URL, clear the challenge themselves in
    seconds and keep the `cf_clearance` that did it.
  * HEADLESS is detected: `--headless=new` Chrome and `--headless` Firefox both
    end on the interstitial. A real window is load-bearing, so this module
    opens one — positioned off to the side (`WINDOW_POSITION`), never hidden,
    because a hidden window is exactly what the challenge refuses.

So this is the third route for a scraped host: after the plain HTTP GET with
whatever clearance is in hand, and INSTEAD of the solver (which cannot pass),
the page is fetched by driving a browser the user already trusts, over its
DevTools protocol. The profile is the module's own, under the app's state dir,
so the clearance it earns survives to the next run — the second import of a
session pays no challenge at all.

Nothing here changes what the scrapers do with a page: it is the same HTML the
plain path would have received, handed back in `fetch_page`'s own shape.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request

# Chrome and Edge, in the order they are tried. Both are Chromium and both
# speak the same protocol; Edge is the one Windows always has.
_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

# One loopback port for the app's own browser, so a second call ADOPTS a
# running one instead of spawning a rival (the rule `server/solver.py` applies
# to FlareSolverr). Fixed, and unlikely to collide with a debugging port a
# developer is already using.
MANAGED_PORT = 9333

# Where the window goes. Off to the side rather than hidden: the challenge
# measures what a hidden window looks like, and an off-screen window is still a
# real one. On a single-monitor desktop this is simply out of view.
WINDOW_POSITION = "2400,40"
WINDOW_SIZE = "1000,720"

# How long to let a challenge clear before giving up on that page. A real
# browser clears these in a few seconds; the ceiling is for a slow first visit,
# which also has to unpack the profile.
CLEAR_TIMEOUT = 45.0
# How long a navigation has to reach "complete" before the page is read anyway.
NAV_TIMEOUT = 60.0

# The same marker `cfchallenge` matches, kept here so this module does not
# import it (it is imported BY it — a cycle here would be a real one).
_CHALLENGE_RE = None

_lock = threading.Lock()
_proc = {"popen": None, "started_at": 0.0}


def _challenge(text: str) -> bool:
    global _CHALLENGE_RE
    if _CHALLENGE_RE is None:
        import re
        _CHALLENGE_RE = re.compile(
            r"just a moment|cf-?challenge|_cf_chl|checking your browser|"
            r"enable javascript and cookies|attention required", re.I)
    return bool(text and _CHALLENGE_RE.search(text[:4000]))


def browser_exe():
    """The Chromium browser this host can drive, or "".

    Chrome first, then Edge: Edge is the one every Windows machine has, Chrome
    is the one most users are already signed in to.
    """
    for raw in _CANDIDATES:
        path = raw if os.path.isabs(raw) else os.path.join(
            os.environ.get("LOCALAPPDATA") or "", raw.split("LOCALAPPDATA\\", 1)[-1])
        if os.path.isfile(path):
            return path
    return ""


def enabled(cfg=None):
    """Whether the user allows a browser fetch (`cf_browser_fetch`).

    The rule `cfchallenge.ensure_solver` applies to a hand-built cfg: a cfg that
    does not CARRY the key means "ask only what I gave you", so a unit test's
    own cfg gets no browser and cannot silently start one on the machine
    running it. `cfg=None` is the same answer — this never goes looking for a
    config file of its own, so the route is taken only where a caller passed
    the live config (`mlo.config.normalize_config` fills `DEFAULT_CONFIG` in,
    so every real one carries the key).
    """
    if not cfg or "cf_browser_fetch" not in cfg:
        return False
    value = cfg.get("cf_browser_fetch")
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def available(cfg=None):
    """Whether a browser fetch can happen here: ALLOWED, and a browser exists."""
    return enabled(cfg) and bool(browser_exe())


def _profile_dir():
    """The profile this module owns: <app state>/browser.

    It is persistent on purpose — the `cf_clearance` a visit earns lives here,
    so a later run of the app starts already cleared instead of paying the
    challenge again. `mlo.paths.app_data_dir()` keeps it beside the rest of the
    app's state.
    """
    try:
        from mlo.paths import app_data_dir
        folder = app_data_dir()
    except Exception:
        folder = ""
    if not folder:
        return ""
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        return ""
    return os.path.join(folder, "browser")


def base_url():
    """The CDP endpoint of the managed browser, or ""."""
    return f"http://127.0.0.1:{MANAGED_PORT}"


def _version(timeout=1.5):
    """The browser's `/json/version` answer, or None when nothing is there."""
    try:
        with urllib.request.urlopen(base_url() + "/json/version", timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def is_running():
    return _version() is not None


def _kill_tree(proc):
    """Stop the browser AND its children — a Chrome root is not the browser."""
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, creationflags=0x08000000)
        else:
            proc.terminate()
    except Exception:
        pass


def stop():
    """Stop the managed browser (its profile stays — it holds the clearance)."""
    with _lock:
        proc = _proc.get("popen")
        _proc["popen"] = None
        _proc["started_at"] = 0.0
    _kill_tree(proc)


def _spawn():
    exe = browser_exe()
    profile = _profile_dir()
    if not exe or not profile:
        return None
    try:
        os.makedirs(profile, exist_ok=True)
    except OSError:
        return None
    flags = [
        f"--remote-debugging-port={MANAGED_PORT}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-sync", "--disable-background-networking",
        # The profile has to persist — it is where the clearance lives — so
        # keep it a profile and not a cache: the disk cache is the part that
        # grows without bound, and this route reads a handful of pages.
        "--disk-cache-size=52428800",
        f"--window-position={WINDOW_POSITION}", f"--window-size={WINDOW_SIZE}",
        "about:blank",
    ]
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000        # CREATE_NO_WINDOW
    try:
        return subprocess.Popen([exe] + flags, **kwargs)
    except OSError:
        return None


def ensure(timeout=40.0):
    """Start the browser once and return its CDP endpoint, or "".

    ADOPTS a browser already answering on the managed port rather than
    spawning a second one: two Chromes would fight over the same profile, and
    the one already running is already cleared.
    """
    if is_running():
        return base_url()
    with _lock:
        if is_running():
            return base_url()
        proc = _spawn()
        if proc is None:
            return ""
        _proc["popen"] = proc
        _proc["started_at"] = time.time()
    end = time.time() + timeout
    while time.time() < end:
        if _version():
            return base_url()
        if proc.poll() is not None:
            return ""
        time.sleep(0.3)
    return ""


def _target():
    """The page target to drive, creating one on about:blank if needed."""
    try:
        with urllib.request.urlopen(base_url() + "/json/list", timeout=3) as r:
            pages = [t for t in json.load(r)
                     if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    except Exception:
        return ""
    if pages:
        return pages[0]["webSocketDebuggerUrl"]
    try:
        with urllib.request.urlopen(base_url() + "/json/new?about:blank",
                                    timeout=3) as r:
            return (json.load(r) or {}).get("webSocketDebuggerUrl") or ""
    except Exception:
        return ""


class _Session:
    """One DevTools connection: JSON commands over a WebSocket, one at a time.

    The framing is Chrome's own (`{"id", "method", "params"}` in, a message
    with the same `id` out), and the connection is a plain `websockets` sync
    client — a dependency the app already has for its own `/ws` route, so this
    costs nothing new.
    """

    def __init__(self, url):
        from websockets.sync.client import connect
        self._ws = connect(url, max_size=32 << 20, open_timeout=20)
        self._id = 0

    def call(self, method, params=None, timeout=NAV_TIMEOUT):
        self._id += 1
        mine = self._id
        self._ws.send(json.dumps({"id": mine, "method": method,
                                  "params": params or {}}))
        while True:
            msg = json.loads(self._ws.recv(timeout=timeout))
            if msg.get("id") == mine:
                return msg

    def value(self, expr, timeout=NAV_TIMEOUT):
        got = self.call("Runtime.evaluate",
                        {"expression": expr, "returnByValue": True}, timeout)
        return ((((got.get("result") or {}).get("result")) or {}).get("value"))

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


def fetch(url, cfg=None, timeout=CLEAR_TIMEOUT, settle=0.4):
    """*url* read by the managed browser: ``{"html", "status"}`` or None.

    Returns None — never raises — when the user has the route off
    (`cf_browser_fetch`), when there is no browser to drive, when the
    navigation fails, or when the page is STILL the Cloudflare interstitial
    after `timeout`: the caller then reports a challenge, exactly as it does
    for every other route.
    """
    if not available(cfg):
        return None
    if not ensure():
        return None
    with _lock:                     # one browser, so one page at a time
        ws_url = _target()
        if not ws_url:
            return None
        session = None
        try:
            session = _Session(ws_url)
            session.call("Page.enable", timeout=20)
            session.call("Runtime.enable", timeout=20)
            session.call("Page.navigate", {"url": str(url)}, timeout=30)
            html = ""
            end = time.time() + timeout
            while time.time() < end:
                time.sleep(settle)
                html = session.value("document.documentElement.outerHTML") or ""
                if (html and not _challenge(html)
                        and session.value("document.readyState") == "complete"):
                    return {"html": html, "status": 200, "via": "browser"}
            # Never cleared. A cleared-but-empty read is still worth returning
            # only when it is not the challenge — the caller decides.
            if html and not _challenge(html):
                return {"html": html, "status": 200, "via": "browser"}
            return None
        except Exception:
            return None
        finally:
            if session is not None:
                session.close()