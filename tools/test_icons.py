#!/usr/bin/env python3
"""Every app icon the repo ships is the one artwork in desktop/icon-source.png.

The source master and each shipped raster are both resized to 64x64 with their
alpha composited over white — a transparent corner that carries leftover RGB
must not be able to pass by looking like a black one — and compared by mean
absolute pixel difference (0..255). Over CANVAS_TOL means the file is not the
app icon any more.

Run: python tools/test_icons.py
Exit 0 = pass, 1 = icons drifted, 2 = skip (no Pillow).
"""
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICONS = os.path.join(ROOT, "desktop", "src-tauri", "icons")
SOURCE = os.path.join(ROOT, "desktop", "icon-source.png")
WEB_ICON = os.path.join(ROOT, "web", "public", "icon.png")

SIDE = 64
# A file drawn from different artwork lands 60-100 away in either metric.
CANVAS_TOL = 30.0

try:
    from PIL import Image, ImageChops, ImageStat
except ImportError:
    print("SKIP: Pillow not installed")
    raise SystemExit(2)

FAILS = []


def check(label, cond, extra=""):
    print(f"{'ok  ' if cond else 'FAIL'} {label}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(label)


def _canvas(im):
    """RGBA -> the 64x64 RGB comparison frame, transparent shown as white."""
    base = Image.new("RGBA", im.size, (255, 255, 255, 255))
    base.alpha_composite(im)
    return base.convert("RGB").resize((SIDE, SIDE), Image.LANCZOS)


def canvas_mad(im, src):
    a, b = _canvas(im), _canvas(src)
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3.0


def raster(pattern, metric, tol):
    """Check every file the glob names, and that the glob named something."""
    found = sorted(glob.glob(pattern))
    check(f"{os.path.relpath(pattern, ROOT)} — files present", found)
    for path in found:
        try:
            with Image.open(path) as im:
                got = metric(im.convert("RGBA"), SRC)
        except Exception as e:
            check(os.path.relpath(path, ROOT), False, f"unreadable: {e}")
            continue
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        check(f"{rel} matches the source", got <= tol, f"MAD {got:.1f} > {tol}")
        SCORES.append((got, rel))


SCORES = []
check("desktop/icon-source.png", os.path.isfile(SOURCE))
if FAILS:
    raise SystemExit(1)

SRC = Image.open(SOURCE).convert("RGBA")

raster(os.path.join(ICONS, "*.png"), canvas_mad, CANVAS_TOL)
# Not artwork the app renders itself, but the picture the Windows installer and
# the macOS bundle show in a taskbar, a dock and a file manager — and the two
# files Pillow's own ICO/ICNS readers make it easy to leave behind, since no
# `*.png` glob above would ever name them.
raster(os.path.join(ICONS, "icon.ico"), canvas_mad, CANVAS_TOL)
raster(os.path.join(ICONS, "icon.icns"), canvas_mad, CANVAS_TOL)
raster(WEB_ICON, canvas_mad, CANVAS_TOL)

print(f"\n{len(SCORES)} rasters against desktop/icon-source.png "
      f"(canvas tol {CANVAS_TOL:g})")
print("worst offenders:")
for mad, rel in sorted(SCORES, reverse=True)[:5]:
    print(f"  {mad:6.2f}  {rel}")

if FAILS:
    print(f"\nFAIL {len(FAILS)} check(s): " + ", ".join(FAILS))
    raise SystemExit(1)
print("\nPASS")
raise SystemExit(0)
