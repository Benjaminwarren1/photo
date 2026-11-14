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

from .fat import SECTOR, SURVEY_MAGIC as MAGIC, Entry, FatVolume, magic_rows
from .util import ceil_div

CLUSTER_SIZES = [512 << k for k in range(8)]          # 512 bytes .. 64 KB
JPEG = ("jpg", "jpeg", "jpe")
MP4 = ("mp4", "mov", "3gp", "m4v")


class _Sectors(FatVolume):
    """The whole image seen as 512-byte "clusters", so folder data can be found
    wherever it lies, whatever the layout used to be."""

    def __init__(self, m, upto: int | None = None):
        self.m, self.part = m, 0
        self.bps = self.cl = SECTOR
        self.data_off = 2 * SECTOR      # so that "cluster" c is simply sector c
        self.root_off = self.fat_off = 0
        self.root_size = self.fat_size = self.root_cluster = 0
        self.ncl = max(0, (len(m) if upto is None else min(upto, len(m))) // SECTOR - 2)
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
        c1 = min(self.ncl + 2, ceil_div(b - self.data_off, self.cl) + 2)
        if c1 > c0:
            self.fat[c0:c1] = self.eoc

    @property
    def kind(self) -> str:
        return f"FAT{self.fat_bits}, as the card was before it was reformatted"

    def _walk_live(self):
        return iter(())




def _signature_maps(m) -> dict[str, np.ndarray]:
    """For each kind of file, the 512-byte sectors (in order) that begin the way that kind begins."""
    n = len(m) // SECTOR
    step = (64 << 20) // SECTOR
    found = {kind: [] for kind in MAGIC}
    for s0 in range(0, n, step):
        k = min(step, n - s0)
        head = np.frombuffer(m, np.uint8, k * SECTOR, s0 * SECTOR).reshape(k, SECTOR)[:, :8]
        for kind, (o, magic) in MAGIC.items():
            found[kind].append(np.flatnonzero(magic_rows(head, o, magic)) + s0)
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


def _explained(vol, e: Entry) -> bool:
    """Does this folder entry point, in the layout the card has now, at a file of its kind?"""
    o, magic = MAGIC[_kind(e)]
    tries = [e.start]
    if getattr(vol, "fat_bits", 0) == 32:     # deleting on FAT32 often zeroes the high half
        tries += [(e.start & 0xFFFF) | (hi << 16) for hi in range((vol.max_cluster >> 16) + 1)]
    return any(vol.valid(c) and bytes(vol.cluster(c)[o:o + len(magic)]) == magic for c in tries)


def find_old_layout(vol: FatVolume, entries: list[Entry], log=None):
    """If folders survive on the card that belong to an earlier layout (it was reformatted with
    different settings), returns a volume for that layout; otherwise None. Those folders need not
    be visible in the current layout at all, so the whole card is looked over, not just `entries`."""
    return _infer(vol.m, vol, log, entries, elsewhere=_other_volume_spans(vol))


def _other_volume_spans(vol: FatVolume) -> list[tuple[int, int]]:
    """Where the card's other readable partitions lie, as byte ranges. Their folders and photos
    belong to them (each is recovered in its own right), not to an older layout of this one."""
    from .exfat import ExFatVolume
    from .fat import all_volumes
    out = []
    for kind, off in all_volumes(vol.m):
        if off == getattr(vol, "part", None):
            continue
        try:
            v = ExFatVolume(vol.m, off) if kind == "exfat" else FatVolume(vol.m, off)
            out.append((off, v.data_off + v.ncl * v.cl))
        except Exception:  # noqa: BLE001
            continue
    return out


def _outside(pos: np.ndarray, spans: list[tuple[int, int]]) -> np.ndarray:
    keep = np.ones(len(pos), bool)
    for a, b in spans:
        keep &= (pos < a) | (pos >= b)
    return keep


def _infer(m, vol: FatVolume | None, log=None, entries: list[Entry] | None = None, survey=None,
           elsewhere: list[tuple[int, int]] = ()):
    """Work out an earlier layout from the folders and photo starts found on the card. Anything
    inside the byte ranges in `elsewhere` (the card's other partitions) is left out."""
    total = len(m) // SECTOR
    survey = survey if survey is not None else getattr(vol, "survey", None)
    sigs = survey.sigs if survey is not None else _signature_maps(m)
    if vol is not None and entries is not None and vol.data_off % SECTOR == 0:
        # The card's own sweep has already read every sector of its data area for folders,
        # whichever layout they belong to. Only the part in front of that is left to look at.
        front = _Sectors(m, vol.data_off)
        front.survey = survey
        ahead = front.scan_entries()
        if elsewhere:
            ahead = [e for e in ahead if _outside(np.array([e.where * SECTOR]), elsewhere)[0]]
        found = [e for e in entries if not e.in_tree] + ahead
    else:
        whole = _Sectors(m)
        whole.survey = survey
        found = whole.scan_entries()
    # Only files that the current layout cannot account for say anything about an older one.
    old_entries = [e for e in found
                   if not e.is_dir and e.size > 0 and _kind(e) and (vol is None or not _explained(vol, e))]
    testable, votes = _votes(old_entries, sigs, total)
    if vol is not None:
        votes.pop((vol.cl, vol.data_off), None)
    if testable and votes:
        (cs, off), best = max(votes.items(), key=lambda kv: kv[1])
        if best >= max(4, 0.3 * testable) and _same_files(m, old_entries, cs, off, best, testable):
            if log:
                if vol is not None:
                    log("The card has folders from before it was last formatted; working out how it was laid out then")
                log(f"Found the old layout: {cs // 1024 or cs} {'KB' if cs >= 1024 else 'byte'} blocks; "
                    f"{best} of {testable} listed files start exactly where it says")
            return _with(LegacyFatVolume(m, cs, off, vol), survey)
    # no folder entries to go on: fall back on where photos start
    pos = sigs["jpg"].astype(np.int64) * SECTOR
    if elsewhere:
        pos = pos[_outside(pos, elsewhere)]
    sizes = CLUSTER_SIZES
    if vol is not None:
        # Photos that sit where the current layout puts files say nothing about an older one, and
        # "every 512 bytes" is no layout at all: pictures packed inside other files line up that way.
        on_grid = (pos >= vol.data_off) & ((pos - vol.data_off) % vol.cl == 0)
        pos = pos[~on_grid] if len(pos) and on_grid.mean() < 0.5 else pos[:0]
        sizes = [cs for cs in CLUSTER_SIZES if cs >= 2048]
    if len(pos) >= 8:
        for cs in reversed(sizes):
            res, counts = np.unique(pos % cs, return_counts=True)
            if counts.max() >= 0.9 * len(pos):
                off = int(res[counts.argmax()])
                if log:
                    log("No old folders survive, but photos on the card line up with an older layout" if vol is not None
                        else f"Photos on this drive start every {cs // 1024 or cs} {'KB' if cs >= 1024 else 'bytes'}; using that to find them")
                return _with(LegacyFatVolume(m, cs, off, vol), survey)
    return None


def _with(vol, survey):
    vol.survey = survey
    return vol


def _same_files(m, entries: list[Entry], cs: int, off: int, best: int, testable: int) -> bool:
    """A photo starting where a layout says is not yet proof: on a card of evenly spaced photos
    a wrong layout lands on the start of *other* photos. The right layout finds the very file
    each entry describes, so a photo's end marker sits exactly where its recorded size puts it."""
    hits = exact = 0
    for e in entries:
        if _kind(e) != "jpg" or e.start < 2:
            continue
        o = off + (e.start - 2) * cs
        if o + e.size > len(m) or bytes(m[o:o + 3]) != b"\xff\xd8\xff":
            continue
        hits += 1
        exact += bytes(m[o + e.size - 2:o + e.size]) == b"\xff\xd9"
    if hits >= 4:
        return exact >= max(3, 0.25 * hits)
    return best >= 0.8 * testable       # no photos to test with: only a near-unanimous vote will do


class UnreadableVolume(LegacyFatVolume):
    """A drive with no file system this tool reads (NTFS, a Mac or Linux format, or a damaged
    one). Nothing is known about which files are deleted, so every photo found is recovered,
    including ones that are still on the drive in the normal way."""
    what = "a format this tool cannot read"

    @property
    def kind(self) -> str:
        return "Unreadable format, searched directly;"


def _format_name(m) -> str:
    """The file system on a drive this tool cannot read, in plain words."""
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
    from .fat import Survey
    survey = Survey(m)
    found = _infer(m, None, log, survey=survey)
    cs, off = (found.cl, found.data_off) if found is not None else (4096, 0)
    v = UnreadableVolume(m, cs, off)
    v.survey = survey
    v.what = what
    return v

