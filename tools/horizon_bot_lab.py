#!/usr/bin/env python3
"""Disposable vanilla-server lab for the demand-rendered Horizon bot (Unix)."""
import argparse
from collections import deque
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(os.environ.get("TMPDIR", tempfile.gettempdir())) / "opencode"
TEMP.mkdir(exist_ok=True)
BOT = "HorizonLabBot"


def isolated_env():
    env = os.environ.copy()
    for key in ("HORIZON_HOME", "HORIZON_LAUNCHER_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        env[key] = tempfile.mkdtemp(prefix="hbl-", dir=TEMP)
    env.update(HORIZON_NO_AUDIO="1", HORIZON_LAUNCHER_NO_SELF_UPDATE="1")
    # This lab never borrows a production account, connection or fixture hook.
    for key in list(env):
        if key.startswith("HORIZON_") and key not in (
            "HORIZON_HOME", "HORIZON_LAUNCHER_HOME", "HORIZON_NO_AUDIO",
            "HORIZON_LAUNCHER_NO_SELF_UPDATE",
        ):
            del env[key]
    return env


def control(path, line, timeout=40):
    with socket.socket(socket.AF_UNIX) as stream:
        stream.settimeout(timeout)
        stream.connect(str(path))
        stream.sendall((line + "\n").encode())
        with stream.makefile("rb") as reply:
            data = json.loads(reply.readline(16 * 1024 * 1024))
    if "error" in data:
        raise RuntimeError(data["error"])
    return data


def until(check, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except ChildProcessError:
            raise
        except (OSError, RuntimeError):
            pass
        time.sleep(0.1)
    raise TimeoutError("lab readiness deadline expired")


class Lab:
    def __init__(self, args):
        self.args = args
        self.env = isolated_env()
        self.directory = Path(self.env["HORIZON_HOME"])
        self.socket = self.directory / "bot.sock"
        self.image = self.directory / "view.png"
        self.logs = deque(maxlen=400)
        self.children = []
        self.files = []
        self.image_lock = threading.Lock()
        self.teleports = 0
        self.positions = queue.Queue()

    def console(self, line):
        self.server.stdin.write(line + "\n")
        self.server.stdin.flush()

    def read_logs(self):
        for line in self.server.stdout:
            if f"Teleported {BOT} to " in line:
                self.teleports += 1
            match = re.search(r"HorizonLabBot has the following entity data: \[([^]]+)\]", line)
            if match:
                self.positions.put([float(part.strip().removesuffix("d")) for part in match[1].split(",")])
            self.logs.append(line.rstrip())
            self.server_log.write(line)
            self.server_log.flush()

    def launch_bot(self, name, path, env):
        log = open(Path(env["HORIZON_HOME"]) / "client.log", "w")
        self.files.append(log)
        process = subprocess.Popen([
            str(self.args.game), "--resources", str(self.args.resources),
            "--server", f"127.0.0.1:{self.args.port}", "--name", name,
            "--bot", "--control-socket", str(path), "--render-distance", "6",
            "--width", "640", "--height", "360",
        ], env=env, stdout=log, stderr=subprocess.STDOUT)
        self.children.append(process)

        def ready():
            if process.poll() is not None:
                raise ChildProcessError(f"{name} exited; inspect {log.name}")
            state = control(path, "status")
            if (state.get("server") or {}).get("loaded") and state.get("player"):
                return state
            return None

        until(ready, 120)
        return process

    def start(self):
        server_dir = self.directory / "server"
        server_dir.mkdir()
        (server_dir / "eula.txt").write_text("eula=true\n")
        (server_dir / "server.properties").write_text(
            f"server-ip=127.0.0.1\nserver-port={self.args.port}\n"
            "online-mode=false\nenforce-secure-profile=false\n"
            "level-type=minecraft:flat\nlevel-seed=12345\n"
            'generator-settings={"biome":"minecraft:plains","layers":[{"block":"minecraft:bedrock","height":1},{"block":"minecraft:dirt","height":2},{"block":"minecraft:grass_block","height":1}]}\n'
            "gamemode=creative\ndifficulty=peaceful\nspawn-protection=0\n"
            "view-distance=6\nsimulation-distance=4\nmax-players=6\n"
            "enable-rcon=false\nenable-query=false\nwhite-list=false\n"
            "motd=Horizon bot v1 - disposable localhost lab\n"
        )
        owner = "cheesemany"
        offline_uuid = uuid.UUID(bytes=hashlib.md5(f"OfflinePlayer:{owner}".encode()).digest(), version=3)
        (server_dir / "ops.json").write_text(json.dumps([
            {"uuid": str(offline_uuid), "name": owner, "level": 4, "bypassesPlayerLimit": True}
        ]))
        self.server_log = open(server_dir / "console.log", "w")
        self.files.append(self.server_log)
        self.server = subprocess.Popen([
            str(self.args.java), "-Xms256M", "-Xmx1G", "-jar", str(self.args.server_jar), "nogui",
        ], cwd=server_dir, env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.children.append(self.server)
        threading.Thread(target=self.read_logs, daemon=True).start()

        def ready():
            if self.server.poll() is not None:
                raise ChildProcessError(f"server exited; inspect {self.server_log.name}")
            return any('Done (' in line for line in self.logs)

        until(ready, 120)
        self.console("forceload add -48 -48 48 48")
        def arena_loaded():
            self.console("execute if loaded -48 64 -48 if loaded 48 64 48 run say LAB_ARENA_READY")
            return any("LAB_ARENA_READY" in line for line in self.logs)
        until(arena_loaded)
        for command in (
            "fill -48 63 -48 48 64 48 minecraft:stone",
            "fill -48 64 -48 48 64 48 minecraft:white_concrete",
            "fill 8 65 -8 8 67 8 minecraft:stone_bricks",
            "fill -12 65 -4 -8 65 4 minecraft:quartz_block",
            "setworldspawn 0 65 0", "time set day", "weather clear",
            "gamerule minecraft:advance_time false", "gamerule minecraft:advance_weather false",
            "gamerule minecraft:keep_inventory true",
        ):
            self.console(command)
        self.bot = self.launch_bot(BOT, self.socket, isolated_env())
        self.console(f"gamemode survival {BOT}")
        self.reset()

    def reset(self):
        control(self.socket, "stop")
        before = self.teleports
        self.console(f"tp {BOT} 0.5 65 0.5 0 0")
        def settled():
            player = control(self.socket, "state")["player"]
            if player is None:
                return False
            return (self.teleports > before and abs(player["yaw"]) < 0.1
                    and all(abs(a - b) < 0.1 for a, b in zip(player["position"], [0.5, 65, 0.5]))
                    and player["on_ground"])
        until(settled)
        # Teleporting does not reset vanilla's forward double-tap sprint timer.
        # Start each replay after ten released ticks, with motion/input settled.
        control(self.socket, "wait 10")

    def snapshot(self):
        with self.image_lock:
            result = control(self.socket, f"screenshot {self.image}")
            return result

    def server_position(self):
        while not self.positions.empty():
            self.positions.get_nowait()
        self.console(f"data get entity {BOT} Pos")
        return self.positions.get(timeout=10)

    def action(self, data):
        if not isinstance(data, dict):
            raise ValueError("action must be a JSON object")
        action = data.get("action")
        if action == "snapshot":
            return self.snapshot()
        if action == "reset":
            self.reset()
            return {"ok": True}
        if action == "follow":
            name = data.get("name", "cheesemany")
            if not isinstance(name, str) or not name.isascii() or not name.replace("_", "").isalnum() or len(name) > 16:
                raise ValueError("invalid player name")
            return control(self.socket, f"follow {name}")
        if action in ("stop", "jump", "respawn"):
            return control(self.socket, action)
        if action == "goto":
            coords = [float(data[key]) for key in ("x", "y", "z")]
            if not all(-48 <= value <= 96 for value in coords):
                raise ValueError("coordinates must stay in the lab arena")
            return control(self.socket, "goto " + " ".join(map(str, coords)) + " --near 1 --timeout 20")
        raise ValueError("unknown lab action")

    def check(self):
        before = control(self.socket, "status")
        time.sleep(1.2)
        after = control(self.socket, "status")
        assert after["ticks"] - before["ticks"] >= 15, "client ticks stalled"
        assert after["frames"] == before["frames"], "idle client drew frames"
        control(self.socket, "look 0 0")
        move = control(self.socket, "move forward 1")
        assert 2 < move["moved"] < 6, move
        authoritative = self.server_position()
        predicted = control(self.socket, "state")["player"]["position"]
        assert sum((a - b) ** 2 for a, b in zip(authoritative, predicted)) < 0.25, (authoritative, predicted)
        turn = control(self.socket, "turn 45 0 --over 0.2")
        assert 40 < turn["yaw"] < 50, turn
        image = self.snapshot()
        assert self.image.read_bytes().startswith(b"\x89PNG"), image
        assert image.get("capture", {}).get("tick", 0) > before["ticks"], image
        # Verify stop interrupts an outstanding action without a picture being drawn.
        result = []
        def move_until_cancelled():
            try:
                control(self.socket, "move forward 10")
            except RuntimeError as error:
                result.append(str(error))
        worker = threading.Thread(target=move_until_cancelled)
        worker.start()
        until(lambda: control(self.socket, "state").get("busy") == "move")
        control(self.socket, "stop")
        worker.join(5)
        assert not worker.is_alive() and result, "stop did not cancel the action"
        assert not control(self.socket, "status")["keys"], "stop left keys held"
        # Navigation may use a grid to plan, but must not execute a Manhattan staircase.
        self.reset()
        trace = []
        outcome = []
        def diagonal():
            try:
                outcome.append(control(self.socket, "goto -16.5 65 16.5 --near 0.7 --timeout 20"))
            except RuntimeError as error:
                outcome.append({"error": str(error)})
        walking = threading.Thread(target=diagonal)
        walking.start()
        while walking.is_alive():
            trace.append(control(self.socket, "state"))
            time.sleep(0.05)
        walking.join()
        assert outcome and outcome[0].get("arrived"), outcome
        points = [state["player"]["position"] for state in trace if state.get("player")]
        assert points and any(p[0] < -2 and p[2] > 2 for p in points), points
        deviation = max(abs(-17 * (p[2] - 0.5) - 16 * (p[0] - 0.5)) / (17 ** 2 + 16 ** 2) ** 0.5 for p in points)
        assert deviation < 0.6, ("diagonal travel still follows grid centres", deviation)
        for a, b in zip(trace, trace[1:]):
            ticks = b["ticks"] - a["ticks"]
            turn = (b["player"]["yaw"] - a["player"]["yaw"] + 180) % 360 - 180
            assert abs(turn) <= 9.01 * max(1, ticks), ("snapped head rotation", turn, ticks)
        self.reset()
        around_wall = control(self.socket, "goto 16.5 65 0.5 --near 0.7 --timeout 25")
        assert around_wall.get("arrived"), around_wall
        # Same tick-indexed inputs with and without continuously drawing pictures.
        endpoints = []
        for mode in ("idle", "live"):
            self.reset()
            control(self.socket, f"vision {mode}")
            replay = control(self.socket, "move-ticks forward 20")
            assert replay["client_ticks"] == 20, replay
            time.sleep(0.5)
            endpoints.append(self.server_position())
        assert sum((a - b) ** 2 for a, b in zip(*endpoints)) < 0.25, endpoints
        control(self.socket, "vision idle")
        self.console(f"tp {BOT} 5.5 65 4.5 -90 0")
        until(lambda: abs(control(self.socket, "state")["player"]["position"][0] - 5.5) < 0.1)
        control(self.socket, "move forward 1.5")
        wall = self.server_position()
        assert 7 < wall[0] <= 7.71, wall
        self.console(f"tp {BOT} -6.5 65 0.5 90 0")
        until(lambda: abs(control(self.socket, "state")["player"]["position"][0] + 6.5) < 0.1)
        control(self.socket, "move forward 1.2 --jump")
        step = self.server_position()
        assert step[0] < -8 and step[1] >= 66, step
        # A second disposable client is a visible moving follow target, not a production account.
        target_env = isolated_env()
        target_socket = Path(target_env["HORIZON_HOME"]) / "target.sock"
        target = self.launch_bot("cheesemany", target_socket, target_env)
        self.reset()
        control(target_socket, "command tp @s 1.7 65 0.5")
        until(lambda: abs(control(target_socket, "state")["player"]["position"][0] - 1.7) < 0.1)
        until(lambda: control(self.socket, "follow cheesemany").get("ok"))
        until(lambda: abs(control(self.socket, "state")["player"]["yaw"] + 90) < 5, 5)
        assert abs(self.server_position()[0] - 0.5) < 0.1, "idle head tracking moved the bot"
        control(target_socket, "command tp @s 0.5 65 14.5")
        until(lambda: abs(control(target_socket, "state")["player"]["position"][2] - 14.5) < 0.1)
        until(lambda: control(self.socket, "follow cheesemany").get("ok"))
        until(lambda: control(self.socket, "state")["player"]["position"][2] > 11, 20)
        control(target_socket, "command tp @s 0.5 65 24.5")
        until(lambda: control(self.socket, "state")["player"]["position"][2] > 21, 20)
        control(self.socket, "stop")
        time.sleep(0.3)
        stopped = self.server_position()
        control(target_socket, "command tp @s 0.5 65 34.5")
        time.sleep(0.5)
        assert abs(self.server_position()[2] - stopped[2]) < 0.1, "follow resumed after stop"
        control(self.socket, "follow cheesemany")
        control(target_socket, f"command kill {BOT}")
        until(lambda: control(self.socket, "state")["screen"] == "dead")
        assert control(self.socket, "state").get("following") is None, "death kept follow intent"
        assert not control(self.socket, "status")["keys"], "death left keys held"
        control(self.socket, "respawn")
        until(lambda: control(self.socket, "state")["screen"] == "playing")
        self.reset()
        control(target_socket, "command tp @s 0.5 65 14.5")
        until(lambda: abs(control(target_socket, "state")["player"]["position"][2] - 14.5) < 0.1)
        until(lambda: control(self.socket, "follow cheesemany").get("ok"))
        control(target_socket, "quit")
        target.wait(timeout=15)
        until(lambda: control(self.socket, "state").get("following") is None)
        assert not control(self.socket, "status")["keys"], "lost follow target left keys held"
        self.reset()
        print("PASS: idle ticks / zero idle frames; server-confirmed movement, wall and step; smooth diagonal travel, wall detour and bounded turns; idle head tracking; frameless mouse turn; snapshot metadata; cancellation; idle/live input replay; follow, retarget, stop, death/respawn and target loss", flush=True)

    def close(self):
        for child in reversed(self.children):
            if child.poll() is None:
                child.terminate()
        for child in reversed(self.children):
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()


PAGE = b"""<!doctype html><meta charset="utf-8"><title>Horizon bot lab</title>
<style>body{background:#151719;color:#e7e9eb;font:16px system-ui;max-width:1040px;margin:40px auto;padding:0 20px}button,input{font:inherit;padding:8px;margin:4px}button{cursor:pointer}img{width:100%;image-rendering:pixelated;background:#222;min-height:180px}pre{white-space:pre-wrap}small{color:#aab}</style>
<h1>Horizon bot lab <small>v1</small></h1>
<p>Join Minecraft Java 26.3 at <strong id="address"></strong>. This is an isolated, offline-mode localhost server.</p>
<p><input id="name" value="cheesemany" aria-label="Player to follow">
<button onclick="act('follow',{name:document.querySelector('#name').value})">Follow player</button>
<button onclick="act('stop')">Stop / stay</button><button onclick="act('jump')">Jump</button>
<button onclick="act('respawn')">Respawn after death</button>
<button onclick="act('reset')">Return bot to start</button></p>
<p><input id="x" value="0.5" size="5" aria-label="X"><input id="y" value="65" size="5" aria-label="Y"><input id="z" value="12.5" size="5" aria-label="Z">
<button onclick="act('goto',{x:document.querySelector('#x').value,y:document.querySelector('#y').value,z:document.querySelector('#z').value})">Walk to coordinates</button></p>
<p><button onclick="picture()">Request bot's view</button><label><input id="live" type="checkbox">Watch (one picture per second)</label></p>
<img id="view" alt="Request a picture to see the bot's own first-person view">
<pre id="capture"></pre><pre id="result" role="status"></pre><pre id="state"></pre>
<p>Pictures are client observations, not server truth. The renderer and meshes stay allocated in v1. No digging, building or chat-based authorization.</p>
<script>
let capturing=false;
async function act(action,extra={}){try{const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,...extra})});const d=await r.json();document.querySelector('#result').textContent=JSON.stringify(d,null,2);return d}catch(e){document.querySelector('#result').textContent=String(e)}}
async function picture(){if(capturing)return;capturing=true;try{const d=await act('snapshot');if(d?.ok){document.querySelector('#view').src='/image.png?t='+Date.now();document.querySelector('#capture').textContent=JSON.stringify(d.capture,null,2)}}finally{capturing=false}}
async function state(){try{const d=await(await fetch('/api/state')).json();document.querySelector('#address').textContent=d.address;document.querySelector('#state').textContent=JSON.stringify(d,null,2)}catch(e){document.querySelector('#state').textContent=String(e)}}
setInterval(state,1000);setInterval(()=>{if(document.querySelector('#live').checked)picture()},1000);state();
</script>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accept-eula", action="store_true")
    parser.add_argument("--server-jar", type=Path, required=True)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--java", type=Path, default=Path("java"))
    parser.add_argument("--game", type=Path, default=ROOT / "target/fast/horizon-client")
    parser.add_argument("--port", type=int, default=25576)
    parser.add_argument("--web-port", type=int, default=8776)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.accept_eula:
        parser.error("read https://www.minecraft.net/eula and pass --accept-eula if you agree")
    for name in ("server_jar", "resources", "game"):
        setattr(args, name, getattr(args, name).resolve(strict=True))
    # Reserve both loopback ports before creating a world or starting any process.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", args.port))
    lab = Lab(args)

    class Handler(BaseHTTPRequestHandler):
        def reply(self, value, code=200, kind="application/json"):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get("Host") != f"127.0.0.1:{args.web_port}":
                return self.reply({"error": "loopback host required"}, 403)
            try:
                if self.path == "/":
                    return self.reply(PAGE, kind="text/html; charset=utf-8")
                if self.path.startswith("/image.png"):
                    with lab.image_lock:
                        return self.reply(lab.image.read_bytes(), kind="image/png")
                if self.path == "/api/state":
                    status = control(lab.socket, "status")
                    return self.reply({
                        "address": f"127.0.0.1:{args.port}", "bot": BOT,
                        "state": control(lab.socket, "state"), "frames": status["frames"],
                        "presentation": status["presentation"], "decode_errors": (status["server"] or {}).get("decode_errors"),
                    })
                self.reply({"error": "not found"}, 404)
            except (OSError, RuntimeError, KeyError) as error:
                self.reply({"error": str(error)}, 503)

        def do_POST(self):
            # No cross-origin controls, HTML forms, or DNS-rebound Host names.
            expected = f"127.0.0.1:{args.web_port}"
            if self.headers.get("Host") != expected or self.headers.get("Origin", f"http://{expected}") != f"http://{expected}":
                return self.reply({"error": "loopback origin required"}, 403)
            if self.path != "/api/action" or self.headers.get("Content-Type") != "application/json":
                return self.reply({"error": "JSON action required"}, 400)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 2048:
                    raise ValueError("invalid request size")
                self.reply(lab.action(json.loads(self.rfile.read(size))))
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                self.reply({"error": str(error)}, 400)

        def log_message(self, *_):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", args.web_port), Handler)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        lab.start()
        if args.check:
            lab.check()
        info = {"directory": str(lab.directory), "socket": str(lab.socket),
                "server": f"127.0.0.1:{args.port}", "panel": f"http://127.0.0.1:{args.web_port}",
                "pid": os.getpid()}
        (ROOT / ".local").mkdir(exist_ok=True)
        info_name = "bot-lab.json" if args.port == 25576 else f"bot-lab-{args.port}.json"
        (ROOT / ".local" / info_name).write_text(json.dumps(info, indent=2))
        print(json.dumps(info), flush=True)
        http.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        http.server_close()
        lab.close()


if __name__ == "__main__":
    main()
