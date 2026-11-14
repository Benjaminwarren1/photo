"""Generates photo-like JPEGs with a phone-style EXIF preview, for tests.

No real photographs are used. Each picture is drawn from a seed: a sky-to-ground
gradient, soft blobs, hard-edged shapes and sensor-like noise, which gives the
varied colour and texture that real photos have from top to bottom.
"""
from __future__ import annotations

import io
import struct

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


def draw_scene(seed: int, width: int = 1600, height: int = 1200, dark: bool = False) -> Image.Image:
    rng = np.random.default_rng(seed)
    top = rng.integers(40, 255, 3)
    bottom = rng.integers(20, 200, 3)
    t = np.linspace(0, 1, height)[:, None, None]
    base = (top * (1 - t) + bottom * t) * np.ones((1, width, 1))
    im = Image.fromarray(base.astype(np.uint8), "RGB")
    dr = ImageDraw.Draw(im)
    for _ in range(int(rng.integers(25, 45))):
        x, y = int(rng.integers(0, width)), int(rng.integers(0, height))
        r = int(rng.integers(height // 30, height // 4))
        col = tuple(int(v) for v in rng.integers(0, 255, 3))
        kind = rng.integers(0, 3)
        if kind == 0:
            dr.ellipse([x - r, y - r, x + r, y + r], fill=col)
        elif kind == 1:
            dr.rectangle([x - r, y - r // 2, x + r, y + r // 2], fill=col)
        else:
            pts = [(x + int(rng.integers(-r, r)), y + int(rng.integers(-r, r))) for _ in range(3)]
            dr.polygon(pts, fill=col)
    im = im.filter(ImageFilter.GaussianBlur(2))
    dr = ImageDraw.Draw(im)
    for _ in range(int(rng.integers(30, 60))):
        x, y = int(rng.integers(0, width)), int(rng.integers(0, height))
        col = tuple(int(v) for v in rng.integers(0, 255, 3))
        dr.line([x, y, x + int(rng.integers(-300, 300)), y + int(rng.integers(-300, 300))],
                fill=col, width=int(rng.integers(1, 6)))
    a = np.asarray(im, np.float32)
    a += rng.normal(0, 6, a.shape)
    if dark:
        a *= 0.18
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB")


def _exif_with_thumbnail(thumb: bytes, orientation: int = 1) -> bytes:
    """A minimal little-endian EXIF block: IFD0 (Make, Model, Orientation) and
    IFD1 holding a JPEG thumbnail, laid out the way cameras write it."""
    make, model = b"photolifeboat\0", b"synthetic\0\0"
    ifd0_n, ifd1_n = 3, 3
    ifd0_off = 8
    ifd0_len = 2 + ifd0_n * 12 + 4
    strings_off = ifd0_off + ifd0_len
    ifd1_off = strings_off + len(make) + len(model)
    ifd1_len = 2 + ifd1_n * 12 + 4
    thumb_off = ifd1_off + ifd1_len
    t = b"II*\0" + struct.pack("<I", ifd0_off)
    t += struct.pack("<H", ifd0_n)
    t += struct.pack("<HHII", 0x010F, 2, len(make), strings_off)
    t += struct.pack("<HHII", 0x0110, 2, len(model), strings_off + len(make))
    t += struct.pack("<HHIHH", 0x0112, 3, 1, orientation, 0)
    t += struct.pack("<I", ifd1_off)
    t += make + model
    t += struct.pack("<H", ifd1_n)
    t += struct.pack("<HHIHH", 0x0103, 3, 1, 6, 0)
    t += struct.pack("<HHII", 0x0201, 4, 1, thumb_off)
    t += struct.pack("<HHII", 0x0202, 4, 1, len(thumb))
    t += struct.pack("<I", 0)
    t += thumb
    return b"Exif\0\0" + t


def make_photo(seed: int, width: int = 1600, height: int = 1200, quality: int = 88,
               thumb_size: tuple[int, int] = (160, 120), preview: bool = True,
               dark: bool = False, restart_interval: int = 0, subsampling: int = 2,
               letterbox: bool = False, progressive: bool = False, optimize: bool = False) -> bytes:
    """Returns the bytes of a JPEG. With preview=True it carries an EXIF thumbnail
    of the whole picture, as phone and camera JPEGs do."""
    im = draw_scene(seed, width, height, dark)
    kw: dict = dict(quality=quality, subsampling=subsampling, progressive=progressive, optimize=optimize)
    if restart_interval:
        kw["restart_marker_blocks"] = restart_interval
    if preview:
        th = im.copy()
        th.thumbnail(thumb_size, Image.BILINEAR)
        if letterbox:
            canvas = Image.new("RGB", thumb_size, (0, 0, 0))
            canvas.paste(th, ((thumb_size[0] - th.width) // 2, (thumb_size[1] - th.height) // 2))
            th = canvas
        tb = io.BytesIO()
        th.save(tb, "JPEG", quality=75)
        kw["exif"] = _exif_with_thumbnail(tb.getvalue())
    out = io.BytesIO()
    im.save(out, "JPEG", **kw)
    return out.getvalue()
