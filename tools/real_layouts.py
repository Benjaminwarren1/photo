"""Build card images with real formatting tools (not the test suite's own builders) and check
that photorecover gets every deleted file back byte for byte.

Linux only, run as root. Needs: sgdisk, sfdisk, mkfs.fat, mkfs.exfat, mtools, exfat-fuse,
mkfs.ext4 and loop devices. Each layout copies photos and videos on with the real
filesystem code (mtools for FAT, exfat-fuse for exFAT, the kernel for ext4), deletes some,
writes more so later files land in the gaps (fragmented), deletes some of those too, then
runs `photorecover recover` and compares what came out with what was deleted.

    sudo python tools/real_layouts.py [WORKDIR] [layout ...]
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from photorecover.synth.photos import make_photo
from photorecover.synth.videos import make_video

MB = 1 << 20


def sh(*cmd, **kw):
    return subprocess.run([str(c) for c in cmd], check=True, capture_output=True, text=True, **kw).stdout


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Part:
    """One partition: start/size in bytes and how to put files on it."""

    def __init__(self, img, start, size, fs):
        self.img, self.start, self.size, self.fs = img, start, size, fs
        self.loop = self.mnt = None

    def mkfs(self):
        if self.fs == "fat32":
            sh("mkfs.fat", "-F", "32", "-s", "8", "--offset", self.start // 512, self.img, self.size // 1024)
        else:
            self._attach()
            sh({"exfat": "mkfs.exfat", "ext4": "mkfs.ext4"}[self.fs], *(["-q"] if self.fs == "ext4" else []), self.loop)
            self._detach()

    def _attach(self):
        self.loop = sh("losetup", "-f", "--show", "-o", self.start, "--sizelimit", self.size, self.img).strip()

    def _detach(self):
        sh("losetup", "-d", self.loop)
        self.loop = None

    def __enter__(self):
        if self.fs != "fat32":
            self._attach()
            self.mnt = tempfile.mkdtemp()
            if self.fs == "exfat":
                sh("mount.exfat-fuse", self.loop, self.mnt)
            else:
                sh("mount", self.loop, self.mnt)
        return self

    def __exit__(self, *a):
        if self.mnt:
            sh("umount", self.mnt)
            os.rmdir(self.mnt)
            self.mnt = None
            self._detach()

    def _m(self, path):
        return f"{self.img}@@{self.start}"

    def mkdir(self, d):
        if self.mnt:
            os.makedirs(os.path.join(self.mnt, d), exist_ok=True)
        else:
            parts = d.split("/")
            for i in range(1, len(parts) + 1):
                subprocess.run(["mmd", "-i", self._m(d), "::/" + "/".join(parts[:i])], capture_output=True)

    def put(self, path, data):
        if self.mnt:
            with open(os.path.join(self.mnt, path), "wb") as f:
                f.write(data)
        else:
            with tempfile.NamedTemporaryFile(delete=False) as t:
                t.write(data)
            sh("mcopy", "-i", self._m(path), t.name, "::/" + path)
            os.unlink(t.name)

    def rm(self, path):
        if self.mnt:
            os.unlink(os.path.join(self.mnt, path))
        else:
            sh("mdel", "-i", self._m(path), "::/" + path)


def fill(part, seed, folder="DCIM/100PHOTO"):
    """Copy on files, delete some, copy more (they fall into the gaps), delete some of those.
    Returns {sha: name} of everything deleted and never written over."""
    part.mkdir(folder)
    files = {}
    for i in range(12):
        files[f"{folder}/IMG_{seed:02d}{i:02d}.JPG"] = make_photo(seed * 100 + i, 1600 + 64 * (i % 3), 1200)
    files[f"{folder}/MOV_{seed:02d}00.MP4"] = make_video(seed, frames=150)
    for name, data in files.items():
        part.put(name, data)
    deleted = {}
    for i, name in enumerate(list(files)):
        if i % 3 == 1:
            part.rm(name)
            deleted[name] = files.pop(name)
    # later, bigger photos: the filesystem puts them into the holes, so they are in pieces
    later = {f"{folder}/IMG_{seed:02d}{50 + i:02d}.JPG": make_photo(seed * 100 + 50 + i, 2400, 1800) for i in range(3)}
    for name, data in later.items():
        part.put(name, data)
    files.update(later)
    # anything deleted earlier may now be partly written over: drop those from the answer key
    deleted = {}
    for i, name in enumerate(list(files)):
        if i % 2 == 0:
            part.rm(name)
            deleted[name] = files.pop(name)
    return {sha(d): n for n, d in deleted.items()}, {sha(d): n for n, d in files.items()}


def blank(img, size):
    with open(img, "wb") as f:
        f.truncate(size)


def gpt(img, parts):
    """parts: list of (size_bytes or 0 for rest, typecode, name)."""
    sh("sgdisk", "-o", img)
    for i, (size, code, name) in enumerate(parts, 1):
        sh("sgdisk", "-n", f"{i}:0:" + (f"+{size // 1024}K" if size else "0"), "-t", f"{i}:{code}", "-c", f"{i}:{name}", img)
    out = []
    for i in range(1, len(parts) + 1):
        info = sh("sgdisk", "-i", i, img)
        first = int(info.split("First sector: ")[1].split()[0])
        last = int(info.split("Last sector: ")[1].split()[0])
        out.append((first * 512, (last - first + 1) * 512))
    return out


def mbr(img, script):
    sh("sfdisk", "-q", img, input=script)
    out = json.loads(sh("sfdisk", "-J", img))["partitiontable"]["partitions"]
    return [(p["start"] * 512, p["size"] * 512, p["type"]) for p in out]


def layout(name, img):
    """Builds one layout. Returns (deleted, live, expect) where expect is a note on what should happen."""
    deleted, live, notes = {}, {}, []

    def use(part, seed, **kw):
        part.mkfs()
        with part as p:
            d, l = fill(p, seed, **kw)
        deleted.update(d)
        live.update(l)

    if name == "gpt-windows":          # what Windows' Disk Management makes on a GPT card
        blank(img, 128 * MB)
        (ms, mz), (s, z) = gpt(img, [(16 * MB, "0c01", "Microsoft reserved partition"), (0, "0700", "Basic data partition")])
        use(Part(img, s, z, "fat32"), 1)
    elif name == "gpt-fat32-exfat":
        blank(img, 192 * MB)
        (s1, z1), (s2, z2) = gpt(img, [(80 * MB, "0700", "PHOTOS"), (0, "0700", "VIDEO")])
        use(Part(img, s1, z1, "fat32"), 2)
        use(Part(img, s2, z2, "exfat"), 3)
    elif name == "mbr-logical-exfat":
        blank(img, 192 * MB)
        p = mbr(img, "label: dos\n,80M,c\n,,5\n,,7\n")
        p = [x for x in p if x[2] not in ("5", "f")]
        use(Part(img, p[0][0], p[0][1], "fat32"), 4)
        use(Part(img, p[1][0], p[1][1], "exfat"), 5)
    elif name == "sdxc-exfat":          # SD Association formatter style: one exFAT partition 4 MB in
        blank(img, 160 * MB)
        p = mbr(img, "label: dos\nstart=8192,type=7\n")
        use(Part(img, p[0][0], p[0][1], "exfat"), 6)
    elif name == "superfloppy-exfat":   # no partition table at all
        blank(img, 128 * MB)
        use(Part(img, 0, 128 * MB, "exfat"), 7)
    elif name == "raspberry-pi":        # FAT32 boot + ext4 root, as Raspberry Pi Imager writes
        blank(img, 256 * MB)
        p = mbr(img, "label: dos\nstart=8192,size=128M,type=c\n,,83\n")
        use(Part(img, p[0][0], p[0][1], "fat32"), 8, folder="photos")
        e = Part(img, p[1][0], p[1][1], "ext4")
        e.mkfs()
        with e as q:
            q.mkdir("home/pi")
            q.put("home/pi/cam.jpg", make_photo(899))
            q.rm("home/pi/cam.jpg")
        notes.append("photo deleted from ext4 is not expected back; the run should say a part could not be read")
    elif name == "android-adoptable":   # phone 'internal storage' card: encrypted, nothing to find
        blank(img, 96 * MB)
        (s1, z1), (s2, z2) = gpt(img, [(16 * MB, "8300", "android_meta"), (0, "8300", "android_expand")])
        with open(img, "r+b") as f:
            f.seek(s2)
            for _ in range(z2 // MB):
                f.write(os.urandom(MB))
        notes.append("expect an 'encrypted' finding")
    else:
        raise SystemExit(f"unknown layout {name}")
    return deleted, live, notes


LAYOUTS = ["gpt-windows", "gpt-fat32-exfat", "mbr-logical-exfat", "sdxc-exfat", "superfloppy-exfat",
           "raspberry-pi", "android-adoptable"]


def run(work, name):
    img = work / f"{name}.img"
    out = work / f"{name} recovered"
    deleted, live, notes = layout(name, str(img))
    r = subprocess.run([sys.executable, "-m", "photorecover", "recover", str(img), "--out", str(out), "--json"],
                       capture_output=True, text=True)
    if r.returncode:
        return dict(layout=name, error=r.stderr[-2000:])
    summary = json.loads(r.stdout)
    got = {}
    for p in out.rglob("*"):
        if p.is_file() and p.parent != out:
            got[sha(p.read_bytes())] = str(p.relative_to(out))
    back = [n for h, n in deleted.items() if h in got]
    missing = sorted(n for h, n in deleted.items() if h not in got)
    return dict(layout=name, deleted=len(deleted), back_exact=len(back), missing=missing,
                live_on_card=len(live), still_on_card=summary.get("still_on_card"),
                findings=summary.get("findings"), notes=notes, counts=summary.get("counts"))


def main():
    args = sys.argv[1:]
    work = Path(args.pop(0)) if args and not args[0] in LAYOUTS else Path(tempfile.mkdtemp())
    work.mkdir(parents=True, exist_ok=True)
    results = [run(work, n) for n in (args or LAYOUTS)]
    print(json.dumps(results, indent=1))
    return 1 if any(r.get("error") or (r["deleted"] and r["missing"]) for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
