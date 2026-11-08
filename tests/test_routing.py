"""Selection runs a role's ordered candidates, whatever provider coordinates."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_box import launch_options, project_config, routing, workflow, workflow_state
from agent_box.config import BalanceConfig, Config, WorkflowConfig, model_entry
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


class WeightedRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "balance.json"
        patched = patch.object(workflow_state, "WORKFLOW_STATE", self.path.parent)
        patched.start()
        self.addCleanup(patched.stop)
        self.config = WorkflowConfig(balance=BalanceConfig("weighted", {"claude": 3, "codex": 1}))
        for role in ("worker", "reviewer", "sync", "scout"):
            self.config.role(role).models = [candidate("claude", "opus"), candidate("codex", "gpt"),
                                           candidate("claude", "sonnet")]

    def select(self, role="worker", tier=None, **kwargs):
        return routing.select(self.config, role, tier, **kwargs)

    def test_smooth_proportions_are_deterministic_without_files(self):
        """Three-to-one shares repeat evenly; multiple models never multiply a vendor's allocation."""
        credits = {}
        def allocate(role, tier, weights):
            return workflow_state.weighted_turn(weights, credits)
        results = [self.select(balance=allocate)["provider"] for _ in range(24)]
        self.assertEqual(results, ["claude", "claude", "codex", "claude"] * 6)
        for start in range(len(results) - 3):
            self.assertEqual(results[start:start + 4].count("claude"), 3)
        self.assertFalse(self.path.exists())

    def test_a_full_rotation_returns_credits_to_zero(self):
        """Credits stay bounded, so persisted counters never grow with the number of launches."""
        for weights in ({"a": 3, "b": 1}, {"a": 10**20, "b": 10**20}):
            with self.subTest(weights=weights):
                credits = {}
                selected = [workflow_state.weighted_turn(weights, credits) for _ in range(8)]
                expected = ["a", "a", "b", "a"] * 2 if weights["a"] != weights["b"] else ["a", "b"] * 4
                self.assertEqual(selected, expected)
                self.assertEqual(credits, {"a": 0, "b": 0})

    def test_separate_selections_share_credits_only_for_the_same_role_and_tier(self):
        """Launches resume the saved rotation while another role or effective tier starts its own."""
        for role, tier in (("worker", None), ("worker", 1), ("worker", 2), ("reviewer", 1),
                           ("sync", 1), ("scout", None)):
            with self.subTest(role=role, tier=tier):
                self.assertEqual([self.select(role, tier)["provider"] for _ in range(3)],
                                 ["claude", "claude", "codex"])
        self.assertEqual(set(json.loads(self.path.read_text())["keys"]),
                         {"worker:none", "worker:1", "worker:2", "reviewer:1", "sync:1", "scout:none"})

    def test_weight_changes_reset_credits_before_the_next_choice(self):
        """Old subscription shares must not bias a newly configured rotation."""
        self.select()
        self.select()
        self.config.balance.weights = {"claude": 4, "codex": 1}
        self.assertEqual(self.select()["provider"], "claude")
        self.assertEqual(json.loads(self.path.read_text())["keys"]["worker:none"],
                         {"weights": {"claude": 4, "codex": 1}, "credits": {"claude": -1, "codex": 1}})

    def test_eligible_set_changes_reset_credits_and_ties_follow_the_candidate_list(self):
        """Removing a usable vendor starts a fresh rotation, and JSON key sorting must not break list-order ties."""
        self.config.balance.weights = {"codex": 1, "deepseek": 1, "claude": 1}
        self.config.worker.models.append(candidate("claude", "api", api_provider="deepseek"))
        self.assertEqual(self.select()["provider"], "claude")
        self.config.worker.models.pop()
        self.assertEqual(self.select()["provider"], "claude")
        self.assertEqual(self.select()["provider"], "codex")
        self.config.worker.models = self.config.worker.models[1:] + self.config.worker.models[:1]
        self.assertEqual(self.select()["provider"], "codex")

    def test_only_the_winning_vendors_first_candidate_moves_ahead_of_ordered_fallbacks(self):
        """Weights allocate vendors, then the existing list chooses a model and preserves every backup."""
        self.config.worker.models = [candidate("claude", "unweighted", api_provider="free"),
                                    candidate("codex", "gpt"), candidate("claude", "opus"),
                                    candidate("codex", "backup"), candidate("claude", "sonnet")]
        self.config.balance.weights["free"] = 0
        result = self.select()
        self.assertEqual(result["model"], "opus")
        self.assertEqual([item["model"] for item in result["fallbacks"]], ["unweighted", "gpt", "backup", "sonnet"])
        self.assertIn("Balanced by weights codex=1, claude=3; chose claude", result["reason"])
        del self.config.balance.weights["free"]
        self.assertEqual([self.select()["model"] for _ in range(3)], ["gpt", "opus", "opus"])

    def test_external_vendor_identity_is_independent_of_the_launching_cli(self):
        """Two APIs through one CLI still balance independently, while one API through both CLIs has one share."""
        self.config.balance.weights = {"first": 1, "second": 1}
        self.config.worker.models = [candidate("codex", "a", api_provider="first"),
                                    candidate("claude", "b", api_provider="first"),
                                    candidate("codex", "c", api_provider="second")]
        self.assertEqual([self.select()["model"] for _ in range(4)], ["a", "c", "a", "c"])

    def test_zero_or_one_eligible_vendor_keeps_list_order_and_never_touches_state(self):
        """A lone weighted vendor is no balancing choice, even when an unweighted candidate precedes it."""
        unused = Mock(side_effect=AssertionError("allocation reached"))
        for weights in ({"claude": 0, "codex": 1}, {"unused": 1}):
            self.config.balance.weights = weights
            self.assertEqual(self.select(balance=unused)["model"], "opus")
        unused.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_cli_and_reserve_filters_remove_vendors_before_balancing(self):
        """Missing or depleted subscriptions cannot win or advance a counter when only one vendor remains."""
        self.select()
        before = self.path.read_bytes()
        usage = {"claude": {"windows": [{"remaining": 5, "reset_at": 2000}]}}
        for options in ({"available": ["codex"]}, {"usage": usage, "now": 1000}):
            with self.subTest(options=options):
                self.assertEqual(self.select(**options)["provider"], "codex")
                self.assertEqual(self.path.read_bytes(), before)
        self.config.worker.models[0]["model"] = "opus-only"
        usage["claude"]["windows"][0]["models"] = ["opus-only"]
        self.assertEqual([self.select(usage=usage, now=1000)["model"] for _ in range(2)], ["gpt", "sonnet"])

    def test_pins_and_off_mode_preserve_selection_without_reading_counters(self):
        """Explicit pins and off mode must work even when balancing state is unreadable."""
        self.path.write_text("unreadable counters")
        self.config.pins["worker"] = [candidate("codex", "pinned")]
        self.assertEqual(self.select()["model"], "pinned")
        self.config.pins.clear()
        self.config.balance.mode = "off"
        expected = routing.select(WorkflowConfig.from_dict({"worker": self.config.to_dict()["worker"]}), "worker", None)
        self.assertEqual(self.select(), expected)
        self.assertEqual(self.path.read_text(), "unreadable counters")

    def test_other_vendor_first_balances_independent_reviewers_and_keeps_authors_last(self):
        """Even a heavily weighted author cannot displace the independently balanced group."""
        self.config.review.other_vendor_first = True
        self.config.balance.weights["deepseek"] = 100
        self.config.review.models.insert(0, candidate("claude", "author", api_provider="deepseek"))
        routes = [self.select("reviewer", authors=("deepseek",)) for _ in range(4)]
        self.assertEqual([route["provider"] for route in routes], ["claude", "claude", "codex", "claude"])
        self.assertTrue(all(route["fallbacks"][-1]["model"] == "author" for route in routes))
        self.assertNotIn("deepseek", json.loads(self.path.read_text())["keys"]["reviewer:none"]["weights"])

    def test_authors_balance_when_no_independent_candidate_is_usable(self):
        """An exhausted independent subscription leaves the author group available for review."""
        self.config.review.other_vendor_first = True
        self.config.balance.weights["deepseek"] = 1
        self.config.review.models = [candidate("claude", "other"), candidate("codex", "author1"),
                                    candidate("codex", "author2", api_provider="deepseek")]
        usage = {"claude": {"windows": [{"remaining": 0, "reset_at": 2000}]}}
        routes = [self.select("reviewer", authors=("codex", "deepseek"), usage=usage, now=1000) for _ in range(4)]
        self.assertEqual([route["model"] for route in routes], ["author1", "author2"] * 2)

    def test_an_independent_group_with_fewer_than_two_shares_keeps_authors_last(self):
        """Author shares cannot take a turn from a usable independent opener, even when it has no weight."""
        self.config.review.other_vendor_first = True
        self.config.review.models = [candidate("codex", "author1"), candidate("claude", "other"),
                                    candidate("codex", "author2", api_provider="deepseek")]
        self.config.balance.weights["deepseek"] = 100
        for other_weight in (0, 3):
            self.config.balance.weights["claude"] = other_weight
            self.assertEqual(self.select("reviewer", authors=("codex", "deepseek"))["model"], "other")
            self.assertFalse(self.path.exists())
        self.config.pins["reviewer"] = [self.config.review.models[0]]
        self.assertEqual(self.select("reviewer", authors=("codex", "deepseek"))["model"], "author1")
        self.assertFalse(self.path.exists())

    def test_managed_candidates_consume_the_same_persisted_rotation_for_all_roles(self):
        """Delegates, reviews, sync and scout summaries share the managed selection boundary."""
        with patch.object(workflow, "usage_snapshot", return_value={}), \
             patch.object(workflow.shutil, "which", side_effect=lambda name: name):
            for role in ("worker", "reviewer", "sync", "scout"):
                with self.subTest(role=role):
                    self.assertEqual([workflow.candidates(Config(workflow=self.config), role, None)["provider"]
                                      for _ in range(4)], ["claude", "claude", "codex", "claude"])


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
