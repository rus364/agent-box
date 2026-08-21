"""Claude Code provider: drives ``claude`` over its stream-json protocol.

Uses the officially supported programmatic surface: ``claude --print
--input-format stream-json --output-format stream-json --session-id <uuid>``.
The process is started once per agent-box session and kept running; stdin
accepts one JSON object per line for each new user turn, and stdout emits one
JSON object per line describing assistant text and tool calls (the same
message shape as the Anthropic Messages API).

Important limitation (see README "Claude vs Codex capabilities"): Claude Code
session identity is bound to the process that was started with a given
``--session-id``. There is no supported way to rebind an existing session to
a *new* long-lived stream-json process after the original one exits, so a
crashed/restarted daemon can only continue a Claude session via ``--resume``,
which starts a fresh process. Codex's ``thread/resume`` is more directly
resumable.

AskUserQuestion round-trip: when Claude calls the ``AskUserQuestion`` tool,
this adapter answers by sending a ``tool_result`` message on stdin, which is
the documented mechanism for returning tool output over stream-json input.
This has not been verified against a live Claude session in this environment
(no network/credentials here), so as a safety net every question is also
answerable by plain text: if the user free-texts an answer instead of
tapping a button, it is delivered as an ordinary next user turn rather than
a tool_result, which Claude will still receive as an answer to its question.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from .base import AgentEvent, EventKind, Provider, ProviderError, Question, QuestionOption

CLAUDE_BIN = "claude"


class ClaudeProvider(Provider):
    name = "claude"

    def __init__(self, host_project: Path, on_event: Callable[[AgentEvent], None] | None = None):
        super().__init__(host_project, on_event)
        self._proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._pending_tool_use: dict[str, dict[str, Any]] = {}  # question_id -> {tool_use_id, header, text}

    def _write(self, obj: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(obj) + "\n")
        self._proc.stdin.flush()

    def start(self) -> None:
        if not shutil.which(CLAUDE_BIN):
            raise ProviderError("claude CLI not found on PATH")
        session_id = str(uuid.uuid4())
        self._proc = subprocess.Popen(
            [
                CLAUDE_BIN, "--print", "--input-format", "stream-json", "--output-format", "stream-json",
                "--session-id", session_id, "--permission-mode", "bypassPermissions",
            ],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, cwd=str(self.host_project),
        )
        self.provider_session_id = session_id
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
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
        # stream-json input has no dedicated interrupt RPC; the documented
        # mechanism is closing stdin / terminating the turn is not
        # supported mid-turn without ending the whole process, so this is
        # honestly reported as unsupported rather than faking success.
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
