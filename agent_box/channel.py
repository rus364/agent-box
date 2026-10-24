"""Preflight and single-owner locking for Anthropic's official Telegram plugin.

agent-box implements no Telegram control of Claude. It passes one flag --
``--channels plugin:telegram@claude-plugins-official`` -- and the official
plugin owns everything behind it: the bot connection, pairing, the allowlist
and access policy, message and question delivery, and polling.

Two things are agent-box's job, and they are the whole of this module.

**Preflight.** A session started with a channel the plugin cannot serve looks
like a working session that silently never reaches Telegram. So the plugin's
presence and token are checked before the terminal is handed over. Installing
or configuring the plugin on the user's behalf is deliberately not done here:
``/plugin install`` and ``/telegram:configure`` are the supported mechanisms,
they run inside a Claude session, and writing Claude's plugin state from
outside would be guessing at an interface that is not ours. This module only
reports; :func:`setup_instructions` prints the exact commands.

**One owner.** A Telegram bot has exactly one conversation with its user. Two
concurrent Claude sessions both holding the channel would both receive every
inbound message and both answer it, which is worse than the second one simply
not being connected. A small host-side lock file makes the first live session
the owner; a second one runs as a normal local Claude session instead. There
is no daemon and no coordination beyond that file. "Live" means the session,
not the terminal it was started from: a detached session (see
:mod:`agent_box.sessions`) is still answering Telegram, so it keeps the
channel even though the launcher that claimed it has exited.

Everything inspected lives under ``CLAUDE_HOME``, which agent-box bind-mounts
read-write at its host path -- so a plugin installed once on the host is
already present in every container.
"""
from __future__ import annotations

import fcntl
import os
import stat
import time
from pathlib import Path
from typing import Any

from .atomicio import atomic_write_json, read_json
from .config import CLAUDE_TELEGRAM_CHANNEL, CLAUDE_TELEGRAM_PLUGIN
from .paths import CHANNEL_LOCK, CLAUDE_HOME
from .sessions import tmux_session_exists

INSTALLED_PLUGINS = "plugins/installed_plugins.json"
#: Where the plugin's MCP server keeps its token, allowlist and inbox. The
#: server derives this from CLAUDE_CONFIG_DIR, exactly like CLAUDE_HOME does.
CHANNEL_STATE = "channels/telegram"


def _home(path: Path | None) -> Path:
    # Resolved at call time, never as a default argument: a default binds the
    # module global once at import, which would make CLAUDE_HOME/CHANNEL_LOCK
    # silently un-overridable for tests and for any caller that relocates them.
    return CLAUDE_HOME if path is None else path


def _lock(path: Path | None) -> Path:
    return CHANNEL_LOCK if path is None else path


# -- preflight -------------------------------------------------------------

def plugin_installed(claude_home: Path | None = None) -> bool:
    try:
        data = read_json(_home(claude_home) / INSTALLED_PLUGINS)
    except (OSError, ValueError):
        return False
    plugins = data.get("plugins") if isinstance(data, dict) else None
    return bool(isinstance(plugins, dict) and plugins.get(CLAUDE_TELEGRAM_PLUGIN))


def token_configured(claude_home: Path | None = None) -> bool:
    """True when the plugin will find a bot token *inside the container*.

    Only the plugin's own persisted ``.env`` counts. A host shell token is
    never forwarded as a credential: non-owner Claude containers receive an
    explicit *empty* token to prevent the global plugin falling back to .env,
    while an owner reads this persisted token normally. Counting a host shell
    token here would therefore make preflight pass while the actual owner had
    no usable setup. The ``.env`` file lives under CLAUDE_HOME, which is
    mounted, so it is the canonical setup.
    """
    try:
        text = (_home(claude_home) / CHANNEL_STATE / ".env").read_text(encoding="utf-8")
    except OSError:
        return False
    return any(
        line.strip().startswith("TELEGRAM_BOT_TOKEN=") and line.strip() != "TELEGRAM_BOT_TOKEN="
        for line in text.splitlines()
    )


def missing_requirements(claude_home: Path | None = None) -> list[str]:
    """Human-readable list of what still has to be set up. Empty means ready."""
    claude_home = _home(claude_home)
    missing = []
    if not plugin_installed(claude_home):
        missing.append(f"the {CLAUDE_TELEGRAM_PLUGIN} plugin is not installed")
    if not token_configured(claude_home):
        missing.append(f"no bot token in {claude_home / CHANNEL_STATE / '.env'}")
    return missing


def setup_instructions(claude_home: Path | None = None) -> str:
    claude_home = _home(claude_home)
    return "\n".join([
        "Set up the official Telegram channel first (once, on this host):",
        "",
        "  1. Use your existing bot token, or create a bot with @BotFather on Telegram.",
        "  2. Start a Claude session and install the plugin:",
        "",
        "         agent-box claude",
        f"         /plugin install {CLAUDE_TELEGRAM_PLUGIN}",
        "         /telegram:configure <token>",
        "",
        f"     The token is written to {claude_home / CHANNEL_STATE / '.env'}.",
        "  3. Exit that session, then enable the channel:",
        "",
        "         agent-box config",
        "         Agents > Claude > Telegram channel: enable, then save.",
        "",
        "  4. Start Claude again and pair: DM the bot, then run",
        "     /telegram:access pair <code> and /telegram:access policy allowlist",
    ])


# -- single-owner lock -----------------------------------------------------
#
# Ownership is recorded, not enforced by a held file descriptor: whether an
# inherited fd stays open across the launcher's podman client is not something
# to depend on. Instead the lock file names what owns the channel, and
# liveness is decided by asking about it directly. flock is used only to make
# the read-check-write sequence atomic between two launchers racing at the
# same instant.
#
# A record names two things, and either one being alive means the channel is
# taken: the launcher process, and the box session it started. The second
# exists because a session outlives its launcher whenever the terminal goes
# away -- Claude is still running in the box and still holds the bot, so
# releasing the channel then would hand the bot to a second session while the
# first one is still answering it. A launch with no session to name (a piped
# ``claude -p`` run) records only the launcher, as before.

def _boot_id() -> str:
    """Identifies this boot, so PIDs from before a reboot are never trusted."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _pid_start_ticks(pid: int) -> str:
    """Field 22 of /proc/<pid>/stat: process start time, in clock ticks.

    Recorded alongside the PID so a *recycled* PID cannot be mistaken for the
    original owner. A PID alone is not enough: after the owning podman exits,
    the kernel is free to hand that number to an unrelated process, which
    would leave the channel looking permanently taken.
    """
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return ""
    # The comm field is parenthesized and may itself contain spaces, so split
    # after the closing parenthesis rather than on whitespace from the start.
    fields = stat_line[stat_line.rindex(")") + 2:].split()
    return fields[19] if len(fields) > 19 else ""


def _launcher_alive(record: dict[str, Any]) -> bool:
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    start = _pid_start_ticks(pid)
    return bool(start) and start == record.get("pid_start")


def _session_alive(record: dict[str, Any]) -> bool:
    """Whether the box session this record named is still running.

    This is what makes a detached Claude keep the channel after its terminal
    is gone. A box that cannot be reached at all reads as gone, so a removed
    container never leaves the channel permanently claimed.
    """
    container, session = record.get("container"), record.get("session")
    if not (isinstance(container, str) and isinstance(session, str) and container and session):
        return False
    return tmux_session_exists(container, session)


def _owner_alive(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    if record.get("boot_id") != _boot_id():
        return False  # written before a reboot: neither a PID nor a box survives one
    return _launcher_alive(record) or _session_alive(record)


def channel_owner(path: Path | None = None) -> dict[str, Any] | None:
    """The live owning session's record, or None if the channel is free.

    A record whose process is gone (exited, killed, or from before a reboot)
    is stale and reported as free; nothing has to clean it up first.
    """
    try:
        record = read_json(_lock(path))
    except (OSError, ValueError):
        return None
    return record if _owner_alive(record) else None


def acquire_channel(
    project: str = "", path: Path | None = None, container: str = "", session: str = "",
) -> bool:
    """Claim the channel for this launch. False means someone else has it.

    ``container``/``session`` name the box session this launch is about to
    start, when it has one. They are what keeps the claim standing after this
    process exits with the session still running; a launch without them is
    owned by this process alone and is free again the moment it ends.
    """
    path = _lock(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.parent / ".channel.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if channel_owner(path) is not None:
            return False
        pid = os.getpid()
        atomic_write_json(path, {
            "pid": pid,
            "pid_start": _pid_start_ticks(pid),
            "boot_id": _boot_id(),
            "container": container,
            "session": session,
            "project": project,
            "started_at": int(time.time()),
        }, stat.S_IRUSR | stat.S_IWUSR)
        return True
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def release_channel(path: Path | None = None) -> None:
    """Drop this launch's record when its session is not still running.

    A launcher exits both when its session ends and when it merely detaches
    from one. Only the first frees the channel: in the second the agent is
    still in its box, still connected to the bot, and reachable again with
    ``agent-box attach``.
    """
    path = _lock(path)
    record = channel_owner(path)
    if isinstance(record, dict) and record.get("pid") == os.getpid() and not _session_alive(record):
        path.unlink(missing_ok=True)


def describe_owner(record: dict[str, Any]) -> str:
    project = str(record.get("project") or "")
    session = str(record.get("session") or "")
    # Name the session when there is one: it is what the user can go back to,
    # and after a closed terminal the recorded PID is long gone.
    owner = f"session {session}" if session and not _launcher_alive(record) else f"pid {record.get('pid')}"
    return owner + (f", {project}" if project else "")


def channel_flag() -> list[str]:
    return ["--channels", CLAUDE_TELEGRAM_CHANNEL]
