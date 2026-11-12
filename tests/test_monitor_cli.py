import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from codex_monitor import main, monitor_folder, watcher_command


class MonitorCliTests(unittest.TestCase):
    def test_only_watcher_subcommands_can_be_backgrounded(self):
        for command in (["sessions"], ["python3", "unrelated.py"], []):
            with self.subTest(command=command), self.assertRaises(ValueError):
                watcher_command(command)
        self.assertEqual("watch-git", watcher_command(["watch-git", "--repo", "/tmp"])[3])

    def test_monitor_id_cannot_escape_the_state_directory(self):
        with self.assertRaises(ValueError):
            monitor_folder(Path("/tmp/state"), "../../somewhere")

    def test_failed_worker_launch_is_recorded_as_failed(self):
        with tempfile.TemporaryDirectory() as root:
            arguments = ["start", "--thread", "thread-test", "--state-dir", root,
                         "--", "watch-git", "--repo", root]
            with patch("codex_monitor.check_connection"), patch(
                    "codex_monitor.subprocess.Popen", side_effect=OSError("cannot launch")):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(2, main(arguments))
            state_file = next(Path(root).glob("*/state.json"))
            state = json.loads(state_file.read_text())
            self.assertEqual("failed", state["status"])
            self.assertIn("cannot launch", state["error"])

    def test_stop_requests_only_the_selected_monitor_without_killing_a_pid(self):
        with tempfile.TemporaryDirectory() as root:
            first, second = "a" * 32, "b" * 32
            for ident in (first, second):
                folder = Path(root) / ident
                folder.mkdir()
                (folder / "state.json").write_text(json.dumps({
                    "id": ident, "status": "running", "updated_at": time.time()}))
            with contextlib.redirect_stdout(io.StringIO()):
                result = main(["stop", "--state-dir", root, "--id", first])
            self.assertEqual(0, result)
            self.assertTrue((Path(root) / first / "stop").exists())
            self.assertFalse((Path(root) / second / "stop").exists())


if __name__ == "__main__":
    unittest.main()
