"""The whole recovery run: read a card image, bring back every file that can be proved whole,
rebuild what is stored in pieces, check everything once more, save it, and write the report.

The image is only ever read. Output goes to a separate folder.

How a run goes (Recovery.run):

1. Read the card's folders, including deleted ones and folders a format left behind. If the card
   was reformatted with different settings, work out its old layout too (legacy.py) and recover
   the files deleted since that format first, in the card's current layout.
2. Listed photos still in one piece (_whole_photos), then videos (_videos, checked frame by frame
   against their own index), then other files (_other_files).
3. Listed photos stored in pieces (_photos_in_pieces), followed piece by piece through the card.
4. Photos, other pictures and videos no folder lists any more (scan.py).
5. The final check and saving (deliver.py), then files never deleted (_save_existing), any other
   partitions on the card (_other_partitions), and why a card may have nothing to give (diagnose.py).

Each pass takes only what its own evidence proves, and what it takes leaves the pool of free
clusters (self._avail), so that no later pass can hand the same space out twice.

Where things live: checks.py says whether a file's bytes are whole, rebuild.py follows one photo
through the card, video.py one video, output.py names and sorts the saved files, review.py lets
a person decide close calls, items.py holds the record kept for each file.
"""
from __future__ import annotations

import json
import time
import warnings
from pathlib import Path

import numpy as np

from . import __version__, jpeg
from .checks import SIGNATURES, _valid_isobmff, audit_jpeg, boxes_length, check_other, pixel_audit
from .deliver import SaveResults
from .fat import Entry, NotFat, open_volume
from .items import (GRADE_LIVE, GRADE_LOST, GRADE_NO_PREVIEW, GRADE_PARTIAL, GRADE_REVIEW, GRADE_UNVERIFIED,
                    GRADE_VERIFIED, JPEG_EXT, Item, kind_of)
from .output import FOLDER_LIVE, FOLDER_OK
from .rebuild import Rebuilder, Result
from .review import Review
from .scan import FindUnlisted
from .util import ceil_div, count_pieces, file_ext, look_ahead, safe_name, set_mtime, unique_name
from .video import CONTAINERS, VideoRebuilder

warnings.filterwarnings("ignore", message="Image appears to be a malformed MPO")


class Recovery(FindUnlisted, Review, SaveResults):
    """One recovery run over a card image.

        summary = Recovery(image, out_dir, log).run()

    image is a path to the card image (or the image itself, memory-mapped). out_dir is where the
    results go (None: recover without saving anything, as the tests sometimes do). log is called
    with a line of plain text as each step starts. Before run(), always_ask leaves every uncertain
    join in a photo to a person, and include_existing also copies out files that were never deleted.

    While it runs, progress is (done, total) within the current step. Afterwards items lists every
    file, summary is what the report and the wizard show, and review_queue() lists the close calls
    still waiting for a person.

    A run can start runs of its own for other parts of the same card: the files deleted since the
    card was last reformatted (see _layer) and each further partition (see _partition).
    """

    def __init__(self, image, out_dir=None, log=None):
        self._setup(out_dir, log)
        self.vol, self.unreadable = self._open(image)
        self._first_vol = self.vol
        self.blank_ranges = _unreadable_ranges(image)

    def _setup(self, out_dir, log, listed_only: list[Entry] | None = None, is_partition: bool = False,
               taken: set[str] | None = None) -> None:
        """Everything a run starts with, before a volume is opened."""
        self.out = Path(out_dir) if out_dir else None
        self._listed_only = listed_only     # recover just these folder entries, and look for nothing else
        self._is_partition = is_partition   # one more partition of a card that another run is recovering
        self._shared_taken = taken          # output names already used by the run that started this one
        self._out_full = False              # the output drive has filled up: nothing more can be saved
        say = log or (lambda msg: None)
        self.steps: list[list] = []         # [message, time it started] for each step, for the report

        def _log(msg):                      # a new step starts: the count within the old one no longer applies
            self.progress = None
            self.steps.append([msg, time.time()])
            say(msg)
        self.log = _log
        self.items: list[Item] = []
        self.summary: dict = {}
        self.problems: list[str] = []       # anything that went wrong along the way, for the report
        self.always_ask = False
        self.include_existing = False       # also copy out files that were never deleted
        self.blank_ranges: list[tuple[int, int]] = []   # byte ranges the copy could not read from the card
        self._existing: list[Entry] = []    # files the card lists as never deleted
        self._other_starts: list[tuple[int, str]] = []
        self.progress: tuple[int, int] | None = None     # (done, total) within the current step

    @property
    def _is_top(self) -> bool:
        """The run a person started, as opposed to one it started itself for part of the card. Only
        this one looks for more partitions, says what kind of card this is, and writes the report."""
        return self._listed_only is None and not self._is_partition

    def _open(self, image):
        """The card's file system, and "" or what kind of drive it is when this tool cannot read its
        format (it is then searched directly for photos)."""
        try:
            vol = open_volume(image)
            if getattr(vol, "repaired", False):
                self.log("The start of this card is damaged, which is why a computer says it needs formatting. "
                         "A spare copy of its layout was found on the card and is being used")
            return vol, ""
        except NotFat:
            from .fat import _open_ro
            from .legacy import open_unreadable
            m = _open_ro(image) if isinstance(image, (str, Path)) else image
            if len(m) < (1 << 20):
                raise
            vol = open_unreadable(m, self.log)
            return vol, vol.what

    @classmethod
    def _layer(cls, parent: "Recovery", vol, entries: list[Entry]) -> "Recovery":
        """A quiet run over the files deleted since the card was last reformatted, read in the card's
        current layout (vol), while parent goes on to read the older layout."""
        sub = cls.__new__(cls)
        sub._setup(parent.out, None, listed_only=entries, taken=set())
        sub.vol = sub._first_vol = vol
        sub.unreadable = ""
        sub.log = lambda msg: None
        sub.always_ask = parent.always_ask
        sub.blank_ranges = parent.blank_ranges
        return sub

    @classmethod
    def _partition(cls, parent: "Recovery", vol) -> "Recovery":
        """A run over one more partition (vol) of the card parent is recovering. It logs as parent
        does, and saves into the same folders without reusing any of parent's names."""
        sub = cls.__new__(cls)
        sub._setup(parent.out, None, is_partition=True, taken=parent._taken)
        sub.vol = sub._first_vol = vol
        sub.unreadable = ""
        sub.log, sub.always_ask, sub.include_existing = parent.log, parent.always_ask, parent.include_existing
        sub.blank_ranges = parent.blank_ranges
        return sub

    def run(self) -> dict:
        """The whole recovery, pass by pass (see the top of this file). Returns the summary."""
        t0 = self._t0 = time.time()
        entries, newer = self._read_folders()
        vol = self.vol
        self._blank = vol.blank_clusters()
        self._avail = ~vol.allocated() & ~self._blank      # clusters that may still hold a lost file
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
        found = self._scan_for_photos(tried) if self._listed_only is None else []
        if self._listed_only is None:
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
        self._summarize(newer, len(items), used_fraction, t0)
        if self._is_top:
            self._other_partitions()
        self.summary["unverified_kinds"] = self._unverified_kinds()
        self.summary["findings"] = self._findings() if self._is_top else []
        unsaved = sum(1 for it in self.items if it._unsaved)
        if unsaved and self._is_top:
            self.summary["findings"].insert(0, _out_full_finding(unsaved))
        if self.out and self._is_top:
            try:
                self._write_report()
            except OSError as e:
                self.summary["report_error"] = f"{type(e).__name__}: {e}"
                self.summary["problems"] = (self.problems + [f"the report could not be written: {e}"])[:50]
        return self.summary

    def _summarize(self, newer: "Recovery | None", listed: int, used_fraction: float, t0: float) -> None:
        """The summary of this run's own part of the card. (Other partitions are added to it after.)"""
        vol, steps = self.vol, self.steps
        self.summary = dict(tool=f"photorecover {__version__}",
                            filesystem=(f"{newer.vol.kind} now, and {vol.kind}" if newer is not None else vol.kind),
                            cluster_size=vol.cl, clusters=vol.ncl, card_in_use=round(float(used_fraction), 3),
                            files_listed=listed, counts=self._counts(), seconds=round(time.time() - t0, 1),
                            fast_decoder=jpeg.HAVE_C, problems=self.problems[:50],
                            steps=[[m, round((steps[i + 1][1] if i + 1 < len(steps) else time.time()) - t, 1)]
                                   for i, (m, t) in enumerate(steps)],
                            old_layout=self.old_layout, unreadable=self.unreadable)
        self.summary["since_format"] = len(newer.items) if newer is not None else 0
        self.summary["still_on_card"] = self._save_existing() if self._listed_only is None else None
        if self._listed_only is None:       # the files never deleted may just have been added
            self.summary["counts"] = self._counts()
            self.summary["files_listed"] = len(self.items)
            self.summary["problems"] = self.problems[:50]
        self.summary["damaged_start"] = bool(getattr(self.vol if newer is None else newer.vol, "repaired", False))
        self.summary["unreadable_bytes"] = sum(b - a for a, b in self.blank_ranges)

    def _unverified_kinds(self) -> dict[str, int]:
        """What the files that came back but cannot be checked are: photos, videos or other."""
        kinds = {"photos": 0, "videos": 0, "other": 0}
        for it in self.items:
            if it.grade == GRADE_UNVERIFIED:
                kinds[kind_of(file_ext(it.name))] += 1
        return kinds

    def _claim(self, chain) -> None:
        self._avail[np.asarray(chain, np.int64)] = False

    def _still_free(self, r) -> bool:
        return bool(self._avail[np.asarray(r.chain, np.int64)].all())

    def _ahead(self, jobs, fn):
        """look_ahead with as many processor cores as decoding can use."""
        return look_ahead(jobs, fn, jpeg.decode_workers())

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
        if self._listed_only is not None:
            entries, old = self._listed_only, None
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
                old = None if self.unreadable or self._is_partition else find_old_layout(vol, entries, self.log)
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
                newer = Recovery._layer(self, vol, mine)
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

    # ---- helpers ----------------------------------------------------------
    def _resolve_start(self, e: Entry, ext: str) -> int:
        """On FAT32 the high half of the start cluster is often zeroed by the delete.
        Try the stored value first, then every other high half, and keep the one
        that lands on the right kind of file."""
        vol = self.vol
        if vol.fat_bits != 32:
            return e.start

        if self._begins_like(e.start, ext):
            return e.start
        lo = e.start & 0xFFFF
        hits = [c for hi in range((vol.max_cluster >> 16) + 1) if self._begins_like(c := lo | (hi << 16), ext)]
        return hits[0] if len(hits) == 1 else e.start

    def _begins_like(self, c: int, ext: str) -> bool:
        """Does cluster c begin the way a file with this ending begins? False for kinds that have
        no fixed first bytes."""
        vol = self.vol
        if not vol.valid(c):
            return False
        head = vol.cluster(c)[:16]
        if ext in JPEG_EXT:
            return head[:3] == b"\xff\xd8\xff"
        sig = SIGNATURES.get(ext)
        return bool(sig and sig[1] and head[sig[0]:sig[0] + len(sig[1])] == sig[1])

    def _starts_right(self, e: Entry) -> bool:
        """Does the cluster this entry points to, in the card's current layout, begin the way
        a file of its type begins?"""
        return self._begins_like(self._resolve_start(e, e.ext), e.ext)

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
                pieces = count_pieces(hint)
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
        videos = [i for i in others if file_ext(i.name) in CONTAINERS]
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
            pieces = count_pieces(real)
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
            n = ceil_div(it.size, cl)
            if not vol.valid(it.start) or it.start + n - 1 > vol.max_cluster:
                it.note = ("the card no longer records where this file was stored" if it.start == 0
                           else "start cluster is outside the card")
                continue
            chain = it._hint or list(range(it.start, it.start + n))
            if not self._avail[chain].all():
                it.note = "its space was reused by another file"
                continue
            ext = file_ext(it.name)
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
                it.note = it._video_why or \
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
                    pieces = count_pieces(r.chain)
                    self._accept(it, r, "found whole" if pieces == 1 else f"rebuilt from {pieces} pieces")
                    self._claim(r.chain)
                elif r.status == "complete":
                    loose.append((it, r))
                else:
                    it._last = r
                    still.append(it)
            pending = still
            if rnd == 0 and pending:
                self.log(f"Searching the whole card for missing pieces of {len(pending)} damaged photos")
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
        n = ceil_div(it.size, vol.cl)
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
                sub = Recovery._partition(self, vol)
                sub.run()
            except Exception as e:  # noqa: BLE001
                self.problems.append(f"partition {n}: {type(e).__name__}: {e}")
                continue
            for it in sub.items:
                if it._owner is None:
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
        live = self._existing
        if not live:
            return None
        info = dict(files=len(live), photos=0, videos=0, other=0, bytes=sum(e.size for e in live), saved=False, folders={})
        media: dict[str, int] = {}
        for e in live:
            info[kind_of(e.ext)] += 1
            top = e.path or "(top folder)"
            info["folders"][top] = info["folders"].get(top, 0) + 1
            media[top] = media.get(top, 0) + (kind_of(e.ext) != "other")
        # folders that hold photos and videos first: those are what the person needs to hear about
        info["folders"] = dict(sorted(info["folders"].items(), key=lambda kv: (-media[kv[0]], -kv[1]))[:15])
        save = self.out and (self.include_existing or getattr(vol, "repaired", False))
        if not save:
            return info
        self.log(f"Saving {len(live)} files that are still on the card")
        taken = self._taken
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
                rel = unique_name("/".join([FOLDER_LIVE] + [safe_name(p) for p in e.path.split("/") if p]
                                           + [safe_name(e.name)]), taken)
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
                set_mtime(dest, e.mtime)
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


def _unreadable_ranges(image) -> list[tuple[int, int]]:
    """The parts of the card the copy could not read (blank in the image), as (start, end) byte
    ranges. The copy lists them in a file beside the image."""
    if isinstance(image, (str, Path)):
        try:
            listed = json.loads(Path(str(image) + ".bad.json").read_text())
            return sorted((int(a), int(a) + int(n)) for a, n in listed["unreadable"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return []


def _out_full_finding(unsaved: int) -> dict:
    """What the report says first when the drive the results go to filled up."""
    return dict(kind="out_full", sure=True, text=(
        f"The drive the results were being saved to filled up, so {unsaved} file{'s' if unsaved != 1 else ''} "
        "that could be recovered "
        f"{'were' if unsaved != 1 else 'was'} not saved. Nothing is wrong with the card. Free some space, or "
        "choose a folder on a drive with more room, and run the recovery again."))
