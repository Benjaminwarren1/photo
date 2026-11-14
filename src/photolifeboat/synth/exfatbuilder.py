"""Builds exFAT card images in memory for tests, and remembers the truth.

Mirrors FatBuilder. The behaviour that matters for recovery:

* a file that gets one unbroken run is flagged NoFatChain and has no FAT entries;
  a fragmented file gets a FAT chain;
* deleting clears the in-use bit of the file's directory entries and its bits in
  the allocation bitmap. The FAT chain and the data are left alone, as Windows does;
* a quick format writes a fresh bitmap, FAT and root directory.
"""
from __future__ import annotations

import hashlib
import struct

from .fatbuilder import SECTOR, TruthFile

_STAMP = ((2015 - 1980) << 25) | (6 << 21) | (14 << 16) | (12 << 11) | (30 << 5)


def _set_checksum(entries: bytes) -> int:
    s = 0
    for i, b in enumerate(entries):
        if i in (2, 3):
            continue
        s = (((s & 1) << 15) | (s >> 1)) + b & 0xFFFF
    return s


def _name_hash(name: str) -> int:
    s = 0
    for b in name.upper().encode("utf-16le"):
        s = (((s & 1) << 15) | (s >> 1)) + b & 0xFFFF
    return s


class _Dir:
    def __init__(self, path: str, clusters: list[int]):
        self.path, self.clusters = path, clusters


class ExFatBuilder:
    fat_type = "exfat"

    def __init__(self, size_mb: int = 128, cluster_size: int = 32768, partition_offset: int = 2048 * SECTOR,
                 clear_fat_on_delete: bool = False):
        self.cl = cluster_size
        self.part = partition_offset
        self.clear_fat = clear_fat_on_delete
        self.size = size_mb * 1024 * 1024
        self.img = bytearray(self.size)
        self.spc = cluster_size // SECTOR
        vol = (self.size - self.part) // SECTOR
        self.vol_sectors = vol
        self.fat_sec = 128
        fatlen = 1
        while True:
            heap = self.fat_sec + fatlen
            heap += -heap % self.spc                      # align the cluster heap
            ncl = (vol - heap) // self.spc
            need = -(-(ncl + 2) * 4 // SECTOR)
            if need <= fatlen:
                break
            fatlen = need
        self.fatlen, self.heap_sec, self.ncl = fatlen, heap, ncl
        self.fat_off = self.part + self.fat_sec * SECTOR
        self.data_off = self.part + heap * SECTOR
        self.owner: dict[int, TruthFile] = {}
        self.truth: dict[str, TruthFile] = {}
        self.history: list[TruthFile] = []
        self._entries: dict[str, tuple[_Dir, int, int]] = {}
        self._format()

    # ---- layout -----------------------------------------------------------
    def _format(self) -> None:
        self.fat = [0] * (self.ncl + 2)
        self.fat[0], self.fat[1] = 0xFFFFFFF8, 0xFFFFFFFF
        self.used = bytearray(self.ncl + 2)               # one flag per cluster
        self.next_free = 2
        bm_bytes = -(-self.ncl // 8)
        self.bm_clusters = self._alloc(-(-bm_bytes // self.cl), chain=True)
        self.bm_bytes = bm_bytes
        up = self._alloc(1, chain=True)
        table = struct.pack("<128H", *range(128))
        self.img[self.cluster_off(up[0]):self.cluster_off(up[0]) + len(table)] = table
        root = self._alloc(1, chain=True)
        self.root = root[0]
        off = self.cluster_off(self.root)
        self.img[off:off + self.cl] = bytes(self.cl)
        e = bytearray(96)
        e[0] = 0x83
        e[1] = 6
        e[2:14] = "CARD  ".encode("utf-16le")
        e[32] = 0x81
        struct.pack_into("<IQ", e, 32 + 20, self.bm_clusters[0], bm_bytes)
        e[64] = 0x82
        struct.pack_into("<I", e, 64 + 4, sum(table) & 0xFFFFFFFF)
        struct.pack_into("<IQ", e, 64 + 20, up[0], len(table))
        self.img[off:off + 96] = e
        self._dirs = {"": _Dir("", [self.root])}
        bs = bytearray(SECTOR)
        bs[0:3] = b"\xeb\x76\x90"
        bs[3:11] = b"EXFAT   "
        struct.pack_into("<QQIIIIIIHH", bs, 64, self.part // SECTOR, self.vol_sectors, self.fat_sec, self.fatlen,
                         self.heap_sec, self.ncl, self.root, 0x12345678, 0x0100, 0)
        bs[108] = 9
        bs[109] = self.spc.bit_length() - 1
        bs[110] = 1
        bs[111] = 0x80
        bs[510:512] = b"\x55\xaa"
        self.img[self.part:self.part + SECTOR] = bs
        self.img[self.part + 12 * SECTOR:self.part + 13 * SECTOR] = bs
        if self.part:
            mbr = bytearray(SECTOR)
            mbr[446:462] = struct.pack("<B3sB3sII", 0, b"\0\0\0", 0x07, b"\0\0\0",
                                       self.part // SECTOR, min(self.vol_sectors, 0xFFFFFFFF))
            mbr[510:512] = b"\x55\xaa"
            self.img[0:SECTOR] = mbr

    def cluster_off(self, c: int) -> int:
        return self.data_off + (c - 2) * self.cl

    def _alloc(self, n: int, chain: bool = False) -> list[int]:
        out: list[int] = []
        c, scanned = self.next_free, 0
        while len(out) < n:
            if scanned > self.ncl:
                raise OSError("card full")
            if c >= self.ncl + 2:
                c = 2
            if not self.used[c]:
                out.append(c)
                self.used[c] = 1
                old = self.owner.pop(c, None)
                if old is not None:
                    old.overwritten.add(old.clusters.index(c))
            c += 1
            scanned += 1
        self.next_free = c
        if chain:
            self._chain(out)
        return out

    def _chain(self, cs: list[int]) -> None:
        for a, b in zip(cs, cs[1:]):
            self.fat[a] = b
        self.fat[cs[-1]] = 0xFFFFFFFF

    # ---- directories ------------------------------------------------------
    def _slots(self, d: _Dir) -> list[int]:
        return [o for c in d.clusters for o in range(self.cluster_off(c), self.cluster_off(c) + self.cl, 32)]

    def _add_set(self, d: _Dir, name: str, attr: int, start: int, size: int, contiguous: bool) -> tuple[int, int]:
        u = name.encode("utf-16le")
        nname = -(-len(name) // 15)
        ents = bytearray(32 * (2 + nname))
        ents[0] = 0x85
        ents[1] = 1 + nname
        struct.pack_into("<H", ents, 4, attr)
        struct.pack_into("<III", ents, 8, _STAMP, _STAMP, _STAMP)
        ents[32] = 0xC0
        ents[33] = 0x03 if contiguous else 0x01
        ents[35] = len(name)
        struct.pack_into("<H", ents, 36, _name_hash(name))
        struct.pack_into("<Q", ents, 40, size)
        struct.pack_into("<IQ", ents, 52, start, size)
        for i in range(nname):
            o = 64 + 32 * i
            ents[o] = 0xC1
            part = u[i * 30:(i + 1) * 30]
            ents[o + 2:o + 2 + len(part)] = part
        struct.pack_into("<H", ents, 2, _set_checksum(ents))
        need = len(ents) // 32
        while True:
            slots = self._slots(d)
            first = next((i for i, o in enumerate(slots) if self.img[o] == 0 and i + need <= len(slots)), None)
            if first is not None:
                break
            (c,) = self._alloc(1)
            self._chain(d.clusters + [c])
            d.clusters.append(c)
            self.img[self.cluster_off(c):self.cluster_off(c) + self.cl] = bytes(self.cl)
        for k in range(need):
            o = slots[first + k]
            self.img[o:o + 32] = ents[32 * k:32 * k + 32]
        return first, need

    def mkdir(self, path: str) -> None:
        path = path.strip("/")
        if path in self._dirs:
            return
        parent, _, name = path.rpartition("/")
        if parent:
            self.mkdir(parent)
        (c,) = self._alloc(1, chain=True)
        self.img[self.cluster_off(c):self.cluster_off(c) + self.cl] = bytes(self.cl)
        self._add_set(self._dirs[parent], name, 0x10, c, self.cl, contiguous=False)
        self._dirs[path] = _Dir(path, [c])

    # ---- files ------------------------------------------------------------
    def write(self, path: str, data: bytes) -> TruthFile:
        path = path.strip("/")
        parent, _, name = path.rpartition("/")
        if parent:
            self.mkdir(parent)
        chain = self._alloc(max(1, -(-len(data) // self.cl)))
        contiguous = all(b == a + 1 for a, b in zip(chain, chain[1:]))
        if not contiguous:
            self._chain(chain)
        for k, c in enumerate(chain):
            piece = data[k * self.cl:(k + 1) * self.cl]
            self.img[self.cluster_off(c):self.cluster_off(c) + len(piece)] = piece
        t = TruthFile(path, len(data), chain, hashlib.sha256(data).hexdigest())
        for c in chain:
            self.owner[c] = t
        d = self._dirs[parent]
        self._entries[path] = (d, *self._add_set(d, name, 0x20, chain[0], len(data), contiguous))
        self.truth[path] = t
        self.history.append(t)
        return t

    def delete(self, path: str) -> None:
        path = path.strip("/")
        d, first, count = self._entries.pop(path)
        slots = self._slots(d)
        for k in range(count):
            self.img[slots[first + k]] &= 0x7F
        t = self.truth[path]
        for c in t.clusters:
            self.used[c] = 0
            if self.clear_fat:
                self.fat[c] = 0
        t.deleted = True

    def delete_all(self) -> None:
        for p in list(self._entries):
            self.delete(p)

    def quick_format(self) -> None:
        for t in self.truth.values():
            t.deleted = True
        self._entries.clear()
        self._format()

    # ---- output -----------------------------------------------------------
    def tobytes(self) -> bytes:
        raw = struct.pack(f"<{len(self.fat)}I", *self.fat)
        self.img[self.fat_off:self.fat_off + len(raw)] = raw
        bm = bytearray(self.bm_bytes)
        for c in range(2, self.ncl + 2):
            if self.used[c]:
                bm[(c - 2) >> 3] |= 1 << ((c - 2) & 7)
        for k, c in enumerate(self.bm_clusters):
            piece = bm[k * self.cl:(k + 1) * self.cl]
            self.img[self.cluster_off(c):self.cluster_off(c) + len(piece)] = piece
        return bytes(self.img)

    def save(self, path) -> None:
        with open(path, "wb") as f:
            f.write(self.tobytes())
