import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from hw_codex_rpc import CodexRpc


class CodexRpcTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "requests.jsonl"

    def client(self, mode="idle"):
        command = [sys.executable, "-B", str(Path(__file__).with_name("fake_rpc.py")),
                   str(self.log), mode]
        client = CodexRpc(command, timeout=2)
        self.addCleanup(client.close)
        return client

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_event_remains_tool_output_on_the_selected_thread(self):
        client = self.client()
        client.check_thread("thread-test")
        client.deliver("thread-test", "[herdr-watch test] HEARTBEAT idle")
        sent = [r for r in self.requests() if r.get("method") == "turn/start"][0]
        self.assertEqual("thread-test", sent["params"]["threadId"])
        self.assertEqual([], sent["params"]["input"])
        self.assertEqual("herdr_watch", sent["params"]["toolOutput"]["name"])
        self.assertEqual("[herdr-watch test] HEARTBEAT idle",
                         sent["params"]["toolOutput"]["output"])

    def test_unloaded_thread_is_not_resumed_or_given_an_event(self):
        client = self.client("notLoaded")
        with self.assertRaisesRegex(RuntimeError, "loaded"):
            client.deliver("thread-test", "HEARTBEAT")
        self.assertFalse(any(r.get("method") == "turn/start" for r in self.requests()))

    def test_active_thread_can_receive_events(self):
        self.client("active").deliver("thread-test", "FINISHED")
        self.assertEqual("turn/start", self.requests()[-1]["method"])

    def test_rpc_error_is_reported_instead_of_claiming_delivery(self):
        client = self.client("reject")
        with self.assertRaisesRegex(RuntimeError, "toolOutput unavailable"):
            client.request("turn/start", {"threadId": "thread-test", "input": []})

    def test_dead_peer_fails_promptly(self):
        with self.assertRaisesRegex(RuntimeError, "closed|exited"):
            CodexRpc([sys.executable, "-c", "pass"], timeout=1)

    def test_idle_dispatcher_declines_requests_and_discards_notifications(self):
        client = self.client("idle-events")
        client.request("turn/start", {"threadId": "thread-test", "input": []})
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            approvals = [r for r in self.requests() if r.get("id") == "approval-test"]
            if approvals:
                break
            time.sleep(0.01)
        self.assertTrue(approvals, "server request must be answered without another event")
        self.assertIn("cannot answer approval", approvals[0]["error"]["message"])
        self.assertEqual(0, client.replies.qsize())
        self.assertEqual("codex/0.153.4", client.request("initialize", {})["userAgent"])

    def test_idle_disconnect_is_visible_without_another_rpc_request(self):
        client = self.client("disconnect")
        client.request("turn/start", {"threadId": "thread-test", "input": []})
        client.process.wait(timeout=2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                client.ensure_connected()
            except RuntimeError as error:
                self.assertIn("closed", str(error))
                return
            time.sleep(0.01)
        self.fail("idle disconnect was not surfaced")


if __name__ == "__main__":
    unittest.main()
