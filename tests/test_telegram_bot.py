import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box.config import RemoteControlConfig, TelegramConfig
from agent_box.telegram.bot import TelegramBot
from agent_box.telegram.offset import OffsetStore
from agent_box.telegram.pending import PendingRegistry
from agent_box.telegram.sessions import STATUS_DISCONNECTED, STATUS_RUNNING, SessionRegistry


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


class FakeProvider:
    """A minimal Provider stand-in for bot-level routing/callback tests."""

    def __init__(self, alive=True, answer_ok=True):
        self.sent_messages = []
        self._alive = alive
        self._answer_ok = answer_ok
        self.provider_session_id = "fake-session"
        self.answered = []

    def start(self):
        pass

    def is_alive(self):
        return self._alive

    def send_message(self, text):
        self.sent_messages.append(text)

    def answer_question(self, question_id, answers):
        self.answered.append((question_id, answers))
        return self._answer_ok

    def interrupt(self):
        return False

    def stop(self):
        self._alive = False


def make_bot(allowed_ids=(111,), roots=None, tmpdir=None):
    config = TelegramConfig(token="tok", notify_chat_id="1", remote_control=RemoteControlConfig(
        enabled=True, allowed_user_ids=list(allowed_ids), workspace_roots=roots or [],
    ))
    tmp = Path(tmpdir)
    sessions = SessionRegistry(tmp / "sessions.json")
    pending = PendingRegistry(tmp / "pending.json")
    offsets = OffsetStore(tmp / "offset.json")
    with mock.patch("agent_box.telegram.bot.TelegramClient", FakeClient):
        bot = TelegramBot(config, tmp, sessions=sessions, pending=pending, offsets=offsets)
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
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                TelegramBot(config, Path(tmp))


class CallbackValidationTests(unittest.TestCase):
    def test_session_menu_requires_matching_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            session = bot.sessions.create("claude", "/tmp", chat_id=999, owner_user_id=999)
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
            resolved = bot.pending.claim(token, user_id=222)
            self.assertIsNone(resolved)


class CallbackDeliveryGatingTests(unittest.TestCase):
    """Success must only be reported once the provider actually confirms."""

    def _setup_pending_answer(self, tmp, answer_ok):
        bot = make_bot(tmpdir=tmp)
        session = bot.sessions.create("claude", "/tmp", chat_id=1, owner_user_id=111)
        provider = FakeProvider(alive=True, answer_ok=answer_ok)
        bot._providers[session.id] = provider
        from agent_box.telegram.providers.base import Question, QuestionOption
        question = Question(id="q1", header="h", text="t", options=[QuestionOption("Keep it")])
        bot._send_question(session, question)
        token = list(bot.pending._load())[0]["token"]
        return bot, session, provider, token

    def test_successful_delivery_marks_resolved_and_clears_buttons(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, session, provider, token = self._setup_pending_answer(tmp, answer_ok=True)
            bot._handle_answer_callback(1, 111, f"ans:{token}:0")
            self.assertEqual(provider.answered, [("q1", ["Keep it"])])
            self.assertEqual(len(bot.client.edited), 1)
            self.assertIsNone(bot.pending.claim(token, 111))  # cannot answer again

    def test_failed_delivery_does_not_claim_success_and_does_not_clear_buttons(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, session, provider, token = self._setup_pending_answer(tmp, answer_ok=False)
            bot._handle_answer_callback(1, 111, f"ans:{token}:0")
            self.assertEqual(len(bot.client.edited), 0, "buttons must not be cleared on delivery failure")
            self.assertIn("did not accept", bot.client.sent[-1][1])
            # The token is still consumed (no replay), but honestly as failed.
            self.assertIsNone(bot.pending.claim(token, 111))

    def test_dead_provider_reports_failure_not_silent_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, session, provider, token = self._setup_pending_answer(tmp, answer_ok=True)
            provider._alive = False
            bot._handle_answer_callback(1, 111, f"ans:{token}:0")
            self.assertEqual(provider.answered, [])
            self.assertIn("no longer active", bot.client.sent[-1][1])


class TopicCreationTests(unittest.TestCase):
    """A session created via /new inside a forum topic must actually record
    that topic's message_thread_id, not just route by it after the fact."""

    def test_new_via_command_inside_topic_records_thread_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "code"
            (root / "proj").mkdir(parents=True)
            bot = make_bot(tmpdir=tmp, roots=[str(root)])
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111}, "text": "/new", "message_thread_id": 42})
            bot._wizard_pick_provider(1, 111, "claude")
            projects = bot._wizard_projects[(1, 111)]
            idx = next(i for i, p in enumerate(projects) if p.path.name == "proj")
            with mock.patch("agent_box.telegram.bot.PROVIDERS", {"claude": lambda ctx, on_event=None: FakeProvider()}):
                bot._wizard_pick_project(1, 111, idx)
            sessions = bot.sessions.list_for_chat(1)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].message_thread_id, 42)

    def test_new_via_menu_button_inside_topic_records_thread_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "code"
            (root / "proj").mkdir(parents=True)
            bot = make_bot(tmpdir=tmp, roots=[str(root)])
            bot._handle_callback({
                "id": "cb", "from": {"id": 111},
                "message": {"chat": {"id": 1}, "message_thread_id": 7}, "data": "menu:new",
            })
            self.assertEqual(bot._wizards[(1, 111)].thread_id, 7)


class RoutingTests(unittest.TestCase):
    def test_free_text_with_single_live_session_routes_implicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            session = bot.sessions.create("claude", "/tmp", chat_id=1, owner_user_id=111)
            bot.sessions.update(session.id, status=STATUS_RUNNING)
            provider = FakeProvider()
            bot._providers[session.id] = provider
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111}, "text": "hello"})
            self.assertEqual(provider.sent_messages, ["hello"])

    def test_free_text_with_two_sessions_requires_explicit_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            s1 = bot.sessions.create("claude", "/a", chat_id=1, owner_user_id=111)
            s2 = bot.sessions.create("codex", "/b", chat_id=1, owner_user_id=111)
            bot.sessions.update(s1.id, status=STATUS_RUNNING)
            bot.sessions.update(s2.id, status=STATUS_RUNNING)
            p1, p2 = FakeProvider(), FakeProvider()
            bot._providers[s1.id] = p1
            bot._providers[s2.id] = p2
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111}, "text": "hello"})
            self.assertEqual(p1.sent_messages, [])
            self.assertEqual(p2.sent_messages, [])
            self.assertIn("No active session selected", bot.client.sent[-1][1])

    def test_opening_a_session_makes_it_the_active_routing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            s1 = bot.sessions.create("claude", "/a", chat_id=1, owner_user_id=111)
            s2 = bot.sessions.create("codex", "/b", chat_id=1, owner_user_id=111)
            bot.sessions.update(s1.id, status=STATUS_RUNNING)
            bot.sessions.update(s2.id, status=STATUS_RUNNING)
            p1, p2 = FakeProvider(), FakeProvider()
            bot._providers[s1.id] = p1
            bot._providers[s2.id] = p2

            bot._handle_callback({"id": "cb", "from": {"id": 111}, "message": {"chat": {"id": 1}}, "data": f"session:open:{s2.id}"})
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111}, "text": "hi s2"})
            self.assertEqual(p1.sent_messages, [])
            self.assertEqual(p2.sent_messages, ["hi s2"])

            bot._handle_callback({"id": "cb", "from": {"id": 111}, "message": {"chat": {"id": 1}}, "data": f"session:open:{s1.id}"})
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111}, "text": "hi s1"})
            self.assertEqual(p1.sent_messages, ["hi s1"])

    def test_topic_thread_routes_to_matching_session_regardless_of_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            s1 = bot.sessions.create("claude", "/a", chat_id=1, owner_user_id=111, message_thread_id=42)
            bot.sessions.update(s1.id, status=STATUS_RUNNING)
            p1 = FakeProvider()
            bot._providers[s1.id] = p1
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111}, "text": "in topic", "message_thread_id": 42})
            self.assertEqual(p1.sent_messages, ["in topic"])


class ReconciliationTests(unittest.TestCase):
    def test_run_reconciles_stale_running_sessions_before_polling(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            session = bot.sessions.create("claude", "/a", chat_id=1, owner_user_id=111)
            bot.sessions.update(session.id, status=STATUS_RUNNING)
            bot.reconcile()
            self.assertEqual(bot.sessions.get(session.id).status, STATUS_DISCONNECTED)

    def test_resume_requires_provider_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            session = bot.sessions.create("claude", "/a", chat_id=1, owner_user_id=111)
            bot.sessions.update(session.id, status=STATUS_DISCONNECTED)
            bot._resume_session(1, bot.sessions.get(session.id))
            self.assertIn("Cannot resume", bot.client.sent[-1][1])

    def test_resume_reuses_same_session_id_not_a_new_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            session = bot.sessions.create("claude", "/a", chat_id=1, owner_user_id=111)
            bot.sessions.update(session.id, status=STATUS_DISCONNECTED, provider_session_id="claude-sess-1")
            before_count = len(bot.sessions.list_for_chat(1))

            fake_provider = FakeProvider()

            class FakeProviderClass:
                def __init__(self, context, on_event=None):
                    self.context = context
                    self.on_event = on_event

                def resume(self, provider_session_id):
                    fake_provider.provider_session_id = provider_session_id

                def is_alive(self):
                    return True

            with mock.patch.dict("agent_box.telegram.bot.PROVIDERS", {"claude": FakeProviderClass}):
                bot._resume_session(1, bot.sessions.get(session.id))

            self.assertEqual(len(bot.sessions.list_for_chat(1)), before_count, "resume must not create a new session record")
            self.assertEqual(bot.sessions.get(session.id).status, STATUS_RUNNING)


if __name__ == "__main__":
    unittest.main()
