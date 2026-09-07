"""Local fast-forward landing with Git and tracker state as the authorities."""
from __future__ import annotations

from . import git_workflow as gw
from .branch_cleanup import after_done
from .errors import AgentBoxError
from .tracker.core import blocking_reasons, completion_reasons
from .workflow_state import lock


def _constraints(root, base, workflow_config, adapter, task, saved, *, require_acceptance,
                 states=("Review",)):
    from .workflow import diff_stats, review_required, scope, tree

    tasks = tree(adapter, task)
    reasons = completion_reasons(task, tasks, str(adapter.identity()["id"]), states=states)
    reasons += blocking_reasons(task, adapter.get)
    if require_acceptance:
        accepted = saved if saved.get("scope") == scope(root, base, tasks) else {}
        if not accepted.get("validation"):
            reasons.append("Local validation for the current scope is missing")
        files, lines = diff_stats(root, base)
        if (review_required(workflow_config.review, tasks, files, lines)
                and accepted.get("review", {}).get("verdict") != "PASS"):
            reasons.append("This scope requires a fresh independent PASS review")
    return reasons


def _identity(root, info):
    branch, base = info["branch"], info["base"]
    if branch == base:
        raise AgentBoxError("Task branch cannot be the base branch")
    return branch, base, gw.local_branch_head(root, branch), gw.local_branch_head(root, base)


def operate(args, root, config, adapter, task, parent, info, saved):
    if task.id != parent.id:
        raise AgentBoxError("Land the parent task after all subtasks are integrated")
    if args.command == "pr":
        raise AgentBoxError("Pull requests are unavailable in local merge mode")
    if getattr(args, "method", None) is not None:
        raise AgentBoxError("--method is incompatible with local merge mode; local merge is fast-forward-only")
    if args.action == "request-review":
        provider = config.workflow.review.github_provider
        if provider in ("codex", "claude"):
            raise AgentBoxError("Cloud review is unavailable in local merge mode; run ab-review locally")
        return {"provider": "local", "next": "Run ab-review in fresh context"}

    with lock(root, "work"):
        gw.require_clean(root)
        task = adapter.get(task.id)
        branch, base, task_head, base_head = _identity(root, info)
        current = gw.current_branch(root)
        if args.action == "reconcile":
            if current != base:
                raise AgentBoxError(f"Switch to local base branch {base} before reconciling")
            reasons = _constraints(root, base, config.workflow, adapter, task, saved,
                                   require_acceptance=False, states=("Review", "Done"))
            if not gw._is_ancestor(root, task_head, base_head):
                reasons.append(
                    f"Current task branch {branch} is not integrated into local base {base}")
            if reasons:
                raise AgentBoxError("Cannot reconcile local merge: " + "; ".join(reasons))
            if task.state != "Done":
                adapter.set_state(task, "Done")
            return after_done({"task": task.id, "state": "Done",
                               "merge": {"base": base, "head": task_head}},
                              root, config, task, branch=branch, base=base, expected_head=task_head)

        if current != branch:
            raise AgentBoxError(f"Switch to the parent branch {branch} before local landing")
        reasons = _constraints(root, base, config.workflow, adapter, task, saved,
                               require_acceptance=True)
        integrated = gw._is_ancestor(root, task_head, base_head)
        if not integrated and not gw._is_ancestor(root, base_head, task_head):
            reasons.append(f"Local base {base} and task branch {branch} have diverged")
        if args.action == "inspect":
            return {"task": task.id, "branch": branch, "base": base,
                    "head": task_head, "base_head": base_head,
                    "integrated": integrated, "blockers": reasons,
                    "max_rounds": config.workflow.review.max_rounds}
        if reasons:
            raise AgentBoxError("Cannot merge locally: " + "; ".join(reasons))
        if gw.local_branch_head(root, branch) != task_head:
            raise AgentBoxError("Task branch moved after acceptance was checked; inspect it again")
        if not integrated:
            gw.git(root, "switch", base)
            gw.git(root, "merge", "--ff-only", task_head)
            base_head = gw.local_branch_head(root, base)
            if base_head != task_head:
                raise AgentBoxError("Local base did not advance to the validated task head")
        elif current != base:
            gw.git(root, "switch", base)
        if gw.local_branch_head(root, branch) != task_head:
            raise AgentBoxError("Task branch moved during local merge; reconcile before completion")
        if task.state != "Done":
            adapter.set_state(task, "Done")
        return after_done({"task": task.id, "state": "Done",
                           "merge": {"base": base, "head": task_head}},
                          root, config, task, branch=branch, base=base, expected_head=task_head)
