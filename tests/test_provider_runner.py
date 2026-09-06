"""CLI results must distinguish provider failures from actual review findings."""
import importlib
import json
import unittest


class ProviderRunnerTests(unittest.TestCase):
    def test_claude_exit_zero_error_is_not_a_review_verdict(self):
        r = importlib.import_module("agent_box.provider_runner")
        with self.assertRaises(r.ProviderFailure) as caught:
            r.parse_result("claude", 0, json.dumps({"is_error": True, "result": "model is unavailable", "api_error_status": 404}), "")
        self.assertEqual(caught.exception.category, "model-unavailable")

    def test_success_without_valid_review_schema_is_inconclusive(self):
        r = importlib.import_module("agent_box.provider_runner")
        with self.assertRaises(r.ProviderFailure):
            r.validate_report({"verdict": "PASS", "findings": [{"severity": "high"}]}, "reviewer")
        report = r.validate_report({"verdict": "NEEDS_WORK", "summary": "Missing guard", "findings": [
            {"severity": "high", "path": "api.py", "line": 5, "message": "Missing guard", "evidence": "Unauthenticated path", "requirement": "Access control"}]}, "reviewer")
        self.assertEqual(report["verdict"], "NEEDS_WORK")

    def test_read_only_claude_reviewer_has_tool_restrictions_and_no_model_guess(self):
        r = importlib.import_module("agent_box.provider_runner")
        from pathlib import Path
        command = r.build_command({"provider": "claude", "model": "chosen-model", "effort": "high"},
                                  "reviewer", Path("/tmp"), Path("/tmp/schema.json"), Path("/tmp/result.json"))
        self.assertEqual(command[command.index("--model") + 1], "chosen-model")
        self.assertIn("--tools", command)
        self.assertNotIn("Bash", command[command.index("--tools") + 1])
        self.assertIn("--strict-mcp-config", command)

    def test_expired_oauth_is_authentication_failure_not_a_review(self):
        r = importlib.import_module("agent_box.provider_runner")
        with self.assertRaises(r.ProviderFailure) as caught:
            r.parse_result("claude", 1, json.dumps({"is_error": True, "api_error_status": 401,
                "result": "Failed to authenticate. OAuth access token has expired."}), "")
        self.assertEqual(caught.exception.category, "authentication")
