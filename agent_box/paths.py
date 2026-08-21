"""Host-side path resolution.

agent-box is meant to run from an arbitrary machine and directory. This module
centralizes every XDG-aware, host-side path so no other module hardcodes a
home layout.
"""
from __future__ import annotations

import os
from pathlib import Path

HOST_HOME = Path.home()


def env_path(name: str, default: Path) -> Path:
    """Read an absolute-path environment variable, falling back safely.

    ``Path(os.environ.get(name, "")).expanduser()`` is a classic trap:
    ``Path("")`` normalizes to ``Path(".")``, which is truthy, so a naive
    ``... or default`` never falls back for an unset/empty variable and
    silently resolves to the current working directory instead. That bug
    previously caused ``agent-box`` to write real state (e.g. synced Claude
    skills) into whatever directory it happened to be invoked from.
    """
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return Path(value).expanduser()


XDG_CONFIG_HOME = env_path("XDG_CONFIG_HOME", HOST_HOME / ".config")
XDG_DATA_HOME = env_path("XDG_DATA_HOME", HOST_HOME / ".local/share")
XDG_CACHE_HOME = env_path("XDG_CACHE_HOME", HOST_HOME / ".cache")

# agent-box's own persistent state (build markers, seeded git/gh config,
# notification state, Telegram remote-control state).
STATE_ROOT = env_path("AGENT_BOX_STATE_DIR", XDG_DATA_HOME / "agent-box")
CONFIG_ROOT = env_path("AGENT_BOX_CONFIG_DIR", XDG_CONFIG_HOME / "agent-box")

BUILD_STATE = STATE_ROOT / "build"
TOOLS_REFRESH_FILE = BUILD_STATE / "tools-refresh"

NOTIFY_CONFIG = CONFIG_ROOT / "telegram.json"
NOTIFY_STATE_ROOT = STATE_ROOT / "notify"

TELEGRAM_CONFIG = CONFIG_ROOT / "telegram.json"
TELEGRAM_STATE_ROOT = STATE_ROOT / "telegram"
TELEGRAM_SESSIONS_FILE = TELEGRAM_STATE_ROOT / "sessions.json"
TELEGRAM_PENDING_FILE = TELEGRAM_STATE_ROOT / "pending.json"
TELEGRAM_OFFSET_FILE = TELEGRAM_STATE_ROOT / "offset.json"

CLAUDE_HOME = env_path("CLAUDE_CONFIG_DIR", HOST_HOME / ".claude")
CODEX_HOME = env_path("CODEX_HOME", HOST_HOME / ".codex")
