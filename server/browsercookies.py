"""A cleared session, read straight out of a browser the user already uses.

WHY THIS EXISTS — MEASURED, 2026-10-10, on the owner's machine:

  * RateYourMusic serves its RELEASE pages only to a client carrying a session
    cookie. Anonymous, EVERY release URL answers `503` (`net::
    ERR_HTTP_RESPONSE_CODE_FAILURE` — Chrome renders an error page), while
    `/artist/` and `/search` answer `200`. With the owner's own cookies the
    same URLs answer `200` and the page is 654 KB of real content.
  * The `cf_clearance` alone is not that session: RYM wants the browser's own
    cookie set (`sec_ts`/`sec_bs`/`sec_id` are short-lived and must be FRESH —
    an expired set is a `403 Just a moment...`), and it wants the User-Agent
    the clearance was bound to (Firefox's UA → `200`; the app's built-in
    Chrome UA → `403`).
  * So the two things a scrape needs are the browser's jar AND its UA, and the
    user already has both — in the browser they browse the site with.

This module gets them out. `jar()` asks the yt-dlp the app already ships
(`--cookies-from-browser`, the same mechanism Settings → Sources offers for
YouTube) to dump that browser's cookies; `user_agent()` derives the browser's
own UA from its profile. The caller filters the jar to the host it is for and
stores both — see `server/api_cookies.py`.

NOTHING here starts a browser: the session is the user's own, taken from a
profile that already has it.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

# The browsers yt-dlp understands, in the order the UI offers them: the ones a
# user is most likely to have signed in with first.
BROWSERS = ("firefox", "chrome", "edge", "brave", "chromium", "vivaldi",
            "opera", "safari")

LABELS = {"firefox": "Firefox", "chrome": "Chrome", "edge": "Edge",
          "brave": "Brave", "chromium": "Chromium", "vivaldi": "Vivaldi",
          "opera": "Opera", "safari": "Safari"}

# Where each family keeps its profiles (Windows, the platform this ships to;
# the functions return "" rather than guessing on anything else).
_FIREFOX_ROOTS = (
    r"%APPDATA%\Mozilla\Firefox",
    r"%APPDATA%\librewolf",
    r"%APPDATA%\Waterfox",
)
_CHROMIUM_ROOTS = {
    "chrome": r"%LOCALAPPDATA%\Google\Chrome\User Data",
    "chromium": r"%LOCALAPPDATA%\Chromium\User Data",
    "edge": r"%LOCALAPPDATA%\Microsoft\Edge\User Data",
    "brave": r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data",
    "vivaldi": r"%LOCALAPPDATA%\Vivaldi\User Data",
    "opera": r"%APPDATA%\Opera Software\Opera Stable",
}


def _expand(path):
    return os.path.expandvars(path) if path else ""


def ttl_seconds():
    """How long a jar read from a browser is treated as fresh.

    RYM's own `sec_*` tokens die in about an hour, so a jar is a credential
    with a short life — the Sources panel re-reads it on demand, and a refusal
    is the signal to read it again.
    """
    return 45 * 60.0


# --------------------------------------------------------------------------- #
# What is installed
# --------------------------------------------------------------------------- #
def firefox_profiles():
    """[profile dir] — the Firefox-family profiles that have a cookie store."""
    found = []
    for root in _FIREFOX_ROOTS:
        base = _expand(root)
        if not base or not os.path.isdir(base):
            continue
        ini = os.path.join(base, "profiles.ini")
        names = []
        try:
            with open(ini, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = re.match(r"\s*Path\s*=\s*(.+?)\s*$", line)
                    if m:
                        names.append(m.group(1).replace("\\", os.sep))
        except OSError:
            pass
        for name in names:
            path = name if os.path.isabs(name) else os.path.join(base, name)
            if os.path.isfile(os.path.join(path, "cookies.sqlite")):
                found.append(path)
    return found


def chromium_profiles(browser):
    """[profile dir] — the Chromium-family profiles that have a cookie store."""
    base = _expand(_CHROMIUM_ROOTS.get(str(browser or "").lower(), ""))
    if not base or not os.path.isdir(base):
        return []
    out = []
    try:
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            if os.path.isdir(path) and os.path.isfile(
                    os.path.join(path, "Network", "Cookies")):
                out.append(path)
    except OSError:
        pass
    return out


def installed():
    """({"firefox": n, "chrome": n, ...}) — browsers with a real profile here."""
    out = {}
    n = len(firefox_profiles())
    if n:
        out["firefox"] = n
    for name in BROWSERS:
        if name == "firefox":
            continue
        n = len(chromium_profiles(name))
        if n:
            out[name] = n
    return out


def available():
    """[(id, label, profiles)] for the browsers this machine actually has."""
    have = installed()
    return [(name, LABELS.get(name, name.title()), have[name])
            for name in BROWSERS if name in have]


# --------------------------------------------------------------------------- #
# The jar
# --------------------------------------------------------------------------- #
def _ytdlp():
    try:
        from mlo.tools import detect_all_tools
        entry = (detect_all_tools() or {}).get("yt-dlp") or {}
        exe = entry.get("ytdlp_exe")
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return ""


_module_state = {"module": None, "tried": False}


def _ytdlp_module():
    """The ``yt_dlp`` module — site-packages, else the vendored pip folder.

    Read the way `server/youtube.py` reads it, and for the same measured
    reason: the pip layout the app installs keeps its entry point at
    ``bin/yt-dlp`` with no ``.exe`` beside it, and Windows cannot spawn an
    extension-less script. MEASURED 2026-10-11 on the owner's own install —
    `<music>/.mlo/tools/yt-dlp v2026.8.19/bin/yt-dlp` — where the tool scan
    answered ``ytdlp_exe: None`` and an exe-only lookup therefore returned ""
    for EVERY browser, so the borrowed RYM session (the signed-in half a
    `/release/` page needs) was silently empty and per-track genres had
    nothing to read. The module route extracts the same cookies in-process,
    with no subprocess and no interpreter to guess at.
    """
    if _module_state["tried"]:
        return _module_state["module"]
    _module_state["tried"] = True
    module = None
    try:
        import yt_dlp  # noqa: A004
        module = yt_dlp
    except ImportError:
        try:
            from mlo.tools import python_pkg_path
            folder = python_pkg_path("yt-dlp")
        except Exception:
            folder = None
        if folder:
            sys.path.insert(0, folder)
            try:
                import yt_dlp  # noqa: A004
                module = yt_dlp
            except ImportError:
                module = None
    _module_state["module"] = module
    return module


class _Quiet:
    """The logger yt-dlp insists on — its chatter is not the app's log."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def _jar_body(text):
    """*text* when it carries a cookie line, else "".

    A jar with only yt-dlp's header comment is "nothing was read", not an
    empty credential to store over a working one.
    """
    body = [ln for ln in str(text or "").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]
    return text if body else ""


def _jar_from_module(browser):
    """That browser's cookies, extracted IN PROCESS, as Netscape jar text.

    yt-dlp's own reader (`yt_dlp.cookies`), so the file is byte-for-byte the
    shape its `--cookies` flag writes — and the shape every reader here
    (`server/cookies.py`, `browserfetch._browser_session`) parses. Empty when
    the module is absent, the browser's store is unreadable (a locked or
    DPAPI-encrypted profile), or it holds no cookies at all.
    """
    module = _ytdlp_module()
    if module is None:
        return ""
    yt_cookies = getattr(module, "cookies", None)
    if yt_cookies is None:
        return ""
    try:
        jar = yt_cookies.extract_cookies_from_browser(str(browser),
                                                      logger=_Quiet())
    except Exception:
        return ""
    if not len(jar):
        return ""
    folder = tempfile.mkdtemp(prefix="mlo-cookies-")
    path = os.path.join(folder, "cookies.txt")
    try:
        try:
            # `ignore_discard` keeps the SESSION cookies (RYM's own `ulv` and
            # `is_logged_in` are two) — without it the jar would lose exactly
            # the half a signed-in release page needs.
            jar.save(path, ignore_discard=True, ignore_expires=True)
        except Exception:
            import http.cookiejar
            plain = http.cookiejar.MozillaCookieJar(path)
            for cookie in jar:
                plain.set_cookie(cookie)
            plain.save(ignore_discard=True, ignore_expires=True)
        with open(path, encoding="utf-8", errors="replace") as fh:
            return _jar_body(fh.read())
    except OSError:
        return ""
    finally:
        try:
            import shutil
            shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass


def jar(browser, timeout=120):
    """That browser's cookies as a Netscape jar, or "".

    Two routes, in the order that works: the `yt_dlp` MODULE in process (see
    `_ytdlp_module` — the app's own install is a pip folder with no spawnable
    binary), then the `yt-dlp` EXECUTABLE (a distro/PATH build, which is what
    the module route has to make room for on Linux).

    The exe route runs yt-dlp against a URL that is not a video — which is
    EVERY url this is used for — so its return code is ignored and the FILE it
    writes is what is read. A missing yt-dlp, an unknown browser, or a
    locked/corrupt profile all answer "" rather than raising: the caller
    reports "could not read that browser", never a crash.
    """
    name = str(browser or "").strip().lower()
    if name not in BROWSERS:
        return ""
    text = _jar_from_module(name)
    if text:
        return text
    exe = _ytdlp()
    if not exe:
        return ""
    # The jar path must NOT exist when yt-dlp starts: `--cookies <path>` is
    # READ first (an empty file is "not a Netscape cookies file" and it aborts
    # before extracting anything) and written as it exits. A private folder
    # gives a name that is genuinely absent without racing anyone for it.
    folder = tempfile.mkdtemp(prefix="mlo-cookies-")
    path = os.path.join(folder, "cookies.txt")
    try:
        cmd = [exe, "--cookies-from-browser", name, "--cookies", path,
               "--skip-download", "--no-warnings", "--no-playlist",
               "https://example.invalid/"]
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000        # CREATE_NO_WINDOW
        try:
            subprocess.run(cmd, capture_output=True, timeout=timeout, **kwargs)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return _jar_body(fh.read())
        except OSError:
            return ""
    finally:
        try:
            import shutil
            shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# The User-Agent the clearance is bound to
# --------------------------------------------------------------------------- #
def user_agent(browser):
    """That browser's own User-Agent, or "".

    Cloudflare binds `cf_clearance` to the UA that earned it — MEASURED: the
    same jar with Firefox's UA answers `200` and with the app's built-in Chrome
    UA answers `403 Just a moment...` — so the UA is half of the credential and
    is derived from the profile, not guessed.
    """
    name = str(browser or "").strip().lower()
    if name == "firefox":
        for path in firefox_profiles():
            try:
                with open(os.path.join(path, "compatibility.ini"),
                          encoding="utf-8", errors="replace") as fh:
                    m = re.search(r"LastVersion=([0-9][0-9.]*)", fh.read())
            except OSError:
                continue
            if m:
                v = m.group(1)
                return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{v}) "
                        f"Gecko/20100101 Firefox/{v}")
        return ""
    profiles = chromium_profiles(name)
    for path in profiles:
        for probe in (os.path.join(path, "Last Version"),
                      os.path.join(os.path.dirname(path), "Last Version")):
            try:
                with open(probe, encoding="utf-8", errors="replace") as fh:
                    v = fh.read().strip()
            except OSError:
                continue
            if re.match(r"^[0-9][0-9.]*$", v):
                return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        f"AppleWebKit/537.36 (KHTML, like Gecko) "
                        f"Chrome/{v} Safari/537.36")
    return ""


def summary():
    """What the sources panel needs: the browsers, their profiles, their UA."""
    out = []
    for name, label, count in available():
        out.append({"id": name, "label": label, "profiles": count,
                    "user_agent": user_agent(name)})
    return out