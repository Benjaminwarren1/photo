"""Read-only exFAT access to a card image (cards over 32GB use exFAT).

exFAT is kinder to recovery than FAT16/32. A deleted file's directory entries
keep the start cluster, the exact size and a flag saying whether the file was
one unbroken run. If it was fragmented, its chain in the FAT is normally still
there, because deleting only clears the allocation bitmap.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from .fat import ClusterVolume, Entry, NotFat, _open_ro
from .util import ceil_div


def find_exfat(m) -> int | None:
    """Where the exFAT volume on this image starts: at the very beginning, or in a partition."""
    def is_boot(off: int) -> bool:
        return off + 512 <= len(m) and m[off + 3:off + 11] == b"EXFAT   " and m[off + 510:off + 512] == b"\x55\xaa"

    if is_boot(0):
        return 0
    from .fat import partition_starts
    for off in partition_starts(m):
        if is_boot(off):
            return off
    return None


class ExFatVolume(ClusterVolume):
    """An exFAT file system, read straight from the image."""

    fat_bits = 0            # not a FAT16/FAT32 volume
    kind = "exFAT"

    def __init__(self, source, partition_offset: int | None = None, boot_at: int | None = None):
        self.m = _open_ro(source) if isinstance(source, (str, Path)) else source
        off = partition_offset if partition_offset is not None else find_exfat(self.m)
        if off is None:
            raise NotFat("no exFAT volume found in this image")
        self.part = off
        at = off if boot_at is None else boot_at            # boot_at: the spare copy of a damaged boot sector
        b = bytes(self.m[at:at + 512])
        (_po, _vl, fat_sec, fat_len, heap_sec, ncl, root) = struct.unpack_from("<QQIIIII", b, 64)
        bps_shift, spc_shift = b[108], b[109]
        if not 9 <= bps_shift <= 12 or spc_shift > 25 - bps_shift:
            raise NotFat("exFAT boot sector is damaged")
        self.bps = 1 << bps_shift
        self.cl = self.bps << spc_shift
        self.fat_off = off + fat_sec * self.bps
        self.data_off = off + heap_sec * self.bps
        self.ncl = min(ncl, max(0, (len(self.m) - self.data_off) // self.cl))
        self.root_cluster = root
        n = min(self.ncl + 2, fat_len * self.bps // 4)
        fat = np.frombuffer(self.m, "<u4", n, self.fat_off).astype(np.uint32)
        if n < self.ncl + 2:
            fat = np.concatenate([fat, np.zeros(self.ncl + 2 - n, np.uint32)])
        self.fat = fat
        self._bitmap: np.ndarray | None = None

    # ---- clusters ---------------------------------------------------------
    def chain(self, start: int, limit: int | None = None) -> list[int] | None:
        """Follow the FAT from start, stopping after limit clusters if given. None if the chain is broken."""
        out, c, seen = [], start, set()
        while True:
            if not self.valid(c) or c in seen:
                return None
            out.append(c)
            seen.add(c)
            if limit is not None and len(out) >= limit:
                return out
            nx = int(self.fat[c])
            if nx >= 0xFFFFFFF8:
                return out
            if nx < 2:
                return None
            c = nx

    def allocated(self) -> np.ndarray:
        """In use according to the allocation bitmap."""
        out = np.zeros(self.ncl + 2, bool)
        out[:2] = True
        if self._bitmap is not None:
            bits = np.unpackbits(self._bitmap, bitorder="little")[:self.ncl]
            out[2:2 + len(bits)] = bits.astype(bool)
        return out

    # ---- directories ------------------------------------------------------
    def _parse(self, buf: bytes, where: int) -> tuple[list[Entry], int]:
        """Entry sets in a block of directory data. Returns (entries, bad_slots)."""
        out: list[Entry] = []
        bad = 0
        n = len(buf) // 32
        i = 0
        while i < n:
            t = buf[i * 32]
            if t == 0:
                break
            base = t & 0x7F
            if base == 0x05:                              # File entry, in use (0x85) or deleted (0x05)
                count = buf[i * 32 + 1]
                if not 2 <= count <= 18:
                    bad += 1
                    i += 1
                    continue
                if i + 1 + count > n:
                    i += 1                                # set runs past this block; skip it
                    continue
                s = buf[(i + 1) * 32:(i + 2) * 32]
                if s[0] & 0x7F != 0x40:
                    bad += 1
                    i += 1
                    continue
                attr, = struct.unpack_from("<H", buf, i * 32 + 4)
                (stamp,) = struct.unpack_from("<I", buf, i * 32 + 12)
                flags, name_len = s[1], s[3]
                (start, size) = struct.unpack_from("<IQ", s, 20)
                chars = b""
                ok = True
                for k in range(2, count + 1):
                    e = buf[(i + k) * 32:(i + k + 1) * 32]
                    if e[0] & 0x7F != 0x41:
                        ok = False
                        break
                    chars += e[2:32]
                if not ok or name_len == 0 or name_len * 2 > len(chars):
                    bad += 1
                    i += 1
                    continue
                name = chars[:name_len * 2].decode("utf-16le", "replace")
                deleted = not t & 0x80
                e = Entry(name=name, short=name, deleted=deleted, is_dir=bool(attr & 0x10), start=start,
                          size=size, where=where,
                          mtime=((stamp >> 25) + 1980, (stamp >> 21) & 15, (stamp >> 16) & 31,
                                 (stamp >> 11) & 31, (stamp >> 5) & 63))
                e.contiguous = bool(flags & 0x02)
                out.append(e)
                i += 1 + count
                continue
            if t == 0x81 and self._bitmap is None and where == self.root_cluster:
                (bstart, blen) = struct.unpack_from("<IQ", buf, i * 32 + 20)
                ch = self.chain(bstart)
                if ch:
                    self._bitmap = np.frombuffer(self.read_chain(ch, blen), np.uint8)
            elif base not in (0x01, 0x02, 0x03, 0x20, 0x21, 0x40, 0x41):
                bad += 1
            i += 1
        return out, bad

    def _dir_clusters(self, e: Entry) -> list[int]:
        """The clusters of a folder, from its entry."""
        n = max(1, ceil_div(e.size, self.cl))
        if getattr(e, "contiguous", False):
            return [c for c in range(e.start, e.start + n) if self.valid(c)]
        return self.chain(e.start) or ([e.start] if self.valid(e.start) else [])

    def hint_chain(self, e: Entry) -> list[int] | None:
        """Where the directory entry and the FAT say this file's data is. For a deleted
        file this is a strong hint, to be proved before it is believed."""
        n = max(1, ceil_div(e.size, self.cl))
        if getattr(e, "contiguous", False):
            if self.valid(e.start) and e.start + n - 1 <= self.max_cluster:
                return list(range(e.start, e.start + n))
            return None
        ch = self.chain(e.start, limit=n)
        return ch if ch and len(ch) == n else None

    def scan_entries(self, progress=None) -> list[Entry]:
        """Every entry on the volume: the live tree from the root, plus folder data found anywhere
        else on the card (folders that were deleted, or that a format left behind)."""
        out: list[Entry] = []
        seen: set[int] = set()
        dir_path: dict[int, str] = {}
        todo = [(self.chain(self.root_cluster) or [self.root_cluster], "")]
        while todo:
            clusters, path = todo.pop()
            if not clusters or clusters[0] in seen:
                continue
            for c in clusters:
                seen.add(c)
                ents, _ = self._parse(self.cluster(c), c)
                for e in ents:
                    e.path = path
                    e.in_tree = True          # listed in a folder the card still reaches from its top
                    sub = (path + "/" if path else "") + e.name
                    if e.is_dir:
                        dir_path.setdefault(e.start, sub)
                        if not e.deleted:
                            e.live = True
                            todo.append((self._dir_clusters(e), sub))
                    elif not e.deleted:
                        e.live = True
                    out.append(e)
        alloc = self.allocated()
        # sweep every cluster for directory data left by deleted folders or a format
        orphans = []
        m, cl = self.m, self.cl
        for c in range(2, self.max_cluster + 1):
            if c in seen:
                continue
            o = self.data_off + (c - 2) * cl
            if m[o] & 0x7F not in (0x05, 0x40, 0x41):
                continue
            ents, bad = self._parse(m[o:o + cl], c)
            if not ents or bad:
                continue
            orphans.append((c, ents))
        for c, ents in orphans:
            for e in ents:
                if e.is_dir:
                    dir_path.setdefault(e.start, e.name)
        last, prev = "", -2
        for c, ents in orphans:
            if c in dir_path:
                last = dir_path[c]
            elif c != prev + 1 or not last:      # not simply the next cluster of the same folder
                last = f"_lost_folder_{c}"
            prev = c
            for e in ents:
                e.path = last
                out.append(e)
        for e in out:
            # an in-use entry found outside the live tree is no longer live
            if e.live and not e.is_dir and e.size:
                ch = self.hint_chain(e)
                e.live = bool(ch) and bool(alloc[np.asarray(ch, np.int64)].all())
        uniq: dict[tuple, Entry] = {}
        for e in out:
            uniq.setdefault((e.where, e.name, e.start, e.size, e.deleted), e)
        return list(uniq.values())
