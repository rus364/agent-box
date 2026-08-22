"""Flow-Next auto-push: pending marker, guarded push, and the managed Claude
hooks that drive both.

The contract is deliberately narrow:

* a successful *actual* ``flowctl done`` invocation -- detected from a Claude
  ``PostToolUse`` hook on the ``Bash`` tool -- arms a small marker recorded in
  git's own private state for the current repository and branch;
* a later Claude ``Stop`` consumes that marker and, only if every guard in
  :func:`push_preflight` passes, runs exactly ``git push -u origin HEAD``.

Nothing here decides whether tests passed, review shipped, or a quality
audit succeeded -- that is Flow-Next's job, expressed entirely through
whether ``flowctl done`` itself exited zero. This module never parses Claude
or reviewer prose, never merges/rebases/resets, and never force-pushes.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .atomicio import atomic_write_json, read_json
from .errors import AgentBoxError
from .flow_next import AUTO_PUSH_ENV, is_flow_next_workspace

#: Relative to `git rev-parse --git-path <name>` -- resolved through git
#: itself rather than assuming ``.git/...`` literally, because `.git` is a
#: *file* (not a directory) in a linked worktree.
MARKER_GIT_PATH = "agent-box/flow-next-push-pending.json"

PROTECTED_BRANCHES = {"main", "master"}

#: Stable, distinct command identifiers so hook ownership/removal is
#: deterministic and independent of the notification hooks in `agent_box.notify`.
POST_TOOL_USE_COMMAND = "agent-box flow-next post-tool-use"
STOP_COMMAND = "agent-box flow-next stop"

#: Matches `flowctl done ...` or `/any/path/flowctl done ...`, anchored at the
#: start of the command so `echo "flowctl done"` or `flowctl list`/`show`
#: never match.
FLOWCTL_DONE_RE = re.compile(r"^(?:\S*/)?flowctl(?:\.\w+)?\s+done(?:\s|$)")


def _run(args: list[str], cwd: Path | str | None = None, timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout, check=False,
    )


# -- git-local marker -------------------------------------------------------

def _git_path(cwd: Path, name: str) -> Path | None:
    """Resolve a private git path via ``git rev-parse --git-path``.

    Deliberately not ``cwd / ".git" / name``: ``.git`` is a plain file (not a
    directory) inside a linked worktree, so only git itself knows the real
    location of its own state.
    """
    result = _run(["git", "rev-parse", "--git-path", name], cwd=cwd)
    if result.returncode != 0:
        return None
    path = result.stdout.strip()
    return (cwd / path).resolve() if path else None


def current_branch(cwd: Path) -> str | None:
    """The branch HEAD is attached to, or None when HEAD is detached."""
    result = _run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=cwd)
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


def head_sha(cwd: Path) -> str | None:
    result = _run(["git", "rev-parse", "HEAD"], cwd=cwd)
    return result.stdout.strip() if result.returncode == 0 else None


def arm_marker(cwd: Path) -> None:
    """Record "a Flow-Next task just completed here" for a later Stop.

    No-ops (silently) outside a git repository or with a detached HEAD --
    there is nothing safe to record, and this must never raise into the
    caller's hook process.
    """
    marker_path = _git_path(cwd, MARKER_GIT_PATH)
    if marker_path is None:
        return
    branch = current_branch(cwd)
    if branch is None:
        return
    atomic_write_json(marker_path, {"branch": branch, "head": head_sha(cwd)})


def read_marker(cwd: Path) -> dict[str, Any] | None:
    marker_path = _git_path(cwd, MARKER_GIT_PATH)
    if marker_path is None or not marker_path.is_file():
        return None
    try:
        data = read_json(marker_path)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clear_marker(cwd: Path) -> None:
    marker_path = _git_path(cwd, MARKER_GIT_PATH)
    if marker_path is not None:
        marker_path.unlink(missing_ok=True)


# -- done detection (PostToolUse) -------------------------------------------

def _bash_call_succeeded(payload: dict[str, Any]) -> bool:
    """Best-effort success signal for a completed Bash tool call.

    Claude Code's public hook schema does not (as of this writing) document a
    dedicated exit-code field on ``tool_response`` for the Bash tool. This
    treats an explicit failure indicator -- ``is_error``/``isError`` true, or
    ``success`` false -- as a failure, and otherwise assumes success. That is
    a deliberate fail-open choice: worst case a marker is armed one Stop early
    for a command that actually failed, and it is still gated by every other
    push guard below, none of which can be satisfied by a `flowctl done` that
    didn't actually run.
    """
    response = payload.get("tool_response")
    if response is None:
        return True
    if not isinstance(response, dict):
        return False
    if response.get("is_error") or response.get("isError"):
        return False
    return response.get("success") is not False


def handle_post_tool_use(payload: dict[str, Any]) -> bool:
    """Arm the pending-push marker if this Bash call was a successful
    ``flowctl done``. Returns whether it was armed (for tests); never raises.
    """
    if payload.get("tool_name") != "Bash":
        return False
    command = payload.get("tool_input", {})
    command = command.get("command") if isinstance(command, dict) else None
    if not isinstance(command, str) or not FLOWCTL_DONE_RE.match(command.strip()):
        return False
    if not _bash_call_succeeded(payload):
        return False
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return False
    arm_marker(Path(cwd))
    return True


def run_post_tool_use_hook(stdin_text: str) -> None:
    try:
        payload = json.loads(stdin_text)
    except json.JSONDecodeError:
        return
    if isinstance(payload, dict):
        handle_post_tool_use(payload)


# -- push preflight / guarded push -------------------------------------------

class PushDecision:
    def __init__(self, action: str, detail: str = ""):
        #: One of "pushed", "skipped", "invalidated", "failed".
        self.action = action
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"PushDecision({self.action!r}, {self.detail!r})"


def _remote_default_branch(cwd: Path) -> str | None:
    """The repo's remote default branch, from local state only.

    Deliberately not a network call: this reads the cached
    ``refs/remotes/origin/HEAD`` symref (set by `git clone`/`git remote
    set-head`) rather than asking the remote, so an auto-push decision never
    blocks on network access. Absent that ref, this returns None and the
    guard simply does not apply -- `main`/`master` are still covered
    explicitly above it.
    """
    result = _run(["git", "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"], cwd=cwd)
    if result.returncode != 0:
        return None
    ref = result.stdout.strip()
    return ref.split("/", 1)[1] if "/" in ref else ref or None


def _worktree_clean(cwd: Path) -> bool:
    result = _run(["git", "status", "--porcelain"], cwd=cwd)
    return result.returncode == 0 and result.stdout.strip() == ""


def _upstream_ref(cwd: Path) -> str | None:
    result = _run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=cwd)
    return result.stdout.strip() if result.returncode == 0 else None


def _is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    result = _run(["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=cwd)
    return result.returncode == 0


def push_preflight(cwd: Path, auto_push_enabled: bool) -> PushDecision:
    """Every required guard, in order; the first failure decides the outcome.

    Returns a :class:`PushDecision` describing what happened (or would have
    happened) without performing the push itself -- see :func:`guarded_push`.
    """
    if not auto_push_enabled:
        return PushDecision("skipped", "auto-push not enabled for this launch")
    if not is_flow_next_workspace(cwd):
        return PushDecision("skipped", "not a Flow-Next workspace")
    marker = read_marker(cwd)
    if marker is None:
        return PushDecision("skipped", "no pending marker")

    if _run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd).returncode != 0:
        return PushDecision("skipped", "not a git repository")

    branch = current_branch(cwd)
    if branch is None:
        return PushDecision("skipped", "HEAD is detached")

    if marker.get("branch") != branch:
        clear_marker(cwd)
        return PushDecision("invalidated", f"marker was for '{marker.get('branch')}', not '{branch}'")

    if branch in PROTECTED_BRANCHES:
        clear_marker(cwd)
        return PushDecision("invalidated", f"'{branch}' is a protected branch")

    default_branch = _remote_default_branch(cwd)
    if default_branch and branch == default_branch:
        clear_marker(cwd)
        return PushDecision("invalidated", f"'{branch}' is the remote default branch")

    if not _worktree_clean(cwd):
        return PushDecision("skipped", "worktree is dirty; marker kept for a later clean Stop")

    upstream = _upstream_ref(cwd)
    if upstream is not None:
        upstream_sha = _run(["git", "rev-parse", upstream], cwd=cwd).stdout.strip()
        if upstream_sha and upstream_sha == head_sha(cwd):
            clear_marker(cwd)
            return PushDecision("invalidated", "nothing new to push")
        if not _is_ancestor(cwd, upstream, "HEAD"):
            return PushDecision("skipped", f"'{branch}' has diverged from '{upstream}'; refusing to repair it")

    return PushDecision("ready")


def guarded_push(cwd: Path, auto_push_enabled: bool) -> PushDecision:
    """Run :func:`push_preflight` and, only when it says ``ready``, push.

    The only command this ever runs is ``git push -u origin HEAD`` -- never
    ``--force``/``--force-with-lease``, never another branch, never tags,
    never another remote. A push failure (auth/network) keeps the marker so
    a later, clean Stop can retry; it never forces anything and never blocks
    Claude from stopping.
    """
    decision = push_preflight(cwd, auto_push_enabled)
    if decision.action != "ready":
        return decision
    result = _run(["git", "push", "-u", "origin", "HEAD"], cwd=cwd, timeout=60)
    if result.returncode == 0:
        clear_marker(cwd)
        return PushDecision("pushed", "git push -u origin HEAD")
    return PushDecision("failed", (result.stderr or result.stdout or "git push failed").strip())


def run_stop_hook(stdin_text: str) -> PushDecision:
    """The Stop hook entry point. Never raises and never blocks the stop."""
    try:
        payload = json.loads(stdin_text)
    except json.JSONDecodeError:
        payload = {}
    cwd_value = payload.get("cwd") if isinstance(payload, dict) else None
    cwd = Path(cwd_value) if isinstance(cwd_value, str) and cwd_value else Path.cwd()
    import os
    auto_push_enabled = bool(os.environ.get(AUTO_PUSH_ENV, "").strip())
    decision = guarded_push(cwd, auto_push_enabled)
    if decision.action in ("pushed", "failed", "invalidated"):
        print(f"agent-box flow-next: {decision.action}: {decision.detail}", file=sys.stderr)
    return decision


# -- managed Claude hooks -----------------------------------------------------

def _post_tool_use_entry() -> dict[str, Any]:
    return {"matcher": "Bash", "hooks": [{"type": "command", "command": POST_TOOL_USE_COMMAND}]}


def _stop_entry() -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": STOP_COMMAND}]}


def _uses_command(entry: Any, command: str) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get("hooks"), list) and any(
        isinstance(h, dict) and h.get("type") == "command" and h.get("command") == command
        for h in entry["hooks"]
    )


def install_flow_push_hooks(settings_path: Path) -> None:
    """Install the PostToolUse/Bash and Stop hooks that drive auto-push.

    Independent of the notification hooks in ``agent_box.notify`` -- both may
    coexist under the same ``Stop`` event, each identified by its own stable
    command string, and installing/removing one never touches the other or
    any unrelated hook the user configured by hand. Idempotent: running this
    again with the hooks already present changes nothing.
    """
    settings = read_json(settings_path) if settings_path.exists() else {}
    if not isinstance(settings, dict):
        raise AgentBoxError(f"{settings_path} must contain a JSON object")
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise AgentBoxError(f"{settings_path}: hooks must be a JSON object")
    for event, entry in (
        ("PostToolUse", _post_tool_use_entry()),
        ("Stop", _stop_entry()),
    ):
        items = hooks.setdefault(event, [])
        if not isinstance(items, list):
            raise AgentBoxError(f"{settings_path}: hooks.{event} must be a list")
        if entry not in items:
            items.append(entry)
    atomic_write_json(settings_path, settings)


def remove_flow_push_hooks(settings_path: Path) -> None:
    """Remove only the hooks installed by :func:`install_flow_push_hooks`."""
    if not settings_path.exists():
        return
    settings = read_json(settings_path)
    if not isinstance(settings, dict):
        raise AgentBoxError(f"{settings_path} must contain a JSON object")
    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        cleaned = {}
        for event, items in hooks.items():
            if not isinstance(items, list):
                cleaned[event] = items
                continue
            owned = (POST_TOOL_USE_COMMAND,) if event == "PostToolUse" else (STOP_COMMAND,)
            remaining = [x for x in items if not any(_uses_command(x, cmd) for cmd in owned)]
            if remaining:
                cleaned[event] = remaining
        if cleaned:
            settings["hooks"] = cleaned
        else:
            settings.pop("hooks", None)
    atomic_write_json(settings_path, settings)
