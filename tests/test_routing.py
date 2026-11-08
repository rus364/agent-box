"""Selection runs a role's ordered candidates, whatever provider coordinates."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_box import launch_options, project_config, routing, workflow
from agent_box.config import Config, WorkflowConfig, model_entry
from agent_box.errors import AgentBoxError


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
             self.assertRaisesRegex(AgentBoxError, "workflow.scout.models.*5% usage reserve"):
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

    def test_reserve_skips_candidates_for_every_role_without_reordering_fallbacks(self):
        """Launching at the reserve risks immediate quota failure; surviving candidates keep their preference order."""
        for role in ("worker", "scout", "reviewer", "sync"):
            for reserve in (5, 12):
                c = WorkflowConfig(reserve_percent=reserve)
                c.role(role).models = [candidate("codex", "gpt-5.6-luna"),
                                       candidate("claude", "opus"), candidate("claude", "sonnet")]
                for remaining in (1, reserve, reserve + 0.1):
                    usage = {"codex": {"windows": [
                        {"remaining": 90, "reset_at": 2000},
                        {"remaining": remaining, "reset_at": 2000}]}}
                    with self.subTest(role=role, reserve=reserve, remaining=remaining):
                        result = routing.select(c, role, None, usage, now=1000)
                        expected = ["opus", "sonnet"] if remaining <= reserve else ["gpt-5.6-luna", "opus", "sonnet"]
                        self.assertEqual([result["model"], *[item["model"] for item in result["fallbacks"]]], expected)

    def test_only_applicable_windows_with_future_resets_spend_the_reserve(self):
        """A model-specific restriction must not exclude other models, and an elapsed reset cannot block a launch."""
        c = WorkflowConfig()
        c.worker.models = [candidate("claude", "claude-opus-5"), candidate("claude", "claude-sonnet-5"),
                           candidate("codex", "gpt-5.5")]
        for models, reset, expected in (([], 2000, "gpt-5.5"), (["opus"], 2000, "claude-sonnet-5"),
                                        (["haiku"], 2000, "claude-opus-5"),
                                        (["opus"], 1000, "claude-opus-5"), ([], 999, "claude-opus-5")):
            usage = {"claude": {"windows": [{"remaining": 80, "reset_at": 2000},
                                            {"remaining": 1, "reset_at": reset, "models": models}]}}
            with self.subTest(models=models, reset=reset):
                self.assertEqual(routing.select(c, "worker", None, usage, now=1000)["model"], expected)

    def test_zero_reserve_preserves_the_exhaustion_rule(self):
        """Zero permits any positive remainder and excludes exhaustion only until its reset."""
        c = WorkflowConfig(reserve_percent=0)
        c.scout.models = [candidate("codex", "gpt-5.6-luna"), candidate("claude", "haiku")]
        for remaining in (-1, 0, 0.1, 1, 5):
            for reset in (999, 1000, 1001):
                usage = {"codex": {"windows": [{"remaining": remaining, "reset_at": reset}]}}
                with self.subTest(remaining=remaining, reset=reset):
                    expected = "haiku" if remaining <= 0 and reset > 1000 else "gpt-5.6-luna"
                    self.assertEqual(routing.select(c, "scout", None, usage, now=1000)["model"], expected)

    def test_session_pins_bypass_the_reserve_even_when_exhausted(self):
        """An explicit session model must not be replaced by a subscription headroom policy."""
        c = WorkflowConfig(reserve_percent=99)
        c.scout.models = [candidate("claude", "haiku")]
        c.pins["scout"] = [candidate("codex", "gpt-5.6-luna")]
        usage = {"codex": {"windows": [{"remaining": 0, "reset_at": 2000}]}}
        result = routing.select(c, "scout", None, usage, now=1000)
        self.assertEqual((result["model"], result["fallbacks"]), ("gpt-5.6-luna", []))

    def test_external_api_candidates_do_not_spend_the_clis_subscription_reserve(self):
        """A CLI subscription restriction cannot exclude a separately funded API candidate."""
        c = WorkflowConfig(reserve_percent=99)
        c.worker.models = [candidate("codex", "gpt-5.5"), candidate("codex", "v4", api_provider="deepseek")]
        usage = {"codex": {"windows": [{"remaining": 0, "reset_at": 2000}]}}
        result = routing.select(c, "worker", None, usage, now=1000)
        self.assertEqual((result["model"], result["api_provider"], result["fallbacks"]), ("v4", "deepseek", []))

    def test_every_candidate_below_reserve_is_an_error_naming_the_list_and_reserve(self):
        """A depleted list cannot silently launch; its diagnostic must identify the setting and threshold to fix."""
        for reserve in (0, 5, 12):
            c = WorkflowConfig(reserve_percent=reserve, use_tiers=True)
            c.scout.models = c.worker.tiers["2"] = [candidate("codex", "gpt-5.6-luna"), candidate("claude", "opus")]
            usage = {provider: {"windows": [{"remaining": reserve, "reset_at": 2000}]}
                     for provider in ("codex", "claude")}
            for role, tier, path in (("scout", None, "workflow.scout.models"), ("worker", 2, "workflow.worker.tiers.2")):
                with self.subTest(reserve=reserve, path=path), self.assertRaises(AgentBoxError) as caught:
                    routing.select(c, role, tier, usage, now=1000)
                self.assertIn(path, str(caught.exception))
                self.assertIn(f"{reserve:g}% usage reserve", str(caught.exception))

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
