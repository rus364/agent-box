"""Host-side path resolution.

agent-box is meant to run from an arbitrary machine and directory. This module
centralizes every XDG-aware, host-side path so no other module hardcodes a
home layout.
"""
from __future__ import annotations

import os
import pwd
from pathlib import Path


def passwd_home(uid: int | None = None) -> Path:
    """This process's real home directory, from the passwd database.

    Deliberately *not* ``Path.home()``/``os.path.expanduser("~")``: those
    consult ``$HOME`` first, so any shell (or a hostile/misconfigured
    environment) can point agent-box's build identity, bind mounts, state
    directories and container ``HOME`` at an arbitrary path. The passwd entry
    is the actual account home and is what ``--userns=keep-id`` maps back to
    on the host, so it is the only definition used here.

    Falls back to ``/`` only when the current UID has no passwd entry or the
    entry reports a non-absolute home -- a conservative, absolute value rather
    than a ``$HOME``-derived guess.
    """
    uid = os.getuid() if uid is None else uid
    try:
        home = Path(pwd.getpwuid(uid).pw_dir or "")
    except KeyError:
        home = Path("")
    return home if home.is_absolute() else Path("/")


# The real, passwd-derived home. ``$HOME`` never overrides this.
HOST_HOME = passwd_home()


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
    return expand_user(value)


def expand_user(value: str) -> Path:
    """``~``-expansion anchored on the passwd home, never on ``$HOME``.

    ``Path.expanduser()`` resolves ``~`` through ``$HOME`` when it is set, so
    a poisoned ``$HOME`` would silently redirect any tilde-using setting
    (``XDG_*``, ``CLAUDE_CONFIG_DIR``, a configured workspace root, ...) away
    from the real account home. This keeps tilde expansion consistent with
    :data:`HOST_HOME`.
    """
    if value == "~":
        return HOST_HOME
    if value.startswith("~/"):
        return HOST_HOME / value[2:]
    return Path(value)


XDG_CONFIG_HOME = env_path("XDG_CONFIG_HOME", HOST_HOME / ".config")
XDG_DATA_HOME = env_path("XDG_DATA_HOME", HOST_HOME / ".local/share")
XDG_CACHE_HOME = env_path("XDG_CACHE_HOME", HOST_HOME / ".cache")

# agent-box's own persistent state (build markers, seeded git/gh config,
# notification state).
STATE_ROOT = env_path("AGENT_BOX_STATE_DIR", XDG_DATA_HOME / "agent-box")
CONFIG_ROOT = env_path("AGENT_BOX_CONFIG_DIR", XDG_CONFIG_HOME / "agent-box")

BUILD_STATE = STATE_ROOT / "build"
TOOLS_REFRESH_FILE = BUILD_STATE / "tools-refresh"

# The one agent-box configuration file (see agent_box.config).
CONFIG_FILE = CONFIG_ROOT / "config.json"
NOTIFY_STATE_ROOT = STATE_ROOT / "notify"
# The only credential the container ever sees for notifications: a generated
# two-key file (bot token + chat id), written solely when notifications are
# usable and mounted read-only. Deliberately NOT the main config file, which
# also describes host-integration switches the container has no business
# reading -- and which would otherwise carry the bot token into every
# container even with notifications turned off. See agent_box.notify.
NOTIFY_CREDENTIALS = STATE_ROOT / "runtime/notify.json"
# Where the container looks for it: agent-box inside the container gets no
# AGENT_BOX_STATE_DIR/XDG_DATA_HOME, so it resolves the default location.
CONTAINER_NOTIFY_CREDENTIALS = HOST_HOME / ".local/share/agent-box/runtime/notify.json"

# Guards the "one live Claude session owns the Telegram channel" invariant
# (see agent_box.channel).
CHANNEL_LOCK = STATE_ROOT / "runtime/telegram-channel.json"

CLAUDE_HOME = env_path("CLAUDE_CONFIG_DIR", HOST_HOME / ".claude")
CODEX_HOME = env_path("CODEX_HOME", HOST_HOME / ".codex")
