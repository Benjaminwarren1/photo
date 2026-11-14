"""The wizard: a small web page served only to this computer, which walks
someone from "I plugged my card in" to a Recovered folder.

The page is shown in a window of its own (pywebview, which uses the web engine built into the system:
Edge WebView2 on Windows), or in the browser if that is not available. Either way it talks to this
process over 127.0.0.1 with a random token. Nothing is sent anywhere else.
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


PLACES = ("pictures", "desktop")


def default_folder(place: str = "pictures", base: Path | None = None) -> Path:
    """Where the copy of the card and the results go: a new dated folder in the person's Pictures (the
    default), on their Desktop, or in a folder they chose ("other" with base), so that nobody has to type
    a folder path."""
    name = time.strftime("Rescued photos %Y-%m-%d %H%M")
    root = base if (place == "other" and base) else device.known_folder(place if place in PLACES else "pictures")
    return root / name


# Set by the window when there is one: shows the system's own folder or file chooser and returns the path.
PICKER = None


class Session:
    """Where one person has got to in the wizard, and everything it needs to carry on. The page
    asks for state() every moment and sends the person's clicks to act()."""

    def __init__(self):
        self.lock = threading.Lock()
        self.step = "start"            # start | plug | confirm | cloning | recovering | done | error
        self.before: list[device.Drive] = []
        self.found: list[device.Drive] = []
        self.drive: device.Drive | None = None
        self.place = "pictures"        # pictures | desktop | other (the person's choice of where to save)
        self.folder: Path = default_folder()
        self.made_copy = False          # the card.img in folder was made by this program (so it may offer to delete it)
        self.copy_deleted = False
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
    def act(self, action: str, body: dict) -> dict:
        """Do what a button on the page asked for. Returns anything to send back besides the state."""
        if action == "begin_card":
            self.begin_card()
        elif action == "find_card":
            self.find_card()
        elif action == "choose":
            self.choose(str(body.get("id", "")))
        elif action == "set_folder":
            self.folder = Path(str(body.get("folder") or self.folder))
        elif action == "set_place":
            self.set_place(str(body.get("place", "")), body.get("folder"))
        elif action == "delete_copy":
            self.delete_copy()
        elif action == "start_clone":
            self.start_clone(body.get("folder"))
        elif action == "resume_clone":
            self.resume_clone()
        elif action == "use_image":
            self.use_image(str(body.get("path", "")))
        elif action == "review_pick":
            self.review_pick(int(body.get("index", -1)), body.get("cluster"))
            return {"review": self.review_next()}
        elif action == "review_next":
            return {"review": self.review_next()}
        elif action == "restart":
            self.__init__()
        elif action == "open":
            target = self.folder / "report.html" if body.get("what") == "report" else self.folder
            if target.exists():
                open_in_system(target)
        return {}

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
        size = self.drive.size if self.drive else 0
        need = int(size * 1.02) + (64 << 20)        # the copy has to fit; what is recovered is usually far smaller
        return dict(free=free, need=need, ok=free >= need, tight=free < int(size * 1.5))

    def set_place(self, place: str, folder: str | None = None):
        """Where to save: "pictures", "desktop", or "other" with a folder the person picked."""
        if place == "other":
            if not folder:
                return
            self.place, self.folder = "other", default_folder("other", Path(folder))
        elif place in PLACES:
            self.place, self.folder = place, default_folder(place)
        self.message = ""

    def delete_copy(self):
        """Delete the copy of the card once the person is happy with the results. Only ever the card.img this
        program made in its own results folder, never an image file the person brought."""
        if not (self.made_copy and self.image and self.image.parent == self.folder and self.image.name == "card.img"):
            return
        for f in (self.image, self.folder / "card.img.sha256.txt", self.folder / "card.img.bad.json"):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                self.message = f"The copy could not be deleted ({e.strerror}). It may be open in another program."
                return
        self.copy_deleted, self.rec = True, None    # the review needs the copy, so it ends here

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
        why = device.dest_problem(self.folder, self.drive.size)
        if why:
            self.message = why
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        self.image = self.folder / "card.img"
        self.status_file = self.folder / "clone-status.json"
        self.made_copy = True
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
        self.place = "other"
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
            self.can_resume = bool(info.get("resumable"))

    def resume_clone(self):
        """Carry on a copy that was interrupted, into the same file, from where it stopped."""
        if not (self.drive and self.image and self.status_file):
            return
        self.can_resume = False
        device.start_clone(self.drive, self.image, self.status_file)
        self.step, self.message = "cloning", ""

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
                     windows=sys.platform == "win32", place=self.place, window=PICKER is not None,
                     can_resume=bool(getattr(self, "can_resume", False)),
                     found=[device.drive_dict(d) for d in self.found],
                     drive=device.drive_dict(self.drive) if self.drive else None)
            if self.step == "confirm" and self.drive:
                s["space"] = self.space()
            if self.step == "cloning":
                s["clone"] = self.clone_info
            if self.step in ("recovering", "done"):
                s["log"] = self.log[-40:]
                prog = self.running.progress if (self.step == "recovering" and self.running) else None
                if prog:
                    s["progress"] = dict(done=prog[0], total=prog[1])
                s["clone"] = dict(bad_bytes=self.clone_info.get("bad_bytes", 0))
            if self.step == "done":
                s["summary"] = self.summary
                s["to_review"] = len(self.rec.review_queue()) if self.rec else 0
                img = self.image if (self.made_copy and self.image and not self.copy_deleted) else None
                s["copy"] = dict(size=img.stat().st_size if img and img.exists() else 0, deleted=self.copy_deleted,
                                 offered=self.made_copy)
            return s


def pick_path(kind: str) -> str:
    """Show the system's own file or folder chooser: the window's own when there is a window, otherwise
    in a helper process, since the chooser needs to own its main thread."""
    if PICKER is not None:
        return PICKER(kind) or ""
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
                # outside the lock: the chooser stays open for as long as the person takes
                extra["path"] = pick_path("folder" if body.get("kind") == "folder" else "file")
                if extra["path"]:
                    session.message = ""        # whatever was wrong before, a fresh choice starts clean
            else:
                with session.lock:
                    if a == "quit":
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
                        if QUIT is not None:
                            QUIT()
                    else:
                        extra = session.act(a, body)
            self._send(200, json.dumps(dict(session.state(), **extra)).encode())

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


QUIT = None       # set by the window: closes it


def _window(session: Session, url: str) -> bool:
    """Show the wizard in a window of its own. Returns False if no window can be made here (no pywebview,
    or no web engine, such as a very old Windows 10 without WebView2), so the browser is used instead."""
    global PICKER, QUIT
    try:
        import webview
    except Exception:  # noqa: BLE001
        return False
    try:
        win = webview.create_window("Photo Recovery", url, width=820, height=640, min_size=(560, 480),
                                    background_color="#F3F3F3", text_select=True)
    except Exception:  # noqa: BLE001
        return False

    def picker(kind: str) -> str:
        try:
            if kind == "folder":
                got = win.create_file_dialog(webview.FileDialog.FOLDER)
            else:
                got = win.create_file_dialog(webview.FileDialog.OPEN, file_types=(
                    "Card images (*.img;*.dd;*.raw;*.bin)", "All files (*.*)"))
        except Exception:  # noqa: BLE001
            return ""
        if not got:
            return ""
        return str(got[0] if isinstance(got, (list, tuple)) else got)

    def smoke_test(report: str):
        # For the build checks: open the window, note what the page shows, and close again.
        text = ""
        try:
            win.events.loaded.wait(60)
            for _ in range(40):
                text = win.evaluate_js("document.getElementById('view').innerText") or ""
                if text.strip():
                    break
                time.sleep(0.5)
        finally:
            Path(report).write_text(text, encoding="utf-8")
            win.destroy()

    def watch():
        if os.environ.get("PHOTORECOVER_SMOKE_WINDOW"):
            threading.Thread(target=smoke_test, args=(os.environ["PHOTORECOVER_SMOKE_WINDOW"],), daemon=True).start()
        # Closing the window while the card is being copied or recovered would lose the work, so ask first,
        # but only then: nobody should be asked "are you sure?" after it has finished.
        while not closed.is_set():
            win.confirm_close = session.step in ("cloning", "recovering")
            time.sleep(0.5)

    closed = threading.Event()
    win.events.closed += closed.set
    PICKER, QUIT = picker, win.destroy
    words = {"global.quitConfirmation": "Photo Recovery is still working. If you close it now, the rescue stops "
                                        "and you will need to start again. Close anyway?",
             "global.ok": "Close", "global.cancel": "Keep working"}
    try:
        webview.start(watch, localization=words, private_mode=True,
                      gui="edgechromium" if sys.platform == "win32" else None)
    except Exception:  # noqa: BLE001
        PICKER = QUIT = None
        return False
    PICKER = QUIT = None
    return True


def run(open_browser: bool = True) -> int:
    session = Session()
    token = secrets.token_urlsafe(16)
    srv = make_server(session, token)
    url = f"http://127.0.0.1:{srv.server_address[1]}/?t={token}"
    if open_browser and not os.environ.get("PHOTORECOVER_BROWSER"):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        if _window(session, url):
            srv.shutdown()
            return 0
        srv.shutdown()
        srv = make_server(session, token)
        url = f"http://127.0.0.1:{srv.server_address[1]}/?t={token}"
    if sys.stdout is not None:
        print(f"photorecover is running. If your browser did not open, go to:\n  {url}\nClose this window to stop.", flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
