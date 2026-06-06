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

Design: a NEUTRAL status disc (filled circle, darker rim) — a placeholder, NOT
the brand logo. Bourdon is named for the pipe-organ "bourdon" (the deep drone
tone), NOT a bumblebee; the official mark comes from claude.design / the Bourdon
Design System kit and will replace this disc. The four tray variants differ ONLY
in fill color so they are unmistakably distinct at 16px:
  grey   = #9AA0A6  (neutral, benign — NOT red)
  green  = #2EA043
  yellow = #D29922
  red    = #D1242F
The app icon uses the brand amber (#E3A008); the tray's resting "all good" state
is unambiguous green.

Why a solid disc rather than detail: at 16x16 (the effective tray render size on
Windows/macOS) fine detail is mud. A solid color-coded shape reads instantly,
which is the whole point of the health indicator.

Requires Pillow (PIL). On this machine: Pillow 12.2.0, Python 3.12.
"""
from __future__ import annotations

import math
from PIL import Image, ImageDraw

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


def make_disc_icon(size: int, fill: tuple[int, int, int, int]) -> Image.Image:
    """NEUTRAL PLACEHOLDER — a filled status disc whose color encodes health.

    This is intentionally NOT a brand mark. The official Bourdon logo (the
    pipe-organ "bourdon" drone motif) comes from claude.design and will replace
    this; until then a plain disc is an honest status light, not invented brand.
    Rendered at 4x SSAA then downscaled; darker rim for definition on light trays.
    """
    ss = 4
    big = size * ss
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = cy = big / 2
    r = big * 0.42
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_darken(fill, 0.55))  # rim
    ri = r * 0.80
    d.ellipse([cx - ri, cy - ri, cx + ri, cy + ri], fill=fill)            # face
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
        make_disc_icon(32, color).save(f"{here}/{name}.png")
        print(f"wrote {name}.png")

    # App icon set (brand amber). 512 source then downscales.
    master = make_disc_icon(512, BRAND_AMBER)
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
