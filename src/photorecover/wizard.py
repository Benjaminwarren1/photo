"""The wizard: a small web page served only to this computer, which walks
someone from "I plugged my card in" to a Recovered folder.

The page talks to this process over 127.0.0.1 with a random token. Nothing is
sent anywhere else.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, device
from .fat import NotFat
from .recover import Recovery
from .wizard_page import PAGE


def default_folder() -> Path:
    home = Path.home()
    base = home / "Desktop" if (home / "Desktop").is_dir() else home
    return base / time.strftime("Photo recovery %Y-%m-%d %H%M")


class Session:
    def __init__(self):
        self.lock = threading.Lock()
        self.step = "start"            # start | plug | confirm | cloning | recovering | done | error
        self.before: list[device.Drive] = []
        self.found: list[device.Drive] = []
        self.drive: device.Drive | None = None
        self.folder: Path = default_folder()
        self.image: Path | None = None
        self.status_file: Path | None = None
        self.message = ""
        self.log: list[str] = []
        self.summary: dict | None = None
        self.clone_info: dict = {}
        self.rec: Recovery | None = None
        self.running: Recovery | None = None

    def review_next(self) -> dict | None:
        """The next close call for a person to decide, with pictures as data URLs."""
        import base64
        if not self.rec:
            return None
        for idx in self.rec.review_queue():
            shown = self.rec.review_images(idx)
            if not shown["candidates"]:
                self.rec.review_pick(idx, None)
                continue

            def url(b):
                return "data:image/jpeg;base64," + base64.b64encode(b).decode()

            return dict(index=idx, name=shown["name"], percent=shown["percent"],
                        left=len(self.rec.review_queue()),
                        preview=url(shown["preview"]) if shown["preview"] else None,
                        candidates=[dict(cluster=c["cluster"], image=url(c["image"]), join=c["join"])
                                    for c in shown["candidates"]])
        return None

    def review_pick(self, index: int, cluster) -> None:
        if self.rec and index in self.rec.review_queue():
            self.rec.review_pick(index, None if cluster is None else int(cluster))
            self.summary = self.rec.summary

    # ---- actions ----------------------------------------------------------
    def begin_card(self):
        self.before = device.list_drives()
        self.step, self.message = "plug", ""

    def find_card(self):
        self.found = device.new_drives(self.before, device.list_drives())
        if not self.found:
            self.message = ("No new card has appeared yet. Push it in firmly, wait a few seconds and try again. "
                            "If Windows asks to format or scan the card, choose Cancel.")
            return
        self.drive = self.found[0] if len(self.found) == 1 else None
        self.step, self.message = "confirm", ""

    def choose(self, drive_id: str):
        self.drive = next((d for d in self.found if d.id == drive_id), self.drive)

    def space(self) -> dict:
        free = device.free_space(self.folder)
        need = int((self.drive.size if self.drive else 0) * 1.5)
        return dict(free=free, need=need, ok=free >= need)

    def start_clone(self, folder: str | None):
        if folder:
            self.folder = Path(folder)
        if not self.drive:
            self.message = "Choose which drive is the card first."
            return
        if device.drive_of_path(self.folder) == self.drive.id:
            self.message = "That folder is on the card itself. Choose a folder on this computer or another drive."
            return
        if not self.space()["ok"]:
            self.message = "There is not enough free space in that folder. Choose another drive, such as an external one."
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        self.image = self.folder / "card.img"
        self.status_file = self.folder / "clone-status.json"
        device.start_clone(self.drive, self.image, self.status_file)
        self.step, self.message = "cloning", ""

    def use_image(self, path: str):
        shown = path.strip().strip('"')
        if not shown:
            self.message = "Choose the file first: press Choose…, or paste its full path into the box."
            return
        p = Path(shown)
        if not p.is_file():
            self.message = f"There is no file at {shown}. Check the path, or press Choose… to pick it."
            return
        self.image = p
        self.folder = p.with_name(p.stem + " recovered")
        self._recover()

    def poll_clone(self):
        try:
            info = json.loads(self.status_file.read_text())
        except (OSError, ValueError):
            return
        self.clone_info = info
        if info.get("state") == "done":
            try:
                self.status_file.unlink()
            except OSError:
                pass
            (self.folder / "card.img.sha256.txt").write_text(f"{info.get('sha256', '')}  card.img\n")
            self._recover()
        elif info.get("state") in ("error", "cancelled"):
            self.step = "error"
            self.message = info.get("message") or "Copying the card was cancelled."

    def _recover(self):
        self.step, self.log, self.message = "recovering", [], ""
        out = self.folder

        def work():
            try:
                rec = Recovery(self.image, out, lambda m: self.log.append(m))
                self.running = rec
                self.summary = rec.run()
                self.rec = rec
                self.step = "done"
            except NotFat as e:
                self.step, self.message = "error", f"This card image is too small or too damaged to search: {e}"
            except Exception as e:  # noqa: BLE001 - shown to the user rather than lost
                self.step, self.message = "error", f"Something went wrong while recovering: {type(e).__name__}: {e}"

        threading.Thread(target=work, daemon=True).start()

    def state(self) -> dict:
        with self.lock:
            if self.step == "cloning":
                self.poll_clone()
            s = dict(step=self.step, message=self.message, version=__version__, folder=str(self.folder),
                     windows=sys.platform == "win32",
                     found=[device.drive_dict(d) for d in self.found],
                     drive=device.drive_dict(self.drive) if self.drive else None)
            if self.step == "confirm" and self.drive:
                s["space"] = self.space()
            if self.step == "cloning":
                s["clone"] = self.clone_info
            if self.step in ("recovering", "done"):
                s["log"] = self.log[-6:]
                prog = self.running.progress if (self.step == "recovering" and self.running) else None
                if prog:
                    s["progress"] = dict(done=prog[0], total=prog[1])
                s["clone"] = dict(bad_bytes=self.clone_info.get("bad_bytes", 0))
            if self.step == "done":
                s["summary"] = self.summary
                s["to_review"] = len(self.rec.review_queue()) if self.rec else 0
            return s


def pick_path(kind: str) -> str:
    """Show the system's own file or folder chooser (in a helper process, since
    the chooser needs to own its main thread)."""
    r = subprocess.run(device.self_command() + ["pick", kind], capture_output=True, text=True, timeout=600)
    return r.stdout.strip()


def open_in_system(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def make_server(session: Session, token: str, port: int = 0) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _allowed(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost") and self.headers.get("X-Token") == token

        def do_GET(self):
            if self.path.split("?")[0] == "/":
                if f"t={token}" not in self.path:
                    return self._send(403, b"Open the link the program printed.", "text/plain")
                return self._send(200, PAGE.replace("__TOKEN__", token).encode(), "text/html; charset=utf-8")
            if self.path == "/api/state" and self._allowed():
                return self._send(200, json.dumps(session.state()).encode())
            self._send(404, b"{}")

        def do_POST(self):
            if not self._allowed():
                return self._send(403, b"{}")
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            a = self.path.removeprefix("/api/")
            extra = {}
            if a == "pick":
                extra["path"] = pick_path("folder" if body.get("kind") == "folder" else "file")
                if extra["path"]:
                    session.message = ""        # whatever was wrong before, a fresh choice starts clean
            else:
                with session.lock:
                    if a == "begin_card":
                        session.begin_card()
                    elif a == "find_card":
                        session.find_card()
                    elif a == "choose":
                        session.choose(str(body.get("id", "")))
                    elif a == "set_folder":
                        session.folder = Path(str(body.get("folder") or session.folder))
                    elif a == "start_clone":
                        session.start_clone(body.get("folder"))
                    elif a == "use_image":
                        session.use_image(str(body.get("path", "")))
                    elif a == "review_pick":
                        session.review_pick(int(body.get("index", -1)), body.get("cluster"))
                        extra["review"] = session.review_next()
                    elif a == "review_next":
                        extra["review"] = session.review_next()
                    elif a == "restart":
                        session.__init__()
                    elif a == "open":
                        target = session.folder / "report.html" if body.get("what") == "report" else session.folder
                        if target.exists():
                            open_in_system(target)
                    elif a == "quit":
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
            self._send(200, json.dumps(dict(session.state(), **extra)).encode())

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def run(open_browser: bool = True) -> int:
    session = Session()
    token = secrets.token_urlsafe(16)
    srv = make_server(session, token)
    url = f"http://127.0.0.1:{srv.server_address[1]}/?t={token}"
    print(f"photorecover is running. If your browser did not open, go to:\n  {url}\nClose this window to stop.", flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
