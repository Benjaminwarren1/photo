"""Video that is not built like an MP4: AVI, and the transport streams dashcams and camcorders write.

An AVI file (older compact cameras, many dashcams) is a list of frames followed by an index that
says where each frame is and how long: every entry of that index is a checkpoint, as with MP4.
A transport stream (.ts, .mts, .m2ts) has no index at all: it is a run of equal packets, each
beginning with the same byte. That makes it the one kind of video that survives a recording being
cut off, and the only check it offers is that the packets keep coming at the right step."""
from __future__ import annotations

import struct

import numpy as np

TS_EXT = {"ts", "mts", "m2ts", "m2t", "tod"}
_FRAME = set(b"0123456789")


def _riff(buf, pos: int, end: int) -> tuple[int, bytes] | None:
    if pos + 12 > end or bytes(buf[pos:pos + 4]) != b"RIFF":
        return None
    (size,) = struct.unpack("<I", bytes(buf[pos + 4:pos + 8]))
    return size + 8, bytes(buf[pos + 8:pos + 12])


def avi_length(buf, pos: int, end: int) -> int | None:
    """Length of the AVI file that starts at pos, by its own headers (a long recording is several
    parts one after another), or None when it is not one or does not fit."""
    got = _riff(buf, pos, end)
    if got is None or got[1] != b"AVI " or got[0] < 64 or pos + got[0] > end:
        return None
    total = got[0]
    while True:
        more = _riff(buf, pos + total, end)
        if more is None or more[1] != b"AVIX" or more[0] < 12 or pos + total + more[0] > end:
            return total
        total += more[0]


def _chunks(buf, a: int, b: int):
    """(name, start of contents, length) of each part between a and b; stops at the first that does not fit."""
    while a + 8 <= b:
        name = bytes(buf[a:a + 4])
        (size,) = struct.unpack("<I", bytes(buf[a + 4:a + 8]))
        if a + 8 + size > b:
            return
        yield name, a + 8, size
        a += 8 + size + (size & 1)


def _frame_name(name: bytes) -> bool:
    """Frames and sound blocks are named by stream number and kind: 00dc, 01wb, ix00 ..."""
    return (name[0] in _FRAME and name[1] in _FRAME and name[2:4] in (b"dc", b"db", b"wb", b"pc", b"tx", b"sb")) \
        or name in (b"JUNK", b"LIST") or name[:2] == b"ix"


def _movi(buf, pos: int, end: int) -> tuple[int, int, int, int] | None:
    """(where the frames begin, their length, where the index begins, its length) inside the first part."""
    movi = idx = None
    for name, at, size in _chunks(buf, pos + 12, end):
        if name == b"LIST" and bytes(buf[at:at + 4]) == b"movi":
            movi = (at, size)
        elif name == b"idx1":
            idx = (at, size)
    if movi is None:
        return None
    return movi[0], movi[1], idx[0] if idx else 0, idx[1] if idx else 0


def check_avi(data) -> tuple[str, str] | None:
    """('verified' or 'unverified', note) for a whole AVI file, or None when it is clearly not one any more."""
    n = len(data)
    total = avi_length(data, 0, n)
    if total is None or total < 0.98 * n:
        return None
    first = _riff(data, 0, n)[0]
    got = _movi(data, 0, first)
    if got is None:
        return None
    movi, movi_len, idx, idx_len = got
    if idx_len >= 16:
        e = np.frombuffer(bytes(data[idx:idx + idx_len - idx_len % 16]), np.dtype("<u4")).reshape(-1, 4).astype(np.int64)
        whole = np.frombuffer(data, np.uint8)
        for base in (movi, 0):                       # where the index counts from differs between cameras
            at = e[:, 2] + base
            if (at < 0).any() or (at + 8 > first).any():
                continue
            name = sum(whole[at + i].astype(np.int64) << (8 * i) for i in range(4))
            size = sum(whole[at + 4 + i].astype(np.int64) << (8 * i) for i in range(4))
            if bool((name == e[:, 0]).all()) and bool((size == e[:, 3]).all()):
                k = len(e)
                return "verified", f"all {k} frames and sound blocks are where the video's own index says"
        return None                                   # an index that points at something else: overwritten
    # no index (some dashcams leave it out): the frames must at least follow one another to the end
    a = movi + 4
    for name, at, size in _chunks(data, a, movi + movi_len):
        if not _frame_name(name):
            return None
        a = at + size + (size & 1)
    if a < movi + movi_len - 8:
        return None
    return "unverified", "its frames follow one another from start to end, but it has no index to check them against"


def unfinished_avi(buf, pos: int, end: int) -> int | None:
    """Length of frames that can be kept from an AVI recording that was cut off before the camera
    wrote its index and filled in its length, or None."""
    got = _riff(buf, pos, end)
    if got is None or got[1] != b"AVI ":
        return None
    a = pos + 12
    for name, at, size in _chunks(buf, pos + 12, min(end, pos + (1 << 20))):
        if name == b"LIST" and bytes(buf[at:at + 4]) == b"movi":
            a = at + 4
            break
        a = at + size + (size & 1)
    else:
        if bytes(buf[a:a + 4]) != b"LIST" or bytes(buf[a + 8:a + 12]) != b"movi":
            return None
        a += 12                                      # the frame list's own length was never filled in either
    frames = 0
    while a + 8 <= end:
        name = bytes(buf[a:a + 4])
        (size,) = struct.unpack("<I", bytes(buf[a + 4:a + 8]))
        if not _frame_name(name) or size > 64 << 20 or a + 8 + size > end:
            break
        if name == b"LIST":
            size = 4                                 # step into a group of frames
        a += 8 + size + (size & 1)
        frames += 1
    return a - pos if frames >= 8 else None


def ts_shape(buf, pos: int, end: int) -> tuple[int, int] | None:
    """(packet length, where in each packet its marker byte is) when a transport stream starts at pos."""
    for step, off in ((188, 0), (192, 4)):
        if pos + off + 4 * step < end and all(buf[pos + off + k * step] == 0x47 for k in range(5)):
            return step, off
    return None


def ts_run(buf, pos: int, end: int) -> tuple[int, int, int]:
    """(bytes, packet length, marker place) of the unbroken run of packets starting at pos; (0, 0, 0) if none."""
    shape = ts_shape(buf, pos, end)
    if shape is None:
        return 0, 0, 0
    step, off = shape
    good, per = 0, 1 << 16                           # packets per look
    whole = (end - pos) // step
    while good < whole:
        k = min(per, whole - good)
        a = np.frombuffer(buf, np.uint8, k * step, pos + good * step)
        bad = np.flatnonzero(a[off::step] != 0x47)
        if len(bad):
            good += int(bad[0])
            break
        good += k
    return good * step, step, off


def ts_jump(buf, pos: int, nbytes: int, step: int, off: int) -> int | None:
    """Every packet carries a count (0 to 15) that goes up by one within its own strand of the
    recording. Returns the number of the first packet whose count does not follow on, which is
    where a different recording has been written over this one (or a glitch); None if there is none."""
    last: dict[int, int] = {}
    total, per = nbytes // step, 1 << 16
    for p0 in range(0, total, per):
        k = min(per, total - p0)
        a = np.frombuffer(buf, np.uint8, k * step, pos + p0 * step).reshape(k, step)[:, off:off + 6].astype(np.int64)
        pid = (a[:, 1] & 0x1F) << 8 | a[:, 2]
        cc = a[:, 3] & 15
        excused = ((a[:, 3] & 0x20) != 0) & (a[:, 4] > 0) & ((a[:, 5] & 0x80) != 0)      # the camera says: count restarts here
        use = np.flatnonzero(((a[:, 3] & 0x10) != 0) & (pid != 0x1FFF))
        if not len(use):
            continue
        order = use[np.argsort(pid[use], kind="stable")]
        same = pid[order][1:] == pid[order][:-1]
        step_ok = ((cc[order][1:] - cc[order][:-1]) & 15) <= 1
        bad = order[1:][same & ~step_ok & ~excused[order][1:]]
        first = int(bad.min()) if len(bad) else None
        heads = order[np.concatenate(([True], ~same))]            # each strand's first packet in this stretch
        for i in heads.tolist():
            was = last.get(int(pid[i]))
            if was is not None and ((int(cc[i]) - was) & 15) > 1 and not excused[i] and (first is None or i < first):
                first = i
        if first is not None:
            return p0 + first
        tails = order[np.concatenate((~same, [True]))]
        for i in tails.tolist():
            last[int(pid[i])] = int(cc[i])
    return None


def ts_begins(buf, pos: int, step: int, off: int) -> bool:
    """Does the stream start the way a recording starts: with its table of contents (packet id 0)?"""
    b = bytes(buf[pos + off + 1:pos + off + 3])
    return len(b) == 2 and ((b[0] & 0x1F) << 8 | b[1]) == 0


def check_ts(data, ext: str = "mts") -> tuple[str, str] | str | None:
    n = len(data)
    run, step, _off = ts_run(data, 0, n)
    if not run:
        # ".ts" is also what some program source files are called: not ours to judge
        return "unverified" if ext == "ts" else None
    if run < n - step:
        return None
    jump = ts_jump(data, 0, run, step, _off)
    if jump is not None:
        # either a glitch in the recording or the place where a later recording was written over
        # this one: a file is not thrown away on that doubt, but the doubt is said
        return "unverified", (f"its packets line up from start to end, but their running count jumps about "
                              f"{100 * jump * step // max(1, n)}% of the way through. That is either a glitch in the "
                              "recording or the point where a different recording takes over, so play it past there")
    return "unverified", (f"all {run // step} of its packets follow one another from start to end; this kind of video "
                          "carries no further check, so play it through to make sure")
