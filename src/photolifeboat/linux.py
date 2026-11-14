"""The Linux side of finding and reading a card. Only device.py uses this module.

Reading a card on Linux
-----------------------
A card shows up as /dev/sdb or /dev/mmcblk0, which ordinary users are not allowed to read. Three ways in,
tried in this order, so that most people are never asked anything:

1. The program can already read it (it runs as root, or the person is in the "disk" group).
2. UDisks, the service desktops use to mount cards, hands over a read-only handle to the card. For a card
   or USB stick plugged into the computer the person is sitting at, desktops allow this without a
   password; for anything else the system asks for one in its usual window.
3. pkexec runs the copying helper as root (the system's own password window). Used only where UDisks is
   missing, such as on a minimal install.

The card is opened read-only every time; nothing here can write to it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SECTOR = 512
# Mount points that mean "this drive holds the running system": never offered as a card.
SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var", "[SWAP]"}


def _is_skipped(name: str) -> bool:
    """Not a drive anyone would plug in: RAM disks, loop files, CD drives, the eMMC boot areas of a laptop."""
    return (name.startswith(("loop", "ram", "zram", "dm-", "sr", "md", "nbd", "fd"))
            or re.fullmatch(r"mmcblk\d+(boot\d+|rpmb)", name) is not None)


def _mounts_in(node: dict) -> set[str]:
    """Every mount point on a drive, its partitions, and anything inside them (encrypted or LVM volumes)."""
    found = set()
    for key in ("mountpoints", "mountpoint"):
        v = node.get(key)
        for m in (v if isinstance(v, list) else [v]):
            if m:
                found.add(m)
    for child in node.get("children") or []:
        found |= _mounts_in(child)
    return found


def _nice_name(node: dict) -> str:
    vendor = (node.get("vendor") or "").strip()
    model = (node.get("model") or "").strip()
    if vendor.lower().startswith("0x") or vendor.lower() in ("ata", "generic"):
        vendor = ""
    name = f"{vendor} {model}" if vendor and model and not model.lower().startswith(vendor.lower()) else model or vendor
    if not name and ((node.get("tran") or "") == "mmc" or str(node.get("name", "")).startswith("mmcblk")):
        return "SD card"
    return name


def drives_from_lsblk(data: dict, make) -> list:
    """Whole drives from `lsblk --json` output. make(id, path, size, name, removable, system) builds each."""
    out = []
    for node in data.get("blockdevices") or []:
        name = str(node.get("name") or "")
        if node.get("type") != "disk" or not name or _is_skipped(name):
            continue
        try:
            size = int(node.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if not size:                    # a card reader with no card in it
            continue
        tran = (node.get("tran") or "").lower()
        removable = (_truthy(node.get("rm")) or _truthy(node.get("hotplug")) or tran in ("usb", "mmc")
                     or name.startswith("mmcblk"))
        system = bool(_mounts_in(node) & SYSTEM_MOUNTS)
        out.append(make(name, node.get("path") or f"/dev/{name}", size, _nice_name(node), removable, system))
    return out


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("1", "true")


def list_drives(make) -> list:
    """Whole drives attached to this computer, from lsblk (part of every Linux), else from /sys."""
    cols = "NAME,PATH,SIZE,MODEL,VENDOR,TRAN,RM,HOTPLUG,TYPE,MOUNTPOINT"
    for c in (cols, cols.replace("PATH,", "")):          # PATH needs util-linux 2.33 (2019)
        try:
            r = subprocess.run(["lsblk", "--json", "--bytes", "-o", c], capture_output=True, text=True,
                               timeout=30, env=outside_env())
        except (OSError, subprocess.SubprocessError):
            break
        if r.returncode == 0 and r.stdout.strip():
            try:
                return drives_from_lsblk(json.loads(r.stdout), make)
            except ValueError:
                break
    return _drives_from_sys(make)


def _drives_from_sys(make) -> list:
    mounts = _read_mounts()
    out = []
    for blk in sorted(Path("/sys/block").glob("*")):
        n = blk.name
        if _is_skipped(n):
            continue
        try:
            size = int((blk / "size").read_text()) * SECTOR
            removable = (blk / "removable").read_text().strip() == "1"
        except (OSError, ValueError):
            continue
        if not size:
            continue
        model = ""
        try:
            model = (blk / "device/model").read_text().strip()
        except OSError:
            pass
        parts = {n} | {p.name for p in blk.glob(f"{n}*")}
        system = any(Path(dev).name in parts and mp in SYSTEM_MOUNTS for dev, mp, _ in mounts)
        out.append(make(n, f"/dev/{n}", size, model or ("SD card" if n.startswith("mmcblk") else ""),
                        removable or n.startswith("mmcblk"), system))
    return out


def _read_mounts(text: str | None = None) -> list[tuple[str, str, str]]:
    """(device, mount point, filesystem) for everything mounted, from /proc/mounts."""
    if text is None:
        try:
            text = Path("/proc/mounts").read_text()
        except OSError:
            return []
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 3:
            unesc = lambda s: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)  # noqa: E731
            out.append((unesc(f[0]), unesc(f[1]), f[2]))
    return out


FS_NAMES = {"vfat": "FAT32", "msdos": "FAT", "fat": "FAT", "exfat": "exFAT", "ntfs": "NTFS", "ntfs3": "NTFS",
            "fuseblk": "", "ext4": "ext4", "ext3": "ext3", "btrfs": "Btrfs", "xfs": "XFS"}


def filesystem_of(folder: Path, mounts_text: str | None = None) -> str:
    """The format of the drive a folder is on, named the way device.dest_problem expects ("FAT32"...)."""
    p = Path(folder).absolute()
    try:
        p = p.resolve()             # follows links in the part that exists; the rest is kept as written
    except OSError:
        pass
    best, fs = "", ""
    for _, mp, kind in _read_mounts(mounts_text):
        if (str(p) == mp or str(p).startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best):
            best, fs = mp, kind
    return FS_NAMES.get(fs, fs)


def user_folder(which: str, home: Path | None = None) -> Path | None:
    """The person's Pictures or Desktop folder as their desktop names it (it may be "Bilder" or "Bureau"),
    from ~/.config/user-dirs.dirs. None when it is not set or does not exist."""
    home = home or Path.home()
    key = {"pictures": "XDG_PICTURES_DIR", "desktop": "XDG_DESKTOP_DIR"}[which]
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "user-dirs.dirs"
    try:
        text = cfg.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    m = re.search(rf'^\s*{key}\s*=\s*"?([^"\n]*)"?\s*$', text, re.M)
    if not m:
        return None
    value = m.group(1).replace("$HOME", str(home))
    path = Path(value) if value.startswith("/") else home / value
    return path if path.is_dir() and path != home else None


def outside_env() -> dict:
    """The environment for starting other programs (the file manager, the browser, pkexec). The packaged
    program points the library search path at its own bundled copies, which would break those programs,
    so anything that points inside the bundle is put back the way the system had it."""
    env = dict(os.environ)
    bundle = getattr(sys, "_MEIPASS", None)
    if not bundle:
        return env
    for key in list(env):
        if key.endswith("_ORIG"):
            continue
        if bundle in env[key]:
            orig = env.get(key + "_ORIG")
            if orig is not None:
                env[key] = orig
            else:
                env.pop(key)
    for key in [k for k in env if k.endswith("_ORIG")]:
        env.pop(key)
    return env


# ---- reading the card -----------------------------------------------------------------------------

UDISKS = "org.freedesktop.UDisks2"


def _bus():
    # Tests point this at a private bus with a stand-in UDisks.
    return os.environ.get("PHOTOLIFEBOAT_DBUS") or "SYSTEM"


def open_with_udisks(path: str, timeout: float = 600) -> int:
    """Ask UDisks for a read-only handle to a whole drive. Returns a file descriptor, or raises
    PermissionError when UDisks is not there or the person said no. May show the system's password
    window, which is why the timeout is long."""
    try:
        from jeepney import DBusAddress, MessageFlag, new_method_call
        from jeepney.io.blocking import open_dbus_connection
        from jeepney.wrappers import unwrap_msg
    except ImportError as e:
        raise PermissionError("UDisks cannot be reached from this copy of the program") from e
    try:
        conn = open_dbus_connection(bus=_bus(), enable_fds=True)
    except Exception as e:  # noqa: BLE001 - no system bus (a container, a server without it)
        raise PermissionError(f"UDisks cannot be reached ({e})") from e
    try:
        mgr = DBusAddress("/org/freedesktop/UDisks2/Manager", bus_name=UDISKS,
                          interface="org.freedesktop.UDisks2.Manager")
        obj = None
        try:
            found = unwrap_msg(conn.send_and_get_reply(
                new_method_call(mgr, "ResolveDevice", "a{sv}a{sv}", ({"path": ("s", path)}, {})), timeout=30))
            obj = found[0][0] if found and found[0] else None
        except Exception:  # noqa: BLE001 - older UDisks (before 2.7.3) has no ResolveDevice
            obj = None
        if not obj:
            name = re.sub(r"[^A-Za-z0-9]", lambda m: f"_{ord(m.group()):02x}", Path(path).name)
            obj = f"/org/freedesktop/UDisks2/block_devices/{name}"
        blk = DBusAddress(obj, bus_name=UDISKS, interface="org.freedesktop.UDisks2.Block")
        msg = new_method_call(blk, "OpenDevice", "sa{sv}", ("r", {}))
        msg.header.flags |= MessageFlag.allow_interactive_authorization   # lets the system ask for a password
        try:
            reply = unwrap_msg(conn.send_and_get_reply(msg, timeout=timeout))
        except Exception as e:  # noqa: BLE001 - refused, cancelled, or no such device
            raise PermissionError(f"UDisks did not open the card ({e})") from e
        return reply[0].to_raw_fd()
    finally:
        conn.close()


def pkexec_command() -> str | None:
    """pkexec, the system's way of running one program as root after a password window, if it is here."""
    forced = os.environ.get("PHOTOLIFEBOAT_PKEXEC")    # tests: a stand-in that runs the command as is
    if forced:
        return forced
    return shutil.which("pkexec", path=outside_env().get("PATH"))


def elevated_command(cmd: list[str]) -> list[str] | None:
    """cmd run as root through pkexec, or None if that is not possible here.

    Inside an AppImage the program lives in a folder only this user can see, so root has to start the
    AppImage file itself (its launcher sends any command-line arguments to the command-line program)."""
    pk = pkexec_command()
    if not pk:
        return None
    appimage = os.environ.get("APPIMAGE")
    if getattr(sys, "frozen", False) and appimage and Path(appimage).is_file():
        cmd = [appimage] + cmd[1:]
    return [pk] + cmd


def give_back(paths, uid: int | None = None, gid: int | None = None) -> None:
    """Files written while running as root through pkexec or sudo belong to root. Hand them back to the
    person who asked, so they can move and delete them like any other file."""
    if os.geteuid() != 0:
        return
    if uid is None:
        raw = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
        if not raw or not raw.isdigit():
            return
        uid = int(raw)
    if gid is None:
        raw = os.environ.get("SUDO_GID")
        if raw and raw.isdigit():
            gid = int(raw)
        else:
            try:
                import pwd
                gid = pwd.getpwuid(uid).pw_gid
            except (KeyError, ImportError):
                gid = -1
    for p in paths:
        try:
            if p and Path(p).exists():
                os.chown(p, uid, gid)
        except OSError:
            pass
