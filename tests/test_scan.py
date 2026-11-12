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


def test_flat_colour_is_not_mistaken_for_photo():
    """Stray data can decode as 'more of the last colour'. Where the picture has detail,
    that must never count as a match, however close the colour is."""
    import numpy as np
    from photorecover.rebuild import NOT_A_PHOTO, Judge
    photo = make_photo(78, 640, 480)
    info = jpeg.parse_header(photo)
    st = jpeg.Stream(info)
    st.feed(photo[info.data_off:])
    judge = Judge(info, jpeg.decode_thumb(info))
    m0, n = info.mcus_x * 5, 64
    real = st.dc[m0:m0 + n]
    flat = np.repeat(real[:1], n, axis=0)
    flat[:, :judge.ny] = real[0, 0]          # every block the same: what data with no changes decodes to
    assert judge.preview_score(real, m0) < 20
    assert judge.preview_score(flat, m0) == NOT_A_PHOTO
    both = judge.screen_scores(st.dc, np.stack([real, flat]), m0)
    assert both[0] == pytest.approx(judge.preview_score(real, m0), rel=1e-3) and both[1] == NOT_A_PHOTO


def test_quick_folder_filter_never_disagrees_with_the_folder_parser():
    """The sweep for lost folders rules most blocks out in bulk. That shortcut must say
    exactly what the real parser says about a block's first slot."""
    import numpy as np
    from photorecover.fat import FatVolume, _slot_ok
    rng = np.random.default_rng(3)
    good = bytearray(b"IMG_0001JPG\x20" + bytes(20))
    slots = []
    for i in range(6000):
        s = bytearray(good) if i % 3 else bytearray(rng.bytes(32))
        if i % 3 == 1:
            s[int(rng.integers(0, 12))] = int(rng.integers(0, 256))
        if i % 3 == 2:
            s[11] = 0x0F
            s[12] = int(rng.integers(0, 2)); s[26] = int(rng.integers(0, 2)); s[27] = 0
        if s[0] == 0:
            s[0] = 0x41
        slots.append(bytes(s))
    vol = FatVolume.__new__(FatVolume)
    vol.fat_bits = 16
    want = [vol.parse_dir(s, 0)[2] == 0 for s in slots]
    got = _slot_ok(np.frombuffer(b"".join(slots), np.uint8).reshape(-1, 32)).tolist()
    assert got == want
    assert 500 < sum(want) < 5500


@pytest.mark.parametrize("kw", [dict(), dict(restart_interval=2), dict(quality=97)])
def test_c_matches_python_fed_in_pieces(kw):
    """The same answer, and the same resume point, however the data is cut up and damaged:
    markers and runs of FF dropped in, bits flipped, pieces as small as one byte."""
    data = make_photo(8, 160, 120, **kw)
    info = jpeg.parse_header(data)
    scan = data[info.data_off:]
    st0 = jpeg.STATE.pack(0, 0, 0, 0, 0, 0, 0, 0, info.restart, 0)
    rnd = random.Random(2)
    for trial in range(40):
        d = bytearray(scan)
        p = rnd.randrange(len(d))
        if trial % 4 == 1:
            d[p] ^= 1 << rnd.randrange(8)
        elif trial % 4 == 2:
            d[p:p + rnd.randrange(1, 6)] = b"\xff" * 5
        elif trial % 4 == 3:
            d[p:p + 2] = bytes([0xFF, rnd.choice([0, 0xD0, 0xD3, 0xD9, 0xC4])])
        state, buf, pos = st0, b"", 0
        while True:
            take = rnd.choice([1, 2, 3, 7, 64, 512, len(d)])
            buf += bytes(d[pos:pos + take])
            pos += take
            mx = rnd.choice([-1, -1, 0, 1, 5])
            got = _scan.decode(buf, state, info.spec, mx)
            assert got == _scan_py.decode(buf, state, info.spec, mx)
            if got[0] != jpeg.ST_NEED_DATA or (pos >= len(d) and mx == -1):
                break
            s = list(jpeg.STATE.unpack(got[3]))
            buf, s[0] = buf[s[0]:], 0
            state = jpeg.STATE.pack(*s)
