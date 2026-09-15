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
CONTAINER_LAUNCHER_ROOT = HOST_HOME / ".local/lib/agent-box"


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
XDG_STATE_HOME = env_path("XDG_STATE_HOME", HOST_HOME / ".local/state")
# The login runtime directory comes from the real uid, not $XDG_RUNTIME_DIR,
# for the same reason HOST_HOME ignores $HOME: its sockets become bind mounts.
HOST_RUNTIME_DIR = Path("/run/user") / str(os.getuid())

def launcher_state_root(shared: Path) -> Path:
    """Keep nested builds and workspace records outside their parent's mounts.

    Podman creates missing bind parents as root. Even a writable parent would
    reuse the outer workspace records and read-only policy mount. A namespace
    suffix keeps the local directory stable for this box without aliasing a
    mounted parent launcher directory. Explicit test/user overrides still win.
    """
    if os.environ.get("AGENT_BOX_STATE_DIR", "").strip() or not Path("/run/.containerenv").is_file():
        return shared
    namespace = Path("/proc/self/ns/mnt").stat().st_ino
    return HOST_HOME / ".local/share" / f"agent-box-launcher-{namespace}"


# Notification hooks keep the existing shared mount; launcher state is local
# to the container. Forwarded workflow state retains its explicit path below.
_SHARED_STATE_ROOT = env_path("AGENT_BOX_STATE_DIR", XDG_DATA_HOME / "agent-box")
STATE_ROOT = launcher_state_root(_SHARED_STATE_ROOT)
CONFIG_ROOT = env_path("AGENT_BOX_CONFIG_DIR", XDG_CONFIG_HOME / "agent-box")
HOST_SESSIONS_ROOT = STATE_ROOT / "runtime/host-sessions"
HOST_TMUX_CONFIG = Path(__file__).with_name("tmux.conf")
HOST_SESSION_RUNNER = Path(__file__).with_name("host_session.py")
LOCAL_LAUNCHER = Path(__file__).resolve().parent.parent / "agent-box"
# Unix sockets need a short path; ignore caller-controlled TMPDIR.
SESSION_TEMP_ROOT = Path("/tmp")

BUILD_STATE = STATE_ROOT / "build"
TOOLS_REFRESH_FILE = BUILD_STATE / "tools-refresh"

# The one agent-box configuration file (see agent_box.config).
CONFIG_FILE = CONFIG_ROOT / "config.json"
NOTIFY_STATE_ROOT = _SHARED_STATE_ROOT / "notify"
# The only credential the container ever sees for notifications: a generated
# two-key file (bot token + chat id), written solely when notifications are
# usable and mounted read-only. Deliberately NOT the main config file, which
# also describes host-integration switches the container has no business
# reading -- and which would otherwise carry the bot token into every
# container even with notifications turned off. See agent_box.notify.
NOTIFY_CREDENTIALS = _SHARED_STATE_ROOT / "runtime/notify.json"
# Where the container looks for it: agent-box inside the container gets no
# AGENT_BOX_STATE_DIR/XDG_DATA_HOME, so it resolves the default location.
CONTAINER_NOTIFY_CREDENTIALS = HOST_HOME / ".local/share/agent-box/runtime/notify.json"

# Guards the "one live Claude session owns the Telegram channel" invariant
# (see agent_box.channel).
CHANNEL_LOCK = STATE_ROOT / "runtime/telegram-channel.json"

CLAUDE_HOME = env_path("CLAUDE_CONFIG_DIR", HOST_HOME / ".claude")
CODEX_HOME = env_path("CODEX_HOME", HOST_HOME / ".codex")
# Shared with host and other boxes that mount this same provider home.
CODEX_REMOTE_LOCK = CODEX_HOME / ".agent-box-remote-control.lock"
CODEX_PLUGIN_CACHE = CODEX_HOME / "plugins/cache"

# The shared agent tree. ``skills/`` under it is the canonical location both
# Claude and Codex read, and the place agent-box installs the skills it ships
# (see runtime.install_managed_skills).
AGENTS_DIR = env_path("AGENT_BOX_AGENTS_DIR", HOST_HOME / ".agents")
AGENT_SKILLS_DIR = AGENTS_DIR / "skills"

# The skills agent-box ships and installs itself, inside the package.
MANAGED_SKILLS_SOURCE = Path(__file__).resolve().parent / "skills"

# Workflow scratch, routing counters, and import journals; never a task database.
WORKFLOW_STATE = env_path("AGENT_BOX_WORKFLOW_STATE", STATE_ROOT / "workflow")
# Nonsecret policy is exposed by default so config consumers agree even when
# tracker/workflow are disabled; their credential directory remains opt-in.
POLICY_RUNTIME_DIR = STATE_ROOT / "runtime/policy"
CONTAINER_POLICY_RUNTIME_DIR = HOST_HOME / ".local/share/agent-box/runtime/policy"
TRACKER_RUNTIME_DIR = STATE_ROOT / "runtime/tracker"
CONTAINER_TRACKER_RUNTIME_DIR = HOST_HOME / ".local/share/agent-box/runtime/tracker"
CONTAINER_WORKFLOW_STATE = HOST_HOME / ".local/share/agent-box/workflow"
