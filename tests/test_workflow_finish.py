"""Completion retries retain acceptance of the work that actually advanced Git."""
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from agent_box import git_workflow as gw
from agent_box import workflow
from agent_box.errors import AgentBoxError
from agent_box.tracker.core import Task
import test_local_workflow
from test_workflow_handoff import BINDING


class ChildFinishTests(unittest.TestCase):
    dispatch = test_local_workflow.LocalLandTests.dispatch

    def setUp(self):
        test_local_workflow.LocalLandTests.setUp(self)
        self.parent.state = "Doing"
        self.parent.children = ["8"]
        self.child = Task("8", "1", "Child", state="Doing", assignees=["1"],
                          parent="7", description="- [x] accepted")
        self.adapter.get.side_effect = lambda ident: {"7": self.parent, "8": self.child}[ident]
        gw.git(self.root, "switch", "-c", "ab/8-child")
        self.commit("child.py")
        self.head = gw.head_sha(self.root)
        self.dispatch(["validate", "8", "--", "true"])
        self.adapter.reset_mock()

    def commit(self, name):
        (self.root / name).write_text("value = 1\n")
        gw.git(self.root, "add", name)
        gw.git(self.root, "commit", "-m", name)

    def interrupted_finish(self):
        self.adapter.set_state.side_effect = AgentBoxError("tracker unavailable")
        with self.assertRaisesRegex(AgentBoxError, "tracker unavailable"):
            self.dispatch(["finish", "8"])
        self.assertEqual(gw.local_branch_head(self.root, "ab/7-parent"), self.head)
        self.adapter.set_state.side_effect = None
        self.adapter.reset_mock()

    def test_tracker_outage_retry_finishes_original_work_without_merge_or_validation(self):
        """The advanced parent must not turn the accepted child into an empty diff."""
        self.interrupted_finish()
        with patch.object(gw, "git", wraps=gw.git) as git, \
             patch.object(workflow, "scope", wraps=workflow.scope) as scope:
            result = self.dispatch(["finish", "8"])
        self.assertEqual(result["state"], "Done")
        self.adapter.set_state.assert_called_once_with(self.child, "Done")
        self.assertFalse(any(call.args[1] == "merge" for call in git.call_args_list))
        scope.assert_not_called()

    def test_retry_rejects_changed_requirements_and_changed_integrated_head(self):
        """Ancestry of a different task revision cannot reuse the old acceptance."""
        self.interrupted_finish()
        self.child.description += "\nNew requirement"
        with self.assertRaisesRegex(AgentBoxError, "requirements.*changed"):
            self.dispatch(["finish", "8"])
        self.child.description = "- [x] accepted"
        gw.git(self.root, "switch", "ab/8-child")
        self.commit("later.py")
        gw.git(self.root, "switch", "ab/7-parent")
        gw.git(self.root, "merge", "--ff-only", "ab/8-child")
        with self.assertRaisesRegex(AgentBoxError, "head.*changed"):
            self.dispatch(["finish", "8"])
        self.adapter.set_state.assert_not_called()

    def test_missing_snapshot_cannot_accept_an_already_integrated_empty_diff(self):
        """Even a new validation of the empty diff cannot recover original acceptance."""
        self.interrupted_finish()
        self.dispatch(["validate", "8", "--", "true"])
        with self.assertRaisesRegex(AgentBoxError, "acceptance.*missing"):
            self.dispatch(["finish", "8"])
        self.adapter.set_state.assert_not_called()

    def test_retry_applies_current_review_policy_to_original_changed_paths(self):
        """A stricter policy must see the original files after the parent advances."""
        self.interrupted_finish()
        self.config.workflow.review.policy = "auto"
        self.config.workflow.review.sensitive_paths = ["child.py"]
        with self.assertRaisesRegex(AgentBoxError, "sensitive path child.py"):
            self.dispatch(["finish", "8"])
        self.adapter.set_state.assert_not_called()

    def test_retry_rechecks_current_checklist_and_assignment(self):
        """Normalized requirements never substitute for current completion conditions."""
        self.interrupted_finish()
        self.child.description = "- [ ] accepted"
        with self.assertRaisesRegex(AgentBoxError, "checklist"):
            self.dispatch(["finish", "8"])
        self.child.description = "- [x] accepted"
        self.child.assignees = ["2"]
        with self.assertRaisesRegex(AgentBoxError, "assigned"):
            self.dispatch(["finish", "8"])
        self.adapter.set_state.assert_not_called()

    def test_enabled_publication_failure_keeps_done_unwritten_and_retryable(self):
        """Publishing the accepted parent precedes Done and can retry after Git succeeds."""
        self.config.workflow.merge_mode = "github"
        self.config.workflow.auto_push = True
        self.config.github.gh_token = True
        with patch.object(gw, "push_parent", side_effect=AgentBoxError("publication unavailable")):
            with self.assertRaisesRegex(AgentBoxError, "publication unavailable"):
                self.dispatch(["finish", "8"])
        self.adapter.set_state.assert_not_called()
        with patch.object(gw, "push_parent", return_value={"pushed": True}) as push:
            self.assertEqual(self.dispatch(["finish", "8"])["state"], "Done")
        push.assert_called_once()

    def test_retry_accepts_original_work_after_later_parent_work_without_publishing_it(self):
        """Current integration may include later work, but child acceptance covers its own head only."""
        self.interrupted_finish()
        self.commit("parent-later.py")
        self.assertEqual(self.dispatch(["finish", "8"])["state"], "Done")
        self.adapter.reset_mock()
        self.config.workflow.merge_mode = "github"
        self.config.workflow.auto_push = True
        self.config.github.gh_token = True
        with patch.object(gw, "push_parent") as push, \
             self.assertRaisesRegex(AgentBoxError, "exact accepted child head"):
            self.dispatch(["finish", "8"])
        push.assert_not_called()
        self.adapter.set_state.assert_not_called()

    def test_retry_rejects_retired_or_stale_snapshot(self):
        """Neither evidence from another scope nor acceptance revoked by a review round survives."""
        self.interrupted_finish()
        saved = workflow.read_evidence(self.root, self.child)
        saved["local_acceptance"]["scope"] = "stale"
        workflow.save_evidence(self.root, self.child, saved)
        with self.assertRaisesRegex(AgentBoxError, "scope is stale"):
            self.dispatch(["finish", "8"])
        original_lock = workflow.lock
        revoked = []

        @contextmanager
        def interleave(root, purpose):
            if not revoked:
                revoked.append(True)
                workflow.reserve_review(root, BINDING, self.child, 3, "new-round", saved["scope"])
            with original_lock(root, purpose):
                yield

        with patch.object(workflow, "lock", interleave), \
             self.assertRaisesRegex(AgentBoxError, "acceptance cache is missing"):
            self.dispatch(["finish", "8"])
        self.adapter.set_state.assert_not_called()

    def test_child_ref_change_after_acceptance_cannot_merge_unvalidated_work(self):
        """The branch may move outside our lock, so merge only the checked immutable head."""
        base_head = gw.local_branch_head(self.root, "ab/7-parent")

        def advance(*args, **kwargs):
            gw.git(self.root, "commit", "--allow-empty", "-m", "unvalidated")
            return []

        with patch.object(workflow, "review_blockers", side_effect=advance), \
             self.assertRaisesRegex(AgentBoxError, "head changed after acceptance"):
            self.dispatch(["finish", "8"])
        self.assertEqual(gw.local_branch_head(self.root, "ab/7-parent"), base_head)
        self.adapter.set_state.assert_not_called()

    def test_snapshot_is_saved_before_git_integration_and_keeps_writer(self):
        """A failure immediately after Git still leaves the accepted scope and writer recoverable."""
        saved = workflow.read_evidence(self.root, self.child)
        saved["writer"] = "codex"
        workflow.save_evidence(self.root, self.child, saved)
        original_git = gw.git

        def observe(root, *args, **kwargs):
            if args[0] == "merge":
                snapshot = workflow.read_evidence(root, self.child)["local_acceptance"]
                self.assertEqual(snapshot["head"], self.head)
                self.assertEqual(snapshot["scope"], saved["scope"])
            return original_git(root, *args, **kwargs)

        with patch.object(gw, "git", side_effect=observe):
            self.dispatch(["finish", "8"])
        self.assertEqual(workflow.read_evidence(self.root, self.parent)["writer"], "codex")


class TransitionReadTests(unittest.TestCase):
    setUp = test_local_workflow.LocalLandTests.setUp
    dispatch = test_local_workflow.LocalLandTests.dispatch

    def test_finish_and_local_land_have_one_locked_task_tree_scope_read(self):
        """Unused dispatch reads add round trips without making locked acceptance fresher."""
        workflow.save_evidence(self.root, self.parent, self.saved)
        for arguments in (["finish", "7"], ["land", "7", "inspect"]):
            self.adapter.get.reset_mock()
            with self.subTest(arguments=arguments), \
                 patch.object(workflow, "tree", wraps=workflow.tree) as tree, \
                 patch.object(workflow, "scope", wraps=workflow.scope) as scope:
                self.dispatch(arguments)
            self.assertEqual(self.adapter.get.call_count, 1)
            self.assertEqual(tree.call_count, 1)
            self.assertEqual(scope.call_count, 1)

    def test_github_pr_and_land_retain_post_network_tree_refresh_only(self):
        """PR network calls can reopen a child, so the second handler tree read is necessary."""
        from agent_box import github_workflow as github

        child = Task("8", "1", "Child", state="Done", parent="7")
        self.parent.children = ["8"]
        self.adapter.get.side_effect = lambda ident: {"7": self.parent, "8": child}[ident]
        self.config.workflow.merge_mode = "github"
        self.config.github.gh_token = True
        self.saved["scope"] = workflow.scope(self.root, "main", [self.parent, child])
        self.saved["review"] = {"verdict": "PASS"}
        workflow.save_evidence(self.root, self.parent, self.saved)
        pr = {"number": 9, "url": "https://github.com/o/r/pull/9", "state": "OPEN", "isDraft": False,
              "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "headRefOid": gw.head_sha(self.root),
              "headRefName": "ab/7-parent", "baseRefName": "main", "latestReviews": [], "statusCheckRollup": []}
        for arguments in (["pr", "7", "--title", "Parent", "--body-file", "/unused"],
                          ["land", "7", "inspect"]):
            self.adapter.get.reset_mock()
            with self.subTest(arguments=arguments), \
                 patch.object(gw, "github_origin", return_value="o/r"), \
                 patch.object(gw, "push_parent"), \
                 patch.object(github, "find_pr", return_value=9), \
                 patch.object(github, "view", return_value=pr), \
                 patch.object(github, "threads", return_value=[]), \
                 patch.object(github, "gh", return_value=[]), \
                 patch.object(workflow, "tree", wraps=workflow.tree) as tree, \
                 patch.object(workflow, "scope", wraps=workflow.scope) as scope:
                self.dispatch(arguments)
            self.assertEqual(self.adapter.get.call_count, 3)
            self.assertEqual(tree.call_count, 2)
            self.assertEqual(scope.call_count, 1)

    def test_validation_keeps_both_task_and_scope_reads(self):
        """Validation must compare requirements and committed work before and after the command."""
        with patch.object(workflow, "tree", wraps=workflow.tree) as tree, \
             patch.object(workflow, "scope", wraps=workflow.scope) as scope:
            self.dispatch(["validate", "7", "--", "true"])
        self.assertEqual(self.adapter.get.call_count, 2)
        self.assertEqual(tree.call_count, 2)
        self.assertEqual(scope.call_count, 2)
