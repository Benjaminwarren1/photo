"""Camera RAW files and multi-picture files: how long one is, and whether it is intact.

Most RAW formats (Nikon NEF and NRW, Canon CR2, Sony ARW, Pentax PEF, Adobe DNG, and plain TIFF)
are a TIFF file: a chain of tables, each listing where a block of the file lies and how long it
is. Following every table gives the file's true length, and shows whether everything it points
to is still there. The raw sensor data itself carries no checksum, so the strongest check
available is that the tables hold together and the previews stored inside the file open."""
from __future__ import annotations

import io
import struct
import warnings

_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
RAW_EXT = {"orf", "rw2", "nef", "nrw", "cr2", "arw", "sr2", "srf", "pef", "dng", "tif", "tiff", "3fr", "erf", "kdc", "dcr", "iiq"}
_SUB_TABLES = (330, 34665, 34853)        # SubIFDs, Exif, GPS


class Tiff:
    """The tables of a TIFF-based file that starts at pos in buf."""

    def __init__(self, buf, pos: int = 0, end: int | None = None):
        self.buf, self.pos = buf, pos
        self.end = len(buf) if end is None else end
        head = bytes(buf[pos:pos + 8])
        # 42 marks plain TIFF; Olympus (ORF) and Panasonic (RW2) use the same layout under other numbers
        if head[:4] in (b"II*\0", b"IIRO", b"IIRS", b"IIU\0"):
            self.e = "<"
        elif head[:4] in (b"MM\0*", b"MMOR"):
            self.e = ">"
        else:
            raise ValueError("not a TIFF")
        self.extent = 8               # how far into the file anything listed reaches
        self.tables = 0
        self.make = ""
        self.dng = False
        self.blocks: list[tuple[int, int, int]] = []     # (offset, length, compression) of picture data
        self.jpegs: list[tuple[int, int]] = []           # (offset, length) of previews stored as JPEG
        self.ok = True
        seen: set[int] = set()
        todo = [struct.unpack(self.e + "I", head[4:8])[0]]
        while todo and self.tables < 64:
            off = todo.pop(0)
            if off in seen or off < 8:
                continue
            seen.add(off)
            todo += self._table(off)

    def _values(self, typ: int, count: int, field: bytes, limit: int = 4096) -> list[int] | bytes | None:
        size = _SIZE.get(typ)
        if size is None:
            return None
        total = size * count
        if total <= 4:
            raw = field[:total]
        else:
            (off,) = struct.unpack(self.e + "I", field)
            if off + total > self.end - self.pos:
                self.ok = False
                return None
            self.extent = max(self.extent, off + total)
            if count > limit and typ not in (1, 2, 7):
                count, total = limit, limit * size
            raw = bytes(self.buf[self.pos + off:self.pos + off + min(total, 1 << 16)])
        if typ in (1, 2, 6, 7):
            return raw
        code = {3: "H", 4: "I", 8: "h", 9: "i", 13: "I"}.get(typ)
        if code is None:
            return None
        n = min(count, len(raw) // size)
        return list(struct.unpack(self.e + code * n, raw[:n * size]))

    def _table(self, off: int) -> list[int]:
        buf, pos, e = self.buf, self.pos, self.e
        if off + 2 > self.end - pos:
            self.ok = False
            return []
        (n,) = struct.unpack(e + "H", bytes(buf[pos + off:pos + off + 2]))
        if not 1 <= n <= 1000 or off + 2 + n * 12 + 4 > self.end - pos:
            self.ok = False
            return []
        self.tables += 1
        self.extent = max(self.extent, off + 2 + n * 12 + 4)
        raw = bytes(buf[pos + off + 2:pos + off + 2 + n * 12 + 4])
        tags: dict[int, list[int] | bytes] = {}
        for i in range(n):
            tag, typ, count = struct.unpack(e + "HHI", raw[i * 12:i * 12 + 8])
            if count > 1 << 28:
                self.ok = False
                continue
            v = self._values(typ, count, raw[i * 12 + 8:i * 12 + 12])
            if v is not None:
                tags[tag] = v
        more = []
        for t in _SUB_TABLES:
            if isinstance(tags.get(t), list):
                more += tags[t]
        (nxt,) = struct.unpack(e + "I", raw[n * 12:n * 12 + 4])
        if nxt:
            more.append(nxt)
        if isinstance(tags.get(271), bytes) and not self.make:
            self.make = tags[271].split(b"\0")[0].decode("latin1").strip()
        self.dng = self.dng or 50706 in tags
        comp = tags.get(259, [1])
        comp = comp[0] if isinstance(comp, list) and comp else 1
        for where, length in ((273, 279), (324, 325)):
            a, b = tags.get(where), tags.get(length)
            if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
                for o, ln in zip(a, b):
                    self._block(o, ln, comp)
        a, b = tags.get(513), tags.get(514)
        if isinstance(a, list) and isinstance(b, list) and a and b:
            if self._block(a[0], b[0], 6):
                self.jpegs.append((a[0], b[0]))
        return more

    def _block(self, o: int, ln: int, comp: int) -> bool:
        if ln == 0:
            return False
        if o < 8 or o + ln > self.end - self.pos:
            self.ok = False
            return False
        self.extent = max(self.extent, o + ln)
        self.blocks.append((o, ln, comp))
        if comp in (6, 7) and bytes(self.buf[self.pos + o:self.pos + o + 2]) == b"\xff\xd8" and (o, ln) not in self.jpegs:
            if comp == 6 or ln < 8 << 20:           # a preview, not lossless-JPEG raw data
                self.jpegs.append((o, ln))
        return True

    @property
    def ext(self) -> str:
        head = bytes(self.buf[self.pos + 8:self.pos + 10])
        make = self.make.upper()
        first = bytes(self.buf[self.pos:self.pos + 4])
        if first in (b"IIRO", b"IIRS", b"MMOR"):
            return "orf"
        if first == b"IIU\0":
            return "rw2"
        if self.dng:
            return "dng"
        if head == b"CR":
            return "cr2"
        for name, ext in (("NIKON", "nef"), ("SONY", "arw"), ("PENTAX", "pef"), ("RICOH", "pef"), ("CANON", "cr2"),
                          ("HASSELBLAD", "3fr"), ("KODAK", "dcr")):
            if name in make:
                return ext
        return "tif"


def tiff_length(buf, pos: int, end: int) -> tuple[int, str] | None:
    """(length, usual file ending) of the TIFF-based picture that starts at pos, if its tables hold
    together and it is big enough to be a picture rather than a scrap of one."""
    try:
        t = Tiff(buf, pos, end)
    except (ValueError, struct.error):
        return None
    data = sum(ln for _, ln, _ in t.blocks)
    if not t.ok or not t.blocks or t.tables < 1 or data < 16384:
        return None
    return t.extent, t.ext


def check_tiff(data, size: int | None = None, audit=None) -> tuple[str, str] | None:
    """For a file known by name to be a RAW or TIFF: ('verified' or 'unverified', note), or None
    when what is there is clearly not this file any more."""
    from PIL import Image
    try:
        t = Tiff(data, 0, len(data))
    except (ValueError, struct.error):
        return None
    if not t.ok:
        # this reading of the tables has not been proved on every camera, so a file is never
        # thrown away on it: it is kept, and the doubt is said
        return "unverified", "its internal layout does not add up, so it may be damaged; open it to check"
    opened = 0
    for o, ln in t.jpegs:
        part = bytes(data[o:o + ln])
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                with Image.open(io.BytesIO(part)) as im:
                    im.load()
        except Exception:  # noqa: BLE001
            return None                     # a preview inside the file is damaged: so is the file
        if audit is not None and not audit(part)[0]:
            # the strict check has only been proved on ordinary photos, so a RAW file is not thrown
            # away on its word alone: it is kept, and the doubt is said
            return "unverified", ("a preview picture stored inside it looks damaged, so the file probably is too; "
                                  "open it to check")
        opened += 1
    whole = size is None or t.extent >= size - max(65536, size // 50)
    if opened and whole:
        return "unverified", (f"its layout is intact and the {opened} preview picture{'s' if opened > 1 else ''} stored "
                              "inside it open correctly; the raw sensor data itself carries no check, so open it to make sure")
    return "unverified", "its layout is intact; the raw sensor data itself carries no check, so open it to make sure"


def mpo_parts(data) -> list[tuple[int, int]] | None:
    """(offset, length) of each picture in a multi-picture file (a 3D photo), from its own index."""
    if bytes(data[:2]) != b"\xff\xd8":
        return None
    p = 2
    while p + 4 <= min(len(data), 1 << 20):
        if data[p] != 0xFF:
            return None
        marker, (ln,) = data[p + 1], struct.unpack(">H", bytes(data[p + 2:p + 4]))
        if marker == 0xDA:
            return None
        if marker == 0xE2 and bytes(data[p + 4:p + 8]) == b"MPF\0":
            base = p + 8
            e = "<" if bytes(data[base:base + 2]) == b"II" else ">"
            (ifd,) = struct.unpack(e + "I", bytes(data[base + 4:base + 8]))
            (n,) = struct.unpack(e + "H", bytes(data[base + ifd:base + ifd + 2]))
            for i in range(min(n, 32)):
                tag, typ, count, val = struct.unpack(e + "HHII", bytes(data[base + ifd + 2 + i * 12:base + ifd + 14 + i * 12]))
                if tag == 0xB002 and count % 16 == 0 and count:
                    out = []
                    for k in range(count // 16):
                        _attr, size, off = struct.unpack(e + "III", bytes(data[base + val + k * 16:base + val + k * 16 + 12]))
                        out.append((0 if off == 0 else base + off, size))
                    return out
            return None
        p += 2 + ln
    return None


def check_mpo(data, audit) -> tuple[str, str] | None:
    """A 3D photo is whole when each picture in it passes the same final check as any photo."""
    parts = mpo_parts(data)
    if not parts:
        return "unverified", "its list of pictures could not be read; open it to make sure"
    for n, (o, ln) in enumerate(parts, 1):
        if o + ln > len(data) or bytes(data[o:o + 2]) != b"\xff\xd8":
            return None
        ok, why, _ = audit(bytes(data[o:o + ln]))
        if not ok:
            return None if n == 1 else ("unverified", f"picture {n} of {len(parts)} inside it does not pass its check ({why})")
    return "verified", f"all {len(parts)} pictures inside it pass their checks"
