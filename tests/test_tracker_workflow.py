"""Tracker progress and readiness cannot silently erase work or release unavailable code."""
import importlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch


class TrackerWorkflowTests(unittest.TestCase):
    def core(self):
        return importlib.import_module("agent_box.tracker.core")

    def test_claim_cli_keeps_task_json_for_new_work_and_resumes(self):
        """Internal claim provenance must not change the public task JSON response."""
        from agent_box import workflow_state
        from agent_box.tracker import cli

        for state in ("Ready", "In progress", "In review"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                task = self.core().Task("1", "p", "Work", state=state, assignees=["7"])
                adapter = Mock()
                adapter.get.return_value = task
                adapter.identity.return_value = {"id": 7}
                adapter.update.side_effect = lambda current, fields: replace(current, **fields)
                adapter.set_state.side_effect = lambda current, state: replace(current, state=state)
                output = io.StringIO()
                with patch.object(cli, "context", return_value=(adapter, None, Path(tmp))), \
                     patch.object(workflow_state, "WORKFLOW_STATE", Path(tmp) / "state"), \
                     redirect_stdout(output):
                    self.assertEqual(cli.main(["claim", "1", "--resume"]), 0)
                self.assertEqual(json.loads(output.getvalue()), replace(task, state="In progress").to_dict())

    def test_untiered_readiness_claim_resume_and_completion_preserve_labels(self):
        """Ordinary tracker lifecycle operations must never invent a routing tier."""
        c = self.core()
        task = c.Task("1", "p", "Work", state="Ready", labels=["product"],
                      description="- [ ] acceptance")
        adapter = Mock()
        adapter.get.side_effect = lambda _: task
        adapter.identity.return_value = {"id": 7}

        def update(current, fields):
            nonlocal task
            task = replace(current, **fields)
            return task

        adapter.update.side_effect = update
        adapter.set_state.side_effect = lambda current, state: update(current, {"state": state})
        self.assertEqual(c.ready_tasks([task], adapter.get, "7"), [task])
        claimed, started = c.claim(adapter, "1")
        self.assertTrue(started)
        self.assertEqual((claimed.state, claimed.assignees), ("In progress", ["7"]))
        self.assertIsNone(claimed.tier)
        self.assertEqual(claimed.labels, ["product"])
        self.assertIn("Acceptance or validation checklist remains incomplete",
                      c.completion_reasons(task, [task], "7"))
        adapter.update(task, {"description": c.set_checkboxes(task.description, [1], True)})
        for state in ("In progress", "In review"):
            current = adapter.set_state(task, state)
            self.assertEqual(c.completion_reasons(current, [current], "7"), [])
            resumed, started = c.claim(adapter, "1", resume=True)
            self.assertFalse(started)
            self.assertEqual(resumed.state, "In progress")
            self.assertIsNone(resumed.tier)
            self.assertEqual(resumed.labels, ["product"])
        finished = adapter.set_state(task, "Done")
        self.assertEqual(c.completion_reasons(finished, [finished], "7", states=("Done",)), [])
        self.assertIsNone(finished.tier)
        self.assertEqual(finished.labels, ["product"])
        self.assertEqual([entry.args[1] for entry in adapter.update.call_args_list],
                         [{"assignees": ["7"]}, {"description": "- [x] acceptance"}])

    def test_cross_parent_blocker_waits_for_its_parent_merge(self):
        c = self.core()
        tasks = {"1": c.Task("1", "p", "Parent A", state="In progress"),
                 "2": c.Task("2", "p", "API", state="Done", parent="1"),
                 "3": c.Task("3", "p", "UI", state="Ready", parent="1", blockers=["2"]),
                 "4": c.Task("4", "p", "Other parent", state="In progress"),
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

    def test_checkbox_edits_change_only_named_items_and_ignore_code_fences(self):
        """One write records several decisions; an example inside a fence is not an item."""
        c = self.core()
        body = "## Steps\n- [ ] API\n```md\n- [ ] example\n```\n- [x] UI\n"
        self.assertEqual(len(c.checkboxes(body)), 2)
        self.assertEqual(c.set_checkboxes(body, [1], True), body.replace("- [ ] API", "- [x] API"))
        both = c.set_checkboxes(body, [2, 1], True)
        self.assertEqual(both, body.replace("- [ ] API", "- [x] API"))
        self.assertEqual(c.set_checkboxes(both, [1, 2], False),
                         body.replace("- [ ] API", "- [ ] API").replace("- [x] UI", "- [ ] UI"))
        with self.assertRaisesRegex(c.TrackerError, "no item 3"):
            c.set_checkboxes(body, [1, 3], True)

    def test_setup_is_idempotent_and_refuses_edited_blocks_without_partial_writes(self):
        b = importlib.import_module("agent_box.repo_binding")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_bytes(b"User rules\r\n")
            binding = b.Binding("vikunja", "https://tasks.example", "12")
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
