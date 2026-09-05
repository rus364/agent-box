"""Tracker progress and readiness cannot silently erase work or release unavailable code."""
import importlib
import tempfile
import unittest
from pathlib import Path


class TrackerWorkflowTests(unittest.TestCase):
    def core(self):
        return importlib.import_module("agent_box.tracker.core")

    def test_cross_parent_blocker_waits_for_its_parent_merge(self):
        c = self.core()
        tasks = {"1": c.Task("1", "p", "Parent A", state="Doing"),
                 "2": c.Task("2", "p", "API", state="Done", parent="1"),
                 "3": c.Task("3", "p", "UI", state="Ready", parent="1", blockers=["2"]),
                 "4": c.Task("4", "p", "Other parent", state="Doing"),
                 "5": c.Task("5", "p", "External client", state="Ready", parent="4", blockers=["2"])}
        self.assertEqual(c.blocking_reasons(tasks["3"], tasks.__getitem__), [])
        self.assertTrue(c.blocking_reasons(tasks["5"], tasks.__getitem__))
        tasks["1"].state = "Done"
        self.assertEqual(c.blocking_reasons(tasks["5"], tasks.__getitem__), [])

    def test_unknown_dependency_and_cycles_are_not_ready(self):
        c = self.core()
        task = c.Task("1", "p", "Work", state="Ready", blockers=["2"])
        self.assertTrue(c.blocking_reasons(task, {}.__getitem__))
        task.parent = "1"
        with self.assertRaises(c.TrackerError):
            c.root_task(task, {"1": task}.__getitem__)

    def test_checkbox_edits_require_the_current_text_and_ignore_code_fences(self):
        c = self.core()
        body = "## Steps\n- [ ] API\n```md\n- [ ] example\n```\n- [x] UI\n"
        changed = c.set_checkbox(body, 1, True, "API")
        self.assertEqual(changed, body.replace("- [ ] API", "- [x] API"))
        self.assertEqual(len(c.checkboxes(body)), 2)
        with self.assertRaises(c.TrackerError):
            c.set_checkbox(body, 1, False, "Old API requirement")

    def test_setup_is_idempotent_and_refuses_edited_blocks_without_partial_writes(self):
        b = importlib.import_module("agent_box.repo_binding")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_bytes(b"User rules\r\n")
            binding = b.Binding("vikunja", "https://tasks.example", "12", "7", {
                "Backlog": "1", "Ready": "2", "Doing": "3", "Review": "4", "Done": "5"})
            b.write_setup(root, binding)
            before = (root / "AGENTS.md").read_bytes()
            b.write_setup(root, binding)
            self.assertEqual((root / "AGENTS.md").read_bytes(), before)
            self.assertTrue(before.endswith(b"User rules\r\n"))
            (root / "AGENTS.md").write_bytes(before.replace(b"Read the tracked task", b"Changed rule"))
            previous = (root / ".agent-box.json").read_bytes()
            binding.project = "99"
            with self.assertRaises(Exception):
                b.write_setup(root, binding)
            self.assertEqual((root / ".agent-box.json").read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
