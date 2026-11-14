"""Rebuild one photo cluster by cluster.

Starting from the cluster a directory entry (or a carved header) points at, the
photo is followed through the card. At every step a candidate cluster has to
pass two tests:

* structure - the resumable decoder must run through it without the data
  ceasing to be valid JPEG (this alone rules out everything that is not photo
  data);
* picture - the part of the picture it adds must match the photo's own embedded
  preview (or, with no preview, join smoothly to the rows above it).

A photo is only called complete when the decoder reaches the exact number of
blocks the header promises and finds the end marker where the size says it is.
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from . import jpeg
from .jpeg import ST_DONE, ST_ERROR, ST_NEED_DATA
from .util import ceil_div, next_free

CLEAR_WIN = 1.6          # a winner must beat the runner-up by this factor
CLEAR_WIN_NO_PREVIEW = 2.0   # stricter when only the join itself can be judged
MIN_MCUS = 12            # fewer new MCUs than this cannot be judged
CEILING = 2.4            # times the photo's own limit
BRANCHES = 24            # contenders followed to the end at an uncertain join
BRANCHES_NO_PREVIEW = 96
MAX_SEARCHES = 200       # whole-card searches allowed for one photo
MAX_HEADER = 4 * 1024 * 1024
FLAT = 0.75              # luma spread (0-255 scale) below which decoded data is one flat colour
NOT_A_PHOTO = 1e6        # the score given to such data
NEAR_FWD, NEAR_BACK = 2048, 64    # free clusters after and before the last piece that are searched first.
# On a real, much-used card every one of 1,025 joins went forward, 99.8% of them within 2,048 free clusters.
SEARCH_SPAN = 1 << 18    # on very large cards, the most free clusters one search will ever look through
SCREEN_WIDE = 1500       # how many get the longer second look
SCREEN_MCUS_2 = 192      # ... and how many MCUs that look covers (about a row of the picture)
SCREEN_MCUS = 32         # a whole-card search first looks at this many MCUs of every candidate
SCREEN_KEEP = 300        # ... and only the best-looking go on to be decoded in full
SCREEN_SURE = 100        # this many always do, however poor they look


class Judge:
    """Scores newly decoded MCUs against the embedded preview (colour + structure),
    or against the rows above them when there is no preview."""

    def __init__(self, info: jpeg.JpegInfo, thumb: np.ndarray | None):
        self.info = info
        self.X = info.mcus_x
        self.q = np.concatenate([np.full(info.comps[ci][1] * info.comps[ci][2], info.qdc[ci] / 8.0, np.float32)
                                 for ci, _, _ in info.scan])
        self.ny = info.comps[info.scan[0][0]][1] * info.comps[info.scan[0][0]][2]
        self.mono = len(info.scan) == 1
        self.target = None
        if thumb is not None:
            self.target = self._layout(thumb)

    def _layout(self, thumb: np.ndarray) -> np.ndarray:
        info = self.info
        w8, h8 = ceil_div(info.width, 8), ceil_div(info.height, 8)
        im = Image.fromarray(np.clip(thumb, 0, 255).astype(np.uint8), "YCbCr").resize((w8, h8), Image.BILINEAR)
        a = np.asarray(im, np.float32)
        Hd, Wd = info.mcus_y * info.vmax, info.mcus_x * info.hmax
        a = np.pad(a, ((0, Hd - h8), (0, Wd - w8), (0, 0)), mode="edge")
        cols = []
        for k, (ci, _, _) in enumerate(info.scan):
            _, h, v, _ = info.comps[ci]
            p = a[..., min(k, 2)]
            fy, fx = info.vmax // v, info.hmax // h
            if fy > 1 or fx > 1:
                p = p.reshape(info.mcus_y * v, fy, info.mcus_x * h, fx).mean(axis=(1, 3))
            cols.append(p.reshape(info.mcus_y, v, info.mcus_x, h).transpose(0, 2, 1, 3).reshape(info.total_mcus, v * h))
        return np.concatenate(cols, axis=1)

    def values(self, dc: np.ndarray) -> np.ndarray:
        return dc.astype(np.float32) * self.q + 128.0

    def preview_score(self, dc: np.ndarray, m0: int) -> float | None:
        n = len(dc)
        if self.target is None or n < MIN_MCUS:
            return None
        x = self.values(dc)
        y = self.target[m0:m0 + n]
        xl, yl = x[:, :self.ny].ravel(), y[:, :self.ny].ravel()
        if self.mono:
            cm = float(np.abs(xl - yl).mean())
        else:
            cm = float(np.abs(x[:, self.ny:] - y[:, self.ny:]).mean())
        if xl.std() < FLAT and yl.std() > 4 * FLAT:
            return NOT_A_PHOTO          # data that decodes to one flat colour where the picture has detail
        pen = 0.0
        if yl.std() > 6:
            a, b = xl - xl.mean(), yl - yl.mean()
            cc = float((a * b).sum() / np.sqrt((a * a).sum() * (b * b).sum() + 1e-6))
            pen = 8.0 * max(0.0, 0.85 - cc)
        return cm + pen

    def seam_score(self, prev: np.ndarray, dc: np.ndarray, m0: int) -> float | None:
        """Mean jump between each new MCU and the MCU directly above it."""
        n = len(dc)
        X = self.X
        if n < MIN_MCUS or m0 < X:
            return None
        new = self.values(dc)
        mean_new = np.concatenate([new[:, :self.ny].mean(axis=1, keepdims=True), new[:, self.ny:]], axis=1)
        idx = np.arange(m0, m0 + n) - X
        above = np.empty_like(mean_new)
        from_prev = idx < m0
        pv = self.values(prev[idx[from_prev]])
        above[from_prev] = np.concatenate([pv[:, :self.ny].mean(axis=1, keepdims=True), pv[:, self.ny:]], axis=1)
        if (~from_prev).any():
            above[~from_prev] = mean_new[idx[~from_prev] - m0]
        if n >= 2 * MIN_MCUS and mean_new[:, 0].std() < FLAT and above[:, 0].std() > 4 * FLAT:
            return NOT_A_PHOTO
        return float(np.abs(mean_new - above).mean())


    def screen_scores(self, prev: np.ndarray, dc: np.ndarray, m0: int) -> np.ndarray | None:
        """The same judgement as preview_score (or seam_score when there is no preview),
        for many candidates at once: dc is [candidates, n, blocks]. None if it cannot be judged."""
        k, n, _ = dc.shape
        if n < MIN_MCUS or not k:
            return None
        x = dc.astype(np.float32) * self.q + 128.0
        ny = self.ny
        if self.target is not None:
            y = self.target[m0:m0 + n]
            if len(y) < n:
                return None
            xl, yl = x[:, :, :ny].reshape(k, -1), y[:, :ny].ravel()
            if self.mono:
                cm = np.abs(xl - yl).mean(axis=1)
            else:
                cm = np.abs(x[:, :, ny:] - y[None, :, ny:]).reshape(k, -1).mean(axis=1)
            pen = np.zeros(k, np.float32)
            if yl.std() > 6:
                a, b = xl - xl.mean(axis=1, keepdims=True), yl - yl.mean()
                cc = (a * b).sum(axis=1) / np.sqrt((a * a).sum(axis=1) * (b * b).sum() + 1e-6)
                pen = 8.0 * np.maximum(0.0, 0.85 - cc)
            sc = cm + pen
            if yl.std() > 4 * FLAT:
                sc[xl.std(axis=1) < FLAT] = NOT_A_PHOTO
            return sc
        X = self.X
        if m0 < X:
            return None
        pv = self.values(prev[m0 - X:m0 - X + min(n, X)])
        above = np.concatenate([pv[:, :ny].mean(axis=1, keepdims=True), pv[:, ny:]], axis=1)
        new = np.concatenate([x[:, :, :ny].mean(axis=2, keepdims=True), x[:, :, ny:]], axis=2)
        jump = np.abs(new[:, :X] - above[None]).reshape(k, -1).sum(axis=1)
        if n > X:           # past one row, each new MCU sits under another new one
            jump += np.abs(new[:, X:] - new[:, :n - X]).reshape(k, -1).sum(axis=1)
        sc = jump / (n * new.shape[2])
        if n >= 2 * MIN_MCUS and above[:, 0].std() > 4 * FLAT:
            sc[new[:, :, 0].std(axis=1) < FLAT] = NOT_A_PHOTO
        return sc


def limit(scores: list[float], floor: float) -> float:
    """Each photo is judged against its own first good chunks, never recent ones,
    so one bad accept cannot loosen the limit."""
    base = float(np.median(scores[:12])) if scores else floor / 2.5
    return max(floor, 2.5 * base)


@dataclass
class Result:
    """How following one photo turned out."""
    start: int
    size: int | None
    chain: list[int] = field(default_factory=list)
    status: str = "failed"        # complete | partial | review | unsupported | failed
    reason: str = ""
    percent: float = 0.0
    has_preview: bool = False
    picks: list[dict] = field(default_factory=list)       # non-contiguous decisions made
    candidates: list[tuple[float, int]] = field(default_factory=list)
    info: jpeg.JpegInfo | None = None
    exact_end: bool = True        # the end marker sits exactly where the recorded size says
    image_bytes: int = 0          # length up to and including the end marker
    scores: list[float] = field(default_factory=list)
    # The clusters this answer relied on being free: the ones it took into the chain or tried
    # as the next piece. None when it searched the whole card. Free clusters only ever get
    # taken, never given back, so while all of these are still free the answer still holds.
    looked: list[int] | None = field(default_factory=list, repr=False, compare=False)


class _Work:
    """Everything that changes while one photo is being followed, so a close call
    can be explored down several branches."""

    def __init__(self, stream, chain, used, scores, seams, picks):
        self.stream, self.chain, self.used = stream, chain, used
        self.scores, self.seams, self.picks = scores, seams, picks
        self.status, self.reason, self.candidates = "", "", []

    def fork(self) -> "_Work":
        return _Work(self.stream.copy(), list(self.chain), set(self.used),
                     list(self.scores), list(self.seams), list(self.picks))


class Rebuilder:
    """Follows photos through one card, cluster by cluster (see the top of this file)."""

    def __init__(self, vol, avail: np.ndarray):
        """avail[c] is True for clusters that may still belong to an unrecovered file."""
        self.vol = vol
        self.avail = avail
        self.always_ask = False      # leave every join that is not simply "the next free cluster" to a person
        # What the rebuild running on this thread has relied on being free (see Result.looked).
        # Kept per thread because rebuilds run ahead on other cores while the main thread
        # takes clusters for the photos it has accepted.
        self._seen = threading.local()

    def _next_avail(self, c: int, used: set[int]) -> int | None:
        """The next cluster after c that may still hold part of an unrecovered file."""
        nx = next_free(self.avail, c, used, self.vol.max_cluster)
        seen = getattr(self._seen, "looked", None)
        if seen is not None and nx is not None:
            seen.append(nx)
        return nx

    def prove_chain(self, chain: list[int], size: int) -> Result | None:
        """Check a chain suggested by the file system (exFAT keeps a deleted file's
        chain). Returns a complete Result only if the photo decodes exactly along it."""
        vol = self.vol
        if not chain or not all(vol.valid(c) and self.avail[c] for c in chain):
            return None
        data = vol.read_chain(chain, size)
        try:
            info = jpeg.parse_header(data)
        except (jpeg.NotJpeg, jpeg.HeaderIncomplete):
            return None
        if not info.supported:
            return None
        st = jpeg.Stream(info)
        st.feed(data[info.data_off:])
        if st.status != ST_DONE or st.end_offset() is None or info.data_off + st.end_offset() != size:
            return None
        res = Result(chain[0], size, chain=list(chain), status="complete", percent=100.0, info=info,
                     looked=list(chain))
        res.has_preview = jpeg.decode_thumb(info) is not None
        res.image_bytes = size
        return res

    def rebuild(self, start: int, size: int | None, contiguous_only: bool = False,
                forced: dict[int, int] | None = None, wide: bool = True) -> Result:
        """Follow the photo that starts at cluster start through the card. size is its length as
        the card recorded it, or None for a photo found by scanning (it then ends where its end
        marker is). contiguous_only only looks at the clusters that follow straight on.
        forced maps a position in the chain to the cluster a person chose for it.
        wide=False looks for each next piece only near the last one, which is where it is
        almost every time, and leaves the rest of the card to a later, wider attempt."""
        self._seen.looked, self._seen.everywhere = [], False
        try:
            res = self._rebuild(start, size, contiguous_only, forced, wide)
        finally:
            looked, everywhere = self._seen.looked, self._seen.everywhere
            self._seen.looked = None
        res.looked = None if everywhere else looked
        return res

    def _rebuild(self, start: int, size: int | None, contiguous_only: bool,
                 forced: dict[int, int] | None, wide: bool) -> Result:
        """rebuild, without the record of what it looked at."""
        vol = self.vol
        res = Result(start, size)
        if not vol.valid(start):
            res.reason = "start cluster is outside the card"
            return res
        chain, used = [start], {start}
        buf = bytes(vol.cluster(start))
        while True:
            try:
                info = jpeg.parse_header(buf)
                break
            except jpeg.NotJpeg as e:
                res.reason = f"not a JPEG at its start cluster ({e}); probably overwritten"
                return res
            except jpeg.HeaderIncomplete:
                nx = self._next_avail(chain[-1], used)
                if nx is None or len(buf) > MAX_HEADER:
                    res.reason = "header does not end"
                    return res
                chain.append(nx)
                used.add(nx)
                buf += bytes(vol.cluster(nx))
        res.info, res.chain = info, chain
        if not info.supported:
            res.status, res.reason = "unsupported", info.why_unsupported
            return res
        f = _Follow(self, info, size, contiguous_only, forced, wide)
        res.has_preview = f.has_preview
        w = _Work(jpeg.Stream(info), chain, used, [], [], [])
        f.commit(w, None, buf[info.data_off:])
        if w.stream.status == ST_ERROR:
            res.reason = "picture data is damaged in the first cluster"
            return res
        f.advance(w)
        return self._result(res, w, f)

    def _result(self, res: Result, w: _Work, f: "_Follow") -> Result:
        """How far following the photo got, as a Result."""
        info, size, n_total = f.info, f.size, f.n_total
        stream, chain = w.stream, w.chain
        res.chain, res.picks, res.candidates, res.scores = chain, w.picks, w.candidates, w.scores
        res.status, res.reason = w.status or "review", w.reason
        res.percent = round(100.0 * stream.mcus / max(1, info.total_mcus), 1)
        if stream.status == ST_ERROR:
            res.status, res.reason = "review", "picture data stopped making sense"
        if stream.status == ST_DONE:
            end = stream.end_offset()
            if end is None:
                res.status, res.reason = "review", "picture is complete but its end marker is missing"
                return res
            res.image_bytes = info.data_off + end
            if size is None:
                res.size = res.image_bytes
                res.status, res.reason = "complete", ""
            elif res.image_bytes == size:
                res.status, res.reason = "complete", ""
            elif res.image_bytes < size:
                # data after the end marker (a second picture, a motion clip): take it as it lies
                while len(chain) < n_total:
                    nx = self._next_avail(chain[-1], w.used)
                    if nx is None:
                        break
                    chain.append(nx)
                    w.used.add(nx)
                res.status = "complete" if len(chain) == n_total else "partial"
                res.exact_end = False
                res.reason = "extra data after the picture was copied without checking"
            else:
                res.status, res.reason = "review", "picture ends after the recorded size"
        return res


class _Follow:
    """Following one photo through the card: what stays fixed while it is followed (its header,
    its preview, its recorded size) and each step of the way. The changing part, which can be
    forked to try several possible next pieces, is a _Work."""

    def __init__(self, rb: Rebuilder, info: jpeg.JpegInfo, size: int | None, contiguous_only: bool,
                 forced: dict[int, int] | None, wide: bool):
        self.rb, self.vol, self.cl = rb, rb.vol, rb.vol.cl
        self.info, self.size = info, size
        self.contiguous_only, self.forced, self.wide = contiguous_only, forced, wide
        self.n_total = None if size is None else max(1, ceil_div(size, self.cl))    # clusters the size allows
        self.judge = Judge(info, jpeg.decode_thumb(info))
        self.has_preview = self.judge.target is not None
        self.end_off = None if size is None else (size - 2) % self.cl     # where the end marker sits in the last cluster
        self.margin = CLEAR_WIN if self.has_preview else CLEAR_WIN_NO_PREVIEW

    # ---- judging and taking one piece ---------------------------------------
    def assess(self, w: _Work, dcv, m0: int):
        """(match with the preview, smoothness of the join) for MCUs dcv decoded from position m0."""
        return self.judge.preview_score(dcv, m0), self.judge.seam_score(w.stream.dc, dcv, m0)

    def score(self, w: _Work, dcv, m0: int) -> float | None:
        """The one score that decides: the preview match when there is a preview, else the join."""
        ps, ss = self.assess(w, dcv, m0)
        return ps if self.has_preview else ss

    def lim_of(self, w: _Work) -> float:
        """The worst score this photo's own first good pieces allow."""
        return limit(w.scores, 8.0) if self.has_preview else limit(w.seams, 6.0)

    def commit(self, w: _Work, c: int | None, data: bytes, scored=None) -> None:
        """Take cluster c (with these bytes) as the next piece of the photo."""
        m0 = w.stream.mcus
        _, n = w.stream.feed(data)
        ps, ss = scored if scored is not None else self.assess(w, w.stream.dc[m0:m0 + n], m0)
        if ps is not None:
            w.scores.append(ps)
        if ss is not None:
            w.seams.append(ss)
        if c is not None:
            w.chain.append(c)
            w.used.add(c)

    def finished(self, w: _Work) -> bool:
        """Exact structural proof: all MCUs decoded, end marker where it must be."""
        if w.stream.status != ST_DONE:
            return False
        end = w.stream.end_offset()
        return end is not None and (self.size is None or self.info.data_off + end == self.size)

    def run_on(self, w: _Work) -> bool:
        """Follow the next free clusters to the end of the photo, checking structure
        only. True if the photo then finishes exactly."""
        while w.stream.status == ST_NEED_DATA:
            if self.n_total is not None and len(w.chain) >= self.n_total:
                return False
            nx = self.rb._next_avail(w.chain[-1], w.used)
            if nx is None:
                return False
            self.commit(w, nx, self.vol.cluster(nx))
        return self.finished(w)

    def tail_fits(self, w: _Work, b: _Work) -> bool:
        """A finish that decodes cleanly to the last block is strong evidence, but not proof:
        another photo's data can do it. The picture it makes must also look like this photo,
        piece by piece, by the photo's own standard. An average over the whole tail is not
        enough, because a preview that is mostly dark forgives almost anything on average."""
        if not self.has_preview:
            # Without a preview there is only the join between rows to go on, and in a small
            # or busy picture that varies too much from piece to piece to judge each alone.
            return True
        new = b.scores[len(w.scores):]
        if len(new) < 2:
            return True
        bar = CEILING * self.lim_of(w)
        over = sum(1 for v in new if v > bar)
        return over <= max(1, len(new) // 10)

    # ---- following the photo to its end -------------------------------------
    def advance(self, w: _Work) -> None:
        """Follow the photo piece by piece until it is finished, or a join cannot be decided
        (w.status and w.reason then say why)."""
        vol, searches = self.vol, 0
        while w.stream.status == ST_NEED_DATA:
            if self.n_total is not None and len(w.chain) >= self.n_total:
                w.status, w.reason = "review", "picture needs more data than the recorded size allows"
                return
            last = self.n_total is not None and len(w.chain) == self.n_total - 1
            m0 = w.stream.mcus
            if self.forced and len(w.chain) in self.forced:
                c = self.forced[len(w.chain)]
                w.picks.append(dict(index=len(w.chain), cluster=c, how="chosen by a person"))
                self.commit(w, c, vol.cluster(c))
                continue
            # Almost always the next piece is simply the next free cluster.
            nx = self.rb._next_avail(w.chain[-1], w.used)
            nx_ok = False
            if nx is not None:
                status, n, dcv, err = w.stream.probe(vol.cluster(nx))
                nx_ok = status != ST_ERROR and not (last and status != ST_DONE)
                if nx_ok:
                    ps, ss = self.assess(w, dcv, m0)
                    sc = ps if self.has_preview else ss
                    if sc is None or sc <= self.lim_of(w):
                        self.commit(w, nx, vol.cluster(nx), (ps, ss))
                        continue
            if self.contiguous_only:
                w.status, w.reason = "partial", "not in one piece"
                return
            searches += 1
            if searches > MAX_SEARCHES:
                w.status, w.reason = "review", "too many uncertain joins"
                return
            # The next free cluster does not clearly continue the photo: look everywhere.
            if not self._search(w, nx, nx_ok, m0, last):
                return

    def _stages(self, w: _Work, last: bool) -> tuple[list[np.ndarray], bool]:
        """Where to look for the next piece, nearest first, and whether that leaves part of the
        card unsearched. Pieces of a file usually lie near each other, so look nearby first. A
        nearby piece is only taken if it goes on to finish the photo exactly, which is proof
        wherever it was found; otherwise the search widens to the rest of the card."""
        self.rb._seen.everywhere = True
        pool = np.flatnonzero(self.rb.avail)
        pool = pool[pool >= 2]
        pool = pool[~np.isin(pool, np.fromiter(w.used, np.int64, len(w.used)))]
        if last or self.rb.always_ask or len(pool) <= NEAR_BACK + NEAR_FWD:
            return [pool], False
        capped = False
        i = int(np.searchsorted(pool, w.chain[-1]))
        span = pool
        if len(pool) > SEARCH_SPAN:
            span, capped = pool[max(0, i - SEARCH_SPAN // 4):i + 3 * SEARCH_SPAN // 4], True
        stages = [pool[max(0, i - NEAR_BACK):i + NEAR_FWD]]
        if self.wide:
            stages.append(span)
        else:
            capped = True
        return stages, capped

    def _end_marker_fits(self, cand: np.ndarray) -> np.ndarray:
        """For the last piece: only clusters with the end marker exactly where the recorded size puts it."""
        cl, end_off = self.cl, self.end_off
        if end_off + 2 > cl:
            return cand[:0]
        at = self.vol.data_off + (cand.astype(np.int64) - 2) * cl + end_off
        raw = np.frombuffer(self.vol.m, np.uint8)
        return cand[(raw[at] == 0xFF) & (raw[at + 1] == 0xD9)]

    def _screen(self, w: _Work, cand: np.ndarray, m0: int) -> np.ndarray:
        """A whole card is far too much to decode in full for every join. Look at the first few
        blocks of each candidate, and carry on with the best-looking only. First a glance at
        every candidate, then a longer look at the best of those: a glance alone sometimes ranks
        the true piece too low to get its full check."""
        vol, cl, judge = self.vol, self.cl, self.judge
        full, dcs, short = w.stream.screen_many(vol.m, vol.data_off, cl, cand, SCREEN_MCUS)
        sc = judge.screen_scores(w.stream.dc, dcs, m0)
        if sc is None:
            return cand
        shorts = [short]
        if SCREEN_KEEP and len(full) > SCREEN_KEEP:
            first = np.sort(full[np.argsort(sc, kind="stable")[:SCREEN_WIDE]])
            full, dcs, short2 = w.stream.screen_many(vol.m, vol.data_off, cl, first, SCREEN_MCUS_2)
            shorts.append(short2)
            sc2 = judge.screen_scores(w.stream.dc, dcs, m0)
            if sc2 is None:       # too near the end of the picture to judge that far
                full, sc2 = first, None
            sc = sc2
        if sc is not None:
            order = np.argsort(sc, kind="stable")[:SCREEN_KEEP or len(sc)]
            # beyond the first hundred, only those that could still be credible
            ok = (np.arange(len(order)) < SCREEN_SURE) | (sc[order] <= 2 * CEILING * self.lim_of(w))
            full = full[order[ok]]
        return np.unique(np.concatenate([full] + shorts))

    def _ranked(self, w: _Work, cand: np.ndarray, m0: int, last: bool) -> list[tuple[float, int]]:
        """Every candidate the decoder accepts as the next piece, as (score, cluster), best first."""
        vol = self.vol
        found: list[tuple[float, int]] = []
        for c, status, dcv in w.stream.probe_many(vol.m, vol.data_off, self.cl, cand, last):
            sc = self.score(w, dcv, m0)
            found.append((0.0 if sc is None else sc, int(c)))
        found.sort()
        return found

    def _distinct(self, found: list[tuple[float, int]], last: bool) -> list[tuple[float, int]]:
        """Two places on the card can hold the very same bytes (a photo saved twice). They are
        one choice, not two rivals: keep the first of each."""
        seen_bytes: set[bytes] = set()
        uniq = []
        for f in found:
            piece = self.vol.cluster(f[1])
            if last:                  # what lies after the end of the photo is not part of it
                piece = piece[:self.end_off + 2]
            key = hashlib.blake2b(piece, digest_size=12).digest()
            if key not in seen_bytes:
                seen_bytes.add(key)
                uniq.append(f)
        return uniq

    def _completions(self, w: _Work, tries: list[tuple[float, int]], m0: int) -> list[tuple[float, int, _Work]]:
        """Proof by completion. Let each contender run on through the free clusters that follow
        it: only a right choice can end with every block decoded and the end marker exactly in
        place. Returns those that do, as (score, cluster, the finished work), best first, with
        two ways of finishing that produce identical bytes counted as one."""
        vol, cl = self.vol, self.cl
        done = []
        for sc, c in tries:
            b = w.fork()
            self.commit(b, c, vol.cluster(c))
            if self.run_on(b) and self.tail_fits(w, b):
                whole = self.score(w, b.stream.dc[m0:b.stream.mcus], m0)
                done.append((sc if whole is None else whole, c, b))
        done.sort(key=lambda d: d[0])
        seen_bytes, uniq = set(), []
        for d in done:
            h = hashlib.blake2b(digest_size=12)
            end = d[2].stream.end_offset()
            left = (self.size if self.size is not None else self.info.data_off + end) - len(w.chain) * cl
            for c in d[2].chain[len(w.chain):]:
                h.update(vol.cluster(c)[:max(0, left)])     # only up to the end of the photo
                left -= cl
            if h.digest() not in seen_bytes:
                seen_bytes.add(h.digest())
                uniq.append(d)
        return uniq

    def _search(self, w: _Work, nx: int | None, nx_ok: bool, m0: int, last: bool) -> bool:
        """Look for the next piece away from the next free cluster. True when one was taken and
        the photo can be followed on; False when following ends here (finished, or w.status says
        why not)."""
        stages, capped = self._stages(w, last)
        for cand in stages:
            if last:
                # the end marker has to sit exactly where the recorded size puts it
                cand = self._end_marker_fits(cand)
            elif len(cand) > 2 * SCREEN_KEEP:
                cand = self._screen(w, cand, m0)
            every = self._ranked(w, cand, m0, last)
            found = self._distinct(every, last)    # all copies still get a try at finishing the photo
            ceiling = CEILING * self.lim_of(w)       # beyond this even the best is not credible
            plausible = [f for f in found if f[0] <= ceiling]
            if self.rb.always_ask and not (self.forced and len(w.chain) in self.forced):
                w.candidates = [(round(sc, 2), c) for sc, c in plausible[:8]]
                w.status, w.reason = ("review", "waiting for a person to choose the next piece") if plausible \
                    else ("partial", "the rest of this photo is not on the card")
                return False
            # 1. Proof by completion. The next free cluster always gets a try.
            tries = [f for f in every if f[0] <= ceiling][:BRANCHES if self.has_preview else BRANCHES_NO_PREVIEW]
            if nx_ok and all(c != nx for _, c in tries):
                tries = [next((f for f in every if f[1] == nx), (ceiling, nx))] + tries
            done = self._completions(w, tries, m0)
            if done and done[0][0] <= ceiling and (
                    len(done) == 1 or done[1][0] >= CLEAR_WIN * max(done[0][0], 0.25)):
                sc, c, b = done[0]
                if c != nx:
                    b.picks.insert(len(w.picks), dict(index=len(w.chain), cluster=c, score=round(sc, 2),
                                                      how="only this choice completes the photo"))
                w.__dict__.update(b.__dict__)
                return False
        # 2. A photo in three or more pieces: take a clear best match and carry on.
        pick = None
        if last and len(found) == 1:
            pick = found[0]
        elif plausible and (len(found) == 1 or found[1][0] >= self.margin * max(found[0][0], 0.25)):
            pick = found[0]      # a wrong pick here cannot survive the exact finish required later
        if pick is not None:
            if pick[1] != nx:
                w.picks.append(dict(index=len(w.chain), cluster=pick[1], score=round(pick[0], 2),
                                    runner_up=round(found[1][0], 2) if len(found) > 1 else None,
                                    how="clear best match"))
            self.commit(w, pick[1], self.vol.cluster(pick[1]))
            return True
        w.candidates = [(round(sc, 2), c) for sc, c in plausible[:8]]
        if not plausible:
            w.status, w.reason = "partial", ("the rest of this photo was not found anywhere near it"
                                             if capped else "the rest of this photo is not on the card")
        else:
            w.status, w.reason = "review", "more than one piece of the card could come next"
        return False
