"""Codex provider: drives ``codex app-server`` over its JSON-RPC/stdio protocol.

Protocol details (method names, param/result shapes) come from the schema the
installed Codex CLI itself generates (``codex app-server generate-json-schema
--out <dir>``), not from memory of an older protocol.

Handshake, in order:

1. ``initialize`` request. Its ``capabilities.experimentalApi`` **must** be
   ``true``: ``item/tool/requestUserInput`` (and its params/response types) is
   marked EXPERIMENTAL in the generated schema, and the capability is
   documented as "Opt into receiving experimental API methods and fields".
   Omitting it -- as an earlier revision of this adapter did -- leaves the
   server free never to send the structured-question request at all, so the
   feature silently does nothing.
2. The required ``initialized`` client notification.
3. ``thread/start`` / ``thread/resume``.

``item/tool/requestUserInput`` is a single JSON-RPC server request that can
carry multiple questions, and must be answered with exactly one JSON-RPC
response containing every question's answer
(``ToolRequestUserInputResponse``: ``{"answers": {<qid>: {"answers": [...]}}}``).
This adapter therefore tracks each request's questions independently and
reports an individual answer as *recorded*, never as *delivered*, until the
aggregate response has actually been written to the server.

Two things that can end a pending request without the user answering it are
handled explicitly, so questions never leak:

* ``serverRequest/resolved`` -- the server telling us a request it sent has
  been resolved elsewhere (another client, a timeout, an interrupt). The
  pending questions are dropped and reported closed.
* a write/provider failure while submitting the aggregate response -- the
  recorded answer is rolled back and the failure reported, so the caller can
  release its own claim on the question instead of leaving it stuck.

The subprocess runs in its own disposable container, built from the same
image and configured by the same runtime policy as normal ``agent-box codex``
(via ``Provider._spawn``/``runtime.machine_podman_argv``), not on the bare
host.
"""
from __future__ import annotations

import itertools
import json
import threading
from typing import Any, ClassVar

from .base import (
    AgentEvent, AnswerResult, AnswerStatus, EventKind, Provider, ProviderError,
    Question, QuestionOption, RuntimeContext,
)


class CodexProvider(Provider):
    name = "codex"

    #: Client capabilities sent with ``initialize``. ``experimentalApi``
    #: gates ``item/tool/requestUserInput``.
    CAPABILITIES: ClassVar[dict[str, Any]] = {"experimentalApi": True}

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
        # Guards the pending-question bookkeeping below. Deliberately separate
        # from ``_lock`` (which serializes RPC call bookkeeping) so answering a
        # question never blocks the reader thread's response dispatch.
        self._questions_lock = threading.Lock()
        # rpc_id -> {"order": [qid,...], "questions": {qid: {"answered": bool, "answers": [...]}}}
        self._pending_requests: dict[Any, dict[str, Any]] = {}
        self._qid_to_rpc: dict[str, Any] = {}

    # -- JSON-RPC plumbing ------------------------------------------------
    def _write(self, obj: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.closed:
            raise ProviderError("codex app-server stdin is not available")
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

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
        # The process is gone: nothing can ever answer an outstanding
        # question, so say so instead of leaving buttons that silently fail.
        self._close_all_pending("the Codex session ended before this question was answered")
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
        elif method == "serverRequest/resolved":
            # ServerRequestResolvedNotification: a request the server sent us
            # has been resolved without (or regardless of) our response.
            self._discard_request(params.get("requestId"), "Codex withdrew this question")
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
            try:
                self._write({"id": rpc_id, "error": {"code": -32601, "message": f"unsupported: {method}"}})
            except ProviderError:
                pass

    def _handle_request_user_input(self, rpc_id: Any, params: dict[str, Any]) -> None:
        questions = params.get("questions") or []
        order: list[str] = []
        qmap: dict[str, dict[str, Any]] = {}
        for q in questions:
            qid = str(q.get("id") or "")
            if not qid or qid in qmap:
                continue
            order.append(qid)
            qmap[qid] = {"answered": False, "answers": []}
        if not order:
            try:
                self._respond(rpc_id, {"answers": {}})
            except ProviderError:
                pass
            return
        with self._questions_lock:
            self._pending_requests[rpc_id] = {"order": order, "questions": qmap}
            for qid in order:
                self._qid_to_rpc[qid] = rpc_id
        for q in questions:
            qid = str(q.get("id") or "")
            if qid not in qmap:
                continue
            options = [QuestionOption(o.get("label", ""), o.get("description", "")) for o in (q.get("options") or [])]
            self._emit(AgentEvent(kind=EventKind.QUESTION, question=Question(
                id=qid, header=str(q.get("header") or "Codex"), text=str(q.get("question") or ""),
                # ToolRequestUserInputQuestion.isOther defaults to false in
                # the generated schema: a Codex question only accepts a
                # free-text answer when it says so.
                options=options, allow_free_text=bool(q.get("isOther", False)),
                # The schema has no multi-select for request_user_input; one
                # answer per question (the response type still takes a list).
                multi_select=False,
            )))

    # -- withdrawal --------------------------------------------------------
    def _discard_request(self, rpc_id: Any, reason: str) -> None:
        with self._questions_lock:
            record = self._pending_requests.pop(rpc_id, None)
            if record is None:
                return
            for qid in record["order"]:
                self._qid_to_rpc.pop(qid, None)
        for qid in record["order"]:
            self._emit(AgentEvent(kind=EventKind.QUESTION_CLOSED, text=qid))
        self._emit(AgentEvent(kind=EventKind.ERROR, text=reason))

    def _close_all_pending(self, reason: str) -> None:
        with self._questions_lock:
            records = list(self._pending_requests.items())
            self._pending_requests.clear()
            self._qid_to_rpc.clear()
        for _rpc_id, record in records:
            for qid in record["order"]:
                self._emit(AgentEvent(kind=EventKind.QUESTION_CLOSED, text=qid))
        if records:
            self._emit(AgentEvent(kind=EventKind.ERROR, text=reason))

    # -- handshake -----------------------------------------------------
    def _handshake(self) -> None:
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        self._call("initialize", {
            "clientInfo": {"name": "agent-box", "version": "1"},
            "capabilities": dict(self.CAPABILITIES),
        })
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

    def answer_question(self, question_id: str, answers: list[str]) -> AnswerResult:
        with self._questions_lock:
            rpc_id = self._qid_to_rpc.get(question_id)
            if rpc_id is None:
                return AnswerResult(AnswerStatus.REJECTED, detail="unknown or already-resolved question")
            record = self._pending_requests.get(rpc_id)
            if record is None or question_id not in record["questions"]:
                return AnswerResult(AnswerStatus.REJECTED, detail="unknown or already-resolved question")
            if record["questions"][question_id]["answered"]:
                return AnswerResult(AnswerStatus.REJECTED, detail="already answered")
            record["questions"][question_id] = {"answered": True, "answers": list(answers)}
            outstanding = [q for q in record["order"] if not record["questions"][q]["answered"]]
            if outstanding:
                # Recorded only. The server has seen nothing yet, so this must
                # not be reported to the user as a delivered answer.
                return AnswerResult(AnswerStatus.RECORDED, outstanding=outstanding)
            payload = {"answers": {
                qid: {"answers": list(record["questions"][qid]["answers"])} for qid in record["order"]
            }}

        # The write happens outside the lock (it is real I/O), but the record
        # is still registered, so a concurrent answer for the same request
        # cannot slip past the "already answered" checks above.
        try:
            self._respond(rpc_id, payload)
        except (ProviderError, OSError, ValueError) as exc:
            # Roll the recorded answer back so the request stays answerable
            # and the caller's own claim can be released rather than left
            # stuck half-way through delivery.
            with self._questions_lock:
                current = self._pending_requests.get(rpc_id)
                if current is not None and question_id in current["questions"]:
                    current["questions"][question_id] = {"answered": False, "answers": []}
            return AnswerResult(AnswerStatus.FAILED, detail=str(exc) or exc.__class__.__name__)

        with self._questions_lock:
            self._pending_requests.pop(rpc_id, None)
            for qid in record["order"]:
                self._qid_to_rpc.pop(qid, None)
        return AnswerResult(AnswerStatus.DELIVERED, resolved=list(record["order"]))

    def interrupt(self) -> bool:
        if not self._thread_id or not self._current_turn_id:
            return False
        try:
            self._call("turn/interrupt", {"threadId": self._thread_id, "turnId": self._current_turn_id}, timeout=10)
            return True
        except ProviderError:
            return False
