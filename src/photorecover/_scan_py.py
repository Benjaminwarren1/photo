"""Pure-Python twin of _scan.c. Same inputs, same outputs, much slower.

Used when the C extension is not built, and by the tests to check the two agree.
"""
from __future__ import annotations

import struct

ST_NEED_DATA, ST_DONE, ST_ERROR = 0, 1, 2
E_NONE, E_HUFF, E_OVERRUN, E_SIZE, E_MARKER, E_RST, E_DCRANGE = range(7)

STATE = struct.Struct("<qii4iqii")
SPEC_HEAD = struct.Struct("<i16iiq")


class _Stop(Exception):
    def __init__(self, err: int):
        self.err = err


def _tables(raw: bytes):
    out = []
    for i in range(8):
        t = raw[i * 272:(i + 1) * 272]
        lut, code, k = {}, 0, 0
        for l in range(1, 17):
            for _ in range(t[l - 1]):
                lut[(l, code)] = t[16 + k]
                code += 1
                k += 1
            code <<= 1
        out.append(lut)
    return out


def decode(data, state: bytes, spec: bytes, max_mcus: int):
    head = SPEC_HEAD.unpack_from(spec, 0)
    ncomp = head[0]
    comps = [head[1 + 4 * i:5 + 4 * i] for i in range(ncomp)]   # nblocks, dc, ac, bound
    restart, total = head[17], head[18]
    tabs = _tables(spec[SPEC_HEAD.size:])
    pos, cur, left, p0, p1, p2, p3, mcu, to_restart, next_rst = STATE.unpack(state)
    pred = [p0, p1, p2, p3]
    d, end = data, len(data)
    out: list[int] = []
    done = 0
    todo = total - mcu if max_mcus < 0 else min(max_mcus, total - mcu)

    def bit():
        nonlocal pos, cur, left
        if left == 0:
            if pos >= end:
                raise _Stop(E_NONE)
            b = d[pos]
            if b == 0xFF:
                if pos + 1 >= end:
                    raise _Stop(E_NONE)
                if d[pos + 1] != 0:
                    raise _Stop(E_MARKER)
                pos += 2
            else:
                pos += 1
            cur, left = b, 8
        left -= 1
        return (cur >> left) & 1

    def bits(n):
        v = 0
        for _ in range(n):
            v = (v << 1) | bit()
        return v

    def huff(lut):
        code = 0
        for l in range(1, 17):
            code = (code << 1) | bit()
            s = lut.get((l, code))
            if s is not None:
                return s
        raise _Stop(E_HUFF)

    status, err = ST_NEED_DATA, E_NONE
    while True:
        good = (pos, cur, left, list(pred), mcu, to_restart, next_rst)
        if mcu >= total:
            status = ST_DONE
            break
        if done >= todo:
            break
        if restart > 0 and to_restart == 0:
            p = pos
            while p + 1 < end and d[p] == 0xFF and d[p + 1] == 0xFF:
                p += 1
            if p + 1 >= end:
                break
            if d[p] != 0xFF or d[p + 1] != (0xD0 | next_rst):
                status, err = ST_ERROR, E_RST
                break
            pos, left = p + 2, 0
            pred = [0, 0, 0, 0]
            next_rst = (next_rst + 1) & 7
            to_restart = restart
        vals: list[int] = []
        try:
            for ci, (nb, dt, at, bound) in enumerate(comps):
                for _ in range(nb):
                    s = huff(tabs[dt])
                    if s > 11:
                        raise _Stop(E_SIZE)
                    if s:
                        v = bits(s)
                        if v < (1 << (s - 1)):
                            v -= (1 << s) - 1
                        pred[ci] += v
                    if not -bound <= pred[ci] <= bound:
                        raise _Stop(E_DCRANGE)
                    vals.append(pred[ci])
                    k = 1
                    while k < 64:
                        rs = huff(tabs[4 + at])
                        run, sz = rs >> 4, rs & 15
                        if sz == 0:
                            if run != 15:
                                break
                            k += 16
                            if k > 63:
                                raise _Stop(E_OVERRUN)
                            continue
                        k += run
                        if k > 63:
                            raise _Stop(E_OVERRUN)
                        if sz > 10:
                            raise _Stop(E_SIZE)
                        bits(sz)
                        k += 1
        except _Stop as st:
            pos, cur, left, pred, mcu, to_restart, next_rst = good
            if st.err != E_NONE:
                status, err = ST_ERROR, st.err
            break
        out += vals
        mcu += 1
        if restart > 0:
            to_restart -= 1
        done += 1
    if status != ST_ERROR:
        pos, cur, left, pred, mcu, to_restart, next_rst = good
    return (status, done, struct.pack(f"<{len(out)}h", *out),
            STATE.pack(pos, cur, left, *pred, mcu, to_restart, next_rst), err)


def probe_many(image, data_off: int, cl: int, clusters: bytes, carry: bytes, state: bytes, spec: bytes, want: int):
    out = []
    st = STATE.pack(0, *STATE.unpack(state)[1:])
    for (c,) in struct.iter_unpack("<q", clusters):
        o = data_off + (c - 2) * cl
        if c < 2 or o + cl > len(image):
            continue
        status, n, dc, _, _ = decode(bytes(carry) + bytes(image[o:o + cl]), st, spec, -1)
        if status == ST_ERROR or (want == 1 and status != ST_DONE):
            continue
        out.append((c, status, n, dc))
    return out
