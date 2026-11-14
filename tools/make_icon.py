"""Draws the Photo Lifeboat icon (an orange and white life ring) and writes packaging/icon.ico.

Run from the repository root:  python tools/make_icon.py [preview.png]
Each size is drawn on its own at 8x and scaled down, so the small sizes stay crisp."""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ORANGE, WHITE, EDGE = (242, 101, 34, 255), (255, 255, 255, 255), (122, 46, 8, 255)
SIZES = [16, 24, 32, 48, 64, 128, 256]


def ring(size: int) -> Image.Image:
    s = size * 8
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = s * 0.03
    outer = (pad, pad, s - pad, s - pad)
    edge = max(8, s * 0.035)                       # dark rim, so the white parts show on a white desktop
    hole = 0.44 if size > 24 else 0.38             # a smaller hole keeps the ring readable at 16 px
    d.ellipse(outer, fill=EDGE)
    o2 = (pad + edge, pad + edge, s - pad - edge, s - pad - edge)
    d.ellipse(o2, fill=ORANGE)
    for mid in (45, 135, 225, 315):                # four white bands on the diagonals
        d.pieslice(o2, mid - 22, mid + 22, fill=WHITE)
    r_in = (s - 2 * pad) / 2 * hole
    c = s / 2
    d.ellipse((c - r_in - edge, c - r_in - edge, c + r_in + edge, c + r_in + edge), fill=EDGE)
    d.ellipse((c - r_in, c - r_in, c + r_in, c + r_in), fill=(0, 0, 0, 0))
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    images = [ring(n) for n in SIZES]
    images[-1].save(root / "packaging" / "icon.ico", sizes=[(n, n) for n in SIZES], append_images=images[:-1])
    if len(sys.argv) > 1:                          # a preview sheet on light and dark backgrounds
        w = sum(SIZES) + 20 * (len(SIZES) + 1)
        sheet = Image.new("RGBA", (w, 2 * 276 + 20), (255, 255, 255, 255))
        ImageDraw.Draw(sheet).rectangle((0, 296, w, 2 * 276 + 20), fill=(32, 32, 32, 255))
        x = 20
        for n, im in zip(SIZES, images):
            for y in (10, 306):
                sheet.alpha_composite(im, (x, y + (256 - n) // 2))
            x += n + 20
        sheet.save(sys.argv[1])


if __name__ == "__main__":
    main()
