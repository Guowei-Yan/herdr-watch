import base64
import gc
import hashlib
import json
import socket
import struct
import sys
import tempfile
import threading
import unittest
import warnings
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from hw_codex_socket import SocketTransport


GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

def receive_exact(connection, size):
    chunks, remaining = [], size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise EOFError("peer closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)

def capture_errors(errors, action):
    try:
        action()
    except Exception as exc:
        errors.append(exc)

def read_client_frame(connection):
    first, second = receive_exact(connection, 2)
    length = second & 0x7f
    if length == 126:
        length = struct.unpack("!H", receive_exact(connection, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", receive_exact(connection, 8))[0]
    mask, payload = receive_exact(connection, 4), receive_exact(connection, length)
    decoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return first & 0x0f, bool(second & 0x80), decoded

def server_frame(opcode, payload=b"", *, final=True, masked=False):
    first = opcode | (0x80 if final else 0)
    length = len(payload)
    if length < 126:
        header = bytes((first, length | (0x80 if masked else 0)))
    elif length < 65536:
        header = bytes((first, 126 | (0x80 if masked else 0))) + struct.pack("!H", length)
    else:
        header = bytes((first, 127 | (0x80 if masked else 0))) + struct.pack("!Q", length)
    if not masked:
        return header + payload
    mask = b"test"
    encoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return header + mask + encoded

class UnixPeer:
    def __init__(self, path, initial=b"", *, handler=None, response="valid"):
        self.initial = initial
        self.handler = handler
        self.response = response
        self.error = None
        self.request = b""
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(1)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            connection, _ = self.server.accept()
            with connection:
                connection.settimeout(2)
                while b"\r\n\r\n" not in self.request:
                    self.request += connection.recv(4096)
                key = next(line.split(b":", 1)[1].strip() for line in
                           self.request.split(b"\r\n")
                           if line.lower().startswith(b"sec-websocket-key:"))
                accept = base64.b64encode(hashlib.sha1(key + GUID.encode()).digest())
                if self.response == "bad-accept":
                    accept = b"not-the-expected-value"
                status = (b"HTTP/1.1 403 Forbidden" if self.response == "rejected"
                          else b"HTTP/1.1 101 Switching Protocols")
                response = (status + b"\r\n"
                            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                            b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
                connection.sendall(response + self.initial)
                if self.handler:
                    self.handler(connection)
        except Exception as exc:
            self.error = exc
        finally:
            self.server.close()

    def finish(self):
        self.thread.join(2)
        if self.thread.is_alive():
            self.server.close()
            raise AssertionError("fake WebSocket peer did not finish")
        if self.error:
            raise self.error

class TcpPeer(UnixPeer):
    def __init__(self, initial=b"", *, handler=None, family=socket.AF_INET):
        self.initial = initial
        self.handler = handler
        self.response = "valid"
        self.error = None
        self.request = b""
        host = "::1" if family == socket.AF_INET6 else "127.0.0.1"
        self.server = socket.socket(family, socket.SOCK_STREAM)
        self.server.bind((host, 0))
        self.server.listen(1)
        shown_host = "[::1]" if family == socket.AF_INET6 else host
        self.url = "ws://" + shown_host + ":" + str(self.server.getsockname()[1])
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

class SocketTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def connect_unix(self, name, initial=b"", *, timeout=2, **peer_options):
        path = Path(self.temp.name) / name
        peer = UnixPeer(path, initial, **peer_options)
        transport = SocketTransport("unix://" + str(path), timeout=timeout)
        self.addCleanup(transport.close)
        return peer, transport

    def test_unix_upgrade_preserves_frames_and_send_masks_json(self):
        received = []
        peer, transport = self.connect_unix(
            "codex.sock", server_frame(1, b'{"id":7,"result":{"ok":true}}'),
            handler=lambda connection: received.append(read_client_frame(connection)))
        with self.assertRaises(ValueError):
            transport.send({"invalid": float("nan")})
        transport.send({"id": 7, "method": "initialize", "params": {}})
        self.assertEqual({"id": 7, "result": {"ok": True}},
                         next(transport.messages()))
        peer.finish()
        opcode, masked, payload = received[0]
        self.assertEqual(1, opcode)
        self.assertTrue(masked)
        self.assertEqual({"id": 7, "method": "initialize", "params": {}},
                         json.loads(payload))

    def test_tcp_upgrade_uses_url_request_target(self):
        peer = TcpPeer(server_frame(1, b'{"method":"ready"}'))
        self.addCleanup(peer.finish)
        transport = SocketTransport(peer.url + "/codex?client=watch", timeout=2)
        self.addCleanup(transport.close)
        self.assertEqual({"method": "ready"}, next(transport.messages()))
        peer.finish()
        self.assertTrue(peer.request.startswith(
            b"GET /codex?client=watch HTTP/1.1\r\n"))

    def test_ipv6_loopback_endpoint_connects_when_available(self):
        try:
            peer = TcpPeer(server_frame(1, b'{"family":"ipv6"}'),
                           family=socket.AF_INET6)
        except OSError as exc:
            self.skipTest("IPv6 loopback unavailable: " + str(exc))
        self.addCleanup(peer.finish)
        transport = SocketTransport(peer.url, timeout=2)
        self.addCleanup(transport.close)
        self.assertEqual({"family": "ipv6"}, next(transport.messages()))
        peer.finish()

    def test_invalid_endpoints_and_timeout_are_rejected_without_leaks(self):
        invalid = [
            "http://localhost:8080", "wss://localhost:8080",
            "ws://example.com:8080", "ws://user@localhost:8080",
            "ws://localhost:8080/#fragment", "ws://localhost:8080/#",
            "ws://localhost",
            "unix://host/tmp/codex.sock", "unix:relative.sock",
            "unix:///tmp/codex.sock?unused=true",
        ]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ValueError):
                SocketTransport(url, timeout=0.01)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with self.assertRaises(ValueError):
                SocketTransport("unix:///tmp/not-used.sock", timeout=-1)
            gc.collect()
        self.assertFalse(any(isinstance(item.message, ResourceWarning)
                             for item in caught))

    def test_upgrade_rejects_bad_status_and_accept_header(self):
        for response in ("rejected", "bad-accept"):
            with self.subTest(response=response):
                path = Path(self.temp.name) / (response + ".sock")
                peer = UnixPeer(path, response=response)
                with self.assertRaisesRegex(RuntimeError, "upgrade|Accept"):
                    SocketTransport("unix://" + str(path), timeout=2)
                peer.finish()

    def test_fragments_ping_pong_and_close_are_handled(self):
        replies = []
        message = (server_frame(1, b'{"result":', final=False) +
                   server_frame(9, b"probe") +
                   server_frame(0, b'{"ready":true}}') +
                   server_frame(8, struct.pack("!H", 1000)))
        peer, transport = self.connect_unix(
            "controls.sock", message, handler=lambda connection: replies.extend(
                read_client_frame(connection) for _ in range(2)))
        self.assertEqual([{"result": {"ready": True}}], list(transport.messages()))
        peer.finish()
        self.assertEqual([(10, True, b"probe"),
                          (8, True, struct.pack("!H", 1000))], replies)
        self.assertTrue(transport.closed)

    def test_oversized_frame_and_fragmented_message_are_rejected(self):
        chunk = b"x" * (4 * 1024 * 1024 + 1)
        cases = [bytes((0x81, 127)) + struct.pack("!Q", 8 * 1024 * 1024 + 1),
                 server_frame(1, chunk, final=False) + server_frame(0, chunk)]
        for index, oversized in enumerate(cases):
            peer, transport = self.connect_unix("large-" + str(index), oversized)
            with self.assertRaisesRegex(RuntimeError, "large"):
                next(transport.messages())
            peer.finish()

    def test_protocol_and_json_errors_are_not_swallowed(self):
        cases = [
            (server_frame(0, b"{}"), "continuation"),
            (server_frame(1, b"{}", masked=True), "masked"),
            (server_frame(1, b"{"), "JSON"),
            (server_frame(1, b'{"value":NaN}'), "JSON"),
            (server_frame(1, b"[]"), "object"),
            (server_frame(8, struct.pack("!H", 1005)), "close code"),
        ]
        for index, (frame, message) in enumerate(cases):
            with self.subTest(message=message):
                peer, transport = self.connect_unix(
                    "error-" + str(index) + ".sock", frame)
                with self.assertRaisesRegex(RuntimeError, message):
                    next(transport.messages())
                transport.close()
                peer.finish()

    def test_send_timeout_does_not_make_idle_receives_time_out(self):
        release_peer, errors = threading.Event(), []
        peer, transport = self.connect_unix(
            "timeout.sock", timeout=0.1,
            handler=lambda connection: release_peer.wait(2))
        transport.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        sender = threading.Thread(target=capture_errors, args=(
            errors, lambda: transport.send({"payload": "x" * (4 * 1024 * 1024)})))
        sender.start()
        sender.join(1)
        finished = not sender.is_alive()
        release_peer.set()
        transport.close()
        sender.join(2)
        peer.finish()
        self.assertTrue(finished)
        self.assertRegex(str(errors[0]), "timed out")

    def test_close_interrupts_a_blocked_sender_and_reader(self):
        release_peer = threading.Event()
        peer, transport = self.connect_unix(
            "blocking.sock", handler=lambda connection: release_peer.wait(2))
        transport.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        reader_errors, sender_errors = [], []
        reader = threading.Thread(target=capture_errors, args=(
            reader_errors, lambda: next(transport.messages())))
        sender = threading.Thread(target=capture_errors, args=(
            sender_errors, lambda: transport.send(
                {"payload": "x" * (4 * 1024 * 1024)})))
        reader.start()
        sender.start()
        for _ in range(200):
            if transport._send_lock.locked():
                break
            sender.join(0.01)
        self.assertTrue(transport._send_lock.locked())
        closer = threading.Thread(target=transport.close)
        closer.start()
        closer.join(0.5)
        closed_promptly = not closer.is_alive()
        release_peer.set()
        for thread in (reader, sender, closer):
            thread.join(2)
        peer.finish()
        self.assertTrue(closed_promptly)
        self.assertFalse(reader.is_alive() or sender.is_alive())
        self.assertTrue(not reader_errors or isinstance(reader_errors[0], StopIteration))
        self.assertTrue(sender_errors)

if __name__ == "__main__":
    unittest.main()
