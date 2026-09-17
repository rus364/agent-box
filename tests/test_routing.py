"""Selection runs a role's ordered candidates, whatever provider coordinates."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_box import launch_options, project_config, routing, workflow
from agent_box.config import Config, WorkflowConfig, model_entry


def candidate(provider, model, **fields):
    return model_entry({"provider": provider, "model": model, **fields})


class RoutingTests(unittest.TestCase):
    def test_a_lone_exhausted_candidate_is_refused_before_launch(self):
        """Usage is read for a single candidate too, so a known exhausted model is not launched to fail."""
        config = Config()
        config.workflow.scout.models = [candidate("codex", "gpt-5.6-luna")]
        exhausted = {"windows": [{"seconds": 18000, "remaining": 0, "reset_at": 4102444800}]}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True), \
             patch.object(workflow, "WORKFLOW_STATE", Path(directory)), \
             patch.object(workflow.shutil, "which", side_effect=lambda name: name), \
             patch.object(workflow, "usage_snapshot", return_value=exhausted) as usage, \
             self.assertRaisesRegex(Exception, "exhausted"):
            workflow.candidates(config, "scout", None)
        usage.assert_called_once_with("codex", no_cache=True)

    def test_a_claude_coordinator_gets_a_codex_only_scout(self):
        """The reported defect: a codex scout vanished whenever Claude coordinated."""
        c = WorkflowConfig()
        c.scout.models = [candidate("codex", "gpt-5.6-luna", effort="high")]
        result = routing.select(c, "scout", None)
        self.assertEqual((result["provider"], result["model"], result["effort"]), ("codex", "gpt-5.6-luna", "high"))

    def test_scouts_have_no_tier(self):
        """A scout tier used to select its own list; scouts now run only workflow.scout.models."""
        c = WorkflowConfig()
        c.scout.models = [candidate("codex", "gpt-5.6-luna")]
        for tier in (0, 1):
            with self.subTest(tier=tier), self.assertRaisesRegex(Exception, "scout has no tier"):
                routing.select(c, "scout", tier)
        with self.assertRaisesRegex(Exception, "worker has no tier 0"):
            routing.select(c, "worker", 0)

    def test_the_first_candidate_wins_and_the_rest_are_ordered_fallbacks(self):
        c = WorkflowConfig()
        c.worker.models = [candidate("claude", "opus"), candidate("codex", "gpt-5.5"), candidate("claude", "sonnet")]
        result = routing.select(c, "worker", 2)
        self.assertEqual(result["model"], "opus")
        self.assertEqual([f["model"] for f in result["fallbacks"]], ["gpt-5.5", "sonnet"])

    def test_a_nonempty_tier_list_replaces_the_role_models_and_an_empty_one_does_not(self):
        c = WorkflowConfig()
        c.worker.models = [candidate("codex", "gpt-5.5")]
        c.worker.tiers["3"] = [candidate("claude", "opus")]
        self.assertEqual(routing.select(c, "worker", 3)["model"], "opus")
        self.assertEqual(routing.select(c, "worker", 1)["model"], "gpt-5.5")
        self.assertIn("workflow.worker.models", routing.select(c, "worker", 1)["reason"])

    def test_a_missing_cli_or_exhausted_window_skips_to_the_next_candidate(self):
        c = WorkflowConfig()
        c.scout.models = [candidate("codex", "gpt-5.6-luna"), candidate("claude", "haiku")]
        self.assertEqual(routing.select(c, "scout", None, available=["claude"])["model"], "haiku")
        # A failed refresh (fresh: false) keeps the cached exhaustion evidence, and it still excludes.
        usage = {"codex": {"fresh": False, "windows": [{"seconds": 18000, "remaining": 0, "reset_at": 2000}]}}
        self.assertEqual(routing.select(c, "scout", None, usage, now=1000)["model"], "haiku")
        # A window that has already reset no longer excludes anything.
        self.assertEqual(routing.select(c, "scout", None, usage, now=3000)["model"], "gpt-5.6-luna")

    def test_an_exhausted_reviewer_candidate_is_skipped_like_any_other_role(self):
        c = WorkflowConfig()
        c.review.models = [candidate("claude", "opus"), candidate("codex", "gpt-5.5")]
        usage = {"claude": {"windows": [{"seconds": 18000, "remaining": 0, "reset_at": 2000}]}}
        result = routing.select(c, "reviewer", None, usage, now=1000)
        self.assertEqual((result["provider"], result["fallbacks"]), ("codex", []))

    def test_every_candidate_exhausted_is_an_error_not_a_silent_choice(self):
        c = WorkflowConfig()
        c.scout.models = [candidate("codex", "gpt-5.6-luna")]
        usage = {"codex": {"windows": [{"seconds": 18000, "remaining": 0, "reset_at": 2000}]}}
        with self.assertRaisesRegex(Exception, "exhausted"):
            routing.select(c, "scout", None, usage, now=1000)

    def test_the_reviewer_is_the_lists_first_candidate_whoever_wrote_the_code(self):
        """The list states the preference; reordering it around the writer's provider was removed."""
        c = WorkflowConfig()
        c.review.models = [candidate("codex", "gpt-5.5"), candidate("claude", "opus")]
        result = routing.select(c, "reviewer", None)
        self.assertEqual(result["provider"], "codex")
        self.assertEqual([f["provider"] for f in result["fallbacks"]], ["claude"])

    def test_no_candidates_is_an_error_naming_the_list(self):
        c = WorkflowConfig()
        with self.assertRaisesRegex(Exception, "workflow.review.models"):
            routing.select(c, "reviewer", None)


class TierSwitchTests(unittest.TestCase):
    """Only the switch decides whether a task's tier selects models; nothing else guesses."""

    def test_tier_selection_follows_global_project_and_session_switch(self):
        base = Config()
        base.workflow.enabled = True
        for role in ("worker", "reviewer"):
            settings = base.workflow.role(role)
            settings.models = [candidate("claude", f"{role}-base")]
            settings.tiers["2"] = [candidate("claude", f"{role}-tier2")]
        section = {"worker": "worker", "reviewer": "review"}
        for saved in (False, True):
            for project in (None, False, True):
                for session in (None, False, True):
                    config = Config.from_dict(base.to_dict())
                    config.workflow.use_tiers = saved
                    if project is not None:
                        config = project_config.apply(config, {"workflow": {"use_tiers": project}})
                    if session is not None:
                        launch_options.apply(config, {"tiers": session})
                    enabled = session if session is not None else project if project is not None else saved
                    for role in ("worker", "reviewer"):
                        for task_tier in (None, 2):
                            with self.subTest(saved=saved, project=project, session=session, role=role, tier=task_tier):
                                tier = config.workflow.effective_tier(task_tier)
                                path, models = config.workflow.models_for(role, tier)
                                tiered = enabled and task_tier == 2
                                self.assertEqual(path, f"workflow.{section[role]}." + ("tiers.2" if tiered else "models"))
                                self.assertEqual(models[0]["model"], f"{role}-" + ("tier2" if tiered else "base"))

    def test_switching_tiers_off_keeps_tier_lists_configured(self):
        """Off means ignored, not deleted: turning it back on restores the same routing."""
        config = Config.from_dict({"workflow": {"worker": {"tiers": {"1": [candidate("codex", "gpt-5.5")]}}}})
        self.assertFalse(config.workflow.use_tiers)
        again = Config.from_dict(config.to_dict())
        self.assertEqual(again.workflow.worker.tiers["1"][0]["model"], "gpt-5.5")
        again.workflow.use_tiers = True
        self.assertEqual(again.workflow.models_for("worker", again.workflow.effective_tier(1))[0], "workflow.worker.tiers.1")


if __name__ == "__main__":
    unittest.main()
