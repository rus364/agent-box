import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box.config import RemoteControlConfig, TelegramConfig
from agent_box.telegram.bot import TelegramBot
from agent_box.telegram.offset import OffsetStore
from agent_box.telegram.pending import (
    STATUS_CLAIMED, STATUS_DELIVERED, STATUS_RECORDED, PendingRegistry,
)
from agent_box.telegram.providers.base import AnswerResult, AnswerStatus, Question, QuestionOption
from agent_box.telegram.sessions import (
    STATUS_DISCONNECTED, STATUS_RUNNING, STATUS_STOPPED, SessionRegistry,
)


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

    def __init__(self, alive=True, answer_result=None, raises=None):
        self.sent_messages = []
        self._alive = alive
        self._answer_result = answer_result or AnswerResult(AnswerStatus.DELIVERED)
        self._raises = raises
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
        if self._raises is not None:
            raise self._raises
        return self._answer_result

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
        bot = TelegramBot(
            config, tmp, sessions=sessions, pending=pending, offsets=offsets,
            index_path=tmp / "sessions-index.json",
        )
    return bot


def ask(bot, session, question_id="q1", label="Keep it"):
    question = Question(id=question_id, header="h", text="t", options=[QuestionOption(label)])
    bot._send_question(session, question)
    return next(p["token"] for p in bot.pending._load() if p["question_id"] == question_id)


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


class SessionOwnershipTests(unittest.TestCase):
    """Two allow-listed users in one group chat must not be able to see, open,
    drive, stop, resume or answer each other's sessions. Being on the
    allowlist authorizes running *your own* sessions; agent-box has no
    documented shared-session policy that would say otherwise."""

    def _two_user_chat(self, tmp):
        bot = make_bot(allowed_ids=(111, 222), tmpdir=tmp)
        mine = bot.sessions.create("claude", "/a", chat_id=1, owner_user_id=111)
        theirs = bot.sessions.create("codex", "/b", chat_id=1, owner_user_id=222)
        bot.sessions.update(mine.id, status=STATUS_RUNNING)
        bot.sessions.update(theirs.id, status=STATUS_RUNNING)
        p_mine, p_theirs = FakeProvider(), FakeProvider()
        bot._providers[mine.id] = p_mine
        bot._providers[theirs.id] = p_theirs
        return bot, mine, theirs, p_mine, p_theirs

    def test_sessions_menu_only_lists_your_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, mine, theirs, _p1, _p2 = self._two_user_chat(tmp)
            bot._send_sessions_menu(1, 111)
            keyboard = bot.client.sent[-1][2]["reply_markup"]["inline_keyboard"]
            data = [row[0]["callback_data"] for row in keyboard]
            self.assertIn(f"session:open:{mine.id}", data)
            self.assertNotIn(f"session:open:{theirs.id}", data)

    def test_cannot_open_another_users_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _mine, theirs, _p1, _p2 = self._two_user_chat(tmp)
            bot._handle_callback({
                "id": "cb", "from": {"id": 111}, "message": {"chat": {"id": 1}},
                "data": f"session:open:{theirs.id}",
            })
            self.assertEqual(bot.client.sent, [])

    def test_cannot_stop_another_users_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _mine, theirs, _p1, p_theirs = self._two_user_chat(tmp)
            bot._handle_callback({
                "id": "cb", "from": {"id": 111}, "message": {"chat": {"id": 1}},
                "data": f"session:stop:{theirs.id}",
            })
            self.assertEqual(bot.sessions.get(theirs.id).status, STATUS_RUNNING)
            self.assertTrue(p_theirs.is_alive())
            self.assertEqual(bot.client.sent, [])

    def test_cannot_resume_another_users_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _mine, theirs, _p1, _p2 = self._two_user_chat(tmp)
            bot.sessions.update(theirs.id, status=STATUS_DISCONNECTED, provider_session_id="x")
            bot._handle_callback({
                "id": "cb", "from": {"id": 111}, "message": {"chat": {"id": 1}},
                "data": f"session:resume:{theirs.id}",
            })
            self.assertEqual(bot.sessions.get(theirs.id).status, STATUS_DISCONNECTED)
            self.assertEqual(bot.client.sent, [])

    def test_free_text_from_each_user_goes_to_their_own_session(self):
        """Regression: active-session routing keyed only by chat let the
        second user's message land in the first user's agent."""
        with tempfile.TemporaryDirectory() as tmp:
            bot, mine, theirs, p_mine, p_theirs = self._two_user_chat(tmp)
            bot._handle_callback({"id": "c", "from": {"id": 111}, "message": {"chat": {"id": 1}},
                                  "data": f"session:open:{mine.id}"})
            bot._handle_callback({"id": "c", "from": {"id": 222}, "message": {"chat": {"id": 1}},
                                  "data": f"session:open:{theirs.id}"})
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111}, "text": "from user one"})
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 222}, "text": "from user two"})
            self.assertEqual(p_mine.sent_messages, ["from user one"])
            self.assertEqual(p_theirs.sent_messages, ["from user two"])

    def test_single_live_session_of_another_user_is_not_an_implicit_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(allowed_ids=(111, 222), tmpdir=tmp)
            theirs = bot.sessions.create("codex", "/b", chat_id=1, owner_user_id=222)
            bot.sessions.update(theirs.id, status=STATUS_RUNNING)
            provider = FakeProvider()
            bot._providers[theirs.id] = provider
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111}, "text": "hello"})
            self.assertEqual(provider.sent_messages, [])
            self.assertIn("No active session selected", bot.client.sent[-1][1])

    def test_another_user_cannot_answer_your_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(allowed_ids=(111, 222), tmpdir=tmp)
            session = bot.sessions.create("claude", "/a", chat_id=1, owner_user_id=111)
            provider = FakeProvider()
            bot._providers[session.id] = provider
            token = ask(bot, session)
            bot._handle_answer_callback(1, 222, f"ans:{token}:0")
            self.assertEqual(provider.answered, [])


class CallbackValidationTests(unittest.TestCase):
    def test_session_menu_requires_matching_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            session = bot.sessions.create("claude", "/tmp", chat_id=999, owner_user_id=111)
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
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(allowed_ids=(111,), tmpdir=tmp)
            session = bot.sessions.create("claude", "/tmp", chat_id=1, owner_user_id=111)
            token = ask(bot, session)
            self.assertIsNone(bot.pending.claim(token, user_id=222))


class CallbackDeliveryGatingTests(unittest.TestCase):
    """Success must only be reported once the provider actually confirms."""

    def _setup(self, tmp, **provider_kwargs):
        bot = make_bot(tmpdir=tmp)
        session = bot.sessions.create("claude", "/tmp", chat_id=1, owner_user_id=111)
        provider = FakeProvider(**provider_kwargs)
        bot._providers[session.id] = provider
        token = ask(bot, session)
        return bot, session, provider, token

    def _status(self, bot, token):
        return next(p["status"] for p in bot.pending._load() if p["token"] == token)

    def test_successful_delivery_marks_resolved_and_clears_buttons(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _s, provider, token = self._setup(tmp)
            bot._handle_answer_callback(1, 111, f"ans:{token}:0")
            self.assertEqual(provider.answered, [("q1", ["Keep it"])])
            self.assertEqual(len(bot.client.edited), 1)
            self.assertEqual(self._status(bot, token), STATUS_DELIVERED)
            self.assertIsNone(bot.pending.claim(token, 111))  # cannot answer again

    def test_rejected_delivery_does_not_claim_success_and_does_not_clear_buttons(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _s, _p, token = self._setup(tmp, answer_result=AnswerResult(AnswerStatus.REJECTED))
            bot._handle_answer_callback(1, 111, f"ans:{token}:0")
            self.assertEqual(len(bot.client.edited), 0, "buttons must not be cleared on delivery failure")
            self.assertIn("did not accept", bot.client.sent[-1][1])
            self.assertIsNone(bot.pending.claim(token, 111))

    def test_provider_write_failure_does_not_leave_the_question_claimed(self):
        """Regression: a write/provider exception during answer delivery used
        to leave the pending question stuck in `claimed` forever."""
        with tempfile.TemporaryDirectory() as tmp:
            bot, _s, _p, token = self._setup(
                tmp, answer_result=AnswerResult(AnswerStatus.FAILED, detail="broken pipe"))
            bot._handle_answer_callback(1, 111, f"ans:{token}:0")
            self.assertNotEqual(self._status(bot, token), STATUS_CLAIMED)
            self.assertIn("could not be delivered", bot.client.sent[-1][1])
            self.assertIn("broken pipe", bot.client.sent[-1][1])
            self.assertEqual(len(bot.client.edited), 0)

    def test_provider_raising_does_not_leave_the_question_claimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _s, _p, token = self._setup(tmp, raises=OSError("pipe is gone"))
            with self.assertLogs("agent_box.telegram", level="WARNING"):
                bot._handle_answer_callback(1, 111, f"ans:{token}:0")
            self.assertNotEqual(self._status(bot, token), STATUS_CLAIMED)
            self.assertIn("could not be delivered", bot.client.sent[-1][1])

    def test_dead_provider_reports_failure_not_silent_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _s, provider, token = self._setup(tmp, alive=False)
            bot._handle_answer_callback(1, 111, f"ans:{token}:0")
            self.assertEqual(provider.answered, [])
            self.assertIn("no longer active", bot.client.sent[-1][1])


class MultiQuestionReportingTests(unittest.TestCase):
    """An answer to one question of a multi-question request must be reported
    as recorded, not delivered, until the aggregate response is submitted."""

    def _setup(self, tmp):
        bot = make_bot(tmpdir=tmp)
        session = bot.sessions.create("codex", "/tmp", chat_id=1, owner_user_id=111)
        provider = FakeProvider()
        bot._providers[session.id] = provider
        first = ask(bot, session, question_id="q_a", label="Yes")
        second = ask(bot, session, question_id="q_b", label="No")
        return bot, session, provider, first, second

    def _status(self, bot, token):
        return next(p["status"] for p in bot.pending._load() if p["token"] == token)

    def test_first_answer_is_reported_as_recorded_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _s, provider, first, _second = self._setup(tmp)
            provider._answer_result = AnswerResult(AnswerStatus.RECORDED, outstanding=["q_b"])
            bot._handle_answer_callback(1, 111, f"ans:{first}:0")
            message = bot.client.sent[-1][1]
            self.assertIn("Recorded", message)
            self.assertIn("1 more question", message)
            self.assertNotIn("delivered", message.lower())
            self.assertEqual(self._status(bot, first), STATUS_RECORDED)

    def test_final_answer_promotes_the_earlier_recorded_one_to_delivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _s, provider, first, second = self._setup(tmp)
            provider._answer_result = AnswerResult(AnswerStatus.RECORDED, outstanding=["q_b"])
            bot._handle_answer_callback(1, 111, f"ans:{first}:0")
            provider._answer_result = AnswerResult(AnswerStatus.DELIVERED, resolved=["q_a", "q_b"])
            bot._handle_answer_callback(1, 111, f"ans:{second}:0")
            self.assertEqual(self._status(bot, first), STATUS_DELIVERED)
            self.assertEqual(self._status(bot, second), STATUS_DELIVERED)


class QuestionWithdrawalTests(unittest.TestCase):
    def test_provider_withdrawing_a_question_retires_its_buttons(self):
        from agent_box.telegram.providers.base import AgentEvent, EventKind

        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            session = bot.sessions.create("codex", "/tmp", chat_id=1, owner_user_id=111)
            bot._providers[session.id] = FakeProvider()
            token = ask(bot, session)
            bot._on_provider_event(session.id, AgentEvent(kind=EventKind.QUESTION_CLOSED, text="q1"))
            self.assertEqual(len(bot.client.edited), 1)
            self.assertIsNone(bot.pending.claim(token, 111))


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

    def test_main_chat_message_does_not_route_to_a_topic_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            s1 = bot.sessions.create("claude", "/a", chat_id=1, owner_user_id=111, message_thread_id=42)
            bot.sessions.update(s1.id, status=STATUS_RUNNING)
            p1 = FakeProvider()
            bot._providers[s1.id] = p1
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111}, "text": "outside the topic"})
            self.assertEqual(p1.sent_messages, [])
            self.assertIn("No active session selected", bot.client.sent[-1][1])


class ForumTopicMultiSessionTests(unittest.TestCase):
    """A topic that has hosted several sessions over time must route to the
    live one, not to the first historical record that happens to match."""

    def _topic_with_history(self, tmp):
        bot = make_bot(tmpdir=tmp)
        old = bot.sessions.create("claude", "/a", chat_id=1, owner_user_id=111, message_thread_id=42)
        current = bot.sessions.create("codex", "/b", chat_id=1, owner_user_id=111, message_thread_id=42)
        bot.sessions.update(old.id, status=STATUS_STOPPED)
        bot.sessions.update(current.id, status=STATUS_RUNNING)
        p_old, p_current = FakeProvider(alive=False), FakeProvider()
        bot._providers[current.id] = p_current
        return bot, old, current, p_old, p_current

    def test_routes_to_the_live_session_not_the_oldest_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _old, _current, p_old, p_current = self._topic_with_history(tmp)
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111},
                                 "text": "hello topic", "message_thread_id": 42})
            self.assertEqual(p_old.sent_messages, [])
            self.assertEqual(p_current.sent_messages, ["hello topic"])

    def test_two_live_sessions_in_one_topic_require_explicit_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, old, _current, _p_old, p_current = self._topic_with_history(tmp)
            bot.sessions.update(old.id, status=STATUS_RUNNING)
            p_old = FakeProvider()
            bot._providers[old.id] = p_old
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111},
                                 "text": "ambiguous", "message_thread_id": 42})
            self.assertEqual(p_old.sent_messages, [])
            self.assertEqual(p_current.sent_messages, [])
            self.assertIn("No active session selected", bot.client.sent[-1][1])

    def test_explicitly_opened_session_wins_inside_the_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, old, _current, _p_old, p_current = self._topic_with_history(tmp)
            bot.sessions.update(old.id, status=STATUS_RUNNING)
            p_old = FakeProvider()
            bot._providers[old.id] = p_old
            bot._handle_callback({
                "id": "cb", "from": {"id": 111},
                "message": {"chat": {"id": 1}, "message_thread_id": 42},
                "data": f"session:open:{old.id}",
            })
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111},
                                 "text": "to the opened one", "message_thread_id": 42})
            self.assertEqual(p_old.sent_messages, ["to the opened one"])
            self.assertEqual(p_current.sent_messages, [])

    def test_active_selection_in_a_topic_does_not_leak_to_the_main_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            topic = bot.sessions.create("claude", "/a", chat_id=1, owner_user_id=111, message_thread_id=42)
            bot.sessions.update(topic.id, status=STATUS_RUNNING)
            provider = FakeProvider()
            bot._providers[topic.id] = provider
            bot._handle_callback({
                "id": "cb", "from": {"id": 111},
                "message": {"chat": {"id": 1}, "message_thread_id": 42},
                "data": f"session:open:{topic.id}",
            })
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111}, "text": "main chat"})
            self.assertEqual(provider.sent_messages, [])


class ResumeProjectRevalidationTests(unittest.TestCase):
    """Workspace roots are host-administered and can change after a session
    was created. A stored project path is a record of a past decision, not a
    standing grant, so it must be re-authorized on every resume."""

    def _session_in(self, tmp, project):
        bot = make_bot(tmpdir=tmp, roots=[str(project.parent)])
        session = bot.sessions.create("claude", str(project), chat_id=1, owner_user_id=111)
        bot.sessions.update(session.id, status=STATUS_DISCONNECTED, provider_session_id="prov-1")
        return bot, bot.sessions.get(session.id)

    def test_resume_refuses_when_the_workspace_root_was_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "roots" / "proj"
            project.mkdir(parents=True)
            bot, session = self._session_in(tmp, project)
            bot.config.remote_control.workspace_roots = []  # root removed on the host
            bot._resume_session(1, 111, session)
            self.assertIn("no longer inside a configured", bot.client.sent[-1][1])
            self.assertNotIn(session.id, bot._providers)

    def test_resume_refuses_when_the_root_was_narrowed_to_exclude_the_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = Path(tmp) / "roots"
            project = roots / "proj"
            other = roots / "other"
            project.mkdir(parents=True)
            other.mkdir()
            bot, session = self._session_in(tmp, project)
            bot.config.remote_control.workspace_roots = [str(other)]
            bot._resume_session(1, 111, session)
            self.assertIn("no longer inside a configured", bot.client.sent[-1][1])

    def test_resume_refuses_when_the_project_directory_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "roots" / "proj"
            project.mkdir(parents=True)
            bot, session = self._session_in(tmp, project)
            project.rmdir()
            bot._resume_session(1, 111, session)
            self.assertIn("no longer inside a configured", bot.client.sent[-1][1])

    def test_resume_refuses_when_the_project_became_a_symlink_escaping_the_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = Path(tmp) / "roots"
            roots.mkdir()
            project = roots / "proj"
            project.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            bot, session = self._session_in(tmp, project)
            project.rmdir()
            project.symlink_to(outside)
            bot._resume_session(1, 111, session)
            self.assertIn("no longer inside a configured", bot.client.sent[-1][1])

    def test_resume_uses_the_current_canonical_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = Path(tmp) / "roots"
            real = roots / "real"
            real.mkdir(parents=True)
            link = roots / "proj"
            link.symlink_to(real)
            bot = make_bot(tmpdir=tmp, roots=[str(roots)])
            session = bot.sessions.create("claude", str(link), chat_id=1, owner_user_id=111)
            bot.sessions.update(session.id, status=STATUS_DISCONNECTED, provider_session_id="prov-1")
            seen = {}

            class Recording:
                def __init__(self, context, on_event=None):
                    seen["project"] = context.host_project

                def resume(self, provider_session_id):
                    pass

                def is_alive(self):
                    return True

            with mock.patch.dict("agent_box.telegram.bot.PROVIDERS", {"claude": Recording}):
                bot._resume_session(1, 111, bot.sessions.get(session.id))
            self.assertEqual(seen["project"], real.resolve())
            self.assertEqual(bot.sessions.get(session.id).host_project, str(real.resolve()))


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
            bot = make_bot(tmpdir=tmp, roots=[tmp])
            session = bot.sessions.create("claude", tmp, chat_id=1, owner_user_id=111)
            bot.sessions.update(session.id, status=STATUS_DISCONNECTED)
            bot._resume_session(1, 111, bot.sessions.get(session.id))
            self.assertIn("Cannot resume", bot.client.sent[-1][1])

    def test_resume_reuses_same_session_id_not_a_new_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "proj"
            project.mkdir()
            bot = make_bot(tmpdir=tmp, roots=[tmp])
            session = bot.sessions.create("claude", str(project), chat_id=1, owner_user_id=111)
            bot.sessions.update(session.id, status=STATUS_DISCONNECTED, provider_session_id="claude-sess-1")
            before_count = len(bot.sessions.list_for_chat(1))

            class FakeProviderClass:
                def __init__(self, context, on_event=None):
                    self.context = context
                    self.on_event = on_event

                def resume(self, provider_session_id):
                    pass

                def is_alive(self):
                    return True

            with mock.patch.dict("agent_box.telegram.bot.PROVIDERS", {"claude": FakeProviderClass}):
                bot._resume_session(1, 111, bot.sessions.get(session.id))

            self.assertEqual(len(bot.sessions.list_for_chat(1)), before_count, "resume must not create a new session record")
            self.assertEqual(bot.sessions.get(session.id).status, STATUS_RUNNING)


class SessionIndexTests(unittest.TestCase):
    """The container-readable index must carry a real, openable pointer and
    nothing sensitive."""

    def test_index_lists_live_sessions_and_no_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            session = bot.sessions.create("claude", "/a", chat_id=1, owner_user_id=111)
            bot.sessions.update(session.id, status=STATUS_RUNNING)
            bot._providers[session.id] = FakeProvider()
            bot._publish_index()
            import json
            data = json.loads((Path(tmp) / "sessions-index.json").read_text())
            self.assertEqual(data["sessions"], [
                {"host_project": "/a", "id": session.id, "provider": "claude"},
            ])
            raw = (Path(tmp) / "sessions-index.json").read_text()
            for forbidden in ("chat_id", "owner_user_id", "token", "offset"):
                self.assertNotIn(forbidden, raw)

    def test_stopped_sessions_are_not_advertised(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            session = bot.sessions.create("claude", "/a", chat_id=1, owner_user_id=111)
            bot.sessions.update(session.id, status=STATUS_STOPPED)
            bot._publish_index()
            import json
            data = json.loads((Path(tmp) / "sessions-index.json").read_text())
            self.assertEqual(data["sessions"], [])

    def test_session_command_opens_the_session_the_notification_points_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmpdir=tmp)
            session = bot.sessions.create("claude", "/a", chat_id=1, owner_user_id=111)
            bot.sessions.update(session.id, status=STATUS_RUNNING)
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111}, "text": f"/session {session.id}"})
            self.assertIn("Status:", bot.client.sent[-1][1])

    def test_session_command_refuses_another_users_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(allowed_ids=(111, 222), tmpdir=tmp)
            session = bot.sessions.create("claude", "/a", chat_id=1, owner_user_id=222)
            bot._handle_message({"chat": {"id": 1}, "from": {"id": 111}, "text": f"/session {session.id}"})
            self.assertIn("No such session", bot.client.sent[-1][1])


if __name__ == "__main__":
    unittest.main()
