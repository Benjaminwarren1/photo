"""Cards that were formatted with different settings than they had before.

A format writes a new, empty file system at the front of the card. The old folders
often survive further in, but the addresses in them (start clusters) were counted in
the *old* layout: a different cluster size, and a data area that began somewhere else.
Reading them with the new layout lands on the wrong bytes every time.

Here the old layout is worked out from evidence on the card itself. Each surviving
folder entry says "a photo starts at cluster N"; the card says where photos actually
start. Only the true cluster size and data offset make the two agree for many files
at once.
"""
from __future__ import annotations

import numpy as np

from .fat import SECTOR, Entry, FatVolume

CLUSTER_SIZES = [512 << k for k in range(8)]          # 512 bytes .. 64 KB
JPEG = ("jpg", "jpeg", "jpe")
MP4 = ("mp4", "mov", "3gp", "m4v")


class _Sectors(FatVolume):
    """The whole image seen as 512-byte "clusters", so folder data can be found
    wherever it lies, whatever the layout used to be."""

    def __init__(self, m):
        self.m, self.part = m, 0
        self.bps = self.cl = SECTOR
        self.data_off = 2 * SECTOR      # so that "cluster" c is simply sector c
        self.root_off = self.fat_off = 0
        self.root_size = self.fat_size = self.root_cluster = 0
        self.ncl = len(m) // SECTOR - 2
        self.fat_bits = 32            # keep the high word; it is judged later
        self.fat = np.zeros(0, np.uint32)
        self.eoc = 0x0FFFFFF8

    def _walk_live(self):
        return iter(())


class LegacyFatVolume(FatVolume):
    """The card as it was laid out before it was reformatted. There is no FAT to go
    on (the format replaced it), so every file is treated like a deleted one."""

    def __init__(self, m, cluster_size: int, data_off: int, current: FatVolume | None = None):
        self.m, self.part = m, 0
        self.bps, self.cl = SECTOR, cluster_size
        self.data_off = self.root_off = self.fat_off = data_off
        self.root_size = self.fat_size = self.root_cluster = 0
        self.ncl = max(0, (len(m) - data_off) // cluster_size)
        self.fat_bits = 16 if self.ncl < 65525 else 32
        self.eoc = 0xFFF8 if self.fat_bits == 16 else 0x0FFFFFF8
        self.fat = np.zeros(self.ncl + 2, np.uint32)
        if current is not None:
            # whatever the new file system is using now is not old data any more
            self._claim(0, current.data_off)
            used = np.flatnonzero(current.allocated()[2:]) + 2
            for c in used[used <= current.max_cluster]:
                o = current.data_off + (int(c) - 2) * current.cl
                self._claim(o, o + current.cl)

    def _claim(self, a: int, b: int) -> None:
        c0 = max(2, (a - self.data_off) // self.cl + 2)
        c1 = min(self.ncl + 2, -(-(b - self.data_off) // self.cl) + 2)
        if c1 > c0:
            self.fat[c0:c1] = self.eoc

    @property
    def kind(self) -> str:
        return f"FAT{self.fat_bits}, as the card was before it was reformatted"

    def _walk_live(self):
        return iter(())


MAGIC = dict(jpg=(0, b"\xff\xd8\xff"), png=(0, b"\x89PNG"), mp4=(4, b"ftyp"), zip=(0, b"PK\x03\x04"), gif=(0, b"GIF8"))


def _signature_maps(m) -> dict[str, np.ndarray]:
    """For each kind of file, the 512-byte sectors (in order) that begin the way that kind begins."""
    n = len(m) // SECTOR
    step = (64 << 20) // SECTOR
    found = {kind: [] for kind in MAGIC}
    for s0 in range(0, n, step):
        k = min(step, n - s0)
        head = np.frombuffer(m, np.uint8, k * SECTOR, s0 * SECTOR).reshape(k, SECTOR)[:, :8]
        for kind, (o, magic) in MAGIC.items():
            ok = head[:, o] == magic[0]
            for i in range(1, len(magic)):
                ok &= head[:, o + i] == magic[i]
            found[kind].append(np.flatnonzero(ok) + s0)
    return {kind: np.concatenate(v) if v else np.zeros(0, np.int64) for kind, v in found.items()}


def _kind(e: Entry) -> str | None:
    x = e.ext
    return ("jpg" if x in JPEG else "mp4" if x in MP4 else "zip" if x in ("zip", "apk", "docx", "xlsx")
            else x if x in ("png", "gif") else None)


def _votes(entries: list[Entry], sigs: dict[str, np.ndarray], total_sectors: int):
    """Returns (testable, {(cluster_size, data_off): files that start where they should})
    for the best data offset at each cluster size."""
    want = sorted({(e.start, _kind(e))
                   for e in entries if not e.is_dir and e.size > 0 and _kind(e)})
    want = [(n, k) for n, k in want if n >= 2]
    if len(want) > 4000:
        want = want[::len(want) // 4000 + 1]
    span = min(total_sectors, (256 << 20) // SECTOR)      # the data area begins within the first 256 MB
    out = {}
    for cs in CLUSTER_SIZES:
        acc = np.zeros(span, np.int32)
        spc = cs // SECTOR
        for n, k in want:
            s = (n - 2) * spc
            p = sigs[k]
            i, j = np.searchsorted(p, (s, s + span))
            acc[p[i:j] - s] += 1
        best = int(acc.argmax())
        out[(cs, best * SECTOR)] = int(acc[best])
    return len(want), out


def find_old_layout(vol: FatVolume, entries: list[Entry], log=None):
    """If the card's surviving folders belong to an earlier layout, returns a volume
    for that layout; otherwise None. `entries` is what the current layout found."""
    m = vol.m
    total = len(m) // SECTOR

    def hits_now() -> tuple[int, int]:
        n = ok = 0
        for e in lost:
            k = _kind(e)
            if e.is_dir or e.size <= 0 or not k:
                continue
            n += 1
            o, magic = MAGIC[k]
            tries = [e.start]
            if getattr(vol, "fat_bits", 0) == 32:     # deleting on FAT32 often zeroes the high half
                tries += [(e.start & 0xFFFF) | (hi << 16) for hi in range((vol.max_cluster >> 16) + 1)]
            if any(vol.valid(c) and bytes(vol.cluster(c)[o:o + len(magic)]) == magic for c in tries):
                ok += 1
        return n, ok

    lost = [e for e in entries if not e.live]
    n_now, ok_now = hits_now()
    if n_now:
        if ok_now * 2 >= n_now:
            return None                     # the layout the card has now explains its files
    elif not _looks_reformatted(vol):
        return None
    if log:
        log("The card's folders do not match its current format; working out how it was laid out before")
    return _infer(m, vol, ok_now, log)


def _infer(m, vol: FatVolume | None, ok_now: int = 0, log=None):
    """Work out an earlier layout from the folders and photo starts found on the card."""
    total = len(m) // SECTOR
    sigs = _signature_maps(m)
    old_entries = _Sectors(m).scan_entries()
    testable, votes = _votes(old_entries, sigs, total)
    if testable:
        (cs, off), best = max(votes.items(), key=lambda kv: kv[1])
        if best >= max(4, 0.15 * testable) and best > 2 * ok_now and (vol is None or (cs, off) != (vol.cl, vol.data_off)):
            if log:
                log(f"Found the old layout: {cs // 1024 or cs} {'KB' if cs >= 1024 else 'byte'} blocks; "
                    f"{best} of {testable} listed files start exactly where it says")
            return LegacyFatVolume(m, cs, off, vol)
    # no folder entries to go on: fall back on where photos start
    pos = sigs["jpg"].astype(np.int64) * SECTOR
    if len(pos) >= 8 and (vol is None or ((pos - vol.data_off) % vol.cl == 0).mean() < 0.5):
        for cs in reversed(CLUSTER_SIZES):
            res, counts = np.unique(pos % cs, return_counts=True)
            if counts.max() >= 0.9 * len(pos):
                off = int(res[counts.argmax()])
                if log:
                    log("No old folders survive, but photos on the card line up with an older layout" if vol is not None
                        else f"Photos on this drive start every {cs // 1024 or cs} {'KB' if cs >= 1024 else 'bytes'}; using that to find them")
                return LegacyFatVolume(m, cs, off, vol)
    return None


class UnreadableVolume(LegacyFatVolume):
    """A drive with no file system this tool reads (NTFS, a Mac or Linux format, or a damaged
    one). Nothing is known about which files are deleted, so every photo found is recovered,
    including ones that are still on the drive in the normal way."""
    what = "a format this tool cannot read"

    @property
    def kind(self) -> str:
        return "Unreadable format, searched directly;"


def _format_name(m) -> str:
    from .fat import find_partitions
    for off in [0] + [o for o in find_partitions(m) if o]:
        b = bytes(m[off:off + 1100])
        if b[3:11] == b"NTFS    ":
            return "NTFS (the usual Windows format)"
        if b[1024:1026] in (b"H+", b"HX"):
            return "a Mac format"
        if len(b) >= 1082 and b[1080:1082] == b"\x53\xef":
            return "a Linux format"
        if b[32:36] == b"NXSB":
            return "a Mac format"
    return "no format this tool recognises"


def open_unreadable(m, log=None) -> UnreadableVolume:
    """A volume for a drive whose own format cannot be read, laid out the way an earlier FAT
    format had it if signs of one survive, or else on the grid its photos start on."""
    what = _format_name(m)
    if log:
        log(f"This drive has {what}. Its folders cannot be read, so the drive itself is searched for photos")
    found = _infer(m, None, 0, log)
    cs, off = (found.cl, found.data_off) if found is not None else (4096, 0)
    v = UnreadableVolume(m, cs, off)
    v.what = what
    return v


def _looks_reformatted(vol: FatVolume) -> bool:
    """Nothing listed, yet data well beyond what the file system accounts for."""
    alloc = vol.allocated()
    if int(alloc[2:].sum()) > 16:
        return False
    return not vol.blank_clusters()[2:].all()
