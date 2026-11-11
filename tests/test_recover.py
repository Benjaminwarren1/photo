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
    "exfat-32k": dict(fat_type="exfat", cluster_size=32768, size_mb=160, seed=21, width=2048, height=1536),
    "exfat-128k-chain-wiped": dict(fat_type="exfat", cluster_size=131072, size_mb=256, seed=22, width=2592,
                                   height=1944, photos=24, clear_fat=True),
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


@pytest.mark.parametrize("fat_type", [16, 32, "exfat"])
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


def test_exfat_fragmented_video_follows_surviving_chain(tmp_path):
    """exFAT keeps a deleted file's chain, so even a fragmented video comes back whole."""
    import os
    import struct
    from photorecover.synth.exfatbuilder import ExFatBuilder
    from photorecover.synth.photos import make_photo
    b = ExFatBuilder(64, 32768)
    for i in range(4):
        b.write(f"DCIM/A{i}.JPG", make_photo(900 + i, 800, 600))
    b.delete("DCIM/A1.JPG")
    b.next_free = 2
    body = os.urandom(400_000)
    video = struct.pack(">I4s4sI4s", 20, b"ftyp", b"isom", 0, b"isom") + struct.pack(">I4s", 8 + len(body), b"mdat") \
        + body + struct.pack(">I4s", 16, b"moov") + bytes(8)
    t = b.write("DCIM/CLIP.MP4", video)
    assert t.fragmented
    b.delete_all()
    rec = run(tmp_path, b)
    it = next(i for i in rec.items if i.name == "CLIP.MP4")
    assert it.grade == "Verified"
    assert (tmp_path / "out" / it.saved_as).read_bytes() == video


@pytest.mark.parametrize("moov_first", [False, True])
def test_fragmented_videos_on_fat(tmp_path, moov_first):
    """FAT wipes a deleted file's chain, so videos are rebuilt from their own index."""
    from photorecover.synth.fatbuilder import FatBuilder
    from photorecover.synth.photos import make_photo
    from photorecover.synth.videos import make_video
    b = FatBuilder(96, 16, 16384)
    for i in range(16):
        b.write(f"DCIM/IMG_{i:04d}.JPG", make_photo(700 + i, 1024, 768))
    for i in range(1, 16, 2):
        b.delete(f"DCIM/IMG_{i:04d}.JPG")
    b.next_free = 2
    clips = {f"DCIM/VID_{i:04d}.MP4": make_video(40 + i, frames=90 + 30 * i, moov_first=moov_first) for i in range(3)}
    assert sum(b.write(name, data).fragmented for name, data in clips.items()) >= 2
    b.delete_all()
    rec = run(tmp_path, b)
    check(rec, b, tmp_path, expect_all_recoverable=False)
    for name, data in clips.items():
        it = next(i for i in rec.items if i.size == len(data))
        assert it.grade == "Verified", (it.name, it.grade, it.note)
        assert (tmp_path / "out" / it.saved_as).read_bytes() == data


def test_video_with_sparse_checkpoints_is_never_wrongly_verified(tmp_path):
    """Big frames and no sound leave clusters with nothing to check. The tool may then be
    unsure where two pieces join, but it must say so rather than call the video verified."""
    from photorecover.synth.fatbuilder import FatBuilder
    from photorecover.synth.photos import make_photo
    from photorecover.synth.videos import make_video
    b = FatBuilder(96, 16, 4096)
    for i in range(16):
        b.write(f"DCIM/IMG_{i:04d}.JPG", make_photo(800 + i, 1024, 768))
    for i in range(1, 16, 2):
        b.delete(f"DCIM/IMG_{i:04d}.JPG")
    b.next_free = 2
    clips = [make_video(60 + i, frames=60, mean_frame=30000, audio=False) for i in range(2)]
    for i, data in enumerate(clips):
        b.write(f"DCIM/VID_{i}.MP4", data)
    b.delete_all()
    rec = run(tmp_path, b)
    for data in clips:
        it = next(i for i in rec.items if i.size == len(data))
        if it.grade == "Verified":
            assert (tmp_path / "out" / it.saved_as).read_bytes() == data
