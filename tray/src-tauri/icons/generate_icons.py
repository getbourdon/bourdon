#!/usr/bin/env python3
"""Generate Bourdon tray + app icon assets.

Run from this directory:  python generate_icons.py

Produces, in this `icons/` folder:

  App icon (Tauri bundle.icon set — used by the installer/binary, NOT swapped):
    icon.png            512x512  (source PoT for bundlers)
    32x32.png           32x32
    128x128.png         128x128
    128x128@2x.png      256x256
    icon.ico            multi-size Windows ICO (16/24/32/48/64/256)

  Tray state icons (swapped at runtime by src/lib.rs to reflect health):
    tray-grey.png       32x32   (0 agents — installed, nothing published)
    tray-green.png      32x32   (>=1 agent, fresh, no parse errors)
    tray-yellow.png     32x32   (stale / partial parse errors)
    tray-red.png        32x32   (CLI failed / all agents broken)

Design: a filled hexagon ("honeycomb cell" — Bourdon = bumblebee/forager motif)
on a transparent background, with a small darker hex border. The four tray
variants differ ONLY in fill color so they are unmistakably distinct at 16px:
  grey   = #9AA0A6  (neutral, benign — NOT red)
  green  = #2EA043
  yellow = #D29922
  red    = #D1242F
The app icon uses the brand amber (#E3A008) so the installed app is on-brand
while the tray's resting "all good" state is unambiguous green.

Why a hexagon dot rather than a detailed bee: at 16x16 (the effective tray
render size on Windows/macOS) fine detail is mud. A solid color-coded shape
reads instantly, which is the whole point of the health indicator.

Requires Pillow (PIL). On this machine: Pillow 12.2.0, Python 3.12.
"""
from __future__ import annotations

import math
from PIL import Image, ImageDraw, ImageChops

# --- palette -----------------------------------------------------------------
BRAND_AMBER = (227, 160, 8, 255)   # #E3A008  app icon fill
GREY = (154, 160, 166, 255)        # #9AA0A6
GREEN = (46, 160, 67, 255)         # #2EA043
YELLOW = (210, 153, 34, 255)       # #D29922
RED = (209, 36, 47, 255)           # #D1242F


def _hex_points(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    """Flat-top hexagon vertices centered at (cx, cy) with circumradius r."""
    pts = []
    for i in range(6):
        ang = math.radians(60 * i)  # flat-top
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return pts


def _darken(rgba: tuple[int, int, int, int], f: float = 0.62) -> tuple[int, int, int, int]:
    r, g, b, a = rgba
    return (int(r * f), int(g * f), int(b * f), a)


DARK = (22, 27, 34, 255)        # #161b22  stripes/head (matches UI bg-elev)
WING = (191, 224, 255, 150)     # #bfe0ff  translucent wings


def make_bee_icon(size: int, body: tuple[int, int, int, int]) -> Image.Image:
    """The Bourdon bee: a striped bumblebee whose BODY color encodes health.

    Rendered at 4x SSAA then downscaled. Bold + simple so it survives 16px tray
    rasterization: angled translucent wings, a dark head, a color-coded body with
    three dark stripes clipped to the body silhouette.
    """
    ss = 4
    big = size * ss
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    cx = big / 2

    # body geometry (vertical ellipse)
    bw, bh = big * 0.46, big * 0.60
    bx0, bx1 = cx - bw / 2, cx + bw / 2
    by0 = big * 0.30
    by1 = by0 + bh

    # wings — drawn on their own layers and rotated for a natural splay
    ww, wh = big * 0.30, big * 0.185
    wy = big * 0.205
    lw = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(lw).ellipse([cx - big * 0.33, wy, cx - big * 0.33 + ww, wy + wh], fill=WING)
    lw = lw.rotate(18, center=(cx, wy + wh / 2), resample=Image.BICUBIC)
    rw = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(rw).ellipse([cx + big * 0.33 - ww, wy, cx + big * 0.33, wy + wh], fill=WING)
    rw = rw.rotate(-18, center=(cx, wy + wh / 2), resample=Image.BICUBIC)
    img = Image.alpha_composite(img, lw)
    img = Image.alpha_composite(img, rw)

    d = ImageDraw.Draw(img)
    # head
    hr = big * 0.105
    d.ellipse([cx - hr, big * 0.18 - hr, cx + hr, big * 0.18 + hr], fill=DARK)
    # body
    d.ellipse([bx0, by0, bx1, by1], fill=body)

    # three stripes, clipped to the body silhouette
    stripes = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stripes)
    sh = bh * 0.135
    for i in range(3):
        y = by0 + bh * (0.20 + i * 0.265)
        sd.rectangle([bx0 - 4, y, bx1 + 4, y + sh], fill=DARK)
    body_mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(body_mask).ellipse([bx0, by0, bx1, by1], fill=255)
    stripes.putalpha(ImageChops.multiply(stripes.split()[3], body_mask))
    img = Image.alpha_composite(img, stripes)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    here = "."
    # Tray state icons — 32px source (renders crisp down to 16px).
    for name, color in (
        ("tray-grey", GREY),
        ("tray-green", GREEN),
        ("tray-yellow", YELLOW),
        ("tray-red", RED),
    ):
        make_bee_icon(32, color).save(f"{here}/{name}.png")
        print(f"wrote {name}.png")

    # App icon set (brand amber). 512 source then downscales.
    master = make_bee_icon(512, BRAND_AMBER)
    master.save(f"{here}/icon.png")
    for px in (32, 128, 256):
        out = master.resize((px, px), Image.LANCZOS)
        fname = "128x128@2x.png" if px == 256 else f"{px}x{px}.png"
        out.save(f"{here}/{fname}")
        print(f"wrote {fname}")

    # Windows .ico (multi-resolution) for the bundle + window icon.
    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)]
    master.save(f"{here}/icon.ico", sizes=ico_sizes)
    print("wrote icon.ico")


if __name__ == "__main__":
    main()
