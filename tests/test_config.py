import tempfile
import unittest
from pathlib import Path

from agent_box.config import TelegramConfig, load_telegram_config, save_telegram_config


class ConfigMigrationTests(unittest.TestCase):
    def test_legacy_two_key_schema_loads(self):
        config = TelegramConfig.from_dict({"token": "abc", "chat_id": "555"})
        self.assertEqual(config.token, "abc")
        self.assertEqual(config.notify_chat_id, "555")
        self.assertFalse(config.remote_control.enabled)
        self.assertEqual(config.remote_control.allowed_user_ids, [])

    def test_new_schema_roundtrip(self):
        config = TelegramConfig(token="t", notify_chat_id="1")
        config.remote_control.enabled = True
        config.remote_control.allowed_user_ids = [42]
        config.remote_control.workspace_roots = ["~/code"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "telegram.json"
            save_telegram_config(config, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            loaded = load_telegram_config(path)
        self.assertEqual(loaded.token, "t")
        self.assertTrue(loaded.remote_control.enabled)
        self.assertEqual(loaded.remote_control.allowed_user_ids, [42])
        self.assertEqual(loaded.remote_control.workspace_roots, ["~/code"])

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_telegram_config(Path(tmp) / "missing.json"))

    def test_remote_control_disabled_by_default_even_with_notify_configured(self):
        config = TelegramConfig.from_dict({"token": "t", "chat_id": "1"})
        self.assertTrue(config.configured)
        self.assertFalse(config.remote_control.enabled)


if __name__ == "__main__":
    unittest.main()
