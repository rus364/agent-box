import tempfile
import unittest
from pathlib import Path

from agent_box.telegram.pending import (
    STATUS_DELIVERED, STATUS_FAILED, STATUS_RECORDED, Option, PendingRegistry,
)


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

    def _status(self, token):
        return next(p["status"] for p in self.registry._load() if p["token"] == token)


class RecordedAnswerTests(PendingRegistryTests):
    """An answer to one question of a multi-question provider request is
    *recorded*, not delivered: the agent has not been told anything until the
    aggregate response is submitted. The token is still consumed (no replay),
    but the distinction has to survive in the persisted state."""

    def test_mark_recorded_requires_prior_claim(self):
        pending = self._make(user_id=100)
        self.assertFalse(self.registry.mark_recorded(pending.token))
        self.registry.claim(pending.token, user_id=100)
        self.assertTrue(self.registry.mark_recorded(pending.token))
        self.assertEqual(self._status(pending.token), STATUS_RECORDED)

    def test_recorded_token_cannot_be_answered_again(self):
        pending = self._make(user_id=100)
        self.registry.claim(pending.token, user_id=100)
        self.registry.mark_recorded(pending.token)
        self.assertIsNone(self.registry.claim(pending.token, user_id=100))

    def test_promote_recorded_flips_only_matching_questions(self):
        first = self.registry.create("sess-1", 5, 100, "q_a", [Option("Yes", "yes")])
        second = self.registry.create("sess-1", 5, 100, "q_b", [Option("No", "no")])
        other = self.registry.create("sess-1", 5, 100, "q_z", [Option("Maybe", "maybe")])
        for token in (first.token, second.token, other.token):
            self.registry.claim(token, user_id=100)
            self.registry.mark_recorded(token)

        promoted = self.registry.promote_recorded("sess-1", ["q_a", "q_b"])
        self.assertEqual({p.question_id for p in promoted}, {"q_a", "q_b"})
        self.assertEqual(self._status(first.token), STATUS_DELIVERED)
        self.assertEqual(self._status(second.token), STATUS_DELIVERED)
        self.assertEqual(self._status(other.token), STATUS_RECORDED)

    def test_promote_recorded_ignores_other_sessions(self):
        mine = self.registry.create("sess-1", 5, 100, "q_a", [Option("Yes", "yes")])
        theirs = self.registry.create("sess-2", 5, 100, "q_a", [Option("Yes", "yes")])
        for token in (mine.token, theirs.token):
            self.registry.claim(token, user_id=100)
            self.registry.mark_recorded(token)
        self.registry.promote_recorded("sess-1", ["q_a"])
        self.assertEqual(self._status(mine.token), STATUS_DELIVERED)
        self.assertEqual(self._status(theirs.token), STATUS_RECORDED)

    def test_promote_recorded_does_not_resurrect_failed_answers(self):
        pending = self._make(user_id=100)
        self.registry.claim(pending.token, user_id=100)
        self.registry.mark_failed(pending.token)
        self.assertEqual(self.registry.promote_recorded("sess-1", ["q1"]), [])
        self.assertEqual(self._status(pending.token), STATUS_FAILED)


class DiscardQuestionTests(PendingRegistryTests):
    """A withdrawn question must stop being answerable without pretending the
    user answered it."""

    def test_discard_question_retires_an_open_question(self):
        pending = self._make(user_id=100)
        retired = self.registry.discard_question("sess-1", "q1")
        self.assertIsNotNone(retired)
        self.assertEqual(retired.token, pending.token)
        self.assertEqual(self._status(pending.token), STATUS_FAILED)
        self.assertIsNone(self.registry.claim(pending.token, user_id=100))

    def test_discard_question_retires_a_claimed_question(self):
        pending = self._make(user_id=100)
        self.registry.claim(pending.token, user_id=100)
        self.assertIsNotNone(self.registry.discard_question("sess-1", "q1"))
        self.assertEqual(self._status(pending.token), STATUS_FAILED)

    def test_discard_question_leaves_delivered_answers_alone(self):
        pending = self._make(user_id=100)
        self.registry.claim(pending.token, user_id=100)
        self.registry.mark_delivered(pending.token)
        self.assertIsNone(self.registry.discard_question("sess-1", "q1"))
        self.assertEqual(self._status(pending.token), STATUS_DELIVERED)

    def test_discard_unknown_question_is_a_no_op(self):
        self.assertIsNone(self.registry.discard_question("sess-1", "nope"))


if __name__ == "__main__":
    unittest.main()
