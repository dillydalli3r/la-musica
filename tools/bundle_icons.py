#!/usr/bin/env python3
"""Check the artwork a *build* embedded, not the artwork the repo committed.

`tools/test_icons.py` proves every icon file in the repo is the one picture in
`desktop/icon-source.png`. That says nothing about what the compiler and the
platform's asset pipeline put inside the installer a user actually opens: the
iOS bundle and the Android APK are rebuilt from the committed set by tooling
that is free to re-encode, resample or ignore it. This script opens those
artifacts and compares what is inside them to the committed reference of the
same name, so a build that silently ships a stale or placeholder icon fails its
job instead of reaching a phone.

Two modes, one decoder:

    python3 tools/bundle_icons.py --app <path/to/App.app>
    python3 tools/bundle_icons.py --apk <path/to/app.apk>

Apple rewrites PNGs with `pngcrush -iphone`: the file carries a `CgBI` chunk, a
raw-deflate IDAT, and premultiplied BGRA samples, none of which a stock reader
handles — hence the hand-rolled `load_png`. The 64x64 comparison frame is
hand-rolled for the same reason Pillow is not used here: this runs on the
runner's Homebrew Python *before* anything is installed, and PEP 668 refuses
`pip install`. The metric is the one `tools/test_icons.py` documents — mean
absolute RGB difference composited over white, tolerance 30.0.

Exit 0 = every icon inside the artifact matches its committed reference,
1 = a mismatch or a missing icon (each named as a GitHub `::error::`).
"""
import argparse
import os
import pathlib
import plistlib
import re
import struct
import sys
import zipfile
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
IOS_REF = ROOT / "desktop" / "src-tauri" / "icons" / "ios"
ANDROID_REF = ROOT / "desktop" / "src-tauri" / "icons" / "android"

SIDE = 64
TOL = 30.0


def load_png(data):
    """PNG bytes -> (RGBA bytes, width, height).

    Handles Apple's `pngcrush -iphone` variant as well: `CgBI` chunk,
    raw-deflate IDAT, premultiplied BGRA samples and no alpha channel flag,
    plus plain PNG colour types 0/2/3/4/6 at 8 bits.
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, palette, trns, crushed = 8, b"", b"", b"", False
    width = height = depth = color = interlace = None
    while pos + 12 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, color, _comp, _filt, interlace = \
                struct.unpack(">IIBBBBB", body[:13])
        elif kind == b"CgBI":
            crushed = True
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if width is None:
        raise ValueError("the file has no IHDR chunk")
    if (depth, interlace) != (8, 0):
        raise ValueError(f"expected an 8-bit non-interlaced PNG, got depth {depth}, interlace {interlace}")
    if crushed:
        if color != 6:
            raise ValueError(f"crushed PNG is not RGBA (colour type {color})")
        raw = zlib.decompressobj(-15).decompress(idat)
    else:
        raw = zlib.decompress(idat)
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
    if channels is None:
        raise ValueError(f"unsupported PNG colour type {color}")
    stride = width * channels
    rows, prev = bytearray(), bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        if start + 1 + stride > len(raw):
            raise ValueError(f"image data ends inside row {y}")
        filt = raw[start]
        line = bytearray(raw[start + 1:start + 1 + stride])
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if filt == 1:
                line[i] = (line[i] + a) & 255
            elif filt == 2:
                line[i] = (line[i] + b) & 255
            elif filt == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif filt == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 255
            elif filt != 0:
                raise ValueError(f"bad filter {filt} on row {y}")
        rows += line
        prev = line
    out = bytearray(width * height * 4)
    for i in range(width * height):
        s, d = i * channels, i * 4
        if crushed:  # BGRA, premultiplied by alpha
            b, g, r, alpha = rows[s], rows[s + 1], rows[s + 2], rows[s + 3]
            if 0 < alpha < 255:  # un-premultiply
                r = min(255, r * 255 // alpha)
                g = min(255, g * 255 // alpha)
                b = min(255, b * 255 // alpha)
        elif color == 6:
            r, g, b, alpha = rows[s], rows[s + 1], rows[s + 2], rows[s + 3]
        elif color == 2:
            r, g, b, alpha = rows[s], rows[s + 1], rows[s + 2], 255
        elif color == 4:
            r = g = b = rows[s]
            alpha = rows[s + 1]
        elif color == 3:
            idx = rows[s]
            if idx * 3 + 3 > len(palette):
                raise ValueError("palette index outside PLTE")
            r, g, b = palette[idx * 3], palette[idx * 3 + 1], palette[idx * 3 + 2]
            alpha = trns[idx] if idx < len(trns) else 255
        else:
            r = g = b = rows[s]
            alpha = 255
        out[d:d + 4] = bytes((r, g, b, alpha))
    return out, width, height


def frame(image):
    """(RGBA, w, h) -> the 64x64 RGB frame, composited over white.

    Each output pixel is the mean of its source cell, and a source smaller than
    SIDE has cells one sample wide, which is exactly nearest-neighbour.
    Compositing is folded into that mean: the mean of `v` composited over white
    is `(sum(v*a) + 255*(255*n - sum(a))) / (255*n)`, so no full-size RGB copy
    is built.
    """
    rgba, width, height = image
    out = bytearray(SIDE * SIDE * 3)
    for oy in range(SIDE):
        y0 = oy * height // SIDE
        y1 = max(y0 + 1, (oy + 1) * height // SIDE)
        for ox in range(SIDE):
            x0 = ox * width // SIDE
            x1 = max(x0 + 1, (ox + 1) * width // SIDE)
            sr = sg = sb = sa = 0
            for y in range(y0, y1):
                base = y * width * 4
                for x in range(x0, x1):
                    i = base + x * 4
                    a = rgba[i + 3]
                    sr += rgba[i] * a
                    sg += rgba[i + 1] * a
                    sb += rgba[i + 2] * a
                    sa += a
            tot = 255 * (x1 - x0) * (y1 - y0)
            j = (oy * SIDE + ox) * 3
            out[j] = (sr + tot * 255 - sa * 255) // tot
            out[j + 1] = (sg + tot * 255 - sa * 255) // tot
            out[j + 2] = (sb + tot * 255 - sa * 255) // tot
    return out


def mad(icon, ref):
    """Mean absolute RGB difference (0..255) between two PNG payloads."""
    pa, pb = frame(icon), frame(ref)
    return sum(abs(pa[i] - pb[i]) for i in range(len(pa))) / len(pa)


def canon(stem):
    """Xcode copies `AppIcon-60x60@2x.png` in as `AppIcon60x60@2x.png`."""
    return stem.replace("-", "").replace("~ipad", "")


def compare(what, payload, ref, fails):
    """Compare one embedded PNG against its committed reference."""
    try:
        value = mad(load_png(payload), load_png(ref.read_bytes()))
    except Exception as exc:  # unreadable file is a failed check, not a crash
        fails.append(f"{what}: {exc}")
        return
    print(f"{what} vs {ref.name}: mad={value:.2f} (tol {TOL})")
    if value > TOL:
        fails.append(f"{what} drifts from {ref.name} (mad {value:.2f} > {TOL})")


def check_app(app, fails):
    """The icons inside a built iOS .app, against desktop/src-tauri/icons/ios."""
    refs = {canon(p.stem): p for p in IOS_REF.glob("AppIcon*.png")}
    if not refs:
        fails.append(f"no committed iOS reference set in {IOS_REF}")

    plist = {}
    try:
        with open(os.path.join(app, "Info.plist"), "rb") as fh:
            plist = plistlib.load(fh)
    except Exception as exc:  # missing or malformed plist is a failed check
        fails.append(f"cannot read the bundle's Info.plist: {exc}")
    if not isinstance(plist, dict):
        fails.append("the bundle's Info.plist is not a dictionary")
        plist = {}
    # Xcode nests these under `CFBundleIcons > CFBundlePrimaryIcon`; the
    # top-level keys only exist in pre-catalogue plists, so they are the
    # fallback. Both are still hard failures when absent.
    catalog = plist.get("CFBundleIcons")
    primary = catalog.get("CFBundlePrimaryIcon") if isinstance(catalog, dict) else None
    primary = primary if isinstance(primary, dict) else {}
    files = primary.get("CFBundleIconFiles", plist.get("CFBundleIconFiles"))
    name = primary.get("CFBundleIconName", plist.get("CFBundleIconName"))
    if isinstance(files, str):
        files = [files]
    print(f"Info.plist CFBundleIconFiles={files} CFBundleIconName={name}")
    if "AppIcon60x60" not in (files or []):
        fails.append("Info.plist CFBundleIconFiles does not list AppIcon60x60")
    if name != "AppIcon":
        fails.append(f"Info.plist CFBundleIconName is {name!r}, not 'AppIcon'")

    if not os.path.exists(os.path.join(app, "Assets.car")):
        fails.append("Assets.car is missing from the bundle")

    icons = sorted(pathlib.Path(app).rglob("AppIcon*.png"))
    if not icons:
        fails.append("the bundle carries no loose AppIcon*.png")
    for icon in icons:
        ref = refs.get(canon(icon.stem))
        if ref is None:
            fails.append(f"{icon.name} has no committed reference")
            continue
        compare(str(icon.relative_to(app)).replace(os.sep, "/"), icon.read_bytes(), ref, fails)

    if "AppIcon60x60@2x" not in {canon(i.stem) for i in icons}:
        fails.append("the bundle carries no 120x120 iPhone icon (AppIcon60x60@2x)")


def apk_res_name(entry):
    """`res/mipmap-hdpi-v4/ic_launcher.png` -> `mipmap-hdpi/ic_launcher.png`.

    AAPT stamps the API level the directory was qualified for onto the
    packaged name (`-v4` on every density here), which is not part of the
    reference set's path, so it is stripped before the lookup.
    """
    path = entry[len("res/"):]
    return re.sub(r"(mipmap-[a-z]+)-v\d+", r"\1", path)


def check_apk(apk, fails):
    """The icons inside a built .apk, against desktop/src-tauri/icons/android."""
    refs = {p.relative_to(ANDROID_REF).as_posix(): p
            for p in ANDROID_REF.glob("mipmap-*/*.png")}
    if not refs:
        fails.append(f"no committed Android reference set in {ANDROID_REF}")
    with zipfile.ZipFile(apk) as zf:
        names = [n for n in zf.namelist() if n.startswith("res/mipmap-") and n.endswith(".png")]
        if not names:
            fails.append("the APK carries no res/mipmap-*/*.png launcher icon")
        # A density the reference set has but the APK omits is an icon that
        # silently falls back to a lower-resolution launcher graphic.
        want = sorted(n for n in refs if n.endswith(("ic_launcher.png", "ic_launcher_round.png")))
        have = {apk_res_name(n) for n in names}
        missing = [n for n in want if n not in have]
        if missing:
            fails.append(f"the APK is missing launcher icons the reference set has: {missing}")
        for name in sorted(names):
            ref = refs.get(apk_res_name(name))
            if ref is None:
                fails.append(f"{name} has no committed reference")
                continue
            compare(name, zf.read(name), ref, fails)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--app", help="path to a built iOS .app directory")
    target.add_argument("--apk", help="path to a built .apk")
    args = parser.parse_args()

    fails = []
    if args.app:
        if not os.path.isdir(args.app):
            print(f"::error::no .app at {args.app}")
            return 1
        check_app(args.app, fails)
    else:
        if not os.path.isfile(args.apk):
            print(f"::error::no .apk at {args.apk}")
            return 1
        check_apk(args.apk, fails)

    for line in fails:
        print(f"::error::{line}")
    if fails:
        print(f"\nFAIL {len(fails)} icon check(s)")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
