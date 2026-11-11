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
