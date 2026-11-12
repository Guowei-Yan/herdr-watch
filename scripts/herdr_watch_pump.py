#!/usr/bin/env python3
"""Run a herdr-watch watcher and atomically spool events to a file-based inbox.

The plain watcher prints to a log, which nothing reads on its own. This wrapper
tees each event (a line starting with "[herdr-watch <label>]") into
`<root>/inbox/*.event` (default `~/.pi/agent/herdr-watch/inbox`) via atomic
rename so a user-supplied host extension or local file-polling consumer can
drain events into an active conversation.

  herdr_watch_pump.py --label worker-1 -- watch --socket ... --pane ... --once

Everything after `--` is passed to herdr_watch.py unchanged. Exit status is the
watcher's. Stdout still carries the raw lines, so a normal log still works.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PREFIX = re.compile(r"^\[herdr-watch (?P<label>[^\]]+)\] (?P<event>.*)$")
HEADER = re.compile(r"^\[herdr-watch (?P<label>[^\]]+)\] (?P<event>ARMED|STOPPED|BLOCKED|GONE|BACK|REBOUND|ENDED)")
DEFAULT_ROOT = Path.home() / ".pi" / "agent" / "herdr-watch"


def watcher_script() -> Path:
    return Path(__file__).resolve().with_name("herdr_watch.py")


def publish(inbox: Path, label: str, body: str) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    stamp = time.time_ns()
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", label) or "watch"
    path = inbox / f"{stamp:020d}-{safe}.event"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="herdr-watch")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--quiet", action="store_true", help="do not echo watcher output")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    options = parser.parse_args()

    args = options.args
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        parser.error("pass the herdr_watch.py subcommand and its flags after --")

    command = [sys.executable, str(watcher_script()), *args]
    inbox = options.root / "inbox"

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    block: list[str] = []
    label = options.label

    def flush() -> None:
        if not block:
            return
        body = "\n".join(block).strip()
        block.clear()
        if not body:
            return
        publish(inbox, label, body)
        if not options.quiet:
            print(body, flush=True)

    assert process.stdout is not None
    try:
        for raw in process.stdout:
            line = raw.rstrip("\n")
            match = PREFIX.match(line)
            if match:
                label = match.group("label") or label
                if HEADER.match(line) or block:
                    flush()
                block.append(line)
                # ARMED/ENDED are self-contained; publish immediately
                if match.group("event").startswith(("ARMED", "ENDED", "GONE", "BACK", "REBOUND")):
                    flush()
            elif block:
                block.append(line)
            elif not options.quiet:
                print(line, flush=True)
    except KeyboardInterrupt:
        process.terminate()
    finally:
        flush()
        try:
            return process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
