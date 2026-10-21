"""Workflow-controlled deliveries refuse whole payloads before their consumer sees them."""
import contextlib
import io
import json
from pathlib import Path
from unittest.mock import patch

from agent_box import plan_sync, provider_runner, text_delivery, workflow
from agent_box.errors import AgentBoxError
from test_text_delivery import delivered_bytes
from workflow_fixtures import BINDING, WorkflowCase, answer, candidate


class WorkflowTextDeliveryTests(WorkflowCase):
    def setUp(self):
        super().setUp()
        for role in ("worker", "scout", "sync"):
            self.config.workflow.role(role).models = [candidate("codex", "fixture", "high")]
        self.config.workflow.review.policy = "always"
        self.config.workflow.review.incremental = "off"
        self.adapter.subtree.return_value = ["1"]
        usage = patch.object(workflow, "usage_snapshot", return_value={})
        usage.start()
        self.addCleanup(usage.stop)

    def test_all_assignment_routes_refuse_before_launch_pages_or_round_reservation(self):
        """A limit on a final page alone would still expose oversized assignments and spend money."""
        self.config.workflow.text_max_bytes = 64
        for command in (("delegate", "1", "--role", "worker", "--brief", str(self.review_brief)),
                        ("delegate", "1", "--role", "scout", "--brief", str(self.review_brief)),
                        ("review", "1"), ("sync", "1")):
            for native in ((), ("--native",)):
                with self.subTest(command=command, native=native), patch.object(workflow, "run") as run:
                    with self.assertRaisesRegex(AgentBoxError, "TEXT_LIMIT_EXCEEDED.*tell the user"):
                        self.run_workflow(*command, *native)
                    run.assert_not_called()
                    self.assertFalse(text_delivery.WORKFLOW_TEXTS.exists())
                    self.assertFalse(workflow.read_review_state(self.root, BINDING, self.task).get("pending"))
                    self.assertIsNone(plan_sync.last_result(self.root, self.task))

    def test_review_references_count_with_the_assignment_instead_of_resetting_its_budget(self):
        """Individually acceptable evidence files can exceed the combined delivery budget."""
        self.config.workflow.text_max_bytes = 16000
        self.task.description = "T" * 9000
        self.review_brief.write_text("B" * 9000)
        with patch.object(workflow, "run") as run:
            with self.assertRaisesRegex(AgentBoxError, "including evidence"):
                self.native_round("--brief", str(self.review_brief))
        run.assert_not_called()
        self.assertFalse(list(text_delivery.WORKFLOW_TEXTS.glob("*/page-*.txt")))

    def test_reference_mode_preserves_original_reports_and_default_inline_schema(self):
        """The opt-in changes delivery, never the captured report or successful default fields."""
        args = ("delegate", "1", "--role", "scout", "--brief", str(self.review_brief))
        for report in ("Done", "日本語🙂" * 600):
            with patch.object(workflow, "run", return_value=report):
                inline = self.run_workflow(*args)
                result = self.run_workflow(*args, "--output", "reference")
            self.assertEqual(inline["response"], report)
            self.assertNotIn("response_text", inline)
            self.assertEqual(Path(result["response_path"]).read_bytes(), report.encode())
            if len(report.encode()) > text_delivery.PAGE_BYTES:
                self.assertEqual(delivered_bytes(result["response_text"]), report.encode())
                self.assertNotIn(report, result["response"])
            else:
                self.assertEqual(result["response"], report)
                self.assertNotIn("response_text", result)

    def test_refused_managed_reports_are_saved_without_inline_prefix_or_readable_locator(self):
        """Refusing consumption must preserve diagnostics without rerunning the paid provider."""
        report = "SENSITIVE_REPORT_PREFIX" + "x" * 65536
        for mode in ("inline", "reference"):
            with patch.object(workflow, "run", return_value=report) as run, \
                    contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()):
                code = workflow.main(["delegate", "1", "--role", "scout", "--brief", str(self.review_brief),
                                      "--output", mode])
            self.assertEqual(code, 2)
            self.assertIn("TEXT_LIMIT_EXCEEDED", stdout.getvalue())
            self.assertNotIn("SENSITIVE_REPORT_PREFIX", stdout.getvalue())
            self.assertNotIn("response_path", stdout.getvalue())
            run.assert_called_once()
        self.assertTrue(any(p.read_text() == report for p in workflow.WORKFLOW_STATE.rglob("*.md")))
        self.assertFalse(list(text_delivery.WORKFLOW_TEXTS.glob("*/page-*.txt")))

    def test_fallback_reuses_exact_prepared_input_and_preparation_prunes_expired_records(self):
        """Changing provider cannot reset a delivery's budget or postpone cleanup forever."""
        with patch.object(text_delivery.time, "time", return_value=0):
            old = text_delivery.publish("old", self.config)
        self.config.workflow.scout.models.append(candidate("claude", "fallback", "high"))
        failure = provider_runner.ProviderFailure("unavailable", "fixture missing")
        with patch.object(workflow, "run", side_effect=[failure, "Done"]) as run:
            self.run_workflow("delegate", "1", "--role", "scout", "--brief", str(self.review_brief))
        self.assertEqual(run.call_args_list[0].args[3], run.call_args_list[1].args[3])
        self.assertFalse(Path(old["path"]).exists())

    def test_managed_review_refusal_survives_result_lookup_without_destroying_the_receipt(self):
        """A fresh result and a recovery lookup must enforce the same consumption boundary."""
        report = "Verdict: PASS\n" + "x" * 65536
        with patch.object(workflow, "run", return_value=answer(report)):
            with self.assertRaisesRegex(AgentBoxError, "reviewer report.*original saved"):
                self.run_workflow("review", "1")
        state = workflow.read_review_state(self.root, BINDING, self.task)
        self.assertEqual(state["used"], 1)
        self.assertEqual(state["results"][-1]["status"], "saved")
        with self.assertRaisesRegex(AgentBoxError, "TEXT_LIMIT_EXCEEDED"):
            self.run_workflow("review-result", "1")
        self.assertNotIn("review", workflow.read_evidence(self.root, self.task))
        self.config.workflow.text_max_bytes = 131072
        result = self.run_workflow("review-result", "1", "--reconcile")
        self.assertEqual(json.loads(Path(result["result_path"]).read_text())["output"]["report"]["text"], report)
        self.assertEqual(delivered_bytes(result["report_text"]), report.encode())
        self.assertEqual(result["reconciliation"]["status"], "applied")

    def test_sync_reference_and_overflow_keep_the_saved_report_and_partial_write_status(self):
        """Plan sync may have written tasks before output refusal; never disguise it as safe retry."""
        report = "日本語" * 700 + "\nSync: UNCHANGED\n"
        with patch.object(workflow, "run", return_value=report), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(workflow.main(["sync", "1", "--output", "reference"]), 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(delivered_bytes(result["report_text"]), report.encode())
        self.assertEqual(plan_sync.last_result(self.root, self.task)["report"], report)
        oversized = "x" * 65536 + "\nSync: UPDATED\n"
        with patch.object(workflow, "run", return_value=oversized):
            with self.assertRaisesRegex(AgentBoxError, "task writes may have completed.*TEXT_LIMIT_EXCEEDED"):
                self.run_workflow("sync", "1")
        saved = plan_sync.last_result(self.root, self.task)
        self.assertEqual(saved["status"], "ERROR")
        self.assertEqual(saved["report"], oversized)
        with self.assertRaisesRegex(AgentBoxError, "TEXT_LIMIT_EXCEEDED"):
            self.run_workflow("inspect", "1")
