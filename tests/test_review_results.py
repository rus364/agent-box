"""Completed review responses survive applicability failures without paid retries."""
import json
import base64
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_box import workflow, review_results, git_workflow as gw
from agent_box.errors import AgentBoxError
import test_workflow_handoff as fixtures

BINDING = fixtures.BINDING


class ReviewResultTests(unittest.TestCase):
    setUp = fixtures.WorkerHandoffTests.setUp
    run_workflow = fixtures.WorkerHandoffTests.run_workflow

    def review(self):
        return self.run_workflow("review", "1", "--primary", "codex", "--brief", str(self.review_brief))

    def response(self, verdict="PASS"):
        findings = [] if verdict != "NEEDS_WORK" else [{"severity": "high", "path": "file", "line": 1,
            "message": "private finding", "evidence": "private evidence", "requirement": "acceptance"}]
        return {"verdict": verdict, "summary": "private-completed-marker", "findings": findings}

    def retry(self, result):
        return self.run_workflow("review-result", "1", "--round", result["round"], "--reconcile")

    def prepare(self):
        return self.run_workflow("prepare", "1", "--role", "reviewer", "--primary", "codex",
                                 "--brief", str(self.review_brief))

    def native(self, prepared):
        self.report.write_text(json.dumps(self.response()))
        return self.run_workflow("report", "1", "--role", "reviewer", "--provider", "claude",
            "--model", "m", "--effort", "high", "--fresh-context", "--scope", prepared["scope"],
            "--handoff", prepared["handoff"], "--file", str(self.report))

    def test_related_edit_during_provider_preserves_response_and_one_attempt(self):
        """Informational links cannot discard a completed response or charge a second round."""
        def provider(*args, **kwargs):
            self.task.related.append("99")
            kwargs["diagnostics"].update(usage={"input_tokens": 17}, total_cost_usd=0.12)
            return {"verdict": "PASS", "summary": "private-completed-marker", "findings": []}
        original = workflow.scope(self.root, "main", [self.task])
        with patch.object(workflow, "run", side_effect=provider) as run:
            result = self.review()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(workflow.scope(self.root, "main", [self.task]), original)
        self.assertEqual(workflow.read_review_state(self.root, BINDING, self.task)["used"], 1)
        self.assertEqual(result["reconciliation"]["status"], "applied")
        raw = Path(result["result_path"]).read_text()
        self.assertIn("private-completed-marker", raw)
        self.assertEqual(json.loads(raw)["output"]["diagnostics"]["usage"]["input_tokens"], 17)

    def test_material_change_is_terminal_even_when_refs_or_requirements_return(self):
        """An observed stale round cannot become a free new review by restoring its old inputs."""
        self.config.workflow.review.max_rounds = 4
        for component in ("head", "task:1.description", "task:1.blockers", "task:1.children"):
            with self.subTest(component=component):
                original_head = gw.head_sha(self.root)
                original_description = self.task.description
                def provider(*args, **kwargs):
                    if component == "head":
                        gw.git(self.root, "commit", "--allow-empty", "-m", "changed head")
                    elif component.endswith("description"):
                        self.task.description += "\nNew requirement"
                    elif component.endswith("blockers"):
                        self.task.blockers.append("99")
                    else:
                        # A changed hierarchy is material even before the new child can be fetched.
                        self.task.parent = "99"
                    return self.response()
                with patch.object(workflow, "run", side_effect=provider):
                    result = self.review()
                self.assertEqual(result["reconciliation"]["status"], "stale")
                self.assertIn("task:1.parent" if component.endswith("children") else component,
                              result["reconciliation"]["changes"])
                gw.git(self.root, "reset", "--hard", original_head)
                self.task.description = original_description
                self.task.blockers.clear()
                self.task.parent = None
                with patch.object(workflow, "run") as run, patch.object(workflow, "reserve_review") as reserve:
                    repeated = self.retry(result)
                self.assertEqual(repeated["reconciliation"]["status"], "stale")
                run.assert_not_called()
                reserve.assert_not_called()
                self.assertNotIn("review", workflow.read_evidence(self.root, self.task))
                self.assertTrue(Path(result["result_path"]).exists())

    def test_tracker_failure_and_restart_reconcile_without_provider_or_attempt(self):
        """Once the provider responded, tracker availability affects only reconciliation."""
        def provider(*args, **kwargs):
            self.adapter.get.side_effect = AgentBoxError("private tracker error")
            return self.response()
        with patch.object(workflow, "run", side_effect=provider):
            result = self.review()
        self.assertEqual(result["reconciliation"]["status"], "error")
        offline = self.run_workflow("review-result", "1")
        self.assertEqual(offline["result_path"], result["result_path"])
        self.adapter.get.side_effect = None
        with patch.object(workflow, "run") as run, patch.object(workflow, "reserve_review") as reserve:
            repeated = self.retry(offline)
            with patch.object(workflow, "save_evidence") as save:
                self.retry(repeated)
                save.assert_not_called()
        self.assertEqual(repeated["reconciliation"]["status"], "applied")
        self.assertEqual(repeated["review_budget"]["used"], 1)
        run.assert_not_called()
        reserve.assert_not_called()
        self.assertNotIn("private tracker error", json.dumps(result))
        self.assertNotIn("private-completed-marker", json.dumps(result))
        self.assertNotIn("private-completed-marker", json.dumps(workflow.read_evidence(self.root, self.task)))

    def test_evidence_write_failure_preserves_all_verdicts_and_recovers_once(self):
        """Failure to accept PASS, findings or uncertainty must not lose the actual response."""
        original = workflow.save_evidence
        def fail_acceptance(root, task, value):
            if "review" in value:
                raise OSError("synthetic disk failure")
            original(root, task, value)
        for verdict in ("PASS", "NEEDS_WORK", "INCONCLUSIVE"):
            with self.subTest(verdict=verdict):
                with patch.object(workflow, "run", return_value=self.response(verdict)), \
                     patch.object(workflow, "save_evidence", side_effect=fail_acceptance):
                    result = self.review()
                self.assertEqual(result["reconciliation"]["status"], "error")
                private = json.loads(Path(result["result_path"]).read_text())
                self.assertEqual(private["output"]["report"], self.response(verdict))
                with patch.object(workflow, "run") as run:
                    recovered = self.retry(result)
                run.assert_not_called()
                self.assertEqual(recovered["reconciliation"]["status"], "applied")
                self.assertEqual(workflow.read_evidence(self.root, self.task)["review"]["verdict"], verdict)

    def test_newer_round_cannot_destroy_or_accept_late_native_response(self):
        """A late report is history, while its newer round retains the right to report."""
        first = self.prepare()
        second = self.prepare()
        old = self.native(first)
        self.assertEqual(old["reconciliation"]["status"], "superseded")
        self.assertTrue(Path(old["result_path"]).exists())
        latest = self.native(second)
        self.assertEqual(latest["reconciliation"]["status"], "applied")
        with self.assertRaisesRegex(AgentBoxError, "already reported"):
            self.native(second)
        self.assertEqual(self.retry(old)["reconciliation"]["status"], "superseded")
        self.assertEqual(latest["review_budget"]["used"], 2)
        state = workflow.read_review_state(self.root, BINDING, self.task)
        state.update(pending=old["round"], pending_backend="native")
        state["results"][0]["status"] = "saved"
        workflow.save_review_state(self.root, BINDING, self.task, state)
        self.assertEqual(self.retry(old)["reconciliation"]["status"], "superseded")

    def test_missing_modified_or_replaced_receipts_never_apply(self):
        """A receipt seal detects content edits and same-byte replacements before acceptance."""
        for damage in ("missing", "modified", "replaced"):
            with self.subTest(damage=damage):
                prepared = self.prepare()
                self.adapter.get.side_effect = AgentBoxError("offline")
                result = self.native(prepared)
                self.adapter.get.side_effect = None
                path = Path(result["result_path"])
                raw = path.read_bytes()
                path.unlink()
                if damage != "missing":
                    path.write_bytes(raw + (b" " if damage == "modified" else b""))
                    path.chmod(0o400)
                with self.assertRaisesRegex(AgentBoxError, "missing|modified"):
                    self.retry(result)
                self.assertNotIn("review", workflow.read_evidence(self.root, self.task))

    def test_execution_policy_and_tier_are_conditions_not_fabricated_route_changes(self):
        """Restoring execution conditions reuses the exact route and original response."""
        def provider(*args, **kwargs):
            self.config.workflow.worker.execution = "direct"
            return self.response()
        original = self.config.workflow.worker.execution
        with patch.object(workflow, "run", side_effect=provider):
            result = self.review()
        self.assertEqual(result["reconciliation"]["status"], "blocked")
        self.assertIn("execution_policy", result["reconciliation"]["changes"])
        self.config.workflow.worker.execution = original
        recovered = self.retry(result)
        self.assertEqual(recovered["route"], result["route"])
        self.assertEqual(recovered["reconciliation"]["status"], "applied")

    def test_provider_callback_saves_before_runner_cleanup(self):
        """The real runner's completion hook publishes a receipt while its files still exist."""
        def provider(*args, **kwargs):
            report = self.response()
            kwargs["on_completed"](report)
            state = workflow.read_review_state(self.root, BINDING, self.task)
            record = state["results"][-1]
            value, _ = review_results.read(workflow.WORKFLOW_STATE, workflow.repository_key(self.root), record["receipt"])
            self.assertEqual(value["output"]["report"], report)
            self.assertTrue(Path(value["descriptor"]["input"]["path"]).exists())
            return report
        with patch.object(workflow, "run", side_effect=provider):
            self.assertEqual(self.review()["reconciliation"]["status"], "applied")

    def test_tampered_input_retains_response_without_accepting_or_deleting_replacement(self):
        """Canonical-input corruption is distinct from provider failure and scope drift."""
        prepared = self.prepare()
        descriptor = json.loads(base64.urlsafe_b64decode(prepared["handoff"]))
        path = Path(descriptor["input"]["path"])
        target = Path(self.directory.name) / "user-file"
        target.write_text("user-owned")
        def provider(*args, **kwargs):
            path.unlink()
            path.symlink_to(target)
            return self.response()
        with patch.object(workflow, "run", side_effect=provider):
            result = self.run_workflow("review", "1", "--handoff", prepared["handoff"],
                "--brief", str(self.review_brief), "--fallback-reason", "Native unavailable")
        self.assertEqual(result["reconciliation"]["status"], "invalid_input")
        self.assertEqual(target.read_text(), "user-owned")
        self.assertTrue(path.is_symlink())
        self.assertTrue(Path(result["result_path"]).exists())
        self.assertNotIn("review", workflow.read_evidence(self.root, self.task))

    def test_retention_is_bounded_and_never_deletes_unowned_files(self):
        """Expiry and count limits prune only marked private files, never links or foreign files."""
        with patch.object(workflow, "run", return_value=self.response()):
            result = self.review()
        path = Path(result["result_path"])
        unowned = path.parent / ("f" * 64 + ".json")
        unowned.write_text("not ours")
        target = Path(self.directory.name) / "user-owned"
        target.write_text("keep")
        link = path.parent / ("e" * 64 + ".json")
        link.symlink_to(target)
        with patch.object(review_results, "MAX_FILES", 1), \
             patch.object(workflow, "run", return_value=self.response()):
            newer = self.review()
        self.assertFalse(path.exists())
        self.assertTrue(Path(newer["result_path"]).exists())
        self.assertEqual(unowned.read_text(), "not ours")
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(), "keep")
        self.assertEqual(os.stat(newer["result_path"]).st_mode & 0o777, 0o400)

    def test_information_changes_keep_validation_while_policy_labels_still_trigger_review(self):
        """Metadata is not a test input, but a review-trigger label still has its live consumer."""
        self.run_workflow("validate", "1", "--", "true")
        initial = workflow.scope(self.root, "main", [self.task])
        self.task.related = ["88"]
        self.task.labels += ["ordinary", self.config.workflow.review.label]
        self.assertEqual(workflow.scope(self.root, "main", [self.task]), initial)
        self.assertEqual(workflow.evidence(self.root, self.task, initial)["validation"]["exit"], 0)
        self.assertIn("task 1 has review label " + self.config.workflow.review.label,
                      workflow.review_reasons(self.config.workflow.review, [self.task], [], 0))

    def test_written_evidence_then_state_failure_finalizes_without_second_application(self):
        """A torn final state write leaves the receipt and can finish the first evidence write."""
        original = workflow.save_review_state
        def fail_final(root, binding, task, value):
            if value.get("results") and value["results"][-1]["status"] == "applied":
                raise OSError("synthetic final state failure")
            return original(root, binding, task, value)
        with patch.object(workflow, "run", return_value=self.response()), \
             patch.object(workflow, "save_review_state", side_effect=fail_final):
            result = self.review()
        self.assertEqual(result["reconciliation"]["status"], "error")
        self.assertTrue(Path(result["result_path"]).exists())
        saved = self.run_workflow("review-result", "1")
        with patch.object(workflow, "save_evidence") as write, patch.object(workflow, "run") as run:
            recovered = self.retry(saved)
        write.assert_not_called()
        run.assert_not_called()
        self.assertEqual(recovered["reconciliation"]["status"], "applied")
        self.assertEqual(recovered["review_budget"]["used"], 1)

    def test_expired_and_oversized_receipts_do_not_become_acceptance(self):
        """Retention limits are enforced before reuse; failed persistence preserves canonical input."""
        prepared = self.prepare()
        descriptor = json.loads(base64.urlsafe_b64decode(prepared["handoff"]))
        with patch.object(review_results, "MAX_BYTES", 1):
            with self.assertRaisesRegex(AgentBoxError, "8 MiB"):
                self.native(prepared)
        self.assertTrue(Path(descriptor["input"]["path"]).exists())
        result = self.native(prepared)
        with patch.object(review_results.time, "time", return_value=10**12):
            with self.assertRaisesRegex(AgentBoxError, "expired"):
                self.retry(result)
