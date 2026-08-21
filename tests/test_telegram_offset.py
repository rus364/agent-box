import tempfile
import unittest
from pathlib import Path

from agent_box.telegram.offset import OffsetStore


class OffsetStoreTests(unittest.TestCase):
    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OffsetStore(Path(tmp) / "offset.json")
            self.assertIsNone(store.load())

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "offset.json"
            store = OffsetStore(path)
            store.save(1234)
            self.assertEqual(store.load(), 1234)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OffsetStore(Path(tmp) / "offset.json")
            store.save(1)
            store.save(2)
            self.assertEqual(store.load(), 2)

    def test_corrupt_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "offset.json"
            path.write_text("not json")
            store = OffsetStore(path)
            self.assertIsNone(store.load())


if __name__ == "__main__":
    unittest.main()
