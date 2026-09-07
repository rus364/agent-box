"""Local landing proves integration from Git without GitHub or stored merge state."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_box import git_workflow as gw
from agent_box import workflow
from agent_box import workflow_state
from agent_box.config import Config
from agent_box.errors import AgentBoxError
from agent_box.repo_binding import Binding
from agent_box.tracker.core import Task
from test_workflow_handoff import BINDING, repository


class LocalLandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = repository(self.temporary.name)
        state_patch = patch.object(workflow_state, "WORKFLOW_STATE",
                                   Path(self.temporary.name) / "state")
        state_patch.start()
        self.addCleanup(state_patch.stop)
        gw.git(self.root, "switch", "-c", "ab/7-parent")
        (self.root / "change.py").write_text("value = 1\n")
        gw.git(self.root, "add", "change.py")
        gw.git(self.root, "commit", "-m", "task work")
        self.parent = Task("7", "1", "Parent", state="Review", assignees=["1"],
                           description="- [x] accepted")
        self.adapter = Mock()
        self.adapter.identity.return_value = {"id": 1}
        self.adapter.get.side_effect = lambda ident: {"7": self.parent}[ident]
        self.config = Config()
        self.config.workflow.enabled = True
        self.config.workflow.merge_mode = "local"
        self.config.workflow.review.policy = "off"
        self.info = {"branch": "ab/7-parent", "base": "main"}
        self.saved = {"scope": workflow.scope(self.root, "main", [self.parent]),
                      "validation": {"command": ["true"], "exit": 0}}

    def operate(self, action, *, method=None, saved=None):
        from agent_box import local_workflow
        args = SimpleNamespace(command="land", action=action, method=method)
        return local_workflow.operate(args, self.root, self.config, self.adapter,
                                      self.parent, self.parent, self.info,
                                      self.saved if saved is None else saved)

    def test_merge_fast_forwards_exact_task_head_without_push_or_github(self):
        task_head = gw.git(self.root, "rev-parse", "ab/7-parent")
        with patch("agent_box.github_workflow.gh") as github, \
             patch.object(gw, "push_parent") as push:
            result = self.operate("merge")
        self.assertEqual(gw.current_branch(self.root), "main")
        self.assertEqual(gw.head_sha(self.root), task_head)
        self.assertEqual(result["merge"]["head"], task_head)
        self.adapter.set_state.assert_called_once_with(self.parent, "Done")
        github.assert_not_called()
        push.assert_not_called()

    def test_merge_refuses_dirty_or_diverged_work_before_switching(self):
        (self.root / "dirty").write_text("dirty")
        with self.assertRaisesRegex(AgentBoxError, "Commit or preserve"):
            self.operate("merge")
        self.assertEqual(gw.current_branch(self.root), "ab/7-parent")
        (self.root / "dirty").unlink()
        gw.git(self.root, "switch", "main")
        gw.git(self.root, "commit", "--allow-empty", "-m", "base moved")
        gw.git(self.root, "switch", "ab/7-parent")
        with self.assertRaisesRegex(AgentBoxError, "diverged"):
            self.operate("merge")
        self.assertEqual(gw.current_branch(self.root), "ab/7-parent")

    def test_merge_rereads_ownership_dependencies_children_and_checklist(self):
        child = Task("8", "1", "Child", state="Doing", parent="7",
                     description="- [x] child")
        dependency = Task("9", "1", "Dependency", state="Doing")
        self.parent.children = ["8"]
        self.parent.blockers = ["9"]
        self.parent.assignees = ["2"]
        self.parent.description = "- [ ] accepted"
        self.adapter.get.side_effect = lambda ident: {
            "7": self.parent, "8": child, "9": dependency}[ident]
        with self.assertRaisesRegex(AgentBoxError, "assigned.*subtasks.*checklist.*Dependency"):
            self.operate("merge")
        self.adapter.set_state.assert_not_called()

    def test_merge_requires_local_base_validation_and_configured_review(self):
        self.config.workflow.review.policy = "always"
        with self.assertRaisesRegex(AgentBoxError, "PASS review"):
            self.operate("merge")
        with self.assertRaisesRegex(AgentBoxError, "validation"):
            self.operate("merge", saved={})
        self.config.workflow.review.policy = "off"
        gw.git(self.root, "branch", "-D", "main")
        with self.assertRaisesRegex(AgentBoxError, "Local branch main"):
            self.operate("merge")

    def test_reconcile_from_base_uses_current_task_tip_and_rechecks_constraints(self):
        task_head = gw.git(self.root, "rev-parse", "ab/7-parent")
        gw.git(self.root, "switch", "main")
        gw.git(self.root, "merge", "--ff-only", task_head)
        result = self.operate("reconcile", saved={})
        self.assertEqual(result["merge"]["head"], task_head)
        self.adapter.set_state.assert_called_once_with(self.parent, "Done")

        self.adapter.set_state.reset_mock()
        self.parent.state = "Review"
        gw.git(self.root, "switch", "ab/7-parent")
        gw.git(self.root, "commit", "--allow-empty", "-m", "new work")
        gw.git(self.root, "switch", "main")
        with self.assertRaisesRegex(AgentBoxError, "not integrated"):
            self.operate("reconcile", saved={})
        self.adapter.set_state.assert_not_called()

    def test_tracker_failure_after_merge_is_recovered_without_merging_again(self):
        task_head = gw.git(self.root, "rev-parse", "ab/7-parent")
        self.adapter.set_state.side_effect = AgentBoxError("tracker unavailable")
        with self.assertRaisesRegex(AgentBoxError, "tracker unavailable"):
            self.operate("merge")
        self.assertEqual((gw.current_branch(self.root), gw.head_sha(self.root)),
                         ("main", task_head))
        self.adapter.set_state.side_effect = None
        result = self.operate("reconcile", saved={})
        self.assertEqual(result["merge"]["head"], task_head)

    def test_local_mode_rejects_pr_cloud_review_and_merge_methods_without_github(self):
        from agent_box import local_workflow
        with patch("agent_box.github_workflow.gh") as github:
            with self.assertRaisesRegex(AgentBoxError, "local merge mode"):
                local_workflow.operate(SimpleNamespace(command="pr"), self.root, self.config,
                                       self.adapter, self.parent, self.parent, self.info, self.saved)
            self.config.workflow.review.github_provider = "codex"
            with self.assertRaisesRegex(AgentBoxError, "Cloud review"):
                self.operate("request-review")
            with self.assertRaisesRegex(AgentBoxError, "--method"):
                self.operate("merge", method="squash")
        github.assert_not_called()


class MergeModeRoutingTests(unittest.TestCase):
    def test_github_remains_default_and_chooses_squash_when_method_is_omitted(self):
        self.assertEqual(Config().workflow.merge_mode, "github")
        args = workflow.parser().parse_args(["land", "7", "merge"])
        self.assertIsNone(args.method)

    def test_dispatch_invalidates_validation_when_task_head_moves(self):
        with tempfile.TemporaryDirectory() as directory:
            root = repository(directory)
            gw.git(root, "switch", "-c", "ab/7-parent")
            task = Task("7", "1", "Parent", state="Review", assignees=["1"],
                        description="- [x] accepted")
            adapter = Mock()
            adapter.identity.return_value = {"id": 1}
            adapter.get.return_value = task
            config = Config()
            config.workflow.enabled = True
            config.workflow.merge_mode = "local"
            config.workflow.review.policy = "off"
            state = Path(directory) / "state"
            with patch.object(workflow, "WORKFLOW_STATE", state), \
                 patch.object(workflow_state, "WORKFLOW_STATE", state):
                accepted = {"scope": workflow.scope(root, "main", [task]),
                            "validation": {"command": ["true"], "exit": 0}}
                workflow.save_evidence(root, task, accepted)
                gw.git(root, "commit", "--allow-empty", "-m", "new work")
                with patch.object(workflow, "effective_config", return_value=config), \
                     patch.object(workflow, "repository_root", return_value=root), \
                     patch.object(Binding, "load", return_value=BINDING), \
                     patch.object(workflow, "make_adapter", return_value=adapter):
                    with self.assertRaisesRegex(AgentBoxError, "validation"):
                        workflow.dispatch(workflow.parser().parse_args(["land", "7", "merge"]))
            adapter.set_state.assert_not_called()


if __name__ == "__main__":
    unittest.main()
