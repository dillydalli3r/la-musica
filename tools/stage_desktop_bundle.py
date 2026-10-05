#!/usr/bin/env python3
"""Stage the desktop shell's local backend into desktop/bundle.

The desktop build (`npm run build` in desktop/) serves the SAME React app and
spawns the app's own backend (`mlo-server`) from the frozen PyInstaller build
produced by pyinstaller/mlo-server.spec. `tauri build` bundles `desktop/bundle`
next to the shell binary; this script is what fills it before a build:

    python -m PyInstaller pyinstaller/mlo-server.spec   # -> dist/mlo-server
    python tools/stage_desktop_bundle.py                # -> desktop/bundle

It copies:
  * dist/mlo-server/            — the frozen backend (onedir)
  * web/dist/                   — the built SPA (served same-origin by the
                                   backend; NOT read from desktop/ at runtime)
  * rust/target/release/mlo-audio(.exe) — the native analysis helper, found
                                   via MLO_AUDIO_BIN next to the backend

Source roots are the repo defaults; override with MLO_SERVER_DIST, WEB_DIST,
MLO_AUDIO_BIN. Exit 2 (the suite convention) when a required source is missing,
so a build cannot silently ship a desktop install with no backend.
"""
import os
import shutil
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


def main() -> int:
    server = Path(os.environ.get("MLO_SERVER_DIST") or (ROOT / "dist" / "mlo-server"))
    web = Path(os.environ.get("WEB_DIST") or (ROOT / "web" / "dist"))
    audio = Path(os.environ.get("MLO_AUDIO_BIN") or (
        ROOT / "rust" / "target" / "release" / ("mlo-audio.exe" if os.name == "nt" else "mlo-audio")))

    missing = [p for p, label in ((server, "dist/mlo-server (run pyinstaller/mlo-server.spec first)"),
                                  (web, "web/dist (run `cd web && npm run build`)"),
                                  (audio, "rust/target/release/mlo-audio (run `cargo build --release --manifest-path rust/Cargo.toml`)")) if not p.exists()]
    if missing:
        # Exit 2 (the suite convention): a desktop build must not silently ship
        # an install with no backend. This used to print and then crash inside
        # copytree, which read as a traceback rather than as the one missing
        # file — and it ran on with a half-staged bundle.
        for m in missing:
            print(f"stage-desktop-bundle: missing {m}")
        return 2

    for name, src in (("mlo-server", server), ("web-dist", web), ("mlo-audio", audio)):
        dest = BUNDLE / name
        if dest.is_dir():
            shutil.rmtree(dest)
        elif dest.exists():
            dest.unlink()
        if src.is_dir():
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)

    print(f"staged: desktop/bundle (mlo-server {tree_bytes(server) >> 20} MiB, "
          f"web-dist {tree_bytes(web) >> 20} MiB, mlo-audio {tree_bytes(audio) >> 10} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
