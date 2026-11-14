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


def make_avi(seed: int, frames: int = 90, mean_frame: int = 9000, index: bool = True, finished: bool = True) -> bytes:
    """An AVI file with stand-in frames. finished=False gives what a camera leaves when the power
    goes mid-recording: frames, but no index and no lengths filled in."""
    rng = np.random.default_rng(seed)

    def chunk(name: bytes, body: bytes) -> bytes:
        return name + struct.pack("<I", len(body)) + body + (b"\0" if len(body) & 1 else b"")
    hdrl = chunk(b"LIST", b"hdrl" + chunk(b"avih", bytes(56)))
    body, idx = b"movi", b""
    for i in range(frames):
        for name, size in ((b"00dc", int(max(300, rng.normal(mean_frame, mean_frame / 3)))), (b"01wb", 1024)):
            idx += name + struct.pack("<III", 0x10, len(body), size)
            body += chunk(name, b"\xff\xd8" + rng.bytes(size - 2) if name == b"00dc" else rng.bytes(size))
    if not finished:
        return b"RIFF" + bytes(4) + b"AVI " + hdrl + b"LIST" + bytes(4) + body
    inner = b"AVI " + hdrl + chunk(b"LIST", body) + (chunk(b"idx1", idx) if index else b"")
    return b"RIFF" + struct.pack("<I", len(inner)) + inner


def make_ts(seed: int, packets: int = 12000, m2ts: bool = False) -> bytes:
    """A transport stream: 188-byte packets (192 with a camcorder's time stamp in front), the first
    one the table of contents, each with its marker byte and a running count."""
    rng = np.random.default_rng(seed)
    a = rng.integers(0, 256, (packets, 192 if m2ts else 188), dtype=np.uint8)
    o = 4 if m2ts else 0
    a[:, o] = 0x47
    a[:, o + 1] = 0x01
    a[:, o + 2] = 0x00
    a[:, o + 3] = 0x10 | ((np.arange(packets) + seed) & 15)
    a[0, o + 1], a[0, o + 2] = 0x40, 0x00
    a[:, o + 4] = 0                # no flags that excuse a jump in the count
    return a.tobytes()
