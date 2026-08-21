import json
import unittest
from pathlib import Path

from agent_box.telegram.providers.base import EventKind
from agent_box.telegram.providers.codex import CodexProvider

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


class CodexEventTranslationTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.provider = CodexProvider(Path("/tmp"), on_event=self.events.append)
        self.provider._thread_id = "thread_1"
        self.provider._current_turn_id = "turn_1"

    def test_agent_message_delta_becomes_text_event(self):
        obj = load("codex_agent_message_delta.json")
        self.provider._handle_notification(obj)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0].kind, EventKind.TEXT)
        self.assertEqual(self.events[0].text, "Hello from Codex")

    def test_turn_completed_becomes_turn_done(self):
        obj = load("codex_turn_completed.json")
        self.provider._handle_notification(obj)
        self.assertEqual(self.events[0].kind, EventKind.TURN_DONE)
        self.assertIsNone(self.provider._current_turn_id)

    def test_request_user_input_becomes_question_event(self):
        obj = load("codex_request_user_input.json")
        self.provider._handle_server_request(obj)
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event.kind, EventKind.QUESTION)
        self.assertEqual(event.question.id, "q_1")
        self.assertEqual(event.question.header, "Config")
        self.assertEqual([o.label for o in event.question.options], ["Keep it", "Replace it"])
        self.assertIn("q_1", self.provider._question_items)

    def test_answer_question_sends_structured_rpc_response(self):
        obj = load("codex_request_user_input.json")
        self.provider._handle_server_request(obj)
        sent = []
        self.provider._write = sent.append
        ok = self.provider.answer_question("q_1", ["Keep it"])
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["id"], 7)
        self.assertEqual(sent[0]["result"]["answers"]["q_1"]["answers"], ["Keep it"])
        self.assertNotIn("q_1", self.provider._question_items)

    def test_answer_unknown_question_returns_false(self):
        self.assertFalse(self.provider.answer_question("nonexistent", ["x"]))

    def test_unhandled_server_request_declines(self):
        sent = []
        self.provider._write = sent.append
        self.provider._handle_server_request({"id": 3, "method": "item/permissions/requestApproval", "params": {}})
        self.assertIn("error", sent[0])
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
