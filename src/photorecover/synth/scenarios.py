"""Ready-made test cards: a camera's worth of photos written, partly deleted and
rewritten so that later photos fragment into the holes, then all deleted."""
from __future__ import annotations

import numpy as np

from .exfatbuilder import ExFatBuilder
from .fatbuilder import FatBuilder
from .photos import make_photo


def used_card(fat_type=16, cluster_size: int = 16384, size_mb: int = 128, seed: int = 1,
              photos: int = 40, width: int = 1600, height: int = 1200, preview: bool = True,
              zero_high: bool = False, rounds: int = 4, base_cluster: int = 2, clear_fat: bool = False,
              **photo_kw):
    rng = np.random.default_rng(seed)
    if fat_type == "exfat":
        b = ExFatBuilder(size_mb, cluster_size, clear_fat_on_delete=clear_fat)
        base_cluster = max(base_cluster, b.next_free)
    else:
        b = FatBuilder(size_mb, fat_type, cluster_size, zero_high_on_delete=zero_high)
    pics = [make_photo(seed * 1000 + i, width, height, preview=preview, dark=(i % 7 == 3), **photo_kw)
            for i in range(photos)]
    k = 0
    b.mkdir("DCIM/100MEDIA")
    b.next_free = max(b.next_free, base_cluster)   # start partway into the card if asked
    per = photos // rounds
    for rnd in range(rounds):
        names = []
        for _ in range(per):
            name = f"DCIM/100MEDIA/IMG_{k:04d}.JPG"
            b.write(name, pics[k])
            names.append(name)
            k += 1
        if rnd < rounds - 1:
            for name in rng.choice(names, size=len(names) // 3, replace=False):
                b.delete(str(name))
            b.next_free = base_cluster      # the next photos start filling the holes
    return b
