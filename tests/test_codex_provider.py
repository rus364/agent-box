import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_box.telegram.providers.base import AnswerStatus, EventKind, RuntimeContext
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
        result = self.provider.answer_question("q_1", ["Keep it"])
        self.assertIs(result.status, AnswerStatus.DELIVERED)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["id"], 7)
        self.assertEqual(sent[0]["result"]["answers"]["q_1"]["answers"], ["Keep it"])
        self.assertNotIn("q_1", self.provider._qid_to_rpc)

    def test_answer_unknown_question_is_rejected(self):
        self.assertIs(self.provider.answer_question("nonexistent", ["x"]).status, AnswerStatus.REJECTED)

    def test_unhandled_server_request_declines(self):
        sent = []
        self.provider._write = sent.append
        self.provider._handle_server_request({"id": 3, "method": "item/permissions/requestApproval", "params": {}})
        self.assertIn("error", sent[0])
        self.assertEqual(self.events, [])


class _NoThread:
    """A Thread stand-in that never actually starts the reader loop."""

    def __init__(self, *_a, **_kw):
        pass

    def start(self):
        pass


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

        first = self.provider.answer_question("q_a", ["Yes"])
        self.assertIs(first.status, AnswerStatus.RECORDED)
        self.assertFalse(first.delivered, "an individual answer is not delivered on its own")
        self.assertEqual(first.outstanding, ["q_b"])
        self.assertEqual(sent, [], "must not respond to the RPC until all questions are answered")

        second = self.provider.answer_question("q_b", ["No"])
        self.assertIs(second.status, AnswerStatus.DELIVERED)
        self.assertEqual(sorted(second.resolved), ["q_a", "q_b"])
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
        duplicate = self.provider.answer_question("q_a", ["Changed my mind"])
        self.assertIs(duplicate.status, AnswerStatus.REJECTED)
        self.provider.answer_question("q_b", ["No"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["result"]["answers"]["q_a"]["answers"], ["Yes"])

    def test_answering_after_completion_is_rejected(self):
        obj = load("codex_multi_question.json")
        self.provider._handle_server_request(obj)
        self.provider._write = lambda obj: None
        self.provider.answer_question("q_a", ["Yes"])
        self.provider.answer_question("q_b", ["No"])
        self.assertIs(self.provider.answer_question("q_a", ["Again"]).status, AnswerStatus.REJECTED)


class CodexWriteFailureTests(unittest.TestCase):
    """A provider/transport failure while submitting the aggregate response
    must not leave the question half-answered: it is rolled back and reported
    as failed, so the caller can release its own claim."""

    def _provider_with_two_questions(self):
        provider = make_provider([])
        provider._handle_server_request(load("codex_multi_question.json"))
        return provider

    def test_write_exception_reports_failure_and_rolls_back(self):
        provider = self._provider_with_two_questions()
        provider._write = lambda obj: None
        provider.answer_question("q_a", ["Yes"])

        def boom(_obj):
            raise OSError("provider write failed")

        provider._write = boom
        failed = provider.answer_question("q_b", ["No"])
        self.assertIs(failed.status, AnswerStatus.FAILED)
        self.assertFalse(failed.accepted)
        self.assertIn("provider write failed", failed.detail)

        retry = []
        provider._write = retry.append
        again = provider.answer_question("q_b", ["No"])
        self.assertIs(again.status, AnswerStatus.DELIVERED)
        self.assertEqual(len(retry), 1)

    def test_provider_error_on_dead_stdin_is_reported_not_raised(self):
        provider = self._provider_with_two_questions()
        provider._write = lambda obj: None
        provider.answer_question("q_a", ["Yes"])
        provider._proc = None  # the real _write path: no usable stdin
        del provider._write
        failed = provider.answer_question("q_b", ["No"])
        self.assertIs(failed.status, AnswerStatus.FAILED)


class CodexServerRequestResolvedTests(unittest.TestCase):
    def test_resolved_notification_closes_the_pending_questions(self):
        events = []
        provider = make_provider(events)
        provider._handle_server_request(load("codex_request_user_input.json"))
        events.clear()
        provider._handle_notification(load("codex_server_request_resolved.json"))
        closed = [e for e in events if e.kind == EventKind.QUESTION_CLOSED]
        self.assertEqual([e.text for e in closed], ["q_1"])
        self.assertIs(provider.answer_question("q_1", ["Keep it"]).status, AnswerStatus.REJECTED)
        self.assertEqual(provider._pending_requests, {})
        self.assertEqual(provider._qid_to_rpc, {})

    def test_resolved_for_an_unknown_request_is_a_no_op(self):
        events = []
        provider = make_provider(events)
        provider._handle_notification({"method": "serverRequest/resolved",
                                       "params": {"requestId": 999, "threadId": "t"}})
        self.assertEqual(events, [])


class CodexHandshakeOrderTests(unittest.TestCase):
    def test_thread_start_only_sent_after_initialize_and_initialized(self):
        provider = make_provider([])
        sent = []

        def fake_call(method, params, timeout=60):
            sent.append({"method": method, "params": params})
            if method == "thread/start":
                return {"thread": {"id": "thread-xyz"}}
            return {}

        provider._write = sent.append
        provider._call = fake_call
        provider._spawn = lambda command: None
        provider._reader_thread = None
        with mock.patch("threading.Thread", _NoThread):
            provider.start()

        methods = [e.get("method") for e in sent]
        self.assertEqual(methods[:3], ["initialize", "initialized", "thread/start"])
        initialize = sent[0]
        self.assertIs(initialize["params"]["capabilities"]["experimentalApi"], True)


class CodexSubprocessIntegrationTests(unittest.TestCase):
    """Exercises the real JSON-RPC/stdio wiring against a fake
    `codex app-server` that enforces the handshake order and the
    experimentalApi capability."""

    def _spawn_fake(self, provider, command):
        argv = [sys.executable, str(FAKE_CODEX)]
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        provider._proc = proc
        return proc

    def _wait(self, predicate, timeout=5, label="condition"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {label}")

    def _wait_for(self, events, kind, timeout=5):
        self._wait(lambda: any(e.kind == kind for e in events), timeout, str(kind))

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
            result = provider.answer_question(question.id, ["Yes"])
            self.assertIs(result.status, AnswerStatus.DELIVERED)
            self._wait_for(events, EventKind.TURN_DONE)
            texts = [e.text for e in events if e.kind == EventKind.TEXT]
            self.assertTrue(any('"q1": {"answers": ["Yes"]}' in t for t in texts), texts)
        finally:
            provider.stop()

    def test_multi_question_delivers_exactly_one_response_over_real_pipes(self):
        events = []
        provider = make_provider(events)
        with mock.patch.object(provider, "_spawn", side_effect=lambda cmd: self._spawn_fake(provider, cmd)):
            provider.start()
        try:
            self._wait_for(events, EventKind.STARTED)
            provider.send_message("ask2")
            self._wait(lambda: len([e for e in events if e.kind == EventKind.QUESTION]) == 2,
                       label="two questions")
            questions = [e.question for e in events if e.kind == EventKind.QUESTION]
            first = provider.answer_question(questions[0].id, ["Yes"])
            self.assertIs(first.status, AnswerStatus.RECORDED)
            second = provider.answer_question(questions[1].id, ["main"])
            self.assertIs(second.status, AnswerStatus.DELIVERED)
            self._wait_for(events, EventKind.TURN_DONE)
            echoed = [e.text for e in events if e.kind == EventKind.TEXT and e.text.startswith("answers:")]
            self.assertEqual(len(echoed), 1, "exactly one aggregate response reached the server")
            payload = json.loads(echoed[0][len("answers:"):])
            self.assertEqual(payload["answers"]["q1"]["answers"], ["Yes"])
            self.assertEqual(payload["answers"]["q2"]["answers"], ["main"])
        finally:
            provider.stop()

    def test_server_request_resolved_over_real_pipes_withdraws_the_question(self):
        events = []
        provider = make_provider(events)
        with mock.patch.object(provider, "_spawn", side_effect=lambda cmd: self._spawn_fake(provider, cmd)):
            provider.start()
        try:
            self._wait_for(events, EventKind.STARTED)
            provider.send_message("cancel")
            self._wait_for(events, EventKind.QUESTION_CLOSED)
            question = next(e.question for e in events if e.kind == EventKind.QUESTION)
            self.assertIs(provider.answer_question(question.id, ["Yes"]).status, AnswerStatus.REJECTED)
        finally:
            provider.stop()

    def test_without_experimental_api_no_question_is_ever_asked(self):
        """Proves the capability actually matters rather than assuming it.

        With ``experimentalApi`` off, the fake server behaves like a server
        that never sends the EXPERIMENTAL request: the turn just completes
        and no question is surfaced.
        """
        events = []
        provider = make_provider(events)
        with mock.patch.object(CodexProvider, "CAPABILITIES", {"experimentalApi": False}), \
             mock.patch.object(provider, "_spawn", side_effect=lambda cmd: self._spawn_fake(provider, cmd)):
            provider.start()
            try:
                self._wait_for(events, EventKind.STARTED)
                provider.send_message("ask")
                self._wait_for(events, EventKind.TURN_DONE)
                self.assertEqual([e for e in events if e.kind == EventKind.QUESTION], [])
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
