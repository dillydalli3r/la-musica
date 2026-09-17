#!/usr/bin/env python3
"""Platform guards: the dependency installer and the launcher ownership probes.

Three behaviours that only bite on the platform you are not developing on:

* mlo/fetchdeps.py used to download Windows assets on ANY host and report
  success, because the only check was that the marker *.exe NAMES appeared in
  the target folder - on Linux (the Docker image) that "installed" a folder of
  binaries that cannot run, shown as "ok" in the UI. On a non-Windows host every
  Windows-only tool must now refuse with the distro package to use instead.
  Nothing here touches the network: the download and GitHub helpers are stubbed
  so a refused install fails before them.
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
PLATFORM_FREE = ("librosa", "beets", "simpledrmeter")

real_name = os.name
try:
    os.name = "posix"

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
    for key in list(APT_KEYS) + UNSUPPORTED_KEYS:
        try:
            fetchdeps.pick_asset(key)
            check(f"pick_asset({key}) refuses on a non-Windows host", False)
        except RuntimeError:
            pass

    # The platform-independent tools must stay installable.
    for key in PLATFORM_FREE:
        try:
            fetchdeps._require_windows(key)
        except RuntimeError as e:
            check(f"{key} stays installable off Windows ({e})", False)
finally:
    os.name = real_name

# ...and Windows behaviour is unchanged.
for key in list(APT_KEYS) + UNSUPPORTED_KEYS + list(PLATFORM_FREE):
    try:
        fetchdeps._require_windows(key)
    except RuntimeError as e:
        check(f"Windows keeps {key} installable ({e})", False)


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
import tray  # noqa: E402

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
