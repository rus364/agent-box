"""Local landing proves integration from Git without GitHub or stored merge state."""
import tempfile
import unittest
from contextlib import contextmanager
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
        evidence_patch = patch.object(workflow, "WORKFLOW_STATE",
                                      Path(self.temporary.name) / "state")
        evidence_patch.start()
        self.addCleanup(evidence_patch.stop)
        gw.git(self.root, "switch", "-c", "ab/7-parent")
        (self.root / "change.py").write_text("value = 1\n")
        gw.git(self.root, "add", "change.py")
        gw.git(self.root, "commit", "-m", "task work")
        self.parent = Task("7", "1", "Parent", state="Review", assignees=["1"],
                           description="## Acceptance\n- [x] accepted")
        self.adapter = Mock()
        self.adapter.identity.return_value = {"id": 1}
        self.adapter.get.side_effect = lambda ident: {"7": self.parent}[ident]
        self.config = Config()
        self.config.workflow.enabled = True
        self.config.workflow.merge_mode = "local"
        self.config.workflow.auto_push = False
        self.config.workflow.review.policy = "off"
        self.info = {"branch": "ab/7-parent", "base": gw.default_base(self.root)}
        self.saved = {"scope": workflow.scope(self.root, "main", [self.parent]),
                      "validation": {"command": ["true"], "exit": 0}}

    def operate(self, action, *, method=None, saved=None):
        args = SimpleNamespace(command="land", task=self.parent.id, action=action, method=method)
        workflow.save_evidence(self.root, self.parent, self.saved if saved is None else saved)
        return workflow.landing(args, self.root, self.config, self.adapter)

    def dispatch(self, arguments):
        with patch.object(workflow, "effective_config", return_value=self.config), \
             patch.object(workflow, "repository_root", return_value=self.root), \
             patch.object(Binding, "load", return_value=BINDING), \
             patch.object(workflow, "make_adapter", return_value=self.adapter):
            return workflow.dispatch(workflow.parser().parse_args(arguments))

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

    def test_reservation_between_dispatch_and_acceptance_blocks_finish_and_landing(self):
        """A cached PASS cannot survive a new round before the acceptance lock is acquired."""
        self.config.workflow.review.policy = "always"
        self.saved["review"] = {"verdict": "PASS"}
        base_head = gw.local_branch_head(self.root, "main")
        for command in (["finish", "7"], ["land", "7", "merge"]):
            self.parent.state = "Doing" if command[0] == "finish" else "Review"
            workflow.save_evidence(self.root, self.parent, self.saved)
            original_lock = workflow.lock
            reserved = []

            @contextmanager
            def interleave(root, purpose):
                if not reserved:
                    reserved.append(True)
                    workflow.reserve_review(root, BINDING, self.parent, 3,
                                            "new-round", self.saved["scope"])
                with original_lock(root, purpose):
                    yield

            with self.subTest(command=command), patch.object(workflow, "lock", interleave), \
                 self.assertRaisesRegex(AgentBoxError, "PASS review"):
                self.dispatch(command)
            self.assertTrue(reserved)
            self.assertEqual(gw.local_branch_head(self.root, "main"), base_head)
            self.adapter.set_state.assert_not_called()

    def test_validation_does_not_restore_pass_retired_while_checks_run(self):
        """Validation adds its result to current evidence, never its cached acceptance."""
        self.saved["review"] = {"verdict": "PASS"}
        workflow.save_evidence(self.root, self.parent, self.saved)
        original_run = workflow.subprocess.run

        def run(command, *args, **kwargs):
            if command == ["validation-fixture"]:
                workflow.reserve_review(self.root, BINDING, self.parent, 3,
                                        "validation-round", self.saved["scope"])
                return SimpleNamespace(returncode=0)
            return original_run(command, *args, **kwargs)

        with patch.object(workflow.subprocess, "run", run):
            result = self.dispatch(["validate", "7", "--", "validation-fixture"])
        self.assertEqual(result["validation"]["exit"], 0)
        self.assertNotIn("review", result)
        self.assertNotIn("review", workflow.read_evidence(self.root, self.parent))

    def test_landing_keeps_reservation_out_until_acceptance_is_consumed(self):
        """The other ordering allows landing and rejects a concurrent new reservation."""
        from agent_box import local_workflow

        self.config.workflow.review.policy = "always"
        self.saved["review"] = {"verdict": "PASS"}
        workflow.save_evidence(self.root, self.parent, self.saved)
        original_publish = local_workflow._publish

        def publish(*args):
            with self.assertRaisesRegex(AgentBoxError, "Another local review-budget"):
                workflow.reserve_review(self.root, BINDING, self.parent, 3,
                                        "too-late", self.saved["scope"])
            return original_publish(*args)

        with patch.object(local_workflow, "_publish", publish):
            result = self.dispatch(["land", "7", "merge"])
        self.assertEqual(result["state"], "Done")
        self.assertEqual(workflow.read_review_budget(self.root, BINDING, self.parent, 3)["used"], 0)

    def test_reconciliation_rereads_revoked_premerge_acceptance(self):
        """A reservation after dispatch also invalidates the cached recovery snapshot."""

        self.config.workflow.review.policy = "always"
        self.saved["review"] = {"verdict": "PASS"}
        self.adapter.set_state.side_effect = AgentBoxError("tracker unavailable")
        with self.assertRaisesRegex(AgentBoxError, "tracker unavailable"):
            self.operate("merge")
        self.adapter.set_state.side_effect = None
        self.adapter.set_state.reset_mock()
        original_landing = workflow.landing

        def interleave(*args):
            workflow.reserve_review(self.root, BINDING, self.parent, 3,
                                    "recovery-round", workflow.scope(self.root, "main", [self.parent]))
            return original_landing(*args)

        with patch.object(workflow, "landing", interleave), \
             self.assertRaisesRegex(AgentBoxError, "Pre-merge acceptance cache is missing"):
            self.dispatch(["land", "7", "reconcile"])
        self.adapter.set_state.assert_not_called()

    def test_github_refreshes_local_acceptance_but_retains_current_head_remote_approval(self):
        """A new local round removes cached PASS without invalidating GitHub's own approval."""
        from agent_box import github_workflow as github

        self.config.workflow.merge_mode = "github"
        self.config.github.gh_token = True
        self.saved["review"] = {"verdict": "PASS"}
        head = gw.head_sha(self.root)
        opened = {"number": 9, "url": "https://github.com/o/r/pull/9", "state": "OPEN",
                  "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                  "headRefOid": head, "headRefName": self.info["branch"], "baseRefName": "main",
                  "latestReviews": [], "statusCheckRollup": []}
        original_landing = workflow.landing

        def interleave(*args):
            workflow.reserve_review(self.root, BINDING, self.parent, 3,
                                    "github-round", self.saved["scope"])
            return original_landing(*args)

        for approved in (False, True):
            workflow.save_evidence(self.root, self.parent, self.saved)
            opened["latestReviews"] = [{"state": "APPROVED", "commit": {"oid": head}}] if approved else []
            with self.subTest(approved=approved), patch.object(workflow, "landing", interleave), \
                 patch.object(gw, "github_origin", return_value="o/r"), \
                 patch.object(github, "find_pr", return_value=9), \
                 patch.object(github, "threads", return_value=[]), \
                 patch.object(github, "view", return_value=opened), patch.object(github, "gh") as gh:
                if approved:
                    self.dispatch(["land", "7", "merge"])
                    self.assertEqual(gh.call_args.args[1:3], ("pr", "merge"))
                else:
                    with self.assertRaisesRegex(AgentBoxError, "fresh local PASS"):
                        self.dispatch(["land", "7", "merge"])
                    gh.assert_not_called()

    def test_inspect_explains_runtime_route_and_exact_auto_boundary_without_mutation(self):
        """A two-file change must expose the real review trigger before landing."""
        (self.root / "second.py").write_text("value = 2\n")
        gw.git(self.root, "add", "second.py")
        gw.git(self.root, "commit", "-m", "second file")
        self.config.workflow.review.policy = "auto"
        self.config.workflow.review.max_files = 2
        result = self.dispatch(["inspect", "7"])
        self.assertEqual(result["configuration"]["merge_mode"], "local")
        self.assertFalse(result["configuration"]["auto_push"])
        self.assertEqual(result["review"]["files"], 2)
        self.assertIn("changed files 2 >= max_files 2", result["review"]["reasons"])
        self.assertTrue(result["review"]["required"])
        self.assertEqual(gw.current_branch(self.root), "ab/7-parent")
        self.adapter.set_state.assert_not_called()

    def approve(self, reason="User explicitly accepted this result"):
        workflow.save_evidence(self.root, self.parent, self.saved)
        return self.dispatch(["approve", "7", "--scope", self.saved["scope"], "--reason", reason])

    def test_explicit_human_acceptance_lands_and_cleans_up_without_fabricating_review(self):
        """Human approval is separate evidence and retains the normal completion path."""
        self.parent.state = "Doing"
        self.config.workflow.review.policy = "always"
        self.config.workflow.cleanup_local_branches = True
        self.config.workflow.cleanup_remote_branches = True
        approval = self.approve()
        self.assertEqual(approval["human_acceptance"]["actor"], "1")
        saved = workflow.read_evidence(self.root, self.parent)
        self.assertNotIn("review", saved)
        self.dispatch(["finish", "7"])
        self.adapter.set_state.assert_called_with(self.parent, "Review")
        self.parent.state = "Review"
        with patch("agent_box.branch_cleanup._remote_refs", side_effect=AgentBoxError("SSH host key unavailable")):
            result = self.dispatch(["land", "7", "merge"])
        self.assertEqual(result["state"], "Done")
        self.assertEqual(result["cleanup"]["local"]["status"], "deleted")
        self.assertEqual(result["cleanup"]["remote"]["status"], "error")
        self.assertIn("SSH host key unavailable", result["cleanup"]["remote"]["reason"])
        self.assertEqual(gw.current_branch(self.root), "main")
        self.assertEqual(gw.git(self.root, "branch", "--list", "ab/7-parent"), "")

    def test_human_approval_requires_explicit_scope_validation_and_local_parent(self):
        """A vague acceptance or Github task cannot acquire a local review exception."""
        workflow.save_evidence(self.root, self.parent, self.saved)
        with self.assertRaisesRegex(AgentBoxError, "scope"):
            self.dispatch(["approve", "7", "--scope", "stale", "--reason", "Accepted"])
        with self.assertRaisesRegex(AgentBoxError, "reason"):
            self.approve("   ")
        self.config.workflow.merge_mode = "github"
        with self.assertRaisesRegex(AgentBoxError, "local parent"):
            self.approve()
        self.config.workflow.merge_mode = "local"
        self.saved.pop("validation")
        with self.assertRaisesRegex(AgentBoxError, "validation"):
            self.approve()
        self.adapter.set_state.assert_not_called()

    def test_human_acceptance_expires_on_new_work_and_does_not_approve_another_scope(self):
        """An accepted commit does not authorize later edits, even after validation."""
        self.config.workflow.review.policy = "always"
        self.approve()
        gw.git(self.root, "commit", "--allow-empty", "-m", "new work")
        saved = workflow.evidence(self.root, self.parent, workflow.scope(self.root, "main", [self.parent]))
        saved["validation"] = {"exit": 0}
        workflow.save_evidence(self.root, self.parent, saved)
        with self.assertRaisesRegex(AgentBoxError, "PASS review"):
            self.dispatch(["land", "7", "merge"])
        self.adapter.set_state.assert_not_called()

    def test_human_acceptance_recovery_preserves_original_scope_after_tracker_failure(self):
        """The same approval must survive base advancement, but not new requirements."""
        self.config.workflow.review.policy = "always"
        self.approve()
        self.adapter.set_state.side_effect = AgentBoxError("tracker unavailable")
        with self.assertRaisesRegex(AgentBoxError, "tracker unavailable"):
            self.dispatch(["land", "7", "merge"])
        self.adapter.set_state.side_effect = None
        self.adapter.set_state.reset_mock()
        result = self.dispatch(["land", "7", "reconcile"])
        self.assertEqual(result["state"], "Done")
        self.parent.description += "\n- [x] changed requirement"
        with self.assertRaisesRegex(AgentBoxError, "requirements.*changed"):
            self.dispatch(["land", "7", "reconcile"])

    def test_human_acceptance_cannot_waive_ownership_or_checklist_constraints(self):
        """Only a ready result owned by the coordinator can be presented for acceptance."""
        self.parent.assignees = ["someone-else"]
        with self.assertRaisesRegex(AgentBoxError, "assigned"):
            self.approve()
        self.parent.assignees = ["1"]
        self.parent.description = "## Acceptance\n- [ ] accepted"
        with self.assertRaisesRegex(AgentBoxError, "checklist"):
            self.approve()
        self.assertNotIn("human_acceptance", workflow.read_evidence(self.root, self.parent))

    def test_human_acceptance_expires_when_requirements_change(self):
        """Checking a new requirement must not silently accept a broader task."""
        self.config.workflow.review.policy = "always"
        self.approve()
        self.parent.description += "\n- [x] newly required behavior"
        saved = workflow.evidence(self.root, self.parent, workflow.scope(self.root, "main", [self.parent]))
        saved["validation"] = {"exit": 0}
        workflow.save_evidence(self.root, self.parent, saved)
        with self.assertRaisesRegex(AgentBoxError, "PASS review"):
            self.dispatch(["land", "7", "merge"])
        self.adapter.set_state.assert_not_called()

    def test_human_acceptance_does_not_relax_github_completion(self):
        """Switching modes cannot turn local human acceptance into GitHub review."""
        self.config.workflow.review.policy = "always"
        self.parent.state = "Doing"
        self.approve()
        self.config.workflow.merge_mode = "github"
        with self.assertRaisesRegex(AgentBoxError, "PASS review"):
            self.dispatch(["finish", "7"])
        self.adapter.set_state.assert_not_called()

    def test_worker_start_retires_human_acceptance_even_before_new_commits(self):
        """A failed worker must not leave earlier approval standing over its work."""
        self.parent.state = "Doing"
        self.config.workflow.review.policy = "always"
        self.approve()
        saved = workflow.read_evidence(self.root, self.parent)
        workflow.retire_acceptance(self.root, self.adapter, self.parent, saved)
        saved["validation"] = {"exit": 0}
        workflow.save_evidence(self.root, self.parent, saved)
        with self.assertRaisesRegex(AgentBoxError, "PASS review"):
            self.dispatch(["finish", "7"])

    def test_merge_refuses_dirty_or_diverged_work_before_switching(self):
        (self.root / "dirty").write_text("dirty")
        with self.assertRaisesRegex(AgentBoxError, "requires a clean working tree"):
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

    def test_reconcile_refuses_ancestry_without_acceptance_even_for_unchanged_branch(self):
        """Integration alone must never manufacture validation or a PASS review."""
        gw.git(self.root, "switch", "main")
        gw.git(self.root, "branch", "-f", "ab/7-parent", "main")
        self.config.workflow.review.policy = "always"
        with self.assertRaisesRegex(AgentBoxError, "fresh validation.*review"):
            self.operate("reconcile", saved={})
        self.adapter.set_state.assert_not_called()
        self.assertEqual(self.parent.state, "Review")

    def test_reconcile_refuses_raw_manual_merge_without_acceptance(self):
        """Manual Git inclusion is not evidence of workflow acceptance."""
        task_head = gw.git(self.root, "rev-parse", "ab/7-parent")
        gw.git(self.root, "switch", "main")
        gw.git(self.root, "merge", "--ff-only", task_head)
        with self.assertRaisesRegex(AgentBoxError, "fresh validation.*review"):
            self.operate("reconcile", saved={})
        self.adapter.set_state.assert_not_called()

    def failed_tracker_merge(self):
        self.adapter.set_state.side_effect = AgentBoxError("tracker unavailable")
        with self.assertRaisesRegex(AgentBoxError, "tracker unavailable"):
            self.operate("merge")
        self.adapter.set_state.side_effect = None
        self.adapter.set_state.reset_mock()
        return workflow.read_evidence(self.root, self.parent)

    def test_reconcile_rejects_changed_head_even_after_new_work_is_integrated(self):
        """A second merge cannot reuse acceptance of the previous task tip."""
        accepted = self.failed_tracker_merge()
        gw.git(self.root, "switch", "ab/7-parent")
        gw.git(self.root, "commit", "--allow-empty", "-m", "new work")
        gw.git(self.root, "switch", "main")
        with self.assertRaisesRegex(AgentBoxError, "not integrated"):
            self.operate("reconcile", saved=accepted)
        gw.git(self.root, "merge", "--ff-only", "ab/7-parent")
        with self.assertRaisesRegex(AgentBoxError, "head.*changed"):
            self.operate("reconcile", saved=accepted)
        self.adapter.set_state.assert_not_called()
        self.assertEqual(self.parent.state, "Review")

    def test_reconcile_rejects_changed_requirements_and_missing_cache(self):
        """Checked acceptance boxes cannot substitute for acceptance of new requirements."""
        accepted = self.failed_tracker_merge()
        self.parent.description += "\n- [x] newly added requirement"
        with self.assertRaisesRegex(AgentBoxError, "requirements.*changed"):
            self.operate("reconcile", saved=accepted)
        with self.assertRaisesRegex(AgentBoxError, "fresh validation.*review"):
            self.operate("reconcile", saved={})
        self.adapter.set_state.assert_not_called()
        self.assertEqual(self.parent.state, "Review")

    def test_reconcile_applies_current_review_policy_to_original_diff(self):
        """After merge the base diff is empty, but auto review still covers original work."""
        self.saved["review"] = {"verdict": "SKIPPED"}
        accepted = self.failed_tracker_merge()
        for policy in ("always", "auto"):
            self.config.workflow.review.policy = policy
            self.config.workflow.review.max_files = 1
            with self.assertRaisesRegex(AgentBoxError, "PASS review"):
                self.operate("reconcile", saved=accepted)
        self.adapter.set_state.assert_not_called()

    def test_reconcile_accepts_real_review_under_always_and_auto_policies(self):
        """A saved PASS retains its scope across the base fast-forward."""
        self.config.workflow.review.policy = "always"
        self.saved["review"] = {"verdict": "PASS", "summary": "Independent review"}
        accepted = self.failed_tracker_merge()
        for policy in ("always", "auto", "off"):
            self.config.workflow.review.policy = policy
            result = self.operate("reconcile", saved=accepted)
            self.assertEqual(result["state"], "Done")

    def test_dispatch_preserves_premerge_acceptance_after_base_advances_further(self):
        """Recovery loads original acceptance but proves inclusion from the live base."""
        self.config.workflow.review.policy = "auto"
        self.saved["review"] = {"verdict": "SKIPPED"}
        workflow.save_evidence(self.root, self.parent, self.saved)
        self.adapter.set_state.side_effect = AgentBoxError("tracker unavailable")
        with self.assertRaisesRegex(AgentBoxError, "tracker unavailable"):
            self.dispatch(["land", "7", "merge"])
        gw.git(self.root, "commit", "--allow-empty", "-m", "later base work")
        self.adapter.set_state.side_effect = None
        result = self.dispatch(["land", "7", "reconcile"])
        self.assertEqual(result["state"], "Done")

    def test_snapshot_write_failure_stops_before_git_mutation(self):
        """A tracker failure must never strand a merge without its acceptance snapshot."""
        with patch.object(workflow, "save_evidence", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.operate("merge")
        self.assertEqual(gw.current_branch(self.root), "ab/7-parent")
        self.adapter.set_state.assert_not_called()

    def test_saved_base_identity_cannot_select_another_ref(self):
        """Snapshot names constrain acceptance; only live Git resolution selects refs."""
        accepted = self.failed_tracker_merge()
        accepted["local_acceptance"]["base"] = "ab/7-parent"
        with self.assertRaisesRegex(AgentBoxError, "base identity has changed"):
            self.operate("reconcile", saved=accepted)
        self.adapter.set_state.assert_not_called()

    def test_acceptance_snapshot_does_not_replace_live_git_inclusion(self):
        """Acceptance survives a failed tracker write, but is never a merge registry."""
        original_base = gw.local_branch_head(self.root, "main")
        accepted = self.failed_tracker_merge()
        gw.git(self.root, "reset", "--hard", original_base)
        with self.assertRaisesRegex(AgentBoxError, "not integrated"):
            self.operate("reconcile", saved=accepted)
        self.adapter.set_state.assert_not_called()

    def test_reconcile_rechecks_head_after_acceptance_checks(self):
        """Concurrent task work cannot be completed using the earlier accepted head."""
        from agent_box import local_workflow
        accepted = self.failed_tracker_merge()
        check = local_workflow._recovery_reasons

        def move_head(*args):
            reasons = check(*args)
            gw.git(self.root, "commit", "--allow-empty", "-m", "concurrent work")
            gw.git(self.root, "branch", "-f", "ab/7-parent", "main")
            return reasons

        with patch.object(local_workflow, "_recovery_reasons", side_effect=move_head):
            with self.assertRaisesRegex(AgentBoxError, "head has changed during reconciliation"):
                self.operate("reconcile", saved=accepted)
        self.adapter.set_state.assert_not_called()

    def test_off_policy_missing_cache_recovery_still_requires_new_validation(self):
        """Turning off review never turns off validation of the integrated task head."""
        self.failed_tracker_merge()
        workflow.evidence_path(self.root, self.parent).unlink()
        gw.git(self.root, "switch", "ab/7-parent")
        with self.assertRaisesRegex(AgentBoxError, "validation"):
            self.dispatch(["land", "7", "merge"])
        self.dispatch(["validate", "7", "--", "true"])
        self.assertEqual(self.dispatch(["land", "7", "merge"])["state"], "Done")

    def test_missing_cache_can_recover_with_fresh_validation_and_independent_review(self):
        """Auto must request an actual review when the original diff has been lost."""
        self.failed_tracker_merge()
        workflow.evidence_path(self.root, self.parent).unlink()
        with self.assertRaisesRegex(AgentBoxError, "fresh validation.*review"):
            self.dispatch(["land", "7", "reconcile"])
        self.parent.labels = ["ab:tier:1"]
        self.config.workflow.review.policy = "auto"
        gw.git(self.root, "switch", "ab/7-parent")
        self.dispatch(["validate", "7", "--", "true"])
        with self.assertRaisesRegex(AgentBoxError, "PASS review"):
            self.dispatch(["land", "7", "merge"])
        report = {"verdict": "PASS", "summary": "Reviewed complete implementation", "findings": []}
        brief = Path(self.temporary.name) / "review-brief.md"
        brief.write_text("Changes/reasons: integrated implementation. Risks: recovered history. Validation: true passed; no live proof.\n")
        with patch.object(workflow, "route", return_value={}), \
             patch.object(workflow, "execute", return_value={"report": report}) as reviewer:
            self.dispatch(["review", "7", "--primary", "codex", "--brief", str(brief)])
        reviewer.assert_called_once()
        self.assertIn("review the complete implementation", reviewer.call_args.args[-1])
        result = self.dispatch(["land", "7", "merge"])
        self.assertEqual(result["state"], "Done")

    def test_tracker_failure_after_merge_is_recovered_without_merging_again(self):
        task_head = gw.git(self.root, "rev-parse", "ab/7-parent")
        self.adapter.set_state.side_effect = AgentBoxError("tracker unavailable")
        with self.assertRaisesRegex(AgentBoxError, "tracker unavailable"):
            self.operate("merge")
        self.assertEqual((gw.current_branch(self.root), gw.head_sha(self.root)),
                         ("main", task_head))
        self.adapter.set_state.side_effect = None
        result = self.operate("reconcile", saved=workflow.read_evidence(self.root, self.parent))
        self.assertEqual(result["merge"]["head"], task_head)

    def test_local_mode_rejects_pr_cloud_review_and_merge_methods_without_github(self):
        from agent_box import local_workflow
        with patch("agent_box.github_workflow.gh") as github:
            with self.assertRaisesRegex(AgentBoxError, "local merge mode"):
                local_workflow.operate(SimpleNamespace(command="pr"), self.root, self.config,
                                       self.adapter, self.parent)
            self.config.workflow.review.github_provider = "codex"
            with self.assertRaisesRegex(AgentBoxError, "Cloud review"):
                self.operate("request-review")
            with self.assertRaisesRegex(AgentBoxError, "--method"):
                self.operate("merge", method="squash")
        github.assert_not_called()


class LocalPushTests(unittest.TestCase):
    setUp = LocalLandTests.setUp
    operate = LocalLandTests.operate
    dispatch = LocalLandTests.dispatch

    def remote_head(self):
        return gw.git(Path(self.temporary.name) / "origin.git", "rev-parse", "main")

    def reject_push(self):
        hook = Path(self.temporary.name) / "origin.git/hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        return hook

    def test_enabled_publishes_exact_base_without_task_tags_or_github(self):
        """Local transport needs no GitHub integration and publishes only accepted main."""
        self.config.workflow.auto_push = True
        gw.git(self.root, "tag", "-a", "private-tag", "-m", "private")
        gw.git(self.root, "config", "push.followTags", "true")
        gw.git(self.root, "config", "push.recurseSubmodules", "on-demand")
        gw.git(self.root, "config", "remote.origin.mirror", "true")
        gw.git(self.root, "config", "remote.origin.push", "refs/heads/*:refs/heads/*")
        with patch("agent_box.github_workflow.gh") as github:
            result = self.operate("merge")
        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(self.remote_head(), gw.head_sha(self.root))
        refs = gw.git(Path(self.temporary.name) / "origin.git", "for-each-ref", "--format=%(refname)")
        self.assertEqual(refs, "refs/heads/main")
        github.assert_not_called()

    def test_disabled_leaves_remote_unchanged(self):
        """An explicit opt-out completes locally without publishing."""
        before = self.remote_head()
        result = self.operate("merge")
        self.assertEqual(result["publication"], {"status": "disabled", "pushed": False})
        self.assertEqual(self.remote_head(), before)
        self.adapter.set_state.assert_called_once()

    def test_rejection_preserves_review_and_task_then_reconcile_retries_without_merge(self):
        """A server rejection must retain recovery evidence and postpone cleanup and Done."""
        self.config.workflow.auto_push = True
        self.config.workflow.cleanup_local_branches = True
        hook = self.reject_push()
        with self.assertRaisesRegex(AgentBoxError, "(?s)Local merge succeeded.*land.*reconcile"):
            self.operate("merge")
        self.adapter.set_state.assert_not_called()
        self.assertEqual(gw.current_branch(self.root), "main")
        self.assertEqual(gw.local_branch_head(self.root, "ab/7-parent"), gw.head_sha(self.root))
        hook.unlink()
        with patch.object(gw, "git", wraps=gw.git) as git:
            self.operate("reconcile", saved=workflow.read_evidence(self.root, self.parent))
        self.assertFalse(any("merge" in call.args[1:] for call in git.call_args_list))
        self.assertEqual(self.remote_head(), gw.head_sha(self.root))
        self.assertEqual(gw.git(self.root, "branch", "--list", "ab/7-parent"), "")

    def test_reconcile_never_publishes_advanced_unvalidated_base(self):
        """Acceptance of the task cannot authorize unrelated later base commits."""
        self.config.workflow.auto_push = True
        hook = self.reject_push()
        with self.assertRaises(AgentBoxError):
            self.operate("merge")
        hook.unlink()
        gw.git(self.root, "commit", "--allow-empty", "-m", "unvalidated base work")
        before = self.remote_head()
        with self.assertRaisesRegex(AgentBoxError, "exact accepted head"):
            self.operate("reconcile", saved=workflow.read_evidence(self.root, self.parent))
        self.assertEqual(self.remote_head(), before)
        self.adapter.set_state.assert_not_called()

    def test_tracker_failure_after_publication_retries_already_published_head(self):
        """A successful push followed by a failed tracker write is idempotent."""
        self.config.workflow.auto_push = True
        self.adapter.set_state.side_effect = AgentBoxError("tracker unavailable")
        with self.assertRaisesRegex(AgentBoxError, "tracker unavailable"):
            self.operate("merge")
        self.reject_push()
        self.adapter.set_state.side_effect = None
        self.operate("reconcile", saved=workflow.read_evidence(self.root, self.parent))
        self.assertEqual(self.remote_head(), gw.head_sha(self.root))


    def test_live_default_change_and_unknown_remote_stop_before_done(self):
        """A stale cached default or unseen remote commit is never authority to push."""
        self.config.workflow.auto_push = True
        origin = Path(self.temporary.name) / "origin.git"
        gw.git(origin, "branch", "renamed", "main")
        gw.git(origin, "symbolic-ref", "HEAD", "refs/heads/renamed")
        before = self.remote_head()
        with self.assertRaisesRegex(AgentBoxError, "live default/base"):
            self.operate("merge")
        self.assertEqual(self.remote_head(), before)
        self.adapter.set_state.assert_not_called()
        gw.git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
        from test_workflow_handoff import clone
        other = clone(self.temporary.name)
        gw.git(other, "commit", "--allow-empty", "-m", "remote-only work")
        gw.git(other, "push", "origin", "main")
        with self.assertRaisesRegex(AgentBoxError, "unknown or diverged"):
            self.operate("reconcile", saved=workflow.read_evidence(self.root, self.parent))
        gw.git(self.root, "fetch", "origin")
        with self.assertRaisesRegex(AgentBoxError, "unknown or diverged"):
            self.operate("reconcile", saved=workflow.read_evidence(self.root, self.parent))
        self.adapter.set_state.assert_not_called()

    def test_unique_push_url_and_chained_rewrite_safety(self):
        """Read proof and publication must address one frozen destination."""
        self.config.workflow.auto_push = True
        origin = Path(self.temporary.name) / "origin.git"
        gw.git(self.root, "config", "--add", "remote.origin.pushurl", str(origin))
        gw.git(self.root, "config", "--add", "remote.origin.pushurl", str(origin))
        with self.assertRaisesRegex(AgentBoxError, "exactly one"):
            self.operate("merge")
        gw.git(self.root, "config", "--unset-all", "remote.origin.pushurl")
        gw.git(self.root, "config", "remote.origin.url", "publish-test:repo")
        gw.git(self.root, "config", "url." + str(origin) + ".pushInsteadOf", "publish-test:repo")
        gw.git(self.root, "config", "url.other-destination.insteadOf", str(origin))
        saved = workflow.read_evidence(self.root, self.parent)
        with self.assertRaisesRegex(AgentBoxError, "chained URL"):
            self.operate("reconcile", saved=saved)
        gw.git(self.root, "config", "--unset", "url.other-destination.insteadOf")
        self.operate("reconcile", saved=saved)
        self.assertEqual(self.remote_head(), gw.head_sha(self.root))

    def test_done_reconcile_never_publishes_later_base_work(self):
        """Done reconciliation is proof and optional cleanup, not a publication retry."""
        self.operate("merge")
        before = self.remote_head()
        self.parent.state = "Done"
        self.config.workflow.auto_push = True
        gw.git(self.root, "commit", "--allow-empty", "-m", "later unvalidated work")
        self.dispatch(["land", "7", "reconcile"])
        self.assertEqual(self.remote_head(), before)

    def test_explicit_opt_out_recovers_a_rejected_push_locally(self):
        """Changing policy to false intentionally permits local-only completion."""
        self.config.workflow.auto_push = True
        self.reject_push()
        before = self.remote_head()
        with self.assertRaises(AgentBoxError):
            self.operate("merge")
        self.config.workflow.auto_push = False
        self.operate("reconcile", saved=workflow.read_evidence(self.root, self.parent))
        self.assertEqual(self.remote_head(), before)
        self.adapter.set_state.assert_called_once()


    def test_symbolic_local_base_and_unsafe_destination_are_not_publication_targets(self):
        """Symbolic branches or option-like URLs must not redirect the accepted push."""
        head = gw.head_sha(self.root)
        gw.git(self.root, "switch", "main")
        gw.git(self.root, "merge", "--ff-only", head)
        gw.git(self.root, "symbolic-ref", "refs/heads/alias", "refs/heads/main")
        with patch.object(gw, "current_branch", return_value="alias"):
            with self.assertRaisesRegex(AgentBoxError, "symbolic"):
                gw.push_local_base(self.root, "alias", head)
        for url in ("--mirror", "other"):
            if url == "other":
                gw.git(self.root, "remote", "add", "other", str(Path(self.temporary.name) / "origin.git"))
            gw.git(self.root, "config", "remote.origin.pushurl", url)
            with self.assertRaisesRegex(AgentBoxError, "unsafe URL or remote alias"):
                gw.push_local_base(self.root, "main", head)

    def test_parent_push_still_rejects_base(self):
        """Local publication does not widen the task-branch push helper's contract."""
        gw.git(self.root, "switch", "main")
        with self.assertRaisesRegex(AgentBoxError, "parent branch"):
            gw.push_parent(self.root, "main", "main")


    def test_destination_change_during_remote_check_stops_publication(self):
        """A changed origin cannot silently receive proof obtained from its predecessor."""
        self.config.workflow.auto_push = True
        before = self.remote_head()
        original = gw._query_origin

        def change_destination(root, branch, url):
            result = original(root, branch, url)
            gw.git(root, "config", "remote.origin.pushurl", str(Path(self.temporary.name) / "other.git"))
            return result

        with patch.object(gw, "_query_origin", side_effect=change_destination):
            with self.assertRaisesRegex(AgentBoxError, "destination changed"):
                self.operate("merge")
        self.assertEqual(self.remote_head(), before)
        self.adapter.set_state.assert_not_called()


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
