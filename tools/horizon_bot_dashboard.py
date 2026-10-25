#!/usr/bin/env python3
"""Local dashboard and optional FFmpeg broadcast for an already-running disposable bot lab."""
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
from pathlib import Path
import re
import signal
import tempfile
import threading
import time
import urllib.error
import urllib.request

if __package__:
    from .bot_stream import BotStream
    from .horizon_bot_lab import ROOT, TEMP, control
else:
    from bot_stream import BotStream
    from horizon_bot_lab import ROOT, TEMP, control


ASSETS = Path(__file__).with_name("bot_dashboard")


class Dashboard:
    def __init__(self, info):
        if not re.fullmatch(r"127\.0\.0\.1:\d{1,5}", info["server"]) or not re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}", info["panel"]):
            raise ValueError("this dashboard only attaches to a loopback test lab")
        self.info = info
        self.socket = Path(info["socket"])
        self.directory = Path(tempfile.mkdtemp(prefix="bot-view-", dir=TEMP))
        self.image = self.directory / "snapshot.png"
        self.image_lock = threading.Lock()
        self.stream_lock = threading.Lock()
        self.stream = None
        self.activity = deque(maxlen=12)
        self.last_tick = None
        self.tps = None

    def note(self, message, error=False):
        self.activity.appendleft({"time": datetime.now().strftime("%H:%M:%S"), "message": message, "error": error})

    def state(self):
        status = control(self.socket, "status")
        state = control(self.socket, "state")
        now = time.monotonic()
        if self.last_tick and now - self.last_tick[0] > 0.25:
            measured = (state["ticks"] - self.last_tick[1]) / (now - self.last_tick[0])
            self.tps = measured if self.tps is None else self.tps * 0.7 + measured * 0.3
            self.last_tick = (now, state["ticks"])
        elif self.last_tick is None:
            self.last_tick = (now, state["ticks"])
        remote = status.get("server") or {}
        return {"state": state, "server": self.info["server"], "players": remote.get("players", []),
                "connected": bool(remote.get("joined")), "decode_errors": remote.get("decode_errors", 0),
                "tick_rate": round(self.tps, 1) if self.tps is not None else None,
                "frames": status["frames"], "presentation": status["presentation"],
                "stream": self.stream.status() if self.stream else {"active": False},
                "activity": list(self.activity)}

    def start_stream(self, data):
        fps = data.get("fps", 60)
        audio = data.get("audio", False)
        if type(fps) is not int or fps not in (15, 20, 30, 60) or type(audio) is not bool:
            raise ValueError("choose 15, 20, 30 or 60 FPS and a boolean audio setting")
        with self.stream_lock:
            if self.stream and self.stream.running.is_set() and self.stream.fps == fps:
                changed = self.stream.audio != audio
                self.stream.set_audio(audio)
                if changed:
                    self.note("Sound on" if audio else "Sound off")
                return self.stream.status()
            if self.stream:
                self.stream.stop()
            status = control(self.socket, "status")
            size = (status.get("renderer") or {}).get("size")
            if not size or any(value % 2 for value in size):
                raise RuntimeError("a renderer with an even frame size is required")
            self.stream = BotStream(self.socket, self.directory, size, fps, audio)
            self.note("Stream started" + (" with game audio" if audio else " · audio off"))
            return self.stream.status()

    def stop_stream(self):
        with self.stream_lock:
            if self.stream:
                self.stream.stop()
            self.note("Stream stopped · bot still running")
        return {"ok": True}

    def action(self, data):
        if not isinstance(data, dict):
            raise ValueError("action must be an object")
        action = data.get("action")
        if action == "stream-start":
            return self.start_stream(data)
        if action == "stream-stop":
            return self.stop_stream()
        if action == "snapshot":
            with self.image_lock:
                result = control(self.socket, f"screenshot {self.image}")
            self.note("Captured a frame")
            return result
        if action == "follow":
            name = data.get("name", "")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,16}", name):
                raise ValueError("choose a player to follow")
            control(self.socket, "stop")
            result = control(self.socket, f"follow {name}")
            self.note(f"Following {name}")
            return result
        if action == "goto":
            xyz = [float(data[key]) for key in ("x", "y", "z")]
            if not all(-48 <= value <= 96 for value in xyz):
                raise ValueError("destination must stay inside the lab bounds")
            control(self.socket, "stop")
            self.note("Walking to " + ", ".join(f"{v:g}" for v in xyz))
            return control(self.socket, "goto " + " ".join(map(str, xyz)) + " --near 1 --timeout 25")
        if action in ("stop", "jump", "respawn"):
            result = control(self.socket, action)
            self.note({"stop": "Standing by · movement cancelled", "jump": "Jumped", "respawn": "Respawn requested"}[action])
            return result
        if action == "reset":
            request = urllib.request.Request(self.info["panel"] + "/api/action", data=b'{"action":"reset"}', headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                result = json.load(response)
            self.note("Returned to the lab start")
            return result
        raise ValueError("unknown dashboard action")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab", type=Path, default=ROOT / ".local/bot-lab.json")
    parser.add_argument("--port", type=int, default=8786)
    args = parser.parse_args()
    dashboard = Dashboard(json.loads(args.lab.read_text()))
    expected = f"127.0.0.1:{args.port}"

    class Handler(BaseHTTPRequestHandler):
        def reply(self, value, code=200, kind="application/json"):
            data = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.headers.get("Host") != expected:
                return self.reply({"error": "loopback host required"}, 403)
            try:
                assets = {"/": ("index.html", "text/html; charset=utf-8"),
                          "/app.css": ("app.css", "text/css"), "/app.js": ("app.js", "application/javascript")}
                if self.path in assets:
                    name, mime = assets[self.path]
                    return self.reply((ASSETS / name).read_bytes(), kind=mime)
                if self.path == "/api/state":
                    return self.reply(dashboard.state())
                if self.path.startswith("/snapshot.png"):
                    with dashboard.image_lock:
                        return self.reply(dashboard.image.read_bytes(), kind="image/png")
                if self.path.startswith("/stream/"):
                    stream = dashboard.stream
                    if not stream or self.path != f"/stream/{stream.id}" or not stream.running.is_set():
                        return self.reply({"error": "stream is not active"}, 409)
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.connection.settimeout(3)
                    try:
                        for fragment in stream.subscribe():
                            self.wfile.write(fragment)
                            self.wfile.flush()
                    except (OSError, RuntimeError):
                        pass
                    return
                self.reply({"error": "not found"}, 404)
            except (OSError, RuntimeError, KeyError, ValueError) as error:
                self.reply({"error": str(error)}, 503)

        def do_POST(self):
            if self.headers.get("Host") != expected or self.headers.get("Origin", "http://" + expected) != "http://" + expected:
                return self.reply({"error": "loopback origin required"}, 403)
            if self.path != "/api/action" or self.headers.get("Content-Type") != "application/json":
                return self.reply({"error": "JSON action required"}, 400)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 2048:
                    raise ValueError("invalid request size")
                self.reply(dashboard.action(json.loads(self.rfile.read(size))))
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                dashboard.note(str(error), error=True)
                self.reply({"error": str(error)}, 400)

        def log_message(self, *_):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    print(f"Dashboard: http://{expected} (stream off; game speakers remain disabled)", flush=True)
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        http.server_close()
        dashboard.stop_stream()


if __name__ == "__main__":
    main()
