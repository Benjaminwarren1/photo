"""Drives the wizard's local API end to end with a fake card (an image file standing in for a drive)."""
import json
import time
import urllib.request

import pytest

from photorecover import device, wizard
from photorecover.synth.scenarios import used_card


def call(port, token, action, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/{action}",
                                 data=None if action == "state" else json.dumps(body or {}).encode(),
                                 headers={"X-Token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def test_clone_copies_exactly_and_reports_hash(tmp_path):
    import hashlib
    src = tmp_path / "card.bin"
    src.write_bytes(bytes(range(256)) * 5000)
    info = device.clone(str(src), tmp_path / "out" / "c.img")
    assert info["state"] == "done" and info["bad"] == []
    assert (tmp_path / "out" / "c.img").read_bytes() == src.read_bytes()
    assert info["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()


def test_wizard_card_to_recovered_folder(tmp_path, monkeypatch):
    drives = tmp_path / "drives"
    drives.mkdir()
    monkeypatch.setenv("PHOTORECOVER_FAKE_DRIVES", str(drives))
    session = wizard.Session()
    srv = wizard.make_server(session, "tok")
    port = srv.server_address[1]
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(Exception):
            call(port, "wrong", "begin_card")                      # no token, no access
        assert call(port, "tok", "begin_card")["step"] == "plug"
        assert call(port, "tok", "find_card")["step"] == "plug"    # nothing plugged in yet
        b = used_card(fat_type=16, cluster_size=16384, size_mb=64, seed=31, photos=12)
        b.delete_all()
        b.save(drives / "SDCARD.img")                              # "plug the card in"
        s = call(port, "tok", "find_card")
        assert s["step"] == "confirm" and s["drive"]["id"] == "SDCARD"
        out = tmp_path / "Photo recovery"
        s = call(port, "tok", "start_clone", {"folder": str(out)})
        assert s["step"] == "cloning", s
        for _ in range(240):
            s = call(port, "tok", "state")
            if s["step"] in ("done", "error"):
                break
            time.sleep(0.5)
        assert s["step"] == "done", s
        assert s["summary"]["counts"]["Verified"] == sum(t.recoverable for t in b.history)
        assert (out / "card.img").read_bytes() == (drives / "SDCARD.img").read_bytes()
        assert (out / "report.html").exists() and (out / "card.img.sha256.txt").exists()
    finally:
        srv.shutdown()


def test_wizard_review_api(tmp_path):
    """The wizard hands out close calls as pictures and applies the choice."""
    from photorecover.items import GRADE_REVIEW
    b = used_card(fat_type=16, cluster_size=16384, size_mb=128, seed=2)
    b.delete_all()
    img = tmp_path / "card.img"
    b.save(img)
    session = wizard.Session()
    session.use_image(str(img))
    for _ in range(240):
        if session.step in ("done", "error"):
            break
        time.sleep(0.25)
    assert session.step == "done" and session.state()["to_review"] == 0
    assert session.review_next() is None
    rec = session.rec
    truth = {(t.clusters[0], t.size): t for t in b.history}
    idx, it, t = next((i, it, truth[(it.start, it.size)]) for i, it in enumerate(rec.items)
                      if (it.start, it.size) in truth and truth[(it.start, it.size)].fragmented
                      and truth[(it.start, it.size)].recoverable)
    rec._avail[:] = ~rec.vol.allocated() & ~rec.vol.blank_clusters()
    rec._rb.always_ask = True
    r = rec._rb.rebuild(it.start, it.size)
    it._last, it.grade, it.candidates, it.chain, it.percent = r, GRADE_REVIEW, r.candidates, r.chain, r.percent
    for _ in range(12):
        nxt = session.review_next()
        if nxt is None:
            break
        assert nxt["index"] == idx and nxt["candidates"][0]["image"].startswith("data:image/jpeg;base64,")
        session.review_pick(idx, t.clusters[len(it.chain)])
    assert it.grade == "Verified" and session.state()["to_review"] == 0


def test_copy_destination_is_checked_before_copying(tmp_path):
    from photorecover import device
    big = 128 * 10**9
    assert device.dest_problem(tmp_path, big, free=200 * 10**9, fs="exFAT") is None
    assert "not enough room" in device.dest_problem(tmp_path, big, free=120 * 10**9, fs="exFAT")
    assert "4 GB" in device.dest_problem(tmp_path, big, free=200 * 10**9, fs="FAT32")
    assert device.dest_problem(tmp_path, 2 * 10**9, free=200 * 10**9, fs="FAT32") is None
    assert device.dest_problem(tmp_path / "not yet made", 1000) is None


@pytest.mark.parametrize("with_status", [False, True])
def test_copy_command_takes_a_drive_by_the_name_the_drive_list_shows(tmp_path, monkeypatch, with_status):
    import json
    import os
    from photorecover import cli
    drives = tmp_path / "drives"
    drives.mkdir()
    (drives / "CARD7.img").write_bytes(os.urandom(300_000))
    monkeypatch.setenv("PHOTORECOVER_FAKE_DRIVES", str(drives))
    dest, status = tmp_path / "out" / "card.img", tmp_path / "status.json"
    (tmp_path / "out").mkdir()
    assert cli.main(["clone", "card7", str(dest)] + (["--status", str(status)] if with_status else [])) == 0
    assert dest.read_bytes() == (drives / "CARD7.img").read_bytes()
    if with_status:
        assert json.loads(status.read_text())["state"] == "done"


def _faulty(tmp_path, monkeypatch, plan, size=6 << 20):
    import os
    src = tmp_path / "dying-card.bin"
    data = os.urandom(size)
    src.write_bytes(data)
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    monkeypatch.setenv("PHOTORECOVER_FAULTS", str(tmp_path / "plan.json"))
    device._Faulty.opens, device._Faulty.counts = 0, {}
    return src, data


def test_copy_of_a_failing_card_keeps_everything_readable_and_lists_the_rest(tmp_path, monkeypatch):
    import hashlib
    bad = [[1_500_000 // 512 * 512, 1_500_000 // 512 * 512 + 2048], [4 << 20, (4 << 20) + 512]]
    src, data = _faulty(tmp_path, monkeypatch, dict(bad=bad, flaky=[[3 << 20, (3 << 20) + 4096, 1]]))
    dest = tmp_path / "out" / "card.img"
    info = device.clone(str(src), dest)
    assert info["state"] == "done"
    want = bytearray(data)
    for a, b in bad:
        want[a:b] = bytes(b - a)
    got = dest.read_bytes()
    assert got == bytes(want), "only the unreadable sectors may differ, and they must be zeros"
    assert [list(x) for x in info["bad"]] == [[a, b - a] for a, b in bad]
    assert info["bad_bytes"] == 2048 + 512
    assert info["sha256"] == hashlib.sha256(got).hexdigest()
    listed = json.loads((tmp_path / "out" / "card.img.bad.json").read_text())
    assert [list(x) for x in listed["unreadable"]] == [[a, b - a] for a, b in bad]
    assert not (tmp_path / "out" / "card.img.map.json").exists()


def test_copy_carries_on_after_the_card_drops_off(tmp_path, monkeypatch):
    import hashlib
    monkeypatch.setattr(device, "GIVE_UP_AFTER", 2)
    src, data = _faulty(tmp_path, monkeypatch, dict(gone_at=(2 << 20) + 300_000, back_after=1,
                                                    bad=[[1 << 20, (1 << 20) + 512]]))
    dest = tmp_path / "out" / "card.img"
    first = device.clone(str(src), dest, size=len(data))
    assert first["state"] == "error" and first["error"] == "gone" and first.get("resumable")
    assert dest.exists() and (tmp_path / "out" / "card.img.map.json").exists()
    assert first["done"] == 1 << 20        # back to where the unreadable stretch began
    second = device.clone(str(src), dest, size=len(data))
    assert second["state"] == "done"
    want = bytearray(data)
    want[1 << 20:(1 << 20) + 512] = bytes(512)
    assert dest.read_bytes() == bytes(want)
    assert second["sha256"] == hashlib.sha256(bytes(want)).hexdigest()


def test_progress_file_held_open_by_another_program_does_not_stop_the_copy(tmp_path, monkeypatch):
    """Seen on a real 128 GB copy: 26 minutes in, Windows refused to swap the progress file for an
    instant (something else was reading it) and the copy stopped, blaming administrator rights."""
    import os
    from photorecover import device
    src = tmp_path / "card.bin"
    src.write_bytes(os.urandom(3 << 20))
    real, calls = os.replace, {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] % 2:
            raise PermissionError(13, "Access is denied")
        return real(a, b)
    monkeypatch.setattr(device.os, "replace", flaky)
    info = device.clone(str(src), tmp_path / "copy.img", status_file=tmp_path / "status.json")
    assert info["state"] == "done", info
    assert (tmp_path / "copy.img").read_bytes() == src.read_bytes()


def test_copy_never_empties_a_file_that_is_already_there(tmp_path):
    """Three ways a copy could destroy data: starting again while the card is unplugged, saving
    onto the thing being copied, and reusing the name of an unfinished copy of another card."""
    import json
    import os
    from photorecover import device
    src = tmp_path / "card.bin"
    src.write_bytes(os.urandom(2 << 20))
    part = tmp_path / "copy" / "card.img"
    part.parent.mkdir()
    part.write_bytes(src.read_bytes()[:1 << 20])
    progress = dict(source=str(tmp_path / "unplugged.bin"), total=2 << 20, done=1 << 20, later=[], bad=[])
    (tmp_path / "copy" / "card.img.map.json").write_text(json.dumps(progress))
    # the card is not there: the part already copied must survive
    info = device.clone(str(tmp_path / "unplugged.bin"), part)
    assert info["state"] == "error" and part.stat().st_size == 1 << 20
    # an unfinished copy of a different card is saved under this name: it must survive too
    info = device.clone(str(src), part)
    assert info["state"] == "error" and info["error"] == "exists" and part.stat().st_size == 1 << 20
    # onto itself
    before = src.read_bytes()
    info = device.clone(str(src), src)
    assert info["state"] == "error" and src.read_bytes() == before
    from photorecover import cli
    assert cli.main(["clone", str(src), str(src)]) == 1 and src.read_bytes() == before
    # and the same card, back in, carries on
    progress["source"] = str(src)
    (tmp_path / "copy" / "card.img.map.json").write_text(json.dumps(progress))
    info = device.clone(str(src), part)
    assert info["state"] == "done" and part.read_bytes() == before


def test_save_place_is_pictures_desktop_or_a_chosen_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard.device, "known_folder", lambda which: tmp_path / which)
    s = wizard.Session()
    assert s.place == "pictures" and s.folder.parent == tmp_path / "pictures"
    assert s.folder.name.startswith("Rescued photos ")
    s.set_place("desktop")
    assert s.place == "desktop" and s.folder.parent == tmp_path / "desktop"
    s.set_place("other", None)                 # chooser cancelled: nothing changes
    assert s.place == "desktop"
    s.set_place("other", str(tmp_path / "usb"))
    assert s.place == "other" and s.folder.parent == tmp_path / "usb"


def test_delete_copy_only_removes_the_copy_this_program_made(tmp_path):
    s = wizard.Session()
    s.folder = tmp_path / "out"
    s.folder.mkdir()
    mine = s.folder / "card.img"
    mine.write_bytes(b"x" * 10)
    (s.folder / "card.img.sha256.txt").write_text("abc  card.img\n")
    (s.folder / "Recovered").mkdir()
    (s.folder / "Recovered" / "photo.jpg").write_bytes(b"jpg")
    s.image = mine
    s.delete_copy()                            # an image the person brought is never deleted
    assert mine.exists()
    s.made_copy = True
    s.delete_copy()
    assert not mine.exists() and not (s.folder / "card.img.sha256.txt").exists()
    assert (s.folder / "Recovered" / "photo.jpg").exists() and s.copy_deleted
