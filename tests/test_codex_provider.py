import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_box.telegram.providers.base import EventKind, RuntimeContext
from agent_box.telegram.providers.codex import CodexProvider
from agent_box.workspace import Workspace

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CODEX = Path(__file__).parent / "fakes" / "fake_codex_app_server.py"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def make_provider(events):
    tmp = Path(tempfile.mkdtemp())
    ctx = RuntimeContext(workspace=Workspace(tmp, tmp), script_dir=tmp)
    return CodexProvider(ctx, on_event=events.append)


class CodexEventTranslationTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.provider = make_provider(self.events)
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
        self.assertIn("q_1", self.provider._qid_to_rpc)

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
        self.assertNotIn("q_1", self.provider._qid_to_rpc)

    def test_answer_unknown_question_returns_false(self):
        self.assertFalse(self.provider.answer_question("nonexistent", ["x"]))

    def test_unhandled_server_request_declines(self):
        sent = []
        self.provider._write = sent.append
        self.provider._handle_server_request({"id": 3, "method": "item/permissions/requestApproval", "params": {}})
        self.assertIn("error", sent[0])
        self.assertEqual(self.events, [])


class CodexMultiQuestionAggregationTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.provider = make_provider(self.events)

    def test_two_questions_require_both_answers_before_one_response(self):
        obj = load("codex_multi_question.json")
        self.provider._handle_server_request(obj)
        self.assertEqual(len(self.events), 2)
        sent = []
        self.provider._write = sent.append

        ok1 = self.provider.answer_question("q_a", ["Yes"])
        self.assertTrue(ok1)
        self.assertEqual(sent, [], "must not respond to the RPC until all questions are answered")

        ok2 = self.provider.answer_question("q_b", ["No"])
        self.assertTrue(ok2)
        self.assertEqual(len(sent), 1, "exactly one JSON-RPC response for the whole request")
        self.assertEqual(sent[0]["id"], 42)
        self.assertEqual(sent[0]["result"]["answers"]["q_a"]["answers"], ["Yes"])
        self.assertEqual(sent[0]["result"]["answers"]["q_b"]["answers"], ["No"])

    def test_duplicate_answer_to_same_question_does_not_resend(self):
        obj = load("codex_multi_question.json")
        self.provider._handle_server_request(obj)
        sent = []
        self.provider._write = sent.append
        self.provider.answer_question("q_a", ["Yes"])
        ok_dup = self.provider.answer_question("q_a", ["Changed my mind"])
        self.assertFalse(ok_dup)
        self.provider.answer_question("q_b", ["No"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["result"]["answers"]["q_a"]["answers"], ["Yes"])

    def test_answering_after_completion_fails(self):
        obj = load("codex_multi_question.json")
        self.provider._handle_server_request(obj)
        self.provider._write = lambda obj: None
        self.provider.answer_question("q_a", ["Yes"])
        self.provider.answer_question("q_b", ["No"])
        self.assertFalse(self.provider.answer_question("q_a", ["Again"]))


class CodexHandshakeOrderTests(unittest.TestCase):
    def test_thread_start_only_sent_after_initialize_and_initialized(self):
        events = []
        provider = make_provider(events)
        sent = []

        def fake_write(obj):
            sent.append(obj)

        def fake_call(method, params, timeout=60):
            sent.append({"method": method, "params": params})
            if method == "initialize":
                return {}
            if method == "thread/start":
                return {"thread": {"id": "thread-xyz"}}
            return {}

        provider._write = fake_write
        provider._call = fake_call
        provider._reader_thread = None
        provider._spawn = lambda command: None
        # Bypass real reader thread startup (no live subprocess in this test).
        def handshake_without_reader():
            provider._call("initialize", {"clientInfo": {"name": "agent-box", "version": "1"}})
            provider._notify("initialized")

        provider._handshake = handshake_without_reader
        provider.start()

        methods = [e.get("method") for e in sent]
        self.assertEqual(methods[0], "initialize")
        self.assertEqual(methods[1], "initialized")
        self.assertEqual(methods[2], "thread/start")
        self.assertLess(methods.index("initialize"), methods.index("thread/start"))
        self.assertLess(methods.index("initialized"), methods.index("thread/start"))


class CodexSubprocessIntegrationTests(unittest.TestCase):
    """Exercises the real JSON-RPC/stdio wiring against a fake
    `codex app-server`, instead of only proving our own JSON parsing against
    self-authored fixtures."""

    def _spawn_fake(self, provider, command):
        argv = [sys.executable, str(FAKE_CODEX)]
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        provider._proc = proc
        return proc

    def _wait_for(self, events, kind, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(e.kind == kind for e in events):
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {kind}")

    def test_start_handshake_and_turn_over_real_pipes(self):
        events = []
        provider = make_provider(events)
        with mock.patch.object(provider, "_spawn", side_effect=lambda cmd: self._spawn_fake(provider, cmd)):
            provider.start()
        try:
            self._wait_for(events, EventKind.STARTED)
            self.assertEqual(provider.provider_session_id, "thread-fake")
            provider.send_message("hello")
            self._wait_for(events, EventKind.TURN_DONE)
            texts = [e.text for e in events if e.kind == EventKind.TEXT]
            self.assertIn("echo: hello", texts)
        finally:
            provider.stop()

    def test_request_user_input_round_trip_over_real_pipes(self):
        events = []
        provider = make_provider(events)
        with mock.patch.object(provider, "_spawn", side_effect=lambda cmd: self._spawn_fake(provider, cmd)):
            provider.start()
        try:
            self._wait_for(events, EventKind.STARTED)
            provider.send_message("ask")
            self._wait_for(events, EventKind.QUESTION)
            question = next(e.question for e in events if e.kind == EventKind.QUESTION)
            ok = provider.answer_question(question.id, ["Yes"])
            self.assertTrue(ok)
        finally:
            provider.stop()

    def test_resume_uses_thread_resume_over_real_pipes(self):
        events = []
        provider = make_provider(events)
        with mock.patch.object(provider, "_spawn", side_effect=lambda cmd: self._spawn_fake(provider, cmd)):
            provider.resume("thread-existing")
        try:
            self._wait_for(events, EventKind.STARTED)
            self.assertEqual(provider.provider_session_id, "thread-existing")
        finally:
            provider.stop()


if __name__ == "__main__":
    unittest.main()
