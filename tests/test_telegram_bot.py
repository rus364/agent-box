import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box.config import RemoteControlConfig, TelegramConfig
from agent_box.telegram.bot import TelegramBot
from agent_box.telegram.pending import PendingRegistry
from agent_box.telegram.sessions import SessionRegistry


class FakeClient:
    def __init__(self, *_a, **_kw):
        self.sent = []
        self.answered = []
        self.edited = []

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return {"message_id": len(self.sent)}

    def answer_callback_query(self, *_a, **_kw):
        self.answered.append(_a)

    def edit_message_reply_markup(self, *a, **kw):
        self.edited.append((a, kw))


def make_bot(allowed_ids=(111,), roots=None, tmpdir=None):
    config = TelegramConfig(token="tok", notify_chat_id="1", remote_control=RemoteControlConfig(
        enabled=True, allowed_user_ids=list(allowed_ids), workspace_roots=roots or [],
    ))
    tmp = Path(tmpdir)
    sessions = SessionRegistry(tmp / "sessions.json")
    pending = PendingRegistry(tmp / "pending.json")
    with mock.patch("agent_box.telegram.bot.TelegramClient", FakeClient):
        bot = TelegramBot(config, sessions=sessions, pending=pending)
    return bot


class AuthorizationTests(unittest.TestCase):
    def test_unauthorized_message_gets_no_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 999}, "text": "/start"})
            self.assertEqual(bot.client.sent, [])

    def test_authorized_start_sends_menu(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111}, "text": "/start"})
            self.assertEqual(len(bot.client.sent), 1)
            self.assertIn("Remote control menu", bot.client.sent[0][1])

    def test_unauthorized_callback_still_acknowledged_but_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            bot._handle_callback({"id": "cb1", "from": {"id": 999}, "message": {"chat": {"id": 1}}, "data": "menu:sessions"})
            self.assertEqual(bot.client.sent, [])
            self.assertEqual(len(bot.client.answered), 1)

    def test_disabled_remote_control_refuses_to_construct(self):
        config = TelegramConfig(token="tok", remote_control=RemoteControlConfig(enabled=False))
        with self.assertRaises(RuntimeError):
            TelegramBot(config)


class CallbackValidationTests(unittest.TestCase):
    def test_session_menu_requires_matching_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            session = bot.sessions.create("claude", "/tmp", chat_id=999, owner_user_id=999)
            # A different chat cannot open someone else's session.
            bot._handle_callback({"id": "cb1", "from": {"id": 111}, "message": {"chat": {"id": 1}}, "data": f"session:open:{session.id}"})
            self.assertEqual(bot.client.sent, [])

    def test_answer_callback_with_unknown_token_reports_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            bot._handle_callback({"id": "cb1", "from": {"id": 111}, "message": {"chat": {"id": 1}}, "data": "ans:doesnotexist:0"})
            self.assertEqual(len(bot.client.sent), 1)
            self.assertIn("no longer valid", bot.client.sent[0][1])

    def test_new_session_wizard_restricted_to_allowed_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "code"
            (root / "proj").mkdir(parents=True)
            bot = make_bot(tmpdir=tmp, roots=[str(root)])
            bot._wizard_pick_provider(1, 111, "claude")
            projects = bot._wizard_projects[(1, 111)]
            self.assertTrue(all(str(p.path).startswith(str(root.resolve())) for p in projects))

    def test_wizard_without_roots_configured_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp, roots=[])
            bot._wizard_pick_provider(1, 111, "claude")
            self.assertIn("No allowed workspace roots", bot.client.sent[-1][1])

    def test_question_answer_is_tied_to_session_owner_not_chat_id(self):
        from agent_box.telegram.providers.base import Question, QuestionOption

        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(allowed_ids=(111,), tmpdir=tmp)
            session = bot.sessions.create("claude", "/tmp", chat_id=1, owner_user_id=111)
            question = Question(id="q1", header="h", text="t", options=[QuestionOption("A")])
            bot._send_question(session, question)
            token = list(bot.pending._load())[0]["token"]
            # A different allowed user in the same chat cannot answer someone
            # else's pending question.
            resolved = bot.pending.resolve(token, user_id=222)
            self.assertIsNone(resolved)


if __name__ == "__main__":
    unittest.main()
