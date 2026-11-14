"""The last step of a recovery: check every file once more on the exact bytes being written, and
save it. Nothing reaches the Recovered folder without passing that final check.

This is a part of Recovery (see recover.py), kept in its own file.
"""
from __future__ import annotations

import errno
import json

from .checks import audit_jpeg
from .items import GRADE_LOST, GRADE_NO_PREVIEW, GRADE_PARTIAL, GRADE_REVIEW, GRADE_VERIFIED, GRADES_RECOVERED, Item
from .output import (FOLDER_FILLED, FOLDER_OK, FOLDER_PARTIAL, FULL_NOTE, cluster_runs, kind_folder, preview_fill,
                     write_whole)
from .util import ceil_div, in_one_run, safe_name, set_mtime, unique_name


class SaveResults:
    """Writing the recovered files and the report."""

    def _deliver(self) -> None:
        """The mandatory final audit, then write files. Nothing reaches Recovered
        without passing the audit on the exact bytes being written."""
        self._taken: set[str] = self._shared_taken if self._shared_taken is not None else set()
        todo = [it for it in self.items if it.chain]
        if self.blank_ranges:
            self._mark_unreadable(todo)
        if todo:
            self.log(f"Checking every recovered file once more and saving {len(todo)} files")
        for k, (it, verdict) in enumerate(self._ahead(todo, self._audit)):
            self.progress = (k, len(todo))
            if self._out_full:
                it._unsaved, it.saved_as = True, ""
                it.add_note(FULL_NOTE)
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
                    it.add_note(FULL_NOTE)
                    continue
                self.problems.append(f"{it.name}: could not be saved ({type(e).__name__}: {e})")
                it.add_note("this file could not be written to the output folder")
                it.saved_as = ""
                if it.grade != GRADE_LOST:
                    it.grade = GRADE_REVIEW

    def _mark_unreadable(self, todo: list[Item]) -> None:
        """On a failing card, some parts could not be copied and are blank in the image. Every file
        that touches one of them says so, and none of them is passed off as whole."""
        vol = self.vol
        hit: set[int] = set()            # clusters of this layout that touch a part of the card that could not be read
        for a, b in self.blank_ranges:
            c0 = max(2, (a - vol.data_off) // vol.cl + 2)
            c1 = min(vol.max_cluster, (b - 1 - vol.data_off) // vol.cl + 2)
            hit.update(range(c0, c1 + 1))

        def straight_run(it: Item) -> range:
            return range(it.start, it.start + ceil_div(it.size, vol.cl))

        for it in self.items:
            if it.grade in (GRADE_PARTIAL, GRADE_REVIEW, GRADE_LOST) and it.size and hit.isdisjoint(it.chain) \
                    and not hit.isdisjoint(straight_run(it)):
                if it.grade == GRADE_REVIEW:
                    # nothing for a person to choose between: the next piece is simply unreadable
                    it.grade, it.candidates, it.note = GRADE_PARTIAL, [], ""
                it.add_note("part of the card where this file is stored could not be read"
                            + (f"; the picture is good down to about {it.percent:.0f}% and lost from there on"
                               if it.kind == "jpeg" and it.chain else ""))
                # If the photo's own end marker sits exactly where its recorded size says, the file
                # lies in one run straight through the unreadable patch. Then the whole file is
                # worth keeping as well: picture viewers carry on past a gap, usually with the
                # rest shifted or discoloured, which is far more than a sliver of the top.
                it._whole = self._runs_through(it)
        for it in todo:
            if not hit.isdisjoint(it.chain):
                it.add_note("part of this file was in an area of the card that could not be read, and is blank there")
                if it.grade in GRADES_RECOVERED:
                    it.grade = GRADE_PARTIAL        # bytes known to be missing: never "recovered"
                if it.kind == "jpeg" and it.percent < 100.0 and it.size and not hit.isdisjoint(straight_run(it)):
                    it._whole = self._runs_through(it)

    def _runs_through(self, it: Item) -> bool:
        """Does this photo's own ending sit exactly where its recorded size says, counting
        straight on from its start? (The ending is the end marker, or the block of notes some
        phone cameras add after it, which states its own length.)"""
        vol = self.vol
        n = ceil_div(it.size, vol.cl)
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

    def _data(self, it: Item) -> bytes:
        """The bytes to save for this file, read from its chain of clusters."""
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
        """Run the final check on one file (unless verdict already holds its result) and save it."""
        vol, taken = self.vol, self._taken
        if not it.chain:
            return
        data = self._data(it)
        if verdict == "ask":
            verdict = self._audit(it)
        if verdict is not None:
            ok, note = verdict
            if not ok:
                whole = in_one_run(it.chain)
                if whole and "preview" in note:
                    it.add_note("the picture " + note + " (its preview may be out of date)")
                    it.grade = GRADE_NO_PREVIEW      # complete and in one piece, but the preview did not vouch for it
                else:
                    it.grade, it.note = GRADE_REVIEW, "failed the final check: " + note
        it.out_bytes = len(data)
        if not self.out:
            return
        folder = FOLDER_OK if it.grade in GRADES_RECOVERED else FOLDER_PARTIAL
        # Sorted by what the file is, so that the photos are one click away. The folders the
        # card had are not rebuilt (the report says where each file was): this rescues files,
        # it does not put a card back the way it was.
        sub = kind_folder(it.name, data)
        name = safe_name(it.name)
        if folder == FOLDER_PARTIAL and it.percent < 100.0:
            stem, dot, ext = name.rpartition(".")
            name = f"{stem} ({it.percent:.0f}%){dot}{ext}" if dot else f"{name} ({it.percent:.0f}%)"
        rel = unique_name("/".join(x for x in (folder, sub, name) if x), taken)
        dest = self.out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_whole(dest, data)
        set_mtime(dest, it.mtime)
        it.saved_as = rel
        if it._whole:
            stem, dot, ext = rel.rpartition(".")
            wrel = stem.rsplit(" (", 1)[0] + f" (whole file, damaged from {it.percent:.0f}%).jpg"
            write_whole(self.out / wrel, bytes(vol.read_run(it.start, it.size)))
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
                write_whole(fdest, filled)
                taken.add(frel.lower())
                it.filled_as = frel

    def _write_report(self) -> None:
        """report.json (every detail, for programs) and report.html (the same, for people)."""
        from .report import write_html
        rows = [dict(name=i.name, folder=i.folder, grade=i.grade, percent=i.percent, method=i.method, filled_as=i.filled_as,
                     note=i.note, bytes=i.out_bytes, saved_as=i.saved_as, source=i.source,
                     start_cluster=i.start, clusters=len(i.chain), picks=i.picks, candidates=i.candidates,
                     size=i.size, modified=list(i.mtime) if i.mtime else None, runs=cluster_runs(i.chain))
                for i in self.items]
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "report.json").write_text(json.dumps(dict(summary=self.summary, files=rows), indent=1))
        write_html(self.out / "report.html", self.summary, rows)
