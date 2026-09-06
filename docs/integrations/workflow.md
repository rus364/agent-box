# Task workflow

Enable **workflow** in `agent-box config`, configure tracker access, and run
`ab-setup` in a repository. This installs eight short owned skills:
`ab-tracker`, `ab-setup`, `ab-interview`, `ab-plan`, `ab-work`, `ab-review`,
`ab-pr`, `ab-land`. They compose around the external tracker, without a `.flow`
task database, bootstrap framework, update patches or agent-turn push hooks.

| State | Meaning |
| --- | --- |
| Backlog | Requirements or scheduling are not ready |
| Ready | Specified; dependencies and ownership are checked before starting |
| Doing | Assigned to the API-key user; implementation or review fixes underway |
| Review | Parent implementation accepted locally; PR/review/merge pending |
| Done | Subtask integrated and validated, or parent actually merged into base |

A parent task represents a deliverable/spec, and subtasks belong to the bound
project or, with a recursive binding, to any project under it. Each task owns the
branch namespace `ab/<task id>-*`, so the branch is found back in Git instead of
recorded anywhere. A local branch wins; failing that a branch already pushed as
`origin/ab/<task id>-*` is adopted with a tracking branch, so a fresh clone or
another machine continues the work rather than forking the base a second time.
Two branches claiming one task, a local and a remote name that disagree, or a
local branch whose remote has moved ahead all stop instead of choosing.

The base is origin's default branch, asked of the remote itself, because
`refs/remotes/origin/HEAD` is only as fresh as the last fetch and a renamed
default branch would otherwise keep receiving work at its old name. The local ref
remains the offline answer. The orchestrator creates the parent branch from that
base, then creates each subtask branch from the parent, executes it sequentially,
and integrates it before starting the next. No parallel workers or automatic
branch creation by delegated agents. Read-only scouts may run concurrently within
`scout_limit`. A finished sibling unblocks after integration; a dependency in
another parent remains blocked until that parent's merge.

The primary can do tiny work directly. Larger tasks use explicit English
handoffs; tracker comments and descriptions use the task's language. Requirements,
relations and GitHub writes belong to the orchestrator. Workers may post progress
and check existing items. Reviewers return findings to the orchestrator, which
comments in the task and unchecks contradicted items. A checkbox is not a PASS.

Tracker comments carry no workflow state. Branch, base and pull request come from
Git and GitHub; lifecycle comes from the tracker's own fields; the provider that
actually wrote the code is local evidence. So a worker, which may comment, cannot
reach any of it by writing one.

A worker changes working files and nothing else. Delegation takes a baseline -
the branch, HEAD, a digest of every local ref, the task requirements and the
lifecycle of the whole task tree - and the report is refused if the worker
committed, moved a branch or a tag, or changed requirements, relations, state or
assignment. This comparison, not the role environment variable the worker could
unset, is what makes the boundary checkable. Accepting a worker report also drops
any validation or review acceptance recorded before it, so a later PASS always
describes code the worker actually left behind.

The baseline never touches disk. A delegate runs as the same user, so anything in
`WORKFLOW_STATE` is writable by it, and a saved baseline could simply be rewritten
to describe whatever it did. `prepare` therefore returns an opaque `handoff`
token - the baseline, the scope and the selected route, bound to this repository
and task - which the orchestrator holds and passes to `report --handoff`. The
delegate never sees it, so the copy that decides acceptance is out of its reach.
The same token carries the route, so a delegate cannot widen the providers its
report may claim either. Keep it out of briefs and prompts.

Claude workers and reviewers run with `--safe-mode`, which is what keeps the
context free of the user's hooks, skills, MCP servers and agents - and which also
disables CLAUDE.md discovery. The repository's own `AGENTS.md` and `CLAUDE.md`
therefore travel in the handoff itself, together with an instruction to read the
nested ones next to the files being changed. `prepare` returns the same text as
`repository_instructions` for native delegation.

## Models and routing

Configure an exact model ID and effort for each provider in four tiers. Model
fields start empty rather than guessing what the account can access. Tier 0 is
for facts, scouting and structuring; task difficulty remains 1-3, represented in
Vikunja by one `ab:tier:1`, `ab:tier:2` or `ab:tier:3` label. A user-set task tier
wins over an orchestrator suggestion. Each subtask gets its own assessment.

Workers/scouts choose one of three modes:

- **current**: only the session's provider;
- **weighted**: smooth weighted round robin, independently per tier;
- **usage-aware**: fresh account/model budget windows, otherwise static weights.

Static weights represent relative usable subscription capacity, not dollar
prices. `1:1` replaces a separate 50/50 mode; `5:1` gives the first provider five
shares. Zero excludes a provider from workers/scouts. A user supplies weights
because subscription prices do not reliably measure interchangeable token
budgets across models and providers.

When both candidates have fresh weekly data, choose the highest score:

```text
score = weight * max(0, remaining_percent - reserve_percent) / 100
        / (hours_until_weekly_reset + smoothing_hours)
```

Defaults: reserve 5%, smoothing 24 hours. Model-specific weekly windows constrain
the score by taking the smallest applicable score. Short windows gate availability
but do not change weekly reset preference. This is an explicit budget-pacing
heuristic, not a claim of optimal scheduling: it favors available weekly capacity
and earlier weekly resets, with smoothing preventing a last-minute spike. Missing
or failed freshness uses static weights; known exhausted windows with a future
reset remain excluded even if the refresh fails. Active workers are not preempted.

`agent-box usage --json --no-cache` requests live usage. A failed refresh has
`fresh: false`, retains any useful cached exhaustion evidence and exits nonzero.
The provider APIs may themselves delay updates; bypassing the local cache cannot
remove server-side lag.

Review routing is independent: prefer the other **actual writer** provider, then
a fresh context from the same provider. Worker weights and usage do not select
reviewers. Fallback defaults on; strict mode stops instead. All attempts preserve
the selected tier and configured model/effort. An unsupported model is an error,
not permission to guess a cheaper model.

`config apply` installs owned native agent profiles with exact model/effort.
Use native delegation only when its tool can enforce those settings; otherwise
`codex exec` or `claude -p` creates the context inside the existing host/container.
Provider errors, including Claude exit-zero error envelopes, are not review
findings. A worker failure after possible side effects requires reconciliation.

## Validation, review and completion

```bash
agent-box workflow begin TASK --primary codex
agent-box workflow prepare TASK --role worker --primary codex
agent-box workflow run TASK --role worker --primary codex --brief /tmp/brief.md
# The primary checks evidence and commits the resulting work.
agent-box workflow validate TASK -- python3 -m unittest discover -s tests
agent-box workflow review TASK --primary codex
agent-box workflow finish TASK
```

Native handoffs use `prepare`, then `report --file FILE --scope DIGEST --handoff
TOKEN --provider PROVIDER` for the returned structured report. Evidence is disposable local cache,
bound to code, task requirements and scope; another device may need to rerun
validation/review. Only the actual writer provider survives a scope change,
because it decides who reviews the result.

Review policy is off/auto/always. Auto considers explicit review labels, sensitive
paths, changed-file count and added/deleted lines (binary changes require review).
Priority is unrelated. Presets are thorough (2 files/80 lines), balanced (5/200)
and light (10/500); all values and paths are editable. Apply the policy both to
subtasks and the complete parent diff. SKIPPED, INCONCLUSIVE and provider errors
are distinct from PASS. Code or requirement changes invalidate acceptance.

`ab-land` bounds fix/review iterations by `max_rounds` (default 3), stopping on
ambiguity or no progress. The skill keeps iteration history in the tracker; it
must not reset the bound by restarting. Validation is a real command with an
observed exit status; the orchestrator does not self-review.

## GitHub

The enabled GitHub integration and workflow `auto_push` (on within this opt-in
workflow) push only the parent after a completed subtask. Push checks the live
remote default branch, unique effective destination, clean worktree and ancestry;
no forced repair, tag/submodule side pushes or child-branch auto-push.

After parent acceptance, offer `ab-pr`. On user request it opens one PR, following
repository templates. Later commands find that PR on GitHub by head branch and
base rather than from a stored number: an open one wins, a single merged one
answers a reconcile, and several of either stop the command. Completion from a
merged PR additionally requires its merged head to be the branch's current head,
so commits added after an old merge on a reused branch cannot ride that merge to
Done - squash and rebase merges rewrite the commit, which leaves head equality as
the only honest test. `ab-land` reads
CI, comments and paginated unresolved threads, fixes sequentially, obtains
current-head acceptance and merges with `--match-head-commit`. Branch protection
is never bypassed. Merge-queue entry is not completion. If the tracker write
fails after merge, `workflow land PARENT reconcile` retries only reconciliation.

`workflow.review.github_provider` selects auto/local/codex/claude. Auto uses local
fresh review without assuming a cloud reviewer is installed. Explicit cloud Codex
uses `@codex review`; its mention interface cannot pin a model or effort. Claude
uses the repository's Claude Action; configure model/effort in `claude_args`, not
in a comment pretending to override the Action. The command checks configured
pins and reports unsupported configurations. `wait_seconds` bounds waiting.
Silence or a stale review never means acceptance; allowed fallback uses local
fresh review. GitHub approvals and required checks still follow repository policy.
