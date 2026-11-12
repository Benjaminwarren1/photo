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
    from photorecover.recover import GRADE_REVIEW
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
