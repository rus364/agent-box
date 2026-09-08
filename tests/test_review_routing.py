"""Review provider policy must bound every attempt, including failed ones."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_box.config import Config
from agent_box.errors import AgentBoxError
from agent_box.provider_runner import ProviderFailure
from agent_box.routing import select
from agent_box import workflow
import test_routing


class ReviewRoutingTests(unittest.TestCase):
    def test_global_candidates_preserve_writer_policy_tier_and_fallback(self):
        """An override replaces model settings, never writer identity or fallback policy."""
        config = test_routing.RoutingTests().config()
        override = config.review.override
        override.enabled = True
        override.models = {p: {"model": p + "-global", "effort": "default"} for p in ("codex", "claude")}
        for tier in (None, 1, 2, 3):
            for apply in (False, True):
                override.apply_to_tiered = apply
                for writer, other in (("codex", "claude"), ("claude", "codex")):
                    for mode in ("same-provider", "cross-provider"):
                        config.review.routing = mode
                        for fallback in (False, True):
                            config.fallback = fallback
                            with self.subTest(tier=tier, apply=apply, writer=writer, mode=mode, fallback=fallback):
                                route = select(config, "reviewer", tier, other, {}, writer=writer)
                                expected = [writer] if mode == "same-provider" else [other] + ([writer] if fallback else [])
                                candidates = [route, *route["fallbacks"]]
                                self.assertEqual([r["provider"] for r in candidates], expected)
                                for candidate in candidates:
                                    global_model = tier is None or apply
                                    self.assertEqual(candidate["model"], candidate["provider"] + ("-global" if global_model else "-reviewer"))
                                    self.assertEqual(candidate["effort"], "default" if global_model else "medium")
                                    self.assertEqual(candidate["tier"], tier)

    def test_empty_or_unavailable_global_candidates_cannot_inherit_session_or_tiers(self):
        """A disabled override candidate cannot silently launch an unrelated configured model."""
        config = test_routing.RoutingTests().config()
        config.review.override.enabled = True
        for writer, other in (("codex", "claude"), ("claude", "codex")):
            for mode in ("same-provider", "cross-provider"):
                config.review.routing = mode
                preferred = writer if mode == "same-provider" else other
                for missing in ("model", "executable"):
                    config.review.override.models = {p: {"model": p + "-global", "effort": "medium"} for p in (writer, other)}
                    available = [writer, other]
                    if missing == "model":
                        config.review.override.models[preferred]["model"] = ""
                    else:
                        available.remove(preferred)
                    for fallback in (False, True):
                        config.fallback = fallback
                        with self.subTest(writer=writer, mode=mode, missing=missing, fallback=fallback):
                            if mode == "cross-provider" and fallback:
                                route = select(config, "reviewer", None, other, {}, writer=writer, available=available)
                                self.assertEqual((route["provider"], route["model"]), (writer, writer + "-global"))
                            else:
                                with self.assertRaisesRegex(AgentBoxError, "workflow.review.override.models"):
                                    select(config, "reviewer", None, other, {}, writer=writer, available=available)

    def test_global_review_override_leaves_worker_and_scout_routes_unchanged(self):
        """Reviewer choices cannot reschedule implementation or introduce a fake tier."""
        config = test_routing.RoutingTests().config()
        for role, tier in (("worker", 2), ("scout", 0)):
            config.review.override.enabled = False
            expected = select(config, role, tier, "codex", {})
            config.review.override.enabled = config.review.override.apply_to_tiered = True
            self.assertEqual(select(config, role, tier, "codex", {}), expected)
        with self.assertRaisesRegex(AgentBoxError, "Tier 0"):
            select(config, "reviewer", 0, "codex", {})

    def test_default_cross_provider_keeps_optional_writer_fallback(self):
        """Disabling fallback must not change the default preference for another provider."""
        config = test_routing.RoutingTests().config()
        self.assertEqual(config.review.routing, "cross-provider")
        for writer, other in (("codex", "claude"), ("claude", "codex")):
            for fallback in (False, True):
                with self.subTest(writer=writer, fallback=fallback):
                    config.fallback = fallback
                    route = select(config, "reviewer", 2, other, {}, writer=writer)
                    self.assertEqual(route["provider"], other)
                    self.assertEqual([p["provider"] for p in route["fallbacks"]], [writer] if fallback else [])

    def test_same_provider_uses_actual_writer_and_exact_reviewer_settings(self):
        """Session provider, worker weights and usage cannot redirect a strict reviewer."""
        config = test_routing.RoutingTests().config()
        config.review.routing = "same-provider"
        config.routing = "usage-aware"
        for writer, other in (("codex", "claude"), ("claude", "codex")):
            for fallback in (False, True):
                with self.subTest(writer=writer, fallback=fallback):
                    config.fallback = fallback
                    config.weights = {writer: 0, other: 1}
                    config.review.tiers["2"][writer]["effort"] = "high"
                    usage = {writer: {"fresh": True, "windows": [{"remaining": 0, "reset_at": 2000}]}}
                    route = select(config, "reviewer", 2, other, {}, usage, now=1000, writer=writer)
                    self.assertEqual((route["provider"], route["model"], route["effort"], route["tier"]),
                                     (writer, writer + "-reviewer", "high", 2))
                    self.assertEqual(route["fallbacks"], [])

    def test_unavailable_or_unconfigured_writer_never_executes_other_provider(self):
        """An installed alternative or worker model cannot rescue a strict review."""
        for writer, other in (("codex", "claude"), ("claude", "codex")):
            for missing in ("executable", "model"):
                with self.subTest(writer=writer, missing=missing), tempfile.TemporaryDirectory() as directory:
                    config = test_routing.RoutingTests().config()
                    config.review.routing = "same-provider"
                    if missing == "model":
                        config.review.tiers["2"][writer]["model"] = ""
                    with patch.object(workflow, "WORKFLOW_STATE", Path(directory)), \
                         patch.object(workflow.shutil, "which", side_effect=lambda p: None if p == writer and missing == "executable" else p), \
                         patch.object(workflow, "run") as run:
                        with self.assertRaisesRegex(AgentBoxError, "No allowed reviewer"):
                            route = workflow.route(config, "reviewer", 2, other, writer)
                            workflow.execute(config, route, "reviewer", Path(directory), "test")
                        run.assert_not_called()

    def test_writer_failure_exhausts_same_provider_review_without_other_attempt(self):
        """All provider failures stop strict review even when general fallback is enabled."""
        config = test_routing.RoutingTests().config()
        config.review.routing = "same-provider"
        for writer, other in (("codex", "claude"), ("claude", "codex")):
            for category in ("unavailable", "model-unavailable", "auth", "timeout", "invalid-report"):
                with self.subTest(writer=writer, category=category):
                    route = select(config, "reviewer", 2, other, {}, writer=writer)
                    with patch.object(workflow, "run", side_effect=ProviderFailure("test failure", category)) as run:
                        with self.assertRaisesRegex(AgentBoxError, "No provider produced a valid report"):
                            workflow.execute(config, route, "reviewer", Path("/unused"), "test")
                    self.assertEqual([call.args[0]["provider"] for call in run.call_args_list], [writer])

    def test_review_routing_does_not_change_worker_or_scout_selection(self):
        """Reviewer policy must leave worker scheduling and fallback candidates intact."""
        config = test_routing.RoutingTests().config()
        for mode in ("current", "weighted", "usage-aware"):
            config.routing = mode
            for role, tier in (("worker", 2), ("scout", 0)):
                config.review.routing = "cross-provider"
                expected = select(config, role, tier, "codex", {})
                config.review.routing = "same-provider"
                self.assertEqual(select(config, role, tier, "codex", {}), expected)

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
