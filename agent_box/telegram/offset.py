"""Persisted ``getUpdates`` offset so a daemon restart doesn't replay updates.

Telegram's long-polling contract: passing ``offset = last_update_id + 1``
acknowledges every update up to and including that ID as consumed. This
store records that value *after* an update has been fully handled (an
at-least-once model chosen deliberately -- see ``bot.py`` -- rather than
before, so a crash mid-handling replays instead of silently dropping an
update), so a fresh process resumes from where the previous one left off
instead of Telegram redelivering the last 24h of backlog.
"""
from __future__ import annotations

from pathlib import Path

from ..atomicio import atomic_write_json, read_json
from ..paths import TELEGRAM_OFFSET_FILE


class OffsetStore:
    def __init__(self, path: Path = TELEGRAM_OFFSET_FILE):
        self.path = path

    def load(self) -> int | None:
        if not self.path.is_file():
            return None
        try:
            data = read_json(self.path)
        except (OSError, ValueError):
            return None
        value = data.get("offset") if isinstance(data, dict) else None
        return int(value) if isinstance(value, (int, float)) else None

    def save(self, offset: int) -> None:
        atomic_write_json(self.path, {"offset": offset}, 0o600)
