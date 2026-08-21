"""Claude Code provider: drives ``claude`` over its stream-json protocol,
inside the same Podman container normal ``agent-box claude`` uses.

Uses the officially supported programmatic surface: ``claude --print
--input-format stream-json --output-format stream-json --session-id <uuid>``.
The process is started once per agent-box session and kept running; stdin
accepts one JSON object per line for each new user turn, and stdout emits one
JSON object per line describing assistant text and tool calls (the same
message shape as the Anthropic Messages API).

Session identity and resume, precisely stated:

Claude Code's own CLI help documents ``--session-id`` (bind a specific ID to
*this* process) and ``--resume [session-id]`` ("Resume a conversation by
session ID") as separate, mutually exclusive things. There is no supported
way to attach a second, still-running process to an existing session, and
--fork-session's own help text ("create a new session ID instead of reusing
the original, use with --resume") confirms that a plain ``--resume
<id>`` reuses the original session ID by default. So: mid-flight recovery of
a live process is not supported (a crashed/restarted daemon cannot "rejoin"
a running Claude turn the way Codex's ``thread/resume`` rejoins a running
thread) -- but starting a *new* process with ``--resume <session-id>`` is a
real, documented way to continue that session's history, and that is what
this adapter's ``resume()`` does. It is not the same guarantee Codex gives;
see the README for the honest comparison.

AskUserQuestion round-trip: when Claude calls the ``AskUserQuestion`` tool,
this adapter answers by sending a ``tool_result`` message on stdin, which is
the documented mechanism for returning tool output over stream-json input.
This has not been verified against a live Claude session in this
environment (no network/credentials here) -- only against a fake ``claude``
executable in the test suite that exercises the real subprocess/stdio
wiring. As a safety net every question is also answerable by plain text: if
the user free-texts an answer instead of tapping a button, it is delivered
as an ordinary next user turn rather than a tool_result, which Claude will
still receive as an answer to its question.
"""
from __future__ import annotations

import json
import threading
import uuid
from typing import Any

from .base import AgentEvent, EventKind, Provider, ProviderError, Question, QuestionOption, RuntimeContext


class ClaudeProvider(Provider):
    name = "claude"

    def __init__(self, context: RuntimeContext, on_event=None):
        super().__init__(context, on_event)
        self._reader_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._pending_tool_use: dict[str, dict[str, Any]] = {}  # question_id -> {tool_use_id, header, text}
        self._started = False
        self._resumed_id: str | None = None

    def _write(self, obj: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(obj) + "\n")
        self._proc.stdin.flush()

    def _command(self, *, resume: str | None) -> list[str]:
        # The installed Claude Code CLI rejects --print --output-format
        # stream-json without --verbose ("--output-format=stream-json
        # requires --verbose") -- confirmed by actually running it, not
        # assumed from --help text.
        base = ["claude", "--print", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--permission-mode", "bypassPermissions"]
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
        self._emit(AgentEvent(kind=EventKind.STOPPED))

    def _handle_event(self, obj: dict[str, Any]) -> None:
        kind = obj.get("type")
        if kind == "assistant":
            for block in ((obj.get("message") or {}).get("content") or []):
                self._handle_content_block(block)
        elif kind == "result":
            self._emit(AgentEvent(kind=EventKind.TURN_DONE))
        elif kind == "error":
            self._emit(AgentEvent(kind=EventKind.ERROR, text=str(obj.get("error") or obj)))

    def _handle_content_block(self, block: dict[str, Any]) -> None:
        block_type = block.get("type")
        if block_type == "text" and block.get("text"):
            self._emit(AgentEvent(kind=EventKind.TEXT, text=str(block["text"])))
        elif block_type == "tool_use" and block.get("name") == "AskUserQuestion":
            self._handle_ask_user_question(block)

    def _handle_ask_user_question(self, block: dict[str, Any]) -> None:
        tool_use_id = str(block.get("id") or "")
        questions = ((block.get("input") or {}).get("questions") or [])
        if not questions or not tool_use_id:
            return
        # Multiple sub-questions can appear in one AskUserQuestion call; only
        # the first is surfaced as buttons (documented limitation) so the
        # protocol stays simple. Free-text always remains available.
        first = questions[0]
        question_id = f"{tool_use_id}:0"
        options = [QuestionOption(o.get("label", ""), o.get("description", "")) for o in (first.get("options") or [])]
        with self._lock:
            self._pending_tool_use[question_id] = {
                "tool_use_id": tool_use_id, "header": first.get("header", "Claude"), "text": first.get("question", ""),
            }
        self._emit(AgentEvent(kind=EventKind.QUESTION, question=Question(
            id=question_id, header=str(first.get("header") or "Claude"), text=str(first.get("question") or ""),
            options=options, allow_free_text=True,
        )))

    def send_message(self, text: str) -> None:
        self._write({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})

    def answer_question(self, question_id: str, answers: list[str]) -> bool:
        with self._lock:
            record = self._pending_tool_use.pop(question_id, None)
        if record is None:
            return False
        answer_text = ", ".join(answers) if answers else ""
        self._write({
            "type": "user",
            "message": {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": record["tool_use_id"],
                    "content": answer_text,
                }],
            },
        })
        return True

    def interrupt(self) -> bool:
        # stream-json input has no documented interrupt RPC; reported
        # honestly as unsupported rather than faking success.
        return False
