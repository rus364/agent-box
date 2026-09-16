"""Reviewer settings and native route evidence must describe the actual delegate."""
import json
import base64
import unittest
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent_box import routing, workflow, workflow_state
from agent_box.config import Config
from test_agent_profiles import entry
from agent_box.agent_profiles import sync
from agent_box.errors import AgentBoxError
import test_workflow_handoff as handoff_tests


class NativePreparationTests(unittest.TestCase):
    def test_profile_presence_never_claims_explicit_tool_capabilities(self):
        """Missing, exact and overridden profiles retain independent explicit requirements."""
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            config = Config()
            config.workflow.enabled = True
            config.workflow.review.tiers["1"] = [entry("codex", "review-model", "high"), entry("claude", "other-review", "medium")]
            config.workflow.worker.tiers["1"] = [entry("codex", "worker-model", "low")]
            profile = root / "codex/agents/ab-reviewer-1.toml"
            with patch("agent_box.paths.CODEX_HOME", root / "codex"), \
                 patch("agent_box.paths.CLAUDE_HOME", root / "claude"), \
                 patch.object(workflow, "WORKFLOW_STATE", root / "state"), \
                 patch.object(workflow_state, "WORKFLOW_STATE", root / "state"), \
                 patch.object(workflow.shutil, "which", return_value="provider-cli"):
                for state, ready in (("missing", False), ("exact", True), ("overridden", False)):
                    with self.subTest(state=state):
                        if state == "exact":
                            sync(config, root / "codex", root / "claude")
                        elif state == "overridden":
                            profile.write_text(profile.read_text().replace('"high"', '"low"'))
                        selected = workflow.route(config, "reviewer", 1, "codex", "claude")
                        self.assertEqual(selected["native_profile_ready"], ready)
                        self.assertEqual(selected["native_explicit_settings"], {"provider": "codex", "model": "review-model", "effort": "high"})
                        # The writer's own fallback belongs to the other CLI than this Codex session: managed only.
                        fallback = selected["fallbacks"][0]
                        self.assertTrue(fallback["managed_only"])
                        self.assertIsNone(fallback["native_explicit_settings"])
                        self.assertIn("managed CLI", fallback["native_limitation"])
                config.workflow.review.tiers["1"][0]["effort"] = "default"
                selected = workflow.route(config, "reviewer", 1, "codex", "claude")
                self.assertFalse(selected["native_profile_ready"])
                self.assertIsNone(selected["native_explicit_settings"])


class ReviewerMatrixTests(unittest.TestCase):
    def test_config_show_reports_merge_mode(self):
        from test_workflow_config import shown
        config = Config()
        config.workflow.merge_mode = "local"
        self.assertEqual(shown(config)["workflow.merge_mode"], "local")

    def test_config_show_distinguishes_reviewer_models_from_worker_models(self):
        from test_workflow_config import shown
        config = Config()
        config.workflow.worker.tiers["1"] = [entry("codex", "worker-model", "medium")]
        config.workflow.review.tiers["1"] = [entry("codex", "review-model", "default")]
        rows = shown(config)
        self.assertIn("1 codex:review-model:default;", rows["workflow.review.tiers"])
        self.assertIn("1 codex:worker-model:medium;", rows["workflow.worker.tiers"])

    def test_config_show_reports_review_routing(self):
        """Inspection must reveal strict reviewer routing next to its triggers."""
        from test_workflow_config import shown
        for mode in ("cross-provider", "same-provider"):
            with self.subTest(routing=mode):
                config = Config()
                config.workflow.review.routing = mode
                rows = shown(config)
                self.assertEqual(rows["workflow.review.routing"], mode)
                self.assertEqual((rows["workflow.review.policy"], rows["workflow.review.max_files"],
                                  rows["workflow.review.max_lines"]), ("auto", "5", "200"))

    def test_reviewer_defaults_are_empty_and_have_only_task_tiers(self):
        config = Config()
        self.assertEqual(config.workflow.review.tiers, {"1": [], "2": [], "3": []})
        self.assertEqual(config.workflow.review.models, [])

    def test_review_models_do_not_inherit_workers_and_round_trip_defaults(self):
        config = Config.from_dict({"workflow": {
            "worker": {"tiers": {"2": [{"provider": "claude", "model": "worker"}]}},
            "review": {"tiers": {"2": [
                {"provider": "codex", "model": "fallback", "effort": "high"},
                {"provider": "claude", "api_provider": "", "fast": False, "model": "reviewer", "effort": "default"}]}}}})
        config = Config.from_dict(config.to_dict())
        selected = routing.select(config.workflow, "reviewer", 2, "codex")
        self.assertEqual((selected["model"], selected["effort"]), ("reviewer", "default"))
        self.assertEqual(selected["fallbacks"][0]["model"], "fallback")
        self.assertEqual(config.workflow.worker.tiers["2"][0]["model"], "worker")
        config.workflow.review.tiers["2"] = []
        with self.assertRaisesRegex(AgentBoxError, r"reviewer model configured in workflow\.review\.models"):
            routing.select(config.workflow, "reviewer", 2, "codex")

    def test_reviewer_effort_is_validated_by_provider(self):
        config = Config.from_dict({"workflow": {"review": {"tiers": {"3": [
            {"provider": "claude", "fast": False, "model": "review", "effort": "ultra"}]}}}})
        self.assertTrue(any("workflow.review.tiers.3.0" in e
                            for e in config.workflow.validate()))


class NativeRouteTests(unittest.TestCase):
    setUp = handoff_tests.WorkerHandoffTests.setUp
    run_workflow = handoff_tests.WorkerHandoffTests.run_workflow

    def test_mixed_speed_routes_do_not_put_candidate_specific_flags_in_shared_reports(self):
        """A fast fallback must not impose speed assertions on a default-speed primary."""
        for role in ('reviewer', 'scout', 'worker'):
            for primary_fast in (False, True):
                with self.subTest(role=role, primary_fast=primary_fast):
                    selected = handoff_tests.selected_route(None, role, 1, 'codex')
                    selected.update(model='claude-opus-5', fast=primary_fast, fallbacks=[{
                        'provider': 'codex', 'model': 'gpt-6-astra', 'effort': 'high',
                        'tier': 1, 'fast': not primary_fast}])
                    extra = ['--question', 'Which file implements routing?'] if role == 'scout' else []
                    with patch.object(workflow, 'route', return_value=selected):
                        prepared = self.run_workflow('prepare', '1', '--role', role, '--primary', 'codex',
                                                    '--brief', str(self.review_brief), *extra)
                    self.assertNotIn('--fast ', prepared['report_command'])
                    self.assertIn('candidate that ran requests fast', prepared['instructions'])
                    if role == 'worker':
                        self.run_workflow('report', '1', '--role', role, '--provider', 'claude',
                            '--model', 'claude-opus-5', '--effort', 'high', '--file', str(self.report),
                            '--scope', prepared['scope'], '--handoff', prepared['handoff'],
                            *(['--fast', 'on'] if primary_fast else []))

    def test_missing_profile_does_not_require_cli_when_explicit_spawn_is_enforced(self):
        """Profile readiness is one option, not permission to reject exact tool settings."""
        selected = handoff_tests.selected_route(None, "worker", 1, "codex")
        selected.update(native_profile="ab-worker-1", native_profile_ready=False)
        with patch.object(workflow, "route", return_value=selected):
            prepared = self.run_workflow("prepare", "1", "--primary", "codex")
        self.assertIn("explicit spawn parameters", prepared["instructions"])
        self.assertIn("without an overriding custom agent", prepared["instructions"])
        self.assertIn("unknown or unsupported", prepared["instructions"])
        with patch.object(workflow, "run") as managed:
            self.assertEqual(self.report_route(prepared)["report"]["status"], "completed")
        managed.assert_not_called()

    def test_tiered_native_review_requires_fresh_context_before_acceptance(self):
        """Exact model/effort cannot make a reused reviewer independent."""
        prepared = self.run_workflow("prepare", "1", "--role", "reviewer", "--primary", "codex",
                                     "--brief", str(self.review_brief))
        self.assertNotIn("--fast ", prepared["report_command"])
        self.report.write_text(json.dumps({"verdict": "PASS", "summary": "Fine", "findings": []}))
        with patch.object(workflow, "save_evidence") as save:
            with self.assertRaisesRegex(AgentBoxError, "fresh-context"):
                self.run_workflow("report", "1", "--role", "reviewer", "--provider", "claude",
                                  "--model", "m", "--effort", "high", "--file", str(self.report),
                                  "--scope", prepared["scope"], "--handoff", prepared["handoff"])
            save.assert_not_called()

    def test_native_report_requires_actual_model_and_effort(self):
        """Explicit tiers still reject unknown facts after route-aware parsing."""
        prepared = self.run_workflow("prepare", "1", "--primary", "codex")
        for missing in ("--model", "--effort"):
            argv = ["report", "1", "--provider", "claude", "--model", "m", "--effort", "high",
                    "--scope", prepared["scope"], "--handoff", prepared["handoff"],
                    "--file", str(self.report)]
            index = argv.index(missing)
            del argv[index:index + 2]
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(AgentBoxError, "outside the configured route"):
                    self.run_workflow(*argv)

    def test_native_worker_saves_the_full_route(self):
        prepared = self.run_workflow("prepare", "1", "--primary", "codex")
        result = self.report_route(prepared)
        expected = {"fast": False, "provider": "claude", "model": "m", "effort": "high", "tier": 1}
        self.assertEqual(result["route"], expected)
        self.assertEqual(workflow.read_evidence(self.root, self.task)["worker"]["route"], expected)

    def test_managed_review_fallback_failures_are_kept_with_acceptance(self):
        self.config.workflow.review.policy = "always"
        selected = {"fast": False, "provider": "claude", "model": "m", "effort": "high", "tier": 1,
                    "role": "reviewer", "fallbacks": [
                        {"fast": False, "provider": "codex", "model": "fallback", "effort": "medium", "tier": 1}]}
        failure = workflow.ProviderFailure("Quota exhausted", "quota")
        failure.diagnostic = {"fast": False, "model": "m", "effort": "high", "role": "reviewer",
                              "stage": "envelope", "reason": "provider error envelope",
                              "artifact": "provider-failures/fixture.json", "exit_code": 0,
                              "duration_seconds": 1.25, "stdout_bytes": 7, "stderr_bytes": 0,
                              "usage": None, "total_cost_usd": None, "response_source": "stdout"}
        with patch.object(workflow, "route", return_value=selected), \
             patch.object(workflow, "run", side_effect=[
                 failure,
                 {"verdict": "PASS", "summary": "Fine", "findings": []}]):
            result = self.run_workflow("review", "1", "--primary", "codex",
                                       "--brief", str(self.review_brief))
        saved = workflow.read_evidence(self.root, self.task)["review"]
        self.assertEqual(saved["route"], selected["fallbacks"][0])
        self.assertEqual(saved["failures"], [{"provider": "claude", "category": "quota",
                                               **failure.diagnostic}])
        self.assertEqual(saved["failures"], result["failures"])

    def report_route(self, prepared, *, role="worker", provider="claude", model="m",
                     effort="high", reason=None):
        argv = ["report", "1", "--role", role, "--provider", provider,
                "--model", model, "--effort", effort, "--file", str(self.report),
                "--scope", prepared["scope"], "--handoff", prepared["handoff"]]
        if reason is not None:
            argv += ["--fallback-reason", reason]
        if role == "reviewer":
            argv += ["--fresh-context"]
        return self.run_workflow(*argv)

    def test_wrong_native_route_is_refused_before_acceptance_changes(self):
        for role in ("worker", "reviewer"):
            extra = ["--brief", str(self.review_brief)] if role == "reviewer" else []
            prepared = self.run_workflow("prepare", "1", "--role", role, "--primary", "codex", *extra)
            for change in ({"model": "wrong"}, {"effort": "medium"}, {"provider": "codex"}):
                with self.subTest(role=role, change=change), \
                     patch.object(workflow, "save_evidence") as save:
                    with self.assertRaisesRegex(AgentBoxError, "configured route"):
                        self.report_route(prepared, role=role, **change)
                    save.assert_not_called()

    def test_native_review_cannot_bypass_primary_and_managed_keeps_the_round(self):
        """Prose cannot authorize a backup; the same round must still try the primary CLI."""
        selected = {"fast": False, "provider": "claude", "model": "m", "effort": "high", "tier": 1,
                    "role": "reviewer", "fallbacks": [
                        {"fast": False, "provider": "codex", "model": "fallback", "effort": "medium", "tier": 1}]}
        self.config.workflow.review.max_rounds = 1
        with patch.object(workflow, "route", return_value=selected):
            prepared = self.run_workflow("prepare", "1", "--role", "reviewer", "--primary", "codex",
                                         "--brief", str(self.review_brief))
        report = {"verdict": "PASS", "summary": "Fine", "findings": []}
        self.report.write_text(json.dumps(report))
        saved = workflow.read_evidence(self.root, self.task)
        descriptor = json.loads(base64.urlsafe_b64decode(prepared["handoff"]))
        input_path = Path(descriptor["input"]["path"])
        original_input = input_path.read_text()
        for reason in (None, " ", "Codex is faster", "Claude has no native tool", "Claude unavailable"):
            with self.subTest(reason=reason), patch.object(workflow, "save_evidence") as save:
                with self.assertRaisesRegex(AgentBoxError, "Native reviewer fallback"):
                    self.report_route(prepared, role="reviewer", provider="codex",
                                      model="fallback", effort="medium", reason=reason)
                save.assert_not_called()
            self.assertEqual(workflow.read_evidence(self.root, self.task), saved)
            self.assertEqual(input_path.read_text(), original_input)
        with patch.object(workflow, "run", return_value=report) as run:
            result = self.run_workflow("review", "1", "--handoff", prepared["handoff"],
                                       "--brief", str(self.review_brief), "--fallback-reason",
                                       "Claude native interface unavailable; use its CLI")
        self.assertEqual([call.args[0]["provider"] for call in run.call_args_list], ["claude"])
        self.assertEqual(run.call_args.args[3], original_input)
        self.assertEqual(result["route"]["provider"], "claude")
        self.assertEqual(result["review_budget"]["used"], 1)
        self.assertEqual(result["review_budget"]["remaining"], 0)


if __name__ == "__main__":
    unittest.main()
