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
                forced: dict[int, int] | None = None, wide: bool = True) -> Result:
        """forced maps a position in the chain to the cluster a person chose for it.
        wide=False looks for each next piece only near the last one, which is where it is
        almost every time, and leaves the rest of the card to a later, wider attempt."""
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

        def commit(w: _Work, c: int | None, data: bytes, scored=None):
            m0 = w.stream.mcus
            _, n = w.stream.feed(data)
            ps, ss = scored if scored is not None else assess(w, w.stream.dc[m0:m0 + n], m0)
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
                            commit(w, nx, vol.cluster(nx), (ps, ss))
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
                # Pieces of a file usually lie near each other, so look nearby first. A nearby piece
                # is only taken if it goes on to finish the photo exactly, which is proof wherever it
                # was found; otherwise the search widens to the rest of the card.
                pool, capped = cand, False
                if last or self.always_ask or len(pool) <= NEAR_BACK + NEAR_FWD:
                    stages = [pool]
                else:
                    i = int(np.searchsorted(pool, w.chain[-1]))
                    span = pool
                    if len(pool) > SEARCH_SPAN:
                        span, capped = pool[max(0, i - SEARCH_SPAN // 4):i + 3 * SEARCH_SPAN // 4], True
                    stages = [pool[max(0, i - NEAR_BACK):i + NEAR_FWD]]
                    if wide:
                        stages.append(span)
                    else:
                        capped = True
                for cand in stages:
                    if last:
                        # the end marker has to sit exactly where the recorded size puts it
                        if end_off + 2 <= cl:
                            at = vol.data_off + (cand.astype(np.int64) - 2) * cl + end_off
                            raw = np.frombuffer(vol.m, np.uint8)
                            cand = cand[(raw[at] == 0xFF) & (raw[at + 1] == 0xD9)]
                        else:
                            cand = cand[:0]
                    elif len(cand) > 2 * SCREEN_KEEP:
                        # A whole card is far too much to decode in full for every join. Look at the
                        # first few blocks of each candidate, and carry on with the best-looking only.
                        # First a glance at every candidate, then a longer look at the best of those:
                        # a glance alone sometimes ranks the true piece too low to get its full check.
                        full, dcs, short = w.stream.screen_many(vol.m, vol.data_off, cl, cand, SCREEN_MCUS)
                        sc = judge.screen_scores(w.stream.dc, dcs, m0)
                        if sc is not None:
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
                                ok = (np.arange(len(order)) < SCREEN_SURE) | (sc[order] <= 2 * CEILING * lim_of(w))
                                full = full[order[ok]]
                            cand = np.unique(np.concatenate([full] + shorts))
                    found: list[tuple[float, int]] = []
                    for c, status, dcv in w.stream.probe_many(vol.m, vol.data_off, cl, cand, last):
                        ps, ss = assess(w, dcv, m0)
                        sc = ps if has_preview else ss
                        found.append((0.0 if sc is None else sc, int(c)))
                    found.sort()
                    # Two places on the card can hold the very same bytes (a photo saved twice).
                    # They are one choice, not two rivals: keep the first of each.
                    seen_bytes: set[bytes] = set()
                    uniq = []
                    for f in found:
                        piece = vol.cluster(f[1])
                        if last:                  # what lies after the end of the photo is not part of it
                            piece = piece[:end_off + 2]
                        key = hashlib.blake2b(piece, digest_size=12).digest()
                        if key not in seen_bytes:
                            seen_bytes.add(key)
                            uniq.append(f)
                    every, found = found, uniq        # all copies still get a try at finishing the photo
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
                    tries = [f for f in every if f[0] <= ceiling][:BRANCHES if has_preview else BRANCHES_NO_PREVIEW]
                    if nx_ok and all(c != nx for _, c in tries):
                        tries = [next((f for f in every if f[1] == nx), (ceiling, nx))] + tries
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
                    # ... and two ways of finishing that produce identical bytes are the same photo
                    seen_bytes, uniq = set(), []
                    for d in done:
                        h = hashlib.blake2b(digest_size=12)
                        end = d[2].stream.end_offset()
                        left = (size if size is not None else info.data_off + end) - len(w.chain) * cl
                        for c in d[2].chain[len(w.chain):]:
                            h.update(vol.cluster(c)[:max(0, left)])     # only up to the end of the photo
                            left -= cl
                        if h.digest() not in seen_bytes:
                            seen_bytes.add(h.digest())
                            uniq.append(d)
                    done = uniq
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
                    w.status, w.reason = "partial", ("the rest of this photo was not found anywhere near it"
                                                     if capped else "the rest of this photo is not on the card")
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
