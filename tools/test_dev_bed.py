#!/usr/bin/env python3
"""Regression: the dev bed asks the port instead of assuming it, its stop kills
the TREE, and `--fresh` cannot delete anything that is not the bed's own.

All three fail silently. A free-port probe written the obvious way — bind with
SO_REUSEADDR — calls a port FREE while another process is listening on it on
Windows, so a second bed starts uvicorn against a taken port; uvicorn's bind
loses, and the bed then seeds the config of whatever was already there (measured:
the second bed's `POST /api/config` landed on the FIRST bed's server, and its
"stop" left a port bound to a pid that no longer exists, because `uvicorn
--reload` hands the listening socket to a forked child). `--fresh` is a recursive
delete driven by a path the user typed, so its guard is the only thing between a
typo and a wiped folder.

No server is started and nothing long-lived is bound; the tree-kill case uses a
two-level process that only writes a heartbeat file.

Run:  python tools/test_dev_bed.py
"""
import argparse
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import dev as devtool                                                    # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{'' if ok else '  ' + str(detail)}")
    if not ok:
        failures += 1


# --------------------------------------------------------------------------- #
# The port probe
# --------------------------------------------------------------------------- #
holder = socket.socket()
holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)     # uvicorn's own
holder.bind(("127.0.0.1", 0))
holder.listen(5)
busy = holder.getsockname()[1]
# Serve and hang up, so the port has peers in TIME_WAIT — the state a server is
# in the moment after it answers a request, and the state in which a bind probe
# wearing SO_REUSEADDR SUCCEEDS on Windows and calls this busy port free.
for _ in range(2):
    peer = socket.create_connection(("127.0.0.1", busy))
    peer.sendall(b"x")
    peer.close()
time.sleep(0.5)

check("a listening port reads as in use", devtool._in_use(busy))
check("free_port walks off a busy port", devtool.free_port(busy) != busy,
      f"returned {busy}, which the holder is listening on")
check("and 8000 is never handed out (the owner's live install)",
      devtool.free_port(7999) != 8000)

holder.close()
time.sleep(0.3)
check("a released port reads as free again", not devtool._in_use(busy))

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
def bed(**overrides):
    base = dict(music=None, port=8011, web_port=5181, no_web=False,
                no_reload=False, no_tray=True, no_open=True, fresh=False)
    base.update(overrides)
    return devtool.DevBed(argparse.Namespace(**base))


default = bed()
check("the default library is the repo's own scratch folder",
      default.music == ROOT / "local" / "dev" / "music", default.music)
check("its logs sit beside it, not in the library",
      default.logs.parent == default.music.parent, default.logs)
check("both ports come from 8011 up", default.port >= 8011 and default.web_port > 0,
      (default.port, default.web_port))

# --------------------------------------------------------------------------- #
# The wipe guard
# --------------------------------------------------------------------------- #
outside = Path(tempfile.gettempdir()).parent / "mlo-dev-bed-guard-probe"
try:
    devtool.wipe(outside, default.home, default.music)
    check("--fresh refuses a path outside the bed", False, "it did not refuse")
except SystemExit as exc:
    check("--fresh refuses a path outside the bed", "refusing" in str(exc), exc)

inside = Path(tempfile.gettempdir()) / "mlo-dev-bed-wipe-probe"
inside.mkdir(parents=True, exist_ok=True)
(inside / "jar").write_text("x", encoding="utf-8")
devtool.wipe(inside, default.home, default.music)
check("--fresh still wipes inside a scratch root", not inside.exists(), inside)

# --------------------------------------------------------------------------- #
# stop() kills the tree
# --------------------------------------------------------------------------- #
heartbeat = Path(tempfile.gettempdir()) / "mlo-dev-bed-heartbeat.txt"
heartbeat.unlink(missing_ok=True)
grandchild = (
    "import sys, time\n"
    "f = open(sys.argv[1], 'a')\n"
    "while True:\n"
    "    f.write('.')\n"
    "    f.flush()\n"
    "    time.sleep(0.15)\n")
child = devtool.Child("probe", Path(tempfile.gettempdir()) / "mlo-dev-bed-probe.log")
child.start([sys.executable, "-c",
             "import subprocess, sys, time\n"
             f"subprocess.Popen([sys.executable, '-c', {grandchild!r}, sys.argv[1]])\n"
             "time.sleep(60)\n", str(heartbeat)],
            ROOT, dict(os.environ))
time.sleep(2.0)
grew = heartbeat.exists() and heartbeat.stat().st_size > 0
check("the probe's grandchild is running", grew, heartbeat)
child.stop()
# Let the kill land before measuring — a taskkill is not instantaneous, and a
# write that races it would read as "still alive". A grandchild that SURVIVED
# keeps writing, so the two samples below still differ and the check still fails.
time.sleep(1.5)
size = heartbeat.stat().st_size if heartbeat.exists() else 0
time.sleep(1.5)
now = heartbeat.stat().st_size if heartbeat.exists() else 0
check("stop() killed the grandchild too, not just the leader", grew and size == now,
      f"heartbeat kept going: {size} -> {now}")
check("stop() is safe to call twice", not child.alive())
heartbeat.unlink(missing_ok=True)

# --------------------------------------------------------------------------- #
print()
if failures:
    print(f"dev bed: {failures} check(s) FAILED")
    sys.exit(1)
print("dev bed: ok — the port probe, the wipe guard and the tree kill all hold")
