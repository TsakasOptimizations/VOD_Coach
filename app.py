"""VOD Coach desktop app: pywebview window + tiny local server so the HTML player can seek the VOD."""
import json, os, platform, re, sys, threading, time, traceback, urllib.request, webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import webview

import coach

HERE = Path(__file__).parent
SETTINGS = Path.home() / ".vodcoach.json"
VERSION = "1.4"  # bump with every GitHub release (tag it v<VERSION>)
REPO = "TsakasOptimizations/VOD_Coach"
STORE = "WindowsApps" in sys.executable  # installed from the Microsoft Store: the Store handles updates
current = {"path": None}
# Discord webhook that bug reports go to. Git-ignored (the repo is public), so only builds made with the file have it.
WEBHOOK_FILE = HERE / "report_webhook.txt"


def scrub(text, api_key=""):
    """Strip what must never leave the PC: the API key and the Windows username (it's in every path)."""
    text = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "<key>", text.replace(api_key, "<key>") if api_key else text)
    home = str(Path.home())
    for h in (home, home.replace("\\", "/"), home.replace("\\", "\\\\")):
        text = re.sub(re.escape(h), "~", text, flags=re.I)
    user = Path.home().name
    if len(user) >= 3:  # the name can show up on its own too (temp folder names, OneDrive paths)
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(user)}(?![A-Za-z0-9])", "<user>", text, flags=re.I)
    return text


class VideoHandler(SimpleHTTPRequestHandler):
    """Serves the open VOD with HTTP Range support (needed for seeking)."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = current["path"]
        if not path or not os.path.exists(path):
            return self.send_error(404)
        size = os.path.getsize(path)
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng:
            a, _, b = rng.removeprefix("bytes=").partition("-")
            start, end = (int(a), int(b) if b else size - 1) if a else (size - int(b), size - 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (ConnectionError, OSError):
                pass  # player seeked away and dropped the connection


server = ThreadingHTTPServer(("127.0.0.1", 0), VideoHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def report_path(video):
    return video + ".coach.json"


class Cancelled(Exception):
    pass


def flash_taskbar():
    """Blink the taskbar button until the user comes back: a review takes minutes and people switch away."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    class FLASHWINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                    ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, "VOD Coach")
    if hwnd and hwnd != user32.GetForegroundWindow():
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 3 | 12, 0, 0)  # FLASHW_ALL | FLASHW_TIMERNOFG
        user32.FlashWindowEx(ctypes.byref(info))


class Api:
    _status = {}
    _models = []
    _bug = ""  # the last bug's details, exactly as they'd be sent

    def _record_bug(self, where, details):
        s = self.load_settings()
        self._bug = scrub(f"VOD Coach {VERSION} ({'Store' if STORE else 'GitHub'}) on {platform.platform()}\n"
                          f"Game: {s.get('game', '?')} · coach: {s.get('model', '?')} · where: {where}\n\n{details}",
                          s.get("api_key", ""))

    def report_js_error(self, details):
        self._record_bug("app window", details)

    def bug_report(self):  # the details stay on the Python side: only Send report ships them, straight to Discord
        return {"can_send": WEBHOOK_FILE.exists()}

    def send_report(self, note, feedback=False):
        """A bug report (the last bug's details + their note) or, with feedback=True, just their message."""
        note = scrub(note.strip()[:1500 if feedback else 400], self.load_settings().get("api_key", ""))
        if feedback:
            if not note:
                raise RuntimeError("Write your feedback first.")
            name, content = "VOD Coach feedback", f"**Feedback** (VOD Coach {VERSION}, {'Store' if STORE else 'GitHub'}):\n{note}"
        else:
            head = f"**What they were doing:** {note}\n" if note else ""
            body = self._bug[-(1900 - len(head)):]  # Discord caps a message at 2000 chars; the end of a traceback matters most
            name, content = "VOD Coach bug report", f"{head}```\n{body}\n```"
        req = urllib.request.Request(WEBHOOK_FILE.read_text(encoding="utf-8").strip(),
                                     data=json.dumps({"username": name, "content": content}).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": f"VODCoach/{VERSION}"})
        try:
            urllib.request.urlopen(req, timeout=15)
        except OSError as e:
            raise RuntimeError("Couldn't send the report. Check your internet connection and try again.") from e

    def version(self):
        return {"version": VERSION, "store": STORE}

    def load_settings(self):
        try:
            return json.loads(SETTINGS.read_text())
        except (OSError, ValueError):
            return {}

    def save_settings(self, s):
        SETTINGS.write_text(json.dumps(s))

    def list_models(self, api_key):
        """Only the regular Flash models: on a free key they each get their own 20 requests/day."""
        from google import genai
        from google.genai import errors
        if not api_key.strip():
            raise RuntimeError("Paste your key first.")
        client = genai.Client(api_key=api_key.strip())  # must outlive the pager, or it closes mid-listing
        try:
            names = [m.name.removeprefix("models/") for m in client.models.list()]
        except errors.ClientError as e:  # plain-English versions of the errors new users actually hit
            if "SERVICE_DISABLED" in str(e):
                raise RuntimeError("This key's Google project doesn't have Gemini switched on. Easiest fix: in AI Studio click "
                                   "Create API key, pick \"Default Gemini Project\" as the project, and paste that key.") from e
            if e.code in (400, 401, 403):
                raise RuntimeError("Google didn't accept that key. Copy it again from AI Studio and paste it here.") from e
            raise
        except OSError as e:
            raise RuntimeError("Couldn't reach Google. Check your internet connection.") from e
        flash = [n for n in names if re.fullmatch(r"gemini-\d+(\.\d+)?-flash", n) and n not in coach.retired()]
        self._models = sorted(flash, key=lambda n: -float(n.split("-")[1]))
        return self._models

    _fullscreen = False

    def set_fullscreen(self, on):
        if on != self._fullscreen:  # toggle-only API, so track the state ourselves
            webview.windows[0].toggle_fullscreen()
            self._fullscreen = on

    def check_updates(self):
        try:
            rel = coach.get_json(f"https://api.github.com/repos/{REPO}/releases/latest")
        except Exception as e:
            raise RuntimeError("Couldn't reach GitHub. Check your internet connection.") from e
        ver = lambda t: tuple(int(n) for n in re.findall(r"\d+", t))  # "v1.10" -> (1, 10)
        return {"current": VERSION, "latest": rel["tag_name"].lstrip("vV"),
                "newer": ver(rel["tag_name"]) > ver(VERSION)}

    def open_releases(self):
        webbrowser.open(f"https://github.com/{REPO}/releases/latest")

    def open_key_page(self):
        webbrowser.open("https://aistudio.google.com/apikey")

    def quota(self, model, seconds, game="valorant"):
        per_game = coach.requests_per_game(seconds or 35 * 60, game)
        others = [m for m in self._models if m != model]
        return {"games": coach.requests_left(model) // per_game,
                "games_all": sum(coach.requests_left(m) for m in [model] + others) // per_game,
                "reset_in": coach.reset_in_seconds()}

    def agent_names(self, game):
        kits = coach.GAMES[game]["kits"]()
        return [{"name": n, "icon": kits[n]["icon"]} for n in sorted(kits)]

    def ranks(self, game):
        return coach.GAMES[game]["ranks"]()

    def open_video(self):
        res = webview.windows[0].create_file_dialog(
            webview.FileDialog.OPEN, file_types=("Videos (*.mp4;*.mkv;*.mov;*.webm;*.flv)", "All files (*.*)"))
        if not res:
            return None
        current["path"] = res[0]
        rp = report_path(res[0])
        report = json.loads(Path(rp).read_text(encoding="utf-8")) if os.path.exists(rp) else None
        try:
            current["seconds"] = coach.duration(res[0])
        except coach.CoachError as e:
            return {"error": str(e)}
        return {"name": os.path.basename(res[0]), "seconds": current["seconds"], "url": f"http://127.0.0.1:{server.server_port}/v?{os.path.getmtime(res[0])}",
                "report": report}

    def set_disagree(self, i, reason):
        """reason=None removes the disagreement. Saved on the report (for display) and in the coach's memory."""
        rp = report_path(current["path"])
        report = json.loads(Path(rp).read_text(encoding="utf-8"))
        m = report["moments"][i]
        key = f"{report['video']}@{m['seconds']}"
        if reason is None:
            m.pop("disagree", None)
        else:
            m["disagree"] = reason
        Path(rp).write_text(json.dumps(report, indent=1), encoding="utf-8")
        memory = [f for f in coach.load_feedback() if f["key"] != key]
        if reason is not None:
            memory.append({"key": key, "game": report.get("game", "valorant"), "agent": report["agent"], "note": f"{m['title']}: {m['what_happened']} -> {m['better']}",
                           "reason": reason})
        coach.FEEDBACK_FILE.write_text(json.dumps(memory, indent=1), encoding="utf-8")
        return report

    def get_status(self):
        return self._status

    _run = None  # the running review; cancel() clears it

    def cancel(self):
        self._run = None
        self._status = {}

    def analyze(self, s):
        path, run, out, done = current["path"], object(), {}, threading.Event()
        self._run = run

        def progress(msg, span=None, left=None):  # span: this step's share of the job, "since" lets the bar creep through it
            if self._run is not run:
                raise Cancelled  # stops the cancelled review at its next step (finished parts stay saved)
            self._status = {**self._status, "msg": msg, **({"from": span[0], "to": span[1], "since": time.time()} if span else {}),
                            **({"end": time.time() + left} if left is not None else {})}

        def work():
            try:
                report = coach.analyze(path, s["api_key"], [s["model"]] + [m for m in self._models if m != s["model"]], s, progress)
                Path(report_path(path)).write_text(json.dumps(report, indent=1), encoding="utf-8")
                out["report"] = report
            except Cancelled:
                pass
            except coach.CoachError as e:  # limits, Gemini busy...: the message says what to do
                out["error"] = str(e)
            except Exception:
                traceback.print_exc()
                self._record_bug(f"analyzing ({self._status.get('msg', 'starting')})", traceback.format_exc())
                out["bug"] = True
            finally:
                done.set()

        # Work runs on its own thread so Cancel answers at once, even mid-request; the old run quits at its next step.
        threading.Thread(target=work, daemon=True).start()
        while not done.wait(.25):
            if self._run is not run:
                break
        if self._run is run:
            self._run, self._status = None, {}
            flash_taskbar()
        else:
            out = {"cancelled": True}
        out["resume"] = os.path.exists(coach.partial_path(path))
        return out


if __name__ == "__main__":
    if getattr(sys, "frozen", False):  # packaged .exe has no console: send errors to a log file instead
        sys.stdout = sys.stderr = open(Path.home() / ".vodcoach.log", "a", encoding="utf-8", buffering=1)
    webview.create_window("VOD Coach", str(HERE / "index.html"), js_api=Api(), width=1500, height=900,
                          background_color="#0f1419")
    webview.start(icon=str(HERE / "assets" / "icon.ico"))  # Windows needs .ico, a .png crashes on startup
