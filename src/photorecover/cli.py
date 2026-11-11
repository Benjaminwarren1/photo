"""Command line: photorecover recover <card image> [--out folder] [--json]"""
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
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("recover", help="recover everything possible from a card image")
    r.add_argument("image", type=Path, help="raw image of the card (a .img or .dd file)")
    r.add_argument("--out", type=Path, help="output folder (default: '<image name> recovered' next to the image)")
    r.add_argument("--json", action="store_true", help="print the summary as JSON, for other programs")
    a = ap.parse_args(argv)

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
