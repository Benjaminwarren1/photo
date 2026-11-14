"""Filling the missing part of a partial photo from its own preview."""
import io

import numpy as np
from PIL import Image

from photorecover.output import preview_fill
from photorecover.synth.photos import make_photo


def test_missing_part_is_filled_from_the_preview():
    whole = make_photo(4242)
    want = np.asarray(Image.open(io.BytesIO(whole)).convert("RGB"), np.float32)
    cut = whole[:int(len(whole) * 0.5)]                      # the second half of the file is gone
    filled = preview_fill(cut, 40.0)                         # the rebuilder vouches for the first 40%
    got = np.asarray(Image.open(io.BytesIO(filled)).convert("RGB"), np.float32)
    assert got.shape == want.shape
    h = want.shape[0]
    assert np.abs(got[:int(h * 0.3)] - want[:int(h * 0.3)]).mean() < 4       # the real part is kept as it is
    blank = np.abs(128 - want[int(h * 0.6):]).mean()
    assert np.abs(got[int(h * 0.6):] - want[int(h * 0.6):]).mean() < 0.5 * blank   # the rest resembles the photo


def test_no_preview_means_no_filled_copy():
    whole = make_photo(4243, preview=False)
    assert preview_fill(whole[:len(whole) // 2], 40.0) is None
