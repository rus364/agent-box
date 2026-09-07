"""Optional parent-only cleanup; Git and merged PRs supply every deletion proof."""
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


def _destination(root):
    url = gw.effective_push_url(root)
    if not url:
        raise AgentBoxError("origin must have exactly one effective push URL")
    # Passing an already rewritten URL to Git applies insteadOf once more.
    # Refuse chained rewrites rather than read one repository and delete in another.
    if gw.git(root, "ls-remote", "--get-url", url) != url:
        raise AgentBoxError("Cannot freeze origin destination: chained URL rewrites")
    rules = gw._run(["git", "config", "--null", "--get-regexp",
                     r"^url\..*\.pushinsteadof$"], cwd=root)
    if rules.returncode not in (0, 1):
        raise AgentBoxError("Cannot read origin URL rewrite configuration")
    for rule in rules.stdout.split("\0"):
        if not rule:
            continue
        key, prefix = rule.split("\n", 1)
        replacement = key[len("url."):-len(".pushinsteadof")]
        if url.startswith(prefix) and replacement + url[len(prefix):] != url:
            raise AgentBoxError("Cannot freeze origin destination: chained push URL rewrites")
    return url


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


def _merged_head(root, url, branch, base):
    from . import github_workflow as github
    repo = gw.github_repository(url)
    rows = github.gh(root, "pr", "list", "--repo", repo, "--head", branch,
                     "--base", base, "--state", "all", "--limit", "1000",
                     "--json", "number,state", json_output=True)
    relevant = [row for row in rows if row["state"] in ("OPEN", "MERGED")]
    if len(rows) >= 1000 or len(relevant) != 1 or relevant[0]["state"] != "MERGED":
        raise AgentBoxError("Merged PR is missing or ambiguous; preserving the branch")
    return merged_identity(github.view(root, repo, relevant[0]["number"]), branch, base)


def _integrated_head(root, head, base, merged_head=None):
    if merged_head is not None:
        if head != merged_head:
            raise AgentBoxError("Branch head differs from exact merged PR head")
    elif not gw._is_ancestor(root, head, gw.local_branch_head(root, base)):
        raise AgentBoxError("Task head is not integrated into the local base; fetch missing objects if needed")


def reconcile(root, config, task):
    """Done avoids another lifecycle write, never proof of surviving work.

    Unlike optional deletion, reconciliation must inspect live origin even when
    cleanup is disabled. Tracking refs can disambiguate names but cannot prove
    that a deleted branch survives, or supply a missing merge identity.
    """
    from .workflow_state import lock
    if task.parent or task.state != "Done":
        raise AgentBoxError("Reconciliation retry requires a Done parent task")
    with lock(root, "work"):
        prefix = gw.branch_prefix(task.id)
        local, names = _local_refs(root, prefix)
        url = _destination(root)
        remote, symbols, base, _ = _remote_refs(root, url, prefix)
        names.update(remote)
        if len(names) > 1:
            raise AgentBoxError("Ambiguous task namespace; preserving all candidates: " + ", ".join(sorted(names)))
        branch = next(iter(names), None)
        result = {"task": task.id, "state": "Done"}
        if not local and not remote:
            return after_done(result, root, config, task)
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
        return after_done(result, root, config, task, branch=branch, base=base)


def cleanup(root, config, task, *, branch=None, base=None, expected_head=None):
    """Return independent outcomes, including failures, after a successful Done write.

    A retry supplies no old merge record. A missing ref permits only a no-op;
    every surviving candidate must prove integration again.
    """
    enabled = {"local": config.workflow.cleanup_local_branches,
               "remote": config.workflow.cleanup_remote_branches}
    result = {kind: {"status": "pending" if value else "disabled"}
              for kind, value in enabled.items()}
    result["scope"] = "parent branch only"
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
                url = _destination(root)
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
            raise AgentBoxError("Branch is outside the parent task namespace")
        result["branch"] = branch
        default = remote_base or gw.default_base(root)
        base = base or default
        if branch in (base, default):
            raise AgentBoxError("Preserving the base/default branch")
        _unoccupied(root, branch, base)
        merged_head = None
        if config.workflow.merge_mode == "github":
            if not (config.github.gh_token or config.github.gh_config):
                raise AgentBoxError("Enable GitHub integration before GitHub-mode cleanup")
            url = url or _destination(root)
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
                _integrated_head(root, head, base, merged_head)
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


def after_done(result, root, config, task, **identity):
    if config.workflow.cleanup_local_branches or config.workflow.cleanup_remote_branches:
        result["cleanup"] = cleanup(root, config, task, **identity)
    return result


def retry(root, config, task):
    from .workflow_state import lock
    if task.parent or task.state != "Done":
        raise AgentBoxError("Cleanup requires a Done parent task; reconcile a proven merge first")
    with lock(root, "work"):
        return {"task": task.id, "state": "Done", "cleanup": cleanup(root, config, task)}
