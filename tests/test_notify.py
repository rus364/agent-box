import tempfile
import unittest
from pathlib import Path

from agent_box import notify


class NotifyLogicTests(unittest.TestCase):
    def test_html_escaping(self):
        text = notify.telegram_notice("💬", "Claude", "a<b>&", "Waiting & ready")
        self.assertEqual(text, "💬 <b>Claude · a&lt;b&gt;&amp;</b>\nWaiting &amp; ready")

    def test_project_from_cwd(self):
        self.assertEqual(notify.notify_project("/tmp/my-project"), "my-project")
        self.assertEqual(notify.notify_project("/"), "unknown")
        self.assertEqual(notify.notify_project(""), "unknown")

    def test_parse_payload_bad_json(self):
        self.assertEqual(notify.parse_payload("not json"), {})
        self.assertEqual(notify.parse_payload("[]"), {})
        self.assertEqual(notify.parse_payload('{"a": 1}'), {"a": 1})

    def test_background_tasks_extraction(self):
        self.assertIsNone(notify.claude_background_tasks({}))
        self.assertEqual(notify.claude_background_tasks({"background_tasks": []}), [])
        self.assertEqual(notify.claude_background_tasks({"background_tasks": [1, 2]}), [1, 2])
        self.assertIsNone(notify.claude_background_tasks({"background_tasks": "nope"}))

    def test_background_task_suppression_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(notify.claude_background_active("s1", root))
            notify.update_claude_background_state("s1", [{"id": "x"}], root)
            self.assertTrue(notify.claude_background_active("s1", root))
            notify.update_claude_background_state("s1", [], root)
            self.assertFalse(notify.claude_background_active("s1", root))

    def test_background_state_ignores_none_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notify.update_claude_background_state("s1", None, root)
            self.assertFalse(notify.claude_background_active("s1", root))

    def test_hook_install_and_remove_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            notify.install_claude_notify_hooks(path)
            settings = __import__("json").loads(path.read_text())
            self.assertIn("Stop", settings["hooks"])
            notify.remove_claude_notify_hooks(path)
            settings = __import__("json").loads(path.read_text())
            self.assertNotIn("hooks", settings)

    def test_hook_install_preserves_unrelated_settings(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"model": "opus", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}}))
            notify.install_claude_notify_hooks(path)
            settings = json.loads(path.read_text())
            self.assertEqual(settings["model"], "opus")
            self.assertIn("PreToolUse", settings["hooks"])


if __name__ == "__main__":
    unittest.main()
