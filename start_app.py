#!/usr/bin/env python3
"""Launch la musica.

Starts the FastAPI backend (if not already running), waits for it to come
up, then opens the app in your browser. Double-click "Start la musica.bat"
(Windows) or run `python start_app.py`.
"""
import os
import socket
import subprocess
import sys
import time
import webbrowser

PORT = 8000
URL = f"http://127.0.0.1:{PORT}"
ROOT = os.path.dirname(os.path.abspath(__file__))


def port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _prompt_close():
    try:
        input("Press Enter to close...")
    except (EOFError, KeyboardInterrupt):
        pass


def _backend_ours():
    """True when something on PORT answers like our API (not a stray app)."""
    import urllib.request
    try:
        with urllib.request.urlopen(URL + "/api/health", timeout=2) as r:
            return r.status == 200
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

    if port_open(PORT) and _backend_ours():
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
                 "--host", "127.0.0.1", "--port", str(PORT)],
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
            if port_open(PORT):
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