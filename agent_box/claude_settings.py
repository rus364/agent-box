"""Small shared helpers for managing hook entries in Claude's
``~/.claude/settings.json``.

Both ``agent_box.notify`` (outbound notifications) and ``agent_box.flow_push``
(Flow-Next auto-push) install and remove their own managed hook commands from
this one file, independently of each other and without disturbing any hook
the user configured by hand. This module holds only the generic "append this
entry if missing" / "remove entries using these commands" mechanics; each
caller still owns its own command strings and entry shapes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .atomicio import atomic_write_json, read_json
from .errors import AgentBoxError


def uses_command(entry: Any, command: str) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get("hooks"), list) and any(
        isinstance(h, dict) and h.get("type") == "command" and h.get("command") == command
        for h in entry["hooks"]
    )


def install_hook_entries(settings_path: Path, entries: list[tuple[str, dict[str, Any]]]) -> None:
    """Append each ``(event, entry)`` pair to ``settings_path`` if not already
    present. Idempotent; leaves every other event and unrelated entry alone.
    """
    settings = read_json(settings_path) if settings_path.exists() else {}
    if not isinstance(settings, dict):
        raise AgentBoxError(f"{settings_path} must contain a JSON object")
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise AgentBoxError(f"{settings_path}: hooks must be a JSON object")
    for event, entry in entries:
        items = hooks.setdefault(event, [])
        if not isinstance(items, list):
            raise AgentBoxError(f"{settings_path}: hooks.{event} must be a list")
        if entry not in items:
            items.append(entry)
    atomic_write_json(settings_path, settings)


def remove_hook_commands(settings_path: Path, owned: dict[str, tuple[str, ...]]) -> None:
    """Remove only entries whose command is in ``owned[event]``, per event."""
    if not settings_path.exists():
        return
    settings = read_json(settings_path)
    if not isinstance(settings, dict):
        raise AgentBoxError(f"{settings_path} must contain a JSON object")
    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        cleaned = {}
        for event, items in hooks.items():
            if not isinstance(items, list):
                cleaned[event] = items
                continue
            commands = owned.get(event, ())
            remaining = [x for x in items if not any(uses_command(x, cmd) for cmd in commands)]
            if remaining:
                cleaned[event] = remaining
        if cleaned:
            settings["hooks"] = cleaned
        else:
            settings.pop("hooks", None)
    atomic_write_json(settings_path, settings)
