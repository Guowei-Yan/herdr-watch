#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Background herdr watchers with event delivery into an existing Codex App Server."""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from hw_background import process_options, read_state, run_monitor, save_state
from hw_codex_rpc import CodexRpc, proxy_command

WATCHERS = ("watch", "watch-git", "watch-subagents")
START_TIMEOUT = 30
STALE_AFTER = 60


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="action", required=True)
    for action in ("doctor", "start", "list", "stop", "_run"):
        sub = commands.add_parser(action)
        sub.add_argument("--state-dir", type=Path,
                         default=Path.home() / ".cache" / "herdr-watch")
        if action in ("doctor", "start"):
            sub.add_argument("--thread", default=os.environ.get("CODEX_THREAD_ID", ""))
            endpoints = sub.add_mutually_exclusive_group()
            endpoints.add_argument("--codex-url", default="",
                                   help="local ws:// or unix:// App Server endpoint")
            endpoints.add_argument("--codex-socket", default="",
                                   help="managed daemon control socket (proxy transport)")
            sub.add_argument("--codex-bin", default="codex")
        if action in ("stop", "_run"):
            sub.add_argument("--id", required=True)
        if action == "start":
            sub.add_argument("watcher", nargs=argparse.REMAINDER,
                             help="-- watch|watch-git|watch-subagents <watcher flags>")
    return root


def monitor_folder(root: Path, ident: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", ident):
        raise ValueError("Invalid monitor id; use an id returned by start/list")
    folder = root.expanduser().resolve() / ident
    if folder.is_symlink():
        raise ValueError("Monitor folders must not be symlinks")
    return folder


def watcher_command(arguments: list[str]) -> list[str]:
    arguments = arguments[1:] if arguments[:1] == ["--"] else arguments
    if not arguments or arguments[0] not in WATCHERS:
        raise ValueError("Expected -- watch, watch-git, or watch-subagents with its flags")
    from herdr_watch import build_parser
    build_parser().parse_args(arguments)
    entry = Path(__file__).resolve().with_name("herdr_watch.py")
    return [sys.executable, "-B", str(entry)] + arguments


def connection(args):
    return args.codex_url or proxy_command(args.codex_bin, args.codex_socket)


def check_connection(args) -> dict:
    if not args.thread:
        raise ValueError("Set --thread to the receiving conversation's UUID "
                         "or run inside Codex with CODEX_THREAD_ID")
    with CodexRpc(connection(args)) as client:
        info = client.check_thread(args.thread)
    return {"status": "connected", "thread": args.thread,
            "runtime_status": info["status"]["type"]}


def start(args) -> dict:
    command = watcher_command(args.watcher)
    check_connection(args)
    args.state_dir = args.state_dir.expanduser().resolve()
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _check_duplicate(args, command)
    ident = uuid.uuid4().hex
    folder = monitor_folder(args.state_dir, ident)
    folder.mkdir(mode=0o700)
    state = {"id": ident, "thread": args.thread, "connection": connection(args),
             "command": command, "status": "starting", "created_at": time.time()}
    save_state(folder, state)
    try:
        _launch(args, folder)
    except OSError as exc:
        state.update(status="failed", error=str(exc))
        save_state(folder, state)
        raise
    return _wait_started(folder)


def _check_duplicate(args, command: list[str]) -> None:
    for state in list_monitors(args.state_dir):
        if (state.get("status") in ("starting", "running")
                and state.get("thread") == args.thread
                and state.get("connection") == connection(args)
                and state.get("command") == command):
            raise ValueError("This watch is already active: " + state["id"])


def _launch(args, folder: Path) -> None:
    worker = [sys.executable, "-B", str(Path(__file__).resolve()), "_run",
              "--state-dir", str(args.state_dir), "--id", folder.name]
    options = process_options()
    if os.name == "nt":
        options["creationflags"] |= subprocess.DETACHED_PROCESS
    with (folder / "worker.log").open("a", encoding="utf-8") as log:
        subprocess.Popen(worker, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         close_fds=True, **options)


def _wait_started(folder: Path) -> dict:
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        state = read_state(folder)
        if state["status"] in ("running", "ended"):
            if not state.get("last_delivery"):
                raise RuntimeError("Watcher exited without an acknowledged ARMED event; "
                                   "inspect " + str(folder))
            return state
        if state["status"] in ("failed", "stopped"):
            raise RuntimeError(state.get("error", "Monitor stopped before arming")
                               + "; inspect " + str(folder))
        time.sleep(0.1)
    (folder / "stop").touch()
    raise RuntimeError("Monitor startup timed out; stop requested. Inspect " + str(folder))


def list_monitors(root: Path) -> list[dict]:
    result = []
    for path in sorted(root.expanduser().glob("*/state.json")):
        if path.parent.is_symlink():
            continue
        try:
            state = read_state(path.parent)
        except (OSError, ValueError):
            continue
        if (state.get("status") in ("starting", "running")
                and time.time() - state.get("updated_at", 0) > STALE_AFTER):
            state["status"] = "unresponsive"
        state["log_dir"] = str(path.parent)
        result.append(state)
    return result


def stop(args) -> dict:
    folder = monitor_folder(args.state_dir, args.id)
    state = read_state(folder)
    if state["status"] in ("starting", "running"):
        (folder / "stop").touch()
        return {"id": args.id, "status": "stop-requested"}
    return state


def worker(args) -> dict:
    folder = monitor_folder(args.state_dir, args.id)
    state = read_state(folder)
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        with CodexRpc(state["connection"]) as client:
            client.check_thread(state["thread"])
            run_monitor(state, folder, client)
    except KeyboardInterrupt:
        state.update(status="stopped")
        save_state(folder, state)
    except Exception as exc:
        state.update(status="failed", error=str(exc))
        save_state(folder, state)
    return read_state(folder)


def _interrupt(*_) -> None:
    raise KeyboardInterrupt


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.action == "doctor":
            result = check_connection(args)
        elif args.action == "start":
            result = start(args)
        elif args.action == "list":
            result = list_monitors(args.state_dir)
        elif args.action == "stop":
            result = stop(args)
        else:
            result = worker(args)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print("herdr-watch: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
