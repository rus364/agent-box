"""Provider abstraction for driving Claude Code / Codex programmatically.

Providers speak each tool's supported machine interface directly:

* Codex uses the ``codex app-server`` JSON-RPC protocol over stdio.
* Claude Code uses ``claude --print --input-format stream-json
  --output-format stream-json``.

Neither adapter scrapes terminal output, drives a TUI, or reads/writes a
provider's private on-disk session files.
"""
from __future__ import annotations

import queue
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable


class EventKind(str, Enum):
    TEXT = "text"          # a chunk of assistant-visible text to relay
    QUESTION = "question"  # a structured question with selectable options
    TURN_DONE = "turn_done"
    STARTED = "started"
    ERROR = "error"
    STOPPED = "stopped"


@dataclass
class QuestionOption:
    label: str
    description: str = ""


@dataclass
class Question:
    id: str
    header: str
    text: str
    options: list[QuestionOption] = field(default_factory=list)
    allow_free_text: bool = True


@dataclass
class AgentEvent:
    kind: EventKind
    text: str = ""
    question: Question | None = None


class ProviderError(RuntimeError):
    pass


class Provider:
    """Base class for a single running agent session.

    Implementations push :class:`AgentEvent` onto ``self.events`` from a
    background reader thread; the Telegram bot drains that queue and turns
    events into chat messages/buttons.
    """

    name = "provider"

    def __init__(self, host_project: Path, on_event: Callable[[AgentEvent], None] | None = None):
        self.host_project = host_project
        self.events: "queue.Queue[AgentEvent]" = queue.Queue()
        self._on_event = on_event
        self.provider_session_id: str | None = None

    def _emit(self, event: AgentEvent) -> None:
        self.events.put(event)
        if self._on_event is not None:
            self._on_event(event)

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        raise NotImplementedError

    def send_message(self, text: str) -> None:
        raise NotImplementedError

    def answer_question(self, question_id: str, answers: list[str]) -> bool:
        """Return True only if the provider confirmed the answer was delivered."""
        raise NotImplementedError

    def interrupt(self) -> bool:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def is_alive(self) -> bool:
        raise NotImplementedError
