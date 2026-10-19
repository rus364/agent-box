"""Preserve unfinished work without inventing a second task lifecycle."""
from __future__ import annotations

import subprocess

from . import git_workflow as gw
from .branch_cleanup import _remote_refs
from .errors import AgentBoxError
from .tracker.core import blocking_reasons, root_task


def owned_tasks(adapter, task):
    parent = root_task(task, adapter.get)
    tasks = [parent] if task.id == parent.id else [parent, task]
    user = str(adapter.identity()["id"])
    for item in tasks:
        if item.state not in ("Backlog", "Ready", "In progress", "In review") or item.assignees != [user]:
            raise AgentBoxError(f"Task {item.id} must be open and assigned only to the current user")
    return tasks


def snapshot(root, tasks, *, local_only=False):
    """A stale tracking ref can diagnose ambiguity, but cannot resurrect lost work."""
    url = gw.push_destination(root)
    branches, warnings, base = [], [], None
    for task in tasks:
        known = gw.task_branch(root, task)
        local = gw.task_branches(root, task.id)
        try:
            remote, symbols, default, _ = _remote_refs(root, url, gw.branch_prefix(task.id))
        except (AgentBoxError, OSError, subprocess.TimeoutExpired):
            if not local_only or not local:
                raise
            remote, symbols, default = {}, set(), gw.default_base(root)
            warnings.append(f"Task {task.id}: origin could not be verified; only the local copy is preserved")
        if base is not None and base != default:
            raise AgentBoxError("Origin's default branch changed during inspection; retry")
        base = default
        names = set(local) | set(remote) | ({known} if known else set())
        if len(names) != 1:
            raise AgentBoxError(f"Task {task.id} has missing or ambiguous branches; recover existing work before resuming")
        name = names.pop()
        ref = f"refs/heads/{name}"
        if name == base or ref in symbols:
            raise AgentBoxError(f"Preserving unsafe base or symbolic task branch {name}")
        local_head = None
        if local:
            if gw._run(["git", "symbolic-ref", "-q", ref], cwd=root).returncode != 1:
                raise AgentBoxError(f"Preserving symbolic or unreadable task branch {name}")
            local_head = gw.local_branch_head(root, name)
        remote_head = remote.get(name)
        if not local_head and not remote_head:
            raise AgentBoxError(f"Only a stale tracking ref remains for {name}; recover existing work")
        if local_head and remote_head and not gw._is_ancestor(root, remote_head, local_head):
            raise AgentBoxError(f"Origin/{name} is ahead, diverged or not fetched; reconcile before continuing")
        branches.append({"task": task.id, "branch": name, "head": local_head or remote_head,
                         "local": bool(local_head), "remote_head": remote_head})
    return url, base, branches, warnings


def verify_local(root, branches):
    for item in branches:
        if not item["local"] or gw.local_branch_head(root, item["branch"]) != item["head"]:
            raise AgentBoxError(f"Task branch {item['branch']} moved or disappeared; work was not discarded")


def verify_remote(root, url, base, branches):
    for item in branches:
        heads, symbols, default, _ = _remote_refs(root, url, gw.branch_prefix(item["task"]))
        if default != base or symbols or heads != {item["branch"]: item["head"]}:
            raise AgentBoxError("Origin checkpoint could not be verified; inspect remote refs before retrying")


def pause(root, adapter, task, *, push, reason, resume_when):
    if not reason.strip() or not resume_when.strip():
        raise AgentBoxError("Pause requires a non-empty reason and resumption condition")
    gw.require_clean(root)
    tasks = owned_tasks(adapter, task)
    url, base, branches, warnings = snapshot(root, tasks, local_only=not push)
    if not all(item["local"] for item in branches):
        raise AgentBoxError("Pause needs existing local task branches; resume the remote work first")
    if gw.current_branch(root) not in {base, *(item["branch"] for item in branches)}:
        raise AgentBoxError("Switch to this task's branch or the base before pausing; unrelated work is preserved")
    base_head = gw.local_branch_head(root, base)
    if f"branch refs/heads/{base}" in gw.git(root, "worktree", "list", "--porcelain").splitlines() and gw.current_branch(root) != base:
        raise AgentBoxError(f"Base {base} is occupied by another worktree; release it before pausing")
    published = False
    try:
        if push:
            verify_local(root, branches)
            # Frozen object IDs and explicit non-force refs avoid ambient mirror,
            # matching, tag and submodule push settings. No tracker state changes.
            gw.git(root, *gw._url_push_config_args(root, url), "push", "--no-mirror", "--no-force",
                   "--no-follow-tags", "--recurse-submodules=no", url,
                   *(f"{item['head']}:refs/heads/{item['branch']}" for item in branches), timeout=120)
            verify_remote(root, url, base, branches)
            published = True
        verify_local(root, branches)
        if [item.id for item in owned_tasks(adapter, adapter.get(task.id))] != [item.id for item in tasks]:
            raise AgentBoxError("Task hierarchy changed during pause; inspect before retrying")
        gw.require_clean(root)
        gw.git(root, "switch", base)
        verify_local(root, branches)
        gw.require_clean(root)
        if gw.current_branch(root) != base or gw.local_branch_head(root, base) != base_head:
            raise AgentBoxError("Base changed during pause; inspect the checkout before continuing")
        adapter.comment(task.id, f"Stopped work: {reason.strip()}\n\nResume when: {resume_when.strip()}\n\n"
                        + ("Verified a remote copy of the task work." if published else
                           "Preserved the task work locally only; no remote backup was established.")
                        + " Returned the clean checkout to the base. Lifecycle, assignment and dependencies are unchanged.")
    except Exception as exc:
        raise AgentBoxError(f"Pause did not complete: {exc}. Task refs were not deleted; "
                            f"current branch is {gw.current_branch(root)}. "
                            f"Remote checkpoint {'was verified' if published else 'is not confirmed (a push may have succeeded)'}. "
                            "Reread Git and tracker journal before retrying; no lifecycle change was requested.") from exc
    return {"task": task.id, "state": task.state, "lifecycle_changed": False, "base": base,
            "current_branch": gw.current_branch(root), "branches": branches,
            "preservation": "remote-verified" if published else "local-only", "warnings": warnings,
            "next": f"When the user resumes work, run agent-box workflow resume {task.id}; "
                    "Backlog tasks first need the user's decision to return to Ready"}


def resume(root, adapter, task):
    from .workflow import begin

    gw.require_clean(root)
    tasks = owned_tasks(adapter, task)
    for item in tasks:
        if item.state == "Backlog":
            raise AgentBoxError(f"Ask the user before returning task {item.id} to Ready, then resume")
    blockers = blocking_reasons(task, adapter.get)
    if blockers:
        raise AgentBoxError("; ".join(blockers))
    url, base, branches, warnings = snapshot(root, tasks)
    if gw.current_branch(root) not in {base, *(item["branch"] for item in branches)}:
        raise AgentBoxError("Switch to the base or this task's branch before resuming")
    for item in branches:
        if not item["local"]:
            # Fetch objects only; no arbitrary fetch mapping may overwrite refs.
            gw.git(root, "fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
                   url, f"refs/heads/{item['branch']}", timeout=120)
            heads, symbols, default, _ = _remote_refs(root, url, gw.branch_prefix(item["task"]))
            if default != base or symbols or heads != {item["branch"]: item["head"]}:
                raise AgentBoxError("Remote branch moved during recovery; retry from fresh Git state")
            gw.git(root, "branch", item["branch"], item["head"])
            item["local"] = True
    verify_local(root, branches)
    # Existing begin owns claim/resume and provenance. It may not create a new
    # base-derived branch here: both expected refs were resolved and preserved.
    return {**begin(root, adapter, adapter.get(task.id), resume=True, existing_only=True), "warnings": warnings,
            "next": f"Run agent-box workflow inspect {task.id}; revalidate if the accepted scope changed"}
