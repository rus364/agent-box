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

    def test_claim_by_correct_user(self):
        pending = self._make(user_id=100)
        claimed = self.registry.claim(pending.token, user_id=100)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.session_id, "sess-1")

    def test_claim_wrong_user_fails_closed(self):
        pending = self._make(user_id=100)
        claimed = self.registry.claim(pending.token, user_id=999)
        self.assertIsNone(claimed)

    def test_claim_wrong_user_still_burns_token(self):
        pending = self._make(user_id=100)
        self.registry.claim(pending.token, user_id=999)
        # A retry by the correct user must also fail: the token is burned.
        self.assertIsNone(self.registry.claim(pending.token, user_id=100))

    def test_double_claim_rejected(self):
        pending = self._make(user_id=100)
        first = self.registry.claim(pending.token, user_id=100)
        second = self.registry.claim(pending.token, user_id=100)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_unknown_token_returns_none(self):
        self.assertIsNone(self.registry.claim("does-not-exist", user_id=1))

    def test_expired_question_is_pruned(self):
        pending = self._make(user_id=100)
        expired_registry = PendingRegistry(self.registry.path, ttl=-1)
        self.assertIsNone(expired_registry.claim(pending.token, user_id=100))

    def test_discard_session_removes_its_questions(self):
        pending = self._make(user_id=100)
        self.registry.discard_session("sess-1")
        self.assertIsNone(self.registry.claim(pending.token, user_id=100))

    def test_mark_delivered_requires_prior_claim(self):
        pending = self._make(user_id=100)
        self.assertFalse(self.registry.mark_delivered(pending.token))  # still just "pending"
        self.registry.claim(pending.token, user_id=100)
        self.assertTrue(self.registry.mark_delivered(pending.token))

    def test_mark_delivered_is_not_reclaimable(self):
        pending = self._make(user_id=100)
        self.registry.claim(pending.token, user_id=100)
        self.registry.mark_delivered(pending.token)
        self.assertIsNone(self.registry.claim(pending.token, user_id=100))

    def test_mark_failed_after_claim(self):
        pending = self._make(user_id=100)
        self.registry.claim(pending.token, user_id=100)
        self.assertTrue(self.registry.mark_failed(pending.token))
        # A failed delivery still permanently consumes the token -- no replay.
        self.assertIsNone(self.registry.claim(pending.token, user_id=100))

    def test_cannot_mark_delivered_twice(self):
        pending = self._make(user_id=100)
        self.registry.claim(pending.token, user_id=100)
        self.assertTrue(self.registry.mark_delivered(pending.token))
        self.assertFalse(self.registry.mark_delivered(pending.token))


if __name__ == "__main__":
    unittest.main()
