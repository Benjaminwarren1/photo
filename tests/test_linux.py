"""The Linux side: finding cards with lsblk, reading them without root through UDisks (checked against a
stand-in UDisks on a private D-Bus), and the pkexec fallback when that is not possible."""
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from photolifeboat import cli, device, linux

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")


def make(*a):
    return device.Drive(*a)


LSBLK = {"blockdevices": [
    # a laptop: the system is inside an encrypted volume on the NVMe drive
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "size": 512110190592, "model": "Samsung SSD 980", "vendor": None,
     "tran": "nvme", "rm": False, "hotplug": False, "type": "disk", "mountpoint": None,
     "children": [{"name": "nvme0n1p1", "type": "part", "mountpoint": "/boot/efi"},
                  {"name": "nvme0n1p2", "type": "part", "mountpoint": None,
                   "children": [{"name": "dm_crypt-0", "type": "crypt", "mountpoint": None,
                                 "children": [{"name": "ubuntu--vg-root", "type": "lvm", "mountpoint": "/"}]}]}]},
    # the card in the laptop's own slot, opened by the desktop
    {"name": "mmcblk0", "path": "/dev/mmcblk0", "size": 1977614336, "model": None, "vendor": None,
     "tran": None, "rm": False, "hotplug": False, "type": "disk", "mountpoint": None,
     "children": [{"name": "mmcblk0p1", "type": "part", "mountpoint": "/media/ben/NIKON D3500"}]},
    # the eMMC boot areas some laptops have: never a card
    {"name": "mmcblk1boot0", "path": "/dev/mmcblk1boot0", "size": 4194304, "type": "disk", "rm": False},
    # a USB card reader with no card in it
    {"name": "sdb", "path": "/dev/sdb", "size": 0, "model": "SD/MMC", "vendor": "Generic-", "tran": "usb",
     "rm": True, "hotplug": True, "type": "disk"},
    # a USB reader with a card, older lsblk (strings, no path column)
    {"name": "sdc", "size": "15931539456", "model": "STORE N GO", "vendor": "Verbatim", "tran": "usb",
     "rm": "1", "hotplug": "1", "type": "disk", "mountpoint": None},
    {"name": "loop3", "path": "/dev/loop3", "size": 4096, "type": "loop", "mountpoint": "/snap/core/1"},
    {"name": "sr0", "path": "/dev/sr0", "size": 1073741312, "type": "rom", "rm": True},
]}


def test_drives_from_lsblk():
    got = {d.id: d for d in linux.drives_from_lsblk(LSBLK, make)}
    assert set(got) == {"nvme0n1", "mmcblk0", "sdc"}
    assert got["nvme0n1"].system and not got["nvme0n1"].removable
    assert got["mmcblk0"].removable and not got["mmcblk0"].system and got["mmcblk0"].name == "SD card"
    assert got["mmcblk0"].path == "/dev/mmcblk0"
    assert got["sdc"].path == "/dev/sdc" and got["sdc"].size == 15931539456
    assert got["sdc"].label() == "Verbatim STORE N GO (16 GB)"


def test_new_card_is_found_between_two_looks():
    before = linux.drives_from_lsblk({"blockdevices": LSBLK["blockdevices"][:1]}, make)
    after = linux.drives_from_lsblk(LSBLK, make)
    assert [d.id for d in device.new_drives(before, after)] == ["mmcblk0", "sdc"]


def test_drive_list_runs_on_this_machine():
    for d in device.list_drives():                 # whatever is here; it must not fail
        assert d.path.startswith("/dev/") and d.size > 0


def test_filesystem_of_a_folder():
    mounts = ("/dev/vda1 / ext4 rw 0 0\n"
              "/dev/sdb1 /media/ben/MY\\040STICK vfat rw,nosuid 0 0\n"
              "/dev/sdc1 /media/ben/BIG exfat rw 0 0\n")
    assert linux.filesystem_of(Path("/media/ben/MY STICK/Rescued photos"), mounts) == "FAT32"
    assert linux.filesystem_of(Path("/media/ben/BIG/x"), mounts) == "exFAT"
    assert linux.filesystem_of(Path("/home/ben"), mounts) == "ext4"
    assert device.dest_problem(Path("/x"), 8 << 30, free=1 << 40, fs="FAT32")      # too big for FAT32


def test_pictures_folder_follows_the_desktop_language(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Bilder").mkdir(parents=True)
    (home / ".config").mkdir()
    (home / ".config/user-dirs.dirs").write_text('XDG_DESKTOP_DIR="$HOME/Schreibtisch"\n'
                                                  'XDG_PICTURES_DIR="$HOME/Bilder"\n')
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert linux.user_folder("pictures", home) == home / "Bilder"
    assert linux.user_folder("desktop", home) is None          # set, but the folder is not there


def test_programs_started_from_the_packaged_build_get_the_system_libraries(monkeypatch):
    monkeypatch.setattr(sys, "_MEIPASS", "/tmp/.mount_PhotoX/usr/lib/photolifeboat/_internal", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/.mount_PhotoX/usr/lib/photolifeboat/_internal")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/opt/mine")
    monkeypatch.setenv("QT_PLUGIN_PATH", "/tmp/.mount_PhotoX/usr/lib/photolifeboat/_internal/PyQt6/Qt6/plugins")
    env = linux.outside_env()
    assert env["LD_LIBRARY_PATH"] == "/opt/mine" and "QT_PLUGIN_PATH" not in env
    assert not any(k.endswith("_ORIG") for k in env)


def test_pkexec_inside_an_appimage_starts_the_appimage_file(tmp_path, monkeypatch):
    image = tmp_path / "PhotoLifeboat.AppImage"
    image.write_text("")
    monkeypatch.setenv("PHOTOLIFEBOAT_PKEXEC", "/usr/bin/pkexec")
    monkeypatch.setenv("APPIMAGE", str(image))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    got = linux.elevated_command(["/tmp/.mount_x/usr/lib/photolifeboat/photolifeboat-cli", "clone", "/dev/sdb", "o"])
    assert got == ["/usr/bin/pkexec", str(image), "clone", "/dev/sdb", "o"]


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to hand files to someone else")
def test_files_made_as_root_are_handed_back(tmp_path, monkeypatch):
    f = tmp_path / "card.img"
    f.write_bytes(b"x")
    monkeypatch.setenv("PKEXEC_UID", "54321")
    linux.give_back([f, tmp_path / "not-there", None])
    assert f.stat().st_uid == 54321


# ---- the pkexec fallback ----------------------------------------------------------------------------

def _card(tmp_path):
    src = tmp_path / "card.bin"
    src.write_bytes(bytes(range(256)) * 4096)
    return src


def _as_ordinary_user(monkeypatch, src, stand_in):
    drive = device.Drive("sdz", str(src), src.stat().st_size, "Card", True, False)
    monkeypatch.setattr(device, "list_drives", lambda: [drive])
    monkeypatch.setattr(device, "is_admin", lambda: False)
    monkeypatch.setattr(device, "can_open", lambda source: False)       # as if /dev/sdz were root-only
    monkeypatch.delenv("PHOTOLIFEBOAT_FAKE_DRIVES", raising=False)
    monkeypatch.setenv("PHOTOLIFEBOAT_PKEXEC", str(stand_in))


def test_card_only_root_can_read_is_copied_through_pkexec(tmp_path, monkeypatch):
    src = _card(tmp_path)
    stand_in = tmp_path / "pkexec"
    stand_in.write_text('#!/bin/sh\necho "$@" > "$(dirname "$0")/asked"\nexec "$@"\n')    # "the password was right"
    stand_in.chmod(0o755)
    _as_ordinary_user(monkeypatch, src, stand_in)
    status = tmp_path / "status.json"
    assert cli.main(["clone", "sdz", str(tmp_path / "out" / "card.img"), "--status", str(status)]) == 0
    assert (tmp_path / "out" / "card.img").read_bytes() == src.read_bytes()
    assert json.loads(status.read_text())["state"] == "done"
    assert " clone " in (tmp_path / "asked").read_text()


def test_closing_the_password_window_is_said_plainly(tmp_path, monkeypatch):
    src = _card(tmp_path)
    stand_in = tmp_path / "pkexec"
    stand_in.write_text("#!/bin/sh\nexit 126\n")                       # what pkexec does when dismissed
    stand_in.chmod(0o755)
    _as_ordinary_user(monkeypatch, src, stand_in)
    status = tmp_path / "status.json"
    assert cli.main(["clone", "sdz", str(tmp_path / "card.img"), "--status", str(status)]) == 1
    info = json.loads(status.read_text())
    assert info["state"] == "error" and info["error"] == "elevation" and "password" in info["message"]
    assert not (tmp_path / "card.img").exists()


# ---- UDisks -----------------------------------------------------------------------------------------

jeepney = pytest.importorskip("jeepney")


@pytest.fixture
def private_bus(tmp_path):
    if not shutil.which("dbus-daemon"):
        pytest.skip("no dbus-daemon")
    p = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address"], stdout=subprocess.PIPE,
                         text=True)
    addr = p.stdout.readline().strip()
    yield addr
    p.terminate()
    p.wait()


def _fake_udisks(addr, devices, refuse=False):
    """A stand-in for UDisks on a private bus: ResolveDevice and OpenDevice, handing back a real fd."""
    from jeepney import new_error, new_method_return
    from jeepney.bus_messages import message_bus
    from jeepney.io.blocking import open_dbus_connection
    conn = open_dbus_connection(bus=addr, enable_fds=True)
    conn.send_and_get_reply(message_bus.RequestName(linux.UDISKS))
    seen = []

    def serve():
        while True:
            try:
                msg = conn.receive()
            except Exception:  # noqa: BLE001 - bus gone at the end of the test
                return
            member = msg.header.fields.get(jeepney.HeaderFields.member)
            if member == "ResolveDevice":
                path = msg.body[0]["path"][1]
                name = Path(path).name
                conn.send(new_method_return(msg, "ao", ([f"/org/freedesktop/UDisks2/block_devices/{name}"],)))
            elif member == "OpenDevice":
                seen.append((msg.header.fields[jeepney.HeaderFields.path], msg.body[0],
                             bool(msg.header.flags & jeepney.MessageFlag.allow_interactive_authorization)))
                name = msg.header.fields[jeepney.HeaderFields.path].rsplit("/", 1)[1]
                if refuse:
                    conn.send(new_error(msg, "org.freedesktop.UDisks2.Error.NotAuthorizedDismissed",
                                        "s", ("Not authorized",)))
                else:
                    fd = os.open(devices[name], os.O_RDONLY)
                    conn.send(new_method_return(msg, "h", (fd,)))
                    os.close(fd)

    threading.Thread(target=serve, daemon=True).start()
    return seen


def test_card_is_read_through_udisks_without_root(tmp_path, monkeypatch, private_bus):
    src = _card(tmp_path)
    seen = _fake_udisks(private_bus, {"sdz": str(src)})
    monkeypatch.setenv("PHOTOLIFEBOAT_DBUS", private_bus)
    real_open = open

    def no_entry(path, *a, **k):                         # /dev/sdz exists and only root may open it
        if str(path) == "/dev/sdz":
            raise PermissionError(13, "Permission denied", path)
        return real_open(path, *a, **k)

    monkeypatch.setattr(device, "open", no_entry, raising=False)
    assert device.can_open("/dev/sdz")
    info = device.clone("/dev/sdz", tmp_path / "card.img", src.stat().st_size)
    assert info["state"] == "done"
    assert (tmp_path / "card.img").read_bytes() == src.read_bytes()
    assert seen[0] == ("/org/freedesktop/UDisks2/block_devices/sdz", "r", True)     # read-only, may ask


def test_udisks_saying_no_is_a_permission_problem(tmp_path, monkeypatch, private_bus):
    _fake_udisks(private_bus, {}, refuse=True)
    monkeypatch.setenv("PHOTOLIFEBOAT_DBUS", private_bus)
    with pytest.raises(PermissionError):
        linux.open_with_udisks("/dev/sdz", timeout=10)


def test_no_udisks_at_all_is_a_permission_problem(monkeypatch):
    monkeypatch.setenv("PHOTOLIFEBOAT_DBUS", "unix:path=/nonexistent/bus")
    with pytest.raises(PermissionError):
        linux.open_with_udisks("/dev/sdz", timeout=5)
