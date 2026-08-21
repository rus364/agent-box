"""Structured questions must round-trip the provider's real semantics.

Two things were previously flattened away:

* **Free text.** Both providers can accept an answer that is not one of the
  offered options (Claude's always-available "Other", Codex's ``isOther``).
  Typing an answer used to be delivered as an ordinary *next turn* -- which
  leaves the provider's outstanding request unanswered, because both block on
  it. The answer has to go back through ``answer_question``.
* **Multi-select.** Claude's ``AskUserQuestion`` questions can set
  ``multiSelect``. One-button-one-answer silently discarded that, so a question
  asking for several choices could only ever be answered with one.

The multi-question aggregation guarantees (recorded vs. delivered, exactly one
provider response) must survive both additions unchanged.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box.telegram.providers.base import AnswerResult, AnswerStatus, RuntimeContext
from agent_box.telegram.providers.claude import ClaudeProvider
from agent_box.telegram.providers.codex import CodexProvider
from agent_box.workspace import Workspace
from tests.test_telegram_bot import FakeProvider, make_bot

FIXTURES = Path(__file__).parent / "fixtures"


def provider_for(cls, events):
    tmp = Path(tempfile.mkdtemp())
    return cls(RuntimeContext(workspace=Workspace(tmp, tmp), script_dir=tmp), on_event=events.append)


class ProviderSemanticsTests(unittest.TestCase):
    def test_claude_reports_multi_select_and_free_text(self):
        events = []
        provider = provider_for(ClaudeProvider, events)
        provider._handle_event({
            "type": "control_request", "request_id": "r1",
            "request": {"subtype": "can_use_tool", "tool_name": "AskUserQuestion",
                        "input": {"questions": [
                            {"question": "Which files?", "header": "Files", "multiSelect": True,
                             "options": [{"label": "a.py"}, {"label": "b.py"}]},
                            {"question": "Which branch?", "header": "Branch",
                             "options": [{"label": "main"}]},
                        ]}},
        })
        first, second = (e.question for e in events)
        self.assertTrue(first.multi_select)
        self.assertFalse(second.multi_select)
        # AskUserQuestion always offers "Other"; both must allow free text.
        self.assertTrue(first.allow_free_text)
        self.assertTrue(second.allow_free_text)

    def test_claude_multi_select_answer_is_comma_joined(self):
        events = []
        provider = provider_for(ClaudeProvider, events)
        provider._handle_event({
            "type": "control_request", "request_id": "r1",
            "request": {"subtype": "can_use_tool", "tool_name": "AskUserQuestion",
                        "input": {"questions": [
                            {"question": "Which files?", "header": "Files", "multiSelect": True,
                             "options": [{"label": "a.py"}, {"label": "b.py"}]},
                        ]}},
        })
        sent = []
        provider._write = sent.append
        result = provider.answer_question("r1:0", ["a.py", "b.py"])
        self.assertIs(result.status, AnswerStatus.DELIVERED)
        answers = sent[0]["response"]["response"]["updatedInput"]["answers"]
        self.assertEqual(answers, {"Which files?": "a.py, b.py"})

    def test_claude_free_text_answer_is_the_questions_value(self):
        events = []
        provider = provider_for(ClaudeProvider, events)
        provider._handle_event(json.loads((FIXTURES / "claude_can_use_tool.json").read_text()))
        sent = []
        provider._write = sent.append
        provider.answer_question("req_01:0", ["something else entirely"])
        answers = sent[0]["response"]["response"]["updatedInput"]["answers"]
        self.assertEqual(
            answers, {"How should I handle the existing config?": "something else entirely"})

    def test_codex_free_text_follows_is_other(self):
        events = []
        provider = provider_for(CodexProvider, events)
        provider._handle_server_request({
            "id": 1, "method": "item/tool/requestUserInput",
            "params": {"questions": [
                {"id": "open", "header": "H", "question": "Q?", "isOther": True, "options": []},
                {"id": "closed", "header": "H", "question": "Q?", "options": [{"label": "Yes"}]},
            ]},
        })
        by_id = {e.question.id: e.question for e in events}
        self.assertTrue(by_id["open"].allow_free_text)
        # isOther defaults to false in the generated schema; a closed question
        # must not be advertised as accepting arbitrary text.
        self.assertFalse(by_id["closed"].allow_free_text)
        self.assertFalse(by_id["open"].multi_select)

    def test_codex_answer_keeps_the_list_shape(self):
        events = []
        provider = provider_for(CodexProvider, events)
        provider._handle_server_request({
            "id": 5, "method": "item/tool/requestUserInput",
            "params": {"questions": [{"id": "q", "header": "H", "question": "Q?",
                                      "isOther": True, "options": []}]},
        })
        sent = []
        provider._write = sent.append
        provider.answer_question("q", ["typed by hand"])
        self.assertEqual(sent[0]["result"]["answers"]["q"]["answers"], ["typed by hand"])


def send_question(bot, session, question):
    bot._send_question(session, question)
    return next(p["token"] for p in bot.pending._load() if p["question_id"] == question.id)


def keyboard(bot):
    return bot.client.sent[-1][2]["reply_markup"]["inline_keyboard"]


def make_question(**kwargs):
    from agent_box.telegram.providers.base import Question, QuestionOption
    kwargs.setdefault("id", "q1")
    kwargs.setdefault("header", "H")
    kwargs.setdefault("text", "Pick")
    kwargs.setdefault("options", [QuestionOption("A"), QuestionOption("B")])
    kwargs.setdefault("allow_free_text", False)
    return Question(**kwargs)


class KeyboardTests(unittest.TestCase):
    def _bot_with(self, tmp, question, provider=None):
        bot = make_bot(tmpdir=tmp)
        session = bot.sessions.create("claude", "/p", chat_id=1, owner_user_id=111)
        bot._providers[session.id] = provider or FakeProvider()
        token = send_question(bot, session, question)
        return bot, session, bot._providers[session.id], token

    def test_single_select_has_one_button_per_option_and_no_footer(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, *_ = self._bot_with(tmp, make_question())
            rows = keyboard(bot)
            self.assertEqual([r[0]["text"] for r in rows], ["A", "B"])
            self.assertTrue(all(r[0]["callback_data"].startswith("ans:") for r in rows))

    def test_free_text_adds_an_other_button(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, *_ = self._bot_with(tmp, make_question(allow_free_text=True))
            rows = keyboard(bot)
            self.assertEqual(rows[-1][0]["text"], "Other...")
            self.assertTrue(rows[-1][0]["callback_data"].startswith("text:"))

    def test_multi_select_uses_toggles_and_a_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, *_ = self._bot_with(tmp, make_question(multi_select=True))
            rows = keyboard(bot)
            self.assertEqual([r[0]["text"] for r in rows[:2]], ["☐ A", "☐ B"])
            self.assertTrue(all(r[0]["callback_data"].startswith("sel:") for r in rows[:2]))
            self.assertEqual(rows[-1][0]["text"], "Submit")
            self.assertTrue(rows[-1][0]["callback_data"].startswith("done:"))

    def test_question_with_no_options_still_offers_free_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, *_ = self._bot_with(tmp, make_question(options=[], allow_free_text=False))
            rows = keyboard(bot)
            self.assertEqual(rows, [[{"text": "Other...",
                                      "callback_data": rows[0][0]["callback_data"]}]])
            self.assertTrue(rows[0][0]["callback_data"].startswith("text:"))

    def test_callback_data_stays_within_the_telegram_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, *_ = self._bot_with(tmp, make_question(multi_select=True, allow_free_text=True))
            for row in keyboard(bot):
                for button in row:
                    self.assertLessEqual(len(button["callback_data"].encode()), 64)


class MultiSelectFlowTests(KeyboardTests):
    def test_toggling_does_not_deliver_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _s, provider, token = self._bot_with(tmp, make_question(multi_select=True))
            bot._handle_callback({"id": "c", "from": {"id": 111}, "message": {"chat": {"id": 1}},
                                  "data": f"sel:{token}:0"})
            self.assertEqual(provider.answered, [])
            self.assertEqual(bot.pending.peek(token).selected, [0])
            self.assertEqual(len(bot.client.edited), 1, "buttons are redrawn with the checkmark")

    def test_toggle_is_reversible(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _s, _p, token = self._bot_with(tmp, make_question(multi_select=True))
            for _ in range(2):
                bot._handle_callback({"id": "c", "from": {"id": 111}, "message": {"chat": {"id": 1}},
                                      "data": f"sel:{token}:0"})
            self.assertEqual(bot.pending.peek(token).selected, [])

    def test_submit_delivers_every_selected_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _s, provider, token = self._bot_with(tmp, make_question(multi_select=True))
            for index in (0, 1):
                bot._handle_callback({"id": "c", "from": {"id": 111}, "message": {"chat": {"id": 1}},
                                      "data": f"sel:{token}:{index}"})
            bot._handle_callback({"id": "c", "from": {"id": 111}, "message": {"chat": {"id": 1}},
                                  "data": f"done:{token}"})
            self.assertEqual(provider.answered, [("q1", ["A", "B"])])

    def test_submitting_nothing_is_refused_without_burning_the_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _s, provider, token = self._bot_with(tmp, make_question(multi_select=True))
            bot._handle_callback({"id": "c", "from": {"id": 111}, "message": {"chat": {"id": 1}},
                                  "data": f"done:{token}"})
            self.assertEqual(provider.answered, [])
            self.assertIn("at least one option", bot.client.sent[-1][1])
            bot._handle_callback({"id": "c", "from": {"id": 111}, "message": {"chat": {"id": 1}},
                                  "data": f"sel:{token}:0"})
            bot._handle_callback({"id": "c", "from": {"id": 111}, "message": {"chat": {"id": 1}},
                                  "data": f"done:{token}"})
            self.assertEqual(provider.answered, [("q1", ["A"])])

    def test_another_user_cannot_toggle_your_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(allowed_ids=(111, 222), tmpdir=tmp)
            session = bot.sessions.create("claude", "/p", chat_id=1, owner_user_id=111)
            bot._providers[session.id] = FakeProvider()
            token = send_question(bot, session, make_question(multi_select=True))
            bot._handle_callback({"id": "c", "from": {"id": 222}, "message": {"chat": {"id": 1}},
                                  "data": f"sel:{token}:0"})
            self.assertEqual(bot.pending.peek(token).selected, [])
            # ...and the owner's question survives the attempt.
            bot._handle_callback({"id": "c", "from": {"id": 111}, "message": {"chat": {"id": 1}},
                                  "data": f"sel:{token}:0"})
            self.assertEqual(bot.pending.peek(token).selected, [0])

    def test_submitting_twice_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _s, provider, token = self._bot_with(tmp, make_question(multi_select=True))
            bot._handle_callback({"id": "c", "from": {"id": 111}, "message": {"chat": {"id": 1}},
                                  "data": f"sel:{token}:0"})
            for _ in range(2):
                bot._handle_callback({"id": "c", "from": {"id": 111}, "message": {"chat": {"id": 1}},
                                      "data": f"done:{token}"})
            self.assertEqual(len(provider.answered), 1)


class FreeTextFlowTests(KeyboardTests):
    def _arm(self, bot, token, user_id=111, thread_id=None):
        message = {"chat": {"id": 1}}
        if thread_id is not None:
            message["message_thread_id"] = thread_id
        bot._handle_callback({"id": "c", "from": {"id": user_id}, "message": message,
                              "data": f"text:{token}"})

    def test_next_message_answers_the_question_instead_of_starting_a_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _s, provider, token = self._bot_with(tmp, make_question(allow_free_text=True))
            self._arm(bot, token)
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111}, "text": "my own answer"})
            self.assertEqual(provider.answered, [("q1", ["my own answer"])])
            self.assertEqual(provider.sent_messages, [],
                             "a free-text answer must not also be sent as a new turn")

    def test_only_the_next_message_is_captured(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, session, provider, token = self._bot_with(tmp, make_question(allow_free_text=True))
            from agent_box.telegram.sessions import STATUS_RUNNING
            bot.sessions.update(session.id, status=STATUS_RUNNING)
            self._arm(bot, token)
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111}, "text": "answer"})
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111}, "text": "next turn"})
            self.assertEqual(provider.answered, [("q1", ["answer"])])
            self.assertEqual(provider.sent_messages, ["next turn"])

    def test_a_closed_question_disarms_the_prompt(self):
        from agent_box.telegram.providers.base import AgentEvent, EventKind

        with tempfile.TemporaryDirectory() as tmp:
            bot, session, provider, token = self._bot_with(tmp, make_question(allow_free_text=True))
            from agent_box.telegram.sessions import STATUS_RUNNING
            bot.sessions.update(session.id, status=STATUS_RUNNING)
            self._arm(bot, token)
            bot._on_provider_event(session.id, AgentEvent(kind=EventKind.QUESTION_CLOSED, text="q1"))
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111}, "text": "too late"})
            self.assertEqual(provider.answered, [])
            self.assertEqual(provider.sent_messages, ["too late"], "falls back to a normal turn")

    def test_a_question_that_forbids_free_text_refuses_to_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _s, _p, token = self._bot_with(tmp, make_question(allow_free_text=False))
            self._arm(bot, token)
            self.assertIn("only accepts one of the listed options", bot.client.sent[-1][1])
            self.assertEqual(bot._awaiting_text, {})

    def test_another_user_cannot_arm_your_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(allowed_ids=(111, 222), tmpdir=tmp)
            session = bot.sessions.create("claude", "/p", chat_id=1, owner_user_id=111)
            bot._providers[session.id] = FakeProvider()
            token = send_question(bot, session, make_question(allow_free_text=True))
            self._arm(bot, token, user_id=222)
            self.assertEqual(bot._awaiting_text, {})

    def test_free_text_is_armed_in_the_sessions_own_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            session = bot.sessions.create("claude", "/p", chat_id=1, owner_user_id=111,
                                          message_thread_id=7)
            provider = FakeProvider()
            bot._providers[session.id] = provider
            token = send_question(bot, session, make_question(allow_free_text=True))
            self._arm(bot, token, thread_id=7)
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111},
                                 "text": "in topic", "message_thread_id": 7})
            self.assertEqual(provider.answered, [("q1", ["in topic"])])


class AggregationStillHoldsTests(KeyboardTests):
    """Free text and multi-select must not weaken the recorded/delivered split."""

    def test_free_text_answer_can_be_only_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = FakeProvider(
                answer_result=AnswerResult(AnswerStatus.RECORDED, outstanding=["q2"]))
            bot, _s, _p, token = self._bot_with(
                tmp, make_question(allow_free_text=True), provider=provider)
            bot._handle_callback({"id": "c", "from": {"id": 111}, "message": {"chat": {"id": 1}},
                                  "data": f"text:{token}"})
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111}, "text": "typed"})
            self.assertIn("Recorded", bot.client.sent[-1][1])
            self.assertNotIn("delivered", bot.client.sent[-1][1].lower())

    def test_multi_select_submit_can_be_only_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = FakeProvider(
                answer_result=AnswerResult(AnswerStatus.RECORDED, outstanding=["q2"]))
            bot, _s, provider, token = self._bot_with(
                tmp, make_question(multi_select=True), provider=provider)
            bot._handle_callback({"id": "c", "from": {"id": 111}, "message": {"chat": {"id": 1}},
                                  "data": f"sel:{token}:1"})
            bot._handle_callback({"id": "c", "from": {"id": 111}, "message": {"chat": {"id": 1}},
                                  "data": f"done:{token}"})
            self.assertEqual(provider.answered, [("q1", ["B"])])
            self.assertIn("Recorded", bot.client.sent[-1][1])

    def test_free_text_delivery_failure_does_not_leave_the_token_claimed(self):
        from agent_box.telegram.pending import STATUS_CLAIMED

        with tempfile.TemporaryDirectory() as tmp:
            provider = FakeProvider(
                answer_result=AnswerResult(AnswerStatus.FAILED, detail="broken pipe"))
            bot, _s, _p, token = self._bot_with(
                tmp, make_question(allow_free_text=True), provider=provider)
            bot._handle_callback({"id": "c", "from": {"id": 111}, "message": {"chat": {"id": 1}},
                                  "data": f"text:{token}"})
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111}, "text": "typed"})
            status = next(p["status"] for p in bot.pending._load() if p["token"] == token)
            self.assertNotEqual(status, STATUS_CLAIMED)
            self.assertIn("could not be delivered", bot.client.sent[-1][1])


class ClaudeMultiSelectIntegrationTests(unittest.TestCase):
    """The whole path, over real pipes, against the fake `claude`."""

    def test_multi_select_round_trip(self):
        import subprocess
        import sys
        import time

        fake = Path(__file__).parent / "fakes" / "fake_claude.py"
        events = []
        provider = provider_for(ClaudeProvider, events)

        def spawn(command):
            proc = subprocess.Popen(
                [sys.executable, str(fake), *command[1:]],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
            provider._proc = proc
            return proc

        with mock.patch.object(provider, "_spawn", side_effect=spawn):
            provider.start()
        try:
            provider.send_message("multi")
            deadline = time.time() + 5
            from agent_box.telegram.providers.base import EventKind
            while time.time() < deadline and not any(e.kind == EventKind.QUESTION for e in events):
                time.sleep(0.05)
            question = next(e.question for e in events if e.kind == EventKind.QUESTION)
            self.assertTrue(question.multi_select)
            result = provider.answer_question(question.id, ["a.py", "b.py"])
            self.assertIs(result.status, AnswerStatus.DELIVERED)
            deadline = time.time() + 5
            while time.time() < deadline and not any(e.kind == EventKind.TURN_DONE for e in events):
                time.sleep(0.05)
            texts = [e.text for e in events if e.kind == EventKind.TEXT]
            self.assertTrue(any("a.py, b.py" in t for t in texts), texts)
        finally:
            provider.stop()


if __name__ == "__main__":
    unittest.main()
