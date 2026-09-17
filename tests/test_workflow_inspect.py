"""Read-only diagnostics must not become acceptance or spend a review attempt."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_box import git_workflow as gw, workflow, workflow_state
from agent_box.config import Config
from agent_box.errors import AgentBoxError
from agent_box.repo_binding import Binding
from agent_box.tracker.core import Task, TrackerError
from workflow_fixtures import BINDING, repository


class InspectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = repository(self.temporary.name)
        gw.git(self.root, "switch", "-c", "ab/1-task")
        (self.root / "code.py").write_text("value = 1\n")
        gw.git(self.root, "add", "code.py")
        gw.git(self.root, "commit", "-m", "implementation")
        self.task = Task("1", "1", "Task", state="Doing", assignees=["1"],
                         description="## Acceptance\n- [x] implementation")
        self.tasks = {"1": self.task}
        self.adapter = Mock()
        self.adapter.get.side_effect = self.tasks.__getitem__
        self.adapter.identity.return_value = {"id": 1}
        self.config = Config()
        self.config.workflow.enabled = True
        self.config.workflow.merge_mode = "local"
        self.config.workflow.auto_push = False
        self.config.workflow.review.policy = "always"
        for item in (
            patch.object(workflow, "repository_root", return_value=self.root),
            patch.object(workflow, "effective_config", return_value=self.config),
            patch.object(workflow, "effective_config_source", return_value="test configuration"),
            patch.object(workflow, "make_adapter", return_value=self.adapter),
            patch.object(Binding, "load", return_value=BINDING),
            patch.object(workflow, "WORKFLOW_STATE", Path(self.temporary.name) / "state"),
            patch.object(workflow_state, "WORKFLOW_STATE", Path(self.temporary.name) / "state"),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.scope = workflow.scope(self.root, "main", [self.task])
        workflow.save_evidence(self.root, self.task, {
            "scope": self.scope, "validation": {"exit": 0}, "review": {"verdict": "PASS"}})
        workflow.save_review_state(self.root, BINDING, self.task, {"used": 1})

    def dispatch(self, *args):
        return workflow.dispatch(workflow.parser().parse_args(list(args)))

    def snapshot(self):
        return {str(path.relative_to(self.temporary.name)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in Path(self.temporary.name).rglob("*") if path.is_file()}

    def inspect_readonly(self, task="1"):
        before = self.snapshot()
        self.adapter.reset_mock()
        try:
            return self.dispatch("inspect", task)
        finally:
            self.assertEqual(self.snapshot(), before, "inspect changed Git, working files or evidence")
            self.assertTrue(all(call[0] == "get" for call in self.adapter.mock_calls), self.adapter.mock_calls)

    def test_before_begin_reports_missing_branch_and_lifecycle_appropriate_next_step(self):
        """A not-yet-started or cleaned-up task needs diagnosis without a fabricated scope."""
        gw.git(self.root, "switch", "main")
        gw.git(self.root, "branch", "-D", "ab/1-task")
        for state, next_step in (("Ready", "workflow begin 1"), ("Backlog", "ab-plan"),
                                 ("Doing", "--resume"), ("Review", "--resume"),
                                 ("Done", "do not restart")):
            with self.subTest(state=state):
                self.task.state = state
                result = self.inspect_readonly()
                self.assertIsNone(result["branch"])
                self.assertIsNone(result["task_branch"])
                self.assertEqual(result["current_branch"], "main")
                self.assertIsNone(result["scope"])
                self.assertFalse(result["acceptance"]["available"])
                self.assertIn("No branch exists", result["acceptance"]["reasons"][0])
                self.assertEqual(result["review"]["budget"]["used"], 1)
                self.assertIsNone(result["review"]["required"])
                self.assertIn(next_step, result["next_step"])

    def test_dirty_tree_and_other_or_detached_branch_never_expose_saved_acceptance(self):
        """Even valid cached PASS belongs only to the clean checked-out task scope."""
        (self.root / "code.py").write_text("value = 2\n")
        (self.root / ".review-brief.md").write_text("temporary review input")
        result = self.inspect_readonly()
        self.assertIsNone(result["scope"])
        self.assertIsNone(result["validation"])
        self.assertIsNone(result["review"]["independent_verdict"])
        self.assertIn("code.py", result["acceptance"]["reasons"][0])
        self.assertIn(".review-brief.md", result["acceptance"]["reasons"][0])
        gw.git(self.root, "restore", "code.py")
        (self.root / ".review-brief.md").unlink()
        for destination in ("main", "--detach"):
            gw.git(self.root, "switch", destination)
            result = self.inspect_readonly()
            self.assertIsNone(result["scope"])
            self.assertEqual(result["branch"], "ab/1-task")
            self.assertEqual(result["task_branch"], "ab/1-task")
            self.assertEqual(result["current_branch"], "main" if destination == "main" else None)
            self.assertIn("not task branch", result["acceptance"]["reasons"][0])
        gw.git(self.root, "switch", "ab/1-task")
        result = self.inspect_readonly()
        self.assertEqual(result["scope"], self.scope)
        self.assertTrue(result["acceptance"]["available"])
        self.assertEqual(result["validation"], {"exit": 0})
        self.assertEqual(result["review"]["independent_verdict"], "PASS")

    def test_subtask_needs_its_own_branch_and_uses_parent_as_diff_base(self):
        """The parent's checked-out branch cannot supply acceptance for an unstarted child."""
        child = Task("2", "1", "Child", parent="1", state="Ready",
                     description="## Acceptance\n- [ ] child work")
        self.tasks["2"] = child
        self.task.children = ["2"]
        result = self.inspect_readonly("2")
        self.assertEqual(result["branch"], "ab/1-task")
        self.assertIsNone(result["task_branch"])
        self.assertIsNone(result["scope"])
        gw.git(self.root, "switch", "-c", "ab/2-child")
        result = self.inspect_readonly("2")
        self.assertEqual(result["task_branch"], "ab/2-child")
        self.assertEqual(result["scope"], workflow.scope(self.root, "ab/1-task", [child]))

    def test_ambiguous_refs_and_failed_git_tracker_or_budget_reads_remain_errors(self):
        """Diagnostic availability must not hide broken authority as ordinary missing work."""
        gw.git(self.root, "branch", "ab/1-duplicate")
        with self.assertRaisesRegex(AgentBoxError, "Several local branches"):
            self.inspect_readonly()
        gw.git(self.root, "branch", "-D", "ab/1-duplicate")
        (self.root / "dirty").write_text("untracked")
        with patch.object(self.adapter, "get", side_effect=TrackerError("unavailable")), \
             self.assertRaisesRegex(TrackerError, "unavailable"):
            self.inspect_readonly()
        status = subprocess.CompletedProcess([], 128, "", "index unavailable")
        original = gw._run

        def fail_status(args, **kwargs):
            return status if "status" in args else original(args, **kwargs)

        with patch.object(gw, "_run", side_effect=fail_status), \
             self.assertRaisesRegex(AgentBoxError, "Cannot inspect working tree: index unavailable"):
            self.inspect_readonly()
        def fail_branch(args, **kwargs):
            return status if "symbolic-ref" in args and "HEAD" in args else original(args, **kwargs)

        with patch.object(gw, "_run", side_effect=fail_branch), \
             self.assertRaisesRegex(AgentBoxError, "Cannot read current Git branch"):
            self.inspect_readonly()
        workflow.review_budget_path(self.root, BINDING, self.task).write_text("broken")
        with self.assertRaisesRegex(AgentBoxError, "Cannot read review budget"):
            self.inspect_readonly()

    def test_in_tree_brief_blocks_acceptance_without_spending_review_budget(self):
        """The early dirty refusal leaves evidence intact; moving the same brief enables prepare."""
        # Completion owns its lock before checking cleanliness; lock-file creation
        # does not spend review budget or change acceptance evidence.
        with workflow.lock(self.root, "review-budget"), workflow.lock(self.root, "work"):
            pass
        brief = self.root / ".review-brief.md"
        brief.write_text("Changes/reasons: implementation. Risks: none known. Validation: tests only.")
        for args in (("prepare", "1", "--role", "reviewer", "--primary", "codex", "--brief", str(brief)),
                     ("review", "1", "--primary", "codex", "--brief", str(brief)),
                     ("approve", "1", "--scope", self.scope, "--reason", "User accepts"),
                     ("validate", "1", "--", "true"), ("finish", "1"), ("land", "1", "merge")):
            with self.subTest(command=args[0]):
                before = self.snapshot()
                with self.assertRaisesRegex(AgentBoxError, "requires a clean working tree") as error:
                    self.dispatch(*args)
                self.assertIn(".review-brief.md", str(error.exception))
                self.assertNotIn("changing workflow branches", str(error.exception))
                self.assertEqual(self.snapshot(), before)
        external = Path(self.temporary.name) / "review-brief.md"
        brief.rename(external)
        prepared = self.dispatch("prepare", "1", "--role", "reviewer", "--primary", "codex", "--brief", str(external))
        self.assertEqual(prepared["scope"], self.scope)
        self.assertEqual(prepared["review_budget"]["used"], 2)

    def test_dirty_filenames_are_quoted_bounded_and_keep_status_columns(self):
        """Unusual names remain recognizable without allowing an unbounded diagnostic."""
        (self.root / "code.py").write_text("modified")
        for name in (' leading name ', 'quote"name', 'line\nbreak', 'имя\u2028файла'):
            (self.root / name).write_text("untracked")
        changes = gw.worktree_changes(self.root)
        self.assertIn(" M code.py", changes)
        self.assertTrue(any('line\\nbreak' in item for item in changes))
        self.assertTrue(any(' leading name ' in item for item in changes))
        self.assertTrue(any('имя\u2028файла' in item for item in changes))
        for number in range(20):
            (self.root / f"extra-{number:02}").write_text("untracked")
        reason = self.inspect_readonly()["acceptance"]["reasons"][0]
        self.assertIn("15 more", reason)
        self.assertLess(len(reason), 2400)
        self.assertNotIn("extra-19", reason)
