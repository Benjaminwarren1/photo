"""Close calls decided by a person, or by a program driving the tool (see cli.pilot).

When the rebuilder cannot tell which piece of the card comes next in a photo, it stops and lists
the candidates. Here each candidate can be shown as a picture of the photo around the join, and a
choice applied: the photo is then rebuilt with that piece, and kept only if it passes the same
checks as any other.

This is a part of Recovery (see recover.py), kept in its own file.
"""
from __future__ import annotations

import io

import numpy as np
from PIL import Image

from .items import GRADE_PARTIAL, GRADE_REVIEW
from .rebuild import Result
from .util import ceil_div, count_pieces


class Review:
    """Showing and applying the choices left to a person."""

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
        if it._owner is not None and it._owner is not self:
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
            for _score, c in it.candidates[:6]:
                chain, used, cc = list(r.chain) + [c], set(r.chain) | {c}, c
                for _ in range(max(3, ceil_div(it.size or 0, vol.cl) // 4)):   # let it run on for a quarter of the photo
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
        if it._owner is not None and it._owner is not self:
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
                pieces = count_pieces(r2.chain)
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
        if self.out and self._is_top:
            self._write_report()
        return it.grade
