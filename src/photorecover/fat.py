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


def gpt_partitions(m) -> list[tuple[bytes, int, int, str]]:
    """(type, first byte, length in bytes, name) for each entry of a GPT partition table."""
    h = bytes(m[SECTOR:SECTOR + 92])
    if len(h) < 92 or h[:8] != b"EFI PART":
        return []
    lba, count, size = struct.unpack_from("<QII", h, 72)
    out = []
    if not (128 <= size <= 1024 and count <= 256):
        return out
    table = bytes(m[lba * SECTOR:lba * SECTOR + count * size])
    for i in range(len(table) // size):
        e = table[i * size:(i + 1) * size]
        if e[:16] == bytes(16):
            continue
        first, last = struct.unpack_from("<QQ", e, 32)
        if first and last >= first and first * SECTOR < len(m):
            name = e[56:128].decode("utf-16-le", "replace").split("\0")[0]
            out.append((e[:16], first * SECTOR, (last - first + 1) * SECTOR, name))
    return out


def partition_starts(m) -> list[int]:
    """Where every partition begins, from either kind of partition table (the older MBR, with its
    chain of extra "logical" partitions, or GPT)."""
    out: list[int] = []
    s0 = bytes(m[0:SECTOR])
    if _looks_like_bpb(s0) or s0[3:11] == b"EXFAT   ":       # no table: the volume fills the card
        return out
    if len(s0) == SECTOR and s0[510:512] == b"\x55\xaa":
        for i in range(4):
            ptype, lba, count = struct.unpack_from("<4xB3xII", s0, 446 + i * 16)
            if not ptype or not lba or lba * SECTOR + SECTOR > len(m):
                continue
            if ptype in (0x05, 0x0F, 0x85):                     # a container of logical partitions
                base = at = lba
                for _ in range(64):
                    b = bytes(m[at * SECTOR:(at + 1) * SECTOR])
                    if len(b) < SECTOR or b[510:512] != b"\x55\xaa":
                        break
                    t1, l1 = struct.unpack_from("<4xB3xI", b, 446)
                    t2, l2 = struct.unpack_from("<4xB3xI", b, 462)
                    if t1 and l1 and (at + l1) * SECTOR < len(m):
                        out.append((at + l1) * SECTOR)
                    if not t2 or not l2 or (base + l2) * SECTOR >= len(m) or base + l2 <= at:
                        break
                    at = base + l2
            elif ptype != 0xEE:
                out.append(lba * SECTOR)
    out += [p[1] for p in gpt_partitions(m)]
    return sorted(set(out))


def find_partitions(m) -> list[int]:
    """Byte offsets of candidate FAT volumes: a volume at offset 0, or one in a partition."""
    s0 = bytes(m[0:512])
    if _looks_like_bpb(s0) and s0[3:11] != b"EXFAT   ":
        return [0]
    return partition_starts(m)


def all_volumes(m) -> list[tuple[str, int]]:
    """Every FAT or exFAT volume the partition table leads to: ("fat" or "exfat", where it starts)."""
    out = []
    for off in sorted({0, *partition_starts(m)}):
        b = bytes(m[off:off + SECTOR])
        if len(b) < SECTOR or b[510:512] != b"\x55\xaa":
            continue
        if b[3:11] == b"EXFAT   ":
            out.append(("exfat", off))
        elif _looks_like_bpb(b):
            out.append(("fat", off))
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


SURVEY_MAGIC = dict(jpg=(0, b"\xff\xd8\xff"), png=(0, b"\x89PNG"), mp4=(4, b"ftyp"), zip=(0, b"PK\x03\x04"),
                    gif=(0, b"GIF8"), tif=(0, b"II*\0"), tifm=(0, b"MM\0*"), riff=(0, b"RIFF"))


class Survey:
    """One read of the whole card, 512 bytes at a time, noting everything the later steps want to
    know about each sector: is it blank, does a known kind of file begin there, and could it be
    the start of folder data. On a big card each full read takes minutes, so it is done once."""

    def __init__(self, m, progress=None, workers: int | None = None):
        import os
        from concurrent.futures import ThreadPoolExecutor
        n = len(m) // SECTOR
        per = (32 << 20) // SECTOR
        starts = list(range(0, n, per))

        def one(s0: int):
            k = min(per, n - s0)
            a = np.frombuffer(m, np.uint8, k * SECTOR, s0 * SECTOR).reshape(k, SECTOR)
            same = (a[:, 0] == a[:, 1]) & (a[:, 0] == a[:, 255]) & (a[:, 0] == a[:, 511])
            idx = np.flatnonzero(same)
            if len(idx):
                rows = a if len(idx) == k else a[idx]
                same[idx] = (rows == rows[:, :1]).all(axis=1)
            sig = {}
            for kind, (o, magic) in SURVEY_MAGIC.items():
                ok = a[:, o] == magic[0]
                for i in range(1, len(magic)):
                    ok &= a[:, o + i] == magic[i]
                sig[kind] = np.flatnonzero(ok) + s0
            # a transport stream: the same marker byte at the start of every 188- or 192-byte packet
            sig["ts"] = np.flatnonzero((a[:, 0] == 0x47) & (a[:, 188] == 0x47) & (a[:, 376] == 0x47)
                                       | (a[:, 4] == 0x47) & (a[:, 196] == 0x47) & (a[:, 388] == 0x47)) + s0
            keep = (a[:, 0] != 0) & ~same & _slot_ok(a[:, :32])
            keep &= (a[:, 32] == 0) | _slot_ok(a[:, 32:64])
            return np.packbits(same), sig, np.flatnonzero(keep) + s0

        parts = []
        with ThreadPoolExecutor(workers or min(8, os.cpu_count() or 2)) as pool:
            for i, r in enumerate(pool.map(one, starts)):
                parts.append(r)
                if progress:
                    progress(i + 1, len(starts))
        self.n = n
        self._per = per
        self._same = [p[0] for p in parts]                       # packed bits, one array per stretch
        self.sigs = {kind: (np.concatenate([p[1][kind] for p in parts]) if parts else np.zeros(0, np.int64))
                     for kind in (*SURVEY_MAGIC, "ts")}
        self.dirs = np.concatenate([p[2] for p in parts]) if parts else np.zeros(0, np.int64)

    def blank(self, s0: int, count: int) -> np.ndarray:
        """Boolean per sector for sectors s0 .. s0+count: every byte in it the same."""
        out = np.zeros(count, bool)
        for i in range(s0 // self._per, min(len(self._same), -(-(s0 + count) // self._per))):
            base = i * self._per
            bits = np.unpackbits(self._same[i]).astype(bool)[:min(self._per, self.n - base)]
            a, b = max(s0, base), min(s0 + count, base + len(bits))
            if b > a:
                out[a - s0:b - s0] = bits[a - base:b - base]
        return out

    def starts(self, kind: str, data_off: int, cl: int, max_cluster: int) -> np.ndarray:
        """Cluster numbers, in a layout, that begin the way this kind of file begins."""
        pos = self.sigs[kind].astype(np.int64) * SECTOR - data_off
        pos = pos[(pos >= 0) & (pos % cl == 0)]
        c = pos // cl + 2
        return c[c <= max_cluster]


def blank_from_survey(vol) -> np.ndarray | None:
    """blank_clusters() answered from the survey, when the layout sits on whole sectors."""
    sv = getattr(vol, "survey", None)
    if sv is None or vol.data_off % SECTOR or vol.cl % SECTOR:
        return None
    spc, out = vol.cl // SECTOR, np.zeros(vol.ncl + 2, bool)
    step = max(1, (64 << 20) // spc)
    for c0 in range(2, vol.max_cluster + 1, step):
        n = min(step, vol.max_cluster + 1 - c0)
        bits = sv.blank(vol.data_off // SECTOR + (c0 - 2) * spc, n * spc)
        out[c0:c0 + n] = bits.reshape(n, spc).all(axis=1)
    return out


class FatVolume:
    survey: Survey | None = None

    def __init__(self, source, partition_offset: int | None = None, boot_at: int | None = None):
        self.m = _open_ro(source) if isinstance(source, (str, Path)) else source
        offs = [partition_offset] if partition_offset is not None else find_partitions(self.m)
        for off in offs:
            at = off if boot_at is None else boot_at       # boot_at: a spare copy of a damaged boot sector
            b = bytes(self.m[at:at + 512])
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
        known = blank_from_survey(self)
        if known is not None:
            return known
        out = np.zeros(self.ncl + 2, bool)
        step = max(1, (64 << 20) // self.cl)
        for c0 in range(2, self.max_cluster + 1, step):
            n = min(step, self.max_cluster + 1 - c0)
            a = np.frombuffer(self.m, np.uint8, n * self.cl, self.data_off + (c0 - 2) * self.cl).reshape(n, self.cl)
            out[c0:c0 + n] = (a == a[:, :1]).all(axis=1)
        return out

    # ---- directories ------------------------------------------------------
    def parse_dir(self, buf: bytes, where: int, stop_on_bad: bool = False) -> tuple[list[Entry], int, int]:
        """Returns (entries, used_slots, bad_slots) for a block of 32-byte slots."""
        out: list[Entry] = []
        lfn: list[str] = []
        used = bad = 0
        ended = False
        for o in range(0, len(buf) - 31, 32):
            if bad and stop_on_bad:
                break
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
            if not long and e[12] & 0x18:      # Windows stores an all-lower-case short name as flags
                shown = (base.lower() if e[12] & 0x08 else base) + ("." + (x.lower() if e[12] & 0x10 else x) if x else "")
                long = shown
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
        spc = max(1, cl // SECTOR)
        sec = cl // spc
        sv = self.survey if (self.survey is not None and sec == SECTOR and self.data_off % SECTOR == 0) else None
        step = max(1, (32 << 20) // cl)
        for c0 in ([2] if sv is not None else range(2, self.max_cluster + 1, step)):
            if sv is not None:
                # the survey has already picked out the sectors that could begin folder data
                rel = sv.dirs - self.data_off // SECTOR
                rel = rel[(rel >= 0) & (rel < self.ncl * spc)]
                n = self.ncl
                rows, cols = rel // spc, rel % spc
                keep_rows = np.unique(rows)
                by_row = dict(zip(keep_rows.tolist(), np.split(cols, np.flatnonzero(np.diff(rows)) + 1))) if len(rows) else {}
            else:
                n = min(step, self.max_cluster + 1 - c0)
                if progress:
                    progress(c0, self.max_cluster)
                a = np.frombuffer(m, np.uint8, n * cl, self.data_off + (c0 - 2) * cl).reshape(n * spc, sec)
                keep = (a[:, 0] != 0) & _slot_ok(a[:, :32])
                if sec >= 64:
                    keep &= (a[:, 32] == 0) | _slot_ok(a[:, 32:64])
                keep = keep.reshape(n, spc)
                keep_rows = np.flatnonzero(keep.any(axis=1))
                by_row = {i: np.flatnonzero(keep[i]) for i in keep_rows.tolist()}
            for i in keep_rows.tolist():
                c = i + c0
                if c in seen_clusters:
                    continue
                o = self.data_off + (c - 2) * cl
                # The whole cluster first. If that is not a folder, any run of sectors inside it
                # that is: a folder's later part can outlive its start, and an older layout's
                # folders do not begin where this layout's clusters do.
                secs = by_row[i].tolist()
                pieces = ([(0, cl)] if secs[0] == 0 else []) + [(j * sec, cl) for j in secs if j] + \
                    [(j * sec, (j + 1) * sec) for j in secs]
                done_to, got = -1, []
                for a0, a1 in pieces:
                    if a0 < done_to:
                        continue
                    buf = m[o + a0:o + a1]
                    self.strict = True
                    try:
                        ents, used, bad = self.parse_dir(buf, c, stop_on_bad=True)
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
        # Name each lost folder and rebuild its whole path. A folder's own first cluster still says
        # where it is (".") and where its parent is (".."); its name is in the parent's entry for it.
        # On FAT32 a delete often blanks the upper half of the start cluster in that entry, so a
        # match on the lower half is accepted when the entry sits in the right parent.
        root = self.root_cluster if self.fat_bits == 32 else 0
        parent: dict[int, int] = {}
        home: dict[int, int] = {}              # any cluster of a lost folder -> that folder's first cluster
        first = None
        for c, ents in orphans:
            if ents and ents[0].short == ".":
                first = c
                up = next((e.start for e in ents[:2] if e.short == ".."), 0)
                parent[c] = 0 if up in (0, root) else up
            if first is not None:
                home[c] = first
        named: dict[int, list[Entry]] = {}
        for e in out + [e for _, ents in orphans for e in ents]:
            if e.is_dir and e.short not in (".", ".."):
                named.setdefault(e.start, []).append(e)
                if e.deleted and self.fat_bits == 32 and e.start >> 16 == 0:
                    named.setdefault(-(e.start & 0xFFFF) - 1, []).append(e)
        live_path = dict(dir_path)
        full: dict[int, str] = {}

        def inside(e: Entry, up: int) -> bool:
            if up == 0:
                return e.in_tree and e.path == ""
            return home.get(e.where, e.where) == up

        def path_of(c: int, depth: int = 0) -> str:
            if c in full:
                return full[c]
            if c in live_path and c not in parent:
                return live_path[c]
            up = parent.get(c)
            exact, low = named.get(c, []), named.get(-(c & 0xFFFF) - 1, []) if c >> 16 else []
            pick = None
            if up is not None:
                pick = next((e for e in exact + low if inside(e, up)), None)
            pick = pick or (exact[0] if exact else None) or (low[0] if len(low) == 1 else None)
            name = pick.name if pick else f"_lost_folder_{c}"
            if up is None and pick is not None and pick.in_tree:
                up_path = pick.path
            elif up and depth < 32:
                full[c] = name                       # guards against a loop of folders
                up_path = path_of(up, depth + 1)
            else:
                up_path = ""
            full[c] = (up_path + "/" if up_path else "") + name
            return full[c]

        last_path = ""
        for c, ents in orphans:
            if ents and ents[0].short == ".":
                last_path = path_of(c)
            elif c in named or c in live_path:
                last_path = path_of(c)
            # a directory that grew past one cluster: its later clusters follow, with no dot entries
            for e in ents:
                if e.short in (".", ".."):
                    continue
                e.path = last_path
                out.append(e)
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


def find_spare_boot(m, span: int = 64 << 20) -> tuple[str, int, int] | None:
    """When the start of a card is damaged (the usual reason a computer says it "needs formatting"),
    look for a boot sector that survives: FAT32 and exFAT each keep a spare copy a few sectors in,
    and a boot sector also survives when only the partition table in front of it is lost.
    Returns (kind, where the volume starts, where the usable boot sector is)."""
    n = min(span, len(m)) // SECTOR
    if n < 1:
        return None
    a = np.frombuffer(m, np.uint8, n * SECTOR).reshape(n, SECTOR)
    cand = np.flatnonzero((a[:, 510] == 0x55) & (a[:, 511] == 0xAA) & ((a[:, 0] == 0xEB) | (a[:, 0] == 0xE9)))
    for i in cand.tolist():
        at = i * SECTOR
        b = bytes(m[at:at + SECTOR])
        if b[3:11] == b"EXFAT   ":
            (start,) = struct.unpack_from("<Q", b, 64)
            for vol in (start * SECTOR, at - 12 * SECTOR, at):
                if 0 <= vol <= at and at - vol in (0, 12 * SECTOR):
                    return "exfat", vol, at
        elif _looks_like_bpb(b):
            (hidden,) = struct.unpack_from("<I", b, 28)
            fat32 = struct.unpack_from("<H", b, 22)[0] == 0
            spare = struct.unpack_from("<H", b, 50)[0] * SECTOR if fat32 else 0
            for vol in (hidden * SECTOR, at - spare, at):
                if 0 <= vol <= at and at - vol in (0, spare):
                    return "fat", vol, at
    return None


def open_volume(source):
    """Open whatever file system the image holds: FAT16, FAT32 or exFAT."""
    from .exfat import ExFatVolume, find_exfat
    m = _open_ro(source) if isinstance(source, (str, Path)) else source
    try:
        if find_exfat(m) is not None:
            return ExFatVolume(m)
        return FatVolume(m)
    except NotFat:
        found = find_spare_boot(m)
        if found is None:
            raise
        kind, part, at = found
        vol = ExFatVolume(m, part, boot_at=at) if kind == "exfat" else FatVolume(m, part, boot_at=at)
        vol.repaired = True
        return vol
