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

from agent_box.config import Config, load_config, save_config


class DefaultsAreSafeTests(unittest.TestCase):
    def test_credential_passthrough_is_off_by_default(self):
        config = Config()
        self.assertFalse(config.github.gh_token)
        self.assertFalse(config.github.gh_config)
        self.assertFalse(config.claude.telegram_channel)
        self.assertFalse(config.notifications.enabled)

    def test_non_secret_conveniences_default_on(self):
        config = Config()
        self.assertTrue(config.git.config)
        self.assertTrue(config.providers.env)

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


class RoundTripTests(unittest.TestCase):
    def test_save_load_roundtrip_and_permissions(self):
        config = Config()
        config.claude.telegram_channel = True
        config.notifications.enabled = True
        config.notifications.telegram_token = "123:abc"
        config.notifications.telegram_chat_id = "555"
        config.github.gh_token = True
        config.git.config = False
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
        self.assertFalse(loaded.git.config)

    def test_unknown_keys_are_dropped_on_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"version": 1, "remote_control": {"enabled": True}}))
            save_config(load_config(path), path)
            self.assertNotIn("remote_control", json.loads(path.read_text()))


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


if __name__ == "__main__":
    unittest.main()
