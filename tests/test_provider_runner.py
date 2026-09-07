"""CLI results must distinguish provider failures from actual review findings."""
import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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
        self.assertEqual(command[command.index("--effort") + 1], "high")
        self.assertIn("--tools", command)
        self.assertNotIn("Bash", command[command.index("--tools") + 1])
        self.assertIn("--strict-mcp-config", command)

    def test_expired_oauth_is_authentication_failure_not_a_review(self):
        r = importlib.import_module("agent_box.provider_runner")
        with self.assertRaises(r.ProviderFailure) as caught:
            r.parse_result("claude", 1, json.dumps({"is_error": True, "api_error_status": 401,
                "result": "Failed to authenticate. OAuth access token has expired."}), "")
        self.assertEqual(caught.exception.category, "authentication")

    def test_default_effort_is_omitted_from_provider_commands(self):
        r = importlib.import_module("agent_box.provider_runner")
        root = Path("/tmp")
        schema = Path("/nonexistent")
        result = Path("/tmp/result.json")
        codex = r.build_command(
            {"provider": "codex", "model": "chosen-model", "effort": "default"},
            "worker", root, schema, result)
        claude = r.build_command(
            {"provider": "claude", "model": "chosen-model", "effort": "default"},
            "worker", root, schema, result)
        self.assertFalse(any("model_reasoning_effort" in value for value in codex))
        self.assertNotIn("--effort", claude)
        explicit_codex = r.build_command(
            {"provider": "codex", "model": "chosen-model", "effort": "high"},
            "worker", root, schema, result)
        self.assertIn('model_reasoning_effort="high"', explicit_codex)

    def test_managed_claude_default_does_not_inherit_effort_environment(self):
        r = importlib.import_module("agent_box.provider_runner")
        process = unittest.mock.Mock(returncode=0)
        process.communicate.return_value = (None, None)
        report = {"status": "completed", "summary": "done", "evidence": []}
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": "high"}), \
             patch.object(r.subprocess, "Popen", return_value=process) as popen, \
             patch.object(r, "parse_result", return_value=report):
            self.assertEqual(r.run(
                {"provider": "claude", "model": "chosen-model", "effort": "default"},
                "worker", directory, "prompt"), report)
        self.assertNotIn("CLAUDE_CODE_EFFORT_LEVEL", popen.call_args.kwargs["env"])
        self.assertNotIn("--effort", popen.call_args.args[0])
