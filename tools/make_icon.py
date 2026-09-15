#!/usr/bin/env python3
"""Generate the la musica app icon (app_icon.ico + PNGs).

Draws a rounded square with an indigo->violet diagonal gradient, a white
eighth-note glyph and small sparkles, supersampled 4x for crisp edges.
Re-run after changing the design:  python tools/make_icon.py
"""
import os
import sys

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
S = 4                       # supersampling factor
SIZE = 1024                 # final master size


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def diagonal_gradient(size, c1, c2):
    """Smooth diagonal gradient via a tiny bilinear-upscaled raster."""
    small = Image.new("RGB", (64, 64))
    px = small.load()
    for y in range(64):
        for x in range(64):
            px[x, y] = lerp(c1, c2, (x + y) / 126.0)
    return small.resize((size, size), Image.BILINEAR)


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def sparkle(draw, cx, cy, r, color):
    w = r * 0.24
    pts = [
        (cx, cy - r), (cx + w, cy - w), (cx + r, cy), (cx + w, cy + w),
        (cx, cy + r), (cx - w, cy + w), (cx - r, cy), (cx - w, cy - w),
    ]
    draw.polygon(pts, fill=color)


def build_master():
    big = SIZE * S

    # --- background: solid black rounded square matching titlebar BG #0d0d0d
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    mask = rounded_mask(big, int(big * 0.225))
    titlebar_bg = (13, 13, 13, 255)  # BG #0d0d0d to match window titlebar (apply_window_chrome)
    black = Image.new("RGBA", (big, big), titlebar_bg)
    img.paste(black, (0, 0), mask)

    # --- spectrum: white vertical bars (audio equalizer) --------------
    # 7 bars centered, varying heights like a spectrum, rounded tops
    bar_layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bar_layer)
    # normalized bar heights (0..1) - variable ascending: slow start then shoot up (real spectrum)
    heights = [0.30, 0.36, 0.44, 0.58, 0.74, 0.88, 0.97]
    n = len(heights)
    # layout: total width 62% of icon, centered
    total_w = big * 0.62
    gap = total_w * 0.10 / (n - 1)  # 10% of total as gaps
    bar_w = (total_w - gap * (n - 1)) / n
    # bottom and top bounds for bars (leave 18% top/bottom margin)
    bottom = big * 0.78
    top_base = big * 0.22
    avail_h = bottom - top_base
    start_x = (big - total_w) / 2
    for i, h_norm in enumerate(heights):
        h = avail_h * h_norm
        x0 = start_x + i * (bar_w + gap)
        x1 = x0 + bar_w
        y0 = bottom - h
        y1 = bottom
        # rounded top (radius = half bar width)
        r = bar_w * 0.28
        # draw as rounded rectangle (flat bottom, rounded top)
        # Use rectangle + top ellipse for rounded top
        bd.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=(255, 255, 255, 255))
    # composite bars over black
    img = Image.alpha_composite(img, bar_layer)

    return img.resize((SIZE, SIZE), Image.LANCZOS)


def build_window_master():
    """Window icon: just white spectrum bars on transparent (no black square).
    Used for the Tk titlebar so the black titlebar shows through."""
    big = SIZE * S
    bar_layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bar_layer)
    heights = [0.30, 0.36, 0.44, 0.58, 0.74, 0.88, 0.97]
    n = len(heights)
    total_w = big * 0.70  # slightly wider for window small icon
    gap = total_w * 0.10 / (n - 1)
    bar_w = (total_w - gap * (n - 1)) / n
    bottom = big * 0.78
    top_base = big * 0.22
    avail_h = bottom - top_base
    start_x = (big - total_w) / 2
    for i, h_norm in enumerate(heights):
        h = avail_h * h_norm
        x0 = start_x + i * (bar_w + gap)
        x1 = x0 + bar_w
        y0 = bottom - h
        y1 = bottom
        r = bar_w * 0.28
        bd.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=(255, 255, 255, 255))
    return bar_layer.resize((SIZE, SIZE), Image.LANCZOS)


def main():
    os.makedirs(ASSETS, exist_ok=True)
    master = build_master()
    window_master = build_window_master()

    ico_path = os.path.join(ROOT, "app_icon.ico")
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
             (128, 128), (256, 256)]
    frames = [master.resize(s, Image.LANCZOS) for s in sizes]
    frames[-1].save(ico_path, format="ICO", sizes=sizes, append_images=frames[:-1])

    master.resize((256, 256), Image.LANCZOS).save(
        os.path.join(ASSETS, "icon_256.png"))
    master.resize((64, 64), Image.LANCZOS).save(
        os.path.join(ASSETS, "icon_64.png"))
    # Window-only icon: transparent background, just white bars (for titlebar)
    window_ico = os.path.join(ROOT, "app_icon_window.ico")
    window_png = os.path.join(ASSETS, "icon_window_256.png")
    window_frames = [window_master.resize(s, Image.LANCZOS) for s in sizes]
    window_frames[-1].save(window_ico, format="ICO", sizes=sizes, append_images=window_frames[:-1])
    window_master.resize((256, 256), Image.LANCZOS).save(window_png)

    print(f"wrote {ico_path}")
    print(f"wrote {window_ico}")
    print(f"wrote {ASSETS}" + os.sep + "icon_256.png / icon_64.png")
    print(f"wrote {window_png}")


if __name__ == "__main__":
    sys.exit(main())
