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
from workflow_fixtures import BINDING, answer, candidate, repository, selected_route


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
        self.saved = {**workflow.evidence(self.root, self.parent, workflow.scope(self.root, "main"), "main"),
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

    def test_landing_a_parent_discards_its_raw_review_streams_only_once_it_is_done(self):
        """Streams explain open review rounds; a failed Done write still needs them, a landed parent does not."""
        from agent_box import review_results
        run = review_results.stream_run(workflow.WORKFLOW_STATE, workflow.repository_key(self.root), "7", "round")
        (run / "stdout").write_text("events\n")
        streams = run.parent
        other = review_results.stream_run(workflow.WORKFLOW_STATE, workflow.repository_key(self.root), "9", "round")
        self.adapter.set_state.side_effect = AgentBoxError("tracker unavailable")
        with self.assertRaisesRegex(AgentBoxError, "tracker unavailable"):
            self.operate("merge")
        self.assertTrue(streams.is_dir())
        self.adapter.set_state.side_effect = None
        self.assertEqual(self.operate("merge", saved=workflow.read_evidence(self.root, self.parent))["state"], "Done")
        self.assertFalse(streams.exists())
        self.assertTrue(other.is_dir())

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
                    workflow.start_review(root, BINDING, self.parent, 3,
                                            "new-round", self.saved["scope"], "managed")
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
                workflow.start_review(self.root, BINDING, self.parent, 3,
                                        "validation-round", self.saved["scope"], "managed")
                return SimpleNamespace(returncode=0)
            return original_run(command, *args, **kwargs)

        with patch.object(workflow.subprocess, "run", run):
            result = self.dispatch(["validate", "7", "--", "validation-fixture"])
        self.assertEqual(result["validation"]["exit"], 0)
        self.assertNotIn("review", result)
        self.assertNotIn("review", workflow.read_evidence(self.root, self.parent))

    def test_the_project_command_keeps_its_own_options_after_the_separator(self):
        """An option after -- belongs to the project command; without -- it cannot be told from a workflow option."""
        commands = []
        original_run = workflow.subprocess.run

        def run(command, *args, **kwargs):
            if command[:1] == ["validation-fixture"]:
                commands.append(command)
                return SimpleNamespace(returncode=0)
            return original_run(command, *args, **kwargs)

        with patch.object(workflow.subprocess, "run", run):
            result = self.dispatch(["validate", "7", "--", "validation-fixture", "--help"])
            self.assertEqual(result["validation"]["exit"], 0)
            self.assertEqual(commands, [["validation-fixture", "--help"]])
            with self.assertRaisesRegex(AgentBoxError, r"after --: agent-box workflow validate ID -- COMMAND"):
                self.dispatch(["validate", "7", "validation-fixture", "--verbose"])
        self.assertEqual(len(commands), 1)

    def test_landing_keeps_reservation_out_until_acceptance_is_consumed(self):
        """The other ordering allows landing and rejects a concurrent new reservation."""
        from agent_box import local_workflow

        self.config.workflow.review.policy = "always"
        self.saved["review"] = {"verdict": "PASS"}
        workflow.save_evidence(self.root, self.parent, self.saved)
        original_publish = local_workflow._publish

        def publish(*args):
            with self.assertRaisesRegex(AgentBoxError, "Another local review-budget"):
                workflow.start_review(self.root, BINDING, self.parent, 3,
                                        "too-late", self.saved["scope"], "managed")
            return original_publish(*args)

        with patch.object(local_workflow, "_publish", publish):
            result = self.dispatch(["land", "7", "merge"])
        self.assertEqual(result["state"], "Done")
        self.assertEqual(workflow.read_review_budget(self.root, BINDING, self.parent, 3)["used"], 0)

    def test_a_repeated_merge_rereads_acceptance_revoked_by_a_new_round(self):
        """A reservation after dispatch retires the acceptance the merged head rests on."""

        self.config.workflow.review.policy = "always"
        self.saved["review"] = {"verdict": "PASS"}
        self.adapter.set_state.side_effect = AgentBoxError("tracker unavailable")
        with self.assertRaisesRegex(AgentBoxError, "tracker unavailable"):
            self.operate("merge")
        self.adapter.set_state.side_effect = None
        self.adapter.set_state.reset_mock()
        original_landing = workflow.landing

        def interleave(*args):
            workflow.start_review(self.root, BINDING, self.parent, 3,
                                    "recovery-round", workflow.scope(self.root, "main"), "managed")
            return original_landing(*args)

        with patch.object(workflow, "landing", interleave), \
             self.assertRaisesRegex(AgentBoxError, "No acceptance is recorded"):
            self.dispatch(["land", "7", "merge"])
        self.adapter.set_state.assert_not_called()

    def test_github_refreshes_local_acceptance_but_retains_current_head_remote_approval(self):
        """A new local round removes cached PASS without invalidating GitHub's own approval."""
        from agent_box import github_review, github_workflow as github

        self.config.workflow.merge_mode = "github"
        self.config.workflow.review.policy = "always"
        self.config.github.gh_token = True
        self.saved["review"] = {"verdict": "PASS"}
        head = gw.head_sha(self.root)
        opened = {"number": 9, "url": "https://github.com/o/r/pull/9", "state": "OPEN",
                  "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                  "headRefOid": head, "headRefName": self.info["branch"], "baseRefName": "main",
                  "statusCheckRollup": []}
        original_landing = workflow.landing

        def interleave(*args):
            workflow.start_review(self.root, BINDING, self.parent, 3,
                                    "github-round", self.saved["scope"], "managed")
            return original_landing(*args)

        for approved in (False, True):
            workflow.save_evidence(self.root, self.parent, self.saved)
            reviews = [{"user": {"login": "maintainer", "type": "User"},
                        "state": "APPROVED", "commit_id": head}] if approved else []
            with self.subTest(approved=approved), patch.object(workflow, "landing", interleave), \
                 patch.object(github_review, "paginate", return_value=reviews), \
                 patch.object(gw, "github_origin", return_value="o/r"), \
                 patch.object(github, "find_pr", return_value=9), \
                 patch.object(github, "threads", return_value=[]), \
                 patch.object(github, "view", return_value=opened), patch.object(github, "gh") as gh:
                if approved:
                    self.dispatch(["land", "7", "merge"])
                    self.assertEqual(gh.call_args.args[1:3], ("pr", "merge"))
                else:
                    with self.assertRaisesRegex(AgentBoxError, "fresh independent PASS"):
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
        return self.dispatch(["approve", "7", "--reason", reason])

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

    def test_human_approval_requires_a_reason_and_validation_of_the_current_head(self):
        """A vague acceptance or an unvalidated head cannot acquire a review exception."""
        workflow.save_evidence(self.root, self.parent, self.saved)
        with self.assertRaisesRegex(AgentBoxError, "reason"):
            self.approve("   ")
        self.saved.pop("validation")
        with self.assertRaisesRegex(AgentBoxError, "validation"):
            self.approve()
        self.adapter.set_state.assert_not_called()

    def test_human_acceptance_expires_on_new_work_and_does_not_approve_another_scope(self):
        """An accepted commit does not authorize later edits, even after validation."""
        self.config.workflow.review.policy = "always"
        self.approve()
        gw.git(self.root, "commit", "--allow-empty", "-m", "new work")
        saved = workflow.evidence(self.root, self.parent, workflow.scope(self.root, "main"))
        saved["validation"] = {"exit": 0}
        workflow.save_evidence(self.root, self.parent, saved)
        with self.assertRaisesRegex(AgentBoxError, "PASS review"):
            self.dispatch(["land", "7", "merge"])
        self.adapter.set_state.assert_not_called()

    def test_human_acceptance_recovery_preserves_original_scope_after_tracker_failure(self):
        """The same approval survives base advancement and later card edits."""
        self.config.workflow.review.policy = "always"
        self.approve()
        self.adapter.set_state.side_effect = AgentBoxError("tracker unavailable")
        with self.assertRaisesRegex(AgentBoxError, "tracker unavailable"):
            self.dispatch(["land", "7", "merge"])
        self.adapter.set_state.side_effect = None
        self.adapter.set_state.reset_mock()
        result = self.dispatch(["land", "7", "merge"])
        self.assertEqual(result["state"], "Done")
        self.parent.description += "\n- [x] clarified requirement"
        self.assertEqual(self.dispatch(["land", "7", "merge"])["state"], "Done")

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

    def test_human_acceptance_replaces_the_parent_review_in_github_mode_too(self):
        """The user's explicit decision closes a looping review in either merge mode."""
        self.config.workflow.review.policy = "always"
        self.parent.state = "Doing"
        self.config.workflow.merge_mode = "github"
        workflow.save_evidence(self.root, self.parent, self.saved)
        with self.assertRaisesRegex(AgentBoxError, "PASS review"):
            self.dispatch(["finish", "7"])
        self.approve()
        self.assertEqual(self.dispatch(["finish", "7"])["state"], "Review")

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

    def test_merge_refuses_ancestry_without_acceptance_even_for_an_unchanged_branch(self):
        """Integration alone must never manufacture validation or a PASS review."""
        gw.git(self.root, "switch", "main")
        gw.git(self.root, "branch", "-f", "ab/7-parent", "main")
        self.config.workflow.review.policy = "always"
        with self.assertRaisesRegex(AgentBoxError, "No acceptance is recorded"):
            self.operate("merge", saved={})
        self.adapter.set_state.assert_not_called()
        self.assertEqual(self.parent.state, "Review")

    def test_merge_refuses_a_raw_manual_merge_without_acceptance(self):
        """Manual Git inclusion is not evidence of workflow acceptance."""
        task_head = gw.git(self.root, "rev-parse", "ab/7-parent")
        gw.git(self.root, "switch", "main")
        gw.git(self.root, "merge", "--ff-only", task_head)
        with self.assertRaisesRegex(AgentBoxError, "No acceptance is recorded"):
            self.operate("merge", saved={})
        self.adapter.set_state.assert_not_called()

    def failed_tracker_merge(self):
        self.adapter.set_state.side_effect = AgentBoxError("tracker unavailable")
        with self.assertRaisesRegex(AgentBoxError, "tracker unavailable"):
            self.operate("merge")
        self.adapter.set_state.side_effect = None
        self.adapter.set_state.reset_mock()
        return workflow.read_evidence(self.root, self.parent)

    def test_merge_rejects_a_changed_head_even_after_the_new_work_is_integrated(self):
        """A repeated merge cannot reuse acceptance of the previous task tip."""
        accepted = self.failed_tracker_merge()
        gw.git(self.root, "switch", "ab/7-parent")
        gw.git(self.root, "commit", "--allow-empty", "-m", "new work")
        for _ in range(2):
            with self.assertRaisesRegex(AgentBoxError, "(?s)Cannot merge locally.*workflow validate"):
                self.operate("merge", saved=accepted)
            gw.git(self.root, "switch", "main")
            gw.git(self.root, "merge", "--ff-only", "ab/7-parent")
        self.adapter.set_state.assert_not_called()
        self.assertEqual(self.parent.state, "Review")

    def test_merge_rejects_evidence_that_was_lost_after_the_merge(self):
        """Without acceptance recorded for it, the merged head is not completed."""
        self.failed_tracker_merge()
        with self.assertRaisesRegex(AgentBoxError, "No acceptance is recorded.*workflow validate"):
            self.operate("merge", saved={})
        self.adapter.set_state.assert_not_called()
        self.assertEqual(self.parent.state, "Review")

    def test_a_repeated_merge_reuses_the_recorded_acceptance_after_card_edits(self):
        """Links, labels, a tier or a new checked item cannot demand another review after a tracker outage."""
        accepted = self.failed_tracker_merge()
        self.parent.related.append("99")
        self.parent.labels = ["ordinary", "ab:tier:3"]
        self.parent.description += "\n- [x] clarified requirement"
        with patch.object(workflow, "execute") as review, patch.object(workflow, "scope") as scope:
            result = self.operate("merge", saved=accepted)
        self.assertEqual(result["state"], "Done")
        review.assert_not_called()
        scope.assert_not_called()

    def test_a_repeated_merge_applies_the_current_review_policy_to_the_original_diff(self):
        """After merge the base diff is empty, but auto review still covers original work."""
        self.saved["review"] = {"verdict": "SKIPPED"}
        accepted = self.failed_tracker_merge()
        for policy in ("always", "auto"):
            self.config.workflow.review.policy = policy
            self.config.workflow.review.max_files = 1
            with self.assertRaisesRegex(AgentBoxError, "PASS review"):
                self.operate("merge", saved=accepted)
        self.adapter.set_state.assert_not_called()

    def test_a_repeated_merge_accepts_a_real_review_under_always_and_auto_policies(self):
        """A saved PASS retains its scope across the base fast-forward."""
        self.config.workflow.review.policy = "always"
        self.saved["review"] = {"verdict": "PASS", "summary": "Independent review"}
        accepted = self.failed_tracker_merge()
        for policy in ("always", "auto", "off"):
            self.config.workflow.review.policy = policy
            result = self.operate("merge", saved=accepted)
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
        result = self.dispatch(["land", "7", "merge"])
        self.assertEqual(result["state"], "Done")

    def test_snapshot_write_failure_stops_before_git_mutation(self):
        """A tracker failure must never strand a merge without its acceptance snapshot."""
        with patch.object(workflow, "save_evidence", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.operate("merge")
        self.assertEqual(gw.current_branch(self.root), "ab/7-parent")
        self.adapter.set_state.assert_not_called()

    def test_recorded_acceptance_must_reproduce_its_own_scope(self):
        """Acceptance names two commits; an edited pair describes a diff nobody accepted."""
        accepted = self.failed_tracker_merge()
        for key in ("head", "base_head"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(AgentBoxError, "No acceptance is recorded"):
                    self.operate("merge", saved={**accepted, key: "f" * 40})
        self.adapter.set_state.assert_not_called()

    def test_acceptance_is_not_a_merge_registry_and_a_rewound_base_is_merged_again(self):
        """Acceptance survives a failed tracker write; inclusion is always read from Git."""
        original_base = gw.local_branch_head(self.root, "main")
        accepted = self.failed_tracker_merge()
        gw.git(self.root, "reset", "--hard", original_base)
        gw.git(self.root, "switch", "ab/7-parent")
        result = self.operate("merge", saved=accepted)
        self.assertEqual((result["state"], result["merge"]["head"]), ("Done", gw.head_sha(self.root)))
        self.assertEqual(gw.local_branch_head(self.root, "main"), accepted["head"])

    def test_merge_rechecks_the_branch_after_the_acceptance_checks(self):
        """Concurrent task work cannot be completed using the earlier accepted head."""
        from agent_box import local_workflow
        accepted = self.failed_tracker_merge()
        check = local_workflow.acceptance_reasons if hasattr(local_workflow, "acceptance_reasons") else None
        self.assertIsNone(check, "acceptance reasons are imported inside operate, not module state")
        original = workflow.acceptance_reasons

        def move_head(*args, **kwargs):
            reasons = original(*args, **kwargs)
            gw.git(self.root, "commit", "--allow-empty", "-m", "concurrent work")
            gw.git(self.root, "branch", "-f", "ab/7-parent", "main")
            return reasons

        with patch.object(workflow, "acceptance_reasons", side_effect=move_head):
            with self.assertRaisesRegex(AgentBoxError, "moved after acceptance was checked"):
                self.operate("merge", saved=accepted)
        self.adapter.set_state.assert_not_called()

    def test_off_policy_recovery_needs_validation_and_acceptance_of_the_absorbed_work(self):
        """Turning off review never turns off validation, and an empty diff still proves nothing."""
        self.failed_tracker_merge()
        workflow.evidence_path(self.root, self.parent).unlink()
        with self.assertRaisesRegex(AgentBoxError, "workflow validate"):
            self.dispatch(["land", "7", "merge"])
        gw.git(self.root, "switch", "ab/7-parent")
        self.dispatch(["validate", "7", "--", "true"])
        with self.assertRaisesRegex(AgentBoxError, "original work is unproven"):
            self.dispatch(["land", "7", "merge"])
        self.dispatch(["approve", "7", "--reason", "User accepted the merged implementation"])
        self.assertEqual(self.dispatch(["land", "7", "merge"])["state"], "Done")

    def test_missing_cache_can_recover_with_fresh_validation_and_independent_review(self):
        """Auto must request an actual review when the original diff has been lost."""
        self.failed_tracker_merge()
        workflow.evidence_path(self.root, self.parent).unlink()
        with self.assertRaisesRegex(AgentBoxError, "workflow validate"):
            self.dispatch(["land", "7", "merge"])
        self.config.workflow.review.models = [candidate("claude", "m", "high")]
        self.config.workflow.review.policy = "auto"
        gw.git(self.root, "switch", "ab/7-parent")
        self.dispatch(["validate", "7", "--", "true"])
        with self.assertRaisesRegex(AgentBoxError, "original work is unproven"):
            self.dispatch(["land", "7", "merge"])
        report = answer("Verdict: PASS\n\nReviewed complete implementation.\n")
        brief = Path(self.temporary.name) / "review-brief.md"
        brief.write_text("Changes/reasons: integrated implementation. Risks: recovered history. Validation: true passed; no live proof.\n")
        with patch.object(workflow, "candidates", side_effect=selected_route), \
             patch.object(workflow.shutil, "which", return_value="provider-cli"), \
             patch.object(workflow, "execute", return_value={"report": report, "route": {"provider": "claude", "model": "m", "effort": "high", "tier": 1}}) as reviewer:
            self.dispatch(["review", "7", "--brief", str(brief)])
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
        result = self.operate("merge", saved=workflow.read_evidence(self.root, self.parent))
        self.assertEqual(result["merge"]["head"], task_head)

    def test_local_mode_rejects_pull_requests_and_merge_methods_without_github(self):
        """Local landing is fast-forward only, and a cloud reviewer has no pull request to answer in."""
        from agent_box import local_workflow
        with patch("agent_box.github_workflow.gh") as github:
            with self.assertRaisesRegex(AgentBoxError, "local merge mode"):
                local_workflow.operate(SimpleNamespace(command="pr"), self.root, self.config,
                                       self.adapter, self.parent)
            self.config.workflow.review.github_provider = "codex"
            self.assertFalse(workflow.cloud_review(self.config, self.parent, self.parent))
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
        self.assertEqual(gw.git(self.root, "rev-parse", "refs/remotes/origin/main"), self.remote_head())
        self.assertEqual(result["publication"]["tracking"]["status"], "updated")
        refs = gw.git(Path(self.temporary.name) / "origin.git", "for-each-ref", "--format=%(refname)")
        self.assertEqual(refs, "refs/heads/main")
        github.assert_not_called()

    def test_fetch_mapping_and_rewrites_select_only_corresponding_tracking_ref(self):
        """A custom fetch mapping, including URL rewrites, is the tracking authority."""
        self.config.workflow.auto_push = True
        origin = str(Path(self.temporary.name) / "origin.git")
        before = gw.git(self.root, "rev-parse", "refs/remotes/origin/main")
        gw.git(self.root, "config", "remote.origin.url", "tracking-test:repo")
        gw.git(self.root, "config", f"url.{origin}.insteadOf", "tracking-test:repo")
        gw.git(self.root, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/custom/*")
        result = self.operate("merge")
        self.assertEqual(result["publication"]["tracking"]["ref"], "refs/remotes/custom/main")
        self.assertEqual(gw.git(self.root, "rev-parse", "refs/remotes/custom/main"), self.remote_head())
        self.assertEqual(gw.git(self.root, "rev-parse", "refs/remotes/origin/main"), before)

    def test_distinct_fetch_destination_does_not_receive_published_sha(self):
        """Push-only URL rewriting must not claim the fetch repository was updated."""
        self.config.workflow.auto_push = True
        before = gw.git(self.root, "rev-parse", "refs/remotes/origin/main")
        origin = str(Path(self.temporary.name) / "origin.git")
        gw.git(self.root, "config", "remote.origin.url", "push-only:repo")
        gw.git(self.root, "config", f"url.{origin}.pushInsteadOf", "push-only:repo")
        result = self.operate("merge")
        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(result["publication"]["tracking"]["status"], "skipped")
        self.assertIn("destinations", result["publication"]["tracking"]["reason"])
        self.assertEqual(gw.git(self.root, "rev-parse", "refs/remotes/origin/main"), before)
        self.assertEqual(self.remote_head(), gw.head_sha(self.root))

    def test_excluded_ambiguous_and_local_fetch_mappings_are_skipped(self):
        """Negative, ambiguous and non-tracking mappings cannot widen local writes."""
        self.config.workflow.auto_push = True
        for spec in ("^refs/heads/main", "+refs/heads/main:refs/remotes/other/main",
                     "+refs/heads/main:refs/heads/unrelated"):
            with self.subTest(spec=spec):
                gw.git(self.root, "config", "--add", "remote.origin.fetch", spec)
                head = gw.head_sha(self.root)
                gw.git(self.root, "switch", "main")
                gw.git(self.root, "merge", "--ff-only", head)
                before = gw.git(self.root, "for-each-ref")
                result = gw.push_local_base(self.root, "main", head)
                self.assertEqual(result["tracking"]["status"], "skipped")
                self.assertEqual(gw.git(self.root, "for-each-ref"), before)
                gw.git(self.root, "config", "--unset-all", "remote.origin.fetch")
                gw.git(self.root, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")

    def test_concurrent_tracking_change_survives_successful_publication(self):
        """A concurrent fetch wins over the pre-push snapshot, even after push succeeds."""
        self.config.workflow.auto_push = True
        original = gw._sync_tracking
        old = gw.git(self.root, "rev-parse", "refs/remotes/origin/main")
        gw.git(self.root, "commit", "--allow-empty", "-m", "later task work")
        concurrent = gw.head_sha(self.root)
        gw.git(self.root, "reset", "--soft", "HEAD~1")

        def race(root, base, url, head, snapshot):
            gw.git(root, "update-ref", "refs/remotes/origin/main", concurrent, old)
            return original(root, base, url, head, snapshot)

        with patch.object(gw, "_sync_tracking", side_effect=race):
            result = self.operate("merge")
        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(result["publication"]["tracking"]["status"], "failed")
        self.assertEqual(gw.git(self.root, "rev-parse", "refs/remotes/origin/main"), concurrent)
        self.adapter.set_state.assert_called_once()

    def test_symbolic_replacement_during_publication_is_preserved(self):
        """OID CAS must not replace a new symref whose target still has the old OID."""
        self.config.workflow.auto_push = True
        original = gw._update_tracking_ref
        gw.git(self.root, "branch", "unrelated", "main")
        before = gw.local_branch_head(self.root, "unrelated")

        def race(root, target, head, old):
            gw.git(root, "symbolic-ref", target, "refs/heads/unrelated")
            return original(root, target, head, old)

        with patch.object(gw, "_update_tracking_ref", side_effect=race):
            result = self.operate("merge")
        self.assertEqual(result["publication"]["tracking"]["status"], "failed")
        self.assertEqual(gw.git(self.root, "symbolic-ref", "refs/remotes/origin/main"), "refs/heads/unrelated")
        self.assertEqual(gw.local_branch_head(self.root, "unrelated"), before)
        self.assertFalse((self.root / ".git/refs/remotes/origin/main.lock").exists())
        self.assertEqual(self.remote_head(), gw.head_sha(self.root))

    def test_fetch_configuration_change_during_publication_skips_local_write(self):
        """A new fetch mapping cannot inherit proof bound to the previous mapping."""
        self.config.workflow.auto_push = True
        original = gw._sync_tracking
        before = gw.git(self.root, "rev-parse", "refs/remotes/origin/main")

        def race(root, base, url, head, snapshot):
            gw.git(root, "config", "remote.origin.fetch", "+refs/heads/main:refs/remotes/new/main")
            return original(root, base, url, head, snapshot)

        with patch.object(gw, "_sync_tracking", side_effect=race):
            result = self.operate("merge")
        self.assertEqual(result["publication"]["tracking"]["status"], "failed")
        self.assertEqual(gw.git(self.root, "rev-parse", "refs/remotes/origin/main"), before)
        self.assertEqual(gw.git(self.root, "for-each-ref", "refs/remotes/new"), "")

    def test_fetch_mapping_shared_with_other_source_is_ambiguous(self):
        """One destination shared by different source branches has no unique identity."""
        self.config.workflow.auto_push = True
        gw.git(self.root, "config", "--add", "remote.origin.fetch", "+refs/heads/other:refs/remotes/origin/main")
        before = gw.git(self.root, "rev-parse", "refs/remotes/origin/main")
        result = self.operate("merge")
        self.assertEqual(result["publication"]["tracking"]["status"], "skipped")
        self.assertIn("another branch", result["publication"]["tracking"]["reason"])
        self.assertEqual(gw.git(self.root, "rev-parse", "refs/remotes/origin/main"), before)

    def test_symbolic_tracking_ref_does_not_mutate_its_target(self):
        """A symbolic tracking name is not permission to update another local branch."""
        self.config.workflow.auto_push = True
        gw.git(self.root, "branch", "unrelated", "main")
        before = gw.local_branch_head(self.root, "unrelated")
        gw.git(self.root, "symbolic-ref", "refs/remotes/origin/main", "refs/heads/unrelated")
        result = self.operate("merge")
        self.assertEqual(result["publication"]["tracking"]["status"], "skipped")
        self.assertEqual(gw.local_branch_head(self.root, "unrelated"), before)
        self.assertEqual(gw.git(self.root, "symbolic-ref", "refs/remotes/origin/main"), "refs/heads/unrelated")

    def test_tracking_lock_failure_does_not_retry_push_or_block_done(self):
        """Local ref lock failure is diagnostic, not a remote publication failure."""
        self.config.workflow.auto_push = True
        before = gw.git(self.root, "rev-parse", "refs/remotes/origin/main")
        lock = self.root / ".git/refs/remotes/origin/main.lock"
        lock.write_text("another process owns this lock\n")
        with patch.object(gw, "git", wraps=gw.git) as git:
            result = self.operate("merge")
        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(result["publication"]["tracking"]["status"], "failed")
        self.assertEqual(sum("push" in call.args[1:] for call in git.call_args_list), 1)
        self.assertEqual(gw.git(self.root, "rev-parse", "refs/remotes/origin/main"), before)
        self.assertEqual(self.remote_head(), gw.head_sha(self.root))
        self.adapter.set_state.assert_called_once()

    def test_disabled_leaves_remote_unchanged(self):
        """An explicit opt-out completes locally without publishing."""
        before = self.remote_head()
        result = self.operate("merge")
        self.assertEqual(result["publication"], {"status": "disabled", "pushed": False})
        self.assertEqual(self.remote_head(), before)
        self.adapter.set_state.assert_called_once()

    def test_rejection_preserves_review_and_task_then_a_repeat_publishes_without_merging(self):
        """A server rejection must retain recovery evidence and postpone cleanup and Done."""
        self.config.workflow.auto_push = True
        self.config.workflow.cleanup_local_branches = True
        hook = self.reject_push()
        with self.assertRaisesRegex(AgentBoxError, "(?s)Local merge succeeded.*land 7 merge"):
            self.operate("merge")
        self.adapter.set_state.assert_not_called()
        self.assertEqual(gw.current_branch(self.root), "main")
        self.assertEqual(gw.local_branch_head(self.root, "ab/7-parent"), gw.head_sha(self.root))
        hook.unlink()
        with patch.object(gw, "git", wraps=gw.git) as git:
            self.operate("merge", saved=workflow.read_evidence(self.root, self.parent))
        self.assertFalse(any("merge" in call.args[1:] for call in git.call_args_list))
        self.assertEqual(self.remote_head(), gw.head_sha(self.root))
        self.assertEqual(gw.git(self.root, "branch", "--list", "ab/7-parent"), "")

    def test_a_repeated_merge_never_publishes_advanced_unvalidated_base(self):
        """Acceptance of the task cannot authorize unrelated later base commits."""
        self.config.workflow.auto_push = True
        hook = self.reject_push()
        with self.assertRaises(AgentBoxError):
            self.operate("merge")
        hook.unlink()
        gw.git(self.root, "commit", "--allow-empty", "-m", "unvalidated base work")
        before = self.remote_head()
        with self.assertRaisesRegex(AgentBoxError, "exact accepted head"):
            self.operate("merge", saved=workflow.read_evidence(self.root, self.parent))
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
        self.operate("merge", saved=workflow.read_evidence(self.root, self.parent))
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
        from workflow_fixtures import clone
        other = clone(self.temporary.name)
        gw.git(other, "commit", "--allow-empty", "-m", "remote-only work")
        gw.git(other, "push", "origin", "main")
        with self.assertRaisesRegex(AgentBoxError, "unknown or diverged"):
            self.operate("merge", saved=workflow.read_evidence(self.root, self.parent))
        gw.git(self.root, "fetch", "origin")
        with self.assertRaisesRegex(AgentBoxError, "unknown or diverged"):
            self.operate("merge", saved=workflow.read_evidence(self.root, self.parent))
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
            self.operate("merge", saved=saved)
        gw.git(self.root, "config", "--unset", "url.other-destination.insteadOf")
        self.operate("merge", saved=saved)
        self.assertEqual(self.remote_head(), gw.head_sha(self.root))

    def test_a_merge_of_a_done_task_never_publishes_later_base_work(self):
        """A completed task has only cleanup left; it is not a publication retry."""
        self.operate("merge")
        before = self.remote_head()
        self.parent.state = "Done"
        self.config.workflow.auto_push = True
        gw.git(self.root, "commit", "--allow-empty", "-m", "later unvalidated work")
        self.dispatch(["land", "7", "merge"])
        self.assertEqual(self.remote_head(), before)

    def test_explicit_opt_out_recovers_a_rejected_push_locally(self):
        """Changing policy to false intentionally permits local-only completion."""
        self.config.workflow.auto_push = True
        self.reject_push()
        before = self.remote_head()
        with self.assertRaises(AgentBoxError):
            self.operate("merge")
        self.config.workflow.auto_push = False
        self.operate("merge", saved=workflow.read_evidence(self.root, self.parent))
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
                accepted = {"scope": workflow.scope(root, "main"),
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
