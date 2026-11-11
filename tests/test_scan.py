"""The C decoder and its pure-Python twin must agree exactly, on good and damaged data."""
import random

import pytest

from photorecover import _scan_py, jpeg
from photorecover.synth.photos import make_photo

_scan = pytest.importorskip("photorecover._scan")


@pytest.mark.parametrize("kw", [dict(), dict(restart_interval=3), dict(subsampling=0), dict(subsampling=1)])
def test_c_matches_python(kw):
    data = make_photo(5, 320, 240, **kw)
    info = jpeg.parse_header(data)
    scan = bytearray(data[info.data_off:])
    st0 = jpeg.STATE.pack(0, 0, 0, 0, 0, 0, 0, 0, info.restart, 0)
    rnd = random.Random(1)
    for trial in range(25):
        d = bytearray(scan)
        if trial:
            p = rnd.randrange(len(d))
            d[p:p + 64] = bytes(rnd.randrange(256) for _ in range(64))
        cut = len(d) if trial % 3 == 0 else rnd.randrange(200, len(d))
        assert _scan.decode(bytes(d[:cut]), st0, info.spec, -1) == _scan_py.decode(bytes(d[:cut]), st0, info.spec, -1)


def test_resume_in_pieces_equals_one_pass():
    data = make_photo(6, 640, 480)
    info = jpeg.parse_header(data)
    whole = jpeg.Stream(info)
    whole.feed(data[info.data_off:])
    parts = jpeg.Stream(info)
    for o in range(info.data_off, len(data), 777):
        parts.feed(data[o:o + 777])
    assert whole.status == parts.status == jpeg.ST_DONE
    assert (whole.dc == parts.dc).all()
    assert info.data_off + parts.end_offset() == len(data)


def test_quick_look_at_candidates_agrees_with_full_decode():
    """The fast first look used in whole-card searches must keep exactly the candidates the
    full decoder accepts, with the same values, in C and in Python."""
    import numpy as np
    photo = make_photo(77, 640, 480)
    info = jpeg.parse_header(photo)
    cl = 2048
    body = photo[info.data_off:]
    st = jpeg.Stream(info)
    st.feed(body[:cl])
    rng = random.Random(3)
    junk = bytes(rng.randrange(256) for _ in range(cl * 6))
    image = body[cl:cl * 9] + junk                      # eight real pieces (one the true next), six of noise
    clusters = np.arange(2, 2 + len(image) // cl, dtype=np.int64)
    full, dc, short = st.screen_many(image, 0, cl, clusters, 16)
    by_full = {c: d for c, status, d in st.probe_many(image, 0, cl, clusters, False)}
    assert 2 in full and len(full) + len(short) <= len(clusters)
    for c, d in zip(full, dc):
        assert (by_full[int(c)][:16] == d).all()
    from photorecover.jpeg import STATE
    s0 = STATE.pack(0, *STATE.unpack(st.state)[1:])
    py = _scan_py.screen_many(image, 0, cl, clusters.astype("<i8").tobytes(), st.carry, s0, info.spec, 16)
    assert py[0] == full.astype("<i8").tobytes() and py[1] == dc.tobytes() and py[2] == short.astype("<i8").tobytes()
