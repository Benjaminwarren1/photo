"""Compare two report.json files from runs on the same card image.

    python tools/compare_reports.py OLD/report.json NEW/report.json

Says what the new run gained and lost. A photo counts as the same photo when it was read
from the same places on the card. Exit code 1 if the new run lost any complete photo.
"""
import json
import sys

GOOD = ("Verified", "Complete, no preview")


def load(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return d["summary"], d["files"]


def main(old_path, new_path):
    (so, fo), (sn, fn) = load(old_path), load(new_path)
    key = lambda r: tuple(map(tuple, r["runs"]))
    go = {key(r): r for r in fo if r["grade"] in GOOD and r["runs"]}
    gn = {key(r): r for r in fn if r["grade"] in GOOD and r["runs"]}
    lost, gained = [go[k] for k in go.keys() - gn.keys()], [gn[k] for k in gn.keys() - go.keys()]
    out = dict(old=dict(seconds=so["seconds"], counts=so["counts"]), new=dict(seconds=sn["seconds"], counts=sn["counts"]),
               complete_old=len(go), complete_new=len(gn),
               lost=[dict(name=r["name"], pieces=len(r["runs"]), start=r["start_cluster"]) for r in lost],
               gained=[dict(name=r["name"], pieces=len(r["runs"]), start=r["start_cluster"]) for r in gained],
               steps=sn.get("steps", []), problems=sn.get("problems", []))
    print(json.dumps(out, indent=1))
    return 1 if lost else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
