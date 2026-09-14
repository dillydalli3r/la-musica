#!/usr/bin/env python3
"""la musica system-tray app (Windows taskbar).

Shows a tray icon while the backend runs and provides:
  * Open app (browser)
  * Restart backend
  * Auto-start on login (Windows registry Run key, HKCU — no admin needed)
  * Stop backend + Exit

Launched by "Start Music Library Optimizer.bat". If pystray is missing it
degrades to the plain launcher (start backend + open browser + exit).
"""
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
except ImportError:
    HAVE_TRAY = False


def port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


class Backend:
    """Owns the uvicorn child process (when started by us)."""

    def __init__(self):
        self.proc = None

    def ensure_running(self):
        if port_open(PORT):
            return "already-running"
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        exe = sys.executable
        if os.name == "nt" and exe.lower().endswith("python.exe"):
            # never give the backend a console of its own
            pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.isfile(pythonw):
                exe = pythonw
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
            if port_open(PORT):
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
    launcher that set MLO_ALLOW_SHUTDOWN=1)."""
    try:
        import urllib.request
        req = urllib.request.Request(f"{URL}/api/shutdown", method="POST", data=b"")
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except Exception:
        return False


def _kill_port_listener(port=None):
    """Force-kill whatever process is LISTENING on the backend port."""
    if os.name != "nt":
        return False
    port = port or PORT
    killed = False
    try:
        out = subprocess.run(["netstat", "-aon"], capture_output=True,
                             text=True, timeout=15).stdout or ""
        suffix = f":{port}"
        for line in out.splitlines():
            if "LISTENING" not in line:
                continue
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(suffix):
                pid = parts[-1]
                if pid.isdigit() and int(pid) != os.getpid():
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                   capture_output=True, timeout=15)
                    killed = True
    except Exception:
        pass
    return killed


def stop_any_backend():
    """Stop our child if we have one, then any adopted/orphaned backend:
    graceful shutdown endpoint first, force-kill as the last resort."""
    stopped = backend.stop()
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
AUTOSTART_NAME = "MusicLibraryOptimizer"


def _autostart_command():
    exe = shutil.which("pythonw") or sys.executable
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
            winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, _autostart_command())
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
    webbrowser.open(URL)


def on_restart(icon, item):
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


def run_tray():
    if not _acquire_single_instance():
        # A tray instance already manages the backend — just open the app.
        webbrowser.open(URL)
        return
    status = backend.ensure_running()
    if status == "failed":
        # no console on pythonw — fall back to a spawned console for errors
        try:
            subprocess.Popen(["cmd", "/c", "echo Backend failed to start & pause"])
        except Exception:
            pass
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
    webbrowser.open(URL)
    if status == "failed":
        print("Backend failed to start — run `python -m uvicorn server.main:app` "
              "to see errors.")


def _relaunch_detached():
    """When started with console python (double-click on tray.py), re-exec
    via pythonw so no terminal window stays open while the tray runs."""
    if os.name != "nt" or not sys.executable.lower().endswith("python.exe"):
        return False
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(pythonw):
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