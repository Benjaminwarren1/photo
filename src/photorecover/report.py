"""Writes report.html: one line per file, in plain words."""
from __future__ import annotations

import html
from pathlib import Path

ORDER = ["Verified", "Complete, no preview", "Recovered, unverified", "Partial", "Needs review", "Not recoverable"]
MEANING = {
    "Verified": "Every check passed, including the match against the photo's own preview.",
    "Complete, no preview": "Structure is complete and correct. There was no preview to compare the picture with.",
    "Recovered, unverified": "Came back whole, but this file type cannot be checked. Open it to make sure.",
    "Partial": "The good part is saved. The rest is not on the card.",
    "Needs review": "A close call the tool would not make on its own.",
    "Not recoverable": "Nothing usable is left on the card for this file.",
}
CSS = """
body{font:15px/1.5 system-ui,Segoe UI,Arial,sans-serif;margin:2rem auto;max-width:70rem;padding:0 1rem;color:#1c1c1c;background:#fff}
h1{font-size:1.5rem}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.35rem .6rem;border-bottom:1px solid #ddd;vertical-align:top}
th{background:#f3f3f3}.g0,.g1{color:#136c2e}.g2{color:#7a5b00}.g3,.g4{color:#a14a00}.g5{color:#a11}
.sum td{font-size:1.05rem}small{color:#555}
@media(prefers-color-scheme:dark){body{background:#161616;color:#eee}th{background:#262626}th,td{border-color:#3a3a3a}
.g0,.g1{color:#6fd08a}.g2{color:#e2c15a}.g3,.g4{color:#f0a060}.g5{color:#ff8080}small{color:#aaa}}
"""


def write_html(path: Path, summary: dict, rows: list[dict]) -> None:
    e = html.escape
    counts = summary.get("counts", {})
    out = ["<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>",
           "<title>Photo recovery report</title><style>", CSS, "</style>",
           "<h1>Photo recovery report</h1>",
           f"<p><small>{e(summary.get('tool', ''))} · {e(summary.get('filesystem', ''))} card · "
           f"{summary.get('files_listed', 0)} files · took {summary.get('seconds', 0)} s. "
           "The card image was only read, never changed.</small></p>",
           ("<p>This card had been reformatted with different settings. The files below were read using the "
            f"layout the card had before ({summary['old_layout']['cluster_size'] // 1024 or 1} KB blocks), "
            "worked out from what is still on it."
            + (f" {summary['since_format']} files deleted since that format were looked for first, in the card's current layout."
               if summary.get("since_format") else "") + "</p>") if summary.get("old_layout") else "",
           (f"<p>This drive has {e(summary['unreadable'])}, so its folders could not be read. The drive itself was "
            "searched for photos instead. That finds photos whether or not they were deleted, so some of what is "
            "below may still be on the drive in the normal way, and original file names are not known.</p>")
           if summary.get("unreadable") else "",
           "<table class=sum><tr><th>Result</th><th>Files</th><th>What it means</th></tr>"]
    for g in ORDER:
        if counts.get(g):
            out.append(f"<tr><td class=g{ORDER.index(g)}><b>{e(g)}</b></td><td>{counts[g]}</td><td>{e(MEANING[g])}</td></tr>")
    out.append("</table><h2>Every file</h2><table><tr><th>File</th><th>Result</th><th>How</th><th>Notes</th></tr>")
    for r in sorted(rows, key=lambda r: (ORDER.index(r["grade"]), r["folder"], r["name"])):
        grade = r["grade"] + (f" ({r['percent']:.0f}%)" if r["grade"] in ("Partial", "Needs review") and r["percent"] else "")
        where = f"<br><small>{e(r['saved_as'])}</small>" if r["saved_as"] else ""
        if r.get("filled_as"):
            where += f"<br><small>with the rest filled in from its preview: {e(r['filled_as'])}</small>"
        out.append(f"<tr><td>{e(r['name'])}{where}</td><td class=g{ORDER.index(r['grade'])}>{e(grade)}</td>"
                   f"<td>{e(r['method'])}</td><td>{e(r['note'])}</td></tr>")
    out.append("</table>")
    if summary.get("steps"):
        out.append("<details><summary>Where the time went</summary><table><tr><th>Step</th><th>Seconds</th></tr>")
        out += [f"<tr><td>{e(str(m))}</td><td>{t}</td></tr>" for m, t in summary["steps"]]
        out.append("</table></details>")
    path.write_text("\n".join(out), encoding="utf-8")
