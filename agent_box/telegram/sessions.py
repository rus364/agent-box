"""Persistent registry mapping Telegram chats to agent sessions.

A small JSON file plus an advisory lock (``fcntl.flock``) is sufficient here:
writes are infrequent (session lifecycle events), and the lock only needs to
serialize the bot daemon's own concurrent handlers, not distinct processes.
No secrets are stored in this file.
"""
from __future__ import annotations

import contextlib
import fcntl
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..atomicio import atomic_write_json, read_json
from ..paths import TELEGRAM_SESSIONS_FILE

STATUS_STARTING = "starting"
STATUS_RUNNING = "running"
STATUS_WAITING_INPUT = "waiting_input"
STATUS_STOPPED = "stopped"
STATUS_ERROR = "error"


@dataclass
class Session:
    id: str
    provider: str  # "claude" or "codex"
    host_project: str
    chat_id: int
    owner_user_id: int
    provider_session_id: str | None = None
    message_thread_id: int | None = None
    status: str = STATUS_STARTING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        known = {f: data.get(f) for f in (
            "id", "provider", "host_project", "chat_id", "owner_user_id", "provider_session_id",
            "message_thread_id", "status", "created_at", "updated_at",
        ) if f in data}
        known.setdefault("owner_user_id", known.get("chat_id"))
        return cls(**known)


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class SessionRegistry:
    def __init__(self, path: Path = TELEGRAM_SESSIONS_FILE):
        self.path = path

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        try:
            data = read_json(self.path)
        except (OSError, ValueError):
            return []
        return data.get("sessions", []) if isinstance(data, dict) else []

    def _save(self, sessions: list[dict[str, Any]]) -> None:
        atomic_write_json(self.path, {"sessions": sessions}, 0o600)

    def list(self) -> list[Session]:
        return [Session.from_dict(s) for s in self._load()]

    def list_for_chat(self, chat_id: int) -> list[Session]:
        return [s for s in self.list() if s.chat_id == chat_id]

    def get(self, session_id: str) -> Session | None:
        for s in self.list():
            if s.id == session_id:
                return s
        return None

    def find_by_project(self, provider: str, host_project: str) -> Session | None:
        for s in self.list():
            if s.provider == provider and s.host_project == host_project and s.status != STATUS_STOPPED:
                return s
        return None

    def create(
        self, provider: str, host_project: str, chat_id: int, owner_user_id: int,
        message_thread_id: int | None = None,
    ) -> Session:
        session = Session(
            id=uuid.uuid4().hex[:12],
            provider=provider,
            host_project=host_project,
            chat_id=chat_id,
            owner_user_id=owner_user_id,
            message_thread_id=message_thread_id,
        )
        with _locked(self.path):
            sessions = self._load()
            sessions.append(session.to_dict())
            self._save(sessions)
        return session

    def update(self, session_id: str, **fields: Any) -> Session | None:
        with _locked(self.path):
            sessions = self._load()
            updated = None
            for entry in sessions:
                if entry.get("id") == session_id:
                    entry.update(fields)
                    entry["updated_at"] = time.time()
                    updated = Session.from_dict(entry)
                    break
            if updated is not None:
                self._save(sessions)
            return updated

    def delete(self, session_id: str) -> None:
        with _locked(self.path):
            sessions = [s for s in self._load() if s.get("id") != session_id]
            self._save(sessions)
