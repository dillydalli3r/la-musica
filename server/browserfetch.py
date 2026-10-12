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
  * HEADLESS is detected by BOTH hosts, in every spelling tried — re-MEASURED,
    2026-10-11: `--headless=new` ends on the interstitial even with the real
    Chrome User-Agent, a matching `userAgentData` override, the anti-automation
    JS patches (webdriver, `window.chrome`, plugins, WebGL) and a profile that
    already holds a valid `cf_clearance`. Headless is a fingerprint the WAF can
    read, so there is nothing to gain from a headless process.
  * But a VISIBLE window is not what the challenge wants either. The same
    Chrome, launched headed onto a WINDOWS DESKTOP THE USER NEVER SEES
    (`CreateDesktopW` + `CreateProcessW` with `lpDesktop`), clears both hosts
    and returns real pages — nothing appears on any display, in the taskbar or
    on the alt-tab list, because the desktop it draws on is not one the logon
    session shows. Off-screen POSITIONING is what does not work: a multi-monitor
    desktop can put `-3200,-3200` back on a real display, which is the window
    the owner saw. A hidden desktop is invisible on every layout.

So this is the third route for a scraped host: after the plain HTTP GET with
whatever clearance is in hand, and INSTEAD of the solver (which cannot pass),
the page is fetched by driving a browser the user already trusts, over its
DevTools protocol. On Windows it runs on a hidden desktop and no window is
ever shown; elsewhere it falls back to `--headless=new`, which the two hosts
refuse but costs nothing on a host that does not challenge. The profile is the
module's own, under the app's state dir: its clearance survives later runs, and
every page hands cookies back to `cfchallenge`: AOTY's plain path is fast when
an explicit `aoty_cookie` is stored; without it, AOTY uses this browser route
because a plain `200` can omit its search results and genres.

When no `rym_cookie`/`aoty_cookie` paste is stored, the browser fetch lazily
reads that host's session from this machine's browser profiles via yt-dlp,
filters the jar to the requested host, and keeps only that cookie string in
memory for 45 minutes. A stored paste always wins; a borrowed session is never
written to config or sent to another host. It is injected as session cookies
for that page — Cloudflare's own pairs from the paste are dropped, the site's
signed-in tokens travel — and deleted in `finally`; Chrome's own clearance
remains in its managed profile. That is the signed-in half RYM requires for
`/release/` pages.

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

# The desktop the browser is drawn on, on Windows: one this process creates for
# itself (`CreateDesktopW`) and never shows, so the window exists for the WAF
# and for nobody else — it is not on any display, in the taskbar, or on the
# alt-tab list. The name carries the pid: two runs of the app (the install and a
# dev bed, say) must not fight over one desktop. The handle is kept for as long
# as the browser lives, because destroying the desktop takes the browser with
# it. See the module docstring: off-screen POSITIONING is what fails (a
# multi-monitor layout can put the window back on a real display), a hidden
# desktop does not.
HIDDEN_DESKTOP_PREFIX = "mlo-browser-fetch"

# How long to let a challenge clear before giving up on that page. MEASURED,
# 2026-10-11, from a COLD profile on this machine: Album of the Year clears in
# ~27 s and RateYourMusic in ~49 s — the first version of this route used 45 s
# and lost the RYM page by four seconds, which read as "no data" for a host
# that answers perfectly a moment later. The ceiling is for a challenge that
# will not clear at all; a profile that already holds a clearance pays none of
# it. `cfchallenge.warm_browser` takes this cost at boot instead (see there).
CLEAR_TIMEOUT = 75.0
# How long a navigation has to reach "complete" before the page is read anyway.
NAV_TIMEOUT = 60.0

# The same marker `cfchallenge` matches, kept here so this module does not
# import it (it is imported BY it — a cycle here would be a real one).
_CHALLENGE_RE = None

# One lock for the managed browser AND its desktop, and it is REENTRANT: the
# desktop is created from inside `ensure`'s own critical section (see
# `_spawn` → `_hidden_desktop`), and a plain `Lock` deadlocked the first
# version of that route — `ensure()` never returned, so every page the browser
# was supposed to fetch hung instead. Any nested acquire must be safe here.
_lock = threading.RLock()
_proc = {"popen": None, "started_at": 0.0}
# The desktop the browser draws on, on Windows. Created once per process and
# never shown; the handle is held for the browser's whole life (see
# `HIDDEN_DESKTOP_PREFIX`).
_desktop = {"handle": None, "name": ""}


def _challenge(text: str) -> bool:
    global _CHALLENGE_RE
    if _CHALLENGE_RE is None:
        import re
        _CHALLENGE_RE = re.compile(
            r"just a moment|cf-?challenge|_cf_chl|checking your browser|"
            r"enable javascript and cookies|attention required", re.I)
    return bool(text and _CHALLENGE_RE.search(text[:4000]))


# Chrome's OWN network-error document, which is not a page from any site.
# MEASURED 2026-10-10: RYM answers an anonymous client `503` on every
# `/release/` URL, and that document — `<body class="neterror">`,
# `id="main-frame-error"`, the Chromium Authors stylesheet — was read as "the
# answer": 185 KB with no markup from the site at all, so it parsed to no
# genres and no track list. One got into a library's 30-day `rym_cache` and
# every later lookup was served it from disk, which reads exactly like a
# scraper that no longer works.
_ERROR_PAGE_RE = None


def is_error_page(html) -> bool:
    """Whether *html* is the browser's error document rather than a site page.

    Callers cache whole pages for 30 days, so this is what keeps a navigation
    failure out of that cache — and what lets a page already cached from
    before this check be recognised, dropped, and fetched again.
    """
    global _ERROR_PAGE_RE
    if _ERROR_PAGE_RE is None:
        import re
        _ERROR_PAGE_RE = re.compile(
            r'<body[^>]+class="[^"]*neterror|id="main-frame-error"|'
            r"--error-code-color", re.I)
    return bool(html) and bool(_ERROR_PAGE_RE.search(str(html)[:8000]))


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
    _close_desktop()


def _close_desktop():
    """Destroy the hidden desktop once nothing is drawn on it (Windows)."""
    with _lock:
        handle = _desktop.get("handle")
        _desktop["handle"], _desktop["name"] = None, ""
    if handle and os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.CloseDesktop(ctypes.c_void_p(handle))
        except Exception:
            pass


def _hidden_desktop():
    """``(name, handle)`` of a desktop only this process can see, or ``("", 0)``.

    MEASURED, 2026-10-11: a headed Chrome on a desktop created this way clears
    both scraped hosts' challenge and returns real pages, while no window is
    shown on any display — see the module docstring for why headless cannot be
    made to do this. Windows only; every other platform has no desktop to hide
    on and gets `--headless=new` instead.
    """
    if os.name != "nt":
        return "", 0
    with _lock:
        if _desktop["handle"]:
            return _desktop["name"], _desktop["handle"]
        import ctypes
        # DESKTOP_ALL_ACCESS: the browser creates its windows on this desktop.
        # The restype has to be a pointer — the default `int` truncates a
        # 64-bit HANDLE and CreateDesktop fails with an access error.
        create = ctypes.windll.user32.CreateDesktopW
        create.restype = ctypes.c_void_p
        name = f"{HIDDEN_DESKTOP_PREFIX}-{os.getpid()}"
        handle = create(name, None, None, 0, 0x01FF, None, None)
        if not handle:
            return "", 0
        _desktop["name"], _desktop["handle"] = name, handle
        return name, handle


class _HiddenProc:
    """A `subprocess.Popen`-shaped handle on a process started with
    `CreateProcessW`.

    `Popen` is what this module wants — `_kill_tree` and `ensure` read its
    `pid`, `poll()` and return value — but it cannot name a desktop:
    `STARTUPINFO.lpDesktop` is not exposed by Python's `subprocess`. So the one
    Windows-only spawn goes through ctypes and comes back in this shape.
    """

    def __init__(self, pid, handle):
        self.pid = pid
        self._handle = handle

    def poll(self):
        """None while the process runs, its exit code once it has exited."""
        import ctypes
        code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(
                ctypes.c_void_p(self._handle), ctypes.byref(code)):
            return 0
        return None if code.value == 259 else int(code.value)   # STILL_ACTIVE


def _spawn_on_hidden_desktop(exe, flags):
    """Chrome on the invisible desktop, or None (Windows, ctypes)."""
    import ctypes
    import ctypes.wintypes as wt
    name, _handle = _hidden_desktop()
    if not name:
        return None

    class STARTUPINFO(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
            ("lpTitle", wt.LPWSTR), ("dwX", wt.DWORD), ("dwY", wt.DWORD),
            ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
            ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD),
            ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD),
            ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
            ("hStdError", wt.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
                    ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]

    si = STARTUPINFO()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = name
    si.dwFlags = 0x00000001            # STARTF_USESHOWWINDOW
    si.wShowWindow = 0                 # SW_HIDE — belt and braces; the desktop
                                       # it names is one nothing shows anyway
    pi = PROCESS_INFORMATION()
    kernel32 = ctypes.windll.kernel32
    ok = kernel32.CreateProcessW(
        exe, ctypes.create_unicode_buffer(subprocess.list2cmdline([exe] + flags)),
        None, None, False, 0x08000000,   # CREATE_NO_WINDOW
        None, None, ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        return None
    try:
        kernel32.CloseHandle(pi.hThread)
    except Exception:
        pass
    return _HiddenProc(int(pi.dwProcessId), pi.hProcess)


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
        "about:blank",
    ]
    # NO `--user-agent` and no UA override of any kind: MEASURED, 2026-10-11 —
    # a UA that disagrees with Chrome's own `userAgentData` build is exactly
    # what stops the challenge clearing. Neither host answered until that flag
    # was gone. Chrome's own UA is also what the `cf_clearance` it earns is
    # bound to, so overriding it defeats the clearance rather than hiding us.
    if os.name == "nt":
        return _spawn_on_hidden_desktop(exe, flags)
    kwargs = {}
    try:
        return subprocess.Popen([exe, "--headless=new"] + flags, **kwargs)
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


# The cookies that belong to the browser+UA that earned them, and to nothing
# else: Cloudflare's clearance pair and its per-client ids. A paste read out of
# the user's own Firefox carries these for THAT browser, and handing them to
# this Chrome is worse than sending none — the WAF sees a clearance that can
# never validate and refuses the request instead of letting the browser solve
# it. Everything a session needs (`is_logged_in`, `username`, `ulv`,
# `rememberMe`, and RYM's own `sec_*` tokens, which travel WITH that signed-in
# jar) is not Cloudflare's to bind and does travel.
#
# MEASURED, 2026-10-11: dropping RYM's `sec_*` alongside the clearance made the
# release page fail on a fresh profile — the signed-in jar is one credential,
# its `sec_*` rotation included — so only Cloudflare's own pairs are filtered.
_CF_OWNED_COOKIES = frozenset({"cf_clearance", "__cf_bm", "__cfruid", "__cfuvid"})


def _is_foreign_browser_cookie(name):
    """Whether *name* is Cloudflare's own, and so must not come from a paste."""
    return name.lower() in _CF_OWNED_COOKIES


def _session_cookie(cfg, url):
    """The session to hand the browser for *url*'s host, or "".

    The stored paste wins (`rym_cookie` / `aoty_cookie` — the same two the plain
    scrapers send). When nothing is stored, the session the user's OWN browser is
    signed in with is used instead (`_browser_session`), because that is the
    difference between an album's genres being readable and RYM answering `503`
    to every anonymous client — see `_set_session_cookies`.
    """
    try:
        from . import cfchallenge
        host = cfchallenge.host_of(url)
    except Exception:
        return ""
    key = {"rateyourmusic.com": "rym_cookie",
           "albumoftheyear.org": "aoty_cookie"}.get(host, "")
    if not key:
        return ""
    stored = str((cfg or {}).get(key) or "").strip()
    if stored:
        return stored
    return _browser_session(host)


# The session read out of the user's own browser, per host:
# {"cookie": str, "at": ts}. Cached for the same 45 minutes
# `server/browsercookies.ttl_seconds` gives a browser-read jar, and an EMPTY
# answer is cached too — it is an answer ("that browser is not signed in"), and
# re-shelling out to yt-dlp for every page would be the cost this cache exists
# to avoid.
_BROWSER_SESSION: dict = {}
BROWSER_SESSION_TTL = 45 * 60.0


def _browser_session(host):
    """The `host` session out of a browser this machine is signed in to, or "".

    WHY: RateYourMusic serves `/release/` — an album's genres and its rating —
    ONLY to a signed-in client; an anonymous browser is refused `503` while
    `/artist/` and `/search` answer (MEASURED, see the module docstring and
    `AGENTS.md`). An install that never pasted `rym_cookie` therefore could not
    read a single album's RYM data even though the owner's own browser is signed
    in. The panel's "read from a browser" import is this same read made by
    hand (`POST /api/cookies/{source}/from-browser`); this is done once, lazily,
    only when nothing is stored. The Python cache keeps the filtered cookie
    string in memory for 45 minutes; `fetch` injects it as host-scoped session
    cookies for the requested page, then deletes those names in `finally`.
    Nothing is written to config or sent to another host; Chrome's clearance
    and site-set cookies remain in its app-owned profile as the fast path.

    Never raises: no yt-dlp, no browser, no profile, or a signed-out profile all
    answer "" and the caller reports RYM's refusal as it did before.
    """
    now = time.time()
    entry = _BROWSER_SESSION.get(host)
    if entry and now - entry.get("at", 0) < BROWSER_SESSION_TTL:
        return str(entry.get("cookie") or "")
    cookie = ""
    try:
        from server import browsercookies
        from server import cookies as cookies_mod
        have = browsercookies.installed() or {}
        for browser in ("firefox", "chrome", "edge", "brave", "chromium"):
            if browser not in have:
                continue
            jar = browsercookies.jar(browser)
            if not jar:
                continue
            _filtered, kept, _dropped = cookies_mod.filter_jar(jar, [host])
            pairs = [f"{row.name}={row.value}" for row in kept if row.name]
            if pairs:
                cookie = "; ".join(pairs)
                break
    except Exception:
        cookie = ""
    _BROWSER_SESSION[host] = {"cookie": cookie, "at": now}
    return cookie


def _set_session_cookies(session, url, cookie_header):
    """Set the user's session on the browser, returning names to delete later.

    The managed browser's own profile is an ANONYMOUS visit: it clears the
    challenge, and on RateYourMusic an anonymous visit is served `/artist/` and
    `/search` but has `/release/` answered `503` (MEASURED 2026-10-10 and again
    2026-10-11 — a `chrome-error` page, not a challenge). A stored paste OR the
    filtered session read from the user's own browser (`_browser_session`) is a
    signed-in session; a signed-in release page is `200`. Cloudflare's own
    pairs are dropped on the way in (`_CF_OWNED_COOKIES`); the site's signed-in
    tokens, `sec_*` included, are set as session cookies and the returned names
    are what `fetch` removes from the managed profile in `finally`. They are
    never written to config or left in the profile after a fetch. The clearance
    Chrome earns is separate and remains in its profile.
    """
    text = str(cookie_header or "").strip()
    if not text:
        return []
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return []
    if not host:
        return []
    domain = "." + host[4:] if host.startswith("www.") else host
    set_names = []
    for pair in text.split(";"):
        name, _sep, value = pair.strip().partition("=")
        name = name.strip()
        if not name or _is_foreign_browser_cookie(name):
            continue
        try:
            reply = session.call("Network.setCookie", {
                "name": name, "value": value, "domain": domain,
                "path": "/", "secure": True,
            }, timeout=10)
            if not reply.get("error") and (reply.get("result") or {}).get("success") is not False:
                set_names.append(name)
        except Exception:
            continue
    return list(dict.fromkeys(set_names))


def _publish_clearance(session, url):
    """Hand the browser's cookies for *url* to `cfchallenge`.

    MEASURED, 2026-10-11: the cookies a real browser holds — `cf_clearance`
    plus whatever else the visit set — are exactly what `cfchallenge.
    fetch_page`'s fast path reads (`solved_cookies`), so publishing them here
    makes a cleared fast path available where the source can trust plain HTTP.
    RYM's own module still prefers the browser (the WAF refuses its plain client
    whatever it carries); AOTY's plain path uses it only with an explicit
    `aoty_cookie`, because an anonymous 200 may omit the search results/genres.
    Never raises — a fetch that worked must not fail over a cache update.
    """
    try:
        from . import cfchallenge
        host = cfchallenge.host_of(url)
        if not host:
            return
        user_agent = str(session.value("navigator.userAgent") or "")
        session.call("Network.enable", timeout=10)
        got = session.call("Network.getCookies", {"urls": [str(url)]}, timeout=10)
        cookies = {}
        for cookie in ((got.get("result") or {}).get("cookies") or []):
            name = str((cookie or {}).get("name") or "").strip()
            if name:
                cookies[name] = str((cookie or {}).get("value") or "")
        if cookies:
            cfchallenge.remember_clearance(host, cookies, user_agent)
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
    # This navigation is a real GET too. Use the shared per-host gate even on
    # the browser-only path and on a challenge fallback, so rapid AOTY/RYM page
    # sequences keep the same 1 req/s politeness as the plain path.
    from . import cfchallenge
    cfchallenge._pace(cfchallenge.host_of(url))
    with _lock:                     # one browser, so one page at a time
        ws_url = _target()
        if not ws_url:
            return None
        session = None
        session_cookies = []
        try:
            session = _Session(ws_url)
            session.call("Page.enable", timeout=20)
            session.call("Runtime.enable", timeout=20)
            session.call("Network.enable", timeout=20)
            # Stored or borrowed session for this host; Cloudflare-owned
            # cookies are dropped and these session cookies are removed after
            # the page is read (`finally`).
            session_cookies = _set_session_cookies(session, url,
                                                   _session_cookie(cfg, url))
            nav = session.call("Page.navigate", {"url": str(url)}, timeout=30)
            # A navigation that never reached a document is NOT a page. Chrome
            # answers `net::ERR_*` and then renders its own error document,
            # which carries no challenge marker — so without this check a 503
            # or a dead host came back as "a page with no genres", the caller
            # reported "no data", and the source looked like it had answered.
            # MEASURED 2026-10-10: RateYourMusic serves `503` to an anonymous
            # client on every `/release/` URL, and that error page was being
            # returned as the album page.
            if str(((nav.get("result") or {}).get("errorText")) or ""):
                return None
            html = ""
            end = time.time() + timeout
            while time.time() < end:
                time.sleep(settle)
                if str(session.value("location.href") or "").startswith("chrome-error"):
                    return None
                html = session.value("document.documentElement.outerHTML") or ""
                if (html and not _challenge(html) and not is_error_page(html)
                        and session.value("document.readyState") == "complete"):
                    _publish_clearance(session, url)
                    return {"html": html, "status": 200, "via": "browser"}
            # Never cleared. A cleared-but-empty read is still worth returning
            # only when it is not the challenge — the caller decides.
            if html and not _challenge(html) and not is_error_page(html):
                _publish_clearance(session, url)
                return {"html": html, "status": 200, "via": "browser"}
            return None
        except Exception:
            return None
        finally:
            if session is not None:
                for name in session_cookies:
                    try:
                        session.call("Network.deleteCookies",
                                     {"name": name, "url": str(url)}, timeout=10)
                    except Exception:
                        pass
                session.close()