"""Outbound-only Telegram notifications for Claude and Codex.

Deliberately one-directional: this module sends ``sendMessage`` calls and
nothing else. There is no polling, no ``getUpdates``, no callback handling and
no way to drive a session from Telegram -- controlling a live Claude session
from Telegram is the job of Anthropic's official ``telegram`` plugin (see
``agent_box.config.CLAUDE_TELEGRAM_CHANNEL``), and Codex users have the
official ChatGPT remote-control experience. Notifications are a separate,
much smaller feature: "something happened in a container you left running".

Claude delivers events through hooks installed in its ``settings.json``
(``agent-box notify claude``), Codex through its ``notify`` program setting
(``agent-box notify codex``). Both run *inside* the container, which is why a
minimal derived credential file and notification state directory are mounted
only for agents that can use outbound notifications; the main config file is
never mounted.
"""
from __future__ import annotations

import html
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .atomicio import atomic_write_json, read_json
from .paths import NOTIFY_CREDENTIALS, NOTIFY_STATE_ROOT

NOTIFY_STATE_TTL = 1800
#: PATH-resolved so the same hook command works on the host and inside the
#: container, where `agent-box` is mounted onto PATH rather than living at a
#: host-specific path.
NOTIFY_COMMAND = "agent-box notify claude"
#: The one hook command older agent-box versions wrote, back when the launcher
#: was referenced by its install path instead of resolved on PATH. Removed
#: when the current hooks are installed -- not as a compatibility subsystem,
#: but because leaving it in place means every Claude event fires the hook
#: twice and posts two identical Telegram messages.
STALE_NOTIFY_COMMAND = "~/.local/bin/agent-box notify claude"
#: Set by ``agent_box.runtime`` on containers whose Claude session already
#: talks to Telegram through the official channel plugin. Those sessions relay
#: interactive replies, prompts and questions to the paired chat, so only the
#: matching ``Notification`` hooks need to stand down.
SUPPRESS_NOTIFY_ENV = "AGENT_BOX_SUPPRESS_NOTIFY"


def outbound_suppressed() -> bool:
    return bool(os.environ.get(SUPPRESS_NOTIFY_ENV, "").strip())


def claude_interaction_suppressed(event: str) -> bool:
    """Whether the official Claude Telegram channel already owns ``event``.

    The channel relays interactive prompts to its paired chat, so agent-box's
    Notification hooks would duplicate those messages.  Stop and StopFailure
    are lifecycle signals, not interactive channel traffic; keep sending them
    so the notification bot reports terminal success/failure reliably.
    """
    return outbound_suppressed() and event == "Notification"


def sync_credentials(config: Any, path: Path = NOTIFY_CREDENTIALS) -> None:
    """Write, or delete, the minimal credential file the hooks read.

    This is the whole reason the container never sees ``config.json``: that
    file holds the bot token unconditionally, so mounting it would put the
    token in every container even with notifications switched off, alongside
    host-integration settings the container has no reason to read. Instead the
    host derives a two-key file and mounts it read-only *only* while
    notifications are actually usable. Turning notifications off removes it,
    so the credential stops being reachable rather than merely being ignored.
    """
    if config.notifications.usable:
        atomic_write_json(path, {
            "token": config.notifications.telegram_token,
            "chat_id": config.notifications.telegram_chat_id,
        }, stat.S_IRUSR | stat.S_IWUSR)
    else:
        path.unlink(missing_ok=True)


def load_credentials(path: Path = NOTIFY_CREDENTIALS) -> tuple[str, str] | None:
    """The bot token and chat id, or None when notifications are not active.

    The same code path runs on the host (``agent-box notify test``) and inside
    the container (the Claude/Codex hooks), so both read exactly the file that
    is mounted -- there is no second, host-only way to reach the token.
    """
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    token, chat_id = str(data.get("token") or ""), str(data.get("chat_id") or "")
    return (token, chat_id) if token and chat_id else None


def telegram_send(text: str) -> bool:
    """POST one HTML message to the configured chat. Never raises, never logs
    the bot token."""
    credentials = load_credentials()
    if credentials is None:
        return True
    token, chat_id = credentials
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False
    return bool(isinstance(payload, dict) and payload.get("ok"))


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


# -- Claude background-task state -----------------------------------------
#
# Claude fires Stop for the visible turn even when background tasks keep
# running, and then an idle_prompt Notification once it is genuinely waiting.
# Recording the task count at Stop lets the idle notification be suppressed
# while work is still in flight, so a long background run does not ping the
# chat as if it needed input.

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


# -- hook entry points ----------------------------------------------------

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
        # Older/unusual payloads do not expose task state. Clear any stale
        # marker rather than risk suppressing a real prompt indefinitely.
        update_claude_background_state(session_id, [] if tasks is None else tasks)
    elif event == "StopFailure":
        update_claude_background_state(session_id, [])

    # The official channel owns prompt/input messages, but not the mechanical
    # lifecycle hooks below.  Decide that at the Claude-event boundary rather
    # than silently dropping every notification in telegram_send(), which is
    # also shared with Codex.
    if claude_interaction_suppressed(event):
        return

    if (event, kind) == ("Notification", "permission_prompt"):
        telegram_send(telegram_notice(
            "🔐", "Claude", project, "Permission required",
            message or "Claude is waiting for approval.",
        ))
    elif (event, kind) == ("Notification", "idle_prompt"):
        if claude_background_active(session_id):
            return
        telegram_send(telegram_notice(
            "💬", "Claude", project, "Waiting for input",
            message or "Claude is waiting for your input.",
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
    project = notify_project(str(obj.get("cwd") or os.getcwd()))
    detail = str(obj.get("last-assistant-message") or "Turn completed.")
    telegram_send(telegram_notice("✅", "Codex", project, "Turn finished", detail))


# -- Claude settings.json hook wiring -------------------------------------

def hook_entry(matcher: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"hooks": [{"type": "command", "command": NOTIFY_COMMAND}]}
    if matcher:
        item["matcher"] = matcher
    return item


def install_claude_notify_hooks(settings_path: Path) -> None:
    from .claude_settings import install_hook_entries, remove_hook_commands
    # The stale command is removed first (a separate, pre-existing owned
    # command from an older agent-box), then the current entries are
    # installed -- both go through the same shared, generic mechanics.
    remove_hook_commands(settings_path, {
        "Notification": (STALE_NOTIFY_COMMAND,),
        "Stop": (STALE_NOTIFY_COMMAND,),
        "StopFailure": (STALE_NOTIFY_COMMAND,),
    })
    install_hook_entries(settings_path, [
        ("Notification", hook_entry("permission_prompt")),
        ("Notification", hook_entry("idle_prompt")),
        ("Stop", hook_entry()),
        ("StopFailure", hook_entry()),
    ])


def remove_claude_notify_hooks(settings_path: Path) -> None:
    from .claude_settings import remove_hook_commands
    owned = (NOTIFY_COMMAND, STALE_NOTIFY_COMMAND)
    remove_hook_commands(settings_path, {
        "Notification": owned, "Stop": owned, "StopFailure": owned,
    })


def reconcile_claude_notify_hooks(settings_path: Path, notifications_enabled: bool) -> None:
    """Synchronize owned Claude notification hooks for a Claude session launch.

    Configuration remains the source of truth, but launch is the ordinary
    lifecycle every existing installation passes through.  Reconciling here
    migrates legacy absolute-path hooks even when a user does not reopen the
    interactive config screen after upgrading.
    """
    if notifications_enabled:
        install_claude_notify_hooks(settings_path)
    else:
        remove_claude_notify_hooks(settings_path)
