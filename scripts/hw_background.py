"""Own one watcher process, batch its output, and stop on delivery failure."""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path

BATCH_QUIET = 0.5
BATCH_MAX_AGE = 2
BATCH_CHARS = 12000
STATE_INTERVAL = 5
STOP_POLL = 0.1


def save_state(folder: Path, state: dict) -> None:
    state["updated_at"] = time.time()
    pending = folder / "state.tmp"
    pending.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    pending.replace(folder / "state.json")


def read_state(folder: Path) -> dict:
    return json.loads((folder / "state.json").read_text(encoding="utf-8"))


def process_options() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=3)


def pump_output(process, folder: Path, events: queue.Queue) -> None:
    try:
        with (folder / "events.log").open("a", encoding="utf-8") as log:
            for line in iter(lambda: process.stdout.readline(BATCH_CHARS), ""):
                log.write(line)
                log.flush()
                events.put(line)
    except Exception as exc:
        events.put(exc)
    finally:
        events.put(None)


class Monitor:
    def __init__(self, spec: dict, folder: Path, client):
        self.state = dict(spec)
        self.folder, self.client = folder, client
        self.pending = ""
        self.first_line = self.last_line = 0.0
        self.armed = False
        self.last_saved = 0.0

    def update(self, status: str, **extra) -> None:
        self.state.update(status=status, **extra)
        save_state(self.folder, self.state)
        self.last_saved = time.monotonic()

    def flush(self) -> None:
        if not self.pending:
            return
        self.preserve_pending()
        payload = "Monitor " + self.state["id"] + "\n" + self.pending
        self.client.deliver(self.state["thread"], payload)
        self.pending = ""
        (self.folder / "undelivered.txt").unlink()
        if self.armed:
            self.update("running", last_delivery=time.time())

    def preserve_pending(self) -> None:
        if self.pending:
            (self.folder / "undelivered.txt").write_text(self.pending, encoding="utf-8")

    def add_line(self, line: str) -> None:
        now = time.monotonic()
        if not self.pending:
            self.first_line = now
        self.pending += line
        self.last_line = now
        if line.startswith("[herdr-watch ") and "] ARMED " in line:
            self.armed = True

    def drain(self, process) -> str:
        events = queue.Queue()
        reader = threading.Thread(target=pump_output,
                                  args=(process, self.folder, events), daemon=True)
        reader.start()
        try:
            return self._consume(process, events)
        finally:
            stop_process(process)
            reader.join(timeout=3)
            process.stdout.close()

    def _consume(self, process, events: queue.Queue) -> str:
        while not (self.folder / "stop").exists():
            try:
                line = events.get(timeout=STOP_POLL)
                if isinstance(line, Exception):
                    raise RuntimeError("Watcher output reader failed: %s" % line) from line
                if line is None:
                    self.flush()
                    code = process.wait(timeout=3)
                    if code:
                        raise RuntimeError("Watcher exited with code %s; see events.log" % code)
                    return "ended"
                self.add_line(line)
            except queue.Empty:
                pass
            self._tick()
        self.flush()
        return "stopped"

    def _tick(self) -> None:
        try:
            self.client.ensure_connected()
        except RuntimeError:
            self.preserve_pending()
            raise
        now = time.monotonic()
        if self.pending and (now - self.last_line >= BATCH_QUIET
                             or now - self.first_line >= BATCH_MAX_AGE
                             or len(self.pending) >= BATCH_CHARS):
            self.flush()
        if now - self.last_saved >= STATE_INTERVAL:
            self.update(self.state.get("status", "starting"))


def run_monitor(spec: dict, folder: Path, client) -> None:
    monitor = Monitor(spec, folder, client)
    monitor.update("starting", pid=os.getpid())
    try:
        process = subprocess.Popen(spec["command"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace",
                                   bufsize=1, **process_options())
        status = monitor.drain(process)
        monitor.update(status)
    except KeyboardInterrupt:
        monitor.update("stopped")
    except Exception as exc:
        monitor.update("failed", error=str(exc))
