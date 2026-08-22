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

from agent_box import channel, cli, runtime
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
            set(record) - {"pid", "pid_start", "boot_id", "project", "started_at"}, set(),
            "the lock must stay minimal — no session state, no credentials",
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


class LaunchDecisionTests(unittest.TestCase):
    """runtime.claude_channel_args: never blocks Claude from starting."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.lock = self.root / "runtime/telegram-channel.json"
        self.workspace = Workspace(self.root, self.root)
        self.config = Config()
        self.config.claude.telegram_channel = True

    def _decide(self, *, ready=True):
        home = claude_home(str(self.root / "claude"), plugin=ready, token=ready)
        err = io.StringIO()
        with mock.patch.object(channel, "CLAUDE_HOME", home), \
             mock.patch.object(channel, "CHANNEL_LOCK", self.lock), \
             redirect_stderr(err):
            flags, env = runtime.claude_channel_args("claude", self.config, self.workspace)
        return flags, env, err.getvalue()

    def test_ready_and_free_takes_the_channel(self):
        flags, env, _ = self._decide()
        self.assertEqual(flags, ["--channels", CLAUDE_TELEGRAM_CHANNEL])
        self.assertEqual(env, ["AGENT_BOX_SUPPRESS_NOTIFY=1"])

    def test_second_session_falls_back_to_a_normal_claude(self):
        self._decide()
        flags, env, err = self._decide()
        self.assertEqual(flags, [], "no --channels for the second session")
        self.assertEqual(env, [], "and its notification hooks stay active")
        self.assertIn("Telegram channel already active in another Claude session", err)

    def test_prerequisites_lost_after_enabling_warns_but_still_runs(self):
        flags, env, err = self._decide(ready=False)
        self.assertEqual(flags, [])
        self.assertIn("Telegram channel unavailable", err)
        self.assertIn("starting Claude without it", err)

    def test_disabled_setting_takes_no_lock(self):
        self.config.claude.telegram_channel = False
        flags, _, _ = self._decide()
        self.assertEqual(flags, [])
        self.assertIsNone(channel.channel_owner(self.lock))

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


class ConfigEnableGatingTests(unittest.TestCase):
    """`agent-box config` must not store an enabled channel that cannot work."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config_file = self.root / "config.json"

    def _run_config(self, *, ready, answers):
        home = claude_home(str(self.root / "claude"), plugin=ready, token=ready)
        saved = {}
        out = io.StringIO()
        # getpass must be mocked too, or a token prompt reads the real tty and
        # the test hangs instead of failing.
        with mock.patch.object(cli.getpass, "getpass", return_value=""), \
             mock.patch.object(cli.channel_module, "CLAUDE_HOME", home), \
             mock.patch.object(cli, "CONFIG_FILE", self.config_file), \
             mock.patch.object(cli, "CLAUDE_HOME", self.root / "claude"), \
             mock.patch.object(cli, "load_config", return_value=Config()), \
             mock.patch.object(cli, "save_config", lambda c, *a: saved.update(c.to_dict())), \
             mock.patch.object(cli.notify_module, "sync_credentials"), \
             mock.patch.object(cli.notify_module, "remove_claude_notify_hooks"), \
             mock.patch.object(cli, "config_show"), \
             mock.patch("builtins.input", side_effect=list(answers) + ["n"] * 20), \
             redirect_stdout(out):
            cli.config_interactive()
        return saved, out.getvalue()

    def test_cannot_enable_before_the_plugin_exists(self):
        saved, out = self._run_config(ready=False, answers=["y"])
        self.assertFalse(saved["claude"]["telegram_channel"])
        self.assertIn("Not available yet", out)
        self.assertIn(f"/plugin install {CLAUDE_TELEGRAM_PLUGIN}", out)

    def test_ready_prerequisites_allow_enabling(self):
        saved, _ = self._run_config(ready=True, answers=["y"])
        self.assertTrue(saved["claude"]["telegram_channel"])

    def test_ready_prerequisites_still_allow_declining(self):
        saved, _ = self._run_config(ready=True, answers=["n"])
        self.assertFalse(saved["claude"]["telegram_channel"])


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
        config.claude.telegram_channel = True
        text, code = self._check(config, ready=True)
        self.assertIn(f"--channels {CLAUDE_TELEGRAM_CHANNEL}", text)
        self.assertEqual(code, 0)

    def test_missing_prerequisites_print_setup_steps(self):
        config = Config()
        config.claude.telegram_channel = True
        text, code = self._check(config, ready=False)
        self.assertIn("/telegram:configure", text)
        self.assertEqual(code, 1)

    def test_reports_whether_the_channel_is_currently_active(self):
        config = Config()
        config.claude.telegram_channel = True
        with mock.patch.object(cli.channel_module, "CHANNEL_LOCK", self.root / "lock.json"):
            channel.acquire_channel("/p", self.root / "lock.json")
        text, _ = self._check(config, ready=True)
        self.assertIn("Currently active:        yes", text)


if __name__ == "__main__":
    unittest.main()
