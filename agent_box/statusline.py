"""The whole status line agent-box renders for Claude Code.

``agent-box usage`` answers "how much of my subscription is left"; this module
answers the different question Claude Code's ``statusLine`` actually asks,
which is "what should the bottom line of the terminal say". That is the usage
numbers *plus* the things a shell prompt would normally show -- working
directory, git branch, clock -- because ``statusLine`` accepts exactly one
command and something has to compose them.

Keeping that composition here, rather than in a hand-written wrapper script,
is what makes it version-controlled, testable, and identical on the host and
inside the container, where ``agent-box`` is mounted onto PATH.

Claude Code pipes a JSON payload in on stdin and prints whatever comes back.
Nothing here reaches the model's context: a status line is an ordinary shell
command whose output only ever goes to the terminal.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from . import usage as usage_module
from .paths import HOST_HOME

#: Bold hues for the prompt half of the line. They stay distinct from the
#: muted 256-colour usage segments so the two halves read as different kinds
#: of information rather than one long list.
ARROW_COLOR = "1;32"
DIRECTORY_COLOR = "1;36"
GIT_COLOR = "1;34"
BRANCH_COLOR = "1;33"
ACCENT_COLOR = "22;33"

ARROW = "➜"
DIRTY_MARKER = "✗"
#: A status line redraws constantly, so git may never stall it. Every call is
#: bounded and a repository too slow to answer simply loses its segment.
GIT_TIMEOUT = 1.0


def git_output(cwd: Path, *args: str) -> str:
    """One bounded read-only git query. Returns "" for any kind of failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "--no-optional-locks", *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def git_branch(cwd: Path) -> str:
    """The checked-out branch, or the short commit when HEAD is detached."""
    if git_output(cwd, "rev-parse", "--is-inside-work-tree") != "true":
        return ""
    return git_output(cwd, "symbolic-ref", "--short", "HEAD") or git_output(
        cwd, "rev-parse", "--short", "HEAD")


def worktree_dirty(cwd: Path) -> bool:
    return bool(git_output(cwd, "status", "--porcelain"))


def format_directory(cwd: Path) -> str:
    """The trailing path component, with the home directory shown as "~"."""
    if cwd == HOST_HOME:
        return "~"
    return cwd.name or str(cwd)


def format_prompt(cwd: Path, *, now: str, virtual_env: str = "") -> str:
    """The shell-prompt half of the line: clock, venv, directory, git."""
    color = usage_module.colorize
    parts = [color(ARROW, ARROW_COLOR), f"[{now}]"]
    if virtual_env:
        parts.append(color(f"({Path(virtual_env).name})", ACCENT_COLOR))
    parts.append(color(format_directory(cwd), DIRECTORY_COLOR))
    branch = git_branch(cwd)
    if branch:
        segment = (
            f"{color('git:(', GIT_COLOR)}{color(branch, BRANCH_COLOR)}"
            f"{color(')', GIT_COLOR)}"
        )
        if worktree_dirty(cwd):
            segment += f" {color(DIRTY_MARKER, ACCENT_COLOR)}"
        parts.append(segment)
    return " ".join(parts)


def payload_cwd(obj: dict) -> Path:
    """The directory Claude Code is working in, however it reported it."""
    workspace = obj.get("workspace")
    candidates = [
        workspace.get("current_dir") if isinstance(workspace, dict) else None,
        obj.get("cwd"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return Path(candidate)
    return Path.cwd()


def render(payload: str, *, now: str | None = None) -> str:
    """Build the complete status line from one Claude Code payload."""
    obj = usage_module.parse_payload(payload)
    cwd = payload_cwd(obj)
    try:
        claude = usage_module.claude_usage_from_payload(obj) or usage_module.claude_usage(allow_refresh=False)
    except Exception:
        claude = usage_module.PLACEHOLDER_USAGE
    try:
        codex = usage_module.codex_usage()
    except Exception:
        codex = usage_module.PLACEHOLDER_USAGE
    return usage_module.join_segments(
        format_prompt(
            cwd,
            now=time.strftime("%H:%M") if now is None else now,
            virtual_env=os.environ.get("VIRTUAL_ENV", ""),
        ),
        usage_module.colorize(
            usage_module.format_context_window(obj), usage_module.CONTEXT_COLOR),
        usage_module.colorize(claude, usage_module.CLAUDE_COLOR),
        usage_module.colorize(codex, usage_module.CODEX_COLOR),
    )
