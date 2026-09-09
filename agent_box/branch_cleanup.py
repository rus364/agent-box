"""Optional task-tree cleanup; Git and merged PRs supply every deletion proof."""
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


def _integrated_head(root, head, base, merged_head=None):
    if merged_head is not None:
        if head != merged_head:
            raise AgentBoxError("Branch head differs from exact merged PR head")
    elif not gw._is_ancestor(root, head, gw.local_branch_head(root, base)):
        raise AgentBoxError("Task head is not integrated into the local base; fetch missing objects if needed")


def reconcile(root, config, task, adapter):
    """Recheck Done under workflow.landing locks, never infer proof from lifecycle.

    Unlike optional deletion, reconciliation must inspect live origin even when
    cleanup is disabled. Tracking refs can disambiguate names but cannot prove
    that a deleted branch survives, or supply a missing merge identity.
    """
    if task.parent or task.state != "Done":
        raise AgentBoxError("Reconciliation retry requires a Done parent task")
    prefix = gw.branch_prefix(task.id)
    local, names = _local_refs(root, prefix)
    url = gw.push_destination(root)
    remote, symbols, base, _ = _remote_refs(root, url, prefix)
    names.update(remote)
    if len(names) > 1:
        raise AgentBoxError("Ambiguous task namespace; preserving all candidates: " + ", ".join(sorted(names)))
    branch = next(iter(names), None)
    result = {"task": task.id, "state": "Done"}
    if not local and not remote:
        return after_done(result, root, config, task, adapter)
    merged_head = None
    if config.workflow.merge_mode == "github":
        if not (config.github.gh_token or config.github.gh_config):
            raise AgentBoxError("Enable GitHub integration before GitHub-mode reconciliation")
        merged_head = _merged_head(root, url, branch, base)
    candidates = list(local.values())
    candidates.extend((head, f"refs/heads/{name}" in symbols)
                      for name, head in remote.items())
    for head, symbolic in candidates:
        if symbolic:
            raise AgentBoxError("Preserving symbolic ref")
        _integrated_head(root, head, base, merged_head)
    return after_done(result, root, config, task, adapter, branch=branch, base=base)


def _cleanup_branch(root, config, task, *, branch=None, base=None, expected_head=None,
                    parent_proof=None):
    """Return independent outcomes, including failures, after a successful Done write.

    A retry supplies no old merge record. A missing ref permits only a no-op;
    every surviving candidate must prove integration again.
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
                if config.workflow.merge_mode == "github" and not (
                        config.github.gh_token or config.github.gh_config):
                    raise AgentBoxError("Enable GitHub integration before GitHub-mode cleanup")
                url = gw.push_destination(root)
                remote, symbols, remote_base, published = _remote_refs(root, url, prefix)
                names.update(remote)
            except (AgentBoxError, OSError, subprocess.TimeoutExpired) as exc:
                result["remote"] = {"status": "error", "reason": str(exc)}
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
        merged_head = None
        if config.workflow.merge_mode == "github" and parent_proof is None:
            if not (config.github.gh_token or config.github.gh_config):
                raise AgentBoxError("Enable GitHub integration before GitHub-mode cleanup")
            url = url or gw.push_destination(root)
            merged_head = _merged_head(root, url, branch, base)
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
                if parent_proof is None:
                    _integrated_head(root, head, base, merged_head)
                else:
                    accepted_head = parent_proof()
                    if not gw._is_ancestor(root, head, accepted_head):
                        raise AgentBoxError("Descendant head is not integrated into the accepted parent result; "
                                            "preserving moved or unproven work; fetch missing objects if needed")
                if config.workflow.merge_mode == "local":
                    if kind == "remote" and (remote_base != base or not gw._is_ancestor(root, head, published)):
                        raise AgentBoxError("Task head is not proven in the live published origin base; publish the base or fetch missing objects, then retry")
                _unoccupied(root, branch, base)
                if kind == "local":
                    gw.git(root, "update-ref", "--no-deref", "-d", ref, head)
                else:
                    gw.git(root, *gw._push_config_args(), "push", "--no-mirror", "--no-follow-tags",
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


def _parent_proof(root, config, task, *, branch=None, base=None, expected_head=None):
    """Recover proof before deleting the parent; a PR survives rewritten/absent refs."""
    prefix = gw.branch_prefix(task.id)
    local, names = _local_refs(root, prefix)
    remote = {}
    symbols = set()
    url = None
    if config.workflow.cleanup_remote_branches:
        try:
            url = gw.push_destination(root)
            remote, symbols, remote_base, _ = _remote_refs(root, url, prefix)
            names.update(remote)
            base = base or remote_base
        except (AgentBoxError, OSError, subprocess.TimeoutExpired):
            # Origin failure must not disable a surviving local integration
            # proof. Each remote deletion reports its own transport failure.
            if not local and expected_head is None:
                raise
    if branch:
        names.add(branch)
    if len(names) > 1:
        raise AgentBoxError("Ambiguous parent namespace; cannot prove descendant integration")
    branch = next(iter(names), None)
    base = base or gw.default_base(root)
    if branch == base:
        raise AgentBoxError("Parent namespace identifies the base/default branch")
    if config.workflow.merge_mode == "github":
        if not (config.github.gh_token or config.github.gh_config):
            raise AgentBoxError("Enable GitHub integration before GitHub-mode cleanup")
        head = _merged_head(root, url or gw.push_destination(root), branch, base, prefix=prefix)
        if expected_head is not None and head != expected_head:
            raise AgentBoxError("Parent merged PR head differs from the accepted head")
        return head
    # With no surviving parent, the local base is the remaining Git integration
    # proof. Never reconstruct a deleted ref from a title or tracker journal.
    if expected_head is None and ((branch in local and local[branch][1])
                                  or f"refs/heads/{branch}" in symbols):
        raise AgentBoxError("Symbolic parent ref cannot prove descendant integration")
    head = expected_head or (local[branch][0] if branch in local else remote.get(branch))
    head = head or gw.local_branch_head(root, base)
    _integrated_head(root, head, base)
    return head


def cleanup(root, config, task, adapter, **identity):
    """Discover only live descendants and isolate optional failures from Done."""
    descendants = []
    discovery_error = None
    if config.workflow.cleanup_local_branches or config.workflow.cleanup_remote_branches:
        try:
            from .workflow import tree
            tasks = tree(adapter, task)
            by_id = {item.id: item for item in tasks}
            for parent in tasks:
                if any(by_id[child].parent != parent.id for child in parent.children):
                    raise AgentBoxError("Inconsistent live task hierarchy; preserving descendant branches")
            descendants = [item for item in tasks if item.id != task.id]
        except Exception as exc:
            discovery_error = str(exc)

    accepted_head = None

    def proof():
        nonlocal accepted_head
        if accepted_head is None:
            accepted_head = _parent_proof(root, config, task, **identity)
        return accepted_head

    outcomes = []
    # Descendants consume the parent proof while its refs still exist; each ref
    # has independent deletion/lease checks and cannot prevent sibling cleanup.
    for child in descendants:
        if child.state != "Done":
            outcome = {kind: ({"status": "preserved", "reason": f"Task {child.id} is {child.state}, not Done"}
                              if enabled else {"status": "disabled"})
                       for kind, enabled in (("local", config.workflow.cleanup_local_branches),
                                             ("remote", config.workflow.cleanup_remote_branches))}
        else:
            outcome = _cleanup_branch(root, config, child, base=identity.get("base"), parent_proof=proof)
        outcomes.append({"task": child.id, **outcome})
    result = _cleanup_branch(root, config, task, **identity)
    result.update(scope="parent and descendant task branches", descendants=outcomes)
    if discovery_error is not None:
        result["descendants_error"] = discovery_error
    return result


def after_done(result, root, config, task, adapter, **identity):
    if config.workflow.cleanup_local_branches or config.workflow.cleanup_remote_branches:
        result["cleanup"] = cleanup(root, config, task, adapter, **identity)
    return result


def retry(root, config, task, adapter):
    """Retry parent cleanup with the fresh task and locks owned by workflow.landing."""
    if task.parent or task.state != "Done":
        raise AgentBoxError("Cleanup requires a Done parent task; reconcile a proven merge first")
    return {"task": task.id, "state": "Done", "cleanup": cleanup(root, config, task, adapter)}
