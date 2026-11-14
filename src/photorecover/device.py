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
        return _drive_of_path_unix(Path(path))
    letter = str(Path(path).resolve().drive).rstrip(":")
    if len(letter) != 1:
        return None
    n = _powershell(f"(Get-Partition -DriveLetter {letter}).DiskNumber").strip()
    return f"PhysicalDrive{n}" if n.isdigit() else None


def _drive_of_path_unix(path: Path) -> str | None:
    """The whole drive (sdb, mmcblk0, disk2) that holds a folder on Linux or macOS."""
    import re
    p = path
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        r = subprocess.run(["df", "-P", str(p)], capture_output=True, text=True, timeout=20)
        dev = r.stdout.strip().splitlines()[-1].split()[0]
    except (OSError, IndexError, subprocess.SubprocessError):
        return None
    if not dev.startswith("/dev/"):
        return None
    name = dev[5:]
    for pattern in (r"^(disk\d+)s\d+$", r"^((?:mmcblk|nvme\d+n|loop)\d+)p\d+$", r"^([a-z]+)\d+$"):
        m = re.match(pattern, name)
        if m:
            return m.group(1)
    return name


def free_space(folder: Path) -> int:
    p = Path(folder)
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(p).free


def filesystem_of(folder: Path) -> str:
    """The format of the drive a folder is on ("FAT32", "exFAT", "NTFS"...), or "" when unknown."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        p = Path(folder).resolve()
        name = ctypes.create_unicode_buffer(64)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(p.anchor, None, 0, None, None, None, name, 64)
        return name.value if ok else ""
    except (OSError, AttributeError, ValueError):
        return ""


def dest_problem(folder: Path, size: int | None, free: int | None = None, fs: str | None = None) -> str | None:
    """Why a copy of a card of this size cannot be saved in this folder, in plain words, or None."""
    if not size:
        return None
    gb = lambda n: f"{n / 1e9:.1f} GB"
    free = free_space(folder) if free is None else free
    if free < size:
        return (f"There is not enough room there: the copy needs {gb(size)} and only {gb(free)} is free. "
                "Choose another drive.")
    fs = filesystem_of(folder) if fs is None else fs
    if size >= 1 << 32 and fs.upper() in ("FAT32", "FAT", "FAT16"):
        return (f"That drive is formatted as {fs}, which cannot hold a file bigger than 4 GB, and the copy "
                f"will be {gb(size)}. Choose another drive, or reformat that one as exFAT (this erases it).")
    return None


def _device_size(f, path: str, hint: int | None) -> int:
    if hint:
        return hint
    if sys.platform == "win32":          # a raw drive has no file size; ask the drive itself
        try:
            import ctypes
            import msvcrt
            n, got = ctypes.c_longlong(0), ctypes.c_ulong(0)
            if ctypes.windll.kernel32.DeviceIoControl(ctypes.c_void_p(msvcrt.get_osfhandle(f.fileno())), 0x0007405C,
                                                      None, 0, ctypes.byref(n), 8, ctypes.byref(got), None) and n.value > 0:
                return int(n.value)
        except (OSError, ValueError, AttributeError):
            pass
    try:
        return os.fstat(f.fileno()).st_size or f.seek(0, os.SEEK_END)
    except OSError:
        return 0
    finally:
        try:
            f.seek(0)
        except OSError:
            pass


class _Faulty:
    """For testing: a drive (really a file) that fails the way a dying card does. `plan` is a dict:
    bad: [[start, end], ...] byte ranges that can never be read; flaky: [[start, end, times], ...]
    ranges that fail that many times and then read; gone_at: a position past which, once reached,
    every read fails (the card has dropped off the computer) until the drive is opened again
    `back_after` times; slow: [[start, end, seconds], ...]."""
    counts: dict = {}
    opens = 0

    def __init__(self, path: str, plan: dict):
        self.f, self.plan, self.pos = open(path, "rb", buffering=0), plan, 0
        type(self).opens += 1
        self.gone = False
        if plan.get("gone_at") is not None and type(self).opens > 1 and type(self).opens <= 1 + plan.get("back_after", 0):
            raise OSError("the device is not ready")

    def seek(self, pos: int, whence: int = 0) -> int:
        self.pos = self.f.seek(pos, whence)
        return self.pos

    def read(self, n: int) -> bytes:
        a, b, plan = self.pos, self.pos + n, self.plan
        if self.gone or (plan.get("gone_at") is not None and b > plan["gone_at"] and type(self).opens == 1):
            self.gone = True
            raise OSError("the device is not ready")
        for x, y, secs in plan.get("slow", []):
            if a < y and b > x:
                time.sleep(secs)
        for x, y in plan.get("bad", []):
            if a < y and b > x:
                raise OSError("data error (cyclic redundancy check)")
        for i, (x, y, times) in enumerate(plan.get("flaky", [])):
            if a < y and b > x and self.counts.setdefault(i, 0) < times:
                self.counts[i] += 1
                raise OSError("data error (cyclic redundancy check)")
        self.f.seek(a)
        data = self.f.read(n)
        self.pos = a + len(data)
        return data

    def fileno(self) -> int:
        return self.f.fileno()

    def close(self) -> None:
        self.f.close()


def _open_source(source: str):
    plan = os.environ.get("PHOTORECOVER_FAULTS")       # tests: a JSON file describing a failing card
    if plan:
        return _Faulty(source, json.loads(Path(plan).read_text()))
    return open(source, "rb", buffering=0)


GIVE_UP_AFTER = 48          # this many chunks in a row unreadable: the card has probably dropped off
BLOCK = 64 << 10


def _put(path: Path, text: str, must: bool = True) -> None:
    """Replace a small file in one step. On Windows the swap is refused for a moment whenever another
    program (a virus scanner, or whatever is watching the progress) has the old file open, so it
    is tried again; a progress note that still cannot be written is skipped, not treated as a failure."""
    tmp = Path(str(path) + ".tmp")
    for attempt in range(40):
        try:
            tmp.write_text(text)
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 39 and must:
                raise
            time.sleep(0.05)


def clone(source: str, dest: Path, size: int | None = None, status_file: Path | None = None,
          progress=None, cancelled=None) -> dict:
    """Read the whole card into one image file.

    The card is opened read-only. A failing card may not survive much reading, so everything
    that reads easily is copied first; only then are the difficult patches gone back to, in
    smaller and smaller steps. What still cannot be read is left as zeros and listed beside the
    image (<image>.bad.json). If the card stops responding, what has been copied is kept, and
    running the same copy again carries on from there."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    map_file = Path(str(dest) + ".map.json")
    bad_file = Path(str(dest) + ".bad.json")
    t0 = time.time()
    sha = hashlib.sha256()
    bad: list[tuple[int, int]] = []          # (start, length) that could not be read at all
    later: list[tuple[int, int]] = []        # (start, length) skipped on the first pass
    done = 0                                  # how far the first pass has got
    in_order = True                           # the running hash is only right if nothing was skipped
    last_note = last_map = 0.0
    total = 0
    phase = "cloning"

    def note(state: str, **extra):
        missing = sum(n for _, n in bad) + sum(n for _, n in later)
        info = dict(state=state, done=done, total=total, bad_bytes=missing,
                    seconds=round(time.time() - t0, 1), **extra)
        if status_file:
            _put(Path(status_file), json.dumps(info), must=False)
        if progress:
            progress(info)
        return info

    def save_map():
        _put(map_file, json.dumps(dict(source=str(source), total=total, done=done, later=later, bad=bad)))

    def add(ranges: list, o: int, n: int) -> None:
        if ranges and ranges[-1][0] + ranges[-1][1] == o:
            ranges[-1] = (ranges[-1][0], ranges[-1][1] + n)
        else:
            ranges.append((o, n))

    resumed = None
    if map_file.exists() and dest.exists():
        try:
            resumed = json.loads(map_file.read_text())
        except (OSError, ValueError):
            resumed = None
    f = out = None
    created = False                 # only a file this run created may ever be removed by it
    try:
        if os.path.exists(source) and dest.exists() and os.path.samefile(source, dest):
            return note("error", error="same", message="The copy cannot be saved onto the very thing being copied. "
                                                        "Choose a different place to save it.")
    except OSError:
        pass
    try:
        f = _open_source(source)
        total = _device_size(f, source, size)
        if resumed and resumed.get("total") == total and total and resumed.get("source") == str(source):
            done = int(resumed["done"])
            later = [tuple(x) for x in resumed["later"]]
            bad = [tuple(x) for x in resumed["bad"]]
            in_order = False
            out = open(dest, "r+b")
        elif dest.exists() and dest.stat().st_size:
            # Never empty a file that is already there: it may be hours of reading a failing card.
            return note("error", error="exists",
                        message=("An unfinished copy of a different card (or of this card when it reported a "
                                 "different size) is already saved under this name. " if resumed else
                                 "A file with this name is already there. ")
                        + "Choose another name or folder, or move that file away first.")
        else:
            out = open(dest, "wb")
            created = True
        fpos = -1

        def read_at(o: int, n: int) -> bytes:
            nonlocal fpos
            if fpos != o:
                f.seek(o)
            fpos = -1
            data = f.read(n)
            fpos = o + len(data)
            return data

        # First pass: everything that reads without trouble.
        failures = 0
        while total == 0 or done < total:
            if cancelled and cancelled():
                out.close()
                dest.unlink(missing_ok=True)
                map_file.unlink(missing_ok=True)
                return note("cancelled")
            want = CHUNK if not total else min(CHUNK, total - done)
            want += -want % SECTOR
            try:
                buf = read_at(done, want)
            except OSError:
                buf = None
            if buf is None and not total:
                # size unknown: is this a bad patch, or the end of the card?
                got, readable = b"", 0
                for o in range(done, done + want, SECTOR):
                    try:
                        sec = read_at(o, SECTOR)
                    except OSError:
                        sec = b""
                    readable += len(sec) == SECTOR
                    if len(sec) < SECTOR:
                        add(bad, o, SECTOR)
                    got += sec + bytes(SECTOR - len(sec))
                if not readable:
                    while bad and bad[-1][0] + bad[-1][1] > done:       # nothing there: the card ends here
                        o, n = bad.pop()
                        if o < done:
                            bad.append((o, done - o))
                            break
                    break
                buf = got
            if buf is None:
                failures += 1
                add(later, done, want)
                in_order = False
                done += want
                if failures >= GIVE_UP_AFTER:
                    # Nothing has read for a long stretch. See whether the card is still there at all.
                    try:
                        f.close()
                        f = _open_source(source)
                        fpos = -1
                        read_at(0, SECTOR)
                        failures = 0
                    except OSError:
                        back = sum(n for o, n in later[-1:])
                        done -= back                  # that last stretch was the card going, not bad patches
                        later.pop()
                        save_map()
                        out.close()
                        return note("error", error="gone", resumable=True,
                                    message="The card stopped responding. What has been copied so far is kept. "
                                            "Take the card out, put it back in, and start the copy again: "
                                            "it will carry on from where it stopped.")
            elif not buf:
                break
            else:
                failures = 0
                if total and done + len(buf) > total:
                    buf = buf[:total - done]
                out.seek(done)
                out.write(buf)
                if in_order:
                    sha.update(buf)
                done += len(buf)
            now = time.time()
            if now - last_note > 0.5:
                last_note = now
                note(phase)
            if now - last_map > 5 and total:
                last_map = now
                out.flush()
                save_map()
        if total and done < total:
            save_map()
            out.close()
            return note("error", error="short", resumable=True,
                        message="The card stopped responding before the end. What has been copied so far is kept; "
                                "start the copy again to carry on.")
        total = total or done

        # Second pass: back to the difficult patches, in smaller steps, smallest last.
        phase = "retrying"
        for step in (BLOCK, SECTOR):
            todo, later = later, []
            for k, (o0, n0) in enumerate(todo):
                for o in range(o0, o0 + n0, step):
                    if cancelled and cancelled():           # enough: keep what there is
                        add(bad, o, o0 + n0 - o)
                        for o1, n1 in todo[k + 1:]:
                            add(bad, o1, n1)
                        todo = []
                        break
                    n = min(step, o0 + n0 - o, total - o)
                    if n <= 0:
                        break
                    try:
                        buf = read_at(o, n)
                    except OSError:
                        buf = b""
                    if len(buf) == n:
                        out.seek(o)
                        out.write(buf)
                    else:
                        add(later if step > SECTOR else bad, o, n)
                    if time.time() - last_note > 0.5:
                        last_note = time.time()
                        note(phase)
                if not todo:
                    break
            if step == SECTOR:
                later = []
        bad.sort()
        out.truncate(total)
        out.close()
        out = None
        if not in_order:
            sha = hashlib.sha256()
            with open(dest, "rb") as g:
                for block in iter(lambda: g.read(8 << 20), b""):
                    sha.update(block)
        map_file.unlink(missing_ok=True)
        if bad:
            bad_file.write_text(json.dumps(dict(unreadable=bad, total=total)))
        else:
            bad_file.unlink(missing_ok=True)
    except PermissionError as e:
        if done:            # it was reading until now, so this is not about administrator rights
            return note("error", error="io", resumable=True,
                        message=f"The copy was interrupted ({e}). What has been copied so far is kept: "
                                "start the copy again and it carries on from where it stopped.")
        return note("error", error="permission", message="Reading the card needs administrator rights.")
    except OSError as e:
        if not done and created:
            Path(dest).unlink(missing_ok=True)
        return note("error", error="io", resumable=bool(resumed),
                    message=f"Could not read the card: {e}" + (
                        ". The part already copied is kept; put the card back in and start the copy again."
                        if resumed else ""))
    finally:
        for h in (f, out):
            try:
                if h is not None:
                    h.close()
            except OSError:
                pass
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
    """How to start this program again for a helper job (works both as an .exe and from source).

    The Windows build has two programs side by side: PhotoRecovery.exe, the windowed app with no console, and
    photorecover-cli.exe, the command-line one. Helpers write to standard output, which a windowed program
    does not have, so they always run the command-line one when it is there."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        cli = exe.with_name("photorecover-cli" + exe.suffix)
        return [str(cli if cli.is_file() else exe)]
    return [sys.executable, "-m", "photorecover"]


def known_folder(which: str) -> Path:
    """The person's own Pictures or Desktop folder ("pictures" or "desktop"), wherever it really is.

    On Windows this asks Windows, because OneDrive and company setups often move these folders away from
    C:\\Users\\name\\Pictures. Falls back to the home folder if the folder does not exist."""
    home = Path.home()
    if sys.platform == "win32":
        try:
            import ctypes
            import uuid
            from ctypes import wintypes
            guid = {"pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
                    "desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641"}[which]

            class GUID(ctypes.Structure):
                _fields_ = [("bytes", ctypes.c_ubyte * 16)]

            g = GUID()
            ctypes.memmove(ctypes.byref(g), uuid.UUID(guid).bytes_le, 16)
            out = ctypes.c_wchar_p()
            shell32 = ctypes.windll.shell32
            shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                                     ctypes.POINTER(ctypes.c_wchar_p)]
            if shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) == 0:
                path = Path(out.value)
                ctypes.windll.ole32.CoTaskMemFree(out)
                if path.is_dir():
                    return path
        except Exception:  # noqa: BLE001 - fall back to the usual place
            pass
    path = home / {"pictures": "Pictures", "desktop": "Desktop"}[which]
    return path if path.is_dir() else home


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
