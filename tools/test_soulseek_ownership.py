#!/usr/bin/env python3
"""Verify this app refuses to adopt ANOTHER app's slskd on its web port.

slskd's default web port (5030) is shared with every other app that bundles
slskd. When a foreign instance held the port, `web_up()` said "running" while
every API call was rejected (auth required / different account), so the UI was
stuck on "running, not logged in" forever — and Stop killed the other app's
process, which re-spawned it.

Run:  python tools/test_soulseek_ownership.py
"""
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import soulseek

# --------------------------------------------------------------------------- #
# stub slskd web API
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    status = 200
    body = b"{}"

    def do_GET(self):
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *a):
        pass

def serve(status, body):
    """Start a stub on a free port; returns (port, shutdown_fn)."""
    handler = type("H", (_Handler,), {"status": status, "body": body})
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1], srv.shutdown

def cfg(port, username="masayoshifan6767"):
    return {"soulseek_web_port": port, "soulseek_username": username}

# --------------------------------------------------------------------------- #
# 1. classification
# --------------------------------------------------------------------------- #
# nothing listening -> not ours, no conflict (caller may spawn)
free_port, close_free = serve(200, b"{}")
close_free()
ours, who, why = soulseek.instance_owner(cfg(free_port))
assert (ours, who, why) == (False, None, ""), (ours, who, why)

# another app's slskd, web auth enabled -> 401/403
port, close = serve(401, b"")
ours, who, why = soulseek.instance_owner(cfg(port))
assert ours is False and who is None and "requires authentication" in why, (ours, who, why)
close()

# another app's slskd, auth disabled, DIFFERENT Soulseek account
port, close = serve(200, b'{"user":{"username":"dillydallier07"}}')
ours, who, why = soulseek.instance_owner(cfg(port))
assert ours is False and who == "dillydallier07", (ours, who, why)
assert "signed in as dillydallier07" in why, why
# ...and it is exactly the case where web_up() alone lies:
assert soulseek.web_up(cfg(port)) is True, "web_up must still see the port as taken"
close()

# a non-slskd program squatting the port
port, close = serve(500, b"")
ours, who, why = soulseek.instance_owner(cfg(port))
assert ours is False and "HTTP 500" in why, (ours, who, why)
close()

# our own slskd (same account, auth disabled on localhost)
port, close = serve(200, b'{"user":{"username":"masayoshifan6767"}}')
assert soulseek.instance_owner(cfg(port)) == (True, "masayoshifan6767", "")

# our own slskd, signed out -> ours, account unknown (login still pending)
port2, close2 = serve(200, b'{"user":{}}')
assert soulseek.instance_owner(cfg(port2)) == (True, None, ""), soulseek.instance_owner(cfg(port2))

# --------------------------------------------------------------------------- #
# 2. start() must refuse a foreign listener instead of adopting it
# --------------------------------------------------------------------------- #
spawned = []
soulseek.write_config = lambda cfg=None: "test-key"
_real_popen = soulseek.subprocess.Popen
soulseek.subprocess.Popen = lambda *a, **kw: spawned.append(a) or (_ for _ in ()).throw(AssertionError("spawned"))
soulseek._proc["proc"] = None

port_foreign, close3 = serve(401, b"")
ok, msg = soulseek.start(cfg(port_foreign))
assert ok is False, (ok, msg)
assert "cannot start slskd" in msg and "another application's slskd" in msg, msg
assert "one slskd can run at a time" in msg, msg
assert spawned == [], f"must not spawn slskd while a foreign one holds the port: {spawned}"

# our own instance IS adopted (no duplicate spawn)
ok, msg = soulseek.start(cfg(port))
assert (ok, msg) == (True, "adopted already-running slskd"), (ok, msg)
assert spawned == [], spawned

# --------------------------------------------------------------------------- #
# 3. stop() must never kill a foreign listener
# --------------------------------------------------------------------------- #
killed = []
soulseek._kill_port_listener = lambda p: killed.append(p) or True
soulseek._proc["proc"] = None  # untracked/adopted path
assert soulseek.stop(cfg(port_foreign)) is False, "stop() claimed to stop a foreign slskd"
assert killed == [], f"stop() killed another app's process on port {port_foreign}"

# an openly-answering slskd on our port is ours to stop, even if it is signed
# in as another account (e.g. the account was changed after it was started)
assert soulseek.stop(cfg(port)) is True
assert killed == [port], killed

# --------------------------------------------------------------------------- #
# 4. slskd's single-instance lock must not look like a dead Start button
# --------------------------------------------------------------------------- #
import tempfile  # noqa: E402

tmpdir = tempfile.mkdtemp()
cmd = os.path.join(tmpdir, "fake_slskd.cmd")
with open(cmd, "w", newline="\r\n") as f:
    f.write("@echo off\necho [00:00:01 INF] slskd 0.26.0 starting\n"
            "echo An instance of slskd is already running\nexit /b 1\n")

soulseek.subprocess.Popen = _real_popen
soulseek.slskd_exe = lambda: cmd
soulseek.config_path = lambda: os.path.join(tmpdir, "slskd.yaml")
soulseek._proc["proc"] = None
free_port2, close_fp2 = serve(200, b"{}")
close_fp2()
ok, msg = soulseek.start(cfg(free_port2))
assert ok is False, (ok, msg)
assert "already running" in msg, msg
assert "one slskd can run at a time" in msg, msg

# --------------------------------------------------------------------------- #
# 5. the reported symptom: the status endpoint must NOT say "running, not
#    logged in" when the port belongs to another app
# --------------------------------------------------------------------------- #
from server import main as mlo_main  # noqa: E402  (heavy import, only for this)

port_sym, close_sym = serve(401, b"")
conf = cfg(port_sym)
mlo_main.load_config = lambda: conf
soulseek.is_running = lambda: False

st = mlo_main.soulseek_status()
assert st["running"] is False, st
assert st["logged_in"] is None, st
assert "another application's slskd" in (st["conflict"] or ""), st
close_sym()
close()
close2()
close3()

print("soulseek ownership: all assertions passed")
