"""The whole recovery run: scan a card image, recover what is whole, rebuild
what is fragmented, audit everything, write the output folders and the report.

The image is only ever read. Output goes to a separate folder.
"""
from __future__ import annotations

import io
import json
import os
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from . import __version__, jpeg
from .fat import Entry, open_volume
from .rebuild import Judge, Rebuilder, Result
from .video import CONTAINERS, VideoRebuilder

JPEG_EXT = {"jpg", "jpeg", "jpe"}
SIGNATURES = {            # extension -> bytes a file of that type starts with (offset, magic)
    "png": (0, b"\x89PNG\r\n\x1a\n"), "gif": (0, b"GIF8"), "bmp": (0, b"BM"),
    "mp4": (4, b"ftyp"), "mov": (4, None), "3gp": (4, b"ftyp"), "m4v": (4, b"ftyp"),
    "heic": (4, b"ftyp"), "avi": (0, b"RIFF"), "wav": (0, b"RIFF"), "pdf": (0, b"%PDF"),
    "zip": (0, b"PK"), "cr2": (0, b"II*\0"), "nef": (0, b"MM\0*"), "arw": (0, b"II*\0"),
    "dng": (0, None), "tif": (0, None), "mp3": (0, None),
}
BOX_TYPES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"meta", b"udta", b"pnot", b"moof",
             b"mfra", b"sidx", b"styp", b"PICT", b"pict", b"thum", b"junk"}

FOLDER_OK = "Recovered"
FOLDER_PARTIAL = "Recovered - Partial"
FOLDER_FILLED = "Recovered - Preview filled"

GRADE_VERIFIED = "Verified"
GRADE_NO_PREVIEW = "Complete, no preview"
GRADE_UNVERIFIED = "Recovered, unverified"
GRADE_PARTIAL = "Partial"
GRADE_REVIEW = "Needs review"
GRADE_LOST = "Not recoverable"


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


def check_other(ext: str, data) -> str | None:
    """'verified', 'unverified' or None (clearly not this file any more)."""
    sig = SIGNATURES.get(ext)
    if sig and sig[1] is not None and data[sig[0]:sig[0] + len(sig[1])] != sig[1]:
        return None
    if ext == "png":
        return "verified" if _valid_png(data) else None
    if ext in ("mp4", "mov", "3gp", "m4v", "heic"):
        return "verified" if _valid_isobmff(data) else "unverified"
    if ext == "gif":
        return "verified" if data[-1:] == b";" else "unverified"
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
    if scores:
        med, worst = float(np.median(scores)), max(scores)
        if worst > max(10.0, 3.0 * med):
            at = 100 * scores.index(worst) // len(scores)
            return False, f"does not match its own preview about {at}% of the way down", True
    return True, "", True


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
    return not scores or max(scores) <= max(10.0, 3.0 * float(np.median(scores)))


class Recovery:
    def __init__(self, image, out_dir=None, log=None):
        self.vol = open_volume(image)
        self.out = Path(out_dir) if out_dir else None
        say = log or (lambda msg: None)

        def _log(msg):               # a new step starts: the count within the old one no longer applies
            self.progress = None
            say(msg)
        self.log = _log
        self.items: list[Item] = []
        self.summary: dict = {}
        self.problems: list[str] = []
        self.always_ask = False
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

    def run(self) -> dict:
        t0 = time.time()
        vol = self.vol
        self.log("Reading the card's folders, including deleted ones")
        entries = vol.scan_entries()
        self.old_layout = None
        try:
            from .legacy import find_old_layout
            old = find_old_layout(vol, entries, self.log)
        except Exception as e:  # noqa: BLE001
            old = None
            self.problems.append(f"looking for an older layout: {type(e).__name__}: {e}")
        if old is not None:
            # the card was reformatted with different settings: read it the way it used to be
            self.old_layout = dict(cluster_size=old.cl, data_offset=old.data_off)
            self.vol = vol = old
            entries = vol.scan_entries()
        blank = vol.blank_clusters()
        allocated = vol.allocated()
        avail = ~allocated & ~blank
        avail[:2] = False
        used_fraction = 1.0 - blank[2:].mean() if vol.ncl else 0.0
        targets = [e for e in entries if not e.live and not e.is_dir and e.size > 0]
        self.log(f"{len(targets)} deleted or lost files listed; {used_fraction:.0%} of the card holds data")
        rb = Rebuilder(vol, avail)
        rb.always_ask = self.always_ask
        cl = vol.cl

        def claim(chain):
            avail[np.asarray(chain, np.int64)] = False

        def attempt(it, fn, *args, **kw):
            """One odd file must never stop the whole run."""
            try:
                return fn(*args, **kw)
            except Exception as e:  # noqa: BLE001
                self.problems.append(f"{it.name if it else '?'}: {type(e).__name__}: {e}")
                if it is not None:
                    it.note = "the tool hit an unexpected problem with this file and skipped it"
                return None

        # one item per distinct (start, size); a long name wins over a short one
        seen: dict[tuple[int, int], Item] = {}
        for e in targets:
            ext = e.ext
            start = self._resolve_start(e, ext)
            key = (start, e.size)
            it = Item(name=e.name, folder=e.path, size=e.size, start=start,
                      kind="jpeg" if ext in JPEG_EXT else "other",
                      source="deleted entry" if e.deleted else "lost folder entry", mtime=e.mtime)
            it._hint = vol.hint_chain(e)
            if key not in seen or (seen[key].name.startswith("_") and not e.name.startswith("_")):
                seen[key] = it
        items = sorted(seen.values(), key=lambda i: i.start)
        jpegs = [i for i in items if i.kind == "jpeg"]
        others = [i for i in items if i.kind == "other"]

        # Pass 1: photos still in one piece. Exact structural proof, then they leave the pool.
        self.log(f"Checking {len(jpegs)} photos")
        pending: list[Item] = []
        loose: list[tuple[Item, Result]] = []
        for k, it in enumerate(jpegs):
            self.progress = (k, len(jpegs))
            hint = it._hint
            r = attempt(it, rb.prove_chain, hint, it.size) if hint else None
            if r is not None:
                pieces = 1 + sum(1 for a, b in zip(hint, hint[1:]) if b != a + 1)
                self._accept(it, r, "found whole" if pieces == 1 else
                             f"rebuilt from {pieces} pieces using the card's own record")
                claim(r.chain)
                continue
            r = attempt(it, rb.rebuild, it.start, it.size, contiguous_only=True)
            if r is None:
                continue
            if r.status == "complete" and r.exact_end:
                self._accept(it, r, "found whole")
                claim(r.chain)
            elif r.status == "complete":
                loose.append((it, r))
            elif r.status == "unsupported":
                self._unsupported(it, r)
            elif r.status == "failed":
                it.note = r.reason
            else:
                pending.append(it)

        # Videos: every frame and audio packet listed in the video's index is a checkpoint.
        vr = VideoRebuilder(vol, avail)
        videos = [i for i in others if i.name.rpartition(".")[2].lower() in CONTAINERS and not i._hint]
        if videos:
            self.log(f"Checking {len(videos)} videos")
        for k, it in enumerate(videos):
            self.progress = (k, len(videos))
            v = attempt(it, vr.rebuild, it.start, it.size)
            if v is None:
                continue
            if v.status == "failed":
                it.note = v.reason
                continue
            if v.status == "complete" and not _valid_isobmff(vr.read(v, it.size)):
                # every checkpoint matched, but the file does not end where this entry says:
                # the space now belongs to a different, later video
                it.note = "its space was reused by a later file"
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
                claim(real)
            else:
                it.grade, it.note = GRADE_PARTIAL, v.reason + "; the gaps are left blank and will show as glitches"
        done_videos = {id(i) for i in videos if i.chain}

        # Other file types that are whole.
        for it in others:
            if id(it) in done_videos:
                continue
            n = -(-it.size // cl)
            if not vol.valid(it.start) or it.start + n - 1 > vol.max_cluster:
                it.note = "start cluster is outside the card"
                continue
            chain = it._hint or list(range(it.start, it.start + n))
            if not avail[chain].all():
                it.note = "its space was reused by another file"
                continue
            verdict = check_other(it.name.rpartition(".")[2].lower(), vol.read_chain(chain, it.size))
            if verdict is None:
                it.note = "its space no longer holds this kind of file (overwritten, or stored in pieces)"
                continue
            it.chain, it.percent = chain, 100.0
            it.grade = GRADE_UNVERIFIED
            it.method = "found whole"
            if verdict == "verified":
                it.grade = GRADE_VERIFIED
                claim(chain)
            else:
                it.note = "this file type cannot be checked; open it to make sure"

        # Pass 2: photos in pieces. Two rounds, because every photo finished frees
        # the others from having to consider its clusters.
        if pending:
            self.log(f"Rebuilding {len(pending)} photos that are stored in pieces")
        for rnd in range(2):
            still = []
            for k, it in enumerate(pending):
                self.progress = (k, len(pending))
                r = attempt(it, rb.rebuild, it.start, it.size)
                if r is None:
                    continue
                if r.status == "complete" and r.exact_end:
                    pieces = 1 + sum(1 for a, b in zip(r.chain, r.chain[1:]) if b != a + 1)
                    self._accept(it, r, f"rebuilt from {pieces} pieces")
                    claim(r.chain)
                elif r.status == "complete":
                    loose.append((it, r))
                else:
                    it._last = r
                    still.append(it)
            if len(still) == len(pending):
                break
            pending = still
            if rnd == 0 and pending:
                self.log(f"Second look at {len(pending)} photos, now that the others are out of the way")
        for it in pending:
            r: Result = it._last
            it.chain, it.percent, it.picks = r.chain, r.percent, r.picks
            it.candidates = r.candidates
            it.note = r.reason
            it.grade = GRADE_REVIEW if r.status == "review" else GRADE_PARTIAL
            it.method = "top part recovered"

        # Photos whose picture ends before the recorded size (extra data after the end
        # marker). Only believable if nobody else proved a claim to the same clusters.
        for it, r in loose:
            if avail[np.asarray(r.chain, np.int64)].all():
                self._accept(it, r, "found whole")
                it.note = "has extra data after the picture, copied without checking"
                claim(r.chain)
            else:
                it.note = "its space was reused by a later file"

        # Pass 3: photos with no folder entry left at all.
        heads = [int(c) for c in np.flatnonzero(avail) if vol.cluster(int(c))[:3] == b"\xff\xd8\xff"]
        if heads:
            self.log(f"Looking at {len(heads)} photo starts that have no folder entry")
        n_found = 0
        for k, c in enumerate(heads):
            self.progress = (k, len(heads))
            if not avail[c]:
                continue
            r = attempt(None, rb.rebuild, c, None)
            if r is not None and r.status == "complete":
                n_found += 1
                it = Item(name=f"found_{n_found:05d}.jpg", folder="Found by scanning", size=r.size, start=c,
                          kind="jpeg", source="found by scanning")
                pieces = 1 + sum(1 for a, b in zip(r.chain, r.chain[1:]) if b != a + 1)
                self._accept(it, r, "found whole" if pieces == 1 else f"rebuilt from {pieces} pieces")
                claim(r.chain)
                items.append(it)

        self.items = items
        self._rb, self._avail = rb, avail
        self._deliver()
        self.progress = None
        self.summary = dict(tool=f"photorecover {__version__}", filesystem=vol.kind,
                            cluster_size=cl, clusters=vol.ncl, card_in_use=round(float(used_fraction), 3),
                            files_listed=len(items), counts=self._counts(), seconds=round(time.time() - t0, 1),
                            fast_decoder=jpeg.HAVE_C, problems=self.problems[:50],
                            old_layout=self.old_layout)
        if self.out:
            self._write_report()
        return self.summary

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
        if self.out:
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
        self._taken: set[str] = set()
        for it in self.items:
            self._deliver_item(it)

    def _deliver_item(self, it: Item) -> None:
        vol, taken = self.vol, self._taken
        if True:
            if not it.chain:
                return
            if -1 in it.chain:       # a video with pieces missing: blank where the card has nothing
                data = b"".join(bytes(vol.cl) if c < 0 else bytes(vol.cluster(c)) for c in it.chain)[:it.size]
            else:
                data = vol.read_chain(it.chain, it.size if (it.percent == 100.0 or it.kind != "jpeg") else None)
            if it.kind == "jpeg" and it.percent == 100.0 and it.grade in (GRADE_VERIFIED, GRADE_NO_PREVIEW):
                try:
                    ok, note, _ = audit_jpeg(data)
                except Exception as e:  # noqa: BLE001
                    ok, note = False, f"the check itself failed ({type(e).__name__})"
                if not ok:
                    whole = all(b == a + 1 for a, b in zip(it.chain, it.chain[1:]))
                    if whole and "preview" in note:
                        it.note = (it.note + "; " if it.note else "") + "the picture " + note + \
                            " (its preview may be out of date)"
                    else:
                        it.grade, it.note = GRADE_REVIEW, "failed the final check: " + note
            it.out_bytes = len(data)
            if not self.out:
                return
            folder = FOLDER_OK if it.grade in (GRADE_VERIFIED, GRADE_NO_PREVIEW, GRADE_UNVERIFIED) else FOLDER_PARTIAL
            sub = "/".join(_safe(p) for p in it.folder.split("/") if p)
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
            dest.write_bytes(data)
            if it.mtime:
                try:
                    y, mo, d, h, mi = it.mtime
                    ts = time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))
                    os.utime(dest, (ts, ts))
                except (ValueError, OverflowError, OSError):
                    pass
            it.saved_as = rel
            if it.kind == "jpeg" and it.percent < 100.0:
                filled = preview_fill(data, it.percent)
                if filled:
                    frel = "/".join([FOLDER_FILLED] + rel.split("/")[1:])
                    stem, dot, ext = frel.rpartition(".")
                    frel = stem.rsplit(" (", 1)[0] + f" ({it.percent:.0f}% real, rest from preview).jpg"
                    fdest = self.out / frel
                    fdest.parent.mkdir(parents=True, exist_ok=True)
                    fdest.write_bytes(filled)
                    taken.add(frel.lower())
                    it.filled_as = frel

    def _write_report(self) -> None:
        from .report import write_html
        rows = [dict(name=i.name, folder=i.folder, grade=i.grade, percent=i.percent, method=i.method, filled_as=i.filled_as,
                     note=i.note, bytes=i.out_bytes, saved_as=i.saved_as, source=i.source,
                     start_cluster=i.start, clusters=len(i.chain), picks=i.picks, candidates=i.candidates)
                for i in self.items]
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "report.json").write_text(json.dumps(dict(summary=self.summary, files=rows), indent=1))
        write_html(self.out / "report.html", self.summary, rows)


def _safe(name: str) -> str:
    bad = '<>:"/\\|?*'
    out = "".join("_" if (ch in bad or ord(ch) < 32) else ch for ch in name).strip(" .")
    return out or "_"
