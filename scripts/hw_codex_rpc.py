"""Send observations to an already-loaded Codex thread over local JSON-RPC."""
from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import threading
import time

RPC_TIMEOUT = 20


class CodexRpc:
    def __init__(self, command: list[str] | str, *, timeout: float = RPC_TIMEOUT,
                 retain_notifications: bool = False):
        self.timeout = timeout
        self.sequence = 0
        self.replies = queue.Queue()
        self.connection_error = None
        self.retain_notifications = retain_notifications
        self.write_lock = threading.Lock()
        self.errors = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
        self.socket = None
        try:
            if isinstance(command, str):
                from hw_codex_socket import SocketTransport
                self.socket = SocketTransport(command, timeout=timeout)
            else:
                self.process = subprocess.Popen(
                    command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=self.errors, text=True, encoding="utf-8", errors="replace",
                    bufsize=1)
        except Exception:
            self.errors.close()
            raise
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.request("initialize", {"clientInfo": {
                "name": "herdr_watch", "version": "1.0"},
                "capabilities": {"experimentalApi": True}})
            self._write({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def _read(self) -> None:
        try:
            if self.socket:
                for message in self.socket.messages():
                    self._dispatch(message)
            else:
                for line in self.process.stdout:
                    try:
                        self._dispatch(json.loads(line))
                    except ValueError:
                        continue
        except Exception as exc:
            self.connection_error = exc
            self.replies.put(exc)
        finally:
            if self.connection_error is None:
                self.connection_error = RuntimeError("Codex connection closed")
            self.replies.put(None)

    def ensure_connected(self) -> None:
        if self.connection_error is not None:
            raise RuntimeError("Codex connection closed or failed: %s" % self.connection_error)

    def _dispatch(self, message: dict) -> None:
        if "method" in message and "id" in message:
            self._write({"id": message["id"], "error": {"code": -32601,
                         "message": "The watcher cannot answer approval requests"}})
        elif "id" in message or self.retain_notifications:
            self.replies.put(message)

    def _write(self, message: dict) -> None:
        with self.write_lock:
            self._send(message)

    def _send(self, message: dict) -> None:
        if self.socket:
            self.socket.send(message)
            return
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise RuntimeError("Codex connection closed") from exc

    def request(self, method: str, params: dict) -> dict:
        self.sequence += 1
        ident = self.sequence
        self._write({"id": ident, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if time.monotonic() >= deadline:
                    raise queue.Empty
                reply = self.replies.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise RuntimeError("Codex acknowledgement timed out; delivery is uncertain, "
                                   "so the event was not retried") from exc
            if reply is None:
                raise RuntimeError("Codex connection closed; connect to the App Server "
                                   "hosting this thread (check --codex-url/--codex-socket)")
            if isinstance(reply, Exception):
                raise RuntimeError("Codex transport failed: %s" % reply) from reply
            if reply.get("id") == ident:
                if "error" in reply:
                    raise RuntimeError(str(reply["error"].get("message", reply["error"])))
                return reply.get("result", {})

    def check_thread(self, thread: str) -> dict:
        result = self.request("thread/read", {"threadId": thread, "includeTurns": False})
        info = result.get("thread", {})
        if info.get("id") != thread:
            raise RuntimeError("Codex returned a different thread; refusing delivery")
        if info.get("status", {}).get("type") not in ("idle", "active"):
            raise RuntimeError("Target thread must already be loaded and idle or active "
                               "in this App Server; the watcher will not resume a saved copy")
        return info

    def deliver(self, thread: str, event: str) -> dict:
        self.check_thread(thread)
        return self.request("turn/start", {"threadId": thread, "input": [],
                            "toolOutput": {"name": "herdr_watch", "output": event}})

    def close(self) -> None:
        if self.socket:
            self.socket.close()
            self.errors.close()
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdin, self.process.stdout, self.errors):
            stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def proxy_command(binary: str, socket: str = "") -> list[str]:
    command = [binary, "app-server", "proxy"]
    return command + ["--sock", socket] if socket else command
