"""Workspace root resolution.

agent-box no longer requires projects to live under a fixed host directory
(previously ``~/code``). Instead it mounts a single, per-invocation workspace
root into the container at ``/workspace`` and preserves the caller's relative
working directory inside it.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .paths import CONTAINER_WORKSPACE


@dataclass(frozen=True)
class Workspace:
    host_root: Path
    container_root: Path
    relative: Path

    @property
    def container_cwd(self) -> Path:
        return self.container_root / self.relative


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

    The caller's position relative to the resolved root is preserved so a
    container started from a subdirectory keeps that subdirectory as its
    working directory inside ``/workspace``.
    """
    pwd = (cwd or Path.cwd()).resolve()

    override = override if override is not None else os.environ.get("AGENT_BOX_WORKSPACE_ROOT", "").strip()
    if override:
        root = Path(override).expanduser().resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"AGENT_BOX_WORKSPACE_ROOT does not exist: {root}")
        try:
            relative = pwd.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"current directory {pwd} is not inside the configured workspace root {root}"
            ) from exc
        return Workspace(root, CONTAINER_WORKSPACE, relative)

    root = git_toplevel(pwd) or pwd
    relative = pwd.relative_to(root)
    return Workspace(root, CONTAINER_WORKSPACE, relative)
