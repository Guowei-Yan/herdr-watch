import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from hw_background import Monitor, run_monitor


class Receiver:
    def __init__(self, fail=False, disconnect=False):
        self.events = []
        self.fail = fail
        self.disconnect = disconnect

    def ensure_connected(self):
        if self.disconnect and self.events:
            raise RuntimeError("server disconnected")

    def deliver(self, thread, event):
        if self.fail:
            raise RuntimeError("connection lost")
        self.events.append((thread, event))
        return {"turn": {"id": "accepted", "status": "inProgress"}}


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def spec(self, mode="finish"):
        return {"id": "a" * 32, "thread": "thread-test", "command": [sys.executable,
                "-B", str(Path(__file__).with_name("fake_watcher.py")), mode]}

    def state(self):
        return json.loads((self.folder / "state.json").read_text())

    def test_exit_flushes_the_final_report_with_its_event(self):
        receiver = Receiver()
        run_monitor(self.spec(), self.folder, receiver)
        reports = [event for thread, event in receiver.events if "FINISHED" in event]
        self.assertEqual(1, len(reports))
        self.assertIn("Actual final report", reports[0])
        self.assertEqual("ended", self.state()["status"])
        self.assertTrue(all(thread == "thread-test" for thread, _ in receiver.events))

    def test_failed_delivery_preserves_the_event_and_stops_the_worker(self):
        run_monitor(self.spec(), self.folder, Receiver(fail=True))
        self.assertEqual("failed", self.state()["status"])
        self.assertIn("connection lost", self.state()["error"])
        self.assertIn("Actual final report",
                      (self.folder / "undelivered.txt").read_text())

    def test_stop_request_ends_a_running_watch_without_sending_fake_events(self):
        receiver = Receiver()
        thread = threading.Thread(target=run_monitor,
                                  args=(self.spec("wait"), self.folder, receiver))
        thread.start()
        deadline = time.monotonic() + 5
        try:
            while time.monotonic() < deadline:
                if (self.folder / "state.json").exists() and self.state()["status"] == "running":
                    break
                time.sleep(0.02)
            else:
                self.fail("watch never armed")
        finally:
            (self.folder / "stop").touch()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual("stopped", self.state()["status"])
        self.assertFalse(any("FINISHED" in event for _, event in receiver.events))

    def test_log_failure_is_reported_instead_of_leaving_a_live_worker(self):
        (self.folder / "events.log").mkdir()
        thread = threading.Thread(target=run_monitor,
                                  args=(self.spec("wait"), self.folder, Receiver()))
        thread.start()
        thread.join(1)
        try:
            self.assertFalse(thread.is_alive(), "log failure must stop the monitor")
            self.assertEqual("failed", self.state()["status"])
            self.assertIn("output reader failed", self.state()["error"])
        finally:
            (self.folder / "stop").touch()
            thread.join(5)

    def test_idle_connection_failure_stops_a_previously_armed_watch(self):
        receiver = Receiver(disconnect=True)
        thread = threading.Thread(target=run_monitor,
                                  args=(self.spec("wait"), self.folder, receiver))
        thread.start()
        thread.join(2)
        try:
            self.assertFalse(thread.is_alive(), "closed connection must stop the monitor")
            self.assertEqual("failed", self.state()["status"])
            self.assertIn("server disconnected", self.state()["error"])
            self.assertEqual(1, len(receiver.events))
        finally:
            (self.folder / "stop").touch()
            thread.join(5)

    def test_disconnect_during_batching_preserves_pending_without_retry(self):
        receiver = Receiver()
        monitor = Monitor(self.spec(), self.folder, receiver)
        monitor.add_line("[herdr-watch test] COMMIT pending\n")
        with mock.patch.object(receiver, "ensure_connected",
                               side_effect=RuntimeError("server disconnected")):
            with self.assertRaisesRegex(RuntimeError, "server disconnected"):
                monitor._tick()
        self.assertIn("COMMIT pending", (self.folder / "undelivered.txt").read_text())
        self.assertEqual([], receiver.events)


if __name__ == "__main__":
    unittest.main()
