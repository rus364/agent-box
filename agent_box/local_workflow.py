"""Local fast-forward landing with Git and tracker state as the authorities."""
from __future__ import annotations

import subprocess

from . import git_workflow as gw
from .branch_cleanup import after_done
from .errors import AgentBoxError
from .tracker.core import blocking_reasons, completion_reasons


def _constraints(adapter, task, *, states=("Review",)):
    from .workflow import tree

    tasks = tree(adapter, task)
    reasons = completion_reasons(task, tasks, str(adapter.identity()["id"]), states=states)
    reasons += blocking_reasons(task, adapter.get)
    return reasons, tasks


def _acceptance_reasons(config, tasks, accepted):
    from .workflow import review_blockers

    reasons = []
    if not accepted.get("validation") or accepted["validation"].get("exit") != 0:
        reasons.append("Local validation for the current scope is missing")
    # An already-integrated scope has lost its original diff. Auto cannot infer
    # that the work was small from an empty diff against the advanced base.
    reasons += review_blockers(config.review, tasks, accepted.get("files", []), accepted.get("lines", 0),
                               accepted, allow_human=True, integrated=accepted.get("integrated_scope", False))
    return reasons


def _recovery_reasons(config, tasks, saved, base, task_head):
    from .workflow import requirements, scope_requirements

    accepted = saved.get("local_acceptance", {})
    recovery = ("Switch to the task branch, run fresh validation with workflow validate "
                "and independent review with ab-review as required, then run workflow land "
                "PARENT merge; it rechecks acceptance before returning to the base")
    if not accepted:
        return ["Pre-merge acceptance cache is missing; " + recovery]
    reasons = []
    if accepted.get("head") != task_head:
        reasons.append("Accepted task head has changed; " + recovery)
    if accepted.get("base") != base:
        reasons.append("Accepted base identity has changed; " + recovery)
    if scope_requirements(accepted.get("requirements") or []) != scope_requirements(requirements(tasks)):
        reasons.append("Accepted task requirements have changed; " + recovery)
    reasons += _acceptance_reasons(config, tasks, accepted)
    return reasons


def _identity(root, info):
    branch, base = info["branch"], info["base"]
    if branch == base:
        raise AgentBoxError("Task branch cannot be the base branch")
    return branch, base, gw.local_branch_head(root, branch), gw.local_branch_head(root, base)


def _publish(root, config, task, base, task_head):
    if task.state == "Done":
        return {"status": "not-required", "pushed": False}
    if not config.workflow.auto_push:
        return {"status": "disabled", "pushed": False}
    try:
        return {"status": "published", **gw.push_local_base(root, base, task_head)}
    except (AgentBoxError, OSError, subprocess.TimeoutExpired) as exc:
        raise AgentBoxError(
            f"Local merge succeeded, but base publication failed: {exc}. "
            f"Task remains Review; preserve its branch and retry workflow land {task.id} reconcile "
            "after resolving the publication failure") from None


def operate(args, root, config, adapter, task):
    """Consume the fresh task while workflow.landing owns the acceptance locks."""
    if args.command == "pr":
        raise AgentBoxError("Pull requests are unavailable in local merge mode")
    if getattr(args, "method", None) is not None:
        raise AgentBoxError("--method is incompatible with local merge mode; local merge is fast-forward-only")
    from .workflow import read_evidence, resolve

    parent, info, _, _ = resolve(root, adapter, task)
    if task.id != parent.id:
        raise AgentBoxError("Land the parent task after all subtasks are integrated")
    if args.action == "request-review":
        provider = config.workflow.review.github_provider
        if provider in ("codex", "claude"):
            raise AgentBoxError("Cloud review is unavailable in local merge mode; run ab-review locally")
        return {"provider": "local", "next": "Run ab-review in fresh context"}
    saved = read_evidence(root, task)
    gw.require_clean(root)
    branch, base, task_head, base_head = _identity(root, info)
    current = gw.current_branch(root)
    if args.action == "reconcile":
        if current != base:
            raise AgentBoxError(f"Switch to local base branch {base} before reconciling")
        reasons, tasks = _constraints(adapter, task, states=("Review", "Done"))
        reasons += _recovery_reasons(config.workflow, tasks, saved, base, task_head)
        if gw.local_branch_head(root, branch) != task_head:
            reasons.append("Task head has changed during reconciliation; inspect it again")
        if not gw._is_ancestor(root, task_head, gw.local_branch_head(root, base)):
            reasons.append(
                f"Current task branch {branch} is not integrated into local base {base}")
        if reasons:
            raise AgentBoxError("Cannot reconcile local merge: " + "; ".join(reasons))
        publication = _publish(root, config, task, base, task_head)
        if task.state != "Done":
            adapter.set_state(task, "Done")
        return after_done({"task": task.id, "state": "Done",
                           "merge": {"base": base, "head": task_head}, "publication": publication},
                          root, config, task, adapter, branch=branch, base=base, expected_head=task_head)

    if current != branch:
        raise AgentBoxError(f"Switch to the parent branch {branch} before local landing")
    from .workflow import configuration_summary, diff_stats, requirements, review_reasons, save_evidence, scope

    reasons, tasks = _constraints(adapter, task)
    integrated = gw._is_ancestor(root, task_head, base_head)
    accepted = saved if saved.get("scope") == scope(root, base, tasks) else {}
    files, lines = diff_stats(root, base, config.workflow.review)
    snapshot = {"head": task_head, "base": base, "base_head": base_head,
                "requirements": requirements(tasks), "scope": accepted.get("scope"),
                "files": files, "lines": lines, "integrated_scope": integrated,
                "validation": accepted.get("validation", {}),
                "review": accepted.get("review", {}),
                "human_acceptance": accepted.get("human_acceptance", {})}
    reasons += _acceptance_reasons(config.workflow, tasks, snapshot)
    if not integrated and not gw._is_ancestor(root, base_head, task_head):
        reasons.append(f"Local base {base} and task branch {branch} have diverged")
    if args.action == "inspect":
        return {"task": task.id, "branch": branch, "base": base,
                "head": task_head, "base_head": base_head,
                "integrated": integrated, "blockers": reasons,
                "configuration": configuration_summary(config), "scope": accepted.get("scope"),
                "review": {"files": len(files), "lines": lines,
                           "reasons": review_reasons(config.workflow.review, tasks, files, lines)},
                "max_rounds": config.workflow.review.max_rounds}
    if reasons:
        raise AgentBoxError("Cannot merge locally: " + "; ".join(reasons))
    if gw.local_branch_head(root, branch) != task_head:
        raise AgentBoxError("Task branch moved after acceptance was checked; inspect it again")
    # This single disposable snapshot carries acceptance across the base
    # fast-forward. It never establishes integration or selects a Git ref.
    saved["local_acceptance"] = snapshot
    save_evidence(root, task, saved)
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
    publication = _publish(root, config, task, base, task_head)
    if task.state != "Done":
        adapter.set_state(task, "Done")
    return after_done({"task": task.id, "state": "Done",
                       "merge": {"base": base, "head": task_head}, "publication": publication},
                      root, config, task, adapter, branch=branch, base=base, expected_head=task_head)
