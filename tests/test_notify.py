import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import notify


class NotifyLogicTests(unittest.TestCase):
    def _claude_notices(
        self, payload: dict, *, official_channel: bool = False, state_root: Path | None = None,
    ) -> list[str]:
        """Run the real Claude hook sender and return its Telegram text bodies."""
        if state_root is None:
            with tempfile.TemporaryDirectory() as tmp:
                return self._claude_notices(
                    payload, official_channel=official_channel, state_root=Path(tmp))

        sent: list[str] = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        def send_response(request, **_kwargs):
            sent.append(json.loads(request.data.decode())["text"])
            return Response(b'{"ok": true}')

        update_state = notify.update_claude_background_state
        background_active = notify.claude_background_active
        env = {notify.SUPPRESS_NOTIFY_ENV: "1"} if official_channel else {}
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(notify, "load_credentials", return_value=("token", "chat")),
            mock.patch.object(notify.urllib.request, "urlopen", side_effect=send_response),
            mock.patch.object(
                notify, "update_claude_background_state",
                side_effect=lambda session_id, tasks: update_state(session_id, tasks, state_root),
            ),
            mock.patch.object(
                notify, "claude_background_active",
                side_effect=lambda session_id: background_active(session_id, state_root),
            ),
            mock.patch.object(notify.sys, "stdin", io.StringIO(json.dumps(payload))),
        ):
            notify.notify_claude()
        return sent

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

    def test_official_channel_still_sends_stop_completion(self):
        notices = self._claude_notices({
            "hook_event_name": "Stop", "cwd": "/work/project", "background_tasks": [],
        }, official_channel=True)
        self.assertEqual(notices, ["✅ <b>Claude · project</b>\nTurn finished"])

    def test_official_channel_still_sends_stop_failure(self):
        notices = self._claude_notices({
            "hook_event_name": "StopFailure", "cwd": "/work/project", "error": "API unavailable",
        }, official_channel=True)
        self.assertEqual(notices, [
            "❌ <b>Claude · project</b>\nStopped with an error\nAPI unavailable",
        ])

    def test_official_channel_owns_interaction_notifications(self):
        for notification_type in ("permission_prompt", "idle_prompt"):
            with self.subTest(notification_type=notification_type):
                notices = self._claude_notices({
                    "hook_event_name": "Notification",
                    "notification_type": notification_type,
                    "cwd": "/work/project",
                }, official_channel=True)
                self.assertEqual(notices, [])

    def test_official_channel_does_not_claim_unknown_notification_types(self):
        with mock.patch.dict(os.environ, {notify.SUPPRESS_NOTIFY_ENV: "1"}, clear=True):
            self.assertFalse(notify.claude_interaction_suppressed("Notification"))
            self.assertFalse(notify.claude_interaction_suppressed("Notification", "future_prompt"))

    def test_normal_claude_session_keeps_all_notification_behavior(self):
        cases = [
            (
                {"hook_event_name": "Notification", "notification_type": "permission_prompt",
                 "cwd": "/work/project", "message": "Approve this command"},
                "🔐 <b>Claude · project</b>\nPermission required\nApprove this command",
            ),
            (
                {"hook_event_name": "Notification", "notification_type": "idle_prompt",
                 "cwd": "/work/project", "message": "What next?"},
                "💬 <b>Claude · project</b>\nWaiting for input\nWhat next?",
            ),
            (
                {"hook_event_name": "Stop", "cwd": "/work/project", "background_tasks": []},
                "✅ <b>Claude · project</b>\nTurn finished",
            ),
            (
                {"hook_event_name": "StopFailure", "cwd": "/work/project", "error": "API unavailable"},
                "❌ <b>Claude · project</b>\nStopped with an error\nAPI unavailable",
            ),
        ]
        for payload, expected in cases:
            with self.subTest(event=payload["hook_event_name"]):
                self.assertEqual(self._claude_notices(payload), [expected])

    def test_official_channel_reports_background_work_without_false_completion(self):
        notices = self._claude_notices({
            "hook_event_name": "Stop",
            "cwd": "/work/project",
            "session_id": "session-1",
            "background_tasks": [{"task_id": "background-1"}],
        }, official_channel=True)
        self.assertEqual(notices, [
            "⚙️ <b>Claude · project</b>\nBackground work continues · 1 task",
        ])

    def test_codex_notification_is_not_suppressed_by_claude_channel_mode(self):
        sent: list[str] = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        def send_response(request, **_kwargs):
            sent.append(json.loads(request.data.decode())["text"])
            return Response(b'{"ok": true}')

        with (
            mock.patch.dict(os.environ, {notify.SUPPRESS_NOTIFY_ENV: "1"}, clear=True),
            mock.patch.object(notify, "load_credentials", return_value=("token", "chat")),
            mock.patch.object(notify.urllib.request, "urlopen", side_effect=send_response),
        ):
            notify.notify_codex(json.dumps({
                "type": "agent-turn-complete", "cwd": "/work/project",
                "last-assistant-message": "Completed the task.",
            }))
        self.assertEqual(sent, [
            "✅ <b>Codex · project</b>\nTurn finished\nCompleted the task.",
        ])

    def test_background_work_suppresses_the_following_idle_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notices = self._claude_notices({
                "hook_event_name": "Stop", "cwd": "/work/project", "session_id": "session-1",
                "background_tasks": [{"task_id": "background-1"}],
            }, state_root=root)
            notices += self._claude_notices({
                "hook_event_name": "Notification", "notification_type": "idle_prompt",
                "cwd": "/work/project", "session_id": "session-1",
            }, state_root=root)
        self.assertEqual(notices, [
            "⚙️ <b>Claude · project</b>\nBackground work continues · 1 task",
        ])


if __name__ == "__main__":
    unittest.main()
