"""Asking a workspace box about the agent sessions running inside it.

A ``podman exec`` session cannot be re-attached: when its client dies (a
closed terminal, a dropped ssh connection) the process inside the box keeps
running with nobody on the other end of its pty, and ``podman attach`` reaches
only the container's main process, which is the keeper. So agent-box runs
interactive agent sessions under a tmux server *inside* the box, and the exec
client owns nothing but a tmux client.

This module is deliberately the small, dependency-light half of that: how to
address that tmux server and what it says is running. Both the launcher
(:mod:`agent_box.runtime`) and the Telegram channel's single-owner lock
(:mod:`agent_box.channel`) need the answer, and the channel must be able to
ask without importing the container machinery.
"""
from __future__ import annotations

import subprocess

from .errors import AgentBoxError
from .paths import HOST_HOME

#: Mounted read-only into every box beside keeper.sh, so editing it does not
#: require rebuilding the image.
TMUX_CONFIG_PATH = HOST_HOME / ".local/lib/agent-box/agent_box/tmux.conf"
#: A private server socket, so a tmux the user starts by hand inside the box
#: never shares a server with agent-box's own agent sessions.
TMUX_SOCKET = "agent-box"


def tmux_argv(*args: str) -> list[str]:
    return ["tmux", "-f", str(TMUX_CONFIG_PATH), "-L", TMUX_SOCKET, *args]


def tmux_sessions(container: str) -> list[str]:
    """Every live session in a running box.

    Deliberately just names.  tmux still counts the client a closed terminal
    left behind as attached -- its pty is held open by conmon, so tmux never
    learns that nobody is there -- which makes "attached" exactly wrong for
    the sessions this is used to find.
    """
    result = subprocess.run(
        ["podman", "exec", container, *tmux_argv("list-sessions", "-F", "#{session_name}")],
        text=True, capture_output=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        # An empty box has no server yet; that is an answer, not a failure.
        if "no server running" in detail or "error connecting" in detail:
            return []
        if "not found" in detail:
            raise AgentBoxError("this workspace image has no tmux; run 'agent-box build'")
        raise AgentBoxError(f"could not list workspace sessions: {detail or 'no diagnostic from podman'}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def tmux_session_exists(container: str, name: str) -> bool:
    """Whether one named session is still running. Unknowable counts as gone.

    Callers use this to decide whether something an exited launcher started is
    still alive, so an unreachable box (removed, Podman unavailable) has to
    read as "gone" rather than propagate: the alternative is a session that
    can never be confirmed dead holding a resource forever.
    """
    try:
        return name in tmux_sessions(container)
    except AgentBoxError:
        return False
