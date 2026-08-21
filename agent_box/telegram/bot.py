"""Telegram remote-control daemon.

Long-polls ``getUpdates`` (no webhook, no public port), renders an
inline-button menu, and routes messages/callbacks to per-session provider
adapters (Claude or Codex). Security-sensitive by design: every command and
callback is checked against the configured user allowlist and, for project
selection, against configured workspace roots — never an arbitrary host path.
"""
from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import TelegramConfig
from .api import TelegramAPIError, TelegramClient
from .pending import Option, PendingRegistry
from .projects import list_projects, resolve_allowed_project
from .providers.base import AgentEvent, EventKind, Provider, ProviderError
from .providers.claude import ClaudeProvider
from .providers.codex import CodexProvider
from .sessions import STATUS_ERROR, STATUS_RUNNING, STATUS_STOPPED, STATUS_WAITING_INPUT, Session, SessionRegistry

logger = logging.getLogger("agent_box.telegram")

PROVIDERS = {"claude": ClaudeProvider, "codex": CodexProvider}
MAX_TELEGRAM_TEXT = 3500


def escape(text: str) -> str:
    return html.escape(text, quote=False)


@dataclass
class WizardState:
    provider: str | None = None
    created_at: float = field(default_factory=time.time)


class TelegramBot:
    def __init__(self, config: TelegramConfig, sessions: SessionRegistry | None = None, pending: PendingRegistry | None = None):
        if not config.remote_control.enabled:
            raise RuntimeError("remote control is not enabled in the Telegram configuration")
        if not config.token:
            raise RuntimeError("Telegram bot token is not configured")
        self.config = config
        self.client = TelegramClient(config.token)
        self.sessions = sessions or SessionRegistry()
        self.pending = pending or PendingRegistry()
        self._providers: dict[str, Provider] = {}
        self._wizards: dict[tuple[int, int], WizardState] = {}
        self._wizard_projects: dict[tuple[int, int], list] = {}

    # -- authorization -------------------------------------------------
    def _is_allowed(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.config.remote_control.allowed_user_ids

    # -- main loop -------------------------------------------------------
    def run(self, stop_after: int | None = None) -> None:
        offset: int | None = None
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
                offset = int(update["update_id"]) + 1
                try:
                    self.handle_update(update)
                except Exception:
                    logger.exception("error handling update")
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
        self._route_free_text(chat_id, user_id, text)

    def _handle_command(self, chat_id: int, user_id: int, text: str, thread_id: int | None) -> None:
        command = text.split()[0].split("@")[0].lower()
        if command == "/start":
            self._send_main_menu(chat_id)
        elif command == "/sessions":
            self._send_sessions_menu(chat_id)
        elif command == "/new":
            self._start_new_session_wizard(chat_id, user_id)
        elif command == "/status":
            self._send_status(chat_id)
        elif command == "/usage":
            self._send_usage(chat_id)
        else:
            self.client.send_message(chat_id, "Unknown command. Try /start.")

    def _route_free_text(self, chat_id: int, user_id: int, text: str) -> None:
        for session in self.sessions.list_for_chat(chat_id):
            if session.status in (STATUS_RUNNING, STATUS_WAITING_INPUT):
                provider = self._providers.get(session.id)
                if provider is not None and provider.is_alive():
                    provider.send_message(text)
                    self.sessions.update(session.id, status=STATUS_RUNNING)
                    return
        self.client.send_message(chat_id, "No active session to send that to. Use /new or /sessions first.")

    # -- menus ----------------------------------------------------------
    def _send_main_menu(self, chat_id: int) -> None:
        keyboard = {"inline_keyboard": [
            [{"text": "Sessions", "callback_data": "menu:sessions"}, {"text": "New session", "callback_data": "menu:new"}],
            [{"text": "Usage", "callback_data": "menu:usage"}, {"text": "Refresh", "callback_data": "menu:main"}],
        ]}
        self.client.send_message(chat_id, "<b>agent-box</b>\nRemote control menu", reply_markup=keyboard)

    def _send_sessions_menu(self, chat_id: int) -> None:
        sessions = self.sessions.list_for_chat(chat_id)
        if not sessions:
            self.client.send_message(chat_id, "No sessions yet. Use /new to create one.")
            return
        rows = []
        for s in sessions:
            label = f"{s.provider} · {Path(s.host_project).name} · {s.status}"
            rows.append([{"text": label, "callback_data": f"session:open:{s.id}"}])
        rows.append([{"text": "New session", "callback_data": "menu:new"}])
        self.client.send_message(chat_id, "<b>Sessions</b>", reply_markup={"inline_keyboard": rows})

    def _send_session_menu(self, chat_id: int, session: Session) -> None:
        rows = [[
            {"text": "Interrupt", "callback_data": f"session:interrupt:{session.id}"},
            {"text": "Stop", "callback_data": f"session:stop:{session.id}"},
        ]]
        if session.status == STATUS_STOPPED:
            rows = [[{"text": "Resume", "callback_data": f"session:resume:{session.id}"}]]
        rows.append([{"text": "Back", "callback_data": "menu:sessions"}])
        text = (
            f"<b>{escape(session.provider)} · {escape(Path(session.host_project).name)}</b>\n"
            f"Status: {escape(session.status)}\n"
            f"Project: {escape(session.host_project)}\n\n"
            "Send a normal message to talk to this session."
        )
        self.client.send_message(chat_id, text, reply_markup={"inline_keyboard": rows})

    def _send_status(self, chat_id: int) -> None:
        sessions = self.sessions.list_for_chat(chat_id)
        running = sum(1 for s in sessions if s.status in (STATUS_RUNNING, STATUS_WAITING_INPUT))
        self.client.send_message(chat_id, f"{len(sessions)} session(s), {running} active.")

    def _send_usage(self, chat_id: int) -> None:
        from .. import usage
        try:
            claude = usage.claude_usage()
        except Exception:
            claude = usage.PLACEHOLDER_USAGE
        try:
            codex = usage.codex_usage()
        except Exception:
            codex = usage.PLACEHOLDER_USAGE
        self.client.send_message(chat_id, f"Claude {escape(claude)} │ Codex {escape(codex)}")

    # -- new-session wizard ------------------------------------------
    def _start_new_session_wizard(self, chat_id: int, user_id: int) -> None:
        self._wizards[(chat_id, user_id)] = WizardState()
        keyboard = {"inline_keyboard": [
            [{"text": "Claude", "callback_data": "new:provider:claude"}, {"text": "Codex", "callback_data": "new:provider:codex"}],
        ]}
        self.client.send_message(chat_id, "New session — choose an agent:", reply_markup=keyboard)

    def _wizard_pick_provider(self, chat_id: int, user_id: int, provider: str) -> None:
        state = self._wizards.setdefault((chat_id, user_id), WizardState())
        state.provider = provider
        roots = self.config.remote_control.workspace_roots
        projects = list_projects(roots)
        if not projects:
            self.client.send_message(chat_id, "No allowed workspace roots are configured. Run `agent-box telegram roots add <path>` on the host.")
            return
        rows = [[{"text": p.label, "callback_data": f"new:project:{idx}"}] for idx, p in enumerate(projects)]
        self._wizard_projects[(chat_id, user_id)] = projects
        self.client.send_message(chat_id, f"Project for {escape(provider)}:", reply_markup={"inline_keyboard": rows})

    def _wizard_pick_project(self, chat_id: int, user_id: int, idx: int) -> None:
        state = self._wizards.get((chat_id, user_id))
        projects = self._wizard_projects.get((chat_id, user_id)) or []
        if state is None or state.provider is None or idx < 0 or idx >= len(projects):
            self.client.send_message(chat_id, "That session setup expired; use /new again.")
            return
        project = projects[idx]
        allowed = resolve_allowed_project(project.path, self.config.remote_control.workspace_roots)
        if allowed is None:
            self.client.send_message(chat_id, "That project is no longer within an allowed workspace root.")
            return
        self._wizards.pop((chat_id, user_id), None)
        self._create_session(chat_id, user_id, state.provider, allowed)

    def _create_session(self, chat_id: int, user_id: int, provider_name: str, project: Path) -> None:
        session = self.sessions.create(provider_name, str(project), chat_id, user_id)
        provider_cls = PROVIDERS[provider_name]
        provider = provider_cls(project, on_event=lambda ev, sid=session.id: self._on_provider_event(sid, ev))
        try:
            provider.start()
        except ProviderError as exc:
            self.sessions.update(session.id, status=STATUS_ERROR)
            self.client.send_message(chat_id, f"Failed to start {escape(provider_name)} session: {escape(str(exc))}")
            return
        self._providers[session.id] = provider
        self.sessions.update(session.id, status=STATUS_RUNNING, provider_session_id=provider.provider_session_id)
        self.client.send_message(chat_id, f"Session created: {escape(provider_name)} · {escape(project.name)}\nSend a message to begin.")

    # -- provider events -> Telegram -----------------------------------
    def _on_provider_event(self, session_id: str, event: AgentEvent) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            return
        if event.kind == EventKind.TEXT and event.text.strip():
            self._send_chunked(session.chat_id, event.text, session.message_thread_id)
        elif event.kind == EventKind.QUESTION and event.question is not None:
            self._send_question(session, event.question)
        elif event.kind == EventKind.TURN_DONE:
            self.sessions.update(session.id, status=STATUS_RUNNING)
        elif event.kind == EventKind.ERROR:
            self.sessions.update(session.id, status=STATUS_ERROR)
            self.client.send_message(session.chat_id, f"⚠️ {escape(event.text)}", message_thread_id=session.message_thread_id)
        elif event.kind == EventKind.STOPPED:
            self.sessions.update(session.id, status=STATUS_STOPPED)

    def _send_chunked(self, chat_id: int, text: str, thread_id: int | None) -> None:
        for i in range(0, len(text), MAX_TELEGRAM_TEXT):
            self.client.send_message(chat_id, escape(text[i:i + MAX_TELEGRAM_TEXT]), message_thread_id=thread_id)

    def _send_question(self, session: Session, question) -> None:
        self.sessions.update(session.id, status=STATUS_WAITING_INPUT)
        options = [Option(o.label, o.label) for o in question.options] or [Option("Other...", "")]
        pending = self.pending.create(session.id, session.chat_id, session.owner_user_id, question.id, options)
        rows = [[{"text": o.label, "callback_data": f"ans:{pending.token}:{i}"}] for i, o in enumerate(options)]
        text = f"<b>{escape(question.header)}</b>\n\n{escape(question.text)}"
        result = self.client.send_message(session.chat_id, text, reply_markup={"inline_keyboard": rows}, message_thread_id=session.message_thread_id)
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is not None:
            self.pending.set_message_id(pending.token, message_id)

    # -- callbacks --------------------------------------------------
    def _handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = callback.get("id", "")
        user_id = (callback.get("from") or {}).get("id")
        chat_id = (callback.get("message") or {}).get("chat", {}).get("id")
        data = callback.get("data") or ""
        if not self._is_allowed(user_id) or chat_id is None:
            self.client.answer_callback_query(callback_id)
            return
        try:
            self._dispatch_callback(chat_id, user_id, data)
        finally:
            self.client.answer_callback_query(callback_id)

    def _dispatch_callback(self, chat_id: int, user_id: int, data: str) -> None:
        if data == "menu:main":
            self._send_main_menu(chat_id)
        elif data == "menu:sessions":
            self._send_sessions_menu(chat_id)
        elif data == "menu:new":
            self._start_new_session_wizard(chat_id, user_id)
        elif data == "menu:usage":
            self._send_usage(chat_id)
        elif data.startswith("new:provider:"):
            self._wizard_pick_provider(chat_id, user_id, data.split(":", 2)[2])
        elif data.startswith("new:project:"):
            self._wizard_pick_project(chat_id, user_id, int(data.split(":", 2)[2]))
        elif data.startswith("session:open:"):
            session = self.sessions.get(data.split(":", 2)[2])
            if session and session.chat_id == chat_id:
                self._send_session_menu(chat_id, session)
        elif data.startswith("session:interrupt:"):
            self._session_action(chat_id, data.split(":", 2)[2], "interrupt")
        elif data.startswith("session:stop:"):
            self._session_action(chat_id, data.split(":", 2)[2], "stop")
        elif data.startswith("session:resume:"):
            self._session_action(chat_id, data.split(":", 2)[2], "resume")
        elif data.startswith("ans:"):
            self._handle_answer_callback(chat_id, user_id, data)

    def _session_action(self, chat_id: int, session_id: str, action: str) -> None:
        session = self.sessions.get(session_id)
        if session is None or session.chat_id != chat_id:
            return
        provider = self._providers.get(session_id)
        if action == "interrupt":
            ok = provider.interrupt() if provider else False
            self.client.send_message(chat_id, "Interrupted." if ok else "Interrupt is not supported for this provider/state.")
        elif action == "stop":
            if provider:
                provider.stop()
                self._providers.pop(session_id, None)
            self.sessions.update(session_id, status=STATUS_STOPPED)
            self.pending.discard_session(session_id)
            self.client.send_message(chat_id, "Session stopped.")
        elif action == "resume":
            project = Path(session.host_project)
            self._create_session(chat_id, session.owner_user_id, session.provider, project)

    def _handle_answer_callback(self, chat_id: int, user_id: int, data: str) -> None:
        _, token, idx_str = data.split(":", 2)
        pending = self.pending.resolve(token, user_id)
        if pending is None:
            self.client.send_message(chat_id, "This question is no longer valid.")
            return
        if pending.chat_id != chat_id:
            return
        session = self.sessions.get(pending.session_id)
        provider = self._providers.get(pending.session_id) if session else None
        if session is None or provider is None or not provider.is_alive():
            self.client.send_message(chat_id, "That session is no longer active.")
            return
        try:
            option = pending.options[int(idx_str)]
        except (IndexError, ValueError):
            return
        provider.answer_question(pending.question_id, [option["value"]])
        self.sessions.update(session.id, status=STATUS_RUNNING)
        if pending.message_id is not None:
            self.client.edit_message_reply_markup(chat_id, pending.message_id, None)
