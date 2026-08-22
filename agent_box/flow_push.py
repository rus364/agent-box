"""Flow-Next auto-push: pending marker, guarded push, and the managed Claude
hooks that drive both.

The contract is deliberately narrow:

* a successful *actual, unchained* ``flowctl done`` invocation -- detected
  from a Claude ``PostToolUse`` hook on the ``Bash`` tool -- arms a small
  marker recorded in git's own private state for the repository and branch;
* a later Claude ``Stop`` consumes that marker and, only if every guard in
  :func:`push_preflight` passes, runs exactly ``git push -u origin HEAD``.

Nothing here decides whether tests passed, review shipped, or a quality
audit succeeded -- that is Flow-Next's job, expressed entirely through
whether ``flowctl done`` itself exited zero. This module never parses Claude
or reviewer prose, never merges/rebases/resets, and never force-pushes.

Both entry points normalize whatever ``cwd`` Claude reports to the git
worktree top-level directory (:func:`agent_box.workspace.git_toplevel`) --
the same resolver the launcher uses -- so a session started in a nested
directory or a linked worktree agrees with the launcher on what "the
workspace" is; see the module docstring note in ``run_post_tool_use_hook``/
``run_stop_hook``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .atomicio import atomic_write_json, read_json
from .flow_next import AUTO_PUSH_ENV, is_flow_next_workspace
from .workspace import git_toplevel

#: Relative to `git rev-parse --git-path <name>` -- resolved through git
#: itself rather than assuming ``.git/...`` literally, because `.git` is a
#: *file* (not a directory) in a linked worktree.
MARKER_GIT_PATH = "agent-box/flow-next-push-pending.json"

PROTECTED_BRANCHES = {"main", "master"}

#: Stable, distinct command identifiers so hook ownership/removal is
#: deterministic and independent of the notification hooks in `agent_box.notify`.
POST_TOOL_USE_COMMAND = "agent-box flow-next post-tool-use"
STOP_COMMAND = "agent-box flow-next stop"

#: A bounded network round trip (`git ls-remote`) is used only as a
#: fail-closed fallback when the answer can't be found locally -- never to
#: repair anything, and never allowed to hang the Stop hook indefinitely.
REMOTE_QUERY_TIMEOUT = 8.0

#: Any of these mean the text is not a single, unchained, un-piped,
#: un-backgrounded command -- so a successful overall Bash result would not
#: reliably mean `flowctl done` itself is what exited zero (`flowctl done
#: ... || true` still reports a normal Bash success). Rejecting these is a
#: deliberate, narrow syntax gate, not a shell parser.
_SHELL_CHAINING_RE = re.compile(r"[;&|`\n]|\$\(")
#: `flowctl done ...` or `/abs/path/flowctl done ...`, anchored at both ends
#: once chaining characters have already been ruled out above.
_FLOWCTL_DONE_RE = re.compile(r"^(?:\S*/)?flowctl(?:\.\w+)?\s+done(?:\s+\S.*)?$")


def _run(
    args: list[str], cwd: Path | str | None = None, timeout: float = 10,
) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        args, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
        timeout=timeout, check=False, env=env,
    )


def _hook_workspace_root(cwd_value: Any) -> Path:
    """The same repository root the launcher would resolve for this ``cwd``.

    Claude reports whatever directory the session's ``cwd`` happens to be --
    a nested subdirectory, or a linked worktree -- not necessarily the
    repository root the launcher mounted/resolved
    (``agent_box.workspace.resolve_workspace``). Every Flow-Next detection,
    marker, and push-guard decision below must agree on the same root, so
    this is the one place that normalizes it via ``git rev-parse
    --show-toplevel``. Outside a git repository, `git_toplevel` returns None
    and the raw value is used as-is -- every check below already fails
    safely (no marker, "not a git repository", etc.) in that case.
    """
    cwd = Path(cwd_value) if isinstance(cwd_value, str) and cwd_value else Path.cwd()
    return git_toplevel(cwd) or cwd


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

def _is_bare_flowctl_done(command: str) -> bool:
    """Whether ``command`` is *exactly* one unchained ``flowctl done`` call.

    Rejects anything that could let a shell mask ``flowctl done``'s own exit
    status while the overall Bash tool call still reports success --
    ``flowctl done ... || true``, ``; echo ok``, ``| cat``, background ``&``,
    command substitution, multiple lines. Wrapper forms such as ``env FOO=bar
    flowctl done`` or ``cd repo && flowctl done`` are deliberately rejected
    rather than special-cased: this is a narrow syntax gate, not a shell
    parser, and it is safe to under-detect here (see module docstring).
    """
    command = command.strip()
    if not command or _SHELL_CHAINING_RE.search(command):
        return False
    return bool(_FLOWCTL_DONE_RE.match(command))


def _bash_call_succeeded(payload: dict[str, Any]) -> bool:
    """Whether this ``Bash`` ``PostToolUse`` represents a successful command.

    Per Claude Code's current documented hook contract, ``PostToolUse``
    fires only after a tool call *succeeds*; a Bash command that exits
    nonzero instead produces the separate ``PostToolUseFailure`` event
    (documented with an ``error`` field such as ``"Exit code 1: ..."``), and
    :func:`handle_post_tool_use` below only accepts ``hook_event_name ==
    "PostToolUse"`` -- so a failed Bash command never reaches this function
    at all. The documented current shape of ``tool_response`` for ``Bash``
    is a plain string (the command's stdout), carrying no separate
    success/failure field to inspect.

    The dict-shaped check below (``is_error``/``isError``/``success``) is
    kept only as defensive compatibility with an alternate or older
    ``tool_response`` shape, not because current documentation describes it
    for Bash -- if such a shape is ever seen, it is treated as failure;
    otherwise (a plain string, or the field absent) this is success, exactly
    matching the documented contract above.
    """
    response = payload.get("tool_response")
    if isinstance(response, dict):
        if response.get("is_error") or response.get("isError"):
            return False
        if response.get("success") is False:
            return False
    return True


def handle_post_tool_use(payload: dict[str, Any]) -> bool:
    """Arm the pending-push marker if this Bash call was a successful,
    unchained ``flowctl done``. Returns whether it was armed (for tests);
    never raises.
    """
    if payload.get("hook_event_name") != "PostToolUse":
        # Strict, not `in (None, "PostToolUse")`: this hook is registered
        # only under the PostToolUse event key (see
        # `install_flow_push_hooks`), and Claude Code always sends
        # `hook_event_name` on every real invocation. Accepting a missing
        # value would only be accommodating an incomplete synthetic
        # payload, not real hook traffic.
        return False
    if payload.get("tool_name") != "Bash":
        return False
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    if tool_input.get("run_in_background"):
        # A backgrounded command has not necessarily finished by the time
        # PostToolUse fires; there is no completion signal to trust here.
        return False
    command = tool_input.get("command")
    if not isinstance(command, str) or not _is_bare_flowctl_done(command):
        return False
    if not _bash_call_succeeded(payload):
        return False
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return False
    arm_marker(_hook_workspace_root(cwd))
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


class RemoteState:
    """What one bounded, live query of ``origin`` established.

    Both ``default_branch`` and ``branch_sha`` are answered by the *same*
    ``git ls-remote`` round trip (see :func:`_query_origin`) -- there is no
    separate "trust the local cache" path for either question. A cached
    ``refs/remotes/origin/HEAD`` symref is not authoritative evidence: it
    can go stale the moment the remote's default branch changes underneath
    it, and a decision this consequential must be based on what ``origin``
    says *right now*, not what a prior ``clone``/``fetch`` happened to
    record locally.
    """

    def __init__(self, default_branch: str | None, branch_sha: str | None, branch_exists: bool):
        #: None means the query could not positively determine it -- callers
        #: must fail closed, never fall back to a local guess.
        self.default_branch = default_branch
        #: The current SHA of ``refs/heads/<branch>`` on ``origin``, or None
        #: if ``branch_exists`` is False (a normal new-branch push).
        self.branch_sha = branch_sha
        self.branch_exists = branch_exists

    @property
    def default_determined(self) -> bool:
        return self.default_branch is not None


def _query_origin(cwd: Path, branch: str) -> RemoteState | None:
    """One bounded, read-only ``git ls-remote --symref origin HEAD
    refs/heads/<branch>`` call answering both the remote's current default
    branch and the current state of the actual push target in a single
    network round trip.

    Returns ``None`` on any failure, timeout, or output with no resolvable
    default-branch symref -- callers must treat that as "cannot be
    determined", never as "safe to push". A missing ``refs/heads/<branch>``
    line (with the query otherwise successful) means the remote confirmed
    the branch doesn't exist there yet, which is a normal new-branch push.
    """
    branch_ref = f"refs/heads/{branch}"
    try:
        result = _run(
            ["git", "ls-remote", "--symref", "origin", "HEAD", branch_ref],
            cwd=cwd, timeout=REMOTE_QUERY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    default_branch: str | None = None
    branch_sha: str | None = None
    branch_exists = False
    for line in result.stdout.splitlines():
        if line.startswith("ref:"):
            rest = line[len("ref:"):].strip()
            ref, _, target = rest.rpartition("\t")
            if target == "HEAD" and ref.startswith("refs/heads/"):
                default_branch = ref[len("refs/heads/"):]
            continue
        sha, _, ref = line.partition("\t")
        if ref == branch_ref and sha:
            branch_sha = sha
            branch_exists = True
    if default_branch is None:
        # Ambiguous/unborn remote HEAD: cannot positively determine the
        # default branch from this query, so nothing here can be trusted.
        return None
    return RemoteState(default_branch, branch_sha, branch_exists)


def _worktree_clean(cwd: Path) -> bool:
    result = _run(["git", "status", "--porcelain"], cwd=cwd)
    return result.returncode == 0 and result.stdout.strip() == ""


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
        # Usually transient (mid-rebase, a temporary detached checkout); the
        # marker is kept so a later Stop back on the recorded branch can
        # still complete the push, per module docstring on marker lifetime.
        return PushDecision("skipped", "HEAD is detached")

    if marker.get("branch") != branch:
        clear_marker(cwd)
        return PushDecision("invalidated", f"marker was for '{marker.get('branch')}', not '{branch}'")

    if branch in PROTECTED_BRANCHES:
        clear_marker(cwd)
        return PushDecision("invalidated", f"'{branch}' is a protected branch")

    if not _worktree_clean(cwd):
        return PushDecision("skipped", "worktree is dirty; marker kept for a later clean Stop")

    # One live, bounded query answers both "what is origin's current default
    # branch" and "what does origin/<branch> currently look like". Neither
    # question is ever answered from a local cache: a cached
    # refs/remotes/origin/HEAD can go stale the moment the remote's default
    # branch changes, so only what `origin` reports *right now* counts.
    state = _query_origin(cwd, branch)
    if state is None:
        return PushDecision(
            "skipped", "current remote default branch could not be determined; refusing to auto-push")
    if branch == state.default_branch:
        clear_marker(cwd)
        return PushDecision("invalidated", f"'{branch}' is the remote default branch")

    if state.branch_exists:
        local_sha = head_sha(cwd)
        if state.branch_sha == local_sha:
            clear_marker(cwd)
            return PushDecision("invalidated", "nothing new to push")
        if not _is_ancestor(cwd, state.branch_sha, "HEAD"):
            return PushDecision(
                "skipped", f"'origin/{branch}' is not an ancestor of HEAD; refusing to repair it")

    return PushDecision("ready")


def guarded_push(cwd: Path, auto_push_enabled: bool) -> PushDecision:
    """Run :func:`push_preflight` and, only when it says ``ready``, push.

    The only command this ever runs is ``git push -u origin HEAD`` -- never
    ``--force``/``--force-with-lease``, never another branch, never tags,
    never another remote. A push failure, timeout, or any other operational
    problem keeps the marker so a later, clean Stop can retry; it never
    forces anything and never blocks Claude from stopping (see
    :func:`run_stop_hook`, which is the only caller that needs to be
    exception-safe against this).
    """
    decision = push_preflight(cwd, auto_push_enabled)
    if decision.action != "ready":
        return decision
    try:
        result = _run(["git", "push", "-u", "origin", "HEAD"], cwd=cwd, timeout=60)
    except subprocess.TimeoutExpired:
        return PushDecision("failed", "git push timed out")
    if result.returncode == 0:
        clear_marker(cwd)
        return PushDecision("pushed", "git push -u origin HEAD")
    return PushDecision("failed", (result.stderr or result.stdout or "git push failed").strip())


def run_stop_hook(stdin_text: str) -> PushDecision:
    """The Stop hook entry point. Never raises and never blocks the stop.

    Every operational failure -- a timeout, a missing ``git``, an
    unexpected exception anywhere in the guard chain -- is caught here and
    turned into a ``"failed"`` decision with the marker left untouched,
    rather than letting an exception escape into Claude's hook runner. This
    is deliberately the only place that catches broadly: every helper above
    already fails closed on its own terms, this is just the outermost net
    so a genuinely unexpected error still can't produce a stop-loop or a
    traceback where Claude expects a hook to just finish.
    """
    try:
        payload = json.loads(stdin_text)
    except json.JSONDecodeError:
        payload = {}
    cwd_value = payload.get("cwd") if isinstance(payload, dict) else None
    root = _hook_workspace_root(cwd_value)
    auto_push_enabled = bool(os.environ.get(AUTO_PUSH_ENV, "").strip())
    try:
        decision = guarded_push(root, auto_push_enabled)
    except Exception as exc:  # noqa: BLE001 - the outermost safety net, see docstring
        decision = PushDecision("failed", f"unexpected error: {exc}")
    if decision.action in ("pushed", "failed", "invalidated"):
        print(f"agent-box flow-next: {decision.action}: {decision.detail}", file=sys.stderr)
    return decision


# -- managed Claude hooks -----------------------------------------------------

def _post_tool_use_entry() -> dict[str, Any]:
    return {"matcher": "Bash", "hooks": [{"type": "command", "command": POST_TOOL_USE_COMMAND}]}


def _stop_entry() -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": STOP_COMMAND}]}


def install_flow_push_hooks(settings_path: Path) -> None:
    """Install the PostToolUse/Bash and Stop hooks that drive auto-push.

    Independent of the notification hooks in ``agent_box.notify`` -- both may
    coexist under the same ``Stop`` event, each identified by its own stable
    command string, and installing/removing one never touches the other or
    any unrelated hook the user configured by hand. Idempotent: running this
    again with the hooks already present changes nothing. Deliberately never
    installs a ``StopFailure`` hook -- a failed Claude turn must never push.
    """
    from .claude_settings import install_hook_entries
    install_hook_entries(settings_path, [
        ("PostToolUse", _post_tool_use_entry()),
        ("Stop", _stop_entry()),
    ])


def remove_flow_push_hooks(settings_path: Path) -> None:
    """Remove only the hooks installed by :func:`install_flow_push_hooks`."""
    from .claude_settings import remove_hook_commands
    remove_hook_commands(settings_path, {
        "PostToolUse": (POST_TOOL_USE_COMMAND,),
        "Stop": (STOP_COMMAND,),
    })
