"""The parent's final review inside its pull request, read back as an ordinary round.

A cloud reviewer answers where it runs, so the request is a PR comment and the
verdict is whatever that bot published for exactly this head. Both shapes were
observed live on 2026-09-17 against this repository's own bots and are described
in docs/integrations/workflow.md: Claude answers in the canonical reviewer form
because the request asks for it, and Codex answers in its own form, which names
the commit it reviewed and attaches priorities to inline comments.

The alternative, a GitHub approval, was rejected because neither bot submits one:
Codex reviews are COMMENTED, and reading a verdict from silence or from a
thumbs-up would accept work nobody judged.
"""
from __future__ import annotations

import datetime
import json
import re
import secrets
import tempfile
from pathlib import Path

from .atomicio import atomic_write_text
from . import git_workflow as gw
from .errors import AgentBoxError
from .provider_runner import ProviderFailure, review_report

#: Codex prints its priority as a badge image; only P0 and P1 reach GitHub today.
CODEX_PRIORITY = re.compile(r"!\[(P[0-3]) Badge\]")
CODEX_SEVERITY = {"P0": "CRITICAL", "P1": "HIGH", "P2": "MEDIUM", "P3": "LOW"}
#: Both Codex answers, the summary comment and the review, name what they read.
CODEX_COMMIT = re.compile(r"\*\*Reviewed commit:\*\*\s*`([0-9a-f]{7,64})`")

REQUEST_FORM = """@{provider} review

Review commit {head} of this pull request against its description and the repository's
guidance. Read only: change no files, push nothing, and answer in this pull request.

Report every finding on its own line as

- [LOW|MEDIUM|HIGH|CRITICAL] path:line — what is wrong and why it matters

and end the answer with exactly these two lines, the verdict being one of PASS,
NEEDS_WORK or INCONCLUSIVE:

Reviewed-Commit: {head}
Verdict: NEEDS_WORK

A PASS lists no findings. Write exactly one Verdict line and no example verdicts anywhere
in the answer.{context}{brief}
"""


def request_body(provider, head, review_context="", brief=None):
    context = f"\n\n{review_context.strip()}" if review_context else ""
    note = f"\n\nContext from the coordinator:\n\n{brief.strip()}" if brief else ""
    return REQUEST_FORM.format(provider=provider, head=head, context=context, brief=note)


def paginate(root, repo, path):
    from .github_workflow import gh

    pages = gh(root, "api", "--paginate", "--slurp", f"repos/{repo}/{path}", json_output=True)
    return [item for page in pages for item in page]


def moment(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_bot(user):
    return (user or {}).get("type") == "Bot"


def find_request(comments, provider, head):
    """Our own published request for this exact head, whoever's session posted it.

    Identity is the visible text, not a hidden marker: a comment that mentions
    the bot and names this commit is a request, and one is enough per head.
    """
    mention = f"@{provider} review"
    for comment in comments:
        if not is_bot(comment.get("user")) and mention in comment["body"] and head in comment["body"]:
            return comment
    return None


def claude_answer(root, repo, number, comments, head, since):
    """Claude answers in the reviewer's own form, so its comment is read as one."""
    for comment in comments:
        if (comment["user"]["login"] != "claude[bot]" or moment(comment["created_at"]) < since
                or f"Reviewed-Commit: {head}" not in comment["body"]):
            continue
        try:
            return {**review_report(comment["body"]), "text": comment["body"]}, comment["html_url"]
        except ProviderFailure:
            # It answered about this head and its answer carries no single verdict.
            # Waiting an hour for a finished comment to improve helps nobody.
            return None, comment["html_url"]
    return None, None


def codex_findings(root, repo, number, head):
    """Codex attaches its priorities to inline comments on the commit it reviewed."""
    findings, text = [], []
    for comment in paginate(root, repo, f"pulls/{number}/comments?per_page=100"):
        priority = CODEX_PRIORITY.search(comment["body"])
        if not is_bot(comment.get("user")) or comment.get("commit_id") != head or not priority:
            continue
        location = f"{comment['path']}:{comment.get('line') or comment.get('original_line') or 0}"
        findings.append({"severity": CODEX_SEVERITY[priority[1]], "location": location})
        text.append(f"- [{CODEX_SEVERITY[priority[1]]}] {location} —\n\n{comment['body']}")
    return findings, text


def codex_answer(root, repo, number, comments, head, since):
    """A review for this head means findings; a comment naming it means none.

    Codex publishes findings as a COMMENTED review bound to the commit, and
    otherwise a plain comment that names the same commit. Its own English
    ("Didn't find any major issues") is not parsed: what it attached decides.
    """
    answers = [(moment(review["submitted_at"]), review["body"], review["html_url"])
               for review in paginate(root, repo, f"pulls/{number}/reviews?per_page=100")
               if is_bot(review.get("user")) and review.get("commit_id") == head
               and review.get("submitted_at")]
    answers += [(moment(comment["created_at"]), comment["body"], comment["html_url"])
                for comment in comments
                if is_bot(comment.get("user")) and (named := CODEX_COMMIT.search(comment["body"]))
                and head.startswith(named[1])]
    answers = [item for item in answers if item[0] >= since]
    if not answers:
        return None, None
    findings, rendered = codex_findings(root, repo, number, head)
    body = "\n\n".join(text for _, text, _ in sorted(answers))
    verdict = "NEEDS_WORK" if findings else "PASS"
    return ({"verdict": verdict, "findings": findings,
             "text": "\n\n".join([body, *rendered, f"Verdict: {verdict}"])},
            sorted(answers)[0][2])


def read_answer(root, repo, number, provider, comments, head, since):
    reader = claude_answer if provider == "claude" else codex_answer
    return reader(root, repo, number, comments, head, since)


def configured_reviewer(root, config):
    """Refuse an override the cloud reviewer cannot honour before publishing anything."""
    provider = config.github_provider
    if provider == "codex" and (config.github_model or config.github_effort):
        raise AgentBoxError("GitHub @codex review cannot pin model/effort; clear cloud overrides or select local review")
    if provider == "claude":
        workflow = root / ".github/workflows/claude.yml"
        if not workflow.is_file():
            raise AgentBoxError("Configure a Claude GitHub Action first, or use local review")
        source = workflow.read_text()
        if "anthropics/claude-code-action@" not in source:
            raise AgentBoxError("Cannot identify the Claude Action; use local review or inspect the workflow")
        for flag, value in (("--model", config.github_model), ("--effort", config.github_effort)):
            if value and f"{flag} {value}" not in source:
                raise AgentBoxError(f"Claude Action must configure claude_args with {flag} {value}; comments cannot pin it")
    return provider


def pending_round(root, task, state, fingerprint):
    """The descriptor of the round a previous run left waiting for this same head.

    The stored descriptor is the round's own, including the input it bound, so a
    later run records the answer against what the reviewer was actually given.
    """
    from .workflow import review_evidence_path

    round_id = state.get("pending")
    if not round_id or state.get("pending_backend") != "cloud" or any(
            item["round"] == round_id for item in state.get("results", [])):
        return None
    try:
        descriptor = json.loads((review_evidence_path(root, task.id, round_id) / "descriptor.json").read_text())
    except (OSError, ValueError):
        return None
    return descriptor if descriptor.get("scope") == fingerprint and descriptor.get("task") == task.id else None


def review(root, binding, adapter, config, task, base, fingerprint, identity, brief, review_context):
    """Publish one request per head, then read that head's verdict back.

    Waiting is not a failure and spends nothing: the same command reads the
    answer later. Only the wait budget ends the round, so the user decides what
    to do about a bot that never answered.
    """
    from .github_workflow import find_pr, gh, view
    from .workflow import (REVIEW_FAILURE_LIMIT, bind_delegation_input, lock, read_review_budget,
                           read_review_state, review_descriptor, review_evidence_path, review_result,
                           save_review_state, start_review, store_review_result)

    settings = config.workflow.review
    if not (config.github.gh_token or config.github.gh_config):
        raise AgentBoxError("Enable the GitHub integration in agent-box config before a cloud review, "
                            "or select local review with workflow.review.github_provider")
    repo = gw.github_origin(root)
    gw.require_clean(root)
    head = gw.head_sha(root)
    branch = gw.current_branch(root)
    number = find_pr(root, repo, branch, base)
    if not number:
        raise AgentBoxError(f"GitHub has no pull request for {branch} into {base}; open it with ab-pr "
                            "before the cloud review of this head")
    pr = view(root, repo, number)
    if pr["headRefOid"] != head:
        raise AgentBoxError(f"PR #{number} is at {pr['headRefOid'][:12]}, not the local {head[:12]}; "
                            "publish this head before asking its reviewer")
    comments = paginate(root, repo, f"issues/{number}/comments?per_page=100")
    provider = configured_reviewer(root, settings)
    request = find_request(comments, provider, head)
    with lock(root, "review-budget"):
        state = read_review_state(root, binding, task)
        descriptor = pending_round(root, task, state, fingerprint)
    if request is not None and descriptor is not None:
        round_id = descriptor["delegation"]
        report, url = read_answer(root, repo, number, provider, comments, head, moment(request["created_at"]))
        if report is not None:
            store_review_result(root, binding, task, descriptor,
                                {"route": {"provider": provider, "model": settings.github_model or "cloud",
                                           "backend": "cloud", "answer": url},
                                 "report": report, "diagnostics": {"usage": None}}, "cloud")
            return review_result(root, binding, adapter, task, config, round_id, reconcile=True)
        waited = (datetime.datetime.now(datetime.timezone.utc) - moment(request["created_at"])).total_seconds()
        if url is None and waited <= settings.wait_seconds:
            return {"task": task.id, "round": round_id, "scope": fingerprint, "provider": provider,
                    "pull_request": pr["url"], "requested_head": head,
                    "waited_seconds": int(waited), "wait_seconds": settings.wait_seconds,
                    "outcome": "no answer for this head yet; silence is not approval",
                    "next": f"Run agent-box workflow review {task.id} again to read the answer",
                    "review_budget": read_review_budget(root, binding, task, settings.max_rounds)}
        with lock(root, "review-budget"):
            state = read_review_state(root, binding, task)
            if state.get("pending") == round_id:
                state.pop("pending", None)
                state.pop("pending_backend", None)
                save_review_state(root, binding, task, state)
            failures = state.get("failures", 0)
        raise AgentBoxError(
            f"{provider} published no verdict for {head[:12]} in " + (url or pr["url"])
            + ("; its answer carries no single Verdict line. " if url else
               f" within {settings.wait_seconds}s. ")
            + f"The round ended as failure {failures} of {REVIEW_FAILURE_LIMIT} without spending an attempt. "
            f"Ask the user before rerunning workflow review {task.id} or reviewing this head another way")
    body = request["body"] if request is not None else request_body(provider, head, review_context, brief)
    round_id = secrets.token_urlsafe(18)
    budget = start_review(root, binding, task, settings.max_rounds, round_id, fingerprint, "cloud")
    descriptor = review_descriptor(identity, task, fingerprint, round_id)
    # The published request is this round's canonical input, so a later answer is
    # reconciled against the text the reviewer was actually given.
    path = review_evidence_path(root, task.id, round_id)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write_text(path / "handoff.txt", body, 0o400)
    bind_delegation_input(root, descriptor, body)
    if request is None:
        with tempfile.TemporaryDirectory(prefix="agent-box-pr-") as temporary:
            file = Path(temporary) / "comment.md"
            file.write_text(body)
            gh(root, "pr", "comment", number, "--repo", repo, "--body-file", file)
    return {"task": task.id, "round": round_id, "scope": fingerprint, "provider": provider,
            "pull_request": pr["url"], "requested_head": head, "wait_seconds": settings.wait_seconds,
            "outcome": ("request published; the reviewer answers in the pull request" if request is None
                        else "this head already carries a request; reading its answer"),
            "next": f"Run agent-box workflow review {task.id} again to read the verdict",
            "review_budget": budget}
