"""Workflow configuration must preserve intent without exposing disabled credentials."""
import unittest

from agent_box.config import Config


class WorkflowConfigTests(unittest.TestCase):
    def test_tracker_and_workflow_start_disabled(self):
        data = Config().to_dict()
        self.assertIn("tracker", data)
        self.assertFalse(data["tracker"]["enabled"])
        self.assertFalse(data["workflow"]["enabled"])
        self.assertEqual(data["workflow"]["merge_mode"], "github")
        self.assertTrue(all(
            spec["effort"] == "medium"
            for tier in data["workflow"]["tiers"].values()
            for spec in tier.values()))

    def test_routing_and_connections_round_trip_without_legacy_sections(self):
        data = {
            "tracker": {"enabled": True, "provider": "vikunja", "connections": {
                "vikunja": {"url": "https://tasks.example", "token": "secret"}}},
            "workflow": {"enabled": True, "routing": "usage-aware", "weights": {
                "codex": 3, "claude": 1}, "tiers": {"0": {
                    "codex": {"model": "small", "effort": "low"}}, "3": {
                    "claude": {"model": "large", "effort": "high"}}},
                "fallback": False, "review": {"policy": "always", "max_files": 9}},
        }
        result = Config.from_dict(data).to_dict()
        self.assertEqual(result["tracker"], data["tracker"])
        self.assertEqual(result["workflow"]["weights"], {"codex": 3, "claude": 1})
        self.assertEqual(result["workflow"]["tiers"]["3"]["claude"]["model"], "large")
        self.assertFalse(result["workflow"]["fallback"])
        self.assertEqual(result["workflow"]["review"]["policy"], "always")
        self.assertNotIn("flow_next", result)
        self.assertNotIn("vikunja", result)

    def test_invalid_routing_values_are_reported_before_work_is_dispatched(self):
        config = Config.from_dict({"workflow": {"routing": "magic", "weights": {
            "codex": -1, "claude": 0}}})
        self.assertTrue(any("routing" in e for e in config.workflow.validate()))

    def test_merge_mode_round_trips_and_rejects_unknown_values(self):
        config = Config.from_dict({"workflow": {"merge_mode": "local"}})
        self.assertEqual(Config.from_dict(config.to_dict()).workflow.merge_mode, "local")
        config.workflow.merge_mode = "magic"
        self.assertTrue(any("merge_mode" in error for error in config.workflow.validate()))

    def test_default_effort_round_trips_for_both_providers(self):
        loaded = Config.from_dict({"workflow": {"tiers": {"0": {
            "codex": {"model": "small", "effort": "default"},
            "claude": {"model": "haiku", "effort": "default"},
        }}}})
        config = Config.from_dict(loaded.to_dict())
        self.assertEqual(config.workflow.tiers["0"]["codex"]["effort"], "default")
        self.assertEqual(config.workflow.tiers["0"]["claude"]["effort"], "default")
        self.assertEqual(config.workflow.validate(), [])

    def test_unknown_effort_is_rejected_without_changing_existing_levels(self):
        config = Config.from_dict({"workflow": {"tiers": {"1": {
            "codex": {"model": "codex", "effort": "ultra"},
            "claude": {"model": "claude", "effort": "unsupported"},
        }}}})
        errors = config.workflow.validate()
        self.assertFalse(any("codex" in error for error in errors))
        self.assertTrue(any("claude" in error and "effort" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
