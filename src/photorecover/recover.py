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
    mtime: tuple | None = None
    candidates: list = field(default_factory=list)
    picks: list = field(default_factory=list)
    _hint = None                    # chain suggested by the file system, if any
    _last = None                    # last rebuild attempt


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


class Recovery:
    def __init__(self, image, out_dir=None, log=None):
        self.vol = open_volume(image)
        self.out = Path(out_dir) if out_dir else None
        self.log = log or (lambda msg: None)
        self.items: list[Item] = []
        self.summary: dict = {}
        self.problems: list[str] = []

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
        blank = vol.blank_clusters()
        allocated = vol.allocated()
        avail = ~allocated & ~blank
        avail[:2] = False
        used_fraction = 1.0 - blank[2:].mean() if vol.ncl else 0.0
        targets = [e for e in entries if not e.live and not e.is_dir and e.size > 0]
        self.log(f"{len(targets)} deleted or lost files listed; {used_fraction:.0%} of the card holds data")
        rb = Rebuilder(vol, avail)
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
        for it in jpegs:
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
        for it in videos:
            v = attempt(it, vr.rebuild, it.start, it.size)
            if v is None:
                continue
            if v.status == "failed":
                it.note = v.reason
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
            for it in pending:
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
        for c in heads:
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
        self._deliver()
        counts: dict[str, int] = {}
        for it in items:
            counts[it.grade] = counts.get(it.grade, 0) + 1
        self.summary = dict(tool=f"photorecover {__version__}", filesystem=vol.kind,
                            cluster_size=cl, clusters=vol.ncl, card_in_use=round(float(used_fraction), 3),
                            files_listed=len(items), counts=counts, seconds=round(time.time() - t0, 1),
                            fast_decoder=jpeg.HAVE_C, problems=self.problems[:50])
        if self.out:
            self._write_report()
        return self.summary

    def _accept(self, it: Item, r: Result, method: str) -> None:
        it.chain, it.percent, it.picks = r.chain, 100.0, r.picks
        it.size = r.size
        it.method = method
        it.grade = GRADE_VERIFIED if r.has_preview else GRADE_NO_PREVIEW

    def _unsupported(self, it: Item, r: Result) -> None:
        """Progressive and other unusual JPEGs: taken only if whole, checked by opening them."""
        vol = self.vol
        n = -(-it.size // vol.cl)
        if it.start + n - 1 > vol.max_cluster:
            it.note = "start cluster is outside the card"
            return
        data = vol.read_run(it.start, it.size)
        ok = data[-2:] == b"\xff\xd9"
        if ok:
            try:
                with Image.open(io.BytesIO(data)) as im:
                    im.load()
            except Exception:  # noqa: BLE001
                ok = False
        if ok:
            it.chain, it.percent = list(range(it.start, it.start + n)), 100.0
            it.grade, it.method = GRADE_UNVERIFIED, "found whole"
            it.note = f"{r.reason}: opens correctly, but this kind cannot be checked block by block"
        else:
            it.note = f"{r.reason}: stored in pieces, and this kind cannot be rebuilt yet"

    # ---- output -----------------------------------------------------------
    def _deliver(self) -> None:
        """The mandatory final audit, then write files. Nothing reaches Recovered
        without passing the audit on the exact bytes being written."""
        vol = self.vol
        taken: set[str] = set()
        for it in self.items:
            if not it.chain:
                continue
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
                continue
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

    def _write_report(self) -> None:
        from .report import write_html
        rows = [dict(name=i.name, folder=i.folder, grade=i.grade, percent=i.percent, method=i.method,
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
