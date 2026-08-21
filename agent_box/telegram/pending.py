"""Pending structured questions asked by a provider, answered via inline buttons.

Callback data must stay short and opaque (Telegram caps it at 64 bytes) and
must never let a Telegram user forge access to another session's question, an
already-answered question, or inject raw agent input through button labels.
This module is the single source of truth for a callback token's validity.

A callback goes through two phases, not one: ``claim`` reserves the token
(the anti-replay lock -- a second callback for the same token can never
succeed, regardless of outcome), and only after the caller has actually
tried delivering the answer to the provider does it call
``mark_delivered``/``mark_failed`` to record what really happened. This
matters because "the button was tapped" and "the provider accepted the
answer" are different facts: reporting success to the user before the
provider confirms would be a lie, but the token still must not become
answerable twice just because delivery failed.
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
from ..paths import TELEGRAM_PENDING_FILE

PENDING_TTL_SECONDS = 3600

STATUS_PENDING = "pending"
STATUS_CLAIMED = "claimed"
STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"


@dataclass
class Option:
    label: str
    value: str


@dataclass
class PendingQuestion:
    token: str
    session_id: str
    chat_id: int
    user_id: int
    question_id: str
    message_id: int | None
    options: list[dict[str, str]] = field(default_factory=list)
    status: str = STATUS_PENDING
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingQuestion":
        known = {f: data.get(f) for f in (
            "token", "session_id", "chat_id", "user_id", "question_id",
            "message_id", "options", "status", "created_at",
        ) if f in data}
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


class PendingRegistry:
    def __init__(self, path: Path = TELEGRAM_PENDING_FILE, ttl: int = PENDING_TTL_SECONDS):
        self.path = path
        self.ttl = ttl

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        try:
            data = read_json(self.path)
        except (OSError, ValueError):
            return []
        items = data.get("pending", []) if isinstance(data, dict) else []
        now = time.time()
        return [i for i in items if now - float(i.get("created_at", 0)) < self.ttl]

    def _save(self, items: list[dict[str, Any]]) -> None:
        atomic_write_json(self.path, {"pending": items}, 0o600)

    def create(
        self, session_id: str, chat_id: int, user_id: int, question_id: str,
        options: list[Option], message_id: int | None = None,
    ) -> PendingQuestion:
        pending = PendingQuestion(
            token=uuid.uuid4().hex[:12],
            session_id=session_id, chat_id=chat_id, user_id=user_id,
            question_id=question_id, message_id=message_id,
            options=[asdict(o) for o in options],
        )
        with _locked(self.path):
            items = self._load()
            items.append(pending.to_dict())
            self._save(items)
        return pending

    def set_message_id(self, token: str, message_id: int) -> None:
        with _locked(self.path):
            items = self._load()
            for item in items:
                if item.get("token") == token:
                    item["message_id"] = message_id
            self._save(items)

    def peek(self, token: str) -> PendingQuestion | None:
        for item in self._load():
            if item.get("token") == token:
                return PendingQuestion.from_dict(item)
        return None

    def claim(self, token: str, user_id: int) -> PendingQuestion | None:
        """Reserve a token for delivery. The anti-replay lock.

        Returns the question only if it exists, is still ``pending``,
        unexpired, and belongs to ``user_id``. A found-but-mismatched-user
        token is immediately burned (marked failed) rather than left
        claimable by anyone; any other outcome (already claimed/delivered/
        failed/unexpired) also fails closed.
        """
        with _locked(self.path):
            items = self._load()
            found = None
            for item in items:
                if item.get("token") == token:
                    found = item
                    break
            if found is None or found.get("status", STATUS_PENDING) != STATUS_PENDING:
                return None
            if int(found.get("user_id", -1)) != int(user_id):
                found["status"] = STATUS_FAILED
                self._save(items)
                return None
            found["status"] = STATUS_CLAIMED
            self._save(items)
            return PendingQuestion.from_dict(found)

    def mark_delivered(self, token: str) -> bool:
        return self._transition(token, expect=STATUS_CLAIMED, to=STATUS_DELIVERED)

    def mark_failed(self, token: str) -> bool:
        return self._transition(token, expect=STATUS_CLAIMED, to=STATUS_FAILED)

    def _transition(self, token: str, *, expect: str, to: str) -> bool:
        with _locked(self.path):
            items = self._load()
            for item in items:
                if item.get("token") == token:
                    if item.get("status") != expect:
                        return False
                    item["status"] = to
                    self._save(items)
                    return True
            return False

    def discard_session(self, session_id: str) -> None:
        with _locked(self.path):
            items = [i for i in self._load() if i.get("session_id") != session_id]
            self._save(items)
