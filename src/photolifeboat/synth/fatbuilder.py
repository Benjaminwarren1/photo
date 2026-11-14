"""Builds FAT16/FAT32 card images in memory for tests, and remembers the truth.

This is a deliberately small FAT *writer* that behaves like a camera or
Windows does in the ways that matter for recovery:

* clusters are handed out next-fit, so new files fill the holes that deleted
  files left behind and become fragmented the way real files do;
* deleting a file marks its directory entries with 0xE5 and zeroes its FAT
  chain, but leaves the data alone;
* on FAT32 a delete can zero the high word of the start cluster, as Windows
  often does;
* a quick format wipes the FATs and the root directory only.

For every file ever written it records the clusters it used, its bytes and
whether any of them were later overwritten, so tests know every right answer.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field

SECTOR = 512
_DATE = ((2015 - 1980) << 9) | (6 << 5) | 14
_TIME = (12 << 11) | (30 << 5)


@dataclass
class TruthFile:
    path: str
    size: int
    clusters: list[int]          # cluster numbers as the FAT counts them (first is 2)
    sha256: str
    deleted: bool = False
    overwritten: set[int] = field(default_factory=set)  # indexes into clusters

    @property
    def fragmented(self) -> bool:
        return any(b != a + 1 for a, b in zip(self.clusters, self.clusters[1:]))

    @property
    def recoverable(self) -> bool:
        return not self.overwritten


@dataclass
class _Dir:
    path: str
    clusters: list[int]          # empty for the fixed FAT16 root
    fixed_off: int = 0
    fixed_size: int = 0


def _lfn_checksum(short11: bytes) -> int:
    s = 0
    for b in short11:
        s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
    return s


class FatBuilder:
    def __init__(self, size_mb: int = 64, fat_type: int = 16, cluster_size: int = 4096,
                 partition_offset: int = 63 * SECTOR, zero_high_on_delete: bool = False):
        assert fat_type in (16, 32)
        assert cluster_size % SECTOR == 0
        self.fat_type = fat_type
        self.cl = cluster_size
        self.part = partition_offset
        self.zero_high = zero_high_on_delete
        self.size = size_mb * 1024 * 1024
        self.img = bytearray(self.size)
        self.spc = cluster_size // SECTOR
        tot = (self.size - self.part) // SECTOR
        self.tot_sectors = tot
        if fat_type == 16:
            self.reserved, self.root_entries = 1, 512
            root_secs = self.root_entries * 32 // SECTOR
            fatsz = 1
            while True:
                ncl = (tot - self.reserved - root_secs - 2 * fatsz) // self.spc
                need = -(-(ncl + 2) * 2 // SECTOR)
                if need <= fatsz:
                    break
                fatsz = need
            assert 4085 <= ncl < 65525, f"{ncl} clusters is not FAT16; change size or cluster size"
        else:
            self.reserved, self.root_entries = 32, 0
            root_secs = 0
            fatsz = 1
            while True:
                ncl = (tot - self.reserved - 2 * fatsz) // self.spc
                need = -(-(ncl + 2) * 4 // SECTOR)
                if need <= fatsz:
                    break
                fatsz = need
        self.fatsz, self.ncl = fatsz, ncl
        self.fat_off = self.part + self.reserved * SECTOR
        self.root_off = self.fat_off + 2 * fatsz * SECTOR
        self.data_off = self.root_off + root_secs * SECTOR
        self.fat = [0] * (ncl + 2)
        self.fat[0], self.fat[1] = 0x0FFFFFF8, 0x0FFFFFFF
        self.next_free = 2
        self.owner: dict[int, TruthFile] = {}
        self.truth: dict[str, TruthFile] = {}
        self.history: list[TruthFile] = []
        self._dirs: dict[str, _Dir] = {}
        self._entries: dict[str, tuple[_Dir, int, int]] = {}  # path -> (dir, first slot, slot count)
        self._format_structures()

    # ---- layout -----------------------------------------------------------
    def _format_structures(self) -> None:
        if self.part:
            mbr = bytearray(SECTOR)
            ptype = 0x06 if self.fat_type == 16 else 0x0C
            mbr[446:462] = struct.pack("<B3sB3sII", 0, b"\0\0\0", ptype, b"\0\0\0",
                                       self.part // SECTOR, self.tot_sectors)
            mbr[510:512] = b"\x55\xaa"
            self.img[0:SECTOR] = mbr
        bs = bytearray(SECTOR)
        bs[0:3] = b"\xeb\x3c\x90"
        bs[3:11] = b"MSDOS5.0"
        struct.pack_into("<HBHBHHBHHHII", bs, 11, SECTOR, self.spc, self.reserved, 2,
                         self.root_entries, self.tot_sectors if self.tot_sectors < 65536 else 0,
                         0xF8, self.fatsz if self.fat_type == 16 else 0, 63, 255,
                         self.part // SECTOR, self.tot_sectors if self.tot_sectors >= 65536 else 0)
        if self.fat_type == 16:
            bs[36:38] = b"\x80\x00"
            bs[38] = 0x29
            bs[43:54] = b"NO NAME    "
            bs[54:62] = b"FAT16   "
        else:
            struct.pack_into("<IHHIHH", bs, 36, self.fatsz, 0, 0, 2, 1, 6)
            bs[64] = 0x80
            bs[66] = 0x29
            bs[71:82] = b"NO NAME    "
            bs[82:90] = b"FAT32   "
        bs[510:512] = b"\x55\xaa"
        self.img[self.part:self.part + SECTOR] = bs
        if self.fat_type == 32:
            self.img[self.part + 6 * SECTOR:self.part + 7 * SECTOR] = bs
            self.fat[2] = 0x0FFFFFFF
            self._dirs[""] = _Dir("", [2])
            self.next_free = 3
        else:
            self._dirs[""] = _Dir("", [], self.root_off, self.root_entries * 32)

    def cluster_off(self, c: int) -> int:
        return self.data_off + (c - 2) * self.cl

    # ---- allocation -------------------------------------------------------
    def _alloc(self, n: int) -> list[int]:
        out: list[int] = []
        c, scanned = self.next_free, 0
        while len(out) < n:
            if scanned > self.ncl:
                raise OSError("card full")
            if c >= self.ncl + 2:
                c = 2
            if self.fat[c] == 0:
                out.append(c)
                old = self.owner.pop(c, None)
                if old is not None:
                    old.overwritten.add(old.clusters.index(c))
            c += 1
            scanned += 1
        self.next_free = c
        for a, b in zip(out, out[1:]):
            self.fat[a] = b
        self.fat[out[-1]] = 0x0FFFFFFF
        return out

    # ---- directories ------------------------------------------------------
    def _dir_slots(self, d: _Dir):
        if not d.clusters:
            for o in range(d.fixed_off, d.fixed_off + d.fixed_size, 32):
                yield o
        else:
            for c in d.clusters:
                base = self.cluster_off(c)
                for o in range(base, base + self.cl, 32):
                    yield o

    def _free_run(self, d: _Dir, n: int) -> int:
        """Offset index (in slot order) of n unused slots at the end of the directory."""
        while True:
            slots = list(self._dir_slots(d))
            for i, o in enumerate(slots):
                if self.img[o] == 0 and i + n <= len(slots):
                    return i
            if not d.clusters:
                raise OSError("root directory full")
            (c,) = self._alloc(1)
            self.fat[d.clusters[-1]] = c
            d.clusters.append(c)
            self.img[self.cluster_off(c):self.cluster_off(c) + self.cl] = bytes(self.cl)

    def _short_name(self, d: _Dir, name: str) -> tuple[bytes, bool]:
        base, _, ext = name.rpartition(".") if "." in name else (name, "", "")
        ok = (name == name.upper() and len(base) <= 8 and len(ext) <= 3 and base
              and all(ch.isalnum() or ch in "_-~" for ch in base + ext))
        if ok:
            return base.ljust(8).encode("ascii") + ext.ljust(3).encode("ascii"), False
        clean = "".join(ch for ch in base.upper() if ch.isalnum() or ch in "_-") or "FILE"
        e = "".join(ch for ch in ext.upper() if ch.isalnum())[:3].ljust(3)
        used = {bytes(self.img[o:o + 11]) for o in self._dir_slots(d)}
        for n in range(1, 100000):
            tail = f"~{n}"
            cand = (clean[:8 - len(tail)] + tail).ljust(8).encode("ascii") + e.encode("ascii")
            if cand not in used:
                return cand, True
        raise OSError("no short name")

    def _add_entry(self, d: _Dir, name: str, attr: int, start: int, size: int) -> tuple[int, int]:
        short, need_lfn = self._short_name(d, name)
        lfn: list[bytes] = []
        if need_lfn:
            u = name.encode("utf-16le") + b"\0\0"
            if len(u) % 26:
                u += b"\xff" * (26 - len(u) % 26)
            chk = _lfn_checksum(short)
            parts = [u[i:i + 26] for i in range(0, len(u), 26)]
            for i, p in reversed(list(enumerate(parts, 1))):
                seq = i | (0x40 if i == len(parts) else 0)
                lfn.append(bytes([seq]) + p[0:10] + bytes([0x0F, 0, chk]) + p[10:22] + b"\0\0" + p[22:26])
        entry = bytearray(32)
        entry[0:11] = short
        entry[11] = attr
        struct.pack_into("<HHHHHHHI", entry, 14, _TIME, _DATE, _DATE, (start >> 16) & 0xFFFF,
                         _TIME, _DATE, start & 0xFFFF, size)
        first = self._free_run(d, len(lfn) + 1)
        slots = list(self._dir_slots(d))
        for k, raw in enumerate(lfn + [bytes(entry)]):
            o = slots[first + k]
            self.img[o:o + 32] = raw
        return first, len(lfn) + 1

    def mkdir(self, path: str) -> None:
        path = path.strip("/")
        if path in self._dirs:
            return
        parent, _, name = path.rpartition("/")
        self.mkdir(parent) if parent else None
        pd = self._dirs[parent]
        (c,) = self._alloc(1)
        off = self.cluster_off(c)
        self.img[off:off + self.cl] = bytes(self.cl)
        self._add_entry(pd, name, 0x10, c, 0)
        d = _Dir(path, [c])
        self._dirs[path] = d
        pstart = pd.clusters[0] if (pd.clusters and parent) else 0
        for i, (nm, st) in enumerate(((b".          ", c), (b"..         ", pstart))):
            e = bytearray(32)
            e[0:11] = nm
            e[11] = 0x10
            struct.pack_into("<HHHHHHHI", e, 14, _TIME, _DATE, _DATE, (st >> 16) & 0xFFFF,
                             _TIME, _DATE, st & 0xFFFF, 0)
            self.img[off + i * 32:off + (i + 1) * 32] = e

    # ---- files ------------------------------------------------------------
    def write(self, path: str, data: bytes) -> TruthFile:
        (t,) = self.write_interleaved([(path, data)])
        return t

    def write_interleaved(self, files: list[tuple[str, bytes]], chunk_clusters: int = 8) -> list[TruthFile]:
        """Write several files at once, a few clusters at a time each, as a camera
        saving a photo while a video is still recording would."""
        chains: list[list[int]] = [[] for _ in files]
        needs = [max(1, -(-len(d) // self.cl)) for _, d in files]
        while any(len(c) < n for c, n in zip(chains, needs)):
            for i, n in enumerate(needs):
                take = min(chunk_clusters if len(files) > 1 else n, n - len(chains[i]))
                if take <= 0:
                    continue
                got = self._alloc(take)
                if chains[i]:
                    self.fat[chains[i][-1]] = got[0]
                chains[i] += got
        out = []
        for (path, data), chain in zip(files, chains):
            path = path.strip("/")
            parent, _, name = path.rpartition("/")
            self.mkdir(parent) if parent else None
            for k, c in enumerate(chain):
                piece = data[k * self.cl:(k + 1) * self.cl]
                off = self.cluster_off(c)
                self.img[off:off + len(piece)] = piece
            t = TruthFile(path, len(data), chain, hashlib.sha256(data).hexdigest())
            for c in chain:
                self.owner[c] = t
            d = self._dirs[parent]
            first, count = self._add_entry(d, name, 0x20, chain[0], len(data))
            self._entries[path] = (d, first, count)
            self.truth[path] = t
            self.history.append(t)
            out.append(t)
        return out

    def delete(self, path: str) -> None:
        path = path.strip("/")
        d, first, count = self._entries.pop(path)
        slots = list(self._dir_slots(d))
        for k in range(count):
            o = slots[first + k]
            self.img[o] = 0xE5
        if self.fat_type == 32 and self.zero_high:
            o = slots[first + count - 1]
            self.img[o + 20:o + 22] = b"\0\0"
        t = self.truth[path]
        for c in t.clusters:
            self.fat[c] = 0
        t.deleted = True

    def delete_all(self) -> None:
        for p in list(self._entries):
            self.delete(p)

    def quick_format(self) -> None:
        """What 'Format' with Quick ticked does: new empty FATs and root directory."""
        for t in self.truth.values():
            t.deleted = True
        self._entries.clear()
        root = self._dirs[""]
        self.fat = [0] * (self.ncl + 2)
        self.fat[0], self.fat[1] = 0x0FFFFFF8, 0x0FFFFFFF
        if self.fat_type == 32:
            self.fat[2] = 0x0FFFFFFF
            off = self.cluster_off(2)
            self.img[off:off + self.cl] = bytes(self.cl)
            for t in self.history:
                if 2 in t.clusters and self.owner.get(2) is t:
                    t.overwritten.add(t.clusters.index(2))
            self._dirs = {"": _Dir("", [2])}
            self.next_free = 3
        else:
            self.img[root.fixed_off:root.fixed_off + root.fixed_size] = bytes(root.fixed_size)
            self._dirs = {"": root}
            self.next_free = 2

    # ---- output -----------------------------------------------------------
    def tobytes(self) -> bytes:
        if self.fat_type == 16:
            raw = struct.pack(f"<{len(self.fat)}H", *[v & 0xFFFF for v in self.fat])
        else:
            raw = struct.pack(f"<{len(self.fat)}I", *self.fat)
        for k in range(2):
            o = self.fat_off + k * self.fatsz * SECTOR
            self.img[o:o + len(raw)] = raw
        return bytes(self.img)

    def save(self, path) -> None:
        with open(path, "wb") as f:
            f.write(self.tobytes())
