"""Telegram remote-control daemon.

Long-polls ``getUpdates`` (no webhook, no public port), renders an
inline-button menu, and routes messages/callbacks to per-session provider
adapters (Claude or Codex). Each session gets its own disposable container,
built from the same image and configured by the same authoritative runtime
policy (``agent_box.runtime.base_podman_args``) as normal ``agent-box
claude``/``codex`` use.

Security model, stated precisely, because "the chat is authorized" is not the
same statement as "this person may drive this session":

* Every update must come from a user in the configured allowlist. That is the
  outer gate and it is unchanged.
* Every session has an ``owner_user_id``. Only the owner can list it, open
  it, route free text to it, interrupt/stop/resume it, or answer its
  questions. agent-box has no shared-session concept, so there is no
  documented policy under which a second allow-listed user in the same group
  chat may act on somebody else's session; without one, the safe reading of
  "authorized user" is "authorized to run *their own* sessions here".
* Routing is scoped by ``(chat_id, user_id, message_thread_id)``. Two
  allow-listed people talking in one group chat therefore have independent
  active sessions instead of silently sharing (and stealing) one.
* In a forum topic, routing uses the *live* session for that topic, never
  "the first historical session that was once created in this topic".
* A project is re-validated against the currently configured
  ``workspace_roots`` on every resume, not only when the session was created:
  a root removed (or a project moved/replaced by a symlink) after the fact
  must not still be reachable through an old session record.
"""
from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import session_index
from ..config import TelegramConfig
from ..mounts import MountSpec
from ..workspace import Workspace
from .api import TelegramAPIError, TelegramClient
from .offset import OffsetStore
from .pending import Option, PendingRegistry
from .projects import list_projects, resolve_allowed_project
from .providers.base import (
    AgentEvent, AnswerStatus, EventKind, Provider, ProviderError, Question, RuntimeContext,
)
from .providers.claude import ClaudeProvider
from .providers.codex import CodexProvider
from .sessions import (
    LIVE_STATUSES, STATUS_DISCONNECTED, STATUS_ERROR, STATUS_RUNNING, STATUS_STOPPED,
    STATUS_WAITING_INPUT, Session, SessionRegistry,
)

logger = logging.getLogger("agent_box.telegram")

PROVIDERS = {"claude": ClaudeProvider, "codex": CodexProvider}
MAX_TELEGRAM_TEXT = 3500

RESUMABLE_STATUSES = (STATUS_STOPPED, STATUS_DISCONNECTED, STATUS_ERROR)


def escape(text: str) -> str:
    return html.escape(text, quote=False)


@dataclass
class WizardState:
    provider: str | None = None
    thread_id: int | None = None
    created_at: float = field(default_factory=time.time)


class TelegramBot:
    def __init__(
        self, config: TelegramConfig, script_dir: Path, *,
        sessions: SessionRegistry | None = None, pending: PendingRegistry | None = None,
        offsets: OffsetStore | None = None, index_path: Path | None = None,
    ):
        if not config.remote_control.enabled:
            raise RuntimeError("remote control is not enabled in the Telegram configuration")
        if not config.token:
            raise RuntimeError("Telegram bot token is not configured")
        self.config = config
        self.script_dir = script_dir
        self.client = TelegramClient(config.token)
        self.sessions = sessions or SessionRegistry()
        self.pending = pending or PendingRegistry()
        self.offsets = offsets or OffsetStore()
        self.index_path = index_path
        self._providers: dict[str, Provider] = {}
        self._wizards: dict[tuple[int, int], WizardState] = {}
        self._wizard_projects: dict[tuple[int, int], list] = {}
        # (chat_id, user_id, message_thread_id) -> session_id. The user is part
        # of the key on purpose: two allow-listed people in one group chat must
        # not share (or hijack) each other's active session. message_thread_id
        # is None for the chat's "main" routing target (a private chat, or a
        # forum message outside any topic).
        self._active: dict[tuple[int, int, int | None], str] = {}
        # (chat_id, user_id, message_thread_id) -> pending token whose owner
        # tapped "Other..." and whose next plain message is that question's
        # free-text answer rather than a new turn.
        self._awaiting_text: dict[tuple[int, int, int | None], str] = {}

    # -- authorization -------------------------------------------------
    def _is_allowed(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.config.remote_control.allowed_user_ids

    def _owned(self, session: Session | None, chat_id: int, user_id: int) -> Session | None:
        """Return the session only if this user owns it *in this chat*."""
        if session is None:
            return None
        if session.chat_id != chat_id:
            return None
        if session.owner_user_id != user_id:
            return None
        return session

    def _sessions_for(self, chat_id: int, user_id: int) -> list[Session]:
        return [s for s in self.sessions.list_for_chat(chat_id) if s.owner_user_id == user_id]

    # -- startup reconciliation -----------------------------------------
    def reconcile(self) -> None:
        """Mark persisted "running" sessions honestly as disconnected.

        Called once before the poll loop starts: this fresh process has no
        provider attached to anything yet, so any session whose stored
        status implies a live process is lying until proven otherwise.
        """
        changed = self.sessions.reconcile_on_startup()
        for session in changed:
            self._forget_session_awaited(session.id)
            self.pending.discard_session(session.id)
        self._publish_index()

    def _publish_index(self) -> None:
        """Republish the container-readable session index (see session_index).

        Only sessions with a live provider process in *this* daemon are
        published: the index exists so a notification can point at a session
        the user can actually open right now.
        """
        live = [s for s in self.sessions.list() if s.status in LIVE_STATUSES and s.id in self._providers]
        try:
            if self.index_path is not None:
                session_index.publish(live, self.index_path)
            else:
                session_index.publish(live)
        except OSError:
            logger.warning("could not publish the session index", exc_info=True)

    # -- runtime context -------------------------------------------------
    def _context_for(self, project: Path, extra_mounts: list[MountSpec] | None = None) -> RuntimeContext:
        return RuntimeContext(
            workspace=Workspace(project, project), script_dir=self.script_dir,
            extra_mounts=extra_mounts or [],
        )

    # -- main loop -------------------------------------------------------
    def run(self, stop_after: int | None = None) -> None:
        self.reconcile()
        offset = self.offsets.load()
        if offset is not None:
            offset += 1  # stored value is the last *processed* update_id
        processed = 0
        backoff = 1.0
        while stop_after is None or processed < stop_after:
            try:
                updates = self.client.get_updates(offset, timeout=30)
                backoff = 1.0
            except TelegramAPIError as exc:
                logger.warning("getUpdates failed: %s", exc.description or exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for update in updates:
                update_id = int(update["update_id"])
                offset = update_id + 1
                try:
                    self.handle_update(update)
                except Exception:
                    logger.exception("error handling update")
                # Persisted only *after* the handler returns (at-least-once,
                # not at-most-once): a crash mid-handling replays this
                # update on restart rather than silently dropping it.
                self.offsets.save(update_id)
                processed += 1

    def handle_update(self, update: dict[str, Any]) -> None:
        if "message" in update:
            self._handle_message(update["message"])
        elif "callback_query" in update:
            self._handle_callback(update["callback_query"])

    # -- messages -----------------------------------------------------
    def _handle_message(self, message: dict[str, Any]) -> None:
        chat_id = message.get("chat", {}).get("id")
        user_id = (message.get("from") or {}).get("id")
        text = (message.get("text") or "").strip()
        thread_id = message.get("message_thread_id")
        if chat_id is None:
            return
        if not self._is_allowed(user_id):
            return  # unknown users receive no response and no information
        if text.startswith("/"):
            self._handle_command(chat_id, user_id, text, thread_id)
            return
        self._route_free_text(chat_id, user_id, text, thread_id)

    def _handle_command(self, chat_id: int, user_id: int, text: str, thread_id: int | None) -> None:
        parts = text.split()
        command = parts[0].split("@")[0].lower()
        args = parts[1:]
        if command == "/start":
            self._send_main_menu(chat_id, thread_id)
        elif command == "/sessions":
            self._send_sessions_menu(chat_id, user_id, thread_id)
        elif command == "/session":
            self._open_session_by_id(chat_id, user_id, args[0] if args else "", thread_id)
        elif command == "/new":
            self._start_new_session_wizard(chat_id, user_id, thread_id)
        elif command == "/status":
            self._send_status(chat_id, user_id, thread_id)
        elif command == "/usage":
            self._send_usage(chat_id, thread_id)
        else:
            self._say(chat_id, "Unknown command. Try /start.", thread_id)

    def _say(self, chat_id: int, text: str, thread_id: int | None = None, **kwargs: Any) -> Any:
        return self.client.send_message(chat_id, text, message_thread_id=thread_id, **kwargs)

    def _route_free_text(self, chat_id: int, user_id: int, text: str, thread_id: int | None) -> None:
        awaited = self._awaiting_text.pop((chat_id, user_id, thread_id), None)
        if awaited is not None:
            # The user asked to answer this question in their own words, so
            # this message is that question's answer -- not a new turn. It
            # matters that it goes back as the *answer*: both providers block
            # on an outstanding request, and a plain turn would leave that
            # request unanswered.
            self._submit_answer(chat_id, user_id, awaited, lambda _claimed: [text])
            return
        session = self._resolve_routing_target(chat_id, user_id, thread_id)
        if session is None:
            self._say(
                chat_id,
                "No active session selected. Use /sessions to pick one, or /new to create one.",
                thread_id,
            )
            return
        provider = self._providers.get(session.id)
        if provider is None or not provider.is_alive():
            self._say(
                chat_id, f"Session {escape(session.id)} isn't running. Open it from /sessions to resume.",
                thread_id,
            )
            return
        provider.send_message(text)
        self.sessions.update(session.id, status=STATUS_RUNNING)

    def _resolve_routing_target(self, chat_id: int, user_id: int, thread_id: int | None) -> Session | None:
        """Pick the session a plain message belongs to, deterministically.

        Scoped to this user's own sessions in this chat, and -- in a forum
        topic -- to that topic. Within the candidate set the order is:
        the explicitly opened active session, else the single live session,
        else nothing (the user is asked to choose rather than the bot
        guessing). Historical, non-live sessions never win: a topic that has
        hosted several sessions over time routes to the one that is actually
        running now, not to the oldest record that happens to match.
        """
        candidates = [s for s in self._sessions_for(chat_id, user_id) if s.message_thread_id == thread_id]
        by_id = {s.id: s for s in candidates}

        active_id = self._active.get((chat_id, user_id, thread_id))
        active = by_id.get(active_id) if active_id else None
        if active is not None and active.status in LIVE_STATUSES:
            return active

        live = [s for s in candidates if s.status in (STATUS_RUNNING, STATUS_WAITING_INPUT)]
        if len(live) == 1:
            return live[0]
        if active is not None and len(live) == 0:
            # Explicitly opened but not live: let the caller report that
            # honestly ("isn't running") rather than claiming no selection.
            return active
        return None

    def _set_active(self, chat_id: int, user_id: int, session: Session) -> None:
        self._active[(chat_id, user_id, session.message_thread_id)] = session.id

    # -- menus ----------------------------------------------------------
    def _send_main_menu(self, chat_id: int, thread_id: int | None = None) -> None:
        keyboard = {"inline_keyboard": [
            [{"text": "Sessions", "callback_data": "menu:sessions"}, {"text": "New session", "callback_data": "menu:new"}],
            [{"text": "Usage", "callback_data": "menu:usage"}, {"text": "Refresh", "callback_data": "menu:main"}],
        ]}
        self._say(chat_id, "<b>agent-box</b>\nRemote control menu", thread_id, reply_markup=keyboard)

    def _send_sessions_menu(self, chat_id: int, user_id: int, thread_id: int | None = None) -> None:
        sessions = self._sessions_for(chat_id, user_id)
        if not sessions:
            self._say(chat_id, "No sessions yet. Use /new to create one.", thread_id)
            return
        active_id = self._active.get((chat_id, user_id, thread_id))
        rows = []
        for s in sessions:
            marker = " *" if s.id == active_id else ""
            label = f"{s.provider} · {Path(s.host_project).name} · {s.status}{marker}"
            rows.append([{"text": label, "callback_data": f"session:open:{s.id}"}])
        rows.append([{"text": "New session", "callback_data": "menu:new"}])
        self._say(chat_id, "<b>Sessions</b> (* = active)", thread_id, reply_markup={"inline_keyboard": rows})

    def _open_session_by_id(self, chat_id: int, user_id: int, session_id: str, thread_id: int | None) -> None:
        session = self._owned(self.sessions.get(session_id), chat_id, user_id) if session_id else None
        if session is None:
            self._say(chat_id, "No such session in this chat. Use /sessions to list yours.", thread_id)
            return
        self._send_session_menu(chat_id, user_id, session)

    def _send_session_menu(self, chat_id: int, user_id: int, session: Session) -> None:
        self._set_active(chat_id, user_id, session)
        rows = [[
            {"text": "Interrupt", "callback_data": f"session:interrupt:{session.id}"},
            {"text": "Stop", "callback_data": f"session:stop:{session.id}"},
        ]]
        if session.status in RESUMABLE_STATUSES:
            rows = [[{"text": "Resume", "callback_data": f"session:resume:{session.id}"}]]
        rows.append([{"text": "Back", "callback_data": "menu:sessions"}])
        where = "this topic" if session.message_thread_id is not None else "this chat"
        text = (
            f"<b>{escape(session.provider)} · {escape(Path(session.host_project).name)}</b>\n"
            f"Status: {escape(session.status)}\n"
            f"Project: {escape(session.host_project)}\n\n"
            f"This is now your active session for {where} -- your plain messages go to it."
        )
        self._say(chat_id, text, session.message_thread_id, reply_markup={"inline_keyboard": rows})

    def _send_status(self, chat_id: int, user_id: int, thread_id: int | None = None) -> None:
        sessions = self._sessions_for(chat_id, user_id)
        running = sum(1 for s in sessions if s.status in (STATUS_RUNNING, STATUS_WAITING_INPUT))
        self._say(chat_id, f"{len(sessions)} session(s), {running} active.", thread_id)

    def _send_usage(self, chat_id: int, thread_id: int | None = None) -> None:
        from .. import usage
        try:
            claude = usage.claude_usage()
        except Exception:
            claude = usage.PLACEHOLDER_USAGE
        try:
            codex = usage.codex_usage()
        except Exception:
            codex = usage.PLACEHOLDER_USAGE
        self._say(chat_id, f"Claude {escape(claude)} │ Codex {escape(codex)}", thread_id)

    # -- new-session wizard ------------------------------------------
    def _start_new_session_wizard(self, chat_id: int, user_id: int, thread_id: int | None = None) -> None:
        self._wizards[(chat_id, user_id)] = WizardState(thread_id=thread_id)
        keyboard = {"inline_keyboard": [
            [{"text": "Claude", "callback_data": "new:provider:claude"}, {"text": "Codex", "callback_data": "new:provider:codex"}],
        ]}
        self._say(chat_id, "New session — choose an agent:", thread_id, reply_markup=keyboard)

    def _wizard_pick_provider(self, chat_id: int, user_id: int, provider: str) -> None:
        if provider not in PROVIDERS:
            self._say(chat_id, "Unknown agent.")
            return
        state = self._wizards.setdefault((chat_id, user_id), WizardState())
        state.provider = provider
        roots = self.config.remote_control.workspace_roots
        projects = list_projects(roots)
        if not projects:
            self._say(chat_id, "No allowed workspace roots are configured. Run 'agent-box telegram roots add <path>' on the host.", state.thread_id)
            return
        rows = [[{"text": p.label, "callback_data": f"new:project:{idx}"}] for idx, p in enumerate(projects)]
        self._wizard_projects[(chat_id, user_id)] = projects
        self._say(chat_id, f"Project for {escape(provider)}:", state.thread_id, reply_markup={"inline_keyboard": rows})

    def _wizard_pick_project(self, chat_id: int, user_id: int, idx: int) -> None:
        state = self._wizards.get((chat_id, user_id))
        projects = self._wizard_projects.get((chat_id, user_id)) or []
        if state is None or state.provider is None or idx < 0 or idx >= len(projects):
            self._say(chat_id, "That session setup expired; use /new again.")
            return
        project = projects[idx]
        allowed = resolve_allowed_project(project.path, self.config.remote_control.workspace_roots)
        if allowed is None:
            self._say(chat_id, "That project is no longer within an allowed workspace root.", state.thread_id)
            return
        self._wizards.pop((chat_id, user_id), None)
        self._create_session(chat_id, user_id, state.provider, allowed, thread_id=state.thread_id)

    def _create_session(
        self, chat_id: int, user_id: int, provider_name: str, project: Path, thread_id: int | None = None,
    ) -> None:
        session = self.sessions.create(provider_name, str(project), chat_id, user_id, message_thread_id=thread_id)
        provider_cls = PROVIDERS[provider_name]
        provider = provider_cls(self._context_for(project), on_event=lambda ev, sid=session.id: self._on_provider_event(sid, ev))
        try:
            provider.start()
        except ProviderError as exc:
            self.sessions.update(session.id, status=STATUS_ERROR)
            self._say(chat_id, f"Failed to start {escape(provider_name)} session: {escape(str(exc))}", thread_id)
            return
        self._providers[session.id] = provider
        stored = self.sessions.update(
            session.id, status=STATUS_RUNNING, provider_session_id=provider.provider_session_id,
        )
        session = stored or session
        self._set_active(chat_id, user_id, session)
        self._publish_index()
        self._say(
            chat_id,
            f"Session created: {escape(provider_name)} · {escape(project.name)}\n"
            f"Session id: <code>{escape(session.id)}</code>\nSend a message to begin.",
            thread_id,
        )

    # -- provider events -> Telegram -----------------------------------
    def _on_provider_event(self, session_id: str, event: AgentEvent) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            return
        if event.kind == EventKind.TEXT and event.text.strip():
            self._send_chunked(session.chat_id, event.text, session.message_thread_id)
        elif event.kind == EventKind.QUESTION and event.question is not None:
            self._send_question(session, event.question)
        elif event.kind == EventKind.QUESTION_CLOSED:
            self._close_question(session, event.text)
        elif event.kind == EventKind.TURN_DONE:
            self.sessions.update(session.id, status=STATUS_RUNNING)
        elif event.kind == EventKind.ERROR:
            self.sessions.update(session.id, status=STATUS_ERROR)
            self._say(session.chat_id, f"⚠️ {escape(event.text)}", session.message_thread_id)
            self._publish_index()
        elif event.kind == EventKind.STOPPED:
            current = self.sessions.get(session.id)
            if current is not None and current.status != STATUS_ERROR:
                self.sessions.update(session.id, status=STATUS_STOPPED)
            self._providers.pop(session.id, None)
            self._publish_index()

    def _send_chunked(self, chat_id: int, text: str, thread_id: int | None) -> None:
        for i in range(0, len(text), MAX_TELEGRAM_TEXT):
            self._say(chat_id, escape(text[i:i + MAX_TELEGRAM_TEXT]), thread_id)

    def _question_keyboard(self, pending) -> dict[str, Any]:
        """Render a question's buttons for its provider semantics.

        Single-select: one button per option, tapping it answers immediately.
        Multi-select: the options are checkbox toggles and nothing is submitted
        until "Submit" is tapped. Free text ("Other..."), where the provider
        allows it, arms the next plain message as the answer.
        """
        rows: list[list[dict[str, str]]] = []
        selected = set(pending.selected or [])
        for index, option in enumerate(pending.options):
            label = option["label"]
            if pending.multi_select:
                mark = "☑" if index in selected else "☐"
                rows.append([{"text": f"{mark} {label}", "callback_data": f"sel:{pending.token}:{index}"}])
            else:
                rows.append([{"text": label, "callback_data": f"ans:{pending.token}:{index}"}])
        footer: list[dict[str, str]] = []
        if pending.multi_select:
            count = len(selected)
            footer.append({"text": f"Submit ({count})" if count else "Submit",
                           "callback_data": f"done:{pending.token}"})
        if pending.allow_free_text:
            footer.append({"text": "Other...", "callback_data": f"text:{pending.token}"})
        if footer:
            rows.append(footer)
        return {"inline_keyboard": rows}

    def _send_question(self, session: Session, question: Question) -> None:
        self.sessions.update(session.id, status=STATUS_WAITING_INPUT)
        options = [Option(o.label, o.label) for o in question.options]
        allow_free_text = question.allow_free_text or not options
        pending = self.pending.create(
            session.id, session.chat_id, session.owner_user_id, question.id, options,
            multi_select=question.multi_select and bool(options),
            allow_free_text=allow_free_text,
        )
        lines = [f"<b>{escape(question.header)}</b>", "", escape(question.text)]
        if pending.multi_select:
            lines += ["", "<i>Pick any number of options, then Submit.</i>"]
        elif allow_free_text and options:
            lines += ["", "<i>Or tap Other... to answer in your own words.</i>"]
        elif allow_free_text:
            lines += ["", "<i>Tap Other... and send your answer as a message.</i>"]
        result = self._say(
            session.chat_id, "\n".join(lines), session.message_thread_id,
            reply_markup=self._question_keyboard(pending),
        )
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is not None:
            self.pending.set_message_id(pending.token, message_id)

    def _forget_awaited(self, tokens: set[str]) -> None:
        """Disarm any "answer this by message" prompt for retired questions."""
        for key, token in list(self._awaiting_text.items()):
            if token in tokens:
                del self._awaiting_text[key]

    def _forget_session_awaited(self, session_id: str) -> None:
        tokens = {p["token"] for p in self.pending._load() if p.get("session_id") == session_id}
        self._forget_awaited(tokens)

    def _close_question(self, session: Session, question_id: str) -> None:
        """The provider withdrew a question: retire its buttons honestly."""
        retired = self.pending.discard_question(session.id, question_id)
        if retired is not None:
            self._forget_awaited({retired.token})
        if retired is None or retired.message_id is None:
            return
        try:
            self.client.edit_message_reply_markup(session.chat_id, retired.message_id, None)
        except TelegramAPIError:
            logger.warning("could not clear buttons for a withdrawn question", exc_info=True)

    # -- callbacks --------------------------------------------------
    def _handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = callback.get("id", "")
        user_id = (callback.get("from") or {}).get("id")
        message = callback.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        thread_id = message.get("message_thread_id")
        data = callback.get("data") or ""
        if not self._is_allowed(user_id) or chat_id is None:
            self.client.answer_callback_query(callback_id)
            return
        try:
            self._dispatch_callback(chat_id, user_id, data, thread_id)
        finally:
            self.client.answer_callback_query(callback_id)

    def _dispatch_callback(self, chat_id: int, user_id: int, data: str, thread_id: int | None = None) -> None:
        if data == "menu:main":
            self._send_main_menu(chat_id, thread_id)
        elif data == "menu:sessions":
            self._send_sessions_menu(chat_id, user_id, thread_id)
        elif data == "menu:new":
            self._start_new_session_wizard(chat_id, user_id, thread_id)
        elif data == "menu:usage":
            self._send_usage(chat_id, thread_id)
        elif data.startswith("new:provider:"):
            self._wizard_pick_provider(chat_id, user_id, data.split(":", 2)[2])
        elif data.startswith("new:project:"):
            try:
                index = int(data.split(":", 2)[2])
            except ValueError:
                return
            self._wizard_pick_project(chat_id, user_id, index)
        elif data.startswith("session:open:"):
            session = self._owned(self.sessions.get(data.split(":", 2)[2]), chat_id, user_id)
            if session is not None:
                self._send_session_menu(chat_id, user_id, session)
        elif data.startswith("session:interrupt:"):
            self._session_action(chat_id, user_id, data.split(":", 2)[2], "interrupt")
        elif data.startswith("session:stop:"):
            self._session_action(chat_id, user_id, data.split(":", 2)[2], "stop")
        elif data.startswith("session:resume:"):
            self._session_action(chat_id, user_id, data.split(":", 2)[2], "resume")
        elif data.startswith("ans:"):
            self._handle_answer_callback(chat_id, user_id, data)
        elif data.startswith("sel:"):
            self._handle_toggle_callback(chat_id, user_id, data)
        elif data.startswith("done:"):
            self._handle_submit_callback(chat_id, user_id, data.split(":", 1)[1])
        elif data.startswith("text:"):
            self._handle_free_text_callback(chat_id, user_id, thread_id, data.split(":", 1)[1])

    def _session_action(self, chat_id: int, user_id: int, session_id: str, action: str) -> None:
        session = self._owned(self.sessions.get(session_id), chat_id, user_id)
        if session is None:
            return
        thread_id = session.message_thread_id
        provider = self._providers.get(session_id)
        if action == "interrupt":
            ok = provider.interrupt() if provider else False
            self._say(chat_id, "Interrupted." if ok else "Interrupt is not supported for this provider/state.", thread_id)
        elif action == "stop":
            if provider:
                provider.stop()
                self._providers.pop(session_id, None)
            self.sessions.update(session_id, status=STATUS_STOPPED)
            self._forget_session_awaited(session_id)
            self.pending.discard_session(session_id)
            self._publish_index()
            self._say(chat_id, "Session stopped.", thread_id)
        elif action == "resume":
            self._resume_session(chat_id, user_id, session)

    def _resume_session(self, chat_id: int, user_id: int, session: Session) -> None:
        """Real resume: rejoins the provider's own session/thread, keeping
        the same local session record -- never creates a new one.

        The persisted project path is re-authorized here against the config
        as it stands *now*. Workspace roots are host-administered and can be
        narrowed after a session was created (or the project itself moved,
        deleted, or replaced by a symlink pointing elsewhere); a stored path
        is a record of a past decision, not a standing grant.
        """
        if not session.provider_session_id:
            self._say(chat_id, "Cannot resume: no known provider session ID was recorded for this session.", session.message_thread_id)
            return
        provider_cls = PROVIDERS.get(session.provider)
        if provider_cls is None:
            self._say(chat_id, f"Cannot resume: unknown provider '{escape(session.provider)}'.", session.message_thread_id)
            return
        project = self._revalidate_project(session)
        if project is None:
            self.sessions.update(session.id, status=STATUS_ERROR)
            self._say(
                chat_id,
                "Cannot resume: this session's project is no longer inside a configured "
                "workspace root (or no longer exists). Check 'agent-box telegram roots list' "
                "on the host.",
                session.message_thread_id,
            )
            return
        provider = provider_cls(self._context_for(project), on_event=lambda ev, sid=session.id: self._on_provider_event(sid, ev))
        try:
            provider.resume(session.provider_session_id)
        except ProviderError as exc:
            self.sessions.update(session.id, status=STATUS_ERROR)
            self._say(chat_id, f"Resume failed: {escape(str(exc))}", session.message_thread_id)
            return
        self._providers[session.id] = provider
        self.sessions.update(session.id, status=STATUS_RUNNING, host_project=str(project))
        self._set_active(chat_id, user_id, session)
        self._publish_index()
        self._say(chat_id, f"Session {escape(session.id)} resumed.", session.message_thread_id)

    def _revalidate_project(self, session: Session) -> Path | None:
        """Re-resolve and re-authorize a persisted project path.

        Returns the *current* canonical path (symlinks followed as they are
        today, not as they were at creation time) when it is still inside a
        configured root and still a directory; otherwise ``None``.
        """
        roots = self.config.remote_control.workspace_roots
        allowed = resolve_allowed_project(Path(session.host_project), roots)
        if allowed is None or not allowed.is_dir():
            return None
        return allowed

    # -- answering questions -------------------------------------------
    def _handle_answer_callback(self, chat_id: int, user_id: int, data: str) -> None:
        """A single-select option tap: answer immediately."""
        try:
            _, token, idx_str = data.split(":", 2)
        except ValueError:
            return

        def resolve(claimed):
            try:
                return [claimed.options[int(idx_str)]["value"]]
            except (IndexError, ValueError):
                return None

        self._submit_answer(chat_id, user_id, token, resolve)

    def _handle_toggle_callback(self, chat_id: int, user_id: int, data: str) -> None:
        """A multi-select checkbox tap: record it, commit nothing yet."""
        try:
            _, token, idx_str = data.split(":", 2)
            index = int(idx_str)
        except ValueError:
            return
        updated = self.pending.toggle_selection(token, user_id, index)
        if updated is None or updated.chat_id != chat_id:
            return
        if updated.message_id is None:
            return
        try:
            self.client.edit_message_reply_markup(
                chat_id, updated.message_id, self._question_keyboard(updated),
            )
        except TelegramAPIError:
            logger.warning("could not redraw multi-select question buttons", exc_info=True)

    def _handle_submit_callback(self, chat_id: int, user_id: int, token: str) -> None:
        """The explicit submit of a multi-select question."""
        peeked = self.pending.peek(token)
        if peeked is not None and peeked.multi_select and not peeked.selected:
            # Submitting nothing is almost certainly a mis-tap, and an empty
            # answer is a real answer to the agent. Refuse without burning the
            # token so the user can still pick something.
            self._say(chat_id, "Pick at least one option before submitting.")
            return

        def resolve(claimed):
            values = [claimed.options[i]["value"] for i in claimed.selected
                      if 0 <= i < len(claimed.options)]
            return values or None

        self._submit_answer(chat_id, user_id, token, resolve)

    def _handle_free_text_callback(
        self, chat_id: int, user_id: int, thread_id: int | None, token: str,
    ) -> None:
        """"Other...": arm the next plain message as this question's answer."""
        peeked = self.pending.peek(token)
        if peeked is None or peeked.chat_id != chat_id or peeked.user_id != user_id:
            return
        if not peeked.allow_free_text:
            self._say(chat_id, "This question only accepts one of the listed options.", thread_id)
            return
        session = self.sessions.get(peeked.session_id)
        target_thread = session.message_thread_id if session else thread_id
        self._awaiting_text[(chat_id, user_id, target_thread)] = token
        self._say(
            chat_id,
            "Send your answer as a normal message — it goes back as the answer to "
            "that question, not as a new turn.",
            target_thread,
        )

    def _submit_answer(self, chat_id: int, user_id: int, token: str, resolve) -> None:
        """Claim a pending question and deliver ``resolve(claimed)`` to the provider.

        One path for every answer shape (single tap, multi-select submit, free
        text) so the claim/deliver/report guarantees cannot drift apart between
        them.
        """
        claimed = self.pending.claim(token, user_id)
        if claimed is None:
            self._say(chat_id, "This question is no longer valid.")
            return
        if claimed.chat_id != chat_id:
            self.pending.mark_failed(token)
            return
        session = self._owned(self.sessions.get(claimed.session_id), chat_id, user_id)
        provider = self._providers.get(claimed.session_id) if session else None
        thread_id = session.message_thread_id if session else None
        answers = resolve(claimed)
        if not answers:
            self.pending.mark_failed(token)
            return
        if session is None or provider is None or not provider.is_alive():
            self.pending.mark_failed(token)
            self._say(chat_id, "That session is no longer active; the answer was not delivered.", thread_id)
            return

        try:
            result = provider.answer_question(claimed.question_id, answers)
        except Exception as exc:  # provider/transport failure, not a refusal
            self.pending.mark_failed(token)
            logger.warning("answer delivery raised", exc_info=True)
            self._say(
                chat_id,
                "The answer could not be delivered to the agent "
                f"({escape(str(exc) or exc.__class__.__name__)}). It was not marked as answered.",
                thread_id,
            )
            return

        if result.status is AnswerStatus.REJECTED:
            self.pending.mark_failed(token)
            self._say(
                chat_id,
                "The agent did not accept that answer (the question may have already been "
                "resolved another way). It was not marked as answered.",
                thread_id,
            )
            return
        if result.status is AnswerStatus.FAILED:
            self.pending.mark_failed(token)
            self._say(
                chat_id,
                "The answer could not be delivered to the agent"
                + (f" ({escape(result.detail)})" if result.detail else "")
                + ". It was not marked as answered.",
                thread_id,
            )
            return

        self._forget_awaited({token})
        self._clear_buttons(chat_id, claimed.message_id)
        if result.status is AnswerStatus.RECORDED:
            # Truthful wording: the agent has been told nothing yet.
            self.pending.mark_recorded(token)
            remaining = len(result.outstanding)
            noun = "question" if remaining == 1 else "questions"
            self._say(
                chat_id,
                f"Recorded. Waiting for {remaining} more {noun} in the same request "
                "before the agent receives any of the answers.",
                thread_id,
            )
            return

        self.pending.mark_delivered(token)
        for promoted in self.pending.promote_recorded(session.id, result.resolved):
            self._clear_buttons(chat_id, promoted.message_id)
        self.sessions.update(session.id, status=STATUS_RUNNING)

    def _clear_buttons(self, chat_id: int, message_id: int | None) -> None:
        if message_id is None:
            return
        try:
            self.client.edit_message_reply_markup(chat_id, message_id, None)
        except TelegramAPIError:
            logger.warning("could not clear answered question buttons", exc_info=True)
