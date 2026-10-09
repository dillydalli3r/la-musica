#!/usr/bin/env python3
"""Regenerate every la musica icon set from ``desktop/icon-source.png``.

Tauri's own ``icon`` command is the only thing in this repo that draws an app
icon — it writes the desktop set (png/ico/icns) into
``desktop/src-tauri/icons/``, and it writes the native-project sets
(``icons/ios/AppIcon-*.png``, ``icons/android/mipmap-*/``) when the generated
Xcode/Android projects are not there, or straight into ``src-tauri/gen/`` when
they are. Drawing a second set with PIL here is what previously left the
committed icons showing artwork that was not the app's logo, so this script now
just drives that command. (``tools/make_icons.py`` afterwards only sorts the
ICNS element blocks the CLI writes out of a ``HashMap``, so a rerun is a no-op;
that moves no pixel.)

Run it on its own, or as part of a mobile build *after* ``tauri android init``
/ ``tauri ios init``, so the generated project picks the icons up
(``.github/workflows/mobile.yml`` does both).
"""
import os
import subprocess
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "desktop"

sys.exit(
    subprocess.call(
        # --no-install: use the CLI pinned in desktop/package-lock.json
        # (`npm install` in desktop/ first) instead of whatever 2.x npm is
        # serving today, so the icons stay reproducible.
        ["npx", "--no-install", "tauri", "icon", "icon-source.png"],
        cwd=DESKTOP,
        # npx is a .cmd shim on Windows, which CreateProcess cannot run itself.
        shell=os.name == "nt",
    )
)
