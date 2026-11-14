"""Is this file whole? Checks that work from a file's own bytes, one kind of file at a time.

Each kind of file carries its own evidence of being complete: a PNG has a checksum on every part,
an MP4 is a run of boxes that must tile the file, a JPEG must decode to its last block and match
its own embedded preview. Nothing here looks at the card; these functions are given the bytes.
"""
from __future__ import annotations

import io
import struct
import zlib

import numpy as np
from PIL import Image

from . import formats, jpeg, streams
from .rebuild import Judge
from .video import TOP_BOXES

SIGNATURES = {            # extension -> bytes a file of that type starts with (offset, magic)
    "png": (0, b"\x89PNG\r\n\x1a\n"), "gif": (0, b"GIF8"), "bmp": (0, b"BM"),
    "mp4": (4, b"ftyp"), "mov": (4, None), "3gp": (4, b"ftyp"), "m4v": (4, b"ftyp"),
    "heic": (4, b"ftyp"), "avi": (0, b"RIFF"), "wav": (0, b"RIFF"), "pdf": (0, b"%PDF"),
    "zip": (0, b"PK"), "mp3": (0, None),
    **{ext: (0, None) for ext in ("nef", "nrw", "cr2", "arw", "sr2", "srf", "pef", "dng", "tif", "tiff", "3fr",
                                  "erf", "kdc", "dcr", "iiq", "orf", "rw2")},
    "cr3": (4, b"ftyp"), "raf": (0, b"FUJIFILMCCD-RAW"),     # camera RAW built on TIFF: see formats.py
    "mpo": (0, b"\xff\xd8\xff"),       # a 3D photo: two JPEGs in one file
    **{ext: (0, None) for ext in streams.TS_EXT},        # dashcam and camcorder video: see streams.py
}
# The top-level parts an MP4, MOV, 3GP or HEIC file can be made of. Whole-file checks also accept
# the picture and thumbnail parts some cameras add; the video rebuilder does not need them.
BOX_TYPES = TOP_BOXES | {b"PICT", b"pict", b"thum"}


def _valid_isobmff(data) -> bool:
    """Walk top-level boxes of an MP4/MOV/3GP/HEIC file; they must tile the file exactly."""
    pos, n, seen = 0, len(data), 0
    while pos + 8 <= n:
        size, typ = struct.unpack_from(">I4s", data, pos)
        if size == 1:
            if pos + 16 > n:
                return False
            (size,) = struct.unpack_from(">Q", data, pos + 8)
        elif size == 0:
            size = n - pos
        if size < 8 or typ not in BOX_TYPES:
            return False
        pos += size
        seen += 1
    return pos == n and seen >= 2


def _gif_lines_up(data) -> bool:
    """Walk a GIF block by block to its end mark. Every block states its own length, so a file
    that was cut, or had its middle replaced, almost never walks through."""
    n = len(data)
    if n < 14 or bytes(data[:4]) != b"GIF8":
        return False
    p = 13 + (3 << ((data[10] & 7) + 1) if data[10] & 0x80 else 0)

    def sub_blocks(p: int) -> int:
        while p < n and data[p]:
            p += 1 + data[p]
        return p + 1

    while p < n:
        kind = data[p]
        if kind == 0x3B:
            return True
        if kind == 0x21:
            p = sub_blocks(p + 2)
        elif kind == 0x2C:
            if p + 10 > n:
                return False
            flags = data[p + 9]
            p = sub_blocks(p + 10 + (3 << ((flags & 7) + 1) if flags & 0x80 else 0) + 1)
        else:
            return False
    return False


def png_length(buf, pos: int, end: int) -> int | None:
    """Length of the PNG that starts at pos, if every one of its parts passes its own checksum."""
    if bytes(buf[pos:pos + 8]) != b"\x89PNG\r\n\x1a\n":
        return None
    p, first = pos + 8, True
    while p + 12 <= end:
        ln, typ = struct.unpack_from(">I4s", buf, p)
        if p + 12 + ln > end or ln > 1 << 30 or (first and typ != b"IHDR"):
            return None
        if zlib.crc32(buf[p + 4:p + 8 + ln]) != struct.unpack_from(">I", buf, p + 8 + ln)[0]:
            return None
        p, first = p + 12 + ln, False
        if typ == b"IEND":
            return p - pos
    return None


def boxes_length(buf, pos: int, end: int) -> tuple[int, set, bytes] | None:
    """Follow the top-level boxes of a video or HEIC file that starts at pos for as long as they
    run on. Returns (length, box types seen, brand), or None if it does not begin like one."""
    if bytes(buf[pos + 4:pos + 8]) != b"ftyp":
        return None
    brand, p, seen = bytes(buf[pos + 8:pos + 12]), pos, set()
    while p + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, p)
        if size == 1 and p + 16 <= end:
            (size,) = struct.unpack_from(">Q", buf, p + 8)
        if size < 8 or typ not in BOX_TYPES or p + size > end or (typ == b"ftyp" and p != pos):
            break
        seen.add(typ)
        p += size
    return (p - pos, seen, brand) if len(seen) >= 2 else None


def unfinished_video(buf, pos: int, end: int) -> int | None:
    """For a video that starts at pos and whose picture data begins but whose index does not follow:
    the length its picture data says the file has up to the index, or 0 when that was never filled
    in (the recording was cut off). None when no such video starts here."""
    if bytes(buf[pos + 4:pos + 8]) != b"ftyp":
        return None
    (size,) = struct.unpack_from(">I", buf, pos)
    if not 8 <= size <= 1024:
        return None
    p = pos + size
    for _ in range(4):
        if p + 16 > end:
            return None
        size, typ = struct.unpack_from(">I4s", buf, p)
        if typ == b"mdat":
            if size == 1:
                (size,) = struct.unpack_from(">Q", buf, p + 8)
            return p - pos + size if 16 <= size <= end - p else 0
        if typ not in (b"free", b"wide", b"skip", b"uuid") or not 8 <= size <= 1 << 20:
            return None
        p += size
    return None


def check_other(ext: str, data):
    """'verified', 'unverified' or None (clearly not this file any more); or (one of those, a note)."""
    sig = SIGNATURES.get(ext)
    if sig and sig[1] is not None and data[sig[0]:sig[0] + len(sig[1])] != sig[1]:
        return None
    if ext in formats.RAW_EXT:
        return formats.check_tiff(data, len(data), audit_jpeg)
    if ext == "mpo":
        return formats.check_mpo(data, audit_jpeg)
    if ext == "png":
        return "verified" if png_length(data, 0, len(data)) == len(data) else None
    if ext in ("mp4", "mov", "3gp", "m4v", "heic", "cr3"):
        # Its outer parts lining up proves the file is all there, not that what is inside them
        # is still this video: only the frame-by-frame check can say that.
        lined_up = ("its parts line up from start to end, but its contents could not be checked; "
                    "play it through to make sure")
        if _valid_isobmff(data):
            return "unverified", lined_up
        got = boxes_length(data, 0, len(data))
        if got and got[0] >= 0.98 * len(data) and b"mdat" in got[1] and ({b"moov", b"moof", b"meta"} & got[1]):
            return "unverified", lined_up
        return None
    if ext == "avi":
        return streams.check_avi(data)
    if ext in streams.TS_EXT:
        return streams.check_ts(data, ext)
    if ext == "gif":
        return ("unverified", "its parts line up from start to end; this file type carries no further check") \
            if _gif_lines_up(data) else None
    return "unverified"


def audit_jpeg(data: bytes) -> tuple[bool, str, bool]:
    """Independent final check of a delivered photo, from its bytes alone.
    Returns (passed, note, had_preview)."""
    try:
        info = jpeg.parse_header(data)
    except (jpeg.NotJpeg, jpeg.HeaderIncomplete) as e:
        return False, f"header: {e or 'incomplete'}", False
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
    except Exception as e:  # noqa: BLE001
        return False, f"does not open as a picture ({type(e).__name__})", False
    if not info.supported:
        return True, f"{info.why_unsupported}: opened fine, structure not checked", False
    st = jpeg.Stream(info)
    st.feed(data[info.data_off:])
    if st.status != jpeg.ST_DONE:
        why = jpeg.ERROR_NAMES.get(st.error, "data ends early")
        return False, f"picture data fails at {100 * st.mcus // max(1, info.total_mcus)}% ({why})", False
    if st.end_offset() is None:
        return False, "no end marker after the last block", False
    judge = Judge(info, jpeg.decode_thumb(info))
    if judge.target is None:
        return True, "", False
    # compare with the preview band by band, never as one whole-picture average
    X, total = info.mcus_x, info.total_mcus
    band = max(1, info.mcus_y // 24) * X
    scores = [s for m0 in range(0, total, band)
              if (s := judge.preview_score(st.dc[m0:m0 + band], m0)) is not None]
    bad = first_bad_band(scores)
    if bad is not None:
        return False, f"does not match its own preview about {100 * bad // len(scores)}% of the way down", True
    return True, "", True


def first_bad_band(scores: list[float]) -> int | None:
    """Given how well each band of a picture matches its preview (low is good), the first band
    that does not belong, or None. One band far worse than the picture's typical band fails it.
    So does a large part of the picture being far worse than its best parts: when most of a
    picture is wrong, "typical" is itself wrong, and only the good part can be the yardstick."""
    if not scores:
        return None
    med, worst = float(np.median(scores)), max(scores)
    if worst > max(10.0, 3.0 * med):
        return scores.index(worst)
    if len(scores) >= 8:
        bar = max(10.0, 4.0 * float(np.percentile(scores, 10)))
        over = [i for i, v in enumerate(scores) if v > bar]
        if len(over) >= len(scores) / 4:
            return over[0]
    return None


def pixel_audit(data: bytes, info) -> bool | None:
    """Preview check for JPEG kinds the block decoder cannot read: decode with Pillow at
    reduced size and compare with the preview in bands. None = no preview or cannot decode."""
    thumb = jpeg.decode_thumb(info) if info else None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.draft("YCbCr", (max(1, im.width // 8), max(1, im.height // 8)))
            a = np.asarray(im.convert("YCbCr"), np.float32)
    except Exception:  # noqa: BLE001
        return False
    if thumb is None:
        return None
    th, tw = thumb.shape[:2]
    a = np.asarray(Image.fromarray(a.astype(np.uint8), "YCbCr").resize((tw, th), Image.BILINEAR), np.float32)
    band, scores = max(2, th // 24), []
    for r0 in range(0, th - band + 1, band):
        x, y = a[r0:r0 + band], thumb[r0:r0 + band]
        cm = float(np.abs(x[..., 1:] - y[..., 1:]).mean())
        yx, yy = x[..., 0] - x[..., 0].mean(), y[..., 0] - y[..., 0].mean()
        pen = 0.0
        if yy.std() > 6:
            pen = 8 * max(0.0, 0.85 - float((yx * yy).sum() / np.sqrt((yx ** 2).sum() * (yy ** 2).sum() + 1e-6)))
        scores.append(cm + pen)
    return first_bad_band(scores) is None
