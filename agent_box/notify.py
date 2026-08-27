"""Outbound-only Telegram notifications for Claude and Codex.

Deliberately one-directional: this module only sends Telegram messages. There
is no polling, no ``getUpdates``, no callback handling and
no way to drive a session from Telegram -- controlling a live Claude session
from Telegram is the job of Anthropic's official ``telegram`` plugin (see
``agent_box.config.CLAUDE_TELEGRAM_CHANNEL``), and Codex users have the
official ChatGPT remote-control experience. Notifications are a separate,
much smaller feature: "something happened in a container you left running".

Claude delivers events through hooks installed in its ``settings.json``
(``agent-box notify claude``), Codex through its ``notify`` program setting
(``agent-box notify codex``). Both run *inside* the container. The main
config file is never mounted; persistent workspace boxes receive notification
credentials only through the relevant exec environment.
"""
from __future__ import annotations

import html
import json
import os
import re
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
NOTIFY_TOKEN_ENV = "AGENT_BOX_NOTIFY_TOKEN"
NOTIFY_CHAT_ID_ENV = "AGENT_BOX_NOTIFY_CHAT_ID"
OFFICIAL_CHANNEL_NOTIFICATION_TYPES = frozenset({"permission_prompt", "idle_prompt"})
# Bot API limits: sendMessage text is 1-4096 characters after entity parsing;
# sendRichMessage accepts up to 32768 UTF-8 characters in the rich text.
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_RICH_TEXT_LIMIT = 32768

_FENCE_RE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})([^\r\n]*)(?:\r?\n)?$")
_MARKDOWN_PUNCTUATION_RE = re.compile(r"([\\`*_{}\[\]()#+.!|>~-])")


def outbound_suppressed() -> bool:
    return bool(os.environ.get(SUPPRESS_NOTIFY_ENV, "").strip())


def claude_interaction_suppressed(event: str, kind: str = "") -> bool:
    """Whether the official Claude Telegram channel already owns ``event``.

    The channel relays interactive prompts to its paired chat, so agent-box's
    Notification hooks would duplicate those messages.  Stop and StopFailure
    are lifecycle signals, not interactive channel traffic; keep sending them
    so the notification bot reports terminal success/failure reliably.
    """
    return (
        outbound_suppressed()
        and event == "Notification"
        and kind in OFFICIAL_CHANNEL_NOTIFICATION_TYPES
    )


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
    the container (the Claude/Codex hooks). The host fallback uses the local
    file; persistent container sessions use the explicitly forwarded env.
    """
    # Runtime injects these values only into notification-capable exec
    # sessions so they never enter persistent box metadata or a shared file.
    # Same-user peers in the shared PID namespace may still inspect a running
    # process's environment; this is metadata minimisation, not isolation.
    token = os.environ.get(NOTIFY_TOKEN_ENV, "")
    chat_id = os.environ.get(NOTIFY_CHAT_ID_ENV, "")
    if token and chat_id:
        return token, chat_id
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    token, chat_id = str(data.get("token") or ""), str(data.get("chat_id") or "")
    return (token, chat_id) if token and chat_id else None


def _telegram_post(method: str, body: dict[str, Any]) -> bool:
    """POST one Bot API request. Never raises or exposes the bot token."""
    credentials = load_credentials()
    if credentials is None:
        return True
    token, chat_id = credentials
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps({"chat_id": chat_id, **body}).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False
    return bool(isinstance(payload, dict) and payload.get("ok"))


def telegram_send(text: str) -> bool:
    """POST one regular HTML message to the configured chat."""
    return _telegram_post("sendMessage", {
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def _escape_markdown_inline(text: str) -> str:
    """Make trusted context data literal inside a Rich Markdown inline span."""
    return _MARKDOWN_PUNCTUATION_RE.sub(r"\\\1", text.replace("\r", " ").replace("\n", " "))


def _sanitize_markdown_line(line: str) -> str:
    """Neutralize Rich Markdown extensions outside inline code spans.

    Rich Markdown intentionally accepts raw Rich HTML, including interactive
    buttons and media blocks. Model output gets ordinary Markdown semantics,
    but raw tags, media image syntax, and Telegram-specific URLs are rendered
    inert. Dangerous introducers become character references, which CommonMark
    resolves as text without parsing the result again. Existing ampersands are
    encoded first so model-provided references can't synthesize an introducer.
    Code spans are copied byte-for-byte because markup is not active in them.
    """
    result: list[str] = []
    index = 0
    while index < len(line):
        if line[index] == "`":
            end = index + 1
            while end < len(line) and line[end] == "`":
                end += 1
            delimiter = line[index:end]
            backslashes = 0
            before = index - 1
            while before >= 0 and line[before] == "\\":
                backslashes += 1
                before -= 1
            if backslashes % 2:
                result.append(delimiter)
                index = end
                continue
            closing = end
            while True:
                closing = line.find(delimiter, closing)
                if closing < 0:
                    break
                before_is_tick = closing > 0 and line[closing - 1] == "`"
                after = closing + len(delimiter)
                after_is_tick = after < len(line) and line[after] == "`"
                if not before_is_tick and not after_is_tick:
                    break
                closing = after
            if closing >= 0:
                closing += len(delimiter)
                result.append(line[index:closing])
                index = closing
                continue
            result.append(delimiter)
            index = end
            continue
        if line.startswith("![", index):
            result.append("&#33;[")
            index += 2
            continue
        if line[index] == "<":
            result.append("&lt;")
            index += 1
            continue
        if line[index] == "&":
            result.append("&amp;")
            index += 1
            continue
        if line[index:index + 2].lower() == "tg":
            target = index + 2
            while target < len(line) and line[target] == "\\":
                target += 1
            if target < len(line) and line[target] == ":":
                result.append(f"{line[index]}\u200b{line[index + 1]}")
                index += 2
                continue
        result.append(line[index])
        index += 1
    return "".join(result)


def _fence_opening(line: str) -> re.Match[str] | None:
    match = _FENCE_RE.match(line)
    if not match:
        return None
    marker, info = match.groups()
    if marker[0] == "`" and "`" in info:
        return None
    return match


def _fence_closes(line: str, character: str, length: int) -> bool:
    match = _FENCE_RE.match(line)
    return bool(
        match
        and match.group(1)[0] == character
        and len(match.group(1)) >= length
        and not match.group(2).strip()
    )


def sanitize_rich_markdown(markdown: str) -> str:
    """Allow ordinary Markdown while disabling Telegram-specific extensions."""
    result: list[str] = []
    fence_character = ""
    fence_length = 0
    for line in markdown.splitlines(keepends=True):
        if fence_character:
            result.append(line)
            if _fence_closes(line, fence_character, fence_length):
                fence_character = ""
                fence_length = 0
            continue
        match = _fence_opening(line)
        if match:
            marker = match.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
            result.append(line)
            continue
        result.append(_sanitize_markdown_line(line))
    return "".join(result)


def _fence_transition(
    line: str, opening_line: str, closing_marker: str,
) -> tuple[str, str]:
    """Return updated fence reopening/closing strings after one whole line."""
    match = _FENCE_RE.match(line)
    if not match:
        return opening_line, closing_marker
    marker = match.group(1)
    if not opening_line:
        if not _fence_opening(line):
            return "", ""
        return line.rstrip("\r\n"), marker[0] * len(marker)
    if _fence_closes(line, closing_marker[0], len(closing_marker)):
        return "", ""
    return opening_line, closing_marker


def split_rich_markdown(
    markdown: str, *, first_prefix: str = "", limit: int = TELEGRAM_RICH_TEXT_LIMIT,
) -> list[str]:
    """Split Markdown without leaving a fence malformed at a message boundary."""
    if len(first_prefix) > limit:
        raise ValueError("rich message prefix exceeds Telegram limit")
    if not markdown:
        return [first_prefix] if first_prefix else []

    chunks: list[str] = []
    chunk = first_prefix
    base_length = len(first_prefix)
    opening_line = ""
    closing_marker = ""

    def close_and_store() -> None:
        nonlocal chunk, base_length
        if closing_marker:
            if chunk and not chunk.endswith("\n"):
                chunk += "\n"
            chunk += closing_marker
        if chunk:
            chunks.append(chunk)
        chunk = f"{opening_line}\n" if opening_line else ""
        base_length = len(chunk)

    for source_line in markdown.splitlines(keepends=True):
        line = source_line
        is_fence_line = _FENCE_RE.match(line) is not None
        while line:
            next_opening, next_closing = _fence_transition(
                line, opening_line, closing_marker,
            ) if is_fence_line else (opening_line, closing_marker)
            if (
                not opening_line
                and next_opening
                and len(next_opening) + 1 + len(next_closing) >= limit
            ):
                raise ValueError("fenced-code opener leaves no room for content")
            closer_size = (1 if chunk and not (chunk + line).endswith("\n") else 0) + len(next_closing)
            if len(chunk) + len(line) + closer_size <= limit:
                chunk += line
                opening_line, closing_marker = next_opening, next_closing
                line = ""
                continue

            fresh_prefix_size = len(f"{opening_line}\n") if opening_line else 0
            fresh_closer_size = len(closing_marker) + (1 if closing_marker else 0)
            if len(chunk) > base_length and (
                is_fence_line or len(line) + fresh_prefix_size + fresh_closer_size <= limit
            ):
                close_and_store()
                continue

            # A single source line is longer than a message. Fill the remaining
            # capacity; slicing Python text keeps every emitted body valid UTF-8.
            active_closer = closing_marker
            reserve = len(active_closer) + (1 if active_closer else 0)
            capacity = limit - len(chunk) - reserve
            if capacity <= 0:
                raise ValueError("fenced-code wrapper exceeds Telegram limit")
            chunk += line[:capacity]
            line = line[capacity:]
            close_and_store()

    if chunk:
        close_and_store()
    return chunks


def _take_text_chunk(text: str, capacity: int) -> tuple[str, str]:
    if len(text) <= capacity:
        return text, ""
    cut = text.rfind("\n", 0, capacity + 1)
    if cut > 0:
        cut += 1
    else:
        cut = capacity
    return text[:cut], text[cut:]


def telegram_send_plain_report(agent: str, project: str, status: str, detail: str) -> bool:
    """Send a complete escaped report, split for regular-message limits."""
    visible_header = f"✅ {agent} · {project}\n{status}\n"
    html_header = f"✅ <b>{html.escape(agent)} · {html.escape(project)}</b>\n{html.escape(status)}\n"
    remaining = detail
    first = True
    success = True
    while remaining or first:
        capacity = TELEGRAM_TEXT_LIMIT - (len(visible_header) if first else 0)
        piece, remaining = _take_text_chunk(remaining, capacity)
        text = (html_header if first else "") + html.escape(piece)
        success = telegram_send(text) and success
        first = False
    return success


def telegram_send_codex(project: str, detail: str) -> bool:
    """Send Codex Markdown richly, falling back once to complete safe HTML."""
    safe_project = _escape_markdown_inline(project)
    prefix = f"✅ **Codex · {safe_project}**\nTurn finished\n\n"
    markdown = sanitize_rich_markdown(detail)
    try:
        chunks = split_rich_markdown(markdown, first_prefix=prefix)
    except ValueError:
        return telegram_send_plain_report("Codex", project, "Turn finished", detail)
    for chunk in chunks:
        if not _telegram_post("sendRichMessage", {
            "rich_message": {"markdown": chunk},
        }):
            return telegram_send_plain_report("Codex", project, "Turn finished", detail)
    return True


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
    if claude_interaction_suppressed(event, kind):
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
        # A Stop with live background tasks is only a pause in the visible turn,
        # not completion. Keep it silent; a later Stop with no tasks reports the
        # actual terminal completion.
        if not tasks:
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
    detail = str(obj.get("last-assistant-message") or "Turn completed.").strip()
    telegram_send_codex(project, detail)


# -- Claude settings.json hook wiring -------------------------------------

def hook_entry(matcher: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"hooks": [{"type": "command", "command": NOTIFY_COMMAND}]}
    if matcher:
        item["matcher"] = matcher
    return item


def install_claude_notify_hooks(settings_path: Path) -> None:
    from .claude_settings import reconcile_hook_entries
    # The stale command is removed first (a separate, pre-existing owned
    # command from an older agent-box), then the current entries are
    # installed -- both go through the same shared, generic mechanics.
    reconcile_hook_entries(settings_path, {
        "Notification": (STALE_NOTIFY_COMMAND,),
        "Stop": (STALE_NOTIFY_COMMAND,),
        "StopFailure": (STALE_NOTIFY_COMMAND,),
    }, [
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
