import tempfile
import unittest
from pathlib import Path

from agent_box.telegram.sessions import STATUS_RUNNING, STATUS_STOPPED, SessionRegistry


class SessionRegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.registry = SessionRegistry(Path(self._tmp.name) / "sessions.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_and_get(self):
        session = self.registry.create("claude", "/home/u/code/proj", chat_id=123, owner_user_id=123)
        fetched = self.registry.get(session.id)
        self.assertEqual(fetched.provider, "claude")
        self.assertEqual(fetched.host_project, "/home/u/code/proj")
        self.assertEqual(fetched.chat_id, 123)
        self.assertEqual(fetched.status, "starting")

    def test_update_persists(self):
        session = self.registry.create("codex", "/p", chat_id=1, owner_user_id=1)
        self.registry.update(session.id, status=STATUS_RUNNING, provider_session_id="thread-1")
        fetched = self.registry.get(session.id)
        self.assertEqual(fetched.status, STATUS_RUNNING)
        self.assertEqual(fetched.provider_session_id, "thread-1")

    def test_survives_reload_from_disk(self):
        session = self.registry.create("claude", "/p", chat_id=1, owner_user_id=1)
        reopened = SessionRegistry(self.registry.path)
        self.assertIsNotNone(reopened.get(session.id))

    def test_list_for_chat_filters(self):
        self.registry.create("claude", "/p1", chat_id=1, owner_user_id=1)
        self.registry.create("claude", "/p2", chat_id=2, owner_user_id=2)
        self.assertEqual(len(self.registry.list_for_chat(1)), 1)
        self.assertEqual(len(self.registry.list_for_chat(2)), 1)
        self.assertEqual(len(self.registry.list_for_chat(3)), 0)

    def test_find_by_project_skips_stopped(self):
        session = self.registry.create("claude", "/p", chat_id=1, owner_user_id=1)
        self.assertIsNotNone(self.registry.find_by_project("claude", "/p"))
        self.registry.update(session.id, status=STATUS_STOPPED)
        self.assertIsNone(self.registry.find_by_project("claude", "/p"))

    def test_delete(self):
        session = self.registry.create("claude", "/p", chat_id=1, owner_user_id=1)
        self.registry.delete(session.id)
        self.assertIsNone(self.registry.get(session.id))

    def test_no_secrets_stored(self):
        session = self.registry.create("claude", "/p", chat_id=1, owner_user_id=1)
        raw = self.registry.path.read_text()
        for forbidden in ("token", "bot", "api_key", "password"):
            self.assertNotIn(forbidden, raw.lower())


if __name__ == "__main__":
    unittest.main()
