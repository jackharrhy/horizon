#!/usr/bin/env python3
"""Replace only the disposable lab bot, preserving the running vanilla server/world."""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess

from horizon_bot_lab import BOT, ROOT, control, isolated_env, until


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--lab", type=Path, default=ROOT / ".local/bot-lab.json")
    parser.add_argument("--game", type=Path, default=ROOT / "target/fast/horizon-client")
    parser.add_argument("--restart-missing", action="store_true", help="reconnect when the lab bot has already exited")
    args = parser.parse_args()
    resources = args.resources.resolve(strict=True)
    game = args.game.resolve(strict=True)
    info = json.loads(args.lab.read_text())
    if not re.fullmatch(r"127\.0\.0\.1:\d{1,5}", info["server"]):
        parser.error("only a loopback test server may be reloaded")
    path = Path(info["socket"])
    os.kill(info["pid"], 0)
    try:
        status = control(path, "status")
    except (FileNotFoundError, ConnectionRefusedError):
        if not args.restart_missing:
            parser.error("lab bot is absent; use --restart-missing to reconnect it")
    else:
        remote = status.get("server") or {}
        if remote.get("name") != BOT or remote.get("address") != info["server"]:
            parser.error("the control socket is not the disposable lab bot")
        control(path, "quit")
        until(lambda: not path.exists(), 15)
    env = isolated_env()
    log_path = Path(env["HORIZON_HOME"]) / "client.log"
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    with log_path.open("w") as log:
        process = subprocess.Popen([
            str(game), "--resources", str(resources), "--server", info["server"],
            "--name", BOT, "--bot", "--control-socket", str(path),
            "--render-distance", "6", "--width", "640", "--height", "360",
        ], env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            def ready():
                if process.poll() is not None:
                    raise ChildProcessError(f"bot exited; inspect {log_path}")
                state = control(path, "status")
                return (state.get("server") or {}).get("loaded") or state.get("screen") == "dead"
            until(ready, 120)
            print(json.dumps({"ok": True, "bot_pid": process.pid, "supervisor_pid": os.getpid(),
                              "server_unchanged": info["server"], "log": str(log_path)}), flush=True)
            while True:
                try:
                    process.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    try:
                        os.kill(info["pid"], 0)
                    except ProcessLookupError:
                        break
        except KeyboardInterrupt:
            pass
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
