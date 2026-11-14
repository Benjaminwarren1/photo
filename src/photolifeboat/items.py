"""What the recovery knows about each file: its grade, and the record kept for it from first
sighting to the report.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import streams

# The result each file ends up with. These words appear in the report and the wizard as they are.
GRADE_VERIFIED = "Verified"                    # whole, and its picture matches its own preview
GRADE_NO_PREVIEW = "Complete, no preview"      # whole by every structural check; no preview to compare with
GRADE_UNVERIFIED = "Recovered, unverified"     # came back whole, but this kind of file cannot be checked
GRADE_PARTIAL = "Partial"                      # the good part is saved; the rest is not on the card
GRADE_REVIEW = "Needs review"                  # a close call left to a person
GRADE_LOST = "Not recoverable"                 # nothing usable is left on the card
GRADE_LIVE = "Still on the card"               # never deleted: copied as it is
GRADES_RECOVERED = (GRADE_VERIFIED, GRADE_NO_PREVIEW, GRADE_UNVERIFIED)    # these go in the Recovered folder

JPEG_EXT = {"jpg", "jpeg", "jpe"}
VIDEO_EXT = {"mp4", "mov", "3gp", "3g2", "m4v", "avi", "mkv", "lrv"} | streams.TS_EXT
PHOTO_EXT = JPEG_EXT | {"png", "gif", "bmp", "heic", "heif", "mpo", "tif", "tiff", "dng", "nef", "nrw", "pef", "cr2", "cr3", "arw",
                        "raf", "orf", "rw2", "webp"}


def kind_of(ext: str) -> str:
    """"photos", "videos" or "other", for counting what a set of files is."""
    return "photos" if ext in PHOTO_EXT else "videos" if ext in VIDEO_EXT else "other"


@dataclass
class Item:
    """One file the recovery is trying to bring back, and everything decided about it so far.

    The fields without a leading underscore go into the report. The private ones are working notes
    that only matter while the run is going on."""
    name: str
    folder: str                     # where it used to be on the card ("Found by scanning" if not known)
    size: int | None                # bytes, as the card recorded it (None until a scanned photo is finished)
    start: int                      # first cluster
    kind: str                       # "jpeg" (rebuilt block by block) or "other"
    source: str                     # "deleted entry" | "lost folder entry" | "found by scanning" | "still on the card"
    grade: str = GRADE_LOST
    percent: float = 0.0            # how much of it is recovered
    method: str = ""                # how it was recovered, in plain words
    note: str = ""                  # anything a person should know about it
    chain: list[int] = field(default_factory=list)      # its clusters, in order (-1: missing from the card)
    out_bytes: int = 0              # bytes saved
    saved_as: str = ""              # path inside the output folder
    filled_as: str = ""             # a second copy whose missing part is filled in from the preview
    mtime: tuple | None = None      # (year, month, day, hour, minute) the card recorded
    candidates: list = field(default_factory=list)      # (score, cluster) choices for the next piece, for a person
    picks: list = field(default_factory=list)           # joins that were not simply "the next cluster", and why
    _forced: dict = field(default_factory=dict, repr=False)   # choices made by a person: chain index -> cluster
    _hint: list | None = field(default=None, repr=False, compare=False)     # chain the file system suggests, if any
    _last: object = field(default=None, repr=False, compare=False)          # the last rebuild attempt (a Result)
    _owner: object = field(default=None, repr=False, compare=False)         # the Recovery that found it, when that
    #                                                       was a separate run (an older layer, another partition)
    _video_why: str = field(default="", repr=False, compare=False)          # why the frame check could not run
    _whole: bool = field(default=False, repr=False, compare=False)          # also save it whole, past an unreadable patch
    _unsaved: bool = field(default=False, repr=False, compare=False)        # could be recovered, but the output drive was full

    def add_note(self, text: str) -> None:
        """Add one more thing to say about this file to its note."""
        self.note = (self.note + "; " if self.note else "") + text
