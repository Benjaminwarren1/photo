"""Finding files that no folder entry lists any more.

When a card's folders are gone (overwritten, or the card was reformatted), the files can still be
found by what they begin with: every JPEG starts with the same three bytes, every PNG with the
same eight, and so on. Each such start is followed through the card like a listed file, and is
only kept when its own contents vouch for it.

This is a part of Recovery (see recover.py), kept in its own file. It works on the same state:
self.vol, self._avail, self._blank, self._items, self._rb, self._vr, and the helpers _claim,
_still_free, _attempt, _ahead and _accept.
"""
from __future__ import annotations

import numpy as np

from . import formats, streams
from .checks import audit_jpeg, boxes_length, png_length, unfinished_video
from .items import GRADE_PARTIAL, GRADE_REVIEW, GRADE_LOST, GRADE_UNVERIFIED, GRADE_VERIFIED, Item
from .util import ceil_div, count_pieces, in_one_run


class FindUnlisted:
    """The passes that look for photos, other pictures and videos with no folder entry."""

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

    def _scan_for_photos(self, tried) -> list[Item]:
        """Pass 3: photos with no folder entry left at all."""
        heads = self._photo_starts(self._avail)
        if heads:
            self.log(f"Looking at {len(heads)} photo starts that have no folder entry")
        found: list[Item] = []

        def take(c: int, r) -> None:
            it = Item(name="", folder="Found by scanning", size=r.size, start=c,
                      kind="jpeg", source="found by scanning")
            pieces = count_pieces(r.chain)
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
            self.log(f"Searching the whole card for missing pieces of {len(again)} damaged photos")
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
        others_found = [(c, k) for c, k in self._other_starts
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
                  method="found whole" if in_one_run(chain) else "rebuilt from pieces",
                  note=note, chain=chain)
        self._claim([x for x in chain if x >= 0])
        self._items.append(it)

    def _scan_other(self, c: int, kind: str) -> None:
        """What starts at cluster c, when it is a whole picture or video of another kind. kind is
        what its first bytes look like: "png", "tiff" (camera RAW), "riff" (AVI), "ts" (a
        transport stream) or "ftyp" (MP4, MOV, HEIC and the like)."""
        vol, cl = self.vol, self.vol.cl
        data_end = vol.data_off + (vol.max_cluster - 1) * cl
        pos = vol.offset(c)
        scan = {"png": self._scan_png, "tiff": self._scan_tiff, "riff": self._scan_avi,
                "ts": self._scan_ts}.get(kind, self._scan_isobmff)
        scan(c, pos, data_end)

    def _scan_png(self, c: int, pos: int, data_end: int) -> None:
        """A PNG, taken only if every part of it passes its own checksum."""
        cl = self.vol.cl
        size = png_length(self.vol.m, pos, data_end)
        chain = list(range(c, c + ceil_div(size, cl))) if size else []
        if size and self._avail[chain].all():
            self._found_other(c, "png", "png", size, chain, GRADE_VERIFIED, "every part of it passes its own checksum")

    def _scan_tiff(self, c: int, pos: int, data_end: int) -> None:
        """A camera RAW (or TIFF) file whose tables hold together."""
        vol, cl = self.vol, self.vol.cl
        got = formats.tiff_length(vol.m, pos, data_end)
        if got is None:
            return
        size, ext = got
        chain = list(range(c, c + ceil_div(size, cl)))
        if not self._avail[chain].all():
            return
        checked = formats.check_tiff(vol.read_chain(chain, size), size, audit_jpeg)
        if checked is not None:
            self._found_other(c, "raw", ext, size, chain, GRADE_UNVERIFIED, checked[1])

    def _scan_isobmff(self, c: int, pos: int, data_end: int) -> None:
        """A file built of boxes: an MP4 or MOV video, a HEIC photo, or Canon's newer RAW."""
        vol, cl = self.vol, self.vol.cl
        got = boxes_length(vol.m, pos, data_end)
        if got is None or not ({b"moov", b"moof", b"meta"} & got[1]):
            self._scan_unfinished_video(c, pos, data_end)
            return
        size, seen, brand = got
        chain = list(range(c, c + ceil_div(size, cl)))
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

    def _scan_unfinished_video(self, c: int, pos: int, data_end: int) -> None:
        """A recording the camera never finished (battery died, card pulled out): the video
        data is there but the index that players need was never written. Keep the data
        in one run, up to the first blank or foreign cluster, and say what it is."""
        vol, cl = self.vol, self.vol.cl
        want = unfinished_video(vol.m, pos, data_end)
        if want is None:
            return
        limit = c + (ceil_div(want, cl) if want else (64 << 30) // cl)
        k = c + 1
        while k <= vol.max_cluster and k < limit and self._avail[k] and not self._blank[k] and \
                bytes(vol.cluster(k)[:3]) != b"\xff\xd8\xff" and bytes(vol.cluster(k)[4:8]) != b"ftyp":
            k += 1
        have = min(want, (k - c) * cl) if want else (k - c) * cl
        if have < 256 << 10:
            return
        brand = bytes(vol.m[pos + 8:pos + 12])
        ext = "mov" if brand == b"qt  " else "mp4"
        fix = "A video repair tool can often rebuild it using another clip from the same camera"
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
            chain = list(range(c, c + ceil_div(size, cl)))
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
            self._found_other(c, "video", "avi", have, list(range(c, c + ceil_div(have, cl))), GRADE_PARTIAL,
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
            n = ceil_div(size, cl)
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
