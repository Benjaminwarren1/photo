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

from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from . import jpeg
from .jpeg import ST_DONE, ST_ERROR, ST_NEED_DATA

CLEAR_WIN = 1.6          # a winner must beat the runner-up by this factor
CLEAR_WIN_NO_PREVIEW = 2.0   # stricter when only the join itself can be judged
MIN_MCUS = 12            # fewer new MCUs than this cannot be judged
CEILING = 2.4            # times the photo's own limit
BRANCHES = 24            # contenders followed to the end at an uncertain join
BRANCHES_NO_PREVIEW = 96
MAX_SEARCHES = 200       # whole-card searches allowed for one photo
MAX_HEADER = 4 * 1024 * 1024


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
        w8, h8 = -(-info.width // 8), -(-info.height // 8)
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
        return float(np.abs(mean_new - above).mean())


def limit(scores: list[float], floor: float) -> float:
    """Each photo is judged against its own first good chunks, never recent ones,
    so one bad accept cannot loosen the limit."""
    base = float(np.median(scores[:12])) if scores else floor / 2.5
    return max(floor, 2.5 * base)


@dataclass
class Result:
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
    trailer_clusters: int = 0
    exact_end: bool = True        # the end marker sits exactly where the recorded size says
    image_bytes: int = 0          # length up to and including the end marker
    scores: list[float] = field(default_factory=list)


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
    def __init__(self, vol, avail: np.ndarray):
        """avail[c] is True for clusters that may still belong to an unrecovered file."""
        self.vol = vol
        self.avail = avail
        self.always_ask = False      # leave every join that is not simply "the next free cluster" to a person

    def _next_avail(self, c: int, used: set[int]) -> int | None:
        c += 1
        top = self.vol.max_cluster
        while c <= top:
            if self.avail[c] and c not in used:
                return c
            c += 1
        return None

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
        res = Result(chain[0], size, chain=list(chain), status="complete", percent=100.0, info=info)
        res.has_preview = jpeg.decode_thumb(info) is not None
        res.image_bytes = size
        return res

    def rebuild(self, start: int, size: int | None, contiguous_only: bool = False,
                forced: dict[int, int] | None = None) -> Result:
        """forced maps a position in the chain to the cluster a person chose for it."""
        vol, cl = self.vol, self.vol.cl
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
        n_total = None if size is None else max(1, -(-size // cl))
        judge = Judge(info, jpeg.decode_thumb(info))
        has_preview = res.has_preview = judge.target is not None
        end_off = None if size is None else (size - 2) % cl
        margin = CLEAR_WIN if has_preview else CLEAR_WIN_NO_PREVIEW

        def assess(w: _Work, dcv, m0):
            return judge.preview_score(dcv, m0), judge.seam_score(w.stream.dc, dcv, m0)

        def lim_of(w: _Work) -> float:
            return limit(w.scores, 8.0) if has_preview else limit(w.seams, 6.0)

        def commit(w: _Work, c: int | None, data: bytes):
            m0 = w.stream.mcus
            _, n = w.stream.feed(data)
            ps, ss = assess(w, w.stream.dc[m0:m0 + n], m0)
            if ps is not None:
                w.scores.append(ps)
            if ss is not None:
                w.seams.append(ss)
            if c is not None:
                w.chain.append(c)
                w.used.add(c)

        def finished(w: _Work) -> bool:
            """Exact structural proof: all MCUs decoded, end marker where it must be."""
            if w.stream.status != ST_DONE:
                return False
            end = w.stream.end_offset()
            return end is not None and (size is None or info.data_off + end == size)

        def run_on(w: _Work) -> bool:
            """Follow the next free clusters to the end of the photo, checking structure
            only. True if the photo then finishes exactly."""
            while w.stream.status == ST_NEED_DATA:
                if n_total is not None and len(w.chain) >= n_total:
                    return False
                nx = self._next_avail(w.chain[-1], w.used)
                if nx is None:
                    return False
                commit(w, nx, vol.cluster(nx))
            return finished(w)

        def advance(w: _Work) -> None:
            searches = 0
            while w.stream.status == ST_NEED_DATA:
                if n_total is not None and len(w.chain) >= n_total:
                    w.status, w.reason = "review", "picture needs more data than the recorded size allows"
                    return
                last = n_total is not None and len(w.chain) == n_total - 1
                m0 = w.stream.mcus
                if forced and len(w.chain) in forced:
                    c = forced[len(w.chain)]
                    w.picks.append(dict(index=len(w.chain), cluster=c, how="chosen by a person"))
                    commit(w, c, vol.cluster(c))
                    continue
                nx = self._next_avail(w.chain[-1], w.used)
                nx_ok = False
                if nx is not None:
                    status, n, dcv, err = w.stream.probe(vol.cluster(nx))
                    nx_ok = status != ST_ERROR and not (last and status != ST_DONE)
                    if nx_ok:
                        ps, ss = assess(w, dcv, m0)
                        sc = ps if has_preview else ss
                        if sc is None or sc <= lim_of(w):
                            commit(w, nx, vol.cluster(nx))
                            continue
                if contiguous_only:
                    w.status, w.reason = "partial", "not in one piece"
                    return
                searches += 1
                if searches > MAX_SEARCHES:
                    w.status, w.reason = "review", "too many uncertain joins"
                    return
                # The next free cluster does not clearly continue the photo: look everywhere.
                cand = np.flatnonzero(self.avail)
                cand = cand[cand >= 2]
                cand = cand[~np.isin(cand, np.fromiter(w.used, np.int64, len(w.used)))]
                if last:
                    # the end marker has to sit exactly where the recorded size puts it
                    cand = np.array([c for c in cand if vol.cluster(int(c))[end_off:end_off + 2] == b"\xff\xd9"],
                                    np.int64)
                found: list[tuple[float, int]] = []
                for c, status, dcv in w.stream.probe_many(vol.m, vol.data_off, cl, cand, last):
                    ps, ss = assess(w, dcv, m0)
                    sc = ps if has_preview else ss
                    found.append((0.0 if sc is None else sc, int(c)))
                found.sort()
                ceiling = CEILING * lim_of(w)       # beyond this even the best is not credible
                plausible = [f for f in found if f[0] <= ceiling]
                # 1. Proof by completion. Let each contender run on through the free clusters
                #    that follow it: only a right choice can end with every block decoded and
                #    the end marker exactly in place. The next free cluster always gets a try.
                if self.always_ask and not (forced and len(w.chain) in forced):
                    w.candidates = [(round(sc, 2), c) for sc, c in plausible[:8]]
                    w.status, w.reason = ("review", "waiting for a person to choose the next piece") if plausible \
                        else ("partial", "the rest of this photo is not on the card")
                    return
                tries = plausible[:BRANCHES if has_preview else BRANCHES_NO_PREVIEW]
                if nx_ok and all(c != nx for _, c in tries):
                    tries = [next((f for f in found if f[1] == nx), (ceiling, nx))] + tries
                done = []
                for sc, c in tries:
                    b = w.fork()
                    commit(b, c, vol.cluster(c))
                    if run_on(b):
                        dcv = b.stream.dc[m0:b.stream.mcus]
                        ps, ss = assess(w, dcv, m0)
                        whole = ps if has_preview else ss
                        done.append((sc if whole is None else whole, c, b))
                done.sort(key=lambda d: d[0])
                if done and done[0][0] <= ceiling and (
                        len(done) == 1 or done[1][0] >= CLEAR_WIN * max(done[0][0], 0.25)):
                    sc, c, b = done[0]
                    if c != nx:
                        b.picks.insert(len(w.picks), dict(index=len(w.chain), cluster=c, score=round(sc, 2),
                                                          how="only this choice completes the photo"))
                    w.__dict__.update(b.__dict__)
                    return
                # 2. A photo in three or more pieces: take a clear best match and carry on.
                pick = None
                if last and len(found) == 1:
                    pick = found[0]
                elif plausible and (len(found) == 1 or found[1][0] >= margin * max(found[0][0], 0.25)):
                    pick = found[0]      # a wrong pick here cannot survive the exact finish required later
                if pick is not None:
                    if pick[1] != nx:
                        w.picks.append(dict(index=len(w.chain), cluster=pick[1], score=round(pick[0], 2),
                                            runner_up=round(found[1][0], 2) if len(found) > 1 else None,
                                            how="clear best match"))
                    commit(w, pick[1], vol.cluster(pick[1]))
                    continue
                w.candidates = [(round(sc, 2), c) for sc, c in plausible[:8]]
                if not plausible:
                    w.status, w.reason = "partial", "the rest of this photo is not on the card"
                else:
                    w.status, w.reason = "review", "more than one piece of the card could come next"
                return

        w = _Work(jpeg.Stream(info), chain, used, [], [], [])
        commit(w, None, buf[info.data_off:])
        if w.stream.status == ST_ERROR:
            res.reason = "picture data is damaged in the first cluster"
            return res
        advance(w)
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
                    res.trailer_clusters += 1
                res.status = "complete" if len(chain) == n_total else "partial"
                res.exact_end = False
                res.reason = "extra data after the picture was copied without checking"
            else:
                res.status, res.reason = "review", "picture ends after the recorded size"
        return res
