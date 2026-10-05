"""The desktop shell's private backend (PyInstaller onedir target).

This is the process the Tauri shell spawns when "Local backend" is on. Only
desktop packaging builds it; the repo's own server (server.main) is unchanged
and is what Docker and the zip still use.

What changes vs. the web build, and why:

* The SPA is served from THIS server instead of from the Tauri asset loader,
  so the webview origin and the API origin are the SAME server (http://127.0.0.1:<port>).
  That is what makes the HttpOnly ``mlo_session`` cookie work in the shell:
  browser rules drop a cross-site cookie on the loopback requests the old
  (tauri://localhost) build made, so a shell without a token kept 401ing on
  media. Serving the app from the same origin fixes media, playback, covers
  and downloads the way the browser build already has them — the cookie does
  the auth, exactly as the README describes. The ``token`` query path still
  exists for anyone pointed at a remote server.

* App state does NOT live next to the executable. The pip tools the
  Dependencies page installs are the same ``<music>/.mlo/tools`` the Python
  sources install (a tools folder is shared by every install that reads the
  library), but the app's own config and DBs are the real install's, and a
  packaged app's folder is read-only on macOS and shared under Program Files
  on Windows. ``mlo.paths.SCRIPT_DIR`` is therefore redirected to the shell's
  per-user data dir — `%LOCALAPPDATA%/la musica` on Windows,
  `~/Library/Application Support/la musica` on macOS, `~/.local/share/la musica`
  on Linux — before ``server.main`` is imported. The redirect is transparent
  to the engine and to the running server.
"""
import json
import os
import runpy
import sys

from pathlib import Path


def _data_dir() -> str:
    """The per-user folder this install's own state (config.json etc.) lives in."""
    override = os.environ.get("MLO_APP_DATA_DIR")
    if override:
        return os.path.abspath(override)
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "la musica")
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(base, "la musica")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return os.path.join(base, "la musica")


_DATA_DIR = _data_dir()


def _install_script_dir() -> Path:
    """Where the packaged server's files live: the onedir's own ``_internal``."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


_USER = "desktop-shell"  # a stable import marker for test harnesses


def _ensure_std_streams(log_path: str) -> None:
    """A WINDOWED PyInstaller build has no console: `sys.stdout` and
    `sys.stderr` are None. uvicorn's default logging config calls
    `sys.stdout.isatty()` while building its formatters — an AttributeError on
    None, raised from `dictConfig` as "Unable to configure formatter 'default'"
    — which killed the backend at boot and left the tray showing a server that
    never came up. Give both streams a real file instead: the backend's own
    log, in the per-user data dir beside config.json, so a failing start can be
    read afterwards rather than guessed at.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    handle = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
    if sys.stdout is None:
        sys.stdout = handle
    if sys.stderr is None:
        sys.stderr = handle


def _augment_gui_path() -> None:
    """Put the package managers' bins on PATH — macOS only, and it is the
    difference between "install it with Homebrew" working and not working.

    A GUI-launched app inherits the launchd environment, where PATH is
    `/usr/bin:/bin:/usr/sbin:/sbin` — NOT the PATH a login shell builds from
    `path_helper` and the shell profiles. Homebrew lives outside all four
    (`/opt/homebrew/bin` on Apple Silicon, `/usr/local/bin` on Intel,
    `/opt/local/bin` for MacPorts), so without this the app cannot SEE a tool
    the user installed there — detection reads PATH — and even a tool it did
    see could not be SPAWNED by name, because the child inherits this same
    PATH. Same set the user's own shell search uses; only directories that
    exist are added, and only ones not already there.
    """
    if sys.platform != "darwin":
        return
    path = os.environ.get("PATH", "")
    parts = path.split(os.pathsep) if path else []
    extra = [d for d in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin")
             if os.path.isdir(d) and d not in parts]
    if extra:
        os.environ["PATH"] = os.pathsep.join(extra + parts)


def _register_frozen_resource_finders() -> None:
    """Teach pip's vendored distlib about PyInstaller's module loader.

    distlib maps a package's loader TYPE to a resource finder, and it knows
    only CPython's own loaders. A frozen module's loader is PyInstaller's, so
    `pip install` aborts at import — `pip._vendor.distlib.scripts` calls
    `finder(DISTLIB_PACKAGE)` at module scope and gets "Unable to locate finder
    for 'pip._vendor.distlib'". The plain file-system finder is the right one:
    the package directories are real files under `sys._MEIPASS` (collect_all
    puts them there), which is all `ResourceFinder` reads. Registering it for
    the loader the import system actually used is what makes the bundled pip
    usable without a second interpreter.
    """
    try:
        from pip._vendor.distlib import resources
    except Exception:
        return
    registry = getattr(resources, "_finder_registry", None)
    register = getattr(resources, "register_finder", None)
    if registry is None or register is None:      # distlib internals changed
        return
    for name in ("pip._vendor.distlib", "pip"):
        module = sys.modules.get(name)
        loader = getattr(module, "__loader__", None) if module is not None else None
        if loader is not None and type(loader) not in registry:
            register(loader, resources.ResourceFinder)


def _run_python(argv) -> int:
    """Run *argv* the way a CPython executable would (see `--mlo-python`).

    A packaged desktop install has no separate interpreter, and the Python
    packages the Dependencies page installs (`pip install --target`, see
    mlo.fetchdeps) and the tools that run as a script (beets, see
    server.beetscfg) need one that is the SAME interpreter this frozen server
    imports with — a downloaded python would pick wheels whose C extensions
    this build cannot load. So the frozen runtime is the interpreter, and this
    is the entry point that behaves like `python`: `-m module`, `-c code` or a
    script path, with `-u` accepted and ignored (there is no buffering to
    change in a console-less build).

    `MLO_PYTHONPATH` names the vendored tools folders to import from. The
    PyInstaller bootloader owns `sys.path` and need not honour `PYTHONPATH`,
    so those entries are spliced in here explicitly rather than trusted to the
    environment.
    """
    for part in reversed([p for p in os.environ.get("MLO_PYTHONPATH", "").split(os.pathsep) if p]):
        if os.path.isdir(part):
            sys.path.insert(0, part)
    if getattr(sys, "frozen", False):
        _register_frozen_resource_finders()
    args = list(argv)
    while args and args[0] == "-u":
        args.pop(0)
    if not args:
        return 2
    if args[0] == "-m":
        if len(args) < 2:
            return 2
        sys.argv = args[1:]
        runpy.run_module(args[1], run_name="__main__", alter_sys=True)
        return 0
    if args[0] == "-c":
        if len(args) < 2:
            return 2
        sys.argv = ["-c", *args[2:]]
        exec(compile(args[1], "<string>", "exec"), {"__name__": "__main__"})
        return 0
    sys.argv = args
    runpy.run_path(args[0], run_name="__main__")
    return 0


def _redirect_engine_home(data_dir: str) -> None:
    """Point the engine's module-level locations at the per-user data dir.

    Run BEFORE anything imports ``mlo``: config.json, the legacy
    ``.dependencies`` fallback and the app's writable scratch all live in the
    per-user dir, never next to a read-only bundle. ``LEGACY_DATA_DIR`` is in
    the list because ``mlo.paths`` derives it from ``SCRIPT_DIR`` at import
    time, and it is where ``app_data_dir()`` falls back to while no music
    folder is configured (auth.db, playlists.db, the beets library on a fresh
    install) — naming only ``SCRIPT_DIR`` wrote that state into the INSTALLED
    tree, which uninstall deletes."""
    mlo = __import__("mlo.paths", fromlist=["SCRIPT_DIR"])
    mlo.SCRIPT_DIR = data_dir
    mlo.CONFIG_FILE = os.path.join(data_dir, "config.json")
    mlo.REPO_CONFIG_FILE = mlo.CONFIG_FILE
    mlo.DEPS_DIR = os.path.join(data_dir, ".dependencies")
    mlo.LEGACY_DATA_DIR = os.path.join(data_dir, "server", "data")


def main() -> int:
    """Boot the app the way the shell expects, then run uvicorn forever."""
    _augment_gui_path()
    os.makedirs(_DATA_DIR, exist_ok=True)
    _ensure_std_streams(os.path.join(_DATA_DIR, "mlo-server.log"))

    _redirect_engine_home(_DATA_DIR)

    # Resolve the tree the bundled sources need. server.main computes ROOT
    # from its own file and inserts it into sys.path, and the static SPA lives
    # beside that same root; the webview is pointed at this server, so the
    # SPA must be served here, not from the Tauri asset loader.
    installed = _install_script_dir()
    root = installed.parent if getattr(sys, "frozen", False) else installed.parent.parent
    for key, name in (("MLO_SERVER_ROOT", str(root)),):
        os.environ.setdefault(key, name)

    # Bind loopback unless the shell overrides (remote debugging on LAN).
    bind_host = os.environ.get("MLO_SERVER_HOST") or "127.0.0.1"
    bind_port = int(os.environ.get("MLO_SERVER_PORT") or "8011")

    # The bundled SPA served same-origin with the API (the cookie's home).
    # server.main honors MLO_WEB_DIST (see its static section); PyInstaller
    # onedir packs web/dist at <root>/web/dist exactly as it exists in the
    # repo, so the default names that, and a bundle built from a checkout with
    # no built frontend (or a test harness) can override it.
    spa_dir = Path(os.environ.get("MLO_WEB_DIST") or os.path.join(str(root), "web", "dist"))
    if spa_dir.is_dir():
        os.environ["MLO_WEB_DIST"] = str(spa_dir)

    # The native dynamic-range helper, packed beside these sources by
    # pyinstaller/mlo-server.spec. mlo.dr probes MLO_AUDIO_BIN first (before
    # its repo-relative dev paths and PATH), so naming it here is what makes
    # the shipped helper the one that runs. Absent, mlo.dr falls back to the
    # numpy block math — the same numbers, slower.
    helper = installed / ("mlo-audio.exe" if os.name == "nt" else "mlo-audio")
    if helper.is_file():
        os.environ.setdefault("MLO_AUDIO_BIN", str(helper))

    import server.main as app_mod
    import uvicorn
    uvicorn.run(app_mod.app, host=bind_host, port=bind_port, log_level="warning")
    return 0


if __name__ == "__main__":
    # `mlo-server --mlo-python …` is the frozen build acting as its own
    # interpreter (see _run_python), and it must NOT touch the server: the
    # state dir, the std-stream log and uvicorn all belong to the server run.
    if len(sys.argv) > 1 and sys.argv[1] == "--mlo-python":
        raise SystemExit(_run_python(sys.argv[2:]))
    raise SystemExit(main())