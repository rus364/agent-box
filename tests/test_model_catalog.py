"""Catalog probes never need a real client, network, or user-owned credentials."""
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

from agent_box import model_catalog


def claude_response(models, **response_fields):
    return json.dumps({"type": "control_response", "response": {
        "request_id": "catalog-probe", "subtype": "success",
        "response": {"models": models}, **response_fields}})


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.home = self.root / "claude"
        self.home.mkdir()
        for target, value in (("CLAUDE_HOME", self.home), ("CODEX_HOME", self.root / "codex")):
            patcher = patch.object(model_catalog, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, kwargs in (
            ("shutil.which", {"side_effect": lambda provider: str(self.root / provider)}),
            ("_probe", {"return_value": ""}),
            ("subprocess.Popen", {"side_effect": AssertionError("Real process forbidden")}),
        ):
            patcher = patch("agent_box.model_catalog." + target, **kwargs)
            setattr(self, target.rsplit(".", 1)[-1], patcher.start())
            self.addCleanup(patcher.stop)

    def test_codex_refresh_uses_visible_unique_ids_and_anchored_home(self):
        """Only selectable CLI models replace fallback, without reading a disk cache."""
        self._probe.return_value = json.dumps({"models": [
            {"slug": "gpt-6-astra", "visibility": "list"},
            {"slug": "gpt-6-astra", "visibility": "list"},
            {"slug": "hidden", "visibility": "hide"},
            {"slug": "bad model", "visibility": "list"},
            {"slug": "new-model", "visibility": "list"}, None,
        ]})
        self.assertEqual(model_catalog.load_models("codex"), ("gpt-6-astra", "new-model"))
        command = self._probe.call_args.args[0]
        self.assertEqual(command, [str(self.root / "codex"), "debug", "models"])
        self.assertEqual(self._probe.call_args.kwargs["env"]["CODEX_HOME"], str(self.root / "codex"))

    def test_claude_retains_aliases_and_concrete_ids_but_not_default_sentinel(self):
        """Concrete long-context IDs survive while duplicate/default choices disappear."""
        self._probe.return_value = claude_response([
            {"value": "default", "resolvedModel": "claude-sonnet-5"},
            {"value": "sonnet", "resolvedModel": "claude-sonnet-5"},
            {"value": "opus[1m]", "resolvedModel": "claude-opus-5[1m]"},
            {"value": "bad value", "resolvedModel": None}, None,
        ])
        self.assertEqual(model_catalog.load_models("claude"),
                         ("claude-sonnet-5", "sonnet", "opus[1m]", "claude-opus-5[1m]"))

    def test_claude_auth_copy_is_private_and_all_initialization_writes_are_removed(self):
        """Initialization may refresh copied credentials but cannot overwrite host state."""
        credentials = self.home / ".credentials.json"
        credentials.write_text('{"test-token":"credential-value"}')
        settings = self.home / "settings.json"
        settings.write_text('{"hooks":{"must-not-run":true}}')
        directories = []

        def probe(command, *, env, cwd, request):
            directory = Path(cwd)
            directories.append(directory)
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], cwd)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            copied = directory / ".credentials.json"
            self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
            self.assertEqual(copied.read_bytes(), credentials.read_bytes())
            self.assertEqual(list(directory.iterdir()), [copied])
            self.assertEqual(env["ANTHROPIC_API_KEY"], "api-secret")
            self.assertNotIn("CLAUDECODE", env)
            self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
            self.assertNotIn("api-secret", " ".join(command))
            self.assertNotIn("credential-value", " ".join(command))
            for flag in ("--safe-mode", "--no-session-persistence", "--strict-mcp-config"):
                self.assertIn(flag, command)
            for flag in ("--tools", "--setting-sources"):
                self.assertEqual(command[command.index(flag) + 1], "")
            self.assertEqual(json.loads(command[command.index("--mcp-config") + 1]), {"mcpServers": {}})
            self.assertEqual(request.count("\n"), 1)
            self.assertEqual(json.loads(request), {"type": "control_request", "request_id": "catalog-probe",
                                                  "request": {"subtype": "initialize"}})
            copied.write_text("refreshed-secret")
            (directory / ".claude.json").write_text("initialization state")
            (directory / "backups").mkdir()
            return claude_response([{"value": "sonnet"}])

        self._probe.side_effect = probe
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "api-secret", "CLAUDECODE": "1",
                                     "CLAUDE_CODE_SESSION_ID": "session"}):
            self.assertEqual(model_catalog.load_models("claude"), ("sonnet",))
        self.assertTrue(all(not directory.exists() for directory in directories))
        self.assertEqual(credentials.read_text(), '{"test-token":"credential-value"}')
        self.assertEqual(settings.read_text(), '{"hooks":{"must-not-run":true}}')
        self.assertEqual(set(self.home.iterdir()), {credentials, settings})

    def test_missing_clients_failures_timeouts_and_bad_outputs_use_static_fallback(self):
        """Client failures remain silent and every Claude attempt removes temporary state."""
        invalid = ["", "not json", "null", "[]", '{}', '{"models":{}}', '{"models":[]}',
                   claude_response([]), claude_response([{"value": "sonnet"}], request_id="other"),
                   claude_response([{"value": "sonnet"}], subtype="error"),
                   claude_response("sonnet"),
                   json.dumps({"type": "control_response", "response": None})]
        for provider in ("codex", "claude"):
            for failure in [FileNotFoundError("missing"), PermissionError("denied"),
                            subprocess.TimeoutExpired("client", 3),
                            subprocess.CalledProcessError(1, "client", stderr="secret"), *invalid]:
                with self.subTest(provider=provider, failure=type(failure).__name__), \
                     redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
                    self._probe.side_effect = failure if isinstance(failure, Exception) else None
                    self._probe.return_value = failure
                    self.assertEqual(model_catalog.load_models(provider), model_catalog.BUNDLED_MODELS[provider])
                    self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
                    if provider == "claude":
                        self.assertFalse(Path(self._probe.call_args.kwargs["cwd"]).exists())
        self.which.side_effect = None
        self.which.return_value = None
        self._probe.reset_mock()
        for provider in ("codex", "claude"):
            self.assertEqual(model_catalog.load_models(provider), model_catalog.BUNDLED_MODELS[provider])
        self._probe.assert_not_called()

    def test_claude_ignores_unrelated_responses_and_rejects_malformed_protocol(self):
        """Only the successful response matching our initialize request supplies models."""
        valid = claude_response([{"value": "sonnet"}])
        unrelated = claude_response([{"value": "wrong"}], request_id="other")
        self._probe.return_value = unrelated + "\n" + valid + "\n"
        self.assertEqual(model_catalog.load_models("claude"), ("sonnet",))
        self._probe.return_value = "not json\n" + valid
        self.assertEqual(model_catalog.load_models("claude"), model_catalog.BUNDLED_MODELS["claude"])

    def test_catalog_refresh_attempts_both_providers_once_concurrently(self):
        """One unavailable provider must not spend the other provider's timeout budget."""
        barrier = threading.Barrier(2)

        def probe(command, **kwargs):
            barrier.wait(timeout=1)
            if command[1] == "debug":
                return json.dumps({"models": [{"slug": "codex-new", "visibility": "list"}]})
            return claude_response([{"value": "claude-new"}])

        self._probe.side_effect = probe
        self.assertEqual(model_catalog.load_model_catalogs(), {"codex": ("codex-new",), "claude": ("claude-new",)})
        self.assertEqual(self._probe.call_count, 2)
        self.assertCountEqual([call.args[0] for call in self.which.call_args_list], ["codex", "claude"])


class ProbeTests(unittest.TestCase):
    def test_timeout_kills_wrapper_process_group_and_reaps_before_returning(self):
        """A timed-out npm wrapper must not leave its child holding pipes or credentials."""
        process = MagicMock(pid=123, returncode=-9)
        process.communicate.side_effect = [subprocess.TimeoutExpired("fixture", 3), ("", None)]
        with patch.object(model_catalog.subprocess, "Popen") as popen, \
             patch.object(model_catalog.os, "killpg") as kill:
            popen.return_value.__enter__.return_value = process
            with self.assertRaises(subprocess.TimeoutExpired):
                model_catalog._probe(["fixture"], env={}, request="request\n")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)
        process.communicate.assert_any_call("request\n", timeout=3)
        kill.assert_called_once_with(123, signal.SIGKILL)
        self.assertEqual(process.communicate.call_count, 2)

    def test_nonzero_process_exit_rejects_even_valid_stdout(self):
        """A failed refresh cannot lend credibility to partial catalog output."""
        process = MagicMock(returncode=1)
        process.communicate.return_value = ('{"models":[]}', None)
        with patch.object(model_catalog.subprocess, "Popen") as popen:
            popen.return_value.__enter__.return_value = process
            with self.assertRaises(subprocess.CalledProcessError):
                model_catalog._probe(["fixture"], env={})
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
