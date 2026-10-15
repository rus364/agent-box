"""GitHub PR primitives; ab-land owns the bounded, sequential fix conversation."""
from __future__ import annotations

import json
import re
import subprocess
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
              "number,url,state,isDraft,headRefName,headRefOid,baseRefName,mergeStateStatus,mergeable,reviewDecision,statusCheckRollup,mergeCommit,mergedAt", json_output=True)


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


def blockers(pr, review_threads):
    """Whether GitHub itself is ready to merge; acceptance of the head is separate."""
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
    return reasons


def acceptance_blockers(root, config, tasks, pr, saved, base, *, approved, integrated=False):
    """Why this head is not accepted yet; a confirmed merge is not acceptance of what it merged.

    The review policy decides here exactly as it does in local landing: a cloud
    verdict read back from the pull request is recorded like any other verdict,
    so a human approval on GitHub is the only acceptance that is special to this
    mode.
    """
    from .workflow import diff_stats, review_blockers

    reasons = []
    accepted = approved
    if approved and pr["state"] == "MERGED":
        # GitHub binds reviews to commits, not to local evidence. Only an
        # approval observed before merge can be associated with that scope.
        accepted = (bool(saved.get("scope"))
                    and saved.get("github_approval") == approval_scope(pr, saved["scope"]))
    if not accepted:
        files, lines = diff_stats(root, base, config.workflow.review)
        reasons = review_blockers(config.workflow.review, tasks, files, lines, saved, integrated=integrated)
        if reasons and approved:
            reasons.append("Merged GitHub approval needs a matching pre-merge scope snapshot; "
                           "otherwise accept this head again before completing it")
    if saved.get("validation", {}).get("exit") != 0:
        reasons.append("Local validation for the current scope is missing")
    return reasons


def human_approval(root, repo, pr):
    """An approval GitHub attributes to a person and to exactly this head.

    The reviews REST endpoint is read instead of the pull request's own
    `latestReviews`, which carries neither fact: its `commit.oid` is empty for
    every review, and its author login does not say whether the account is an
    app. A bot's approval is not acceptance here, or the cloud reviewer could
    accept the work it was asked to judge by pressing a button instead of
    answering. A later review by the same person supersedes an earlier one.
    """
    from .github_review import paginate

    verdicts = {}
    for review in paginate(root, repo, f"pulls/{pr['number']}/reviews?per_page=100"):
        user = review.get("user") or {}
        if user.get("type") == "Bot" or review.get("commit_id") != pr["headRefOid"]:
            continue
        if review.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            verdicts[user.get("login")] = review["state"]
    return "APPROVED" in verdicts.values()


def approval_scope(pr, fingerprint):
    return {"scope": fingerprint, "head": pr["headRefOid"], "url": pr["url"],
            "branch": pr["headRefName"], "base": pr["baseRefName"]}


def operate(args, root, config, adapter, task):
    """Consume the fresh task while workflow.landing owns the acceptance locks."""
    from .workflow import evidence, resolve, scope, tree

    parent, info, _, _ = resolve(root, adapter, task)
    tasks, saved = [], {}
    if args.command == "pr":
        tasks = tree(adapter, task)
        saved = evidence(root, task, scope(root, info["base"]), info["base"])
    return _operate(args, root, config, adapter, task, parent, info, saved, tasks)


def _landing_tasks(root, adapter, task_id, info, *, states=("In review",)):
    from .workflow import tree

    # Tracker responses are snapshots: traversing an old root misses changed
    # completion state and newly attached children even if descendants are reread.
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


#: GitHub's closing keywords: in a PR into the default branch they link an issue, which
#: moves its card back to In progress, and merging closes it before workflow finish has
#: proved anything.
CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*:?\s+"
                     r"(?:[\w.-]+/[\w.-]+#\d+|#\d+|https?://\S+/issues/\d+)", re.IGNORECASE)


def closing_references(adapter, body_file):
    """Closing keywords a PR body may not carry; only a GitHub Projects tracker owns those issues."""
    from .tracker.github import GitHubAdapter
    if not isinstance(adapter, GitHubAdapter):
        return []
    return [match.group(0) for match in CLOSING.finditer(Path(body_file).read_text(encoding="utf-8"))]


def _operate(args, root, config, adapter, task, parent, info, saved, tasks):
    from .workflow import delta_review, evidence, integrated_parent, review_route, save_evidence, scope
    if not (config.credentials.github.token or config.credentials.github.config):
        raise AgentBoxError("Enable the GitHub integration in agent-box config before workflow GitHub actions")
    if task.id != parent.id:
        raise AgentBoxError("Create and land one PR for the parent task")
    repo = gw.github_origin(root)
    if gw.current_branch(root) != info["branch"]:
        raise AgentBoxError(f"Switch to the parent branch {info['branch']} for PR operations")
    if args.command == "pr":
        if task.state != "In review" or not saved.get("validation"):
            raise AgentBoxError("Finish parent acceptance and validation before opening a PR")
        refused = closing_references(adapter, args.body_file)
        if refused:
            raise AgentBoxError("The PR body would close issues on merge (" + ", ".join(refused) + "); with a GitHub "
                                "Projects tracker workflow closes tasks itself. Refer to the task without a closing keyword")
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
    if pr["state"] == "MERGED":
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
        # deletes the head branch on merge when the repository asks it to, and a
        # repeated merge exists precisely for the run that failed after that merge.
        # Being unable to read origin at all still refuses, because then nothing
        # rules out a branch that moved on.
        remote_head = gw.remote_branch_head(root, info["branch"])
        if remote_head is not None and remote_head != pr["headRefOid"]:
            raise AgentBoxError(
                f"origin/{info['branch']} is at {remote_head[:12]}, not the merged "
                f"{pr['headRefOid'][:12]}; another checkout advanced the branch, so reconcile them first")
        merged_identity(pr, info["branch"], info["base"])
        task, tasks, reasons = _landing_tasks(root, adapter, task.id, info, states=("In review", "Done"))
        saved, delta = {}, None
        if task.state != "Done":
            # A merged PR proves integration, not acceptance of the merged head.
            saved = evidence(root, task, scope(root, info["base"]), info["base"])
            delta = delta_review(root, config, task, tasks)
            reasons += acceptance_blockers(
                root, config, tasks, pr, {**saved, "delta_review": delta},
                info["base"], approved=human_approval(root, repo, pr),
                integrated=integrated_parent(root, task, parent, info["base"]))
        _current_head(root, pr)
        if args.action == "inspect":
            # Inspection reports what is left; completing what GitHub merged, and
            # releasing the branches it used, is the work of `land PARENT merge`.
            return {"pr": pr, "threads": [], "comments": [], "blockers": reasons, "state": task.state,
                    "merge": pr["mergeCommit"],
                    "review": {"verdict": saved.get("review", {}).get("verdict"), "delta": delta,
                               "route": review_route(config, task, parent)},
                    "max_rounds": config.workflow.review.max_rounds,
                    "wait_seconds": config.workflow.review.wait_seconds}
        gw.require_clean(root)
        if reasons:
            raise AgentBoxError("Cannot complete merged task: " + "; ".join(reasons))
        if task.state != "Done":
            adapter.set_state(task, "Done")
        return after_done({"task": task.id, "state": "Done", "merge": pr["mergeCommit"], "url": pr["url"]},
                          root, config, task, adapter, branch=info["branch"], base=info["base"],
                          expected_head=pr["headRefOid"])
    feedback = threads(root, repo, pr["number"])
    if args.action == "inspect":
        comments = gh(root, "api", "--paginate", "--slurp", f"repos/{repo}/issues/{pr['number']}/comments", json_output=True)
    # Acceptance belongs after network feedback, immediately before the action.
    task, tasks, reasons = _landing_tasks(root, adapter, task.id, info)
    saved = evidence(root, task, scope(root, info["base"]), info["base"])
    _current_head(root, pr)
    delta = delta_review(root, config, task, tasks)
    approved = human_approval(root, repo, pr)
    reasons += blockers(pr, feedback) + acceptance_blockers(
        root, config, tasks, pr, {**saved, "delta_review": delta}, info["base"], approved=approved,
        integrated=integrated_parent(root, task, parent, info["base"]))
    if args.action == "inspect":
        return {"pr": pr, "threads": feedback, "comments": [c for page in comments for c in page],
                "blockers": reasons,
                "review": {"verdict": saved.get("review", {}).get("verdict"), "delta": delta,
                           "route": review_route(config, task, parent)},
                "max_rounds": config.workflow.review.max_rounds,
                "wait_seconds": config.workflow.review.wait_seconds}
    if reasons:
        raise AgentBoxError("Cannot merge: " + "; ".join(reasons))
    if approved:
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
    current = evidence(root, task, scope(root, info["base"]), info["base"])
    _current_head(root, merged)
    if current["scope"] != saved["scope"]:
        reasons.append("Code changed during merge; validate and review the current head")
    reasons += blockers(pr, feedback) + acceptance_blockers(
        root, config, tasks, pr, {**current, "delta_review": delta_review(root, config, task, tasks)},
        info["base"], approved=approved,
        integrated=integrated_parent(root, task, parent, info["base"]))
    if reasons:
        raise AgentBoxError("Cannot complete merged task: " + "; ".join(reasons))
    adapter.set_state(task, "Done")
    return after_done({"task": task.id, "state": "Done", "url": merged["url"], "merge": merged["mergeCommit"]},
                      root, config, task, adapter, branch=info["branch"], base=info["base"],
                      expected_head=merged["headRefOid"])
