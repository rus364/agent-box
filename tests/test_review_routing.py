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
            for writer, other in (("codex", "claude"), ("claude", "codex")):
                with self.subTest(tier=tier, writer=writer):
                    route = select(config, "reviewer", tier, other, writer=writer)
                    candidates = [route, *route["fallbacks"]]
                    self.assertEqual([r["provider"] for r in candidates], [other, writer])
                    self.assertEqual([r["model"] for r in candidates], [other + suffix, writer + suffix])
                    self.assertTrue(all(r["tier"] == tier for r in candidates))

    def test_cross_provider_falls_back_to_the_writer_only_when_the_other_provider_cannot_run(self):
        """A missing CLI or candidate moves cross-provider review to the writer; same-provider never moves."""
        for writer, other in (("codex", "claude"), ("claude", "codex")):
            for missing in ("model", "executable"):
                config = reviewer_config()
                available = [writer, other]
                if missing == "model":
                    config.review.tiers["2"] = [spec for spec in config.review.tiers["2"] if spec["provider"] != other]
                else:
                    available.remove(other)
                with self.subTest(writer=writer, missing=missing):
                    route = select(config, "reviewer", 2, other, writer=writer, available=available)
                    self.assertEqual((route["provider"], route["fallbacks"]), (writer, []))
                    config.review.routing = "same-provider"
                    self.assertEqual(select(config, "reviewer", 2, other, writer=writer, available=available)["provider"], writer)

    def test_reviewer_models_leave_worker_and_scout_routes_unchanged(self):
        """Reviewer choices cannot reschedule implementation or introduce a fake tier."""
        config = reviewer_config()
        for role, tier in (("worker", 2), ("scout", 0)):
            expected = select(config, role, tier, "codex")
            config.review.models = [model_entry({"provider": "claude", "model": "opus"})]
            self.assertEqual(select(config, role, tier, "codex"), expected)
        with self.assertRaisesRegex(AgentBoxError, "Tier 0"):
            select(config, "reviewer", 0, "codex")

    def test_default_cross_provider_prefers_the_other_provider_with_the_writer_as_fallback(self):
        config = reviewer_config()
        self.assertEqual(config.review.routing, "cross-provider")
        for writer, other in (("codex", "claude"), ("claude", "codex")):
            with self.subTest(writer=writer):
                route = select(config, "reviewer", 2, other, writer=writer)
                self.assertEqual(route["provider"], other)
                self.assertEqual([p["provider"] for p in route["fallbacks"]], [writer])

    def test_same_provider_uses_actual_writer_and_exact_reviewer_settings(self):
        """The session provider cannot redirect a strict reviewer."""
        config = reviewer_config()
        config.review.routing = "same-provider"
        for writer, other in (("codex", "claude"), ("claude", "codex")):
            with self.subTest(writer=writer):
                next(spec for spec in config.review.tiers["2"] if spec["provider"] == writer)["effort"] = "high"
                route = select(config, "reviewer", 2, other, writer=writer)
                self.assertEqual((route["provider"], route["model"], route["effort"], route["tier"]),
                                 (writer, writer + "-reviewer", "high", 2))
                self.assertEqual(route["fallbacks"], [])

    def test_unavailable_or_unconfigured_writer_never_executes_other_provider(self):
        """An installed alternative or worker model cannot rescue a strict review."""
        for writer, other in (("codex", "claude"), ("claude", "codex")):
            for missing in ("executable", "model"):
                with self.subTest(writer=writer, missing=missing), tempfile.TemporaryDirectory() as directory:
                    config = reviewer_config()
                    config.review.routing = "same-provider"
                    if missing == "model":
                        config.review.tiers["2"] = [spec for spec in config.review.tiers["2"] if spec["provider"] != writer]
                    with patch.object(workflow, "WORKFLOW_STATE", Path(directory)), \
                         patch.object(workflow_state, "WORKFLOW_STATE", Path(directory)), \
                         patch.object(workflow.shutil, "which", side_effect=lambda p: None if p == writer and missing == "executable" else p), \
                         patch.object(workflow, "run") as run:
                        with self.assertRaisesRegex(AgentBoxError, "No allowed reviewer"):
                            route = workflow.route(Config(workflow=config), "reviewer", 2, other, writer)
                            workflow.execute(config, route, "reviewer", Path(directory), "test")
                        run.assert_not_called()

    def test_writer_failure_exhausts_same_provider_review_without_other_attempt(self):
        """All provider failures stop strict review even when general fallback is enabled."""
        config = reviewer_config()
        config.review.routing = "same-provider"
        for writer, other in (("codex", "claude"), ("claude", "codex")):
            for category in ("unavailable", "model-unavailable", "authentication", "quota", "timeout",
                             "invalid-report", "provider-error", "unsupported"):
                with self.subTest(writer=writer, category=category):
                    route = select(config, "reviewer", 2, other, {}, writer=writer)
                    with patch.object(workflow, "run", side_effect=ProviderFailure("test failure", category)) as run:
                        with self.assertRaises(AgentBoxError):
                            workflow.execute(config, route, "reviewer", Path("/unused"), "test")
                    self.assertEqual([call.args[0]["provider"] for call in run.call_args_list], [writer])

    def test_cross_provider_requires_observed_unavailability_before_fallback(self):
        """Only availability failures permit another provider, never an unknown or malformed response."""
        config = reviewer_config()
        allowed = ("unavailable", "model-unavailable", "authentication", "quota", "timeout")
        for writer in ("codex", "claude"):
            selected = select(config, "reviewer", 2, writer, {}, writer=writer)
            primary = selected["provider"]
            for category in (*allowed, "invalid-report", "provider-error", "unsupported"):
                for fallback in (False, True):
                    route = {**selected, "fallbacks": selected["fallbacks"] if fallback else []}
                    with self.subTest(writer=writer, category=category, fallback=fallback), \
                         patch.object(workflow, "run", side_effect=[ProviderFailure("Failure", category),
                             {"verdict": "PASS", "summary": "Checked", "findings": []}]) as run:
                        if category in allowed and fallback:
                            result = workflow.execute(config, route, "reviewer", Path("/unused"), "test")
                            self.assertEqual(result["route"]["provider"], writer)
                            self.assertEqual(result["failures"], [{"provider": primary, "category": category}])
                        else:
                            with self.assertRaises(AgentBoxError):
                                workflow.execute(config, route, "reviewer", Path("/unused"), "test")
                        expected = [primary, writer] if category in allowed and fallback else [primary]
                        self.assertEqual([call.args[0]["provider"] for call in run.call_args_list], expected)

    def test_review_verdict_never_triggers_provider_fallback(self):
        """A negative or inconclusive verdict must reach the coordinator without verdict shopping."""
        config = reviewer_config()
        route = select(config, "reviewer", 2, "codex", {}, writer="codex")
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
        route = select(config, "reviewer", 2, "codex", {}, writer="codex")
        with patch.object(workflow, "preflight", side_effect=ProviderFailure("Unsupported fast", "unsupported")) as check:
            with self.assertRaisesRegex(AgentBoxError, "correct the route"):
                workflow.managed_review_route(route)
        self.assertEqual(check.call_count, 1)
        with patch.object(workflow, "preflight", side_effect=[ProviderFailure("Missing CLI", "unavailable"), None]):
            selected, failures = workflow.managed_review_route(route)
        self.assertEqual(selected["provider"], "codex")
        self.assertEqual(failures[0]["category"], "unavailable")

    def test_unsupported_backup_preserves_primary_and_preflight_diagnostics(self):
        """An unusable backup must not prevent an available primary from reviewing."""
        config = reviewer_config()
        route = select(config, "reviewer", 2, "claude", {}, writer="claude")
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

    def test_review_routing_does_not_change_worker_or_scout_selection(self):
        """Reviewer policy must leave worker scheduling and fallback candidates intact."""
        config = reviewer_config()
        for role, tier in (("worker", 2), ("scout", 0)):
            config.review.routing = "cross-provider"
            expected = select(config, role, tier, "codex")
            config.review.routing = "same-provider"
            self.assertEqual(select(config, role, tier, "codex"), expected)

    def test_config_roundtrip_defaults_and_rejects_unknown_review_routing(self):
        """Saved routing must survive reload while malformed strings fail validation."""
        self.assertEqual(Config.from_dict({}).workflow.review.routing, "cross-provider")
        for mode in ("cross-provider", "same-provider"):
            config = Config.from_dict({"workflow": {"review": {"routing": mode}}})
            self.assertEqual(Config.from_dict(config.to_dict()).workflow.review.routing, mode)
            self.assertEqual(config.workflow.validate(), [])
        for mode in ("", "current", "same_provider"):
            config = Config.from_dict({"workflow": {"review": {"routing": mode}}})
            self.assertIn("workflow.review.routing must be cross-provider or same-provider", config.workflow.validate())
