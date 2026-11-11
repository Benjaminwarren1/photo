"""JPEG structure: header parsing, the embedded preview, and resumable decoding.

`JpegInfo` describes one photo from its header. `Stream` walks its scan data a
piece at a time using the resumable decoder, collecting the 1/8-scale picture
(the DC value of every block) and noticing the moment the data stops being
valid JPEG.
"""
from __future__ import annotations

import io
import os
import struct
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from . import _scan_py

try:  # the C decoder is roughly a thousand times faster
    from . import _scan as _impl
    HAVE_C = True
except ImportError:  # pragma: no cover - exercised on machines with no compiler
    _impl = _scan_py
    HAVE_C = False

ST_NEED_DATA, ST_DONE, ST_ERROR = 0, 1, 2
STATE = _scan_py.STATE
ERROR_NAMES = {1: "invalid Huffman code", 2: "block overrun", 3: "bad coefficient size",
               4: "unexpected marker", 5: "restart marker out of order", 6: "DC out of range"}


class HeaderIncomplete(Exception):
    """The header runs past the data given; supply more bytes."""


class NotJpeg(Exception):
    pass


@dataclass
class JpegInfo:
    width: int
    height: int
    progressive: bool
    comps: list[tuple[int, int, int, int]]     # id, h, v, quant table
    scan: list[tuple[int, int, int]]           # component index, dc table, ac table
    qdc: list[int]                             # DC quantiser per component
    restart: int
    data_off: int                              # where the entropy-coded data starts
    thumb: bytes | None
    orientation: int
    spec: bytes = b""
    supported: bool = True
    why_unsupported: str = ""
    hmax: int = 1
    vmax: int = 1
    mcus_x: int = 0
    mcus_y: int = 0

    @property
    def total_mcus(self) -> int:
        return self.mcus_x * self.mcus_y

    @property
    def blocks_per_mcu(self) -> int:
        return sum(self.comps[ci][1] * self.comps[ci][2] for ci, _, _ in self.scan)


def _exif_thumbnail(app1: bytes) -> tuple[bytes | None, int]:
    """Pull the IFD1 JPEG thumbnail and the orientation out of an Exif block."""
    if app1[:6] != b"Exif\0\0":
        return None, 1
    t = app1[6:]
    if len(t) < 8 or t[:2] not in (b"II", b"MM"):
        return None, 1
    e = "<" if t[:2] == b"II" else ">"
    orientation = 1

    def ifd(off):
        if off + 2 > len(t):
            return {}, 0
        (n,) = struct.unpack_from(e + "H", t, off)
        tags = {}
        for i in range(n):
            o = off + 2 + i * 12
            if o + 12 > len(t):
                break
            tag, typ, cnt = struct.unpack_from(e + "HHI", t, o)
            val = struct.unpack_from(e + ("H" if typ == 3 else "I"), t, o + 8)[0]
            tags[tag] = val
        nxt_o = off + 2 + n * 12
        nxt = struct.unpack_from(e + "I", t, nxt_o)[0] if nxt_o + 4 <= len(t) else 0
        return tags, nxt

    tags0, nxt = ifd(struct.unpack_from(e + "I", t, 4)[0])
    orientation = tags0.get(0x0112, 1)
    if not nxt:
        return None, orientation
    tags1, _ = ifd(nxt)
    off, ln = tags1.get(0x0201), tags1.get(0x0202)
    if off is None or ln is None or off + 4 > len(t):
        return None, orientation
    th = t[off:off + ln]
    return (th if th[:2] == b"\xff\xd8" else None), orientation


def parse_header(buf: bytes) -> JpegInfo:
    """Parse from SOI to the start of scan data. Raises HeaderIncomplete if buf is too short."""
    if buf[:3] != b"\xff\xd8\xff":
        raise NotJpeg("no JPEG start marker")
    pos = 2
    qt: dict[int, int] = {}
    huff: dict[tuple[int, int], bytes] = {}
    sof = None
    restart = 0
    thumb, orientation = None, 1
    progressive = False
    unsupported = ""
    while True:
        if pos + 4 > len(buf):
            raise HeaderIncomplete
        if buf[pos] != 0xFF:
            raise NotJpeg("marker expected in header")
        m = buf[pos + 1]
        if m == 0xFF:
            pos += 1
            continue
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            pos += 2
            continue
        if m == 0xD9:
            raise NotJpeg("end marker before any picture data")
        (ln,) = struct.unpack_from(">H", buf, pos + 2)
        if ln < 2:
            raise NotJpeg("bad segment length")
        seg_end = pos + 2 + ln
        if seg_end > len(buf):
            raise HeaderIncomplete
        seg = buf[pos + 4:seg_end]
        if m == 0xE1 and thumb is None and seg[:6] == b"Exif\0\0":
            thumb, orientation = _exif_thumbnail(seg)
        elif m == 0xDB:
            i = 0
            while i < len(seg):
                pq, tq = seg[i] >> 4, seg[i] & 15
                if pq:
                    qt[tq] = struct.unpack_from(">H", seg, i + 1)[0]
                    i += 129
                else:
                    qt[tq] = seg[i + 1]
                    i += 65
        elif m == 0xC4:
            i = 0
            while i + 17 <= len(seg):
                tc, th_ = seg[i] >> 4, seg[i] & 15
                n = sum(seg[i + 1:i + 17])
                huff[(tc, th_)] = bytes(seg[i + 1:i + 17 + n])
                i += 17 + n
        elif m == 0xDD:
            (restart,) = struct.unpack_from(">H", seg, 0)
        elif m in (0xC0, 0xC1, 0xC2) or 0xC3 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
            prec, h, w, nc = struct.unpack_from(">BHHB", seg, 0)
            comps = [(seg[6 + 3 * i], seg[7 + 3 * i] >> 4, seg[7 + 3 * i] & 15, seg[8 + 3 * i]) for i in range(nc)]
            sof = (w, h, comps)
            progressive = m == 0xC2
            if m not in (0xC0, 0xC1):
                unsupported = "progressive JPEG" if progressive else "unusual JPEG coding"
            elif prec != 8:
                unsupported = "not 8-bit"
        elif m == 0xDA:
            if sof is None:
                raise NotJpeg("scan before frame header")
            w, h, comps = sof
            ns = seg[0]
            scan = []
            for i in range(ns):
                cid, tabs = seg[1 + 2 * i], seg[2 + 2 * i]
                idx = next((k for k, c in enumerate(comps) if c[0] == cid), None)
                if idx is None:
                    raise NotJpeg("scan names an unknown component")
                scan.append((idx, tabs >> 4, tabs & 15))
            info = JpegInfo(w, h, progressive, comps, scan, [qt.get(c[3], 1) for c in comps],
                            restart, seg_end, thumb, orientation)
            if not unsupported and (ns != len(comps) or len(comps) not in (1, 3)):
                unsupported = "unusual component layout"
            if not unsupported and (w == 0 or h == 0):
                unsupported = "no picture size in header"
            info.hmax = max(c[1] for c in comps)
            info.vmax = max(c[2] for c in comps)
            if len(comps) == 1:      # a single-component scan is never interleaved
                info.comps = [(comps[0][0], 1, 1, comps[0][3])]
                info.hmax = info.vmax = 1
            info.mcus_x = -(-w // (8 * info.hmax))
            info.mcus_y = -(-h // (8 * info.vmax))
            if unsupported:
                info.supported, info.why_unsupported = False, unsupported
                return info
            tables = bytearray(8 * 272)
            for (tc, th_), raw in huff.items():
                if th_ < 4:
                    o = (tc * 4 + th_) * 272
                    tables[o:o + len(raw)] = raw
            head = [len(scan)]
            for k in range(4):
                if k < len(scan):
                    ci, dt, at = scan[k]
                    q = max(1, info.qdc[ci])
                    head += [info.comps[ci][1] * info.comps[ci][2], dt, at, 2 * (1024 // q) + 4]
                else:
                    head += [0, 0, 0, 0]
            info.spec = _scan_py.SPEC_HEAD.pack(*head, restart, info.total_mcus) + bytes(tables)
            return info
        pos = seg_end


def find_end(data: bytes) -> int | None:
    """Walk the markers of any JPEG (progressive included) and return the offset just
    past its end marker, or None if the structure breaks first. This says where the
    picture really ends, which need not be where the file ends."""
    n = len(data)
    if data[:2] != b"\xff\xd8":
        return None
    pos = 2
    while pos + 2 <= n:
        if data[pos] != 0xFF:
            return None
        m = data[pos + 1]
        if m == 0xFF:
            pos += 1
            continue
        if m == 0xD9:
            return pos + 2
        if m == 0x01 or 0xD0 <= m <= 0xD8:
            pos += 2
            continue
        if pos + 4 > n:
            return None
        (ln,) = struct.unpack_from(">H", data, pos + 2)
        if ln < 2:
            return None
        pos += 2 + ln
        if m == 0xDA:      # entropy-coded data follows: skip to the next real marker
            while True:
                pos = data.find(b"\xff", pos)
                if pos < 0 or pos + 1 >= n:
                    return None
                nxt = data[pos + 1]
                if nxt == 0 or 0xD0 <= nxt <= 0xD7:
                    pos += 2
                    continue
                break
    return None


def decode_thumb(info: JpegInfo) -> np.ndarray | None:
    """The embedded preview as a float YCbCr array, with any black bars removed."""
    if not info.thumb:
        return None
    try:
        im = Image.open(io.BytesIO(info.thumb)).convert("YCbCr")
        a = np.asarray(im, np.float32)
    except Exception:  # noqa: BLE001 - a damaged preview simply means no preview
        return None
    if a.shape[0] < 8 or a.shape[1] < 8:
        return None
    th, tw = a.shape[:2]
    if (info.height > info.width) != (th > tw):
        return None                     # preview is rotated relative to the data; not usable as is
    want = info.width / info.height
    if abs(tw / th - want) / want > 0.03:
        # phone previews are often 4:3 with black bars around a 16:9 picture
        if tw / th < want:
            keep = int(round(tw / want))
            top = (th - keep) // 2
            bars = np.concatenate([a[:top, :, 0].ravel(), a[top + keep:, :, 0].ravel()])
            if keep < 8 or (bars.size and bars.mean() > 24):
                return None
            a = a[top:top + keep].copy()
            if keep > 12:    # the bars bleed into the rows beside them when the preview is compressed
                a[:2], a[-2:] = a[2], a[-3]
        else:
            keep = int(round(th * want))
            left = (tw - keep) // 2
            bars = np.concatenate([a[:, :left, 0].ravel(), a[:, left + keep:, 0].ravel()])
            if keep < 8 or (bars.size and bars.mean() > 24):
                return None
            a = a[:, left:left + keep].copy()
            if keep > 12:
                a[:, :2], a[:, -2:] = a[:, 2:3], a[:, -3:-2]
    return a


class Stream:
    """Resumable walk through one photo's scan data.

    feed(data) decodes as far as the data allows and remembers where it stopped.
    probe(data) does the same without changing anything, for testing a candidate.
    """

    def __init__(self, info: JpegInfo):
        self.info = info
        self.state = STATE.pack(0, 0, 0, 0, 0, 0, 0, 0, info.restart, 0)
        self.carry = b""                 # bytes given but not yet consumed
        self.dc = np.zeros((info.total_mcus, info.blocks_per_mcu), np.int16)
        self.mcus = 0
        self.status = ST_NEED_DATA
        self.error = 0
        self.consumed = 0                # scan bytes consumed so far (relative to data_off)

    def _run(self, data: bytes):
        buf = self.carry + data
        st = STATE.pack(0, *STATE.unpack(self.state)[1:])
        status, n, dc, new_state, err = _impl.decode(buf, st, self.info.spec, -1)
        return buf, status, n, dc, new_state, err

    def probe(self, data: bytes):
        """Returns (status, new_mcus, dc_block_values, error) without committing."""
        _, status, n, dc, _, err = self._run(data)
        return status, n, np.frombuffer(dc, np.int16).reshape(n, self.info.blocks_per_mcu), err

    def feed(self, data: bytes):
        buf, status, n, dc, new_state, err = self._run(data)
        if n:
            self.dc[self.mcus:self.mcus + n] = np.frombuffer(dc, np.int16).reshape(n, self.info.blocks_per_mcu)
        self.mcus += n
        pos = STATE.unpack(new_state)[0]
        self.consumed += pos
        self.carry = buf[pos:]
        self.state = new_state
        self.status, self.error = status, err
        return status, n

    def probe_many(self, image, data_off: int, cl: int, clusters: np.ndarray, want_done: bool):
        """Try each cluster number as the next piece. Returns [(cluster, status, dc_values)]
        for those the decoder accepts."""
        st = STATE.pack(0, *STATE.unpack(self.state)[1:])
        res = _impl.probe_many(image, data_off, cl, np.ascontiguousarray(clusters, "<i8").tobytes(),
                               self.carry, st, self.info.spec, 1 if want_done else 0)
        b = self.info.blocks_per_mcu
        return [(c, status, np.frombuffer(dc, np.int16).reshape(n, b)) for c, status, n, dc in res]

    def screen_many(self, image, data_off: int, cl: int, clusters: np.ndarray, n_mcus: int):
        """A fast first look: the first n_mcus MCUs each cluster would add. Returns
        (clusters that gave that many, their DC values [k, n_mcus, blocks], clusters that
        decode cleanly but give fewer). Work is shared between processor cores."""
        st = STATE.pack(0, *STATE.unpack(self.state)[1:])
        clusters = np.ascontiguousarray(clusters, "<i8")
        b = self.info.blocks_per_mcu

        def part(cs):
            return _impl.screen_many(image, data_off, cl, cs.tobytes(), self.carry, st, self.info.spec, n_mcus)

        workers = min(8, os.cpu_count() or 1) if HAVE_C and len(clusters) >= 2048 else 1
        if workers > 1:
            with ThreadPoolExecutor(workers) as ex:
                parts = list(ex.map(part, np.array_split(clusters, workers)))
        else:
            parts = [part(clusters)]
        full = np.concatenate([np.frombuffer(p[0], "<i8") for p in parts])
        dc = np.concatenate([np.frombuffer(p[1], np.int16) for p in parts]).reshape(len(full), n_mcus, b)
        short = np.concatenate([np.frombuffer(p[2], "<i8") for p in parts])
        return full, dc, short

    def end_offset(self) -> int | None:
        """Once DONE: offset (relative to data_off) just past the end marker, or None
        if no end marker follows the last MCU."""
        c = self.carry
        i = 0
        while i + 1 < len(c) and c[i] == 0xFF and c[i + 1] == 0xFF:
            i += 1
        if c[i:i + 2] == b"\xff\xd9":
            return self.consumed + i + 2
        return None

    def copy(self) -> "Stream":
        s = Stream.__new__(Stream)
        s.info, s.state, s.carry = self.info, self.state, self.carry
        s.dc = self.dc.copy()
        s.mcus, s.status, s.error, s.consumed = self.mcus, self.status, self.error, self.consumed
        return s


def dc_planes(info: JpegInfo, dc: np.ndarray, mcus: int | None = None) -> np.ndarray:
    """Turn per-MCU DC values into a YCbCr picture at 1/8 scale of the luma grid.
    MCUs not decoded yet are mid grey."""
    n = info.total_mcus if mcus is None else mcus
    X, Y = info.mcus_x, info.mcus_y
    planes = []
    col = 0
    for ci, _, _ in info.scan:
        _, h, v, _ = info.comps[ci]
        q = info.qdc[ci]
        blk = dc[:, col:col + h * v].astype(np.float32) * (q / 8.0) + 128.0
        blk[n:] = 128.0
        col += h * v
        p = blk.reshape(Y, X, v, h).transpose(0, 2, 1, 3).reshape(Y * v, X * h)
        if h != info.hmax or v != info.vmax:
            p = np.repeat(np.repeat(p, info.vmax // v, axis=0), info.hmax // h, axis=1)
        planes.append(p)
    if len(planes) == 1:
        g = np.full_like(planes[0], 128.0)
        planes += [g, g]
    return np.clip(np.stack(planes, axis=-1), 0, 255)
