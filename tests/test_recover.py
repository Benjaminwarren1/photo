"""End-to-end tests on synthetic cards where every right answer is known.

The rule that matters most: nothing wrong may ever land in Recovered.
"""
import hashlib

import pytest

from photorecover.recover import FOLDER_OK, Recovery
from photorecover.synth.scenarios import used_card

CARDS = {
    "fat16-4k": dict(fat_type=16, cluster_size=4096, size_mb=64, seed=1),
    "fat16-16k": dict(fat_type=16, cluster_size=16384, size_mb=128, seed=2),
    "fat16-32k-5mp": dict(fat_type=16, cluster_size=32768, size_mb=256, seed=3, width=2592, height=1944, photos=28),
    "fat32-4k-highword-zeroed": dict(fat_type=32, cluster_size=4096, size_mb=64, seed=4, zero_high=True),
    "fat32-big-highword-zeroed": dict(fat_type=32, cluster_size=512, size_mb=72, seed=9, zero_high=True,
                                      photos=16, width=800, height=600, base_cluster=70000),
    "restart-markers": dict(fat_type=16, cluster_size=8192, size_mb=64, seed=6, restart_interval=8),
    "no-chroma-subsampling": dict(fat_type=16, cluster_size=8192, size_mb=128, seed=7, subsampling=0, photos=24),
    "letterboxed-preview": dict(fat_type=16, cluster_size=16384, size_mb=128, seed=8, width=1920, height=1080,
                                letterbox=True),
    "no-preview": dict(fat_type=16, cluster_size=8192, size_mb=64, seed=5, preview=False),
}


def run(tmp_path, builder):
    img = tmp_path / "card.img"
    builder.save(img)
    before = hashlib.sha256(img.read_bytes()).hexdigest()
    rec = Recovery(img, tmp_path / "out")
    rec.run()
    assert hashlib.sha256(img.read_bytes()).hexdigest() == before, "the card image was modified"
    return rec


def check(rec, builder, tmp_path, expect_all_recoverable=True):
    truth = {(t.clusters[0], t.size): t for t in builder.history}
    wrong, missed, good = [], [], 0
    for it in rec.items:
        t = truth.get((it.start, it.size))
        if it.saved_as.startswith(FOLDER_OK + "/"):
            data = (tmp_path / "out" / it.saved_as).read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            if t is None:
                t = next((x for x in builder.history if x.sha256 == sha), None)
            if t is None or sha != t.sha256:
                wrong.append(it.name)
            else:
                good += 1
        elif t is not None and t.recoverable:
            missed.append((it.name, it.grade, it.note))
    assert not wrong, f"wrong files delivered as recovered: {wrong}"
    if expect_all_recoverable:
        assert not missed, f"recoverable files not recovered: {missed}"
    return good, missed


@pytest.mark.parametrize("name", [n for n in CARDS if n != "no-preview"])
def test_deleted_card(tmp_path, name):
    b = used_card(**CARDS[name])
    assert any(t.fragmented and t.recoverable for t in b.history), "scenario should include fragmented photos"
    b.delete_all()
    rec = run(tmp_path, b)
    if name == "fat32-big-highword-zeroed":
        # 512-byte clusters can split a photo inside its header, which is not rebuilt yet.
        # This card is here for start clusters above 65535 whose high half was zeroed.
        good, missed = check(rec, b, tmp_path, expect_all_recoverable=False)
        assert all("header" in note for _, _, note in missed) and len(missed) <= 2
        assert any(it.start > 65535 and it.saved_as for it in rec.items)
        return
    good, _ = check(rec, b, tmp_path)
    assert good == sum(t.recoverable for t in b.history)


def test_no_preview_card_never_guesses(tmp_path):
    """Without a preview, whole photos come back; fragmented ones may need a person,
    but must never be delivered wrong."""
    b = used_card(**CARDS["no-preview"])
    b.delete_all()
    rec = run(tmp_path, b)
    good, missed = check(rec, b, tmp_path, expect_all_recoverable=False)
    whole = sum(t.recoverable and not t.fragmented for t in b.history)
    assert good >= whole
    assert all(g == "Needs review" for _, g, _ in missed)


@pytest.mark.parametrize("fat_type", [16, 32])
def test_quick_formatted_card(tmp_path, fat_type):
    b = used_card(fat_type=fat_type, cluster_size=8192, size_mb=96, seed=11)
    b.quick_format()
    rec = run(tmp_path, b)
    good, _ = check(rec, b, tmp_path)
    assert good == sum(t.recoverable for t in b.history)


def test_photos_with_no_folder_entry_are_found(tmp_path):
    """Wipe the folder itself: photos must still be found by scanning for their headers."""
    b = used_card(fat_type=16, cluster_size=8192, size_mb=96, seed=12, photos=24)
    b.delete_all()
    d = b._dirs["DCIM/100MEDIA"]
    for c in d.clusters:
        off = b.cluster_off(c)
        b.img[off:off + b.cl] = bytes(b.cl)
    rec = run(tmp_path, b)
    good, _ = check(rec, b, tmp_path, expect_all_recoverable=False)
    assert good == sum(t.recoverable for t in b.history)


def test_report_written(tmp_path):
    b = used_card(fat_type=16, cluster_size=16384, size_mb=64, seed=13, photos=12)
    b.delete_all()
    run(tmp_path, b)
    html = (tmp_path / "out" / "report.html").read_text()
    assert "Verified" in html and (tmp_path / "out" / "report.json").exists()
