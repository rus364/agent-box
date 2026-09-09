"""A prior PASS cannot authorize landing work that has since been reopened."""
import unittest
from agent_box import workflow
from agent_box.tracker.core import Task


class MergeAcceptanceTests(unittest.TestCase):
    def setUp(self):
        # These unit tests isolate PR/ref identity; scope acceptance is exercised
        # below against real temporary Git repositories and evidence files.
        from unittest.mock import patch
        for name, value in (("scope", "current"),
                            ("evidence", {"scope": "current", "validation": {"exit": 0},
                                          "review": {"verdict": "PASS"}})):
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
        parent = Task("1", "1", "Parent", state="Review", assignees=["1"], description="- [x] Accepted")
        adapter = Mock()
        adapter.identity.return_value = {"id": 1}
        adapter.get.side_effect = lambda ident: {"1": parent}[ident]
        config = Config()
        config.github.gh_token = True
        merged = {"number": 9, "url": "https://github.com/o/r/pull/9", "state": "MERGED",
                  "isDraft": False, "headRefOid": "oldhead", "headRefName": "ab/task",
                  "baseRefName": "main", "mergedAt": "2026-01-01T00:00:00Z",
                  "mergeCommit": {"oid": "mergecommit"}, "latestReviews": [], "statusCheckRollup": []}
        info = {"branch": "ab/task", "base": "main"}
        args = SimpleNamespace(command="land", action="reconcile", method="squash")
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
        parent = Task("1", "1", "Parent", state="Review", assignees=["1"], description="- [x] Accepted")
        adapter = Mock()
        adapter.identity.return_value = {"id": 1}
        adapter.get.side_effect = lambda ident: {"1": parent}[ident]
        config = Config()
        config.github.gh_token = True
        merged = {"number": 9, "url": "https://github.com/o/r/pull/9", "state": "MERGED",
                  "isDraft": False, "headRefOid": "A", "headRefName": "ab/task",
                  "baseRefName": "main", "mergedAt": "2026-01-01T00:00:00Z",
                  "mergeCommit": {"oid": "mergecommit"}, "latestReviews": [], "statusCheckRollup": []}
        info = {"branch": "ab/task", "base": "main"}
        args = SimpleNamespace(command="land", action="reconcile", method="squash")
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
        parent = Task("1", "1", "Parent", state="Review", assignees=["1"], description="- [x] Accepted")
        adapter = Mock()
        adapter.identity.return_value = {"id": 1}
        adapter.get.side_effect = lambda ident: {"1": parent}[ident]
        config = Config()
        config.github.gh_token = True
        merged = {"number": 9, "url": "https://github.com/o/r/pull/9", "state": "MERGED",
                  "isDraft": False, "headRefOid": "A", "headRefName": "ab/task",
                  "baseRefName": "main", "mergedAt": "2026-01-01T00:00:00Z",
                  "mergeCommit": {"oid": "mergecommit"}, "latestReviews": [], "statusCheckRollup": []}
        info = {"branch": "ab/task", "base": "main"}
        args = SimpleNamespace(command="land", action="reconcile", method="squash")
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

    def test_silence_pending_checks_and_stale_approval_block_merge(self):
        from agent_box.github_workflow import blockers
        pr = {"state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "headRefOid": "new",
              "latestReviews": [{"state": "APPROVED", "commit": {"oid": "old"}}],
              "statusCheckRollup": [{"status": "IN_PROGRESS", "name": "tests"}]}
        reasons = blockers(pr, [], {"validation": {"exit": 0}})
        self.assertTrue(any("CI" in reason for reason in reasons))
        self.assertTrue(any("approval" in reason for reason in reasons))


class LandingFreshnessTests(unittest.TestCase):
    """Provider responses are immutable snapshots, not shared live task objects."""

    def setUp(self):
        from copy import deepcopy
        from agent_box import git_workflow as gw, workflow
        from test_local_workflow import LocalLandTests
        LocalLandTests.setUp(self)
        self.config.github.gh_token = True
        self.config.workflow.merge_mode = "github"
        self.saved["review"] = {"verdict": "PASS"}
        workflow.save_evidence(self.root, self.parent, self.saved)
        self.child = Task("8", "1", "Child", state="Done", parent="7")
        self.adapter.get.side_effect = lambda ident: deepcopy({"7": self.parent, "8": self.child}[ident])
        self.pr = {"number": 9, "url": "https://github.com/o/r/pull/9", "state": "OPEN", "isDraft": False,
                   "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "headRefOid": gw.head_sha(self.root),
                   "headRefName": "ab/7-parent", "baseRefName": "main", "latestReviews": [], "statusCheckRollup": []}
        self.merged = {**self.pr, "state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z",
                       "mergeCommit": {"oid": "merged"}}

    def run_land(self, phase=None, change=lambda: None, *, action="merge"):
        from contextlib import ExitStack
        from types import SimpleNamespace
        from unittest.mock import patch
        from agent_box import github_workflow as github, workflow

        def event(name, value):
            if phase == name:
                change()
            return value

        views = iter((self.merged,) if action == "reconcile" else (self.pr, self.merged))

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
            self.command = stack.enter_context(patch.object(github, "gh"))
            return workflow.landing(SimpleNamespace(command="land", task=self.parent.id, action=action, method="squash"),
                                    self.root, self.config, self.adapter)

    def advance(self):
        from agent_box import git_workflow as gw
        (self.root / "later.py").write_text("value = 2\n")
        gw.git(self.root, "add", "later.py")
        gw.git(self.root, "commit", "-m", "later work")

    def dispatch(self, arguments):
        from test_local_workflow import LocalLandTests
        return LocalLandTests.dispatch(self, arguments)

    def recover_review(self, *, native=False):
        import json
        from pathlib import Path
        from unittest.mock import patch
        from test_workflow_handoff import selected_route
        brief = Path(self.temporary.name) / "recovery-brief.md"
        brief.write_text("Changes: original implementation. Risks: lost acceptance. Validation: true passed; no live proof.")
        report = {"verdict": "PASS", "summary": "Reviewed current implementation and requirements", "findings": []}
        with patch.object(workflow, "route", side_effect=selected_route):
            if native:
                prepared = self.dispatch(["prepare", "7", "--role", "reviewer", "--primary", "codex",
                                          "--brief", str(brief)])
                handoff = json.loads(prepared["delegate_handoff"].split("Delegation handoff:\n", 1)[1])
                self.assertIn("review the complete implementation", handoff["scope_note"])
                self.assertEqual(handoff["change_summary"]["path_count"], 0)
                output = Path(self.temporary.name) / "review.json"
                output.write_text(json.dumps(report))
                self.dispatch(["report", "7", "--role", "reviewer", "--scope", prepared["scope"],
                               "--handoff", prepared["handoff"], "--file", str(output),
                               "--provider", "claude", "--model", "m", "--effort", "high", "--fresh-context"])
            else:
                with patch.object(workflow, "execute", return_value={"report": report}) as reviewer:
                    self.dispatch(["review", "7", "--primary", "codex", "--brief", str(brief)])
                reviewer.assert_called_once()
                self.assertIn("review the complete implementation", reviewer.call_args.args[-1])

    def test_lost_evidence_with_integrated_remote_only_base_requires_real_review(self):
        """New-clone recovery can review an absorbed implementation through either route."""
        from agent_box import git_workflow as gw
        from agent_box.errors import AgentBoxError
        gw.git(self.root, "update-ref", "refs/remotes/origin/main", self.pr["headRefOid"])
        gw.git(self.root, "branch", "-D", "main")
        self.parent.labels = ["ab:tier:1"]
        for native, policy in ((False, "auto"), (True, "always")):
            with self.subTest(native=native, policy=policy):
                self.config.workflow.review.policy = policy
                workflow.evidence_path(self.root, self.parent).unlink(missing_ok=True)
                with self.assertRaisesRegex(AgentBoxError, "validation"):
                    self.run_land(action="reconcile")
                self.dispatch(["validate", "7", "--", "true"])
                inspected = self.dispatch(["inspect", "7"])
                self.assertTrue(inspected["review"]["required"])
                self.assertEqual(inspected["review"]["files"], 0)
                with self.assertRaisesRegex(AgentBoxError, "PASS review"):
                    self.dispatch(["finish", "7"])
                with self.assertRaisesRegex(AgentBoxError, "GitHub approval or a fresh local PASS"):
                    self.run_land(action="reconcile")
                self.adapter.set_state.assert_not_called()
                self.recover_review(native=native)
                self.assertEqual(self.run_land(action="reconcile")["state"], "Done")
                self.command.assert_not_called()
                self.adapter.set_state.assert_called_once_with(self.parent, "Done")
                self.adapter.set_state.reset_mock()

    def test_integrated_recovery_with_review_off_requires_validation_and_acceptance(self):
        """Skipping local review cannot substitute merge proof for GitHub acceptance."""
        from agent_box import git_workflow as gw
        from agent_box.errors import AgentBoxError
        gw.git(self.root, "update-ref", "refs/heads/main", self.pr["headRefOid"])
        workflow.evidence_path(self.root, self.parent).unlink()
        with self.assertRaisesRegex(AgentBoxError, "validation"):
            self.run_land(action="reconcile")
        self.dispatch(["validate", "7", "--", "true"])
        self.assertEqual(self.dispatch(["review", "7", "--primary", "codex"])["review"]["verdict"], "SKIPPED")
        with self.assertRaisesRegex(AgentBoxError, "GitHub approval or a fresh local PASS"):
            self.run_land(action="reconcile")
        self.adapter.set_state.assert_not_called()
        self.merged["latestReviews"] = [{"state": "APPROVED", "commit": {"oid": self.pr["headRefOid"]}}]
        self.assertEqual(self.run_land(action="reconcile")["state"], "Done")
        self.command.assert_not_called()

    def test_reconcile_requires_github_approval_or_local_pass_regardless_of_local_policy(self):
        """Reconcile uses the same acceptance alternatives as merging an open PR."""
        from agent_box.errors import AgentBoxError
        for policy in ("off", "auto", "always"):
            self.config.workflow.review.policy = policy
            for approval, verdict, accepted in ((None, None, False), (None, "SKIPPED", False),
                                                ("old-head", "SKIPPED", False),
                                                (self.pr["headRefOid"], "SKIPPED", True),
                                                (None, "PASS", True)):
                with self.subTest(policy=policy, approval=approval, verdict=verdict):
                    self.merged["latestReviews"] = ([{"state": "APPROVED", "commit": {"oid": approval}}]
                                                    if approval else [])
                    workflow.save_evidence(self.root, self.parent, {**self.saved, "review": {"verdict": verdict}})
                    if accepted:
                        self.assertEqual(self.run_land(action="reconcile")["state"], "Done")
                        self.adapter.set_state.assert_called_once_with(self.parent, "Done")
                    else:
                        with self.assertRaisesRegex(AgentBoxError, "GitHub approval or a fresh local PASS"):
                            self.run_land(action="reconcile")
                        self.adapter.set_state.assert_not_called()
                    self.command.assert_not_called()
                    self.adapter.set_state.reset_mock()

    def test_reconcile_approval_cannot_replace_current_successful_validation(self):
        """Current-head cloud approval still needs a successful check of today's scope."""
        from agent_box.errors import AgentBoxError
        self.merged["latestReviews"] = [{"state": "APPROVED", "commit": {"oid": self.pr["headRefOid"]}}]
        for validation in ({}, {"exit": 1}, {"command": ["true"]}):
            with self.subTest(validation=validation):
                workflow.save_evidence(self.root, self.parent, {**self.saved, "validation": validation})
                with self.assertRaisesRegex(AgentBoxError, "validation"):
                    self.run_land(action="reconcile")
                self.adapter.set_state.assert_not_called()
                self.command.assert_not_called()

    def test_reconcile_rejects_stale_pass_after_current_scope_revalidation(self):
        """Revalidation cannot revive review acceptance of earlier requirements."""
        from dataclasses import replace
        from agent_box.errors import AgentBoxError
        self.parent = replace(self.parent, title="Changed goal")
        self.dispatch(["validate", "7", "--", "true"])
        with self.assertRaisesRegex(AgentBoxError, "GitHub approval or a fresh local PASS"):
            self.run_land(action="reconcile")
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
                self.run_land(action="reconcile")
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
        self.assertEqual(self.run_land(action="reconcile")["state"], "Done")
        self.command.assert_not_called()
        self.adapter.set_state.assert_called_once_with(self.parent, "Done")

    def test_checked_root_requirements_changed_during_network_need_new_acceptance(self):
        """A replaced root invalidates saved PASS even if every checkbox remains checked."""
        from dataclasses import replace
        from agent_box.errors import AgentBoxError
        original = self.parent
        for phase in ("find", "view", "threads"):
            for change in ({"title": "New goal"}, {"description": "- [x] Different acceptance"},
                           {"labels": ["sensitive"]}):
                self.parent = original
                with self.subTest(phase=phase, change=change):
                    with self.assertRaisesRegex(AgentBoxError, "validation"):
                        self.run_land(phase, lambda: setattr(self, "parent", replace(original, **change)))
                    self.command.assert_not_called()
                    self.adapter.set_state.assert_not_called()

    def test_new_child_topology_after_feedback_invalidates_parent_acceptance(self):
        """A freshly completed new child still changes the scope that was reviewed."""
        from dataclasses import replace
        from agent_box.errors import AgentBoxError
        with self.assertRaisesRegex(AgentBoxError, "validation"):
            self.run_land("threads", lambda: setattr(self, "parent", replace(self.parent, children=["8"])))
        self.command.assert_not_called()
        self.adapter.set_state.assert_not_called()

    def test_reopened_children_and_unchecked_root_after_feedback_block_merge(self):
        """Completion checks use new tracker snapshots even when normalized scope is unchanged."""
        from dataclasses import replace
        from agent_box import workflow
        from agent_box.errors import AgentBoxError
        self.parent = replace(self.parent, children=["8"])
        self.saved["scope"] = workflow.scope(self.root, "main", [self.parent, self.child])
        workflow.save_evidence(self.root, self.parent, self.saved)
        with self.assertRaisesRegex(AgentBoxError, "subtasks"):
            self.run_land("threads", lambda: setattr(self, "child", replace(self.child, state="Doing")))
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

    def test_requirements_or_completion_changed_during_merge_result_prevent_done(self):
        """Successful GitHub merge does not authorize newly changed tracker acceptance."""
        from dataclasses import replace
        from agent_box.errors import AgentBoxError
        original = self.parent
        for change in ({"title": "New goal"}, {"children": ["8"]}, {"assignees": ["2"]},
                       {"description": "## Acceptance\n- [ ] accepted"}):
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

    def test_reconcile_cannot_bypass_requirements_changed_after_merge(self):
        """Retrying a confirmed merge must not accept requirements the first call refused."""
        from dataclasses import replace
        from agent_box.errors import AgentBoxError
        with self.assertRaisesRegex(AgentBoxError, "requirements changed"):
            self.run_land("result", lambda: setattr(self, "parent", replace(self.parent, title="New goal")))
        self.command.assert_called_once()
        with self.assertRaisesRegex(AgentBoxError, "validation"):
            self.run_land(action="reconcile")
        self.command.assert_not_called()
        self.adapter.set_state.assert_not_called()
        self.config.workflow.review.policy = "always"
        self.dispatch(["validate", "7", "--", "true"])
        with self.assertRaisesRegex(AgentBoxError, "GitHub approval or a fresh local PASS"):
            self.run_land(action="reconcile")
        current = workflow.read_evidence(self.root, self.parent)
        workflow.save_evidence(self.root, self.parent, {**current, "review": {"verdict": "PASS"}})
        self.assertEqual(self.run_land(action="reconcile")["state"], "Done")
        self.command.assert_not_called()

    def test_reconcile_rechecks_head_and_checklist_after_remote_lookup(self):
        """The remote branch lookup is itself a network window before Done."""
        from dataclasses import replace
        from agent_box.errors import AgentBoxError
        with self.assertRaisesRegex(AgentBoxError, "checklist"):
            self.run_land("remote", lambda: setattr(self, "parent", replace(self.parent, description="- [ ] accepted")),
                          action="reconcile")
        self.adapter.set_state.assert_not_called()
        self.parent = replace(self.parent, description="- [x] accepted")
        with self.assertRaisesRegex(AgentBoxError, "not merged"):
            self.run_land("remote", self.advance, action="reconcile")
        self.adapter.set_state.assert_not_called()

    def test_reconcile_rechecks_current_requirements_and_constraints_after_remote_lookup(self):
        """A merged PR cannot mask fresh topology, ownership, blocker or scope changes."""
        from dataclasses import replace
        from agent_box.errors import AgentBoxError
        original = self.parent
        self.child = replace(self.child, state="Doing")
        for change in ({"title": "Changed goal"}, {"labels": ["sensitive"]}, {"children": ["8"]},
                       {"assignees": ["2"]}, {"blockers": ["8"]},
                       {"description": "## Acceptance\n- [x] Different requirement"}):
            self.parent = original
            with self.subTest(change=change):
                with self.assertRaises(AgentBoxError):
                    self.run_land("remote", lambda: setattr(self, "parent", replace(original, **change)),
                                  action="reconcile")
                self.command.assert_not_called()
                self.adapter.set_state.assert_not_called()

    def test_already_done_after_remote_lookup_needs_no_acceptance_or_lifecycle_write(self):
        """A concurrent successful completion retains ref checks without rewriting Done."""
        from dataclasses import replace
        from unittest.mock import patch
        workflow.evidence_path(self.root, self.parent).unlink()
        with patch.object(workflow, "evidence") as evidence:
            result = self.run_land("remote", lambda: setattr(self, "parent", replace(self.parent, state="Done")),
                                   action="reconcile")
        self.assertEqual(result["state"], "Done")
        evidence.assert_not_called()
        self.adapter.set_state.assert_not_called()
        self.command.assert_not_called()

    def test_review_request_and_reconcile_still_refuse_dirty_work(self):
        """Dropping unused scope reads must retain their clean-worktree prerequisite."""
        from agent_box.errors import AgentBoxError
        (self.root / "change.py").write_text("uncommitted work\n")
        for action in ("request-review", "reconcile"):
            with self.subTest(action=action):
                with self.assertRaisesRegex(AgentBoxError, "clean"):
                    self.run_land(action=action)
                self.command.assert_not_called()
                self.adapter.set_state.assert_not_called()

    def test_current_merge_and_merge_queue_preserve_completion_boundary(self):
        """Confirmed current work completes; a queued PR remains Review."""
        merged = self.merged
        self.merged = self.pr
        self.assertEqual(self.run_land()["state"], "OPEN")
        self.adapter.set_state.assert_not_called()
        self.merged = merged
        self.assertEqual(self.run_land()["state"], "Done")
        self.adapter.set_state.assert_called_once_with(self.parent, "Done")
