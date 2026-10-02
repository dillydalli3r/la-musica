"""A one-key dev bed for la musica on this machine — `python dev.py`.

What it is for: running the app you are EDITING, against a throwaway library,
without touching the owner's live install. It starts the same two things the
app is made of and then stays out of the way:

  * the backend — `uvicorn server.main:app --reload`, watching `server/` and
    `mlo/` only, so a web build (or a test writing into `local/`) cannot
    restart it;
  * the UI — `npm run dev` in `web/`, whose vite server proxies `/api` and
    `/ws` to that backend, so frontend edits hot-reload in the browser.

Both ports come from AGENTS.md's rule: never 8000 (that is the owner's live
container, and a dev bed answering there would look like the real library),
8011 up, and if a port is taken the next one up is used. The library is
`local/dev/music` by default — inside the repo, gitignored, disposable — and
the app's own state lands beside it in `local/dev/music/.mlo`, exactly as it
would for a real install. `first_run_done` is seeded through `POST /api/config`
(AGENTS.md's own recipe) so the bed opens on the app rather than the wizard.

A tray icon (pystray + the desktop shell's own icon; `pip install pystray` if it
is missing, the bed runs in console mode without it) carries the small verbs
you want while testing: open the app, open the dev library, open the logs,
restart, quit. Ctrl+C quits too.

Testing a feature against REAL files: copy an album in with
`cp -r "<album>" local/dev/music/Artists/`, or point the whole bed at a folder
of your choosing with `--music`. The bed never writes outside that folder and
`local/`, and `--fresh` refuses to wipe anything that is not under them.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_PORT = 8011        # AGENTS.md: 8000 is the owner's live install.
DEFAULT_WEB_PORT = 5181


# --------------------------------------------------------------------------- #
# Ports, readiness
# --------------------------------------------------------------------------- #
def _in_use(port: int) -> bool:
    """Is something already answering on this loopback port?

    Two probes, because neither alone is enough on Windows: a CONNECT answers
    "something is listening here" — and a bind probe answers "the port is
    reserved" — while the bind alone would call a live port free, since
    Windows' SO_REUSEADDR lets a second socket bind an address another process
    is already listening on. That is how a dev bed ends up seeding the config
    of whatever was already on the port instead of its own server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def free_port(preferred: int) -> int:
    """`preferred`, or the next free port above it — never 8000."""
    for port in range(preferred, preferred + 40):
        if port == 8000:
            continue
        if not _in_use(port):
            return port
    raise SystemExit(f"no free port between {preferred} and {preferred + 40}")


def wait_http(url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as reply:
                if reply.status < 500:
                    return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def seed_config(port: int, music: Path) -> str:
    """Flip the wizard off for this bed — `first_run_done` alone is not saved
    (the folder is what the app pins itself to), so both go together."""
    body = json.dumps({"music_folder": str(music), "first_run_done": True}).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/config", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as reply:
            return f"seeded ({reply.status})"
    except urllib.error.HTTPError as exc:
        return f"NOT seeded: {exc.code} {exc.read().decode('utf-8', 'replace')[:200]}"
    except Exception as exc:
        return f"NOT seeded: {type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# Processes
# --------------------------------------------------------------------------- #
class Child:
    """One supervised process, its output tee'd to the console (prefixed, so
    two interleaved logs stay readable) and to a log file."""

    def __init__(self, name: str, log: Path):
        self.name = name
        self.log_path = log
        self.proc: subprocess.Popen | None = None

    def start(self, cmd: list[str], cwd: Path, env: dict) -> None:
        log = open(self.log_path, "w", encoding="utf-8", errors="replace")
        log.write(f"$ {' '.join(cmd)}\n\n")
        log.flush()
        self.proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
            # A group of its own: the tree, not just the leader, has to die
            # with the bed (see stop()).
            start_new_session=(os.name != "nt"),
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0))
        threading.Thread(target=self._pump, args=(log,), daemon=True).start()

    def _pump(self, log) -> None:
        try:
            for line in self.proc.stdout:
                # The log first: if the console is gone (piped into `head`, or
                # the window closed) the log still has the whole run.
                log.write(line)
                log.flush()
                try:
                    print(f"[{self.name}] {line.rstrip()}", flush=True)
                except OSError:
                    pass
        finally:
            log.close()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        # The TREE: `uvicorn --reload` and `npm run dev` each put a child in
        # front of the real server, and killing the parent alone leaves the
        # port held by the child.
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


class DevBed:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        # Logs and the default library live in the repo's own scratch folder
        # (gitignored); `--music` replaces only the library.
        self.home = ROOT / "local" / "dev"
        self.music = Path(args.music).resolve() if args.music \
            else (self.home / "music")
        self.logs = self.home / "logs"
        self.port = free_port(args.port)
        self.web_port = free_port(args.web_port)
        self.server = Child("server", self.logs / "server.log")
        self.web = Child("web", self.logs / "web.log")
        self.web_started = False
        self.quit = threading.Event()

    # -- lifecycle -------------------------------------------------------- #
    def start(self) -> bool:
        self.logs.mkdir(parents=True, exist_ok=True)
        self.music.mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        env["MLO_MUSIC_FOLDER"] = str(self.music)
        env["MLO_SERVER_HOST"] = "127.0.0.1"
        env["PYTHONUNBUFFERED"] = "1"

        cmd = [sys.executable, "-m", "uvicorn", "server.main:app",
               "--host", "127.0.0.1", "--port", str(self.port)]
        if not self.args.no_reload:
            # Scoped on purpose: watching the repo root would restart the
            # backend on every web build and every scratch file.
            cmd += ["--reload", "--reload-dir", "server", "--reload-dir", "mlo"]
        self.server.start(cmd, ROOT, env)
        print(f"· backend starting on http://127.0.0.1:{self.port} "
              f"(library {self.music})", flush=True)
        if not wait_http(f"http://127.0.0.1:{self.port}/api/health", 120):
            print(f"· the backend never answered /api/health — see "
                  f"{self.server.log_path}", flush=True)
            return False
        print(f"· {seed_config(self.port, self.music)}", flush=True)

        if not self.args.no_web:
            npm = shutil.which("npm")
            if not npm:
                print("· npm not on PATH — the backend serves whatever build "
                      "web/dist holds (`cd web && npm run build`)", flush=True)
            else:
                web_env = dict(env)
                web_env["MLO_API_TARGET"] = f"http://127.0.0.1:{self.port}"
                web_env["MLO_WEB_PORT"] = str(self.web_port)
                # cmd.exe on Windows: npm is a .cmd, which CreateProcess
                # cannot start on its own.
                # `--host 127.0.0.1`: vite's default ("localhost") resolves to
                # ::1 on Windows and then the IPv4 URL printed below — the one
                # the tray and the browser are handed — is refused.
                web_cmd = [npm, "run", "dev", "--", "--host", "127.0.0.1"]
                if os.name == "nt":
                    web_cmd = ["cmd.exe", "/c", *web_cmd]
                self.web.start(web_cmd, ROOT / "web", web_env)
                self.web_started = True
                print(f"· UI (vite, hot reload) starting on "
                      f"http://127.0.0.1:{self.web_port}", flush=True)
        return True

    def stop(self) -> None:
        if self.server.alive() or self.web.alive():
            print("· stopping…", flush=True)
        self.web.stop()
        self.server.stop()

    def restart(self) -> None:
        self.stop()
        self.quit.clear()
        self.start()

    def url(self) -> str:
        # The vite URL only while vite is the thing serving the UI: with
        # `--no-web`, or when npm is missing, the backend's own build is it.
        return f"http://127.0.0.1:{self.web_port}" if self.web_started \
            else f"http://127.0.0.1:{self.port}"

    def open_app(self, *_) -> None:
        webbrowser.open(self.url())

    def open_folder(self, *_) -> None:
        self._reveal(self.music)

    def open_logs(self, *_) -> None:
        self._reveal(self.logs)

    @staticmethod
    def _reveal(path: Path) -> None:
        if os.name == "nt":
            os.startfile(path)                      # noqa: S606 - explorer
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    # -- surfaces --------------------------------------------------------- #
    def banner(self) -> None:
        print()
        print(f"  la musica dev bed   {self.url()}")
        print(f"  library             {self.music}")
        print(f"  logs                {self.logs}")
        print("  the tray has open / logs / restart / quit; Ctrl+C quits too")
        print()

    def run_console(self) -> None:
        try:
            while not self.quit.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass

    def run_tray(self) -> bool:
        """The tray, or False if pystray is not usable here."""
        try:
            import pystray
        except ImportError:
            print("· pystray not installed (`pip install pystray`) — "
                  "console mode", flush=True)
            return False
        # The Win32 backend runs its own message loop, so a thread is fine
        # there; AppKit wants the main thread, so macOS keeps the console.
        if sys.platform == "darwin":
            return False
        try:
            image = self._icon_image()
            menu = pystray.Menu(
                pystray.MenuItem("Open la musica", self.open_app, default=True),
                pystray.MenuItem("Open dev library", self.open_folder),
                pystray.MenuItem("Open logs", self.open_logs),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Restart the app", lambda *_: self.restart()),
                pystray.MenuItem("Quit", lambda *_: self.quit_now()),
            )
            self.icon = pystray.Icon("la-musica-dev", image,
                                     f"la musica dev — {self.url()}", menu)
            threading.Thread(target=self.icon.run, daemon=True).start()
            return True
        except Exception as exc:                    # no tray here: keep going
            print(f"· tray unavailable ({type(exc).__name__}: {exc}) — "
                  "console mode", flush=True)
            return False

    def quit_now(self) -> None:
        self.quit.set()
        icon = getattr(self, "icon", None)
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

    @staticmethod
    def _icon_image():
        """The desktop shell's own icon, so the tray looks like the app; a
        drawn disc when this is a checkout without it."""
        from PIL import Image, ImageDraw
        icons = ROOT / "desktop" / "src-tauri" / "icons"
        for name in ("128x128.png", "icon.png", "32x32.png"):
            if (icons / name).exists():
                return Image.open(icons / name)
        image = Image.new("RGB", (64, 64), (24, 24, 27))
        draw = ImageDraw.Draw(image)
        draw.ellipse((8, 8, 56, 56), fill=(16, 185, 129))
        draw.ellipse((26, 26, 38, 38), fill=(24, 24, 27))
        return image


# --------------------------------------------------------------------------- #
# Wiping
# --------------------------------------------------------------------------- #
def wipe(path: Path, home: Path, music: Path) -> None:
    """`--fresh`: a first-run state, but only inside the dev bed's own folders
    — a typed path that is not under them is refused, never deleted."""
    resolved = path.resolve()
    allowed = [home, music, Path(tempfile.gettempdir()).resolve()]
    if not any(resolved == root or root in resolved.parents for root in allowed):
        raise SystemExit(f"refusing --fresh outside the dev bed: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved, ignore_errors=True)
        print(f"· wiped {resolved}", flush=True)


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run la musica against a throwaway library, with hot "
                    "reload on both sides and a tray to drive it.")
    parser.add_argument("--music", help="the library the bed uses "
                        "(default: local/dev/music)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"backend port, 8011 up (default: {DEFAULT_PORT})")
    parser.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT,
                        help=f"vite port (default: {DEFAULT_WEB_PORT})")
    parser.add_argument("--no-web", action="store_true",
                        help="backend only, serving the built UI in web/dist")
    parser.add_argument("--no-reload", action="store_true",
                        help="no uvicorn --reload")
    parser.add_argument("--no-tray", action="store_true",
                        help="console only (what an agent or CI wants)")
    parser.add_argument("--no-open", action="store_true",
                        help="do not open a browser window")
    parser.add_argument("--fresh", action="store_true",
                        help="wipe the dev library and its state first")
    args = parser.parse_args()

    bed = DevBed(args)
    if args.fresh:
        wipe(bed.music, bed.home, bed.music)
    if not bed.start():
        # Nothing to drive: say why (already printed) and leave the port free
        # rather than sitting on a console that has no app behind it.
        bed.stop()
        return 1
    bed.banner()
    if not args.no_open:
        threading.Timer(1.0, bed.open_app).start()
    if args.no_tray or not bed.run_tray():
        print("· console mode", flush=True)
    bed.run_console()
    bed.quit_now()
    bed.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
