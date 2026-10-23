"""Interrupted atomic writes should not grow forever or endanger another writer."""
import fcntl
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from agent_box import atomicio


class AtomicWriteRetentionTests(unittest.TestCase):
    def test_expired_orphans_are_removed_but_live_writers_and_unowned_files_survive(self):
        """Kernel locks protect a stalled writer even after its temporary file ages out."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orphan = root / ".agent-box.tmp.abcd1234"
            active = root / ".agent-box.tmp.efgh5678"
            legacy = root / ".agent-box.legacy"
            target = root / "settings.json"
            for path in (orphan, active, legacy):
                path.write_text("keep until safe")
                os.utime(path, (0, 0))
            victim = root / "valuable"
            victim.write_text("user data")
            link = root / ".agent-box.tmp.ijkl1234"
            link.symlink_to(victim)
            fd = os.open(active, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                atomicio.atomic_write_json(target, {"choice": True}, 0o600)
                self.assertFalse(orphan.exists())
                self.assertTrue(active.exists())
            finally:
                os.close(fd)
            atomicio.atomic_write_json(target, {"choice": False})
            self.assertFalse(active.exists())
            self.assertTrue(legacy.exists())
            self.assertTrue(link.is_symlink())
            self.assertEqual(victim.read_text(), "user data")

    def test_failed_write_keeps_original_and_releases_its_temporary_file(self):
        """A failed fsync must neither replace settings nor leave a cleanup burden."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "settings"
            target.write_text("original")
            with mock.patch.object(atomicio.os, "fsync", side_effect=OSError("disk full")), self.assertRaises(OSError):
                atomicio.atomic_write_text(target, "replacement")
            self.assertEqual(target.read_text(), "original")
            self.assertEqual(list(target.parent.glob(".agent-box.tmp.*")), [])

    def test_identical_usage_response_still_refreshes_the_cache_observation_time(self):
        """Skipping equal writes would repeatedly refresh an apparently expired usage cache."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "usage.json"
            atomicio.atomic_write_json(target, {"remaining": 1})
            os.utime(target, (0, 0))
            atomicio.atomic_write_json(target, {"remaining": 1})
            self.assertGreater(target.stat().st_mtime, time.time() - 10)
