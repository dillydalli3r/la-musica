#!/usr/bin/env python3
"""Every app icon the repo ships is the one artwork in desktop/icon-source.png.

The source master and each shipped raster are both resized to 64x64 with their
alpha composited over white — a transparent corner that carries leftover RGB
must not be able to pass by looking like a black one — and compared by mean
absolute pixel difference (0..255). Over CANVAS_TOL means the file is not the
app icon any more.

Two of the Android rasters are the exception. ``mipmap-*/ic_launcher.png`` and
``mipmap-*/ic_launcher_round.png`` are the artwork *masked* (legacy square, and
circular) and re-laid out in Android's own envelope, so scaling them straight
to 64x64 never matches: they are cropped to the artwork's alpha bounding box on
both sides and compared over the file's opaque pixels, against TILE_TOL. The
third Android raster, ``ic_launcher_foreground.png``, is the source untouched,
and is checked like everything else.

Also asserts the Android XML that picks those rasters up is still there and
still names them, so an icon cannot go missing from the manifest silently.

Run: python tools/test_icons.py
Exit 0 = pass, 1 = icons drifted, 2 = skip (no Pillow).
"""
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICONS = os.path.join(ROOT, "desktop", "src-tauri", "icons")
ANDROID = os.path.join(ICONS, "android")
SOURCE = os.path.join(ROOT, "desktop", "icon-source.png")
WEB_ICON = os.path.join(ROOT, "web", "public", "icon.png")

SIDE = 64
# The 20x20 iOS rasters, upscaled to 64x64, lose ~25 on the triangle's edges
# alone; a file drawn from other artwork lands 60-100 away in either metric.
CANVAS_TOL = 30.0
TILE_TOL = 20.0

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


def tile_mad(im, src):
    """The file's artwork envelope against the source's, over opaque pixels."""
    box = im.getchannel("A").getbbox()
    if not box:
        return 255.0
    tile = im.crop(box)
    ref = src.crop(src.getchannel("A").getbbox()).resize(tile.size, Image.LANCZOS)
    mask = tile.getchannel("A").point(lambda v: 255 if v > 127 else 0)
    opaque = sum(mask.histogram()[128:])
    if not opaque:
        return 255.0
    solid = Image.merge("RGB", (mask, mask, mask))
    diff = ImageChops.multiply(
        ImageChops.difference(tile.convert("RGB"), ref.convert("RGB")), solid
    )
    return sum(ImageStat.Stat(diff).sum) / (3.0 * opaque * 255.0)


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


def xml(pattern, needles):
    for path in sorted(glob.glob(pattern)):
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        missing = [n for n in needles if n not in body]
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        check(f"{rel} references the raster set", not missing, f"missing {missing}")
    check(f"{os.path.relpath(pattern, ROOT)} — files present", glob.glob(pattern))


SCORES = []
check("desktop/icon-source.png", os.path.isfile(SOURCE))
if FAILS:
    raise SystemExit(1)

SRC = Image.open(SOURCE).convert("RGBA")

raster(os.path.join(ICONS, "*.png"), canvas_mad, CANVAS_TOL)
raster(os.path.join(ICONS, "ios", "*.png"), canvas_mad, CANVAS_TOL)
raster(os.path.join(ANDROID, "mipmap-*", "ic_launcher_foreground.png"), canvas_mad, CANVAS_TOL)
raster(WEB_ICON, canvas_mad, CANVAS_TOL)
raster(os.path.join(ANDROID, "mipmap-*", "ic_launcher.png"), tile_mad, TILE_TOL)
raster(os.path.join(ANDROID, "mipmap-*", "ic_launcher_round.png"), tile_mad, TILE_TOL)

# The XML is not artwork, but it is what makes Android use the rasters above.
xml(os.path.join(ANDROID, "mipmap-anydpi-v26", "ic_launcher.xml"),
    ["@mipmap/ic_launcher_foreground", "@color/ic_launcher_background"])
xml(os.path.join(ANDROID, "values", "ic_launcher_background.xml"),
    ["ic_launcher_background"])

print(f"\n{len(SCORES)} rasters against desktop/icon-source.png "
      f"(canvas tol {CANVAS_TOL:g}, Android mask tol {TILE_TOL:g})")
print("worst offenders:")
for mad, rel in sorted(SCORES, reverse=True)[:5]:
    print(f"  {mad:6.2f}  {rel}")

if FAILS:
    print(f"\nFAIL {len(FAILS)} check(s): " + ", ".join(FAILS))
    raise SystemExit(1)
print("\nPASS")
raise SystemExit(0)
