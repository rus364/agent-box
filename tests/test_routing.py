"""Selection runs a role's ordered candidates, whatever provider coordinates."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_box import routing, workflow
from agent_box.config import Config, WorkflowConfig, model_entry


def candidate(provider, model, **fields):
    return model_entry({"provider": provider, "model": model, **fields})


class RoutingTests(unittest.TestCase):
    def test_a_claude_coordinator_gets_a_codex_only_scout(self):
        """The reported defect: a codex scout vanished whenever Claude coordinated."""
        c = WorkflowConfig()
        c.scout.models = [candidate("codex", "gpt-5.6-luna", effort="high")]
        result = routing.select(c, "scout", 0, "claude")
        self.assertEqual((result["provider"], result["model"], result["effort"]), ("codex", "gpt-5.6-luna", "high"))

    def test_a_prepared_route_marks_another_clis_candidate_managed_only(self):
        """The coordinator must see that it cannot spawn the candidate natively, and why."""
        config = Config()
        config.workflow.scout.models = [candidate("codex", "gpt-5.6-luna", effort="high"), candidate("claude", "haiku", effort="low")]
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True), \
             patch.object(workflow, "WORKFLOW_STATE", Path(directory)), \
             patch.object(workflow.shutil, "which", side_effect=lambda name: name), \
             patch.object(workflow, "usage_snapshot", return_value={}):
            route = workflow.route(config, "scout", 0, "claude")
        self.assertEqual((route["provider"], route["model"], route["managed_only"]), ("codex", "gpt-5.6-luna", True))
        self.assertIsNone(route["native_explicit_settings"])
        self.assertIn("claude session cannot run a native codex agent", route["native_limitation"])
        fallback = route["fallbacks"][0]
        self.assertFalse(fallback.get("managed_only"))
        self.assertEqual(fallback["native_explicit_settings"], {"provider": "claude", "model": "haiku", "effort": "low"})

    def test_the_first_candidate_wins_and_the_rest_are_ordered_fallbacks(self):
        c = WorkflowConfig()
        c.worker.models = [candidate("claude", "opus"), candidate("codex", "gpt-5.5"), candidate("claude", "sonnet")]
        result = routing.select(c, "worker", 2, "codex")
        self.assertEqual(result["model"], "opus")
        self.assertEqual([f["model"] for f in result["fallbacks"]], ["gpt-5.5", "sonnet"])

    def test_a_nonempty_tier_list_replaces_the_role_models_and_an_empty_one_does_not(self):
        c = WorkflowConfig()
        c.worker.models = [candidate("codex", "gpt-5.5")]
        c.worker.tiers["3"] = [candidate("claude", "opus")]
        self.assertEqual(routing.select(c, "worker", 3, "codex")["model"], "opus")
        self.assertEqual(routing.select(c, "worker", 1, "codex")["model"], "gpt-5.5")
        self.assertIn("workflow.worker.models", routing.select(c, "worker", 1, "codex")["reason"])

    def test_a_missing_cli_or_exhausted_window_skips_to_the_next_candidate(self):
        c = WorkflowConfig()
        c.scout.models = [candidate("codex", "gpt-5.6-luna"), candidate("claude", "haiku")]
        self.assertEqual(routing.select(c, "scout", 0, "codex", available=["claude"])["model"], "haiku")
        usage = {"codex": {"windows": [{"seconds": 18000, "remaining": 0, "reset_at": 2000}]}}
        self.assertEqual(routing.select(c, "scout", 0, "codex", usage, now=1000)["model"], "haiku")
        # A window that has already reset no longer excludes anything.
        self.assertEqual(routing.select(c, "scout", 0, "codex", usage, now=3000)["model"], "gpt-5.6-luna")

    def test_every_candidate_exhausted_is_an_error_not_a_silent_choice(self):
        c = WorkflowConfig()
        c.scout.models = [candidate("codex", "gpt-5.6-luna")]
        usage = {"codex": {"windows": [{"seconds": 18000, "remaining": 0, "reset_at": 2000}]}}
        with self.assertRaisesRegex(Exception, "exhausted"):
            routing.select(c, "scout", 0, "codex", usage, now=1000)

    def test_cross_provider_review_prefers_the_other_writer_provider_whatever_the_list_order(self):
        c = WorkflowConfig()
        c.review.models = [candidate("codex", "gpt-5.5"), candidate("claude", "opus")]
        result = routing.select(c, "reviewer", 2, "codex", writer="codex")
        self.assertEqual(result["provider"], "claude")
        self.assertEqual([f["provider"] for f in result["fallbacks"]], ["codex"])
        c.review.routing = "same-provider"
        result = routing.select(c, "reviewer", 2, "codex", writer="codex")
        self.assertEqual((result["provider"], result["fallbacks"]), ("codex", []))

    def test_no_candidates_for_a_tier_is_an_error_without_a_tier_downgrade(self):
        c = WorkflowConfig()
        with self.assertRaisesRegex(Exception, "workflow.review.models"):
            routing.select(c, "reviewer", 2, "codex", writer="codex")


if __name__ == "__main__":
    unittest.main()
