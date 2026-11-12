"""End-to-end tests on synthetic cards where every right answer is known.

The rule that matters most: nothing wrong may ever land in Recovered.
"""
import hashlib
from pathlib import Path

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
    assert rec.summary["old_layout"] is None, "an ordinary card was mistaken for a reformatted one"
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


def test_progressive_photos_whole_only_and_never_wrong(tmp_path):
    """Progressive JPEGs cannot be rebuilt yet. Whole ones come back; a stale entry whose
    space now holds a different photo must not be delivered."""
    b = used_card(fat_type=16, cluster_size=16384, size_mb=128, seed=42, progressive=True, photos=20)
    b.delete_all()
    rec = run(tmp_path, b)
    good, missed = check(rec, b, tmp_path, expect_all_recoverable=False)
    assert good >= sum(t.recoverable and not t.fragmented for t in b.history)


def test_close_calls_can_be_decided_by_a_person(tmp_path):
    """When the tool will not decide a join itself, a person chooses from pictures. Choosing
    the right piece each time must finish the photo exactly, and the pictures must be real."""
    from photorecover.recover import GRADE_REVIEW
    b = used_card(**CARDS["fat16-16k"])
    b.delete_all()
    rec = run(tmp_path, b)
    truth = {(t.clusters[0], t.size): t for t in b.history}
    # Put fragmented photos back into the undecided state: every cluster free again, and the
    # rebuilder told to leave each join to a person.
    rec._avail[:] = ~rec.vol.allocated() & ~rec.vol.blank_clusters()
    rec._rb.always_ask = True
    from photorecover.cli import pilot_command
    finished = piloted = 0
    for idx, it in enumerate(rec.items):
        t = truth.get((it.start, it.size))
        if not (t and t.recoverable and t.fragmented):
            continue
        r = rec._rb.rebuild(it.start, it.size)
        assert r.status == "review" and r.candidates
        (tmp_path / "out" / it.saved_as).unlink()
        it._last, it.grade, it.candidates, it.chain, it.percent, it.saved_as = r, GRADE_REVIEW, r.candidates, r.chain, r.percent, ""
        assert idx in rec.review_queue()
        for _ in range(12):
            if it.grade != GRADE_REVIEW:
                break
            shown = rec.review_images(idx)
            assert shown["candidates"] and all(c["image"][:2] == b"\xff\xd8" for c in shown["candidates"])
            assert shown["preview"]
            want = t.clusters[len(it.chain)]
            assert want in [c for _, c in it.candidates], "the right piece was not among those offered"
            if finished % 2:        # every other photo is decided through the JSON commands instead
                out = tmp_path / "out"
                assert idx in [u["index"] for u in pilot_command(rec, out, dict(cmd="unresolved"))["unresolved"]]
                offered = pilot_command(rec, out, dict(cmd="candidates", index=idx))["candidates"]
                assert want in [c["cluster"] for c in offered] and all(Path(c["image"]).is_file() for c in offered)
                assert "error" in pilot_command(rec, out, dict(cmd="pick", index=10 ** 6, cluster=want))
                pilot_command(rec, out, dict(cmd="pick", index=idx, cluster=want))
                piloted += 1
            else:
                rec.review_pick(idx, want)
        assert it.grade == "Verified" and it.saved_as.startswith("Recovered/")
        assert hashlib.sha256((tmp_path / "out" / it.saved_as).read_bytes()).hexdigest() == t.sha256
        assert any(p.get("how") == "chosen by a person" for p in it.picks)
        finished += 1
    assert finished >= 3 and piloted
    assert "chosen by eye" in (tmp_path / "out" / "report.html").read_text()


def _reformat(old, fat_type, cluster_size):
    """What formatting a used card with other settings does: a new, empty file system is
    written over the front of the card, and everything further in is left as it was."""
    from photorecover.synth.fatbuilder import FatBuilder
    new = FatBuilder(old.size >> 20, fat_type, cluster_size)
    img = bytearray(old.tobytes())
    front = new.data_off + (new.cl if fat_type == 32 else 0)
    img[:front] = new.tobytes()[:front]
    return bytes(img), front


@pytest.mark.parametrize("deleted_first,folders_survive", [(False, True), (True, True), (True, False)])
def test_card_reformatted_with_different_settings(tmp_path, deleted_first, folders_survive):
    """The old folders survive a format, but their addresses are in the old layout. The
    tool has to work that layout out, or every address lands on the wrong bytes."""
    old = used_card(fat_type=16, cluster_size=16384, size_mb=128, base_cluster=40, folder_at_base=folders_survive)
    if deleted_first:
        old.delete_all()
    data, front = _reformat(old, 32, 4096)
    img = tmp_path / "card.img"
    img.write_bytes(data)
    rec = Recovery(img, tmp_path / "out")
    rec.run()
    lay = rec.summary["old_layout"]
    if folders_survive:
        assert lay == dict(cluster_size=16384, data_offset=old.data_off)
    else:       # the format wiped the folders: only how the photos line up is left to go on
        assert lay["cluster_size"] == 16384 and (lay["data_offset"] - old.data_off) % 16384 == 0
    assert hashlib.sha256(img.read_bytes()).hexdigest() == hashlib.sha256(data).hexdigest()
    good = {t.sha256 for t in old.history}
    wrong = [p for p in (tmp_path / "out" / FOLDER_OK).rglob("*") if p.is_file()
             and hashlib.sha256(p.read_bytes()).hexdigest() not in good]
    assert not wrong, f"delivered files that are not on the card: {wrong[:3]}"
    intact = [t for t in old.history if not t.overwritten and old.cluster_off(min(t.clusters)) >= front]
    got = {hashlib.sha256(p.read_bytes()).hexdigest() for p in (tmp_path / "out" / FOLDER_OK).rglob("*") if p.is_file()}
    found = sum(t.sha256 in got for t in intact)
    assert found >= 0.9 * len(intact) and found >= 10, f"{found} of {len(intact)}"



def test_photo_saved_twice_is_not_a_close_call(tmp_path):
    """Two copies of one photo put identical bytes in two places. Either copy is a correct
    continuation, so that must not be sent to a person as if it were a hard choice."""
    from photorecover.synth.fatbuilder import FatBuilder
    from photorecover.synth.photos import make_photo
    b = FatBuilder(64, 16, 8192)
    b.mkdir("DCIM")
    twin = make_photo(555, 1600, 1200)
    for i in range(20):
        b.write(f"DCIM/A{i}.JPG", make_photo(600 + i % 10, 640, 480))
    for i in range(1, 20, 2):
        b.delete(f"DCIM/A{i}.JPG")
    b.next_free = 2
    t1 = b.write("DCIM/TWIN1.JPG", twin)     # both land in the holes, so both are stored in pieces
    t2 = b.write("DCIM/TWIN2.JPG", twin)
    pieces = lambda t: 1 + sum(b != a + 1 for a, b in zip(t.clusters, t.clusters[1:]))
    assert pieces(t1) >= 3 and pieces(t2) >= 2
    b.delete_all()
    for i in range(0, 20, 2):       # the photos in between lost their first block long ago, so they
        o = b.cluster_off(b.truth[f"DCIM/A{i}.JPG"].clusters[0])     # cannot be recognised and set aside
        b.img[o:o + b.cl] = bytes([7]) * b.cl
    rec = run(tmp_path, b)
    twins = [it for it in rec.items if it.size == len(twin)]
    assert len(twins) == 2 and all(it.grade == "Verified" for it in twins), [(it.name, it.grade, it.note) for it in twins]
    for it in twins:
        assert (tmp_path / "out" / it.saved_as).read_bytes() == twin


@pytest.mark.parametrize("folders_survive", [True, False])
def test_drive_with_a_format_the_tool_cannot_read_is_searched_anyway(tmp_path, folders_survive):
    """A drive reformatted as NTFS (or with a damaged start) has no folders this tool can
    read. It must still find the photos, and still deliver nothing wrong."""
    old = used_card(fat_type=16, cluster_size=16384, size_mb=128, base_cluster=40, folder_at_base=True)
    img_bytes = bytearray(old.tobytes())
    front = old.data_off if folders_survive else old.cluster_off(40) + 16 * old.cl
    img_bytes[:front] = bytes(front)
    img_bytes[0:11] = b"\xebR\x90NTFS    "
    img_bytes[510:512] = b"\x55\xaa"
    img = tmp_path / "card.img"
    img.write_bytes(img_bytes)
    rec = Recovery(img, tmp_path / "out")
    rec.run()
    assert "NTFS" in rec.summary["unreadable"]
    assert rec.summary["cluster_size"] == 16384
    assert "searched for photos" in (tmp_path / "out" / "report.html").read_text()
    good = {t.sha256 for t in old.history}
    files = [p for p in (tmp_path / "out" / FOLDER_OK).rglob("*") if p.is_file()]
    wrong = [p for p in files if hashlib.sha256(p.read_bytes()).hexdigest() not in good]
    assert not wrong, f"delivered files that are not on the card: {wrong[:3]}"
    got = {hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    intact = [t for t in old.history if not t.overwritten and old.cluster_off(min(t.clusters)) >= front]
    found = sum(t.sha256 in got for t in intact)
    assert found >= 0.9 * len(intact) and found >= 10, f"{found} of {len(intact)}"
    if folders_survive:
        assert any(p.name.startswith("IMG_") for p in files), "names from the old folders were not used"


def test_final_check_fails_a_photo_whose_larger_part_is_another_picture():
    """Seen on a real card: the top 40% of one photo joined to data that decoded cleanly to
    the end but showed something else. With most of the picture wrong, the wrong part must
    not become the yardstick."""
    import io
    from PIL import Image
    from photorecover.recover import audit_jpeg, first_bad_band
    from photorecover.synth.photos import _exif_with_thumbnail, draw_scene
    real, other = draw_scene(71, 1200, 1600), draw_scene(72, 1200, 1600, dark=True)
    th = real.copy()
    th.thumbnail((120, 160), Image.BILINEAR)
    tb = io.BytesIO()
    th.save(tb, "JPEG", quality=75)

    def photo(im):
        out = io.BytesIO()
        im.save(out, "JPEG", quality=88, exif=_exif_with_thumbnail(tb.getvalue()))
        return out.getvalue()

    assert audit_jpeg(photo(real))[0]
    mixed = real.copy()
    mixed.paste(other.crop((0, 640, 1200, 1600)), (0, 640))
    ok, note, _ = audit_jpeg(photo(mixed))
    assert not ok and "preview" in note
    assert first_bad_band([2.0] * 9 + [30.0] * 15) == 9
    assert first_bad_band([1.0] * 6 + [5.0] * 18) is None      # plain sky above busy ground is one picture


@pytest.mark.parametrize("new_photos", [10, 60])
def test_card_reformatted_then_used_again_gives_back_both_layers(tmp_path, new_photos):
    """Seen on a real card: formatted with new settings, used again, and those newer photos
    deleted. The newer photos are listed in the format the card has now; the older ones can only
    be read the way the card used to be laid out. Both must come back, whichever layer is bigger."""
    from photorecover.synth.fatbuilder import FatBuilder
    from photorecover.synth.photos import make_photo
    old = used_card(fat_type=16, cluster_size=16384, size_mb=128, base_cluster=1500, folder_at_base=True)
    old.delete_all()
    new = FatBuilder(old.size >> 20, 32, 4096)
    front = new.data_off + new.cl
    new.img[front:] = old.tobytes()[front:]
    new.mkdir("DCIM/Camera")
    for k in range(new_photos):
        new.write(f"DCIM/Camera/PET_{k:03d}.JPG", make_photo(7000 + k, 800, 600))
    new.delete_all()
    used_to = max(new.cluster_off(max(t.clusters)) for t in new.history) + new.cl
    img = tmp_path / "card.img"
    new.save(img)
    rec = Recovery(img, tmp_path / "out")
    rec.run()
    assert rec.summary["old_layout"] == dict(cluster_size=16384, data_offset=old.data_off)
    assert rec.summary["since_format"] >= new_photos
    files = [p for p in (tmp_path / "out" / FOLDER_OK).rglob("*") if p.is_file()]
    got = {hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    good = {t.sha256 for t in old.history} | {t.sha256 for t in new.history}
    assert not [p for p in files if hashlib.sha256(p.read_bytes()).hexdigest() not in good], "something wrong was delivered"
    assert all(t.sha256 in got for t in new.history), "photos deleted since the format did not all come back"
    intact = [t for t in old.history if not t.overwritten and old.cluster_off(min(t.clusters)) >= used_to]
    found = sum(t.sha256 in got for t in intact)
    assert found >= 0.9 * len(intact) and found >= 10, f"{found} of {len(intact)} older photos"
    assert any("ET_0" in p.name for p in files), "the newer photos lost their names"


def test_overwritten_photo_is_not_saved_as_part_of_the_photo_that_replaced_it(tmp_path):
    """Seen on a real card: a deleted photo's space was reused by a later, bigger photo that begins
    at the same place. Its folder entry survives, but what is there now is the later photo."""
    from photorecover.synth.fatbuilder import FatBuilder
    from photorecover.synth.photos import make_photo
    b = FatBuilder(64, 16, 4096)
    b.mkdir("DCIM")
    b.write("DCIM/KEEP_0.JPG", make_photo(80, 800, 600))
    first = b.next_free
    gone = b.write("DCIM/GONE.JPG", make_photo(81, 1024, 768))
    b.delete("DCIM/GONE.JPG")
    b.next_free = first
    later = b.write("DCIM/LATER.JPG", make_photo(82, 1600, 1200))
    assert later.clusters[0] == gone.clusters[0] and len(later.clusters) > len(gone.clusters)
    b.delete_all()
    rec = run(tmp_path, b)
    check(rec, b, tmp_path, expect_all_recoverable=False)
    it = next(i for i in rec.items if i.size == gone.size)
    assert it.grade == "Not recoverable" and not it.saved_as, (it.grade, it.note, it.saved_as)
    assert "different file" in it.note
    assert not list((tmp_path / "out").glob("Recovered - Partial/**/*ONE*"))
