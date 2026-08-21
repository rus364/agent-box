"""Wiring for Anthropic's official Telegram channel plugin.

agent-box implements none of the Telegram control path; what it owns is the
decision to pass ``--channels plugin:telegram@claude-plugins-official`` and
the preflight that keeps a half-configured setup from looking like it worked.
The failure mode these tests exist to prevent: Claude starts, the terminal
looks normal, and nothing ever reaches Telegram because the plugin was never
installed or has no token.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import channel
from agent_box.config import CLAUDE_TELEGRAM_CHANNEL, CLAUDE_TELEGRAM_PLUGIN
from agent_box.errors import AgentBoxError


def claude_home(tmp: str, *, plugin: bool = False, token: bool = False) -> Path:
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
    def setUp(self):
        # The plugin reads the real environment first; isolate from the host.
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        self.addCleanup(self.env.stop)

    def test_token_in_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(channel.token_configured(claude_home(tmp, token=True)))

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


class ChannelArgsTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        self.addCleanup(self.env.stop)

    def test_ready_returns_the_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = claude_home(tmp, plugin=True, token=True)
            self.assertEqual(channel.missing_requirements(home), [])
            self.assertEqual(channel.channel_args(home), ["--channels", CLAUDE_TELEGRAM_CHANNEL])

    def test_nothing_set_up_reports_both_gaps_with_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(len(channel.missing_requirements(Path(tmp))), 2)
            with self.assertRaises(AgentBoxError) as ctx:
                channel.channel_args(Path(tmp))
            message = str(ctx.exception)
            self.assertIn(f"/plugin install {CLAUDE_TELEGRAM_PLUGIN}", message)
            self.assertIn("/telegram:configure", message)
            self.assertIn("agent-box config", message)

    def test_plugin_without_token_still_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = claude_home(tmp, plugin=True)
            self.assertEqual(len(channel.missing_requirements(home)), 1)
            with self.assertRaises(AgentBoxError):
                channel.channel_args(home)

    def test_error_never_leaks_the_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = claude_home(tmp, token=True)
            with self.assertRaises(AgentBoxError) as ctx:
                channel.channel_args(home)
            self.assertNotIn("AAHfiqksKZ8", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
