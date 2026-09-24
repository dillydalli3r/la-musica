#!/usr/bin/env python3
"""Platform guards: where a dependency install is allowed to go.

What an install can do is decided in ONE place — fetchdeps.install_kind() — and
all three answers are checked here on both platforms, with the download and
GitHub helpers stubbed so nothing touches the network:

  * `deps`        the installer fetches it: the pinned Windows asset on
                  Windows, a native Linux build, a pip package or a source
                  archive elsewhere;
  * `system`      a distro package (flac, ffmpeg, …): the row reports it and has
                  no Install button, and "Install all" skips it;
  * `unsupported` a Windows-only tool (AudioAuditor, CUETools, Logchecker+php):
                  refused with a reason, never a folder of unrunnable .exe
                  files reported as "ok".

The regression this file exists for: on Linux, "Install / update all" attempted
every one of the sixteen tools and failed on twelve — the distro-provided ones
and the Windows-only ones — while oxipng and slskd, which upstream DOES publish
Linux builds for, were refused for ever and stayed "missing" behind an Install
button that could not work.

Both branches are simulated rather than read off the host (see
simulated_platform).
"""
import os
import re
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo import fetchdeps  # noqa: E402

# Read before any simulated_platform() block: the exec-bit assertions only mean
# something on a host that has the bit.
HOST_WINDOWS = os.name == "nt"

FAILURES = []


def check(label, cond):
    if not cond:
        FAILURES.append(label)
        print(f"FAIL {label}")


# --------------------------------------------------------------------------- #
# Installer refuses Windows-only assets off Windows
# --------------------------------------------------------------------------- #
def boom(*a, **k):
    raise AssertionError("installer reached the network for a refused tool")


fetchdeps._download = boom
fetchdeps._api_json = boom

APT_KEYS = {k: v for k, v in fetchdeps.LINUX_PACKAGES.items() if v}
# Tools that install on Linux through an interpreter, with the runtime they
# need on PATH (fetchdeps.LINUX_RUNNERS) — the interpreter's package is what the
# row names when it is missing.
RUNNER_KEYS = dict(fetchdeps.LINUX_RUNNERS)
# Tools upstream publishes a native Linux build for: the *fetchable* half of a
# Linux install, and the reason a container is no longer stuck without them.
LINUX_NATIVE = tuple(fetchdeps.LINUX_BINARIES)
# ...grouped by the architectures their assets cover. All but rsgain publish for
# both of the 64-bit architectures installations run on; rsgain ships an x86-64
# build only, so an ARM host has no rsgain download and takes the distro package
# instead — which is why LINUX_PACKAGES keeps an entry for a tool that IS
# installable here. Their per-architecture answers are checked below.
LINUX_NATIVE_ARM_TOO = tuple(k for k, s in fetchdeps.LINUX_BINARIES.items()
                             if "arm64" in s["patterns"])
LINUX_NATIVE_X64_ONLY = tuple(k for k, s in fetchdeps.LINUX_BINARIES.items()
                              if "arm64" not in s["patterns"])
# The native entries upstream ALSO packages, i.e. the overlap between the two
# tables: no build for the 32-bit architectures, so the package covers those.
LINUX_PACKAGED_NATIVE = tuple(k for k in LINUX_NATIVE if k in fetchdeps.LINUX_PACKAGES)
# The distro rows with no build at all: no asset upstream ships for ANY
# architecture this app runs on. A key can be in both tables (rsgain,
# chromaprint), and then LINUX_PACKAGES is the fallback for the architectures
# LINUX_BINARIES has no pattern for.
APT_ONLY = {k: v for k, v in APT_KEYS.items() if k not in fetchdeps.LINUX_BINARIES}
PLATFORM_FREE = ("librosa", "beets", "yt-dlp", "logchecker")


class simulated_platform:
    """Pin the platform the installer reads instead of the host's.

    host_platform() reads os.name and sys.platform, so both are set — the Linux
    branch has to be enterable on a Windows dev box and the Windows branch on
    Linux CI — and both are restored in __exit__, i.e. unconditionally.
    """

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.real = (os.name, sys.platform)
        if self.name == "nt":
            os.name, sys.platform = "nt", "win32"
        else:
            os.name, sys.platform = "posix", "linux"
        return self

    def __exit__(self, *exc):
        os.name, sys.platform = self.real
        return False


def install_fails(key):
    """The RuntimeError install_dependency() raises, or None when it got past."""
    try:
        fetchdeps.install_dependency(key, log=lambda m: None)
    except RuntimeError as e:
        return e
    return None


class runners:
    """Say whether this machine has the interpreters (mono, php) a block needs.

    Two things are pinned: `fetchdeps.LINUX_RUNNERS` stays the real table, and
    the LOOKUP is stubbed — `shutil.which` follows the simulated platform
    (`os.name` is patched inside these blocks), and what these blocks are
    testing is the decision install_kind() makes, not the filesystem.
    """

    class _Which:
        def __init__(self, present):
            self._present = present

        def which(self, name):
            return f"/usr/bin/{name}" if self._present else None

    def __init__(self, present: bool):
        self.present = present

    def __enter__(self):
        self.real = fetchdeps.shutil
        fetchdeps.shutil = self._Which(self.present)
        return self

    def __exit__(self, *exc):
        fetchdeps.shutil = self.real
        return False


# --------------------------------------------------------------------------- #
# The platform table: three answers, both platforms, no globals patched
# --------------------------------------------------------------------------- #
# install_kind() is pure, so the whole truth table is checked here whatever the
# suite runs on.
for key in fetchdeps.DISPLAY_NAMES:
    check(f"Windows installs {key} itself",
          fetchdeps.install_kind(key, platform="windows") == "deps")

with runners(True):
    # EVERY tool the app knows, on Linux: upstream's native build (oxipng,
    # slskd, AudioAuditor, CUETools through mono, and the rsgain/fpcalc pair),
    # the pip/source/phar set, or the distro package. There is no "cannot
    # install this here" row left for a 64-bit host.
    for key in LINUX_NATIVE_ARM_TOO + PLATFORM_FREE:
        check(f"Linux installs {key} from its own release",
              fetchdeps.install_kind(key, platform="linux", machine="x86_64") == "deps")
        check(f"...on arm64 too",
              fetchdeps.install_kind(key, platform="linux", machine="aarch64") == "deps")
        check(f"...and offers it no refusal",
              fetchdeps.install_problem(key, platform="linux", machine="x86_64") is None)
    # rsgain is the exception: upstream ships an x86-64 Linux build and no ARM
    # one, so an ARM host must fall back to the distro package rather than
    # reading a download that does not exist.
    for key in LINUX_NATIVE_X64_ONLY:
        check(f"Linux/x86-64 installs {key} from its own release",
              fetchdeps.install_kind(key, platform="linux", machine="x86_64") == "deps"
              and fetchdeps.install_problem(key, platform="linux", machine="x86_64") is None)
        check(f"Linux/arm64 falls back to the {key} distro package",
              fetchdeps.install_kind(key, platform="linux", machine="aarch64") == "system")

# The grouping above has to cover the whole table, or a tool added to
# LINUX_BINARIES silently skips every check in this file.
check("the native table is exactly the arm-and-x64 and x64-only entries",
      set(LINUX_NATIVE) == set(LINUX_NATIVE_ARM_TOO) | set(LINUX_NATIVE_X64_ONLY))

for key in APT_ONLY:
    check(f"Linux reports {key} as the distro package",
          fetchdeps.install_kind(key, platform="linux", machine="x86_64") == "system")

# The interpreter is what the tool runs on: without it there is nothing to
# install, and the row names the package that provides it.
with runners(False):
    for key, (_bin, pkg) in RUNNER_KEYS.items():
        check(f"Linux cannot install {key} without its interpreter",
              fetchdeps.install_kind(key, platform="linux") == "unsupported")
        problem = fetchdeps.install_problem(key, platform="linux")
        check(f"...and the row names {pkg} ({problem!r})",
              problem and f"apt-get install {pkg}" in problem)

# A container on a 32-bit ARM/x86 host: upstream ships no build for it, so the
# row must never promise a download — a tool upstream ALSO packages falls back
# to that package, and the rest say why there is nothing to fetch.
with runners(True):
    for key in LINUX_NATIVE:
        expected = "system" if key in LINUX_PACKAGED_NATIVE else "unsupported"
        check(f"{key} on 32-bit ARM resolves to {expected}",
              fetchdeps.install_kind(key, platform="linux", machine="armv7l") == expected)
    check("...and a tool with no package says why",
          "architecture" in (fetchdeps.install_problem(
              "oxipng", platform="linux", machine="armv7l") or ""))

# macOS: apt is not the answer there, so nothing is a "system package" and only
# the platform-independent downloads stay installable.
with runners(True):
    for key in PLATFORM_FREE:
        check(f"macOS installs {key} itself",
              fetchdeps.install_kind(key, platform="other") == "deps")
check("macOS does not pretend flac is an apt package",
      fetchdeps.install_kind("flac", platform="other") == "unsupported")


# The refusal is the row's own text, so what a user reads and what an install
# would say cannot drift apart. On a 32-bit ARM host every entry here is a
# system row (no native build matches), so the package name is the whole answer
# whatever this suite runs on.
for key, pkg in APT_KEYS.items():
    problem = fetchdeps.install_problem(key, platform="linux", machine="armv7l")
    check(f"{key}'s row names the distro package ({problem!r})",
          problem and f"apt-get install {pkg}" in problem)
check("an installable tool carries no refusal",
      fetchdeps.install_problem("oxipng", platform="linux") is None
      and fetchdeps.install_problem("cuetools", platform="windows") is None)

# Markers follow the platform: the Windows install must still look for .exe
# names, the Linux one for the bare binaries a machine here can exec.
check("Windows markers are the .exe names",
      fetchdeps.markers("oxipng", platform="windows") == ("oxipng.exe",)
      and fetchdeps.markers("slskd", platform="windows") == ("slskd.exe",))
check("Linux markers are the bare names",
      fetchdeps.markers("oxipng", platform="linux") == ("oxipng",)
      and fetchdeps.markers("slskd", platform="linux") == ("slskd",))

# Asset selection per platform: the Windows pin must never be picked on Linux,
# and the Linux build must never be picked on Windows.
WINDOWS_ASSETS = ["oxipng-10.2.0-x86_64-pc-windows-msvc.zip",
                  "oxipng-10.2.0-i686-pc-windows-msvc.zip"]
LINUX_ASSETS = ["oxipng-10.2.0-x86_64-unknown-linux-musl.tar.gz",
                "oxipng-10.2.0-aarch64-unknown-linux-musl.tar.gz",
                "oxipng_10.2.0-1_amd64.deb"]


def picks(key, names, **kw):
    """First of *names* the platform patterns for *key* accept."""
    for pattern in fetchdeps._asset_patterns(key, **kw):
        for name in names:
            if re.match(pattern, name, re.IGNORECASE):
                return name
    return None


check("Linux picks oxipng's musl tarball",
      picks("oxipng", WINDOWS_ASSETS + LINUX_ASSETS,
            platform="linux", machine="x86_64")
      == "oxipng-10.2.0-x86_64-unknown-linux-musl.tar.gz")
check("Linux does not pick a Windows zip",
      picks("oxipng", WINDOWS_ASSETS, platform="linux", machine="x86_64") is None)
check("Windows does not pick a Linux tarball",
      picks("oxipng", LINUX_ASSETS, platform="windows") is None)
check("oxipng's one static tarball serves every libc",
      picks("oxipng", LINUX_ASSETS, platform="linux", machine="aarch64")
      == "oxipng-10.2.0-aarch64-unknown-linux-musl.tar.gz")

# slskd ships both libcs and they are not interchangeable: its musl apphost
# names a loader a glibc host does not have (it exits 127, "not found"), so the
# host's libc has to decide.
SLSKD_ASSETS = ["slskd-0.26.0-win-x64.zip", "slskd-0.26.0-linux-x64.zip",
                "slskd-0.26.0-linux-musl-x64.zip"]
real_musl = fetchdeps.musl_libc
try:
    fetchdeps.musl_libc = lambda: False
    check("a glibc host picks slskd's glibc build",
          picks("slskd", SLSKD_ASSETS, platform="linux", machine="x86_64")
          == "slskd-0.26.0-linux-x64.zip")
    fetchdeps.musl_libc = lambda: True
    check("a musl host picks slskd's musl build",
          picks("slskd", SLSKD_ASSETS, platform="linux", machine="x86_64")
          == "slskd-0.26.0-linux-musl-x64.zip")
finally:
    fetchdeps.musl_libc = real_musl

# The two tools that used to read "no build here" on Linux: AudioAuditor has
# real linux binaries upstream (the pin's Windows asset predates them), and
# CUETools' Windows console tool runs under mono — so both install here, and
# what callers execute is the launcher rather than the .exe.
AUDIOAUDITOR_ASSETS = ["AudioAuditorCLI-win-x64.exe", "AudioAuditorCLI-linux-arm64",
                       "AudioAuditorCLI-linux-x64"]
check("Linux picks AudioAuditor's own linux build, never the Windows exe",
      picks("audioauditor", AUDIOAUDITOR_ASSETS, platform="linux", machine="x86_64")
      == "AudioAuditorCLI-linux-x64")
check("...and the arm64 build for arm64",
      picks("audioauditor", AUDIOAUDITOR_ASSETS, platform="linux", machine="aarch64")
      == "AudioAuditorCLI-linux-arm64")
check("Windows still picks the pinned .exe",
      picks("audioauditor", AUDIOAUDITOR_ASSETS, platform="windows")
      == "AudioAuditorCLI-win-x64.exe")
check("Linux takes CUETools' Windows zip (mono runs it)",
      picks("cuetools", ["CUETools_2.2.6.zip"], platform="linux", machine="x86_64")
      == "CUETools_2.2.6.zip")
check("...and callers run the mono launcher there, the .exe on Windows",
      fetchdeps.run_name("cuetools", platform="linux") == "CUETools.ARCUE"
      and fetchdeps.run_name("cuetools", platform="windows") == "CUETools.ARCUE.exe")


# --------------------------------------------------------------------------- #
# What a Linux host offers, end to end (with the network stubbed out)
# --------------------------------------------------------------------------- #
with runners(True), simulated_platform("posix"):
    # "Install / update all" and the per-tool buttons read this list: every tool
    # this platform can fetch — the native builds upstream has an asset for on
    # THIS architecture (rsgain is x86-64 only), the pip/source/phar set.
    installable = fetchdeps.installable_keys()
    check("Install all on Linux lists every platform-free tool",
          set(PLATFORM_FREE) <= set(installable))
    check("...and every native build upstream has an asset for here",
          set(LINUX_NATIVE_ARM_TOO) | (
              set(LINUX_NATIVE_X64_ONLY) if fetchdeps.linux_arch() == "x64" else set()
          ) <= set(installable))
    check("...and no distro row it cannot download",
          not (set(APT_ONLY) & set(installable)))

    # The installer must go for oxipng's own release rather than refuse it.
    real_release = fetchdeps.get_latest_release
    fetchdeps.get_latest_release = (
        lambda key, upstream=False: {"version": "10.2.0", "assets": LINUX_ASSETS,
                                     "urls": {}})
    try:
        got = fetchdeps.pick_asset("oxipng")
        check(f"Linux picks a native asset through pick_asset() (got {got!r})",
              bool(got) and got.endswith("-unknown-linux-musl.tar.gz"))

        # A release whose assets do not match our patterns fails as a missing
        # asset — the point is that it is NOT the old "Windows binary only"
        # refusal, which arrived before any download was attempted.
        fetchdeps.get_latest_release = (
            lambda key, upstream=False: {"version": "10.2.0",
                                         "assets": ["oxipng-10.2.0.tar.xz"],
                                         "urls": {}})
        e = install_fails("oxipng")
        check(f"a Linux install reaches the download path ({e})",
              e is not None and "No suitable" in str(e))
    finally:
        fetchdeps.get_latest_release = real_release

    # The distro-only tools still refuse — with the reason the row shows, and
    # before anything is downloaded. rsgain and chromaprint no longer do: on
    # x86-64 upstream publishes a build this app can unpack, so they install
    # here and only fall back to their package on an architecture without one.
    for key, pkg in APT_ONLY.items():
        e = install_fails(key)
        check(f"{key}: refused on Linux, naming the package ({e})",
              e is not None and f"apt-get install {pkg}" in str(e))
    for key in LINUX_PACKAGED_NATIVE:
        check(f"{key} is not refused on this host, its package is the fallback",
              fetchdeps.install_kind(key, platform="linux", machine="armv7l") == "system"
              and fetchdeps.install_problem(key, platform="linux",
                                            machine="armv7l") is not None)

    # yt-dlp's Windows .exe is not what Linux installs: it is pip-routed
    # (PIP_ON_LINUX) and must NOT be refused, or Linux never gets it at all.
    routed = []
    real_pip = fetchdeps._install_pip_package
    fetchdeps._install_pip_package = (
        lambda key, log=print, progress=None: routed.append(key) or "pip-routed")
    try:
        for key in ("librosa", "beets", "yt-dlp"):
            version = fetchdeps.install_dependency(key, log=lambda m: None)
            check(f"{key} installs from pip on Linux (got {version!r})",
                  version == "pip-routed")
    finally:
        fetchdeps._install_pip_package = real_pip
    check(f"linux pip routing (routed {routed})",
          routed == ["librosa", "beets", "yt-dlp"])

    # The target column has to agree with the install path: a fetched tool shows
    # its release, a distro tool its package.
    latest = fetchdeps.latest_versions()
    for key in LINUX_NATIVE + PLATFORM_FREE:
        check(f"Linux reports {key}'s own version as the target",
              latest[key] == fetchdeps.PINNED[key]["version"])
    for key in APT_ONLY:
        check(f"Linux reports {key}'s target as apt: {APT_ONLY[key]}",
              latest[key] == f"apt: {APT_ONLY[key]}")
    check("every Linux row reports either a version or its package",
          all(v for v in latest.values()))

# ...and Windows behaviour is unchanged.
with simulated_platform("nt"):
    for key in fetchdeps.DISPLAY_NAMES:
        check(f"Windows keeps {key} installable",
              fetchdeps.installable(key) and fetchdeps.install_problem(key) is None)

    # The pinned Windows assets still resolve to those exact names.
    real_release = fetchdeps.get_latest_release
    fetchdeps.get_latest_release = (
        lambda key, upstream=False: {"assets": [fetchdeps.PINNED[key]["asset"]]})
    try:
        for key in fetchdeps.PINNED:
            if not fetchdeps.PINNED[key].get("asset"):
                continue          # pip packages and the source archive
            got = fetchdeps.pick_asset(key)
            pinned = fetchdeps.PINNED[key]["asset"]
            check(f"Windows picks {key}'s pinned asset (got {got!r})",
                  got == pinned)
    finally:
        fetchdeps.get_latest_release = real_release


# --------------------------------------------------------------------------- #
# A native Linux install: tarball extraction, marker lookup, exec bit
# --------------------------------------------------------------------------- #
with simulated_platform("posix"), tempfile.TemporaryDirectory() as tmp:
    # The downloaded temp file is named from the asset, and a ".gz" name is not
    # a tarball to _extract_archive: it fell through to "run it as an installer"
    # and died with rc=126 the first time a Linux tarball was fetched.
    check("a tarball asset keeps its whole extension on disk",
          fetchdeps.archive_suffix("oxipng-10.2.0-x86_64-unknown-linux-musl.tar.gz")
          == ".tar.gz")
    check("a zip asset keeps its own",
          fetchdeps.archive_suffix("slskd-0.26.0-linux-musl-x64.zip") == ".zip")
    check("a bare-exe asset still gets its name",
          fetchdeps.archive_suffix("yt-dlp.exe") == ".exe")

    src = os.path.join(tmp, "oxipng-10.2.0-x86_64-unknown-linux-musl")
    os.makedirs(src)
    binary = os.path.join(src, "oxipng")
    with open(binary, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\necho oxipng\n")
    with open(os.path.join(src, "LICENSE"), "w", encoding="utf-8") as f:
        f.write("MIT\n")
    tarball = os.path.join(tmp, "oxipng.tar.gz")
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(src, arcname=os.path.basename(src))
    out = os.path.join(tmp, "out")
    os.makedirs(out)
    fetchdeps.extract_installer(tarball, out, log=lambda m: None)
    landed = os.path.join(out, os.path.basename(src), "oxipng")
    check("a .tar.gz release asset extracts", os.path.isfile(landed))
    check("the marker lookup finds the native build",
          fetchdeps._locate_binaries(out, "oxipng") == os.path.dirname(landed))

    # zipfile restores no file modes, so a binary unpacked from a .zip lands
    # unexecutable — slskd, the one tool this app RUNS, included.
    os.chmod(landed, 0o644)
    fetchdeps._make_executable(os.path.dirname(landed), ("oxipng",))
    if not HOST_WINDOWS:
        check("the installed binary is executable", os.access(landed, os.X_OK))
    check("the marker lookup ignores a folder without the markers",
          fetchdeps._locate_binaries(out, "slskd") is None)


# --------------------------------------------------------------------------- #
# The detector sees a native install (an installed tool is never "missing")
# --------------------------------------------------------------------------- #
from mlo import tools  # noqa: E402

with simulated_platform("posix"), tempfile.TemporaryDirectory() as tmp:
    # Detection reads every tools folder through the resolver (mlo.paths
    # .tools_dirs, imported by name into mlo.tools): pointing it at one temp
    # folder is what makes the assertions below independent of what this
    # machine happens to have installed.
    real_dirs = tools.tools_dirs
    tools.tools_dirs = lambda music_folder=None: [tmp]
    try:
        folder = os.path.join(tmp, "oxipng v10.2.0")
        os.makedirs(folder)
        with open(os.path.join(folder, "oxipng"), "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n")
        found = tools._detect_deps_native()
        check("a native .dependencies install is detected with its version",
              found.get("oxipng", {}).get("version") == "10.2.0"
              and found["oxipng"]["oxipng_exe"].endswith(
                  os.path.join("oxipng v10.2.0", "oxipng")))

        # The Windows folders this folder can also hold (a shared volume, a
        # desktop install) must never be taken for a native one.
        win = os.path.join(tmp, "slskd v0.26.0")
        os.makedirs(win)
        with open(os.path.join(win, "slskd.exe"), "w", encoding="utf-8") as f:
            f.write("MZ\n")
        check("an .exe folder is not a native install",
              "slskd" not in tools._detect_deps_native())
    finally:
        tools.tools_dirs = real_dirs

with simulated_platform("nt"):
    check("Windows never detects a native binary as a tool",
          tools._detect_deps_native() == {})


# --------------------------------------------------------------------------- #
# Versions the table can only learn by ASKING the tool
# --------------------------------------------------------------------------- #
# A distro package has no versioned folder name to read (that is how a
# .dependencies install reports one), so the row showed "—" and could never be
# compared with upstream. Each tool is asked with its own flag, and jpegtran /
# fpcalc answer on stderr.
for text, want in (
    ("flac 1.5.0", "1.5.0"),
    ("cjxl v0.11.2 [AVX2,SSE4,SSE2]", "0.11.2"),
    ("libjpeg-turbo version 2.1.5 (build 20250503)", "2.1.5"),
    ("rsgain 3.6 - using:", "3.6"),
    ("ffmpeg version 7.1.5-0+deb13u1 Copyright (c) 2000-2026", "7.1.5"),
    ("fpcalc version 1.5.1 (FFmpeg Lavc61.19.100)", "1.5.1"),
    ("no version in this banner", None),
    ("", None),
):
    check(f"version parsed from {text[:32]!r}", tools._first_version(text) == want)

check("a tool that cannot be run yields no version, not an exception",
      tools._probe_version(None) is None
      and tools._probe_version("/nonexistent/tool") is None)
check("the probe reads what the tool prints on stdout",
      tools._probe_version(sys.executable, ("-c", "print('tool 9.8.7')")) == "9.8.7")
check("...and on stderr, where jpegtran and fpcalc answer",
      tools._probe_version(
          sys.executable,
          ("-c", "import sys; print('tool 2.1.5', file=sys.stderr)")) == "2.1.5")


# --------------------------------------------------------------------------- #
# A distro row behind upstream says so — and offers the command, not a dead
# "Install" it cannot perform
# --------------------------------------------------------------------------- #
def rows_with(installed, target, upstream):
    """dependency_rows() for every tool, with the four lookups stubbed out."""
    real = (fetchdeps.detect_all_tools, fetchdeps.installed_versions,
            fetchdeps.latest_versions, fetchdeps.upstream_versions)
    fetchdeps.detect_all_tools = (
        lambda: {k: {"version": v} for k, v in installed.items()})
    fetchdeps.installed_versions = lambda: dict(installed)
    fetchdeps.latest_versions = lambda: dict(target)
    fetchdeps.upstream_versions = lambda refresh=False, block=False: {
        k: {"version": v, "checked_at": 0.0, "error": None}
        for k, v in upstream.items()}
    try:
        return {row["key"]: row for row in fetchdeps.dependency_rows()}
    finally:
        (fetchdeps.detect_all_tools, fetchdeps.installed_versions,
         fetchdeps.latest_versions, fetchdeps.upstream_versions) = real


with simulated_platform("posix"):
    # A distro tool: the distro ships flac, upstream has 1.5.0, and this app has
    # no Linux build of it to fetch — so the row is BEHIND and has to say so,
    # with the one action that is actually available: the package manager's own
    # command. The regression this pins: state was forced to `ok` for every
    # distro row, so the row read a green Ready beside an amber Available whose
    # update no button could make.
    #
    # libjpeg-turbo used to be this file's example and is not any more: upstream
    # publishes a .deb the installer unpacks now (LINUX_BINARIES), so its row is
    # an app-managed download — asserted below, because that IS the change.
    rows = rows_with({"flac": "1.4.3", "oxipng": "10.2.0"},
                     {"flac": "apt: flac", "oxipng": "10.2.0"},
                     {"flac": "1.5.0", "oxipng": "10.2.1"})
    fl = rows["flac"]
    check("a distro tool behind upstream reads Update, not Ready",
          fl["state"] == "update")
    check("...and offers the upgrade the package manager performs",
          fl["action"] == "upgrade"
          and fl["upgrade_command"] == "apt-get install --only-upgrade flac")
    check("...and is never offered a download",
          fl["installable"] is False)
    check("...and carries the versions and the command in its note",
          fl["note"] and "1.4.3" in fl["note"] and "1.5.0" in fl["note"]
          and fl["upgrade_command"] in fl["note"])
    check("...and still shows the newer upstream version",
          fl["update_available"] is True and fl["upstream_version"] == "1.5.0")

    # oxipng is app-managed: Update IS actionable, so it must stay a download —
    # same state, different action, which is the whole point of the split.
    ox = rows["oxipng"]
    check("an app-managed tool behind upstream still reads Update",
          ox["state"] == "update" and ox["installable"] is True
          and ox["action"] == "update" and ox["upgrade_command"] is None)

    # A distro row AT the upstream version is up to date, and its action column
    # stays empty: there is nothing to copy and nothing to press.
    rows = rows_with({"flac": "1.5.0"}, {"flac": "apt: flac"},
                     {"flac": "1.5.0"})
    fl = rows["flac"]
    check("a distro tool at the upstream version reads Ready",
          fl["state"] == "ok" and fl["action"] == "none"
          and fl["upgrade_command"] is None
          and fl["installable"] is False)

    # libjpeg-turbo is the tool that moved: upstream's .deb is unpacked by this
    # app now, so a behind row must offer its OWN install instead of the package
    # manager's command (which is what it offered when the .deb was unusable).
    rows = rows_with({"libjpeg_turbo": "2.1.5"}, {"libjpeg_turbo": "apt: libjpeg-progs"},
                     {"libjpeg_turbo": "3.2.0"})
    jt = rows["libjpeg_turbo"]
    check("a libjpeg-turbo behind upstream is an app-managed download",
          jt["state"] == "update" and jt["installable"] is True
          and jt["install_kind"] == "deps" and jt["action"] == "update"
          and jt["upgrade_command"] is None)


# --------------------------------------------------------------------------- #
# Platform-independent installer invariants
# --------------------------------------------------------------------------- #
check("yt-dlp's Linux install is the pip package, with its version in PINNED",
      fetchdeps.PIP_PACKAGES.get("yt-dlp") == "yt-dlp"
      and bool(fetchdeps.PINNED["yt-dlp"]["version"]))
check("yt-dlp is pip-on-Linux, not a distro package",
      "yt-dlp" in fetchdeps.PIP_ON_LINUX
      and "yt-dlp" not in fetchdeps.LINUX_PACKAGES)
for key in fetchdeps.SINGLE_EXE_TOOLS:
    asset = (fetchdeps.PINNED.get(key) or {}).get("asset", "")
    check(f"{key} is pinned to a bare binary, never an archive ({asset})",
          asset.endswith((".exe", ".phar")))


# --------------------------------------------------------------------------- #
# Archive licences land next to the installed binaries
# --------------------------------------------------------------------------- #
with tempfile.TemporaryDirectory() as tmp:
    src = os.path.join(tmp, "extracted")
    os.makedirs(os.path.join(src, "Win64"))
    for rel in ("LICENSE.txt", "Win64/COPYING", "Win64/README.md",
                "Win64/flac.exe", "Win64/notes.md", "Win64/big.dll"):
        with open(os.path.join(src, rel), "w", encoding="utf-8") as f:
            f.write("text\n")
    dest = os.path.join(tmp, "flac v1.5.0")
    os.makedirs(dest)
    copied = fetchdeps._copy_licence_files(src, dest, log=lambda m: None)
    names = sorted(os.listdir(dest))
    check("licence/readme files copied", names == ["COPYING", "LICENSE.txt", "README.md"])
    check("nothing else copied", sorted(copied) == ["COPYING", "LICENSE.txt", "README.md"])


if FAILURES:
    print(f"{len(FAILURES)} failure(s)")
    sys.exit(1)
print("OK test_platform_guards")
sys.exit(0)
