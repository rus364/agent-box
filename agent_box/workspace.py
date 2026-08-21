"""Workspace root resolution.

agent-box does not require projects to live under a fixed host directory
(previously ``~/code``), and does not remap the project into a neutral
in-container path either (previously a fixed ``/workspace``). Instead the
resolved host project root is bind-mounted at the *same absolute path*
inside the container, and the container's working directory mirrors the
host's current working directory exactly. This keeps paths shown to the
agent, in its logs/history, in hooks, and in shell commands identical to
what the user sees on the host.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .paths import expand_user


@dataclass(frozen=True)
class Workspace:
    host_root: Path
    cwd: Path  # absolute; always host_root or a descendant of it

    @property
    def container_root(self) -> Path:
        return self.host_root

    @property
    def container_cwd(self) -> Path:
        return self.cwd


def git_toplevel(path: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return Path(top).resolve() if top else None


def resolve_workspace(cwd: Path | None = None, override: str | None = None) -> Workspace:
    """Resolve the host workspace root for the current invocation.

    Resolution order:
      1. An explicit override (``AGENT_BOX_WORKSPACE_ROOT`` or a caller-supplied
         path) is used verbatim, provided ``cwd`` is inside it.
      2. The current git worktree/repository top-level directory.
      3. The current working directory itself.

    The container's working directory is set to the same absolute path as
    the host's current directory (not remapped), so a container started from
    a subdirectory of a git worktree keeps that subdirectory as its cwd.
    """
    pwd = (cwd or Path.cwd()).resolve()

    override = override if override is not None else os.environ.get("AGENT_BOX_WORKSPACE_ROOT", "").strip()
    if override:
        # Passwd-anchored ~ expansion (see paths.expand_user): $HOME must not
        # be able to move the workspace root out from under this check.
        root = expand_user(override).resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"AGENT_BOX_WORKSPACE_ROOT does not exist: {root}")
        if pwd != root and root not in pwd.parents:
            raise ValueError(
                f"current directory {pwd} is not inside the configured workspace root {root}"
            )
        return Workspace(root, pwd)

    root = git_toplevel(pwd) or pwd
    return Workspace(root, pwd)
