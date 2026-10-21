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

import contextlib
import datetime
import json
import re
import secrets
import shlex
import tempfile
from pathlib import Path

from .atomicio import atomic_write_text
from . import git_workflow as gw
from .delegation import REVIEW_SCOPE, review_language
from .errors import AgentBoxError
from .provider_runner import ProviderFailure, review_report

#: The reviewer is one GitHub account, not "some bot": another app's review of the
#: same commit is not the answer we asked for, and a no-findings reading of it
#: would be a PASS nobody gave.
BOT = {"claude": "claude[bot]", "codex": "chatgpt-codex-connector[bot]"}
#: Codex prints its priority as a badge image; only P0 and P1 reach GitHub today.
CODEX_PRIORITY = re.compile(r"!\[(P[0-3]) Badge\]")
CODEX_SEVERITY = {"P0": "CRITICAL", "P1": "HIGH", "P2": "MEDIUM", "P3": "LOW"}
#: Both Codex answers, the summary comment and the review, name what they read.
#: It abbreviates to ten characters; a shorter prefix names too many commits to
#: tell this head from an older one.
CODEX_COMMIT = re.compile(r"\*\*Reviewed commit:\*\*\s*`([0-9a-f]{10,64})`")
#: The Action's own arguments, inline or as a block scalar, and nothing else in the file.
CLAUDE_ARGS = re.compile(r"^(\s*)claude_args\s*:\s*(.*)$")
#: A mention anywhere in a comment summons that bot: the Action triggers on the words
#: of the comment, not on who is addressed. Quoted text therefore carries none.
MENTION = re.compile(r"@(?=\w)")
#: A key whose value is a block scalar; everything indented under it is that value.
BLOCK_KEY = re.compile(r"^(\s*)[\w.-]+\s*:\s*[|>][-+]?\s*$")

#: GitHub refuses a longer comment, and a truncated requirement is a requirement nobody read.
COMMENT_LIMIT = 65536

REQUEST_FORM = """@{provider} review

Review commit {head}, as this pull request stands against base commit {base}, against the
requirements quoted below, the repository's guidance and this pull request's description.
Read only: change no files, push nothing, and answer in this pull request.
{scope}
{language}
Report every finding on its own line as

- [LOW|MEDIUM|HIGH|CRITICAL] path:line — what is wrong and why it matters

and end the answer with exactly these three lines, replacing the alternatives with the
one verdict that holds:

Reviewed-Commit: {head}
Reviewed-Base: {base}
Verdict: PASS|NEEDS_WORK|INCONCLUSIVE

A PASS lists no findings. Write exactly one Verdict line and no example verdicts anywhere
in the answer.{context}{brief}

---

The requirements, quoted from the tracker; the first card is the task under review. They
carry no comments and no checkmarks, and the bot has no other access to them.

{cards}
"""


def quoted(text):
    """Text this request repeats rather than says, with its mentions defused.

    A task description that names `@claude` would summon Claude from a request
    addressed to Codex, because the Action reads the comment's words and not its
    addressee. The HTML comment disappears when GitHub renders the mention and
    keeps the trigger from matching.
    """
    return MENTION.sub("@<!-- -->", text.strip())


def request_body(provider, head, base, descriptions, review_context="", brief=None, language="", behavior_version="v2"):
    """The request names both commits of the scope it asks about, because that is the
    only place a published request can record which diff the answer will describe.

    It also carries the task descriptions themselves. A local reviewer is handed
    them as files; the bot reads only what this comment says, and a pull request
    body is the author's summary, not the requirements a verdict is measured against.

    The verdict line keeps its alternatives: a spelled-out example is copied back
    sooner or later, and a copied `PASS|NEEDS_WORK|INCONCLUSIVE` ends the round as
    a failure the user sees, while a copied `NEEDS_WORK` would be a verdict nobody gave.
    """
    context = f"\n\n{quoted(review_context)}" if review_context else ""
    note = f"\n\nContext from the coordinator:\n\n{quoted(brief)}" if brief else ""
    cards = "\n\n---\n\n".join(quoted(text) for text in descriptions.values())
    from .workflow_behavior import instructions
    return REQUEST_FORM.format(provider=provider, head=head, base=base, context=context,
                               brief=note, cards=cards, scope=REVIEW_SCOPE.strip(),
                               language=review_language(language) + instructions(behavior_version, "reviewer"))


def paginate(root, repo, path):
    from .github_workflow import gh

    pages = gh(root, "api", "--paginate", "--slurp", f"repos/{repo}/{path}", json_output=True)
    return [item for page in pages for item in page]


def moment(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def ours(comment):
    """Anyone may comment on a pull request, and our own request is not even
    recognisable by account type: `gh` authenticated as a GitHub App posts it as a
    bot. Only the reviewers' own accounts are excluded, so a bot quoting a request
    back is never mistaken for one."""
    return comment["user"]["login"] not in set(BOT.values())


def same(body, other):
    """GitHub returns a comment as it stores it, and line endings are the one
    difference it may introduce in text we posted ourselves."""
    return body.replace("\r\n", "\n").strip() == other.replace("\r\n", "\n").strip()


def find_request(comments, bodies):
    """The published comment that is one of our own request texts, word for word.

    Provenance cannot come from the author: anybody may comment, and a stranger
    could otherwise publish `@claude review` naming this scope, with instructions
    of their own, and the answer to that question would be read as our round's.
    An identical text is our question whoever pressed the button, and a different
    one is somebody else's, so this run publishes its own.
    """
    for comment in comments:
        if ours(comment) and any(same(comment["body"], body) for body in bodies):
            return comment
    return None


def stale_request(comments, provider, head, base):
    """A request already published for this same head, about another base.

    Codex names only the commit it read, so after the base moves its answer to
    the older question is indistinguishable from an answer to the new one, and
    accepting either would accept a verdict about a diff nobody asked about.
    Claude echoes the base back and needs no such rule.
    """
    mention = f"@{provider} review"
    return any(ours(comment) and comment["body"].startswith(mention)
               and f"Reviewed-Commit: {head}" in comment["body"]
               and f"Reviewed-Base: {base}" not in comment["body"]
               for comment in comments)


def claude_answer(root, repo, number, comments, head, base, since):
    """Claude answers in the reviewer's own form, so its comment is read as one.

    Both commits of the scope must be echoed back: an answer in flight when the
    base moved describes the diff of the question it was asked, not this one.
    """
    for comment in comments:
        if (comment["user"]["login"] != BOT["claude"] or moment(comment["created_at"]) <= since
                or f"Reviewed-Commit: {head}" not in comment["body"]):
            continue
        if f"Reviewed-Base: {base}" not in comment["body"]:
            if "Reviewed-Base:" in comment["body"]:
                # It answered the question this head was asked before the base moved.
                continue
            return None, comment["html_url"], "its answer does not echo the base the request named"
        try:
            return {**review_report(comment["body"]), "text": comment["body"]}, comment["html_url"], None
        except ProviderFailure:
            # It answered about this head and its answer carries no single verdict.
            # Waiting an hour for a finished comment to improve helps nobody.
            return None, comment["html_url"], "its answer carries no single Verdict line"
    return None, None, None


def codex_findings(root, repo, number, head, since):
    """Codex attaches its priorities to inline comments on the commit it reviewed.

    Codex also reviews on its own, so this head may already carry findings from a
    review nobody asked for; counting those would turn its "no major issues" into
    a NEEDS_WORK for the question we did ask.
    """
    findings, text = [], []
    for comment in paginate(root, repo, f"pulls/{number}/comments?per_page=100"):
        priority = CODEX_PRIORITY.search(comment["body"])
        if (comment["user"]["login"] != BOT["codex"] or comment.get("commit_id") != head
                or not priority or moment(comment["created_at"]) <= since):
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
               if review["user"]["login"] == BOT["codex"] and review.get("commit_id") == head
               and review.get("state") == "COMMENTED" and review.get("submitted_at")]
    answers += [(moment(comment["created_at"]), comment["body"], comment["html_url"])
                for comment in comments
                if comment["user"]["login"] == BOT["codex"]
                and (named := CODEX_COMMIT.search(comment["body"])) and head.startswith(named[1])]
    # GitHub timestamps are whole seconds, so an answer stamped with the request's
    # own second may predate it; the reviewer needs minutes, not one second.
    answers = [item for item in answers if item[0] > since]
    if not answers:
        return None, None, None
    findings, rendered = codex_findings(root, repo, number, head, since)
    body = "\n\n".join(text for _, text, _ in sorted(answers))
    verdict = "NEEDS_WORK" if findings else "PASS"
    return ({"verdict": verdict, "findings": findings,
             "text": "\n\n".join([body, *rendered, f"Verdict: {verdict}"])},
            sorted(answers)[0][2], None)


def read_answer(root, repo, number, provider, comments, head, base, since):
    if provider == "claude":
        return claude_answer(root, repo, number, comments, head, base, since)
    return codex_answer(root, repo, number, comments, head, since)


def action_step(lines):
    """The lines of the step that runs the Claude Action, and nothing after it.

    Another job in the same workflow may carry its own `claude_args`, and reading
    that one would report a pin this Action never receives.
    """
    start = next((index for index, line in enumerate(lines)
                  if "anthropics/claude-code-action@" in line
                  and line.split("#", 1)[0].strip().removeprefix("- ").startswith("uses:")), None)
    if start is None:
        return []
    indent = len(lines[start]) - len(lines[start].lstrip())
    step = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() and (len(line) - len(line.lstrip()) < indent or line.lstrip().startswith("- ")):
            break
        step.append(line)
    return step


def claude_args(lines):
    """The arguments the Claude Action's own step is configured with.

    A search over the whole YAML would accept a flag written in a comment or in
    another step, so the `claude_args` value itself is extracted, in both the
    inline and the block form, and split into whole arguments. Another key's block
    scalar is skipped as well: a prompt that spells out `claude_args` is prose the
    Action never parses.
    """
    inside = None
    for index, line in enumerate(lines):
        indent = len(line) - len(line.lstrip())
        if inside is not None and (not line.strip() or indent > inside):
            continue
        inside = None
        block = BLOCK_KEY.match(line)
        found = CLAUDE_ARGS.match(line)
        if block and not found:
            inside = len(block[1])
            continue
        if not found:
            continue
        indent, value = found[1], found[2].strip()
        if value in ("|", ">", "|-", ">-", "|+", ">+"):
            block = []
            for following in lines[index + 1:]:
                if following.strip() and not following.startswith(indent + " "):
                    break
                block.append(following.strip())
            value = " ".join(block)
        elif len(value) > 1 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        try:
            return shlex.split(value)
        except ValueError:
            return []
    return []


def pinned(args, flag, value):
    """`--model opus` is not pinned by `--model opus-v2`, so arguments are compared whole."""
    return any(item == f"{flag}={value}" or (item == flag and args[index + 1:index + 2] == [value])
               for index, item in enumerate(args))


def configured_reviewer(root, config):
    """Refuse an override the cloud reviewer cannot honour before publishing anything."""
    provider = config.github_provider
    if provider == "codex" and (config.github_model or config.github_effort):
        raise AgentBoxError("GitHub @codex review cannot pin model/effort; clear cloud overrides or select local review")
    if provider == "claude":
        workflow = root / ".github/workflows/claude.yml"
        if not workflow.is_file():
            raise AgentBoxError("Configure a Claude GitHub Action first, or use local review")
        step = action_step(workflow.read_text().splitlines())
        if not step:
            raise AgentBoxError("No step in .github/workflows/claude.yml uses the Claude Action; "
                                "a comment naming it answers nothing. Use local review or fix the workflow")
        args = claude_args(step)
        for flag, value in (("--model", config.github_model), ("--effort", config.github_effort)):
            if value and not pinned(args, flag, value):
                raise AgentBoxError(f"Claude Action must configure claude_args with {flag} {value}; comments cannot pin it")
    return provider


def published_requests(root, task, provider, head, base):
    """The request texts this task published from this checkout, for this reviewer
    and this scope.

    A round that ended in failure leaves its request in the thread, and a rerun
    must recognise it even when a later run would word the brief differently:
    asking the bot the same question twice is the one thing this command must not
    do. A request about another scope, or addressed to the other bot, is another
    question: a Codex run must not read Claude's answer, nor stay silent because
    Claude was already asked.
    """
    from .workflow import review_evidence_path

    rounds = review_evidence_path(root, task.id, "round").parent
    texts = []
    for path in sorted(rounds.glob("*/handoff.txt")):
        with contextlib.suppress(OSError):
            text = path.read_text()
            if text.startswith(f"@{provider} review") and head in text and base in text:
                texts.append(text)
    return texts


def pending_round(root, task, state, fingerprint, provider):
    """The descriptor of the round a previous run left waiting for this same question.

    The stored descriptor is the round's own, including the input it bound, so a
    later run records the answer against what the reviewer was actually given. The
    round's own request says which bot was asked, and the answer of the other one
    cannot continue it.
    """
    from .workflow import review_evidence_path

    round_id = state.get("pending")
    if not round_id or state.get("pending_backend") != "cloud" or any(
            item["round"] == round_id for item in state.get("results", [])):
        return None
    path = review_evidence_path(root, task.id, round_id)
    try:
        descriptor = json.loads((path / "descriptor.json").read_text())
        asked = (path / "handoff.txt").read_text()
    except (OSError, ValueError):
        return None
    if descriptor.get("scope") != fingerprint or descriptor.get("task") != task.id:
        return None
    return descriptor if asked.startswith(f"@{provider} review") else None


def review(root, binding, adapter, config, task, parent, tasks, base, fingerprint, identity,
           brief, review_context):
    """Publish one request per head, then read that head's verdict back.

    Waiting is not a failure and spends nothing: the same command reads the
    answer later. Only the wait budget ends the round, so the user decides what
    to do about a bot that never answered.
    """
    from .github_workflow import find_pr, view
    from .workflow import lock, read_evidence, review_result, scope_fingerprint

    settings = config.workflow.review
    if not (config.credentials.github.token or config.credentials.github.config):
        raise AgentBoxError("Enable the GitHub integration in agent-box config before a cloud review, "
                            "or select local review with workflow.review.github_provider")
    repo = gw.github_origin(root)
    gw.require_clean(root)
    head, base_head = gw.head_sha(root), gw.base_commit(root, base)
    if scope_fingerprint(head, base_head) != fingerprint:
        # The scope was fingerprinted before this command reached GitHub. Asking about
        # a head that has already moved spends a paid answer on a stale question.
        raise AgentBoxError(f"The head or base moved while this command was starting; "
                            f"run agent-box workflow review {task.id} again")
    branch = gw.current_branch(root)
    number = find_pr(root, repo, branch, base)
    if not number:
        raise AgentBoxError(f"GitHub has no pull request for {branch} into {base}; open it with ab-pr "
                            "before the cloud review of this head")
    pr = view(root, repo, number)
    if pr["headRefOid"] != head:
        raise AgentBoxError(f"PR #{number} is at {pr['headRefOid'][:12]}, not the local {head[:12]}; "
                            "publish this head before asking its reviewer")
    if pr["headRefName"] != branch or pr["baseRefName"] != base:
        # A pull request retargeted between the lookup and this read describes another
        # question, and its answer would be recorded for this task all the same.
        raise AgentBoxError(f"PR #{number} now merges {pr['headRefName']} into {pr['baseRefName']}, "
                            f"not {branch} into {base}; resolve the pull request before reviewing it")
    # One published request per scope: the lock keeps a second invocation on this
    # checkout from asking again, or from starting a second round on one answer,
    # between reading the recorded verdict and acting on it.
    with lock(root, "review"):
        saved = read_evidence(root, task)
        if saved.get("scope") == fingerprint and saved.get("review", {}).get("round"):
            # This scope already has its answer. Asking a bot the same question again
            # is not another review, and re-reading the same comment is not a round.
            return {**review_result(root, binding, adapter, task, config, saved["review"]["round"]),
                    "outcome": "this head already has a recorded verdict; no request published, no round started"}
        return _round(root, binding, adapter, config, task, parent, tasks, fingerprint, identity,
                      brief, review_context, repo, number, pr, head, base_head, settings)


def _round(root, binding, adapter, config, task, parent, tasks, fingerprint, identity, brief,
           review_context, repo, number, pr, head, base_head, settings):
    """Open or continue the one round this scope's published request belongs to."""
    from .github_workflow import gh
    from .workflow import (REVIEW_FAILURE_LIMIT, bind_delegation_input, lock, read_review_budget,
                           read_review_state, review_descriptions, review_descriptor,
                           review_evidence_path, review_result, save_review_state, start_review,
                           store_review_result)

    comments = paginate(root, repo, f"issues/{number}/comments?per_page=100")
    provider = configured_reviewer(root, settings)
    descriptions = review_descriptions(task, parent, tasks)
    if provider == "codex" and stale_request(comments, provider, head, base_head):
        raise AgentBoxError(
            f"{head[:12]} was already asked about another base, and Codex names only the commit it "
            f"read, so an answer for {base_head[:12]} cannot be told from an answer to that older "
            f"question. Commit this work again, or review this head with workflow review {task.id} --native")
    with lock(root, "review-budget"):
        state = read_review_state(root, binding, task)
        descriptor = pending_round(root, task, state, fingerprint, provider)
    # A request already in the thread is this round's only when its text is one of
    # ours; anything else is a stranger's question, and this run publishes its own.
    request = find_request(comments, [*published_requests(root, task, provider, head, base_head),
                                      request_body(provider, head, base_head, descriptions,
                                                   review_context, brief, config.workflow.language,
                                                   config.workflow.behavior_version)])
    if descriptor is not None:
        round_id = descriptor["delegation"]
        report, url, note = None, None, None
        if request is not None:
            asked = moment(request["created_at"])
            report, url, note = read_answer(root, repo, number, provider, comments, head, base_head, asked)
        else:
            # GitHub may have accepted the comment while gh reported an error, and a
            # fresh comment may not be listed yet. Publishing a second request would
            # pay twice for one question, so this round waits for its own to appear.
            asked = datetime.datetime.now(datetime.timezone.utc)
            with contextlib.suppress(OSError):
                asked = datetime.datetime.fromtimestamp(
                    (review_evidence_path(root, task.id, round_id) / "handoff.txt").stat().st_mtime,
                    datetime.timezone.utc)
            note = "the request this round published is not in the pull request"
        if report is not None:
            store_review_result(root, binding, task, descriptor,
                                {"route": {"provider": provider, "model": settings.github_model or "cloud",
                                           "backend": "cloud", "answer": url},
                                 "report": report, "diagnostics": {"usage": None}}, "cloud")
            return review_result(root, binding, adapter, task, config, round_id, reconcile=True)
        waited = (datetime.datetime.now(datetime.timezone.utc) - asked).total_seconds()
        if url is None and waited <= settings.wait_seconds:
            return {"task": task.id, "round": round_id, "scope": fingerprint, "provider": provider,
                    "pull_request": pr["url"], "requested_head": head,
                    "waited_seconds": int(waited), "wait_seconds": settings.wait_seconds,
                    "outcome": ("no answer for this head yet; silence is not approval" if request is not None
                                else f"{note}; no second request published"),
                    "next": f"Run agent-box workflow review {task.id} again to read the answer",
                    "review_budget": read_review_budget(root, binding, task, settings.max_rounds)}
        with lock(root, "review-budget"):
            state = read_review_state(root, binding, task)
            if state.get("pending") == round_id:
                state.pop("pending", None)
                state.pop("pending_backend", None)
                save_review_state(root, binding, task, state)
            failures = state.get("failures", 0)
        # The binding goes, so nothing can resume this round, but the request text
        # stays: it is already published on GitHub, and it is what keeps the next
        # round from paying the bot for a question it was asked once.
        with contextlib.suppress(OSError):
            (review_evidence_path(root, task.id, round_id) / "descriptor.json").unlink(missing_ok=True)
        raise AgentBoxError(
            f"{provider} published no verdict for {head[:12]} in " + (url or pr["url"])
            + (f"; {note}. " if note else f" within {settings.wait_seconds}s. ")
            + f"The round ended as failure {failures} of {REVIEW_FAILURE_LIMIT} without spending an attempt. "
            f"Ask the user before rerunning workflow review {task.id} or reviewing this head another way")
    body = (request["body"] if request is not None
            else request_body(provider, head, base_head, descriptions, review_context, brief,
                              config.workflow.language, config.workflow.behavior_version))
    if request is None and len(body) > COMMENT_LIMIT:
        raise AgentBoxError(
            f"The request for {head[:12]} is {len(body)} characters and GitHub accepts {COMMENT_LIMIT}; "
            f"shorten the task descriptions or review this head with workflow review {task.id} --native")
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
