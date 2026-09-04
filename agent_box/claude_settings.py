"""Everything agent-box manages inside Claude's ``~/.claude/settings.json``.

Two kinds of setting live here. **Hook entries**: ``agent_box.notify``
(outbound notifications) and ``agent_box.flow_push`` (Flow-Next auto-push)
install and remove their own managed commands independently of each other and
without disturbing any hook the user configured by hand, using the generic
"append this entry if missing" / "remove entries using these commands"
mechanics below; each caller still owns its own command strings and entry
shapes. And the **status line**, which is agent-box's own and so is owned
outright by this module.

Everything here reconciles rather than overwrites, and every write goes
through the atomic writer, because this file belongs to Claude Code and to the
user -- agent-box is one of several things with an entry in it.
"""
from __future__ import annotations

import os
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
    *,
    statusline: bool | None = None,
) -> bool:
    """Reconcile everything agent-box manages, with at most one file write.

    The file is parsed once, every removal and addition is made in memory, and
    its atomic writer runs only when the resulting data differs.  An empty
    ``entries`` list is a removal-only operation, so a missing file remains
    untouched.  ``statusline`` is tri-state: ``None`` leaves the status line
    alone entirely, which is what a caller that only owns hooks wants.
    """
    creates_file = bool(entries) or statusline is True
    if not settings_path.exists() and not creates_file:
        return False
    settings = read_json(settings_path) if settings_path.exists() else {}
    if not isinstance(settings, dict):
        raise AgentBoxError(f"{settings_path} must contain a JSON object")
    changed = _remove_hook_commands(settings, owned)
    changed = _install_hook_entries(settings, entries) or changed
    if statusline is not None:
        changed = _apply_statusline(settings, statusline) or changed
    if changed:
        atomic_write_json(settings_path, settings)
    return changed


def install_hook_entries(settings_path: Path, entries: list[tuple[str, dict[str, Any]]]) -> bool:
    """Install managed entries, without rewriting an already-correct file."""
    return reconcile_hook_entries(settings_path, {}, entries)


def remove_hook_commands(settings_path: Path, owned: dict[str, tuple[str, ...]]) -> bool:
    """Remove managed handlers, without rewriting an unchanged file."""
    return reconcile_hook_entries(settings_path, owned, [])


#: The status-line command agent-box installs and, symmetrically, is the only
#: one it will ever remove.
STATUSLINE_COMMAND = "agent-box statusline"

#: ``AGENT_BOX_STATUSLINE=0`` keeps agent-box out of the status line for one
#: invocation.
STATUSLINE_ENV = "AGENT_BOX_STATUSLINE"


def statusline_intent(enabled: bool) -> bool | None:
    """What this launch should do about the status line.

    ``None`` -- leave ``statusLine`` exactly as it is -- rather than ``False``
    when the environment override is set, and that distinction is the whole
    point of the variable. ``settings.json`` is the user's own file, shared
    with Claude sessions that never go through agent-box, so a per-run
    override must be able to mean "do not touch it" without also meaning
    "delete the line I have been using", which is what a one-shot ``False``
    would do and would not undo.

    Turning the setting off in ``agent-box config`` remains the way to remove
    it: that is a persistent decision, deliberately made.
    """
    return None if os.environ.get(STATUSLINE_ENV, "").strip() == "0" else enabled


def statusline_is_ours(settings: dict[str, Any]) -> bool:
    status = settings.get("statusLine")
    return isinstance(status, dict) and status.get("command") == STATUSLINE_COMMAND


def _apply_statusline(settings: dict[str, Any], enabled: bool) -> bool:
    """Apply the status-line rules in memory; report whether anything changed.

    Unlike the hook events above, ``statusLine`` is a single slot rather than a
    list, so installing means occupying it. Two rules keep that honest. A
    status line that is *someone else's* is never touched, whether it was
    hand-written or installed by another tool -- agent-box fills the slot only
    when it is empty or already holds its own command. And disabling removes
    only that same command, so a setting agent-box did not write survives being
    switched off.

    This is deliberately not a migration: no earlier or alternative command
    string is recognized, so an old `agent-box usage all --statusline` counts
    as somebody else's line and is left exactly where it is.
    """
    ours = statusline_is_ours(settings)
    if enabled:
        if ours or "statusLine" in settings:
            return False
        settings["statusLine"] = {"type": "command", "command": STATUSLINE_COMMAND}
        return True
    if not ours:
        return False
    settings.pop("statusLine", None)
    return True


def reconcile_statusline(settings_path: Path, enabled: bool) -> bool:
    """Reconcile only the status line, for a caller that owns nothing else."""
    return reconcile_hook_entries(settings_path, {}, [], statusline=enabled)


def reconcile_claude_settings(
    settings_path: Path, *, notifications: bool, statusline: bool | None,
) -> bool:
    """Everything agent-box owns in ``settings.json``, in one read and write.

    Claude launch and ``agent-box config`` both need the notification hooks
    and the status line brought in line at the same moment. Doing them as two
    separate reconciliations would parse and rewrite the same file twice; this
    composes both plans and hands them to the single writer above.
    """
    from .notify import claude_notify_plan
    owned, entries = claude_notify_plan(notifications)
    return reconcile_hook_entries(
        settings_path, owned, entries, statusline=statusline)
