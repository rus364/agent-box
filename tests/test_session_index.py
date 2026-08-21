"""The notification session pointer must be real, readable, and non-secret.

Two separate bugs are covered here:

1. The pointer text was ``/sessions <id>``, which the bot never accepted --
   ``/sessions`` takes no argument. The command is now ``/session <id>``.
2. The lookup read the Telegram *session registry*, which lives under
   ``TELEGRAM_STATE_ROOT``. Notification hooks run inside the container, where
   that directory is deliberately not mounted, so the lookup always came back
   empty there. It now reads a minimal published index under the notification
   state directory, which *is* mounted -- and that index must not carry the
   pending-question callback tokens or the getUpdates offset, which is exactly
   why the state directory itself stays out of the container.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_box import notify, session_index


def fake_session(**kwargs):
    base = {"id": "sess1", "provider": "claude", "host_project": "/projects/app",
            "chat_id": 42, "owner_user_id": 7, "provider_session_id": "secret-thread",
            "status": "running"}
    base.update(kwargs)
    return SimpleNamespace(**base)


class PublishTests(unittest.TestCase):
    def test_only_allow_listed_fields_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions-index.json"
            session_index.publish([fake_session()], path)
            raw = path.read_text()
            self.assertEqual(json.loads(raw)["sessions"], [
                {"host_project": "/projects/app", "id": "sess1", "provider": "claude"},
            ])
            for forbidden in ("chat_id", "owner_user_id", "provider_session_id",
                              "secret-thread", "status", "token", "offset"):
                self.assertNotIn(forbidden, raw)

    def test_index_file_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions-index.json"
            session_index.publish([fake_session()], path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_index_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(session_index.load(Path(tmp) / "nope.json"), [])
            self.assertIsNone(session_index.find("claude", "/x", Path(tmp) / "nope.json"))

    def test_corrupt_index_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions-index.json"
            path.write_text("{not json")
            self.assertEqual(session_index.load(path), [])

    def test_find_matches_provider_and_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions-index.json"
            session_index.publish([
                fake_session(id="a", provider="claude", host_project="/projects/app"),
                fake_session(id="b", provider="codex", host_project="/projects/app"),
            ], path)
            self.assertEqual(session_index.find("claude", "/projects/app", path), "a")
            self.assertEqual(session_index.find("codex", "/projects/app", path), "b")
            self.assertIsNone(session_index.find("claude", "/projects/other", path))
            self.assertIsNone(session_index.find("claude", "", path))


class NotifyHintTests(unittest.TestCase):
    def _hint(self, tmp, provider, cwd, sessions):
        path = Path(tmp) / "sessions-index.json"
        session_index.publish(sessions, path)
        with mock.patch("agent_box.session_index.TELEGRAM_SESSION_INDEX_FILE", path):
            return notify._session_hint(provider, cwd)

    def test_hint_uses_the_command_the_bot_actually_accepts(self):
        with tempfile.TemporaryDirectory() as tmp:
            hint = self._hint(tmp, "claude", "/projects/app", [fake_session()])
        self.assertEqual(hint, "/session sess1")

    def test_hint_is_empty_without_a_matching_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            hint = self._hint(tmp, "codex", "/projects/app", [fake_session()])
        self.assertEqual(hint, "")

    def test_hint_is_empty_when_the_index_is_unreadable(self):
        with mock.patch("agent_box.session_index.find", side_effect=OSError("boom")):
            self.assertEqual(notify._session_hint("claude", "/projects/app"), "")

    def test_hint_does_not_read_the_telegram_state_directory(self):
        """Regression: the old lookup used SessionRegistry, which is
        unreadable from inside the container."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions-index.json"
            session_index.publish([fake_session()], path)
            with mock.patch("agent_box.session_index.TELEGRAM_SESSION_INDEX_FILE", path), \
                 mock.patch("agent_box.telegram.sessions.SessionRegistry") as registry:
                hint = notify._session_hint("claude", "/projects/app")
        self.assertEqual(hint, "/session sess1")
        registry.assert_not_called()


class NotifyMessageTests(unittest.TestCase):
    def test_idle_notification_carries_the_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions-index.json"
            session_index.publish([fake_session()], path)
            sent = []
            payload = json.dumps({
                "hook_event_name": "Notification", "notification_type": "idle_prompt",
                "session_id": "cc-1", "cwd": "/projects/app", "message": "waiting",
            })
            with mock.patch("agent_box.session_index.TELEGRAM_SESSION_INDEX_FILE", path), \
                 mock.patch("agent_box.notify.telegram_send", lambda text, **kw: sent.append(text) or True), \
                 mock.patch("agent_box.notify.claude_background_active", return_value=False), \
                 mock.patch("sys.stdin"), \
                 mock.patch("agent_box.notify.sys.stdin.read", return_value=payload):
                notify.notify_claude()
        self.assertIn("/session sess1", sent[0])


class NotifyStateDirectoryIsolationTests(unittest.TestCase):
    """The index lives beside the notification state, which is the only
    Telegram-adjacent directory bind-mounted into the container."""

    def test_index_path_is_under_the_mounted_notify_state_root(self):
        from agent_box.paths import NOTIFY_STATE_ROOT, TELEGRAM_SESSION_INDEX_FILE, TELEGRAM_STATE_ROOT
        self.assertEqual(TELEGRAM_SESSION_INDEX_FILE.parent, NOTIFY_STATE_ROOT)
        self.assertNotEqual(TELEGRAM_SESSION_INDEX_FILE.parent, TELEGRAM_STATE_ROOT)

    def test_telegram_state_root_is_not_mounted_into_the_container(self):
        import tempfile as tf

        from agent_box import runtime
        from agent_box.paths import TELEGRAM_STATE_ROOT
        from agent_box.workspace import Workspace
        with tf.TemporaryDirectory() as project:
            workspace = Workspace(Path(project), Path(project))
            argv = runtime.machine_podman_argv(workspace, [], Path(project), ["claude"])
        mounts = [a for a in argv if a.startswith("type=bind")]
        self.assertFalse(
            [m for m in mounts if str(TELEGRAM_STATE_ROOT) in m],
            "pending-question tokens and the polling offset must stay out of the container",
        )


if __name__ == "__main__":
    unittest.main()
