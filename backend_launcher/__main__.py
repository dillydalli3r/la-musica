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


def main() -> int:
    """Boot the app the way the shell expects, then run uvicorn forever."""
    os.makedirs(_DATA_DIR, exist_ok=True)
    _ensure_std_streams(os.path.join(_DATA_DIR, "mlo-server.log"))

    # The engine's own home, BEFORE anything imports mlo: config.json, the
    # legacy .dependencies fallback and the app's writable scratch all live in
    # the per-user dir, never next to a read-only bundle.
    mlo = __import__("mlo.paths", fromlist=["SCRIPT_DIR"])
    mlo.SCRIPT_DIR = _DATA_DIR
    mlo.CONFIG_FILE = os.path.join(_DATA_DIR, "config.json")
    mlo.REPO_CONFIG_FILE = mlo.CONFIG_FILE
    mlo.DEPS_DIR = os.path.join(_DATA_DIR, ".dependencies")

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
    raise SystemExit(main())