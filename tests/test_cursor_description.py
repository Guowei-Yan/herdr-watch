import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from hw_subagents import CursorLayout


class CursorDescriptionTests(unittest.TestCase):
    def test_spawn_description_strips_timestamp_and_query_markup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.jsonl"
            record = {"message": {"role": "user", "content":
                      "<timestamp>2026-09-07</timestamp><user_query>Review the fix</user_query>"}}
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            layout = CursorLayout(directory, set())
            self.assertEqual("Review the fix", layout.describe("test", str(path)))


if __name__ == "__main__":
    unittest.main()
