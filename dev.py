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
8011 up, and if a port is taken the next one up is used.

WHICH LIBRARY is `dev.config.json` beside this file — one key, machine-local,
gitignored, so a clone of the repo gets the scratch default:

    { "music_folder": "F:/Media/Music" }

Unset it is `local/dev/music` (inside the repo, disposable) and `--music <dir>`
overrides it for one run. The app's own state lands beside whichever library is
in play, in its `.mlo`, exactly as it would for a real install. The wizard flag
is seeded through `POST /api/config` (AGENTS.md's own recipe) so the bed opens
on the app rather than the wizard — read first, written only when it differs,
and only for a library of the bed's OWN: for any other library it writes
nothing, because that `.mlo` config belongs to whatever real install uses it.

A library outside `local/` and the temp dir is NOT the bed's own: the banner
says so, because the state above is then shared with whatever else uses that
library (the live install on 8000), and `--fresh` refuses to wipe it. Pointing
the bed at the real library is a legitimate thing to want — silently sharing its
state is not, so for a library it does not own the bed WITHHOLDS the env vars the
app would seed into that config (`MLO_SERVER_HOST` when it differs) and reads
those settings as that library's own install has them — the login gate among
them, so the dev UI can ask you to log in. The app still re-stamps the config's
`music_folder` with this machine's path on every start (mlo.config
`_migrate_to_data_dir`: "keep the live value aligned with the new folder"); the
bed remembers the old value and puts it back when it stops, so the install that
owns the library is left exactly as it was found.

A tray icon (pystray + the desktop shell's own icon; `pip install pystray` if it
is missing, the bed runs in console mode without it) carries the small verbs
you want while testing: open the app, open the library, open the logs, restart,
quit. Ctrl+C quits too, and takes the whole process tree with it.
"""

from __future__ import annotations

import argparse
import atexit
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
CONFIG_PATH = ROOT / "dev.config.json"
DEFAULT_PORT = 8011        # AGENTS.md: 8000 is the owner's live install.
DEFAULT_WEB_PORT = 5181


def load_settings(path: "Path | None" = None) -> dict:
    """The bed's own knobs, from `dev.config.json` beside it:

        { "music_folder": "F:/Media/Music" }

    Machine-local by design — it names a path from THIS machine, so it is
    gitignored — and a missing file is simply the default. `--music` still wins
    over it, so a one-off run somewhere else needs no edit."""
    path = Path(path) if path else CONFIG_PATH
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SystemExit(f"{path.name} could not be read: {exc}")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SystemExit(f"{path.name} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f'{path.name} must be a JSON object, e.g. '
                         '{"music_folder": "F:/Media/Music"}')
    return data


def scratch_roots() -> list[Path]:
    """Where `--fresh` may delete: the repo's own scratch folder and the OS temp
    dir. A library anywhere else is somebody's music — pointing the bed at it is
    fine, wiping it never is."""
    return [ROOT / "local", Path(tempfile.gettempdir()).resolve()]


def is_scratch(path: Path) -> bool:
    resolved = Path(path).resolve()
    return any(resolved == root or root in resolved.parents
               for root in scratch_roots())


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


def ensure_config(port: int, music: Path) -> str:
    """Make the app's answer the bed's, and only when it is not already.

    `first_run_done` alone is not saved (the folder is what the app pins itself
    to), so both go together — but a library the bed is pointed at may be one a
    REAL install is already using (`dev.config.json` can name the live one), and
    rewriting its config on every start of a dev tool is not the bed's business.
    So: read first, write only on a difference. A path that differs in case or
    slash alone is the same folder on Windows."""
    base = f"http://127.0.0.1:{port}"
    current: dict = {}
    try:
        with urllib.request.urlopen(base + "/api/config", timeout=20) as reply:
            loaded = json.loads(reply.read().decode("utf-8"))
            current = loaded if isinstance(loaded, dict) else {}
    except Exception:
        current = {}
    same = (current.get("first_run_done") is True
            and os.path.normcase(str(current.get("music_folder") or ""))
            == os.path.normcase(str(music)))
    if same:
        return "already configured — left alone"
    if not is_scratch(music):
        # A library the bed does not own: the config in its `.mlo` is the one
        # whatever real install uses — the live container on 8000 stores ITS
        # folder in the same file — so writing ours into it is churn with a
        # failure window (a `/music` in a Linux install, a Windows path in the
        # server that reads it). The wizard flag there is that install's
        # business; the bed serves the app and touches nothing.
        return "not configured (a library the bed does not own) — left alone"

    body = json.dumps({"music_folder": str(music), "first_run_done": True}).encode()
    request = urllib.request.Request(
        base + "/api/config", data=body, method="POST",
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
    def __init__(self, args: argparse.Namespace, settings: "dict | None" = None):
        self.args = args
        self.settings = settings or {}
        # Logs always live in the repo's own scratch folder (gitignored); the
        # LIBRARY is `--music`, else `music_folder` in dev.config.json, else
        # that same scratch folder.
        self.home = ROOT / "local" / "dev"
        self.logs = self.home / "logs"
        self.config_path = CONFIG_PATH
        self.music = self._library(args.music)
        self.port = free_port(args.port)
        self.web_port = free_port(args.web_port)
        self.server = Child("server", self.logs / "server.log")
        self.web = Child("web", self.logs / "web.log")
        self.web_started = False
        self.foreign_folder = None
        self.quit = threading.Event()

    def _library(self, override: "str | None") -> Path:
        """`--music` > dev.config.json > local/dev/music. A configured folder
        that is not there is a typo, not a fallback: say so rather than start a
        bed against the wizard's default."""
        if override:
            return Path(override).expanduser().resolve()
        configured = str(self.settings.get("music_folder") or "").strip()
        if not configured:
            return self.home / "music"
        path = Path(os.path.expandvars(configured)).expanduser()
        if not path.is_dir():
            raise SystemExit(
                f"music_folder in {self.config_path.name} is not a folder on this "
                f"machine: {path}\n  fix it there, or run with --music <folder>")
        return path.resolve()

    # -- a library the bed does not own ----------------------------------- #
    def shared_config(self) -> "Path | None":
        """The app's config file for a library the bed does NOT own — the one a
        real install (the container on 8000) reads and writes too. None when the
        library is the bed's own."""
        if is_scratch(self.music):
            return None
        return self.music / ".mlo" / "data" / "config.json"

    def remember_foreign_folder(self) -> None:
        """What that config says its music folder is, BEFORE the app starts.

        The app aligns that value with the folder the run was TOLD
        (`mlo.config._migrate_to_data_dir`: "keep the live value aligned with
        the new folder"), so any run of this bed against a real library rewrites
        the file with a path from THIS machine — measured, on every start. That
        file is shared with the install that owns the library, so the bed puts
        the original back when it stops.

        The value is copied to a marker file FIRST (see `marker_path`): a run
        killed before it could put the file back leaves that marker behind, and
        the next run heals from it instead of reading the leftover as if it were
        the library's own value — which is exactly how one killed run once
        pinned this machine's path into the owner's config for good."""
        self.foreign_folder = None
        config = self.shared_config()
        if config is None or not config.is_file():
            return
        self.heal_leftover(config)
        try:
            value = json.loads(config.read_text(encoding="utf-8")).get("music_folder")
        except Exception:
            return
        if not value:
            return
        if os.path.normcase(str(value)) == os.path.normcase(str(self.music)):
            # Already this machine's path. Either the install that owns the
            # library runs on this very folder (the app's stamp writes the same
            # bytes — there is nothing to undo), or a bed run older than the
            # marker above was killed with the value still stamped. Only the
            # second case is damage, and its original value is not recorded
            # anywhere, so it is said out loud rather than guessed at: writing
            # the leftover back would keep this machine's path in a config that
            # a container reads.
            print(f"! {config} already reads this machine's path {str(value)!r} — "
                  f"either that library's own install runs here (nothing to put "
                  f"back) or an older bed run was killed before it could. It will "
                  f"be left as it is; set music_folder by hand if it should point "
                  f"somewhere else", flush=True)
            return
        self.foreign_folder = value
        try:
            self.marker_path(config).write_text(
                json.dumps({"value": value, "stamped": str(self.music)}),
                encoding="utf-8")
        except OSError as exc:                # no marker is survivable: the
            print(f"! cannot write the restore marker ({exc}) — a killed run "
                  f"would leave the stamped path behind", flush=True)

    @staticmethod
    def marker_path(config: Path) -> Path:
        """Where a run records what it is about to overwrite. Hidden beside the
        config, and NOT the same name the atomic write uses for its temp file:
        an interrupted run leaves this file, and `heal_leftover` finds it."""
        return config.with_name(config.name + ".devbed-restore")

    def heal_leftover(self, config: Path) -> None:
        """Undo what a KILLED bed run left in a shared config.

        A run writes the marker before it lets the app start, so a marker that
        is still there means that run never reached its restore: the value in
        the file may be this machine's path where the library's own install had
        something else. The marker is believed only about the exact value IT
        stamped — a value anything else has written since is left alone."""
        marker = self.marker_path(config)
        if not marker.is_file():
            return
        try:
            saved = json.loads(marker.read_text(encoding="utf-8"))
        except Exception:
            marker.unlink(missing_ok=True)     # unusable: nothing to heal from
            return
        stamped = str(saved.get("stamped") or "")
        value = saved.get("value")
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
            if stamped and value is not None and os.path.normcase(
                    str(data.get("music_folder") or "")) == os.path.normcase(stamped):
                data["music_folder"] = value
                self.write_config(config, data)
                print(f"! an earlier bed run was killed before it could put "
                      f"{config} back — music_folder is {value!r} again", flush=True)
            marker.unlink(missing_ok=True)
        except OSError as exc:                 # keep the marker: heal next time
            print(f"! could not put {config} back from {marker.name} ({exc})",
                  flush=True)

    @staticmethod
    def write_config(config: Path, data: dict) -> None:
        """Replace the config atomically, the way the app itself does: a reader
        (the container's own install) never sees half a file."""
        temp = config.with_name(config.name + ".devbed")
        temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        os.replace(temp, config)

    def restore_foreign_folder(self) -> None:
        """Undo the one write this bed causes in somebody else's config, and
        only that one: the value goes back only while it is still the path this
        run stamped. If the install that owns the library saved in the meantime,
        its value is not ours to touch.

        Retried, and it never raises: this is the LAST thing the bed does, and a
        file another process still holds (Windows sharing violations are
        transient) or a second Ctrl+C must not leave a Windows path in a
        library whose own install is a container."""
        config = self.shared_config()
        if config is None or not self.foreign_folder or not config.is_file():
            return
        for attempt in range(4):
            try:
                data = json.loads(config.read_text(encoding="utf-8"))
                if os.path.normcase(str(data.get("music_folder") or "")) \
                        != os.path.normcase(str(self.music)):
                    # Not ours (or already put back): the marker has nothing
                    # left to protect either way.
                    self.marker_path(config).unlink(missing_ok=True)
                    return
                data["music_folder"] = self.foreign_folder
                self.write_config(config, data)
                self.marker_path(config).unlink(missing_ok=True)
                print(f"· restored music_folder in {config} to "
                      f"{self.foreign_folder!r}", flush=True)
                return
            except KeyboardInterrupt:
                continue                      # still ours to undo: try again
            except OSError as exc:
                if attempt == 3:
                    # The marker stays: the next run heals from it, which is
                    # what it is for.
                    print(f"· COULD NOT put {config} back ({exc}) — it still says "
                          f"{self.music!r}; the next bed run will put it back, and "
                          f"that library's own install rewrites it on its next start",
                          flush=True)
                    return
                time.sleep(0.5)
            except Exception as exc:
                print(f"· COULD NOT put {config} back ({type(exc).__name__}: {exc})",
                      flush=True)
                return

    # -- lifecycle -------------------------------------------------------- #
    def start(self) -> bool:
        self.logs.mkdir(parents=True, exist_ok=True)
        self.music.mkdir(parents=True, exist_ok=True)
        self.remember_foreign_folder()

        env = dict(os.environ)
        env["MLO_MUSIC_FOLDER"] = str(self.music)
        env["PYTHONUNBUFFERED"] = "1"
        if is_scratch(self.music):
            # MLO_SERVER_HOST is what the app SEEDS INTO THE CONFIG when the
            # stored value differs (server/main.py), and MLO_MUSIC_FOLDER is
            # stamped on by any save — both fine for the bed's own library, and
            # both a hostile write to somebody else's: measured, one run against
            # the real library rewrote its `server_host` from 0.0.0.0 to
            # 127.0.0.1 (the login gate's own input) and its music folder, in the
            # very .mlo the live install on 8000 uses. For a library the bed does
            # not own, bind loopback with uvicorn's own `--host` and leave the
            # file alone: the app then reads that library's real settings, gate
            # included, and nothing writes.
            env["MLO_SERVER_HOST"] = "127.0.0.1"

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
        print(f"· {ensure_config(self.port, self.music)}", flush=True)

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
                # Nothing to build while working: vite serves web/src with hot
                # reload and uvicorn reloads server/ and mlo/. Only the BACKEND
                # port serves a build — and a stale one at that — which looks
                # exactly like "my change did not apply, I must need to compile".
                if (ROOT / "web" / "dist" / "index.html").is_file():
                    print(f"  edit web/src and reload that tab; "
                          f"http://127.0.0.1:{self.port} serves the last "
                          f"`npm run build` in web/dist, not your edits",
                          flush=True)
        return True

    def stop(self) -> None:
        if self.server.alive() or self.web.alive():
            print("· stopping…", flush=True)
        self.web.stop()
        self.server.stop()
        # The app writes its config while it STARTS (and on a save). Give the
        # killed tree a moment to be really gone before putting the file back,
        # or a late write lands on top of the restore — measured once: the bed
        # stopped, the restore ran, and the file still read this machine's path.
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and _in_use(self.port):
            time.sleep(0.2)
        self.restore_foreign_folder()

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
        print(f"  config              {self.config_path}")
        if not is_scratch(self.music):
            # Not a refusal — pointing the bed at the real library is a
            # legitimate thing to want — but the app writes its state BESIDE
            # the library it is given, so this is not a private copy of it.
            print()
            print("  ! this library is not the bed's own scratch folder. The state")
            print("    below lives beside it (.mlo: caches, job locks, ratings, wishes,")
            print("    playlists), so anything else using this library — the live")
            print("    install on 8000 — shares it, and an import or a script run from")
            print("    this window edits these real files. The app re-stamps that")
            print("    library's music_folder with this machine's path on every start;")
            print("    the bed puts the old value back when it stops.")
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
def wipe(path: Path) -> None:
    """`--fresh`: a first-run state, and only ever of the bed's own scratch
    folders. The library may now be configured anywhere — including the real
    library a live install is using — so this guard is what keeps a flag meaning
    "throw away the test data" from deleting a music collection."""
    resolved = Path(path).resolve()
    if not is_scratch(resolved):
        raise SystemExit(
            f"refusing --fresh outside the dev bed: {resolved}\n"
            f"  --fresh only wipes {ROOT / 'local'} and {tempfile.gettempdir()}"
            f" — delete a real library by hand if that is what you mean")
    if resolved.exists():
        shutil.rmtree(resolved, ignore_errors=True)
        print(f"· wiped {resolved}", flush=True)


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run la musica against a library of its own, with hot "
                    "reload on both sides and a tray to drive it. The library "
                    "comes from dev.config.json beside this file (or --music).")
    parser.add_argument("--music", help="the library the bed uses (default: "
                        "music_folder in dev.config.json, else local/dev/music)")
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
                        help="wipe the bed's own scratch library first (refused "
                             "outside local/ and the temp dir)")
    args = parser.parse_args()

    bed = DevBed(args, load_settings())
    if args.fresh:
        wipe(bed.music)
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
    try:
        bed.run_console()
    finally:
        # The restore is the only thing that undoes the write this run caused in
        # a config it does not own, so nothing may skip it: not a second Ctrl+C
        # (the console hands one to every process in the group, and it can land
        # inside the stop itself) and not an exception on the way out.
        atexit.register(bed.restore_foreign_folder)
        while True:
            try:
                bed.quit_now()
                bed.stop()
                break
            except KeyboardInterrupt:
                continue
    return 0


if __name__ == "__main__":
    sys.exit(main())
