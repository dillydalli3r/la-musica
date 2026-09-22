#!/usr/bin/env python3
"""Regenerate the app icons from the one source of truth.

``desktop/icon-source.png`` (1024x1024) is that source, cropped square from
``kitten-headphones.jpg`` — the photograph at the repo root is the original
artwork, kept here so the source of the source is not a mystery. What this
script writes from it:

- ``web/public/icon.png`` (512x512, square centre-crop + LANCZOS). It is the
  browser favicon (``web/index.html``), the sidebar brand and the home-screen
  icon the manifest points at.
- everything else — the desktop set (``icons/*.png``, ``icon.ico``,
  ``icon.icns``, the Windows Store logos), ``icons/ios/**`` and
  ``icons/android/**`` — is left to ``tools/make_tauri_icons.py``, which
  drives the Tauri CLI pinned in ``desktop/package-lock.json``. That CLI is
  the only *producer* of those files, so there is no second drawing code here
  to drift from it. The one thing this script touches afterwards is the order
  of the ICNS element blocks, which the CLI writes out of a ``HashMap`` — see
  ``ordered_icns``, it moves no pixel.

Run: python tools/make_icons.py
"""
import os
import subprocess
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "desktop", "icon-source.png")
ICNS = os.path.join(ROOT, "desktop", "src-tauri", "icons", "icon.icns")
WEB_ICON = os.path.join(ROOT, "web", "public", "icon.png")
WEB_SIZE = 512


def ordered_icns(path):
    """Rewrite an ICNS with its element blocks in OSType order.

    ``tauri icon`` builds the file through the ``icns`` crate, which keeps its
    images in a ``HashMap`` and serialises them in iteration order — and Rust
    reseeds that order for every process. So two runs over the same source
    agree byte for byte on every PNG and on the ICO, and differ on the ICNS in
    the sequence of its blocks alone; without this, "regenerate and see what
    changed" could never come back clean for that one file. Nothing reads an
    ICNS by position (a consumer looks a block up by its OSType), so sorting
    them changes no picture. The block bytes are the CLI's, untouched.
    """
    with open(path, "rb") as fh:
        blob = fh.read()
    # Shape guard: a file this does not recognise is left exactly as the CLI
    # wrote it, so a format change degrades to "no canonical order" instead of
    # to a corrupt icon.
    if blob[:4] != b"icns" or int.from_bytes(blob[4:8], "big") != len(blob):
        return
    blocks, off = [], 8
    while off < len(blob):
        size = int.from_bytes(blob[off + 4:off + 8], "big")
        if size < 8 or off + size > len(blob):
            return
        blocks.append(blob[off:off + size])
        off += size
    blocks.sort()
    with open(path, "wb") as fh:
        fh.write(blob[:8] + b"".join(blocks))


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
    ordered_icns(ICNS)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
