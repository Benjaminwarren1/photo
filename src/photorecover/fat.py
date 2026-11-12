"""Read-only FAT16/FAT32 access to a card image.

The image is opened read-only and memory-mapped; nothing here can write to it.
Geometry comes from the boot sector, never from constants.
"""
from __future__ import annotations

import mmap
import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

SECTOR = 512


class NotFat(Exception):
    pass


@dataclass
class Entry:
    name: str                 # long name if one survived, else the 8.3 name
    short: str
    deleted: bool             # marked 0xE5
    is_dir: bool
    start: int                # start cluster as stored (FAT numbering, first data cluster is 2)
    start_hi_stored: int      # the high word as stored, FAT32 only
    size: int
    where: int                # cluster the entry was found in, 0 for the FAT16 root
    live: bool = False        # reachable from the root through intact FAT chains
    path: str = ""            # folder path, best effort
    mtime: tuple | None = None
    contiguous: bool = False  # exFAT only: recorded as one unbroken run
    in_tree: bool = False     # listed in a folder that the current file system still reaches from its root

    @property
    def ext(self) -> str:
        return self.name.rpartition(".")[2].lower() if "." in self.name else ""


def _open_ro(path) -> mmap.mmap | bytes:
    f = open(path, "rb")
    return mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)


def _looks_like_bpb(b: bytes) -> bool:
    if len(b) < 512 or b[510:512] != b"\x55\xaa" or b[0] not in (0xEB, 0xE9):
        return False
    bps, spc, res, nfat = struct.unpack_from("<HBHB", b, 11)
    return bps in (512, 1024, 2048, 4096) and spc in (1, 2, 4, 8, 16, 32, 64, 128) and res >= 1 and nfat in (1, 2)


def find_partitions(m) -> list[int]:
    """Byte offsets of candidate FAT volumes: a volume at offset 0, or MBR partitions."""
    s0 = bytes(m[0:512])
    if _looks_like_bpb(s0) and s0[3:11] != b"EXFAT   ":
        return [0]
    out = []
    if s0[510:512] == b"\x55\xaa":
        for i in range(4):
            ptype, lba, count = struct.unpack_from("<4xB3xII", s0, 446 + i * 16)
            if ptype and lba and lba * SECTOR + 512 <= len(m):
                out.append(lba * SECTOR)
    return out


_NAME_OK = np.ones(256, bool)
_NAME_OK[:0x20] = False
_NAME_OK[list(b'"*+,/:;<=>?[\\]|')] = False


def _slot_ok(a: np.ndarray) -> np.ndarray:
    """For many 32-byte folder slots at once: could parse_dir accept this slot? (It never
    says no to one that parse_dir would take; it exists to skip photo data quickly.)"""
    attr = a[:, 11]
    lfn = ((attr & 0x3F) == 0x0F) & (a[:, 12] == 0) & (a[:, 26] == 0) & (a[:, 27] == 0)
    first = _NAME_OK[a[:, 0]] | (a[:, 0] == 0xE5)
    plain = ((attr & 0x3F) != 0x0F) & ((attr & 0xC0) == 0) & first & _NAME_OK[a[:, 1:11]].all(axis=1)
    return lfn | plain


class FatVolume:
    def __init__(self, source, partition_offset: int | None = None):
        self.m = _open_ro(source) if isinstance(source, (str, Path)) else source
        offs = [partition_offset] if partition_offset is not None else find_partitions(self.m)
        for off in offs:
            b = bytes(self.m[off:off + 512])
            if _looks_like_bpb(b) and b[3:11] != b"EXFAT   ":
                self.part = off
                break
        else:
            raise NotFat("no FAT16/FAT32 volume found in this image")
        (bps, spc, res, nfat, rootent, tot16, _media, fatsz16, _spt, _heads, _hidden,
         tot32) = struct.unpack_from("<HBHBHHBHHHII", b, 11)
        self.bps, self.cl = bps, bps * spc
        tot = tot16 or tot32
        if fatsz16 == 0:
            self.fat_bits = 32
            fatsz, self.root_cluster = struct.unpack_from("<I4xI", b, 36)
        else:
            fatsz, self.root_cluster = fatsz16, 0
        self.fat_off = self.part + res * bps
        self.fat_size = fatsz * bps
        self.root_off = self.fat_off + nfat * self.fat_size
        self.root_size = rootent * 32
        root_secs = -(-self.root_size // bps)
        self.data_off = self.root_off + root_secs * bps
        avail = min(self.part + tot * bps, len(self.m)) - self.data_off
        self.ncl = max(0, avail // self.cl)          # number of data clusters
        if fatsz16:
            self.fat_bits = 12 if self.ncl < 4085 else 16
        if self.fat_bits == 12:
            raise NotFat("FAT12 volumes (very small cards) are not supported yet")
        dt = "<u2" if self.fat_bits == 16 else "<u4"
        n = min(self.ncl + 2, self.fat_size // (self.fat_bits // 8))
        fat = np.frombuffer(self.m, dtype=dt, count=n, offset=self.fat_off).astype(np.uint32)
        if self.fat_bits == 32:
            fat &= 0x0FFFFFFF
        if n < self.ncl + 2:
            fat = np.concatenate([fat, np.zeros(self.ncl + 2 - n, np.uint32)])
        self.fat = fat
        self.eoc = 0xFFF8 if self.fat_bits == 16 else 0x0FFFFFF8

    @property
    def kind(self) -> str:
        return f"FAT{self.fat_bits}"

    def hint_chain(self, e: "Entry"):
        return None          # FAT16/32 wipe a deleted file's chain; there is nothing to go on

    # ---- clusters ---------------------------------------------------------
    @property
    def max_cluster(self) -> int:
        return self.ncl + 1

    def valid(self, c: int) -> bool:
        return 2 <= c <= self.max_cluster

    def cluster(self, c: int) -> bytes:
        o = self.data_off + (c - 2) * self.cl
        return self.m[o:o + self.cl]

    def read_chain(self, clusters, size: int | None = None) -> bytes:
        d = b"".join(self.cluster(c) for c in clusters)
        return d if size is None else d[:size]

    def read_run(self, start: int, size: int) -> bytes:
        o = self.data_off + (start - 2) * self.cl
        return self.m[o:o + size]

    def chain(self, start: int, limit: int = 1 << 22) -> list[int] | None:
        """Follow the FAT from start. None if the chain is broken."""
        out, c, seen = [], start, set()
        while True:
            if not self.valid(c) or c in seen or len(out) > limit:
                return None
            out.append(c)
            seen.add(c)
            nx = int(self.fat[c])
            if nx >= self.eoc:
                return out
            if nx == 0:
                return None
            c = nx

    def allocated(self) -> np.ndarray:
        """Boolean per cluster number (index = cluster number): in use according to the FAT."""
        return self.fat != 0

    def blank_clusters(self) -> np.ndarray:
        """Boolean per cluster number: every byte the same (never-used or erased space)."""
        out = np.zeros(self.ncl + 2, bool)
        step = max(1, (64 << 20) // self.cl)
        for c0 in range(2, self.max_cluster + 1, step):
            n = min(step, self.max_cluster + 1 - c0)
            a = np.frombuffer(self.m, np.uint8, n * self.cl, self.data_off + (c0 - 2) * self.cl).reshape(n, self.cl)
            out[c0:c0 + n] = (a == a[:, :1]).all(axis=1)
        return out

    # ---- directories ------------------------------------------------------
    def parse_dir(self, buf: bytes, where: int) -> tuple[list[Entry], int, int]:
        """Returns (entries, used_slots, bad_slots) for a block of 32-byte slots."""
        out: list[Entry] = []
        lfn: list[str] = []
        used = bad = 0
        ended = False
        for o in range(0, len(buf) - 31, 32):
            e = buf[o:o + 32]
            if e[0] == 0:
                # The end of the folder. Usually nothing but zeros follows, but some devices leave
                # the entries of files deleted earlier beyond it; those are read as deleted files.
                if not any(buf[o:]):
                    break
                ended, lfn = True, []
                if any(e):
                    bad += 1
                continue
            used += 1
            attr = e[11]
            if attr & 0x3F == 0x0F:
                if e[26:28] != b"\0\0" or e[12] != 0:
                    bad += 1
                    lfn = []
                    continue
                raw = e[1:11] + e[14:26] + e[28:32]
                lfn.insert(0, raw.decode("utf-16le", "replace").split("\0")[0])
                continue
            name, ext = e[0:8], e[8:11]
            deleted = name[0] == 0xE5
            if not self._strict_slot(e):
                bad += 1
                lfn = []
                continue
            body = (name[1:] if deleted else name) + ext
            if attr & 0xC0 or any(c < 0x20 or c in b'"*+,/:;<=>?[\\]|' for c in body):
                bad += 1
                lfn = []
                continue
            hi, lo, size = struct.unpack_from("<H4xHI", e, 20)
            if self.fat_bits != 32:
                hi = 0
            if attr & 0x08:               # volume label
                lfn = []
                continue
            base = ("_" + name[1:].decode("latin1") if deleted else name.decode("latin1")).rstrip()
            deleted = deleted or ended
            x = ext.decode("latin1").rstrip()
            short = base + ("." + x if x else "")
            long = "".join(lfn).rstrip("￿") if lfn else ""
            wt, wd = struct.unpack_from("<HH", e, 22)
            out.append(Entry(name=long or short, short=short, deleted=deleted, is_dir=bool(attr & 0x10),
                             start=lo | (hi << 16), start_hi_stored=hi, size=size, where=where,
                             mtime=((wd >> 9) + 1980, (wd >> 5) & 15, wd & 31, wt >> 11, (wt >> 5) & 63)))
            lfn = []
        return out, used, bad

    strict = False      # set while sweeping data that may not be folders at all

    def _strict_slot(self, e) -> bool:
        """While sweeping: the parts of a folder entry that the format reserves must look right.
        Random data that happens to pass the name checks nearly always fails here."""
        if not self.strict:
            return True
        wd = e[24] | (e[25] << 8)
        return (e[12] & 0xE7) == 0 and e[13] < 200 and 1 <= (wd & 31) and 1 <= ((wd >> 5) & 15) <= 12

    def _walk_live(self):
        """Yields (entries, where_cluster, path) for every directory reachable from the root."""
        todo: list[tuple[list[int] | None, str]] = []
        if self.fat_bits == 32:
            todo.append((self.chain(self.root_cluster) or [self.root_cluster], ""))
        else:
            ents, _, _ = self.parse_dir(self.m[self.root_off:self.root_off + self.root_size], 0)
            yield ents, 0, ""
            todo += [(self.chain(e.start), e.name) for e in ents
                     if e.is_dir and not e.deleted and e.short not in (".", "..")]
        seen: set[int] = set()
        while todo:
            ch, path = todo.pop()
            if not ch or ch[0] in seen:
                continue
            seen.add(ch[0])
            for c in ch:
                ents, _, _ = self.parse_dir(self.cluster(c), c)
                yield ents, c, path
                for e in ents:
                    if e.is_dir and not e.deleted and e.short not in (".", ".."):
                        todo.append((self.chain(e.start), (path + "/" if path else "") + e.name))

    def scan_entries(self, progress=None) -> list[Entry]:
        """Every directory entry on the volume: the live tree, plus any cluster
        anywhere that holds directory data (deleted folders, or folders orphaned
        by a format). Each file entry carries its start cluster and exact size."""
        out: list[Entry] = []
        dir_path: dict[int, str] = {}          # directory start cluster -> path
        seen_clusters: set[int] = set()
        for ents, c, path in self._walk_live():
            seen_clusters.add(c)
            for e in ents:
                e.path = path
                e.in_tree = True
                if e.short in (".", ".."):
                    continue
                if not e.deleted:
                    e.live = e.is_dir or e.size == 0 or self._chain_fits(e)
                if e.is_dir:
                    dir_path.setdefault(e.start, (path + "/" if path else "") + e.name)
                out.append(e)
        # sweep every cluster for directory-shaped data; most are ruled out many at a time
        m, cl = self.m, self.cl
        orphans: list[tuple[int, list[Entry]]] = []
        step = max(1, (32 << 20) // cl)
        for c0 in range(2, self.max_cluster + 1, step):
            n = min(step, self.max_cluster + 1 - c0)
            if progress:
                progress(c0, self.max_cluster)
            spc = max(1, cl // SECTOR)
            sec = cl // spc
            a = np.frombuffer(m, np.uint8, n * cl, self.data_off + (c0 - 2) * cl).reshape(n * spc, sec)
            keep = (a[:, 0] != 0) & _slot_ok(a[:, :32])
            if sec >= 64:
                keep &= (a[:, 32] == 0) | _slot_ok(a[:, 32:64])
            keep = keep.reshape(n, spc)
            for i in np.flatnonzero(keep.any(axis=1)).tolist():
                c = i + c0
                if c in seen_clusters:
                    continue
                o = self.data_off + (c - 2) * cl
                # The whole cluster first. If that is not a folder, any run of sectors inside it
                # that is: a folder's later part can outlive its start, and an older layout's
                # folders do not begin where this layout's clusters do.
                secs = np.flatnonzero(keep[i]).tolist()
                pieces = ([(0, cl)] if secs[0] == 0 else []) + [(j * sec, cl) for j in secs if j] + \
                    [(j * sec, (j + 1) * sec) for j in secs]
                done_to, got = -1, []
                for a0, a1 in pieces:
                    if a0 < done_to:
                        continue
                    buf = m[o + a0:o + a1]
                    self.strict = True
                    try:
                        ents, used, bad = self.parse_dir(buf, c)
                    finally:
                        self.strict = False
                    real = [e for e in ents if e.short not in (".", "..")]
                    dotted = buf[0:11] == b".          " and buf[32:43] == b"..         "
                    if bad or not ents:
                        continue
                    if not dotted and not self._plausible(real, used):
                        continue
                    got += ents
                    done_to = a1
                    if a0 == 0 and a1 == cl:
                        break
                if got:
                    orphans.append((c, got))
        for c, ents in orphans:
            for e in ents:
                if e.is_dir and e.short not in (".", ".."):
                    dir_path.setdefault(e.start, e.name)
        last_path = ""
        for c, ents in orphans:
            if ents and ents[0].short == ".":
                last_path = dir_path.get(c, f"_lost_folder_{c}")
            elif c in dir_path:
                last_path = dir_path[c]
            # a directory that grew past one cluster: its later clusters follow, with no dot entries
            for e in ents:
                if e.short in (".", ".."):
                    continue
                e.path = last_path
                out.append(e)
        # resolve nested orphan paths one level (DCIM/100MEDIA)
        parent_of = {e.start: e.path for e in out if e.is_dir}
        for e in out:
            if not e.live and e.path and "/" not in e.path:
                for st, pth in dir_path.items():
                    if pth == e.path and parent_of.get(st):
                        e.path = parent_of[st] + "/" + e.path
                        break
        # the same entry can be seen twice; keep one
        uniq: dict[tuple, Entry] = {}
        for e in out:
            k = (e.where, e.short, e.start, e.size, e.deleted)
            uniq.setdefault(k, e)
        return list(uniq.values())

    def _chain_fits(self, e: Entry) -> bool:
        ch = self.chain(e.start)
        return ch is not None and len(ch) == max(1, -(-e.size // self.cl))

    def _plausible(self, ents: list[Entry], used: int) -> bool:
        """Is a cluster with no dot entries really directory data? Entropy-coded
        photo data almost never passes the name checks even once, so two clean
        entries with sane start clusters and dates is strong evidence."""
        if len(ents) < 2:
            return False
        ok = 0
        for e in ents:
            y, mo, d, h, mi = e.mtime
            sane_date = 1980 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60
            lo_ok = self.valid(e.start & 0xFFFF) or self.valid(e.start) or e.size == 0
            if sane_date and lo_ok and e.size < (self.ncl + 1) * self.cl:
                ok += 1
        return ok == len(ents)


def open_volume(source):
    """Open whatever file system the image holds: FAT16, FAT32 or exFAT."""
    from .exfat import ExFatVolume, find_exfat
    m = _open_ro(source) if isinstance(source, (str, Path)) else source
    if find_exfat(m) is not None:
        return ExFatVolume(m)
    return FatVolume(m)
