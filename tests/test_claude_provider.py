import json
import unittest
from pathlib import Path

from agent_box.telegram.providers.base import EventKind
from agent_box.telegram.providers.claude import ClaudeProvider

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


class ClaudeEventTranslationTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.provider = ClaudeProvider(Path("/tmp"), on_event=self.events.append)

    def test_assistant_text_becomes_text_event(self):
        self.provider._handle_event(load("claude_assistant_text.json"))
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0].kind, EventKind.TEXT)
        self.assertEqual(self.events[0].text, "Working on it now.")

    def test_result_becomes_turn_done(self):
        self.provider._handle_event(load("claude_result.json"))
        self.assertEqual(self.events[0].kind, EventKind.TURN_DONE)

    def test_ask_user_question_becomes_question_event(self):
        self.provider._handle_event(load("claude_ask_user_question.json"))
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event.kind, EventKind.QUESTION)
        self.assertEqual(event.question.header, "Config")
        self.assertEqual([o.label for o in event.question.options], ["Keep it", "Replace it"])
        self.assertEqual(event.question.id, "toolu_01:0")

    def test_answer_question_sends_tool_result(self):
        self.provider._handle_event(load("claude_ask_user_question.json"))
        sent = []
        self.provider._write = sent.append
        ok = self.provider.answer_question("toolu_01:0", ["Keep it"])
        self.assertTrue(ok)
        block = sent[0]["message"]["content"][0]
        self.assertEqual(block["type"], "tool_result")
        self.assertEqual(block["tool_use_id"], "toolu_01")
        self.assertEqual(block["content"], "Keep it")

    def test_answer_unknown_question_returns_false(self):
        self.assertFalse(self.provider.answer_question("nope", ["x"]))

    def test_answering_twice_fails_second_time(self):
        self.provider._handle_event(load("claude_ask_user_question.json"))
        self.provider._write = lambda obj: None
        self.assertTrue(self.provider.answer_question("toolu_01:0", ["Keep it"]))
        self.assertFalse(self.provider.answer_question("toolu_01:0", ["Keep it"]))

    def test_interrupt_reports_unsupported(self):
        self.assertFalse(self.provider.interrupt())

    def test_send_message_writes_user_turn(self):
        sent = []
        self.provider._write = sent.append
        self.provider.send_message("hello")
        self.assertEqual(sent[0]["message"]["content"][0]["text"], "hello")


if __name__ == "__main__":
    unittest.main()
