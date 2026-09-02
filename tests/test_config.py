"""The unified agent-box configuration.

The load/save round trip is trivial; what these tests actually pin down is the
security posture of the defaults. Every switch that hands host credentials to
a container must be off until the user turns it on in ``agent-box config`` --
in particular the GitHub token, which an earlier agent-box forwarded to every
container purely because ``gh`` happened to be authenticated on the host.
"""
import json
import tempfile
import unittest
from pathlib import Path

from agent_box import config as config_module
from agent_box.config import Config, load_config, save_config


class DefaultsAreSafeTests(unittest.TestCase):
    def test_credential_passthrough_is_off_by_default(self):
        config = Config()
        self.assertFalse(config.github.gh_token)
        self.assertFalse(config.github.gh_config)
        self.assertFalse(config.claude.telegram_channel)
        self.assertFalse(config.notifications.enabled)

    def test_provider_api_keys_are_off_by_default(self):
        """An ANTHROPIC_API_KEY exported in your shell is not a decision to
        hand it to an agent container; the agents authenticate through the
        mounted ~/.claude / ~/.codex directories instead."""
        self.assertFalse(Config().providers.api_keys)

    def test_full_git_config_copy_is_off_by_default(self):
        """A real ~/.gitconfig routinely carries credential.helper entries,
        http.<url>.extraHeader tokens and include.path directives."""
        self.assertFalse(Config().git.full_config)

    def test_non_secret_conveniences_default_on(self):
        config = Config()
        self.assertTrue(config.git.identity)       # only user.name / user.email
        self.assertTrue(config.providers.transport)  # endpoints, proxies, CA bundle

    def test_missing_file_yields_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(Path(tmp) / "missing.json")
            self.assertFalse(config.github.gh_token)

    def test_unreadable_file_yields_defaults_rather_than_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{ not json")
            self.assertFalse(load_config(path).github.gh_token)

    def test_partial_file_does_not_silently_enable_anything(self):
        """A file written by a future/older agent-box must not turn a
        credential switch on just by omitting it."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"version": 1, "claude": {"telegram_channel": True}}))
            config = load_config(path)
            self.assertTrue(config.claude.telegram_channel)
            self.assertFalse(config.github.gh_token)
            self.assertFalse(config.notifications.enabled)
            self.assertFalse(config.providers.api_keys)
            self.assertFalse(config.git.full_config)


class RoundTripTests(unittest.TestCase):
    def test_save_load_roundtrip_and_permissions(self):
        config = Config()
        config.claude.telegram_channel = True
        config.notifications.enabled = True
        config.notifications.telegram_token = "123:abc"
        config.notifications.telegram_chat_id = "555"
        config.github.gh_token = True
        config.git.identity = False
        config.git.full_config = True
        config.providers.api_keys = True
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            save_config(config, path)
            # The file holds a bot token, so it must not be world-readable.
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["version"], 1)
            loaded = load_config(path)
        self.assertTrue(loaded.claude.telegram_channel)
        self.assertEqual(loaded.notifications.telegram_token, "123:abc")
        self.assertEqual(loaded.notifications.telegram_chat_id, "555")
        self.assertTrue(loaded.github.gh_token)
        self.assertFalse(loaded.git.identity)
        self.assertTrue(loaded.git.full_config)
        self.assertTrue(loaded.providers.api_keys)

    def test_unknown_keys_are_dropped_on_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"version": 1, "remote_control": {"enabled": True}}))
            save_config(load_config(path), path)
            self.assertNotIn("remote_control", json.loads(path.read_text()))


class FlowNextConfigTests(unittest.TestCase):
    def test_old_config_without_flow_next_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "version": 1,
                "claude": {"telegram_channel": False},
                "notifications": {"enabled": False, "telegram_token": "", "telegram_chat_id": ""},
                "github": {"gh_token": False, "gh_config": False},
                "git": {"identity": True, "full_config": False},
                "providers": {"api_keys": False, "transport": True},
            }))
            config = load_config(path)
        self.assertEqual(config.flow_next.review_backends["claude"], "codex:gpt-5.6-sol:medium")
        self.assertEqual(config.flow_next.review_backends["codex"], "codex:gpt-5.6-sol:medium")
        self.assertFalse(config.flow_next.auto_push)

    def test_default_review_mapping(self):
        config = Config()
        self.assertEqual(config.flow_next.review_backends, {
            "claude": "codex:gpt-5.6-sol:medium",
            "codex": "codex:gpt-5.6-sol:medium",
        })

    def test_auto_push_defaults_false(self):
        self.assertFalse(Config().flow_next.auto_push)

    def test_configured_mapping_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "version": 1,
                "flow_next": {"review_backends": {"claude": "claude:opus-9:high"}, "auto_push": True},
            }))
            config = load_config(path)
        self.assertEqual(config.flow_next.review_backends["claude"], "claude:opus-9:high")
        # codex mapping wasn't overridden, so it keeps its default.
        self.assertEqual(config.flow_next.review_backends["codex"], "codex:gpt-5.6-sol:medium")
        self.assertTrue(config.flow_next.auto_push)

    def test_malformed_review_backends_fall_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "version": 1,
                "flow_next": {"review_backends": {"claude": 12345, "codex": ""}, "auto_push": "yes"},
            }))
            config = load_config(path)
        self.assertEqual(config.flow_next.review_backends["claude"], "codex:gpt-5.6-sol:medium")
        self.assertEqual(config.flow_next.review_backends["codex"], "codex:gpt-5.6-sol:medium")
        self.assertFalse(config.flow_next.auto_push)

    def test_flow_next_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = Config()
            config.flow_next.auto_push = True
            config.flow_next.review_backends["claude"] = "claude:future-model:high"
            save_config(config, path)
            loaded = load_config(path)
        self.assertTrue(loaded.flow_next.auto_push)
        self.assertEqual(loaded.flow_next.review_backends["claude"], "claude:future-model:high")


class NotificationsUsableTests(unittest.TestCase):
    def test_enabled_alone_is_not_enough(self):
        config = Config()
        config.notifications.enabled = True
        self.assertFalse(config.notifications.usable)
        config.notifications.telegram_token = "t"
        self.assertFalse(config.notifications.usable)
        config.notifications.telegram_chat_id = "1"
        self.assertTrue(config.notifications.usable)

    def test_credentials_without_enabled_stay_inactive(self):
        config = Config()
        config.notifications.telegram_token = "t"
        config.notifications.telegram_chat_id = "1"
        self.assertFalse(config.notifications.usable)


class ClaudeStatuslineDefaultTests(unittest.TestCase):
    def test_statusline_defaults_on(self):
        self.assertTrue(config_module.ClaudeConfig().statusline)

    def test_a_config_written_before_the_setting_existed_gets_the_default(self):
        claude = config_module.ClaudeConfig.from_dict({"telegram_channel": True})
        self.assertTrue(claude.statusline)
        self.assertTrue(claude.telegram_channel)

    def test_an_explicit_false_is_kept(self):
        self.assertFalse(
            config_module.ClaudeConfig.from_dict({"statusline": False}).statusline)

    def test_round_trip(self):
        claude = config_module.ClaudeConfig(telegram_channel=True, statusline=False)
        self.assertEqual(
            config_module.ClaudeConfig.from_dict(claude.to_dict()), claude)


if __name__ == "__main__":
    unittest.main()
