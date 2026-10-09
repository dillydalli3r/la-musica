#!/usr/bin/env python3
"""Stage the desktop shell's local backend into desktop/bundle.

The desktop build (`npm run build` in desktop/) spawns the app's own backend
from the frozen PyInstaller build produced by pyinstaller/mlo-server.spec, and
`tauri build` packs `desktop/bundle` into the installer as Tauri RESOURCES —
which is how the installed app finds `<resource_dir>/mlo-server`. This script
is what fills that folder before a build:

    python -m PyInstaller pyinstaller/mlo-server.spec   # -> dist/mlo-server
    python tools/stage_desktop_bundle.py                # -> desktop/bundle

It copies exactly one tree: `dist/mlo-server/`, the onedir backend. The SPA
the webview loads (`_internal/web/dist`) and the native analysis helper
(`_internal/mlo-audio`) travel INSIDE that backend — the spec packs both, the
launcher names both from `_MEIPASS` — so there is nothing else to stage and no
second copy to drift.

Source root is the repo default; override with MLO_SERVER_DIST. Exit 2 (the
suite convention) when the frozen backend is missing what the install needs, so
a desktop build cannot silently ship a shell with no backend (or a backend with
no UI).
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "desktop" / "bundle"


def tree_bytes(path: Path) -> int:
    """The bytes a source actually carries — `Path.stat()` on a directory is
    its own entry, not its contents."""
    if path.is_file():
        return path.stat().st_size
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


def _real_7zip_dir(base) -> str | None:
    """*base* when it holds a usable 7z.exe, else None.

    "Usable" is a size floor: a `7z.exe` that is a WindowsApps execution alias
    (a 0-byte reparse point the Store installs) is on PATH ahead of the real
    one, opens as a file yet cannot be read, and would otherwise be picked as
    the source and fail the copy with EINVAL.
    """
    if not base:
        return None
    exe = os.path.join(base, "7z.exe")
    try:
        if os.path.isfile(exe) and os.path.getsize(exe) > 1:
            return base
    except OSError:
        return None
    return None


def stage_7zip(bundle: Path) -> str:
    """Copy 7-Zip's console exe + dll beside the frozen backend.

    The Windows installer's libjpeg-turbo asset is an NSIS executable upstream
    ships no zip of, and NSIS is read by 7-Zip alone; the app also reads a
    user's `.7z`/`.rar` through it. A machine with no 7-Zip on PATH and none
    under Program Files — most Windows boxes — otherwise cannot install that
    tool at all, or open those archives, so the app carries its own copy.
    `mlo.archives.find_7z` looks in the frozen tree (`sys._MEIPASS`, here) and
    prefers it over PATH.

    Windows only: macOS installs libjpeg-turbo via Homebrew and Linux via its
    `.deb`, and neither needs 7-Zip at install time. Source: MLO_7Z_DIR, then a
    `7z` on PATH, then the usual install directory. Returns what happened, for
    the one caller to print — never raises, because a bundle without it still
    works for every tool except this one.
    """
    if os.name != "nt":
        return "not needed off Windows"
    which = shutil.which("7z")
    src = (_real_7zip_dir(os.environ.get("MLO_7Z_DIR"))
           or _real_7zip_dir(os.path.dirname(which) if which else None)
           or _real_7zip_dir(r"C:\Program Files\7-Zip")
           or _real_7zip_dir(r"C:\Program Files (x86)\7-Zip"))
    if src is None:
        return "ABSENT — Windows libjpeg-turbo installs will refuse (install 7-Zip or set MLO_7Z_DIR)"
    dest = bundle / "_internal"
    if not dest.is_dir():
        dest = bundle
    copied = []
    for name in ("7z.exe", "7z.dll", "License.txt"):
        here = os.path.join(src, name)
        if os.path.isfile(here):
            # Read/write rather than shutil.copy: CopyFile2 (what copy2 uses on
            # Windows) refuses some Program Files sources with WinError 1920,
            # and there is no metadata worth preserving in these two files.
            with open(here, "rb") as fh:
                data = fh.read()
            with open(dest / name, "wb") as fh:
                fh.write(data)
            copied.append(name)
    return f"copied {', '.join(copied)} (from {src})"


def unavailable_runners(exe: Path) -> tuple[int, str]:
    """What the frozen backend CANNOT run — asked of the runtime itself.

    The script tables name a runner's module as a string and resolve it with
    `__import__` (server/script_runners.RUNNERS, mlo/cli.build_script_runners),
    and PyInstaller's analysis finds an import only where one is written. A
    module reached only that way is therefore absent from the build with nothing
    in the tree's file listing to show it: the backend starts, the menu offers
    the script, and /api/run answers "runner N not available". `mlo.taghygiene`
    (script 23) shipped exactly that. The exe is the only thing that knows what
    it carries, so it is asked — `--mlo-python -c` is its own interpreter entry
    point, and the answer is an exit code plus stderr, because a frozen build
    has no console to print to.
    """
    code = (
        "import server.script_runners as s, sys\n"
        "bad = {i: n for i, (n, r) in s.RUNNERS.items() if r is None}\n"
        "sys.stderr.write('missing runners: ' + repr(bad))\n"
        "sys.exit(1 if bad else 0)\n"
    )
    proc = subprocess.run([str(exe), "--mlo-python", "-c", code],
                          capture_output=True, text=True, timeout=600)
    detail = (proc.stderr or proc.stdout or "").strip()
    return proc.returncode, detail or f"exit {proc.returncode}"


def main() -> int:
    server = Path(os.environ.get("MLO_SERVER_DIST") or (ROOT / "dist" / "mlo-server"))

    def missing(why: str) -> int:
        # Exit 2 (the suite convention): a desktop build must not silently ship
        # a shell with no backend, or a backend with no UI. This used to print
        # and then crash inside copytree, which read as a traceback rather than
        # as the one missing file — and it ran on with a half-staged bundle.
        print(f"stage-desktop-bundle: {why}")
        return 2

    if not server.is_dir():
        return missing("missing dist/mlo-server (run `python -m PyInstaller pyinstaller/mlo-server.spec` first)")
    exe = next((server / n for n in ("mlo-server.exe", "mlo-server") if (server / n).is_file()), None)
    if exe is None:
        return missing(f"missing {server.name}/mlo-server(.exe) — that folder is not a frozen backend")
    # The SPA and the helper ride INSIDE the frozen backend (the spec packs
    # them, the launcher names them from _MEIPASS). A backend built before
    # `web/dist` existed would install a shell that serves no UI at all, which
    # is invisible until someone opens the app.
    spa = next((p for p in (server / "_internal" / "web" / "dist" / "index.html",
                            server / "web" / "dist" / "index.html") if p.is_file()), None)
    if spa is None:
        return missing(f"{server.name} carries no web/dist — rebuild it after `cd web && npm run build`")

    # A backend that boots and serves the SPA can still be missing a runner's
    # module (see unavailable_runners): the shell offers the button and the
    # press comes back 400. Nothing about the tree says so, so the exe is asked
    # before anything is staged — a bad tree must fail the BUILD, not the
    # install, and must not wipe the bundle that is already there.
    code, detail = unavailable_runners(exe)
    if code != 0:
        return missing(f"{server.name} cannot run a script it offers: {detail} "
                       "(rerun `python -m PyInstaller pyinstaller/mlo-server.spec`)")

    # The bundle folder is OURS: wipe it and lay down the one tree. A leftover
    # from an earlier layout (the separate web-dist/mlo-audio this script used
    # to stage) would otherwise sit there for ever, unreferenced and unpacked.
    if BUNDLE.exists():
        shutil.rmtree(BUNDLE)
    shutil.copytree(server, BUNDLE / "mlo-server")

    helper = next((p for base in (server / "_internal", server)
                   for p in base.glob("mlo-audio*") if p.is_file()), None)
    sevenzip = stage_7zip(BUNDLE / "mlo-server")
    print(f"staged: desktop/bundle/mlo-server ({tree_bytes(server) >> 20} MiB, "
          f"helper {'included' if helper else 'ABSENT — dynamic range falls back to numpy'}, "
          f"7-Zip {sevenzip})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
