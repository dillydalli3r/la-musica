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


def jar(browser, timeout=120):
    """That browser's cookies as a Netscape jar, or "".

    yt-dlp writes the jar as it exits, and it exits non-zero on a URL that is
    not a video — which is EVERY url this is used for — so the return code is
    ignored and the FILE is what is read. A missing yt-dlp, an unknown browser,
    or a locked/corrupt profile all answer "" rather than raising: the caller
    reports "could not read that browser", never a crash.
    """
    exe = _ytdlp()
    name = str(browser or "").strip().lower()
    if not exe or name not in BROWSERS:
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
                text = fh.read()
        except OSError:
            return ""
        # A jar with only yt-dlp's header comment is "nothing was read", not an
        # empty credential to store over a working one.
        body = [ln for ln in text.splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]
        return text if body else ""
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