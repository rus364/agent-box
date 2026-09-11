"""Address private tmux servers and list their sessions without runtime imports.

Containers provide project separation; host servers use the project's state
path instead. Neither shares a server with the user's ordinary tmux sessions.
"""
from __future__ import annotations

import subprocess
import hashlib
import os
from pathlib import Path

from .errors import AgentBoxError
from .paths import HOST_HOME, HOST_TMUX_CONFIG

#: Mounted read-only into every box beside keeper.sh, so editing it does not
#: require rebuilding the image.
TMUX_CONFIG_PATH = HOST_HOME / ".local/lib/agent-box/agent_box/tmux.conf"
#: A private server socket, so a tmux the user starts by hand inside the box
#: never shares a server with agent-box's own agent sessions.
TMUX_SOCKET = "agent-box"


def host_tmux_env(environment: dict[str, str] | None = None) -> dict[str, str]:
    # All clients use tmux's default socket directory, even across different
    # shells. An outer user tmux must not prevent attaching to our server.
    source = os.environ if environment is None else environment
    return {key: value for key, value in source.items() if key not in {"TMUX_TMPDIR", "TMUX"}}


def tmux_argv(*args: str, host_root: Path | None = None) -> list[str]:
    if host_root is not None:
        socket = "agent-box-host-" + hashlib.sha256(os.fsencode(host_root.resolve())).hexdigest()[:24]
        return ["tmux", "-f", str(HOST_TMUX_CONFIG), "-L", socket, *args]
    return ["tmux", "-f", str(TMUX_CONFIG_PATH), "-L", TMUX_SOCKET, *args]


def tmux_sessions(container: str | None, *, host_root: Path | None = None) -> list[str]:
    """Every live session in a running box.

    Deliberately just names.  tmux still counts the client a closed terminal
    left behind as attached -- its pty is held open by conmon, so tmux never
    learns that nobody is there -- which makes "attached" exactly wrong for
    the sessions this is used to find.
    """
    command = tmux_argv("list-sessions", "-F", "#{session_name}", host_root=host_root)
    try:
        result = subprocess.run(
            ["podman", "exec", container, *command] if container is not None else command,
            text=True, capture_output=True,
            **({"env": host_tmux_env()} if host_root is not None else {}),
        )
    except OSError as exc:
        raise AgentBoxError(f"could not list workspace sessions: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        # An empty box has no server yet; that is an answer, not a failure.
        if "no server running" in detail or "error connecting" in detail:
            return []
        if "not found" in detail:
            if host_root is not None:
                raise AgentBoxError("tmux not found on the host")
            raise AgentBoxError("this workspace image has no tmux; run 'agent-box build'")
        raise AgentBoxError(f"could not list workspace sessions: {detail or 'no diagnostic from podman'}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def tmux_session_exists(container: str | None, name: str, *, host_root: Path | None = None) -> bool:
    """Whether one named session is still running. Unknowable counts as gone.

    Callers use this to decide whether something an exited launcher started is
    still alive, so an unreachable box (removed, Podman unavailable) has to
    read as "gone" rather than propagate: the alternative is a session that
    can never be confirmed dead holding a resource forever.
    """
    try:
        return name in (tmux_sessions(container) if host_root is None else
                        tmux_sessions(container, host_root=host_root))
    except AgentBoxError:
        return False
