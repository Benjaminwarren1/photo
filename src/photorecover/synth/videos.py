"""Generates MP4 files with a real index (moov) and stand-in frame data, for tests.

The frames are random bytes behind genuine H.264-style length prefixes and NAL
headers, and the audio packets are AMR-style, so every checkpoint the rebuilder
relies on is present. No real footage is used.
"""
from __future__ import annotations

import struct

import numpy as np


def _box(typ: bytes, *parts: bytes) -> bytes:
    body = b"".join(parts)
    return struct.pack(">I4s", 8 + len(body), typ) + body


def _trak(handler: bytes, codec: bytes, sizes: list[int], offsets: list[int]) -> bytes:
    stsd = _box(b"stsd", bytes(4), struct.pack(">I", 1), _box(codec, bytes(8)))
    stsz = _box(b"stsz", bytes(4), struct.pack(">II", 0, len(sizes)), struct.pack(f">{len(sizes)}I", *sizes))
    stco = _box(b"stco", bytes(4), struct.pack(">I", len(offsets)), struct.pack(f">{len(offsets)}I", *offsets))
    stsc = _box(b"stsc", bytes(4), struct.pack(">I", 1), struct.pack(">III", 1, 1, 1))
    hdlr = _box(b"hdlr", bytes(8), handler, bytes(12), b"\0")
    return _box(b"trak", _box(b"mdia", hdlr, _box(b"minf", _box(b"stbl", stsd, stsz, stco, stsc))))


def make_video(seed: int, frames: int = 120, mean_frame: int = 9000, audio: bool = True,
               moov_first: bool = False) -> bytes:
    rng = np.random.default_rng(seed)
    ftyp = _box(b"ftyp", b"isom", bytes(4), b"isomavc1")
    vsizes = [int(max(200, rng.normal(mean_frame, mean_frame / 2))) if i % 30 else mean_frame * 6 for i in range(frames)]
    samples: list[tuple[str, bytes]] = []
    for i, sz in enumerate(vsizes):
        nal = 0x65 if i % 30 == 0 else 0x41
        samples.append(("v", struct.pack(">IB", sz - 4, nal) + rng.bytes(sz - 5)))
        if audio:
            for _ in range(2):
                samples.append(("a", b"\x3c" + rng.bytes(31)))

    def build(data_start: int):
        voff, aoff, pos = [], [], data_start
        for kind, payload in samples:
            (voff if kind == "v" else aoff).append(pos)
            pos += len(payload)
        traks = [_trak(b"vide", b"avc1", vsizes, voff)]
        if audio:
            traks.append(_trak(b"soun", b"samr", [32] * len(aoff), aoff))
        return _box(b"moov", _box(b"mvhd", bytes(100)), *traks)

    body = b"".join(p for _, p in samples)
    if moov_first:
        moov = build(0)
        moov = build(len(ftyp) + len(moov) + 8)
        return ftyp + moov + _box(b"mdat", body)
    return ftyp + _box(b"mdat", body) + build(len(ftyp) + 8)
