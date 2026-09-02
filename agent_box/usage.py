"""Claude/Codex subscription usage reporting (host-side, no container)."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from .atomicio import atomic_write_json, read_json
from .errors import AgentBoxError
from .paths import CLAUDE_HOME, CODEX_HOME, XDG_CACHE_HOME, env_path

PLACEHOLDER_USAGE = "—/— · —/—"
#: Claude Code joins status-line segments nowhere; agent-box owns its own line,
#: so one separator is used for every segment it prints.
STATUSLINE_SEPARATOR = " │ "
#: One muted 256-colour hue per segment, so the three numbers are told apart at
#: a glance without competing with the transcript above them. Claude Code
#: already renders a single-line status dim, which these are chosen to survive.
#: Colour is applied by the combined `usage all` path only: it exists to tell
#: several segments apart, and a single-provider `usage claude` / `usage codex`
#: has nothing to disambiguate, so it prints plain.
CONTEXT_COLOR = "38;5;140"   # muted violet
CLAUDE_COLOR = "38;5;173"    # muted terracotta
CODEX_COLOR = "38;5;110"     # muted steel blue
#: End bold and restore the default foreground, and nothing else. A full reset
#: (``0``) would also clear the dim attribute Claude Code wraps the status line
#: in, making everything after it brighter than the rest of the line.
COLOR_OFF = "\x1b[22;39m"


def color_enabled() -> bool:
    """Whether to emit colour. Honours NO_COLOR, never a terminal check.

    A status line is always a piped command, so an ``isatty`` test would
    suppress colour in exactly the case it is meant for.
    """
    return not os.environ.get("NO_COLOR", "")


def colorize(text: str, color: str) -> str:
    if not text or not color_enabled():
        return text
    return f"\x1b[{color}m{text}{COLOR_OFF}"


def http_json(url: str, *, headers: dict[str, str] | None = None, data: bytes | None = None, timeout: float = 15) -> Any:
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def cache_fresh(path: Path, ttl: int) -> bool:
    try:
        st = path.stat()
        return st.st_size > 0 and time.time() - st.st_mtime < ttl
    except OSError:
        return False


def compact_time(seconds: float) -> str:
    s = max(0, int(seconds))
    if s >= 86400:
        return f"{s // 86400}d{(s % 86400) // 3600}h"
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60}m"
    return f"{s // 60}m"


def format_codex_usage(data: Any) -> str:
    rate = data.get("rate_limit", {}) if isinstance(data, dict) else {}
    windows = [w for w in (rate.get("primary_window"), rate.get("secondary_window")) if isinstance(w, dict)]
    short = next((w for w in windows if w.get("limit_window_seconds") is not None and float(w["limit_window_seconds"]) <= 86400), None)
    week = next((w for w in windows if w.get("limit_window_seconds") is not None and float(w["limit_window_seconds"]) > 86400), None)
    if short is None and week is None:
        raise AgentBoxError("response carried no recognizable rate-limit windows")

    def remaining(w: Any) -> str:
        return "—" if not isinstance(w, dict) or w.get("used_percent") is None else str(int(100 - float(w["used_percent"])))

    def reset(w: Any) -> str:
        return "—" if not isinstance(w, dict) or w.get("reset_after_seconds") is None else compact_time(float(w["reset_after_seconds"]))

    return f"{remaining(short)}/{remaining(week)} · {reset(short)}/{reset(week)}"


def parse_epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def normalize_claude_usage(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"rate_limits": {}}
    if isinstance(data.get("rate_limits"), dict):
        return {"rate_limits": data["rate_limits"]}

    def window(w: Any) -> dict[str, Any] | None:
        if not isinstance(w, dict):
            return None
        return {"used_percentage": w.get("used_percentage", w.get("utilization")), "resets_at": parse_epoch(w.get("resets_at"))}

    return {"rate_limits": {"five_hour": window(data.get("five_hour")), "seven_day": window(data.get("seven_day"))}}


def format_claude_usage(data: Any, now: float | None = None) -> str:
    now = time.time() if now is None else now
    rate = normalize_claude_usage(data).get("rate_limits", {})

    def fmt(w: Any) -> tuple[str, str]:
        if not isinstance(w, dict):
            return "—", "—"
        used = w.get("used_percentage", w.get("utilization"))
        reset_at = parse_epoch(w.get("resets_at"))
        if used is None or reset_at is None or reset_at <= now:
            return "—", "—"
        return str(int(100 - float(used))), compact_time(reset_at - now)

    a, ar = fmt(rate.get("five_hour"))
    b, br = fmt(rate.get("seven_day"))
    return f"{a}/{b} · {ar}/{br}"


def compact_tokens(count: float) -> str:
    n = max(0, int(count))
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{round(n / 1_000)}k"
    return str(n)


def format_context_window(data: Any) -> str:
    """Session context consumption, from a Claude status-line payload.

    Claude Code computes this itself and ships it in the status-line JSON
    (``context_window``), so agent-box neither reads the transcript nor asks
    the API for it -- and, because a status line is a plain shell command whose
    output only reaches the terminal, none of it costs session context.

    Returns an empty segment before the first assistant message of a session,
    when ``used_percentage`` is still null and there is nothing to report.
    """
    window = data.get("context_window") if isinstance(data, dict) else None
    if not isinstance(window, dict):
        return ""
    used = window.get("used_percentage")
    total = window.get("context_window_size")
    tokens = window.get("total_input_tokens")
    if not isinstance(used, (int, float)) or not isinstance(total, (int, float)) or total <= 0:
        return ""
    if not isinstance(tokens, (int, float)):
        return ""
    return f"ctx {int(used)}% · {compact_tokens(tokens)}/{compact_tokens(total)}"


def join_segments(*segments: str) -> str:
    """Join the non-empty status-line segments agent-box has to show."""
    return STATUSLINE_SEPARATOR.join(segment for segment in segments if segment)


def parse_payload(payload: str) -> dict[str, Any]:
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def codex_usage() -> str:
    cache = XDG_CACHE_HOME / "codex/usage.json"
    ttl = int(os.environ.get("CODEX_USAGE_CACHE_TTL", "60"))
    # CODEX_HOME comes from agent_box.paths, which normalizes it once (with
    # passwd-anchored "~" expansion). Re-reading the raw variable here would
    # give a second, subtly different answer than the directory the container
    # actually mounts and the rest of agent-box uses.
    auth = env_path("CODEX_AUTH_PATH", CODEX_HOME / "auth.json")
    url = os.environ.get("CODEX_USAGE_URL", "https://chatgpt.com/backend-api/wham/usage")
    if cache_fresh(cache, ttl):
        return format_codex_usage(read_json(cache))
    if not auth.is_file():
        print(f"agent-box usage codex: authentication file not found: {auth}", file=sys.stderr)
        raise AgentBoxError("Codex authentication unavailable")
    try:
        tokens = read_json(auth)["tokens"]
        token = tokens["access_token"]
        account_id = tokens.get("account_id")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AgentBoxError(f"access token not found in {auth}") from exc
    headers = {"Authorization": f"Bearer {token}"}
    if account_id:
        headers["chatgpt-account-id"] = str(account_id)
    try:
        response = http_json(url, headers=headers)
        atomic_write_json(cache, response)
        return format_codex_usage(response)
    except Exception:
        if cache.is_file() and cache.stat().st_size:
            print("agent-box usage codex: request failed; showing cached usage", file=sys.stderr)
            return format_codex_usage(read_json(cache))
        raise AgentBoxError("Codex usage request failed")


def claude_usage(statusline: bool = False, payload: str | None = None) -> str:
    cache = XDG_CACHE_HOME / "claude-code/rate-limits.json"
    # Same reasoning as codex_usage: CLAUDE_HOME already honors
    # CLAUDE_CONFIG_DIR, so a relocated Claude config directory is picked up
    # here without this function knowing about that variable at all.
    credentials = env_path("CLAUDE_CREDENTIALS_PATH", CLAUDE_HOME / ".credentials.json")
    url = os.environ.get("CLAUDE_CODE_CUSTOM_OAUTH_URL", "https://api.anthropic.com").rstrip("/") + "/api/oauth/usage"
    ttl = int(os.environ.get("CLAUDE_USAGE_CACHE_TTL", "60"))
    if statusline:
        obj = parse_payload(payload if payload is not None else sys.stdin.read())
        if isinstance(obj.get("rate_limits"), dict):
            normalized = normalize_claude_usage(obj)
            atomic_write_json(cache, normalized)
            return format_claude_usage(normalized)
        # A status-line invocation normally receives Claude's payload on stdin.
        # When called manually (or with empty stdin), fall back to the regular
        # cached/OAuth usage path instead of rendering an empty placeholder.
    if cache_fresh(cache, ttl):
        return format_claude_usage(read_json(cache))
    if credentials.is_file():
        try:
            token = read_json(credentials)["claudeAiOauth"]["accessToken"]
            response = http_json(url, headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "Content-Type": "application/json",
                "User-Agent": "claude-cli (external, cli)",
            })
            normalized = normalize_claude_usage(response)
            atomic_write_json(cache, normalized)
            return format_claude_usage(normalized)
        except Exception:
            pass
    else:
        print(f"agent-box usage claude: OAuth credentials not found: {credentials}", file=sys.stderr)
    if cache.is_file() and cache.stat().st_size:
        return format_claude_usage(read_json(cache))
    return PLACEHOLDER_USAGE
