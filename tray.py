#!/usr/bin/env python3
"""la musica system-tray app (Windows taskbar).

Shows a tray icon while the backend runs and provides:
  * Open app (browser)
  * Restart backend
  * Auto-start on login (Windows registry Run key, HKCU — no admin needed)
  * Stop backend + Exit

Launched by "Start la musica.bat". If pystray is missing it
degrades to the plain launcher (start backend + open browser + exit).

The tray only ever manages ITS OWN backend: anything listening on the port
that does not answer /api/health as ours is reported as a foreign app and
never opened, shut down or force-killed.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8000
URL = f"http://127.0.0.1:{PORT}"

try:
    import pystray
    from PIL import Image, ImageDraw
    HAVE_TRAY = True
except Exception:
    # Not just ImportError: pystray's import itself starts a backend, and a
    # headless Linux host raises Xlib.error.DisplayNameError (no $DISPLAY).
    # Either way there is no tray, so degrade to the plain launcher instead
    # of dying at import time.
    HAVE_TRAY = False


def port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def backend_ours():
    """True when the thing on PORT answers like OUR API (not a stray app).

    Same ownership probe start_app.py uses: without it the tray adopted any
    listener on :8000 — reporting a running backend, opening the foreign app
    in the browser, and taskkilling it on "Exit (stop backend)".
    """
    import urllib.request
    try:
        with urllib.request.urlopen(URL + "/api/health", timeout=2) as r:
            if r.status != 200:
                return False
            return (json.loads(r.read()) or {}).get("status") == "ok"
    except Exception:
        return False


def backend_state():
    """'down' (nothing listening) | 'ours' | 'foreign' (someone else's app)."""
    if not port_open(PORT):
        return "down"
    return "ours" if backend_ours() else "foreign"


class Backend:
    """Owns the uvicorn child process (when started by us)."""

    def __init__(self):
        self.proc = None

    def ensure_running(self):
        state = backend_state()
        if state == "ours":
            return "already-running"
        if state == "foreign":
            return "foreign"  # never spawn into, adopt or kill someone else's port
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        # never give the backend a console of its own
        exe = _pythonw() or sys.executable
        env = dict(os.environ)
        env["MLO_ALLOW_SHUTDOWN"] = "1"  # lets any launcher stop this backend
        self.proc = subprocess.Popen(
            [exe, "-m", "uvicorn", "server.main:app",
             "--host", "127.0.0.1", "--port", str(PORT)],
            cwd=ROOT,
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        for _ in range(30):
            time.sleep(1)
            if backend_state() == "ours":
                return "started"
        return "failed"

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
            return True
        return False


backend = Backend()


def _request_backend_shutdown(timeout=2.0):
    """Ask a running backend to exit (only works when it was spawned by a
    launcher that set MLO_ALLOW_SHUTDOWN=1).

    Only ever called once backend_state() said the listener is ours."""
    if not backend_ours():
        return False
    try:
        import urllib.request
        req = urllib.request.Request(f"{URL}/api/shutdown", method="POST", data=b"")
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except Exception:
        return False


def _kill_port_listener(port=None):
    """Force-kill whatever process is LISTENING on the backend port.

    Never called for a foreign listener: stop_any_backend() checks ownership
    first, and this re-checks so a caller cannot taskkill a stray app."""
    if os.name != "nt":
        return False
    if not backend_ours():
        return False
    port = port or PORT
    killed = False
    try:
        # CREATE_NO_WINDOW on both: the tray runs windowed, so console tools
        # like netstat/taskkill would otherwise flash a terminal each call.
        _no_win = 0x08000000 if os.name == "nt" else 0
        out = subprocess.run(["netstat", "-aon"], capture_output=True,
                             text=True, timeout=15,
                             creationflags=_no_win).stdout or ""
        suffix = f":{port}"
        for line in out.splitlines():
            if "LISTENING" not in line:
                continue
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(suffix):
                pid = parts[-1]
                if pid.isdigit() and int(pid) != os.getpid():
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                   capture_output=True, timeout=15,
                                   creationflags=_no_win)
                    killed = True
    except Exception:
        pass
    return killed


def stop_any_backend():
    """Stop our child if we have one, then any adopted/orphaned backend:
    graceful shutdown endpoint first, force-kill as the last resort.

    A listener that does not answer /api/health is another app's: it is left
    completely alone (no shutdown request, no taskkill)."""
    stopped = backend.stop()
    if backend_state() == "foreign":
        return stopped
    if port_open(PORT):
        _request_backend_shutdown()
        for _ in range(6):
            time.sleep(0.5)
            if not port_open(PORT):
                return True
    if port_open(PORT):
        _kill_port_listener()
        for _ in range(4):
            time.sleep(0.5)
            if not port_open(PORT):
                return True
    return stopped or not port_open(PORT)


# --------------------------------------------------------------------------- #
# Auto-start on login (Windows HKCU Run key — per-user, no admin required)
# --------------------------------------------------------------------------- #
AUTOSTART_NAME = "la musica"
# This value name used to be "MusicLibraryOptimizer". An install from before the
# rename still has that entry pointing at this same tray.py: the toggle would
# read as off while the app kept starting, and switching it on would add a
# second entry and a second tray fighting for the port. So adopt what the old
# name holds, then drop it.
LEGACY_AUTOSTART_NAME = "MusicLibraryOptimizer"

def migrate_legacy_autostart():
    """Move a pre-rename auto-start entry onto the current value name."""
    if os.name != "nt":
        return
    import winreg
    path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as k:
            try:
                command, kind = winreg.QueryValueEx(k, LEGACY_AUTOSTART_NAME)
            except OSError:
                return
            if not autostart_enabled():
                winreg.SetValueEx(k, AUTOSTART_NAME, 0, kind, command)
            winreg.DeleteValue(k, LEGACY_AUTOSTART_NAME)
    except OSError:
        pass


def _pythonw():
    """pythonw.exe for this interpreter (falls back to PATH), or None."""
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        sibling = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(sibling):
            return sibling
    return shutil.which("pythonw")


def _autostart_command():
    """Login command for tray.py, or None when pythonw is unavailable.

    It MUST be pythonw: sys.executable would put a console window on screen at
    every login (and the tray would then detach into yet another one), so a
    machine without pythonw gets no entry instead of a visible terminal.
    """
    exe = _pythonw()
    if not exe:
        return None
    return f'"{exe}" "{os.path.join(ROOT, "tray.py")}"'


def autostart_enabled():
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            winreg.QueryValueEx(k, AUTOSTART_NAME)
        return True
    except OSError:
        return False


def set_autostart(enabled):
    if os.name != "nt":
        return False
    import winreg
    key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
        if enabled:
            command = _autostart_command()
            if command is None:
                _alert("pythonw.exe not found - refusing to register an "
                       "auto-start entry that would open a console window.")
                return False
            winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(k, AUTOSTART_NAME)
            except FileNotFoundError:
                pass
    return autostart_enabled() == enabled


# --------------------------------------------------------------------------- #
# Tray menu actions
# --------------------------------------------------------------------------- #
def on_open(icon, item):
    if backend_state() == "foreign":
        return  # opening another app's page is not what this icon means
    webbrowser.open(URL)


def on_restart(icon, item):
    if backend_state() == "foreign":
        return  # never stop/restart a listener that is not ours
    stop_any_backend()
    time.sleep(1)
    backend.ensure_running()
    webbrowser.open(URL)


def on_autostart(icon, item):
    enabled = autostart_enabled()
    set_autostart(not enabled)


def on_exit(icon, item):
    stop_any_backend()
    icon.stop()


def _menu():
    items = [
        pystray.MenuItem("Open la musica", on_open, default=True),
        pystray.MenuItem("Restart backend", on_restart),
    ]
    if os.name == "nt":
        items.append(
            pystray.MenuItem(
                "Auto-start on login",
                on_autostart,
                checked=lambda item: autostart_enabled(),
            )
        )
    items.append(pystray.Menu.SEPARATOR)
    items.append(pystray.MenuItem("Exit (stop backend)", on_exit))
    return pystray.Menu(*items)


def make_icon():
    """The app icon (web/public/icon.png — the gato picture, regenerated by
    tools/make_icons.py) center-cropped with rounded corners for the tray,
    falling back to a drawn dark play-tile when the file is unavailable."""
    size = 64
    try:
        from PIL import Image, ImageDraw
        src = Image.open(os.path.join(ROOT, "web", "public", "icon.png")).convert("RGBA")
        side = min(src.size)
        src = src.crop(((src.width - side) // 2, (src.height - side) // 2,
                        (src.width + side) // 2, (src.height + side) // 2))
        img = src.resize((size, size), Image.LANCZOS)
        mask = Image.new("L", (size, size), 0)
        d = ImageDraw.Draw(mask)
        d.rounded_rectangle([0, 0, size - 1, size - 1], radius=14, fill=255)
        img.putalpha(mask)
        return img
    except Exception:
        pass
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, size - 2, size - 2], radius=12, fill=(12, 12, 14, 255))
    cx, cy = size / 2, size / 2
    r = size * 0.34
    d.polygon(
        [(cx - r * 0.62, cy - r), (cx - r * 0.62, cy + r), (cx + r, cy)],
        fill=(255, 255, 255, 255),
    )
    return img


def _lock_path():
    try:
        from mlo.paths import app_data_dir
        return os.path.join(app_data_dir(), "tray.lock")
    except Exception:
        pass
    return os.path.join(ROOT, "server", "data", "tray.lock")


_lock_fh = None


def _acquire_single_instance():
    """True when this process is the only tray instance.

    Uses a real OS lock on the lockfile (msvcrt.locking on Windows,
    flock elsewhere) instead of comparing PIDs: the OS releases the lock the
    moment the owning process dies, so a crashed tray can never block the
    next launch. The old PID-liveness check produced false positives — a
    recycled PID made every future launch believe a tray was already
    running, silently reducing it to "open browser and exit".
    """
    global _lock_fh
    lock_path = _lock_path()  # resolved per call — music folder may change
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fh = open(lock_path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False  # a live tray holds the lock
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        _lock_fh = fh  # keep the handle (and the lock) for this process's life
        return True
    except OSError:
        return False


def _alert(msg):
    """Visible error when there is no console (pythonw has none)."""
    print(msg)
    try:
        subprocess.Popen(["cmd", "/c", f"echo {msg} & pause"])
    except Exception:
        pass


def _run_foreign_tray():
    """Tray for a port owned by another app: report it and manage nothing.

    Deliberately has no Open/Restart/Stop: those would drive or kill a
    process that is not ours."""
    icon = pystray.Icon(
        "la-musica", make_icon(),
        f"la musica - port {PORT} is in use by another app",
        menu=pystray.Menu(
            pystray.MenuItem(f"Port {PORT} belongs to another app",
                             lambda *_: None, enabled=False),
            pystray.MenuItem("Exit", lambda icon, item: icon.stop()),
        ),
    )
    icon.run()


def run_tray():
    migrate_legacy_autostart()
    if not _acquire_single_instance():
        # A tray instance already manages the backend — just open the app.
        webbrowser.open(URL)
        return
    status = backend.ensure_running()
    if status == "foreign":
        _run_foreign_tray()
        return
    if status == "failed":
        _alert("Backend failed to start - run `python -m uvicorn server.main:app` "
               "to see errors.")
        return
    icon = pystray.Icon("la-musica", make_icon(),
                        "la musica — backend running",
                        menu=_menu())
    if status == "started":
        threading.Thread(target=lambda: (time.sleep(2), webbrowser.open(URL)),
                         daemon=True).start()
    icon.run()


def run_plain():
    """Fallback when pystray is unavailable."""
    status = backend.ensure_running()
    if status == "foreign":
        print(f"Port {PORT} is in use by another app (not la musica) — stop that "
              "app or free the port. Nothing was started or stopped.")
        return
    webbrowser.open(URL)
    if status == "failed":
        print("Backend failed to start — run `python -m uvicorn server.main:app` "
              "to see errors.")


def _relaunch_detached():
    """When started with console python (double-click on tray.py), re-exec
    via pythonw so no terminal window stays open while the tray runs."""
    if os.name != "nt" or not sys.executable.lower().endswith("python.exe"):
        return False
    pythonw = _pythonw()
    if not pythonw:
        return False
    subprocess.Popen(
        [pythonw, os.path.abspath(__file__)],
        cwd=ROOT,
        creationflags=0x08000000,  # CREATE_NO_WINDOW
    )
    return True


if __name__ == "__main__":
    if HAVE_TRAY and _relaunch_detached():
        sys.exit(0)
    if HAVE_TRAY:
        run_tray()
    else:
        run_plain()