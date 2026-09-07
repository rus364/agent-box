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
| Review | Parent implementation accepted locally; configured landing pending |
| Done | Subtask integrated and validated, or parent proven merged into base |

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
remains the offline answer. The coordinator creates the parent branch from that
base, then creates each subtask branch from the parent, executes it sequentially,
and integrates it before starting the next. No parallel workers or automatic
branch creation by delegated agents. Read-only scouts may run concurrently within
`scout_limit`. A finished sibling unblocks after integration; a dependency in
another parent remains blocked until that parent's merge.

The coordinator is the only writer. It alone writes tracker requirements,
lifecycle state, assignees, relations and dependencies, progress comments and
checkboxes; it alone creates branches, commits and pushes; it alone touches
GitHub, pull requests and merges. The coordinator may also do tiny work itself.
Larger tasks use explicit English handoffs; tracker comments and descriptions
use the task's language.

A worker reads its task and the repository, changes project files in the working
tree, and returns a structured report. It never commits, pushes, switches or
creates a branch, never writes the tracker, and never writes GitHub. Progress
and evidence travel back in the report, and the coordinator decides from it what
to record. Reviewers and scouts are read-only: they change no project file and
no tracker, Git or GitHub state either, and return findings the coordinator
comments into the task, unchecking contradicted items. A checkbox is not a PASS.

A native subagent of the coordinator's session and a managed `codex exec` /
`claude -p` run share exactly this logical contract. It is a contract boundary,
not credential isolation - a native delegation inherits the coordinator's
workspace, `.git` and credentials - so neither backend gets private
restrictions, and what follows a worker report is detection of an obvious
contract violation rather than a perimeter. Five cheap facts are compared
against what the delegation was prepared with: the repository checkout and its
tracker binding, the branch, HEAD, the tip of the branch a subtask integrates
into, and that the task is still Doing and assigned to the coordinator's own
user. There is no digest of the ref store, the requirements or the task tree.
Accepting a worker report also drops any validation or review acceptance
recorded before it, so a later PASS always describes code the worker actually
left behind; acceptance is retired when the delegation starts as well, so a
worker that fails or never answers leaves no stale PASS standing.

Tracker comments carry no workflow state at all. Branch, base and pull request
come from Git and GitHub, lifecycle comes from the tracker's own fields, and the
provider that actually wrote the code is local evidence.

`prepare` returns a `handoff` descriptor: plain, readable JSON, deliberately not
treated as unforgeable and not a security token. It ties a later report to the
repository, tracker project, task, scope, role, tier and route the delegation
was prepared for, so the coordinator cannot ingest a report against the wrong
one of several delegations in flight. The coordinator holds the descriptor and
passes it back to `report --handoff`; each delegation needs its own `prepare`.

Claude workers and reviewers run with `--safe-mode`, which is what keeps the
context free of the user's hooks, skills, MCP servers and agents - and which also
disables CLAUDE.md discovery. The repository's own `AGENTS.md` and `CLAUDE.md`
therefore travel in the handoff itself, together with an instruction to read the
nested ones next to the files being changed. `prepare` returns the same text as
`repository_instructions` for native delegation.

## Models and routing

Configure worker/scout model IDs and effort in `workflow.tiers` (tiers 0-3),
and independent reviewer settings in `workflow.review.tiers` (tiers 1-3), each
with `codex` and `claude` entries containing `model` and `effort`. Both matrices
are available in the configuration menu and `config show`, and travel in the
container's minimal workflow configuration. Empty reviewer models disable those
candidates; they never inherit worker models. The
effort value `default` omits agent-box's effort override; it does not disable
thinking or pass a literal `none` value to the provider. The existing default
for newly created configuration remains `medium`. Model fields start empty
rather than guessing what the account can access. Tier 0 is
for facts, scouting and structuring; task difficulty remains 1-3, represented in
Vikunja by one `ab:tier:1`, `ab:tier:2` or `ab:tier:3` label. A user-set task tier
wins over a coordinator suggestion. Each subtask gets its own assessment.

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

Set `workflow.review.routing` in `agent-box config` under workflow / review:

- **cross-provider** (default): prefer the other **actual writer** provider, then
  a fresh context from the writer provider when `workflow.fallback` is enabled
  (the default). With fallback disabled, only the other provider is allowed.
- **same-provider**: use only the actual writer provider in fresh context.
  An unavailable writer executable, empty reviewer model or provider failure
  stops review without calling the other provider, even with fallback enabled.

Both modes use only `workflow.review.tiers`. Worker weights and usage do not
select reviewers. Routing changes neither review policy nor worker/scout routing.
All attempts preserve the selected tier and configured reviewer model/effort.
An unsupported model is an error, not permission to guess a cheaper model.

`config apply` installs owned native agent profiles with exact model/effort. A
profile configured with effort `default` omits the effort field, but a native
subagent may still inherit effort from its parent session. Such a route reports
`native_profile_ready=false` and uses managed `codex exec` or `claude -p`, which
can omit the CLI override and clear inherited Claude effort. Use native
delegation only after checking the available role's provider, model and effort
against the exact selected candidate and verifying the tool can enforce them.
The role name and `native_profile_ready` alone do not establish the runtime
settings. Each fallback candidate also reports its native profile readiness;
check that candidate's settings before choosing it. Otherwise
the managed CLI creates the context inside the existing host/container.
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
DESCRIPTOR --provider ACTUAL_PROVIDER --model ACTUAL_MODEL --effort ACTUAL_EFFORT`
for the returned structured report. Supply `--fallback-reason REASON` when using
a fallback candidate; unknown model or effort is a reason to use managed CLI,
never to guess report fields. Report ingestion checks role/tier and exact
provider/model/effort against the prepared candidates before changing acceptance.
Accepted worker/reviewer evidence records the full route and any native fallback
reason or managed provider failures. These are coordinator-supplied execution
facts, not provider attestation. Evidence is
disposable local cache, bound to code, task requirements and scope; another
device may need to rerun validation/review. Only the actual writer provider survives a scope change,
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
observed exit status; the coordinator does not self-review.

## Landing mode

`workflow.merge_mode` selects `github` (the default) or `local`. Both routes
derive the base from origin's default branch and retain scoped validation,
configured local review policy, ownership, dependency, subtask and checklist
checks. `workflow finish PARENT` moves accepted parent work to Review and points
the coordinator at the configured route.

Local mode keeps task publication and GitHub out of the workflow. It requires
local task and base refs, a clean worktree and the exact parent branch before
landing. `workflow land PARENT inspect` reports the derived refs and blockers;
`workflow land PARENT merge` verifies that the local base is an ancestor of the
validated task head before switching branches, then fast-forwards the base to
that exact commit. `--method` is incompatible because local landing has only
fast-forward semantics. The base is not pushed automatically.

If the tracker update fails after the fast-forward, remain on the base and run
`workflow land PARENT reconcile`. It rereads completion constraints and marks
Done only when the current local task-branch tip is contained in the current
local base. A later commit on the task branch therefore prevents an old merge
from completing new work. Reconciliation does not reuse the pre-merge scope
fingerprint because moving the base necessarily changed it.

Local mode rejects `workflow pr` before any GitHub call. `workflow land
request-review` points `auto` and `local` review selection to `ab-review`, and
rejects explicit cloud Codex or Claude review without contacting GitHub.

## GitHub landing

In the default GitHub mode, the enabled GitHub integration and workflow
`auto_push` (on within this opt-in workflow) push only the parent after a
completed subtask. Push checks the live
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

## Branch cleanup

`workflow.cleanup_local_branches` and `workflow.cleanup_remote_branches` are
independent booleans, both off by default. They are editable in the workflow
config menu and visible in `config show`. With either enabled, landing and
reconciliation attempt cleanup only after proving the merge and successfully
marking the parent Done. Cleanup outcomes appear separately under `cleanup` in
the result; a cleanup failure never reverses merge or Done. With both disabled,
cleanup performs no additional Git or GitHub operations.

Cleanup covers only the completed parent's local branch and its copy on origin.
It leaves child branches alone. Candidates come from the parent's task namespace
in Git, including live origin refs when remote cleanup is enabled. It never
guesses a missing branch from the task's title. Ambiguous names, symbolic refs,
the base/default branch, the current branch and branches used by any worktree
are preserved with a diagnostic. This also protects the remote copy of a branch
that is checked out locally. Cleanup never switches branches.

In GitHub mode, cleanup requires the enabled GitHub integration and one exact
merged PR head/base with a merge timestamp and merge commit. The candidate tip
must equal the merged head, so squash and rebase merges work without treating
local ancestry as proof. After GitHub landing, the parent may still be checked
out: switch to the base yourself and run `workflow land PARENT cleanup`.
In local mode, the candidate head must be an ancestor of the local base.
Remote deletion additionally requires that head in the live published base on
the same effective origin destination. An unpublished local merge, or a missing
commit object, preserves the remote branch with publish/fetch guidance; cleanup
never publishes the base or fetches objects implicitly.

Deletion compares the expected head: local cleanup uses
`git update-ref --no-deref -d REF EXPECTED`, and remote cleanup uses an exact
`--force-with-lease=REF:EXPECTED` with one deletion refspec. The lease authorizes
only deletion, never history repair. The effective push URL must be unique and
usable unchanged for both read and delete; chained URL rewrites are refused.
Tag following, submodule pushes and mirror pushing are disabled for that one
command, without editing user config. Worktree occupancy is checked again near
deletion. As with other workflow operations, concurrent agents cooperate;
expected-head comparison protects against refs moving during the operation.

`workflow land PARENT cleanup` retries the same configured policy for a Done
parent without merging or writing tracker state. Each surviving branch must
prove integration again; Done alone is never merge evidence. Missing branches
are safe no-ops, including after a partial cleanup or a title change.
Reconciliation of an already Done parent also takes this retry route, so it
continues to work after branch deletion. Keep cleanup failures separate from
the successful completion when reporting results.

## GitHub review

`workflow.review.github_provider` selects auto/local/codex/claude. Auto uses local
fresh review without assuming a cloud reviewer is installed. Explicit cloud Codex
uses `@codex review`; its mention interface cannot pin a model or effort. Claude
uses the repository's Claude Action; configure model/effort in `claude_args`, not
in a comment pretending to override the Action. The command checks configured
pins and reports unsupported configurations. `wait_seconds` bounds waiting.
Silence or a stale review never means acceptance; allowed fallback uses local
fresh review. GitHub approvals and required checks still follow repository policy.
