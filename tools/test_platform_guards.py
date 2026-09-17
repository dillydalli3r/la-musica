#!/usr/bin/env python3
"""Platform guards: the dependency installer and the launcher ownership probes.

Three behaviours that only bite on the platform you are not developing on:

* mlo/fetchdeps.py used to download Windows assets on ANY host and report
  success, because the only check was that the marker *.exe NAMES appeared in
  the target folder - on Linux (the Docker image) that "installed" a folder of
  binaries that cannot run, shown as "ok" in the UI. On a non-Windows host every
  Windows-only tool must now refuse with the distro package to use instead.
  Nothing here touches the network: the download and GitHub helpers are stubbed
  so a refused install fails before them. Both branches are simulated rather
  than read off the host, and tray.py's GUI import is stubbed - a headless CI
  runner has no DISPLAY, so the exit code is the same everywhere.
* start_app.py declared "Backend is up." for anything listening on :8000, while
  tray.py required our own JSON status - so a foreign server answering 200 got
  adopted by one launcher and called foreign by the other. Both must agree.
* The archive's LICENSE/COPYING/README has to land in the versioned
  .dependencies folder (GPL-2.0 §1 asks for the licence text with the binary).

Run: python tools/test_platform_guards.py
Exit 0 = pass, 1 = failure.
"""
import json
import os
import sys
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo import fetchdeps  # noqa: E402

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
UNSUPPORTED_KEYS = [k for k, v in fetchdeps.LINUX_PACKAGES.items() if not v]
WINDOWS_ONLY = list(APT_KEYS) + UNSUPPORTED_KEYS
PLATFORM_FREE = ("librosa", "beets", "simpledrmeter", "yt-dlp")


class simulated_platform:
    """Pin the platform the guard reads (os.name) instead of the host's.

    Which branch of _require_windows() runs must not depend on where the suite
    runs, so both are entered explicitly - "nt" here, "posix" (Linux AND macOS)
    below - and os.name is restored in __exit__, i.e. unconditionally.
    """

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.real = os.name
        os.name = self.name
        return self

    def __exit__(self, *exc):
        os.name = self.real
        return False


def require_windows_fails(key):
    """The RuntimeError _require_windows() raises, or None when it passes."""
    try:
        fetchdeps._require_windows(key)
    except RuntimeError as e:
        return e
    return None


with simulated_platform("posix"):
    for key, pkg in APT_KEYS.items():
        try:
            fetchdeps.install_dependency(key, log=lambda m: None)
            check(f"{key}: refused on a non-Windows host", False)
        except RuntimeError as e:
            check(f"{key}: names the distro package",
                  f"apt-get install {pkg}" in str(e))

    for key in UNSUPPORTED_KEYS:
        try:
            fetchdeps.install_dependency(key, log=lambda m: None)
            check(f"{key}: refused on a non-Windows host", False)
        except RuntimeError as e:
            check(f"{key}: reported unsupported",
                  "unsupported on this platform" in str(e))

    # Selection, not just installation: no .exe asset may even be picked.
    for key in WINDOWS_ONLY:
        try:
            asset = fetchdeps.pick_asset(key)
            check(f"pick_asset({key}) refuses off Windows (got {asset!r})", False)
        except RuntimeError:
            pass

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

    # The platform-independent tools must stay installable.
    for key in PLATFORM_FREE:
        e = require_windows_fails(key)
        check(f"{key} stays installable off Windows ({e})", e is None)

# ...and Windows behaviour is unchanged.
with simulated_platform("nt"):
    for key in WINDOWS_ONLY + list(PLATFORM_FREE):
        e = require_windows_fails(key)
        check(f"Windows keeps {key} installable ({e})", e is None)

    # The pinned Windows assets still resolve to those exact names.
    real_release = fetchdeps.get_latest_release
    fetchdeps.get_latest_release = (
        lambda key: {"assets": [fetchdeps.PINNED[key]["asset"]]})
    try:
        for key in WINDOWS_ONLY:
            got = fetchdeps.pick_asset(key)
            pinned = fetchdeps.PINNED[key]["asset"]
            check(f"Windows picks {key}'s pinned asset (got {got!r})",
                  got == pinned)
    finally:
        fetchdeps.get_latest_release = real_release


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
