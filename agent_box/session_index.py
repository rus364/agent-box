"""A minimal, non-secret projection of the Telegram session registry.

Why this exists: the notification hooks (``agent-box notify claude`` /
``notify codex``) run **inside the container**, and the container has exactly
one Telegram-related host directory bind-mounted into it -- the notification
state directory (``NOTIFY_STATE_ROOT``; see ``runtime.build_mounts``). The
real session registry, the pending-question tokens and the ``getUpdates``
offset all live under ``TELEGRAM_STATE_ROOT``, which is deliberately *not*
mounted. So container-side notification code could never read session state,
and the "open this session in Telegram" pointer it printed was always empty
(and named a command that did not exist).

The fix is not to mount the Telegram state directory: pending-question
callback tokens are anti-replay secrets and the polling offset is
availability-relevant, and neither has any business inside an agent's
container. Instead the daemon publishes this index -- session id, provider,
and project path only. No callback tokens, no offset, no bot token, no chat
or Telegram user IDs.
"""
from __future__ import annotations

import stat
from pathlib import Path
from typing import Any, Iterable

from .atomicio import atomic_write_json, read_json
from .paths import TELEGRAM_SESSION_INDEX_FILE

INDEX_MODE = stat.S_IRUSR | stat.S_IWUSR


def _resolve(path: Path | None) -> Path:
    # Read the module global at call time rather than binding it as a default
    # argument, so the location stays overridable (tests, alternate state
    # roots) after import.
    return TELEGRAM_SESSION_INDEX_FILE if path is None else path


def publish(entries: Iterable[Any], path: Path | None = None) -> None:
    """Write the index from session-like objects (id/provider/host_project).

    Anything else on the objects is ignored on purpose -- the projection is
    allow-listed, not blocklisted, so a future field added to ``Session``
    cannot leak here by accident.
    """
    rows = [
        {
            "id": str(getattr(entry, "id", "")),
            "provider": str(getattr(entry, "provider", "")),
            "host_project": str(getattr(entry, "host_project", "")),
        }
        for entry in entries
    ]
    rows = [row for row in rows if row["id"]]
    path = _resolve(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"sessions": rows}, INDEX_MODE)


def load(path: Path | None = None) -> list[dict[str, str]]:
    path = _resolve(path)
    if not path.is_file():
        return []
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return []
    rows = data.get("sessions", []) if isinstance(data, dict) else []
    return [r for r in rows if isinstance(r, dict) and r.get("id")]


def find(provider: str, host_project: str, path: Path | None = None) -> str | None:
    """Session id for a live session of ``provider`` on ``host_project``."""
    if not host_project:
        return None
    for row in load(path):
        if row.get("provider") == provider and row.get("host_project") == host_project:
            return str(row["id"])
    return None
