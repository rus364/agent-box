import tempfile
import unittest
from pathlib import Path

from agent_box.telegram.pending import Option, PendingRegistry


class PendingRegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.registry = PendingRegistry(Path(self._tmp.name) / "pending.json")

    def tearDown(self):
        self._tmp.cleanup()

    def _make(self, user_id=100):
        return self.registry.create("sess-1", chat_id=5, user_id=user_id, question_id="q1",
                                     options=[Option("Keep it", "keep"), Option("Replace it", "replace")])

    def test_token_is_short_and_opaque(self):
        pending = self._make()
        self.assertLessEqual(len(pending.token), 16)
        self.assertNotIn("keep", pending.token)
        self.assertNotIn("q1", pending.token)

    def test_resolve_by_correct_user(self):
        pending = self._make(user_id=100)
        resolved = self.registry.resolve(pending.token, user_id=100)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.session_id, "sess-1")

    def test_resolve_wrong_user_fails_closed(self):
        pending = self._make(user_id=100)
        resolved = self.registry.resolve(pending.token, user_id=999)
        self.assertIsNone(resolved)

    def test_resolve_wrong_user_still_consumes_token(self):
        pending = self._make(user_id=100)
        self.registry.resolve(pending.token, user_id=999)
        # A retry by the correct user must also fail: the token is burned.
        self.assertIsNone(self.registry.resolve(pending.token, user_id=100))

    def test_double_answer_rejected(self):
        pending = self._make(user_id=100)
        first = self.registry.resolve(pending.token, user_id=100)
        second = self.registry.resolve(pending.token, user_id=100)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_unknown_token_returns_none(self):
        self.assertIsNone(self.registry.resolve("does-not-exist", user_id=1))

    def test_expired_question_is_pruned(self):
        pending = self._make(user_id=100)
        expired_registry = PendingRegistry(self.registry.path, ttl=-1)
        self.assertIsNone(expired_registry.resolve(pending.token, user_id=100))

    def test_discard_session_removes_its_questions(self):
        pending = self._make(user_id=100)
        self.registry.discard_session("sess-1")
        self.assertIsNone(self.registry.resolve(pending.token, user_id=100))


if __name__ == "__main__":
    unittest.main()
