"""CLI results must distinguish provider failures from actual review findings."""
import importlib
import json
import os
import signal
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


class ProviderRunnerTests(unittest.TestCase):
    def test_claude_exit_zero_error_is_not_a_review_verdict(self):
        r = importlib.import_module("agent_box.provider_runner")
        with self.assertRaises(r.ProviderFailure) as caught:
            r.parse_result("claude", 0, json.dumps({"is_error": True, "result": "model is unavailable", "api_error_status": 404}), "")
        self.assertEqual(caught.exception.category, "model-unavailable")

    def test_a_verdict_is_the_one_verdict_line_among_prose(self):
        """Formatting around the line is tolerated; zero, several or an unknown value is ambiguous and fails."""
        r = importlib.import_module("agent_box.provider_runner")
        report = r.review_report("I read the diff.\n\n**Verdict:** NEEDS_WORK\n\n"
                                 "- [HIGH] api.py:5 — missing guard\n* [LOW] README.md — typo\n- [P1] x.py:1 — not a severity\n")
        self.assertEqual(report, {"verdict": "NEEDS_WORK", "findings": [
            {"severity": "HIGH", "location": "api.py:5"}, {"severity": "LOW", "location": "README.md"}]})
        self.assertEqual(r.review_report("## Verdict: PASS\n")["verdict"], "PASS")
        for text, reason in (("The verdict is PASS.\n", "response has no Verdict line"),
                             ("Verdict: PASS\nVerdict: PASS\n", "response has more than one Verdict line"),
                             ("Verdict: PASS, mostly\n", "Verdict must be PASS, NEEDS_WORK or INCONCLUSIVE")):
            with self.subTest(text=text), self.assertRaises(r.ProviderFailure) as caught:
                r.review_report(text)
            self.assertEqual((caught.exception.category, caught.exception.stage, caught.exception.reason),
                             ("invalid-report", "verdict", reason))

    def test_read_only_claude_reviewer_has_tool_restrictions_and_no_model_guess(self):
        r = importlib.import_module("agent_box.provider_runner")
        from pathlib import Path
        evidence = Path("/tmp/exact-review-evidence")
        command = r.build_command({"provider": "claude", "model": "chosen-model", "effort": "high"},
                                  "reviewer", Path("/tmp"), Path("/tmp/result.json"), [evidence])
        self.assertEqual(command[command.index("--model") + 1], "chosen-model")
        self.assertEqual(command[command.index("--effort") + 1], "high")
        self.assertIn("--tools", command)
        self.assertEqual(command[command.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(command[command.index("--allowedTools") + 1], "Read,Grep,Glob")
        self.assertEqual(command[command.index("--add-dir") + 1], str(evidence))
        self.assertNotIn(str(evidence.parent), [command[index + 1] for index, value in enumerate(command[:-1])
                                                if value == "--add-dir"])
        self.assertIn("--strict-mcp-config", command)

    def test_each_managed_role_starts_without_resume_or_parent_transcript(self):
        """All roles use fresh CLI entrypoints and answer in plain text; no role is held to a JSON schema."""
        r = importlib.import_module("agent_box.provider_runner")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        result = root / "result.json"
        for role in ("worker", "scout", "reviewer"):
            for provider, flag in (("codex", "--ephemeral"), ("claude", "--no-session-persistence")):
                with self.subTest(role=role, provider=provider):
                    command = r.build_command({"provider": provider, "model": "m", "effort": "high"},
                                              role, root, result)
                    self.assertIn(flag, command)
                    self.assertFalse({"--output-schema", "--json-schema"} & set(command))
                    self.assertFalse({"resume", "--resume", "--continue", "--fork-session", "--session-id"} & set(command))

    def test_expired_oauth_is_authentication_failure_not_a_review(self):
        r = importlib.import_module("agent_box.provider_runner")
        with self.assertRaises(r.ProviderFailure) as caught:
            r.parse_result("claude", 1, json.dumps({"is_error": True, "api_error_status": 401,
                "result": "Failed to authenticate. OAuth access token has expired."}), "")
        self.assertEqual(caught.exception.category, "authentication")

    def test_default_effort_is_omitted_from_provider_commands(self):
        r = importlib.import_module("agent_box.provider_runner")
        root = Path("/tmp")
        result = Path("/tmp/result.json")
        codex = r.build_command(
            {"provider": "codex", "model": "chosen-model", "effort": "default"},
            "worker", root, result)
        claude = r.build_command(
            {"provider": "claude", "model": "chosen-model", "effort": "default"},
            "worker", root, result)
        self.assertFalse(any("model_reasoning_effort" in value for value in codex))
        self.assertNotIn("--effort", claude)
        explicit_codex = r.build_command(
            {"provider": "codex", "model": "chosen-model", "effort": "high"},
            "worker", root, result)
        self.assertIn('model_reasoning_effort="high"', explicit_codex)

    def test_managed_claude_default_does_not_inherit_effort_environment(self):
        r = importlib.import_module("agent_box.provider_runner")
        process = unittest.mock.Mock(returncode=0)
        process.communicate.return_value = (None, None)
        report = "## Done\nEdited."
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
        self.text = "Checked the diff.\n\nVerdict: PASS\n"
        self.report = {"verdict": "PASS", "findings": [], "text": self.text}
        self.envelope = json.dumps({"result": self.text}).encode()

    def run_response(self, output, *, stderr=b"", code=0, route=None, result=None, timeout=False, diagnostics=None,
                     on_completed=None, role="reviewer", streams=None):
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
            return self.r.run(route or self.route, role, self.root, "private prompt",
                              diagnostics=diagnostics, on_completed=on_completed, streams=streams)

    def test_completion_hook_runs_with_diagnostics_before_temporary_files_are_removed(self):
        """Workflow must save a successful response before the provider's temporary context exits."""
        diagnostics = {}
        active = []
        original = self.r.tempfile.TemporaryDirectory
        def temporary(*args, **kwargs):
            directory = original(*args, **kwargs)
            active.append(Path(directory.name))
            return directory
        def completed(report):
            self.assertEqual(report, self.report)
            self.assertTrue((active[-1] / "stdout").is_file())
            self.assertEqual(diagnostics["usage"]["input_tokens"], 17)
        envelope = {"result": self.text, "usage": {"input_tokens": 17}}
        with patch.object(self.r.tempfile, "TemporaryDirectory", side_effect=temporary):
            self.run_response(json.dumps(envelope).encode(), diagnostics=diagnostics, on_completed=completed)
        self.assertFalse(active[-1].exists())

    def test_success_usage_survives_the_runner_and_workflow_without_artifacts(self):
        """All observed Codex turn counters must survive aggregation into workflow output."""
        from types import SimpleNamespace
        from agent_box import workflow
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                route = {**self.route, "provider": provider, "fallbacks": []}
                envelope = {"result": self.text, "usage": {"input_tokens": 17, "output_tokens": 3},
                            "total_cost_usd": 0.002, "private-key": "private-value"}
                events = [{"type": "item.completed", "item": {"text": "private-value"}},
                          {"type": "turn.completed", "usage": {"input_tokens": 17, "output_tokens": 3,
                                                                "cached_input_tokens": 5,
                                                                "cache_write_input_tokens": 4,
                                                                "reasoning_output_tokens": 2,
                                                                "private-key": 99}},
                          {"type": "turn.completed", "usage": {"cache_write_input_tokens": 6,
                                                                "reasoning_output_tokens": 1}}]
                output = ("\n".join(json.dumps(event) for event in events) if provider == "codex"
                          else json.dumps(envelope)).encode()

                def runner(*args, diagnostics, **kwargs):
                    return self.run_response(output, route=route, diagnostics=diagnostics,
                                             result=self.text.encode() if provider == "codex" else None)

                with patch.object(workflow, "run", side_effect=runner):
                    result = workflow.execute(SimpleNamespace(timeout_seconds=1), route, "reviewer", self.root, "prompt")
                self.assertEqual(result["report"], self.report)
                metadata = result["diagnostics"]
                self.assertEqual(metadata["usage"]["input_tokens"], 17)
                self.assertEqual(metadata["usage"]["output_tokens"], 3)
                self.assertEqual(metadata["usage"]["cached_input_tokens"], 5 if provider == "codex" else None)
                self.assertEqual(metadata["usage"]["cache_write_input_tokens"], 10 if provider == "codex" else None)
                self.assertEqual(metadata["usage"]["reasoning_output_tokens"], 3 if provider == "codex" else None)
                # The CLI's own price estimate is not an external API's bill, so it is not kept.
                self.assertNotIn("total_cost_usd", json.dumps(result))
                self.assertNotIn("private", json.dumps(metadata))
        self.assertFalse((self.state / "provider-failures").exists())

    def test_codex_cache_write_and_reasoning_counters_keep_numeric_bounds(self):
        """Invalid values stay unknown, zero survives, and summed counters remain bounded."""
        path = self.root / "usage.jsonl"
        cases = [({}, None), ({"value": None}, None), ({"value": True}, None),
                 ({"value": "secret"}, None), ({"value": -1}, None),
                 ({"value": float("nan")}, None), ({"value": float("inf")}, None),
                 ({"value": 10**15 + 1}, None), ({"value": 0}, 0),
                 ({"value": 1.5}, 1.5), ({"value": 10**15}, 10**15)]
        for key in ("cache_write_input_tokens", "reasoning_output_tokens"):
            for values, expected in cases:
                with self.subTest(key=key, values=values):
                    usage = {"input_tokens": 1, "secret-key": 99}
                    if values:
                        usage[key] = values["value"]
                    event = {"type": "turn.completed", "usage": usage}
                    path.write_text(json.dumps(event) + "\n")
                    for metadata in (self.r.response_metadata(event), self.r.codex_usage(path)):
                        self.assertEqual(metadata["usage"][key], expected)
                        self.assertNotIn("secret", json.dumps(metadata))
            path.write_text((json.dumps({"type": "turn.completed", "usage": {key: 10**15}}) + "\n") * 2)
            self.assertIsNone(self.r.codex_usage(path)["usage"][key])

    def test_missing_success_counters_remain_unknown(self):
        """No usage event or envelope value must not turn into zero cost/tokens."""
        for provider in ("codex", "claude"):
            diagnostics = {}
            self.run_response(self.envelope, result=self.text.encode() if provider == "codex" else None,
                              route={**self.route, "provider": provider}, diagnostics=diagnostics)
            self.assertEqual(diagnostics, {"usage": None})

    def fail_response(self, output, **kwargs):
        with self.assertRaises(self.r.ProviderFailure) as caught:
            self.run_response(output, **kwargs)
        return caught.exception

    def test_unreadable_output_keeps_raw_private_bytes_and_safe_metadata(self):
        """Invalid provider output and stderr survive privately, not in the error/evidence."""
        import base64
        failure = self.fail_response(b"secret-stdout\xff", stderr=b"secret-stderr")
        public = json.dumps(failure.diagnostic) + str(failure)
        self.assertNotIn("secret", public)
        self.assertEqual(failure.diagnostic["stage"], "response")
        self.assertEqual(failure.diagnostic["exit_code"], 0)
        self.assertEqual(failure.diagnostic["stdout_bytes"], 14)
        self.assertEqual(failure.diagnostic["stderr_bytes"], 13)
        self.assertEqual(failure.diagnostic["model"], "haiku")
        self.assertEqual(failure.diagnostic["role"], "reviewer")
        self.assertIsNone(failure.diagnostic["usage"])
        self.assertGreaterEqual(failure.diagnostic["duration_seconds"], 0)
        artifact = self.state / failure.diagnostic["artifact"]
        self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)
        self.assertEqual(artifact.parent.stat().st_mode & 0o777, 0o700)
        data = json.loads(artifact.read_text())
        self.assertEqual(base64.b64decode(data["streams"]["stdout"]["base64"]), b"secret-stdout\xff")
        self.assertNotIn("private prompt", artifact.read_text())

    def test_envelope_and_verdict_failures_remain_distinct(self):
        """A missing answer, an empty one and an ambiguous verdict each fail with their own stage."""
        cases = [(b"not json", "envelope", "Claude envelope has no result text"),
                 (b"[]", "envelope", "Claude envelope has no result text"),
                 (b'{"result":42}', "envelope", "Claude envelope has no result text"),
                 (json.dumps({"structured_output": {"verdict": "PASS"}}).encode(), "envelope",
                  "Claude envelope has no result text"),
                 (json.dumps({"result": " \n"}).encode(), "response", "the final message is empty"),
                 (json.dumps({"result": "Looks good."}).encode(), "verdict", "response has no Verdict line"),
                 (json.dumps({"result": "Verdict: PASS\nVerdict: NEEDS_WORK"}).encode(), "verdict",
                  "response has more than one Verdict line")]
        for output, stage, reason in cases:
            with self.subTest(reason=reason, output=output):
                failure = self.fail_response(output)
                self.assertEqual(failure.category, "invalid-report")
                self.assertEqual((failure.diagnostic["stage"], failure.diagnostic["reason"]), (stage, reason))

    def test_a_verdict_beside_prose_is_accepted_for_both_providers(self):
        """Task 388: a paid review wrapped its valid PASS in narration and lost the round."""
        prose = "I have completed the review.\n\n**Verdict:** PASS\n\n**Limitations:** read-only sandbox.\n"
        expected = {"verdict": "PASS", "findings": [], "text": prose}
        self.assertEqual(self.run_response(json.dumps({"result": prose}).encode()), expected)
        self.assertEqual(self.run_response(b"codex events", route={**self.route, "provider": "codex"},
                                           result=prose.encode()), expected)
        self.assertFalse((self.state / "provider-failures").exists())

    def test_streams_are_written_where_they_survive_a_killed_runner(self):
        """The process writes straight into the private run directory, so a kill leaves its streams behind.

        A refusal before launch opens nothing; a finished run's files become read-only and bounded.
        """
        runs = []

        def run_directory():
            path = self.root / f"run-{len(runs)}"
            path.mkdir(mode=0o700)
            runs.append(path)
            return path

        def written_while_running(command, **kwargs):
            kwargs["stdout"].buffer.write(b"partial events")
            kwargs["stdout"].flush()
            self.assertEqual((runs[-1] / "stdout").read_bytes(), b"partial events")
            raise KeyboardInterrupt

        with patch.object(self.r.subprocess, "Popen", side_effect=written_while_running), \
             self.assertRaises(self.r.ProviderFailure):
            self.r.run({**self.route, "provider": "codex"}, "reviewer", self.root, "prompt", streams=run_directory)
        with patch.object(self.r, "STREAM_LIMIT", 4):
            self.run_response(self.envelope, stderr=b"note", streams=run_directory)
        self.assertEqual(((runs[-1] / "stdout").read_bytes(), (runs[-1] / "stderr").read_bytes()),
                         (self.envelope[:4], b"note"))
        self.assertEqual((runs[-1] / "stdout").stat().st_mode & 0o777, 0o400)
        failure = self.fail_response(json.dumps({"result": "no verdict"}).encode(), streams=run_directory)
        self.assertEqual(failure.diagnostic["stdout_bytes"], len(json.dumps({"result": "no verdict"})))
        with self.assertRaises(self.r.ProviderFailure):
            self.r.run({**self.route, "provider": "other"}, "reviewer", self.root, "prompt", streams=run_directory)
        self.assertEqual([path.exists() for path in runs], [True, True, True, False])

    def test_error_subtypes_or_errors_cannot_smuggle_valid_verdict(self):
        """Reproduce the observed parser defect with explicitly synthetic envelopes."""
        for extra in ({"subtype": "error_max_turns"}, {"subtype": "error_max_structured_output_retries"},
                      {"errors": ["private-provider-error"]}, {"subtype": "error_secret-value"}):
            failure = self.fail_response(json.dumps({"type": "result", "is_error": False,
                                                    "result": self.text, **extra}).encode())
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

    def test_delegate_answers_are_the_final_markdown_message_without_a_schema(self):
        """A worker or scout answer is returned as written; only an empty or missing answer fails."""
        answer = "## Done\nEdited parser.\n\n## Evidence\n- tests passed\n\n## Limitations\nNone."
        for role in ("worker", "scout"):
            with self.subTest(role=role):
                self.assertEqual(self.run_response(json.dumps({"result": answer, "usage": {"input_tokens": 3}}).encode(),
                                                   role=role), answer)
                self.assertEqual(self.run_response(b"codex events", route={**self.route, "provider": "codex"},
                                                   result=answer.encode(), role=role), answer)
        self.assertFalse((self.state / "provider-failures").exists())
        for output, route, result, reason in (
                (json.dumps({"result": " \n"}).encode(), self.route, None, "the final message is empty"),
                (json.dumps({"structured_output": {"verdict": "PASS"}}).encode(), self.route, None,
                 "Claude envelope has no result text"),
                (b"codex events", {**self.route, "provider": "codex"}, None, "Codex wrote no final message")):
            with self.subTest(reason=reason):
                with self.assertRaises(self.r.ProviderFailure) as caught:
                    self.run_response(output, route=route, result=result, role="scout")
                self.assertEqual(caught.exception.category, "invalid-report")
                self.assertEqual(caught.exception.diagnostic["reason"], reason)

    def test_usage_allowlist_preserves_numeric_counters_and_unknowns(self):
        """Provider-controlled labels and text cannot enter safe usage metadata."""
        envelope = {"result": "No verdict line.", "total_cost_usd": 0.001,
                    "usage": {"input_tokens": 7, "output_tokens": "secret", "cache_read_input_tokens": True,
                              "cache_creation_input_tokens": float("inf"), "secret-key": 99,
                              "server_tool_use": {"web_search_requests": 2, "secret": "secret"}}}
        failure = self.fail_response(json.dumps(envelope).encode())
        self.assertEqual(failure.diagnostic["usage"]["input_tokens"], 7)
        self.assertIsNone(failure.diagnostic["usage"]["output_tokens"])
        self.assertIsNone(failure.diagnostic["usage"]["cache_read_input_tokens"])
        self.assertIsNone(failure.diagnostic["usage"]["cache_creation_input_tokens"])
        self.assertNotIn("total_cost_usd", failure.diagnostic)
        self.assertNotIn("secret", json.dumps(failure.diagnostic))

    def test_codex_result_file_is_bounded_and_its_source_and_size_are_recorded(self):
        """The final-file path must not bypass the stdout parse or artifact limits."""
        import base64
        output = b"x" * (self.r.RESPONSE_LIMIT + 1)
        failure = self.fail_response(b"events", route={**self.route, "provider": "codex"}, result=output)
        self.assertEqual(failure.reason, "response exceeds 4 MiB limit")
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

    def test_signal_to_the_runner_stops_its_cli_and_records_an_interruption(self):
        """The CLI runs in its own session: a SIGTERM that only reached the runner
        used to leave no failure record, and could leave the CLI spending alone."""
        started = []
        real_popen = self.r.subprocess.Popen

        def popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            started.append(process)
            threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGTERM)).start()
            return process

        command = [sys.executable, "-c", "import sys, time; sys.stdin.read(); time.sleep(60)"]
        previous = signal.getsignal(signal.SIGTERM)
        with patch.object(self.r, "build_command", return_value=command), \
             patch.object(self.r.subprocess, "Popen", side_effect=popen), \
             self.assertRaises(self.r.ProviderFailure) as caught:
            self.r.run(self.route, "reviewer", self.root, "private prompt")
        self.assertEqual(caught.exception.category, "interrupted")
        self.assertIsNotNone(started[0].returncode)
        self.assertTrue((self.state / caught.exception.diagnostic["artifact"]).exists())
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_timeout_unavailable_and_persistence_failure_keep_safe_failure_context(self):
        """Diagnostic storage failure must not mask the original category."""
        timeout = self.fail_response(b"partial-private", code=-9, timeout=True)
        self.assertEqual(timeout.category, "timeout")
        self.assertEqual(timeout.diagnostic["exit_code"], -9)
        self.assertIsNone(timeout.diagnostic["response_source"])
        with patch.object(self.r.subprocess, "Popen", side_effect=FileNotFoundError("secret")):
            with self.assertRaises(self.r.ProviderFailure) as caught:
                self.r.run(self.route, "reviewer", self.root, "private")
        self.assertEqual(caught.exception.category, "unavailable")
        self.assertIsNone(caught.exception.diagnostic["exit_code"])
        self.assertIsNone(caught.exception.diagnostic["response_source"])
        with patch.object(self.r, "save_failure", side_effect=OSError("secret")):
            failure = self.fail_response(b"bad")
        self.assertEqual(failure.category, "invalid-report")
        self.assertIsNone(failure.diagnostic["artifact"])
        self.assertNotIn("secret", json.dumps(failure.diagnostic))

    def test_all_failed_candidates_surface_artifact_references_without_exception_text(self):
        """A final failure still points to every saved attempt after fallback exhaustion."""
        from types import SimpleNamespace
        from agent_box import workflow
        first = self.fail_response(b"private-first", code=-9, timeout=True)
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
