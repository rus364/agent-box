"""Workflow configuration must preserve intent without exposing disabled credentials."""
import unittest

from agent_box.config import Config


class WorkflowConfigTests(unittest.TestCase):
    def test_tracker_and_workflow_start_disabled(self):
        data = Config().to_dict()
        self.assertIn("tracker", data)
        self.assertFalse(data["tracker"]["enabled"])
        self.assertFalse(data["workflow"]["enabled"])

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


if __name__ == "__main__":
    unittest.main()
