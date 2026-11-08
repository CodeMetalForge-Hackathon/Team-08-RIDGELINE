"""Draw the RIDGELINE phone app icons (the ridge mark from the page header) with Pillow.

    python mockups/phone_screen/icons/make_icons.py

Writes icon-180.png (iPhone home screen), icon-192.png, icon-512.png and icon-maskable-512.png (Android adaptive
icon: the mark kept inside the central 80% safe zone). The path is the header's SVG,
M2 26 13 6 19 17 23 10 30 26 M9 26 l7-12 7 12 on a 32 x 32 grid, drawn 4x larger and scaled down for smooth edges.
"""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
BG, INK = (11, 17, 23), (134, 220, 233)   # --bg and --cyan of the phone page
RIDGE = [(2, 26), (13, 6), (19, 17), (23, 10), (30, 26)]
INNER = [(9, 26), (16, 14), (23, 26)]


def icon(size, mark, rounded):
    s = 4 * size
    im = Image.new("RGBA", (s, s), BG + (255,))
    if rounded:   # a rounded square on transparent, for browsers that show the icon as is
        im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ImageDraw.Draw(im).rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.18), fill=BG + (255,))
    d = ImageDraw.Draw(im)
    box = s * mark            # the 32 x 32 grid fills this share of the icon
    off = (s - box) / 2
    k = box / 32
    w = max(2, round(2.2 * k))

    def pts(p):
        return [(off + x * k, off + (y - 1) * k) for x, y in p]   # nudged up one unit: the mark sits low in its grid

    for line in (RIDGE, INNER):
        q = pts(line)
        d.line(q, fill=INK + (255,), width=w, joint="curve")
        for x, y in (q[0], q[-1]):   # round the ends
            d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=INK + (255,))
    return im.resize((size, size), Image.LANCZOS)


def main():
    out = {"icon-180.png": icon(180, 0.70, False), "icon-192.png": icon(192, 0.70, True),
           "icon-512.png": icon(512, 0.70, True), "icon-maskable-512.png": icon(512, 0.56, False)}
    for name, im in out.items():
        path = os.path.join(HERE, name)
        im.save(path, optimize=True)
        print(f"{name}: {im.size[0]} px, {os.path.getsize(path)} bytes")


if __name__ == "__main__":
    main()
