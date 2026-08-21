import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_box.telegram.providers.base import EventKind, RuntimeContext
from agent_box.telegram.providers.claude import ClaudeProvider
from agent_box.workspace import Workspace

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CLAUDE = Path(__file__).parent / "fakes" / "fake_claude.py"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def make_provider(events):
    tmp = Path(tempfile.mkdtemp())
    ctx = RuntimeContext(workspace=Workspace(tmp, tmp), script_dir=tmp)
    return ClaudeProvider(ctx, on_event=events.append)


class ClaudeEventTranslationTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.provider = make_provider(self.events)

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


class ClaudeCommandConstructionTests(unittest.TestCase):
    def test_start_uses_session_id_not_resume(self):
        events = []
        provider = make_provider(events)
        command = provider._command(resume=None)
        self.assertIn("--session-id", command)
        self.assertNotIn("--resume", command)
        self.assertIn("--input-format", command)
        self.assertIn("stream-json", command)

    def test_resume_uses_resume_flag_not_session_id(self):
        events = []
        provider = make_provider(events)
        command = provider._command(resume="abc-123")
        self.assertIn("--resume", command)
        self.assertEqual(command[command.index("--resume") + 1], "abc-123")
        self.assertNotIn("--session-id", command)


class ClaudeSubprocessIntegrationTests(unittest.TestCase):
    """Exercises the real subprocess/stdio wiring against a fake `claude`
    executable, instead of only proving our own JSON parsing against
    self-authored fixtures."""

    def _spawn_fake(self, provider, command):
        import subprocess
        argv = [sys.executable, str(FAKE_CLAUDE), *command[1:]]
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        provider._proc = proc
        return proc

    def test_send_message_and_receive_echo(self):
        events = []
        provider = make_provider(events)
        with mock.patch.object(provider, "_spawn", side_effect=lambda cmd: self._spawn_fake(provider, cmd)):
            provider.start()
        try:
            provider.send_message("hello world")
            deadline = time.time() + 5
            while time.time() < deadline and not any(e.kind == EventKind.TURN_DONE for e in events):
                time.sleep(0.05)
            texts = [e.text for e in events if e.kind == EventKind.TEXT]
            self.assertIn("echo: hello world", texts)
        finally:
            provider.stop()

    def test_ask_user_question_round_trip_over_real_pipes(self):
        events = []
        provider = make_provider(events)
        with mock.patch.object(provider, "_spawn", side_effect=lambda cmd: self._spawn_fake(provider, cmd)):
            provider.start()
        try:
            provider.send_message("ask")
            deadline = time.time() + 5
            while time.time() < deadline and not any(e.kind == EventKind.QUESTION for e in events):
                time.sleep(0.05)
            questions = [e for e in events if e.kind == EventKind.QUESTION]
            self.assertEqual(len(questions), 1)
            qid = questions[0].question.id
            ok = provider.answer_question(qid, ["Yes"])
            self.assertTrue(ok)
            deadline = time.time() + 5
            while time.time() < deadline and not any(e.kind == EventKind.TURN_DONE for e in events):
                time.sleep(0.05)
            texts = [e.text for e in events if e.kind == EventKind.TEXT]
            self.assertTrue(any("received answer: Yes" in t for t in texts))
        finally:
            provider.stop()


if __name__ == "__main__":
    unittest.main()
