"""What an AI assistant (or any other program) needs to drive Photo Lifeboat for someone.

The command line and the MCP server (mcp_server.py) both use this: the guide an assistant reads
first, progress as JSON lines, drives as plain data, and contact sheets, which put many recovered
photos into one picture so an assistant can look them over by eye cheaply.

Nothing here writes to a card. The card is only ever read (by clone), and everything else works on
the copy of it.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

INTRO = """\
# Photo Lifeboat: guide for AI assistants

You are helping someone get deleted photos back from a memory card (or USB stick). This tool
was written after an AI assistant wiped a person's SD card, so the rules below matter.

"""

RULES = """\
## Rules
1. Never write to the card. Do not format it, repair it, run chkdsk / fsck / diskpart / dd
   onto it, copy files onto it, or "fix" it in any way. Every write can destroy photos for good.
   This tool only ever reads the card; keep it that way.
2. Tell the person not to use the card (no new photos, no formatting) until recovery is done.
3. Save everything on a different drive from the card (the computer's own disk is best).
4. Looking at a photo yourself sends that photo to your AI provider. Ask the person before you
   look at any recovered photo, and say that plainly. If they say no, use only the numbers and the
   automatic checks (report.json, `check`), and let them look at the photos themselves.
5. Explain each step in plain words. The person may not be technical and may be upset.

"""

CLI_STEPS = """\
## Steps (command line; add --json to any of them for machine-readable output)
1. `photolifeboat-cli drives --json` lists drives. The card is usually "removable": true and
   never "system": true. Running it before and after the card is plugged in shows which is new.
   Confirm the drive with the person (name and size) before going on.
2. `photolifeboat-cli clone <drive id> "<folder on another drive>\\card.img" --json` copies the
   whole card into one image file, read-only. On Windows this shows one permission prompt that
   the person must click Yes on. Progress comes as JSON lines; the last line has "state".
   If the card is failing and the copy stops, running the same command again carries on.
   If the person already has an image file, skip this step.
3. `photolifeboat-cli recover card.img --out "<results folder>" --json --progress` finds,
   rebuilds and checks everything. Progress is JSON lines on standard error; the summary is one
   JSON object on standard output. It can take from a few minutes to an hour or more.
4. Close calls: if the summary says "Needs review" photos, `photolifeboat-cli pilot card.img
   --out "<results folder>"` re-runs the recovery and then takes JSON lines on standard input:
   {"cmd": "unresolved"}, {"cmd": "candidates", "index": N} (saves a picture of each possible
   next piece and the photo's own small preview), {"cmd": "pick", "index": N, "cluster": C or
   null}, {"cmd": "quit"}. Pick the candidate whose picture carries on smoothly from the part
   above it and matches the preview; pick null if none do. A pick is only kept if the finished
   photo then passes the same checks as any other.
5. Check by eye (only with the person's OK, see rule 4): `photolifeboat-cli sheet
   "<results folder>" --json` makes contact sheets, numbered grids of the recovered photos, and
   says which file each number is. Look for photos that are garbled, have a grey or smeared
   band, or are two pictures joined together. The automatic checks catch nearly all of these,
   so anything you spot is worth telling the person about (and reporting as a bug).
6. Tell the person where their photos are: "<results folder>\\Recovered\\Photos" for the good
   ones, "Recovered - Partial" for incomplete ones, and report.html for the full list in plain
   words. Partial photos also get a copy with the missing part filled in from the preview, in
   "Recovered - Preview filled".

## Exit codes
0 done; 1 a check or copy failed; 2 bad arguments (for example the output is on the card);
3 the image cannot be read as a card; 4 recovered, but not all results could be saved (the
output drive is probably full).

"""

RESULTS = """\
## Results, in plain words
Verified: whole, and matches the photo's own preview. Complete, no preview: whole by every
check, but there was no preview to compare with. Recovered, unverified: a kind of file that
cannot be checked; open it to make sure. Partial: the good part is saved, the rest is not on the
card. Needs review: a close call left to a person (or to you, step 4). Not recoverable: nothing
usable is left. Still on the card: never deleted; copied as it is.

If nothing came back, report.html explains why (for example the card was wiped, encrypted by
the phone, or overwritten). Do not promise anything other tools or labs cannot deliver: a photo
that has been written over is gone.
"""

MCP_STEPS = """\
## Steps (with this server's tools)
1. list_drives, before and after the card is plugged in: the new one, "removable" and never
   "system", is the card. Confirm its name and size with the person before going on.
2. copy_card copies the whole card into one image file on another drive, read-only. On Windows
   the person must click Yes on one permission prompt. Follow it with job_status. If the person
   already has an image file, skip this step.
3. recover finds, rebuilds and checks everything in the image. Follow it with job_status (every
   minute or so is plenty; it can take from a few minutes to an hour or more), then read results.
4. close_calls lists photos the tool would not finish on its own. show_close_call shows the
   part of the photo above the join with each possible next piece, plus the photo's own small
   preview. Pick the piece that carries on smoothly and matches the preview with
   decide_close_call, or cluster null if none do. A pick is only kept if the finished photo then
   passes the same checks as any other. (This means looking at photos: see rule 4.)
5. Check by eye (only with the person's OK, see rule 4): contact_sheet shows 20 recovered photos
   at a time, numbered; view_photo shows one larger. Look for photos that are garbled, have a grey
   or smeared band, or are two pictures joined together. The automatic checks catch nearly all of
   these, so anything you spot is worth telling the person about (and reporting as a bug).
6. Tell the person where their photos are: the results folder's "Recovered/Photos" for the good
   ones, "Recovered - Partial" for incomplete ones, and report.html for the full list in plain
   words.
"""

GUIDE = INTRO + RULES + CLI_STEPS + RESULTS
MCP_GUIDE = INTRO + RULES + MCP_STEPS + RESULTS


def drive_info(d) -> dict:
    """One drive as plain data, with a label a person would recognise."""
    return dict(id=d.id, path=d.path, size=d.size, name=d.name, removable=d.removable, system=d.system,
                label=d.label())


def emit(obj: dict, stream=None) -> None:
    """One JSON object on its own line, flushed straight away so the reader sees it now."""
    stream = stream or sys.stderr
    stream.write(json.dumps(obj) + "\n")
    stream.flush()


class Progress:
    """Reports a recovery as it runs: a "step" line as each step starts, then "progress" lines
    (done, total within that step) at most once every `every` seconds.

    Use it as the recovery's log function, and start() it once the Recovery exists."""

    def __init__(self, stream=None, every: float = 2.0):
        self.stream, self.every = stream, every
        self.rec = None
        self._stop = threading.Event()
        self._t0 = time.time()

    def __call__(self, message: str) -> None:
        emit(dict(event="step", text=message, seconds=round(time.time() - self._t0, 1)), self.stream)

    def start(self, rec) -> "Progress":
        self.rec = rec
        threading.Thread(target=self._watch, daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _watch(self) -> None:
        last = None
        while not self._stop.wait(self.every):
            p = getattr(self.rec, "progress", None)
            if p and p != last:
                last = p
                emit(dict(event="progress", done=p[0], total=p[1], seconds=round(time.time() - self._t0, 1)),
                     self.stream)


# ---- contact sheets ------------------------------------------------------------------------------

SHEET_KINDS = {
    "complete": ("Verified", "Complete, no preview"),
    "partial": ("Partial", "Needs review"),
    "all": ("Verified", "Complete, no preview", "Partial", "Needs review", "Still on the card"),
}
PHOTO_TYPES = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic")


def sheet_entries(out: Path, which: str = "complete") -> list[dict]:
    """The photos in a results folder that a contact sheet should show, from its report.json."""
    report = json.loads((Path(out) / "report.json").read_text(encoding="utf-8"))
    grades = SHEET_KINDS[which]
    rows = [r for r in report["files"] if r.get("saved_as") and r["grade"] in grades
            and Path(r["saved_as"]).suffix.lower() in PHOTO_TYPES
            and "Small pictures" not in r["saved_as"]]
    return [dict(file=r["saved_as"], name=r["name"], grade=r["grade"], percent=r.get("percent")) for r in rows]


def make_sheets(out: Path, to: Path, which: str = "complete", per_sheet: int = 20, cell: int = 300,
                start: int = 0, limit: int | None = None) -> list[dict]:
    """Numbered grids of recovered photos, saved as JPEGs in `to`. Returns, for each sheet, its
    path and which photo each number on it is. Numbers count from 1 across all the sheets, so
    "photo 37" means the same thing whichever sheet it is on.

    Only reads the results folder; the sheets go wherever `to` says (by default not inside the
    results, so the person's folder stays tidy)."""
    from PIL import Image, ImageDraw, ImageOps
    out, to = Path(out), Path(to)
    to.mkdir(parents=True, exist_ok=True)
    entries = sheet_entries(out, which)[start:]
    if limit is not None:
        entries = entries[:limit]
    cols = 5 if per_sheet >= 10 else max(1, per_sheet)
    label_h = 22
    sheets = []
    for s in range(0, len(entries), per_sheet):
        chunk = entries[s:s + per_sheet]
        rows = (len(chunk) + cols - 1) // cols
        page = Image.new("RGB", (cols * cell, rows * (cell + label_h)), "white")
        draw = ImageDraw.Draw(page)
        listed = []
        for k, e in enumerate(chunk):
            n = start + s + k + 1
            x, y = (k % cols) * cell, (k // cols) * (cell + label_h)
            try:
                with Image.open(out / e["file"]) as im:
                    im.draft("RGB", (cell, cell))        # JPEGs decode straight at a small size: fast
                    im = ImageOps.exif_transpose(im).convert("RGB")
                    im.thumbnail((cell - 6, cell - 6))
                    page.paste(im, (x + (cell - im.width) // 2, y + label_h + (cell - im.height) // 2))
            except Exception as err:  # noqa: BLE001 - a broken file is shown as broken, never stops the sheet
                draw.text((x + 8, y + label_h + 8), f"cannot open: {type(err).__name__}", fill="red")
            tag = f"{n}" + ("" if e["grade"] in SHEET_KINDS["complete"] else f"  {e['grade']}")
            draw.rectangle([x, y, x + cell - 1, y + label_h - 1], fill="black")
            draw.text((x + 6, y + 5), tag, fill="white")
            listed.append(dict(number=n, **e))
        path = to / f"sheet-{len(sheets) + 1 + start // per_sheet:03d}.jpg"
        page.save(path, quality=80)
        sheets.append(dict(sheet=str(path), photos=listed))
    return sheets
