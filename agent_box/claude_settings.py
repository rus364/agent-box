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
    """Append each ``(event, entry)`` pair to ``settings_path`` if an entry
    with the same ``matcher`` doesn't already carry that command as one of
    its handlers. Idempotent, and safe against a *mixed* entry -- one an
    earlier install (or a user) already combined our command with other
    handlers under the same matcher -- which must not gain a second,
    duplicate outer entry for the same command.
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
        matcher = entry.get("matcher")
        commands = {
            h.get("command") for h in entry.get("hooks", [])
            if isinstance(h, dict) and h.get("type") == "command"
        }
        already_present = any(
            isinstance(item, dict) and item.get("matcher") == matcher
            and any(uses_command(item, cmd) for cmd in commands)
            for item in items
        )
        if not already_present:
            items.append(entry)
    atomic_write_json(settings_path, settings)


def remove_hook_commands(settings_path: Path, owned: dict[str, tuple[str, ...]]) -> None:
    """Remove only the *handlers* whose command is in ``owned[event]``, per
    event -- operating inside each outer entry's ``hooks`` array rather than
    dropping the whole entry.

    Claude hook entries can group several handlers (and a caller's own,
    unrelated handler) under one outer matcher/group object; agent-box may
    only ever remove the handler(s) it owns. An outer entry is dropped
    entirely only once filtering leaves its ``hooks`` array empty; every
    other field on a surviving entry (``matcher``, ``timeout``,
    ``statusMessage``, ``once``, or anything else) is preserved unchanged.
    """
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
            remaining_items = []
            for item in items:
                if not commands or not isinstance(item, dict) or not isinstance(item.get("hooks"), list):
                    remaining_items.append(item)
                    continue
                filtered_handlers = [
                    h for h in item["hooks"]
                    if not (isinstance(h, dict) and h.get("type") == "command" and h.get("command") in commands)
                ]
                if not filtered_handlers:
                    continue  # every handler in this entry was ours; drop the entry
                if len(filtered_handlers) != len(item["hooks"]):
                    item = {**item, "hooks": filtered_handlers}
                remaining_items.append(item)
            if remaining_items:
                cleaned[event] = remaining_items
        if cleaned:
            settings["hooks"] = cleaned
        else:
            settings.pop("hooks", None)
    atomic_write_json(settings_path, settings)
