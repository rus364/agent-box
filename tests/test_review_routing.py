"""Review provider policy must bound every attempt, including failed ones."""
import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_box.config import Config, WorkflowConfig, model_entry
from agent_box.errors import AgentBoxError
from agent_box.provider_runner import ProviderFailure
from agent_box.routing import select
from agent_box import workflow, workflow_state


def reviewer_config():
    """Worker, scout and reviewer lists that name each provider, so a route shows which list served it."""
    config = WorkflowConfig()
    config.worker.models = [model_entry({"provider": p, "model": p + "-model"}) for p in ("codex", "claude")]
    config.scout.models = [model_entry({"provider": p, "model": p + "-model"}) for p in ("codex", "claude")]
    for tier in config.review.tiers:
        config.review.tiers[tier] = [model_entry({"provider": p, "model": p + "-reviewer"}) for p in ("codex", "claude")]
    return config


class ReviewRoutingTests(unittest.TestCase):
    def test_role_models_serve_untiered_review_and_tiers_without_their_own_list(self):
        """The role list replaces session inheritance; a tier list still wins where it is set."""
        config = reviewer_config()
        config.review.models = [model_entry({"provider": p, "model": p + "-global", "effort": "default"}) for p in ("codex", "claude")]
        config.review.tiers["3"] = []
        for tier, suffix in ((None, "-global"), (2, "-reviewer"), (3, "-global")):
            with self.subTest(tier=tier):
                route = select(config, "reviewer", tier)
                candidates = [route, *route["fallbacks"]]
                self.assertEqual([r["model"] for r in candidates], ["codex" + suffix, "claude" + suffix])
                self.assertTrue(all(r["tier"] == tier for r in candidates))

    def test_an_uninstalled_or_unconfigured_first_reviewer_moves_to_the_next_candidate(self):
        """The reviewer is the first available candidate, whichever provider that is."""
        for first, second in (("codex", "claude"), ("claude", "codex")):
            for missing in ("model", "executable"):
                config = reviewer_config()
                config.review.tiers["2"] = [spec for provider in (first, second)
                                            for spec in config.review.tiers["2"] if spec["provider"] == provider]
                available = [first, second]
                if missing == "model":
                    config.review.tiers["2"] = config.review.tiers["2"][1:]
                else:
                    available.remove(first)
                with self.subTest(first=first, missing=missing):
                    route = select(config, "reviewer", 2, available=available)
                    self.assertEqual((route["provider"], route["fallbacks"]), (second, []))

    def test_reviewer_models_leave_worker_and_scout_routes_unchanged(self):
        """Reviewer choices cannot reschedule implementation or introduce a fake tier."""
        config = reviewer_config()
        for role, tier in (("worker", 2), ("scout", None)):
            expected = select(config, role, tier)
            config.review.models = [model_entry({"provider": "claude", "model": "opus"})]
            self.assertEqual(select(config, role, tier), expected)
        with self.assertRaisesRegex(AgentBoxError, "reviewer has no tier 0"):
            select(config, "reviewer", 0)

    def test_an_unconfigured_reviewer_list_never_executes_a_worker_model(self):
        """An installed CLI or a worker model cannot stand in for a missing reviewer candidate."""
        with tempfile.TemporaryDirectory() as directory:
            config = reviewer_config()
            config.review.tiers["2"] = []
            with patch.object(workflow, "WORKFLOW_STATE", Path(directory)), \
                 patch.object(workflow_state, "WORKFLOW_STATE", Path(directory)), \
                 patch.object(workflow.shutil, "which", side_effect=lambda p: p), \
                 patch.object(workflow, "run") as run:
                with self.assertRaisesRegex(AgentBoxError, "No reviewer candidate configured in workflow.review.models"):
                    workflow.candidates(Config(workflow=config), "reviewer", 2)
                run.assert_not_called()

    def test_provider_fallback_requires_observed_unavailability(self):
        """Only availability failures permit another provider, never an unknown or malformed response."""
        config = reviewer_config()
        allowed = ("unavailable", "model-unavailable", "authentication", "quota", "timeout")
        for first, second in (("codex", "claude"), ("claude", "codex")):
            config.review.tiers["2"] = [model_entry({"provider": p, "model": p + "-reviewer"}) for p in (first, second)]
            selected = select(config, "reviewer", 2, {})
            primary = selected["provider"]
            for category in (*allowed, "invalid-report", "provider-error", "unsupported"):
                for fallback in (False, True):
                    route = {**selected, "fallbacks": selected["fallbacks"] if fallback else []}
                    with self.subTest(first=first, category=category, fallback=fallback), \
                         patch.object(workflow, "run", side_effect=[ProviderFailure("Failure", category),
                             {"verdict": "PASS", "summary": "Checked", "findings": []}]) as run:
                        if category in allowed and fallback:
                            result = workflow.execute(config, route, "reviewer", Path("/unused"), "test")
                            self.assertEqual(result["route"]["provider"], second)
                            self.assertEqual(result["failures"], [{"provider": primary, "category": category}])
                        else:
                            with self.assertRaises(AgentBoxError):
                                workflow.execute(config, route, "reviewer", Path("/unused"), "test")
                        expected = [primary, second] if category in allowed and fallback else [primary]
                        self.assertEqual([call.args[0]["provider"] for call in run.call_args_list], expected)

    def test_review_verdict_never_triggers_provider_fallback(self):
        """A negative or inconclusive verdict must reach the coordinator without verdict shopping."""
        config = reviewer_config()
        route = select(config, "reviewer", 2, {})
        for verdict in ("PASS", "NEEDS_WORK", "INCONCLUSIVE"):
            report = {"verdict": verdict, "summary": "Checked", "findings": [] if verdict != "NEEDS_WORK" else [{
                "severity": "high", "path": "file.py", "line": 1, "message": "Incorrect", "evidence": "Example",
                "requirement": "Preserve behavior"}]}
            with self.subTest(verdict=verdict), patch.object(workflow, "run", return_value=report) as run:
                result = workflow.execute(config, route, "reviewer", Path("/unused"), "test")
            self.assertEqual(result["report"], report)
            self.assertEqual(run.call_count, 1)

    def test_managed_preflight_does_not_hide_unsupported_primary_settings(self):
        """A configuration error is not proof that the preferred service is unavailable."""
        config = reviewer_config()
        route = select(config, "reviewer", 2, {})
        with patch.object(workflow, "preflight", side_effect=ProviderFailure("Unsupported fast", "unsupported")) as check:
            with self.assertRaisesRegex(AgentBoxError, "correct the route"):
                workflow.managed_review_route(route)
        self.assertEqual(check.call_count, 1)
        with patch.object(workflow, "preflight", side_effect=[ProviderFailure("Missing CLI", "unavailable"), None]):
            selected, failures = workflow.managed_review_route(route)
        self.assertEqual(selected["provider"], "claude")
        self.assertEqual(failures[0]["category"], "unavailable")

    def test_unsupported_backup_preserves_primary_and_preflight_diagnostics(self):
        """An unusable backup must not prevent an available primary from reviewing."""
        config = reviewer_config()
        route = select(config, "reviewer", 2, {})
        route["fallbacks"][0].update(model="claude-opus-5", effort="high", fast=True)
        with patch.dict(os.environ, {"CLAUDE_CODE_DISABLE_FAST_MODE": "1"}), \
             patch.object(workflow.shutil, "which", return_value="cli"), \
             patch.object(workflow, "run", return_value={"verdict": "PASS", "summary": "Checked", "findings": []}) as run:
            selected, failures = workflow.managed_review_route(route)
            workflow.execute(config, selected, "reviewer", Path("/unused"), "test")
            self.assertEqual([call.args[0]["provider"] for call in run.call_args_list], ["codex"])
            self.assertEqual(selected["fallbacks"], [])
            self.assertEqual([(f["provider"], f["category"]) for f in failures], [("claude", "unsupported")])
            with patch.object(workflow.shutil, "which", side_effect=lambda p: None if p == "codex" else "cli"):
                with self.assertRaises(AgentBoxError) as caught:
                    workflow.managed_review_route(route)
            for fact in ('"provider": "codex"', '"unavailable"', '"provider": "claude"', '"unsupported"'):
                self.assertIn(fact, str(caught.exception))
