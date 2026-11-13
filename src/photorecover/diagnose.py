"""Reasons a card cannot give its photos back, said plainly.

Some cards hold nothing a recovery tool can use: storage that is encrypted, a card that has been
wiped, and a card that was never as big as it claims. A person looking at an empty result needs
to know which, and whether trying another tool or paying a lab could help. Each check here
answers from what is on the card and stays quiet unless the evidence is strong."""
from __future__ import annotations

import struct

import numpy as np

from .fat import gpt_partitions, partition_starts

SECTOR = 512

ANDROID_EXPAND = bytes.fromhex("a41e3d19cab3e411b0751060 4b889dcf".replace(" ", ""))
PHOTO_START = {"jpg": b"\xff\xd8\xff", "jpeg": b"\xff\xd8\xff", "png": b"\x89PN", "mp4": None, "heic": None}


def _starts(m) -> list[int]:
    return sorted({0, *partition_starts(m)})


def locked_container(m) -> str:
    """Names the lock, when the drive (or a partition of it) says outright that it is encrypted."""
    for _type, _at, _n, name in gpt_partitions(m):
        if _type == ANDROID_EXPAND or name == "android_expand":
            return "set up as a phone's built-in storage (Android calls this adopting the card), which the phone encrypts"
    for at in _starts(m):
        b = bytes(m[at:at + SECTOR])
        if b[3:11] == b"-FVE-FS-":
            return "locked with BitLocker, the Windows drive encryption"
        if b[:6] == b"LUKS\xba\xbe":
            return "encrypted with LUKS, the Linux drive encryption"
    return ""


def _find(kind: str, sure: bool, text: str) -> dict:
    return dict(kind=kind, sure=sure, text=text)


def _noise(m, survey, n: int = 512) -> bool:
    """Do the card's sectors look like random noise? A sector of noise holds about 220 different
    byte values; text, folders and tables hold far fewer, and photo data rarely as many throughout."""
    total = survey.n
    rng = np.random.default_rng(1)
    seen = []
    for s in rng.integers(0, total, n * 4).tolist():
        if survey.blank(s, 1)[0]:
            continue
        a = np.frombuffer(m, np.uint8, SECTOR, s * SECTOR)
        seen.append(len(np.unique(a)))
        if len(seen) >= n:
            break
    if len(seen) < 64:
        return False
    seen.sort()
    return seen[len(seen) // 20] >= 190        # 19 in 20 of the sectors looked at


def _scrambled_files(vol, existing) -> tuple[int, int]:
    """Of the photos the card lists as ordinary files, how many do not begin the way their kind
    of file begins. (looked at, wrong)"""
    looked = wrong = 0
    m = vol.m
    for e in existing:
        want = PHOTO_START.get(e.ext)
        if want is None or e.size < 4096 or not (2 <= e.start <= getattr(vol, "max_cluster", 0)):
            continue
        o = vol.data_off + (e.start - 2) * vol.cl
        looked += 1
        wrong += bytes(m[o:o + 3]) != want
        if looked >= 400:
            break
    return looked, wrong


def _wraps(m, survey) -> int:
    """A card that claims more room than it has often stores a write to its upper part on top of
    its lower part. Then the upper part reads back as a copy of the lower: returns the real size
    in bytes when that pattern is there, else 0."""
    total = survey.n * SECTOR
    sizes = {total >> k for k in range(1, 7)} | {g << 30 for g in (1, 2, 4, 8, 16, 32, 64, 128, 256)}
    rng = np.random.default_rng(2)
    for real in sorted(s for s in sizes if (64 << 20) <= s <= total // 2 and s % SECTOR == 0):
        same = looked = 0
        for p in rng.integers(real // SECTOR, survey.n, 600).tolist():
            if survey.blank(p, 1)[0]:
                continue
            looked += 1
            q = (p * SECTOR) % real
            same += bytes(m[p * SECTOR:(p + 1) * SECTOR]) == bytes(m[q:q + SECTOR])
            if looked >= 48:
                break
        if looked >= 32 and same >= looked * 0.9:
            return real
    return 0


def _empty_beyond(vol) -> int:
    """The other way a too-small card fails: what is written past its real size is simply lost, so
    files the card lists up there read back blank. Returns the byte position where that begins."""
    try:
        used = vol.allocated()
        blank = vol.blank_clusters()
    except Exception:  # noqa: BLE001
        return 0
    n = min(len(used), len(blank))
    used, blank = used[:n].copy(), blank[:n]
    used[:2] = False
    lost = np.flatnonzero(used & blank)
    in_use = np.flatnonzero(used)
    need = max(64, (4 << 20) // vol.cl)
    if len(lost) < need or len(lost) < 0.1 * len(in_use):
        return 0
    edge = int(lost[len(lost) // 50])           # ignore a stray blank cluster or two lower down
    above, below = in_use[in_use >= edge], in_use[in_use < edge]
    if len(below) < need:
        return 0
    lost_above = np.count_nonzero(blank[above]) / len(above)
    lost_below = np.count_nonzero(blank[below]) / len(below)
    if lost_above >= 0.98 and lost_below <= 0.02:
        return vol.data_off + (edge - 2) * vol.cl
    return 0


def _gb(n: float) -> str:
    return f"{n / 1e9:.0f} GB" if n >= 10e9 else f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def diagnose(m, survey, vol=None, existing=(), unreadable: bool = False, recovered: int = 0) -> list[dict]:
    """What, if anything, makes this card one that cannot give its photos back."""
    out: list[dict] = []
    total = survey.n
    if total == 0:
        return out
    blank = sum(int(np.unpackbits(bits).sum()) for bits in survey._same)
    blank_part = blank / total
    photo_starts = sum(len(survey.sigs[k]) for k in ("jpg", "png", "mp4") if k in survey.sigs)

    lock = locked_container(m)
    if lock:
        out.append(_find("encrypted", True,
                         f"This drive is {lock}. Everything on it is scrambled, and no recovery tool can read it "
                         "from outside. Unlock it first on the device or computer that locked it, with its password "
                         "or recovery key, and copy the photos off from there."))
    elif unreadable and total - blank >= 2048 and photo_starts <= (total - blank) // (1 << 21) + 2 and _noise(m, survey):
        out.append(_find("encrypted", False,
                         "Nearly everything on this drive looks like random noise, with no sign of a single photo. "
                         "That is what encrypted storage looks like (a phone or computer that encrypts the card, or "
                         "an encryption program). If so, it can only be read on the device that encrypted it."))
    if vol is not None and not unreadable and not lock:
        looked, wrong = _scrambled_files(vol, existing)
        if looked >= 20 and wrong >= looked * 0.9:
            out.append(_find("encrypted", False,
                             f"The card lists {looked if looked < 400 else 'hundreds of'} photos as ordinary files, but "
                             "their contents are scrambled: they do not begin the way photos do. Some phones encrypt "
                             "each file on a memory card so that only that phone can open it. Put the card back in "
                             "the phone that wrote it and turn the card's encryption off there (decrypt the card); "
                             "after a phone reset or with a different phone, the key is gone and nothing can read them."))

    if blank_part >= 0.999 and photo_starts == 0 and not recovered:
        out.append(_find("wiped", True,
                         f"This card is blank: {blank_part * 100:.1f}% of it holds nothing at all. It has been wiped "
                         "(a full format, a secure erase, or a camera or phone telling the card to clear itself), and "
                         "what is wiped this way is gone from the card for every tool. A paid recovery lab cannot "
                         "bring it back either."))
    elif blank_part >= 0.9 and not recovered and not out:
        out.append(_find("mostly_blank", False,
                         f"{blank_part * 100:.0f}% of this card holds nothing at all. Those parts were either never used "
                         "or have been wiped, and nothing can be recovered from them by any tool."))

    real = _wraps(m, survey)
    how = "what is written to the rest lands on top of older files"
    if not real and vol is not None and not unreadable:
        real = _empty_beyond(vol)
        how = "files stored beyond that read back empty"
    if real:
        out.append(_find("fake_capacity", False,
                         f"This card may not be the size it says. It reports {_gb(total * SECTOR)}, but only about the "
                         f"first {_gb(real)} seems to really store anything: {how}. Cards sold with a false size do "
                         "this, and photos saved after the card filled its real size were never kept, so no tool can "
                         "recover them. Stop using the card; a free checker (H2testw on Windows, F3 on Mac or Linux) "
                         "will confirm it."))
    return out
