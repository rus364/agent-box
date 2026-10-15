"""Local fast-forward landing with Git and tracker state as the authorities."""
from __future__ import annotations

import subprocess

from . import git_workflow as gw
from .branch_cleanup import after_done
from .errors import AgentBoxError
from .tracker.core import blocking_reasons, completion_reasons


def _constraints(adapter, task):
    from .workflow import tree

    tasks = tree(adapter, task)
    reasons = completion_reasons(task, tasks, str(adapter.identity()["id"]), states=("In review",))
    reasons += blocking_reasons(task, adapter.get)
    return reasons, tasks


def _publish(root, config, task, base, task_head):
    if not config.workflow.auto_push:
        return {"status": "disabled", "pushed": False}
    try:
        return {"status": "published", **gw.push_local_base(root, base, task_head)}
    except (AgentBoxError, OSError, subprocess.TimeoutExpired) as exc:
        raise AgentBoxError(
            f"Local merge succeeded, but base publication failed: {exc}. "
            f"Task remains In review; preserve its branch and run workflow land {task.id} merge "
            "again after resolving the publication failure; it continues from the merge") from None


def operate(args, root, config, adapter, task):
    """Consume the fresh task while workflow.landing owns the acceptance locks."""
    if args.command == "pr":
        raise AgentBoxError("Pull requests are unavailable in local merge mode")
    if getattr(args, "method", None) is not None:
        raise AgentBoxError("--method is incompatible with local merge mode; local merge is fast-forward-only")
    from .workflow import (acceptance_reasons, configuration_summary, delta_review, diff_stats,
                           evidence, read_evidence, review_reasons, resolve, scope)

    parent, info, _, _ = resolve(root, adapter, task)
    if task.id != parent.id:
        raise AgentBoxError("Land the parent task after all subtasks are integrated")
    gw.require_clean(root)
    branch, base = info["branch"], info["base"]
    if branch == base:
        raise AgentBoxError("Task branch cannot be the base branch")
    task_head, base_head = gw.local_branch_head(root, branch), gw.local_branch_head(root, base)
    current = gw.current_branch(root)
    # Merging is one idempotent step: an accepted head the base already contains
    # skips it and the run continues with publication, Done and cleanup.
    integrated = gw._is_ancestor(root, task_head, base_head)
    if not integrated and current != branch:
        raise AgentBoxError(f"Switch to the parent branch {branch} before local landing")
    if integrated and current not in (branch, base):
        raise AgentBoxError(f"Switch to {base} or {branch} before completing the merged parent")
    # Before the merge Git still names the fork point and must agree with what
    # acceptance recorded; afterwards only that record names the original diff.
    fork = None if integrated else gw.base_commit(root, base)
    saved = read_evidence(root, task) if integrated else evidence(root, task, scope(root, base), base)
    reasons, tasks = _constraints(adapter, task)
    delta = delta_review(root, config, task, tasks, head=task_head)
    reasons += acceptance_reasons(root, config.workflow.review, tasks,
                                  {**saved, "delta_review": delta}, task_head, fork)
    if not integrated and not gw._is_ancestor(root, base_head, task_head):
        reasons.append(f"Local base {base} and task branch {branch} have diverged")
    if args.action == "inspect":
        files, lines = diff_stats(root, base, config.workflow.review)
        return {"task": task.id, "branch": branch, "base": base,
                "head": task_head, "base_head": base_head,
                "integrated": integrated, "blockers": reasons,
                "configuration": configuration_summary(config), "scope": saved.get("scope"),
                "review": {"files": len(files), "lines": lines,
                           "reasons": review_reasons(config.workflow.review, tasks, files, lines),
                           "verdict": saved.get("review", {}).get("verdict"), "delta": delta},
                "max_rounds": config.workflow.review.max_rounds}
    if reasons:
        raise AgentBoxError("Cannot merge locally: " + "; ".join(reasons))
    if gw.local_branch_head(root, branch) != task_head:
        raise AgentBoxError("Task branch moved after acceptance was checked; inspect it again")
    if current != base:
        gw.git(root, "switch", base)
    if not integrated:
        gw.git(root, "merge", "--ff-only", task_head)
        if gw.local_branch_head(root, base) != task_head:
            raise AgentBoxError("Local base did not advance to the validated task head")
    if gw.local_branch_head(root, branch) != task_head:
        raise AgentBoxError("Task branch moved during local merge; inspect it before completion")
    publication = _publish(root, config, task, base, task_head)
    adapter.set_state(task, "Done")
    return after_done({"task": task.id, "state": "Done",
                       "merge": {"base": base, "head": task_head}, "publication": publication},
                      root, config, task, adapter, branch=branch, base=base, expected_head=task_head)
