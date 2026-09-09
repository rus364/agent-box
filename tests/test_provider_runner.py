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
        evidence = Path("/tmp/exact-review-evidence")
        command = r.build_command({"provider": "claude", "model": "chosen-model", "effort": "high"},
                                  "reviewer", Path("/tmp"), Path("/tmp/schema.json"), Path("/tmp/result.json"),
                                  [evidence])
        self.assertEqual(command[command.index("--model") + 1], "chosen-model")
        self.assertEqual(command[command.index("--effort") + 1], "high")
        self.assertIn("--tools", command)
        self.assertEqual(command[command.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(command[command.index("--allowedTools") + 1], "Read,Grep,Glob")
        self.assertEqual(command[command.index("--add-dir") + 1], str(evidence))
        self.assertNotIn(str(evidence.parent), [command[index + 1] for index, value in enumerate(command[:-1])
                                                if value == "--add-dir"])
        self.assertIn("--strict-mcp-config", command)

    def test_each_managed_reviewer_command_is_ephemeral_and_has_no_resume(self):
        r = importlib.import_module("agent_box.provider_runner")
        root = Path("/tmp")
        schema = Path("/tmp/schema.json")
        result = Path("/tmp/result.json")
        codex = r.build_command({"provider": "codex", "model": "m", "effort": "high"},
                                "reviewer", root, schema, result)
        claude = r.build_command({"provider": "claude", "model": "m", "effort": "high"},
                                 "reviewer", root, schema, result)
        self.assertIn("--ephemeral", codex)
        self.assertIn("--no-session-persistence", claude)
        for command in (codex, claude):
            self.assertNotIn("resume", command)
            self.assertNotIn("--session-id", command)

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


class FailureDiagnosticsTests(unittest.TestCase):
    """Lost outputs must be inspectable offline without entering public evidence."""

    def setUp(self):
        self.r = importlib.import_module("agent_box.provider_runner")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "workflow"
        patcher = patch.object(self.r, "WORKFLOW_STATE", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.route = {"provider": "claude", "model": "haiku", "effort": "low", "tier": 1}
        self.report = {"verdict": "PASS", "summary": "Checked", "findings": []}

    def run_response(self, output, *, stderr=b"", code=0, route=None, result=None, timeout=False):
        def popen(command, **kwargs):
            kwargs["stdout"].buffer.write(output)
            kwargs["stderr"].buffer.write(stderr)
            if result is not None:
                Path(command[command.index("-o") + 1]).write_bytes(result)
            process = unittest.mock.Mock(returncode=code, pid=12345)
            if timeout:
                process.communicate.side_effect = self.r.subprocess.TimeoutExpired(command, 1)
            return process
        with patch.object(self.r.subprocess, "Popen", side_effect=popen), \
             patch.object(self.r.os, "killpg"):
            return self.r.run(route or self.route, "reviewer", self.root, "private prompt")

    def fail_response(self, output, **kwargs):
        with self.assertRaises(self.r.ProviderFailure) as caught:
            self.run_response(output, **kwargs)
        return caught.exception

    def test_invalid_json_keeps_raw_private_bytes_and_safe_metadata(self):
        """Invalid provider prose and stderr survive privately, not in the error/evidence."""
        import base64
        failure = self.fail_response(b"secret-stdout\xff", stderr=b"secret-stderr")
        public = json.dumps(failure.diagnostic) + str(failure)
        self.assertNotIn("secret", public)
        self.assertEqual(failure.diagnostic["stage"], "json")
        self.assertEqual(failure.diagnostic["exit_code"], 0)
        self.assertEqual(failure.diagnostic["stdout_bytes"], 14)
        self.assertEqual(failure.diagnostic["stderr_bytes"], 13)
        self.assertEqual(failure.diagnostic["model"], "haiku")
        self.assertEqual(failure.diagnostic["role"], "reviewer")
        self.assertIsNone(failure.diagnostic["usage"])
        self.assertIsNone(failure.diagnostic["total_cost_usd"])
        self.assertGreaterEqual(failure.diagnostic["duration_seconds"], 0)
        artifact = self.state / failure.diagnostic["artifact"]
        self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)
        self.assertEqual(artifact.parent.stat().st_mode & 0o777, 0o700)
        data = json.loads(artifact.read_text())
        self.assertEqual(base64.b64decode(data["streams"]["stdout"]["base64"]), b"secret-stdout\xff")
        self.assertNotIn("private prompt", artifact.read_text())

    def test_parse_and_envelope_failures_remain_distinct_without_salvage(self):
        """Null structured outputs and Markdown results cannot silently become PASS."""
        cases = [(b"not json", "json", "response is not valid JSON"),
                 (b"[]", "envelope", "response must be a JSON object"),
                 (b"null", "envelope", "response must be a JSON object"),
                 (json.dumps({"structured_output": None, "result": json.dumps(self.report)}).encode(),
                  "envelope", "structured_output must be an object"),
                 (b'{"structured_output":[]}', "envelope", "structured_output must be an object"),
                 (b'{"result":42}', "envelope", "result must contain a JSON object"),
                 (b'{"result":"```json secret ```"}', "json", "result string is not valid JSON"),
                 (b'{"type":"result","subtype":"success"}', "envelope", "Claude envelope has no structured_output or result")]
        for output, stage, reason in cases:
            with self.subTest(reason=reason):
                failure = self.fail_response(output)
                self.assertEqual(failure.category, "invalid-report")
                self.assertEqual(failure.diagnostic["stage"], stage)
                self.assertEqual(failure.diagnostic["reason"], reason)

    def test_successful_direct_structured_and_string_reports_create_no_artifacts(self):
        """Established successful formats retain their exact reports."""
        for envelope in (self.report, {"structured_output": self.report}, {"result": json.dumps(self.report)}):
            with self.subTest(envelope=envelope):
                self.assertEqual(self.run_response(json.dumps(envelope).encode()), self.report)
        self.assertEqual(self.run_response(b"codex events", route={**self.route, "provider": "codex"},
                                           result=json.dumps(self.report).encode()), self.report)
        self.assertFalse((self.state / "provider-failures").exists())

    def test_error_subtypes_or_errors_cannot_smuggle_valid_verdict(self):
        """Reproduce the observed parser defect with explicitly synthetic envelopes."""
        for extra in ({"subtype": "error_max_turns"}, {"subtype": "error_max_structured_output_retries"},
                      {"errors": ["private-provider-error"]}, {"subtype": "error_secret-value"}):
            failure = self.fail_response(json.dumps({"type": "result", "is_error": False,
                                                    "structured_output": self.report, **extra}).encode())
            self.assertEqual(failure.category, "provider-error")
            self.assertEqual(failure.stage, "envelope")
            self.assertNotIn("secret", json.dumps(failure.diagnostic))
            self.assertNotIn("private-provider-error", json.dumps(failure.diagnostic))

    def test_error_categories_survive_and_consider_stderr_and_errors(self):
        """Provider failures remain failures even at exit zero with only errors populated."""
        for envelope, code, stderr, category in [
            ({"errors": ["authentication private"]}, 0, b"", "authentication"),
            ({"is_error": True, "api_error_status": 429}, 0, b"", "quota"),
            ({}, 1, b"model unavailable private", "model-unavailable"),
            ({"is_error": True}, 0, b"", "provider-error"),
        ]:
            with self.subTest(category=category):
                failure = self.fail_response(json.dumps(envelope).encode(), code=code, stderr=stderr)
                self.assertEqual(failure.category, category)

    def test_schema_reasons_identify_fields_and_invariants_without_values(self):
        """A schema mismatch identifies the actionable field without leaking its value."""
        finding = {"severity": "high", "path": "a.py", "line": 1, "message": "Bug", "evidence": "Proof", "requirement": "Rule"}
        cases = [({**self.report, "summary": " "}, "summary"),
                 ({**self.report, "findings": [finding]}, "PASS requires empty findings"),
                 ({**self.report, "verdict": "NEEDS_WORK"}, "NEEDS_WORK requires findings"),
                 ({**self.report, "secret-key": "secret"}, "unexpected fields"),
                 ({**self.report, "verdict": "NEEDS_WORK", "findings": [{**finding, "line": True}]}, "findings[0].line"),
                 ({**self.report, "verdict": "NEEDS_WORK", "findings": [{**finding, "line": -1}]}, "findings[0].line")]
        for report, reason in cases:
            with self.subTest(reason=reason):
                failure = self.fail_response(json.dumps({"structured_output": report}).encode())
                self.assertEqual(failure.diagnostic["stage"], "schema")
                self.assertIn(reason, failure.diagnostic["reason"])
                self.assertNotIn("secret", json.dumps(failure.diagnostic))
                self.assertEqual(failure.diagnostic["response_source"], "stdout.structured_output")
        schema = self.r.report_schema("reviewer")
        self.assertEqual(schema["properties"]["summary"]["pattern"], r"\S")
        self.assertEqual(schema["properties"]["findings"]["items"]["properties"]["line"]["minimum"], 0)
        self.assertIn("nonempty for NEEDS_WORK", schema["properties"]["findings"]["description"])

    def test_schema_avoids_claude_rejected_top_level_conditionals(self):
        """The live Claude 400 fixture proves top-level combinators prevent any review."""
        for role in ("reviewer", "worker"):
            with self.subTest(role=role):
                schema = self.r.report_schema(role)
                self.assertFalse({"oneOf", "allOf", "anyOf"} & schema.keys())
                self.assertEqual(schema["type"], "object")

    def test_usage_allowlist_preserves_numeric_counters_and_unknowns(self):
        """Provider-controlled labels and text cannot enter safe usage metadata."""
        envelope = {"structured_output": {**self.report, "summary": ""}, "total_cost_usd": 0.001,
                    "usage": {"input_tokens": 7, "output_tokens": "secret", "cache_read_input_tokens": True,
                              "cache_creation_input_tokens": float("inf"), "secret-key": 99,
                              "server_tool_use": {"web_search_requests": 2, "secret": "secret"}}}
        failure = self.fail_response(json.dumps(envelope).encode())
        self.assertEqual(failure.diagnostic["usage"]["input_tokens"], 7)
        self.assertIsNone(failure.diagnostic["usage"]["output_tokens"])
        self.assertIsNone(failure.diagnostic["usage"]["cache_read_input_tokens"])
        self.assertIsNone(failure.diagnostic["usage"]["cache_creation_input_tokens"])
        self.assertEqual(failure.diagnostic["total_cost_usd"], 0.001)
        self.assertNotIn("secret", json.dumps(failure.diagnostic))

    def test_codex_result_file_is_bounded_and_its_source_and_size_are_recorded(self):
        """The final-file path must not bypass the stdout parse or artifact limits."""
        import base64
        output = b"x" * (self.r.RESPONSE_LIMIT + 1)
        failure = self.fail_response(b"events", route={**self.route, "provider": "codex"}, result=output)
        self.assertIn("parse limit", failure.reason)
        self.assertEqual(failure.diagnostic["response_source"], "result-file")
        self.assertEqual(failure.diagnostic["result_bytes"], len(output))
        artifact = json.loads((self.state / failure.diagnostic["artifact"]).read_text())
        saved = artifact["streams"]["result.json"]
        self.assertTrue(saved["truncated"])
        self.assertEqual(len(base64.b64decode(saved["base64"])), self.r.DIAGNOSTIC_STREAM_LIMIT)

    def test_retention_removes_expired_and_oldest_only_with_fixed_stream_limits(self):
        """Failures cannot grow state indefinitely or delete unrelated investigation files."""
        import base64
        first = self.fail_response(b"bad")
        first_path = self.state / first.diagnostic["artifact"]
        os.utime(first_path, (0, 0))
        unrelated = first_path.parent / "investigation.md"
        unrelated.write_text("keep")
        with patch.object(self.r, "DIAGNOSTIC_MAX_FILES", 2):
            self.fail_response(b"a")
            self.assertFalse(first_path.exists())
            second = self.fail_response(b"b")
            third = self.fail_response(b"c" * (self.r.DIAGNOSTIC_STREAM_LIMIT + 1),
                                       stderr=b"e" * (self.r.STDERR_LIMIT + 1))
        files = list(first_path.parent.glob("*.json"))
        self.assertEqual(len(files), 2)
        self.assertTrue((self.state / second.diagnostic["artifact"]).exists())
        data = json.loads((self.state / third.diagnostic["artifact"]).read_text())
        for name, limit in (("stdout", self.r.DIAGNOSTIC_STREAM_LIMIT), ("stderr", self.r.STDERR_LIMIT)):
            self.assertEqual(len(base64.b64decode(data["streams"][name]["base64"])), limit)
            self.assertTrue(data["streams"][name]["truncated"])
        self.assertEqual(unrelated.read_text(), "keep")

    def test_timeout_unavailable_and_persistence_failure_keep_safe_failure_context(self):
        """Diagnostic storage failure must not mask the original category."""
        timeout = self.fail_response(b"partial-private", code=-9, timeout=True)
        self.assertEqual(timeout.category, "timeout")
        self.assertEqual(timeout.diagnostic["exit_code"], -9)
        with patch.object(self.r.subprocess, "Popen", side_effect=FileNotFoundError("secret")):
            with self.assertRaises(self.r.ProviderFailure) as caught:
                self.r.run(self.route, "reviewer", self.root, "private")
        self.assertEqual(caught.exception.category, "unavailable")
        self.assertIsNone(caught.exception.diagnostic["exit_code"])
        with patch.object(self.r, "save_failure", side_effect=OSError("secret")):
            failure = self.fail_response(b"bad")
        self.assertEqual(failure.category, "invalid-report")
        self.assertIsNone(failure.diagnostic["artifact"])
        self.assertNotIn("secret", json.dumps(failure.diagnostic))

    def test_all_failed_candidates_surface_artifact_references_without_exception_text(self):
        """A final failure still points to every saved attempt after fallback exhaustion."""
        from types import SimpleNamespace
        from agent_box import workflow
        first = self.fail_response(b"private-first")
        second = self.fail_response(b"private-second", route={**self.route, "provider": "codex"})
        selected = {**self.route, "fallbacks": [{**self.route, "provider": "codex"}]}
        with patch.object(workflow, "run", side_effect=[first, second]):
            with self.assertRaises(workflow.AgentBoxError) as caught:
                workflow.execute(SimpleNamespace(timeout_seconds=1), selected, "reviewer", self.root, "prompt")
        public = str(caught.exception)
        self.assertIn(first.diagnostic["artifact"], public)
        self.assertIn(second.diagnostic["artifact"], public)
        self.assertNotIn("private-first", public)
        self.assertNotIn("private-second", public)
