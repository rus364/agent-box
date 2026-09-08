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
wins over a coordinator suggestion. Each subtask may have its own explicit tier. Without a task tier, no assessment or label is required for readiness, audit, claim, validation, review or completion; malformed/conflicting explicit labels remain invalid. An explicit `--tier` invocation override selects configured routing when the card has no tier and never changes its labels.

Untiered workers stay in the active session's provider, model and effort.
`prepare` and `run` return `action: work-in-current-session`, with the task,
handoff and instructions, instead of launching another CLI. The coordinator
implements directly, preserving settings. The action checks assigned Doing
ownership, retires previous acceptance and records the actual primary writer;
it is not evidence that work ran or completed. Pass the active provider with
`--primary` (or `AGENT_BOX_PRIMARY_PROVIDER`). No usage counters, tier matrices
or installed tier profiles participate.

Untiered review uses that active session provider/model/effort in an independent
fresh read-only context, even when explicit-tier review routing prefers another
provider. Without exact session facts, `review` returns
`action: review-in-fresh-context`; use native inheritance without a named tier
profile. The returned route keeps unknown model/effort as JSON null. To ingest
an actual inherited report, use `report --provider PROVIDER --session-inherited
--fresh-context --role reviewer --file FILE --scope SCOPE --handoff DESCRIPTOR`.
The flags affirm preserved settings and independent context; they do not turn
an action, silence or provider error into PASS. If inheritance cannot be
ensured, obtain verified session facts before running a reviewer.

When both actual settings are known, `prepare`, `run` and `review` accept
`--session-model MODEL --session-effort EFFORT`. Values must come from the active
session, not configured profiles; models outside every tier matrix are valid.
With those facts, managed untiered review launches a fresh CLI with that exact
provider/model/effort and no fallback. Native reports must match the prepared
concrete settings and affirm `--fresh-context` for review. For inherited worker
reports, use `--session-inherited` and omit unknown model/effort. Independent
review and validation/finish gates remain required by policy; SKIPPED is
resolved before asking for session settings.

Explicit-tier workers/scouts choose one of three modes:

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

For explicit tiers, both modes use only `workflow.review.tiers`. Worker weights and usage do not
select reviewers. Routing changes neither review policy nor worker/scout routing.
All attempts preserve the selected tier and configured reviewer model/effort.
An unsupported model is an error, not permission to guess a cheaper model.

`config apply` installs owned native agent profiles with exact model/effort.
Native execution has two options: an exact available profile, or explicit spawn
parameters supported by the current tool without an overriding custom agent.
`native_profile_ready` checks only the installed profile; false does not forbid
explicit spawn. Each candidate's `native_explicit_settings` lists required
provider/model/effort, not discovered capabilities or proof of execution.

The coordinator checks the live tool's provider, model availability, effort
support, profile precedence, defaults and inheritance. A matching file, role name
or prompt is not sufficient. For Codex explicit spawn, pass both `model` and
`reasoning_effort`, use fresh context (`fork_turns="none"` where supported), and
avoid named tier profiles; even built-in names can be shadowed. Unknown or
unsupported capabilities require managed CLI with the exact selected settings.
Reviewers always require independent fresh read-only context.

Effort `default` omits agent-box's override; native omission can inherit one.
It therefore has `native_profile_ready=false` and `native_explicit_settings=null`
and uses managed CLI, retaining the existing protection against inherited Claude
effort. This is not a literal provider effort value or a promise to disable thinking.

Concrete `workflow run` and `workflow review` routes execute managed `codex exec`
or `claude -p` inside the existing host/container. They cannot invoke the
coordinator's native tool. Native execution uses `prepare`, coordinator spawn,
then `report`. Provider routing (same/cross-provider) is independent of this
backend choice; neither a new configuration matrix nor a persistent capability
registry is needed. Untiered current-session actions retain their behavior above.
See [native research and smoke evidence](../native-launch.md) for verified
interfaces and limits.
Provider errors, including Claude exit-zero error envelopes, are not review
findings. A worker failure after possible side effects requires reconciliation.

## Validation, review and completion

```bash
agent-box workflow begin TASK --primary codex
agent-box workflow prepare TASK --role worker --primary codex
agent-box workflow run TASK --role worker --primary codex --brief /tmp/brief.md
# The primary checks evidence and commits the resulting work.
agent-box workflow validate TASK -- python3 -m unittest discover -s tests
agent-box workflow review TASK --primary codex --brief /tmp/review-brief.md
agent-box workflow finish TASK
```

The review brief is ephemeral per-round input with changes/reasons, technical risks,
validation commands/results and limitations; it is not stored in tracker comments.
Every native verdict uses a newly spawned reviewer with no inherited coordinator or
reviewer history (`fork_turns="none"` for Codex), never followup/resume. Pass the
returned `review_handoff` unchanged as the initial message. Managed commands remain
ephemeral/no-session-persistence and do not resume a prior review. The bounded handoff
indexes immutable base/head boundaries, task/path counts and samples, and paths and hashes
for temporary read-only evidence containing the complete requirements, diff, changed-path
list, mandatory repository instructions, author claims and recorded validation. The
reviewer reads the normative requirements and diff before consulting untrusted author
claims. Workflow state keeps native and managed paths shared and removes the generated
artifacts after report ingestion or managed review; oversized index metadata fails closed.

The shared prompt checks correctness and unnecessary complexity against current requirements
and consumers. Complexity findings need a concrete present maintenance/cognitive cost or
state-divergence risk and a simpler behavior-preserving alternative, even without a runtime
bug. Local fixes/reuse come first; redesign needs justification, required checks remain,
and style preferences or hypothetical futures do not block PASS. The coordinator verifies
findings before fixing, challenges them with evidence and escalates material disagreement
without inventing independent acceptance. No extra review stage or finding quota is added.

Explicit-tier native handoffs use `prepare --brief FILE`, then `report --file FILE --scope DIGEST --handoff
DESCRIPTOR --provider ACTUAL_PROVIDER --model ACTUAL_MODEL --effort ACTUAL_EFFORT`
for the returned structured report. Supply `--fallback-reason REASON` when using
a fallback candidate, and `--fresh-context` for every native reviewer; unknown
model or effort is a reason to use managed CLI,
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
`workflow inspect TASK` on the task branch reports the effective configuration
source, merge mode, exact scope, validation and every review trigger. Thresholds
are inclusive: two files trigger review when `max_files=2`. `config show` reads
the same rendered configuration inside a container; host-only settings are not
part of that artifact.
Priority is unrelated. Presets are thorough (2 files/80 lines), balanced (5/200)
and light (10/500); all values and paths are editable. Apply the policy both to
subtasks and the complete parent diff. SKIPPED, INCONCLUSIVE and provider errors
are distinct from PASS. Code or requirement changes invalidate acceptance.

`max_rounds` (default 3) is enforced for local reviewer `prepare`, `run` and
`review`: each reserves one attempt under a local lock before handoff or provider
execution. A failed budget write dispatches nothing and allocates no attempt;
after durable reservation, packaging/execution failures still consume it.
Provider fallback belongs to that same attempt. Failed/abandoned
attempts consume budget; report ingestion and policy SKIPPED do not. Separate
budget state is keyed by checkout, tracker provider/server/project and task,
survives scope changes and coordinator restarts, and never resets automatically.
`workflow inspect` exposes `review.budget` with used/limit/remaining; reviewer
command results expose `review_budget` for the coordinator, outside the neutral
reviewer prompt. At exhaustion, only the user's explicit decision permits
`--review-override-reason TEXT` for one additional attempt, with its reason saved.
Every new review retires previous PASS and human acceptance under the reservation
lock, including explicit tiers and provider failures. Result ingestion uses the
same lock and accepts only the latest pending round; late or replayed results
cannot restore acceptance. Remote GitHub review requests remain coordinated
separately. Skills stop on ambiguity or no progress and retain human iteration
history in the tracker. Validation is a real command with an observed exit
status; the coordinator does not self-review.

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
fast-forward semantics. `workflow.auto_push` defaults to true within the opt-in
workflow: local landing publishes only the exact accepted base head to the unique
effective origin before marking Done or running cleanup. It needs no GitHub API
or integration and uses only available Git transport credentials. Setting it to
false completes locally without publication. Local mode never publishes task
branches. Publication checks the live default/base and fast-forward ancestry,
freezes the destination and disables force, mirror, tag and submodule side pushes.
Unknown or diverged remote heads and server rejection stop completion.

For a local parent, the coordinator can record the user's explicit acceptance
of the inspected, validated result as an alternative to independent review:

```bash
agent-box workflow inspect PARENT
agent-box workflow approve PARENT --scope DIGEST --reason 'User accepted the inspected result'
agent-box workflow finish PARENT
agent-box workflow land PARENT merge
```

Use the exact scope returned by inspect, after successful validation and completed
acceptance checks. `approve` records the actor and reason separately as human
acceptance; it does not write a reviewer PASS, change policy or mark the task Done.
As with delegated reports, this is a coordinator contract, not authentication of
the human's identity. A request to avoid delegation alone is not approval. Code
or requirement changes, a worker, or a later independent review retire the
decision. GitHub mode and subtasks do not accept this exception. Ownership,
checklists, dependencies, validation and merge proof remain mandatory, and the
normal post-merge cleanup still runs.

If publication or the tracker update fails after the fast-forward, the local merge
and task branch are preserved and the task stays Review. Remain on the base and run
`workflow land PARENT reconcile`. With auto-push enabled it retries publication
of the exact accepted head without repeating the merge; an advanced local base
is refused, and an already-published exact head safely permits retry after a
tracker failure. An explicit auto-push=false permits local-only recovery.
Done reconciliation and cleanup never opportunistically publish the base.
It rereads completion constraints and marks
Done only when the current local task-branch tip is contained in the current
local base **and** the pre-merge acceptance still covers that exact head, base
identity and task requirements. Before changing Git, landing saves one disposable
acceptance snapshot with the validated scope, original diff size/paths and review
result, including any explicit human acceptance. Recovery applies the current
off/auto/always review policy to that original diff, accepting the original scoped
human decision when present, so advancing the base cannot manufacture acceptance.
Changed requirements or task heads invalidate acceptance even if the new head is
also integrated. Git remains the authority for integration; the snapshot neither
records lifecycle nor selects branch refs. A refusal leaves the parent in Review.

Manual merge or ancestry alone cannot complete an unaccepted parent. If the
acceptance cache is lost or stale, switch to the current task branch, run
`workflow validate PARENT -- COMMAND ARGS`, obtain fresh independent review with
`ab-review` as required, then run `workflow land PARENT merge` again. It rechecks
acceptance and returns to the base without repeating an already-completed merge.
For this fresh acceptance of already-integrated work, auto and always require an
independent PASS or new explicit human acceptance. The reviewer or user must
examine the complete implementation against requirements using code and Git
history, since the now-empty base diff cannot describe the original work.
If the scope cannot be established, review must be
INCONCLUSIVE. Policy off still requires fresh validation. Do not reset the base,
invent a PASS, or edit tracker state to bypass recovery.

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
Reconciliation of an already Done parent checks surviving local and live origin
refs independently of cleanup settings, without rewriting tracker state. Each
head must match the exact merged PR in GitHub mode or be integrated into the
local base in local mode. Ambiguous or unmerged refs refuse reconciliation;
current-branch and worktree protections apply only to deletion. If all task
refs are absent, reconciliation is a safe no-op and reports no merge identity.
Keep cleanup failures separate from successful completion when reporting results.

## GitHub review

`workflow.review.github_provider` selects auto/local/codex/claude. Auto uses local
fresh review without assuming a cloud reviewer is installed. Explicit cloud Codex
uses `@codex review`; its mention interface cannot pin a model or effort. Claude
uses the repository's Claude Action; configure model/effort in `claude_args`, not
in a comment pretending to override the Action. The command checks configured
pins and reports unsupported configurations. `wait_seconds` bounds waiting.
Silence or a stale review never means acceptance; allowed fallback uses local
fresh review. GitHub approvals and required checks still follow repository policy.
