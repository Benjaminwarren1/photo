"""Command line.

photorecover                      start the wizard (also what double-clicking the .exe does)
photorecover recover IMAGE        recover from a card image; --json prints a machine-readable summary
photorecover drives               list attached drives
photorecover clone SOURCE DEST    copy a card to an image file (used by the wizard, needs admin rights)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .fat import NotFat
from .recover import Recovery


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="photorecover",
                                 description="Rescue deleted photos from a memory card image. The image is never changed.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")
    w = sub.add_parser("wizard", help="start the step-by-step wizard in your browser (the default)")
    w.add_argument("--no-browser", action="store_true")
    sub.add_parser("drives", help="list attached drives")
    c = sub.add_parser("clone", help="copy a whole card to an image file, read-only")
    c.add_argument("source")
    c.add_argument("dest", type=Path)
    c.add_argument("--size", type=int)
    c.add_argument("--status", type=Path)
    k = sub.add_parser("pick")
    k.add_argument("kind", choices=["file", "folder"])
    r = sub.add_parser("recover", help="recover everything possible from a card image")
    r.add_argument("image", type=Path, help="raw image of the card (a .img or .dd file)")
    r.add_argument("--out", type=Path, help="output folder (default: '<image name> recovered' next to the image)")
    r.add_argument("--json", action="store_true", help="print the summary as JSON, for other programs")
    a = ap.parse_args(argv)

    if a.cmd in (None, "wizard"):
        from . import wizard
        return wizard.run(open_browser=not getattr(a, "no_browser", False))
    if a.cmd == "drives":
        from . import device
        for d in device.list_drives():
            kind = "system" if d.system else "removable" if d.removable else "fixed"
            print(f"{d.id:18} {kind:10} {d.label()}")
        return 0
    if a.cmd == "clone":
        from . import device
        show = None if a.status else (lambda i: print(f"\r{i['done'] >> 20} MB", end="", flush=True))
        info = device.clone(a.source, a.dest, a.size, a.status, show)
        if not a.status:
            print("\n" + (info.get("message") or f"Done. SHA-256 {info.get('sha256')}"))
        return 0 if info["state"] == "done" else 1
    if a.cmd == "pick":
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory() if a.kind == "folder" else filedialog.askopenfilename(
            filetypes=[("Card images", "*.img *.dd *.raw *.bin"), ("All files", "*.*")])
        print(path or "")
        return 0
    if not a.image.is_file():
        print(f"Cannot find the image file: {a.image}", file=sys.stderr)
        return 2
    out = a.out or a.image.with_name(a.image.stem + " recovered")
    if out.resolve() == a.image.resolve():
        print("The output folder cannot be the image itself.", file=sys.stderr)
        return 2
    log = (lambda m: None) if a.json else (lambda m: print(m, flush=True))
    try:
        rec = Recovery(a.image, out, log)
    except NotFat as e:
        print(f"Cannot read this card image: {e}", file=sys.stderr)
        return 3
    summary = rec.run()
    if a.json:
        print(json.dumps(dict(summary, out=str(out))))
    else:
        print()
        for grade, n in summary["counts"].items():
            print(f"  {n:6d}  {grade}")
        print(f"\nSaved to: {out}\nFull details: {out / 'report.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
