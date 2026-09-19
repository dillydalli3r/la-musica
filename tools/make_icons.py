#!/usr/bin/env python3
"""Regenerate the app icons from the one source of truth.

``desktop/icon-source.png`` (1024x1024) is that source, cropped square from
``gato.jpg`` — the photograph at the repo root is the original artwork, kept
here so the source of the source is not a mystery. What this script writes
from it:

- ``web/public/icon.png`` (512x512, square centre-crop + LANCZOS). It is the
  browser favicon (``web/index.html``), the sidebar brand and the copy
  ``tray.py`` puts in the tray.
- everything else — the desktop set (``icons/*.png``, ``icon.ico``,
  ``icon.icns``, the Windows Store logos), ``icons/ios/**`` and
  ``icons/android/**`` — is left to ``tools/make_tauri_icons.py``, which
  drives the Tauri CLI pinned in ``desktop/package-lock.json``. That CLI is
  the only writer of those files, so there is no second drawing code here to
  drift from it.

Run: python tools/make_icons.py
"""
import os
import subprocess
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "desktop", "icon-source.png")
WEB_ICON = os.path.join(ROOT, "web", "public", "icon.png")
WEB_SIZE = 512


def main():
    im = Image.open(SRC).convert("RGBA")
    side = min(im.size)
    im = im.crop(((im.width - side) // 2, (im.height - side) // 2,
                  (im.width + side) // 2, (im.height + side) // 2))
    im.resize((WEB_SIZE, WEB_SIZE), Image.LANCZOS).save(WEB_ICON)
    print(f"web/public/icon.png ({WEB_SIZE}x{WEB_SIZE})")

    code = subprocess.call(
        [sys.executable, os.path.join(ROOT, "tools", "make_tauri_icons.py")]
    )
    if code:
        return code
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
