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
