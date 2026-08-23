"""Flow-Next auto-push: state reconciliation at agent-turn completion, and
the managed Claude/Codex adapters that drive it.

The contract is a reconciliation, not an event detector:

* at the end of an agent turn (Claude ``Stop``, Codex's turn-complete notify
  event), the workspace for that event's ``cwd`` is resolved;
* Git itself is asked what is locally committed on the current branch but not
  yet on ``origin`` -- the *unpushed range*;
* the paths that changed within that range are inspected for tracked
  Flow-Next task receipts (``.flow/tasks/<task-id>.md``);
* for each such task id, Flow-Next's own CLI (``flowctl show <id> --json``)
  is asked for that task's current status and its provenance
  (``status_source``);
* if at least one of those tasks is confirmed ``"done"`` with an
  authoritative ``status_source`` (``"flow-state"`` -- Flow-Next's own
  runtime state answered, never the possibly-stale ``"committed"`` tracked
  snapshot), every guard in :func:`push_preflight` runs, and only if all of
  them pass does this push exactly ``git push -u origin HEAD``.

Nothing here parses shell commands, commit messages, or prose to infer
completion -- Flow-Next's CLI is the sole authority on task status, and Git
is the sole authority on what is locally new. There is no marker, no pending
state, and nothing to invalidate: every reconciliation starts from scratch,
so it is safe to call repeatedly (a later call after a real push finds
nothing new and simply does nothing) and safe across any number of
independent sessions/workspaces sharing one long-lived container -- each
event is scoped only by the workspace resolved from its own ``cwd``.

A missed auto-push is an acceptable outcome (the user can always run ``git
push`` manually), so every ambiguous or undeterminable condition -- an
unresolved Flowctl executable, a malformed Flowctl response, a non-
authoritative ``status_source``, an undeterminable remote state, a dirty
worktree -- fails closed to "skip", not to "push anyway".
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .flow_next import AUTO_PUSH_ENV, FLOWCTL_ENV, is_flow_next_workspace
from .workspace import git_toplevel

PROTECTED_BRANCHES = {"main", "master"}

#: Stable, distinct command identifier so hook ownership/removal is
#: deterministic and independent of the notification hooks in `agent_box.notify`.
STOP_COMMAND = "agent-box flow-next stop"

#: Bounded network round trips (`git ls-remote`) and Flowctl invocations are
#: used only as fail-closed answers to "what does origin/Flow-Next currently
#: say" -- never to repair anything, and never allowed to hang a Stop/notify
#: hook indefinitely.
REMOTE_QUERY_TIMEOUT = 8.0
FLOWCTL_TIMEOUT = 15.0

#: Tracked Flow-Next task receipts live at exactly this flat path shape
#: relative to the workspace root; nothing below this module ever looks
#: inside a task file's content, only at whether its path changed.
FLOW_TASKS_DIR = ".flow/tasks/"


#: Applied only to the final `git push` subprocess (see `guarded_push`),
#: never written to the repository or global config -- `git push -u origin
#: HEAD` already names one branch and one remote explicitly, but two Git
#: settings can still silently widen what that command actually does:
#: `push.followTags` pushes annotated tags reachable from the pushed
#: commit, and `push.recurseSubmodules` can push into an entirely
#: different (submodule) repository. Both were mechanically reproduced.
#:
#: These are passed as Git's own command-line `-c <key>=<value>`, and that
#: choice is load-bearing in two independent ways -- both verified against
#: git 2.55 rather than assumed:
#:
#: * `-c` occupies Git's *command* scope, the highest-precedence scope
#:   there is. It therefore overrides an inherited `GIT_CONFIG_PARAMETERS`
#:   (also command scope, but `-c` is appended after it, and last wins) as
#:   well as an inherited `GIT_CONFIG_COUNT` entry (merely *environment*
#:   scope).
#: * `-c` is purely *additive*. It leaves every inherited `GIT_CONFIG_*`
#:   entry in place, so the final push resolves `remote.origin.url`,
#:   `remote.origin.pushurl`, and `url.*.insteadOf`/`pushInsteadOf` from
#:   exactly the same configuration the preflight queries saw.
#:
#: `push.default`, `remote.<name>.push`, and `remote.<name>.mirror` are
#: deliberately not neutralized: the explicit `HEAD` refspec on the command
#: line already makes the first two moot and makes the third fail outright
#: (`--mirror can't be combined with refspecs`) rather than expand anything
#: -- verified mechanically, see flow_push tests.
_PUSH_CONFIG_OVERRIDES = {
    "push.followTags": "false",
    "push.recurseSubmodules": "no",
}


def _push_config_args() -> list[str]:
    """``_PUSH_CONFIG_OVERRIDES`` as ``-c key=value`` argv, for `git push`."""
    return [arg for key, value in _PUSH_CONFIG_OVERRIDES.items() for arg in ("-c", f"{key}={value}")]


def _run(
    args: list[str], cwd: Path | str | None = None, timeout: float = 10,
) -> subprocess.CompletedProcess:
    """Run one git or Flowctl subprocess with a *single*, uniform environment.

    Every invocation in this module -- preflight queries, the Flowctl query,
    and the final push alike -- inherits exactly the same environment, so
    they cannot disagree about what ``origin`` resolves to. The push's extra
    safety settings ride on the command line instead (see
    `_push_config_args`) precisely so that this stays true. Always argv-style
    (never ``shell=True``): every caller passes a literal list, so a task id
    or path can never be interpreted as shell syntax.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        args, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
        timeout=timeout, check=False, env=env,
    )


def _hook_workspace_root(cwd_value: Any) -> Path:
    """The same repository root the launcher would resolve for this ``cwd``.

    An agent reports whatever directory its session's ``cwd`` happens to be
    -- a nested subdirectory, or a linked worktree -- not necessarily the
    repository root the launcher mounted/resolved
    (``agent_box.workspace.resolve_workspace``). Every Flow-Next detection
    and push-guard decision below must agree on the same root, so this is the
    one place that normalizes it via ``git rev-parse --show-toplevel``.
    Outside a git repository, `git_toplevel` returns None and the raw value
    is used as-is -- every check below already fails safely ("not a git
    repository", etc.) in that case.

    This is also the isolation boundary for multiple independent sessions
    sharing one long-lived container: each event is resolved from its own
    ``cwd`` alone, so a completion in ``/project/a`` can never touch
    ``/project/b``, and no state here is ever shared across workspaces.
    """
    cwd = Path(cwd_value) if isinstance(cwd_value, str) and cwd_value else Path.cwd()
    return git_toplevel(cwd) or cwd


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


# -- push preflight / guarded push -------------------------------------------

class PushDecision:
    def __init__(self, action: str, detail: str = ""):
        #: One of "pushed", "skipped", "failed".
        self.action = action
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"PushDecision({self.action!r}, {self.detail!r})"


class RemoteState:
    """What one bounded, live query of ``origin`` established.

    Every field is answered by the *same* ``git ls-remote`` round trip (see
    :func:`_query_origin`) -- there is no separate "trust the local cache"
    path for any of them. A cached ``refs/remotes/origin/HEAD`` symref is not
    authoritative evidence: it can go stale the moment the remote's default
    branch changes underneath it, and a decision this consequential must be
    based on what ``origin`` says *right now*, not what a prior
    ``clone``/``fetch`` happened to record locally.
    """

    def __init__(
        self, default_branch: str | None, default_branch_sha: str | None,
        branch_sha: str | None, branch_exists: bool,
    ):
        #: None means the query could not positively determine it -- callers
        #: must fail closed, never fall back to a local guess.
        self.default_branch = default_branch
        #: The current tip SHA of `origin`'s default branch, from the same
        #: `HEAD` line of the same round trip -- used only to bound the
        #: unpushed-range scan for a brand-new branch (see `push_preflight`),
        #: never for a push-safety decision.
        self.default_branch_sha = default_branch_sha
        #: The current SHA of ``refs/heads/<branch>`` on ``origin``, or None
        #: if ``branch_exists`` is False (a normal new-branch push).
        self.branch_sha = branch_sha
        self.branch_exists = branch_exists


def _effective_push_url(cwd: Path) -> str | None:
    """``origin``'s single effective push destination, or ``None``.

    ``origin`` can have a fetch URL that differs from its push URL
    (``remote.origin.pushurl``), including *multiple* push URLs, and either
    can be affected by ``url.<base>.pushInsteadOf``/``insteadOf``
    rewriting. ``git remote get-url --push --all`` is git's own
    authoritative resolver for this (rewriting included), so this never
    parses ``.git/config`` by hand.

    Returns ``None`` (fail closed) whenever there is not *exactly* one
    resolved push destination: zero (no remote/no usable URL) or more than
    one both mean the actual receiving repository can't be pinned down
    safely, and this module must never guess or push to more than one
    destination.
    """
    result = _run(["git", "remote", "get-url", "--push", "--all", "origin"], cwd=cwd)
    if result.returncode != 0:
        return None
    urls = [line for line in result.stdout.splitlines() if line.strip()]
    return urls[0] if len(urls) == 1 else None


def _query_origin(cwd: Path, branch: str, remote_url: str) -> RemoteState | None:
    """One bounded, read-only ``git ls-remote --symref <remote_url> HEAD
    refs/heads/<branch>`` call answering the remote's current default branch
    (name and tip SHA) and the current state of the actual push target, in a
    single network round trip.

    ``remote_url`` must be the resolved *push* destination from
    :func:`_effective_push_url`, not the bare name ``origin`` -- querying
    ``origin`` by name would consult its *fetch* URL, which can be a
    different repository than what ``git push`` actually reaches.

    Returns ``None`` on any failure, timeout, or output with no resolvable
    default-branch symref -- callers must treat that as "cannot be
    determined", never as "safe to push". A missing ``refs/heads/<branch>``
    line (with the query otherwise successful) means the remote confirmed
    the branch doesn't exist there yet, which is a normal new-branch push.
    """
    branch_ref = f"refs/heads/{branch}"
    try:
        result = _run(
            ["git", "ls-remote", "--symref", remote_url, "HEAD", branch_ref],
            cwd=cwd, timeout=REMOTE_QUERY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    default_branch: str | None = None
    default_branch_sha: str | None = None
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
        if ref == "HEAD" and sha:
            # The same query's plain `HEAD` line: its SHA is, by definition,
            # the current tip of whatever branch the `ref:` symref line above
            # names -- one round trip, no extra network call.
            default_branch_sha = sha
        if ref == branch_ref and sha:
            branch_sha = sha
            branch_exists = True
    if default_branch is None:
        # Ambiguous/unborn remote HEAD: cannot positively determine the
        # default branch from this query, so nothing here can be trusted.
        return None
    return RemoteState(default_branch, default_branch_sha, branch_sha, branch_exists)


def _worktree_clean(cwd: Path) -> bool:
    result = _run(["git", "status", "--porcelain"], cwd=cwd)
    return result.returncode == 0 and result.stdout.strip() == ""


def _is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    result = _run(["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=cwd)
    return result.returncode == 0


def _merge_base(cwd: Path, a: str, b: str) -> str | None:
    result = _run(["git", "merge-base", a, b], cwd=cwd)
    return result.stdout.strip() if result.returncode == 0 else None


# -- proving a local Flow-Next completion ------------------------------------

def changed_flow_task_ids(cwd: Path, base: str, head: str) -> list[str]:
    """Task ids whose tracked receipt (``.flow/tasks/<id>.md``) changed
    between ``base`` and ``head``.

    The task id is derived mechanically from the known, flat Flow-Next task
    path shape (the filename stem) -- never inferred from a commit message,
    a diff's content, or any prose. A nested path under ``.flow/tasks/`` is
    not a task receipt and is ignored.
    """
    result = _run(["git", "diff", "--name-only", base, head, "--", ".flow/tasks"], cwd=cwd)
    if result.returncode != 0:
        return []
    ids: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith(FLOW_TASKS_DIR) or not line.endswith(".md"):
            continue
        task_id = line[len(FLOW_TASKS_DIR):-len(".md")]
        if task_id and "/" not in task_id:
            ids.append(task_id)
    return ids


def flowctl_show(flowctl: Path, cwd: Path, task_id: str) -> dict[str, Any] | None:
    """``flowctl show <task_id> --json``, parsed -- or ``None`` on any
    failure (missing/non-executable Flowctl, non-zero exit, timeout,
    malformed JSON, or a JSON value that isn't an object).

    Argv-style only: ``task_id`` reaches the subprocess as one argv element
    and is never interpolated into a shell command string.
    """
    try:
        result = _run([str(flowctl), "show", task_id, "--json"], cwd=cwd, timeout=FLOWCTL_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


#: The only `status_source` value Flow-Next documents as authoritative --
#: the runtime Flow-Next state itself answered. `"committed"` means the
#: answer instead fell back to the tracked task snapshot, which Flow-Next
#: explicitly documents can be arbitrarily stale (for example on a fresh or
#: diff-scoped checkout that never ran Flow-Next's runtime state). Auto-push
#: is convenience-only, so that ambiguity must fail closed rather than being
#: treated as good enough.
AUTHORITATIVE_STATUS_SOURCE = "flow-state"


def task_confirmed_done(flowctl: Path, cwd: Path, task_id: str) -> bool:
    """Whether Flow-Next's own CLI currently reports ``task_id`` as
    authoritatively done.

    Trusts ``flowctl show``'s merged ``status`` outright rather than
    re-deriving Flow-Next's own runtime/committed status-merging rules here
    -- Flow-Next already decided which source is authoritative for this
    task and reports it as ``status_source``; duplicating that merge logic
    in agent-box would only risk disagreeing with it. What this function
    adds is refusing to *act* on a ``status_source`` other than
    :data:`AUTHORITATIVE_STATUS_SOURCE` -- a done status sourced from the
    committed snapshot (`"committed"`) or with no provenance at all is not
    trusted to authorize a push. There is no fallback: missing, unexpected,
    or absent provenance always means "not confirmed", never "assume
    authoritative".
    """
    data = flowctl_show(flowctl, cwd, task_id)
    return (
        isinstance(data, dict)
        and data.get("status") == "done"
        and data.get("status_source") == AUTHORITATIVE_STATUS_SOURCE
    )


def push_preflight(cwd: Path, auto_push_enabled: bool, flowctl: Path | None) -> PushDecision:
    """Every required guard, in order; the first failure decides the outcome.

    Returns a :class:`PushDecision` describing what happened (or would have
    happened) without performing the push itself -- see :func:`guarded_push`.
    There is no persistent state to invalidate: every call recomputes the
    answer from Git and Flow-Next's current state, so calling this again
    after a real push (or with nothing new completed) simply finds nothing
    to do.
    """
    if not auto_push_enabled:
        return PushDecision("skipped", "auto-push not enabled for this launch")
    if not is_flow_next_workspace(cwd):
        return PushDecision("skipped", "not a Flow-Next workspace")
    if _run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd).returncode != 0:
        return PushDecision("skipped", "not a git repository")

    branch = current_branch(cwd)
    if branch is None:
        return PushDecision("skipped", "HEAD is detached")
    if branch in PROTECTED_BRANCHES:
        return PushDecision("skipped", f"'{branch}' is a protected branch")
    if not _worktree_clean(cwd):
        return PushDecision("skipped", "worktree is dirty")

    head = head_sha(cwd)
    if head is None:
        return PushDecision("skipped", "HEAD could not be resolved")

    # `origin` can fetch from one repository and push to a different one
    # (remote.origin.pushurl, possibly URL-rewritten), so the safety query
    # below must be aimed at the exact repository `git push` will actually
    # reach -- never at `origin`'s fetch URL, and never at more than one
    # destination.
    push_url = _effective_push_url(cwd)
    if push_url is None:
        return PushDecision(
            "skipped",
            "origin does not resolve to exactly one push destination; refusing to auto-push")

    # One live, bounded query answers "what is the receiving repo's current
    # default branch" and "what does its <branch> currently look like" --
    # never from a local cache: a cached refs/remotes/origin/HEAD can go
    # stale the moment the remote's default branch changes.
    state = _query_origin(cwd, branch, push_url)
    if state is None:
        return PushDecision(
            "skipped", "current remote default branch could not be determined; refusing to auto-push")
    if branch == state.default_branch:
        return PushDecision("skipped", f"'{branch}' is the remote default branch")

    if state.branch_exists:
        if state.branch_sha == head:
            return PushDecision("skipped", "nothing new to push")
        if not _is_ancestor(cwd, state.branch_sha, "HEAD"):
            return PushDecision(
                "skipped", f"'origin/{branch}' is not an ancestor of HEAD; refusing to repair it")
        base = state.branch_sha
    else:
        # A brand-new branch: everything on it is "unpushed" relative to
        # origin. Bound the completion scan to this branch's own work by
        # diffing from its merge-base with the remote's default branch,
        # rather than the entire branch history back to the root commit.
        if state.default_branch_sha is None:
            return PushDecision("skipped", "could not determine a base commit for a new branch push")
        base = _merge_base(cwd, state.default_branch_sha, head) or state.default_branch_sha

    if flowctl is None:
        return PushDecision("skipped", "Flowctl could not be resolved for this launch")
    task_ids = changed_flow_task_ids(cwd, base, head)
    if not task_ids:
        return PushDecision("skipped", "no Flow-Next task files changed in the unpushed range")
    # A turn can legitimately touch more than one task's receipt (plan-sync
    # neighboring metadata, for instance) -- requiring *every* changed task
    # to be done would reject an otherwise-legitimate completion, so one
    # confirmed completion among the changed ones is sufficient.
    if not any(task_confirmed_done(flowctl, cwd, task_id) for task_id in task_ids):
        return PushDecision("skipped", "no confirmed Flow-Next task completion in the unpushed range")

    return PushDecision("ready")


def guarded_push(cwd: Path, auto_push_enabled: bool, flowctl: Path | None) -> PushDecision:
    """Run :func:`push_preflight` and, only when it says ``ready``, push.

    The only command this ever runs is ``git push -u origin HEAD`` -- never
    ``--force``/``--force-with-lease``, never another branch, never tags,
    never another remote. By the time this runs, :func:`push_preflight` has
    already proven ``origin`` resolves to exactly one push destination and
    queried that exact destination, so ``origin`` here is unambiguous --
    and because the safety settings ride on the command line rather than in
    the environment (:func:`_push_config_args`), this subprocess resolves
    ``origin`` from byte-identical configuration to the preflight queries,
    so the two cannot disagree about which repository receives the push.
    ``_PUSH_CONFIG_OVERRIDES`` additionally neutralizes the Git settings
    that could otherwise widen a plain push beyond that one branch (see its
    docstring) without touching the user's stored config. A push failure or
    timeout is simply reported; there is no marker or retry state to keep --
    the next completion event recomputes everything from scratch, and since
    the same local commits are still unpushed, it can try again the same way.
    """
    decision = push_preflight(cwd, auto_push_enabled, flowctl)
    if decision.action != "ready":
        return decision
    try:
        result = _run(
            ["git", *_push_config_args(), "push", "-u", "origin", "HEAD"], cwd=cwd, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return PushDecision("failed", "git push timed out")
    if result.returncode == 0:
        return PushDecision("pushed", "git push -u origin HEAD")
    return PushDecision("failed", (result.stderr or result.stdout or "git push failed").strip())


def _reconcile_from_env(cwd_value: Any) -> PushDecision:
    """Resolve the workspace/policy for one completion event and run the
    guarded push -- shared by every provider adapter below.

    Reads :data:`AUTO_PUSH_ENV`/:data:`FLOWCTL_ENV` from *this* process's
    environment, which is exactly the per-launch policy
    ``flow_next_launch_env`` forwarded when this agent was started (via
    ``podman exec --env`` for a container launch, or the host process
    environment for ``--host``) -- never a value stored or shared across
    workspaces.
    """
    root = _hook_workspace_root(cwd_value)
    auto_push_enabled = bool(os.environ.get(AUTO_PUSH_ENV, "").strip())
    flowctl_value = os.environ.get(FLOWCTL_ENV, "").strip()
    flowctl = Path(flowctl_value) if flowctl_value else None
    try:
        decision = guarded_push(root, auto_push_enabled, flowctl)
    except Exception as exc:  # noqa: BLE001 - the outermost safety net, see docstring below
        decision = PushDecision("failed", f"unexpected error: {exc}")
    if decision.action in ("pushed", "failed"):
        print(f"agent-box flow-next: {decision.action}: {decision.detail}", file=sys.stderr)
    return decision


def run_stop_hook(stdin_text: str) -> PushDecision:
    """The Claude ``Stop`` hook entry point. Never raises and never blocks
    the stop.

    Every operational failure -- a timeout, a missing ``git``, an
    unexpected exception anywhere in the guard chain -- is caught in
    :func:`_reconcile_from_env` and turned into a ``"failed"`` decision,
    rather than letting an exception escape into Claude's hook runner.
    """
    try:
        payload = json.loads(stdin_text)
    except json.JSONDecodeError:
        payload = {}
    cwd_value = payload.get("cwd") if isinstance(payload, dict) else None
    return _reconcile_from_env(cwd_value)


def run_codex_turn_complete_hook(payload_text: str) -> PushDecision | None:
    """The Codex turn-completion notify adapter.

    Codex's ``notify`` program is invoked for every notify event with one
    JSON payload argument; only ``"agent-turn-complete"`` is a real turn
    completion, so anything else (and any payload that fails to parse) is a
    silent no-op, returning ``None`` rather than a decision. This mirrors
    :func:`run_stop_hook` using Codex's own completion event and its own
    ``cwd`` instead of Claude's, but drives the identical shared reconciler.
    """
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("type") != "agent-turn-complete":
        return None
    return _reconcile_from_env(payload.get("cwd"))


# -- managed Claude hooks -----------------------------------------------------

def _stop_entry() -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": STOP_COMMAND}]}


def install_flow_push_hooks(settings_path: Path) -> None:
    """Install the Claude ``Stop`` hook that drives auto-push.

    Independent of the notification hooks in ``agent_box.notify`` -- both may
    coexist under the same ``Stop`` event, each identified by its own stable
    command string, and installing/removing one never touches the other or
    any unrelated hook the user configured by hand. Idempotent: running this
    again with the hook already present changes nothing. Deliberately never
    installs a ``StopFailure`` hook -- a failed Claude turn must never push.
    Codex needs no managed-settings hook: its turn-complete event is wired
    through the ``-c notify=[...]`` launch argument in ``agent_box.runtime``.
    """
    from .claude_settings import install_hook_entries
    install_hook_entries(settings_path, [("Stop", _stop_entry())])


def remove_flow_push_hooks(settings_path: Path) -> None:
    """Remove only the hook installed by :func:`install_flow_push_hooks`."""
    from .claude_settings import remove_hook_commands
    remove_hook_commands(settings_path, {"Stop": (STOP_COMMAND,)})
