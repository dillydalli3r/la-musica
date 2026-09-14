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
the required binaries are copied out. Standard-library only - no requests.
"""

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import urllib.request

from .paths import DEPS_DIR
from .subproc import run_tool
from .tools import detect_all_tools

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
}

# Vendored pure-Python tools: installed with `pip install --target` into a
# versioned .dependencies folder instead of shipping binaries. They are
# imported by prepending the folder to sys.path (see tools.python_pkg_path).
PIP_PACKAGES = {
    "librosa": "librosa==0.11.0",
    "beets": "beets==2.4.0",
}

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
    return os.path.join(DEPS_DIR, prefix)

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
}

# Tools whose release asset is a single bare exe - no archive to extract.
SINGLE_EXE_TOOLS = {"audioauditor", "logchecker"}

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
    """{tool key: pinned version string} for all tools (no network needed)."""
    # Return pinned versions for every tool we track (REPOS + php/simpledrmeter)
    out = {key: PINNED[key]["version"] for key in PINNED}
    return out


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
    return out


def pip_package_path(key):
    """Folder of a vendored pip package (e.g. '.dependencies/librosa v0.11.0')
    when its top-level package dir is present, else None."""
    root = installed_path(key)
    if not root or not os.path.isdir(root):
        return None
    top = key  # package name matches the tool key (librosa, beets)
    if os.path.isfile(os.path.join(root, top, "__init__.py")):
        return root
    return None


def tools_mod_simple_dr_meter():
    from .tools import simple_dr_meter_path
    return simple_dr_meter_path() is not None


def pick_asset(key):
    """Return the exact pinned asset name for a tool, if it exists."""
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
def _remove_older_versions(prefix, keep_dir):
    if not os.path.isdir(DEPS_DIR):
        return
    rx = re.compile(rf"^{re.escape(prefix)}\s+v?\d", re.IGNORECASE)
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
    if key == "simpledrmeter":
        version = _install_simple_dr_meter(log=log, progress=progress)
        _patch_simple_dr_meter(os.path.join(DEPS_DIR, "simple-dr-meter"))
        return version
    if key == "php":
        return _install_php(log=log, progress=progress)
    if key in PIP_PACKAGES:
        return _install_pip_package(key, log=log, progress=progress)

    rel = get_latest_release(key)
    version = rel["version"]
    asset = pick_asset(key)

    if not asset:
        raise RuntimeError(f"No suitable Windows asset in latest {key} release")

    display = DISPLAY_NAMES[key]
    prefix = INSTALL_PREFIX[key]
    dest_dir = os.path.join(DEPS_DIR, f"{prefix} v{version}")

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
                    shutil.copy2(s, os.path.join(dest_dir, fname))

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
    """Force re-detection of .dependencies on the next detect_all_tools()."""
    import mlo.tools as tools_mod
    tools_mod._TOOLS_CACHE = None
    return detect_all_tools()
