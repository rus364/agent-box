"""Wiring for Anthropic's official Telegram channel plugin.

agent-box implements none of the Telegram control path. What it owns is two
decisions, and this file pins both:

* **Bootstrap order.** The plugin is installed from *inside* a Claude session,
  so the channel cannot be enabled before it exists. `agent-box config` must
  refuse to store `telegram_channel: true` until the prerequisites are real,
  otherwise the setting is just a promise every later run has to warn about.
* **One owner.** A Telegram bot has one conversation. If a second concurrent
  session also held the channel, both would answer every inbound message.
  The second session runs local-only instead — and, critically, still runs.
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_box import channel, cli, notify, runtime
from agent_box.config import CLAUDE_TELEGRAM_CHANNEL, CLAUDE_TELEGRAM_PLUGIN, Config
from agent_box.workspace import Workspace


def claude_home(tmp, *, plugin=False, token=False) -> Path:
    home = Path(tmp)
    if plugin:
        path = home / "plugins" / "installed_plugins.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "version": 2,
            "plugins": {CLAUDE_TELEGRAM_PLUGIN: [{"scope": "user", "version": "0.0.7"}]},
        }))
    if token:
        env = home / channel.CHANNEL_STATE / ".env"
        env.parent.mkdir(parents=True, exist_ok=True)
        env.write_text("TELEGRAM_BOT_TOKEN=123456:AAHfiqksKZ8\n")
    return home


class PluginDetectionTests(unittest.TestCase):
    def test_absent_state_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(channel.plugin_installed(Path(tmp)))

    def test_installed_plugin_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(channel.plugin_installed(claude_home(tmp, plugin=True)))

    def test_another_plugin_does_not_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plugins" / "installed_plugins.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"version": 2, "plugins": {"other@somewhere": [{}]}}))
            self.assertFalse(channel.plugin_installed(Path(tmp)))

    def test_corrupt_state_file_is_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plugins" / "installed_plugins.json"
            path.parent.mkdir(parents=True)
            path.write_text("{ truncated")
            self.assertFalse(channel.plugin_installed(Path(tmp)))


class TokenDetectionTests(unittest.TestCase):
    def test_token_in_the_plugins_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(channel.token_configured(claude_home(tmp, token=True)))

    def test_host_environment_variable_does_not_count(self):
        """agent-box never forwards TELEGRAM_BOT_TOKEN into the container, so
        counting a host-exported one would pass the preflight while the actual
        session had no token — the exact silent failure this guards against."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:abc"}):
                self.assertFalse(channel.token_configured(Path(tmp)))

    def test_empty_assignment_is_not_a_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / channel.CHANNEL_STATE / ".env"
            env.parent.mkdir(parents=True)
            env.write_text("TELEGRAM_BOT_TOKEN=\n")
            self.assertFalse(channel.token_configured(Path(tmp)))

    def test_unrelated_variables_are_not_a_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / channel.CHANNEL_STATE / ".env"
            env.parent.mkdir(parents=True)
            env.write_text("TELEGRAM_ACCESS_MODE=static\n")
            self.assertFalse(channel.token_configured(Path(tmp)))


class MissingRequirementsTests(unittest.TestCase):
    def test_nothing_set_up_reports_both_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(len(channel.missing_requirements(Path(tmp))), 2)

    def test_plugin_without_token_reports_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(len(channel.missing_requirements(claude_home(tmp, plugin=True))), 1)

    def test_fully_set_up_reports_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = claude_home(tmp, plugin=True, token=True)
            self.assertEqual(channel.missing_requirements(home), [])

    def test_instructions_name_the_bootstrap_order_and_leak_no_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = claude_home(tmp, token=True)
            text = channel.setup_instructions(home)
            self.assertIn("agent-box claude", text)
            self.assertIn(f"/plugin install {CLAUDE_TELEGRAM_PLUGIN}", text)
            self.assertIn("/telegram:configure", text)
            self.assertIn("agent-box config", text)
            self.assertLess(text.index("/plugin install"), text.index("agent-box config"))
            self.assertNotIn("AAHfiqksKZ8", text)


class ChannelLockTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.lock = Path(tmp.name) / "runtime/telegram-channel.json"

    def test_free_when_no_record_exists(self):
        self.assertIsNone(channel.channel_owner(self.lock))

    def test_acquire_then_second_attempt_fails(self):
        self.assertTrue(channel.acquire_channel("/p", self.lock))
        self.assertFalse(channel.acquire_channel("/other", self.lock))

    def test_owner_records_only_pid_and_light_metadata(self):
        channel.acquire_channel("/p", self.lock)
        record = channel.channel_owner(self.lock)
        self.assertEqual(record["pid"], os.getpid())
        self.assertEqual(record["project"], "/p")
        self.assertEqual(
            set(record) - {
                "pid", "pid_start", "boot_id", "container", "session", "project", "started_at",
            }, set(),
            "the lock must stay minimal — names of things to check, never credentials",
        )

    def test_lock_file_is_not_world_readable(self):
        channel.acquire_channel("/p", self.lock)
        self.assertEqual(self.lock.stat().st_mode & 0o777, 0o600)

    def test_stale_lock_from_a_dead_process_is_recovered(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text(json.dumps(
            {"pid": 2 ** 30, "pid_start": "999", "boot_id": channel._boot_id()}))
        self.assertIsNone(channel.channel_owner(self.lock))
        self.assertTrue(channel.acquire_channel("/p", self.lock))

    def test_recycled_pid_is_not_mistaken_for_the_owner(self):
        """This process is alive, but the recorded start time is not ours —
        so the PID was reused and the record is stale."""
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text(json.dumps(
            {"pid": os.getpid(), "pid_start": "0", "boot_id": channel._boot_id()}))
        self.assertIsNone(channel.channel_owner(self.lock))

    def test_record_from_before_a_reboot_is_stale(self):
        channel.acquire_channel("/p", self.lock)
        record = json.loads(self.lock.read_text())
        record["boot_id"] = "00000000-0000-0000-0000-000000000000"
        self.lock.write_text(json.dumps(record))
        self.assertIsNone(channel.channel_owner(self.lock))

    def test_corrupt_lock_is_treated_as_free(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text("{ truncated")
        self.assertIsNone(channel.channel_owner(self.lock))
        self.assertTrue(channel.acquire_channel("/p", self.lock))

    def test_release_clears_our_own_record(self):
        channel.acquire_channel("/p", self.lock)
        channel.release_channel(self.lock)
        self.assertIsNone(channel.channel_owner(self.lock))

    def test_release_does_not_clear_someone_elses_record(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text(json.dumps(
            {"pid": 1, "pid_start": channel._pid_start_ticks(1), "boot_id": channel._boot_id()}))
        channel.release_channel(self.lock)
        self.assertTrue(self.lock.exists())


class DetachedOwnerTests(unittest.TestCase):
    """A session outlives the launcher that started it whenever the terminal
    goes away, and it is still the one answering Telegram."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.lock = Path(tmp.name) / "runtime/telegram-channel.json"

    def _record_from_a_dead_launcher(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text(json.dumps({
            "pid": 2 ** 30, "pid_start": "999", "boot_id": channel._boot_id(),
            "container": "agent-box-p-1", "session": "claude-4242", "project": "/p",
        }))

    def test_detached_session_keeps_the_channel_after_its_launcher_exits(self):
        self._record_from_a_dead_launcher()
        with mock.patch.object(channel, "tmux_session_exists", return_value=True) as alive:
            self.assertIsNotNone(channel.channel_owner(self.lock))
            self.assertFalse(channel.acquire_channel("/other", self.lock))
        alive.assert_called_with("agent-box-p-1", "claude-4242")

    def test_channel_is_free_once_that_session_is_gone_too(self):
        self._record_from_a_dead_launcher()
        with mock.patch.object(channel, "tmux_session_exists", return_value=False):
            self.assertIsNone(channel.channel_owner(self.lock))
            self.assertTrue(channel.acquire_channel("/other", self.lock))

    def test_a_reboot_still_invalidates_a_session_record(self):
        """Neither a PID nor a container survives one, so the box is not asked."""
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text(json.dumps({
            "pid": 2 ** 30, "pid_start": "999", "boot_id": "0000-before-the-reboot",
            "container": "agent-box-p-1", "session": "claude-4242",
        }))
        with mock.patch.object(channel, "tmux_session_exists", return_value=True) as alive:
            self.assertIsNone(channel.channel_owner(self.lock))
        alive.assert_not_called()

    def test_detaching_does_not_release_the_channel(self):
        channel.acquire_channel("/p", self.lock, container="agent-box-p-1", session="claude-9")
        with mock.patch.object(channel, "tmux_session_exists", return_value=True):
            channel.release_channel(self.lock)
        self.assertTrue(self.lock.exists())

    def test_a_finished_session_releases_the_channel_normally(self):
        channel.acquire_channel("/p", self.lock, container="agent-box-p-1", session="claude-9")
        with mock.patch.object(channel, "tmux_session_exists", return_value=False):
            channel.release_channel(self.lock)
        self.assertFalse(self.lock.exists())

    def test_a_launch_with_no_session_is_owned_by_its_launcher_alone(self):
        """`claude -p ...` has no detachable session; nothing outlives it."""
        channel.acquire_channel("/p", self.lock)
        with mock.patch.object(channel, "tmux_session_exists") as alive:
            channel.release_channel(self.lock)
        self.assertFalse(self.lock.exists())
        alive.assert_not_called()

    def test_a_refused_second_launch_is_told_which_session_holds_the_channel(self):
        self._record_from_a_dead_launcher()
        with mock.patch.object(channel, "tmux_session_exists", return_value=True):
            described = channel.describe_owner(channel.channel_owner(self.lock))
        self.assertIn("claude-4242", described)


class LaunchDecisionTests(unittest.TestCase):
    """runtime.claude_channel_args: never blocks Claude from starting."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.lock = self.root / "runtime/telegram-channel.json"
        self.workspace = Workspace(self.root, self.root)
        self.config = Config()
        self.config.agents.claude.telegram_channel = True

    def _decide(self, *, ready=True, rest=None):
        home = claude_home(str(self.root / "claude"), plugin=ready, token=ready)
        err = io.StringIO()
        with mock.patch.object(channel, "CLAUDE_HOME", home), \
             mock.patch.object(channel, "CHANNEL_LOCK", self.lock), \
             redirect_stderr(err):
            flags, env = runtime.claude_channel_args(
                "claude", self.config, self.workspace, rest or [])
        return flags, env, err.getvalue()

    def test_ready_and_free_takes_the_channel(self):
        flags, env, _ = self._decide()
        self.assertEqual(flags, ["--channels", CLAUDE_TELEGRAM_CHANNEL])
        self.assertEqual(env, ["AGENT_BOX_SUPPRESS_NOTIFY=1"])

    def test_claim_names_the_session_so_a_detach_does_not_free_the_channel(self):
        session = runtime.TmuxSession(name="claude-4242", status_path=self.root / "s")
        home = claude_home(str(self.root / "claude"), plugin=True, token=True)
        with mock.patch.object(channel, "CLAUDE_HOME", home), \
             mock.patch.object(channel, "CHANNEL_LOCK", self.lock):
            runtime.claude_channel_args("claude", self.config, self.workspace, [], session)
        record = json.loads(self.lock.read_text())
        self.assertEqual(record["session"], "claude-4242")
        self.assertEqual(record["container"], runtime.workspace_container_name(self.workspace))

    def test_a_launch_without_a_session_claims_the_channel_for_itself_alone(self):
        self._decide()
        record = json.loads(self.lock.read_text())
        self.assertEqual((record["container"], record["session"]), ("", ""))

    def test_session_options_before_a_prompt_keep_the_channel(self):
        flags, env, _ = self._decide(rest=["--model", "opus", "review this change"])
        self.assertEqual(flags, ["--channels", CLAUDE_TELEGRAM_CHANNEL])
        self.assertEqual(env, ["AGENT_BOX_SUPPRESS_NOTIFY=1"])

    def test_plugin_management_does_not_take_the_channel(self):
        flags, env, _ = self._decide(rest=["plugin", "marketplace", "update", "flow-next"])
        self.assertEqual(flags, [])
        self.assertEqual(env, ["TELEGRAM_BOT_TOKEN="])
        self.assertIsNone(channel.channel_owner(self.lock))

    def test_leading_model_mcp_does_not_take_the_channel(self):
        """Claude accepts --model before mcp, unlike daemon and logs."""
        flags, env, _ = self._decide(rest=["--model", "opus", "mcp", "list"])
        self.assertEqual(flags, [])
        self.assertEqual(env, ["TELEGRAM_BOT_TOKEN="])
        self.assertIsNone(channel.channel_owner(self.lock))

    def test_leading_model_before_daemon_is_a_session(self):
        """Claude treats ``--model opus daemon status`` as prompt input."""
        flags, env, _ = self._decide(rest=["--model", "opus", "daemon", "status"])
        self.assertEqual(flags, ["--channels", CLAUDE_TELEGRAM_CHANNEL])
        self.assertEqual(env, ["AGENT_BOX_SUPPRESS_NOTIFY=1"])
        self.assertIsNotNone(channel.channel_owner(self.lock))

    def test_init_only_does_not_take_the_channel(self):
        flags, env, _ = self._decide(rest=["--init-only"])
        self.assertEqual(flags, [])
        self.assertEqual(env, ["TELEGRAM_BOT_TOKEN="])
        self.assertIsNone(channel.channel_owner(self.lock))

    def test_background_exec_does_not_take_the_channel(self):
        flags, env, _ = self._decide(rest=["--bg", "--exec", "pytest -x"])
        self.assertEqual(flags, [])
        self.assertEqual(env, ["TELEGRAM_BOT_TOKEN="])
        self.assertIsNone(channel.channel_owner(self.lock))

    def test_second_session_falls_back_to_a_normal_claude(self):
        self._decide()
        flags, env, err = self._decide()
        self.assertEqual(flags, [], "no --channels for the second session")
        self.assertEqual(env, ["TELEGRAM_BOT_TOKEN="], "its plugin must not load the shared .env")
        self.assertIn("Telegram channel already active in another Claude session", err)

    def test_prerequisites_lost_after_enabling_warns_but_still_runs(self):
        flags, env, err = self._decide(ready=False)
        self.assertEqual(flags, [])
        self.assertIn("Telegram channel unavailable", err)
        self.assertIn("starting Claude without it", err)

    def test_disabled_setting_takes_no_lock(self):
        self.config.agents.claude.telegram_channel = False
        flags, env, _ = self._decide()
        self.assertEqual(flags, [])
        self.assertEqual(env, ["TELEGRAM_BOT_TOKEN="])
        self.assertIsNone(channel.channel_owner(self.lock))

    def test_empty_token_has_the_official_plugin_precedence_needed_for_isolation(self):
        """Pin the upstream contract rather than assuming --channels gates startup.

        The installed plugin loads .env only when the process variable is
        undefined.  An empty explicit value therefore prevents it from
        discovering the persisted shared token and polling Telegram.
        """
        source = (Path.home() / ".claude/plugins/marketplaces/claude-plugins-official"
                  "/external_plugins/telegram/server.ts")
        if not source.is_file():
            self.skipTest("official Telegram plugin source is not installed")
        text = source.read_text()
        self.assertIn("process.env[m[1]] === undefined", text)
        self.assertIn("const TOKEN = process.env.TELEGRAM_BOT_TOKEN", text)

    def test_codex_never_touches_the_channel(self):
        home = claude_home(str(self.root / "claude"), plugin=True, token=True)
        with mock.patch.object(channel, "CLAUDE_HOME", home), \
             mock.patch.object(channel, "CHANNEL_LOCK", self.lock):
            flags, env = runtime.claude_channel_args("codex", self.config, self.workspace)
        self.assertEqual((flags, env), ([], []))
        self.assertIsNone(channel.channel_owner(self.lock))

    def test_stale_lock_lets_the_next_session_take_the_channel(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text(json.dumps(
            {"pid": 2 ** 30, "pid_start": "999", "boot_id": channel._boot_id()}))
        flags, _, _ = self._decide()
        self.assertEqual(flags, ["--channels", CLAUDE_TELEGRAM_CHANNEL])


class ClaudeManagementArgvTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.lock = self.root / "runtime/telegram-channel.json"
        self.workspace = Workspace(self.root, self.root)
        self.config = Config()
        self.config.agents.claude.telegram_channel = True

    def _launch(self, rest):
        home = claude_home(str(self.root / "claude"), plugin=True, token=True)
        captured = []
        extra_env = []

        def fake_launch(_spec, _workspace, command, env, _process_env):
            captured.append(command)
            extra_env.extend(env)
            return 0

        with mock.patch.object(runtime.host_podman, "require"), \
             mock.patch.object(runtime, "resolve_workspace", return_value=self.workspace), \
             mock.patch.object(runtime, "ensure_state_dirs"), \
             mock.patch.object(runtime, "ensure_refresh_token"), \
             mock.patch.object(runtime, "load_config", return_value=self.config), \
             mock.patch.object(runtime, "image_exists", return_value=True), \
             mock.patch.object(runtime, "sync_managed_skills"), \
             mock.patch.object(runtime, "claude_skill_links"), \
             mock.patch.object(runtime, "workspace_container_spec", return_value=mock.Mock()), \
             mock.patch.object(runtime, "existing_workspace_spec", return_value=None), \
             mock.patch.object(runtime, "launch_in_workspace_container", side_effect=fake_launch), \
             mock.patch.object(runtime, "CLAUDE_HOME", home), \
             mock.patch.object(runtime, "POLICY_RUNTIME_DIR", home / "runtime/policy"), \
             mock.patch.object(channel, "CLAUDE_HOME", home), \
             mock.patch.object(channel, "CHANNEL_LOCK", self.lock):
            with self.assertRaises(SystemExit):
                runtime.runtime_main(["claude", *rest], runtime.PACKAGE_ROOT)
        return captured[0], extra_env

    def test_bare_session_gets_the_container_bypass_and_telegram_channel(self):
        argv, extra_env = self._launch([])
        self.assertEqual(argv[-4:], [
            "claude", "--dangerously-skip-permissions", "--channels", CLAUDE_TELEGRAM_CHANNEL,
        ])
        self.assertIn("AGENT_BOX_SUPPRESS_NOTIFY=1", extra_env)
        self.assertIsNone(channel.channel_owner(self.lock))

    def test_positional_prompt_precedes_the_variadic_telegram_channel(self):
        argv, _ = self._launch(["review this change"])
        self.assertEqual(argv[-5:], [
            "claude", "--dangerously-skip-permissions", "review this change",
            "--channels", CLAUDE_TELEGRAM_CHANNEL,
        ])

    def test_model_and_positional_prompt_precede_the_telegram_channel(self):
        argv, _ = self._launch(["--model", "opus", "review this change"])
        self.assertEqual(argv[-7:], [
            "claude", "--dangerously-skip-permissions", "--model", "opus", "review this change",
            "--channels", CLAUDE_TELEGRAM_CHANNEL,
        ])

    def test_plugin_marketplace_update_keeps_argv_without_session_flags_or_a_lock(self):
        rest = ["plugin", "marketplace", "update", "flow-next"]
        argv, extra_env = self._launch(rest)
        self.assertEqual(argv[-5:], ["claude", *rest])
        self.assertNotIn("--channels", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertIn("TELEGRAM_BOT_TOKEN=", extra_env)
        self.assertIsNone(channel.channel_owner(self.lock))

    def test_mcp_management_keeps_argv_without_session_flags_or_a_lock(self):
        rest = ["mcp", "list"]
        argv, extra_env = self._launch(rest)
        self.assertEqual(argv[-3:], ["claude", *rest])
        self.assertNotIn("--channels", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertIn("TELEGRAM_BOT_TOKEN=", extra_env)
        self.assertIsNone(channel.channel_owner(self.lock))

    def test_leading_model_mcp_keeps_argv_without_session_flags(self):
        rest = ["--model", "opus", "mcp", "list"]
        argv, extra_env = self._launch(rest)
        self.assertEqual(argv[-5:], ["claude", *rest])
        self.assertNotIn("--channels", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertIn("TELEGRAM_BOT_TOKEN=", extra_env)
        self.assertIsNone(channel.channel_owner(self.lock))

    def test_import_starts_a_session_with_flags_and_a_channel(self):
        rest = ["import", "--dry-run", "codex"]
        argv, extra_env = self._launch(rest)
        self.assertEqual(argv[-7:], [
            "claude", "--dangerously-skip-permissions", *rest,
            "--channels", CLAUDE_TELEGRAM_CHANNEL,
        ])
        self.assertIn("AGENT_BOX_SUPPRESS_NOTIFY=1", extra_env)
        self.assertIsNone(channel.channel_owner(self.lock))

    def test_daemon_keeps_argv_without_session_flags(self):
        rest = ["daemon", "status"]
        argv, extra_env = self._launch(rest)
        self.assertEqual(argv[-3:], ["claude", *rest])
        self.assertNotIn("--channels", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertIn("TELEGRAM_BOT_TOKEN=", extra_env)
        self.assertIsNone(channel.channel_owner(self.lock))

    def test_leading_model_before_daemon_keeps_session_behavior(self):
        rest = ["--model", "opus", "daemon", "status"]
        argv, extra_env = self._launch(rest)
        self.assertEqual(argv[-8:], [
            "claude", "--dangerously-skip-permissions", *rest,
            "--channels", CLAUDE_TELEGRAM_CHANNEL,
        ])
        self.assertIn("AGENT_BOX_SUPPRESS_NOTIFY=1", extra_env)
        self.assertIsNone(channel.channel_owner(self.lock))

    def test_init_only_keeps_argv_without_session_flags(self):
        rest = ["--init-only"]
        argv, extra_env = self._launch(rest)
        self.assertEqual(argv[-2:], ["claude", *rest])
        self.assertNotIn("--channels", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertIn("TELEGRAM_BOT_TOKEN=", extra_env)
        self.assertIsNone(channel.channel_owner(self.lock))

    def test_background_exec_keeps_argv_without_session_flags(self):
        rest = ["--bg", "--exec", "pytest -x"]
        argv, extra_env = self._launch(rest)
        self.assertEqual(argv[-4:], ["claude", *rest])
        self.assertNotIn("--channels", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertIn("TELEGRAM_BOT_TOKEN=", extra_env)
        self.assertIsNone(channel.channel_owner(self.lock))

    def test_leading_model_version_keeps_argv_without_session_flags(self):
        rest = ["--model", "opus", "--version"]
        argv, extra_env = self._launch(rest)
        self.assertEqual(argv[-4:], ["claude", *rest])
        self.assertNotIn("--channels", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertIn("TELEGRAM_BOT_TOKEN=", extra_env)
        self.assertIsNone(channel.channel_owner(self.lock))


class RuntimeNotificationSelectionTests(unittest.TestCase):
    """Only agents that can emit outbound notifications receive its token."""

    def _run(self, agent, channel):
        config = Config()
        config.host.notifications.enabled = True
        config.host.notifications.telegram_token = "token"
        config.host.notifications.telegram_chat_id = "chat"
        workspace = Workspace(Path("/project"), Path("/project"))
        captured = []

        def fake_launch(_spec, _workspace, _command, env, _process_env):
            captured.append(env)
            return 0

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(runtime.host_podman, "require"), \
             mock.patch.object(runtime, "resolve_workspace", return_value=workspace), \
             mock.patch.object(runtime, "ensure_state_dirs"), \
             mock.patch.object(runtime, "ensure_refresh_token"), \
             mock.patch.object(runtime, "load_config", return_value=config), \
             mock.patch.object(runtime, "image_exists", return_value=True), \
             mock.patch.object(runtime, "sync_managed_skills"), \
             mock.patch.object(runtime, "claude_skill_links"), \
             mock.patch.object(runtime, "claude_channel_args", return_value=(channel, [])), \
             mock.patch.object(runtime, "workspace_container_spec", return_value=mock.Mock()), \
             mock.patch.object(runtime, "existing_workspace_spec", return_value=None), \
             mock.patch.object(runtime, "launch_in_workspace_container", side_effect=fake_launch), \
             mock.patch.object(runtime, "CLAUDE_HOME", Path(tmp) / "claude"), \
             mock.patch.object(runtime, "POLICY_RUNTIME_DIR", Path(tmp) / "runtime/policy"):
            with self.assertRaises(SystemExit):
                runtime.runtime_main([agent], runtime.PACKAGE_ROOT)
        return captured

    def test_credentials_are_scoped_by_actual_launch_role(self):
        for agent, channel_flags, expected in (
            ("codex", [], True), ("claude", [], True),
            ("claude", ["--channels", CLAUDE_TELEGRAM_CHANNEL], True),
            ("shell", [], False), ("doctor", [], False),
        ):
            env = self._run(agent, channel_flags)[0]
            self.assertEqual(notify.NOTIFY_TOKEN_ENV in env, expected)


class ConfigEnableGatingTests(unittest.TestCase):
    def test_missing_channel_prerequisites_prevent_menu_save(self):
        from agent_box import config_menu
        config = Config()
        config.agents.claude.telegram_channel = True
        with mock.patch.object(cli.channel_module, "missing_requirements", return_value=["Install the Telegram plugin"]):
            self.assertIn("Install the Telegram plugin", config_menu.validate(config))

    def test_ready_channel_can_be_saved_or_left_disabled(self):
        from agent_box import config_menu
        with mock.patch.object(cli.channel_module, "missing_requirements", return_value=[]):
            for enabled in (True, False):
                config = Config()
                config.agents.claude.telegram_channel = enabled
                self.assertEqual(config_menu.validate(config), [])


class ConfigCheckHonestyTests(unittest.TestCase):
    def _check(self, config, *, ready):
        home = claude_home(str(self.root / "claude"), plugin=ready, token=ready)
        out = io.StringIO()
        code = 0
        with mock.patch.object(cli.channel_module, "CLAUDE_HOME", home), \
             mock.patch.object(cli.channel_module, "CHANNEL_LOCK", self.root / "lock.json"), \
             mock.patch.object(cli, "load_config", return_value=config), \
             redirect_stdout(out):
            try:
                cli.config_check()
            except SystemExit as exc:
                code = exc.code
        return out.getvalue(), code

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_disabled_setting_never_claims_the_channel_will_be_used(self):
        config = Config()  # prerequisites ready, but the switch is off
        text, code = self._check(config, ready=True)
        self.assertNotIn("will pass --channels", text)
        self.assertIn("the channel is disabled", text)
        self.assertEqual(code, 1)

    def test_enabled_and_ready_reports_the_flag(self):
        config = Config()
        config.agents.claude.telegram_channel = True
        text, code = self._check(config, ready=True)
        self.assertIn(f"--channels {CLAUDE_TELEGRAM_CHANNEL}", text)
        self.assertEqual(code, 0)

    def test_missing_prerequisites_print_setup_steps(self):
        config = Config()
        config.agents.claude.telegram_channel = True
        text, code = self._check(config, ready=False)
        self.assertIn("/telegram:configure", text)
        self.assertEqual(code, 1)

    def test_reports_whether_the_channel_is_currently_active(self):
        config = Config()
        config.agents.claude.telegram_channel = True
        with mock.patch.object(cli.channel_module, "CHANNEL_LOCK", self.root / "lock.json"):
            channel.acquire_channel("/p", self.root / "lock.json")
        text, _ = self._check(config, ready=True)
        self.assertIn("Currently active:        yes", text)


if __name__ == "__main__":
    unittest.main()
