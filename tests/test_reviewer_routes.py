"""Reviewer settings and native route evidence must describe the actual delegate."""
import unittest
from unittest.mock import patch

from agent_box import routing, workflow
from agent_box.config import Config
from test_agent_profiles import entry
from agent_box.errors import AgentBoxError
import workflow_fixtures


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
        self.assertIn("1 codex:review-model:default speed=inherit;", rows["workflow.review.tiers"])
        self.assertIn("1 codex:worker-model:medium speed=inherit;", rows["workflow.worker.tiers"])

    def test_config_show_reports_the_tier_switch(self):
        """Inspection must reveal whether task tiers select models, since the tier lists stay listed either way."""
        from test_workflow_config import shown
        for enabled in (False, True):
            with self.subTest(use_tiers=enabled):
                config = Config()
                config.workflow.use_tiers = enabled
                self.assertEqual(shown(config)["workflow.use_tiers"], "on" if enabled else "off")

    def test_reviewer_defaults_are_empty_and_have_only_task_tiers(self):
        config = Config()
        self.assertEqual(config.workflow.review.tiers, {"1": [], "2": [], "3": []})
        self.assertEqual(config.workflow.review.models, [])

    def test_review_models_do_not_inherit_workers_and_round_trip_defaults(self):
        config = Config.from_dict({"workflow": {
            "worker": {"tiers": {"2": [{"provider": "claude", "model": "worker"}]}},
            "review": {"tiers": {"2": [
                {"provider": "claude", "api_provider": "", "speed": "inherit", "model": "reviewer", "effort": "default"},
                {"provider": "codex", "model": "fallback", "effort": "high"}]}}}})
        config = Config.from_dict(config.to_dict())
        selected = routing.select(config.workflow, "reviewer", 2)
        self.assertEqual((selected["model"], selected["effort"]), ("reviewer", "default"))
        self.assertEqual(selected["fallbacks"][0]["model"], "fallback")
        self.assertEqual(config.workflow.worker.tiers["2"][0]["model"], "worker")
        config.workflow.review.tiers["2"] = []
        with self.assertRaisesRegex(AgentBoxError, r"reviewer candidate configured in workflow\.review\.models"):
            routing.select(config.workflow, "reviewer", 2)

    def test_reviewer_effort_is_validated_by_provider(self):
        config = Config.from_dict({"workflow": {"review": {"tiers": {"3": [
            {"provider": "claude", "speed": "inherit", "model": "review", "effort": "ultra"}]}}}})
        self.assertTrue(any("workflow.review.tiers.3.0" in e
                            for e in config.workflow.validate()))


class ManagedReviewRouteTests(unittest.TestCase):
    setUp = workflow_fixtures.WorkflowCase.setUp
    run_workflow = workflow_fixtures.WorkflowCase.run_workflow

    def test_managed_review_fallback_failures_are_kept_with_acceptance(self):
        self.config.workflow.review.policy = "always"
        selected = {"speed": "inherit", "provider": "claude", "model": "m", "effort": "high", "tier": 1,
                    "role": "reviewer", "fallbacks": [
                        {"speed": "inherit", "provider": "codex", "model": "fallback", "effort": "medium", "tier": 1}]}
        failure = workflow.ProviderFailure("Quota exhausted", "quota")
        failure.diagnostic = {"speed": "inherit", "model": "m", "effort": "high", "role": "reviewer",
                              "stage": "envelope", "reason": "provider error envelope",
                              "artifact": "provider-failures/fixture.json", "exit_code": 0,
                              "duration_seconds": 1.25, "stdout_bytes": 7, "stderr_bytes": 0,
                              "usage": None, "response_source": "stdout"}
        with patch.object(workflow, "candidates", return_value=selected), \
             patch.object(workflow, "run", side_effect=[failure, workflow_fixtures.answer()]):
            result = self.run_workflow("review", "1", "--brief", str(self.review_brief))
        saved = workflow.read_evidence(self.root, self.task)["review"]
        self.assertEqual(saved["route"], selected["fallbacks"][0])
        self.assertEqual(saved["failures"], [{"provider": "claude", "category": "quota",
                                               **failure.diagnostic}])
        self.assertEqual(saved["failures"], result["failures"])


if __name__ == "__main__":
    unittest.main()
