"""Everything that depends on the operating system: finding the card, and
cloning it. All other modules work on an image file and are the same everywhere.

Cloning opens the card read-only and never writes to it.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

CHUNK = 1 << 20
SECTOR = 512


@dataclass
class Drive:
    id: str            # stable for this session, e.g. "PhysicalDrive2" or "sdb"
    path: str          # what to open for raw reading
    size: int
    name: str
    removable: bool
    system: bool       # holds the operating system: never offered as a card

    def label(self) -> str:
        gb = self.size / 1e9
        size = f"{gb:.0f} GB" if gb >= 10 else f"{gb:.1f} GB" if gb >= 1 else f"{self.size / 1e6:.0f} MB"
        return f"{self.name.strip() or 'Unnamed drive'} ({size})"


def _powershell(cmd: str) -> str:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                       capture_output=True, text=True, timeout=60, creationflags=flags)
    return r.stdout


def list_drives() -> list[Drive]:
    """Whole physical drives currently attached (not partitions or drive letters)."""
    fake = os.environ.get("PHOTORECOVER_FAKE_DRIVES")       # tests: a folder of *.img files acts as the drives
    if fake:
        return [Drive(p.stem, str(p), p.stat().st_size, p.stem, True, False) for p in sorted(Path(fake).glob("*.img"))]
    out: list[Drive] = []
    if sys.platform == "win32":
        raw = _powershell("Get-Disk | Select-Object Number,FriendlyName,Size,BusType,IsSystem,IsBoot | ConvertTo-Json")
        try:
            disks = json.loads(raw) if raw.strip() else []
        except json.JSONDecodeError:
            disks = []
        if isinstance(disks, dict):
            disks = [disks]
        for d in disks:
            if not d.get("Size"):
                continue
            bus = str(d.get("BusType", ""))
            out.append(Drive(f"PhysicalDrive{d['Number']}", rf"\\.\PhysicalDrive{d['Number']}", int(d["Size"]),
                             str(d.get("FriendlyName") or ""), bus in ("USB", "SD", "MMC", "7", "12", "13"),
                             bool(d.get("IsSystem") or d.get("IsBoot"))))
    elif sys.platform == "darwin":
        import plistlib
        r = subprocess.run(["diskutil", "list", "-plist", "physical"], capture_output=True, timeout=60)
        for name in plistlib.loads(r.stdout).get("WholeDisks", []) if r.stdout else []:
            i = subprocess.run(["diskutil", "info", "-plist", name], capture_output=True, timeout=60)
            info = plistlib.loads(i.stdout) if i.stdout else {}
            if info.get("TotalSize"):
                out.append(Drive(name, f"/dev/r{name}", int(info["TotalSize"]), str(info.get("MediaName", "")),
                                 bool(info.get("RemovableMediaOrExternalDevice")), bool(info.get("Internal"))))
    else:
        mounts = Path("/proc/mounts").read_text() if Path("/proc/mounts").exists() else ""
        for blk in sorted(Path("/sys/block").glob("*")):
            n = blk.name
            if n.startswith(("loop", "ram", "zram", "dm-", "sr")):
                continue
            try:
                size = int((blk / "size").read_text()) * SECTOR
                removable = (blk / "removable").read_text().strip() == "1"
            except (OSError, ValueError):
                continue
            if not size:
                continue
            model = (blk / "device/model").read_text().strip() if (blk / "device/model").exists() else ""
            system = any(line.split()[0].startswith(f"/dev/{n}") and line.split()[1] in ("/", "/boot", "/boot/efi")
                         for line in mounts.splitlines() if line.strip())
            out.append(Drive(n, f"/dev/{n}", size, model, removable or n.startswith("mmcblk"), system))
    return out


def new_drives(before: list[Drive], after: list[Drive]) -> list[Drive]:
    """Drives that appeared since the first look. The one new drive is the card."""
    had = {(d.id, d.size) for d in before}
    return [d for d in after if (d.id, d.size) not in had and not d.system]


def drive_of_path(path: Path) -> str | None:
    """Which physical drive a folder lives on (Windows), so a clone is never saved onto the card."""
    if sys.platform != "win32":
        return None
    letter = str(Path(path).resolve().drive).rstrip(":")
    if len(letter) != 1:
        return None
    n = _powershell(f"(Get-Partition -DriveLetter {letter}).DiskNumber").strip()
    return f"PhysicalDrive{n}" if n.isdigit() else None


def free_space(folder: Path) -> int:
    p = Path(folder)
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(p).free


def _device_size(f, path: str, hint: int | None) -> int:
    if hint:
        return hint
    try:
        return os.fstat(f.fileno()).st_size or f.seek(0, os.SEEK_END)
    except OSError:
        return 0
    finally:
        try:
            f.seek(0)
        except OSError:
            pass


def clone(source: str, dest: Path, size: int | None = None, status_file: Path | None = None,
          progress=None, cancelled=None) -> dict:
    """Read the whole card into one image file.

    The card is opened read-only. Unreadable sectors are retried one at a time,
    filled with zeros if they still fail, and listed in the result."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    sha = hashlib.sha256()
    bad: list[tuple[int, int]] = []
    done = 0
    last_note = 0.0

    def note(state: str, **extra):
        info = dict(state=state, done=done, total=total, bad_bytes=sum(n for _, n in bad),
                    seconds=round(time.time() - t0, 1), **extra)
        if status_file:
            tmp = Path(str(status_file) + ".tmp")
            tmp.write_text(json.dumps(info))
            os.replace(tmp, status_file)
        if progress:
            progress(info)
        return info

    total = 0
    try:
        with open(source, "rb", buffering=0) as f, open(dest, "wb") as out:
            total = _device_size(f, source, size)
            while total == 0 or done < total:
                if cancelled and cancelled():
                    out.close()
                    dest.unlink(missing_ok=True)
                    return note("cancelled")
                want = CHUNK if not total else min(CHUNK, total - done)
                want += -want % SECTOR
                try:
                    buf = f.read(want)
                except OSError:
                    # a bad patch: go through it one sector at a time
                    buf = b""
                    for o in range(done, done + want, SECTOR):
                        try:
                            f.seek(o)
                            s = f.read(SECTOR)
                        except OSError:
                            s = b""
                        if len(s) < SECTOR:
                            if not bad or bad[-1][0] + bad[-1][1] != o:
                                bad.append((o, SECTOR))
                            else:
                                bad[-1] = (bad[-1][0], bad[-1][1] + SECTOR)
                            s = s + bytes(SECTOR - len(s))
                        buf += s
                    try:
                        f.seek(done + want)
                    except OSError:
                        pass
                if not buf:
                    break
                if total and done + len(buf) > total:
                    buf = buf[:total - done]
                out.write(buf)
                sha.update(buf)
                done += len(buf)
                if time.time() - last_note > 0.5:
                    last_note = time.time()
                    note("cloning")
    except PermissionError:
        return note("error", error="permission", message="Reading the card needs administrator rights.")
    except OSError as e:
        Path(dest).unlink(missing_ok=True)
        return note("error", error="io", message=f"Could not read the card: {e}")
    if total and done < total:
        return note("error", error="short", message="The card stopped responding before the end.")
    total = total or done
    return note("done", sha256=sha.hexdigest(), image=str(dest), bad=bad)


def is_admin() -> bool:
    if sys.platform == "win32":
        import ctypes
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except OSError:
            return False
    return os.geteuid() == 0


def self_command() -> list[str]:
    """How to start this program again (works both as an .exe and from source)."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "photorecover"]


def start_clone(drive: Drive, dest: Path, status_file: Path) -> None:
    """Start the clone as a separate process. On Windows, raw drive reads need
    administrator rights, so only this helper asks for them (one UAC prompt)."""
    cmd = self_command() + ["clone", drive.path, str(dest), "--size", str(drive.size), "--status", str(status_file)]
    Path(status_file).write_text(json.dumps(dict(state="starting", done=0, total=drive.size)))
    if sys.platform == "win32" and not is_admin() and not os.environ.get("PHOTORECOVER_FAKE_DRIVES"):
        import ctypes
        params = subprocess.list2cmdline(cmd[1:])
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", cmd[0], params, None, 0)
        if rc <= 32:
            Path(status_file).write_text(json.dumps(dict(
                state="error", error="elevation", done=0, total=drive.size,
                message="Windows did not allow the card to be read. Click Yes on the permission prompt to continue.")))
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)


def drive_dict(d: Drive) -> dict:
    return dict(asdict(d), label=d.label())
