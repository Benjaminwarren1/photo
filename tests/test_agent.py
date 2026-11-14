"""What an AI assistant (or any program) uses to drive a recovery: JSON output and progress, the
guide, contact sheets, the MCP server, and the guards that keep anything from being written to a drive."""
import hashlib
import io
import json
import time
from pathlib import Path

import pytest

from photolifeboat import cli, device
from photolifeboat.synth.scenarios import used_card


@pytest.fixture(scope="module")
def card(tmp_path_factory):
    """A small deleted card, recovered once, shared by the tests that only read the results."""
    d = tmp_path_factory.mktemp("card")
    b = used_card(fat_type=16, cluster_size=16384, size_mb=64, seed=31, photos=12)
    b.delete_all()
    b.save(d / "card.img")
    return d, b


def run_cli(capsys, *argv):
    code = cli.main([str(a) for a in argv])
    got = capsys.readouterr()
    return code, got.out, got.err


def test_recover_json_and_progress_keep_to_their_streams(card, tmp_path, capsys):
    d, b = card
    before = hashlib.sha256((d / "card.img").read_bytes()).hexdigest()
    code, out, err = run_cli(capsys, "recover", d / "card.img", "--out", tmp_path / "out", "--json", "--progress")
    assert code == 0
    summary = json.loads(out)                                      # standard output is exactly one JSON object
    assert summary["counts"]["Verified"] == sum(t.recoverable for t in b.history)
    events = [json.loads(line) for line in err.splitlines()]       # standard error is only JSON lines
    assert events and events[0]["event"] == "step" and all(e["event"] in ("step", "progress") for e in events)
    assert hashlib.sha256((d / "card.img").read_bytes()).hexdigest() == before


def test_errors_are_json_too_and_a_drive_is_never_recovered_from_directly(tmp_path, capsys):
    code, out, _ = run_cli(capsys, "recover", tmp_path / "nothing.img", "--json")
    assert code == 2 and json.loads(out)["error"] == "missing"
    for drive in (r"\\.\PhysicalDrive1", "E:", "/dev/null"):
        code, out, err = run_cli(capsys, "recover", drive, "--json")
        assert code == 2 and json.loads(out)["error"] == "device" and "clone" in err


def test_a_copy_is_never_written_onto_a_drive(tmp_path, capsys):
    src = tmp_path / "card.bin"
    src.write_bytes(b"x" * 4096)
    for dest in (r"\\.\PhysicalDrive1", "/dev/null"):
        code, out, _ = run_cli(capsys, "clone", src, dest, "--json")
        assert code == 1 and json.loads(out)["error"] == "device"
        assert device.clone(str(src), Path(dest))["error"] == "device"     # and not by the library either


def test_the_system_drive_is_never_taken_for_a_card(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(device, "list_drives", lambda: [device.Drive("sda", "/dev/sda", 10 ** 12, "Disk", False, True)])
    code, out, _ = run_cli(capsys, "clone", "sda", tmp_path / "c.img", "--json")
    assert code == 1 and json.loads(out)["error"] == "system"


def test_drives_and_clone_as_json(tmp_path, monkeypatch, capsys):
    drives = tmp_path / "drives"
    drives.mkdir()
    (drives / "SDCARD.img").write_bytes(bytes(range(256)) * 4096)
    monkeypatch.setenv("PHOTOLIFEBOAT_FAKE_DRIVES", str(drives))
    code, out, _ = run_cli(capsys, "drives", "--json")
    listed = json.loads(out)
    assert code == 0 and listed[0]["id"] == "SDCARD" and listed[0]["removable"] and not listed[0]["system"]
    code, out, _ = run_cli(capsys, "clone", drives / "SDCARD.img", tmp_path / "copy" / "card.img", "--json")
    last = json.loads(out.splitlines()[-1])
    assert code == 0 and last["state"] == "done"
    assert (tmp_path / "copy" / "card.img").read_bytes() == (drives / "SDCARD.img").read_bytes()


def test_guide_says_the_rules(capsys):
    code, out, _ = run_cli(capsys, "guide")
    assert code == 0 and "Never write to the card" in out and "sends that photo to your AI provider" in out


def test_contact_sheets_number_every_photo_once(card, tmp_path, capsys):
    d, b = card
    run_cli(capsys, "recover", d / "card.img", "--out", tmp_path / "out", "--json")
    code, out, _ = run_cli(capsys, "sheet", tmp_path / "out", "--to", tmp_path / "sheets", "--per-sheet", "5", "--json")
    sheets = json.loads(out)["sheets"]
    numbers = [p["number"] for s in sheets for p in s["photos"]]
    assert code == 0 and numbers == list(range(1, sum(t.recoverable for t in b.history) + 1))
    assert all(Path(s["sheet"]).read_bytes()[:2] == b"\xff\xd8" for s in sheets)
    assert all((tmp_path / "out" / p["file"]).is_file() for s in sheets for p in s["photos"])


# ---- the MCP server --------------------------------------------------------------------------------

class Client:
    def __init__(self):
        from photolifeboat.mcp_server import Server
        self.server, self.n = Server(), 0

    def rpc(self, method, params=None):
        self.n += 1
        reply = self.server.handle(dict(jsonrpc="2.0", id=self.n, method=method, params=params or {}))
        assert reply["id"] == self.n and "error" not in reply, reply
        return reply["result"]

    def tool(self, name, **args):
        return self.rpc("tools/call", dict(name=name, arguments=args))

    def data(self, name, **args):
        r = self.tool(name, **args)
        assert not r["isError"], r
        return json.loads(r["content"][0]["text"])

    def wait(self, seconds=240):
        for _ in range(seconds * 2):
            s = self.data("job_status")
            if s["state"] in ("done", "error"):
                return s
            time.sleep(0.5)
        raise AssertionError("the job never finished")


def test_mcp_server_drives_a_whole_recovery(tmp_path, monkeypatch):
    drives = tmp_path / "drives"
    drives.mkdir()
    b = used_card(fat_type=16, cluster_size=16384, size_mb=64, seed=31, photos=12)
    b.delete_all()
    b.save(drives / "SDCARD.img")
    monkeypatch.setenv("PHOTOLIFEBOAT_FAKE_DRIVES", str(drives))
    c = Client()
    init = c.rpc("initialize", dict(protocolVersion="2025-06-18", capabilities={}, clientInfo=dict(name="t")))
    assert init["capabilities"]["tools"] == {} and "read_guide" in init["instructions"]
    assert c.server.handle(dict(jsonrpc="2.0", method="notifications/initialized")) is None
    names = {t["name"] for t in c.rpc("tools/list")["tools"]}
    assert {"list_drives", "copy_card", "recover", "contact_sheet", "decide_close_call"} <= names
    assert not any(w in n for n in names for w in ("format", "write", "repair", "delete"))
    assert "Never write to the card" in c.tool("read_guide")["content"][0]["text"]

    assert c.data("list_drives")[0]["id"] == "SDCARD"
    assert c.tool("copy_card", drive="SDCARD", save_as="relative.img")["isError"]
    assert c.tool("copy_card", drive="nope", save_as=str(tmp_path / "x.img"))["isError"]
    image = tmp_path / "rescue" / "card.img"
    assert not c.tool("copy_card", drive="SDCARD", save_as=str(image))["isError"]
    assert c.wait()["state"] == "done"
    assert image.read_bytes() == (drives / "SDCARD.img").read_bytes()

    assert c.tool("results")["isError"]                                        # nothing recovered yet
    assert c.tool("recover", image="/dev/null")["isError"]
    assert not c.tool("recover", image=str(image))["isError"]
    assert c.wait()["state"] == "done"
    res = c.data("results")
    assert res["counts"]["Verified"] == sum(t.recoverable for t in b.history) and res["close_calls"] == 0
    assert c.data("close_calls") == []

    sheet = c.tool("contact_sheet")
    assert sheet["content"][1]["type"] == "image" and "1: Recovered/" in sheet["content"][0]["text"]
    first = sheet["content"][0]["text"].split("\n")[1].split(": ", 1)[1].rsplit(" (", 1)[0]
    assert c.tool("view_photo", file=first)["content"][1]["type"] == "image"
    assert c.tool("view_photo", file="../card.img")["isError"]                 # nothing outside the results
    checked = c.data("check_photos", files=[str(Path(res["results_folder"]) / first)])
    assert checked[0]["passes"]


def test_mcp_server_speaks_json_lines(tmp_path):
    from photolifeboat.mcp_server import serve
    lines = [dict(jsonrpc="2.0", id=1, method="initialize", params={}),
             dict(jsonrpc="2.0", method="notifications/initialized"),
             dict(jsonrpc="2.0", id=2, method="tools/call", params=dict(name="format_card", arguments={})),
             dict(jsonrpc="2.0", id=3, method="nonsense")]
    out = io.StringIO()
    serve(io.StringIO("\n".join(json.dumps(m) for m in lines) + "\nnot json\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, 3, None]
    assert replies[1]["result"]["isError"] and replies[2]["error"]["code"] == -32601
    assert replies[3]["error"]["code"] == -32700


def test_mcp_close_calls_show_pictures_and_take_a_decision(tmp_path):
    """The same close calls a person decides in the wizard, decided through the MCP tools."""
    from photolifeboat.items import GRADE_REVIEW
    from photolifeboat.recover import Recovery
    b = used_card(fat_type=16, cluster_size=16384, size_mb=128, seed=2)
    b.delete_all()
    b.save(tmp_path / "card.img")
    rec = Recovery(tmp_path / "card.img", tmp_path / "out")
    rec.run()
    rec._avail[:] = ~rec.vol.allocated() & ~rec.vol.blank_clusters()
    rec._rb.always_ask = True
    truth = {(t.clusters[0], t.size): t for t in b.history}
    idx, it = next((i, it) for i, it in enumerate(rec.items)
                   if (t := truth.get((it.start, it.size))) and t.recoverable and t.fragmented)
    t = truth[(it.start, it.size)]
    r = rec._rb.rebuild(it.start, it.size)
    (tmp_path / "out" / it.saved_as).unlink()
    it._last, it.grade, it.candidates, it.chain, it.percent, it.saved_as = r, GRADE_REVIEW, r.candidates, r.chain, r.percent, ""
    c = Client()
    c.server.rec, c.server.out = rec, tmp_path / "out"
    assert idx in [x["index"] for x in c.data("close_calls")]
    assert c.tool("show_close_call", index=10 ** 6)["isError"]
    for _ in range(12):
        if it.grade != GRADE_REVIEW:
            break
        shown = c.tool("show_close_call", index=idx)["content"]
        assert sum(x["type"] == "image" for x in shown) >= 2              # the preview and at least one piece
        want = t.clusters[len(it.chain)]
        assert f"With piece {want}:" in [x.get("text") for x in shown]
        c.data("decide_close_call", index=idx, cluster=want)
    assert it.grade == "Verified"
    assert hashlib.sha256((tmp_path / "out" / it.saved_as).read_bytes()).hexdigest() == t.sha256
