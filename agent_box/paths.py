"""Host-side path resolution.

agent-box is meant to run from an arbitrary machine and directory. This module
centralizes every XDG-aware, host-side path so no other module hardcodes a
home layout.
"""
from __future__ import annotations

import os
from pathlib import Path

HOST_HOME = Path.home()


def _xdg(var: str, default: str) -> Path:
    value = os.environ.get(var, "").strip()
    return Path(value) if value else HOST_HOME / default


XDG_CONFIG_HOME = _xdg("XDG_CONFIG_HOME", ".config")
XDG_DATA_HOME = _xdg("XDG_DATA_HOME", ".local/share")
XDG_CACHE_HOME = _xdg("XDG_CACHE_HOME", ".cache")

# agent-box's own persistent state (build markers, seeded git/gh config,
# notification state, Telegram remote-control state).
STATE_ROOT = Path(os.environ.get("AGENT_BOX_STATE_DIR", "")).expanduser() or (XDG_DATA_HOME / "agent-box")
CONFIG_ROOT = Path(os.environ.get("AGENT_BOX_CONFIG_DIR", "")).expanduser() or (XDG_CONFIG_HOME / "agent-box")

BUILD_STATE = STATE_ROOT / "build"
TOOLS_REFRESH_FILE = BUILD_STATE / "tools-refresh"

NOTIFY_CONFIG = CONFIG_ROOT / "telegram.json"
NOTIFY_STATE_ROOT = STATE_ROOT / "notify"

TELEGRAM_CONFIG = CONFIG_ROOT / "telegram.json"
TELEGRAM_STATE_ROOT = STATE_ROOT / "telegram"
TELEGRAM_SESSIONS_FILE = TELEGRAM_STATE_ROOT / "sessions.json"
TELEGRAM_PENDING_FILE = TELEGRAM_STATE_ROOT / "pending.json"
TELEGRAM_OFFSET_FILE = TELEGRAM_STATE_ROOT / "offset.json"

# The neutral in-container identity. Not derived from the host user so the
# image and its mounted state are portable across machines/usernames.
CONTAINER_USER = "agentbox"
CONTAINER_HOME = Path(f"/home/{CONTAINER_USER}")
CONTAINER_WORKSPACE = Path("/workspace")

CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", "")).expanduser() or (HOST_HOME / ".claude")
CODEX_HOME = Path(os.environ.get("CODEX_HOME", "")).expanduser() or (HOST_HOME / ".codex")
