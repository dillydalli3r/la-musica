"""Automatic dependency fetcher.

Downloads the latest official Windows builds of the external encoder
toolchain from GitHub releases and installs them into .dependencies/
using exactly the layout the auto-detection in tools.py expects:

    .dependencies/
        flac v1.5.0/           flac.exe, metaflac.exe
        libjxl v0.12.0/        cjxl.exe, djxl.exe
        libjpeg-turbo v3.2.0/  jpegtran.exe
        oxipng v10.2.0/        oxipng.exe

Asset sources:
    flac            xiph/flac          flac-<v>-win.zip
    libjxl          libjxl/libjxl      jxl-x64-windows-static.zip
    libjpeg-turbo   libjpeg-turbo/...  libjpeg-turbo-<v>-vc-x64.exe (NSIS)
    oxipng          oxipng/oxipng      oxipng-<v>-x86_64-pc-windows-msvc.zip
    AudioAuditor    Angel2mp3/...      AudioAuditorCLI-win-x64.exe (bare exe)

The libjpeg-turbo release only ships NSIS installers for Windows; those are
unpacked with 7-Zip when available, otherwise installed silently into a
temporary folder (which needs a space-free path, hence GetShortPathName) and
the required binaries are copied out.

Only the vendored pip packages (librosa, beets) and simple-dr-meter are
platform-independent: every other tool above is a Windows binary, so on
Linux/macOS install_dependency() refuses with the distro package that already
provides it (LINUX_PACKAGES) rather than downloading something that cannot run.
Each archive's LICENSE/COPYING/README is copied next to the installed binaries
(_copy_licence_files).

Standard-library only - no requests.
"""

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
import urllib.request

from .paths import DEPS_DIR
from .subproc import run_tool
from .tools import detect_all_tools, python_pkg_path

DISPLAY_NAMES = {
    "flac": "FLAC",
    "libjxl": "libjxl",
    "libjpeg_turbo": "libjpeg-turbo",
    "oxipng": "oxipng",
    "audioauditor": "AudioAuditor",
    "rsgain": "rsgain",
    "ffmpeg": "ffmpeg",
    "simpledrmeter": "simple-dr-meter",
    "logchecker": "Logchecker",
    "php": "PHP",
    "cuetools": "CUETools",
    "librosa": "librosa",
    "beets": "beets",
    "slskd": "slskd",
    "chromaprint": "Chromaprint (fpcalc)",
    "yt-dlp": "yt-dlp",
}

REPOS = {
    "slskd": "slskd/slskd",
    "flac": "xiph/flac",
    "libjxl": "libjxl/libjxl",
    "libjpeg_turbo": "libjpeg-turbo/libjpeg-turbo",
    "oxipng": "oxipng/oxipng",
    "audioauditor": "Angel2mp3/AudioAuditor",
    "rsgain": "complexlogic/rsgain",
    "ffmpeg": "BtbN/FFmpeg-Builds",
    "logchecker": "OPSnet/Logchecker",
    "cuetools": "gchudov/cuetools.net",
    "chromaprint": "acoustid/chromaprint",
    "yt-dlp": "yt-dlp/yt-dlp",
}

# Ordered asset-name preferences (regex, matched case-insensitively).
ASSET_PATTERNS = {
    "slskd": [r"^slskd-[\d.]+-win-x64\.zip$"],
    "flac": [r"^flac-[\d.]+-win\.zip$"],
    "libjxl": [r"^jxl-x64-windows-static\.zip$", r"^jxl-x64-windows\.zip$"],
    "libjpeg_turbo": [
        r"^libjpeg-turbo-[\d.]+-vc-x64\.exe$",
        r"^libjpeg-turbo-[\d.]+-gcc-x64\.exe$",
    ],
    "oxipng": [r"^oxipng-[\d.]+-x86_64-pc-windows-msvc\.zip$"],
    "audioauditor": [r"^AudioAuditorCLI-win-x64\.exe$"],
    "rsgain": [r"^rsgain-[\d.]+-win64\.zip$"],
    "ffmpeg": [r"^ffmpeg-master-latest-win64-gpl\.zip$"],
    "logchecker": [r"^logchecker\.phar$"],
    "cuetools": [r"^CUETools\.zip$", r"^cuetools.*\.zip$"],
    "chromaprint": [r"^chromaprint-fpcalc-[\d.]+-windows-x86_64\.zip$"],
    # The release also ships extensionless POSIX builds and a tarball; the
    # Windows binary is the bare .exe (SINGLE_EXE_TOOLS).
    "yt-dlp": [r"^yt-dlp\.exe$"],
}

INSTALL_PREFIX = {
    "flac": "flac",
    "libjxl": "libjxl",
    "libjpeg_turbo": "libjpeg-turbo",
    "oxipng": "oxipng",
    "audioauditor": "AudioAuditor",
    "rsgain": "rsgain",
    "ffmpeg": "ffmpeg",
    "logchecker": "Logchecker",
    "php": "php",
    "cuetools": "CUETools",
    "librosa": "librosa",
    "beets": "beets",
    "slskd": "slskd",
    "chromaprint": "chromaprint",
    "yt-dlp": "yt-dlp",
}

# Tools whose upstream releases only ship Windows builds, mapped to the Linux
# package providing the same tool (None = no packaged equivalent). Every asset
# below is a .exe/win-zip, and MARKER_EXES can only check that files with the
# right NAMES landed - so on Linux a download would "succeed" with a folder of
# unrunnable .exe files. install_dependency()/pick_asset() refuse via
# _require_windows() instead, naming the distro package to use (the Docker
# image installs them; see Dockerfile).
LINUX_PACKAGES = {
    "flac": "flac",
    "libjxl": "libjxl-tools",
    "libjpeg_turbo": "libjpeg-progs",
    "oxipng": "oxipng",
    "ffmpeg": "ffmpeg",
    "rsgain": "rsgain",
    "chromaprint": "libchromaprint-tools",
    "slskd": None,
    "audioauditor": None,
    "logchecker": None,
    "php": None,
    "cuetools": None,
}

# Vendored pure-Python tools: installed with `pip install --target` into a
# versioned .dependencies folder instead of shipping binaries. They are
# imported by prepending the folder to sys.path (see tools.python_pkg_path).
PIP_PACKAGES = {
    "librosa": "librosa==0.11.0",
    "beets": "beets==2.4.0",
    "yt-dlp": "yt-dlp==2026.8.19",
}

# Tools of which only the Windows build is vendored as a binary: on Linux the
# same program is installed as the pip package above (yt-dlp has no Linux
# release asset at all, and its pip package is the upstream-supported install).
# They are deliberately NOT in LINUX_PACKAGES - _require_windows() would refuse
# the download instead of using pip.
PIP_ON_LINUX = {"yt-dlp"}

TOOL_DIRS = INSTALL_PREFIX  # backward compat for app.py (use installed_path() for versioned folder)

def installed_path(key):
    """Return the actual versioned folder for an installed tool, or None."""
    prefix = INSTALL_PREFIX.get(key, key)
    if not os.path.isdir(DEPS_DIR):
        return None
    # Find folder starting with prefix (e.g. "php v8.1.28")
    try:
        for entry in os.listdir(DEPS_DIR):
            full = os.path.join(DEPS_DIR, entry)
            if os.path.isdir(full) and entry.lower().startswith(prefix.lower()):
                # Prefer exact prefix match with version
                return full
    except OSError:
        pass
    return None

# Exe files that must be present after installation.
MARKER_EXES = {
    "flac": ("flac.exe", "metaflac.exe"),
    "libjxl": ("cjxl.exe", "djxl.exe"),
    "libjpeg_turbo": ("jpegtran.exe",),
    "oxipng": ("oxipng.exe",),
    "audioauditor": ("AudioAuditorCLI.exe",),
    "rsgain": ("rsgain.exe",),
    "ffmpeg": ("ffmpeg.exe", "ffprobe.exe"),
    "logchecker": ("logchecker.phar",),
    "php": ("php.exe",),
    "cuetools": ("CUETools.exe",),
    "slskd": ("slskd.exe",),
    "chromaprint": ("fpcalc.exe",),
    "yt-dlp": ("yt-dlp.exe",),
}

# Tools whose release asset is a single bare exe - no archive to extract.
SINGLE_EXE_TOOLS = {"audioauditor", "logchecker", "yt-dlp"}

# Exact, pinned dependency versions. Every tool is downloaded from a specific
# GitHub release tag (never "latest") so installs and CI builds are fully
# reproducible. `tag` is the GitHub release tag, `asset` the exact file to
# fetch, `version` the version label used in the .dependencies folder.
PINNED = {
    "flac": {
        "tag": "1.5.0",
        "asset": "flac-1.5.0-win.zip",
        "version": "1.5.0",
    },
    "libjxl": {
        "tag": "v0.12.0",
        "asset": "jxl-x64-windows-static.zip",
        "version": "0.12.0",
    },
    "libjpeg_turbo": {
        "tag": "3.2.0",
        "asset": "libjpeg-turbo-3.2.0-vc-x64.exe",
        "version": "3.2.0",
    },
    "oxipng": {
        "tag": "v10.2.0",
        "asset": "oxipng-10.2.0-x86_64-pc-windows-msvc.zip",
        "version": "10.2.0",
    },
    "audioauditor": {
        "tag": "V2.0.0",
        "asset": "AudioAuditorCLI-win-x64.exe",
        "version": "2.0.0",
    },
    "rsgain": {
        "tag": "v3.7",
        "asset": "rsgain-3.7-win64.zip",
        "version": "3.7",
    },
    "ffmpeg": {
        "tag": "autobuild-2026-08-19-19-21",
        "asset": "ffmpeg-N-126217-ge1e325235e-win64-gpl.zip",
        "version": "2026.8.19",
    },
    "simpledrmeter": {
        "tag": "v0.0.0",
        "asset": "",
        "version": "0.0.0",
    },
    "logchecker": {
        "tag": "0.14.4",
        "asset": "logchecker.phar",
        "version": "0.14.4",
    },
    "php": {
        "tag": "8.1.28",
        "asset": "php-8.1.28-nts-Win32-vs16-x64.zip",
        "version": "8.1.28",
    },
    "cuetools": {
        "tag": "v2.2.6",
        "asset": "CUETools_2.2.6.zip",
        "version": "2.2.6",
    },
    "librosa": {
        "tag": "0.11.0",
        "asset": "",
        "version": "0.11.0",
    },
    "beets": {
        "tag": "v2.4.0",
        "asset": "",
        "version": "2.4.0",
    },
    "slskd": {
        "tag": "0.26.0",
        "asset": "slskd-0.26.0-win-x64.zip",
        "version": "0.26.0",
    },
    "chromaprint": {
        "tag": "v1.6.1",
        "asset": "chromaprint-fpcalc-1.6.1-windows-x86_64.zip",
        "version": "1.6.1",
    },
    "yt-dlp": {
        "tag": "2026.08.19",
        "asset": "yt-dlp.exe",
        "version": "2026.8.19",
    },
}

# simple-dr-meter is a Python script (no Windows binary / no releases); it is
# fetched from the repo's v0.0.0 tag archive instead of a GitHub release.
SIMPLE_DR_METER_ZIP_URL = (
    "https://github.com/magicgoose/simple-dr-meter/archive/refs/tags/v0.0.0.zip"
)

# PHP for Windows (needed for Logchecker phar) — not on GitHub, direct from windows.php.net
PHP_ZIP_URL = (
    "https://windows.php.net/downloads/releases/archives/php-8.1.28-nts-Win32-vs16-x64.zip"
)

_HEADERS = {
    "User-Agent": "la-musica/2.1",
    "Accept": "application/vnd.github+json",
}

_release_cache = {}


# ----------------------------------------------------------------------
# GitHub API
# ----------------------------------------------------------------------
def _api_json(url):
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_latest_release(key):
    """Return the PINNED release dict for a tool (cached per session).

    Tools are pinned to exact versions (see PINNED) rather than "latest", so
    installs are reproducible. Fetches the specific release tag from GitHub.
    Rolling-release repos (ffmpeg autobuilds) delete old tags, so a 404 on
    the pinned tag falls back to the repo's current latest release.
    """
    if key not in _release_cache:
        pin = PINNED[key]
        try:
            data = _api_json(
                f"https://api.github.com/repos/{REPOS[key]}/releases/tags/{pin['tag']}"
            )
            version = pin["version"]
        except urllib.error.HTTPError as e:
            if e.code != 404 or not ASSET_PATTERNS.get(key):
                raise
            data = _api_json(
                f"https://api.github.com/repos/{REPOS[key]}/releases/latest"
            )
            version = str(data.get("tag_name") or pin["version"])
        urls = {a.get("name", ""): a.get("browser_download_url", "")
                for a in data.get("assets", [])}
        _release_cache[key] = {
            "version": version,
            "assets": list(urls),
            "urls": urls,
        }
    return _release_cache[key]


def latest_versions():
    """{tool key: version the installer would fetch} for every tracked tool.

    Keyed by DISPLAY_NAMES - the exact set `server.main` /api/dependencies
    serves - so every row the UI can show has an entry. No network needed.

    Off Windows the pinned Windows builds are not what a user runs and not
    fetchable either (`_require_windows` refuses them): the install path is
    LINUX_PACKAGES (distro package) or PIP_PACKAGES, so the target reported is
    the distro package, and a tool with no Linux build at all reports None
    (the UI shows "unknown" rather than a version nobody can install).
    """
    out = {}
    for key in DISPLAY_NAMES:
        out[key] = PINNED.get(key, {}).get("version")
        if os.name != "nt" and key in LINUX_PACKAGES:
            pkg = LINUX_PACKAGES[key]
            out[key] = f"apt: {pkg}" if pkg else None
    return out


# ----------------------------------------------------------------------
# Live upstream versions
# ----------------------------------------------------------------------
# How long an upstream answer stays usable. GitHub's anonymous API allows 60
# requests/hour; one request per GitHub-published tool (12) every 30 minutes is
# 24/hour, so a full pass has headroom and a page load never hammers the API.
UPSTREAM_TTL_S = 30 * 60

# {key: {"version": str|None, "checked_at": float, "error": str|None}}
_upstream_cache = {}
_upstream_lock = threading.Lock()
_upstream_thread = None
_upstream_running = False
_upstream_done_at = None
_upstream_error = None


def _iso(timestamp):
    """UTC ISO-8601 for a `time.time()` stamp, or None (JSON-friendly)."""
    if not timestamp:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def _version_label(version):
    """Comparable label for a version or GitHub tag, or None when the string
    carries no version at all (a rolling release tagged `latest`).

    Three forms must compare equal, or an update is reported that installing
    cannot fix:
      * GitHub tags carry the `v`/`V` prefix the pinned labels drop
      * ffmpeg's rolling releases are tagged `autobuild-<date>-<time>` while
        PINNED labels that same build by its date
      * yt-dlp's tag is zero-padded (`2026.08.19`) where its label is not
        (`2026.8.19`)
    """
    if not version:
        return None
    text = str(version).strip()
    m = re.match(r"^autobuild-(\d{4})-(\d{2})-(\d{2})", text, re.IGNORECASE)
    if m:
        text = ".".join(m.groups())
    text = text.lstrip("vV")
    if not re.search(r"\d", text):
        return None
    return ".".join(
        str(int(part)) if part.isdigit() else part for part in text.split("."))


def _upstream_keys():
    """Tools whose newest release a GitHub API call can answer.

    Only repos in REPOS: php (windows.php.net), simple-dr-meter (a tag archive)
    and the two PyPI packages publish elsewhere, so their rows keep the pinned
    target and report no upstream version at all.
    """
    return [key for key in DISPLAY_NAMES if key in REPOS]


def _fetch_upstream(key):
    """Newest release of *key* as a version label, or None when it has no tag.

    The same GitHub call as get_latest_release() - which is asked for the
    PINNED tag instead - so the API handling (headers, JSON, 30 s timeout)
    stays in one place.
    """
    data = _api_json(
        f"https://api.github.com/repos/{REPOS[key]}/releases/latest")
    return _version_label(data.get("tag_name"))


def _refresh_upstream(keys=None):
    """One pass over *keys*; every failure is kept per tool, never raised.

    A failed check still stamps `checked_at`, so it is retried next TTL window
    rather than on every request, and the tool keeps the version it already had
    - a GitHub outage shows the last known value, not an empty table.
    """
    global _upstream_done_at, _upstream_error
    keys = _upstream_keys() if keys is None else keys
    last_error = None
    for key in keys:
        previous = _upstream_cache.get(key) or {}
        try:
            entry = {"version": _fetch_upstream(key), "checked_at": time.time(),
                     "error": None}
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"[:200]
            entry = {"version": previous.get("version"), "checked_at": time.time(),
                     "error": last_error}
        with _upstream_lock:
            _upstream_cache[key] = entry
    with _upstream_lock:
        _upstream_done_at = time.time()
        _upstream_error = last_error


def _upstream_stale(keys, now):
    return any(
        not _upstream_cache.get(key)
        or (now - _upstream_cache[key]["checked_at"]) >= UPSTREAM_TTL_S
        for key in keys
    )


def _kick_upstream(keys, force=False):
    """Start ONE background pass when something is stale (or `force`), and say
    whether one is running.

    The request that found the cache stale answers from what is known while the
    thread fetches; the versions land in the cache for the next request.
    """
    global _upstream_thread, _upstream_running
    with _upstream_lock:
        if _upstream_running:
            return True
        if not force and not _upstream_stale(keys, time.time()):
            return False
        _upstream_running = True

    def _work():
        global _upstream_running
        try:
            _refresh_upstream(keys)
        finally:
            with _upstream_lock:
                _upstream_running = False

    _upstream_thread = threading.Thread(target=_work, name="deps-upstream",
                                        daemon=True)
    _upstream_thread.start()
    return True


def upstream_versions(refresh=False, block=False):
    """{key: {"version", "checked_at", "error"}} for GitHub-published tools.

    Never blocks a caller on GitHub: by default a stale (or `refresh=True`
    forced) cache is re-fetched by a background thread while the caller gets
    what is already known. `block=True` fetches inline - the CLI prints its
    table once and has nobody to return to.
    """
    keys = _upstream_keys()
    if block:
        if refresh or _upstream_stale(keys, time.time()):
            _refresh_upstream(keys)
    else:
        _kick_upstream(keys, force=refresh)
    with _upstream_lock:
        return {key: dict(value) for key, value in _upstream_cache.items()}


def _upstream_check_state():
    """(a pass is running, last completed pass, last pass's first error)."""
    with _upstream_lock:
        return _upstream_running, _upstream_done_at, _upstream_error


def dependency_rows(refresh=False, block=False):
    """One row per tool - the single source of truth for the API, the CLI table
    and the auto-update worker, so all three agree on what "update" means.

    Three versions per tool, deliberately NOT merged:
      installed_version  what is on disk / on PATH
      latest_version     the pinned target `install_dependency` fetches; the
                         reviewed release on purpose (see PINNED)
      upstream_version   what GitHub's newest release actually is (None while
                         unknown / not a GitHub tool)

    `state` is derived from the LIVE upstream value: `ok` (installed ==
    upstream), `update` (upstream known and different), `missing`, `error`
    (that tool's check failed). Rows with no upstream at all (PyPI, php,
    simple-dr-meter) fall back to the pinned pair, which is the only answer
    available for them.
    """
    tools = detect_all_tools()
    installed = installed_versions()
    latest = latest_versions()
    upstream = upstream_versions(refresh=refresh, block=block)
    out = []
    for key, name in DISPLAY_NAMES.items():
        info = tools.get(key) or {}
        exe = next((v for k, v in info.items() if k.endswith("_exe") and v), None)
        ver = info.get("version")
        iv = installed.get(key)
        have = iv or ver
        target = latest.get(key)
        entry = upstream.get(key) or {}
        uv = entry.get("version")
        err = entry.get("error")
        update_available = bool(
            uv and have and _version_label(uv) != _version_label(have))
        # A failed upstream check must NOT mark a healthy install as broken:
        # GitHub rate-limits unauthenticated callers, and a wall of red for a
        # transient 403 is worse than no check at all. The upstream cell and
        # the note carry the failure; the status falls back to the pinned
        # pair, which is the one answer always available.
        if not (iv or info):
            state = "missing"
        elif uv:
            state = "update" if update_available else "ok"
        elif target and have and _version_label(target) != _version_label(have):
            state = "update"
        else:
            state = "ok"
        if err:
            note = f"upstream check failed: {err} — status is against the pinned target"
        elif key not in REPOS:
            note = "no GitHub releases — only the pinned target is installable"
        elif entry and not uv:
            note = "upstream has no versioned release (rolling build)"
        else:
            note = None
        out.append({
            "key": key,
            "name": name,
            "installed_version": iv,
            "latest_version": target,
            "detected_version": ver,
            "path": exe,
            "state": state,
            "upstream_version": uv,
            "upstream_checked_at": _iso(entry.get("checked_at")),
            "update_available": update_available,
            "note": note,
        })
    return out


def dependencies_payload(refresh=False):
    """The `/api/dependencies` body: the rows plus the state of the check.

    `checking` is true while a background pass is in flight (the UI polls on
    it) and `note` carries the last pass's error, so a GitHub failure is
    visible instead of silent.
    """
    rows = dependency_rows(refresh=refresh)
    running, done, err = _upstream_check_state()
    return {
        "tools": rows,
        "checking": running,
        "upstream_checked_at": _iso(done),
        "note": err,
    }


def installed_versions():
    """{tool key: installed version} for currently detected tools only."""
    tools = detect_all_tools()
    out = {key: info["version"] for key, info in tools.items()}
    if tools_mod_simple_dr_meter():
        out["simpledrmeter"] = PINNED["simpledrmeter"]["version"]
    for key in PIP_PACKAGES:
        if pip_package_path(key):
            out[key] = PINNED[key]["version"]
    # slskd has an exe but detect_all_tools doesn't scan for it; check the
    # marker directly so the Dependencies UI shows it correctly.
    d = installed_path("slskd")
    if d and os.path.isfile(os.path.join(d, "slskd.exe")):
        out["slskd"] = PINNED["slskd"]["version"]
    # Same for chromaprint's fpcalc - detect_all_tools doesn't scan for it.
    d = installed_path("chromaprint")
    if d and any(os.path.isfile(os.path.join(d, n))
                 for n in MARKER_EXES["chromaprint"]):
        out["chromaprint"] = PINNED["chromaprint"]["version"]
    return out


def pip_package_path(key):
    """Folder of a vendored pip package (e.g. '.dependencies/librosa v0.11.0')
    when its top-level package dir is present, else None."""
    return python_pkg_path(key)


def tools_mod_simple_dr_meter():
    from .tools import simple_dr_meter_path
    return simple_dr_meter_path() is not None


def _require_windows(key):
    """Refuse Windows-only downloads on a non-Windows host.

    Nothing else in the install path knows the platform: the archive unpacks
    fine and the marker check passes, so a Linux install used to report
    success for tools it can never run.
    """
    if os.name == "nt" or key not in LINUX_PACKAGES:
        return
    display = DISPLAY_NAMES.get(key, key)
    pkg = LINUX_PACKAGES[key]
    if pkg:
        raise RuntimeError(
            f"{display} is distributed as a Windows binary only - install the "
            f"system package instead (Debian/Ubuntu: apt-get install {pkg}; "
            f"the Docker image already ships it)."
        )
    raise RuntimeError(
        f"{display} is a Windows binary only and has no Linux build - it is "
        f"unsupported on this platform."
    )


def pick_asset(key):
    """Return the exact pinned asset name for a tool, if it exists."""
    _require_windows(key)
    pin = PINNED.get(key) or {}
    if pin.get("asset"):
        rel = get_latest_release(key)
        if pin["asset"] in rel["assets"]:
            return pin["asset"]
    return _pattern_asset(key)


def _pattern_asset(key, assets=None):
    """First release asset matching ASSET_PATTERNS for a tool."""
    rel = get_latest_release(key)
    names = assets if assets is not None else rel["assets"]
    for pattern in ASSET_PATTERNS.get(key, []):
        rx = re.compile(pattern, re.IGNORECASE)
        for name in names:
            if rx.match(name):
                return name
    return None


def _fallback_assets(key, rel, exclude=""):
    """Assets in a release matching our patterns, minus a failed pin."""
    candidates = [a for a in rel["assets"] if a != exclude]
    picked = _pattern_asset(key, candidates)
    return [picked] if picked else []


# ----------------------------------------------------------------------
# Download / extraction helpers
# ----------------------------------------------------------------------
def _download(url, dest_path, progress=None):
    req = urllib.request.Request(url, headers={"User-Agent": _HEADERS["User-Agent"]})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest_path, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done, total)
    if total and done != total:
        raise RuntimeError(
            f"incomplete download: {done}/{total} bytes")
    if done < 4096:
        raise RuntimeError("downloaded file is unexpectedly small")


def _find_7z():
    path = shutil.which("7z") or shutil.which("7za")
    if path:
        return path
    for candidate in (
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def _windows_short_path(path):
    """8.3 short path (space-free) for NSIS /D=, or None on failure."""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(
            os.path.abspath(path), buf, len(buf)
        )
        if 0 < n < len(buf):
            return buf.value
    except Exception:
        pass
    return None


def _extract_with_7z(sevenz, archive_path, dest_dir):
    result = run_tool(
        [sevenz, "x", "-y", f"-o{dest_dir}", archive_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=180, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"7-Zip extraction failed (rc={result.returncode})")


def _extract_archive(archive_path, dest_dir, log):
    """Extract zip / 7z / NSIS installer into dest_dir."""
    lower = archive_path.lower()

    if lower.endswith(".zip"):
        try:
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(dest_dir)
            return
        except Exception:
            # Some release zips (libjxl) use methods zipfile cannot read;
            # fall through to 7-Zip if it is available.
            sevenz = _find_7z()
            if not sevenz:
                raise RuntimeError(
                    "This zip uses a compression method Python cannot read "
                    "and 7-Zip is not installed. Install 7-Zip and retry."
                )
            _extract_with_7z(sevenz, archive_path, dest_dir)
            return

    if lower.endswith(".7z"):
        sevenz = _find_7z()
        if not sevenz:
            raise RuntimeError("Extracting .7z archives requires 7-Zip.")
        _extract_with_7z(sevenz, archive_path, dest_dir)
        return

    # NSIS installer.
    sevenz = _find_7z()
    if sevenz:
        _extract_with_7z(sevenz, archive_path, dest_dir)
        return

    log("  7-Zip not found - falling back to silent install of the installer.")
    target = _windows_short_path(dest_dir) or dest_dir
    if " " in target:
        raise RuntimeError(
            "Cannot silently install: temporary path contains spaces and "
            "7-Zip is unavailable. Install 7-Zip and retry."
        )
    # /D= is passed through cmd.exe unquoted, so shell metacharacters in the
    # path would either break the command or inject into it.
    if any(ch in target for ch in '&^|<>"'):
        raise RuntimeError(
            "Cannot silently install: temporary path contains shell "
            "metacharacters and 7-Zip is unavailable."
        )
    # /D must be the last argument and unquoted.
    result = run_tool(
        f'"{archive_path}" /S /D={target}',
        shell=True, timeout=300, capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"silent install failed (rc={result.returncode})")


def _locate_binaries(root, key):
    """Find the directory containing the tool's marker exes."""
    markers = MARKER_EXES[key]
    candidates = []
    for dirpath, _dirnames, filenames in os.walk(root):
        names = {f.lower() for f in filenames}
        if all(m.lower() in names for m in markers):
            candidates.append(dirpath)

    if not candidates:
        return None
    # Prefer 64-bit layouts (flac zip ships Win64 + Win32 side by side).
    for cand in candidates:
        low = cand.lower()
        if "win64" in low or "x64" in low:
            return cand
    return candidates[0]


# ----------------------------------------------------------------------
# Installation
# ----------------------------------------------------------------------
_LICENCE_RX = re.compile(
    r"^(licen[cs]e|copying|copyright|notice|readme)(\.[a-z0-9._-]+)?$", re.IGNORECASE)


def _copy_licence_files(root, dest_dir, log=print):
    """Carry an archive's LICENSE/COPYING/NOTICE/README into dest_dir.

    These tools are downloaded from upstream on the user's behalf, so the
    licence text has to arrive with them (GPL-2.0 §1, LGPL-2.1 §1 ask for the
    licence and a source offer alongside the binary). The files sit anywhere
    in the archive - rarely beside the executable - so walk the whole extract.
    """
    copied = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            if fname in copied or not _LICENCE_RX.match(fname):
                continue
            try:
                shutil.copy2(os.path.join(dirpath, fname),
                             os.path.join(dest_dir, fname))
                copied.append(fname)
            except OSError:
                pass
    if copied:
        log(f"  licence files: {', '.join(sorted(copied))}")
    return copied


def _existing_install(prefix, markers):
    """Folder name of an existing WORKING install of *prefix*, or None.

    A fresh install must land in the folder that is already there — the
    shipped layout of a rolling-release tool is `ffmpeg vlatest`, while a
    pinned install would otherwise create `ffmpeg v2026.8.19` beside it and
    leave the detector two folders to choose from.

    Only a folder carrying every marker executable counts: a half-written or
    manually emptied folder is never treated as the install to reuse.
    """
    if not os.path.isdir(DEPS_DIR):
        return None
    rx = re.compile(rf"^{re.escape(prefix)}\s+v", re.IGNORECASE)
    found = [
        entry for entry in os.listdir(DEPS_DIR)
        if rx.match(entry)
        and os.path.isdir(os.path.join(DEPS_DIR, entry))
        and all(os.path.isfile(os.path.join(DEPS_DIR, entry, m)) for m in markers)
    ]
    # A `vlatest` folder is the shipped layout for rolling releases: keep it
    # rather than renaming the install to the pinned version label.
    found.sort(key=lambda entry: (not entry.lower().endswith("vlatest"), entry.lower()))
    return found[0] if found else None


def _remove_older_versions(prefix, keep_dir):
    if not os.path.isdir(DEPS_DIR):
        return
    # Versioned folders, `vlatest` included: a folder the installer can name
    # must also be one the stale-version pruner can reconcile, or every new
    # install leaves a second ffmpeg folder behind that is never cleaned up.
    # keep_dir (the install just verified to carry its marker executables) is
    # never touched.
    rx = re.compile(rf"^{re.escape(prefix)}\s+v(?:\d|latest$)", re.IGNORECASE)
    for entry in os.listdir(DEPS_DIR):
        full = os.path.join(DEPS_DIR, entry)
        if os.path.isdir(full) and rx.match(entry) and entry != keep_dir:
            shutil.rmtree(full, ignore_errors=True)


def _install_simple_dr_meter(log=print, progress=None):
    """Download the simple-dr-meter source archive (no binaries exist)."""
    dest_dir = os.path.join(DEPS_DIR, "simple-dr-meter")
    fd, tmp_zip = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    workdir = tempfile.mkdtemp(prefix="mlo_drmeter_")
    try:
        log("Downloading simple-dr-meter (source archive) …")
        _download(SIMPLE_DR_METER_ZIP_URL, tmp_zip, progress)
        log("Extracting simple-dr-meter …")
        with zipfile.ZipFile(tmp_zip) as zf:
            zf.extractall(workdir)
        # The archive extracts to <workdir>/simple-dr-meter-main/
        src_candidates = [
            os.path.join(workdir, d)
            for d in os.listdir(workdir)
            if os.path.isdir(os.path.join(workdir, d))
            and "simple-dr-meter" in d.lower()
        ]
        if not src_candidates or not os.path.isfile(
                os.path.join(src_candidates[0], "main.py")):
            raise RuntimeError("Could not find simple-dr-meter main.py in archive")
        src = src_candidates[0]
        shutil.rmtree(dest_dir, ignore_errors=True)
        shutil.copytree(src, dest_dir)
        log(f"Installed simple-dr-meter -> {dest_dir}")
        return "main"
    finally:
        try:
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)
        except OSError:
            pass
        shutil.rmtree(workdir, ignore_errors=True)


def _pip_python():
    """Interpreter for `pip install --target`; sys.executable is the frozen
    exe (not a python) in PyInstaller builds."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    for cand in ("python", "python3", "py"):
        found = shutil.which(cand)
        if found:
            return found
    raise RuntimeError("vendored Python packages need a Python interpreter on PATH")


def _install_pip_package(key, log=print, progress=None):
    """Vendor a pure-Python tool into .dependencies with pip --target.

    Keeps the running interpreter's site-packages untouched (portable
    installs) and mirrors the versioned-folder layout of binary tools.
    """
    pin = PINNED[key]
    version = pin["version"]
    display = DISPLAY_NAMES[key]
    dest_dir = os.path.join(DEPS_DIR, f"{key} v{version}")
    if pip_package_path(key):
        log(f"{display} v{version} already installed")
        return version
    log(f"Downloading {display} v{version} (pip) …")
    cmd = [
        _pip_python(), "-m", "pip", "install",
        "--target", dest_dir, "--no-cache-dir",
        "--progress-bar", "off", "--disable-pip-version-check",
        PIP_PACKAGES[key],
    ]
    proc = run_tool(cmd, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=1800)
    if proc.returncode != 0 or not pip_package_path(key):
        shutil.rmtree(dest_dir, ignore_errors=True)
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(
            f"pip install failed for {display}: {tail[-1] if tail else 'unknown error'}")
    _remove_older_versions(key, os.path.basename(dest_dir))
    log(f"Installed {display} v{version} -> {dest_dir}")
    return version


def _install_php(log=print, progress=None):
    """Download PHP for Windows (needed for Logchecker phar)."""
    pin = PINNED["php"]
    version = pin["version"]
    display = DISPLAY_NAMES["php"]
    log(f"Downloading {display} v{version} (php zip) …")
    dest_dir = os.path.join(DEPS_DIR, f"php v{version}")
    fd, tmp_zip = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    workdir = tempfile.mkdtemp(prefix="mlo_php_")
    try:
        _download(PHP_ZIP_URL, tmp_zip, progress)
        log(f"Extracting PHP v{version} …")
        _extract_archive(tmp_zip, workdir, log)
        src = _locate_binaries(workdir, "php")
        if src is None:
            # Fallback: workdir itself may contain php.exe directly
            if os.path.isfile(os.path.join(workdir, "php.exe")):
                src = workdir
            else:
                # Search one level deeper
                for entry in os.listdir(workdir):
                    cand = os.path.join(workdir, entry)
                    if os.path.isdir(cand) and os.path.isfile(os.path.join(cand, "php.exe")):
                        src = cand
                        break
        if src is None:
            raise RuntimeError("Could not find php.exe inside the archive")
        os.makedirs(dest_dir, exist_ok=True)
        for fname in os.listdir(src):
            s = os.path.join(src, fname)
            d = os.path.join(dest_dir, fname)
            if os.path.isfile(s):
                shutil.copy2(s, d)
            elif os.path.isdir(s):
                if os.path.exists(d):
                    shutil.rmtree(d, ignore_errors=True)
                shutil.copytree(s, d)
        names = {f.lower() for f in os.listdir(dest_dir)}
        if "php.exe" not in names:
            raise RuntimeError("Installed folder is missing: php.exe")
        _copy_licence_files(workdir, dest_dir, log)
        _remove_older_versions("php", os.path.basename(dest_dir))
        log(f"Installed {display} v{version} -> {dest_dir}")
        return version
    finally:
        try:
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)
        except OSError:
            pass
        shutil.rmtree(workdir, ignore_errors=True)


def _patch_simple_dr_meter(root_dir):
    """Apply known compatibility fixes to the installed simple-dr-meter:
    empty-peaks (silent/very short tracks) crash the batch otherwise.
    Idempotent — safe to run after every install/update."""
    main_py = os.path.join(root_dir, "main.py")
    metrics_py = os.path.join(root_dir, "audio_metrics", "audio_metrics.py")
    for path, old, new in (
        (metrics_py,
         "    peak_index = block_count - 2\n    rms_percentile = 0.2",
         "    if block_count < 2:\n        return None  # too few blocks (silent/very short track): no DR\n\n    peak_index = block_count - 2\n    rms_percentile = 0.2"),
        (main_py,
         "        for track_info, dr_metrics in analyzed_tracks:\n            dr = dr_metrics.dr",
         "        for track_info, dr_metrics in analyzed_tracks:\n            if dr_metrics is None:\n                # silent/very short track produced no blocks - skip\n                continue\n            dr = dr_metrics.dr"),
        (main_py,
         "    if keep_precision:\n        dr_mean_rounded = numpy.mean(dr_items)\n    else:\n        dr_mean_rounded = int(numpy.round(numpy.mean(dr_items)))  # official\n    dr_median = numpy.median(dr_items)",
         "    valid = [d for d in dr_items if d is not None and d == d]\n    if not valid:\n        valid = [0]\n    if keep_precision:\n        dr_mean_rounded = numpy.mean(valid)\n    else:\n        dr_mean_rounded = int(numpy.round(numpy.mean(valid)))  # official\n    dr_median = numpy.median(valid)"),
    ):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            if old not in text:
                continue
            with open(path, "w", encoding="utf-8") as f:
                f.write(text.replace(old, new))
        except OSError:
            pass


def install_dependency(key, log=print, progress=None):
    """Download and install the latest release of a tool.

    Returns the installed version string. Raises on any failure.
    """
    _require_windows(key)
    if key == "simpledrmeter":
        version = _install_simple_dr_meter(log=log, progress=progress)
        _patch_simple_dr_meter(os.path.join(DEPS_DIR, "simple-dr-meter"))
        return version
    if key == "php":
        return _install_php(log=log, progress=progress)
    # Vendored pip packages — plus the tools whose Linux install IS the pip
    # package (PIP_ON_LINUX): on Windows those take the pinned .exe below.
    if key in PIP_PACKAGES and (key not in PIP_ON_LINUX or os.name != "nt"):
        return _install_pip_package(key, log=log, progress=progress)

    rel = get_latest_release(key)
    version = rel["version"]
    asset = pick_asset(key)

    if not asset:
        raise RuntimeError(f"No suitable Windows asset in latest {key} release")

    display = DISPLAY_NAMES[key]
    prefix = INSTALL_PREFIX[key]
    # Install into the folder a working copy already lives in (the shipped
    # `ffmpeg vlatest`, or this tool's pinned folder) so a fresh install can
    # never end up as a second folder beside it — see _existing_install.
    existing = _existing_install(prefix, MARKER_EXES[key])
    dest_dir = os.path.join(DEPS_DIR, existing or f"{prefix} v{version}")

    tmp_archived_fd, tmp_archived = tempfile.mkstemp(
        suffix=os.path.splitext(asset)[1])
    os.close(tmp_archived_fd)
    workdir = tempfile.mkdtemp(prefix="mlo_dep_")

    try:
        try:
            log(f"Downloading {display} v{version} ({asset}) …")
            _download(rel["urls"][asset], tmp_archived, progress)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            # Rolling releases (e.g. BtbN ffmpeg autobuilds) rotate assets
            # out of old tags — a pinned name can disappear. Fall back to
            # any other asset in the same release matching our patterns.
            fallbacks = _fallback_assets(key, rel, exclude=asset)
            if not fallbacks:
                raise
            asset = fallbacks[0]
            suffix = os.path.splitext(asset)[1]
            if suffix != os.path.splitext(tmp_archived)[1]:
                os.remove(tmp_archived)
                fd, tmp_archived = tempfile.mkstemp(suffix=suffix)
                os.close(fd)
            log(f"Pinned asset is gone (404) — falling back to {asset}")
            _download(rel["urls"][asset], tmp_archived, progress)

        if key in SINGLE_EXE_TOOLS:
            # The release asset is the tool itself - no extraction step.
            os.makedirs(dest_dir, exist_ok=True)
            shutil.copy2(tmp_archived,
                         os.path.join(dest_dir, MARKER_EXES[key][0]))
        else:
            log(f"Extracting {asset} …")
            _extract_archive(tmp_archived, workdir, log)

            src = _locate_binaries(workdir, key)
            if src is None:
                raise RuntimeError(
                    f"Could not find {' + '.join(MARKER_EXES[key])} inside the archive"
                )

            os.makedirs(dest_dir, exist_ok=True)
            for fname in os.listdir(src):
                s = os.path.join(src, fname)
                if os.path.isfile(s):
                    try:
                        shutil.copy2(s, os.path.join(dest_dir, fname))
                    except OSError as e:
                        # Windows refuses to replace a file another process is
                        # EXECUTING (WinError 32), and slskd is the one tool
                        # this app runs — so "install all" used to end with a
                        # bare "used by another process" and nothing explaining
                        # it. The server stops the managed daemon around the
                        # install (see server.main.dependencies_install); this
                        # is the honest message for anything else holding a
                        # file open.
                        raise RuntimeError(
                            f"{display} is running — {fname} is in use by another "
                            f"process, so it cannot be replaced. Stop it and "
                            f"install again ({type(e).__name__}: {e})") from e
            _copy_licence_files(workdir, dest_dir, log)

        names = {f.lower() for f in os.listdir(dest_dir)}
        missing = [m for m in MARKER_EXES[key] if m.lower() not in names]
        if missing:
            raise RuntimeError(f"Installed folder is missing: {', '.join(missing)}")

        _remove_older_versions(prefix, os.path.basename(dest_dir))
        log(f"Installed {display} v{version} -> {dest_dir}")
        return version

    finally:
        for path in (tmp_archived,):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        shutil.rmtree(workdir, ignore_errors=True)


def refresh_tool_cache():
    """Force re-detection of .dependencies on the next detect_all_tools().

    The per-module latency caches latch too: mlo.audio and mlo.loudness each
    remember "no ffprobe/ffmpeg" for the life of the process, so installing
    ffmpeg from the Dependencies UI mid-session used to leave video tag reads
    and on-demand ReplayGain failing until the app restarted."""
    import mlo.tools as tools_mod
    tools_mod._TOOLS_CACHE = None
    for module_name, attr in (("mlo.audio", "_FFPROBE_CACHE"),
                              ("mlo.loudness", "_FFMPEG_CACHE")):
        try:
            import importlib
            mod = importlib.import_module(module_name)
            cache = getattr(mod, attr, None)
            if isinstance(cache, dict):
                cache["exe"] = None
                cache["checked"] = False
        except Exception:
            pass
    return detect_all_tools()


# ----------------------------------------------------------------------
# Automatic updates
# ----------------------------------------------------------------------
# How often the loop looks at the config flag. Short, because the flag is what
# decides whether anything happens: switching it off stops the next pass, and
# switching it on starts one within a tick.
AUTO_UPDATE_TICK_S = 300

# One install pass per 6 h. Installing downloads the PINNED release and the pin
# does not move between passes, so a shorter interval only re-fetches the same
# file. ponytail: fixed interval, make it a config key if it ever needs tuning.
AUTO_UPDATE_INTERVAL_S = 6 * 3600

_auto_thread = None
_auto_stop = threading.Event()


def auto_update_enabled():
    """The `dependencies_auto_update` switch, read fresh on every pass so
    turning it off stops the next one instead of a cached answer."""
    try:
        from .config import load_config
        return bool(load_config().get("dependencies_auto_update", False))
    except Exception:
        return False


def auto_update_pass(log=print):
    """Install every tool whose state is `missing` or `update`, once.

    Never raises into the caller: a failed tool is logged and picked up again
    next pass. States come from dependency_rows(), i.e. the LIVE upstream
    values (its own non-blocking cache; the background check fills it).
    """
    try:
        rows = dependency_rows()
    except Exception as e:
        log(f"[deps] auto-update: could not list the tools ({e})")
        return 0
    changed = 0
    for row in rows:
        if row["state"] not in ("missing", "update"):
            continue
        # An "update" can mean only that UPSTREAM moved past the pin this app
        # ships (the pin is what install_dependency fetches). Re-installing it
        # every pass would download the same archive forever and change
        # nothing — the pin only moves with an app release.
        if (row.get("installed_version") and row.get("latest_version")
                and _version_label(row["installed_version"]) == _version_label(row["latest_version"])):
            continue
        try:
            install_dependency(row["key"], log=lambda m: None)
            log(f"[deps] auto-update: {row['name']} was {row['state']}, "
                f"installed {row['latest_version'] or 'the pinned release'}")
            changed += 1
        except Exception as e:
            log(f"[deps] auto-update: {row['name']} failed: {e}")
    if changed:
        try:
            refresh_tool_cache()
        except Exception:
            pass
    return changed


def _auto_update_loop():
    """Flag check, then at most one pass per interval."""
    last = 0.0
    while not _auto_stop.wait(AUTO_UPDATE_TICK_S):
        try:
            if not auto_update_enabled():
                continue
            if time.time() - last < AUTO_UPDATE_INTERVAL_S:
                continue
            last = time.time()
            auto_update_pass()
        except Exception as e:  # the loop outlives any single pass
            print(f"[deps] auto-update loop error: {e}")


def ensure_auto_update_worker():
    """Start the auto-update loop, once, in the background.

    Started lazily by the app (the first /api/dependencies request) - that is
    also the first moment anything knows which tools are installed.
    """
    global _auto_thread
    if _auto_thread is not None and _auto_thread.is_alive():
        return False
    _auto_stop.clear()
    _auto_thread = threading.Thread(target=_auto_update_loop,
                                    name="deps-auto-update", daemon=True)
    _auto_thread.start()
    return True
