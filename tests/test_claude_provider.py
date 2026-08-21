import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_box.telegram.providers.base import AnswerStatus, EventKind, RuntimeContext
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

    def test_ask_user_question_tool_use_block_is_not_answerable(self):
        """Regression: the tool_use block is NOT the answerable surface.

        AskUserQuestion is a built-in tool the CLI runs itself; a client
        cannot resolve it by replying to this block. An earlier revision
        registered a pending question here and answered it with a
        ``tool_result``, which a real Claude Code install ignores.
        """
        self.provider._handle_event(load("claude_ask_user_question.json"))
        self.assertEqual([e.kind for e in self.events], [])
        self.assertEqual(self.provider._pending_requests, {})

    def test_can_use_tool_control_request_becomes_question_event(self):
        self.provider._handle_event(load("claude_can_use_tool.json"))
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event.kind, EventKind.QUESTION)
        self.assertEqual(event.question.header, "Config")
        self.assertEqual([o.label for o in event.question.options], ["Keep it", "Replace it"])
        self.assertEqual(event.question.id, "req_01:0")

    def test_answer_sends_control_response_with_answers_in_updated_input(self):
        self.provider._handle_event(load("claude_can_use_tool.json"))
        sent = []
        self.provider._write = sent.append
        result = self.provider.answer_question("req_01:0", ["Keep it"])
        self.assertIs(result.status, AnswerStatus.DELIVERED)
        self.assertEqual(len(sent), 1)
        envelope = sent[0]
        self.assertEqual(envelope["type"], "control_response")
        self.assertEqual(envelope["response"]["subtype"], "success")
        self.assertEqual(envelope["response"]["request_id"], "req_01")
        payload = envelope["response"]["response"]
        self.assertEqual(payload["behavior"], "allow")
        self.assertEqual(
            payload["updatedInput"]["answers"],
            {"How should I handle the existing config?": "Keep it"},
        )
        # The original questions must survive: updatedInput has to satisfy
        # the tool's own input schema.
        self.assertEqual(len(payload["updatedInput"]["questions"]), 1)

    def test_answer_unknown_question_is_rejected(self):
        self.assertIs(self.provider.answer_question("nope", ["x"]).status, AnswerStatus.REJECTED)

    def test_answering_twice_is_rejected_the_second_time(self):
        self.provider._handle_event(load("claude_can_use_tool.json"))
        self.provider._write = lambda obj: None
        self.assertIs(self.provider.answer_question("req_01:0", ["Keep it"]).status, AnswerStatus.DELIVERED)
        self.assertIs(self.provider.answer_question("req_01:0", ["Keep it"]).status, AnswerStatus.REJECTED)

    def test_non_ask_permission_request_is_denied_not_auto_allowed(self):
        sent = []
        self.provider._write = sent.append
        self.provider._handle_event({
            "type": "control_request", "request_id": "r9",
            "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "rm -rf /"}},
        })
        self.assertEqual(sent[0]["response"]["response"]["behavior"], "deny")

    def test_unknown_control_request_subtype_gets_an_error_response(self):
        sent = []
        self.provider._write = sent.append
        self.provider._handle_event({
            "type": "control_request", "request_id": "r10", "request": {"subtype": "mcp_message"},
        })
        self.assertEqual(sent[0]["response"]["subtype"], "error")

    def test_interrupt_reports_unsupported(self):
        self.assertFalse(self.provider.interrupt())

    def test_send_message_writes_user_turn(self):
        sent = []
        self.provider._write = sent.append
        self.provider.send_message("hello")
        self.assertEqual(sent[0]["message"]["content"][0]["text"], "hello")


class ClaudeMultiQuestionAggregationTests(unittest.TestCase):
    """One can_use_tool request carrying several questions must be answered
    by exactly one control_response, and an individual answer must never be
    reported as delivered before that response is actually written."""

    def setUp(self):
        self.events = []
        self.provider = make_provider(self.events)
        self.provider._handle_event(load("claude_can_use_tool_multi.json"))
        self.sent = []
        self.provider._write = self.sent.append

    def test_first_answer_is_recorded_not_delivered(self):
        result = self.provider.answer_question("req_02:0", ["Yes"])
        self.assertIs(result.status, AnswerStatus.RECORDED)
        self.assertFalse(result.delivered)
        self.assertEqual(result.outstanding, ["req_02:1"])
        self.assertEqual(self.sent, [], "nothing may reach Claude until every question is answered")

    def test_last_answer_delivers_one_aggregate_response(self):
        self.provider.answer_question("req_02:0", ["Yes"])
        result = self.provider.answer_question("req_02:1", ["No"])
        self.assertIs(result.status, AnswerStatus.DELIVERED)
        self.assertEqual(sorted(result.resolved), ["req_02:0", "req_02:1"])
        self.assertEqual(len(self.sent), 1)
        answers = self.sent[0]["response"]["response"]["updatedInput"]["answers"]
        self.assertEqual(answers, {
            "Proceed with migration?": "Yes",
            "Also take a backup first?": "No",
        })

    def test_write_failure_rolls_back_so_the_question_stays_answerable(self):
        self.provider.answer_question("req_02:0", ["Yes"])

        def boom(_obj):
            raise OSError("broken pipe")

        self.provider._write = boom
        failed = self.provider.answer_question("req_02:1", ["No"])
        self.assertIs(failed.status, AnswerStatus.FAILED)
        self.assertIn("broken pipe", failed.detail)

        retry = []
        self.provider._write = retry.append
        again = self.provider.answer_question("req_02:1", ["No"])
        self.assertIs(again.status, AnswerStatus.DELIVERED)
        self.assertEqual(len(retry), 1)


class ClaudeQuestionWithdrawalTests(unittest.TestCase):
    def test_control_cancel_request_closes_the_question(self):
        events = []
        provider = make_provider(events)
        provider._handle_event(load("claude_can_use_tool.json"))
        events.clear()
        provider._handle_event({"type": "control_cancel_request", "request_id": "req_01"})
        closed = [e for e in events if e.kind == EventKind.QUESTION_CLOSED]
        self.assertEqual([e.text for e in closed], ["req_01:0"])
        self.assertIs(provider.answer_question("req_01:0", ["Keep it"]).status, AnswerStatus.REJECTED)


class ClaudeCommandConstructionTests(unittest.TestCase):
    def test_start_uses_session_id_not_resume(self):
        provider = make_provider([])
        command = provider._command(resume=None)
        self.assertIn("--session-id", command)
        self.assertNotIn("--resume", command)
        self.assertIn("--input-format", command)
        self.assertIn("stream-json", command)

    def test_resume_uses_resume_flag_not_session_id(self):
        provider = make_provider([])
        command = provider._command(resume="abc-123")
        self.assertIn("--resume", command)
        self.assertEqual(command[command.index("--resume") + 1], "abc-123")
        self.assertNotIn("--session-id", command)

    def test_permission_prompt_surface_is_requested(self):
        """Without --permission-prompt-tool stdio the CLI never routes
        AskUserQuestion to the client, so structured questions silently do
        nothing. This is the capability the feature depends on."""
        provider = make_provider([])
        command = provider._command(resume=None)
        self.assertIn("--permission-prompt-tool", command)
        self.assertEqual(command[command.index("--permission-prompt-tool") + 1], "stdio")


class ClaudeSubprocessIntegrationTests(unittest.TestCase):
    """Exercises the real subprocess/stdio wiring against a fake `claude`
    executable that implements the actual control protocol (and refuses to
    ask at all without the stdio permission surface)."""

    def _spawn_fake(self, provider, command):
        import subprocess
        argv = [sys.executable, str(FAKE_CLAUDE), *command[1:]]
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        provider._proc = proc
        return proc

    def _wait(self, events, predicate, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate(events):
                return
            time.sleep(0.05)
        self.fail("timed out waiting for the expected events")

    def test_send_message_and_receive_echo(self):
        events = []
        provider = make_provider(events)
        with mock.patch.object(provider, "_spawn", side_effect=lambda cmd: self._spawn_fake(provider, cmd)):
            provider.start()
        try:
            provider.send_message("hello world")
            self._wait(events, lambda e: any(x.kind == EventKind.TURN_DONE for x in e))
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
            self._wait(events, lambda e: any(x.kind == EventKind.QUESTION for x in e))
            question = next(e.question for e in events if e.kind == EventKind.QUESTION)
            result = provider.answer_question(question.id, ["Yes"])
            self.assertIs(result.status, AnswerStatus.DELIVERED)
            self._wait(events, lambda e: any(x.kind == EventKind.TURN_DONE for x in e))
            texts = [e.text for e in events if e.kind == EventKind.TEXT]
            self.assertTrue(any('"Continue?"="Yes"' in t for t in texts), texts)
        finally:
            provider.stop()

    def test_multi_question_round_trip_delivers_once_over_real_pipes(self):
        events = []
        provider = make_provider(events)
        with mock.patch.object(provider, "_spawn", side_effect=lambda cmd: self._spawn_fake(provider, cmd)):
            provider.start()
        try:
            provider.send_message("ask2")
            self._wait(events, lambda e: len([x for x in e if x.kind == EventKind.QUESTION]) == 2)
            questions = [e.question for e in events if e.kind == EventKind.QUESTION]
            first = provider.answer_question(questions[0].id, ["Yes"])
            self.assertIs(first.status, AnswerStatus.RECORDED)
            second = provider.answer_question(questions[1].id, ["main"])
            self.assertIs(second.status, AnswerStatus.DELIVERED)
            self._wait(events, lambda e: any(x.kind == EventKind.TURN_DONE for x in e))
            texts = [e.text for e in events if e.kind == EventKind.TEXT]
            self.assertTrue(any('"Which branch?"="main"' in t for t in texts), texts)
        finally:
            provider.stop()

    def test_withdrawn_question_over_real_pipes_becomes_unanswerable(self):
        events = []
        provider = make_provider(events)
        with mock.patch.object(provider, "_spawn", side_effect=lambda cmd: self._spawn_fake(provider, cmd)):
            provider.start()
        try:
            provider.send_message("cancel")
            self._wait(events, lambda e: any(x.kind == EventKind.QUESTION_CLOSED for x in e))
            question = next(e.question for e in events if e.kind == EventKind.QUESTION)
            self.assertIs(provider.answer_question(question.id, ["Yes"]).status, AnswerStatus.REJECTED)
        finally:
            provider.stop()


if __name__ == "__main__":
    unittest.main()
