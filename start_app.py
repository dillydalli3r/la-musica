#!/usr/bin/env python3
"""Launch la musica.

Starts the FastAPI backend (if not already running), waits for it to come
up, then opens the app in your browser. Run `python start_app.py` (or
double-click "Start la musica.bat", which is just a wrapper).

This script is standalone: it sets MLO_ALLOW_SHUTDOWN=1 on the backend it
spawns itself, so the tray / desktop shell can stop that backend later.
"""
import os
import socket
import subprocess
import sys
import time
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))


def _bind():
    """`(host, port)` the backend should bind, from the app's config.

    Read from config.json rather than hardcoded, because `server_host` is also
    what decides whether the login gate applies (server/auth.py): a user who
    sets `server_host` to a LAN address and finds the launcher still binding
    loopback would have configured remote access that never happens — and a
    gate that never turns on.
    """
    host, port = "127.0.0.1", 8000
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from mlo.config import load_config
        cfg = load_config()
        host = str(cfg.get("server_host") or host).strip() or host
        port = int(cfg.get("server_port") or port)
    except Exception:
        pass
    return host, port


def _dialable(host):
    """A concrete address for the health probe and the browser.

    A wildcard bind ("0.0.0.0", "::") answers on every interface and cannot be
    dialled as written, so the probe uses loopback — the backend is always on
    this machine, whatever else it also listens on.
    """
    return "127.0.0.1" if host in ("0.0.0.0", "::", "*", "") else host


HOST, PORT = _bind()
URL = f"http://{_dialable(HOST)}:{PORT}"


def port_open(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def _prompt_close():
    try:
        input("Press Enter to close...")
    except (EOFError, KeyboardInterrupt):
        pass


def _backend_ours():
    """True when something on PORT answers like our API (not a stray app).

    Same probe as tray.backend_ours(): a bare HTTP 200 is not enough - any
    server on :8000 can answer that, and start_app would then declare the
    backend "up" and open a browser onto someone else's app.
    """
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(URL + "/api/health", timeout=2) as r:
            if r.status != 200:
                return False
            return (json.loads(r.read()) or {}).get("status") == "ok"
    except Exception:
        return False


def main():
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        print("Missing Python dependencies.")
        print("Install them with:  python -m pip install -r server/requirements.txt")
        _prompt_close()
        sys.exit(1)

    if _backend_ours():
        print(f"Backend already running — opening {URL}")
    elif port_open(PORT):
        print(f"Port {PORT} is busy (not our backend) — "
              "stop the other app or change port.")
        _prompt_close()
        sys.exit(1)
    else:
        print("Starting backend...")
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        env = dict(os.environ)
        env["MLO_ALLOW_SHUTDOWN"] = "1"  # lets the tray / desktop shell stop it
        try:
            subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "server.main:app",
                 "--host", HOST, "--port", str(PORT)],
                cwd=ROOT,
                creationflags=flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        except Exception as e:
            print(f"Failed to start backend: {e}")
            _prompt_close()
            sys.exit(1)
        for _ in range(30):
            time.sleep(1)
            # Only OUR backend counts as up: a foreign app that grabbed :8000
            # during the spawn window would otherwise be reported as ours and
            # opened in the browser.
            if _backend_ours():
                print("Backend is up.")
                break
        else:
            print("Backend did not start in time — check for errors with "
                  "`python -m uvicorn server.main:app`")
            _prompt_close()
            sys.exit(1)

    webbrowser.open(URL)


if __name__ == "__main__":
    main()