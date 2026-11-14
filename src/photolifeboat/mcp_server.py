"""Photo Lifeboat as an MCP server, so AI apps that support MCP (the Model Context Protocol) can
drive a recovery: find the card, copy it, recover, settle the close calls and look the results over.

Run with `photolifeboat mcp` (or `photolifeboat-cli.exe mcp` from the Windows build). It speaks
JSON-RPC 2.0, one message per line on standard input and output, using only the standard library.

Long jobs (copying the card, recovering) run in the background: the tool that starts one answers
straight away, and job_status says how far it has got. One job runs at a time, and the last
recovery stays open so its close calls can be decided.

Like the rest of the program, nothing here writes to a card: there is no tool that could.
"""
from __future__ import annotations

import base64
import inspect
import io
import json
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from . import __version__
from .agent import MCP_GUIDE, drive_info, make_sheets, sheet_entries

PROTOCOL = "2025-06-18"
PER_SHEET = 20

INSTRUCTIONS = ("Photo Lifeboat rescues deleted photos from memory cards. The card is only ever read. Call read_guide "
                "before starting: it has the safety rules (never write to the card; ask the person before looking at "
                "any of their photos, because that sends them to the AI provider) and the steps in order.")


def _obj(props: dict, required=()) -> dict:
    return dict(type="object", properties=props, required=list(required), additionalProperties=False)


TOOLS = [
    dict(name="read_guide", description="The safety rules and the steps of a recovery, in order. Read this first.",
         inputSchema=_obj({})),
    dict(name="list_drives",
         description="Drives attached to the computer. The memory card is usually removable and never a system drive. "
                     "Call before and after the card is plugged in to see which one is new.",
         inputSchema=_obj({})),
    dict(name="copy_card",
         description="Copy the whole card into one image file, read-only, on a different drive. Runs in the background: "
                     "follow it with job_status. On Windows the person must click Yes on a permission prompt.",
         inputSchema=_obj({"drive": {"type": "string", "description": "the drive's id from list_drives"},
                           "save_as": {"type": "string", "description": "full path of the image file to create, "
                                                                        "e.g. C:\\Users\\me\\Pictures\\Card rescue\\card.img"}},
                          ["drive", "save_as"])),
    dict(name="recover",
         description="Find, rebuild and check every lost file in a card image. Runs in the background (minutes to an "
                     "hour or more): follow it with job_status, then call results.",
         inputSchema=_obj({"image": {"type": "string", "description": "path of the card image file"},
                           "results_folder": {"type": "string",
                                              "description": "where to save what comes back (default: next to the image)"},
                           "include_existing": {"type": "boolean",
                                                "description": "also copy out files that were never deleted"}},
                          ["image"])),
    dict(name="job_status", description="How far the current copy or recovery has got.", inputSchema=_obj({})),
    dict(name="results", description="The finished recovery's summary: how many files came back, and any warnings.",
         inputSchema=_obj({})),
    dict(name="close_calls", description="Photos the tool would not finish on its own: each needs a choice of next piece.",
         inputSchema=_obj({})),
    dict(name="show_close_call",
         description="Pictures of one close call: each possible next piece joined onto the photo, and the photo's own "
                     "small preview to compare with. Sends these pictures to the AI provider.",
         inputSchema=_obj({"index": {"type": "integer"}}, ["index"])),
    dict(name="decide_close_call",
         description="Choose the piece that carries on the photo smoothly and matches its preview, or null if none do. "
                     "Kept only if the finished photo then passes every check.",
         inputSchema=_obj({"index": {"type": "integer"},
                           "cluster": {"type": ["integer", "null"], "description": "the chosen piece, or null"}},
                          ["index", "cluster"])),
    dict(name="contact_sheet",
         description=f"{PER_SHEET} recovered photos at a time in one numbered grid, for checking them by eye. "
                     "Sends these photos to the AI provider: ask the person first.",
         inputSchema=_obj({"which": {"type": "string", "enum": ["complete", "partial", "all"]},
                           "page": {"type": "integer", "minimum": 1}})),
    dict(name="view_photo",
         description="One recovered photo, larger. Sends it to the AI provider: ask the person first.",
         inputSchema=_obj({"file": {"type": "string", "description": "its path inside the results folder, "
                                                                     "as contact_sheet or report.json gives it"}},
                          ["file"])),
    dict(name="check_photos",
         description="The final automatic check on photo files: is each one whole, and does it match its own preview?",
         inputSchema=_obj({"files": {"type": "array", "items": {"type": "string"}}}, ["files"])),
]


class Refused(Exception):
    """A request the server will not carry out, said in words the assistant can pass on."""


class Server:
    def __init__(self):
        self.lock = threading.Lock()
        self.job: dict | None = None          # the current or last background job
        self.rec = None                       # the last finished recovery, kept for its close calls
        self.out: Path | None = None
        self.sheets = Path(tempfile.mkdtemp(prefix="photolifeboat-sheets-"))

    # ---- protocol -----------------------------------------------------------------------------
    def handle(self, msg: dict) -> dict | None:
        """Answer one JSON-RPC message. Notifications (no id) get no answer."""
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            return None
        try:
            if method == "initialize":
                asked = (msg.get("params") or {}).get("protocolVersion")
                result = dict(protocolVersion=asked if isinstance(asked, str) else PROTOCOL,
                              capabilities=dict(tools={}),
                              serverInfo=dict(name="photo-lifeboat", title="Photo Lifeboat", version=__version__),
                              instructions=INSTRUCTIONS)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = dict(tools=TOOLS)
            elif method == "tools/call":
                p = msg.get("params") or {}
                result = self.call(p.get("name"), p.get("arguments") or {})
            else:
                return dict(jsonrpc="2.0", id=mid, error=dict(code=-32601, message=f"unknown method {method}"))
        except Exception as e:  # noqa: BLE001 - always answer, never die
            return dict(jsonrpc="2.0", id=mid, error=dict(code=-32603, message=f"{type(e).__name__}: {e}"))
        return dict(jsonrpc="2.0", id=mid, result=result)

    def call(self, name: str, args: dict) -> dict:
        fn = getattr(self, f"tool_{name}", None)
        if fn is None or name not in {t["name"] for t in TOOLS}:
            return _text(f"There is no tool called {name}.", error=True)
        try:
            inspect.signature(fn).bind(**args)
        except TypeError as e:
            return _text(f"Wrong arguments for {name}: {e}", error=True)
        try:
            return fn(**args)
        except Refused as e:
            return _text(str(e), error=True)
        except Exception as e:  # noqa: BLE001 - reported to the assistant rather than lost
            return _text(f"{name} failed: {type(e).__name__}: {e}", error=True)

    # ---- tools ----------------------------------------------------------------------------------
    def tool_read_guide(self) -> dict:
        return _text(MCP_GUIDE)

    def tool_list_drives(self) -> dict:
        from . import device
        return _data([drive_info(d) for d in device.list_drives()])

    def tool_copy_card(self, drive: str, save_as: str) -> dict:
        from . import device
        from .cli import _named_drive
        self._idle()
        dest = Path(save_as).expanduser()
        if device.is_device(dest) or not dest.is_absolute():
            raise Refused("save_as must be the full path of a new file in a folder, not a drive.")
        known = _named_drive(drive, None)
        if known is None:
            raise Refused(f"There is no drive {drive!r}. Call list_drives for the ids.")
        if known.system:
            raise Refused("That drive holds this computer's own system, so it is not a memory card.")
        if device.drive_of_path(dest.parent if dest.parent.exists() else dest.anchor) == known.id:
            raise Refused("The copy cannot be saved onto the card itself. Choose a folder on another drive.")
        why = device.dest_problem(_existing_parent(dest), known.size)
        if why:
            raise Refused(why)
        if dest.exists() and dest.stat().st_size and not Path(str(dest) + ".map.json").exists():
            raise Refused("A file with that name is already there. Choose another name.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        status = Path(tempfile.gettempdir()) / f"photolifeboat-mcp-clone-{id(self)}.json"
        device.start_clone(known, dest, status)
        with self.lock:
            self.job = dict(kind="copy", state="running", drive=known.label(), image=str(dest), status=str(status),
                            started=time.time())
        return _text(f"Copying {known.label()} to {dest}. On Windows, ask the person to click Yes on the permission "
                     "prompt. Call job_status to follow it.")

    def tool_recover(self, image: str, results_folder: str | None = None, include_existing: bool = False) -> dict:
        from .device import is_device
        from .fat import NotFat
        from .recover import Recovery
        self._idle()
        img = Path(image).expanduser()
        if is_device(img):
            raise Refused("That is a drive, not an image file. Recovery always works on a copy: use copy_card first.")
        if not img.is_file():
            raise Refused(f"Cannot find the image file {img}.")
        out = Path(results_folder).expanduser() if results_folder else img.with_name(img.stem + " recovered")
        if is_device(out) or out.resolve() == img.resolve():
            raise Refused("results_folder must be a folder, not a drive or the image itself.")
        job = dict(kind="recover", state="running", image=str(img), out=str(out), steps=[], started=time.time())

        def log(m):
            job["steps"].append([m, round(time.time() - job["started"], 1)])

        try:
            rec = Recovery(img, out, log)
        except NotFat as e:
            raise Refused(f"Cannot read this card image: {e}") from None
        rec.include_existing = bool(include_existing)
        job["rec"] = rec

        def work():
            try:
                job["summary"] = rec.run()
                with self.lock:
                    self.rec, self.out = rec, out
                job["state"] = "done"
            except Exception as e:  # noqa: BLE001 - reported through job_status
                job["state"], job["error"] = "error", f"{type(e).__name__}: {e}"
                traceback.print_exc(file=sys.stderr)

        with self.lock:
            self.job = job
        threading.Thread(target=work, daemon=True).start()
        return _text(f"Recovering from {img} into {out}. Call job_status to follow it (every minute or so).")

    def tool_job_status(self) -> dict:
        job = self.job
        if job is None:
            return _text("Nothing is running, and nothing has run yet.")
        seconds = round(time.time() - job["started"])
        if job["kind"] == "copy":
            try:
                info = json.loads(Path(job["status"]).read_text())
            except (OSError, ValueError):
                info = dict(state="starting")
            job["state"] = {"done": "done", "error": "error", "cancelled": "error"}.get(info.get("state"), "running")
            return _data(dict(info, job="copy card", drive=job["drive"], image=job["image"],
                              state_of_copy=info.get("state"), state=job["state"]))
        rec = job.get("rec")
        p = getattr(rec, "progress", None)
        out = dict(job="recover", state=job["state"], image=job["image"], results_folder=job["out"], seconds=seconds,
                   step=job["steps"][-1][0] if job["steps"] else "starting",
                   step_progress=dict(done=p[0], total=p[1]) if p else None,
                   steps_so_far=[s[0] for s in job["steps"][-8:]])
        if job.get("error"):
            out["error"] = job["error"]
        if job["state"] == "done":
            out["next"] = "call results"
        return _data(out)

    def tool_results(self) -> dict:
        rec = self._rec()
        s = rec.summary
        keep = ("counts", "findings", "still_on_card", "filesystem", "files_listed", "seconds", "partitions",
                "damaged_start", "unreadable", "unreadable_bytes", "report_error")
        out = {k: s[k] for k in keep if s.get(k) is not None}
        out.update(results_folder=str(self.out), report=str(self.out / "report.html"),
                   close_calls=len(rec.review_queue()),
                   folders={"good files": "Recovered", "incomplete files": "Recovered - Partial",
                            "incomplete photos with the gap filled from their preview": "Recovered - Preview filled"})
        return _data(out)

    def tool_close_calls(self) -> dict:
        rec = self._rec()
        return _data([dict(index=i, name=rec.items[i].name, percent=rec.items[i].percent) for i in rec.review_queue()])

    def tool_show_close_call(self, index: int) -> dict:
        rec = self._rec()
        if index not in rec.review_queue():
            raise Refused("That photo is not waiting for a decision. Call close_calls for the list.")
        shown = rec.review_images(index)
        content = [dict(type="text", text=(
            f"{shown['name']}: {shown['percent']:.0f}% of the photo is known to be right. Below are the photo's own "
            "small preview (if it has one), then the area around the join with each possible next piece. Choose the "
            "piece that carries on smoothly with no seam, smear or jump in colour, and matches the preview."))]
        if shown["preview"]:
            content += [dict(type="text", text="The photo's own preview:"), _image(shown["preview"])]
        for c in shown["candidates"]:
            content += [dict(type="text", text=f"With piece {c['cluster']}:"), _image(c["image"])]
        if not shown["candidates"]:
            content.append(dict(type="text", text="No possible pieces are left: decide with cluster null."))
        return dict(content=content)

    def tool_decide_close_call(self, index: int, cluster: int | None) -> dict:
        rec = self._rec()
        if index not in rec.review_queue():
            raise Refused("That photo is not waiting for a decision. Call close_calls for the list.")
        grade = rec.review_pick(index, None if cluster is None else int(cluster))
        return _data(dict(index=index, result=grade, still_waiting=index in rec.review_queue()))

    def tool_contact_sheet(self, which: str = "complete", page: int = 1) -> dict:
        out = self._out()
        page = max(1, int(page))
        total = len(sheet_entries(out, which))
        pages = max(1, -(-total // PER_SHEET))
        if total == 0:
            return _text(f"There are no {which} photos to show.")
        if page > pages:
            raise Refused(f"There are only {pages} pages.")
        sheet = make_sheets(out, self.sheets / which, which, PER_SHEET, start=(page - 1) * PER_SHEET,
                            limit=PER_SHEET)[0]
        listing = "\n".join(f"{p['number']}: {p['file']} ({p['grade']})" for p in sheet["photos"])
        return dict(content=[dict(type="text", text=f"Page {page} of {pages} ({total} {which} photos). "
                                                    f"The numbers on the sheet are these files:\n{listing}"),
                             _image(Path(sheet["sheet"]).read_bytes())])

    def tool_view_photo(self, file: str) -> dict:
        from PIL import Image, ImageOps
        out = self._out().resolve()
        path = (out / file).resolve()
        if out not in path.parents or not path.is_file():
            raise Refused("Give the path of a file inside the results folder, as contact_sheet lists it.")
        with Image.open(path) as im:
            im.draft("RGB", (1600, 1600))
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((1600, 1600))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
        return dict(content=[dict(type="text", text=file), _image(buf.getvalue())])

    def tool_check_photos(self, files: list) -> dict:
        from .checks import audit_jpeg
        found = []
        for f in files:
            try:
                ok, note, had_preview = audit_jpeg(Path(f).read_bytes())
            except OSError as e:
                ok, note, had_preview = False, f"cannot be read ({e.strerror})", False
            found.append(dict(file=f, passes=ok, note=note, had_preview=had_preview))
        return _data(found)

    # ---- helpers --------------------------------------------------------------------------------
    def _idle(self) -> None:
        if self.job and self.job["state"] == "running":
            self.tool_job_status()               # a copy's state is only known by looking
            if self.job["state"] == "running":
                raise Refused(f"A {self.job['kind']} job is still running. Wait for it (job_status) first.")

    def _rec(self):
        if self.rec is None:
            raise Refused("No recovery has finished yet. Use recover, then follow it with job_status.")
        return self.rec

    def _out(self) -> Path:
        self._rec()
        return self.out


def _existing_parent(p: Path) -> Path:
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def _text(text: str, error: bool = False) -> dict:
    return dict(content=[dict(type="text", text=text)], isError=error)


def _data(obj) -> dict:
    return dict(content=[dict(type="text", text=json.dumps(obj, indent=1))],
                structuredContent=obj if isinstance(obj, dict) else dict(items=obj), isError=False)


def _image(data: bytes) -> dict:
    return dict(type="image", data=base64.b64encode(data).decode(), mimeType="image/jpeg")


def serve(stdin=None, stdout=None) -> int:
    """Answer MCP messages until standard input closes. Anything else the program prints goes to
    standard error, so it can never get mixed into the conversation with the AI app."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    real, sys.stdout = sys.stdout, sys.stderr
    server = Server()
    try:
        for line in stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                reply = dict(jsonrpc="2.0", id=None, error=dict(code=-32700, message="not JSON"))
            else:
                if isinstance(msg, list):
                    reply = [r for r in (server.handle(m) for m in msg) if r] or None
                else:
                    reply = server.handle(msg)
            if reply:
                stdout.write(json.dumps(reply) + "\n")
                stdout.flush()
    finally:
        sys.stdout = real
    return 0
