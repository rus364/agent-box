"""Codex provider: drives ``codex app-server`` over its JSON-RPC/stdio protocol.

Protocol details (method names, param/result shapes) were taken from the
schema the installed Codex CLI itself generates
(``codex app-server generate-json-schema``), not from memory of an older
protocol. Key methods used here: ``initialize``, ``thread/start``,
``turn/start``, ``turn/interrupt``, and the server-initiated
``item/tool/requestUserInput`` request (Codex's structured
question-with-options mechanism).
"""
from __future__ import annotations

import itertools
import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from .base import AgentEvent, EventKind, Provider, ProviderError, Question, QuestionOption

CODEX_BIN = "codex"


class CodexProvider(Provider):
    name = "codex"

    def __init__(self, host_project: Path, on_event: Callable[[AgentEvent], None] | None = None):
        super().__init__(host_project, on_event)
        self._proc: subprocess.Popen | None = None
        self._id_counter = itertools.count(1)
        self._lock = threading.Lock()
        self._pending_calls: dict[int, threading.Event] = {}
        self._results: dict[int, Any] = {}
        self._errors: dict[int, str] = {}
        self._reader_thread: threading.Thread | None = None
        self._thread_id: str | None = None
        self._current_turn_id: str | None = None
        self._question_items: dict[str, dict[str, Any]] = {}  # question_id -> {itemId, threadId, turnId, rpc_id}

    # -- JSON-RPC plumbing ------------------------------------------------
    def _write(self, obj: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        line = json.dumps(obj) + "\n"
        self._proc.stdin.write(line)
        self._proc.stdin.flush()

    def _call(self, method: str, params: dict[str, Any], timeout: float = 60) -> Any:
        rpc_id = next(self._id_counter)
        event = threading.Event()
        with self._lock:
            self._pending_calls[rpc_id] = event
        self._write({"id": rpc_id, "method": method, "params": params})
        if not event.wait(timeout):
            raise ProviderError(f"codex app-server timed out waiting for {method}")
        with self._lock:
            self._pending_calls.pop(rpc_id, None)
            if rpc_id in self._errors:
                message = self._errors.pop(rpc_id)
                self._results.pop(rpc_id, None)
                raise ProviderError(f"codex app-server {method} failed: {message}")
            return self._results.pop(rpc_id, None)

    def _respond(self, rpc_id: Any, result: Any) -> None:
        self._write({"id": rpc_id, "result": result})

    def _reader_loop(self) -> None:
        assert self._proc and self._proc.stdout
        for raw in self._proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            self._handle_message(obj)
        self._emit(AgentEvent(kind=EventKind.STOPPED))

    def _handle_message(self, obj: dict[str, Any]) -> None:
        if "method" in obj and "id" in obj:
            self._handle_server_request(obj)
        elif "method" in obj:
            self._handle_notification(obj)
        elif "id" in obj:
            self._handle_response(obj)

    def _handle_response(self, obj: dict[str, Any]) -> None:
        rpc_id = obj.get("id")
        with self._lock:
            event = self._pending_calls.get(rpc_id)
            if "error" in obj:
                self._errors[rpc_id] = str((obj.get("error") or {}).get("message", "unknown error"))
            else:
                self._results[rpc_id] = obj.get("result")
        if event is not None:
            event.set()

    def _handle_notification(self, obj: dict[str, Any]) -> None:
        method = obj.get("method", "")
        params = obj.get("params") or {}
        if method == "item/agentMessage/delta":
            delta = str(params.get("delta") or "")
            if delta:
                self._emit(AgentEvent(kind=EventKind.TEXT, text=delta))
        elif method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                pass  # already streamed via delta notifications
        elif method == "turn/completed":
            self._current_turn_id = None
            self._emit(AgentEvent(kind=EventKind.TURN_DONE))
        elif method == "error":
            self._emit(AgentEvent(kind=EventKind.ERROR, text=str(params.get("message") or obj)))

    def _handle_server_request(self, obj: dict[str, Any]) -> None:
        method = obj.get("method", "")
        params = obj.get("params") or {}
        rpc_id = obj.get("id")
        if method == "item/tool/requestUserInput":
            self._handle_request_user_input(rpc_id, params)
        else:
            # Unhandled server request kinds (approvals, elicitations, ...):
            # decline rather than silently approving unknown/privileged actions.
            self._write({"id": rpc_id, "error": {"code": -32601, "message": f"unsupported: {method}"}})

    def _handle_request_user_input(self, rpc_id: Any, params: dict[str, Any]) -> None:
        questions = params.get("questions") or []
        thread_id = str(params.get("threadId") or "")
        turn_id = str(params.get("turnId") or "")
        item_id = str(params.get("itemId") or "")
        for q in questions:
            question_id = str(q.get("id") or "")
            options = [QuestionOption(o.get("label", ""), o.get("description", "")) for o in (q.get("options") or [])]
            self._question_items[question_id] = {"rpc_id": rpc_id, "threadId": thread_id, "turnId": turn_id, "itemId": item_id, "answered": set()}
            self._emit(AgentEvent(kind=EventKind.QUESTION, question=Question(
                id=question_id, header=str(q.get("header") or "Codex"), text=str(q.get("question") or ""),
                options=options, allow_free_text=bool(q.get("isOther", True)),
            )))

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if not shutil.which(CODEX_BIN):
            raise ProviderError("codex CLI not found on PATH")
        self._proc = subprocess.Popen(
            [CODEX_BIN, "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, cwd=str(self.host_project),
        )
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        self._call("initialize", {"clientInfo": {"name": "agent-box", "version": "1"}})
        result = self._call("thread/start", {
            "cwd": str(self.host_project),
            "sandbox": "workspace-write",
            "approvalPolicy": "never",
        })
        thread = (result or {}).get("thread") or {}
        self._thread_id = str(thread.get("id") or "")
        self.provider_session_id = self._thread_id
        self._emit(AgentEvent(kind=EventKind.STARTED, text=self._thread_id))

    def send_message(self, text: str) -> None:
        if not self._thread_id:
            raise ProviderError("codex session not started")
        result = self._call("turn/start", {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text": text}],
        }, timeout=5)
        turn = (result or {}).get("turn") or {}
        self._current_turn_id = str(turn.get("id") or "") or self._current_turn_id

    def answer_question(self, question_id: str, answers: list[str]) -> bool:
        record = self._question_items.pop(question_id, None)
        if record is None:
            return False
        self._respond(record["rpc_id"], {"answers": {question_id: {"answers": answers}}})
        return True

    def interrupt(self) -> bool:
        if not self._thread_id or not self._current_turn_id:
            return False
        try:
            self._call("turn/interrupt", {"threadId": self._thread_id, "turnId": self._current_turn_id}, timeout=10)
            return True
        except ProviderError:
            return False

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
            except OSError:
                pass
            self._proc.terminate()

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None
