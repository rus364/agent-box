"""Launch allocation survives processes without turning counters into task state."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_box import atomicio, workflow_state
from agent_box.errors import AgentBoxError


class BalanceStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "balance.json"
        patched = patch.object(workflow_state, "WORKFLOW_STATE", self.root)
        patched.start()
        self.addCleanup(patched.stop)

    def turn(self):
        return workflow_state.balance_vendor("worker", None, {"claude": 3, "codex": 1})

    def test_unknown_and_malformed_state_is_refused_without_overwriting_any_key(self):
        """A launch must not repair or discard unreadable counters, including another role's record."""
        cases = ["not json", "[]", '{"format_version": 999, "keys": {}}',
                 '{"format_version": "1", "keys": {}}', '{"format_version": true, "keys": {}}',
                 '{"format_version": 0, "keys": {}}', '{"format_version": 1}',
                 '{"format_version": 1, "keys": []}',
                 '{"format_version": 1, "keys": {}, "unknown": {}}',
                 json.dumps({"format_version": 1, "keys": {"scout:1": {}}}),
                 json.dumps({"format_version": 1, "keys": {"reviewer:2": {
                     "weights": {"codex": 1, "claude": 3}, "credits": {"codex": 0}}}}),
                 json.dumps({"format_version": 1, "keys": {"reviewer:2": {
                     "weights": {"codex": 1, "claude": 1.5}, "credits": {"codex": 0, "claude": 0}}}}),
                 json.dumps({"format_version": 1, "keys": {"reviewer:2": {
                     "weights": {"codex": 1, "claude": 3}, "credits": {"codex": float("nan"), "claude": 0}}}})]
        for content in cases:
            with self.subTest(content=content):
                self.path.write_text(content)
                with self.assertRaises(AgentBoxError) as caught:
                    self.turn()
                self.assertIn(str(self.path), str(caught.exception))
                self.assertEqual(self.path.read_text(), content)

    def test_unreadable_file_and_symlink_are_not_replaced(self):
        """Only a missing file starts empty; read failures cannot silently reset an allocation."""
        self.turn()
        original = self.path.read_bytes()
        with patch.object(workflow_state, "read_json", side_effect=PermissionError("unreadable")), \
             self.assertRaisesRegex(AgentBoxError, "balance.json"):
            self.turn()
        self.assertEqual(self.path.read_bytes(), original)
        self.path.unlink()
        target = self.root / "missing.json"
        self.path.symlink_to(target)
        with self.assertRaisesRegex(AgentBoxError, "balance.json"):
            self.turn()
        self.assertTrue(self.path.is_symlink())
        self.assertFalse(target.exists())

    def test_counters_are_private_versioned_and_preserved_on_atomic_write_failure(self):
        """Failed replacement leaves the prior turn intact and no partially written allocation."""
        self.turn()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.path.read_text())["format_version"], 1)
        before = self.path.read_bytes()
        with patch.object(atomicio.os, "replace", side_effect=OSError("disk failed")), \
             self.assertRaisesRegex(AgentBoxError, "balance.json"):
            self.turn()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".agent-box.tmp.*")), [])

    def test_global_lock_identity_survives_different_mount_paths(self):
        """Host and container paths may differ while their state directory and lock inode are shared."""
        with patch.object(Path, "resolve", return_value=Path("/host/state")):
            with workflow_state.lock(None, "balance"):
                with patch.object(Path, "resolve", return_value=Path("/container/state")), \
                     self.assertRaisesRegex(AgentBoxError, "Another local balance operation"):
                    with workflow_state.lock(None, "balance"):
                        self.fail("A second path bypassed the global lock")
        self.assertEqual(self.turn(), "claude")

    def process(self, turns):
        source = """
import sys
from agent_box.config import BalanceConfig, WorkflowConfig, model_entry
from agent_box.routing import select
c = WorkflowConfig(balance=BalanceConfig('weighted', {'claude': 3, 'codex': 1}))
c.worker.models = [model_entry({'provider': p, 'model': p}) for p in ('claude', 'codex')]
for _ in range(int(sys.argv[1])):
    print(select(c, 'worker', None)['provider'], flush=True)
"""
        return subprocess.Popen([sys.executable, "-c", source, str(turns)],
                                env={**os.environ, "AGENT_BOX_WORKFLOW_STATE": str(self.root)},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def output(self, process):
        out, err = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, err)
        return out.splitlines()

    def test_fresh_processes_resume_the_sequence_instead_of_restarting_it(self):
        """Each process reads the previous launch's credits; memory-local balancing would starve Codex."""
        self.assertEqual([self.output(self.process(1))[0] for _ in range(8)],
                         ["claude", "claude", "codex", "claude"] * 2)

    def test_concurrent_processes_serialize_global_allocation_without_lost_turns(self):
        """A per-repository or nonblocking lock would lose turns or fail simultaneous managed launches."""
        processes = [self.process(20) for _ in range(4)]
        launches = [vendor for process in processes for vendor in self.output(process)]
        self.assertEqual((launches.count("claude"), launches.count("codex")), (60, 20))
        self.assertEqual(json.loads(self.path.read_text())["keys"]["worker:none"]["credits"],
                         {"claude": 0, "codex": 0})
