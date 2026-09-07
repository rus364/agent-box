"""Reviewer settings and native route evidence must describe the actual delegate."""
import json
import contextlib
import io
import unittest
from unittest.mock import patch

from agent_box import routing, workflow
from agent_box.config import Config
from agent_box.errors import AgentBoxError
import test_workflow_handoff as handoff_tests


class ReviewerMatrixTests(unittest.TestCase):
    def test_config_show_distinguishes_reviewer_models_from_worker_models(self):
        from agent_box import cli
        config = Config()
        config.workflow.tiers["1"]["codex"]["model"] = "worker-model"
        config.workflow.review.tiers["1"]["codex"] = {"model": "review-model", "effort": "default"}
        output = io.StringIO()
        with patch.object(cli, "load_config", return_value=config), \
             patch.object(cli, "_statusline_note", return_value=""), \
             patch.object(cli, "_image_note", return_value=""), \
             patch.object(cli, "_browser_note", return_value=""), contextlib.redirect_stdout(output):
            cli.config_show()
        rows = output.getvalue().splitlines()
        self.assertTrue(any("reviewer tier 1:" in row and "review-model" in row and "default" in row for row in rows))
        self.assertTrue(any("worker/scout tier 1:" in row and "worker-model" in row for row in rows))

    def test_config_show_reports_review_routing_separately_from_worker_routing(self):
        """Inspection must reveal strict reviewer routing even when workers use another mode."""
        from agent_box import cli
        for mode in ("cross-provider", "same-provider"):
            with self.subTest(routing=mode):
                config = Config()
                config.workflow.routing = "weighted"
                config.workflow.review.routing = mode
                output = io.StringIO()
                with patch.object(cli, "load_config", return_value=config), \
                     patch.object(cli, "_statusline_note", return_value=""), \
                     patch.object(cli, "_image_note", return_value=""), \
                     patch.object(cli, "_browser_note", return_value=""), contextlib.redirect_stdout(output):
                    cli.config_show()
                rows = [" ".join(row.split()) for row in output.getvalue().splitlines()]
                self.assertIn(f"review routing {mode}", rows)
                self.assertTrue(any(row.startswith("routing weighted;") for row in rows))
                self.assertIn("review auto; files 5; lines 200", rows)

    def test_reviewer_defaults_are_empty_and_have_only_task_tiers(self):
        config = Config()
        self.assertEqual(set(config.workflow.review.tiers), {"1", "2", "3"})
        for tier in config.workflow.review.tiers.values():
            self.assertEqual(set(tier), {"codex", "claude"})
            self.assertTrue(all(spec == {"model": "", "effort": "medium"} for spec in tier.values()))

    def test_review_models_do_not_inherit_workers_and_round_trip_defaults(self):
        config = Config.from_dict({"workflow": {
            "tiers": {"2": {"claude": {"model": "worker"}}},
            "review": {"tiers": {"2": {
                "claude": {"model": "reviewer", "effort": "default"},
                "codex": {"model": "fallback", "effort": "high"}}}}}})
        config = Config.from_dict(config.to_dict())
        selected = routing.select(config.workflow, "reviewer", 2, "codex", {})
        self.assertEqual((selected["model"], selected["effort"]), ("reviewer", "default"))
        self.assertEqual(selected["fallbacks"][0]["model"], "fallback")
        self.assertEqual(config.workflow.tiers["2"]["claude"]["model"], "worker")
        config.workflow.review.tiers["2"]["claude"]["model"] = ""
        config.workflow.review.tiers["2"]["codex"]["model"] = ""
        with self.assertRaisesRegex(AgentBoxError, r"reviewer model configured in workflow\.review\.tiers\.2"):
            routing.select(config.workflow, "reviewer", 2, "codex", {})

    def test_reviewer_effort_is_validated_by_provider(self):
        config = Config.from_dict({"workflow": {"review": {"tiers": {"3": {
            "claude": {"model": "review", "effort": "ultra"}}}}}})
        self.assertTrue(any("workflow.review.tiers.3.claude" in e
                            for e in config.workflow.validate()))


class NativeRouteTests(unittest.TestCase):
    setUp = handoff_tests.WorkerHandoffTests.setUp
    run_workflow = handoff_tests.WorkerHandoffTests.run_workflow

    def test_native_report_requires_actual_model_and_effort(self):
        for missing in ("--model", "--effort"):
            argv = ["report", "1", "--provider", "claude", "--model", "m", "--effort", "high",
                    "--scope", "scope", "--file", str(self.report)]
            index = argv.index(missing)
            del argv[index:index + 2]
            with self.subTest(missing=missing), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    workflow.parser().parse_args(argv)

    def test_native_worker_saves_the_full_route(self):
        prepared = self.run_workflow("prepare", "1", "--primary", "codex")
        result = self.report_route(prepared)
        expected = {"provider": "claude", "model": "m", "effort": "high", "tier": 1}
        self.assertEqual(result["route"], expected)
        self.assertEqual(workflow.read_evidence(self.root, self.task)["worker"]["route"], expected)

    def test_managed_review_fallback_failures_are_kept_with_acceptance(self):
        self.config.workflow.review.policy = "always"
        selected = {"provider": "claude", "model": "m", "effort": "high", "tier": 1,
                    "role": "reviewer", "fallbacks": [
                        {"provider": "codex", "model": "fallback", "effort": "medium", "tier": 1}]}
        with patch.object(workflow, "route", return_value=selected), \
             patch.object(workflow, "run", side_effect=[
                 workflow.ProviderFailure("Claude unavailable", "unavailable"),
                 {"verdict": "PASS", "summary": "Fine", "findings": []}]):
            result = self.run_workflow("review", "1", "--primary", "codex")
        saved = workflow.read_evidence(self.root, self.task)["review"]
        self.assertEqual(saved["route"], selected["fallbacks"][0])
        self.assertEqual(saved["failures"], [{"provider": "claude", "category": "unavailable"}])
        self.assertEqual(saved["failures"], result["failures"])

    def report_route(self, prepared, *, role="worker", provider="claude", model="m",
                     effort="high", reason=None):
        argv = ["report", "1", "--role", role, "--provider", provider,
                "--model", model, "--effort", effort, "--file", str(self.report),
                "--scope", prepared["scope"], "--handoff", prepared["handoff"]]
        if reason is not None:
            argv += ["--fallback-reason", reason]
        return self.run_workflow(*argv)

    def test_wrong_native_route_is_refused_before_acceptance_changes(self):
        for role in ("worker", "reviewer"):
            prepared = self.run_workflow("prepare", "1", "--role", role, "--primary", "codex")
            for change in ({"model": "wrong"}, {"effort": "medium"}, {"provider": "codex"}):
                with self.subTest(role=role, change=change), \
                     patch.object(workflow, "accept_worker") as accept, \
                     patch.object(workflow, "save_evidence") as save:
                    with self.assertRaisesRegex(AgentBoxError, "configured route"):
                        self.report_route(prepared, role=role, **change)
                    accept.assert_not_called()
                    save.assert_not_called()

    def test_native_review_saves_full_route_and_requires_fallback_reason(self):
        selected = {"provider": "claude", "model": "m", "effort": "high", "tier": 1,
                    "role": "reviewer", "fallbacks": [
                        {"provider": "codex", "model": "fallback", "effort": "medium", "tier": 1}]}
        with patch.object(workflow, "route", return_value=selected):
            prepared = self.run_workflow("prepare", "1", "--role", "reviewer", "--primary", "codex")
        self.report.write_text(json.dumps({"verdict": "PASS", "summary": "Fine", "findings": []}))
        for reason in (None, " "):
            with self.subTest(reason=reason), patch.object(workflow, "save_evidence") as save:
                with self.assertRaisesRegex(AgentBoxError, "fallback reason"):
                    self.report_route(prepared, role="reviewer", provider="codex",
                                      model="fallback", effort="medium", reason=reason)
                save.assert_not_called()
        result = self.report_route(prepared, role="reviewer", provider="codex", model="fallback",
                                   effort="medium", reason="Claude unavailable")
        self.assertEqual(result["route"], selected["fallbacks"][0])
        saved = workflow.read_evidence(self.root, self.task)["review"]
        self.assertEqual(saved["route"], selected["fallbacks"][0])
        self.assertEqual(saved["fallback_reason"], "Claude unavailable")


if __name__ == "__main__":
    unittest.main()
