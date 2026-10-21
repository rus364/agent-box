"""A prior PASS cannot authorize landing work that has since been reopened."""
import unittest
from agent_box import workflow
from agent_box.tracker.core import Task
from workflow_fixtures import LandingCase


def approval(commit, *, login="maintainer", kind="User"):
    """One entry of the reviews endpoint: a person's APPROVED review of that commit."""
    return {"user": {"login": login, "type": kind}, "state": "APPROVED", "commit_id": commit}


class MergeAcceptanceTests(unittest.TestCase):
    def setUp(self):
        # These unit tests isolate PR/ref identity; scope acceptance, which reads
        # the real diff, is exercised below against temporary Git repositories.
        from unittest.mock import patch
        for name, value in (("scope", "current"),
                            ("evidence", {"scope": "current", "validation": {"exit": 0},
                                          "review": {"verdict": "PASS"}}),
                            ("integrated_parent", False), ("diff_stats", ([], 0))):
            patcher = patch.object(workflow, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_pull_request_is_looked_up_on_github_not_read_from_a_comment(self):
        """A stored number let tracker text decide which PR the workflow lands."""
        from unittest.mock import patch
        from pathlib import Path
        from agent_box import github_workflow as github
        from agent_box.errors import AgentBoxError
        rows = [{"number": 9, "state": "OPEN"}, {"number": 4, "state": "CLOSED"}]
        with patch.object(github, "gh", return_value=rows) as command:
            self.assertEqual(github.find_pr(Path("/tmp"), "o/r", "ab/task", "main"), 9)
        arguments = command.call_args.args
        self.assertIn("--head", arguments)
        self.assertEqual(arguments[arguments.index("--head") + 1], "ab/task")
        with patch.object(github, "gh", return_value=[{"number": 12, "state": "MERGED"}]):
            self.assertEqual(github.find_pr(Path("/tmp"), "o/r", "ab/task", "main"), 12)
        with patch.object(github, "gh", return_value=[{"number": 4, "state": "CLOSED"}]):
            self.assertIsNone(github.find_pr(Path("/tmp"), "o/r", "ab/task", "main"))
        with patch.object(github, "gh", return_value=[{"number": 9, "state": "OPEN"},
                                                      {"number": 10, "state": "OPEN"}]):
            with self.assertRaisesRegex(AgentBoxError, "Several pull requests"):
                github.find_pr(Path("/tmp"), "o/r", "ab/task", "main")

    def test_an_old_merged_pr_cannot_complete_work_added_after_it(self):
        """A reused branch keeps matching by head and base long after its merge."""
        from unittest.mock import Mock, patch
        from types import SimpleNamespace
        from pathlib import Path
        from agent_box import github_workflow as github
        from agent_box.config import Config
        from agent_box.errors import AgentBoxError
        parent = Task("1", "1", "Parent", state="In review", assignees=["1"], description="- [x] Accepted")
        adapter = Mock()
        adapter.identity.return_value = {"id": 1}
        adapter.get.side_effect = lambda ident: {"1": parent}[ident]
        config = Config()
        config.credentials.github.token = True
        merged = {"number": 9, "url": "https://github.com/o/r/pull/9", "state": "MERGED",
                  "isDraft": False, "headRefOid": "oldhead", "headRefName": "ab/task",
                  "baseRefName": "main", "mergedAt": "2026-01-01T00:00:00Z",
                  "mergeCommit": {"oid": "mergecommit"}, "statusCheckRollup": []}
        info = {"branch": "ab/task", "base": "main"}
        args = SimpleNamespace(command="land", action="merge", merged=True, method="squash")
        with patch.object(github.gw, "github_origin", return_value="o/r"), \
             patch.object(github.gw, "current_branch", return_value="ab/task"), \
             patch.object(github.gw, "require_clean"), \
             patch.object(github, "view", return_value=merged), \
             patch.object(github, "find_pr", return_value=9), \
             patch.object(github, "gh") as command:
            with patch.object(github.gw, "head_sha", return_value="newhead"):
                with self.assertRaisesRegex(AgentBoxError, "not merged"):
                    github._operate(args, Path("/tmp"), config, adapter, parent, parent, info, {}, [parent])
                adapter.set_state.assert_not_called()
                command.assert_not_called()
            # The normal reconcile after a real merge still completes the task.
            with patch.object(github.gw, "head_sha", return_value="oldhead"), \
                 patch.object(github.gw, "remote_branch_head", return_value="oldhead"):
                result = github._operate(args, Path("/tmp"), config, adapter, parent, parent, info, {}, [parent])
        self.assertEqual((result["state"], result["merge"]), ("Done", {"oid": "mergecommit"}))
        adapter.set_state.assert_called_once_with(parent, "Done")

    def test_an_old_merged_pr_cannot_complete_work_another_machine_pushed(self):
        """This checkout may simply not have fetched what someone else published.

        Head equality against the local branch is not enough once a task branch
        can be continued from a second clone: the branch that origin holds now,
        asked live, is what decides whether the merged commit is still the work.
        """
        from unittest.mock import Mock, patch
        from types import SimpleNamespace
        from pathlib import Path
        from agent_box import github_workflow as github
        from agent_box.config import Config
        from agent_box.errors import AgentBoxError
        parent = Task("1", "1", "Parent", state="In review", assignees=["1"], description="- [x] Accepted")
        adapter = Mock()
        adapter.identity.return_value = {"id": 1}
        adapter.get.side_effect = lambda ident: {"1": parent}[ident]
        config = Config()
        config.credentials.github.token = True
        merged = {"number": 9, "url": "https://github.com/o/r/pull/9", "state": "MERGED",
                  "isDraft": False, "headRefOid": "A", "headRefName": "ab/task",
                  "baseRefName": "main", "mergedAt": "2026-01-01T00:00:00Z",
                  "mergeCommit": {"oid": "mergecommit"}, "statusCheckRollup": []}
        info = {"branch": "ab/task", "base": "main"}
        args = SimpleNamespace(command="land", action="merge", merged=True, method="squash")
        with patch.object(github.gw, "github_origin", return_value="o/r"), \
             patch.object(github.gw, "current_branch", return_value="ab/task"), \
             patch.object(github.gw, "require_clean"), \
             patch.object(github.gw, "head_sha", return_value="A"), \
             patch.object(github, "view", return_value=merged), \
             patch.object(github, "find_pr", return_value=9), \
             patch.object(github, "gh") as command:
            with patch.object(github.gw, "remote_branch_head", return_value="B") as remote:
                with self.assertRaisesRegex(AgentBoxError, "another checkout advanced the branch"):
                    github._operate(args, Path("/tmp"), config, adapter, parent, parent, info, {}, [parent])
                remote.assert_called_once_with(Path("/tmp"), "ab/task")
                adapter.set_state.assert_not_called()
                command.assert_not_called()
            with patch.object(github.gw, "remote_branch_head", return_value="A"):
                result = github._operate(args, Path("/tmp"), config, adapter, parent, parent, info, {}, [parent])
        self.assertEqual(result["state"], "Done")
        adapter.set_state.assert_called_once_with(parent, "Done")

    def test_reconcile_completes_a_merge_whose_head_branch_github_deleted(self):
        """Auto-deleting the head branch on merge is normal, not a reason to refuse.

        Reconcile exists for the run that merged and then failed to record Done,
        by which time GitHub may already have removed the branch. Only a branch
        that still exists and has moved past the merged commit contradicts the
        merge; a branch that cannot be read at all still refuses, because then
        nothing rules that out.
        """
        from unittest.mock import Mock, patch
        from types import SimpleNamespace
        from pathlib import Path
        from agent_box import github_workflow as github
        from agent_box.config import Config
        from agent_box.errors import AgentBoxError
        parent = Task("1", "1", "Parent", state="In review", assignees=["1"], description="- [x] Accepted")
        adapter = Mock()
        adapter.identity.return_value = {"id": 1}
        adapter.get.side_effect = lambda ident: {"1": parent}[ident]
        config = Config()
        config.credentials.github.token = True
        merged = {"number": 9, "url": "https://github.com/o/r/pull/9", "state": "MERGED",
                  "isDraft": False, "headRefOid": "A", "headRefName": "ab/task",
                  "baseRefName": "main", "mergedAt": "2026-01-01T00:00:00Z",
                  "mergeCommit": {"oid": "mergecommit"}, "statusCheckRollup": []}
        info = {"branch": "ab/task", "base": "main"}
        args = SimpleNamespace(command="land", action="merge", merged=True, method="squash")
        with patch.object(github.gw, "github_origin", return_value="o/r"), \
             patch.object(github.gw, "current_branch", return_value="ab/task"), \
             patch.object(github.gw, "require_clean"), \
             patch.object(github.gw, "head_sha", return_value="A"), \
             patch.object(github, "view", return_value=merged), \
             patch.object(github, "find_pr", return_value=9), patch.object(github, "gh"):
            with patch.object(github.gw, "remote_branch_head", return_value=None):
                result = github._operate(args, Path("/tmp"), config, adapter, parent, parent, info, {}, [parent])
            self.assertEqual((result["state"], result["merge"]), ("Done", {"oid": "mergecommit"}))
            adapter.set_state.assert_called_once_with(parent, "Done")
            # Origin that cannot be read at all is still a refusal.
            adapter.set_state.reset_mock()
            with patch.object(github.gw, "remote_branch_head",
                              side_effect=AgentBoxError("Cannot read origin's copy of ab/task")):
                with self.assertRaisesRegex(AgentBoxError, "Cannot read origin"):
                    github._operate(args, Path("/tmp"), config, adapter, parent, parent, info, {}, [parent])
            adapter.set_state.assert_not_called()

    def test_a_new_open_pr_wins_over_the_branch_history_and_several_do_not(self):
        from unittest.mock import patch
        from pathlib import Path
        from agent_box import github_workflow as github
        from agent_box.errors import AgentBoxError
        history = [{"number": 4, "state": "MERGED"}, {"number": 9, "state": "OPEN"},
                   {"number": 5, "state": "CLOSED"}]
        with patch.object(github, "gh", return_value=history):
            self.assertEqual(github.find_pr(Path("/tmp"), "o/r", "ab/task", "main"), 9)
        with patch.object(github, "gh", return_value=[{"number": 4, "state": "MERGED"},
                                                      {"number": 6, "state": "MERGED"}]):
            with self.assertRaisesRegex(AgentBoxError, "Several pull requests"):
                github.find_pr(Path("/tmp"), "o/r", "ab/task", "main")

    def test_pending_checks_block_merge_and_an_unapproved_head_is_not_acceptance(self):
        """GitHub readiness and acceptance of this head are separate answers."""
        from pathlib import Path
        from agent_box.config import Config
        from agent_box.github_workflow import acceptance_blockers, blockers
        pr = {"state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "headRefOid": "new",
              "statusCheckRollup": [{"status": "IN_PROGRESS", "name": "tests"}]}
        self.assertTrue(any("CI" in reason for reason in blockers(pr, [])))
        config = Config()
        config.workflow.review.policy = "always"
        saved = {"scope": "current", "validation": {"exit": 0}}
        reasons = acceptance_blockers(Path("/tmp"), config, [], pr, saved, "main", approved=False)
        self.assertTrue(any("fresh independent PASS" in reason for reason in reasons))

    def test_only_a_person_approving_this_exact_head_counts_as_an_approval(self):
        """An app's approval would let the cloud reviewer accept the work it was asked to judge,
        and a pull request's own latestReviews reports neither the account type nor the commit."""
        from pathlib import Path
        from unittest.mock import patch
        from agent_box import github_review, github_workflow
        person = {"login": "maintainer", "type": "User"}
        cases = (([], False),
                 ([{"user": {"login": "claude", "type": "Bot"}, "state": "APPROVED", "commit_id": "new"}], False),
                 ([{"user": person, "state": "APPROVED", "commit_id": "old"}], False),
                 ([{"user": person, "state": "COMMENTED", "commit_id": "new"}], False),
                 ([{"user": person, "state": "APPROVED", "commit_id": "new"}], True),
                 ([{"user": person, "state": "APPROVED", "commit_id": "new"},
                   {"user": person, "state": "CHANGES_REQUESTED", "commit_id": "new"}], False))
        for reviews, approved in cases:
            with self.subTest(reviews=reviews):
                with patch.object(github_review, "paginate", return_value=reviews) as read:
                    self.assertIs(github_workflow.human_approval(
                        Path("/tmp"), "o/r", {"number": 9, "headRefOid": "new"}), approved)
                self.assertIn("pulls/9/reviews", read.call_args.args[-1])

    def test_a_delta_below_the_triggers_accepts_a_pull_request_like_a_local_pass(self):
        """GitHub mode gets the same economy; a delta that still triggers review does not."""
        from pathlib import Path
        from agent_box.config import Config
        from agent_box.github_workflow import acceptance_blockers
        config = Config()
        config.workflow.review.policy = "always"
        pr = {"state": "OPEN", "headRefOid": "new"}
        saved = {"validation": {"exit": 0}, "delta_review": {"round": "r", "reasons": []}}
        self.assertEqual(acceptance_blockers(Path("/tmp"), config, [], pr, saved, "main", approved=False), [])
        saved["delta_review"]["reasons"] = ["sensitive path auth/x matches **/auth/**"]
        self.assertTrue(any("fresh independent PASS" in reason
                            for reason in acceptance_blockers(Path("/tmp"), config, [], pr, saved, "main",
                                                              approved=False)))


class LandingFreshnessTests(LandingCase):
    """Provider responses are immutable snapshots, not shared live task objects."""

    def setUp(self):
        from copy import deepcopy
        from agent_box import git_workflow as gw, workflow
        super().setUp()
        self.config.credentials.github.token = True
        self.config.workflow.merge_mode = "github"
        self.saved["review"] = {"verdict": "PASS"}
        workflow.save_evidence(self.root, self.parent, self.saved)
        self.child = Task("8", "1", "Child", state="Done", parent="7")
        self.adapter.get.side_effect = lambda ident: deepcopy({"7": self.parent, "8": self.child}[ident])
        self.pr = {"number": 9, "url": "https://github.com/o/r/pull/9", "state": "OPEN", "isDraft": False,
                   "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "headRefOid": gw.head_sha(self.root),
                   "headRefName": "ab/7-parent", "baseRefName": "main", "statusCheckRollup": []}
        #: What the reviews endpoint returns; approvals are read from there, not from the PR view.
        self.reviews = []
        self.merged = {**self.pr, "state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z",
                       "mergeCommit": {"oid": "merged"}}

    def run_land(self, phase=None, change=lambda: None, *, action="merge", merged=False):
        from contextlib import ExitStack
        from types import SimpleNamespace
        from unittest.mock import patch
        from agent_box import github_review, github_workflow as github, workflow

        def event(name, value):
            if phase == name:
                change()
            return value

        # `merged` is the repeat of a merge GitHub already completed: the only view is the merged PR.
        views = iter((self.merged,) if merged else (self.pr, self.merged))

        def view(*args):
            value = next(views)
            return event("result" if value["state"] == "MERGED" else "view", value)

        with ExitStack() as stack:
            stack.enter_context(patch.object(github.gw, "github_origin", return_value="o/r"))
            stack.enter_context(patch.object(github, "find_pr", side_effect=lambda *a: event("find", 9)))
            stack.enter_context(patch.object(github, "view", side_effect=view))
            stack.enter_context(patch.object(github, "threads", side_effect=lambda *a: event("threads", [])))
            stack.enter_context(patch.object(github.gw, "remote_branch_head",
                                            side_effect=lambda *a: event("remote", self.pr["headRefOid"])))
            stack.enter_context(patch.object(github_review, "paginate", side_effect=lambda *a: list(self.reviews)))
            self.command = stack.enter_context(patch.object(github, "gh"))
            return workflow.landing(SimpleNamespace(command="land", task=self.parent.id, action=action, method="squash"),
                                    self.root, self.config, self.adapter)

    def advance(self):
        from agent_box import git_workflow as gw
        (self.root / "later.py").write_text("value = 2\n")
        gw.git(self.root, "add", "later.py")
        gw.git(self.root, "commit", "-m", "later work")

    def recover_review(self, *, native=False, integrated=True):
        import json
        from pathlib import Path
        from unittest.mock import patch
        from workflow_fixtures import answer, candidate, selected_route
        # A configured reviewer list routes the round; without one it would inherit the session.
        self.config.workflow.review.models = [candidate("claude", "m", "high")]
        brief = Path(self.temporary.name) / "recovery-brief.md"
        brief.write_text("Changes: original implementation. Risks: lost acceptance. Validation: true passed; no live proof.")
        report = answer("Verdict: PASS\n\nReviewed current implementation and requirements.\n")
        with patch.object(workflow, "candidates", side_effect=selected_route):
            if native:
                opened = self.dispatch(["review", "7", "--native", "--brief", str(brief)])
                prompt = (workflow.review_evidence_path(self.root, "7", opened["round"]) / "handoff.txt").read_text()
                handoff = json.JSONDecoder().raw_decode(prompt.split("Delegation handoff:\n", 1)[1])[0]
                self.assertIn("review the complete implementation", handoff["scope_note"])
                self.assertEqual(handoff["change_summary"], "")
                output = Path(self.temporary.name) / "review.md"
                output.write_text(report["text"])
                self.dispatch(["review", "7", "--result", str(output), "--provider", "claude", "--model", "m"])
            else:
                with patch.object(workflow, "preflight", return_value=None), \
                     patch.object(workflow, "execute", return_value={"report": report, "route": {"provider": "claude", "model": "m", "effort": "high", "tier": 1}}) as reviewer:
                    self.dispatch(["review", "7", "--brief", str(brief)])
                reviewer.assert_called_once()
                if integrated:
                    self.assertIn("review the complete implementation", reviewer.call_args.args[-1])

    def test_inspecting_a_merged_pull_request_reports_instead_of_completing_it(self):
        """The step ab-land prescribes first must not write the tracker or release branches."""
        self.config.workflow.cleanup_local_branches = True
        self.config.workflow.cleanup_remote_branches = True
        inspected = self.run_land(action="inspect", merged=True)
        self.assertEqual(inspected["state"], "In review")
        self.assertEqual(inspected["blockers"], [])
        self.assertNotIn("cleanup", inspected)
        self.adapter.set_state.assert_not_called()
        self.assertEqual(self.run_land(merged=True)["state"], "Done")
        self.adapter.set_state.assert_called_once_with(self.parent, "Done")

    def test_lost_evidence_with_integrated_remote_only_base_requires_real_review(self):
        """A new clone must review current requirements even when the merged SHA was approved."""
        from agent_box import git_workflow as gw
        from agent_box.errors import AgentBoxError
        gw.git(self.root, "update-ref", "refs/remotes/origin/main", self.pr["headRefOid"])
        gw.git(self.root, "branch", "-D", "main")
        self.parent.labels = ["ab:tier:1"]
        self.reviews = [approval(self.pr["headRefOid"])]
        for native, policy in ((False, "auto"), (True, "always")):
            with self.subTest(native=native, policy=policy):
                self.config.workflow.review.policy = policy
                workflow.evidence_path(self.root, self.parent).unlink(missing_ok=True)
                with self.assertRaisesRegex(AgentBoxError, "validation"):
                    self.run_land(merged=True)
                self.dispatch(["validate", "7", "--", "true"])
                inspected = self.dispatch(["inspect", "7"])
                self.assertTrue(inspected["review"]["required"])
                self.assertEqual(inspected["review"]["files"], 0)
                with self.assertRaisesRegex(AgentBoxError, "PASS review"):
                    self.dispatch(["finish", "7"])
                with self.assertRaisesRegex(AgentBoxError, "fresh independent PASS"):
                    self.run_land(merged=True)
                self.adapter.set_state.assert_not_called()
                self.recover_review(native=native)
                self.assertEqual(self.run_land(merged=True)["state"], "Done")
                self.command.assert_not_called()
                self.adapter.set_state.assert_called_once_with(self.parent, "Done")
                self.adapter.set_state.reset_mock()

    def test_integrated_recovery_with_review_off_requires_validation_and_acceptance(self):
        """Skipping local review cannot substitute merge proof for GitHub acceptance."""
        from agent_box import git_workflow as gw
        from agent_box.errors import AgentBoxError
        gw.git(self.root, "update-ref", "refs/heads/main", self.pr["headRefOid"])
        self.parent.labels = ["ab:tier:1"]
        workflow.evidence_path(self.root, self.parent).unlink()
        with self.assertRaisesRegex(AgentBoxError, "validation"):
            self.run_land(merged=True)
        self.dispatch(["validate", "7", "--", "true"])
        self.assertEqual(self.dispatch(["review", "7"])["review"]["verdict"], "SKIPPED")
        with self.assertRaisesRegex(AgentBoxError, "already integrated scope"):
            self.run_land(merged=True)
        self.adapter.set_state.assert_not_called()
        self.reviews = [approval(self.pr["headRefOid"])]
        with self.assertRaisesRegex(AgentBoxError, "pre-merge scope snapshot"):
            self.run_land(merged=True)
        self.config.workflow.review.policy = "always"
        self.recover_review()
        self.assertEqual(self.run_land(merged=True)["state"], "Done")
        self.command.assert_not_called()

    def test_an_unbound_github_approval_leaves_acceptance_to_the_same_policy_as_local_mode(self):
        """A merged approval GitHub cannot tie to this scope is not acceptance; the policy decides.

        The rule that GitHub mode always demanded a fresh PASS is gone: one review
        policy answers both modes, and a cloud verdict is recorded like any other.
        """
        from agent_box.errors import AgentBoxError
        for policy, accepted in (("off", True), ("auto", True), ("always", False)):
            for reviewed, verdict in ((None, None), (None, "SKIPPED"), ("old-head", "SKIPPED"),
                                      (self.pr["headRefOid"], "SKIPPED")):
                with self.subTest(policy=policy, approval=reviewed, verdict=verdict):
                    self.config.workflow.review.policy = policy
                    self.reviews = [approval(reviewed)] if reviewed else []
                    workflow.save_evidence(self.root, self.parent, {**self.saved, "review": {"verdict": verdict}})
                    if accepted:
                        self.assertEqual(self.run_land(merged=True)["state"], "Done")
                        self.adapter.set_state.assert_called_once_with(self.parent, "Done")
                    else:
                        with self.assertRaisesRegex(AgentBoxError, "fresh independent PASS"):
                            self.run_land(merged=True)
                        self.adapter.set_state.assert_not_called()
                    self.command.assert_not_called()
                    self.adapter.set_state.reset_mock()
        # The same head with a recorded PASS completes under the strictest policy.
        workflow.save_evidence(self.root, self.parent, {**self.saved, "review": {"verdict": "PASS"}})
        self.assertEqual(self.run_land(merged=True)["state"], "Done")

    def test_reconcile_approval_cannot_replace_current_successful_validation(self):
        """Current-head cloud approval still needs a successful check of today's scope."""
        from agent_box.errors import AgentBoxError
        self.reviews = [approval(self.pr["headRefOid"])]
        for validation in ({}, {"exit": 1}, {"command": ["true"]}):
            with self.subTest(validation=validation):
                workflow.save_evidence(self.root, self.parent, {**self.saved, "validation": validation})
                with self.assertRaisesRegex(AgentBoxError, "validation"):
                    self.run_land(merged=True)
                self.adapter.set_state.assert_not_called()
                self.command.assert_not_called()

    def test_reconcile_rechecks_head_after_acceptance_reads(self):
        """Acceptance work must not open a window for completing a newer local commit."""
        from unittest.mock import patch
        from agent_box.errors import AgentBoxError
        evidence = workflow.evidence
        def moving_evidence(*args):
            value = evidence(*args)
            self.advance()
            return value
        with patch.object(workflow, "evidence", side_effect=moving_evidence):
            with self.assertRaisesRegex(AgentBoxError, "heads differ"):
                self.run_land(merged=True)
        self.adapter.set_state.assert_not_called()

    def test_same_scope_done_outage_retry_uses_current_acceptance_without_remerge(self):
        """A failed lifecycle write does not invalidate surviving exact-scope acceptance."""
        from agent_box.errors import AgentBoxError
        self.config.workflow.review.policy = "always"
        self.adapter.set_state.side_effect = AgentBoxError("tracker unavailable")
        with self.assertRaisesRegex(AgentBoxError, "tracker unavailable"):
            self.run_land()
        self.command.assert_called_once()
        self.adapter.set_state.side_effect = None
        self.adapter.set_state.reset_mock()
        self.assertEqual(self.run_land(merged=True)["state"], "Done")
        self.command.assert_not_called()
        self.adapter.set_state.assert_called_once_with(self.parent, "Done")

    def approve_github(self):
        self.reviews = [approval(self.pr["headRefOid"])]
        workflow.save_evidence(self.root, self.parent, {**self.saved, "review": {"verdict": "SKIPPED"}})

    def test_github_approval_is_saved_before_merge_and_recovers_same_scope_tracker_failure(self):
        """The observed cloud approval must survive a failed Done write without remerging."""
        from unittest.mock import patch
        from agent_box.errors import AgentBoxError
        self.approve_github()
        save = workflow.save_evidence

        def save_before_merge(*args):
            self.command.assert_not_called()
            return save(*args)

        self.adapter.set_state.side_effect = AgentBoxError("tracker unavailable")
        with patch.object(workflow, "save_evidence", side_effect=save_before_merge):
            with self.assertRaisesRegex(AgentBoxError, "tracker unavailable"):
                self.run_land()
        self.command.assert_called_once()
        saved = workflow.read_evidence(self.root, self.parent)
        self.assertEqual(saved["github_approval"], {
            "scope": self.saved["scope"], "head": self.pr["headRefOid"], "url": self.pr["url"],
            "branch": self.pr["headRefName"], "base": "main"})
        self.adapter.set_state.side_effect = None
        self.adapter.set_state.reset_mock()
        self.assertEqual(self.run_land(merged=True)["state"], "Done")
        self.command.assert_not_called()
        self.adapter.set_state.assert_called_once_with(self.parent, "Done")

    def test_approved_merge_survives_card_edits_after_merge(self):
        """The approval bound to the merged head stays acceptance when only the card changes afterwards."""
        from dataclasses import replace
        from agent_box.errors import AgentBoxError
        self.approve_github()
        self.adapter.set_state.side_effect = AgentBoxError("tracker unavailable")
        with self.assertRaisesRegex(AgentBoxError, "tracker unavailable"):
            self.run_land()
        self.adapter.set_state.side_effect = None
        self.adapter.set_state.reset_mock()
        self.parent = replace(self.parent, description="## Acceptance\n- [x] Clarified requirements",
                              labels=["ab:tier:3"])
        self.assertEqual(self.run_land(merged=True)["state"], "Done")
        self.command.assert_not_called()

    def test_inspecting_open_approval_does_not_authorize_a_later_external_merge(self):
        """Read-only inspection must not bind approval to requirements on another merge path."""
        # Under always the approval is the only acceptance on offer, so binding it
        # during inspection would be the difference between refusing and merging.
        self.config.workflow.review.policy = "always"
        self.approve_github()
        self.assertEqual(self.run_land(action="inspect")["blockers"], [])
        self.assertNotIn("github_approval", workflow.read_evidence(self.root, self.parent))
        from agent_box.errors import AgentBoxError
        with self.assertRaisesRegex(AgentBoxError, "pre-merge scope snapshot"):
            self.run_land(merged=True)
        self.adapter.set_state.assert_not_called()

    def test_card_edits_during_network_keep_acceptance(self):
        """A reworded title or checked item while GitHub answers is not new work to validate again."""
        from dataclasses import replace
        original = self.parent
        for phase in ("find", "view", "threads"):
            for change in ({"title": "New goal"}, {"description": "- [x] Different acceptance"}):
                self.parent = original
                self.adapter.set_state.reset_mock()
                with self.subTest(phase=phase, change=change):
                    result = self.run_land(phase, lambda: setattr(self, "parent", replace(original, **change)))
                    self.assertEqual(result["state"], "Done")
                    self.command.assert_called_once()

    def test_reopened_children_and_unchecked_root_after_feedback_block_merge(self):
        """Completion checks use new tracker snapshots even when normalized scope is unchanged."""
        from dataclasses import replace
        from agent_box import workflow
        from agent_box.errors import AgentBoxError
        self.parent = replace(self.parent, children=["8"])
        self.saved["scope"] = workflow.scope(self.root, "main")
        workflow.save_evidence(self.root, self.parent, self.saved)
        with self.assertRaisesRegex(AgentBoxError, "subtasks"):
            self.run_land("threads", lambda: setattr(self, "child", replace(self.child, state="In progress")))
        self.command.assert_not_called()
        self.child = replace(self.child, state="Done")
        with self.assertRaisesRegex(AgentBoxError, "checklist"):
            self.run_land("threads", lambda: setattr(self, "parent", replace(self.parent, description="## Acceptance\n- [ ] accepted")))
        self.command.assert_not_called()
        self.adapter.set_state.assert_not_called()

    def test_local_head_advanced_during_feedback_cannot_merge_old_pr(self):
        """The local head check must follow the last feedback network request."""
        from agent_box.errors import AgentBoxError
        with self.assertRaisesRegex(AgentBoxError, "heads differ"):
            self.run_land("threads", self.advance)
        self.command.assert_not_called()
        self.adapter.set_state.assert_not_called()

    def test_local_head_advanced_during_merge_result_cannot_complete_old_work(self):
        """An actual merge of the accepted PR cannot finish a newer local head."""
        from agent_box.errors import AgentBoxError
        with self.assertRaisesRegex(AgentBoxError, "heads differ|not merged"):
            self.run_land("result", self.advance)
        self.command.assert_called_once()
        self.adapter.set_state.assert_not_called()

    def test_completion_changed_during_merge_result_prevents_done(self):
        """A successful GitHub merge does not waive another owner or an unchecked item."""
        from dataclasses import replace
        from agent_box.errors import AgentBoxError
        original = self.parent
        for change in ({"assignees": ["2"]}, {"description": "## Acceptance\n- [ ] accepted"}):
            self.parent = original
            with self.subTest(change=change):
                with self.assertRaises(AgentBoxError):
                    self.run_land("result", lambda: setattr(self, "parent", replace(original, **change)))
                self.command.assert_called_once()
                self.adapter.set_state.assert_not_called()

    def test_changed_base_scope_during_feedback_or_result_invalidates_acceptance(self):
        """If the base absorbs reviewed work, old diff acceptance cannot authorize the new scope."""
        from agent_box import git_workflow as gw
        from agent_box.errors import AgentBoxError
        original = gw.local_branch_head(self.root, "main")
        for phase in ("threads", "result"):
            gw.git(self.root, "update-ref", "refs/heads/main", original)
            with self.subTest(phase=phase):
                with self.assertRaisesRegex(AgentBoxError, "validation|scope"):
                    self.run_land(phase, lambda: gw.git(self.root, "update-ref", "refs/heads/main", self.pr["headRefOid"]))
                self.assertEqual(self.command.call_count, 0 if phase == "threads" else 1)
                self.adapter.set_state.assert_not_called()

    def test_reconcile_rechecks_head_and_checklist_after_remote_lookup(self):
        """The remote branch lookup is itself a network window before Done."""
        from dataclasses import replace
        from agent_box.errors import AgentBoxError
        with self.assertRaisesRegex(AgentBoxError, "checklist"):
            self.run_land("remote", lambda: setattr(self, "parent", replace(self.parent, description="- [ ] accepted")),
                          merged=True)
        self.adapter.set_state.assert_not_called()
        self.parent = replace(self.parent, description="- [x] accepted")
        with self.assertRaisesRegex(AgentBoxError, "not merged"):
            self.run_land("remote", self.advance, merged=True)
        self.adapter.set_state.assert_not_called()

    def test_reconcile_rechecks_current_requirements_and_constraints_after_remote_lookup(self):
        """A merged PR cannot mask an unfinished child, another owner or a blocker."""
        from dataclasses import replace
        from agent_box.errors import AgentBoxError
        original = self.parent
        self.child = replace(self.child, state="In progress")
        for change in ({"children": ["8"]}, {"assignees": ["2"]}, {"blockers": ["8"]}):
            self.parent = original
            with self.subTest(change=change):
                with self.assertRaises(AgentBoxError):
                    self.run_land("remote", lambda: setattr(self, "parent", replace(original, **change)),
                                  merged=True)
                self.command.assert_not_called()
                self.adapter.set_state.assert_not_called()

    def test_already_done_after_remote_lookup_needs_no_acceptance_or_lifecycle_write(self):
        """A concurrent successful completion retains ref checks without rewriting Done."""
        from dataclasses import replace
        from unittest.mock import patch
        workflow.evidence_path(self.root, self.parent).unlink()
        with patch.object(workflow, "evidence") as evidence:
            result = self.run_land("remote", lambda: setattr(self, "parent", replace(self.parent, state="Done")),
                                   merged=True)
        self.assertEqual(result["state"], "Done")
        evidence.assert_not_called()
        self.adapter.set_state.assert_not_called()
        self.command.assert_not_called()

    def test_landing_an_open_or_an_already_merged_pull_request_refuses_dirty_work(self):
        """Dropping unused scope reads must retain their clean-worktree prerequisite."""
        from agent_box.errors import AgentBoxError
        (self.root / "change.py").write_text("uncommitted work\n")
        for merged in (False, True):
            with self.subTest(merged=merged):
                with self.assertRaisesRegex(AgentBoxError, "clean"):
                    self.run_land(merged=merged)
                self.command.assert_not_called()
                self.adapter.set_state.assert_not_called()

    def test_current_merge_and_merge_queue_preserve_completion_boundary(self):
        """Confirmed current work completes; a queued PR remains In review."""
        merged = self.merged
        self.merged = self.pr
        self.assertEqual(self.run_land()["state"], "OPEN")
        self.adapter.set_state.assert_not_called()
        self.merged = merged
        self.assertEqual(self.run_land()["state"], "Done")
        self.adapter.set_state.assert_called_once_with(self.parent, "Done")
