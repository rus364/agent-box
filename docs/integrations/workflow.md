# Task workflow

Enable **workflow** in `agent-box config`, configure tracker access, and run
`ab-setup` in a repository. This installs eleven short owned skills:
`ab-tracker`, `ab-setup`, `ab-adopt`, `ab-normalize`, `ab-interview`, `ab-plan`, `ab-work`, `ab-sync`, `ab-review`,
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
`workflow.scout.limit`. A finished sibling unblocks after integration; a dependency in
another parent remains blocked until that parent's merge.

`workflow begin CHILD` records the initial head only for a new branch claimed
from Ready to Doing, in the existing local evidence. A child still at that head
can finish without commits after
`workflow validate CHILD -- COMMAND ARGS` and the configured review. Resuming
Doing/Review work (even with a missing branch) or adopting a remote branch does
not create or reset this evidence. Child finish saves acceptance before
integration/publication, so retry `workflow finish CHILD`
after a failed Done write. It rechecks the accepted head, base and current
review policy. GitHub auto-push publishes the current parent tip containing the
accepted child before Done, including after later parent work. Lost acceptance
of an integrated change cannot be replaced by
validating its now-empty diff; preserve the branches and restore the original
acceptance or reconstruct the original change. Missing creation evidence also
prevents claiming that an already-integrated child began without commits.

The coordinator owns requirements, lifecycle state, assignees, relations,
dependencies and progress checkboxes. Plan sync may update descriptions and
journal comments from accepted decisions. The coordinator alone creates
branches, commits and pushes; it alone touches
GitHub, pull requests and merges. Tracker comments and descriptions use the
task's language; everything handed to an agent is English.

A worker reads its brief and the repository, changes project files in the working
tree, and answers in Markdown. It never commits, pushes, switches or creates a
branch, never writes the tracker, and never writes GitHub. Scouts are read-only
and answer with facts, sources and uncertainty. Reviewers are read-only and return
violated-requirement findings for the coordinator to journal and reconcile. A
checkbox is not a PASS.

A native subagent of the coordinator's session and a managed `codex exec` /
`claude -p` run share exactly this logical contract. It is a contract boundary,
not credential isolation - a native delegation inherits the coordinator's
workspace, `.git` and credentials - so neither backend gets private
restrictions.

Tracker comments carry no workflow state at all. Branch, base and pull request
come from Git and GitHub, and lifecycle comes from the tracker's own fields.

### Workers and scouts

Small bounded edits are the coordinator's own work: commit, validate, review by
policy; nothing else is recorded. For a separate implementation assignment or a
bounded research question the route comes from the role's model list, and
`workflow inspect TASK` names it under `execution.worker` and `execution.scout`.
An empty list (`self`) means the coordinator does that work in its own session,
never through a general-purpose subagent. A configured list (`delegate`) names
the native profile for the provider of its first candidate; a coordinator of that
provider spawns it, any other launches a managed run, so the first candidate is
never skipped for a later fallback:

```bash
agent-box workflow delegate TASK --role worker --brief /tmp/brief.md
agent-box workflow delegate TASK --role scout --brief /tmp/question.md
```

`delegate` takes the role's first available candidate (see
[models and routing](#models-and-routing)), runs it once as a fresh managed CLI
process and prints its Markdown answer on stdout. The route, token usage and the
path of a private copy of the answer follow on stderr as one JSON line. The prompt
asks for `## Done`, `## Evidence` and `## Limitations`; nothing parses the answer.
A failure prints JSON naming each tried candidate's category and the path of its
private diagnostic. Workflow records nothing about the run and checks nothing
afterwards, so editing the task description or committing while a delegate works
affects no later command.

A managed worker needs the task branch checked out and holds the local work lock,
so validation cannot run under it. A worker failure other than an unavailable CLI
or model never falls back to another candidate, because files may already have
changed. Managed scouts may overlap up to `workflow.scout.limit` (default 3),
counted by per-repository slot locks that a finished or killed process releases;
the coordinator counts its own native scouts. With no candidate configured for the
role, `delegate` refuses and the coordinator does the work in its own session.

With workflow enabled, interactive `agent-box codex` and `agent-box --host codex`
launches derive native Codex capacity from the effective `workflow.scout.limit`,
including project overrides. Codex V1 receives that many subagent slots; V2
receives one additional slot for the coordinator. For example, a scout limit of 6
sets `agents.max_concurrent_threads_per_session=6` and
`features.multi_agent_v2.max_concurrent_threads_per_session=7`. Codex shares this
capacity across native roles; the coordinator still enforces the scout count.
These launch-time overrides do not depend on `agents.codex.native_agent_wait.enabled`
and do not write `~/.codex/config.toml`. Batch, managed and service commands
(including `app-server`) receive no coordinator overrides. Already-running
sessions and independently launched IDE/app-server sessions retain their own
Codex settings; start a new interactive agent-box session to receive this policy.

The brief is a compact English file outside the working tree: goal, scope and
relevant paths, decisions and limitations, and for a scout its one question. Never
pass conversation, tracker comments or other agents' history. Every assignment gets
a fresh agent. The coordinator verifies the answer against the code, commits what
it accepts and reverts the rest.

Claude managed runs use `--safe-mode`, which keeps the context free of the user's
hooks, skills, MCP servers and agents - and also disables CLAUDE.md discovery. The
repository's own `AGENTS.md` and `CLAUDE.md` therefore travel completely in the
prompt (or a reviewer's mandatory evidence), together with instructions to read
nested files and @path imports. Read errors and invalid UTF-8 fail explicitly; text
is not silently truncated. Symlinked instruction files are excluded with an
explicit limitation.

### Plan sync

**Workflow → Plan sync** configures a separate agent that reads Git, documents
and the live tracker itself. It starts from the source decisions and finds candidates
by changed paths, anchors, domain terms and relations, including tasks without links.
Scope bounds discovery; it does not require reading every task or its history.
It changes descriptions only for a concrete stale assertion, a decision changing
scope or acceptance, or a missing/stale reference needed by an affected task.
No informational recaps, commit hashes, sync-task bookkeeping or copies of workflow
instructions belong in descriptions. No correction means no write.

Reference documents use `path#heading` plus purpose. It preserves the existing
reference section's heading, language and location (for example, «Опорные документы»);
when absent, it adds one in the task's language under Context. It preserves
requirements and progress marks, verifies saved descriptions, and reports unresolved conflicts.
It never edits project files, Git, lifecycle or dependencies. Reviewers still
judge repository evidence; they do not maintain live tasks.

| Setting | Behavior |
| --- | --- |
| `workflow.sync.policy` | `off` (default), `auto`, or `always`; independent of review |
| `events` | Default `plan` after planning or an accepted decision and `after` integration; optional `before` implementation |
| `paths` | Auto after-event filter, default `docs/**` and `*.md`; `*` also matches `/` |
| `scope` | `project` (default, respects recursive binding) or source `parent` tree |
| `references` | Maintain reference-document sections (default true) |
| `models`, `tiers.N` | Own ordered candidates, effort/speed/API and normal task tiers |

For every task, choose `always` and the desired events. Auto runs selected
before/plan events and after events whose committed changes match `paths`;
missing source commit evidence triggers inspection rather than a silent skip.
Auto after-events ignore changes confined to an intact, checksum-verified managed
`workflow` block in root `AGENTS.md`, comparing both committed versions. Project
text outside that block and other matching paths still trigger sync. Missing files,
damaged blocks or unreadable contents cannot qualify for this exception. Manual
runs and `always` retain their selected scope and event behavior.
Explicit event lists replace the defaults, including in project overrides.
Managed runs use the shared `workflow.timeout_seconds`.

Begin, child finish and parent merge return a Plan sync request. The coordinator
handles it through `ab-sync` before starting affected work; plan/interview invoke
the plan event through the same skill. After means the code has already passed
its configured review and been integrated. `workflow inspect TASK` retains the
last request/result for recovery. Repeating a completed after event at the same
head does not request another run.

```bash
agent-box workflow sync TASK                 # manual, even with policy off
agent-box workflow sync TASK --event plan    # applies automatic policy
agent-box workflow sync TASK --native        # message for configured native profile
agent-box workflow sync TASK --result /tmp/sync-report.md
agent-box workflow sync TASK --reconcile /tmp/sync-recovery.md
```

`ab-sync` prefers the first candidate's native profile when actually available
with the requested settings in this session; otherwise it uses managed execution.
This skill and `workflow sync` are coordinator operations. The already launched
sync executor uses `agent-box tracker` directly; it never starts another sync or
runs workflow commands. Tracker description/comment writes need network access,
not filesystem writes. Managed Codex/Claude executors, including external API
routes, receive `AGENT_BOX_SYNC_EXECUTOR=1` only in their child environment.
`workflow sync` rejects marked callers before configuration, tracker access or
local locks, including `--native`, `--result` and `--reconcile`. Tracker commands
remain usable. This diagnoses accidental recursion, not credential isolation;
native subagents follow the role contract without this process marker.
The coordinator passes a task reference and scope, never assembled task texts or
diffs. Empty models require configuration; the coordinator never substitutes
itself. Session `--sync SPEC` pins the managed route. `config apply` installs
`ab-sync` and nonempty tier profiles.

Codex sync uses a read-only filesystem permission profile with network enabled.
Managed Claude exposes Read/Grep/Glob and allows Bash for tracker CLI and read-only
Git commands, without Edit/Write. Native Claude inherits session shell permissions;
its no-file-edit rule is a role contract, not filesystem isolation. Both routes
use tracker CLI binding and revision guards (`update --revision REV`), reread
before edits and confirm saved text. The executor itself edits open tasks in scope,
including Backlog, Ready, Doing and Review; being open never makes a task read-only.
Only Done tasks remain historical evidence: text superseded by accepted decisions
and current documents is not a blocker. Lifecycle, assignees, dependencies and
progress marks remain unchanged; actual decision conflicts require clarification.

Results are `UPDATED`, `UNCHANGED`, `BLOCKED` (a decision/clarification is needed),
or `ERROR` (reading, writing or verification failed), never review acceptance.
Native and managed reports accept the same outcomes; runner failures also record
ERROR. Both BLOCKED and ERROR can follow partial writes: retain the report and
reconcile live tasks before retrying, especially after an unknown write outcome.
No provider fallback runs after possible writes; failure before launch may use
the next configured candidate. Requirement conflicts return to ab-interview.

`workflow sync TASK --reconcile FILE` records the coordinator's recovery after the
previous executor has stopped and possibly changed tasks have been reread. The
report describes verified writes and resolved/remaining blockers, ending with one
`Sync: UPDATED|UNCHANGED|BLOCKED|ERROR|REQUIRED` line. UPDATED/UNCHANGED closes the original
scope; count earlier writes when choosing UPDATED. BLOCKED retains decision issues,
ERROR retains technical failures; REQUIRED permits a fresh run after partial writes
have been checked and more synchronization remains. A historical Done
task alone can be dismissed without another model run once the remaining scope is
verified. Reconciliation invokes no model and edits no tracker tasks or Git; it
preserves the original report, diagnostics, event and HEAD alongside the recovery
report. A new HEAD still needs its own synchronization. Pending/error/BLOCKED
results cannot be silently overwritten by a new run. Use `--result` for a completed
native answer; use reconciliation for an interrupted executor or a corrected
assessment, including a native spawn that failed before work began.

### Reviewer rounds

A review round runs managed, native or, for a parent whose route is a cloud
reviewer, in its pull request (see [cloud review](#cloud-review-in-the-pull-request)).
`workflow inspect TASK` exposes `execution.reviewer`: for a local round the
coordinator uses the first candidate's native profile when available in its session
with the configured model, effort and speed; otherwise it uses managed execution.
An empty reviewer list requires an independent native reviewer, never self-review.
Cloud parents keep their pull-request route.
Select the backend anew for every new round. An earlier thread-limit error from
another assignment or review is not proof that native launching is still
unavailable. When the current route, tool and profile support native execution,
attempt a fresh spawn. If it fails, explain the current reason and use the
configured managed route within the existing failure budget; do not loop spawn
or skip the preferred candidate solely because of a thread limit. A later round
rechecks native availability. An empty reviewer list still has no managed route.
`workflow review TASK` runs the first available reviewer candidate as a managed CLI. `workflow review TASK --native`
writes the same input package and prints `message`, the prompt for a fresh
read-only subagent the coordinator starts without its own history; the
coordinator saves the subagent's final message and records it with
`workflow review TASK --result FILE --provider PROVIDER --model MODEL [--effort EFFORT]`.
The native result is taken on trust: the named model and the freshness of the
context are the coordinator's word, not something workflow can check. Only the
latest round can be answered, and a new round removes the input of an unanswered
native one. `workflow inspect` names the route that will answer under
`review.route`, so no skill has to infer it from settings.

The reviewer answers in Markdown with exactly one line
`Verdict: PASS|NEEDS_WORK|INCONCLUSIVE` and one bullet per finding,
`- [LOW|MEDIUM|HIGH|CRITICAL] path:line — text`; its instructions define each
severity. Workflow reads only the verdict line and the finding severities, and
the rest stays prose for the coordinator. No verdict line, more than one, or a PASS
that lists findings is a failure rather than a guess. A JSON schema was the rejected alternative: external
APIs ignored it, and a valid verdict wrapped in narration then cost a whole round.

Successful managed runs return `usage` token counters through workflow output;
native and unavailable counters remain null. Codex turn counters, including
cache-write input and reasoning output tokens, come from JSONL; Claude counters
come from the result envelope. A CLI's own cost figure is not kept: for an
external API it is the CLI's price estimate, not the provider's bill.

## Models and routing

Each role owns an ordered candidate list: `workflow.scout`, `workflow.worker`,
`workflow.sync` and `workflow.review` each hold `models`; workers, sync agents and reviewers also hold optional
`tiers.N` for task tiers 1-3. Scouts have no tiers. A candidate names the CLI that
runs it and what it runs: `provider` (`codex` or `claude`), `model`, `effort`,
`speed` (`inherit`, `standard`, or `fast`; default `inherit`) and `api_provider` (empty for the built-in API). Roles
never share or inherit each other's lists.

The first available candidate runs and the rest are ordered fallbacks. This holds
for every role, reviewers included: the list order states the preference, whoever
wrote the code and whichever CLI coordinates. A candidate is unavailable when its
CLI is not installed or, for a subscription model, a known usage window is
exhausted with a future reset. `workflow delegate` runs a Codex scout from a Claude
session, and a Claude worker from a Codex session, as a managed `codex exec` /
`claude -p` process, and `workflow review` does the same for the reviewer list
(see [external providers](external-providers.md#workflow) for candidates on an
external API).

The lists are edited in the configuration menu (add, reorder, change effort or
speed, remove; the picker offers every CLI's models together), shown in
`config show` as `PROVIDER[@API]:MODEL:EFFORT speed=MODE`, and travel in
the container's minimal workflow configuration. The effort value `default` omits
agent-box's effort override; it does not disable thinking or pass a literal `none`
value to the provider. A new candidate starts at `medium`. Lists start empty
rather than guessing what the account can access.

### Task tiers

A task may carry one `ab:tier:1`, `ab:tier:2` or `ab:tier:3` label. It selects
models only while `workflow.use_tiers` is on (default off): set it globally, in
`.agent-box.json`, or for one session with `--tiers` / `--no-tiers`. Off, every
worker, sync agent and reviewer runs its role's `models` whatever the task's tier; tier lists
and tier labels stay as they are. When and how a tier is assigned is decided in
ab-plan.

| Worker, sync or reviewer work | Candidates |
| --- | --- |
| Session pin (`--worker SPEC`, `--sync SPEC`, `--reviewer SPEC`) | The pinned model only |
| Tiers on, task tier N with a nonempty `tiers.N` | `tiers.N` |
| Otherwise, nonempty `models` | `models` |
| Otherwise | Reviewer: native review only (`review --native`); `delegate` and sync refuse |

Scouts run `workflow.scout.models`, or `--scout SPEC`. Model selection never
changes tracker labels or invents a tier. If no candidate is eligible, selection
fails with an explicit error; it never falls back to another list, to the session
or to another role's list.

`agent-box usage --json --no-cache` requests live usage. A failed refresh has
`fresh: false`, retains any useful cached exhaustion evidence and exits nonzero.
Model selection keeps skipping a candidate whose retained window is exhausted
with a future reset; a failure without such evidence excludes nothing.
The provider APIs may themselves delay updates; bypassing the local cache cannot
remove server-side lag.

### Reviewer fallback

A managed round advances to the next reviewer candidate only after observed
CLI/model unavailability, authentication, quota or expiration of
`timeout_seconds`, all within the same round. Unknown errors, unsupported primary
settings and an answer without a verdict line stop that run as a failure.
Unusable backups do not block the primary. Speed, cost, preference, silence
before timeout and review verdicts never justify switching provider. Missing CLIs
and explicitly empty models may exclude candidates before the round starts. An
unsupported model is an error, not permission to guess a cheaper one.

A project can replace any role list in `.agent-box.json` without
changing the global configuration; a project list replaces the global list whole.

`config apply` installs owned native agent profiles with exact model/effort, one
per CLI for each role's `models` (`ab-scout`, `ab-worker`, `ab-sync`, `ab-reviewer`) and for
each nonempty worker, sync or reviewer tier list (`ab-worker-N`, `ab-sync-N`, `ab-reviewer-N`); each
takes the first candidate of that CLI on its built-in API. A profile agent-box no
longer defines is removed while its content is unedited. A profile is a
convenience for native subagents, not proof of the live tool's capabilities.

Effort `default` omits agent-box's effort override. This is not a literal provider
effort value or a promise to disable thinking.

`workflow delegate` and `workflow review` execute managed `codex exec` or
`claude -p` inside the existing host/container. They cannot invoke the
coordinator's native tool; a native reviewer uses `review --native`, a coordinator
spawn, then `review --result`. See [native research and smoke evidence](../native-launch.md)
for observed interfaces. Provider errors, including Claude exit-zero error
envelopes, are not review findings. A worker failure after possible side effects
requires reconciliation.

## Validation, review and completion

```bash
agent-box workflow begin TASK
agent-box workflow delegate TASK --role worker --brief /tmp/brief.md
# The coordinator verifies the answer against the code and commits what it accepts.
agent-box workflow validate TASK -- python3 -m unittest discover -s tests
agent-box workflow review TASK
agent-box workflow finish TASK
```

Acceptance belongs to code: validation, review and human acceptance are recorded
for the task's head and base commits, and those two commits are recorded with
them. A new commit or a moved base retires all three; editing the description,
checking items, changing the tier, relations or hierarchy retires nothing. The
recorded pair is what still names the original diff once the base has
fast-forwarded past the head, where Git can no longer derive the fork point. Move anything important from a discussion into the
task description, since that is what the reviewer reads. Each description file
is headed with the task's visible number, ID and URL, so a card that cites
`#N` or an ID maps to the other. The reviewer judges only what the repository
shows. Requirements about tracker state (other tasks' descriptions, relations or
states) or operator actions are closed by the coordinator. The reviewer lists
them under limitations, and they are neither findings nor a reason for
INCONCLUSIVE. Landing requires the whole checklist closed, so the coordinator
records its evidence there. A reviewer that judged tracker claims from an
untrusted brief was the rejected alternative: the same change passed or stayed
INCONCLUSIVE depending on how the brief was worded.

`--brief FILE` is optional for review: a short English note on what the tracker
lacks, such as technical risks or validation limits. Keep it outside the working
tree (`mktemp /tmp/agent-box-review.XXXXXX`) and remove it after the round.
Acceptance operations require a clean tree including untracked files; an untracked in-tree
brief blocks review even when no branch switch is needed. An acceptance or validation item
only the user can close, such as a check that cannot run from a container, is asked for by landing
alone, which reads the whole tree, so nothing merges while one is open. A review round,
`approve` and `finish` never wait for it: a checkmark is the coordinator's progress claim, while
one open item would otherwise stop the subtask, its parent's move to Review, the pull request
that move is the precondition for, and the cloud round that lives in it.
Every native verdict uses a newly started reviewer in fresh context without the
parent's history, never a resumed one; pass the returned `message` unchanged as its
whole prompt. Managed commands remain ephemeral/no-session-persistence and do not
resume a prior review. The bounded handoff
names base/head boundaries, a diff shortstat and a manifest (path and SHA-256) of temporary
read-only files: `task.md` with the complete task description, `parent.md` for a subtask or
one `subtask-N.md` per subtask for a parent, the full diff, the changed-path list, recorded
validation, repository instructions and, when given, the brief as `author-report.md`.
Descriptions keep their language and headings; checkmarks are cleared, and comments,
history and earlier verdicts are never included. A description of any size stays in its
file, outside the handoff limit. The reviewer reads descriptions and diff before the brief. The prompt also states the round's
own boundary - read-only, with no way to run the suite - so recorded validation is read as the
only run there is instead of being rediscovered every round.
Workflow state keeps native and managed paths shared and removes the generated
artifacts once the round's result is recorded or its managed run fails; oversized
index metadata fails closed.

The shared prompt checks correctness and unnecessary complexity against current requirements
and consumers. Complexity findings need a concrete present maintenance/cognitive cost or
state-divergence risk and a simpler behavior-preserving alternative, even without a runtime
bug. Local fixes/reuse come first; redesign needs justification, required checks remain,
and style preferences or hypothetical futures do not block PASS. The coordinator verifies
findings before fixing, challenges them with evidence and escalates material disagreement
without inventing independent acceptance. No extra review stage or finding quota is added.

Accepted reviewer evidence records the route that ran and any provider failures
of earlier candidates. A native route is the coordinator's statement, not provider
attestation. Evidence is disposable local cache bound to the head and base; another
device may need to rerun validation/review.

Review policy is off/auto/always. Auto considers explicit review labels, sensitive
paths, changed-file count and added/deleted lines (binary changes require review).
`sensitive_paths` and `ignored_paths` are `fnmatch` globs against the
repository-relative path, also tried with a leading `/`: `*` matches across `/`,
so `*.md` and `**/*.md` both match Markdown anywhere, `docs/**` matches under the
top-level `docs`, and `**/auth/**` matches an `auth` directory at any depth. A
changed path matching `ignored_paths` leaves the file and line counts, so a
documentation-only change with `["**/*.md", "docs/**"]` needs no auto review. A
rename leaves the counts only when both its old and new names are ignored. A
path matching both lists stays counted and triggers review as sensitive. Ignoring
never changes `always`, labels, scope, validation or the diff a reviewer reads.
`agent-box workflow inspect REFERENCE` is read-only and works before begin, from another
branch or with uncommitted files. Like `tracker get`, it accepts a visible number,
exact title, task URL or canonical ID, refusing ambiguity; other workflow commands
still take canonical IDs. It reports current/found branches, effective
configuration, review budget, acceptance availability/reasons and a next step. That step is
derived from what is recorded for this head rather than from the shape of the cycle: a branch
still at its fork point asks for work, a head without successful validation asks for it, a head
that still needs acceptance asks for the review route, and an accepted one asks for finish or
landing. A Done task points at its parent or at the next task, never at the branch finish
released.
`scope` is null until the expected task branch is clean; scope-dependent review
and validation fields are unavailable too. On that branch it returns the exact
scope, validation and every review trigger. Ambiguous refs or failed Git/tracker
reads remain errors. Thresholds
are inclusive: two files trigger review when `max_files=2`. `config show` reads
the same rendered configuration inside a container; host-only settings are not
part of that artifact.
Priority is unrelated. Presets are thorough (2 files/80 lines), balanced (5/200)
and light (10/500); all values and paths are editable. Apply the policy both to
subtasks and the complete parent diff. SKIPPED, INCONCLUSIVE and provider errors
are distinct from PASS. A new commit or a moved base invalidates acceptance.

`workflow.review.incremental` (default `off`; `LOW`, `MEDIUM`, `HIGH` or
`CRITICAL`) saves rounds on follow-up commits. When the task's own latest round was
applied and was PASS, or NEEDS_WORK with no finding above the level (`CRITICAL`
allows any), the triggers above run on the delta from that round's reviewed head to
the current head instead of the whole diff. A delta that triggers nothing is
*accepted by the delta threshold* of that round: `workflow review` starts no round,
and finish, local landing and GitHub merge accept it in place of a PASS. It never
becomes a PASS; `inspect` and `land inspect` report it under `delta` with the
round. The delta always starts at the reviewed head, so small steps add up. Only
the task's own rounds count: a parent's first review is always of its whole diff,
and subtask rounds never shorten it. Without such a round (stale, superseded,
INCONCLUSIVE, an expired result, or history that no longer descends from the
reviewed head) and for an already-integrated parent, the whole diff decides.

`max_rounds` (default 3) counts received verdicts: a round spends an attempt only
when its verdict arrives, whether PASS, NEEDS_WORK or INCONCLUSIVE, managed or
native. Known managed launch constraints (missing CLI, unsupported route, another
local review running) are checked first; their refusal opens no round and counts
nothing. Preflight checks local executables and supported routes, without guessing
live model or account availability.

A failure spends no attempt and may be rerun: the process was killed, the provider
failed or timed out, a native reviewer never answered, or the answer had no verdict
line, which also ends a native round. Failures are counted in a row, and every
round start counts as one until its verdict clears it, so a killed run or an
abandoned native reviewer that could record nothing still counts. After 2 failures in a row every review
of the task stops until the user decides; a received verdict resets the count.
The limit is fixed.

Budget state is keyed by checkout, tracker provider/server/project and task,
survives scope changes and coordinator restarts, and never resets automatically.
`workflow inspect` exposes `review.budget` with configured_limit, effective
limit, used, remaining, failures and failure_limit; reviewer command results
expose `review_budget` for the coordinator, outside the neutral reviewer prompt.
On the user's explicit decision the assigned coordinator of a Doing/Review task
runs `workflow review-budget ID --reason TEXT [--limit TOTAL]`. It always clears
the failure count; `--limit` sets an absolute nonnegative ceiling (with three
attempts used and three more authorized, set six), so repeating it adds nothing.
It preserves used attempts, a pending round and acceptance evidence. The explicit
ceiling overrides configured max_rounds for this task, including after scope or
configuration changes; zero stops further attempts without erasing usage. There
is no other way to reset either counter.

Every new round retires previous PASS and human acceptance under the budget lock,
including one that later fails. Result ingestion uses the same lock and accepts
only the latest pending round; late or replayed results cannot restore acceptance.
Finish/landing consume freshly reread evidence under that lock; validation rereads
before saving so a revoked PASS cannot return. A request published in a pull request
runs through the same locks, budget and receipts as a local round. Skills stop on
ambiguity or no progress and retain human
iteration history in the tracker. Validation is a real command with an observed
exit status; the coordinator does not self-review.

## Landing mode

`workflow.merge_mode` selects `github` (the default) or `local`. Both routes
derive the base from origin's default branch and retain scoped validation,
configured local review policy, ownership, dependency, subtask and checklist
checks. `workflow finish PARENT` moves accepted parent work to Review and points
the coordinator at the configured route. With a cloud reviewer it asks for
validation alone, because the reviewer reads a pull request that does not exist
yet; the verdict is what the merge requires.

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
After confirmation, publication updates only the remote-tracking ref selected by
origin's fetch refspec, using its pre-publication value as a compare-and-swap
guard. Effective fetch and push URLs must match after Git's URL rewrites; missing,
excluded, ambiguous or non-tracking mappings and symbolic refs are skipped.
`publication.tracking` reports `updated`, `skipped` (with a reason), or `failed`.
A local tracking failure leaves publication successful and does not repeat the
push or merge, block Done, or update another local branch. A later fetch can
refresh the tracking ref.

For any task, subtask or parent, in either merge mode, the coordinator can record
the user's explicit acceptance of the current head as an alternative to a required
independent review:

```bash
agent-box workflow approve TASK --reason 'User accepted the inspected result'
agent-box workflow finish TASK
```

It needs successful validation of the same head, and the ownership and dependency checks.
`approve` records the head, actor and reason as human acceptance; it does not
write a reviewer PASS, change policy or mark the task Done. As with reviewer
reports, this is a coordinator contract, not authentication of the human's
identity. A request to avoid delegation alone is not approval. A new commit, a
moved base or a later review round retires the decision. Ownership, dependencies,
validation, the checklist and merge proof remain mandatory for completion, and the
normal post-merge cleanup still runs.

`workflow land PARENT merge` is one idempotent command: it checks acceptance of
the task head, fast-forwards the base, publishes it, marks the task Done and runs
cleanup. If any step fails, the merge and the task branch are preserved, the task
stays Review, and running the same command again continues from the step that
failed. A head the base already contains is not merged a second time; publication
of anything but the accepted head is refused, so an advanced local base stops the
retry, and an already-published exact head lets it finish after a tracker
failure. An explicit auto_push=false permits local-only recovery, and a merge of
an already Done parent only retries cleanup, never publication.

The retry proves acceptance again from the two commits the acceptance names, so
the current off/auto/always policy applies to the original diff rather than to
the empty one the advanced base now shows. A changed task head invalidates
acceptance even if the new head is also integrated. Git remains the authority for
integration; the recorded pair neither records lifecycle nor selects branch refs.
A refusal leaves the parent in Review.

Manual merge or ancestry alone cannot complete an unaccepted parent. When no
acceptance is recorded for the head, including in a new clone, switch to the
current task branch, run `workflow validate PARENT -- COMMAND ARGS`, obtain fresh
independent review with `ab-review` as required, then run `workflow land PARENT
merge` again. If the base already contained the accepted head, its diff is empty
and describes no work at all: only an independent PASS or explicit human
acceptance of the complete implementation, read from requirements, code and Git
history, can cover it, whatever the review policy says. If the scope cannot be
established, review must be INCONCLUSIVE. Do not reset the base, invent a PASS,
or edit tracker state to bypass recovery.

Local mode rejects `workflow pr` before any GitHub call, and has no pull request
for a cloud reviewer to answer in: every round is a local one, whatever
`github_provider` says.

## GitHub landing

In the default GitHub mode, the enabled GitHub integration and workflow
`auto_push` (on within this opt-in workflow) push only the parent after a
completed subtask. Push checks the live
remote default branch, unique effective destination, clean worktree and ancestry;
no forced repair, tag/submodule side pushes or child-branch auto-push.

After parent acceptance the configured route continues without asking: `ab-pr`
opens one PR, following repository templates. Later commands find that PR on GitHub by head branch and
base rather than from a stored number: an open one wins, a single merged
one answers a repeated merge, and several of either stop the command. Completion from a
merged PR additionally requires its merged head to be the branch's current head,
so commits added after an old merge on a reused branch cannot ride that merge to
Done - squash and rebase merges rewrite the commit, which leaves head equality as
the only honest test. `ab-land` reads
CI, comments and paginated unresolved threads, fixes sequentially, obtains
current-head acceptance and merges with `--match-head-commit`. Branch protection
is never bypassed. Merge-queue entry is not completion. If the tracker write
fails after merge, `workflow land PARENT merge` continues from there: a PR GitHub
has already merged is never merged again. Before changing an unfinished parent to
Done it requires successful validation and the acceptance its review policy asks
for, of the merged head; with a cloud provider that verdict is the one
`workflow review PARENT` read in the pull request. A merged PR proves
integration, not acceptance, and a current-head human `APPROVED` is acceptance
only when it was observed before the merge, since GitHub binds reviews to commits
and not to local evidence. Approvals are read from the reviews endpoint, which
alone reports both the reviewed commit and whether the account is a person: an
app's `APPROVED` is not acceptance, or a cloud reviewer could accept the work it
was asked to judge by pressing a button instead of answering. Missing local
evidence, including in a new clone, requires fresh `workflow validate` and
ab-review as required from the parent branch, then the same merge command again. If the base already contains the implementation, an
empty diff describes no work: only an independent PASS or the user's explicit
acceptance of the complete implementation covers it, whatever the policy says. A merge of an already Done
parent runs cleanup alone and does not write lifecycle.

## Branch cleanup

`workflow.cleanup_local_branches` and `workflow.cleanup_remote_branches` are
independent booleans, both off by default. They are editable in the workflow
config menu and visible in `config show`. With either enabled, landing attempts
cleanup only after proving the merge and successfully marking the parent Done. Cleanup outcomes appear separately under `cleanup` in
the result; a cleanup failure never reverses merge or Done. With both disabled,
cleanup performs no additional Git or GitHub operations.

Cleanup covers the completed parent and its completed descendants, including
nested tasks discovered from the live tracker hierarchy. Unfinished descendants
receive read-only ref discovery: each enabled local/remote outcome reports a
unique `branch`/`head` as `preserved` with the task state, no refs as `absent`,
or ambiguity as `error` with `candidates` containing branch/head pairs and a
reason. Remote read failures remain separate errors; unfinished tasks never
trigger merge proof or deletion. Each task has independent local/remote
outcomes; descendant results appear in `cleanup.descendants`, and hierarchy read
failures in `cleanup.descendants_error`. Candidates come from each task namespace
in Git, including live origin refs when remote cleanup is enabled. It never
guesses a missing branch from the task's title. Ambiguous names, symbolic refs,
the base/default branch, the current branch and branches used by any worktree
are preserved with a diagnostic. This also protects the remote copy of a branch
that is checked out locally. Cleanup never switches branches.

In GitHub mode, cleanup requires the enabled GitHub integration and one exact
merged PR head/base with a merge timestamp and merge commit. The candidate tip
of the parent must equal the merged head. Descendant heads must be ancestors of
that exact parent PR head, so squashing or rebasing the parent does not erase
their integration proof. A missing parent ref can recover proof from a unique
merged PR in its task namespace; missing or ambiguous PR evidence preserves
descendants. After GitHub landing, the parent may still be checked out: switch
to the base yourself and run `workflow land PARENT cleanup`.
In local mode, every head, the parent's and each descendant's, must be an
ancestor of the local base, which works after parent refs disappear and needs no
acceptance evidence: the base itself is the proof that nothing is lost.
`workflow finish` releases a finished subtask's own branch the same way and
under the same two settings, against the parent branch that just absorbed it,
and preserves it with a reason when its head moved afterwards. Remote deletion additionally requires the candidate head in the
live published base on the same effective origin destination. An unpublished local merge, or a missing
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

`workflow land PARENT cleanup`, and a merge of an already Done parent, retry the
same configured policy for the parent and its live descendants without merging or
writing tracker state. Remaining descendants are considered even when the parent
ref is absent. Each surviving branch proves its own integration again; Done alone
is never merge evidence, and a branch that cannot prove it survives with its
reason instead of failing the command. Missing branches are safe no-ops,
including after a partial cleanup or a title change.
Keep cleanup failures separate from successful completion when reporting results.

## Cloud review in the pull request

`workflow.review.github_provider` selects auto/local/codex/claude. With `auto` or
`local` every round is a local one in both merge modes. With `codex` or `claude`
in GitHub merge mode, one thing moves: the *parent's* final review happens in its
pull request, where the bot already runs. Subtasks keep their local rounds, and
the review policy is unchanged — `off` requires no review anywhere, `auto` applies
the same triggers, `always` always asks. There is no separate rule demanding a
fresh PASS for GitHub landings.

`workflow review PARENT` is the whole interface. On a head with no request it
publishes one comment asking `@codex review` or `@claude review` for that exact
commit and returns; every later run of the same command reads the answer back, so
a rerun never asks twice. A round belongs to the bot it asked: after the
reviewer is changed the other bot's request and answer are not this round's, and the
new reviewer is asked its own question. A request already in the thread is this
round's only when its text is one this checkout published for this scope and this
reviewer, word for word:
anybody may comment on a pull request, and a question somebody else wrote, with
instructions of its own, is not the question this round asked. The request offers
no example verdict either, because an example is copied back sooner or later. A `--brief` note is published with the request, since
that is where the reviewer reads it, and so are the task descriptions themselves: a
local reviewer is handed them as files, while the bot reads only this comment, and a
pull request body is the author's summary rather than the requirements a verdict is
measured against. Everything the request quotes - the cards, the brief and the scope
note - carries its mentions defused, because a Claude Action triggers on a comment's
words and a card naming `@claude` would otherwise start a second, paid review from a
request addressed to Codex. A request that would exceed GitHub's comment limit is refused before
anything is published, because a truncated requirement is a requirement nobody read. The bot's answer becomes an ordinary round:
the same budget, the same failure limit, the same receipt, `reconciliation.status`
and `incremental` delta economy as a local verdict. `workflow review PARENT
--native` still opens a local round when the user decides the bot cannot answer.

Before publishing anything the command refuses a pin the reviewer cannot honour:
`github_model`/`github_effort` are rejected for Codex, whose mention interface
cannot pin them, and for Claude must already appear in the `claude_args` of the step
that uses the Claude Action, which must be a step and not a mention of one. The pull request must exist (`ab-pr`) and its head
must be the local head, because the bot reviews what GitHub has.

Answers are read as the bots actually publish them, verified live against this
repository's bots on 2026-09-17:

- Claude answers in the reviewer's own form because the request asks for it: a
  comment from `claude[bot]` ending in `Reviewed-Commit: <head>`,
  `Reviewed-Base: <base>` and one `Verdict:` line, with findings as
  `- [LOW|MEDIUM|HIGH|CRITICAL] path:line — …`. Its progress checklist lines are
  not findings. Both commits must be echoed back, so an answer still in flight
  when the base moved answers the older question and not this one; an answer that
  echoes no base at all ends the round at once, since that is the request form
  going unanswered rather than a question of scope. An answer about this head
  with no single verdict line ends the round at once as well.
- Codex answers in its own: findings arrive as a COMMENTED review bound to the
  commit, with the priority badge on each inline comment (P0 → CRITICAL, P1 →
  HIGH, P2 → MEDIUM, P3 → LOW), and otherwise a comment naming the same
  `Reviewed commit`. Nothing attached to the reviewed head is the no-findings
  signal, so the verdict is PASS with findings absent and NEEDS_WORK with them.
  Codex also reviews on its own, so only what it attached after the request counts;
  its English is never parsed, and neither is its 👍 reaction, which only appears
  once every review of the pull request has finished. A review in any other state
  is not its answer. Because Codex names only the commit, a head already asked
  about another base cannot be asked again: the command refuses and the work is
  committed again or reviewed locally.

A round whose own request is not in the thread yet publishes no second one: GitHub
may have accepted the comment while `gh` reported an error, or may list it a moment
later, and the wait budget ends that round if it never appears.

Only an answer published after the request counts, and only for the commit it
names, so a bot that reviewed the previous push cannot accept this one. Silence
is never acceptance: until `wait_seconds` (default 3600) the command reports that
there is no answer yet; past it the round ends as a failure that spends no
attempt, and the user decides what to do. A human `APPROVED` on the current head
remains acceptance in its own right, and GitHub approvals and required checks
still follow repository policy.

What the 2026-09-17 check covered live is reading both bots' answers on a throwaway
pull request and recording a verdict from them end to end. The request has asked
Claude for the `Reviewed-Base` line since then, and the probe answers predate that
form, so a live answer carrying it has not been observed; without it the round ends
as a failure that spends no attempt. Publishing the request
and the `wait_seconds` timeout are covered by tests alone: the bots answer for real
money, the budget for that check was two requests to each of them, and an hour of
silence cannot be observed cheaply.

## Saved review results and failed attempts

Completed native and managed reviews save a private immutable receipt before
input cleanup or applicability checks. `workflow review-result TASK --round=ROUND`
reads its summary without tracker access: the verdict, finding counts by severity,
route, usage, `result_path` with the complete Markdown answer and scope/head/base,
and `stream_path` when raw streams exist. Public output never carries the answer's
text. `workflow inspect` names the last received result and a pending round.

`workflow review-result TASK --round=ROUND --reconcile` retries application after
tracker/evidence errors without dispatching a model or spending an attempt. Applied
rounds do not apply twice. Changed code or base and superseded rounds retain their
answers but cannot accept new work, even after old refs are restored. Check
`reconciliation.status` separately from the verdict: `saved`, `error` and `blocked`
may be retried; `stale`, `superseded` and `invalid_input` cannot be accepted. CLI
application failures exit 2 with the saved-result summary. A receipt is operational
evidence, not an unforgeable credential; editing workflow state is not a recovery
procedure.

Uncommitted/untracked files or a different checked-out branch temporarily block
application; restore a clean task checkout, then reconcile the same round. If the
first budget-index write failed, the command discovers the receipt by its task/round
identity, checks its private `.seal` before restoring the index, and counts the
verdict then. An unavailable budget stays unknown and prevents application; it is
never reset. A missing or modified receipt or recovery seal refuses acceptance.

Task edits never make a saved result stale: only the head and base do. Review
labels still trigger live review policy, and reconciliation checks current lifecycle
and ownership. Completion gates continue checking dependencies and hierarchy
independently.

Receipts use `WORKFLOW_STATE/review-results`, outside the checkout, with 0700
directories and 0400 files. Per checkout, retention is 20 receipts, 32 MiB total,
8 MiB each and seven days. Saving prunes only verified owned files; unknown files
and symlinks stay untouched. No background cleanup or automatic paid rerun occurs.
Read the full answer locally and journal only necessary findings/evidence.

Every launched managed reviewer writes its raw provider streams (Codex JSONL or the
Claude envelope as stdout, stderr, and Codex's final message file) directly beside
the receipts under `review-results/<checkout>/streams/<task>/<round>/<run>`, one
numbered run per candidate, so a killed run leaves them too. A finished run's files
become read-only and are cut to 8 MiB each; a linked stream directory is refused. They exist to diagnose an
INCONCLUSIVE, a failure or a disputed result, for example a reviewer that claims
its input files were missing although workflow verified them; the coordinator does
not read them in normal work. They are removed when the task becomes Done (`finish`
of a subtask, landing of a parent) and otherwise bounded like receipts: 20 runs,
32 MiB and seven days per checkout. A failed removal is reported as
`review_streams_error` beside the completed result, which stays Done.

Managed provider failures also record the configured provider/model/effort/role,
exit code, elapsed seconds, response source, stream byte sizes, numeric usage and a
specific envelope, response or verdict reason. Missing counters remain null.
Fallback evidence keeps each failed attempt and the accepted answer's actual route.
Error envelopes never supply a task verdict, even when they contain one.

Raw output of any failed managed run, worker and scout included, stays in private
`WORKFLOW_STATE/provider-failures/<id>.json` files (directory 0700, files 0600).
Each contains safe metadata and base64 byte prefixes for offline decoding: at most
1 MiB each of stdout and the final result file, and 64 KiB stderr, with truncation
flags. Responses are limited to 4 MiB. Prompts and commands are not retained.
Public results carry only safe metadata and a relative artifact reference, never
raw output. Up to 20 artifacts are retained (about 55 MiB maximum); files older
than seven days and excess oldest files are removed when the next failure is saved.
Delete individual artifact files to remove them sooner; inactive state is not
cleaned by a background service.

## Model speed

Each candidate has its own `speed`: `inherit` (default), `standard`, or `fast`.
Choose **Speed** in the candidate menu; `config show` includes `speed=...` for
every candidate. Roles, tiers and fallbacks retain their own selection without
changing model, effort, provider order or review policy.

- `inherit` adds no speed override. Native agents use the parent session's
  speed. Managed processes use their provider defaults: Codex keeps
  `--ignore-user-config`, so it does not copy the interactive parent's settings.
- `standard` explicitly requests ordinary speed even when the coordinator uses
  Fast. Managed Codex passes `service_tier="default"` and
  `features.fast_mode=false` (Codex omits the service tier on the wire); Claude passes `--settings '{"fastMode":false}'`.
- `fast` requests faster responses at higher provider usage/cost. Managed Codex
  passes `service_tier="fast"` and `features.fast_mode=true`; Claude passes
  `--settings '{"fastMode":true}'`. Provider access is still required.

Explicit speed uses managed CLI for every role. Codex 0.155.1 overwrites a
native child's service tier with the root session's tier after applying its
custom profile; a profile cannot pin independent speed. Claude native profiles
also inherit speed. `workflow inspect` reports this limitation; `config apply`
installs native profiles only for `inherit`, removing only intact owned profiles
for explicit speed and preserving user edits.

External API candidates support `inherit` only: their endpoints have no shared
standard/Fast contract. GitHub-hosted reviews have no independent speed control
here; use local workflow review for configured speed. Ordinary interactive
launches preserve provider-owned settings and user CLI arguments.

Fast model support: Codex exact IDs `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`,
`gpt-5.6-luna`, `gpt-5.6`, `gpt-5.5`, `gpt-5.4`; Claude exact IDs
`claude-opus-5`, `claude-opus-4-8`. Unknown Fast models are rejected to avoid a
silent model switch. Standard does not require a Fast-capable model. Claude
Fast on third-party cloud backends is unsupported.

Mechanisms checked 2026-09-22:
[Codex schema](https://learn.chatgpt.com/docs/config-schema.json),
[Codex 0.155.1 native spawn](https://github.com/openai/codex/blob/rust-v0.155.1/codex-rs/core/src/agent/control/spawn.rs),
[Codex speed](https://learn.chatgpt.com/docs/agent-configuration/speed),
[Claude Fast mode](https://code.claude.com/docs/en/fast-mode).
Tests prove configuration, routing and request construction, not account
entitlement, billing or delivered acceleration. Providers may deny or downgrade
requests due to account limits or policy. Config/apply runs no paid inference.
