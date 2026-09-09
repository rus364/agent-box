"""Completion retries retain acceptance of the work that actually advanced Git."""
import unittest
from contextlib import contextmanager
from dataclasses import replace
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

    def begin_ready_child(self):
        def claimed(task, state):
            task.state = state
            return task

        self.adapter.update.return_value = self.child
        self.adapter.set_state.side_effect = claimed
        try:
            return self.dispatch(["begin", self.child.id])
        finally:
            self.adapter.set_state.side_effect = None

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

    def test_retry_publishes_current_parent_containing_the_accepted_child(self):
        """Auto-push publishes the parent tip while acceptance stays bound to the child."""
        self.interrupted_finish()
        self.commit("parent-later.py")
        parent_head = gw.head_sha(self.root)
        self.config.workflow.merge_mode = "github"
        self.config.workflow.auto_push = True
        self.config.github.gh_token = True

        def publish(root, branch, base):
            self.adapter.set_state.assert_not_called()
            self.assertEqual(gw.local_branch_head(root, branch), parent_head)
            self.assertEqual(workflow.read_evidence(root, self.child)["local_acceptance"]["head"], self.head)
            return {"pushed": True, "head": parent_head}

        with patch.object(gw, "push_parent", side_effect=publish) as push, \
             patch.object(workflow, "scope", wraps=workflow.scope) as scope:
            self.assertEqual(self.dispatch(["finish", "8"])["state"], "Done")
        push.assert_called_once_with(self.root, "ab/7-parent", "main")
        scope.assert_not_called()
        self.adapter.set_state.assert_called_once_with(self.child, "Done")

    def test_parent_or_child_change_during_publication_keeps_done_unwritten(self):
        """A containing parent may be published only while both checked refs stay stable."""
        self.interrupted_finish()
        self.commit("parent-later.py")
        self.config.workflow.merge_mode = "github"
        self.config.workflow.auto_push = True
        self.config.github.gh_token = True
        for branch in ("ab/8-child", "ab/7-parent"):
            original = gw.local_branch_head(self.root, branch)

            def publish(*args):
                gw.git(self.root, "update-ref", "refs/heads/" + branch, "main")
                return {"pushed": True}

            with self.subTest(branch=branch), patch.object(gw, "push_parent", side_effect=publish), \
                 self.assertRaisesRegex(AgentBoxError, "head changed during publication"):
                self.dispatch(["finish", "8"])
            self.adapter.set_state.assert_not_called()
            gw.git(self.root, "update-ref", "refs/heads/" + branch, original)

    def test_deleted_child_branch_and_lost_acceptance_cannot_invent_no_code_work(self):
        """Resuming Doing after integration cannot recreate the provenance lost with state."""
        self.interrupted_finish()
        gw.git(self.root, "branch", "-D", "ab/8-child")
        workflow.evidence_path(self.root, self.child).unlink()
        self.dispatch(["begin", "8", "--resume"])
        self.dispatch(["validate", "8", "--", "true"])
        with self.assertRaisesRegex(AgentBoxError, "acceptance.*missing"):
            self.dispatch(["finish", "8"])
        self.assertNotIn("child_start", workflow.read_evidence(self.root, self.child))
        self.adapter.set_state.assert_not_called()

    def test_stale_ready_read_cannot_resume_doing_as_a_new_child_claim(self):
        """Only a claim that itself observes Ready can prove a new no-code origin."""
        self.interrupted_finish()
        gw.git(self.root, "branch", "-D", "ab/8-child")
        workflow.evidence_path(self.root, self.child).unlink()
        reads = []

        def get(ident):
            if ident == self.parent.id:
                return self.parent
            reads.append(ident)
            return replace(self.child, state="Ready") if len(reads) == 1 else self.child

        self.adapter.get.side_effect = get
        with self.assertRaisesRegex(AgentBoxError, "already Doing"):
            self.dispatch(["begin", "8", "--resume"])
        self.assertNotIn("child_start", workflow.read_evidence(self.root, self.child))
        self.adapter.set_state.assert_not_called()

    def test_reopened_child_without_origin_cannot_promote_prior_acceptance_to_no_code(self):
        """Deleting an accepted branch does not make its later recreation a first start."""
        self.interrupted_finish()
        gw.git(self.root, "branch", "-D", "ab/8-child")
        self.child.state = "Ready"
        self.begin_ready_child()
        self.adapter.reset_mock()
        workflow.retire_acceptance(self.root, self.adapter, self.child,
                                   workflow.read_evidence(self.root, self.child))
        self.dispatch(["validate", "8", "--", "true"])
        with self.assertRaisesRegex(AgentBoxError, "acceptance.*missing"):
            self.dispatch(["finish", "8"])
        self.assertNotIn("child_start", workflow.read_evidence(self.root, self.child))
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


class NoCodeChildFinishTests(unittest.TestCase):
    """A child begun without commits still needs current acceptance, not just ancestry."""

    dispatch = test_local_workflow.LocalLandTests.dispatch
    commit = ChildFinishTests.commit
    interrupted_finish = ChildFinishTests.interrupted_finish
    begin_ready_child = ChildFinishTests.begin_ready_child

    def setUp(self):
        test_local_workflow.LocalLandTests.setUp(self)
        self.parent.state = "Doing"
        self.parent.children = ["8"]
        self.child = Task("8", "1", "Child", state="Ready", assignees=["1"],
                          parent="7", description="- [x] accepted")
        self.adapter.get.side_effect = lambda ident: {"7": self.parent, "8": self.child}[ident]
        self.begin_ready_child()
        self.head = gw.head_sha(self.root)
        self.adapter.reset_mock()

    def test_new_child_without_commits_finishes_only_after_current_validation(self):
        """Beginning at the parent tip identifies no-code work but does not accept it."""
        with self.assertRaisesRegex(AgentBoxError, "workflow validate"):
            self.dispatch(["finish", "8"])
        self.adapter.set_state.assert_not_called()
        self.dispatch(["validate", "8", "--", "true"])
        with patch.object(gw, "git", wraps=gw.git) as git:
            self.assertEqual(self.dispatch(["finish", "8"])["state"], "Done")
        self.assertFalse(any(call.args[1] == "merge" for call in git.call_args_list))
        self.adapter.set_state.assert_called_once_with(self.child, "Done")

    def test_no_code_done_outage_retries_original_acceptance_from_parent(self):
        """An unchanged child needs the same reliable Done retry as one with code."""
        self.dispatch(["validate", "8", "--", "true"])
        self.interrupted_finish()
        with patch.object(workflow, "scope", wraps=workflow.scope) as scope:
            self.assertEqual(self.dispatch(["finish", "8"])["state"], "Done")
        scope.assert_not_called()

    def test_no_code_requirements_change_requires_fresh_validation(self):
        """The creation head survives a scope change; the prior acceptance does not."""
        self.dispatch(["validate", "8", "--", "true"])
        self.interrupted_finish()
        self.child.description += "\nNew requirement"
        with self.assertRaisesRegex(AgentBoxError, "requirements.*changed"):
            self.dispatch(["finish", "8"])
        self.dispatch(["begin", "8", "--resume"])
        with self.assertRaisesRegex(AgentBoxError, "Validation failed"):
            self.dispatch(["validate", "8", "--", "false"])
        with self.assertRaisesRegex(AgentBoxError, "requirements.*changed"):
            self.dispatch(["finish", "8"])
        self.dispatch(["validate", "8", "--", "true"])
        self.assertEqual(self.dispatch(["finish", "8"])["state"], "Done")

    def test_no_code_current_review_policy_and_retirement_apply(self):
        """An unchanged head cannot skip a required review or retain a retired PASS."""
        self.dispatch(["validate", "8", "--", "true"])
        self.interrupted_finish()
        self.config.workflow.review.policy = "always"
        with self.assertRaisesRegex(AgentBoxError, "PASS review"):
            self.dispatch(["finish", "8"])
        self.dispatch(["begin", "8", "--resume"])
        saved = workflow.read_evidence(self.root, self.child)
        workflow.reserve_review(self.root, BINDING, self.child, 3, "new-round", saved["scope"])
        with self.assertRaisesRegex(AgentBoxError, "PASS review"):
            self.dispatch(["finish", "8"])
        saved = workflow.read_evidence(self.root, self.child)
        saved["review"] = {"verdict": "PASS"}
        workflow.save_evidence(self.root, self.child, saved)
        self.assertEqual(self.dispatch(["finish", "8"])["state"], "Done")

    def test_no_code_acceptance_and_retry_allow_later_parent_work_locally(self):
        """The child's empty scope ends at its own head even when its parent advances."""
        gw.git(self.root, "switch", "ab/7-parent")
        self.commit("later-parent.py")
        parent_head = gw.head_sha(self.root)
        gw.git(self.root, "switch", "ab/8-child")
        self.dispatch(["validate", "8", "--", "true"])
        self.adapter.set_state.side_effect = AgentBoxError("tracker unavailable")
        with self.assertRaisesRegex(AgentBoxError, "tracker unavailable"):
            self.dispatch(["finish", "8"])
        self.adapter.set_state.side_effect = None
        self.assertEqual(self.dispatch(["finish", "8"])["state"], "Done")
        self.assertEqual(gw.local_branch_head(self.root, "ab/7-parent"), parent_head)

    def test_no_code_auto_push_allows_later_parent_work_and_retries_failure(self):
        """An empty child still publishes the current parent before Done, including retries."""
        gw.git(self.root, "switch", "ab/7-parent")
        self.commit("later-parent.py")
        parent_head = gw.head_sha(self.root)
        gw.git(self.root, "switch", "ab/8-child")
        self.dispatch(["validate", "8", "--", "true"])
        self.config.workflow.merge_mode = "github"
        self.config.workflow.auto_push = True
        self.config.github.gh_token = True
        with patch.object(gw, "push_parent", side_effect=AgentBoxError("publication unavailable")):
            with self.assertRaisesRegex(AgentBoxError, "publication unavailable"):
                self.dispatch(["finish", "8"])
        self.adapter.set_state.assert_not_called()

        def publish(root, branch, base):
            self.adapter.set_state.assert_not_called()
            self.assertEqual(gw.local_branch_head(root, branch), parent_head)
            return {"pushed": True, "head": parent_head}

        with patch.object(gw, "push_parent", side_effect=publish) as push:
            self.assertEqual(self.dispatch(["finish", "8"])["state"], "Done")
        push.assert_called_once_with(self.root, "ab/7-parent", "main")
        self.adapter.set_state.assert_called_once_with(self.child, "Done")

    def test_resume_after_real_integration_cannot_reset_start_to_current_head(self):
        """Revalidation after resume cannot disguise lost acceptance of committed work."""
        self.commit("child.py")
        self.head = gw.head_sha(self.root)
        self.dispatch(["validate", "8", "--", "true"])
        self.interrupted_finish()
        self.dispatch(["begin", "8", "--resume"])
        self.dispatch(["validate", "8", "--", "true"])
        with self.assertRaisesRegex(AgentBoxError, "acceptance.*missing"):
            self.dispatch(["finish", "8"])
        self.adapter.set_state.assert_not_called()

    def test_reopening_after_integration_preserves_original_start_and_refuses_empty_acceptance(self):
        """A genuine Ready reopen must not erase the origin of already integrated code."""
        original_start = workflow.read_evidence(self.root, self.child)["child_start"]
        self.commit("child.py")
        self.head = gw.head_sha(self.root)
        self.dispatch(["validate", "8", "--", "true"])
        self.interrupted_finish()
        gw.git(self.root, "branch", "-D", "ab/8-child")
        self.child.state = "Ready"
        self.begin_ready_child()
        self.adapter.reset_mock()
        workflow.retire_acceptance(self.root, self.adapter, self.child,
                                   workflow.read_evidence(self.root, self.child))
        self.dispatch(["validate", "8", "--", "true"])
        with self.assertRaisesRegex(AgentBoxError, "acceptance.*missing"):
            self.dispatch(["finish", "8"])
        self.assertEqual(workflow.read_evidence(self.root, self.child)["child_start"], original_start)
        self.adapter.set_state.assert_not_called()

    def test_unrelated_scope_evidence_does_not_prevent_first_no_code_start(self):
        """Read-only investigation before begin is not prior implementation or acceptance."""
        self.child = replace(self.child, id="9", state="Ready")
        self.parent.children = ["9"]
        self.adapter.get.side_effect = lambda ident: {"7": self.parent, "9": self.child}[ident]
        workflow.save_evidence(self.root, self.child, {"scope": "prior-investigation"})
        self.begin_ready_child()
        self.adapter.reset_mock()
        self.dispatch(["validate", "9", "--", "true"])
        self.assertEqual(self.dispatch(["finish", "9"])["state"], "Done")
        self.adapter.set_state.assert_called_once_with(self.child, "Done")

    def test_missing_start_cannot_be_recreated_by_resume(self):
        """Existing equal tips cannot prove how a child began after its evidence is lost."""
        workflow.evidence_path(self.root, self.child).unlink(missing_ok=True)
        self.dispatch(["begin", "8", "--resume"])
        self.dispatch(["validate", "8", "--", "true"])
        with self.assertRaisesRegex(AgentBoxError, "acceptance.*missing"):
            self.dispatch(["finish", "8"])
        self.adapter.set_state.assert_not_called()

    def test_adopting_remote_equal_tip_does_not_invent_no_code_start(self):
        """A published branch can already contain integrated work, even at the parent tip."""
        gw.git(self.root, "push", "origin", "ab/8-child")
        gw.git(self.root, "switch", "ab/7-parent")
        gw.git(self.root, "branch", "-D", "ab/8-child")
        workflow.evidence_path(self.root, self.child).unlink(missing_ok=True)
        self.dispatch(["begin", "8", "--resume"])
        self.dispatch(["validate", "8", "--", "true"])
        with self.assertRaisesRegex(AgentBoxError, "acceptance.*missing"):
            self.dispatch(["finish", "8"])
        self.adapter.set_state.assert_not_called()

    def test_worker_retirement_keeps_origin_but_requires_new_validation(self):
        """Delegating investigation can retire acceptance without creating a code diff."""
        self.dispatch(["validate", "8", "--", "true"])
        workflow.retire_acceptance(self.root, self.adapter, self.child,
                                   workflow.read_evidence(self.root, self.child))
        with self.assertRaisesRegex(AgentBoxError, "workflow validate"):
            self.dispatch(["finish", "8"])
        self.dispatch(["validate", "8", "--", "true"])
        self.assertEqual(self.dispatch(["finish", "8"])["state"], "Done")


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

    def test_github_pr_reuses_locked_tree_and_land_refreshes_after_network(self):
        """PR reads once; landing defers its full root/tree/scope until after network reads."""
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
                          ["land", "7", "inspect"], ["land", "7", "merge"]):
            self.adapter.get.reset_mock()
            with self.subTest(arguments=arguments), \
                 patch.object(gw, "github_origin", return_value="o/r"), \
                 patch.object(gw, "push_parent"), \
                 patch.object(github, "find_pr", return_value=9), \
                 patch.object(github, "view", side_effect=[pr, {**pr, "state": "MERGED",
                                                               "mergedAt": "2026-01-01T00:00:00Z",
                                                               "mergeCommit": {"oid": "merged"}}]), \
                 patch.object(github, "threads", return_value=[]), \
                 patch.object(github, "gh", return_value=[]), \
                 patch.object(workflow, "tree", wraps=workflow.tree) as tree, \
                 patch.object(workflow, "scope", wraps=workflow.scope) as scope:
                self.dispatch(arguments)
            merging = arguments[-1] == "merge"
            self.assertEqual(self.adapter.get.call_count, 2 if arguments[0] == "pr" else 5 if merging else 3)
            self.assertEqual(tree.call_count, 2 if merging else 1)
            self.assertEqual(scope.call_count, 2 if merging else 1)

    def test_validation_keeps_both_task_and_scope_reads(self):
        """Validation must compare requirements and committed work before and after the command."""
        with patch.object(workflow, "tree", wraps=workflow.tree) as tree, \
             patch.object(workflow, "scope", wraps=workflow.scope) as scope:
            self.dispatch(["validate", "7", "--", "true"])
        self.assertEqual(self.adapter.get.call_count, 2)
        self.assertEqual(tree.call_count, 2)
        self.assertEqual(scope.call_count, 2)
