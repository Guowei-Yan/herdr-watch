"""Small standard-library WebSocket transport for a local Codex App Server."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from select import select
import socket
import struct
import threading
import time
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_HEADERS = 64 * 1024
MAX_PAYLOAD = 8 * 1024 * 1024

class _ConnectionEnded(Exception):
    pass

def _endpoint(url: str):
    if not isinstance(url, str):
        raise ValueError("Codex WebSocket endpoint must be a string")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid Codex WebSocket endpoint") from exc
    if "#" in url:
        raise ValueError("WebSocket endpoint must not contain a fragment")
    if parsed.scheme == "unix":
        if parsed.netloc or parsed.query or not parsed.path.startswith("/"):
            raise ValueError("expected unix:///absolute/socket/path")
        return socket.AF_UNIX, parsed.path, "/", "localhost"
    if parsed.scheme != "ws":
        raise ValueError("endpoint must use unix:// or ws://")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("WebSocket endpoint must not contain credentials")
    host = parsed.hostname
    if host not in ("127.0.0.1", "localhost", "::1") or port is None:
        raise ValueError("WebSocket endpoint must name a local host and port")
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    if "\r" in target or "\n" in target:
        raise ValueError("invalid WebSocket request target")
    host_header = ("[::1]" if host == "::1" else host) + ":" + str(port)
    connect_host = "127.0.0.1" if host == "localhost" else host
    family = socket.AF_INET6 if host == "::1" else socket.AF_INET
    return family, (connect_host, port), target, host_header

def _mask_frame(opcode: int, payload: bytes) -> bytes:
    mask = os.urandom(4)
    length = len(payload)
    if length < 126:
        header = bytes((0x80 | opcode, 0x80 | length))
    elif length < 65536:
        header = bytes((0x80 | opcode, 0xfe)) + struct.pack("!H", length)
    else:
        header = bytes((0x80 | opcode, 0xff)) + struct.pack("!Q", length)
    masked = bytes(value ^ mask[index % 4]
                   for index, value in enumerate(payload))
    return header + mask + masked

def _parse_response(response: bytes, key: str) -> None:
    lines = response.decode("latin-1").split("\r\n")
    status = lines[0].split(" ", 2)
    if len(status) < 2 or status[0] != "HTTP/1.1" or status[1] != "101":
        raise RuntimeError("WebSocket upgrade was rejected")
    headers = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            raise RuntimeError("malformed WebSocket upgrade response")
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    expected = base64.b64encode(
        hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
    if headers.get("sec-websocket-accept") != expected:
        raise RuntimeError("invalid Sec-WebSocket-Accept")
    if headers.get("upgrade", "").lower() != "websocket":
        raise RuntimeError("invalid WebSocket Upgrade header")
    connection = [token.strip().lower() for token in
                  headers.get("connection", "").split(",")]
    if "upgrade" not in connection:
        raise RuntimeError("invalid WebSocket Connection header")

def _valid_close_code(code: int) -> bool:
    return (code not in (1004, 1005, 1006, 1015) and
            (1000 <= code <= 1014 or 3000 <= code <= 4999))

def _reject_json_constant(value: str):
    raise ValueError("invalid JSON constant: " + value)

class SocketTransport:
    def __init__(self, url: str, *, timeout: float = 20):
        family, address, self.target, self.host = _endpoint(url)
        self.socket = socket.socket(family, socket.SOCK_STREAM)
        self.buffer = bytearray()
        self.closed = False
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        try:
            self.socket.settimeout(timeout)
            self.timeout = timeout
            self.socket.connect(address)
            self._upgrade()
        except Exception:
            self.socket.close()
            self.closed = True
            raise
        self.socket.settimeout(None)

    def _upgrade(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = ("GET " + self.target + " HTTP/1.1\r\nHost: " + self.host +
                   "\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Key: " + key +
                   "\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.socket.sendall(request.encode("ascii"))
        _parse_response(self._receive_headers(), key)

    def _receive_headers(self) -> bytes:
        while True:
            end = self.buffer.find(b"\r\n\r\n")
            if end >= 0:
                break
            if len(self.buffer) > MAX_HEADERS:
                raise RuntimeError("WebSocket upgrade response headers are too large")
            chunk = self.socket.recv(4096)
            if not chunk:
                raise RuntimeError("connection closed during WebSocket upgrade")
            self.buffer.extend(chunk)
        if end > MAX_HEADERS:
            raise RuntimeError("WebSocket upgrade response headers are too large")
        headers = bytes(self.buffer[:end])
        del self.buffer[:end + 4]
        return headers

    def _receive_exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            try:
                chunk = self.socket.recv(size - len(self.buffer))
            except OSError as exc:
                if self.closed:
                    raise _ConnectionEnded from exc
                raise RuntimeError("WebSocket receive failed") from exc
            if not chunk:
                if self.closed:
                    raise _ConnectionEnded
                raise RuntimeError("WebSocket connection closed")
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        frame = _mask_frame(opcode, payload)
        with self._send_lock:
            with self._state_lock:
                if self.closed:
                    raise RuntimeError("WebSocket connection is closed")
            try:
                self._send_bytes(frame)
            except RuntimeError:
                self.close()
                raise

    def _send_bytes(self, data: bytes) -> None:
        deadline, remaining = time.monotonic() + self.timeout, memoryview(data)
        flags = getattr(socket, "MSG_DONTWAIT", 0)
        while remaining:
            wait = deadline - time.monotonic()
            if wait <= 0:
                raise RuntimeError("WebSocket send timed out")
            try:
                _, writable, _ = select([], [self.socket], [], wait)
                if not writable:
                    raise RuntimeError("WebSocket send timed out")
                chunk = remaining[:64 * 1024] if flags else remaining[:1]
                sent = self.socket.send(chunk, flags)
            except BlockingIOError:
                continue
            except (OSError, ValueError) as exc:
                raise RuntimeError("WebSocket send failed") from exc
            if not sent:
                raise RuntimeError("WebSocket connection closed during send")
            remaining = remaining[sent:]

    def send(self, message: dict) -> None:
        payload = json.dumps(message, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
        self._send_frame(1, payload)

    def _read_frame(self):
        first, second = self._receive_exact(2)
        final, opcode = bool(first & 0x80), first & 0x0f
        if first & 0x70:
            raise RuntimeError("WebSocket frame uses unsupported reserved bits")
        if second & 0x80:
            raise RuntimeError("server sent a masked WebSocket frame")
        marker = second & 0x7f
        if marker == 126:
            length = struct.unpack("!H", self._receive_exact(2))[0]
            if length < 126:
                raise RuntimeError("WebSocket frame has a non-minimal length")
        elif marker == 127:
            length = struct.unpack("!Q", self._receive_exact(8))[0]
            if length < 65536 or length & (1 << 63):
                raise RuntimeError("WebSocket frame has an invalid length")
        else:
            length = marker
        if opcode >= 8 and (not final or length > 125):
            raise RuntimeError("invalid WebSocket control frame")
        if length > MAX_PAYLOAD:
            raise RuntimeError("WebSocket message is too large")
        return final, opcode, self._receive_exact(length)

    def _handle_control(self, opcode: int, payload: bytes) -> bool:
        if opcode == 9:
            self._send_frame(10, payload)
            return True
        if opcode == 10:
            return True
        if opcode != 8:
            raise RuntimeError("unsupported WebSocket opcode")
        if len(payload) == 1:
            raise RuntimeError("invalid WebSocket close payload")
        if len(payload) >= 2 and not _valid_close_code(struct.unpack("!H", payload[:2])[0]):
            raise RuntimeError("invalid WebSocket close code")
        if len(payload) > 2:
            try:
                payload[2:].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RuntimeError("invalid WebSocket close reason") from exc
        try:
            self._send_frame(8, payload)
        except RuntimeError:
            pass
        self.close()
        return False

    @staticmethod
    def _decode(payload: bytes) -> dict:
        try:
            message = json.loads(payload.decode("utf-8"),
                                 parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("invalid JSON in WebSocket text message") from exc
        if not isinstance(message, dict):
            raise RuntimeError("WebSocket JSON message must be an object")
        return message

    def _handle_data(self, fragments, frame):
        final, opcode, payload = frame
        if opcode == 1:
            if fragments is not None:
                raise RuntimeError("text frame interrupted fragmented message")
            if final:
                return None, self._decode(payload)
            return bytearray(payload), None
        if opcode != 0:
            raise RuntimeError("unsupported WebSocket binary frame")
        if fragments is None:
            raise RuntimeError("unexpected WebSocket continuation frame")
        if len(fragments) + len(payload) > MAX_PAYLOAD:
            raise RuntimeError("WebSocket message is too large")
        fragments.extend(payload)
        if final:
            return None, self._decode(bytes(fragments))
        return fragments, None

    def messages(self):
        fragments = None
        try:
            while not self.closed:
                final, opcode, payload = self._read_frame()
                if opcode >= 8 and not self._handle_control(opcode, payload):
                    return
                if opcode >= 8:
                    continue
                fragments, message = self._handle_data(
                    fragments, (final, opcode, payload))
                if message is not None:
                    yield message
        except _ConnectionEnded:
            return
        except RuntimeError:
            self.close()
            raise
    def close(self) -> None:
        with self._state_lock:
            if self.closed:
                return
            self.closed = True
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.socket.close()
