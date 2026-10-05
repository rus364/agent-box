"""Optional task-tree cleanup with live integration proof for every ref it deletes."""
from __future__ import annotations

import subprocess

from . import git_workflow as gw
from .errors import AgentBoxError


def _local_refs(root, prefix):
    rows = gw.git(root, "for-each-ref", "--format=%(refname)%09%(objectname)%09%(symref)",
                  f"refs/heads/{prefix}*", f"refs/remotes/origin/{prefix}*")
    local, names = {}, set()
    for row in rows.splitlines():
        ref, oid, symbolic = (row.split("\t") + [""])[:3]
        if ref.startswith("refs/heads/"):
            name = ref.removeprefix("refs/heads/")
            local[name] = (oid, symbolic)
        else:
            name = ref.removeprefix("refs/remotes/origin/")
        names.add(name)
    return local, names


def _remote_refs(root, url, prefix):
    rows = gw.git(root, "ls-remote", "--symref", url, "HEAD", f"refs/heads/{prefix}*",
                  timeout=gw.REMOTE_QUERY_TIMEOUT)
    refs, symbols, default, base_head = {}, set(), None, None
    for row in rows.splitlines():
        value, ref = row.split("\t", 1)
        if value.startswith("ref: "):
            if ref == "HEAD":
                default = value.removeprefix("ref: refs/heads/")
            else:
                symbols.add(ref)
        elif ref == "HEAD":
            base_head = value
        elif ref.startswith(f"refs/heads/{prefix}"):
            refs[ref.removeprefix("refs/heads/")] = value
    if not default or not base_head:
        raise AgentBoxError("Cannot establish origin's live default branch and published head")
    return refs, symbols, default, base_head


def _unoccupied(root, branch, base):
    ref = f"refs/heads/{branch}"
    if branch == base:
        raise AgentBoxError("Preserving the base/default branch")
    if gw.current_branch(root) == branch:
        raise AgentBoxError(f"Preserving the current branch; switch to {base} and retry cleanup")
    rows = gw.git(root, "worktree", "list", "--porcelain")
    if f"branch {ref}" in rows.splitlines():
        raise AgentBoxError("Preserving branch used by a worktree; release it before retrying cleanup")
    result = gw._run(["git", "symbolic-ref", "-q", ref], cwd=root)
    if result.returncode != 1:
        raise AgentBoxError("Preserving symbolic ref or unreadable branch")


def merged_identity(pr, branch, base, expected=None):
    if (pr.get("state") != "MERGED" or not pr.get("mergedAt")
            or not (pr.get("mergeCommit") or {}).get("oid")
            or pr.get("headRefName") != branch or pr.get("baseRefName") != base
            or not pr.get("headRefOid")
            or expected is not None and pr["headRefOid"] != expected):
        raise AgentBoxError("Exact merged PR head/base and merge identity are not confirmed")
    return pr["headRefOid"]


def _merged_head(root, url, branch, base, *, prefix=None):
    from . import github_workflow as github
    repo = gw.github_repository(url)
    head_filter = ("--head", branch) if branch else ()
    rows = github.gh(root, "pr", "list", "--repo", repo, *head_filter,
                     "--base", base, "--state", "all", "--limit", "1000",
                     "--json", "number,state,headRefName", json_output=True)
    relevant = [row for row in rows if row["state"] in ("OPEN", "MERGED")
                and (branch or row["headRefName"].startswith(prefix))]
    if len(rows) >= 1000 or len(relevant) != 1 or relevant[0]["state"] != "MERGED":
        raise AgentBoxError("Merged PR is missing or ambiguous; preserving the branch")
    return merged_identity(github.view(root, repo, relevant[0]["number"]),
                           branch or relevant[0]["headRefName"], base)


def _integrated_head(root, head, base, merged_head=None, *, exact=True):
    """A deletable branch is in the base: exactly what landed, or history that head contains.

    A squash or rebase merge rewrites the commit, so in GitHub mode only the
    merged PR head names what landed; the parent branch has to be it, and a
    subtask branch has to be inside it.
    """
    if merged_head is None:
        if not gw._is_ancestor(root, head, gw.local_branch_head(root, base)):
            raise AgentBoxError("Task head is not integrated into the local base; fetch missing objects if needed")
    elif head != merged_head and (exact or not gw._is_ancestor(root, head, merged_head)):
        raise AgentBoxError("Branch head is not part of the exact merged PR head")


def _merged_parent(root, config, task, branch, base, expected_head):
    """The parent head GitHub actually merged; local mode proves inclusion from the base itself."""
    if config.workflow.merge_mode != "github":
        return None
    if expected_head is not None:
        return expected_head
    if not (config.credentials.github.token or config.credentials.github.config):
        raise AgentBoxError("Enable GitHub integration before GitHub-mode cleanup")
    return _merged_head(root, gw.push_destination(root), branch, base or gw.default_base(root),
                        prefix=gw.branch_prefix(task.id))


def _cleanup_branch(root, config, task, *, branch=None, base=None, expected_head=None,
                    merged=None, exact=True, preserve_reason=None):
    """Return independent outcomes, including failures, after a successful Done write.

    A missing ref permits only a no-op; every surviving candidate proves its own
    inclusion in the base again, whatever the caller already established.
    """
    enabled = {"local": config.workflow.cleanup_local_branches,
               "remote": config.workflow.cleanup_remote_branches}
    result = {kind: {"status": "pending" if value else "disabled"}
              for kind, value in enabled.items()}
    if not any(enabled.values()):
        return result
    try:
        prefix = gw.branch_prefix(task.id)
        local, names = _local_refs(root, prefix)
        remote, symbols, url, published, remote_base = {}, set(), None, None, None
        if enabled["remote"]:
            try:
                url = gw.push_destination(root)
                remote, symbols, remote_base, published = _remote_refs(root, url, prefix)
                names.update(remote)
            except (AgentBoxError, OSError, subprocess.TimeoutExpired) as exc:
                result["remote"] = {"status": "error", "reason": str(exc)}
        if preserve_reason is not None:
            for kind, refs in (("local", local), ("remote", remote)):
                if result[kind]["status"] != "pending":
                    continue
                candidates = [{"branch": name, "head": refs[name][0] if kind == "local" else refs[name]}
                              for name in sorted(refs)]
                if not candidates:
                    result[kind] = {"status": "absent"}
                elif len(candidates) == 1:
                    result[kind] = {"status": "preserved", "reason": preserve_reason, **candidates[0]}
                else:
                    result[kind] = {"status": "error", "reason": "Ambiguous task namespace; " + preserve_reason,
                                    "candidates": candidates}
            return result
        if branch:
            names.add(branch)
        if len(names) > 1:
            raise AgentBoxError("Ambiguous task namespace; preserving all candidates: " + ", ".join(sorted(names)))
        branch = next(iter(names), None)
        for kind, refs in (("local", local), ("remote", remote)):
            if result[kind]["status"] == "pending" and branch not in refs:
                result[kind] = {"status": "absent"}
        if not any(result[k]["status"] == "pending" for k in enabled):
            return result
        if not branch.startswith(prefix):
            raise AgentBoxError("Branch is outside the task namespace")
        result["branch"] = branch
        default = remote_base or gw.default_base(root)
        base = base or default
        if branch in (base, default):
            raise AgentBoxError("Preserving the base/default branch")
        _unoccupied(root, branch, base)
        merged_head = merged() if merged is not None else None
        for kind in enabled:
            if result[kind]["status"] != "pending":
                continue
            try:
                ref = f"refs/heads/{branch}"
                head = local[branch][0] if kind == "local" else remote[branch]
                if (kind == "local" and local[branch][1]) or (kind == "remote" and ref in symbols):
                    raise AgentBoxError("Preserving symbolic ref")
                if expected_head is not None and head != expected_head:
                    raise AgentBoxError("Branch head moved since merge; preserving new work")
                _integrated_head(root, head, base, merged_head, exact=exact)
                if config.workflow.merge_mode == "local" and kind == "remote" and (
                        remote_base != gw.default_base(root) or not gw._is_ancestor(root, head, published)):
                    raise AgentBoxError("Task head is not proven in the live published origin base; publish the base or fetch missing objects, then retry")
                _unoccupied(root, branch, base)
                if kind == "local":
                    gw.git(root, "update-ref", "--no-deref", "-d", ref, head)
                else:
                    gw.git(root, *gw._url_push_config_args(root, url), "push", "--no-mirror", "--no-follow-tags",
                           "--recurse-submodules=no", f"--force-with-lease={ref}:{head}",
                           url, f":{ref}", timeout=120)
                result[kind] = {"status": "deleted", "head": head}
            except (AgentBoxError, OSError, subprocess.TimeoutExpired) as exc:
                result[kind] = {"status": "error", "reason": str(exc)}
    except Exception as exc:
        # Cleanup cannot undo a successful merge and tracker write, even if an
        # unexpected transport response prevents us from constructing proof.
        for kind in enabled:
            if result[kind]["status"] == "pending":
                result[kind] = {"status": "error", "reason": str(exc)}
    return result


def cleanup(root, config, task, adapter, **identity):
    """Discover only live descendants and isolate optional failures from Done."""
    descendants = []
    discovery_error = None
    if config.workflow.cleanup_local_branches or config.workflow.cleanup_remote_branches:
        try:
            from .workflow import tree
            tasks = tree(adapter, adapter.get(task.id))
            by_id = {item.id: item for item in tasks}
            for parent in tasks:
                if any(by_id[child].parent != parent.id for child in parent.children):
                    raise AgentBoxError("Inconsistent live task hierarchy; preserving descendant branches")
            descendants = [item for item in tasks if item.id != task.id]
        except Exception as exc:
            discovery_error = str(exc)

    merged_head = None
    resolved = False

    def merged():
        # One live lookup serves every branch, and only if one actually survives.
        nonlocal merged_head, resolved
        if not resolved:
            merged_head = _merged_parent(root, config, task, identity.get("branch"),
                                         identity.get("base"), identity.get("expected_head"))
            resolved = True
        return merged_head

    outcomes = []
    # Each ref has independent deletion and lease checks, so one failure cannot
    # prevent sibling cleanup; an unfinished subtask keeps its branch either way.
    for child in descendants:
        reason = f"Task {child.id} is {child.state}, not Done" if child.state != "Done" else None
        outcome = _cleanup_branch(root, config, child, base=identity.get("base"),
                                  merged=merged, exact=False, preserve_reason=reason)
        outcomes.append({"task": child.id, **outcome})
    result = _cleanup_branch(root, config, task, merged=merged, **identity)
    result.update(scope="parent and descendant task branches", descendants=outcomes)
    if discovery_error is not None:
        result["descendants_error"] = discovery_error
    return result


def discard_review_streams(result, root, task):
    """Raw review streams serve diagnosis while a task is open; Done ends that.

    Removal cannot undo Done, so a failure is reported beside the result instead of raised.
    """
    from . import review_results, workflow
    try:
        review_results.discard_streams(workflow.WORKFLOW_STATE, workflow.repository_key(root), task.id)
    except (OSError, AgentBoxError) as exc:
        result["review_streams_error"] = f"Raw review streams were not removed: {exc}"
    return result


def after_done(result, root, config, task, adapter, **identity):
    discard_review_streams(result, root, task)
    if config.workflow.cleanup_local_branches or config.workflow.cleanup_remote_branches:
        result["cleanup"] = cleanup(root, config, task, adapter, **identity)
    return result


def retry(root, config, task, adapter):
    """Retry parent cleanup with the fresh task and locks owned by workflow.landing."""
    if task.parent or task.state != "Done":
        raise AgentBoxError("Cleanup requires a Done parent task; complete the merge with workflow land ID merge")
    return {"task": task.id, "state": "Done", "cleanup": cleanup(root, config, task, adapter)}
