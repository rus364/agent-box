"""Claude Code provider: drives ``claude`` over its stream-json protocol,
in a disposable container built from the same image and configured by the same
runtime policy as normal ``agent-box claude``.

Uses the officially supported programmatic surface::

    claude --print --input-format stream-json --output-format stream-json
           --verbose --permission-mode bypassPermissions
           --permission-prompt-tool stdio
           (--session-id <uuid> | --resume <session-id>)

The process is started once per agent-box session and kept running; stdin
accepts one JSON object per line for each new user turn, and stdout emits one
JSON object per line describing assistant text and tool calls.

Structured questions (``AskUserQuestion``), precisely stated
--------------------------------------------------------

``AskUserQuestion`` is a *built-in* tool: the CLI executes it itself, so a
client cannot answer it by sending a ``tool_result`` block on stdin for the
tool_use id. An earlier revision of this adapter did exactly that; it only
appeared to work because the test suite's fake ``claude`` executable was
written to accept it. Against a real Claude Code install it does nothing.

The supported mechanism is the Agent SDK's ``canUseTool`` callback, which the
SDK implements over the CLI's own control protocol -- the SDK launches the CLI
with ``--permission-prompt-tool stdio`` and answers a ``can_use_tool``
``control_request``. ``AskUserQuestion`` declares ``requiresUserInteraction``,
so the CLI routes it to that permission surface with
``requires_user_interaction: true``, and the answer is returned as the tool's
``updatedInput``: the ``AskUserQuestion`` input schema carries an ``answers``
map, described by the CLI itself as "User answers collected by the permission
component", keyed by question text.

This adapter speaks that protocol directly rather than adding the SDK as a
second in-container execution path. The exchange, live-verified against
Claude Code 2.1.239 with real credentials:

    <- {"type":"control_request","request_id":"...","request":{
          "subtype":"can_use_tool","tool_name":"AskUserQuestion",
          "input":{"questions":[...]},"tool_use_id":"toolu_...",
          "requires_user_interaction":true}}
    -> {"type":"control_response","response":{"subtype":"success",
          "request_id":"...","response":{"behavior":"allow",
          "updatedInput":{...,"answers":{"<question text>":"<label>"}}}}}

after which the CLI feeds Claude ``Your questions have been answered:
"<question>"="<label>"`` and the turn continues. A request carrying several
questions is answered with exactly one control_response containing all of
them, so an individual answer is reported as *recorded*, never *delivered*,
until that response is actually written.

Two per-question semantics are carried through rather than flattened away:
``multiSelect`` (several options at once, comma-joined into the single value
the schema's ``answers`` map expects) and the always-available "Other" answer
(free text, delivered as that question's value instead of as a separate user
turn).

``control_cancel_request`` (the CLI withdrawing an in-flight prompt, e.g.
after an interrupt) is handled by closing the affected questions.

Session identity and resume, precisely stated
---------------------------------------------

Claude Code's CLI help documents ``--session-id`` (bind a specific ID to
*this* process) and ``--resume [session-id]`` ("Resume a conversation by
session ID") as separate, mutually exclusive things. There is no supported
way to attach a second, still-running process to an existing session, and
``--fork-session``'s own help text ("create a new session ID instead of
reusing the original, use with --resume") confirms that a plain ``--resume
<id>`` reuses the original session ID by default. So: mid-flight recovery of
a live process is not supported -- but starting a *new* process with
``--resume <session-id>`` is a real, documented way to continue that
session's history, and that is what ``resume()`` does.
"""
from __future__ import annotations

import json
import threading
import uuid
from typing import Any

from .base import (
    AgentEvent, AnswerResult, AnswerStatus, EventKind, Provider, ProviderError,
    Question, QuestionOption, RuntimeContext,
)

#: Value the Agent SDK passes for ``--permission-prompt-tool`` when a
#: ``canUseTool`` callback is configured; it selects the stdio control-protocol
#: permission surface instead of an MCP prompt tool.
STDIO_PERMISSION_SURFACE = "stdio"

ASK_USER_QUESTION = "AskUserQuestion"


class ClaudeProvider(Provider):
    name = "claude"

    def __init__(self, context: RuntimeContext, on_event=None):
        super().__init__(context, on_event)
        self._reader_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # request_id -> {"input": <tool input>, "order": [qid...],
        #                "questions": {qid: {"text": str, "answered": bool, "answer": str}}}
        self._pending_requests: dict[str, dict[str, Any]] = {}
        self._qid_to_request: dict[str, str] = {}
        self._started = False
        self._resumed_id: str | None = None

    def _write(self, obj: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.closed:
            raise ProviderError("claude stdin is not available")
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def _command(self, *, resume: str | None) -> list[str]:
        # The installed Claude Code CLI rejects --print --output-format
        # stream-json without --verbose ("--output-format=stream-json
        # requires --verbose") -- confirmed by actually running it, not
        # assumed from --help text.
        base = ["claude", "--print", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--permission-mode", "bypassPermissions",
                # Enables the can_use_tool control_request surface that
                # AskUserQuestion is routed to (see the module docstring).
                "--permission-prompt-tool", STDIO_PERMISSION_SURFACE]
        if resume:
            return [*base, "--resume", resume]
        return [*base, "--session-id", str(uuid.uuid4())]

    def start(self) -> None:
        command = self._command(resume=None)
        session_id = command[command.index("--session-id") + 1]
        self._spawn(command)
        self.provider_session_id = session_id
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        self._started = True
        self._emit(AgentEvent(kind=EventKind.STARTED, text=session_id))

    def resume(self, session_id: str) -> None:
        """Start a *new* process with ``--resume <session_id>``.

        This is real, documented Claude Code history continuation -- not a
        rebind of a still-running process (see module docstring) -- so it is
        only offered, and only claimed as "resume", for that specific
        guarantee.
        """
        if not session_id:
            raise ProviderError("no Claude session ID to resume")
        command = self._command(resume=session_id)
        self._spawn(command)
        self.provider_session_id = session_id
        self._resumed_id = session_id
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        self._started = True
        self._emit(AgentEvent(kind=EventKind.STARTED, text=session_id))

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
            self._handle_event(obj)
        if not self._started:
            tail = self.stderr_tail().strip()
            self._emit(AgentEvent(kind=EventKind.ERROR, text=(
                "claude exited before starting a session" + (f": {tail}" if tail else "")
            )))
        self._close_all_pending("the Claude session ended before this question was answered")
        self._emit(AgentEvent(kind=EventKind.STOPPED))

    def _handle_event(self, obj: dict[str, Any]) -> None:
        kind = obj.get("type")
        if kind == "assistant":
            for block in ((obj.get("message") or {}).get("content") or []):
                self._handle_content_block(block)
        elif kind == "control_request":
            self._handle_control_request(obj)
        elif kind == "control_cancel_request":
            self._discard_request(str(obj.get("request_id") or ""), "Claude withdrew this question")
        elif kind == "result":
            self._emit(AgentEvent(kind=EventKind.TURN_DONE))
        elif kind == "error":
            self._emit(AgentEvent(kind=EventKind.ERROR, text=str(obj.get("error") or obj)))

    def _handle_content_block(self, block: dict[str, Any]) -> None:
        # Only assistant *text* is relayed. A tool_use block for
        # AskUserQuestion is informational here: the answerable surface is
        # the can_use_tool control_request the CLI sends for it, not this
        # block (which the client cannot fulfil).
        if block.get("type") == "text" and block.get("text"):
            self._emit(AgentEvent(kind=EventKind.TEXT, text=str(block["text"])))

    # -- control protocol --------------------------------------------------
    def _control_response(self, request_id: str, response: dict[str, Any]) -> None:
        self._write({"type": "control_response", "response": {
            "subtype": "success", "request_id": request_id, "response": response,
        }})

    def _handle_control_request(self, obj: dict[str, Any]) -> None:
        request_id = str(obj.get("request_id") or "")
        request = obj.get("request") or {}
        if not request_id:
            return
        if request.get("subtype") != "can_use_tool":
            self._write({"type": "control_response", "response": {
                "subtype": "error", "request_id": request_id,
                "error": f"unsupported control_request subtype: {request.get('subtype')}",
            }})
            return
        if request.get("tool_name") == ASK_USER_QUESTION:
            self._handle_ask_user_question(request_id, request)
            return
        # Any other permission ask: this session runs with
        # --permission-mode bypassPermissions, so a tool reaching the prompt
        # surface is unexpected. Deny explicitly rather than blanket-allowing
        # an action nobody in the Telegram chat was shown.
        self._control_response(request_id, {
            "behavior": "deny",
            "message": (
                f"agent-box: {request.get('tool_name')} asked for interactive permission, "
                "which the Telegram remote-control surface does not grant."
            ),
        })

    def _handle_ask_user_question(self, request_id: str, request: dict[str, Any]) -> None:
        tool_input = request.get("input") or {}
        questions = tool_input.get("questions") or []
        order: list[str] = []
        qmap: dict[str, dict[str, Any]] = {}
        emit: list[Question] = []
        for index, q in enumerate(questions):
            text = str(q.get("question") or "")
            if not text:
                continue
            # Question texts are unique within one AskUserQuestion call (the
            # CLI's own schema enforces it), so index+text is a stable id.
            qid = f"{request_id}:{index}"
            order.append(qid)
            qmap[qid] = {"text": text, "answered": False, "answer": ""}
            options = [QuestionOption(o.get("label", ""), o.get("description", ""))
                       for o in (q.get("options") or [])]
            emit.append(Question(
                id=qid, header=str(q.get("header") or "Claude"), text=text,
                options=options,
                # AskUserQuestion always lets the user answer with something
                # that is not on the list; the CLI's own tool description says
                # so ("Users will always be able to select 'Other'").
                allow_free_text=True,
                multi_select=bool(q.get("multiSelect")),
            ))
        if not order:
            self._control_response(request_id, {"behavior": "allow", "updatedInput": tool_input})
            return
        with self._lock:
            self._pending_requests[request_id] = {"input": tool_input, "order": order, "questions": qmap}
            for qid in order:
                self._qid_to_request[qid] = request_id
        for question in emit:
            self._emit(AgentEvent(kind=EventKind.QUESTION, question=question))

    # -- withdrawal --------------------------------------------------------
    def _discard_request(self, request_id: str, reason: str) -> None:
        with self._lock:
            record = self._pending_requests.pop(request_id, None)
            if record is None:
                return
            for qid in record["order"]:
                self._qid_to_request.pop(qid, None)
        for qid in record["order"]:
            self._emit(AgentEvent(kind=EventKind.QUESTION_CLOSED, text=qid))
        self._emit(AgentEvent(kind=EventKind.ERROR, text=reason))

    def _close_all_pending(self, reason: str) -> None:
        with self._lock:
            records = list(self._pending_requests.values())
            self._pending_requests.clear()
            self._qid_to_request.clear()
        for record in records:
            for qid in record["order"]:
                self._emit(AgentEvent(kind=EventKind.QUESTION_CLOSED, text=qid))
        if records:
            self._emit(AgentEvent(kind=EventKind.ERROR, text=reason))

    # -- turns and answers -------------------------------------------------
    def send_message(self, text: str) -> None:
        self._write({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})

    def answer_question(self, question_id: str, answers: list[str]) -> AnswerResult:
        # The AskUserQuestion schema's `answers` map takes one value per
        # question. Multi-select answers are comma-joined, which is exactly the
        # normalization the CLI applies to an array it receives there.
        answer_text = ", ".join(a for a in answers if a)
        with self._lock:
            request_id = self._qid_to_request.get(question_id)
            if request_id is None:
                return AnswerResult(AnswerStatus.REJECTED, detail="unknown or already-resolved question")
            record = self._pending_requests.get(request_id)
            if record is None or question_id not in record["questions"]:
                return AnswerResult(AnswerStatus.REJECTED, detail="unknown or already-resolved question")
            entry = record["questions"][question_id]
            if entry["answered"]:
                return AnswerResult(AnswerStatus.REJECTED, detail="already answered")
            entry["answered"] = True
            entry["answer"] = answer_text
            outstanding = [q for q in record["order"] if not record["questions"][q]["answered"]]
            if outstanding:
                return AnswerResult(AnswerStatus.RECORDED, outstanding=outstanding)
            updated_input = dict(record["input"])
            updated_input["answers"] = {
                record["questions"][qid]["text"]: record["questions"][qid]["answer"]
                for qid in record["order"]
            }

        try:
            self._control_response(request_id, {"behavior": "allow", "updatedInput": updated_input})
        except (ProviderError, OSError, ValueError) as exc:
            with self._lock:
                current = self._pending_requests.get(request_id)
                if current is not None and question_id in current["questions"]:
                    current["questions"][question_id] = {
                        "text": current["questions"][question_id]["text"], "answered": False, "answer": "",
                    }
            return AnswerResult(AnswerStatus.FAILED, detail=str(exc) or exc.__class__.__name__)

        with self._lock:
            self._pending_requests.pop(request_id, None)
            for qid in record["order"]:
                self._qid_to_request.pop(qid, None)
        return AnswerResult(AnswerStatus.DELIVERED, resolved=list(record["order"]))

    def interrupt(self) -> bool:
        # stream-json input has no documented interrupt RPC; reported
        # honestly as unsupported rather than faking success.
        return False
