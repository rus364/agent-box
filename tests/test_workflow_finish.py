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

    def begin_ready_child(self, *, snapshot_state=None, resume=False):
        initial = replace(self.child, state=snapshot_state or self.child.state)
        authoritative = self.child
        reads = []

        def get(ident):
            if ident == self.parent.id:
                return self.parent
            reads.append(ident)
            return initial if len(reads) == 1 else self.child

        def update(task, fields):
            self.child = replace(task, **fields)
            return self.child

        previous = (self.adapter.get.side_effect, self.adapter.update.side_effect,
                    self.adapter.set_state.side_effect)
        self.adapter.get.side_effect = get
        self.adapter.update.side_effect = update
        self.adapter.set_state.side_effect = lambda task, state: update(task, {"state": state})
        try:
            result = self.dispatch(["begin", self.child.id, *(["--resume"] if resume else [])])
        finally:
            (self.adapter.get.side_effect, self.adapter.update.side_effect,
             self.adapter.set_state.side_effect) = previous
        self.assertEqual(initial.state, snapshot_state or authoritative.state)
        return result

    def test_active_child_execution_blocks_integration_despite_validation(self):
        """Child completion must not merge or close work with an outstanding worker."""
        prepared = self.dispatch(["prepare", "8", "--primary", "codex", "--execution", "direct",
                                  "--reason", "Inspect child work"])
        self.dispatch(["validate", "8", "--", "true"])
        parent_head = gw.local_branch_head(self.root, "ab/7-parent")
        saved = workflow.read_evidence(self.root, self.child)
        with self.assertRaisesRegex(AgentBoxError, "Cannot finish: worker execution is prepared"):
            self.dispatch(["finish", "8"])
        self.assertEqual(gw.local_branch_head(self.root, "ab/7-parent"), parent_head)
        self.adapter.set_state.assert_not_called()
        self.assertEqual(saved["execution"]["descriptor"]["scope"], prepared["scope"])

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
        self.dispatch(["begin", "8", "--resume"])
        self.assertNotIn("child_start", workflow.read_evidence(self.root, self.child))
        self.dispatch(["validate", "8", "--", "true"])
        with self.assertRaisesRegex(AgentBoxError, "acceptance.*missing"):
            self.dispatch(["finish", "8"])
        self.adapter.set_state.assert_not_called()

    def test_reopened_child_without_origin_cannot_promote_prior_acceptance_to_no_code(self):
        """Deleting an accepted branch does not make its later recreation a first start."""
        self.interrupted_finish()
        gw.git(self.root, "branch", "-D", "ab/8-child")
        self.child.state = "Ready"
        self.begin_ready_child()
        self.adapter.reset_mock()
        test_local_workflow.completed_worker(self, self.child)
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

    def test_begin_cannot_overwrite_a_concurrent_review_reservation(self):
        """Origin writes must preserve a new round and never resurrect retired acceptance."""
        self.dispatch(["validate", "8", "--", "true"])
        saved = workflow.read_evidence(self.root, self.child)
        saved["review"] = {"verdict": "PASS"}
        workflow.save_evidence(self.root, self.child, saved)
        self.interrupted_finish()
        gw.git(self.root, "switch", "ab/8-child")
        self.commit("later.py")

        for resume in (True, False):
            with self.subTest(resume=resume):
                if not resume:
                    self.child = replace(self.child, id="9", state="Ready")
                    self.parent.children = [self.child.id]
                original_read = workflow.read_evidence
                attempted, busy = [], []

                def reserve(fingerprint):
                    return workflow.reserve_review(self.root, BINDING, self.child, 3,
                                                   "new-round", fingerprint)

                def interleave(root, task):
                    value = original_read(root, task)
                    if task.id == self.child.id and not attempted:
                        fingerprint = workflow.scope(root, "ab/7-parent", [self.child])
                        attempted.append(fingerprint)
                        try:
                            reserve(fingerprint)
                        except AgentBoxError as exc:
                            self.assertIn("Another local review-budget", str(exc))
                            busy.append(True)
                    return value

                with patch.object(workflow, "read_evidence", interleave):
                    self.begin_ready_child(resume=resume)
                self.assertEqual(len(attempted), 1)
                if busy:
                    reserve(attempted[0])
                current = workflow.read_evidence(self.root, self.child)
                self.assertNotIn("review", current)
                self.assertNotIn("local_acceptance", current)
                self.assertEqual(current.get("scope"), attempted[0])
                self.assertEqual(current["child_start"]["work_observed"], resume)
                budget = workflow.read_review_state(self.root, BINDING, self.child)
                self.assertEqual((budget["used"], budget["pending"]), (1, "new-round"))

    def test_claimed_child_recovers_creation_and_switch_failures_at_original_parent(self):
        """A successful Ready claim survives Git failure without substituting the later parent tip."""
        for failure, ident in (("branch", "9"), ("switch", "10")):
            with self.subTest(failure=failure):
                self.child = replace(self.child, id=ident, state="Ready")
                self.parent.children = [ident]
                self.adapter.get.side_effect = lambda key: self.parent if key == "7" else self.child
                origin = gw.local_branch_head(self.root, "ab/7-parent")
                original_git = gw.git

                def fail(root, *args, **kwargs):
                    if args[:2] == (failure, f"ab/{ident}-child"):
                        self.assertEqual(self.child.state, "Doing")
                        raise AgentBoxError("injected child Git failure")
                    return original_git(root, *args, **kwargs)

                with patch.object(gw, "git", side_effect=fail), \
                     self.assertRaisesRegex(AgentBoxError, "injected child Git failure"):
                    self.begin_ready_child()
                self.assertEqual(self.child.state, "Doing")
                saved = workflow.read_evidence(self.root, self.child)
                self.assertEqual(saved.get("child_begin", saved.get("child_start"))["head"], origin)
                self.commit(f"parent-after-{ident}.py")
                self.begin_ready_child(resume=True)
                self.assertEqual(gw.head_sha(self.root), origin)
                self.assertEqual(workflow.read_evidence(self.root, self.child)["child_start"],
                                 {"head": origin, "parent": self.parent.id, "work_observed": False})
                self.adapter.reset_mock()
                with self.assertRaisesRegex(AgentBoxError, "workflow validate"):
                    self.dispatch(["finish", ident])
                self.adapter.set_state.assert_not_called()
                self.dispatch(["validate", ident, "--", "true"])
                self.assertEqual(self.dispatch(["finish", ident])["state"], "Done")

    def test_interrupted_creation_does_not_adopt_an_arbitrary_existing_branch(self):
        """A branch appearing after failed creation does not prove the recorded origin."""
        self.child = replace(self.child, id="9", state="Ready")
        self.parent.children = ["9"]
        self.adapter.get.side_effect = lambda key: self.parent if key == "7" else self.child
        original_git = gw.git

        def fail(root, *args, **kwargs):
            if args[:2] == ("branch", "ab/9-child"):
                raise AgentBoxError("injected creation failure")
            return original_git(root, *args, **kwargs)

        with patch.object(gw, "git", side_effect=fail), \
             self.assertRaisesRegex(AgentBoxError, "injected creation failure"):
            self.begin_ready_child()
        for head in ("ab/7-parent", "main"):
            with self.subTest(head=head):
                gw.git(self.root, "branch", "ab/9-child", head)
                with self.assertRaisesRegex(AgentBoxError, "reconcile its origin"):
                    self.begin_ready_child(resume=True)
                self.assertNotIn("child_start", workflow.read_evidence(self.root, self.child))
                gw.git(self.root, "branch", "-D", "ab/9-child")

    def test_stale_doing_snapshot_cannot_suppress_a_genuine_ready_start(self):
        """The Ready claim read must prove no-code origin even if dispatch saw Doing."""
        for resume, ident in ((False, "9"), (True, "10")):
            with self.subTest(resume=resume):
                self.child = replace(self.child, id=ident, state="Ready")
                self.parent.children = [ident]
                self.adapter.get.side_effect = lambda key: self.parent if key == "7" else self.child
                ready = self.child
                self.begin_ready_child(snapshot_state="Doing", resume=resume)
                self.assertEqual(ready.state, "Ready")
                self.assertEqual(workflow.read_evidence(self.root, self.child)["child_start"],
                                 {"head": gw.head_sha(self.root), "parent": self.parent.id,
                                  "work_observed": False})
                self.adapter.reset_mock()
                self.dispatch(["validate", ident, "--", "true"])
                self.assertEqual(self.dispatch(["finish", ident])["state"], "Done")
                self.adapter.set_state.assert_called_once_with(self.child, "Done")

    def test_stale_ready_snapshot_requires_resume_and_cannot_invent_origin(self):
        """Doing and Review claims need deliberate resume and never establish a new start."""
        for state, ident in (("Doing", "9"), ("Review", "10")):
            with self.subTest(state=state):
                self.child = replace(self.child, id=ident, state=state)
                self.parent.children = [ident]
                self.adapter.get.side_effect = lambda key: self.parent if key == "7" else self.child
                current = self.child
                with self.assertRaisesRegex(AgentBoxError, "already Doing"):
                    self.begin_ready_child(snapshot_state="Ready")
                self.begin_ready_child(snapshot_state="Ready", resume=True)
                self.assertEqual(current.state, state)
                self.assertEqual(self.child.state, "Doing")
                self.assertNotIn("child_start", workflow.read_evidence(self.root, self.child))
                self.adapter.reset_mock()
                self.dispatch(["validate", ident, "--", "true"])
                with self.assertRaisesRegex(AgentBoxError, "acceptance.*missing"):
                    self.dispatch(["finish", ident])
                self.adapter.set_state.assert_not_called()

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

    def test_parent_head_seen_during_validation_is_not_child_work(self):
        """Validation invoked from another branch must not invent changes to an untouched child."""
        gw.git(self.root, "switch", "ab/7-parent")
        self.commit("later-parent.py")
        self.dispatch(["validate", "8", "--", "true"])
        self.dispatch(["begin", "8", "--resume"])
        self.dispatch(["validate", "8", "--", "true"])
        self.assertEqual(self.dispatch(["finish", "8"])["state"], "Done")

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

    def returned_origin(self, *, remote=False, retirement="scope", integrate=True):
        origin = self.head
        if remote:
            gw.git(self.root, "push", "origin", "ab/8-child")
        self.commit("child.py")
        self.head = gw.head_sha(self.root)
        self.dispatch(["validate", "8", "--", "true"])
        if integrate:
            self.interrupted_finish()
        else:
            gw.git(self.root, "switch", "ab/7-parent")
        if retirement == "scope":
            self.child.description += "\nNew requirement"
            self.dispatch(["begin", "8", "--resume"])
            self.dispatch(["validate", "8", "--", "true"])
            gw.git(self.root, "switch", "ab/7-parent")
        elif retirement == "review":
            saved = workflow.read_evidence(self.root, self.child)
            workflow.reserve_review(self.root, BINDING, self.child, 3, "new-round", saved["scope"])
        else:
            gw.git(self.root, "switch", "ab/8-child")
            test_local_workflow.completed_worker(self, self.child)
            gw.git(self.root, "switch", "ab/7-parent")
        self.assertNotIn("local_acceptance", workflow.read_evidence(self.root, self.child))
        if remote:
            gw.git(self.root, "branch", "-D", "ab/8-child")
        else:
            gw.git(self.root, "update-ref", "refs/heads/ab/8-child", origin)
        self.dispatch(["begin", "8", "--resume"])
        self.assertEqual(gw.head_sha(self.root), origin)
        self.assertEqual(gw.local_branch_head(self.root, "ab/7-parent"), self.head if integrate else origin)
        self.dispatch(["validate", "8", "--", "true"])
        self.assertEqual(gw.git(self.root, "diff", "ab/7-parent...HEAD", "--"), "")
        with self.assertRaisesRegex(AgentBoxError, "acceptance.*missing"):
            self.dispatch(["finish", "8"])
        self.adapter.set_state.assert_not_called()

    def test_stale_remote_origin_cannot_accept_work_lost_after_requirements_retirement(self):
        """Adopting published H0 cannot disguise integrated H1 after its acceptance is retired."""
        self.returned_origin(remote=True)

    def test_finish_records_non_origin_history_before_merge_and_retries_accepted_work(self):
        """Durable history precedes integration and does not revoke its valid retry evidence."""
        origin = self.head
        self.commit("child.py")
        self.head = gw.head_sha(self.root)
        saved = workflow.read_evidence(self.root, self.child)
        saved.update(scope=workflow.scope(self.root, "ab/7-parent", [self.child]),
                     validation={"command": ["true"], "exit": 0})
        workflow.save_evidence(self.root, self.child, saved)
        original_git = gw.git

        def observe(root, *args, **kwargs):
            if args[0] == "merge":
                before_merge = workflow.read_evidence(root, self.child)
                self.assertEqual(before_merge["child_start"],
                                 {"head": origin, "parent": self.parent.id, "work_observed": True})
                self.assertEqual(before_merge["local_acceptance"]["head"], self.head)
            return original_git(root, *args, **kwargs)

        with patch.object(gw, "git", side_effect=observe):
            self.interrupted_finish()
        with patch.object(gw, "git", wraps=gw.git) as git, \
             patch.object(workflow, "scope", wraps=workflow.scope) as scope:
            self.assertEqual(self.dispatch(["finish", "8"])["state"], "Done")
        scope.assert_not_called()
        self.assertFalse(any(call.args[1] == "merge" for call in git.call_args_list))
        self.adapter.set_state.assert_called_once_with(self.child, "Done")

    def test_direct_ref_return_cannot_erase_integration_after_review_retirement(self):
        """Starting another review cannot make a reset child look continuously unchanged."""
        self.returned_origin(retirement="review")

    def test_direct_ref_return_cannot_erase_integration_after_worker_retirement(self):
        """Delegation retires acceptance but must preserve known non-origin work."""
        self.returned_origin(retirement="worker")

    def test_validated_non_origin_work_cannot_be_reset_into_no_code_acceptance(self):
        """Observed child work stays known even when a ref is reset before workflow integrates it."""
        self.returned_origin(integrate=False)

    def test_resumed_non_origin_ref_cannot_be_reset_into_no_code_acceptance(self):
        """Resuming an existing changed child must not forget that work on a later reset."""
        origin = self.head
        self.commit("child.py")
        self.dispatch(["begin", "8", "--resume"])
        gw.git(self.root, "reset", "--hard", origin)
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
        self.child = replace(self.child, state="Ready")
        self.begin_ready_child(snapshot_state="Doing", resume=True)
        self.adapter.reset_mock()
        test_local_workflow.completed_worker(self, self.child)
        self.dispatch(["validate", "8", "--", "true"])
        with self.assertRaisesRegex(AgentBoxError, "acceptance.*missing"):
            self.dispatch(["finish", "8"])
        self.assertEqual(workflow.read_evidence(self.root, self.child)["child_start"],
                         {**original_start, "work_observed": True})
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
        test_local_workflow.completed_worker(self, self.child)
        with self.assertRaisesRegex(AgentBoxError, "workflow validate"):
            self.dispatch(["finish", "8"])
        self.dispatch(["validate", "8", "--", "true"])
        self.assertEqual(self.dispatch(["finish", "8"])["state"], "Done")


class TransitionReadTests(unittest.TestCase):
    setUp = test_local_workflow.LocalLandTests.setUp
    dispatch = test_local_workflow.LocalLandTests.dispatch

    def locked_task(self, ident):
        for purpose in ("review-budget", "work"):
            with self.assertRaisesRegex(AgentBoxError, f"Another local {purpose}"):
                with workflow.lock(self.root, purpose):
                    pass
        return self.parent

    def test_local_reconcile_reads_recovery_root_once_under_both_locks(self):
        """Recovery routing and acceptance must consume the same locked root snapshot."""
        workflow.save_evidence(self.root, self.parent, self.saved)
        self.adapter.set_state.side_effect = AgentBoxError("tracker unavailable")
        with self.assertRaisesRegex(AgentBoxError, "tracker unavailable"):
            self.dispatch(["land", "7", "merge"])
        self.adapter.set_state.side_effect = None
        self.adapter.get.reset_mock()
        self.adapter.get.side_effect = self.locked_task
        with patch.object(workflow, "tree", wraps=workflow.tree) as tree, \
             patch.object(workflow, "scope", wraps=workflow.scope) as scope, \
             patch.object(gw, "git", wraps=gw.git) as git:
            self.assertEqual(self.dispatch(["land", "7", "reconcile"])["state"], "Done")
        self.adapter.get.assert_called_once_with("7")
        tree.assert_called_once()
        scope.assert_not_called()
        self.assertFalse(any(call.args[1:2] == ("merge",) for call in git.call_args_list))

    def test_github_reconcile_keeps_locked_root_refresh_after_network(self):
        """The routing snapshot cannot replace the acceptance read after remote queries."""
        from agent_box import github_workflow as github

        self.config.workflow.merge_mode = "github"
        self.config.github.gh_token = True
        workflow.save_evidence(self.root, self.parent, {**self.saved, "review": {"verdict": "PASS"}})
        events = []

        def read(ident):
            events.append("task")
            return self.locked_task(ident)

        def remote(*args):
            events.append("remote")
            return None

        self.adapter.get.side_effect = read
        merged = {"number": 9, "url": "https://github.com/o/r/pull/9", "state": "MERGED",
                  "headRefOid": gw.head_sha(self.root), "headRefName": "ab/7-parent",
                  "baseRefName": "main", "mergedAt": "2026-01-01T00:00:00Z",
                  "mergeCommit": {"oid": "merged"}}
        with patch.object(gw, "github_origin", return_value="o/r"), \
             patch.object(github, "find_pr", return_value=9), \
             patch.object(github, "view", return_value=merged), \
             patch.object(gw, "remote_branch_head", side_effect=remote), \
             patch.object(workflow, "tree", wraps=workflow.tree) as tree, \
             patch.object(workflow, "scope", wraps=workflow.scope) as scope:
            self.assertEqual(self.dispatch(["land", "7", "reconcile"])["state"], "Done")
        self.assertEqual(events, ["task", "remote", "task"])
        tree.assert_called_once()
        scope.assert_called_once()
        self.adapter.set_state.assert_called_once_with(self.parent, "Done")

    def test_done_reconcile_and_cleanup_read_root_under_the_action_locks(self):
        """Done routing must obtain fresh lifecycle before inspecting or deleting refs."""
        gw.git(self.root, "switch", "main")
        gw.git(self.root, "merge", "--ff-only", "ab/7-parent")
        self.parent.state = "Done"
        self.adapter.get.side_effect = self.locked_task
        for action in ("reconcile", "cleanup"):
            self.adapter.get.reset_mock()
            with self.subTest(action=action):
                self.assertEqual(self.dispatch(["land", "7", action])["state"], "Done")
            self.adapter.get.assert_called_once_with("7")
        self.adapter.set_state.assert_not_called()

    def test_reconcile_selects_route_from_lifecycle_at_lock_acquisition(self):
        """A lifecycle change before locking must select the corresponding recovery path."""
        from agent_box import branch_cleanup, local_workflow

        workflow.save_evidence(self.root, self.parent, self.saved)
        self.adapter.set_state.side_effect = AgentBoxError("tracker unavailable")
        with self.assertRaisesRegex(AgentBoxError, "tracker unavailable"):
            self.dispatch(["land", "7", "merge"])
        self.adapter.set_state.side_effect = None
        original_lock = workflow.lock
        for initial, fresh in (("Review", "Done"), ("Done", "Review")):
            self.parent.state = initial
            self.adapter.set_state.reset_mock()
            self.adapter.get.side_effect = lambda ident: replace(self.parent)

            @contextmanager
            def interleave(root, purpose):
                if purpose == "work":
                    self.parent.state = fresh
                with original_lock(root, purpose):
                    yield

            with self.subTest(initial=initial, fresh=fresh), \
                 patch.object(workflow, "lock", interleave), \
                 patch.object(branch_cleanup, "reconcile", wraps=branch_cleanup.reconcile) as done, \
                 patch.object(local_workflow, "operate", wraps=local_workflow.operate) as active:
                self.assertEqual(self.dispatch(["land", "7", "reconcile"])["state"], "Done")
            (done if fresh == "Done" else active).assert_called_once()
            (active if fresh == "Done" else done).assert_not_called()
            if fresh == "Done":
                self.adapter.set_state.assert_not_called()
            else:
                self.adapter.set_state.assert_called_once_with(self.parent, "Done")

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
