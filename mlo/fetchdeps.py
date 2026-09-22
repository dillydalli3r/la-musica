"""Automatic dependency fetcher.

Downloads the official builds of the external encoder toolchain from GitHub
releases and installs them into .dependencies/ using exactly the layout the
auto-detection in tools.py expects:

    .dependencies/
        flac v1.5.0/           flac.exe, metaflac.exe      (Windows)
        oxipng v10.2.0/        oxipng.exe | oxipng        (Windows | Linux)
        slskd v0.26.0/         slskd.exe | slskd + wwwroot

Asset sources:
    flac            xiph/flac          flac-<v>-win.zip
    libjxl          libjxl/libjxl      jxl-x64-windows-static.zip
    libjpeg-turbo   libjpeg-turbo/...  libjpeg-turbo-<v>-vc-x64.exe (NSIS)
    oxipng          oxipng/oxipng      oxipng-<v>-x86_64-pc-windows-msvc.zip
                                       oxipng-<v>-x86_64-unknown-linux-musl.tar.gz
    slskd           slskd/slskd        slskd-<v>-win-x64.zip
                                       slskd-<v>-linux-musl-x64.zip
    AudioAuditor    Angel2mp3/...      AudioAuditorCLI-win-x64.exe (bare exe)

The libjpeg-turbo release only ships NSIS installers for Windows; those are
unpacked with 7-Zip when available, otherwise installed silently into a
temporary folder (which needs a space-free path, hence GetShortPathName) and
the required binaries are copied out.

Platforms: install_kind() is the single answer to how a tool installs here.
Every tool has a Windows build; the ones upstream also ships a Linux build for
(LINUX_BINARIES - oxipng, slskd) install natively there too; the rest are distro
packages (LINUX_PACKAGES: flac, ffmpeg, …) or have no build this app can use
(AudioAuditor, CUETools, Logchecker + php). An install the platform cannot
perform is refused with that reason instead of downloading something that cannot
run, and the Dependencies rows carry the same answer so the UI never shows an
Install button for a tool that cannot be installed here.

The vendored pip packages (librosa, beets, yt-dlp) are platform-independent.
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
import tarfile
import tempfile
import threading
import time
import zipfile
import urllib.request

from .paths import DEPS_DIR
from .subproc import run_tool
from .tools import (
    PIP_IMPORT_NAMES,
    detect_all_tools,
    python_pkg_path,
    python_pkg_version,
)

DISPLAY_NAMES = {
    "flac": "FLAC",
    "libjxl": "libjxl",
    "libjpeg_turbo": "libjpeg-turbo",
    "oxipng": "oxipng",
    "audioauditor": "AudioAuditor",
    "rsgain": "rsgain",
    "ffmpeg": "ffmpeg",
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

# Tools whose upstream releases ship a NATIVE Linux build, mapped to the asset
# name that build carries per architecture and the files the installer must
# find inside it. The release tag and the version label are the shared PINNED
# ones: both platforms install from the same GitHub release.
#
# This table is what makes these installable at all in a container. Debian
# bookworm has no oxipng package, so the Docker image cannot apt-install it and
# its row sat "missing" for ever behind an Install button that refused - and
# slskd, the one dependency this app RUNS, ships Linux binaries that make
# Soulseek work in the Docker image.
#
# Each pattern key picks the build for one machine+libc: "x64"/"arm64" is the
# host's libc as named there, "<arch>-musl" the musl one. A tool published for a
# single libc (oxipng's static musl tarball) may cover both — see _linux_pattern.
LINUX_BINARIES = {
    "oxipng": {
        # Static musl build: one file that runs on glibc and musl alike
        # (verified in the Debian-based image).
        "patterns": {
            "x64": r"^oxipng-[\d.]+-x86_64-unknown-linux-musl\.tar\.gz$",
            "arm64": r"^oxipng-[\d.]+-aarch64-unknown-linux-musl\.tar\.gz$",
        },
        "markers": ("oxipng",),
    },
    "slskd": {
        # .NET apphost, and dynamically linked: the musl zip names
        # /lib/ld-musl-x86_64.so.1 as its loader, which a glibc system does not
        # have, so it exits "not found" instead of running. Both libcs exist
        # upstream and the host's decides (see _linux_pattern).
        "patterns": {
            "x64": r"^slskd-[\d.]+-linux-x64\.zip$",
            "arm64": r"^slskd-[\d.]+-linux-arm64\.zip$",
            "x64-musl": r"^slskd-[\d.]+-linux-musl-x64\.zip$",
            "arm64-musl": r"^slskd-[\d.]+-linux-musl-arm64\.zip$",
        },
        "markers": ("slskd",),
    },
    "audioauditor": {
        # Self-contained .NET builds, one bare executable per architecture (no
        # archive), so the install copies the asset itself (SINGLE_EXE_TOOLS).
        # The pin's Windows asset predates these, which is why this row used to
        # read "No build here" on Linux — upstream had shipped them all along.
        "patterns": {
            "x64": r"^AudioAuditorCLI-linux-x64$",
            "arm64": r"^AudioAuditorCLI-linux-arm64$",
        },
        "markers": ("AudioAuditorCLI",),
    },
    "cuetools": {
        # Windows binaries only, and its console tool runs under the mono
        # runtime — verified on trixie: CUETools.ARCUE.exe prints its usage
        # under `mono`. Both architectures take the same x64/AnyCPU zip, and
        # the install writes a launcher beside it (see _write_launcher) so
        # callers keep invoking one executable.
        "patterns": {
            "x64": r"^CUETools_[\d.]+\.zip$",
            "arm64": r"^CUETools_[\d.]+\.zip$",
        },
        "markers": ("CUETools.ARCUE.exe",),
        "runner": "mono",
        "launcher": ("CUETools.ARCUE", "CUETools.ARCUE.exe"),
    },
    "rsgain": {
        # One static x86-64 build — the v3.8 asset `rsgain-3.8-Linux.tar.xz`
        # holds a bare `rsgain` (plus its presets/ folder), verified by
        # unpacking it. Upstream publishes NO arm64 Linux asset, so the key is
        # x64 only and an ARM host falls through to the distro package in
        # LINUX_PACKAGES instead of matching a pattern that cannot exist.
        "patterns": {
            "x64": r"^rsgain-\d+\.\d+(?:\.\d+)*-Linux\.tar\.xz$",
        },
        "markers": ("rsgain",),
    },
    "chromaprint": {
        # fpcalc, one tarball per architecture (verified in both: a single
        # `fpcalc`, statically linked, so nothing else has to be on the host).
        # The Windows zip in ASSET_PATTERNS is a different asset of the SAME
        # release, which is why the pin and this table can share one version.
        "patterns": {
            "x64": r"^chromaprint-fpcalc-\d+\.\d+(?:\.\d+)*-linux-x86_64\.tar\.gz$",
            "arm64": r"^chromaprint-fpcalc-\d+\.\d+(?:\.\d+)*-linux-arm64\.tar\.gz$",
        },
        "markers": ("fpcalc",),
    },
}

# Tools whose Linux install needs an interpreter to exist on this machine: a
# Windows build run through one (cuetools/mono, above), or a script (the
# Logchecker phar is PHP). Without the interpreter there is nothing to install
# — the row says which package provides it — and with it, the tool installs
# and runs like any other.
#
# cuetools names THREE packages, not just the runtime: CUETools' ARCUE pass
# loads System.Drawing to verify a disc, which mono-runtime does not provide —
# without libgdiplus and mono's own System.Drawing assembly it dies on "Could
# not load file or assembly 'System.Drawing'" and writes no .accurip at all.
# `mono CUETools.ARCUE.exe` with no arguments still prints its usage, so the
# installer cannot see the difference; the packages are named where the row
# tells the user what to install.
LINUX_RUNNERS = {
    "cuetools": ("mono", "mono-runtime libgdiplus libmono-system-drawing4.0-cil"),
    "logchecker": ("php", "php-cli"),
}

# Tools whose install is the same download on every platform this app supports
# (a pip package, or a phar a runtime elsewhere executes — the Logchecker phar
# is PHP), so no platform table decides anything about them.
PLATFORM_INDEPENDENT = {"librosa", "beets", "yt-dlp", "logchecker"}

# Tools with no build this app fetches, mapped to the distro package providing
# the same tool (None = no packaged equivalent). MARKER_EXES can only check that
# files with the right NAMES landed, so on Linux a Windows download would
# "succeed" with a folder of unrunnable .exe files. install_dependency()/
# pick_asset() refuse via _require_installable() instead, naming the distro
# package to use (the Docker image installs them; see Dockerfile).
#
# A key can be in BOTH this table and LINUX_BINARIES: upstream ships a build for
# one architecture and the distro package covers the rest. rsgain publishes
# x86-64 only, chromaprint publishes 64-bit ARM and x86-64 but nothing for a
# 32-bit ARM host — install_kind() takes the download where a pattern matches
# this machine and the package everywhere else, so no entry here is dead code
# while its tool has a build for SOME architecture.
LINUX_PACKAGES = {
    "flac": "flac",
    "libjxl": "libjxl-tools",
    "libjpeg_turbo": "libjpeg-progs",
    "ffmpeg": "ffmpeg",
    "rsgain": "rsgain",
    "chromaprint": "libchromaprint-tools",
    "php": "php-cli",
}

# Vendored pure-Python tools: installed with `pip install --target` into a
# versioned .dependencies folder instead of shipping binaries. They are
# imported by prepending the folder to sys.path (see tools.python_pkg_path).
#
# The pip NAME only. The version lives in PINNED (and in the version-stamped
# folder name), because the same number written twice is one number that can
# drift: `librosa==0.11.0` here against PINNED's 0.11.0 was two places to edit
# per bump, and the row read the folder while the installer read this string.
PIP_PACKAGES = {
    "librosa": "librosa",
    "beets": "beets",
    "yt-dlp": "yt-dlp",
}

# Pip packages whose releases GitHub does not carry, so their newest version
# comes from PyPI's JSON API instead (yt-dlp IS in REPOS and stays a GitHub
# check). Derived, never a second list to keep in sync with the one above.
PYPI_PROBES = {key for key in PIP_PACKAGES if key not in REPOS}

# Tools of which only the Windows build is vendored as a binary: on Linux the
# same program is installed as the pip package above (yt-dlp has no Linux
# release asset at all, and its pip package is the upstream-supported install).
# They are deliberately NOT in LINUX_PACKAGES - _require_installable() would
# refuse the download instead of using pip.
PIP_ON_LINUX = {"yt-dlp"}


# --------------------------------------------------------------------------- #
# How a tool installs here
# --------------------------------------------------------------------------- #
# One answer for the whole installer - which asset table applies, which marker
# files a finished install must carry, and whether there is anything to fetch
# at all - so the refusal message, the `latest_version` column and the row's
# Install button can never disagree about a tool.
def host_platform():
    """`windows` | `linux` | `other` for this host.

    Linux is the only non-Windows platform with downloads of its own
    (LINUX_BINARIES); everywhere else a tool is a pip package or one the user
    installs with the system package manager. The server runs in Docker or from
    a checkout on a desktop OS — no phone hosts a backend any more — so this is
    simply what the interpreter reports.
    """
    if os.name == "nt":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return "other"


def _platform_of(platform=None):
    """*platform* when the caller names one (tests), else the host's."""
    return platform or host_platform()


def _machine():
    """This host's CPU architecture name, the direct way.

    `os.uname()` on POSIX, and the environment on Windows, which has no
    `os.uname`. Deliberately NOT platform.machine(): that goes through a cached
    platform.uname() keyed on sys.platform, so it answers for the wrong system
    the moment the platform is simulated — and returns "" rather than raising,
    which would silently turn every native build into "unsupported".
    """
    uname = getattr(os, "uname", None)
    if uname is not None:
        try:
            return uname().machine
        except OSError:
            pass
    return os.environ.get("PROCESSOR_ARCHITECTURE", "")


def linux_arch(machine=None):
    """`x64` | `arm64` for a machine name, else None when upstream ships no
    Linux build for it (a 32-bit ARM or x86 container)."""
    text = str(machine if machine is not None else _machine()).lower()
    if text in ("x86_64", "amd64"):
        return "x64"
    if text in ("aarch64", "arm64"):
        return "arm64"
    return None


def musl_libc():
    """True on a musl system (Alpine and friends).

    Release assets come in both libcs and they are not interchangeable: the
    musl build names /lib/ld-musl-<arch>.so.1 as its ELF loader, and a glibc
    host has no such file — the install then "succeeds" and every run dies with
    "not found" (exit 127), which is exactly how slskd's musl zip behaved in the
    Debian-based image.
    """
    if os.path.exists("/etc/alpine-release"):
        return True
    return any(os.path.exists(p) for p in (
        "/lib/ld-musl-x86_64.so.1", "/lib/ld-musl-aarch64.so.1",
        "/lib/ld-musl-arm.so.1", "/lib/ld-musl-i386.so.1"))


def _linux_pattern(key, machine=None):
    """Asset pattern of *key*'s native Linux build on this machine, or None when
    upstream ships none for it.

    A tool published for one libc only (oxipng's static musl tarball, which runs
    on both) has a single pattern per architecture; where both variants exist the
    host's libc is tried first and the other one is the fallback, so a platform
    that cannot be identified still gets a build rather than a refusal.
    """
    spec = LINUX_BINARIES.get(key)
    if not spec:
        return None
    arch = linux_arch(machine)
    if not arch:
        return None
    names = ([f"{arch}-musl", arch] if musl_libc() else [arch, f"{arch}-musl"])
    for name in names:
        pattern = spec["patterns"].get(name)
        if pattern:
            return pattern
    return None


def runner_missing(key, platform=None) -> str:
    """The interpreter package *key* needs and this machine lacks, else "".

    A Windows build run through mono, or a phar a PHP runtime executes: without
    the interpreter there is nothing installable, and naming its package is the
    whole help the row can give.
    """
    if _platform_of(platform) == "windows":
        return ""
    runner = LINUX_RUNNERS.get(key)
    if not runner:
        return ""
    return "" if shutil.which(runner[0]) else runner[1]


def install_kind(key, platform=None, machine=None):
    """How *key* installs on *platform*: `deps` | `system` | `unsupported`.

    `deps`        the installer fetches it into .dependencies (a pinned
                  Windows binary, a native Linux build or a pip package)
    `system`      the platform provides it as a distro package
    `unsupported` nothing to fetch: upstream ships no build for this platform,
                  or the interpreter its build needs is not here
    """
    plat = _platform_of(platform)
    if plat == "windows":
        return "deps"
    if key in PLATFORM_INDEPENDENT or key in PIP_PACKAGES:
        fetchable = True
    elif plat == "linux" and key in LINUX_BINARIES:
        fetchable = bool(_linux_pattern(key, machine))
    else:
        fetchable = False
    if fetchable:
        return "unsupported" if runner_missing(key, platform=platform) else "deps"
    if plat == "linux" and key in LINUX_PACKAGES:
        return "system" if LINUX_PACKAGES[key] else "unsupported"
    return "unsupported"


def installable(key, platform=None, machine=None):
    """Whether this platform can install *key* into .dependencies."""
    return install_kind(key, platform=platform, machine=machine) == "deps"


def installable_keys(platform=None, machine=None):
    """Every tool the installer can fetch here, in DISPLAY_NAMES order."""
    return [key for key in DISPLAY_NAMES
            if installable(key, platform=platform, machine=machine)]


def install_problem(key, platform=None, machine=None):
    """Why this platform cannot install *key*, or None when it can.

    The Dependencies row shows this next to a missing tool that has no Install
    button, and install_dependency() raises it when one is asked for anyway -
    the same sentence from one place.
    """
    kind = install_kind(key, platform=platform, machine=machine)
    if kind == "deps":
        return None
    display = DISPLAY_NAMES.get(key, key)
    plat = _platform_of(platform)
    pkg = LINUX_PACKAGES.get(key)
    missing_runner = runner_missing(key, platform=platform)
    if missing_runner:
        return (f"{display} needs the {LINUX_RUNNERS[key][0]} runtime on this "
                f"platform - install it with your package manager "
                f"(Debian/Ubuntu: apt-get install {missing_runner})")
    if kind == "system" and pkg:
        return (f"{display} is a system package on this platform - install it "
                f"with your package manager (Debian/Ubuntu: apt-get install "
                f"{pkg}); the Docker image already ships it.")
    if plat == "linux" and key in LINUX_BINARIES:
        return (f"{display} publishes no Linux build for this machine's "
                f"architecture - install it with your package manager.")
    if plat == "linux" and key in LINUX_PACKAGES:
        return (f"{display} is a Windows binary only and has no Linux build - "
                f"it is unsupported on this platform.")
    return (f"{display} has no build this app can install on this platform - "
            f"install it with your system package manager.")


def system_upgrade_command(key, platform=None):
    """The exact command that upgrades *key*'s distro package, or None.

    A tool the package manager owns can still be BEHIND, and the row has to say
    so (see dependency_rows). The most this app can then do is name the command
    that closes the gap, which is the one thing the Dependencies action column
    offers for that row: the user copies it.

    LINUX_PACKAGES names Debian packages, so the command is apt's, and it is the
    --only-upgrade form of the plain install install_problem() names — a row
    that already has the tool must not read as "install it". Never executed:
    this app does not run package managers, and certainly not unattended.
    """
    if _platform_of(platform) != "linux":
        return None
    pkg = LINUX_PACKAGES.get(key)
    if not pkg:
        return None
    return f"apt-get install --only-upgrade {pkg}"


def launcher(key, platform=None):
    """`(name, program, runner)` when an install of *key* is a Windows build
    this platform runs through an interpreter, else None."""
    spec = LINUX_BINARIES.get(key) or {}
    if _platform_of(platform) == "windows" or not spec.get("launcher"):
        return None
    return (*spec["launcher"], spec["runner"])


def run_name(key, platform=None):
    """The file to EXECUTE inside *key*'s install folder.

    The first marker, except where the install writes a launcher beside a
    Windows build (cuetools): that launcher is what callers run, so it is what
    detection has to point at.
    """
    spec = LINUX_BINARIES.get(key) or {}
    if _platform_of(platform) != "windows" and spec.get("launcher"):
        return spec["launcher"][0]
    return (spec.get("markers") or MARKER_EXES[key])[0]


def markers(key, platform=None):
    """The files a finished install of *key* must carry here."""
    if _platform_of(platform) != "windows":
        spec = LINUX_BINARIES.get(key)
        if spec:
            return spec["markers"]
    return MARKER_EXES[key]


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

# PHP for Windows (needed for Logchecker phar) — not on GitHub, direct from
# windows.php.net. The pinned build is the archived 8.1.28 zip; anything newer
# is named by the release index below.
PHP_ZIP_URL = (
    "https://windows.php.net/downloads/releases/archives/php-8.1.28-nts-Win32-vs16-x64.zip"
)

# windows.php.net's own release index: one JSON object per released SERIES
# ("8.1": {"version": "8.1.34", "nts-vs16-x64": {"zip": {"path": …}}, …}). It is
# the only place that knows which Windows builds exist — php publishes no
# GitHub releases — and its `path` is upstream's own name for the zip. The
# request 302s to downloads.php.net/~windows/, which urllib follows.
PHP_RELEASES_URL = "https://windows.php.net/downloads/releases/releases.json"
# The one build variant this app installs: 64-bit, non-thread-safe, VS16. VS16
# is deliberate — those zips run on any supported Windows, while the vs17 builds
# (PHP 8.4 and 8.5 publish no vs16) need the VC++ 2022 redistributable on the
# host, so offering them as an update would offer a php that cannot start.
PHP_VARIANT = "nts-vs16-x64"
_PHP_RELEASE_URL = "https://windows.php.net/downloads/releases/{name}"
_PHP_ARCHIVE_URL = "https://windows.php.net/downloads/releases/archives/{name}"

_HEADERS = {
    "User-Agent": "la-musica/2.1",
    "Accept": "application/vnd.github+json",
}

# The same client for the non-GitHub probes (PyPI, windows.php.net), which do
# not speak GitHub's media type.
_JSON_HEADERS = {"User-Agent": _HEADERS["User-Agent"]}

_release_cache = {}


# ----------------------------------------------------------------------
# GitHub API
# ----------------------------------------------------------------------
def _api_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or _HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_latest_release(key, upstream=False):
    """The release `key` resolves to, cached per session.

    `upstream=False` (the default) is the PINNED release: tools are pinned to
    exact versions (see PINNED) rather than "latest", so a FIRST install is
    reproducible. Fetches the specific release tag from GitHub.

    `upstream=True` asks the repo for its newest release instead. That is what
    an already-installed tool installs, because the table advertises an update
    the moment upstream moves past the pin — and an install that fetched the
    pin anyway reported success while changing nothing, so the button looked
    broken (see install_dependency). A tool with no GitHub repo (php) keeps the
    pin, which is the only release it has.

    Rolling-release repos (ffmpeg autobuilds) delete old tags, so a 404 on
    the pinned tag falls back to the repo's current latest release.
    """
    cache_key = (key, bool(upstream) and key in REPOS)
    if cache_key not in _release_cache:
        pin = PINNED[key]
        try:
            if upstream and key in REPOS:
                data = _api_json(
                    f"https://api.github.com/repos/{REPOS[key]}/releases/latest"
                )
                version = str(data.get("tag_name") or pin["version"])
            else:
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
        # A tag ("v10.2.1") and a version ("10.2.1") name the same release, and
        # the rest of the app compares them through _version_label; store the
        # comparable form so a folder name or a row never reads "vv10.2.1".
        version = _version_label(version) or version
        urls = {a.get("name", ""): a.get("browser_download_url", "")
                for a in data.get("assets", [])}
        _release_cache[cache_key] = {
            "version": version,
            "assets": list(urls),
            "urls": urls,
        }
    return _release_cache[cache_key]


def _release(key, upstream=False):
    """`get_latest_release` for the path an install is on.

    A one-line indirection so the default (pinned) path stays exactly the call
    the installer's tests already stub: `get_latest_release(key)`.
    """
    if upstream:
        return get_latest_release(key, upstream=True)
    return get_latest_release(key)


def latest_versions():
    """{tool key: reviewed target version} for every tracked tool.

    Keyed by DISPLAY_NAMES - the exact set `server.main` /api/dependencies
    serves - so every row the UI can show has an entry. No network needed.

    Windows: the pinned release of every tool. Linux: the pinned release for
    what the installer fetches there (the native builds in LINUX_BINARIES, the
    pip packages), the distro package for the rest (LINUX_PACKAGES), and None
    for a tool with no build at all - the UI shows "unknown" rather than a
    version nobody can install. install_kind() decides which of those applies.

    This is the REVIEWED release, and it is what a first install on a machine
    that cannot reach the upstream check fetches; a tool that is already
    installed takes the newest release its publisher has instead (see
    install_dependency), which is what the row's `upstream_version` names.
    """
    out = {}
    for key in DISPLAY_NAMES:
        kind = install_kind(key)
        if kind == "deps":
            out[key] = PINNED.get(key, {}).get("version")
        elif kind == "system":
            out[key] = f"apt: {LINUX_PACKAGES[key]}"
        else:
            out[key] = None
    return out


# ----------------------------------------------------------------------
# Live upstream versions
# ----------------------------------------------------------------------
# How long an upstream answer stays usable. GitHub's anonymous API allows 60
# requests/hour; a full pass is one request per probed tool (15: the 12 GitHub
# repos every 30 minutes, i.e. 24/hour, plus PyPI's two and windows.php.net's
# one, which are not GitHub's to rate-limit), so a page load never hammers the
# API and the GitHub budget keeps its headroom.
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


def same_version(a, b):
    """True when two version strings name the same release.

    A pin ("10.2.0"), a GitHub tag ("v10.2.0") and a detected version are three
    spellings of one release; `_version_label` is the normal form they compare
    through. An unreadable value is never "the same" as anything.
    """
    la, lb = _version_label(a), _version_label(b)
    return bool(la) and la == lb


def newer_version(candidate, current):
    """True when *candidate* is strictly newer than *current*.

    "Behind" is an ORDER, not a difference: a tool installed at 10.2.1 while the
    pin says 10.2.0 differs from the pin but is not missing an update, and the
    old `!=` test marked exactly that row "Update" forever — pointing at an
    older release the installer would then fetch, changing nothing. Same-version
    spellings ("10.2" vs "10.2.0") compare equal by padding the shorter one.

    An unreadable label never counts as newer: a rolling build must not produce
    an update the installer cannot perform.
    """
    a, b = _version_label(candidate), _version_label(current)
    if not a or not b:
        return False

    def parts(v):
        return [int(p) if p.isdigit() else 0 for p in v.split(".")]

    pa, pb = parts(a), parts(b)
    n = max(len(pa), len(pb))
    return pa + [0] * (n - len(pa)) > pb + [0] * (n - len(pb))


def _upstream_source(key):
    """Which publisher answers *key*'s newest version, or None.

    GitHub for the repos in REPOS, PyPI for the packages GitHub does not carry,
    and windows.php.net for php. A None here is a tool with no upstream check
    at all: its row keeps the pinned target and says so (dependency_rows).
    """
    if key in REPOS:
        return "GitHub"
    if key in PYPI_PROBES:
        return "PyPI"
    if key == "php":
        return "windows.php.net"
    return None


def _upstream_keys():
    """Tools whose newest release a probe can answer (see _upstream_source)."""
    return [key for key in DISPLAY_NAMES if _upstream_source(key)]


_php_build_cache = {"at": 0.0, "version": None, "url": None}
_php_build_lock = threading.Lock()


def php_upstream_build(force=False):
    """(version, url) of the newest PHP for Windows this app can install.

    windows.php.net's release index is the only source: php has no GitHub
    releases, and upstream's `path` is the name of the file rather than one
    this app guesses. Only the x64 non-thread-safe VS16 build is eligible (the
    layout the pin uses) — 8.4 and 8.5 publish vs17 zips only, and offering one
    of those as an update would offer a php that will not start on a host
    without the VC++ 2022 redistributable. Cached for the upstream TTL so the
    row's check and the install it leads to share one fetch; (None, None) on
    any failure, which every caller reads as "unknown".
    """
    now = time.time()
    with _php_build_lock:
        cached = dict(_php_build_cache)
    if not force and cached["version"] and now - cached["at"] < UPSTREAM_TTL_S:
        return cached["version"], cached["url"]
    version = url = None
    try:
        data = _api_json(PHP_RELEASES_URL, headers=_JSON_HEADERS)
        for entry in (data or {}).values():
            if not isinstance(entry, dict):
                continue
            zip_path = (((entry.get(PHP_VARIANT) or {}).get("zip")) or {}).get("path")
            label = _version_label(entry.get("version"))
            if not label or not zip_path:
                continue
            if version is None or newer_version(label, version):
                version = label
                url = _PHP_RELEASE_URL.format(name=zip_path)
    except Exception:
        version = url = None
    with _php_build_lock:
        _php_build_cache.update(at=now, version=version, url=url)
    return version, url


def php_zip_url(version):
    """Download URL of the x64 NTS VS16 zip of PHP *version*.

    The pin is the archived zip the app has always fetched. Anything else takes
    the release index's own path when it knows that version (the file upstream
    itself publishes), and falls back to the archives' naming convention — they
    keep every released patch, so a version the index has moved past is still
    fetchable.
    """
    label = _version_label(version)
    if not label:
        return PHP_ZIP_URL
    if same_version(label, PINNED["php"]["version"]):
        return PHP_ZIP_URL
    known_version, known_url = php_upstream_build()
    if known_url and same_version(known_version, label):
        return known_url
    return _PHP_ARCHIVE_URL.format(name=f"php-{label}-nts-Win32-vs16-x64.zip")


def _fetch_upstream(key):
    """Newest release of *key* as a version label, or None when it has no tag.

    The same GitHub call as get_latest_release() - which is asked for the
    PINNED tag instead - so the API handling (headers, JSON, 30 s timeout)
    stays in one place.
    """
    data = _api_json(
        f"https://api.github.com/repos/{REPOS[key]}/releases/latest")
    return _version_label(data.get("tag_name"))


def _probe_version(key):
    """Newest version of *key* from whichever publisher releases it."""
    source = _upstream_source(key)
    if source == "PyPI":
        data = _api_json(f"https://pypi.org/pypi/{PIP_PACKAGES[key]}/json",
                         headers=_JSON_HEADERS)
        return _version_label((data.get("info") or {}).get("version"))
    if source == "windows.php.net":
        return php_upstream_build()[0]
    return _fetch_upstream(key)


def _known_upstream_version(key):
    """Newest version known for *key*: the check cache, else the probe itself.

    The cache is what the table's background pass fills (30-minute TTL, one
    pass per page load). An INSTALL is a deliberate act, so a tool the cache
    has no answer for yet is probed here and now instead of installing the pin
    over a release upstream has long replaced — the press that changed nothing
    this whole issue is about. Any failure is "unknown" and the caller falls
    back to the reviewed pin, which an offline machine can still fetch.
    """
    entry = _upstream_cache.get(key)
    if entry and entry.get("version"):
        return entry["version"]
    if not _upstream_source(key):
        return None
    try:
        version = _probe_version(key)
    except Exception:
        return None
    if not version:
        return None
    with _upstream_lock:
        _upstream_cache[key] = {"version": version, "checked_at": time.time(),
                                "error": None}
    return version


def _install_target(key):
    """The version an install of *key* should fetch: the newest release this
    app can see, else the reviewed pin (see _known_upstream_version)."""
    return _known_upstream_version(key) or PINNED[key]["version"]


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
            entry = {"version": _probe_version(key), "checked_at": time.time(),
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
    """{key: {"version", "checked_at", "error"}} for every probed tool.

    Never blocks a caller on the network: by default a stale (or `refresh=True`
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
      latest_version     the reviewed pinned target (see PINNED) — the version
                         a FIRST install on a machine that cannot reach the
                         upstream check fetches
      upstream_version   the newest release the tool's own publisher lists
                         (GitHub, PyPI, windows.php.net — see
                         _upstream_source), None while unknown

    `state` is derived from the LIVE upstream value: `ok` (installed ==
    upstream), `update` (upstream is NEWER than what is installed, whatever
    installs it — a distro row behind its package is still behind), `missing`
    (nothing installed), `error` (that tool's check failed). Rows for a tool
    with no upstream probe at all fall back to the pinned pair, which is the
    only answer available for them.

    `state` deliberately does NOT say what can be done about it, because those
    are two different facts and one field could only ever carry one of them: a
    green Ready beside an amber Available is what the merge produced. The other
    two facts are their own fields —
      update_available  is a newer upstream release than what is installed (a)
      install_kind      what this host can do: `deps` = fetch into
                        .dependencies, `system` = the OS package manager owns
                        it, `unsupported` = nothing this app can fetch (b)
      action            what the row's action column offers — `install`,
                        `update` (both fetch a download), `upgrade` (copy
                        upgrade_command), `none` (nothing to do here) (c)
    — and `upgrade_command` carries the exact command for `action == upgrade`.
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
        # A failed upstream check must NOT mark a healthy install as broken:
        # GitHub rate-limits unauthenticated callers, and a wall of red for a
        # transient 403 is worse than no check at all. The upstream cell and
        # the note carry the failure; the status falls back to the pinned
        # pair, which is the one answer always available.
        kind = install_kind(key)
        update_available = bool(
            uv and have and newer_version(uv, have))
        if not (iv or info):
            state = "missing"
        elif uv:
            state = "update" if update_available else "ok"
        elif target and have and newer_version(target, have):
            state = "update"
        else:
            state = "ok"
        # What the ACTION column offers, from the two facts above: `state` says
        # whether the row is behind, `install_kind` says whether a download can
        # close the gap. A distro tool behind upstream reads `update` like any
        # other row and offers the package manager's command instead of a
        # button that cannot download anything — the two used to be conflated,
        # which is how a row kept a green Ready with an amber Available beside
        # it and no way to clear either.
        if state == "update":
            action = {"deps": "update", "system": "upgrade"}.get(kind, "none")
        elif state == "missing" and kind == "deps":
            action = "install"
        else:
            action = "none"
        upgrade_command = (
            system_upgrade_command(key) if action == "upgrade" else None)
        if err:
            note = f"upstream check failed: {err} — status is against the pinned target"
        elif action == "upgrade":
            note = (f"the system package provides {have}; upstream ships {uv} — "
                    f"upgrade it with your package manager ({upgrade_command})")
        elif update_available and action == "none":
            # Behind upstream with nothing here that can fetch it (a
            # Windows-only tool on this platform, or a build for another
            # architecture): install_note already names the package manager or
            # the missing build, so this only has to say the row really is
            # behind. A row a download CAN move keeps the versions in its
            # upstream column and no note at all.
            note = (f"installed {have}; upstream ships {uv} — nothing this host "
                    f"can install to close that gap")
        elif _upstream_source(key) is None:
            note = ("no upstream check for this tool — only the pinned target "
                    "is installable")
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
            # Whether an Install press can do anything HERE, and the reason
            # when it cannot (see install_kind/install_problem). The page shows
            # that reason instead of counting the row as a missing tool, which
            # is what made a Windows-only tool on Linux - or a distro package
            # the image already ships - read as a broken Install button.
            "installable": installable(key),
            "install_note": install_problem(key),
            "install_kind": kind,
            # The action this row offers, and the exact command it copies (see
            # the docstring). Both are computed here so the page never has to
            # work out for itself what a row behind upstream can do.
            "action": action,
            "upgrade_command": upgrade_command,
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
    """{tool key: installed version} for currently detected tools only.

    A vendored pip package reports what its folder ACTUALLY holds
    (tools.python_pkg_version — pip's own `.dist-info`, falling back to the
    version in the folder name) rather than PINNED. Reporting the pin for every
    folder that merely existed made INSTALLED a copy of the target: an upstream
    release following the pin never looked like an update, and the installer's
    own "already installed" short-circuit was fed by the same number, so a pip
    tool could never move.

    Only a folder neither pip's metadata nor its own name can date — one no
    install of this app wrote — falls back to the pin, because reporting
    nothing at all would read as "missing" for a package that is right there.
    """
    tools = detect_all_tools()
    out = {key: info["version"] for key, info in tools.items()}
    for key in PIP_PACKAGES:
        if pip_package_path(key):
            out[key] = python_pkg_version(key) or PINNED[key]["version"]
    return out


def pip_package_path(key):
    """Folder of a vendored pip package (e.g. '.dependencies/librosa v0.11.0')
    when its top-level package dir is present, else None."""
    return python_pkg_path(key)


def _require_installable(key):
    """Refuse an install this platform cannot perform.

    Nothing else in the install path knows the platform: a Windows archive
    unpacks fine on Linux and passes the marker check, so a download used to
    "succeed" into a folder of unrunnable .exe files, and a tool this host can
    never install (its distro package, or a Windows-only one) read as a broken
    button. install_problem() supplies the one sentence every caller shows.
    """
    problem = install_problem(key)
    if problem:
        raise RuntimeError(problem)


def _asset_patterns(key, platform=None, machine=None):
    """Asset-name patterns to try for *key* on this platform, best first.

    Windows names come from ASSET_PATTERNS; a native Linux tool names the one
    asset its architecture is published as. A platform-independent tool has ONE
    list — the Logchecker phar is the same file everywhere — and asking the
    Linux table for it returned nothing at all (it is not in there, since
    nothing about it is platform-specific).
    """
    if key in PLATFORM_INDEPENDENT or _platform_of(platform) == "windows":
        return ASSET_PATTERNS.get(key, [])
    pattern = _linux_pattern(key, machine)
    return [pattern] if pattern else []


def pick_asset(key, upstream=False):
    """Return the exact pinned asset name for a tool, if it exists.

    `upstream=True` picks from the newest release instead, so the asset and the
    version it is installed as come from the same release (see
    get_latest_release). The pin itself is a Windows asset name, so it is only
    consulted there; on Linux the pattern decides, from the same release.
    """
    _require_installable(key)
    pin = PINNED.get(key) or {}
    if pin.get("asset") and _platform_of() == "windows":
        rel = _release(key, upstream)
        if pin["asset"] in rel["assets"]:
            return pin["asset"]
    return _pattern_asset(key, upstream=upstream)


def _pattern_asset(key, assets=None, upstream=False):
    """First release asset matching the platform patterns for a tool."""
    names = assets if assets is not None else _release(key, upstream)["assets"]
    for pattern in _asset_patterns(key):
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


def _archive_suffix(asset):
    """The archive extension of a release asset, compression chain included.

    `os.path.splitext` alone cuts "….tar.gz" down to ".gz", and a temp file
    named "*.gz" is not recognisable as a tarball to _extract_archive — it fell
    through to the "run it as an installer" branch and died with rc=126 on the
    first Linux tarball this installer ever fetched.
    """
    lower = asset.lower()
    for suffix in (".tar.gz", ".tar.xz", ".tar.bz2", ".tgz", ".tar",
                   ".zip", ".7z", ".exe", ".phar"):
        if lower.endswith(suffix):
            return suffix
    return os.path.splitext(asset)[1]


def _extract_archive(archive_path, dest_dir, log):
    """Extract zip / tar / 7z / NSIS installer into dest_dir."""
    lower = archive_path.lower()

    if lower.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar")):
        # Linux release assets are tarballs (oxipng) as often as zips. tarfile
        # restores each entry's mode, which is what makes the unpacked binary
        # executable; `filter="data"` (3.12+) keeps that while refusing entries
        # that would escape dest_dir or carry device nodes, the same guarantee
        # zipfile gives.
        with tarfile.open(archive_path) as tf:
            try:
                tf.extractall(dest_dir, filter="data")
            except TypeError:      # Python < 3.12: no extraction filters
                tf.extractall(dest_dir)
        return

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
    """Find the directory containing the tool's marker files here.

    The markers are the platform's (see markers()): a Linux install looks for
    the bare binary names, which is also what keeps a folder of unrunnable
    .exe files from being mistaken for an install.
    """
    wanted = markers(key)
    candidates = []
    for dirpath, _dirnames, filenames in os.walk(root):
        names = {f.lower() for f in filenames}
        if all(m.lower() in names for m in wanted):
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


def _write_launcher(dest_dir, name, program, runner):
    """Write *name* as a script that runs *program* under *runner*.

    CUETools ships Windows binaries, and its console tool runs under mono on
    Linux (measured: CUETools.ARCUE.exe prints its usage under mono on trixie).
    Everything downstream invokes ONE executable — `resolve_arcue_exe` returns a
    path, `run_tool([exe, cue])` runs it — so the launcher is where the runtime
    lives, rather than every caller learning about it.
    """
    path = os.path.join(dest_dir, name)
    program = os.path.basename(program)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("#!/bin/sh\n"
                 f"# Generated by la musica: {program} is a Windows build that\n"
                 f"# runs under {runner} on this platform.\n"
                 f'exec {runner} "$(dirname "$0")/{program}" "$@"\n')
    os.chmod(path, 0o755)


def _make_executable(dest_dir, marker_names):
    """Give the installed binaries the exec bit (POSIX).

    zipfile does not restore file modes, so a Linux binary unpacked from a
    .zip would land without it and every spawn - slskd is the one tool this
    app RUNS - would fail with EACCES. A no-op on Windows, where the bit does
    not exist; a tarball already carries its own.
    """
    if os.name == "nt":
        return
    for name in marker_names:
        path = os.path.join(dest_dir, name)
        try:
            os.chmod(path, os.stat(path).st_mode | 0o111)
        except OSError:
            pass


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


def _rename_install(dest_dir, prefix, version, log=print):
    """Rename an install folder to the version that is now inside it.

    The detector reads a tool's version off its FOLDER name (see
    mlo.tools._detect_tool), and an update installs into the folder the previous
    version lived in — so replacing the binaries of "oxipng v10.2.0" left a
    folder that still reported 10.2.0, the row kept saying "Update", and the
    press looked like it had done nothing at all. Renaming keeps the single
    folder that _existing_install exists to preserve AND makes the version the
    app reports the version that is actually installed.

    A `vlatest` rolling folder has no version to carry and is left alone. So is
    a folder that cannot be renamed (a file held open on Windows): the tool
    still works, and a stale label is the lesser problem. Returns the folder to
    use from here on.
    """
    current = os.path.basename(dest_dir)
    want = f"{prefix} v{version}"
    if current == want:
        return dest_dir
    if not re.match(rf"^{re.escape(prefix)}\s+v\d", current, re.IGNORECASE):
        return dest_dir          # `vlatest`, or a folder this app did not name
    target = os.path.join(os.path.dirname(dest_dir), want)
    if os.path.exists(target):
        return dest_dir
    try:
        os.rename(dest_dir, target)
    except OSError as e:
        log(f"  could not rename {current} to {want}: {e}")
        return dest_dir
    return target


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

    The version installed is the newest release this app can see (PyPI for
    beets/librosa, GitHub for yt-dlp — see _install_target), and the reviewed
    pin only when nothing can be seen. The old code installed the pin and
    returned "already installed" whenever a folder existed at all, so a pip
    tool could never update: the row said Update, the press said nothing to do,
    and the version never moved.
    """
    name = PIP_PACKAGES[key]
    display = DISPLAY_NAMES[key]
    installed = python_pkg_version(key)
    target = _install_target(key)
    if installed and not newer_version(target, installed):
        # Nothing to do: the folder already holds the target, or one NEWER than
        # anything upstream publishes — an update must never walk a copy back.
        log(f"{display} is already at v{installed} — nothing to install")
        return installed
    dest_dir = os.path.join(DEPS_DIR, f"{key} v{target}")
    log(f"Downloading {display} v{target} (pip) …")
    cmd = [
        _pip_python(), "-m", "pip", "install",
        # --upgrade, deliberately: without it pip answers "Requirement already
        # satisfied" for a version it finds ANYWHERE it looks — the running
        # interpreter's site-packages, or a folder from an interrupted install
        # — and writes nothing into the target, so the install "succeeds" with
        # no package in the folder it was told to fill.
        "--upgrade",
        "--target", dest_dir, "--no-cache-dir",
        "--progress-bar", "off", "--disable-pip-version-check",
        f"{name}=={target}",
    ]
    proc = run_tool(cmd, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=1800)
    top = PIP_IMPORT_NAMES.get(key, key)
    # The folder this install wrote, not "some folder for this package": an
    # older install still on disk would answer a lookup the wrong way round.
    landed = os.path.isfile(os.path.join(dest_dir, top, "__init__.py"))
    if proc.returncode != 0 or not landed:
        shutil.rmtree(dest_dir, ignore_errors=True)
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(
            f"pip install failed for {display}: {tail[-1] if tail else 'unknown error'}")
    _remove_older_versions(key, os.path.basename(dest_dir))
    log(f"Installed {display} v{target} -> {dest_dir}")
    return target


def _install_php(log=print, progress=None):
    """Download PHP for Windows (needed for Logchecker phar).

    The newest build windows.php.net publishes in the x64 NTS VS16 layout the
    pin uses (see php_upstream_build), the pinned 8.1.28 archive when that
    index cannot be read — and nothing at all when the copy on disk is already
    the newest, or newer than it. Same rule as every other tool: an existing
    copy takes the newest release, a first install falls back to the pin.
    """
    display = DISPLAY_NAMES["php"]
    installed = installed_versions().get("php")
    version = _install_target("php")
    if installed and not newer_version(version, installed):
        log(f"{display} is already at v{installed} — nothing to install")
        return installed
    zip_url = php_zip_url(version)
    log(f"Downloading {display} v{version} (php zip) …")
    dest_dir = os.path.join(DEPS_DIR, f"php v{version}")
    fd, tmp_zip = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    workdir = tempfile.mkdtemp(prefix="mlo_php_")
    try:
        _download(zip_url, tmp_zip, progress)
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


# One install at a time PER TOOL. Every row can now be installed on its own
# (the page's per-row button), so two presses of one row — or a press landing
# while the auto-update pass is running the same tool — would unpack two
# downloads into one folder and prune each other's files. A second press is
# REFUSED rather than queued: a queue's second entry would re-download what the
# first just installed, and the honest answer to "it is being installed right
# now" is to say so.
_install_locks = {}
_install_locks_guard = threading.Lock()


def install_lock(key):
    """The lock guarding installs of *key* (created on first use)."""
    with _install_locks_guard:
        lock = _install_locks.get(key)
        if lock is None:
            lock = _install_locks[key] = threading.Lock()
        return lock


def installing(key):
    """True while an install of *key* is in flight in this process."""
    with _install_locks_guard:
        lock = _install_locks.get(key)
    return bool(lock is not None and lock.locked())


def install_dependency(key, log=print, progress=None):
    """Download and install the latest release of a tool.

    Returns the installed version string. Raises on any failure.
    """
    _require_installable(key)
    lock = install_lock(key)
    if not lock.acquire(blocking=False):
        raise RuntimeError(
            f"{DISPLAY_NAMES.get(key, key)} is already being installed — "
            f"wait for that install to finish")
    try:
        return _install_one(key, log=log, progress=progress)
    finally:
        lock.release()


def _install_one(key, log=print, progress=None):
    """The install itself, with the per-tool lock already held."""
    if key == "php":
        return _install_php(log=log, progress=progress)
    # Vendored pip packages — plus the tools whose Linux install IS the pip
    # package (PIP_ON_LINUX): on Windows those take the pinned .exe below.
    # Both carry their own target/version handling (see _install_target).
    if key in PIP_PACKAGES and (key not in PIP_ON_LINUX
                                or host_platform() != "windows"):
        return _install_pip_package(key, log=log, progress=progress)

    # An installed copy takes the NEWEST release; only a tool that is not there
    # yet takes the reviewed pin.
    #
    # The table calls a row "Update" the moment GitHub publishes past the pin,
    # and that row has an Install button. Fetching the pin for it downloaded the
    # version already on disk: the request returned 200, the version column did
    # not move, and the chip still said Update - i.e. the button looked broken,
    # which is exactly how it was reported. Upstream is tried first and the pin
    # stays the fallback, so an unreachable GitHub, a repo with no release or a
    # release whose assets do not match our patterns all still install
    # something known-good rather than failing.
    prefix = INSTALL_PREFIX[key]
    wanted = markers(key)
    installed = installed_versions().get(key)
    # "Already there" is what the DETECTOR says, PATH included — not only a
    # .dependencies folder. A copy the user installed with scoop/apt is a copy
    # the table calls Ready-or-Update, and an Install press on such a row used
    # to take the pin (no folder of ours = "first install"): the press
    # re-downloaded the version already on PATH, reported changed: false, and
    # the Update chip survived. A PATH copy NEWER than the pin was worse — it
    # was "updated" downwards into .dependencies.
    upstream = bool(installed or _existing_install(prefix, wanted))
    rel = _release(key, upstream)
    version = rel["version"]

    # An install exists to replace a copy that is behind. There is nothing
    # behind when the installed version already IS the target (without this,
    # "Install / update all" re-fetched slskd's 118 MB on every press), and
    # nothing to gain when the copy is NEWER than the target — a hand-pulled
    # release, or upstream yanking one — where installing would be a downgrade.
    if upstream and installed:
        if same_version(version, installed) or newer_version(installed, version):
            log(f"{DISPLAY_NAMES[key]} is already at v{installed} — "
                f"nothing to install")
            return installed

    asset = pick_asset(key, upstream)

    if not asset and upstream:
        log(f"{DISPLAY_NAMES[key]}: the newest release carries no asset this app "
            f"installs — using the pinned release instead")
        upstream = False
        rel = _release(key)
        version = rel["version"]
        asset = pick_asset(key)
        # The fallback is the pin, so it can be what is installed already.
        if installed and not newer_version(version, installed):
            log(f"{DISPLAY_NAMES[key]} is already at v{installed} — "
                f"nothing to install")
            return installed

    if not asset:
        raise RuntimeError(
            f"No suitable {host_platform()} asset in the latest {key} release")

    display = DISPLAY_NAMES[key]
    existing = _existing_install(prefix, wanted)
    dest_dir = os.path.join(DEPS_DIR, existing or f"{prefix} v{version}")

    tmp_archived_fd, tmp_archived = tempfile.mkstemp(
        suffix=_archive_suffix(asset))
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
            suffix = _archive_suffix(asset)
            if suffix != _archive_suffix(os.path.basename(tmp_archived)):
                os.remove(tmp_archived)
                fd, tmp_archived = tempfile.mkstemp(suffix=suffix)
                os.close(fd)
            log(f"Pinned asset is gone (404) — falling back to {asset}")
            _download(rel["urls"][asset], tmp_archived, progress)

        if key in SINGLE_EXE_TOOLS:
            # The release asset is the tool itself - no extraction step.
            os.makedirs(dest_dir, exist_ok=True)
            shutil.copy2(tmp_archived, os.path.join(dest_dir, wanted[0]))
        else:
            log(f"Extracting {asset} …")
            _extract_archive(tmp_archived, workdir, log)

            src = _locate_binaries(workdir, key)
            if src is None:
                raise RuntimeError(
                    f"Could not find {' + '.join(wanted)} inside the archive"
                )

            os.makedirs(dest_dir, exist_ok=True)
            for fname in os.listdir(src):
                s = os.path.join(src, fname)
                if os.path.isdir(s):
                    # A native Linux build carries a runtime tree beside its
                    # binary (slskd ships wwwroot/ and etc/ and refuses to boot
                    # without them), so the whole layout has to come along.
                    shutil.copytree(s, os.path.join(dest_dir, fname),
                                    dirs_exist_ok=True)
                    continue
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
        missing = [m for m in wanted if m.lower() not in names]
        if missing:
            raise RuntimeError(f"Installed folder is missing: {', '.join(missing)}")
        wrapped = launcher(key)
        if wrapped:
            _write_launcher(dest_dir, *wrapped)
        _make_executable(dest_dir, list(wanted) + ([wrapped[0]] if wrapped else []))

        # Before the pruner runs, and before anything reports a version: the
        # folder has to carry the version that is now in it.
        dest_dir = _rename_install(dest_dir, prefix, version, log)
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

# One install pass per 6 h. A pass only touches rows whose state is `missing`
# or `update`, and `update` now means a strictly newer upstream release exists,
# so a second pass with nothing published downloads nothing.
# ponytail: fixed interval, make it a config key if it ever needs tuning.
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
        # A tool this platform cannot install is not a failure to retry every
        # pass: its row says why (Windows-only, or a distro package), and the
        # old loop logged the same refusal every six hours.
        if not row.get("installable", True):
            continue
        # No "installed == pin, so skip" guard here any more: it existed
        # because install_dependency fetched the pin regardless, so a tool whose
        # upstream had moved on would have been re-downloaded every pass for no
        # change. Installs now take the newest release (and `update` means
        # STRICTLY newer, see newer_version), so an update pass installs once
        # and the row settles at `ok` — the guard would only hide the update
        # this loop exists to perform.
        try:
            version = install_dependency(row["key"], log=lambda m: None)
            log(f"[deps] auto-update: {row['name']} was {row['state']}, "
                f"installed {version or 'the pinned release'}")
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
