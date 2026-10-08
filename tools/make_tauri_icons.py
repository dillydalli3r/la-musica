#!/usr/bin/env python3
"""Regenerate every la musica icon set from ``desktop/icon-source.png``.

Tauri's own ``icon`` command is the only thing in this repo that draws an app
icon — it writes the desktop set (png/ico/icns) into
``desktop/src-tauri/icons/``. (``tools/make_icons.py`` afterwards only sorts
the ICNS element blocks the CLI writes out of a ``HashMap``, so a rerun is a
no-op; that moves no pixel.)

The command also draws the mobile sets (``icons/ios/**``, ``icons/android/**``)
from the same source; this repo ships no mobile build any more, so those
directories are removed straight after the CLI writes them.

Run it on its own, or as part of ``tools/make_icons.py``.
"""
import os
import shutil
import subprocess
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "desktop"
ICONS = DESKTOP / "src-tauri" / "icons"

code = subprocess.call(
    # --no-install: use the CLI pinned in desktop/package-lock.json
    # (`npm install` in desktop/ first) instead of whatever 2.x npm is
    # serving today, so the icons stay reproducible.
    ["npx", "--no-install", "tauri", "icon", "icon-source.png"],
    cwd=DESKTOP,
    # npx is a .cmd shim on Windows, which CreateProcess cannot run itself.
    shell=os.name == "nt",
)
if code:
    sys.exit(code)

# The CLI writes the mobile sets from the same source too; nothing in this
# repo consumes them, so drop them rather than commit them.
for name in ("ios", "android"):
    shutil.rmtree(ICONS / name, ignore_errors=True)