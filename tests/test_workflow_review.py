"""Review rounds: one neutral input, a bounded attempt budget, and reports bound to their round."""
import base64
import hashlib
import io
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from agent_box import git_workflow as gw
from agent_box import provider_runner
from agent_box import workflow
from agent_box import workflow_state
from agent_box.errors import AgentBoxError
from agent_box.repo_binding import Binding
from agent_box.tracker.core import Task
from workflow_fixtures import (BINDING, WorkflowCase, candidate, managed_runner, repository,
                               selected_route)


class ReviewWorkflowTests(WorkflowCase):

    def test_review_input_damage_blocks_both_backends_without_consuming(self):
        """Native and managed reviewers require the same intact package before dispatch or acceptance."""
        for damage in ("missing", "modified", "symlink", "replaced-package", "unknown-file"):
            with self.subTest(damage=damage):
                prepared = self.prepare_review_input()
                descriptor = json.loads(base64.urlsafe_b64decode(prepared["handoff"]))
                path = self.delegation_input(prepared)
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
                before = workflow.read_evidence(self.root, self.task)
                review_state = workflow.read_review_state(self.root, BINDING, self.task)
                with patch.object(workflow, "execute") as execute:
                    for managed in (False, True):
                        with self.assertRaisesRegex(AgentBoxError, "[Dd]elegation input"):
                            self.consume_review_input(prepared, managed=managed)
                    execute.assert_not_called()
                self.assertEqual(workflow.read_evidence(self.root, self.task), before)
                self.assertEqual(workflow.read_review_state(self.root, BINDING, self.task), review_state)
                if damage in ("replaced-package", "unknown-file"):
                    with self.assertRaisesRegex(AgentBoxError, "replaced|Unknown delegation input"):
                        workflow.cleanup_delegation_input(self.root, descriptor)
                    self.assertEqual(path.read_bytes(), original)
                    if damage == "unknown-file":
                        notes = path.parent / "user-notes.txt"
                        self.assertEqual(notes.read_text(), "keep")
                        notes.unlink()
                        self.consume_review_input(prepared, managed=False)
                        self.assertFalse(path.parent.exists())
                else:
                    workflow.cleanup_delegation_input(self.root, descriptor)
                    self.assertFalse(path.parent.exists())
                    if damage == "symlink":
                        self.assertEqual(target.read_bytes(), original)

    def test_managed_reviews_use_exact_bytes_and_clean_only_owned_packages(self):
        """Failures retire each started input; replacement packages and unknown files remain untouched."""
        packages = set()
        for outcome in ("success", "failure", "symlink", "replaced-package", "unknown-file"):
            with self.subTest(outcome=outcome):
                prepared = self.prepare_review_input()
                path = self.delegation_input(prepared)
                self.assertNotIn(path.parent, packages)
                packages.add(path.parent)
                raw = path.read_bytes()
                target = path.parent.with_name(path.parent.name + "-original")

                def provider(route, actual_role, root, prompt, **kwargs):
                    self.assertEqual(actual_role, "reviewer")
                    self.assertEqual(prompt.encode("utf-8"), raw)
                    if outcome == "failure":
                        raise provider_runner.ProviderFailure("mock timeout", "timeout")
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
                    return {"verdict": "PASS", "summary": "Checked", "findings": []}

                with patch.object(workflow, "run", side_effect=managed_runner(provider)) as runner:
                    if outcome == "success":
                        self.consume_review_input(prepared, managed=True)
                    elif outcome != "failure":
                        result = self.consume_review_input(prepared, managed=True)
                        self.assertEqual(result["reconciliation"]["status"], "invalid_input")
                        self.assertTrue(Path(result["result_path"]).exists())
                        self.assertNotIn("review", workflow.read_evidence(self.root, self.task))
                    else:
                        with self.assertRaises(AgentBoxError):
                            self.consume_review_input(prepared, managed=True)
                        self.assertNotIn("review", workflow.read_evidence(self.root, self.task))
                    self.assertEqual(runner.call_count, 1)
                if outcome == "symlink":
                    self.assertTrue(path.is_symlink())
                elif outcome in ("replaced-package", "unknown-file"):
                    self.assertEqual(path.read_bytes(), raw)
                    if outcome == "unknown-file":
                        self.assertEqual((path.parent / "user-notes.txt").read_text(), "keep")
                else:
                    self.assertFalse(path.parent.exists())
                    if outcome == "symlink":
                        self.assertEqual(target.read_bytes(), raw)
                with patch.object(workflow, "execute") as execute:
                    with self.assertRaisesRegex(AgentBoxError, "stale|already started"):
                        self.consume_review_input(prepared, managed=True)
                    execute.assert_not_called()

    def test_unpublished_review_input_is_cleaned_after_write_failure(self):
        """A descriptor write failure must not strand an unpublished input."""
        write = workflow.atomic_write_text

        def fail(path, *args, **kwargs):
            if path.name == "descriptor.json":
                raise OSError("disk full")
            return write(path, *args, **kwargs)
        with patch.object(workflow, "atomic_write_text", side_effect=fail):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.prepare_review_input()
        self.assertFalse(list(workflow.WORKFLOW_STATE.rglob("handoff.txt")))

    def test_reviewer_models_prepare_and_report_bind_real_candidates_without_fabricating_a_tier(self):
        """Config changes cannot rewrite a prepared review's parameters or admit stale reports."""
        self.patches[5].stop()  # Exercise the real route, not the generic handoff fixture.
        self.task.labels = []
        self.config.workflow.review.models = [candidate("claude", "claude-global", "high"),
                                              candidate("codex", "codex-global", "medium")]
        with patch.object(workflow.shutil, "which", side_effect=lambda name: name):
            prepared = self.run_workflow("prepare", "1", "--role", "reviewer", "--primary", "claude",
                                         "--brief", str(self.review_brief))
        route = prepared["route"]
        self.assertEqual((route["provider"], route["model"], route["tier"]), ("claude", "claude-global", None))
        self.assertEqual(route["native_profile"], "ab-reviewer")
        self.assertFalse(route.get("managed_only"))
        self.assertEqual([fallback["model"] for fallback in route["fallbacks"]], ["codex-global"])
        self.config.workflow.review.models[0]["model"] = "changed-after-prepare"
        self.report.write_text(json.dumps({"verdict": "PASS", "summary": "Reviewed", "findings": []}))
        argv = ["report", "1", "--role", "reviewer", "--scope", prepared["scope"],
                "--handoff", prepared["handoff"], "--file", str(self.report)]
        with self.assertRaisesRegex(AgentBoxError, "outside the configured route"):
            self.run_workflow(*argv, "--provider", "claude", "--model", "changed-after-prepare", "--effort", "high", "--fresh-context")
        with self.assertRaisesRegex(AgentBoxError, "requires managed CLI execution"):
            self.run_workflow(*argv, "--provider", "codex", "--model", "codex-global", "--effort", "medium", "--fresh-context")
        with self.assertRaisesRegex(AgentBoxError, "fresh-context"):
            self.run_workflow(*argv, "--provider", "claude", "--model", "claude-global", "--effort", "high")
        result = self.run_workflow(*argv, "--provider", "claude", "--model", "claude-global", "--effort", "high", "--fresh-context")
        self.assertEqual(result["report"]["verdict"], "PASS")
        self.assertIsNone(result["route"]["tier"])
        self.assertEqual(self.task.labels, [])

    def test_the_task_tier_selects_reviewer_models_only_while_tiers_are_on(self):
        """Tiers off run the role's own list for a tier-labelled task; with no list, the session is inherited."""
        self.patches[5].stop()
        self.task.labels = ["ab:tier:2"]
        self.config.workflow.review.max_rounds = 30
        self.config.workflow.review.policy = "always"
        review = self.config.workflow.review
        for provider in ("codex", "claude"):
            for use_tiers, models, tier_list, expected in ((False, False, True, "session"), (False, True, True, "global"),
                                                           (True, False, True, "tier"), (True, True, True, "tier"),
                                                           (True, True, False, "global")):
                self.config.workflow.use_tiers = use_tiers
                review.models = [candidate(provider, provider + "-global", "default")] if models else []
                review.tiers["2"] = [candidate(provider, provider + "-tier", "high")] if tier_list else []
                argv = ["review", "1", "--primary", provider, "--brief", str(self.review_brief),
                        "--session-model", provider + "-session", "--session-effort", "medium"]
                with self.subTest(provider=provider, use_tiers=use_tiers, models=models, tier_list=tier_list), \
                     patch.object(workflow.shutil, "which", side_effect=lambda name: name), \
                     patch.object(workflow, "run", return_value={"verdict": "PASS", "summary": "Reviewed", "findings": []}) as run:
                    result = self.run_workflow(*argv)
                    route = run.call_args.args[0]
                    self.assertEqual(route["model"], provider + "-" + expected)
                    self.assertEqual(route["tier"], 2 if use_tiers and expected != "session" else None)
                    if expected == "global":
                        self.assertEqual(route["effort"], "default")
                        cmd = provider_runner.build_command(route, "reviewer", self.root, self.report, self.report)
                        self.assertNotIn("--effort", cmd)
                        self.assertFalse(any("model_reasoning_effort=" in arg for arg in cmd))
                    self.assertEqual(result["report"]["verdict"], "PASS")

    def test_untiered_native_review_requires_inheritance_and_fresh_context(self):
        """Unknown session settings stay unknown, with explicit coordinator evidence."""
        self.task.labels = []
        self.task.description = "## Acceptance\n- [x] requirement"
        self.config.workflow.review.policy = "always"
        fingerprint = workflow.scope(self.root, "main", [self.task])
        workflow.save_evidence(self.root, self.task, {
            "scope": fingerprint, "review": {"verdict": "PASS"}, "validation": {"exit": 0}})
        with patch.object(workflow, "route", side_effect=AssertionError("matrix consulted")), \
             patch.object(workflow, "run", side_effect=AssertionError("provider launched")):
            prepared = self.run_workflow("review", "1", "--primary", "codex",
                                         "--brief", str(self.review_brief))
        self.assertEqual(prepared["action"], "review-in-fresh-context")
        self.assertIn("--session-inherited --fresh-context", prepared["report_command"])
        self.assertIn("--session-model MODEL --session-effort EFFORT", prepared["instructions"])
        self.assertIn("cannot run managed", prepared["instructions"])
        self.assertIn(workflow.LAUNCH_CONTEXT, prepared["instructions"])
        self.assertNotIn("review", workflow.read_evidence(self.root, self.task))
        self.report.write_text(json.dumps({"verdict": "PASS", "summary": "Reviewed", "findings": []}))
        argv = ["report", "1", "--role", "reviewer", "--provider", "codex",
                "--scope", prepared["scope"], "--handoff", prepared["handoff"], "--file", str(self.report)]
        with self.assertRaisesRegex(AgentBoxError, "session-inherited"):
            self.run_workflow(*argv)
        with self.assertRaisesRegex(AgentBoxError, "fresh-context"):
            self.run_workflow(*argv, "--session-inherited")
        with self.assertRaisesRegex(AgentBoxError, "provider differs"):
            self.run_workflow(*argv, "--provider", "claude", "--session-inherited", "--fresh-context")
        with self.assertRaisesRegex(AgentBoxError, "PASS review"):
            self.run_workflow("finish", "1")
        result = self.run_workflow(*argv, "--session-inherited", "--fresh-context")
        self.assertEqual(result["report"]["verdict"], "PASS")
        self.assertIsNone(result["route"]["model"])
        self.assertIsNone(result["route"]["effort"])
        self.assertTrue(result["route"]["fresh_context"])

    def test_untiered_review_with_verified_settings_uses_fresh_managed_runner(self):
        """Concrete settings permit a CLI review without consulting reviewer tiers."""
        self.task.labels = []
        self.config.workflow.review.policy = "always"
        with patch.object(workflow, "route", side_effect=AssertionError("matrix consulted")), \
             patch.object(workflow, "run", return_value={"verdict": "PASS", "summary": "Reviewed", "findings": []}) as run:
            result = self.run_workflow("review", "1", "--primary", "codex",
                                       "--session-model", "session-only", "--session-effort", "high",
                                       "--brief", str(self.review_brief))
        self.assertEqual(run.call_args.args[0]["model"], "session-only")
        self.assertEqual(run.call_args.args[0]["provider"], "codex")
        self.assertEqual(result["report"]["verdict"], "PASS")
        self.assertIsNone(result["route"]["tier"])

    def test_untiered_review_policy_skip_needs_no_session_settings(self):
        """Disabled review remains SKIPPED instead of requesting execution facts."""
        self.task.labels = []
        self.config.workflow.review.policy = "off"
        self.assertEqual(self.run_workflow("review", "1")["review"]["verdict"], "SKIPPED")

    def test_untiered_session_facts_must_be_complete(self):
        """Partial facts cannot imply the missing session setting."""
        self.task.labels = []
        for flags in (["--session-model", "session-only"], ["--session-effort", "high"]):
            with self.subTest(flags=flags), self.assertRaisesRegex(AgentBoxError, "Supply both"):
                self.run_workflow("prepare", "1", "--primary", "codex", "--brief", str(self.review_brief), *flags)
        args = workflow.parser().parse_args(["prepare", "1", "--brief", str(self.review_brief)])
        args.primary = None
        with self.assertRaisesRegex(AgentBoxError, "active session provider"):
            workflow.dispatch(args)

    def test_the_cli_reviewer_path_reaches_the_managed_runner_too(self):
        """`workflow review` calls the same runner, so it breaks on the same mismatch."""
        self.config.workflow.review.policy = "always"
        fingerprint = workflow.scope(self.root, "main", [self.task])
        workflow.save_evidence(self.root, self.task, {
            "scope": fingerprint, "human_acceptance": {
                "scope": fingerprint, "actor": "1", "reason": "Earlier human decision"}})

        def provider(route, role, root, prompt, *, timeout, read_paths, diagnostics):
            self.assertEqual(role, "reviewer")
            return {"verdict": "PASS", "summary": "Reads correctly", "findings": []}

        with patch.object(workflow, "run", side_effect=managed_runner(provider)):
            result = self.run_workflow("review", "1", "--primary", "codex",
                                       "--brief", str(self.review_brief))
        self.assertEqual(result["report"]["verdict"], "PASS")
        self.assertEqual(workflow.read_evidence(self.root, self.task)["review"]["verdict"], "PASS")
        self.assertNotIn("human_acceptance", workflow.read_evidence(self.root, self.task))

    def test_native_and_managed_rounds_share_one_neutral_complete_handoff(self):
        """A post-fix verdict gets the updated diff, not the first review's story."""
        self.config.workflow.review.policy = "always"
        self.task.description = (
            "## Goal\nCurrent goal.\n\n"
            "### Compatibility\nKeep the supporting behavior.\n\n"
            "## Context\nPrevious verdict found an obsolete problem.\n\n"
            "## Acceptance\n- [ ] Current behavior works.\n\n"
            "### Edge cases\n- [ ] Fixed values remain bounded.\n\n"
            "## Validation\nRun the focused unittest.\n")
        (self.root / "change.py").write_text("value = 1\n")
        (self.root / "support.py").write_text("stable = True\n")
        gw.git(self.root, "add", ".")
        gw.git(self.root, "commit", "-m", "current implementation")
        first = self.run_workflow("prepare", "1", "--role", "reviewer", "--primary", "codex",
                                  "--brief", str(self.review_brief))
        first_payload = json.loads(self.delegation_input(first).read_bytes().decode("utf-8").split("Delegation handoff:\n", 1)[1])
        first_reads = first_payload["required_read_evidence"]
        first_artifacts = [Path(first_reads["manifest_path"]),
                           *(Path(item["path"]) for item in first_reads["artifacts"].values()),
                           *(Path(item["artifact_path"])
                             for item in first_reads["repository_instructions"])]
        self.assertTrue(all(path.is_file() for path in first_artifacts))
        finding = "value must be bounded before release"
        review = Path(self.directory.name) / "first-review.json"
        review.write_text(json.dumps({
            "verdict": "NEEDS_WORK", "summary": "Fix the boundary", "findings": [{
                "severity": "high", "path": "change.py", "line": 1,
                "message": finding, "evidence": "value is still 1",
                "requirement": "Fixed values remain bounded"}]}))
        result = self.run_workflow(
            "report", "1", "--role", "reviewer",
            "--provider", "claude", "--model", "m", "--effort", "high",
            "--scope", first["scope"], "--handoff", first["handoff"],
            "--file", str(review), "--fresh-context")
        self.assertEqual(result["report"]["verdict"], "NEEDS_WORK")
        self.assertTrue(all(not path.exists() for path in first_artifacts))

        (self.root / "change.py").write_text("value = 2\n")
        gw.git(self.root, "add", ".")
        gw.git(self.root, "commit", "-m", "fix review finding")
        second = self.run_workflow("prepare", "1", "--role", "reviewer", "--primary", "codex",
                                   "--brief", str(self.review_brief))
        self.assertNotEqual(first["scope"], second["scope"])
        self.assertNotEqual(first["handoff"], second["handoff"])
        self.assertNotIn("repository_instructions", second)
        prompt = self.delegation_input(second).read_bytes().decode("utf-8")
        payload = json.loads(prompt.split("Delegation handoff:\n", 1)[1])
        requirements_text = Path(
            payload["required_read_evidence"]["artifacts"]["requirements"]["path"]).read_text()
        author_text = Path(
            payload["required_read_evidence"]["artifacts"]["author_report"]["path"]).read_text()
        self.assertIn("Current goal", requirements_text)
        self.assertIn("Keep the supporting behavior", requirements_text)
        self.assertIn("Current behavior works", requirements_text)
        self.assertIn("Fixed values remain bounded", requirements_text)
        self.assertIn("focused unittest", requirements_text)
        self.assertNotIn("Current goal", prompt)
        self.assertNotIn("Previous verdict", prompt)
        self.assertNotIn(finding, prompt)
        self.assertIn("Changes and reasons", author_text)
        self.assertNotIn("Changes and reasons", prompt)
        self.assertIn(gw.head_sha(self.root), prompt)
        self.assertIn("diff_range", prompt)
        self.assertIn("required_read_evidence", prompt)
        self.assertIn("change.py", prompt)
        self.assertIn("support.py", prompt)
        self.assertNotIn("diff --git", prompt)
        self.assertNotIn("+value = 2", prompt)
        self.assertNotIn("+stable = True", prompt)
        self.assertIn("Untrusted author claims", prompt)
        self.assertIn("Use English for all commentary and the final report; preserve source quotations.", prompt)
        self.assertIn("counterexamples", prompt)
        self.assertIn("Successful tests", prompt)
        self.assertIn("actual consumers and requirements", prompt)
        self.assertIn("current cost of understanding/change", prompt)
        self.assertIn("simpler behavior-preserving alternative", prompt)
        self.assertIn("a runtime bug is not required", prompt)
        self.assertIn("Preserve required error, safety and compatibility checks", prompt)
        self.assertNotIn("without a concrete failure mode", prompt)
        self.assertIn("fork_turns=none", second["instructions"])
        self.assertIn(workflow.LAUNCH_CONTEXT, second["instructions"])
        self.assertIn("never use followup_task or resume", second["instructions"])
        self.adapter.comments.assert_not_called()

        seen = []

        def provider(route, role, root, managed_prompt, *, timeout, read_paths, diagnostics):
            seen.append(managed_prompt)
            return {"verdict": "PASS", "summary": "Reviewed", "findings": []}

        with patch.object(workflow, "run", side_effect=managed_runner(provider)):
            self.run_workflow("review", "1", "--primary", "codex",
                              "--brief", str(self.review_brief))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].split("Delegation handoff:\n", 1)[0],
                         prompt.split("Delegation handoff:\n", 1)[0])
        managed_payload = json.loads(seen[0].split("Delegation handoff:\n", 1)[1])
        self.assertEqual(managed_payload["task_summary"], payload["task_summary"])
        self.assertEqual(managed_payload["boundaries"], payload["boundaries"])
        self.assertEqual(managed_payload["change_summary"], payload["change_summary"])
        self.assertEqual(
            {key: item["sha256"] for key, item in
             managed_payload["required_read_evidence"]["artifacts"].items()},
            {key: item["sha256"] for key, item in
             payload["required_read_evidence"]["artifacts"].items()})
        self.assertNotEqual(
            Path(managed_payload["required_read_evidence"]["manifest_path"]).parent,
            Path(payload["required_read_evidence"]["manifest_path"]).parent)

    def test_same_scope_native_review_rounds_keep_and_clean_their_own_packages(self):
        """Concurrent verdict rounds must not overwrite or retire each other's evidence."""
        self.config.workflow.review.policy = "always"
        first = self.run_workflow(
            "prepare", "1", "--role", "reviewer", "--primary", "codex",
            "--brief", str(self.review_brief))
        second = self.run_workflow(
            "prepare", "1", "--role", "reviewer", "--primary", "codex",
            "--brief", str(self.review_brief))

        def package(prepared):
            payload = json.loads(
                self.delegation_input(prepared).read_bytes().decode("utf-8").split("Delegation handoff:\n", 1)[1])
            reads = payload["required_read_evidence"]
            paths = [Path(reads["manifest_path"]),
                     *(Path(item["path"]) for item in reads["artifacts"].values()),
                     *(Path(item["artifact_path"])
                       for item in reads["repository_instructions"])]
            return paths[0].parent, paths

        first_dir, first_paths = package(first)
        second_dir, second_paths = package(second)
        self.assertEqual(first["scope"], second["scope"])
        self.assertNotEqual(first["handoff"], second["handoff"])
        self.assertNotEqual(first_dir, second_dir)
        self.assertEqual(first_dir.parent, second_dir.parent)
        self.assertTrue(all(path.is_file() for path in (*first_paths, *second_paths)))

        report = Path(self.directory.name) / "same-scope-review.json"
        report.write_text(json.dumps({
            "verdict": "PASS", "summary": "Reviewed independently", "findings": []}))
        def report_round(prepared):
            return self.run_workflow(
                "report", "1", "--role", "reviewer",
                "--provider", "claude", "--model", "m", "--effort", "high",
                "--scope", prepared["scope"], "--handoff", prepared["handoff"],
                "--file", str(report), "--fresh-context")

        retired = report_round(first)
        self.assertEqual(retired["reconciliation"]["status"], "superseded")
        self.assertTrue(Path(retired["result_path"]).exists())
        self.assertNotIn("review", workflow.read_evidence(self.root, self.task))
        self.assertTrue(workflow.review_blockers(
            self.config.workflow.review, [self.task], [], 0,
            workflow.read_evidence(self.root, self.task)))
        self.assertFalse(first_dir.exists())
        self.assertTrue(all(path.is_file() for path in second_paths))
        accepted = report_round(second)
        self.assertEqual(accepted["review_budget"]["used"], 2)
        self.assertNotIn("pending", accepted["review_budget"])
        for cleaned_dir, cleaned_paths in ((first_dir, first_paths), (second_dir, second_paths)):
            self.assertFalse(cleaned_dir.exists())
            self.assertTrue(all(not path.exists() for path in cleaned_paths))
        report.write_text(json.dumps({"verdict": "INCONCLUSIVE", "summary": "Replay", "findings": []}))
        for old in (first, second):
            with self.assertRaisesRegex(AgentBoxError, "Review round is stale"):
                report_round(old)
        self.assertEqual(workflow.read_evidence(self.root, self.task)["review"]["verdict"], "PASS")

    def test_managed_result_cannot_overwrite_newer_native_round(self):
        """A managed provider returning after native prepare cannot restore acceptance."""
        self.config.workflow.review.policy = "always"
        pending = []

        def provider(route, role, root, prompt, *, timeout, read_paths, diagnostics):
            pending.append(self.run_workflow(
                "prepare", "1", "--role", "reviewer", "--primary", "codex",
                "--brief", str(self.review_brief)))
            return {"verdict": "PASS", "summary": "Older managed round", "findings": []}

        with patch.object(workflow, "run", side_effect=managed_runner(provider)):
            result = self.run_workflow("review", "1", "--primary", "codex", "--brief", str(self.review_brief))
        self.assertEqual(result["reconciliation"]["status"], "superseded")
        self.assertTrue(Path(result["result_path"]).exists())
        self.assertNotIn("review", workflow.read_evidence(self.root, self.task))
        prepared = pending[0]
        self.report.write_text(json.dumps({"verdict": "PASS", "summary": "Latest native", "findings": []}))
        result = self.run_workflow(
            "report", "1", "--role", "reviewer",
            "--provider", "claude", "--model", "m", "--effort", "high",
            "--scope", prepared["scope"], "--handoff", prepared["handoff"],
            "--file", str(self.report), "--fresh-context")
        self.assertEqual(result["review_budget"]["used"], 2)
        private = json.loads(Path(result["result_path"]).read_text())
        self.assertEqual(private["output"]["report"]["summary"], "Latest native")

    def test_managed_reviewer_reads_ephemeral_evidence_before_cleanup(self):
        self.config.workflow.review.policy = "always"
        marker = "MANAGED_REVIEW_DIFF_EVIDENCE"
        (self.root / "managed.py").write_text(marker + "\n")
        gw.git(self.root, "add", "managed.py")
        gw.git(self.root, "commit", "-m", "managed evidence fixture")
        pending = self.run_workflow(
            "prepare", "1", "--role", "reviewer", "--primary", "codex",
            "--brief", str(self.review_brief))
        pending_payload = json.loads(
            self.delegation_input(pending).read_bytes().decode("utf-8").split("Delegation handoff:\n", 1)[1])
        pending_path = Path(pending_payload["required_read_evidence"]["manifest_path"]).parent
        artifact_paths = []

        def provider(route, role, root, prompt, *, timeout, read_paths, diagnostics):
            payload = json.loads(prompt.split("Delegation handoff:\n", 1)[1])
            reads = payload["required_read_evidence"]
            artifact_paths.append(Path(reads["manifest_path"]))
            artifact_paths.extend(Path(item["path"]) for item in reads["artifacts"].values())
            artifact_paths.extend(Path(item["artifact_path"])
                                  for item in reads["repository_instructions"])
            self.assertEqual(list(read_paths), [artifact_paths[0].parent])
            self.assertNotEqual(read_paths[0], pending_path)
            self.assertTrue((pending_path / "manifest.json").is_file())
            self.assertTrue(all(path.is_file() for path in artifact_paths))
            self.assertIn(marker, Path(reads["artifacts"]["full_diff"]["path"]).read_text())
            self.assertEqual(json.loads(Path(reads["manifest_path"]).read_text())["boundaries"],
                             payload["boundaries"])
            return {"verdict": "PASS", "summary": "Read exact evidence", "findings": []}

        with patch.object(workflow, "run", side_effect=managed_runner(provider)):
            result = self.run_workflow("review", "1", "--primary", "codex",
                                       "--brief", str(self.review_brief))
        self.assertEqual(result["report"]["verdict"], "PASS")
        self.assertTrue(all(not path.exists() for path in artifact_paths))
        self.assertTrue((pending_path / "manifest.json").is_file())
        self.report.write_text(json.dumps({"verdict": "INCONCLUSIVE", "summary": "Older native", "findings": []}))
        retired = self.run_workflow(
                "report", "1", "--role", "reviewer",
                "--provider", "claude", "--model", "m", "--effort", "high",
                "--scope", pending["scope"], "--handoff", pending["handoff"],
                "--file", str(self.report), "--fresh-context")
        self.assertEqual(retired["reconciliation"]["status"], "superseded")
        self.assertTrue(Path(retired["result_path"]).exists())
        self.assertFalse(pending_path.exists())
        self.assertEqual(workflow.read_evidence(self.root, self.task)["review"]["verdict"], "PASS")
        self.assertEqual(workflow.read_review_budget(self.root, BINDING, self.task, 3)["used"], 2)

    def test_review_requirements_keep_nested_sections_and_refuse_unknown_structure(self):
        description = (
            "## Goal\nShip it.\n### Constraints\nKeep nested detail.\n"
            "## Context\nOld review history.\n"
            "## Acceptance\nWorks.\n#### Examples\nNested example.\n")
        selected = workflow.review_description(description)
        self.assertIn("### Constraints", selected)
        self.assertIn("#### Examples", selected)
        self.assertNotIn("Old review history", selected)
        for unsupported in (
                "Unstructured task body with old history.",
                "## Ziel\nUnsupported language.\n## Context\nOld history."):
            with self.subTest(description=unsupported):
                self.task.description = unsupported
                with self.assertRaisesRegex(AgentBoxError, "recognized Goal, Acceptance, or Validation"):
                    self.run_workflow("prepare", "1", "--role", "reviewer", "--primary", "codex",
                                      "--brief", str(self.review_brief))

    def test_fenced_examples_neither_remove_requirements_nor_import_history(self):
        """Only real H2 sections delimit acceptance, including after misleading fence closers."""
        for marker in ("````", "~~~~"):
            with self.subTest(marker=marker):
                description = (f"## Acceptance\n{marker}markdown\n## Context\n"
                               f"{marker[:3]}\n## Unknown example\n{marker} text\n"
                               f"## More example\n{marker}\nRequired after example.\n"
                               f"## Context\n{marker}\n## Acceptance\n{marker}\nOld history.\n"
                               "  ## Validation ##\nRun checks.\n")
                selected = workflow.review_description(description)
                self.assertIn("Required after example.", selected)
                self.assertIn("Run checks.", selected)
                self.assertNotIn("Old history.", selected)
        with self.assertRaisesRegex(AgentBoxError, "Unclosed Markdown fence"):
            workflow.review_description("## Acceptance\n```\n## Context\nOld history.")

    def test_invalid_review_requirements_do_not_reserve_publish_or_execute(self):
        """Repeated author input errors must leave both usage and existing acceptance intact."""
        self.config.workflow.review.policy = "always"
        for description in ("No headings", "## Acceptance\nWorks\n## Mystery\nUnknown",
                            "## Goal\n```python\nnot closed"):
            self.task.description = description
            fingerprint = workflow.scope(self.root, "main", [self.task])
            saved = {"scope": fingerprint, "review": {"verdict": "PASS"},
                     "human_acceptance": {"reason": "User accepted"}}
            workflow.save_evidence(self.root, self.task, saved)
            for command in ("prepare", "review"):
                with self.subTest(description=description, command=command), \
                     patch.object(workflow, "reserve_review", wraps=workflow.reserve_review) as reserve, \
                     patch.object(workflow, "reviewer_handoff") as handoff, \
                     patch.object(workflow, "run") as provider:
                    args = [] if command == "review" else ["--role", "reviewer"]
                    with self.assertRaises(AgentBoxError):
                        self.run_workflow(command, "1", *args, "--primary", "codex",
                                          "--brief", str(self.review_brief))
                    reserve.assert_not_called()
                    handoff.assert_not_called()
                    provider.assert_not_called()
                    self.assertEqual(workflow.read_evidence(self.root, self.task), saved)
        self.assertFalse(workflow.review_budget_path(self.root, BINDING, self.task).exists())

    def test_known_managed_launch_refusals_precede_reservation(self):
        """Only observed local process constraints may block a round before spending it."""
        self.config.workflow.review.policy = "always"
        for cause in ("unavailable", "unsupported", "busy"):
            with self.subTest(cause=cause), \
                 patch.object(workflow, "reserve_review", wraps=workflow.reserve_review) as reserve, \
                 patch.object(workflow, "reviewer_handoff") as handoff, \
                 patch.object(workflow, "run") as provider:
                with patch.object(provider_runner.shutil, "which", return_value=None if cause == "unavailable" else "cli"):
                    if cause == "unsupported":
                        self.task.labels = []
                        args = ["--session-model", "m", "--session-effort", "high"]
                        with patch.object(workflow, "session_route", return_value={
                                "mode": "current-session", "provider": "other", "model": "m",
                                "effort": "high", "tier": None, "role": "reviewer", "fallbacks": []}), \
                             self.assertRaisesRegex(AgentBoxError, "Unsupported managed provider"):
                            self.run_workflow("review", "1", "--primary", "codex", *args,
                                              "--brief", str(self.review_brief))
                        self.task.labels = ["ab:tier:1"]
                    elif cause == "busy":
                        with workflow_state.lock(self.root, "review"), \
                             self.assertRaisesRegex(AgentBoxError, "Another local review"):
                            self.run_workflow("review", "1", "--primary", "codex",
                                              "--brief", str(self.review_brief))
                    else:
                        with self.assertRaisesRegex(AgentBoxError, "Cannot find claude CLI"):
                            self.run_workflow("review", "1", "--primary", "codex",
                                              "--brief", str(self.review_brief))
                reserve.assert_not_called()
                handoff.assert_not_called()
                provider.assert_not_called()
        self.assertEqual(workflow.read_review_state(self.root, BINDING, self.task), {"used": 0})

    def test_prepared_managed_fallback_spends_once_and_cannot_be_replayed(self):
        """Changing backend is part of the reserved round, even with no remaining allowance."""
        self.config.workflow.review.max_rounds = 1
        with patch.object(workflow, "reserve_review", wraps=workflow.reserve_review) as reserve, \
             patch.object(workflow, "run", return_value={
                 "verdict": "PASS", "summary": "Mock independent review", "findings": []}) as provider:
            prepared = self.prepared_review()
            self.assertEqual(reserve.call_count, 1)
            provider.assert_not_called()
            # Policy off must not skip a round explicitly selected by its descriptor.
            self.config.workflow.review.policy = "off"
            result = self.managed_fallback(prepared)
            self.assertEqual((reserve.call_count, provider.call_count), (1, 1))
            self.assertEqual((result["review_budget"]["used"], result["review_budget"]["remaining"]), (1, 0))
            self.assertIn("Native interface", result["fallback_reason"])
            with self.assertRaisesRegex(AgentBoxError, "stale or already reported"):
                self.managed_fallback(prepared)
            self.assertEqual((reserve.call_count, provider.call_count), (1, 1))
            self.run_workflow("review-budget", "1", "--limit", "2", "--reason", "User grants one new round")
            self.prepared_review()
            self.assertEqual((reserve.call_count, provider.call_count), (2, 1))

    def test_failed_managed_fallback_stays_spent_and_rejects_native_report(self):
        """A provider failure cannot turn a pending descriptor into an unlimited retry token."""
        with patch.object(workflow, "reserve_review", wraps=workflow.reserve_review) as reserve, \
             patch.object(workflow, "run", side_effect=provider_runner.ProviderFailure("mock failure", "timeout")) as provider:
            prepared = self.prepared_review()
            workflow.save_evidence(self.root, self.task, {
                "scope": prepared["scope"], "human_acceptance": {"reason": "Accepted while pending"}})
            with self.assertRaisesRegex(AgentBoxError, "No provider produced"):
                self.managed_fallback(prepared)
            with self.assertRaisesRegex(AgentBoxError, "already started managed"):
                self.managed_fallback(prepared)
            self.report.write_text(json.dumps({"verdict": "PASS", "summary": "Late native", "findings": []}))
            with self.assertRaisesRegex(AgentBoxError, "native report is stale"):
                self.run_workflow("report", "1", "--role", "reviewer", "--provider", "claude",
                                  "--model", "m", "--effort", "high", "--fresh-context",
                                  "--scope", prepared["scope"], "--handoff", prepared["handoff"],
                                  "--file", str(self.report))
            self.assertEqual((reserve.call_count, provider.call_count), (1, 1))
            self.assertNotIn("review", workflow.read_evidence(self.root, self.task))
            self.assertNotIn("human_acceptance", workflow.read_evidence(self.root, self.task))

    def test_interrupted_managed_review_reads_back_as_ended_not_as_an_older_round(self):
        """A killed run left no result, and review-result showed the previous verdict
        while pointing at diagnostics that were never written."""
        with patch.object(workflow, "run", return_value={
                "verdict": "PASS", "summary": "Earlier round", "findings": []}):
            self.managed_fallback(self.prepared_review())
        prepared = self.prepared_review()
        with patch.object(workflow, "run", side_effect=provider_runner.ProviderFailure("signal", "interrupted")), \
             self.assertRaisesRegex(AgentBoxError, "interrupted by a signal.*attempt stays spent"):
            self.managed_fallback(prepared)
        self.assertFalse(self.delegation_input(prepared).exists())
        state = workflow.read_review_state(self.root, BINDING, self.task)
        self.assertEqual(state["used"], 2)
        with self.assertRaisesRegex(AgentBoxError, "ended without a saved provider response.*stays spent"):
            self.run_workflow("review-result", "1")
        self.assertEqual(self.run_workflow("inspect", "1")["review"]["pending_round"],
                         {"round": state["pending"], "backend": "managed", "status": "ended_without_response"})
        # The lock is what a live `workflow review` holds for its whole execution.
        with workflow_state.lock(self.root, "review"):
            with self.assertRaisesRegex(AgentBoxError, "still running"):
                self.run_workflow("review-result", "1", "--round=" + state["pending"])
            self.assertEqual(self.run_workflow("inspect", "1")["review"]["pending_round"]["status"], "running")

    def test_managed_fallback_preserves_prepared_prompt_and_artifact_hashes(self):
        """Brief and validation edits cannot rewrite files a native reviewer may still read."""
        prepared = self.prepared_review()
        prompt = self.delegation_input(prepared).read_bytes().decode("utf-8")
        payload = json.loads(prompt.split("Delegation handoff:\n", 1)[1])
        evidence = payload["required_read_evidence"]
        directory = Path(evidence["manifest_path"]).parent
        before = {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
                  for path in directory.iterdir()}
        self.review_brief.write_text("Goal: changed after prepare")
        saved = workflow.read_evidence(self.root, self.task)
        saved["validation"] = {"command": ["changed-after-prepare"], "exit": 0}
        workflow.save_evidence(self.root, self.task, saved)

        def provider(route, role, root, managed_prompt, **kwargs):
            self.assertEqual(managed_prompt, prompt)
            self.assertEqual(hashlib.sha256(managed_prompt.encode()).hexdigest(),
                             hashlib.sha256(prompt.encode()).hexdigest())
            for path, snapshot in before.items():
                self.assertEqual((path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns), snapshot)
            for artifact in evidence["artifacts"].values():
                self.assertEqual(hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest(),
                                 artifact["sha256"])
            return {"verdict": "PASS", "summary": "Mock preserved input", "findings": []}

        with patch.object(workflow, "run", side_effect=managed_runner(provider)) as runner:
            result = self.managed_fallback(prepared)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(result["review_budget"]["used"], 1)
        self.assertFalse(directory.exists())

    def test_provider_fallbacks_share_the_claim_without_new_reservations(self):
        """Runtime provider failures may try configured alternatives once within the same round."""
        selected = selected_route(None, "reviewer", 1, "codex")
        selected["fallbacks"] = [{**selected, "provider": "codex", "fallbacks": []}]
        with patch.object(workflow, "route", return_value=selected), \
             patch.object(workflow, "reserve_review", wraps=workflow.reserve_review) as reserve, \
             patch.object(workflow, "run", side_effect=[
                 provider_runner.ProviderFailure("Mock timeout", "timeout"),
                 {"verdict": "PASS", "summary": "Mock fallback", "findings": []}]) as provider:
            result = self.managed_fallback(self.prepared_review())
        self.assertEqual((reserve.call_count, provider.call_count), (1, 2))
        self.assertEqual([call.args[0]["provider"] for call in provider.call_args_list], ["claude", "codex"])
        self.assertEqual(result["review_budget"]["used"], 1)
        self.assertEqual(result["route"]["provider"], "codex")

    def test_fallback_preflight_failure_keeps_native_round_available(self):
        """An unavailable CLI or unknown inherited settings must not claim the prepared round."""
        prepared = self.prepared_review()
        with patch.object(provider_runner.shutil, "which", return_value=None), \
             patch.object(workflow, "run") as provider, \
             self.assertRaisesRegex(AgentBoxError, "No managed review route"):
            self.managed_fallback(prepared)
        provider.assert_not_called()
        self.assertEqual(workflow.read_review_state(self.root, BINDING, self.task)["pending_backend"], "native")
        self.task.labels = []
        prepared = self.prepared_review()
        with patch.object(workflow, "run") as provider, \
             self.assertRaisesRegex(AgentBoxError, "known model and effort"):
            self.managed_fallback(prepared)
        provider.assert_not_called()
        self.assertEqual(workflow.read_review_state(self.root, BINDING, self.task)["pending_backend"], "native")

    def test_stale_or_wrong_role_descriptor_cannot_start_managed_review(self):
        """Fallback must validate the same exact scope and role as native acceptance."""
        with patch.object(workflow, "run") as provider:
            first = self.prepared_review()
            payload = json.loads(base64.urlsafe_b64decode(first["handoff"]))
            payload["route"] = {**payload["route"], "tier": 3}
            with self.assertRaisesRegex(AgentBoxError, "different role or tier"):
                self.managed_fallback({"handoff": base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()})
            old = self.prepared_review()
            current = self.prepared_review()
            with self.assertRaisesRegex(AgentBoxError, "stale or already reported"):
                self.managed_fallback(old)
            self.task.description += "\nNew requirement"
            with self.assertRaisesRegex(AgentBoxError, "different scope"):
                self.managed_fallback(current)
            provider.assert_not_called()
        self.assertEqual(workflow.read_review_state(self.root, BINDING, self.task)["used"], 3)

    def test_restart_and_concurrent_claim_keep_one_dispatch_per_prepared_round(self):
        """Separate coordinator processes share the durable claim, including after abandonment."""
        prepared = self.prepared_review()
        descriptor = json.loads(base64.urlsafe_b64decode(prepared["handoff"]))
        script = '''
import sys
from pathlib import Path
from agent_box import workflow, workflow_state
from agent_box.errors import AgentBoxError
from agent_box.repo_binding import Binding
from agent_box.tracker.core import Task
workflow.WORKFLOW_STATE = workflow_state.WORKFLOW_STATE = Path(sys.argv[1])
try:
    binding = Binding("vikunja", "https://tasks.example", "1")
    if sys.argv[5] == "claim":
        workflow.claim_managed_review(Path(sys.argv[2]), binding, Task("1", "1", "Task"),
                                      sys.argv[3], sys.argv[4])
    else:
        workflow.reserve_review(Path(sys.argv[2]), binding, Task("2", "1", "Task"),
                                1, sys.argv[3], sys.argv[4])
except AgentBoxError:
    sys.exit(2)
'''
        command = [sys.executable, "-c", script, str(workflow.WORKFLOW_STATE), str(self.root),
                   descriptor["delegation"], prepared["scope"], "claim"]
        processes = [subprocess.Popen(command) for _ in range(2)]
        self.assertEqual(sorted(process.wait(timeout=10) for process in processes), [0, 2])
        self.assertEqual(subprocess.run(command, timeout=10).returncode, 2)
        self.assertEqual(workflow.read_review_state(self.root, BINDING, self.task)["used"], 1)
        with patch.object(workflow, "run") as provider, \
             self.assertRaisesRegex(AgentBoxError, "already started managed"):
            self.managed_fallback(prepared)
        provider.assert_not_called()
        command[-1] = "reserve"
        processes = [subprocess.Popen(command) for _ in range(2)]
        self.assertEqual(sorted(process.wait(timeout=10) for process in processes), [0, 2])
        self.assertEqual(subprocess.run(command, timeout=10).returncode, 2)
        self.assertEqual(workflow.read_review_state(self.root, BINDING, Task("2", "1", "Task"))["used"], 1)

    def test_review_budget_spans_native_managed_scope_changes_and_reports(self):
        """Retries cannot replenish the task budget by changing scope or ingesting a report."""
        self.config.workflow.review.max_rounds = 1
        self.config.workflow.review.policy = "always"
        first = self.run_workflow("prepare", "1", "--role", "reviewer", "--primary", "codex",
                                  "--brief", str(self.review_brief))
        self.assertEqual(first["review_budget"]["used"], 1)
        self.assertNotIn("review_budget", self.delegation_input(first).read_bytes().decode("utf-8"))
        self.report.write_text(json.dumps({"verdict": "PASS", "summary": "Checked", "findings": []}))
        report = self.run_workflow("report", "1", "--role", "reviewer",
                                  "--provider", "claude", "--model", "m", "--effort", "high",
                                  "--fresh-context", "--scope", first["scope"], "--handoff", first["handoff"],
                                  "--file", str(self.report))
        self.assertEqual(report["review_budget"]["used"], 1)
        self.assertEqual(report["review_budget"]["remaining"], 0)
        self.task.description += "\nNew requirement."
        self.run_workflow("review-budget", "1", "--limit", "2", "--reason", "User authorizes retry")
        with patch.object(workflow, "run", side_effect=AgentBoxError("provider unavailable")), \
             self.assertRaisesRegex(AgentBoxError, "provider unavailable"):
            self.run_workflow("review", "1", "--primary", "codex", "--brief", str(self.review_brief))
        budget = self.run_workflow("inspect", "1")["review"]["budget"]
        self.assertEqual((budget["used"], budget["limit"], budget["remaining"]), (2, 2, 0))
        for command, flags in (("prepare", ("--role", "reviewer")), ("review", ())):
            with self.subTest(command=command), patch.object(workflow, "run") as provider, \
                 self.assertRaisesRegex(AgentBoxError, "budget exhausted"):
                self.run_workflow(command, "1", *flags, "--primary", "codex", "--brief", str(self.review_brief))
            provider.assert_not_called()
        self.run_workflow("review-budget", "1", "--limit", "3", "--reason", "User requests one more")
        extra = self.run_workflow("prepare", "1", "--role", "reviewer", "--primary", "codex",
                                  "--brief", str(self.review_brief))
        self.assertEqual(extra["review_budget"]["used"], 3)
        self.assertNotIn("User requests one more", self.delegation_input(extra).read_bytes().decode("utf-8"))
        with self.assertRaisesRegex(AgentBoxError, "budget exhausted"):
            self.run_workflow("prepare", "1", "--role", "reviewer", "--primary", "codex",
                              "--brief", str(self.review_brief))

    def test_review_budget_lock_corruption_and_binding_isolation(self):
        """Concurrent reservation and unreadable counters fail closed; other projects are distinct."""
        with workflow_state.lock(self.root, "review-budget"), \
             self.assertRaisesRegex(AgentBoxError, "Another local review-budget"):
            workflow.reserve_review(self.root, BINDING, self.task, 1, "round", "scope")
        self.assertEqual(workflow.read_review_budget(self.root, BINDING, self.task, 1)["used"], 0)
        workflow.reserve_review(self.root, BINDING, self.task, 1, "round", "scope")
        other = Binding("vikunja", "https://tasks.example", "other")
        self.assertEqual(workflow.read_review_budget(self.root, other, self.task, 1)["used"], 0)
        path = workflow.review_budget_path(self.root, BINDING, self.task)
        for malformed in ("broken", "{}", '{"used":true}', '{"used":-1}'):
            path.write_text(malformed)
            with self.subTest(malformed=malformed), self.assertRaisesRegex(AgentBoxError, "Cannot read review budget"):
                workflow.reserve_review(self.root, BINDING, self.task, 1, "round", "scope")

    def test_per_attempt_override_flag_is_rejected_before_reservation(self):
        """An invocation cannot bypass the persistent absolute ceiling."""
        self.config.workflow.review.policy = "always"
        self.run_workflow("review-budget", "1", "--limit", "0", "--reason", "No further attempts")
        for command, flags in (("prepare", ("--role", "reviewer")), ("review", ())):
            with self.subTest(command=command), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.run_workflow(command, "1", *flags, "--primary", "codex", "--brief", str(self.review_brief),
                                  "--review-override-reason", "Replayable excuse")
            with self.assertRaisesRegex(AgentBoxError, "used=0, limit=0"):
                self.run_workflow(command, "1", *flags, "--primary", "codex", "--brief", str(self.review_brief))
        self.assertEqual(workflow.read_review_state(self.root, BINDING, self.task)["used"], 0)

    def test_failed_budget_write_cannot_dispatch_an_unaccounted_review(self):
        """A failed reservation never publishes a handoff or reaches a provider."""
        self.config.workflow.review.policy = "always"
        self.config.workflow.review.max_rounds = 1
        for command, flags in (("prepare", ("--role", "reviewer")), ("review", ())):
            with self.subTest(command=command), \
                 patch.object(workflow, "save_review_state", side_effect=OSError("disk failure")), \
                 patch.object(workflow, "reviewer_handoff") as handoff, \
                 patch.object(workflow, "run") as provider, \
                 self.assertRaisesRegex(OSError, "disk failure"):
                self.run_workflow(command, "1", *flags, "--primary", "codex",
                                  "--brief", str(self.review_brief))
            handoff.assert_not_called()
            provider.assert_not_called()
        allocated = self.run_workflow("prepare", "1", "--role", "reviewer", "--primary", "codex",
                                      "--brief", str(self.review_brief))
        self.assertEqual(allocated["review_budget"]["used"], 1)
        with self.assertRaisesRegex(AgentBoxError, "budget exhausted"):
            self.run_workflow("prepare", "1", "--role", "reviewer", "--primary", "codex",
                              "--brief", str(self.review_brief))

    def test_absolute_review_budget_is_idempotent_and_preserves_pending_and_acceptance(self):
        """Retrying a budget decision must never grant more attempts or alter review evidence."""
        state = {"used": 3, "pending": "existing-round", "annotation": "Preserved opaque state"}
        workflow.save_review_state(self.root, BINDING, self.task, state)
        accepted = {"scope": workflow.scope(self.root, "main", [self.task]),
                    "validation": {"exit": 0}, "review": {"verdict": "PASS"}}
        workflow.save_evidence(self.root, self.task, accepted)
        for task_state in ("Doing", "Review"):
            self.task.state = task_state
            result = self.run_workflow("review-budget", "1", "--limit", "6", "--reason", "User grants three more")
            budget = result["review_budget"]
            self.assertEqual((budget["used"], budget["configured_limit"], budget["limit"], budget["remaining"]),
                             (3, 3, 6, 3))
            self.assertEqual(workflow.read_review_state(self.root, BINDING, self.task),
                             {**state, "ceiling": 6, "ceiling_reason": "User grants three more"})
            self.assertEqual(workflow.read_evidence(self.root, self.task), accepted)
        self.config.workflow.review.max_rounds = 2
        budget = self.run_workflow("inspect", "1")["review"]["budget"]
        self.assertEqual((budget["configured_limit"], budget["limit"]), (2, 6))
        limited = self.run_workflow("review-budget", "1", "--limit", "0", "--reason", "User stops further attempts")
        self.assertEqual((limited["review_budget"]["used"], limited["review_budget"]["remaining"]), (3, 0))

    def test_absolute_allowance_is_shared_by_native_and_managed_across_scope_changes(self):
        """Three additional attempts means a ceiling of six, even after failure and repeated setters."""
        self.config.workflow.review.policy = "always"
        fingerprint = workflow.scope(self.root, "main", [self.task])
        for attempt in range(3):
            workflow.reserve_review(self.root, BINDING, self.task, 3, f"abandoned-{attempt}", fingerprint)
        self.run_workflow("review-budget", "1", "--limit", "6", "--reason", "THREE_EXTRA_AUTHORIZED")
        for attempt in (4, 5, 6):
            self.task.description += f"\nRequirement {attempt}."
            if attempt == 5:
                with patch.object(workflow, "run", side_effect=AgentBoxError("provider unavailable")), \
                     self.assertRaisesRegex(AgentBoxError, "provider unavailable"):
                    self.run_workflow("review", "1", "--primary", "codex", "--brief", str(self.review_brief))
            else:
                result = self.run_workflow("prepare", "1", "--role", "reviewer", "--primary", "codex",
                                           "--brief", str(self.review_brief))
                self.assertNotIn("THREE_EXTRA_AUTHORIZED", self.delegation_input(result).read_bytes().decode("utf-8"))
                self.assertNotIn("configured_limit", self.delegation_input(result).read_bytes().decode("utf-8"))
            budget = self.run_workflow("review-budget", "1", "--limit", "6", "--reason", "THREE_EXTRA_AUTHORIZED")["review_budget"]
            self.assertEqual((budget["used"], budget["limit"], budget["remaining"]), (attempt, 6, 6 - attempt))
        for command, flags in (("prepare", ("--role", "reviewer")), ("review", ())):
            with self.subTest(command=command), self.assertRaisesRegex(AgentBoxError, "used=6, limit=6"):
                self.run_workflow(command, "1", *flags, "--primary", "codex", "--brief", str(self.review_brief))
        self.report.write_text(json.dumps({"verdict": "PASS", "summary": "Complete review", "findings": []}))
        report = self.run_workflow("report", "1", "--role", "reviewer",
                                  "--provider", "claude", "--model", "m", "--effort", "high", "--fresh-context",
                                  "--scope", result["scope"], "--handoff", result["handoff"], "--file", str(self.report))
        self.assertEqual((report["review_budget"]["used"], report["review_budget"]["limit"]), (6, 6))

    def test_budget_decision_refuses_invalid_input_ownership_and_corrupt_state(self):
        """Only the assigned coordinator can change a valid budget for active work."""
        for limit, reason in (("-1", "User decision"), ("6", " ")):
            with self.subTest(limit=limit), self.assertRaisesRegex(AgentBoxError, "nonnegative limit and non-empty reason"):
                self.run_workflow("review-budget", "1", "--limit", limit, "--reason", reason)
        for state, assignees in (("Done", ["1"]), ("Doing", ["someone-else"]), ("Review", [])):
            self.task.state, self.task.assignees = state, assignees
            with self.subTest(state=state, assignees=assignees), self.assertRaisesRegex(AgentBoxError, "current user's Doing or Review"):
                self.run_workflow("review-budget", "1", "--limit", "6", "--reason", "User decision")
        self.task.state, self.task.assignees = "Doing", ["1"]
        self.assertFalse(workflow.review_budget_path(self.root, BINDING, self.task).exists())
        with workflow_state.lock(self.root, "review-budget"), \
             self.assertRaisesRegex(AgentBoxError, "Another local review-budget"):
            self.run_workflow("review-budget", "1", "--limit", "6", "--reason", "User decision")
        for bad in ({"ceiling": True, "ceiling_reason": "Reason"}, {"ceiling": 6},
                    {"ceiling": -1, "ceiling_reason": "Reason"}):
            workflow.save_review_state(self.root, BINDING, self.task, {"used": 0, **bad})
            with self.subTest(bad=bad), self.assertRaisesRegex(AgentBoxError, "Cannot read review budget"):
                self.run_workflow("review-budget", "1", "--limit", "6", "--reason", "Must not repair corruption")

    def test_policy_skipped_review_does_not_reserve_an_attempt(self):
        """A policy decision is not an independent review attempt."""
        self.config.workflow.review.policy = "off"
        result = self.run_workflow("review", "1", "--primary", "codex")
        self.assertEqual(result["review"]["verdict"], "SKIPPED")
        self.assertEqual(result["review_budget"]["used"], 0)

    def test_review_requirements_accept_explicit_aliases_without_context(self):
        description = (
            "## Goals\nShip the complete change.\n"
            "## Context\nOld discussion must stay out.\n"
            "## Acceptance Criteria\n- [ ] Preserve every requirement.\n"
            "### Edge cases\n- [ ] Keep nested acceptance.\n"
            "## Verification\nRun the focused unittest.\n")
        selected = workflow.review_description(description)
        self.assertIn("Ship the complete change", selected)
        self.assertIn("Preserve every requirement", selected)
        self.assertIn("### Edge cases", selected)
        self.assertIn("focused unittest", selected)
        self.assertNotIn("Old discussion", selected)
        russian = workflow.review_description(
            "## Цели\nВыпустить изменение.\n"
            "## Контекст\nСтарая история.\n"
            "## Критерии приёмки\n- [ ] Сохранить требования.\n"
            "## Проверки\nЗапустить тест.\n")
        self.assertIn("Сохранить требования", russian)
        self.assertIn("Запустить тест", russian)
        self.assertNotIn("Старая история", russian)

    def test_review_requirements_fail_closed_on_partial_unknown_structure(self):
        description = (
            "## Goal\nShip it.\n"
            "## Context\nOld discussion must stay out.\n"
            "## Release Conditions\nDo not lose this hidden requirement.\n"
            "## Validation\nRun the focused unittest.\n")
        self.task.description = description
        with self.assertRaisesRegex(
                AgentBoxError, "Ambiguous level-2 review requirements headings: Release Conditions"):
            self.run_workflow("prepare", "1", "--role", "reviewer", "--primary", "codex",
                              "--brief", str(self.review_brief))

    def test_required_review_refuses_a_missing_or_empty_author_brief(self):
        self.config.workflow.review.policy = "always"
        with self.assertRaisesRegex(AgentBoxError, "Pass --brief"):
            self.run_workflow("prepare", "1", "--role", "reviewer", "--primary", "codex")
        self.review_brief.write_text(" \n")
        with self.assertRaisesRegex(AgentBoxError, "handoff brief is empty"):
            self.run_workflow("review", "1", "--primary", "codex",
                              "--brief", str(self.review_brief))

    def test_starting_review_retires_human_acceptance_before_a_report_exists(self):
        """A pending native review or failed provider must not leave human approval usable."""
        self.config.workflow.merge_mode = "local"
        self.config.workflow.review.policy = "always"
        self.task.description = "## Acceptance\n- [x] accepted"
        fingerprint = workflow.scope(self.root, "main", [self.task])
        for command in ("prepare", "review"):
            with self.subTest(command=command):
                workflow.save_evidence(self.root, self.task, {
                    "scope": fingerprint, "validation": {"exit": 0},
                    "review": {"verdict": "PASS"},
                    "human_acceptance": {"scope": fingerprint, "actor": "1", "reason": "User accepted"}})
                if command == "prepare":
                    self.run_workflow("prepare", "1", "--role", "reviewer", "--primary", "codex",
                                      "--brief", str(self.review_brief))
                else:
                    with patch.object(workflow, "run", side_effect=AgentBoxError("provider unavailable")), \
                         self.assertRaisesRegex(AgentBoxError, "provider unavailable"):
                        self.run_workflow("review", "1", "--primary", "codex",
                                          "--brief", str(self.review_brief))
                self.assertNotIn("human_acceptance", workflow.read_evidence(self.root, self.task))
                self.assertNotIn("review", workflow.read_evidence(self.root, self.task))
                self.assertEqual(self.run_workflow("inspect", "1")["review"]["budget"]["used"],
                                 1 if command == "prepare" else 2)
                with self.assertRaisesRegex(AgentBoxError, "PASS review"):
                    self.run_workflow("finish", "1")

    def test_a_reviewer_reports_at_the_role_and_tier_it_was_prepared_for(self):
        prepared = self.run_workflow("prepare", "1", "--role", "reviewer", "--primary", "codex",
                                     "--brief", str(self.review_brief))
        self.assertEqual(prepared["route"]["role"], "reviewer")
        review = Path(self.directory.name) / "review.json"
        review.write_text(json.dumps({"verdict": "PASS", "summary": "Fine", "findings": []}))
        result = self.run_workflow("report", "1", "--role", "reviewer",
                                   "--provider", "claude", "--model", "m", "--effort", "high", "--file", str(review),
                                   "--scope", prepared["scope"], "--handoff", prepared["handoff"], "--fresh-context")
        self.assertEqual(result["report"]["verdict"], "PASS")



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
            evidence_path = workflow.review_evidence_path(
                root, task.id, "a" * 64, "unreadable-instruction")
            original_read_text = Path.read_text

            def fail_instruction_read(path, *args, **kwargs):
                if path == root / "AGENTS.md":
                    raise OSError("synthetic read failure")
                return original_read_text(path, *args, **kwargs)

            with patch.object(Path, "read_text", new=fail_instruction_read), \
                 self.assertRaisesRegex(AgentBoxError, "Cannot read repository instruction AGENTS.md"):
                workflow.reviewer_handoff(
                    root, "main", [task], "Validation: not run.", {}, "", task.id,
                    "a" * 64, "unreadable-instruction")
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

            small_fingerprint = "a" * 64
            small = workflow.reviewer_handoff(
                root, "main", [task], brief, {}, "", task.id, small_fingerprint, "small")
            small_payload = json.loads(small.split("Delegation handoff:\n", 1)[1])
            reads = small_payload["required_read_evidence"]
            self.assertEqual([item["source_path"] for item in reads["repository_instructions"]],
                             ["AGENTS.md", "CLAUDE.md"])
            manifest_path = Path(reads["manifest_path"])
            diff_path = Path(reads["artifacts"]["full_diff"]["path"])
            instruction_paths = [Path(item["artifact_path"])
                                 for item in reads["repository_instructions"]]
            manifest = json.loads(manifest_path.read_text())
            diff_evidence = diff_path.read_text()
            self.assertEqual(manifest["boundaries"], small_payload["boundaries"])
            self.assertEqual(manifest["artifacts"]["full_diff"]["sha256"],
                             hashlib.sha256(diff_evidence.encode()).hexdigest())
            self.assertEqual([item["sha256"] for item in manifest["repository_instructions"]],
                             [hashlib.sha256(path.read_text().encode()).hexdigest()
                              for path in instruction_paths])
            self.assertIn(diff_marker, diff_evidence)
            self.assertEqual(instruction_paths[0].read_text(), instruction_marker + "\n")
            evidence_paths = [Path(item["path"]) for item in reads["artifacts"].values()]
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o400
                                for path in (manifest_path, *evidence_paths, *instruction_paths)))
            self.assertEqual(small_payload["boundaries"]["head_commit"], gw.head_sha(root))
            self.assertEqual(small_payload["boundaries"]["base_commit"], gw.base_commit(root, "main"))
            self.assertTrue(any("large.py" in item for item in small_payload["change_summary"]["path_sample"]))
            self.assertIn("files changed", small_payload["change_summary"]["shortstat"])
            self.assertNotIn(instruction_marker, small)
            self.assertNotIn(diff_marker, small)
            self.assertNotIn("diff --git", small)

            (root / "large.py").write_text((diff_marker + "\n") * 12000)
            gw.git(root, "add", "large.py")
            gw.git(root, "commit", "-m", "large review fixture")
            large_fingerprint = "b" * 64
            large = workflow.reviewer_handoff(
                root, "main", [task], brief, {}, "", task.id, large_fingerprint, "large")
            self.assertNotIn(diff_marker, large)
            self.assertLess(abs(len(large) - len(small)), 100)
            self.assertTrue(manifest_path.exists())
            large_reads = json.loads(large.split("Delegation handoff:\n", 1)[1])["required_read_evidence"]
            large_path = Path(large_reads["artifacts"]["full_diff"]["path"])
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
            prompt = workflow.reviewer_handoff(
                root, "main", [task], author_marker, {}, "", task.id, "c" * 64, "bounded")
            payload = json.loads(prompt.split("Delegation handoff:\n", 1)[1])
            reads = payload["required_read_evidence"]

            self.assertLessEqual(len(prompt.encode()), workflow.REVIEW_HANDOFF_MAX_BYTES)
            self.assertNotIn(requirement_marker, prompt)
            self.assertNotIn(author_marker, prompt)
            self.assertEqual(payload["change_summary"]["path_count"], 250)
            self.assertEqual(len(payload["change_summary"]["path_sample"]),
                             workflow.REVIEW_SAMPLE_ITEMS)
            self.assertEqual(payload["change_summary"]["paths_omitted"],
                             250 - workflow.REVIEW_SAMPLE_ITEMS)
            self.assertIn("[truncated]", payload["task_summary"]["sample"][0]["title"])
            requirements = Path(reads["artifacts"]["requirements"]["path"]).read_text()
            author = Path(reads["artifacts"]["author_report"]["path"]).read_text()
            changed = Path(reads["artifacts"]["changed_paths"]["path"]).read_text().splitlines()
            self.assertIn(requirement_marker, requirements)
            self.assertEqual(json.loads(requirements), workflow.review_requirements([task]))
            self.assertEqual(author, author_marker)
            self.assertEqual(len(changed), 250)
            workflow.cleanup_review_evidence(Path(reads["manifest_path"]).parent)

            rejected_path = workflow.review_evidence_path(root, task.id, "d" * 64, "rejected")
            with patch.object(workflow, "REVIEW_HANDOFF_MAX_BYTES", 1), \
                 self.assertRaisesRegex(AgentBoxError, "Compact reviewer handoff exceeds"):
                workflow.reviewer_handoff(
                    root, "main", [task], author_marker, {}, "", task.id, "d" * 64,
                    "rejected")
            self.assertFalse(rejected_path.exists())



if __name__ == "__main__":
    unittest.main()
