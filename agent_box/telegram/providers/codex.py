"""Codex provider: drives ``codex app-server`` over its JSON-RPC/stdio protocol.

Protocol details (method names, param/result shapes) were taken from the
schema the installed Codex CLI itself generates
(``codex app-server generate-json-schema``), not from memory of an older
protocol. Handshake: ``initialize`` request, then the required
``initialized`` client notification, *then* ``thread/start``/
``thread/resume`` -- skipping the ``initialized`` notification (as an
earlier revision of this adapter did) is not the documented sequence.

``item/tool/requestUserInput`` is a single JSON-RPC server request that can
carry multiple questions; it must be answered with exactly one JSON-RPC
response containing every question's answer. This adapter tracks each
request's questions independently and only sends the response once all of
them have an answer recorded.

The subprocess runs *inside* the same Podman container as normal
``agent-box codex`` (via ``Provider._spawn``/``runtime.machine_podman_argv``),
not on the bare host.
"""
from __future__ import annotations

import itertools
import json
import threading
from typing import Any

from .base import AgentEvent, EventKind, Provider, ProviderError, Question, QuestionOption, RuntimeContext


class CodexProvider(Provider):
    name = "codex"

    def __init__(self, context: RuntimeContext, on_event=None):
        super().__init__(context, on_event)
        self._id_counter = itertools.count(1)
        self._lock = threading.Lock()
        self._pending_calls: dict[int, threading.Event] = {}
        self._results: dict[int, Any] = {}
        self._errors: dict[int, str] = {}
        self._reader_thread: threading.Thread | None = None
        self._thread_id: str | None = None
        self._current_turn_id: str | None = None
        self._started = False
        # rpc_id -> {"order": [qid,...], "questions": {qid: {"answered": bool, "answers": [...]}}}
        self._pending_requests: dict[Any, dict[str, Any]] = {}
        self._qid_to_rpc: dict[str, Any] = {}

    # -- JSON-RPC plumbing ------------------------------------------------
    def _write(self, obj: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(obj) + "\n")
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

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        obj: dict[str, Any] = {"method": method}
        if params is not None:
            obj["params"] = params
        self._write(obj)

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
        if not self._started:
            tail = self.stderr_tail().strip()
            self._emit(AgentEvent(kind=EventKind.ERROR, text=(
                "codex app-server exited before starting a session"
                + (f": {tail}" if tail else "")
            )))
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
        order: list[str] = []
        qmap: dict[str, dict[str, Any]] = {}
        for q in questions:
            qid = str(q.get("id") or "")
            if not qid:
                continue
            order.append(qid)
            qmap[qid] = {"answered": False, "answers": []}
            self._qid_to_rpc[qid] = rpc_id
        if not order:
            self._respond(rpc_id, {"answers": {}})
            return
        self._pending_requests[rpc_id] = {"order": order, "questions": qmap}
        for q in questions:
            qid = str(q.get("id") or "")
            if qid not in qmap:
                continue
            options = [QuestionOption(o.get("label", ""), o.get("description", "")) for o in (q.get("options") or [])]
            self._emit(AgentEvent(kind=EventKind.QUESTION, question=Question(
                id=qid, header=str(q.get("header") or "Codex"), text=str(q.get("question") or ""),
                options=options, allow_free_text=bool(q.get("isOther", True)),
            )))

    # -- handshake -----------------------------------------------------
    def _handshake(self) -> None:
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        self._call("initialize", {"clientInfo": {"name": "agent-box", "version": "1"}})
        # Required post-initialize notification; must precede thread/start
        # or thread/resume per the app-server protocol's initialization
        # lifecycle (mirrors the LSP-style initialize/initialized handshake).
        self._notify("initialized")

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._spawn(["codex", "app-server"])
        self._handshake()
        result = self._call("thread/start", {
            "cwd": str(self.host_project),
            "sandbox": "workspace-write",
            "approvalPolicy": "never",
        })
        thread = (result or {}).get("thread") or {}
        self._thread_id = str(thread.get("id") or "")
        self.provider_session_id = self._thread_id
        self._started = True
        self._emit(AgentEvent(kind=EventKind.STARTED, text=self._thread_id))

    def resume(self, thread_id: str) -> None:
        """Real resume: rejoins an existing Codex thread via thread/resume.

        Live-verified: a thread that completed at least one turn resumes
        with the identical thread ID and continues its history. A thread
        that was started but never given a turn has no resumable rollout
        yet and thread/resume raises a real ProviderError ("no rollout
        found for thread id ...") -- surfaced as-is rather than papered
        over.
        """
        if not thread_id:
            raise ProviderError("no Codex thread ID to resume")
        self._spawn(["codex", "app-server"])
        self._handshake()
        result = self._call("thread/resume", {"threadId": thread_id, "cwd": str(self.host_project)})
        thread = (result or {}).get("thread") or {}
        self._thread_id = str(thread.get("id") or thread_id)
        self.provider_session_id = self._thread_id
        self._started = True
        self._emit(AgentEvent(kind=EventKind.STARTED, text=self._thread_id))

    def send_message(self, text: str) -> None:
        if not self._thread_id:
            raise ProviderError("codex session not started")
        result = self._call("turn/start", {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text": text}],
        }, timeout=10)
        turn = (result or {}).get("turn") or {}
        self._current_turn_id = str(turn.get("id") or "") or self._current_turn_id

    def answer_question(self, question_id: str, answers: list[str]) -> bool:
        rpc_id = self._qid_to_rpc.get(question_id)
        if rpc_id is None:
            return False
        record = self._pending_requests.get(rpc_id)
        if record is None or question_id not in record["questions"]:
            return False
        if record["questions"][question_id]["answered"]:
            return False  # already answered; do not double-count
        record["questions"][question_id] = {"answered": True, "answers": answers}
        if all(v["answered"] for v in record["questions"].values()):
            payload = {"answers": {qid: {"answers": record["questions"][qid]["answers"]} for qid in record["order"]}}
            self._respond(rpc_id, payload)
            del self._pending_requests[rpc_id]
            for qid in record["order"]:
                self._qid_to_rpc.pop(qid, None)
        return True

    def interrupt(self) -> bool:
        if not self._thread_id or not self._current_turn_id:
            return False
        try:
            self._call("turn/interrupt", {"threadId": self._thread_id, "turnId": self._current_turn_id}, timeout=10)
            return True
        except ProviderError:
            return False
