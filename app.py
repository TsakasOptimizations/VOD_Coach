"""VOD Coach desktop app: pywebview window + tiny local server so the HTML player can seek the VOD."""
import json, os, re, sys, threading, traceback, webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import webview

import coach

HERE = Path(__file__).parent
SETTINGS = Path.home() / ".vodcoach.json"
VERSION = "1.0"  # bump with every GitHub release (tag it v<VERSION>)
REPO = "TsakasOptimizations/VOD_Coach"
current = {"path": None}


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


class Api:
    _status = ""
    _models = []

    def version(self):
        return VERSION

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
        flash = [n for n in names if re.fullmatch(r"gemini-\d+(\.\d+)?-flash", n)]
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

    def quota(self, model, seconds):
        per_game = coach.requests_per_game(seconds or 35 * 60)
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
        current["seconds"] = coach.duration(res[0])
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

    def analyze(self, s):
        path = current["path"]
        try:
            report = coach.analyze(path, s["api_key"], [s["model"]] + [m for m in self._models if m != s["model"]], s, lambda msg: setattr(self, "_status", msg))
            Path(report_path(path)).write_text(json.dumps(report, indent=1), encoding="utf-8")
            return {"report": report}
        except Exception as e:
            traceback.print_exc()
            return {"error": f"{type(e).__name__}: {e}"}
        finally:
            self._status = ""


if __name__ == "__main__":
    if getattr(sys, "frozen", False):  # packaged .exe has no console: send errors to a log file instead
        sys.stdout = sys.stderr = open(Path.home() / ".vodcoach.log", "a", encoding="utf-8", buffering=1)
    webview.create_window("VOD Coach", str(HERE / "index.html"), js_api=Api(), width=1500, height=900,
                          background_color="#0f1419")
    webview.start(icon=str(HERE / "assets" / "icon.ico"))  # Windows needs .ico, a .png crashes on startup
