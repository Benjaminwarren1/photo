"""The whole recovery run: scan a card image, recover what is whole, rebuild
what is fragmented, audit everything, write the output folders and the report.

The image is only ever read. Output goes to a separate folder.
"""
from __future__ import annotations

import errno
import io
import itertools
import json
import os
import struct
import time
import warnings
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from . import __version__, jpeg
from . import formats
from . import streams

warnings.filterwarnings("ignore", message="Image appears to be a malformed MPO")
from .fat import Entry, NotFat, open_volume
from .rebuild import Judge, Rebuilder, Result
from .video import CONTAINERS, VideoRebuilder

JPEG_EXT = {"jpg", "jpeg", "jpe"}
SIGNATURES = {            # extension -> bytes a file of that type starts with (offset, magic)
    "png": (0, b"\x89PNG\r\n\x1a\n"), "gif": (0, b"GIF8"), "bmp": (0, b"BM"),
    "mp4": (4, b"ftyp"), "mov": (4, None), "3gp": (4, b"ftyp"), "m4v": (4, b"ftyp"),
    "heic": (4, b"ftyp"), "avi": (0, b"RIFF"), "wav": (0, b"RIFF"), "pdf": (0, b"%PDF"),
    "zip": (0, b"PK"), "mp3": (0, None),
    **{ext: (0, None) for ext in ("nef", "nrw", "cr2", "arw", "sr2", "srf", "pef", "dng", "tif", "tiff", "3fr",
                                  "erf", "kdc", "dcr", "iiq", "orf", "rw2")},
    "cr3": (4, b"ftyp"), "raf": (0, b"FUJIFILMCCD-RAW"),     # camera RAW built on TIFF: see formats.py
    "mpo": (0, b"\xff\xd8\xff"),       # a 3D photo: two JPEGs in one file
    **{ext: (0, None) for ext in streams.TS_EXT},        # dashcam and camcorder video: see streams.py
}
BOX_TYPES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"meta", b"udta", b"pnot", b"moof",
             b"mfra", b"sidx", b"styp", b"PICT", b"pict", b"thum", b"junk"}

FOLDER_OK = "Recovered"
FOLDER_PARTIAL = "Recovered - Partial"
FOLDER_FILLED = "Recovered - Preview filled"
FOLDER_LIVE = "Still on the card"
FOLDER_SMALL = "Small pictures (thumbnails and app images)"
SMALL_PICTURE = 640          # longest side, in pixels, below which a picture is not counted as a photo
DOC_EXT = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "rtf", "odt", "ods", "odp", "csv", "epub", "pages", "numbers"}
MUSIC_EXT = {"mp3", "wav", "flac", "m4a", "ogg", "oga", "opus", "wma", "aac", "amr", "mid", "aiff"}
FULL_NOTE = "not saved: the drive the results are being saved to ran out of space"

GRADE_VERIFIED = "Verified"
GRADE_NO_PREVIEW = "Complete, no preview"
GRADE_UNVERIFIED = "Recovered, unverified"
GRADE_PARTIAL = "Partial"
GRADE_REVIEW = "Needs review"
GRADE_LOST = "Not recoverable"
GRADE_LIVE = "Still on the card"
VIDEO_EXT = {"mp4", "mov", "3gp", "3g2", "m4v", "avi", "mkv", "lrv"} | streams.TS_EXT
PHOTO_EXT = JPEG_EXT | {"png", "gif", "bmp", "heic", "heif", "mpo", "tif", "tiff", "dng", "nef", "nrw", "pef", "cr2", "cr3", "arw",
                        "raf", "orf", "rw2", "webp"}


@dataclass
class Item:
    name: str
    folder: str
    size: int | None
    start: int
    kind: str                       # jpeg | other
    source: str                     # "deleted entry" | "lost folder entry" | "found by scanning"
    grade: str = GRADE_LOST
    percent: float = 0.0
    method: str = ""
    note: str = ""
    chain: list[int] = field(default_factory=list)
    out_bytes: int = 0
    saved_as: str = ""
    filled_as: str = ""             # a second copy whose missing part is filled in from the preview
    mtime: tuple | None = None
    candidates: list = field(default_factory=list)
    picks: list = field(default_factory=list)
    _hint = None                    # chain suggested by the file system, if any
    _last = None                    # last rebuild attempt
    _forced: dict = field(default_factory=dict, repr=False)   # choices made by a person: chain index -> cluster


def preview_fill(data: bytes, percent: float) -> bytes | None:
    """A copy of a partly recovered photo whose missing lower part is filled in from the
    photo's own small preview, enlarged. The filled part is blurry but it is the real
    picture, not an invention. None if the photo has no usable preview."""
    from PIL import ImageFile
    try:
        info = jpeg.parse_header(data)
        thumb = jpeg.decode_thumb(info)
        if thumb is None or percent <= 0:
            return None
        w, h = info.width, info.height
        fill = Image.fromarray(np.clip(thumb, 0, 255).astype(np.uint8), "YCbCr").convert("RGB")
        fill = fill.resize((w, h), Image.BICUBIC)
        old = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            with Image.open(io.BytesIO(bytes(data) + b"\xff\xd9")) as im:
                real = im.convert("RGB")
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = old
        if real.size != (w, h):
            return None
        good = max(0, (int(percent / 100.0 * h) // 16 - 1) * 16)   # whole block rows known to be real
        fade = min(24, good)
        mask = np.zeros((h, 1), np.float32)                         # 1 where the real picture is kept
        mask[:good - fade] = 1.0
        if fade:
            mask[good - fade:good, 0] = np.linspace(1.0, 0.0, fade)
        a = np.asarray(real, np.float32) * mask[:, :, None] + np.asarray(fill, np.float32) * (1 - mask[:, :, None])
        buf = io.BytesIO()
        Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB").save(buf, "JPEG", quality=92)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - this copy is a convenience; the partial file is already saved
        return None


def _valid_isobmff(data) -> bool:
    """Walk top-level boxes of an MP4/MOV/3GP/HEIC file; they must tile the file exactly."""
    pos, n, seen = 0, len(data), 0
    while pos + 8 <= n:
        size, typ = struct.unpack_from(">I4s", data, pos)
        if size == 1:
            if pos + 16 > n:
                return False
            (size,) = struct.unpack_from(">Q", data, pos + 8)
        elif size == 0:
            size = n - pos
        if size < 8 or typ not in BOX_TYPES:
            return False
        pos += size
        seen += 1
    return pos == n and seen >= 2


def _valid_png(data) -> bool:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    pos, n = 8, len(data)
    while pos + 12 <= n:
        ln, typ = struct.unpack_from(">I4s", data, pos)
        pos += 12 + ln
        if typ == b"IEND":
            return pos == n
    return False


def _gif_lines_up(data) -> bool:
    """Walk a GIF block by block to its end mark. Every block states its own length, so a file
    that was cut, or had its middle replaced, almost never walks through."""
    n = len(data)
    if n < 14 or bytes(data[:4]) != b"GIF8":
        return False
    p = 13 + (3 << ((data[10] & 7) + 1) if data[10] & 0x80 else 0)

    def sub_blocks(p: int) -> int:
        while p < n and data[p]:
            p += 1 + data[p]
        return p + 1

    while p < n:
        kind = data[p]
        if kind == 0x3B:
            return True
        if kind == 0x21:
            p = sub_blocks(p + 2)
        elif kind == 0x2C:
            if p + 10 > n:
                return False
            flags = data[p + 9]
            p = sub_blocks(p + 10 + (3 << ((flags & 7) + 1) if flags & 0x80 else 0) + 1)
        else:
            return False
    return False


def png_length(buf, pos: int, end: int) -> int | None:
    """Length of the PNG that starts at pos, if every one of its parts passes its own checksum."""
    import zlib
    if bytes(buf[pos:pos + 8]) != b"\x89PNG\r\n\x1a\n":
        return None
    p, first = pos + 8, True
    while p + 12 <= end:
        ln, typ = struct.unpack_from(">I4s", buf, p)
        if p + 12 + ln > end or ln > 1 << 30 or (first and typ != b"IHDR"):
            return None
        if zlib.crc32(buf[p + 4:p + 8 + ln]) != struct.unpack_from(">I", buf, p + 8 + ln)[0]:
            return None
        p, first = p + 12 + ln, False
        if typ == b"IEND":
            return p - pos
    return None


def boxes_length(buf, pos: int, end: int) -> tuple[int, set, bytes] | None:
    """Follow the top-level boxes of a video or HEIC file that starts at pos for as long as they
    run on. Returns (length, box types seen, brand), or None if it does not begin like one."""
    if bytes(buf[pos + 4:pos + 8]) != b"ftyp":
        return None
    brand, p, seen = bytes(buf[pos + 8:pos + 12]), pos, set()
    while p + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, p)
        if size == 1 and p + 16 <= end:
            (size,) = struct.unpack_from(">Q", buf, p + 8)
        if size < 8 or typ not in BOX_TYPES or p + size > end or (typ == b"ftyp" and p != pos):
            break
        seen.add(typ)
        p += size
    return (p - pos, seen, brand) if len(seen) >= 2 else None


def unfinished_video(buf, pos: int, end: int) -> int | None:
    """For a video that starts at pos and whose picture data begins but whose index does not follow:
    the length its picture data says the file has up to the index, or 0 when that was never filled
    in (the recording was cut off). None when no such video starts here."""
    if bytes(buf[pos + 4:pos + 8]) != b"ftyp":
        return None
    (size,) = struct.unpack_from(">I", buf, pos)
    if not 8 <= size <= 1024:
        return None
    p = pos + size
    for _ in range(4):
        if p + 16 > end:
            return None
        size, typ = struct.unpack_from(">I4s", buf, p)
        if typ == b"mdat":
            if size == 1:
                (size,) = struct.unpack_from(">Q", buf, p + 8)
            return p - pos + size if 16 <= size <= end - p else 0
        if typ not in (b"free", b"wide", b"skip", b"uuid") or not 8 <= size <= 1 << 20:
            return None
        p += size
    return None


def check_other(ext: str, data):
    """'verified', 'unverified' or None (clearly not this file any more); or (one of those, a note)."""
    sig = SIGNATURES.get(ext)
    if sig and sig[1] is not None and data[sig[0]:sig[0] + len(sig[1])] != sig[1]:
        return None
    if ext in formats.RAW_EXT:
        return formats.check_tiff(data, len(data), audit_jpeg)
    if ext == "mpo":
        return formats.check_mpo(data, audit_jpeg)
    if ext == "png":
        return "verified" if png_length(data, 0, len(data)) == len(data) else None
    if ext in ("mp4", "mov", "3gp", "m4v", "heic", "cr3"):
        # Its outer parts lining up proves the file is all there, not that what is inside them
        # is still this video: only the frame-by-frame check can say that.
        lined_up = ("its parts line up from start to end, but its contents could not be checked; "
                    "play it through to make sure")
        if _valid_isobmff(data):
            return "unverified", lined_up
        got = boxes_length(data, 0, len(data))
        if got and got[0] >= 0.98 * len(data) and b"mdat" in got[1] and ({b"moov", b"moof", b"meta"} & got[1]):
            return "unverified", lined_up
        return None
    if ext == "avi":
        return streams.check_avi(data)
    if ext in streams.TS_EXT:
        return streams.check_ts(data, ext)
    if ext == "gif":
        return ("unverified", "its parts line up from start to end; this file type carries no further check") \
            if _gif_lines_up(data) else None
    return "unverified"


def audit_jpeg(data: bytes) -> tuple[bool, str, bool]:
    """Independent final check of a delivered photo, from its bytes alone.
    Returns (passed, note, had_preview)."""
    try:
        info = jpeg.parse_header(data)
    except (jpeg.NotJpeg, jpeg.HeaderIncomplete) as e:
        return False, f"header: {e or 'incomplete'}", False
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
    except Exception as e:  # noqa: BLE001
        return False, f"does not open as a picture ({type(e).__name__})", False
    if not info.supported:
        return True, f"{info.why_unsupported}: opened fine, structure not checked", False
    st = jpeg.Stream(info)
    st.feed(data[info.data_off:])
    if st.status != jpeg.ST_DONE:
        why = jpeg.ERROR_NAMES.get(st.error, "data ends early")
        return False, f"picture data fails at {100 * st.mcus // max(1, info.total_mcus)}% ({why})", False
    if st.end_offset() is None:
        return False, "no end marker after the last block", False
    judge = Judge(info, jpeg.decode_thumb(info))
    if judge.target is None:
        return True, "", False
    # compare with the preview band by band, never as one whole-picture average
    X, total = info.mcus_x, info.total_mcus
    band = max(1, info.mcus_y // 24) * X
    scores = [s for m0 in range(0, total, band)
              if (s := judge.preview_score(st.dc[m0:m0 + band], m0)) is not None]
    bad = first_bad_band(scores)
    if bad is not None:
        return False, f"does not match its own preview about {100 * bad // len(scores)}% of the way down", True
    return True, "", True


def first_bad_band(scores: list[float]) -> int | None:
    """Given how well each band of a picture matches its preview (low is good), the first band
    that does not belong, or None. One band far worse than the picture's typical band fails it.
    So does a large part of the picture being far worse than its best parts: when most of a
    picture is wrong, "typical" is itself wrong, and only the good part can be the yardstick."""
    if not scores:
        return None
    med, worst = float(np.median(scores)), max(scores)
    if worst > max(10.0, 3.0 * med):
        return scores.index(worst)
    if len(scores) >= 8:
        bar = max(10.0, 4.0 * float(np.percentile(scores, 10)))
        over = [i for i, v in enumerate(scores) if v > bar]
        if len(over) >= len(scores) / 4:
            return over[0]
    return None


def pixel_audit(data: bytes, info) -> bool | None:
    """Preview check for JPEG kinds the block decoder cannot read: decode with Pillow at
    reduced size and compare with the preview in bands. None = no preview or cannot decode."""
    thumb = jpeg.decode_thumb(info) if info else None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.draft("YCbCr", (max(1, im.width // 8), max(1, im.height // 8)))
            a = np.asarray(im.convert("YCbCr"), np.float32)
    except Exception:  # noqa: BLE001
        return False
    if thumb is None:
        return None
    th, tw = thumb.shape[:2]
    a = np.asarray(Image.fromarray(a.astype(np.uint8), "YCbCr").resize((tw, th), Image.BILINEAR), np.float32)
    band, scores = max(2, th // 24), []
    for r0 in range(0, th - band + 1, band):
        x, y = a[r0:r0 + band], thumb[r0:r0 + band]
        cm = float(np.abs(x[..., 1:] - y[..., 1:]).mean())
        yx, yy = x[..., 0] - x[..., 0].mean(), y[..., 0] - y[..., 0].mean()
        pen = 0.0
        if yy.std() > 6:
            pen = 8 * max(0.0, 0.85 - float((yx * yy).sum() / np.sqrt((yx ** 2).sum() * (yy ** 2).sum() + 1e-6)))
        scores.append(cm + pen)
    return first_bad_band(scores) is None


def _workers() -> int:
    return min(8, os.cpu_count() or 1) if jpeg.HAVE_C else 1


def _ahead(jobs, fn, workers: int):
    """Yields (job, fn(job)) in order, with fn running on other processor cores a little
    ahead of whoever is reading. fn must only look, never change anything: its answer
    is based on the card as it was a moment ago, and the reader checks it still holds."""
    if workers < 2 or len(jobs) < 4 * workers:
        for j in jobs:
            yield j, fn(j)
        return
    with ThreadPoolExecutor(workers) as pool:
        queue: deque = deque()
        it_jobs = iter(jobs)
        for j in itertools.islice(it_jobs, 4 * workers):
            queue.append((j, pool.submit(fn, j)))
        while queue:
            j, f = queue.popleft()
            yield j, f.result()
            for nj in itertools.islice(it_jobs, 1):
                queue.append((nj, pool.submit(fn, nj)))


class Recovery:
    def __init__(self, image, out_dir=None, log=None, _only=None, _taken=None, _vol=None):
        self._only = _only          # internal: recover just these folder entries, and do not scan for more
        self._part = _vol is not None   # internal: one more partition of a card already being recovered
        self._shared_taken = _taken
        self._out_full = False
        self.out = Path(out_dir) if out_dir else None
        say = log or (lambda msg: None)

        self.steps: list[list] = []       # [message, time it started] for each step, for the report

        def _log(msg):               # a new step starts: the count within the old one no longer applies
            self.progress = None
            self.steps.append([msg, time.time()])
            say(msg)
        self.log = _log
        try:
            self.vol = _vol if _vol is not None else open_volume(image)
            self.unreadable = ""
            if getattr(self.vol, "repaired", False):
                self.log("The start of this card is damaged, which is why a computer says it needs formatting. "
                         "A spare copy of its layout was found on the card and is being used")
        except NotFat:
            from .fat import _open_ro
            from .legacy import open_unreadable
            m = _open_ro(image) if isinstance(image, (str, Path)) else image
            if len(m) < (1 << 20):
                raise
            self.vol = open_unreadable(m, self.log)
            self.unreadable = self.vol.what
        # Areas the copy could not read from the card (blank in the image), listed beside the image.
        self.blank_ranges: list[tuple[int, int]] = []
        if isinstance(image, (str, Path)):
            try:
                import json
                listed = json.loads(Path(str(image) + ".bad.json").read_text())
                self.blank_ranges = sorted((int(a), int(a) + int(n)) for a, n in listed["unreadable"])
            except (OSError, ValueError, KeyError, TypeError):
                pass
        self.items: list[Item] = []
        self.summary: dict = {}
        self.problems: list[str] = []
        self.always_ask = False
        self.include_existing = False      # also copy out files that were never deleted
        self._first_vol = self.vol
        self._existing: list = []
        self.progress: tuple[int, int] | None = None     # (done, total) within the current step

    # ---- helpers ----------------------------------------------------------
    def _resolve_start(self, e: Entry, ext: str) -> int:
        """On FAT32 the high half of the start cluster is often zeroed by the delete.
        Try the stored value first, then every other high half, and keep the one
        that lands on the right kind of file."""
        vol = self.vol
        if vol.fat_bits != 32:
            return e.start

        def fits(c: int) -> bool:
            if not vol.valid(c):
                return False
            head = vol.cluster(c)[:16]
            if ext in JPEG_EXT:
                return head[:3] == b"\xff\xd8\xff"
            sig = SIGNATURES.get(ext)
            if sig and sig[1]:
                return head[sig[0]:sig[0] + len(sig[1])] == sig[1]
            return False

        if fits(e.start):
            return e.start
        lo = e.start & 0xFFFF
        hits = [c for hi in range((vol.max_cluster >> 16) + 1) if fits(c := lo | (hi << 16))]
        return hits[0] if len(hits) == 1 else e.start

    def _starts_right(self, e: Entry) -> bool:
        """Does the cluster this entry points to, in the card's current layout, begin the way
        a file of its type begins?"""
        ext, vol = e.ext, self.vol
        c = self._resolve_start(e, ext)
        if not vol.valid(c):
            return False
        head = vol.cluster(c)[:16]
        if ext in JPEG_EXT:
            return head[:3] == b"\xff\xd8\xff"
        sig = SIGNATURES.get(ext)
        return bool(sig and sig[1] and head[sig[0]:sig[0] + len(sig[1])] == sig[1])

    def _photo_starts(self, avail: np.ndarray) -> list[int]:
        """Unclaimed clusters that begin the way a JPEG begins. The same read of the card also
        notes (in self._other_starts) those that begin the way a PNG or a video does."""
        vol, out = self.vol, []
        self._other_starts: list[tuple[int, str]] = []
        sv = getattr(vol, "survey", None)
        if sv is not None and vol.data_off % 512 == 0 and vol.cl % 512 == 0:
            def at(kind):
                c = sv.starts(kind, vol.data_off, vol.cl, vol.max_cluster)
                return c[avail[c]].tolist()
            self._other_starts = sorted([(c, "png") for c in at("png")] + [(c, "ftyp") for c in at("mp4")]
                                        + [(c, "tiff") for c in at("tif") + at("tifm")]
                                        + [(c, "riff") for c in at("riff")] + [(c, "ts") for c in at("ts")])
            return at("jpg")
        step = max(1, (64 << 20) // vol.cl)
        for c0 in range(2, vol.max_cluster + 1, step):
            n = min(step, vol.max_cluster + 1 - c0)
            a = np.frombuffer(vol.m, np.uint8, n * vol.cl, vol.data_off + (c0 - 2) * vol.cl).reshape(n, vol.cl)
            free = avail[c0:c0 + n]
            hit = (a[:, 0] == 0xFF) & (a[:, 1] == 0xD8) & (a[:, 2] == 0xFF) & free
            out += (np.flatnonzero(hit) + c0).tolist()
            if vol.cl >= 8:
                png = (a[:, 0] == 0x89) & (a[:, 1] == 0x50) & (a[:, 2] == 0x4E) & (a[:, 3] == 0x47) & free
                vid = (a[:, 4] == 0x66) & (a[:, 5] == 0x74) & (a[:, 6] == 0x79) & (a[:, 7] == 0x70) & free
                self._other_starts += [(int(c) + c0, "png") for c in np.flatnonzero(png)]
                self._other_starts += [(int(c) + c0, "ftyp") for c in np.flatnonzero(vid)]
                riff = (a[:, 0] == 0x52) & (a[:, 1] == 0x49) & (a[:, 2] == 0x46) & (a[:, 3] == 0x46) & free
                self._other_starts += [(int(c) + c0, "riff") for c in np.flatnonzero(riff)]
            if vol.cl >= 512:
                ts = ((a[:, 0] == 0x47) & (a[:, 188] == 0x47) & (a[:, 376] == 0x47)
                      | (a[:, 4] == 0x47) & (a[:, 196] == 0x47) & (a[:, 388] == 0x47)) & free
                self._other_starts += [(int(c) + c0, "ts") for c in np.flatnonzero(ts)]
        self._other_starts.sort()
        return out

    def run(self) -> dict:
        """The whole recovery, pass by pass. Each pass takes only what its own evidence proves, and
        what it takes leaves the pool, so that later passes cannot hand the same space out twice."""
        t0 = self._t0 = time.time()
        entries, newer = self._read_folders()
        vol = self.vol
        self._blank = vol.blank_clusters()
        self._avail = ~vol.allocated() & ~self._blank
        self._avail[:2] = False
        used_fraction = 1.0 - self._blank[2:].mean() if vol.ncl else 0.0
        targets = [e for e in entries if not e.live and not e.is_dir and e.size > 0]
        self.log(f"{len(targets)} deleted or lost files listed; {used_fraction:.0%} of the card holds data")
        self._rb = Rebuilder(vol, self._avail)
        self._rb.always_ask = self.always_ask
        self._vr = VideoRebuilder(vol, self._avail)
        self._items = items = self._list_items(targets)
        jpegs = [i for i in items if i.kind == "jpeg"]
        others = [i for i in items if i.kind == "other"]

        pending, loose = self._whole_photos(jpegs)
        ruled = self._videos(others)
        self._other_files([it for it in others if id(it) not in ruled])
        tried = {it.start for it in pending}
        self._photos_in_pieces(pending, loose)
        found = self._scan_for_photos(tried) if self._only is None else []
        if self._only is None:
            self._scan_for_others()
        self._name_found(found)

        self.items = items
        if newer is not None:
            self._shared_taken = newer._taken
        self._deliver()
        if newer is not None:
            self.items = newer.items + items
            self.problems += newer.problems
        self.progress = None
        cl = vol.cl
        self.summary = dict(tool=f"photorecover {__version__}", filesystem=(f"{newer.vol.kind} now, and {vol.kind}" if newer is not None else vol.kind),
                            cluster_size=cl, clusters=vol.ncl, card_in_use=round(float(used_fraction), 3),
                            files_listed=len(items), counts=self._counts(), seconds=round(time.time() - t0, 1),
                            fast_decoder=jpeg.HAVE_C, problems=self.problems[:50],
                            steps=[[m, round((self.steps[i + 1][1] if i + 1 < len(self.steps) else time.time()) - t, 1)]
                                   for i, (m, t) in enumerate(self.steps)],
                            old_layout=self.old_layout, unreadable=self.unreadable)
        self.summary["since_format"] = len(newer.items) if newer is not None else 0
        self.summary["still_on_card"] = self._save_existing() if self._only is None else None
        if self._only is None:
            self.summary["counts"] = self._counts()
            self.summary["files_listed"] = len(self.items)
            self.summary["problems"] = self.problems[:50]
        self.summary["damaged_start"] = bool(getattr(self.vol if newer is None else newer.vol, "repaired", False))
        self.summary["unreadable_bytes"] = sum(b - a for a, b in self.blank_ranges)
        if self._only is None and not self._part:
            self._other_partitions()
        kinds = {"photos": 0, "videos": 0, "other": 0}       # what the files that cannot be checked are
        for it in self.items:
            if it.grade == GRADE_UNVERIFIED:
                ext = it.name.rpartition(".")[2].lower() if "." in it.name else ""
                kinds["photos" if ext in PHOTO_EXT else "videos" if ext in VIDEO_EXT else "other"] += 1
        self.summary["unverified_kinds"] = kinds
        self.summary["findings"] = self._findings() if self._only is None and not self._part else []
        unsaved = sum(1 for it in self.items if getattr(it, "_unsaved", False))
        if unsaved and self._only is None and not self._part:
            self.summary["findings"].insert(0, dict(kind="out_full", sure=True, text=(
                f"The drive the results were being saved to filled up, so {unsaved} file{'s' if unsaved != 1 else ''} "
                "that could be recovered "
                f"{'were' if unsaved != 1 else 'was'} not saved. Nothing is wrong with the card. Free some space, or "
                "choose a folder on a drive with more room, and run the recovery again.")))
        if self.out and self._only is None and not self._part:
            try:
                self._write_report()
            except OSError as e:
                self.summary["report_error"] = f"{type(e).__name__}: {e}"
                self.summary["problems"] = (self.problems + [f"the report could not be written: {e}"])[:50]
        return self.summary

    def _claim(self, chain) -> None:
        self._avail[np.asarray(chain, np.int64)] = False

    def _still_free(self, r) -> bool:
        return bool(self._avail[np.asarray(r.chain, np.int64)].all())

    def _ahead(self, jobs, fn):
        return _ahead(jobs, fn, _workers())

    def _attempt(self, it, fn, *args, **kw):
        """One odd file must never stop the whole run."""
        try:
            return fn(*args, **kw)
        except Exception as e:  # noqa: BLE001
            import traceback
            at = traceback.extract_tb(e.__traceback__)[-1]
            self.problems.append(f"{it.name if it else '?'}: {type(e).__name__}: {e} "
                                 f"({Path(at.filename).name}:{at.lineno})")
            if it is not None:
                it.note = "the tool hit an unexpected problem with this file and skipped it"
            return None

    def _read_folders(self):
        """Every folder entry on the card, and, when the card was reformatted with different
        settings and used again, the recovery of the newer layer (self.vol is then the older one)."""
        vol = self.vol
        self.old_layout = None
        newer: Recovery | None = None
        if self._only is not None:
            entries, old = self._only, None
        else:
            if getattr(vol, "survey", None) is None:
                from .fat import Survey
                self.log("Reading through the whole card once")

                def seen(k, n):
                    self.progress = (k, n)
                vol.survey = Survey(vol.m, seen)
            self.log("Reading the card's folders, including deleted ones")
            entries = vol.scan_entries()
            self._existing = [e for e in entries if e.live and not e.is_dir and e.size > 0 and not e.deleted]
            try:
                from .legacy import find_old_layout
                old = None if self.unreadable or self._part else find_old_layout(vol, entries, self.log)
            except Exception as e:  # noqa: BLE001
                old = None
                self.problems.append(f"looking for an older layout: {type(e).__name__}: {e}")
        if old is not None:
            # The card was reformatted with different settings. Anything deleted since then is
            # listed in the format it has now, so that layer is recovered first, as it stands...
            mine = [e for e in entries if not e.live and not e.is_dir and e.size > 0
                    and (e.in_tree or self._starts_right(e))]
            if mine:
                self.log(f"Recovering {len(mine)} files deleted since the card was last formatted")
                newer = Recovery(vol.m, self.out, None, _only=mine, _taken=set())
                newer.vol, newer.log, newer.always_ask = vol, (lambda msg: None), self.always_ask
                newer.blank_ranges = self.blank_ranges
                try:
                    newer.run()
                except Exception as e:  # noqa: BLE001
                    self.problems.append(f"files deleted since the last format: {type(e).__name__}: {e}")
                    newer = None
            # ... and then everything older is read the way the card used to be laid out.
            self.old_layout = dict(cluster_size=old.cl, data_offset=old.data_off)
            if newer is not None:
                for it in newer.items:
                    it._owner = newer
                    if it.saved_as.startswith(FOLDER_OK + "/"):
                        for c in it.chain:
                            if c >= 2:
                                o = vol.data_off + (c - 2) * vol.cl
                                old._claim(o, o + vol.cl)
            seen_now = {(e.short, e.size, e.mtime) for e in mine}
            self.vol = vol = old
            entries = [e for e in vol.scan_entries() if (e.short, e.size, e.mtime) not in seen_now]
        return entries, newer

    def _list_items(self, targets) -> list[Item]:
        """One item per distinct (start, size); a long name wins over a short one."""
        vol = self.vol
        seen: dict[tuple[int, int], Item] = {}
        for e in targets:
            ext = e.ext
            start = self._resolve_start(e, ext)
            if not e.in_tree and not vol.valid(start):
                continue        # read from a swept block, and points nowhere: not a real file's entry
            key = (start, e.size)
            it = Item(name=e.name, folder=e.path, size=e.size, start=start,
                      kind="jpeg" if ext in JPEG_EXT else "other",
                      source="deleted entry" if e.deleted else "lost folder entry", mtime=e.mtime)
            it._hint = vol.hint_chain(e)
            if key not in seen or (seen[key].name.startswith("_") and not e.name.startswith("_")):
                seen[key] = it
        return sorted(seen.values(), key=lambda i: i.start)

    def _whole_photos(self, jpegs):
        """Pass 1: photos still in one piece. Exact structural proof, then they leave the pool.
        Returns the photos still to rebuild, and those whose picture ends before the recorded size."""
        self.log(f"Checking {len(jpegs)} photos")
        pending: list[Item] = []
        loose: list[tuple[Item, Result]] = []
        def whole(it):
            r = self._attempt(it, self._rb.prove_chain, it._hint, it.size) if it._hint else None
            if r is not None:
                return True, r
            return False, self._attempt(it, self._rb.rebuild, it.start, it.size, contiguous_only=True)

        for k, (it, (proved, r)) in enumerate(self._ahead(jpegs, whole)):
            self.progress = (k, len(jpegs))
            hint = it._hint
            if r is not None and r.status == "complete" and not self._still_free(r):
                proved, r = whole(it)       # an earlier photo has just taken some of this space: look again
            if proved:
                pieces = 1 + sum(1 for a, b in zip(hint, hint[1:]) if b != a + 1)
                self._accept(it, r, "found whole" if pieces == 1 else
                             f"rebuilt from {pieces} pieces using the card's own record")
                self._claim(r.chain)
                continue
            if r is None:
                continue
            if r.status == "complete" and r.exact_end:
                self._accept(it, r, "found whole")
                self._claim(r.chain)
            elif r.status == "complete":
                loose.append((it, r))
            elif r.status == "unsupported":
                self._unsupported(it, r)
            elif r.status == "failed":
                it.note = r.reason
            else:
                pending.append(it)
        return pending, loose

    def _videos(self, others) -> set[int]:
        """Videos: every frame and audio packet listed in the video's index is a checkpoint.
        Returns the ids of the items this pass has ruled on."""
        videos = [i for i in others if i.name.rpartition(".")[2].lower() in CONTAINERS]
        rejected: set[int] = set()         # videos the frame check has ruled on: no second chance as "other"
        if videos:
            self.log(f"Checking {len(videos)} videos")
        for k, it in enumerate(videos):
            self.progress = (k, len(videos))
            v = self._attempt(it, self._vr.rebuild, it.start, it.size)
            if v is None:
                continue
            if v.status == "failed":
                # no index could be read. If the card records which clusters the file had (exFAT),
                # or the file lies whole in one run, the pass below may still save it, unverified.
                it._video_why = v.reason
                continue
            if v.status == "complete" and not _valid_isobmff(self._vr.read(v, it.size)):
                # every checkpoint matched, but the file does not end where this entry says:
                # the space now belongs to a different, later video
                it.note = "its space was reused by a later file"
                rejected.add(id(it))
                continue
            it.chain = v.chain
            it.percent = round(100.0 * (len(v.chain) - v.missing) / max(1, len(v.chain)), 1)
            real = [c for c in v.chain if c >= 0]
            pieces = 1 + sum(1 for a, b in zip(real, real[1:]) if b != a + 1)
            it.method = "found whole" if pieces == 1 and not v.missing else f"rebuilt from {pieces} pieces"
            if v.status == "complete":
                it.grade = GRADE_VERIFIED
                it.note = f"all {v.checked} frame and sound checkpoints match"
                if v.uncertain_joins:
                    it.grade = GRADE_UNVERIFIED
                    it.note += ("; but a join between two pieces could not be placed exactly, "
                                "so watch it through for a brief glitch")
                self._claim(real)
            else:
                it.grade, it.note = GRADE_PARTIAL, v.reason + "; the gaps are left blank and will show as glitches"
        return {id(i) for i in videos if i.chain} | rejected

    def _other_files(self, others) -> None:
        """Other file types that are whole."""
        vol, cl = self.vol, self.vol.cl
        for it in others:
            n = -(-it.size // cl)
            if not vol.valid(it.start) or it.start + n - 1 > vol.max_cluster:
                it.note = ("the card no longer records where this file was stored" if it.start == 0
                           else "start cluster is outside the card")
                continue
            chain = it._hint or list(range(it.start, it.start + n))
            if not self._avail[chain].all():
                it.note = "its space was reused by another file"
                continue
            ext = it.name.rpartition(".")[2].lower() if "." in it.name else ""
            if ext not in SIGNATURES and bytes(vol.cluster(chain[0])[:8]) == b"\x89PNG\r\n\x1a\n":
                # a picture saved without its ending (some apps do): check it as what it is, and
                # give it the ending so that it opens with a double click
                if not ext:
                    it.name += ".png"
                ext = "png"
            # only kinds that can be checked need reading through; the rest are saved as they are
            verdict = "unverified"
            if ext in SIGNATURES:
                verdict = self._attempt(it, lambda: check_other(ext, vol.read_chain(chain, it.size)) or "gone")
                if verdict is None:          # the check itself failed on this file: noted, and the run goes on
                    continue
                verdict = None if verdict == "gone" else verdict
            why = "this file type cannot be checked; open it to make sure"
            if isinstance(verdict, tuple):
                verdict, why = verdict
            if verdict is None:
                it.note = getattr(it, "_video_why", "") or \
                    "its space no longer holds this kind of file (overwritten, or stored in pieces)"
                continue
            it.chain, it.percent = chain, 100.0
            it.grade = GRADE_UNVERIFIED
            it.method = "found whole"
            if verdict == "verified":
                it.grade = GRADE_VERIFIED
                self._claim(chain)
                if why and not why.startswith("this file type"):
                    it.note = why
            else:
                it.note = why

    def _stale_entry(self, it) -> bool:
        """Its first cluster has since been proved to belong to another file, so this folder
        entry is out of date. What starts there is that file, not this one."""
        if not self.vol.valid(it.start) or self._avail[it.start]:
            return False
        now = next((o for o in self._items if o.chain and o is not it and it.start in o.chain[:1]), None) \
            or next((o for o in self._items if o.chain and o is not it and it.start in o.chain), None)
        it.note = ("its space now holds a different file" +
                   (f" ({now.name})" if now is not None and now.name else "") + ", saved after this one was deleted")
        it._last = None
        return True

    def _photos_in_pieces(self, pending, loose) -> None:
        """Pass 2: photos in pieces. Two rounds, because every photo finished frees the others
        from having to consider its clusters."""
        if pending:
            self.log(f"Rebuilding {len(pending)} photos that are stored in pieces")
        for rnd in range(2):
            still = []
            for k, it in enumerate(pending):
                self.progress = (k, len(pending))
                if self._stale_entry(it):
                    continue
                # First time round each next piece is sought only near the last one. On a real,
                # much-used card it was within reach there for 998 joins in 1000; the second time
                # round, with the easy photos out of the way, the whole card is searched.
                r = self._attempt(it, self._rb.rebuild, it.start, it.size, wide=rnd == 1)
                if r is None:
                    continue
                if r.status == "complete" and r.exact_end:
                    pieces = 1 + sum(1 for a, b in zip(r.chain, r.chain[1:]) if b != a + 1)
                    self._accept(it, r, "found whole" if pieces == 1 else f"rebuilt from {pieces} pieces")
                    self._claim(r.chain)
                elif r.status == "complete":
                    loose.append((it, r))
                else:
                    it._last = r
                    still.append(it)
            pending = still
            if rnd == 0 and pending:
                self.log(f"Searching the whole card for the rest of {len(pending)} photos")
        for it in pending:
            if self._stale_entry(it):      # the photo that replaced it may only have been finished after its own turn
                continue
            r: Result = it._last
            it.chain, it.percent, it.picks = r.chain, r.percent, r.picks
            it.candidates = r.candidates
            it.note = r.reason
            it.grade = GRADE_REVIEW if r.status == "review" else GRADE_PARTIAL
            it.method = "top part recovered"

        # Photos whose picture ends before the recorded size (extra data after the end
        # marker). Only believable if nobody else proved a claim to the same clusters.
        for it, r in loose:
            if self._avail[np.asarray(r.chain, np.int64)].all():
                self._accept(it, r, "found whole")
                it.note = self._trailer_note(it, r)
                self._claim(r.chain)
            else:
                it.note = "its space was reused by a later file"
        for it in self._items:        # and a photo left unfinished whose start one of those has just taken
            if it.kind == "jpeg" and it.grade in (GRADE_REVIEW, GRADE_PARTIAL) and self._stale_entry(it):
                it.grade, it.chain, it.candidates, it.percent, it.method = GRADE_LOST, [], [], 0.0, ""

    def _scan_for_photos(self, tried) -> list[Item]:
        """Pass 3: photos with no folder entry left at all."""
        heads = self._photo_starts(self._avail)
        if heads:
            self.log(f"Looking at {len(heads)} photo starts that have no folder entry")
        found: list[Item] = []

        def take(c: int, r) -> None:
            it = Item(name="", folder="Found by scanning", size=r.size, start=c,
                      kind="jpeg", source="found by scanning")
            pieces = 1 + sum(1 for a, b in zip(r.chain, r.chain[1:]) if b != a + 1)
            self._accept(it, r, "found whole" if pieces == 1 else f"rebuilt from {pieces} pieces")
            self._claim(r.chain)
            found.append(it)

        def carve(c: int, wide: bool):
            r = self._attempt(None, self._rb.rebuild, c, None, wide=wide)
            if r is not None and r.status == "complete":
                take(c, r)
            return r

        # First the ones still in one piece, which is most of them and needs no searching.
        def in_one_piece(c: int):
            return self._attempt(None, self._rb.rebuild, c, None, contiguous_only=True) if self._avail[c] else None

        broken: list[int] = []
        for k, (c, r) in enumerate(self._ahead(heads, in_one_piece)):
            self.progress = (k, len(heads))
            if not self._avail[c]:
                continue
            if r is not None and r.status == "complete" and not self._still_free(r):
                r = in_one_piece(c)
            if r is not None and r.status == "complete":
                take(c, r)
            # Where a listed photo could not be finished, its folder entry may be out of date and a
            # different, whole photo may start there: worth that look, but not another search.
            elif c not in tried and r is not None and r.status not in ("failed", "unsupported"):
                broken.append(c)
        if broken:
            self.log(f"Rebuilding {len(broken)} of them that are stored in pieces")
        again: list[int] = []
        for k, c in enumerate(broken):
            self.progress = (k, len(broken))
            if self._avail[c]:
                r = carve(c, False)
                # One that joined a piece by searching nearby, then got stuck, has shown that its
                # pieces are on the card: it has earned a search of all of it. So has one with a
                # preview to judge against, since a photo in many scattered pieces can only be
                # followed join by join, which the nearby search (all-or-nothing) cannot do.
                if r is not None and r.status != "complete" and (r.picks or r.has_preview):
                    again.append(c)
        if again:
            self.log(f"Searching the whole card for the rest of {len(again)} of them")
        for k, c in enumerate(again):
            self.progress = (k, len(again))
            if self._avail[c]:
                carve(c, True)
        return found

    def _scan_for_others(self) -> None:
        """Pass 4: pictures of other kinds, and videos, with no folder entry left. A PNG carries a
        checksum on every part and a video an index of every frame, so each is taken only when
        its own contents vouch for it."""
        # (a file saved unverified from its folder entry does not claim its space, so skip its start here)
        begun = {it.chain[0] for it in self._items if it.chain}
        others_found = [(c, k) for c, k in getattr(self, "_other_starts", [])
                        if self._avail[c] and c not in begun]
        if others_found:
            self.log(f"Looking at {len(others_found)} other pictures and videos that have no folder entry")
        self._numbers = {"png": 0, "video": 0, "heic": 0, "raw": 0}
        # a transport stream looks like the start of one at many places along its length, so one
        # already saved under its own name must not be found again in pieces
        held = np.zeros(self.vol.max_cluster + 2, bool)
        for it in self._items:
            if it.chain:
                held[np.asarray([x for x in it.chain if x >= 0], np.int64)] = True
        for k, (c, kind) in enumerate(others_found):
            self.progress = (k, len(others_found))
            if kind == "ts" and held[c]:
                continue
            if self._avail[c]:
                self._attempt(None, self._scan_other, c, kind)

    def _found_other(self, c, kind, ext, size, chain, grade, note, percent=100.0) -> None:
        numbers = self._numbers
        numbers[kind] += 1
        it = Item(name=f"found_{kind}_{numbers[kind]:05d}.{ext}", folder="Found by scanning", size=size, start=c,
                  kind="other", source="found by scanning", grade=grade, percent=percent,
                  method="found whole" if all(b == a + 1 for a, b in zip(chain, chain[1:])) else "rebuilt from pieces",
                  note=note, chain=chain)
        self._claim([x for x in chain if x >= 0])
        self._items.append(it)

    def _scan_other(self, c: int, kind: str) -> None:
        """What starts at cluster c, when it is a whole picture or video of another kind."""
        vol, cl = self.vol, self.vol.cl
        data_end = vol.data_off + (vol.max_cluster - 1) * cl
        pos = vol.data_off + (c - 2) * cl
        if kind == "png":
            size = png_length(vol.m, pos, data_end)
            chain = list(range(c, c + -(-size // cl))) if size else []
            if size and self._avail[chain].all():
                self._found_other(c, "png", "png", size, chain, GRADE_VERIFIED, "every part of it passes its own checksum")
            return
        if kind == "tiff":
            got = formats.tiff_length(vol.m, pos, data_end)
            if got is None:
                return
            size, ext = got
            chain = list(range(c, c + -(-size // cl)))
            if not self._avail[chain].all():
                return
            checked = formats.check_tiff(vol.read_chain(chain, size), size, audit_jpeg)
            if checked is not None:
                self._found_other(c, "raw", ext, size, chain, GRADE_UNVERIFIED, checked[1])
            return
        if kind == "riff":
            self._scan_avi(c, pos, data_end)
            return
        if kind == "ts":
            self._scan_ts(c, pos, data_end)
            return
        got = boxes_length(vol.m, pos, data_end)
        if got is None or not ({b"moov", b"moof", b"meta"} & got[1]):
            # A recording the camera never finished (battery died, card pulled out): the video
            # data is there but the index that players need was never written. Keep the data
            # in one run, up to the first blank or foreign cluster, and say what it is.
            want = unfinished_video(vol.m, pos, data_end)
            if want is not None:
                limit = c + (-(-want // cl) if want else (64 << 30) // cl)
                k = c + 1
                while k <= vol.max_cluster and k < limit and self._avail[k] and not self._blank[k] and \
                        bytes(vol.cluster(k)[:3]) != b"\xff\xd8\xff" and bytes(vol.cluster(k)[4:8]) != b"ftyp":
                    k += 1
                have = min(want, (k - c) * cl) if want else (k - c) * cl
                if have >= 256 << 10:
                    brand = bytes(vol.m[pos + 8:pos + 12])
                    ext = "mov" if brand == b"qt  " else "mp4"
                    fix = ("A video repair tool can often rebuild it using another clip from the same camera")
                    if not want:
                        self._found_other(c, "video", ext, have, list(range(c, k)), GRADE_PARTIAL,
                                    "a recording that was cut off before the camera finished writing it: its video "
                                    "data is here, but the index that players need was never saved, so it will "
                                    "not play as it is. " + fix)
                    else:
                        self._found_other(c, "video", ext, have, list(range(c, k)), GRADE_PARTIAL,
                                    ("the video data of a clip whose index (the part players need) is no longer "
                                     "on the card" if have == want else
                                     "the first part of a clip's video data; the rest, and the index players "
                                     "need, are no longer where they should be (overwritten, or stored elsewhere "
                                     "on the card)") + ", so it will not play as it is. " + fix,
                                    percent=round(100.0 * have / want, 1) if have < want else 100.0)
            return
        size, seen, brand = got
        chain = list(range(c, c + -(-size // cl)))
        if not self._avail[chain].all():
            return
        if brand == b"crx ":                      # Canon's newer RAW: built like a video file
            if b"mdat" in seen:
                self._found_other(c, "raw", "cr3", size, chain, GRADE_UNVERIFIED,
                            "its parts line up from start to end; the raw sensor data itself carries no check")
        elif b"moov" in seen or b"moof" in seen:
            ext = "mov" if brand == b"qt  " else "3gp" if brand[:2] == b"3g" else "m4a" if brand[:3] == b"M4A" else "mp4"
            v = self._vr.rebuild(c, size) if b"moov" in seen else None
            if v is not None and v.status == "complete" and not v.uncertain_joins and \
                    self._avail[np.asarray(v.chain, np.int64)].all():
                self._found_other(c, "video", ext, size, v.chain, GRADE_VERIFIED,
                            f"all {v.checked} frame and sound checkpoints match")
            elif b"mdat" in seen:
                self._found_other(c, "video", ext, size, chain, GRADE_UNVERIFIED,
                            "its parts line up from start to end, but its frames could not be checked; "
                            "watch it through to make sure")
        elif brand in (b"heic", b"heix", b"mif1", b"msf1", b"avif") and b"meta" in seen and b"mdat" in seen:
            self._found_other(c, "heic", "avif" if brand == b"avif" else "heic", size, chain, GRADE_UNVERIFIED,
                        "its parts line up from start to end; this file type cannot be checked further")

    def _free_run(self, c: int, most: int) -> int:
        """How many clusters from c on, up to most, are unclaimed and hold something."""
        k = c
        while k <= self.vol.max_cluster and k < c + most and self._avail[k] and not self._blank[k]:
            k += 1
        return k - c

    def _scan_avi(self, c: int, pos: int, data_end: int) -> None:
        vol, cl = self.vol, self.vol.cl
        if bytes(vol.m[pos + 8:pos + 12]) != b"AVI ":
            return
        size = streams.avi_length(vol.m, pos, data_end)
        if size:
            chain = list(range(c, c + -(-size // cl)))
            if not self._avail[chain].all():
                return
            checked = streams.check_avi(vol.read_chain(chain, size))
            if checked is not None:
                self._found_other(c, "video", "avi", size, chain,
                                  GRADE_VERIFIED if checked[0] == "verified" else GRADE_UNVERIFIED, checked[1])
                return
        # A recording that was cut off, or whose end has been overwritten: keep the frames that are
        # there, in one run. Without its index a player has to feel its way through, and many can.
        limit = pos + self._free_run(c, (16 << 30) // cl) * cl
        have = streams.unfinished_avi(vol.m, pos, min(limit, data_end))
        if have and have >= 256 << 10:
            self._found_other(c, "video", "avi", have, list(range(c, c + -(-have // cl))), GRADE_PARTIAL,
                              "a recording that was cut off, or whose end is no longer on the card: its frames are "
                              "here but its index is not, so some players will refuse it. VLC will usually play it, "
                              "and a video repair tool can rebuild the index")

    def _scan_ts(self, c: int, pos: int, data_end: int) -> None:
        """Keep the run of stream packets that starts at cluster c. Where the packets' running count
        jumps, a different recording begins (loop recording writes new clips over old ones): each
        is kept as its own file."""
        vol, cl = self.vol, self.vol.cl
        lead = 0                     # bytes of cluster c before its first whole packet
        for _ in range(4096):
            limit = min(pos + self._free_run(c, (64 << 30) // cl) * cl, data_end)
            run, step, off = streams.ts_run(vol.m, pos + lead, limit)
            if not run:
                return
            jump = streams.ts_jump(vol.m, pos + lead, run, step, off)
            size = lead + (run if jump is None else jump * step)
            n = -(-size // cl)
            if size >= 1 << 20:      # anything shorter is not worth keeping, or is a chance likeness
                ext = "ts" if step == 188 else "mts"
                if not lead and streams.ts_begins(vol.m, pos, step, off):
                    self._found_other(c, "video", ext, size, list(range(c, c + n)), GRADE_UNVERIFIED,
                                      f"a recording from its beginning: all {size // step} of its packets follow "
                                      "one another. This kind of video does not say how long it should be, so "
                                      "play it through to see that it runs to the end")
                else:
                    self._found_other(c, "video", ext, size, list(range(c, c + n)), GRADE_PARTIAL,
                                      "a stretch from the middle of a recording, with its beginning missing; most "
                                      "players will play it from where it starts")
            if jump is None or c + n > vol.max_cluster or not self._avail[c + n]:
                return
            # the other recording carries on from the next whole cluster
            lead = (size - n * cl) % step
            c, pos = c + n, pos + n * cl

    def _name_found(self, found) -> None:
        """Number the photos found by scanning; a listed photo left unfinished that began where
        one of them begins was that photo's out-of-date folder entry."""
        stale: dict[int, list[Item]] = {}
        for old in self._items:
            if old.kind == "jpeg" and old.grade in (GRADE_REVIEW, GRADE_PARTIAL):
                stale.setdefault(old.start, []).append(old)
        for n, it in enumerate(sorted(found, key=lambda i: i.start), 1):
            it.name = f"found_{n:05d}.jpg"
            self._items.append(it)
            for old in stale.get(it.start, ()):      # the listed photo that began here is this one's stale folder entry
                old.grade, old.chain, old.candidates, old.percent = GRADE_LOST, [], [], 0.0
                old.note = f"its space now holds a different, complete photo (saved as {it.name})"

    def _trailer_note(self, it, r) -> str:
        """What follows the picture inside its file. Phones add a short video (a motion or live
        photo), 3D cameras a second picture; either can be checked for being whole."""
        plain = "has extra data after the picture, copied without checking"
        try:
            data = self.vol.read_chain(r.chain, it.size)
            at = int(r.image_bytes)
            near = bytes(data[at:at + 4096]).find(b"ftyp")
            if near >= 4:
                got = boxes_length(data, at + near - 4, len(data))
                if got and (b"moov" in got[1]) and at + near - 4 + got[0] >= len(data) - max(4096, len(data) // 50):
                    return ("a motion photo: the picture passes its checks, and the short video stored after it "
                            "runs unbroken to the end of the file (its frames themselves were not checked)")
            tail = len(data) - at
            if tail >= 12 and bytes(data[-8:]) == b"ctrace\0\0" and int.from_bytes(data[-12:-8], "little") == tail - 12:
                # a block of processing notes some phone cameras add; it states its own length
                return ("the phone camera's own processing notes follow the picture, and they end exactly "
                        "where they say they do, so the file is whole")
            if bytes(data[at:at + 2]) == b"\xff\xd8" and audit_jpeg(bytes(data[at:]))[0]:
                return "holds a second picture after the first (as 3D photos do), which also passes its check"
        except Exception:  # noqa: BLE001
            pass
        return plain

    def _other_partitions(self) -> None:
        """A card can hold more than one partition (cards from game consoles, dashcams, single-board
        computers, or split by hand). Each one this tool can read is recovered like the first."""
        from .exfat import ExFatVolume
        from .fat import FatVolume, all_volumes
        first = self._first_vol
        if self.unreadable or not hasattr(first, "part"):
            return
        others = [(kind, off) for kind, off in all_volumes(first.m) if off != first.part]
        self.summary["partitions"] = 1 + len(others)
        for n, (kind, off) in enumerate(others, 2):
            try:
                vol = ExFatVolume(first.m, off) if kind == "exfat" else FatVolume(first.m, off)
                vol.survey = getattr(first, "survey", None)
                self.log(f"Partition {n} of {1 + len(others)} on this card")
                sub = Recovery(first.m, self.out, None, _taken=self._taken, _vol=vol)
                sub.log, sub.always_ask, sub.include_existing = self.log, self.always_ask, self.include_existing
                sub.blank_ranges = self.blank_ranges
                sub.run()
            except Exception as e:  # noqa: BLE001
                self.problems.append(f"partition {n}: {type(e).__name__}: {e}")
                continue
            for it in sub.items:
                if getattr(it, "_owner", None) is None:
                    it._owner = sub
                it.folder = f"partition {n}/{it.folder}".rstrip("/")
            self.items += sub.items
            self.problems += sub.problems
            self._existing += sub._existing
            a, b = self.summary.get("still_on_card"), sub.summary.get("still_on_card")
            if a and b:
                for k in ("files", "photos", "videos", "other", "bytes"):
                    a[k] += b[k]
                a["saved"] = a["saved"] or b["saved"]
                for name, cnt in b["folders"].items():
                    a["folders"].setdefault(f"partition {n}: {name}", cnt)
            elif b:
                self.summary["still_on_card"] = b
        if others:
            self.summary["counts"] = self._counts()
            self.summary["files_listed"] = len(self.items)
            self.summary["problems"] = self.problems[:50]
            self.summary["seconds"] = round(time.time() - self._t0, 1)

    def _findings(self) -> list[dict]:
        """Anything that makes this a card no tool can recover from: encrypted, wiped, or not the size it says."""
        first = self._first_vol
        survey = getattr(first, "survey", None) or getattr(self.vol, "survey", None)
        if survey is None:
            return []
        from .diagnose import diagnose
        got = sum(n for g, n in self.summary["counts"].items() if g not in (GRADE_LOST, GRADE_LIVE))
        try:
            return diagnose(first.m, survey, first, self._existing, bool(self.unreadable), got)
        except Exception as e:  # noqa: BLE001
            self.problems.append(f"checking what kind of card this is: {type(e).__name__}: {e}")
            return []

    def _save_existing(self) -> dict | None:
        """Files that were never deleted. They are not lost, so normally they are only counted and
        the report says they are there. When a computer cannot open the card at all (its start is
        damaged), or when asked, they are copied out too: then they are what the person came for."""
        vol = self._first_vol
        live = getattr(self, "_existing", [])
        if not live:
            return None
        kind = lambda e: "photos" if e.ext in PHOTO_EXT else "videos" if e.ext in VIDEO_EXT else "other"
        info = dict(files=len(live), photos=0, videos=0, other=0, bytes=sum(e.size for e in live), saved=False, folders={})
        media: dict[str, int] = {}
        for e in live:
            info[kind(e)] += 1
            top = e.path or "(top folder)"
            info["folders"][top] = info["folders"].get(top, 0) + 1
            media[top] = media.get(top, 0) + (kind(e) != "other")
        # folders that hold photos and videos first: those are what the person needs to hear about
        info["folders"] = dict(sorted(info["folders"].items(), key=lambda kv: (-media[kv[0]], -kv[1]))[:15])
        save = self.out and (self.include_existing or getattr(vol, "repaired", False))
        if not save:
            return info
        self.log(f"Saving {len(live)} files that are still on the card")
        taken = self._taken if getattr(self, "_taken", None) is not None else set()
        for k, e in enumerate(live):
            self.progress = (k, len(live))
            it = Item(name=e.name, folder=e.path, size=e.size, start=e.start,
                      kind="jpeg" if e.ext in JPEG_EXT else "other", source="still on the card", mtime=e.mtime)
            self.items.append(it)
            try:
                chain = vol.hint_chain(e) or vol.chain(e.start)
                if not chain or len(chain) * vol.cl < e.size:
                    it.note = "the card's record of where this file is stored is damaged"
                    continue
                rel = "/".join([FOLDER_LIVE] + [_safe(p) for p in e.path.split("/") if p] + [_safe(e.name)])
                k2, base = 1, rel
                while rel.lower() in taken:
                    k2 += 1
                    stem, dot, ext = base.rpartition(".")
                    rel = f"{stem} ({k2}){dot}{ext}" if dot else f"{base} ({k2})"
                taken.add(rel.lower())
                dest = self.out / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                left = e.size
                with open(dest, "wb") as f:
                    for c in chain:
                        if left <= 0:
                            break
                        block = vol.cluster(c)[:left]
                        f.write(block)
                        left -= len(block)
                if e.mtime:
                    try:
                        y, mo, d, h, mi = e.mtime
                        ts = time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))
                        os.utime(dest, (ts, ts))
                    except (ValueError, OverflowError, OSError):
                        pass
                it.grade, it.percent, it.saved_as, it.out_bytes = GRADE_LIVE, 100.0, rel, e.size
                it.method = "copied as it is"
            except Exception as ex:  # noqa: BLE001
                self.problems.append(f"{e.name}: {type(ex).__name__}: {ex}")
                it.note = "the tool hit an unexpected problem with this file and skipped it"
        info["saved"] = True
        return info

    def _counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for it in self.items:
            counts[it.grade] = counts.get(it.grade, 0) + 1
        return counts

    # ---- close calls decided by a person (or a program driving the tool) ----
    def review_queue(self) -> list[int]:
        """Indexes of photos waiting for someone to choose the next piece."""
        return [i for i, it in enumerate(self.items)
                if it.grade == GRADE_REVIEW and it.kind == "jpeg" and it.candidates and it._last is not None]

    def review_images(self, index: int, width: int = 420) -> dict:
        """For one waiting photo: what each candidate would make the picture look like
        around the join, as JPEG bytes, plus the photo's own preview if it has one."""
        from PIL import ImageFile
        it = self.items[index]
        if getattr(it, "_owner", self) is not self:
            return it._owner.review_images(it._owner.items.index(it), width)
        r: Result = it._last
        vol, info = self.vol, r.info
        rows_done = (r.percent / 100.0) * info.height
        top = max(0, int(rows_done - info.height * 0.2))
        bottom = min(info.height, int(rows_done + info.height * 0.3))
        out = dict(name=it.name, percent=it.percent, candidates=[], preview=None)
        if info.thumb:
            out["preview"] = bytes(info.thumb)
        old = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            for score, c in it.candidates[:6]:
                chain, used, cc = list(r.chain) + [c], set(r.chain) | {c}, c
                for _ in range(max(3, -(-(it.size or 0) // vol.cl) // 4)):   # let it run on for a quarter of the photo
                    cc = self._rb._next_avail(cc, used)
                    if cc is None:
                        break
                    chain.append(cc)
                    used.add(cc)
                try:
                    with Image.open(io.BytesIO(vol.read_chain(chain) + b"\xff\xd9")) as im:
                        im.draft("RGB", (max(1, im.width // 2), max(1, im.height // 2)))
                        k = im.height / info.height
                        im = im.convert("RGB").crop((0, int(top * k), im.width, int(bottom * k)))
                        im.thumbnail((width, width * 3))
                        buf = io.BytesIO()
                        im.save(buf, "JPEG", quality=80)
                    out["candidates"].append(dict(cluster=c, image=buf.getvalue(), join=(rows_done - top) / (bottom - top)))
                except Exception:  # noqa: BLE001
                    continue
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = old
        return out

    def review_pick(self, index: int, cluster: int | None) -> str:
        """Apply a choice. None means "none of these": the photo stays partial.
        Returns the photo's new grade."""
        it = self.items[index]
        if getattr(it, "_owner", self) is not self:
            grade = it._owner.review_pick(it._owner.items.index(it), cluster)
            self.summary["counts"] = self._counts()
            if self.out:
                self._write_report()
            return grade
        r: Result = it._last
        if cluster is None:
            it.grade, it.candidates = GRADE_PARTIAL, []
            it.note = "a person looked and none of the possible next pieces fitted"
        else:
            if cluster not in [c for _, c in it.candidates]:
                raise ValueError("not one of the offered pieces")
            it._forced[len(r.chain)] = cluster
            r2 = self._rb.rebuild(it.start, it.size, forced=dict(it._forced))
            it._last = r2
            for rel in (it.saved_as, it.filled_as):      # what was saved before is about to be replaced
                if rel and self.out:
                    (self.out / rel).unlink(missing_ok=True)
                    self._taken.discard(rel.lower())
            it.saved_as = it.filled_as = ""
            if r2.status == "complete" and r2.exact_end:
                pieces = 1 + sum(1 for a, b in zip(r2.chain, r2.chain[1:]) if b != a + 1)
                self._accept(it, r2, f"rebuilt from {pieces} pieces, {len(it._forced)} chosen by eye")
                it.note, it.candidates, it.saved_as = "", [], ""
                self._avail[np.asarray(r2.chain, np.int64)] = False
                self._deliver_item(it)
            else:
                it.chain, it.percent, it.picks, it.candidates = r2.chain, r2.percent, r2.picks, r2.candidates
                it.note = r2.reason
                it.grade = GRADE_REVIEW if (r2.status == "review" and r2.candidates) else GRADE_PARTIAL
                self._deliver_item(it)
        self.summary["counts"] = self._counts()
        if self.out and self._only is None and not self._part:
            self._write_report()
        return it.grade

    def _accept(self, it: Item, r: Result, method: str) -> None:
        it.chain, it.percent, it.picks = r.chain, 100.0, r.picks
        it.size = r.size
        it.method = method
        it.grade = GRADE_VERIFIED if r.has_preview else GRADE_NO_PREVIEW

    def _unsupported(self, it: Item, r: Result) -> None:
        """Progressive and other unusual JPEGs cannot be followed block by block. They are
        taken only if whole: the marker structure must end exactly at the recorded size,
        the picture must open, and it must match its preview band by band."""
        vol = self.vol
        n = -(-it.size // vol.cl)
        if it.start + n - 1 > vol.max_cluster:
            it.note = "start cluster is outside the card"
            return
        data = bytes(vol.read_run(it.start, it.size))
        why = None
        if jpeg.find_end(data) != it.size:
            why = "stored in pieces or overwritten, and this kind cannot be rebuilt yet"
        else:
            verdict = pixel_audit(data, r.info)
            if verdict is False:
                why = "does not match its own preview, so it is probably stored in pieces"
        if why:
            it.note = f"{r.reason}: {why}"
            return
        it.chain, it.percent, it.method = list(range(it.start, it.start + n)), 100.0, "found whole"
        if verdict is True:
            it.grade = GRADE_VERIFIED
            it.note = f"{r.reason}: checked against its preview"
        else:
            it.grade = GRADE_UNVERIFIED
            it.note = f"{r.reason}: opens correctly, but has no preview to check the picture against"

    # ---- output -----------------------------------------------------------
    def _deliver(self) -> None:
        """The mandatory final audit, then write files. Nothing reaches Recovered
        without passing the audit on the exact bytes being written."""
        self._taken: set[str] = self._shared_taken if self._shared_taken is not None else set()
        todo = [it for it in self.items if it.chain]
        if self.blank_ranges:
            # which clusters of this layout touch a part of the card that could not be read
            vol, hit = self.vol, set()
            def runs_through(it) -> bool:
                """Does this photo's own ending sit exactly where its recorded size says, counting
                straight on from its start? (The ending is the end marker, or the block of notes some
                phone cameras add after it, which states its own length.)"""
                n = -(-it.size // vol.cl)
                if it.kind != "jpeg" or not it.chain or it.size < 16 or it.start + n - 1 > vol.max_cluster:
                    return False
                last = bytes(vol.read_run(it.start, it.size)[-12:])
                if last[-2:] == b"\xff\xd9":
                    return True
                if last[-8:] == b"ctrace\0\0":
                    extra = int.from_bytes(last[:4], "little") + 12
                    return 12 <= extra < it.size - 2 and \
                        bytes(vol.read_run(it.start, it.size)[it.size - extra - 2:it.size - extra]) == b"\xff\xd9"
                return False

            for a, b in self.blank_ranges:
                c0 = max(2, (a - vol.data_off) // vol.cl + 2)
                c1 = min(vol.max_cluster, (b - 1 - vol.data_off) // vol.cl + 2)
                hit.update(range(c0, c1 + 1))
            for it in self.items:
                if it.grade in (GRADE_PARTIAL, GRADE_REVIEW, GRADE_LOST) and it.size and hit.isdisjoint(it.chain) \
                        and not hit.isdisjoint(range(it.start, it.start + -(-it.size // vol.cl))):
                    if it.grade == GRADE_REVIEW:
                        # nothing for a person to choose between: the next piece is simply unreadable
                        it.grade, it.candidates, it.note = GRADE_PARTIAL, [], ""
                    it.note = (it.note + "; " if it.note else "") + (
                        "part of the card where this file is stored could not be read"
                        + (f"; the picture is good down to about {it.percent:.0f}% and lost from there on"
                           if it.kind == "jpeg" and it.chain else ""))
                    # If the photo's own end marker sits exactly where its recorded size says, the file
                    # lies in one run straight through the unreadable patch. Then the whole file is
                    # worth keeping as well: picture viewers carry on past a gap, usually with the
                    # rest shifted or discoloured, which is far more than a sliver of the top.
                    it._whole = runs_through(it)
            for it in todo:
                if not hit.isdisjoint(it.chain):
                    it.note = (it.note + "; " if it.note else "") + \
                        "part of this file was in an area of the card that could not be read, and is blank there"
                    if it.grade in (GRADE_VERIFIED, GRADE_NO_PREVIEW, GRADE_UNVERIFIED):
                        it.grade = GRADE_PARTIAL        # bytes known to be missing: never "recovered"
                    if it.kind == "jpeg" and it.percent < 100.0 and it.size and \
                            not hit.isdisjoint(range(it.start, it.start + -(-it.size // vol.cl))):
                        it._whole = runs_through(it)
        if todo:
            self.log(f"Checking every recovered file once more and saving {len(todo)} files")
        for k, (it, verdict) in enumerate(_ahead(todo, self._audit, _workers())):
            self.progress = (k, len(todo))
            if self._out_full:
                it._unsaved, it.saved_as = True, ""
                it.note = (it.note + "; " if it.note else "") + FULL_NOTE
                continue
            try:
                self._deliver_item(it, verdict)
            except Exception as e:  # noqa: BLE001
                if isinstance(e, OSError) and (e.errno == errno.ENOSPC or getattr(e, "winerror", None) in (39, 112)):
                    # the drive being saved to is full: nothing more can be written, and saying so
                    # once is worth more than the same failure for every file that follows
                    self._out_full = True
                    self.problems.append(f"{it.name}: the drive being saved to is full")
                    it._unsaved, it.saved_as = True, ""
                    it.note = (it.note + "; " if it.note else "") + FULL_NOTE
                    continue
                self.problems.append(f"{it.name}: could not be saved ({type(e).__name__}: {e})")
                it.note = (it.note + "; " if it.note else "") + "this file could not be written to the output folder"
                it.saved_as = ""
                if it.grade != GRADE_LOST:
                    it.grade = GRADE_REVIEW

    def _data(self, it: Item) -> bytes:
        vol = self.vol
        if -1 in it.chain:       # a video with pieces missing: blank where the card has nothing
            return b"".join(bytes(vol.cl) if c < 0 else bytes(vol.cluster(c)) for c in it.chain)[:it.size]
        return vol.read_chain(it.chain, it.size if (it.percent == 100.0 or it.kind != "jpeg") else None)

    def _audit(self, it: Item):
        """The final check of one file's exact bytes: None if it needs none, else (passed, note)."""
        if not (it.kind == "jpeg" and it.percent == 100.0 and it.grade in (GRADE_VERIFIED, GRADE_NO_PREVIEW)):
            return None
        try:
            ok, note, _ = audit_jpeg(self._data(it))
        except Exception as e:  # noqa: BLE001
            ok, note = False, f"the check itself failed ({type(e).__name__})"
        return ok, note

    def _deliver_item(self, it: Item, verdict="ask") -> None:
        vol, taken = self.vol, self._taken
        if True:
            if not it.chain:
                return
            data = self._data(it)
            if verdict == "ask":
                verdict = self._audit(it)
            if verdict is not None:
                ok, note = verdict
                if not ok:
                    whole = all(b == a + 1 for a, b in zip(it.chain, it.chain[1:]))
                    if whole and "preview" in note:
                        it.note = (it.note + "; " if it.note else "") + "the picture " + note + \
                            " (its preview may be out of date)"
                        it.grade = GRADE_NO_PREVIEW      # complete and in one piece, but the preview did not vouch for it
                    else:
                        it.grade, it.note = GRADE_REVIEW, "failed the final check: " + note
            it.out_bytes = len(data)
            if not self.out:
                return
            folder = FOLDER_OK if it.grade in (GRADE_VERIFIED, GRADE_NO_PREVIEW, GRADE_UNVERIFIED) else FOLDER_PARTIAL
            # Sorted by what the file is, so that the photos are one click away. The folders the
            # card had are not rebuilt (the report says where each file was): this rescues files,
            # it does not put a card back the way it was.
            sub = kind_folder(it.name, data)
            name = _safe(it.name)
            if folder == FOLDER_PARTIAL and it.percent < 100.0:
                stem, dot, ext = name.rpartition(".")
                name = f"{stem} ({it.percent:.0f}%){dot}{ext}" if dot else f"{name} ({it.percent:.0f}%)"
            rel = "/".join(x for x in (folder, sub, name) if x)
            k = 1
            while rel.lower() in taken:
                stem, dot, ext = name.rpartition(".")
                k += 1
                rel = "/".join(x for x in (folder, sub, f"{stem} ({k}){dot}{ext}" if dot else f"{name} ({k})") if x)
            taken.add(rel.lower())
            dest = self.out / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            _write(dest, data)
            if it.mtime:
                try:
                    y, mo, d, h, mi = it.mtime
                    ts = time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))
                    os.utime(dest, (ts, ts))
                except (ValueError, OverflowError, OSError):
                    pass
            it.saved_as = rel
            if getattr(it, "_whole", False):
                stem, dot, ext = rel.rpartition(".")
                wrel = stem.rsplit(" (", 1)[0] + f" (whole file, damaged from {it.percent:.0f}%).jpg"
                _write(self.out / wrel, bytes(vol.read_run(it.start, it.size)))
                taken.add(wrel.lower())
                it.note += (f"; the whole file is saved too, as {wrel.rpartition('/')[2]}: below the unreadable "
                            "part it may look shifted or discoloured")
            if it.kind == "jpeg" and it.percent < 100.0:
                filled = preview_fill(data, it.percent)
                if filled:
                    frel = "/".join([FOLDER_FILLED] + rel.split("/")[1:])
                    stem, dot, ext = frel.rpartition(".")
                    frel = stem.rsplit(" (", 1)[0] + f" ({it.percent:.0f}% real, rest from preview).jpg"
                    fdest = self.out / frel
                    fdest.parent.mkdir(parents=True, exist_ok=True)
                    _write(fdest, filled)
                    taken.add(frel.lower())
                    it.filled_as = frel

    def _write_report(self) -> None:
        from .report import write_html
        rows = [dict(name=i.name, folder=i.folder, grade=i.grade, percent=i.percent, method=i.method, filled_as=i.filled_as,
                     note=i.note, bytes=i.out_bytes, saved_as=i.saved_as, source=i.source,
                     start_cluster=i.start, clusters=len(i.chain), picks=i.picks, candidates=i.candidates,
                     size=i.size, modified=list(i.mtime) if i.mtime else None, runs=_runs(i.chain))
                for i in self.items]
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "report.json").write_text(json.dumps(dict(summary=self.summary, files=rows), indent=1))
        write_html(self.out / "report.html", self.summary, rows)


def kind_folder(name: str, data: bytes = b"") -> str:
    """The folder a recovered file is sorted into, by what kind of file it is."""
    ext = name.rpartition(".")[2].lower() if "." in name else ""
    if ext in JPEG_EXT or ext in ("png", "gif", "bmp"):
        # thumbnails, icons and pictures apps keep for themselves would bury the real photos
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                with Image.open(io.BytesIO(data)) as im:
                    if max(im.size) < SMALL_PICTURE:
                        return FOLDER_SMALL
        except Exception:  # noqa: BLE001
            pass
    return ("Photos" if ext in PHOTO_EXT else "Videos" if ext in VIDEO_EXT else "Documents" if ext in DOC_EXT
            else "Music and sound" if ext in MUSIC_EXT else "Other files")


def _write(dest: Path, data: bytes) -> None:
    """Write a file whole or not at all: a cut-short copy must never be left among the results."""
    try:
        dest.write_bytes(data)
    except OSError:
        try:
            dest.unlink()
        except OSError:
            pass
        raise


def _runs(chain: list[int]) -> list[list[int]]:
    """A chain of clusters as [first, count] runs: where on the card the file's pieces were."""
    out: list[list[int]] = []
    for c in chain:
        if out and c >= 0 and out[-1][0] + out[-1][1] == c:
            out[-1][1] += 1
        else:
            out.append([c, 1])
    return out


def _safe(name: str) -> str:
    bad = '<>:"/\\|?*'
    out = "".join("_" if (ch in bad or ord(ch) < 32) else ch for ch in name).strip(" .")
    return out or "_"
