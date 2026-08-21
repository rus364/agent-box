"""Outbound Claude/Codex Telegram notifications.

This is the transport the Telegram remote-control subsystem also reuses
(``agent_box.telegram.api``) so there is exactly one piece of code that talks
to the Bot API for sending messages.
"""
from __future__ import annotations

import html
import json
import os
import stat
import sys
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

from .atomicio import atomic_write_json, read_json
from .config import load_telegram_config
from .paths import NOTIFY_STATE_ROOT
from .telegram.api import TelegramAPIError, TelegramClient

NOTIFY_STATE_TTL = 1800
# PATH-resolved so the same hook command works both on the host (when a user
# runs `agent-box notify status` directly) and inside the container, where
# `agent-box` is mounted onto PATH rather than living at a host-specific path.
NOTIFY_COMMAND = "agent-box notify claude"
# Hook command written by versions that hardcoded the launcher's install path.
# Recognized so `notify setup`/`notify disable` clean up (rather than
# duplicate) hooks installed by an older agent-box.
LEGACY_NOTIFY_COMMANDS = ("~/.local/bin/agent-box notify claude",)


def notify_project(cwd: str) -> str:
    if not cwd or cwd == "/":
        return "unknown"
    return Path(cwd).name or "unknown"


def parse_payload(payload: str) -> dict[str, Any]:
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def telegram_notice(icon: str, agent: str, project: str, status: str, detail: str = "") -> str:
    lines = [f"{icon} <b>{html.escape(agent)} · {html.escape(project)}</b>", html.escape(status)]
    if detail.strip():
        lines.append(html.escape(detail.strip()))
    return "\n".join(lines)


def telegram_send(text: str, reply_markup: dict[str, Any] | None = None) -> bool:
    config = load_telegram_config()
    if config is None or not config.configured or not config.notify_chat_id:
        return True
    try:
        client = TelegramClient(config.token)
        client.send_message(config.notify_chat_id, text, reply_markup=reply_markup)
        return True
    except TelegramAPIError:
        return False


def notify_session_path(session_id: str, root: Path = NOTIFY_STATE_ROOT) -> Path | None:
    if not session_id:
        return None
    return root / f"{urllib.parse.quote(session_id, safe='')}.json"


def claude_background_tasks(obj: dict[str, Any]) -> list[Any] | None:
    if "background_tasks" not in obj:
        return None
    tasks = obj.get("background_tasks")
    return tasks if isinstance(tasks, list) else None


def cache_fresh(path: Path, ttl: int) -> bool:
    import time
    try:
        st = path.stat()
        return st.st_size > 0 and time.time() - st.st_mtime < ttl
    except OSError:
        return False


def update_claude_background_state(session_id: str, tasks: list[Any] | None, root: Path = NOTIFY_STATE_ROOT) -> None:
    path = notify_session_path(session_id, root)
    if path is None or tasks is None:
        return
    if tasks:
        atomic_write_json(path, {"count": len(tasks)}, stat.S_IRUSR | stat.S_IWUSR)
    else:
        path.unlink(missing_ok=True)


def claude_background_active(session_id: str, root: Path = NOTIFY_STATE_ROOT) -> bool:
    path = notify_session_path(session_id, root)
    if path is None:
        return False
    if not cache_fresh(path, NOTIFY_STATE_TTL):
        path.unlink(missing_ok=True)
        return False
    try:
        state = read_json(path)
        return isinstance(state, dict) and int(state.get("count", 0)) > 0
    except (OSError, ValueError, TypeError):
        path.unlink(missing_ok=True)
        return False


def _session_hint(provider: str, cwd: str) -> str:
    """Best-effort pointer to a registered remote-control session, if any."""
    try:
        from .telegram.sessions import SessionRegistry
    except Exception:
        return ""
    try:
        registry = SessionRegistry()
        session = registry.find_by_project(provider, cwd)
    except Exception:
        return ""
    return f"/sessions {session.id}" if session else ""


def notify_claude() -> None:
    obj = parse_payload(sys.stdin.read())
    event = str(obj.get("hook_event_name") or "")
    kind = str(obj.get("notification_type") or "")
    session_id = str(obj.get("session_id") or "")
    cwd = str(obj.get("cwd") or "")
    project = notify_project(cwd)
    message = str(obj.get("message") or obj.get("error") or "")
    tasks = claude_background_tasks(obj) if event == "Stop" else None

    if event == "Stop":
        if tasks is None:
            # Older/unusual payloads do not expose task state. Clear any stale
            # marker and keep the legacy notification behavior rather than risk
            # suppressing a real prompt indefinitely.
            update_claude_background_state(session_id, [])
        else:
            update_claude_background_state(session_id, tasks)
    elif event == "StopFailure":
        update_claude_background_state(session_id, [])

    hint = _session_hint("claude", cwd)

    if (event, kind) == ("Notification", "permission_prompt"):
        telegram_send(telegram_notice(
            "🔐", "Claude", project, "Permission required",
            (message or "Claude is waiting for approval.") + (f"\n{hint}" if hint else ""),
        ))
    elif (event, kind) == ("Notification", "idle_prompt"):
        if claude_background_active(session_id):
            return
        telegram_send(telegram_notice(
            "💬", "Claude", project, "Waiting for input",
            (message or "Claude is waiting for your input.") + (f"\n{hint}" if hint else ""),
        ))
    elif event == "Stop":
        if tasks:
            count = len(tasks)
            label = "task" if count == 1 else "tasks"
            telegram_send(telegram_notice(
                "⚙️", "Claude", project, f"Background work continues · {count} {label}"
            ))
        else:
            telegram_send(telegram_notice("✅", "Claude", project, "Turn finished"))
    elif event == "StopFailure":
        telegram_send(telegram_notice(
            "❌", "Claude", project, "Stopped with an error",
            message or "Claude stopped because of an API error.",
        ))


def notify_codex(payload: str | None) -> None:
    obj = parse_payload(payload if payload is not None else sys.stdin.read())
    if obj.get("type") != "agent-turn-complete":
        return
    cwd = str(obj.get("cwd") or os.getcwd())
    project = notify_project(cwd)
    detail = str(obj.get("last-assistant-message") or "Turn completed.")
    hint = _session_hint("codex", cwd)
    telegram_send(telegram_notice("✅", "Codex", project, "Turn finished", detail + (f"\n{hint}" if hint else "")))


def hook_entry(matcher: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"hooks": [{"type": "command", "command": NOTIFY_COMMAND}]}
    if matcher:
        item["matcher"] = matcher
    return item


def install_claude_notify_hooks(settings_path: Path) -> None:
    from .errors import AgentBoxError
    settings = read_json(settings_path) if settings_path.exists() else {}
    if not isinstance(settings, dict):
        raise AgentBoxError(f"{settings_path} must contain a JSON object")
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise AgentBoxError(f"{settings_path}: hooks must be a JSON object")
    for event, items in list(hooks.items()):
        if isinstance(items, list):
            hooks[event] = [x for x in items if not _uses_legacy_notify(x)]
    for event, entry in (
        ("Notification", hook_entry("permission_prompt")),
        ("Notification", hook_entry("idle_prompt")),
        ("Stop", hook_entry()),
        ("StopFailure", hook_entry()),
    ):
        items = hooks.setdefault(event, [])
        if not isinstance(items, list):
            raise AgentBoxError(f"{settings_path}: hooks.{event} must be a list")
        if entry not in items:
            items.append(entry)
    atomic_write_json(settings_path, settings)


def _uses_notify(entry: Any) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get("hooks"), list) and any(
        isinstance(h, dict) and h.get("type") == "command"
        and h.get("command") in (NOTIFY_COMMAND, *LEGACY_NOTIFY_COMMANDS)
        for h in entry["hooks"]
    )


def _uses_legacy_notify(entry: Any) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get("hooks"), list) and any(
        isinstance(h, dict) and h.get("type") == "command" and h.get("command") in LEGACY_NOTIFY_COMMANDS
        for h in entry["hooks"]
    )


def remove_claude_notify_hooks(settings_path: Path) -> None:
    from .errors import AgentBoxError
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
            remaining = [x for x in items if not _uses_notify(x)]
            if remaining:
                cleaned[event] = remaining
        if cleaned:
            settings["hooks"] = cleaned
        else:
            settings.pop("hooks", None)
    atomic_write_json(settings_path, settings)
