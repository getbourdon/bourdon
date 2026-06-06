#!/usr/bin/env python3
"""Generate Bourdon tray + app icon assets from the OFFICIAL brand mark.

Run from this directory:  python generate_icons.py

The mark is the Bourdon organ-pipe "b" (the pipe-organ *bourdon* / drone motif) —
reproduced pixel-faithfully in Pillow from the official `brand/favicon.svg`
geometry (rounded paper card + six rising rose pipes + the bowl of the "b").
We redraw rather than rasterize the SVG because Windows has no cairo/rsvg
delegate; the coordinates below are copied exactly from the brand SVG, so this
is the real mark, not a reinterpretation.

Brand palette (from the Bourdon Design System `colors_and_type.css`):
  paper  #f7f1e8   card background
  drone  #c17c74   the mark (dusty rose) — primary accent
Health is shown as a corner BADGE in brand-native semantic colors, sized large
enough (~35% of the icon) to read at 16px:
  fresh    moss   #4f6b50
  attention ochre #a47b3a
  error    clay   #a8543a
  idle/grey ink-faint #8a7d75

Produces:
  Tray state icons (swapped at runtime by src/lib.rs):
    tray-grey/green/yellow/red.png   32px, mark + health badge
  App icon set (bundle.icon — no health badge):
    icon.png 512, 32x32, 128x128, 128x128@2x (256), icon.ico (multi-size)

Requires Pillow. On this machine: Pillow 12.x, Python 3.12.
"""
from __future__ import annotations

from PIL import Image, ImageDraw

# --- palette -----------------------------------------------------------------
PAPER = (247, 241, 232, 255)   # #f7f1e8
ROSE = (193, 124, 116, 255)    # #c17c74  (drone / mark)
MOSS = (79, 107, 80, 255)      # #4f6b50  fresh
OCHRE = (164, 123, 58, 255)    # #a47b3a  attention
CLAY = (168, 84, 58, 255)      # #a8543a  error
FAINT = (138, 125, 117, 255)   # #8a7d75  idle/grey

# Official favicon.svg geometry (viewBox 0 0 400 400).
PIPES = [  # (x, y, w, h)
    (80, 250, 16, 70),
    (104, 220, 16, 100),
    (128, 185, 16, 135),
    (152, 150, 16, 170),
    (176, 115, 16, 205),
    (194, 70, 22, 250),
]
SLITS = [  # paper mouth-slit on each pipe (x, y, w, h)
    (83, 306, 10, 3),
    (107, 306, 10, 3),
    (131, 306, 10, 3),
    (155, 306, 10, 3),
    (179, 306, 10, 3),
    (199, 306, 14, 3),
]


def _cubic(p0, p1, p2, p3, n=26):
    """Sample a cubic Bézier into n points."""
    pts = []
    for i in range(n + 1):
        t = i / n
        mt = 1 - t
        x = mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0]
        y = mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1]
        pts.append((x, y))
    return pts


def _bowl_polygon():
    """The bowl of the 'b' from favicon.svg, as one closed polygon (400-space)."""
    pts = _cubic((216, 198), (258, 182), (304, 200), (304, 244))
    pts += _cubic((304, 244), (304, 286), (262, 314), (216, 318))[1:]
    pts += [(216, 258)]
    pts += _cubic((216, 258), (238, 256), (256, 248), (256, 234))[1:]
    pts += _cubic((256, 234), (256, 220), (240, 218), (216, 230))[1:]
    return pts


def make_icon(size: int, badge: tuple[int, int, int, int] | None) -> Image.Image:
    """The Bourdon mark at `size` px (4x SSAA). If `badge` is given, overlay a
    health badge in the bottom-right corner."""
    ss = 4
    big = size * ss
    s = big / 400.0  # brand-space -> pixel scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def sc(v):
        return v * s

    # paper card (rounded rect, rx=56)
    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=sc(56), fill=PAPER)
    # pipes + mouth slits
    for x, y, w, h in PIPES:
        d.rectangle([sc(x), sc(y), sc(x + w), sc(y + h)], fill=ROSE)
    # bowl of the b
    d.polygon([(sc(px), sc(py)) for px, py in _bowl_polygon()], fill=ROSE)
    for x, y, w, h in SLITS:
        d.rectangle([sc(x), sc(y), sc(x + w), sc(y + h)], fill=PAPER)

    # health badge — bottom-right, paper ring for separation, large enough to
    # read at 16px (~35% of the icon).
    if badge is not None:
        cx, cy = sc(312), sc(316)
        ring = sc(78)
        fill = sc(62)
        d.ellipse([cx - ring, cy - ring, cx + ring, cy + ring], fill=PAPER)
        d.ellipse([cx - fill, cy - fill, cx + fill, cy + fill], fill=badge)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    here = "."
    for name, badge in (
        ("tray-grey", FAINT),
        ("tray-green", MOSS),
        ("tray-yellow", OCHRE),
        ("tray-red", CLAY),
    ):
        make_icon(32, badge).save(f"{here}/{name}.png")
        print(f"wrote {name}.png")

    master = make_icon(512, None)  # app icon: mark only, no health badge
    master.save(f"{here}/icon.png")
    for px in (32, 128, 256):
        out = make_icon(px, None)
        fname = "128x128@2x.png" if px == 256 else f"{px}x{px}.png"
        out.save(f"{here}/{fname}")
        print(f"wrote {fname}")

    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)]
    master.save(f"{here}/icon.ico", sizes=ico_sizes)
    print("wrote icon.ico")


if __name__ == "__main__":
    main()
