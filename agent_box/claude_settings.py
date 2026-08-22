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


def _install_hook_entries(settings: dict[str, Any], entries: list[tuple[str, dict[str, Any]]]) -> bool:
    """Apply managed entries in memory and report whether they changed it."""
    if not entries:
        return False
    changed = False
    hooks = settings.get("hooks")
    if hooks is None:
        hooks = {}
        settings["hooks"] = hooks
        changed = True
    if not isinstance(hooks, dict):
        raise AgentBoxError("hooks must be a JSON object")
    for event, entry in entries:
        items = hooks.get(event)
        if items is None:
            items = []
            hooks[event] = items
            changed = True
        if not isinstance(items, list):
            raise AgentBoxError(f"hooks.{event} must be a list")
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
            changed = True
    return changed


def _remove_hook_commands(settings: dict[str, Any], owned: dict[str, tuple[str, ...]]) -> bool:
    """Remove managed handlers in memory and report whether they changed it.

    Claude hook entries can group several handlers (and a caller's own,
    unrelated handler) under one outer matcher/group object; agent-box may
    only ever remove the handler(s) it owns. An outer entry is dropped
    entirely only once filtering leaves its ``hooks`` array empty; every
    other field on a surviving entry (``matcher``, ``timeout``,
    ``statusMessage``, ``once``, or anything else) is preserved unchanged.
    """
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    cleaned: dict[str, Any] = {}
    for event, items in hooks.items():
        commands = owned.get(event, ())
        if not isinstance(items, list) or not commands:
            cleaned[event] = items
            continue
        remaining_items = []
        event_changed = False
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("hooks"), list):
                remaining_items.append(item)
                continue
            filtered_handlers = [
                h for h in item["hooks"]
                if not (isinstance(h, dict) and h.get("type") == "command" and h.get("command") in commands)
            ]
            if len(filtered_handlers) == len(item["hooks"]):
                remaining_items.append(item)
                continue
            event_changed = True
            if filtered_handlers:
                remaining_items.append({**item, "hooks": filtered_handlers})
        if event_changed:
            changed = True
        if remaining_items or not event_changed:
            cleaned[event] = remaining_items
    if changed:
        if cleaned:
            settings["hooks"] = cleaned
        else:
            settings.pop("hooks", None)
    return changed


def reconcile_hook_entries(
    settings_path: Path,
    owned: dict[str, tuple[str, ...]],
    entries: list[tuple[str, dict[str, Any]]],
) -> bool:
    """Reconcile managed hook handlers with at most one settings-file write.

    The file is parsed once, all removals and additions are made in memory,
    and its atomic writer runs only when the resulting data differs.  An empty
    ``entries`` list is a removal-only operation, so a missing file remains
    untouched.
    """
    if not settings_path.exists() and not entries:
        return False
    settings = read_json(settings_path) if settings_path.exists() else {}
    if not isinstance(settings, dict):
        raise AgentBoxError(f"{settings_path} must contain a JSON object")
    changed = _remove_hook_commands(settings, owned)
    changed = _install_hook_entries(settings, entries) or changed
    if changed:
        atomic_write_json(settings_path, settings)
    return changed


def install_hook_entries(settings_path: Path, entries: list[tuple[str, dict[str, Any]]]) -> bool:
    """Install managed entries, without rewriting an already-correct file."""
    return reconcile_hook_entries(settings_path, {}, entries)


def remove_hook_commands(settings_path: Path, owned: dict[str, tuple[str, ...]]) -> bool:
    """Remove managed handlers, without rewriting an unchanged file."""
    return reconcile_hook_entries(settings_path, owned, [])
