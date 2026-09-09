"""GitHub PR primitives; ab-land owns the bounded, sequential fix conversation."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from .errors import AgentBoxError
from .tracker.core import completion_reasons, blocking_reasons
from . import git_workflow as gw
from .branch_cleanup import after_done, merged_identity


def gh(root, *args, json_output=False):
    try:
        result = subprocess.run(["gh", *map(str, args)], cwd=root, capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        raise AgentBoxError("GitHub CLI unavailable or timed out") from None
    if result.returncode:
        raise AgentBoxError("GitHub command failed: " + result.stderr.strip()[:1500])
    if json_output:
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise AgentBoxError("GitHub returned malformed JSON") from None
    return result.stdout.strip()


def find_pr(root, repo, branch, base):
    """The PR is whatever GitHub has for this head and base, not a saved number.

    Storing the number in the tracker made a comment authoritative over GitHub;
    asking GitHub cannot go stale, and an ambiguous answer stops the operation
    rather than picking one.
    """
    rows = gh(root, "pr", "list", "--repo", repo, "--head", branch, "--base", base,
              "--state", "all", "--json", "number,state", json_output=True)
    open_prs = [row for row in rows if row["state"] == "OPEN"]
    merged = [row for row in rows if row["state"] == "MERGED"]
    if len(open_prs) == 1:
        return open_prs[0]["number"]
    if not open_prs and len(merged) == 1:
        return merged[0]["number"]
    if open_prs or merged:
        raise AgentBoxError(f"Several pull requests match {branch}; select the intended PR explicitly")
    return None


def view(root, repo, reference):
    return gh(root, "pr", "view", reference, "--repo", repo, "--json",
              "number,url,state,isDraft,headRefName,headRefOid,baseRefName,mergeStateStatus,mergeable,reviewDecision,latestReviews,statusCheckRollup,mergeCommit,mergedAt", json_output=True)


def threads(root, repo, number):
    owner, name = repo.split("/")
    query = """query($owner:String!,$name:String!,$number:Int!,$endCursor:String) {
      repository(owner:$owner,name:$name) { pullRequest(number:$number) {
        reviewThreads(first:100,after:$endCursor) { nodes { id isResolved isOutdated path line
          comments(first:100) { nodes { id body url author { login } commit { oid } } pageInfo { hasNextPage } }
        } pageInfo { hasNextPage endCursor } }
      } }
    }"""
    pages = gh(root, "api", "graphql", "--paginate", "--slurp", "-f", "query=" + query,
               "-f", "owner=" + owner, "-f", "name=" + name, "-F", f"number={number}", json_output=True)
    result = []
    for page in pages:
        if page.get("errors"):
            raise AgentBoxError("GitHub could not read complete PR review threads")
        for thread in page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]:
            if thread["comments"]["pageInfo"]["hasNextPage"]:
                raise AgentBoxError("A review thread exceeds 100 comments; inspect it explicitly before landing")
            result.append(thread)
    return result


def blockers(pr, review_threads, saved):
    reasons = []
    if pr["state"] != "OPEN" or pr["isDraft"]:
        reasons.append("PR must be open and ready for review")
    if pr["mergeable"] != "MERGEABLE" or pr["mergeStateStatus"] not in ("CLEAN", "HAS_HOOKS"):
        reasons.append("GitHub merge readiness is not confirmed")
    if pr.get("reviewDecision") in ("CHANGES_REQUESTED", "REVIEW_REQUIRED"):
        reasons.append("GitHub review requirements are not satisfied")
    for check in pr.get("statusCheckRollup") or []:
        if check.get("__typename") == "StatusContext":
            passed = check.get("state") == "SUCCESS"
        else:
            passed = check.get("status") == "COMPLETED" and check.get("conclusion") in ("SUCCESS", "NEUTRAL", "SKIPPED")
        if not passed:
            reasons.append("CI pending or failed: " + (check.get("name") or check.get("context") or "unknown check"))
    if any(not thread["isResolved"] for thread in review_threads):
        reasons.append("Unresolved review threads remain")
    return reasons + acceptance_blockers(pr, saved)


def acceptance_blockers(pr, saved):
    """A confirmed merge cannot replace the acceptance required before merging."""
    reasons = []
    approved = current_approval(pr)
    if pr["state"] == "MERGED":
        # GitHub binds reviews to commits, not tracker requirements. Only an
        # approval observed before merge can be associated with that scope.
        approved = (approved and bool(saved.get("scope"))
                    and saved.get("github_approval") == approval_scope(pr, saved["scope"]))
    if not approved and saved.get("review", {}).get("verdict") != "PASS":
        reasons.append("Need a current-head GitHub approval or a fresh local PASS; silence is not approval")
        if pr["state"] == "MERGED":
            reasons.append("Merged GitHub approval needs a matching pre-merge scope snapshot; otherwise run a fresh independent local review")
    if saved.get("validation", {}).get("exit") != 0:
        reasons.append("Local validation for the current scope is missing")
    return reasons


def current_approval(pr):
    return any(review.get("state") == "APPROVED" and (review.get("commit") or {}).get("oid") == pr["headRefOid"]
               for review in pr.get("latestReviews") or [])


def approval_scope(pr, fingerprint):
    return {"scope": fingerprint, "head": pr["headRefOid"], "url": pr["url"],
            "branch": pr["headRefName"], "base": pr["baseRefName"]}


def request_review(root, repo, pr, config):
    choice = config.github_provider
    if choice in ("local", "auto"):
        return {"provider": "local", "next": "Run ab-review in fresh context; configure codex or claude for explicit cloud review"}
    if choice == "codex" and (config.github_model or config.github_effort):
        raise AgentBoxError("GitHub @codex review cannot pin model/effort; clear cloud overrides or select local review")
    if choice == "claude":
        workflow = root / ".github/workflows/claude.yml"
        if not workflow.is_file():
            raise AgentBoxError("Configure a Claude GitHub Action first, or use local review")
        source = workflow.read_text()
        if "anthropics/claude-code-action@" not in source:
            raise AgentBoxError("Cannot identify the Claude Action; use local review or inspect the workflow")
        for flag, value in (("--model", config.github_model), ("--effort", config.github_effort)):
            if value and f"{flag} {value}" not in source:
                raise AgentBoxError(f"Claude Action must configure claude_args with {flag} {value}; comments cannot pin it")
    body = (f"@{choice} review\n\nReview commit {pr['headRefOid']} against this PR's acceptance criteria. "
            "Report actionable findings with file, line, evidence and requirement. Do not change code. "
            "State explicitly when no issues are found.")
    with tempfile.TemporaryDirectory(prefix="agent-box-pr-") as temporary:
        file = Path(temporary) / "comment.md"
        file.write_text(body)
        gh(root, "pr", "comment", pr["number"], "--repo", repo, "--body-file", file)
    return {"provider": choice, "requested_head": pr["headRefOid"], "wait_seconds": config.wait_seconds,
            "next": "Read feedback; absence of a current-head approval requires local review"}


def operate(args, root, config, adapter, task):
    """Consume the fresh task while workflow.landing owns the acceptance locks."""
    from .workflow import evidence, resolve, scope, tree

    parent, info, _, _ = resolve(root, adapter, task)
    tasks, saved = [], {}
    if args.command == "pr":
        tasks = tree(adapter, task)
        saved = evidence(root, task, scope(root, info["base"], tasks))
    return _operate(args, root, config, adapter, task, parent, info, saved, tasks)


def _landing_tasks(root, adapter, task_id, info, *, states=("Review",)):
    from .workflow import tree

    # Tracker responses are snapshots: traversing an old root misses changed
    # requirements and newly attached children even if descendants are reread.
    task = adapter.get(task_id)
    tasks = tree(adapter, task)
    reasons = completion_reasons(task, tasks, str(adapter.identity()["id"]), states=states)
    reasons += blocking_reasons(task, adapter.get)
    if task.parent or gw.current_branch(root) != info["branch"]:
        raise AgentBoxError("Current parent branch identity differs from the landing request")
    return task, tasks, reasons


def _current_head(root, pr):
    if pr["headRefOid"] != gw.head_sha(root):
        raise AgentBoxError("Local and remote parent heads differ; the current work is not merged")


def _operate(args, root, config, adapter, task, parent, info, saved, tasks):
    from .workflow import evidence, save_evidence, scope
    if not (config.github.gh_token or config.github.gh_config):
        raise AgentBoxError("Enable the GitHub integration in agent-box config before workflow GitHub actions")
    if task.id != parent.id:
        raise AgentBoxError("Create and land one PR for the parent task")
    repo = gw.github_origin(root)
    if gw.current_branch(root) != info["branch"]:
        raise AgentBoxError(f"Switch to the parent branch {info['branch']} for PR operations")
    if args.command == "pr":
        if task.state != "Review" or not saved.get("validation"):
            raise AgentBoxError("Finish parent acceptance and validation before opening a PR")
        if any(child.state != "Done" for child in tasks if child.id != task.id):
            raise AgentBoxError("All subtasks must be integrated before opening the parent PR")
        gw.push_parent(root, info["branch"], info["base"])
        existing = gh(root, "pr", "list", "--repo", repo, "--head", info["branch"], "--base", info["base"],
                      "--state", "open", "--json", "number,url", json_output=True)
        if len(existing) > 1:
            raise AgentBoxError("Multiple matching PRs; select the intended PR explicitly")
        if not existing:
            command = ["pr", "create", "--repo", repo, "--base", info["base"], "--head", info["branch"],
                       "--title", args.title, "--body-file", str(Path(args.body_file).resolve())]
            if args.draft:
                command.append("--draft")
            gh(root, *command)
        return view(root, repo, info["branch"])
    number = find_pr(root, repo, info["branch"], info["base"])
    if not number:
        raise AgentBoxError(f"GitHub has no pull request for {info['branch']} into {info['base']}; run ab-pr")
    pr = view(root, repo, number)
    if pr["headRefName"] != info["branch"] or pr["baseRefName"] != info["base"]:
        raise AgentBoxError("PR branch identity differs from the tracked parent")
    if args.action == "reconcile" or pr["state"] == "MERGED":
        if pr["state"] != "MERGED" or not pr.get("mergedAt") or not pr.get("mergeCommit"):
            raise AgentBoxError("GitHub has not confirmed an actual merge")
        # A reused branch keeps matching an old merged PR by head and base, so
        # completion asks what was merged, not merely that something once was.
        # Squash and rebase merges rewrite the commit, leaving head equality as
        # the only honest test that this branch is what landed.
        if pr["headRefOid"] != gw.head_sha(root):
            raise AgentBoxError(
                f"PR #{pr['number']} merged {pr['headRefOid'][:12]}, but {info['branch']} is now at "
                f"{(gw.head_sha(root) or '')[:12]}; the current work is not merged")
        # This checkout is not the only one that can push the task branch, and it
        # may not have fetched since another machine did, so origin is asked live
        # rather than through a remote-tracking ref that predates the push. A
        # branch origin no longer has is the normal case, not a problem: GitHub
        # deletes the head branch on merge when the repository asks it to, and
        # reconcile exists precisely for the run that failed after that merge.
        # Being unable to read origin at all still refuses, because then nothing
        # rules out a branch that moved on.
        remote_head = gw.remote_branch_head(root, info["branch"])
        if remote_head is not None and remote_head != pr["headRefOid"]:
            raise AgentBoxError(
                f"origin/{info['branch']} is at {remote_head[:12]}, not the merged "
                f"{pr['headRefOid'][:12]}; another checkout advanced the branch, so reconcile first")
        merged_identity(pr, info["branch"], info["base"])
        task, tasks, reasons = _landing_tasks(root, adapter, task.id, info, states=("Review", "Done"))
        gw.require_clean(root)
        if task.state != "Done":
            # A merged PR proves integration, not acceptance of today's requirements.
            saved = evidence(root, task, scope(root, info["base"], tasks))
            reasons += acceptance_blockers(pr, saved)
        _current_head(root, pr)
        if reasons:
            raise AgentBoxError("Cannot complete merged task: " + "; ".join(reasons))
        if task.state != "Done":
            adapter.set_state(task, "Done")
        return after_done({"task": task.id, "state": "Done", "merge": pr["mergeCommit"], "url": pr["url"]},
                          root, config, task, adapter, branch=info["branch"], base=info["base"],
                          expected_head=pr["headRefOid"])
    if args.action == "request-review":
        gw.require_clean(root)
        _current_head(root, pr)
        return request_review(root, repo, pr, config.workflow.review)
    feedback = threads(root, repo, pr["number"])
    if args.action == "inspect":
        comments = gh(root, "api", "--paginate", "--slurp", f"repos/{repo}/issues/{pr['number']}/comments", json_output=True)
    # Acceptance belongs after network feedback, immediately before the action.
    task, tasks, reasons = _landing_tasks(root, adapter, task.id, info)
    saved = evidence(root, task, scope(root, info["base"], tasks))
    _current_head(root, pr)
    reasons += blockers(pr, feedback, saved)
    if args.action == "inspect":
        return {"pr": pr, "threads": feedback, "comments": [c for page in comments for c in page],
                "blockers": reasons, "max_rounds": config.workflow.review.max_rounds,
                "wait_seconds": config.workflow.review.wait_seconds}
    if reasons:
        raise AgentBoxError("Cannot merge: " + "; ".join(reasons))
    if current_approval(pr):
        saved["github_approval"] = approval_scope(pr, saved["scope"])
        save_evidence(root, task, saved)
    gh(root, "pr", "merge", pr["number"], "--repo", repo, "--" + (args.method or "squash"),
       "--match-head-commit", pr["headRefOid"])
    merged = view(root, repo, pr["number"])
    if merged["state"] != "MERGED":
        return {"state": merged["state"], "next": "Wait for GitHub merge queue; do not mark the task Done"}
    merged_identity(merged, info["branch"], info["base"], pr["headRefOid"])
    # GitHub can merge the accepted head while local work or tracker acceptance
    # advances during the request. That successful merge cannot finish new work.
    task, tasks, reasons = _landing_tasks(root, adapter, task.id, info)
    current = evidence(root, task, scope(root, info["base"], tasks))
    _current_head(root, merged)
    if current["scope"] != saved["scope"]:
        reasons.append("Code or requirements changed during merge; validate and review the current scope")
    reasons += blockers(pr, feedback, current)
    if reasons:
        raise AgentBoxError("Cannot complete merged task: " + "; ".join(reasons))
    adapter.set_state(task, "Done")
    return after_done({"task": task.id, "state": "Done", "url": merged["url"], "merge": merged["mergeCommit"]},
                      root, config, task, adapter, branch=info["branch"], base=info["base"],
                      expected_head=merged["headRefOid"])
