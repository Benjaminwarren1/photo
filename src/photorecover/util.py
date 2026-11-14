"""Small helpers shared by several parts of the program.

Nothing here knows about any one file system or file format; each helper does one plain job
that used to be written out by hand wherever it was needed.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np


def ceil_div(a: int, b: int) -> int:
    """a / b rounded up: how many clusters of b bytes it takes to hold a bytes."""
    return -(-a // b)


def file_ext(name: str) -> str:
    """The file ending of a name, lower case and without the dot ("" when there is none)."""
    return name.rpartition(".")[2].lower() if "." in name else ""


def count_pieces(chain) -> int:
    """How many separate runs of clusters a chain is made of: 1 for a file stored in one piece."""
    return 1 + sum(1 for a, b in zip(chain, chain[1:]) if b != a + 1)


def in_one_run(chain) -> bool:
    """True when every cluster in the chain follows straight on from the one before."""
    return all(b == a + 1 for a, b in zip(chain, chain[1:]))


def next_free(avail: np.ndarray, c: int, used: set[int], top: int) -> int | None:
    """The first cluster after c, up to top, that is still free (avail) and not already in used."""
    c += 1
    while c <= top:
        if avail[c] and c not in used:
            return c
        c += 1
    return None


def unique_name(rel: str, taken: set[str]) -> str:
    """rel itself, or "name (2).ext", "name (3).ext" ... if that path is already in taken.
    Compared without regard to case, as Windows does. The chosen path is added to taken."""
    out, k = rel, 1
    while out.lower() in taken:
        k += 1
        stem, dot, ext = rel.rpartition(".")
        out = f"{stem} ({k}){dot}{ext}" if dot else f"{rel} ({k})"
    taken.add(out.lower())
    return out


def safe_name(name: str) -> str:
    """A file or folder name with the characters Windows does not allow replaced by "_"."""
    bad = '<>:"/\\|?*'
    out = "".join("_" if (ch in bad or ord(ch) < 32) else ch for ch in name).strip(" .")
    return out or "_"


def set_mtime(path: Path, mtime: tuple | None) -> None:
    """Give a saved file the date and time the card recorded for it, (year, month, day, hour, minute).
    A date the card got wrong is simply left out."""
    if not mtime:
        return
    try:
        y, mo, d, h, mi = mtime
        ts = time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))
        os.utime(path, (ts, ts))
    except (ValueError, OverflowError, OSError):
        pass
