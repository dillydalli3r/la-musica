#!/usr/bin/env python3
"""Platform guards: where a dependency install is allowed to go, and who owns :8000.

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
simulated_platform), and tray.py's GUI import is stubbed — a headless CI runner
has no display.
"""
import json
import os
import re
import sys
import tarfile
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

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
UNSUPPORTED_KEYS = [k for k in fetchdeps.LINUX_PACKAGES if not fetchdeps.LINUX_PACKAGES[k]]
WINDOWS_ONLY = list(APT_KEYS) + UNSUPPORTED_KEYS
# Tools upstream publishes a native Linux build for: the *fetchable* half of a
# Linux install, and the reason a container is no longer stuck without them.
LINUX_NATIVE = tuple(fetchdeps.LINUX_BINARIES)
PLATFORM_FREE = ("librosa", "beets", "simpledrmeter", "yt-dlp")


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


# --------------------------------------------------------------------------- #
# The platform table: three answers, both platforms, no globals patched
# --------------------------------------------------------------------------- #
# install_kind() is pure, so the whole truth table is checked here whatever the
# suite runs on.
for key in fetchdeps.DISPLAY_NAMES:
    check(f"Windows installs {key} itself",
          fetchdeps.install_kind(key, platform="windows") == "deps")

for key in LINUX_NATIVE + PLATFORM_FREE:
    check(f"Linux installs {key} from its own release",
          fetchdeps.install_kind(key, platform="linux", machine="x86_64") == "deps")
    check(f"...on arm64 too",
          fetchdeps.install_kind(key, platform="linux", machine="aarch64") == "deps")

for key in APT_KEYS:
    check(f"Linux reports {key} as the distro package",
          fetchdeps.install_kind(key, platform="linux", machine="x86_64") == "system")

for key in UNSUPPORTED_KEYS:
    check(f"Linux cannot install {key} at all",
          fetchdeps.install_kind(key, platform="linux", machine="x86_64") == "unsupported")

# A container on a 32-bit ARM/x86 host: upstream ships no build for it, so the
# row must say so instead of promising a download that cannot happen.
for key in LINUX_NATIVE:
    check(f"{key} reports unsupported on an architecture it has no build for",
          fetchdeps.install_kind(key, platform="linux", machine="armv7l") == "unsupported")

# macOS: apt is not the answer there, so nothing is a "system package" and only
# the platform-independent downloads stay installable.
for key in PLATFORM_FREE:
    check(f"macOS installs {key} itself",
          fetchdeps.install_kind(key, platform="other") == "deps")
check("macOS does not pretend flac is an apt package",
      fetchdeps.install_kind("flac", platform="other") == "unsupported")


class simulated_android:
    """Android as CPython reports it: posix + "linux", plus getandroidapilevel.

    The doc'd test for Android is that attribute's presence (`sys.platform` is
    "linux" there too), which is why the installer has to ask.
    """

    def __enter__(self):
        self.real = (os.name, sys.platform)
        os.name, sys.platform = "posix", "linux"
        sys.getandroidapilevel = lambda: 24
        return self

    def __exit__(self, *exc):
        os.name, sys.platform = self.real
        del sys.getandroidapilevel
        return False


with simulated_android():
    check("Android is not a Linux host with downloads of its own",
          fetchdeps.host_platform() == "other")
    for key in LINUX_NATIVE:
        check(f"Android is not offered a desktop {key} build",
              fetchdeps.install_kind(key) == "unsupported")

# The refusal is the row's own text, so what a user reads and what an install
# would say cannot drift apart.
for key, pkg in APT_KEYS.items():
    problem = fetchdeps.install_problem(key, platform="linux")
    check(f"{key}'s row names the distro package ({problem!r})",
          problem and f"apt-get install {pkg}" in problem)
for key in UNSUPPORTED_KEYS:
    problem = fetchdeps.install_problem(key, platform="linux")
    check(f"{key}'s row says it is unsupported here ({problem!r})",
          problem and "unsupported on this platform" in problem)
check("an installable tool carries no refusal",
      fetchdeps.install_problem("oxipng", platform="linux") is None
      and fetchdeps.install_problem("flac", platform="windows") is None)

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


# --------------------------------------------------------------------------- #
# What a Linux host offers, end to end (with the network stubbed out)
# --------------------------------------------------------------------------- #
with simulated_platform("posix"):
    # "Install / update all" and the per-tool buttons read this list: the
    # fetchable tools only — never a distro package, never a Windows-only one.
    installable = fetchdeps.installable_keys()
    check("Install all on Linux lists exactly what it can fetch",
          set(installable) == set(LINUX_NATIVE) | set(PLATFORM_FREE))

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

    # The distro-provided and Windows-only tools still refuse — with the reason
    # the row shows, and before anything is downloaded.
    for key, pkg in APT_KEYS.items():
        e = install_fails(key)
        check(f"{key}: refused on Linux, naming the package ({e})",
              e is not None and f"apt-get install {pkg}" in str(e))
    for key in UNSUPPORTED_KEYS:
        e = install_fails(key)
        check(f"{key}: refused on Linux as unsupported ({e})",
              e is not None and "unsupported on this platform" in str(e))

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
    # its release, a distro tool its package, a Windows-only one nothing.
    latest = fetchdeps.latest_versions()
    for key in LINUX_NATIVE:
        check(f"Linux reports {key}'s own version as the target",
              latest[key] == fetchdeps.PINNED[key]["version"])
    for key in APT_KEYS:
        check(f"Linux reports {key}'s target as apt: {APT_KEYS[key]}",
              latest[key] == f"apt: {APT_KEYS[key]}")
    for key in UNSUPPORTED_KEYS:
        check(f"Linux reports no target for {key}", latest[key] is None)

# ...and Windows behaviour is unchanged.
with simulated_platform("nt"):
    for key in WINDOWS_ONLY + list(PLATFORM_FREE) + list(LINUX_NATIVE):
        check(f"Windows keeps {key} installable",
              fetchdeps.installable(key) and fetchdeps.install_problem(key) is None)

    # The pinned Windows assets still resolve to those exact names.
    real_release = fetchdeps.get_latest_release
    fetchdeps.get_latest_release = (
        lambda key, upstream=False: {"assets": [fetchdeps.PINNED[key]["asset"]]})
    try:
        for key in WINDOWS_ONLY:
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
          fetchdeps._archive_suffix("oxipng-10.2.0-x86_64-unknown-linux-musl.tar.gz")
          == ".tar.gz")
    check("a zip asset keeps its own",
          fetchdeps._archive_suffix("slskd-0.26.0-linux-musl-x64.zip") == ".zip")
    check("a bare-exe asset still gets its name",
          fetchdeps._archive_suffix("yt-dlp.exe") == ".exe")

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
    fetchdeps._extract_archive(tarball, out, log=lambda m: None)
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
    real_deps = tools.DEPS_DIR
    tools.DEPS_DIR = tmp
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
        tools.DEPS_DIR = real_deps

with simulated_platform("nt"):
    check("Windows never detects a native binary as a tool",
          tools._detect_deps_native() == {})


# --------------------------------------------------------------------------- #
# Platform-independent installer invariants
# --------------------------------------------------------------------------- #
check("yt-dlp's Linux install is the pip package",
      fetchdeps.PIP_PACKAGES.get("yt-dlp", "").startswith("yt-dlp=="))
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


# --------------------------------------------------------------------------- #
# The two launchers agree on who owns :8000
# --------------------------------------------------------------------------- #
class _Health(BaseHTTPRequestHandler):
    status = "ok"

    def do_GET(self):  # noqa: N802 - http.server API
        body = json.dumps({"status": self.status}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


import start_app  # noqa: E402

# tray.py imports pystray at module scope, and on a headless host that import
# dies on Xlib ("Bad display name \"\"": no DISPLAY on a CI runner). The
# ownership probes below never touch the GUI, so the backend is stubbed out for
# the import and put back afterwards.
_saved_pystray = sys.modules.get("pystray")
sys.modules["pystray"] = types.ModuleType("pystray")
try:
    import tray  # noqa: E402
finally:
    if _saved_pystray is None:
        del sys.modules["pystray"]
    else:
        sys.modules["pystray"] = _saved_pystray

server = HTTPServer(("127.0.0.1", 0), _Health)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
for mod in (start_app, tray):
    mod.PORT = port
    mod.URL = f"http://127.0.0.1:{port}"
try:
    # A foreign server: HTTP 200 on /api/health, but not our status.
    _Health.status = ""
    check("start_app: a foreign 200 is not our backend", start_app._backend_ours() is False)
    check("tray: a foreign 200 is not our backend", tray.backend_ours() is False)
    check("tray calls it foreign", tray.backend_state() == "foreign")

    # Our backend: the same JSON start_app is now required to check for.
    _Health.status = "ok"
    check("start_app: ours is recognised", start_app._backend_ours() is True)
    check("tray: ours is recognised", tray.backend_ours() is True)
    check("tray calls it ours", tray.backend_state() == "ours")
finally:
    server.shutdown()
    server.server_close()

if FAILURES:
    print(f"{len(FAILURES)} failure(s)")
    sys.exit(1)
print("OK test_platform_guards")
sys.exit(0)
