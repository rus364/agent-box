"""Provider abstraction for driving Claude Code / Codex programmatically.

Providers speak each tool's supported machine interface directly:

* Codex uses the ``codex app-server`` JSON-RPC protocol over stdio.
* Claude Code uses ``claude --print --input-format stream-json
  --output-format stream-json``.

Neither adapter scrapes terminal output, drives a TUI, or reads/writes a
provider's private on-disk session files. Each session runs the provider CLI
in a disposable container of its own, built from the same image and
configured by the same authoritative runtime policy as normal ``agent-box
claude``/``codex`` use (``agent_box.runtime.machine_podman_argv``, which goes
through the same ``base_podman_args`` interactive runs do), not on the bare
host -- a machine with Podman, the built image, and no host-installed
``claude``/``codex`` binaries can still be Telegram-remote-controlled.
"""
from __future__ import annotations

import queue
import subprocess
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from ...mounts import MountSpec
from ...workspace import Workspace


class EventKind(str, Enum):
    TEXT = "text"          # a chunk of assistant-visible text to relay
    QUESTION = "question"  # a structured question with selectable options
    # A previously asked question can no longer be answered: the provider
    # withdrew it (Codex ``serverRequest/resolved``, Claude
    # ``control_cancel_request``) or the session died with it outstanding.
    # ``AgentEvent.text`` carries the question id.
    QUESTION_CLOSED = "question_closed"
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
    #: The provider accepts an answer that is not one of ``options`` (Claude's
    #: always-available "Other", Codex's ``isOther``). Surfaced as an
    #: "Other..." button that collects the next plain message as the answer.
    allow_free_text: bool = True
    #: The provider accepts several of ``options`` at once (Claude's
    #: ``multiSelect``). Surfaced as toggles plus an explicit submit.
    multi_select: bool = False


@dataclass
class AgentEvent:
    kind: EventKind
    text: str = ""
    question: Question | None = None


class AnswerStatus(str, Enum):
    """What actually happened to a single answer handed to a provider.

    The distinction that matters: a provider request can carry several
    questions and must be answered with exactly one response containing all
    of them. Until that response is genuinely handed to the provider, an
    individual answer is only *recorded* -- reporting it to the user as
    delivered would be a lie, because the agent has not seen anything yet.
    """
    DELIVERED = "delivered"  # the provider has the complete response
    RECORDED = "recorded"    # accepted, but other questions are outstanding
    REJECTED = "rejected"    # unknown/duplicate/withdrawn question
    FAILED = "failed"        # delivery attempted and failed; nothing recorded


@dataclass
class AnswerResult:
    status: AnswerStatus
    #: Question ids in the same provider request still awaiting an answer.
    outstanding: list[str] = field(default_factory=list)
    #: On DELIVERED, every question id covered by the response just sent --
    #: including siblings answered earlier and only *recorded* until now.
    resolved: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def accepted(self) -> bool:
        """True when the answer was taken (delivered or recorded)."""
        return self.status in (AnswerStatus.DELIVERED, AnswerStatus.RECORDED)

    @property
    def delivered(self) -> bool:
        return self.status is AnswerStatus.DELIVERED


class ProviderError(RuntimeError):
    pass


@dataclass
class RuntimeContext:
    """Everything a provider needs to launch its podman subprocess.

    ``workspace.host_root``/``cwd`` are both the selected project directory
    for a Telegram session (there's no "current subdirectory" concept
    remotely). ``script_dir`` locates the Containerfile/agent-box checkout
    on the host that ``agent_box.runtime`` needs.
    """
    workspace: Workspace
    script_dir: Path
    extra_mounts: list[MountSpec] = field(default_factory=list)

    @property
    def host_project(self) -> Path:
        return self.workspace.host_root


def podman_argv(context: RuntimeContext, command: list[str]) -> list[str]:
    from ...runtime import machine_podman_argv
    return machine_podman_argv(context.workspace, context.extra_mounts, context.script_dir, command)


class Provider:
    """Base class for a single running agent session.

    Implementations push :class:`AgentEvent` onto ``self.events`` from a
    background reader thread; the Telegram bot drains that queue and turns
    events into chat messages/buttons.
    """

    name = "provider"
    STDERR_TAIL_CHARS = 4000

    def __init__(self, context: RuntimeContext, on_event: Callable[[AgentEvent], None] | None = None):
        self.context = context
        self.host_project = context.host_project
        self.events: "queue.Queue[AgentEvent]" = queue.Queue()
        self._on_event = on_event
        self.provider_session_id: str | None = None
        self._proc: subprocess.Popen | None = None
        self._stderr_tail: list[str] = []
        self._stderr_thread: threading.Thread | None = None

    def _emit(self, event: AgentEvent) -> None:
        self.events.put(event)
        if self._on_event is not None:
            self._on_event(event)

    def _spawn(self, command: list[str]) -> subprocess.Popen:
        from ...runtime import image_exists
        if not image_exists():
            raise ProviderError(
                "agent-box image is not built; run 'agent-box build' on the host first"
            )
        from ...mounts import MountError
        try:
            argv = podman_argv(self.context, command)
        except MountError as exc:
            raise ProviderError(str(exc)) from exc
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self._proc = proc
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        return proc

    def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            self._stderr_tail.append(line)
            joined = "".join(self._stderr_tail)
            if len(joined) > self.STDERR_TAIL_CHARS:
                self._stderr_tail = [joined[-self.STDERR_TAIL_CHARS:]]

    def stderr_tail(self) -> str:
        return "".join(self._stderr_tail)[-self.STDERR_TAIL_CHARS:]

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        raise NotImplementedError

    def resume(self, provider_session_id: str) -> None:
        """Rejoin/continue an existing provider session by its own ID.

        Declared here because the Telegram daemon calls it on any provider it
        knows how to restart; what "resume" guarantees differs per provider
        and is documented on each implementation.
        """
        raise NotImplementedError

    def send_message(self, text: str) -> None:
        raise NotImplementedError

    def answer_question(self, question_id: str, answers: list[str]) -> AnswerResult:
        """Record/deliver one answer.

        Never reports :attr:`AnswerStatus.DELIVERED` before the complete
        response for the whole provider request has actually been written to
        the provider.
        """
        raise NotImplementedError

    def interrupt(self) -> bool:
        raise NotImplementedError

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
            except OSError:
                pass
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None
