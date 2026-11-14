"""Rebuild fragmented MP4 / MOV / 3GP videos.

A video's index (the `moov` box) lists where every frame and audio packet
starts and how big it is. The first bytes of each one are recognisable, so each
is a checkpoint: a cluster of the card can only be cluster k of the file if
every checkpoint that falls in cluster k looks right there. The file is walked
from its start cluster; where the checkpoints stop matching, the whole card is
searched for the cluster where they match again.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

from .util import ceil_div, next_free

CONTAINERS = {"mp4", "mov", "3gp", "3g2", "m4v"}
TOP_BOXES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"meta", b"udta", b"pnot", b"moof",
             b"mfra", b"sidx", b"styp", b"junk"}
NAL_TYPES_AVC = {1, 5, 6, 7, 8, 9, 12}
MAX_MOOV = 64 << 20


def boxes(buf, a: int, b: int):
    """(type, start, size, header length) of each box between a and b; stops at the first that does not fit."""
    while a + 8 <= b:
        size, typ = struct.unpack_from(">I4s", buf, a)
        head = 8
        if size == 1:
            if a + 16 > b:
                return
            (size,) = struct.unpack_from(">Q", buf, a + 8)
            head = 16
        if size < head or a + size > b:
            return
        yield typ, a, size, head
        a += size


@dataclass
class Track:
    """One track of a video (picture, sound, data) and where each of its samples lies in the file."""
    codec: str
    handler: str
    samples: list[tuple[int, int]]      # (file offset, size)
    first_byte: int | None = None       # learned from the trusted start of the file


def parse_moov(buf: bytes) -> list[Track] | None:
    """buf is a whole moov box. Returns its tracks, or None if it is not sound."""
    try:
        return _parse_moov(buf)
    except (struct.error, IndexError, ValueError, KeyError):
        return None


def _parse_moov(buf: bytes) -> list[Track]:
    """Read every track's sample table: sizes (stsz), chunk offsets (stco/co64), samples per chunk (stsc)."""
    n = len(buf)

    def find(a, b, path):
        for typ, o, size, head in boxes(buf, a, b):
            if typ == path[0]:
                if len(path) == 1:
                    yield o, size, head
                else:
                    yield from find(o + head, o + size, path[1:])

    tracks = []
    for to, tsz, th in find(8, n, [b"trak"]):
        handler = ""
        for o, sz, h in find(to + th, to + tsz, [b"mdia", b"hdlr"]):
            handler = buf[o + 16:o + 20].decode("latin1")
        stbl = list(find(to + th, to + tsz, [b"mdia", b"minf", b"stbl"]))
        if not stbl:
            continue
        so, ssz, sh = stbl[0]
        sub = {typ: (o, sz) for typ, o, sz, _ in boxes(buf, so + sh, so + ssz)}
        codec = buf[sub[b"stsd"][0] + 20:sub[b"stsd"][0] + 24].decode("latin1") if b"stsd" in sub else ""
        o, _ = sub[b"stsz"]
        uniform, count = struct.unpack_from(">II", buf, o + 12)
        if count > 50_000_000:
            raise ValueError("absurd sample count")
        sizes = [uniform] * count if uniform else list(struct.unpack_from(f">{count}I", buf, o + 20))
        if b"stco" in sub:
            o, _ = sub[b"stco"]
            (m,) = struct.unpack_from(">I", buf, o + 12)
            chunks = struct.unpack_from(f">{m}I", buf, o + 16)
        else:
            o, _ = sub[b"co64"]
            (m,) = struct.unpack_from(">I", buf, o + 12)
            chunks = struct.unpack_from(f">{m}Q", buf, o + 16)
        o, _ = sub[b"stsc"]
        (m,) = struct.unpack_from(">I", buf, o + 12)
        stsc = [struct.unpack_from(">III", buf, o + 16 + 12 * i) for i in range(m)]
        samples, si, run = [], 0, 0
        for ci, off in enumerate(chunks):
            while run + 1 < len(stsc) and stsc[run + 1][0] <= ci + 1:
                run += 1
            for _ in range(stsc[run][1] if stsc else 0):
                if si >= len(sizes):
                    break
                samples.append((off, sizes[si]))
                off += sizes[si]
                si += 1
        tracks.append(Track(codec, handler, samples))
    if not tracks:
        raise ValueError("no tracks")
    return tracks


def checkpoint_ok(tr: Track, data: bytes, size: int) -> bool | None:
    """Do these bytes look like the start of a sample of this track? None = cannot tell."""
    c = tr.codec
    if len(data) < 8 or size < 1:
        return None
    if c in ("avc1", "avc3"):
        if size < 5:
            return None
        (ln,) = struct.unpack_from(">I", data, 0)
        h = data[4]
        return 1 <= ln <= size - 4 and not h & 0x80 and (h & 0x1F) in NAL_TYPES_AVC
    if c in ("hvc1", "hev1"):
        if size < 6:
            return None
        (ln,) = struct.unpack_from(">I", data, 0)
        return 2 <= ln <= size - 4 and not data[4] & 0x80 and (data[4] >> 1) <= 40
    if c == "mp4v":
        return data[:3] == b"\x00\x00\x01"
    if c in ("jpeg", "mjpa", "mjpb"):
        return data[:2] == b"\xff\xd8"
    if c == "gpmd":
        return data[:4] == b"DEVC"
    if tr.first_byte is not None:
        return data[0] == tr.first_byte
    return None


@dataclass
class VideoResult:
    """How rebuilding one video turned out."""
    status: str = "failed"            # complete | partial | failed
    reason: str = ""
    chain: list[int] = field(default_factory=list)   # -1 = cluster missing on the card
    checked: int = 0
    passed: int = 0
    uncertain_joins: int = 0
    missing: int = 0


class VideoRebuilder:
    """Rebuilds videos on one card from their own index (see the top of this file)."""

    def __init__(self, vol, avail: np.ndarray):
        self.vol, self.avail = vol, avail
        self.img = np.frombuffer(vol.m, np.uint8)

    def _next(self, c: int, used: set[int]) -> int | None:
        """The next free cluster after c that this video is not already using."""
        return next_free(self.avail, c, used, self.vol.max_cluster)

    def _prev(self, c: int, used: set[int]) -> int | None:
        c -= 1
        if c >= 2 and self.avail[c] and c not in used:
            return c
        return None

    def _find_moov(self, start: int, size: int):
        """Returns (moov bytes, file offset of moov, card clusters holding it, which cluster of the
        file the first of those is) or None."""
        vol, cl = self.vol, self.vol.cl
        head = vol.read_run(start, min(size, 16 * cl))
        pos, mdat_end = 0, None
        while pos + 8 <= len(head):
            bsize, typ = struct.unpack_from(">I4s", head, pos)
            if bsize == 1 and pos + 16 <= len(head):
                (bsize,) = struct.unpack_from(">Q", head, pos + 8)
            if typ not in TOP_BOXES or bsize < 8:
                return None
            if typ == b"moov":
                if pos + bsize > size or bsize > MAX_MOOV:
                    return None
                k0, k1 = pos // cl, (pos + bsize - 1) // cl
                clusters = list(range(start + k0, start + k1 + 1))      # moov at the front: taken as one run
                if clusters[-1] > vol.max_cluster:
                    return None
                data = vol.read_run(start, pos + bsize)[pos:]
                return bytes(data), pos, clusters, k0
            if typ == b"mdat":
                mdat_end = pos + bsize
                break
            pos += bsize
        if mdat_end is None or mdat_end + 8 > size:
            return None
        # moov after the picture data: it sits at a known offset inside its cluster, wherever that cluster is
        k0, within = mdat_end // cl, mdat_end % cl
        want = np.frombuffer(b"moov", np.uint8)
        cand = np.flatnonzero(self.avail)
        cand = cand[cand >= 2]
        if within + 8 <= cl:
            offs = vol.data_off + (cand - 2) * cl + within + 4
            hit = np.ones(len(cand), bool)
            for i in range(4):
                hit &= self.img[offs + i] == want[i]
            cand = cand[hit]
        else:
            return None        # the 8-byte box header straddles two clusters: not handled
        guess = start + k0     # prefer the place it would be if the file were in one piece
        for c in sorted((int(x) for x in cand), key=lambda x: (x != guess, x)):
            o = vol.data_off + (c - 2) * cl + within
            (msize,) = struct.unpack_from(">I", vol.m, o)
            if not 16 <= msize <= min(MAX_MOOV, size - mdat_end):
                continue
            span = (within + msize - 1) // cl + 1
            if c + span - 1 > vol.max_cluster or not self.avail[c:c + span].all():
                continue
            data = bytes(vol.m[o:o + msize])
            tracks = parse_moov(data)
            if tracks and all(off + sz <= size for t in tracks for off, sz in t.samples[-1:]):
                return data, mdat_end, list(range(c, c + span)), k0
        return None

    def rebuild(self, start: int, size: int) -> VideoResult:
        """Walk the video that starts at cluster start, cluster by cluster, checking every frame and
        sound packet its index places in each one. Where they stop matching, search the card for
        the cluster where they match again; leave a gap (-1) where no single cluster does."""
        vol, cl = self.vol, self.vol.cl
        res = VideoResult()
        if not vol.valid(start):
            res.reason = "start cluster is outside the card"
            return res
        if vol.cluster(start)[4:8] not in TOP_BOXES:
            res.reason = "its first cluster no longer holds a video; probably overwritten"
            return res
        found = self._find_moov(start, size)
        if found is None:
            res.reason = "the video's index (moov) could not be found on the card"
            return res
        moov, _moov_off, moov_clusters, moov_k = found
        tracks = parse_moov(moov)
        if not tracks:
            res.reason = "the video's index is damaged"
            return res
        n = ceil_div(size, cl)
        # learn the usual first byte of tracks that have no fixed signature, from the trusted first cluster
        # (the first few clusters are read as one run; if that is wrong the bytes will not agree
        # with each other and nothing is learned)
        first = vol.read_run(start, min(size, 8 * cl))
        for t in tracks:
            if checkpoint_ok(t, b"\0" * 8, 100) is None:
                seen = [first[o] for o, s in t.samples[:64] if o < len(first) and s > 0]
                if len(seen) >= 4 and max(seen.count(b) for b in set(seen)) >= 0.9 * len(seen):
                    t.first_byte = max(set(seen), key=seen.count)
        cps: list[list[tuple[int, int, Track]]] = [[] for _ in range(n)]
        for t in tracks:
            for off, sz in t.samples:
                k, within = divmod(off, cl)
                if k < n and within <= cl - 8 and checkpoint_ok(t, b"\0\0\0\1\x65\0\0\0", max(sz, 5)) is not None:
                    cps[k].append((within, sz, t))
        m = vol.m

        def ok(k: int, c: int | None) -> list[bool]:
            if c is None or not vol.valid(c):
                return [False]
            base = vol.data_off + (c - 2) * cl
            return [bool(checkpoint_ok(t, m[base + w:base + w + 8], sz)) for w, sz, t in cps[k]]

        chain: list[int | None] = [None] * n
        used: set[int] = set()
        for i, c in enumerate(moov_clusters):
            if moov_k + i < n:
                chain[moov_k + i] = c
                used.add(c)
        chain[0] = start
        used.add(start)
        lastgood, k, cur = 0, 1, self._next(start, used)
        while k < n:
            if chain[k] is not None:
                cur = self._next(chain[k], used)
                k += 1
                continue
            r = ok(k, cur)
            if cur is None and not cps[k]:
                chain[k] = -1
                k += 1
                continue
            if all(r):
                chain[k] = cur
                used.add(cur)
                if r:
                    lastgood = k
                cur = self._next(cur, used)
                k += 1
                continue
            # checkpoints stop matching: find where cluster k really is
            w0, sz0, t0 = cps[k][0]
            cand = np.flatnonzero(self.avail)
            cand = cand[cand >= 2]
            offs = vol.data_off + (cand - 2) * cl + w0
            if t0.codec in ("avc1", "avc3", "hvc1", "hev1"):
                cand = cand[self.img[offs] == (0 if sz0 < (1 << 24) else self.img[offs])]
            elif t0.first_byte is not None:
                cand = cand[self.img[offs] == t0.first_byte]
            scored = []
            for c in cand:
                c = int(c)
                if c in used or not all(ok(k, c)):
                    continue
                run, j, cc, u = len(cps[k]), k + 1, c, set(used) | {c}
                while j < n and j < k + 200 and chain[j] is None:
                    cc = self._next(cc, u)
                    rr = ok(j, cc)
                    if not all(rr):
                        break
                    u.add(cc)
                    run += len(rr)
                    j += 1
                scored.append((run, c))
            scored.sort(reverse=True)
            if not scored or (len(scored) > 1 and scored[0][0] == scored[1][0]):
                chain[k] = -1                       # not on the card, or no way to choose: leave a gap
                res.missing += 1
                k += 1
                continue
            c = scored[0][1]
            # clusters between the last proven one and k carry no checkpoint. The new piece
            # starts where free space starts before c; walk back as far as that allows.
            back, b, cc = [], k, c
            while b - 1 > lastgood:
                p = self._prev(cc, used | set(back))
                if p is None:
                    break
                back.append(p)
                cc = p
                b -= 1
            if back and b - 1 == lastgood and (self._prev(cc, used | set(back)) is not None):
                # free space reaches back further than the gap: the exact join cannot be placed
                res.uncertain_joins += 1
                back = []
            for j, p in zip(range(k - 1, k - 1 - len(back), -1), back):
                if chain[j] is not None and chain[j] >= 0:
                    used.discard(chain[j])
                chain[j] = p
                used.add(p)
            chain[k] = c
            used.add(c)
            lastgood = k
            cur = self._next(c, used)
            k += 1
        for k in range(n):
            if chain[k] is None or chain[k] < 0:
                continue
            r = ok(k, chain[k])
            res.checked += len(r)
            res.passed += sum(r)
        res.chain = [(-1 if c is None else c) for c in chain]
        res.missing = sum(1 for c in res.chain if c < 0)
        if res.missing == 0 and res.passed == res.checked and res.checked > 0:
            res.status = "complete"
        elif res.checked == 0:
            res.status, res.reason = "failed", "this kind of video has no checkpoints the tool knows how to test"
        else:
            res.status = "partial"
            res.reason = f"{res.missing} of {n} pieces are not on the card" if res.missing else \
                "some parts did not pass their checks"
        return res

    def read(self, res: VideoResult, size: int) -> bytes:
        cl = self.vol.cl
        return b"".join(bytes(cl) if c < 0 else bytes(self.vol.cluster(c)) for c in res.chain)[:size]
