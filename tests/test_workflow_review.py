"""Review rounds: one neutral input, attempts spent only by verdicts, and results bound to their round."""
import hashlib
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_box import git_workflow as gw
from agent_box import provider_runner
from agent_box import workflow
from agent_box import workflow_state
from agent_box.errors import AgentBoxError
from agent_box.repo_binding import Binding
from agent_box.tracker.core import Task
from workflow_fixtures import (BINDING, WorkflowCase, answer, candidate, managed_runner, repository,
                               selected_route)


def package(payload):
    """A round's manifest and every file it names, as a reviewer would reach them from the handoff."""
    manifest_path = Path(payload["manifest"]["path"])
    manifest = json.loads(manifest_path.read_text())
    return manifest, [manifest_path, *(Path(item["path"]) for item in manifest["artifacts"].values()),
                      *(Path(item["artifact_path"]) for item in manifest["repository_instructions"])]


def payload_of(prompt):
    return json.loads(prompt.split("Delegation handoff:\n", 1)[1])


def timeout(*args, **kwargs):
    raise provider_runner.ProviderFailure("mock timeout", "timeout")


class ReviewWorkflowTests(WorkflowCase):

    def setUp(self):
        super().setUp()
        self.config.workflow.review.policy = "always"

    def budget(self):
        return workflow.read_review_budget(self.root, BINDING, self.task, self.config.workflow.review.max_rounds)

    def test_native_result_is_recorded_on_trust_for_the_round_it_opened(self):
        """The coordinator's word names the reviewer; no descriptor, model check or context flag stands in for it."""
        opened = self.native_round()
        handoff = self.delegation_input(opened)
        self.assertIn(str(handoff), opened["message"])
        self.assertIn(hashlib.sha256(handoff.read_bytes()).hexdigest(), opened["message"])
        self.assertIn("fresh context without the parent's history", opened["instructions"])
        self.assertNotIn("fork_turns", json.dumps(opened))
        self.assertEqual(self.budget()["used"], 0)
        self.assertEqual(self.run_workflow("inspect", "1")["review"]["pending_round"],
                         {"round": opened["round"], "backend": "native", "status": "awaiting --result"})
        with self.assertRaisesRegex(AgentBoxError, "--provider PROVIDER --model MODEL"):
            self.run_workflow("review", "1", "--result", str(self.report))
        result = self.native_result("Summary first.\n\n**Verdict:** PASS\n\nNo findings.\n", "--effort", "high")
        self.assertEqual((result["verdict"], result["reconciliation"]["status"]), ("PASS", "applied"))
        self.assertEqual(result["route"], {"provider": "claude", "model": "m", "effort": "high", "backend": "native"})
        self.assertEqual(result["review_budget"]["used"], 1)
        self.assertEqual(workflow.read_evidence(self.root, self.task)["review"]["verdict"], "PASS")
        self.assertFalse(handoff.parent.exists())
        with self.assertRaisesRegex(AgentBoxError, "No native review round is pending"):
            self.native_result()
        with self.assertRaisesRegex(AgentBoxError, "pass them with --result"):
            self.run_workflow("review", "1", "--native", "--model", "m")

    def test_a_native_answer_without_exactly_one_verdict_line_is_a_failure_like_a_managed_one(self):
        """A native round is counted from its start, so neither a bad answer nor an abandoned reviewer retries forever."""
        for count, text in ((1, "Looks fine to me.\n"), (2, "Verdict: PASS\nVerdict: NEEDS_WORK\n")):
            opened = self.native_round()
            with self.assertRaisesRegex(AgentBoxError, f"ended as failure {count} of 2.*--native"):
                self.native_result(text)
            self.assertFalse(self.delegation_input(opened).exists())
            with self.assertRaisesRegex(AgentBoxError, "No native review round is pending"):
                self.native_result()
        self.assertEqual((self.budget()["used"], self.budget()["failures"]), (0, 2))
        self.assertNotIn("review", workflow.read_evidence(self.root, self.task))
        with self.assertRaisesRegex(AgentBoxError, "Review stopped"):
            self.native_round()
        self.run_workflow("review-budget", "1", "--reason", "User allows another native review")
        self.native_round()
        result = self.native_result("Verdict: NEEDS_WORK\n\n- [HIGH] change.py:1 — bound the value\n"
                                    "- [LOW] docs.md:3 — typo\n")
        self.assertEqual((result["verdict"], result["findings"]), ("NEEDS_WORK", {"HIGH": 1, "LOW": 1}))
        self.assertEqual((result["review_budget"]["used"], result["review_budget"]["failures"]), (1, 0))

    def test_a_commit_after_the_native_round_opened_makes_its_verdict_stale(self):
        """The verdict still cost a round, but it describes code that is no longer the head."""
        self.native_round()
        gw.git(self.root, "commit", "--allow-empty", "-m", "later work")
        result = self.native_result()
        self.assertEqual((result["reconciliation"]["status"], result["reconciliation"]["changes"]), ("stale", ["head"]))
        self.assertEqual(result["review_budget"]["used"], 1)
        self.assertNotIn("review", workflow.read_evidence(self.root, self.task))

    def test_damaged_native_input_refuses_the_answer_without_spending(self):
        """A reviewer that may have read other bytes cannot record a verdict; owned cleanup stays exact."""
        for damage in ("missing", "modified", "symlink", "replaced-package", "unknown-file"):
            with self.subTest(damage=damage):
                workflow.save_review_state(self.root, BINDING, self.task, {"used": 0})
                opened = self.native_round()
                path = self.delegation_input(opened)
                descriptor = json.loads((path.parent / "descriptor.json").read_text())
                original = path.read_bytes()
                target = path.parent.with_name(path.parent.name + "-original")
                if damage == "missing":
                    path.unlink()
                elif damage == "modified":
                    path.chmod(0o600)
                    path.write_text("changed")
                    path.chmod(0o400)
                elif damage == "symlink":
                    path.rename(target)
                    path.symlink_to(target)
                elif damage == "replaced-package":
                    path.parent.rename(target)
                    path.parent.mkdir(mode=0o700)
                    for item in target.iterdir():
                        item.rename(path.parent / item.name)
                else:
                    (path.parent / "user-notes.txt").write_text("keep")
                used = self.budget()["used"]
                with self.assertRaisesRegex(AgentBoxError, "[Dd]elegation input|native round"):
                    self.native_result()
                self.assertEqual(self.budget()["used"], used)
                self.assertNotIn("review", workflow.read_evidence(self.root, self.task))
                if damage in ("replaced-package", "unknown-file"):
                    with self.assertRaisesRegex(AgentBoxError, "replaced|Unknown delegation input"):
                        workflow.cleanup_delegation_input(self.root, descriptor)
                    self.assertEqual(path.read_bytes(), original)
                    if damage == "unknown-file":
                        (path.parent / "user-notes.txt").unlink()
                        self.assertEqual(self.native_result()["reconciliation"]["status"], "applied")
                        self.assertFalse(path.parent.exists())
                elif damage != "missing":
                    workflow.cleanup_delegation_input(self.root, descriptor)
                    self.assertFalse(path.parent.exists())
                    if damage == "symlink":
                        self.assertEqual(target.read_bytes(), original)

    def test_managed_reviews_use_exact_bytes_and_clean_only_owned_packages(self):
        """A changed input after dispatch keeps the verdict private; a failure retires its input unspent."""
        for outcome in ("success", "failure", "symlink", "replaced-package", "unknown-file"):
            with self.subTest(outcome=outcome):
                workflow.save_review_state(self.root, BINDING, self.task, {"used": 0})
                seen = {}

                def provider(route, actual_role, root, prompt, **kwargs):
                    path = Path(kwargs["read_paths"][0]) / "handoff.txt"
                    seen["path"] = path
                    self.assertEqual(actual_role, "reviewer")
                    self.assertEqual(prompt.encode("utf-8"), path.read_bytes())
                    target = path.parent.with_name(path.parent.name + "-original")
                    if outcome == "failure":
                        timeout()
                    if outcome == "symlink":
                        path.rename(target)
                        path.symlink_to(target)
                    elif outcome == "replaced-package":
                        path.parent.rename(target)
                        path.parent.mkdir(mode=0o700)
                        for item in target.iterdir():
                            item.rename(path.parent / item.name)
                    elif outcome == "unknown-file":
                        (path.parent / "user-notes.txt").write_text("keep")
                    return answer()

                with patch.object(workflow, "run", side_effect=managed_runner(provider)) as runner:
                    if outcome == "failure":
                        with self.assertRaisesRegex(AgentBoxError, "no attempt spent"):
                            self.run_workflow("review", "1")
                        self.assertEqual(self.budget()["used"], 0)
                    else:
                        result = self.run_workflow("review", "1")
                        self.assertEqual(result["reconciliation"]["status"],
                                         "applied" if outcome == "success" else "invalid_input")
                        self.assertEqual(result["review_budget"]["used"], 1)
                    self.assertEqual(runner.call_count, 1)
                path = seen["path"]
                if outcome == "symlink":
                    self.assertTrue(path.is_symlink())
                elif outcome in ("replaced-package", "unknown-file"):
                    self.assertTrue(path.exists())
                else:
                    self.assertFalse(path.parent.exists())
                self.assertEqual("review" in workflow.read_evidence(self.root, self.task), outcome == "success")

    def test_unpublished_review_input_is_cleaned_after_write_failure(self):
        """A descriptor write failure must not strand an unpublished input."""
        write = workflow.atomic_write_text

        def fail(path, *args, **kwargs):
            if path.name == "descriptor.json":
                raise OSError("disk full")
            return write(path, *args, **kwargs)
        with patch.object(workflow, "atomic_write_text", side_effect=fail):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.native_round()
        self.assertFalse(list(workflow.WORKFLOW_STATE.rglob("handoff.txt")))

    def test_the_task_tier_selects_reviewer_models_only_while_tiers_are_on(self):
        """Tiers off run the role's own list for a tier-labelled task; with no list, review is native only."""
        self.patches[5].stop()  # Exercise the real candidate selection.
        self.task.labels = ["ab:tier:2"]
        self.config.workflow.review.max_rounds = 30
        review = self.config.workflow.review
        for provider in ("codex", "claude"):
            for use_tiers, models, tier_list, expected in ((False, False, True, None), (False, True, True, "global"),
                                                           (True, False, True, "tier"), (True, True, True, "tier"),
                                                           (True, True, False, "global")):
                self.config.workflow.use_tiers = use_tiers
                review.models = [candidate(provider, provider + "-global", "default")] if models else []
                review.tiers["2"] = [candidate(provider, provider + "-tier", "high")] if tier_list else []
                with self.subTest(provider=provider, use_tiers=use_tiers, models=models, tier_list=tier_list), \
                     patch.object(workflow.shutil, "which", side_effect=lambda name: name), \
                     patch.object(workflow, "run", return_value=answer()) as run:
                    if expected is None:
                        with self.assertRaisesRegex(AgentBoxError, "No reviewer candidates.*--native"):
                            self.run_workflow("review", "1")
                        run.assert_not_called()
                        continue
                    result = self.run_workflow("review", "1")
                    route = run.call_args.args[0]
                    self.assertEqual(route["model"], provider + "-" + expected)
                    self.assertEqual(route["tier"], 2 if use_tiers else None)
                    if expected == "global":
                        cmd = provider_runner.build_command(route, "reviewer", self.root, self.report)
                        self.assertNotIn("--effort", cmd)
                        self.assertFalse(any("model_reasoning_effort=" in arg for arg in cmd))
                    self.assertEqual(result["verdict"], "PASS")

    def test_a_managed_verdict_replaces_earlier_human_acceptance(self):
        """Starting a review retires acceptance; its PASS becomes the evidence."""
        fingerprint = workflow.scope(self.root, "main")
        workflow.save_evidence(self.root, self.task, {
            "scope": fingerprint, "human_acceptance": {
                "scope": fingerprint, "actor": "1", "reason": "Earlier human decision"}})
        with patch.object(workflow, "run", side_effect=managed_runner(lambda *a, **k: answer())):
            result = self.run_workflow("review", "1", "--brief", str(self.review_brief))
        self.assertEqual(result["verdict"], "PASS")
        saved = workflow.read_evidence(self.root, self.task)
        self.assertEqual(saved["review"]["verdict"], "PASS")
        self.assertNotIn("human_acceptance", saved)

    def test_native_and_managed_rounds_share_one_neutral_complete_handoff(self):
        """A post-fix verdict gets the updated diff, not the first review's story."""
        self.task.description = (
            "## Goal\nCurrent goal.\n\n## Context\nPrevious verdict found an obsolete problem.\n\n"
            "## Acceptance\n- [ ] Current behavior works.\n")
        (self.root / "change.py").write_text("value = 1\n")
        gw.git(self.root, "add", ".")
        gw.git(self.root, "commit", "-m", "current implementation")
        first = self.native_round("--brief", str(self.review_brief))
        _, first_artifacts = package(payload_of(self.delegation_input(first).read_text()))
        self.assertTrue(all(path.is_file() for path in first_artifacts))
        finding = "value must be bounded before release"
        self.native_result(f"Verdict: NEEDS_WORK\n\n- [HIGH] change.py:1 — {finding}\n")
        self.assertTrue(all(not path.exists() for path in first_artifacts))

        (self.root / "change.py").write_text("value = 2\n")
        gw.git(self.root, "add", ".")
        gw.git(self.root, "commit", "-m", "fix review finding")
        second = self.native_round("--brief", str(self.review_brief))
        self.assertNotEqual(first["scope"], second["scope"])
        prompt = self.delegation_input(second).read_text()
        payload = payload_of(prompt)
        manifest, _ = package(payload)
        self.assertEqual(Path(manifest["artifacts"]["task.md"]["path"]), Path(payload["task_description"]))
        self.assertEqual(Path(payload["task_description"]).read_text(),
                         "# Task (id 1): Task\n\n" + self.task.description.strip() + "\n")
        self.assertIn("Changes and reasons", Path(manifest["artifacts"]["author-report.md"]["path"]).read_text())
        for absent in ("Current goal", "Previous verdict", finding, "Changes and reasons", "diff --git",
                       "+value = 2", "report_schema"):
            self.assertNotIn(absent, prompt)
        for present in (gw.head_sha(self.root), "diff_range", "untrusted English brief", "counterexamples",
                        'ask "then what?"', "discard the finding", "Prefer leaving correct code unchanged.",
                        "Verdict: PASS", "[SEVERITY] path:line", "CRITICAL —", "LOW —",
                        "closed by the coordinator"):
            self.assertIn(present, prompt)
        self.adapter.comments.assert_not_called()
        self.native_result()

        seen = []

        def provider(route, role, root, managed_prompt, **kwargs):
            seen.append(managed_prompt)
            return answer()

        with patch.object(workflow, "run", side_effect=managed_runner(provider)):
            self.run_workflow("review", "1", "--brief", str(self.review_brief))
        self.assertEqual(seen[0].split("Delegation handoff:\n", 1)[0], prompt.split("Delegation handoff:\n", 1)[0])
        self.assertEqual(payload_of(seen[0])["boundaries"], payload["boundaries"])

    def test_the_handoff_states_the_reviewer_sandbox_instead_of_leaving_it_to_be_found(self):
        """A managed round runs read-only, so an unstated limit costs part of every attempt."""
        prompt = self.delegation_input(self.native_round()).read_text()
        self.assertIn("read-only", prompt)
        self.assertIn("test suite cannot be run", prompt)
        self.assertIn("recorded-validation.json", prompt)

    def test_a_managed_result_cannot_overwrite_a_newer_native_round(self):
        """A provider returning after another round opened keeps its verdict private."""
        def provider(*args, **kwargs):
            self.native_round()
            return answer("Verdict: PASS\n\nOlder managed round.\n")

        with patch.object(workflow, "run", side_effect=managed_runner(provider)):
            result = self.run_workflow("review", "1")
        self.assertEqual(result["reconciliation"]["status"], "superseded")
        self.assertTrue(Path(result["result_path"]).exists())
        self.assertNotIn("review", workflow.read_evidence(self.root, self.task))
        latest = self.native_result("Verdict: PASS\n\nLatest native.\n")
        self.assertEqual((latest["reconciliation"]["status"], latest["review_budget"]["used"]), ("applied", 2))
        private = json.loads(Path(latest["result_path"]).read_text())
        self.assertIn("Latest native", private["output"]["report"]["text"])

    def test_managed_reviewer_reads_its_own_evidence_before_cleanup(self):
        marker = "MANAGED_REVIEW_DIFF_EVIDENCE"
        (self.root / "managed.py").write_text(marker + "\n")
        gw.git(self.root, "add", "managed.py")
        gw.git(self.root, "commit", "-m", "managed evidence fixture")
        artifact_paths = []

        def provider(route, role, root, prompt, *, read_paths, **kwargs):
            manifest, paths = package(payload_of(prompt))
            artifact_paths.extend(paths)
            self.assertEqual(list(read_paths), [paths[0].parent])
            self.assertTrue(all(path.is_file() for path in paths))
            self.assertIn(marker, Path(manifest["artifacts"]["full.diff"]["path"]).read_text())
            return answer()

        with patch.object(workflow, "run", side_effect=managed_runner(provider)):
            self.assertEqual(self.run_workflow("review", "1")["verdict"], "PASS")
        self.assertTrue(artifact_paths and all(not path.exists() for path in artifact_paths))

    def test_known_managed_launch_refusals_precede_the_round(self):
        """Local constraints observed before launch neither open a round nor count as failures."""
        for cause in ("unavailable", "unsupported", "busy"):
            with self.subTest(cause=cause), \
                 patch.object(workflow, "reviewer_handoff") as handoff, \
                 patch.object(workflow, "run") as provider, \
                 patch.object(provider_runner.shutil, "which", return_value=None if cause == "unavailable" else "cli"):
                if cause == "unsupported":
                    with patch.object(workflow, "candidates", return_value={
                            **selected_route(None, "reviewer", 1), "provider": "other"}), \
                         self.assertRaisesRegex(AgentBoxError, "Unsupported managed provider"):
                        self.run_workflow("review", "1")
                elif cause == "busy":
                    with workflow_state.lock(self.root, "review"), \
                         self.assertRaisesRegex(AgentBoxError, "Another local review"):
                        self.run_workflow("review", "1")
                else:
                    with self.assertRaisesRegex(AgentBoxError, "Cannot find claude CLI"):
                        self.run_workflow("review", "1")
                handoff.assert_not_called()
                provider.assert_not_called()
        self.assertFalse(workflow.review_budget_path(self.root, BINDING, self.task).exists())

    def test_failures_spend_nothing_and_two_in_a_row_stop_review_until_the_user_decides(self):
        """A kill, a timeout or a missing verdict line is retried free, but never forever."""
        with patch.object(workflow, "run", side_effect=timeout) as provider:
            for count in (1, 2):
                with self.assertRaisesRegex(AgentBoxError, f"no attempt spent, failure {count} of 2"):
                    self.run_workflow("review", "1")
            self.assertEqual((self.budget()["used"], self.budget()["failures"]), (0, 2))
            for flags in ((), ("--native",)):
                with self.subTest(flags=flags), self.assertRaisesRegex(AgentBoxError, "Review stopped.*user must decide"):
                    self.run_workflow("review", "1", *flags)
            self.assertEqual(provider.call_count, 2)
        budget = self.run_workflow("review-budget", "1", "--reason", "User allows another try")["review_budget"]
        self.assertEqual((budget["failures"], budget["limit"]), (0, 3))
        self.assertNotIn("ceiling", workflow.read_review_state(self.root, BINDING, self.task))
        with patch.object(workflow, "run", side_effect=timeout), self.assertRaises(AgentBoxError):
            self.run_workflow("review", "1")
        with patch.object(workflow, "run", return_value=answer()):
            result = self.run_workflow("review", "1")
        self.assertEqual((result["review_budget"]["used"], result["review_budget"]["failures"]), (1, 0))

    def test_a_managed_answer_without_a_verdict_line_is_a_failure_with_readable_streams(self):
        """The runner's parse refuses the answer; the round costs nothing and its raw streams stay private."""
        def provider(route, role, root, prompt, *, streams, **kwargs):
            run = streams()
            (run / "stdout").write_text('{"type": "turn.completed"}\n')
            (run / "stderr").write_text("warning\n")
            return provider_runner.review_report("No verdict here.\n")

        with patch.object(workflow, "run", side_effect=provider), \
             self.assertRaisesRegex(AgentBoxError, r"no attempt spent, failure 1 of 2 in a row\. Raw streams: ") as raised:
            self.run_workflow("review", "1")
        streams = Path(raised.exception.message.rsplit("Raw streams: ", 1)[1])
        self.assertEqual((streams / "1" / "stderr").read_text(), "warning\n")
        self.assertEqual(stat.S_IMODE((streams / "1").stat().st_mode), 0o700)
        self.assertEqual(self.budget()["used"], 0)

    def test_a_killed_managed_review_reads_back_as_an_ended_failure(self):
        """No code runs after a kill, so the round was already counted as a failure when it started."""
        with patch.object(workflow, "run", return_value=answer()):
            self.run_workflow("review", "1")
        with patch.object(workflow, "run", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.run_workflow("review", "1")
        state = workflow.read_review_state(self.root, BINDING, self.task)
        self.assertEqual((state["used"], state["failures"]), (1, 1))
        with self.assertRaisesRegex(AgentBoxError, "ended without a verdict.*spent no attempt.*failure 1 of 2"):
            self.run_workflow("review-result", "1")
        self.assertEqual(self.run_workflow("inspect", "1")["review"]["pending_round"],
                         {"round": state["pending"], "backend": "managed", "status": "ended_without_response"})
        with workflow_state.lock(self.root, "review"):
            with self.assertRaisesRegex(AgentBoxError, "still running"):
                self.run_workflow("review-result", "1", "--round=" + state["pending"])

    def test_provider_fallbacks_run_within_one_round(self):
        """An unavailable first candidate hands the same round to the next one."""
        selected = selected_route(None, "reviewer", 1)
        selected["fallbacks"] = [{**selected, "provider": "codex", "fallbacks": []}]
        with patch.object(workflow, "candidates", return_value=selected), \
             patch.object(workflow, "run", side_effect=[
                 provider_runner.ProviderFailure("Mock timeout", "timeout"), answer()]) as provider:
            result = self.run_workflow("review", "1")
        self.assertEqual([call.args[0]["provider"] for call in provider.call_args_list], ["claude", "codex"])
        self.assertEqual((result["review_budget"]["used"], result["review_budget"]["failures"]), (1, 0))
        self.assertEqual(result["route"]["provider"], "codex")
        self.assertEqual(result["failures"][0]["category"], "timeout")

    def test_raw_streams_stay_until_the_task_is_done(self):
        """A verdict's streams explain an INCONCLUSIVE later; Done is when nobody needs them."""
        def provider(route, role, root, prompt, *, streams, **kwargs):
            (streams() / "stdout").write_text("events\n")
            return answer("Verdict: INCONCLUSIVE\n\nInput files not found.\n")

        with patch.object(workflow, "run", side_effect=managed_runner(provider)):
            self.run_workflow("review", "1")
        result = self.run_workflow("review-result", "1")
        self.assertEqual(result["verdict"], "INCONCLUSIVE")
        self.assertEqual((Path(result["stream_path"]) / "1" / "stdout").read_text(), "events\n")
        from agent_box.branch_cleanup import discard_review_streams
        self.assertNotIn("review_streams_error", discard_review_streams({}, self.root, self.task))
        self.assertFalse(Path(result["stream_path"]).exists())
        self.assertNotIn("stream_path", self.run_workflow("review-result", "1"))

    def test_review_budget_counts_verdicts_across_scope_changes(self):
        """Neither a new commit nor the backend replenishes the attempts a task has spent."""
        self.config.workflow.review.max_rounds = 1
        with patch.object(workflow, "run", return_value=answer()):
            first = self.run_workflow("review", "1")
        self.assertEqual((first["review_budget"]["used"], first["review_budget"]["remaining"]), (1, 0))
        gw.git(self.root, "commit", "--allow-empty", "-m", "new work")
        for flags in ((), ("--native",)):
            with self.subTest(flags=flags), patch.object(workflow, "run") as provider, \
                 self.assertRaisesRegex(AgentBoxError, "budget exhausted.*review-budget 1 --limit TOTAL"):
                self.run_workflow("review", "1", *flags)
            provider.assert_not_called()
        self.run_workflow("review-budget", "1", "--limit", "2", "--reason", "User grants one round")
        opened = self.native_round()
        self.assertNotIn("User grants one round", self.delegation_input(opened).read_text())
        self.assertEqual(self.native_result()["review_budget"]["used"], 2)

    def test_review_budget_lock_corruption_and_binding_isolation(self):
        """Concurrent rounds and unreadable counters fail closed; other projects are distinct."""
        with workflow_state.lock(self.root, "review-budget"), \
             self.assertRaisesRegex(AgentBoxError, "Another local review-budget"):
            workflow.start_review(self.root, BINDING, self.task, 1, "round", "scope", "managed")
        workflow.start_review(self.root, BINDING, self.task, 1, "round", "scope", "managed")
        self.assertEqual(self.budget()["failures"], 1)
        other = Binding("vikunja", "https://tasks.example", "other")
        self.assertEqual(workflow.read_review_budget(self.root, other, self.task, 1)["failures"], 0)
        path = workflow.review_budget_path(self.root, BINDING, self.task)
        for malformed in ("broken", "{}", '{"used":true}', '{"used":-1}', '{"used":0,"failures":-1}'):
            path.write_text(malformed)
            with self.subTest(malformed=malformed), self.assertRaisesRegex(AgentBoxError, "Cannot read review budget"):
                workflow.start_review(self.root, BINDING, self.task, 1, "round", "scope", "managed")

    def test_failed_budget_write_cannot_dispatch_an_unaccounted_review(self):
        """A failed round start never publishes a handoff or reaches a provider."""
        for flags in ((), ("--native",)):
            with self.subTest(flags=flags), \
                 patch.object(workflow, "save_review_state", side_effect=OSError("disk failure")), \
                 patch.object(workflow, "reviewer_handoff") as handoff, \
                 patch.object(workflow, "run") as provider, \
                 self.assertRaisesRegex(OSError, "disk failure"):
                self.run_workflow("review", "1", *flags)
            handoff.assert_not_called()
            provider.assert_not_called()

    def test_budget_decision_is_absolute_idempotent_and_preserves_acceptance(self):
        """Retrying a budget decision never grants more attempts or alters review evidence."""
        state = {"used": 3, "failures": 2, "pending": "existing-round", "annotation": "Preserved opaque state"}
        workflow.save_review_state(self.root, BINDING, self.task, state)
        accepted = {"scope": workflow.scope(self.root, "main"), "validation": {"exit": 0}, "review": {"verdict": "PASS"}}
        workflow.save_evidence(self.root, self.task, accepted)
        for task_state in ("Doing", "Review"):
            self.task.state = task_state
            budget = self.run_workflow("review-budget", "1", "--limit", "6", "--reason", "User grants three more")["review_budget"]
            self.assertEqual((budget["used"], budget["configured_limit"], budget["limit"], budget["remaining"]),
                             (3, 3, 6, 3))
            self.assertEqual(workflow.read_review_state(self.root, BINDING, self.task),
                             {**state, "failures": 0, "ceiling": 6, "ceiling_reason": "User grants three more"})
            self.assertEqual(workflow.read_evidence(self.root, self.task), accepted)
        limited = self.run_workflow("review-budget", "1", "--limit", "0", "--reason", "User stops further attempts")
        self.assertEqual((limited["review_budget"]["used"], limited["review_budget"]["remaining"]), (3, 0))

    def test_budget_decision_refuses_invalid_input_ownership_and_corrupt_state(self):
        """Only the assigned coordinator can change a valid budget for active work."""
        for flags in (("--limit", "-1", "--reason", "User decision"), ("--limit", "6", "--reason", " ")):
            with self.subTest(flags=flags), self.assertRaisesRegex(AgentBoxError, "non-empty reason.*nonnegative limit"):
                self.run_workflow("review-budget", "1", *flags)
        for state, assignees in (("Done", ["1"]), ("Doing", ["someone-else"]), ("Review", [])):
            self.task.state, self.task.assignees = state, assignees
            with self.subTest(state=state, assignees=assignees), self.assertRaisesRegex(AgentBoxError, "current user's Doing or Review"):
                self.run_workflow("review-budget", "1", "--reason", "User decision")
        self.task.state, self.task.assignees = "Doing", ["1"]
        self.assertFalse(workflow.review_budget_path(self.root, BINDING, self.task).exists())
        for bad in ({"ceiling": True, "ceiling_reason": "Reason"}, {"ceiling": 6},
                    {"ceiling": -1, "ceiling_reason": "Reason"}):
            workflow.save_review_state(self.root, BINDING, self.task, {"used": 0, **bad})
            with self.subTest(bad=bad), self.assertRaisesRegex(AgentBoxError, "Cannot read review budget"):
                self.run_workflow("review-budget", "1", "--limit", "6", "--reason", "Must not repair corruption")

    def test_policy_skipped_review_opens_no_round(self):
        """A policy decision is not an independent review attempt."""
        self.config.workflow.review.policy = "off"
        for flags in ((), ("--native",)):
            result = self.run_workflow("review", "1", *flags)
            self.assertEqual((result["review"]["verdict"], result["review_budget"]["used"]), ("SKIPPED", 0))

    def test_a_card_with_any_sections_is_reviewed_with_its_parent_and_without_comments(self):
        """Headings are not parsed: every card reaches the reviewer whole, and discussion never does."""
        parent = Task("7", "1", "Parent", description="Free-form parent goal.\n\n## Release Conditions\nKeep hidden rule.")
        self.task.parent = "7"
        self.task.description = "No headings at all.\n- [x] checked item\n## Mystery\n```python\nunclosed fence"
        self.adapter.comments.return_value = [{"comment": "PRIVATE_DISCUSSION"}]
        gw.git(self.root, "branch", "ab/7-parent")
        with patch.object(workflow, "resolve", return_value=(parent, {"branch": "ab/7-parent", "base": "main"},
                                                             {"branch": "ab/1-task"}, "main")):
            opened = self.native_round()
        manifest, paths = package(payload_of(self.delegation_input(opened).read_text()))
        self.assertEqual(sorted(manifest["artifacts"]), ["changed-paths.txt", "full.diff", "parent.md",
                                                         "recorded-validation.json", "task.md"])
        self.assertIn("unclosed fence", Path(manifest["artifacts"]["task.md"]["path"]).read_text())
        self.assertIn("- [ ] checked item", Path(manifest["artifacts"]["task.md"]["path"]).read_text())
        self.assertIn("Keep hidden rule.", Path(manifest["artifacts"]["parent.md"]["path"]).read_text())
        self.assertTrue(all("PRIVATE_DISCUSSION" not in path.read_text() for path in paths))
        self.adapter.comments.assert_not_called()

    def test_every_description_heading_maps_the_visible_number_to_the_id(self):
        """Cards cite tasks as #N or by ID; a heading with only one left the reviewer unable to match them."""
        parent = Task("607", "84", "Parent", description="See #59.", number=141,
                      url="https://tracker.example/tasks/607")
        child = Task("608", "84", "Child", description="Blocked by 607.", parent="607", number=142,
                     url="https://tracker.example/tasks/608")
        descriptions = workflow.review_descriptions(child, parent, [parent, child])
        self.assertTrue(descriptions["task.md"].startswith(
            "# Task #142 (id 608, https://tracker.example/tasks/608): Child\n"))
        self.assertTrue(descriptions["parent.md"].startswith(
            "# Task #141 (id 607, https://tracker.example/tasks/607): Parent\n"))

    def test_a_parent_review_carries_every_subtask_description(self):
        """The final review checks acceptance that lives in the subtasks, one file each."""
        with tempfile.TemporaryDirectory() as directory:
            root = repository(directory)
            gw.git(root, "switch", "-c", "ab/7-parent")
            parent = Task("7", "1", "Parent", description="Parent goal", children=["8", "9"])
            children = [Task("8", "1", "First", description="First acceptance", parent="7"),
                        Task("9", "1", "Second", description="Second acceptance", parent="7")]
            workflow.reviewer_handoff(root, "main", parent, parent, [children[1], parent, children[0]],
                                      None, {}, "", "parent")
            path = workflow.review_evidence_path(root, "7", "parent")
            self.assertEqual(sorted(item.name for item in path.iterdir()),
                             ["changed-paths.txt", "full.diff", "handoff.txt", "manifest.json",
                              "recorded-validation.json", "subtask-1.md", "subtask-2.md", "task.md"])
            self.assertIn("Second acceptance", (path / "subtask-1.md").read_text())
            self.assertIn("First acceptance", (path / "subtask-2.md").read_text())
            (path / "subtask-3.md").write_text("not written by this round")
            with self.assertRaisesRegex(AgentBoxError, "Unknown delegation input content"):
                workflow.cleanup_review_evidence(path)
            self.assertTrue((path / "subtask-1.md").exists())
            (path / "subtask-3.md").unlink()
            workflow.cleanup_review_evidence(path)
            self.assertFalse(path.exists())

    def test_review_runs_without_a_brief(self):
        """The tracker already holds the requirements; a brief only adds what it lacks."""
        with patch.object(workflow, "run", side_effect=managed_runner(lambda *args, **kwargs: answer())):
            self.assertEqual(self.run_workflow("review", "1")["reconciliation"]["status"], "applied")
        self.review_brief.write_text(" \n")
        with self.assertRaisesRegex(AgentBoxError, "handoff brief is empty"):
            self.run_workflow("review", "1", "--brief", str(self.review_brief))

    def test_starting_review_retires_human_acceptance_before_a_verdict_exists(self):
        """A pending native round or a failed provider must not leave earlier acceptance usable."""
        self.config.workflow.merge_mode = "local"
        self.task.description = "## Acceptance\n- [x] accepted"
        fingerprint = workflow.scope(self.root, "main")
        for flags in (("--native",), ()):
            with self.subTest(flags=flags):
                workflow.save_evidence(self.root, self.task, {
                    "scope": fingerprint, "validation": {"exit": 0}, "review": {"verdict": "PASS"},
                    "human_acceptance": {"scope": fingerprint, "actor": "1", "reason": "User accepted"}})
                with patch.object(workflow, "run", side_effect=timeout):
                    if flags:
                        self.run_workflow("review", "1", *flags)
                    else:
                        with self.assertRaisesRegex(AgentBoxError, "no attempt spent"):
                            self.run_workflow("review", "1")
                saved = workflow.read_evidence(self.root, self.task)
                self.assertNotIn("human_acceptance", saved)
                self.assertNotIn("review", saved)
                with self.assertRaisesRegex(AgentBoxError, "PASS review"):
                    self.run_workflow("finish", "1")


class ReviewerInputTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = patch.object(workflow, "WORKFLOW_STATE", Path(directory.name))
        state.start()
        self.addCleanup(state.stop)

    def test_reviewer_handoff_fails_closed_when_a_regular_instruction_cannot_be_read(self):
        """A verdict must not proceed with missing mandatory repository rules."""
        with tempfile.TemporaryDirectory() as directory:
            root = repository(directory)
            (root / "AGENTS.md").write_text("Mandatory review rule.\n")
            task = Task("1", "1", "Review", description="## Goal\nReview it.\n")
            evidence_path = workflow.review_evidence_path(root, task.id, "unreadable-instruction")
            original_read_text = Path.read_text

            def fail_instruction_read(path, *args, **kwargs):
                if path == root / "AGENTS.md":
                    raise OSError("synthetic read failure")
                return original_read_text(path, *args, **kwargs)

            with patch.object(Path, "read_text", new=fail_instruction_read), \
                 self.assertRaisesRegex(AgentBoxError, "Cannot read repository instruction AGENTS.md"):
                workflow.reviewer_handoff(root, "main", task, task, [task], "Validation: not run.", {}, "",
                                          "unreadable-instruction")
            self.assertFalse(evidence_path.exists())

    def test_reviewer_handoff_points_at_checkout_evidence_without_copying_large_bodies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = repository(directory)
            gw.git(root, "switch", "-c", "ab/1-review")
            instruction_marker = "MANDATORY_RULE_BODY_MUST_NOT_BE_COPIED"
            diff_marker = "LARGE_DIFF_BODY_MUST_NOT_BE_COPIED"
            (root / "AGENTS.md").write_text(instruction_marker + "\n")
            (root / "CLAUDE.md").write_text("@AGENTS.md\n")
            (root / "large.py").write_text(diff_marker + "\n")
            gw.git(root, "add", ".")
            gw.git(root, "commit", "-m", "small review fixture")
            task = Task("1", "1", "Review", description="## Goal\nReview it.\n")
            brief = "Changes and reasons: fixture. Validation: not run. Limitations: synthetic."

            small = workflow.reviewer_handoff(root, "main", task, task, [task], brief, {}, "", "small")
            small_payload = payload_of(small)
            manifest, _ = package(small_payload)
            self.assertEqual([item["source_path"] for item in manifest["repository_instructions"]],
                             ["AGENTS.md", "CLAUDE.md"])
            manifest_path = Path(small_payload["manifest"]["path"])
            self.assertEqual(small_payload["manifest"]["sha256"],
                             hashlib.sha256(manifest_path.read_bytes()).hexdigest())
            diff_path = Path(manifest["artifacts"]["full.diff"]["path"])
            instruction_paths = [Path(item["artifact_path"]) for item in manifest["repository_instructions"]]
            diff_evidence = diff_path.read_text()
            self.assertEqual(manifest["boundaries"], small_payload["boundaries"])
            self.assertEqual(manifest["artifacts"]["full.diff"]["sha256"],
                             hashlib.sha256(diff_evidence.encode()).hexdigest())
            self.assertIn(diff_marker, diff_evidence)
            self.assertEqual(instruction_paths[0].read_text(), instruction_marker + "\n")
            evidence_paths = [Path(item["path"]) for item in manifest["artifacts"].values()]
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o400
                                for path in (manifest_path, *evidence_paths, *instruction_paths)))
            self.assertEqual(small_payload["boundaries"]["head_commit"], gw.head_sha(root))
            self.assertIn("large.py", (manifest_path.parent / "changed-paths.txt").read_text())
            self.assertRegex(small_payload["change_summary"], "files? changed")
            for absent in (instruction_marker, diff_marker, "diff --git"):
                self.assertNotIn(absent, small)

            (root / "large.py").write_text((diff_marker + "\n") * 12000)
            gw.git(root, "add", "large.py")
            gw.git(root, "commit", "-m", "large review fixture")
            large = workflow.reviewer_handoff(root, "main", task, task, [task], brief, {}, "", "large")
            self.assertNotIn(diff_marker, large)
            self.assertLess(abs(len(large) - len(small)), 100)
            large_manifest, _ = package(payload_of(large))
            large_path = Path(large_manifest["artifacts"]["full.diff"]["path"])
            self.assertGreater(len(large_path.read_text()), 100000)
            workflow.cleanup_review_evidence(large_path.parent)
            workflow.cleanup_review_evidence(manifest_path.parent)
            self.assertFalse(large_path.exists())

    def test_reviewer_prompt_is_bounded_with_many_paths_and_oversized_required_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = repository(directory)
            gw.git(root, "switch", "-c", "ab/1-large-review")
            for index in range(250):
                (root / f"changed-{index:03}.txt").write_text(f"change {index}\n")
            gw.git(root, "add", ".")
            gw.git(root, "commit", "-m", "many changed paths")
            requirement_marker = "FULL_NORMATIVE_REQUIREMENT_" * 8000
            author_marker = "UNTRUSTED_AUTHOR_CLAIM_" * 8000
            task = Task("task-" + "i" * 1000, "1", "Title " + "t" * 1000,
                        description="## Goal\n" + requirement_marker + "\n")
            prompt = workflow.reviewer_handoff(root, "main", task, task, [task], author_marker, {}, "", "bounded")
            payload = payload_of(prompt)
            manifest, _ = package(payload)
            self.assertLessEqual(len(prompt.encode()), workflow.REVIEW_HANDOFF_MAX_BYTES)
            for absent in (requirement_marker, author_marker, "changed-042.txt"):
                self.assertNotIn(absent, prompt)
            self.assertIn(requirement_marker, Path(manifest["artifacts"]["task.md"]["path"]).read_text())
            self.assertEqual(Path(manifest["artifacts"]["author-report.md"]["path"]).read_text(), author_marker)
            self.assertEqual(len(Path(manifest["artifacts"]["changed-paths.txt"]["path"]).read_text().splitlines()), 250)
            workflow.cleanup_review_evidence(Path(payload["manifest"]["path"]).parent)

            rejected_path = workflow.review_evidence_path(root, task.id, "rejected")
            with patch.object(workflow, "REVIEW_HANDOFF_MAX_BYTES", 1), \
                 self.assertRaisesRegex(AgentBoxError, "Compact reviewer handoff exceeds"):
                workflow.reviewer_handoff(root, "main", task, task, [task], author_marker, {}, "", "rejected")
            self.assertFalse(rejected_path.exists())


if __name__ == "__main__":
    unittest.main()
