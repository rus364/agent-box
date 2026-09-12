"""Selection must respect tier, account limits, and independent review."""
import importlib
import unittest

from agent_box.config import WorkflowConfig


class RoutingTests(unittest.TestCase):
    def config(self):
        c = WorkflowConfig()
        c.routing = "weighted"
        c.weights = {"codex": 3, "claude": 1}
        for tier in c.tiers.values():
            for name in tier:
                tier[name]["model"] = name + "-model"
        for tier in c.review.tiers.values():
            for name in tier:
                tier[name]["model"] = name + "-reviewer"
        return c

    def test_weighted_counts_are_independent_per_tier(self):
        r = importlib.import_module("agent_box.routing")
        state = {}
        c = self.config()
        first = r.select(c, "worker", 3, "codex", state)["provider"]
        for _ in range(12):
            r.select(c, "scout", 0, "codex", state)
        providers = [first] + [r.select(c, "worker", 3, "codex", state)["provider"] for _ in range(3)]
        self.assertEqual(providers.count("codex"), 3)
        self.assertEqual(providers.count("claude"), 1)

    def test_weekly_reset_and_capacity_weight_affect_selection(self):
        r = importlib.import_module("agent_box.routing")
        c = self.config()
        c.routing = "usage-aware"
        c.reserve_percent = 0
        c.weights = {"codex": 1, "claude": 1}
        usage = {"codex": {"fresh": True, "windows": [{"seconds": 604800, "remaining": 60, "reset_at": 87400}]},
                 "claude": {"fresh": True, "windows": [{"seconds": 604800, "remaining": 60, "reset_at": 519400}]}}
        self.assertEqual(r.select(c, "worker", 2, "codex", {}, usage, now=1000)["provider"], "codex")
        c.weights["claude"] = 5
        self.assertEqual(r.select(c, "worker", 2, "codex", {}, usage, now=1000)["provider"], "claude")

    def test_review_ignores_weights_and_stays_in_the_selected_tier(self):
        r = importlib.import_module("agent_box.routing")
        c = self.config()
        c.weights["claude"] = 0
        result = r.select(c, "reviewer", 2, "codex", {}, writer="codex")
        self.assertEqual(result["provider"], "claude")
        self.assertEqual(result["fallbacks"][0]["provider"], "codex")
        c.review.tiers["2"]["claude"]["model"] = ""
        c.review.tiers["2"]["codex"]["model"] = ""
        with self.assertRaises(Exception):
            r.select(c, "reviewer", 2, "codex", {})

    def test_stale_statistics_fall_back_but_a_known_exhausted_window_still_excludes(self):
        r = importlib.import_module("agent_box.routing")
        c = self.config()
        c.routing = "usage-aware"
        usage = {"codex": {"fresh": False, "windows": [{"seconds": 18000, "remaining": 0, "reset_at": 2000}]}}
        result = r.select(c, "worker", 1, "codex", {}, usage, now=1000)
        self.assertEqual(result["provider"], "claude")
        self.assertIn("static", result["reason"])

    def test_usage_error_falls_back_to_weights_without_excluding_provider(self):
        """An unavailable budget is unknown, not an exhausted subscription."""
        r = importlib.import_module("agent_box.routing")
        c = self.config()
        c.routing = "usage-aware"
        snapshots = {
            "codex": {"fresh": True, "windows": [
                {"seconds": 604800, "remaining": 90, "reset_at": 604900}]},
            "claude": {"fresh": False, "error": "claude: HTTP 401", "windows": []},
        }
        state = {}
        results = [r.select(c, "worker", 2, "codex", state, snapshots, now=1000) for _ in range(4)]
        self.assertEqual([item["provider"] for item in results].count("claude"), 1)
        self.assertTrue(all("static weights" in item["reason"] for item in results))


if __name__ == "__main__":
    unittest.main()
