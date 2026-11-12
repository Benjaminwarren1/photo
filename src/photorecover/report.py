"""Writes report.html: one line per file, in plain words."""
from __future__ import annotations

import html
from pathlib import Path

ORDER = ["Verified", "Complete, no preview", "Recovered, unverified", "Still on the card", "Partial", "Needs review",
         "Not recoverable"]
MEANING = {
    "Verified": "Every check passed, including the match against the photo's own preview.",
    "Complete, no preview": "Structure is complete and correct. There was no preview to compare the picture with.",
    "Recovered, unverified": "Came back whole, but this file type cannot be checked. Open it to make sure.",
    "Partial": "The good part is saved. The rest is not on the card.",
    "Needs review": "A close call the tool would not make on its own.",
    "Not recoverable": "Nothing usable is left on the card for this file.",
    "Still on the card": "Never deleted. Copied from the card exactly as it is.",
}
CSS = """
body{font:15px/1.5 system-ui,Segoe UI,Arial,sans-serif;margin:2rem auto;max-width:70rem;padding:0 1rem;color:#1c1c1c;background:#fff}
h1{font-size:1.5rem}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.35rem .6rem;border-bottom:1px solid #ddd;vertical-align:top}
th{background:#f3f3f3}.g0,.g1,.g3{color:#136c2e}.g2{color:#7a5b00}.g4,.g5{color:#a14a00}.g6{color:#a11}
.sum td{font-size:1.05rem}small{color:#555}
@media(prefers-color-scheme:dark){body{background:#161616;color:#eee}th{background:#262626}th,td{border-color:#3a3a3a}
.g0,.g1,.g3{color:#6fd08a}.g2{color:#e2c15a}.g4,.g5{color:#f0a060}.g6{color:#ff8080}small{color:#aaa}}
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
           _still(summary.get("still_on_card"), e),
           ("<p>The start of this card was damaged, which is what makes a computer say the card needs formatting. "
            "A spare copy of the card's layout was found further in and used to read it. Do not format the card.</p>")
           if summary.get("damaged_start") else "",
           (f"<p>The card is failing: {summary['unreadable_bytes'] / 1e6:.1f} MB of it could not be read when it was "
            "copied, and is blank in the copy. Every file below that touches one of those areas says so in its note.</p>")
           if summary.get("unreadable_bytes") else "",
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


def _still(info, e) -> str:
    """What is on the card as ordinary files. Said plainly, because someone about to format a
    card must not read an empty list of lost files as "there is nothing on it"."""
    if not info:
        return ""
    gb = info["bytes"] / 1e9
    size = f"{gb:.1f} GB" if gb >= 1 else f"{info['bytes'] / 1e6:.0f} MB"
    what = ", ".join(f"{info[k]} {k if k != 'other' else 'other files'}" for k in ("photos", "videos", "other") if info[k])
    shown = [(name, n) for name, n in info["folders"].items() if len(name) <= 40][:4]
    where = "; ".join(f"{e(name)} ({n})" for name, n in shown) or "several folders"
    if info["saved"]:
        tail = "They have been copied into the folder <b>Still on the card</b>, exactly as they are."
    else:
        tail = ("They are not lost, so they were not copied: copy them off the card in the usual way. "
                "<b>Do this before formatting the card.</b>")
    return (f"<p><b>{info['files']} files are still on the card as normal files</b> ({what}; {size}), "
            f"for example in {where}. {tail}</p>")
